"""사전에 고정한 결함별 기대값과 실제 Reviewer 제출물의 대조. 의미 oracle을 수정하지 않는다."""
from __future__ import annotations

from typing import Any

from flowmarshal.canonical import sha256_bytes, sha256_digest
from .planner_roles import PlanReviewEnvelope
from .plan_inspection import PlanInspection


class InspectionExpectationError(ValueError):
    """사전 검토한 사례와 실제 의미 입력의 결속 실패."""


CASE_EVALUATION_SCOPE = {
    "ac_validation_relations": "complete_matrix",
    "independent_defects": "fixed_direct_evidence",
    "constraint_semantics": "not_scored",
    "mechanism_semantics": "fixed_defect_anchors_only",
}


def inspection_input_binding(payload: dict[str, Any]) -> dict[str, str]:
    """ID만 아닌 실제 제공 Goal·Plan·등록 근거 전체를 평가 입력에 결속한다.

    Plan 전체에는 validation 원문·소유 Task/Goal·method·mode·evidence와 현재
    연결이 포함된다. source catalog 본문이 없으면 부재 자체를 결속하며 능력을 추정하지 않는다.
    """
    catalog = payload["evidence_catalog"]
    sources = payload["inspection_source_catalog"]
    registered = {ref: value for ref, value in sources.items() if ref.startswith("project:")}
    for ref, value in registered.items():
        if not isinstance(value, dict) or value.get("source_ref") != ref:
            raise InspectionExpectationError("REGISTERED_SOURCE_BINDING_INVALID")
        if "content" in value and sha256_bytes(value["content"].encode("utf-8")) != value.get("content_digest"):
            raise InspectionExpectationError("REGISTERED_SOURCE_CONTENT_DIGEST_MISMATCH")
    parts = {
        "goal_contract_digest": catalog["source:goal"],
        "plan_contract_digest": catalog["artifact:plan_contract"],
        "project_map_digest": catalog["source:project_map"],
        "registered_sources_digest": registered,
        "validation_projection_digest": payload["validation_scope_rows"],
        "comparison_targets_digest": payload["validation_comparison_targets"],
    }
    result = {key: sha256_digest(value) for key, value in parts.items()}
    return result | {"input_digest": sha256_digest(result)}


def bind_case_expectation(
    *, case_id: str, payload: dict[str, Any], rows: list[dict[str, Any]],
    defects: list[dict[str, Any]], review_digest: str,
) -> dict[str, Any]:
    """호출 전에 독립 검토표를 해당 사례 입력에만 결속한다. 의미 정답은 생성하지 않는다."""
    pairs = [(row["criterion_id"], row["validation_id"]) for row in rows]
    targets = {(row["criterion_id"], row["validation_id"])
               for row in payload["validation_comparison_targets"]["ac_validation_pairs"]}
    if not rows or len(pairs) != len(set(pairs)) or set(pairs) != targets:
        raise InspectionExpectationError("CASE_RELATION_TABLE_MISSING_OR_INCOMPLETE")
    if any(row["relation"] not in {"explicit_procedure", "global_constraint_only", "optional_or_unrelated"}
           for row in rows):
        raise InspectionExpectationError("CASE_RELATION_INVALID")
    if not case_id or not review_digest.startswith("sha256:"):
        raise InspectionExpectationError("INDEPENDENT_REVIEW_BINDING_MISSING")
    body = {"case_id": case_id, "input_binding": inspection_input_binding(payload),
            "evaluation_scope": dict(CASE_EVALUATION_SCOPE), "ac_validation_rows": rows,
            "expected_defects": defects, "independent_review_digest": review_digest}
    return body | {"expectation_digest": sha256_digest(body)}


def verify_case_expectation(
    expectation: dict[str, Any], *, case_id: str, payload: dict[str, Any],
) -> None:
    body = {key: value for key, value in expectation.items() if key != "expectation_digest"}
    if expectation.get("expectation_digest") != sha256_digest(body):
        raise InspectionExpectationError("CASE_EXPECTATION_DIGEST_MISMATCH")
    if expectation.get("case_id") != case_id or expectation.get("input_binding") != inspection_input_binding(payload):
        raise InspectionExpectationError("CASE_SEMANTIC_INPUT_MISMATCH")
    if expectation.get("evaluation_scope") != CASE_EVALUATION_SCOPE:
        raise InspectionExpectationError("CASE_EVALUATION_SCOPE_MISMATCH")
    rebuilt = bind_case_expectation(case_id=case_id, payload=payload,
                                   rows=expectation["ac_validation_rows"], defects=expectation["expected_defects"],
                                   review_digest=expectation["independent_review_digest"])
    if rebuilt != expectation:
        raise InspectionExpectationError("CASE_EXPECTATION_CONTRACT_MISMATCH")


