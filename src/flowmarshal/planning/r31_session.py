from __future__ import annotations

from .r31_domain import (
    ArtifactReference,
    PlanningRole,
    SessionHint,
    SessionStrategy,
)


class SessionAdvisor:
    """artifact-only resume를 전제로 reuse/isolate/handoff를 결정한다."""

    def advise(
        self,
        *,
        role: PlanningRole,
        reusable_prefix_digest: str | None = None,
        candidate_id: str | None = None,
        artifact_refs: tuple[ArtifactReference, ...] = (),
        prior_context_reuse_high: bool = False,
        independent_subsystem: bool = False,
        parallelizable: bool = False,
        failure_contamination_risk: bool = False,
        design_invalidated: bool = False,
        compaction_risk: bool = False,
    ) -> SessionHint:
        if role in {
            PlanningRole.INTENT_REVIEWER,
            PlanningRole.HARD_GATE_REVIEWER,
            PlanningRole.CRITICAL_REVIEWER,
        }:
            return SessionHint(
                role=role,
                strategy=SessionStrategy.ISOLATE,
                candidate_id=candidate_id,
                artifact_refs=artifact_refs,
                independent_review_session=True,
                rationale="생성 세션의 숨은 문맥·자기평가·순서 효과를 배제합니다.",
            )
        if design_invalidated or compaction_risk:
            reason = (
                "대규모 설계 변경으로 기존 reasoning 전제가 무효화됐습니다."
                if design_invalidated
                else "긴 세션의 compaction/context loss 위험이 커졌습니다."
            )
            return SessionHint(
                role=role,
                strategy=SessionStrategy.HANDOFF,
                candidate_id=candidate_id,
                artifact_refs=artifact_refs,
                independent_review_session=False,
                rationale=f"{reason} 현재 artifact로 handoff한 뒤 새 세션에서 재개합니다.",
            )
        if independent_subsystem or parallelizable or failure_contamination_risk:
            reasons: list[str] = []
            if independent_subsystem:
                reasons.append("이전 reasoning 재사용이 거의 없는 독립 subsystem")
            if parallelizable:
                reasons.append("artifact 계약으로 병렬 진행 가능한 작업")
            if failure_contamination_risk:
                reasons.append("한 branch 실패가 다른 판단을 오염시킬 위험")
            return SessionHint(
                role=role,
                strategy=SessionStrategy.ISOLATE,
                candidate_id=candidate_id,
                artifact_refs=artifact_refs,
                independent_review_session=False,
                rationale="; ".join(reasons) + "이므로 별도 세션을 사용합니다.",
            )
        return SessionHint(
            role=role,
            strategy=SessionStrategy.REUSE,
            candidate_id=candidate_id,
            reusable_prefix_digest=(
                reusable_prefix_digest if prior_context_reuse_high else None
            ),
            artifact_refs=artifact_refs,
            independent_review_session=False,
            rationale=(
                "동일 Mission의 코드·설계 문맥과 안정된 prompt prefix를 재사용합니다."
                if prior_context_reuse_high
                else "분리 이득보다 artifact 연속성이 커 현재 세션을 재사용합니다."
            ),
        )


__all__ = ["SessionAdvisor"]
