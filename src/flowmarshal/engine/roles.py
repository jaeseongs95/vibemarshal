from __future__ import annotations

from contextlib import nullcontext
from .capabilities import role_execution_scope

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import Field, model_serializer, model_validator

from ..canonical import canonical_json, sha256_digest
from .domain import (
    PROVIDER_SUCCESS_TERMINAL_STATUSES,
    PROVIDER_TERMINAL_STATUSES,
    EngineModel,
    ParsedProviderUsage,
    USAGE_COMPONENT_INVALID,
    USAGE_CONTRACT_VERSION,
    new_id,
    parse_provider_usage,
    unavailable_provider_usage,
    utc_now,
)
from .runtime import (
    CodexRuntimePort,
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
)
from .role_execution import bind_active_role_timeout
from .runtime_observation import RoleObservationPolicy, bounded_observation_call


from .model_lock import (
    ModelInventory, OperationalBinding, ROLE_CAPABILITIES, bind_models, role_lock, verify_binding,
)


from .role_observations import RoleCallReceipt, RoleInputContractError, StructuredRoleError
from .operation_trace import OperationTrace, current_operation_trace_scope
from .model_observation import (
    PROVIDER_RAW_MODEL_OBSERVATION_SOURCE,
    authoritative_model_observation,
)


class RoleCallRequest(EngineModel):
    role: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=50_000)
    payload: dict[str, Any]
    output_schema: dict[str, Any]
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    inventory_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cwd: str = Field(min_length=1, max_length=2000)
    timeout_seconds: float = Field(default=900, gt=0, le=3600)
    timeout_policy_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    observation_policy: RoleObservationPolicy | None = None
    observation_policy_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    operational_binding: OperationalBinding | None = None

    @model_validator(mode="after")
    def audit_matches_request(self):
        if ((self.observation_policy is None) != (self.observation_policy_digest is None)
                or (self.observation_policy is not None
                    and self.observation_policy.policy_digest != self.observation_policy_digest)):
            raise ValueError("ROLE_OBSERVATION_POLICY_REQUEST_MISMATCH")
        if self.operational_binding is not None:
            if not set(ROLE_CAPABILITIES).issubset(x.name for x in self.operational_binding.lock.runtime_capabilities):
                raise ValueError("MODEL_LOCK_REQUIRED_CAPABILITY_MISSING")
            if self.inventory_digest != self.operational_binding.inventory_digest:
                raise ValueError("MODEL_LOCK_REQUEST_DIGEST_MISMATCH")
            verify_binding(self.operational_binding, self.operational_binding.inventory,
                           role=self.role, model=self.model, effort=self.effort)
        return self

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)

    @model_serializer(mode="wrap")
    def omit_absent_timeout_policy(self, handler):
        value = handler(self)
        if self.timeout_policy_digest is None:
            value.pop("timeout_policy_digest", None)
        if self.observation_policy is None:
            value.pop("observation_policy", None)
            value.pop("observation_policy_digest", None)
        return value


def make_role_request(*, inventory: ModelInventory | None = None, allowed_fallbacks=(), **kwargs) -> RoleCallRequest:
    kwargs = bind_active_role_timeout(kwargs["role"], kwargs)
    if kwargs.get("observation_policy") is not None:
        policy = RoleObservationPolicy.model_validate(kwargs["observation_policy"])
        if kwargs.get("observation_policy_digest", policy.policy_digest) != policy.policy_digest:
            raise ValueError("ROLE_OBSERVATION_POLICY_REQUEST_MISMATCH")
        kwargs["observation_policy"] = policy
        kwargs["observation_policy_digest"] = policy.policy_digest
    binding = None if inventory is None else bind_models(
        inventory, (role_lock(kwargs["role"], kwargs["model"], kwargs["effort"], allowed_fallbacks),),
        required_capabilities=ROLE_CAPABILITIES,
    )
    # request 자체에 완성된 strict schema를 보존해 canonical key 정렬 뒤에도
    # required 배열에 기록한 선언 순서로 transport schema를 재구성한다.
    kwargs["output_schema"] = strict_json_output_schema(kwargs["output_schema"])
    return RoleCallRequest(**kwargs, operational_binding=binding)


class RoleCallResult(EngineModel):
    payload: dict[str, Any]
    receipt: RoleCallReceipt


