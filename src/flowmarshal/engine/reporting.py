from __future__ import annotations

from .domain import (
    AttemptRecord,
    BudgetUsageRecord,
    CandidateDecision,
    GoalContractRevision,
    GoalVerdict,
    PlanContractRevision,
    RecoveryAssessment,
)
from .application import summarize_usage_records
from .read_models import (
    ExecutionObservationSummary,
    GovernancePluginTaskObservation,
    HistoryCursor,
    ReadOnlyReportVerification,
    ReadPresentation,
    UsageSummary,
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
    usage_summary: UsageSummary | None = None,
    execution_summary: ExecutionObservationSummary | None = None,
    read_only_verification: ReadOnlyReportVerification | None = None,
    governance_plugin_tasks: tuple[GovernancePluginTaskObservation, ...] = (),
) -> str:
    if usage_summary is None:
        usage_summary = summarize_usage_records(
            project_id=goal.definition.project_id,
            goal_id=goal.goal_id,
            goal_revision_digests=(goal.definition_digest,),
            records=usage,
            presentation=ReadPresentation(
                entity_refs=(), history_cursor=HistoryCursor(
                    project_id=goal.definition.project_id, sequence=0,
                ),
            ),
        )
    total_input = usage_summary.input_tokens.total
    total_cached = usage_summary.cached_input_tokens.total
    total_output = usage_summary.output_tokens.total
    total_latency = usage_summary.latency_ms.total
    cache_ratio = None if total_input in (None, 0) or total_cached is None else total_cached / total_input
    display = lambda value: "미확인" if value is None else f"{value:,}"
    lines = [
        "# FlowMarshal 최종 보고",
        "",
        f"- Goal: {goal.definition.observable_outcome}",
        f"- Plan: `{plan.activation_digest}`",
        f"- 최종 판정: `{verdict.status.value}`",
        f"- Evidence가 결속된 Hard AC: {len(verdict.criteria)}개",
        f"- 원장 provider 호출: {display(usage_summary.provider_call_count)}회",
        f"- 소계 집계 대상 usage 호출: {usage_summary.logical_call_count}회",
        f"- 확인된 입력/캐시 입력/출력 소계: {usage_summary.input_tokens.known_subtotal:,} / {usage_summary.cached_input_tokens.known_subtotal:,} / {usage_summary.output_tokens.known_subtotal:,} token",
        f"- 입력/캐시 입력/출력 token: {display(total_input)} / {display(total_cached)} / {display(total_output)}",
        f"- 입력 캐시 비율: {'미확인' if cache_ratio is None else f'{cache_ratio:.2%}'}",
        f"- 누적 지연: {display(total_latency)} ms",
        "",
        "## Hard AC 판정과 근거",
        "",
    ]
    criterion_by_id = {item.criterion_id: item for item in verdict.criteria}
    for criterion in goal.definition.hard_acceptance:
        result = criterion_by_id.get(criterion.criterion_id)
        if result is None:
            lines.append(f"- `{criterion.criterion_id}` 미보고 — {criterion.statement}")
            continue
        evidence = ", ".join(f"`{item}`" for item in result.evidence_ids) or "없음"
        lines.append(
            f"- `{criterion.criterion_id}` `{result.status.value}` — {criterion.statement} "
            f"(evidence: {evidence}; {result.rationale})"
        )
    if read_only_verification is not None:
        lines.extend(
            (
                "",
                "## read_only 검증",
                "",
                f"- 전체 AC 포함: {'PASS' if read_only_verification.criteria_complete else 'FAIL'}",
                f"- evidence 결속: {'PASS' if read_only_verification.evidence_grounded else 'FAIL'}",
                f"- 프로젝트 source 무변경: {'PASS' if read_only_verification.source_unchanged else 'FAIL'}",
                f"- 기준/현재 source digest: `{read_only_verification.baseline_project_map_semantic_digest}` / "
                f"`{read_only_verification.observed_project_map_semantic_digest}`",
            )
        )
    lines.extend(("", "## Task별 governance plugin identity", ""))
    if governance_plugin_tasks:
        for item in governance_plugin_tasks:
            lines.append(
                f"- `{item.task_ref}`: closure="
                f"`{item.closure_tree_digest or '미관측'}`, version="
                f"`{item.plugin_version_label or '미관측'}`, changed="
                f"`{'true' if item.changed else 'false'}`, phases="
                f"`{','.join(item.changed_phases) if item.changed_phases else '없음'}`"
            )
    else:
        lines.append("- 관측된 실행 Task가 없습니다.")
    lines.extend([
        "",
        "## 단계별 소계 집계 호출",
        "",
    ])
    lines.extend(f"- {item.value}: {item.logical_call_count}회" for item in usage_summary.by_stage)
    if execution_summary is not None:
        lines.extend(("", "## 실행 관측 축", ""))
        if execution_summary.model_observations:
            for item in execution_summary.model_observations:
                observed = (
                    "미관측"
                    if item.observed_model is None or item.observed_effort is None
                    else f"{item.observed_model}/{item.observed_effort}"
                )
                lines.append(
                    f"- `{item.logical_call_ref}` {item.role}: requested="
                    f"`{item.requested_model}/{item.requested_effort}`, observed=`{observed}`"
                )
        else:
            lines.append("- model binding: provider 호출 미관측")
        missing = ", ".join(execution_summary.usage_missing_components) or "없음"
        lines.append(
            f"- usage: `{execution_summary.usage_status}`; 누락 구성요소: {missing}"
        )
        lines.append(
            f"- external effect: `{execution_summary.external_effect_status}`; "
            f"{execution_summary.external_effect_reason}"
        )
    if usage_summary.incomplete_reasons:
        lines.extend(("", "## 미확인 사용량", ""))
        lines.extend(
            f"- `{item.call_ref}` ({item.code}): {item.detail}"
            for item in usage_summary.incomplete_reasons
        )
    if usage_summary.conflicts:
        lines.extend(("", "## Usage 충돌", ""))
        lines.extend(f"- `{item.logical_call_ref}`: 서로 다른 receipt가 있어 합산하지 않았습니다."
                     for item in usage_summary.conflicts)
    return "\n".join(lines) + "\n"
