"""The four health row serialisers the skill and the web routes each had a copy of.

``skills/health/__init__.py`` and ``health/routes.py`` both turn an encounter,
a diagnosis, an immunization and an immunization-coverage row into a JSON
dict. Two of the four pairs were byte-identical; the other two differed by a
single trailing ``created_at`` key that the route copy emits and the skill copy
does not.

``include_created_at`` is that difference made a parameter rather than
resolved. Each caller passes what it passes today, so the extraction is inert
— the route responses and the skill envelopes carry exactly the keys, in
exactly the order, that they carried before. The flag is on
``encounter_to_dict`` and ``diagnosis_to_dict`` only, because those are the
only two the two copies disagreed about: ``immunization_to_dict`` emits
``created_at`` on both sides and ``coverage_to_dict`` on neither, and giving
either a flag would invent an option nothing asked for.

**Key order is part of the contract**, which is why ``created_at`` is appended
after the literal rather than being a conditional entry inside it. A wire
format read by a model and by a browser is compared by tests as a whole dict;
``tests/test_health_serialize.py`` pins both the key set and its order against
output captured from the two pre-consolidation copies.

What is deliberately **not** here: ``history_summary`` and the dashboard
payload, which the spec's stage line names alongside these four. They are not
a duplicated pair — the route builds its history packet from a five-year
procedure window and an immunization coverage block that the skill's version
has neither of, and the route dashboard reads panel counts, settings and
diagnosis totals the skill's ``summary`` never touches. Folding them would be
a behaviour change, and this stage's contract is that there is none.

Stdlib only — ``types``, for the coverage adapter at the bottom — and nothing
from the package: a row is anything carrying the attributes, which is what
lets a skill subprocess and a FastAPI route share one body. It sits inside
``health/`` rather than beside the top-level leaves because both callers are
health's own. Importing it costs ``istota.health.__init__`` — the module
package's ``_loader``, ``_migrate``, ``db`` and ``models`` — which is why the
skill imports it *inside* its four wrappers, as it does every other
``istota.health`` import: at module scope it would load all of that for
``--help`` and for an argparse error too, the two paths that skip it. The
routes pay it either way.
"""

from __future__ import annotations

from types import SimpleNamespace


def encounter_to_dict(e, *, include_created_at: bool = False) -> dict:
    """Serialise an encounter row. ``created_at`` for the web routes only."""
    out = {
        "id": e.id,
        "encounter_date": e.encounter_date,
        "encounter_type": e.encounter_type,
        "provider": e.provider,
        "facility": e.facility,
        "specialty": e.specialty,
        "reason": e.reason,
        "notes": e.notes,
    }
    if include_created_at:
        out["created_at"] = e.created_at
    return out


def diagnosis_to_dict(
    d,
    encounter_ids: list[int] | None = None,
    *,
    include_created_at: bool = False,
) -> dict:
    """Serialise a diagnosis row. ``created_at`` for the web routes only."""
    out = {
        "id": d.id,
        "name": d.name,
        "icd10": d.icd10,
        "status": d.status,
        "date_diagnosed": d.date_diagnosed,
        "date_resolved": d.date_resolved,
        # Deprecated: a condition is routinely seen at several encounters, so
        # `encounter_ids` is the real answer and this only ever held the first.
        # Kept because an older client (and the legacy column it mirrors) still
        # reads a single id.
        "encounter_id": d.encounter_id,
        "encounter_ids": encounter_ids if encounter_ids is not None else [],
        "severity": d.severity,
        "notes": d.notes,
    }
    if include_created_at:
        out["created_at"] = d.created_at
    return out


def immunization_to_dict(i) -> dict:
    """Serialise an immunization row. Both callers emit ``created_at``."""
    return {
        "id": i.id,
        "name": i.name,
        "product_name": i.product_name,
        "date_given": i.date_given,
        "manufacturer": i.manufacturer,
        "dose_label": i.dose_label,
        "lot_number": i.lot_number,
        "route": i.route,
        "site": i.site,
        "administered_by": i.administered_by,
        "facility": i.facility,
        "encounter_id": i.encounter_id,
        "cvx_code": i.cvx_code,
        "notes": i.notes,
        "source": i.source,
        "created_at": i.created_at,
    }


def coverage_to_dict(c) -> dict:
    """Serialise a computed immunization-coverage row. Neither caller emits ``created_at``."""
    return {
        "name": c.name,
        "display_name": c.display_name,
        "category": c.category,
        "status": c.status,
        "last_given": c.last_given,
        "dose_count": c.dose_count,
        "next_due": c.next_due,
        "is_overdue": c.is_overdue,
        "days_until_due": c.days_until_due,
    }


def unmatched_coverage_to_dict(name: str, rows) -> dict:
    """The immunization coverage view's "Other" bucket: a row for a vaccine
    name that matches no entry in ``immunization_refs``.

    A hand-built dict in ``routes.api_immunization_coverage`` carrying the same
    nine keys in the same order as :func:`coverage_to_dict`, which is a third
    copy of the shape rather than a different one — it lands in the same array
    the browser reads, and a key added to one and not the other is a row the
    view renders wrong. Built through ``coverage_to_dict`` so there is one
    place the key set is written down.
    """
    return coverage_to_dict(SimpleNamespace(
        name=name,
        display_name=name,
        category="other",
        status="recorded",
        last_given=max((r.date_given for r in rows), default=None),
        dose_count=len(rows),
        next_due=None,
        is_overdue=False,
        days_until_due=None,
    ))
