from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.domain import PlanContractRevision
from flowmarshal.engine.plan_inspection_eval import assess_inspection_review
from flowmarshal.engine.planner_roles import PlanReviewEnvelope
from scripts.diagnostics.r_s06_10_fixtures import (
    FixtureRevisionError, SOURCE_INPUT_FILENAMES, build_revision, verify_integration_criterion_coverage,
)


ROOT = Path(__file__).resolve().parent / "fixtures" / "engine"
LEGACY = json.loads((ROOT / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
SOURCE_INPUTS = json.loads((ROOT / "plan-inspection-v2-source-inputs.json").read_text(encoding="utf-8"))
EXPECTATIONS = json.loads((ROOT / "plan-inspection-v5-expectations.json").read_text(encoding="utf-8"))


def _write_portable_source(source: Path) -> None:
    files = deepcopy(LEGACY) | deepcopy(SOURCE_INPUTS)
    semantic = deepcopy(files["input-semantic-explicit-plan.json"])
    next(row for row in semantic["definition"]["goal_coverage"] if row["criterion_id"] == "ac_001")[
        "validation_ids"
    ].remove("val_task_validator_review")
    from flowmarshal.canonical import sha256_digest
    semantic["definition_digest"] = sha256_digest(semantic["definition"])
    files["input-semantic-missing-link-plan.json"] = semantic
    files["input-stored-multi-defect-plan.json"] = deepcopy(files["expanded-plan.json"])
    for filename in SOURCE_INPUT_FILENAMES:
        source.joinpath(filename).write_text(
            json.dumps(files[filename], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )


def _plan(destination: Path, filename: str) -> dict:
    return json.loads((destination / filename).read_text(encoding="utf-8"))


def _coverage(plan: dict, criterion_id: str) -> set[str]:
    return set(next(row for row in plan["definition"]["goal_coverage"]
                    if row["criterion_id"] == criterion_id)["validation_ids"])


def _fixed_envelope(expected: list[dict]) -> PlanReviewEnvelope:
    """고정 oracle 결속만 위한 scripted 제출물이며 의미 탐지의 증명이 아니다."""
    citations = []
    findings = []
    links = []
    for index, defect in enumerate(expected):
        basis_refs = []
        for selector in defect["required_citations"]:
            citation_id = f"C{len(citations)}"
            citations.append({"citation_id": citation_id, "source_ref": selector["source_ref"],
                              "selector": selector["selectors"][0], "quote": "고정 근거"})
            basis_refs.append(citation_id)
        finding_code = f"FIXED_{index}"
        task_refs = defect["allowed_task_ref_sets"][0]
        links.append({"finding_code": finding_code, "defect_kind": defect["defect_kind"],
                      "criterion_ids": defect["criterion_ids"], "validation_ids": defect["validation_ids"],
                      "task_refs": task_refs, "basis_refs": basis_refs})
        findings.append({"finding_code": finding_code, "gate": "verification", "severity": "error",
                         "summary": " ".join((*defect["criterion_ids"], *defect["validation_ids"], defect["defect_id"])),
                         "evidence_refs": defect["required_evidence_refs"], "affected_task_refs": task_refs,
                         "remediable": True})
    if not citations:
        citations.append({"citation_id": "C0", "source_ref": "source:goal", "selector": "/hard_acceptance/0/statement",
                          "quote": "정상 고정 oracle"})
    review = {"findings": findings, "ratings": None if findings else
              {"goal_fit": 4, "grounding": 4, "engineering": 4, "verification": 4, "execution_safety": 4}}
    return PlanReviewEnvelope.model_validate({"review": review, "inspection": {
        "citations": citations, "ac_validation_rows": [], "constraint_task_rows": [], "validation_rows": [],
        "validation_scope_rows": [],
        "finding_links": links,
    }})


class InspectionFixtureRevisionTests(unittest.TestCase):
    def build(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "r-s06-09-source"
        source.mkdir()
        _write_portable_source(source)
        destination = Path(temporary.name) / "r-s06-10-revision"
        build_revision(source, destination)
        return source, destination

    def test_preserves_thirteen_provider_cases_and_writes_a_new_locked_revision(self):
        source, destination = self.build()
        assessment = _plan(destination, "fixture-assessment.json")
        self.assertEqual(13, len(EXPECTATIONS["provider_call_order"]))
        self.assertEqual(13, len(set(EXPECTATIONS["provider_call_order"])))
        self.assertEqual(EXPECTATIONS["provider_call_order"], assessment["provider_call_order"])
        self.assertTrue((destination / "expectations.json").is_file())
        independent_review = _plan(destination, "independent-fixture-review.json")
        self.assertTrue(independent_review["review_complete"])
        self.assertEqual(EXPECTATIONS["provider_call_order"][:-2], independent_review["reviewed_cases"])
        self.assertEqual(
            independent_review["expectations_digest"],
            "sha256:" + hashlib.sha256((destination / "expectations.json").read_bytes()).hexdigest(),
        )
        runtime_expectations = _plan(destination, "expectations.json")
        self.assertEqual([], runtime_expectations["clean"])
        self.assertEqual(["ac003-task-oracle-link"],
                         [item["defect_id"] for item in runtime_expectations["missing-link"]])
        for filename in SOURCE_INPUT_FILENAMES:
            self.assertEqual((source / filename).read_bytes(), (destination / f"source-{filename}").read_bytes())
            self.assertTrue((destination / filename).is_file())
        with self.assertRaises(FileExistsError):
            build_revision(source, destination)

    def test_source_input_meaning_is_locked_before_any_fixture_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "r-s06-09-source"
            source.mkdir()
            _write_portable_source(source)
            altered = _plan(source, "input-clean-plan.json")
            altered["definition"]["tasks"][0]["objective"] += " 변경"
            source.joinpath("input-clean-plan.json").write_text(
                json.dumps(altered, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            destination = Path(temporary) / "r-s06-10-revision"
            with self.assertRaisesRegex(FixtureRevisionError, "input-clean-plan.json"):
                build_revision(source, destination)
            self.assertFalse(destination.exists())

    def test_normalization_fixes_only_historical_f001_and_f002_links(self):
        _, destination = self.build()
        historical = _plan(destination, "input-historical-r-s06-09-clean-plan.json")
        self.assertNotIn("val_goal_independent_unittest", _coverage(historical, "ac_001"))
        self.assertNotIn("val_goal_independent_behavior_contract", _coverage(historical, "ac_003"))
        for filename in ("input-clean-plan.json", "input-combined-plan.json", "input-semantic-explicit-plan.json",
                         "input-bad-plan.json", "input-wrong-goal-plan.json", "input-semantic-missing-link-plan.json"):
            with self.subTest(filename=filename):
                plan = _plan(destination, filename)
                self.assertIn("val_goal_independent_unittest", _coverage(plan, "ac_001"))
                self.assertIn("val_goal_independent_behavior_contract", _coverage(plan, "ac_003"))
                PlanContractRevision.model_validate(plan)
                verify_integration_criterion_coverage(plan)

    def test_integration_criterion_refs_and_goal_coverage_are_bidirectionally_locked(self):
        _, destination = self.build()
        plan = _plan(destination, "input-clean-plan.json")
        verify_integration_criterion_coverage(plan)
        for mutation in ("criterion_refs_only", "goal_coverage_only"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(plan)
                if mutation == "criterion_refs_only":
                    changed["definition"]["integration_validations"][0]["criterion_refs"].remove("ac_003")
                else:
                    _coverage(changed, "ac_003").discard("val_goal_independent_behavior_contract")
                    row = next(item for item in changed["definition"]["goal_coverage"]
                               if item["criterion_id"] == "ac_003")
                    row["validation_ids"].remove("val_goal_independent_behavior_contract")
                with self.assertRaisesRegex(FixtureRevisionError, "INTEGRATION_CRITERION_COVERAGE_MISMATCH"):
                    verify_integration_criterion_coverage(changed)

    def test_revised_cases_keep_one_intended_defect_or_the_recorded_normal_semantics(self):
        _, destination = self.build()
        cases = EXPECTATIONS["cases"]
        for case_id in ("clean", "combined", "semantic-explicit", "evidence-simple-mention"):
            self.assertEqual([], cases[case_id]["expected_defects"])
        semantic = _plan(destination, "input-semantic-explicit-plan.json")
        self.assertIn("val_task_validator_review", _coverage(semantic, "ac_001"))
        removed = _plan(destination, "input-v2-semantic-explicit-removal-plan.json")
        self.assertNotIn("val_task_validator_review", _coverage(removed, "ac_001"))
        self.assertEqual(["ac001-explicit-semantic-link"],
                         cases["semantic-explicit-removal"]["expected_defects"])
        f001 = _plan(destination, "input-v2-missing-ac001-goal-unittest-link-plan.json")
        self.assertNotIn("val_goal_independent_unittest", _coverage(f001, "ac_001"))
        self.assertIn("val_goal_independent_behavior_contract", _coverage(f001, "ac_003"))
        f002 = _plan(destination, "input-v2-missing-ac003-goal-behavior-link-plan.json")
        self.assertIn("val_goal_independent_unittest", _coverage(f002, "ac_001"))
        self.assertNotIn("val_goal_independent_behavior_contract", _coverage(f002, "ac_003"))
        partial = _plan(destination, "input-v2-partial-task-inspection-missing-plan.json")
        self.assertNotIn("val_task_validator_review", [item["validation_id"]
                         for item in partial["definition"]["tasks"][0]["validations"]])
        explicit = _plan(destination, "input-v2-evidence-explicit-exclusion-plan.json")
        statement = next(item["statement"] for item in explicit["definition"]["tasks"][0]["validations"]
                         if item["validation_id"] == "val_task_validator_review")
        self.assertEqual(EXPECTATIONS["evidence_semantics"]["explicit_exclusion_statement"], statement)

    def test_full_fixed_matrix_and_constraint_semantics_are_explicit(self):
        rows = EXPECTATIONS["ac_validation_rows"]
        self.assertEqual(28, len(rows))
        self.assertEqual(28, len({(row["criterion_id"], row["validation_id"]) for row in rows}))
        self.assertEqual(["constraint_001", "constraint_002", "constraint_003"],
                         [row["constraint_id"] for row in EXPECTATIONS["constraint_task_rows"]])
        first, second, third = EXPECTATIONS["constraint_task_rows"]
        self.assertEqual(("not_applicable", []), (first["applicability"], first["required_validation_ids"]))
        self.assertEqual(("not_applicable", []), (second["applicability"], second["required_validation_ids"]))
        self.assertEqual(["existing_unittest", "scope_preservation", "independent_validator_review"],
                         third["required_responsibilities"])
        self.assertEqual(["val_task_validator_review"],
                         third["satisfying_validation_ids"]["independent_validator_review"])
        semantic = next(row for row in rows if (row["criterion_id"], row["validation_id"]) ==
                        ("ac_001", "val_task_validator_review"))
        self.assertFalse(semantic["ac_link_required"])
        task_scope = next(row for row in rows if (row["criterion_id"], row["validation_id"]) ==
                          ("ac_004", "val_task_scope_preservation"))
        self.assertFalse(task_scope["ac_link_required"])

    def test_ac004_composite_phases_require_each_oracle_without_infecting_siblings(self):
        rows = {
            row["validation_id"]: row["ac_link_required"]
            for row in EXPECTATIONS["case_ac_validation_rows"]["clean"]
            if row["criterion_id"] == "ac_004"
        }
        self.assertTrue(rows["val_task_add_behavior_contract"])
        self.assertTrue(rows["val_goal_independent_behavior_contract"])
        self.assertFalse(rows["val_task_unittest"])
        self.assertFalse(rows["val_task_scope_preservation"])

    def test_bad_axis_conflation_expectation_keeps_scope_defect_and_three_true_rows(self):
        defects = json.loads((ROOT / "plan-inspection-expectations.json").read_text(encoding="utf-8"))["bad"]
        self.assertEqual(["task-phase-overclaim"], [row["defect_id"] for row in defects])
        self.assertEqual(["val_task_add_behavior_contract"], defects[0]["validation_ids"])
        rows = {
            (row["criterion_id"], row["validation_id"]): row["ac_link_required"]
            for row in EXPECTATIONS["case_ac_validation_rows"]["bad"]
        }
        for pair in (
            ("ac_003", "val_task_add_behavior_contract"),
            ("ac_003", "val_goal_independent_behavior_contract"),
            ("ac_004", "val_task_add_behavior_contract"),
        ):
            self.assertTrue(rows[pair], pair)
        for pair in (
            ("ac_003", "val_task_scope_preservation"),
            ("ac_004", "val_task_scope_preservation"),
            ("ac_004", "val_task_unittest"),
        ):
            self.assertFalse(rows[pair], pair)

    def test_r_s06_13_provenance_binds_the_raw_rejection_and_normal_fixture_selectors(self):
        provenance = EXPECTATIONS["r_s06_13_provenance"]
        raw_manifest = json.loads((ROOT / provenance["raw_rejection_fixture"]).read_text(encoding="utf-8"))
        normal = json.loads((ROOT / provenance["normal_fixture"]).read_text(encoding="utf-8"))
        self.assertEqual("FAIL", raw_manifest["expected_status"])
        self.assertEqual(provenance["raw_summary_byte_digest"], raw_manifest["files"]["summary.json"])
        self.assertEqual("/expected_ac_validation_rows", normal["provenance"]["relation_rows_selector"])
        self.assertEqual(EXPECTATIONS["parent_fixture_revision"], normal["provenance"]["expectation_revision"])
        self.assertEqual(EXPECTATIONS["parent_expectations_byte_digest"],
                         sha256_bytes((ROOT / "plan-inspection-v4-expectations.json").read_bytes()))

    def test_fixed_review_expectations_require_each_independent_defect(self):
        fixed = EXPECTATIONS["revision_review_expectations"]
        historical = _fixed_envelope(fixed["historical-r-s06-09-clean"])
        self.assertTrue(assess_inspection_review(historical, fixed["historical-r-s06-09-clean"])["passed"])
        incomplete = historical.model_dump(mode="json")
        incomplete["review"]["findings"].pop()
        incomplete["inspection"]["finding_links"].pop()
        report = assess_inspection_review(PlanReviewEnvelope.model_validate(incomplete),
                                          fixed["historical-r-s06-09-clean"])
        self.assertFalse(report["passed"])
        self.assertEqual(["ac003-goal-behavior-link"], report["missing_defects"])
        for case_id in ("semantic-explicit-removal", "missing-ac001-goal-unittest-link",
                        "missing-ac003-goal-behavior-link", "partial-task-inspection-missing",
                        "evidence-explicit-exclusion"):
            with self.subTest(case_id=case_id):
                self.assertTrue(assess_inspection_review(_fixed_envelope(fixed[case_id]), fixed[case_id])["passed"])
        for case_id in ("clean", "combined", "semantic-explicit", "evidence-simple-mention"):
            with self.subTest(case_id=case_id):
                self.assertTrue(assess_inspection_review(_fixed_envelope([]), EXPECTATIONS["cases"][case_id]["expected_defects"])
                                ["passed"])


if __name__ == "__main__":
    unittest.main()
