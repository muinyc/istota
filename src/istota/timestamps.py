"""The current instant as a string, in the two formats the tables already hold.

Two functions rather than one, deliberately, because there are two formats in
the stored data and this module changes neither of them. What it does is put
the choice in one place, so a reader can see that the choice exists.

``iso_now`` is ``datetime.now(timezone.utc).isoformat()`` — microsecond
precision, offset-aware, ``2026-09-05T14:22:31.482913+00:00``. Written by
``briefings``, ``feeds``, ``health`` and ``money``.

``iso_now_seconds`` is ``strftime("%Y-%m-%dT%H:%M:%SZ")`` — second precision,
``Z`` suffix, ``2026-09-05T14:22:31Z``. Written by ``memory/curation/audit``.

**A comparison or a sort spanning two of those stores is a string comparison
across two formats, and it does not mean what it reads as.** The two strings
first differ at the character after the seconds field, where the offset-aware
form has ``.`` (0x2E) or ``+`` (0x2B) and the ``Z`` form has ``Z`` (0x5A) — so
for one and the same instant the offset-aware form always sorts first,
whatever the actual times were. Nothing here fixes that. Unifying the formats
means rewriting stored rows across six stores, which is a migration and a spec
of its own; this module is where the fact is recorded until then.

``events.py`` writes a third format — millisecond precision with a ``Z``
suffix — and deliberately keeps its own helper rather than gaining a third
function here. Its precision is load-bearing: ``task_events`` is an ordered
log the streaming surfaces read back, and two events inside one second are
routine.

stdlib-only leaf, imports nothing from the package.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_now() -> str:
    """Microsecond-precision, offset-aware UTC: ``2026-09-05T14:22:31.482913+00:00``."""
    return datetime.now(timezone.utc).isoformat()


def iso_now_seconds() -> str:
    """Second-precision UTC with a ``Z`` suffix: ``2026-09-05T14:22:31Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