def assess_case_inspection_review(
    envelope: PlanReviewEnvelope, expectation: dict[str, Any], *, case_id: str, payload: dict[str, Any],
) -> dict[str, Any]:
    verify_case_expectation(expectation, case_id=case_id, payload=payload)
    assessment = assess_inspection_review(envelope, expectation["expected_defects"],
                                         fixed_ac_validation_rows=expectation["ac_validation_rows"])
    return assessment | {"case_id": case_id, "expectation_digest": expectation["expectation_digest"],
                         "input_binding": expectation["input_binding"],
                         "evaluation_scope": expectation["evaluation_scope"]}


def assess_fixed_ac_validation_relations(
    inspection: PlanInspection, expected_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """사전 관계표와 실제 제출의 relation만 대조한다.

    실제 진단은 assess_case_inspection_review의 입력 결속을 거친다. 이 저수준 함수는
    과거 원시 제출 회귀에도 쓰며 의미 정답을 adapter나 Plan에 주입하지 않는다.
    """
    expected = {
        (row["criterion_id"], row["validation_id"]): row["relation"]
        for row in expected_rows
    }
    actual = {
        (row.criterion_id, row.validation_id): row.relation
        for row in inspection.ac_validation_rows
    }
    pair_set_matches = (bool(expected_rows) and set(actual) == set(expected)
                        and len(actual) == len(inspection.ac_validation_rows) and len(expected) == len(expected_rows))
    differences = [
        {
            "criterion_id": criterion_id,
            "validation_id": validation_id,
            "expected": expected[(criterion_id, validation_id)],
            "actual": actual[(criterion_id, validation_id)],
        }
        for criterion_id, validation_id in sorted(set(actual) & set(expected))
        if actual[(criterion_id, validation_id)] != expected[(criterion_id, validation_id)]
    ]
    return {
        "applicable": True,
        "pair_set_matches": pair_set_matches,
        "relation_differences": differences,
        "passed": pair_set_matches and not differences,
    }


def assess_inspection_review(
    envelope: PlanReviewEnvelope,
    expected: list[dict[str, Any]],
    *,
    fixed_ac_validation_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """finding의 존재 여부가 아닌 독립 결함의 ID·직접 근거·실제 finding 결속을 검사한다.

    expected는 호출 전 고정하는 외부 평가 입력이다. 대조표의 의미 분류를 정답으로 채택하지 않는다.
    adapter의 selector/인용/집합 검증을 통과한 envelope를 받으며 Core 판정을 생성하지 않는다.
    """
    findings = {finding.finding_code: finding for finding in envelope.review.findings}
    citations = {citation.citation_id: citation for citation in envelope.inspection.citations}
    links = {link.finding_code: link for link in envelope.inspection.finding_links}

    def matches(code: str, defect: dict[str, Any]) -> bool:
        if code not in links:
            return False
        finding, link = findings[code], links[code]
        if link.defect_kind != defect["defect_kind"]:
            return False
        if set(link.validation_ids) != set(defect["validation_ids"]):
            return False
        if not set(defect["criterion_ids"]) <= set(link.criterion_ids) <= set(defect["allowed_criterion_ids"]):
            return False
        if sorted(finding.affected_task_refs) not in [sorted(items) for items in defect["allowed_task_ref_sets"]]:
            return False
        if set(link.task_refs) != set(finding.affected_task_refs):
            return False
        if not set(defect["required_evidence_refs"]) <= set(finding.evidence_refs):
            return False
        if not all(anchor in finding.summary for anchor in (*defect["criterion_ids"], *defect["validation_ids"])):
            return False
        basis = [citations[ref] for ref in link.basis_refs if ref in citations]
        if not all(any(citation.source_ref == selector["source_ref"] and
                       citation.selector in selector["selectors"] for citation in basis)
                   for selector in defect["required_citations"]):
            return False
        # gate·severity·remediable의 Core 분류와 결함별 탐지 결속은 별도 책임이다.
        return True

    candidates = {defect["defect_id"]: [code for code in findings if matches(code, defect)] for defect in expected}
    matched = {key: codes[0] for key, codes in candidates.items() if len(codes) == 1}
    # 독립 결함 둘을 같은 finding 하나로 통과시키지 않는다.
    shared = {code for code in matched.values() if list(matched.values()).count(code) > 1}
    matched = {key: code for key, code in matched.items() if code not in shared}
    missing = [defect["defect_id"] for defect in expected if defect["defect_id"] not in matched]
    unexpected = [code for code in findings if code not in matched.values()]
    exclusive = (bool(findings) and envelope.review.ratings is None) or (not findings and envelope.review.ratings is not None)
    relation_assessment = (
        {"applicable": False, "pair_set_matches": None, "relation_differences": [], "passed": None}
        if fixed_ac_validation_rows is None
        else assess_fixed_ac_validation_relations(envelope.inspection, fixed_ac_validation_rows)
    )
    return {"passed": not missing and not unexpected and exclusive and
                      (fixed_ac_validation_rows is None or relation_assessment["passed"]),
            "required_defects": [defect["defect_id"] for defect in expected], "matched_findings": matched,
            "missing_defects": missing, "unexpected_findings": unexpected,
            "reviewer_detection_complete": not missing, "reviewer_precision_ok": not unexpected,
            "finding_rating_exclusive": exclusive, "fixed_ac_validation_relation_assessment": relation_assessment}
