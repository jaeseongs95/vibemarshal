from __future__ import annotations

from .capabilities import CoreActionAuthority

from .domain import ModelFallback

import json
import inspect
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    EffectPolicy,
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    IntegrationValidationContract,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    PlanSkeletonCandidate,
    RecoveryEnvelope,
    ResolvedTarget,
    ReviewRatings,
    ReviewerSubmission,
    RevisionStatus,
    RiskLevel,
    RoleAssignmentPolicy,
    RunOnceAction,
    RuntimeReceipt,
    SourceTrace,
    TaskContract,
    TaskKind,
    TaskSkeleton,
    ThreadBinding,
    ValidationContract,
    ValidationExecutionStep,
    new_id,
    utc_now,
)
from .evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    EvaluationScope,
    ImmutableCheckpointStore,
)
from .evaluation_budget import (
    EvaluationPolicies,
    budgeted_role_runner,
    evaluation_cell_provider_calls,
    policy_contract_fragment,
    verify_metadata_digest,
    verify_service_budget_policy,
    write_immutable_run_metadata,
)
from .application import EngineApplication
from .budget import BudgetManager
from .ledger import SQLiteEngineLedger
from .models import EngineRoleConfiguration, ModelInventory
from .planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from .qualification import (
    QualificationRunError,
    ScopeQualificationReport,
    _default_run_root,
    _model_lock,
    _preflight,
    _profile,
    _write_json,
    build_qualification_reproduction_bundle,
    default_role_configuration,
    project_root,
    qualification_suite_manifest,
    source_manifest_digest,
)
from .qualification_manifest import (
    CandidateWheelBinding,
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    build_qualification_evidence_record,
    classify_qualification_failure,
    evaluate_qualification_responsibilities,
    verify_candidate_wheel_installation,
    verify_candidate_wheel_metadata,
)
from .providers import (
    RuntimeProviderSelection,
    open_harness_runtime,
    provider_run_metadata,
)
from .runtime import (
    CodexAppServerRuntime,
    CodexProjectBinding,
    CodexRuntimePort,
    EngineDispatcher,
    FakeCodexRuntime,
    RuntimeJobSupervisor,
    RuntimeOperationReceipt,
)
from .role_execution import use_role_timeout_policy
from .service import EngineService, EngineServiceError


E2E_SCENARIOS = (
    "normal-completion",
    "stale-after-materialization",
    "stored-turn-restart-resume",
    "unknown-receipt-no-duplicate",
    "forced-termination-no-duplicate",
    "absolute-timeout-no-duplicate",
    "cancel-active-job",
)

_RUNTIME_EVENT_SCHEMA = "flowmarshal.project-e2e.runtime-event.v3"


class RecordedRuntime:
    """실제 adapter 호출과 receipt를 보존하는 평가용 위임 wrapper."""

    def __init__(self, runtime: CodexRuntimePort, *, journal: Path) -> None:
        self.runtime = runtime
        self.requires_budget_policy = getattr(runtime, "requires_budget_policy", False)
        self.journal = journal
        self.events: list[dict[str, Any]] = []
        self.inventory: ModelInventory | None = None
        if journal.is_file():
            self.events = json.loads(journal.read_text(encoding="utf-8"))
            for event in self.events:
                if event.get("schema") != _RUNTIME_EVENT_SCHEMA:
                    raise QualificationRunError("E2E runtime journal schema가 현재 계약과 다릅니다.")
                digest = event.get("event_digest")
                body = {key: value for key, value in event.items() if key != "event_digest"}
                if (
                    digest != sha256_digest(body)
                    or event.get("request_binding_digest")
                    != sha256_digest(event.get("request_binding"))
                    or event.get("receipt_digest") != sha256_digest(event.get("receipt"))
                ):
                    raise QualificationRunError("E2E runtime journal event digest가 일치하지 않습니다.")
                if event.get("operation") == "list_models":
                    self.inventory = ModelInventory.model_validate(event.get("receipt"))
        self.create_calls = sum(item["operation"] == "create_thread" for item in self.events)
        self.turn_calls = sum(item["operation"] == "start_turn" for item in self.events)
        self.read_calls = sum(item["operation"] == "read" for item in self.events)
        self.resume_calls = sum(item["operation"] == "resume" for item in self.events)
        self._completion_forwarding_enabled = True
        self._completion_forwarding_lock = threading.Lock()
        if callable(getattr(runtime, "register_completion_observer", None)):
            # EngineDispatcher는 getattr로 capability를 탐지하므로 실제 runtime이
            # 지원할 때만 public attribute를 노출한다.
            self.register_completion_observer = self._register_completion_observer

    def _request_binding(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        binding: dict[str, Any] = {
            "operation": operation,
            "cwd": (
                str(Path(arguments["cwd"]).resolve()) if arguments.get("cwd") is not None else None
            ),
            "thread_id": arguments.get("thread_id"),
            "turn_id": arguments.get("turn_id"),
            "model": arguments.get("model"),
            "effort": arguments.get("effort"),
            "ephemeral": arguments.get("ephemeral"),
            "inventory_digest": None if self.inventory is None else self.inventory.inventory_digest,
        }
        if arguments.get("prompt") is not None:
            binding["prompt_digest"] = sha256_digest(arguments["prompt"])
        if arguments.get("developer_instructions") is not None:
            binding["developer_instructions_digest"] = sha256_digest(
                arguments["developer_instructions"]
            )
        if arguments.get("output_schema") is not None:
            binding["output_schema_digest"] = sha256_digest(arguments["output_schema"])
        return binding

    def _append_result_event(
        self,
        operation: str,
        request_binding: dict[str, Any],
        result: Any,
        *,
        deduplicate: bool = False,
    ) -> None:
        if isinstance(result, ModelInventory):
            self.inventory = result
        receipt = result.model_dump(mode="json")
        payload = receipt.get("payload") if isinstance(receipt, dict) else None
        # create_thread의 payload만 adapter가 보존한 thread/start 원시 응답이다.
        # start_turn의 model/effort는 SDK 요청 인자를 receipt에 다시 쓴 값이므로
        # provider 관측으로 승격하지 않는다.
        provider_raw = operation == "create_thread" and isinstance(payload, dict)
        raw_model = payload.get("model") if provider_raw else None
        raw_effort = payload.get("effort") if provider_raw else None
        complete_provider_pair = (
            isinstance(raw_model, str)
            and bool(raw_model)
            and isinstance(raw_effort, str)
            and bool(raw_effort)
        )
        observed_model = raw_model if complete_provider_pair else None
        observed_effort = raw_effort if complete_provider_pair else None
        observation_source = "provider_raw_response" if complete_provider_pair else None
        if operation == "start_turn":
            observation_reason = "START_TURN_RECEIPT_MODEL_EFFORT_ARE_REQUEST_ECHO"
        elif observed_model is None or observed_effort is None:
            observation_reason = "PROVIDER_RAW_RESPONSE_MODEL_OR_EFFORT_NOT_REPORTED"
        else:
            observation_reason = None
        event: dict[str, Any] = {
            "schema": _RUNTIME_EVENT_SCHEMA,
            "operation": operation,
            "request_binding": request_binding,
            "request_binding_digest": sha256_digest(request_binding),
            "receipt": receipt,
            "receipt_digest": sha256_digest(receipt),
            "observed_model": observed_model,
            "observed_effort": observed_effort,
            "model_observation_source": observation_source,
            "model_observation_reason": observation_reason,
        }
        event["event_digest"] = sha256_digest(event)
        if deduplicate and any(
            item.get("event_digest") == event["event_digest"] for item in self.events
        ):
            return
        self.events.append(event)
        _write_json(self.journal, self.events)

    def _call(self, operation: str, **arguments: Any) -> Any:
        request_binding = self._request_binding(operation, arguments)
        result = getattr(self.runtime, operation)(**arguments)
        self._append_result_event(operation, request_binding, result)
        return result

    def _register_completion_observer(
        self, *, thread_id: str, turn_id: str, observer: Any
    ) -> None:
        """SDK 완료 usage를 journal에 먼저 보존한 뒤 Core observer에 전달한다."""

        register = getattr(self.runtime, "register_completion_observer")

        def record_and_forward(observation: Any) -> None:
            with self._completion_forwarding_lock:
                if not self._completion_forwarding_enabled:
                    return
                request_binding = self._request_binding(
                    "completion_observation",
                    {"thread_id": thread_id, "turn_id": turn_id},
                )
                self._append_result_event(
                    "completion_observation",
                    request_binding,
                    observation,
                    deduplicate=True,
                )
                observer(observation)

        register(thread_id=thread_id, turn_id=turn_id, observer=record_and_forward)

    def sever_completion_forwarding(self) -> None:
        """collector 장애 주입 뒤 이전 callback이 Core 상태를 전진시키지 못하게 한다."""

        with self._completion_forwarding_lock:
            self._completion_forwarding_enabled = False

    def verify_execution_policy(self, cwd: Path | str) -> Any:
        return self._call("verify_execution_policy", cwd=cwd)

    def list_models(self) -> ModelInventory:
        return self._call("list_models")

    def create_thread(self, **arguments: Any) -> Any:
        result = self._call("create_thread", **arguments)
        self.create_calls += 1
        return result

    def start_turn(self, **arguments: Any) -> Any:
        result = self._call("start_turn", **arguments)
        self.turn_calls += 1
        return result

    def read(self, **arguments: Any) -> Any:
        result = self._call("read", **arguments)
        self.read_calls += 1
        return result

    def read_stored(self, **arguments: Any) -> Any:
        reader = self.runtime.read_stored
        parameters = inspect.signature(reader).parameters
        accepts_keywords = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        forwarded = (
            arguments
            if accepts_keywords
            else {key: value for key, value in arguments.items() if key in parameters}
        )
        request_binding = self._request_binding("read_stored", arguments)
        result = reader(**forwarded)
        self._append_result_event("read_stored", request_binding, result)
        return result

    def resume(self, **arguments: Any) -> Any:
        result = self._call("resume", **arguments)
        self.resume_calls += 1
        return result

    def interrupt(self, **arguments: Any) -> Any:
        return self._call("interrupt", **arguments)

    def close(self) -> None:
        self.runtime.close()


def _checkpoint_model_observation(events: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [item for item in events if item.get("operation") == "start_turn"]
    observed = [
        (item.get("observed_model"), item.get("observed_effort")) for item in turns
        if item.get("model_observation_source") == "provider_raw_response"
        and isinstance(item.get("observed_model"), str)
        and bool(item.get("observed_model"))
        and isinstance(item.get("observed_effort"), str)
        and bool(item.get("observed_effort"))
    ]
    pairs = tuple(sorted(set(observed)))
    missing = len(turns) - len(observed)
    if not turns:
        model = effort = None
        reason = "NO_MODEL_TURN_OBSERVED"
    elif missing:
        model = effort = None
        reason = "PROVIDER_RAW_MODEL_EFFORT_NOT_OBSERVED"
    elif len(pairs) == 1:
        model, effort = pairs[0]
        reason = None
    else:
        model = effort = None
        reason = "MULTIPLE_OBSERVED_MODEL_EFFORT_PAIRS"
    return {
        "actual_model": model,
        "actual_effort": effort,
        "observation_reason": reason,
        "turn_count": len(turns),
        "observed_pairs": [
            {"model": model_value, "effort": effort_value}
            for model_value, effort_value in pairs
        ],
        "requested_pairs": [
            {"model": model_value, "effort": effort_value}
            for model_value, effort_value in sorted({
                (
                    item.get("request_binding", {}).get("model"),
                    item.get("request_binding", {}).get("effort"),
                )
                for item in turns
                if isinstance(item.get("request_binding", {}).get("model"), str)
                and isinstance(item.get("request_binding", {}).get("effort"), str)
            })
        ],
        "provider_observation_reasons": sorted({
            item["model_observation_reason"] for item in turns
            if isinstance(item.get("model_observation_reason"), str)
        }),
        "request_binding_digests": [
            item.get("request_binding_digest") for item in turns
        ],
        "inventory_digests": sorted({
            item.get("request_binding", {}).get("inventory_digest") for item in turns
            if item.get("request_binding", {}).get("inventory_digest") is not None
        }),
    }


def _e2e_failure_disposition(
    scenario: str | None,
    error: Exception,
    *,
    provider_calls: tuple[tuple[str, str], ...] | None,
) -> tuple[EvaluationRunStatus, bool, str]:
    """provider 효과 전 중단이 증명된 normal cell만 PAUSED로 분류한다."""

    rate_limited = any(
        marker in str(error).casefold()
        for marker in ("rate limit", "rate_limit", "usage limit", "usage_limit", "quota")
    )
    provider_effect_absent = provider_calls is not None and all(
        status == "released" for _call_id, status in provider_calls
    )
    resumable = (
        rate_limited
        and scenario == "normal-completion"
        and provider_effect_absent
    )
    if resumable:
        calls = ",".join(
            f"{call_id}={status}" for call_id, status in provider_calls or ()
        )
        return (
            EvaluationRunStatus.PAUSED_RATE_LIMIT,
            True,
            f"E2E_RATE_LIMIT_BEFORE_PROVIDER_EFFECT: provider_calls=[{calls}]; provider "
            "효과가 시작되지 않았거나 효과 전 예약이 해제된 것이 원장에서 확인됐습니다. "
            "같은 run root에서 resume하세요.",
        )
    calls = (
        "unavailable"
        if provider_calls is None
        else ",".join(f"{call_id}={status}" for call_id, status in provider_calls)
    )
    return (
        EvaluationRunStatus.FAILED,
        False,
        "E2E_PROVIDER_EFFECT_RECONCILIATION_REQUIRED: "
        f"provider_calls=[{calls}]; 원장과 작업 artifact를 보존하고 기존 runtime intent, "
        "provider terminal과 예산 예약을 먼저 대조하세요. 미확인 효과가 있으면 새 "
        "provider 호출이나 새 Goal 예산으로 우회하지 않습니다.",
    )


def _guard_e2e_partial_resume(destination: Path) -> None:
    """완료 checkpoint 없는 provider 효과를 실제 runtime 진입 전에 차단한다."""

    contract_path = destination / "evaluation-contract.json"
    state_path = destination / "run-state.json"
    if not contract_path.is_file() and not state_path.is_file():
        return
    if not contract_path.is_file() or not state_path.is_file():
        raise QualificationRunError("E2E_RESUME_BINDING_INCOMPLETE")
    contract = EvaluationContract.model_validate_json(
        contract_path.read_text(encoding="utf-8")
    )
    if contract.scope is not EvaluationScope.PROJECT_E2E:
        return
    store = ImmutableCheckpointStore(destination, contract)
    if not contract.fixture_digests:
        raise QualificationRunError("E2E_RESUME_FIXTURE_BINDING_MISSING")
    if store.completed(contract.fixture_digests[0], 0) is not None:
        return
    cell_root = destination / "work" / "normal-completion"
    if not cell_root.exists():
        return
    try:
        provider_calls = evaluation_cell_provider_calls(cell_root / "state")
    except (OSError, ValueError) as error:
        reason = (
            "E2E_PROVIDER_EFFECT_RECONCILIATION_REQUIRED: provider_calls=[unavailable]; "
            f"기존 cell 원장을 읽을 수 없습니다: {error}"
        )
        store.set_state(EvaluationRunStatus.FAILED, updated_at=utc_now(), reason=reason)
        raise QualificationRunError(reason) from error
    unresolved = tuple(
        (call_id, status)
        for call_id, status in provider_calls
        if status != "released"
    )
    if not unresolved:
        return
    calls = ",".join(f"{call_id}={status}" for call_id, status in provider_calls)
    reason = (
        "E2E_PROVIDER_EFFECT_RECONCILIATION_REQUIRED: "
        f"provider_calls=[{calls}]; 완료된 E2E cell checkpoint 없이 provider 호출이 "
        "남았습니다. 기존 runtime intent, provider terminal과 예산 예약을 대조하기 "
        "전에는 새 provider 호출이나 새 Goal 예산으로 우회하지 않습니다."
    )
    store.set_state(EvaluationRunStatus.FAILED, updated_at=utc_now(), reason=reason)
    raise QualificationRunError(reason)


def _preserve_inventory_observation(
    path: Path, roles: EngineRoleConfiguration, inventory: ModelInventory
) -> None:
    """같은 digest 이름의 기존 관측도 전체 원문과 정확히 같아야 재사용한다."""

    expected = roles.operational_binding(inventory).model_dump(mode="json")
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != expected:
            raise QualificationRunError("E2E inventory observation 내용이 현재 binding과 다릅니다.")
        return
    _write_json(path, expected)


def _observe_frozen_plugin_identity(governance: Any, frozen: Any) -> dict[str, Any]:
    """현재 설치형 identity를 freeze의 네 E2E rebind 필드와 대조한다. 적합성 검사는 재실행하지 않는다."""
    from .governance_conformance import CHECK_SET_DIGEST

    try:
        observed = governance.inspect_identity()
        summary = observed["summary"]
        fields = {
            "closure_tree_digest": summary["closure_tree_digest"],
            "entrypoint_table_digest": summary["entrypoint_table_digest"],
            "check_set_digest": CHECK_SET_DIGEST,
            "conformance_result_digest": frozen.conformance_result_digest,
        }
    except Exception as error:
        raise QualificationRunError(f"E2E_GOVERNANCE_PREFLIGHT_FAILED:{error}") from error
    digest = sha256_digest(fields)
    if digest != frozen.e2e_identity_digest:
        raise QualificationRunError(
            "E2E_GOVERNANCE_PLUGIN_IDENTITY_MISMATCH: "
            f"기대 {frozen.e2e_identity_digest}, 관측 {digest}"
        )
    labels = observed.get("labels", ())
    manifest_label = next((item.get("plugin") for item in labels
                           if item.get("source") == "plugin_manifest_file"), None)
    server_info = next((item.get("serverInfo") for item in labels
                        if item.get("source") == "mcp_server_info"), None)
    audit_changes = {
        "manifest_sha256": summary.get("manifest_sha256") != frozen.manifest_sha256,
        "node_version": summary.get("node_version") != frozen.node_version,
        "plugin_version_label": (
            manifest_label.get("version") if isinstance(manifest_label, dict) else None
        ) != frozen.plugin_version_label,
        "server_info": server_info != frozen.server_info,
        "installation_root": str(governance.plugin_root.resolve()) != frozen.installation_root,
    }
    return {
        "governance_plugin_identity_digest": digest,
        "rebind_fields": fields,
        "audit_only_changes": audit_changes,
        "plugin_version_label": (
            manifest_label.get("version") if isinstance(manifest_label, dict) else None
        ),
    }


def _assert_no_transient_plugin_identity_change(prepared: "PreparedE2E") -> None:
    """cell 원장에 제품 gate가 관측한 transient identity drift가 있으면 PASS를 금지한다."""
    with prepared.service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM history_events WHERE project_id = ? "
            "AND event_type = 'operation.prepared' AND entity_type = 'core_operation'",
            (prepared.project_id,),
        ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        request = payload.get("request", {})
        if payload.get("kind") == "governance_gate" and str(request.get("step", "")).startswith(
            "plugin_identity_changed:"
        ):
            raise QualificationRunError("E2E_GOVERNANCE_PLUGIN_IDENTITY_CHANGED_DURING_CELL")


@dataclass
class PreparedE2E:
    service: EngineService
    project_id: str
    task_id: str
    plan_revision_id: str
    activation_digest: str
    proposal: ExecutionSpecProposal | None
    workspace: Path
    preparation_provenance: EvidenceProvenance = EvidenceProvenance.FAKE
    pipeline_stages: tuple[str, ...] = ()
    preparation_evidence_refs: tuple[str, ...] = ()


_CELL_STATE_SCHEMA = "flowmarshal.project-e2e.cell-state.v1"


def _write_prepared_state(
    cell_root: Path,
    prepared: PreparedE2E,
    *,
    evaluation_contract_digest: str,
    fixture_digest: str,
    governance_plugin_identity_digest: str | None = None,
) -> None:
    """사용량 제한·프로세스 중단 뒤 같은 cell을 이어갈 최소 권위 참조를 저장한다."""

    payload: dict[str, Any] = {
        "schema": _CELL_STATE_SCHEMA,
        "evaluation_contract_digest": evaluation_contract_digest,
        "fixture_digest": fixture_digest,
        "governance_plugin_identity_digest": governance_plugin_identity_digest,
        "project_id": prepared.project_id,
        "task_id": prepared.task_id,
        "plan_revision_id": prepared.plan_revision_id,
        "activation_digest": prepared.activation_digest,
        "workspace": str(prepared.workspace.resolve()),
        "proposal": (
            None
            if prepared.proposal is None
            else prepared.proposal.model_dump(mode="json")
        ),
        "preparation_provenance": prepared.preparation_provenance.value,
        "pipeline_stages": list(prepared.pipeline_stages),
        "preparation_evidence_refs": list(prepared.preparation_evidence_refs),
    }
    payload["state_digest"] = sha256_digest(payload)
    _write_json(cell_root / "cell-state.json", payload)


def _restore_prepared_state(
    cell_root: Path,
    *,
    evaluation_contract_digest: str,
    fixture_digest: str,
    governance_plugin_identity_digest: str | None = None,
) -> PreparedE2E:
    state_path = cell_root / "cell-state.json"
    if not state_path.is_file():
        raise QualificationRunError(
            "미완료 E2E cell에 재개 상태가 없습니다. 원장과 artifact를 보존하고 기존 "
            f"runtime intent, provider terminal과 예산 예약을 먼저 대조하세요: {cell_root}"
        )
    document = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise QualificationRunError("E2E cell 재개 상태는 JSON object여야 합니다.")
    state_digest = document.pop("state_digest", None)
    if state_digest != sha256_digest(document):
        raise QualificationRunError("E2E cell 재개 상태 digest가 일치하지 않습니다.")
    if document.get("schema") != _CELL_STATE_SCHEMA:
        raise QualificationRunError("지원하지 않는 E2E cell 재개 상태 schema입니다.")
    if document.get("evaluation_contract_digest") != evaluation_contract_digest:
        raise QualificationRunError("E2E cell이 현재 evaluation 계약과 다릅니다.")
    if document.get("fixture_digest") != fixture_digest:
        raise QualificationRunError("E2E cell이 현재 fixture와 다릅니다.")
    if document.get("governance_plugin_identity_digest") != governance_plugin_identity_digest:
        raise QualificationRunError("E2E cell이 현재 governance plugin identity와 다릅니다.")

    workspace = (cell_root / "workspace").resolve(strict=True)
    if Path(str(document.get("workspace"))).resolve(strict=True) != workspace:
        raise QualificationRunError("E2E cell workspace 결속이 다릅니다.")
    ledger = SQLiteEngineLedger(
        cell_root / "state" / "flowmarshal-engine.sqlite3",
        artifact_root=cell_root / "state" / "artifacts",
    )
    service = EngineService(ledger)
    service.initialize()
    proposal_document = document.get("proposal")
    proposal = (
        None
        if proposal_document is None
        else ExecutionSpecProposal.model_validate(proposal_document)
    )
    prepared = PreparedE2E(
        service=service,
        project_id=str(document["project_id"]),
        task_id=str(document["task_id"]),
        plan_revision_id=str(document["plan_revision_id"]),
        activation_digest=str(document["activation_digest"]),
        proposal=proposal,
        workspace=workspace,
        preparation_provenance=EvidenceProvenance(
            document.get("preparation_provenance", EvidenceProvenance.FAKE.value)
        ),
        pipeline_stages=tuple(document.get("pipeline_stages", ())),
        preparation_evidence_refs=tuple(
            document.get("preparation_evidence_refs", ())
        ),
    )
    status = service.status(prepared.project_id)
    if status["project"]["active_plan_revision_id"] != prepared.plan_revision_id:
        raise QualificationRunError("E2E cell의 active Plan 결속이 다릅니다.")
    if proposal is not None and proposal.task_id != prepared.task_id:
        raise QualificationRunError("E2E cell의 proposal Task 결속이 다릅니다.")
    return prepared


def _assignment(roles: EngineRoleConfiguration) -> ModelAssignmentContract:
    return ModelAssignmentContract(
        executor=RoleAssignmentPolicy(
            role="executor",
            preferred_model=roles.executor.model,
            preferred_effort=roles.executor.effort,
            allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.executor.allowed_fallbacks),
        ),
        validator=RoleAssignmentPolicy(
            role="validator",
            preferred_model=roles.validator.model,
            preferred_effort=roles.validator.effort,
            allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.validator.allowed_fallbacks),
        ),
        independence_required=True,
    )


