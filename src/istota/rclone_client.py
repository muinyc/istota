"""The rclone API two modules had a copy of each, in one place.

``storage.py`` and ``skills/files/__init__.py`` each carried ``_rclone_run``
plus their own ``mkdir`` / ``lsjson`` / ``cat`` / ``rcat`` wrappers around it,
with byte-identical bodies and two docstrings that each name the other module
as a separate copy. The ``FileNotFoundError`` defect those docstrings describe
— ``subprocess.run`` reports a missing binary by raising rather than by
returning a non-zero status, so the exception escaped every helper documented
to return ``None`` or ``False`` — had to be fixed on both sides, which is what
made the pair worth removing rather than annotating a third time.

**A leaf rather than an import of ``storage``**, and that is a constraint
rather than a preference: ``skills/files`` runs in a skill subprocess and
``storage`` pulls in the package. ``subprocess`` and ``logging`` only; no
config, no paths, no policy — the remote name and the path are arguments.

What deliberately does **not** live here is anything only one caller has.
``skills/files`` keeps ``rclone_list``, ``rclone_move``, ``rclone_download``,
``rclone_upload`` and the ``_rclone_run_or_raise`` variant its four raising
helpers are built on, because none of those was ever written twice; moving a
single implementation into a shared module makes it shared without making it
consolidated.
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("istota.rclone_client")


def rclone_run(args: list[str], **kwargs) -> subprocess.CompletedProcess | None:
    """Run an rclone command, or return ``None`` if rclone could not be run at all.

    Every wrapper below documents "returns None / False on failure", and a
    missing ``rclone`` binary is a failure — but ``subprocess.run`` raises
    ``FileNotFoundError`` for it rather than returning a non-zero status, so
    the exception escaped all of them. Reachable in ordinary operation: the
    rclone path is the fallback for a deployment with no mount, and such a
    deployment need not have rclone installed either.

    ``setdefault`` rather than fixed keywords, so a future caller passing
    ``text=False`` for a binary ``rclone cat`` gets its own value rather than
    "multiple values for keyword argument".
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    try:
        return subprocess.run(args, **kwargs)
    except OSError as exc:
        logger.warning("rclone unavailable (%s); treating %s as a failure", exc, args[1:2])
        return None


def rclone_mkdir(remote: str, path: str) -> bool:
    """Create a directory via rclone. Returns True on success."""
    result = rclone_run(["rclone", "mkdir", f"{remote}:{path}"])
    return result is not None and result.returncode == 0


def rclone_path_exists(remote: str, path: str) -> bool:
    """Check if a path exists via rclone lsjson."""
    result = rclone_run(["rclone", "lsjson", f"{remote}:{path}"])
    return result is not None and result.returncode == 0


def rclone_cat(remote: str, path: str) -> str | None:
    """Read a file via rclone cat. Returns None on failure."""
    result = rclone_run(["rclone", "cat", f"{remote}:{path}"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def rclone_rcat(remote: str, path: str, content: str) -> bool:
    """Write content to a file via rclone rcat. Returns True on success."""
    result = rclone_run(["rclone", "rcat", f"{remote}:{path}"], input=content)
    return result is not None and result.returncode == 0
