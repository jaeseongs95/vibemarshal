"""중립 입력으로 두 planner를 순차 호출하고 완료 cell만 보존한다."""
from __future__ import annotations

import json
import os
import random
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .benchmark_candidate_costs import candidate_output_costs
from .benchmark_lifecycle import collect_lifecycle_observation
from .domain import (
    BehaviorPolicy,
    EffectPolicy,
    EngineModel,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCriterion,
    MissionClass,
    MutationPolicy,
    PlanContractRevision,
    RevisionStatus,
    SourceTrace,
    TaskExecutionSpecRevision,
    new_id,
    utc_now,
)
from .evaluation import (
    BenchmarkCell, BenchmarkLifecycleObservation, BenchmarkTaskLifecycleObservation,
    EvaluationCellCheckpoint, EvaluationContract, EvaluationRunStatus, ImmutableCheckpointStore,
    TokenLatencyGateReport, evaluate_token_latency_gate,
)
from .evaluation_budget import (
    EvaluationPolicies,
    policy_contract_fragment,
    write_immutable_run_metadata,
)
from .ledger import SQLiteEngineLedger
from .models import EngineRoleConfiguration
from .model_lock import ModelInventory
from .qualification import (
    ORDER_SEEDS, PlanningScenarioCatalog, QualificationRunError, ScopeQualificationReport,
    _default_run_root, _is_rate_limit, _manifest, _planning_cell, _planning_contract, _preflight,
    _profile, _role_progress, _write_json, default_role_configuration, project_root, source_manifest_digest,
)
from .roles import RoleCallReceipt, RoleCallRequest, StructuredRoleError
from .runtime import CodexAppServerRuntime


MEASUREMENT_RULES = {
    "version": "neutral-lifecycle-qualified-window-v3",
    "token": "uncached_input_plus_output; six scenarios including expected blocks",
    "latency": "monotonic wall time to Core-admitted feasible Plan; blocks recorded separately",
    "detail": (
        "all materialized ExecutionSpec revisions in the same Goal lineage; failed executions "
        "count as executed, unknown effects remain null; require final validation and State evidence"
    ),
    "discarded_output": (
        "exact expander/refiner call-receipt-output-Plan digest bindings; exclude Skeleton-only "
        "and disputed responses from candidate output; require lifecycle-observed selected cells"
    ),
    "order": "seed-shuffled scenario/implementation cells executed serially",
    "thresholds": {"multi": .30, "overall": .20, "single_regression": .05,
                   "unexecuted_detail": .10, "discarded_output": .25, "first_feasible": .20},
}

IMPLEMENTATION_RUNTIME_CONTRACT = {
    "skeleton_engine": {"ephemeral_threads": False, "max_schema_recovery_attempts": 0},
    "r31_baseline": {"ephemeral_threads": True, "max_schema_recovery_attempts": 0},
}

LEGACY_ROLE_STAGES = {
    "purpose_resolver": {"goal_normalization"},
    "intent_reviewer": {"goal_review"},
    "candidate_generator": {"skeleton_generation", "plan_expansion", "replan"},
    "hard_gate_reviewer": {"plan_review"},
    "critical_reviewer": {"plan_review"},
    "scorer_selector": {"plan_review"},
}


class BenchmarkRunReport(EngineModel):
    contract_digest: str
    status: EvaluationRunStatus
    completed_cell_count: int
    expected_cell_count: int = 36
    passed: bool
    failures: tuple[str, ...] = ()
    token_latency: TokenLatencyGateReport | None = None

    @property
    def report_digest(self):
        return sha256_digest(self)


def neutral_input(root: Path, scenario) -> dict[str, Any]:
    fixture = root / "tests/fixtures/engine/live-smoke-project"
    files = [{"path": path.relative_to(fixture).as_posix(), "content": path.read_text(encoding="utf-8"),
              "digest": sha256_bytes(path.read_bytes())}
             for path in sorted(fixture.rglob("*")) if path.is_file() and "__pycache__" not in path.parts]
    return {"source_request": scenario.source_request, "files": files,
            "profile": _profile("project_" + "0" * 32).definition.model_dump(mode="json")}


def _legacy_hard_timeout_contract(policies: EvaluationPolicies) -> dict[str, Any]:
    maximum_calls = policies.budget.total_tokens // policies.budget.call_reservation_tokens
    if maximum_calls < 1:
        raise QualificationRunError("LEGACY_BUDGET_CANNOT_RESERVE_FIRST_CALL")
    role_timeouts = (
        policies.role_timeouts.default_timeout_seconds,
        *(item.timeout_seconds for item in policies.role_timeouts.overrides),
    )
    body = {
        "derivation": "floor(total_tokens/call_reservation_tokens)*max(role_timeout_seconds)",
        "budget_policy_digest": sha256_digest(policies.budget),
        "role_timeout_policy_digest": policies.role_timeouts.policy_digest,
        "maximum_reserved_calls": maximum_calls,
        "maximum_role_timeout_seconds": max(role_timeouts),
        "timeout_seconds": maximum_calls * max(role_timeouts),
    }
    return body | {"policy_digest": sha256_digest(body)}


def _hidden_windows_options() -> dict[str, Any]:
    if os.name != "nt":
        return {"start_new_session": True}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": startup,
        "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    }


