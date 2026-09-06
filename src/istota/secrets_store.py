"""Encrypted credential store (Phase 5 of the Docker onboarding spec).

Tier-2 secrets (Monarch creds, Karakeep API keys, Tumblr tokens, etc.) live
in the ``secrets`` table, encrypted at rest with a Fernet key derived from
``$ISTOTA_SECRET_KEY``.

The agent process needs the plaintext values to function — encryption here
does not change the access boundary, it just protects the on-disk DB file
(backups, decommissioned hardware, accidental log dumps).

Resolution order used by callers (see ``resolve_secret``):
    1. ``secrets`` table  (web-UI-managed, encrypted)
    2. resource-entry extras in config.toml  (Ansible-managed, plaintext)
    3. environment variables                 (legacy / tier-1 overlap)
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import sqlite_util

logger = logging.getLogger(__name__)

# Env var that supplies the master key. The documented format is a hex-32
# (64-char) value generated via ``python3 -c "import secrets;
# print(secrets.token_hex(32))"``. Anything below ``_MIN_KEY_LEN`` is
# rejected outright — without a length floor a typo like
# ``ISTOTA_SECRET_KEY=test`` derives the well-known SHA-256 of "test".
_KEY_ENV_VAR = "ISTOTA_SECRET_KEY"
_MIN_KEY_LEN = 32

# scrypt cost factors. The numbers are conservative; the master key is
# expected to be high entropy already (the docs steer operators to
# ``token_hex(32)``), so we only need scrypt to (a) defeat the
# "ISTOTA_SECRET_KEY=changeme" footgun and (b) avoid the "raw single-round
# SHA-256" smell. A fixed salt is acceptable here because the master key is
# a per-deployment secret already — scrypt is just a key-stretcher to slow
# down brute force on a leaked DB + leaked weak passphrase.
_SCRYPT_SALT = b"istota-secrets-store-v1"
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


class SecretKeyMissingError(RuntimeError):
    """Raised when an encrypt/decrypt operation runs without ISTOTA_SECRET_KEY."""


class SecretKeyTooWeakError(RuntimeError):
    """Raised when ISTOTA_SECRET_KEY is shorter than the minimum length."""


def _derive_fernet_key(secret: str) -> bytes:
    """Derive a urlsafe-base64 Fernet key from the master secret via scrypt.

    For a high-entropy secret (the documented hex-32 format) scrypt is a
    no-op security-wise — the keyspace is already 256 bits. Its real job
    here is to slow down anyone who tries to brute-force a leaked DB
    against a guessable master passphrase. Fixed salt is deliberate (the
    master key is per-deployment), and the cost factors are tuned to add
    a few-millisecond delay per call without burdening the import path.
    """
    derived = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=_SCRYPT_SALT,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
    )
    return base64.urlsafe_b64encode(derived)


def _validated_key() -> str:
    """Return the master key, after stripping whitespace and length-checking."""
    secret = os.environ.get(_KEY_ENV_VAR, "").strip()
    if not secret:
        raise SecretKeyMissingError(
            f"{_KEY_ENV_VAR} is not set; cannot encrypt or decrypt stored secrets."
        )
    if len(secret) < _MIN_KEY_LEN:
        raise SecretKeyTooWeakError(
            f"{_KEY_ENV_VAR} must be at least {_MIN_KEY_LEN} characters "
            f"(got {len(secret)}). Generate one with "
            f'`python3 -c "import secrets; print(secrets.token_hex(32))"`.'
        )
    return secret


def _get_fernet():
    """Build a Fernet instance from $ISTOTA_SECRET_KEY.

    Imported lazily so importing this module doesn't require ``cryptography``
    at all paths (e.g. CLI commands that never touch secrets).
    """
    from cryptography.fernet import Fernet  # noqa: PLC0415

    return Fernet(_derive_fernet_key(_validated_key()))


def secret_key_available() -> bool:
    """True iff $ISTOTA_SECRET_KEY is present and meets the length floor.

    Safe to call at import time — never raises.
    """
    secret = os.environ.get(_KEY_ENV_VAR, "").strip()
    return bool(secret) and len(secret) >= _MIN_KEY_LEN


@dataclass(frozen=True)
class SecretRef:
    """Identifies a stored secret. Plaintext value is never stored on this dataclass."""

    user_id: str
    service: str
    key: str


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a sqlite3 connection with the same timeout settings as ``db.get_db``.

    ``timeout=30.0`` keeps ``get_secret``'s SELECT-then-UPDATE pattern
    coexistent with the main DB connection pool. No row factory: every query in
    this module indexes positionally.
    """
    with sqlite_util.open_db(
        db_path, row_factory=False, busy_timeout_ms=None, foreign_keys=False,
        commit=True,
    ) as conn:
        yield conn


