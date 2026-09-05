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
from flowmarshal.engine.plan_inspection_eval import assess_fixed_ac_link_requirements, assess_inspection_review
from flowmarshal.engine.planning import plan_review_evidence_catalog
from flowmarshal.engine.planner_roles import PlanReviewEnvelope
from flowmarshal.engine.roles import RoleCallRequest


ROOT = Path(__file__).resolve().parent / "fixtures" / "engine"
RAW = json.loads((ROOT / "plan-inspection-raw-v1.json").read_text(encoding="utf-8"))
RAW_V3_REJECTED = json.loads((ROOT / "plan-inspection-raw-v3-rejected.json").read_text(encoding="utf-8"))
RAW_S06_12_ROOT = ROOT / "r-s06-12-raw-rejected"
RAW_S06_12_MANIFEST = json.loads((RAW_S06_12_ROOT / "manifest.json").read_text(encoding="utf-8"))
RAW_S06_17_ROOT = ROOT / "r-s06-17-bad-axis-conflation"
RAW_S06_17_MANIFEST = json.loads((RAW_S06_17_ROOT / "manifest.json").read_text(encoding="utf-8"))
FILES = json.loads((ROOT / "plan-inspection-regressions.json").read_text(encoding="utf-8"))["files"]
V3_EXPECTATIONS = json.loads((ROOT / "plan-inspection-v2-expectations.json").read_text(encoding="utf-8"))
REFERENCE_ENTRY = "project:entry_ad363844d3392d9ef718d2d6"
RAW_S06_27_PATH = ROOT / "r-s06-27-clean-scope-binding-failure.json"
RAW_S06_27 = json.loads(RAW_S06_27_PATH.read_text(encoding="utf-8"))


def _r27_context():
    plan, goal, project_map, evidence_catalog = _context()
    data = plan.model_dump(mode="json")
    for selector, value in RAW_S06_27["plan_context_overrides"].items():
        target = data
        parts = selector.lstrip("/").split("/")
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        target[parts[-1]] = deepcopy(value)
    data["definition_digest"] = sha256_digest(data["definition"])
    plan = PlanContractRevision.model_validate(data)
    evidence_catalog["artifact:plan_contract"] = data
    return plan, goal, project_map, evidence_catalog


def _validate_r27(payload):
    plan, goal, project_map, evidence_catalog = _r27_context()
    envelope = PlanReviewEnvelope.model_validate(payload)
    validate_plan_inspection(envelope.inspection, plan=plan, goal=goal, project_map=project_map,
                             evidence_catalog=evidence_catalog, findings=envelope.review.findings)
    return envelope


def _r27_complete_refs_in_memory():
    """원본 판단을 유지한 별도 테스트 사본에 참조만 명시적으로 추가한다."""
    payload = json.loads(RAW_S06_27["raw_final_response"])
    inspection = payload["inspection"]
    scopes = {row["scope_id"]: row for row in inspection["validation_scope_rows"]}
    scopes["scope_v5_phase"]["basis_refs"].append("p_contract")
    citations = {row["citation_id"]: row for row in inspection["citations"]}
    project_refs = {}
    for row in inspection["validation_rows"]:
        project_refs[row["validation_id"]] = {
            ref for mechanism in row["mechanisms"] for ref in mechanism["basis_refs"]
            if citations[ref]["source_ref"].startswith("project:")
        }
    for scope in scopes.values():
        project_refs[scope["validation_id"]].update(
            ref for ref in scope["basis_refs"] if citations[ref]["source_ref"].startswith("project:")
        )
    for row in inspection["ac_validation_rows"]:
        required = project_refs[row["validation_id"]].copy()
        for scope_id in row["scope_ids"]:
            required.update([scopes[scope_id]["claim_ref"], *scopes[scope_id]["basis_refs"]])
        row["basis_refs"].extend(sorted(required - set(row["basis_refs"])))
    return payload


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
    return _current_contract(RAW["submission"])


