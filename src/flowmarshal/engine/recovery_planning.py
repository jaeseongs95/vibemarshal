"""승인 경계 안에서 실패 Task subgraph만 교체하는 자동 재계획 provider.

이 모듈은 Core 권위를 대신하지 않는다. 실제 Plan 후보는 기존 `PlanExpanderAdapter`가
만들고 독립 검토는 기존 `PlanReviewerAdapter`가 수행하며, Gate·decision·활성화 판정은
Core의 `plan_gate`·`derive_candidate_decision`·`register_authorized_plan_revision`이 그대로
계산한다. 실패 Task 밖의 Task 의미·goal coverage·integration validation은 보존하고
원 Attempt·receipt·evidence는 읽지도 수정하지도 않는다.

재계획은 세 기준을 나눠 쓴다. 의미 기준은 active Plan(Task 집합·DAG, subgraph 교체 대상)이고,
계보 head는 같은 plan_id의 최신 revision(거절된 후보일 수 있다)이며, 입력은 current
State·ProjectMap이다. active Plan을 `previous_plan`으로 넘기면 Planning 검색의 고정 입력 검사가
active Plan의 base State를 요구해 앞 Task 완료 뒤의 재관측과 충돌하므로 넘기지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..canonical import sha256_digest
from .domain import (
    EngineModel,
    ModelAssignmentContract,
    ModelFallback,
    PlanContractRevision,
    PlanSkeletonCandidate,
    ProjectMapRevision,
    RoleAssignmentPolicy,
    StateSnapshot,
    derive_candidate_decision,
)
from .models import EngineRoleConfiguration, ModelInventory
from .plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V1
from .role_budget import replan_budget
from .roles import StructuredRolePort
from .runtime import REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE
from .service import EngineService

#: 원 planning reviewer와 구분되는 복구 전용 독립 검토 역할.
RECOVERY_PLAN_REVIEWER_ROLE = "recovery_plan_reviewer"


class RecoveryPlanError(RuntimeError):
    """재계획 입력이 원장 상태와 결속되지 않았을 때의 오류."""


#: review 입력 결속이 어긋났을 때 detail의 접두다. code·enum이 아니다(설계 v5.1 D1 §6).
REVIEW_INPUT_MISMATCH = "RECOVERY_REPLAN_REVIEW_INPUT_MISMATCH"


class ReplanExpansionCheckpoint(EngineModel):
    """review가 필요한 expand·refine phase의 결과. decision이 없어 이것만으로는 등록·활성화되지 않는다.

    최상위 `plan`은 retry assessment ID 파생(`recovery.current_replan_assessment`)이 읽는 자리다.
    최종 evaluation은 별도 review job의 `review_replan`만 만든다.
    """

    replan_phase: Literal["expand"]
    plan: PlanContractRevision
    deterministic_findings: tuple[()] = ()
    review_required: Literal[True]


def review_input_mismatch(cause: str, *, unknown: str) -> str:
    """review 입력 불일치 detail: 원인 → 모르는 것 → 지금 할 행동 순서다."""

    return (
        f"{REVIEW_INPUT_MISMATCH}: {cause} {unknown} "
        "review 역할과 expander를 자동으로 다시 부르지 않습니다. replan으로는 이 상태가 풀리지 않습니다. "
        + REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE
    )


def replan_expansion_mismatch(expand_row: Any) -> tuple[ReplanExpansionCheckpoint | None, str | None]:
    """소비된 expand 행의 결과를 review 입력으로 검증한다(설계 v5.1 D1 §6 (i)(ii)(iii)).

    (i) `result_json`의 canonical digest를 다시 계산해 `result_digest` 열과 대조하고, (ii) strict
    `ReplanExpansionCheckpoint`로 파싱하고, (iii) Plan 본문을 저장 모양 그대로 다시 검증한다.
    통과하면 (checkpoint, None), 어긋나면 (None, detail)이다. raise 여부는 호출자가 정한다
    (Core의 review 예약 전 reader는 typed 반환, review worker는 `RecoveryPlanError`).
    """

    job_id = expand_row["id"]
    try:
        recomputed = sha256_digest(json.loads(expand_row["result_json"]))
    except (TypeError, ValueError) as error:
        return None, review_input_mismatch(
            f"expand job {job_id}의 result_json을 JSON으로 읽을 수 없습니다({type(error).__name__}).",
            unknown="결과가 언제 어떻게 바뀌었는지는 모릅니다.",
        )
    if recomputed != expand_row["result_digest"]:
        return None, review_input_mismatch(
            f"expand job {job_id}의 result_json에서 다시 계산한 canonical digest {recomputed}가 "
            f"result_digest 열 {expand_row['result_digest']}와 다릅니다.",
            unknown="result_json과 result_digest 가운데 어느 쪽이 바뀌었는지는 모릅니다.",
        )
    try:
        checkpoint = ReplanExpansionCheckpoint.model_validate_json(expand_row["result_json"])
        PlanContractRevision.model_validate_json(checkpoint.plan.model_dump_json())
    except ValueError as error:
        first = (str(error).splitlines() or [""])[0]
        return None, review_input_mismatch(
            f"expand job {job_id}의 result_json이 ReplanExpansionCheckpoint 형식과 맞지 않습니다"
            f"({type(error).__name__}: {first[:300]}).",
            unknown="결과가 언제 어떻게 바뀌었는지는 모릅니다.",
        )
    return checkpoint, None


def _assignment(binding: Any, *, role: str) -> RoleAssignmentPolicy:
    return RoleAssignmentPolicy(
        role=role,
        preferred_model=binding.model,
        preferred_effort=binding.effort,
        allowed_fallbacks=tuple(
            ModelFallback(model=item.model, effort=item.effort)
            for item in binding.allowed_fallbacks
        ),
    )


def failed_subgraph_task_refs(
    plan: PlanContractRevision, *, failed_task_ref: str
) -> frozenset[str]:
    """실패 Task와 그 후행 Task만 교체 대상으로 계산한다."""

    ref_by_id = {item.task_id: item.task_ref for item in plan.definition.tasks}
    successors: dict[str, set[str]] = {}
    for dependency in plan.definition.dependencies:
        producer = ref_by_id[dependency.producer_task_id]
        successors.setdefault(producer, set()).add(
            ref_by_id[dependency.consumer_task_id]
        )
    reached = {failed_task_ref}
    queue = [failed_task_ref]
    while queue:
        current = queue.pop()
        for following in successors.get(current, ()):
            if following not in reached:
                reached.add(following)
                queue.append(following)
    return frozenset(reached)


def _validation_owner(plan: PlanContractRevision) -> dict[str, str | None]:
    owners: dict[str, str | None] = {}
    for task in plan.definition.tasks:
        for validation in task.validations:
            owners[validation.validation_id] = task.task_ref
    for validation in plan.definition.integration_validations:
        owners[validation.validation_id] = None
    return owners


def replace_failed_subgraph(
    *,
    previous: PlanContractRevision,
    expanded: PlanContractRevision,
    replaced_task_refs: frozenset[str],
) -> PlanContractRevision:
    """새 상세화 결과에서 실패 subgraph만 취하고 나머지 Task 계약을 보존한다.

    원장 `task_contracts.id`는 revision마다 새로 발급되므로 보존 Task도 새 ID를 받는다.
    보존 Task의 계약 의미는 직전 revision 원문을 그대로 복사하므로 완료 Task의 근거
    재사용(`task_ref` 기준)과 Goal coverage 결속은 변하지 않는다.
    """

    previous_by_ref = {item.task_ref: item for item in previous.definition.tasks}
    expanded_by_ref = {item.task_ref: item for item in expanded.definition.tasks}
    if set(previous_by_ref) != set(expanded_by_ref):
        raise RecoveryPlanError(
            "RECOVERY_REPLAN_TASK_SET_DRIFT: 재계획이 Task 집합을 바꿨습니다."
        )
    preserved_refs = set(previous_by_ref) - set(replaced_task_refs)
    if not preserved_refs:
        return expanded

    tasks = tuple(
        item
        if item.task_ref in replaced_task_refs
        else previous_by_ref[item.task_ref].model_copy(
            update={"task_id": item.task_id}
        )
        for item in expanded.definition.tasks
    )
    previous_owner = _validation_owner(previous)
    expanded_owner = _validation_owner(expanded)
    previous_coverage = {
        item.criterion_id: item for item in previous.definition.goal_coverage
    }
    coverage = []
    for item in expanded.definition.goal_coverage:
        prior = previous_coverage.get(item.criterion_id)
        merged: list[str] = []
        for validation_id in () if prior is None else prior.validation_ids:
            owner = previous_owner.get(validation_id)
            if owner is None or owner in preserved_refs:
                merged.append(validation_id)
        for validation_id in item.validation_ids:
            if expanded_owner.get(validation_id) in replaced_task_refs:
                merged.append(validation_id)
        coverage.append(
            item.model_copy(
                update={"validation_ids": tuple(dict.fromkeys(merged))}
            )
        )
    definition = expanded.definition.model_copy(
        update={
            "tasks": tasks,
            "goal_coverage": tuple(coverage),
            # 독립 Goal Test는 Task subgraph 밖의 계약이므로 직전 revision을 보존한다.
            "integration_validations": previous.definition.integration_validations,
        }
    )
    return expanded.model_copy(
        update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        }
    )


@dataclass
class RecoveryPlanProvider:
    """실패 evidence에 결속된 Plan subgraph 후보와 독립 검토를 만드는 provider."""

    service: EngineService
    runner: StructuredRolePort
    roles: EngineRoleConfiguration
    inspection_contract: str = PLAN_INSPECTION_PROVIDER_V1
    reviewer_role: str = RECOVERY_PLAN_REVIEWER_ROLE

    def _active_plan(self, project_id: str) -> tuple[PlanContractRevision, PlanSkeletonCandidate]:
        with self.service.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if project is None or project["active_plan_revision_id"] is None:
                raise RecoveryPlanError(
                    "RECOVERY_REPLAN_ACTIVE_PLAN_REQUIRED: 활성 PlanContract가 없습니다."
                )
            plan = PlanContractRevision.model_validate_json(
                connection.execute(
                    "SELECT payload_json FROM plan_revisions WHERE id=?",
                    (project["active_plan_revision_id"],),
                ).fetchone()["payload_json"]
            )
            skeleton_row = connection.execute(
                "SELECT payload_json FROM skeleton_candidates WHERE project_id=? "
                "AND candidate_digest=?",
                (project_id, plan.definition.source_skeleton_digest),
            ).fetchone()
        if skeleton_row is None:
            raise RecoveryPlanError(
                "RECOVERY_REPLAN_SKELETON_NOT_FOUND: 활성 Plan의 Skeleton 원본이 없습니다."
            )
        return plan, PlanSkeletonCandidate.model_validate_json(skeleton_row["payload_json"])

    def _bind_lineage(
        self, plan: PlanContractRevision, *, semantic_base: PlanContractRevision
    ) -> PlanContractRevision:
        """후보를 같은 plan_id 계보의 최신 revision 다음 자리(MAX+1, 최신 supersede)에 결속한다.

        `_register_plan`의 등록 규칙과 같다. State·ProjectMap은 호출자가 넘긴 입력 그대로이며
        그 밖의 고정 입력은 의미 기준(active Plan)과 같아야 한다.
        """

        for name in ("project_id", "goal_contract_digest", "planning_budget", "model_inventory_digest"):
            if getattr(plan.definition, name) != getattr(semantic_base.definition, name):
                raise RecoveryPlanError(
                    f"RECOVERY_REPLAN_FIXED_INPUT_CHANGED: 재계획이 고정 입력을 바꿨습니다: {name}"
                )
        with self.service.ledger.read() as connection:
            latest = connection.execute(
                "SELECT id, revision_no FROM plan_revisions WHERE plan_id=? "
                "ORDER BY revision_no DESC LIMIT 1",
                (semantic_base.plan_id,),
            ).fetchone()
        return PlanContractRevision.model_validate(
            plan.model_dump(mode="json") | {
                "plan_id": semantic_base.plan_id,
                "revision_no": int(latest["revision_no"]) + 1,
                "supersedes_plan_revision_id": latest["id"],
            }
        )

    def _needs_revision_source(self, project_id: str, assessment: Any) -> Any:
        """needs_revision typed basis로 기록된 재시도면 차단 후보 evaluation과 그 입력을 돌려준다.

        차단 후보의 finding 원문·evidence_refs를 역할 입력에 결속하려고 기존 bounded feedback
        (`PlanExpanderAdapter.refine`)을 쓴다. refine은 차단 후보가 결속한 State·ProjectMap에서만
        같은 Plan의 다음 revision을 만들 수 있으므로 그 입력을 digest로 읽어 함께 돌려준다.
        """

        from .runtime import replan_final_evaluation

        assessment_id = getattr(assessment, "assessment_id", None)
        if assessment_id is None:
            return None
        with self.service.ledger.read() as connection:
            event = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id=? "
                "AND event_type='recovery.replan_retry_requested' AND entity_id=?",
                (project_id, assessment_id),
            ).fetchone()
            if event is None:
                return None
            request = json.loads(event["payload_json"])
            if request["basis"]["kind"] != "candidate_needs_revision":
                return None
            # 차단 후보의 최종 evaluation은 run_once·status와 같은 reader(규칙 1~6)로 읽는다.
            blocked = replan_final_evaluation(
                connection, project_id, request["prior_assessment_id"],
            ).final
            if blocked is None or blocked.plan.plan_revision_id != request["blocked_plan_revision_id"]:
                raise RecoveryPlanError(
                    "RECOVERY_REPLAN_BASIS_NOT_FOUND: 재시도 basis의 차단 후보 결과가 원장과 다릅니다."
                )
            definition = blocked.plan.definition
            state = connection.execute(
                "SELECT payload_json FROM state_snapshots WHERE project_id=? AND snapshot_digest=?",
                (project_id, definition.base_state_snapshot_digest),
            ).fetchone()
            project_map = connection.execute(
                "SELECT payload_json FROM project_map_revisions WHERE project_id=? "
                "AND revision_digest=?",
                (project_id, definition.project_map_digest),
            ).fetchone()
        return (
            blocked,
            StateSnapshot.model_validate_json(state["payload_json"]),
            ProjectMapRevision.model_validate_json(project_map["payload_json"]),
        )

    def _budgeted_runner(self, project_id: str, goal: Any) -> Any:
        from .budget import BudgetedRoleRunner

        return BudgetedRoleRunner(
            self.runner,
            self.service,
            project_id=project_id,
            goal_id=goal.goal_id,
            goal_digest=goal.definition_digest,
        )

    def replan(
        self,
        *,
        project_id: str,
        task_id: str,
        assessment: Any,
        evidence_documents: tuple[dict[str, Any], ...] = (),
        inventory: ModelInventory,
    ) -> Any:
        """expand·refine phase: 실패 Task subgraph를 교체한 새 Plan revision을 만든다.

        역할 turn은 expander(또는 refiner) 하나다. deterministic finding이 없으면 review가 필요하므로
        decision 없는 `ReplanExpansionCheckpoint`를 돌려주고, 독립 검토는 별도 review job의
        `review_replan`이 자기 turn에서 한다(설계 v5.1 D1). deterministic finding이 있으면 review 없이
        Core 재계산 decision의 최종 evaluation이다.
        """

        del evidence_documents  # 실패 근거는 Core assessment에 이미 결속돼 있다.
        from .models import AssignmentResolver
        from .planner_roles import PlanExpanderAdapter, RuleBasedTaskAssigner
        from .planning import ExpandedPlanEvaluation, plan_gate

        self.roles.validate_inventory(inventory)
        plan, skeleton = self._active_plan(project_id)
        failed = next(
            (item for item in plan.definition.tasks if item.task_id == task_id), None
        )
        if failed is None:
            raise RecoveryPlanError(
                "RECOVERY_REPLAN_TASK_NOT_IN_ACTIVE_PLAN: 실패 Task가 활성 Plan에 없습니다."
            )
        if assessment is not None and getattr(assessment, "attempt_id", None) is None:
            raise RecoveryPlanError(
                "RECOVERY_REPLAN_ASSESSMENT_REQUIRED: 원장 assessment 결속이 필요합니다."
            )
        goal = self.service.load_active_goal(project_id)
        needs_revision = self._needs_revision_source(project_id, assessment)
        if needs_revision is None:
            state = self.service.load_current_state(project_id, goal.definition_digest)
            project_map = self.service.load_current_project_map(project_id)
        else:
            blocked, state, project_map = needs_revision

        assignment = ModelAssignmentContract(
            executor=_assignment(self.roles.executor, role="executor"),
            validator=_assignment(self.roles.validator, role="validator"),
            independence_required=True,
        )
        AssignmentResolver().resolve_contract(assignment, inventory)
        assigner = RuleBasedTaskAssigner(assignment, assignment, assignment)
        with replan_budget():
            expander = PlanExpanderAdapter(
                self._budgeted_runner(project_id, goal),
                assigner,
                model=self.roles.plan_expander.model,
                effort=self.roles.plan_expander.effort,
                allowed_fallbacks=self.roles.plan_expander.allowed_fallbacks,
                inspection_provider_contract=self.inspection_contract,
                inventory_digest=inventory.inventory_digest,
                inventory=inventory,
                cwd=Path(project_map.root),
            )
            if needs_revision is None:
                # previous_plan 없이 상세화한다. 역할 입력(payload)은 previous_plan을 쓰지 않으므로
                # 같고, 계보는 아래 `_bind_lineage`가 원장 규칙으로 결속한다.
                expanded = expander.expand(
                    candidate=skeleton, goal=goal, state=state, project_map=project_map,
                )
            else:
                proposal = expander.refine(
                    evaluation=blocked, candidate=skeleton, goal=goal, state=state,
                    project_map=project_map,
                    planning_budget=blocked.plan.definition.planning_budget,
                    allow_skeleton_revision=False,
                )
                if proposal.plan is None:
                    # ponytail: 수정 역할이 disputed·unresolved면 새 후보가 없다. 같은 차단 후보를
                    # 그대로 돌려줘 원장 변화 없이 같은 typed 차단을 재생한다. 수정 역할의
                    # 사유는 역할 receipt에만 남는다. 사유 표시가 필요하면 별도 read model로 노출한다.
                    return blocked
                expanded = proposal.plan
            replacement = self._bind_lineage(
                replace_failed_subgraph(
                    previous=plan,
                    expanded=expanded,
                    replaced_task_refs=failed_subgraph_task_refs(
                        plan, failed_task_ref=failed.task_ref
                    ),
                ),
                semantic_base=plan,
            )
            deterministic = plan_gate(
                replacement,
                source=skeleton,
                goal=goal,
                state=state,
                project_map=project_map,
            )
        if not deterministic:
            return ReplanExpansionCheckpoint(
                replan_phase="expand", plan=replacement, review_required=True,
            )
        return ExpandedPlanEvaluation(
            plan=replacement,
            deterministic_findings=deterministic,
            semantic_submissions=(),
            decision=derive_candidate_decision(
                candidate_digest=replacement.activation_digest,
                findings=deterministic,
                ratings=None,
            ),
        )

    def review_replan(
        self,
        *,
        project_id: str,
        task_id: str,
        assessment: Any,
        inventory: ModelInventory,
    ) -> Any:
        """review phase: expand checkpoint의 교체 Plan을 독립 검토해 최종 evaluation을 만든다.

        입력은 closure가 아니라 원장에서 읽는다. 이 assessment의 `:review` job request로 expand 행을
        다시 읽어 결속(project·task·assessment, expand_job_id, 재계산 digest = result_digest 열 = request 값,
        checkpoint Plan activation digest = request 값, active Goal digest = Plan goal digest)을 대조하고,
        하나라도 어긋나면 역할 호출 전에 `RecoveryPlanError`다. 최초 실행과 재시작이 같은 경로다.
        """

        from .domain import validate_reviewer_submission_evidence
        from .planner_roles import PlanReviewerAdapter
        from .planning import ExpandedPlanEvaluation, plan_review_evidence_catalog

        def mismatch(cause: str) -> RecoveryPlanError:
            return RecoveryPlanError(review_input_mismatch(
                cause, unknown="어느 쪽이 언제 바뀌었는지는 모릅니다.",
            ))

        self.roles.validate_inventory(inventory)
        assessment_id = getattr(assessment, "assessment_id", None)
        expand_key = f"replanning:{assessment_id}"
        with self.service.ledger.read() as connection:
            review_row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                (project_id, f"{expand_key}:review"),
            ).fetchone()
            request = {} if review_row is None else json.loads(review_row["request_json"])
            expand_row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE id=? AND project_id=? AND checkpoint_key=?",
                (request.get("expand_job_id"), project_id, expand_key),
            ).fetchone()
        if review_row is None or request.get("replan_phase") != "review":
            raise mismatch(f"review job {expand_key}:review의 review phase 요청이 원장에 없습니다.")
        if (
            review_row["task_id"] != task_id
            or not isinstance(request.get("assessment"), dict)
            or request["assessment"].get("assessment_id") != assessment_id
        ):
            raise mismatch(
                f"review job {review_row['id']}의 task·assessment 결속이 이 review 호출"
                f"(task {task_id}, assessment {assessment_id})과 다릅니다."
            )
        if expand_row is None or expand_row["status"] != "consumed" or expand_row["task_id"] != task_id:
            raise mismatch(
                f"review job {review_row['id']}의 expand_job_id {request.get('expand_job_id')}가 "
                f"이 assessment의 소비된 expand 행({expand_key})과 결속되지 않습니다."
            )
        expansion, detail = replan_expansion_mismatch(expand_row)
        if detail is not None:
            raise RecoveryPlanError(detail)
        if request.get("expand_result_digest") != expand_row["result_digest"]:
            raise mismatch(
                f"review request의 expand_result_digest {request.get('expand_result_digest')}가 expand 행의 "
                f"result_digest 열 {expand_row['result_digest']}(재계산 값과 같음)과 다릅니다."
            )
        plan = expansion.plan
        if request.get("activation_digest") != plan.activation_digest:
            raise mismatch(
                f"review request의 activation_digest {request.get('activation_digest')}가 expand checkpoint "
                f"Plan의 activation digest {plan.activation_digest}와 다릅니다."
            )
        goal = self.service.load_active_goal(project_id)
        if goal.definition_digest != plan.definition.goal_contract_digest:
            raise mismatch(
                f"active Goal의 definition_digest {goal.definition_digest}가 교체 Plan의 "
                f"goal_contract_digest {plan.definition.goal_contract_digest}와 다릅니다."
            )
        with self.service.ledger.read() as connection:
            state_row = connection.execute(
                "SELECT payload_json FROM state_snapshots WHERE project_id=? AND snapshot_digest=?",
                (project_id, plan.definition.base_state_snapshot_digest),
            ).fetchone()
            map_row = connection.execute(
                "SELECT payload_json FROM project_map_revisions WHERE project_id=? "
                "AND revision_digest=?",
                (project_id, plan.definition.project_map_digest),
            ).fetchone()
        if state_row is None or map_row is None:
            raise mismatch("교체 Plan이 결속한 StateSnapshot·ProjectMap이 원장에 없습니다.")
        state = StateSnapshot.model_validate_json(state_row["payload_json"])
        project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        with replan_budget():
            submission = PlanReviewerAdapter(
                self._budgeted_runner(project_id, goal),
                model=self.roles.general_reviewer.model,
                effort=self.roles.general_reviewer.effort,
                allowed_fallbacks=self.roles.general_reviewer.allowed_fallbacks,
                critical_model=self.roles.critical_reviewer.model,
                critical_effort=self.roles.critical_reviewer.effort,
                critical_allowed_fallbacks=self.roles.critical_reviewer.allowed_fallbacks,
                inspection_provider_contract=self.inspection_contract,
                inventory_digest=inventory.inventory_digest,
                inventory=inventory,
                cwd=Path(project_map.root),
            ).review(
                plan=plan,
                goal=goal,
                state=state,
                project_map=project_map,
                risk_route=self.reviewer_role,
            )
            validate_reviewer_submission_evidence(
                submission,
                evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
                known_task_refs={item.task_ref for item in plan.definition.tasks},
            )
        return ExpandedPlanEvaluation(
            plan=plan,
            deterministic_findings=(),
            semantic_submissions=(submission,),
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest,
                findings=submission.findings,
                ratings=submission.ratings if not submission.findings else None,
            ),
        )