class StructuredRolePort(Protocol):
    def run(
        self,
        request: RoleCallRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RoleCallResult: ...


def strict_json_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        # Structured Outputs는 모든 속성을 명시적으로 요구한다. Pydantic의
        # default는 입력 생략용 annotation이며 특히 $ref 옆에서 provider가 거부한다.
        # 원본 schema와 Core의 실제 값 검증 제약은 변경하지 않는다.
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            properties = node.get("properties", {})
            if isinstance(properties, dict):
                # 최초 변환에서는 properties의 schema 선언 순서를 사용한다. 이미
                # strict인 schema는 전체 required 배열이 그 순서를 보존하므로,
                # canonical JSON이 object key를 정렬한 뒤 재로드해도 같은 순서를
                # 복원할 수 있다.
                declared_required = node.get("required")
                if (
                    isinstance(declared_required, list)
                    and all(isinstance(name, str) for name in declared_required)
                    and len(declared_required) == len(properties)
                    and len(set(declared_required)) == len(declared_required)
                    and set(declared_required) == set(properties)
                ):
                    property_names = list(declared_required)
                else:
                    property_names = list(properties)
                node["properties"] = {name: properties[name] for name in property_names}
                node["additionalProperties"] = False
                node["required"] = property_names
                for child in node["properties"].values():
                    visit(child)
        for key in ("$defs", "definitions"):
            definitions = node.get(key)
            if isinstance(definitions, dict):
                for child in definitions.values():
                    visit(child)
        for key in ("items", "prefixItems", "anyOf", "oneOf", "allOf", "not", "if", "then", "else"):
            if key in node:
                visit(node[key])
        dependent_schemas = node.get("dependentSchemas")
        if isinstance(dependent_schemas, dict):
            for child in dependent_schemas.values():
                visit(child)

    visit(normalized)
    return normalized


def _parsed_usage(
    payload: dict[str, Any],
    *,
    thread_id: str | None = None,
    turn_ids: tuple[str, ...] = (),
    observation_thread_id: str | None = None,
    observation_turn_id: str | None = None,
    turn_binding_proven: bool = False,
    empty_thread_creation_proven: bool = False,
    first_empty_turn_proven: bool = False,
) -> ParsedProviderUsage:
    usage = payload.get("usage")
    usage_scope = payload.get("usage_scope")
    single_turn_bound = (
        thread_id is not None
        and observation_thread_id == thread_id
        and len(turn_ids) == 1
        and observation_turn_id == turn_ids[0]
        and turn_binding_proven
    )
    if not isinstance(usage, dict) or not single_turn_bound:
        return unavailable_provider_usage("PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE")
    if usage_scope == "turn":
        source = usage
    elif (
        usage_scope == "thread"
        and empty_thread_creation_proven
        and first_empty_turn_proven
        and isinstance(usage.get("total"), dict)
    ):
        source = usage["total"]
    else:
        return unavailable_provider_usage("PROVIDER_USAGE_UNAVAILABLE_OR_UNATTRIBUTABLE")
    return parse_provider_usage(source)


def _usage(
    payload: dict[str, Any],
    **proof: Any,
) -> tuple[int | None, int | None, int | None, int | None, bool]:
    """기존 diagnostic 호출 계약을 유지한다. 신규 receipt는 `_parsed_usage`를 쓴다."""
    parsed = _parsed_usage(payload, **proof)
    if (
        parsed.values[0] is not None
        and parsed.values[2] is not None
        and parsed.component_reasons["total_tokens"] == USAGE_COMPONENT_INVALID
    ):
        return (None, None, None, None, False)
    return (*parsed.values[:4], parsed.available)


class CodexStructuredRoleRunner:
    """CodexRuntimePort를 통해 strict JSON 역할을 최대 두 turn으로 실행한다."""

    def __init__(self, runtime: CodexRuntimePort, *, poll_interval_seconds: float = 0.25,
                 progress_sink: Callable[[dict[str, Any]], None] | None = None,
                 max_schema_recovery_attempts: int = 1,
                 operational_binding: OperationalBinding | None = None,
                 ephemeral_threads: bool = True,
                 interrupt_observation_seconds: float | None = None,
                 operation_trace_path: (
                     Path | str | Callable[[str], Path | str] | None
                 ) = None) -> None:
        if type(max_schema_recovery_attempts) is not int or max_schema_recovery_attempts not in {0, 1}:
            raise ValueError("schema recovery 상한은 0 또는 1이어야 합니다.")
        if type(ephemeral_threads) is not bool:
            raise ValueError("ephemeral_threads는 명시적 bool이어야 합니다.")
        if interrupt_observation_seconds is not None:
            RoleObservationPolicy(interrupt_observation_seconds=interrupt_observation_seconds)
        self.runtime = runtime
        self.ephemeral_threads = ephemeral_threads
        self.operational_binding = operational_binding
        self.max_schema_recovery_attempts = max_schema_recovery_attempts
        self.poll_interval_seconds = poll_interval_seconds
        self.progress_sink = progress_sink
        self.interrupt_observation_seconds = interrupt_observation_seconds
        self.operation_trace_path = operation_trace_path
        self.receipts: list[RoleCallReceipt] = []
        self.pending_terminal_observations: dict[str, Any] = {}
        self.operation_traces: dict[str, OperationTrace] = {}

    def _trace_path(self, call_id: str) -> Path | None:
        configured = self.operation_trace_path
        if configured is None:
            return None
        value = configured(call_id) if callable(configured) else configured
        path = Path(value)
        if path.suffix.casefold() != ".jsonl":
            path = path / f"{call_id}.operation-trace.jsonl"
        return path

    @staticmethod
    def _trace_response(value: Any) -> Any:
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    def _invoke_traced(
        self,
        trace: OperationTrace,
        kind: str,
        request: dict[str, Any],
        invoke: Callable[[], Any],
        *,
        call_id: str,
        deadline_seconds: float | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> Any:
        parent_scope = current_operation_trace_scope()
        category = (
            "wait" if kind == "sdk_wait" else
            "logical" if getattr(self.runtime, "emits_rpc_operation_trace", False) else
            "rpc"
        )
        token = trace.begin(
            kind, request, call_id=call_id, thread_id=thread_id, turn_id=turn_id,
            deadline_seconds=deadline_seconds,
            category=category,
            parent_operation_id=(
                None if parent_scope is None else parent_scope.parent.operation_id
            ),
            rpc_method=(f"port.{kind}" if category == "rpc" else None),
        )
        try:
            with trace.operation_scope(token), (
                role_execution_scope("structured_role") if kind in {"create", "start", "resume"} else nullcontext()
            ):
                result = invoke()
        except Exception as error:
            trace.finish(token, error=error, thread_id=thread_id, turn_id=turn_id)
            raise
        binding = getattr(result, "binding", None)
        trace.finish(
            token,
            response=self._trace_response(result),
            provider_call_id=getattr(result, "operation_id", None),
            thread_id=(getattr(binding, "thread_id", None) or thread_id),
            turn_id=(getattr(binding, "turn_id", None) or turn_id),
        )
        return result

    def _verify_inventory(self, request: RoleCallRequest, inventory: ModelInventory) -> OperationalBinding:
        if self.operational_binding is not None:
            verify_binding(self.operational_binding, inventory)
        return verify_binding(request.operational_binding, inventory,
                              role=request.role, model=request.model, effort=request.effort)

    def _progress(self, event: str, request: RoleCallRequest, call_id: str, **details: Any) -> None:
        self._progress_to(self.progress_sink, event, request, call_id, **details)

    @staticmethod
    def _progress_to(
        sink: Callable[[dict[str, Any]], None] | None,
        event: str,
        request: RoleCallRequest,
        call_id: str,
        *,
        durable_sink: Callable[[dict[str, Any]], None] | None = None,
        **details: Any,
    ) -> None:
        from .runtime import notify_active_runtime_job_progress

        progress = {"event": event, "call_id": call_id, "role": request.role,
                    "request_digest": request.request_digest,
                    "recorded_at": utc_now().isoformat(), **details}
        if durable_sink is None:
            notify_active_runtime_job_progress(progress)
        else:
            durable_sink(progress)
        if sink is not None:
            sink(progress)

    def run(
        self,
        request: RoleCallRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RoleCallResult:
        started = time.monotonic()
        call_id = new_id("model_call")
        trace = OperationTrace(
            context={"call_id": call_id, "role": request.role, "request_digest": request.request_digest},
            path=self._trace_path(call_id), expected_operations=("create", "start"),
        )
        self.operation_traces[call_id] = trace
        try:
            schema = strict_json_output_schema(request.output_schema)
            schema_digest = sha256_digest(schema)
            cwd = Path(request.cwd).resolve(strict=True)
            policy = self.runtime.verify_execution_policy(cwd)
            if (
                policy.environment != "local"
                or policy.permission_profile != REQUIRED_PERMISSION_PROFILE
                or policy.approval_policy != REQUIRED_APPROVAL_POLICY
            ):
                raise StructuredRoleError(
                    "PERMISSION_POLICY_MISMATCH", receipts=tuple(self.receipts),
                    effects_started=False,
                )
            inventory = self.runtime.list_models()
            request = RoleCallRequest.model_validate(request.model_dump(mode="python"))
            observation_policy = request.observation_policy or RoleObservationPolicy(
                interrupt_observation_seconds=(self.interrupt_observation_seconds or 0),
            )
            if (request.observation_policy is not None
                    and self.interrupt_observation_seconds is not None
                    and self.interrupt_observation_seconds != observation_policy.interrupt_observation_seconds):
                raise ValueError("ROLE_OBSERVATION_POLICY_RUNNER_MISMATCH")
            observed_binding = self._verify_inventory(request, inventory)
            self._progress("role_requested", request, call_id, model=request.model, effort=request.effort)
        except StructuredRoleError:
            raise
        except ValueError as error:
            raise StructuredRoleError(
                str(error), receipts=tuple(self.receipts), effects_started=False,
            ) from error
        except Exception as error:
            raise StructuredRoleError(
                str(error), receipts=tuple(self.receipts), effects_started=False,
            ) from error
        create_request = {
            "cwd": str(cwd), "title": f"FlowMarshal role: {request.role}",
            "model": request.model, "developer_instructions_digest": sha256_digest(request.instructions),
            "ephemeral": self.ephemeral_threads,
        }
        thread = self._invoke_traced(
            trace, "create", create_request,
            lambda: self.runtime.create_thread(
                cwd=cwd,
                title=f"FlowMarshal role: {request.role}",
                model=request.model,
                developer_instructions=request.instructions,
                ephemeral=self.ephemeral_threads,
            ),
            call_id=call_id, deadline_seconds=request.timeout_seconds,
        )
        if thread.binding is None:
            raise StructuredRoleError(
                "role thread binding이 없습니다.", receipts=tuple(self.receipts)
            )
        thread_id = thread.binding.thread_id
        created_thread = thread.payload.get("thread")
        empty_thread_creation_proven = (
            thread.operation_id == thread_id
            and thread.binding.turn_id is None
            and isinstance(created_thread, dict)
            and created_thread.get("id") == thread_id
            and created_thread.get("turns") == []
        )
        self._progress("thread_created", request, call_id, thread_id=thread_id,
                       ephemeral=self.ephemeral_threads)
        turn_ids: list[str] = []
        turn_binding_proofs: list[bool] = []
        first_empty_turn_proofs: list[bool] = []
        prompt = canonical_json(request.payload)
        recovery_attempts = 0
        last_error: Exception | None = None
        final_text: str | None = None
        observation_payload: dict[str, Any] = {}
        observation_thread_id: str | None = None
        observation_turn_id: str | None = None
        for attempt_index in range(self.max_schema_recovery_attempts + 1):
            if attempt_index == 1:
                recovery_attempts = 1
                prompt = canonical_json(
                    {
                        "original_input": request.payload,
                        "schema_recovery": {
                            "error": str(last_error),
                            "instruction": "직전 출력을 버리고 지정된 JSON Schema의 object만 반환하세요.",
                        },
                    }
                )
            try:
                observed_binding = self._verify_inventory(request, self.runtime.list_models())
            except ValueError as error:
                raise StructuredRoleError(str(error), receipts=tuple(self.receipts)) from error
            turn_request = {
                "thread_id": thread_id, "cwd": str(cwd), "prompt_digest": sha256_digest(prompt),
                "model": request.model, "effort": request.effort,
                "output_schema_digest": schema_digest,
            }
            turn = self._invoke_traced(
                trace, "start", turn_request,
                lambda: self.runtime.start_turn(
                    thread_id=thread_id,
                    cwd=cwd,
                    prompt=prompt,
                    model=request.model,
                    effort=request.effort,
                    output_schema=schema,
                ),
                call_id=call_id, deadline_seconds=request.timeout_seconds, thread_id=thread_id,
            )
            turn_ids.append(turn.operation_id)
            turn_binding_proofs.append(
                turn.binding is not None
                and turn.binding.thread_id == thread_id
                and turn.binding.turn_id == turn.operation_id
                and turn.payload.get("thread_id") == thread_id
                and turn.payload.get("turn_id") == turn.operation_id
            )
            first_empty_turn_proofs.append(turn.payload.get("first_empty_thread") is True)
            role_call_proofs = {
                "thread_creation_receipt": thread.model_dump(mode="json"),
                "turn_start_receipt": turn.model_dump(mode="json"),
            }
            self._progress("turn_started", request, call_id, thread_id=thread_id, turn_id=turn.operation_id)
            register = getattr(self.runtime, "register_completion_observer", None)
            if callable(register):
                from .runtime import capture_active_runtime_job_progress

                durable_progress = capture_active_runtime_job_progress()
                def record_terminal(observed, *, expected_turn_id=turn.operation_id,
                                    proofs=role_call_proofs, sink=self.progress_sink,
                                    durable_sink=durable_progress):
                    if (observed.thread_id != thread_id or observed.turn_id != expected_turn_id
                            or observed.active):
                        raise ValueError("ROLE_TERMINAL_OBSERVATION_BINDING_MISMATCH")
                    observed = observed.model_copy(update={"payload": observed.payload | {
                        "role_call_proofs": proofs, "role_call_proofs_digest": sha256_digest(proofs),
                    }})
                    self.pending_terminal_observations[call_id] = observed
                    self._progress_to(
                        sink,
                        ("role_terminal_observed" if observed.terminal_status in
                         PROVIDER_TERMINAL_STATUSES else "role_observation_incomplete"),
                        request,
                        call_id,
                        durable_sink=durable_sink,
                        terminal_observation=observed.model_dump(mode="json"),
                        terminal_observation_digest=sha256_digest(observed),
                    )
                register(thread_id=thread_id, turn_id=turn.operation_id, observer=record_terminal)
            deadline = started + request.timeout_seconds
            while True:
                observation = self._invoke_traced(
                    trace, "sdk_wait", {"thread_id": thread_id, "turn_id": turn.operation_id},
                    lambda: self.runtime.read(thread_id=thread_id),
                    call_id=call_id, deadline_seconds=max(0, deadline - time.monotonic()),
                    thread_id=thread_id, turn_id=turn.operation_id,
                )
                observation_payload = observation.payload
                observation_thread_id = observation.thread_id
                observation_turn_id = observation.turn_id
                if not observation.active:
                    if observation.terminal_status in PROVIDER_TERMINAL_STATUSES:
                        terminal_observation = observation.model_copy(update={
                            "payload": observation.payload | {
                                "role_call_proofs": role_call_proofs,
                                "role_call_proofs_digest": sha256_digest(role_call_proofs),
                            }
                        })
                        self._progress(
                            "role_terminal_observed",
                            request,
                            call_id,
                            terminal_observation=terminal_observation.model_dump(mode="json"),
                            terminal_observation_digest=sha256_digest(terminal_observation),
                        )
                    break
                if time.monotonic() >= deadline:
                    observation_deadline = time.monotonic() + observation_policy.interrupt_observation_seconds
                    interrupt_request = {
                        "thread_id": thread_id,
                        "turn_id": turn.operation_id,
                        "reason": "role_timeout",
                        "timeout_seconds": request.timeout_seconds,
                        "timeout_policy_digest": request.timeout_policy_digest,
                        "observation_policy_digest": request.observation_policy_digest,
                    }
                    interrupt_request_digest = sha256_digest(interrupt_request)
                    self._progress(
                        "interrupt_requested", request, call_id,
                        interrupt_request=interrupt_request,
                        interrupt_request_digest=interrupt_request_digest,
                    )
                    interrupt_receipt_digest = None
                    interrupt_error = None
                    try:
                        interrupt_wait = (
                            observation_policy.rpc_timeout_seconds
                            if observation_policy.interrupt_observation_seconds == 0 else
                            min(observation_policy.rpc_timeout_seconds, observation_deadline - time.monotonic())
                        )
                        interrupt_receipt = self._invoke_traced(
                            trace, "sdk_wait",
                            {"operation": "role_interrupt_wait", "thread_id": thread_id,
                             "turn_id": turn.operation_id},
                            lambda: bounded_observation_call(
                                lambda: self._invoke_traced(
                                    trace, "interrupt",
                                    {"thread_id": thread_id, "turn_id": turn.operation_id},
                                    lambda: self.runtime.interrupt(
                                        thread_id=thread_id, turn_id=turn.operation_id
                                    ),
                                    call_id=call_id, deadline_seconds=interrupt_wait,
                                    thread_id=thread_id, turn_id=turn.operation_id,
                                ),
                                timeout_seconds=interrupt_wait, operation_name="role_interrupt",
                            ),
                            call_id=call_id, deadline_seconds=interrupt_wait,
                            thread_id=thread_id, turn_id=turn.operation_id,
                        )
                        interrupt_receipt_digest = sha256_digest(interrupt_receipt)
                        self._progress(
                            "interrupt_receipt", request, call_id,
                            interrupt_receipt=interrupt_receipt.model_dump(mode="json"),
                            interrupt_receipt_digest=interrupt_receipt_digest,
                        )
                    except Exception as error:
                        interrupt_error = f"{type(error).__name__}: {error}"
                        self._progress(
                            "interrupt_failed", request, call_id, error=interrupt_error
                        )
                    terminal_observation_digest = None
                    terminal_status_after_interrupt = None
                    terminal_observation_error = None
                    observation_attempted = False
                    while True:
                        remaining = observation_deadline - time.monotonic()
                        if observation_attempted and remaining <= 0:
                            self._progress("terminal_observation_pending", request, call_id,
                                           observation=observation.model_dump(mode="json"))
                            break
                        try:
                            observation_attempted = True
                            # 유예 0은 과거 명시적 동작을 위한 단일 조회다.
                            read_wait = (observation_policy.rpc_timeout_seconds
                                         if observation_policy.interrupt_observation_seconds == 0 else
                                         min(observation_policy.rpc_timeout_seconds, remaining))
                            observation = self._invoke_traced(
                                trace, "sdk_wait",
                                {"operation": "role_terminal_read", "thread_id": thread_id,
                                 "turn_id": turn.operation_id},
                                lambda: bounded_observation_call(
                                    lambda: self.runtime.read(thread_id=thread_id),
                                    timeout_seconds=read_wait, operation_name="role_terminal_read",
                                ),
                                call_id=call_id, deadline_seconds=read_wait,
                                thread_id=thread_id, turn_id=turn.operation_id,
                            )
                            if observation.thread_id != thread_id or observation.turn_id != turn.operation_id:
                                raise ValueError("ROLE_TERMINAL_OBSERVATION_BINDING_MISMATCH")
                            observation = observation.model_copy(update={"payload": observation.payload | {
                                "role_call_proofs": role_call_proofs,
                                "role_call_proofs_digest": sha256_digest(role_call_proofs),
                            }})
                        except Exception as error:
                            terminal_observation_error = f"{type(error).__name__}: {error}"
                            self._progress(
                                "terminal_observation_failed", request, call_id,
                                error=terminal_observation_error,
                            )
                            break
                        observation_payload = observation.payload
                        observation_thread_id = observation.thread_id
                        observation_turn_id = observation.turn_id
                        if not observation.active:
                            self.pending_terminal_observations[call_id] = observation
                            if observation.terminal_status in PROVIDER_TERMINAL_STATUSES:
                                terminal_observation_digest = sha256_digest(observation)
                                terminal_status_after_interrupt = observation.terminal_status
                            self._progress(
                                ("terminal_observed_after_interrupt" if terminal_observation_digest
                                 else "terminal_observation_pending"), request, call_id,
                                terminal_observation=observation.model_dump(mode="json"),
                                terminal_observation_digest=terminal_observation_digest,
                            )
                            break
                        if time.monotonic() >= observation_deadline:
                            self._progress(
                                "terminal_observation_pending", request, call_id,
                                observation=observation.model_dump(mode="json"),
                            )
                            break
                        time.sleep(min(self.poll_interval_seconds, max(0, observation_deadline - time.monotonic())))
                    receipt = self._receipt(
                        call_id=call_id,
                        request=request, observed_binding=observed_binding,
                        schema_digest=schema_digest,
                        thread_id=thread_id,
                        turn_ids=tuple(turn_ids),
                        started=started,
                        status="timed_out",
                        recovery_attempts=recovery_attempts,
                        error="; ".join(item for item in (
                            "role turn timeout",
                            None if interrupt_error is None else f"interrupt={interrupt_error}",
                            None if terminal_observation_error is None else
                            f"terminal_observation={terminal_observation_error}",
                        ) if item is not None),
                        observation_payload=observation_payload,
                        observation_thread_id=observation_thread_id,
                        observation_turn_id=observation_turn_id,
                        turn_binding_proven=turn_binding_proofs == [True],
                        empty_thread_creation_proven=empty_thread_creation_proven,
                        first_empty_turn_proven=first_empty_turn_proofs == [True],
                        interrupt_request_digest=interrupt_request_digest,
                        interrupt_receipt_digest=interrupt_receipt_digest,
                        terminal_observation_digest=terminal_observation_digest,
                        terminal_status_after_interrupt=terminal_status_after_interrupt,
                    )
                    self.receipts.append(receipt)
                    raise StructuredRoleError(
                        "role turn timeout",
                        receipt=receipt,
                        receipts=tuple(self.receipts),
                    )
                time.sleep(self.poll_interval_seconds)
            if observation.terminal_status not in PROVIDER_SUCCESS_TERMINAL_STATUSES:
                receipt = self._receipt(
                    call_id=call_id,
                    request=request, observed_binding=observed_binding,
                    schema_digest=schema_digest,
                    thread_id=thread_id,
                    turn_ids=tuple(turn_ids),
                    started=started,
                    status=("failed" if observation.terminal_status in
                            PROVIDER_TERMINAL_STATUSES - PROVIDER_SUCCESS_TERMINAL_STATUSES
                            else "external_unknown"),
                    recovery_attempts=recovery_attempts,
                    error=f"terminal status={observation.terminal_status}",
                    observation_payload=observation_payload,
                    observation_thread_id=observation_thread_id,
                    observation_turn_id=observation_turn_id,
                    turn_binding_proven=turn_binding_proofs == [True],
                    empty_thread_creation_proven=empty_thread_creation_proven,
                    first_empty_turn_proven=first_empty_turn_proofs == [True],
                )
                self.receipts.append(receipt)
                raise StructuredRoleError(
                    "role turn이 완료되지 않았습니다: "
                    f"terminal_status={observation.terminal_status}, "
                    f"detail={observation.final_response or observation.payload.get('error')}",
                    receipt=receipt,
                    receipts=tuple(self.receipts),
                )
            final_text = observation.final_response
            try:
                if not final_text:
                    raise ValueError("빈 structured output")
                decoded = json.loads(final_text)
                if not isinstance(decoded, dict):
                    raise ValueError("structured output은 JSON object여야 합니다.")
                if validator is not None:
                    validator(decoded)
                receipt = self._receipt(
                    call_id=call_id,
                    request=request, observed_binding=observed_binding,
                    schema_digest=schema_digest,
                    thread_id=thread_id,
                    turn_ids=tuple(turn_ids),
                    started=started,
                    status="succeeded",
                    recovery_attempts=recovery_attempts,
                    output_digest=sha256_digest(decoded),
                    observation_payload=observation_payload,
                    observation_thread_id=observation_thread_id,
                    observation_turn_id=observation_turn_id,
                    turn_binding_proven=turn_binding_proofs == [True],
                    empty_thread_creation_proven=empty_thread_creation_proven,
                    first_empty_turn_proven=first_empty_turn_proofs == [True],
                )
                self.receipts.append(receipt)
                return RoleCallResult(payload=decoded, receipt=receipt)
            except Exception as error:
                last_error = error
                if isinstance(error, RoleInputContractError):
                    break
                if attempt_index < self.max_schema_recovery_attempts:
                    continue
        receipt = self._receipt(
            call_id=call_id,
            request=request, observed_binding=observed_binding,
            schema_digest=schema_digest,
            thread_id=thread_id,
            turn_ids=tuple(turn_ids),
            started=started,
            status="input_contract_failed" if isinstance(last_error, RoleInputContractError) else "schema_failed",
            recovery_attempts=recovery_attempts,
            error=str(last_error),
            observation_payload=observation_payload,
            observation_thread_id=observation_thread_id,
            observation_turn_id=observation_turn_id,
            turn_binding_proven=turn_binding_proofs == [True],
            empty_thread_creation_proven=empty_thread_creation_proven,
            first_empty_turn_proven=first_empty_turn_proofs == [True],
        )
        self.receipts.append(receipt)
        raise StructuredRoleError(
            (f"역할 입력 계약을 확인할 수 없습니다: {last_error}"
             if isinstance(last_error, RoleInputContractError)
             else f"structured output이 유효하지 않습니다. schema recovery {recovery_attempts}회"),
            receipt=receipt,
            receipts=tuple(self.receipts),
        )

    def _receipt(
        self,
        *,
        call_id: str,
        request: RoleCallRequest,
        schema_digest: str,
        observed_binding: OperationalBinding,
        thread_id: str,
        turn_ids: tuple[str, ...],
        started: float,
        status: str,
        recovery_attempts: int,
        observation_payload: dict[str, Any],
        observation_thread_id: str | None,
        observation_turn_id: str | None,
        turn_binding_proven: bool,
        empty_thread_creation_proven: bool,
        first_empty_turn_proven: bool,
        output_digest: str | None = None,
        error: str | None = None,
        interrupt_request_digest: str | None = None,
        interrupt_receipt_digest: str | None = None,
        terminal_observation_digest: str | None = None,
        terminal_status_after_interrupt: str | None = None,
    ) -> RoleCallReceipt:
        parsed_usage = _parsed_usage(
            observation_payload,
            thread_id=thread_id,
            turn_ids=turn_ids,
            observation_thread_id=observation_thread_id,
            observation_turn_id=observation_turn_id,
            turn_binding_proven=turn_binding_proven,
            empty_thread_creation_proven=empty_thread_creation_proven,
            first_empty_turn_proven=first_empty_turn_proven,
        )
        if (status not in {"succeeded", "failed", "schema_failed", "input_contract_failed"}
                and terminal_status_after_interrupt not in PROVIDER_TERMINAL_STATUSES):
            # 수집기 종료나 interrupt ACK는 provider 종료를 증명하지 않는다.
            parsed_usage = unavailable_provider_usage("PROVIDER_TERMINAL_UNOBSERVED")
        input_tokens, cached_tokens, output_tokens, reasoning_tokens, total_tokens = (
            parsed_usage.values
        )
        usage_available = parsed_usage.available
        trace = self.operation_traces.get(call_id)
        trace_body = None
        if trace is not None:
            pending = trace.snapshot()["manifest"]["pending_operation_ids"]
            trace_body = trace.snapshot() if pending else trace.seal()
        observed_model, observed_effort = authoritative_model_observation(
            observation_payload
        )
        receipt = RoleCallReceipt(
            call_id=call_id,
            role=request.role,
            status=status,
            model=request.model,
            effort=request.effort,
            binding_provenance_version="2.0",
            requested_model=request.model,
            requested_effort=request.effort,
            observed_model=observed_model,
            observed_effort=observed_effort,
            provider_inventory_digest=observed_binding.inventory.provider_inventory_digest,
            adapter_capability_digest=observed_binding.inventory.adapter_capability_digest,
            binding_provenance={
                "requested": "role_request",
                "observed": (
                    PROVIDER_RAW_MODEL_OBSERVATION_SOURCE
                    if observed_model is not None and observed_effort is not None
                    else None
                ),
                "provider_inventory": "model/list",
                "adapter_capability": "local_operational_binding",
            },
            inventory_digest=request.inventory_digest,
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            thread_id=thread_id,
            turn_ids=turn_ids,
            input_digest=request.request_digest,
            output_digest=output_digest,
            output_schema_digest=schema_digest,
            timeout_policy_digest=request.timeout_policy_digest,
            observation_policy_digest=request.observation_policy_digest,
            interrupt_request_digest=interrupt_request_digest,
            interrupt_receipt_digest=interrupt_receipt_digest,
            terminal_observation_digest=terminal_observation_digest,
            terminal_status_after_interrupt=terminal_status_after_interrupt,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_contract_version=USAGE_CONTRACT_VERSION,
            total_tokens=total_tokens,
            usage_component_reasons=parsed_usage.component_reasons,
            usage_available=usage_available,
            latency_ms=round((time.monotonic() - started) * 1000),
            schema_recovery_attempts=recovery_attempts,
            error_summary=error,
            recorded_at=utc_now(),
            observed_binding=observed_binding,
            operation_trace=trace_body,
            operation_trace_ref=None if trace is None or trace.path is None else str(trace.path.resolve()),
            operation_trace_digest=None if trace_body is None else sha256_digest(trace_body),
        )
        self._progress("role_receipt", request, call_id, receipt=receipt.model_dump(mode="json"))
        return receipt


class ScriptedStructuredRoleRunner:
    """typed adapter와 bounded search를 실모델 없이 검증하는 role port."""

    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = {role: list(values) for role, values in responses.items()}
        self.calls: list[RoleCallRequest] = []

    def run(
        self,
        request: RoleCallRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RoleCallResult:
        self.calls.append(request)
        try:
            payload = self.responses[request.role].pop(0)
        except (KeyError, IndexError) as error:
            raise StructuredRoleError(f"scripted response가 없습니다: {request.role}") from error
        if validator is not None:
            validator(payload)
        provenance = {}
        if request.operational_binding is not None:
            provenance = {
                "binding_provenance_version": "2.0",
                "requested_model": request.model,
                "requested_effort": request.effort,
                "observed_model": None,
                "observed_effort": None,
                "provider_inventory_digest": (
                    request.operational_binding.inventory.provider_inventory_digest
                ),
                "adapter_capability_digest": (
                    request.operational_binding.inventory.adapter_capability_digest
                ),
                "binding_provenance": {
                    "requested": "role_request",
                    "observed": None,
                    "provider_inventory": "model/list",
                    "adapter_capability": "local_operational_binding",
                },
            }
        call_id = new_id("model_call")
        receipt = RoleCallReceipt(
            call_id=call_id,
            role=request.role,
            status="succeeded",
            model=request.model,
            effort=request.effort,
            **provenance,
            inventory_digest=request.inventory_digest,
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            thread_id=f"scripted-thread-{call_id}",
            turn_ids=(f"scripted-turn-{call_id}",),
            input_digest=request.request_digest,
            output_digest=sha256_digest(payload),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            timeout_policy_digest=request.timeout_policy_digest,
            observation_policy_digest=request.observation_policy_digest,
            latency_ms=0,
            recorded_at=utc_now(),
            observed_binding=request.operational_binding,
        )
        return RoleCallResult(payload=payload, receipt=receipt)


def verify_role_receipt(request: RoleCallRequest, result: RoleCallResult) -> None:
    """provider 관측을 받아들이기 전에 요청·응답·inventory 증거를 함께 검사한다."""
    request = RoleCallRequest.model_validate(request.model_dump(mode="python"))
    receipt = RoleCallReceipt.model_validate(result.receipt.model_dump(mode="python"))
    if (receipt.input_digest != request.request_digest or receipt.output_digest != sha256_digest(result.payload)
            or receipt.output_schema_digest != sha256_digest(strict_json_output_schema(request.output_schema))
            or receipt.timeout_policy_digest != request.timeout_policy_digest
            or receipt.observation_policy_digest != request.observation_policy_digest
            or (receipt.role, receipt.model, receipt.effort, receipt.inventory_digest)
            != (request.role, request.model, request.effort, request.inventory_digest)):
        raise StructuredRoleError("ROLE_RECEIPT_BINDING_MISMATCH")
    if receipt.binding_provenance_version == "2.0":
        if receipt.observed_binding is None or (
            receipt.provider_inventory_digest
            != receipt.observed_binding.inventory.provider_inventory_digest
            or receipt.adapter_capability_digest
            != receipt.observed_binding.inventory.adapter_capability_digest
        ):
            raise StructuredRoleError("ROLE_RECEIPT_PROVENANCE_MISMATCH")
    if receipt.operation_trace is not None:
        verification = OperationTrace.verify(
            receipt.operation_trace, expected_call_ids=(receipt.call_id,)
        )
        if not verification.valid:
            raise StructuredRoleError(
                "ROLE_OPERATION_TRACE_INVALID: " + ", ".join(verification.errors)
            )
        if receipt.operation_trace.get("context", {}).get("call_id") != receipt.call_id:
            raise StructuredRoleError("ROLE_OPERATION_TRACE_CALL_BINDING_MISMATCH")
        if receipt.status == "succeeded" and not verification.complete:
            raise StructuredRoleError("ROLE_OPERATION_TRACE_INCOMPLETE")
    if request.operational_binding is not None:
        if receipt.observed_binding is None:
            raise StructuredRoleError("MODEL_LOCK_RECEIPT_OBSERVATION_MISSING")
        try:
            observed = verify_binding(request.operational_binding, receipt.observed_binding.inventory,
                                      role=request.role, model=request.model, effort=request.effort)
            if observed != receipt.observed_binding:
                raise ValueError("MODEL_LOCK_RECEIPT_DIGEST_MISMATCH")
        except ValueError as error:
            raise StructuredRoleError(str(error)) from error
