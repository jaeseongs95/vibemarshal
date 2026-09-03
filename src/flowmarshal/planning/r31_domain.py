from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..canonical import sha256_bytes, sha256_digest
from ..core.domain import PlanDraft
from .domain import RequestSpec


R31_SCHEMA_VERSION = "3.1"
R31_SCORE_POLICY_ID = "balanced-mvp"
R31_SCORE_POLICY_VERSION = "v0"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$"
_FIELD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"


class R31Model(BaseModel):
    """R3.1 planning artifact의 공통 불변 모델."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
    )


def _unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 항목이 중복됐습니다.")
    return values


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact 시각에는 timezone이 필요합니다.")
    return value


def _normalized_local_path(value: str) -> str:
    """동일 로컬 파일을 표현하는 상대·절대/대소문자 차이를 제거한다."""

    return str(Path(value).resolve(strict=False)).casefold()


class LifecycleStage(StrEnum):
    PROTOTYPE = "prototype"
    GROWTH = "growth"
    MATURE = "mature"
    LEGACY = "legacy"


class Criticality(StrEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class CompatibilityPolicy(StrEnum):
    FLEXIBLE = "flexible"
    PRESERVE = "preserve"
    STRICT = "strict"


class RiskTolerance(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    EXPLORATORY = "exploratory"


class ProfileFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


class ProfileRevisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class ProfileSection(R31Model):
    source_refs: tuple[str, ...] = ()
    source_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    freshness: ProfileFreshness
    unknown: bool = False
    unknown_reasons: tuple[str, ...] = ()

    @field_validator("source_refs", "unknown_reasons")
    @classmethod
    def entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "profile section")

    @model_validator(mode="after")
    def unknown_shape_is_consistent(self) -> "ProfileSection":
        if self.unknown:
            if not self.unknown_reasons:
                raise ValueError("unknown profile section에는 이유가 필요합니다.")
            if self.freshness is not ProfileFreshness.UNKNOWN:
                raise ValueError("unknown profile section의 freshness는 unknown이어야 합니다.")
        elif self.unknown_reasons:
            raise ValueError("확정된 profile section에는 unknown_reasons를 둘 수 없습니다.")
        if not self.unknown and (not self.source_refs or self.source_digest is None):
            raise ValueError("확정된 profile section에는 source와 digest가 필요합니다.")
        return self


class ProjectProfileDefinition(R31Model):
    schema_version: Literal[R31_SCHEMA_VERSION] = R31_SCHEMA_VERSION
    product_goal: str = Field(min_length=1, max_length=5000)
    lifecycle_stage: LifecycleStage
    criticality: Criticality
    compatibility_policy: CompatibilityPolicy
    default_risk_tolerance: RiskTolerance
    architecture: ProfileSection
    validation: ProfileSection
    runtime: ProfileSection
    risk: ProfileSection
    compatibility: ProfileSection

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class ProjectProfileRevision(R31Model):
    profile_revision_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    project_id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    definition: ProjectProfileDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: ProfileRevisionStatus
    supersedes_profile_revision_id: str | None = Field(
        default=None, pattern=_ARTIFACT_ID_PATTERN
    )
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def definition_is_bound(self) -> "ProjectProfileRevision":
        if self.definition_digest != self.definition.definition_digest:
            raise ValueError("profile definition_digest가 실제 definition과 다릅니다.")
        if self.supersedes_profile_revision_id == self.profile_revision_id:
            raise ValueError("profile revision은 자신을 supersede할 수 없습니다.")
        return self

    @property
    def revision_digest(self) -> str:
        return sha256_digest(self)


class MissionPrimary(StrEnum):
    NEW_BUILD = "new_build"
    FEATURE_EXTENSION = "feature_extension"
    LEGACY_REFACTOR = "legacy_refactor"
    BUGFIX_STABILIZATION = "bugfix_stabilization"
    MIGRATION_MODERNIZATION = "migration_modernization"
    ANALYSIS_AUDIT = "analysis_audit"


class MutationPolicy(StrEnum):
    READ_ONLY = "read_only"
    MINIMAL_CHANGE = "minimal_change"
    SCOPED_CHANGE = "scoped_change"
    STRUCTURAL_CHANGE = "structural_change"
    MIGRATION_CHANGE = "migration_change"


class BehaviorPreservation(StrEnum):
    PRESERVE_OBSERVED_BEHAVIOR = "preserve_observed_behavior"
    PRESERVE_PUBLIC_CONTRACTS = "preserve_public_contracts"
    ALLOW_EXPLICIT_BREAKING_CHANGES = "allow_explicit_breaking_changes"
    NOT_APPLICABLE = "not_applicable"


class MissionSelectedBy(StrEnum):
    USER = "user"
    INFERRED = "inferred"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskTag(StrEnum):
    EXISTING_BEHAVIOR = "existing_behavior"
    PUBLIC_CONTRACT_CHANGE = "public_contract_change"
    PERSISTENT_STATE_CHANGE = "persistent_state_change"
    DESTRUCTIVE_EFFECT = "destructive_effect"
    EXTERNAL_EFFECT = "external_effect"
    SHARED_CONCURRENCY = "shared_concurrency"
    SECURITY_SENSITIVE = "security_sensitive"
    SCALE_OR_SLO = "scale_or_slo"
    HUMAN_CHECKPOINT = "human_checkpoint"


class PlanningMissionDefinition(R31Model):
    schema_version: Literal[R31_SCHEMA_VERSION] = R31_SCHEMA_VERSION
    primary: MissionPrimary
    secondary: MissionPrimary | None = None
    observable_outcome: str = Field(min_length=1, max_length=5000)
    mutation_policy: MutationPolicy
    behavior_preservation: BehaviorPreservation
    allowed_external_effects: tuple[str, ...] = ()
    forbidden_scopes: tuple[str, ...] = ()
    risk_tags: tuple[RiskTag, ...] = ()
    selected_by: MissionSelectedBy
    confidence: ConfidenceLevel
    mission_policy_id: str = Field(pattern=_FIELD_ID_PATTERN)
    mission_policy_version: str = Field(min_length=1, max_length=80)

    @field_validator("allowed_external_effects", "forbidden_scopes")
    @classmethod
    def text_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "mission")

    @field_validator("risk_tags")
    @classmethod
    def risk_tags_are_unique(cls, value: tuple[RiskTag, ...]) -> tuple[RiskTag, ...]:
        if len(value) != len(set(value)):
            raise ValueError("risk tag가 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def mission_shape_is_valid(self) -> "PlanningMissionDefinition":
        if self.secondary is self.primary:
            raise ValueError("secondary Mission은 primary와 달라야 합니다.")
        if (
            self.primary is MissionPrimary.ANALYSIS_AUDIT
            and self.mutation_policy is not MutationPolicy.READ_ONLY
        ):
            raise ValueError("analysis_audit Mission은 반드시 read_only여야 합니다.")
        return self

    @property
    def mission_digest(self) -> str:
        return sha256_digest(self)


class MissionResolutionHintKind(StrEnum):
    """모델 또는 UI가 Resolver에 전달할 수 있는 제한된 판단 종류."""

    UNAMBIGUOUS = "unambiguous"
    OUTCOME_AMBIGUOUS = "outcome_ambiguous"
    CONFLICT = "conflict"


class MissionResolutionHint(R31Model):
    kind: MissionResolutionHintKind
    recommended: PlanningMissionDefinition | None = None
    options: tuple[PlanningMissionDefinition, ...] = ()
    reasons: tuple[str, ...] = ()
    question: str | None = Field(default=None, max_length=2000)


class MissionResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    NEEDS_USER_INPUT = "needs_user_input"
    BLOCKED = "blocked"


class ProfileOverrideReceipt(R31Model):
    field_name: str = Field(pattern=_FIELD_ID_PATTERN)
    profile_value: str = Field(min_length=1, max_length=2000)
    effective_value: str = Field(min_length=1, max_length=2000)
    winning_authority: Literal["explicit_request", "user_mission"]
    reason: str = Field(min_length=1, max_length=2000)


class MissionSelectionReceipt(R31Model):
    request_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    raw_request_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_revision_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: MissionResolutionStatus
    mission: PlanningMissionDefinition | None = None
    options: tuple[PlanningMissionDefinition, ...] = ()
    question: str | None = Field(default=None, max_length=2000)
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    override_receipts: tuple[ProfileOverrideReceipt, ...] = ()

    @field_validator("conflicts", "warnings")
    @classmethod
    def conflicts_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "mission conflict")

    @model_validator(mode="after")
    def resolution_shape_is_valid(self) -> "MissionSelectionReceipt":
        if self.status is MissionResolutionStatus.RESOLVED:
            if self.mission is None:
                raise ValueError("resolved Mission receipt에는 mission이 필요합니다.")
            if self.question is not None or self.conflicts:
                raise ValueError("resolved Mission receipt에는 미결 질문·충돌을 둘 수 없습니다.")
            if self.options and self.mission.primary not in {
                option.primary for option in self.options
            }:
                raise ValueError("선택 Mission이 제시된 options에 없습니다.")
        elif self.status is MissionResolutionStatus.NEEDS_USER_INPUT:
            if self.mission is not None or self.question is None:
                raise ValueError("needs_user_input에는 question만 있고 확정 mission은 없어야 합니다.")
            if not 2 <= len(self.options) <= 3:
                raise ValueError("Mission 선택지는 2~3개여야 합니다.")
        else:
            if self.mission is not None or not self.conflicts:
                raise ValueError("blocked Mission receipt에는 충돌 근거가 필요합니다.")
        primaries = [option.primary for option in self.options]
        if len(primaries) != len(set(primaries)):
            raise ValueError("Mission option의 primary가 중복됐습니다.")
        return self

    @property
    def mission_resolution_digest(self) -> str:
        return sha256_digest(self)


class ExplicitRequestConstraint(R31Model):
    constraint_id: str = Field(pattern=_FIELD_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    source_ref: str = Field(min_length=1, max_length=500)


class EffectivePlanningPolicy(R31Model):
    schema_version: Literal[R31_SCHEMA_VERSION] = R31_SCHEMA_VERSION
    policy_id: str = Field(pattern=_FIELD_ID_PATTERN)
    policy_version: str = Field(min_length=1, max_length=80)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_resolution_digest: str = Field(pattern=_DIGEST_PATTERN)
    lifecycle_stage: LifecycleStage
    criticality: Criticality
    compatibility_policy: CompatibilityPolicy
    risk_tolerance: RiskTolerance
    mutation_policy: MutationPolicy
    behavior_preservation: BehaviorPreservation
    allowed_external_effects: tuple[str, ...] = ()
    forbidden_scopes: tuple[str, ...] = ()
    explicit_request_constraints: tuple[ExplicitRequestConstraint, ...] = ()
    override_receipts: tuple[ProfileOverrideReceipt, ...] = ()

    @field_validator("allowed_external_effects", "forbidden_scopes")
    @classmethod
    def policy_text_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "effective policy")

    @model_validator(mode="after")
    def policy_identifiers_are_unique(self) -> "EffectivePlanningPolicy":
        identifiers = [item.constraint_id for item in self.explicit_request_constraints]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("explicit request constraint ID가 중복됐습니다.")
        return self

    @property
    def effective_policy_digest(self) -> str:
        return sha256_digest(self)


class SnapshotEntryKind(StrEnum):
    FILE = "file"
    MANIFEST = "manifest"
    SCHEMA = "schema"
    TEST = "test"
    REFERENCE = "reference"


class ProjectStateEntry(R31Model):
    entry_id: str = Field(pattern=_FIELD_ID_PATTERN)
    kind: SnapshotEntryKind
    path: str = Field(min_length=1, max_length=2000)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    relevance: str = Field(min_length=1, max_length=2000)


class ProjectStateSnapshot(R31Model):
    snapshot_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    project_id: str = Field(pattern=r"^project_[0-9a-f]{32}$")
    mission_resolution_digest: str = Field(pattern=_DIGEST_PATTERN)
    entries: tuple[ProjectStateEntry, ...] = ()
    unknowns: tuple[str, ...] = ()
    captured_at: datetime

    _captured_at_is_aware = field_validator("captured_at")(_aware)

    @field_validator("unknowns")
    @classmethod
    def unknowns_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "project snapshot unknown")

    @model_validator(mode="after")
    def entries_are_unique(self) -> "ProjectStateSnapshot":
        ids = [entry.entry_id for entry in self.entries]
        paths = [entry.path.casefold() for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("ProjectState entry ID가 중복됐습니다.")
        if len(paths) != len(set(paths)):
            raise ValueError("ProjectState path가 중복됐습니다.")
        return self

    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(self)

    @property
    def semantic_digest(self) -> str:
        """capture 시각·artifact ID를 제외한 프로젝트 상태 의미 digest."""

        entries = sorted(
            (entry.model_dump(mode="json") for entry in self.entries),
            key=lambda item: (
                item["path"].casefold(),
                item["kind"],
                item["entry_id"],
            ),
        )
        return sha256_digest(
            {
                "project_id": self.project_id,
                "mission_resolution_digest": self.mission_resolution_digest,
                "entries": entries,
                "unknowns": sorted(self.unknowns),
            }
        )


class ProfileSectionName(StrEnum):
    ARCHITECTURE = "architecture"
    VALIDATION = "validation"
    RUNTIME = "runtime"
    RISK = "risk"
    COMPATIBILITY = "compatibility"


class SelectedProfileSection(R31Model):
    """요구 추출 역할에 공개할 ProjectProfile의 최소 section."""

    name: ProfileSectionName
    section: ProfileSection


class RequirementAnalysisContext(R31Model):
    """숨은 대화 문맥 없이 요구 추출 draft를 만들 수 있는 입력 snapshot."""

    raw_request: str = Field(min_length=1, max_length=50_000)
    raw_request_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_request_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission: PlanningMissionDefinition
    mission_resolution_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_options: tuple[PlanningMissionDefinition, ...] = ()
    mission_warnings: tuple[str, ...] = ()
    mission_override_receipts: tuple[ProfileOverrideReceipt, ...] = ()
    profile_revision_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_sections: tuple[SelectedProfileSection, ...] = Field(min_length=1)
    project_snapshot: ProjectStateSnapshot

    @model_validator(mode="after")
    def semantic_sources_are_bound(self) -> "RequirementAnalysisContext":
        if self.raw_request_digest != sha256_bytes(self.raw_request.encode("utf-8")):
            raise ValueError("요구 분석 context의 raw request digest가 실제 원문과 다릅니다.")
        if (
            self.project_snapshot.mission_resolution_digest
            != self.mission_resolution_digest
        ):
            raise ValueError(
                "요구 분석 전 ProjectStateSnapshot은 확정 Mission resolution에 결속돼야 합니다."
            )
        source_mission_receipt = MissionSelectionReceipt(
            request_spec_digest=self.source_request_spec_digest,
            raw_request_digest=self.raw_request_digest,
            profile_revision_id=self.profile_revision_id,
            profile_definition_digest=self.profile_definition_digest,
            status=MissionResolutionStatus.RESOLVED,
            mission=self.mission,
            options=self.mission_options,
            warnings=self.mission_warnings,
            override_receipts=self.mission_override_receipts,
        )
        if source_mission_receipt.mission_resolution_digest != self.mission_resolution_digest:
            raise ValueError("요구 분석 context가 원래 Mission selection receipt와 다릅니다.")
        names = [item.name for item in self.profile_sections]
        if len(names) != len(set(names)):
            raise ValueError("요구 분석 context의 profile section이 중복됐습니다.")
        return self

    @property
    def context_digest(self) -> str:
        return sha256_digest(self)


class RequirementKind(StrEnum):
    REQUIREMENT = "requirement"
    CONSTRAINT = "constraint"
    EXCLUSION = "exclusion"
    OBSERVABLE_OUTCOME = "observable_outcome"


class RawRequestTrace(R31Model):
    trace_id: str = Field(pattern=_FIELD_ID_PATTERN)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    excerpt: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def range_is_valid(self) -> "RawRequestTrace":
        if self.end_offset <= self.start_offset:
            raise ValueError("raw request trace의 끝 offset은 시작보다 커야 합니다.")
        return self


class ExtractedPlanningRequirement(R31Model):
    requirement_id: str = Field(pattern=_FIELD_ID_PATTERN)
    kind: RequirementKind
    statement: str = Field(min_length=1, max_length=5000)
    mandatory: bool
    trace_refs: tuple[str, ...] = ()
    profile_source_refs: tuple[str, ...] = ()
    snapshot_entry_refs: tuple[str, ...] = ()
    mission_grounded: bool = False

    @field_validator("trace_refs", "profile_source_refs", "snapshot_entry_refs")
    @classmethod
    def requirement_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "requirement trace")

    @model_validator(mode="after")
    def requirement_has_declared_grounding(self) -> "ExtractedPlanningRequirement":
        if not (
            self.trace_refs
            or self.profile_source_refs
            or self.snapshot_entry_refs
            or self.mission_grounded
        ):
            raise ValueError("추출 requirement에는 명시적인 근거가 필요합니다.")
        if self.mission_grounded and self.kind in {
            RequirementKind.REQUIREMENT,
            RequirementKind.CONSTRAINT,
        }:
            raise ValueError(
                "Mission만을 근거로 새 requirement나 constraint를 발명할 수 없습니다."
            )
        return self


class RequirementAnalysisNote(R31Model):
    """강제 요구사항으로 승격하지 않는 가정 또는 구현 제안."""

    note_id: str = Field(pattern=_FIELD_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    trace_refs: tuple[str, ...] = ()
    profile_source_refs: tuple[str, ...] = ()
    snapshot_entry_refs: tuple[str, ...] = ()
    requires_confirmation: bool = False

    @field_validator("trace_refs", "profile_source_refs", "snapshot_entry_refs")
    @classmethod
    def note_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "analysis note reference")


def _validate_extraction_graph(
    traces: tuple[RawRequestTrace, ...],
    requirements: tuple[ExtractedPlanningRequirement, ...],
    assumptions: tuple[RequirementAnalysisNote, ...],
    implementation_suggestions: tuple[RequirementAnalysisNote, ...],
) -> None:
    trace_ids = [trace.trace_id for trace in traces]
    requirement_ids = [item.requirement_id for item in requirements]
    note_ids = [item.note_id for item in (*assumptions, *implementation_suggestions)]
    if len(trace_ids) != len(set(trace_ids)):
        raise ValueError("raw request trace ID가 중복됐습니다.")
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError("추출 requirement ID가 중복됐습니다.")
    if len(note_ids) != len(set(note_ids)):
        raise ValueError("요구 분석 note ID가 중복됐습니다.")
    known = set(trace_ids)
    referenced: set[str] = set()
    for item in (*requirements, *assumptions, *implementation_suggestions):
        missing = set(item.trace_refs) - known
        if missing:
            raise ValueError(f"추출 항목이 알 수 없는 trace를 참조합니다: {sorted(missing)}")
        referenced.update(item.trace_refs)
    if referenced != known:
        raise ValueError("모든 raw request trace는 추출 항목에서 역참조되어야 합니다.")


class RequirementExtractionDraft(R31Model):
    """모델 역할이 반환하고 결정적 조립기가 검증하는 typed extraction."""

    raw_request_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    project_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    request_summary: str = Field(min_length=1, max_length=5000)
    traces: tuple[RawRequestTrace, ...] = Field(min_length=1)
    requirements: tuple[ExtractedPlanningRequirement, ...] = Field(min_length=1)
    assumptions: tuple[RequirementAnalysisNote, ...] = ()
    implementation_suggestions: tuple[RequirementAnalysisNote, ...] = ()
    unresolved_items: tuple[str, ...] = ()

    @field_validator("unresolved_items")
    @classmethod
    def draft_unresolved_items_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "unresolved requirement")

    @model_validator(mode="after")
    def draft_graph_is_bidirectional(self) -> "RequirementExtractionDraft":
        _validate_extraction_graph(
            self.traces,
            self.requirements,
            self.assumptions,
            self.implementation_suggestions,
        )
        return self

    @property
    def draft_digest(self) -> str:
        return sha256_digest(self)


class IntentReviewVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"


class IntentReviewFinding(R31Model):
    finding_code: str = Field(pattern=_FIELD_ID_PATTERN)
    message: str = Field(min_length=1, max_length=5000)
    requirement_refs: tuple[str, ...] = ()
    trace_refs: tuple[str, ...] = ()
    blocking: bool

    @field_validator("requirement_refs", "trace_refs")
    @classmethod
    def intent_finding_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "intent review reference")


class RequirementIntentReview(R31Model):
    analysis_context_digest: str = Field(pattern=_DIGEST_PATTERN)
    extraction_draft_digest: str = Field(pattern=_DIGEST_PATTERN)
    verdict: IntentReviewVerdict
    findings: tuple[IntentReviewFinding, ...] = ()

    @model_validator(mode="after")
    def intent_review_is_consistent(self) -> "RequirementIntentReview":
        has_blocking = any(item.blocking for item in self.findings)
        if self.verdict is IntentReviewVerdict.PASS and has_blocking:
            raise ValueError("통과 intent review에는 blocking finding을 둘 수 없습니다.")
        if self.verdict is not IntentReviewVerdict.PASS and not has_blocking:
            raise ValueError("fail/blocked intent review에는 blocking finding이 필요합니다.")
        return self

    @property
    def review_digest(self) -> str:
        return sha256_digest(self)


class PlanningRole(StrEnum):
    PURPOSE_RESOLVER = "purpose_resolver"
    INTENT_REVIEWER = "intent_reviewer"
    CANDIDATE_GENERATOR = "candidate_generator"
    HARD_GATE_REVIEWER = "hard_gate_reviewer"
    CRITICAL_REVIEWER = "critical_reviewer"
    SCORER_SELECTOR = "scorer_selector"
    SESSION_ADVISOR = "session_advisor"
    EVAL_RUNNER = "eval_runner"


class ModelCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    SCHEMA_RECOVERED = "schema_recovered"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    REQUIRED_MODEL_UNAVAILABLE = "required_model_unavailable"


class ModelUsageMetric(R31Model):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,199}$")
    value: int = Field(ge=0)


class ModelCallReceipt(R31Model):
    schema_version: Literal[R31_SCHEMA_VERSION] = R31_SCHEMA_VERSION
    call_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    role: PlanningRole
    model_id: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    inventory_digest: str = Field(pattern=_DIGEST_PATTERN)
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    status: ModelCallStatus
    schema_recovery_attempts: int = Field(default=0, ge=0, le=1)
    thread_id: str | None = Field(default=None, min_length=1, max_length=200)
    turn_ids: tuple[str, ...] = ()
    token_count: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    usage: tuple[ModelUsageMetric, ...] = ()
    error_summary: str | None = Field(default=None, max_length=5000)

    @field_validator("turn_ids")
    @classmethod
    def turn_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "model call turn")

    @field_validator("usage")
    @classmethod
    def usage_metric_names_are_unique(
        cls, value: tuple[ModelUsageMetric, ...]
    ) -> tuple[ModelUsageMetric, ...]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("model usage metric 이름이 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def call_receipt_is_consistent(self) -> "ModelCallReceipt":
        if self.status in {ModelCallStatus.SUCCEEDED, ModelCallStatus.SCHEMA_RECOVERED}:
            if self.output_digest is None or self.error_summary is not None:
                raise ValueError("성공 model call에는 output digest만 필요합니다.")
        else:
            if self.error_summary is None:
                raise ValueError("실패 model call에는 오류 요약이 필요합니다.")
            if self.output_digest is not None:
                raise ValueError("실패 model call에는 성공 output digest를 둘 수 없습니다.")
        if self.status is ModelCallStatus.SCHEMA_RECOVERED:
            if self.schema_recovery_attempts != 1:
                raise ValueError("schema_recovered는 정확히 1회 복구를 뜻합니다.")
        elif self.status not in {
            ModelCallStatus.FAILED,
            ModelCallStatus.TIMED_OUT,
        } and self.schema_recovery_attempts != 0:
            raise ValueError("schema recovery 횟수와 status가 일치하지 않습니다.")
        return self


class RequirementExtractionReceipt(R31Model):
    analysis_context_digest: str = Field(pattern=_DIGEST_PATTERN)
    raw_request_digest: str = Field(pattern=_DIGEST_PATTERN)
    request_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_resolution_digest: str = Field(pattern=_DIGEST_PATTERN)
    project_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    traces: tuple[RawRequestTrace, ...] = Field(min_length=1)
    requirements: tuple[ExtractedPlanningRequirement, ...] = Field(min_length=1)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_project_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    extraction_draft_digest: str = Field(pattern=_DIGEST_PATTERN)
    intent_review_digest: str = Field(pattern=_DIGEST_PATTERN)
    selected_profile_sections: tuple[ProfileSectionName, ...] = Field(min_length=1)
    assumptions: tuple[RequirementAnalysisNote, ...] = ()
    implementation_suggestions: tuple[RequirementAnalysisNote, ...] = ()
    unresolved_items: tuple[str, ...] = ()

    @field_validator("unresolved_items")
    @classmethod
    def unresolved_items_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "unresolved requirement")

    @model_validator(mode="after")
    def trace_graph_is_bidirectional(self) -> "RequirementExtractionReceipt":
        _validate_extraction_graph(
            self.traces,
            self.requirements,
            self.assumptions,
            self.implementation_suggestions,
        )
        if len(self.selected_profile_sections) != len(set(self.selected_profile_sections)):
            raise ValueError("선택된 profile section이 중복됐습니다.")
        return self

    @property
    def extraction_digest(self) -> str:
        return sha256_digest(self)

    @property
    def semantic_digest(self) -> str:
        """artifact provenance를 제외하고 후보 생성에 영향을 주는 의미 digest."""

        def ordered(items: tuple[Any, ...], identifier: str) -> list[dict[str, Any]]:
            return sorted(
                (item.model_dump(mode="json") for item in items),
                key=lambda item: item[identifier],
            )

        return sha256_digest(
            {
                "raw_request_digest": self.raw_request_digest,
                "request_spec_digest": self.request_spec_digest,
                "mission_resolution_digest": self.mission_resolution_digest,
                "profile_definition_digest": self.profile_definition_digest,
                "traces": ordered(self.traces, "trace_id"),
                "requirements": ordered(self.requirements, "requirement_id"),
                "selected_profile_sections": sorted(
                    item.value for item in self.selected_profile_sections
                ),
                "assumptions": ordered(self.assumptions, "note_id"),
                "implementation_suggestions": ordered(
                    self.implementation_suggestions,
                    "note_id",
                ),
                "unresolved_items": sorted(self.unresolved_items),
            }
        )


class RequirementReviewEvidence(R31Model):
    """freeze 가능한 요구 추출이 실제 독립 model review를 거쳤다는 증거."""

    intent_review: RequirementIntentReview
    extractor_receipt: ModelCallReceipt
    reviewer_receipt: ModelCallReceipt

    @model_validator(mode="after")
    def independent_successful_calls_are_present(self) -> "RequirementReviewEvidence":
        if self.intent_review.verdict is not IntentReviewVerdict.PASS:
            raise ValueError("requirement review evidence에는 PASS 판정이 필요합니다.")
        successful = {ModelCallStatus.SUCCEEDED, ModelCallStatus.SCHEMA_RECOVERED}
        expected = (
            (self.extractor_receipt, PlanningRole.PURPOSE_RESOLVER),
            (self.reviewer_receipt, PlanningRole.INTENT_REVIEWER),
        )
        for receipt, role in expected:
            if receipt.role is not role or receipt.status not in successful:
                raise ValueError("requirement review evidence의 역할 또는 성공 상태가 다릅니다.")
            if receipt.thread_id is None or not receipt.turn_ids:
                raise ValueError("requirement review evidence에는 thread/turn 결속이 필요합니다.")
        if self.extractor_receipt.call_id == self.reviewer_receipt.call_id:
            raise ValueError("extractor와 reviewer call ID는 달라야 합니다.")
        if self.extractor_receipt.thread_id == self.reviewer_receipt.thread_id:
            raise ValueError("요구 생성과 독립 review는 서로 다른 thread여야 합니다.")
        return self

    @property
    def evidence_digest(self) -> str:
        return sha256_digest(self)


class MissionReviewEvidence(R31Model):
    """Mission 제안·독립 review와 결정적 resolution의 freeze 증거."""

    proposal: MissionResolutionHint
    reviewed_proposal: MissionResolutionHint
    resolved_selection: MissionSelectionReceipt
    proposer_receipt: ModelCallReceipt
    reviewer_receipt: ModelCallReceipt

    @model_validator(mode="after")
    def independent_successful_calls_are_bound(self) -> "MissionReviewEvidence":
        successful = {ModelCallStatus.SUCCEEDED, ModelCallStatus.SCHEMA_RECOVERED}
        expected = (
            (
                self.proposer_receipt,
                PlanningRole.PURPOSE_RESOLVER,
                sha256_digest(self.proposal),
            ),
            (
                self.reviewer_receipt,
                PlanningRole.INTENT_REVIEWER,
                sha256_digest(self.reviewed_proposal),
            ),
        )
        for receipt, role, output_digest in expected:
            if receipt.role is not role or receipt.status not in successful:
                raise ValueError("Mission review evidence의 역할 또는 성공 상태가 다릅니다.")
            if receipt.thread_id is None or not receipt.turn_ids:
                raise ValueError("Mission review evidence에는 thread/turn 결속이 필요합니다.")
            if receipt.output_digest != output_digest:
                raise ValueError("Mission review evidence의 model output digest가 다릅니다.")
        if self.proposer_receipt.call_id == self.reviewer_receipt.call_id:
            raise ValueError("Mission proposer와 reviewer call ID는 달라야 합니다.")
        if self.proposer_receipt.thread_id == self.reviewer_receipt.thread_id:
            raise ValueError("Mission 생성과 독립 review는 서로 다른 thread여야 합니다.")
        return self

    @property
    def evidence_digest(self) -> str:
        return sha256_digest(self)


class ArtifactReference(R31Model):
    artifact_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    artifact_type: str = Field(pattern=_FIELD_ID_PATTERN)
    digest: str = Field(pattern=_DIGEST_PATTERN)
    source_run_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)


class PlanningRunInput(R31Model):
    schema_version: Literal[R31_SCHEMA_VERSION] = R31_SCHEMA_VERSION
    request_spec: RequestSpec
    profile_revision: ProjectProfileRevision
    mission_selection: MissionSelectionReceipt
    mission_review_evidence: MissionReviewEvidence
    effective_policy: EffectivePlanningPolicy
    requirement_extraction: RequirementExtractionReceipt
    requirement_review_evidence: RequirementReviewEvidence
    project_snapshot: ProjectStateSnapshot
    source_run_ids: tuple[str, ...] = ()
    input_artifact_refs: tuple[ArtifactReference, ...] = ()

    @field_validator("source_run_ids")
    @classmethod
    def source_runs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "source run")

    @model_validator(mode="after")
    def semantic_inputs_are_bound(self) -> "PlanningRunInput":
        request_digest = self.request_spec.canonical_digest
        mission_digest = self.mission_selection.mission_resolution_digest
        profile_digest = self.profile_revision.definition_digest
        if self.mission_selection.status is not MissionResolutionStatus.RESOLVED:
            raise ValueError("확정되지 않은 Mission은 PlanningRun으로 freeze할 수 없습니다.")
        reviewed_selection = self.mission_review_evidence.resolved_selection
        if reviewed_selection.status is not MissionResolutionStatus.RESOLVED:
            raise ValueError("Mission review evidence에는 resolved selection이 필요합니다.")
        selection_semantic_fields = (
            "raw_request_digest",
            "profile_revision_id",
            "profile_definition_digest",
            "status",
            "mission",
            "options",
            "question",
            "conflicts",
            "warnings",
            "override_receipts",
        )
        if any(
            getattr(reviewed_selection, field_name)
            != getattr(self.mission_selection, field_name)
            for field_name in selection_semantic_fields
        ):
            raise ValueError(
                "Mission review evidence가 최종 RequestSpec에 재결속된 Mission과 다릅니다."
            )
        if self.request_spec.project_id != self.profile_revision.project_id:
            raise ValueError("RequestSpec과 ProjectProfile의 project_id가 다릅니다.")
        if self.project_snapshot.project_id != self.request_spec.project_id:
            raise ValueError("ProjectStateSnapshot의 project_id가 다릅니다.")
        if self.mission_selection.request_spec_digest != request_digest:
            raise ValueError("Mission receipt의 RequestSpec digest가 다릅니다.")
        if self.mission_selection.raw_request_digest != sha256_bytes(
            self.request_spec.user_request.encode("utf-8")
        ):
            raise ValueError("Mission receipt의 raw request digest가 다릅니다.")
        if self.mission_selection.profile_revision_id != self.profile_revision.profile_revision_id:
            raise ValueError("Mission receipt의 profile revision이 다릅니다.")
        if self.mission_selection.profile_definition_digest != profile_digest:
            raise ValueError("Mission receipt의 profile definition digest가 다릅니다.")
        if self.effective_policy.profile_definition_digest != profile_digest:
            raise ValueError("EffectivePlanningPolicy의 profile digest가 다릅니다.")
        if self.effective_policy.mission_resolution_digest != mission_digest:
            raise ValueError("EffectivePlanningPolicy의 Mission digest가 다릅니다.")
        if self.project_snapshot.mission_resolution_digest != mission_digest:
            raise ValueError("ProjectStateSnapshot의 Mission digest가 다릅니다.")
        if tuple(
            self.effective_policy.override_receipts[
                : len(self.mission_selection.override_receipts)
            ]
        ) != self.mission_selection.override_receipts:
            raise ValueError("Mission의 profile override 이력이 effective policy에서 유실됐습니다.")
        profile_policy_values = {
            "lifecycle_stage": self.profile_revision.definition.lifecycle_stage.value,
            "criticality": self.profile_revision.definition.criticality.value,
            "compatibility_policy": (
                self.profile_revision.definition.compatibility_policy.value
            ),
            "risk_tolerance": (
                self.profile_revision.definition.default_risk_tolerance.value
            ),
        }
        effective_policy_values = {
            "lifecycle_stage": self.effective_policy.lifecycle_stage.value,
            "criticality": self.effective_policy.criticality.value,
            "compatibility_policy": self.effective_policy.compatibility_policy.value,
            "risk_tolerance": self.effective_policy.risk_tolerance.value,
        }
        supported_override_fields = {"compatibility_policy", "risk_tolerance"}
        unknown_override_fields = {
            receipt.field_name
            for receipt in self.effective_policy.override_receipts
            if receipt.field_name not in profile_policy_values
        }
        if unknown_override_fields:
            raise ValueError(
                "EffectivePlanningPolicy가 지원하지 않는 profile field를 override합니다: "
                f"{sorted(unknown_override_fields)}"
            )
        authority_rank = {"user_mission": 1, "explicit_request": 2}
        for field_name, profile_value in profile_policy_values.items():
            field_receipts = tuple(
                receipt
                for receipt in self.effective_policy.override_receipts
                if receipt.field_name == field_name
            )
            if field_receipts and field_name not in supported_override_fields:
                raise ValueError(
                    f"{field_name}은 R3.1에서 override할 수 없는 ProjectProfile 값입니다."
                )
            current_value = profile_value
            previous_rank = 0
            for receipt in field_receipts:
                if receipt.profile_value != current_value:
                    raise ValueError(
                        f"{field_name} override 이력이 이전 값에 연속적으로 결속되지 않았습니다."
                    )
                if receipt.effective_value == current_value:
                    raise ValueError(f"{field_name}에 변화 없는 override receipt가 있습니다.")
                observed_rank = authority_rank[receipt.winning_authority]
                if observed_rank < previous_rank:
                    raise ValueError(f"{field_name} override가 권위 우선순위를 역전했습니다.")
                previous_rank = observed_rank
                current_value = receipt.effective_value
            if effective_policy_values[field_name] != current_value:
                raise ValueError(
                    f"EffectivePlanningPolicy의 {field_name} 값에 검증 가능한 profile override 이력이 없습니다."
                )
        mission = self.mission_selection.mission
        assert mission is not None
        if self.effective_policy.mutation_policy is not mission.mutation_policy:
            raise ValueError("EffectivePlanningPolicy의 mutation policy가 Mission과 다릅니다.")
        if (
            self.effective_policy.behavior_preservation
            is not mission.behavior_preservation
        ):
            raise ValueError(
                "EffectivePlanningPolicy의 behavior preservation이 Mission과 다릅니다."
            )
        if set(self.effective_policy.allowed_external_effects) != set(
            mission.allowed_external_effects
        ):
            raise ValueError(
                "EffectivePlanningPolicy의 외부 효과 허용 범위가 Mission과 다릅니다."
            )
        if set(self.effective_policy.forbidden_scopes) != set(
            mission.forbidden_scopes
        ):
            raise ValueError("EffectivePlanningPolicy의 금지 범위가 Mission과 다릅니다.")
        extraction = self.requirement_extraction
        if extraction.raw_request_digest != sha256_bytes(
            self.request_spec.user_request.encode("utf-8")
        ):
            raise ValueError("Requirement receipt의 raw request digest가 다릅니다.")
        if extraction.request_spec_digest != request_digest:
            raise ValueError("Requirement receipt의 RequestSpec digest가 다릅니다.")
        if extraction.mission_resolution_digest != mission_digest:
            raise ValueError("Requirement receipt의 Mission digest가 다릅니다.")
        if extraction.project_snapshot_digest != self.project_snapshot.snapshot_digest:
            raise ValueError("Requirement receipt의 ProjectStateSnapshot digest가 다릅니다.")
        if extraction.profile_definition_digest != profile_digest:
            raise ValueError("Requirement receipt의 ProjectProfile digest가 다릅니다.")
        reconstructed_draft = RequirementExtractionDraft(
            raw_request_digest=extraction.raw_request_digest,
            mission_digest=mission.mission_digest,
            profile_definition_digest=extraction.profile_definition_digest,
            project_snapshot_digest=extraction.source_project_snapshot_digest,
            request_summary=self.request_spec.request_summary,
            traces=extraction.traces,
            requirements=extraction.requirements,
            assumptions=extraction.assumptions,
            implementation_suggestions=extraction.implementation_suggestions,
            unresolved_items=extraction.unresolved_items,
        )
        if reconstructed_draft.draft_digest != extraction.extraction_draft_digest:
            raise ValueError(
                "Requirement receipt의 내용이 독립 review를 받은 extraction draft와 다릅니다."
            )
        review_evidence = self.requirement_review_evidence
        intent_review = review_evidence.intent_review
        if intent_review.analysis_context_digest != extraction.analysis_context_digest:
            raise ValueError("독립 requirement review의 analysis context digest가 다릅니다.")
        if intent_review.extraction_draft_digest != extraction.extraction_draft_digest:
            raise ValueError("독립 requirement review의 extraction draft digest가 다릅니다.")
        if intent_review.review_digest != extraction.intent_review_digest:
            raise ValueError("Requirement receipt의 intent review digest가 실제 review와 다릅니다.")
        if (
            review_evidence.extractor_receipt.output_digest
            != extraction.extraction_draft_digest
        ):
            raise ValueError("요구 extractor receipt가 검토된 extraction draft와 다릅니다.")
        if (
            review_evidence.reviewer_receipt.output_digest
            != intent_review.review_digest
        ):
            raise ValueError("요구 reviewer receipt가 실제 intent review와 다릅니다.")
        extracted_items = (
            *extraction.requirements,
            *extraction.assumptions,
            *extraction.implementation_suggestions,
        )
        known_snapshot_entries = {item.entry_id for item in self.project_snapshot.entries}
        unknown_snapshot_refs = {
            ref
            for item in extracted_items
            for ref in item.snapshot_entry_refs
            if ref not in known_snapshot_entries
        }
        if unknown_snapshot_refs:
            raise ValueError(
                "Requirement receipt가 알 수 없는 snapshot entry를 참조합니다: "
                f"{sorted(unknown_snapshot_refs)}"
            )
        if extraction.selected_profile_sections:
            allowed_profile_refs = {
                ref
                for section_name in extraction.selected_profile_sections
                for ref in getattr(self.profile_revision.definition, section_name.value).source_refs
            }
            unknown_profile_refs = {
                ref
                for item in extracted_items
                for ref in item.profile_source_refs
                if ref not in allowed_profile_refs
            }
            if unknown_profile_refs:
                raise ValueError(
                    "Requirement receipt가 선택하지 않은 profile source를 참조합니다: "
                    f"{sorted(unknown_profile_refs)}"
                )
        raw_request = self.request_spec.user_request
        for trace in extraction.traces:
            if trace.end_offset > len(raw_request):
                raise ValueError("raw request trace 범위가 사용자 원문을 벗어납니다.")
            if raw_request[trace.start_offset : trace.end_offset] != trace.excerpt:
                raise ValueError("raw request trace excerpt가 선언한 원문 범위와 다릅니다.")
        extracted_requirements = {
            item.requirement_id: item
            for item in extraction.requirements
            if item.kind in {RequirementKind.REQUIREMENT, RequirementKind.CONSTRAINT}
        }
        request_requirements = {
            item.requirement_id: item for item in self.request_spec.requirements
        }
        if extracted_requirements.keys() != request_requirements.keys():
            raise ValueError(
                "RequestSpec requirement 집합이 검토된 추출 receipt와 다릅니다."
            )
        for requirement_id, request_requirement in request_requirements.items():
            extracted_requirement = extracted_requirements[requirement_id]
            if request_requirement.statement != extracted_requirement.statement:
                raise ValueError(
                    f"RequestSpec requirement 문장이 검토된 추출과 다릅니다: {requirement_id}"
                )
            expected_must = extracted_requirement.mandatory
            observed_must = request_requirement.priority.value == "must"
            if observed_must != expected_must:
                raise ValueError(
                    f"RequestSpec requirement 우선순위가 검토된 추출과 다릅니다: {requirement_id}"
                )
        exclusions = {
            item.statement
            for item in extraction.requirements
            if item.kind is RequirementKind.EXCLUSION
        }
        if set(self.request_spec.out_of_scope) != exclusions:
            raise ValueError("RequestSpec out_of_scope과 검토된 추출 exclusion이 다릅니다.")
        outcomes = {
            item.statement
            for item in extraction.requirements
            if item.kind is RequirementKind.OBSERVABLE_OUTCOME
        }
        if mission.observable_outcome not in outcomes:
            raise ValueError("Mission observable outcome이 추출 receipt에서 누락됐습니다.")
        traces_by_id = {trace.trace_id: trace for trace in extraction.traces}
        for constraint in self.effective_policy.explicit_request_constraints:
            if constraint.source_ref not in traces_by_id:
                raise ValueError(
                    "명시 요청 constraint가 raw request trace에 결속되지 않았습니다: "
                    f"{constraint.constraint_id}"
                )
            matching_constraints = [
                item
                for item in extraction.requirements
                if item.kind is RequirementKind.CONSTRAINT
                and item.statement == constraint.statement
                and constraint.source_ref in item.trace_refs
            ]
            if not matching_constraints:
                raise ValueError(
                    "명시 요청 constraint가 검토된 constraint와 일치하지 않습니다: "
                    f"{constraint.constraint_id}"
                )
        expected_explicit_constraints = {
            (item.statement, trace_ref)
            for item in extraction.requirements
            if item.kind is RequirementKind.CONSTRAINT
            for trace_ref in item.trace_refs
        }
        observed_explicit_constraints = {
            (item.statement, item.source_ref)
            for item in self.effective_policy.explicit_request_constraints
        }
        if observed_explicit_constraints != expected_explicit_constraints:
            raise ValueError(
                "EffectivePlanningPolicy가 raw request의 명시 constraint 전체와 정확히 "
                "일치하지 않습니다."
            )
        snapshot_by_path: dict[str, str] = {}
        for entry in self.project_snapshot.entries:
            normalized_path = _normalized_local_path(entry.path)
            if normalized_path in snapshot_by_path:
                raise ValueError("ProjectStateSnapshot에 동일한 정규화 경로가 중복됐습니다.")
            snapshot_by_path[normalized_path] = entry.content_digest
        for source in self.request_spec.context_sources:
            reviewed_digest = snapshot_by_path.get(_normalized_local_path(source.path))
            if reviewed_digest is None:
                raise ValueError(
                    f"RequestSpec context가 검토된 snapshot에 없습니다: {source.path}"
                )
            if reviewed_digest != source.content_digest:
                raise ValueError(
                    f"RequestSpec context가 검토 후 변경됐습니다: {source.path}"
                )
        artifact_ids = [item.artifact_id for item in self.input_artifact_refs]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("input artifact ID가 중복됐습니다.")
        referenced_runs = {item.source_run_id for item in self.input_artifact_refs}
        if not referenced_runs.issubset(set(self.source_run_ids)):
            raise ValueError("input artifact의 source run이 source_run_ids에 없습니다.")
        return self

    @property
    def planning_input_digest(self) -> str:
        """Telemetry와 run 식별자를 제외한 R3.1 semantic input digest."""

        artifacts = sorted(
            (
                {
                    "artifact_type": item.artifact_type,
                    "digest": item.digest,
                }
                for item in self.input_artifact_refs
            ),
            key=lambda item: (item["artifact_type"], item["digest"]),
        )
        return sha256_digest(
            {
                "request_spec_digest": self.request_spec.canonical_digest,
                "profile_definition_digest": self.profile_revision.definition_digest,
                "mission_resolution_digest": (
                    self.mission_selection.mission_resolution_digest
                ),
                "effective_policy_digest": self.effective_policy.effective_policy_digest,
                "requirement_extraction_semantic_digest": (
                    self.requirement_extraction.semantic_digest
                ),
                "project_snapshot_semantic_digest": self.project_snapshot.semantic_digest,
                "analysis_artifacts": artifacts,
            }
        )


class PlanningRunStatus(StrEnum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    FROZEN = "frozen"
    SEARCHING = "searching"
    READY_FOR_REVIEW = "ready_for_review"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class PlanningRunReceipt(R31Model):
    run_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    planning_input: PlanningRunInput
    planning_input_digest: str = Field(pattern=_DIGEST_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=500)
    status: PlanningRunStatus
    created_at: datetime
    updated_at: datetime
    reason: str | None = Field(default=None, max_length=5000)
    superseded_by_run_id: str | None = Field(default=None, pattern=_ARTIFACT_ID_PATTERN)

    _times_are_aware = field_validator("created_at", "updated_at")(_aware)

    @model_validator(mode="after")
    def receipt_is_consistent(self) -> "PlanningRunReceipt":
        if self.planning_input_digest != self.planning_input.planning_input_digest:
            raise ValueError("PlanningRun의 input digest가 frozen input과 다릅니다.")
        if self.updated_at < self.created_at:
            raise ValueError("PlanningRun updated_at은 created_at보다 빠를 수 없습니다.")
        if self.status is PlanningRunStatus.SUPERSEDED:
            if self.reason is None:
                raise ValueError("superseded run에는 이유가 필요합니다.")
        elif self.superseded_by_run_id is not None:
            raise ValueError("superseded가 아닌 run에는 superseded_by_run_id를 둘 수 없습니다.")
        if self.status in {PlanningRunStatus.BLOCKED, PlanningRunStatus.FAILED} and not self.reason:
            raise ValueError("blocked/failed run에는 이유가 필요합니다.")
        return self

    @property
    def receipt_digest(self) -> str:
        return sha256_digest(self)


class ApproachBrief(R31Model):
    approach_id: str = Field(pattern=_FIELD_ID_PATTERN)
    mission_primary: MissionPrimary
    strategy_family: str = Field(min_length=1, max_length=500)
    change_shape: str = Field(min_length=1, max_length=1000)
    compatibility: str = Field(min_length=1, max_length=1000)
    rollout_recovery: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=5000)
    tradeoffs: tuple[str, ...] = ()
    risk_tags: tuple[RiskTag, ...] = ()

    @field_validator("tradeoffs")
    @classmethod
    def tradeoffs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "approach tradeoff")

    @field_validator("risk_tags")
    @classmethod
    def approach_risk_tags_are_unique(cls, value: tuple[RiskTag, ...]) -> tuple[RiskTag, ...]:
        if len(value) != len(set(value)):
            raise ValueError("approach risk tag가 중복됐습니다.")
        return value

    @property
    def signature(self) -> str:
        return sha256_digest(
            {
                "strategy_family": self.strategy_family.casefold(),
                "change_shape": self.change_shape.casefold(),
                "compatibility": self.compatibility.casefold(),
                "rollout_recovery": self.rollout_recovery.casefold(),
            }
        )


class PlanOutcomeContract(R31Model):
    observable_outcome: str = Field(min_length=1, max_length=5000)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    excluded_outcomes: tuple[str, ...] = ()

    @field_validator("acceptance_criteria", "excluded_outcomes")
    @classmethod
    def outcome_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "plan outcome")


class IntegrationValidationContract(R31Model):
    validation_id: str = Field(pattern=_FIELD_ID_PATTERN)
    work_item_refs: tuple[str, ...] = Field(min_length=1)
    check_type: str = Field(min_length=1, max_length=80)
    specification: dict[str, Any] = Field(default_factory=dict)
    required_evidence: tuple[str, ...] = Field(min_length=1)

    @field_validator("work_item_refs", "required_evidence")
    @classmethod
    def integration_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "integration validation")


class CriterionValidationBinding(R31Model):
    work_item_ref: str = Field(pattern=_FIELD_ID_PATTERN)
    criterion_id: str = Field(pattern=_FIELD_ID_PATTERN)
    check_type: str = Field(min_length=1, max_length=80)
    capability_id: str | None = Field(default=None, pattern=_FIELD_ID_PATTERN)
    specification_digest: str = Field(pattern=_DIGEST_PATTERN)
    required_evidence: tuple[str, ...] = Field(min_length=1)

    @field_validator("required_evidence")
    @classmethod
    def binding_evidence_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "criterion evidence")


class DependencyContract(R31Model):
    producer_work_item_ref: str = Field(pattern=_FIELD_ID_PATTERN)
    consumer_work_item_ref: str = Field(pattern=_FIELD_ID_PATTERN)
    produces: tuple[str, ...] = Field(min_length=1)
    consumes: tuple[str, ...] = Field(min_length=1)
    compatibility_contract: str = Field(min_length=1, max_length=5000)

    @field_validator("produces", "consumes")
    @classmethod
    def product_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "dependency product")

    @model_validator(mode="after")
    def consumed_products_are_produced(self) -> "DependencyContract":
        if self.producer_work_item_ref == self.consumer_work_item_ref:
            raise ValueError("dependency contract의 producer와 consumer는 달라야 합니다.")
        missing = set(self.consumes) - set(self.produces)
        if missing:
            raise ValueError(f"consumer가 생산되지 않은 산출물을 소비합니다: {sorted(missing)}")
        return self


class FailureRecoveryContract(R31Model):
    work_item_ref: str = Field(pattern=_FIELD_ID_PATTERN)
    failure_detection: str = Field(min_length=1, max_length=5000)
    idempotency_strategy: str = Field(min_length=1, max_length=5000)
    partial_execution_strategy: str = Field(min_length=1, max_length=5000)
    duplicate_dispatch_strategy: str = Field(min_length=1, max_length=5000)
    retry_policy: str = Field(min_length=1, max_length=5000)
    rollback_strategy: str = Field(min_length=1, max_length=5000)
    human_checkpoint: str | None = Field(default=None, max_length=5000)


class PlanContractSidecar(R31Model):
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_outcome: PlanOutcomeContract
    integration_validations: tuple[IntegrationValidationContract, ...] = Field(
        min_length=1
    )
    criterion_bindings: tuple[CriterionValidationBinding, ...] = Field(min_length=1)
    dependency_contracts: tuple[DependencyContract, ...] = ()
    failure_recovery_contracts: tuple[FailureRecoveryContract, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def local_identifiers_are_unique(self) -> "PlanContractSidecar":
        validation_ids = [item.validation_id for item in self.integration_validations]
        binding_keys = [
            (item.work_item_ref, item.criterion_id) for item in self.criterion_bindings
        ]
        dependency_keys = [
            (item.producer_work_item_ref, item.consumer_work_item_ref)
            for item in self.dependency_contracts
        ]
        recovery_refs = [item.work_item_ref for item in self.failure_recovery_contracts]
        if len(validation_ids) != len(set(validation_ids)):
            raise ValueError("integration validation ID가 중복됐습니다.")
        if len(binding_keys) != len(set(binding_keys)):
            raise ValueError("criterion-validation binding이 중복됐습니다.")
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("dependency contract가 중복됐습니다.")
        if len(recovery_refs) != len(set(recovery_refs)):
            raise ValueError("failure/recovery contract가 중복됐습니다.")
        return self

    @property
    def contract_digest(self) -> str:
        return sha256_digest(self)


class GateName(StrEnum):
    INTENT = "intent"
    PLAN = "plan"
    ENGINEERING = "engineering"
    VERIFICATION = "verification"
    EXECUTION = "execution"


class PlanVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class RuntimeStatus(StrEnum):
    NOT_RUN = "not_run"
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class GateDiagnostic(R31Model):
    finding_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    severity: FindingSeverity
    message: str = Field(min_length=1, max_length=5000)
    evidence_refs: tuple[str, ...] = ()
    work_item_refs: tuple[str, ...] = ()
    remediable: bool

    @field_validator("evidence_refs", "work_item_refs")
    @classmethod
    def diagnostic_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "gate diagnostic")


class GateFinding(R31Model):
    gate: GateName
    plan_verdict: PlanVerdict
    runtime_status: RuntimeStatus = RuntimeStatus.NOT_RUN
    summary: str = Field(min_length=1, max_length=5000)
    diagnostics: tuple[GateDiagnostic, ...] = ()
    not_applicable_rationale: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def finding_shape_is_valid(self) -> "GateFinding":
        if self.plan_verdict is PlanVerdict.NOT_APPLICABLE:
            if self.not_applicable_rationale is None:
                raise ValueError("not_applicable Gate에는 근거가 필요합니다.")
        elif self.not_applicable_rationale is not None:
            raise ValueError("not_applicable이 아닌 Gate에는 N/A 근거를 둘 수 없습니다.")
        if self.plan_verdict in {PlanVerdict.FAIL, PlanVerdict.BLOCKED} and not self.diagnostics:
            raise ValueError("fail/blocked Gate에는 진단 항목이 필요합니다.")
        if self.plan_verdict is PlanVerdict.PASS and any(
            item.severity is FindingSeverity.ERROR for item in self.diagnostics
        ):
            raise ValueError("통과 Gate에는 error 진단을 둘 수 없습니다.")
        if self.plan_verdict is PlanVerdict.NOT_APPLICABLE and self.diagnostics:
            raise ValueError("not_applicable Gate에는 진단 항목을 둘 수 없습니다.")
        return self


class ScoreDimensionRatings(R31Model):
    goal_fit_change_safety: int = Field(ge=0, le=4)
    verification_evidence_strength: int = Field(ge=0, le=4)
    execution_risk_control: int = Field(ge=0, le=4)
    maintainability_reproducibility: int = Field(ge=0, le=4)
    resource_efficiency: int = Field(ge=0, le=4)

    @property
    def calculated_score(self) -> int:
        numerator = (
            25 * self.goal_fit_change_safety
            + 25 * self.verification_evidence_strength
            + 20 * self.execution_risk_control
            + 20 * self.maintainability_reproducibility
            + 10 * self.resource_efficiency
        )
        return (numerator + 2) // 4


class ScoreDimension(StrEnum):
    GOAL_FIT_CHANGE_SAFETY = "goal_fit_change_safety"
    VERIFICATION_EVIDENCE_STRENGTH = "verification_evidence_strength"
    EXECUTION_RISK_CONTROL = "execution_risk_control"
    MAINTAINABILITY_REPRODUCIBILITY = "maintainability_reproducibility"
    RESOURCE_EFFICIENCY = "resource_efficiency"


class ScoreDimensionEvidence(R31Model):
    dimension: ScoreDimension
    rating: int = Field(ge=0, le=4)
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    sensitivity: str = Field(min_length=1, max_length=5000)

    @field_validator("evidence_refs")
    @classmethod
    def score_evidence_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "score evidence")


class WorkItemQualityRating(R31Model):
    work_item_ref: str = Field(pattern=_FIELD_ID_PATTERN)
    self_containment: int = Field(ge=0, le=2)
    functional_cohesion: int = Field(ge=0, le=2)
    acceptance_validation: int = Field(ge=0, le=2)
    interface_clarity: int = Field(ge=0, le=2)
    failure_retry: int = Field(ge=0, le=2)

    @property
    def weakest_rating(self) -> int:
        return min(
            self.self_containment,
            self.functional_cohesion,
            self.acceptance_validation,
            self.interface_clarity,
            self.failure_retry,
        )


class TieBreakEvidence(R31Model):
    reversibility: int = Field(ge=0, le=4)
    public_contract_change: bool
    change_surface: int = Field(ge=0)
    cost: int = Field(ge=0)
    mission_metrics: dict[str, int] = Field(default_factory=dict)

    @field_validator("mission_metrics")
    @classmethod
    def metric_values_are_bounded(cls, value: dict[str, int]) -> dict[str, int]:
        invalid_names = [name for name in value if not name or len(name) > 100]
        invalid_values = [rating for rating in value.values() if rating < 0 or rating > 4]
        if invalid_names:
            raise ValueError("mission metric 이름이 유효하지 않습니다.")
        if invalid_values:
            raise ValueError("mission metric 평점은 0~4여야 합니다.")
        return value


class PlanQualityReport(R31Model):
    planning_input_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_verdict: PlanVerdict
    gate_findings: tuple[GateFinding, ...] = Field(min_length=5, max_length=5)
    dimension_ratings: ScoreDimensionRatings | None = None
    dimension_evidence: tuple[ScoreDimensionEvidence, ...] = ()
    fitness_score: int | None = Field(default=None, ge=0, le=100)
    work_item_quality: tuple[WorkItemQualityRating, ...] = ()
    weakest_work_item_ref: str | None = Field(default=None, pattern=_FIELD_ID_PATTERN)
    weakest_work_item_rating: int | None = Field(default=None, ge=0, le=2)
    confidence: ConfidenceLevel
    tie_break_evidence: TieBreakEvidence | None = None

    @model_validator(mode="after")
    def quality_report_is_consistent(self) -> "PlanQualityReport":
        gates = [finding.gate for finding in self.gate_findings]
        if set(gates) != set(GateName) or len(gates) != len(set(gates)):
            raise ValueError("PlanQualityReport에는 5개 Hard Gate가 정확히 한 번씩 필요합니다.")
        verdicts = {finding.plan_verdict for finding in self.gate_findings}
        if PlanVerdict.BLOCKED in verdicts:
            derived = PlanVerdict.BLOCKED
        elif PlanVerdict.FAIL in verdicts:
            derived = PlanVerdict.FAIL
        elif verdicts == {PlanVerdict.NOT_APPLICABLE}:
            derived = PlanVerdict.NOT_APPLICABLE
        else:
            derived = PlanVerdict.PASS
        if self.plan_verdict is not derived:
            raise ValueError("plan_verdict가 5개 Gate 판정과 일치하지 않습니다.")
        has_score_inputs = self.dimension_ratings is not None
        if has_score_inputs != (self.fitness_score is not None):
            raise ValueError("dimension ratings와 fitness_score는 함께 있어야 합니다.")
        if self.dimension_ratings is not None:
            if self.plan_verdict is not PlanVerdict.PASS:
                raise ValueError("Hard Gate 미통과 계획은 점수화할 수 없습니다.")
            if self.fitness_score != self.dimension_ratings.calculated_score:
                raise ValueError("fitness_score가 balanced-mvp-v0 계산값과 다릅니다.")
            if self.tie_break_evidence is None:
                raise ValueError("점수화된 계획에는 tie-break 근거가 필요합니다.")
            evidence_by_dimension = {
                item.dimension: item for item in self.dimension_evidence
            }
            if (
                len(evidence_by_dimension) != len(self.dimension_evidence)
                or set(evidence_by_dimension) != set(ScoreDimension)
            ):
                raise ValueError("점수화된 계획에는 5개 차원의 근거가 정확히 필요합니다.")
            for dimension, evidence in evidence_by_dimension.items():
                if evidence.rating != getattr(self.dimension_ratings, dimension.value):
                    raise ValueError("차원별 score evidence rating이 점수 입력과 다릅니다.")
        elif self.tie_break_evidence is not None:
            raise ValueError("미점수 계획에는 tie-break 근거를 둘 수 없습니다.")
        elif self.dimension_evidence:
            raise ValueError("미점수 계획에는 score evidence를 둘 수 없습니다.")
        refs = [item.work_item_ref for item in self.work_item_quality]
        if len(refs) != len(set(refs)):
            raise ValueError("WorkItem quality rating이 중복됐습니다.")
        if self.work_item_quality:
            minimum = min(item.weakest_rating for item in self.work_item_quality)
            expected_ref = min(
                item.work_item_ref
                for item in self.work_item_quality
                if item.weakest_rating == minimum
            )
            if (
                self.weakest_work_item_ref != expected_ref
                or self.weakest_work_item_rating != minimum
            ):
                raise ValueError("최약 WorkItem 참조·평점이 세부 평점과 다릅니다.")
        elif self.weakest_work_item_ref is not None or self.weakest_work_item_rating is not None:
            raise ValueError("WorkItem 평점 없이 최약 WorkItem을 지정할 수 없습니다.")
        return self

    @property
    def report_digest(self) -> str:
        return sha256_digest(self)


class CandidateStatus(StrEnum):
    GENERATED = "generated"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    ADMISSIBLE = "admissible"
    SELECTED = "selected"


class CandidateObservationStatus(StrEnum):
    NOT_OBSERVED = "not_observed"


class CandidateEnvelope(R31Model):
    candidate_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    parent_candidate_id: str | None = Field(default=None, pattern=_ARTIFACT_ID_PATTERN)
    version: int = Field(ge=1, le=5)
    refinement_round: int = Field(ge=0, le=1)
    status: CandidateStatus
    observation_status: CandidateObservationStatus = (
        CandidateObservationStatus.NOT_OBSERVED
    )
    planning_input_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_resolution_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_primary: MissionPrimary
    approach: ApproachBrief
    plan: PlanDraft
    contract: PlanContractSidecar
    quality_report: PlanQualityReport | None = None
    policy_id: str = Field(pattern=_FIELD_ID_PATTERN)
    policy_version: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def candidate_contract_is_consistent(self) -> "CandidateEnvelope":
        if self.parent_candidate_id == self.candidate_id:
            raise ValueError("candidate는 자신을 parent로 참조할 수 없습니다.")
        if self.version == 1 and self.parent_candidate_id is not None:
            raise ValueError("초기 candidate에는 parent_candidate_id를 둘 수 없습니다.")
        if self.version > 1 and self.parent_candidate_id is None:
            raise ValueError("정제 candidate에는 parent_candidate_id가 필요합니다.")
        if self.refinement_round == 0 and self.version > 1:
            raise ValueError("정제 candidate의 refinement_round는 1이어야 합니다.")
        if self.refinement_round == 1 and self.parent_candidate_id is None:
            raise ValueError("refinement_round 1에는 parent candidate가 필요합니다.")
        if self.approach.mission_primary is not self.mission_primary:
            raise ValueError("ApproachBrief의 Mission이 candidate와 다릅니다.")
        if self.contract.plan_digest != self.plan.canonical_digest:
            raise ValueError("PlanContractSidecar가 정확한 PlanDraft에 결속되지 않았습니다.")
        self._validate_contract_against_plan()
        if self.quality_report is not None:
            report = self.quality_report
            if report.planning_input_digest != self.planning_input_digest:
                raise ValueError("quality report의 planning input digest가 다릅니다.")
            if report.plan_digest != self.plan.canonical_digest:
                raise ValueError("quality report의 plan digest가 다릅니다.")
            quality_refs = {item.work_item_ref for item in report.work_item_quality}
            plan_refs = {item.client_ref for item in self.plan.work_items}
            if quality_refs and quality_refs != plan_refs:
                raise ValueError("WorkItem quality 평점은 PlanDraft의 모든 작업에 필요합니다.")
        if self.status in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}:
            report = self.quality_report
            if (
                report is None
                or report.plan_verdict is not PlanVerdict.PASS
                or report.fitness_score is None
                or not report.work_item_quality
                or any(item.weakest_rating == 0 for item in report.work_item_quality)
            ):
                raise ValueError("admissible/selected candidate는 모든 Gate·점수·WorkItem 축을 통과해야 합니다.")
        elif self.quality_report is not None and self.quality_report.fitness_score is not None:
            raise ValueError("admissible/selected가 아닌 candidate는 점수화할 수 없습니다.")
        if self.status is CandidateStatus.NEEDS_REVISION and self.quality_report is not None:
            diagnostics = [
                diagnostic
                for finding in self.quality_report.gate_findings
                for diagnostic in finding.diagnostics
            ]
            if not diagnostics or not all(item.remediable for item in diagnostics):
                raise ValueError("needs_revision 결함은 모두 정제 가능한 진단이어야 합니다.")
        return self

    def _validate_contract_against_plan(self) -> None:
        work_items = {item.client_ref: item for item in self.plan.work_items}
        known = set(work_items)
        integration_refs = {
            ref
            for validation in self.contract.integration_validations
            for ref in validation.work_item_refs
        }
        if not integration_refs.issubset(known):
            raise ValueError("integration validation이 알 수 없는 WorkItem을 참조합니다.")
        expected_bindings = {
            (item.client_ref, validation.criterion_id): (
                validation.check_type,
                validation.capability_id,
                sha256_digest(validation.specification),
            )
            for item in self.plan.work_items
            for validation in item.validations
        }
        actual_bindings = {
            (binding.work_item_ref, binding.criterion_id): (
                binding.check_type,
                binding.capability_id,
                binding.specification_digest,
            )
            for binding in self.contract.criterion_bindings
        }
        if actual_bindings != expected_bindings:
            raise ValueError("criterion-validation binding이 PlanDraft validations와 다릅니다.")
        expected_dependencies = {
            (dependency, item.client_ref)
            for item in self.plan.work_items
            for dependency in item.dependencies
        }
        actual_dependencies = {
            (item.producer_work_item_ref, item.consumer_work_item_ref)
            for item in self.contract.dependency_contracts
        }
        if actual_dependencies != expected_dependencies:
            raise ValueError("dependency contract가 PlanDraft DAG와 다릅니다.")
        recovery_refs = {
            item.work_item_ref for item in self.contract.failure_recovery_contracts
        }
        if recovery_refs != known:
            raise ValueError("모든 WorkItem에는 failure/retry/rollback 계약이 필요합니다.")

    @property
    def candidate_digest(self) -> str:
        """평가·선택 상태와 분리된 immutable candidate definition digest."""

        return sha256_digest(
            {
                "candidate_id": self.candidate_id,
                "parent_candidate_id": self.parent_candidate_id,
                "version": self.version,
                "refinement_round": self.refinement_round,
                "planning_input_digest": self.planning_input_digest,
                "mission_resolution_digest": self.mission_resolution_digest,
                "mission_primary": self.mission_primary,
                "approach": self.approach,
                "plan": self.plan,
                "contract": self.contract,
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
            }
        )


class SelectionSource(StrEnum):
    RECOMMENDED_DEFAULT = "recommended_default"
    USER_OVERRIDE = "user_override"
    NONE_LOW_CONFIDENCE = "none_low_confidence"
    NONE_NO_ADMISSIBLE = "none_no_admissible"


class SelectionReceipt(R31Model):
    run_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    planning_input_digest: str = Field(pattern=_DIGEST_PATTERN)
    recommended_candidate_id: str | None = Field(default=None, pattern=_ARTIFACT_ID_PATTERN)
    selected_candidate_id: str | None = Field(default=None, pattern=_ARTIFACT_ID_PATTERN)
    selection_source: SelectionSource
    ranked_candidate_ids: tuple[str, ...] = ()
    alternative_candidate_ids: tuple[str, ...] = ()
    pruned_duplicate_candidate_ids: tuple[str, ...] = ()
    tie_break_reasons: tuple[str, ...] = ()
    supersedes_selection_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    policy_id: str = Field(default=R31_SCORE_POLICY_ID, pattern=_FIELD_ID_PATTERN)
    policy_version: str = Field(default=R31_SCORE_POLICY_VERSION, min_length=1, max_length=80)

    @field_validator(
        "ranked_candidate_ids",
        "alternative_candidate_ids",
        "pruned_duplicate_candidate_ids",
        "tie_break_reasons",
    )
    @classmethod
    def selection_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "selection")

    @model_validator(mode="after")
    def selection_shape_is_valid(self) -> "SelectionReceipt":
        ranked = set(self.ranked_candidate_ids)
        if self.recommended_candidate_id is not None and self.recommended_candidate_id not in ranked:
            raise ValueError("recommended candidate가 ranking에 없습니다.")
        if self.selected_candidate_id is not None and self.selected_candidate_id not in ranked:
            raise ValueError("selected candidate가 ranking에 없습니다.")
        if not set(self.alternative_candidate_ids).issubset(ranked):
            raise ValueError("alternative candidate가 ranking에 없습니다.")
        if self.selected_candidate_id in self.alternative_candidate_ids:
            raise ValueError("selected candidate를 alternative로 기록할 수 없습니다.")
        if set(self.pruned_duplicate_candidate_ids) & ranked:
            raise ValueError("중복 제거 후보를 ranking에 함께 기록할 수 없습니다.")
        if self.selection_source is SelectionSource.RECOMMENDED_DEFAULT:
            if self.selected_candidate_id is None or self.selected_candidate_id != self.recommended_candidate_id:
                raise ValueError("recommended_default는 추천 candidate를 기본 선택해야 합니다.")
        elif self.selection_source is SelectionSource.USER_OVERRIDE:
            if self.selected_candidate_id is None:
                raise ValueError("user_override에는 selected candidate가 필요합니다.")
        else:
            if self.selected_candidate_id is not None:
                raise ValueError("선택 보류 receipt에는 selected candidate를 둘 수 없습니다.")
        if self.selection_source is SelectionSource.NONE_NO_ADMISSIBLE and self.ranked_candidate_ids:
            raise ValueError("admissible candidate가 없으면 ranking도 비어야 합니다.")
        return self

    @property
    def selection_digest(self) -> str:
        return sha256_digest(self)


class SessionStrategy(StrEnum):
    REUSE = "reuse"
    ISOLATE = "isolate"
    HANDOFF = "handoff"


class SessionHint(R31Model):
    role: PlanningRole
    strategy: SessionStrategy
    candidate_id: str | None = Field(default=None, pattern=_ARTIFACT_ID_PATTERN)
    reusable_prefix_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    artifact_refs: tuple[ArtifactReference, ...] = ()
    independent_review_session: bool
    hidden_context_required: Literal[False] = False
    rationale: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def reviewer_isolation_is_enforced(self) -> "SessionHint":
        reviewer_roles = {
            PlanningRole.INTENT_REVIEWER,
            PlanningRole.HARD_GATE_REVIEWER,
            PlanningRole.CRITICAL_REVIEWER,
        }
        if self.role in reviewer_roles:
            if self.strategy is not SessionStrategy.ISOLATE or not self.independent_review_session:
                raise ValueError("intent/Hard Gate reviewer는 독립 세션이어야 합니다.")
        return self


class SearchOutcomeStatus(StrEnum):
    READY_FOR_REVIEW = "ready_for_review"
    NO_ADMISSIBLE = "no_admissible"
    BLOCKED = "blocked"
    FAILED = "failed"


class PlanningSearchOutcome(R31Model):
    run_id: str = Field(pattern=_ARTIFACT_ID_PATTERN)
    planning_input_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_primary: MissionPrimary
    status: SearchOutcomeStatus
    candidates: tuple[CandidateEnvelope, ...] = Field(max_length=5)
    top_k_candidate_ids: tuple[str, ...] = Field(default=(), max_length=2)
    selection_receipt: SelectionReceipt
    session_hints: tuple[SessionHint, ...] = ()
    model_call_receipts: tuple[ModelCallReceipt, ...] = ()
    failure_reasons: tuple[str, ...] = ()

    @field_validator("top_k_candidate_ids", "failure_reasons")
    @classmethod
    def outcome_entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "search outcome")

    @model_validator(mode="after")
    def outcome_graph_is_consistent(self) -> "PlanningSearchOutcome":
        if self.selection_receipt.run_id != self.run_id:
            raise ValueError("SelectionReceipt의 run_id가 search outcome과 다릅니다.")
        if self.selection_receipt.planning_input_digest != self.planning_input_digest:
            raise ValueError("SelectionReceipt의 planning input digest가 다릅니다.")
        identifiers = [candidate.candidate_id for candidate in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("candidate ID가 중복됐습니다.")
        by_id = {candidate.candidate_id: candidate for candidate in self.candidates}
        for candidate in self.candidates:
            if candidate.planning_input_digest != self.planning_input_digest:
                raise ValueError("서로 다른 planning input의 후보를 함께 비교할 수 없습니다.")
            if candidate.mission_primary is not self.mission_primary:
                raise ValueError("서로 다른 Mission의 후보를 함께 비교할 수 없습니다.")
            if candidate.parent_candidate_id is not None:
                parent = by_id.get(candidate.parent_candidate_id)
                if parent is None or parent.version >= candidate.version:
                    raise ValueError("candidate lineage의 parent가 없거나 version 순서가 잘못됐습니다.")
        top_k = set(self.top_k_candidate_ids)
        if not top_k.issubset(by_id):
            raise ValueError("Top-K가 알 수 없는 candidate를 참조합니다.")
        if any(
            by_id[candidate_id].status
            not in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}
            for candidate_id in top_k
        ):
            raise ValueError("Hard Gate 미통과 candidate는 Top-K에 들어갈 수 없습니다.")
        signatures = [by_id[candidate_id].approach.signature for candidate_id in self.top_k_candidate_ids]
        if len(signatures) != len(set(signatures)):
            raise ValueError("동일 signature cluster의 후보를 Top-K에 함께 둘 수 없습니다.")
        ranked = set(self.selection_receipt.ranked_candidate_ids)
        if ranked != top_k:
            raise ValueError("SelectionReceipt ranking은 Top-K와 같은 후보 집합이어야 합니다.")
        if not set(self.selection_receipt.pruned_duplicate_candidate_ids).issubset(by_id):
            raise ValueError("SelectionReceipt가 알 수 없는 중복 제거 후보를 참조합니다.")
        referenced = {
            candidate_id
            for candidate_id in (
                self.selection_receipt.selected_candidate_id,
                self.selection_receipt.recommended_candidate_id,
            )
            if candidate_id is not None
        }
        if not referenced.issubset(top_k):
            raise ValueError("SelectionReceipt가 Top-K 밖의 candidate를 참조합니다.")
        selected = self.selection_receipt.selected_candidate_id
        selected_status_ids = {
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.status is CandidateStatus.SELECTED
        }
        if selected is None:
            if selected_status_ids:
                raise ValueError("SelectionReceipt 없이 selected 상태 candidate가 있습니다.")
        elif selected_status_ids != {selected}:
            raise ValueError("SelectionReceipt와 selected candidate 상태가 다릅니다.")
        if self.status is SearchOutcomeStatus.READY_FOR_REVIEW:
            if not self.top_k_candidate_ids:
                raise ValueError("ready_for_review에는 admissible Top-K가 필요합니다.")
        else:
            if self.top_k_candidate_ids:
                raise ValueError("미완료 search outcome에는 Top-K를 둘 수 없습니다.")
            if not self.failure_reasons:
                raise ValueError("미완료 search outcome에는 이유가 필요합니다.")
        return self

    @property
    def outcome_digest(self) -> str:
        return sha256_digest(self)
