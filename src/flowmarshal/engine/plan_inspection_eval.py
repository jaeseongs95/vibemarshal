"""사전에 고정한 결함별 기대값과 실제 Reviewer 제출물의 대조. 의미 oracle을 수정하지 않는다."""
from __future__ import annotations

from typing import Any

from .planner_roles import PlanReviewEnvelope


def assess_inspection_review(envelope: PlanReviewEnvelope, expected: list[dict[str, Any]]) -> dict[str, Any]:
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
    return {"passed": not missing and not unexpected and exclusive,
            "required_defects": [defect["defect_id"] for defect in expected], "matched_findings": matched,
            "missing_defects": missing, "unexpected_findings": unexpected,
            "reviewer_detection_complete": not missing, "reviewer_precision_ok": not unexpected,
            "finding_rating_exclusive": exclusive}
