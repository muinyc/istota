"""One-shot importer: legacy ``invoicing.toml`` / ``tax.toml`` / ``monarch.toml``
→ per-user money DB.

Mirrors :mod:`istota.feeds._migrate`. Runs idempotently on first touch from
``resolve_for_user``, the web routes, the CLI and the skill facade (via
:func:`ensure_initialised`).

Each section (invoicing / tax / monarch) is migrated independently and gated
on its own ``schema_meta`` sentinel — a workspace with only ``monarch.toml``
gets the monarch sentinel set and leaves the other two unset.
"""

from __future__ import annotations

import logging
import os
import json
import sqlite3
import tomllib
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from istota.money import config_store
from istota.money.cli import UserContext
from istota.timestamps import iso_now as _iso_now


logger = logging.getLogger(__name__)


_SECTIONS = ("invoicing", "tax", "monarch")
_SENTINEL_KEYS = {
    "invoicing": "invoicing_legacy_imported_at",
    "tax": "tax_legacy_imported_at",
    "monarch": "monarch_legacy_imported_at",
}
_TOML_FILENAMES = {
    "invoicing": ("invoicing.toml", "INVOICING.md"),
    "tax": ("tax.toml", "TAX.md"),
    "monarch": ("monarch.toml", "MONARCH.md"),
}

_IDS_BACKFILLED_SENTINEL_KEY = "money_ids_backfilled_at"
_DEFAULT_LEDGER_SENTINEL_KEY = "money_default_ledger_seeded_at"
_DEFAULT_LEDGER_FILENAME = "main.beancount"
_BUNDLED_LEDGER_LABEL = "<bundled:istota.money:data/main.beancount.tmpl>"


def _config_search_dirs(ctx: UserContext) -> list[Path]:
    """Mirror :func:`workspace._config_search_dirs`'s default order.

    ``{data_dir}/config/`` first, then ``{workspace_root}/config/`` (which
    is ``data_dir.parent / "config"`` in the default workspace layout).
    """
    data_dir = Path(ctx.data_dir)
    return [data_dir / "config", data_dir.parent / "config"]


def _find_legacy_file(ctx: UserContext, section: str) -> Path | None:
    primary, legacy = _TOML_FILENAMES[section]
    for fname in (primary, legacy):
        for cd in _config_search_dirs(ctx):
            p = cd / fname
            if p.is_file():
                return p
    return None


def _read_toml(path: Path) -> dict:
    if path.suffix.lower() == ".md":
        from istota.money._config_io import read_toml_config
        return read_toml_config(path)
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _section_already_populated(db_path: Path, section: str) -> bool:
    if section == "invoicing":
        return config_store.has_invoicing_data(db_path)
    if section == "tax":
        return config_store.has_tax_data(db_path)
    if section == "monarch":
        return config_store.has_monarch_config_rows(db_path)
    return False


_LOCK_KEY_PREFIX = "_migrate_lock_"


def _claim_lock(db_path: Path, section: str) -> bool:
    """Race-safe transient lock. Returns True if we won the race.

    Used to serialize first-touch migration of a section. The lock is
    cleared by :func:`_finalize_migration` (success) or
    :func:`_release_lock` (parse / save failure) so a follow-up run can
    retry from a clean slate.
    """
    config_store.init_db(db_path)
    key = _LOCK_KEY_PREFIX + section
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (key, _iso_now()),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def _release_lock(db_path: Path, section: str) -> None:
    key = _LOCK_KEY_PREFIX + section
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (key,))
        conn.commit()


def _finalize_migration(db_path: Path, section: str) -> None:
    """Atomically: drop the lock and set the real sentinel.

    Called only after the importer + file rename succeeded. If we crash
    after the importer wrote rows but before this call, the next process
    sees the lock + populated tables and bails harmlessly via the
    populated-table check; the operator can clear the lock by retrying
    once the underlying issue is fixed.
    """
    sentinel_key = _SENTINEL_KEYS[section]
    lock_key = _LOCK_KEY_PREFIX + section
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DELETE FROM schema_meta WHERE key = ?", (lock_key,))
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            (sentinel_key, _iso_now()),
        )
        conn.commit()


