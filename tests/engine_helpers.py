from __future__ import annotations

from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine import runtime as _engine_runtime
from flowmarshal.engine.runtime import (
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    ExecutionPolicyEvidence,
    RuntimeObservation,
    RuntimeOperationReceipt,
    RuntimePolicyError,
)
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
    ThreadBinding,
    ValidationContract,
    new_id,
    utc_now,
)
from flowmarshal.engine.models import ModelCapability, ModelInventory


@dataclass
class _FakeThread:
    thread_id: str
    cwd: Path
    turn_id: str | None = None
    terminal_status: str | None = None
    final_response: str | None = None
    provider_payload: dict[str, Any] = field(default_factory=dict)


class FakeCodexRuntime:
    """중복·재시작 검사를 위한 full-access/never 결정적 테스트 runtime."""

    is_deterministic_test_runtime = True

    def __init__(self, inventory: ModelInventory) -> None:
        self.inventory = inventory
        self.threads: dict[str, _FakeThread] = {}
        self.create_calls = 0
        self.turn_calls = 0
        self.read_calls = 0
        self.resume_calls = 0
        self.interrupt_calls = 0

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
        return None

    def verify_execution_policy(self, cwd: Path | str) -> ExecutionPolicyEvidence:
        workspace = Path(cwd).resolve()
        return ExecutionPolicyEvidence(
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            config_digest=sha256_digest({"permission": REQUIRED_PERMISSION_PROFILE, "approval": REQUIRED_APPROVAL_POLICY}),
            profile_catalog_digest=sha256_digest([REQUIRED_PERMISSION_PROFILE]),
            cwd=str(workspace),
        )

    def list_models(self) -> ModelInventory:
        return self.inventory

    def create_thread(
        self,
        *,
        cwd: Path,
        title: str,
        model: str,
        developer_instructions: str,
        ephemeral: bool = False,
    ) -> RuntimeOperationReceipt:
        del title, model, developer_instructions, ephemeral
        self.verify_execution_policy(cwd)
        self.create_calls += 1
        thread_id = new_id("runtime_thread")
        self.threads[thread_id] = _FakeThread(thread_id=thread_id, cwd=cwd.resolve())
        binding = ThreadBinding(thread_id=thread_id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={"thread_id": thread_id, "thread": {"id": thread_id, "turns": []}},
            binding=binding,
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt:
        del model, effort, output_schema
        self.verify_execution_policy(cwd)
        self.turn_calls += 1
        thread = self.threads[thread_id]
        first_empty_thread = thread.turn_id is None
        turn_id = new_id("runtime_turn")
        thread.turn_id = turn_id
        thread.terminal_status = None
        thread.final_response = None
        thread.provider_payload = {}
        binding = ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "prompt_digest": sha256_digest(prompt),
                "first_empty_thread": first_empty_thread,
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
            },
            binding=binding,
        )

    def complete(self, thread_id: str, *, response: str = "완료") -> None:
        self.threads[thread_id].terminal_status = "completed"
        self.threads[thread_id].final_response = response

    def fail(
        self, thread_id: str, *, response: str = "실패", error_code: str | None = None,
    ) -> None:
        self.threads[thread_id].terminal_status = "failed"
        self.threads[thread_id].final_response = response
        self.threads[thread_id].provider_payload = (
            {} if error_code is None else {"error_code": error_code}
        )

    def read(self, *, thread_id: str) -> RuntimeObservation:
        self.read_calls += 1
        thread = self.threads[thread_id]
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=thread.turn_id,
            active=thread.turn_id is not None and thread.terminal_status is None,
            terminal_status=thread.terminal_status,
            final_response=thread.final_response,
            payload={
                "thread_id": thread_id,
                "turn_id": thread.turn_id,
                **thread.provider_payload,
            },
        )

    def operation_kind_for_read(self, thread_id: str) -> str:
        del thread_id
        return "read"

    def read_stored(
        self, *, thread_id: str, turn_id: str | None = None, timeout_seconds: float = 5.0,
    ) -> RuntimeObservation:
        del timeout_seconds
        self.read_calls += 1
        thread = self.threads[thread_id]
        if turn_id is not None and thread.turn_id != turn_id:
            raise RuntimePolicyError(
                "RUNTIME_OBSERVATION_BINDING_MISMATCH: 요청한 exact turn을 찾지 못했습니다."
            )
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=thread.turn_id,
            active=thread.turn_id is not None and thread.terminal_status is None,
            terminal_status=thread.terminal_status,
            final_response=thread.final_response,
            payload={
                "thread_id": thread_id,
                "requested_turn_id": turn_id,
                "turn_count": 0 if thread.turn_id is None else 1,
                "turn_status": thread.terminal_status,
                "usage": None,
                "usage_scope": "unavailable",
                **thread.provider_payload,
                "usage_source": "thread/read",
                "turn_history_available": True,
                "turn_history_source": "thread/read(includeTurns=true)",
            },
        )

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt:
        self.resume_calls += 1
        thread = self.threads[thread_id]
        if thread.cwd != cwd.resolve():
            raise RuntimeError("thread cwd가 다릅니다.")
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={"thread_id": thread_id, "resumed": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=thread.turn_id, bound_at=utc_now()),
        )

    def interrupt(
        self, *, thread_id: str, turn_id: str, timeout_seconds: float = 5.0,
    ) -> RuntimeOperationReceipt:
        del timeout_seconds
        thread = self.threads[thread_id]
        if thread.turn_id != turn_id:
            raise KeyError(turn_id)
        self.interrupt_calls += 1
        thread.terminal_status = "interrupted"
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupted": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )


setattr(_engine_runtime, "_FakeThread", _FakeThread)
setattr(_engine_runtime, "FakeCodexRuntime", FakeCodexRuntime)


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
    return ProjectMapper().build(
        project_id=project_id,
        root=root,
        revision_no=1,
        observed_paths=tuple(
            path.relative_to(root)
            for path in root.rglob("*")
            if path.is_file()
            and not any(part.startswith(".flowmarshal-engine") for part in path.relative_to(root).parts)
        ),
    )


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
