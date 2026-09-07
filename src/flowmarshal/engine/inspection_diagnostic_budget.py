"""R-S06 진단의 외부 운영 정책과 Goal 계보 예산 원장을 결속한다."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import Field

from ..canonical import json_value, sha256_bytes, sha256_digest
from .budget import BudgetManager, BudgetedRoleRunner
from .domain import EngineModel, GoalContractRevision
from .evaluation_budget import (
    EvaluationPolicies,
    load_evaluation_policies,
    verify_service_budget_policy,
)
from .ledger import SQLiteEngineLedger
from .service import EngineService


class DiagnosticPolicyInput(EngineModel):
    """호출 전 원문 bytes와 typed 정책을 함께 잠근 portable diagnostic 입력."""

    format: str = Field(default="flowmarshal-inspection-diagnostic-policy-input-v1")
    budget_policy: dict[str, Any]
    role_timeout_policy: dict[str, Any]
    codex_project_binding: dict[str, Any]
    source_paths: dict[str, str]
    source_bytes_digests: dict[str, str]
    source_canonical_digests: dict[str, str]
    source_snapshots: dict[str, str]
    policy_digest: str


def _read_input(path: Path | str, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    source = Path(path)
    if not source.is_absolute() or not source.is_file():
        raise ValueError(f"DIAGNOSTIC_{label.upper()}_PATH_REQUIRED")
    raw = source.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"DIAGNOSTIC_{label.upper()}_JSON_INVALID") from error
    if not isinstance(document, dict):
        raise ValueError(f"DIAGNOSTIC_{label.upper()}_JSON_OBJECT_REQUIRED")
    return source.resolve(), raw, document


def bind_diagnostic_policy_input(
    *,
    snapshot_root: Path | str,
    budget_policy_path: Path | str,
    role_timeout_policy_path: Path | str,
    codex_project_binding_path: Path | str | None,
    require_project_binding: bool,
) -> tuple[DiagnosticPolicyInput, EvaluationPolicies]:
    """원문을 snapshot으로 한 번 복사하고 typed 정책 digest를 lock에 남긴다."""

    if require_project_binding and codex_project_binding_path is None:
        raise ValueError("DIAGNOSTIC_CODEX_PROJECT_BINDING_REQUIRED")
    inputs = {
        "budget_policy": _read_input(budget_policy_path, "budget_policy"),
        "role_timeout_policy": _read_input(role_timeout_policy_path, "role_timeout_policy"),
    }
    if codex_project_binding_path is not None:
        inputs["codex_project_binding"] = _read_input(
            codex_project_binding_path, "codex_project_binding"
        )
    policies = load_evaluation_policies(
        budget_policy_path=budget_policy_path,
        role_timeout_policy_path=role_timeout_policy_path,
        codex_project_binding_path=codex_project_binding_path,
    )
    if require_project_binding and policies.codex_project is None:
        raise ValueError("DIAGNOSTIC_CODEX_PROJECT_BINDING_REQUIRED")
    root = Path(snapshot_root)
    paths: dict[str, str] = {}
    digests: dict[str, str] = {}
    canonical_digests: dict[str, str] = {}
    snapshots: dict[str, str] = {}
    for label, (source, raw, document) in inputs.items():
        destination = root / f"{label}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(raw)
            stream.flush()
        paths[label] = str(source)
        digests[label] = sha256_bytes(raw)
        canonical_digests[label] = sha256_digest(document)
        snapshots[label] = str(destination.relative_to(root.parent))
    binding = DiagnosticPolicyInput(
        budget_policy=policies.budget.model_dump(mode="json"),
        role_timeout_policy=policies.role_timeouts.model_dump(mode="json"),
        codex_project_binding=(
            {} if policies.codex_project is None else policies.codex_project.model_dump(mode="json")
        ),
        source_paths=paths,
        source_bytes_digests=digests,
        source_canonical_digests=canonical_digests,
        source_snapshots=snapshots,
        policy_digest=policies.policy_digest,
    )
    return binding, policies


def verify_diagnostic_policy_input(
    binding_value: dict[str, Any] | DiagnosticPolicyInput,
    *,
    run_root: Path | str,
    require_project_binding: bool,
) -> EvaluationPolicies:
    binding = DiagnosticPolicyInput.model_validate(binding_value)
    required = ("budget_policy", "role_timeout_policy") + (
        ("codex_project_binding",) if require_project_binding else ()
    )
    required_set = set(required)
    if any(set(values) != required_set for values in (
        binding.source_paths, binding.source_bytes_digests,
        binding.source_canonical_digests, binding.source_snapshots,
    )):
        raise ValueError("DIAGNOSTIC_POLICY_BINDING_INCOMPLETE")
    root = Path(run_root).resolve()
    for label in required:
        source = Path(binding.source_paths[label])
        snapshot_ref = Path(binding.source_snapshots[label])
        snapshot = (root / snapshot_ref).resolve()
        if (snapshot_ref.is_absolute() or not snapshot.is_relative_to(root / "policy-inputs")
                or not source.is_absolute() or not source.is_file() or not snapshot.is_file()
                or sha256_bytes(source.read_bytes()) != binding.source_bytes_digests[label]
                or snapshot.read_bytes() != source.read_bytes()):
            raise ValueError("DIAGNOSTIC_POLICY_INPUT_CHANGED")
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("DIAGNOSTIC_POLICY_INPUT_CHANGED") from error
        if sha256_digest(document) != binding.source_canonical_digests[label]:
            raise ValueError("DIAGNOSTIC_POLICY_INPUT_CHANGED")
    policies = load_evaluation_policies(
        budget_policy_path=binding.source_paths["budget_policy"],
        role_timeout_policy_path=binding.source_paths["role_timeout_policy"],
        codex_project_binding_path=(
            binding.source_paths.get("codex_project_binding")
            if "codex_project_binding" in required_set else None
        ),
    )
    if (policies.budget.model_dump(mode="json") != binding.budget_policy
            or policies.role_timeouts.model_dump(mode="json") != binding.role_timeout_policy
            or ({} if policies.codex_project is None else policies.codex_project.model_dump(mode="json"))
            != binding.codex_project_binding
            or policies.policy_digest != binding.policy_digest):
        raise ValueError("DIAGNOSTIC_POLICY_TYPED_DIGEST_CHANGED")
    if require_project_binding and policies.codex_project is None:
        raise ValueError("DIAGNOSTIC_CODEX_PROJECT_BINDING_REQUIRED")
    return policies


def diagnostic_budget_state_root(run_root: Path | str, *, project_id: str, goal_id: str) -> Path:
    """revision digest를 key에 넣지 않아 같은 Goal 새 revision이 한도를 나누지 못한다."""
    identity = sha256_digest({"project_id": project_id, "goal_id": goal_id}).split(":", 1)[1]
    return Path(run_root) / "budget-state" / identity


def _ledger_snapshot(database: Path) -> dict[str, Any]:
    """WAL 파일 byte hash 대신 append-only 원장의 읽기 전용 논리 상태를 고정한다."""
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        history = [dict(row) for row in connection.execute(
            "SELECT * FROM history_events ORDER BY project_id, sequence"
        )]
        previous: dict[str, str | None] = {}
        history_valid = True
        for row in history:
            body = {
                "id": row["id"], "project_id": row["project_id"], "sequence": row["sequence"],
                "event_type": row["event_type"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"], "created_at": row["created_at"],
            }
            if row["previous_hash"] != previous.get(row["project_id"]) or row["event_hash"] != sha256_digest(body):
                history_valid = False
            previous[row["project_id"]] = row["event_hash"]
        calls = [dict(row) for row in connection.execute(
            "SELECT id,project_id,goal_id,goal_contract_digest,request_digest,status,actual_tokens,receipt_json,usage_id "
            "FROM provider_calls ORDER BY rowid"
        )]
        policies = [dict(row) for row in connection.execute(
            "SELECT project_id,scope_key,policy_digest,payload_json FROM budget_policy_revisions ORDER BY rowid"
        )]
        usages = [dict(row) for row in connection.execute(
            "SELECT id,project_id,goal_contract_digest,logical_call_ref,payload_json FROM budget_usage ORDER BY rowid"
        )]
    finally:
        connection.close()
    receipts = []
    observations = []
    effective_usage = []
    malformed_receipt = False
    malformed_observation = False
    for call in calls:
        value = call["receipt_json"]
        try:
            receipt = None if value is None else json.loads(value)
        except json.JSONDecodeError:
            receipt, malformed_receipt = None, True
        receipts.append({"provider_call_id": call["id"], "receipt": receipt})
        call_observations = []
        for event in history:
            if event["entity_type"] != "provider_call" or event["entity_id"] != call["id"] \
                    or event["event_type"] != "budget.call_observed":
                continue
            payload = json.loads(event["payload_json"])
            if payload.get("observation_kind") != "runtime_observation":
                continue
            document = payload.get("observation")
            valid = isinstance(document, dict) and payload.get("observation_digest") == sha256_digest(document)
            malformed_observation = malformed_observation or not valid
            call_observations.append({
                "observation": document, "observation_digest": payload.get("observation_digest"),
                "valid": valid, "history_sequence": event["sequence"],
            })
        observations.append({"provider_call_id": call["id"], "observations": call_observations})
        latest = call_observations[-1] if call_observations else None
        effective_usage.append({
            "provider_call_id": call["id"], "status": call["status"],
            "actual_tokens": call["actual_tokens"], "usage_id": call["usage_id"],
            "source": "runtime_observation" if latest is not None else "role_receipt",
            "observation_digest": None if latest is None else latest["observation_digest"],
        })
    unresolved = [call["id"] for call in calls if call["status"] != "settled" or call["actual_tokens"] is None]
    body = {
        "history": history, "policies": policies, "provider_calls": calls,
        "usage": usages, "receipts": receipts, "observations": observations,
        "effective_usage": effective_usage,
    }
    return {
        "history_event_count": len(history), "history_chain_valid": history_valid,
        "policy_count": len(policies), "provider_call_count": len(calls),
        "usage_observation_count": len(usages), "unresolved_provider_call_ids": unresolved,
        "malformed_receipt": malformed_receipt, "malformed_observation": malformed_observation,
        "provider_receipts": receipts,
        "provider_observations": observations, "effective_provider_usage": effective_usage,
        "logical_snapshot_digest": sha256_digest(body),
    }


def diagnostic_budget_ledger_observation(run_root: Path | str) -> dict[str, Any]:
    """모든 Goal ledger의 logical snapshot과 사용량 완결성을 읽기 전용으로 집계한다."""
    root = Path(run_root) / "budget-state"
    entries: dict[str, Any] = {}
    missing: list[str] = []
    if root.exists():
        for state_root in sorted(path for path in root.iterdir() if path.is_dir()):
            database = state_root / "flowmarshal-engine.sqlite3"
            if not database.is_file():
                missing.append(state_root.name)
                continue
            entries[state_root.name] = _ledger_snapshot(database)
    unresolved = [
        call_id for item in entries.values()
        for call_id in item["unresolved_provider_call_ids"]
    ]
    return {
        "ledger_count": len(entries), "entries": entries, "missing_ledger_state_roots": missing,
        "unresolved_provider_call_ids": unresolved,
        "all_history_chains_valid": all(item["history_chain_valid"] for item in entries.values()),
        "all_receipts_well_formed": all(not item["malformed_receipt"] for item in entries.values()),
        "all_observations_well_formed": all(not item["malformed_observation"] for item in entries.values()),
        "logical_snapshot_digest": sha256_digest(entries),
    }


class DiagnosticBudgetRegistry:
    """Profile 원문이 없는 diagnostic의 Goal 계보별 공유 pre-Goal 예산 원장을 연다."""

    def __init__(self, *, run_root: Path | str, workspace: Path | str, policies: EvaluationPolicies):
        self.run_root = Path(run_root)
        self.workspace = Path(workspace).resolve()
        self.policies = policies
        self._services: dict[tuple[str, str], EngineService] = {}

    def service_for(self, goal: GoalContractRevision) -> EngineService:
        project_id, goal_id = goal.definition.project_id, goal.goal_id
        key = (project_id, goal_id)
        if key in self._services:
            service = self._services[key]
            verify_service_budget_policy(service, project_id, self.policies)
            self._bind_original_goal(root=self.run_root, goal=goal)
            return service
        root = diagnostic_budget_state_root(self.run_root, project_id=project_id, goal_id=goal_id)
        database = root / "flowmarshal-engine.sqlite3"
        if not database.exists():
            ledger = SQLiteEngineLedger(database, artifact_root=root / "artifacts")
            service = EngineService(ledger)
            service.initialize()
            manager = BudgetManager(service)
            service.create_project(
                name="FlowMarshal inspection diagnostic pre-Goal budget",
                root=self.workspace,
                project_id=project_id,
            )
            manager.configure(project_id, self.policies.budget)
        else:
            ledger = SQLiteEngineLedger(database, artifact_root=root / "artifacts")
            service = EngineService(ledger)
            service.initialize()
            manager = BudgetManager(service)
            with ledger.read() as connection:
                project = connection.execute("SELECT root FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None or Path(project["root"]).resolve() != self.workspace:
                raise ValueError("DIAGNOSTIC_BUDGET_PROJECT_BINDING_MISMATCH")
            verify_service_budget_policy(service, project_id, self.policies)
        self._bind_original_goal(root=self.run_root, goal=goal)
        self._services[key] = service
        return service

    @staticmethod
    def _bind_original_goal(*, root: Path, goal: GoalContractRevision) -> None:
        """Profile을 추정해 Goal을 등록하지 않고, 원래 Goal digest만 별도 evidence로 보존한다."""
        # Windows에서도 전체 digest를 보존하도록 예산 key 아래에 digest 경로를 중첩하지 않는다.
        # run 루트의 이 원문은 generation pending의 입력 manifest에도 직접 결속된다.
        digest = goal.definition_digest.split(":", 1)[1]
        destination = root / "goal-bindings" / f"{digest}.json"
        body = {
            "project_id": goal.definition.project_id,
            "goal_id": goal.goal_id,
            "goal_revision_id": goal.goal_revision_id,
            "goal_definition_digest": goal.definition_digest,
            "goal_contract": goal.model_dump(mode="json"),
        }
        payload = json.dumps(json_value(body), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
        except FileExistsError:
            if destination.read_text(encoding="utf-8") != payload:
                raise ValueError("DIAGNOSTIC_GOAL_BINDING_CHANGED")

    def runner_for(self, runner: Any, goal: GoalContractRevision) -> BudgetedRoleRunner:
        service = self.service_for(goal)
        return BudgetedRoleRunner(
            runner, service, project_id=goal.definition.project_id,
            # profile 원문이 없으므로 Goal을 재작성·등록하지 않는다. policy scope는 원래 Goal ID다.
            goal_id=goal.goal_id, goal_digest=None,
        )
