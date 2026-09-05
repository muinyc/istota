"""Availability state shared between the scheduler and web processes.

The scheduler's in-memory breaker decides whether to skip a primary brain. The
web service runs in a separate process on the server deployment, so it reads
this small status file to report that the scheduler is serving work through a
fallback. The file is observational only: a bad or missing file never changes
task routing.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import fcntl

from .atomic_write import write_text_atomic


def _path(config, primary: str) -> Path | None:
    db_path = getattr(config, "db_path", None)
    kind = str(primary).strip().lower()
    if db_path is None or kind not in {"claude_code", "native", "tmux_claude"}:
        return None
    return Path(db_path).parent / f"brain_availability.{kind}.json"


@contextmanager
def _exclusive_lock(path: Path):
    """Serialize a primary's publish and clear operations across workers."""
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_unavailable(
    config,
    primary: str,
    reason: str,
    *,
    cooldown_seconds: float,
    now: float | None = None,
) -> bool:
    """Atomically publish a primary brain's cooldown to sibling processes."""
    path = _path(config, primary)
    if path is None or cooldown_seconds <= 0:
        return False

    opened_at = time.time() if now is None else now
    payload = {
        "primary": primary,
        "reason": reason,
        "opened_at": opened_at,
        "expires_at": opened_at + cooldown_seconds,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(path):
            write_text_atomic(
                path,
                json.dumps(payload, separators=(",", ":")),
                mode=0o600,
                fsync=True,
            )
    except OSError:
        return False
    return True


def read_unavailable(
    config, primary: str, *, now: float | None = None
) -> dict[str, str] | None:
    """Return an unexpired state for ``primary``, or ``None`` on any failure."""
    path = _path(config, primary)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("primary") != primary:
            return None
        expires_at = float(payload["expires_at"])
        if expires_at <= (time.time() if now is None else now):
            return None
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            return None
        return {"reason": reason}
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
        return None


def clear_unavailable(
    config, primary: str, *, started_at: float | None = None
) -> bool:
    """Remove a primary's published cooldown after a successful probe."""
    path = _path(config, primary)
    if path is None:
        return False
    try:
        with _exclusive_lock(path):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("primary") != primary:
                return False
            if started_at is not None and float(payload["opened_at"]) > started_at:
                return False
            path.unlink()
    except (OSError, TypeError, ValueError, json.JSONDecodeError, KeyError):
        return False
    return True


def clear_all(config) -> int:
    """Clear scheduler availability state left behind by an earlier process."""
    db_path = getattr(config, "db_path", None)
    if db_path is None:
        return 0
    try:
        paths = list(Path(db_path).parent.glob("brain_availability.*.json"))
    except OSError:
        return 0
    cleared = 0
    for path in paths:
        try:
            path.unlink()
        except OSError:
            continue
        cleared += 1
    return cleared
