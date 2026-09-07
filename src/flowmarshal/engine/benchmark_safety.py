"""성능 cell의 원본 원장·receipt·operation trace를 읽어 안전성을 재계산한다."""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from ..canonical import sha256_digest
from .budget import BudgetManager
from .domain import BudgetUsageRecord
from .evaluation import BenchmarkCell, EvaluationCellCheckpoint
from .evaluation_budget import EvaluationPolicies
from .ledger import SQLiteEngineLedger
from .operation_trace import verify_operation_trace
from .performance_lifecycle_safety import audit_runtime_lifecycle
from .performance import (
    PerformanceSafetyCounters, PerformanceSafetyObservation, PerformanceUsageCounters,
)
from .role_observations import RoleCallReceipt
from .roles import RoleCallRequest
from .service import EngineService


FORMAT = "flowmarshal-benchmark-safety-checkpoint-v1"
_COUNTERS = tuple(PerformanceSafetyCounters.model_fields)
_TRACE_COUNTERS = ("duplicate_interrupt_count", "unapproved_retry_or_resume_count", "deadline_violation_count")


class BenchmarkSafetyError(ValueError):
    """원본 cell과 안전성 관측의 결속을 검증하지 못했다."""


def _json(value: str | None) -> Any:
    return None if value is None else json.loads(value)


def _history_valid(rows: list[dict[str, Any]]) -> bool:
    previous = None
    for sequence, row in enumerate(rows, 1):
        body = {key: row[key] for key in ("id", "project_id", "sequence", "event_type", "entity_type", "entity_id", "previous_hash", "created_at")}
        body["payload"] = _json(row["payload_json"])
        if row["sequence"] != sequence or row["previous_hash"] != previous or sha256_digest(body) != row["event_hash"]:
            return False
        previous = row["event_hash"]
    return True


def read_safety_ledger(database: Path, artifact_root: Path, project_id: str, goal_id: str) -> dict[str, Any]:
    """동일 read transaction의 원장을 투영하며 schema·상태를 수정하지 않는다."""
    if not database.is_file():
        raise BenchmarkSafetyError("PERFORMANCE_LEDGER_MISSING")
    ledger = SQLiteEngineLedger(database, artifact_root=artifact_root)
    ledger._assert_identity()
    with ledger.read() as connection:
        connection.execute("BEGIN")
        snapshot: dict[str, Any] = {
            "project_id": project_id, "goal_id": goal_id,
            "integrity": [row[0] for row in connection.execute("PRAGMA integrity_check")],
            "foreign_key_errors": [list(row) for row in connection.execute("PRAGMA foreign_key_check")],
        }
        for table in ("projects", "goal_revisions", "budget_policy_revisions", "provider_calls", "budget_usage", "budget_adjustments", "attempts", "recovery_assessments", "history_events"):
            field = "id" if table == "projects" else "project_id"
            order = "sequence" if table == "history_events" else "rowid"
            snapshot[table] = [dict(row) for row in connection.execute(
                f"SELECT * FROM {table} WHERE {field}=? ORDER BY {order}", (project_id,)
            )]
        snapshot["runtime_intents"] = [dict(row) for row in connection.execute(
            "SELECT i.* FROM runtime_intents i JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY i.rowid", (project_id,)
        )]
        snapshot["runtime_receipts"] = [dict(row) for row in connection.execute(
            "SELECT r.* FROM runtime_receipts r JOIN runtime_intents i ON i.id=r.intent_id JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY r.rowid", (project_id,)
        )]
    snapshot["history_valid"] = _history_valid(snapshot["history_events"])
    # 공개 예산 projection도 읽기 전용이며 현재 미확정 호출을 별도로 검출한다.
    snapshot["budget_status"] = BudgetManager(EngineService(ledger)).status(project_id, goal_id=goal_id).model_dump(mode="json")
    return snapshot


