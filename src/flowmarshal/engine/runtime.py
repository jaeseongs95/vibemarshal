from __future__ import annotations

from contextlib import nullcontext
from .capabilities import role_execution_scope

import json
import hashlib
import inspect
import os
import subprocess
import threading
import time
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import Field, ValidationError, model_serializer, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    AttemptKind,
    CriterionVerdict,
    DeterministicValidationObservation,
    EngineModel,
    EvidenceKind,
    EvidenceRecord,
    ExecutionSpecProposal,
    FailureClass,
    GoalVerdict,
    GoalVerdictStatus,
    PlanContractRevision,
    PROVIDER_FAILURE_TERMINAL_STATUSES,
    PROVIDER_SUCCESS_TERMINAL_STATUSES,
    RepairAction,
    RunOnceAction,
    RunOnceOutcome,
    RunOnceResult,
    RuntimeJob,
    RuntimeJobKind,
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    RuntimeIntentKind,
    SemanticValidationObservation,
    TaskExecutionSpecRevision,
    ThreadBinding,
    ValidationExecutionStep,
    ValidationResult,
    ValidationStatus,
    new_id,
    utc_now,
)
from .models import ModelCapability, ModelInventory
from .runtime_observation import bounded_observation_call
from .operation_trace import (
    OperationTrace,
    OperationTraceScope,
    current_operation_trace_scope,
    use_operation_trace_scope,
)
from .service import ContextRequiredError, EngineService, EngineServiceError
from .recovery import (
    FAILURE_DIAGNOSIS_PROVENANCE,
    FAILURE_REPAIR_ACTIONS,
    EvidenceFirstFailureClassifier,
    FailureDiagnosis,
    FailureSignal,
    recovery_limit_decision,
    recovery_route,
    stable_recovery_assessment_id,
    terminal_failure_diagnosis,
)


REQUIRED_PERMISSION_PROFILE = ":danger-full-access"
REQUIRED_APPROVAL_POLICY = "never"


from .model_lock import RUNTIME_CAPABILITIES, parse_inventory_models, verify_binding


class RuntimePolicyError(RuntimeError):
    pass


_ACTIVE_RUNTIME_JOB: ContextVar[tuple["RuntimeJobSupervisor", str] | None] = ContextVar(
    "flowmarshal_active_runtime_job", default=None
)


def active_runtime_job_id() -> str | None:
    current = _ACTIVE_RUNTIME_JOB.get()
    return None if current is None else current[1]


def notify_active_runtime_job_progress(event: dict[str, Any]) -> None:
    """역할 runner의 provider lifecycle을 현재 durable job에 연결한다."""

    current = _ACTIVE_RUNTIME_JOB.get()
    if current is not None:
        current[0].record_role_progress(current[1], event)


def capture_active_runtime_job_progress() -> Callable[[dict[str, Any]], None] | None:
    """callback thread에서도 원래 job 결속을 유지하는 durable sink를 캡처한다."""

    current = _ACTIVE_RUNTIME_JOB.get()
    if current is None:
        return None
    supervisor, job_id = current
    return lambda event: supervisor.record_role_progress(job_id, event)


class ExecutionPolicyEvidence(EngineModel):
    environment: str = "local"
    permission_profile: str
    approval_policy: str
    config_digest: str
    profile_catalog_digest: str
    cwd: str


class CodexProjectBinding(EngineModel):
    """App Server가 소유한 저장 프로젝트와 thread를 결속하는 계약."""

    project_id: str = Field(min_length=1, max_length=500)
    expected_root: str = Field(min_length=1)
    expected_name: str | None = Field(default=None, min_length=1, max_length=500)


class _NewThreadProjectProof(EngineModel):
    thread_id: str
    project_binding: CodexProjectBinding
    cwd: str
    rollout_path: str
    receipt_digest: str


class RuntimeOperationReceipt(EngineModel):
    operation_id: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any]
    binding: ThreadBinding | None = None
    operation_trace: dict[str, Any] | None = None
    operation_trace_ref: str | None = None
    operation_trace_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def operation_trace_is_bound(self):
        if self.operation_trace is None:
            if self.operation_trace_digest is not None:
                raise ValueError("operation trace body 없이 digest를 결속할 수 없습니다.")
        elif self.operation_trace_digest != sha256_digest(self.operation_trace):
            raise ValueError("operation trace digest가 body와 다릅니다.")
        return self

    @model_serializer(mode="wrap")
    def omit_absent_operation_trace(self, handler):
        value = handler(self)
        for field_name in ("operation_trace", "operation_trace_ref", "operation_trace_digest"):
            if getattr(self, field_name) is None:
                value.pop(field_name, None)
        return value


class RuntimeObservation(EngineModel):
    thread_id: str
    turn_id: str | None = None
    active: bool
    terminal_status: str | None = None
    final_response: str | None = None
    payload: dict[str, Any]


class CodexRuntimePort(Protocol):
    def verify_execution_policy(self, cwd: Path | str) -> ExecutionPolicyEvidence: ...

    def list_models(self) -> ModelInventory: ...

    def create_thread(
        self,
        *,
        cwd: Path,
        title: str,
        model: str,
        developer_instructions: str,
        ephemeral: bool = False,
    ) -> RuntimeOperationReceipt: ...

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt: ...

    def read(self, *, thread_id: str) -> RuntimeObservation: ...

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt: ...

    def interrupt(
        self, *, thread_id: str, turn_id: str, timeout_seconds: float = 5.0,
    ) -> RuntimeOperationReceipt: ...

    def read_stored(
        self, *, thread_id: str, turn_id: str | None = None, timeout_seconds: float = 5.0,
    ) -> RuntimeObservation: ...

    def close(self, *, timeout_seconds: float = 5.0) -> None: ...


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value))


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


