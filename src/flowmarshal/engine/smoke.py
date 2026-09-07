from __future__ import annotations

from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES

import json
import sys
from pathlib import Path
from typing import Any

from ..canonical import sha256_bytes, sha256_digest
from .context import ContextNeed, ContextSelector, ProjectMapper, PromptAssembler
from .domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    CommitHorizon,
    ContextManifest,
    CriterionVerdict,
    Criticality,
    EffectPolicy,
    EvidenceKind,
    EvidenceRecord,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    GoalVerdict,
    GoalVerdictStatus,
    IntegrationValidationContract,
    LifecycleStage,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    PlanningBudgetPolicy,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RecoveryEnvelope,
    ReviewRatings,
    ReviewerSubmission,
    ResolvedTarget,
    RevisionStatus,
    RiskLevel,
    RoleAssignmentPolicy,
    SourceTrace,
    StateFact,
    StateSnapshot,
    TaskContract,
    TaskExecutionSpecDefinition,
    TaskExecutionSpecRevision,
    TaskKind,
    TaskSkeleton,
    ValidationContract,
    ValidationResult,
    ValidationExecutionStep,
    ValidationStatus,
    ExecutionAction,
    new_id,
    utc_now,
)
from .ledger import SQLiteEngineLedger
from .models import AssignmentResolver, ModelCapability, ModelInventory
from .planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from .runtime import EngineDispatcher, FakeCodexRuntime
from .service import EngineService


