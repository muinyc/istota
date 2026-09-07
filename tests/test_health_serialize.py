"""The health serialisers: the wire format is pinned, not merely shared.

``skills/health/__init__.py`` and ``health/routes.py`` each had a copy of four
row serialisers. The obvious test for a consolidation like this — "the two
callers produce the same dict" — is true by construction the moment both call
one function, and would pass just as happily against a survivor that had
dropped or gained a key. So the expected dicts below are **captured from the
two pre-consolidation copies** at ``70b94a8a`` and written down as literals:
every key, every value, and the order, for both sides of the
``include_created_at`` split. A consolidation that changed the wire format
fails here rather than reaching a browser or a model.

The drift guard at the bottom is the other half — it fails if a fifth copy of
any of the four key sets appears anywhere under ``src/istota/``.
"""

import ast
import pathlib
from types import SimpleNamespace

import pytest

from istota.health import routes as health_routes
from istota.health import serialize
from istota.skills import health as health_skill

ENCOUNTER = SimpleNamespace(
    id=7,
    encounter_date="2026-03-04",
    encounter_type="office_visit",
    provider="Dr Fixture",
    facility="Fixture Clinic",
    specialty="cardiology",
    reason="annual review",
    notes="fixture notes",
    created_at="2026-03-04T09:00:00+00:00",
)

DIAGNOSIS = SimpleNamespace(
    id=11,
    name="Fixture condition",
    icd10="I10",
    status="active",
    date_diagnosed="2025-01-02",
    date_resolved=None,
    encounter_id=7,
    severity="mild",
    notes="diag notes",
    created_at="2025-01-02T10:00:00+00:00",
)

IMMUNIZATION = SimpleNamespace(
    id=3,
    name="influenza",
    product_name="Fluvax",
    date_given="2026-01-15",
    manufacturer="FixtureCo",
    dose_label="1 of 1",
    lot_number="LOT-1",
    route="IM",
    site="left deltoid",
    administered_by="Nurse Fixture",
    facility="Fixture Clinic",
    encounter_id=7,
    cvx_code="150",
    notes="imm notes",
    source="manual",
    created_at="2026-01-15T11:00:00+00:00",
)

COVERAGE = SimpleNamespace(
    name="influenza",
    display_name="Influenza",
    category="routine",
    status="up_to_date",
    last_given="2026-01-15",
    dose_count=1,
    next_due="2027-01-15",
    is_overdue=False,
    days_until_due=300,
)

# --- captured from the two copies before they were folded together ----------

SKILL_ENCOUNTER = {
    "id": 7,
    "encounter_date": "2026-03-04",
    "encounter_type": "office_visit",
    "provider": "Dr Fixture",
    "facility": "Fixture Clinic",
    "specialty": "cardiology",
    "reason": "annual review",
    "notes": "fixture notes",
}
ROUTE_ENCOUNTER = {**SKILL_ENCOUNTER, "created_at": "2026-03-04T09:00:00+00:00"}

SKILL_DIAGNOSIS = {
    "id": 11,
    "name": "Fixture condition",
    "icd10": "I10",
    "status": "active",
    "date_diagnosed": "2025-01-02",
    "date_resolved": None,
    "encounter_id": 7,
    "encounter_ids": [],
    "severity": "mild",
    "notes": "diag notes",
}
ROUTE_DIAGNOSIS = {**SKILL_DIAGNOSIS, "created_at": "2025-01-02T10:00:00+00:00"}

IMMUNIZATION_DICT = {
    "id": 3,
    "name": "influenza",
    "product_name": "Fluvax",
    "date_given": "2026-01-15",
    "manufacturer": "FixtureCo",
    "dose_label": "1 of 1",
    "lot_number": "LOT-1",
    "route": "IM",
    "site": "left deltoid",
    "administered_by": "Nurse Fixture",
    "facility": "Fixture Clinic",
    "encounter_id": 7,
    "cvx_code": "150",
    "notes": "imm notes",
    "source": "manual",
    "created_at": "2026-01-15T11:00:00+00:00",
}

COVERAGE_DICT = {
    "name": "influenza",
    "display_name": "Influenza",
    "category": "routine",
    "status": "up_to_date",
    "last_given": "2026-01-15",
    "dose_count": 1,
    "next_due": "2027-01-15",
    "is_overdue": False,
    "days_until_due": 300,
}


def _assert_exact(got: dict, expected: dict) -> None:
    """Equality *and* key order — the wire format is an ordered document."""
    assert got == expected
    assert list(got) == list(expected)


