from __future__ import annotations

from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES

from pathlib import Path

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine.domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    Criticality,
    EffectPolicy,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    IntegrationValidationContract,
    LifecycleStage,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryEnvelope,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    RiskLevel,
    RoleAssignmentPolicy,
    SourceTrace,
    StateFact,
    StateSnapshot,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ValidationContract,
    new_id,
    utc_now,
)
from flowmarshal.engine.models import ModelCapability, ModelInventory


def profile(project_id: str) -> ProjectProfileRevision:
    definition = ProjectProfileDefinition(
        product_goal="합성 workflow",
        lifecycle_stage=LifecycleStage.DEVELOPMENT,
        criticality=Criticality.HIGH,
        compatibility_policy="공개 계약 보존",
        validation_policy=("unit",),
    )
    return ProjectProfileRevision(
        profile_revision_id=new_id("profile_revision"),
        project_id=project_id,
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )


def goal(project_id: str, profile_digest: str, *, read_only: bool = False) -> GoalContractRevision:
    request = "검증 가능한 변경을 수행한다."
    request_digest = sha256_bytes(request.encode())
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=request,
        source_request_digest=request_digest,
        mission_class=MissionClass.ANALYSIS_AUDIT if read_only else MissionClass.FEATURE_EXTENSION,
        observable_outcome="evidence가 존재한다.",
        hard_acceptance=(
            GoalCriterion(
                criterion_id="ac_one",
                statement="요구가 충족된다.",
                validation_intent="실제 evidence를 확인한다.",
                trace_refs=("trace_one",),
            ),
        ),
        source_traces=(
            SourceTrace(
                trace_id="trace_one",
                source_ref="user-request",
                statement=request,
                source_digest=request_digest,
            ),
        ),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy.READ_ONLY if read_only else MutationPolicy.SCOPED_CHANGE,
            behavior_policy=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS,
        ),
        profile_definition_digest=profile_digest,
    )
    return GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=new_id("goal"),
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )


def project_map(project_id: str, root: Path):
    return ProjectMapper().build(project_id=project_id, root=root, revision_no=1)


def state(project_id: str, goal_digest: str, map_digest: str) -> StateSnapshot:
    return StateSnapshot(
        snapshot_id=new_id("snapshot"),
        project_id=project_id,
        goal_contract_digest=goal_digest,
        version=1,
        scope_fingerprint=sha256_digest({"goal": goal_digest, "map": map_digest}),
        facts=(
            StateFact(
                fact_id="fact_one",
                predicate="project map current",
                value=map_digest,
                source_ref="project-map",
                evidence_digest=map_digest,
            ),
        ),
        observed_at=utc_now(),
    )


def inventory() -> ModelInventory:
    return ModelInventory(
        executable_digest="sha256:" + "0" * 64, runtime_capabilities=RUNTIME_CAPABILITIES,
        source="test-model-list",
        models=(
            ModelCapability(model="worker", supported_efforts=("medium",)),
            ModelCapability(model="validator", supported_efforts=("high",)),
        ),
    )


def assignment() -> ModelAssignmentContract:
    return ModelAssignmentContract(
        executor=RoleAssignmentPolicy(
            role="executor",
            preferred_model="worker",
            preferred_effort="medium",
        ),
        validator=RoleAssignmentPolicy(
            role="validator",
            preferred_model="validator",
            preferred_effort="high",
        ),
        independence_required=True,
    )


def clean_review(
    candidate_digest: str,
    *,
    role: str,
    evidence_catalog: dict[str, object],
) -> ReviewerSubmission:
    return ReviewerSubmission(
        reviewer_role=role,
        candidate_digest=candidate_digest,
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(evidence_catalog),
    )


def skeleton(goal_revision: GoalContractRevision, snapshot: StateSnapshot) -> PlanSkeletonCandidate:
    return PlanSkeletonCandidate(
        candidate_id=new_id("candidate"),
        goal_contract_digest=goal_revision.definition_digest,
        state_signature=snapshot.semantic_digest,
        approach=ApproachSignature(
            strategy_family="direct",
            change_shape="single",
            compatibility="preserve",
            rollout_recovery="retry bounded",
        ),
        tasks=(
            TaskSkeleton(
                task_ref="task_one",
                kind=TaskKind.CHANGE,
                objective="요구를 구현한다.",
                contributes_to=("ac_one",),
                produces=("result:one",),
                consumes=("input:request",),
            ),
        ),
        goal_coverage=(GoalCoverage(criterion_id="ac_one", task_refs=("task_one",)),),
        estimated_change_cost=1,
        estimated_context_tokens=100,
    )


def plan(
    project_id: str,
    goal_revision: GoalContractRevision,
    snapshot: StateSnapshot,
    map_digest: str,
    source_skeleton: PlanSkeletonCandidate,
    model_inventory: ModelInventory,
) -> tuple[PlanContractRevision, TaskContract, CandidateDecision]:
    task = TaskContract(
        task_id=new_id("task"),
        task_ref="task_one",
        project_id=project_id,
        kind=TaskKind.CHANGE,
        objective="요구를 구현한다.",
        goal_criterion_refs=("ac_one",),
        produces=("result:one",),
        consumes=("input:request",),
        acceptance_criteria=("validation이 PASS다.",),
        validations=(
            ValidationContract(
                validation_id="validation_task",
                statement="Task 결과를 확인한다.",
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        risk_level=RiskLevel.LOW,
        approval_class=ApprovalClass.PLAN_ACTIVATION,
        recovery=RecoveryEnvelope(retryable_failure_classes=("implementation",)),
        assignment=assignment(),
    )
    definition = PlanContractDefinition(
        project_id=project_id,
        goal_contract_digest=goal_revision.definition_digest,
        base_state_snapshot_digest=snapshot.snapshot_digest,
        project_map_digest=map_digest,
        source_skeleton_digest=sha256_digest(source_skeleton),
        tasks=(task,),
        goal_coverage=(
            PlanGoalCoverage(
                criterion_id="ac_one",
                task_ids=(task.task_id,),
                validation_ids=("validation_task", "validation_goal"),
            ),
        ),
        integration_validations=(
            IntegrationValidationContract(
                validation_id="validation_goal",
                statement="Goal을 확인한다.",
                evidence_mode="task_aggregate",
                criterion_refs=("ac_one",),
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        model_inventory_digest=model_inventory.inventory_digest,
    )
    revision = PlanContractRevision(
        plan_revision_id=new_id("plan_revision"),
        plan_id=new_id("plan"),
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    decision = CandidateDecision(
        candidate_digest=revision.activation_digest,
        status=CandidateStatus.ADMISSIBLE,
        fitness_score=100,
        weakest_dimension="engineering",
    )
    return revision, task, decision
