from __future__ import annotations

from ..canonical import sha256_bytes, sha256_digest
from ..core.domain import (
    PlanDraft,
    RequirementCoverage,
    RequirementDisposition,
    ValidationDefinition,
    WorkItemDefinition,
)
from .r31_domain import (
    R31_SCORE_POLICY_ID,
    R31_SCORE_POLICY_VERSION,
    ApproachBrief,
    CandidateEnvelope,
    CandidateStatus,
    ConfidenceLevel,
    CriterionValidationBinding,
    FailureRecoveryContract,
    GateFinding,
    GateName,
    IntegrationValidationContract,
    MissionPrimary,
    MissionResolutionHint,
    MissionResolutionHintKind,
    MissionReviewEvidence,
    MissionSelectionReceipt,
    ModelCallReceipt,
    ModelCallStatus,
    PlanContractSidecar,
    PlanOutcomeContract,
    PlanQualityReport,
    PlanVerdict,
    PlanningRole,
    RequirementExtractionDraft,
    RequirementIntentReview,
    RequirementReviewEvidence,
    ScoreDimensionRatings,
    ScoreDimension,
    ScoreDimensionEvidence,
    TieBreakEvidence,
    WorkItemQualityRating,
)
from .r31_pipeline import RoleOutput


_STRATEGIES = (
    (
        "minimal-change",
        "기존 경계 안의 최소 변경 vertical slice",
        "공개 계약을 그대로 유지한다.",
        "작은 diff를 제거해 rollback한다.",
    ),
    (
        "adapter-boundary",
        "adapter와 extension point를 추가하는 additive 변경",
        "기존 소비자는 adapter 뒤의 기존 계약을 계속 사용한다.",
        "새 adapter routing을 끄고 기존 경계로 복귀한다.",
    ),
    (
        "staged-rollout",
        "새 경계를 단계적으로 도입하고 검증 checkpoint를 둔다.",
        "중간 단계마다 이전·새 소비자를 함께 지원한다.",
        "checkpoint별 상태를 확인하고 직전 단계로 복귀한다.",
    ),
)


def _fixture_model_call_receipt(
    value,
    *,
    role: PlanningRole,
    label: str,
    effort: str,
) -> ModelCallReceipt:
    """실제 model qualification으로 오인하지 않는 deterministic fixture receipt."""

    return ModelCallReceipt(
        call_id=f"model_call_fixture_{label}",
        role=role,
        model_id="fixture-model-not-live",
        reasoning_effort=effort,
        inventory_digest=sha256_bytes(b"fixture-model-inventory-not-live"),
        input_digest=sha256_digest({"fixture_call": label}),
        output_schema_digest=sha256_digest({"fixture_schema": label}),
        output_digest=sha256_digest(value),
        status=ModelCallStatus.SUCCEEDED,
        thread_id=f"thread_fixture_{label}",
        turn_ids=(f"turn_fixture_{label}",),
    )


def fixture_mission_review_evidence(
    mission_selection: MissionSelectionReceipt,
    *,
    proposal: MissionResolutionHint | None = None,
    reviewed_proposal: MissionResolutionHint | None = None,
) -> MissionReviewEvidence:
    """model 없는 테스트·smoke에서만 사용하는 Mission review fixture."""

    mission = mission_selection.mission
    if mission is None:
        raise ValueError("fixture Mission evidence에는 resolved Mission이 필요합니다.")
    proposal = proposal or MissionResolutionHint(
        kind=MissionResolutionHintKind.UNAMBIGUOUS,
        recommended=mission,
    )
    reviewed_proposal = reviewed_proposal or proposal
    return MissionReviewEvidence(
        proposal=proposal,
        reviewed_proposal=reviewed_proposal,
        resolved_selection=mission_selection,
        proposer_receipt=_fixture_model_call_receipt(
            proposal,
            role=PlanningRole.PURPOSE_RESOLVER,
            label="mission_proposer",
            effort="medium",
        ),
        reviewer_receipt=_fixture_model_call_receipt(
            reviewed_proposal,
            role=PlanningRole.INTENT_REVIEWER,
            label="mission_reviewer",
            effort="high",
        ),
    )