_REJECTED_KEY_PREFIX = "_migrate_rejected_"


def _file_fingerprint(path: Path) -> str:
    """Enough to tell an edited file from an untouched one."""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{st.st_mtime_ns}:{st.st_size}"


def _remember_rejection(
    db_path: Path, section: str, path: Path, exc: Exception,
) -> None:
    config_store.set_meta(
        db_path, _REJECTED_KEY_PREFIX + section,
        json.dumps({"fingerprint": _file_fingerprint(path), "error": str(exc)}),
    )


def _rejection_still_stands(db_path: Path, section: str, path: Path) -> bool:
    """Whether this exact file was already refused.

    Without this the refusal is re-derived on every money call — a re-parse, a
    rolled-back write and a warning line per skill invocation and per web
    request, for as long as the file sits there. Keyed on the file rather than
    on the section so correcting it still imports on the next run.
    """
    raw = config_store.get_meta(db_path, _REJECTED_KEY_PREFIX + section)
    if not raw:
        return False
    try:
        recorded = json.loads(raw).get("fingerprint")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
    return bool(recorded) and recorded == _file_fingerprint(path)


def _clear_rejection(db_path: Path, section: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM schema_meta WHERE key = ?",
            (_REJECTED_KEY_PREFIX + section,),
        )
        conn.commit()


def _sentinel_present(db_path: Path, section: str) -> bool:
    return config_store.get_meta(db_path, _SENTINEL_KEYS[section]) is not None


def _rename_imported(path: Path) -> Path:
    target = path.with_name(path.name + ".imported")
    try:
        path.rename(target)
    except OSError as exc:
        logger.warning(
            "money_legacy_rename_failed path=%s error=%s", path, exc,
        )
        return path
    return target


def _import_invoicing(db_path: Path, data: dict) -> dict:
    cfg = config_store.invoicing_config_from_toml_dict(data)
    config_store.save_invoicing(db_path, cfg, replace_collections=True)
    return {
        "companies": len(cfg.companies),
        "clients": len(cfg.clients),
        "services": len(cfg.services),
    }


def _import_tax(db_path: Path, data: dict) -> dict:
    cfg = config_store.tax_config_from_toml_dict(data)
    # Legacy TOML import: these really are the user's own rates for
    # this config's own year and filing status.
    config_store.save_tax(db_path, cfg, replace_collections=True,
                          write_schedules=True)
    return {
        "tax_year": cfg.tax_year,
        "patterns": len(cfg.se_income_accounts) + len(cfg.se_expense_accounts),
        "year_rates": 1 if any(v is not None for v in (
            cfg.federal_brackets, cfg.state_brackets,
            cfg.federal_standard_deduction, cfg.state_standard_deduction,
            cfg.ss_wage_base, cfg.ss_rate,
        )) else 0,
    }


def _import_monarch(db_path: Path, data: dict) -> dict:
    cfg = config_store.monarch_config_from_toml_dict(data, secrets=None)
    config_store.save_monarch(db_path, cfg, replace_collections=True)
    return {"profiles": len(cfg.profiles)}


_IMPORTERS = {
    "invoicing": _import_invoicing,
    "tax": _import_tax,
    "monarch": _import_monarch,
}