def _current_contract(payload: dict) -> dict:
    """byte-preserved 과거 제출을 현재 provider 계약으로 메모리에서만 투영한다."""
    converted = deepcopy(payload)
    inspection = converted["inspection"]
    scope_ids = {}
    scopes = []
    for index, row in enumerate(inspection["validation_rows"]):
        assessment = row.pop("assessment")
        finding_codes = row.pop("finding_codes")
        supported_id = f"legacy_scope_{index}_supported"
        scope_ids[row["validation_id"]] = supported_id
        basis_refs = list(dict.fromkeys(
            [row["claim_ref"], *(ref for mechanism in row["mechanisms"] for ref in mechanism["basis_refs"])]
        ))
        phase = row["mechanisms"][0]["phase"] if len({item["phase"] for item in row["mechanisms"]}) == 1 else None
        scopes.append({"scope_id": supported_id, "validation_id": row["validation_id"],
                       "claim_ref": row["claim_ref"], "procedure": row["mechanisms"][0]["tool"],
                       "phase": phase, "basis_refs": basis_refs, "assessment": "supported",
                       "finding_codes": []})
        if assessment != "supported":
            scopes.append({"scope_id": f"legacy_scope_{index}_{assessment}",
                           "validation_id": row["validation_id"], "claim_ref": row["claim_ref"],
                           "procedure": row["mechanisms"][0]["tool"], "phase": phase,
                           "basis_refs": basis_refs, "assessment": assessment,
                           "finding_codes": finding_codes})
    inspection["validation_scope_rows"] = scopes
    for row in inspection["ac_validation_rows"]:
        if "relation" in row:
            row["ac_link_required"] = row.pop("relation") == "explicit_procedure"
        row["scope_ids"] = [scope_ids[row["validation_id"]]] if row["ac_link_required"] else []
    return converted


def _current_rows(rows: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in row.items() if key != "relation"}
        | {"ac_link_required": row["relation"] == "explicit_procedure"}
        for row in deepcopy(rows)
    ]


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
    for section in ("ac_validation_rows", "constraint_task_rows", "validation_scope_rows"):
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


def _make_ac_statement_citations(payload: dict, goal: GoalContractRevision) -> None:
    """원시 intent 인용은 유지하고, 현재 adapter 계약의 statement 인용만 명시적으로 보완한다."""
    goal_data = goal.definition.model_dump(mode="json")
    statement_refs = {}
    intent_refs = {}
    for index, criterion in enumerate(goal_data["hard_acceptance"], start=1):
        citation_id = f"C_AC{index}_STATEMENT"
        payload["inspection"]["citations"].append({
            "citation_id": citation_id,
            "source_ref": "source:goal",
            "selector": f"/hard_acceptance/{index - 1}/statement",
            "quote": criterion["statement"],
        })
        statement_refs[criterion["criterion_id"]] = citation_id
        intent_refs[criterion["criterion_id"]] = next(
            item["citation_id"]
            for item in payload["inspection"]["citations"]
            if item["source_ref"] == "source:goal" and
            item["selector"] == f"/hard_acceptance/{index - 1}/validation_intent"
        )
    for row in payload["inspection"]["ac_validation_rows"]:
        row["basis_refs"].append(statement_refs[row["criterion_id"]])
    for link in payload["inspection"]["finding_links"]:
        for criterion_id in link["criterion_ids"]:
            for citation_id in (statement_refs[criterion_id], intent_refs[criterion_id]):
                if citation_id not in link["basis_refs"]:
                    link["basis_refs"].append(citation_id)


def _link_registered_mechanism_citations(payload: dict) -> None:
    """검사 범위 판단에 쓴 정식 project citation을 같은 validation의 모든 AC 행에 재사용한다."""
    citations = {item["citation_id"]: item for item in payload["inspection"]["citations"]}
    project_refs_by_validation = {
        row["validation_id"]: {
            reference
            for mechanism in row["mechanisms"]
            for reference in mechanism["basis_refs"]
            if citations[reference]["source_ref"].startswith("project:")
        }
        for row in payload["inspection"]["validation_rows"]
    }
    for row in payload["inspection"]["ac_validation_rows"]:
        for reference in project_refs_by_validation[row["validation_id"]]:
            if reference not in row["basis_refs"]:
                row["basis_refs"].append(reference)


