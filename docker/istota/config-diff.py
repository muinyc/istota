#!/usr/bin/env python3
"""Report how two rendered ``config.toml`` files differ, by key, into the log.

ISSUE-368's own conclusion: the class of failure is the silence, not the
staleness. A boot that changes a setting should say which one, and a boot that
deliberately holds one back should say that too — one look at ``docker logs
istota`` rather than a shell into the volume.

Two callers, both in ``entrypoint.sh``'s config stage:

* after a re-render, ``config.toml.prev`` against the file just written — what
  this boot changed;
* under ``ISTOTA_CONFIG_RENDER=preserve``, the file on disk against a throwaway
  render of the current environment — what this boot is ignoring.

**Values are printed, sensitive ones are not.** The output goes to the container
log, which is the least private place in the deployment; ``config.toml`` holds
the bot's app password, the OAuth2 client secret, the forge tokens, the IMAP
password, the location ingest token and the Talk room tokens. A key `is_sensitive`
answers for is reported as changed with its value withheld, which is the whole
point of reporting by key rather than shelling out to ``diff``.

Three rules decide that, and each exists because the one before it is not
enough: a substring match on the normalized leaf catches everything spelled like
a credential, an exact-leaf list catches the ones that are not, and a
whole-subtree list catches the ones whose *contents* are credentials under a key
that is not named like one. ``log_channel`` and ``alerts_channel`` hold Talk
room tokens and match no credential word at all — the first draft of this module
printed both in full, under a docstring that said it did not.

**The marker list is a restatement of ``admin_config_view.SECRET_NAME_PATTERNS``
and cannot be an import**, because this file runs under the system ``python3``
from ``entrypoint.sh``, before and independently of the venv. It had drifted:
``apikey``, ``private_key``, ``auth`` and ``pass`` were missing, the leaf was not
normalized, and there was no wholesale rule — so ``x-api-key``, ``api-key`` and
``authorization`` under ``brain.native.extra_headers`` all printed in full.

**On one boot mode only, and worth knowing which.** ``render-config.sh`` has no
passthrough for ``extra_headers``, so under ``ISTOTA_CONFIG_RENDER=always`` both
sides of the comparison are renders and neither carries the table. The reachable
path is ``preserve``, where ``old`` is a hand-maintained ``config.toml`` that can
carry it and ``new`` is the probe render that cannot — so `describe`'s removed-key
branch fired, printing the header on *every* such boot rather than only when the
value changed. That is also the mode that writes ``.probe``, a second full copy
of every credential, which ``entrypoint.sh`` already treats as the sensitive one.

``tests/test_config_diff.py::TestParityWithTheAdminConfigView`` is the pin, over
a corpus walked out of the ``Config`` dataclass rather than a hand-written list.

Stdlib only, and once its arguments parse it never exits non-zero: this runs on
the boot path of a deployment that is otherwise fine, so a defect here must cost
a log line rather than a container. The qualifier is exact — ``argparse`` exits 2
on a usage error by raising ``SystemExit``, which the ``__main__`` guard's
``except Exception`` does not catch — and it costs nothing today because
``entrypoint.sh`` invokes this behind ``|| true``. A second caller that does not
would inherit a boot failure under ``set -euo pipefail``. Tested by
``tests/test_config_diff.py``.
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from typing import Any

#: A key whose last dotted component contains one of these holds a credential.
#: Substring rather than suffix: ``monarch_password``, ``session_secret_key``,
#: ``ingest_token`` and ``api_key`` all have to match, and they do not share an
#: ending. The cost of the loose rule is an occasional withheld value that did
#: not need withholding, which is the safe direction on a log line.
#:
#: The lowercased mirror of ``admin_config_view.SECRET_NAME_PATTERNS``. ``pass``
#: subsumes ``password`` and both are kept so the two lists read the same;
#: ``auth`` is what catches an ``authorization`` header and every ``oauth2_*``
#: field, and withholding an OAuth2 *client id* is the accepted cost of the rule
#: above — ``admin_config_view`` redacts it too, for the same reason. It also
#: catches ``developer.author_credit`` and ``email.authserv_id`` on the letters
#: rather than the meaning, and ``pass`` catches ``passthrough_env_vars`` and the
#: two ``bypass_*`` tmux markers. Those five are accepted false positives, named
#: here so the next reader does not read one as a defect: ``admin_config_view``
#: buys them back with a ``NON_SECRET_KEYS`` allowlist and this module
#: deliberately has none, because an allowlist is fail-open and the thing it
#: would buy is one legible line in a container log.
_CREDENTIAL_MARKERS = (
    "password",
    "pass",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "auth",
)

#: Keys that must be withheld and that no substring rule catches, matched on the
#: whole leaf rather than a fragment of it.
#:
#: ``log_channel`` and ``alerts_channel`` hold **Talk room tokens** — the render
#: writes the tokens `create_group_room` returned straight into them, and a room
#: token is a bearer capability: whoever has it can read and post in that room.
#: They are named here rather than by adding ``channel`` to the markers above,
#: because ``scheduler.log_channel_show_skills`` is a boolean whose leaf also
#: contains that word and whose value is worth seeing change.
#:
#: ``email_addresses`` is not a credential at all. It is withheld because the
#: destination is a container log and it is the operator's own address.
_WITHHELD_LEAVES = frozenset({"log_channel", "alerts_channel", "email_addresses"})

#: Whole subtrees withheld regardless of what the leaves under them are called,
#: mirroring ``admin_config_view._ALWAYS_SECRET_KEYS``.
#:
#: ``brain.native.extra_headers`` is the live case: ``llm/openai_compat.py``
#: merges it over the ``Authorization`` header, so it is where a non-Anthropic
#: deployment puts its ``x-api-key`` / ``api-key`` / ``authorization`` value. The
#: normalized leaf rule above catches those three spellings on its own now, but
#: the field is an auth channel by construction and a header nobody has thought
#: of should not be the thing standing between a container log and a live
#: provider key.
_ALWAYS_WITHHELD_PREFIXES = ("brain.native.extra_headers",)

#: Long enough for a model name, a URL or a room token; short enough that a
#: pasted blob cannot bury the rest of the report.
_MAX_VALUE_CHARS = 80

REDACTED = "(changed; value withheld)"


def is_sensitive(key: str) -> bool:
    """Whether this key's *value* must never reach the log.

    Named for what it decides rather than for what most of its hits are: two of
    the three exact leaves hold a bearer token and the third holds an email
    address, and calling all of that "a credential" is how ``log_channel``
    stayed printable through the first draft.

    Hyphens fold to underscores before anything is compared, so an HTTP *header*
    name matches the same markers a Python field name would — ``x-api-key`` is
    the shape that reaches here, because `flatten` turns a header table into one
    dotted key per header and the header is the leaf.
    """
    lowered = key.lower()
    for prefix in _ALWAYS_WITHHELD_PREFIXES:
        if lowered == prefix or lowered.startswith((prefix + ".", prefix + "[")):
            return True
    leaf = lowered.rsplit(".", 1)[-1].replace("-", "_")
    if leaf in _WITHHELD_LEAVES:
        return True
    return any(marker in leaf for marker in _CREDENTIAL_MARKERS)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Reduce a parsed TOML document to one entry per scalar, dotted-key.

    Arrays of tables are indexed (``users.x.resources[0].type``) so a resource
    appearing, disappearing or changing type reads as a key rather than as one
    opaque list value. A plain array of scalars stays whole — ``email_addresses``
    is more legible as a list than as three indexed rows.
    """
    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for name, child in value.items():
            flat.update(flatten(child, f"{prefix}.{name}" if prefix else str(name)))
        return flat
    if isinstance(value, list) and any(isinstance(item, dict) for item in value):
        flat = {}
        for index, item in enumerate(value):
            flat.update(flatten(item, f"{prefix}[{index}]"))
        return flat
    return {prefix: value}


