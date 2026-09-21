from __future__ import annotations

from .capabilities import CoreActionAuthority

from .domain import ModelFallback

import json
import inspect
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    ApproachSignature,
    ApprovalClass,
    BehaviorPolicy,
    CandidateDecision,
    CandidateStatus,
    EffectContract,
    EffectPolicy,
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    GoalAuthorization,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCoverage,
    GoalCriterion,
    GoalOperatingPolicy,
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
    ValidationResult,
    ValidationStatus,
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
from .budget import BudgetManager, GoalBudgetPolicy
from .ledger import SQLiteEngineLedger
from .models import EngineRoleConfiguration, ModelInventory
from .planner_roles import DetailedTaskDraft
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
from .clean_install_qualification import (
    CLEAN_INSTALL_RESPONSIBILITY_ID,
    CleanInstallLinkResult,
    verify_clean_install_link,
)
from .qualification_manifest import (
    CandidateWheelBinding,
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    QualificationSuiteManifest,
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
from .recovery_planning import RecoveryPlanProvider
from .role_execution import use_role_timeout_policy
from .roles import ScriptedStructuredRoleRunner
from .service import (
    EngineService,
    EngineServiceError,
    GoalAuthorizationRequired,
    PlanStateSnapshotStaleError,
)


_E2E_SOURCE_REQUEST = (
    "app.py의 add 함수가 두 정수의 합을 반환하도록 최소 수정하고 "
    "공개 함수 계약을 유지하며 test_app.py 검증을 통과시켜줘."
)
# E2E-14만 사용자 금지 효과를 원문에 명시한다. 금지를 Goal·승인에 넣는 것은 live 역할이다.
_PROHIBITED_EFFECT_REQUEST = (
    _E2E_SOURCE_REQUEST + " 원격 저장소 push나 패키지 배포 같은 외부 시스템 변경은 하지 마."
)
# E2E-03만 수정 지시 없는 분석·보고를 요구한다. read_only 정규화는 live 역할이다.
_READ_ONLY_REPORT_REQUEST = (
    "app.py의 공개 함수들이 test_app.py가 기대하는 동작을 실제로 만족하는지 분석해서 "
    "보고해줘. 어떤 파일도 만들거나 고치거나 지우지 마."
)
# E2E-02는 앞 단계 산출물을 뒤 단계가 쓰는 두 단계 요청이다. Task 분할·의존성은 live 역할이 정한다.
_MULTI_TASK_DAG_REQUEST = (
    "두 단계로 진행해줘. 1단계: money.py의 cents 함수를 구현해 달러 금액 문자열을 센트 "
    "정수로 바꾸고 test_money.py 검증을 통과시켜. 2단계: 1단계가 끝난 뒤 invoice.py의 "
    "total 함수가 money.py의 cents를 재사용해 합계를 센트로 돌려주도록 구현하고 "
    "test_invoice.py 검증을 통과시켜. 테스트 파일과 공개 함수 이름은 바꾸지 마."
)
# E2E-04만 구현 결함의 같은 Task 자동 재시도를 사용자 요구로 적는다. 허용 여부를 Plan에 넣는 것은 live 역할이다.
_APPROVED_REPAIR_REQUEST = (
    _E2E_SOURCE_REQUEST
    + " 구현 결함으로 검사가 실패하면 같은 Task를 자동으로 다시 시도해도 된다."
)
# E2E-05는 규칙 모듈 파일명을 적지 않는다. 초기 Project Map에 없는 그 파일을 찾는 것은 live 준비 역할이다.
_CONTEXT_DISCOVERY_REQUEST = (
    "app.py의 shipping_fee 함수를 사내 배송비 규칙대로 구현해서 test_app.py 검증을 "
    "통과시켜줘. 규칙 값은 코드에 직접 적지 말고 기존 규칙 모듈을 그대로 재사용해."
)


@dataclass(frozen=True)
class _E2EScenarioPlan:
    """scenario마다 달라지는 fixture·요청 원문·사용자 승인 여부만 고른다."""

    fixture: str
    source_request: str
    authorize: bool = True


# 새 scenario는 여기에 한 줄을 더한다. 선언 순서가 그대로 cell 순서다.
_E2E_SCENARIO_PLANS: dict[str, _E2EScenarioPlan] = {
    "normal-completion": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "stale-after-materialization": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "stored-turn-restart-resume": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "unknown-receipt-no-duplicate": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "forced-termination-no-duplicate": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "absolute-timeout-no-duplicate": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "cancel-active-job": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "partial-write-input-changed": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "in-flight-replan-protection": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "partial-write-resume": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "prohibited-effect": _E2EScenarioPlan("project-e2e", _PROHIBITED_EFFECT_REQUEST),
    "scope-expansion": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST, authorize=False),
    "read-only-report": _E2EScenarioPlan("project-e2e-read-only", _READ_ONLY_REPORT_REQUEST),
    "usage-missing-late": _E2EScenarioPlan("project-e2e", _E2E_SOURCE_REQUEST),
    "multi-task-dag": _E2EScenarioPlan("project-e2e-multi-task", _MULTI_TASK_DAG_REQUEST),
    "approved-repair": _E2EScenarioPlan("project-e2e", _APPROVED_REPAIR_REQUEST),
    "context-discovery": _E2EScenarioPlan("project-e2e-context", _CONTEXT_DISCOVERY_REQUEST),
}

