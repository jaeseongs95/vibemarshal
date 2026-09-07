from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    EngineModel,
    GoalContractRevision,
    PlanContractRevision,
    PlanSkeletonCandidate,
    ProjectMapRevision,
    StateSnapshot,
)
from .planner_roles import PlanExpansionEnvelopeV2, compact_project_map
from .roles import RoleCallRequest, RoleCallResult, verify_role_receipt
from .runtime import REQUIRED_APPROVAL_POLICY, REQUIRED_PERMISSION_PROFILE


GENERATION_REVIEW_CRITERIA: tuple[str, ...] = (
    "Goal과 생성 Plan의 전체 의미, Hard AC, 제약, 비목표, 효과 정책이 보존됐는지 대조한다.",
    "모든 Hard AC와 Task 기여 및 validation 연결을 원문 statement와 validation_intent에 대조한다.",
    "명시된 검사 절차, 도구, phase, method, evidence mode와 실제 검사 소유 책임이 일치하는지 대조한다.",
    "등록된 instruction/reference의 검증된 실제 본문이 주장한 검사 능력과 적용 범위를 지원하는지 대조한다.",
    "각 Task의 producer/consumer 및 produces/consumes가 Skeleton의 기여 책임과 DAG를 보존하는지 대조한다.",
    "Plan에 적힌 검사 책임과 이 진단에서 실제로 새로 실행해 관측한 검사의 범위를 구분한다.",
)

_CRITERIA_FORMAT = "flowmarshal-independent-generation-review-criteria-v1"
_SOURCE_FORMAT = "flowmarshal-generation-review-source-binding-v1"
_PROVENANCE_FORMAT = "independent_manual_review"
_INDEPENDENCE_SCOPE = "operator_attestation_not_cryptographic_identity_proof"
_ACCOUNTING_SCOPE = "manual_review_separate_from_harness_provider_receipts"


class GenerationReviewBindingError(ValueError):
    """생성 평가의 원문·검토 provenance 결속이 불완전하거나 변조됐다."""


def generation_review_criteria_binding() -> dict[str, Any]:
    body = {"format": _CRITERIA_FORMAT, "criteria": list(GENERATION_REVIEW_CRITERIA)}
    return body | {"criteria_digest": sha256_digest(body)}


def generation_review_criteria_digest() -> str:
    return generation_review_criteria_binding()["criteria_digest"]


class GenerationReviewSourceBinding(EngineModel):
    format: Literal["flowmarshal-generation-review-source-binding-v1"] = _SOURCE_FORMAT
    run_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    criteria_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expansion_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expansion_result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expansion_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expansion_call_id: str = Field(min_length=1)
    expansion_thread_id: str = Field(min_length=1)
    expansion_turn_id: str = Field(min_length=1)
    goal_revision_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    goal_definition_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state_snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    project_map_revision_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expanded_plan_revision_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expanded_plan_activation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    registered_source_content_digests: dict[str, str]

    @field_validator("registered_source_content_digests")
    @classmethod
    def registered_digests_are_canonical(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key.startswith("project:")
            or len(digest) != 71
            or not digest.startswith("sha256:")
            for key, digest in value.items()
        ):
            raise ValueError("등록 원문 digest 형식이 잘못됐습니다.")
        return value

    @property
    def source_binding_digest(self) -> str:
        return sha256_digest(self)


class GenerationAssessmentProvenance(EngineModel):
    format: Literal["independent_manual_review"] = _PROVENANCE_FORMAT
    author_ref: str = Field(min_length=1, max_length=500)
    review_notes_path: str = Field(min_length=1, max_length=2000)
    review_notes_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    criteria_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    independence_scope: Literal[
        "operator_attestation_not_cryptographic_identity_proof"
    ] = _INDEPENDENCE_SCOPE
    provider_accounting_scope: Literal[
        "manual_review_separate_from_harness_provider_receipts"
    ] = _ACCOUNTING_SCOPE

    @field_validator("review_notes_path")
    @classmethod
    def notes_path_is_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("review notes는 같은 run 내부의 상대 경로여야 합니다.")
        return path.as_posix()