def _prepare_from_raw_request(
    *,
    workspace: Path,
    state_root: Path,
    runtime: CodexRuntimePort,
    roles: EngineRoleConfiguration,
    evaluation_policies: EvaluationPolicies,
    evaluation_contract_digest: str,
    fixture_digest: str,
    source_request: str,
    structured_runner: Any | None = None,
) -> PreparedE2E:
    """실제 사용자 facade로 raw request부터 승인·Plan 활성화까지 수행한다."""

    ledger = SQLiteEngineLedger(
        state_root / "flowmarshal-engine.sqlite3",
        artifact_root=state_root / "artifacts",
    )
    authority = CoreActionAuthority()
    service = EngineService(ledger, action_authority=authority)
    service.initialize()
    project_id = service.create_project(name="Engine raw-request E2E", root=workspace)
    service.register_profile(_profile(project_id))
    BudgetManager(service).configure(project_id, evaluation_policies.budget)
    application = EngineApplication(
        service,
        runtime=runtime,
        role_configuration=roles,
        structured_runner=structured_runner,
    )
    preparation = application.prepare(
        project_id,
        source_request=source_request,
        candidate_count=1,
    )
    if (
        preparation.status != "ready_for_authorization"
        or preparation.planning is None
        or preparation.planning.selected_activation_digest is None
    ):
        raise QualificationRunError(
            "E2E_RAW_REQUEST_PIPELINE_NOT_READY: 실제 Goal/Planning 결과가 승인 후보를 "
            "만들지 못했습니다."
        )
    selected_digest = preparation.planning.selected_activation_digest
    selected = [
        item.plan
        for item in preparation.planning.plan_evaluations
        if item.plan.activation_digest == selected_digest
    ]
    if len(selected) != 1:
        raise QualificationRunError("E2E_SELECTED_PLAN_CARDINALITY_MISMATCH")
    plan = selected[0]
    authorization = application.authorize(
        project_id,
        source="qualification user authorization",
        capability=authority.issue_goal_authorization(
            ledger_path=ledger.path,
            target=service.goal_authorization_target(project_id=project_id),
        ),
    )
    evidence_root = state_root.parent
    raw_request_path = evidence_root / "raw-request.json"
    goal_path = evidence_root / "goal-preparation.json"
    planning_path = evidence_root / "planning-outcome.json"
    authorization_path = evidence_root / "goal-authorization.json"
    generated_plan_path = evidence_root / "generated-plan.json"
    activation_receipt_path = evidence_root / "plan-activation-receipt.json"
    _write_json(
        raw_request_path,
        {
            "schema": "flowmarshal.project-e2e.raw-request.v1",
            "evaluation_contract_digest": evaluation_contract_digest,
            "fixture_digest": fixture_digest,
            "source_request": source_request,
            "source_request_digest": sha256_bytes(source_request.encode("utf-8")),
        },
    )
    _write_json(goal_path, preparation.goal_preparation)
    _write_json(planning_path, preparation.planning)
    _write_json(authorization_path, authorization)
    _write_json(
        generated_plan_path,
        {
            "schema": "flowmarshal.project-e2e.generated-plan.v2",
            "evaluation_contract_digest": evaluation_contract_digest,
            "fixture_digest": fixture_digest,
            "source": "EngineApplication.prepare",
            "planning_search_id": preparation.planning_search_id,
            "plan": plan.model_dump(mode="json"),
            "plan_revision_id": plan.plan_revision_id,
            "activation_digest": plan.activation_digest,
        },
    )
    _write_json(
        activation_receipt_path,
        {
            "schema": "flowmarshal.project-e2e.plan-activation-receipt.v2",
            "evaluation_contract_digest": evaluation_contract_digest,
            "fixture_digest": fixture_digest,
            "source": "EngineApplication.authorize",
            "authorization_id": authorization.authorization.authorization_id,
            "activation_id": authorization.activation_id,
            "plan_revision_id": plan.plan_revision_id,
            "activation_digest": plan.activation_digest,
        },
    )
    with service.ledger.read() as connection:
        role_rows = connection.execute(
            "SELECT role FROM provider_calls WHERE project_id=? ORDER BY rowid",
            (project_id,),
        ).fetchall()
    actual_roles = tuple(str(row["role"]) for row in role_rows)
    required_roles = (
        "goal_normalizer",
        "goal_reviewer",
        "skeleton_generator",
        "skeleton_reviewer",
        "plan_expander",
    )
    if not all(role in actual_roles for role in required_roles) or not any(
        role in {"plan_reviewer", "compact_plan_reviewer", "critical_plan_reviewer"}
        for role in actual_roles
    ):
        raise QualificationRunError(
            "E2E_RAW_REQUEST_ROLE_COVERAGE_MISSING: " + ",".join(actual_roles)
        )
    return PreparedE2E(
        service=service,
        project_id=project_id,
        task_id=plan.definition.tasks[0].task_id,
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        proposal=None,
        workspace=workspace,
        preparation_provenance=EvidenceProvenance.LIVE,
        pipeline_stages=(
            "raw_request",
            "goal_normalizer",
            "goal_reviewer",
            "skeleton_generator",
            "skeleton_reviewer",
            "plan_expander",
            "plan_reviewer",
            "goal_authorization",
            "plan_activation",
        ),
        preparation_evidence_refs=tuple(
            str(path.resolve())
            for path in (
                raw_request_path,
                goal_path,
                planning_path,
                authorization_path,
                generated_plan_path,
                activation_receipt_path,
            )
        ),
    )


