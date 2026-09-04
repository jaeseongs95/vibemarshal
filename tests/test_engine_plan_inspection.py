from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from flowmarshal.engine.domain import GoalContractRevision, PlanContractRevision, ProjectMapRevision, StateSnapshot
from flowmarshal.engine.plan_inspection import PlanInspection, PlanInspectionError, validate_plan_inspection
from flowmarshal.engine.plan_inspection_eval import assess_inspection_review
from flowmarshal.engine.planner_roles import PlanExpansionEnvelope, PlanReviewEnvelope
from flowmarshal.engine.planning import plan_review_evidence_catalog, validation_comparison_targets
from tests.engine_inspection_helpers import inspection_fixture

ROOT = Path(__file__).resolve().parent / "fixtures" / "engine"
FIXTURE = json.loads((ROOT / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
EXPECTED = json.loads((ROOT / "plan-inspection-expectations.json").read_text(encoding="utf-8"))
RATINGS = dict.fromkeys(("goal_fit", "grounding", "engineering", "verification", "execution_safety"), 4)


def inputs(name):
    semantic = name in {"semantic-explicit", "semantic-missing-link"}
    stem = "input-semantic-explicit-" if semantic else "input-"
    goal = GoalContractRevision.model_validate(FIXTURE[stem + "goal.json"])
    state = StateSnapshot.model_validate(FIXTURE[stem + "state.json"])
    filename = "expanded-plan.json" if name == "stored-multi-defect" else f"input-{'semantic-explicit' if semantic else name}-plan.json"
    raw = deepcopy(FIXTURE[filename])
    if name == "semantic-missing-link":
        raw["definition"]["goal_coverage"][0]["validation_ids"].remove("val_task_validator_review")
        from flowmarshal.canonical import sha256_digest
        raw["definition_digest"] = sha256_digest(raw["definition"])
    plan = PlanContractRevision.model_validate(raw)
    raw_map = deepcopy(FIXTURE["input-project-map.json"])
    reference = next(entry for entry in raw_map["entries"] if entry["kind"] == "reference")
    reference["path"] = str(ROOT / "plan-inspection-reference.md")
    project_map = ProjectMapRevision.model_validate(raw_map)
    return plan, goal, state, project_map


def submission(name):
    """고정 결함의 명시적 scripted 제출물이다. 모델 의미 탐지를 흉내 내지 않는다."""
    plan, goal, _, project_map = inputs(name)
    inspection = inspection_fixture(plan.model_dump(mode="json"), goal.definition.model_dump(mode="json"), revision=True)
    findings = []

    def cite(source, selector, text):
        for item in inspection["citations"]:
            if (item["source_ref"], item["selector"], item["quote"]) == (source, selector, text[:240]):
                return item["citation_id"]
        ref = f"c{len(inspection['citations'])}"
        inspection["citations"].append({"citation_id": ref, "source_ref": source, "selector": selector, "quote": text[:240]})
        return ref

    for index, defect in enumerate(EXPECTED[name]):
        code = f"FIXTURE_DEFECT_{index}"
        vid = defect["validation_ids"][0]
        vrow = next(row for row in inspection["validation_rows"] if row["validation_id"] == vid)
        basis = [vrow["claim_ref"]]
        if defect["defect_kind"] == "missing_validation_link":
            ac = defect["criterion_ids"][0]
            row = next(row for row in inspection["ac_validation_rows"] if (row["criterion_id"], row["validation_id"]) == (ac, vid))
            row.update(relation="explicit_procedure", finding_codes=[code])
            basis = list(row["basis_refs"])
            ci = next(i for i, coverage in enumerate(plan.definition.goal_coverage) if coverage.criterion_id == ac)
            basis.append(cite("artifact:plan_contract", f"/definition/goal_coverage/{ci}/validation_ids/0",
                              plan.definition.goal_coverage[ci].validation_ids[0]))
        elif defect["defect_kind"] == "validation_scope":
            entry = next(entry for entry in project_map.entries if entry.kind.value == "reference")
            content = Path(entry.path).read_text(encoding="utf-8")
            start = content.index("기존 도구를")
            scope_ref = cite(f"project:{entry.entry_id}", "/content", content[start:start + 240])
            basis.append(scope_ref)
            vrow.update(assessment="contradicted", finding_codes=[code],
                        mechanisms=[{"tool": "oracle.py", "phase": "task", "basis_refs": [scope_ref]}])
        elif defect["defect_kind"] == "result_order":
            basis.append(cite("artifact:plan_contract", "/definition/tasks/0/acceptance_criteria/4",
                              plan.definition.tasks[0].acceptance_criteria[4]))
        refs = defect["allowed_task_ref_sets"][-1]
        inspection["finding_links"].append({
            "finding_code": code, "defect_kind": defect["defect_kind"], "criterion_ids": defect["criterion_ids"],
            "validation_ids": [vid], "task_refs": refs, "basis_refs": basis})
        findings.append({"finding_code": code, "gate": "verification", "severity": "error",
                         "summary": " ".join((*defect["criterion_ids"], vid, defect["defect_kind"])),
                         "evidence_refs": defect["required_evidence_refs"], "affected_task_refs": refs, "remediable": True})
    return {"review": {"findings": findings, "ratings": None if findings else RATINGS}, "inspection": inspection}


def validate(name, payload):
    plan, goal, state, project_map = inputs(name)
    envelope = PlanReviewEnvelope.model_validate(payload)
    validate_plan_inspection(envelope.inspection, plan=plan, goal=goal, project_map=project_map,
                             evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
                             findings=envelope.review.findings)
    return envelope


class PlanInspectionTests(unittest.TestCase):
    def test_targets_enumerate_all_pairs_including_disconnected_validations(self):
        plan, goal, _, _ = inputs("missing-link")
        targets = validation_comparison_targets(goal, plan)
        self.assertEqual(4 * 5, len(targets["ac_validation_pairs"]))
        self.assertIn({"criterion_id": "ac_003", "validation_id": "val_task_oracle"}, targets["ac_validation_pairs"])
        self.assertEqual(3, len(targets["constraint_task_pairs"]))
        self.assertEqual(None, targets["validations"][-1]["task_ref"])
        self.assertEqual({"criterion_id", "validation_id"}, set(targets["ac_validation_pairs"][0]))

    def test_fixed_defects_and_normal_combined_are_preserved(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                result = assess_inspection_review(validate(name, submission(name)), EXPECTED[name])
                self.assertTrue(result["passed"], result)
        self.assertEqual(2, len(submission("stored-multi-defect")["review"]["findings"]))

    def test_missing_duplicate_unknown_selector_quote_and_finding_conflicts_are_rejected(self):
        def bad_quote(p): p["inspection"]["citations"][0]["quote"] = "원문에 없는 인용"
        def bad_source(p): p["inspection"]["citations"][0]["source_ref"] = "project:invented"
        def bad_selector(p): p["inspection"]["citations"][0]["selector"] = "/hard_acceptance/-1/statement"
        def omit_pair(p): p["inspection"]["ac_validation_rows"].pop()
        def duplicate_pair(p): p["inspection"]["ac_validation_rows"].append(deepcopy(p["inspection"]["ac_validation_rows"][0]))
        def bad_id(p): p["inspection"]["validation_rows"][0]["validation_id"] = "invented"
        def omit_constraint(p): p["inspection"]["constraint_task_rows"].pop()
        def lose_finding(p): p["inspection"]["ac_validation_rows"][10]["finding_codes"] = []
        def wrong_task(p): p["inspection"]["finding_links"][0]["task_refs"] = []
        def wrong_evidence(p): p["review"]["findings"][0]["evidence_refs"] = ["source:goal"]
        def wrong_kind(p): p["inspection"]["finding_links"][0]["defect_kind"] = "validation_scope"
        for mutation in (bad_quote, bad_source, bad_selector, omit_pair, duplicate_pair, bad_id,
                         omit_constraint, lose_finding, wrong_task, wrong_evidence, wrong_kind):
            with self.subTest(mutation=mutation.__name__):
                payload = submission("missing-link")
                mutation(payload)
                with self.assertRaises((PlanInspectionError, ValueError)):
                    validate("missing-link", payload)

    def test_two_defects_require_separate_consistent_finding_links(self):
        payload = submission("stored-multi-defect")
        payload["inspection"]["validation_rows"][0]["finding_codes"] = ["FIXTURE_DEFECT_0"]
        with self.assertRaisesRegex(PlanInspectionError, "결함 종류"):
            validate("stored-multi-defect", payload)
        payload = submission("stored-multi-defect")
        payload["inspection"]["validation_rows"][0]["assessment"] = "supported"
        with self.assertRaisesRegex(PlanInspectionError, "정상 검사"):
            validate("stored-multi-defect", payload)

    def test_adapter_does_not_infer_semantic_answer_or_fix_coverage(self):
        plan, goal, _, _ = inputs("missing-link")
        original = plan.definition_digest
        payload = {"review": {"findings": [], "ratings": RATINGS},
                   "inspection": inspection_fixture(plan.model_dump(mode="json"), goal.definition.model_dump(mode="json"), revision=True)}
        accepted = validate("missing-link", payload)
        self.assertEqual(original, plan.definition_digest)
        report = assess_inspection_review(accepted, EXPECTED["missing-link"])
        self.assertFalse(report["passed"])
        self.assertEqual(["ac003-task-oracle-link"], report["missing_defects"])

    def test_fixed_evaluator_does_not_accept_only_one_of_two_defects_or_extra_findings(self):
        payload = submission("stored-multi-defect")
        payload["review"]["findings"].pop(0)
        payload["inspection"]["finding_links"].pop(0)
        for row in payload["inspection"]["ac_validation_rows"]:
            row.update(relation="optional_or_unrelated", finding_codes=[])
        # 미사용 coverage 인용도 함께 제거해 구조는 일관되지만 의미적으로 불완전한 제출물을 만든다.
        payload["inspection"]["citations"] = [c for c in payload["inspection"]["citations"] if "/goal_coverage/" not in c["selector"]]
        report = assess_inspection_review(validate("stored-multi-defect", payload), EXPECTED["stored-multi-defect"])
        self.assertEqual(["ac004-task-oracle-link"], report["missing_defects"])
        self.assertFalse(report["passed"])
        report = assess_inspection_review(validate("bad", submission("bad")), [])
        self.assertEqual(["FIXTURE_DEFECT_0"], report["unexpected_findings"])

    def test_global_semantic_obligation_is_distinct_from_explicit_ac_procedure(self):
        name = "semantic-missing-link"
        payload = submission(name)
        self.assertTrue(assess_inspection_review(validate(name, payload), EXPECTED[name])["passed"])
        plan, goal, _, _ = inputs("clean")
        payload = submission("clean")
        row = next(row for row in payload["inspection"]["ac_validation_rows"]
                   if (row["criterion_id"], row["validation_id"]) == ("ac_001", "val_task_validator_review"))
        constraint = next(c for c in payload["inspection"]["citations"] if c["selector"] == "/constraints/2/statement")
        row.update(relation="global_constraint_only", basis_refs=row["basis_refs"] + [constraint["citation_id"]])
        validate("clean", payload)
        row["relation"] = "explicit_procedure"
        with self.assertRaisesRegex(PlanInspectionError, "finding 연결"):
            validate("clean", payload)

    def test_envelope_is_required_and_core_decisions_cannot_be_submitted(self):
        with self.assertRaises(ValueError):
            PlanReviewEnvelope.model_validate({"findings": [], "ratings": RATINGS})
        with self.assertRaises(ValueError):
            PlanExpansionEnvelope.model_validate({"tasks": []})
        payload = submission("clean")
        payload["inspection"]["admissible"] = True
        with self.assertRaises(ValueError):
            validate("clean", payload)

    def test_partial_global_obligation_can_cite_existing_checks_and_missing_responsibility(self):
        payload = submission("clean")
        row = payload["inspection"]["constraint_task_rows"][2]
        code = "PARTIAL_TASK_INSPECTION_MISSING"
        row.update(applicability="required", validation_ids=["val_task_add_behavior_contract"], finding_codes=[code])
        basis = row["basis_refs"] + [payload["inspection"]["validation_rows"][0]["claim_ref"]]
        payload["review"] = {"ratings": None, "findings": [{"finding_code": code, "gate": "verification",
            "severity": "error", "summary": "존재하는 검사와 별개로 전역 검사 의무의 일부가 누락되었다는 합성 제출이다.",
            "evidence_refs": ["source:goal", "artifact:plan_contract"], "affected_task_refs": ["task_change_add"], "remediable": True}]}
        payload["inspection"]["finding_links"] = [{"finding_code": code, "defect_kind": "missing_task_validation",
            "criterion_ids": [], "validation_ids": [], "task_refs": ["task_change_add"], "basis_refs": basis}]
        validate("clean", payload)
        # 구조상 일관된 제출은 허용하지만 고정 정상 oracle을 바꾸지는 않는다.
        self.assertFalse(assess_inspection_review(PlanReviewEnvelope.model_validate(payload), [])["passed"])

    def test_detection_binding_does_not_override_core_gate_classification(self):
        payload = submission("missing-link")
        payload["review"]["findings"][0]["gate"] = "goal"
        self.assertTrue(assess_inspection_review(validate("missing-link", payload), EXPECTED["missing-link"])["passed"])


if __name__ == "__main__":
    unittest.main()