def capture_planning_safety_checkpoint(*, work_root: Path, implementation: str, raw: dict[str, Any], policies: EvaluationPolicies) -> dict[str, Any]:
    """새 planning cell의 원장 위치와 당시 전체 호출 집합을 고정한다."""
    if implementation == "skeleton_engine":
        goal = raw.get("goal_preparation", {}).get("goal_contract", {})
        project_id, goal_id = goal.get("project_id"), goal.get("goal_id")
        state = work_root / "budget-state"
    else:
        binding = raw.get("budget_evidence", {}).get("cell_binding", {})
        project_id, goal_id = binding.get("project_id"), binding.get("goal_id")
        state = work_root / "legacy-state" / "engine-budget"
    if not isinstance(project_id, str) or not isinstance(goal_id, str):
        raise BenchmarkSafetyError("PERFORMANCE_GOAL_LINEAGE_MISSING")
    database, artifacts = state / "flowmarshal-engine.sqlite3", state / "artifacts"
    snapshot = read_safety_ledger(database, artifacts, project_id, goal_id)
    body = {
        "format": FORMAT, "implementation": implementation,
        "workspace": str((work_root / "project").resolve()),
        "database_path": str(database.resolve()), "artifact_root": str(artifacts.resolve()),
        "project_id": project_id, "goal_id": goal_id,
        "evaluation_policy_digest": policies.policy_digest,
        "planning_ledger": snapshot, "planning_ledger_digest": sha256_digest(snapshot),
        "runner_receipt_digest": sha256_digest(raw["receipts"]),
    }
    return body | {"checkpoint_digest": sha256_digest(body)}


def _counter_values() -> dict[str, int | None]:
    return dict.fromkeys(_COUNTERS, 0)


def _increment(counts: dict[str, int | None], key: str, value: int = 1) -> None:
    if counts[key] is not None:
        counts[key] += value


