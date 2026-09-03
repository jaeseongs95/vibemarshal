from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import Field

from ..canonical import canonical_json, sha256_digest
from .domain import EngineModel, new_id, utc_now
from .runtime import (
    CodexRuntimePort,
    REQUIRED_APPROVAL_POLICY,
    REQUIRED_PERMISSION_PROFILE,
    RuntimePolicyError,
)


class StructuredRoleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        receipt: "RoleCallReceipt | None" = None,
        receipts: tuple["RoleCallReceipt", ...] = (),
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.receipts = receipts or (() if receipt is None else (receipt,))


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

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)


class RoleCallReceipt(EngineModel):
    call_id: str
    role: str
    status: str
    model: str
    effort: str
    inventory_digest: str
    permission_profile: str
    approval_policy: str
    thread_id: str | None = None
    turn_ids: tuple[str, ...] = ()
    input_digest: str
    output_digest: str | None = None
    output_schema_digest: str
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    usage_available: bool = False
    latency_ms: int = Field(ge=0)
    schema_recovery_attempts: int = Field(default=0, ge=0, le=1)
    error_summary: str | None = None
    recorded_at: datetime


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
                node["additionalProperties"] = False
                node["required"] = list(properties)
                for child in properties.values():
                    visit(child)
        for key in ("$defs", "definitions"):
            definitions = node.get(key)
            if isinstance(definitions, dict):
                for child in definitions.values():
                    visit(child)
        for key in ("items", "anyOf", "oneOf", "allOf"):
            if key in node:
                visit(node[key])

    visit(normalized)
    return normalized


def _usage(payload: dict[str, Any]) -> tuple[int, int, int, int, bool]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0, 0, False)
    totals = usage.get("total")
    source = totals if isinstance(totals, dict) else usage

    def integer(*names: str) -> int:
        for name in names:
            value = source.get(name)
            if isinstance(value, int) and value >= 0:
                return value
        return 0

    values = (
        integer("inputTokens", "input_tokens"),
        integer("cachedInputTokens", "cached_input_tokens"),
        integer("outputTokens", "output_tokens"),
        integer("reasoningOutputTokens", "reasoning_tokens"),
    )
    known_names = {
        "inputTokens",
        "input_tokens",
        "cachedInputTokens",
        "cached_input_tokens",
        "outputTokens",
        "output_tokens",
        "reasoningOutputTokens",
        "reasoning_tokens",
    }
    available = any(isinstance(source.get(name), int) for name in known_names)
    return (*values, available)


