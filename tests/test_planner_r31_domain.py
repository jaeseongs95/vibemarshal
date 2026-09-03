from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.core.domain import (
    PlanDraft,
    RequirementCoverage,
    RequirementDisposition,
    ValidationDefinition,
    WorkItemDefinition,
)
from flowmarshal.planning.domain import (
    ContextSourceKind,
    ContextSourceSpec,
    RequestSpec,
    RequirementPriority,
    RequirementSource,
    RequirementSpec,
    ValidationCapability,
)
from flowmarshal.planning.r31_domain import (
    ApproachBrief,
    BehaviorPreservation,
    CandidateEnvelope,
    CandidateObservationStatus,
    CandidateStatus,
    CompatibilityPolicy,
    ConfidenceLevel,
    CriterionValidationBinding,
    Criticality,
    DependencyContract,
    EffectivePlanningPolicy,
    FailureRecoveryContract,
    FindingSeverity,
    GateDiagnostic,
    GateFinding,
    GateName,
    IntegrationValidationContract,
    LifecycleStage,
    MissionPrimary,
    MissionResolutionHint,
    MissionResolutionHintKind,
    MissionResolutionStatus,
    MissionReviewEvidence,
    MissionSelectedBy,
    MissionSelectionReceipt,
    ModelCallReceipt,
    ModelCallStatus,
    MutationPolicy,
    PlanContractSidecar,
    PlanningMissionDefinition,
    PlanningRole,
    PlanningRunInput,
    PlanningRunReceipt,
    PlanningRunStatus,
    PlanningSearchOutcome,
    PlanOutcomeContract,
    PlanQualityReport,
    PlanVerdict,
    ProfileFreshness,
    ProfileRevisionStatus,
    ProfileSection,
    ProfileSectionName,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    ProjectStateEntry,
    ProjectStateSnapshot,
    RawRequestTrace,
    RequirementExtractionDraft,
    RequirementExtractionReceipt,
    RequirementIntentReview,
    RequirementReviewEvidence,
    IntentReviewVerdict,
    RequirementKind,
    ExtractedPlanningRequirement,
    RiskTolerance,
    ScoreDimension,
    ScoreDimensionEvidence,
    ScoreDimensionRatings,
    SearchOutcomeStatus,
    SelectionReceipt,
    SelectionSource,
    SessionHint,
    SessionStrategy,
    SnapshotEntryKind,
    TieBreakEvidence,
    WorkItemQualityRating,
)


PROJECT_ID = "project_11111111111111111111111111111111"
NOW = datetime(2026, 9, 2, 1, 2, 3, tzinfo=timezone.utc)


def request_spec() -> RequestSpec:
    context = ContextSourceSpec.from_text(
        source_id="project-agents",
        kind=ContextSourceKind.PROJECT_INSTRUCTIONS,
        path="D:/work/AGENTS.md",
        purpose="프로젝트 지침",
        content="지침",
        required_for_all_work_items=True,
    )
    return RequestSpec(
        project_id=PROJECT_ID,
        project_name="R3.1 테스트",
        project_root="D:/work",
        project_description="목적 기반 계획 테스트",
        user_request="기존 동작을 보존하며 기능을 추가하고 검증해줘.",
        request_summary="호환 기능 확장",
        requirements=(
            RequirementSpec(
                requirement_id="req.feature",
                statement="기존 동작을 보존하며 기능을 추가한다.",
                source=RequirementSource.USER,
                priority=RequirementPriority.MUST,
            ),
        ),
        context_sources=(context,),
        available_validations=(
            ValidationCapability(
                capability_id="python-tests",
                check_type="command",
                description="Python 테스트 실행",
            ),
        ),
    )


def profile_definition() -> ProjectProfileDefinition:
    section = ProfileSection(
        source_refs=("project-agents",),
        source_digest=sha256_bytes(b"profile"),
        freshness=ProfileFreshness.CURRENT,
    )
    return ProjectProfileDefinition(
        product_goal="기존 사용자를 해치지 않고 기능을 확장한다.",
        lifecycle_stage=LifecycleStage.MATURE,
        criticality=Criticality.STANDARD,
        compatibility_policy=CompatibilityPolicy.PRESERVE,
        default_risk_tolerance=RiskTolerance.BALANCED,
        architecture=section,
        validation=section,
        runtime=section,
        risk=section,
        compatibility=section,
    )