def _trace_rows(payload: dict[str, Any], *, expected_call_id: str | None, thread_id: str | None, turn_ids: tuple[str, ...], trace_root: Path, policies: EvaluationPolicies, counts: dict[str, int | None], failures: list[str], missing: list[str]) -> list[dict[str, Any]]:
    trace = payload.get("operation_trace")
    if not isinstance(trace, dict) or payload.get("operation_trace_digest") != sha256_digest(trace):
        missing.append("PERFORMANCE_OPERATION_TRACE_MISSING_OR_UNBOUND")
        for key in _TRACE_COUNTERS:
            counts[key] = None
        return []
    verified = verify_operation_trace(trace)
    if not verified.valid:
        failures.extend("PERFORMANCE_TRACE_INVALID: " + item for item in verified.errors)
        return []
    manifest = verified.manifest
    # receipt는 당시 snapshot이다. 같은 호출의 현재 파일도 읽어 이후 operation을 검출한다.
    trace_ref = payload.get("operation_trace_ref")
    try:
        if not isinstance(trace_ref, str):
            raise ValueError("operation_trace_ref가 없습니다.")
        trace_path = Path(trace_ref).resolve(strict=True)
        trace_path.relative_to(trace_root.resolve())
        live = verify_operation_trace(trace_path)
        if not live.valid:
            failures.extend("PERFORMANCE_TRACE_FILE_INVALID: " + item for item in live.errors)
        if live.manifest != manifest:
            failures.append("PERFORMANCE_TRACE_FILE_RECEIPT_MISMATCH")
        if (not live.valid or not live.manifest.get("sealed")
                or live.manifest.get("pending_operation_ids")
                or live.manifest.get("missing_operation_counts")
                or live.manifest.get("rpc_coverage_missing_operation_ids")):
            missing.append("PERFORMANCE_OPERATION_TRACE_FILE_INCOMPLETE")
            for key in _TRACE_COUNTERS:
                counts[key] = None
        if live.manifest.get("post_seal_operation_ids"):
            failures.append("PERFORMANCE_POST_TERMINAL_OPERATION")
    except (OSError, ValueError):
        missing.append("PERFORMANCE_OPERATION_TRACE_FILE_MISSING_OR_UNBOUND")
        for key in _TRACE_COUNTERS:
            counts[key] = None
    if (not manifest.get("sealed") or manifest.get("pending_operation_ids")
            or manifest.get("missing_operation_counts")
            or manifest.get("rpc_coverage_missing_operation_ids")):
        missing.append("PERFORMANCE_OPERATION_TRACE_INCOMPLETE")
        for key in _TRACE_COUNTERS:
            counts[key] = None
    if manifest.get("error_operation_ids"):
        failures.append("PERFORMANCE_OPERATION_ERROR")
    if manifest.get("post_seal_operation_ids"):
        failures.append("PERFORMANCE_POST_TERMINAL_OPERATION")
    rows = trace.get("rows", [])
    if not rows:
        missing.append("PERFORMANCE_OPERATION_TRACE_EMPTY")
        return []
    context = trace.get("context", {})
    if expected_call_id is not None and context.get("call_id") != expected_call_id:
        if any(row.get("call_id") != expected_call_id for row in rows):
            failures.append("PERFORMANCE_TRACE_CALL_BINDING_MISMATCH")
    root_rows = [row for row in rows if row.get("parent_operation_id") is None]
    starts = [row for row in root_rows if row["kind"] == "start"]
    # Planning role 계약은 schema recovery 0이며 정확히 한 실제 turn을 요구한다.
    if expected_call_id is not None and (len(starts) != 1 or len(turn_ids) != 1
            or sum(row["kind"] == "create" for row in root_rows) != 1):
        _increment(counts, "unapproved_retry_or_resume_count")
    if expected_call_id is not None and not any(row["kind"] == "sdk_wait" for row in root_rows):
        missing.append("PERFORMANCE_SDK_WAIT_NOT_OBSERVED")
    rpc_limit = (5.0 if policies.role_timeouts.observation_policy is None
                 else policies.role_timeouts.observation_policy.rpc_timeout_seconds)
    for row in root_rows:
        if row.get("thread_id") != thread_id:
            failures.append("PERFORMANCE_TRACE_TURN_BINDING_MISMATCH")
        if row["kind"] in {"start", "resume", "sdk_wait", "interrupt"} and row.get("turn_id") not in turn_ids:
            failures.append("PERFORMANCE_TRACE_TURN_BINDING_MISMATCH")
        if row["kind"] == "read" and row.get("turn_id") not in (None, *turn_ids):
            failures.append("PERFORMANCE_TRACE_TURN_BINDING_MISMATCH")
    deadline_ids = set(manifest.get("deadline_exceeded_operation_ids", ())) | set(manifest.get("late_operation_ids", ()))
    for row in rows:
        deadline, elapsed = row.get("deadline_seconds"), row.get("elapsed_ms")
        if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline < 0):
            failures.append("PERFORMANCE_OPERATION_DEADLINE_INVALID")
        elif deadline is not None and isinstance(elapsed, (int, float)) and elapsed > deadline * 1000:
            deadline_ids.add(row["operation_id"])
        if row.get("category") == "rpc" and row["kind"] in {"read", "interrupt"} and (deadline is None or deadline > rpc_limit):
            deadline_ids.add(row["operation_id"])
    # 같은 RPC의 부모 logical 대기와 자식 RPC timeout을 중복 계수하지 않는다.
    covered_parents = {row.get("parent_operation_id") for row in rows if row["operation_id"] in deadline_ids}
    _increment(counts, "deadline_violation_count", len(deadline_ids - covered_parents))
    return rows


def _usage_summary(records: list[BudgetUsageRecord], *, complete: bool) -> PerformanceUsageCounters:
    if not complete or any(not item.usage_available for item in records):
        return PerformanceUsageCounters()
    inputs = sum(item.input_tokens for item in records)
    cached = sum(item.cached_input_tokens for item in records)
    return PerformanceUsageCounters(input_tokens=inputs, cached_input_tokens=cached, uncached_input_tokens=inputs-cached, output_tokens=sum(item.output_tokens for item in records))