def fixture_requirement_review_evidence(
    draft: RequirementExtractionDraft,
    review: RequirementIntentReview,
) -> RequirementReviewEvidence:
    """model 없는 테스트·smoke에서만 사용하는 requirement review fixture."""

    return RequirementReviewEvidence(
        intent_review=review,
        extractor_receipt=_fixture_model_call_receipt(
            draft,
            role=PlanningRole.PURPOSE_RESOLVER,
            label="requirement_extractor",
            effort="medium",
        ),
        reviewer_receipt=_fixture_model_call_receipt(
            review,
            role=PlanningRole.INTENT_REVIEWER,
            label="requirement_reviewer",
            effort="high",
        ),
    )


class FixtureApproachGenerator:
    """모델 없는 pipeline·CLI 검증용 결정적 adapter."""

    def generate(self, planning_run, *, limit: int):
        mission = planning_run.planning_input.mission_selection.mission
        assert mission is not None
        if mission.primary is MissionPrimary.ANALYSIS_AUDIT:
            strategies = (
                (
                    "evidence-first",
                    "권위 source를 수집하고 재현 가능한 분석 artifact를 만든다.",
                    "제품 상태를 변경하지 않는다.",
                    "분석 artifact만 폐기하면 된다.",
                ),
            )
        elif (
            mission.primary is MissionPrimary.BUGFIX_STABILIZATION
            and not planning_run.planning_input.input_artifact_refs
        ):
            strategies = (
                (
                    "causal-diagnosis",
                    "재현 절차와 원인 evidence를 만드는 진단 WorkItem만 계획한다.",
                    "원인 확정 전 제품 코드는 변경하지 않는다.",
                    "진단 artifact만 폐기하면 된다.",
                ),
            )
        else:
            strategies = _STRATEGIES
        approaches = tuple(
            ApproachBrief(
                approach_id=f"approach.{strategy}",
                mission_primary=mission.primary,
                strategy_family=strategy,
                change_shape=shape,
                compatibility=compatibility,
                rollout_recovery=recovery,
                rationale="동일 Mission을 다른 변경·복구 전략으로 충족하는 fixture 후보입니다.",
                risk_tags=mission.risk_tags,
            )
            for strategy, shape, compatibility, recovery in strategies[:limit]
        )
        return RoleOutput(approaches)