def profile_revision(*, created_at: datetime = NOW) -> ProjectProfileRevision:
    definition = profile_definition()
    return ProjectProfileRevision(
        profile_revision_id="profile_revision_11111111111111111111111111111111",
        project_id=PROJECT_ID,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=ProfileRevisionStatus.ACTIVE,
        created_at=created_at,
    )


def mission(
    primary: MissionPrimary = MissionPrimary.FEATURE_EXTENSION,
) -> PlanningMissionDefinition:
    return PlanningMissionDefinition(
        primary=primary,
        observable_outcome="호환되는 기능과 실행 가능한 회귀 검사를 제공한다.",
        mutation_policy=(
            MutationPolicy.READ_ONLY
            if primary is MissionPrimary.ANALYSIS_AUDIT
            else MutationPolicy.SCOPED_CHANGE
        ),
        behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
        forbidden_scopes=("공개 API 파괴",),
        selected_by=MissionSelectedBy.INFERRED,
        confidence=ConfidenceLevel.HIGH,
        mission_policy_id="mission-policy",
        mission_policy_version="v1",
    )


def planning_input(
    *,
    primary: MissionPrimary = MissionPrimary.FEATURE_EXTENSION,
    created_at: datetime = NOW,
) -> PlanningRunInput:
    request = request_spec()
    profile = profile_revision(created_at=created_at)
    selected_mission = mission(primary)
    mission_receipt = MissionSelectionReceipt(
        request_spec_digest=request.canonical_digest,
        raw_request_digest=sha256_bytes(request.user_request.encode("utf-8")),
        profile_revision_id=profile.profile_revision_id,
        profile_definition_digest=profile.definition_digest,
        status=MissionResolutionStatus.RESOLVED,
        mission=selected_mission,
    )
    mission_hint = MissionResolutionHint(
        kind=MissionResolutionHintKind.UNAMBIGUOUS,
        recommended=selected_mission,
    )
    mission_review_evidence = MissionReviewEvidence(
        proposal=mission_hint,
        reviewed_proposal=mission_hint,
        resolved_selection=mission_receipt,
        proposer_receipt=ModelCallReceipt(
            call_id="model_call_fixture_mission_proposer",
            role=PlanningRole.PURPOSE_RESOLVER,
            model_id="fixture-model",
            reasoning_effort="medium",
            inventory_digest=sha256_bytes(b"fixture-inventory"),
            input_digest=sha256_bytes(b"fixture-mission-proposer-input"),
            output_schema_digest=sha256_bytes(b"fixture-mission-schema"),
            output_digest=sha256_digest(mission_hint),
            status=ModelCallStatus.SUCCEEDED,
            thread_id="thread_fixture_mission_proposer",
            turn_ids=("turn_fixture_mission_proposer",),
        ),
        reviewer_receipt=ModelCallReceipt(
            call_id="model_call_fixture_mission_reviewer",
            role=PlanningRole.INTENT_REVIEWER,
            model_id="fixture-model",
            reasoning_effort="high",
            inventory_digest=sha256_bytes(b"fixture-inventory"),
            input_digest=sha256_bytes(b"fixture-mission-reviewer-input"),
            output_schema_digest=sha256_bytes(b"fixture-mission-schema"),
            output_digest=sha256_digest(mission_hint),
            status=ModelCallStatus.SUCCEEDED,
            thread_id="thread_fixture_mission_reviewer",
            turn_ids=("turn_fixture_mission_reviewer",),
        ),
    )
    policy = EffectivePlanningPolicy(
        policy_id="effective-policy",
        policy_version="v1",
        profile_definition_digest=profile.definition_digest,
        mission_resolution_digest=mission_receipt.mission_resolution_digest,
        lifecycle_stage=profile.definition.lifecycle_stage,
        criticality=profile.definition.criticality,
        compatibility_policy=profile.definition.compatibility_policy,
        risk_tolerance=profile.definition.default_risk_tolerance,
        mutation_policy=selected_mission.mutation_policy,
        behavior_preservation=selected_mission.behavior_preservation,
        forbidden_scopes=selected_mission.forbidden_scopes,
    )
    snapshot = ProjectStateSnapshot(
        snapshot_id="snapshot_11111111111111111111111111111111",
        project_id=PROJECT_ID,
        mission_resolution_digest=mission_receipt.mission_resolution_digest,
        entries=(
            ProjectStateEntry(
                entry_id="project-agents",
                kind=SnapshotEntryKind.REFERENCE,
                path=request.context_sources[0].path,
                content_digest=request.context_sources[0].content_digest,
                relevance="프로젝트 지침",
            ),
            ProjectStateEntry(
                entry_id="core-file",
                kind=SnapshotEntryKind.FILE,
                path="src/core.py",
                content_digest=sha256_bytes(b"core"),
                relevance="기능 확장 대상",
            ),
        ),
        captured_at=created_at,
    )
    trace = RawRequestTrace(
        trace_id="request-feature",
        start_offset=0,
        end_offset=len(request.user_request),
        excerpt=request.user_request,
    )
    extracted_requirements = (
        ExtractedPlanningRequirement(
            requirement_id="req.feature",
            kind=RequirementKind.REQUIREMENT,
            statement=request.requirements[0].statement,
            mandatory=True,
            trace_refs=(trace.trace_id,),
        ),
        ExtractedPlanningRequirement(
            requirement_id="mission.outcome",
            kind=RequirementKind.OBSERVABLE_OUTCOME,
            statement=selected_mission.observable_outcome,
            mandatory=True,
            trace_refs=(trace.trace_id,),
        ),
    )
    extraction_draft = RequirementExtractionDraft(
        raw_request_digest=sha256_bytes(request.user_request.encode("utf-8")),
        mission_digest=selected_mission.mission_digest,
        profile_definition_digest=profile.definition_digest,
        project_snapshot_digest=snapshot.snapshot_digest,
        request_summary=request.request_summary,
        traces=(trace,),
        requirements=extracted_requirements,
    )
    analysis_context_digest = sha256_bytes(b"fixture-requirement-analysis-context")
    intent_review = RequirementIntentReview(
        analysis_context_digest=analysis_context_digest,
        extraction_draft_digest=extraction_draft.draft_digest,
        verdict=IntentReviewVerdict.PASS,
    )
    extraction = RequirementExtractionReceipt(
        analysis_context_digest=analysis_context_digest,
        raw_request_digest=sha256_bytes(request.user_request.encode("utf-8")),
        request_spec_digest=request.canonical_digest,
        mission_resolution_digest=mission_receipt.mission_resolution_digest,
        project_snapshot_digest=snapshot.snapshot_digest,
        traces=(trace,),
        requirements=extracted_requirements,
        profile_definition_digest=profile.definition_digest,
        source_project_snapshot_digest=snapshot.snapshot_digest,
        extraction_draft_digest=extraction_draft.draft_digest,
        intent_review_digest=intent_review.review_digest,
        selected_profile_sections=(ProfileSectionName.ARCHITECTURE,),
    )
    requirement_review_evidence = RequirementReviewEvidence(
        intent_review=intent_review,
        extractor_receipt=ModelCallReceipt(
            call_id="model_call_fixture_requirement_extractor",
            role=PlanningRole.PURPOSE_RESOLVER,
            model_id="fixture-model",
            reasoning_effort="medium",
            inventory_digest=sha256_bytes(b"fixture-inventory"),
            input_digest=sha256_bytes(b"fixture-requirement-extractor-input"),
            output_schema_digest=sha256_bytes(b"fixture-requirement-schema"),
            output_digest=extraction_draft.draft_digest,
            status=ModelCallStatus.SUCCEEDED,
            thread_id="thread_fixture_requirement_extractor",
            turn_ids=("turn_fixture_requirement_extractor",),
        ),
        reviewer_receipt=ModelCallReceipt(
            call_id="model_call_fixture_requirement_reviewer",
            role=PlanningRole.INTENT_REVIEWER,
            model_id="fixture-model",
            reasoning_effort="high",
            inventory_digest=sha256_bytes(b"fixture-inventory"),
            input_digest=sha256_bytes(b"fixture-requirement-reviewer-input"),
            output_schema_digest=sha256_bytes(b"fixture-review-schema"),
            output_digest=intent_review.review_digest,
            status=ModelCallStatus.SUCCEEDED,
            thread_id="thread_fixture_requirement_reviewer",
            turn_ids=("turn_fixture_requirement_reviewer",),
        ),
    )
    return PlanningRunInput(
        request_spec=request,
        profile_revision=profile,
        mission_selection=mission_receipt,
        mission_review_evidence=mission_review_evidence,
        effective_policy=policy,
        requirement_extraction=extraction,
        requirement_review_evidence=requirement_review_evidence,
        project_snapshot=snapshot,
    )