def run_synthetic_lifecycle(
    *,
    project_root: Path | str,
    database_path: Path | str,
    artifact_root: Path | str,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    ledger = SQLiteEngineLedger(database_path, artifact_root=artifact_root)
    service = EngineService(ledger)
    service.initialize()
    project_id = service.create_project(name="FlowMarshal Engine synthetic E2E", root=root)

    profile_definition = ProjectProfileDefinition(
        product_goal="검증 가능한 로컬 workflow orchestration",
        lifecycle_stage=LifecycleStage.DEVELOPMENT,
        criticality=Criticality.HIGH,
        compatibility_policy="공개 계약을 보존한다.",
        validation_policy=("결정적 테스트와 Goal Test를 모두 통과한다.",),
        runtime_requirements=("local Windows", "Python 3.10+"),
    )
    profile = ProjectProfileRevision(
        profile_revision_id=new_id("profile_revision"),
        project_id=project_id,
        revision_no=1,
        definition=profile_definition,
        definition_digest=profile_definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_profile(profile)

    request = "승인된 단일 Task를 실행하고 evidence로 Goal 완료를 판정한다."
    request_digest = sha256_bytes(request.encode("utf-8"))
    goal_definition = GoalContractDefinition(
        project_id=project_id,
        source_request=request,
        source_request_digest=request_digest,
        mission_class=MissionClass.FEATURE_EXTENSION,
        observable_outcome="Task와 Goal 검증 evidence가 원장에 남는다.",
        hard_acceptance=(
            GoalCriterion(
                criterion_id="ac_delivery",
                statement="Task 실행과 검증이 완료된다.",
                validation_intent="Task validation과 plan-level Goal Test를 확인한다.",
                trace_refs=("trace_request",),
            ),
        ),
        source_traces=(
            SourceTrace(
                trace_id="trace_request",
                source_ref="user-request",
                statement=request,
                source_digest=request_digest,
            ),
        ),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_policy=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS,
        ),
        profile_definition_digest=profile_definition.definition_digest,
    )
    goal = GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=new_id("goal"),
        revision_no=1,
        definition=goal_definition,
        definition_digest=goal_definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_goal(goal)

    project_map = ProjectMapper().build(project_id=project_id, root=root, revision_no=1,
                                       excluded_paths=(ledger.artifact_root.resolve(),))
    if not project_map.entries:
        raise RuntimeError("synthetic E2E에는 최소 한 개의 텍스트 프로젝트 파일이 필요합니다.")
    service.record_project_map(project_map)
    state = StateSnapshot(
        snapshot_id=new_id("snapshot"),
        project_id=project_id,
        goal_contract_digest=goal.definition_digest,
        version=1,
        scope_fingerprint=sha256_digest(
            {"goal": goal.definition_digest, "map": project_map.revision_digest}
        ),
        facts=(
            StateFact(
                fact_id="fact_project_map",
                predicate="project map is available",
                value=project_map.revision_digest,
                source_ref="project-map",
                evidence_digest=project_map.revision_digest,
            ),
        ),
        observed_at=utc_now(),
    )
    service.record_state_snapshot(state)

    skeleton = PlanSkeletonCandidate(
        candidate_id=new_id("candidate"),
        goal_contract_digest=goal.definition_digest,
        state_signature=state.semantic_digest,
        approach=ApproachSignature(
            strategy_family="single verified task",
            change_shape="one task",
            compatibility="preserve public contract",
            rollout_recovery="intent receipt recovery",
        ),
        tasks=(
            TaskSkeleton(
                task_ref="task_delivery",
                kind=TaskKind.CHANGE,
                objective="활성 Task 계약을 수행하고 evidence를 남긴다.",
                contributes_to=("ac_delivery",),
                produces=("delivery:evidence",),
                consumes=("input:request",),
            ),
        ),
        goal_coverage=(GoalCoverage(criterion_id="ac_delivery", task_refs=("task_delivery",)),),
        estimated_change_cost=1,
        estimated_context_tokens=2000,
    )
    skeleton_decision = CandidateDecision(
        candidate_digest=sha256_digest(skeleton),
        status=CandidateStatus.ADMISSIBLE,
        fitness_score=100,
        weakest_dimension="engineering",
    )
    skeleton_review = ReviewerSubmission(
        reviewer_role="synthetic_skeleton_reviewer",
        candidate_digest=sha256_digest(skeleton),
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(
            skeleton_review_evidence_catalog(skeleton, goal, state, project_map)
        ),
    )
    service.record_skeleton_evaluation(
        CandidateEvaluation(
            candidate=skeleton,
            semantic_submission=skeleton_review,
            decision=skeleton_decision,
        )
    )

    inventory = ModelInventory(
        executable_digest="sha256:" + "0" * 64, runtime_capabilities=RUNTIME_CAPABILITIES,
        source="synthetic-model-list",
        models=(
            ModelCapability(model="fake-balanced", supported_efforts=("medium",)),
            ModelCapability(model="fake-validator", supported_efforts=("high",)),
        ),
    )
    assignment = ModelAssignmentContract(
        executor=RoleAssignmentPolicy(
            role="executor",
            preferred_model="fake-balanced",
            preferred_effort="medium",
        ),
        validator=RoleAssignmentPolicy(
            role="validator",
            preferred_model="fake-validator",
            preferred_effort="high",
        ),
        independence_required=True,
    )
    task = TaskContract(
        task_id=new_id("task"),
        task_ref="task_delivery",
        project_id=project_id,
        kind=TaskKind.CHANGE,
        objective="활성 Task 계약을 수행하고 evidence를 남긴다.",
        goal_criterion_refs=("ac_delivery",),
        produces=("delivery:evidence",),
        consumes=("input:request",),
        acceptance_criteria=("결정적 Task validation이 PASS다.",),
        validations=(
            ValidationContract(
                validation_id="validation_task",
                statement="Task 결과 evidence를 확인한다.",
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        risk_level=RiskLevel.LOW,
        approval_class=ApprovalClass.PLAN_ACTIVATION,
        recovery=RecoveryEnvelope(
            retryable_failure_classes=("implementation", "context", "environment"),
        ),
        assignment=assignment,
    )
    plan_definition = PlanContractDefinition(
        project_id=project_id,
        goal_contract_digest=goal.definition_digest,
        base_state_snapshot_digest=state.snapshot_digest,
        project_map_digest=project_map.revision_digest,
        source_skeleton_digest=sha256_digest(skeleton),
        tasks=(task,),
        goal_coverage=(
            PlanGoalCoverage(
                criterion_id="ac_delivery",
                task_ids=(task.task_id,),
                validation_ids=("validation_task", "validation_goal"),
            ),
        ),
        integration_validations=(
            IntegrationValidationContract(
                validation_id="validation_goal",
                statement="Task 결과가 Goal observable outcome을 충족한다.",
                evidence_mode="task_aggregate",
                criterion_refs=("ac_delivery",),
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        commit_horizon=CommitHorizon(),
        planning_budget=PlanningBudgetPolicy(),
        model_inventory_digest=inventory.inventory_digest,
    )
    plan = PlanContractRevision(
        plan_revision_id=new_id("plan_revision"),
        plan_id=new_id("plan"),
        revision_no=1,
        definition=plan_definition,
        definition_digest=plan_definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    plan_decision = CandidateDecision(
        candidate_digest=plan.activation_digest,
        status=CandidateStatus.ADMISSIBLE,
        fitness_score=100,
        weakest_dimension="engineering",
    )
    plan_review = ReviewerSubmission(
        reviewer_role="synthetic_plan_reviewer",
        candidate_digest=plan.activation_digest,
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(
            plan_review_evidence_catalog(plan, goal, state, project_map)
        ),
    )
    service.register_plan_evaluation(
        ExpandedPlanEvaluation(
            plan=plan,
            semantic_submissions=(plan_review,),
            decision=plan_decision,
        )
    )
    service.authorize_goal(project_id=project_id, source="합성 사용자 승인")
    service.activate_plan(
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        source="synthetic-smoke",
    )

    prompt = PromptAssembler().assemble(
        static_policy="Task 계약과 Core 권위를 지킨다.",
        project_policy="프로젝트 AGENTS.md를 정상 입력으로 따른다.",
        stage_schema="Task 결과와 evidence summary를 반환한다.",
        task_instruction=task.objective,
        reference_blocks=(),
    )
    needs = (
        ContextNeed(
            need_id="project_source",
            description="최소 프로젝트 텍스트",
            tag_hints=(project_map.entries[0].tags[0],),
        ),
    )
    selection = ContextSelector().select(
        project_map=project_map,
        task=task,
        prompt_binding=prompt.binding,
        needs=needs,
    )
    if selection.manifest is None:
        raise RuntimeError(selection.additional_context_request)
    executor, validator = AssignmentResolver().resolve_contract(assignment, inventory)
    first_entry = project_map.entries[0]
    target_path = first_entry.path
    spec_definition = TaskExecutionSpecDefinition(
        plan_activation_digest=plan.activation_digest,
        task_contract_digest=task.contract_digest,
        task_id=task.task_id,
        snapshot_digest=state.snapshot_digest,
        project_map_digest=project_map.revision_digest,
        context_manifest=selection.manifest,
        resolved_targets=(
            ResolvedTarget(
                target_ref="target_project_file",
                path=target_path,
                expected_content_digest=first_entry.content_digest,
                access="read",
            ),
        ),
        actions=(
            ExecutionAction(
                action_ref="action_inspect",
                kind="inspect",
                description="대상 파일을 확인하고 Task 결과를 보고한다.",
            ),
        ),
        validation_steps=(
            ValidationExecutionStep(
                validation_id="validation_task",
                method="deterministic",
                argv=(sys.executable, "-c", "print('synthetic validation')"),
                working_directory=str(root),
                timeout_seconds=30,
                expected_exit_codes=(0,),
                required_evidence_kinds=("test",),
            ),
        ),
        executor=executor,
        validator=validator,
        resource_locks=(f"project:{project_id}",),
        idempotency_key=f"flowmarshal-engine-{task.task_id}",
    )
    from .worker_prompt import assemble_worker_prompt
    bundle = assemble_worker_prompt(task=task, definition=spec_definition,
                                    profile=profile.definition, root=root)
    spec_definition = spec_definition.model_copy(update={
        "context_manifest": spec_definition.context_manifest.model_copy(update={"prompt_binding": bundle.binding}),
    })
    spec = TaskExecutionSpecRevision(
        execution_spec_revision_id=new_id("execution_spec"),
        task_id=task.task_id,
        revision_no=1,
        definition=spec_definition,
        definition_digest=spec_definition.definition_digest,
        created_at=utc_now(),
    )
    service.materialize_execution_spec(spec, inventory=inventory)

    runtime = FakeCodexRuntime(inventory)
    dispatcher = EngineDispatcher(service, runtime)
    dispatch = dispatcher.run_once(project_id)
    attempt_id = dispatch.attempt_id
    if attempt_id is None:
        raise RuntimeError("synthetic Task가 dispatch되지 않았습니다.")
    with ledger.read() as connection:
        binding_json = connection.execute(
            "SELECT binding_json FROM attempts WHERE id = ?", (attempt_id,)
        ).fetchone()["binding_json"]
    from .domain import ThreadBinding

    binding = ThreadBinding.model_validate_json(binding_json)
    runtime.complete(binding.thread_id, response="synthetic worker completed")
    expected_actions = ("observed", "validated", "completed", "validated", "completed")
    actions = tuple(dispatcher.run_once(project_id).action.value for _ in expected_actions)
    if actions != expected_actions:
        raise RuntimeError(f"synthetic 상태 머신 순서가 다릅니다: {actions}")
    return service.status(project_id)


def main() -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="FlowMarshal Engine synthetic E2E smoke")
    parser.add_argument("--project-root", default=str(Path.cwd()))
    parser.add_argument("--output-root")
    arguments = parser.parse_args()
    if arguments.output_root:
        output = Path(arguments.output_root).resolve()
        output.mkdir(parents=True, exist_ok=True)
        status = run_synthetic_lifecycle(
            project_root=arguments.project_root,
            database_path=output / "flowmarshal-engine.sqlite3",
            artifact_root=output / "artifacts",
        )
    else:
        with tempfile.TemporaryDirectory(prefix="flowmarshal-engine-smoke-") as temp:
            output = Path(temp)
            status = run_synthetic_lifecycle(
                project_root=arguments.project_root,
                database_path=output / "flowmarshal-engine.sqlite3",
                artifact_root=output / "artifacts",
            )
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