class FixtureCandidateExpander:
    def expand(self, planning_run, approach: ApproachBrief):
        source = planning_run.planning_input.request_spec
        validation = source.available_validations[0]
        safe_strategy = approach.strategy_family.replace("-", "_")
        work_ref = f"slice.{safe_strategy}"
        criterion_id = f"validate.{safe_strategy}"
        work = WorkItemDefinition(
            client_ref=work_ref,
            title=f"{approach.strategy_family} 계획 slice",
            objective=(
                f"{approach.change_shape}를 구현 또는 산출하고 독립 검증 가능한 결과로 닫는다."
            ),
            context_sources=tuple(item.source_id for item in source.context_sources),
            expected_changes=(f"artifacts/{safe_strategy}",),
            out_of_scope=source.out_of_scope,
            deliverables=(f"{approach.strategy_family} 결과 artifact",),
            acceptance_criteria=(
                f"{source.request_summary}의 관찰 가능한 결과를 확인할 수 있다.",
            ),
            validations=(
                ValidationDefinition(
                    criterion_id=criterion_id,
                    check_type=validation.check_type,
                    capability_id=validation.capability_id,
                ),
            ),
        )
        plan = PlanDraft(
            project_id=source.project_id,
            request_summary=source.request_summary,
            request_spec_digest=source.canonical_digest,
            work_items=(work,),
            requirement_coverage=tuple(
                RequirementCoverage(
                    requirement_id=requirement.requirement_id,
                    disposition=RequirementDisposition.WORK_ITEMS,
                    work_item_refs=(work_ref,),
                    rationale="기능적으로 응집된 하나의 검증 가능한 slice에서 처리합니다.",
                )
                for requirement in source.requirements
            ),
        )
        contract = PlanContractSidecar(
            plan_digest=plan.canonical_digest,
            plan_outcome=PlanOutcomeContract(
                observable_outcome=(
                    planning_run.planning_input.mission_selection.mission.observable_outcome
                ),
                acceptance_criteria=(
                    f"{source.request_summary} 결과와 validation evidence가 함께 존재한다.",
                ),
                excluded_outcomes=source.out_of_scope,
            ),
            integration_validations=(
                IntegrationValidationContract(
                    validation_id=f"integration.{safe_strategy}",
                    work_item_refs=(work_ref,),
                    check_type=validation.check_type,
                    specification=validation.configuration,
                    required_evidence=("검증 명령의 종료 상태 또는 동등한 기계 판정",),
                ),
            ),
            criterion_bindings=(
                CriterionValidationBinding(
                    work_item_ref=work_ref,
                    criterion_id=criterion_id,
                    check_type=validation.check_type,
                    capability_id=validation.capability_id,
                    specification_digest=sha256_digest({}),
                    required_evidence=("validation 실행 결과",),
                ),
            ),
            failure_recovery_contracts=(
                FailureRecoveryContract(
                    work_item_ref=work_ref,
                    failure_detection="validation과 예상 diff/artifact를 함께 검사한다.",
                    idempotency_strategy="같은 input digest와 Attempt에서는 같은 결과만 만든다.",
                    partial_execution_strategy="부분 artifact를 식별하고 검증된 checkpoint부터 재개한다.",
                    duplicate_dispatch_strategy="run·candidate·Attempt binding으로 중복 실행을 막는다.",
                    retry_policy="수정 가능한 동일 범위 실패만 새 Attempt로 제한 재시도한다.",
                    rollback_strategy=approach.rollout_recovery,
                ),
            ),
        )
        suffix = approach.strategy_family.replace("-", ".")
        candidate = CandidateEnvelope(
            candidate_id=f"candidate.{suffix}",
            version=1,
            refinement_round=0,
            status=CandidateStatus.GENERATED,
            planning_input_digest=planning_run.planning_input_digest,
            mission_resolution_digest=(
                planning_run.planning_input.mission_selection.mission_resolution_digest
            ),
            mission_primary=approach.mission_primary,
            approach=approach,
            plan=plan,
            contract=contract,
            policy_id=R31_SCORE_POLICY_ID,
            policy_version=R31_SCORE_POLICY_VERSION,
        )
        return RoleOutput(candidate)


