from __future__ import annotations

from .domain import ModelFallback

import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    EffectPolicy,
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    IntegrationValidationContract,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    RecoveryEnvelope,
    ResolvedTarget,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    RiskLevel,
    RoleAssignmentPolicy,
    RunOnceAction,
    SourceTrace,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ThreadBinding,
    ValidationContract,
    ValidationExecutionStep,
    new_id,
    utc_now,
)
from .evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    EvaluationScope,
    ImmutableCheckpointStore,
)
from .evaluation_budget import (
    EvaluationPolicies,
    budgeted_role_runner,
    policy_contract_fragment,
    verify_service_budget_policy,
    write_immutable_run_metadata,
)
from .budget import BudgetManager
from .ledger import SQLiteEngineLedger
from .models import EngineRoleConfiguration, ModelInventory
from .planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from .qualification import (
    QualificationRunError,
    ScopeQualificationReport,
    _default_run_root,
    _model_lock,
    _preflight,
    _profile,
    _write_json,
    default_role_configuration,
    project_root,
    source_manifest_digest,
)
from .runtime import CodexAppServerRuntime, CodexRuntimePort, EngineDispatcher, FakeCodexRuntime
from .role_execution import use_role_timeout_policy
from .service import EngineService, EngineServiceError


E2E_SCENARIOS = (
    "normal-completion",
    "stale-after-materialization",
    "stored-turn-restart-resume",
    "unknown-receipt-no-duplicate",
)


class RecordedRuntime:
    """실제 adapter 호출과 receipt를 보존하는 평가용 위임 wrapper."""

    def __init__(self, runtime: CodexRuntimePort, *, journal: Path) -> None:
        self.runtime = runtime
        self.journal = journal
        self.events: list[dict[str, Any]] = []
        if journal.is_file():
            self.events = json.loads(journal.read_text(encoding="utf-8"))
        self.create_calls = sum(item["operation"] == "create_thread" for item in self.events)
        self.turn_calls = sum(item["operation"] == "start_turn" for item in self.events)
        self.read_calls = sum(item["operation"] == "read" for item in self.events)
        self.resume_calls = sum(item["operation"] == "resume" for item in self.events)

    def _call(self, operation: str, **arguments: Any) -> Any:
        result = getattr(self.runtime, operation)(**arguments)
        self.events.append({"operation": operation, "receipt": result.model_dump(mode="json")})
        _write_json(self.journal, self.events)
        return result

    def verify_execution_policy(self, cwd: Path | str) -> Any:
        return self._call("verify_execution_policy", cwd=cwd)

    def list_models(self) -> ModelInventory:
        return self._call("list_models")

    def create_thread(self, **arguments: Any) -> Any:
        result = self._call("create_thread", **arguments)
        self.create_calls += 1
        return result

    def start_turn(self, **arguments: Any) -> Any:
        result = self._call("start_turn", **arguments)
        self.turn_calls += 1
        return result

    def read(self, **arguments: Any) -> Any:
        result = self._call("read", **arguments)
        self.read_calls += 1
        return result

    def read_stored(self, **arguments: Any) -> Any:
        return self._call("read_stored", **arguments)

    def resume(self, **arguments: Any) -> Any:
        result = self._call("resume", **arguments)
        self.resume_calls += 1
        return result

    def interrupt(self, **arguments: Any) -> Any:
        return self._call("interrupt", **arguments)

    def close(self) -> None:
        self.runtime.close()


@dataclass
class PreparedE2E:
    service: EngineService
    project_id: str
    task_id: str
    plan_revision_id: str
    activation_digest: str
    proposal: ExecutionSpecProposal
    workspace: Path


_CELL_STATE_SCHEMA = "flowmarshal.project-e2e.cell-state.v1"


def _write_prepared_state(
    cell_root: Path,
    prepared: PreparedE2E,
    *,
    evaluation_contract_digest: str,
    fixture_digest: str,
) -> None:
    """사용량 제한·프로세스 중단 뒤 같은 cell을 이어갈 최소 권위 참조를 저장한다."""

    payload: dict[str, Any] = {
        "schema": _CELL_STATE_SCHEMA,
        "evaluation_contract_digest": evaluation_contract_digest,
        "fixture_digest": fixture_digest,
        "project_id": prepared.project_id,
        "task_id": prepared.task_id,
        "plan_revision_id": prepared.plan_revision_id,
        "activation_digest": prepared.activation_digest,
        "workspace": str(prepared.workspace.resolve()),
        "proposal": prepared.proposal.model_dump(mode="json"),
    }
    payload["state_digest"] = sha256_digest(payload)
    _write_json(cell_root / "cell-state.json", payload)


