"""Every image whose base this repository chooses is on the release production runs.

ISSUE-440. `docker/istota/Dockerfile` and `docker/test/Dockerfile` were on
`python:3.12-slim-bookworm` while the Ansible role's support target is
"Debian 13+" and `docker/devbox/Dockerfile` had already moved to
`debian:trixie-slim`.

The one that mattered is `docker/test/Dockerfile`, which ships nothing: it is
the Linux tier runner, the image whose whole purpose is that the sandbox code
path really executes rather than being asserted as argv on a macOS host. Its
apt line pins the environment those tests observe, and bookworm gave them
bubblewrap 0.8.0 and git 2.39 against the 0.12.0 and 2.47 every supported
deployment runs — so the tier that exists to prove the sandbox works was
proving it against a bubblewrap four years older than the one in production.

**Nothing in the default suite could see this.** A base image is a string in a
Dockerfile that no test read, so an image could sit a release behind
indefinitely; the tiers that build them are discretionary and had not been run
since the onboarding refresh. This file is the guard that was missing.

Three exclusions, and each is a different reason rather than a variation of one:

  * `node:20-slim` (`docker/istota/Dockerfile:5`), the web builder. It **is** a
    Debian image — node:20-slim tracks bookworm — so the exemption is not that
    its tag names no release. It is that the stage copies static JS into the
    runtime image and contributes no library to it, so its Debian release is
    coupled to nothing here, and pinning it is a decision about node's image
    with its own argument. The codename rule happens to skip it because the tag
    carries no codename; that is a mechanism, not the reason.
  * `docker/browser/Dockerfile` is **vendored** from the stealth-browser repo,
    which owns it and the full test suite for that module
    (`tests/test_browser_render.py`). It is byte-identical to upstream, nothing
    in this repository builds it, and a base bumped here would be reverted by
    the next re-sync — so its release is upstream's call and is tracked in
    `VENDORED_DOCKERFILES` rather than asserted.
  * The `Dockerfile.*` negative controls under `docker/test/` build
    `FROM ${BASE}` and inherit whatever the tier hands them. Asserting on those
    would be asserting on a build argument.

Two vacuity guards, because a sweep that finds nothing passes and would then
say nothing at all — the failure this repository has found eight times over.
One runs list-to-disk (every named file exists), and the other runs
**disk-to-list**, which is the direction that catches a *new* Dockerfile added
somewhere nobody thought to name. That second direction is the convention
`tests/test_lint_scope.py` already sets for a hand-maintained list here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCKER = REPO / "docker"

# The release every supported deployment runs. `deploy/ansible/README.md` says
# "Debian 13+", and `docker/devbox/Dockerfile` was already here.
TARGET = "trixie"

# Enough of them to recognise a base that names one. Anything not in this set
# carries no Debian release in its tag and is not this file's business.
DEBIAN_CODENAMES = frozenset({
    "jessie", "stretch", "buster", "bullseye", "bookworm", "trixie", "forky",
    "sid",
})

# Dockerfiles whose base is this repository's decision.
BASE_OWNING_DOCKERFILES = (
    Path("docker/istota/Dockerfile"),
    Path("docker/test/Dockerfile"),
    Path("docker/devbox/Dockerfile"),
)

# Vendored from another repository, which owns the base. Listed rather than
# ignored so the disk-to-list guard below still accounts for them, and so
# removing one is a visible edit rather than a silent gap.
VENDORED_DOCKERFILES = (
    Path("docker/browser/Dockerfile"),
)

_FROM_RE = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)

# `FROM --platform=linux/amd64 img` — the flags come before the image, and
# taking the first token would capture the flag and then find no codename in
# it, skipping the line in silence. No Dockerfile here uses one today; the
# release checklist in AGENTS.md builds with `--platform`, so one is plausible.
_FLAG = re.compile(r"^--")


def _from_images(path: Path) -> list[tuple[int, str]]:
    """Every `FROM` image in *path*, as (line number, image reference)."""
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        match = _FROM_RE.match(line)
        if not match:
            continue
        tokens = [t for t in match.group("rest").split() if not _FLAG.match(t)]
        if tokens:
            found.append((lineno, tokens[0]))
    return found


def _codename(image: str) -> str | None:
    """The Debian release *image* names in its tag, if it names one.

    Split on every delimiter that can abut a codename — `/` and `@` as well as
    `:` and `-` — so a registry path like `reg.io/bookworm-python:3.12` is read
    rather than passed over. A `@sha256:` digest cannot collide, since neither
    of its halves can equal a codename.
    """
    for token in re.split(r"[:\-/@]", image):
        if token in DEBIAN_CODENAMES:
            return token
    return None


def _is_control(path: Path) -> bool:
    return path.parent == DOCKER / "test" and path.name != "Dockerfile"


def test_the_named_dockerfiles_all_exist() -> None:
    """List to disk: a rename must fail here, not empty the sweep."""
    for rel in BASE_OWNING_DOCKERFILES + VENDORED_DOCKERFILES:
        assert (REPO / rel).is_file(), (
            f"{rel} is named here as owning a base image but does not exist. "
            "Update the list rather than deleting the check."
        )


def test_every_dockerfile_in_the_tree_is_accounted_for() -> None:
    """Disk to list: a new Dockerfile cannot go unguarded in silence.

    The direction that matters, and the one a hand-maintained list does not
    have on its own. Without it a fourth image added later is checked by
    nothing while this file still reports everything passing —
    `tests/test_lint_scope.py` exists for exactly this reason.
    """
    known = set(BASE_OWNING_DOCKERFILES) | set(VENDORED_DOCKERFILES)
    for path in sorted(DOCKER.rglob("Dockerfile*")):
        if _is_control(path):
            continue
        rel = path.relative_to(REPO)
        assert rel in known, (
            f"{rel} is a Dockerfile nothing in this file knows about. Add it "
            "to BASE_OWNING_DOCKERFILES if this repository chooses its base, "
            "or to VENDORED_DOCKERFILES if another repository owns it."
        )


@pytest.mark.parametrize("rel", BASE_OWNING_DOCKERFILES, ids=lambda p: p.parent.name)
def test_each_dockerfile_pins_a_debian_release(rel: Path) -> None:
    """Each named Dockerfile has at least one base naming a Debian release.

    Without this, a base rewritten to something with no codename in its tag
    would satisfy the release check below by having nothing to check.
    """
    images = _from_images(REPO / rel)
    assert images, f"{rel} has no FROM line at all"
    named = [(lineno, img) for lineno, img in images if _codename(img)]
    assert named, (
        f"{rel} names no Debian release in any of its bases {images!r}. "
        "If that is deliberate, say so here rather than dropping the file."
    )


@pytest.mark.parametrize("rel", BASE_OWNING_DOCKERFILES, ids=lambda p: p.parent.name)
def test_every_dockerfile_is_on_the_deployment_release(rel: Path) -> None:
    """No image names a Debian release other than the one production runs."""
    for lineno, image in _from_images(REPO / rel):
        codename = _codename(image)
        if codename is None:
            continue
        assert codename == TARGET, (
            f"{rel}:{lineno} builds on {image!r} ({codename}), which is not "
            f"the release every supported deployment runs ({TARGET}). The "
            "Linux tier in particular observes the base's bubblewrap and git, "
            "so a base that disagrees makes the tier report on an environment "
            "nobody runs."
        )


def test_the_negative_controls_take_their_base_as_an_argument() -> None:
    """`docker/test/Dockerfile.*` inherit; they pin nothing of their own.

    If one ever hardcodes a base, it silently stops tracking the tier and the
    control it carries starts answering about a different image.
    """
    controls = sorted(p for p in DOCKER.glob("test/Dockerfile.*") if _is_control(p))
    assert controls, "no negative-control Dockerfiles found under docker/test/"
    for path in controls:
        for lineno, image in _from_images(path):
            assert "${BASE}" in image or "$BASE" in image, (
                f"{path.relative_to(REPO)}:{lineno} pins {image!r} instead of "
                "building FROM ${BASE}. A control has to inherit the image it "
                "is a control for."
            )
