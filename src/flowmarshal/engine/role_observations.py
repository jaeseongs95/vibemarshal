from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_serializer, model_validator

from ..canonical import sha256_digest

from .domain import EngineModel
from .model_lock import OperationalBinding


class RoleInputContractError(ValueError):
    """역할 출력이 아니라 결속한 권위 입력·관측 원문의 무결성 실패."""


class StructuredRoleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        receipt: "RoleCallReceipt | None" = None,
        receipts: tuple["RoleCallReceipt", ...] = (),
        effects_started: bool = True,
        settled_provider_call_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.receipts = receipts or (() if receipt is None else (receipt,))
        self.effects_started = effects_started
        self.settled_provider_call_id = settled_provider_call_id


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
    timeout_policy_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    observation_policy_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    interrupt_request_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    interrupt_receipt_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    terminal_observation_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    terminal_status_after_interrupt: str | None = None
    input_tokens: int | None = Field(default=0, ge=0)
    cached_input_tokens: int | None = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=0, ge=0)
    reasoning_tokens: int | None = Field(default=0, ge=0)
    usage_available: bool = False
    latency_ms: int = Field(ge=0)
    schema_recovery_attempts: int = Field(default=0, ge=0, le=1)
    error_summary: str | None = None
    recorded_at: datetime
    observed_binding: OperationalBinding | None = None
    operation_trace: dict | None = None
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
    def omit_absent_execution_observations(self, handler):
        value = handler(self)
        for field_name in (
            "timeout_policy_digest", "observation_policy_digest", "interrupt_request_digest",
            "interrupt_receipt_digest", "terminal_observation_digest",
            "terminal_status_after_interrupt",
            "operation_trace", "operation_trace_ref", "operation_trace_digest",
        ):
            if getattr(self, field_name) is None:
                value.pop(field_name, None)
        return value