class CodexStructuredRoleRunner:
    """CodexRuntimePort를 통해 strict JSON 역할을 최대 두 turn으로 실행한다."""

    def __init__(self, runtime: CodexRuntimePort, *, poll_interval_seconds: float = 0.25,
                 progress_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.runtime = runtime
        self.poll_interval_seconds = poll_interval_seconds
        self.progress_sink = progress_sink
        self.receipts: list[RoleCallReceipt] = []

    def _progress(self, event: str, request: RoleCallRequest, call_id: str, **details: Any) -> None:
        if self.progress_sink is not None:
            self.progress_sink({"event": event, "call_id": call_id, "role": request.role,
                                "request_digest": request.request_digest,
                                "recorded_at": utc_now().isoformat(), **details})

    def run(
        self,
        request: RoleCallRequest,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> RoleCallResult:
        started = time.monotonic()
        call_id = new_id("model_call")
        schema = strict_json_output_schema(request.output_schema)
        schema_digest = sha256_digest(schema)
        cwd = Path(request.cwd).resolve(strict=True)
        policy = self.runtime.verify_execution_policy(cwd)
        if (
            policy.environment != "local"
            or policy.permission_profile != REQUIRED_PERMISSION_PROFILE
            or policy.approval_policy != REQUIRED_APPROVAL_POLICY
        ):
            raise StructuredRoleError("PERMISSION_POLICY_MISMATCH", receipts=tuple(self.receipts))
        inventory = self.runtime.list_models()
        if inventory.inventory_digest != request.inventory_digest:
            raise StructuredRoleError("MODEL_INVENTORY_CHANGED", receipts=tuple(self.receipts))
        if not inventory.supports(request.model, request.effort):
            raise StructuredRoleError("MODEL_BINDING_UNAVAILABLE", receipts=tuple(self.receipts))
        self._progress("role_requested", request, call_id, model=request.model, effort=request.effort)
        thread = self.runtime.create_thread(
            cwd=cwd,
            title=f"FlowMarshal role: {request.role}",
            model=request.model,
            developer_instructions=request.instructions,
            ephemeral=True,
        )
        if thread.binding is None:
            raise StructuredRoleError(
                "role thread binding이 없습니다.", receipts=tuple(self.receipts)
            )
        thread_id = thread.binding.thread_id
        self._progress("thread_created", request, call_id, thread_id=thread_id, ephemeral=True)
        turn_ids: list[str] = []
        prompt = canonical_json(request.payload)
        recovery_attempts = 0
        last_error: Exception | None = None
        final_text: str | None = None
        observation_payload: dict[str, Any] = {}
        for attempt_index in range(2):
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
            turn = self.runtime.start_turn(
                thread_id=thread_id,
                cwd=cwd,
                prompt=prompt,
                model=request.model,
                effort=request.effort,
                output_schema=schema,
            )
            turn_ids.append(turn.operation_id)
            self._progress("turn_started", request, call_id, thread_id=thread_id, turn_id=turn.operation_id)
            deadline = started + request.timeout_seconds
            while True:
                observation = self.runtime.read(thread_id=thread_id)
                observation_payload = observation.payload
                if not observation.active:
                    break
                if time.monotonic() >= deadline:
                    try:
                        self.runtime.interrupt(thread_id=thread_id, turn_id=turn.operation_id)
                    except Exception:
                        pass
                    receipt = self._receipt(
                        call_id=call_id,
                        request=request,
                        schema_digest=schema_digest,
                        thread_id=thread_id,
                        turn_ids=tuple(turn_ids),
                        started=started,
                        status="timed_out",
                        recovery_attempts=recovery_attempts,
                        error="role turn timeout",
                        observation_payload=observation_payload,
                    )
                    self.receipts.append(receipt)
                    raise StructuredRoleError(
                        "role turn timeout",
                        receipt=receipt,
                        receipts=tuple(self.receipts),
                    )
                time.sleep(self.poll_interval_seconds)
            if observation.terminal_status not in {"completed", "success", "succeeded"}:
                receipt = self._receipt(
                    call_id=call_id,
                    request=request,
                    schema_digest=schema_digest,
                    thread_id=thread_id,
                    turn_ids=tuple(turn_ids),
                    started=started,
                    status="failed",
                    recovery_attempts=recovery_attempts,
                    error=f"terminal status={observation.terminal_status}",
                    observation_payload=observation_payload,
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
                    request=request,
                    schema_digest=schema_digest,
                    thread_id=thread_id,
                    turn_ids=tuple(turn_ids),
                    started=started,
                    status="succeeded",
                    recovery_attempts=recovery_attempts,
                    output_digest=sha256_digest(decoded),
                    observation_payload=observation_payload,
                )
                self.receipts.append(receipt)
                return RoleCallResult(payload=decoded, receipt=receipt)
            except Exception as error:
                last_error = error
                if attempt_index == 0:
                    continue
        receipt = self._receipt(
            call_id=call_id,
            request=request,
            schema_digest=schema_digest,
            thread_id=thread_id,
            turn_ids=tuple(turn_ids),
            started=started,
            status="schema_failed",
            recovery_attempts=recovery_attempts,
            error=str(last_error),
            observation_payload=observation_payload,
        )
        self.receipts.append(receipt)
        raise StructuredRoleError(
            "schema recovery 후에도 structured output이 유효하지 않습니다.",
            receipt=receipt,
            receipts=tuple(self.receipts),
        )

    def _receipt(
        self,
        *,
        call_id: str,
        request: RoleCallRequest,
        schema_digest: str,
        thread_id: str,
        turn_ids: tuple[str, ...],
        started: float,
        status: str,
        recovery_attempts: int,
        observation_payload: dict[str, Any],
        output_digest: str | None = None,
        error: str | None = None,
    ) -> RoleCallReceipt:
        input_tokens, cached_tokens, output_tokens, reasoning_tokens, usage_available = _usage(observation_payload)
        receipt = RoleCallReceipt(
            call_id=call_id,
            role=request.role,
            status=status,
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            thread_id=thread_id,
            turn_ids=turn_ids,
            input_digest=request.request_digest,
            output_digest=output_digest,
            output_schema_digest=schema_digest,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            usage_available=usage_available,
            latency_ms=round((time.monotonic() - started) * 1000),
            schema_recovery_attempts=recovery_attempts,
            error_summary=error,
            recorded_at=utc_now(),
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
        receipt = RoleCallReceipt(
            call_id=new_id("model_call"),
            role=request.role,
            status="succeeded",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=REQUIRED_PERMISSION_PROFILE,
            approval_policy=REQUIRED_APPROVAL_POLICY,
            input_digest=request.request_digest,
            output_digest=sha256_digest(payload),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            latency_ms=0,
            recorded_at=utc_now(),
        )
        return RoleCallResult(payload=payload, receipt=receipt)
