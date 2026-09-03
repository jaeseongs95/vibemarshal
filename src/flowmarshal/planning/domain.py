from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..canonical import sha256_bytes, sha256_digest
from ..core.domain import PlanDraft


PLANNER_SCHEMA_VERSION = "1.0"


class PlanningModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class PlannerContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RequirementSource(StrEnum):
    USER = "user"
    PROJECT_INSTRUCTION = "project_instruction"
    REFERENCE = "reference"


class RequirementPriority(StrEnum):
    MUST = "must"
    SHOULD = "should"


class ContextSourceKind(StrEnum):
    PROJECT_INSTRUCTIONS = "project_instructions"
    PROJECT_FILE = "project_file"
    REFERENCE = "reference"
    EXTERNAL_REFERENCE = "external_reference"


class PlanIssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class CandidateReadiness(StrEnum):
    INVALID = "invalid"
    NEEDS_USER_INPUT = "needs_user_input"
    READY_FOR_ASSIGNMENT = "ready_for_assignment"


class PlanIssueCode(StrEnum):
    PROJECT_MISMATCH = "PROJECT_MISMATCH"
    PARENT_REVISION_MISMATCH = "PARENT_REVISION_MISMATCH"
    REQUEST_DIGEST_MISMATCH = "REQUEST_DIGEST_MISMATCH"
    REQUIREMENT_COVERAGE_MISSING = "REQUIREMENT_COVERAGE_MISSING"
    REQUIREMENT_COVERAGE_UNKNOWN = "REQUIREMENT_COVERAGE_UNKNOWN"
    WORK_ITEM_WITHOUT_REQUIREMENT = "WORK_ITEM_WITHOUT_REQUIREMENT"
    WORK_ITEM_TOO_LARGE = "WORK_ITEM_TOO_LARGE"
    PLAN_OVER_FRAGMENTED = "PLAN_OVER_FRAGMENTED"
    REQUIREMENT_OVER_FRAGMENTED = "REQUIREMENT_OVER_FRAGMENTED"
    WORK_ITEM_OBJECTIVE_TOO_VAGUE = "WORK_ITEM_OBJECTIVE_TOO_VAGUE"
    CHANGE_CONFLICT_UNORDERED = "CHANGE_CONFLICT_UNORDERED"
    CONTEXT_SOURCE_UNKNOWN = "CONTEXT_SOURCE_UNKNOWN"
    REQUIRED_CONTEXT_MISSING = "REQUIRED_CONTEXT_MISSING"
    VALIDATION_CAPABILITY_MISSING = "VALIDATION_CAPABILITY_MISSING"
    VALIDATION_CAPABILITY_UNKNOWN = "VALIDATION_CAPABILITY_UNKNOWN"
    VALIDATION_TYPE_MISMATCH = "VALIDATION_TYPE_MISMATCH"
    PREMATURE_ASSIGNMENT = "PREMATURE_ASSIGNMENT"
    CLARIFICATION_REQUIREMENT_UNKNOWN = "CLARIFICATION_REQUIREMENT_UNKNOWN"


class ContextSourceSpec(PlanningModel):
    # content의 앞뒤 공백과 줄바꿈도 digest 대상이므로 전역 문자열 정리를 끈다.
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    kind: ContextSourceKind
    path: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=500_000)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    required_for_all_work_items: bool = False
    truncated: bool = False

    @field_validator("path", "purpose")
    @classmethod
    def normalize_descriptive_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("ContextSource path와 purpose는 비어 있을 수 없습니다.")
        return normalized

    @model_validator(mode="after")
    def digest_matches_content(self) -> "ContextSourceSpec":
        observed = sha256_bytes(self.content.encode("utf-8"))
        if observed != self.content_digest:
            raise ValueError("ContextSource content_digest가 실제 내용과 다릅니다.")
        return self

    @classmethod
    def from_text(
        cls,
        *,
        source_id: str,
        kind: ContextSourceKind,
        path: str,
        purpose: str,
        content: str,
        required_for_all_work_items: bool = False,
        truncated: bool = False,
    ) -> "ContextSourceSpec":
        return cls(
            source_id=source_id,
            kind=kind,
            path=path,
            purpose=purpose,
            content=content,
            content_digest=sha256_bytes(content.encode("utf-8")),
            required_for_all_work_items=required_for_all_work_items,
            truncated=truncated,
        )


class RequirementSpec(PlanningModel):
    requirement_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    statement: str = Field(min_length=1, max_length=5000)
    source: RequirementSource
    priority: RequirementPriority = RequirementPriority.MUST
    source_refs: tuple[str, ...] = ()

    @field_validator("source_refs")
    @classmethod
    def unique_source_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requirement source 참조가 중복됐습니다.")
        return value


