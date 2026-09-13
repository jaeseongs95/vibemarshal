from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from ..canonical import sha256_digest

from .domain import EngineModel, validate_usage_component_contract
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
    binding_provenance_version: str | None = None
    requested_model: str | None = None
    requested_effort: str | None = None
    observed_model: str | None = None
    observed_effort: str | None = None
    provider_inventory_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    adapter_capability_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    binding_provenance: dict[str, str | None] | None = None
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
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    usage_contract_version: Literal["2.0"] | None = None
    total_tokens: int | None = Field(default=None, ge=0)
    usage_component_reasons: dict[str, str | None] | None = None
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

    @property
    def usage_complete(self) -> bool:
        if self.usage_contract_version is not None:
            return self.usage_available and all(
                value is not None
                for value in (
                    self.input_tokens,
                    self.cached_input_tokens,
                    self.output_tokens,
                    self.reasoning_tokens,
                    self.total_tokens,
                )
            )
        return self.usage_available and all(
            value is not None
            for value in (
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                self.reasoning_tokens,
            )
        )

    @property
    def observed_token_subtotal(self) -> int | None:
        if self.usage_contract_version is not None and self.total_tokens is not None:
            return self.total_tokens
        values = tuple(
            value for value in (self.input_tokens, self.output_tokens) if value is not None
        )
        return sum(values) if values else None

    @property
    def usage_total_complete(self) -> bool:
        if not self.usage_available:
            return False
        if self.usage_contract_version is not None:
            return self.total_tokens is not None
        return self.input_tokens is not None and self.output_tokens is not None

    @model_validator(mode="after")
    def operation_trace_is_bound(self):
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.total_tokens,
        )
        validate_usage_component_contract(
            version=self.usage_contract_version,
            values=values,
            component_reasons=self.usage_component_reasons,
        )
        effective_values = values if self.usage_contract_version is not None else values[:4]
        if (
            self.usage_contract_version is not None
            and self.usage_available != any(value is not None for value in effective_values)
        ):
            raise ValueError("usage_available은 관측된 token 구성요소와 일치해야 합니다.")
        if self.binding_provenance_version is not None:
            if self.binding_provenance_version != "2.0":
                raise ValueError("지원하지 않는 model provenance projection입니다.")
            if (self.requested_model, self.requested_effort) != (self.model, self.effort):
                raise ValueError("legacy model/effort alias는 requested binding과 같아야 합니다.")
            if any(value is None for value in (
                self.provider_inventory_digest,
                self.adapter_capability_digest,
                self.binding_provenance,
            )):
                raise ValueError("v2 model provenance 필드가 완전하지 않습니다.")
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
        if self.binding_provenance_version is None:
            for field_name in (
                "binding_provenance_version", "requested_model", "requested_effort",
                "observed_model", "observed_effort", "provider_inventory_digest",
                "adapter_capability_digest", "binding_provenance",
            ):
                value.pop(field_name, None)
        if self.usage_contract_version is None:
            for field_name in (
                "usage_contract_version", "total_tokens", "usage_component_reasons",
            ):
                value.pop(field_name, None)
        return value
