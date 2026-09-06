"""Document storage for the health module.

Bytes on disk under ``{uploads_dir}/documents/{id}/{filename}``, metadata in
the per-user health DB (``documents`` + ``document_links``). Kept out of
:mod:`istota.health.routes` so the skill CLI and the deferred-op replayer
share the same code path verbatim rather than reimplementing containment
and sanitisation.

The parent is ``uploads_dir`` — the same root ``/panels/{id}/source`` already
guards — so one containment rule (``relative_to(uploads_dir)``) covers both
panel sources and documents.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import time
from pathlib import Path

from istota.atomic_write import write_bytes_atomic
from istota.health import db as health_db
from istota.health.models import Document, HealthContext


logger = logging.getLogger(__name__)


# Deliberately narrow. Everything here is either a scan (PDF / image) or a
# plain-text export; anything active (HTML, SVG) is refused at the door
# rather than relying on the Content-Disposition header alone.
_ALLOWED_DOCUMENT_MIMES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "image/tiff",
    "text/plain",
})

DEFAULT_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

_DOCUMENTS_SUBDIR = "documents"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_RUNS = re.compile(r"_{2,}")

_MAX_STEM_CHARS = 100


class DocumentError(Exception):
    """Base for document-storage refusals."""


class UnsupportedDocumentType(DocumentError):
    pass


class DocumentTooLarge(DocumentError):
    pass


class UnknownEntity(DocumentError):
    """The encounter / diagnosis / immunization to attach to doesn't exist."""


def sanitize_document_filename(raw: str) -> str:
    """Derive a safe on-disk name from an untrusted client filename.

    Each document lives in its own numbered directory, so this does not have
    to guarantee uniqueness — only that the name can't escape that directory
    or hide from a file browser.
    """
    name = Path(raw or "").name
    # A Windows client may send a backslash path; Path.name won't split it.
    name = name.rsplit("\\", 1)[-1]
    ext = Path(name).suffix
    stem = name[: len(name) - len(ext)] if ext else name

    ext = _UNSAFE_CHARS.sub("_", ext)
    ext = _RUNS.sub("_", ext)
    if ext in (".", "_", ""):
        ext = ""

    stem = _UNSAFE_CHARS.sub("_", stem)
    stem = _RUNS.sub("_", stem)
    stem = stem.lstrip(".")           # no hidden files
    stem = stem.strip("_.")
    stem = stem[:_MAX_STEM_CHARS]
    stem = stem.strip("_.")

    if not stem:
        return f"document{ext}" if ext else "document.bin"
    return f"{stem}{ext}" if ext else stem


def relative_document_path(document_id: int, filename: str) -> str:
    """The ``stored_path`` for a document — relative to ``uploads_dir``."""
    return f"{_DOCUMENTS_SUBDIR}/{int(document_id)}/{filename}"


def resolve_document_path(ctx: HealthContext, doc: Document) -> Path:
    """Absolute path of a document's bytes, containment-checked.

    Raises :class:`ValueError` when the stored path resolves outside
    ``uploads_dir`` — the second line of defence behind sanitisation, and
    the one that matters for a row written before this code existed or by
    something other than :func:`store_document`.
    """
    uploads_root = Path(ctx.uploads_dir).resolve()
    candidate = (Path(ctx.uploads_dir) / doc.stored_path).resolve()
    try:
        candidate.relative_to(uploads_root)
    except ValueError:
        raise ValueError("invalid source path")
    return candidate


# Leading bytes that identify a format unambiguously. Consulted only when the
# declared type and the filename both fail to name one — an email attachment
# saved without an extension is the common case, and refusing a perfectly good
# scan for want of a ".pdf" is a poor answer.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def _sniff_mime(raw: bytes) -> str | None:
    for prefix, mime in _MAGIC:
        if raw.startswith(prefix):
            return mime
    # RIFF....WEBP
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    # ....ftypheic / ftypheix / ftypmif1 (HEIF brands)
    if raw[4:8] == b"ftyp" and raw[8:12] in (
        b"heic", b"heix", b"hevc", b"mif1", b"msf1",
    ):
        return "image/heic"
    return None


def _resolve_mime(mime: str | None, filename: str, raw: bytes = b"") -> str:
    import mimetypes

    if mime:
        # Strip any "; charset=..." parameter the client tacked on.
        mime = mime.split(";")[0].strip().lower()
    if not mime or mime == "application/octet-stream":
        guessed = mimetypes.guess_type(filename)[0]
        if guessed:
            mime = guessed.lower()
    if (not mime or mime == "application/octet-stream") and raw:
        sniffed = _sniff_mime(raw)
        if sniffed:
            mime = sniffed
    return mime or "application/octet-stream"


