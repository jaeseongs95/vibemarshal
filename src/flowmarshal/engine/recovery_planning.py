"""승인 경계 안에서 실패 Task subgraph만 교체하는 자동 재계획 provider.

이 모듈은 Core 권위를 대신하지 않는다. 실제 Plan 후보는 기존 `PlanExpanderAdapter`가
만들고 독립 검토는 기존 `PlanReviewerAdapter`가 수행하며, Gate·decision·활성화 판정은
Core의 `plan_gate`·`derive_candidate_decision`·`register_authorized_plan_revision`이 그대로
계산한다. 실패 Task 밖의 Task 의미·goal coverage·integration validation은 보존하고
원 Attempt·receipt·evidence는 읽지도 수정하지도 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import (
    ModelAssignmentContract,
    ModelFallback,
    PlanContractRevision,
    PlanSkeletonCandidate,
    RoleAssignmentPolicy,
    derive_candidate_decision,
)
from .models import EngineRoleConfiguration, ModelInventory
from .plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V1
from .role_budget import replan_budget
from .roles import StructuredRolePort
from .service import EngineService

#: 원 planning reviewer와 구분되는 복구 전용 독립 검토 역할.
RECOVERY_PLAN_REVIEWER_ROLE = "recovery_plan_reviewer"


class RecoveryPlanError(RuntimeError):
    """재계획 입력이 원장 상태와 결속되지 않았을 때의 오류."""


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

    def replan(
        self,
        *,
        project_id: str,
        task_id: str,
        assessment: Any,
        evidence_documents: tuple[dict[str, Any], ...] = (),
        inventory: ModelInventory,
    ) -> Any:
        """실패 Task subgraph를 교체한 새 Plan revision과 Core 재계산 decision을 만든다."""

        del evidence_documents  # 실패 근거는 Core assessment에 이미 결속돼 있다.
        from .budget import BudgetedRoleRunner
        from .models import AssignmentResolver
        from .planner_roles import (
            PlanExpanderAdapter,
            PlanReviewerAdapter,
            RuleBasedTaskAssigner,
        )
        from .planning import (
            ExpandedPlanEvaluation,
            plan_gate,
            plan_review_evidence_catalog,
        )
        from .domain import validate_reviewer_submission_evidence

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
        state = self.service.load_current_state(project_id, goal.definition_digest)
        project_map = self.service.load_current_project_map(project_id)
        root = Path(project_map.root)

        assignment = ModelAssignmentContract(
            executor=_assignment(self.roles.executor, role="executor"),
            validator=_assignment(self.roles.validator, role="validator"),
            independence_required=True,
        )
        AssignmentResolver().resolve_contract(assignment, inventory)
        assigner = RuleBasedTaskAssigner(assignment, assignment, assignment)
        runner = BudgetedRoleRunner(
            self.runner,
            self.service,
            project_id=project_id,
            goal_id=goal.goal_id,
            goal_digest=goal.definition_digest,
        )
        common = {
            "inventory_digest": inventory.inventory_digest,
            "inventory": inventory,
            "cwd": root,
        }
        with replan_budget():
            expanded = PlanExpanderAdapter(
                runner,
                assigner,
                model=self.roles.plan_expander.model,
                effort=self.roles.plan_expander.effort,
                allowed_fallbacks=self.roles.plan_expander.allowed_fallbacks,
                inspection_provider_contract=self.inspection_contract,
                **common,
            ).expand(
                candidate=skeleton,
                goal=goal,
                state=state,
                project_map=project_map,
                previous_plan=plan,
            )
            replacement = replace_failed_subgraph(
                previous=plan,
                expanded=expanded,
                replaced_task_refs=failed_subgraph_task_refs(
                    plan, failed_task_ref=failed.task_ref
                ),
            )
            deterministic = plan_gate(
                replacement,
                source=skeleton,
                goal=goal,
                state=state,
                project_map=project_map,
            )
            submissions = ()
            if not deterministic:
                submission = PlanReviewerAdapter(
                    runner,
                    model=self.roles.general_reviewer.model,
                    effort=self.roles.general_reviewer.effort,
                    allowed_fallbacks=self.roles.general_reviewer.allowed_fallbacks,
                    critical_model=self.roles.critical_reviewer.model,
                    critical_effort=self.roles.critical_reviewer.effort,
                    critical_allowed_fallbacks=self.roles.critical_reviewer.allowed_fallbacks,
                    inspection_provider_contract=self.inspection_contract,
                    **common,
                ).review(
                    plan=replacement,
                    goal=goal,
                    state=state,
                    project_map=project_map,
                    risk_route=self.reviewer_role,
                )
                validate_reviewer_submission_evidence(
                    submission,
                    evidence_catalog=plan_review_evidence_catalog(
                        replacement, goal, state, project_map
                    ),
                    known_task_refs={
                        item.task_ref for item in replacement.definition.tasks
                    },
                )
                submissions = (submission,)
        findings = deterministic + tuple(
            finding for submission in submissions for finding in submission.findings
        )
        return ExpandedPlanEvaluation(
            plan=replacement,
            deterministic_findings=deterministic,
            semantic_submissions=submissions,
            decision=derive_candidate_decision(
                candidate_digest=replacement.activation_digest,
                findings=findings,
                ratings=submissions[0].ratings if submissions and not findings else None,
            ),
        )