def plan_draft(request: RequestSpec | None = None) -> PlanDraft:
    request = request or request_spec()
    foundation = WorkItemDefinition(
        client_ref="foundation",
        title="호환 기능 구현",
        objective="기존 계약을 보존하는 additive 기능 경계를 구현한다.",
        context_sources=("project-agents",),
        expected_changes=("src/core.py",),
        deliverables=("호환 기능 구현",),
        acceptance_criteria=("기존 계약과 새 기능이 함께 동작한다.",),
        validations=(
            ValidationDefinition(
                criterion_id="foundation-tests",
                check_type="command",
                capability_id="python-tests",
            ),
        ),
    )
    integration = WorkItemDefinition(
        client_ref="integration",
        title="통합 회귀 검증",
        objective="기존 소비자와 새 기능의 통합 동작을 회귀 검증한다.",
        dependencies=("foundation",),
        context_sources=("project-agents",),
        expected_changes=("tests/test_core.py",),
        deliverables=("회귀 검사",),
        acceptance_criteria=("통합 검사를 실행할 수 있다.",),
        validations=(
            ValidationDefinition(
                criterion_id="integration-tests",
                check_type="command",
                capability_id="python-tests",
            ),
        ),
    )
    return PlanDraft(
        project_id=PROJECT_ID,
        request_summary=request.request_summary,
        request_spec_digest=request.canonical_digest,
        work_items=(foundation, integration),
        requirement_coverage=(
            RequirementCoverage(
                requirement_id="req.feature",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("foundation", "integration"),
                rationale="구현과 통합 검사가 요구를 충족한다.",
            ),
        ),
    )