def render_value(key: str, value: Any) -> str:
    """A value, ready for a log line: withheld, or short enough to read.

    Truncation happens *inside* the value and before `repr`, so a long string
    still comes back as a balanced literal. Truncating the repr instead cut a
    quoted string after its opening quote and printed `'aaaa...` — which reads
    as a malformed value rather than an elided one.
    """
    if is_sensitive(key):
        return REDACTED
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return repr(value[: _MAX_VALUE_CHARS - 3] + "...")
    text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[: _MAX_VALUE_CHARS - 3] + "..."
    return text


def load(path: str) -> tuple[dict[str, Any] | None, str]:
    try:
        with open(path, "rb") as handle:
            return flatten(tomllib.load(handle)), ""
    except FileNotFoundError:
        return None, f"{path} does not exist"
    except tomllib.TOMLDecodeError as exc:
        return None, f"{path} is not valid TOML ({exc})"
    except OSError as exc:
        return None, f"{path} could not be read ({exc})"


def describe(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    label_old: str,
    label_new: str,
) -> list[str]:
    """One line per key that differs. Empty when the two agree."""
    lines: list[str] = []
    for key in sorted(set(old) | set(new)):
        if key in old and key in new:
            if old[key] == new[key]:
                continue
            if is_sensitive(key):
                lines.append(f"  {key}: {REDACTED}")
            else:
                lines.append(
                    f"  {key}: {render_value(key, old[key])} ({label_old})"
                    f" -> {render_value(key, new[key])} ({label_new})"
                )
        elif key in new:
            lines.append(f"  + {key} = {render_value(key, new[key])} ({label_new})")
        else:
            lines.append(f"  - {key} (was {render_value(key, old[key])}, {label_old})")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("--label-old", default="before")
    parser.add_argument("--label-new", default="after")
    parser.add_argument(
        "--heading",
        default="config.toml differs",
        help="the summary line printed above the per-key lines",
    )
    args = parser.parse_args(argv)

    old, old_error = load(args.old)
    new, new_error = load(args.new)
    if old is None or new is None:
        # Not silence, but not a boot failure either: the caller has already
        # written the config it means to run on.
        print(
            f"[istota] Could not compare configurations: {old_error or new_error}",
            file=sys.stderr,
        )
        return 0

    lines = describe(old, new, label_old=args.label_old, label_new=args.label_new)
    if not lines:
        return 0

    print(f"[istota] {args.heading} ({len(lines)} key(s)):")
    for line in lines:
        print(f"[istota] {line}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover - the boot path must survive us
        print(f"[istota] config-diff.py failed: {exc}", file=sys.stderr)
        sys.exit(0)
