"""Tests for skills/files.py module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config
from istota.skills.files import (
    get_local_path,
    list_files,
    mkdir,
    move_file,
    path_exists,
    rclone_list,
    rclone_mkdir,
    rclone_move,
    rclone_path_exists,
    rclone_read_text,
    rclone_write_text,
    read_text,
    write_text,
)


@pytest.fixture
def mount_config(tmp_path):
    """Config using local mount path."""
    mount_dir = tmp_path / "mount"
    mount_dir.mkdir()
    return Config(
        nextcloud_mount_path=mount_dir,
        rclone_remote="nextcloud",
    )


# --- Mount-aware file operations ---


class TestMountFileOps:
    def test_list_files(self, mount_config):
        mount = mount_config.nextcloud_mount_path
        (mount / "subdir").mkdir()
        (mount / "subdir" / "a.txt").write_text("hello")
        (mount / "subdir" / "b.txt").write_text("world")
        (mount / "subdir" / "nested").mkdir()

        items = list_files(mount_config, "subdir")
        names = {item["name"] for item in items}
        assert "a.txt" in names
        assert "b.txt" in names
        assert "nested" in names

        # Check is_dir flag
        dirs = {item["name"] for item in items if item["is_dir"]}
        files = {item["name"] for item in items if not item["is_dir"]}
        assert "nested" in dirs
        assert "a.txt" in files

    def test_list_files_nonexistent(self, mount_config):
        with pytest.raises(RuntimeError, match="Path not found"):
            list_files(mount_config, "nonexistent")

    def test_read_text(self, mount_config):
        mount = mount_config.nextcloud_mount_path
        (mount / "doc.txt").write_text("file content here")

        content = read_text(mount_config, "doc.txt")
        assert content == "file content here"

    def test_read_text_nonexistent(self, mount_config):
        with pytest.raises(RuntimeError, match="File not found"):
            read_text(mount_config, "missing.txt")

    def test_write_text(self, mount_config):
        write_text(mount_config, "output/result.txt", "test output")

        mount = mount_config.nextcloud_mount_path
        assert (mount / "output" / "result.txt").read_text() == "test output"

    def test_write_text_creates_parents(self, mount_config):
        write_text(mount_config, "deep/nested/dir/file.txt", "content")

        mount = mount_config.nextcloud_mount_path
        assert (mount / "deep" / "nested" / "dir" / "file.txt").exists()

    def test_mkdir(self, mount_config):
        result = mkdir(mount_config, "new_folder/sub")

        assert result is True
        assert (mount_config.nextcloud_mount_path / "new_folder" / "sub").is_dir()

    def test_path_exists_true(self, mount_config):
        mount = mount_config.nextcloud_mount_path
        (mount / "exists.txt").write_text("yes")

        assert path_exists(mount_config, "exists.txt") is True

    def test_path_exists_false(self, mount_config):
        assert path_exists(mount_config, "nope.txt") is False

    def test_move_file(self, mount_config):
        mount = mount_config.nextcloud_mount_path
        (mount / "source.txt").write_text("data")

        result = move_file(mount_config, "source.txt", "dest/moved.txt")

        assert result is True
        assert not (mount / "source.txt").exists()
        assert (mount / "dest" / "moved.txt").read_text() == "data"

    def test_get_local_path(self, mount_config):
        result = get_local_path(mount_config, "/alice/TODO.txt")
        expected = mount_config.nextcloud_mount_path / "alice" / "TODO.txt"
        assert result == expected

    def test_get_local_path_no_mount(self):
        config = Config(nextcloud_mount_path=None)
        result = get_local_path(config, "/alice/TODO.txt")
        assert result is None


# --- Rclone file operations ---


class TestRcloneFileOps:
    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_list(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([
                {"Name": "file.txt", "Size": 100, "ModTime": "2025-01-01T00:00:00Z", "IsDir": False},
                {"Name": "subdir", "Size": 0, "ModTime": "2025-01-01T00:00:00Z", "IsDir": True},
            ]),
        )

        result = rclone_list("nextcloud", "/docs")

        assert len(result) == 2
        assert result[0]["name"] == "file.txt"
        assert result[0]["size"] == 100
        assert result[0]["is_dir"] is False
        assert result[1]["name"] == "subdir"
        assert result[1]["is_dir"] is True
        mock_run.assert_called_once_with(
            ["rclone", "lsjson", "nextcloud:/docs"],
            capture_output=True, text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_list_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")

        with pytest.raises(RuntimeError, match="rclone list failed"):
            rclone_list("nextcloud", "/bad")

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_read_text(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="hello world")

        content = rclone_read_text("nextcloud", "/doc.txt")

        assert content == "hello world"
        mock_run.assert_called_once_with(
            ["rclone", "cat", "nextcloud:/doc.txt"],
            capture_output=True, text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_write_text(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        rclone_write_text("nextcloud", "/output.txt", "content here")

        mock_run.assert_called_once_with(
            ["rclone", "rcat", "nextcloud:/output.txt"],
            input="content here", capture_output=True, text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_write_text_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="write failed")

        with pytest.raises(RuntimeError, match="rclone write failed"):
            rclone_write_text("nextcloud", "/output.txt", "content")

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_mkdir_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        result = rclone_mkdir("nextcloud", "/new_dir")

        assert result is True
        mock_run.assert_called_once_with(
            ["rclone", "mkdir", "nextcloud:/new_dir"],
            capture_output=True, text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_mkdir_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)

        result = rclone_mkdir("nextcloud", "/bad_dir")
        assert result is False

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_path_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert rclone_path_exists("nextcloud", "/exists") is True

        mock_run.return_value = MagicMock(returncode=1)
        assert rclone_path_exists("nextcloud", "/missing") is False

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_move(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        result = rclone_move("nextcloud", "/src.txt", "/dst.txt")

        assert result is True
        mock_run.assert_called_once_with(
            ["rclone", "moveto", "nextcloud:/src.txt", "nextcloud:/dst.txt"],
            capture_output=True, text=True,
        )

    @patch("istota.rclone_client.subprocess.run")
    def test_rclone_move_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")

        result = rclone_move("nextcloud", "/src.txt", "/dst.txt")
        assert result is False
