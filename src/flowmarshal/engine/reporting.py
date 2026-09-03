from __future__ import annotations

from collections import Counter

from .domain import (
    AttemptRecord,
    BudgetUsageRecord,
    CandidateDecision,
    GoalContractRevision,
    GoalVerdict,
    PlanContractRevision,
    RecoveryAssessment,
)


def render_plan_review(
    *,
    goal: GoalContractRevision,
    plan: PlanContractRevision,
    decision: CandidateDecision,
) -> str:
    lines = [
        "# FlowMarshal 계획 검토",
        "",
        f"- 목표: {goal.definition.observable_outcome}",
        f"- 활성화 digest: `{plan.activation_digest}`",
        f"- Core 판정: `{decision.status.value}`",
        f"- Task 수: {len(plan.definition.tasks)}",
        "",
        "## Task 계약",
        "",
    ]
    for index, task in enumerate(plan.definition.tasks, start=1):
        lines.extend(
            (
                f"{index}. **{task.task_ref}** — {task.objective}",
                f"   - Goal AC: {', '.join(task.goal_criterion_refs)}",
                f"   - 위험: {task.risk_level.value} / 승인: {task.approval_class.value}",
                f"   - 완료조건: {'; '.join(task.acceptance_criteria)}",
            )
        )
    lines.extend(("", "## 활성화 의미", ""))
    lines.append(
        "이 digest를 활성화하면 위 Task 목적·DAG·완료조건·효과 정책을 승인합니다. "
        "실제 파일·명령·Context Pack은 각 Task가 ready일 때 이 범위 안에서 정해집니다."
    )
    return "\n".join(lines) + "\n"


def render_failure(
    *,
    attempt: AttemptRecord,
    recovery: RecoveryAssessment,
) -> str:
    return (
        "# FlowMarshal 실패 보고\n\n"
        f"- Attempt: `{attempt.attempt_id}`\n"
        f"- Task: `{attempt.task_id}`\n"
        f"- 실패 분류: `{recovery.failure_class.value}`\n"
        f"- 권장 처리: `{recovery.action.value}`\n"
        f"- 이유: {recovery.rationale}\n"
    )


def render_final(
    *,
    goal: GoalContractRevision,
    plan: PlanContractRevision,
    verdict: GoalVerdict,
    usage: tuple[BudgetUsageRecord, ...],
) -> str:
    stages = Counter(item.stage.value for item in usage)
    known = tuple(item for item in usage if item.usage_available)
    total_input = sum(item.input_tokens for item in known)
    total_cached = sum(item.cached_input_tokens for item in known)
    total_output = sum(item.output_tokens for item in known)
    total_latency = sum(item.latency_ms for item in usage)
    cache_ratio = 0 if total_input == 0 else total_cached / total_input
    lines = [
        "# FlowMarshal 최종 보고",
        "",
        f"- Goal: {goal.definition.observable_outcome}",
        f"- Plan: `{plan.activation_digest}`",
        f"- 최종 판정: `{verdict.status.value}`",
        f"- Evidence가 결속된 Hard AC: {len(verdict.criteria)}개",
        f"- 기록된 논리 역할 호출: {len(usage)}회",
        f"- 실제 token usage가 제공된 호출: {len(known)}회",
        f"- 입력/캐시 입력/출력 token: {total_input:,} / {total_cached:,} / {total_output:,}",
        f"- 입력 캐시 비율: {cache_ratio:.2%}",
        f"- 누적 지연: {total_latency:,} ms",
        "",
        "## 단계별 호출",
        "",
    ]
    lines.extend(f"- {stage}: {count}회" for stage, count in sorted(stages.items()))
    return "\n".join(lines) + "\n"
