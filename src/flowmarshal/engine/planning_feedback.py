from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel, PlanContractRevision, PlanSkeletonCandidate


RefinementAction = Literal["detail_revision", "skeleton_revision", "disputed", "unresolved"]


class PlanRefinementProvenance(EngineModel):
    source_evaluation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposal_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    call_id: str = Field(min_length=1)
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PlanRefinementProposal(EngineModel):
    """실패에 대한 비권위 수정 제안. 원본 검토나 Core 판정을 대체하지 않는다."""

    action: RefinementAction
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    plan: PlanContractRevision | None = None
    skeleton: PlanSkeletonCandidate | None = None
    provenance: PlanRefinementProvenance | None = None

    @model_validator(mode="after")
    def action_matches_candidate(self) -> "PlanRefinementProposal":
        if (self.plan is not None) != (self.action == "detail_revision"):
            raise ValueError("상세 수정 제안에만 새 Plan이 필요합니다.")
        if (self.skeleton is not None) != (self.action == "skeleton_revision"):
            raise ValueError("Skeleton 수정 제안에만 새 Skeleton이 필요합니다.")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("수정 제안의 evidence ref가 중복됐습니다.")
        if self.provenance is not None:
            digest = sha256_digest(self.model_dump(mode="json", exclude={"provenance"}))
            if self.provenance.proposal_digest != digest:
                raise ValueError("수정 제안이 실제 생성 관측의 digest와 다릅니다.")
        return self


class PlanRefinementAttempt(EngineModel):
    source_plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposal: PlanRefinementProposal
    result: Literal["evaluated", "unchanged_candidate", "disputed", "unresolved"]


class PlanRefinementStop(EngineModel):
    source_plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reason: Literal["refinement_limit", "candidate_version_budget", "insufficient_call_budget", "refiner_unavailable"]


def validate_plan_revision(previous: PlanContractRevision, revised: PlanContractRevision) -> None:
    if (
        revised.plan_id != previous.plan_id
        or revised.plan_revision_id == previous.plan_revision_id
        or revised.revision_no != previous.revision_no + 1
        or revised.supersedes_plan_revision_id != previous.plan_revision_id
    ):
        raise ValueError("수정 Plan은 같은 Plan의 직전 revision을 명시적으로 supersede해야 합니다.")
    for name in ("project_id", "goal_contract_digest", "base_state_snapshot_digest", "project_map_digest", "planning_budget", "model_inventory_digest"):
        if getattr(previous.definition, name) != getattr(revised.definition, name):
            raise ValueError(f"수정 Plan이 고정 검색 입력을 바꿨습니다: {name}")


def skeleton_semantic_digest(candidate: PlanSkeletonCandidate) -> str:
    """새 ID·version·비용 추정만으로 같은 후보를 재검토하지 않는다."""
    value = candidate.model_dump(mode="json", exclude={
        "candidate_id", "parent_candidate_id", "version", "refinement_round",
        "estimated_change_cost", "estimated_context_tokens",
    })
    for name in ("tasks", "dependencies", "goal_coverage", "unknowns"):
        value[name].sort(key=sha256_digest)
    return sha256_digest(value)


def plan_semantic_digest(plan: PlanContractRevision) -> str:
    """원장용 새 ID와 지역 검사 ID 이름을 제외한 실제 계약을 비교한다."""
    value = plan.definition.model_dump(mode="json")
    task_refs = {task["task_id"]: task["task_ref"] for task in value["tasks"]}
    validation_refs = {}
    for task in value["tasks"]:
        task.pop("task_id")
        for precondition in task["preconditions"]:
            precondition.pop("precondition_id")
        for effect in task["expected_effects"] + task["prohibited_effects"]:
            effect.pop("effect_id")
        for validation in task["validations"]:
            identifier = validation.pop("validation_id")
            validation_refs[identifier] = sha256_digest({
                "owner": task["task_ref"], "validation": validation,
            })
        for name in ("preconditions", "expected_effects", "prohibited_effects", "validations"):
            task[name].sort(key=sha256_digest)
    for validation in value["integration_validations"]:
        identifier = validation.pop("validation_id")
        validation_refs[identifier] = sha256_digest({"owner": "integration", "validation": validation})
    for edge in value["dependencies"]:
        for key in ("producer_task_id", "consumer_task_id"):
            edge[key] = task_refs[edge[key]]
    for coverage in value["goal_coverage"]:
        coverage["task_ids"] = sorted(task_refs[key] for key in coverage["task_ids"])
        coverage["validation_ids"] = sorted(validation_refs[key] for key in coverage["validation_ids"])
    for name in ("tasks", "dependencies", "goal_coverage", "integration_validations"):
        value[name].sort(key=sha256_digest)
    return sha256_digest(value)