def _observe_timeout_ledger(arguments: list[str]) -> list[dict[str, Any]]:
    if "--request-file" not in arguments:
        return []
    request_path = Path(arguments[arguments.index("--request-file") + 1])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        state_root = Path(request["state_root"]) / "engine-budget"
        database = state_root / "flowmarshal-engine.sqlite3"
        if not database.is_file():
            return []
        ledger = SQLiteEngineLedger(database, artifact_root=state_root / "artifacts")
        with ledger.read() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id,project_id,goal_id,call_key,request_digest,status,actual_tokens "
                "FROM provider_calls ORDER BY created_at,id"
            ).fetchall()]
    except Exception as error:
        return [{"observation_error": f"{type(error).__name__}: {error}"}]


def _legacy_process(
    root: Path,
    arguments: list[str],
    *,
    timeout_contract: dict[str, Any],
) -> None:
    expected_digest = timeout_contract.get("policy_digest")
    body = dict(timeout_contract)
    body.pop("policy_digest", None)
    timeout_seconds = timeout_contract.get("timeout_seconds")
    if expected_digest != sha256_digest(body) or not isinstance(timeout_seconds, (int, float)) \
            or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise QualificationRunError("LEGACY_PROCESS_TIMEOUT_POLICY_INVALID")
    command = [sys.executable, "-m", "flowmarshal.benchmark_legacy", *arguments]
    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_windows_options(),
    )
    started = time.monotonic()
    try:
        _stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        termination: dict[str, Any] = {
            "method": None,
            "error": None,
            "tree_termination_request_succeeded": False,
        }
        try:
            if os.name == "nt":
                helper = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                    **_hidden_windows_options(),
                )
                termination.update(
                    method="taskkill-tree",
                    helper_returncode=helper.returncode,
                    helper_stdout=helper.stdout[-2000:],
                    helper_stderr=helper.stderr[-2000:],
                    tree_termination_request_succeeded=helper.returncode == 0,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
                termination["method"] = "kill-process-group"
                termination["tree_termination_request_succeeded"] = True
        except BaseException as terminate_error:
            termination["error"] = f"{type(terminate_error).__name__}: {terminate_error}"
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                termination["post_kill_wait_timeout"] = True
            except BaseException as communicate_error:
                termination["post_kill_wait_error"] = (
                    f"{type(communicate_error).__name__}: {communicate_error}"
                )
        evidence_body = {
            "format": "flowmarshal-legacy-process-timeout-v1",
            "timeout_contract": timeout_contract,
            "command_digest": sha256_digest(command),
            "pid": process.pid,
            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
            "termination": termination,
            "termination_observed": (
                process.poll() is not None
                and termination["tree_termination_request_succeeded"] is True
            ),
            "returncode": process.poll(),
            "provider_calls_after_termination": _observe_timeout_ledger(arguments),
        }
        evidence = evidence_body | {"evidence_digest": sha256_digest(evidence_body)}
        if "--output" in arguments:
            output = Path(arguments[arguments.index("--output") + 1])
            timeout_path = output.with_name(output.stem + "-process-timeout.json")
            _write_json(timeout_path, evidence)
        else:
            timeout_path = None
        raise QualificationRunError(
            "LEGACY_PROCESS_TIMEOUT: process tree 종료 관측="
            f"{evidence['evidence_digest']} artifact={timeout_path}"
        ) from error
    if process.returncode:
        raise QualificationRunError("독립 legacy benchmark harness 실패: " + stderr[-5000:])


_EFFORT_ORDER = {
    name: index for index, name in enumerate(
        ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    )
}


def _legacy_inventory_digest(inventory: Any) -> str:
    raw = inventory.raw_response
    if raw is None:
        projected = [{
            "model_id": item.model,
            "display_name": item.model,
            "supported_efforts": sorted(
                item.supported_efforts,
                key=lambda value: (_EFFORT_ORDER.get(value, len(_EFFORT_ORDER)), value),
            ),
            "is_default": False,
        } for item in inventory.models]
    else:
        projected = []
        for row in raw["data"]:
            if row.get("hidden", False):
                continue
            efforts = row.get("supported_reasoning_efforts", row.get("supportedReasoningEfforts"))
            projected.append({
                "model_id": row["id"],
                "display_name": row.get("display_name", row.get("displayName", row["id"])),
                "supported_efforts": sorted(
                    (item.get("reasoning_effort", item.get("reasoningEffort")) for item in efforts),
                    key=lambda value: (_EFFORT_ORDER.get(value, len(_EFFORT_ORDER)), value),
                ),
                "is_default": bool(row.get("is_default", row.get("isDefault", False))),
            })
    return sha256_digest(sorted(projected, key=lambda item: item["model_id"]))


def _resolve_codex_executable(codex_bin: str | None) -> Path:
    if codex_bin is not None:
        return Path(codex_bin).resolve(strict=True)
    from openai_codex.client import CodexConfig, _resolve_codex_bin
    return _resolve_codex_bin(CodexConfig()).resolve(strict=True)


def _stable_entity(prefix: str, value: Any) -> str:
    return prefix + "_" + sha256_digest(value)[7:39]


def _legacy_budget_goal(
    *, project_id: str, profile_digest: str, cell_scope: dict[str, Any]
) -> GoalContractRevision:
    source = canonical_json(cell_scope)
    source_digest = sha256_bytes(source.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=source,
        source_request_digest=source_digest,
        mission_class=MissionClass.ANALYSIS_AUDIT,
        observable_outcome="동결 R3.1 benchmark cell의 역할 호출 비용과 결과를 관측한다.",
        hard_acceptance=(GoalCriterion(
            criterion_id="ac_legacy_budget",
            statement="모든 실제 legacy 역할 turn이 호출 전 예약되고 원시 usage에 따라 정산된다.",
            validation_intent="SQLite provider call, 역할 receipt와 R3.1 turn ID를 대조한다.",
            trace_refs=("trace_legacy_cell",),
        ),),
        source_traces=(SourceTrace(
            trace_id="trace_legacy_cell",
            source_ref="qualification:legacy-budget-cell",
            statement=source,
            source_digest=source_digest,
        ),),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy.READ_ONLY,
            behavior_policy=BehaviorPolicy.NOT_APPLICABLE,
        ),
        profile_definition_digest=profile_digest,
    )
    goal_id = _stable_entity("goal", cell_scope)
    return GoalContractRevision(
        goal_revision_id=_stable_entity("goal_revision", cell_scope),
        goal_id=goal_id,
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )


