"""Task 3개짜리 합성 Goal(W4 대조 검사·W5 실제 측정용 spike).

`e2e_qualification._prepare`의 1 Task 계획을 본떠 Goal·Skeleton·Plan을 등록·승인·활성화한다.
Task: add 수정(app.py) → shout 수정(text.py) → add를 쓰는 total 추가(app.py, add 수정에 의존).
각 Task의 Execution Spec 후보는 Task가 ready가 된 시점의 Project Map으로 만든다(`MultitaskProposals`).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.capabilities import CoreActionAuthority
from flowmarshal.engine.domain import (
    ApproachSignature, ApprovalClass, BehaviorPolicy, CandidateDecision, CandidateStatus, DependencyType, EffectPolicy,
    ExecutionAction, ExecutionContextNeed, ExecutionSpecProposal, GoalContractDefinition, GoalContractRevision,
    GoalCoverage, GoalCriterion, IntegrationValidationContract, MissionClass, MutationPolicy,
    PlanContractDefinition, PlanContractRevision, PlanDependency, PlanGoalCoverage, PlanSkeletonCandidate,
    RecoveryEnvelope,
    ResolvedTarget, ReviewRatings, ReviewerSubmission, RevisionStatus, RiskLevel, SkeletonDependency, SourceTrace,
    TaskContract,
    TaskKind, TaskSkeleton, ValidationContract, ValidationExecutionStep, new_id, utc_now,
)
from flowmarshal.engine.e2e_qualification import _assignment
from flowmarshal.engine.execution import ExecutionPreparation
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planning import (
    CandidateEvaluation, ExpandedPlanEvaluation, plan_review_evidence_catalog, skeleton_review_evidence_catalog,
)
from flowmarshal.engine.qualification import _profile
from flowmarshal.engine.service import EngineService

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "multitask"
REQUEST = ("app.py의 add와 text.py의 shout 버그를 고치고, add로 정수 목록의 합을 구하는 total을 app.py에 "
           "추가해 test_app.py·test_text.py·test_total.py를 통과시켜줘.")


@dataclass(frozen=True)
class Step:
    ref: str
    criterion: str
    objective: str
    write: str
    test: str
    produces: str
    consumes: tuple[str, ...] = ()


STEPS = (
    Step("task_fix_add", "ac_add", "app.py의 add 구현만 최소 수정해 두 정수의 합을 반환하게 한다.",
         "app.py", "test_app", "artifact:fixed-add"),
    Step("task_fix_shout", "ac_shout", "text.py의 shout 구현만 최소 수정해 대문자에 느낌표를 붙여 반환하게 한다.",
         "text.py", "test_text", "artifact:fixed-shout"),
    Step("task_add_total", "ac_total", "app.py에 add를 써서 정수 목록의 합을 반환하는 total(values)를 추가한다.",
         "app.py", "test_total", "artifact:total", ("artifact:fixed-add",)),
)
# (producer, consumer, type, products). 같은 프로젝트는 직렬 실행하므로 control 의존성으로 한 줄로 잇는다.
# ponytail: 독립 Task를 병렬로 두면 bare EngineDispatcher가 둘 다 먼저 materialize하고, 앞 Task 완료가
# Project Map revision을 올려 다음 Task dispatch가 STALE_EXECUTION_INPUT 예외로 멈춘다(spike 관측).
EDGES = tuple(
    [(a.ref, b.ref, DependencyType.CONTROL, ()) for a, b in zip(STEPS, STEPS[1:])]
    + [(a.ref, b.ref, DependencyType.DATA, b.consumes) for b in STEPS for a in STEPS if set(b.consumes) & {a.produces}]
)
STATEMENTS = {
    "ac_add": "add(2, 3)이 5를 반환한다.",
    "ac_shout": "shout('hi')가 'HI!'를 반환한다.",
    "ac_total": "total([1, 2, 3])이 6을 반환한다.",
}


@dataclass
class PreparedMultitask:
    service: EngineService
    project_id: str
    task_ids: dict[str, str]
    workspace: Path
    roles: Any


def goal_step(prepared: PreparedMultitask) -> ValidationExecutionStep:
    """모든 Task 완료 뒤 독립 Goal Test로 전체 unittest를 다시 실행한다."""
    return ValidationExecutionStep(
        validation_id="validation_goal", method="deterministic",
        argv=(sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"),
        working_directory=str(prepared.workspace), timeout_seconds=60, expected_exit_codes=(0,),
        required_evidence_kinds=("test",))


def git_init(workspace: Path) -> None:
    """플러그인 범위 확인은 git 저장소를 요구한다. 검증이 만드는 __pycache__는 범위 밖으로 둔다."""
    (workspace / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=fm", "-c", "user.email=fm@local", "commit", "-qm", "fixture")):
        subprocess.run(["git", "-C", str(workspace), *args], check=True)


def copy_fixture(cell_root: Path) -> Path:
    workspace = cell_root / "workspace"
    shutil.copytree(FIXTURE, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return workspace


def _ratings() -> ReviewRatings:
    return ReviewRatings(goal_fit=4, grounding=4, engineering=4, verification=4, execution_safety=4)


def prepare(*, workspace: Path, state_root: Path, inventory, roles) -> PreparedMultitask:
    ledger = SQLiteEngineLedger(state_root / "flowmarshal-engine.sqlite3", artifact_root=state_root / "artifacts")
    authority = CoreActionAuthority()
    service = EngineService(ledger, action_authority=authority)
    service.initialize()
    project_id = service.create_project(name="governance multitask spike", root=workspace)
    profile = _profile(project_id)
    service.register_profile(profile)
    digest = sha256_bytes(REQUEST.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id, source_request=REQUEST, source_request_digest=digest,
        mission_class=MissionClass.BUGFIX_STABILIZATION,
        observable_outcome="세 함수가 요구대로 동작하고 세 테스트 모듈이 통과한다.",
        hard_acceptance=tuple(
            GoalCriterion(criterion_id=key, statement=value, validation_intent="Python unittest로 실제 동작을 확인한다.",
                          trace_refs=("trace_request",))
            for key, value in STATEMENTS.items()),
        non_goals=("테스트 파일 변경",),
        source_traces=(SourceTrace(trace_id="trace_request", source_ref="user-request", statement=REQUEST,
                                   source_digest=digest),),
        effect_policy=EffectPolicy(mutation_policy=MutationPolicy.SCOPED_CHANGE,
                                   behavior_policy=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS),
        profile_definition_digest=profile.definition_digest,
    )
    goal = GoalContractRevision(goal_revision_id=new_id("goal_revision"), goal_id=new_id("goal"), revision_no=1,
                                definition=definition, definition_digest=definition.definition_digest,
                                status=RevisionStatus.READY, created_at=utc_now())
    service.register_goal(goal)
    project_map, state = service.reobserve_project(project_id)
    skeleton = PlanSkeletonCandidate(
        candidate_id=new_id("candidate"), goal_contract_digest=goal.definition_digest,
        state_signature=state.semantic_digest,
        approach=ApproachSignature(strategy_family="minimal bugfix and small addition",
                                   change_shape="two implementation files",
                                   compatibility="preserve public contracts",
                                   rollout_recovery="digest-bound validation and retry"),
        tasks=tuple(TaskSkeleton(task_ref=step.ref, kind=TaskKind.CHANGE, objective=step.objective,
                                 contributes_to=(step.criterion,), produces=(step.produces,),
                                 consumes=(f"input:{step.write}", f"input:{step.test}.py", *step.consumes))
                    for step in STEPS),
        dependencies=tuple(
            SkeletonDependency(producer_task_ref=producer, consumer_task_ref=consumer, dependency_type=kind,
                               produces=products, consumes=products)
            for producer, consumer, kind, products in EDGES),
        goal_coverage=tuple(GoalCoverage(criterion_id=step.criterion, task_refs=(step.ref,)) for step in STEPS),
        estimated_change_cost=3, estimated_context_tokens=3000,
    )
    service.record_skeleton_evaluation(CandidateEvaluation(
        candidate=skeleton,
        semantic_submission=ReviewerSubmission(
            reviewer_role="spike-skeleton-reviewer", candidate_digest=sha256_digest(skeleton), ratings=_ratings(),
            evidence_catalog_digest=sha256_digest(skeleton_review_evidence_catalog(skeleton, goal, state, project_map))),
        decision=CandidateDecision(candidate_digest=sha256_digest(skeleton), status=CandidateStatus.ADMISSIBLE,
                                   fitness_score=100, weakest_dimension="engineering"),
    ))
    assignment = _assignment(roles)
    tasks = tuple(TaskContract(
        task_id=new_id("task"), task_ref=step.ref, project_id=project_id, kind=TaskKind.CHANGE,
        objective=step.objective, goal_criterion_refs=(step.criterion,), produces=(step.produces,),
        consumes=(f"input:{step.write}", f"input:{step.test}.py", *step.consumes),
        acceptance_criteria=(f"{step.test} unittest가 종료 코드 0으로 통과한다.",),
        validations=(ValidationContract(validation_id=f"validation_{step.test}", statement=f"{step.test} unittest를 실행한다.",
                                        method="deterministic", required_evidence_kinds=("test",)),),
        risk_level=RiskLevel.LOW, approval_class=ApprovalClass.PLAN_ACTIVATION,
        recovery=RecoveryEnvelope(retryable_failure_classes=("implementation", "context", "environment")),
        assignment=assignment,
    ) for step in STEPS)
    ids = {task.task_ref: task.task_id for task in tasks}
    plan_definition = PlanContractDefinition(
        project_id=project_id, goal_contract_digest=goal.definition_digest,
        base_state_snapshot_digest=state.snapshot_digest, project_map_digest=project_map.revision_digest,
        source_skeleton_digest=sha256_digest(skeleton), tasks=tasks,
        dependencies=tuple(
            PlanDependency(producer_task_id=ids[producer], consumer_task_id=ids[consumer], dependency_type=kind,
                           products=products)
            for producer, consumer, kind, products in EDGES),
        goal_coverage=tuple(PlanGoalCoverage(criterion_id=step.criterion, task_ids=(task.task_id,),
                                             validation_ids=(f"validation_{step.test}", "validation_goal"))
                            for step, task in zip(STEPS, tasks)),
        integration_validations=(IntegrationValidationContract(
            validation_id="validation_goal", statement="모든 Task 완료 뒤 전체 unittest를 독립 재실행한다.",
            criterion_refs=tuple(STATEMENTS), method="deterministic", required_evidence_kinds=("test",)),),
        model_inventory_digest=inventory.inventory_digest,
        expected_effects=("app.py·text.py 구현 최소 수정과 total 추가",),
        prohibited_effects=("테스트 파일 또는 공개 함수 계약 변경",),
    )
    plan = PlanContractRevision(plan_revision_id=new_id("plan_revision"), plan_id=new_id("plan"), revision_no=1,
                                definition=plan_definition, definition_digest=plan_definition.definition_digest,
                                status=RevisionStatus.READY, created_at=utc_now())
    service.register_plan_evaluation(ExpandedPlanEvaluation(
        plan=plan,
        semantic_submissions=(ReviewerSubmission(
            reviewer_role="spike-plan-reviewer", candidate_digest=plan.activation_digest, ratings=_ratings(),
            evidence_catalog_digest=sha256_digest(plan_review_evidence_catalog(plan, goal, state, project_map))),),
        decision=CandidateDecision(candidate_digest=plan.activation_digest, status=CandidateStatus.ADMISSIBLE,
                                   fitness_score=100, weakest_dimension="engineering"),
    ))
    service.authorize_goal(project_id=project_id, source="합성 spike 승인",
                           capability=authority.issue_goal_authorization(
                               ledger_path=ledger.path, target=service.goal_authorization_target(project_id=project_id)))
    service.activate_plan(plan_revision_id=plan.plan_revision_id, activation_digest=plan.activation_digest,
                          source="spike")
    return PreparedMultitask(service, project_id, ids, workspace, roles)


class MultitaskProposals:
    """ready Task의 Execution Spec 후보를 그 시점 Project Map digest로 만든다(EngineDispatcher proposal_provider)."""

    def __init__(self, prepared: PreparedMultitask) -> None:
        self.prepared = prepared
        self.roles = prepared.roles  # dispatcher가 Goal Test binding에 역할 설정 digest를 결속한다.
        self.steps = {prepared.task_ids[step.ref]: step for step in STEPS}

    def prepare_task(self, *, project_id: str, task_id: str, inventory) -> ExecutionPreparation:
        step = self.steps[task_id]
        project_map, _ = self.prepared.service.reobserve_project(project_id)
        digest = {item.path: item.content_digest for item in project_map.entries}
        test = f"{step.test}.py"
        return ExecutionPreparation(proposal=ExecutionSpecProposal(
            task_id=task_id,
            context_needs=(ExecutionContextNeed(need_id="source", description="수정 대상 구현", path_hints=(step.write,)),
                           ExecutionContextNeed(need_id="test", description="고정 회귀 테스트", path_hints=(test,))),
            resolved_targets=(ResolvedTarget(target_ref="target_source", path=step.write,
                                             expected_content_digest=digest[step.write], access="write"),
                              ResolvedTarget(target_ref="target_test", path=test,
                                             expected_content_digest=digest[test], access="read")),
            actions=(ExecutionAction(action_ref="action_edit", kind="edit", description=step.objective),),
            validation_steps=(ValidationExecutionStep(
                validation_id=f"validation_{step.test}", method="deterministic",
                argv=(sys.executable, "-m", "unittest", step.test),
                working_directory=str(self.prepared.workspace), timeout_seconds=60, expected_exit_codes=(0,),
                required_evidence_kinds=("test",)),),
            resource_locks=(f"file:{self.prepared.workspace / step.write}",),
            timeout_seconds=900, idempotency_hint=f"multitask-{step.ref}",
        ))
