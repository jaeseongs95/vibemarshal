"""동결 R3.1 runner의 provider 경계에 Engine 예산 원장을 합성한다.

이 모듈은 비동결 benchmark harness만 import한다. ``flowmarshal.engine``은 이
bridge나 legacy planning 모듈을 import하지 않는다.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .canonical import sha256_digest
from .engine.budget import BudgetManager
from .engine.domain import BudgetStage, utc_now
from .engine.evaluation_budget import EvaluationPolicies
from .engine.operation_trace import OperationTrace
from .engine.runtime import CodexProjectBinding
from .engine.roles import RoleCallReceipt, RoleCallRequest
from .planning.r31_domain import PlanningRole
from .planning.r31_models import PolicyVerifiedCodex, _normalize_model_inventory


LEGACY_BUDGET_EVIDENCE_FORMAT = "flowmarshal-legacy-budget-evidence-v1"
LEGACY_SCHEMA_RECOVERY_ERROR = (
    "LEGACY_SCHEMA_RECOVERY_DISABLED: qualification 계약은 schema recovery 0회입니다."
)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
        os.path.abspath(str(right))
    )


def _trace_response(value: Any) -> dict[str, Any]:
    """SDK 객체를 trace에 안전하고 결정적으로 투영한다.

    원본 응답 본문이나 Python 객체 표현을 trace에 넣지 않는다. provider 효과와
    receipt 결속에 필요한 식별자·상태·본문 digest만 남긴다.
    """
    if value is None:
        return {"value": None}
    if isinstance(value, dict):
        return {"mapping_digest": sha256_digest(value)}
    dumped = value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else None
    if dumped is not None:
        return {"model_digest": sha256_digest(dumped)}
    status = getattr(getattr(value, "status", None), "value", getattr(value, "status", None))
    response: dict[str, Any] = {
        "type": type(value).__name__,
        "id": None if getattr(value, "id", None) is None else str(value.id),
        "status": None if status is None else str(status),
    }
    final_response = getattr(value, "final_response", None)
    if final_response is not None:
        response["final_response_digest"] = sha256_digest(final_response)
    return response


def _trace_error(error: BaseException) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


class _ProjectBoundPolicyVerifiedCodex:
    """동결 PolicyVerifiedCodex 검증을 유지하며 raw projectId만 보강한다."""

    def __init__(self, wrapped: Any, binding: CodexProjectBinding) -> None:
        self._wrapped = wrapped
        self._binding = binding
        self._codex = wrapped._codex
        self._evidence_sink = wrapped._evidence_sink
        self.creation_thread_id: str | None = None

    def verify_execution_policy(self, cwd: str | Path) -> Any:
        return self._wrapped.verify_execution_policy(cwd)

    def _raw_object(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        routed = copy.deepcopy(params)
        if method == "thread/start":
            routed["projectId"] = self._binding.project_id
            routed["ephemeral"] = False
        response = self._wrapped._raw_object(method, routed)
        if method == "thread/start":
            thread = response.get("thread")
            if (
                not isinstance(thread, dict)
                or not isinstance(thread.get("id"), str)
                or not thread["id"]
                or thread.get("projectId") != self._binding.project_id
                or thread.get("ephemeral") is not False
                or thread.get("turns") != []
                or routed.get("ephemeral") is not False
            ):
                raise ValueError(
                    "LEGACY_PROJECT_BINDING_MISMATCH: thread/start raw receipt의 "
                    "id/projectId/ephemeral/빈 turns 또는 저장형 요청 결속이 다릅니다."
                )
            self.creation_thread_id = thread["id"]
        return response

    def thread_start(self, **kwargs: Any) -> Any:
        routed = dict(kwargs)
        routed["ephemeral"] = False
        # 동결 메서드 자체가 permission/cwd/model과 raw receipt를 검증한다.
        thread = PolicyVerifiedCodex.thread_start(self, **routed)
        if self.creation_thread_id is None or str(thread.id) != self.creation_thread_id:
            raise ValueError(
                "LEGACY_PROJECT_BINDING_MISMATCH: 생성 receipt와 Thread handle ID가 다릅니다."
            )
        return thread


def _verify_project_binding(wrapped: Any, binding: CodexProjectBinding) -> dict[str, Any]:
    try:
        response = wrapped._raw_object("project/read", {"projectId": binding.project_id})
    except Exception as error:
        raise ValueError(
            "LEGACY_PROJECT_BINDING_MISMATCH: project/read에 실패했습니다."
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
        raise ValueError(
            "LEGACY_PROJECT_BINDING_MISMATCH: App Server project가 고정 계약과 다릅니다."
        )
    return response


def _verify_thread_project(wrapped: Any, thread_id: str, binding: CodexProjectBinding) -> None:
    try:
        response = wrapped._raw_object(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
    except Exception as error:
        raise ValueError(
            "LEGACY_PROJECT_BINDING_MISMATCH: turn 시작 전 thread/read에 실패했습니다."
        ) from error
    thread = response.get("thread")
    if (
        not isinstance(thread, dict)
        or thread.get("id") != thread_id
        or thread.get("projectId") != binding.project_id
    ):
        raise ValueError(
            "LEGACY_PROJECT_BINDING_MISMATCH: 기존 thread가 고정 프로젝트와 다릅니다."
        )


@dataclass(frozen=True)
class LegacyRoleScope:
    stage: BudgetStage
    allowed_roles: tuple[PlanningRole, ...]


_ACTIVE_SCOPE: ContextVar[LegacyRoleScope | None] = ContextVar(
    "flowmarshal_legacy_budget_scope", default=None
)


@contextmanager
def legacy_role_scope(scope: LegacyRoleScope) -> Iterator[None]:
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield
    finally:
        _ACTIVE_SCOPE.reset(token)


class ScopedLegacyAdapter:
    """기존 adapter method 호출 동안 권위 role/stage scope를 제공한다."""

    def __init__(self, wrapped: Any, method: str, scope: LegacyRoleScope) -> None:
        self._wrapped = wrapped
        self._method = method
        self._scope = scope

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._wrapped, name)
        if name != self._method:
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            with legacy_role_scope(self._scope):
                return value(*args, **kwargs)

        return call


def install_legacy_role_scopes(runtime: Any) -> None:
    """비동결 composition의 각 실제 역할 호출에 예산 stage를 결속한다."""

    def scope(stage: BudgetStage, *roles: PlanningRole) -> LegacyRoleScope:
        return LegacyRoleScope(stage=stage, allowed_roles=tuple(roles))

    runtime.mission_service._proposer = ScopedLegacyAdapter(
        runtime.mission_service._proposer,
        "propose",
        scope(BudgetStage.GOAL_NORMALIZATION, PlanningRole.PURPOSE_RESOLVER),
    )
    runtime.mission_service._reviewer = ScopedLegacyAdapter(
        runtime.mission_service._reviewer,
        "review",
        scope(BudgetStage.GOAL_REVIEW, PlanningRole.INTENT_REVIEWER),
    )
    runtime.requirement_service._extractor = ScopedLegacyAdapter(
        runtime.requirement_service._extractor,
        "extract",
        scope(BudgetStage.GOAL_NORMALIZATION, PlanningRole.PURPOSE_RESOLVER),
    )
    runtime.requirement_service._reviewer = ScopedLegacyAdapter(
        runtime.requirement_service._reviewer,
        "review",
        scope(BudgetStage.GOAL_REVIEW, PlanningRole.INTENT_REVIEWER),
    )
    pipeline = runtime.search_service
    pipeline._generator = ScopedLegacyAdapter(
        pipeline._generator,
        "generate",
        scope(BudgetStage.SKELETON_GENERATION, PlanningRole.CANDIDATE_GENERATOR),
    )
    pipeline._expander = ScopedLegacyAdapter(
        pipeline._expander,
        "expand",
        scope(BudgetStage.PLAN_EXPANSION, PlanningRole.CANDIDATE_GENERATOR),
    )
    pipeline._reviewer = ScopedLegacyAdapter(
        pipeline._reviewer,
        "review",
        scope(
            BudgetStage.PLAN_REVIEW,
            PlanningRole.HARD_GATE_REVIEWER,
            PlanningRole.CRITICAL_REVIEWER,
        ),
    )
    if pipeline._refiner is not None:
        pipeline._refiner = ScopedLegacyAdapter(
            pipeline._refiner,
            "refine",
            scope(BudgetStage.REPLAN, PlanningRole.CANDIDATE_GENERATOR),
        )
    if pipeline._walkthrough is not None:
        pipeline._walkthrough = ScopedLegacyAdapter(
            pipeline._walkthrough,
            "walkthrough",
            scope(BudgetStage.PLAN_REVIEW, PlanningRole.SCORER_SELECTOR),
        )


@dataclass
class LegacyBudgetJournal:
    cell_binding: dict[str, Any]
    records: list[dict[str, Any]] = field(default_factory=list)
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _trace_root: Path | None = field(default=None, init=False, repr=False)
    _traces: dict[str, OperationTrace] = field(default_factory=dict, init=False, repr=False)
    _trace_expected_operations: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.cell_binding = copy.deepcopy(self.cell_binding)

    @property
    def cell_binding_digest(self) -> str:
        return sha256_digest(self.cell_binding)

    def next_call_key(self, request_digest: str) -> str:
        with self._lock:
            self._counter += 1
            ordinal = self._counter
        return f"legacy:{self.cell_binding_digest[7:23]}:{ordinal}:{request_digest[7:23]}"

    def add(self, value: dict[str, Any]) -> None:
        with self._lock:
            self.records.append(copy.deepcopy(value))

    def configure_trace_root(self, root: Path) -> None:
        """호출별 append-only trace의 영속 위치를 한 번만 고정한다."""
        resolved = root.resolve()
        with self._lock:
            if self._trace_root is not None and self._trace_root != resolved:
                raise ValueError("LEGACY_OPERATION_TRACE_ROOT_MISMATCH")
            self._trace_root = resolved

    def create_operation_trace(
        self,
        *,
        provider_call_id: str,
        call_key: str,
        request: RoleCallRequest,
        stage: BudgetStage,
        expected_operations: tuple[str, ...],
    ) -> OperationTrace:
        with self._lock:
            if provider_call_id in self._traces:
                raise ValueError("LEGACY_OPERATION_TRACE_DUPLICATE_CALL")
            if self._trace_root is None:
                raise ValueError("LEGACY_OPERATION_TRACE_ROOT_MISSING")
            trace = OperationTrace(
                context={
                    "legacy_budget_evidence_format": LEGACY_BUDGET_EVIDENCE_FORMAT,
                    "cell_binding_digest": self.cell_binding_digest,
                    "provider_call_id": provider_call_id,
                    "call_key": call_key,
                    "request_digest": request.request_digest,
                    "role": request.role,
                    "stage": stage.value,
                },
                path=self._trace_root / f"{provider_call_id}.operation-trace.jsonl",
                expected_operations=expected_operations,
            )
            self._traces[provider_call_id] = trace
            self._trace_expected_operations[provider_call_id] = expected_operations
            return trace

    def set_trace_expected_operations(
        self, provider_call_id: str, expected_operations: tuple[str, ...]
    ) -> None:
        with self._lock:
            if provider_call_id not in self._traces:
                raise ValueError("LEGACY_OPERATION_TRACE_MISSING_CALL")
            self._trace_expected_operations[provider_call_id] = expected_operations

    def operation_trace_evidence(self) -> list[dict[str, Any]]:
        """현재까지 파일에 기록된 trace를 원장 evidence에 투영한다.

        timeout 또는 parent hard-kill 직전에는 seal하지 않는다. 따라서 provider
        효과가 미확정이면 ``pending``과 ``sealed=false``가 그대로 남는다.
        """
        with self._lock:
            entries = [
                (provider_call_id, trace, self._trace_expected_operations[provider_call_id])
                for provider_call_id, trace in self._traces.items()
            ]
        result = []
        for provider_call_id, trace, expected_operations in entries:
            body = trace.snapshot(expected_operations=expected_operations)
            result.append({
                "provider_call_id": provider_call_id,
                "trace_path": None if trace.path is None else str(trace.path),
                "trace": body,
                "trace_digest": sha256_digest(body),
            })
        return result


class BudgetedPolicyVerifiedCodex:
    """PolicyVerifiedCodex와 같은 표면을 제공하는 호출 전 예산 proxy."""

    def __init__(
        self,
        wrapped: Any,
        manager: BudgetManager,
        *,
        project_id: str,
        goal_id: str,
        goal_digest: str,
        policies: EvaluationPolicies,
        cell_binding: dict[str, Any],
        role_instructions: dict[PlanningRole, str],
        journal: LegacyBudgetJournal,
        expected_inventory_digest: str,
    ) -> None:
        self._wrapped = wrapped
        self._manager = manager
        self._project_id = project_id
        self._goal_id = goal_id
        self._goal_digest = goal_digest
        self._policies = policies
        self._cell_binding = copy.deepcopy(cell_binding)
        self._journal = journal
        self._journal.configure_trace_root(
            self._manager.service.ledger.artifact_root / "legacy-operation-traces"
        )
        self._expected_inventory_digest = expected_inventory_digest
        self._inventory_digest: str | None = None
        self._creation_project_proofs: set[str] = set()
        by_digest: dict[str, PlanningRole] = {}
        for role, instructions in role_instructions.items():
            digest = sha256_digest(instructions)
            if digest in by_digest:
                raise ValueError("LEGACY_ROLE_INSTRUCTION_DIGEST_COLLISION")
            by_digest[digest] = role
        self._roles_by_instruction_digest = by_digest

    def _start_thread(self, kwargs: dict[str, Any]) -> Any:
        binding = self._policies.codex_project
        if binding is None:
            return self._wrapped.thread_start(**kwargs)
        routed = _ProjectBoundPolicyVerifiedCodex(self._wrapped, binding)
        thread = routed.thread_start(**kwargs)
        self._creation_project_proofs.add(str(thread.id))
        return thread

    def _verify_thread_project(self, thread_id: str) -> None:
        binding = self._policies.codex_project
        if binding is not None:
            if thread_id in self._creation_project_proofs:
                # 같은 연결에서 직접 생성한 exact raw receipt proof는 첫 turn에만 쓴다.
                self._creation_project_proofs.remove(thread_id)
                return
            _verify_thread_project(self._wrapped, thread_id, binding)

    def models(self, *, include_hidden: bool = False) -> Any:
        response = self._wrapped.models(include_hidden=include_hidden)
        self._inventory_digest = sha256_digest(_normalize_model_inventory(response))
        if self._inventory_digest != self._expected_inventory_digest:
            raise ValueError("LEGACY_BUDGET_INVENTORY_DIGEST_MISMATCH")
        return response

    def verify_execution_policy(self, cwd: Any) -> Any:
        return self._wrapped.verify_execution_policy(cwd)

    def thread_start(self, **kwargs: Any) -> Any:
        scope = _ACTIVE_SCOPE.get()
        if scope is None:
            raise ValueError("LEGACY_BUDGET_SCOPE_REQUIRED")
        instructions = kwargs.get("base_instructions")
        role = self._roles_by_instruction_digest.get(sha256_digest(instructions))
        if role is None or role not in scope.allowed_roles:
            raise ValueError("LEGACY_BUDGET_ROLE_BINDING_MISMATCH")
        if self._inventory_digest is None:
            raise ValueError("LEGACY_BUDGET_INVENTORY_BINDING_MISSING")
        if kwargs.get("ephemeral") is not True:
            raise ValueError("LEGACY_EPHEMERAL_BASELINE_CHANGED")
        project_binding = self._policies.codex_project
        expected_effective_ephemeral = project_binding is None
        if self._cell_binding.get("ephemeral_threads") is not expected_effective_ephemeral:
            raise ValueError("LEGACY_PROJECT_THREAD_PERSISTENCE_BINDING_MISMATCH")
        expected_project = (
            None
            if project_binding is None
            else project_binding.model_dump(mode="json", exclude_none=True)
        )
        if self._cell_binding.get("codex_project") != expected_project:
            raise ValueError("LEGACY_PROJECT_BINDING_MISMATCH")
        if project_binding is not None:
            _verify_project_binding(self._wrapped, project_binding)
        return _LazyBudgetedLegacyThread(
            owner=self,
            thread_kwargs=copy.deepcopy(kwargs),
            role=role,
            stage=scope.stage,
            inventory_digest=self._inventory_digest,
        )

    def close(self) -> None:
        self._wrapped.close()


class _LazyBudgetedLegacyThread:
    def __init__(
        self,
        *,
        owner: BudgetedPolicyVerifiedCodex,
        thread_kwargs: dict[str, Any],
        role: PlanningRole,
        stage: BudgetStage,
        inventory_digest: str,
    ) -> None:
        self._owner = owner
        self._thread_kwargs = thread_kwargs
        self._role = role
        self._stage = stage
        self._inventory_digest = inventory_digest
        self._thread: Any | None = None
        self._provider_call_id: str | None = None
        self._run_count = 0

    @property
    def id(self) -> str:
        if self._thread is None:
            return "unobserved_thread_for_" + (self._provider_call_id or "unreserved_call")
        return str(self._thread.id)

    def run(self, input: Any, **kwargs: Any) -> Any:
        if self._run_count:
            raise RuntimeError(LEGACY_SCHEMA_RECOVERY_ERROR)
        self._run_count += 1
        owner = self._owner
        effort = getattr(kwargs.get("effort"), "value", kwargs.get("effort"))
        model = str(kwargs.get("model") or self._thread_kwargs.get("model") or "")
        thread_model = str(self._thread_kwargs.get("model") or "")
        approval_mode = getattr(self._thread_kwargs.get("approval_mode"), "value", None)
        thread_sandbox = getattr(self._thread_kwargs.get("sandbox"), "value", None)
        run_sandbox = getattr(kwargs.get("sandbox"), "value", None)
        if (
            not model
            or model != thread_model
            or not effort
            or approval_mode != "deny_all"
            or thread_sandbox != "full-access"
            or run_sandbox != "full-access"
        ):
            raise ValueError("LEGACY_ROLE_CALL_POLICY_BINDING_MISMATCH")
        output_schema = copy.deepcopy(kwargs.get("output_schema"))
        if not isinstance(output_schema, dict):
            raise ValueError("LEGACY_OUTPUT_SCHEMA_BINDING_MISSING")
        timeout = owner._policies.role_timeouts.timeout_for(self._role.value)
        legacy_payload = {
            "legacy_turn_input": input,
            "legacy_cell_binding_digest": owner._journal.cell_binding_digest,
            "legacy_cell_binding": copy.deepcopy(owner._cell_binding),
            "legacy_stage": self._stage.value,
            "legacy_ephemeral": True,
            "legacy_thread_policy": {
                "approval_mode": approval_mode,
                "thread_sandbox": thread_sandbox,
                "run_sandbox": run_sandbox,
                "thread_model": thread_model,
            },
        }
        if owner._policies.codex_project is not None:
            legacy_payload.update(
                legacy_requested_ephemeral=True,
                legacy_effective_ephemeral=False,
                codex_project=owner._policies.codex_project.model_dump(
                    mode="json", exclude_none=True
                ),
            )
        request = RoleCallRequest(
            role=self._role.value,
            instructions=str(self._thread_kwargs.get("base_instructions") or ""),
            payload=legacy_payload,
            output_schema=output_schema,
            model=model,
            effort=str(effort or ""),
            inventory_digest=self._inventory_digest,
            cwd=str(self._thread_kwargs.get("cwd") or ""),
            timeout_seconds=timeout,
            timeout_policy_digest=owner._policies.role_timeouts.policy_digest,
        )
        call_key = owner._journal.next_call_key(request.request_digest)
        provider_call_id = owner._manager.reserve(
            project_id=owner._project_id,
            goal_id=owner._goal_id,
            goal_digest=owner._goal_digest,
            call_key=call_key,
            role=request.role,
            request=request.model_dump(mode="json"),
            stage=self._stage,
        )
        self._provider_call_id = provider_call_id
        started = time.monotonic()
        deadline = started + timeout
        expected_operations = ("create", "start", "sdk_wait") + (
            ("read",) if owner._policies.codex_project is not None else ()
        )
        trace = owner._journal.create_operation_trace(
            provider_call_id=provider_call_id,
            call_key=call_key,
            request=request,
            stage=self._stage,
            expected_operations=expected_operations,
        )
        base_record = {
            "provider_call_id": provider_call_id,
            "call_key": call_key,
            "request_digest": request.request_digest,
            "role": request.role,
            "stage": self._stage.value,
        }
        try:
            self._thread = self._bounded_rpc(
                lambda: owner._start_thread(self._thread_kwargs),
                deadline=deadline,
                operation="thread_start",
                trace=trace,
                kind="create",
                request={
                    "request_digest": request.request_digest,
                    "model": model,
                    "approval_mode": approval_mode,
                    "sandbox": thread_sandbox,
                    "requested_ephemeral": True,
                },
                call_key=call_key,
                provider_call_id=provider_call_id,
            )
            if owner._policies.codex_project is not None:
                observation_policy = owner._policies.role_timeouts.observation_policy
                rpc_timeout = (
                    5.0
                    if observation_policy is None
                    else observation_policy.rpc_timeout_seconds
                )
                self._bounded_rpc(
                    lambda: owner._verify_thread_project(str(self._thread.id)),
                    deadline=min(deadline, time.monotonic() + rpc_timeout),
                    operation="thread_project_read",
                    trace=trace,
                    kind="read",
                    request={
                        "thread_id": str(self._thread.id),
                        "project_id": owner._policies.codex_project.project_id,
                    },
                    call_key=call_key,
                    provider_call_id=provider_call_id,
                    thread_id=str(self._thread.id),
                )
            handle = self._bounded_rpc(
                lambda: self._thread.turn(input, **kwargs),
                deadline=deadline,
                operation="turn_start",
                trace=trace,
                kind="start",
                request={
                    "request_digest": request.request_digest,
                    "input_digest": sha256_digest(input),
                    "output_schema_digest": sha256_digest(output_schema),
                    "model": model,
                    "effort": str(effort),
                },
                call_key=call_key,
                provider_call_id=provider_call_id,
                thread_id=str(self._thread.id),
            )
        except BaseException as error:
            owner._journal.add(base_record | {
                "outcome": "effect_unknown",
                "error": f"{type(error).__name__}: {error}",
            })
            raise

        box: dict[str, Any] = {}
        completed = threading.Event()
        run_token = trace.begin(
            "sdk_wait",
            {
                "request_digest": request.request_digest,
                "handle_id": str(handle.id),
                "operation": "handle.run",
            },
            call_id=call_key,
            provider_call_id=provider_call_id,
            thread_id=str(self._thread.id),
            turn_id=str(handle.id),
            deadline_seconds=max(0.0, deadline - time.monotonic()),
            category="wait",
        )

        def collect() -> None:
            try:
                box["result"] = handle.run()
                try:
                    trace.finish(run_token, response=_trace_response(box["result"]))
                except (RuntimeError, ValueError):
                    # 호출자 watchdog이 먼저 deadline 관측을 확정한 경우다.
                    pass
            except BaseException as error:
                box["error"] = error
                try:
                    trace.finish(run_token, error=_trace_error(error))
                except (RuntimeError, ValueError):
                    pass
            finally:
                completed.set()

        worker = threading.Thread(target=collect, name="legacy-budget-turn", daemon=True)
        worker.start()
        remaining = max(0.0, timeout - (time.monotonic() - started))
        if not completed.wait(remaining):
            try:
                trace.finish(run_token, error=TimeoutError("LEGACY_ROLE_TIMEOUT"))
            except (RuntimeError, ValueError):
                # worker가 deadline을 초과해 먼저 종료를 관측한 경우도 허용한다.
                pass
            interrupt_request = {
                "thread_id": str(self._thread.id),
                "turn_id": str(handle.id),
                "reason": "role_timeout",
                "timeout_seconds": timeout,
                "timeout_policy_digest": owner._policies.role_timeouts.policy_digest,
            }
            interrupt_request_digest = sha256_digest(interrupt_request)
            interrupt_receipt_digest = None
            interrupt_error = None
            try:
                interrupt_receipt = self._bounded_rpc(
                    handle.interrupt,
                    deadline=time.monotonic() + timeout,
                    operation="interrupt",
                    trace=trace,
                    kind="interrupt",
                    request=interrupt_request,
                    call_key=call_key,
                    provider_call_id=provider_call_id,
                    thread_id=str(self._thread.id),
                    turn_id=str(handle.id),
                )
                interrupt_receipt_digest = sha256_digest(
                    interrupt_receipt.model_dump(mode="json", by_alias=True)
                    if hasattr(interrupt_receipt, "model_dump") else interrupt_receipt
                )
            except BaseException as error:
                interrupt_error = f"{type(error).__name__}: {error}"
            observed_after_interrupt = box.get("result") if completed.is_set() else None
            terminal_status = (
                getattr(getattr(observed_after_interrupt, "status", None), "value", None)
                if observed_after_interrupt is not None else None
            )
            trace_expected_operations = expected_operations + ("interrupt",)
            owner._journal.set_trace_expected_operations(
                provider_call_id, trace_expected_operations
            )
            receipt = self._receipt(
                request=request,
                call_key=call_key,
                handle=handle,
                result=observed_after_interrupt,
                started=started,
                status="timed_out",
                error="role turn timeout",
                interrupt_request_digest=interrupt_request_digest,
                interrupt_receipt_digest=interrupt_receipt_digest,
                terminal_status=terminal_status,
                operation_trace=(
                    trace.seal(expected_operations=trace_expected_operations)
                    if terminal_status in {"completed", "interrupted", "failed"}
                    else trace.snapshot(expected_operations=trace_expected_operations)
                ),
            )
            owner._manager.settle(provider_call_id, receipt)
            owner._journal.add(base_record | {
                "outcome": (
                    "timeout_terminal_observed"
                    if terminal_status in {"completed", "interrupted", "failed"}
                    else "timeout_unknown"
                ),
                "receipt_digest": sha256_digest(receipt),
                "interrupt_request_digest": interrupt_request_digest,
                "interrupt_receipt_digest": interrupt_receipt_digest,
                "interrupt_error": interrupt_error,
            })
            raise TimeoutError("LEGACY_ROLE_TIMEOUT")
        if "error" in box:
            error = box["error"]
            owner._journal.add(base_record | {
                "outcome": "effect_unknown",
                "error": f"{type(error).__name__}: {error}",
                "turn_id": str(handle.id),
            })
            raise error
        result = box["result"]
        status_value = getattr(getattr(result, "status", None), "value", None)
        terminal = status_value in {"completed", "interrupted", "failed"}
        status = "succeeded" if status_value == "completed" else "failed"
        receipt = self._receipt(
            request=request,
            call_key=call_key,
            handle=handle,
            result=result,
            started=started,
            status=status,
            error=None if status == "succeeded" else f"terminal status={status_value}",
            operation_trace=trace.seal(),
        )
        owner._manager.settle(provider_call_id, receipt)
        owner._journal.add(base_record | {
            "outcome": "settled" if receipt.usage_available and terminal else "usage_unknown",
            "receipt_digest": sha256_digest(receipt),
            "turn_id": str(handle.id),
        })
        return result

    @staticmethod
    def _bounded_rpc(
        operation_call: Any,
        *,
        deadline: float,
        operation: str,
        trace: OperationTrace,
        kind: str,
        request: dict[str, Any],
        call_key: str,
        provider_call_id: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> Any:
        """SDK의 동기 RPC를 정책 timeout 안에서만 기다린다.

        daemon worker가 늦게 반환해도 호출자는 provider 효과를 미확인 상태로
        보존한다. 강제 프로세스 종료는 parent hard-timeout 경계가 담당한다.
        """
        box: dict[str, Any] = {}
        completed = threading.Event()
        token = trace.begin(
            kind,
            request,
            call_id=call_key,
            provider_call_id=provider_call_id,
            thread_id=thread_id,
            turn_id=turn_id,
            deadline_seconds=max(0.0, deadline - time.monotonic()),
        )

        def invoke() -> None:
            try:
                box["result"] = operation_call()
                resolved_thread_id = thread_id
                resolved_turn_id = turn_id
                if kind == "create" and getattr(box["result"], "id", None) is not None:
                    resolved_thread_id = str(box["result"].id)
                elif kind == "start" and getattr(box["result"], "id", None) is not None:
                    resolved_turn_id = str(box["result"].id)
                try:
                    trace.finish(
                        token,
                        response=_trace_response(box["result"]),
                        thread_id=resolved_thread_id,
                        turn_id=resolved_turn_id,
                    )
                except (RuntimeError, ValueError):
                    pass
            except BaseException as error:
                box["error"] = error
                try:
                    trace.finish(token, error=_trace_error(error))
                except (RuntimeError, ValueError):
                    pass
            finally:
                completed.set()

        threading.Thread(
            target=invoke,
            name=f"legacy-budget-{operation}",
            daemon=True,
        ).start()
        remaining = max(0.0, deadline - time.monotonic())
        if not completed.wait(remaining):
            error = TimeoutError(f"LEGACY_{operation.upper()}_TIMEOUT")
            try:
                trace.finish(token, error=error)
            except (RuntimeError, ValueError):
                pass
            raise error
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _receipt(
        self,
        *,
        request: RoleCallRequest,
        call_key: str,
        handle: Any,
        result: Any | None,
        started: float,
        status: str,
        error: str | None,
        interrupt_request_digest: str | None = None,
        interrupt_receipt_digest: str | None = None,
        terminal_status: str | None = None,
        operation_trace: dict[str, Any] | None = None,
    ) -> RoleCallReceipt:
        usage = None if result is None else getattr(getattr(result, "usage", None), "last", None)
        fields = (
            getattr(usage, "input_tokens", None),
            getattr(usage, "cached_input_tokens", None),
            getattr(usage, "output_tokens", None),
            getattr(usage, "reasoning_output_tokens", None),
        )
        usage_available = all(type(value) is int and value >= 0 for value in fields)
        turn_id = str(handle.id)
        result_id = None if result is None else str(getattr(result, "id", ""))
        if result is not None and result_id != turn_id:
            usage_available = False
        terminal_digest = None
        if terminal_status is not None and result is not None:
            terminal_digest = sha256_digest({
                "thread_id": str(self._thread.id),
                "turn_id": result_id,
                "status": terminal_status,
                "duration_ms": getattr(result, "duration_ms", None),
            })
        return RoleCallReceipt(
            call_id=call_key,
            role=request.role,
            status=status,
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id=str(self._thread.id),
            turn_ids=(turn_id,),
            input_digest=request.request_digest,
            output_digest=(
                None
                if result is None or getattr(result, "final_response", None) is None
                else sha256_digest(result.final_response)
            ),
            output_schema_digest=sha256_digest(request.output_schema),
            timeout_policy_digest=request.timeout_policy_digest,
            interrupt_request_digest=interrupt_request_digest,
            interrupt_receipt_digest=interrupt_receipt_digest,
            terminal_observation_digest=terminal_digest,
            terminal_status_after_interrupt=terminal_status,
            operation_trace=operation_trace,
            operation_trace_digest=(
                None if operation_trace is None else sha256_digest(operation_trace)
            ),
            input_tokens=fields[0] if usage_available else None,
            cached_input_tokens=fields[1] if usage_available else None,
            output_tokens=fields[2] if usage_available else None,
            reasoning_tokens=fields[3] if usage_available else None,
            usage_available=usage_available,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            schema_recovery_attempts=0,
            error_summary=error,
            recorded_at=utc_now(),
        )


def build_legacy_budget_evidence(
    manager: BudgetManager,
    journal: LegacyBudgetJournal,
    *,
    project_id: str,
    goal_id: str,
    legacy_receipts: list[Any],
) -> dict[str, Any]:
    with manager.service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT * FROM provider_calls WHERE project_id=? AND goal_id=? ORDER BY created_at,id",
            (project_id, goal_id),
        ).fetchall()
    calls = [{
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
    budget_turn_ids = {
        turn_id
        for call in calls
        if call["receipt"] is not None
        for turn_id in call["receipt"].get("turn_ids", ())
    }
    legacy_turn_ids = {
        str(turn_id)
        for receipt in legacy_receipts
        for turn_id in getattr(receipt, "turn_ids", ())
    }
    status = manager.status(project_id, goal_id=goal_id)
    operation_traces = journal.operation_trace_evidence()
    body = {
        "format": LEGACY_BUDGET_EVIDENCE_FORMAT,
        "cell_binding": copy.deepcopy(journal.cell_binding),
        "cell_binding_digest": journal.cell_binding_digest,
        "journal": copy.deepcopy(journal.records),
        "calls": calls,
        "legacy_turn_ids": sorted(legacy_turn_ids),
        "budget_turn_ids": sorted(budget_turn_ids),
        "turn_coverage_complete": legacy_turn_ids == budget_turn_ids,
        "budget_status": status.model_dump(mode="json"),
        "history_valid": manager.service.ledger.verify_history(project_id),
        "operation_traces": operation_traces,
        "operation_traces_digest": sha256_digest(operation_traces),
    }
    return body | {"evidence_digest": sha256_digest(body)}