def _legacy_budget_cell_binding(
    *, contract_digest: str, fixture_digest: str, scenario: Any, seed: int,
    neutral_input_digest: str, project_id: str, goal: GoalContractRevision,
    evaluation_policies: EvaluationPolicies, roles: EngineRoleConfiguration,
    inventory: Any, legacy_role_map: dict[str, str],
    legacy_inventory_digest: str, codex_executable_digest: str,
    timeout_contract: dict[str, Any],
) -> dict[str, Any]:
    configured_roles = roles.model_dump(mode="json")
    selected_roles = {
        name: {"model": value["model"], "effort": value["effort"]}
        for name, value in configured_roles.items()
    }
    legacy_roles = {
        role: selected_roles[binding] for role, binding in legacy_role_map.items()
    }
    return {
        "format": "flowmarshal-legacy-budget-cell-v1",
        "evaluation_contract_digest": contract_digest,
        "fixture_digest": fixture_digest,
        "scenario_id": scenario.scenario_id,
        "scenario_digest": scenario.scenario_digest,
        "order_seed": seed,
        "implementation": "r31_baseline",
        "neutral_input_digest": neutral_input_digest,
        "project_id": project_id,
        "goal_id": goal.goal_id,
        "goal_contract_digest": goal.definition_digest,
        "evaluation_policy_digest": evaluation_policies.policy_digest,
        "budget_policy_digest": sha256_digest(evaluation_policies.budget),
        "role_timeout_policy_digest": evaluation_policies.role_timeouts.policy_digest,
        "role_configuration_digest": roles.configuration_digest,
        "expected_engine_role_bindings": selected_roles,
        "expected_role_bindings": legacy_roles,
        "expected_engine_inventory": inventory.model_dump(mode="json"),
        "engine_inventory_digest": inventory.inventory_digest,
        "legacy_inventory_digest": legacy_inventory_digest,
        "codex_executable_digest": codex_executable_digest,
        "parent_hard_timeout_contract": timeout_contract,
        "parent_hard_timeout_seconds": timeout_contract["timeout_seconds"],
        "parent_hard_timeout_policy_digest": timeout_contract["policy_digest"],
        **IMPLEMENTATION_RUNTIME_CONTRACT["r31_baseline"],
    }


