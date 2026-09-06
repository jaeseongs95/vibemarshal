"""상세 후보 출력 비용을 실제 역할 호출과 정확한 Plan revision에 결속한다."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel, PlanContractRevision
from .roles import RoleCallReceipt


_DIGEST = r"^sha256:[0-9a-f]{64}$"


class CandidateOutputBinding(EngineModel):
    schema_version: Literal["1.0"] = "1.0"
    call_id: str = Field(min_length=1)
    role: Literal["plan_expander", "plan_refiner"]
    receipt_digest: str = Field(pattern=_DIGEST)
    output_digest: str = Field(pattern=_DIGEST)
    output_kind: Literal["detail_plan", "skeleton_revision", "no_candidate"]
    plan_activation_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def plan_matches_output_kind(self) -> "CandidateOutputBinding":
        if (self.output_kind == "detail_plan") != (self.plan_activation_digest is not None):
            raise ValueError("상세 Plan 출력에만 정확한 activation digest가 필요합니다.")
        if self.role == "plan_expander" and self.output_kind != "detail_plan":
            raise ValueError("Plan expander의 완료 출력은 상세 Plan이어야 합니다.")
        return self


class CandidateOutputRecorder:
    """adapter의 후보 반환과 그 호출 receipt를 관측하며 후보·판정을 바꾸지 않는다."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.bindings: list[CandidateOutputBinding] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    def _record(self, before: int, *, role: str, output_kind: str, plan: Any) -> None:
        receipts = self.adapter.receipts[before:]
        if len(receipts) != 1:
            raise ValueError("상세 후보 반환과 역할 receipt를 일대일로 결속할 수 없습니다.")
        receipt = receipts[0]
        if receipt.role != role or receipt.status != "succeeded" or receipt.output_digest is None:
            raise ValueError("상세 후보에 해당 역할의 완료 receipt가 없습니다.")
        self.bindings.append(CandidateOutputBinding(
            call_id=receipt.call_id,
            role=role,
            receipt_digest=sha256_digest(receipt),
            output_digest=receipt.output_digest,
            output_kind=output_kind,
            plan_activation_digest=None if plan is None else plan.activation_digest,
        ))

    def expand(self, **arguments: Any) -> PlanContractRevision:
        before = len(self.adapter.receipts)
        plan = self.adapter.expand(**arguments)
        self._record(before, role="plan_expander", output_kind="detail_plan", plan=plan)
        return plan

    def refine(self, **arguments: Any) -> Any:
        before = len(self.adapter.receipts)
        proposal = self.adapter.refine(**arguments)
        kind = (
            "detail_plan" if proposal.plan is not None
            else "skeleton_revision" if proposal.skeleton is not None
            else "no_candidate"
        )
        self._record(before, role="plan_refiner", output_kind=kind, plan=proposal.plan)
        binding = self.bindings[-1]
        provenance = proposal.provenance
        if (
            provenance is None or provenance.call_id != binding.call_id
            or provenance.receipt_digest != binding.receipt_digest
            or provenance.output_digest != binding.output_digest
        ):
            raise ValueError("Plan 수정의 출력 비용이 원래 수정 provenance와 다릅니다.")
        return proposal


def candidate_output_costs(raw: dict[str, Any]) -> tuple[int, int]:
    """배분 가능한 expander/refiner 출력만 합산하며 순서로 호출 귀속을 추정하지 않는다."""
    receipts: dict[str, RoleCallReceipt] = {}
    for value in raw["receipts"]:
        if value["role"] not in {"plan_expander", "plan_refiner"}:
            continue
        if type(value.get("output_tokens")) is not int:
            raise ValueError("상세 후보 출력 token의 실제 관측이 없습니다.")
        receipt = RoleCallReceipt.model_validate(value)
        if receipt.call_id in receipts:
            raise ValueError("상세 후보 비용에 중복 호출 receipt가 있습니다.")
        receipts[receipt.call_id] = receipt
    bindings = tuple(CandidateOutputBinding.model_validate(value)
                     for value in raw.get("candidate_output_bindings", ()))
    if len(bindings) != len(receipts) or {item.call_id for item in bindings} != set(receipts):
        raise ValueError("상세 후보·수정 역할과 출력 비용 결속의 범위가 다릅니다.")
    plans = {
        PlanContractRevision.model_validate(item["plan"]).activation_digest
        for item in raw.get("planning_outcome", {}).get("plan_evaluations", ())
    }
    refinements = raw.get("planning_outcome", {}).get("plan_refinements", ())
    for item in refinements:
        proposal = item["proposal"]
        if proposal.get("plan") is not None:
            plans.add(PlanContractRevision.model_validate(proposal["plan"]).activation_digest)
    seen_plans: set[str] = set()
    candidate_tokens = discarded_tokens = 0
    for binding in bindings:
        receipt = receipts[binding.call_id]
        if (receipt.role != binding.role or receipt.status != "succeeded"
                or sha256_digest(receipt) != binding.receipt_digest
                or receipt.output_digest != binding.output_digest
                or not receipt.usage_available
                or type(receipt.output_tokens) is not int):
            raise ValueError("상세 후보 비용의 역할·receipt·실측 token 결속이 다릅니다.")
        if binding.role == "plan_refiner":
            matches = [item["proposal"] for item in refinements
                       if (item["proposal"].get("provenance") or {}).get("call_id") == binding.call_id]
            if len(matches) != 1:
                raise ValueError("수정 역할의 비용에 정확한 수정 응답 provenance가 없습니다.")
            proposal = matches[0]
            provenance = proposal["provenance"]
            expected_kind = (
                "detail_plan" if proposal.get("plan") is not None
                else "skeleton_revision" if proposal.get("skeleton") is not None
                else "no_candidate"
            )
            expected_plan = (
                None if proposal.get("plan") is None
                else PlanContractRevision.model_validate(proposal["plan"]).activation_digest
            )
            if (provenance.get("receipt_digest") != binding.receipt_digest
                    or provenance.get("output_digest") != binding.output_digest
                    or binding.output_kind != expected_kind
                    or binding.plan_activation_digest != expected_plan):
                raise ValueError("수정 응답과 비용 결속의 상세 후보가 다릅니다.")
        if binding.output_kind != "detail_plan":
            continue
        plan_digest = binding.plan_activation_digest
        if plan_digest not in plans or plan_digest in seen_plans:
            raise ValueError("상세 후보 비용에 알 수 없거나 중복된 Plan revision이 있습니다.")
        seen_plans.add(plan_digest)
        candidate_tokens += receipt.output_tokens
        if plan_digest != raw.get("selected_activation_digest"):
            discarded_tokens += receipt.output_tokens
    if seen_plans != plans:
        raise ValueError("상세 Plan 후보 중 출력 비용을 관측하지 못한 revision이 있습니다.")
    return candidate_tokens, discarded_tokens