def contract(plan: PlanDraft) -> PlanContractSidecar:
    return PlanContractSidecar(
        plan_digest=plan.canonical_digest,
        plan_outcome=PlanOutcomeContract(
            observable_outcome="기존 계약과 새 기능이 함께 동작한다.",
            acceptance_criteria=("회귀 및 통합 검사를 실행할 수 있다.",),
        ),
        integration_validations=(
            IntegrationValidationContract(
                validation_id="whole-plan",
                work_item_refs=("foundation", "integration"),
                check_type="command",
                specification={"command": "pytest"},
                required_evidence=("pytest exit code",),
            ),
        ),
        criterion_bindings=(
            CriterionValidationBinding(
                work_item_ref="foundation",
                criterion_id="foundation-tests",
                check_type="command",
                capability_id="python-tests",
                specification_digest=sha256_digest({}),
                required_evidence=("테스트 결과",),
            ),
            CriterionValidationBinding(
                work_item_ref="integration",
                criterion_id="integration-tests",
                check_type="command",
                capability_id="python-tests",
                specification_digest=sha256_digest({}),
                required_evidence=("통합 결과",),
            ),
        ),
        dependency_contracts=(
            DependencyContract(
                producer_work_item_ref="foundation",
                consumer_work_item_ref="integration",
                produces=("호환 기능 boundary",),
                consumes=("호환 기능 boundary",),
                compatibility_contract="통합 검사는 확정된 boundary만 소비한다.",
            ),
        ),
        failure_recovery_contracts=tuple(
            FailureRecoveryContract(
                work_item_ref=work_item_ref,
                failure_detection="명령 종료 상태와 diff를 검사한다.",
                idempotency_strategy="같은 입력에서는 같은 변경만 적용한다.",
                partial_execution_strategy="부분 변경을 검사 후 재개한다.",
                duplicate_dispatch_strategy="기존 Attempt binding을 재사용한다.",
                retry_policy="원인이 수정 가능한 경우 새 Attempt로 한정 재시도한다.",
                rollback_strategy="해당 WorkItem 변경만 되돌린다.",
            )
            for work_item_ref in ("foundation", "integration")
        ),
    )