E2E_SCENARIOS = tuple(_E2E_SCENARIO_PLANS)

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
    authority: CoreActionAuthority | None = None


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
    authorize: bool = True,
) -> PreparedE2E:
    """실제 사용자 facade로 raw request부터 승인·Plan 활성화까지 수행한다.

    ``authorize=False``면 승인 후보 준비까지만 하고 승인·활성화는 cell driver에 맡긴다.
    역할이 scripted runner나 fake runtime으로 돌았으면 준비 출처를 LIVE로 적지 않는다.
    """

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
    authorization = None if not authorize else application.authorize(
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
    if authorization is not None:
        _write_json(authorization_path, authorization)
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
        preparation_provenance=(
            EvidenceProvenance.LIVE
            if structured_runner is None
            and not isinstance(getattr(runtime, "runtime", runtime), FakeCodexRuntime)
            else EvidenceProvenance.FAKE
        ),
        pipeline_stages=(
            "raw_request",
            "goal_normalizer",
            "goal_reviewer",
            "skeleton_generator",
            "skeleton_reviewer",
            "plan_expander",
            "plan_reviewer",
            *(("goal_authorization", "plan_activation") if authorization is not None else ()),
        ),
        preparation_evidence_refs=tuple(
            str(path.resolve())
            for path in (
                raw_request_path,
                goal_path,
                planning_path,
                generated_plan_path,
                *(
                    (authorization_path, activation_receipt_path)
                    if authorization is not None
                    else ()
                ),
            )
        ),
        authority=authority,
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
    read_only: bool = False,
) -> PreparedE2E:
    """합성 Goal·Plan을 활성화한다. ``read_only``는 E2E-03 결정적 검사용 분석 Goal이다."""

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

    request = _READ_ONLY_REPORT_REQUEST if read_only else (
        "app.py의 add 함수가 두 정수의 합을 반환하도록 최소 수정하고 공개 함수 계약을 "
        "유지하며 test_app.py 검증을 통과시켜줘."
    )
    task_kind = TaskKind.INSPECT if read_only else TaskKind.CHANGE
    task_objective = (
        "app.py 공개 함수가 test_app.py 기대를 만족하는지 파일을 바꾸지 않고 분석한다."
        if read_only
        else "app.py의 add 구현만 최소 수정해 실제 덧셈을 반환하게 한다."
    )
    task_product = "artifact:analysis-report" if read_only else "artifact:fixed-add"
    request_digest = sha256_bytes(request.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=request,
        source_request_digest=request_digest,
        mission_class=(
            MissionClass.ANALYSIS_AUDIT if read_only else MissionClass.BUGFIX_STABILIZATION
        ),
        observable_outcome=(
            "app.py 공개 함수의 실제 동작을 확인한 보고가 남는다."
            if read_only
            else "add(2, 3)이 5를 반환하고 회귀 테스트가 통과한다."
        ),
        hard_acceptance=(
            GoalCriterion(
                criterion_id="ac_fix",
                statement=(
                    "app.py 공개 함수가 test_app.py의 기대 동작을 만족하는지 확인된다."
                    if read_only
                    else "add(2, 3)이 5를 반환한다."
                ),
                validation_intent="Python unittest로 실제 동작을 확인한다.",
                trace_refs=("trace_request",),
            ),
        ),
        non_goals=(
            ("프로젝트 파일 생성·수정·삭제",)
            if read_only
            else ("공개 함수 이름과 test_app.py 변경",)
        ),
        source_traces=(
            SourceTrace(
                trace_id="trace_request",
                source_ref="user-request",
                statement=request,
                source_digest=request_digest,
            ),
        ),
        effect_policy=EffectPolicy(
            mutation_policy=(
                MutationPolicy.READ_ONLY if read_only else MutationPolicy.SCOPED_CHANGE
            ),
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
                kind=task_kind,
                objective=task_objective,
                contributes_to=("ac_fix",),
                produces=(task_product,),
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
        kind=task_kind,
        objective=task_objective,
        goal_criterion_refs=("ac_fix",),
        produces=(task_product,),
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
        expected_effects=() if read_only else ("app.py의 add 구현 최소 수정",),
        prohibited_effects=(
            ("프로젝트 파일 생성·수정·삭제",)
            if read_only
            else ("test_app.py 또는 공개 함수 계약 변경",)
        ),
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
                access="read" if read_only else "write",
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
                kind="inspect" if read_only else "edit",
                description=(
                    "app.py와 test_app.py를 읽어 공개 함수 동작을 확인한다."
                    if read_only
                    else "app.py의 뺄셈 연산만 덧셈으로 바꾼다."
                ),
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
        resource_locks=() if read_only else (f"file:{workspace / 'app.py'}",),
        timeout_seconds=900,
        idempotency_hint="project-e2e-read-only" if read_only else "project-e2e-add-fix",
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


def _fixture_directory_digest(source: Path) -> str:
    if not source.is_dir():
        raise QualificationRunError(f"E2E fixture 디렉터리가 없습니다: {source}")
    return sha256_digest(
        {
            path.relative_to(source).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(source.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        }
    )


def _copy_fixture(root: Path, cell_root: Path, fixture: str = "project-e2e") -> tuple[Path, str]:
    source = root / "tests" / "fixtures" / "engine" / fixture
    source_digest = _fixture_directory_digest(source)
    workspace = cell_root / "workspace"
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return workspace, source_digest


def _initialize_workspace_git(workspace: Path) -> dict[str, str]:
    """governance gate가 요구하는 git 저장소와 기준 commit을 workspace에 만든다.

    gate는 project root가 git 저장소 루트여야 하고 쓰기 target의 현재 내용을
    `HEAD:path`와 비교하므로 `git init`만으로는 부족하고 초기 commit까지 필요하다.
    사용자 전역 git 설정(서명·identity)에 기대지 않도록 호출마다 `-c`로 준다.
    """

    options = (
        "-c", "init.defaultBranch=main",
        "-c", "user.name=FlowMarshal E2E",
        "-c", "user.email=e2e@flowmarshal.invalid",
        "-c", "commit.gpgsign=false",
    )
    for arguments in (
        ("init", "--quiet"),
        ("add", "--all"),
        ("commit", "--quiet", "--no-gpg-sign", "-m", "E2E fixture baseline"),
        ("rev-parse", "HEAD"),
    ):
        result = subprocess.run(
            ("git", *options, *arguments),
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise QualificationRunError(
                f"E2E_WORKSPACE_GIT_INIT_FAILED: git {arguments[0]} -> "
                f"{result.returncode}: {result.stderr.strip()}"
            )
    return {"workspace_git_head": result.stdout.strip()}


def _workspace_files(workspace: Path) -> dict[str, bytes]:
    """Project Map 기본 무시 디렉터리(`.git`·가상환경·캐시·dist·build·Engine 경로 등)를 뺀 workspace 파일 본문이다."""

    from .context import DEFAULT_IGNORED_DIRECTORIES

    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
        and not any(
            part.casefold() in DEFAULT_IGNORED_DIRECTORIES
            for part in path.relative_to(workspace).parts
        )
    }


def _workspace_tree_digest(workspace: Path) -> dict[str, Any]:
    """`_workspace_files`의 무시 디렉터리를 뺀 workspace 전체 파일 트리의 digest다.

    제품 `_read_only_report_verification`의 `source_unchanged`는 baseline
    ProjectMap이 관측한 경로만 다시 읽으므로 baseline 밖에 새로 생긴 파일을 보지
    못한다(known limitation). E2E-03은 이 전체 트리 digest로 그 빈틈까지 덮으며
    제품 new-file detection은 후속 과제다.
    """

    files = {
        name: sha256_bytes(content) for name, content in _workspace_files(workspace).items()
    }
    return {
        "file_count": len(files),
        "files": files,
        "tree_digest": sha256_digest(files),
    }


def _workspace_tree_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    names = set(before["files"]) | set(after["files"])
    return sorted(
        name for name in names if before["files"].get(name) != after["files"].get(name)
    )


def _independent_goal_test_evidence_count(
    connection: Any, project_id: str, definition: dict[str, Any]
) -> int:
    """독립 Goal Test evidence는 활성 Plan의 integration validation ID와 그 검사가
    요구한 evidence 종류로 찾는다. live 역할이 지은 ID를 하드코딩하지 않는다."""

    count = 0
    for item in definition["integration_validations"]:
        kinds = tuple(item["required_evidence_kinds"])
        if not kinds:
            continue
        prefix = f"validation:{item['validation_id']}:"
        count += connection.execute(
            "SELECT COUNT(*) FROM evidence_records WHERE project_id = ? AND task_id IS NULL "
            f"AND kind IN ({','.join('?' for _ in kinds)}) AND substr(source_ref, 1, ?) = ?",
            (project_id, *kinds, len(prefix), prefix),
        ).fetchone()[0]
    return count


def _status_assertions(
    prepared: PreparedE2E,
    source_digest: str,
    *,
    required_files: tuple[str, ...] = ("app.py",),
) -> dict[str, Any]:
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
        plan_row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id = ?", (prepared.plan_revision_id,)).fetchone()
        definition = json.loads(plan_row["payload_json"])["definition"]
        expected_validation_count = sum(len(item["validations"]) for item in definition["tasks"]) + len(definition["integration_validations"])
        goal_direct_evidence_count = _independent_goal_test_evidence_count(
            connection, prepared.project_id, definition
        )
    passed = all(
        (
            status["project"]["run_state"] == "completed",
            status["history_valid"],
            evidence_count >= 3,
            validation_count == expected_validation_count,
            binding_count >= 1,
            verdict_count == 1,
            goal_direct_evidence_count >= 1,
            all((prepared.workspace / name).is_file() for name in required_files),
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
    required_files: tuple[str, ...] = ("app.py",),
) -> dict[str, Any]:
    provider = _execution_proposal_provider(prepared, runtime, roles)
    if provider is None and prepared.proposal is None:
        raise QualificationRunError(
            "E2E_EXECUTION_PROPOSAL_PROVIDER_REQUIRED: raw-request E2E는 실제 "
            "execution_spec_prepare 역할을 사용해야 합니다."
        )
    task_gate = _open_e2e_task_gate(prepared, runtime, roles, governance)
    try:
        return _drive_normal_completion(
            prepared, runtime, source_digest, provider, task_gate,
            required_files=required_files,
        )
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
    *,
    required_files: tuple[str, ...] = ("app.py",),
) -> dict[str, Any]:
    dispatcher = EngineDispatcher(prepared.service, runtime, proposal_provider=provider, task_gate=task_gate)
    deadline = time.monotonic() + 900
    actions: list[str] = []
    if prepared.service.status(prepared.project_id)["project"]["run_state"] == "completed":
        result = _status_assertions(prepared, source_digest, required_files=required_files)
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
            result = _status_assertions(prepared, source_digest, required_files=required_files)
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
        calls = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(new_turn_count),0),"
            "COALESCE(SUM(CASE WHEN execution_status='reserved' THEN 1 ELSE 0 END),0) "
            "FROM provider_calls WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        receipts = connection.execute(
            "SELECT COUNT(*),COUNT(DISTINCT provider_operation_id) "
            "FROM runtime_receipts WHERE intent_id IN "
            "(SELECT id FROM runtime_intents WHERE attempt_id=?)",
            (attempt_id,),
        ).fetchone()
        return {
            "provider_calls": calls[0],
            "provider_new_turn_count": calls[1],
            "provider_reserved_calls": calls[2],
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


def _execution_fault_paths(prepared: PreparedE2E) -> tuple[Path, Path, dict[str, Any]]:
    """현재 spec에서 쓰기 target과 별도 immutable 파일을 고른다."""

    with prepared.service.ledger.read() as connection:
        row = connection.execute(
            "SELECT payload_json FROM execution_spec_revisions "
            "WHERE task_id=? AND is_current=1",
            (prepared.task_id,),
        ).fetchone()
    if row is None:
        raise QualificationRunError("E2E_EXECUTION_SPEC_MISSING")
    spec = json.loads(row["payload_json"])
    definition = spec["definition"]
    mutable = {
        item["path"]
        for item in definition["resolved_targets"]
        if item["access"] in {"write", "create", "delete"}
    }
    if not mutable:
        raise QualificationRunError("E2E_MUTABLE_TARGET_MISSING")
    mutable_ref = sorted(mutable)[0]
    immutable_refs = [
        item["source_ref"]
        for item in definition["context_manifest"]["fragments"]
        if item["source_ref"] not in mutable
        and (prepared.workspace / item["source_ref"]).is_file()
    ]
    if not immutable_refs:
        raise QualificationRunError("E2E_IMMUTABLE_CONTEXT_MISSING")
    immutable_ref = (
        "AGENTS.md" if "AGENTS.md" in immutable_refs else sorted(immutable_refs)[0]
    )
    return (
        prepared.workspace / mutable_ref,
        prepared.workspace / immutable_ref,
        spec,
    )


def _await_terminal_after_fault(
    runtime: RecordedRuntime,
    binding: ThreadBinding,
    *,
    timeout_seconds: float,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    observation = runtime.read(thread_id=binding.thread_id)
    while observation.active and time.monotonic() < deadline:
        time.sleep(0.05)
        observation = runtime.read(thread_id=binding.thread_id)
    if observation.active or observation.terminal_status is None:
        raise QualificationRunError("E2E fault 뒤 exact provider turn terminal을 확인하지 못했습니다.")
    return observation


def _partial_write_input_changed(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """부분 쓰기 뒤 immutable 입력 변경을 구조화해 막고 효과 0을 증명한다."""

    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=RuntimeJobSupervisor(
            prepared.service, runtime, observation_timeout_seconds=5.0
        ),
    )
    try:
        _materialize_with_application(application, prepared, timeout_seconds=timeout_seconds)
        dispatched = application.run_once(prepared.project_id)
        if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
            raise QualificationRunError("E2E-13 active execution Attempt를 시작하지 못했습니다.")
        binding, _job_id = _await_active_execution_binding(
            prepared, dispatched.attempt_id, timeout_seconds=timeout_seconds
        )
        _read_active_exact_turn(runtime, binding, scenario="partial-write-input-changed")
        mutable_path, immutable_path, spec = _execution_fault_paths(prepared)
        immutable_original = immutable_path.read_bytes()
        mutable_before = sha256_bytes(mutable_path.read_bytes())
        runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
        terminal = _await_terminal_after_fault(
            runtime, binding, timeout_seconds=timeout_seconds
        )
        mutable_path.write_bytes(mutable_path.read_bytes() + b"\n# injected partial write\n")
        immutable_path.write_bytes(immutable_original + b"\n# injected immutable change\n")
        fault = _write_fault_injection(
            cell_root / "fault-injection.json",
            {
                "kind": "partial_write_then_immutable_input_change",
                "attempt_id": dispatched.attempt_id,
                "thread_id": binding.thread_id,
                "turn_id": binding.turn_id,
                "mutable_path": str(mutable_path.relative_to(prepared.workspace)),
                "mutable_before_digest": mutable_before,
                "mutable_after_digest": sha256_bytes(mutable_path.read_bytes()),
                "immutable_path": str(immutable_path.relative_to(prepared.workspace)),
                "immutable_before_digest": sha256_bytes(immutable_original),
                "immutable_after_digest": sha256_bytes(immutable_path.read_bytes()),
                "writer": "qualification_fault_injector",
            },
        )
        before = _journal_operation_counts(runtime.events)
        effects_before = _attempt_effect_counts(prepared, dispatched.attempt_id)
        blocked = application.run_once(prepared.project_id)
        deadline = time.monotonic() + timeout_seconds
        while (
            not (
                blocked.action is RunOnceAction.BLOCKED
                and blocked.blocker_code == "STALE_EXECUTION_INPUT"
            )
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            blocked = application.run_once(prepared.project_id)
        middle = _journal_operation_counts(runtime.events)
        effects_after_block = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        repeated = application.run_once(prepared.project_id)
        after = _journal_operation_counts(runtime.events)
        effects_after_repeat = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        with prepared.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT status,binding_json FROM attempts WHERE id=?",
                (dispatched.attempt_id,),
            ).fetchone()
            marker = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE entity_id IN (SELECT id FROM runtime_intents WHERE attempt_id=?) "
                "AND event_type='runtime.effect_not_started' "
                "ORDER BY sequence DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
        marker_payload = None if marker is None else json.loads(marker["payload_json"])
        changes = () if marker_payload is None else tuple(marker_payload.get("changes", ()))
        immutable_ref = str(immutable_path.relative_to(prepared.workspace))
        mutable_ref = str(mutable_path.relative_to(prepared.workspace))
        passed = all((
            blocked.action is RunOnceAction.BLOCKED,
            blocked.blocker_code == "STALE_EXECUTION_INPUT",
            repeated.action is RunOnceAction.BLOCKED,
            repeated.blocker_code == "STALE_EXECUTION_INPUT",
            attempt is not None
            and attempt["status"] in {"reserved", "starting", "running"},
            attempt is not None and attempt["binding_json"] is not None,
            marker_payload is not None,
            any(
                item.get("kind") == "immutable_input_changed"
                and item.get("path") == immutable_ref
                for item in changes
            ),
            any(
                item.get("kind") == "mutable_target_observation"
                and item.get("path") == mutable_ref
                and item.get("changed") is True
                for item in changes
            ),
            middle == after,
            middle["create_thread"] == before["create_thread"],
            middle["start_turn"] == before["start_turn"],
            middle["resume"] == before["resume"],
            effects_after_repeat == effects_after_block,
            effects_after_block["runtime_receipts"]
            == effects_before["runtime_receipts"],
        ))
        return {
            "passed": passed,
            "source_fixture_digest": source_digest,
            "partial_effect": fault,
            "source_digest": {
                "execution_spec_digest": spec["definition_digest"],
                "immutable_before": fault["immutable_before_digest"],
                "immutable_after": fault["immutable_after_digest"],
            },
            "error": marker_payload,
            "effect_count": {
                "provider_operations": {
                    key: after[key] - before[key]
                    for key in ("create_thread", "start_turn", "resume")
                },
                "attempt_before_preflight": effects_before,
                "attempt_after_first_block": effects_after_block,
                "attempt_after_repeated_block": effects_after_repeat,
            },
            "runtime_observation": terminal.model_dump(mode="json"),
            "repeated_blocker": repeated.blocker_code,
        }
    finally:
        application.close_task_gate()


def _partial_write_resume(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """부분 쓰기 뒤 immutable 입력이 같을 때 같은 thread를 한 번만 재개한다."""

    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=RuntimeJobSupervisor(
            prepared.service, runtime, observation_timeout_seconds=5.0
        ),
    )
    try:
        _materialize_with_application(application, prepared, timeout_seconds=timeout_seconds)
        dispatched = application.run_once(prepared.project_id)
        if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
            raise QualificationRunError("E2E-12 active execution Attempt를 시작하지 못했습니다.")
        binding, _job_id = _await_active_execution_binding(
            prepared, dispatched.attempt_id, timeout_seconds=timeout_seconds
        )
        _read_active_exact_turn(runtime, binding, scenario="partial-write-resume")
        mutable_path, _immutable_path, _spec = _execution_fault_paths(prepared)
        before_digest = sha256_bytes(mutable_path.read_bytes())
        runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
        _await_terminal_after_fault(runtime, binding, timeout_seconds=timeout_seconds)
        mutable_path.write_bytes(mutable_path.read_bytes() + b"\n# injected partial write\n")
        fault = _write_fault_injection(
            cell_root / "fault-injection.json",
            {
                "kind": "partial_write_with_immutable_inputs_preserved",
                "attempt_id": dispatched.attempt_id,
                "thread_id": binding.thread_id,
                "turn_id": binding.turn_id,
                "mutable_path": str(mutable_path.relative_to(prepared.workspace)),
                "mutable_before_digest": before_digest,
                "mutable_after_digest": sha256_bytes(mutable_path.read_bytes()),
                "writer": "qualification_fault_injector",
            },
        )
        effect_counts_before = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        before = _journal_operation_counts(runtime.events)
        resumed = application.run_once(prepared.project_id)
        deadline = time.monotonic() + timeout_seconds
        while runtime.resume_calls == before["resume"] and time.monotonic() < deadline:
            time.sleep(0.05)
            resumed = application.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?",
                (dispatched.attempt_id,),
            ).fetchone()
            dispatching = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE project_id=? AND event_type='runtime.effect_dispatching' "
                "AND json_extract(payload_json,'$.attempt_id')=? "
                "AND json_extract(payload_json,'$.mutable_target_resume')=1 "
                "ORDER BY sequence DESC LIMIT 1",
                (prepared.project_id, dispatched.attempt_id),
            ).fetchone()
        resumed_binding = ThreadBinding.model_validate_json(row["binding_json"])
        after_resume = _journal_operation_counts(runtime.events)
    finally:
        application.close_task_gate()

    completion = _normal_completion(
        prepared,
        runtime,
        source_digest,
        roles=roles,
        governance=governance,
    )
    after = _journal_operation_counts(runtime.events)
    effect_counts_after = _attempt_effect_counts(prepared, dispatched.attempt_id)
    passed = bool(completion.get("passed")) and all((
        resumed.action is RunOnceAction.DISPATCHED,
        resumed_binding.thread_id == binding.thread_id,
        resumed_binding.turn_id != binding.turn_id,
        after_resume["create_thread"] == before["create_thread"],
        after_resume["resume"] == before["resume"] + 1,
        after["resume"] == before["resume"] + 1,
        effect_counts_after["runtime_receipts"]
        == effect_counts_after["distinct_provider_operation_ids"],
        dispatching is not None,
    ))
    return {
        **completion,
        "passed": passed,
        "partial_effect": fault,
        "binding": {
            "before": binding.model_dump(mode="json"),
            "after": resumed_binding.model_dump(mode="json"),
        },
        "runtime_observation": {
            "resume_action": resumed.action.value,
            "operation_counts_before": before,
            "operation_counts_after_resume": after_resume,
            "operation_counts_after_completion": after,
            "attempt_effect_counts_before": effect_counts_before,
            "attempt_effect_counts_after": effect_counts_after,
        },
        "validation": {
            "pass_validation_count": completion.get("pass_validation_count"),
            "goal_verdict_count": completion.get("goal_verdict_count"),
        },
    }


def _register_equivalent_plan_revision(
    prepared: PreparedE2E,
    *,
    base_plan_revision_id: str | None = None,
) -> tuple[PlanContractRevision, TaskContract]:
    service = prepared.service
    goal = service.load_active_goal(prepared.project_id)
    project_map = service.load_current_project_map(prepared.project_id)
    state = service.load_current_state(prepared.project_id, goal.definition_digest)
    with service.ledger.read() as connection:
        project = connection.execute(
            "SELECT active_plan_revision_id FROM projects WHERE id=?",
            (prepared.project_id,),
        ).fetchone()
        plan_revision_id = (
            base_plan_revision_id
            or project["active_plan_revision_id"]
            or prepared.plan_revision_id
        )
        active = connection.execute(
            "SELECT payload_json FROM plan_revisions WHERE id=?",
            (plan_revision_id,),
        ).fetchone()
    current = PlanContractRevision.model_validate_json(active["payload_json"])
    if len(current.definition.tasks) != 1:
        raise QualificationRunError("E2E-17 requires a single-task source plan")
    previous_task = current.definition.tasks[0]
    task = previous_task.model_copy(update={"task_id": new_id("task")})
    definition = current.definition.model_copy(update={
        "base_state_snapshot_digest": state.snapshot_digest,
        "project_map_digest": project_map.revision_digest,
        "tasks": (task,),
        "goal_coverage": tuple(
            item.model_copy(update={"task_ids": (task.task_id,)})
            for item in current.definition.goal_coverage
        ),
    })
    revision = PlanContractRevision(
        plan_revision_id=new_id("plan_revision"),
        plan_id=current.plan_id,
        revision_no=current.revision_no + 1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        supersedes_plan_revision_id=current.plan_revision_id,
        created_at=utc_now(),
    )
    review = ReviewerSubmission(
        reviewer_role="qualification-plan-reviewer",
        candidate_digest=revision.activation_digest,
        ratings=ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        ),
        evidence_catalog_digest=sha256_digest(
            plan_review_evidence_catalog(revision, goal, state, project_map)
        ),
    )
    service.register_plan_evaluation(
        ExpandedPlanEvaluation(
            plan=revision,
            semantic_submissions=(review,),
            decision=CandidateDecision(
                candidate_digest=revision.activation_digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
    )
    return revision, task


def _replacement_state(service: EngineService, project_id: str) -> dict[str, Any]:
    with service.ledger.read() as connection:
        return {
            "project": dict(connection.execute(
                "SELECT active_plan_revision_id,run_state FROM projects WHERE id=?",
                (project_id,),
            ).fetchone()),
            "plans": [dict(row) for row in connection.execute(
                "SELECT id,status FROM plan_revisions WHERE project_id=? ORDER BY revision_no",
                (project_id,),
            )],
            "tasks": [dict(row) for row in connection.execute(
                "SELECT id,plan_revision_id,status FROM task_contracts WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )],
            "attempts": [dict(row) for row in connection.execute(
                "SELECT id,plan_revision_id,task_id,status,binding_json FROM attempts "
                "WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )],
            "intents": [dict(row) for row in connection.execute(
                "SELECT i.id,i.attempt_id,i.kind,i.status FROM runtime_intents i "
                "JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY i.rowid",
                (project_id,),
            )],
            "receipts": [dict(row) for row in connection.execute(
                "SELECT r.id,r.intent_id,r.provider_operation_id FROM runtime_receipts r "
                "JOIN runtime_intents i ON i.id=r.intent_id JOIN attempts a ON a.id=i.attempt_id "
                "WHERE a.project_id=? ORDER BY r.rowid",
                (project_id,),
            )],
            "plan_activations": [dict(row) for row in connection.execute(
                "SELECT plan_revision_id,authorization_id,activation_digest "
                "FROM plan_activations WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )],
            "replacement_history": [dict(row) for row in connection.execute(
                "SELECT sequence,event_type,entity_type,entity_id FROM history_events "
                "WHERE project_id=? AND (event_type LIKE 'plan.%' "
                "OR event_type LIKE 'project.%' OR event_type LIKE 'task.%' "
                "OR event_type LIKE 'attempt.%') ORDER BY sequence",
                (project_id,),
            )],
        }


_LEDGER_ROWS = {
    "project": "SELECT id,active_plan_revision_id,run_state FROM projects WHERE id=?",
    "plans": "SELECT id,status FROM plan_revisions WHERE project_id=? ORDER BY rowid",
    "tasks": "SELECT id,plan_revision_id,status FROM task_contracts WHERE project_id=? ORDER BY rowid",
    "goal_authorizations": (
        "SELECT id,revision_no,authorization_digest FROM goal_authorizations "
        "WHERE project_id=? ORDER BY rowid"
    ),
    "plan_activations": (
        "SELECT id,plan_revision_id,authorization_id FROM plan_activations "
        "WHERE project_id=? ORDER BY rowid"
    ),
    "execution_specs": (
        "SELECT s.id,s.task_id FROM execution_spec_revisions s "
        "JOIN task_contracts t ON t.id=s.task_id WHERE t.project_id=? ORDER BY s.rowid"
    ),
    "attempts": "SELECT id,task_id,status FROM attempts WHERE project_id=? ORDER BY rowid",
    "runtime_intents": (
        "SELECT i.id,i.kind,i.status FROM runtime_intents i "
        "JOIN attempts a ON a.id=i.attempt_id WHERE a.project_id=? ORDER BY i.rowid"
    ),
    "runtime_jobs": "SELECT id,kind,status FROM runtime_jobs WHERE project_id=? ORDER BY rowid",
    "provider_calls": (
        "SELECT id,role,attempt_id,execution_status FROM provider_calls "
        "WHERE project_id=? ORDER BY rowid"
    ),
    "budget_policy_revisions": (
        "SELECT id,scope_key,revision_no,policy_digest FROM budget_policy_revisions "
        "WHERE project_id=? ORDER BY rowid"
    ),
    "history_events": (
        "SELECT id,event_type,entity_type,entity_id FROM history_events "
        "WHERE project_id=? ORDER BY sequence"
    ),
}
# 실행 효과로 이어지는 표. 승인 경계 판정 동안 여기에는 새 행이 생기면 안 된다.
_EXECUTION_TABLES = frozenset({"execution_specs", "attempts", "runtime_intents", "runtime_jobs"})
_ZERO_EFFECTS = {"create_thread": 0, "start_turn": 0, "resume": 0}
_AUTHORIZATION_REQUIRED_PREFIX = str(GoalAuthorizationRequired(()))[: -len("[]")]
# 비admissible 후보는 draft로 저장되고 활성화는 이 문구로 거절된다(service._activate_plan_in_transaction).
_DRAFT_ACTIVATION_ERROR = "admissible이며 ready인 PlanContract만 활성화할 수 있습니다."


def _cell_failure(
    code: str,
    detail: str,
    failure_class: QualificationFailureClass = QualificationFailureClass.PRODUCT,
) -> dict[str, str]:
    """cell 실패를 안정된 code·설명·class로 남긴다. 책임 판정은 code와 class만 쓴다."""

    return {
        "failure": f"{code}: {detail}",
        "failure_code": code,
        "failure_class": failure_class.value,
    }


def _history_types(delta: dict[str, dict[str, list[Any]]]) -> list[str] | None:
    """history_events가 추가만 됐으면 그 event_type 목록, 바뀌거나 지워졌으면 None이다."""

    history = delta.get("history_events", {"added": [], "changed": [], "removed": []})
    if history["changed"] or history["removed"]:
        return None
    return [row["event_type"] for row in history["added"]]


def _ledger_rows(service: EngineService, project_id: str) -> dict[str, dict[str, dict[str, Any]]]:
    """승인·활성화·실행 판정에 쓰는 원장 행(append-only history 포함)을 표·id별로 읽는다."""

    with service.ledger.read() as connection:
        return {
            key: {row["id"]: dict(row) for row in connection.execute(query, (project_id,))}
            for key, query in _LEDGER_ROWS.items()
        }


def _ledger_delta(
    before: dict[str, dict[str, dict[str, Any]]],
    after: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, list[Any]]]:
    """표별로 새 행·바뀐 행·사라진 행만 남긴다. 변화 없는 표는 뺀다."""

    delta: dict[str, dict[str, list[Any]]] = {}
    for key, rows in after.items():
        old = before[key]
        added = [row for row_id, row in rows.items() if row_id not in old]
        changed = [
            {"before": old[row_id], "after": row}
            for row_id, row in rows.items()
            if row_id in old and old[row_id] != row
        ]
        removed = [row for row_id, row in old.items() if row_id not in rows]
        if added or changed or removed:
            delta[key] = {"added": added, "changed": changed, "removed": removed}
    return delta


def _authorization_change_fields(message: str | None) -> list[list[str]] | None:
    """GoalAuthorizationRequired 문구에서 Core가 낸 (boundary, field) 목록을 되읽는다."""

    if not message or not message.startswith(_AUTHORIZATION_REQUIRED_PREFIX):
        return None
    changes = json.loads(message[len(_AUTHORIZATION_REQUIRED_PREFIX):])
    return sorted([item["boundary"], item["field"]] for item in changes)


def _plan_expansion_stub(
    plan: PlanContractRevision, *, task_ref: str, effect: EffectContract
) -> dict[str, Any]:
    """활성 Plan을 plan_expander 응답 형식으로 되돌리고 한 Task에 효과 하나를 더한다."""

    refs = {task.task_id: task.task_ref for task in plan.definition.tasks}
    fields = set(DetailedTaskDraft.model_fields)
    tasks = []
    for task in plan.definition.tasks:
        body = {key: value for key, value in task.model_dump(mode="json").items() if key in fields}
        if task.task_ref == task_ref:
            body["expected_effects"] = [*body["expected_effects"], effect.model_dump(mode="json")]
        tasks.append(body)
    definition = plan.definition
    return {
        "tasks": tasks,
        "dependencies": [
            {
                "producer_task_ref": refs[item.producer_task_id],
                "consumer_task_ref": refs[item.consumer_task_id],
                "dependency_type": item.dependency_type.value,
                "products": list(item.products),
            }
            for item in definition.dependencies
        ],
        "goal_coverage": [
            {
                "criterion_id": item.criterion_id,
                "task_refs": [refs[task_id] for task_id in item.task_ids],
                "validation_ids": list(item.validation_ids),
            }
            for item in definition.goal_coverage
        ],
        "integration_validations": [
            item.model_dump(mode="json") for item in definition.integration_validations
        ],
        "expected_effects": list(definition.expected_effects),
        "prohibited_effects": list(definition.prohibited_effects),
    }


def _stub_plan_inspection(plan: dict[str, Any], goal: dict[str, Any]) -> dict[str, Any]:
    """stub plan_expander 응답의 v1 inspection 대조표. 의미 판단이 아니라 형식 채움이다."""

    citations: list[dict[str, Any]] = []

    def cite(source: str, selector: str, text: str) -> str:
        citations.append({
            "citation_id": f"c{len(citations)}",
            "source_ref": source,
            "selector": selector,
            "quote": text,
        })
        return citations[-1]["citation_id"]

    ac_refs = {
        ac["criterion_id"]: (
            cite("source:goal", f"/hard_acceptance/{index}/statement", ac["statement"]),
            cite("source:goal", f"/hard_acceptance/{index}/validation_intent", ac["validation_intent"]),
        )
        for index, ac in enumerate(goal["hard_acceptance"])
    }
    validations = [
        (f"/tasks/{task_index}/validations/{index}", item)
        for task_index, task in enumerate(plan["tasks"])
        for index, item in enumerate(task["validations"])
    ] + [
        (f"/integration_validations/{index}", item)
        for index, item in enumerate(plan["integration_validations"])
    ]
    validation_refs = {
        item["validation_id"]: cite("artifact:plan_draft", path + "/statement", item["statement"])
        for path, item in validations
    }
    constraint_refs = {
        item["constraint_id"]: cite("source:goal", f"/constraints/{index}/statement", item["statement"])
        for index, item in enumerate(goal.get("constraints", []))
    }
    return {
        "citations": citations,
        "ac_validation_rows": [
            {
                "criterion_id": criterion_id,
                "validation_id": validation_id,
                "ac_link_required": False,
                "scope_ids": [],
                "basis_refs": [*ac_ref, validation_ref],
                "finding_codes": [],
            }
            for criterion_id, ac_ref in ac_refs.items()
            for validation_id, validation_ref in validation_refs.items()
        ],
        "constraint_task_rows": [
            {
                "constraint_id": constraint_id,
                "task_ref": task["task_ref"],
                "applicability": "not_applicable",
                "validation_ids": [],
                "basis_refs": [ref],
                "finding_codes": [],
            }
            for constraint_id, ref in constraint_refs.items()
            for task in plan["tasks"]
        ],
        "validation_rows": [
            {
                "validation_id": validation_id,
                "claim_ref": ref,
                "mechanisms": [{"tool": "합성 검사 책임", "phase": None, "basis_refs": [ref]}],
                "separate_check_refs": [],
            }
            for validation_id, ref in validation_refs.items()
        ],
        "validation_scope_rows": [
            {
                "scope_id": f"scope_{index}",
                "validation_id": validation_id,
                "claim_ref": ref,
                "procedure": "합성 검사 책임",
                "phase": None,
                "basis_refs": [ref],
                "assessment": "supported",
                "finding_codes": [],
            }
            for index, (validation_id, ref) in enumerate(validation_refs.items())
        ],
        "finding_links": [],
    }


class _StubPlanExpanderRunner(ScriptedStructuredRoleRunner):
    """E2E-14 후보를 만드는 synthetic stub(fake). plan_expander 응답을 v1 envelope로 준다."""

    def run(self, request: Any, *, validator: Any = None) -> Any:
        pending = self.responses.get(request.role)
        if request.role == "plan_expander" and pending and "inspection" not in pending[0]:
            pending[0] = {
                "plan": pending[0],
                "inspection": _stub_plan_inspection(pending[0], request.payload["goal"]),
            }
        return super().run(request, validator=validator)


def _prohibited_effect_blocked(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration,
) -> dict[str, Any]:
    """live Goal·승인이 금지한 효과를 요구하는 재계획 후보가 활성화·실행 전에 막히는지 본다.

    live Planner는 금지 효과를 스스로 넣지 않으므로 위반 후보만 제품 RecoveryPlanProvider에
    scripted plan_expander stub(fake)을 끼워 만든다. 판정은 Core plan_gate와 활성화가 한다.
    """

    service, project_id = prepared.service, prepared.project_id
    goal = service.load_active_goal(project_id)
    with service.ledger.read() as connection:
        authorization = GoalAuthorization.model_validate_json(connection.execute(
            "SELECT payload_json FROM goal_authorizations WHERE project_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()["payload_json"])
        active = PlanContractRevision.model_validate_json(connection.execute(
            "SELECT p.payload_json FROM plan_revisions p "
            "JOIN projects j ON j.active_plan_revision_id=p.id WHERE j.id=?",
            (project_id,),
        ).fetchone()["payload_json"])
    prohibited = goal.definition.effect_policy.prohibited_effects
    observation: dict[str, Any] = {
        "schema": "flowmarshal.project-e2e.authorization-observation.v2",
        "cell_id": "prohibited-effect",
        "source_request": _PROHIBITED_EFFECT_REQUEST,
        "authorization": authorization.model_dump(mode="json"),
        "goal_prohibited_effects": list(prohibited),
        "authorization_matches_goal_effect_policy": (
            authorization.effect_policy == goal.definition.effect_policy
        ),
        "live_active_plan_revision_id": active.plan_revision_id,
        "blocking_check": (
            "Core plan_gate가 Goal effect_policy로 막는다. GoalAuthorization 효과 정책과 같은지는 "
            "authorization_matches_goal_effect_policy로 따로 대조한다."
        ),
        "limitations": [
            "후보 Plan 내용은 harness가 stub plan_expander 응답으로 작성한다.",
            "재계획 트리거는 실패 없이 활성 Task로 직접 호출한 합성 트리거다.",
            "run_once 자동 재계획을 거치지 않고 같은 등록·활성화 API를 직접 부른다.",
            "Core Gate는 금지 효과 문장과 정확히 같은 문자열만 막는다. 의미가 같은 다른 표현은 다루지 않는다.",
        ],
    }
    if not prohibited or not observation["authorization_matches_goal_effect_policy"]:
        failure = _cell_failure(
            "E2E14_PRECONDITION_NO_PROHIBITED_EFFECT",
            "live Goal·승인에 금지 효과가 없거나 승인 효과 정책이 Goal과 다릅니다.",
        )
        observation["failure"] = failure["failure"]
        _write_json(cell_root / "authorization-observation.json", observation)
        return {
            "passed": False,
            "source_fixture_digest": source_digest,
            **failure,
            "error": None,
            "execution_spec": {"candidate_execution_spec_count": None},
            "effect_count": None,
        }
    statement = prohibited[0]
    failed_task = active.definition.tasks[0]
    stub = _StubPlanExpanderRunner({
        "plan_expander": [_plan_expansion_stub(
            active,
            task_ref=failed_task.task_ref,
            effect=EffectContract(
                effect_id="qualification-prohibited", statement=statement, external=False
            ),
        )],
    })
    inventory = runtime.list_models()
    journal_before = _journal_operation_counts(runtime.events)
    # 후보 생성과 차단을 따로 잰다. 후보 생성은 stub 계획 호출 하나만, 차단 단계는
    # draft 후보 행만 남겨야 한다.
    before = _ledger_rows(service, project_id)
    evaluation = RecoveryPlanProvider(service=service, runner=stub, roles=roles).replan(
        project_id=project_id, task_id=failed_task.task_id, assessment=None, inventory=inventory
    )
    candidate_rows = _ledger_rows(service, project_id)
    activation_error: str | None = None
    try:
        service.register_authorized_plan_revision(evaluation)
    except EngineServiceError as error:
        activation_error = str(error)
    candidate_delta = _ledger_delta(before, candidate_rows)
    block_delta = _ledger_delta(candidate_rows, _ledger_rows(service, project_id))
    journal_after = _journal_operation_counts(runtime.events)
    effect_count = {key: journal_after[key] - journal_before[key] for key in _ZERO_EFFECTS}
    candidate_id = evaluation.plan.plan_revision_id
    finding = next(
        (item for item in evaluation.deterministic_findings
         if item.finding_code == "PLAN_EFFECT_POLICY_VIOLATION"),
        None,
    )
    blocked = {key: value["added"] for key, value in block_delta.items()}
    candidate_history = _history_types(candidate_delta)
    checks = {
        "decision_not_admissible": evaluation.decision.status is not CandidateStatus.ADMISSIBLE,
        # 차단 사유가 금지 효과 하나여야 stub이 다른 결함을 더하지 않았다고 볼 수 있다.
        "only_effect_policy_finding": tuple(evaluation.decision.finding_codes)
        == ("PLAN_EFFECT_POLICY_VIOLATION",),
        "prohibited_finding_exact": finding is not None and finding.summary == (
            f"Goal 효과 정책 밖의 외부 효과입니다: unexpected=[], prohibited={[statement]}"
        ),
        "reviewer_not_called": not evaluation.semantic_submissions,
        # 후보 생성은 stub plan_expander 호출 한 건과 그 예산 회계 history만 남긴다.
        "candidate_creation_is_one_stub_plan_expander_call": set(candidate_delta)
        <= {"provider_calls", "history_events"}
        and "provider_calls" in candidate_delta
        and not candidate_delta["provider_calls"]["changed"]
        and not candidate_delta["provider_calls"]["removed"]
        and [
            (row["role"], row["attempt_id"])
            for row in candidate_delta["provider_calls"]["added"]
        ] == [("plan_expander", None)]
        and candidate_history is not None
        and all(item.startswith("budget.") for item in candidate_history),
        "activation_rejected_exactly": activation_error == _DRAFT_ACTIVATION_ERROR,
        # 차단은 draft 후보 행과 그 등록 history 한 건만 남긴다.
        "block_adds_only_draft_candidate_rows": set(block_delta)
        <= {"plans", "tasks", "history_events"}
        and all(not value["changed"] and not value["removed"] for value in block_delta.values())
        and [(row["id"], row["status"]) for row in blocked.get("plans", [])]
        == [(candidate_id, "draft")]
        and all(row["plan_revision_id"] == candidate_id for row in blocked.get("tasks", []))
        and _history_types(block_delta) == ["plan.registered"],
        "no_runtime_effect": effect_count == _ZERO_EFFECTS,
    }
    passed = all(checks.values())
    observation.update({
        "candidate_source": "scripted_plan_expander_stub_via_recovery_plan_provider",
        "candidate_plan_revision_id": candidate_id,
        "requested_effect": {"effect_id": "qualification-prohibited", "statement": statement},
        "stub_calls": [call.role for call in stub.calls],
        "decision": evaluation.decision.model_dump(mode="json"),
        "finding_summary": None if finding is None else finding.summary,
        "activation_error": activation_error,
        "measured_tables": sorted(_LEDGER_ROWS),
        "candidate_creation_ledger_delta": candidate_delta,
        "block_ledger_delta": block_delta,
        "stage_attribution": {
            "live_active_plan": list(prepared.pipeline_stages),
            "candidate": [
                "plan_expander (scripted stub)",
                "effect_policy_check (Core plan_gate)",
                "plan_activation (rejected)",
            ],
        },
        "checks": checks,
    })
    _write_json(cell_root / "authorization-observation.json", observation)
    return {
        "passed": passed,
        "source_fixture_digest": source_digest,
        **({"failure": None} if passed else _cell_failure(
            "E2E14_BLOCK_EVIDENCE_MISSING",
            "금지 효과 후보가 실행 전에 막힌다는 증거가 부족합니다: "
            + ",".join(key for key, value in checks.items() if not value),
        )),
        "error": activation_error,
        "execution_spec": {"candidate_execution_spec_count": len(blocked.get("execution_specs", []))},
        "effect_count": effect_count,
        "checks": checks,
        "candidate_plan_revision_id": candidate_id,
        "candidate_decision": evaluation.decision.status.value,
        "candidate_finding_codes": list(evaluation.decision.finding_codes),
    }


def _scope_expansion_blocked(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration,
    evaluation_contract_digest: str,
    fixture_digest: str,
    governance: Any | None = None,
) -> dict[str, Any]:
    """승인 범위를 넘는 운영 정책은 새 사용자 승인 없이 활성화·실행되지 않는지 본다.

    live 1.0 경로는 target·효과 확장 후보를 만들 수 없다(project root 불변, live Goal에
    typed 외부 효과 승인 경로 없음). 그 두 경계는 구조 관측만 남기고 이 cell을 PASS로
    내지 않는다(target·effect NOT_COVERED_LIVE, PRODUCT). PASS는 공개 EngineApplication
    경로로 target·효과·정책 확장을 모두 live로 관측할 수 있을 때만 가능하다.
    """

    service, project_id, authority = prepared.service, prepared.project_id, prepared.authority
    if authority is None:
        raise QualificationRunError("E2E-15에는 준비 단계의 Core 승인 권위가 필요합니다.")
    application = EngineApplication(
        service, runtime=runtime, role_configuration=roles, governance=governance
    )
    goal = service.load_active_goal(project_id)
    with service.ledger.read() as connection:
        plan = PlanContractRevision.model_validate_json(connection.execute(
            "SELECT payload_json FROM plan_revisions WHERE id=?", (prepared.plan_revision_id,)
        ).fetchone()["payload_json"])
        skeleton = PlanSkeletonCandidate.model_validate_json(connection.execute(
            "SELECT payload_json FROM skeleton_candidates WHERE project_id=? AND candidate_digest=?",
            (project_id, plan.definition.source_skeleton_digest),
        ).fetchone()["payload_json"])
        project_map_root = connection.execute(
            "SELECT payload_json FROM project_map_revisions WHERE revision_digest=?",
            (plan.definition.project_map_digest,),
        ).fetchone()["payload_json"]
        budget = GoalBudgetPolicy.model_validate_json(connection.execute(
            "SELECT payload_json FROM budget_policy_revisions WHERE project_id=? AND scope_key='' "
            "ORDER BY revision_no DESC LIMIT 1",
            (project_id,),
        ).fetchone()["payload_json"])
        project_root = connection.execute(
            "SELECT root FROM projects WHERE id=?", (project_id,)
        ).fetchone()["root"]
    narrow = GoalOperatingPolicy.model_validate({
        "planning_budget": plan.definition.planning_budget.model_copy(update={
            "max_logical_role_calls": plan.definition.planning_budget.max_logical_role_calls - 1,
        }).model_dump(mode="json"),
    })
    steps: list[dict[str, Any]] = []

    def authorize(policy: GoalOperatingPolicy | None) -> dict[str, Any]:
        capability = authority.issue_goal_authorization(
            ledger_path=service.ledger.path,
            target=service.goal_authorization_target(project_id=project_id, operating_policy=policy),
        )
        result = application.authorize(
            project_id,
            source="qualification user authorization",
            operating_policy=policy,
            capability=capability,
        )
        return {
            "authorization": result.authorization.model_dump(mode="json"),
            "activation_id": result.activation_id,
        }

    def run_once() -> dict[str, Any]:
        # run_once만 task gate를 연다. 승인 검사에서 멈춰도 매번 닫는다.
        try:
            outcome = application.run_once(project_id)
        finally:
            application.close_task_gate()
        return {
            "run_once": outcome.model_dump(mode="json"),
            "error": outcome.detail if outcome.action is RunOnceAction.BLOCKED else None,
        }

    def step(name: str, action: Any, expected_changes: list[list[str]] | None) -> dict[str, Any]:
        before = _ledger_rows(service, project_id)
        journal_before = _journal_operation_counts(runtime.events)
        record: dict[str, Any] = {"step": name}
        try:
            record.update(action())
        except EngineServiceError as error:
            record["error"] = str(error)
        record.setdefault("error", None)
        record["authorization_changes"] = _authorization_change_fields(record["error"])
        record["ledger_delta"] = _ledger_delta(before, _ledger_rows(service, project_id))
        journal_after = _journal_operation_counts(runtime.events)
        record["effect_count"] = {
            key: journal_after[key] - journal_before[key] for key in _ZERO_EFFECTS
        }
        record["expected_authorization_changes"] = expected_changes
        record["passed"] = (
            record["authorization_changes"] == expected_changes
            and record["effect_count"] == _ZERO_EFFECTS
            and not set(record["ledger_delta"]) & _EXECUTION_TABLES
            and "provider_calls" not in record["ledger_delta"]
        )
        steps.append(record)
        return record

    budget_change = [["policy", f"budget.effective.{goal.goal_id}"]]
    # A: live Plan의 정책보다 좁게 승인하면 활성화되지 않고 승인 기록도 남지 않는다.
    narrow_step = step(
        "A_narrow_authorization",
        lambda: authorize(narrow),
        [["policy", "planning_budget.max_logical_role_calls"]],
    )
    narrow_step["passed"] = narrow_step["passed"] and not narrow_step["ledger_delta"]
    # B: 내부 Plan ID만으로는 승인 없이 활성화되지 않는다.
    id_step = step(
        "B_internal_plan_id_activation",
        lambda: {"activation_id": service.activate_authorized_plan(
            plan_revision_id=plan.plan_revision_id
        )},
        [["goal", "authorization"]],
    )
    id_step["passed"] = id_step["passed"] and not id_step["ledger_delta"]
    # C: 새 사용자 판단(기본 정책 승인)이면 같은 Plan이 활성화된다.
    approved = step("C_user_authorization", lambda: authorize(None), None)
    approved["passed"] = approved["passed"] and (
        len(approved["ledger_delta"].get("goal_authorizations", {}).get("added", [])) == 1
        and len(approved["ledger_delta"].get("plan_activations", {}).get("added", [])) == 1
    )
    if approved["passed"]:
        _write_json(
            cell_root / "plan-activation-receipt.json",
            {
                "schema": "flowmarshal.project-e2e.plan-activation-receipt.v2",
                "evaluation_contract_digest": evaluation_contract_digest,
                "fixture_digest": fixture_digest,
                "source": "EngineApplication.authorize (E2E-15 C_user_authorization)",
                "authorization_id": approved["authorization"]["authorization_id"],
                "activation_id": approved["activation_id"],
                "plan_revision_id": plan.plan_revision_id,
                "activation_digest": plan.activation_digest,
            },
        )
        # D: 승인 뒤 사용자 예산 설정(project budget set)만 넓혀도 실행이 멈춘다.
        # 설정 변경 자체는 run_once 판정과 따로 원장 증가를 남긴다.
        setup_before = _ledger_rows(service, project_id)
        BudgetManager(service).configure(
            project_id,
            budget.model_copy(update={"total_tokens": budget.total_tokens * 2}),
            goal_id=goal.goal_id,
        )
        budget_setup = _ledger_delta(setup_before, _ledger_rows(service, project_id))
        blocked = step("D_budget_expansion_run_once", run_once, budget_change)
        blocked["budget_setup_ledger_delta"] = budget_setup
        blocked["passed"] = (
            blocked["passed"]
            and not blocked["ledger_delta"]
            and set(budget_setup) <= {"budget_policy_revisions", "history_events"}
            and [
                row["scope_key"]
                for row in budget_setup.get("budget_policy_revisions", {}).get("added", [])
            ] == [goal.goal_id]
            and not budget_setup["budget_policy_revisions"]["changed"]
            and _history_types(budget_setup) is not None
        )
        # E: Core 선택 Plan 재활성화(내부 ID)로는 풀리지 않는다.
        reselected = step(
            "E_selected_plan_reactivation",
            lambda: {
                "activation_id": service.activate_selected_plan(project_id=project_id),
                **run_once(),
            },
            budget_change,
        )
        reselected["passed"] = (
            reselected["passed"]
            and not reselected["ledger_delta"]
            and reselected.get("activation_id") == approved["activation_id"]
        )

        # F: 새 사용자 승인이 확장된 예산을 받아들이면 같은 활성 Plan이 다시 승인된다.
        def reauthorize() -> dict[str, Any]:
            result = authorize(None)
            service.assert_project_authorized(project_id)
            return result

        renewed = step("F_user_reauthorization", reauthorize, None)
        renewed["passed"] = (
            renewed["passed"]
            and set(renewed["ledger_delta"]) == {"goal_authorizations", "history_events"}
            and len(renewed["ledger_delta"]["goal_authorizations"]["added"]) == 1
            and _history_types(renewed["ledger_delta"]) == ["goal.authorized"]
            and renewed.get("activation_id") == approved["activation_id"]
        )
    authorization_fields = sorted(GoalAuthorization.model_fields)
    structure = {
        "authorization_project_root": (
            None if not approved.get("authorization") else approved["authorization"]["project_root"]
        ),
        "project_root": str(Path(project_root).resolve()),
        "plan_project_map_root": json.loads(project_map_root)["root"],
        "goal_typed_external_effect_contracts": len(
            goal.definition.effect_policy.allowed_external_effect_contracts
        ),
        "plan_external_effect_count": sum(
            effect.external for task in plan.definition.tasks for effect in task.expected_effects
        ),
        "note": (
            "live 1.0 경로는 target·효과 확장 후보를 만들 수 없다. 이 값은 구조 관측이며 "
            "확장 거절 증거가 아니다."
        ),
    }
    cost = {
        "skeleton_estimated_change_cost": skeleton.estimated_change_cost,
        "skeleton_estimated_context_tokens": skeleton.estimated_context_tokens,
        "authorization_fields": authorization_fields,
        "authorization_has_plan_or_cost_field": any(
            word in field
            for field in authorization_fields
            for word in ("plan", "cost", "estimate")
        ),
    }
    expected_steps = [
        "A_narrow_authorization",
        "B_internal_plan_id_activation",
        "C_user_authorization",
        "D_budget_expansion_run_once",
        "E_selected_plan_reactivation",
        "F_user_reauthorization",
    ]
    observed_steps = [item["step"] for item in steps]
    policy_problems = [
        *(f"{item['step']}:failed" for item in steps if not item["passed"]),
        *(f"{name}:not_run" for name in expected_steps if name not in observed_steps),
        *(["authorization_has_plan_or_cost_field"] if cost["authorization_has_plan_or_cost_field"] else []),
    ]
    policy_passed = observed_steps == expected_steps and not policy_problems
    coverage = {
        "covered_live": ["policy"],
        "not_covered_live": ["target", "effect"],
        "policy_boundary_passed": policy_passed,
    }
    _write_json(
        cell_root / "authorization-observation.json",
        {
            "schema": "flowmarshal.project-e2e.authorization-observation.v2",
            "cell_id": "scope-expansion",
            "narrow_operating_policy": narrow.model_dump(mode="json"),
            "authorizations": [
                item["authorization"] for item in steps if item.get("authorization")
            ],
        },
    )
    _write_json(
        cell_root / "scope-check.json",
        {
            "schema": "flowmarshal.project-e2e.scope-check.v2",
            "cell_id": "scope-expansion",
            "plan_revision_id": plan.plan_revision_id,
            "plan_planning_budget": plan.definition.planning_budget.model_dump(mode="json"),
            "measured_tables": sorted(_LEDGER_ROWS),
            "steps": steps,
            "structure": structure,
            "cost_estimate": cost,
            "coverage": coverage,
        },
    )
    # 둘 다 제품 gap(PRODUCT)이다. 정책 회귀가 있으면 그것을 주 code로 두고 미커버 범위는
    # coverage에 따로 남긴다. 진짜 PASS는 공개 경로로 target·효과·정책을 모두 live로 볼 때만이다.
    if policy_passed:
        failure = _cell_failure(
            "E2E15_TARGET_EFFECT_NOT_COVERED_LIVE",
            "live 경로로 target·효과 확장 후보를 만들 수 없어 정책 경계만 live로 관측했다.",
        )
    else:
        failure = _cell_failure(
            "E2E15_POLICY_BOUNDARY_REGRESSION",
            "정책 경계 단계 실패: " + ",".join(policy_problems)
            + "; target·효과는 live로 관측하지 못했다.",
        )
    return {
        "passed": False,
        "source_fixture_digest": source_digest,
        **failure,
        "coverage": coverage,
        "steps": [
            {key: item[key] for key in ("step", "passed", "error", "authorization_changes")}
            for item in steps
        ],
        "effect_count": {
            key: sum(item["effect_count"][key] for item in steps) for key in _ZERO_EFFECTS
        },
    }


def _complete_task_without_goal_verdict(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    *,
    roles: EngineRoleConfiguration | None,
    governance: Any | None,
    timeout_seconds: float,
) -> dict[str, Any]:
    provider = _execution_proposal_provider(prepared, runtime, roles)
    if provider is None and prepared.proposal is None:
        raise QualificationRunError("E2E_EXECUTION_PROPOSAL_PROVIDER_REQUIRED")
    task_gate = _open_e2e_task_gate(prepared, runtime, roles, governance)
    try:
        dispatcher = EngineDispatcher(
            prepared.service,
            runtime,
            proposal_provider=provider,
            task_gate=task_gate,
        )
        deadline = time.monotonic() + timeout_seconds
        actions: list[str] = []
        while time.monotonic() < deadline:
            outcome = dispatcher.run_once(
                prepared.project_id,
                proposal=prepared.proposal if provider is None else None,
            )
            actions.append(outcome.action.value)
            with prepared.service.ledger.read() as connection:
                status = connection.execute(
                    "SELECT status FROM task_contracts WHERE id=?",
                    (prepared.task_id,),
                ).fetchone()[0]
            if status == "completed":
                return {"passed": True, "actions": actions, "task_status": status}
            if outcome.action is RunOnceAction.BLOCKED:
                return {
                    "passed": False,
                    "actions": actions,
                    "task_status": status,
                    "failure": f"{outcome.blocker_code}: {outcome.detail}",
                }
            time.sleep(0.05)
        return {
            "passed": False,
            "actions": actions,
            "task_status": None,
            "failure": "Task completion timeout",
        }
    finally:
        task_gate.close()


def _in_flight_replan_protection(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """무효 evidence 재사용 거부와 active Attempt의 Plan 교체 보류를 함께 증명한다."""

    completed = _complete_task_without_goal_verdict(
        prepared,
        runtime,
        roles=roles,
        governance=governance,
        timeout_seconds=timeout_seconds,
    )
    if not completed.get("passed"):
        return {**completed, "passed": False}
    prepared.service.reobserve_project(prepared.project_id, force_state_revision=True)
    revision, task = _register_equivalent_plan_revision(prepared)
    with prepared.service.ledger.read() as connection:
        latest = connection.execute(
            "SELECT payload_json FROM validation_results WHERE task_id=? "
            "ORDER BY evaluated_at DESC,rowid DESC LIMIT 1",
            (prepared.task_id,),
        ).fetchone()
    prior = ValidationResult.model_validate_json(latest["payload_json"])
    invalidating_result = ValidationResult(
        validation_result_id=new_id("validation_result"),
        validation_id=prior.validation_id,
        task_id=prepared.task_id,
        status=ValidationStatus.FAIL,
        evidence_ids=prior.evidence_ids,
        rationale="qualification fault: 완료 뒤 새 관측이 기존 evidence 유효성을 부정함",
        evaluated_at=utc_now(),
    )
    prepared.service.record_validation(
        project_id=prepared.project_id,
        plan_revision_id=prepared.plan_revision_id,
        result=invalidating_result,
    )
    first_activation_id = prepared.service.activate_authorized_plan(
        plan_revision_id=revision.plan_revision_id
    )
    with prepared.service.ledger.read() as connection:
        reuse = connection.execute(
            "SELECT payload_json FROM history_events "
            "WHERE entity_id=? AND event_type='task.completion_reuse_rejected' "
            "ORDER BY sequence DESC LIMIT 1",
            (task.task_id,),
        ).fetchone()
        task_status = connection.execute(
            "SELECT status FROM task_contracts WHERE id=?", (task.task_id,)
        ).fetchone()[0]
    current_map = prepared.service.load_current_project_map(prepared.project_id)
    current_digests = {item.path: item.content_digest for item in current_map.entries}
    next_proposal = (
        None
        if prepared.proposal is None
        else prepared.proposal.model_copy(update={
            "task_id": task.task_id,
            "resolved_targets": tuple(
                item.model_copy(update={
                    "expected_content_digest": current_digests.get(item.path)
                })
                for item in prepared.proposal.resolved_targets
            ),
        })
    )
    next_prepared = PreparedE2E(
        service=prepared.service,
        project_id=prepared.project_id,
        task_id=task.task_id,
        plan_revision_id=revision.plan_revision_id,
        activation_digest=revision.activation_digest,
        proposal=next_proposal,
        workspace=prepared.workspace,
        preparation_provenance=prepared.preparation_provenance,
        pipeline_stages=prepared.pipeline_stages,
        preparation_evidence_refs=prepared.preparation_evidence_refs,
    )
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=RuntimeJobSupervisor(
            prepared.service, runtime, observation_timeout_seconds=5.0
        ),
    )
    activation_id: str | None = None
    activation_error: str | None = None
    activation_exception: EngineServiceError | None = None
    stale_activation_error: str | None = None
    fresh_replacement: PlanContractRevision | None = None
    attempt_status: str | None = None
    settlement_actions: list[str] = []
    terminal_observation: Any | None = None
    quiescence_before: dict[str, int] | None = None
    quiescence_after: dict[str, int] | None = None
    quiescence_effects_before: dict[str, int] | None = None
    quiescence_effects_after: dict[str, int] | None = None
    try:
        _materialize_with_application(application, next_prepared, timeout_seconds=timeout_seconds)
        dispatched = application.run_once(prepared.project_id)
        if dispatched.action is not RunOnceAction.DISPATCHED or dispatched.attempt_id is None:
            raise QualificationRunError("E2E-17 active execution Attempt를 시작하지 못했습니다.")
        binding, _job_id = _await_active_execution_binding(
            next_prepared, dispatched.attempt_id, timeout_seconds=timeout_seconds
        )
        replacement, _replacement_task = _register_equivalent_plan_revision(next_prepared)
        before = _replacement_state(prepared.service, prepared.project_id)
        error: str | None = None
        try:
            prepared.service.activate_authorized_plan(
                plan_revision_id=replacement.plan_revision_id
            )
        except EngineServiceError as caught:
            error = str(caught)
        after = _replacement_state(prepared.service, prepared.project_id)

        quiescence_before = _journal_operation_counts(runtime.events)
        quiescence_effects_before = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        terminal_observation = _await_terminal_after_fault(
            runtime, binding, timeout_seconds=timeout_seconds
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with prepared.service.ledger.read() as connection:
                attempt_status = connection.execute(
                    "SELECT status FROM attempts WHERE id=?", (dispatched.attempt_id,)
                ).fetchone()[0]
            if attempt_status in {"interrupted", "failed", "succeeded"}:
                break
            settled = application.run_once(prepared.project_id)
            settlement_actions.append(settled.action.value)
            time.sleep(0.05)
        quiescence_after = _journal_operation_counts(runtime.events)
        quiescence_effects_after = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        if attempt_status in {"interrupted", "failed", "succeeded"}:
            while time.monotonic() < deadline and activation_id is None:
                try:
                    activation_id = prepared.service.activate_authorized_plan(
                        plan_revision_id=replacement.plan_revision_id
                    )
                except EngineServiceError as caught:
                    activation_exception = caught
                    activation_error = str(caught)
                    if not activation_error.startswith("PLAN_REPLACEMENT_IN_FLIGHT:"):
                        break
                    settled = application.run_once(prepared.project_id)
                    settlement_actions.append(settled.action.value)
                    time.sleep(0.05)
            if activation_id is not None:
                activation_error = None
            elif isinstance(activation_exception, PlanStateSnapshotStaleError):
                stale_activation_error = activation_error
                prepared.service.reobserve_project(
                    prepared.project_id, force_state_revision=True
                )
                fresh_replacement, _fresh_task = _register_equivalent_plan_revision(
                    next_prepared,
                    base_plan_revision_id=replacement.plan_revision_id,
                )
                try:
                    activation_id = prepared.service.activate_authorized_plan(
                        plan_revision_id=fresh_replacement.plan_revision_id
                    )
                except EngineServiceError as caught:
                    activation_error = str(caught)
                else:
                    activation_error = None
    finally:
        application.close_task_gate()
    reuse_payload = None if reuse is None else json.loads(reuse["payload_json"])
    activation_calls: list[dict[str, Any]] = [
        {
            "plan_revision_id": revision.plan_revision_id,
            "activation_id": first_activation_id,
            "purpose": "observe_invalid_reuse",
        },
        {
            "plan_revision_id": replacement.plan_revision_id,
            "error": error,
            "purpose": "reject_while_attempt_active",
        },
    ]
    if stale_activation_error is not None:
        activation_calls.append({
            "plan_revision_id": replacement.plan_revision_id,
            "activation_id": None,
            "error": stale_activation_error,
            "purpose": "reject_stale_revision_after_attempt_quiescence",
        })
    if fresh_replacement is not None:
        activation_calls.append({
            "plan_revision_id": fresh_replacement.plan_revision_id,
            "activation_id": activation_id,
            "error": activation_error,
            "purpose": "activate_fresh_revision_after_attempt_quiescence",
        })
    fault = _write_fault_injection(
        cell_root / "fault-injection.json",
        {
            "kind": "equivalent_revision_with_invalidated_completion_evidence",
            "replan_source": "qualification_fixture_equivalent_revision",
            "proves_product_replan_pipeline": False,
            "deferred_responsibility_ids": ["E2E-05", "E2E-14", "E2E-15"],
            "source_task_id": prepared.task_id,
            "invalidating_validation_result_id": (
                invalidating_result.validation_result_id
            ),
            "equivalent_plan_revision_id": revision.plan_revision_id,
            "replacement_plan_revision_id": replacement.plan_revision_id,
            "fresh_replacement_plan_revision_id": (
                None
                if fresh_replacement is None
                else fresh_replacement.plan_revision_id
            ),
            "activation_calls": activation_calls,
        },
    )
    passed = all((
        task_status == "ready",
        reuse_payload is not None,
        reuse_payload is not None
        and reuse_payload.get("reason") == "EVIDENCE_INVALID_OR_INSUFFICIENT",
        error is not None and error.startswith("PLAN_REPLACEMENT_IN_FLIGHT:"),
        before == after,
        binding.thread_id is not None and binding.turn_id is not None,
        attempt_status == "succeeded",
        activation_error is None,
        bool(activation_id),
        quiescence_before is not None,
        quiescence_after is not None,
        quiescence_before is not None
        and quiescence_after is not None
        and all(
            quiescence_after[key] == quiescence_before[key]
            for key in ("create_thread", "start_turn", "resume", "interrupt")
        ),
        quiescence_effects_before is not None,
        quiescence_effects_after is not None,
        quiescence_effects_before is not None
        and quiescence_effects_after is not None
        and all(
            quiescence_effects_after[key] == quiescence_effects_before[key]
            for key in (
                "provider_calls",
                "runtime_intents",
                "runtime_receipts",
                "distinct_provider_operation_ids",
            )
        ),
    ))
    return {
        "passed": passed,
        "source_fixture_digest": source_digest,
        "attempt": {
            "attempt_id": dispatched.attempt_id,
            "status_after_cleanup": attempt_status,
        },
        "binding": binding.model_dump(mode="json"),
        "fault_injection": fault,
        "plan": {
            "blocked_revision_id": replacement.plan_revision_id,
            "fresh_revision_id": (
                None
                if fresh_replacement is None
                else fresh_replacement.plan_revision_id
            ),
            "active_before": before["project"]["active_plan_revision_id"],
            "activation_id_after_quiescence": activation_id,
        },
        "reuse_decision": reuse_payload,
        "in_flight_error": error,
        "state_unchanged_during_rejection": before == after,
        "quiescence": {
            "settlement_actions": settlement_actions,
            "terminal_observation": (
                None
                if terminal_observation is None
                else terminal_observation.model_dump(mode="json")
            ),
            "activation_error": activation_error,
            "operation_counts_before": quiescence_before,
            "operation_counts_after": quiescence_after,
            "attempt_effect_counts_before": quiescence_effects_before,
            "attempt_effect_counts_after": quiescence_effects_after,
            "automatic_resume_occurred": bool(
                quiescence_before is not None
                and quiescence_after is not None
                and quiescence_after["resume"] != quiescence_before["resume"]
            ),
        },
    }


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
                "scope": "collector callback forwarding only",
                "not_proven": ("operating_system_process_termination",),
                "injected_at": utc_now().isoformat(),
                "job_id": job_id,
                "attempt_id": dispatched.attempt_id,
                "thread_id": binding.thread_id,
                "turn_id": binding.turn_id,
                "pre_fault_observation_digest": sha256_digest(pre_fault),
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
        repeated_tick_outcomes = tuple(
            restarted_application.run_once(prepared.project_id)
            for _ in range(3)
        )
        effect_counts_after_repeated_ticks = _attempt_effect_counts(
            prepared, dispatched.attempt_id
        )
        restart_counts = _journal_operation_counts(runtime.events[journal_offset:])

        with prepared.service.ledger.read() as connection:
            observed_job = connection.execute(
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
            observed_job is not None
            and observed_job["thread_id"] == binding.thread_id
            and observed_job["turn_id"] == binding.turn_id
        )
        observation_window_passed = all(
            (
                lost.status.value == "collector_lost",
                observed_job is not None and observed_job["status"] == "running",
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
                effect_counts_after_repeated_ticks == effect_counts_before,
                provider_call is not None,
                all(
                    outcome.action is RunOnceAction.OBSERVED
                    for outcome in repeated_tick_outcomes
                ),
            )
        )

        # qualification evidence를 봉인한 뒤에도 live turn이나 재개 가능한 workflow를
        # 남겨 두지 않는다. 정리는 중복 방지 관측 창과 분리한다.
        cleanup_offset = len(runtime.events)
        restarted_supervisor.request_interrupt(
            job_id, reason="qualification_cell_cleanup"
        )
        cleanup_deadline = time.monotonic() + timeout_seconds
        cleanup_statuses = []
        cleanup_job = prepared.service.load_runtime_job(job_id)
        cleanup_attempt = attempt
        while time.monotonic() < cleanup_deadline:
            cleanup_job = restarted_supervisor.tick(job_id)
            cleanup_statuses.append(cleanup_job.status.value)
            with prepared.service.ledger.read() as connection:
                cleanup_attempt = connection.execute(
                    "SELECT status,failure_class FROM attempts WHERE id=?",
                    (dispatched.attempt_id,),
                ).fetchone()
            if cleanup_job.status.value == "provider_terminal":
                break
            time.sleep(0.05)
        terminal_cleanup_job = cleanup_job
        cleanup_control = restarted_application.cancel(
            prepared.project_id,
            reason="qualification cell cleanup after sealed observation window",
        )
        cleanup_observe = restarted_application.observe(prepared.project_id)
        cleanup_job = prepared.service.load_runtime_job(job_id)
        with prepared.service.ledger.read() as connection:
            cleanup_attempt = connection.execute(
                "SELECT status,failure_class FROM attempts WHERE id=?",
                (dispatched.attempt_id,),
            ).fetchone()
        cleanup_counts = _journal_operation_counts(runtime.events[cleanup_offset:])
        cleanup_passed = all(
            (
                terminal_cleanup_job.status.value == "provider_terminal",
                terminal_cleanup_job.provider_terminal_status is not None,
                cleanup_control["control_state"] == "cancelled",
                cleanup_job.status.value == "consumed",
                cleanup_attempt is not None,
                cleanup_attempt["status"] == "interrupted",
                cleanup_counts["create_thread"] == 0,
                cleanup_counts["start_turn"] == 0,
                cleanup_counts["resume"] == 0,
                cleanup_counts["interrupt"] in {0, 1},
            )
        )
        passed = observation_window_passed and cleanup_passed
        return {
            "passed": passed,
            "source_fixture_digest": source_digest,
            "binding": binding.model_dump(mode="json"),
            "fault_injection": fault,
            "runtime_job": None if observed_job is None else dict(observed_job),
            "runtime_observation": [dict(row) for row in observations],
            "restart_journal": runtime.events[journal_offset:cleanup_offset],
            "exact_binding_preserved": exact_binding_preserved,
            "repeated_tick_actions": tuple(
                outcome.action.value for outcome in repeated_tick_outcomes
            ),
            "effect_count": restart_counts,
            "ledger": {
                "attempt_id": dispatched.attempt_id,
                "attempt_status": None if attempt is None else attempt["status"],
                "failure_class": None if attempt is None else attempt["failure_class"],
                "effect_counts_before": effect_counts_before,
                "effect_counts_after": effect_counts_after_repeated_ticks,
                "provider_call": None if provider_call is None else dict(provider_call),
                "provider_call_counter_semantics": "new_turn_count is settled turns, not started turns",
            },
            "cleanup": {
                "passed": cleanup_passed,
                "terminal_job": terminal_cleanup_job.model_dump(mode="json"),
                "final_job": cleanup_job.model_dump(mode="json"),
                "control": cleanup_control,
                "final_observe": cleanup_observe,
                "attempt_status": (
                    None if cleanup_attempt is None else cleanup_attempt["status"]
                ),
                "failure_class": (
                    None if cleanup_attempt is None else cleanup_attempt["failure_class"]
                ),
                "effect_count": cleanup_counts,
                "journal": runtime.events[cleanup_offset:],
                "job_statuses": tuple(cleanup_statuses),
                "post_cell_reuse_policy": "workflow_cancelled_and_attempt_terminal",
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

    clock_state = {"override": None}
    supervisor = RuntimeJobSupervisor(
        prepared.service,
        runtime,
        observation_timeout_seconds=5.0,
        clock=lambda: clock_state["override"] or utc_now(),
    )
    fault_path = runtime.journal.parent / "fault-injection.json"
    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        governance=governance,
        supervisor=supervisor,
    )
    if prepared.proposal is None and roles is None:
        raise QualificationRunError("absolute-timeout E2E에는 execution proposal provider가 필요합니다.")

    try:
        outcome = application.run_once(
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
            outcome = application.run_once(prepared.project_id)
        if outcome.action is not RunOnceAction.MATERIALIZED:
            raise QualificationRunError("absolute-timeout E2E execution spec materialize timeout")

        dispatched = application.run_once(prepared.project_id)
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
        clock_state["override"] = observed_deadline_at
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
                "clock_before_fault": "utc_now",
                "terminal_observation_grace_exercised": False,
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
            last_outcome = application.run_once(prepared.project_id)
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
        blocked = application.run_once(prepared.project_id)
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
                all(
                    effect_counts_after[key] == effect_counts_before[key]
                    for key in (
                        "provider_calls",
                        "runtime_intents",
                        "runtime_receipts",
                        "distinct_provider_operation_ids",
                    )
                ),
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
            "supervisor_reused_across_deadline": True,
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
        application.close_task_gate()


_READ_ONLY_KNOWN_LIMITATION = (
    "제품 read_only source_unchanged는 baseline ProjectMap이 관측한 경로만 다시 읽으므로 "
    "baseline 밖에 새로 생긴 파일을 보지 못한다. 이 cell은 driver의 실행 전후 workspace "
    "전체 트리 digest로 그 빈틈을 덮는다. 제품 new-file detection은 후속 과제다. "
    "전체 트리 digest도 Project Map 기본 무시 디렉터리(DEFAULT_IGNORED_DIRECTORIES: VCS·가상환경·"
    "node_modules·캐시·dist·build·Engine artifact 경로) 안의 변경은 보지 않는다."
)


def _read_only_report(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    """E2E-03: read_only Goal이 프로젝트를 바꾸지 않고 AC별 보고까지 끝나는지 본다.

    무변경 증명은 제품 `source_unchanged`만 믿지 않고 실행 전후 workspace 전체 트리
    digest(`.git`·Engine 무시 경로 제외)로 새 파일까지 비교한다. 제품 검사의 새 파일
    누락은 `_READ_ONLY_KNOWN_LIMITATION`에 적은 known limitation이다.
    """

    report_path = cell_root / "final-report.json"
    # 책임 판정은 evidence 파일이 있어야 돈다. 실패 경로를 위해 먼저 null과 이유를 남긴다.
    _write_json(
        report_path,
        {
            "schema": "flowmarshal.project-e2e.final-report.v1",
            "final_report": None,
            "reason": "E2E03_FINAL_REPORT_NOT_PRODUCED",
        },
    )
    goal = prepared.service.load_active_goal(prepared.project_id)
    with prepared.service.ledger.read() as connection:
        plan_row = connection.execute(
            "SELECT payload_json FROM plan_revisions WHERE id=?",
            (prepared.plan_revision_id,),
        ).fetchone()
    plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
    precondition = {
        "mutation_policy": goal.definition.effect_policy.mutation_policy.value,
        "change_task_refs": [
            task.task_ref for task in plan.definition.tasks if task.kind is TaskKind.CHANGE
        ],
    }
    if (
        goal.definition.effect_policy.mutation_policy is not MutationPolicy.READ_ONLY
        or precondition["change_task_refs"]
    ):
        # live 정규화·계획이 무변경 Goal을 만들지 않았다. 실행하지 않고 model로 남긴다.
        return {
            "passed": False,
            "source_fixture_digest": source_digest,
            "precondition": precondition,
            "failure": f"E2E03_PRECONDITION_NOT_READ_ONLY: {precondition}",
            "failure_code": "E2E03_PRECONDITION_NOT_READ_ONLY",
            "failure_class": QualificationFailureClass.MODEL.value,
        }
    before = _workspace_tree_digest(prepared.workspace)
    completion = _normal_completion(
        prepared,
        runtime,
        source_digest,
        roles=roles,
        governance=governance,
        required_files=("app.py", "test_app.py"),
    )
    report = None
    if completion.get("passed"):
        report = EngineApplication(prepared.service).final_report(prepared.project_id)
        _write_json(
            report_path,
            {
                "schema": "flowmarshal.project-e2e.final-report.v1",
                "final_report": report.model_dump(mode="json"),
                "reason": None,
            },
        )
    after = _workspace_tree_digest(prepared.workspace)
    changed_paths = _workspace_tree_changes(before, after)
    verification = None if report is None else report.read_only_verification
    checks = {
        "completed_with_goal_verdict": bool(completion.get("passed")),
        "read_only_verification_present": verification is not None,
        "criteria_complete": verification is not None and verification.criteria_complete,
        "evidence_grounded": verification is not None and verification.evidence_grounded,
        "product_source_unchanged": verification is not None and verification.source_unchanged,
        "workspace_tree_unchanged": not changed_paths,
    }
    result = {
        **completion,
        "passed": all(checks.values()),
        "precondition": precondition,
        "checks": checks,
        "source_digest": {
            "workspace_tree_before": before["tree_digest"],
            "workspace_tree_after": after["tree_digest"],
            "file_count_before": before["file_count"],
            "file_count_after": after["file_count"],
            "changed_paths": changed_paths,
        },
        "read_only_verification": (
            None if verification is None else verification.model_dump(mode="json")
        ),
        # read_only 검사가 통과하면 final report error_code는 usage 상태 code로 채워질
        # 수 있다. usage 누락은 E2E-03 판정이 아니므로 감사용으로만 남긴다.
        "report_error_code": None if report is None else report.error_code,
        "known_limitation": _READ_ONLY_KNOWN_LIMITATION,
    }
    if not result["passed"] and completion.get("passed"):
        failed = ",".join(name for name, ok in checks.items() if not ok)
        result.update(
            _cell_failure(
                "E2E03_WORKSPACE_TREE_CHANGED"
                if changed_paths
                else "E2E03_READ_ONLY_REPORT_VERIFICATION_FAILED",
                f"failed_checks=[{failed}], changed_paths={changed_paths}",
            )
        )
    return result


class _RuntimeObservationLayer:
    """Engine → layer → RecordedRuntime 사이에서 provider 관측만 다루는 fault layer의 공통 위임부.

    journal에는 provider 원문이 그대로 남는다. 하위 class는 `_observe`에서 Engine에 넘길
    관측만 바꾸거나, 관측을 넘기기 직전에 통제된 로컬 효과를 낸다. Worker 실행 thread는
    Engine이 붙이는 제목("FlowMarshal <task_ref> execution")으로 구분한다.
    """

    def __init__(self, runtime: RecordedRuntime) -> None:
        self.runtime = runtime
        self.requires_budget_policy = getattr(runtime, "requires_budget_policy", False)
        self.worker_thread_ids: set[str] = set()
        if callable(getattr(runtime, "register_completion_observer", None)):
            # EngineDispatcher는 getattr로 capability를 탐지하므로 아래 runtime이
            # 지원할 때만 public attribute를 노출한다.
            self.register_completion_observer = self._register_completion_observer

    def _observe(self, observation: Any, *, operation: str) -> Any:
        return observation

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.runtime.events

    @property
    def create_calls(self) -> int:
        return self.runtime.create_calls

    @property
    def turn_calls(self) -> int:
        return self.runtime.turn_calls

    @property
    def read_calls(self) -> int:
        return self.runtime.read_calls

    @property
    def resume_calls(self) -> int:
        return self.runtime.resume_calls

    def _register_completion_observer(
        self, *, thread_id: str, turn_id: str, observer: Any
    ) -> None:
        def observe_and_forward(observation: Any) -> None:
            observer(self._observe(observation, operation="completion_observation"))

        self.runtime.register_completion_observer(
            thread_id=thread_id, turn_id=turn_id, observer=observe_and_forward
        )

    def sever_completion_forwarding(self) -> None:
        self.runtime.sever_completion_forwarding()

    def verify_execution_policy(self, cwd: Path | str) -> Any:
        return self.runtime.verify_execution_policy(cwd)

    def list_models(self) -> ModelInventory:
        return self.runtime.list_models()

    def create_thread(self, **arguments: Any) -> Any:
        receipt = self.runtime.create_thread(**arguments)
        # Worker 실행 thread 제목은 "FlowMarshal <task_ref> execution"이다. 역할·검사 thread는 가리지 않는다.
        if str(arguments.get("title", "")).endswith(" execution"):
            self.worker_thread_ids.add(
                receipt.binding.thread_id if receipt.binding is not None else receipt.operation_id
            )
        return receipt

    def start_turn(self, **arguments: Any) -> Any:
        return self.runtime.start_turn(**arguments)

    def read(self, **arguments: Any) -> Any:
        return self._observe(self.runtime.read(**arguments), operation="read")

    def read_stored(self, **arguments: Any) -> Any:
        return self._observe(self.runtime.read_stored(**arguments), operation="read_stored")

    def resume(self, **arguments: Any) -> Any:
        return self.runtime.resume(**arguments)

    def interrupt(self, **arguments: Any) -> Any:
        return self.runtime.interrupt(**arguments)

    def close(self) -> None:
        self.runtime.close()


class UsageWithholdingRuntime(_RuntimeObservationLayer):
    """E2E-06 fault layer. Worker 실행 turn의 provider usage만 Engine 관측에서 뺀다.

    journal에는 provider가 준 원문이 그대로 남고 Engine에 넘기는 관측 payload에서만
    usage를 뺀다. 뺀 원문은 보관했다가 driver가 같은 프로세스에서 늦은 usage로 다시
    전달한다. Claude는 저장된 turn(`read_stored`)에서 usage를 주지 않으므로 프로세스가
    끝난 뒤에는 늦은 usage를 다시 받을 길이 없다.
    """

    _USAGE_KEYS = (
        "usage",
        "usage_scope",
        "usage_scope_basis",
        "provider_usage_raw",
        "provider_model_usage",
    )
    WITHHELD_USAGE_SOURCE = "qualification_fault_injector:usage_withheld"

    def __init__(self, runtime: RecordedRuntime) -> None:
        super().__init__(runtime)
        self.withheld: list[dict[str, Any]] = []

    def _withheld_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Engine에 넘길 usage 없는 payload다. 0·예약량·추정값으로 채우지 않는다."""

        return {
            key: value for key, value in payload.items() if key not in self._USAGE_KEYS
        } | {
            "usage": None,
            "usage_scope": "unavailable",
            "usage_source": self.WITHHELD_USAGE_SOURCE,
        }

    def _observe(self, observation: Any, *, operation: str) -> Any:
        original = observation.model_dump(mode="json")["payload"]
        if (
            observation.thread_id not in self.worker_thread_ids
            or not isinstance(original, dict)
            or original.get("usage") is None
        ):
            return observation
        self.withheld.append(
            {
                "operation": operation,
                "thread_id": observation.thread_id,
                "turn_id": observation.turn_id,
                "terminal_status": observation.terminal_status,
                "withheld_keys": [key for key in self._USAGE_KEYS if key in original],
                "provider_payload": original,
                "provider_payload_digest": sha256_digest(original),
            }
        )
        return observation.model_copy(update={"payload": self._withheld_payload(original)})


class ImplementationFaultRuntime(_RuntimeObservationLayer):
    """E2E-04 fault layer. 첫 Worker 구현을 Engine이 관측하기 직전에 쓰기 target 원본으로 되돌린다.

    Worker turn의 첫 completed terminal 관측을 Engine에 넘기기 전에 그 Attempt 명세의
    쓰기(`write`) target을 driver 시작 때 저장한 원본 bytes로 되돌린다. Engine은 terminal을
    받은 뒤에야 Worker 결과 file evidence를 읽으므로 되돌린 내용이 곧 Worker 결과
    `after_digest`가 된다. 그래서 governance 사용자 변경 검사와 repair의
    `REPAIR_INPUT_CHANGED`에 걸리지 않고 결정적 검사가 구현 결함으로 실패한다.
    주입 기록은 `fault-injection.json` provenance일 뿐 원장에 쓰지 않으며 분류기 입력이 아니다.
    주입은 구현 재시도가 허용된 Task의 Attempt에 한 번만 한다.
    """

    def __init__(
        self,
        runtime: RecordedRuntime,
        *,
        service: EngineService,
        workspace: Path,
        eligible_task_ids: frozenset[str],
    ) -> None:
        super().__init__(runtime)
        self.service = service
        self.workspace = workspace
        self.eligible_task_ids = eligible_task_ids
        self.baseline = _workspace_files(workspace)
        self.injection: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def _observe(self, observation: Any, *, operation: str) -> Any:
        if (
            observation.thread_id in self.worker_thread_ids
            and observation.terminal_status == "completed"
        ):
            with self._lock:
                if self.injection is None:
                    self.injection = self._inject(observation, operation=operation)
        return observation

    def _inject(self, observation: Any, *, operation: str) -> dict[str, Any] | None:
        with self.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT id,task_id,attempt_no,execution_spec_digest FROM attempts "
                "WHERE kind='execution' AND json_extract(binding_json,'$.thread_id')=? "
                "ORDER BY rowid DESC LIMIT 1",
                (observation.thread_id,),
            ).fetchone()
            spec = None if attempt is None else connection.execute(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE task_id=? AND definition_digest=?",
                (attempt["task_id"], attempt["execution_spec_digest"]),
            ).fetchone()
        if attempt is not None and attempt["task_id"] not in self.eligible_task_ids:
            # 구현 재시도가 허용되지 않은 Task는 건드리지 않고 다음 Attempt를 기다린다.
            return None
        record: dict[str, Any] = {
            "operation": operation,
            "thread_id": observation.thread_id,
            "turn_id": observation.turn_id,
            "terminal_status": observation.terminal_status,
            "attempt_id": None if attempt is None else attempt["id"],
            "task_id": None if attempt is None else attempt["task_id"],
            "attempt_no": None if attempt is None else attempt["attempt_no"],
            "targets": [],
            "injected": False,
            "effective": False,
        }
        if spec is None:
            record["reason"] = "Worker thread의 Attempt·ExecutionSpec 결속을 원장에서 찾지 못했습니다."
            return record
        for target in json.loads(spec["payload_json"])["definition"]["resolved_targets"]:
            original = self.baseline.get(target["path"])
            if (
                target["access"] != "write"
                or original is None
                or sha256_bytes(original) != target["expected_content_digest"]
            ):
                continue
            path = self.workspace / target["path"]
            worker_output = path.read_bytes() if path.is_file() else None
            path.write_bytes(original)
            record["targets"].append({
                "path": target["path"],
                "expected_content_digest": target["expected_content_digest"],
                "worker_output_digest": (
                    None if worker_output is None else sha256_bytes(worker_output)
                ),
                "injected_digest": sha256_bytes(original),
            })
        record["injected"] = bool(record["targets"])
        record["effective"] = any(
            item["worker_output_digest"] != item["injected_digest"] for item in record["targets"]
        )
        if not record["injected"]:
            record["reason"] = "원본 bytes가 명세 기대 digest와 같은 write target이 없습니다."
        return record


_USAGE_COMPONENT_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
# 늦은 usage 전후로 바뀌면 안 되는 Worker provider call의 실행·효과 필드다.
_WORKER_CALL_EXECUTION_FIELDS = (
    "execution_status",
    "effect_status",
    "result_status",
    "new_turn_count",
    "completed_at",
    "raw_receipt_digest",
    "usage_id",
)


def _worker_usage_accounting(prepared: PreparedE2E, attempt_id: str) -> dict[str, Any]:
    """Worker 호출의 usage 회계 상태를 실행·효과 필드와 함께 읽는다."""

    with prepared.service.ledger.read() as connection:
        call = connection.execute(
            "SELECT * FROM provider_calls WHERE attempt_id=? AND role='worker' "
            "ORDER BY rowid DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if call is None:
            return {"provider_call": None, "usage_record": None, "usage_observations": []}
        usage = (
            None
            if call["usage_id"] is None
            else connection.execute(
                "SELECT payload_json FROM budget_usage WHERE id=?", (call["usage_id"],)
            ).fetchone()
        )
        observations = [
            dict(row)
            for row in connection.execute(
                "SELECT id,measurement_status,late,previous_observation_id,raw_observation_digest "
                "FROM usage_observations WHERE provider_call_id=? ORDER BY rowid",
                (call["id"],),
            )
        ]
    return {
        "provider_call": {
            key: call[key]
            for key in ("id", "status", "actual_tokens", *_WORKER_CALL_EXECUTION_FIELDS)
        },
        "usage_record": None if usage is None else json.loads(usage["payload_json"]),
        "usage_observations": observations,
    }


def _usage_missing_late(
    prepared: PreparedE2E,
    runtime: RecordedRuntime,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
) -> dict[str, Any]:
    """E2E-06: Worker usage 누락은 진행을 막지 않고, 늦은 usage는 회계 관측만 더한다.

    Worker usage를 가린 채 완주한 뒤 usage 구성요소가 0·추정이 아니라 null인지
    본다. 그다음 가려 둔 provider 원문을 같은 프로세스에서 늦게 전달하고, 실행·효과·
    완료 상태가 그대로이며 append-only 회계 관측만 늘었는지 전후를 비교한다.
    """

    observation_path = cell_root / "usage-observation.json"
    # 책임 판정은 evidence 파일이 있어야 돈다. 실패 경로를 위해 먼저 null과 이유를 남긴다.
    _write_json(
        observation_path,
        {
            "schema": "flowmarshal.project-e2e.usage-observation.v1",
            "before_late": None,
            "after_late": None,
            "reason": "E2E06_USAGE_OBSERVATION_NOT_PRODUCED",
        },
    )
    layer = UsageWithholdingRuntime(runtime)
    completion = _normal_completion(
        prepared, layer, source_digest, roles=roles, governance=governance
    )
    fault = _write_fault_injection(
        cell_root / "fault-injection.json",
        {
            "kind": "worker_usage_withheld_then_late_delivery",
            "writer": "qualification_fault_injector",
            "withheld_usage_source": UsageWithholdingRuntime.WITHHELD_USAGE_SOURCE,
            "withheld": [
                {key: value for key, value in item.items() if key != "provider_payload"}
                for item in layer.withheld
            ],
            "limitation": (
                "늦은 usage는 같은 프로세스에서 보관한 provider 원문으로만 다시 전달한다. "
                "Claude 저장 turn(read_stored)은 usage를 주지 않는다."
            ),
        },
    )
    if not completion.get("passed"):
        return {**completion, "fault_injection": fault}
    with prepared.service.ledger.read() as connection:
        bindings = {
            (binding["thread_id"], binding["turn_id"]): row["id"]
            for row in connection.execute(
                "SELECT id,binding_json FROM attempts WHERE project_id=? AND kind='execution' "
                "AND binding_json IS NOT NULL",
                (prepared.project_id,),
            )
            for binding in (json.loads(row["binding_json"]),)
        }
    entry = next(
        (
            item
            for item in reversed(layer.withheld)
            if item["terminal_status"] is not None
            and (item["thread_id"], item["turn_id"]) in bindings
        ),
        None,
    )
    if entry is None:
        # provider가 처음부터 usage를 주지 않았으면 가릴 것이 없어 fault가 성립하지 않는다.
        return {
            **completion,
            "passed": False,
            "fault_injection": fault,
            "failure": "E2E06_FAULT_NOT_TRIGGERED: Worker terminal 관측에 가릴 usage가 없었습니다.",
            "failure_code": "E2E06_FAULT_NOT_TRIGGERED",
            "failure_class": QualificationFailureClass.ENVIRONMENT.value,
        }
    attempt_id = bindings[(entry["thread_id"], entry["turn_id"])]
    before = _worker_usage_accounting(prepared, attempt_id)
    usage_before = before["usage_record"]
    call_before = before["provider_call"]
    missing_checks = {
        "usage_record_present": usage_before is not None,
        "usage_unavailable": usage_before is not None and usage_before["usage_available"] is False,
        "usage_components_null": usage_before is not None
        and all(usage_before[key] is None for key in _USAGE_COMPONENT_KEYS),
        "provider_call_actual_tokens_null": call_before is not None
        and call_before["actual_tokens"] is None,
        "provider_call_usage_unknown": call_before is not None
        and call_before["status"] == "usage_unknown",
        "first_observation_unavailable": [
            item["measurement_status"] for item in before["usage_observations"]
        ] == ["unavailable"],
    }
    journal_payload_digests = {
        sha256_digest(event["receipt"].get("payload"))
        for event in runtime.events
        if event.get("operation") in {"read", "read_stored", "completion_observation"}
        and isinstance(event.get("receipt"), dict)
    }
    document: dict[str, Any] = {
        "schema": "flowmarshal.project-e2e.usage-observation.v1",
        "attempt_id": attempt_id,
        "thread_id": entry["thread_id"],
        "turn_id": entry["turn_id"],
        "withheld_provider_payload_digest": entry["provider_payload_digest"],
        "before_late": before,
        "after_late": None,
        "missing_checks": missing_checks,
        "reason": None,
    }
    if not all(missing_checks.values()):
        document["reason"] = "E2E06_MISSING_USAGE_NOT_NULL"
        _write_json(observation_path, document)
        failed = ",".join(name for name, ok in missing_checks.items() if not ok)
        return {
            **completion,
            "fault_injection": fault,
            "missing_checks": missing_checks,
            **_cell_failure(
                "E2E06_MISSING_USAGE_NOT_NULL",
                f"usage 누락 뒤 기록이 null/unknown이 아닙니다: failed_checks=[{failed}]",
            ),
            "passed": False,
        }
    status_before = prepared.service.status(prepared.project_id)["project"]["run_state"]
    rows_before = _ledger_rows(prepared.service, prepared.project_id)
    late_error: str | None = None
    try:
        prepared.service.record_worker_usage(
            attempt_id=attempt_id,
            thread_id=entry["thread_id"],
            turn_id=entry["turn_id"],
            terminal_status=entry["terminal_status"],
            provider_payload=entry["provider_payload"],
        )
    except EngineServiceError as error:
        late_error = str(error)
    after = _worker_usage_accounting(prepared, attempt_id)
    delta = _ledger_delta(rows_before, _ledger_rows(prepared.service, prepared.project_id))
    status_after = prepared.service.status(prepared.project_id)["project"]["run_state"]
    observations_before = before["usage_observations"]
    observations_after = after["usage_observations"]
    late = observations_after[-1] if len(observations_after) == len(observations_before) + 1 else None
    call_after = after["provider_call"]
    late_checks = {
        "late_delivery_accepted": late_error is None,
        "late_payload_is_journaled_provider_original": (
            entry["provider_payload_digest"] in journal_payload_digests
        ),
        "one_late_observation_appended": late is not None
        and observations_after[: len(observations_before)] == observations_before,
        "late_observation_measured": late is not None
        and late["late"] == 1
        and late["measurement_status"] == "measured"
        and late["previous_observation_id"] == observations_before[-1]["id"],
        "provider_call_actual_tokens_recorded": call_after is not None
        and call_after["actual_tokens"] is not None,
        "provider_call_execution_fields_unchanged": call_after is not None
        and all(
            call_after[key] == call_before[key] for key in _WORKER_CALL_EXECUTION_FIELDS
        ),
        "usage_record_unchanged": after["usage_record"] == usage_before,
        "ledger_only_usage_reobserved": set(delta) <= {"history_events"}
        and _history_types(delta) == ["budget.usage_reobserved"],
        "run_state_unchanged": status_before == status_after == "completed",
    }
    document.update({"after_late": after, "late_checks": late_checks, "ledger_delta": delta})
    _write_json(observation_path, document)
    result = {
        **completion,
        "passed": all(late_checks.values()),
        "fault_injection": fault,
        "missing_checks": missing_checks,
        "late_checks": late_checks,
        "late_error": late_error,
    }
    if not result["passed"]:
        failed = ",".join(name for name, ok in late_checks.items() if not ok)
        result.update(
            _cell_failure("E2E06_LATE_USAGE_NOT_APPEND_ONLY", f"failed_checks=[{failed}]")
        )
    return result


# E2E-02·04·05 driver가 타는 제품 실행 경로다. `_drive_normal_completion`은 supervisor 없는
# 동기 dispatcher라 ContextRequest 후속 준비 job이 생기지 않는다.
_SUPERVISOR_RUNTIME_PATH = "EngineApplication.run_once+RuntimeJobSupervisor"


def _failed_checks(checks: dict[str, bool]) -> str:
    return ",".join(name for name, ok in checks.items() if not ok)


def _load_plan(prepared: PreparedE2E) -> PlanContractRevision:
    with prepared.service.ledger.read() as connection:
        row = connection.execute(
            "SELECT payload_json FROM plan_revisions WHERE id=?", (prepared.plan_revision_id,)
        ).fetchone()
    return PlanContractRevision.model_validate_json(row["payload_json"])


def _drive_application(
    prepared: PreparedE2E,
    runtime: Any,
    *,
    roles: EngineRoleConfiguration | None,
    governance: Any | None,
    structured_runner: Any | None,
    timeout_seconds: float,
    tolerated_blockers: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """제품 경로(`EngineApplication.run_once`와 기본 RuntimeJobSupervisor)로 Goal 판정까지 돈다.

    준비·실행·검사·복구가 모두 RuntimeJob으로 돈다. 역할 설정이 없는 결정적 테스트만
    준비된 proposal과 Goal Test step을 넘긴다. `tolerated_blockers`는 다음 tick이 Core
    자동 복구로 이어 가는 차단 code다. 그 밖의 차단은 그대로 돌려준다.
    """

    application = EngineApplication(
        prepared.service,
        runtime=runtime,
        role_configuration=roles,
        structured_runner=structured_runner,
        governance=governance,
    )
    supplied = roles is None and prepared.proposal is not None
    goal_step = (
        prepared.proposal.validation_steps[0].model_copy(update={"validation_id": "validation_goal"})
        if supplied
        else None
    )
    actions: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            outcome = application.run_once(
                prepared.project_id,
                proposal=prepared.proposal if supplied else None,
                goal_validation_step=goal_step,
            )
            label = outcome.action.value + (
                "" if outcome.blocker_code is None else f":{outcome.blocker_code}"
            )
            if not actions or actions[-1] != label:
                actions.append(label)
            if outcome.action is RunOnceAction.COMPLETED and outcome.goal_verdict_id is not None:
                return {"completed": True, "actions": actions, "blocker_code": None, "detail": None}
            if (
                outcome.action is RunOnceAction.BLOCKED
                and outcome.blocker_code not in tolerated_blockers
            ):
                return {
                    "completed": False,
                    "actions": actions,
                    "blocker_code": outcome.blocker_code,
                    "detail": outcome.detail,
                }
            time.sleep(0.05)
    finally:
        application.close_task_gate()
    return {
        "completed": False,
        "actions": actions,
        "blocker_code": "E2E_RUN_TIMEOUT",
        "detail": f"{timeout_seconds}초 안에 Goal 판정까지 가지 못했습니다.",
    }


def _goal_completion_checks(prepared: PreparedE2E) -> dict[str, bool]:
    """Goal 완료를 원장으로 확인한다. Task 검사는 repair 뒤 유효한 최신 결과만 본다."""

    status = prepared.service.status(prepared.project_id)
    plan = _load_plan(prepared)
    with prepared.service.ledger.read() as connection:
        task_statuses = [
            row["status"]
            for row in connection.execute(
                "SELECT status FROM task_contracts WHERE plan_revision_id=?",
                (prepared.plan_revision_id,),
            )
        ]
        latest_pass = True
        for task in plan.definition.tasks:
            latest = {
                row["validation_id"]: row["status"]
                for row in EngineService.effective_task_validation_results(connection, task.task_id)
            }
            latest_pass = latest_pass and all(
                latest.get(item.validation_id) == ValidationStatus.PASS.value
                for item in task.validations
            )
        verdicts = [
            row["status"]
            for row in connection.execute(
                "SELECT status FROM goal_verdicts WHERE plan_revision_id=? ORDER BY rowid",
                (prepared.plan_revision_id,),
            )
        ]
        goal_evidence = _independent_goal_test_evidence_count(
            connection, prepared.project_id, plan.definition.model_dump(mode="json")
        )
        plan_row = connection.execute(
            "SELECT status FROM plan_revisions WHERE id=?", (prepared.plan_revision_id,)
        ).fetchone()
    # Goal 충족 시 Core가 active_plan_revision_id를 비우므로(service record_goal_verdict),
    # 재계획 없이 활성화한 같은 Plan revision이 completed로 끝났는지를 원장에서 본다.
    return {
        "run_completed": status["project"]["run_state"] == "completed",
        "history_valid": bool(status["history_valid"]),
        "activated_plan_completed": plan_row is not None and plan_row["status"] == "completed",
        "all_tasks_completed": bool(task_statuses) and set(task_statuses) == {"completed"},
        "task_validations_latest_pass": latest_pass,
        "goal_verdict_satisfied": verdicts == ["satisfied"],
        "independent_goal_test_evidence": goal_evidence >= 1,
    }


def _dag_execution_order(prepared: PreparedE2E, refs: dict[str, str]) -> dict[str, Any]:
    """History 순서로 Task별 첫 materialize·첫 Attempt 예약·완료 sequence와 동시 진행 수를 읽는다.

    materialize부터 완료까지를 진행 중으로 본다. 완료 뒤 같은 Task에 결속된 검사 예약은
    진행 중으로 다시 세지 않는다.
    """

    with prepared.service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT sequence,event_type,entity_id,payload_json FROM history_events "
            "WHERE project_id=? AND event_type IN "
            "('task.materialized','attempt.reserved','task.completed') ORDER BY sequence",
            (prepared.project_id,),
        ).fetchall()
    first: dict[str, dict[str, int]] = {"task.materialized": {}, "attempt.reserved": {}}
    completed: dict[str, int] = {}
    in_flight: set[str] = set()
    max_in_flight = 0
    timeline: list[dict[str, Any]] = []
    for row in rows:
        task_id = (
            row["entity_id"]
            if row["event_type"] == "task.completed"
            else json.loads(row["payload_json"]).get("task_id")
        )
        if task_id not in refs:
            continue
        timeline.append(
            {"sequence": row["sequence"], "event_type": row["event_type"], "task_ref": refs[task_id]}
        )
        if row["event_type"] == "task.completed":
            completed.setdefault(task_id, row["sequence"])
            in_flight.discard(task_id)
        else:
            first[row["event_type"]].setdefault(task_id, row["sequence"])
            if task_id not in completed:
                in_flight.add(task_id)
        max_in_flight = max(max_in_flight, len(in_flight))
    return {
        "materialized": first["task.materialized"],
        "reserved": first["attempt.reserved"],
        "completed": completed,
        "max_in_flight": max_in_flight,
        "timeline": timeline,
    }


def _multi_task_dag(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    structured_runner: Any | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """E2E-02: 실제 선택 Plan의 둘 이상 Task를 dependency 순서대로 하나씩 실행하는지 본다.

    Task 분할·dependency는 live 역할이 정한다. 전제가 안 맞으면 Plan에 맞추지 않고 실행
    없이 model 실패로 남긴다. 실행 순서와 동시 진행 수는 원장 History 순서로만 판정한다.
    """

    plan = _load_plan(prepared)
    refs = {task.task_id: task.task_ref for task in plan.definition.tasks}
    precondition = {
        "task_refs": list(refs.values()),
        "dependencies": [
            {
                "producer": refs[item.producer_task_id],
                "consumer": refs[item.consumer_task_id],
                "dependency_type": item.dependency_type.value,
                "products": list(item.products),
            }
            for item in plan.definition.dependencies
        ],
    }
    if len(refs) < 2 or not plan.definition.dependencies:
        return {
            "passed": False,
            "source_fixture_digest": source_digest,
            "precondition": precondition,
            **_cell_failure(
                "E2E02_PLAN_NOT_MULTI_TASK_DAG",
                f"task_count={len(refs)}, dependency_count={len(plan.definition.dependencies)}",
                QualificationFailureClass.MODEL,
            ),
        }
    drive = _drive_application(
        prepared,
        runtime,
        roles=roles,
        governance=governance,
        structured_runner=structured_runner,
        timeout_seconds=timeout_seconds or 900.0 * (len(refs) + 1),
    )
    order = _dag_execution_order(prepared, refs)

    def after_producer(stage: str, consumer: str, producer: str) -> bool:
        return (
            producer in order["completed"]
            and consumer in order[stage]
            and order[stage][consumer] > order["completed"][producer]
        )

    edges = [
        {
            **edge,
            "respected": after_producer("materialized", item.consumer_task_id, item.producer_task_id)
            and after_producer("reserved", item.consumer_task_id, item.producer_task_id),
        }
        for edge, item in zip(precondition["dependencies"], plan.definition.dependencies)
    ]
    checks = {
        "all_tasks_executed": set(order["completed"]) == set(refs),
        "at_most_one_task_in_flight": order["max_in_flight"] <= 1,
        "dependency_order_respected": all(item["respected"] for item in edges),
        **_goal_completion_checks(prepared),
    }
    result = {
        "passed": drive["completed"] and all(checks.values()),
        "source_fixture_digest": source_digest,
        "runtime_path": _SUPERVISOR_RUNTIME_PATH,
        "actions": drive["actions"],
        "precondition": precondition,
        "execution_order": order["timeline"],
        "max_in_flight": order["max_in_flight"],
        "edges": edges,
        "checks": checks,
    }
    if not drive["completed"]:
        result.update(
            _cell_failure("E2E02_RUN_BLOCKED", f"{drive['blocker_code']}: {drive['detail']}")
        )
    elif not result["passed"]:
        result.update(
            _cell_failure(
                "E2E02_DAG_EXECUTION_NOT_VERIFIED", f"failed_checks=[{_failed_checks(checks)}]"
            )
        )
    return result


_E2E04_FAULT_RECORD = {
    "kind": "worker_implementation_reverted_before_observation",
    "writer": "qualification_fault_injector",
    "injection_point": "Worker 실행 turn의 첫 completed terminal 관측을 Engine에 넘기기 직전",
    "classifier_input": False,
    "injector_rollback": False,
    "rollback_reason": (
        "되돌린 원본이 곧 Worker 결과 file evidence(after_digest)여야 governance 사용자 변경 "
        "검사와 REPAIR_INPUT_CHANGED에 걸리지 않는다. 구현 복구는 다음 Worker Attempt가 한다."
    ),
}


def _file_digest(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def _failed_deterministic_observation(
    row: Any, *, attempt_id: str, task_id: str, validation_id: str
) -> bool:
    """evidence 한 행이 fault Attempt에 결속된 결정적 검사 실패 관측인지 strict 파싱으로 본다."""

    from .domain import DeterministicValidationObservation

    if (
        row["kind"] not in {"test", "command", "build"}
        or row["attempt_id"] != attempt_id
        or row["task_id"] != task_id
    ):
        return False
    try:
        observation = DeterministicValidationObservation.model_validate_json(row["observation"])
    except ValueError:
        return False
    return (
        observation.validation_id == validation_id
        and observation.task_id == task_id
        and row["source_ref"] == f"validation:{observation.validation_id}:{observation.argv[0]}"
        and not observation.timed_out
        and observation.actual_exit_code is not None
        and observation.actual_exit_code not in observation.expected_exit_codes
    )


def _repair_chain(prepared: PreparedE2E, injection: dict[str, Any]) -> dict[str, Any]:
    """원장에서 fault Attempt의 분류·assessment·repair·다음 Attempt 결속을 읽어 판정한다."""

    from .domain import FailureClass, RecoveryAssessment, RepairAction
    from .recovery import FailureDiagnosis
    from .service import latest_write_observation

    attempt_id = injection["attempt_id"]
    task_id = injection["task_id"]
    # 분류기가 직접 evidence로 implementation을 정했을 때만 이 지문이 나온다(error code 없음).
    expected_fingerprint = FailureDiagnosis(
        failure_class=FailureClass.IMPLEMENTATION,
        repair_action=RepairAction.TASK_REPAIR,
        rationale="E2E-04 기대 분류",
        source="direct_evidence",
    ).failure_fingerprint
    limit = next(
        task.recovery.max_same_failure_replans
        for task in _load_plan(prepared).definition.tasks
        if task.task_id == task_id
    )
    with prepared.service.ledger.read() as connection:
        writes = {
            item["path"]: latest_write_observation(connection, attempt_id, item["path"])
            for item in injection["targets"]
        }
        retries = [
            (row["sequence"], json.loads(row["payload_json"]))
            for row in connection.execute(
                "SELECT sequence,payload_json FROM history_events WHERE project_id=? "
                "AND event_type='task.retry_enabled' AND entity_id=? ORDER BY sequence",
                (prepared.project_id, task_id),
            )
        ]
        assessments = [
            RecoveryAssessment.model_validate_json(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM recovery_assessments WHERE project_id=? AND attempt_id=? "
                "ORDER BY rowid",
                (prepared.project_id, attempt_id),
            )
        ]
        failed_id = retries[0][1]["failed_validation_result_id"] if retries else None
        failed_row = (
            None
            if failed_id is None
            else connection.execute(
                "SELECT payload_json FROM validation_results WHERE id=?", (failed_id,)
            ).fetchone()
        )
        failed = (
            None if failed_row is None else ValidationResult.model_validate_json(failed_row["payload_json"])
        )
        evidence = (
            []
            if failed is None or not failed.evidence_ids
            else connection.execute(
                "SELECT id,task_id,attempt_id,kind,source_ref,observation FROM evidence_records "
                f"WHERE id IN ({','.join('?' for _ in failed.evidence_ids)})",
                failed.evidence_ids,
            ).fetchall()
        )
        recovery_job = connection.execute(
            "SELECT status FROM runtime_jobs WHERE project_id=? AND kind='recovery' AND checkpoint_key=?",
            (prepared.project_id, f"recovery:{attempt_id}:{expected_fingerprint}"),
        ).fetchone()
        next_attempt = connection.execute(
            "SELECT a.id,a.status,h.sequence FROM attempts a JOIN history_events h "
            "ON h.project_id=a.project_id AND h.event_type='attempt.reserved' AND h.entity_id=a.id "
            "WHERE a.task_id=? AND a.kind='execution' AND a.attempt_no=?",
            (task_id, injection["attempt_no"] + 1),
        ).fetchone()
    direct = [
        row["id"]
        for row in evidence
        if failed is not None
        and _failed_deterministic_observation(
            row, attempt_id=attempt_id, task_id=task_id, validation_id=failed.validation_id
        )
    ]
    assessment = next(
        (item for item in assessments if item.failure_fingerprint == expected_fingerprint), None
    )
    # 재시도마다 이전 재시도가 쓰지 않은 새 evidence에 결속됐는지 본다.
    seen: set[str] = set()
    fresh = bool(retries)
    for _sequence, payload in retries:
        ids = set(payload["new_evidence_ids"])
        fresh = fresh and bool(ids) and not ids & seen
        seen |= ids
    first = retries[0][1] if retries else None
    checks = {
        "fault_attempt_is_first": injection["attempt_no"] == 1,
        "worker_result_evidence_holds_injected_digest": all(
            writes[item["path"]] == (True, item["injected_digest"]) for item in injection["targets"]
        ),
        "failed_validation_bound_to_task": failed is not None
        and failed.status is ValidationStatus.FAIL
        and failed.task_id == task_id,
        "failure_evidence_is_direct_deterministic": bool(direct),
        "classified_implementation_from_direct_evidence": assessment is not None
        and assessment.failure_class is FailureClass.IMPLEMENTATION
        and assessment.action is RepairAction.TASK_REPAIR,
        "assessment_bound_to_failure_evidence": assessment is not None
        and bool(set(direct) & set(assessment.new_evidence_ids)),
        "recovery_job_consumed": recovery_job is not None and recovery_job["status"] == "consumed",
        "repair_enabled_from_assessment": first is not None
        and assessment is not None
        and first["previous_attempt_id"] == attempt_id
        and first["failure_class"] == FailureClass.IMPLEMENTATION.value
        and first["recovery_assessment_id"] == assessment.assessment_id,
        "repair_bound_to_new_evidence": fresh,
        "recovery_count_within_limit": 1 <= len(retries) <= limit,
        "next_attempt_succeeded_after_repair": next_attempt is not None
        and next_attempt["status"] == "succeeded"
        and bool(retries)
        and next_attempt["sequence"] > retries[0][0],
    }
    return {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "expected_failure_fingerprint": expected_fingerprint,
        "worker_write_observations": {path: list(value) for path, value in writes.items()},
        "failed_validation_result_id": failed_id,
        "direct_failure_evidence_ids": direct,
        "assessments": [item.model_dump(mode="json") for item in assessments],
        "retries": [{"sequence": sequence, **payload} for sequence, payload in retries],
        "recovery_limit": limit,
        "next_attempt": None if next_attempt is None else dict(next_attempt),
        "checks": checks,
    }


def _approved_repair(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    structured_runner: Any | None = None,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    """E2E-04: 통제된 구현 결함 하나를 분류기가 implementation으로 정하고 같은 Task repair로 복구하는지 본다.

    첫 Worker 결과만 `ImplementationFaultRuntime`이 원본으로 되돌린다. 분류·assessment·
    repair·다음 Attempt는 모두 실제 Core 경로이며 주입 기록은 provenance일 뿐 분류기
    입력이 아니다. 구현 재시도를 허용한 Task가 Plan에 없으면 실행 없이 model 실패로 남긴다.
    """

    fault_path = cell_root / "fault-injection.json"
    # 책임 판정은 evidence 파일이 있어야 돈다. 실패 경로를 위해 먼저 null과 이유를 남긴다.
    _write_fault_injection(
        fault_path, _E2E04_FAULT_RECORD | {"injection": None, "reason": "E2E04_FAULT_NOT_INJECTED"}
    )
    plan = _load_plan(prepared)
    precondition = {
        task.task_ref: {
            "task_id": task.task_id,
            "kind": task.kind.value,
            "retryable_failure_classes": [
                item.value for item in task.recovery.retryable_failure_classes
            ],
            "max_same_failure_replans": task.recovery.max_same_failure_replans,
            "deterministic_validation_ids": [
                item.validation_id
                for item in task.validations
                if item.method == "deterministic"
                and set(item.required_evidence_kinds) & {"test", "command", "build"}
            ],
        }
        for task in plan.definition.tasks
    }
    retryable = [
        item
        for item in precondition.values()
        if item["kind"] == TaskKind.CHANGE.value
        and "implementation" in item["retryable_failure_classes"]
        and item["max_same_failure_replans"] >= 1
    ]
    eligible = frozenset(item["task_id"] for item in retryable if item["deterministic_validation_ids"])
    if not eligible:
        return {
            "passed": False,
            "source_fixture_digest": source_digest,
            "precondition": precondition,
            **_cell_failure(
                "E2E04_PRECONDITION_NO_DETERMINISTIC_VALIDATION"
                if retryable
                else "E2E04_PRECONDITION_IMPLEMENTATION_NOT_RETRYABLE",
                "구현 결함을 같은 Task에서 재시도하고 결정적 검사로 확인할 CHANGE Task가 Plan에 없습니다.",
                QualificationFailureClass.MODEL,
            ),
        }
    layer = ImplementationFaultRuntime(
        runtime, service=prepared.service, workspace=prepared.workspace, eligible_task_ids=eligible
    )
    drive = _drive_application(
        prepared,
        layer,
        roles=roles,
        governance=governance,
        structured_runner=structured_runner,
        timeout_seconds=timeout_seconds,
        tolerated_blockers=frozenset({"TASK_VALIDATION_FAILED"}),
    )
    injection = layer.injection
    targets = () if injection is None else injection["targets"]
    fault = _write_fault_injection(
        fault_path,
        _E2E04_FAULT_RECORD
        | {
            "injection": injection,
            "final_target_digests": {
                item["path"]: _file_digest(prepared.workspace / item["path"]) for item in targets
            },
            "reason": (
                None if injection is not None and injection["injected"] else "E2E04_FAULT_NOT_INJECTED"
            ),
        },
    )
    result: dict[str, Any] = {
        "passed": False,
        "source_fixture_digest": source_digest,
        "runtime_path": _SUPERVISOR_RUNTIME_PATH,
        "actions": drive["actions"],
        "precondition": precondition,
        "fault_injection": fault,
    }
    if injection is None:
        return result | _cell_failure(
            "E2E04_RUN_BLOCKED",
            f"Worker terminal 관측 전에 멈췄습니다: {drive['blocker_code']}: {drive['detail']}",
        )
    if not injection["injected"]:
        return result | _cell_failure(
            "E2E04_FAULT_NOT_INJECTED",
            str(injection.get("reason")),
            QualificationFailureClass.FIXTURE,
        )
    if not injection["effective"]:
        return result | _cell_failure(
            "E2E04_FAULT_NOT_EFFECTIVE",
            "Worker가 쓰기 target을 바꾸지 않아 되돌릴 구현이 없었습니다.",
            QualificationFailureClass.MODEL,
        )
    chain = _repair_chain(prepared, injection)
    checks = {**chain["checks"], **_goal_completion_checks(prepared)}
    result.update(
        {
            "repair_chain": chain,
            "checks": checks,
            "passed": drive["completed"] and all(checks.values()),
        }
    )
    if not drive["completed"]:
        result.update(
            _cell_failure("E2E04_RUN_BLOCKED", f"{drive['blocker_code']}: {drive['detail']}")
        )
    elif not result["passed"]:
        result.update(
            _cell_failure(
                "E2E04_REPAIR_CHAIN_INCOMPLETE", f"failed_checks=[{_failed_checks(checks)}]"
            )
        )
    return result


def _verify_context_followup(
    prepared: PreparedE2E,
    job: dict[str, Any],
    *,
    jobs: list[dict[str, Any]],
    baseline: dict[str, bytes],
    initial_paths: set[str],
    project_map: dict[str, str],
) -> dict[str, Any]:
    """후속 준비 job 하나가 요청·해소·Map·Execution Spec·PromptBundle에 결속됐는지 본다."""

    from .context import read_context_fragment
    from .domain import TaskExecutionSpecRevision
    from .worker_prompt import PromptArtifactError, PromptArtifactStore

    root = prepared.workspace.resolve()
    request = json.loads(job["request_json"])
    prefix = job["checkpoint_key"].rsplit("context:", 1)[0]
    provider = next((item for item in jobs if item["checkpoint_key"] == prefix + "provider"), None)
    provider_preparation = (
        None
        if provider is None or provider["result_json"] is None
        else json.loads(provider["result_json"])["preparation"]
    )
    provider_request = None if provider_preparation is None else provider_preparation["context_request"]
    # ContextRequest 대신 proposal의 context_needs에 Map 밖 source를 적으면 Core가 compile
    # 단계의 요청으로 후속 job을 만든다. 그 요청의 need는 proposal의 need 그대로다.
    provider_needs = (
        []
        if provider_preparation is None or provider_preparation.get("proposal") is None
        else provider_preparation["proposal"]["context_needs"]
    )
    followup_request = request.get("context_request") or {}
    if provider_request is not None and provider_request == followup_request:
        request_origin = "provider_context_request"
    elif (
        provider_request is None
        and followup_request.get("missing_needs")
        and all(need in provider_needs for need in followup_request["missing_needs"])
    ):
        request_origin = "provider_proposal_context_needs"
    else:
        request_origin = None
    resolution = request.get("context_resolution") or []
    sources = []
    for item in resolution:
        reference = item["source_ref"]
        resolved = (root / reference).resolve()
        content = baseline.get(reference)
        sources.append(
            {
                **item,
                "inside_approved_root": not Path(reference).is_absolute() and root in resolved.parents,
                "file_digest_matches": content is not None
                and sha256_bytes(content) == item["content_digest"],
                "project_map_digest_matches": project_map.get(reference) == item["content_digest"],
                "initially_unmapped": reference not in initial_paths,
            }
        )
    with prepared.service.ledger.read() as connection:
        spec_row = connection.execute(
            "SELECT s.payload_json FROM attempts a JOIN execution_spec_revisions s "
            "ON s.task_id=a.task_id AND s.definition_digest=a.execution_spec_digest "
            "WHERE a.task_id=? AND a.kind='execution' AND a.status='succeeded' "
            "ORDER BY a.attempt_no DESC LIMIT 1",
            (job["task_id"],),
        ).fetchone()
    bindings = []
    if spec_row is not None:
        manifest = TaskExecutionSpecRevision.model_validate_json(
            spec_row["payload_json"]
        ).definition.context_manifest
        try:
            suffix = PromptArtifactStore(prepared.service.ledger.artifact_root).load(
                manifest.prompt_binding
            ).dynamic_suffix
        except PromptArtifactError:
            suffix = None
        for item in resolution:
            fragment = next(
                (value for value in manifest.fragments if value.source_ref == item["source_ref"]),
                None,
            )
            body = None
            if fragment is not None:
                try:
                    body = read_context_fragment(root, fragment)
                except (OSError, ValueError):
                    body = None
            bindings.append(
                {
                    "source_ref": item["source_ref"],
                    "fragment_selector": None if fragment is None else fragment.selector,
                    "fragment_content_digest": None if fragment is None else fragment.content_digest,
                    "digest_bound": fragment is not None
                    and fragment.content_digest == item["content_digest"],
                    "body_bound": suffix is not None
                    and body is not None
                    and (
                        f'<reference-data source="{fragment.source_ref}#{fragment.selector}">\n'
                        f"{body}\n</reference-data>"
                    )
                    in suffix,
                }
            )
    checks = {
        "single_followup_for_preparation": sum(
            item["checkpoint_key"].startswith(prefix + "context:") for item in jobs
        )
        == 1,
        "checkpoint_bound_to_request_and_resolution": job["checkpoint_key"]
        == prefix
        + "context:"
        + sha256_digest(
            {
                "context_request": request.get("context_request"),
                "context_resolution": request.get("context_resolution"),
            }
        ),
        "request_matches_provider_job": request_origin is not None,
        "followup_consumed": job["status"] == "consumed",
        "resolution_present": bool(sources),
        "resolution_inside_approved_root": all(item["inside_approved_root"] for item in sources),
        "resolution_matches_file_digest": all(item["file_digest_matches"] for item in sources),
        "resolution_in_project_map": all(item["project_map_digest_matches"] for item in sources),
        "resolved_outside_initial_project_map": any(item["initially_unmapped"] for item in sources),
        "prompt_binds_fragment_body_and_digest": bool(bindings)
        and all(item["digest_bound"] and item["body_bound"] for item in bindings),
    }
    return {
        "job_id": job["id"],
        "task_id": job["task_id"],
        "checkpoint_key": job["checkpoint_key"],
        "context_request": request.get("context_request"),
        "request_origin": request_origin,
        "resolution": sources,
        "prompt_bindings": bindings,
        "checks": checks,
    }


def _context_discovery(
    prepared: PreparedE2E,
    runtime: CodexRuntimePort,
    source_digest: str,
    *,
    cell_root: Path,
    roles: EngineRoleConfiguration | None = None,
    governance: Any | None = None,
    structured_runner: Any | None = None,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """E2E-05: 초기 Project Map 밖 로컬 파일을 Core가 자동 해소해 실행까지 결속하는지 본다.

    ContextRequest를 주입하지 않는다. fixture 규칙 모듈은 요청 원문에 파일명이 없어 초기
    Map에 없고, 그 파일이 필요하다고 요청하는 것은 live 준비 역할이다. 해소 증거는
    supervisor 경로의 후속 준비 job(`:context:` checkpoint)만 쓴다.
    """

    path = cell_root / "context-discovery.json"
    initial_paths = {
        entry.path
        for entry in prepared.service.load_current_project_map(prepared.project_id).entries
    }
    document: dict[str, Any] = {
        "schema": "flowmarshal.project-e2e.context-discovery.v1",
        "runtime_path": _SUPERVISOR_RUNTIME_PATH,
        "initial_project_map_paths": sorted(initial_paths),
        "context_jobs": None,
        # 실행 중 예외로 끝나면 미관측으로 오해되지 않도록 판정 전 상태를 따로 적는다.
        "reason": "E2E05_NOT_EVALUATED",
    }
    # 책임 판정은 evidence 파일이 있어야 돈다. 실패 경로를 위해 먼저 null과 이유를 남긴다.
    _write_json(path, document)
    baseline = _workspace_files(prepared.workspace)
    drive = _drive_application(
        prepared,
        runtime,
        roles=roles,
        governance=governance,
        structured_runner=structured_runner,
        timeout_seconds=timeout_seconds,
    )
    with prepared.service.ledger.read() as connection:
        jobs = [
            dict(row)
            for row in connection.execute(
                "SELECT id,task_id,status,checkpoint_key,request_json,result_json FROM runtime_jobs "
                "WHERE project_id=? AND kind='execution_spec_prepare' ORDER BY rowid",
                (prepared.project_id,),
            )
        ]
    project_map = {
        entry.path: entry.content_digest
        for entry in prepared.service.load_current_project_map(prepared.project_id).entries
    }
    followups = [
        _verify_context_followup(
            prepared,
            job,
            jobs=jobs,
            baseline=baseline,
            initial_paths=initial_paths,
            project_map=project_map,
        )
        for job in jobs
        if ":context:" in job["checkpoint_key"]
    ]
    document.update({"context_jobs": followups, "actions": drive["actions"], "reason": None})
    result: dict[str, Any] = {
        "passed": False,
        "source_fixture_digest": source_digest,
        "runtime_path": _SUPERVISOR_RUNTIME_PATH,
        "actions": drive["actions"],
        "context_jobs": followups,
    }
    if not followups:
        if drive["blocker_code"] == "CONTEXT_REQUIRED":
            failure = _cell_failure(
                "E2E05_CONTEXT_REQUIRED_NOT_AUTO_RESOLVED",
                f"후속 준비 job 없이 질문 경계로 멈췄습니다: {drive['detail']}",
            )
        elif not drive["completed"]:
            failure = _cell_failure(
                "E2E05_RUN_BLOCKED", f"{drive['blocker_code']}: {drive['detail']}"
            )
        else:
            failure = _cell_failure(
                "E2E05_CONTEXT_REQUEST_NOT_OBSERVED",
                "준비 역할이 ContextRequest를 내지 않아 후속 준비 job이 없습니다.",
                QualificationFailureClass.MODEL,
            )
        document["reason"] = failure["failure_code"]
        _write_json(path, document)
        return result | failure
    checks = {
        "context_followups_verified": all(all(item["checks"].values()) for item in followups),
        **_goal_completion_checks(prepared),
    }
    result.update({"checks": checks, "passed": drive["completed"] and all(checks.values())})
    if not drive["completed"]:
        result.update(
            _cell_failure("E2E05_RUN_BLOCKED", f"{drive['blocker_code']}: {drive['detail']}")
        )
    elif not result["passed"]:
        failed = [
            f"{item['job_id']}:{_failed_checks(item['checks'])}"
            for item in followups
            if not all(item["checks"].values())
        ]
        result.update(
            _cell_failure(
                "E2E05_CONTEXT_BINDING_NOT_VERIFIED",
                f"failed_checks=[{_failed_checks(checks)}], followups={failed}",
            )
        )
    document["reason"] = result.get("failure_code")
    _write_json(path, document)
    return result


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
        "partial-write-input-changed": (
            "E2E-13",
            ("task_execution", "partial_write", "freshness_check"),
            ("partial_effect", "source_digest", "error", "effect_count"),
            (EvidenceProvenance.FAULT_INJECTED,),
        ),
        "in-flight-replan-protection": (
            "E2E-17",
            ("task_execution", "replan", "in_flight_protection"),
            ("attempt", "binding", "plan", "reuse_decision", "fault_injection"),
            (EvidenceProvenance.FAULT_INJECTED,),
        ),
        "partial-write-resume": (
            "E2E-12",
            (
                "task_execution",
                "partial_write",
                "observe_existing",
                "resume",
                "independent_validation",
            ),
            ("partial_effect", "binding", "runtime_observation", "validation"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        # Goal·승인·활성화는 live이고 위반 후보만 scripted plan_expander stub(fake)이다.
        "prohibited-effect": (
            "E2E-14",
            ("effect_policy_check",),
            ("authorization", "execution_spec", "error", "effect_count"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAKE),
        ),
        # live 준비 위의 사용자 승인·예산 판단만 쓴다. target·효과 미커버라 cell은 FAILED다.
        "scope-expansion": (
            "E2E-15",
            ("goal_authorization", "scope_check"),
            ("authorization", "plan", "finding", "ledger"),
            (EvidenceProvenance.LIVE,),
        ),
        # fault 주입 없이 live 실행 전후 전체 트리 digest와 final report만 쓴다.
        "read-only-report": (
            "E2E-03",
            ("task_execution", "independent_validation", "goal_verdict"),
            ("source_digest", "evidence", "goal_verdict", "final_report"),
            (EvidenceProvenance.LIVE,),
        ),
        # live 실행 위에 Worker usage 가림과 늦은 전달만 주입한다.
        "usage-missing-late": (
            "E2E-06",
            ("task_execution", "independent_validation", "goal_verdict"),
            ("runtime_receipt", "usage_observation", "ledger", "goal_verdict"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        # fault 주입 없이 live Plan의 DAG 실행 순서를 원장 History로만 판정한다.
        "multi-task-dag": (
            "E2E-02",
            ("task_execution", "independent_validation", "goal_verdict"),
            ("plan", "ledger", "runtime_receipt", "validation", "goal_verdict"),
            (EvidenceProvenance.LIVE,),
        ),
        # live 실행 위에 첫 Worker 결과 되돌림만 주입한다. 분류·repair는 실제 Core 경로다.
        "approved-repair": (
            "E2E-04",
            (
                "task_execution",
                "recovery",
                "task_execution",
                "independent_validation",
                "goal_verdict",
            ),
            ("failure", "recovery_assessment", "ledger", "validation"),
            (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
        ),
        # ContextRequest를 주입하지 않는다. supervisor 후속 준비 job만 증거로 쓴다.
        "context-discovery": (
            "E2E-05",
            ("context_discovery", "task_execution", "independent_validation", "goal_verdict"),
            ("context_request", "file", "prompt_binding", "validation"),
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
        "partial_effect": cell_root / "fault-injection.json",
        "attempt": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "plan": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "reuse_decision": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "fault_injection": cell_root / "fault-injection.json",
        "authorization": cell_root / "authorization-observation.json",
        "finding": cell_root / "scope-check.json",
        "goal_verdict": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "evidence": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "final_report": cell_root / "final-report.json",
        "usage_observation": cell_root / "usage-observation.json",
        "failure": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "recovery_assessment": cell_root / "state" / "flowmarshal-engine.sqlite3",
        "context_request": cell_root / "context-discovery.json",
        "file": cell_root / "context-discovery.json",
        "prompt_binding": cell_root / "context-discovery.json",
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
        # _cell_failure로 남긴 cell은 안정된 code·class를, 나머지는 기존 설명 문장을 쓴다.
        failure_class=QualificationFailureClass(cell.get("failure_class", "product")),
        failure_code=str(
            cell.get("failure_code") or cell.get("failure") or cell.get("error") or "E2E_FAILED"
        )[:500],
    )


def _project_e2e_suite(suite: QualificationSuiteManifest) -> QualificationSuiteManifest:
    """project E2E cell이 판정할 책임이다. E2E-18은 clean install 계약이 따로 증명한다."""

    return QualificationSuiteManifest.model_validate(
        suite.model_dump(mode="json")
        | {
            "e2e_responsibilities": [
                item.model_dump(mode="json")
                for item in suite.e2e_responsibilities
                if item.responsibility_id != CLEAN_INSTALL_RESPONSIBILITY_ID
            ]
        }
    )


def _clean_install_anchor(link: CleanInstallLinkResult) -> dict[str, Any] | None:
    if link.status == "not_run":
        return None
    return {
        "run_root": link.run_root,
        "report_digest": link.report_digest,
        "evaluation_contract_digest": link.evaluation_contract_digest,
    }


def _join_clean_install_link(
    responsibility_report: Any, link: CleanInstallLinkResult
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """project E2E 책임 판정에 E2E-18 연결 결과를 더한다.

    clean install evidence를 project E2E 계약으로 다시 결속하지 않고 결과만 합친다.
    """

    return link.failures, {
        "responsibility_count": responsibility_report.responsibility_count + 1,
        "passed_responsibility_count": (
            responsibility_report.passed_responsibility_count + (link.status == "passed")
        ),
        "not_run_responsibility_count": (
            len(responsibility_report.not_run_responsibility_ids) + (link.status == "not_run")
        ),
        "clean_install_link_status": link.status,
        "clean_install_evaluation_contract_digest": link.evaluation_contract_digest,
    }


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
    # cell마다 실제로 쓰는 fixture·요청 원문·승인 여부를 같은 digest에 결속한다.
    fixture_digests = tuple(
        sha256_digest(
            {
                "scenario": scenario,
                "source_fixture_digest": source_digest,
                "scenario_plan": asdict(_E2E_SCENARIO_PLANS[scenario]),
            }
        )
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
                                    "authority_cells": {
                                        "requests": [
                                            _E2E_SOURCE_REQUEST,
                                            _PROHIBITED_EFFECT_REQUEST,
                                            _READ_ONLY_REPORT_REQUEST,
                                            _MULTI_TASK_DAG_REQUEST,
                                            _APPROVED_REPAIR_REQUEST,
                                            _CONTEXT_DISCOVERY_REQUEST,
                                        ],
                                        "ledger_rows": _LEDGER_ROWS,
                                        "sources": [
                                            inspect.getsource(item)
                                            for item in (
                                                _ledger_delta,
                                                _cell_failure,
                                                _history_types,
                                                _plan_expansion_stub,
                                                _stub_plan_inspection,
                                                _StubPlanExpanderRunner,
                                                _prohibited_effect_blocked,
                                                _scope_expansion_blocked,
                                                _workspace_files,
                                                _workspace_tree_digest,
                                                _read_only_report,
                                                _RuntimeObservationLayer,
                                                UsageWithholdingRuntime,
                                                _worker_usage_accounting,
                                                _usage_missing_late,
                                                _independent_goal_test_evidence_count,
                                                _load_plan,
                                                _drive_application,
                                                _goal_completion_checks,
                                                _dag_execution_order,
                                                _multi_task_dag,
                                                ImplementationFaultRuntime,
                                                _failed_deterministic_observation,
                                                _repair_chain,
                                                _approved_repair,
                                                _verify_context_followup,
                                                _context_discovery,
                                            )
                                        ],
                                    },
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
    """scenario 표가 쓰는 모든 project E2E fixture 디렉터리를 하나의 digest로 덮는다."""

    fixtures = base / "tests" / "fixtures" / "engine"
    return sha256_digest(
        {
            name: _fixture_directory_digest(fixtures / name)
            for name in sorted({plan.fixture for plan in _E2E_SCENARIO_PLANS.values()})
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
    clean_install_run_root: Path | str | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    """release project E2E. 실행 Task는 제품 경로와 같은 필수 governance gate를 지난다.

    E2E-18은 이 run이 만들지 않는다. 같은 candidate로 이미 끝난 clean install run root를
    다시 실행하지 않고 검증해 연결하며, 없으면 NOT_RUN으로 남긴다.
    """
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
    # provider 호출 전에 연결을 검증한다. 잘못 준 연결로 campaign 비용을 쓰지 않는다.
    clean_install_link = verify_clean_install_link(
        root=base,
        clean_install_run_root=clean_install_run_root,
        candidate_binding=candidate_binding,
    )
    if clean_install_link.status == "failed":
        raise QualificationRunError(
            "E2E_CLEAN_INSTALL_LINK_INVALID:" + ";".join(clean_install_link.failures)
        )
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
        # metadata 결속을 먼저 대조한다. 불일치면 pause 상태를 그대로 두고 멈춘다.
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
                "clean_install_link": _clean_install_anchor(clean_install_link),
                **governance_observation,
                **provider_run_metadata(runtime_selection),
            },
            evaluation_policies,
        )
        if prior_state is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
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
                    scenario_plan = _E2E_SCENARIO_PLANS[scenario]
                    cell_root.mkdir(parents=True)
                    workspace, _copied_digest = _copy_fixture(
                        base, cell_root, scenario_plan.fixture
                    )
                    if _project_e2e_fixture_source_digest(base) != source_digest:
                        raise QualificationRunError("복사 직전 E2E fixture digest가 변경됐습니다.")
                    # prepare 앞에서만 만든다. .git은 inventory digest 밖이라 STALE을 만들지 않는다.
                    _initialize_workspace_git(workspace)
                    prepared = _prepare_from_raw_request(
                        workspace=workspace,
                        state_root=cell_root / "state",
                        runtime=recorded,
                        roles=roles,
                        evaluation_policies=evaluation_policies,
                        evaluation_contract_digest=contract.contract_digest,
                        fixture_digest=digest,
                        source_request=scenario_plan.source_request,
                        authorize=scenario_plan.authorize,
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
                        elif scenario == "partial-write-input-changed":
                            cell = _partial_write_input_changed(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "in-flight-replan-protection":
                            cell = _in_flight_replan_protection(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "partial-write-resume":
                            cell = _partial_write_resume(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "prohibited-effect":
                            cell = _prohibited_effect_blocked(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                            )
                        elif scenario == "scope-expansion":
                            cell = _scope_expansion_blocked(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                evaluation_contract_digest=contract.contract_digest,
                                fixture_digest=digest,
                                governance=governance,
                            )
                        elif scenario == "read-only-report":
                            cell = _read_only_report(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "usage-missing-late":
                            cell = _usage_missing_late(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "multi-task-dag":
                            cell = _multi_task_dag(
                                prepared,
                                recorded,
                                source_digest,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "approved-repair":
                            cell = _approved_repair(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
                                roles=roles,
                                governance=governance,
                            )
                        elif scenario == "context-discovery":
                            cell = _context_discovery(
                                prepared,
                                recorded,
                                source_digest,
                                cell_root=cell_root,
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
                        # E2E-15만 사용자 승인 단계가 실패하면 활성화 receipt가 없다.
                        # 다른 cell은 receipt가 없으면 여기서 멈춘다.
                        "activation_receipt_digest": (
                            None
                            if scenario == "scope-expansion"
                            and not activation_receipt.is_file()
                            else sha256_bytes(activation_receipt.read_bytes())
                        ),
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
        suite = _project_e2e_suite(qualification_suite_manifest(base))
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
        # 실행 사이 clean install 산출물이 바뀌지 않았는지 고정한 report digest로 다시 본다.
        anchor = _clean_install_anchor(clean_install_link)
        clean_install_link = verify_clean_install_link(
            root=base,
            clean_install_run_root=None if anchor is None else anchor["run_root"],
            candidate_binding=candidate_binding,
            expected_report_digest=None if anchor is None else anchor["report_digest"],
        )
        link_failures, link_metrics = _join_clean_install_link(
            responsibility_report, clean_install_link
        )
        _write_json(destination / "clean-install-link.json", clean_install_link.__dict__)
        failures = tuple((*cell_failures, *responsibility_report.failures, *link_failures))
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
                **link_metrics,
            },
            failures=failures,
            generated_at=utc_now(),
        )
        _write_json(destination / "qualification-report.json", report)
        return destination, report