def _project_calls(snapshot: dict[str, Any], calls: list[dict[str, Any]], *, policies: EvaluationPolicies, trace_root: Path, raw_receipts: list[dict[str, Any]] | None, implementation: str, planning: bool) -> tuple[PerformanceSafetyCounters, PerformanceUsageCounters, list[str], list[str], list[dict[str, Any]]]:
    counts = _counter_values()
    failures: list[str] = []
    missing: list[str] = []
    usages: list[BudgetUsageRecord] = []
    traces: list[dict[str, Any]] = []
    usage_by_id = {row["id"]: row for row in snapshot["budget_usage"]}
    call_ids, keys, turns = set(), set(), set()
    observed_role_receipts: list[dict[str, Any]] = []
    if snapshot.get("integrity") != ["ok"] or snapshot.get("foreign_key_errors") or not snapshot.get("history_valid"):
        failures.append("PERFORMANCE_LEDGER_INTEGRITY_INVALID")
    if not planning and not calls:
        # 비적용 cell은 호출 집합을 실제 원장에서 확인한 경우만 0을 가진다.
        return PerformanceSafetyCounters(**counts), _usage_summary([], complete=True), failures, missing, traces
    if planning and not calls:
        missing.append("PERFORMANCE_PLANNING_CALLS_MISSING")
    for call in calls:
        if call["id"] in call_ids or call["call_key"] in keys:
            _increment(counts, "duplicate_provider_call_count")
        call_ids.add(call["id"])
        keys.add(call["call_key"])
        if call["project_id"] != snapshot["project_id"] or call["goal_id"] != snapshot["goal_id"]:
            failures.append("PERFORMANCE_CALL_GOAL_LINEAGE_MISMATCH")
        request = _json(call["request_json"])
        if not isinstance(request, dict) or sha256_digest(request) != call["request_digest"]:
            failures.append("PERFORMANCE_REQUEST_DIGEST_MISMATCH")
            continue
        if call["policy_digest"] != sha256_digest(policies.budget) or call["estimated_tokens"] != policies.budget.call_reservation_tokens:
            _increment(counts, "budget_policy_violation_count")
        if call["status"] != "settled" or type(call["actual_tokens"]) is not int:
            _increment(counts, "unresolved_or_usage_unknown_count")
            missing.append("PERFORMANCE_USAGE_UNSETTLED")
        receipt = _json(call["receipt_json"])
        usage_row = usage_by_id.get(call["usage_id"])
        if usage_row is None or not isinstance(receipt, dict):
            missing.append("PERFORMANCE_USAGE_OR_RECEIPT_MISSING")
            continue
        try:
            usage = BudgetUsageRecord.model_validate_json(usage_row["payload_json"])
        except ValueError:
            failures.append("PERFORMANCE_USAGE_RECORD_INVALID")
            continue
        if (usage.usage_id != usage_row["id"] or usage.project_id != usage_row["project_id"]
                or usage.goal_contract_digest != usage_row["goal_contract_digest"]
                or usage.stage.value != usage_row["stage"]
                or usage.logical_call_ref != usage_row["logical_call_ref"]):
            failures.append("PERFORMANCE_USAGE_COLUMN_BINDING_MISMATCH")
        if (not usage.usage_available or usage.input_tokens is None or usage.output_tokens is None):
            _increment(counts, "unresolved_or_usage_unknown_count")
            missing.append("PERFORMANCE_USAGE_UNAVAILABLE")
        elif call["actual_tokens"] != usage.input_tokens + usage.output_tokens:
            failures.append("PERFORMANCE_ACTUAL_USAGE_CONFLICT")
        if usage.project_id != call["project_id"] or usage.goal_contract_digest != call["goal_contract_digest"]:
            failures.append("PERFORMANCE_USAGE_GOAL_BINDING_MISMATCH")
        usages.append(usage)
        if "role" in receipt and "call_id" in receipt:
            try:
                role_receipt = RoleCallReceipt.model_validate(receipt)
                bound_request = RoleCallRequest.model_validate(request)
                observed_role_receipts.append(role_receipt.model_dump(mode="json"))
            except ValueError:
                failures.append("PERFORMANCE_ROLE_RECEIPT_INVALID")
                continue
            if (role_receipt.call_id != call["call_key"] or role_receipt.input_digest != bound_request.request_digest
                    or role_receipt.role != call["role"] or role_receipt.model != request.get("model")
                    or role_receipt.effort != request.get("effort")
                    or role_receipt.timeout_policy_digest != policies.role_timeouts.policy_digest
                    or role_receipt.observation_policy_digest != (None if policies.role_timeouts.observation_policy is None else policies.role_timeouts.observation_policy.policy_digest)
                    or request.get("timeout_policy_digest") != role_receipt.timeout_policy_digest
                    or bound_request.timeout_seconds != policies.role_timeouts.timeout_for(call["role"])
                    or bound_request.observation_policy != policies.role_timeouts.observation_policy
                    or role_receipt.permission_profile != ":danger-full-access" or role_receipt.approval_policy != "never"):
                failures.append("PERFORMANCE_ROLE_REQUEST_RECEIPT_BINDING_MISMATCH")
            if role_receipt.schema_recovery_attempts:
                _increment(counts, "unapproved_retry_or_resume_count", role_receipt.schema_recovery_attempts)
            if (role_receipt.input_tokens, role_receipt.cached_input_tokens, role_receipt.output_tokens) != (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens):
                failures.append("PERFORMANCE_RECEIPT_USAGE_CONFLICT")
            if role_receipt.status != "succeeded":
                failures.append("PERFORMANCE_ROLE_CALL_FAILED")
            if "timeout" in role_receipt.status or role_receipt.interrupt_request_digest is not None:
                _increment(counts, "timeout_count")
            trace_payload = receipt
            expected_call_id = role_receipt.call_id
        else:
            if sha256_digest(receipt) != usage.runner_receipt_digest or usage.provider_observation != receipt:
                failures.append("PERFORMANCE_RUNTIME_USAGE_RECEIPT_MISMATCH")
            if receipt.get("terminal_status") not in {"completed", "success", "succeeded"}:
                failures.append("PERFORMANCE_RUNTIME_CALL_NOT_SUCCESSFUL")
                if receipt.get("terminal_status") == "interrupted":
                    _increment(counts, "timeout_count")
            trace_payload = receipt.get("payload", {})
            expected_call_id = None
        if usage.runner_receipt_digest != sha256_digest(receipt):
            failures.append("PERFORMANCE_USAGE_RECEIPT_DIGEST_MISMATCH")
        for turn in usage.turn_ids:
            identity = (usage.thread_id, turn)
            if identity in turns:
                _increment(counts, "duplicate_provider_call_count")
            turns.add(identity)
        traces.extend(_trace_rows(trace_payload, expected_call_id=expected_call_id, thread_id=usage.thread_id, turn_ids=usage.turn_ids, trace_root=trace_root, policies=policies, counts=counts, failures=failures, missing=missing))
    if planning and implementation == "skeleton_engine":
        try:
            original = [RoleCallReceipt.model_validate(item).model_dump(mode="json") for item in raw_receipts or []]
            if original != observed_role_receipts:
                failures.append("PERFORMANCE_ORIGINAL_ROLE_RECEIPT_COVERAGE_MISMATCH")
        except ValueError:
            failures.append("PERFORMANCE_ORIGINAL_ROLE_RECEIPT_INVALID")
    unique_rows: dict[str, dict[str, Any]] = {}
    for row in traces:
        prior = unique_rows.get(row["operation_id"])
        if prior is not None and prior != row:
            failures.append("PERFORMANCE_OPERATION_ID_CONFLICT")
        unique_rows[row["operation_id"]] = row
    interrupt_counts = Counter((row.get("thread_id"), row.get("turn_id")) for row in unique_rows.values() if row["kind"] == "interrupt" and row.get("category") == "rpc")
    _increment(counts, "duplicate_interrupt_count", sum(max(0, count-1) for count in interrupt_counts.values()))
    if planning:
        _increment(counts, "unapproved_retry_or_resume_count", sum(row["kind"] == "resume" and row.get("category") == "rpc" for row in unique_rows.values()))
    if any(value for value in counts.values() if value is not None):
        failures.append("PERFORMANCE_SAFETY_VIOLATION")
    usage_complete = len(usages) == len(calls) and not any(item.startswith("PERFORMANCE_USAGE") or "USAGE_CONFLICT" in item for item in failures+missing)
    return PerformanceSafetyCounters(**counts), _usage_summary(usages, complete=usage_complete), failures, missing, list(unique_rows.values())