def upsert_secret(
    db_path: Path, user_id: str, service: str, key: str, value: str,
) -> str:
    """Idempotent secret upsert. Returns ``"created"``, ``"updated"``, or ``"noop"``.

    Same contract as ``user_briefings.ensure_briefing`` and
    ``db.upsert_user_resource``: compute the final state, only write when
    the value actually changes, return the state literal so the CLI / web UI
    can report it without re-deriving it.

    Empty ``value`` is rejected — use :func:`delete_secret` to clear.
    """
    if not value:
        raise ValueError("upsert_secret requires a non-empty value; use delete_secret to clear")

    existing = get_secret(db_path, user_id, service, key)
    if existing is None:
        state = "created"
    elif existing == value:
        state = "noop"
    else:
        state = "updated"

    if state != "noop":
        set_secret(db_path, user_id, service, key, value)
    return state


def set_secret(db_path: Path, user_id: str, service: str, key: str, value: str) -> None:
    """Encrypt and upsert a secret.

    Empty value deletes the row (UI sends ``""`` to clear). Idempotent.
    Bumps ``updated_at`` on overwrite.
    """
    if not value:
        delete_secret(db_path, user_id, service, key)
        return

    fernet = _get_fernet()
    token = fernet.encrypt(value.encode("utf-8"))

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO secrets (user_id, service, key, encrypted_value, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(user_id, service, key) DO UPDATE SET
                encrypted_value = excluded.encrypted_value,
                updated_at = datetime('now')
            """,
            (user_id, service, key, token),
        )


def get_secret(db_path: Path, user_id: str, service: str, key: str) -> str | None:
    """Decrypt and return a stored secret, or None if missing.

    Bumps ``last_accessed_at`` on every successful read so admins can see
    which credentials are actually in use.
    """
    if not secret_key_available():
        return None

    try:
        fernet = _get_fernet()
    except SecretKeyMissingError:
        return None

    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, encrypted_value FROM secrets "
            "WHERE user_id = ? AND service = ? AND key = ?",
            (user_id, service, key),
        ).fetchone()
        if row is None:
            return None

        secret_id, ciphertext = row[0], row[1]
        try:
            plaintext = fernet.decrypt(ciphertext).decode("utf-8")
        except Exception as e:
            logger.warning(
                "secret decrypt failed user=%s service=%s key=%s: %s "
                "(stale ISTOTA_SECRET_KEY?)",
                user_id, service, key, e,
            )
            return None

        # Bumping last_accessed_at is observability, not correctness — if
        # another writer holds the lock (e.g. a long-lived in-task
        # transaction), don't stall the read on it. Drop busy_timeout to
        # 100ms for this UPDATE so we fail fast and skip the bump.
        try:
            conn.execute("PRAGMA busy_timeout = 100")
            conn.execute(
                "UPDATE secrets SET last_accessed_at = datetime('now') WHERE id = ?",
                (secret_id,),
            )
        except sqlite3.OperationalError as e:
            logger.debug("skipped last_accessed_at update (locked?): %s", e)
        return plaintext


def delete_secret(db_path: Path, user_id: str, service: str, key: str) -> bool:
    """Delete a stored secret. Returns True if a row was removed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM secrets WHERE user_id = ? AND service = ? AND key = ?",
            (user_id, service, key),
        )
        return cur.rowcount > 0


