"""v2 직접 제출물과 동결된 사례 기대값의 평가.

compiler가 만든 closure는 citation/evidence 결속을 확인하는 데만 쓴다. AC 관계의
의미 판단은 모델이 제출한 sparse 양의 AC scope 선택을 전체 행렬로 확장할 뿐이며,
finding·scope 판단을 생성하지 않는다.
"""
from __future__ import annotations

from typing import Any

from .plan_inspection_eval import (
    CASE_EVALUATION_SCOPE,
    InspectionExpectationError,
    verify_case_expectation,
)
from .plan_inspection import InspectionCitation
from .plan_inspection_v2 import (
    CompiledPlanInspectionV2,
    InspectionTargetCatalogEntryV2,
    PlanInspectionV2,
    ReviewFindingV2,
)


V2_EVALUATION_CONTRACT = "plan-inspection-evaluation-v2"


def _fixed_ac_link_requirement_assessment(
        compiled: CompiledPlanInspectionV2, expected_rows: list[dict[str, Any]], plan: Any,
) -> dict[str, Any]:
    """희소 양의 연결에서 파생한 AC×validation bool을 동결 행과 정확히 대조한다."""
    expected = {(row["criterion_id"], row["validation_id"]): row["ac_link_required"]
                for row in expected_rows}
    actual = {(row.criterion_id, row.validation_id): row.ac_link_required
              for row in compiled.ac_validation_decisions}
    pair_set_matches = (bool(expected_rows) and len(expected) == len(expected_rows) and
                        len(actual) == len(compiled.ac_validation_decisions) and
                        set(actual) == set(expected))
    requirement_differences = [
        {"criterion_id": criterion_id, "validation_id": validation_id,
         "expected": expected[(criterion_id, validation_id)],
         "actual": actual[(criterion_id, validation_id)]}
        for criterion_id, validation_id in sorted(set(actual) & set(expected))
        if actual[(criterion_id, validation_id)] != expected[(criterion_id, validation_id)]
    ]
    definition = getattr(plan, "definition", plan)
    coverage = {item.criterion_id: set(item.validation_ids) for item in definition.goal_coverage}
    link_presence = [
        {"criterion_id": criterion_id, "validation_id": validation_id,
         "ac_link_required": actual[(criterion_id, validation_id)],
         "actual_link_exists": validation_id in coverage.get(criterion_id, set())}
        for criterion_id, validation_id in sorted(actual)
    ]
    return {"applicable": True, "pair_set_matches": pair_set_matches,
            "requirement_differences": requirement_differences, "link_presence": link_presence,
            "passed": pair_set_matches and not requirement_differences}