def pass_findings() -> tuple[GateFinding, ...]:
    return tuple(
        GateFinding(
            gate=gate,
            plan_verdict=PlanVerdict.PASS,
            summary=f"{gate.value} 계약이 정의됐다.",
        )
        for gate in GateName
    )


def quality_report(plan: PlanDraft, input_digest: str) -> PlanQualityReport:
    dimensions = ScoreDimensionRatings(
        goal_fit_change_safety=4,
        verification_evidence_strength=4,
        execution_risk_control=4,
        maintainability_reproducibility=4,
        resource_efficiency=2,
    )
    ratings = tuple(
        WorkItemQualityRating(
            work_item_ref=work_item_ref,
            self_containment=2,
            functional_cohesion=2,
            acceptance_validation=2,
            interface_clarity=2,
            failure_retry=2,
        )
        for work_item_ref in ("foundation", "integration")
    )
    return PlanQualityReport(
        planning_input_digest=input_digest,
        plan_digest=plan.canonical_digest,
        plan_verdict=PlanVerdict.PASS,
        gate_findings=pass_findings(),
        dimension_ratings=dimensions,
        dimension_evidence=tuple(
            ScoreDimensionEvidence(
                dimension=dimension,
                rating=getattr(dimensions, dimension.value),
                rationale=f"{dimension.value} fixture 근거가 충분하다.",
                evidence_refs=(plan.canonical_digest,),
                sensitivity="관련 계약이 바뀌면 재평가한다.",
            )
            for dimension in ScoreDimension
        ),
        fitness_score=dimensions.calculated_score,
        work_item_quality=ratings,
        weakest_work_item_ref="foundation",
        weakest_work_item_rating=2,
        confidence=ConfidenceLevel.HIGH,
        tie_break_evidence=TieBreakEvidence(
            reversibility=4,
            public_contract_change=False,
            change_surface=2,
            cost=3,
            mission_metrics={"behavior_compatibility": 4},
        ),
    )


def candidate(
    *,
    candidate_id: str,
    status: CandidateStatus,
    strategy_family: str,
    input_digest: str,
) -> CandidateEnvelope:
    plan = plan_draft()
    return CandidateEnvelope(
        candidate_id=candidate_id,
        version=1,
        refinement_round=0,
        status=status,
        planning_input_digest=input_digest,
        mission_resolution_digest=planning_input().mission_selection.mission_resolution_digest,
        mission_primary=MissionPrimary.FEATURE_EXTENSION,
        approach=ApproachBrief(
            approach_id=f"approach-{strategy_family}",
            mission_primary=MissionPrimary.FEATURE_EXTENSION,
            strategy_family=strategy_family,
            change_shape=f"{strategy_family} 형태",
            compatibility="공개 계약 보존",
            rollout_recovery=f"{strategy_family} 단위 rollback",
            rationale="목적에 맞는 독립 접근이다.",
        ),
        plan=plan,
        contract=contract(plan),
        quality_report=quality_report(plan, input_digest),
        policy_id="balanced-mvp",
        policy_version="v0",
    )


