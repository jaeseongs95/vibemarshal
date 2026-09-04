"""R-S06-10 원시 Plan Reviewer 제출물의 provenance·내부 일관성 회귀.

``plan-inspection-raw-v1.json``은 실제 r-s06-09 terminal 응답을 고정한 자료다.
여기서 사용하는 ``inspection_fixture``와 달리 의미 판단을 새로 만들지 않고, 원시
인용·finding·대조표 행을 그대로 adapter에 공급한다. 정답을 확인하기 위한 사본의
주소·행만 테스트 안에서 명시적으로 치환한다.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.domain import (
    GoalContractRevision,
    PlanContractRevision,
    ProjectMapRevision,
    StateSnapshot,
)
from flowmarshal.engine.plan_inspection import PlanInspection, PlanInspectionError, validate_plan_inspection
from flowmarshal.engine.plan_inspection_eval import assess_fixed_ac_validation_relations
from flowmarshal.engine.planning import plan_review_evidence_catalog
from flowmarshal.engine.planner_roles import PlanReviewEnvelope


ROOT = Path(__file__).resolve().parent / "fixtures" / "engine"
RAW = json.loads((ROOT / "plan-inspection-raw-v1.json").read_text(encoding="utf-8"))
RAW_V3_REJECTED = json.loads((ROOT / "plan-inspection-raw-v3-rejected.json").read_text(encoding="utf-8"))
FILES = json.loads((ROOT / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
V3_EXPECTATIONS = json.loads((ROOT / "plan-inspection-v2-expectations.json").read_text(encoding="utf-8"))
REFERENCE_ENTRY = "project:entry_ad363844d3392d9ef718d2d6"


def _context(*, goal_data: dict | None = None):
    plan = PlanContractRevision.model_validate(deepcopy(FILES["input-clean-plan.json"]))
    goal = GoalContractRevision.model_validate(
        deepcopy(goal_data or FILES["input-goal.json"])
    )
    state = StateSnapshot.model_validate(deepcopy(FILES["input-state.json"]))
    map_data = deepcopy(FILES["input-project-map.json"])
    reference = next(entry for entry in map_data["entries"] if entry["entry_id"] == REFERENCE_ENTRY.split(":", 1)[1])
    # 원본 run 경로 대신 저장소의 고정 reference를 사용한다. 내용·digest는 동일하다.
    reference["path"] = str(ROOT / "plan-inspection-reference.md")
    project_map = ProjectMapRevision.model_validate(map_data)
    evidence_catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
    return plan, goal, project_map, evidence_catalog


def _payload() -> dict:
    return deepcopy(RAW["submission"])


def _formalize_reference_addresses(payload: dict) -> dict:
    """raw quote는 그대로 두고 등록 reference의 source_ref·selector만 고친 사본."""
    for citation in payload["inspection"]["citations"]:
        if citation["citation_id"] in {"C_REF_TASK", "C_REF_VALIDATOR", "C_REF_GOAL"}:
            citation["source_ref"] = REFERENCE_ENTRY
            citation["selector"] = "/content"
    # formal project citation은 finding에서 source:project_map evidence로 귀속되므로,
    # 아래 내부 모순을 검사할 때 이 비목표 provenance 오류가 먼저 발생하지 않게 한다.
    for finding in payload["review"]["findings"]:
        if finding["finding_code"] in {"F002", "F004"} and "source:project_map" not in finding["evidence_refs"]:
            finding["evidence_refs"].append("source:project_map")
    return payload


def _remove_finding(payload: dict, code: str) -> None:
    payload["review"]["findings"] = [
        finding for finding in payload["review"]["findings"] if finding["finding_code"] != code
    ]
    payload["inspection"]["finding_links"] = [
        link for link in payload["inspection"]["finding_links"] if link["finding_code"] != code
    ]
    for section in ("ac_validation_rows", "constraint_task_rows", "validation_rows"):
        for row in payload["inspection"][section]:
            row["finding_codes"] = [item for item in row["finding_codes"] if item != code]


def _make_constraint_citations(payload: dict, goal: GoalContractRevision) -> None:
    goal_data = goal.definition.model_dump(mode="json")
    for index, (citation_id, row_index) in enumerate((("C_CONSTRAINT1", 0), ("C_CONSTRAINT2", 1))):
        payload["inspection"]["citations"].append({
            "citation_id": citation_id,
            "source_ref": "source:goal",
            "selector": f"/constraints/{row_index}/statement",
            "quote": goal_data["constraints"][row_index]["statement"],
        })
        payload["inspection"]["constraint_task_rows"][row_index]["basis_refs"].append(citation_id)


def _clean_non_target_conflicts(payload: dict, goal: GoalContractRevision) -> dict:
    _formalize_reference_addresses(payload)
    _make_constraint_citations(payload, goal)
    _remove_finding(payload, "F003")
    _remove_finding(payload, "F004")
    payload["inspection"]["validation_rows"][3]["assessment"] = "supported"
    # C_REF_TASK/C_REF_GOAL은 등록 파일을 가리키므로 finding의 원본 evidence
    # 집합에도 source:project_map을 명시해야 한다.
    finding = next(item for item in payload["review"]["findings"] if item["finding_code"] == "F002")
    if "source:project_map" not in finding["evidence_refs"]:
        finding["evidence_refs"].append("source:project_map")
    return payload


def _validate(payload: dict, *, goal_data: dict | None = None):
    plan, goal, project_map, evidence_catalog = _context(goal_data=goal_data)
    envelope = PlanReviewEnvelope.model_validate(payload)
    validate_plan_inspection(
        envelope.inspection,
        plan=plan,
        goal=goal,
        project_map=project_map,
        evidence_catalog=evidence_catalog,
        findings=envelope.review.findings,
    )
    return envelope


class RawPlanInspectionRegressionTests(unittest.TestCase):
    def test_v3_rejected_raw_response_keeps_rating_failure_and_independent_inspection_errors(self):
        """v3 원문은 보정하지 않고 rating·constraint·관계 오류를 각자 고정한다."""
        raw = RAW_V3_REJECTED["raw_response"]
        provenance = RAW_V3_REJECTED["provenance"]
        raw_bytes = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(provenance["raw_response_digest"], sha256_bytes(raw_bytes))
        self.assertEqual("finding이 없으면 fitness rating이 필요합니다.", provenance["schema_failure"])
        self.assertEqual({"findings": [], "ratings": None}, raw["review"])
        # 과거의 잘못된 응답을 rating으로 PASS 보정하지 않는다.
        with self.assertRaisesRegex(ValidationError, "finding이 없으면 fitness rating이 필요합니다"):
            PlanReviewEnvelope.model_validate(raw)

        inspection = PlanInspection.model_validate(raw["inspection"])
        relation = assess_fixed_ac_validation_relations(inspection, V3_EXPECTATIONS["ac_validation_rows"])
        self.assertTrue(relation["pair_set_matches"])
        self.assertFalse(relation["passed"])
        self.assertEqual(8, len(relation["relation_differences"]))

        citations = {item.citation_id: item for item in inspection.citations}
        for constraint_id in ("constraint_001", "constraint_002"):
            row = next(item for item in inspection.constraint_task_rows if item.constraint_id == constraint_id)
            self.assertFalse(any(
                citations[reference].selector == f"/constraints/{int(constraint_id[-3:]) - 1}/statement"
                for reference in row.basis_refs
            ))

    def test_each_raw_reference_citation_wrong_goal_trace_address_is_rejected(self):
        for target in ("C_REF_TASK", "C_REF_VALIDATOR", "C_REF_GOAL"):
            with self.subTest(citation_id=target):
                payload = _clean_non_target_conflicts(
                    _payload(), GoalContractRevision.model_validate(FILES["input-goal.json"])
                )
                for citation in payload["inspection"]["citations"]:
                    if citation["citation_id"] == target:
                        citation["source_ref"] = "source:goal"
                        citation["selector"] = "/source_traces/4/statement"
                with self.assertRaisesRegex(
                    PlanInspectionError, "대조표 인용이 선택한 원문 문자열과 일치하지 않습니다"
                ):
                    _validate(payload)

    def test_registered_project_reference_accepts_original_quote_and_digest(self):
        payload = _clean_non_target_conflicts(
            _payload(), GoalContractRevision.model_validate(FILES["input-goal.json"])
        )
        # 주소만 치환했으며 quote는 raw와 동일하다. reference 파일은 project map digest로 검증된다.
        envelope = _validate(payload)
        self.assertEqual(2, len(envelope.review.findings))

    def test_corrected_addresses_alone_still_reject_raw_internal_contradictions(self):
        payload = _formalize_reference_addresses(_payload())
        with self.assertRaisesRegex(PlanInspectionError, "대조표 정상 연결 행의 finding 모순"):
            _validate(payload)

    def test_address_correction_does_not_hide_each_internal_contradiction(self):
        clean = _clean_non_target_conflicts(
            _payload(), GoalContractRevision.model_validate(FILES["input-goal.json"])
        )
        raw = RAW["submission"]
        cases = {
            "ac_global_constraint_row": (
                lambda p: p["inspection"]["ac_validation_rows"][10]["finding_codes"].append("F003"),
                "대조표 정상 연결 행의 finding 모순",
            ),
            "constraint_non_applicable_0": (
                lambda p: p["inspection"]["constraint_task_rows"][0]["basis_refs"].remove("C_CONSTRAINT1"),
                "대조표 해당 constraint 인용 누락",
            ),
            "constraint_non_applicable_1": (
                lambda p: p["inspection"]["constraint_task_rows"][1]["basis_refs"].remove("C_CONSTRAINT2"),
                "대조표 해당 constraint 인용 누락",
            ),
            "constraint_required_f004_kind": (
                lambda p: p["inspection"]["constraint_task_rows"][2]["finding_codes"].append("F004"),
                "대조표 finding 결함 종류 불일치",
            ),
            "validation_contradicted_f004_kind": (
                lambda p: p["inspection"]["validation_rows"][3].update(
                    assessment="contradicted", finding_codes=["F004"]
                ),
                "대조표 finding 결함 종류 불일치",
            ),
        }
        for name, (mutate, expected_error) in cases.items():
            with self.subTest(case=name):
                payload = deepcopy(clean)
                if name.startswith("ac_"):
                    payload["review"]["findings"].append(
                        next(item for item in raw["review"]["findings"] if item["finding_code"] == "F003")
                    )
                    payload["inspection"]["finding_links"].append(
                        next(item for item in raw["inspection"]["finding_links"] if item["finding_code"] == "F003")
                    )
                elif name.endswith("f004_kind"):
                    finding = deepcopy(
                        next(item for item in raw["review"]["findings"] if item["finding_code"] == "F004")
                    )
                    finding["evidence_refs"].append("source:project_map")
                    payload["review"]["findings"].append(finding)
                    payload["inspection"]["finding_links"].append(
                        next(item for item in raw["inspection"]["finding_links"] if item["finding_code"] == "F004")
                    )
                mutate(payload)
                with self.assertRaisesRegex(PlanInspectionError, expected_error):
                    _validate(payload)

    def test_trace_reordering_preserves_formal_project_reference(self):
        payload = _clean_non_target_conflicts(
            _payload(), GoalContractRevision.model_validate(FILES["input-goal.json"])
        )
        goal_data = deepcopy(FILES["input-goal.json"])
        traces = goal_data["definition"]["source_traces"]
        goal_data["definition"]["source_traces"] = list(reversed(traces))
        goal_data["definition_digest"] = sha256_digest(goal_data["definition"])
        _validate(payload, goal_data=goal_data)

    def test_formal_reference_rejects_bad_selector_digest_and_quote(self):
        clean = _clean_non_target_conflicts(
            _payload(), GoalContractRevision.model_validate(FILES["input-goal.json"])
        )
        for label, mutate in (
            ("selector", lambda c: c.update(selector="/missing")),
            ("digest", lambda e: e.update(content_digest="sha256:" + "0" * 64)),
            ("quote", lambda c: c.update(quote="원문에 없는 인용")),
        ):
            with self.subTest(kind=label):
                payload = deepcopy(clean)
                citation = next(
                    item for item in payload["inspection"]["citations"] if item["citation_id"] == "C_REF_GOAL"
                )
                if label == "digest":
                    # ProjectMap entry의 digest만 바꾼 별도 입력을 _validate가 사용할 수 있도록
                    # map construction 경계를 직접 검사한다.
                    plan, goal, project_map, evidence_catalog = _context()
                    bad_entries = tuple(
                        entry.model_copy(update={"content_digest": "sha256:" + "0" * 64})
                        if entry.entry_id == REFERENCE_ENTRY.split(":", 1)[1]
                        else entry
                        for entry in project_map.entries
                    )
                    project_map = project_map.model_copy(update={"entries": bad_entries})
                    with self.assertRaisesRegex(PlanInspectionError, "대조표 파일 원문 digest 불일치"):
                        envelope = PlanReviewEnvelope.model_validate(payload)
                        validate_plan_inspection(
                            envelope.inspection,
                            plan=plan,
                            goal=goal,
                            project_map=project_map,
                            evidence_catalog=evidence_catalog,
                            findings=envelope.review.findings,
                        )
                    continue
                mutate(citation)
                expected_error = (
                    "대조표 selector가 원문에 없습니다."
                    if label == "selector"
                    else "대조표 인용이 선택한 원문 문자열과 일치하지 않습니다."
                )
                with self.assertRaisesRegex(PlanInspectionError, expected_error):
                    _validate(payload)


if __name__ == "__main__":
    unittest.main()