def _prepare(
    *,
    workspace: Path,
    state_root: Path,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    goal_validation: IntegrationValidationContract | None = None,
    semantic_task_validation: bool = False,
    evaluation_policies: EvaluationPolicies | None = None,
    evaluation_contract_digest: str | None = None,
    fixture_digest: str | None = None,
) -> PreparedE2E:
    ledger = SQLiteEngineLedger(
        state_root / "flowmarshal-engine.sqlite3",
        artifact_root=state_root / "artifacts",
    )
    authority = CoreActionAuthority()
    service = EngineService(ledger, action_authority=authority)
    service.initialize()
    project_id = service.create_project(name="Engine project E2E", root=workspace)
    profile = _profile(project_id)
    service.register_profile(profile)

    request = (
        "app.py의 add 함수가 두 정수의 합을 반환하도록 최소 수정하고 공개 함수 계약을 "
        "유지하며 test_app.py 검증을 통과시켜줘."
    )
    request_digest = sha256_bytes(request.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=request,
        source_request_digest=request_digest,
        mission_class=MissionClass.BUGFIX_STABILIZATION,
        observable_outcome="add(2, 3)이 5를 반환하고 회귀 테스트가 통과한다.",
        hard_acceptance=(
            GoalCriterion(
                criterion_id="ac_fix",
                statement="add(2, 3)이 5를 반환한다.",
                validation_intent="Python unittest로 실제 동작을 확인한다.",
                trace_refs=("trace_request",),
            ),
        ),
        non_goals=("공개 함수 이름과 test_app.py 변경",),
        source_traces=(
            SourceTrace(
                trace_id="trace_request",
                source_ref="user-request",
                statement=request,
                source_digest=request_digest,
            ),
        ),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_policy=BehaviorPolicy.PRESERVE_PUBLIC_CONTRACTS,
        ),
        profile_definition_digest=profile.definition_digest,
    )
    goal = GoalContractRevision(
        goal_revision_id=new_id("goal_revision"),
        goal_id=new_id("goal"),
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    service.register_goal(goal)
    if evaluation_policies is not None:
        BudgetManager(service).configure(
            project_id,
            evaluation_policies.budget,
            goal_id=goal.goal_id,
        )
    project_map, state = service.reobserve_project(project_id)

    skeleton = PlanSkeletonCandidate(
        candidate_id=new_id("candidate"),
        goal_contract_digest=goal.definition_digest,
        state_signature=state.semantic_digest,
        approach=ApproachSignature(
            strategy_family="minimal arithmetic bugfix",
            change_shape="one implementation file",
            compatibility="preserve add public contract",
            rollout_recovery="digest-bound validation and retry",
        ),
        tasks=(
            TaskSkeleton(
                task_ref="task_fix_add",
                kind=TaskKind.CHANGE,
                objective="app.py의 add 구현만 최소 수정해 실제 덧셈을 반환하게 한다.",
                contributes_to=("ac_fix",),
                produces=("artifact:fixed-add",),
                consumes=("input:app.py", "input:test_app.py"),
            ),
        ),
        goal_coverage=(GoalCoverage(criterion_id="ac_fix", task_refs=("task_fix_add",)),),
        estimated_change_cost=1,
        estimated_context_tokens=1500,
    )
    skeleton_review = ReviewerSubmission(
        reviewer_role="qualification-skeleton-reviewer",
        candidate_digest=sha256_digest(skeleton),
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(
            skeleton_review_evidence_catalog(skeleton, goal, state, project_map)
        ),
    )
    service.record_skeleton_evaluation(
        CandidateEvaluation(
            candidate=skeleton,
            semantic_submission=skeleton_review,
            decision=CandidateDecision(
                candidate_digest=sha256_digest(skeleton),
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
    )

    assignment = _assignment(roles)
    task = TaskContract(
        task_id=new_id("task"),
        task_ref="task_fix_add",
        project_id=project_id,
        kind=TaskKind.CHANGE,
        objective="app.py의 add 구현만 최소 수정해 실제 덧셈을 반환하게 한다.",
        goal_criterion_refs=("ac_fix",),
        produces=("artifact:fixed-add",),
        consumes=("input:app.py", "input:test_app.py"),
        acceptance_criteria=("Python unittest가 종료 코드 0으로 통과한다.",),
        validations=(
            ValidationContract(
                validation_id="validation_unittest",
                statement="실제 unittest를 실행한다.",
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ) + ((ValidationContract(
            validation_id="validation_public_contract", statement="직접 파일·테스트 evidence로 공개 함수 계약 보존을 독립 검토한다.",
            method="semantic", required_evidence_kinds=("model_review", "file", "test"),
        ),) if semantic_task_validation else ()),
        risk_level=RiskLevel.LOW,
        approval_class=ApprovalClass.PLAN_ACTIVATION,
        recovery=RecoveryEnvelope(
            retryable_failure_classes=("implementation", "context", "environment"),
        ),
        assignment=assignment,
    )
    plan_definition = PlanContractDefinition(
        project_id=project_id,
        goal_contract_digest=goal.definition_digest,
        base_state_snapshot_digest=state.snapshot_digest,
        project_map_digest=project_map.revision_digest,
        source_skeleton_digest=sha256_digest(skeleton),
        tasks=(task,),
        goal_coverage=(
            PlanGoalCoverage(
                criterion_id="ac_fix",
                task_ids=(task.task_id,),
                validation_ids=("validation_unittest", "validation_goal") + (("validation_public_contract",) if semantic_task_validation else ()),
            ),
        ),
        integration_validations=(
            goal_validation or IntegrationValidationContract(
                validation_id="validation_goal",
                statement="완료 후 unittest를 독립 재실행하여 Goal의 통합 동작을 확인한다.",
                criterion_refs=("ac_fix",),
                method="deterministic",
                required_evidence_kinds=("test",),
            ),
        ),
        model_inventory_digest=inventory.inventory_digest,
        expected_effects=("app.py의 add 구현 최소 수정",),
        prohibited_effects=("test_app.py 또는 공개 함수 계약 변경",),
    )
    plan = PlanContractRevision(
        plan_revision_id=new_id("plan_revision"),
        plan_id=new_id("plan"),
        revision_no=1,
        definition=plan_definition,
        definition_digest=plan_definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )
    plan_review = ReviewerSubmission(
        reviewer_role="qualification-plan-reviewer",
        candidate_digest=plan.activation_digest,
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(
            plan_review_evidence_catalog(plan, goal, state, project_map)
        ),
    )
    service.register_plan_evaluation(
        ExpandedPlanEvaluation(
            plan=plan,
            semantic_submissions=(plan_review,),
            decision=CandidateDecision(
                candidate_digest=plan.activation_digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
    )
    _write_json(
        state_root.parent / "generated-plan.json",
        {
            "schema": "flowmarshal.project-e2e.generated-plan.v1",
            "evaluation_contract_digest": evaluation_contract_digest,
            "fixture_digest": fixture_digest,
            "plan": plan.model_dump(mode="json"),
            "plan_revision_id": plan.plan_revision_id,
            "activation_digest": plan.activation_digest,
        },
    )
    service.authorize_goal(
        project_id=project_id, source="합성 qualification 승인",
        capability=authority.issue_goal_authorization(
            ledger_path=ledger.path,
            target=service.goal_authorization_target(project_id=project_id),
        ),
    )
    service.activate_plan(
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        source="qualification",
    )
    _write_json(
        state_root.parent / "plan-activation-receipt.json",
        {
            "schema": "flowmarshal.project-e2e.plan-activation-receipt.v1",
            "evaluation_contract_digest": evaluation_contract_digest,
            "fixture_digest": fixture_digest,
            "plan_revision_id": plan.plan_revision_id,
            "activation_digest": plan.activation_digest,
            "activation_source": "qualification",
            "driver": "flowmarshal-engine-eval project-e2e",
            "workspace": str(workspace.resolve()),
        },
    )
    app_entry = next(item for item in project_map.entries if item.path == "app.py")
    proposal = ExecutionSpecProposal(
        task_id=task.task_id,
        context_needs=(
            ExecutionContextNeed(
                need_id="source_app",
                description="수정 대상 구현",
                path_hints=("app.py",),
            ),
            ExecutionContextNeed(
                need_id="test_app",
                description="고정 회귀 테스트",
                path_hints=("test_app.py",),
            ),
        ),
        resolved_targets=(
            ResolvedTarget(
                target_ref="target_app",
                path="app.py",
                expected_content_digest=app_entry.content_digest,
                access="write",
            ),
            ResolvedTarget(
                target_ref="target_test",
                path="test_app.py",
                expected_content_digest=next(
                    item.content_digest for item in project_map.entries if item.path == "test_app.py"
                ),
                access="read",
            ),
        ),
        actions=(
            ExecutionAction(
                action_ref="action_fix",
                kind="edit",
                description="app.py의 뺄셈 연산만 덧셈으로 바꾼다.",
            ),
        ),
        validation_steps=(
            ValidationExecutionStep(
                validation_id="validation_unittest",
                method="deterministic",
                argv=(sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"),
                working_directory=str(workspace),
                timeout_seconds=60,
                expected_exit_codes=(0,),
                required_evidence_kinds=("test",),
            ),
        ) + ((ValidationExecutionStep(
            validation_id="validation_public_contract", method="semantic",
            semantic_instruction="실제 파일·테스트 관측을 검토해 add 공개 함수의 이름·인자·덧셈 계약 보존을 확인한다.",
            required_evidence_kinds=("model_review", "file", "test"),
        ),) if semantic_task_validation else ()),
        resource_locks=(f"file:{workspace / 'app.py'}",),
        timeout_seconds=900,
        idempotency_hint="project-e2e-add-fix",
    )
    return PreparedE2E(
        service=service,
        project_id=project_id,
        task_id=task.task_id,
        plan_revision_id=plan.plan_revision_id,
        activation_digest=plan.activation_digest,
        proposal=proposal,
        workspace=workspace,
    )


def _copy_fixture(root: Path, cell_root: Path) -> tuple[Path, str]:
    source = root / "tests" / "fixtures" / "engine" / "project-e2e"
    source_digest = sha256_digest(
        {
            path.relative_to(source).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(source.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
    )
    workspace = cell_root / "workspace"
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return workspace, source_digest


def _status_assertions(prepared: PreparedE2E, source_digest: str) -> dict[str, Any]:
    status = prepared.service.status(prepared.project_id)
    with prepared.service.ledger.read() as connection:
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE project_id = ?",
            (prepared.project_id,),
        ).fetchone()[0]
        validation_count = connection.execute(
            "SELECT COUNT(*) FROM validation_results WHERE plan_revision_id = ? AND status = 'pass'",
            (prepared.plan_revision_id,),
        ).fetchone()[0]
        binding_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE project_id = ? AND binding_json IS NOT NULL",
            (prepared.project_id,),
        ).fetchone()[0]
        verdict_count = connection.execute(
            "SELECT COUNT(*) FROM goal_verdicts WHERE plan_revision_id = ?",
            (prepared.plan_revision_id,),
        ).fetchone()[0]
        goal_direct_evidence_count = connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE project_id = ? AND task_id IS NULL "
            "AND kind = 'test' AND source_ref LIKE 'validation:validation_goal:%'",
            (prepared.project_id,),
        ).fetchone()[0]
        plan_row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?", (prepared.plan_revision_id,)).fetchone()
        definition = json.loads(plan_row["payload_json"])["definition"]
        expected_validation_count = sum(len(item["validations"]) for item in definition["tasks"]) + len(definition["integration_validations"])
    passed = all(
        (
            status["project"]["run_state"] == "completed",
            status["history_valid"],
            evidence_count >= 3,
            validation_count == expected_validation_count,
            binding_count >= 1,
            verdict_count == 1,
            goal_direct_evidence_count >= 1,
            (prepared.workspace / "app.py").is_file(),
        )
    )
    return {
        "passed": passed,
        "source_fixture_digest": source_digest,
        "activation_digest": prepared.activation_digest,
        "run_state": status["project"]["run_state"],
        "history_valid": status["history_valid"],
        "evidence_count": evidence_count,
        "pass_validation_count": validation_count,
        "binding_count": binding_count,
        "goal_verdict_count": verdict_count,
        "independent_goal_test_evidence_count": goal_direct_evidence_count,
    }


def _normal_completion(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *, roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    provider = _execution_proposal_provider(prepared, runtime, roles)
    if provider is None and prepared.proposal is None:
        raise QualificationRunError(
            "E2E_EXECUTION_PROPOSAL_PROVIDER_REQUIRED: raw-request E2E는 실제 "
            "execution_spec_prepare 역할을 사용해야 합니다."
        )
    task_gate = _open_e2e_task_gate(prepared, runtime, roles, governance)
    try:
        return _drive_normal_completion(prepared, runtime, source_digest, provider, task_gate)
    finally:
        task_gate.close()


def _execution_proposal_provider(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    roles: EngineRoleConfiguration | None,
) -> Any | None:
    from .execution import ExecutionProposalAdapter

    provider = None
    if roles is not None:
        with prepared.service.ledger.read() as connection:
            goal = connection.execute(
                "SELECT g.goal_id, g.definition_digest FROM projects p "
                "JOIN goal_revisions g ON g.id = p.active_goal_revision_id WHERE p.id = ?",
                (prepared.project_id,),
            ).fetchone()
        if goal is None:
            raise QualificationRunError("E2E_GOAL_BUDGET_BINDING_MISSING")
        provider = ExecutionProposalAdapter(
            prepared.service,
            budgeted_role_runner(
                runtime,
                prepared.service,
                project_id=prepared.project_id,
                goal_id=goal["goal_id"],
                goal_digest=goal["definition_digest"],
            ),
            roles,
        )
    return provider


def _open_e2e_task_gate(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    roles: EngineRoleConfiguration | None,
    governance: Any | None,
) -> Any:
    from .governance_gate import MissingGovernanceGate
    from .roles import CodexStructuredRoleRunner

    # 실행 Task는 제품 경로와 같은 필수 governance gate를 지난다. 설정이 없으면 GOVERNANCE_GATE_REQUIRED로 멈춘다.
    return MissingGovernanceGate() if governance is None else governance.open_gate(
        prepared.service, runtime=runtime, roles=roles,
        runner=None if roles is None else CodexStructuredRoleRunner(
            runtime, max_schema_recovery_attempts=0, ephemeral_threads=False),
    )


def _drive_normal_completion(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    provider: Any,
    task_gate: Any,
) -> dict[str, Any]:
    dispatcher = EngineDispatcher(prepared.service, runtime, proposal_provider=provider, task_gate=task_gate)
    deadline = time.monotonic() + 900
    actions: list[str] = []
    if prepared.service.status(prepared.project_id)["project"]["run_state"] == "completed":
        result = _status_assertions(prepared, source_digest)
        result["actions"] = ["restored_completed"]
        return result
    while time.monotonic() < deadline:
        # proposal은 ready 상태에서만 소비된다. 중단 위치와 무관하게 같은
        # digest-bound proposal로 materialize/dispatch/observe/validate를 재개한다.
        outcome = dispatcher.run_once(
            prepared.project_id, proposal=prepared.proposal if provider is None else None,
            goal_validation_step=(prepared.proposal.validation_steps[0].model_copy(
                update={"validation_id": "validation_goal"}) if provider is None else None),
        )
        actions.append(outcome.action.value)
        if outcome.action is RunOnceAction.COMPLETED and outcome.goal_verdict_id is not None:
            result = _status_assertions(prepared, source_digest)
            result["actions"] = actions
            result["automatic_preparation"] = provider is not None
            return result
        if outcome.action is RunOnceAction.BLOCKED:
            return {
                "passed": False,
                "source_fixture_digest": source_digest,
                "actions": actions,
                "failure": f"{outcome.blocker_code}: {outcome.detail}",
            }
        time.sleep(0.5)
    return {
        "passed": False,
        "source_fixture_digest": source_digest,
        "actions": actions,
        "failure": "900초 안에 실제 Codex E2E가 완료되지 않음",
    }


def _materialize_with_application(
    application: EngineApplication,
    prepared: PreparedE2E,
    *,
    timeout_seconds: float = 900,
) -> Any:
    """비동기 execution-spec 준비를 완료한 뒤 materialized 결과를 돌려준다."""

    outcome = application.run_once(
        prepared.project_id,
        proposal=prepared.proposal,
    )
    deadline = time.monotonic() + timeout_seconds
    while outcome.action is not RunOnceAction.MATERIALIZED and time.monotonic() < deadline:
        if outcome.action is RunOnceAction.BLOCKED:
            raise QualificationRunError(
                f"{outcome.blocker_code}: {outcome.detail}"
            )
        time.sleep(0.01)
        outcome = application.run_once(prepared.project_id)
    if outcome.action is not RunOnceAction.MATERIALIZED:
        raise QualificationRunError(
            f"{timeout_seconds}초 안에 execution spec이 materialize되지 않음"
        )
    return outcome


def _stale_after_materialization(
    prepared: PreparedE2E,
    runtime: Any,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
    )
    first = _materialize_with_application(application, prepared)
    # 쓰기 target 변경은 governance의 사용자 변경 보호가 먼저 막는다. 여기서는
    # 고정된 read-context를 바꿔 실제 효과 직전 freshness 검사를 직접 관측한다.
    (prepared.workspace / "test_app.py").write_text(
        (prepared.workspace / "test_app.py").read_text(encoding="utf-8")
        + "\n# external context change\n",
        encoding="utf-8",
    )
    provider_effect_counts_before = (
        runtime.create_calls,
        runtime.turn_calls,
        runtime.resume_calls,
    )
    failure: str | None = None
    try:
        outcome = application.run_once(prepared.project_id)
        if outcome.action is RunOnceAction.BLOCKED:
            failure = f"{outcome.blocker_code}: {outcome.detail}"
    except EngineServiceError as error:
        failure = str(error)
    with prepared.service.ledger.read() as connection:
        execution_attempt_count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id=? AND kind='execution'",
            (prepared.task_id,),
        ).fetchone()[0]
    provider_effect_count = sum(
        current - before
        for current, before in zip(
            (runtime.create_calls, runtime.turn_calls, runtime.resume_calls),
            provider_effect_counts_before,
        )
    )
    return {
        "passed": first.action is RunOnceAction.MATERIALIZED
        and failure is not None
        and "STALE_EXECUTION_INPUT" in failure
        and provider_effect_count == 0
        and execution_attempt_count == 0,
        "source_fixture_digest": source_digest,
        "error": failure,
        "effect_count": provider_effect_count,
        "worker_attempt_count": execution_attempt_count,
        "thread_create_count": runtime.create_calls,
    }


def _restart_resume(
    prepared: PreparedE2E,
    runtime: FakeCodexRuntime,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
    )
    _materialize_with_application(application, prepared)
    dispatched = application.run_once(prepared.project_id)
    deadline = time.monotonic() + 2
    row = None
    while time.monotonic() < deadline:
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
            ).fetchone()
        if row is not None and row["binding_json"] is not None:
            candidate = ThreadBinding.model_validate_json(row["binding_json"])
            if candidate.turn_id is not None:
                break
        time.sleep(0.01)
    binding = ThreadBinding.model_validate_json(row["binding_json"])
    runtime.threads[binding.thread_id].terminal_status = "interrupted"
    restarted = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
    )
    resumed = restarted.run_once(prepared.project_id)
    resume_deadline = time.monotonic() + 2
    while runtime.resume_calls == 0 and time.monotonic() < resume_deadline:
        time.sleep(0.01)
        resumed = restarted.run_once(prepared.project_id)
    read_before_resume = runtime.read_calls >= 1 and runtime.resume_calls == 1
    runtime.complete(binding.thread_id, response="재개된 worker가 종료됨")
    observed = restarted.run_once(prepared.project_id)
    observe_deadline = time.monotonic() + 2
    while observed.action is not RunOnceAction.OBSERVED and time.monotonic() < observe_deadline:
        time.sleep(0.01)
        observed = restarted.run_once(prepared.project_id)
    return {
        "passed": resumed.action is RunOnceAction.DISPATCHED
        and observed.action is RunOnceAction.OBSERVED
        and read_before_resume
        and runtime.create_calls == 1,
        "source_fixture_digest": source_digest,
        "read_calls": runtime.read_calls,
        "resume_calls": runtime.resume_calls,
        "thread_create_count": runtime.create_calls,
        "turn_start_count": runtime.turn_calls,
    }


def _unknown_receipt(
    prepared: PreparedE2E,
    runtime: Any,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
    )
    _materialize_with_application(application, prepared)

    fault_triggered = False

    def fault(point: str) -> None:
        nonlocal fault_triggered
        if point == "after_thread_effect":
            fault_triggered = True
            raise RuntimeError("fault after provider effect before receipt")

    dispatcher = application._dispatcher()
    dispatcher.fault_hook = fault
    dispatcher.run_once(prepared.project_id)
    recovery_deadline = time.monotonic() + 2
    recovered = application.run_once(prepared.project_id)
    while time.monotonic() < recovery_deadline:
        with prepared.service.ledger.read() as connection:
            intent_statuses = {
                row["status"]
                for row in connection.execute(
                    "SELECT i.status FROM runtime_intents i JOIN attempts a ON a.id=i.attempt_id "
                    "WHERE a.project_id=? AND i.kind='create_thread'",
                    (prepared.project_id,),
                )
            }
        if fault_triggered and intent_statuses & {"unknown", "received"}:
            break
        time.sleep(0.01)
        recovered = application.run_once(prepared.project_id)
    reconciliation: dict[str, Any] | None = None
    if (
        isinstance(runtime, RecordedRuntime)
        and (
            recovered.action is RunOnceAction.OBSERVED
            or (
                recovered.action is RunOnceAction.BLOCKED
                and recovered.blocker_code == "EXTERNAL_EFFECT_UNKNOWN"
            )
        )
    ):
        reconciliation = _reconcile_unknown_empty_thread(prepared, runtime)
    return {
        "passed": fault_triggered
        and (
            recovered.action is RunOnceAction.OBSERVED
            or (
                recovered.action is RunOnceAction.BLOCKED
                and recovered.blocker_code == "EXTERNAL_EFFECT_UNKNOWN"
            )
        )
        and runtime.create_calls == 1
        and runtime.turn_calls == 0
        and (reconciliation is None or reconciliation["passed"]),
        "source_fixture_digest": source_digest,
        "thread_create_count": runtime.create_calls,
        "turn_start_count": runtime.turn_calls,
        "recovery_action": recovered.action.value,
        "blocker_code": recovered.blocker_code,
        "empty_thread_reconciliation": reconciliation,
    }


def _reconcile_unknown_empty_thread(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
) -> dict[str, Any]:
    """trace/외부 journal로 결속된 생성 receipt의 turn 0을 관측해 해소한다."""

    with prepared.service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT i.*, a.project_id FROM runtime_intents i "
            "JOIN attempts a ON a.id=i.attempt_id "
            "WHERE a.project_id=? AND i.kind='create_thread' "
            "AND i.status IN ('unknown','received')",
            (prepared.project_id,),
        ).fetchall()
    if len(rows) != 1:
        raise QualificationRunError(
            f"E2E_UNKNOWN_INTENT_CARDINALITY: expected=1 actual={len(rows)}"
        )
    intent = rows[0]
    request = json.loads(intent["request_json"])
    expected_inventory_digest = request.get("model_observation", {}).get(
        "inventory_digest"
    )
    matching = [
        item for item in runtime.events
        if item.get("operation") == "create_thread"
        and item.get("request_binding", {}).get("model") == request.get("model")
        and item.get("request_binding", {}).get("cwd") == request.get("cwd")
        and item.get("request_binding", {}).get("inventory_digest")
        == expected_inventory_digest
    ]
    if len(matching) != 1:
        raise QualificationRunError(
            f"E2E_UNKNOWN_CREATE_RECEIPT_CARDINALITY: expected=1 actual={len(matching)}"
        )
    event = matching[0]
    receipt = RuntimeOperationReceipt.model_validate(event["receipt"])
    if receipt.binding is None or receipt.binding.turn_id is not None:
        raise QualificationRunError("E2E_UNKNOWN_CREATE_RECEIPT_BINDING_MISMATCH")
    thread = receipt.payload.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != receipt.binding.thread_id or thread.get("turns") != []:
        raise QualificationRunError("E2E_UNKNOWN_CREATE_RECEIPT_NOT_EMPTY_THREAD")
    if intent["status"] == "unknown":
        restored_receipt = prepared.service.record_runtime_receipt(
            intent_id=intent["id"],
            provider_operation_id=receipt.operation_id,
            response=receipt.payload,
            binding=receipt.binding,
            allow_reconcile_unknown=True,
        )
    else:
        with prepared.service.ledger.read() as connection:
            stored = connection.execute(
                "SELECT payload_json FROM runtime_receipts WHERE intent_id=?",
                (intent["id"],),
            ).fetchone()
        if stored is None:
            raise QualificationRunError("E2E_RECEIVED_CREATE_RECEIPT_MISSING")
        restored_receipt = RuntimeReceipt.model_validate_json(stored["payload_json"])
        if restored_receipt.binding != receipt.binding:
            raise QualificationRunError("E2E_RECEIVED_CREATE_RECEIPT_BINDING_MISMATCH")
    observation = runtime.read_stored(thread_id=receipt.binding.thread_id)
    if observation.thread_id != receipt.binding.thread_id or observation.turn_id is not None or observation.active:
        raise QualificationRunError("E2E_UNKNOWN_THREAD_NOT_EMPTY_ON_READ")
    if any(item.get("operation") in {"start_turn", "resume"} for item in runtime.events):
        raise QualificationRunError("E2E_UNKNOWN_THREAD_TURN_EFFECT_DETECTED")
    with prepared.service.ledger.read() as connection:
        reserved_calls = connection.execute(
            "SELECT id FROM provider_calls WHERE project_id=? AND attempt_id=?",
            (prepared.project_id, intent["attempt_id"]),
        ).fetchall()
    if len(reserved_calls) != 1:
        raise QualificationRunError("E2E_UNKNOWN_PROVIDER_RESERVATION_CARDINALITY")
    BudgetManager(prepared.service).release_empty_created_thread(
        reserved_calls[0]["id"],
        receipt=restored_receipt,
        observation=observation,
    )
    with prepared.service.ledger.read() as connection:
        provider_calls = connection.execute(
            "SELECT id, status, actual_tokens, receipt_json, usage_id FROM provider_calls "
            "WHERE project_id=? AND attempt_id=?",
            (prepared.project_id, intent["attempt_id"]),
        ).fetchall()
        usage_count = connection.execute(
            "SELECT COUNT(*) FROM budget_usage WHERE project_id=?",
            (prepared.project_id,),
        ).fetchone()[0]
        intent_status = connection.execute(
            "SELECT status FROM runtime_intents WHERE id=?", (intent["id"],)
        ).fetchone()[0]
        attempt_status = connection.execute(
            "SELECT status FROM attempts WHERE id=?", (intent["attempt_id"],)
        ).fetchone()[0]
        release_history_count = connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE project_id=? "
            "AND event_type='budget.empty_thread_reservation_released' "
            "AND entity_type='provider_call' AND entity_id=?",
            (prepared.project_id, reserved_calls[0]["id"]),
        ).fetchone()[0]
    if len(provider_calls) != 1:
        raise QualificationRunError("E2E_UNKNOWN_PROVIDER_RESERVATION_CARDINALITY")
    provider_call = provider_calls[0]
    reservation_released = (
        provider_call["status"] == "released"
        and provider_call["actual_tokens"] is None
        and provider_call["receipt_json"] is None
        and provider_call["usage_id"] is None
    )
    passed = all(
        (
            intent_status == "received",
            attempt_status == "failed",
            usage_count == 0,
            reservation_released,
            release_history_count == 1,
            runtime.create_calls == 1,
            runtime.turn_calls == 0,
        )
    )
    return {
        "passed": passed,
        "thread_id": receipt.binding.thread_id,
        "thread_read_without_resume": True,
        "observed_turn_count": 0,
        "model_turn_effect": False,
        "model_usage": None,
        "model_usage_reason": "NO_MODEL_TURN_OBSERVED",
        "runtime_intent_status": intent_status,
        "attempt_status": attempt_status,
        "provider_reservation_status": provider_call["status"],
        "provider_reservation_released": reservation_released,
        "provider_reservation_unresolved": False,
        "provider_actual_tokens": provider_call["actual_tokens"],
        "provider_receipt": (
            None
            if provider_call["receipt_json"] is None
            else json.loads(provider_call["receipt_json"])
        ),
        "provider_usage_id": provider_call["usage_id"],
        "reservation_release_history_count": release_history_count,
        "budget_usage_count": usage_count,
        "next_action": "복원된 create receipt와 turn 0 관측으로 예약을 해제했습니다.",
    }


def _live_restart_resume(
    prepared: PreparedE2E,
    source_digest: str,
    *,
    cell_root: Path,
    contract: EvaluationContract,
    fixture_digest: str,
    codex_bin: Path | str | None,
    governance_plugin_identity_digest: str | None = None,
    roles: EngineRoleConfiguration | None = None,
    project_binding: CodexProjectBinding | None = None,
    runtime_selection: RuntimeProviderSelection | None = None,
    claude_state_root: Path | None = None,
    governance: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """실제 App Server 연결과 Core 인스턴스를 닫은 뒤 같은 저장 turn을 재관측한다."""

    journal = cell_root / "runtime-receipts.json"
    with open_harness_runtime(
        runtime_selection, codex_factory=CodexAppServerRuntime, codex_bin=codex_bin,
        project_binding=project_binding,
        default_state_root=claude_state_root or cell_root / "claude-threads",
    ) as first_runtime:
        recorded = RecordedRuntime(first_runtime, journal=journal)
        application = EngineApplication(
            prepared.service,
            runtime=recorded,
            role_configuration=roles,
            governance=governance,
        )
        _materialize_with_application(application, prepared, timeout_seconds=30)
        dispatched = application.run_once(prepared.project_id)
        binding_deadline = time.monotonic() + 30
        row = None
        while time.monotonic() < binding_deadline:
            with prepared.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()
            if row is not None and row["binding_json"] is not None:
                candidate = ThreadBinding.model_validate_json(row["binding_json"])
                if candidate.turn_id is not None:
                    break
            time.sleep(0.05)
        if row is None or row["binding_json"] is None:
            raise QualificationRunError("restart E2E의 실제 thread/turn binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        # start receipt와 로컬 Future만으로 저장 turn의 준비를 추정하지 않는다.
        # Windows에서 빈 rollout이 잠시 보일 수 있으므로 비재개 조회로 확인한다.
        deadline = time.monotonic() + 30
        observation = None
        last_read_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                observation = recorded.read_stored(thread_id=binding.thread_id)
            except Exception as error:
                last_read_error = error
            if observation is not None and observation.turn_id == binding.turn_id:
                break
            time.sleep(0.25)
        if observation is None or observation.turn_id != binding.turn_id:
            raise QualificationRunError(f"restart fault 전 저장 turn을 확인하지 못했습니다: {last_read_error}")
        if not observation.active or binding.turn_id is None:
            raise QualificationRunError("restart fault 전에 실제 turn이 종료돼 재개 경로를 검증하지 못했습니다.")
        try:
            recorded.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
        except Exception as error:
            raise QualificationRunError(f"저장 turn 확인 후 실제 interrupt가 실패했습니다: {error}") from error
        deadline = time.monotonic() + 30
        while observation.active and time.monotonic() < deadline:
            time.sleep(0.25)
            observation = recorded.read(thread_id=binding.thread_id)
        if observation.terminal_status not in {"interrupted", "cancelled", "canceled"}:
            raise QualificationRunError("실제 turn의 중단을 확인하지 못했습니다.")

    restored = _restore_prepared_state(
        cell_root,
        evaluation_contract_digest=contract.contract_digest,
        fixture_digest=fixture_digest,
        governance_plugin_identity_digest=governance_plugin_identity_digest,
    )
    with open_harness_runtime(
        runtime_selection, codex_factory=CodexAppServerRuntime, codex_bin=codex_bin,
        project_binding=project_binding,
        default_state_root=claude_state_root or cell_root / "claude-threads",
    ) as second_runtime:
        recorded = RecordedRuntime(second_runtime, journal=journal)
        restart_index = len(recorded.events)
        restarted = EngineApplication(
            restored.service,
            runtime=recorded,
            role_configuration=roles,
            governance=governance,
            supervisor=RuntimeJobSupervisor(
                restored.service,
                recorded,
                observation_timeout_seconds=5.0,
            ),
        )
        resumed = restarted.run_once(restored.project_id)
        resume_deadline = time.monotonic() + 30
        while recorded.resume_calls == 0 and time.monotonic() < resume_deadline:
            time.sleep(0.05)
            resumed = restarted.run_once(restored.project_id)
        restart_operations = [item["operation"] for item in recorded.events[restart_index:]]
        stored_read_indexes = [
            index
            for index, operation in enumerate(restart_operations)
            if operation in {"read", "read_stored"}
        ]
        read_before_resume = (
            bool(stored_read_indexes)
            and "resume" in restart_operations
            and stored_read_indexes[0] < restart_operations.index("resume")
        )
        result = _normal_completion(
            restored,
            recorded,
            source_digest,
            roles=roles,
            governance=governance,
        )
        result.update(
            {
                "read_before_resume": read_before_resume,
                "thread_create_count": recorded.create_calls,
                "turn_start_count": recorded.turn_calls,
                "resume_calls": recorded.resume_calls,
            }
        )
        result["passed"] = bool(result["passed"]) and all(
            (
                resumed.action is RunOnceAction.DISPATCHED,
                read_before_resume,
                recorded.create_calls == 1,
                recorded.turn_calls == 2,
                recorded.resume_calls == 1,
            )
        )
        return result, recorded.events


def _cancel_active_job(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """활성 turn을 취소하고 새 provider 효과 없이 exact turn을 terminal로 정리한다."""

    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=RuntimeJobSupervisor(
            prepared.service,
            runtime,
            observation_timeout_seconds=5.0,
        ),
    )
    _materialize_with_application(application, prepared, timeout_seconds=timeout_seconds)
    dispatched = application.run_once(prepared.project_id)
    if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
        raise QualificationRunError("cancel E2E에서 active execution Attempt를 시작하지 못했습니다.")

    deadline = time.monotonic() + timeout_seconds
    binding: ThreadBinding | None = None
    job_id: str | None = None
    while time.monotonic() < deadline:
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.binding_json,j.id AS job_id,j.status AS job_status "
                "FROM attempts a JOIN runtime_jobs j ON j.attempt_id=a.id "
                "WHERE a.id=? ORDER BY j.rowid DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
        if row is not None and row["binding_json"] is not None:
            candidate = ThreadBinding.model_validate_json(row["binding_json"])
            if candidate.turn_id is not None and row["job_status"] in {
                "running",
                "interrupting",
                "collector_lost",
            }:
                binding = candidate
                job_id = row["job_id"]
                break
        time.sleep(0.05)
    if binding is None or binding.turn_id is None or job_id is None:
        raise QualificationRunError("cancel E2E의 active exact turn binding을 확인하지 못했습니다.")

    pre_cancel_observation = _read_active_exact_turn(
        runtime,
        binding,
        scenario="cancel",
    )

    def ledger_effect_counts() -> dict[str, int]:
        with prepared.service.ledger.read() as connection:
            provider_calls = connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE attempt_id=?",
                (dispatched.attempt_id,),
            ).fetchone()[0]
            intents = connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id=?",
                (dispatched.attempt_id,),
            ).fetchone()[0]
            receipts = connection.execute(
                "SELECT COUNT(*),COUNT(DISTINCT provider_operation_id) "
                "FROM runtime_receipts WHERE intent_id IN "
                "(SELECT id FROM runtime_intents WHERE attempt_id=?)",
                (dispatched.attempt_id,),
            ).fetchone()
        return {
            "provider_calls": provider_calls,
            "runtime_intents": intents,
            "runtime_receipts": receipts[0],
            "distinct_provider_operation_ids": receipts[1],
        }

    provider_effects_before = (
        runtime.create_calls,
        runtime.turn_calls,
        runtime.resume_calls,
    )
    ledger_effects_before = ledger_effect_counts()
    cancelled = application.cancel(prepared.project_id, reason="E2E-16 active job cancel")
    blocked = application.run_once(prepared.project_id, resume=True)

    deadline = time.monotonic() + timeout_seconds
    observed: dict[str, Any] | None = None
    attempt_status: str | None = None
    failure_class: str | None = None
    while time.monotonic() < deadline:
        observed = application.observe(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT status,failure_class FROM attempts WHERE id=?",
                (dispatched.attempt_id,),
            ).fetchone()
        attempt_status = None if attempt is None else attempt["status"]
        failure_class = None if attempt is None else attempt["failure_class"]
        if attempt_status in {"interrupted", "unknown"}:
            break
        time.sleep(0.25)

    blocked_after_observe = application.run_once(prepared.project_id, resume=True)
    provider_effects_after = (
        runtime.create_calls,
        runtime.turn_calls,
        runtime.resume_calls,
    )
    ledger_effects_after = ledger_effect_counts()
    provider_effect_delta = tuple(
        after - before
        for before, after in zip(provider_effects_before, provider_effects_after, strict=True)
    )
    with prepared.service.ledger.read() as connection:
        job = connection.execute(
            "SELECT status,thread_id,turn_id,provider_terminal_status,result_digest "
            "FROM runtime_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        terminal = connection.execute(
            "SELECT terminal_status,payload_digest FROM runtime_job_observations "
            "WHERE job_id=? AND provider_terminal=1 ORDER BY rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT id,source_ref,content_digest FROM evidence_records "
            "WHERE attempt_id=? AND source_ref LIKE 'runtime-cancelled:%' "
            "ORDER BY rowid DESC LIMIT 1",
            (dispatched.attempt_id,),
        ).fetchone()
        provider_call = connection.execute(
            "SELECT status,usage_id,execution_status,new_turn_count FROM provider_calls WHERE attempt_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (dispatched.attempt_id,),
        ).fetchone()
        interrupts = connection.execute(
            "SELECT kind,payload_json FROM runtime_job_observations "
            "WHERE job_id=? AND kind IN ('interrupt_requested','interrupt_receipt') "
            "ORDER BY rowid",
            (job_id,),
        ).fetchall()
        control_history = connection.execute(
            "SELECT event_type,payload_json FROM history_events WHERE project_id=? "
            "AND event_type='workflow.cancelled' ORDER BY sequence DESC LIMIT 1",
            (prepared.project_id,),
        ).fetchone()
    exact_binding_preserved = bool(
        job is not None
        and job["thread_id"] == binding.thread_id
        and job["turn_id"] == binding.turn_id
    )
    passed = all(
        (
            cancelled["control_state"] == "cancelled",
            blocked.action is RunOnceAction.BLOCKED,
            blocked.blocker_code == "WORKFLOW_CANCELLED",
            blocked_after_observe.action is RunOnceAction.BLOCKED,
            blocked_after_observe.blocker_code == "WORKFLOW_CANCELLED",
            attempt_status == "interrupted",
            failure_class is None,
            job is not None and job["status"] == "consumed",
            terminal is not None,
            evidence is not None,
            exact_binding_preserved,
            provider_effect_delta == (0, 0, 0),
            ledger_effects_after == ledger_effects_before,
            provider_call is not None
            and provider_call["execution_status"] == "terminal"
            and provider_call["new_turn_count"] == 1,
            {row["kind"] for row in interrupts}
            == {"interrupt_requested", "interrupt_receipt"},
            control_history is not None,
        )
    )
    return {
        "passed": passed,
        "source_fixture_digest": source_digest,
        "runtime_job": None if job is None else dict(job),
        "control_state": cancelled["control_state"],
        "run_once_blocker": blocked.blocker_code,
        "post_observe_run_once_blocker": blocked_after_observe.blocker_code,
        "pre_cancel_observation": pre_cancel_observation.model_dump(mode="json"),
        "runtime_observation": None if terminal is None else dict(terminal),
        "ledger": {
            "attempt_id": dispatched.attempt_id,
            "attempt_status": attempt_status,
            "failure_class": failure_class,
            "evidence": None if evidence is None else dict(evidence),
            "provider_call": None if provider_call is None else dict(provider_call),
            "effect_counts_before": ledger_effects_before,
            "effect_counts_after": ledger_effects_after,
            "interrupt_observations": [
                {"kind": row["kind"], "payload": json.loads(row["payload_json"])}
                for row in interrupts
            ],
        },
        "control_history": None if control_history is None else {
            "event_type": control_history["event_type"],
            "payload": json.loads(control_history["payload_json"]),
        },
        "exact_binding_preserved": exact_binding_preserved,
        "effect_count": {
            "create_thread": provider_effect_delta[0],
            "start_turn": provider_effect_delta[1],
            "resume": provider_effect_delta[2],
        },
        "last_observe": observed,
    }


def _await_active_execution_binding(
    prepared: PreparedE2E,
    attempt_id: str,
    *,
    timeout_seconds: float,
) -> tuple[ThreadBinding, str]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.binding_json,j.id AS job_id,j.status AS job_status "
                "FROM attempts a JOIN runtime_jobs j ON j.attempt_id=a.id "
                "WHERE a.id=? ORDER BY j.rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        if row is not None and row["binding_json"] is not None:
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            if binding.turn_id is not None and row["job_status"] in {
                "running",
                "interrupting",
                "collector_lost",
                "provider_terminal",
            }:
                return binding, row["job_id"]
        time.sleep(0.05)
    raise QualificationRunError("E2E fault 주입 전 active exact turn binding을 확인하지 못했습니다.")


def _read_active_exact_turn(
    runtime: RecordedRuntime,
    binding: ThreadBinding,
    *,
    scenario: str,
) -> Any:
    """owner connection의 live 상태로 exact turn이 아직 실행 중인지 확인한다."""

    observation = runtime.read(thread_id=binding.thread_id)
    if (
        observation.thread_id != binding.thread_id
        or observation.turn_id != binding.turn_id
        or not observation.active
    ):
        raise QualificationRunError(
            f"{scenario} fault 주입 전에 active exact provider turn을 관측하지 못했습니다."
        )
    return observation


def _attempt_effect_counts(prepared: PreparedE2E, attempt_id: str) -> dict[str, int]:
    with prepared.service.ledger.read() as connection:
        receipts = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT provider_operation_id) "
            "FROM runtime_receipts WHERE intent_id IN "
            "(SELECT id FROM runtime_intents WHERE attempt_id=?)",
            (attempt_id,),
        ).fetchone()
        return {
            "provider_calls": connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE attempt_id=?", (attempt_id,)
            ).fetchone()[0],
            "runtime_intents": connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id=?", (attempt_id,)
            ).fetchone()[0],
            "runtime_receipts": receipts[0],
            "distinct_provider_operation_ids": receipts[1],
        }


