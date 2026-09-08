from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.diagnostics import r_s06_10_fixtures as fixtures
from tests import test_engine_inspection_case_binding as case_binding
from tests.test_engine_inspection_fixture_revision import _write_portable_source


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class InspectionScopeBoundaryRevisionTests(unittest.TestCase):
    payload_for = case_binding.IndependentlyReviewedCaseTests.payload_for

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        _write_portable_source(self.source)

    def test_v6_changes_only_the_overclaimed_validation_and_preserves_legacy(self):
        legacy, revised = self.root / "v5", self.root / "v6"
        fixtures.build_revision(self.source, legacy)
        fixtures.build_revision(self.source, revised, fixture_version="v6")
        parent = read(legacy / "expectations.json")
        current = read(revised / "expectations.json")
        for field in ("ac_validation_rows", "case_ac_validation_rows", "cases", "constraint_task_rows",
                      "provider_call_order", "derived_cases", "revision_review_expectations",
                      "semantic_evaluation_scope"):
            self.assertEqual(parent[field], current[field], field)
        changed = []
        for previous_path in legacy.glob("*-plan.json"):
            before = read(previous_path)
            after = read(revised / previous_path.name)
            if before == after:
                continue
            changed.append(previous_path.name)
            task = after["definition"]["tasks"][0]
            self.assertEqual("val_task_scope_preservation", task["validations"][2]["validation_id"])
            self.assertNotIn("의존성", task["validations"][2]["statement"])
            self.assertIn("프로젝트 의존성을 추가하거나 변경하지 않는다.",
                          [effect["statement"] for effect in task["prohibited_effects"]])
            restored = deepcopy(after)
            restored["definition"]["tasks"][0]["validations"][2]["statement"] = (
                before["definition"]["tasks"][0]["validations"][2]["statement"]
            )
            restored["definition_digest"] = before["definition_digest"]
            self.assertEqual(before, restored, previous_path.name)
        self.assertEqual(11, len(changed))
        historical = "input-historical-r-s06-09-clean-plan.json"
        self.assertEqual((legacy / historical).read_bytes(), (revised / historical).read_bytes())
        for name in fixtures.SOURCE_INPUT_FILENAMES:
            self.assertEqual((self.source / name).read_bytes(), (revised / f"source-{name}").read_bytes())
        self.assertEqual("plan-inspection-v5-r-s06-15", parent["fixture_revision"])
        self.assertEqual("plan-inspection-v6-scope-boundary", current["fixture_revision"])

    def test_all_v6_case_contracts_and_direct_citations_are_bound_before_calls(self):
        self.fixture_root = self.root / "v6"
        fixtures.build_revision(self.source, self.fixture_root, fixture_version="v6")
        expected = read(self.fixture_root / "expectations.json")
        self.review = read(self.fixture_root / "independent-fixture-review.json")
        for name in self.review["case_reviews"]:
            with self.subTest(case=name):
                fixtures.verify_reviewed_case(name, self.payload_for(name), expected, self.review)
        self.assertEqual(18, len(self.review["case_reviews"]))
        self.assertEqual(13, len(expected["provider_call_order"]))

    def test_unreviewed_v6_and_changed_parent_stop_before_fixture_writes(self):
        original_read = fixtures._read

        def unreviewed(path):
            value = original_read(path)
            if path == fixtures.V6_INDEPENDENT_REVIEW_PATH:
                value["review_complete"] = False
            return value

        destination = self.root / "unreviewed"
        with patch.object(fixtures, "_read", side_effect=unreviewed), self.assertRaisesRegex(
            fixtures.FixtureRevisionError, "독립 fixture review"
        ):
            fixtures.build_revision(self.source, destination, fixture_version="v6")
        self.assertFalse(destination.exists())

        def changed_parent(path):
            value = original_read(path)
            if path == fixtures.V6_INDEPENDENT_REVIEW_PATH:
                value["parent_independent_review_byte_digest"] = "sha256:" + "0" * 64
            return value

        destination = self.root / "changed-parent"
        with patch.object(fixtures, "_read", side_effect=changed_parent), self.assertRaisesRegex(
            fixtures.FixtureRevisionError, "v5 부모 fixture"
        ):
            fixtures.build_revision(self.source, destination, fixture_version="v6")
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
