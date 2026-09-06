"""One module per notification source: its keys, its producer helpers, its view.

A source owns three things that have to agree exactly, and keeping them in one
file is what makes that checkable:

- the ``source`` id and the ``dedup_key`` spelling, which the producer, the
  close path and the backfill all generate independently;
- ``write`` / ``resolve_for_*``, so no producer hand-rolls either string;
- the resolver, which turns a stored row into what the panel renders.

What is *not* per-source is in :mod:`._common`: the ``{prefix}:{id}`` spelling,
the argument set handed to ``write_notification``, the ``resolve_by_object``
call and the integer coercion of ``object_id``. Those were identical in every
file bar a noun, and three of the six documented that by pointing at a fourth.

Nothing is imported here. :func:`istota.notification_sources._register_all` does
the importing, explicitly and once per process, so the registry never depends on
which surface happened to import what first. A producer imports its own source
module directly — these are cheap by construction (no imports at module scope
beyond the standard library) precisely so a daemon hot path can.
"""