class PlannerR31DomainTests(unittest.TestCase):
    def test_profile_revision_is_immutable_and_digest_bound(self) -> None:
        revision = profile_revision()
        with self.assertRaises(ValidationError):
            revision.model_copy(update={"unexpected": True}).model_validate(  # type: ignore[arg-type]
                {**revision.model_dump(), "unexpected": True}
            )
        with self.assertRaises(ValidationError):
            ProjectProfileRevision.model_validate(
                {**revision.model_dump(), "definition_digest": sha256_bytes(b"tampered")}
            )
        with self.assertRaises(ValidationError):
            revision.status = ProfileRevisionStatus.SUPERSEDED  # type: ignore[misc]

    def test_analysis_mission_must_be_read_only(self) -> None:
        document = mission(MissionPrimary.ANALYSIS_AUDIT).model_dump()
        document["mutation_policy"] = MutationPolicy.SCOPED_CHANGE
        with self.assertRaises(ValidationError):
            PlanningMissionDefinition.model_validate(document)

    def test_unresolved_mission_cannot_be_frozen(self) -> None:
        base = planning_input()
        unresolved = MissionSelectionReceipt(
            request_spec_digest=base.request_spec.canonical_digest,
            raw_request_digest=sha256_bytes(
                base.request_spec.user_request.encode("utf-8")
            ),
            profile_revision_id=base.profile_revision.profile_revision_id,
            profile_definition_digest=base.profile_revision.definition_digest,
            status=MissionResolutionStatus.NEEDS_USER_INPUT,
            options=(mission(), mission(MissionPrimary.LEGACY_REFACTOR)),
            question="기능 확장과 리팩터링 중 어느 결과가 필요한가요?",
        )
        document = base.model_dump()
        document["mission_selection"] = unresolved.model_dump()
        with self.assertRaises(ValidationError):
            PlanningRunInput.model_validate(document)

    def test_semantic_input_digest_excludes_telemetry_and_changes_with_mission(self) -> None:
        first = planning_input(created_at=NOW)
        later = planning_input(created_at=NOW + timedelta(days=30))
        changed_mission = planning_input(primary=MissionPrimary.LEGACY_REFACTOR)
        self.assertEqual(first.planning_input_digest, later.planning_input_digest)
        self.assertNotEqual(first.planning_input_digest, changed_mission.planning_input_digest)

        first_receipt = PlanningRunReceipt(
            run_id="run_first",
            planning_input=first,
            planning_input_digest=first.planning_input_digest,
            idempotency_key="same-semantic-run",
            status=PlanningRunStatus.FROZEN,
            created_at=NOW,
            updated_at=NOW,
        )
        later_receipt = PlanningRunReceipt(
            run_id="run_later",
            planning_input=later,
            planning_input_digest=later.planning_input_digest,
            idempotency_key="same-semantic-run",
            status=PlanningRunStatus.FROZEN,
            created_at=NOW + timedelta(days=30),
            updated_at=NOW + timedelta(days=30),
        )
        self.assertEqual(
            first_receipt.planning_input_digest, later_receipt.planning_input_digest
        )
        self.assertNotEqual(first_receipt.receipt_digest, later_receipt.receipt_digest)

    def test_requirement_trace_must_be_bidirectional(self) -> None:
        valid = planning_input().requirement_extraction
        orphan_trace = RawRequestTrace(
            trace_id="orphan",
            start_offset=1,
            end_offset=2,
            excerpt="기",
        )
        document = valid.model_dump()
        document["traces"] = (*document["traces"], orphan_trace.model_dump())
        with self.assertRaises(ValidationError):
            RequirementExtractionReceipt.model_validate(document)

    def test_candidate_contract_requires_every_validation_dependency_and_recovery(self) -> None:
        run_input = planning_input()
        plan = plan_draft(run_input.request_spec)
        candidate_document = candidate(
            candidate_id="candidate_complete",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="adapter",
            input_digest=run_input.planning_input_digest,
        ).model_dump()
        candidate_document["contract"]["dependency_contracts"] = []
        with self.assertRaises(ValidationError):
            CandidateEnvelope.model_validate(candidate_document)

        candidate_document = candidate(
            candidate_id="candidate_complete_2",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="adapter",
            input_digest=run_input.planning_input_digest,
        ).model_dump()
        candidate_document["contract"]["criterion_bindings"] = candidate_document[
            "contract"
        ]["criterion_bindings"][:-1]
        with self.assertRaises(ValidationError):
            CandidateEnvelope.model_validate(candidate_document)
        self.assertEqual(plan.canonical_digest, contract(plan).plan_digest)

    def test_validation_binding_matches_the_full_validation_contract(self) -> None:
        base = candidate(
            candidate_id="candidate_binding_contract",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="adapter",
            input_digest=planning_input().planning_input_digest,
        )
        first = base.contract.criterion_bindings[0].model_copy(
            update={"specification_digest": sha256_digest({"different": True})}
        )
        mismatched = base.contract.model_copy(
            update={
                "criterion_bindings": (
                    first,
                    *base.contract.criterion_bindings[1:],
                )
            }
        )
        with self.assertRaisesRegex(ValidationError, "criterion-validation"):
            CandidateEnvelope.model_validate(
                {**base.model_dump(), "contract": mismatched.model_dump()}
            )

    def test_pass_gate_cannot_hide_error_and_failed_call_cannot_claim_output(self) -> None:
        with self.assertRaises(ValidationError):
            GateFinding(
                gate=GateName.ENGINEERING,
                plan_verdict=PlanVerdict.PASS,
                summary="오류를 숨긴 잘못된 통과",
                diagnostics=(
                    GateDiagnostic(
                        finding_code="HIDDEN_SECURITY_ERROR",
                        severity=FindingSeverity.ERROR,
                        message="보안 결함",
                        remediable=False,
                    ),
                ),
            )
        with self.assertRaises(ValidationError):
            ModelCallReceipt(
                call_id="call_failed_with_output",
                role=PlanningRole.HARD_GATE_REVIEWER,
                model_id="fixture-model",
                reasoning_effort="high",
                inventory_digest=sha256_bytes(b"inventory"),
                input_digest=sha256_bytes(b"input"),
                output_schema_digest=sha256_bytes(b"schema"),
                output_digest=sha256_bytes(b"not-a-success"),
                status=ModelCallStatus.FAILED,
                error_summary="fixture failure",
            )

    def test_hard_gate_failure_cannot_be_scored(self) -> None:
        plan = plan_draft()
        failed = list(pass_findings())
        failed[2] = GateFinding(
            gate=GateName.ENGINEERING,
            plan_verdict=PlanVerdict.FAIL,
            summary="보안 계약 누락",
            diagnostics=(
                GateDiagnostic(
                    finding_code="SECURITY_CONTRACT_MISSING",
                    severity=FindingSeverity.ERROR,
                    message="보안 결함은 soft score로 보상할 수 없습니다.",
                    remediable=True,
                ),
            ),
        )
        dimensions = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=4,
            maintainability_reproducibility=4,
            resource_efficiency=4,
        )
        with self.assertRaises(ValidationError):
            PlanQualityReport(
                planning_input_digest=planning_input().planning_input_digest,
                plan_digest=plan.canonical_digest,
                plan_verdict=PlanVerdict.FAIL,
                gate_findings=tuple(failed),
                dimension_ratings=dimensions,
                fitness_score=100,
                confidence=ConfidenceLevel.HIGH,
                tie_break_evidence=TieBreakEvidence(
                    reversibility=4,
                    public_contract_change=False,
                    change_surface=1,
                    cost=1,
                ),
            )

    def test_balanced_mvp_score_and_weakest_work_item_are_deterministic(self) -> None:
        dimensions = ScoreDimensionRatings(
            goal_fit_change_safety=4,
            verification_evidence_strength=4,
            execution_risk_control=4,
            maintainability_reproducibility=4,
            resource_efficiency=2,
        )
        self.assertEqual(95, dimensions.calculated_score)
        report = quality_report(plan_draft(), planning_input().planning_input_digest)
        self.assertEqual(95, report.fitness_score)
        self.assertEqual("foundation", report.weakest_work_item_ref)
        self.assertEqual(2, report.weakest_work_item_rating)

    def test_each_soft_score_dimension_requires_bound_evidence(self) -> None:
        plan = plan_draft()
        report = quality_report(plan, planning_input().planning_input_digest)
        document = report.model_dump(mode="python")
        document["dimension_evidence"] = ()
        with self.assertRaises(ValidationError):
            PlanQualityReport.model_validate(document)

    def test_work_item_zero_axis_prevents_admission(self) -> None:
        run_input = planning_input()
        valid = candidate(
            candidate_id="candidate_zero_axis",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="adapter",
            input_digest=run_input.planning_input_digest,
        ).model_dump()
        valid["quality_report"]["work_item_quality"][0]["failure_retry"] = 0
        valid["quality_report"]["weakest_work_item_rating"] = 0
        with self.assertRaises(ValidationError):
            CandidateEnvelope.model_validate(valid)

    def test_search_outcome_only_accepts_diverse_admissible_top_k_and_one_selection(self) -> None:
        run_input = planning_input()
        first = candidate(
            candidate_id="candidate_adapter",
            status=CandidateStatus.SELECTED,
            strategy_family="adapter",
            input_digest=run_input.planning_input_digest,
        )
        second = candidate(
            candidate_id="candidate_extension",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="extension",
            input_digest=run_input.planning_input_digest,
        )
        selection = SelectionReceipt(
            run_id="run_search",
            planning_input_digest=run_input.planning_input_digest,
            recommended_candidate_id=first.candidate_id,
            selected_candidate_id=first.candidate_id,
            selection_source=SelectionSource.RECOMMENDED_DEFAULT,
            ranked_candidate_ids=(first.candidate_id, second.candidate_id),
            alternative_candidate_ids=(second.candidate_id,),
            tie_break_reasons=("기존 소비자 호환성이 더 강하다.",),
        )
        outcome = PlanningSearchOutcome(
            run_id="run_search",
            planning_input_digest=run_input.planning_input_digest,
            mission_primary=MissionPrimary.FEATURE_EXTENSION,
            status=SearchOutcomeStatus.READY_FOR_REVIEW,
            candidates=(first, second),
            top_k_candidate_ids=(first.candidate_id, second.candidate_id),
            selection_receipt=selection,
        )
        self.assertEqual(first.candidate_id, outcome.selection_receipt.selected_candidate_id)
        self.assertEqual(2, len(outcome.top_k_candidate_ids))
        self.assertTrue(
            all(
                item.observation_status is CandidateObservationStatus.NOT_OBSERVED
                for item in outcome.candidates
            )
        )

        duplicate = second.model_copy(
            update={
                "candidate_id": "candidate_duplicate",
                "approach": second.approach.model_copy(
                    update={
                        "strategy_family": first.approach.strategy_family,
                        "change_shape": first.approach.change_shape,
                        "compatibility": first.approach.compatibility,
                        "rollout_recovery": first.approach.rollout_recovery,
                    }
                ),
            }
        )
        bad_selection = selection.model_copy(
            update={
                "ranked_candidate_ids": (first.candidate_id, duplicate.candidate_id),
                "alternative_candidate_ids": (duplicate.candidate_id,),
            }
        )
        with self.assertRaises(ValidationError):
            PlanningSearchOutcome(
                run_id="run_search",
                planning_input_digest=run_input.planning_input_digest,
                mission_primary=MissionPrimary.FEATURE_EXTENSION,
                status=SearchOutcomeStatus.READY_FOR_REVIEW,
                candidates=(first, duplicate),
                top_k_candidate_ids=(first.candidate_id, duplicate.candidate_id),
                selection_receipt=bad_selection,
            )

    def test_low_confidence_does_not_default_select(self) -> None:
        receipt = SelectionReceipt(
            run_id="run_low_confidence",
            planning_input_digest=planning_input().planning_input_digest,
            recommended_candidate_id="candidate_review",
            selection_source=SelectionSource.NONE_LOW_CONFIDENCE,
            ranked_candidate_ids=("candidate_review",),
            alternative_candidate_ids=("candidate_review",),
            tie_break_reasons=("승자를 바꿀 수 있는 가정이 남아 있다.",),
        )
        self.assertIsNone(receipt.selected_candidate_id)

    def test_reviewer_sessions_are_isolated_and_schema_recovery_is_limited(self) -> None:
        with self.assertRaises(ValidationError):
            SessionHint(
                role=PlanningRole.HARD_GATE_REVIEWER,
                strategy=SessionStrategy.REUSE,
                independent_review_session=False,
                rationale="잘못된 session 재사용",
            )
        recovered = ModelCallReceipt(
            call_id="call_recovered",
            role=PlanningRole.CANDIDATE_GENERATOR,
            model_id="resolved-at-runtime",
            reasoning_effort="medium",
            inventory_digest=sha256_bytes(b"inventory"),
            input_digest=planning_input().planning_input_digest,
            output_schema_digest=sha256_bytes(b"candidate-schema"),
            output_digest=sha256_bytes(b"candidate"),
            status=ModelCallStatus.SCHEMA_RECOVERED,
            schema_recovery_attempts=1,
        )
        self.assertEqual(1, recovered.schema_recovery_attempts)
        with self.assertRaises(ValidationError):
            recovered.model_copy(update={"schema_recovery_attempts": 2}).model_validate(
                {**recovered.model_dump(), "schema_recovery_attempts": 2}
            )


if __name__ == "__main__":
    unittest.main()
