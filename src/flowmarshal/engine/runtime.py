from __future__ import annotations

import json
import hashlib
import os
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import Field

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
    RepairAction,
    RunOnceAction,
    RunOnceOutcome,
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
from .service import ContextRequiredError, EngineService, EngineServiceError


REQUIRED_PERMISSION_PROFILE = ":danger-full-access"
REQUIRED_APPROVAL_POLICY = "never"


class RuntimePolicyError(RuntimeError):
    pass


class ExecutionPolicyEvidence(EngineModel):
    environment: str = "local"
    permission_profile: str
    approval_policy: str
    config_digest: str
    profile_catalog_digest: str
    cwd: str


class RuntimeOperationReceipt(EngineModel):
    operation_id: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any]
    binding: ThreadBinding | None = None


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

    def interrupt(self, *, thread_id: str, turn_id: str) -> RuntimeOperationReceipt: ...

    def close(self) -> None: ...


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

    def __init__(self, *, codex_bin: Path | str | None = None) -> None:
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

    def close(self) -> None:
        for handle, future in tuple(self._turn_futures.values()):
            if future.done():
                continue
            try:
                handle.interrupt()
            except Exception:
                pass
        self._codex.close()

    def wait_for_active_turns(self, *, timeout_seconds: float) -> bool:
        """CLI가 소유한 연결을 dispatch 직후 닫아 실행을 끊지 않도록 유지한다.

        Core 상태는 전이하지 않는다. 성공·실패의 원장 반영은 다음 observe가 한다.
        """
        deadline = time.monotonic() + timeout_seconds
        for _handle, future in tuple(self._turn_futures.values()):
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
        return True

    def __enter__(self) -> "CodexAppServerRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _raw(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._codex._client._request_raw(method, params)  # noqa: SLF001
        if not isinstance(response, dict):
            raise RuntimePolicyError(f"{method} 응답이 JSON object가 아닙니다.")
        return response

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
        response = self._codex.models(include_hidden=False)
        if _get(response, "next_cursor") is not None:
            raise RuntimePolicyError("model/list pagination을 완전히 읽지 못했습니다.")
        raw_models = _get(response, "data")
        if not isinstance(raw_models, list):
            raw_models = tuple(raw_models or ())
        models: list[ModelCapability] = []
        for item in raw_models:
            if bool(_get(item, "hidden", False)):
                continue
            model_id = str(_get(item, "id", _get(item, "model", ""))).strip()
            options = _get(item, "supported_reasoning_efforts", ())
            efforts = tuple(
                sorted(
                    {
                        _enum(_get(option, "reasoning_effort", _get(option, "effort", option)))
                        for option in options
                    }
                )
            )
            if model_id and efforts:
                models.append(ModelCapability(model=model_id, supported_efforts=efforts))
        if not models:
            raise RuntimePolicyError("model/list에 visible model이 없습니다.")
        return ModelInventory(
            source=f"codex-app-server:model/list@{self.executable_digest}",
            models=tuple(models),
        )

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
        self.verify_execution_policy(cwd)
        response = self._raw(
            "thread/start",
            {
                "approvalPolicy": REQUIRED_APPROVAL_POLICY,
                "cwd": str(cwd.resolve()),
                "developerInstructions": developer_instructions,
                "ephemeral": ephemeral,
                "model": model,
                "permissions": REQUIRED_PERMISSION_PROFILE,
            },
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
        binding = ThreadBinding(thread_id=thread_id, bound_at=utc_now())
        if ephemeral:
            self._ephemeral_thread_ids.add(thread_id)
        return RuntimeOperationReceipt(
            operation_id=thread_id,
            payload=response,
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
        from openai_codex import ApprovalMode, Sandbox
        from openai_codex.api import Thread

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
        handle = thread.turn(prompt, **turn_arguments)
        future: Future[Any] = Future()

        def consume_turn() -> None:
            try:
                future.set_result(handle.run())
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
                "permission_profile": REQUIRED_PERMISSION_PROFILE,
                "approval_policy": REQUIRED_APPROVAL_POLICY,
            },
            binding=binding,
        )

    def read(self, *, thread_id: str) -> RuntimeObservation:
        tracked = self._turn_futures.get(thread_id)
        if tracked is not None:
            handle, future = tracked
            if not future.done():
                return RuntimeObservation(
                    thread_id=thread_id,
                    turn_id=handle.id,
                    active=True,
                    payload={"thread_id": thread_id, "turn_id": handle.id, "usage": None},
                )
            try:
                turn_result = future.result()
            except BaseException as error:
                return RuntimeObservation(
                    thread_id=thread_id,
                    turn_id=handle.id,
                    active=False,
                    terminal_status="failed",
                    final_response=str(error),
                    payload={
                        "thread_id": thread_id,
                        "turn_id": handle.id,
                        "error": f"{type(error).__name__}: {error}",
                        "usage": None,
                    },
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
                },
            )
        if thread_id in self._ephemeral_thread_ids:
            raise RuntimePolicyError("ephemeral thread의 active turn handle이 없습니다.")
        return self.read_stored(thread_id=thread_id)

    def read_stored(self, *, thread_id: str) -> RuntimeObservation:
        """로컬 active handle과 별개로 thread/read의 저장 상태를 확인한다."""
        result = self._codex._client.thread_read(thread_id, include_turns=True)  # noqa: SLF001
        turns = tuple(result.thread.turns)
        latest = None if not turns else turns[-1]
        status = None if latest is None else _enum(latest.status)
        final_response: str | None = None
        if latest is not None:
            latest_document = latest.model_dump(mode="json", by_alias=True)
            for item in latest.items:
                document = item.model_dump(mode="json", by_alias=True)
                if document.get("type") == "agentMessage" and isinstance(document.get("text"), str):
                    final_response = document["text"]
        else:
            latest_document = {}
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=None if latest is None else latest.id,
            active=status in {"inProgress", "in_progress"},
            terminal_status=None if latest is None or status in {"inProgress", "in_progress"} else status,
            final_response=final_response,
            payload={
                "thread_id": thread_id,
                "turn_count": len(turns),
                "turn_status": status,
                "usage": latest_document.get("usage"),
            },
        )

    def resume(self, *, thread_id: str, cwd: Path) -> RuntimeOperationReceipt:
        self.verify_execution_policy(cwd)
        thread = self._codex.thread_resume(thread_id, cwd=str(cwd.resolve()))
        return RuntimeOperationReceipt(
            operation_id=thread.id,
            payload={"thread_id": thread.id, "cwd": str(cwd.resolve()), "resumed": True},
            binding=ThreadBinding(thread_id=thread.id, bound_at=utc_now()),
        )

    def interrupt(self, *, thread_id: str, turn_id: str) -> RuntimeOperationReceipt:
        # 실행 중인 turn을 중단하기 위해 저장 thread를 다시 resume하지 않는다.
        response = self._raw("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
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

    def close(self) -> None:
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
            payload={"thread_id": thread_id},
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
        del prompt, model, effort, output_schema
        self.verify_execution_policy(cwd)
        self.turn_calls += 1
        thread = self.threads[thread_id]
        turn_id = new_id("runtime_turn")
        thread.turn_id = turn_id
        thread.terminal_status = None
        thread.final_response = None
        binding = ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now())
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id},
            binding=binding,
        )

    def complete(self, thread_id: str, *, response: str = "완료") -> None:
        self.threads[thread_id].terminal_status = "completed"
        self.threads[thread_id].final_response = response

    def fail(self, thread_id: str, *, response: str = "실패") -> None:
        self.threads[thread_id].terminal_status = "failed"
        self.threads[thread_id].final_response = response

    def read(self, *, thread_id: str) -> RuntimeObservation:
        self.read_calls += 1
        thread = self.threads[thread_id]
        return RuntimeObservation(
            thread_id=thread_id,
            turn_id=thread.turn_id,
            active=thread.turn_id is not None and thread.terminal_status is None,
            terminal_status=thread.terminal_status,
            final_response=thread.final_response,
            payload={"thread_id": thread_id, "turn_id": thread.turn_id},
        )

    def read_stored(self, *, thread_id: str) -> RuntimeObservation:
        return self.read(thread_id=thread_id)

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

    def interrupt(self, *, thread_id: str, turn_id: str) -> RuntimeOperationReceipt:
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