def _migrate_section(ctx: UserContext, section: str) -> dict | None:
    """Migrate one section's TOML file. Returns summary or ``None``."""
    db_path = Path(ctx.db_path)
    config_store.init_db(db_path)

    if _sentinel_present(db_path, section):
        legacy_path = _find_legacy_file(ctx, section)
        if legacy_path is not None:
            logger.warning(
                "money_legacy_present_but_already_imported section=%s path=%s "
                "— file is no longer read; delete it",
                section, legacy_path,
            )
        return None

    legacy_path = _find_legacy_file(ctx, section)
    if legacy_path is None:
        return None

    if _section_already_populated(db_path, section):
        logger.warning(
            "money_legacy_present_but_db_populated section=%s path=%s "
            "— DB already has rows; delete or merge manually",
            section, legacy_path,
        )
        return None

    if _rejection_still_stands(db_path, section, legacy_path):
        return None

    if not _claim_lock(db_path, section):
        # Another worker won the race.
        return None

    try:
        try:
            parsed = _read_toml(legacy_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "money_legacy_unparseable section=%s path=%s error=%s",
                section, legacy_path, exc,
            )
            _release_lock(db_path, section)
            return None

        try:
            summary = _IMPORTERS[section](db_path, parsed)
        except config_store.InvalidAccountError as exc:
            # Content the importer refuses, same class as an unparseable file.
            # `ensure_initialised` runs on every money call, so raising here
            # would take the module down for that user until the file was fixed.
            logger.warning(
                "money_legacy_rejected section=%s path=%s error=%s",
                section, legacy_path, exc,
            )
            _remember_rejection(db_path, section, legacy_path, exc)
            _release_lock(db_path, section)
            return None
        except Exception:
            # Importer failed; release the lock so the next run can retry.
            _release_lock(db_path, section)
            raise

        new_path = _rename_imported(legacy_path)
        _clear_rejection(db_path, section)
        _finalize_migration(db_path, section)
    except Exception:
        raise

    logger.info(
        "money_legacy_imported section=%s path=%s renamed=%s summary=%s",
        section, legacy_path, new_path, summary,
    )
    return {
        "section": section,
        "path": str(legacy_path),
        "renamed_to": str(new_path),
        **summary,
    }


def migrate_legacy_workspace_config(ctx: UserContext) -> dict | None:
    """Run all three section migrations. Returns a dict summarizing results.

    Returns ``None`` only when nothing happened across every section.
    """
    summaries: dict[str, Any] = {}
    for section in _SECTIONS:
        result = _migrate_section(ctx, section)
        if result is not None:
            summaries[section] = result
    return summaries or None


def _read_bundled_default_ledger() -> str | None:
    """Read the package-shipped ``main.beancount.tmpl`` as text.

    Uses :mod:`importlib.resources` so the lookup works from a wheel
    install, an editable checkout, or a zipapp/PyInstaller bundle.
    Returns ``None`` if the package was built without the data file.
    """
    try:
        resource = files("istota.money").joinpath("data/main.beancount.tmpl")
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    try:
        with as_file(resource) as concrete:
            if not concrete.is_file():
                return None
            return concrete.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None


def _default_ledger_fs_candidates(ctx: UserContext) -> list[Path]:
    """Filesystem paths to probe for a ``main.beancount`` override.

    Per-user override (``{data_dir}/config/main.beancount``) wins over
    the workspace-level override (``{data_dir.parent}/config/main.beancount``
    in the default workspace layout). The bundled default is the final
    fallback and intentionally not included here.
    """
    out: list[Path] = []
    seen: set[Path] = set()

    data_dir = Path(ctx.data_dir)
    primary = data_dir / "config" / _DEFAULT_LEDGER_FILENAME
    out.append(primary)
    seen.add(primary)

    workspace = data_dir.parent / "config" / _DEFAULT_LEDGER_FILENAME
    if workspace not in seen:
        out.append(workspace)

    return out


def _resolve_default_ledger(ctx: UserContext) -> tuple[str, str] | None:
    """Return ``(ledger_text, source_label)`` for the highest-priority
    available defaults file, or ``None`` if none are available.

    File-system override paths are tried first; on miss we fall back to
    the package-shipped bundled template. The label is the absolute path
    for FS sources and a stable sentinel string for the bundled resource.
    """
    for candidate in _default_ledger_fs_candidates(ctx):
        if not candidate.is_file():
            continue
        try:
            return candidate.read_text(encoding="utf-8"), str(candidate)
        except OSError as e:
            logger.warning(
                "money_default_ledger_unreadable path=%s error=%s",
                candidate, e,
            )
            return None
    bundled = _read_bundled_default_ledger()
    if bundled is not None:
        return bundled, _BUNDLED_LEDGER_LABEL
    return None


def _ledgers_dir_has_beancount(ledgers_dir: Path) -> bool:
    if not ledgers_dir.is_dir():
        return False
    for entry in ledgers_dir.iterdir():
        if entry.is_file() and entry.suffix == ".beancount":
            return True
    return False