class ValidationCapability(PlanningModel):
    capability_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    check_type: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    deterministic: bool = True
    configuration: dict[str, Any] = Field(default_factory=dict)


class PlanningLimits(PlanningModel):
    max_work_items: int = Field(default=30, ge=1, le=200)
    max_requirements_per_work_item: int = Field(default=5, ge=1, le=50)
    max_work_items_per_requirement: int = Field(default=4, ge=1, le=20)
    max_change_targets_per_work_item: int = Field(default=12, ge=1, le=100)
    min_objective_characters: int = Field(default=12, ge=5, le=200)


class RequestSpec(PlanningModel):
    schema_version: Literal[PLANNER_SCHEMA_VERSION] = PLANNER_SCHEMA_VERSION
    project_id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    project_name: str = Field(min_length=1, max_length=120)
    project_root: str = Field(min_length=3, max_length=2000)
    project_description: str = Field(min_length=1, max_length=5000)
    user_request: str = Field(min_length=1, max_length=50_000)
    request_summary: str = Field(min_length=1, max_length=5000)
    parent_revision_id: str | None = Field(
        default=None, pattern=r"^revision_[0-9a-f]{32}$"
    )
    requirements: tuple[RequirementSpec, ...] = Field(min_length=1)
    context_sources: tuple[ContextSourceSpec, ...] = Field(min_length=1)
    available_validations: tuple[ValidationCapability, ...] = Field(min_length=1)
    product_capabilities: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    planning_limits: PlanningLimits = Field(default_factory=PlanningLimits)

    @field_validator("product_capabilities", "out_of_scope")
    @classmethod
    def unique_text_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("RequestSpec 항목이 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def identifiers_and_references_are_valid(self) -> "RequestSpec":
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement ID가 중복됐습니다.")
        source_ids = [item.source_id for item in self.context_sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("ContextSource ID가 중복됐습니다.")
        source_paths = [item.path.casefold() for item in self.context_sources]
        if len(source_paths) != len(set(source_paths)):
            raise ValueError("ContextSource path가 중복됐습니다.")
        known_sources = set(source_ids)
        for requirement in self.requirements:
            missing = set(requirement.source_refs) - known_sources
            if missing:
                raise ValueError(
                    f"requirement가 알 수 없는 ContextSource를 참조합니다: {sorted(missing)}"
                )
            if (
                requirement.source is not RequirementSource.USER
                and not requirement.source_refs
            ):
                raise ValueError("프로젝트·참고자료 requirement에는 source_refs가 필요합니다.")
        validation_ids = [item.capability_id for item in self.available_validations]
        if len(validation_ids) != len(set(validation_ids)):
            raise ValueError("validation capability ID가 중복됐습니다.")
        return self

    @property
    def canonical_digest(self) -> str:
        return sha256_digest(self)


class PlanValidationIssue(PlanningModel):
    code: PlanIssueCode
    severity: PlanIssueSeverity
    message: str = Field(min_length=1, max_length=5000)
    requirement_ids: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


class CoverageSummary(PlanningModel):
    requirement_count: int = Field(ge=0)
    covered_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    clarification_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)
    work_item_count: int = Field(ge=0)


class PlanValidationReport(PlanningModel):
    request_spec_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    valid: bool
    readiness: CandidateReadiness
    coverage: CoverageSummary
    issues: tuple[PlanValidationIssue, ...] = ()

    @property
    def report_digest(self) -> str:
        return sha256_digest(self)


class PlannerGenerationRequest(PlanningModel):
    schema_version: Literal[PLANNER_SCHEMA_VERSION] = PLANNER_SCHEMA_VERSION
    request_spec: RequestSpec
    instructions: tuple[str, ...] = Field(min_length=1)
    output_schema: dict[str, Any]

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)


class PlannerGenerationResult(PlanningModel):
    payload: dict[str, Any]
    receipt: dict[str, Any] = Field(default_factory=dict)


class PlannerOutcome(PlanningModel):
    draft: PlanDraft
    validation: PlanValidationReport
    generation_request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_receipt: dict[str, Any] = Field(default_factory=dict)


class PlanValidationError(PlannerContractError):
    def __init__(self, report: PlanValidationReport) -> None:
        codes = ", ".join(issue.code.value for issue in report.issues)
        super().__init__("PLAN_CANDIDATE_INVALID", f"PlanDraft 검증 실패: {codes}")
        self.report = report