def _clean_non_target_conflicts(payload: dict, goal: GoalContractRevision) -> dict:
    _formalize_reference_addresses(payload)
    _make_ac_statement_citations(payload, goal)
    _link_registered_mechanism_citations(payload)
    _make_constraint_citations(payload, goal)
    _remove_finding(payload, "F003")
    _remove_finding(payload, "F004")
    for row in payload["inspection"]["validation_scope_rows"]:
        if row["validation_id"] == payload["inspection"]["validation_rows"][3]["validation_id"]:
            row["assessment"] = "supported"
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
    def test_r27_original_scope_binding_rejection_and_provenance_are_immutable(self):
        self.assertEqual("sha256:5179b61e2ed1765d21f84e85e4f5954941b1a714c42cb04a4a82a406a41b1c73",
                         sha256_bytes(RAW_S06_27_PATH.read_bytes()))
        provenance = RAW_S06_27["provenance"]
        self.assertEqual(provenance["source_final_response_utf8_digest"],
                         sha256_bytes(RAW_S06_27["raw_final_response"].encode("utf-8")))
        self.assertEqual(provenance["base_fixture_bytes_digest"],
                         sha256_bytes((ROOT / "plan-inspection-regressions.json").read_bytes()))
        self.assertEqual(provenance["reference_bytes_digest"],
                         sha256_bytes((ROOT / "plan-inspection-reference.md").read_bytes()))
        self.assertTrue((ROOT / provenance["handoff"]).is_file())
        plan, goal, _, _ = _r27_context()
        self.assertEqual(provenance["source_plan_canonical_digest"], sha256_digest(plan.model_dump(mode="json")))
        self.assertEqual(provenance["source_goal_definition_canonical_digest"],
                         sha256_digest(goal.definition.model_dump(mode="json")))
        self.assertEqual(RAW_S06_27["expected_rows_canonical_digest"],
                         sha256_digest(RAW_S06_27["expected_ac_validation_rows"]))
        payload = json.loads(RAW_S06_27["raw_final_response"])
        before = deepcopy(payload)
        with self.assertRaises(PlanInspectionError) as failure:
            _validate_r27(payload)
        self.assertEqual(
            "대조표 검사 scope 절차·phase·근거 결속 오류: "
            "validation_id=val_goal_independent_behavior_contract, scope_id=scope_v5_phase, phase='goal', "
            "mechanism_candidates=[(0, 'goal', ['p_contract'])] (mechanism_index, phase, missing_refs)",
            str(failure.exception),
        )
        self.assertEqual(before, payload)

    def test_r27_scope_only_fix_preserves_eight_ac_binding_failures(self):
        payload = json.loads(RAW_S06_27["raw_final_response"])
        scopes = {row["scope_id"]: row for row in payload["inspection"]["validation_scope_rows"]}
        missing = {
            (row["criterion_id"], row["validation_id"]): sorted({
                ref for scope_id in row["scope_ids"]
                for ref in [scopes[scope_id]["claim_ref"], *scopes[scope_id]["basis_refs"]]
            } - set(row["basis_refs"]))
            for row in payload["inspection"]["ac_validation_rows"]
        }
        missing = {key: value for key, value in missing.items() if value}
        self.assertEqual({(f"ac_00{i}", validation) for i in range(1, 5) for validation in
                          ("val_task_add_behavior_contract", "val_goal_independent_behavior_contract")}, set(missing))
        self.assertEqual(["p_task", "v5_checks", "v5_phase"],
                         missing[("ac_001", "val_goal_independent_behavior_contract")])
        scopes["scope_v5_phase"]["basis_refs"].append("p_contract")
        before = deepcopy(payload)
        with self.assertRaises(PlanInspectionError) as failure:
            _validate_r27(payload)
        self.assertEqual(
            "대조표 AC 연결 scope 근거 누락: validation_id=val_task_add_behavior_contract, "
            "criterion_id=ac_001, scope_id=scope_v1_phase, missing_refs=['v1_phase']",
            str(failure.exception),
        )
        self.assertEqual(before, payload)

    def test_r27_complete_reference_copy_accepts_structure_but_preserves_one_of_28_boolean_mismatches(self):
        payload = _r27_complete_refs_in_memory()
        before = deepcopy(payload)
        envelope = _validate_r27(payload)
        self.assertEqual(before, payload)
        original = json.loads(RAW_S06_27["raw_final_response"])
        original_bools = [row["ac_link_required"] for row in original["inspection"]["ac_validation_rows"]]
        self.assertEqual(28, len(original_bools))
        self.assertEqual(original_bools, [row.ac_link_required for row in envelope.inspection.ac_validation_rows])
        # 참조 외 필드·인용·판정·선택 scope·finding은 모두 원본과 같다.
        for key in ("validation_scope_rows", "ac_validation_rows"):
            for raw_row, row in zip(original["inspection"][key], payload["inspection"][key], strict=True):
                row["basis_refs"] = raw_row["basis_refs"]
        self.assertEqual(original, payload)
        plan, _, _, _ = _r27_context()
        assessment = assess_fixed_ac_link_requirements(
            envelope.inspection, RAW_S06_27["expected_ac_validation_rows"], plan.model_dump(mode="json"),
        )
        self.assertTrue(assessment["pair_set_matches"])
        self.assertFalse(assessment["passed"])
        self.assertEqual([{
            "criterion_id": "ac_004", "validation_id": "val_goal_independent_unittest",
            "expected": True, "actual": False,
        }], assessment["requirement_differences"])

    def test_r_s06_17_bad_axis_conflation_raw_reproduces_one_missing_finding_and_three_booleans(self):
        for filename, digest in RAW_S06_17_MANIFEST["files"].items():
            with self.subTest(filename=filename):
                self.assertEqual(digest, sha256_bytes((RAW_S06_17_ROOT / filename).read_bytes()))
        for filename, digest in RAW_S06_17_MANIFEST["preserved_baselines"].items():
            with self.subTest(baseline=filename):
                self.assertEqual(digest, sha256_bytes((ROOT / filename).read_bytes()))

        result = json.loads((RAW_S06_17_ROOT / "result.json").read_text(encoding="utf-8"))
        terminal = json.loads((RAW_S06_17_ROOT / "terminal.json").read_text(encoding="utf-8"))
        request = RoleCallRequest.model_validate_json((RAW_S06_17_ROOT / "request.json").read_text(encoding="utf-8"))
        expectation = json.loads((RAW_S06_17_ROOT / "case-expectation.json").read_text(encoding="utf-8"))
        recorded = json.loads((RAW_S06_17_ROOT / "bad-assessment.json").read_text(encoding="utf-8"))
        binding = json.loads((RAW_S06_17_ROOT / "binding-verification.json").read_text(encoding="utf-8"))
        summary = json.loads((RAW_S06_17_ROOT / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(result["payload"], json.loads(terminal["final_response"]))
        self.assertEqual(result["receipt"]["input_digest"], request.request_digest)
        self.assertEqual(result["receipt"]["output_digest"], sha256_digest(result["payload"]))
        self.assertTrue(binding["passed"])
        self.assertTrue(all(binding["checks"].values()))
        self.assertEqual("FAIL", summary["status"])
        self.assertEqual((2, 2), (summary["logical_calls"], summary["provider_turns"]))
        self.assertEqual(0, result["receipt"]["schema_recovery_attempts"])

        envelope = PlanReviewEnvelope.model_validate(_current_contract(result["payload"]))
        reproduced = assess_inspection_review(
            envelope,
            expectation["expected_defects"],
            fixed_ac_link_rows=expectation["ac_validation_rows"],
            plan=request.payload["evidence_catalog"]["artifact:plan_contract"],
        )
        self.assertFalse(reproduced["passed"])
        self.assertEqual(RAW_S06_17_MANIFEST["expected_missing_defects"], reproduced["missing_defects"])
        self.assertEqual(
            RAW_S06_17_MANIFEST["expected_requirement_differences"],
            reproduced["fixed_ac_link_requirement_assessment"]["requirement_differences"],
        )
        self.assertEqual(recorded["missing_defects"], reproduced["missing_defects"])
        self.assertEqual(
            recorded["fixed_ac_link_requirement_assessment"]["requirement_differences"],
            reproduced["fixed_ac_link_requirement_assessment"]["requirement_differences"],
        )

    def test_r_s06_12_raw_rejection_is_byte_preserved_and_keeps_missing_registered_citation_rows(self):
        for filename, digest in RAW_S06_12_MANIFEST["files"].items():
            with self.subTest(filename=filename):
                self.assertEqual(digest, sha256_bytes((RAW_S06_12_ROOT / filename).read_bytes()))
        terminal = json.loads((RAW_S06_12_ROOT / "terminal.json").read_text(encoding="utf-8"))
        raw_response = json.loads(terminal["final_response"])
        self.assertEqual(RAW_S06_12_MANIFEST["raw_final_response_utf8_digest"],
                         sha256_bytes(terminal["final_response"].encode("utf-8")))
        summary = json.loads((RAW_S06_12_ROOT / "summary.json").read_text(encoding="utf-8"))
        failed = json.loads((RAW_S06_12_ROOT / "failed.json").read_text(encoding="utf-8"))
        self.assertEqual(RAW_S06_12_MANIFEST["expected_status"], summary["status"])
        self.assertEqual(RAW_S06_12_MANIFEST["expected_error_summary"], failed["receipts"][0]["error_summary"])
        self.assertEqual(1, summary["logical_calls"])
        self.assertEqual(1, summary["provider_turns"])
        self.assertEqual(0, failed["receipts"][0]["schema_recovery_attempts"])

        envelope = PlanReviewEnvelope.model_validate(_current_contract(raw_response))
        self.assertFalse(envelope.review.findings)
        self.assertIsNotNone(envelope.review.ratings)
        citations = {item.citation_id: item for item in envelope.inspection.citations}
        project_refs_by_validation = {
            row.validation_id: {
                ref for mechanism in row.mechanisms for ref in mechanism.basis_refs
                if citations[ref].source_ref.startswith("project:")
            }
            for row in envelope.inspection.validation_rows
        }
        missing = {
            (row.criterion_id, row.validation_id, ref)
            for row in envelope.inspection.ac_validation_rows
            for ref in project_refs_by_validation[row.validation_id] - set(row.basis_refs)
        }
        self.assertEqual({
            (criterion, validation, "reference_goal_evidence")
            for criterion in ("ac_001", "ac_002", "ac_003")
            for validation in ("val_goal_independent_behavior_contract", "val_goal_independent_scope_preservation")
        }, missing)

    def test_r_s06_13_independent_synthetic_normal_fixture_is_structurally_valid(self):
        fixture = json.loads((ROOT / "plan-inspection-r-s06-13-synthetic-normal.json").read_text(encoding="utf-8"))
        self.assertEqual("/expected_ac_validation_rows", fixture["provenance"]["relation_rows_selector"])
        self.assertEqual(fixture["response_canonical_digest"], sha256_digest(fixture["response"]))
        plan = PlanContractRevision.model_validate(fixture["plan"])
        goal = GoalContractRevision.model_validate(fixture["goal"])
        state = StateSnapshot.model_validate(fixture["state"])
        project_map = ProjectMapRevision.model_validate(fixture["project_map"])
        envelope = PlanReviewEnvelope.model_validate(_current_contract(fixture["response"]))
        validate_plan_inspection(
            envelope.inspection,
            plan=plan,
            goal=goal,
            project_map=project_map,
            evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
            findings=envelope.review.findings,
        )
        assessment = assess_fixed_ac_link_requirements(
            envelope.inspection, _current_rows(fixture["expected_ac_validation_rows"]), fixture["plan"],
        )
        self.assertTrue(assessment["passed"], assessment)

    def test_v1_raw_response_without_ac_statement_citations_is_rejected(self):
        """주소 오류를 분리해도 원시 AC basis는 새 계약을 만족하지 않는다."""
        with self.assertRaisesRegex(PlanInspectionError, "AC statement 인용 누락"):
            _validate(_formalize_reference_addresses(_payload()))

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

        inspection = PlanInspection.model_validate(_current_contract({"inspection": raw["inspection"]})["inspection"])
        relation = assess_fixed_ac_link_requirements(
            inspection, _current_rows(V3_EXPECTATIONS["ac_validation_rows"]), FILES["input-clean-plan.json"],
        )
        self.assertTrue(relation["pair_set_matches"])
        self.assertFalse(relation["passed"])
        self.assertEqual(4, len(relation["requirement_differences"]))

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

    def test_corrected_addresses_and_ac_basis_still_reject_raw_internal_contradictions(self):
        goal = GoalContractRevision.model_validate(FILES["input-goal.json"])
        payload = _formalize_reference_addresses(_payload())
        _make_ac_statement_citations(payload, goal)
        _link_registered_mechanism_citations(payload)
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
                lambda p: next(
                    row for row in p["inspection"]["validation_scope_rows"]
                    if row["validation_id"] == raw["inspection"]["validation_rows"][3]["validation_id"]
                    and row["assessment"] == "supported"
                ).update(assessment="contradicted", finding_codes=["F004"]),
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
