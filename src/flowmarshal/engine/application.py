"""명령 경계에서 쓰는 Engine 원장 조회 facade.

이 모듈은 ``EngineService``의 상태 전이 API를 호출하지 않는다. 모든 결과는
Core 원장에 이미 기록된 사실을 사용자에게 표시하기 위한 read model이다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable

from .domain import (
    AttemptRecord,
    BudgetUsageRecord,
    GoalContractRevision,
    GoalVerdict,
    PlanContractRevision,
    RecoveryAssessment,
    RuntimeIntentRecord,
    RuntimeReceipt,
)
from .models import ModelInventory
from .read_models import (
    AttemptDetail,
    DuplicateLogicalCall,
    EntityRef,
    FinalReport,
    HistoryCursor,
    IntentDetail,
    ModelBindingItem,
    ModelBindingStatus,
    ProviderCallExpectation,
    ReadPresentation,
    RecoveryStatus,
    TaskValidationRecovery,
    UsageBreakdown,
    UsageIncompleteReason,
    UsageMetric,
    UsageReconciliationPointer,
    UsageSummary,
)
from .service import EngineService


class EngineApplicationError(RuntimeError):
    """조회 인자가 원장에 없거나 결속이 깨졌을 때의 읽기 오류."""


def _metric(records: Iterable[BudgetUsageRecord], field: str) -> UsageMetric:
    # provider token usage와 elapsed latency는 독립 관측값이다. token usage가
    # unavailable이어도 terminal receipt의 latency_ms=0 또는 양수는 실측으로 보존한다.
    values = [
        getattr(item, field)
        if field == "latency_ms" or item.usage_available
        else None
        for item in records
    ]
    known = [value for value in values if value is not None]
    incomplete = len(values) - len(known)
    subtotal = sum(known)
    return UsageMetric(
        known_subtotal=subtotal,
        total=subtotal if incomplete == 0 else None,
        incomplete_call_count=incomplete,
    )


def _add_incomplete(metric: UsageMetric, count: int) -> UsageMetric:
    if count == 0:
        return metric
    return UsageMetric(
        known_subtotal=metric.known_subtotal,
        total=None,
        incomplete_call_count=metric.incomplete_call_count + count,
    )


def _breakdown(
    dimension: str,
    records: tuple[BudgetUsageRecord, ...],
    incomplete: dict[str, int] | None = None,
    *,
    latency_records: tuple[BudgetUsageRecord, ...] | None = None,
    latency_incomplete: dict[str, int] | None = None,
) -> tuple[UsageBreakdown, ...]:
    grouped: dict[str, list[BudgetUsageRecord]] = defaultdict(list)
    for item in records:
        grouped[item.stage.value if dimension == "stage" else item.role].append(item)
    incomplete = incomplete or {}
    latency_grouped: dict[str, list[BudgetUsageRecord]] = defaultdict(list)
    for item in (records if latency_records is None else latency_records):
        latency_grouped[item.stage.value if dimension == "stage" else item.role].append(item)
    latency_incomplete = latency_incomplete or {}
    result = []
    for value in sorted(set(grouped) | set(incomplete) | set(latency_grouped) | set(latency_incomplete)):
        items = grouped[value]
        latency_items = latency_grouped[value]
        missing = incomplete.get(value, 0)
        result.append(UsageBreakdown(
            dimension=dimension, value=value, logical_call_count=len(items),
            input_tokens=_add_incomplete(_metric(items, "input_tokens"), missing),
            cached_input_tokens=_add_incomplete(_metric(items, "cached_input_tokens"), missing),
            output_tokens=_add_incomplete(_metric(items, "output_tokens"), missing),
            reasoning_tokens=_add_incomplete(_metric(items, "reasoning_tokens"), missing),
            latency_ms=_add_incomplete(
                _metric(latency_items, "latency_ms"), latency_incomplete.get(value, 0)
            ),
        ))
    return tuple(result)


def _deduplicate_usage(
    records: Iterable[BudgetUsageRecord],
) -> tuple[tuple[BudgetUsageRecord, ...], tuple[DuplicateLogicalCall, ...], tuple[DuplicateLogicalCall, ...]]:
    """같은 논리 호출의 동일 receipt만 하나로 보며, 충돌은 합산하지 않는다."""
    grouped: dict[str, list[BudgetUsageRecord]] = defaultdict(list)
    for item in records:
        grouped[item.logical_call_ref].append(item)
    selected: list[BudgetUsageRecord] = []
    deduplicated: list[DuplicateLogicalCall] = []
    conflicts: list[DuplicateLogicalCall] = []
    for call_ref, items in sorted(grouped.items()):
        ordered = sorted(items, key=lambda item: (item.recorded_at, item.usage_id))
        receipts = tuple(sorted({item.runner_receipt_digest for item in ordered}))
        if len(receipts) > 1:
            conflicts.append(DuplicateLogicalCall(
                logical_call_ref=call_ref, status="conflict",
                usage_ids=tuple(item.usage_id for item in ordered), receipt_digests=receipts,
                stages=tuple(sorted({item.stage.value for item in ordered})),
                roles=tuple(sorted({item.role for item in ordered})),
            ))
            continue
        selected.append(ordered[0])
        if len(ordered) > 1:
            deduplicated.append(DuplicateLogicalCall(
                logical_call_ref=call_ref, status="deduplicated",
                usage_ids=tuple(item.usage_id for item in ordered), receipt_digests=receipts,
                stages=tuple(sorted({item.stage.value for item in ordered})),
                roles=tuple(sorted({item.role for item in ordered})),
            ))
    return tuple(selected), tuple(deduplicated), tuple(conflicts)


def summarize_usage_records(
    *,
    project_id: str,
    goal_id: str,
    goal_revision_digests: Iterable[str],
    records: Iterable[BudgetUsageRecord],
    presentation: ReadPresentation,
    expected_logical_call_refs: Iterable[str] = (),
    provider_call_expectations: Iterable[ProviderCallExpectation] = (),
    superseded_usage_ids: Iterable[str] = (),
    reconciliations: Iterable[UsageReconciliationPointer] = (),
) -> UsageSummary:
    """DB 밖에서 확보한 동일 Goal usage에도 쓸 수 있는 순수 집계 함수다."""
    superseded = tuple(sorted(set(superseded_usage_ids)))
    superseded_set = set(superseded)
    scoped = tuple(item for item in records if item.usage_id not in superseded_set)
    deduplicated_selected, deduplicated, conflicts = _deduplicate_usage(scoped)
    expected = tuple(sorted(set(expected_logical_call_refs)))
    seen = {item.logical_call_ref for item in scoped}
    provider_calls = tuple(provider_call_expectations)
    usage_ids = {item.usage_id for item in scoped}
    provider_incomplete = tuple(
        item for item in provider_calls
        if item.status != "settled" or item.usage_id is None or item.usage_id not in usage_ids
    )
    # reserved/usage_unknown call에 연결된 usage는 최종 관측 전 값이다. 합산 대상에서
    # 먼저 제외한 뒤 하나의 미확인 호출로 표시해 같은 call을 두 번 세지 않는다.
    usage_by_id = {item.usage_id: item for item in scoped}
    incomplete_usage_ids = {item.usage_id for item in provider_incomplete if item.usage_id is not None}
    # provider usage_id가 같은 logical receipt의 deduplicated 아닌 사본을 가리킬 수
    # 있다. 결제 완결성은 usage row ID가 아니라 그 논리 호출 전체에 적용한다.
    incomplete_logical_call_refs = {
        usage_by_id[usage_id].logical_call_ref
        for usage_id in incomplete_usage_ids
        if usage_id in usage_by_id
    }
    selected = tuple(
        item for item in deduplicated_selected
        if item.logical_call_ref not in incomplete_logical_call_refs
    )
    # token settlement은 아직 불완전해도 receipt에서 얻은 latency 관측은 합산할 수
    # 있다. receipt conflict는 deduplicated_selected에서 제외돼 합산하지 않는다.
    latency_records = deduplicated_selected
    missing_expected = tuple(ref for ref in expected if ref not in seen)
    reasons: list[UsageIncompleteReason] = [
        UsageIncompleteReason(
            code="EXPECTED_LOGICAL_CALL_MISSING", call_ref=ref,
            detail="예상 논리 호출에 결속된 usage 레코드가 없습니다.",
        )
        for ref in missing_expected
    ]
    reasons.extend(
        UsageIncompleteReason(
            code=("PROVIDER_CALL_NOT_SETTLED" if item.status != "settled" else "PROVIDER_CALL_USAGE_MISSING"),
            call_ref=item.call_key, stage=item.stage, role=item.role,
            detail=("provider call이 settled 상태가 아니므로 usage_id가 있어도 완결된 실측으로 취급하지 않습니다."
                    if item.status != "settled" else "settled provider call에 결속된 usage 레코드가 없습니다."),
        )
        for item in provider_incomplete
    )
    reasons.extend(
        UsageIncompleteReason(
            code="LOGICAL_CALL_RECEIPT_CONFLICT", call_ref=item.logical_call_ref,
            stage=item.stages[0] if len(item.stages) == 1 else None,
            role=item.roles[0] if len(item.roles) == 1 else None,
            detail="같은 논리 호출에 서로 다른 receipt가 있어 어느 값을 합산할지 결정할 수 없습니다.",
        )
        for item in conflicts
    )
    # selected의 미확인 record는 token _metric이 해당 record를 이미 불완전 호출로
    # 센다. 사유만 추가하고 아래의 원장 없는 호출 수에는 다시 더하지 않는다.
    # provider 미완결·receipt conflict는 token metric을 불완전하게 만들지만,
    # latency는 별도 receipt 관측이 있으면 알려진 소계를 보존한다.
    synthetic_reason_count = len(reasons)
    # provider 미완결 usage도 원시 unavailable/latency 사유는 표시한다. 다만
    # synthetic count에는 넣지 않아 provider 미완결 한 건을 두 번 세지 않는다.
    direct_reason_records = latency_records
    for item in direct_reason_records:
        if not item.usage_available:
            reasons.append(UsageIncompleteReason(
                code="USAGE_UNAVAILABLE", call_ref=item.logical_call_ref,
                stage=item.stage.value, role=item.role,
                detail=item.unavailable_reason or "provider가 이 논리 호출의 사용량을 제공하지 않았습니다.",
            ))
        if item.latency_ms is None:
            reasons.append(UsageIncompleteReason(
                code="LATENCY_UNAVAILABLE", call_ref=item.logical_call_ref,
                stage=item.stage.value, role=item.role,
                detail="이 논리 호출의 지연 시간이 관측되지 않았습니다.",
            ))
    unobserved_provider_calls = tuple(item.call_key for item in provider_incomplete)
    incomplete_by_stage: dict[str, int] = defaultdict(int)
    incomplete_by_role: dict[str, int] = defaultdict(int)
    for reason in reasons[:synthetic_reason_count]:
        if reason.stage is not None:
            incomplete_by_stage[reason.stage] += 1
        if reason.role is not None:
            incomplete_by_role[reason.role] += 1
    latency_incomplete_by_stage: dict[str, int] = defaultdict(int)
    latency_incomplete_by_role: dict[str, int] = defaultdict(int)
    latency_synthetic_count = 0

    def mark_latency_missing(*, stage: str | None = None, role: str | None = None) -> None:
        nonlocal latency_synthetic_count
        latency_synthetic_count += 1
        if stage is not None:
            latency_incomplete_by_stage[stage] += 1
        if role is not None:
            latency_incomplete_by_role[role] += 1

    # 예상 호출/receipt conflict에는 사용할 latency receipt가 없고, provider call에
    # usage record 자체가 없을 때만 provider 상태가 latency total도 불완전하게 만든다.
    for _ref in missing_expected:
        mark_latency_missing()
    for conflict in conflicts:
        mark_latency_missing(
            stage=conflict.stages[0] if len(conflict.stages) == 1 else None,
            role=conflict.roles[0] if len(conflict.roles) == 1 else None,
        )
    latency_logical_call_refs = {item.logical_call_ref for item in latency_records}
    for provider_call in provider_incomplete:
        provider_usage = (
            None if provider_call.usage_id is None
            else usage_by_id.get(provider_call.usage_id)
        )
        if (
            provider_usage is None
            or provider_usage.logical_call_ref not in latency_logical_call_refs
        ):
            mark_latency_missing(stage=provider_call.stage, role=provider_call.role)
    return UsageSummary(
        project_id=project_id, goal_id=goal_id,
        goal_revision_digests=tuple(sorted(set(goal_revision_digests))),
        logical_call_count=len(selected), expected_logical_call_refs=expected,
        provider_call_count=len(provider_calls) if provider_calls else None,
        missing_expected_logical_call_refs=missing_expected,
        conflicts=conflicts, deduplicated=deduplicated,
        incomplete_reasons=tuple(reasons),
        provider_call_expectations=provider_calls,
        provider_calls_without_usage=unobserved_provider_calls,
        superseded_usage_ids=superseded,
        reconciliations=tuple(sorted(reconciliations, key=lambda item: item.reconciliation_id)),
        input_tokens=_add_incomplete(_metric(selected, "input_tokens"), synthetic_reason_count),
        cached_input_tokens=_add_incomplete(_metric(selected, "cached_input_tokens"), synthetic_reason_count),
        output_tokens=_add_incomplete(_metric(selected, "output_tokens"), synthetic_reason_count),
        reasoning_tokens=_add_incomplete(_metric(selected, "reasoning_tokens"), synthetic_reason_count),
        latency_ms=_add_incomplete(
            _metric(latency_records, "latency_ms"), latency_synthetic_count
        ),
        by_stage=_breakdown(
            "stage", selected, incomplete_by_stage,
            latency_records=latency_records,
            latency_incomplete=latency_incomplete_by_stage,
        ),
        by_role=_breakdown(
            "role", selected, incomplete_by_role,
            latency_records=latency_records,
            latency_incomplete=latency_incomplete_by_role,
        ),
        usage_records=selected,
        entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
        error_code=("USAGE_RECEIPT_CONFLICT" if conflicts else
                    "USAGE_INCOMPLETE" if reasons else presentation.error_code),
        next_action=(
            "충돌한 논리 호출의 receipt를 원장에서 대조하십시오."
            if conflicts else "예상 호출과 provider receipt를 원장에서 대조하십시오."
            if reasons else presentation.next_action
        ),
    )


class EngineApplication:
    """Core 상태를 바꾸지 않는 typed read facade."""

    def __init__(self, service: EngineService) -> None:
        self.service = service

    def _project_row(self, project_id: str):
        with self.service.ledger.read() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise EngineApplicationError("PROJECT_NOT_FOUND")
        return row

    def _presentation(self, project_id: str, refs: Iterable[EntityRef]) -> ReadPresentation:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT sequence, id, event_hash, created_at FROM history_events "
                "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return ReadPresentation(
            entity_refs=tuple(refs),
            history_cursor=HistoryCursor(
                project_id=project_id,
                sequence=0 if row is None else int(row["sequence"]),
                event_id=None if row is None else row["id"],
                event_hash=None if row is None else row["event_hash"],
                created_at=None if row is None else row["created_at"],
            ),
        )

    def _goal_rows(self, project_id: str, goal_id: str):
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT id, revision_no, definition_digest, payload_json FROM goal_revisions "
                "WHERE project_id = ? AND goal_id = ? ORDER BY revision_no, rowid",
                (project_id, goal_id),
            ).fetchall()
        if not rows:
            raise EngineApplicationError("GOAL_NOT_FOUND")
        return rows

    def usage_summary(
        self,
        project_id: str,
        *,
        goal_id: str | None = None,
        expected_logical_call_refs: Iterable[str] = (),
    ) -> UsageSummary:
        project = self._project_row(project_id)
        if goal_id is None:
            revision_id = project["active_goal_revision_id"]
            if revision_id is None:
                raise EngineApplicationError("ACTIVE_GOAL_NOT_FOUND")
            with self.service.ledger.read() as connection:
                row = connection.execute("SELECT goal_id FROM goal_revisions WHERE id = ?", (revision_id,)).fetchone()
            if row is None:
                raise EngineApplicationError("ACTIVE_GOAL_BINDING_BROKEN")
            goal_id = row["goal_id"]
        goals = self._goal_rows(project_id, goal_id)
        digests = tuple(row["definition_digest"] for row in goals)
        placeholders = ", ".join("?" for _ in digests)
        with self.service.ledger.read() as connection:
            usage_rows = connection.execute(
                "SELECT payload_json FROM budget_usage WHERE project_id = ? "
                f"AND goal_contract_digest IN ({placeholders}) ORDER BY recorded_at, rowid",
                (project_id, *digests),
            ).fetchall()
            has_provider_calls = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'provider_calls'"
            ).fetchone() is not None
            provider_rows = () if not has_provider_calls else connection.execute(
                "SELECT id, call_key, status, role, stage, usage_id FROM provider_calls "
                "WHERE project_id = ? AND goal_id = ? AND status <> 'released' "
                "ORDER BY created_at, rowid",
                (project_id, goal_id),
            ).fetchall()
            has_reconciliations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_reconciliations'"
            ).fetchone() is not None
            reconciliation_rows = () if not has_reconciliations else connection.execute(
                "SELECT r.id, r.call_id, r.prior_usage_id, r.effective_usage_id, r.observation_digest "
                "FROM usage_reconciliations r JOIN provider_calls c ON c.id=r.call_id "
                "WHERE c.project_id=? AND c.goal_id=? ORDER BY r.created_at, r.rowid",
                (project_id, goal_id),
            ).fetchall()
        records = tuple(BudgetUsageRecord.model_validate_json(row["payload_json"]) for row in usage_rows)
        reconciliations = tuple(UsageReconciliationPointer(
            reconciliation_id=row["id"], call_id=row["call_id"], prior_usage_id=row["prior_usage_id"],
            effective_usage_id=row["effective_usage_id"], observation_digest=row["observation_digest"],
        ) for row in reconciliation_rows)
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="goal", entity_id=goal_id),
             *(EntityRef(entity_type="goal_revision", entity_id=row["id"],
                         revision_no=row["revision_no"], digest=row["definition_digest"]) for row in goals),
             *(EntityRef(entity_type="usage_reconciliation", entity_id=item.reconciliation_id,
                         digest=item.observation_digest)
               for item in reconciliations)),
        )
        return summarize_usage_records(
            project_id=project_id, goal_id=goal_id, goal_revision_digests=digests,
            records=records, presentation=presentation,
            expected_logical_call_refs=expected_logical_call_refs,
            provider_call_expectations=tuple(ProviderCallExpectation(
                provider_call_id=row["id"], call_key=row["call_key"], status=row["status"], role=row["role"],
                stage=row["stage"], usage_id=row["usage_id"],
            ) for row in provider_rows),
            superseded_usage_ids=(item.prior_usage_id for item in reconciliations),
            reconciliations=reconciliations,
        )

    def final_report(self, project_id: str, *, goal_verdict_id: str | None = None) -> FinalReport:
        self._project_row(project_id)
        query = (
            "SELECT id, plan_revision_id, goal_contract_digest, payload_json FROM goal_verdicts "
            "WHERE project_id = ? AND id = ?"
            if goal_verdict_id is not None else
            "SELECT id, plan_revision_id, goal_contract_digest, payload_json FROM goal_verdicts "
            "WHERE project_id = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1"
        )
        parameters = (project_id, goal_verdict_id) if goal_verdict_id is not None else (project_id,)
        with self.service.ledger.read() as connection:
            verdict_row = connection.execute(query, parameters).fetchone()
            if verdict_row is None:
                raise EngineApplicationError("GOAL_VERDICT_NOT_FOUND")
            plan_row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ? AND project_id = ?",
                (verdict_row["plan_revision_id"], project_id),
            ).fetchone()
            goal_row = connection.execute(
                "SELECT id, payload_json FROM goal_revisions WHERE project_id = ? AND definition_digest = ?",
                (project_id, verdict_row["goal_contract_digest"]),
            ).fetchone()
        if plan_row is None:
            raise EngineApplicationError("VERDICT_PLAN_NOT_FOUND")
        if goal_row is None:
            raise EngineApplicationError("VERDICT_GOAL_REVISION_NOT_FOUND")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        verdict = GoalVerdict.model_validate_json(verdict_row["payload_json"])
        goal = GoalContractRevision.model_validate_json(goal_row["payload_json"])
        if plan.definition.goal_contract_digest != goal.definition_digest or verdict.goal_contract_digest != goal.definition_digest:
            raise EngineApplicationError("VERDICT_GOAL_BINDING_MISMATCH")
        usage = self.usage_summary(project_id, goal_id=goal.goal_id)
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="goal_verdict", entity_id=verdict.goal_verdict_id),
             EntityRef(entity_type="plan_revision", entity_id=plan.plan_revision_id,
                       revision_no=plan.revision_no, digest=plan.activation_digest),
             EntityRef(entity_type="goal_revision", entity_id=goal.goal_revision_id,
                       revision_no=goal.revision_no, digest=goal.definition_digest)),
        )
        return FinalReport(
            project_id=project_id, goal=goal, plan=plan, verdict=verdict, usage=usage,
            ledger_history_valid=self.service.ledger.verify_history(project_id),
            entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            error_code=usage.error_code, next_action=usage.next_action,
        )

    def attempt_detail(self, attempt_id: str) -> AttemptDetail:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.*, t.task_ref FROM attempts a JOIN task_contracts t ON t.id = a.task_id "
                "WHERE a.id = ?", (attempt_id,),
            ).fetchone()
            if row is None:
                raise EngineApplicationError("ATTEMPT_NOT_FOUND")
            intent_rows = connection.execute(
                "SELECT i.*, r.payload_json AS receipt_json FROM runtime_intents i "
                "LEFT JOIN runtime_receipts r ON r.intent_id = i.id "
                "WHERE i.attempt_id = ? ORDER BY i.prepared_at, i.rowid", (attempt_id,),
            ).fetchall()
        attempt = AttemptRecord.model_validate({
            "attempt_id": row["id"], "task_id": row["task_id"],
            "execution_spec_digest": row["execution_spec_digest"], "attempt_no": row["attempt_no"],
            "kind": row["kind"], "status": row["status"],
            "binding": None if row["binding_json"] is None else json.loads(row["binding_json"]),
            "failure_class": row["failure_class"], "failure_detail": row["failure_detail"],
            "started_at": row["started_at"], "ended_at": row["ended_at"],
        })
        intents = tuple(IntentDetail(
            intent=RuntimeIntentRecord.model_validate({
                "intent_id": item["id"], "attempt_id": item["attempt_id"], "kind": item["kind"],
                "idempotency_key": item["idempotency_key"], "request_digest": item["request_digest"],
                "status": item["status"], "prepared_at": item["prepared_at"],
            }),
            receipt=None if item["receipt_json"] is None else RuntimeReceipt.model_validate_json(item["receipt_json"]),
        ) for item in intent_rows)
        presentation = self._presentation(
            row["project_id"],
            (EntityRef(entity_type="project", entity_id=row["project_id"]),
             EntityRef(entity_type="plan_revision", entity_id=row["plan_revision_id"]),
             EntityRef(entity_type="attempt", entity_id=attempt_id,
                       digest=row["execution_spec_digest"])),
        )
        next_action = (
            "원장 receipt와 provider 상태를 먼저 관측하십시오."
            if any(item.intent.status.value in {"prepared", "unknown"} for item in intents) else None
        )
        return AttemptDetail(
            project_id=row["project_id"], plan_revision_id=row["plan_revision_id"], task_ref=row["task_ref"],
            attempt=attempt, intents=intents, entity_refs=presentation.entity_refs,
            history_cursor=presentation.history_cursor,
            error_code="EXTERNAL_EFFECT_UNKNOWN" if next_action else None, next_action=next_action,
        )

    def recovery_status(self, project_id: str) -> RecoveryStatus:
        project = self._project_row(project_id)
        active_refs = []
        with self.service.ledger.read() as connection:
            for entity_type, table, revision_id, digest_column in (
                ("goal_revision", "goal_revisions", project["active_goal_revision_id"], "definition_digest"),
                ("plan_revision", "plan_revisions", project["active_plan_revision_id"], "activation_digest"),
            ):
                if revision_id is None:
                    continue
                revision = connection.execute(
                    f"SELECT revision_no,{digest_column} AS digest FROM {table} WHERE id=? AND project_id=?",
                    (revision_id, project_id),
                ).fetchone()
                if revision is None:
                    raise EngineApplicationError("ACTIVE_REVISION_BINDING_BROKEN")
                active_refs.append(EntityRef(entity_type=entity_type, entity_id=revision_id,
                                             revision_no=revision["revision_no"], digest=revision["digest"]))
            intents = connection.execute(
                "SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status IN ('prepared', 'unknown') ORDER BY i.prepared_at, i.rowid",
                (project_id,),
            ).fetchall()
            assessments = connection.execute(
                "SELECT payload_json FROM recovery_assessments WHERE project_id = ? ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
        unresolved = tuple(row["id"] for row in intents)
        validation_failure = self.service.task_validation_recovery_blocker(project_id)
        validation_recovery = (
            None if validation_failure is None
            else TaskValidationRecovery.model_validate(validation_failure)
        )
        validation_refs = () if validation_recovery is None else (
            EntityRef(entity_type="task", entity_id=validation_recovery.task_id),
            EntityRef(entity_type="attempt", entity_id=validation_recovery.attempt_id),
            EntityRef(entity_type="validation_result", entity_id=validation_recovery.validation_result_id),
            *(EntityRef(entity_type="evidence", entity_id=item) for item in validation_recovery.evidence_ids),
        )
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             *(EntityRef(entity_type="runtime_intent", entity_id=item) for item in unresolved),
             *active_refs,
             *validation_refs),
        )
        required = project["run_state"] == "recovery_required" or bool(unresolved)
        return RecoveryStatus(
            project_id=project_id, run_state=project["run_state"], recovery_reason=project["recovery_reason"],
            unresolved_intent_ids=unresolved,
            assessments=tuple(RecoveryAssessment.model_validate_json(row["payload_json"]) for row in assessments),
            task_validation_recovery=validation_recovery,
            entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            error_code=("RECOVERY_REQUIRED" if required else
                        "TASK_VALIDATION_RECOVERY_REQUIRED" if validation_recovery else None),
            next_action=("기존 intent·receipt와 provider 상태를 재개 전에 대조하십시오." if required else
                         "실패 validation과 현재 Worker 근거로 원인을 분류한 RecoveryAssessment를 명시해 재시도하십시오."
                         if validation_recovery else None),
        )

    def model_binding_status(
        self, project_id: str, *, inventory: ModelInventory | None = None,
    ) -> ModelBindingStatus:
        project = self._project_row(project_id)
        plan_id = project["active_plan_revision_id"]
        if plan_id is None:
            presentation = self._presentation(project_id, (EntityRef(entity_type="project", entity_id=project_id),))
            return ModelBindingStatus(
                project_id=project_id, plan_revision_id=None,
                observed_inventory_digest=None if inventory is None else inventory.inventory_digest,
                bindings=(), entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
                error_code="ACTIVE_PLAN_NOT_FOUND", next_action="활성 PlanContract가 필요합니다.",
            )
        with self.service.ledger.read() as connection:
            row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise EngineApplicationError("ACTIVE_PLAN_BINDING_BROKEN")
        plan = PlanContractRevision.model_validate_json(row["payload_json"])
        from .domain import TaskExecutionSpecRevision
        from .model_lock import verify_binding
        with self.service.ledger.read() as connection:
            specs = {row["task_id"]: TaskExecutionSpecRevision.model_validate_json(row["payload_json"])
                     for row in connection.execute("SELECT s.task_id,s.payload_json FROM execution_spec_revisions s "
                         "JOIN task_contracts t ON t.id=s.task_id WHERE t.plan_revision_id=? AND s.is_current=1", (plan_id,))}
        bindings = []
        for task in plan.definition.tasks:
            spec = specs.get(task.task_id)
            choices = ((task.assignment.executor, task.assignment.validator) if spec is None else
                       (spec.definition.executor, spec.definition.validator))
            for choice in choices:
                if choice is None:
                    continue
                model = choice.preferred_model if spec is None else choice.model
                effort = choice.preferred_effort if spec is None else choice.effort
                error = None
                supported = None if inventory is None else inventory.supports(model, effort)
                if inventory is not None and spec is not None:
                    try:
                        if choice.operational_binding is None:
                            raise ValueError("MODEL_BINDING_MISSING")
                        verify_binding(choice.operational_binding, inventory, role=choice.role, model=model, effort=effort)
                    except ValueError as failure:
                        error = str(failure).split(":", 1)[0]
                        supported = False
                bindings.append(ModelBindingItem(task_id=task.task_id, role=choice.role, model=model,
                    effort=effort, supported=supported, error_code=error,
                    execution_spec_digest=None if spec is None else spec.definition_digest,
                    inventory_digest=None if spec is None else choice.inventory_digest))
        bindings = tuple(bindings)
        unsupported = inventory is not None and any(item.supported is False for item in bindings)
        presentation = self._presentation(
            project_id,
            (EntityRef(entity_type="project", entity_id=project_id),
             EntityRef(entity_type="plan_revision", entity_id=plan_id,
                       revision_no=plan.revision_no, digest=plan.activation_digest)),
        )
        return ModelBindingStatus(
            project_id=project_id, plan_revision_id=plan_id,
            observed_inventory_digest=None if inventory is None else inventory.inventory_digest,
            bindings=bindings, entity_refs=presentation.entity_refs, history_cursor=presentation.history_cursor,
            error_code=("MODEL_INVENTORY_NOT_OBSERVED" if inventory is None else
                        "MODEL_BINDING_UNAVAILABLE" if unsupported else None),
            next_action=("model/list 관측 결과를 전달하십시오." if inventory is None else
                         "지원되는 model/effort로 새 binding을 준비하십시오." if unsupported else None),
        )