def _verify_legacy_budget_evidence(
    *, raw: dict[str, Any], expected_binding: dict[str, Any], state_root: Path,
    evaluation_policies: EvaluationPolicies,
) -> None:
    try:
        expected_inventory = ModelInventory.model_validate(
            expected_binding["expected_engine_inventory"]
        )
        timeout_contract = expected_binding["parent_hard_timeout_contract"]
        timeout_body = dict(timeout_contract)
        timeout_digest = timeout_body.pop("policy_digest")
        engine_roles = expected_binding["expected_engine_role_bindings"]
        if (
            expected_binding["engine_inventory_digest"] != expected_inventory.inventory_digest
            or expected_binding["codex_executable_digest"] != expected_inventory.executable_digest
            or any(
                not expected_inventory.supports(item["model"], item["effort"])
                for item in engine_roles.values()
            )
            or timeout_digest != sha256_digest(timeout_body)
            or timeout_digest != expected_binding["parent_hard_timeout_policy_digest"]
            or timeout_body["timeout_seconds"] != expected_binding["parent_hard_timeout_seconds"]
            or timeout_body["role_timeout_policy_digest"]
            != evaluation_policies.role_timeouts.policy_digest
            or timeout_body["budget_policy_digest"] != sha256_digest(evaluation_policies.budget)
        ):
            raise ValueError("binding mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise QualificationRunError("LEGACY_BUDGET_PARENT_BINDING_INVALID") from error
    evidence = raw.get("budget_evidence")
    if not isinstance(evidence, dict):
        raise QualificationRunError("LEGACY_BUDGET_EVIDENCE_MISSING")
    body = dict(evidence)
    observed_digest = body.pop("evidence_digest", None)
    if observed_digest != sha256_digest(body):
        raise QualificationRunError("LEGACY_BUDGET_EVIDENCE_DIGEST_MISMATCH")
    if (
        body.get("format") != "flowmarshal-legacy-budget-evidence-v1"
        or body.get("cell_binding") != expected_binding
        or body.get("cell_binding_digest") != sha256_digest(expected_binding)
        or body.get("history_valid") is not True
        or body.get("turn_coverage_complete") is not True
    ):
        raise QualificationRunError("LEGACY_BUDGET_EVIDENCE_BINDING_MISMATCH")
    calls = body.get("calls")
    if not isinstance(calls, list) or not calls:
        raise QualificationRunError("LEGACY_BUDGET_CALL_EVIDENCE_MISSING")
    raw_receipts = raw.get("receipts")
    if not isinstance(raw_receipts, list) or not raw_receipts or len(raw_receipts) != len(calls):
        raise QualificationRunError("LEGACY_BUDGET_RAW_RECEIPT_CARDINALITY_MISMATCH")
    journal = body.get("journal")
    if not isinstance(journal, list) or len(journal) != len(calls):
        raise QualificationRunError("LEGACY_BUDGET_JOURNAL_INCOMPLETE")
    database = state_root / "engine-budget" / "flowmarshal-engine.sqlite3"
    if not database.is_file():
        raise QualificationRunError("LEGACY_BUDGET_LEDGER_MISSING")
    ledger = SQLiteEngineLedger(
        database, artifact_root=state_root / "engine-budget" / "artifacts"
    )
    if not ledger.verify_history(expected_binding["project_id"]):
        raise QualificationRunError("LEGACY_BUDGET_HISTORY_INVALID")
    with ledger.read() as connection:
        rows = connection.execute(
            "SELECT * FROM provider_calls WHERE project_id=? AND goal_id=? ORDER BY created_at,id",
            (expected_binding["project_id"], expected_binding["goal_id"]),
        ).fetchall()
    projected = [{
        "provider_call_id": row["id"],
        "call_key": row["call_key"],
        "goal_contract_digest": row["goal_contract_digest"],
        "role": row["role"],
        "stage": row["stage"],
        "request_digest": row["request_digest"],
        "request": json.loads(row["request_json"]),
        "policy_digest": row["policy_digest"],
        "status": row["status"],
        "actual_tokens": row["actual_tokens"],
        "receipt": None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
    } for row in rows]
    if projected != calls:
        raise QualificationRunError("LEGACY_BUDGET_LEDGER_EVIDENCE_MISMATCH")
    expected_policy_digest = sha256_digest(evaluation_policies.budget)
    budget_turns: set[str] = set()
    by_turn: dict[str, RoleCallReceipt] = {}
    provider_ids: set[str] = set()
    call_keys: set[str] = set()
    journal_by_provider: dict[str, dict[str, Any]] = {}
    for item in journal:
        if not isinstance(item, dict) or not isinstance(item.get("provider_call_id"), str):
            raise QualificationRunError("LEGACY_BUDGET_JOURNAL_INCOMPLETE")
        provider_id = item["provider_call_id"]
        if provider_id in journal_by_provider:
            raise QualificationRunError("LEGACY_BUDGET_JOURNAL_DUPLICATE")
        journal_by_provider[provider_id] = item
    for call in calls:
        request = RoleCallRequest.model_validate(call["request"])
        receipt = RoleCallReceipt.model_validate(call["receipt"])
        provider_id = call.get("provider_call_id")
        call_key = call.get("call_key")
        if provider_id in provider_ids or call_key in call_keys:
            raise QualificationRunError("LEGACY_BUDGET_DUPLICATE_CALL")
        provider_ids.add(provider_id)
        call_keys.add(call_key)
        expected_role = expected_binding.get("expected_role_bindings", {}).get(request.role)
        journal_item = journal_by_provider.get(provider_id)
        expected_journal = {
            "provider_call_id": provider_id,
            "call_key": call_key,
            "request_digest": request.request_digest,
            "role": call["role"],
            "stage": call["stage"],
            "outcome": "settled",
            "receipt_digest": sha256_digest(call["receipt"]),
            "turn_id": receipt.turn_ids[0] if len(receipt.turn_ids) == 1 else None,
        }
        # Keep the comparison below fail-closed; individual fields are all bound.
        if (
            call["status"] != "settled"
            or call["goal_contract_digest"] != expected_binding["goal_contract_digest"]
            or call["policy_digest"] != expected_policy_digest
            or call["request_digest"] != sha256_digest(call["request"])
            or receipt.call_id != call_key
            or request.request_digest != receipt.input_digest
            or request.role != call["role"]
            or request.payload.get("legacy_stage") != call["stage"]
            or call["stage"] not in LEGACY_ROLE_STAGES.get(request.role, set())
            or request.payload.get("legacy_cell_binding") != expected_binding
            or request.payload.get("legacy_cell_binding_digest") != sha256_digest(expected_binding)
            or request.timeout_policy_digest != evaluation_policies.role_timeouts.policy_digest
            or receipt.timeout_policy_digest != request.timeout_policy_digest
            or request.timeout_seconds != evaluation_policies.role_timeouts.timeout_for(request.role)
            or request.inventory_digest != expected_binding.get("legacy_inventory_digest")
            or receipt.inventory_digest != request.inventory_digest
            or expected_role is None
            or {"model": request.model, "effort": request.effort} != expected_role
            or receipt.role != request.role
            or receipt.model != request.model
            or receipt.effort != request.effort
            or request.payload.get("legacy_ephemeral") is not True
            or request.payload.get("legacy_thread_policy") != {
                "approval_mode": "deny_all",
                "thread_sandbox": "full-access",
                "run_sandbox": "full-access",
                "thread_model": request.model,
            }
            or receipt.permission_profile != ":danger-full-access"
            or receipt.approval_policy != "never"
            or receipt.schema_recovery_attempts != 0
            or not receipt.usage_available
            or len(receipt.turn_ids) != 1
            or receipt.thread_id is None
            or call["actual_tokens"] != receipt.input_tokens + receipt.output_tokens
            or journal_item != expected_journal
        ):
            raise QualificationRunError("LEGACY_BUDGET_CALL_BINDING_MISMATCH")
        turn_id = receipt.turn_ids[0]
        if turn_id in budget_turns:
            raise QualificationRunError("LEGACY_BUDGET_DUPLICATE_TURN")
        budget_turns.add(turn_id)
        by_turn[turn_id] = receipt
    legacy_turns: set[str] = set()
    legacy_call_ids: set[str] = set()
    for raw_receipt in raw_receipts:
        if not isinstance(raw_receipt, dict):
            raise QualificationRunError("LEGACY_BUDGET_RAW_RECEIPT_INVALID")
        raw_call_id = raw_receipt.get("call_id")
        raw_turn_ids = raw_receipt.get("turn_ids")
        if not isinstance(raw_call_id, str) or raw_call_id in legacy_call_ids \
                or not isinstance(raw_turn_ids, list) or len(raw_turn_ids) != 1:
            raise QualificationRunError("LEGACY_BUDGET_RAW_RECEIPT_DUPLICATE_OR_MULTI_TURN")
        legacy_call_ids.add(raw_call_id)
        turn_id = str(raw_turn_ids[0])
        if turn_id in legacy_turns:
            raise QualificationRunError("LEGACY_BUDGET_DUPLICATE_TURN")
        legacy_turns.add(turn_id)
        budget_receipt = by_turn.get(turn_id)
        usage_rows = raw_receipt.get("usage")
        if not isinstance(usage_rows, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or type(item.get("value")) is not int
            for item in usage_rows
        ):
            raise QualificationRunError("LEGACY_BUDGET_RAW_USAGE_INVALID")
        usage = {item["name"]: item["value"] for item in usage_rows}
        if len(usage) != len(usage_rows):
            raise QualificationRunError("LEGACY_BUDGET_RAW_USAGE_DUPLICATE")
        last_components = {
            name.removeprefix("last."): value
            for name, value in usage.items() if name.startswith("last.")
        }
        total_components = {
            name.removeprefix("total."): value
            for name, value in usage.items() if name.startswith("total.")
        }
        if not last_components or last_components != total_components:
            raise QualificationRunError("LEGACY_BUDGET_RAW_USAGE_SCOPE_MISMATCH")

        def metric(scope: str, snake: str, camel: str) -> int:
            names = (f"{scope}.{snake}", f"{scope}.{camel}")
            found = [usage[name] for name in names if name in usage]
            if len(found) != 1 or found[0] < 0:
                raise QualificationRunError("LEGACY_BUDGET_RAW_USAGE_COMPONENT_MISSING")
            return found[0]

        last = tuple(metric("last", snake, camel) for snake, camel in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
            ("total_tokens", "totalTokens"),
        ))
        total = tuple(metric("total", snake, camel) for snake, camel in (
            ("input_tokens", "inputTokens"),
            ("cached_input_tokens", "cachedInputTokens"),
            ("output_tokens", "outputTokens"),
            ("reasoning_output_tokens", "reasoningOutputTokens"),
            ("total_tokens", "totalTokens"),
        ))
        if budget_receipt is None or (
            raw_receipt.get("status") not in {"succeeded", "failed"}
            or raw_receipt.get("schema_recovery_attempts") != 0
            or budget_receipt.role != raw_receipt.get("role")
            or budget_receipt.model != raw_receipt.get("model_id")
            or budget_receipt.effort != raw_receipt.get("reasoning_effort")
            or budget_receipt.inventory_digest != raw_receipt.get("inventory_digest")
            or budget_receipt.output_schema_digest != raw_receipt.get("output_schema_digest")
            or budget_receipt.thread_id != raw_receipt.get("thread_id")
            or last != total
            or last[:4] != (
                budget_receipt.input_tokens,
                budget_receipt.cached_input_tokens,
                budget_receipt.output_tokens,
                budget_receipt.reasoning_tokens,
            )
            or last[4] != last[0] + last[2]
            or raw_receipt.get("token_count") != last[4]
        ):
            raise QualificationRunError("LEGACY_BUDGET_RAW_RECEIPT_BINDING_MISMATCH")
    if budget_turns != legacy_turns or body.get("budget_turn_ids") != sorted(budget_turns) \
            or body.get("legacy_turn_ids") != sorted(legacy_turns):
        raise QualificationRunError("LEGACY_BUDGET_TURN_COVERAGE_MISMATCH")
    status = body.get("budget_status")
    if not isinstance(status, dict) or status.get("error_code") is not None \
            or status.get("unresolved_call_ids") not in ([], ()):
        raise QualificationRunError("LEGACY_BUDGET_UNRESOLVED")


