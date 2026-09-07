"""Nextcloud file operations via rclone or local mount."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ...rclone_client import rclone_mkdir, rclone_path_exists, rclone_run

if TYPE_CHECKING:
    from ..config import Config

logger = logging.getLogger("istota.skills.files")


# =============================================================================
# Mount-aware wrapper functions
# =============================================================================


def list_files(config: "Config", path: str) -> list[dict]:
    """
    List files at a path (mount-aware).
    Returns list of dicts with 'name', 'size', 'mod_time', 'is_dir'.
    """
    if config.use_mount:
        mount_path = config.nextcloud_mount_path / path.lstrip("/")
        if not mount_path.exists():
            raise RuntimeError(f"Path not found: {mount_path}")
        return [
            {
                "name": p.name,
                "size": p.stat().st_size if p.is_file() else 0,
                "mod_time": "",
                "is_dir": p.is_dir(),
            }
            for p in mount_path.iterdir()
        ]
    else:
        return rclone_list(config.rclone_remote, path)


def read_text(config: "Config", path: str) -> str:
    """Read a text file (mount-aware)."""
    if config.use_mount:
        mount_path = config.nextcloud_mount_path / path.lstrip("/")
        if not mount_path.exists():
            raise RuntimeError(f"File not found: {mount_path}")
        return mount_path.read_text()
    else:
        return rclone_read_text(config.rclone_remote, path)


def write_text(config: "Config", path: str, content: str) -> None:
    """Write text content to a file (mount-aware)."""
    if config.use_mount:
        mount_path = config.nextcloud_mount_path / path.lstrip("/")
        mount_path.parent.mkdir(parents=True, exist_ok=True)
        mount_path.write_text(content)
    else:
        rclone_write_text(config.rclone_remote, path, content)


def mkdir(config: "Config", path: str) -> bool:
    """Create a directory (mount-aware). Returns True on success."""
    if config.use_mount:
        mount_path = config.nextcloud_mount_path / path.lstrip("/")
        mount_path.mkdir(parents=True, exist_ok=True)
        return True
    else:
        return rclone_mkdir(config.rclone_remote, path)


def path_exists(config: "Config", path: str) -> bool:
    """Check if a path exists (mount-aware)."""
    if config.use_mount:
        mount_path = config.nextcloud_mount_path / path.lstrip("/")
        return mount_path.exists()
    else:
        return rclone_path_exists(config.rclone_remote, path)


def move_file(config: "Config", src_path: str, dst_path: str) -> bool:
    """Move a file or directory (mount-aware). Returns True on success."""
    if config.use_mount:
        src = config.nextcloud_mount_path / src_path.lstrip("/")
        dst = config.nextcloud_mount_path / dst_path.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return True
    else:
        return rclone_move(config.rclone_remote, src_path, dst_path)


def copy_to_local(config: "Config", remote_path: str, local_path: Path) -> None:
    """Copy a file from Nextcloud to local filesystem (mount-aware)."""
    if config.use_mount:
        src = config.nextcloud_mount_path / remote_path.lstrip("/")
        if not src.exists():
            raise RuntimeError(f"Source file not found: {src}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(local_path))
    else:
        rclone_download(config.rclone_remote, remote_path, local_path)


def copy_to_remote(config: "Config", local_path: Path, remote_path: str) -> None:
    """Copy a file from local filesystem to Nextcloud (mount-aware)."""
    if config.use_mount:
        dst = config.nextcloud_mount_path / remote_path.lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(local_path), str(dst))
    else:
        rclone_upload(config.rclone_remote, local_path, remote_path)


def get_local_path(config: "Config", remote_path: str) -> Path | None:
    """
    Get a local filesystem path for a remote file.

    If using mount, returns the mount path directly.
    If using rclone, returns None (caller should download).
    """
    if config.use_mount:
        return config.nextcloud_mount_path / remote_path.lstrip("/")
    return None


# =============================================================================
# Low-level rclone functions (for backward compatibility and non-mount mode)
# =============================================================================


# `_rclone_run`, `rclone_mkdir` and `rclone_path_exists` were byte-identical
# copies of `istota.storage`'s, down to the docstring naming the other module.
# They live in `istota.rclone_client`, a stdlib-only leaf, which is what lets
# this skill — which runs in a subprocess and must not pull in the package —
# share them. The four helpers below that raise on failure stay here: their
# contract is this module's, not the leaf's, and they were never duplicated.
_rclone_run = rclone_run


def _rclone_run_or_raise(args: list[str], what: str, **kwargs) -> subprocess.CompletedProcess:
    """`_rclone_run` for the helpers whose contract is to raise."""
    result = _rclone_run(args, **kwargs)
    if result is None:
        raise RuntimeError(f"rclone {what} failed: rclone is not installed")
    if result.returncode != 0:
        raise RuntimeError(f"rclone {what} failed: {result.stderr}")
    return result


def rclone_move(remote: str, src_path: str, dst_path: str) -> bool:
    """Move a file or directory within rclone remote. Returns True on success."""
    result = _rclone_run(
        ["rclone", "moveto", f"{remote}:{src_path}", f"{remote}:{dst_path}"]
    )
    if result is None:
        return False
    if result.returncode != 0:
        logger.error("rclone move failed: %s", result.stderr)
        return False
    return True


def rclone_list(remote: str, path: str) -> list[dict]:
    """
    List files at a path.
    Returns list of dicts with 'name', 'size', 'mod_time', 'is_dir'.
    """
    result = _rclone_run_or_raise(["rclone", "lsjson", f"{remote}:{path}"], "list")

    items = json.loads(result.stdout)
    return [
        {
            "name": item["Name"],
            "size": item.get("Size", 0),
            "mod_time": item.get("ModTime", ""),
            "is_dir": item.get("IsDir", False),
        }
        for item in items
    ]


def rclone_download(remote: str, remote_path: str, local_path: Path) -> None:
    """Download a file from Nextcloud."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _rclone_run_or_raise(
        ["rclone", "copy", f"{remote}:{remote_path}", str(local_path.parent)], "download",
    )


def rclone_upload(remote: str, local_path: Path, remote_path: str) -> None:
    """Upload a file to Nextcloud."""
    _rclone_run_or_raise(
        ["rclone", "copy", str(local_path), f"{remote}:{remote_path}"], "upload",
    )


def rclone_read_text(remote: str, remote_path: str) -> str:
    """Read a text file from Nextcloud directly."""
    return _rclone_run_or_raise(["rclone", "cat", f"{remote}:{remote_path}"], "read").stdout


def rclone_write_text(remote: str, remote_path: str, content: str) -> None:
    """Write text content to a file on Nextcloud."""
    _rclone_run_or_raise(
        ["rclone", "rcat", f"{remote}:{remote_path}"], "write", input=content,
    )