def _clear_document_dir(target_dir: Path) -> None:
    """Empty a document's numbered directory without removing it."""
    if not target_dir.is_dir():
        return
    for child in target_dir.iterdir():
        if child.is_file() or child.is_symlink():
            try:
                child.unlink()
            except OSError as e:
                logger.warning(
                    "health_document_dir_clear_failed path=%s error=%s",
                    child, e,
                )


def _write_bytes(target: Path, raw: bytes) -> None:
    """Write via a staging sibling + rename, so a torn write is never visible
    under the final name.

    The staging name used to be a fixed ``.{name}.part`` shared by every
    writer, with nothing removing it on a failure: two uploads of one document
    interleaved into one file and published a mixture. ``atomic_write`` mints
    it per call and unlinks it on any failure.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(target, raw)


def _heal_missing_bytes(ctx: HealthContext, doc: Document, raw: bytes) -> None:
    """Re-materialise a deduped document whose file has gone missing.

    Without this, once a document's bytes are lost (a manual workspace edit, a
    failed sync) re-uploading the identical file is a silent no-op: the hash
    matches, the broken row comes back, and `/documents/{id}/file` 404s
    forever. The upload "succeeds" and fixes nothing.
    """
    try:
        path = resolve_document_path(ctx, doc)
    except ValueError:
        return
    if path.is_file():
        return
    try:
        _write_bytes(path, raw)
    except OSError as e:
        logger.error(
            "health_document_heal_failed id=%d path=%s error=%s",
            doc.id, path, e,
        )
        return
    logger.info("health_document_healed id=%d path=%s", doc.id, path)


def store_document(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    raw: bytes,
    filename: str,
    mime: str | None,
    source: str = "manual",
    ocr_text: str | None = None,
    notes: str | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> tuple[Document, bool]:
    """Write bytes + insert a row. Returns ``(document, created)``.

    ``created`` is False when a document with identical bytes was already
    stored: the existing row is returned and nothing new is written. That is
    the normal case, not an error — attaching one discharge summary to both
    the encounter and the condition it produced should cost one file (D4).
    The dedup hit still *touches* the row, restarting its orphan clock: a
    re-uploaded old scan is being actively used again and must survive its
    own review screen.

    The bytes are written before the caller commits (no caller commits
    mid-function), so a failure after this returns leaves a blob on disk with
    no row. `sweep_orphan_documents` reclaims those directories as well as
    orphaned rows — without that second pass they would be invisible forever,
    since every DB-driven enumeration starts from a row that doesn't exist.
    """
    if not raw:
        raise DocumentError("empty upload")

    resolved_mime = _resolve_mime(mime, filename, raw)
    if resolved_mime not in _ALLOWED_DOCUMENT_MIMES:
        raise UnsupportedDocumentType(
            f"unsupported document type: {resolved_mime}",
        )
    if max_bytes and len(raw) > max_bytes:
        raise DocumentTooLarge(f"document exceeds {max_bytes} bytes")

    content_hash = hashlib.sha256(raw).hexdigest()
    existing = health_db.find_document_by_hash(conn, content_hash)
    if existing is not None:
        health_db.touch_document(conn, existing.id)
        _heal_missing_bytes(ctx, existing, raw)
        return existing, False

    safe_name = sanitize_document_filename(filename)
    try:
        doc_id = health_db.insert_document(
            conn,
            filename=safe_name,
            original_filename=filename or None,
            mime=resolved_mime,
            byte_size=len(raw),
            content_hash=content_hash,
            # Placeholder: the real path needs the id the insert just minted.
            stored_path=relative_document_path(0, safe_name),
            ocr_text=ocr_text,
            source=source,
            notes=notes,
        )
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent attach of the same bytes — the
        # UNIQUE index on content_hash fired. Resolve to the same outcome as
        # a plain duplicate upload.
        duplicate = health_db.find_document_by_hash(conn, content_hash)
        if duplicate is None:
            raise
        return duplicate, False

    rel = relative_document_path(doc_id, safe_name)
    conn.execute(
        "UPDATE documents SET stored_path = ? WHERE id = ?", (rel, doc_id),
    )

    target_dir = Path(ctx.uploads_dir) / _DOCUMENTS_SUBDIR / str(doc_id)
    # A rolled-back predecessor can leave a stale file under a reused id
    # (AUTOINCREMENT hands the number back). Clear the directory first, or
    # the leftover shares the dir with the live document and blocks the
    # rmdir in delete_document_fully.
    _clear_document_dir(target_dir)
    _write_bytes(target_dir / safe_name, raw)

    doc = health_db.get_document(conn, doc_id)
    if doc is None:  # pragma: no cover — the insert just succeeded
        raise DocumentError("document vanished after insert")
    return doc, True


def attach_document(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    raw: bytes,
    filename: str,
    mime: str | None,
    entity_type: str,
    entity_id: int,
    source: str = "manual",
    ocr_text: str | None = None,
    notes: str | None = None,
    max_bytes: int = DEFAULT_MAX_DOCUMENT_BYTES,
) -> tuple[Document, bool]:
    """:func:`store_document` + ``link_document`` in one call.

    Both the entity type and the entity's existence are checked **before**
    anything is stored. A link to a record that isn't there is worse than an
    error: `entity_id` is polymorphic with no FK, so the document would be
    invisible on every page *and* permanently exempt from the orphan sweep,
    because it does have a link. Validating first also means a bad reference
    costs no bytes on disk.
    """
    if not health_db.entity_exists(conn, entity_type, entity_id):
        raise UnknownEntity(f"{entity_type} not found")
    doc, created = store_document(
        conn, ctx, raw=raw, filename=filename, mime=mime, source=source,
        ocr_text=ocr_text, notes=notes, max_bytes=max_bytes,
    )
    health_db.link_document(conn, doc.id, entity_type, entity_id)
    return doc, created


def delete_document_fully(
    conn: sqlite3.Connection, ctx: HealthContext, document_id: int,
) -> bool:
    """Remove links, row, bytes, and the now-empty numbered directory."""
    doc = health_db.get_document(conn, document_id)
    if doc is None:
        return False
    try:
        path = resolve_document_path(ctx, doc)
    except ValueError:
        # A row whose stored_path escapes uploads_dir: drop the row but
        # never touch the file it points at.
        logger.error(
            "health_document_path_invalid id=%d stored_path=%s",
            doc.id, doc.stored_path,
        )
        path = None
    health_db.delete_document(conn, document_id)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.name == str(doc.id) and parent.parent.name == _DOCUMENTS_SUBDIR:
                # Only ever the document's own numbered directory. Clear it
                # first: a rolled-back predecessor under a reused id can have
                # left a stale sibling, and rmdir would silently fail on it.
                _clear_document_dir(parent)
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except OSError as e:
            logger.error(
                "health_document_unlink_failed id=%d path=%s error=%s",
                doc.id, path, e,
            )
    return True


def sweep_orphan_documents(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    older_than_hours: float = 24.0,
) -> int:
    """Delete documents that have had no links for longer than the window.

    Two passes, because a document can be orphaned in either direction. The
    row pass drops linkless rows (and their bytes); the directory pass drops
    bytes with no row — `store_document` writes the file before the caller
    commits, so any rollback after it returns strands a blob that no
    DB-driven enumeration would ever reach.

    Returns the count of deleted *rows*; stranded directories are logged.
    """
    ids = health_db.orphan_document_ids(conn, older_than_hours=older_than_hours)
    swept = 0
    for did in ids:
        try:
            if delete_document_fully(conn, ctx, did):
                swept += 1
        except sqlite3.Error as e:
            logger.error("health_document_sweep_failed id=%d error=%s", did, e)
    if swept:
        logger.info("health_document_sweep swept=%d", swept)
    _sweep_stranded_dirs(conn, ctx, older_than_hours=older_than_hours)
    return swept


def _sweep_stranded_dirs(
    conn: sqlite3.Connection,
    ctx: HealthContext,
    *,
    older_than_hours: float,
) -> int:
    """Remove `{uploads_dir}/documents/<id>/` directories with no row.

    Age-gated on directory mtime for the same reason the row pass is gated:
    `store_document` creates the directory before its row is committed, so a
    document being written *right now* legitimately looks stranded.
    """
    root = Path(ctx.uploads_dir) / _DOCUMENTS_SUBDIR
    if not root.is_dir():
        return 0
    cutoff = time.time() - older_than_hours * 3600.0
    removed = 0
    try:
        children = list(root.iterdir())
    except OSError as e:
        logger.warning("health_document_dir_scan_failed error=%s", e)
        return 0
    for child in children:
        if not child.is_dir() or not child.name.isdigit():
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if health_db.get_document(conn, int(child.name)) is not None:
            continue
        try:
            shutil.rmtree(child)
        except OSError as e:
            logger.warning(
                "health_document_dir_sweep_failed path=%s error=%s", child, e,
            )
            continue
        removed += 1
    if removed:
        logger.info("health_document_dir_sweep removed=%d", removed)
    return removed
