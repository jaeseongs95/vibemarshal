"""결정적으로 비교 가능한 승인 경계. 의미상의 범위 검토는 Plan review가 맡는다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..canonical import canonical_json
from .domain import EffectPolicy, GoalAuthorization, GoalContractRevision, MutationPolicy, PlanContractRevision


def _effects_within(approved: EffectPolicy, requested: EffectPolicy) -> bool:
    """문자열 집합과 명시 checkpoint만 비교한다. 의미상 포함 관계는 추정하지 않는다."""
    return (
        # read_only는 변경 효과를 제거한다. 다른 변경 정책 사이의 순위는 추정하지 않는다.
        (requested.mutation_policy == approved.mutation_policy
         or requested.mutation_policy is MutationPolicy.READ_ONLY)
        and requested.behavior_policy == approved.behavior_policy
        and set(requested.allowed_external_effects) <= set(approved.allowed_external_effects)
        and set(requested.prohibited_effects) >= set(approved.prohibited_effects)
        and (not approved.irreversible_effects_require_checkpoint
             or requested.irreversible_effects_require_checkpoint)
    )


def current_budget_policies(connection: Any, project_id: str) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT scope_key, payload_json FROM budget_policy_revisions b WHERE project_id = ? "
        "AND revision_no = (SELECT MAX(revision_no) FROM budget_policy_revisions "
        "WHERE project_id = b.project_id AND scope_key = b.scope_key) ORDER BY scope_key",
        (project_id,),
    ).fetchall()
    return tuple(canonical_json({"scope_key": row["scope_key"], "policy": json.loads(row["payload_json"])})
                 for row in rows)


def authorization_changes(authorization: GoalAuthorization, *, project: Any,
                          goal: GoalContractRevision, profile_digest: str,
                          plan: PlanContractRevision, budget_policies: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
    changes: list[dict[str, Any]] = []

    def changed(boundary: str, field: str, approved: Any, requested: Any) -> None:
        changes.append({"boundary": boundary, "field": field, "approved": approved,
                        "requested": requested, "evidence_ref": plan.activation_digest})

    if authorization.project_id != project["id"] or plan.definition.project_id != authorization.project_id:
        changed("project", "project_id", authorization.project_id, plan.definition.project_id)
    if Path(authorization.project_root).resolve() != Path(project["root"]).resolve():
        changed("project", "project_root", authorization.project_root, project["root"])
    effects = goal.definition.effect_policy
    effects_within = _effects_within(authorization.effect_policy, effects)
    # 효과 정책만 축소한 새 Goal도 immutable Goal/Plan review는 거친다.
    # 목표·AC·출처·Profile 등 다른 필드 변경을 이 예외로 허용하지 않는다.
    approved_effect_definition = goal.definition.model_copy(update={"effect_policy": authorization.effect_policy})
    if (authorization.goal_id != goal.goal_id
            or not effects_within
            or authorization.goal_contract_digest != approved_effect_definition.definition_digest):
        changed("goal", "goal_contract_digest", authorization.goal_contract_digest, goal.definition_digest)
    if authorization.profile_definition_digest != profile_digest:
        changed("policy", "profile_definition_digest", authorization.profile_definition_digest, profile_digest)
    if not effects_within:
        changed("effect", "effect_policy", authorization.effect_policy.model_dump(mode="json"), effects.model_dump(mode="json"))
    for task in plan.definition.tasks:
        for effect in task.expected_effects:
            if (effect.external and effect.statement not in effects.allowed_external_effects
                    or effect.statement in effects.prohibited_effects):
                changed("effect", f"{task.task_ref}.{effect.effect_id}", effects.model_dump(mode="json"), effect.model_dump(mode="json"))
    policy = authorization.operating_policy
    for field, requested in plan.definition.planning_budget.model_dump().items():
        approved = getattr(policy.planning_budget, field)
        # reserve 비율은 상한이 아니라 배분 정책이므로 동일성을 보존한다.
        if (requested != approved if field == "replan_reserve_percent" else requested > approved):
            changed("policy", f"planning_budget.{field}", approved, requested)
    for task in plan.definition.tasks:
        for field in ("max_same_failure_replans", "max_goal_replans"):
            if getattr(task.recovery, field) > getattr(policy, field):
                changed("policy", f"{task.task_ref}.recovery.{field}", getattr(policy, field), getattr(task.recovery, field))
        if policy.requires_new_evidence and not task.recovery.requires_new_evidence:
            changed("policy", f"{task.task_ref}.requires_new_evidence", True, False)
        if task.recovery.resume_strategy != policy.resume_strategy:
            changed("policy", f"{task.task_ref}.resume_strategy", policy.resume_strategy, task.recovery.resume_strategy)
    approved_budgets = {item["scope_key"]: item["policy"] for item in map(json.loads, authorization.budget_policies)}
    requested_budgets = {item["scope_key"]: item["policy"] for item in map(json.loads, budget_policies)}
    # Goal override는 project 기본값보다 우선한다. scope 추가로 effective 상한을 우회할 수 없다.
    approved = approved_budgets.get(authorization.goal_id, approved_budgets.get(""))
    requested = requested_budgets.get(authorization.goal_id, requested_budgets.get(""))
    if approved is not None:
        # 명시 token 중단 상한의 축소만 자동 허용한다. 누락/0/추정 보충은 하지 않는다.
        if requested is None or any(
            requested.get(field) != value
            and not (field == "total_tokens" and isinstance(requested.get(field), int)
                     and requested[field] <= value)
            for field, value in approved.items()
        ):
            changed("policy", f"budget.effective.{authorization.goal_id}", approved, requested)
    return tuple(changes)