class FixtureHardGateReviewer:
    """정적 clean fixture 전용 reviewer이며 실제 의미 검토 성공을 주장하지 않는다."""

    def review(self, planning_run, candidate: CandidateEnvelope):
        strategy_bonus = {
            "minimal-change": (4, 3, 3, 3, 4),
            "adapter-boundary": (4, 4, 4, 4, 3),
            "staged-rollout": (4, 4, 4, 3, 2),
            "evidence-first": (4, 4, 3, 4, 4),
            "causal-diagnosis": (4, 4, 4, 4, 3),
        }[candidate.approach.strategy_family]
        ratings = ScoreDimensionRatings(
            goal_fit_change_safety=strategy_bonus[0],
            verification_evidence_strength=strategy_bonus[1],
            execution_risk_control=strategy_bonus[2],
            maintainability_reproducibility=strategy_bonus[3],
            resource_efficiency=strategy_bonus[4],
        )
        work_quality = tuple(
            WorkItemQualityRating(
                work_item_ref=item.client_ref,
                self_containment=2,
                functional_cohesion=2,
                acceptance_validation=2,
                interface_clarity=2,
                failure_retry=2,
            )
            for item in candidate.plan.work_items
        )
        weakest_ref = min(item.work_item_ref for item in work_quality)
        report = PlanQualityReport(
            planning_input_digest=candidate.planning_input_digest,
            plan_digest=candidate.plan.canonical_digest,
            plan_verdict=PlanVerdict.PASS,
            gate_findings=tuple(
                GateFinding(
                    gate=gate,
                    plan_verdict=PlanVerdict.PASS,
                    summary=f"정적 clean fixture의 {gate.value} 계약을 충족합니다.",
                )
                for gate in GateName
            ),
            dimension_ratings=ratings,
            dimension_evidence=tuple(
                ScoreDimensionEvidence(
                    dimension=dimension,
                    rating=getattr(ratings, dimension.value),
                    rationale=f"정적 clean fixture의 {dimension.value} 계약 근거입니다.",
                    evidence_refs=(candidate.contract.contract_digest,),
                    sensitivity="프로젝트 상태나 계약이 바뀌면 실제 reviewer가 재평가해야 합니다.",
                )
                for dimension in ScoreDimension
            ),
            fitness_score=ratings.calculated_score,
            work_item_quality=work_quality,
            weakest_work_item_ref=weakest_ref,
            weakest_work_item_rating=2,
            confidence=ConfidenceLevel.HIGH,
            tie_break_evidence=TieBreakEvidence(
                reversibility=4,
                public_contract_change=False,
                change_surface=len(candidate.plan.work_items),
                cost={
                    "minimal-change": 1,
                    "adapter-boundary": 2,
                    "staged-rollout": 3,
                    "evidence-first": 1,
                    "causal-diagnosis": 1,
                }[candidate.approach.strategy_family],
                mission_metrics=_mission_metrics(candidate.mission_primary),
            ),
        )
        reviewed = CandidateEnvelope.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "status": CandidateStatus.ADMISSIBLE,
                "quality_report": report,
            }
        )
        return RoleOutput(reviewed)


class FixtureCandidateRefiner:
    def refine(self, planning_run, candidate: CandidateEnvelope):
        child = CandidateEnvelope.model_validate(
            {
                **candidate.model_dump(mode="python"),
                "candidate_id": f"{candidate.candidate_id}.r1",
                "parent_candidate_id": candidate.candidate_id,
                "version": candidate.version + 1,
                "refinement_round": 1,
                "status": CandidateStatus.GENERATED,
                "quality_report": None,
            }
        )
        return RoleOutput(child)


class FixtureTopKWalkthrough:
    def walkthrough(self, planning_run, candidates):
        # 실제 의미 검토 대체물이 아니라 pipeline/receipt 결속을 검증하는 fixture다.
        return RoleOutput(candidates)


def _mission_metrics(primary: MissionPrimary) -> dict[str, int]:
    return {
        MissionPrimary.NEW_BUILD: {"usable_vertical_slice": 4, "irreversible_foundation_decisions": 0},
        MissionPrimary.FEATURE_EXTENSION: {"existing_behavior_compatibility": 4, "consumer_compatibility": 4},
        MissionPrimary.LEGACY_REFACTOR: {"behavior_equivalence": 4, "incrementality": 4, "blast_radius": 1},
        MissionPrimary.BUGFIX_STABILIZATION: {"causal_evidence": 4, "reproducibility": 4, "causal_change": 4},
        MissionPrimary.MIGRATION_MODERNIZATION: {"data_preservation": 4, "recoverability": 4, "intermediate_compatibility": 4, "rerunnability": 4},
        MissionPrimary.ANALYSIS_AUDIT: {"evidence_coverage": 4, "reproducibility": 4, "source_authority": 4},
    }[primary]


__all__ = [
    "FixtureApproachGenerator",
    "FixtureCandidateExpander",
    "FixtureCandidateRefiner",
    "FixtureHardGateReviewer",
    "FixtureTopKWalkthrough",
    "fixture_mission_review_evidence",
    "fixture_requirement_review_evidence",
]