def _journal_operation_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    operations = ("create_thread", "start_turn", "resume", "interrupt", "read", "read_stored")
    return {
        operation: sum(item.get("operation") == operation for item in events)
        for operation in operations
    }


def _write_fault_injection(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    document = {
        "schema": "flowmarshal.project-e2e.fault-injection.v1",
        **payload,
    }
    document["fault_digest"] = sha256_digest(document)
    _write_json(path, document)
    return document


def _forced_termination_no_duplicate(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """collector를 끊고 새 supervisor가 exact turn을 resume 없이 재관측한다."""

    first_supervisor = RuntimeJobSupervisor(
        prepared.service, runtime, observation_timeout_seconds=5.0
    )
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=first_supervisor,
    )
    restarted_application: EngineApplication | None = None
    try:
        _materialize_with_application(application, prepared, timeout_seconds=timeout_seconds)
        dispatched = application.run_once(prepared.project_id)
        if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
            raise QualificationRunError("forced-termination E2E에서 execution Attempt를 시작하지 못했습니다.")
        binding, job_id = _await_active_execution_binding(
            prepared, dispatched.attempt_id, timeout_seconds=timeout_seconds
        )
        pre_fault = _read_active_exact_turn(
            runtime,
            binding,
            scenario="forced-termination",
        )

        effect_counts_before = _attempt_effect_counts(prepared, dispatched.attempt_id)
        journal_offset = len(runtime.events)
        runtime.sever_completion_forwarding()
        fault = _write_fault_injection(
            runtime.journal.parent / "fault-injection.json",
            {
                "kind": "in_process_collector_termination",
                "injected_at": utc_now().isoformat(),
                "job_id": job_id,
                "attempt_id": dispatched.attempt_id,
                "thread_id": binding.thread_id,
                "turn_id": binding.turn_id,
            },
        )
        lost = first_supervisor.mark_collector_lost(
            job_id, reason="E2E-09 injected collector termination"
        )

        restarted_supervisor = RuntimeJobSupervisor(
            prepared.service, runtime, observation_timeout_seconds=5.0
        )
        restarted_application = EngineApplication(
            prepared.service,
            runtime=runtime,
            role_configuration=roles,
            governance=governance,
            supervisor=restarted_supervisor,
        )
        observed = restarted_application.observe(prepared.project_id)
        effect_counts_after = _attempt_effect_counts(prepared, dispatched.attempt_id)
        restart_counts = _journal_operation_counts(runtime.events[journal_offset:])

        with prepared.service.ledger.read() as connection:
            job = connection.execute(
                "SELECT status,thread_id,turn_id,provider_terminal_status,result_digest "
                "FROM runtime_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            observations = connection.execute(
                "SELECT kind,provider_terminal,terminal_status,payload_digest "
                "FROM runtime_job_observations WHERE job_id=? ORDER BY rowid",
                (job_id,),
            ).fetchall()
            provider_call = connection.execute(
                "SELECT status,execution_status,new_turn_count FROM provider_calls "
                "WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status,failure_class FROM attempts WHERE id=?",
                (dispatched.attempt_id,),
            ).fetchone()

        observation_kinds = [row["kind"] for row in observations]
        exact_binding_preserved = bool(
            job is not None
            and job["thread_id"] == binding.thread_id
            and job["turn_id"] == binding.turn_id
        )
        passed = all(
            (
                lost.status.value == "collector_lost",
                job is not None and job["status"] == "running",
                attempt is not None and attempt["status"] == "running",
                exact_binding_preserved,
                "collector_lost" in observation_kinds,
                "collector_reattached" in observation_kinds,
                "provider_progress" in observation_kinds,
                not any(row["provider_terminal"] for row in observations),
                restart_counts["read_stored"] >= 1,
                restart_counts["create_thread"] == 0,
                restart_counts["start_turn"] == 0,
                restart_counts["resume"] == 0,
                restart_counts["interrupt"] == 0,
                effect_counts_after == effect_counts_before,
                provider_call is not None,
            )
        )
        return {
            "passed": passed,
            "source_fixture_digest": source_digest,
            "binding": binding.model_dump(mode="json"),
            "fault_injection": fault,
            "runtime_job": None if job is None else dict(job),
            "runtime_observation": [dict(row) for row in observations],
            "restart_journal": runtime.events[journal_offset:],
            "exact_binding_preserved": exact_binding_preserved,
            "effect_count": restart_counts,
            "ledger": {
                "attempt_id": dispatched.attempt_id,
                "attempt_status": None if attempt is None else attempt["status"],
                "failure_class": None if attempt is None else attempt["failure_class"],
                "effect_counts_before": effect_counts_before,
                "effect_counts_after": effect_counts_after,
                "provider_call": None if provider_call is None else dict(provider_call),
                "provider_call_counter_semantics": "new_turn_count is settled turns, not started turns",
            },
            "last_observe": observed,
        }
    finally:
        application.close_task_gate()
        if restarted_application is not None:
            restarted_application.close_task_gate()


def _absolute_timeout_no_duplicate(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """active exact turn의 job deadline을 만료시켜 중단·재관측·중복 차단을 검증한다."""

    supervisor = RuntimeJobSupervisor(
        prepared.service, runtime, observation_timeout_seconds=5.0
    )
    fault_path = runtime.journal.parent / "fault-injection.json"
    provider = _execution_proposal_provider(prepared, runtime, roles)
    task_gate = _open_e2e_task_gate(prepared, runtime, roles, governance)
    dispatcher = EngineDispatcher(
        prepared.service,
        runtime,
        proposal_provider=provider,
        supervisor=supervisor,
        task_gate=task_gate,
    )
    if prepared.proposal is None and provider is None:
        raise QualificationRunError("absolute-timeout E2E에는 execution proposal provider가 필요합니다.")

    try:
        outcome = dispatcher.run_once(
            prepared.project_id, proposal=prepared.proposal
        )
        materialize_deadline = time.monotonic() + timeout_seconds
        while (
            outcome.action is not RunOnceAction.MATERIALIZED
            and time.monotonic() < materialize_deadline
        ):
            if outcome.action is RunOnceAction.BLOCKED:
                raise QualificationRunError(f"{outcome.blocker_code}: {outcome.detail}")
            time.sleep(0.01)
            outcome = dispatcher.run_once(prepared.project_id)
        if outcome.action is not RunOnceAction.MATERIALIZED:
            raise QualificationRunError("absolute-timeout E2E execution spec materialize timeout")

        dispatched = dispatcher.run_once(prepared.project_id)
        if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
            raise QualificationRunError("absolute-timeout E2E에서 execution Attempt를 시작하지 못했습니다.")
        binding, job_id = _await_active_execution_binding(
            prepared, dispatched.attempt_id, timeout_seconds=timeout_seconds
        )
        pre_fault = _read_active_exact_turn(
            runtime,
            binding,
            scenario="absolute-timeout",
        )
        bound_job = prepared.service.load_runtime_job(job_id)
        if (
            bound_job.status.value != "running"
            or bound_job.thread_id != binding.thread_id
            or bound_job.turn_id != binding.turn_id
        ):
            raise QualificationRunError(
                "absolute-timeout fault 주입 대상이 active exact turn과 다릅니다."
            )
        observed_deadline_at = bound_job.absolute_deadline_at
        deadline_supervisor = RuntimeJobSupervisor(
            prepared.service,
            runtime,
            observation_timeout_seconds=5.0,
            clock=lambda: observed_deadline_at,
        )
        deadline_dispatcher = EngineDispatcher(
            prepared.service,
            runtime,
            proposal_provider=provider,
            supervisor=deadline_supervisor,
            task_gate=task_gate,
        )
        fault = _write_fault_injection(
            fault_path,
            {
                "kind": "runtime_job_clock_advanced_to_deadline_after_binding",
                "injected_at": utc_now().isoformat(),
                "job_id": job_id,
                "attempt_id": dispatched.attempt_id,
                "thread_id": binding.thread_id,
                "turn_id": binding.turn_id,
                "absolute_deadline_at": observed_deadline_at.isoformat(),
                "injected_clock_at": observed_deadline_at.isoformat(),
                "clock_provenance": "fault_injected",
                "ledger_observed_at_may_precede_injected_clock": True,
                "pre_fault_observation_digest": sha256_digest(pre_fault),
            },
        )

        effect_counts_before = _attempt_effect_counts(prepared, dispatched.attempt_id)
        journal_offset = len(runtime.events)
        attempt_status = None
        failure_class = None
        last_outcome = outcome
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            last_outcome = deadline_dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                attempt = connection.execute(
                    "SELECT status,failure_class FROM attempts WHERE id=?",
                    (dispatched.attempt_id,),
                ).fetchone()
            attempt_status = None if attempt is None else attempt["status"]
            failure_class = None if attempt is None else attempt["failure_class"]
            if attempt_status == "failed":
                break
            time.sleep(0.05)
        blocked = deadline_dispatcher.run_once(prepared.project_id)
        effect_counts_after = _attempt_effect_counts(prepared, dispatched.attempt_id)
        timeout_counts = _journal_operation_counts(runtime.events[journal_offset:])

        with prepared.service.ledger.read() as connection:
            job = connection.execute(
                "SELECT status,thread_id,turn_id,provider_terminal_status,result_digest,"
                "absolute_deadline_at FROM runtime_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            terminal = connection.execute(
                "SELECT terminal_status,payload_digest FROM runtime_job_observations "
                "WHERE job_id=? AND provider_terminal=1 ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            evidence = connection.execute(
                "SELECT id,source_ref,content_digest,observation FROM evidence_records "
                "WHERE attempt_id=? AND source_ref LIKE 'runtime-deadline:%' "
                "ORDER BY rowid DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
            provider_call = connection.execute(
                "SELECT status,execution_status,new_turn_count FROM provider_calls "
                "WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
            interrupts = connection.execute(
                "SELECT kind,payload_json FROM runtime_job_observations "
                "WHERE job_id=? AND kind IN ('interrupt_requested','interrupt_receipt') "
                "ORDER BY rowid",
                (job_id,),
            ).fetchall()

        evidence_observation = (
            None if evidence is None else json.loads(evidence["observation"])
        )
        exact_binding_preserved = bool(
            job is not None
            and job["thread_id"] == binding.thread_id
            and job["turn_id"] == binding.turn_id
        )
        local_code = (
            None
            if evidence_observation is None
            else evidence_observation.get("failure_diagnosis", {}).get("local_engine_code")
        )
        passed = all(
            (
                attempt_status == "failed",
                failure_class == "environment",
                job is not None and job["status"] == "consumed",
                terminal is not None and terminal["terminal_status"] == "interrupted",
                evidence is not None,
                local_code == "ABSOLUTE_DEADLINE_EXCEEDED",
                blocked.action is RunOnceAction.BLOCKED,
                blocked.blocker_code == "ENVIRONMENT_RECOVERY_REQUIRED",
                exact_binding_preserved,
                timeout_counts["create_thread"] == 0,
                timeout_counts["start_turn"] == 0,
                timeout_counts["resume"] == 0,
                timeout_counts["interrupt"] == 1,
                effect_counts_after == effect_counts_before,
                provider_call is not None
                and provider_call["execution_status"] == "terminal"
                and provider_call["new_turn_count"] == 1,
                [row["kind"] for row in interrupts]
                == ["interrupt_requested", "interrupt_receipt"],
            )
        )
        return {
            "passed": passed,
            "source_fixture_digest": source_digest,
            "runtime_job": None if job is None else dict(job),
            "deadline": fault,
            "runtime_observation": None if terminal is None else dict(terminal),
            "binding": binding.model_dump(mode="json"),
            "exact_binding_preserved": exact_binding_preserved,
            "run_once_blocker": blocked.blocker_code,
            "last_action": last_outcome.action.value,
            "effect_count": timeout_counts,
            "ledger": {
                "attempt_id": dispatched.attempt_id,
                "attempt_status": attempt_status,
                "failure_class": failure_class,
                "evidence": None if evidence is None else {
                    "id": evidence["id"],
                    "source_ref": evidence["source_ref"],
                    "content_digest": evidence["content_digest"],
                    "observation": evidence_observation,
                },
                "provider_call": None if provider_call is None else dict(provider_call),
                "effect_counts_before": effect_counts_before,
                "effect_counts_after": effect_counts_after,
                "interrupt_observations": [
                    {"kind": row["kind"], "payload": json.loads(row["payload_json"])}
                    for row in interrupts
                ],
            },
        }
    finally:
        task_gate.close()


def _responsibility_outcome(
    *,
    scenario: str,
    prepared: PreparedE2E,
    cell: dict[str, Any],
    contract: EvaluationContract,
    cell_root: Path,
    run_root: Path,
    fixture_digest: str,
    freeze_bundle_digest: str,
    governance_plugin_identity_digest: str,
    candidate_wheel_digest: str,
    candidate_wheel_binding_digest: str,
    candidate_distribution_name: str,
    candidate_distribution_version: str,
) -> QualificationCellOutcome:
    """각 실행 cell이 실제로 증명한 책임만 명시적으로 투영한다."""

    mapping = {
        "normal-completion": (
            "E2E-01",
            (),
            ("raw_request", "role_receipt", "ledger", "activation_receipt"),
            (EvidenceProvenance.LIVE,),
        ),
        "stale-after-materialization": (
            "E2E-11",
            ("materialize", "freshness_check"),
            ("execution_spec", "source_digest", "error", "effect_count"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        "stored-turn-restart-resume": (
            "E2E-07",
            (
                "task_execution",
                "restart",
                "observe_existing",
                "resume",
                "independent_validation",
                "goal_verdict",
            ),
            ("runtime_receipt", "binding", "restart_journal", "validation"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        "unknown-receipt-no-duplicate": (
            "E2E-08",
            ("task_execution", "fault", "observe_existing"),
            ("intent", "runtime_receipt", "operation_journal", "effect_count"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        "forced-termination-no-duplicate": (
            "E2E-09",
            ("task_execution", "forced_termination", "observe_existing"),
            ("binding", "restart_journal", "runtime_observation", "effect_count"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        "absolute-timeout-no-duplicate": (
            "E2E-10",
            ("task_execution", "timeout", "observe_existing"),
            ("runtime_job", "deadline", "runtime_observation", "effect_count"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        "cancel-active-job": (
            "E2E-16",
            ("task_execution", "cancel", "observe_existing"),
            ("runtime_job", "control_state", "runtime_observation", "ledger"),
            (EvidenceProvenance.LIVE,),
        ),
    }
    responsibility_id, suffix, evidence_kinds, provenance = mapping[scenario]
    artifact_by_kind = {
        "raw_request": cell_root / "raw-request.json",
        "role_receipt": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "ledger": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "activation_receipt": cell_root / "plan-activation-receipt.json",
        "execution_spec": cell_root / "qualification-observation.json",
        "source_digest": cell_root / "qualification-observation.json",
        "error": cell_root / "qualification-observation.json",
        "effect_count": cell_root / "qualification-observation.json",
        "runtime_receipt": cell_root / "runtime-receipts.json",
        "binding": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "restart_journal": cell_root / "runtime-receipts.json",
        "validation": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "intent": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "operation_journal": cell_root / "runtime-receipts.json",
        "runtime_job": cell_root / "qualification-observation.json",
        "deadline": cell_root / "qualification-observation.json",
        "control_state": cell_root / "qualification-observation.json",
        "runtime_observation": cell_root / "runtime-receipts.json",
    }
    evidence_records = tuple(
        build_qualification_evidence_record(
            run_root=run_root,
            path=artifact_by_kind[kind],
            kind=kind,
            cell_id=scenario,
            evaluation_contract_digest=contract.contract_digest,
            fixture_digest=fixture_digest,
            order_seed=0,
            freeze_bundle_digest=freeze_bundle_digest,
            governance_plugin_identity_digest=governance_plugin_identity_digest,
            candidate_wheel_digest=candidate_wheel_digest,
            candidate_wheel_binding_digest=candidate_wheel_binding_digest,
            candidate_distribution_name=candidate_distribution_name,
            candidate_distribution_version=candidate_distribution_version,
        )
        for kind in evidence_kinds
    )
    evidence_refs = tuple(item.relative_path for item in evidence_records)
    if cell.get("passed"):
        return QualificationCellOutcome(
            cell_id=scenario,
            evaluation_contract_digest=contract.contract_digest,
            responsibility_ids=(responsibility_id,),
            status=QualificationCellStatus.PASSED,
            provenance=provenance,
            pipeline_stages=prepared.pipeline_stages + suffix,
            evidence_kinds=evidence_kinds,
            evidence_refs=evidence_refs,
            evidence_records=evidence_records,
        )
    return QualificationCellOutcome(
        cell_id=scenario,
        evaluation_contract_digest=contract.contract_digest,
        responsibility_ids=(responsibility_id,),
        status=QualificationCellStatus.FAILED,
        provenance=provenance,
        pipeline_stages=prepared.pipeline_stages + suffix,
        evidence_kinds=evidence_kinds,
        evidence_refs=evidence_refs,
        evidence_records=evidence_records,
        failure_class=QualificationFailureClass.PRODUCT,
        failure_code=str(cell.get("failure") or cell.get("error") or "E2E_FAILED")[:500],
    )


def _contract(
    root: Path,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    source_digest: str,
    policies: EvaluationPolicies | None = None,
    candidate_wheel_binding_digest: str | None = None,
) -> EvaluationContract:
    import inspect
    from .execution import ProviderExecutionPreparation, GoalTestPreparation, EXECUTION_PREPARATION_INSTRUCTIONS, GOAL_TEST_PREPARATION_INSTRUCTIONS
    from .worker_prompt import assemble_worker_prompt, PromptArtifactStore
    from .roles import strict_json_output_schema
    suite = qualification_suite_manifest(root)
    fixture_digests = tuple(
        sha256_digest({"scenario": scenario, "source_fixture_digest": source_digest})
        for scenario in E2E_SCENARIOS
    )
    return EvaluationContract(
        model_lock_format="flowmarshal-model-lock-v2",
        scope=EvaluationScope.PROJECT_E2E,
        fixture_digests=fixture_digests,
        scenario_set_digest=sha256_digest(
            {"scenarios": E2E_SCENARIOS, "source_fixture_digest": source_digest}
        ),
        order_seeds=(0,),
        expected_cell_count=len(E2E_SCENARIOS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest(
            {
                "actual_codex": list(E2E_SCENARIOS),
                "restart": "new-app-server-and-core",
                "raw_request_pipeline": "EngineApplication.prepare+authorize",
                "responsibility_manifest_digest": suite.manifest_digest,
                "candidate_wheel_binding_digest": candidate_wheel_binding_digest,
            }
            | (
                policy_contract_fragment(policies)
                if policies is not None
                else {"evaluation_policy": "synthetic-unbound"}
            )
        ),
        threshold_digest=sha256_digest({"all_scenarios_pass": True, "duplicate_effects": 0}),
        taxonomy_digest=sha256_digest(
            {
                "requirements": [
                    item.model_dump(mode="json")
                    for item in suite.e2e_responsibilities
                ]
            }
        ),
        prompt_digest=sha256_digest({"task": inspect.getsource(assemble_worker_prompt),
                                    "artifact": inspect.getsource(PromptArtifactStore),
                                    "turn": inspect.getsource(EngineDispatcher._start_turn),
                                    "preparation": EXECUTION_PREPARATION_INSTRUCTIONS,
                                    "goal_preparation": GOAL_TEST_PREPARATION_INSTRUCTIONS,
                                    "validator": inspect.getsource(EngineDispatcher._role_for_attempt),
                                    "fixture_contract": inspect.getsource(_prepare_from_raw_request),
                                    "responsibility_evaluator": inspect.getsource(evaluate_qualification_responsibilities)}),
        output_schema_digest=sha256_digest({
            "preparation": strict_json_output_schema(ProviderExecutionPreparation.model_json_schema()),
            "goal": strict_json_output_schema(GoalTestPreparation.model_json_schema()),
            "semantic": EngineDispatcher._semantic_schema(),
        }),
        model_lock_digest=_model_lock(inventory, roles),
    )


def _bind_project_e2e_candidate_wheel(
    *, run_root: Path | None, candidate_wheel: Path | str | None
) -> CandidateWheelBinding:
    """release project E2E가 요구하는 candidate wheel 결속만 수행한다.

    실제 실행과 pre-provider dry invocation이 같은 결속을 쓰도록 분리했다.
    """

    if candidate_wheel is None:
        raise QualificationRunError("CANDIDATE_WHEEL_REQUIRED")
    try:
        candidate_binding = verify_candidate_wheel_installation(candidate_wheel)
    except Exception as error:
        raise QualificationRunError(str(error)) from error
    if run_root is not None:
        metadata_path = Path(run_root).resolve() / "run-metadata.json"
        if metadata_path.is_file():
            try:
                saved_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
                verify_metadata_digest(saved_metadata)
                saved_binding = verify_candidate_wheel_metadata(saved_metadata)
            except Exception as error:
                raise QualificationRunError(str(error)) from error
            if saved_binding != candidate_binding:
                raise QualificationRunError("CANDIDATE_WHEEL_BINDING_CHANGED")
    return candidate_binding


def _project_e2e_fixture_source_digest(base: Path) -> str:
    fixture_source = base / "tests" / "fixtures" / "engine" / "project-e2e"
    return sha256_digest(
        {
            path.relative_to(fixture_source).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(fixture_source.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
    )


def dry_run_project_e2e_pre_provider(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    candidate_wheel: Path | str | None = None,
) -> dict[str, Any]:
    """provider 연결 전까지의 결속만 실제 실행 경로로 확인하는 dry 관측이다.

    provider turn, evaluation 계약, checkpoint와 cell 실행은 하지 않는다. 이
    결과는 명시적 fake 경계이며 어떤 책임의 release PASS도 만들지 않는다.
    """

    base = (root or project_root()).resolve(strict=True)
    resolved_run_root = None if run_root is None else Path(run_root).resolve()
    if resolved_run_root is not None:
        _guard_e2e_partial_resume(resolved_run_root)
    candidate_binding = _bind_project_e2e_candidate_wheel(
        run_root=resolved_run_root, candidate_wheel=candidate_wheel
    )
    roles = role_configuration or default_role_configuration(base)
    suite = qualification_suite_manifest(base)
    return {
        "mode": "pre_provider_dry_run",
        "release_pass": False,
        "provenance": EvidenceProvenance.FAKE.value,
        "project_root": str(base),
        "scenarios": list(E2E_SCENARIOS),
        "suite_manifest_digest": suite.manifest_digest,
        "source_manifest_digest": source_manifest_digest(base),
        "role_configuration_digest": roles.configuration_digest,
        "project_e2e_fixture_source_digest": _project_e2e_fixture_source_digest(base),
        "candidate_wheel_binding": candidate_binding.model_dump(mode="json"),
        "candidate_wheel_binding_digest": candidate_binding.binding_digest,
        "stages_not_run": [
            "deterministic_preflight",
            "provider_model_list",
            "evaluation_contract",
            "checkpoint_store",
            "cell_execution",
        ],
    }


def run_project_e2e(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    codex_bin: Path | str | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
    candidate_wheel: Path | str | None = None,
    runtime_selection: RuntimeProviderSelection | None = None,
    governance: Any | None = None,
    release_freeze: Path | str | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    """release project E2E. 실행 Task는 제품 경로와 같은 필수 governance gate를 지난다."""
    if evaluation_policies is None:
        raise QualificationRunError("EVALUATION_POLICY_REQUIRED")
    base = (root or project_root()).resolve(strict=True)
    if run_root is not None:
        _guard_e2e_partial_resume(Path(run_root).resolve())
    candidate_binding = _bind_project_e2e_candidate_wheel(
        run_root=None if run_root is None else Path(run_root).resolve(),
        candidate_wheel=candidate_wheel,
    )
    if release_freeze is None:
        raise QualificationRunError("E2E_RELEASE_FREEZE_REQUIRED")
    freeze_path = Path(release_freeze)
    if not freeze_path.is_absolute():
        raise QualificationRunError("E2E_RELEASE_FREEZE_ABSOLUTE_PATH_REQUIRED")
    roles = role_configuration or default_role_configuration(base)
    try:
        from .release_freeze import verify_release_freeze

        release_verification = verify_release_freeze(
            freeze_path,
            source_root=base,
            candidate_wheel=candidate_binding.wheel_path,
            role_configuration=roles,
            evaluation_policies=evaluation_policies,
        )
    except Exception as error:
        raise QualificationRunError(f"E2E_RELEASE_FREEZE_INVALID:{error}") from error
    if not release_verification.valid:
        raise QualificationRunError(
            "E2E_RELEASE_FREEZE_INVALID:" + ";".join(release_verification.mismatches)
        )
    governance_freeze = release_verification.manifest.governance_plugin
    preflight_failures = _preflight(base)
    if preflight_failures:
        raise QualificationRunError("; ".join(preflight_failures))
    source_digest = _project_e2e_fixture_source_digest(base)
    with open_harness_runtime(
        runtime_selection, codex_factory=CodexAppServerRuntime, codex_bin=codex_bin,
        project_binding=evaluation_policies.codex_project,
        default_state_root=base / ".flowmarshal-engine-eval" / "claude-threads",
    ) as real_runtime:
        inventory = real_runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _contract(
            base,
            inventory,
            roles,
            source_digest,
            evaluation_policies,
            candidate_binding.binding_digest,
        )
        destination = (
            run_root or _default_run_root(base, "project-e2e", contract.contract_digest[7:15])
        ).resolve()
        if governance is None:
            from .governance_gate import GovernanceSettings

            governance = GovernanceSettings.from_environment(destination / "governance")
        if governance is None:
            raise QualificationRunError("GOVERNANCE_GATE_REQUIRED")
        governance_observation = _observe_frozen_plugin_identity(governance, governance_freeze)
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        freeze_manifest = build_qualification_reproduction_bundle(
            root=base,
            destination=destination / "reproduction-bundle",
            inventory=inventory,
            roles=roles,
            contracts=(contract,),
        )
        audit_path = destination / (
            "inventory-observation-" + inventory.inventory_digest[7:] + ".json"
        )
        _preserve_inventory_observation(audit_path, roles, inventory)
        prior_state = store.state().status
        if prior_state is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        write_immutable_run_metadata(
            destination / "run-metadata.json",
            {
                "scope": "project-e2e",
                "evaluation_contract_digest": contract.contract_digest,
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
                "candidate_wheel_binding": candidate_binding.model_dump(mode="json"),
                "candidate_wheel_binding_digest": candidate_binding.binding_digest,
                "release_freeze_path": str(freeze_path.resolve(strict=True)),
                "release_freeze_digest": release_verification.manifest.freeze_digest,
                **governance_observation,
                **provider_run_metadata(runtime_selection),
            },
            evaluation_policies,
        )
        active_scenario: str | None = None
        try:
            for index, scenario in enumerate(E2E_SCENARIOS):
                active_scenario = scenario
                digest = contract.fixture_digests[index]
                if store.completed(digest, 0) is not None:
                    continue
                before_identity = _observe_frozen_plugin_identity(governance, governance_freeze)
                cell_root = destination / "work" / scenario
                recorded = RecordedRuntime(
                    real_runtime, journal=cell_root / "runtime-receipts.json"
                )
                if cell_root.exists():
                    if scenario != "normal-completion" or prior_state not in {
                        EvaluationRunStatus.PAUSED_RATE_LIMIT,
                        EvaluationRunStatus.RUNNING,
                    }:
                        raise QualificationRunError(
                            "재개할 수 없는 미완료 E2E 작업 디렉터리가 남았습니다. "
                            "기존 runtime intent, provider terminal과 예산 예약을 먼저 "
                            f"대조하세요: {cell_root}"
                        )
                    prepared = _restore_prepared_state(
                        cell_root,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        governance_plugin_identity_digest=before_identity["governance_plugin_identity_digest"],
                    )
                    if prepared.preparation_provenance is not EvidenceProvenance.LIVE:
                        raise QualificationRunError(
                            "E2E_RAW_REQUEST_PIPELINE_BYPASSED: fake/pre-generated Goal·Plan "
                            "cell은 실제 E2E에서 재사용할 수 없습니다."
                        )
                    verify_service_budget_policy(
                        prepared.service, prepared.project_id, evaluation_policies
                    )
                else:
                    cell_root.mkdir(parents=True)
                    workspace, copied_digest = _copy_fixture(base, cell_root)
                    if copied_digest != source_digest:
                        raise QualificationRunError("복사 직전 E2E fixture digest가 변경됐습니다.")
                    prepared = _prepare_from_raw_request(
                        workspace=workspace,
                        state_root=cell_root / "state",
                        runtime=recorded,
                        roles=roles,
                        evaluation_policies=evaluation_policies,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        source_request=(
                            "app.py의 add 함수가 두 정수의 합을 반환하도록 최소 수정하고 "
                            "공개 함수 계약을 유지하며 test_app.py 검증을 통과시켜줘."
                        ),
                    )
                    _write_prepared_state(
                        cell_root,
                        prepared,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        governance_plugin_identity_digest=before_identity["governance_plugin_identity_digest"],
                    )
                if scenario == "stored-turn-restart-resume":
                    with use_role_timeout_policy(evaluation_policies.role_timeouts):
                        cell, events = _live_restart_resume(
                            prepared,
                            source_digest,
                            cell_root=cell_root,
                            contract=contract,
                            fixture_digest=digest,
                            governance_plugin_identity_digest=before_identity["governance_plugin_identity_digest"],
                            codex_bin=codex_bin,
                            roles=roles,
                            project_binding=evaluation_policies.codex_project,
                            runtime_selection=runtime_selection,
                            claude_state_root=base / ".flowmarshal-engine-eval" / "claude-threads",
                            governance=governance,
                        )
                else:
                    with use_role_timeout_policy(evaluation_policies.role_timeouts):
                        if scenario == "normal-completion":
                            cell = _normal_completion(
                                prepared, recorded, source_digest, roles=roles, governance=governance
                            )
                        elif scenario == "stale-after-materialization":
                            cell = _stale_after_materialization(
                                prepared, recorded, source_digest, roles=roles, governance=governance
                            )
                        elif scenario == "unknown-receipt-no-duplicate":
                            cell = _unknown_receipt(
                                prepared, recorded, source_digest, roles=roles, governance=governance
                            )
                        elif scenario == "forced-termination-no-duplicate":
                            cell = _forced_termination_no_duplicate(
                                prepared,
                                recorded,
                                source_digest,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "absolute-timeout-no-duplicate":
                            cell = _absolute_timeout_no_duplicate(
                                prepared,
                                recorded,
                                source_digest,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "cancel-active-job":
                            cell = _cancel_active_job(
                                prepared,
                                recorded,
                                source_digest,
                                roles=roles,
                                governance=governance,
                            )
                        else:
                            raise QualificationRunError(f"알 수 없는 E2E scenario: {scenario}")
                    events = recorded.events
                after_identity = _observe_frozen_plugin_identity(governance, governance_freeze)
                if after_identity["governance_plugin_identity_digest"] != before_identity[
                    "governance_plugin_identity_digest"
                ]:
                    raise QualificationRunError("E2E_GOVERNANCE_PLUGIN_IDENTITY_CHANGED_DURING_CELL")
                _assert_no_transient_plugin_identity_change(prepared)
                model_observation = _checkpoint_model_observation(events)
                receipt = {
                    "runtime": type(real_runtime).__name__,
                    "actual_model": model_observation["actual_model"],
                    "actual_effort": model_observation["actual_effort"],
                    "model_observation": model_observation,
                    "configured_executor_model": roles.executor.model,
                    "configured_executor_effort": roles.executor.effort,
                    "events": events,
                    "events_digest": sha256_digest(events),
                }
                generated_plan = cell_root / "generated-plan.json"
                activation_receipt = cell_root / "plan-activation-receipt.json"
                _write_json(
                    cell_root / "qualification-observation.json",
                    {
                        "schema": "flowmarshal.project-e2e.qualification-observation.v1",
                        "evaluation_contract_digest": contract.contract_digest,
                        "fixture_digest": digest,
                        "cell_id": scenario,
                        "order_seed": 0,
                        "observation": cell,
                    },
                )
                responsibility_outcome = _responsibility_outcome(
                    scenario=scenario,
                    prepared=prepared,
                    cell=cell,
                    contract=contract,
                    cell_root=cell_root,
                    run_root=destination,
                    fixture_digest=digest,
                    freeze_bundle_digest=freeze_manifest.bundle_digest,
                    governance_plugin_identity_digest=before_identity["governance_plugin_identity_digest"],
                    candidate_wheel_digest=candidate_binding.wheel_digest,
                    candidate_wheel_binding_digest=candidate_binding.binding_digest,
                    candidate_distribution_name=candidate_binding.distribution_name,
                    candidate_distribution_version=candidate_binding.distribution_version,
                )
                cell.update({
                    "scenario": scenario,
                    "order_seed": 0,
                    "qualification_plan_activation": {
                        "generated_plan_digest": sha256_bytes(generated_plan.read_bytes()),
                        "activation_receipt_digest": sha256_bytes(activation_receipt.read_bytes()),
                    },
                    "qualification_outcome": responsibility_outcome.model_dump(
                        mode="json"
                    ),
                })
                store.put(
                    EvaluationCellCheckpoint(
                        model_lock_format="flowmarshal-model-lock-v2",
                        contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        order_seed=0,
                        raw_structured_assessment=cell,
                        runner_receipts=(receipt,),
                    )
                )
        except Exception as error:
            provider_calls: tuple[tuple[str, str], ...] | None = None
            if active_scenario is not None:
                try:
                    provider_calls = evaluation_cell_provider_calls(
                        destination / "work" / active_scenario / "state"
                    )
                except (OSError, ValueError):
                    # 원장을 신뢰성 있게 읽지 못한 경우 효과 전 실패로 추정하지 않는다.
                    provider_calls = None
            status, resumable, next_action = _e2e_failure_disposition(
                active_scenario,
                error,
                provider_calls=provider_calls,
            )
            _write_json(
                destination / "last-error.json",
                {
                    "scenario": active_scenario,
                    "error": type(error).__name__,
                    "message": str(error),
                    "failure_class": classify_qualification_failure(error).value,
                    "run_status": status.value,
                    "resumable": resumable,
                    "next_action": next_action,
                },
            )
            store.set_state(
                status,
                updated_at=utc_now(),
                reason=f"{error}; next_action={next_action}",
            )
            raise
        cells = [
            store.completed(digest, 0).raw_structured_assessment  # type: ignore[union-attr]
            for digest in contract.fixture_digests
        ]
        cell_failures = tuple(
            f"{item['scenario']}: {item.get('failure') or item.get('error') or 'FAIL'}"
            for item in cells
            if not bool(item.get("passed"))
        )
        suite = qualification_suite_manifest(base)
        responsibility_report = evaluate_qualification_responsibilities(
            suite,
            tuple(
                QualificationCellOutcome.model_validate(item["qualification_outcome"])
                for item in cells
            ),
            evaluation_contract_digest=contract.contract_digest,
            run_root=destination,
            expected_cell_bindings={
                scenario: (digest, 0)
                for scenario, digest in zip(
                    E2E_SCENARIOS, contract.fixture_digests, strict=True
                )
            },
            expected_freeze_bundle_digest=freeze_manifest.bundle_digest,
            expected_governance_plugin_identity_digest=governance_observation[
                "governance_plugin_identity_digest"
            ],
            expected_candidate_wheel_digest=candidate_binding.wheel_digest,
            expected_candidate_wheel_binding_digest=candidate_binding.binding_digest,
            expected_candidate_distribution_name=candidate_binding.distribution_name,
            expected_candidate_distribution_version=candidate_binding.distribution_version,
        )
        _write_json(
            destination / "responsibility-qualification-report.json",
            responsibility_report,
        )
        failures = tuple((*cell_failures, *responsibility_report.failures))
        store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
        report = ScopeQualificationReport(
            scope=contract.scope,
            contract_digest=contract.contract_digest,
            status=EvaluationRunStatus.COMPLETED,
            passed=not failures,
            metrics={
                "cell_count": len(cells),
                "passed_cell_count": sum(1 for item in cells if item.get("passed")),
                "actual_codex_cell_count": len(cells),
                "duplicate_effect_count": sum(
                    1
                    for item in cells
                    if item.get("scenario") == "unknown-receipt-no-duplicate"
                    and item.get("thread_create_count") != 1
                ),
                "responsibility_count": responsibility_report.responsibility_count,
                "passed_responsibility_count": (
                    responsibility_report.passed_responsibility_count
                ),
                "not_run_responsibility_count": len(
                    responsibility_report.not_run_responsibility_ids
                ),
            },
            failures=failures,
            generated_at=utc_now(),
        )
        _write_json(destination / "qualification-report.json", report)
        return destination, report