class CodexAppServerRuntime:
    """현재 Codex transport를 새 Engine port 뒤에 둔 local adapter.

    파일·네트워크를 별도로 축소하지 않는다. 대신 새 task를 만들기 직전 실제
    config와 thread receipt가 full-access/never인지 검증한다.
    """

    # Token usage는 terminal 뒤에도 부분/미제공일 수 있으므로 live 실행의
    # 선행조건으로 예산 정책을 요구하지 않는다. 각 요청의 absolute deadline과
    # Planning의 max-provider-call limit이 기본 운영 guard다.
    requires_budget_policy = False
    emits_rpc_operation_trace = True

    @property
    def project_binding(self) -> CodexProjectBinding | None:
        return getattr(self, "_project_binding", None)

    def __init__(
        self,
        *,
        codex_bin: Path | str | None = None,
        project_binding: CodexProjectBinding | None = None,
    ) -> None:
        from openai_codex import Codex
        from openai_codex.client import CodexConfig, _resolve_codex_bin

        resolved = (
            _resolve_codex_bin(CodexConfig()).resolve(strict=True)
            if codex_bin is None
            else Path(codex_bin).resolve(strict=True)
        )
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        self.executable_digest = "sha256:" + digest.hexdigest()
        self._codex_bin = resolved
        self._project_binding = project_binding
        self._codex = Codex(
            CodexConfig(
                codex_bin=None if codex_bin is None else str(resolved),
                client_name="flowmarshal_engine",
                client_title="FlowMarshal Engine",
                client_version="0.2.0a1",
                experimental_api=True,
            )
        )
        self._turn_futures: dict[str, tuple[Any, Future[Any]]] = {}
        self._ephemeral_thread_ids: set[str] = set()
        self._first_empty_threads: set[str] = set()
        self._new_thread_project_proofs: dict[str, _NewThreadProjectProof] = {}
        self._turn_usage_context: dict[str, dict[str, Any]] = {}
        self._completion_observers: dict[str, tuple[str, Callable[[RuntimeObservation], None]]] = {}
        self._completion_observer_lock = threading.Lock()
        self._interrupted_turn_ids: set[str] = set()
        self._thread_trace_scopes: dict[str, OperationTraceScope] = {}
        try:
            self._verify_project_binding()
        except BaseException:
            self._codex.close()
            raise

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        try:
            for thread_id, (handle, future) in tuple(self._turn_futures.items()):
                if future.done():
                    self._flush_completion_observer(thread_id)
                    continue
                interrupted_turn_ids = getattr(self, "_interrupted_turn_ids", set())
                if not hasattr(self, "_interrupted_turn_ids"):
                    self._interrupted_turn_ids = interrupted_turn_ids
                if handle.id not in interrupted_turn_ids:
                    # 요청을 보내기 전에 표시한다. timeout은 원격 미실행 증명이 아니므로
                    # close나 복구 경로가 같은 interrupt를 중복 전송하면 안 된다.
                    interrupted_turn_ids.add(handle.id)
                    try:
                        remaining = min(
                            timeout_seconds, max(0.0, deadline - time.monotonic())
                        )
                        trace_scope = getattr(self, "_thread_trace_scopes", {}).get(thread_id)
                        bounded_observation_call(
                            lambda: self._actual_rpc(
                                "sdk.turn/interrupt",
                                {"threadId": thread_id, "turnId": handle.id},
                                handle.interrupt,
                                thread_id=thread_id, turn_id=handle.id,
                                response_projection=lambda value: (
                                    value.model_dump(mode="json", by_alias=True)
                                    if hasattr(value, "model_dump") else {"completed": True}
                                ),
                                scope=trace_scope, detached=True,
                                deadline_seconds=remaining,
                            ),
                            timeout_seconds=remaining,
                            operation_name=f"turn/interrupt:{handle.id}",
                        )
                    except BaseException:
                        pass
                if thread_id in getattr(self, "_completion_observers", {}):
                    # 종료와 완료 이벤트의 경합에서도 도착한 usage를 버리지 않는다.
                    # 응답이 없는 연결을 무기한 기다리는 복구 보장은 하지 않는다.
                    try:
                        future.result(timeout=max(0.0, deadline - time.monotonic()))
                    except BaseException:
                        pass
                    self._flush_completion_observer(thread_id)
        finally:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    bounded_observation_call(
                        self._codex.close,
                        timeout_seconds=remaining,
                        operation_name="app-server/close",
                    )
                except BaseException:
                    pass
            else:
                # 전체 반환 기한은 넘기지 않되, 앞선 interrupt 대기로 기한을
                # 소진한 경우에도 transport cleanup 자체는 정확히 한 번 시작한다.
                threading.Thread(
                    target=self._codex.close,
                    name="flowmarshal-observe-app-server-close",
                    daemon=True,
                ).start()

    def register_completion_observer(
        self, *, thread_id: str, turn_id: str, observer: Callable[[RuntimeObservation], None],
    ) -> None:
        """start receipt가 원장에 기록된 뒤 완료 usage의 영속 관측자를 연결한다."""
        handle, _future = self._turn_futures[thread_id]
        if handle.id != turn_id:
            raise RuntimePolicyError("WORKER_USAGE_BINDING_MISMATCH: 완료 handle이 다릅니다.")
        if not hasattr(self, "_completion_observers"):
            self._completion_observers = {}
        if not hasattr(self, "_completion_observer_lock"):
            self._completion_observer_lock = threading.Lock()
        with self._completion_observer_lock:
            self._completion_observers[thread_id] = (turn_id, observer)
        _future.add_done_callback(lambda _completed: self._flush_completion_observer(thread_id))

    def _flush_completion_observer(self, thread_id: str) -> None:
        tracked = self._turn_futures.get(thread_id)
        if tracked is None or not tracked[1].done():
            return
        if not hasattr(self, "_completion_observer_lock"):
            self._completion_observer_lock = threading.Lock()
        with self._completion_observer_lock:
            entry = getattr(self, "_completion_observers", {}).get(thread_id)
            if entry is None:
                return
            observation = self.read(thread_id=thread_id)
            if observation.turn_id != entry[0] or observation.active:
                raise RuntimePolicyError("WORKER_USAGE_BINDING_MISMATCH: 완료 관측 turn이 다릅니다.")
            del self._completion_observers[thread_id]
        entry[1](observation)

    def wait_for_active_turns(self, *, timeout_seconds: float) -> bool:
        """CLI가 소유한 연결을 dispatch 직후 닫아 실행을 끊지 않도록 유지한다.

        usage 근거만 영속화한다. Task/Attempt 완료 판정은 다음 observe가 한다.
        """
        deadline = time.monotonic() + timeout_seconds
        for thread_id, (_handle, future) in tuple(self._turn_futures.items()):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                future.result(timeout=remaining)
            except TimeoutError:
                if not future.done():
                    return False
            except BaseException:
                # provider 오류도 종료 관측값이며 이 메서드에서 완료를 판정하지 않는다.
                if not future.done():
                    raise
            self._flush_completion_observer(thread_id)
        return True

    def __enter__(self) -> "CodexAppServerRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _rpc_kind(method: str) -> str:
        if method in {"thread/start", "sdk.thread/start"}:
            return "create"
        if method in {"turn/start", "sdk.turn/start"}:
            return "start"
        if method in {"thread/resume", "sdk.thread/resume"}:
            return "resume"
        if method in {"turn/interrupt", "sdk.turn/interrupt"}:
            return "interrupt"
        return "read"

    def _actual_rpc(
        self,
        method: str,
        params: dict[str, Any],
        invoke: Callable[[], Any],
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        response_projection: Callable[[Any], Any] | None = None,
        scope: OperationTraceScope | None = None,
        detached: bool = False,
        deadline_seconds: float | None = None,
    ) -> Any:
        active = scope or current_operation_trace_scope()
        if active is None:
            return invoke()
        remaining = deadline_seconds
        if remaining is None and active.deadline_monotonic_ns is not None:
            remaining = max(0.0, (active.deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)
        child_deadline_at = None
        if remaining is not None:
            child_deadline_at = utc_now() + timedelta(seconds=remaining)
            if not detached and active.deadline_at is not None:
                child_deadline_at = min(child_deadline_at, active.deadline_at)
        token = active.trace.begin(
            self._rpc_kind(method), {"method": method, "params": params},
            call_id=active.call_id, attempt_id=active.attempt_id,
            intent_id=active.intent_id,
            thread_id=thread_id or active.thread_id,
            turn_id=turn_id or active.turn_id,
            deadline_seconds=remaining, deadline_at=child_deadline_at, category="rpc",
            parent_operation_id=None if detached else active.parent.operation_id,
            rpc_method=method,
            detached=detached,
        )
        try:
            result = invoke()
            response = response_projection(result) if response_projection is not None else result
        except BaseException as error:
            active.trace.finish(
                token, error=error, attempt_id=active.attempt_id,
                intent_id=active.intent_id, thread_id=thread_id or active.thread_id,
                turn_id=turn_id or active.turn_id,
            )
            raise
        active.trace.finish(
            token, response=response, attempt_id=active.attempt_id,
            intent_id=active.intent_id, thread_id=thread_id or active.thread_id,
            turn_id=turn_id or active.turn_id,
        )
        return result

    def _raw(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        active = current_operation_trace_scope()
        # 5초 상한은 조회·중단에 적용한다. 생성·시작의 기존 실행 기한은 유지한다.
        rpc_timeout = 5.0 if self._rpc_kind(method) in {"read", "interrupt"} else None
        if active is not None and active.deadline_monotonic_ns is not None:
            remaining = max(0.0, (active.deadline_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)
            rpc_timeout = remaining if rpc_timeout is None else min(rpc_timeout, remaining)
        if rpc_timeout is not None and rpc_timeout <= 0:
            raise TimeoutError(f"{method}: 상위 operation 기한이 만료됐습니다.")

        def request_raw():
            operation = lambda: self._codex._client._request_raw(method, params)  # noqa: SLF001
            if rpc_timeout is None:
                return operation()
            return bounded_observation_call(
                operation, timeout_seconds=rpc_timeout, operation_name=method,
            )

        response = self._actual_rpc(
            method, params, request_raw,
            thread_id=params.get("threadId"), turn_id=params.get("turnId"),
            deadline_seconds=rpc_timeout,
        )
        if not isinstance(response, dict):
            raise RuntimePolicyError(f"{method} 응답이 JSON object가 아닙니다.")
        return response

    def _verify_project_binding(self) -> None:
        binding = getattr(self, "project_binding", None)
        if binding is None:
            return
        try:
            response = self._raw("project/read", {"projectId": binding.project_id})
        except Exception as error:
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: App Server project/read에 실패했습니다."
            ) from error
        project = response.get("project")
        roots = project.get("roots") if isinstance(project, dict) else None
        root_paths = (
            [item["path"] for item in roots]
            if isinstance(roots, list)
            and all(
                isinstance(item, dict) and isinstance(item.get("path"), str)
                for item in roots
            )
            else None
        )
        if (
            not isinstance(project, dict)
            or project.get("id") != binding.project_id
            or not Path(binding.expected_root).is_absolute()
            or root_paths is None
            or any(not Path(path).is_absolute() for path in root_paths)
            or sum(_same_path(path, binding.expected_root) for path in root_paths) != 1
            or (
                binding.expected_name is not None
                and project.get("name") != binding.expected_name
            )
        ):
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: App Server project가 고정 계약과 다릅니다."
            )

    def _verify_thread_project(
        self,
        thread_id: str,
        *,
        allow_new_empty_observation: bool = False,
        raw_reader: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> _NewThreadProjectProof | None:
        binding = getattr(self, "project_binding", None)
        if binding is None:
            return None
        proof = None
        if allow_new_empty_observation:
            proof = getattr(self, "_new_thread_project_proofs", {}).get(thread_id)
            if proof is not None and (
                proof.thread_id != thread_id or proof.project_binding != binding
            ):
                raise RuntimePolicyError(
                    "PROJECT_BINDING_MISMATCH: 최초 thread 생성 증명이 현재 계약과 다릅니다."
                )
        try:
            response = (raw_reader or self._raw)(
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": False,
                },
            )
        except Exception as error:
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: 저장 thread의 프로젝트를 읽지 못했습니다."
            ) from error
        thread = response.get("thread")
        exact_persisted = (
            isinstance(thread, dict)
            and thread.get("id") == thread_id
            and thread.get("projectId") == binding.project_id
        )
        exact_new_empty = (
            proof is not None
            and isinstance(thread, dict)
            and thread.get("id") == thread_id
            and thread.get("projectId") is None
            and thread.get("ephemeral") is False
            and thread.get("turns") == []
            and isinstance(thread.get("cwd"), str)
            and Path(thread["cwd"]).is_absolute()
            and _same_path(thread.get("cwd", ""), proof.cwd)
            and isinstance(thread.get("path"), str)
            and Path(thread["path"]).is_absolute()
            and _same_path(thread["path"], proof.rollout_path)
            and thread.get("status") in ({"type": "notLoaded"}, {"type": "idle"})
        )
        if not exact_persisted and not exact_new_empty:
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: 저장 thread가 고정 프로젝트에 결속되지 않았습니다."
            )
        return proof if exact_new_empty else None

    def _open_project_read_peer(self) -> "CodexAppServerRuntime":
        """같은 executable과 project 계약으로 독립 읽기 연결을 연다."""
        return CodexAppServerRuntime(
            codex_bin=self._codex_bin,
            project_binding=self.project_binding,
        )

    def _consume_new_thread_project_proof(
        self, thread_id: str, *, cwd: Path | str
    ) -> bool:
        """같은 연결에서 생성한 빈 thread의 첫 turn 증명을 한 번만 소비한다."""
        proof = getattr(self, "_new_thread_project_proofs", {}).pop(thread_id, None)
        if proof is None:
            return False
        binding = getattr(self, "project_binding", None)
        if (
            proof.thread_id != thread_id
            or proof.project_binding != binding
            or not _same_path(Path(cwd).resolve(), proof.cwd)
        ):
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: 최초 thread 생성 증명이 현재 계약과 다릅니다."
            )
        return True

    def verify_execution_policy(self, cwd: Path | str) -> ExecutionPolicyEvidence:
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: cwd가 디렉터리가 아닙니다.")
        response = self._raw("config/read", {"cwd": str(workspace), "includeLayers": True})
        config = response.get("config")
        if not isinstance(config, dict):
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: 실제 config를 읽지 못했습니다.")
        permission = config.get("default_permissions", config.get("defaultPermissions"))
        approval = config.get("approval_policy", config.get("approvalPolicy"))
        catalog_response = self._raw(
            "permissionProfile/list",
            {"cwd": str(workspace), "limit": 100},
        )
        catalog = catalog_response.get("data")
        if not isinstance(catalog, list) or catalog_response.get("nextCursor") is not None:
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: permission profile 목록이 불완전합니다.")
        allowed = any(
            isinstance(item, dict)
            and item.get("id") == REQUIRED_PERMISSION_PROFILE
            and item.get("allowed") is True
            for item in catalog
        )
        if (
            permission != REQUIRED_PERMISSION_PROFILE
            or approval != REQUIRED_APPROVAL_POLICY
            or not allowed
        ):
            raise RuntimePolicyError(
                "PERMISSION_POLICY_MISMATCH: 실제 정책이 "
                f"{REQUIRED_PERMISSION_PROFILE}/{REQUIRED_APPROVAL_POLICY}가 아닙니다: "
                f"{permission!r}/{approval!r}"
            )
        return ExecutionPolicyEvidence(
            permission_profile=permission,
            approval_policy=approval,
            config_digest=sha256_digest(config),
            profile_catalog_digest=sha256_digest(catalog),
            cwd=str(workspace),
        )

    def list_models(self) -> ModelInventory:
        # SDK typed response의 coercion보다 먼저 전체 원본 JSON을 검사한다.
        raw = self._raw("model/list", {"includeHidden": False})
        try:
            return ModelInventory(
                source=f"codex-app-server:model/list@{self.executable_digest}",
                models=parse_inventory_models(raw), raw_response=raw,
                executable_digest=self.executable_digest, runtime_capabilities=RUNTIME_CAPABILITIES,
            )
        except ValueError as error:
            raise RuntimePolicyError(f"INVALID_MODEL_INVENTORY: {error}") from error

    def create_thread(
        self,
        *,
        cwd: Path,
        title: str,
        model: str,
        developer_instructions: str,
        ephemeral: bool = False,
    ) -> RuntimeOperationReceipt:
        del title
        self._verify_project_binding()
        self.verify_execution_policy(cwd)
        params: dict[str, Any] = {
            "approvalPolicy": REQUIRED_APPROVAL_POLICY,
            "cwd": str(cwd.resolve()),
            "developerInstructions": developer_instructions,
            "ephemeral": ephemeral,
            "model": model,
            "permissions": REQUIRED_PERMISSION_PROFILE,
        }
        project_binding = getattr(self, "project_binding", None)
        if project_binding is not None:
            if ephemeral:
                raise RuntimePolicyError(
                    "PROJECT_BINDING_MISMATCH: 프로젝트 결속 thread는 저장형이어야 합니다."
                )
            params["projectId"] = project_binding.project_id
        response = self._raw(
            "thread/start",
            params,
        )
        profile = response.get("activePermissionProfile")
        active_profile = profile.get("id") if isinstance(profile, dict) else profile
        if (
            active_profile != REQUIRED_PERMISSION_PROFILE
            or response.get("approvalPolicy") != REQUIRED_APPROVAL_POLICY
        ):
            raise RuntimePolicyError("PERMISSION_POLICY_MISMATCH: thread receipt 정책이 다릅니다.")
        if not _same_path(response.get("cwd", ""), cwd):
            raise RuntimePolicyError("THREAD_PROVENANCE_MISMATCH: thread cwd가 다릅니다.")
        if response.get("model") != model:
            raise RuntimePolicyError("MODEL_PROVENANCE_MISMATCH: thread model이 다릅니다.")
        thread = response.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimePolicyError("thread/start receipt에 thread ID가 없습니다.")
        active_scope = current_operation_trace_scope()
        if active_scope is not None:
            self._thread_trace_scopes[thread_id] = active_scope
        if (
            project_binding is not None
            and thread.get("projectId") != project_binding.project_id
        ):
            raise RuntimePolicyError(
                "PROJECT_BINDING_MISMATCH: thread/start receipt의 프로젝트가 다릅니다."
            )
        if project_binding is not None:
            thread_path = thread.get("path")
            if (
                thread.get("ephemeral") is not False
                or thread.get("turns") != []
                or not isinstance(thread_path, str)
                or not Path(thread_path).is_absolute()
            ):
                raise RuntimePolicyError(
                    "PROJECT_BINDING_MISMATCH: 최초 thread 생성 증명이 절대 rollout 경로를 "
                    "가진 저장형 빈 thread가 아닙니다."
                )
            if not hasattr(self, "_new_thread_project_proofs"):
                self._new_thread_project_proofs = {}
            self._new_thread_project_proofs[thread_id] = _NewThreadProjectProof(
                thread_id=thread_id,
                project_binding=project_binding,
                cwd=str(cwd.resolve()),
                rollout_path=thread_path,
                receipt_digest=sha256_digest(response),
            )
        binding = ThreadBinding(thread_id=thread_id, bound_at=utc_now())
        if ephemeral:
            self._ephemeral_thread_ids.add(thread_id)
        if thread.get("turns") == []:
            self._first_empty_threads.add(thread_id)
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload=response,
            binding=binding,
        )

    @staticmethod
    def _notification_document(event: Any) -> dict[str, Any]:
        payload = getattr(event, "payload", None)
        if hasattr(payload, "model_dump"):
            document = payload.model_dump(mode="json", by_alias=True)
        elif isinstance(payload, dict):
            document = dict(payload)
        else:
            document = {"value": str(payload)}
        return document if isinstance(document, dict) else {"value": document}

    def _record_turn_event(self, *, thread_id: str, turn_id: str, event: Any) -> None:
        """SDK collector가 failed turn에서 버리는 terminal·usage 근거를 보존한다."""
        context = self._turn_usage_context[thread_id]
        method = str(getattr(event, "method", "unknown"))
        document = self._notification_document(event)
        nested_turn = document.get("turn")
        observed_thread_id = document.get("threadId", document.get("thread_id"))
        observed_turn_id = document.get("turnId", document.get("turn_id"))
        if isinstance(nested_turn, dict):
            observed_turn_id = nested_turn.get("id", observed_turn_id)
        if observed_thread_id is not None and observed_thread_id != thread_id:
            raise RuntimePolicyError(
                "RUNTIME_OBSERVATION_BINDING_MISMATCH: provider event의 thread가 다릅니다."
            )
        if observed_turn_id is not None and observed_turn_id != turn_id:
            raise RuntimePolicyError(
                "RUNTIME_OBSERVATION_BINDING_MISMATCH: provider event의 turn이 다릅니다."
            )

        observed_at = utc_now().isoformat()
        lifecycle = context["lifecycle"]
        if lifecycle["first_event_at"] is None:
            lifecycle["first_event_at"] = observed_at
        lifecycle["last_event_at"] = observed_at
        lifecycle["event_count"] += 1
        last_event = {
            "method": method,
            "observed_at": observed_at,
            "thread_id": observed_thread_id,
            "turn_id": observed_turn_id,
        }
        lifecycle["last_event"] = last_event

        provider_document: dict[str, Any] | None = None
        if method == "thread/tokenUsage/updated":
            provider_document = document
            context["usage"] = document.get("tokenUsage", document.get("token_usage"))
            context["usage_scope"] = "thread"
            context["usage_source"] = "thread/tokenUsage/updated"
        elif method == "turn/completed":
            provider_turn = dict(nested_turn) if isinstance(nested_turn, dict) else {}
            provider_document = document
            lifecycle["provider_started_at"] = provider_turn.get("startedAt")
            lifecycle["provider_completed_at"] = provider_turn.get("completedAt")
            lifecycle["provider_duration_ms"] = provider_turn.get("durationMs")
            lifecycle["terminal_status"] = provider_turn.get("status")
            lifecycle["terminal_error"] = provider_turn.get("error")
        elif method == "error":
            provider_document = document
            lifecycle["terminal_error"] = document

        if provider_document is not None:
            context["provider_events"].append(
                {
                    "method": method,
                    "observed_at": observed_at,
                    "payload": provider_document,
                }
            )

    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt:
        from openai_codex import ApprovalMode, Sandbox
        from openai_codex.api import Thread

        if not self._consume_new_thread_project_proof(thread_id, cwd=cwd):
            self._verify_thread_project(thread_id)
        self.verify_execution_policy(cwd)
        # thread/start 직후의 ephemeral 역할 thread는 영속 rollout이 없으므로
        # thread/resume 대상이 아니다. 같은 App Server 연결의 기존 thread ID에
        # turn/start를 직접 보내고, 실제 중단 후 재개만 resume()에서 처리한다.
        thread = Thread(self._codex._client, thread_id)  # noqa: SLF001
        turn_arguments: dict[str, Any] = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(cwd.resolve()),
            "effort": effort,
            "model": model,
            "sandbox": Sandbox.full_access,
        }
        if output_schema is not None:
            turn_arguments["output_schema"] = output_schema
        # SDK에 넘기는 바로 이 문자열의 digest를 provider start receipt와 함께 보존한다.
        prompt_digest = sha256_digest(prompt)
        first_empty_thread = thread_id in getattr(self, "_first_empty_threads", set())
        handle = self._actual_rpc(
            "sdk.turn/start",
            {"threadId": thread_id, "promptDigest": prompt_digest, **{
                key: (value.value if hasattr(value, "value") else value)
                for key, value in turn_arguments.items()
            }},
            lambda: thread.turn(prompt, **turn_arguments),
            thread_id=thread_id,
            response_projection=lambda value: {"turnId": value.id},
        )
        active_scope = current_operation_trace_scope()
        if active_scope is not None:
            self._thread_trace_scopes[thread_id] = active_scope
        getattr(self, "_first_empty_threads", set()).discard(thread_id)
        if not hasattr(self, "_turn_usage_context"):
            self._turn_usage_context = {}
        self._turn_usage_context[thread_id] = {
            "prompt_digest": prompt_digest,
            "provider_events": [],
            "lifecycle": {
                "observation_started_at": utc_now().isoformat(),
                "first_event_at": None,
                "last_event_at": None,
                "event_count": 0,
                "last_event": None,
                "provider_started_at": None,
                "provider_completed_at": None,
                "provider_duration_ms": None,
                "terminal_status": None,
                "terminal_error": None,
            },
        }
        future: Future[Any] = Future()

        def consume_turn() -> None:
            from openai_codex._run import _collect_turn_result

            def observed_stream():
                for event in handle.stream():
                    self._record_turn_event(
                        thread_id=thread_id, turn_id=handle.id, event=event,
                    )
                    yield event

            try:
                future.set_result(_collect_turn_result(observed_stream(), turn_id=handle.id))
            except BaseException as error:
                future.set_exception(error)

        threading.Thread(
            target=consume_turn,
            name=f"flowmarshal-turn-{handle.id}",
            daemon=True,
        ).start()
        self._turn_futures[thread_id] = (handle, future)
        binding = ThreadBinding(thread_id=thread_id, turn_id=handle.id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=handle.id,
            payload={
                "thread_id": thread_id,
                "turn_id": handle.id,
                "model": model,
                "effort": effort,
                "requested_model": model,
                "requested_effort": effort,
                "model_provenance": "requested",
                "effort_provenance": "requested",
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
                "prompt_digest": prompt_digest,
                "first_empty_thread": first_empty_thread,
            },
            binding=binding,
        )

    def read(self, *, thread_id: str) -> RuntimeObservation:
        tracked = self._turn_futures.get(thread_id)
        if tracked is not None:
            handle, future = tracked
            if not future.done():
                usage_context = getattr(self, "_turn_usage_context", {}).get(thread_id, {})
                return RuntimeObservation(
                    thread_id=thread_id,
                    turn_id=handle.id,
                    active=True,
                    payload={
                        "thread_id": thread_id,
                        "turn_id": handle.id,
                        "usage": None,
                        **usage_context,
                    },
                )
            try:
                turn_result = future.result()
            except BaseException as error:
                usage_context = getattr(self, "_turn_usage_context", {}).get(thread_id, {})
                lifecycle = usage_context.get("lifecycle", {})
                return RuntimeObservation(
                    thread_id=thread_id,
                    turn_id=handle.id,
                    active=False,
                    terminal_status=lifecycle.get("terminal_status"),
                    final_response=str(error),
                    payload={
                        "thread_id": thread_id,
                        "turn_id": handle.id,
                        "error": f"{type(error).__name__}: {error}",
                        "usage": None,
                        "usage_source": "sdk.turn_result.error",
                        **usage_context,
                    },
                )
            if turn_result.id != handle.id:
                raise RuntimePolicyError(
                    "RUNTIME_OBSERVATION_BINDING_MISMATCH: SDK 결과 turn이 다릅니다."
                )
            usage = turn_result.usage
            usage_document = (
                usage.model_dump(mode="json", by_alias=True)
                if hasattr(usage, "model_dump")
                else usage
            )
            return RuntimeObservation(
                thread_id=thread_id,
                turn_id=turn_result.id,
                active=False,
                terminal_status=_enum(turn_result.status),
                final_response=turn_result.final_response,
                payload={
                    "thread_id": thread_id,
                    "turn_id": turn_result.id,
                    "turn_status": _enum(turn_result.status),
                    "duration_ms": turn_result.duration_ms,
                    "item_count": len(turn_result.items),
                    "usage": usage_document,
                    "usage_scope": "thread",
                    "usage_source": "thread/tokenUsage/updated",
                    **getattr(self, "_turn_usage_context", {}).get(thread_id, {}),
                },
            )
        if thread_id in self._ephemeral_thread_ids:
            raise RuntimePolicyError("ephemeral thread의 active turn handle이 없습니다.")
        return self.read_stored(thread_id=thread_id)

    def operation_kind_for_read(self, thread_id: str) -> str:
        return "sdk_wait" if thread_id in self._turn_futures else "read"

    def read_stored(
        self,
        *,
        thread_id: str,
        turn_id: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> RuntimeObservation:
        """저장 상태를 유한 시간에 읽고, 지정한 경우 정확한 과거 turn만 반환한다."""
        active_scope = current_operation_trace_scope()

        def read_with_scope():
            if active_scope is None:
                return self._read_stored_impl(thread_id=thread_id, turn_id=turn_id)
            with use_operation_trace_scope(active_scope):
                return self._read_stored_impl(thread_id=thread_id, turn_id=turn_id)

        return bounded_observation_call(
            read_with_scope,
            timeout_seconds=timeout_seconds,
            operation_name=f"read_stored:{thread_id}:{turn_id or 'latest'}",
        )

    def _read_exact_turn_history(
        self, *, thread_id: str,
    ) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]:
        """모든 순방향 페이지를 검증해 turn ID 중복·누락을 숨기지 않는다."""
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_turn_ids: set[str] = set()
        turns: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        while True:
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "sortDirection": "asc",
                "itemsView": "full",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = self._raw("thread/turns/list", params)
            data = response.get("data")
            next_cursor = response.get("nextCursor")
            backwards_cursor = response.get("backwardsCursor")
            if (
                not isinstance(data, list)
                or (next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor))
                or (
                    backwards_cursor is not None
                    and (not isinstance(backwards_cursor, str) or not backwards_cursor)
                )
            ):
                raise RuntimePolicyError(
                    "RUNTIME_OBSERVATION_PAGINATION_INCOMPLETE: turn 목록 page가 유효하지 않습니다."
                )
            page_turn_ids: list[str] = []
            for turn in data:
                if not isinstance(turn, dict):
                    raise RuntimePolicyError(
                        "RUNTIME_OBSERVATION_PAGINATION_INCOMPLETE: turn 항목이 object가 아닙니다."
                    )
                listed_turn_id = turn.get("id")
                if not isinstance(listed_turn_id, str) or not listed_turn_id:
                    raise RuntimePolicyError(
                        "RUNTIME_OBSERVATION_BINDING_MISMATCH: 저장 turn ID가 없습니다."
                    )
                if listed_turn_id in seen_turn_ids:
                    raise RuntimePolicyError(
                        "RUNTIME_OBSERVATION_BINDING_MISMATCH: turn ID가 pagination에서 중복됐습니다."
                    )
                if turn.get("itemsView") in {"summary", "notLoaded"}:
                    raise RuntimePolicyError(
                        "RUNTIME_OBSERVATION_PAGINATION_INCOMPLETE: full turn 항목을 받지 못했습니다."
                    )
                seen_turn_ids.add(listed_turn_id)
                page_turn_ids.append(listed_turn_id)
                turns.append(turn)
            pages.append(
                {
                    "params": params,
                    "turn_ids": page_turn_ids,
                    "next_cursor": next_cursor,
                    "backwards_cursor": backwards_cursor,
                }
            )
            if next_cursor is None:
                break
            if not data or next_cursor == cursor or next_cursor in seen_cursors:
                raise RuntimePolicyError(
                    "RUNTIME_OBSERVATION_PAGINATION_INCOMPLETE: turn 목록 cursor가 진행하지 않습니다."
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return tuple(turns), pages

    def _read_stored_impl(
        self, *, thread_id: str, turn_id: str | None,
    ) -> RuntimeObservation:
        """daemon watchdog 안에서 실행되는 저장 상태 조회 본체."""
        empty_creation_proof = self._verify_thread_project(
            thread_id,
            allow_new_empty_observation=True,
        )
        turn_history_params: dict[str, Any] | None = None
        turn_history_response: dict[str, Any] | None = None
        read_retry_errors: list[dict[str, Any]] = []
        materialization_read: dict[str, Any] | None = None
        independent_reader_executable_digest: str | None = None
        turn_history_pages: list[dict[str, Any]] | None = None
        if empty_creation_proof is not None:
            from openai_codex import MethodNotFoundError

            turn_history_params = {
                "threadId": thread_id,
                "limit": 1,
                "sortDirection": "asc",
                "itemsView": "full",
            }
            materialization_params = {
                "threadId": thread_id,
                "includeTurns": True,
            }
            try:
                materialized = self._actual_rpc(
                    "sdk.thread/read", materialization_params,
                    lambda: self._codex._client.thread_read(  # noqa: SLF001
                        thread_id, include_turns=True
                    ),
                    thread_id=thread_id,
                )
                materialized_turns = tuple(materialized.thread.turns)
                if str(materialized.thread.id) != thread_id or materialized_turns:
                    raise RuntimePolicyError(
                        "PROJECT_BINDING_MISMATCH: owner materialization read가 동일한 "
                        "빈 thread를 반환하지 않았습니다."
                    )
                if not hasattr(materialized, "model_dump"):
                    raise RuntimePolicyError(
                        "PROJECT_BINDING_MISMATCH: owner materialization receipt를 "
                        "직렬화할 수 없습니다."
                    )
                materialization_read = {
                    "method": "thread/read",
                    "params": materialization_params,
                    "response": materialized.model_dump(mode="json", by_alias=True),
                    "error": None,
                }
            except Exception as error:
                if isinstance(error, RuntimePolicyError):
                    raise
                if (
                    type(error) is not MethodNotFoundError
                    or error.code != -32601
                    or error.message != "list_turns is not supported yet"
                ):
                    raise
                materialization_read = {
                    "method": "thread/read",
                    "params": materialization_params,
                    "response": None,
                    "error": {
                        "type": type(error).__name__,
                        "code": error.code,
                        "message": error.message,
                    },
                }
            with self._open_project_read_peer() as peer:
                if peer.executable_digest != self.executable_digest:
                    raise RuntimePolicyError(
                        "PROJECT_BINDING_MISMATCH: 독립 reader executable이 원본과 다릅니다."
                    )
                peer_proof = self._verify_thread_project(
                    thread_id,
                    allow_new_empty_observation=True,
                    raw_reader=peer._raw,
                )
                if peer_proof != empty_creation_proof:
                    raise RuntimePolicyError(
                        "PROJECT_BINDING_MISMATCH: 독립 reader의 최초 metadata 증명이 다릅니다."
                    )
                turn_history_response = peer._raw(
                    "thread/turns/list", turn_history_params
                )
                repeated_proof = self._verify_thread_project(
                    thread_id,
                    allow_new_empty_observation=True,
                    raw_reader=peer._raw,
                )
                if repeated_proof != empty_creation_proof:
                    raise RuntimePolicyError(
                        "PROJECT_BINDING_MISMATCH: 독립 reader의 재확인 증명이 다릅니다."
                    )
                independent_reader_executable_digest = peer.executable_digest
            if turn_history_response != {
                "data": [],
                "nextCursor": None,
                "backwardsCursor": None,
            }:
                raise RuntimePolicyError(
                    "PROJECT_BINDING_MISMATCH: 빈 thread의 paginated turn 목록이 비어 있지 않습니다."
                )
            turns: tuple[dict[str, Any], ...] = ()
        elif turn_id is not None:
            turns, turn_history_pages = self._read_exact_turn_history(thread_id=thread_id)
            turn_history_params = {
                "threadId": thread_id,
                "limit": 100,
                "sortDirection": "asc",
                "itemsView": "full",
            }
        else:
            result = self._actual_rpc(
                "sdk.thread/read", {"threadId": thread_id, "includeTurns": True},
                lambda: self._codex._client.thread_read(  # noqa: SLF001
                    thread_id, include_turns=True
                ),
                thread_id=thread_id,
            )
            turn_documents: list[dict[str, Any]] = []
            for item in result.thread.turns:
                document = item.model_dump(mode="json", by_alias=True)
                document.setdefault("id", item.id)
                document.setdefault("status", _enum(item.status))
                document.setdefault(
                    "items",
                    [
                        value.model_dump(mode="json", by_alias=True)
                        if hasattr(value, "model_dump")
                        else value
                        for value in item.items
                    ],
                )
                turn_documents.append(document)
            turns = tuple(turn_documents)
        if turn_id is None:
            selected = None if not turns else turns[-1]
        else:
            matches = tuple(item for item in turns if item.get("id") == turn_id)
            if len(matches) != 1:
                raise RuntimePolicyError(
                    "RUNTIME_OBSERVATION_BINDING_MISMATCH: 요청한 exact turn을 정확히 찾지 못했습니다."
                )
            selected = matches[0]
        status = None if selected is None else _enum(selected.get("status"))
        final_response: str | None = None
        if selected is not None:
            selected_document = dict(selected)
            for item in selected.get("items", []):
                document = (
                    item.model_dump(mode="json", by_alias=True)
                    if hasattr(item, "model_dump")
                    else item
                )
                if document.get("type") == "agentMessage" and isinstance(document.get("text"), str):
                    final_response = document["text"]
        else:
            selected_document = {}
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=None if selected is None else selected.get("id"),
            active=status in {"inProgress", "in_progress"},
            terminal_status=None if selected is None or status in {"inProgress", "in_progress"} else status,
            final_response=final_response,
            payload={
                "thread_id": thread_id,
                "requested_turn_id": turn_id,
                "turn_count": len(turns),
                "turn_status": status,
                "provider_turn": selected_document,
                "usage": selected_document.get("usage"),
                "usage_scope": "turn" if selected_document.get("usage") is not None else "unavailable",
                "usage_source": (
                    "thread/turns/list"
                    if empty_creation_proof is not None or turn_id is not None
                    else "thread/read"
                ),
                "turn_history_available": True,
                "turn_history_error": None,
                "turn_history_source": (
                    "thread/turns/list"
                    if empty_creation_proof is not None or turn_id is not None
                    else "thread/read(includeTurns=true)"
                ),
                "turn_history_params": turn_history_params,
                "turn_history_response": turn_history_response,
                "turn_history_pages": turn_history_pages,
                "read_retry_errors": read_retry_errors,
                "turn_history_connection": (
                    "independent_app_server"
                    if empty_creation_proof is not None
                    else ("owner_app_server" if turn_id is not None else None)
                ),
                "lifecycle": {
                    "provider_started_at": selected_document.get("startedAt"),
                    "provider_completed_at": selected_document.get("completedAt"),
                    "provider_duration_ms": selected_document.get("durationMs"),
                    "terminal_status": status,
                    "terminal_error": selected_document.get("error"),
                },
                "independent_reader_executable_digest": (
                    independent_reader_executable_digest
                ),
                "materialization_read": materialization_read,
                "project_id": (
                    None
                    if empty_creation_proof is not None
                    else (
                        self.project_binding.project_id
                        if self.project_binding is not None
                        else None
                    )
                ),
                "project_binding_verification": (
                    {
                        "source": "same_connection_thread_start_receipt_and_empty_turns_list",
                        "creation_receipt_digest": empty_creation_proof.receipt_digest,
                        "creation_project_id": empty_creation_proof.project_binding.project_id,
                        "rollout_path": empty_creation_proof.rollout_path,
                        "raw_metadata_source": "thread/read(includeTurns=false)",
                        "raw_metadata_connection": "independent_app_server",
                        "raw_metadata_confirmation_count": 2,
                    }
                    if empty_creation_proof is not None
                    else (
                        {"source": "thread/read.projectId"}
                        if self.project_binding is not None
                        else {"source": "unbound"}
                    )
                ),
            },
        )

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt:
        self._verify_thread_project(thread_id)
        self.verify_execution_policy(cwd)
        thread = self._actual_rpc(
            "sdk.thread/resume", {"threadId": thread_id, "cwd": str(cwd.resolve())},
            lambda: self._codex.thread_resume(thread_id, cwd=str(cwd.resolve())),
            thread_id=thread_id,
            response_projection=lambda value: {"threadId": value.id},
        )
        return RuntimeOperationReceipt(
            operation_id=thread.id,
            payload={"thread_id": thread.id, "cwd": str(cwd.resolve()), "resumed": True},
            binding=ThreadBinding(thread_id=thread.id, bound_at=utc_now()),
        )

    def interrupt(
        self, *, thread_id: str, turn_id: str, timeout_seconds: float = 5.0,
    ) -> RuntimeOperationReceipt:
        # 실행 중인 turn을 중단하기 위해 저장 thread를 다시 resume하지 않는다.
        interrupted_turn_ids = getattr(self, "_interrupted_turn_ids", set())
        if not hasattr(self, "_interrupted_turn_ids"):
            self._interrupted_turn_ids = interrupted_turn_ids
        if turn_id in interrupted_turn_ids:
            raise RuntimePolicyError(
                "RUNTIME_INTERRUPT_ALREADY_REQUESTED: 같은 turn에 interrupt를 다시 보내지 않습니다."
            )
        interrupted_turn_ids.add(turn_id)
        active_scope = current_operation_trace_scope()

        def interrupt_with_scope():
            if active_scope is None:
                return self._raw(
                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                )
            with use_operation_trace_scope(active_scope):
                return self._raw(
                    "turn/interrupt", {"threadId": thread_id, "turnId": turn_id},
                )

        response = bounded_observation_call(
            interrupt_with_scope,
            timeout_seconds=timeout_seconds,
            operation_name=f"turn/interrupt:{turn_id}",
        )
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "interrupted": True,
                "response": response,
            },
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )


@dataclass
class _FakeThread:
    thread_id: str
    cwd: Path
    turn_id: str | None = None
    terminal_status: str | None = None
    final_response: str | None = None
    provider_payload: dict[str, Any] = field(default_factory=dict)


class FakeCodexRuntime:
    """중복·재시작 검사를 위한 full-access/never 결정적 runtime."""

    def __init__(self, inventory: ModelInventory) -> None:
        self.inventory = inventory
        self.threads: dict[str, _FakeThread] = {}
        self.create_calls = 0
        self.turn_calls = 0
        self.read_calls = 0
        self.resume_calls = 0
        self.interrupt_calls = 0

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        del timeout_seconds
        return None

    def verify_execution_policy(self, cwd: Path | str) -> ExecutionPolicyEvidence:
        workspace = Path(cwd).resolve()
        return ExecutionPolicyEvidence(
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            config_digest=sha256_digest({"permission": REQUIRED_PERMISSION_PROFILE, "approval": REQUIRED_APPROVAL_POLICY}),
            profile_catalog_digest=sha256_digest([REQUIRED_PERMISSION_PROFILE]),
            cwd=str(workspace),
        )

    def list_models(self) -> ModelInventory:
        return self.inventory

    def create_thread(
        self,
        *,
        cwd: Path,
        title: str,
        model: str,
        developer_instructions: str,
        ephemeral: bool = False,
    ) -> RuntimeOperationReceipt:
        del title, model, developer_instructions, ephemeral
        self.verify_execution_policy(cwd)
        self.create_calls += 1
        thread_id = new_id("runtime_thread")
        self.threads[thread_id] = _FakeThread(thread_id=thread_id, cwd=cwd.resolve())
        binding = ThreadBinding(thread_id=thread_id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={"thread_id": thread_id, "thread": {"id": thread_id, "turns": []}},
            binding=binding,
        )


    def start_turn(
        self,
        *,
        thread_id: str,
        cwd: Path,
        prompt: str,
        model: str,
        effort: str,
        output_schema: dict[str, Any] | None = None,
    ) -> RuntimeOperationReceipt:
        del model, effort, output_schema
        self.verify_execution_policy(cwd)
        self.turn_calls += 1
        thread = self.threads[thread_id]
        first_empty_thread = thread.turn_id is None
        turn_id = new_id("runtime_turn")
        thread.turn_id = turn_id
        thread.terminal_status = None
        thread.final_response = None
        thread.provider_payload = {}
        binding = ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "prompt_digest": sha256_digest(prompt),
                     "first_empty_thread": first_empty_thread, "permission_profile": REQUIRED_PERMISSION_PROFILE,
                     "approval_policy": REQUIRED_APPROVAL_POLICY},
            binding=binding,
        )

    def complete(self, thread_id: str, *, response: str = "완료") -> None:
        self.threads[thread_id].terminal_status = "completed"
        self.threads[thread_id].final_response = response

    def fail(
        self, thread_id: str, *, response: str = "실패", error_code: str | None = None,
    ) -> None:
        self.threads[thread_id].terminal_status = "failed"
        self.threads[thread_id].final_response = response
        self.threads[thread_id].provider_payload = (
            {} if error_code is None else {"error_code": error_code}
        )

    def read(self, *, thread_id: str) -> RuntimeObservation:
        self.read_calls += 1
        thread = self.threads[thread_id]
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=thread.turn_id,
            active=thread.turn_id is not None and thread.terminal_status is None,
            terminal_status=thread.terminal_status,
            final_response=thread.final_response,
            payload={
                "thread_id": thread_id,
                "turn_id": thread.turn_id,
                **thread.provider_payload,
            },
        )

    def operation_kind_for_read(self, thread_id: str) -> str:
        del thread_id
        return "read"

    def read_stored(
        self, *, thread_id: str, turn_id: str | None = None, timeout_seconds: float = 5.0,
    ) -> RuntimeObservation:
        del timeout_seconds
        self.read_calls += 1
        thread = self.threads[thread_id]
        if turn_id is not None and thread.turn_id != turn_id:
            raise RuntimePolicyError(
                "RUNTIME_OBSERVATION_BINDING_MISMATCH: 요청한 exact turn을 찾지 못했습니다."
            )
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=thread.turn_id,
            active=thread.turn_id is not None and thread.terminal_status is None,
            terminal_status=thread.terminal_status,
            final_response=thread.final_response,
            payload={
                "thread_id": thread_id,
                "requested_turn_id": turn_id,
                "turn_count": 0 if thread.turn_id is None else 1,
                "turn_status": thread.terminal_status,
                "usage": None,
                "usage_scope": "unavailable",
                **thread.provider_payload,
                "usage_source": "thread/read",
                "turn_history_available": True,
                "turn_history_source": "thread/read(includeTurns=true)",
            },
        )

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt:
        self.resume_calls += 1
        thread = self.threads[thread_id]
        if thread.cwd != cwd.resolve():
            raise RuntimeError("thread cwd가 다릅니다.")
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload={"thread_id": thread_id, "resumed": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=thread.turn_id, bound_at=utc_now()),
        )

    def interrupt(
        self, *, thread_id: str, turn_id: str, timeout_seconds: float = 5.0,
    ) -> RuntimeOperationReceipt:
        del timeout_seconds
        thread = self.threads[thread_id]
        if thread.turn_id != turn_id:
            raise KeyError(turn_id)
        self.interrupt_calls += 1
        thread.terminal_status = "interrupted"
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupted": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )


class RuntimeJobSupervisor:
    """활성 RuntimeJob 동안만 SDK 연결과 관측 수명을 소유한다.

    provider 결과를 저장할 뿐 Attempt·Task·Goal 완료를 판정하지 않는다. collector
    종료나 interrupt receipt도 provider terminal로 승격하지 않는다.
    """

    def __init__(self, service: EngineService, runtime: CodexRuntimePort, *,
                 interrupt_timeout_seconds: float = 5.0,
                 handoff_wait_seconds: float = 0.05,
                 observation_timeout_seconds: float = 0.05,
                 terminal_observation_grace_seconds: float = 30.0) -> None:
        if (interrupt_timeout_seconds <= 0 or handoff_wait_seconds < 0
                or observation_timeout_seconds <= 0
                or terminal_observation_grace_seconds < 0):
            raise ValueError("supervisor bounded wait 값이 유효하지 않습니다.")
        self.service = service
        self.runtime = runtime
        self.interrupt_timeout_seconds = interrupt_timeout_seconds
        self.handoff_wait_seconds = handoff_wait_seconds
        self.observation_timeout_seconds = observation_timeout_seconds
        self.terminal_observation_grace_seconds = terminal_observation_grace_seconds
        self._workers: dict[str, threading.Thread] = {}
        self._results: dict[str, tuple[bool, Any]] = {}
        self._result_events: dict[str, threading.Event] = {}
        self._complete_on_return: dict[str, bool] = {}
        self._terminal_progress: dict[str, dict[str, Any]] = {}
        self._owned_job_ids: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _json_value(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            return value
        raise TypeError(f"runtime job 결과를 JSON으로 보존할 수 없습니다: {type(value).__name__}")

    def schedule(self, *, project_id: str, kind: RuntimeJobKind, checkpoint_key: str,
                 request: dict[str, Any], timeout_seconds: float, target: Callable[[], Any],
                 attempt_id: str | None = None, task_id: str | None = None,
                 complete_on_return: bool = True) -> RuntimeJob:
        if timeout_seconds <= 0:
            raise ValueError("runtime job timeout은 양수여야 합니다.")
        job = self.service.schedule_runtime_job(
            project_id=project_id, kind=kind, checkpoint_key=checkpoint_key,
            request=request, absolute_deadline_at=utc_now() + timedelta(seconds=timeout_seconds),
            attempt_id=attempt_id, task_id=task_id,
        )
        self._owned_job_ids.add(job.job_id)
        if job.status is RuntimeJobStatus.SCHEDULED:
            job = self._start_worker(job, target, complete_on_return=complete_on_return)
        # 예약/시작 tick에서는 빠른 target의 결과도 소비하지 않는다. 다음 tick이
        # 정확히 한 관측을 durable 상태로 옮긴다.
        return job

    def _start_worker(
        self,
        job: RuntimeJob,
        target: Callable[[], Any],
        *,
        complete_on_return: bool,
    ) -> RuntimeJob:
        with self._lock:
            current = self._workers.get(job.job_id)
            if current is not None and current.is_alive():
                return job
            event = threading.Event()
            self._result_events[job.job_id] = event
            self._complete_on_return[job.job_id] = complete_on_return

            def execute() -> None:
                token = _ACTIVE_RUNTIME_JOB.set((self, job.job_id))
                try:
                    outcome = (True, self._json_value(target()))
                except BaseException as error:
                    outcome = (False, {"error_type": type(error).__name__, "error": str(error)})
                finally:
                    _ACTIVE_RUNTIME_JOB.reset(token)
                if outcome[0] and complete_on_return:
                    try:
                        current = self.service.load_runtime_job(job.job_id)
                        self.service.record_runtime_job_observation(
                            job.job_id,
                            kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                            payload={
                                "target_result_checkpoint_version": "1.0",
                                "target_result": outcome[1],
                                "target_result_digest": sha256_digest(outcome[1]),
                                "job_request_digest": current.request_digest,
                                "attempt_id": current.attempt_id,
                                "thread_id": current.thread_id,
                                "turn_id": current.turn_id,
                            },
                        )
                    except BaseException as error:
                        outcome = (
                            False,
                            {
                                "error_type": type(error).__name__,
                                "error": str(error),
                                "stage": "target_result_checkpoint",
                            },
                        )
                with self._lock:
                    if not outcome[0] or complete_on_return:
                        self._results[job.job_id] = outcome
                    event.set()

            job = self.service.start_runtime_job(job.job_id)
            worker = threading.Thread(target=execute,
                name=f"flowmarshal-{job.kind.value}-{job.job_id[-8:]}", daemon=True)
            self._workers[job.job_id] = worker
            worker.start()
            return job

    def record_role_progress(self, job_id: str, event: dict[str, Any]) -> None:
        """현재 role call의 exact provider binding과 lifecycle을 durable하게 남긴다."""

        event_name = event.get("event")
        thread_id = event.get("thread_id")
        turn_id = event.get("turn_id")
        if isinstance(thread_id, str) and isinstance(turn_id, str):
            self.bind_provider_turn(
                job_id, thread_id=thread_id, turn_id=turn_id,
            )
        payload = {"role_progress": self._json_value(event)}
        self.service.record_runtime_job_observation(
            job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS, payload=payload,
        )
        if event_name in {
            "role_terminal_observed", "terminal_observed_after_interrupt"
        }:
            with self._lock:
                self._terminal_progress[job_id] = event

    def _durable_target_result(self, job_id: str) -> tuple[bool, Any]:
        """background target의 반환값을 append-only checkpoint에서 검증해 읽는다."""

        job = self.service.load_runtime_job(job_id)
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runtime_job_observations "
                "WHERE job_id=? AND kind=? ORDER BY rowid DESC",
                (job_id, RuntimeJobObservationKind.PROVIDER_PROGRESS.value),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if payload.get("target_result_checkpoint_version") != "1.0":
                continue
            result = payload.get("target_result")
            if payload.get("target_result_digest") != sha256_digest(result):
                raise RuntimePolicyError(
                    "RUNTIME_JOB_RESULT_CHECKPOINT_MISMATCH: 저장 결과 digest가 다릅니다."
                )
            if (
                payload.get("job_request_digest") != job.request_digest
                or payload.get("attempt_id") != job.attempt_id
                or payload.get("thread_id") != job.thread_id
                or payload.get("turn_id") != job.turn_id
            ):
                raise RuntimePolicyError(
                    "RUNTIME_JOB_RESULT_CHECKPOINT_BINDING_MISMATCH: "
                    "저장 결과가 현재 job/Attempt/exact turn과 다릅니다."
                )
            return True, result
        return False, None

    def _terminal_result_from_observation(
        self,
        job: RuntimeJob,
        observation: RuntimeObservation,
    ) -> Any:
        """exact provider raw 응답만 사용해 재시작 결과를 결정적으로 복원한다."""

        if job.kind in {
            RuntimeJobKind.WORKER_TURN,
            RuntimeJobKind.TASK_SEMANTIC_VALIDATE,
        }:
            return self._json_value(observation)
        try:
            result = json.loads(observation.final_response or "")
        except (TypeError, ValueError):
            result = None
        if isinstance(result, dict):
            return result
        return {
            "runtime_job_result_unavailable": True,
            "provider_observation": self._json_value(observation),
        }

    def tick(self, job_id: str, *, wait_seconds: float = 0.0) -> RuntimeJob:
        """최대 한 관측을 저장하고 긴 role turn을 기다리지 않고 반환한다."""
        if wait_seconds < 0:
            raise ValueError("tick wait은 음수일 수 없습니다.")
        job = self.service.load_runtime_job(job_id)
        if job.status in {RuntimeJobStatus.PROVIDER_TERMINAL, RuntimeJobStatus.CONSUMED,
                          RuntimeJobStatus.CANCELLED}:
            return job
        event = self._result_events.get(job_id)
        if event is not None and wait_seconds:
            event.wait(min(wait_seconds, self.handoff_wait_seconds))
        with self._lock:
            outcome = self._results.pop(job_id, None)
        if outcome is not None:
            succeeded, value = outcome
            if succeeded:
                self.service.record_runtime_job_observation(
                    job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                    payload={"result": value}, provider_terminal=True, terminal_status="completed")
            elif job_id in self._terminal_progress:
                terminal = self._terminal_progress[job_id]
                observation = terminal.get("terminal_observation")
                status = (
                    observation.get("terminal_status")
                    if isinstance(observation, dict) else None
                ) or "unknown"
                self.service.record_runtime_job_observation(
                    job_id,
                    kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                    payload={"result": {"job_error": value, "terminal_observation": observation}},
                    provider_terminal=True,
                    terminal_status=status,
                )
            else:
                self.service.record_runtime_job_observation(
                    job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload=value)
            return self.service.load_runtime_job(job_id)
        checkpointed, _checkpoint_result = self._durable_target_result(job_id)
        if checkpointed:
            return self.reattach(job_id)
        if datetime.now(timezone.utc) >= job.absolute_deadline_at:
            self._request_bounded_interrupt(
                job, reason="absolute_deadline_exceeded",
            )
            job = self.service.load_runtime_job(job_id)
            if job.status is not RuntimeJobStatus.INTERRUPTING:
                return job
            if datetime.now(timezone.utc) >= job.absolute_deadline_at + timedelta(
                seconds=self.terminal_observation_grace_seconds
            ):
                self.service.record_runtime_job_observation(
                    job_id,
                    kind=RuntimeJobObservationKind.COLLECTOR_LOST,
                    payload={
                        "reason": "terminal observation grace elapsed after deadline interrupt",
                        "absolute_deadline_at": job.absolute_deadline_at.isoformat(),
                        "terminal_observation_grace_seconds": (
                            self.terminal_observation_grace_seconds
                        ),
                    },
                )
                return self.service.load_runtime_job(job_id)
            # interrupt receipt는 provider terminal 증거가 아니다. 결속된 job은
            # 같은 짧은 tick에서 아래의 worker 결과 또는 저장 turn을 계속 관측한다.
        worker = self._workers.get(job_id)
        if worker is not None and not worker.is_alive() and (event is None or not event.is_set()):
            self.service.record_runtime_job_observation(
                job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST,
                payload={"reason": "collector thread exited without a result"})
            return self.service.load_runtime_job(job_id)
        if job.thread_id is not None and (
            worker is None
            or not worker.is_alive()
            or self._complete_on_return.get(job_id) is False
        ):
            try:
                return self.reattach(job_id)
            except Exception as error:
                current = self.service.load_runtime_job(job_id)
                if current.status is not RuntimeJobStatus.COLLECTOR_LOST:
                    self.service.record_runtime_job_observation(
                        job_id,
                        kind=RuntimeJobObservationKind.COLLECTOR_LOST,
                        payload={
                            "reason": "bounded provider re-observation failed",
                            "error_type": type(error).__name__,
                            "error": str(error),
                        },
                    )
                return self.service.load_runtime_job(job_id)
        if worker is None and job.status is RuntimeJobStatus.RUNNING:
            self.service.record_runtime_job_observation(
                job_id,
                kind=RuntimeJobObservationKind.COLLECTOR_LOST,
                payload={"reason": "supervisor restarted before provider binding was durable"},
            )
        return self.service.load_runtime_job(job_id)

    def bind_provider_turn(self, job_id: str, *, thread_id: str, turn_id: str) -> RuntimeJob:
        before = self.service.load_runtime_job(job_id)
        bound = self.service.bind_runtime_job_provider(
            job_id, thread_id=thread_id, turn_id=turn_id,
        )
        if (
            before.thread_id is None
            and bound.status in {
                RuntimeJobStatus.INTERRUPTING,
                RuntimeJobStatus.CANCELLED,
            }
            and not self._interrupt_receipt_recorded(job_id)
        ):
            self._deliver_bounded_interrupt(bound)
        return self.service.load_runtime_job(job_id)

    def _request_bounded_interrupt(
        self,
        job: RuntimeJob,
        *,
        reason: str,
        timeout_seconds: float | None = None,
    ) -> None:
        if not reason.strip():
            raise ValueError("interrupt reason이 필요합니다.")
        request = {"reason": reason, "thread_id": job.thread_id,
                   "turn_id": job.turn_id,
                   "absolute_deadline_at": job.absolute_deadline_at.isoformat()}
        requested = self.service.begin_runtime_job_interrupt(job.job_id, payload=request)
        current = self.service.load_runtime_job(job.job_id)
        if not requested and current.status is not RuntimeJobStatus.INTERRUPTING:
            return
        if current.thread_id is None or current.turn_id is None:
            return
        if self._interrupt_receipt_recorded(job.job_id):
            return
        self._deliver_bounded_interrupt(current, timeout_seconds=timeout_seconds)

    def _interrupt_receipt_recorded(self, job_id: str) -> bool:
        with self.service.ledger.read() as connection:
            return connection.execute(
                "SELECT 1 FROM runtime_job_observations WHERE job_id=? AND kind=? LIMIT 1",
                (job_id, RuntimeJobObservationKind.INTERRUPT_RECEIPT.value),
            ).fetchone() is not None

    def _deliver_bounded_interrupt(
        self,
        job: RuntimeJob,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        assert job.thread_id is not None and job.turn_id is not None
        wait = self.interrupt_timeout_seconds if timeout_seconds is None else timeout_seconds
        if wait <= 0:
            return
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM runtime_job_observations "
                "WHERE job_id=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                (job.job_id, RuntimeJobObservationKind.INTERRUPT_REQUESTED.value),
            ).fetchone()
        if row is None:
            raise RuntimePolicyError("RUNTIME_INTERRUPT_REQUEST_MISSING")
        request = json.loads(row["payload_json"])
        try:
            receipt = bounded_observation_call(
                lambda: self.runtime.interrupt(thread_id=job.thread_id, turn_id=job.turn_id,
                    timeout_seconds=wait),
                timeout_seconds=wait,
                operation_name="runtime_job_interrupt")
        except Exception as error:
            payload = request | {"interrupt_error": type(error).__name__, "detail": str(error)}
        else:
            payload = request | {"interrupt_receipt": self._json_value(receipt)}
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.INTERRUPT_RECEIPT, payload=payload)

    def reattach(self, job_id: str) -> RuntimeJob:
        """재시작 후 저장 binding을 resume 없이 먼저 관측한다."""
        self._owned_job_ids.add(job_id)
        job = self.service.load_runtime_job(job_id)
        if job.status not in {
            RuntimeJobStatus.RUNNING, RuntimeJobStatus.COLLECTOR_LOST,
            RuntimeJobStatus.INTERRUPTING, RuntimeJobStatus.CANCELLED,
        }:
            return job
        checkpointed, checkpoint_result = self._durable_target_result(job_id)
        if checkpointed:
            self.service.record_runtime_job_observation(
                job_id,
                kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                payload={
                    "result": checkpoint_result,
                    "result_source": "durable_target_checkpoint",
                },
                provider_terminal=True,
                terminal_status="completed",
            )
            return self.service.load_runtime_job(job_id)
        if job.thread_id is None:
            return job
        if (
            job.status is RuntimeJobStatus.INTERRUPTING
            and job.turn_id is not None
            and not self._interrupt_receipt_recorded(job_id)
        ):
            self._deliver_bounded_interrupt(job)
        if job.status not in {
            RuntimeJobStatus.INTERRUPTING,
            RuntimeJobStatus.CANCELLED,
        }:
            self.service.start_runtime_job(
                job_id, thread_id=job.thread_id, turn_id=job.turn_id
            )
        reader = self.runtime.read_stored
        parameters = inspect.signature(reader).parameters
        accepts_keywords = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        read_arguments: dict[str, Any] = {"thread_id": job.thread_id}
        if accepts_keywords or "turn_id" in parameters:
            read_arguments["turn_id"] = job.turn_id
        if accepts_keywords or "timeout_seconds" in parameters:
            read_arguments["timeout_seconds"] = self.observation_timeout_seconds
        observation = bounded_observation_call(
            lambda: reader(**read_arguments),
            # Port 자체의 bounded timeout이 먼저 종료돼 그 결과나 오류를 회수할
            # 수 있도록 외부 watchdog에는 짧은 정리 여유를 둔다.
            timeout_seconds=self.observation_timeout_seconds + 0.05,
            operation_name="runtime_job_reattach_read")
        if (
            observation.thread_id != job.thread_id
            or (job.turn_id is not None and observation.turn_id != job.turn_id)
        ):
            raise RuntimePolicyError(
                "RUNTIME_OBSERVATION_BINDING_MISMATCH: 재관측 결과가 저장된 exact turn과 다릅니다."
            )
        payload = {"observation": self._json_value(observation)}
        if not observation.active and observation.terminal_status is not None:
            payload["result"] = self._terminal_result_from_observation(job, observation)
            self.service.record_runtime_job_observation(
                job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload=payload,
                provider_terminal=True, terminal_status=observation.terminal_status)
        else:
            self.service.record_runtime_job_observation(
                job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS, payload=payload)
        return self.service.load_runtime_job(job_id)

    def observe_cancelled(self, job_id: str) -> RuntimeJob:
        """cancel된 exact turn을 관측하되 일시 read 실패는 grace 동안 재시도한다."""

        job = self.service.load_runtime_job(job_id)
        if job.status is not RuntimeJobStatus.CANCELLED:
            return job
        grace_anchor = job.ended_at or job.updated_at
        grace_elapsed = datetime.now(timezone.utc) >= grace_anchor + timedelta(
            seconds=self.terminal_observation_grace_seconds
        )
        if job.thread_id is None or job.turn_id is None:
            if grace_elapsed:
                raise RuntimePolicyError("CANCELLED_RUNTIME_BINDING_UNKNOWN")
            return job
        try:
            return self.reattach(job_id)
        except Exception as error:
            self.service.record_runtime_job_observation(
                job_id,
                kind=RuntimeJobObservationKind.COLLECTOR_LOST,
                payload={
                    "reason": "cancelled provider turn re-observation failed",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
            definitive = isinstance(error, RuntimePolicyError) and any(
                code in str(error)
                for code in {
                    "RUNTIME_OBSERVATION_BINDING_MISMATCH",
                    "RUNTIME_OBSERVATION_PAGINATION_INCOMPLETE",
                }
            )
            job = self.service.load_runtime_job(job_id)
            grace_elapsed = datetime.now(timezone.utc) >= (job.ended_at or job.updated_at) + timedelta(
                seconds=self.terminal_observation_grace_seconds
            )
            if definitive or grace_elapsed:
                raise
            return job

    def mark_collector_lost(self, job_id: str, *, reason: str) -> RuntimeJob:
        self.service.record_runtime_job_observation(
            job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": reason})
        return self.service.load_runtime_job(job_id)

    def request_interrupt(
        self, job_id: str, *, reason: str = "absolute_deadline_exceeded",
    ) -> RuntimeJob:
        """pause/cancel facade가 쓰는 bounded interrupt 요청 경계."""

        job = self.service.load_runtime_job(job_id)
        if job.status not in {
            RuntimeJobStatus.PROVIDER_TERMINAL,
            RuntimeJobStatus.CONSUMED,
            RuntimeJobStatus.CANCELLED,
        }:
            self._request_bounded_interrupt(job, reason=reason)
        return self.service.load_runtime_job(job_id)

    def cancel(self, job_id: str, *, reason: str) -> RuntimeJob:
        """중단 요청을 먼저 남긴 뒤 Core job을 cancelled로 표시한다."""

        self.request_interrupt(job_id, reason="workflow_cancelled")
        return self.service.cancel_runtime_job(job_id, reason=reason)

    def close(self, *, timeout_seconds: float | None = None) -> None:
        """SDK close를 bounded 실행하고 미관측 active job은 collector_lost로 남긴다."""
        wait = self.interrupt_timeout_seconds if timeout_seconds is None else timeout_seconds
        if wait <= 0:
            raise ValueError("supervisor close timeout은 양수여야 합니다.")
        deadline = time.monotonic() + wait
        active_ids = set(self._owned_job_ids)
        active_ids.update(self._workers)
        for job_id in active_ids:
            worker = self._workers.get(job_id)
            job = self.service.load_runtime_job(job_id)
            if job.status in {
                RuntimeJobStatus.RUNNING, RuntimeJobStatus.SCHEDULED,
                RuntimeJobStatus.COLLECTOR_LOST, RuntimeJobStatus.INTERRUPTING,
            }:
                if job.thread_id is not None and job.turn_id is not None:
                    self._request_bounded_interrupt(
                        job,
                        reason="supervisor_closed",
                        timeout_seconds=max(0.0, deadline - time.monotonic()),
                    )
                self.mark_collector_lost(
                    job_id,
                    reason=(
                        "supervisor SDK owner closed before provider terminal; "
                        f"worker_alive={worker is not None and worker.is_alive()}"
                    ),
                )
        try:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                bounded_observation_call(
                    lambda: self.runtime.close(timeout_seconds=remaining),
                    timeout_seconds=remaining,
                    operation_name="runtime_job_sdk_close",
                )
        except Exception:
            # close timeout은 provider terminal 증거가 아니며 collector_lost 관측을 보존한다.
            pass

    def __enter__(self) -> "RuntimeJobSupervisor":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


#: `EvidenceRecord.observation`의 최대 길이. 실패 관측 문서는 이 한도 안에서 항상 유효한 JSON이어야 한다.
_FAILURE_OBSERVATION_LIMIT = 10_000
#: 실패 기록 시점에 Engine이 계산한 분류 projection의 provenance 표식.
_FAILURE_DIAGNOSIS_PROVENANCE = FAILURE_DIAGNOSIS_PROVENANCE


class TaskGatePending(RuntimeError):
    """task gate가 아직 판정하지 못했다. Task 상태를 바꾸지 않고 다음 tick에 다시 묻는다."""


class TaskGateContractMismatch(RuntimeError):
    """task gate가 기대는 외부 계약·환경이 맞지 않는다. Task의 잘못이 아니므로 어느 단계에서도 Task 상태를 바꾸지 않는다."""


class EngineDispatcher:
    """원장 우선순위에 따라 호출당 한 상태 단계만 전진시키는 실행기."""

    _SUCCESS = PROVIDER_SUCCESS_TERMINAL_STATUSES
    _FAILED = PROVIDER_FAILURE_TERMINAL_STATUSES

    def __init__(
        self,
        service: EngineService,
        runtime: CodexRuntimePort,
        *,
        fault_hook: Callable[[str], None] | None = None,
        proposal_provider: Any | None = None,
        supervisor: RuntimeJobSupervisor | None = None,
        recovery_provider: Any | None = None,
        failure_classifier: EvidenceFirstFailureClassifier | None = None,
        task_gate: Any | None = None,
    ) -> None:
        self.service = service
        self.runtime = runtime
        # 선택적 외부 gate. before_execution(task_row)와 before_completion(task_row)가
        # 차단 사유 문자열을 돌려주면 Worker dispatch나 Task 완료를 막는다. 완료 판정은 Core에 남는다.
        self.task_gate = task_gate
        self.fault_hook = fault_hook
        self.proposal_provider = proposal_provider
        self.supervisor = supervisor
        self.recovery_provider = recovery_provider
        self.failure_classifier = failure_classifier or EvidenceFirstFailureClassifier()
        self._recovery_supervisor = supervisor or RuntimeJobSupervisor(service, runtime)
        self._operation_traces: dict[str, OperationTrace] = {}
        self._operation_trace_locks: dict[str, threading.RLock] = {}
        self._attempt_provider_call_ids: dict[str, str] = {}

    def _attempt_provider_call_id(self, attempt_id: str) -> str | None:
        current = self._attempt_provider_call_ids.get(attempt_id)
        if current is not None:
            return current
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT id FROM provider_calls WHERE attempt_id = ? ORDER BY rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        if row is not None:
            current = row["id"]
            self._attempt_provider_call_ids[attempt_id] = current
        return current

    def _operation_trace(self, attempt_id: str) -> OperationTrace:
        current = self._operation_traces.get(attempt_id)
        if current is not None:
            return current
        path = self.service.ledger.artifact_root / "operation-traces" / f"{attempt_id}.operation-trace.jsonl"
        if path.exists():
            current = OperationTrace.from_path(path)
        else:
            current = OperationTrace(
                context={"scope": "engine_attempt", "attempt_id": attempt_id},
                path=path,
                expected_operations=("create", "start"),
            )
        self._operation_traces[attempt_id] = current
        return current

    @staticmethod
    def _runtime_trace_payload(value: Any) -> Any:
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    def _verify_runtime_effect_inputs(
        self,
        *,
        kind: str,
        attempt_id: str,
        intent_id: str,
        request: dict[str, Any],
    ) -> None:
        """create/start/resume 직전에 runtime과 Core의 전체 결속을 다시 읽는다."""

        row, spec = self._attempt_context(attempt_id)
        cwd = Path(row["root"])
        policy = self.runtime.verify_execution_policy(cwd)
        if (
            policy.permission_profile != REQUIRED_PERMISSION_PROFILE
            or policy.approval_policy != REQUIRED_APPROVAL_POLICY
            or policy.environment != "local"
            or not _same_path(policy.cwd, cwd)
        ):
            raise RuntimePolicyError(
                "PERMISSION_POLICY_MISMATCH: 효과 직전 runtime 정책이나 cwd 결속이 다릅니다."
            )
        inventory = self.runtime.list_models()
        validation_id = request.get("validation_id")
        role, prompt, output_schema = self._role_for_attempt(
            row,
            spec,
            validation_id=validation_id if isinstance(validation_id, str) else None,
        )
        for resolved_role in (spec.definition.executor, spec.definition.validator):
            if resolved_role is not None:
                self._verify_role_binding(resolved_role, inventory)
        current_binding = self._verify_role_binding(role, inventory)
        if request.get("model_observation") != current_binding.model_dump(mode="json"):
            raise RuntimePolicyError(
                "MODEL_BINDING_CHANGED: intent 준비 뒤 실제 model observation이 바뀌었습니다."
            )
        if request.get("model") != role.model or request.get("effort") != role.effort:
            raise RuntimePolicyError(
                "MODEL_BINDING_CHANGED: intent의 model/effort가 current ExecutionSpec과 다릅니다."
            )
        if "cwd" in request and not _same_path(request["cwd"], cwd):
            raise RuntimePolicyError("TARGET_BINDING_CHANGED: intent cwd가 프로젝트 root와 다릅니다.")
        if request.get("task_id") not in {None, row["task_id"]}:
            raise RuntimePolicyError("TARGET_BINDING_CHANGED: intent Task가 Attempt와 다릅니다.")
        if request.get("execution_spec_digest") not in {None, spec.definition_digest}:
            raise RuntimePolicyError(
                "STALE_EXECUTION_INPUT: intent ExecutionSpec digest가 current spec과 다릅니다."
            )
        if row["kind"] == AttemptKind.EXECUTION.value:
            binding_digest = spec.definition.context_manifest.prompt_binding.binding_digest
            if request.get("prompt_binding_digest") not in {None, binding_digest}:
                raise RuntimePolicyError(
                    "PROMPT_BINDING_MISMATCH: intent와 current prompt artifact가 다릅니다."
                )
        if request.get("resume_notice") is True:
            prompt = "이전 turn이 중단되었습니다. 같은 TaskContract 범위에서 재개하세요.\n" + prompt
        expected_prompt_digest = sha256_digest(prompt)
        if request.get("prompt_digest") not in {None, expected_prompt_digest}:
            raise RuntimePolicyError(
                "PROMPT_BINDING_MISMATCH: intent 준비 뒤 실제 전송 prompt가 바뀌었습니다."
            )
        expected_schema_digest = sha256_digest(output_schema)
        if request.get("output_schema_digest") not in {None, expected_schema_digest}:
            raise RuntimePolicyError(
                "PROMPT_BINDING_MISMATCH: intent output schema가 current schema와 다릅니다."
            )
        self.service.prepare_authorized_runtime_effect(
            attempt_id,
            intent_id,
            request=request,
            allow_mutable_targets=(
                kind == "resume"
                or request.get("resume_notice") is True
                or row["kind"] == AttemptKind.VALIDATION.value
            ),
        )

    def _invoke_runtime_operation(
        self,
        trace: OperationTrace,
        kind: str,
        request: dict[str, Any],
        invoke: Callable[[], Any],
        *,
        attempt_id: str,
        intent_id: str | None = None,
        call_id: str | None = None,
        deadline_seconds: float | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> Any:
        lock = self._operation_trace_locks.setdefault(attempt_id, threading.RLock())
        with lock:
            if kind in {"create", "start", "resume"}:
                if intent_id is None:
                    raise EngineServiceError(
                        "RUNTIME_INTENT_REQUIRED: create/start/resume에는 durable intent가 필요합니다."
                    )
                try:
                    self._verify_runtime_effect_inputs(
                        kind=kind,
                        attempt_id=attempt_id,
                        intent_id=intent_id,
                        request=request,
                    )
                except Exception as error:
                    code = getattr(error, "code", None)
                    if not isinstance(code, str):
                        message = str(error)
                        prefix = message.partition(":")[0]
                        code = (
                            "MODEL_BINDING_CHANGED"
                            if prefix.startswith("MODEL_LOCK_")
                            else prefix
                        ) if prefix in {
                            "STALE_EXECUTION_INPUT",
                            "RUNTIME_INTENT_BINDING_MISMATCH",
                            "PROMPT_BINDING_MISMATCH",
                            "PROMPT_ARTIFACT_INVALID",
                            "MODEL_BINDING_CHANGED",
                            "PERMISSION_POLICY_MISMATCH",
                            "TARGET_BINDING_CHANGED",
                            "EFFECT_CHECKPOINT_STALE",
                            "WORKFLOW_CANCELLED",
                            "WORKFLOW_PAUSED",
                        } or prefix.startswith("MODEL_LOCK_") else "RUNTIME_EFFECT_PREFLIGHT_FAILED"
                    self.service.record_runtime_effect_not_started(
                        attempt_id=attempt_id,
                        intent_id=intent_id,
                        code=code,
                        detail=str(error),
                        changes=tuple(getattr(error, "changes", ())),
                    )
                    raise
            parent_scope = current_operation_trace_scope()
            category = (
                "wait" if kind == "sdk_wait" else
                "logical" if getattr(self.runtime, "emits_rpc_operation_trace", False) else
                "rpc"
            )
            token = trace.begin(
                kind, request, call_id=call_id, attempt_id=attempt_id, intent_id=intent_id,
                thread_id=thread_id, turn_id=turn_id, deadline_seconds=deadline_seconds,
                category=category,
                parent_operation_id=(
                    None if parent_scope is None else parent_scope.parent.operation_id
                ),
                rpc_method=(f"port.{kind}" if category == "rpc" else None),
            )
            try:
                with trace.operation_scope(token), (
                    role_execution_scope("worker_or_validator") if kind in {"create", "start", "resume"} else nullcontext()
                ):
                    result = invoke()
            except Exception as error:
                trace.finish(
                    token, error=error, attempt_id=attempt_id, intent_id=intent_id,
                    thread_id=thread_id, turn_id=turn_id,
                )
                raise
            binding = getattr(result, "binding", None)
            trace.finish(
                token, response=self._runtime_trace_payload(result),
                provider_call_id=getattr(result, "operation_id", None),
                attempt_id=attempt_id, intent_id=intent_id,
                thread_id=getattr(binding, "thread_id", None) or thread_id,
                turn_id=getattr(binding, "turn_id", None) or turn_id,
            )
            return result

    @staticmethod
    def _attach_receipt_trace(
        receipt: RuntimeOperationReceipt, trace: OperationTrace, *, seal: bool = False,
    ) -> RuntimeOperationReceipt:
        body = trace.seal() if seal and not trace.snapshot()["manifest"]["pending_operation_ids"] else trace.snapshot()
        ref = None if trace.path is None else str(trace.path.resolve())
        digest = sha256_digest(body)
        payload = receipt.payload | {
            "operation_trace": body, "operation_trace_digest": digest,
            **({} if ref is None else {"operation_trace_ref": ref}),
        }
        return receipt.model_copy(update={
            "payload": payload, "operation_trace": body,
            "operation_trace_ref": ref, "operation_trace_digest": digest,
        })

    @staticmethod
    def _attach_observation_trace(
        observation: RuntimeObservation, trace: OperationTrace, *, seal: bool = False,
    ) -> RuntimeObservation:
        body = trace.seal() if seal and not trace.snapshot()["manifest"]["pending_operation_ids"] else trace.snapshot()
        ref = None if trace.path is None else str(trace.path.resolve())
        digest = sha256_digest(body)
        return observation.model_copy(update={"payload": observation.payload | {
            "operation_trace": body, "operation_trace_digest": digest,
            **({} if ref is None else {"operation_trace_ref": ref}),
        }})

    def _read_attempt_runtime(
        self,
        attempt_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        seal_terminal: bool,
    ) -> RuntimeObservation:
        trace = self._operation_trace(attempt_id)
        lock = self._operation_trace_locks.setdefault(attempt_id, threading.RLock())
        with lock:
            resolver = getattr(self.runtime, "operation_kind_for_read", None)
            kind = resolver(thread_id) if callable(resolver) else "read"
            observation_timeout = (
                5.0
                if self.supervisor is None
                else self.supervisor.observation_timeout_seconds
            )
            observation = self._invoke_runtime_operation(
                trace, kind, {"thread_id": thread_id, "turn_id": turn_id},
                lambda: bounded_observation_call(
                    lambda: self.runtime.read(thread_id=thread_id),
                    timeout_seconds=observation_timeout,
                    operation_name="runtime_job_active_read",
                ),
                attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
                call_id=self._attempt_provider_call_id(attempt_id),
                deadline_seconds=(
                    observation_timeout
                    if kind == "read" else None
                ),
            )
            terminal = (
                seal_terminal and not observation.active and observation.terminal_status is not None
            )
            return self._attach_observation_trace(observation, trace, seal=terminal)

    def _hit(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def run_once(
        self,
        project_id: str,
        *,
        proposal: ExecutionSpecProposal | None = None,
        goal_validation_step: Any | None = None,
        goal_validation_retry: Any | None = None,
    ) -> RunOnceResult:
        from .budget import BudgetBlocked
        from .service import GoalAuthorizationRequired
        started = time.monotonic()
        with self.service.ledger.read() as connection:
            before_jobs = {
                row["id"]: (row["status"], row["updated_at"])
                for row in connection.execute(
                    "SELECT id,status,updated_at FROM runtime_jobs WHERE project_id=?",
                    (project_id,),
                )
            }
        try:
            outcome = self._run_once(project_id, proposal=proposal,
                                     goal_validation_step=goal_validation_step,
                                     goal_validation_retry=goal_validation_retry)
        except (BudgetBlocked, GoalAuthorizationRequired) as error:
            outcome = RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=project_id,
                                     blocker_code=error.code, detail=str(error))
        with self.service.ledger.read() as connection:
            changed_jobs = [
                row
                for row in connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? ORDER BY updated_at DESC,rowid DESC",
                    (project_id,),
                )
                if before_jobs.get(row["id"]) != (row["status"], row["updated_at"])
            ]
        job = changed_jobs[0] if changed_jobs else None
        if job is None and outcome.action in {
            RunOnceAction.DISPATCHED,
            RunOnceAction.OBSERVED,
        }:
            active = self.service.active_runtime_job(project_id)
            if active is not None:
                with self.service.ledger.read() as connection:
                    job = connection.execute(
                        "SELECT * FROM runtime_jobs WHERE id=?", (active.job_id,)
                    ).fetchone()
        job_fields = {} if job is None else {
            "runtime_job_id": job["id"], "runtime_job_kind": job["kind"],
            "runtime_job_status": job["status"],
        }
        return RunOnceResult(
            **outcome.model_dump(mode="python"), **job_fields,
            tick_elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    def _run_once(
        self, project_id: str, *, proposal: ExecutionSpecProposal | None = None,
        goal_validation_step: Any | None = None,
        goal_validation_retry: Any | None = None,
    ) -> RunOnceOutcome:
        """관측 → 검사 → materialize → dispatch → Goal Test 순서로 한 단계만 수행한다."""

        if goal_validation_step is not None and goal_validation_retry is not None:
            raise EngineServiceError("Goal Test 최초 binding과 재시도 요청을 함께 제출할 수 없습니다.")

        with self.service.ledger.read() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise EngineServiceError("프로젝트를 찾을 수 없습니다.")
            prepared = connection.execute(
                "SELECT i.id FROM runtime_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE a.project_id = ? AND i.status = 'prepared' ORDER BY i.prepared_at",
                (project_id,),
            ).fetchall()
            active_attempt = connection.execute(
                "SELECT a.* FROM attempts a WHERE a.project_id = ? "
                "AND a.status IN ('reserved','starting','running') "
                "AND NOT EXISTS (SELECT 1 FROM runtime_jobs gj WHERE gj.attempt_id=a.id "
                "AND gj.kind='goal_semantic_validate') "
                "ORDER BY a.created_at, a.rowid LIMIT 1",
                (project_id,),
            ).fetchone()
        active_job = self.service.active_runtime_job(project_id)
        if (
            active_job is not None
            and active_job.status is RuntimeJobStatus.COLLECTOR_LOST
            and active_job.thread_id is None
            and active_job.attempt_id is not None
        ):
            with self.service.ledger.read() as connection:
                recoverable_intent = connection.execute(
                    "SELECT 1 FROM runtime_intents WHERE attempt_id=? AND status='prepared' LIMIT 1",
                    (active_job.attempt_id,),
                ).fetchone()
            if recoverable_intent is not None:
                self.service.cancel_runtime_job(
                    active_job.job_id,
                    reason="binding 전 collector가 사라져 prepared effect checkpoint 복구로 전환",
                )
                active_job = None
        if active_job is not None and self.supervisor is not None:
            observed_job = self.supervisor.tick(active_job.job_id)
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=project_id,
                task_id=observed_job.task_id,
                attempt_id=observed_job.attempt_id,
                detail=(
                    "활성 RuntimeJob의 provider 상태를 한 번 관측했습니다: "
                    f"{observed_job.kind.value}/{observed_job.status.value}"
                ),
            )
        if prepared:
            if len(prepared) == 1:
                with self.service.ledger.read() as connection:
                    marker = connection.execute("SELECT event_type,payload_json FROM history_events WHERE entity_id = ? "
                        "AND event_type IN ('runtime.effect_not_started','runtime.effect_dispatching') "
                        "ORDER BY sequence DESC LIMIT 1", (prepared[0]["id"],)).fetchone()
                    intent = connection.execute("SELECT * FROM runtime_intents WHERE id = ?", (prepared[0]["id"],)).fetchone()
                if marker is not None and marker["event_type"] == "runtime.effect_not_started":
                    marker_payload = json.loads(marker["payload_json"])
                    if marker_payload.get("code") == "GOAL_AUTHORIZATION_REQUIRED":
                        return self._retry_authorization_blocked_effect(intent)
                    return RunOnceOutcome(
                        action=RunOnceAction.BLOCKED,
                        project_id=project_id,
                        task_id=None if active_attempt is None else active_attempt["task_id"],
                        attempt_id=None if active_attempt is None else active_attempt["id"],
                        blocker_code=marker_payload.get("code", "RUNTIME_EFFECT_PREFLIGHT_FAILED"),
                        detail=(
                            "provider 호출 전 effect checkpoint가 입력 변경을 차단했습니다: "
                            + marker_payload.get("detail", "preflight rejected")
                        ),
                    )
                recovered = self._reconcile_prepared_intent_from_trace(intent)
                if recovered is not None:
                    return RunOnceOutcome(
                        action=RunOnceAction.OBSERVED,
                        project_id=project_id,
                        task_id=None if active_attempt is None else active_attempt["task_id"],
                        attempt_id=None if active_attempt is None else active_attempt["id"],
                        detail=(
                            "append-only operation trace에서 정확한 provider receipt/binding을 "
                            "복원했습니다. 이 tick에서는 create/start/resume를 재실행하지 않습니다."
                        ),
                    )
            unknown = self.service.recover_inspect(project_id)
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                task_id=None if active_attempt is None else active_attempt["task_id"],
                attempt_id=None if active_attempt is None else active_attempt["id"],
                blocker_code="EXTERNAL_EFFECT_UNKNOWN",
                failure_class=FailureClass.EXTERNAL_UNKNOWN,
                suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                checkpoint_required=True,
                detail=(
                    "receipt 없는 runtime intent를 external_unknown으로 보존했습니다. "
                    "새 thread/turn을 생성하지 않습니다: " + ", ".join(unknown)
                ),
            )
        if active_attempt is not None:
            if active_attempt["status"] == "reserved":
                validation_id = (
                    self._next_semantic_validation_id(active_attempt["task_id"])
                    if active_attempt["kind"] == AttemptKind.VALIDATION.value
                    else None
                )
                if self.supervisor is None:
                    self._dispatch_reserved(
                        active_attempt["id"], validation_id=validation_id
                    )
                else:
                    self._dispatch_attempt_job(
                        active_attempt["id"], validation_id=validation_id
                    )
                return RunOnceOutcome(
                    action=RunOnceAction.DISPATCHED,
                    project_id=project_id,
                    task_id=active_attempt["task_id"],
                    attempt_id=active_attempt["id"],
                    detail="기존 reserved Attempt를 중복 예약 없이 dispatch했습니다.",
                )
            return self._observe_as_outcome(active_attempt["id"])
        from .operations import CoreOperations
        unknown_operations = CoreOperations(self.service).recover_unfinished(project_id)
        if unknown_operations:
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED, project_id=project_id,
                blocker_code="EXTERNAL_EFFECT_UNKNOWN", failure_class=FailureClass.EXTERNAL_UNKNOWN,
                suggested_repair_action=RepairAction.WAIT_EXTERNAL, checkpoint_required=True,
                detail="완료 관측 없는 준비·검증 효과를 재실행하지 않습니다: " + ", ".join(unknown_operations),
            )
        if project["run_state"] == "recovery_required":
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                blocker_code="RECOVERY_REQUIRED",
                failure_class=FailureClass.EXTERNAL_UNKNOWN,
                suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                checkpoint_required=True,
                detail=project["recovery_reason"] or "외부 효과 reconciliation이 필요합니다.",
            )
        if project["active_plan_revision_id"] is None:
            return RunOnceOutcome(
                action=RunOnceAction.IDLE,
                project_id=project_id,
                detail=(
                    "프로젝트가 완료됐습니다."
                    if project["run_state"] == "completed"
                    else "활성 PlanContract가 없습니다."
                ),
            )

        # Task 완료와 재관측 사이에 프로세스가 중단돼도 다음 Task나 Goal Test로
        # 건너뛰지 않는다. append-only History 순서로 미완료 재관측을 복구한다.
        with self.service.ledger.read() as connection:
            pending_reobservation = connection.execute(
                "SELECT h.entity_id AS task_id FROM history_events h "
                "JOIN task_contracts t ON t.id = h.entity_id "
                "WHERE h.project_id = ? AND h.event_type = 'task.completed' "
                "AND t.plan_revision_id = ? AND h.sequence > COALESCE(("
                "SELECT MAX(sequence) FROM history_events "
                "WHERE project_id = ? AND event_type = 'state.observed'"
                "), 0) ORDER BY h.sequence LIMIT 1",
                (project_id, project["active_plan_revision_id"], project_id),
            ).fetchone()
        if pending_reobservation is not None:
            self._hit("before_state_reobservation")
            self.service.reobserve_project(project_id, force_state_revision=True)
            self._hit("after_state_reobservation")
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=project_id,
                task_id=pending_reobservation["task_id"],
                detail=(
                    "중단 전에 완료된 Task를 발견해 Project Map·Goal 관련 State를 "
                    "다음 materialization 전에 재관측했습니다."
                ),
            )

        with self.service.ledger.read() as connection:
            validating = connection.execute(
                "SELECT * FROM task_contracts WHERE project_id = ? AND status = 'validating' "
                "ORDER BY position LIMIT 1",
                (project_id,),
            ).fetchone()
            ready = connection.execute(
                "SELECT * FROM task_contracts WHERE project_id = ? AND status = 'ready' "
                "ORDER BY position LIMIT 1",
                (project_id,),
            ).fetchone()
            materialized = connection.execute(
                "SELECT * FROM task_contracts WHERE project_id = ? AND status = 'materialized' "
                "ORDER BY position LIMIT 1",
                (project_id,),
            ).fetchone()
            plan_row = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ?",
                (project["active_plan_revision_id"],),
            ).fetchone()
        if validating is not None:
            return self._advance_validation(validating)
        if ready is not None:
            self.service.assert_project_authorized(project_id)
            inventory = None
            existing_preparation_job = None
            if self.supervisor is not None:
                with self.service.ledger.read() as connection:
                    existing_preparation_job = connection.execute(
                        "SELECT 1 FROM runtime_jobs WHERE project_id=? AND task_id=? "
                        "AND kind=? AND status <> ? LIMIT 1",
                        (
                            project_id,
                            ready["id"],
                            RuntimeJobKind.EXECUTION_SPEC_PREPARE.value,
                            RuntimeJobStatus.CANCELLED.value,
                        ),
                    ).fetchone()
            if self.supervisor is not None and (
                proposal is not None
                or self.proposal_provider is not None
                or existing_preparation_job is not None
            ):
                from .operations import ExternalOperationUnknown
                try:
                    self._verify_policy(Path(project["root"]))
                    if proposal is None:
                        self.service.reobserve_project(project_id)
                    prepared = self._execution_spec_job(
                        project_id=project_id,
                        task_id=ready["id"],
                        supplied_proposal=proposal,
                    )
                    if prepared is None:
                        job = self.service.active_runtime_job(project_id)
                        return RunOnceOutcome(
                            action=RunOnceAction.DISPATCHED, project_id=project_id,
                            task_id=ready["id"],
                            detail=(
                                "Execution Spec 준비 job을 예약·관측했습니다: "
                                f"{job.job_id if job else 'terminal'}"
                            ),
                        )
                    prepared_spec, inventory = prepared
                    if prepared_spec.context_request is not None:
                        return RunOnceOutcome(
                            action=RunOnceAction.BLOCKED, project_id=project_id, task_id=ready["id"],
                            blocker_code="CONTEXT_REQUIRED", detail=prepared_spec.context_request.model_dump_json(),
                        )
                    proposal = prepared_spec.proposal
                except ExternalOperationUnknown as error:
                    return RunOnceOutcome(
                        action=RunOnceAction.BLOCKED, project_id=project_id, task_id=ready["id"],
                        blocker_code="EXTERNAL_EFFECT_UNKNOWN", failure_class=FailureClass.EXTERNAL_UNKNOWN,
                        suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                        checkpoint_required=True, detail=str(error),
                    )
            elif proposal is None and self.proposal_provider is not None:
                from .operations import ExternalOperationUnknown
                try:
                    self._verify_policy(Path(project["root"]))
                    self.service.reobserve_project(project_id)
                    inventory = self.runtime.list_models()
                    prepared_spec = self.proposal_provider.prepare_task(
                        project_id=project_id,
                        task_id=ready["id"],
                        inventory=inventory,
                    )
                    if prepared_spec.context_request is not None:
                        return RunOnceOutcome(
                            action=RunOnceAction.BLOCKED,
                            project_id=project_id,
                            task_id=ready["id"],
                            blocker_code="CONTEXT_REQUIRED",
                            detail=prepared_spec.context_request.model_dump_json(),
                        )
                    proposal = prepared_spec.proposal
                except ExternalOperationUnknown as error:
                    return RunOnceOutcome(
                        action=RunOnceAction.BLOCKED,
                        project_id=project_id,
                        task_id=ready["id"],
                        blocker_code="EXTERNAL_EFFECT_UNKNOWN",
                        failure_class=FailureClass.EXTERNAL_UNKNOWN,
                        suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                        checkpoint_required=True,
                        detail=str(error),
                    )
            if proposal is None:
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    task_id=ready["id"],
                    blocker_code="EXECUTION_SPEC_PROPOSAL_REQUIRED",
                    detail="ready Task를 materialize할 비권위 ExecutionSpecProposal이 필요합니다.",
                )
            if proposal.task_id != ready["id"]:
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    task_id=ready["id"],
                    blocker_code="PROPOSAL_TASK_MISMATCH",
                    detail="proposal이 현재 우선순위의 ready Task와 다릅니다.",
                )
            root = Path(project["root"])
            self._verify_policy(root)
            if inventory is None:
                inventory = self.runtime.list_models()
            try:
                spec = self.service.compile_execution_spec(proposal, inventory=inventory)
            except ContextRequiredError as error:
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED, project_id=project_id, task_id=ready["id"],
                    blocker_code="CONTEXT_REQUIRED", detail=error.request.model_dump_json(),
                )
            return RunOnceOutcome(
                action=RunOnceAction.MATERIALIZED,
                project_id=project_id,
                task_id=ready["id"],
                execution_spec_revision_id=spec.execution_spec_revision_id,
                detail="proposal을 최신 Goal·Plan·State·Project Map에 결속했습니다.",
            )
        if materialized is not None:
            # stale 입력은 steward를 포함한 어떤 provider 효과보다 먼저 차단한다.
            # gate 뒤 reserve_attempt도 같은 검사를 반복해 TOCTOU를 막는다.
            self.service.assert_execution_inputs_current(materialized["id"])
            gated = self._task_gate_decision("before_execution", materialized)
            if isinstance(gated, RunOnceOutcome):
                return gated
            if gated is not None:
                # dispatch 전 차단은 Task 상태를 바꾸지 않는다. 같은 사유는 History에 한 번만 남긴다.
                self.service.record_task_gate_block(task_id=materialized["id"], phase="before_execution",
                                                    detail=gated)
                return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=project_id,
                    task_id=materialized["id"], blocker_code="GOVERNANCE_GATE_BLOCKED", detail=gated)
            attempt = self.service.reserve_attempt(task_id=materialized["id"])
            try:
                if self.supervisor is None:
                    self._dispatch_reserved(attempt.attempt_id)
                else:
                    self._dispatch_attempt_job(attempt.attempt_id)
            except Exception as error:
                from .budget import BudgetBlocked
                if not isinstance(error, BudgetBlocked):
                    raise
                return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=project_id,
                    task_id=materialized["id"], blocker_code=error.code, detail=str(error))
            return RunOnceOutcome(
                action=RunOnceAction.DISPATCHED,
                project_id=project_id,
                task_id=materialized["id"],
                attempt_id=attempt.attempt_id,
                detail="materialized Task 하나를 새 Codex 실행에 dispatch했습니다.",
            )
        if plan_row is None:
            raise EngineServiceError("active PlanContract payload가 없습니다.")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        with self.service.ledger.read() as connection:
            incomplete = connection.execute(
                "SELECT COUNT(*) FROM task_contracts WHERE plan_revision_id = ? "
                "AND status <> 'completed'",
                (project["active_plan_revision_id"],),
            ).fetchone()[0]
            failed = connection.execute(
                "SELECT t.id AS task_id, a.id AS attempt_id, a.failure_class, a.failure_detail "
                "FROM task_contracts t JOIN attempts a ON a.id = ("
                "SELECT latest.id FROM attempts latest WHERE latest.task_id = t.id "
                "AND latest.kind = 'execution' "
                "ORDER BY latest.attempt_no DESC, latest.rowid DESC LIMIT 1"
                ") WHERE t.plan_revision_id = ? AND t.status IN ('failed','blocked') "
                "AND a.failure_class IS NOT NULL "
                "ORDER BY t.position LIMIT 1",
                (project["active_plan_revision_id"],),
            ).fetchone()
        if incomplete:
            if failed is not None:
                failure_class = FailureClass(failed["failure_class"])
                evidence_ids, documents = self._failure_evidence(
                    failed["attempt_id"]
                )
                # 원장의 failure_class를 권위로 두고, 보존된 terminal 원문에서
                # code 출처와 일시 실패 표시만 다시 관측해 덧붙인다.
                observed = self._terminal_failure_diagnosis(documents)
                matched = observed is not None and observed.failure_class is failure_class
                diagnosis = FailureDiagnosis(
                    failure_class=failure_class,
                    repair_action=self.service.repair_action_for(failure_class),
                    provider_error_code=observed.provider_error_code if matched else None,
                    local_engine_code=observed.local_engine_code if matched else None,
                    evidence_ids=evidence_ids,
                    rationale=failed["failure_detail"] or "원장에 기록된 Attempt 실패",
                    source=(
                        "unclassified"
                        if failure_class is FailureClass.UNCLASSIFIED
                        else "direct_evidence"
                    ),
                    transient=bool(matched and observed.transient),
                )
                automatic = self._automatic_recovery(
                    project_id=project_id,
                    task_id=failed["task_id"],
                    attempt_id=failed["attempt_id"],
                    diagnosis=diagnosis,
                    evidence_documents=documents,
                )
                if automatic is not None:
                    return automatic
                repair_action = self.service.repair_action_for(failure_class)
                checkpoint_required = repair_action in {
                    RepairAction.SUBGRAPH_REPLAN,
                    RepairAction.GOAL_REVISION,
                    RepairAction.WAIT_EXTERNAL,
                }
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    task_id=failed["task_id"],
                    attempt_id=failed["attempt_id"],
                    blocker_code="TASK_RECOVERY_REQUIRED",
                    failure_class=failure_class,
                    suggested_repair_action=repair_action,
                    checkpoint_required=checkpoint_required,
                    detail=(
                        f"failure_class={failure_class.value}; "
                        f"suggested_repair={repair_action.value}. "
                        "Core는 repair 또는 새 권위 revision을 자동 적용하지 않습니다."
                    ),
                )
            validation_failure = self.service.task_validation_recovery_blocker(
                project_id, plan_revision_id=project["active_plan_revision_id"]
            )
            if validation_failure is not None:
                evidence_ids, documents = self._failure_evidence(
                    validation_failure["attempt_id"],
                    evidence_ids=validation_failure["evidence_ids"],
                )
                diagnosis = self.failure_classifier.classify(FailureSignal(
                    terminal_status="validation_failed",
                    final_response=None,
                    provider_payload={
                        "validation_result_id": validation_failure["validation_result_id"]
                    },
                    evidence_ids=evidence_ids,
                    evidence_documents=documents,
                ))
                automatic = self._automatic_recovery(
                    project_id=project_id,
                    task_id=validation_failure["task_id"],
                    attempt_id=validation_failure["attempt_id"],
                    diagnosis=diagnosis,
                    evidence_documents=documents,
                    validation_result_id=validation_failure["validation_result_id"],
                )
                if automatic is not None:
                    return automatic
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    task_id=validation_failure["task_id"],
                    attempt_id=validation_failure["attempt_id"],
                    validation_result_id=validation_failure["validation_result_id"],
                    evidence_ids=validation_failure["evidence_ids"],
                    blocker_code="TASK_VALIDATION_RECOVERY_REQUIRED",
                    detail=(
                        "성공 Worker 뒤 현재 validation epoch의 최신 FAIL입니다. "
                        "해당 FAIL과 현재 Worker Attempt의 직접 evidence를 근거로 "
                        "원인을 분류한 RecoveryAssessment와 failed validation result ID를 "
                        "명시해 Task retry를 요청하십시오."
                    ),
                )
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                blocker_code="NO_RUNNABLE_TASK",
                detail="미완료 Task가 있으나 dependency 또는 실패 상태로 실행 가능하지 않습니다.",
            )
        return self._advance_goal_test(
            project_id, plan, goal_validation_step=goal_validation_step,
            goal_validation_retry=goal_validation_retry,
        )

    def _terminal_failure_diagnosis(
        self, documents: tuple[dict[str, Any], ...]
    ) -> FailureDiagnosis | None:
        """보존된 failed terminal evidence의 분류 projection을 읽는 공유 구현이다."""

        return terminal_failure_diagnosis(documents, classifier=self.failure_classifier)

    @staticmethod
    def _bounded_failure_document(document: dict[str, Any]) -> dict[str, Any]:
        """`EvidenceRecord.observation` 한도 안에서 항상 유효한 JSON 문서를 만든다.

        직렬화 문자열을 자르면 evidence가 파싱 불가능해지고 content digest도
        저장 본문과 어긋난다. 대신 정보를 단계적으로 줄인다: operation_trace 본문을
        digest/ref만 남기고 제거 → provider_payload를 최상위 scalar 항목과 전체 payload
        digest로 투영 → final_response를 자른다. 각 단계는 문서에 명시 표식을 남긴다.
        원문 payload는 runtime job 관측·usage 기록과 operation trace 파일에 별도로
        보존된다.
        """

        def fits(candidate: dict[str, Any]) -> bool:
            encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            return len(encoded) <= _FAILURE_OBSERVATION_LIMIT

        if fits(document):
            return document
        payload = document.get("provider_payload")
        payload = payload if isinstance(payload, dict) else {}
        reduced = dict(document)
        if "operation_trace" in payload:
            reduced["provider_payload"] = {
                key: value for key, value in payload.items() if key != "operation_trace"
            } | {"operation_trace_omitted": True}
            if fits(reduced):
                return reduced
        reduced["provider_payload"] = {
            key: value for key, value in payload.items()
            if key != "operation_trace"
            and isinstance(value, (str, int, float, bool, type(None)))
        } | {
            "provider_payload_projected": True,
            "provider_payload_digest": sha256_digest(payload),
        }
        if fits(reduced):
            return reduced
        response = reduced.get("final_response")
        response = "" if response is None else str(response)
        reduced["final_response_truncated"] = True
        low, high = 0, len(response)
        while low < high:
            middle = (low + high + 1) // 2
            reduced["final_response"] = response[:middle]
            if fits(reduced):
                low = middle
            else:
                high = middle - 1
        reduced["final_response"] = response[:low]
        return reduced

    def _failure_evidence(
        self,
        attempt_id: str,
        *,
        evidence_ids: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
        with self.service.ledger.read() as connection:
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                rows = connection.execute(
                    f"SELECT id,payload_json FROM evidence_records WHERE id IN ({placeholders}) "
                    "ORDER BY observed_at,rowid",
                    tuple(evidence_ids),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id,payload_json FROM evidence_records WHERE attempt_id=? "
                    "ORDER BY observed_at,rowid",
                    (attempt_id,),
                ).fetchall()
        return (
            tuple(row["id"] for row in rows),
            tuple(json.loads(row["payload_json"]) for row in rows),
        )

    @staticmethod
    def _stable_recovery_id(attempt_id: str, failure_fingerprint: str) -> str:
        return stable_recovery_assessment_id(attempt_id, failure_fingerprint)

    def _recovery_assessment(
        self,
        *,
        project_id: str,
        attempt_id: str,
        diagnosis: FailureDiagnosis,
    ) -> tuple[Any, bool]:
        from .domain import RecoveryAssessment

        if diagnosis.failure_class is None or diagnosis.repair_action is None:
            raise EngineServiceError("분류되지 않은 실패에는 assessment를 만들 수 없습니다.")
        assessment_id = self._stable_recovery_id(
            attempt_id, diagnosis.failure_fingerprint
        )
        with self.service.ledger.read() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_assessments WHERE id=?",
                (assessment_id,),
            ).fetchone()
            if existing is not None:
                return RecoveryAssessment.model_validate_json(existing["payload_json"]), True
            same = connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? "
                "AND action='subgraph_replan' AND json_extract(payload_json,'$.failure_fingerprint')=?",
                (project_id, diagnosis.failure_fingerprint),
            ).fetchone()[0]
            total = connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? "
                "AND action IN ('subgraph_replan','goal_revision')",
                (project_id,),
            ).fetchone()[0]
        replan = diagnosis.repair_action in {
            RepairAction.SUBGRAPH_REPLAN, RepairAction.GOAL_REVISION
        }
        assessment = RecoveryAssessment(
            assessment_id=assessment_id,
            attempt_id=attempt_id,
            failure_class=diagnosis.failure_class,
            action=diagnosis.repair_action,
            rationale=diagnosis.rationale,
            failure_fingerprint=diagnosis.failure_fingerprint,
            new_evidence_ids=diagnosis.evidence_ids,
            same_failure_replan_count=(
                int(same) + 1
                if diagnosis.repair_action is RepairAction.SUBGRAPH_REPLAN
                else 0
            ),
            goal_replan_count=int(total) + 1 if replan else 0,
        )
        return assessment, False

    def _automatic_recovery(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        diagnosis: FailureDiagnosis,
        evidence_documents: tuple[dict[str, Any], ...],
        validation_result_id: str | None = None,
    ) -> RunOnceOutcome | None:
        from .operations import ExternalOperationUnknown

        # 분류 → 경로 결정은 status 표시와 같은 공유 구현을 쓴다.
        route = recovery_route(diagnosis, validation_result_id=validation_result_id)
        if route.mode in {"unrouted", "observe_first"}:
            return None
        if route.mode == "user_decision":
            classified = (
                diagnosis.failure_class is not None
                and diagnosis.failure_class is not FailureClass.UNCLASSIFIED
            )
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                evidence_ids=diagnosis.evidence_ids,
                blocker_code=route.blocker_code,
                failure_class=diagnosis.failure_class if classified else None,
                suggested_repair_action=route.suggested_repair_action,
                checkpoint_required=route.checkpoint_required,
                detail=route.detail,
            )

        limit = self._recovery_limit_blocker(
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            diagnosis=diagnosis,
            validation_result_id=validation_result_id,
        )
        if limit is not None:
            return limit

        assessment, already_recorded = self._recovery_assessment(
            project_id=project_id,
            attempt_id=attempt_id,
            diagnosis=diagnosis,
        )
        if not already_recorded:
            request = {
                "assessment": assessment.model_dump(mode="json"),
                "validation_result_id": validation_result_id,
                "evidence_documents_digest": sha256_digest(evidence_documents),
            }
            checkpoint = f"recovery:{attempt_id}:{diagnosis.failure_fingerprint}"
            with self.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                    (project_id, checkpoint),
                ).fetchone()
            if row is None:
                job = self._recovery_supervisor.schedule(
                    project_id=project_id,
                    kind=RuntimeJobKind.RECOVERY,
                    checkpoint_key=checkpoint,
                    request=request,
                    timeout_seconds=900,
                    target=lambda: {"assessment": assessment.model_dump(mode="json")},
                    attempt_id=attempt_id,
                    task_id=task_id,
                )
                if self.supervisor is None:
                    job = self._recovery_supervisor.tick(
                        job.job_id, wait_seconds=self._recovery_supervisor.handoff_wait_seconds
                    )
            else:
                job = self.service._runtime_job_from_row(row)
                if job.status in {RuntimeJobStatus.SCHEDULED, RuntimeJobStatus.RUNNING}:
                    job = self._recovery_supervisor.tick(job.job_id)
            if job.status not in {
                RuntimeJobStatus.PROVIDER_TERMINAL, RuntimeJobStatus.CONSUMED
            }:
                return RunOnceOutcome(
                    action=RunOnceAction.DISPATCHED,
                    project_id=project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    detail="자동 recovery assessment job을 예약·관측했습니다.",
                )
            try:
                result = self.service.consume_runtime_job_required_result(job.job_id)
            except ExternalOperationUnknown as error:
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    blocker_code="EXTERNAL_EFFECT_UNKNOWN",
                    failure_class=FailureClass.EXTERNAL_UNKNOWN,
                    suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                    checkpoint_required=True,
                    detail=str(error),
                )
            from .domain import RecoveryAssessment
            assessment = RecoveryAssessment.model_validate(result["assessment"])
            self.service.record_recovery_assessment(project_id, assessment)

        if assessment.action in {RepairAction.TASK_REPAIR, RepairAction.CONTINUE}:
            try:
                status = self.service.retry_task(
                    task_id=task_id,
                    recovery_assessment=assessment,
                    failed_validation_result_id=validation_result_id,
                )
            except EngineServiceError as error:
                if not str(error).startswith("REPAIR_INPUT_CHANGED"):
                    raise
                # 사용자 변경은 재관측으로 흡수하지 않는다. Task 상태는 그대로 두고 사유만 돌려준다.
                return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=project_id, task_id=task_id,
                                      attempt_id=attempt_id, validation_result_id=validation_result_id,
                                      blocker_code="REPAIR_INPUT_CHANGED",
                                      detail=f"{error}. 바뀐 입력을 직전 상태로 되돌리면 다음 run_once가 repair를 이어 가고, "
                                             "변경을 유지하려면 Goal revision·재계획 경로로 갑니다.")
            return RunOnceOutcome(
                action=RunOnceAction.RECOVERED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                validation_result_id=validation_result_id,
                evidence_ids=assessment.new_evidence_ids,
                detail=(
                    "직전 Attempt가 바꾼 쓰기 target 때문에 입력이 stale해 재관측 뒤 새 ExecutionSpec으로 "
                    "repair를 준비합니다."
                    if status == "ready"
                    else "원장 생성 assessment로 동일 Task repair를 활성화했습니다. 다음 실행은 새 Attempt입니다."
                    if assessment.action is RepairAction.TASK_REPAIR
                    else (
                        "직접 관측된 일시 실패 code를 근거로 원장 한도 안에서 같은 "
                        "ExecutionSpec을 한 번 더 실행합니다."
                    )
                ),
            )
        if assessment.action is RepairAction.EXECUTION_SPEC_REVISION:
            self.service.enable_execution_spec_recovery(
                task_id=task_id, recovery_assessment=assessment
            )
            return RunOnceOutcome(
                action=RunOnceAction.RECOVERED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                evidence_ids=assessment.new_evidence_ids,
                detail="Context 근거로 새 ExecutionSpec 준비를 활성화했습니다.",
            )
        if assessment.action is RepairAction.SUBGRAPH_REPLAN:
            if not already_recorded:
                return RunOnceOutcome(
                    action=RunOnceAction.RECOVERED,
                    project_id=project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    evidence_ids=assessment.new_evidence_ids,
                    detail="replanning 전에 자동 assessment checkpoint를 기록했습니다.",
                )
            return self._automatic_subgraph_replan(
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                assessment=assessment,
                evidence_documents=evidence_documents,
            )
        return None

    def _recovery_limit_blocker(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        diagnosis: FailureDiagnosis,
        validation_result_id: str | None,
    ) -> RunOnceOutcome | None:
        """원장에서 계산한 복구 한도 관측을 그대로 typed 차단으로 바꾼다."""

        with self.service.ledger.read() as connection:
            limit = recovery_limit_decision(
                connection,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                diagnosis=diagnosis,
            )
        if limit.limit_code is None:
            return None
        return RunOnceOutcome(
            action=RunOnceAction.BLOCKED,
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            validation_result_id=validation_result_id,
            evidence_ids=diagnosis.evidence_ids,
            blocker_code=limit.limit_code,
            failure_class=diagnosis.failure_class,
            suggested_repair_action=diagnosis.repair_action,
            checkpoint_required=True,
            detail=limit.detail,
        )

    def _automatic_subgraph_replan(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        assessment: Any,
        evidence_documents: tuple[dict[str, Any], ...],
    ) -> RunOnceOutcome:
        if self.recovery_provider is None:
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                evidence_ids=assessment.new_evidence_ids,
                blocker_code="REPLAN_PROVIDER_REQUIRED",
                failure_class=assessment.failure_class,
                suggested_repair_action=RepairAction.SUBGRAPH_REPLAN,
                checkpoint_required=True,
                detail="승인 범위 안의 Plan subgraph 후보와 독립 review를 생성할 provider가 필요합니다.",
            )
        checkpoint = f"replanning:{assessment.assessment_id}"
        request = {
            "assessment": assessment.model_dump(mode="json"),
            "evidence_documents": list(evidence_documents),
        }
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                (project_id, checkpoint),
            ).fetchone()
        if row is None:
            job = self._recovery_supervisor.schedule(
                project_id=project_id,
                kind=RuntimeJobKind.REPLANNING,
                checkpoint_key=checkpoint,
                request=request,
                timeout_seconds=1800,
                target=lambda: self.recovery_provider.replan(
                    project_id=project_id,
                    task_id=task_id,
                    assessment=assessment,
                    evidence_documents=evidence_documents,
                    inventory=self.runtime.list_models(),
                ),
                attempt_id=attempt_id,
                task_id=task_id,
            )
            if self.supervisor is None:
                job = self._recovery_supervisor.tick(
                    job.job_id, wait_seconds=self._recovery_supervisor.handoff_wait_seconds
                )
        else:
            job = self.service._runtime_job_from_row(row)
            if job.status in {RuntimeJobStatus.SCHEDULED, RuntimeJobStatus.RUNNING}:
                job = self._recovery_supervisor.tick(job.job_id)
        if job.status not in {
            RuntimeJobStatus.PROVIDER_TERMINAL, RuntimeJobStatus.CONSUMED
        }:
            return RunOnceOutcome(
                action=RunOnceAction.DISPATCHED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                detail="Plan subgraph 재계획·독립 review job을 예약·관측했습니다.",
            )
        try:
            result = self.service.consume_runtime_job_required_result(job.job_id)
        except ExternalOperationUnknown as error:
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                blocker_code="EXTERNAL_EFFECT_UNKNOWN",
                failure_class=FailureClass.EXTERNAL_UNKNOWN,
                suggested_repair_action=RepairAction.WAIT_EXTERNAL,
                checkpoint_required=True,
                detail=str(error),
            )
        from .planning import ExpandedPlanEvaluation
        evaluation = ExpandedPlanEvaluation.model_validate(result)
        self.service.register_authorized_plan_revision(evaluation)
        return RunOnceOutcome(
            action=RunOnceAction.RECOVERED,
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            evidence_ids=assessment.new_evidence_ids,
            detail="독립 review를 통과한 Plan subgraph revision을 승인 경계 안에서 자동 활성화했습니다.",
        )

    def _execution_spec_job(
        self,
        *,
        project_id: str,
        task_id: str,
        supplied_proposal: ExecutionSpecProposal | None,
    ) -> tuple[Any, ModelInventory] | None:
        """inventory와 preparation 역할을 job에서 관측하고 다음 tick이 소비한다."""
        from .execution import ExecutionPreparation
        with self.service.ledger.read() as connection:
            task = connection.execute(
                "SELECT updated_at,plan_revision_id FROM task_contracts WHERE id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise EngineServiceError("Execution Spec 준비 Task가 없습니다.")
            prefix = (
                f"execution_spec_prepare:{task['plan_revision_id']}:{task_id}:"
                f"{sha256_digest(task['updated_at'])}:"
            )
            checkpoint = prefix + (
                sha256_digest(supplied_proposal) if supplied_proposal is not None else "provider"
            )
            if supplied_proposal is None:
                # 이전 tick이 caller-supplied proposal과 inventory를 이미 job에
                # 결속했다면 후속 tick은 같은 입력을 다시 요구하지 않고 소비한다.
                # repair·context 복구로 Task를 다시 연 뒤(updated_at 변경)의 옛 준비 결과는 쓰지 않는다.
                row = connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? AND task_id=? "
                    "AND kind=? AND status <> ? AND substr(checkpoint_key, 1, ?) = ? "
                    "ORDER BY created_at DESC,rowid DESC LIMIT 1",
                    (
                        project_id,
                        task_id,
                        RuntimeJobKind.EXECUTION_SPEC_PREPARE.value,
                        RuntimeJobStatus.CANCELLED.value,
                        len(prefix),
                        prefix,
                    ),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                    (project_id, checkpoint),
                ).fetchone()
        if row is None:
            def prepare() -> dict[str, Any]:
                inventory = self.runtime.list_models()
                result = (
                    ExecutionPreparation(proposal=supplied_proposal)
                    if supplied_proposal is not None
                    else self.proposal_provider.prepare_task(
                        project_id=project_id,
                        task_id=task_id,
                        inventory=inventory,
                    )
                )
                return {
                    "preparation": result.model_dump(mode="json"),
                    "model_inventory": inventory.model_dump(mode="json"),
                }

            job = self.supervisor.schedule(
                project_id=project_id, kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
                checkpoint_key=checkpoint,
                request={
                    "task_id": task_id,
                    "supplied_proposal_digest": (
                        None
                        if supplied_proposal is None
                        else sha256_digest(supplied_proposal)
                    ),
                    "inventory_observation": "runtime_job_owned",
                },
                timeout_seconds=900,
                target=prepare,
                task_id=task_id,
            )
        else:
            job = self.service._runtime_job_from_row(row)
            if job.status in {RuntimeJobStatus.RUNNING, RuntimeJobStatus.SCHEDULED}:
                job = self.supervisor.tick(job.job_id)
        if job.status not in {RuntimeJobStatus.PROVIDER_TERMINAL, RuntimeJobStatus.CONSUMED}:
            return None
        result = self.service.consume_runtime_job_required_result(job.job_id)
        return (
            ExecutionPreparation.model_validate(result["preparation"]),
            ModelInventory.model_validate(result["model_inventory"]),
        )

    def _verify_policy(self, cwd: Path) -> None:
        policy = self.runtime.verify_execution_policy(cwd)
        if (
            policy.permission_profile != REQUIRED_PERMISSION_PROFILE
            or policy.approval_policy != REQUIRED_APPROVAL_POLICY
            or policy.environment != "local"
        ):
            raise RuntimePolicyError(
                "PERMISSION_POLICY_MISMATCH: runtime port가 full-access/never local 정책을 "
                "입증하지 못했습니다."
            )

    def _attempt_context(self, attempt_id: str) -> tuple[Any, TaskExecutionSpecRevision]:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.*, t.task_ref, t.payload_json AS task_json, p.root, "
                "s.payload_json AS spec_json FROM attempts a "
                "JOIN task_contracts t ON t.id = a.task_id "
                "JOIN projects p ON p.id = a.project_id "
                "JOIN execution_spec_revisions s ON s.task_id = t.id AND s.is_current = 1 "
                "WHERE a.id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise EngineServiceError("Attempt 또는 current ExecutionSpec을 찾을 수 없습니다.")
        return row, TaskExecutionSpecRevision.model_validate_json(row["spec_json"])

    def _next_semantic_validation_id(self, task_id: str) -> str:
        with self.service.ledger.read() as connection:
            spec_row = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE task_id = ? AND is_current = 1",
                (task_id,),
            ).fetchone()
            recorded = {
                row["validation_id"]
                for row in self.service.effective_task_validation_results(connection, task_id)
            }
        if spec_row is None:
            raise EngineServiceError("validation Attempt의 ExecutionSpec이 없습니다.")
        spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
        for step in spec.definition.validation_steps:
            if step.method == "semantic" and step.validation_id not in recorded:
                return step.validation_id
        raise EngineServiceError("예약할 semantic validation이 없습니다.")

    @staticmethod
    def _semantic_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "passed": {"type": "boolean"},
                "rationale": {"type": "string"},
                "evidence_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "required": ["passed", "rationale", "evidence_refs"],
        }

    def _validation_id_from_attempt(self, attempt_id: str) -> str:
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT request_json FROM runtime_intents WHERE attempt_id = ? "
                "ORDER BY prepared_at, rowid",
                (attempt_id,),
            ).fetchall()
        for row in rows:
            document = json.loads(row["request_json"])
            value = document.get("validation_id")
            if isinstance(value, str):
                return value
        row, _spec = self._attempt_context(attempt_id)
        return self._next_semantic_validation_id(row["task_id"])

    def _role_for_attempt(
        self,
        row: Any,
        spec: TaskExecutionSpecRevision,
        *,
        validation_id: str | None,
    ) -> tuple[Any, str, dict[str, Any] | None]:
        if row["kind"] == AttemptKind.EXECUTION.value:
            from .worker_prompt import PromptArtifactError, PromptArtifactStore
            try:
                bundle = PromptArtifactStore(self.service.ledger.artifact_root).load(
                    spec.definition.context_manifest.prompt_binding
                )
            except PromptArtifactError as error:
                raise EngineServiceError(str(error)) from error
            return spec.definition.executor, bundle.rendered, None
        if spec.definition.validator is None:
            raise EngineServiceError("semantic validation용 독립 validator binding이 없습니다.")
        if validation_id is None:
            validation_id = self._validation_id_from_attempt(row["id"])
        step = self._semantic_validation_step(spec, validation_id)
        catalog = self._semantic_evidence_catalog(row, spec, validation_id)
        if not catalog:
            raise EngineServiceError("독립 Task validator에 제공할 직접 evidence가 없습니다.")
        self._check_semantic_evidence_fresh(catalog, Path(row["root"]))
        prompt = (
            "다음 Task 결과를 독립적으로 검사하고 지정된 JSON object만 반환하세요. "
            "worker의 완료 주장은 근거로 취급하지 마세요. 파일과 명령을 실행·수정하지 마세요. "
            "Core가 수집한 Worker 응답 보고가 있으면 그 내용은 원본 직접 evidence와 대조하되, "
            "응답만으로 충족을 판단하지 마세요. "
            "evidence_refs에는 제공한 catalog의 ID만 사용하세요.\n"
            f"Validation ID: {validation_id}\n"
            f"검사 지시: {step.semantic_instruction}\n"
            f"필수 직접 evidence 종류(model_review는 Core가 추가): {step.required_evidence_kinds}\n"
            f"TaskContract:\n{row['task_json']}\n"
            f"ExecutionSpec:\n{spec.model_dump_json()}\n"
            f"직접 관측 evidence catalog:\n{json.dumps(catalog, ensure_ascii=False)}"
        )
        return spec.definition.validator, prompt, self._semantic_schema()

    @staticmethod
    def _semantic_validation_step(
        spec: TaskExecutionSpecRevision, validation_id: str,
    ) -> ValidationExecutionStep:
        step = next(
            (item for item in spec.definition.validation_steps if item.validation_id == validation_id),
            None,
        )
        if step is None or step.method != "semantic":
            raise EngineServiceError("validation Attempt가 semantic step에 결속되지 않았습니다.")
        return step

    def _semantic_evidence_catalog(
        self,
        row: Any,
        spec: TaskExecutionSpecRevision,
        validation_id: str,
    ) -> dict[str, Any]:
        step = self._semantic_validation_step(spec, validation_id)
        if row["execution_spec_digest"] != spec.definition_digest:
            return {}
        worker_spec_digest = self._worker_spec_digest_for_validation(
            task_id=row["task_id"],
            validation_attempt_id=row["id"],
            current_spec_digest=spec.definition_digest,
        )
        if worker_spec_digest is None:
            return {}
        return self._task_evidence_catalog(
            row["task_id"],
            worker_spec_digest,
            required_evidence_kinds=step.required_evidence_kinds,
        )

    def _worker_spec_digest_for_validation(
        self,
        *,
        task_id: str,
        validation_attempt_id: str,
        current_spec_digest: str,
    ) -> str | None:
        """현재 validator-only Spec에서 정확한 성공 Worker Spec 계보를 찾는다."""

        with self.service.ledger.read() as connection:
            digest = current_spec_digest
            seen: set[str] = set()
            first_hop = True
            while digest not in seen:
                seen.add(digest)
                worker = connection.execute(
                    "SELECT id FROM attempts WHERE task_id = ? AND kind = 'execution' "
                    "AND execution_spec_digest = ? AND status = 'succeeded' "
                    "ORDER BY attempt_no DESC LIMIT 1",
                    (task_id, digest),
                ).fetchone()
                if worker is not None:
                    return digest
                selection = connection.execute(
                    "SELECT s.previous_execution_spec_digest, s.attempt_id "
                    "FROM model_rebinding_selections s JOIN attempts a ON a.id = s.attempt_id "
                    "AND a.task_id = s.task_id AND a.kind = 'validation' "
                    "AND a.execution_spec_digest = s.new_execution_spec_digest "
                    "WHERE s.task_id = ? AND s.role = 'validator' "
                    "AND s.new_execution_spec_digest = ? "
                    "ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
                    (task_id, digest),
                ).fetchone()
                if selection is None or (
                    first_hop and selection["attempt_id"] != validation_attempt_id
                ):
                    return None
                digest = selection["previous_execution_spec_digest"]
                first_hop = False
        return None

    def _task_evidence_catalog(
        self,
        task_id: str,
        spec_digest: str,
        *,
        required_evidence_kinds: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        with self.service.ledger.read() as connection:
            execution = connection.execute(
                "SELECT id, created_at FROM attempts WHERE task_id = ? AND kind = 'execution' "
                "AND execution_spec_digest = ? AND status = 'succeeded' ORDER BY attempt_no DESC LIMIT 1",
                (task_id, spec_digest),
            ).fetchone()
            if execution is None:
                return {}
            include_worker_report = (
                EvidenceKind.EXTERNAL_OBSERVATION.value in set(required_evidence_kinds)
            )
            rows = connection.execute(
                "SELECT id, payload_json FROM evidence_records WHERE task_id = ? AND ("
                "(kind IN ('file','diff','command','test','build') "
                "AND (attempt_id = ? OR (attempt_id IS NULL AND observed_at >= ?))) "
                "OR (? = 1 AND kind = 'external_observation' AND attempt_id = ? "
                "AND source_ref LIKE 'codex-thread:%' AND source_ref NOT LIKE '%:truncated')"
                ") ORDER BY observed_at",
                (
                    task_id,
                    execution["id"],
                    execution["created_at"],
                    int(include_worker_report),
                    execution["id"],
                ),
            ).fetchall()
        return {item["id"]: json.loads(item["payload_json"]) for item in rows}

    @staticmethod
    def _check_semantic_evidence_fresh(catalog: dict[str, Any], root: Path) -> None:
        for evidence in catalog.values():
            if evidence["kind"] not in {"file", "diff"}:
                continue
            observation = json.loads(evidence["observation"])
            path = root / observation["path"]
            actual = sha256_bytes(path.read_bytes()) if path.is_file() else None
            if actual != observation["after_digest"]:
                raise EngineServiceError("STALE_EXECUTION_INPUT: 직접 semantic evidence 이후 파일이 바뀌었습니다.")

    @staticmethod
    def _verify_role_binding(role, inventory):
        try:
            if role.operational_binding is not None and role.inventory_digest != role.operational_binding.inventory_digest:
                raise ValueError("MODEL_LOCK_EVIDENCE_DIGEST_MISMATCH")
            return verify_binding(role.operational_binding, inventory,
                                  role=role.role, model=role.model, effort=role.effort)
        except ValueError as error:
            raise RuntimePolicyError(str(error)) from error

    def _retry_authorization_blocked_effect(self, intent: Any) -> RunOnceOutcome:
        self.service.assert_attempt_authorized(intent["attempt_id"])
        row, spec = self._attempt_context(intent["attempt_id"])
        with self.service.ledger.read() as connection:
            state = connection.execute("SELECT run_state FROM projects WHERE id = ?", (row["project_id"],)).fetchone()[0]
        if state != "active":
            return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=row["project_id"],
                                  blocker_code="PROJECT_NOT_ACTIVE", detail="프로젝트가 active일 때만 미실행 intent를 다시 실행합니다.")
        validation_id = self._next_semantic_validation_id(row["task_id"]) if row["kind"] == "validation" else None
        if intent["kind"] == "create_thread":
            self._dispatch_reserved(row["id"], validation_id=validation_id,
                                    existing_call_id=self._attempt_provider_call_id(row["id"]))
        elif intent["kind"] == "start_turn":
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            attempt_key = intent["idempotency_key"].removesuffix(":turn")
            self._start_turn(row=row, spec=spec, thread_id=binding.thread_id, attempt_key=attempt_key,
                             validation_id=validation_id, resumed=attempt_key.endswith(":resumed"),
                             provider_call_id=self._attempt_provider_call_id(row["id"]))
        elif intent["kind"] == "resume_turn":
            self._resume_bound_attempt(row, spec)
        else:
            raise EngineServiceError("자동 재개할 수 없는 승인 차단 effect입니다.")
        return RunOnceOutcome(action=RunOnceAction.DISPATCHED, project_id=row["project_id"], task_id=row["task_id"],
                              attempt_id=row["id"], detail="미실행이 기록된 동일 intent를 갱신된 승인 범위에서 실행했습니다.")

    def _reconcile_prepared_intent_from_trace(self, intent: Any) -> RuntimeOperationReceipt | None:
        """DB receipt 유실 시 trace의 exact response만 복원하고 effect는 재호출하지 않는다."""

        expected_kind = {
            RuntimeIntentKind.CREATE_THREAD.value: "create",
            RuntimeIntentKind.START_TURN.value: "start",
            RuntimeIntentKind.RESUME_TURN.value: "resume",
        }.get(intent["kind"])
        if expected_kind is None:
            return None
        trace = self._operation_trace(intent["attempt_id"])
        if trace.path is None or not trace.path.is_file():
            return None
        verification = OperationTrace.verify(trace.path)
        if not verification.valid:
            return None
        candidates = [
            item
            for item in trace.snapshot()["rows"]
            if item.get("intent_id") == intent["id"]
            and item.get("kind") == expected_kind
            and item.get("status") == "ok"
            and item.get("request_digest") == intent["request_digest"]
            and isinstance(item.get("response"), dict)
        ]
        if len(candidates) != 1:
            return None
        try:
            recovered = RuntimeOperationReceipt.model_validate(candidates[0]["response"])
        except ValueError:
            return None
        recovered = self._attach_receipt_trace(recovered, trace)
        request = json.loads(intent["request_json"])
        binding = recovered.binding
        if binding is None:
            return None
        if request.get("thread_id") not in {None, binding.thread_id}:
            return None
        if expected_kind == "start" and (
            binding.turn_id is None or recovered.payload.get("turn_id") != binding.turn_id
        ):
            return None
        provider_operation_id = (
            f"resume:{recovered.operation_id}:{intent['attempt_id']}"
            + (":public" if intent["idempotency_key"].endswith(":public-resume") else "")
            if expected_kind == "resume"
            else recovered.operation_id
        )
        self.service.record_runtime_receipt(
            intent_id=intent["id"],
            provider_operation_id=provider_operation_id,
            response=recovered.payload,
            binding=binding,
        )
        if expected_kind == "start":
            row, spec = self._attempt_context(intent["attempt_id"])
            with self.service.ledger.read() as connection:
                existing = connection.execute(
                    "SELECT id FROM runtime_jobs WHERE attempt_id = ? AND turn_id = ? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (row["id"], binding.turn_id),
                ).fetchone()
            if existing is None:
                runtime_job_kind = (
                    RuntimeJobKind.WORKER_TURN
                    if row["kind"] == AttemptKind.EXECUTION.value
                    else RuntimeJobKind.TASK_SEMANTIC_VALIDATE
                )
                job = self.service.schedule_runtime_job(
                    project_id=row["project_id"],
                    kind=runtime_job_kind,
                    checkpoint_key=f"{runtime_job_kind.value}:{row['id']}:{binding.turn_id}",
                    request=request,
                    absolute_deadline_at=utc_now()
                    + timedelta(seconds=spec.definition.timeout_seconds),
                    attempt_id=row["id"],
                    task_id=row["task_id"],
                )
                self.service.start_runtime_job(
                    job.job_id,
                    thread_id=binding.thread_id,
                    turn_id=binding.turn_id,
                )
        return recovered

    def _dispatch_attempt_job(
        self,
        attempt_id: str,
        *,
        validation_id: str | None = None,
        existing_call_id: str | None = None,
    ) -> RuntimeJob:
        """thread 생성·turn 시작도 scheduler 밖의 bounded job owner에서 수행한다."""

        row, spec = self._attempt_context(attempt_id)
        kind = (
            RuntimeJobKind.WORKER_TURN
            if row["kind"] == AttemptKind.EXECUTION.value
            else RuntimeJobKind.TASK_SEMANTIC_VALIDATE
        )
        request = {
            "attempt_id": attempt_id,
            "task_id": row["task_id"],
            "attempt_kind": row["kind"],
            "execution_spec_digest": spec.definition_digest,
            "validation_id": validation_id,
        }
        return self.supervisor.schedule(
            project_id=row["project_id"],
            kind=kind,
            checkpoint_key=f"{kind.value}:{attempt_id}:dispatch",
            request=request,
            timeout_seconds=spec.definition.timeout_seconds,
            target=lambda: self._dispatch_reserved(
                attempt_id,
                validation_id=validation_id,
                existing_call_id=existing_call_id,
            ),
            attempt_id=attempt_id,
            task_id=row["task_id"],
            complete_on_return=False,
        )

    def _dispatch_reserved(self, attempt_id: str, *, validation_id: str | None = None,
                           existing_call_id: str | None = None) -> None:
        self.service.assert_attempt_authorized(attempt_id)
        row, spec = self._attempt_context(attempt_id)
        cwd = Path(row["root"])
        role, prompt, output_schema = self._role_for_attempt(
            row, spec, validation_id=validation_id
        )
        self._verify_policy(cwd)
        inventory = self.runtime.list_models()
        for resolved_role in (spec.definition.executor, spec.definition.validator):
            if resolved_role is not None:
                self._verify_role_binding(resolved_role, inventory)
        current_binding = self._verify_role_binding(role, inventory)
        attempt_key = f"{spec.definition.idempotency_key}:attempt:{row['attempt_no']}:{row['kind']}"
        request = {
            "cwd": str(cwd.resolve()),
            "task_id": row["task_id"],
            "model": role.model,
            "effort": role.effort,
            "attempt_kind": row["kind"],
            "model_observation": current_binding.model_dump(mode="json"),
            "validation_id": validation_id,
            "prompt_digest": sha256_digest(prompt),
            "output_schema_digest": sha256_digest(output_schema),
            "execution_spec_digest": spec.definition_digest,
            "prompt_binding_digest": (
                spec.definition.context_manifest.prompt_binding.binding_digest
                if row["kind"] == AttemptKind.EXECUTION.value
                else None
            ),
            "resume_notice": False,
        }
        if row["kind"] == AttemptKind.EXECUTION.value:
            task_contract = json.loads(row["task_json"])
            request["effect_identities"] = [
                item["identity"] for item in task_contract["expected_effects"]
                if item["external"] and item.get("identity_version") == "2.0"
            ]
        if row["kind"] == AttemptKind.VALIDATION.value:
            semantic_validation_id = validation_id or self._validation_id_from_attempt(attempt_id)
            request["semantic_evidence_ids"] = list(
                self._semantic_evidence_catalog(row, spec, semantic_validation_id)
            )
        from .budget import BudgetBlocked, reserve_attempt_call
        try:
            if existing_call_id is None:
                provider_call_id = reserve_attempt_call(
                    self.service, row, call_key=attempt_key, request=request,
                    require_policy=getattr(self.runtime, "requires_budget_policy", False),
                )
            else:
                with self.service.ledger.read() as connection:
                    reserved = connection.execute("SELECT * FROM provider_calls WHERE id = ? AND attempt_id = ?",
                                                  (existing_call_id, attempt_id)).fetchone()
                if (reserved is None or reserved["execution_status"] != "reserved"
                        or reserved["request_digest"] != sha256_digest(request) or reserved["new_turn_count"] != 0):
                    raise EngineServiceError("승인 차단 이전의 미실행 provider 예약과 입력이 다릅니다.")
                provider_call_id = existing_call_id
            self._attempt_provider_call_ids[attempt_id] = provider_call_id
        except BudgetBlocked as error:
            self.service.release_unstarted_attempt(attempt_id, str(error))
            raise
        self._hit("before_thread_intent")
        create_intent = self.service.prepare_runtime_intent(
            attempt_id=attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key=f"{attempt_key}:thread",
            request=request,
        )
        self._hit("after_thread_intent")
        trace = self._operation_trace(attempt_id)
        thread_receipt = self._invoke_runtime_operation(
            trace, "create", request,
            lambda: self.runtime.create_thread(
                cwd=cwd,
                title=f"FlowMarshal {row['task_ref']} {row['kind']}",
                model=role.model,
                developer_instructions=(
                    "활성 PlanContract가 지정한 역할 하나만 수행한다. 다음 Task를 선택하거나 "
                    "FlowMarshal 원장의 권위 상태를 직접 바꾸지 않는다."
                ),
            ),
            attempt_id=attempt_id, intent_id=create_intent.intent_id,
            call_id=provider_call_id,
            deadline_seconds=getattr(spec.definition, "timeout_seconds", None),
        )
        thread_receipt = self._attach_receipt_trace(thread_receipt, trace)
        self._hit("after_thread_effect")
        self.service.record_runtime_receipt(
            intent_id=create_intent.intent_id,
            provider_operation_id=thread_receipt.operation_id,
            response=thread_receipt.payload,
            binding=thread_receipt.binding,
        )
        self._hit("after_thread_receipt")
        if thread_receipt.binding is None:
            raise RuntimeError("thread receipt에 binding이 없습니다.")
        self._start_turn(
            row=row,
            spec=spec,
            thread_id=thread_receipt.binding.thread_id,
            attempt_key=attempt_key,
            validation_id=validation_id,
            provider_call_id=provider_call_id,
        )

    def _start_turn(
        self,
        *,
        row: Any,
        spec: TaskExecutionSpecRevision,
        thread_id: str,
        attempt_key: str,
        validation_id: str | None,
        resumed: bool = False,
        provider_call_id: str | None = None,
    ) -> None:
        # 전송 직전에 다시 읽는다. 호출자가 임의 본문으로 대체할 인자는 두지 않는다.
        role, prompt, output_schema = self._role_for_attempt(row, spec, validation_id=validation_id)
        current_inventory = self.runtime.list_models()
        for resolved_role in (spec.definition.executor, spec.definition.validator):
            if resolved_role is not None:
                self._verify_role_binding(resolved_role, current_inventory)
        current_binding = self._verify_role_binding(role, current_inventory)
        if resumed:
            prompt = "이전 turn이 중단되었습니다. 같은 TaskContract 범위에서 재개하세요.\n" + prompt
        request = {
            "thread_id": thread_id,
            "cwd": str(Path(row["root"]).resolve()),
            "task_id": row["task_id"],
            "prompt_digest": sha256_digest(prompt),
            "model_observation": current_binding.model_dump(mode="json"),
            "model": role.model,
            "effort": role.effort,
            "validation_id": validation_id,
            "role_usage_contract": 1,
            "output_schema_digest": sha256_digest(output_schema),
            "resume_notice": resumed,
        }
        if row["kind"] == AttemptKind.EXECUTION.value:
            task_contract = json.loads(row["task_json"])
            request.update({
                "worker_usage_contract": 1,
                "execution_spec_digest": spec.definition_digest,
                "prompt_binding_digest": spec.definition.context_manifest.prompt_binding.binding_digest,
                # Context selector와 같은 UTF-8 byte/4 추정이며 실제 usage와 분리한다.
                "prompt_token_estimate": max(1, (len(prompt.encode("utf-8")) + 3) // 4),
                "effect_identities": [
                    item["identity"] for item in task_contract["expected_effects"]
                    if item["external"] and item.get("identity_version") == "2.0"
                ],
            })
        self._hit("before_turn_intent")
        turn_intent = self.service.prepare_runtime_intent(
            attempt_id=row["id"],
            kind=RuntimeIntentKind.START_TURN,
            idempotency_key=f"{attempt_key}:turn",
            request=request,
        )
        self._hit("after_turn_intent")
        trace = self._operation_trace(row["id"])
        turn_receipt = self._invoke_runtime_operation(
            trace, "start", request,
            lambda: self.runtime.start_turn(
                thread_id=thread_id,
                cwd=Path(row["root"]),
                prompt=prompt,
                model=role.model,
                effort=role.effort,
                output_schema=output_schema,
            ),
            attempt_id=row["id"], intent_id=turn_intent.intent_id,
            call_id=provider_call_id,
            thread_id=thread_id,
            deadline_seconds=getattr(spec.definition, "timeout_seconds", None),
        )
        turn_receipt = self._attach_receipt_trace(turn_receipt, trace)
        self._hit("after_turn_effect")
        current_job_id = active_runtime_job_id()
        if current_job_id is not None and turn_receipt.binding is not None:
            # start receipt가 생긴 즉시 exact turn을 durable job에 먼저 결속한다.
            # Attempt receipt 기록과 scheduler 관측이 경합해도 provider turn을 잃지 않는다.
            self.supervisor.bind_provider_turn(
                current_job_id,
                thread_id=turn_receipt.binding.thread_id,
                turn_id=turn_receipt.binding.turn_id,
            )
        self.service.record_runtime_receipt(
            intent_id=turn_intent.intent_id,
            provider_operation_id=turn_receipt.operation_id,
            response=turn_receipt.payload | {
                "effect_identity_digests": [
                    sha256_digest(item) for item in request.get("effect_identities", [])
                ],
            },
            binding=turn_receipt.binding,
        )
        runtime_job = None
        if turn_receipt.binding is not None:
            runtime_job_kind = (
                RuntimeJobKind.WORKER_TURN
                if row["kind"] == AttemptKind.EXECUTION.value
                else RuntimeJobKind.TASK_SEMANTIC_VALIDATE
            )
            if current_job_id is not None:
                runtime_job = self.supervisor.bind_provider_turn(
                    current_job_id,
                    thread_id=turn_receipt.binding.thread_id,
                    turn_id=turn_receipt.binding.turn_id,
                )
            else:
                # 직접 호출 호환 경로도 동일한 durable 관측 모델을 사용한다.
                runtime_job = self.service.schedule_runtime_job(
                    project_id=row["project_id"], kind=runtime_job_kind,
                    checkpoint_key=(
                        f"{runtime_job_kind.value}:{row['id']}:"
                        f"{turn_receipt.binding.turn_id}"
                    ),
                    request=request,
                    absolute_deadline_at=utc_now() + timedelta(seconds=spec.definition.timeout_seconds),
                    attempt_id=row["id"], task_id=row["task_id"],
                )
                runtime_job = self.service.start_runtime_job(
                    runtime_job.job_id, thread_id=turn_receipt.binding.thread_id,
                    turn_id=turn_receipt.binding.turn_id,
                )
        register = getattr(self.runtime, "register_completion_observer", None)
        if register is not None and turn_receipt.binding is not None:
            def record_completion(observation, *, attempt_id=row["id"], active_trace=trace,
                                  job_id=runtime_job.job_id if runtime_job is not None else None):
                lock = self._operation_trace_locks.setdefault(attempt_id, threading.RLock())
                with lock:
                    if active_trace.snapshot()["manifest"]["sealed"]:
                        observed = self._attach_observation_trace(observation, active_trace)
                    else:
                        observed = self._invoke_runtime_operation(
                            active_trace, "sdk_wait",
                            {"operation": "completion_observer", "thread_id": observation.thread_id,
                             "turn_id": observation.turn_id},
                        lambda: observation,
                        attempt_id=attempt_id, thread_id=observation.thread_id,
                        turn_id=observation.turn_id,
                        call_id=self._attempt_provider_call_id(attempt_id),
                        )
                        observed = self._attach_observation_trace(
                            observed, active_trace,
                            seal=observation.terminal_status in self._SUCCESS | self._FAILED,
                        )
                usage = self._record_worker_usage(attempt_id, observed)
                if job_id is not None and not observed.active and observed.terminal_status is not None:
                    self.service.record_runtime_job_observation(
                        job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                        payload={"result": observed.model_dump(mode="json")}, provider_terminal=True,
                        terminal_status=observed.terminal_status,
                    )
                return usage
            register(
                thread_id=thread_id, turn_id=turn_receipt.binding.turn_id,
                observer=record_completion,
            )
        self._hit("after_turn_receipt")

    def _record_worker_usage(self, attempt_id: str, observation: RuntimeObservation) -> Any:
        if observation.active or observation.turn_id is None or observation.terminal_status is None:
            return None
        with self.service.ledger.read() as connection:
            attempt = connection.execute("SELECT kind FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if attempt is not None and attempt["kind"] == AttemptKind.VALIDATION.value:
            from .budget import record_validator_usage
            return record_validator_usage(self.service, attempt_id, observation)
        return self.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=observation.thread_id, turn_id=observation.turn_id,
            terminal_status=observation.terminal_status, provider_payload=observation.payload,
            output_digest=None if observation.final_response is None else sha256_digest(observation.final_response),
        )

    def _record_cancel_evidence(
        self,
        job: RuntimeJob,
        *,
        source_ref: str,
        document: dict[str, Any],
    ) -> str:
        """같은 cancel observation의 evidence를 재시작 뒤에도 한 번만 남긴다."""

        with self.service.ledger.read() as connection:
            existing = connection.execute(
                "SELECT id FROM evidence_records WHERE project_id=? AND source_ref=? "
                "ORDER BY rowid DESC LIMIT 1",
                (job.project_id, source_ref),
            ).fetchone()
        if existing is not None:
            return existing["id"]
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=job.project_id,
            task_id=job.task_id,
            attempt_id=job.attempt_id,
            kind=EvidenceKind.EXTERNAL_OBSERVATION,
            source_ref=source_ref,
            observation=json.dumps(document, ensure_ascii=False, sort_keys=True),
            content_digest=sha256_digest(document),
            observed_at=utc_now(),
        )
        self.service.record_evidence(evidence)
        return evidence.evidence_id

    def finalize_cancelled_runtime_job(
        self,
        job_id: str,
        *,
        observation_error: BaseException | None = None,
    ) -> RuntimeJob:
        """새 provider 효과 없이 cancel된 exact turn의 관측으로 Attempt를 닫는다."""

        job = self.service.load_runtime_job(job_id)
        if job.attempt_id is None or job.task_id is None:
            return job
        with self.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT status FROM attempts WHERE id=?", (job.attempt_id,)
            ).fetchone()
        if attempt is None or attempt["status"] not in {"reserved", "starting", "running"}:
            return job
        if self.service.cancelled_runtime_effect_not_started(job_id):
            document = self._bounded_failure_document({
                "error_code": "WORKFLOW_CANCELLED_BEFORE_PROVIDER_EFFECT",
                "job_id": job_id,
                "thread_id": job.thread_id,
                "turn_id": job.turn_id,
            })
            evidence_id = self._record_cancel_evidence(
                job,
                source_ref=f"runtime-cancelled-before-effect:{job_id}",
                document=document,
            )
            self.service.finalize_cancelled_attempt(
                job_id=job_id,
                evidence_id=evidence_id,
                detail="사용자 취소로 provider turn이 시작되지 않았습니다.",
            )
            return self.service.load_runtime_job(job_id)
        if observation_error is not None:
            document = self._bounded_failure_document({
                "error_code": "CANCEL_OBSERVATION_UNKNOWN",
                "job_id": job_id,
                "thread_id": job.thread_id,
                "turn_id": job.turn_id,
                "error_type": type(observation_error).__name__,
                "detail": str(observation_error),
            })
            evidence_id = self._record_cancel_evidence(
                job,
                source_ref=f"runtime-cancel-unknown:{job_id}",
                document=document,
            )
            self.service.finalize_cancelled_attempt(
                job_id=job_id,
                evidence_id=evidence_id,
                detail="취소한 provider turn의 exact 상태를 재관측하지 못했습니다.",
                observation_unknown=True,
            )
            return self.service.load_runtime_job(job_id)
        if job.status is not RuntimeJobStatus.PROVIDER_TERMINAL:
            return job
        with self.service.ledger.read() as connection:
            terminal = connection.execute(
                "SELECT payload_json FROM runtime_job_observations "
                "WHERE job_id=? AND provider_terminal=1 ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        if terminal is None:
            raise EngineServiceError("CANCELLED_PROVIDER_TERMINAL_OBSERVATION_REQUIRED")
        payload = json.loads(terminal["payload_json"])
        result = payload.get("result")
        try:
            observation = RuntimeObservation.model_validate(result)
        except ValidationError as error:
            return self.finalize_cancelled_runtime_job(
                job_id,
                observation_error=RuntimePolicyError(
                    f"CANCELLED_PROVIDER_OBSERVATION_INVALID: {error}"
                ),
            )
        self._record_worker_usage(job.attempt_id, observation)
        document = self._bounded_failure_document({
            "error_code": "WORKFLOW_CANCELLED",
            "job_id": job_id,
            "provider_observation": observation.model_dump(mode="json"),
        })
        evidence_id = self._record_cancel_evidence(
            job,
            source_ref=f"runtime-cancelled:{job_id}",
            document=document,
        )
        self.service.finalize_cancelled_attempt(
            job_id=job_id,
            evidence_id=evidence_id,
            detail=(
                "사용자 취소 뒤 exact provider turn terminal을 재관측했습니다. "
                f"terminal_status={observation.terminal_status}"
            ),
        )
        return self.service.load_runtime_job(job_id)

    def _resume_bound_attempt(self, row: Any, spec: TaskExecutionSpecRevision) -> None:
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        with self.service.ledger.read() as connection:
            prior_job = connection.execute(
                "SELECT * FROM runtime_jobs WHERE attempt_id=? AND status IN "
                "('scheduled','running','interrupting','collector_lost','provider_terminal') "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],),
            ).fetchone()
        if prior_job is not None and prior_job["status"] != RuntimeJobStatus.PROVIDER_TERMINAL.value:
            prior = self.runtime.read_stored(
                thread_id=binding.thread_id, turn_id=binding.turn_id,
                timeout_seconds=5.0,
            )
            if prior.active or prior.terminal_status is None:
                raise EngineServiceError("provider terminal 관측 전에 Attempt를 재개할 수 없습니다.")
            self.service.record_runtime_job_observation(
                prior_job["id"], kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                payload={"result": prior.model_dump(mode="json")}, provider_terminal=True,
                terminal_status=prior.terminal_status,
            )
        if prior_job is not None:
            self.service.consume_runtime_job(prior_job["id"])
        validation_id = (
            self._validation_id_from_attempt(row["id"])
            if row["kind"] == AttemptKind.VALIDATION.value
            else None
        )
        role, _prompt, _output_schema = self._role_for_attempt(
            row, spec, validation_id=validation_id
        )
        cwd = Path(row["root"])
        self._verify_policy(cwd)
        inventory = self.runtime.list_models()
        for resolved_role in (spec.definition.executor, spec.definition.validator):
            if resolved_role is not None:
                self._verify_role_binding(resolved_role, inventory)
        current_binding = self._verify_role_binding(role, inventory)
        attempt_key = f"{spec.definition.idempotency_key}:attempt:{row['attempt_no']}:{row['kind']}"
        resume_count = 0
        with self.service.ledger.read() as connection:
            resume_count = connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id = ? "
                "AND kind = 'resume_turn' AND status = 'received'",
                (row["id"],),
            ).fetchone()[0]
            resumed_turn_received = connection.execute(
                "SELECT 1 FROM runtime_intents WHERE attempt_id=? AND kind='start_turn' "
                "AND status='received' AND idempotency_key=? LIMIT 1",
                (row["id"], f"{attempt_key}:resumed:turn"),
            ).fetchone()
        if resume_count and resumed_turn_received is not None:
            raise EngineServiceError(
                "같은 Attempt에서 이미 재개한 turn이 다시 중단돼 추가 resume을 거부합니다."
            )
        if resume_count:
            provider_call_id = self._attempt_provider_call_id(row["id"])
            if provider_call_id is None:
                raise EngineServiceError(
                    "저장된 resume receipt에 결속된 provider call이 없습니다."
                )
            self._attempt_provider_call_ids[row["id"]] = provider_call_id
            self._start_turn(
                row=row,
                spec=spec,
                thread_id=binding.thread_id,
                attempt_key=f"{attempt_key}:resumed",
                validation_id=validation_id,
                resumed=True,
                provider_call_id=provider_call_id,
            )
            return
        # create_thread receipt 뒤 아직 실제 turn을 시작하지 않은 예약은 같은 실행
        # 슬롯이다. 연결을 위한 resume RPC를 새 provider turn/호출로 세지 않는다.
        with self.service.ledger.read() as connection:
            unused_call = connection.execute(
                "SELECT id FROM provider_calls WHERE attempt_id=? AND execution_status='reserved' "
                "AND new_turn_count=0 ORDER BY rowid DESC LIMIT 1", (row["id"],),
            ).fetchone()
        if unused_call is not None:
            provider_call_id = unused_call["id"]
        else:
            from .budget import reserve_attempt_call
            provider_call_id = reserve_attempt_call(
                self.service, row, call_key=f"{attempt_key}:resumed",
                require_policy=getattr(self.runtime, "requires_budget_policy", False),
                request={"thread_id": binding.thread_id,
                         "model_observation": current_binding.model_dump(mode="json")},
            )
        self._attempt_provider_call_ids[row["id"]] = provider_call_id
        resume_prompt = (
            "이전 turn이 중단되었습니다. 같은 TaskContract 범위에서 재개하세요.\n"
            + _prompt
        )
        resume_request = {
            "thread_id": binding.thread_id,
            "cwd": str(cwd.resolve()),
            "task_id": row["task_id"],
            "model": role.model,
            "effort": role.effort,
            "model_observation": current_binding.model_dump(mode="json"),
            "validation_id": validation_id,
            "prompt_digest": sha256_digest(resume_prompt),
            "output_schema_digest": sha256_digest(_output_schema),
            "execution_spec_digest": spec.definition_digest,
            "prompt_binding_digest": (
                spec.definition.context_manifest.prompt_binding.binding_digest
                if row["kind"] == AttemptKind.EXECUTION.value
                else None
            ),
            "resume_notice": True,
        }
        if row["kind"] == AttemptKind.EXECUTION.value:
            task_contract = json.loads(row["task_json"])
            resume_request["effect_identities"] = [
                item["identity"] for item in task_contract["expected_effects"]
                if item["external"] and item.get("identity_version") == "2.0"
            ]
        resume_intent = self.service.prepare_runtime_intent(
            attempt_id=row["id"],
            kind=RuntimeIntentKind.RESUME_TURN,
            idempotency_key=f"{attempt_key}:resume",
            request=resume_request,
        )
        self._hit("after_resume_intent")
        trace = self._operation_trace(row["id"])
        receipt = self._invoke_runtime_operation(
            trace, "resume", resume_request,
            lambda: self.runtime.resume(thread_id=binding.thread_id, cwd=cwd),
            attempt_id=row["id"], intent_id=resume_intent.intent_id,
            call_id=provider_call_id, thread_id=binding.thread_id,
            deadline_seconds=getattr(spec.definition, "timeout_seconds", None),
        )
        receipt = self._attach_receipt_trace(receipt, trace)
        self._hit("after_resume_effect")
        self.service.record_runtime_receipt(
            intent_id=resume_intent.intent_id,
            provider_operation_id=f"resume:{receipt.operation_id}:{row['id']}",
            response=receipt.payload,
            binding=receipt.binding,
        )
        self._hit("after_resume_receipt")
        self._start_turn(
            row=row,
            spec=spec,
            thread_id=binding.thread_id,
            attempt_key=f"{attempt_key}:resumed",
            validation_id=validation_id,
            resumed=True,
            provider_call_id=provider_call_id,
        )

    def _observe_as_outcome(self, attempt_id: str) -> RunOnceOutcome:
        row, spec = self._attempt_context(attempt_id)
        if row["binding_json"] is None:
            raise EngineServiceError("Attempt runtime binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        if binding.turn_id is None:
            observation = self._read_attempt_runtime(
                attempt_id, thread_id=binding.thread_id, turn_id=None, seal_terminal=False,
            )
            if observation.active:
                return RunOnceOutcome(
                    action=RunOnceAction.OBSERVED,
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    attempt_id=attempt_id,
                    detail="저장된 thread를 먼저 관측했으며 아직 active입니다.",
                )
            self._resume_bound_attempt(row, spec)
            return RunOnceOutcome(
                action=RunOnceAction.DISPATCHED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                detail="thread/read 후 기존 thread를 resume하고 같은 Attempt의 turn을 시작했습니다.",
            )
        observation = self._read_attempt_runtime(
            attempt_id, thread_id=binding.thread_id, turn_id=binding.turn_id,
            seal_terminal=False,
        )
        with self.service.ledger.read() as connection:
            job_row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE attempt_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
            interrupt_row = (
                None
                if job_row is None
                else connection.execute(
                    "SELECT payload_json FROM runtime_job_observations "
                    "WHERE job_id=? AND kind=? ORDER BY rowid DESC LIMIT 1",
                    (
                        job_row["id"],
                        RuntimeJobObservationKind.INTERRUPT_REQUESTED.value,
                    ),
                ).fetchone()
            )
        if observation.active:
            if job_row is not None:
                self.service.record_runtime_job_observation(
                    job_row["id"], kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                    payload={"observation": observation.model_dump(mode="json")},
                )
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                detail="기존 Attempt가 아직 실행 중입니다.",
            )
        terminal = observation.terminal_status
        interrupt_request = (
            None if interrupt_row is None else json.loads(interrupt_row["payload_json"])
        )
        deadline_interrupted = (
            terminal == "interrupted"
            and interrupt_request is not None
            and interrupt_request.get("reason") == "absolute_deadline_exceeded"
        )
        if terminal is not None and job_row is not None and job_row["status"] not in {
            RuntimeJobStatus.PROVIDER_TERMINAL.value, RuntimeJobStatus.CONSUMED.value,
        }:
            self.service.record_runtime_job_observation(
                job_row["id"], kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                payload={"result": observation.model_dump(mode="json")},
                provider_terminal=True, terminal_status=terminal,
            )
        if terminal is not None and job_row is not None:
            self.service.consume_runtime_job(job_row["id"])
        if terminal in self._SUCCESS | self._FAILED or deadline_interrupted:
            observation = self._attach_observation_trace(
                observation, self._operation_trace(attempt_id), seal=True,
            )
        self._record_worker_usage(attempt_id, observation)
        if terminal in self._SUCCESS:
            if row["kind"] == AttemptKind.VALIDATION.value:
                evidence_ids, result_id = self._record_semantic_observation(
                    row, spec, observation
                )
                self.service.finish_attempt(attempt_id=attempt_id, succeeded=True)
                return RunOnceOutcome(
                    action=RunOnceAction.OBSERVED,
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    attempt_id=attempt_id,
                    validation_result_id=result_id,
                    evidence_ids=evidence_ids,
                    detail="독립 validator 결과를 typed observation과 evidence로 기록했습니다.",
                )
            evidence_ids, violation = self._collect_worker_evidence(row, spec, observation)
            self.service.confirm_worker_target_observation(
                attempt_id=attempt_id, evidence_ids=evidence_ids,
            )
            self._hit("after_execution_observed")
            self.service.finish_attempt(
                attempt_id=attempt_id,
                succeeded=violation is None,
                failure_class=None if violation is None else FailureClass.IMPLEMENTATION,
                detail=violation,
            )
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                evidence_ids=evidence_ids,
                detail=(
                    "worker 종료를 관측하고 직접 파일 evidence를 수집했습니다."
                    if violation is None
                    else "worker 종료 뒤 target 계약 위반을 관측했습니다: " + violation
                ),
            )
        if terminal in self._FAILED or deadline_interrupted:
            document = {
                "terminal_status": terminal,
                "final_response": observation.final_response,
                "provider_payload": observation.payload,
            }
            if deadline_interrupted:
                document["runtime_deadline"] = {
                    "error_code": "ABSOLUTE_DEADLINE_EXCEEDED",
                    "job_id": job_row["id"],
                    "absolute_deadline_at": job_row["absolute_deadline_at"],
                    "interrupt_request": interrupt_request,
                }
            evidence_id = new_id("evidence")
            diagnosis = self.failure_classifier.classify(FailureSignal(
                terminal_status=terminal,
                final_response=observation.final_response,
                # 로컬 absolute deadline은 provider가 동시에 돌려준 재시도 가능
                # 오류보다 우선하는 hard stop이다. 원문 payload는 evidence에 보존한다.
                provider_payload={} if deadline_interrupted else observation.payload,
                evidence_ids=(evidence_id,),
                evidence_documents=({
                    "kind": EvidenceKind.EXTERNAL_OBSERVATION.value,
                    "observation": document,
                },),
                local_engine_codes=(
                    ("ABSOLUTE_DEADLINE_EXCEEDED",) if deadline_interrupted else ()
                ),
            ))
            # 복구 단계가 잘린 원문을 재파싱하지 않도록 기록 시점 분류를 local_derived
            # projection으로 함께 남긴다. 원장 failure_class가 권위이며 이 projection은
            # code 출처와 일시 실패 표시만 보존한다.
            stored = self._bounded_failure_document(document | {
                "failure_diagnosis": {
                    "provenance": _FAILURE_DIAGNOSIS_PROVENANCE,
                    "failure_class": (
                        None if diagnosis.failure_class is None
                        else diagnosis.failure_class.value
                    ),
                    "error_code": diagnosis.error_code,
                    "provider_error_code": diagnosis.provider_error_code,
                    "local_engine_code": diagnosis.local_engine_code,
                    "transient": diagnosis.transient,
                    "source": diagnosis.source,
                    "model_reported_codes": list(diagnosis.model_reported_codes),
                },
            })
            failure_evidence = EvidenceRecord(
                evidence_id=evidence_id,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                kind=EvidenceKind.EXTERNAL_OBSERVATION,
                source_ref=(
                    f"runtime-deadline:{job_row['id']}"
                    if deadline_interrupted
                    else f"codex-terminal:{observation.thread_id}:{observation.turn_id}"
                ),
                observation=json.dumps(stored, ensure_ascii=False, sort_keys=True),
                content_digest=sha256_digest(stored),
                observed_at=utc_now(),
            )
            self.service.record_evidence(failure_evidence)
            failure_class = diagnosis.failure_class or FailureClass.UNCLASSIFIED
            self.service.finish_attempt(
                attempt_id=attempt_id,
                succeeded=False,
                failure_class=failure_class,
                detail=diagnosis.rationale,
            )
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                evidence_ids=(failure_evidence.evidence_id,),
                detail=(
                    f"Attempt 실패를 관측했습니다: {terminal}; "
                    f"classification={failure_class.value}"
                ),
            )
        try:
            self._resume_bound_attempt(row, spec)
        except EngineServiceError as error:
            from .budget import BudgetBlocked
            if isinstance(error, BudgetBlocked):
                # 호출 전 예산 차단은 기존 Attempt의 실패나 resume 소진이 아니다.
                raise
            with self.service.ledger.read() as connection:
                preflight = connection.execute(
                    "SELECT h.payload_json FROM history_events h "
                    "JOIN runtime_intents i ON i.id = h.entity_id "
                    "WHERE i.attempt_id = ? AND i.status = 'prepared' "
                    "AND h.event_type = 'runtime.effect_not_started' "
                    "ORDER BY h.sequence DESC LIMIT 1",
                    (attempt_id,),
                ).fetchone()
            if preflight is not None:
                payload = json.loads(preflight["payload_json"])
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    attempt_id=attempt_id,
                    blocker_code=payload.get(
                        "code", "RUNTIME_EFFECT_PREFLIGHT_FAILED"
                    ),
                    detail=str(error),
                )
            evidence_id = new_id("evidence")
            resume_document = {
                "error_code": "RESUME_EXHAUSTED",
                "thread_id": observation.thread_id,
                "turn_id": observation.turn_id,
                "provider_payload": observation.payload,
                "detail": str(error),
            }
            diagnosis = self.failure_classifier.classify(FailureSignal(
                terminal_status=terminal,
                final_response=observation.final_response,
                provider_payload={},
                evidence_ids=(evidence_id,),
                evidence_documents=({
                    "kind": EvidenceKind.EXTERNAL_OBSERVATION.value,
                    "observation": resume_document,
                },),
                local_engine_codes=("RESUME_EXHAUSTED",),
            ))
            stored_resume = self._bounded_failure_document(resume_document | {
                "failure_diagnosis": {
                    "provenance": _FAILURE_DIAGNOSIS_PROVENANCE,
                    "failure_class": (
                        None if diagnosis.failure_class is None
                        else diagnosis.failure_class.value
                    ),
                    "error_code": diagnosis.error_code,
                    "provider_error_code": diagnosis.provider_error_code,
                    "local_engine_code": diagnosis.local_engine_code,
                    "transient": diagnosis.transient,
                    "source": diagnosis.source,
                    "model_reported_codes": list(diagnosis.model_reported_codes),
                },
            })
            resume_evidence = EvidenceRecord(
                evidence_id=evidence_id,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                kind=EvidenceKind.EXTERNAL_OBSERVATION,
                source_ref=(
                    f"runtime-resume-exhausted:{observation.thread_id}:"
                    f"{observation.turn_id}"
                ),
                observation=json.dumps(stored_resume, ensure_ascii=False, sort_keys=True),
                content_digest=sha256_digest(stored_resume),
                observed_at=utc_now(),
            )
            self.service.record_evidence(resume_evidence)
            self.service.finish_attempt(
                attempt_id=attempt_id,
                succeeded=False,
                failure_class=diagnosis.failure_class or FailureClass.ENVIRONMENT,
                detail=diagnosis.rationale,
            )
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                evidence_ids=(resume_evidence.evidence_id,),
                blocker_code="RESUME_EXHAUSTED",
                detail=str(error),
            )
        return RunOnceOutcome(
            action=RunOnceAction.DISPATCHED,
            project_id=row["project_id"],
            task_id=row["task_id"],
            attempt_id=attempt_id,
            detail="thread/read에서 미완료 turn을 확인한 뒤 기존 binding을 재개했습니다.",
        )

    def _collect_worker_evidence(
        self,
        row: Any,
        spec: TaskExecutionSpecRevision,
        observation: RuntimeObservation,
    ) -> tuple[tuple[str, ...], str | None]:
        evidence_ids: list[str] = []
        report_document = {
            "terminal_status": observation.terminal_status,
            "final_response": observation.final_response,
            "payload": observation.payload,
        }
        report_source_ref = f"codex-thread:{observation.thread_id}"
        if len(observation.final_response or "") > 10_000:
            report_source_ref += ":truncated"
        report = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=row["project_id"],
            task_id=row["task_id"],
            attempt_id=row["id"],
            kind=EvidenceKind.EXTERNAL_OBSERVATION,
            source_ref=report_source_ref,
            observation=(observation.final_response or "worker turn completed")[:10_000],
            content_digest=sha256_digest(report_document),
            observed_at=utc_now(),
        )
        self.service.record_evidence(report)
        evidence_ids.append(report.evidence_id)
        root = Path(row["root"])
        violations: list[str] = []
        for target in spec.definition.resolved_targets:
            path = Path(target.path)
            if not path.is_absolute():
                path = root / path
            exists = path.is_file()
            after_bytes = path.read_bytes() if exists else None
            after_digest = sha256_bytes(after_bytes) if after_bytes is not None else None
            before_digest = target.expected_content_digest
            document = {
                "path": target.path,
                "access": target.access,
                "before_digest": before_digest,
                "after_digest": after_digest,
                "exists": exists,
                "file_excerpt": None if after_bytes is None else after_bytes[:6000].decode("utf-8", errors="replace")[:3000],
                "excerpt_truncated": after_bytes is not None and (len(after_bytes) > 6000 or len(after_bytes.decode("utf-8", errors="replace")) > 3000),
            }
            kind = EvidenceKind.FILE if exists else EvidenceKind.DIFF
            file_evidence = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=row["id"],
                kind=kind,
                source_ref=target.path,
                observation=json.dumps(document, ensure_ascii=False, sort_keys=True),
                content_digest=sha256_digest(document),
                observed_at=utc_now(),
            )
            self.service.record_evidence(file_evidence)
            evidence_ids.append(file_evidence.evidence_id)
            if before_digest != after_digest:
                diff_evidence = EvidenceRecord(
                    evidence_id=new_id("evidence"),
                    project_id=row["project_id"],
                    task_id=row["task_id"],
                    attempt_id=row["id"],
                    kind=EvidenceKind.DIFF,
                    source_ref=target.path,
                    observation=json.dumps(document, ensure_ascii=False, sort_keys=True),
                    content_digest=sha256_digest({"diff": document}),
                    observed_at=utc_now(),
                )
                self.service.record_evidence(diff_evidence)
                evidence_ids.append(diff_evidence.evidence_id)
            if target.access == "read" and before_digest != after_digest:
                violations.append(f"read-only target changed: {target.path}")
            if target.access == "create" and not exists:
                violations.append(f"create target missing: {target.path}")
            if target.access == "write" and not exists:
                violations.append(f"write target missing: {target.path}")
            if target.access == "delete" and exists:
                violations.append(f"delete target still exists: {target.path}")
        return tuple(evidence_ids), "; ".join(violations) or None

    def _record_semantic_observation(
        self,
        row: Any,
        spec: TaskExecutionSpecRevision,
        observation: RuntimeObservation,
    ) -> tuple[tuple[str, ...], str]:
        validation_id = self._validation_id_from_attempt(row["id"])
        try:
            payload = json.loads(observation.final_response or "")
            semantic = SemanticValidationObservation(
                validation_id=validation_id,
                task_id=row["task_id"],
                reviewer_role=spec.definition.validator.role,  # type: ignore[union-attr]
                model=spec.definition.validator.model,  # type: ignore[union-attr]
                effort=spec.definition.validator.effort,  # type: ignore[union-attr]
                passed=payload["passed"],
                rationale=payload["rationale"],
                evidence_refs=tuple(payload["evidence_refs"]),
                observed_at=utc_now(),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise EngineServiceError("validator structured observation이 유효하지 않습니다.") from error
        with self.service.ledger.read() as connection:
            intents = connection.execute("SELECT request_json FROM runtime_intents WHERE attempt_id = ?",
                                         (row["id"],)).fetchall()
        provided = next((set(item["semantic_evidence_ids"]) for item in
                         (json.loads(intent["request_json"]) for intent in intents)
                         if "semantic_evidence_ids" in item), set())
        if not set(semantic.evidence_refs).issubset(provided):
            raise EngineServiceError("validator가 제공되지 않은 evidence ref를 반환했습니다.")
        self._check_semantic_evidence_fresh(
            self._semantic_evidence_catalog(row, spec, validation_id), Path(row["root"])
        )
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=row["project_id"],
            task_id=row["task_id"],
            attempt_id=row["id"],
            kind=EvidenceKind.MODEL_REVIEW,
            source_ref=f"codex-validator:{observation.thread_id}",
            observation=semantic.model_dump_json()[:10_000],
            content_digest=sha256_digest(semantic),
            observed_at=semantic.observed_at,
        )
        self.service.record_evidence(evidence)
        result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id=validation_id,
            task_id=row["task_id"],
            status=ValidationStatus.PASS if semantic.passed else ValidationStatus.FAIL,
            evidence_ids=(evidence.evidence_id, *semantic.evidence_refs),
            rationale=semantic.rationale,
            evaluated_at=utc_now(),
        )
        self.service.record_validation(
            project_id=row["project_id"],
            plan_revision_id=row["plan_revision_id"],
            result=result,
        )
        return (evidence.evidence_id,), result.validation_result_id

    def _task_gate_decision(self, phase: str, task: Any) -> str | RunOnceOutcome | None:
        """task gate의 차단 사유를 돌려준다. 판정 보류·계약 불일치·효과 미확정은 상태 전이 없이 BLOCKED로 끝낸다."""
        if self.task_gate is None:
            return None
        from .operations import ExternalOperationUnknown
        try:
            return getattr(self.task_gate, phase)(task)
        except TaskGatePending as pending:
            return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=task["project_id"], task_id=task["id"],
                                  blocker_code="GOVERNANCE_GATE_PENDING", detail=str(pending))
        except TaskGateContractMismatch as mismatch:
            # dispatch·완료 두 단계 모두 상태 전이 없이 사유만 남긴다. 같은 사유는 History에 한 번만 남는다.
            self.service.record_task_gate_block(task_id=task["id"], phase=phase, detail=str(mismatch))
            return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=task["project_id"], task_id=task["id"],
                                  blocker_code="GOVERNANCE_CONTRACT_MISMATCH", detail=str(mismatch))
        except ExternalOperationUnknown as error:
            return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=task["project_id"], task_id=task["id"],
                                  blocker_code="EXTERNAL_EFFECT_UNKNOWN", failure_class=FailureClass.EXTERNAL_UNKNOWN,
                                  suggested_repair_action=RepairAction.WAIT_EXTERNAL, checkpoint_required=True,
                                  detail=str(error))

    def _advance_validation(self, task: Any) -> RunOnceOutcome:
        with self.service.ledger.read() as connection:
            spec_row = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE task_id = ? AND is_current = 1",
                (task["id"],),
            ).fetchone()
            rows = self.service.effective_task_validation_results(connection, task["id"])
        if spec_row is None:
            raise EngineServiceError("validating Task의 ExecutionSpec이 없습니다.")
        spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
        latest = {row["validation_id"]: row for row in rows}
        for step in spec.definition.validation_steps:
            result = latest.get(step.validation_id)
            if result is not None and result["status"] == ValidationStatus.FAIL.value:
                self.service.block_task_from_validation(
                    task_id=task["id"],
                    detail=f"validation FAIL: {step.validation_id}",
                )
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=task["project_id"],
                    task_id=task["id"],
                    validation_result_id=json.loads(result["payload_json"])["validation_result_id"],
                    blocker_code="TASK_VALIDATION_FAILED",
                    detail=f"Task validation이 실패했습니다: {step.validation_id}",
                )
            if result is not None and result["status"] == ValidationStatus.PASS.value:
                continue
            if step.method == "deterministic":
                return self._run_deterministic_validation(task, step)
            if step.method == "semantic":
                attempt = self.service.reserve_attempt(
                    task_id=task["id"], kind=AttemptKind.VALIDATION
                )
                if self.supervisor is None:
                    self._dispatch_reserved(
                        attempt.attempt_id, validation_id=step.validation_id
                    )
                else:
                    self._dispatch_attempt_job(
                        attempt.attempt_id, validation_id=step.validation_id
                    )
                return RunOnceOutcome(
                    action=RunOnceAction.DISPATCHED,
                    project_id=task["project_id"],
                    task_id=task["id"],
                    attempt_id=attempt.attempt_id,
                    detail=f"독립 semantic validator를 dispatch했습니다: {step.validation_id}",
                )
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=task["project_id"],
                task_id=task["id"],
                blocker_code=(
                    "MANUAL_OBSERVATION_REQUIRED"
                    if step.method == "manual"
                    else "EXTERNAL_OBSERVATION_REQUIRED"
                ),
                detail=f"typed {step.method} observation이 필요합니다: {step.validation_id}",
            )
        blocker = self._task_gate_decision("before_completion", task)
        if isinstance(blocker, RunOnceOutcome):
            return blocker
        if blocker is not None:
            # 완료 뒤 State 재관측은 바뀐 파일을 새 기준으로 흡수하므로 gate는 그 전에 막는다.
            self.service.block_task_from_validation(task_id=task["id"], detail=f"governance gate: {blocker}")
            return RunOnceOutcome(action=RunOnceAction.BLOCKED, project_id=task["project_id"],
                task_id=task["id"], blocker_code="GOVERNANCE_GATE_FAILED", detail=blocker)
        ready = self.service.complete_task(task["id"])
        self._hit("before_state_reobservation")
        self.service.reobserve_project(task["project_id"], force_state_revision=True)
        self._hit("after_state_reobservation")
        return RunOnceOutcome(
            action=RunOnceAction.COMPLETED,
            project_id=task["project_id"],
            task_id=task["id"],
            detail=(
                "모든 Task validation이 PASS여서 Task를 완료하고 Project Map·State를 재관측했습니다. "
                f"새 ready Task: {list(ready)}"
            ),
        )

    def _run_deterministic_validation(self, task: Any, step: Any) -> RunOnceOutcome:
        from .validation_execution import run_command_validation
        return run_command_validation(self.service, task, step, fault_hook=self.fault_hook)


    def _advance_goal_test(
        self,
        project_id: str,
        plan: PlanContractRevision,
        *, goal_validation_step: Any | None = None,
        goal_validation_retry: Any | None = None,
    ) -> RunOnceOutcome:
        with self.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT * FROM validation_results WHERE plan_revision_id = ? "
                "AND task_id IS NULL "
                "ORDER BY evaluated_at, rowid",
                (plan.plan_revision_id,),
            ).fetchall()
            rows += [row for task in plan.definition.tasks
                     for row in self.service.effective_task_validation_results(connection, task.task_id)]
            existing_verdict = connection.execute(
                "SELECT payload_json FROM goal_verdicts WHERE plan_revision_id = ? "
                "ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                (plan.plan_revision_id,),
            ).fetchone()
        if existing_verdict is not None:
            verdict = GoalVerdict.model_validate_json(existing_verdict["payload_json"])
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=project_id,
                goal_verdict_id=verdict.goal_verdict_id,
                blocker_code="GOAL_NOT_SATISFIED",
                detail=f"기존 GoalVerdict가 재계획을 요구합니다: {verdict.status.value}",
            )
        latest = {row["validation_id"]: row for row in rows}
        for contract in plan.definition.integration_validations:
            recorded = latest.get(contract.validation_id)
            if recorded is not None:
                result = ValidationResult.model_validate_json(recorded["payload_json"])
                retry_pending = False
                if contract.evidence_mode == "independent":
                    from .validation_execution import _goal_bindings
                    bindings = _goal_bindings(
                        self.service, project_id, plan.plan_revision_id, contract.validation_id,
                    )
                    current_binding = None if not bindings else bindings[0][0]
                    retry_pending = bool(
                        current_binding is not None
                        and current_binding.retry is not None
                        and current_binding.retry.failed_validation_result_id == result.validation_result_id
                        and len(bindings) > 1
                        and bindings[1][0].binding_digest == current_binding.retry.prior_binding_digest
                        and (
                            result.goal_validation_binding_digest is None
                            or result.goal_validation_binding_digest == current_binding.retry.prior_binding_digest
                        )
                    )
                    semantic_binding_missing = (
                        contract.method == "semantic"
                        and result.goal_validation_binding_digest is None
                    )
                    binding_mismatch = (
                        result.goal_validation_binding_digest is not None
                        and not retry_pending
                        and (
                            current_binding is None
                            or current_binding.binding_digest
                            != result.goal_validation_binding_digest
                        )
                    )
                    if semantic_binding_missing or binding_mismatch:
                        return RunOnceOutcome(
                            action=RunOnceAction.BLOCKED, project_id=project_id,
                            validation_result_id=result.validation_result_id,
                            blocker_code="GOAL_TEST_RESULT_BINDING_MISMATCH",
                            detail="현재 Goal Test binding과 최신 validation 결과의 digest가 다릅니다.",
                        )
                if recorded["status"] == ValidationStatus.FAIL.value:
                    if goal_validation_retry is not None:
                        if contract.evidence_mode != "independent" or contract.method != "deterministic":
                            return RunOnceOutcome(
                                action=RunOnceAction.BLOCKED, project_id=project_id,
                                validation_result_id=result.validation_result_id,
                                blocker_code="GOAL_VALIDATION_RETRY_UNSUPPORTED",
                                detail="독립 deterministic Goal Test만 운영 상세 재시도를 지원합니다.",
                            )
                        from .validation_execution import advance_independent_goal_test
                        try:
                            return advance_independent_goal_test(
                                self.service, self.runtime, project_id, plan, contract,
                                retry_request=goal_validation_retry, provider=self.proposal_provider,
                                fault_hook=self.fault_hook, supervisor=self.supervisor,
                            )
                        except EngineServiceError as error:
                            return RunOnceOutcome(
                                action=RunOnceAction.BLOCKED, project_id=project_id,
                                validation_result_id=result.validation_result_id,
                                blocker_code="GOAL_VALIDATION_RETRY_INVALID",
                                detail=str(error),
                            )
                    if retry_pending:
                        from .validation_execution import advance_independent_goal_test
                        return advance_independent_goal_test(
                            self.service, self.runtime, project_id, plan, contract,
                            provider=self.proposal_provider, fault_hook=self.fault_hook,
                            supervisor=self.supervisor,
                        )
                    return self._record_goal_verdict(project_id, plan, latest)
                if recorded["status"] == ValidationStatus.PASS.value:
                    continue
            if contract.evidence_mode == "independent":
                from .validation_execution import advance_independent_goal_test
                return advance_independent_goal_test(
                    self.service, self.runtime, project_id, plan, contract,
                    supplied_step=goal_validation_step, provider=self.proposal_provider,
                    fault_hook=self.fault_hook, supervisor=self.supervisor,
                )
            if contract.method != "deterministic":
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    blocker_code="GOAL_VALIDATION_OBSERVATION_REQUIRED",
                    detail=(
                        "plan-level semantic/manual/external Goal Test에는 별도 typed observation이 "
                        f"필요합니다: {contract.validation_id}"
                    ),
                )
            criterion_refs = set(contract.criterion_refs)
            covered = [
                item
                for item in plan.definition.goal_coverage
                if item.criterion_id in criterion_refs
            ]
            required_validation_ids = {
                validation_id
                for coverage in covered
                for validation_id in coverage.validation_ids
                if validation_id != contract.validation_id
            }
            if any(
                validation_id not in latest
                or latest[validation_id]["status"] != ValidationStatus.PASS.value
                for validation_id in required_validation_ids
            ):
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    blocker_code="GOAL_TEST_INPUT_INCOMPLETE",
                    detail=f"Goal Test 선행 validation evidence가 부족합니다: {contract.validation_id}",
                )
            evidence_ids = {
                evidence_id
                for validation_id in required_validation_ids
                for evidence_id in json.loads(latest[validation_id]["payload_json"])["evidence_ids"]
            }
            with self.service.ledger.read() as connection:
                if evidence_ids:
                    placeholders = ",".join("?" for _ in evidence_ids)
                    evidence_rows = connection.execute(
                        f"SELECT id, kind FROM evidence_records WHERE project_id = ? "
                        f"AND id IN ({placeholders})",
                        (project_id, *tuple(evidence_ids)),
                    ).fetchall()
                else:
                    evidence_rows = ()
            by_kind = {row["kind"]: row["id"] for row in evidence_rows}
            missing_kinds = set(contract.required_evidence_kinds) - set(by_kind)
            if missing_kinds:
                return RunOnceOutcome(
                    action=RunOnceAction.BLOCKED,
                    project_id=project_id,
                    blocker_code="GOAL_TEST_EVIDENCE_REQUIRED",
                    detail="Goal Test evidence kind가 부족합니다: " + ", ".join(sorted(missing_kinds)),
                )
            bound_ids = tuple(by_kind[kind] for kind in contract.required_evidence_kinds)
            result = ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=contract.validation_id,
                status=ValidationStatus.PASS,
                evidence_ids=bound_ids,
                rationale="완료 Task의 PASS validation과 필수 evidence kind를 Core가 재검산했습니다.",
                evaluated_at=utc_now(),
            )
            self.service.record_validation(
                project_id=project_id,
                plan_revision_id=plan.plan_revision_id,
                result=result,
            )
            return RunOnceOutcome(
                action=RunOnceAction.VALIDATED,
                project_id=project_id,
                validation_result_id=result.validation_result_id,
                evidence_ids=bound_ids,
                detail=f"plan-level Goal Test를 계산했습니다: {contract.validation_id}",
            )
        return self._record_goal_verdict(project_id, plan, latest)

    def _record_goal_verdict(
        self,
        project_id: str,
        plan: PlanContractRevision,
        latest_rows: dict[str, Any],
    ) -> RunOnceOutcome:
        criteria: list[CriterionVerdict] = []
        for coverage in plan.definition.goal_coverage:
            statuses: list[ValidationStatus] = []
            evidence_ids: set[str] = set()
            for validation_id in coverage.validation_ids:
                row = latest_rows.get(validation_id)
                if row is None:
                    statuses.append(ValidationStatus.INCONCLUSIVE)
                    continue
                result = ValidationResult.model_validate_json(row["payload_json"])
                statuses.append(result.status)
                evidence_ids.update(result.evidence_ids)
            status = (
                ValidationStatus.FAIL
                if ValidationStatus.FAIL in statuses
                else (
                    ValidationStatus.PASS
                    if statuses and set(statuses) == {ValidationStatus.PASS}
                    else ValidationStatus.INCONCLUSIVE
                )
            )
            criteria.append(
                CriterionVerdict(
                    criterion_id=coverage.criterion_id,
                    status=status,
                    evidence_ids=tuple(sorted(evidence_ids)),
                    rationale="coverage에 결속된 Task validation과 Goal Test의 최신 결과를 집계했습니다.",
                )
            )
        statuses = {item.status for item in criteria}
        verdict_status = (
            GoalVerdictStatus.NOT_SATISFIED
            if ValidationStatus.FAIL in statuses
            else (
                GoalVerdictStatus.SATISFIED
                if statuses == {ValidationStatus.PASS}
                else GoalVerdictStatus.INCONCLUSIVE
            )
        )
        integration_results = tuple(
            ValidationResult.model_validate_json(latest_rows[item.validation_id]["payload_json"])
            for item in plan.definition.integration_validations
            if item.validation_id in latest_rows
        )
        verdict = GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"),
            goal_contract_digest=plan.definition.goal_contract_digest,
            plan_activation_digest=plan.activation_digest,
            status=verdict_status,
            criteria=tuple(criteria),
            integration_validation_result_ids=tuple(
                item.validation_result_id for item in integration_results
            ),
            evaluated_at=utc_now(),
        )
        self.service.record_goal_verdict(
            project_id=project_id,
            plan_revision_id=plan.plan_revision_id,
            verdict=verdict,
        )
        return RunOnceOutcome(
            action=(
                RunOnceAction.COMPLETED
                if verdict_status is GoalVerdictStatus.SATISFIED
                else RunOnceAction.BLOCKED
            ),
            project_id=project_id,
            goal_verdict_id=verdict.goal_verdict_id,
            blocker_code=(
                None
                if verdict_status is GoalVerdictStatus.SATISFIED
                else "GOAL_NOT_SATISFIED"
            ),
            detail=f"evidence-backed GoalVerdict를 기록했습니다: {verdict_status.value}",
        )

    def observe_attempt(self, attempt_id: str) -> RuntimeObservation:
        row, _spec = self._attempt_context(attempt_id)
        if row["binding_json"] is None:
            raise EngineServiceError("Attempt runtime binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        observation = self._read_attempt_runtime(
            attempt_id, thread_id=binding.thread_id, turn_id=binding.turn_id,
            seal_terminal=False,
        )
        if row["status"] in {"reserved", "starting", "running"}:
            self._observe_as_outcome(attempt_id)
            observation = self._attach_observation_trace(
                observation, self._operation_trace(attempt_id)
            )
        usage = self._record_worker_usage(attempt_id, observation)
        if usage is not None:
            observation = observation.model_copy(update={"payload": observation.payload | {
                "worker_usage_id": usage.usage_id, "usage_available": usage.usage_available,
            }})
        return observation

    def resume_attempt(self, attempt_id: str) -> RuntimeOperationReceipt:
        row, spec = self._attempt_context(attempt_id)
        if row["binding_json"] is None:
            raise EngineServiceError("resume할 기존 binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        observation = self._read_attempt_runtime(
            attempt_id, thread_id=binding.thread_id, turn_id=binding.turn_id,
            seal_terminal=False,
        )
        if observation.active:
            raise EngineServiceError("thread/read 결과가 active이므로 resume하지 않습니다.")
        self._verify_policy(Path(row["root"]))
        inventory = self.runtime.list_models()
        for role in (spec.definition.executor, spec.definition.validator):
            if role is not None:
                self._verify_role_binding(role, inventory)
        validation_id = (
            self._validation_id_from_attempt(attempt_id)
            if row["kind"] == AttemptKind.VALIDATION.value
            else None
        )
        role, prompt, output_schema = self._role_for_attempt(
            row, spec, validation_id=validation_id
        )
        current_binding = self._verify_role_binding(role, inventory)
        request = {
            "thread_id": binding.thread_id,
            "cwd": str(Path(row["root"]).resolve()),
            "task_id": row["task_id"],
            "model": role.model,
            "effort": role.effort,
            "model_observation": current_binding.model_dump(mode="json"),
            "validation_id": validation_id,
            "prompt_digest": sha256_digest(prompt),
            "output_schema_digest": sha256_digest(output_schema),
            "execution_spec_digest": spec.definition_digest,
            "prompt_binding_digest": (
                spec.definition.context_manifest.prompt_binding.binding_digest
                if row["kind"] == AttemptKind.EXECUTION.value
                else None
            ),
            "resume_notice": False,
        }
        if row["kind"] == AttemptKind.EXECUTION.value:
            task_contract = json.loads(row["task_json"])
            request["effect_identities"] = [
                item["identity"] for item in task_contract["expected_effects"]
                if item["external"] and item.get("identity_version") == "2.0"
            ]
        attempt_key = (
            f"{spec.definition.idempotency_key}:attempt:{row['attempt_no']}:{row['kind']}"
        )
        intent = self.service.prepare_runtime_intent(
            attempt_id=attempt_id,
            kind=RuntimeIntentKind.RESUME_TURN,
            idempotency_key=f"{attempt_key}:public-resume",
            request=request,
        )
        if intent.status.value == "received":
            with self.service.ledger.read() as connection:
                stored = connection.execute(
                    "SELECT payload_json FROM runtime_receipts WHERE intent_id = ?",
                    (intent.intent_id,),
                ).fetchone()
            if stored is None:
                raise EngineServiceError("received resume intent의 receipt가 없습니다.")
            receipt = json.loads(stored["payload_json"])
            return RuntimeOperationReceipt(
                operation_id=receipt["provider_operation_id"],
                payload=receipt["response_payload"],
                binding=(
                    None
                    if receipt["binding"] is None
                    else ThreadBinding.model_validate(receipt["binding"])
                ),
            )
        trace = self._operation_trace(attempt_id)
        receipt = self._invoke_runtime_operation(
            trace, "resume", request,
            lambda: self.runtime.resume(thread_id=binding.thread_id, cwd=Path(row["root"])),
            attempt_id=attempt_id, intent_id=intent.intent_id,
            call_id=self._attempt_provider_call_id(attempt_id),
            thread_id=binding.thread_id,
        )
        receipt = self._attach_receipt_trace(receipt, trace)
        self.service.record_runtime_receipt(
            intent_id=intent.intent_id,
            provider_operation_id=f"resume:{receipt.operation_id}:{attempt_id}:public",
            response=receipt.payload,
            binding=receipt.binding,
        )
        return receipt.model_copy(
            update={
                "payload": receipt.payload
                | {"prior_thread_read": observation.model_dump(mode="json")}
            }
        )

    def interrupt_attempt(self, attempt_id: str) -> RuntimeOperationReceipt:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None or row["binding_json"] is None:
            raise EngineServiceError("interrupt할 runtime binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        if binding.turn_id is None:
            raise EngineServiceError("interrupt할 turn binding이 없습니다.")
        trace = self._operation_trace(attempt_id)
        receipt = self._invoke_runtime_operation(
            trace, "interrupt", {"thread_id": binding.thread_id, "turn_id": binding.turn_id},
            lambda: self.runtime.interrupt(
                thread_id=binding.thread_id, turn_id=binding.turn_id
            ),
            attempt_id=attempt_id, call_id=self._attempt_provider_call_id(attempt_id),
            thread_id=binding.thread_id, turn_id=binding.turn_id,
            deadline_seconds=5.0,
        )
        return self._attach_receipt_trace(receipt, trace)