def _restore_prepared_state(
    cell_root: Path,
    *,
    evaluation_contract_digest: str,
    fixture_digest: str,
) -> PreparedE2E:
    state_path = cell_root / "cell-state.json"
    if not state_path.is_file():
        raise QualificationRunError(
            f"미완료 E2E cell에 재개 상태가 없습니다. 새 run root가 필요합니다: {cell_root}"
        )
    document = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise QualificationRunError("E2E cell 재개 상태는 JSON object여야 합니다.")
    state_digest = document.pop("state_digest", None)
    if state_digest != sha256_digest(document):
        raise QualificationRunError("E2E cell 재개 상태 digest가 일치하지 않습니다.")
    if document.get("schema") != _CELL_STATE_SCHEMA:
        raise QualificationRunError("지원하지 않는 E2E cell 재개 상태 schema입니다.")
    if document.get("evaluation_contract_digest") != evaluation_contract_digest:
        raise QualificationRunError("E2E cell이 현재 evaluation 계약과 다릅니다.")
    if document.get("fixture_digest") != fixture_digest:
        raise QualificationRunError("E2E cell이 현재 fixture와 다릅니다.")

    workspace = (cell_root / "workspace").resolve(strict=True)
    if Path(str(document.get("workspace"))).resolve(strict=True) != workspace:
        raise QualificationRunError("E2E cell workspace 결속이 다릅니다.")
    ledger = SQLiteEngineLedger(
        cell_root / "state" / "flowmarshal-engine.sqlite3",
        artifact_root=cell_root / "state" / "artifacts",
    )
    service = EngineService(ledger)
    service.initialize()
    proposal = ExecutionSpecProposal.model_validate(document.get("proposal"))
    prepared = PreparedE2E(
        service=service,
        project_id=str(document["project_id"]),
        task_id=str(document["task_id"]),
        plan_revision_id=str(document["plan_revision_id"]),
        activation_digest=str(document["activation_digest"]),
        proposal=proposal,
        workspace=workspace,
    )
    status = service.status(prepared.project_id)
    if status["project"]["active_plan_revision_id"] != prepared.plan_revision_id:
        raise QualificationRunError("E2E cell의 active Plan 결속이 다릅니다.")
    if proposal.task_id != prepared.task_id:
        raise QualificationRunError("E2E cell의 proposal Task 결속이 다릅니다.")
    return prepared


def _assignment(roles: EngineRoleConfiguration) -> ModelAssignmentContract:
    return ModelAssignmentContract(
        executor=RoleAssignmentPolicy(
            role="executor",
            preferred_model=roles.executor.model,
            preferred_effort=roles.executor.effort,
            allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.executor.allowed_fallbacks),
        ),
        validator=RoleAssignmentPolicy(
            role="validator",
            preferred_model=roles.validator.model,
            preferred_effort=roles.validator.effort,
            allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.validator.allowed_fallbacks),
        ),
        independence_required=True,
    )


