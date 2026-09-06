"""Tests for the health document storage layer (no HTTP)."""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from istota.health import db as health_db
from istota.health import documents as health_documents
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


PDF = b"%PDF-1.4 fake pdf bytes"


def _ctx(tmp_path):
    ctx = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(ctx)
    return ctx


def _backdate(conn, document_id, *, hours_ago):
    """Age a document, clock included — the sweep keys on last_touched_at."""
    stamp = (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat()
    conn.execute(
        "UPDATE documents SET created_at = ?, last_touched_at = ? WHERE id = ?",
        (stamp, stamp, document_id),
    )


class TestSanitizeFilename:
    @pytest.mark.parametrize("raw,expected", [
        ("discharge-summary.pdf", "discharge-summary.pdf"),
        ("../../etc/passwd", "passwd"),
        ("../../x.pdf", "x.pdf"),
        (r"C:\Users\bob\scan.pdf", "scan.pdf"),
        ("my scan (2).pdf", "my_scan_2.pdf"),
        ("Résumé.pdf", "R_sum.pdf"),
        ("???.pdf", "document.pdf"),
        ("", "document.bin"),
        ("   ", "document.bin"),
        (".hidden.pdf", "hidden.pdf"),
        ("noextension", "noextension"),
        ("...", "document.bin"),
    ])
    def test_table(self, raw, expected):
        assert health_documents.sanitize_document_filename(raw) == expected

    def test_truncates_long_stem_keeping_extension(self):
        out = health_documents.sanitize_document_filename("a" * 300 + ".pdf")
        assert out.endswith(".pdf")
        assert len(out) == 100 + len(".pdf")

    def test_never_escapes_its_directory(self):
        for raw in ("../../../etc/passwd", "/etc/passwd", "..", "./../x"):
            out = health_documents.sanitize_document_filename(raw)
            assert "/" not in out and out not in ("..", ".")


class TestStoreDocument:
    def test_writes_bytes_and_row(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, created = health_documents.store_document(
                conn, ctx, raw=PDF, filename="discharge.pdf",
                mime="application/pdf",
            )
            conn.commit()
        assert created is True
        assert doc.stored_path == f"documents/{doc.id}/discharge.pdf"
        assert doc.byte_size == len(PDF)
        assert doc.mime == "application/pdf"
        assert doc.source == "manual"
        on_disk = ctx.uploads_dir / doc.stored_path
        assert on_disk.is_file()
        assert on_disk.read_bytes() == PDF
        # No staging leftovers. Every entry rather than a `.part` glob: the
        # staging name is minted by `atomic_write` and carries no fixed suffix,
        # so a suffix glob would pass whether or not one was left behind.
        assert [p.name for p in on_disk.parent.iterdir()] == [on_disk.name]

    def test_duplicate_bytes_reuse_the_row(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            first, created_a = health_documents.store_document(
                conn, ctx, raw=PDF, filename="a.pdf", mime="application/pdf",
            )
            second, created_b = health_documents.store_document(
                conn, ctx, raw=PDF, filename="b.pdf", mime="application/pdf",
            )
            conn.commit()
            rows = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
        assert created_a is True
        assert created_b is False
        assert first.id == second.id
        assert rows["n"] == 1
        assert not (ctx.uploads_dir / "documents" / str(first.id) / "b.pdf").exists()

    def test_rejects_unsupported_mime_and_writes_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(health_documents.UnsupportedDocumentType):
                health_documents.store_document(
                    conn, ctx, raw=b"<script>x</script>", filename="x.html",
                    mime="text/html",
                )
            conn.commit()
            n = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        assert n == 0
        assert not (ctx.uploads_dir / "documents").exists()

    def test_rejects_oversize_and_writes_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(health_documents.DocumentTooLarge):
                health_documents.store_document(
                    conn, ctx, raw=b"x" * 100, filename="big.pdf",
                    mime="application/pdf", max_bytes=10,
                )
            conn.commit()
            n = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        assert n == 0

    def test_zero_max_bytes_means_unlimited(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=b"x" * 5000, filename="big.pdf",
                mime="application/pdf", max_bytes=0,
            )
            conn.commit()
        assert doc.byte_size == 5000

    def test_rejects_empty(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(health_documents.DocumentError):
                health_documents.store_document(
                    conn, ctx, raw=b"", filename="x.pdf",
                    mime="application/pdf",
                )

    def test_mime_guessed_from_filename_when_octet_stream(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="scan.pdf",
                mime="application/octet-stream",
            )
            conn.commit()
        assert doc.mime == "application/pdf"

    def test_charset_parameter_is_stripped(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=b"notes", filename="notes.txt",
                mime="text/plain; charset=utf-8",
            )
            conn.commit()
        assert doc.mime == "text/plain"

    def test_traversal_filename_lands_in_its_own_directory(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="../../../etc/passwd.pdf",
                mime="application/pdf",
            )
            conn.commit()
        resolved = health_documents.resolve_document_path(ctx, doc)
        assert resolved.parent.name == str(doc.id)
        assert resolved.is_file()

    def test_hash_race_resolves_to_existing_row(self, tmp_path, monkeypatch):
        """A concurrent attach of identical bytes must not raise."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            existing, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="a.pdf", mime="application/pdf",
            )
            conn.commit()

            calls = {"n": 0}
            real_find = health_db.find_document_by_hash

            def flaky_find(c, h):
                # First lookup misses (as it would in the losing racer),
                # forcing the insert; the post-IntegrityError lookup hits.
                calls["n"] += 1
                if calls["n"] == 1:
                    return None
                return real_find(c, h)

            monkeypatch.setattr(
                health_documents.health_db, "find_document_by_hash", flaky_find,
            )
            doc, created = health_documents.store_document(
                conn, ctx, raw=PDF, filename="a.pdf", mime="application/pdf",
            )
        assert created is False
        assert doc.id == existing.id


class TestResolveDocumentPath:
    def test_rejects_escaping_stored_path(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            did = health_db.insert_document(
                conn, filename="passwd", mime="text/plain", byte_size=1,
                content_hash="deadbeef",
                stored_path="../../../etc/passwd",
            )
            conn.commit()
            doc = health_db.get_document(conn, did)
        with pytest.raises(ValueError):
            health_documents.resolve_document_path(ctx, doc)


class TestAttachDocument:
    def test_refuses_a_nonexistent_entity_and_writes_nothing(self, tmp_path):
        """A link to a record that isn't there is worse than an error.

        `entity_id` is polymorphic with no FK, so the document would be
        invisible on every page *and* permanently exempt from the orphan
        sweep — it does have a link. Nothing may be written.
        """
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(health_documents.UnknownEntity):
                health_documents.attach_document(
                    conn, ctx, raw=PDF, filename="v.pdf",
                    mime="application/pdf", entity_type="encounter",
                    entity_id=99999,
                )
            conn.commit()
            n = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            links = conn.execute(
                "SELECT COUNT(*) AS n FROM document_links",
            ).fetchone()["n"]
        assert n == 0
        assert links == 0
        assert not (ctx.uploads_dir / "documents").exists()

    def test_refuses_an_unknown_entity_type(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(ValueError):
                health_documents.attach_document(
                    conn, ctx, raw=PDF, filename="v.pdf",
                    mime="application/pdf", entity_type="panel", entity_id=1,
                )


    def test_stores_and_links(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, created = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="visit.pdf",
                mime="application/pdf", entity_type="encounter",
                entity_id=eid, source="agent",
            )
            conn.commit()
            linked = health_db.documents_for_entity(conn, "encounter", eid)
        assert created is True
        assert doc.source == "agent"
        assert [d.id for d in linked] == [doc.id]


class TestDeleteDocumentFully:
    def test_removes_row_links_bytes_and_directory(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="visit.pdf",
                mime="application/pdf", entity_type="encounter", entity_id=eid,
            )
            conn.commit()
            path = health_documents.resolve_document_path(ctx, doc)
            assert path.is_file()

            assert health_documents.delete_document_fully(conn, ctx, doc.id) is True
            conn.commit()

            assert health_db.get_document(conn, doc.id) is None
            assert health_db.documents_for_entity(conn, "encounter", eid) == []
        assert not path.exists()
        assert not path.parent.exists()

    def test_missing_document_returns_false(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            assert health_documents.delete_document_fully(conn, ctx, 999) is False

    def test_tolerates_bytes_already_gone(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            conn.commit()
            health_documents.resolve_document_path(ctx, doc).unlink()
            assert health_documents.delete_document_fully(conn, ctx, doc.id) is True
            conn.commit()
            assert health_db.get_document(conn, doc.id) is None


class TestSweepOrphanDocuments:
    def test_deletes_old_linkless_keeps_recent_and_linked(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            old_orphan, _ = health_documents.store_document(
                conn, ctx, raw=b"old orphan pdf", filename="a.pdf",
                mime="application/pdf",
            )
            fresh_orphan, _ = health_documents.store_document(
                conn, ctx, raw=b"fresh orphan pdf", filename="b.pdf",
                mime="application/pdf",
            )
            linked, _ = health_documents.attach_document(
                conn, ctx, raw=b"linked pdf", filename="c.pdf",
                mime="application/pdf", entity_type="encounter", entity_id=eid,
            )
            _backdate(conn, old_orphan.id, hours_ago=48)
            _backdate(conn, linked.id, hours_ago=1000)
            conn.commit()

            swept = health_documents.sweep_orphan_documents(conn, ctx)
            conn.commit()

            assert swept == 1
            assert health_db.get_document(conn, old_orphan.id) is None
            assert health_db.get_document(conn, fresh_orphan.id) is not None
            assert health_db.get_document(conn, linked.id) is not None

    def test_detached_document_survives_inside_the_window(self, tmp_path):
        """Detach-then-reattach-elsewhere must not be lossy (D5)."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
                entity_type="encounter", entity_id=eid,
            )
            health_db.unlink_document(conn, doc.id, "encounter", eid)
            conn.commit()

            assert health_documents.sweep_orphan_documents(conn, ctx) == 0
            assert health_db.get_document(conn, doc.id) is not None

    def test_detaching_an_old_document_gives_it_a_fresh_window(self, tmp_path):
        """The bug this column exists for.

        Keying the sweep on `created_at` destroyed any document older than the
        window the instant its last link went — i.e. exactly the
        detach-then-reattach-elsewhere correction the delay is meant to
        protect. A month-old discharge summary detached from encounter 12 on
        the way to encounter 13 was gone before the user got there.
        """
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="discharge.pdf",
                mime="application/pdf", entity_type="encounter", entity_id=eid,
            )
            _backdate(conn, doc.id, hours_ago=24 * 30)
            conn.commit()

            # Detach: the document is now linkless and a month old.
            health_db.unlink_document(conn, doc.id, "encounter", eid)
            conn.commit()

            assert health_documents.sweep_orphan_documents(conn, ctx) == 0
            assert health_db.get_document(conn, doc.id) is not None
            assert health_documents.resolve_document_path(ctx, doc).is_file()

    def test_linking_an_old_document_restarts_its_window(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            _backdate(conn, doc.id, hours_ago=24 * 30)
            health_db.link_document(conn, doc.id, "encounter", eid)
            health_db.unlink_document(conn, doc.id, "encounter", eid)
            conn.commit()
            assert health_documents.sweep_orphan_documents(conn, ctx) == 0

    def test_dedup_hit_restarts_the_window(self, tmp_path):
        """A re-uploaded old scan must survive its own review screen.

        `store_document` returns the existing row on a hash match, keeping its
        original creation date — so an import of a file the user filed last
        week handed `/bulk` a document id that a concurrent sweep would delete
        out from under it, failing the import *and* losing the file.
        """
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="scan.pdf",
                mime="application/pdf",
            )
            _backdate(conn, doc.id, hours_ago=24 * 7)
            conn.commit()

            again, created = health_documents.store_document(
                conn, ctx, raw=PDF, filename="scan.pdf",
                mime="application/pdf", source="import",
            )
            conn.commit()
            assert created is False
            assert again.id == doc.id

            assert health_documents.sweep_orphan_documents(conn, ctx) == 0
            assert health_db.get_document(conn, doc.id) is not None

    def test_custom_window(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            _backdate(conn, doc.id, hours_ago=2)
            conn.commit()
            assert health_documents.sweep_orphan_documents(
                conn, ctx, older_than_hours=1,
            ) == 1

    def test_runs_from_ensure_initialised(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            _backdate(conn, doc.id, hours_ago=48)
            conn.commit()

        ensure_initialised(ctx)

        with health_db.connect(ctx.db_path) as conn:
            assert health_db.get_document(conn, doc.id) is None


class TestDeferredAttachOps:
    """Round-trip: sandboxed CLI defers → scheduler_deferred replays."""

    def _ctx(self, tmp_path):
        """A context shaped like production: the bot workspace sits *inside*
        the user's base dir, so ``inbox/`` is a sibling of it."""
        user_root = tmp_path / "Users" / "alice"
        ctx = synthesize_health_context("alice", user_root / "istota")
        ensure_initialised(ctx)
        return ctx

    def _replay(self, ctx, deferred, ops, *, task_id=99, config=None):
        import json

        from istota import db as core_db
        from istota.scheduler_deferred import _process_deferred_health_ops

        (deferred / f"task_{task_id}_health_ops.json").write_text(
            json.dumps(ops),
        )

        import istota.health as _health

        class _FakeConfig:
            # Mirrors Config.workspace_root(user_id) → {mount}/Users/{uid}.
            @staticmethod
            def workspace_root(user_id=None):
                return ctx.workspace_root.parent

        original = _health.resolve_for_user
        try:
            _health.resolve_for_user = lambda uid, cfg: ctx
            task = core_db.Task(
                id=task_id, status="completed", source_type="cli",
                user_id="alice", prompt="",
            )
            return _process_deferred_health_ops(
                config or _FakeConfig(), task, deferred,
            )
        finally:
            _health.resolve_for_user = original

    def test_attach_op_stores_bytes_and_links(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "card.pdf"
        src.write_bytes(PDF)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        count = self._replay(ctx, deferred, [{
            "op": "attach_document",
            "source_path": str(src),
            "filename": "card.pdf",
            "entity_type": "encounter",
            "entity_id": eid,
            "notes": "From email",
        }])

        assert count == 1
        with health_db.connect(ctx.db_path) as conn:
            docs = health_db.documents_for_entity(conn, "encounter", eid)
        assert len(docs) == 1
        assert docs[0].source == "agent"
        assert docs[0].notes == "From email"
        assert (ctx.uploads_dir / docs[0].stored_path).read_bytes() == PDF

    def test_missing_source_is_skipped_not_fatal(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        survivor = deferred / "ok.pdf"
        survivor.write_bytes(PDF)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        count = self._replay(ctx, deferred, [
            {
                "op": "attach_document",
                "source_path": str(deferred / "gone.pdf"),
                "filename": "gone.pdf",
                "entity_type": "encounter", "entity_id": eid,
            },
            {
                "op": "attach_document",
                "source_path": str(survivor),
                "filename": "ok.pdf",
                "entity_type": "encounter", "entity_id": eid,
            },
        ])

        assert count == 1
        with health_db.connect(ctx.db_path) as conn:
            docs = health_db.documents_for_entity(conn, "encounter", eid)
        assert [d.filename for d in docs] == ["ok.pdf"]

    def test_source_outside_the_workspace_is_rejected(self, tmp_path):
        """The op file is written inside the sandbox, so its path is untrusted."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        outside = tmp_path / "elsewhere" / "secrets.txt"
        outside.parent.mkdir()
        outside.write_bytes(b"not yours")

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        count = self._replay(ctx, deferred, [{
            "op": "attach_document",
            "source_path": str(outside),
            "filename": "secrets.txt",
            "entity_type": "encounter", "entity_id": eid,
        }])

        assert count == 0
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.documents_for_entity(conn, "encounter", eid) == []

    def test_workspace_source_is_allowed(self, tmp_path):
        """An email attachment lands in the user's inbox/, beside the bot dir."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        inbox = ctx.workspace_root.parent / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        src = inbox / "card.jpg"
        src.write_bytes(b"\xff\xd8\xff fake jpeg")

        with health_db.connect(ctx.db_path) as conn:
            iid = health_db.insert_immunization(
                conn, name="Influenza", date_given="2026-01-05",
            )
            conn.commit()

        count = self._replay(ctx, deferred, [{
            "op": "attach_document",
            "source_path": str(src),
            "filename": "card.jpg",
            "entity_type": "immunization", "entity_id": iid,
        }])

        assert count == 1
        with health_db.connect(ctx.db_path) as conn:
            docs = health_db.documents_for_entity(conn, "immunization", iid)
        assert [d.filename for d in docs] == ["card.jpg"]

    def test_encounter_ref_resolves_within_the_batch(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "letter.pdf"
        src.write_bytes(PDF)

        count = self._replay(ctx, deferred, [
            {
                "op": "insert_encounter",
                "encounter_date": "2026-06-29",
                "encounter_type": "visit",
                "ref": "visit",
            },
            {
                "op": "attach_document",
                "source_path": str(src),
                "filename": "letter.pdf",
                "entity_type": "encounter",
                "encounter_ref": "visit",
            },
        ])

        assert count == 2
        with health_db.connect(ctx.db_path) as conn:
            encs = health_db.list_encounters(conn)
            docs = health_db.documents_for_entity(conn, "encounter", encs[0].id)
        assert [d.filename for d in docs] == ["letter.pdf"]

    def test_unresolved_encounter_ref_lands_in_failures(self, tmp_path):
        import json

        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "letter.pdf"
        src.write_bytes(PDF)

        count = self._replay(ctx, deferred, [{
            "op": "attach_document",
            "source_path": str(src),
            "filename": "letter.pdf",
            "entity_type": "encounter",
            "encounter_ref": "nope",
        }])

        assert count == 0
        failures = json.loads(
            (deferred / "task_99_health_op_failures.json").read_text(),
        )
        assert len(failures) == 1
        assert "unresolved encounter_ref" in failures[0]["error"]

    def test_unsupported_type_fails_that_op_only(self, tmp_path):
        import json

        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        bad = deferred / "x.html"
        bad.write_bytes(b"<script>alert(1)</script>")
        good = deferred / "ok.pdf"
        good.write_bytes(PDF)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        count = self._replay(ctx, deferred, [
            {
                "op": "attach_document", "source_path": str(bad),
                "filename": "x.html", "entity_type": "encounter",
                "entity_id": eid,
            },
            {
                "op": "attach_document", "source_path": str(good),
                "filename": "ok.pdf", "entity_type": "encounter",
                "entity_id": eid,
            },
        ])

        assert count == 1
        failures = json.loads(
            (deferred / "task_99_health_op_failures.json").read_text(),
        )
        assert "unsupported document type" in failures[0]["error"]

    def test_detach_op(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
                entity_type="encounter", entity_id=eid,
            )
            conn.commit()

        count = self._replay(ctx, deferred, [{
            "op": "detach_document",
            "document_id": doc.id,
            "entity_type": "encounter",
            "entity_id": eid,
        }])

        assert count == 1
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.documents_for_entity(conn, "encounter", eid) == []
            assert health_db.get_document(conn, doc.id) is not None

    def test_failed_op_does_not_leak_into_the_next_commit(self, tmp_path, monkeypatch):
        """A partially-applied failed op must be rolled back, not swept into
        the next successful op's commit."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "boom.pdf"
        src.write_bytes(PDF)
        ok = deferred / "ok.pdf"
        ok.write_bytes(b"%PDF-1.4 other bytes")

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        from istota import atomic_write

        real_write = atomic_write.write_bytes_atomic

        def flaky_write(path, data, **kwargs):
            # Fail only the first document's byte write, after its row exists.
            if data == PDF:
                raise OSError("disk full")
            return real_write(path, data, **kwargs)

        # Injected at `documents`' own binding rather than at `atomic_write`'s,
        # so this reaches `_write_bytes` and nothing else in the replay.
        monkeypatch.setattr(
            "istota.health.documents.write_bytes_atomic", flaky_write,
        )

        count = self._replay(ctx, deferred, [
            {
                "op": "attach_document", "source_path": str(src),
                "filename": "boom.pdf", "entity_type": "encounter",
                "entity_id": eid,
            },
            {
                "op": "attach_document", "source_path": str(ok),
                "filename": "ok.pdf", "entity_type": "encounter",
                "entity_id": eid,
            },
        ])

        assert count == 1
        with health_db.connect(ctx.db_path) as conn:
            docs = health_db.documents_for_entity(conn, "encounter", eid)
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM documents",
            ).fetchone()["n"]
        # The failed op's row must not survive on the next op's commit.
        assert total == 1
        assert [d.filename for d in docs] == ["ok.pdf"]

    def test_nonexistent_entity_is_a_failure_not_a_ghost_link(self, tmp_path):
        import json

        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "card.pdf"
        src.write_bytes(PDF)

        count = self._replay(ctx, deferred, [{
            "op": "attach_document", "source_path": str(src),
            "filename": "card.pdf", "entity_type": "immunization",
            "entity_id": 99999,
        }])

        assert count == 0
        with health_db.connect(ctx.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM document_links",
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM documents",
            ).fetchone()["n"] == 0
        failures = json.loads(
            (deferred / "task_99_health_op_failures.json").read_text(),
        )
        assert "immunization not found" in failures[0]["error"]

    def test_bad_entity_type_is_rejected_before_any_bytes_land(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "card.pdf"
        src.write_bytes(PDF)

        assert self._replay(ctx, deferred, [{
            "op": "attach_document", "source_path": str(src),
            "filename": "card.pdf", "entity_type": "panel", "entity_id": 1,
        }]) == 0
        assert not (ctx.uploads_dir / "documents").exists()

    def test_operator_cap_applies_to_the_agent_path(self, tmp_path):
        """`max_document_bytes` used to be read only by the web routes, so a
        lowered cap wasn't enforced on a document the agent filed."""
        import json

        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "big.pdf"
        src.write_bytes(b"%PDF-1.4 " + b"x" * 5000)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        class _CappedConfig:
            class health:
                max_document_bytes = 100

            @staticmethod
            def workspace_root(user_id=None):
                return ctx.workspace_root.parent

        assert self._replay(ctx, deferred, [{
            "op": "attach_document", "source_path": str(src),
            "filename": "big.pdf", "entity_type": "encounter",
            "entity_id": eid,
        }], config=_CappedConfig()) == 0
        failures = json.loads(
            (deferred / "task_99_health_op_failures.json").read_text(),
        )
        assert "exceeds 100 bytes" in failures[0]["error"]

    def test_unlimited_cap_applies_to_the_agent_path(self, tmp_path):
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        src = deferred / "huge.pdf"
        src.write_bytes(b"%PDF-1.4 " + b"x" * 200)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        class _UnlimitedConfig:
            class health:
                max_document_bytes = 0

            @staticmethod
            def workspace_root(user_id=None):
                return ctx.workspace_root.parent

        assert self._replay(ctx, deferred, [{
            "op": "attach_document", "source_path": str(src),
            "filename": "huge.pdf", "entity_type": "encounter",
            "entity_id": eid,
        }], config=_UnlimitedConfig()) == 1

    def test_symlink_out_of_the_workspace_is_rejected(self, tmp_path):
        """The guard must resolve, and the read must use what it approved."""
        ctx = self._ctx(tmp_path)
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        secret = tmp_path / "elsewhere" / "secrets.txt"
        secret.parent.mkdir()
        secret.write_bytes(b"not yours")
        link = deferred / "innocent.pdf"
        link.symlink_to(secret)

        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            conn.commit()

        assert self._replay(ctx, deferred, [{
            "op": "attach_document", "source_path": str(link),
            "filename": "innocent.pdf", "entity_type": "encounter",
            "entity_id": eid,
        }]) == 0
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.documents_for_entity(conn, "encounter", eid) == []


class TestStrandedBytes:
    """`store_document` writes bytes before the caller commits, so any
    rollback after it returns strands a blob no DB-driven enumeration can
    reach. The sweep's second pass is what reclaims those."""

    def test_sweep_reclaims_a_directory_with_no_row(self, tmp_path):
        ctx = _ctx(tmp_path)
        stranded = ctx.uploads_dir / "documents" / "42"
        stranded.mkdir(parents=True)
        (stranded / "ghost.pdf").write_bytes(PDF)
        old = time.time() - 48 * 3600
        os.utime(stranded, (old, old))

        with health_db.connect(ctx.db_path) as conn:
            health_documents.sweep_orphan_documents(conn, ctx)
        assert not stranded.exists()

    def test_sweep_leaves_a_fresh_directory_alone(self, tmp_path):
        """A document being written right now legitimately has a directory
        and no committed row yet."""
        ctx = _ctx(tmp_path)
        fresh = ctx.uploads_dir / "documents" / "43"
        fresh.mkdir(parents=True)
        (fresh / "inflight.pdf").write_bytes(PDF)

        with health_db.connect(ctx.db_path) as conn:
            health_documents.sweep_orphan_documents(conn, ctx)
        assert fresh.is_file() is False and fresh.exists()

    def test_sweep_leaves_a_live_document_alone(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-06-29", encounter_type="visit",
            )
            doc, _ = health_documents.attach_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
                entity_type="encounter", entity_id=eid,
            )
            conn.commit()
            path = health_documents.resolve_document_path(ctx, doc)
            old = time.time() - 48 * 3600
            os.utime(path.parent, (old, old))
            health_documents.sweep_orphan_documents(conn, ctx)
        assert path.is_file()

    def test_a_reused_id_does_not_inherit_a_stale_sibling(self, tmp_path):
        """AUTOINCREMENT hands an id back after a rollback, so the next
        document can land in a directory that still holds the old file."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="ghost.pdf",
                mime="application/pdf",
            )
            target_dir = health_documents.resolve_document_path(ctx, doc).parent
            conn.rollback()

            assert (target_dir / "ghost.pdf").is_file()

            again, _ = health_documents.store_document(
                conn, ctx, raw=b"%PDF-1.4 different", filename="real.pdf",
                mime="application/pdf",
            )
            conn.commit()

        assert again.id == doc.id
        contents = sorted(p.name for p in target_dir.iterdir())
        assert contents == ["real.pdf"]


class TestHealMissingBytes:
    def test_reupload_restores_a_document_whose_file_vanished(self, tmp_path):
        """Without healing, a document whose bytes are gone stays broken
        forever: the hash matches, the broken row is returned, nothing is
        written, and /documents/{id}/file 404s while the upload reports OK."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            conn.commit()
            path = health_documents.resolve_document_path(ctx, doc)
            path.unlink()

            again, created = health_documents.store_document(
                conn, ctx, raw=PDF, filename="v.pdf", mime="application/pdf",
            )
            conn.commit()

        assert created is False
        assert again.id == doc.id
        assert path.read_bytes() == PDF


class TestMimeSniffing:
    @pytest.mark.parametrize("raw,expected", [
        (PDF, "application/pdf"),
        (b"\xff\xd8\xff\xe0 jpeg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n rest", "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", "image/heic"),
    ])
    def test_extensionless_upload_is_sniffed(self, tmp_path, raw, expected):
        """An email attachment saved without an extension is a common shape;
        refusing a perfectly good scan for want of a '.pdf' is a poor answer."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=raw, filename="attachment", mime=None,
            )
            conn.commit()
        assert doc.mime == expected

    def test_declared_type_still_wins(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            doc, _ = health_documents.store_document(
                conn, ctx, raw=PDF, filename="x", mime="image/png",
            )
            conn.commit()
        assert doc.mime == "image/png"

    def test_unsniffable_extensionless_upload_is_still_refused(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(health_documents.UnsupportedDocumentType):
                health_documents.store_document(
                    conn, ctx, raw=b"PK\x03\x04 zip bytes", filename="archive",
                    mime=None,
                )