def _budget_audit(snapshot: dict[str, Any], policies: EvaluationPolicies) -> list[str]:
    failures: list[str] = []
    usage_ids = [row["usage_id"] for row in snapshot["provider_calls"] if row["usage_id"] is not None]
    if len(usage_ids) != len(set(usage_ids)) or set(usage_ids) != {row["id"] for row in snapshot["budget_usage"]}:
        failures.append("PERFORMANCE_PROVIDER_USAGE_COVERAGE_MISMATCH")
    turns = [
        (usage.thread_id, turn_id)
        for row in snapshot["budget_usage"]
        for usage in (BudgetUsageRecord.model_validate_json(row["payload_json"]),)
        for turn_id in usage.turn_ids
    ]
    if len(turns) != len(set(turns)):
        failures.append("PERFORMANCE_PROVIDER_TURN_REUSED_ACROSS_STAGES")
    if snapshot["budget_adjustments"]:
        failures.append("PERFORMANCE_ESTIMATED_ADJUSTMENT_NOT_MEASURED")
    policy_rows = snapshot["budget_policy_revisions"]
    if not policy_rows or any(row["policy_digest"] != sha256_digest(policies.budget)
                              or sha256_digest(_json(row["payload_json"])) != row["policy_digest"] for row in policy_rows):
        failures.append("PERFORMANCE_BUDGET_POLICY_MISMATCH")
    cumulative = 0
    for row in snapshot["provider_calls"]:
        cap = policies.budget.total_tokens if row["stage"] == "replan" else policies.budget.total_tokens*(100-policies.budget.replan_reserve_percent)//100
        if cumulative + row["estimated_tokens"] > cap:
            failures.append("PERFORMANCE_RESERVATION_BUDGET_EXCEEDED")
        if type(row["actual_tokens"]) is int:
            cumulative += row["actual_tokens"]
    if cumulative > policies.budget.total_tokens:
        failures.append("PERFORMANCE_TOTAL_BUDGET_EXCEEDED")
    if snapshot["budget_status"].get("error_code"):
        failures.append("PERFORMANCE_BUDGET_BLOCKED")
    return failures