def secret_exists(db_path: Path, user_id: str, service: str, key: str) -> bool:
    """True if a row is present, regardless of whether it can be decrypted.

    Distinct from ``get_secret`` returning ``None`` — that's ambiguous
    between "row missing" and "row present but undecryptable" (e.g. after
    operator key rotation). The import path uses this so a key rotation
    does not silently overwrite web-UI-managed values with stale TOML
    defaults on the next startup.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM secrets WHERE user_id = ? AND service = ? AND key = ? LIMIT 1",
            (user_id, service, key),
        ).fetchone()
    return row is not None


def get_service_secrets(db_path: Path, user_id: str, service: str) -> dict[str, str]:
    """Return all decrypted (key, value) pairs for ``(user_id, service)``.

    Single-query, single-connection alternative to looping ``get_secret``
    once per key. Skips rows that fail to decrypt (e.g. after key rotation)
    and bumps ``last_accessed_at`` for every successfully decrypted row in
    one UPDATE. Returns ``{}`` when nothing is configured or the master key
    is missing.

    Notifications dispatch reads several keys per call (topic, server_url,
    token, username, password); using this avoids 5× connect+PRAGMA WAL
    overhead per send.
    """
    if not secret_key_available():
        return {}
    try:
        fernet = _get_fernet()
    except SecretKeyMissingError:
        return {}

    out: dict[str, str] = {}
    seen_ids: list[int] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, key, encrypted_value FROM secrets "
            "WHERE user_id = ? AND service = ?",
            (user_id, service),
        ).fetchall()
        for secret_id, key, ciphertext in rows:
            try:
                out[key] = fernet.decrypt(ciphertext).decode("utf-8")
                seen_ids.append(secret_id)
            except Exception as e:
                logger.warning(
                    "secret decrypt failed user=%s service=%s key=%s: %s "
                    "(stale ISTOTA_SECRET_KEY?)",
                    user_id, service, key, e,
                )

        if seen_ids:
            try:
                conn.execute("PRAGMA busy_timeout = 100")
                placeholders = ",".join("?" * len(seen_ids))
                conn.execute(
                    f"UPDATE secrets SET last_accessed_at = datetime('now') "
                    f"WHERE id IN ({placeholders})",
                    seen_ids,
                )
            except sqlite3.OperationalError as e:
                logger.debug("skipped last_accessed_at update (locked?): %s", e)
    return out


def list_user_services(db_path: Path, user_id: str) -> dict[str, list[dict]]:
    """List which (service, key) pairs the user has configured.

    Returns ``{service: [{"key": ..., "updated_at": ..., "last_accessed_at": ...}]}``.
    Plaintext values are NEVER returned here — this is the read shape the web UI
    uses to render "configured" / "missing" status badges.
    """
    out: dict[str, list[dict]] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT service, key, updated_at, last_accessed_at FROM secrets "
            "WHERE user_id = ? ORDER BY service, key",
            (user_id,),
        ).fetchall()
        for service, key, updated_at, last_accessed_at in rows:
            out.setdefault(service, []).append({
                "key": key,
                "updated_at": updated_at,
                "last_accessed_at": last_accessed_at,
            })
    return out


def resolve_secret(
    db_path: Path,
    user_id: str,
    service: str,
    key: str,
    fallback_extras: dict | None = None,
    fallback_env: str | None = None,
) -> str | None:
    """Three-tier resolution (spec section 7).

    1. ``secrets`` table.
    2. ``fallback_extras[key]`` — typically a resource entry's extras dict.
    3. ``os.environ[fallback_env]`` — legacy/tier-1 overlap.
    """
    value = get_secret(db_path, user_id, service, key)
    if value:
        return value
    if fallback_extras and fallback_extras.get(key):
        return str(fallback_extras[key])
    if fallback_env:
        env_value = os.environ.get(fallback_env, "")
        if env_value:
            return env_value
    return None


# --- One-time import from config.toml resource entries --------------------
#
# When an existing TOML config carries credentials in resource extras
# (monarch_email/password, karakeep api_key, monarch session_token), this
# walks the user_config and copies them into the secrets table — but only
# when the (user, service, key) row is absent. Idempotent across restarts.
#
# We don't strip the values out of TOML afterwards. The resource entry stays
# valid for Ansible-managed deploys; the secrets table simply takes priority
# at resolution time.

# Per-resource-type credential field maps. For each resource type we list the
# secret_service it maps to plus (resource_attr, secret_key) pairs to copy.
# resource_attr "extra:foo" reads from the ResourceConfig.extra dict.
_IMPORT_MAP: dict[str, tuple[str, list[tuple[str, str]]]] = {
    # Monarch credentials moved to a cookie-pair (session_id + csrftoken)
    # auth model in 7c896c5; the legacy email/password/session_token columns
    # on the retired [[resources]] type=money/monarch blocks have nowhere
    # to land in the new schema. Empty lists keep the resource cleanup pass
    # in db.py:cleanup_obsolete_resources running, just with no field copies.
    "money": ("monarch", []),
    "monarch": ("monarch", []),
    # base_url / api_key for the retired ``karakeep`` [[resources]] type
    # live in ``ResourceConfig.extra`` after the Resources sunset (no longer
    # flat fields); the secrets table is the only place these values live
    # once the row is dropped by cleanup_obsolete_resources.
    "karakeep": ("karakeep", [
        ("extra:base_url", "base_url"),
        ("extra:api_key", "api_key"),
    ]),
    "feeds": ("feeds", [
        ("extra:tumblr_api_key", "tumblr_api_key"),
    ]),
    # Overland ingest token: per-user webhook auth for Overland GPS uploads.
    # The webhook receiver scans the secrets table at startup to build its
    # token → user_id map.
    "overland": ("overland", [
        ("extra:ingest_token", "ingest_token"),
    ]),
}


def _read_resource_attr(resource, attr: str) -> str:
    """Read either a flat field or an extras-dict entry from a ResourceConfig."""
    if attr.startswith("extra:"):
        key = attr[len("extra:"):]
        return str((resource.extra or {}).get(key, "") or "")
    return str(getattr(resource, attr, "") or "")


def import_from_user_configs(
    db_path: Path,
    user_configs: dict[str, "object"],
) -> int:
    """Walk loaded user configs; copy missing tier-2 credentials into secrets.

    ``user_configs`` is ``Config.users`` (dict[user_id, UserConfig]). Returns
    the number of new rows written. Skips users with no resources, skips
    fields that are empty in TOML, skips (user, service, key) rows that
    already exist. Safe to call on every startup.

    Hard requirement: ``ISTOTA_SECRET_KEY`` must be set. We log and return 0
    if not — the import is best-effort, and the resolver still works via
    fallback to TOML extras.
    """
    if not secret_key_available():
        # DEBUG, not INFO: this fires on every subprocess config load (the
        # feeds/money skill facades call load_config()), where the key is
        # intentionally absent — the master-key boundary. The daemon does the
        # real import at its own startup with the key present.
        logger.debug("secrets import skipped: ISTOTA_SECRET_KEY not set")
        return 0

    written = 0
    for user_id, user_config in user_configs.items():
        resources = getattr(user_config, "resources", []) or []
        for resource in resources:
            mapping = _IMPORT_MAP.get(getattr(resource, "type", ""))
            if not mapping:
                continue
            service, fields = mapping
            for attr, secret_key in fields:
                value = _read_resource_attr(resource, attr)
                if not value:
                    continue
                # Skip if a row is already present (idempotency). ``secret_exists``
                # is decrypt-free on purpose: ``get_secret`` returns ``None`` both
                # for "missing" and "undecryptable", and the latter triggers
                # unwanted re-writes of stale TOML over web-UI values whenever
                # the operator rotates ``ISTOTA_SECRET_KEY``. The presence check
                # also avoids bumping ``last_accessed_at`` on every startup, so
                # that column reflects real usage rather than import-loop noise.
                if secret_exists(db_path, user_id, service, secret_key):
                    continue
                try:
                    set_secret(db_path, user_id, service, secret_key, value)
                    written += 1
                except SecretKeyMissingError:
                    return written
                except Exception as e:
                    logger.warning(
                        "secrets import failed user=%s service=%s key=%s: %s",
                        user_id, service, secret_key, e,
                    )
    if written:
        logger.info("secrets import: wrote %d new secret(s) from config.toml", written)
    return written