def _try_write_ledger_sentinel(db_path: Path) -> bool:
    """Insert the ``money_default_ledger_seeded_at`` row.

    Returns True on success, False on PK collision (another process or
    a previous run already claimed the slot).
    """
    config_store.init_db(db_path)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (_DEFAULT_LEDGER_SENTINEL_KEY, _iso_now()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return False
    return True


def seed_default_ledger(ctx: UserContext) -> dict | None:
    """Seed the per-user money workspace with a starter beancount ledger.

    Runs at most once per user. Mirrors
    :func:`istota.feeds._migrate.seed_default_opml`. Successful runs write a
    ``money_default_ledger_seeded_at`` row in ``schema_meta``; the next
    call sees the row and bails. Skip / abort cases:

    * ``ISTOTA_MONEY_SKIP_DEFAULT_SEED`` is set (ops opt-out / test
      suite). No sentinel is written — clearing the env var lets seeding
      run on the next call.
    * Sentinel already set — no-op.
    * ``{data_dir}/ledgers/`` already contains a ``*.beancount`` file
      (legacy / manual setup) — we record the sentinel so we don't
      re-probe on every boot.
    * No defaults file found anywhere — return without writing the
      sentinel, so a later release shipping the template or an operator
      drop-in still triggers seeding on a subsequent boot.
    * Defaults file unreadable — return without writing the sentinel,
      so fixing the file unblocks seeding.

    Resolution order: ``{data_dir}/config/main.beancount`` →
    ``{data_dir.parent}/config/main.beancount`` → bundled
    ``istota.money:data/main.beancount.tmpl``.

    Reset incantation: ``DELETE FROM schema_meta WHERE
    key='money_default_ledger_seeded_at'`` against the per-user money DB,
    and remove ``{data_dir}/ledgers/main.beancount`` if you want it
    re-seeded.
    """
    if os.environ.get("ISTOTA_MONEY_SKIP_DEFAULT_SEED"):
        return None

    data_dir = Path(ctx.data_dir)
    db_path = Path(ctx.db_path) if ctx.db_path else (data_dir / "data" / "money.db")
    config_store.init_db(db_path)

    if config_store.get_meta(db_path, _DEFAULT_LEDGER_SENTINEL_KEY):
        return None

    ledgers_dir = data_dir / "ledgers"
    if _ledgers_dir_has_beancount(ledgers_dir):
        # User got a ledger from another path (manual create, restore from
        # backup, etc). Burn the sentinel so we don't re-probe forever.
        _try_write_ledger_sentinel(db_path)
        return None

    resolved = _resolve_default_ledger(ctx)
    if resolved is None:
        # Nothing to do, but intentionally don't write the sentinel: a
        # later release that ships the bundled template, or an operator
        # drop-in, should still be picked up on a subsequent call.
        logger.debug(
            "money_default_ledger_no_source data_dir=%s", data_dir,
        )
        return None

    ledger_text, source_label = resolved

    ledgers_dir.mkdir(parents=True, exist_ok=True)
    target = ledgers_dir / _DEFAULT_LEDGER_FILENAME
    if target.exists():
        # Defensive: we already passed the .beancount-presence check, but
        # something raced us. Burn the sentinel and bail rather than
        # overwrite the user's file.
        _try_write_ledger_sentinel(db_path)
        return None

    try:
        target.write_text(ledger_text, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "money_default_ledger_write_failed path=%s error=%s",
            target, exc,
        )
        return None

    _try_write_ledger_sentinel(db_path)

    logger.info(
        "money_default_ledger_seeded source=%s target=%s",
        source_label, target,
    )
    return {
        "source": source_label,
        "path": str(target),
    }


def backfill_transaction_ids(ctx: UserContext) -> dict | None:
    """Stamp stable ``id:`` metadata onto every legacy transaction, once.

    Editing a transaction requires a stable handle; legacy ledgers predate the
    ``id:`` convention. This runs the additive backfill (see
    :func:`istota.money.core.edit.backfill_ledger_ids`) across every configured
    ledger exactly once per user, gated by a ``money_ids_backfilled_at``
    sentinel. Idempotent and reversible (each file is backed up + re-validated
    with ``bean-check``; a failed re-check rolls the file back).

    Skip / abort cases mirror :func:`seed_default_ledger`:

    * ``ISTOTA_MONEY_SKIP_ID_BACKFILL`` set (ops opt-out / test suite) — no-op,
      no sentinel written.
    * Sentinel already set — no-op.
    * A ledger fails to backfill (pre-existing errors / failed re-check) — the
      sentinel is NOT written, so a later run retries once the ledger is fixed.
    """
    if os.environ.get("ISTOTA_MONEY_SKIP_ID_BACKFILL"):
        return None

    data_dir = Path(ctx.data_dir)
    db_path = Path(ctx.db_path) if ctx.db_path else (data_dir / "data" / "money.db")
    config_store.init_db(db_path)

    if config_store.get_meta(db_path, _IDS_BACKFILLED_SENTINEL_KEY):
        return None

    from istota.money.core.edit import backfill_ledger_ids

    total = 0
    failures: list[dict] = []
    for entry in ctx.ledgers:
        ledger_path = entry["path"]
        if not Path(ledger_path).exists():
            continue
        result = backfill_ledger_ids(ledger_path)
        if result.get("status") == "ok":
            total += int(result.get("stamped", 0))
        else:
            failures.append({"ledger": entry.get("name"), **result})

    if failures:
        logger.warning(
            "money_id_backfill_failed failures=%s — sentinel not set; will retry",
            failures,
        )
        return {"stamped": total, "failures": failures}

    # All ledgers backfilled (or empty) — burn the sentinel.
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                (_IDS_BACKFILLED_SENTINEL_KEY, _iso_now()),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.warning("money_id_backfill_sentinel_failed error=%s", exc)

    if total:
        logger.info("money_id_backfill_done stamped=%s", total)
    return {"stamped": total}


def ensure_initialised(ctx: UserContext) -> None:
    """Wire up a money workspace for use.

    Creates the data + ledgers dirs (idempotent), initialises the DB schema,
    runs the legacy migration (no-ops on subsequent runs), seeds a starter
    beancount ledger if the workspace is empty, and backfills stable
    transaction ids (once). Safe to call from every entry point.
    """
    data_dir = Path(ctx.data_dir)
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    (data_dir / "ledgers").mkdir(parents=True, exist_ok=True)

    db_path = ctx.db_path or (data_dir / "data" / "money.db")
    config_store.init_db(db_path)
    # Tracking + portfolio schema families. The CLI's _get_db_conn used to be
    # the only caller of db.init_db, which left web-route-only paths without
    # the portfolio tables.
    from istota.money.db import init_db as tracking_init_db
    tracking_init_db(db_path)
    if ctx.db_path is None:
        ctx.db_path = db_path

    migrate_legacy_workspace_config(ctx)
    # Fold the legacy year-keyed bracket columns into the status-keyed table.
    # Markered, so this is a no-op after the first run.
    config_store.migrate_tax_schedules(db_path)
    seed_default_ledger(ctx)
    backfill_transaction_ids(ctx)
    backfill_work_entry_ids(ctx)


def backfill_work_entry_ids(ctx: UserContext) -> int:
    """Stamp a stable ``uid`` on every work entry lacking one.

    The work-entry counterpart of :func:`backfill_transaction_ids`, and the
    thing that makes the web UI's uid-addressed edits safe. Unlike the ledger
    backfill this carries no sentinel — a hand-added entry with no ``uid`` can
    show up at any time, and re-running is how that self-heals. Cheap: the
    "is anything missing?" check is a lock-free read, so the common no-op case
    doesn't contend with a concurrent ``invoice generate`` — this runs on every
    money web request and every skill invocation.

    Never fatal — a failure here must not stop a money command from running.
    """
    from istota.money.work import backfill_work_ids

    try:
        stamped = backfill_work_ids(Path(ctx.data_dir))
    except Exception as exc:  # noqa: BLE001 — advisory, never fatal
        logger.warning("money_work_id_backfill_failed error=%s", exc)
        return 0
    if stamped:
        logger.info("money_work_id_backfill_done stamped=%s", stamped)
    return stamped