class TestTheCapturedWireFormat:
    """The shared body still answers what each copy answered, key for key."""

    def test_encounter_without_created_at(self):
        _assert_exact(serialize.encounter_to_dict(ENCOUNTER), SKILL_ENCOUNTER)

    def test_encounter_with_created_at(self):
        _assert_exact(
            serialize.encounter_to_dict(ENCOUNTER, include_created_at=True),
            ROUTE_ENCOUNTER,
        )

    def test_diagnosis_without_created_at(self):
        _assert_exact(serialize.diagnosis_to_dict(DIAGNOSIS), SKILL_DIAGNOSIS)

    def test_diagnosis_with_created_at(self):
        _assert_exact(
            serialize.diagnosis_to_dict(DIAGNOSIS, include_created_at=True),
            ROUTE_DIAGNOSIS,
        )

    def test_diagnosis_encounter_ids_pass_through(self):
        got = serialize.diagnosis_to_dict(DIAGNOSIS, [7, 9])
        assert got["encounter_ids"] == [7, 9]
        # The deprecated scalar is still emitted beside the list.
        assert got["encounter_id"] == 7

    def test_immunization(self):
        _assert_exact(serialize.immunization_to_dict(IMMUNIZATION), IMMUNIZATION_DICT)

    def test_coverage(self):
        _assert_exact(serialize.coverage_to_dict(COVERAGE), COVERAGE_DICT)

    def test_the_unmatched_other_bucket_keeps_its_own_captured_shape(self):
        """`routes.api_immunization_coverage` built this by hand, in the same
        array the browser reads coverage rows from. Captured before the fold."""
        rows = [
            SimpleNamespace(date_given="2024-05-01"),
            SimpleNamespace(date_given="2026-02-02"),
        ]
        _assert_exact(
            serialize.unmatched_coverage_to_dict("Typhoid oral", rows),
            {
                "name": "Typhoid oral",
                "display_name": "Typhoid oral",
                "category": "other",
                "status": "recorded",
                "last_given": "2026-02-02",
                "dose_count": 2,
                "next_due": None,
                "is_overdue": False,
                "days_until_due": None,
            },
        )

    def test_the_unmatched_bucket_on_an_empty_group(self):
        _assert_exact(
            serialize.unmatched_coverage_to_dict("Typhoid oral", []),
            {
                "name": "Typhoid oral",
                "display_name": "Typhoid oral",
                "category": "other",
                "status": "recorded",
                "last_given": None,
                "dose_count": 0,
                "next_due": None,
                "is_overdue": False,
                "days_until_due": None,
            },
        )


class TestEachCallerKeepsItsOwnKeySet:
    """The route emits ``created_at`` and the skill does not — the flag is not
    a default anyone may quietly flip. Asserting the two are *equal* would be
    vacuous; these assert what each one actually is."""

    def test_route_encounter_carries_created_at(self):
        _assert_exact(health_routes._encounter_to_dict(ENCOUNTER), ROUTE_ENCOUNTER)

    def test_skill_encounter_does_not(self):
        _assert_exact(health_skill._encounter_to_dict(ENCOUNTER), SKILL_ENCOUNTER)
        assert "created_at" not in health_skill._encounter_to_dict(ENCOUNTER)

    def test_route_diagnosis_carries_created_at(self):
        _assert_exact(health_routes._diagnosis_to_dict(DIAGNOSIS), ROUTE_DIAGNOSIS)

    def test_skill_diagnosis_does_not(self):
        _assert_exact(health_skill._diagnosis_to_dict(DIAGNOSIS), SKILL_DIAGNOSIS)
        assert "created_at" not in health_skill._diagnosis_to_dict(DIAGNOSIS)

    def test_route_diagnosis_keeps_the_encounter_ids_argument_positional(self):
        """`routes` calls it as `_diagnosis_to_dict(d, links)`. A conversion
        that made `encounter_ids` keyword-only would raise, not degrade."""
        got = health_routes._diagnosis_to_dict(DIAGNOSIS, [7, 9])
        assert got["encounter_ids"] == [7, 9]
        assert got["created_at"] == "2025-01-02T10:00:00+00:00"

    @pytest.mark.parametrize("mod", [health_routes, health_skill])
    def test_immunization_and_coverage_agree_on_both_sides(self, mod):
        _assert_exact(mod._immunization_to_dict(IMMUNIZATION), IMMUNIZATION_DICT)
        _assert_exact(mod._coverage_to_dict(COVERAGE), COVERAGE_DICT)


class TestNoFifthCopy:
    """A dict literal with one of the four key sets, anywhere under ``src/``,
    is a serialiser that has grown back. Matching on the key set rather than on
    a function name is what makes the guard survive a rename."""

    KEY_SETS = {
        "encounter": tuple(SKILL_ENCOUNTER),
        "encounter+created_at": tuple(ROUTE_ENCOUNTER),
        "diagnosis": tuple(SKILL_DIAGNOSIS),
        "diagnosis+created_at": tuple(ROUTE_DIAGNOSIS),
        "immunization": tuple(IMMUNIZATION_DICT),
        "coverage": tuple(COVERAGE_DICT),
    }

    def _dict_key_sets(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "istota"
        wanted = set(self.KEY_SETS.values())
        found: list[tuple[str, tuple[str, ...]]] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                keys = tuple(
                    k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                )
                if len(keys) != len(node.keys):
                    continue
                if keys in wanted:
                    found.append((str(path.relative_to(root)), keys))
        return found

    def test_only_serialize_py_builds_these_dicts(self):
        offenders = sorted(
            {f for f, _ in self._dict_key_sets()} - {"health/serialize.py"}
        )
        assert offenders == [], (
            "a health row serialiser's key set is built outside "
            f"health/serialize.py: {offenders}"
        )

    def test_the_guard_is_actually_finding_them(self):
        """Without this, deleting `serialize.py` would leave the guard green.

        The walk must see all four bodies — encounter and diagnosis in their
        no-``created_at`` form, since ``created_at`` is appended after the
        literal rather than being part of it.
        """
        found = {keys for f, keys in self._dict_key_sets() if f == "health/serialize.py"}
        assert found == {
            self.KEY_SETS["encounter"],
            self.KEY_SETS["diagnosis"],
            self.KEY_SETS["immunization"],
            self.KEY_SETS["coverage"],
        }