def _read_preflight_binding(run_root: Path) -> tuple[dict[str, Any], str]:
    preflight_path = run_root / "preflight.json"
    try:
        import json

        document = json.loads(preflight_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise GenerationReviewBindingError("GENERATION_REVIEW_PREFLIGHT_INVALID") from error
    if not isinstance(document, dict) or not isinstance(document.get("lock_digest"), str):
        raise GenerationReviewBindingError("GENERATION_REVIEW_PREFLIGHT_LOCK_MISSING")
    body = dict(document)
    lock_digest = body.pop("lock_digest")
    if lock_digest != sha256_digest(body):
        raise GenerationReviewBindingError("GENERATION_REVIEW_PREFLIGHT_LOCK_MISMATCH")
    if body.get("generation_review_criteria") != generation_review_criteria_binding():
        raise GenerationReviewBindingError("GENERATION_REVIEW_CRITERIA_NOT_PREFLIGHT_BOUND")
    if (
        body.get("inspection_provider_contract") != "plan-inspection-v2"
        or not isinstance(body.get("diagnostic_policy_input"), dict)
        or not isinstance(body.get("workspace_binding"), dict)
        or body.get("role_threads_ephemeral") is not False
    ):
        raise GenerationReviewBindingError("GENERATION_REVIEW_PORTABLE_V2_POLICY_REQUIRED")
    run_binding_digest = sha256_digest(
        {"run_root": str(run_root.resolve()), "preflight_lock_digest": lock_digest}
    )
    return body, run_binding_digest


def _verify_registered_sources(
    request: RoleCallRequest, project_map: ProjectMapRevision, *, run_root: Path
) -> dict[str, str]:
    catalog = request.payload.get("inspection_source_catalog")
    if not isinstance(catalog, dict):
        raise GenerationReviewBindingError("GENERATION_REVIEW_SOURCE_CATALOG_MISSING")
    entries = {item.entry_id: item for item in project_map.entries}
    expected_ids = {
        item.entry_id for item in project_map.entries
        if item.kind.value in {"instruction", "reference"}
    }
    observed: dict[str, str] = {}
    for entry_id in sorted(expected_ids):
        item = catalog.get(f"project:{entry_id}")
        entry = entries[entry_id]
        if not isinstance(item, dict) or item.get("content_digest") != entry.content_digest:
            raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_BINDING_MISMATCH")
        content = item.get("content")
        if not isinstance(content, str) or sha256_bytes(content.encode("utf-8")) != entry.content_digest:
            raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_CONTENT_MISMATCH")
        entry_path = Path(entry.path)
        # portable materialization은 원래 등록된 외부 reference만 run/project-references로
        # 절대 경로로 옮긴다. 다른 entry의 root escape는 계속 거부한다.
        if entry_path.is_absolute():
            if (entry.kind.value != "reference"
                    or not entry_path.resolve().is_relative_to(run_root / "project-references")):
                raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_PATH_ESCAPE")
            path = entry_path.resolve()
        else:
            path = (Path(project_map.root) / entry_path).resolve()
        try:
            if (not entry_path.is_absolute()
                    and not path.is_relative_to(Path(project_map.root).resolve())):
                raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_PATH_ESCAPE")
            if sha256_bytes(path.read_bytes()) != entry.content_digest:
                raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_CHANGED")
        except OSError as error:
            raise GenerationReviewBindingError("GENERATION_REVIEW_REGISTERED_SOURCE_UNREADABLE") from error
        observed[f"project:{entry_id}"] = entry.content_digest
    return observed


def bind_generation_review_source(
    *,
    run_root: Path | str,
    expansion_request: RoleCallRequest,
    expansion_result: RoleCallResult,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
    expanded_plan: PlanContractRevision,
) -> GenerationReviewSourceBinding:
    root = Path(run_root).resolve()
    _, run_binding_digest = _read_preflight_binding(root)
    request = RoleCallRequest.model_validate(expansion_request.model_dump(mode="python"))
    result = RoleCallResult.model_validate(expansion_result.model_dump(mode="python"))
    if request.role != "plan_expander" or result.receipt.role != "plan_expander":
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOT_PLAN_EXPANSION")
    try:
        verify_role_receipt(request, result)
        PlanExpansionEnvelopeV2.model_validate(result.payload)
    except Exception as error:
        raise GenerationReviewBindingError("GENERATION_REVIEW_EXPANSION_RECEIPT_INVALID") from error
    receipt = result.receipt
    usage = (
        receipt.input_tokens, receipt.cached_input_tokens,
        receipt.output_tokens, receipt.reasoning_tokens,
    )
    if (
        receipt.status != "succeeded"
        or receipt.permission_profile != REQUIRED_PERMISSION_PROFILE
        or receipt.approval_policy != REQUIRED_APPROVAL_POLICY
        or receipt.thread_id is None
        or len(receipt.turn_ids) != 1
        or not receipt.usage_available
        or any(value is None for value in usage)
    ):
        raise GenerationReviewBindingError("GENERATION_REVIEW_EXPANSION_OBSERVATION_INCOMPLETE")
    if (
        request.payload.get("goal") != goal.definition.model_dump(mode="json")
        or request.payload.get("state") != state.model_dump(mode="json")
        or request.payload.get("project_map") != compact_project_map(project_map)
        or "inspection_citation_catalog" not in request.payload
        or "task_result_field_semantics" not in request.payload
    ):
        raise GenerationReviewBindingError("GENERATION_REVIEW_EXPANSION_INPUT_MISMATCH")
    definition = expanded_plan.definition
    skeleton = request.payload.get("skeleton")
    try:
        skeleton_digest = sha256_digest(PlanSkeletonCandidate.model_validate(skeleton))
    except Exception as error:
        raise GenerationReviewBindingError("GENERATION_REVIEW_SKELETON_INPUT_INVALID") from error
    if (
        not isinstance(skeleton, dict)
        or definition.goal_contract_digest != goal.definition_digest
        or state.goal_contract_digest != goal.definition_digest
        or definition.base_state_snapshot_digest != state.snapshot_digest
        or definition.project_map_digest != project_map.revision_digest
        or definition.source_skeleton_digest != skeleton_digest
        or len({goal.definition.project_id, state.project_id, project_map.project_id,
                definition.project_id}) != 1
    ):
        raise GenerationReviewBindingError("GENERATION_REVIEW_EXPANDED_PLAN_INPUT_MISMATCH")
    registered = _verify_registered_sources(request, project_map, run_root=root)
    return GenerationReviewSourceBinding(
        run_binding_digest=run_binding_digest,
        criteria_digest=generation_review_criteria_digest(),
        expansion_request_digest=request.request_digest,
        expansion_result_digest=sha256_digest(result),
        expansion_receipt_digest=sha256_digest(receipt),
        expansion_call_id=receipt.call_id,
        expansion_thread_id=receipt.thread_id,
        expansion_turn_id=receipt.turn_ids[0],
        goal_revision_digest=goal.revision_digest,
        goal_definition_digest=goal.definition_digest,
        state_snapshot_digest=state.snapshot_digest,
        project_map_revision_digest=project_map.revision_digest,
        expanded_plan_revision_digest=sha256_digest(expanded_plan),
        expanded_plan_activation_digest=expanded_plan.activation_digest,
        registered_source_content_digests=registered,
    )


def _author_identity(value: str) -> str:
    for prefix in ("codex-thread:", "thread:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _notes_file(run_root: Path, relative_path: str) -> Path:
    candidate = (run_root / relative_path).resolve()
    if not candidate.is_relative_to(run_root.resolve()):
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_PATH_ESCAPE")
    if not candidate.is_file():
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_MISSING")
    return candidate


def build_generation_assessment_provenance(
    *,
    run_root: Path | str,
    source_binding: GenerationReviewSourceBinding,
    author_ref: str,
    review_notes_path: Path | str,
) -> GenerationAssessmentProvenance:
    root = Path(run_root).resolve()
    notes = Path(review_notes_path)
    resolved = notes.resolve() if notes.is_absolute() else (root / notes).resolve()
    if not resolved.is_relative_to(root):
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_PATH_ESCAPE")
    try:
        relative = resolved.relative_to(root).as_posix()
        body = resolved.read_bytes()
    except OSError as error:
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_UNREADABLE") from error
    if not body.strip():
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_EMPTY")
    if _author_identity(author_ref) == source_binding.expansion_thread_id:
        raise GenerationReviewBindingError("GENERATION_REVIEW_EXPANDER_CANNOT_ATTEST_INDEPENDENCE")
    return GenerationAssessmentProvenance(
        author_ref=author_ref,
        review_notes_path=relative,
        review_notes_digest=sha256_bytes(body),
        source_binding_digest=source_binding.source_binding_digest,
        criteria_digest=source_binding.criteria_digest,
    )


def verify_generation_assessment(
    assessment: dict[str, Any],
    *,
    run_root: Path | str,
    expansion_request: RoleCallRequest,
    expansion_result: RoleCallResult,
    goal: GoalContractRevision,
    state: StateSnapshot,
    project_map: ProjectMapRevision,
    expanded_plan: PlanContractRevision,
    expected_source_binding: GenerationReviewSourceBinding | None = None,
) -> GenerationAssessmentProvenance:
    actual = bind_generation_review_source(
        run_root=run_root,
        expansion_request=expansion_request,
        expansion_result=expansion_result,
        goal=goal,
        state=state,
        project_map=project_map,
        expanded_plan=expanded_plan,
    )
    if expected_source_binding is not None and actual != expected_source_binding:
        raise GenerationReviewBindingError("GENERATION_REVIEW_SOURCE_BINDING_CHANGED")
    try:
        provenance = GenerationAssessmentProvenance.model_validate(
            assessment["assessment_provenance"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise GenerationReviewBindingError("GENERATION_REVIEW_PROVENANCE_INVALID") from error
    if (
        provenance.source_binding_digest != actual.source_binding_digest
        or provenance.criteria_digest != generation_review_criteria_digest()
        or _author_identity(provenance.author_ref) == actual.expansion_thread_id
    ):
        raise GenerationReviewBindingError("GENERATION_REVIEW_PROVENANCE_BINDING_MISMATCH")
    notes = _notes_file(Path(run_root).resolve(), provenance.review_notes_path)
    if sha256_bytes(notes.read_bytes()) != provenance.review_notes_digest:
        raise GenerationReviewBindingError("GENERATION_REVIEW_NOTES_DIGEST_MISMATCH")
    return provenance
