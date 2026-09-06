"""ISSUE-263 — the docker image shipped neither `gh` nor `glab`.

The image's apt line installed `curl git sqlite3 tmux bubblewrap` and the
WeasyPrint/tesseract libraries, but no forge CLI. Meanwhile the entrypoint
renders a `[developer]` block whenever `ISTOTA_DEVELOPER_ENABLED=true`, which
compose defaults to. Once an operator also set `repos_dir` and a token — the
two gates the skill checks — `setup_env` wrote the wrappers, `gh` and `glab`
resolved on PATH to them, and the wrapper's `os.execve` hit a path that did not
exist, exiting `EXIT_EXEC` (6) with `cannot run /usr/local/bin/gh: ENOENT`.

The entry proposed adding both to the existing apt line. That does not work
here. The image was bookworm (`python:3.12-slim-bookworm`) when this was
written, whose `gh` is 2.23, below the 2.40 floor the skill's verbs need, and
which ships no `glab` at all; ISSUE-440 moved it to `python:3.12-slim-trixie`,
which packages both and is still ~50 and ~60 releases behind the pins.
The fix mirrors what `docker/devbox/Dockerfile` already does — the pinned
release `.deb`s, sha256-verified, with the binary extracted rather than
installed so dpkg does not put a second, real `gh` on PATH.

The tests below pin the three properties that make that safe: the binaries are
present and checksummed, they land *off* PATH so the wrapper stays the only
`gh` a task can reach, and the rendered config tells the skill where they are —
without which `_resolve_real_bin` cannot find an off-PATH binary and falls back
to the broken default.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from istota import forge_bin
from istota.config import DeveloperConfig
from istota.skills.developer import _resolve_real_bin

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
DEVBOX_DOCKERFILE = REPO / "docker" / "devbox" / "Dockerfile"
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"

# Where the real binaries live: deliberately not a PATH directory. The devbox
# image made the same choice for the same reason.
FORGE_LIB = "/usr/local/lib/istota_forge"

PINNED = (
    "GH_VERSION",
    "GH_DEB_SHA256_AMD64",
    "GH_DEB_SHA256_ARM64",
    "GLAB_VERSION",
    "GLAB_DEB_SHA256_AMD64",
    "GLAB_DEB_SHA256_ARM64",
)

# Both images now pin all six, so the mapping is the identity and every value
# is compared. It used to map the amd64 pair onto the devbox's single-arch
# names, because that image was amd64-only — which left the two arm64 checksums
# guarded by nothing: a version bump that updated the amd64 pair and forgot the
# arm64 pair passed every test here and failed only at `sha256sum -c -` during
# an actual arm64 build. ISSUE-280 made the devbox recipe per-architecture, and
# that hole closed as a side effect.
DEVBOX_EQUIVALENT = {name: name for name in PINNED}


def _build_args(body: str) -> dict[str, str]:
    """The `ARG NAME=value` pairs declared in a Dockerfile."""
    return dict(re.findall(r"^ARG\s+([A-Z0-9_]+)=(\S+)", body, re.M))


def _forge_run_block(body: str) -> str:
    """Just the forge `RUN` layer, comments stripped.

    The `not in` assertions below are about what the layer *does*, so they have
    to be scoped to it. Run over the whole file they would also read the 30-odd
    lines of comment above it — and this codebase writes comments that name the
    paths they are explaining, so `/usr/local/bin/gh` appearing in prose would
    fail a test whose message talks about a policy bypass.

    Anchored on the layer that fetches `gh.deb`, not simply on the first
    `RUN set -eux; arch=` in the file: the devbox Dockerfile grew a second block
    of that shape when its Go toolchain became per-architecture (ISSUE-280), and
    a positional match silently returned the wrong layer.
    """
    candidates = [m.start() for m in re.finditer(r"^RUN set -eux; \\$", body, re.M)]
    assert candidates, "no `RUN set -eux;` layer found"
    for start in candidates:
        # `\Z` as the fallback: a layer that is the last thing in the file has
        # no trailing blank line, and `body.index` would raise a bare
        # "substring not found" naming neither the file nor the layer.
        end = re.search(r"\n\n|\Z", body[start:])
        block = body[start:start + end.start()]
        if "gh.deb" in block:
            return "\n".join(
                line for line in block.split("\n")
                if not line.lstrip().startswith("#")
            )
    raise AssertionError("no RUN layer in this Dockerfile fetches gh.deb")


def _developer_block(body: str) -> str:
    """Everything render-config.sh writes into the `[developer]` config block.

    The heredoc, plus the `echo`-appended keys below it: `author_credit` is
    written after the `TOML` terminator, so a slice that stopped there would
    leave it out of the field-name guard. The terminator is anchored to a line
    of its own — a comment containing the word TOML inside the block would
    otherwise truncate the region silently and make the guard pass on less.

    This block used to live in `entrypoint.sh`. It moved to `render-config.sh`
    with the Stage 4 extraction; `tests/test_render_config.py` holds the
    entrypoint to calling that script rather than re-inlining the render.
    """
    start = body.index("[developer]")
    rest = body[start:]
    end = re.search(r"^\s*fi$", rest, re.M)
    assert end, "unterminated [developer] branch in render-config.sh"
    return rest[: end.start()]


class TestTheImageShipsTheForgeBinaries:
    def test_both_binaries_are_installed(self):
        # The bug in one line: neither name appeared in the Dockerfile at all.
        body = DOCKERFILE.read_text()
        assert f"{FORGE_LIB}/gh" in body
        assert f"{FORGE_LIB}/glab" in body

    def test_the_downloads_are_version_pinned(self):
        args = _build_args(DOCKERFILE.read_text())
        for name in PINNED:
            assert name in args, f"{name} is not pinned"

    def test_the_downloads_are_checksum_verified(self):
        # An unverified curl into an image that ships to other people is the
        # supply-chain half of this change; the pin alone does not cover it.
        body = DOCKERFILE.read_text()
        assert body.count("sha256sum -c -") >= 2

    def test_the_binaries_are_verified_to_actually_exec(self):
        # `dpkg-deb | tar` has no pipefail under dash, so a truncated extract
        # exits 0. The --version calls are what catch it.
        body = DOCKERFILE.read_text()
        assert f"{FORGE_LIB}/gh --version" in body
        assert f"{FORGE_LIB}/glab --version" in body

    @pytest.mark.parametrize(
        "dockerfile", [DOCKERFILE, DEVBOX_DOCKERFILE], ids=["istota", "devbox"]
    )
    def test_the_asset_is_chosen_per_architecture(self, dockerfile):
        # Both images build on arm64 (an Apple Silicon `docker compose build` is
        # the common case), and hardcoding the amd64 asset breaks that build
        # outright. The devbox image did exactly that until ISSUE-280, which is
        # why its own test tier effectively never ran.
        run = _forge_run_block(dockerfile.read_text())
        assert 'arch="$(dpkg --print-architecture)"' in run
        assert "_linux_${arch}.deb" in run
        assert "_linux_amd64.deb" not in run

    def test_the_devbox_go_toolchain_is_chosen_per_architecture(self):
        # The third hardcoded string, and the one that gated the devbox build:
        # the forge assets are only two of the three.
        body = DEVBOX_DOCKERFILE.read_text()
        assert "linux-${arch}.tar.gz" in body
        assert "linux-amd64.tar.gz" not in body
        # It had no checksum at all before, so an unverified download would
        # satisfy a naive "is it per-arch" check.
        assert "GO_SHA256_AMD64" in body
        assert "GO_SHA256_ARM64" in body
        # Scoped to the Go tarball, not `"sha256sum -c -" in body`: the forge
        # layer in this same file already contains two of those, so the loose
        # form passed against a Dockerfile with the Go verification deleted and
        # its two GO_SHA256_* args left dangling. Which is exactly the
        # regression this assertion exists for.
        assert '"${go_sha}  /tmp/go.tar.gz" | sha256sum -c -' in body

    @pytest.mark.parametrize(
        "dockerfile", [DOCKERFILE, DEVBOX_DOCKERFILE], ids=["istota", "devbox"]
    )
    def test_an_unpinned_architecture_fails_the_build(self, dockerfile):
        # The one thing worse than no binary is an unverified one: a fallback
        # that skipped the checksum on an unexpected arch would do exactly that.
        assert "no pinned gh/glab checksum for" in dockerfile.read_text()

    def test_an_unpinned_architecture_fails_the_devbox_go_layer_too(self):
        assert "no pinned go checksum for" in DEVBOX_DOCKERFILE.read_text()

    def test_the_binaries_land_off_path(self):
        # Nothing should resolve the real binary by name; that is the wrapper's
        # job. The positive half is what makes this non-vacuous — the pre-fix
        # Dockerfile mentioned neither path, so the `not in` alone passed
        # against a file that installed nothing at all.
        run = _forge_run_block(DOCKERFILE.read_text())
        assert f"> {FORGE_LIB}/gh" in run
        assert f"> {FORGE_LIB}/glab" in run
        assert "/usr/local/bin/gh" not in run
        assert "/usr/local/bin/glab" not in run

    def test_dpkg_is_not_used_to_install_them(self):
        # `dpkg -i` would drop a second, real `gh` at /usr/bin/gh — resolvable
        # by name, which is the thing being avoided. Paired with the positive
        # for the same reason as above.
        run = _forge_run_block(DOCKERFILE.read_text())
        assert "dpkg-deb --fsys-tarfile" in run
        assert "dpkg -i" not in run


class TestTheRenderedConfigPointsTheSkillAtThem:
    def test_the_developer_block_renders_both_bin_paths(self):
        # Without these, `_resolve_real_bin` falls back to the daemon's PATH,
        # finds nothing (the binaries are off PATH by design), and returns the
        # /usr/local/bin default that does not exist.
        block = _developer_block(RENDER_CONFIG.read_text())
        assert "gh_bin_path" in block
        assert "glab_bin_path" in block

    def test_the_rendered_paths_default_to_where_the_dockerfile_puts_them(self):
        # Operator-overridable, but the default has to match the image or the
        # stock container is back to exec'ing a path that does not exist.
        block = _developer_block(RENDER_CONFIG.read_text())
        assert f":-{FORGE_LIB}/gh}}" in block
        assert f":-{FORGE_LIB}/glab}}" in block

    def test_the_override_reaches_the_container(self):
        # An env var the entrypoint reads but compose never forwards is a knob
        # wired to nothing: setting it in .env would silently do nothing.
        compose = (REPO / "docker" / "docker-compose.yml").read_text()
        assert "ISTOTA_DEVELOPER_GH_BIN_PATH" in compose
        assert "ISTOTA_DEVELOPER_GLAB_BIN_PATH" in compose


    def test_every_rendered_key_is_a_developer_config_field(self):
        # Same guard the Ansible template has: the loader ignores unknown keys,
        # so a typo here reaches every container and does nothing at all.
        block = _developer_block(RENDER_CONFIG.read_text())
        rendered = set(re.findall(r"^([a-z_][a-z0-9_]*)\s*=", block, re.M))
        # The conditionally-appended keys are written with `echo`, not by the
        # heredoc, and need covering too — `author_credit` is one.
        rendered |= set(re.findall(r'echo\s+"([a-z_][a-z0-9_]*)\s*=', block))
        assert "author_credit" in rendered, (
            "the [developer] block scanner stopped before the echo-appended "
            "keys; the field-name guard below would cover less than it claims"
        )
        unknown = sorted(rendered - {f.name for f in fields(DeveloperConfig)})
        assert not unknown, f"render-config.sh renders unknown [developer] keys: {unknown}"


class TestTheResolvedBinaryIsTheOneTheImageShips:
    """The file assertions above prove the text is right. This proves the
    behaviour is: that an off-PATH path, once rendered, survives resolution."""

    def test_a_rendered_path_is_used_as_given(self):
        # `_resolve_real_bin` only falls back for an unset key or the code
        # default still standing. The docker path is neither, so it is returned
        # verbatim — which is what makes the off-PATH install work at all.
        assert _resolve_real_bin(f"{FORGE_LIB}/gh", "gh") == f"{FORGE_LIB}/gh"
        assert _resolve_real_bin(f"{FORGE_LIB}/glab", "glab") == f"{FORGE_LIB}/glab"

    def test_an_upgraded_container_finds_the_shipped_binary(self, monkeypatch):
        # The regression Mulder caught: the entrypoint wrote config.toml only on
        # a first boot with a fresh volume, so a container upgraded into an
        # image that ships the binaries still had a [developer] block with no
        # gh_bin_path. The dataclass default stands, /usr/local/bin/gh does not
        # exist, and the binaries are off PATH — so without the probe below
        # this resolves to the same broken path as before the fix. ISSUE-368
        # made the render happen on every boot, so a restart now repairs it too;
        # a container still running from before its upgrade is in exactly this
        # state, which is why the probe stays.
        monkeypatch.setattr(forge_bin.shutil, "which", lambda _name: None)
        monkeypatch.setattr(
            forge_bin.os.path, "exists", lambda p: p.startswith(FORGE_LIB)
        )
        assert _resolve_real_bin(DeveloperConfig().gh_bin_path, "gh") == f"{FORGE_LIB}/gh"
        assert (
            _resolve_real_bin(DeveloperConfig().glab_bin_path, "glab")
            == f"{FORGE_LIB}/glab"
        )

    def test_with_nothing_installed_it_still_reports_the_documented_default(
        self, monkeypatch
    ):
        # The pre-fix container, pinned so the probe cannot mask a total
        # absence: nothing on disk, nothing on PATH, and the caller gets the
        # documented default rather than a silent empty string.
        monkeypatch.setattr(forge_bin.shutil, "which", lambda _name: None)
        monkeypatch.setattr(forge_bin.os.path, "exists", lambda _p: False)
        assert _resolve_real_bin("", "gh") == "/usr/local/bin/gh"

    def test_an_operator_path_still_wins_over_the_shipped_one(self, monkeypatch):
        # The probe must not outrank an explicit choice — `_resolve_real_bin`
        # returns a configured path as given, existing or not.
        monkeypatch.setattr(forge_bin.shutil, "which", lambda _name: None)
        monkeypatch.setattr(forge_bin.os.path, "exists", lambda _p: True)
        assert _resolve_real_bin("/opt/mine/gh", "gh") == "/opt/mine/gh"


class TestTheTwoImagesAgreeOnVersions:
    def test_pinned_versions_and_checksums_match_devbox(self):
        # Two images fetching the same two binaries at different versions is a
        # drift the repo already guards against elsewhere (the vendored wrapper
        # has scripts/sync-devbox-lib.sh and a test). Same idea, cheaper: the
        # pins are literals in two files, so compare them.
        mine = _build_args(DOCKERFILE.read_text())
        devbox = _build_args(DEVBOX_DOCKERFILE.read_text())
        for name, their_name in DEVBOX_EQUIVALENT.items():
            assert mine[name] == devbox[their_name], f"{name} differs from devbox"