class EngineDispatcher:
    """원장 우선순위에 따라 호출당 한 상태 단계만 전진시키는 실행기."""

    _SUCCESS = {"completed", "success", "succeeded"}
    _FAILED = {"failed", "error", "systemError", "system_error"}

    def __init__(
        self,
        service: EngineService,
        runtime: CodexRuntimePort,
        *,
        fault_hook: Callable[[str], None] | None = None,
        proposal_provider: Any | None = None,
    ) -> None:
        self.service = service
        self.runtime = runtime
        self.fault_hook = fault_hook
        self.proposal_provider = proposal_provider

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
                "ORDER BY a.created_at, a.rowid LIMIT 1",
                (project_id,),
            ).fetchone()
        if prepared:
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
                self._dispatch_reserved(active_attempt["id"], validation_id=validation_id)
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
            if proposal is None and self.proposal_provider is not None:
                from .operations import ExternalOperationUnknown
                try:
                    self._verify_policy(Path(project["root"]))
                    self.service.reobserve_project(project_id)
                    prepared_spec = self.proposal_provider.prepare_task(
                        project_id=project_id, task_id=ready["id"], inventory=self.runtime.list_models(),
                    )
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
            attempt = self.service.reserve_attempt(task_id=materialized["id"])
            self._dispatch_reserved(attempt.attempt_id)
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
                "AND latest.failure_class IS NOT NULL "
                "ORDER BY latest.attempt_no DESC, latest.rowid DESC LIMIT 1"
                ") WHERE t.plan_revision_id = ? AND t.status IN ('failed','blocked') "
                "ORDER BY t.position LIMIT 1",
                (project["active_plan_revision_id"],),
            ).fetchone()
        if incomplete:
            if failed is not None:
                failure_class = FailureClass(failed["failure_class"])
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
                for row in connection.execute(
                    "SELECT validation_id FROM validation_results WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
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
        return self._task_evidence_catalog(
            row["task_id"],
            spec.definition_digest,
            required_evidence_kinds=step.required_evidence_kinds,
        )

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

    def _dispatch_reserved(self, attempt_id: str, *, validation_id: str | None = None) -> None:
        row, spec = self._attempt_context(attempt_id)
        cwd = Path(row["root"])
        role, _prompt, _output_schema = self._role_for_attempt(
            row, spec, validation_id=validation_id
        )
        self._verify_policy(cwd)
        inventory = self.runtime.list_models()
        if inventory.inventory_digest != role.inventory_digest:
            raise RuntimePolicyError("MODEL_INVENTORY_CHANGED: materialization 이후 model/list가 변경됐습니다.")
        if not inventory.supports(role.model, role.effort):
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE")
        attempt_key = f"{spec.definition.idempotency_key}:attempt:{row['attempt_no']}:{row['kind']}"
        request = {
            "cwd": str(cwd.resolve()),
            "task_id": row["task_id"],
            "model": role.model,
            "attempt_kind": row["kind"],
            "validation_id": validation_id,
        }
        if row["kind"] == AttemptKind.VALIDATION.value:
            semantic_validation_id = validation_id or self._validation_id_from_attempt(attempt_id)
            request["semantic_evidence_ids"] = list(
                self._semantic_evidence_catalog(row, spec, semantic_validation_id)
            )
        self._hit("before_thread_intent")
        create_intent = self.service.prepare_runtime_intent(
            attempt_id=attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key=f"{attempt_key}:thread",
            request=request,
        )
        self._hit("after_thread_intent")
        thread_receipt = self.runtime.create_thread(
            cwd=cwd,
            title=f"FlowMarshal {row['task_ref']} {row['kind']}",
            model=role.model,
            developer_instructions=(
                "활성 PlanContract가 지정한 역할 하나만 수행한다. 다음 Task를 선택하거나 "
                "FlowMarshal 원장의 권위 상태를 직접 바꾸지 않는다."
            ),
        )
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
    ) -> None:
        # 전송 직전에 다시 읽는다. 호출자가 임의 본문으로 대체할 인자는 두지 않는다.
        role, prompt, output_schema = self._role_for_attempt(row, spec, validation_id=validation_id)
        if resumed:
            prompt = "이전 turn이 중단되었습니다. 같은 TaskContract 범위에서 재개하세요.\n" + prompt
        request = {
            "thread_id": thread_id,
            "prompt_digest": sha256_digest(prompt),
            "model": role.model,
            "effort": role.effort,
            "validation_id": validation_id,
        }
        self._hit("before_turn_intent")
        turn_intent = self.service.prepare_runtime_intent(
            attempt_id=row["id"],
            kind=RuntimeIntentKind.START_TURN,
            idempotency_key=f"{attempt_key}:turn",
            request=request,
        )
        self._hit("after_turn_intent")
        turn_receipt = self.runtime.start_turn(
            thread_id=thread_id,
            cwd=Path(row["root"]),
            prompt=prompt,
            model=role.model,
            effort=role.effort,
            output_schema=output_schema,
        )
        self._hit("after_turn_effect")
        self.service.record_runtime_receipt(
            intent_id=turn_intent.intent_id,
            provider_operation_id=turn_receipt.operation_id,
            response=turn_receipt.payload,
            binding=turn_receipt.binding,
        )
        self._hit("after_turn_receipt")

    def _resume_bound_attempt(self, row: Any, spec: TaskExecutionSpecRevision) -> None:
        binding = ThreadBinding.model_validate_json(row["binding_json"])
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
        if inventory.inventory_digest != role.inventory_digest or not inventory.supports(
            role.model, role.effort
        ):
            raise RuntimePolicyError("MODEL_BINDING_UNAVAILABLE_AFTER_RESUME")
        attempt_key = f"{spec.definition.idempotency_key}:attempt:{row['attempt_no']}:{row['kind']}"
        resume_count = 0
        with self.service.ledger.read() as connection:
            resume_count = connection.execute(
                "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id = ? "
                "AND kind = 'resume_turn' AND status = 'received'",
                (row["id"],),
            ).fetchone()[0]
        if resume_count:
            raise EngineServiceError("저장된 Attempt는 이미 한 번 재개됐으며 다시 자동 재개하지 않습니다.")
        resume_intent = self.service.prepare_runtime_intent(
            attempt_id=row["id"],
            kind=RuntimeIntentKind.RESUME_TURN,
            idempotency_key=f"{attempt_key}:resume",
            request={"thread_id": binding.thread_id, "cwd": str(cwd.resolve())},
        )
        receipt = self.runtime.resume(thread_id=binding.thread_id, cwd=cwd)
        self.service.record_runtime_receipt(
            intent_id=resume_intent.intent_id,
            provider_operation_id=f"resume:{receipt.operation_id}:{row['id']}",
            response=receipt.payload,
            binding=receipt.binding,
        )
        self._start_turn(
            row=row,
            spec=spec,
            thread_id=binding.thread_id,
            attempt_key=f"{attempt_key}:resumed",
            validation_id=validation_id,
            resumed=True,
        )

    def _observe_as_outcome(self, attempt_id: str) -> RunOnceOutcome:
        row, spec = self._attempt_context(attempt_id)
        if row["binding_json"] is None:
            raise EngineServiceError("Attempt runtime binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        if binding.turn_id is None:
            observation = self.runtime.read(thread_id=binding.thread_id)
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
        observation = self.runtime.read(thread_id=binding.thread_id)
        if observation.active:
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                detail="기존 Attempt가 아직 실행 중입니다.",
            )
        terminal = observation.terminal_status
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
        if terminal in self._FAILED:
            self.service.finish_attempt(
                attempt_id=attempt_id,
                succeeded=False,
                failure_class=FailureClass.IMPLEMENTATION,
                detail=observation.final_response or f"terminal_status={terminal}",
            )
            return RunOnceOutcome(
                action=RunOnceAction.OBSERVED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
                detail=f"Attempt 실패를 관측했습니다: {terminal}",
            )
        try:
            self._resume_bound_attempt(row, spec)
        except EngineServiceError as error:
            self.service.finish_attempt(
                attempt_id=attempt_id,
                succeeded=False,
                failure_class=FailureClass.ENVIRONMENT,
                detail=str(error),
            )
            return RunOnceOutcome(
                action=RunOnceAction.BLOCKED,
                project_id=row["project_id"],
                task_id=row["task_id"],
                attempt_id=attempt_id,
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

    def _advance_validation(self, task: Any) -> RunOnceOutcome:
        with self.service.ledger.read() as connection:
            spec_row = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE task_id = ? AND is_current = 1",
                (task["id"],),
            ).fetchone()
            rows = connection.execute(
                "SELECT validation_id, status, payload_json FROM validation_results "
                "WHERE task_id = ? ORDER BY evaluated_at, rowid",
                (task["id"],),
            ).fetchall()
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
                self._dispatch_reserved(attempt.attempt_id, validation_id=step.validation_id)
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
                "ORDER BY evaluated_at, rowid",
                (plan.plan_revision_id,),
            ).fetchall()
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
                    if result.goal_validation_binding_digest is not None and (not retry_pending and (
                        current_binding is None
                        or current_binding.binding_digest != result.goal_validation_binding_digest
                    )):
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
                                fault_hook=self.fault_hook,
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
                        )
                    return self._record_goal_verdict(project_id, plan, latest)
                if recorded["status"] == ValidationStatus.PASS.value:
                    continue
            if contract.evidence_mode == "independent":
                from .validation_execution import advance_independent_goal_test
                return advance_independent_goal_test(
                    self.service, self.runtime, project_id, plan, contract,
                    supplied_step=goal_validation_step, provider=self.proposal_provider,
                    fault_hook=self.fault_hook,
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
        observation = self.runtime.read(thread_id=binding.thread_id)
        if row["status"] in {"reserved", "starting", "running"}:
            self._observe_as_outcome(attempt_id)
        return observation

    def resume_attempt(self, attempt_id: str) -> RuntimeOperationReceipt:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT a.binding_json, p.root FROM attempts a JOIN projects p ON p.id = a.project_id "
                "WHERE a.id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None or row["binding_json"] is None:
            raise EngineServiceError("resume할 기존 binding이 없습니다.")
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        observation = self.runtime.read(thread_id=binding.thread_id)
        if observation.active:
            raise EngineServiceError("thread/read 결과가 active이므로 resume하지 않습니다.")
        receipt = self.runtime.resume(thread_id=binding.thread_id, cwd=Path(row["root"]))
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
        return self.runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