def _target_values(
        finding: ReviewFindingV2, inspection: PlanInspectionV2,
        compiled: CompiledPlanInspectionV2,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """adapter가 target ID에서 해석한 typed target만 평가한다."""
    scopes = {row.scope_id: row for row in inspection.validation_scope_rows}
    resolved = next(
        (row.target_refs for row in compiled.resolved_finding_targets
         if row.finding_code == finding.finding_code),
        (),
    )
    criteria: set[str] = set()
    validations: set[str] = set()
    target_kinds: set[str] = set()
    labels: set[str] = set()
    for target in resolved:
        target_kinds.add(target.kind)
        labels.add(":".join(
            str(value) for value in (target.kind, target.primary_ref, target.secondary_ref)
            if value is not None
        ))
        if target.kind == "ac_validation":
            criteria.add(target.primary_ref)
            validations.add(target.secondary_ref)
        elif target.kind == "validation_scope":
            scope = scopes.get(target.primary_ref)
            if scope is not None:
                validations.add(scope.validation_id)
        elif target.kind == "validation":
            validations.add(target.primary_ref)
    return criteria, validations, target_kinds, labels


def assess_inspection_review_v2(
        inspection: PlanInspectionV2, findings: tuple[ReviewFindingV2, ...], ratings: Any | None,
        compiled: CompiledPlanInspectionV2, expected: list[dict[str, Any]], *,
        fixed_ac_link_rows: list[dict[str, Any]] | None = None, plan: Any | None = None,
) -> dict[str, Any]:
    """v2 finding의 직접 종류·target와 파생 evidence 결속을 고정 defect에 대조한다."""
    direct_by_code = {item.finding_code: item for item in findings}
    derived_by_code = {item.finding_code: item for item in compiled.derived_findings}
    finding_closures = {item.row_id: item.citation_ids for item in compiled.row_closures
                        if item.row_kind == "finding"}
    citations = {item.citation_id: item for item in compiled.used_citations}

    def matches(code: str, defect: dict[str, Any]) -> bool:
        direct = direct_by_code.get(code)
        derived = derived_by_code.get(code)
        closure = finding_closures.get(code)
        if direct is None or derived is None or closure is None or direct.defect_kind != defect["defect_kind"]:
            return False
        criteria, validations, target_kinds, _labels = _target_values(direct, inspection, compiled)
        primary = {
            "missing_validation_link": "ac_validation", "validation_scope": "validation_scope",
            "missing_task_validation": "constraint_task", "result_order": "validation",
            "insufficient_evidence": "validation_scope", "other": "citation",
        }[defect["defect_kind"]]
        if primary not in target_kinds or validations != set(defect["validation_ids"]):
            return False
        if not set(defect["criterion_ids"]) <= criteria <= set(defect["allowed_criterion_ids"]):
            return False
        if sorted(derived.affected_task_refs) not in [
            sorted(items) for items in defect["allowed_task_ref_sets"]
        ]:
            return False
        if not set(defect["required_evidence_refs"]) <= set(derived.evidence_refs):
            return False
        closure_citations = [citations[ref] for ref in closure if ref in citations]
        return all(any(item.source_ref == anchor["source_ref"] and item.selector in anchor["selectors"]
                       for item in closure_citations) for anchor in defect["required_citations"])

    candidates = {defect["defect_id"]: [code for code in direct_by_code if matches(code, defect)]
                  for defect in expected}
    matched = {defect_id: codes[0] for defect_id, codes in candidates.items() if len(codes) == 1}
    shared = {code for code in matched.values() if list(matched.values()).count(code) > 1}
    matched = {defect_id: code for defect_id, code in matched.items() if code not in shared}
    missing = [defect["defect_id"] for defect in expected if defect["defect_id"] not in matched]
    unexpected = [code for code in direct_by_code if code not in matched.values()]
    exclusive = (bool(findings) and ratings is None) or (not findings and ratings is not None)
    link_assessment = (
        {"applicable": False, "pair_set_matches": None, "requirement_differences": [],
         "link_presence": [], "passed": None}
        if fixed_ac_link_rows is None
        else _fixed_ac_link_requirement_assessment(
            compiled, fixed_ac_link_rows,
            plan if plan is not None else _missing_plan(),
        )
    )
    return {
        "passed": not missing and not unexpected and exclusive and
                  (fixed_ac_link_rows is None or link_assessment["passed"]),
        "required_defects": [defect["defect_id"] for defect in expected],
        "matched_findings": matched, "missing_defects": missing,
        "unexpected_findings": unexpected, "reviewer_detection_complete": not missing,
        "reviewer_precision_ok": not unexpected, "finding_rating_exclusive": exclusive,
        "fixed_ac_link_requirement_assessment": link_assessment,
        "evaluation_contract": V2_EVALUATION_CONTRACT,
    }


def assess_case_inspection_review_v2(
        inspection: PlanInspectionV2, findings: tuple[ReviewFindingV2, ...], ratings: Any | None,
        expectation: dict[str, Any], *, case_id: str, payload: dict[str, Any], plan: Any,
        goal: Any, project_map: Any, evidence_catalog: dict[str, Any],
) -> dict[str, Any]:
    """동결 v1 expectation의 의미 입력 결속을 검증한 뒤 v2 제출물을 평가한다."""
    verify_case_expectation(expectation, case_id=case_id, payload=payload)
    from .plan_inspection_v2 import compile_plan_inspection_v2

    compiled = compile_plan_inspection_v2(
        inspection, findings=findings, plan=plan, goal=goal, project_map=project_map,
        evidence_catalog=evidence_catalog,
        citation_catalog=tuple(
            InspectionCitation.model_validate(item)
            for item in payload["inspection_citation_catalog"]
        ),
        target_catalog=tuple(
            InspectionTargetCatalogEntryV2.model_validate(item)
            for item in payload["inspection_target_catalog"]
        ),
    )
    assessment = assess_inspection_review_v2(
        inspection, findings, ratings, compiled, expectation["expected_defects"],
        fixed_ac_link_rows=expectation["ac_validation_rows"], plan=plan,
    )
    return assessment | {
        "case_id": case_id, "expectation_digest": expectation["expectation_digest"],
        "input_binding": expectation["input_binding"],
        "evaluation_scope": CASE_EVALUATION_SCOPE,
        "v2_contract": V2_EVALUATION_CONTRACT,
    }


def _missing_plan() -> Any:
    raise InspectionExpectationError("AC_LINK_ASSESSMENT_PLAN_MISSING")