def _prepare(
    *,
    workspace: Path,
    state_root: Path,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    goal_validation: IntegrationValidationContract | None = None,
    semantic_task_validation: bool = False,
    evaluation_policies: EvaluationPolicies | None = None,
) -> PreparedE2E:
    ledger = SQLiteEngineLedger(
        state_root / "flowmarshal-engine.sqlite3",
        artifact_root=state_root / "artifacts",
    )
    service = EngineService(ledger)
    service.initialize()
    project_id = service.create_project(name="Engine project E2E", root=workspace)
    profile = _profile(project_id)
    service.register_profile(profile)

    request = (
        "app.py의 add 함수가 두 정수의 합을 반환하도록 최소 수정하고 공개 함수 계약을 "
        "유지하며 test_app.py 검증을 통과시켜줘."
    )
    request_digest = sha256_bytes(request.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=request,
        source_request_digest=request_digest,
        mission_class=MissionClass.BUGFIX_STABILIZATION,
        observable_outcome="add(2, 3)이 5를 반환하고 회귀 테스트가 통과한다.",
        hard_acceptance=(
            GoalCriterion(
                criterion_id="ac_fix",
                statement="add(2, 3)이 5를 반환한다.",
                validation_intent="Python unittest로 실제 동작을 확인한다.",
                trace_refs=("trace_request",),
            ),
        ),
        non_goals=("공개 함수 이름과 test_app.py 변경",),
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
        profile_definition_digest=profile.definition_digest,
    )
    goal = GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=new_id("goal"),
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_goal(goal)
    if evaluation_policies is not None:
        BudgetManager(service).configure(
            project_id,
            evaluation_policies.budget,
            goal_id=goal.goal_id,
        )
    project_map, state = service.reobserve_project(project_id)

    skeleton = PlanSkeletonCandidate(
        candidate_id=new_id("candidate"),
        goal_contract_digest=goal.definition_digest,
        state_signature=state.semantic_digest,
        approach=ApproachSignature(
            strategy_family="minimal arithmetic bugfix",
            change_shape="one implementation file",
            compatibility="preserve add public contract",
            rollout_recovery="digest-bound validation and retry",
        ),
        tasks=(
            TaskSkeleton(
                task_ref="task_fix_add",
                kind=TaskKind.CHANGE,
                objective="app.py의 add 구현만 최소 수정해 실제 덧셈을 반환하게 한다.",
                contributes_to=("ac_fix",),
                produces=("artifact:fixed-add",),
                consumes=("input:app.py", "input:test_app.py"),
            ),
        ),
        goal_coverage=(GoalCoverage(criterion_id="ac_fix", task_refs=("task_fix_add",)),),
        estimated_change_cost=1,
        estimated_context_tokens=1500,
    )
    skeleton_review = ReviewerSubmission(
        reviewer_role="qualification-skeleton-reviewer",
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
            decision=CandidateDecision(
                candidate_digest=sha256_digest(skeleton),
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
    )

    assignment = _assignment(roles)
    task = TaskContract(
        task_id=new_id("task"),
        task_ref="task_fix_add",
        project_id=project_id,
        kind=TaskKind.CHANGE,
        objective="app.py의 add 구현만 최소 수정해 실제 덧셈을 반환하게 한다.",
        goal_criterion_refs=("ac_fix",),
        produces=("artifact:fixed-add",),
        consumes=("input:app.py", "input:test_app.py"),
        acceptance_criteria=("Python unittest가 종료 코드 0으로 통과한다.",),
        validations=(
            ValidationContract(
                validation_id="validation_unittest",
                statement="실제 unittest를 실행한다.",
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ) + ((ValidationContract(
            validation_id="validation_public_contract", statement="직접 파일·테스트 evidence로 공개 함수 계약 보존을 독립 검토한다.",
            method="semantic", required_evidence_kinds=("model_review", "file", "test"),
        ),) if semantic_task_validation else ()),
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
                criterion_id="ac_fix",
                task_ids=(task.task_id,),
                validation_ids=("validation_unittest", "validation_goal") + (("validation_public_contract",) if semantic_task_validation else ()),
            ),
        ),
        integration_validations=(
            goal_validation or IntegrationValidationContract(
                validation_id="validation_goal",
                statement="완료 후 unittest를 독립 재실행하여 Goal의 통합 동작을 확인한다.",
                criterion_refs=("ac_fix",),
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        model_inventory_digest=inventory.inventory_digest,
        expected_effects=("app.py의 add 구현 최소 수정",),
        prohibited_effects=("test_app.py 또는 공개 함수 계약 변경",),
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
    plan_review = ReviewerSubmission(
        reviewer_role="qualification-plan-reviewer",
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
            decision=CandidateDecision(
                candidate_digest=plan.activation_digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
    )
    service.activate_plan(
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        source="qualification",
    )
    app_entry = next(item for item in project_map.entries if item.path == "app.py")
    proposal = ExecutionSpecProposal(
        task_id=task.task_id,
        context_needs=(
            ExecutionContextNeed(
                need_id="source_app",
                description="수정 대상 구현",
                path_hints=("app.py",),
            ),
            ExecutionContextNeed(
                need_id="test_app",
                description="고정 회귀 테스트",
                path_hints=("test_app.py",),
            ),
        ),
        resolved_targets=(
            ResolvedTarget(
                target_ref="target_app",
                path="app.py",
                expected_content_digest=app_entry.content_digest,
                access="write",
            ),
            ResolvedTarget(
                target_ref="target_test",
                path="test_app.py",
                expected_content_digest=next(
                    item.content_digest for item in project_map.entries if item.path == "test_app.py"
                ),
                access="read",
            ),
        ),
        actions=(
            ExecutionAction(
                action_ref="action_fix",
                kind="edit",
                description="app.py의 뺄셈 연산만 덧셈으로 바꾼다.",
            ),
        ),
        validation_steps=(
            ValidationExecutionStep(
                validation_id="validation_unittest",
                method="deterministic",
                argv=(sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"),
                working_directory=str(workspace),
                timeout_seconds=60,
                expected_exit_codes=(0,),
                required_evidence_kinds=("test",),
            ),
        ) + ((ValidationExecutionStep(
            validation_id="validation_public_contract", method="semantic",
            semantic_instruction="실제 파일·테스트 관측을 검토해 add 공개 함수의 이름·인자·덧셈 계약 보존을 확인한다.",
            required_evidence_kinds=("model_review", "file", "test"),
        ),) if semantic_task_validation else ()),
        resource_locks=(f"file:{workspace / 'app.py'}",),
        timeout_seconds=900,
        idempotency_hint="project-e2e-add-fix",
    )
    return PreparedE2E(
        service=service,
        project_id=project_id,
        task_id=task.task_id,
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        proposal=proposal,
        workspace=workspace,
    )


def _copy_fixture(root: Path, cell_root: Path) -> tuple[Path, str]:
    source = root / "tests" / "fixtures" / "engine" / "project-e2e"
    source_digest = sha256_digest(
        {
            path.relative_to(source).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(source.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
    )
    workspace = cell_root / "workspace"
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return workspace, source_digest


def _status_assertions(prepared: PreparedE2E, source_digest: str) -> dict[str, Any]:
    status = prepared.service.status(prepared.project_id)
    with prepared.service.ledger.read() as connection:
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE project_id = ?",
            (prepared.project_id,),
        ).fetchone()[0]
        validation_count = connection.execute(
            "SELECT COUNT(*) FROM validation_results WHERE plan_revision_id = ? AND status = 'pass'",
            (prepared.plan_revision_id,),
        ).fetchone()[0]
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE project_id = ? AND binding_json IS NOT NULL",
            (prepared.project_id,),
        ).fetchone()[0]
        verdict_count = connection.execute(
            "SELECT COUNT(*) FROM goal_verdicts WHERE plan_revision_id = ?",
            (prepared.plan_revision_id,),
        ).fetchone()[0]
        goal_direct_evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE project_id = ? AND task_id IS NULL "
            "AND kind = 'test' AND source_ref LIKE 'validation:validation_goal:%'",
            (prepared.project_id,),
        ).fetchone()[0]
        plan_row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?", (prepared.plan_revision_id,)).fetchone()
        definition = json.loads(plan_row["payload_json"])["definition"]
        expected_validation_count = sum(len(item["validations"]) for item in definition["tasks"]) + len(definition["integration_validations"])
    passed = all(
        (
            status["project"]["run_state"] == "completed",
            status["history_valid"],
            evidence_count >= 3,
            validation_count == expected_validation_count,
            binding_count >= 1,
            verdict_count == 1,
            goal_direct_evidence_count >= 1,
            (prepared.workspace / "app.py").is_file(),
        )
    )
    return {
        "passed": passed,
        "source_fixture_digest": source_digest,
        "activation_digest": prepared.activation_digest,
        "run_state": status["project"]["run_state"],
        "history_valid": status["history_valid"],
        "evidence_count": evidence_count,
        "pass_validation_count": validation_count,
        "binding_count": binding_count,
        "goal_verdict_count": verdict_count,
        "independent_goal_test_evidence_count": goal_direct_evidence_count,
    }


def _normal_completion(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *, roles: EngineRoleConfiguration | None = None,
) -> dict[str, Any]:
    from .execution import ExecutionProposalAdapter
    provider = None
    if roles is not None:
        with prepared.service.ledger.read() as connection:
            goal = connection.execute(
                "SELECT g.goal_id, g.definition_digest FROM projects p "
                "JOIN goal_revisions g ON g.id = p.active_goal_revision_id WHERE p.id = ?",
                (prepared.project_id,),
            ).fetchone()
        if goal is None:
            raise QualificationRunError("E2E_GOAL_BUDGET_BINDING_MISSING")
        provider = ExecutionProposalAdapter(
            prepared.service,
            budgeted_role_runner(
                runtime,
                prepared.service,
                project_id=prepared.project_id,
                goal_id=goal["goal_id"],
                goal_digest=goal["definition_digest"],
            ),
            roles,
        )
    dispatcher = EngineDispatcher(prepared.service, runtime, proposal_provider=provider)
    deadline = time.monotonic() + 900
    actions: list[str] = []
    if prepared.service.status(prepared.project_id)["project"]["run_state"] == "completed":
        result = _status_assertions(prepared, source_digest)
        result["actions"] = ["restored_completed"]
        return result
    while time.monotonic() < deadline:
        # proposal은 ready 상태에서만 소비된다. 중단 위치와 무관하게 같은
        # digest-bound proposal로 materialize/dispatch/observe/validate를 재개한다.
        outcome = dispatcher.run_once(
            prepared.project_id, proposal=prepared.proposal if provider is None else None,
            goal_validation_step=(prepared.proposal.validation_steps[0].model_copy(
                update={"validation_id": "validation_goal"}) if provider is None else None),
        )
        actions.append(outcome.action.value)
        if outcome.action is RunOnceAction.COMPLETED and outcome.goal_verdict_id is not None:
            result = _status_assertions(prepared, source_digest)
            result["actions"] = actions
            result["automatic_preparation"] = provider is not None
            return result
        if outcome.action is RunOnceAction.BLOCKED:
            return {
                "passed": False,
                "source_fixture_digest": source_digest,
                "actions": actions,
                "failure": f"{outcome.blocker_code}: {outcome.detail}",
            }
        time.sleep(0.5)
    return {
        "passed": False,
        "source_fixture_digest": source_digest,
        "actions": actions,
        "failure": "900초 안에 실제 Codex E2E가 완료되지 않음",
    }


def _stale_after_materialization(
    prepared: PreparedE2E, runtime: Any, source_digest: str
) -> dict[str, Any]:
    dispatcher = EngineDispatcher(prepared.service, runtime)
    first = dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
    (prepared.workspace / "app.py").write_text(
        (prepared.workspace / "app.py").read_text(encoding="utf-8") + "\n# external change\n",
        encoding="utf-8",
    )
    failure: str | None = None
    try:
        dispatcher.run_once(prepared.project_id)
    except EngineServiceError as error:
        failure = str(error)
    return {
        "passed": first.action is RunOnceAction.MATERIALIZED
        and failure is not None
        and "STALE_EXECUTION_INPUT" in failure
        and runtime.create_calls == 0,
        "source_fixture_digest": source_digest,
        "error": failure,
        "thread_create_count": runtime.create_calls,
    }


def _restart_resume(
    prepared: PreparedE2E, runtime: FakeCodexRuntime, source_digest: str
) -> dict[str, Any]:
    dispatcher = EngineDispatcher(prepared.service, runtime)
    dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
    dispatched = dispatcher.run_once(prepared.project_id)
    with prepared.service.ledger.read() as connection:
        row = connection.execute(
            "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
        ).fetchone()
    binding = ThreadBinding.model_validate_json(row["binding_json"])
    runtime.threads[binding.thread_id].terminal_status = "interrupted"
    restarted = EngineDispatcher(prepared.service, runtime)
    resumed = restarted.run_once(prepared.project_id)
    read_before_resume = runtime.read_calls >= 1 and runtime.resume_calls == 1
    runtime.complete(binding.thread_id, response="재개된 worker가 종료됨")
    observed = restarted.run_once(prepared.project_id)
    return {
        "passed": resumed.action is RunOnceAction.DISPATCHED
        and observed.action is RunOnceAction.OBSERVED
        and read_before_resume
        and runtime.create_calls == 1,
        "source_fixture_digest": source_digest,
        "read_calls": runtime.read_calls,
        "resume_calls": runtime.resume_calls,
        "thread_create_count": runtime.create_calls,
        "turn_start_count": runtime.turn_calls,
    }


def _unknown_receipt(
    prepared: PreparedE2E, runtime: Any, source_digest: str
) -> dict[str, Any]:
    EngineDispatcher(prepared.service, runtime).run_once(
        prepared.project_id, proposal=prepared.proposal
    )

    def fault(point: str) -> None:
        if point == "after_thread_effect":
            raise RuntimeError("fault after provider effect before receipt")

    failed = False
    try:
        EngineDispatcher(prepared.service, runtime, fault_hook=fault).run_once(
            prepared.project_id
        )
    except RuntimeError:
        failed = True
    recovered = EngineDispatcher(prepared.service, runtime).run_once(prepared.project_id)
    return {
        "passed": failed
        and recovered.action is RunOnceAction.BLOCKED
        and recovered.blocker_code == "EXTERNAL_EFFECT_UNKNOWN"
        and runtime.create_calls == 1
        and runtime.turn_calls == 0,
        "source_fixture_digest": source_digest,
        "thread_create_count": runtime.create_calls,
        "turn_start_count": runtime.turn_calls,
        "recovery_action": recovered.action.value,
        "blocker_code": recovered.blocker_code,
    }


def _live_restart_resume(
    prepared: PreparedE2E,
    source_digest: str,
    *,
    cell_root: Path,
    contract: EvaluationContract,
    fixture_digest: str,
    codex_bin: Path | str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """실제 App Server 연결과 Core 인스턴스를 닫은 뒤 같은 저장 turn을 재관측한다."""

    journal = cell_root / "runtime-receipts.json"
    with CodexAppServerRuntime(codex_bin=codex_bin) as first_runtime:
        recorded = RecordedRuntime(first_runtime, journal=journal)
        dispatcher = EngineDispatcher(prepared.service, recorded)
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
            ).fetchone()
        if row is None or row["binding_json"] is None:
            raise QualificationRunError("restart E2E의 실제 thread/turn binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        # start receipt와 로컬 Future만으로 저장 turn의 준비를 추정하지 않는다.
        # Windows에서 빈 rollout이 잠시 보일 수 있으므로 비재개 조회로 확인한다.
        deadline = time.monotonic() + 30
        observation = None
        last_read_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                observation = recorded.read_stored(thread_id=binding.thread_id)
            except Exception as error:
                last_read_error = error
            if observation is not None and observation.turn_id == binding.turn_id:
                break
            time.sleep(0.25)
        if observation is None or observation.turn_id != binding.turn_id:
            raise QualificationRunError(f"restart fault 전 저장 turn을 확인하지 못했습니다: {last_read_error}")
        if not observation.active or binding.turn_id is None:
            raise QualificationRunError("restart fault 전에 실제 turn이 종료돼 재개 경로를 검증하지 못했습니다.")
        try:
            recorded.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
        except Exception as error:
            raise QualificationRunError(f"저장 turn 확인 후 실제 interrupt가 실패했습니다: {error}") from error
        deadline = time.monotonic() + 30
        while observation.active and time.monotonic() < deadline:
            time.sleep(0.25)
            observation = recorded.read(thread_id=binding.thread_id)
        if observation.terminal_status not in {"interrupted", "cancelled", "canceled"}:
            raise QualificationRunError("실제 turn의 중단을 확인하지 못했습니다.")

    restored = _restore_prepared_state(
        cell_root,
        evaluation_contract_digest=contract.contract_digest,
        fixture_digest=fixture_digest,
    )
    with CodexAppServerRuntime(codex_bin=codex_bin) as second_runtime:
        recorded = RecordedRuntime(second_runtime, journal=journal)
        restart_index = len(recorded.events)
        resumed = EngineDispatcher(restored.service, recorded).run_once(restored.project_id)
        restart_operations = [item["operation"] for item in recorded.events[restart_index:]]
        read_before_resume = (
            "read" in restart_operations
            and "resume" in restart_operations
            and restart_operations.index("read") < restart_operations.index("resume")
        )
        result = _normal_completion(restored, recorded, source_digest)
        result.update(
            {
                "read_before_resume": read_before_resume,
                "thread_create_count": recorded.create_calls,
                "turn_start_count": recorded.turn_calls,
                "resume_calls": recorded.resume_calls,
            }
        )
        result["passed"] = bool(result["passed"]) and all(
            (
                resumed.action is RunOnceAction.DISPATCHED,
                read_before_resume,
                recorded.create_calls == 1,
                recorded.turn_calls == 2,
                recorded.resume_calls == 1,
            )
        )
        return result, recorded.events


def _contract(
    root: Path,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    source_digest: str,
    policies: EvaluationPolicies | None = None,
) -> EvaluationContract:
    import inspect
    from .execution import ProviderExecutionPreparation, GoalTestPreparation, EXECUTION_PREPARATION_INSTRUCTIONS, GOAL_TEST_PREPARATION_INSTRUCTIONS
    from .worker_prompt import assemble_worker_prompt, PromptArtifactStore
    from .roles import strict_json_output_schema
    fixture_digests = tuple(
        sha256_digest({"scenario": scenario, "source_fixture_digest": source_digest})
        for scenario in E2E_SCENARIOS
    )
    return EvaluationContract(
        model_lock_format="flowmarshal-model-lock-v2",
        scope=EvaluationScope.PROJECT_E2E,
        fixture_digests=fixture_digests,
        scenario_set_digest=sha256_digest(
            {"scenarios": E2E_SCENARIOS, "source_fixture_digest": source_digest}
        ),
        order_seeds=(0,),
        expected_cell_count=len(E2E_SCENARIOS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest(
            {"actual_codex": list(E2E_SCENARIOS), "restart": "new-app-server-and-core"}
            | (
                policy_contract_fragment(policies)
                if policies is not None
                else {"evaluation_policy": "synthetic-unbound"}
            )
        ),
        threshold_digest=sha256_digest({"all_scenarios_pass": True, "duplicate_effects": 0}),
        taxonomy_digest=sha256_digest(
            {"requirements": ["digest", "binding", "evidence", "validation", "goal", "history"]}
        ),
        prompt_digest=sha256_digest({"task": inspect.getsource(assemble_worker_prompt),
                                    "artifact": inspect.getsource(PromptArtifactStore),
                                    "turn": inspect.getsource(EngineDispatcher._start_turn),
                                    "preparation": EXECUTION_PREPARATION_INSTRUCTIONS,
                                    "goal_preparation": GOAL_TEST_PREPARATION_INSTRUCTIONS,
                                    "validator": inspect.getsource(EngineDispatcher._role_for_attempt),
                                    "fixture_contract": inspect.getsource(_prepare)}),
        output_schema_digest=sha256_digest({
            "preparation": strict_json_output_schema(ProviderExecutionPreparation.model_json_schema()),
            "goal": strict_json_output_schema(GoalTestPreparation.model_json_schema()),
            "semantic": EngineDispatcher._semantic_schema(),
        }),
        model_lock_digest=_model_lock(inventory, roles),
    )


def run_project_e2e(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    codex_bin: Path | str | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    if evaluation_policies is None:
        raise QualificationRunError("EVALUATION_POLICY_REQUIRED")
    base = (root or project_root()).resolve(strict=True)
    preflight_failures = _preflight(base)
    if preflight_failures:
        raise QualificationRunError("; ".join(preflight_failures))
    roles = role_configuration or default_role_configuration(base)
    fixture_source = base / "tests" / "fixtures" / "engine" / "project-e2e"
    source_digest = sha256_digest(
        {
            path.relative_to(fixture_source).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(fixture_source.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
    )
    with CodexAppServerRuntime(codex_bin=codex_bin) as real_runtime:
        inventory = real_runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _contract(base, inventory, roles, source_digest, evaluation_policies)
        destination = (
            run_root or _default_run_root(base, "project-e2e", contract.contract_digest[7:15])
        ).resolve()
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        prior_state = store.state().status
        if prior_state is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        write_immutable_run_metadata(
            destination / "run-metadata.json",
            {
                "scope": "project-e2e",
                "evaluation_contract_digest": contract.contract_digest,
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
            },
            evaluation_policies,
        )
        try:
            for index, scenario in enumerate(E2E_SCENARIOS):
                digest = contract.fixture_digests[index]
                if store.completed(digest, 0) is not None:
                    continue
                cell_root = destination / "work" / scenario
                if cell_root.exists():
                    if scenario != "normal-completion" or prior_state not in {
                        EvaluationRunStatus.PAUSED_RATE_LIMIT,
                        EvaluationRunStatus.RUNNING,
                    }:
                        raise QualificationRunError(
                            "재개할 수 없는 미완료 E2E 작업 디렉터리가 남았습니다. "
                            f"새 run root를 사용하세요: {cell_root}"
                        )
                    prepared = _restore_prepared_state(
                        cell_root,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                    )
                    verify_service_budget_policy(
                        prepared.service, prepared.project_id, evaluation_policies
                    )
                else:
                    cell_root.mkdir(parents=True)
                    workspace, copied_digest = _copy_fixture(base, cell_root)
                    if copied_digest != source_digest:
                        raise QualificationRunError("복사 직전 E2E fixture digest가 변경됐습니다.")
                    prepared = _prepare(
                        workspace=workspace,
                        state_root=cell_root / "state",
                        inventory=inventory,
                        roles=roles,
                        semantic_task_validation=scenario == "normal-completion",
                        evaluation_policies=evaluation_policies,
                    )
                    _write_prepared_state(
                        cell_root,
                        prepared,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                    )
                recorded = RecordedRuntime(
                    real_runtime, journal=cell_root / "runtime-receipts.json"
                )
                if scenario == "stored-turn-restart-resume":
                    with use_role_timeout_policy(evaluation_policies.role_timeouts):
                        cell, events = _live_restart_resume(
                            prepared,
                            source_digest,
                            cell_root=cell_root,
                            contract=contract,
                            fixture_digest=digest,
                            codex_bin=codex_bin,
                        )
                else:
                    with use_role_timeout_policy(evaluation_policies.role_timeouts):
                        if scenario == "normal-completion":
                            cell = _normal_completion(prepared, recorded, source_digest, roles=roles)
                        elif scenario == "stale-after-materialization":
                            cell = _stale_after_materialization(prepared, recorded, source_digest)
                        else:
                            cell = _unknown_receipt(prepared, recorded, source_digest)
                    events = recorded.events
                receipt = {
                    "runtime": "CodexAppServerRuntime",
                    "actual_model": roles.executor.model,
                    "actual_effort": roles.executor.effort,
                    "events": events,
                    "events_digest": sha256_digest(events),
                }
                cell.update({"scenario": scenario, "order_seed": 0})
                store.put(
                    EvaluationCellCheckpoint(
                        model_lock_format="flowmarshal-model-lock-v2",
                        contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        order_seed=0,
                        raw_structured_assessment=cell,
                        runner_receipts=(receipt,),
                    )
                )
        except Exception as error:
            status = (
                EvaluationRunStatus.PAUSED_RATE_LIMIT
                if any(
                    marker in str(error).casefold()
                    for marker in ("rate limit", "rate_limit", "usage limit", "usage_limit", "quota")
                )
                else EvaluationRunStatus.FAILED
            )
            store.set_state(status, updated_at=utc_now(), reason=str(error))
            raise
        cells = [
            store.completed(digest, 0).raw_structured_assessment  # type: ignore[union-attr]
            for digest in contract.fixture_digests
        ]
        failures = tuple(
            f"{item['scenario']}: {item.get('failure') or item.get('error') or 'FAIL'}"
            for item in cells
            if not bool(item.get("passed"))
        )
        store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
        report = ScopeQualificationReport(
            scope=contract.scope,
            contract_digest=contract.contract_digest,
            status=EvaluationRunStatus.COMPLETED,
            passed=not failures,
            metrics={
                "cell_count": len(cells),
                "passed_cell_count": sum(1 for item in cells if item.get("passed")),
                "actual_codex_cell_count": len(cells),
                "duplicate_effect_count": sum(
                    1
                    for item in cells
                    if item.get("scenario") == "unknown-receipt-no-duplicate"
                    and item.get("thread_create_count") != 1
                ),
            },
            failures=failures,
            generated_at=utc_now(),
        )
        _write_json(destination / "qualification-report.json", report)
        return destination, report
