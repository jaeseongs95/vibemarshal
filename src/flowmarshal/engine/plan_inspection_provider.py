from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel
from .roles import RoleCallRequest, RoleCallResult, strict_json_output_schema


PLAN_INSPECTION_PROVIDER_V1 = "plan-inspection-v1"
PLAN_INSPECTION_PROVIDER_V2 = "plan-inspection-v2"
PlanInspectionProviderVersion = Literal["plan-inspection-v1", "plan-inspection-v2"]


class PlanInspectionRequestBinding(EngineModel):
    """Provider 형식과 실제 역할 요청을 한 digest로 결속한다."""

    format: Literal["flowmarshal-plan-inspection-request-binding-v1"]
    provider_version: PlanInspectionProviderVersion
    role: str = Field(min_length=1, max_length=100)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_body(self) -> "PlanInspectionRequestBinding":
        body = self.model_dump(mode="json", exclude={"binding_digest"})
        if self.binding_digest != sha256_digest(body):
            raise ValueError("PLAN_INSPECTION_REQUEST_BINDING_DIGEST_MISMATCH")
        return self


def bind_plan_inspection_request(
    request: RoleCallRequest,
    provider_version: PlanInspectionProviderVersion,
) -> PlanInspectionRequestBinding:
    body = {
        "format": "flowmarshal-plan-inspection-request-binding-v1",
        "provider_version": provider_version,
        "role": request.role,
        "request_digest": request.request_digest,
        "prompt_digest": sha256_digest({"instructions": request.instructions}),
        "output_schema_digest": sha256_digest(strict_json_output_schema(request.output_schema)),
    }
    return PlanInspectionRequestBinding(**body, binding_digest=sha256_digest(body))


def verify_plan_inspection_result_binding(
    binding: PlanInspectionRequestBinding,
    request: RoleCallRequest,
    result: RoleCallResult,
) -> None:
    expected = bind_plan_inspection_request(request, binding.provider_version)
    if expected != binding:
        raise ValueError("PLAN_INSPECTION_REQUEST_BINDING_MISMATCH")
    if (
        result.receipt.input_digest != binding.request_digest
        or result.receipt.output_schema_digest != binding.output_schema_digest
        or result.receipt.role != binding.role
    ):
        raise ValueError("PLAN_INSPECTION_RECEIPT_BINDING_MISMATCH")