def observe_benchmark_safety(*, checkpoint: EvaluationCellCheckpoint, cell: BenchmarkCell, run_root: Path, policies: EvaluationPolicies, assessment_stage: str) -> tuple[PerformanceSafetyObservation, dict[str, Any]]:
    """원본 planning과 현재 lifecycle을 분리한 새 읽기 평가를 반환한다."""
    raw = checkpoint.raw_structured_assessment.get("raw", {})
    binding = raw.get("safety_checkpoint")
    failures: list[str] = []
    missing: list[str] = []
    evidence: dict[str, Any] = {"checkpoint_digest": checkpoint.checkpoint_digest, "cell_digest": sha256_digest(cell), "assessment_stage": assessment_stage}
    planning_counts = lifecycle_counts = PerformanceSafetyCounters()
    planning_usage = lifecycle_usage = PerformanceUsageCounters()
    try:
        if not isinstance(binding, dict):
            raise BenchmarkSafetyError("PERFORMANCE_SAFETY_CHECKPOINT_MISSING")
        body = dict(binding)
        digest = body.pop("checkpoint_digest", None)
        if digest != sha256_digest(body) or binding.get("format") != FORMAT or binding.get("implementation") != cell.implementation or binding.get("evaluation_policy_digest") != policies.policy_digest:
            raise BenchmarkSafetyError("PERFORMANCE_SAFETY_CHECKPOINT_BINDING_INVALID")
        if binding.get("runner_receipt_digest") != cell.runner_receipt_digest or sha256_digest(raw.get("receipts")) != cell.runner_receipt_digest or tuple(raw.get("receipts", ())) != checkpoint.runner_receipts:
            raise BenchmarkSafetyError("PERFORMANCE_ORIGINAL_RECEIPT_BINDING_INVALID")
        original = binding["planning_ledger"]
        if sha256_digest(original) != binding.get("planning_ledger_digest"):
            raise BenchmarkSafetyError("PERFORMANCE_PLANNING_LEDGER_DIGEST_MISMATCH")
        workspace = Path(binding["workspace"]).resolve()
        base = (run_root / "work" / f"seed-{cell.order_seed}" / cell.scenario_id / cell.implementation).resolve()
        workspace.relative_to(base)
        work = workspace.parent
        if workspace.name != "project" or work.parent != base:
            raise BenchmarkSafetyError("PERFORMANCE_SAFETY_WORKSPACE_MISMATCH")
        state = work / ("budget-state" if cell.implementation == "skeleton_engine" else "legacy-state/engine-budget")
        database, artifacts = Path(binding["database_path"]).resolve(), Path(binding["artifact_root"]).resolve()
        if database != state / "flowmarshal-engine.sqlite3" or artifacts != state / "artifacts":
            raise BenchmarkSafetyError("PERFORMANCE_SAFETY_LEDGER_PATH_MISMATCH")
        current = read_safety_ledger(database, artifacts, binding["project_id"], binding["goal_id"])
        if len(current["projects"]) != 1 or Path(current["projects"][0]["root"]).resolve() != workspace:
            raise BenchmarkSafetyError("PERFORMANCE_LEDGER_PROJECT_ROOT_MISMATCH")
        prior_ids = {row["id"] for row in original["provider_calls"]}
        if [row for row in current["provider_calls"] if row["id"] in prior_ids] != original["provider_calls"] or current["history_events"][:len(original["history_events"])] != original["history_events"]:
            raise BenchmarkSafetyError("PERFORMANCE_PLANNING_LEDGER_CHANGED")
        prior_usage_ids = {row["id"] for row in original["budget_usage"]}
        if [row for row in current["budget_usage"] if row["id"] in prior_usage_ids] != original["budget_usage"]:
            raise BenchmarkSafetyError("PERFORMANCE_PLANNING_USAGE_LEDGER_CHANGED")
        prior_goals = {row["id"]: row for row in original["goal_revisions"]}
        current_goals = {row["id"]: row for row in current["goal_revisions"]}
        for identifier, goal in prior_goals.items():
            observed = current_goals.get(identifier)
            if observed is None or any(observed[key] != goal[key] for key in ("project_id", "goal_id", "definition_digest", "payload_json")):
                raise BenchmarkSafetyError("PERFORMANCE_PLANNING_GOAL_LEDGER_CHANGED")
        if cell.implementation == "r31_baseline":
            from .benchmark import _verify_legacy_budget_evidence
            expected_binding = json.loads((work / "request.json").read_text(encoding="utf-8"))["budget_cell_binding"]
            _verify_legacy_budget_evidence(raw=raw, expected_binding=expected_binding, state_root=work / "legacy-state", evaluation_policies=policies, require_operation_trace=True)
        planning_counts, planning_usage, pf, pm, planning_traces = _project_calls(original, original["provider_calls"], policies=policies, trace_root=artifacts, raw_receipts=raw["receipts"], implementation=cell.implementation, planning=True)
        failures.extend(pf)
        missing.extend(pm)
        budget_failures = _budget_audit(original if assessment_stage == "planning" else current, policies)
        failures.extend(budget_failures)
        if budget_failures:
            planning_counts = planning_counts.model_copy(update={"budget_policy_violation_count": len(budget_failures)})
        evidence.update(planning_ledger=original, current_ledger=current, planning_trace_operations=planning_traces)
        lifecycle_calls = [row for row in current["provider_calls"] if row["id"] not in prior_ids]
        applicable = cell.implementation == "skeleton_engine" and cell.expected_disposition == "selected"
        if assessment_stage == "planning":
            if lifecycle_calls or current["attempts"]:
                failures.append("PERFORMANCE_EXECUTION_BEFORE_PLANNING_ASSESSMENT")
        elif not applicable:
            if lifecycle_calls or current["attempts"] or current["runtime_intents"]:
                failures.append("PERFORMANCE_UNEXPECTED_LIFECYCLE")
            else:
                lifecycle_counts = PerformanceSafetyCounters(**_counter_values())
                lifecycle_usage = _usage_summary([], complete=True)
                evidence["lifecycle_not_applicable"] = {"manifest_disposition": cell.expected_disposition, "implementation": cell.implementation, "observed_call_count": 0, "observed_attempt_count": 0}
        else:
            lifecycle_counts, lifecycle_usage, lf, lm, life_traces = _project_calls(current, lifecycle_calls, policies=policies, trace_root=artifacts, raw_receipts=None, implementation=cell.implementation, planning=False)
            failures.extend(lf)
            missing.extend(lm)
            evidence["lifecycle_trace_operations"] = life_traces
            values = lifecycle_counts.model_dump()
            _increment(values, "unknown_effect_count", sum(row["status"] in {"unknown", "reserved", "starting", "running"} for row in current["attempts"]))
            _increment(values, "unknown_effect_count", sum(row["status"] in {"prepared", "unknown"} for row in current["runtime_intents"]))
            unapproved, runtime_failures, runtime_missing, runtime_evidence = audit_runtime_lifecycle(current, life_traces)
            _increment(values, "unapproved_retry_or_resume_count", unapproved)
            failures.extend(runtime_failures)
            missing.extend(runtime_missing)
            evidence["runtime_lifecycle_audit"] = runtime_evidence
            if cell.lifecycle_observation is None or cell.lifecycle_observation.schema_version != "2.0":
                missing.append("PERFORMANCE_LIFECYCLE_NOT_OBSERVED")
            elif cell.lifecycle_observation.has_unknown_execution_effect:
                _increment(values, "unknown_effect_count")
            if not lifecycle_calls:
                missing.append("PERFORMANCE_LIFECYCLE_PROVIDER_CALLS_MISSING")
            lifecycle_counts = PerformanceSafetyCounters(**values)
            if not lifecycle_counts.passed:
                failures.append("PERFORMANCE_LIFECYCLE_SAFETY_FAILED")
    except (ValueError, KeyError, OSError, TypeError) as error:
        missing.append(f"PERFORMANCE_SAFETY_NOT_OBSERVED: {error}")
    # legacy의 보존된 출력 계약에는 passed 필드가 없다. 양쪽의 원래 의미 판정을 사용한다.
    functional = cell.disposition == cell.expected_disposition and (
        raw.get("passed") is True if cell.implementation == "skeleton_engine"
        else raw.get("disposition") == cell.expected_disposition
    )
    if not functional:
        failures.append("PERFORMANCE_FUNCTIONAL_RESULT_FAILED")
    complete = not failures and not missing and planning_counts.complete and planning_usage.complete and (assessment_stage == "planning" or lifecycle_counts.complete and lifecycle_usage.complete)
    evidence.update(failures=sorted(set(failures)), not_observed=sorted(set(missing)))
    result = PerformanceSafetyObservation(
        scenario_id=cell.scenario_id, scenario_digest=cell.scenario_digest, order_seed=cell.order_seed,
        implementation=cell.implementation, cell_digest=sha256_digest(cell), source_evidence_digest=sha256_digest(evidence),
        assessment_stage=assessment_stage, complete=complete, functional_passed=functional,
        safety_passed=not failures and not missing, failures=tuple(sorted(set(failures))), not_observed=tuple(sorted(set(missing))),
        planning_counters=planning_counts, lifecycle_counters=lifecycle_counts, planning_usage=planning_usage, lifecycle_usage=lifecycle_usage,
    )
    return result, evidence