def receipt_cost(receipt: dict[str, Any], implementation: str) -> tuple[int, int]:
    if implementation == "skeleton_engine":
        if not receipt.get("usage_available"):
            raise QualificationRunError("실제 Runner token usage가 없습니다. 0으로 추정하지 않습니다.")
        inputs, cached, outputs = (receipt.get(key) for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
    else:
        usage = {item["name"]: item["value"] for item in receipt.get("usage", ())}
        def metric(names):
            found = next((usage[name] for name in names if name in usage), None)
            if found is None:
                raise QualificationRunError("R3.1 Runner token usage가 없습니다: " + "/".join(names))
            return found
        inputs = metric(("input_tokens", "inputTokens", "total.input_tokens", "total.inputTokens"))
        cached = metric(("cached_input_tokens", "cachedInputTokens", "total.cached_input_tokens", "total.cachedInputTokens"))
        outputs = metric(("output_tokens", "outputTokens", "total.output_tokens", "total.outputTokens"))
    if any(type(item) is not int or item < 0 for item in (inputs, cached, outputs)):
        raise QualificationRunError("실제 token usage는 결측이 없는 0 이상 정수여야 합니다.")
    if cached > inputs:
        raise QualificationRunError("cached input이 전체 input token보다 큽니다.")
    return inputs - cached, outputs


def benchmark_cell(scenario, seed: int, implementation: str, model_lock: str, raw: dict[str, Any]) -> BenchmarkCell:
    receipts = raw["receipts"]
    if not receipts:
        raise QualificationRunError("완료 Runner receipt 없는 benchmark cell은 저장할 수 없습니다.")
    costs = [receipt_cost(item, implementation) for item in receipts]
    if implementation == "skeleton_engine":
        disposition = "selected" if raw.get("selected") else ("blocked" if raw.get("blocking_questions") else "failed")
        selected = raw.get("selected_activation_digest")
        candidate_tokens, discarded = candidate_output_costs(raw)
        lifecycle_raw = raw.get("lifecycle_observation")
        lifecycle = (
            None
            if lifecycle_raw is None
            else BenchmarkLifecycleObservation.model_validate(lifecycle_raw)
        )
        if lifecycle is not None:
            if not raw.get("selected"):
                raise QualificationRunError("선택 Plan이 없는 cell에 lifecycle evidence가 있습니다.")
            expected_bindings = (
                ("plan activation", lifecycle.plan_activation_digest, selected),
                ("model lock", lifecycle.model_lock_digest, model_lock),
                ("neutral input", lifecycle.neutral_input_digest, raw["neutral_input_digest"]),
            )
            mismatched = [name for name, observed, expected in expected_bindings if observed != expected]
            if mismatched:
                raise QualificationRunError(
                    "lifecycle evidence binding이 benchmark cell과 다릅니다: " + ", ".join(mismatched)
                )
        detailed = None if lifecycle is None else lifecycle.detailed_execution_spec_count
        unexecuted = None if lifecycle is None else lifecycle.unexecuted_execution_spec_count
        lifecycle_digest = None if lifecycle is None else lifecycle.observation_digest
        selected_plan_digest = selected
        correct = bool(raw.get("passed"))
    else:
        disposition = raw["disposition"]
        candidate_ids = {record["candidate_id"]: record for record in raw["candidate_records"]}
        by_receipt = {item["call_id"]: receipt_cost(item, implementation)[1] for item in receipts}
        candidate_tokens = sum(by_receipt[ref] for record in candidate_ids.values() for ref in record["receipt_ids"])
        discarded = sum(by_receipt[ref] for record in candidate_ids.values()
                        if record["candidate_id"] != raw["selected_candidate_id"] for ref in record["receipt_ids"])
        detailed = sum(item["task_count"] for item in candidate_ids.values())
        unexecuted = detailed
        lifecycle_digest = None
        lifecycle = None
        selected_plan_digest = None
        correct = disposition == scenario.expected_disposition
    return BenchmarkCell(
        scenario_id=scenario.scenario_id, scenario_digest=scenario.scenario_digest, order_seed=seed,
        path_kind=scenario.path_kind, implementation=implementation, model_lock_digest=model_lock,
        neutral_input_digest=raw["neutral_input_digest"],
        functional_result_digest=sha256_digest({"expected": scenario.expected_disposition,
                                               "disposition": disposition, "contract_satisfied": correct}),
        runner_receipt_digest=sha256_digest(receipts), expected_disposition=scenario.expected_disposition,
        disposition=disposition, uncached_input_tokens=sum(item[0] for item in costs),
        output_tokens=sum(item[1] for item in costs),
        latency_ms_to_first_feasible=raw.get("latency_ms_to_first_feasible") if disposition == "selected" else None,
        latency_ms_to_disposition=raw["latency_ms_to_disposition"],
        selected_plan_activation_digest=selected_plan_digest,
        lifecycle_observation=lifecycle,
        lifecycle_evidence_digest=lifecycle_digest, detailed_task_count=detailed,
        unexecuted_detailed_task_count=unexecuted, candidate_output_tokens=candidate_tokens,
        discarded_candidate_output_tokens=discarded,
    )


def run_benchmark(*, root: Path | None = None, run_root: Path | None = None,
                  role_configuration: EngineRoleConfiguration | None = None, codex_bin: str | None = None,
                  scope_reports: tuple[ScopeQualificationReport, ...] = (),
                  evaluation_policies: EvaluationPolicies | None = None) -> tuple[Path, BenchmarkRunReport]:
    if evaluation_policies is None:
        raise QualificationRunError("EVALUATION_POLICY_REQUIRED")
    base = (root or project_root()).resolve(strict=True)
    failures = _preflight(base)
    if failures:
        raise QualificationRunError("; ".join(failures))
    roles = role_configuration or default_role_configuration(base)
    timeout_contract = _legacy_hard_timeout_contract(evaluation_policies)
    resolved_codex = _resolve_codex_executable(codex_bin)
    codex_executable_digest = sha256_bytes(resolved_codex.read_bytes())
    catalog = PlanningScenarioCatalog.load(base / "tests/fixtures/engine/planning-scenarios.json")
    manifest = _manifest(base)
    skill = next((base / item.path).resolve() for item in manifest.roots if item.scope == "prototype_planner_skill")
    with tempfile.TemporaryDirectory(prefix="flowmarshal-benchmark-contract-") as temporary:
        description_path = Path(temporary) / "legacy-description.json"
        _legacy_process(
            base,
            ["--describe", "--skill-root", str(skill), "--output", str(description_path)],
            timeout_contract=timeout_contract,
        )
        description = json.loads(description_path.read_text(encoding="utf-8"))
    with CodexAppServerRuntime(codex_bin=str(resolved_codex)) as runtime:
        inventory = runtime.list_models()
        if inventory.executable_digest != codex_executable_digest:
            raise QualificationRunError("CODEX_EXECUTABLE_DIGEST_MISMATCH")
        roles.validate_inventory(inventory)
        legacy_inventory_digest = _legacy_inventory_digest(inventory)
        planning_contract = _planning_contract(
            base, catalog, inventory, roles, evaluation_policies
        )
        model_lock = sha256_digest({"engine_lock": planning_contract.model_lock_digest, "legacy_role_map": description["role_map"]})
        neutral = {scenario.scenario_id: neutral_input(base, scenario) for scenario in catalog.scenarios}
        identities = {(scenario.scenario_id, implementation): sha256_digest({
            "scenario": scenario.scenario_digest, "implementation": implementation,
            "neutral": sha256_digest(neutral[scenario.scenario_id]),
        }) for scenario in catalog.scenarios for implementation in ("r31_baseline", "skeleton_engine")}
        contract = EvaluationContract.model_validate(planning_contract.model_dump() | {
            "fixture_digests": tuple(identities.values()), "expected_cell_count": 36,
            "scenario_set_digest": sha256_digest(neutral), "rules_digest": sha256_digest(MEASUREMENT_RULES),
            "threshold_digest": sha256_digest(
                MEASUREMENT_RULES["thresholds"]
                | policy_contract_fragment(evaluation_policies)
                | {"implementation_runtime": IMPLEMENTATION_RUNTIME_CONTRACT}
            ),
            "model_lock_digest": model_lock,
            "prompt_digest": sha256_digest({"engine": planning_contract.prompt_digest, "legacy": description["prompt_digest"]}),
            "output_schema_digest": sha256_digest({"engine": planning_contract.output_schema_digest,
                                                    "legacy": description["schema_digest"], "cell": BenchmarkCell.model_json_schema()}),
        })
        destination = (run_root or _default_run_root(base, "benchmark", contract.contract_digest[7:15])).resolve()
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        write_immutable_run_metadata(destination / "run-metadata.json", {
            "scope": "benchmark", "project_root": str(base), "codex_bin": str(resolved_codex),
            "codex_executable_digest": codex_executable_digest,
            "engine_inventory_digest": inventory.inventory_digest,
            "legacy_inventory_digest": legacy_inventory_digest,
            "legacy_parent_hard_timeout": timeout_contract,
            "evaluation_contract_digest": contract.contract_digest,
            "role_configuration": roles.model_dump(mode="json"),
            "scope_reports": [item.model_dump(mode="json") for item in scope_reports],
            "implementation_runtime": IMPLEMENTATION_RUNTIME_CONTRACT,
        }, evaluation_policies)
        _write_json(destination / "neutral-inputs.json", neutral)
        status, failures = EvaluationRunStatus.COMPLETED, []
        try:
            for seed in ORDER_SEEDS:
                order = list(identities)
                random.Random(seed).shuffle(order)
                for scenario_id, implementation in order:
                    identity = identities[scenario_id, implementation]
                    if store.completed(identity, seed) is not None:
                        continue
                    if source_manifest_digest(base) != contract.source_manifest_digest:
                        raise QualificationRunError("실행 중 source 계약이 변경됐습니다.")
                    scenario = next(item for item in catalog.scenarios if item.scenario_id == scenario_id)
                    work = destination / "work" / f"seed-{seed}" / scenario_id / implementation / new_id("attempt")
                    work.mkdir(parents=True)
                    started = time.monotonic()
                    if implementation == "skeleton_engine":
                        raw, receipts = _planning_cell(
                            scenario=scenario, seed=seed, fixture_root=base / "tests/fixtures/engine/live-smoke-project",
                            runtime=runtime, inventory=inventory, roles=roles, work_root=work,
                            progress_sink=_role_progress(destination, scenario_id=scenario_id, order_seed=seed, implementation=implementation),
                            evaluation_policies=evaluation_policies,
                            retain_execution_checkpoint=True,
                        )
                        raw["receipts"] = [item.model_dump(mode="json") for item in receipts]
                        raw["neutral_input_digest"] = sha256_digest(neutral[scenario_id])
                    else:
                        shutil.copytree(base / "tests/fixtures/engine/live-smoke-project", work / "project",
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                        neutral_digest = sha256_digest(neutral[scenario_id])
                        legacy_project_id = "project_" + neutral_digest[7:39]
                        budget_profile = _profile(legacy_project_id)
                        budget_scope = {
                            "evaluation_contract_digest": contract.contract_digest,
                            "fixture_digest": identity,
                            "scenario_id": scenario.scenario_id,
                            "scenario_digest": scenario.scenario_digest,
                            "order_seed": seed,
                            "implementation": "r31_baseline",
                            "neutral_input_digest": neutral_digest,
                            "evaluation_policy_digest": evaluation_policies.policy_digest,
                            "parent_hard_timeout_policy_digest": timeout_contract["policy_digest"],
                        }
                        budget_goal = _legacy_budget_goal(
                            project_id=legacy_project_id,
                            profile_digest=budget_profile.definition_digest,
                            cell_scope=budget_scope,
                        )
                        budget_binding = _legacy_budget_cell_binding(
                            contract_digest=contract.contract_digest,
                            fixture_digest=identity,
                            scenario=scenario,
                            seed=seed,
                            neutral_input_digest=neutral_digest,
                            project_id=legacy_project_id,
                            goal=budget_goal,
                            evaluation_policies=evaluation_policies,
                            roles=roles,
                            inventory=inventory,
                            legacy_role_map=description["role_map"],
                            legacy_inventory_digest=legacy_inventory_digest,
                            codex_executable_digest=codex_executable_digest,
                            timeout_contract=timeout_contract,
                        )
                        _write_json(work / "request.json", {
                            "workspace": str(work / "project"), "state_root": str(work / "legacy-state"),
                            "neutral_input": neutral[scenario_id], "neutral_input_digest": neutral_digest,
                            "roles": roles.model_dump(mode="json"), "codex_bin": str(resolved_codex),
                            "legacy_inventory_digest": legacy_inventory_digest,
                            "parent_hard_timeout_seconds": timeout_contract["timeout_seconds"],
                            "parent_hard_timeout_policy_digest": timeout_contract["policy_digest"],
                            "parent_hard_timeout_contract": timeout_contract,
                            "skill_root": str(skill), "python_executable": sys.executable,
                            "evaluation_policies": evaluation_policies.model_dump(mode="json"),
                            "evaluation_policy_digest": evaluation_policies.policy_digest,
                            "budget_profile": budget_profile.model_dump(mode="json"),
                            "budget_goal": budget_goal.model_dump(mode="json"),
                            "budget_cell_binding": budget_binding,
                            "budget_cell_binding_digest": sha256_digest(budget_binding),
                        })
                        _legacy_process(
                            base,
                            ["--request-file", str(work / "request.json"), "--output", str(work / "raw-result.json")],
                            timeout_contract=timeout_contract,
                        )
                        raw = json.loads((work / "raw-result.json").read_text(encoding="utf-8"))
                        _verify_legacy_budget_evidence(
                            raw=raw,
                            expected_binding=budget_binding,
                            state_root=work / "legacy-state",
                            evaluation_policies=evaluation_policies,
                        )
                    if raw.get("message") and _is_rate_limit(QualificationRunError(raw["message"])):
                        raise QualificationRunError(raw["message"])
                    # 부모 프로세스 준비 시간은 진단으로 보존하고 각 pipeline의 monotonic 지표를 사용한다.
                    raw["collector_latency_ms"] = max(1, int((time.monotonic() - started) * 1000))
                    _write_json(work / "raw-result.json", raw)
                    cell = benchmark_cell(scenario, seed, implementation, model_lock, raw)
                    store.put(EvaluationCellCheckpoint(
                        model_lock_format="flowmarshal-model-lock-v2",
                        contract_digest=contract.contract_digest, fixture_digest=identity, order_seed=seed,
                        raw_structured_assessment={"benchmark_cell": cell.model_dump(mode="json"), "raw": raw},
                        runner_receipts=tuple(raw["receipts"]),
                    ))
        except Exception as error:
            status = EvaluationRunStatus.PAUSED_RATE_LIMIT if _is_rate_limit(error) else EvaluationRunStatus.FAILED
            failures.append(f"{type(error).__name__}: {error}")
            _write_json(destination / "last-error.json", {"error": type(error).__name__, "message": str(error),
                        "receipts": [item.model_dump(mode="json") for item in getattr(error, "receipts", ())]})
        cells = tuple(BenchmarkCell.model_validate(checkpoint.raw_structured_assessment["benchmark_cell"])
                      for seed in ORDER_SEEDS for identity in identities.values()
                      if (checkpoint := store.completed(identity, seed)) is not None)
        token_report = None
        if len(cells) == 36:
            from .eval_cli import validate_benchmark_matrix
            try:
                validate_benchmark_matrix(cells, catalog)
                functional = len(scope_reports) == 4 and len({item.scope for item in scope_reports}) == 4 and all(item.passed for item in scope_reports)
                token_report = evaluate_token_latency_gate(cells, functional_gate_passed=functional)
                failures.extend(token_report.failures)
                _write_json(destination / "token-latency-report.json", token_report)
            except ValueError as error:
                failures.append(str(error))
        store.set_state(status, updated_at=utc_now(), reason="; ".join(failures)[:5000] or None)
        report = BenchmarkRunReport(contract_digest=contract.contract_digest, status=status,
                                    completed_cell_count=len(cells), passed=bool(token_report and token_report.passed and not failures),
                                    failures=tuple(failures), token_latency=token_report)
        _write_json(destination / "benchmark-cells.json", {"cells": [item.model_dump(mode="json") for item in cells]})
        _write_json(destination / "benchmark-run-report.json", report)
        return destination, report
