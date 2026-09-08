from __future__ import annotations

from .model_lock import OperationalBinding

from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from ..canonical import sha256_digest


ENGINE_SCHEMA_VERSION = "1.0"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_ENTITY_ID_PATTERN = r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$"
_LOCAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"


def new_id(prefix: str) -> str:
    normalized = prefix.strip().casefold().replace("-", "_")
    if not normalized or not normalized[0].isalpha() or not normalized.replace("_", "").isalnum():
        raise ValueError("ID prefix는 영문자로 시작하는 영숫자·밑줄 값이어야 합니다.")
    return f"{normalized}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unique(values: tuple[Any, ...], label: str) -> tuple[Any, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 항목이 중복됐습니다.")
    return values


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("시간 값에는 timezone이 필요합니다.")
    return value


def _assert_acyclic(nodes: set[str], edges: tuple[tuple[str, str], ...]) -> None:
    graph = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for producer, consumer in edges:
        graph[producer].append(consumer)
        indegree[consumer] += 1
    queue = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while queue:
        node = queue.pop(0)
        visited += 1
        for consumer in sorted(graph[node]):
            indegree[consumer] -= 1
            if indegree[consumer] == 0:
                queue.append(consumer)
                queue.sort()
    if visited != len(nodes):
        raise ValueError("Task dependency graph에 cycle이 있습니다.")


class EngineModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
    )


class LifecycleStage(StrEnum):
    PROTOTYPE = "prototype"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Criticality(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MissionClass(StrEnum):
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


class BehaviorPolicy(StrEnum):
    PRESERVE_OBSERVED_BEHAVIOR = "preserve_observed_behavior"
    PRESERVE_PUBLIC_CONTRACTS = "preserve_public_contracts"
    ALLOW_EXPLICIT_BREAKING_CHANGES = "allow_explicit_breaking_changes"
    NOT_APPLICABLE = "not_applicable"


class RevisionStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"
    NEEDS_INPUT = "needs_input"
    CONFLICT = "conflict"


class ProjectProfileDefinition(EngineModel):
    product_goal: str = Field(min_length=1, max_length=5000)
    lifecycle_stage: LifecycleStage
    criticality: Criticality
    compatibility_policy: str = Field(min_length=1, max_length=2000)
    validation_policy: tuple[str, ...] = Field(min_length=1)
    runtime_requirements: tuple[str, ...] = ()
    risk_defaults: tuple[str, ...] = ()
    context_source_refs: tuple[str, ...] = ()

    @field_validator(
        "validation_policy",
        "runtime_requirements",
        "risk_defaults",
        "context_source_refs",
    )
    @classmethod
    def entries_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "ProjectProfile")

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class ProjectProfileRevision(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    profile_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    definition: ProjectProfileDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: RevisionStatus
    supersedes_profile_revision_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def definition_is_bound(self) -> "ProjectProfileRevision":
        if self.definition_digest != self.definition.definition_digest:
            raise ValueError("ProjectProfile definition digest가 실제 내용과 다릅니다.")
        if self.supersedes_profile_revision_id == self.profile_revision_id:
            raise ValueError("ProjectProfile revision은 자신을 supersede할 수 없습니다.")
        return self

    @property
    def revision_digest(self) -> str:
        return sha256_digest(self)

class SourceTrace(EngineModel):
    trace_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    source_ref: str = Field(min_length=1, max_length=2000)
    statement: str = Field(min_length=1, max_length=5000)
    source_digest: str = Field(pattern=_DIGEST_PATTERN)


class GoalCriterion(EngineModel):
    criterion_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    validation_intent: str = Field(min_length=1, max_length=5000)
    trace_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("trace_refs")
    @classmethod
    def traces_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "GoalCriterion trace")


class QualityPreference(EngineModel):
    preference_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    weight: int = Field(ge=1, le=100)
    trace_refs: tuple[str, ...] = ()

    @field_validator("trace_refs")
    @classmethod
    def traces_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "QualityPreference trace")


class GoalConstraint(EngineModel):
    constraint_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    category: str = Field(min_length=1, max_length=80)
    statement: str = Field(min_length=1, max_length=5000)
    trace_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("trace_refs")
    @classmethod
    def traces_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "GoalConstraint trace")


class GoalAssumption(EngineModel):
    assumption_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    validation_required: bool = True


class GoalQuestion(EngineModel):
    question_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    question: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    blocking: bool = True


class EffectPolicy(EngineModel):
    mutation_policy: MutationPolicy
    behavior_policy: BehaviorPolicy
    allowed_external_effects: tuple[str, ...] = ()
    prohibited_effects: tuple[str, ...] = ()
    irreversible_effects_require_checkpoint: bool = True

    @field_validator("allowed_external_effects", "prohibited_effects")
    @classmethod
    def effects_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "effect policy")

    @model_validator(mode="after")
    def effects_do_not_overlap(self) -> "EffectPolicy":
        overlap = set(self.allowed_external_effects) & set(self.prohibited_effects)
        if overlap:
            raise ValueError(f"같은 외부 효과를 허용·금지할 수 없습니다: {sorted(overlap)}")
        return self


class GoalContractDefinition(EngineModel):
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    source_request: str = Field(min_length=1, max_length=50_000)
    source_request_digest: str = Field(pattern=_DIGEST_PATTERN)
    mission_class: MissionClass
    observable_outcome: str = Field(min_length=1, max_length=5000)
    hard_acceptance: tuple[GoalCriterion, ...] = Field(min_length=1)
    quality_preferences: tuple[QualityPreference, ...] = ()
    constraints: tuple[GoalConstraint, ...] = ()
    non_goals: tuple[str, ...] = ()
    assumptions: tuple[GoalAssumption, ...] = ()
    unresolved_questions: tuple[GoalQuestion, ...] = ()
    approved_decision_refs: tuple[str, ...] = ()
    source_traces: tuple[SourceTrace, ...] = Field(min_length=1)
    effect_policy: EffectPolicy
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)

    @field_validator("non_goals", "approved_decision_refs")
    @classmethod
    def text_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "GoalContract")

    @model_validator(mode="after")
    def goal_graph_is_consistent(self) -> "GoalContractDefinition":
        from ..canonical import sha256_bytes

        if self.source_request_digest != sha256_bytes(self.source_request.encode("utf-8")):
            raise ValueError("source_request_digest가 사용자 원문과 다릅니다.")
        collections = {
            "criterion": [item.criterion_id for item in self.hard_acceptance],
            "preference": [item.preference_id for item in self.quality_preferences],
            "constraint": [item.constraint_id for item in self.constraints],
            "assumption": [item.assumption_id for item in self.assumptions],
            "question": [item.question_id for item in self.unresolved_questions],
            "trace": [item.trace_id for item in self.source_traces],
        }
        for label, identifiers in collections.items():
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"GoalContract {label} ID가 중복됐습니다.")
        known_traces = set(collections["trace"])
        referenced = {
            trace
            for item in (*self.hard_acceptance, *self.quality_preferences, *self.constraints)
            for trace in item.trace_refs
        }
        unknown = referenced - known_traces
        if unknown:
            raise ValueError(f"GoalContract가 알 수 없는 source trace를 참조합니다: {sorted(unknown)}")
        hard_ids = set(collections["criterion"])
        other_ids = {
            *collections["preference"],
            *collections["constraint"],
            *collections["assumption"],
            *collections["question"],
        }
        if hard_ids & other_ids:
            raise ValueError("GoalContract의 로컬 ID는 종류 사이에서도 고유해야 합니다.")
        if self.mission_class is MissionClass.ANALYSIS_AUDIT and self.effect_policy.mutation_policy is not MutationPolicy.READ_ONLY:
            raise ValueError("analysis_audit Goal은 read_only mutation policy여야 합니다.")
        return self

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class GoalReviewFindingBinding(EngineModel):
    finding_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,79}$")
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    affected_task_refs: tuple[str, ...] = ()
    remediable: bool

    @model_validator(mode="after")
    def refs_are_unique(self) -> "GoalReviewFindingBinding":
        _unique(self.evidence_refs, "Goal finding evidence ref")
        _unique(self.affected_task_refs, "Goal finding affected task ref")
        return self


class GoalReviewRatingsBinding(EngineModel):
    goal_fit: int = Field(ge=0, le=4)
    grounding: int = Field(ge=0, le=4)
    engineering: int = Field(ge=0, le=4)
    verification: int = Field(ge=0, le=4)
    execution_safety: int = Field(ge=0, le=4)


class GoalPreparationBinding(EngineModel):
    normalization_proposal_digest: str = Field(pattern=_DIGEST_PATTERN)
    reviewer_submission_digest: str = Field(pattern=_DIGEST_PATTERN)
    reviewer_role: str = Field(min_length=1, max_length=100)
    finding_codes: tuple[str, ...] = ()
    finding_evidence_refs: tuple[str, ...] = ()
    findings: tuple[GoalReviewFindingBinding, ...] = ()
    ratings: GoalReviewRatingsBinding | None = None

    @model_validator(mode="after")
    def review_refs_are_unique(self) -> "GoalPreparationBinding":
        _unique(self.finding_codes, "Goal review finding code")
        _unique(self.finding_evidence_refs, "Goal review evidence ref")
        if self.finding_codes != tuple(sorted(item.finding_code for item in self.findings)):
            raise ValueError("Goal review finding code 집계가 finding과 다릅니다.")
        evidence = tuple(sorted({ref for item in self.findings for ref in item.evidence_refs}))
        if self.finding_evidence_refs != evidence:
            raise ValueError("Goal review evidence 집계가 finding과 다릅니다.")
        if self.findings and self.ratings is not None:
            raise ValueError("Goal review finding과 rating을 함께 결속할 수 없습니다.")
        if not self.findings and self.ratings is None:
            raise ValueError("clean Goal review에는 rating 결속이 필요합니다.")
        return self


class GoalContractRevision(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    goal_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    definition: GoalContractDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: RevisionStatus
    preparation_binding: GoalPreparationBinding | None = None
    supersedes_goal_revision_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def definition_is_bound(self) -> "GoalContractRevision":
        if self.definition_digest != self.definition.definition_digest:
            raise ValueError("GoalContract definition digest가 실제 내용과 다릅니다.")
        if self.supersedes_goal_revision_id == self.goal_revision_id:
            raise ValueError("Goal revision은 자신을 supersede할 수 없습니다.")
        blocking_questions = [item for item in self.definition.unresolved_questions if item.blocking]
        if self.status is RevisionStatus.READY and blocking_questions:
            raise ValueError("blocking 질문이 남은 GoalContract는 ready일 수 없습니다.")
        if self.status is RevisionStatus.NEEDS_INPUT and not blocking_questions:
            raise ValueError("needs_input GoalContract에는 blocking 질문이 필요합니다.")
        if self.status is RevisionStatus.CONFLICT:
            if self.preparation_binding is None or not self.preparation_binding.finding_codes:
                raise ValueError("conflict GoalContract에는 reviewer finding 결속이 필요합니다.")
        elif self.preparation_binding is not None and self.preparation_binding.finding_codes:
            raise ValueError("reviewer finding이 남은 GoalContract는 conflict여야 합니다.")
        return self

    @property
    def revision_digest(self) -> str:
        return sha256_digest(self)


class EvidenceFreshness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


class StateFact(EngineModel):
    fact_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    predicate: str = Field(min_length=1, max_length=2000)
    value: Any
    source_ref: str = Field(min_length=1, max_length=2000)
    evidence_digest: str = Field(pattern=_DIGEST_PATTERN)
    confidence_basis: Literal["observed", "derived"] = "observed"
    freshness: EvidenceFreshness = EvidenceFreshness.CURRENT
    invalidates_on: tuple[str, ...] = ()

    @field_validator("invalidates_on")
    @classmethod
    def invalidators_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "StateFact invalidation")


class StateUnknown(EngineModel):
    unknown_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    predicate: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    blocking_if_required: bool = True


class StateSnapshot(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    snapshot_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    version: int = Field(ge=1)
    scope_fingerprint: str = Field(pattern=_DIGEST_PATTERN)
    facts: tuple[StateFact, ...] = ()
    unknowns: tuple[StateUnknown, ...] = ()
    observed_at: datetime

    _observed_at_is_aware = field_validator("observed_at")(_aware)

    @model_validator(mode="after")
    def state_entries_are_unique(self) -> "StateSnapshot":
        fact_ids = [item.fact_id for item in self.facts]
        unknown_ids = [item.unknown_id for item in self.unknowns]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("State fact ID가 중복됐습니다.")
        if len(unknown_ids) != len(set(unknown_ids)):
            raise ValueError("State unknown ID가 중복됐습니다.")
        if set(fact_ids) & set(unknown_ids):
            raise ValueError("State fact와 unknown ID가 겹칩니다.")
        return self

    @property
    def snapshot_digest(self) -> str:
        return sha256_digest(self)

    @property
    def semantic_digest(self) -> str:
        return sha256_digest(
            {
                "project_id": self.project_id,
                "goal_contract_digest": self.goal_contract_digest,
                "scope_fingerprint": self.scope_fingerprint,
                "facts": sorted(
                    (item.model_dump(mode="json") for item in self.facts),
                    key=lambda item: item["fact_id"],
                ),
                "unknowns": sorted(
                    (item.model_dump(mode="json") for item in self.unknowns),
                    key=lambda item: item["unknown_id"],
                ),
            }
        )


class StateDelta(EngineModel):
    from_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    to_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    added_fact_ids: tuple[str, ...] = ()
    changed_fact_ids: tuple[str, ...] = ()
    removed_fact_ids: tuple[str, ...] = ()
    related_to_active_contract: bool
    rationale: str = Field(min_length=1, max_length=5000)

    @field_validator("added_fact_ids", "changed_fact_ids", "removed_fact_ids")
    @classmethod
    def delta_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "StateDelta")

    @model_validator(mode="after")
    def delta_sets_do_not_overlap(self) -> "StateDelta":
        groups = [set(self.added_fact_ids), set(self.changed_fact_ids), set(self.removed_fact_ids)]
        if any(groups[index] & groups[other] for index in range(3) for other in range(index + 1, 3)):
            raise ValueError("StateDelta의 added/changed/removed 집합이 겹칩니다.")
        return self


class ProjectMapEntryKind(StrEnum):
    """관측한 파일의 역할 표지다. 범용 의존 그래프의 노드 종류가 아니다."""

    FILE = "file"
    TEST = "test"
    BUILD = "build"
    INSTRUCTION = "instruction"
    CONFIG = "config"
    REFERENCE = "reference"


class ProjectMapEntry(EngineModel):
    """파일·추출한 symbol·확인된 entry 간 링크만 담는 ProjectMap 항목이다."""

    entry_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: ProjectMapEntryKind
    path: str = Field(min_length=1, max_length=2000)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    symbols: tuple[str, ...] = ()
    observed_link_refs: tuple[str, ...] = ()
    # 동결된 1.0 ProjectMap의 canonical payload를 보존하는 읽기 전용 필드다.
    dependency_refs: tuple[str, ...] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    tags: tuple[str, ...] = ()

    @field_validator("symbols", "observed_link_refs", "tags")
    @classmethod
    def entry_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "ProjectMap entry")

    @field_validator("dependency_refs")
    @classmethod
    def legacy_dependency_values_are_unique(
        cls,
        value: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        return None if value is None else _unique(value, "legacy ProjectMap dependency")

    @model_validator(mode="after")
    def link_vocabularies_do_not_mix(self) -> "ProjectMapEntry":
        if self.dependency_refs is not None and self.observed_link_refs:
            raise ValueError("legacy dependency_refs와 observed_link_refs를 함께 사용할 수 없습니다.")
        return self

    @model_serializer(mode="wrap")
    def preserve_explicit_link_vocabulary(self, handler: Any) -> dict[str, Any]:
        payload = handler(self)
        if "observed_link_refs" not in self.model_fields_set:
            payload.pop("observed_link_refs", None)
        if "dependency_refs" not in self.model_fields_set:
            payload.pop("dependency_refs", None)
        return payload

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        schema.get("properties", {}).pop("dependency_refs", None)
        return schema


class ProjectMapRevision(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    project_map_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    root: str = Field(min_length=1, max_length=2000)
    entries: tuple[ProjectMapEntry, ...]
    instruction_source_refs: tuple[str, ...] = ()
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def entries_are_consistent(self) -> "ProjectMapRevision":
        ids = [item.entry_id for item in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("ProjectMap entry ID가 중복됐습니다.")
        known = set(ids)
        missing = {
            link
            for item in self.entries
            for link in (item.observed_link_refs or item.dependency_refs or ())
            if link not in known
        }
        if missing:
            raise ValueError(f"ProjectMap observed link가 알 수 없는 entry를 참조합니다: {sorted(missing)}")
        if not set(self.instruction_source_refs).issubset(known):
            raise ValueError("ProjectMap instruction source가 알려진 entry가 아닙니다.")
        _unique(self.instruction_source_refs, "ProjectMap instruction source")
        return self

    @property
    def revision_digest(self) -> str:
        return sha256_digest(self)

    @property
    def semantic_digest(self) -> str:
        return sha256_digest(
            {
                "project_id": self.project_id,
                "root": self.root,
                "entries": sorted(
                    (item.model_dump(mode="json") for item in self.entries),
                    key=lambda item: item["entry_id"],
                ),
                "instruction_source_refs": sorted(self.instruction_source_refs),
            }
        )


class TaskKind(StrEnum):
    INSPECT = "inspect"
    DECIDE = "decide"
    CHANGE = "change"
    VALIDATE = "validate"
    APPROVE = "approve"
    RECOVER = "recover"


class DependencyType(StrEnum):
    DATA = "data"
    CONTROL = "control"
    VALIDATION = "validation"
    APPROVAL = "approval"
    RESOURCE = "resource"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalClass(StrEnum):
    PLAN_ACTIVATION = "plan_activation"
    EXECUTION_CHECKPOINT = "execution_checkpoint"
    NONE = "none"


class TaskSkeleton(EngineModel):
    task_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: TaskKind
    objective: str = Field(min_length=1, max_length=5000)
    contributes_to: tuple[str, ...] = Field(min_length=1)
    produces: tuple[str, ...] = Field(min_length=1)
    consumes: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    no_op_when: tuple[str, ...] = ()
    unknown_refs: tuple[str, ...] = ()
    detail_requirements: tuple[str, ...] = ()

    @field_validator(
        "contributes_to",
        "produces",
        "consumes",
        "risk_tags",
        "required_capabilities",
        "no_op_when",
        "unknown_refs",
        "detail_requirements",
    )
    @classmethod
    def task_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "TaskSkeleton")


class SkeletonDependency(EngineModel):
    producer_task_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    consumer_task_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    dependency_type: DependencyType
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()

    @field_validator("produces", "consumes")
    @classmethod
    def products_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "Skeleton dependency")

    @model_validator(mode="after")
    def dependency_is_valid(self) -> "SkeletonDependency":
        if self.producer_task_ref == self.consumer_task_ref:
            raise ValueError("Task는 자신에게 의존할 수 없습니다.")
        if self.dependency_type is DependencyType.DATA:
            if not self.produces or not self.consumes:
                raise ValueError("data dependency에는 produces와 consumes가 필요합니다.")
            missing = set(self.consumes) - set(self.produces)
            if missing:
                raise ValueError(f"data dependency가 생산되지 않은 값을 소비합니다: {sorted(missing)}")
        return self


class GoalCoverage(EngineModel):
    criterion_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("task_refs")
    @classmethod
    def tasks_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "Goal coverage")


class ApproachSignature(EngineModel):
    strategy_family: str = Field(min_length=1, max_length=200)
    change_shape: str = Field(min_length=1, max_length=200)
    compatibility: str = Field(min_length=1, max_length=200)
    rollout_recovery: str = Field(min_length=1, max_length=200)

    @property
    def signature_digest(self) -> str:
        return sha256_digest(
            {
                "strategy_family": self.strategy_family.casefold(),
                "change_shape": self.change_shape.casefold(),
                "compatibility": self.compatibility.casefold(),
                "rollout_recovery": self.rollout_recovery.casefold(),
            }
        )


class PlanSkeletonCandidate(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    candidate_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    state_signature: str = Field(pattern=_DIGEST_PATTERN)
    approach: ApproachSignature
    tasks: tuple[TaskSkeleton, ...] = Field(min_length=1)
    dependencies: tuple[SkeletonDependency, ...] = ()
    goal_coverage: tuple[GoalCoverage, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = ()
    estimated_change_cost: int = Field(ge=0)
    estimated_context_tokens: int = Field(ge=0)
    parent_candidate_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    version: int = Field(default=1, ge=1, le=5)
    refinement_round: int = Field(default=0, ge=0, le=1)

    @field_validator("unknowns")
    @classmethod
    def unknowns_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "PlanSkeleton unknown")

    @model_validator(mode="after")
    def graph_is_consistent(self) -> "PlanSkeletonCandidate":
        task_refs = [item.task_ref for item in self.tasks]
        if len(task_refs) != len(set(task_refs)):
            raise ValueError("PlanSkeleton task_ref가 중복됐습니다.")
        known = set(task_refs)
        edge_pairs = []
        for edge in self.dependencies:
            if edge.producer_task_ref not in known or edge.consumer_task_ref not in known:
                raise ValueError("PlanSkeleton dependency가 알 수 없는 Task를 참조합니다.")
            edge_pairs.append((edge.producer_task_ref, edge.consumer_task_ref))
        if len(edge_pairs) != len(set(edge_pairs)):
            raise ValueError("PlanSkeleton dependency가 중복됐습니다.")
        _assert_acyclic(known, tuple(edge_pairs))
        coverage_ids = [item.criterion_id for item in self.goal_coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("PlanSkeleton goal coverage가 중복됐습니다.")
        if any(not set(item.task_refs).issubset(known) for item in self.goal_coverage):
            raise ValueError("PlanSkeleton coverage가 알 수 없는 Task를 참조합니다.")
        produced_by_task = {item.task_ref: set(item.produces) for item in self.tasks}
        consumed_by_task = {item.task_ref: set(item.consumes) for item in self.tasks}
        for edge in self.dependencies:
            if edge.dependency_type is DependencyType.DATA:
                if not set(edge.produces).issubset(produced_by_task[edge.producer_task_ref]):
                    raise ValueError("dependency produces가 producer Task 계약에 없습니다.")
                if not set(edge.consumes).issubset(consumed_by_task[edge.consumer_task_ref]):
                    raise ValueError("dependency consumes가 consumer Task 계약에 없습니다.")
        if self.version == 1 and self.parent_candidate_id is not None:
            raise ValueError("초기 skeleton에는 parent candidate가 없어야 합니다.")
        if self.version > 1 and (self.parent_candidate_id is None or self.refinement_round != 1):
            raise ValueError("정제 skeleton에는 parent와 refinement_round=1이 필요합니다.")
        return self

    @property
    def graph_signature(self) -> str:
        return sha256_digest(
            {
                "approach": self.approach.signature_digest,
                "tasks": sorted(
                    (
                        {
                            "kind": item.kind.value,
                            "objective": item.objective.casefold(),
                            "contributes_to": sorted(item.contributes_to),
                            "produces": sorted(item.produces),
                            "consumes": sorted(item.consumes),
                        }
                        for item in self.tasks
                    ),
                    key=lambda item: (item["kind"], item["objective"]),
                ),
                "edges": sorted(
                    (
                        item.producer_task_ref,
                        item.consumer_task_ref,
                        item.dependency_type.value,
                    )
                    for item in self.dependencies
                ),
            }
        )


class GateName(StrEnum):
    SCHEMA = "schema"
    GOAL = "goal"
    GROUNDING = "grounding"
    INTENT = "intent"
    PLAN = "plan"
    ENGINEERING = "engineering"
    VERIFICATION = "verification"
    EXECUTION = "execution"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ReviewFinding(EngineModel):
    """Reviewer가 제출할 수 있는 최소 증거 기반 진단이다.

    admission 상태나 점수는 의도적으로 포함하지 않는다. 그 값은 Core가
    이 구조화된 finding으로부터 계산한다.
    """

    finding_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,79}$")
    gate: GateName
    severity: FindingSeverity
    summary: str = Field(min_length=1, max_length=2000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    affected_task_refs: tuple[str, ...] = ()
    remediable: bool

    @field_validator("evidence_refs", "affected_task_refs")
    @classmethod
    def finding_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "review finding")

    @property
    def blocking(self) -> bool:
        return self.severity in {FindingSeverity.ERROR, FindingSeverity.CRITICAL}


class ReviewRatings(EngineModel):
    goal_fit: int = Field(ge=0, le=4)
    grounding: int = Field(ge=0, le=4)
    engineering: int = Field(ge=0, le=4)
    verification: int = Field(ge=0, le=4)
    execution_safety: int = Field(ge=0, le=4)

    @property
    def fitness_score(self) -> int:
        values = (
            self.goal_fit,
            self.grounding,
            self.engineering,
            self.verification,
            self.execution_safety,
        )
        return round(sum(values) * 100 / 20)


class ReviewerSubmission(EngineModel):
    reviewer_role: str = Field(min_length=1, max_length=100)
    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    findings: tuple[ReviewFinding, ...] = ()
    ratings: ReviewRatings | None = None
    evidence_catalog_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def rating_is_only_for_clean_review(self) -> "ReviewerSubmission":
        if self.findings and self.ratings is not None:
            raise ValueError("finding과 fitness rating을 함께 제출할 수 없습니다.")
        if not self.findings and self.ratings is None:
            raise ValueError("finding이 없는 review에는 fitness rating이 필요합니다.")
        _unique(tuple(item.finding_code for item in self.findings), "review finding code")
        return self


def validate_reviewer_submission_evidence(
    submission: ReviewerSubmission,
    *,
    evidence_catalog: dict[str, Any],
    known_task_refs: set[str] | None = None,
) -> None:
    """Reviewer의 ref가 실제로 제공한 evidence catalog를 가리키는지 검사한다."""

    expected_digest = sha256_digest(evidence_catalog)
    if submission.evidence_catalog_digest != expected_digest:
        raise ValueError("Reviewer evidence catalog digest가 실제 입력과 다릅니다.")
    known_evidence = set(evidence_catalog)
    unknown_evidence = {
        evidence_ref
        for finding in submission.findings
        for evidence_ref in finding.evidence_refs
        if evidence_ref not in known_evidence
    }
    if unknown_evidence:
        raise ValueError(
            f"Reviewer finding이 제공되지 않은 evidence를 참조합니다: {sorted(unknown_evidence)}"
        )
    if known_task_refs is not None:
        unknown_tasks = {
            task_ref
            for finding in submission.findings
            for task_ref in finding.affected_task_refs
            if task_ref not in known_task_refs
        }
        if unknown_tasks:
            raise ValueError(
                f"Reviewer finding이 알 수 없는 Task를 참조합니다: {sorted(unknown_tasks)}"
            )


class CandidateStatus(StrEnum):
    GENERATED = "generated"
    NEEDS_REVISION = "needs_revision"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    ADMISSIBLE = "admissible"
    SELECTED = "selected"


class CandidateDecision(EngineModel):
    candidate_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: CandidateStatus
    finding_codes: tuple[str, ...] = ()
    fitness_score: int | None = Field(default=None, ge=0, le=100)
    weakest_dimension: str | None = None
    derived_by: Literal["flowmarshal.engine.core"] = "flowmarshal.engine.core"

    @field_validator("finding_codes")
    @classmethod
    def finding_codes_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "candidate finding")

    @model_validator(mode="after")
    def decision_is_consistent(self) -> "CandidateDecision":
        scored = self.status in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}
        if scored != (self.fitness_score is not None):
            raise ValueError("admissible/selected 후보만 fitness score를 가질 수 있습니다.")
        if scored and self.finding_codes:
            raise ValueError("finding이 남은 후보를 admissible로 판정할 수 없습니다.")
        return self


def derive_candidate_decision(
    *,
    candidate_digest: str,
    findings: tuple[ReviewFinding, ...],
    ratings: ReviewRatings | None,
) -> CandidateDecision:
    """Reviewer 서술과 무관하게 Core가 admission을 결정한다."""

    blocking = tuple(item for item in findings if item.blocking)
    codes = tuple(sorted({item.finding_code for item in findings}))
    if blocking:
        status = (
            CandidateStatus.NEEDS_REVISION
            if all(item.remediable for item in blocking)
            else CandidateStatus.BLOCKED
        )
        return CandidateDecision(
            candidate_digest=candidate_digest,
            status=status,
            finding_codes=codes,
        )
    if findings:
        return CandidateDecision(
            candidate_digest=candidate_digest,
            status=CandidateStatus.REJECTED,
            finding_codes=codes,
        )
    if ratings is None:
        return CandidateDecision(
            candidate_digest=candidate_digest,
            status=CandidateStatus.REJECTED,
            finding_codes=("MISSING_RATINGS",),
        )
    dimensions = {
        "goal_fit": ratings.goal_fit,
        "grounding": ratings.grounding,
        "engineering": ratings.engineering,
        "verification": ratings.verification,
        "execution_safety": ratings.execution_safety,
    }
    weakest = min(dimensions, key=lambda name: (dimensions[name], name))
    return CandidateDecision(
        candidate_digest=candidate_digest,
        status=CandidateStatus.ADMISSIBLE,
        fitness_score=ratings.fitness_score,
        weakest_dimension=weakest,
    )


class PreconditionContract(EngineModel):
    precondition_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=3000)
    evidence_required: bool = True


class EffectContract(EngineModel):
    effect_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=3000)
    external: bool = Field(default=False, description="프로젝트 파일 변경이나 함수 동작이 아닌 외부 시스템·계정·제3자에 대한 효과 여부")
    reversible: bool = True


class EvidenceKind(StrEnum):
    FILE = "file"
    DIFF = "diff"
    COMMAND = "command"
    TEST = "test"
    BUILD = "build"
    MODEL_REVIEW = "model_review"
    USER_DECISION = "user_decision"
    EXTERNAL_OBSERVATION = "external_observation"


def _known_evidence_kind(value: str) -> str:
    try:
        EvidenceKind(value)
    except ValueError as error:
        raise ValueError(f"알 수 없는 evidence kind입니다: {value}") from error
    return value


# 기존 문자열 계약과 canonical digest를 보존하면서 provider와 Core에 같은 집합을 공개한다.
EvidenceKindName = Annotated[
    str,
    AfterValidator(_known_evidence_kind),
    Field(json_schema_extra={"enum": [item.value for item in EvidenceKind]}),
]


class ValidationContract(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=3000)
    method: Literal["deterministic", "semantic", "manual", "external_observation"]
    required_evidence_kinds: tuple[EvidenceKindName, ...] = Field(min_length=1)

    @field_validator("required_evidence_kinds")
    @classmethod
    def evidence_kinds_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "validation evidence kind")


class RecoveryEnvelope(EngineModel):
    max_same_failure_replans: int = Field(default=2, ge=0, le=10)
    max_goal_replans: int = Field(default=5, ge=0, le=20)
    retryable_failure_classes: tuple[str, ...] = ()
    requires_new_evidence: bool = True
    resume_strategy: Literal["existing_binding_first", "new_attempt_only"] = "existing_binding_first"

    @field_validator("retryable_failure_classes")
    @classmethod
    def failure_classes_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "recovery failure class")


class ModelFallback(EngineModel):
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)


class RoleAssignmentPolicy(EngineModel):
    role: str = Field(min_length=1, max_length=100)
    preferred_model: str = Field(min_length=1, max_length=200)
    preferred_effort: str = Field(min_length=1, max_length=50)
    allowed_fallbacks: tuple[ModelFallback, ...] = ()

    @model_validator(mode="after")
    def fallbacks_are_unique(self) -> "RoleAssignmentPolicy":
        values = tuple((item.model, item.effort) for item in self.allowed_fallbacks)
        _unique(values, "model fallback")
        if (self.preferred_model, self.preferred_effort) in values:
            raise ValueError("preferred model을 fallback에 중복할 수 없습니다.")
        return self


class ModelAssignmentContract(EngineModel):
    executor: RoleAssignmentPolicy
    validator: RoleAssignmentPolicy | None = None
    independence_required: bool = False

    @model_validator(mode="after")
    def validator_is_present_when_independent(self) -> "ModelAssignmentContract":
        if self.independence_required and self.validator is None:
            raise ValueError("독립 검사가 필요한 Task에는 validator 배정이 필요합니다.")
        return self


class TaskContract(EngineModel):
    task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    task_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    kind: TaskKind
    objective: str = Field(min_length=1, max_length=5000)
    goal_criterion_refs: tuple[str, ...] = Field(min_length=1)
    produces: tuple[str, ...] = Field(min_length=1)
    consumes: tuple[str, ...] = ()
    preconditions: tuple[PreconditionContract, ...] = ()
    expected_effects: tuple[EffectContract, ...] = ()
    prohibited_effects: tuple[EffectContract, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    validations: tuple[ValidationContract, ...] = Field(min_length=1)
    risk_level: RiskLevel
    risk_tags: tuple[str, ...] = ()
    approval_class: ApprovalClass = ApprovalClass.PLAN_ACTIVATION
    recovery: RecoveryEnvelope
    assignment: ModelAssignmentContract

    @field_validator(
        "goal_criterion_refs",
        "produces",
        "consumes",
        "required_capabilities",
        "acceptance_criteria",
        "risk_tags",
    )
    @classmethod
    def contract_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "TaskContract")

    @model_validator(mode="after")
    def task_contract_is_consistent(self) -> "TaskContract":
        for label, values in (
            ("precondition", tuple(item.precondition_id for item in self.preconditions)),
            ("expected effect", tuple(item.effect_id for item in self.expected_effects)),
            ("prohibited effect", tuple(item.effect_id for item in self.prohibited_effects)),
            ("validation", tuple(item.validation_id for item in self.validations)),
        ):
            _unique(values, f"TaskContract {label}")
        expected = {item.effect_id for item in self.expected_effects}
        prohibited = {item.effect_id for item in self.prohibited_effects}
        if expected & prohibited:
            raise ValueError("같은 effect ID를 기대·금지 효과에 함께 둘 수 없습니다.")
        irreversible_external = any(
            item.external and not item.reversible for item in self.expected_effects
        )
        if irreversible_external and self.approval_class is not ApprovalClass.EXECUTION_CHECKPOINT:
            raise ValueError("비가역 외부 효과에는 execution checkpoint가 필요합니다.")
        if (
            self.approval_class is ApprovalClass.EXECUTION_CHECKPOINT
            and not any(item.external for item in self.expected_effects)
        ):
            raise ValueError("execution checkpoint는 외부 효과가 있는 Task에만 사용합니다.")
        return self

    @property
    def contract_digest(self) -> str:
        return sha256_digest(self)


class PlanDependency(EngineModel):
    producer_task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    consumer_task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    dependency_type: DependencyType
    products: tuple[str, ...] = ()

    @field_validator("products")
    @classmethod
    def products_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "Plan dependency products")

    @model_validator(mode="after")
    def dependency_is_not_self_referential(self) -> "PlanDependency":
        if self.producer_task_id == self.consumer_task_id:
            raise ValueError("Task는 자신에게 의존할 수 없습니다.")
        if self.dependency_type is DependencyType.DATA and not self.products:
            raise ValueError("data dependency에는 products가 필요합니다.")
        return self


class PlanGoalCoverage(EngineModel):
    criterion_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_ids: tuple[str, ...] = Field(min_length=1)
    validation_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("task_ids", "validation_ids")
    @classmethod
    def coverage_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "Plan goal coverage")


class IntegrationValidationContract(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    statement: str = Field(min_length=1, max_length=5000)
    criterion_refs: tuple[str, ...] = Field(min_length=1)
    method: Literal["deterministic", "semantic", "manual", "external_observation"]
    evidence_mode: Literal["independent", "task_aggregate"] = "independent"
    required_evidence_kinds: tuple[EvidenceKindName, ...] = Field(min_length=1)

    @field_validator("criterion_refs", "required_evidence_kinds")
    @classmethod
    def integration_values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "integration validation")

    @model_validator(mode="after")
    def aggregation_is_deterministic(self):
        if self.evidence_mode == "task_aggregate" and self.method != "deterministic":
            raise ValueError("Task evidence 집계는 deterministic Goal Test만 허용합니다.")
        return self


class _LegacyCommitHorizon(EngineModel):
    """동결된 1.0 Plan artifact를 읽기 위한 비공개 호환 타입."""

    max_ready_tasks: int = Field(default=1, ge=1, le=100)
    project_serial_execution: bool = True
    revalidate_snapshot_before_materialization: bool = True
    irreversible_effect_checkpoint: bool = True


class PlanningBudgetPolicy(EngineModel):
    max_logical_role_calls: int = Field(default=14, ge=1, le=100)
    max_initial_candidates: int = Field(default=3, ge=1, le=3)
    max_candidate_versions: int = Field(default=5, ge=1, le=5)
    max_shortlist: int = Field(default=2, ge=1, le=2)
    max_refinement_per_candidate: int = Field(default=1, ge=0, le=1)
    replan_reserve_percent: int = Field(default=25, ge=0, le=90)

    @model_validator(mode="after")
    def budget_limits_are_possible(self) -> "PlanningBudgetPolicy":
        if self.max_shortlist > self.max_initial_candidates:
            raise ValueError("shortlist 수가 초기 후보 수보다 클 수 없습니다.")
        if self.max_candidate_versions < self.max_initial_candidates:
            raise ValueError("candidate version 예산이 초기 후보 수보다 작습니다.")
        return self


class PlanContractDefinition(EngineModel):
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    base_state_snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    project_map_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_skeleton_digest: str = Field(pattern=_DIGEST_PATTERN)
    tasks: tuple[TaskContract, ...] = Field(min_length=1)
    dependencies: tuple[PlanDependency, ...] = ()
    goal_coverage: tuple[PlanGoalCoverage, ...] = Field(min_length=1)
    integration_validations: tuple[IntegrationValidationContract, ...] = Field(min_length=1)
    # 새 계약의 공개 스키마에서는 제거됐지만, 기존 동결 artifact의 digest와
    # canonical payload를 그대로 검증할 수 있도록 읽기 호환성만 유지한다.
    commit_horizon: _LegacyCommitHorizon | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    planning_budget: PlanningBudgetPolicy = PlanningBudgetPolicy()
    model_inventory_digest: str = Field(pattern=_DIGEST_PATTERN)
    expected_effects: tuple[str, ...] = ()
    prohibited_effects: tuple[str, ...] = ()

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        schema.get("properties", {}).pop("commit_horizon", None)
        return schema

    @field_validator("expected_effects", "prohibited_effects")
    @classmethod
    def plan_effects_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "PlanContract effect")

    @model_validator(mode="after")
    def plan_graph_is_consistent(self) -> "PlanContractDefinition":
        task_ids = [item.task_id for item in self.tasks]
        task_refs = [item.task_ref for item in self.tasks]
        if len(task_ids) != len(set(task_ids)) or len(task_refs) != len(set(task_refs)):
            raise ValueError("PlanContract Task ID/ref가 중복됐습니다.")
        if any(item.project_id != self.project_id for item in self.tasks):
            raise ValueError("PlanContract의 모든 Task는 같은 project에 속해야 합니다.")
        known_tasks = set(task_ids)
        edges: list[tuple[str, str]] = []
        edge_keys: list[tuple[str, str, DependencyType]] = []
        task_by_id = {item.task_id: item for item in self.tasks}
        for dependency in self.dependencies:
            if (
                dependency.producer_task_id not in known_tasks
                or dependency.consumer_task_id not in known_tasks
            ):
                raise ValueError("Plan dependency가 알 수 없는 Task를 참조합니다.")
            edges.append((dependency.producer_task_id, dependency.consumer_task_id))
            edge_keys.append(
                (
                    dependency.producer_task_id,
                    dependency.consumer_task_id,
                    dependency.dependency_type,
                )
            )
            if dependency.dependency_type is DependencyType.DATA:
                producer = task_by_id[dependency.producer_task_id]
                consumer = task_by_id[dependency.consumer_task_id]
                if not set(dependency.products).issubset(producer.produces):
                    raise ValueError("Plan dependency product가 producer 계약에 없습니다.")
                if not set(dependency.products).issubset(consumer.consumes):
                    raise ValueError("Plan dependency product가 consumer 계약에 없습니다.")
        _unique(tuple(edge_keys), "Plan dependency")
        _assert_acyclic(known_tasks, tuple(edges))

        coverage_ids = [item.criterion_id for item in self.goal_coverage]
        _unique(tuple(coverage_ids), "Plan goal coverage")
        task_validation_ids = {
            validation.validation_id
            for task in self.tasks
            for validation in task.validations
        }
        integration_ids = {
            validation.validation_id for validation in self.integration_validations
        }
        if len(integration_ids) != len(self.integration_validations):
            raise ValueError("Plan integration validation ID가 중복됐습니다.")
        known_validations = task_validation_ids | integration_ids
        for coverage in self.goal_coverage:
            if not set(coverage.task_ids).issubset(known_tasks):
                raise ValueError("Goal coverage가 알 수 없는 Task를 참조합니다.")
            if not set(coverage.validation_ids).issubset(known_validations):
                raise ValueError("Goal coverage가 알 수 없는 validation을 참조합니다.")
        for validation in self.integration_validations:
            if not set(validation.criterion_refs).issubset(set(coverage_ids)):
                raise ValueError("integration validation이 coverage 없는 criterion을 참조합니다.")
        if set(self.expected_effects) & set(self.prohibited_effects):
            raise ValueError("Plan의 기대 효과와 금지 효과가 겹칩니다.")
        return self

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class PlanContractRevision(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    plan_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    plan_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    definition: PlanContractDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: RevisionStatus = RevisionStatus.READY
    supersedes_plan_revision_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def plan_revision_is_bound(self) -> "PlanContractRevision":
        if self.definition_digest != self.definition.definition_digest:
            raise ValueError("PlanContract definition digest가 실제 내용과 다릅니다.")
        if self.supersedes_plan_revision_id == self.plan_revision_id:
            raise ValueError("Plan revision은 자신을 supersede할 수 없습니다.")
        if self.status not in {RevisionStatus.DRAFT, RevisionStatus.READY}:
            raise ValueError("저장되는 PlanContractRevision은 draft 또는 ready여야 합니다.")
        return self

    @property
    def activation_digest(self) -> str:
        """사용자가 정확히 지정해 활성화하는 불변 계약 digest."""

        return sha256_digest(
            {
                "schema_version": self.schema_version,
                "plan_revision_id": self.plan_revision_id,
                "plan_id": self.plan_id,
                "revision_no": self.revision_no,
                "definition_digest": self.definition_digest,
            }
        )


class ContextSourceKind(StrEnum):
    POLICY = "policy"
    PROJECT = "project"
    GOAL = "goal"
    STATE = "state"
    TASK = "task"
    CODE = "code"
    TEST = "test"
    REFERENCE = "reference"
    TOOL_OUTPUT = "tool_output"


class ContextSourceRegistrationKind(StrEnum):
    REFERENCE = "reference"
    INSTRUCTION = "instruction"


class ContextSourceRegistration(EngineModel):
    """프로젝트 밖 참고자료와 추가 instruction source의 불변 등록 레코드."""

    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    context_source_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    kind: ContextSourceRegistrationKind
    path: str = Field(min_length=1, max_length=2000)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    registered_at: datetime

    _registered_at_is_aware = field_validator("registered_at")(_aware)

    @property
    def registration_digest(self) -> str:
        return sha256_digest(self)


class ContextFragmentRef(EngineModel):
    fragment_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    source_kind: ContextSourceKind
    source_ref: str = Field(min_length=1, max_length=2000)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    selector: str = Field(min_length=1, max_length=2000)
    token_estimate: int = Field(ge=0)
    immutable: bool = False


class PromptBinding(EngineModel):
    static_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    project_prefix_digest: str = Field(pattern=_DIGEST_PATTERN)
    stage_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    dynamic_suffix_digest: str = Field(pattern=_DIGEST_PATTERN)

    @property
    def binding_digest(self) -> str:
        return sha256_digest(self)


class ContextManifest(EngineModel):
    context_pack_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    fragments: tuple[ContextFragmentRef, ...] = Field(min_length=1)
    prompt_binding: PromptBinding
    total_token_estimate: int = Field(ge=0)
    selection_rationale: tuple[str, ...] = Field(min_length=1)
    missing_context: tuple[str, ...] = ()

    @model_validator(mode="after")
    def context_manifest_is_consistent(self) -> "ContextManifest":
        ids = tuple(item.fragment_id for item in self.fragments)
        _unique(ids, "Context fragment")
        if self.total_token_estimate != sum(item.token_estimate for item in self.fragments):
            raise ValueError("Context token 합계가 fragment 합계와 다릅니다.")
        _unique(self.selection_rationale, "Context selection rationale")
        _unique(self.missing_context, "Context missing item")
        return self

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self)


class ResolvedTarget(EngineModel):
    target_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    path: str = Field(min_length=1, max_length=2000)
    symbol: str | None = Field(default=None, max_length=1000)
    expected_content_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    access: Literal["read", "write", "create", "delete"]


class ExecutionAction(EngineModel):
    action_ref: str = Field(pattern=_LOCAL_ID_PATTERN)
    kind: Literal["inspect", "command", "edit", "validate", "external_effect"]
    description: str = Field(min_length=1, max_length=3000)
    command: tuple[str, ...] = ()
    working_directory: str | None = Field(default=None, max_length=2000)
    effect_id: str | None = Field(default=None, pattern=_LOCAL_ID_PATTERN)

    @model_validator(mode="after")
    def action_fields_match_kind(self) -> "ExecutionAction":
        if self.kind == "command" and not self.command:
            raise ValueError("command action에는 argv가 필요합니다.")
        if self.kind != "command" and self.command:
            raise ValueError("command가 아닌 action에는 argv를 둘 수 없습니다.")
        if self.kind == "external_effect" and self.effect_id is None:
            raise ValueError("external_effect action에는 effect_id가 필요합니다.")
        return self


class ValidationExecutionStep(EngineModel):
    """ExecutionSpec에 고정되는 validation별 실제 관측 방법."""

    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    method: Literal["deterministic", "semantic", "manual", "external_observation"]
    argv: tuple[str, ...] = ()
    working_directory: str | None = Field(default=None, max_length=2000)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    required_evidence_kinds: tuple[EvidenceKindName, ...] = Field(min_length=1)
    semantic_instruction: str | None = Field(default=None, max_length=5000)
    manual_instruction: str | None = Field(default=None, max_length=5000)
    external_selector: str | None = Field(default=None, max_length=3000)
    artifact_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def fields_match_method(self) -> "ValidationExecutionStep":
        if any("\x00" in argument for argument in self.argv) or (self.argv and not self.argv[0]):
            raise ValueError("validation argv에는 NUL이나 빈 실행 파일을 둘 수 없습니다.")
        _unique(self.expected_exit_codes, "validation expected exit code")
        _unique(self.required_evidence_kinds, "validation evidence kind")
        _unique(self.artifact_paths, "validation artifact path")
        if any(not path or "\x00" in path for path in self.artifact_paths):
            raise ValueError("validation artifact path가 유효하지 않습니다.")
        if self.method != "deterministic" and self.artifact_paths:
            raise ValueError("직접 artifact 관측 경로는 deterministic validation에만 사용합니다.")
        if self.method == "deterministic":
            if not self.argv or self.working_directory is None:
                raise ValueError("deterministic validation에는 argv와 cwd가 필요합니다.")
            if not self.expected_exit_codes:
                raise ValueError("deterministic validation에는 예상 종료 코드가 필요합니다.")
            if any((self.semantic_instruction, self.manual_instruction, self.external_selector)):
                raise ValueError("deterministic validation에는 다른 관측 지시를 둘 수 없습니다.")
        elif self.method == "semantic":
            if not self.semantic_instruction:
                raise ValueError("semantic validation에는 독립 검사 지시가 필요합니다.")
            if self.argv or self.manual_instruction or self.external_selector:
                raise ValueError("semantic validation 필드 조합이 올바르지 않습니다.")
        elif self.method == "manual":
            if not self.manual_instruction:
                raise ValueError("manual validation에는 사용자 관측 지시가 필요합니다.")
            if self.argv or self.semantic_instruction or self.external_selector:
                raise ValueError("manual validation 필드 조합이 올바르지 않습니다.")
        else:
            if not self.external_selector:
                raise ValueError("external_observation에는 관측 selector가 필요합니다.")
            if self.argv or self.semantic_instruction or self.manual_instruction:
                raise ValueError("external_observation 필드 조합이 올바르지 않습니다.")
        return self


class ExecutionContextNeed(EngineModel):
    need_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    description: str = Field(min_length=1, max_length=2000)
    path_hints: tuple[str, ...] = ()
    symbol_hints: tuple[str, ...] = ()
    tag_hints: tuple[str, ...] = ()
    required: bool = True

    @model_validator(mode="after")
    def hints_are_unique(self) -> "ExecutionContextNeed":
        _unique(self.path_hints, "context path hint")
        _unique(self.symbol_hints, "context symbol hint")
        _unique(self.tag_hints, "context tag hint")
        return self


class ExecutionSpecProposal(EngineModel):
    """Worker/expander가 제출하며 authority digest를 포함하지 않는 운영 상세 후보."""

    task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    context_needs: tuple[ExecutionContextNeed, ...] = Field(min_length=1)
    resolved_targets: tuple[ResolvedTarget, ...]
    actions: tuple[ExecutionAction, ...] = Field(min_length=1)
    validation_steps: tuple[ValidationExecutionStep, ...] = Field(min_length=1)
    resource_locks: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    context_token_budget: int = Field(default=12_000, ge=100, le=200_000)
    idempotency_hint: str = Field(default="run-once", min_length=1, max_length=100)

    @model_validator(mode="after")
    def proposal_entries_are_unique(self) -> "ExecutionSpecProposal":
        _unique(tuple(item.need_id for item in self.context_needs), "context need")
        _unique(tuple(item.target_ref for item in self.resolved_targets), "execution target")
        _unique(tuple(item.action_ref for item in self.actions), "execution action")
        _unique(tuple(item.validation_id for item in self.validation_steps), "validation step")
        _unique(self.resource_locks, "resource lock")
        return self

    @property
    def proposal_digest(self) -> str:
        return sha256_digest(self)


class ResolvedRoleAssignment(EngineModel):
    role: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    inventory_digest: str = Field(pattern=_DIGEST_PATTERN)
    fallback_used: bool = False
    operational_binding: OperationalBinding | None = None


class GoalOperatingPolicy(EngineModel):
    """승인한 운영 상한. 소요량 예측이나 OS 보안 경계가 아니다."""

    planning_budget: PlanningBudgetPolicy = PlanningBudgetPolicy()
    max_same_failure_replans: int = Field(default=2, ge=0, le=10)
    max_goal_replans: int = Field(default=5, ge=0, le=20)
    requires_new_evidence: bool = True
    resume_strategy: Literal["existing_binding_first", "new_attempt_only"] = "existing_binding_first"


class GoalAuthorization(EngineModel):
    """사용자 목표·대상·효과·정책 승인. Plan 선택은 내부 revision에 결속한다."""

    authorization_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    supersedes_authorization_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_root: str = Field(min_length=1, max_length=2000)
    goal_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    profile_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    effect_policy: EffectPolicy
    operating_policy: GoalOperatingPolicy = GoalOperatingPolicy()
    budget_policies: tuple[str, ...] = ()
    source: str = Field(min_length=1, max_length=2000)
    approved_at: datetime

    _approved_at_is_aware = field_validator("approved_at")(_aware)

    @property
    def authorization_digest(self) -> str:
        return sha256_digest(self)


class TaskExecutionSpecDefinition(EngineModel):
    plan_activation_digest: str = Field(pattern=_DIGEST_PATTERN)
    task_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    snapshot_digest: str = Field(pattern=_DIGEST_PATTERN)
    project_map_digest: str = Field(pattern=_DIGEST_PATTERN)
    context_manifest: ContextManifest
    resolved_targets: tuple[ResolvedTarget, ...]
    actions: tuple[ExecutionAction, ...] = Field(min_length=1)
    validation_steps: tuple[ValidationExecutionStep, ...] = Field(min_length=1)
    executor: ResolvedRoleAssignment
    validator: ResolvedRoleAssignment | None = None
    resource_locks: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=1800, ge=1, le=86_400)
    idempotency_key: str = Field(min_length=16, max_length=200)

    @model_validator(mode="after")
    def execution_spec_is_consistent(self) -> "TaskExecutionSpecDefinition":
        _unique(tuple(item.target_ref for item in self.resolved_targets), "execution target")
        _unique(tuple(item.action_ref for item in self.actions), "execution action")
        _unique(tuple(item.validation_id for item in self.validation_steps), "validation step")
        _unique(self.resource_locks, "resource lock")
        if self.validator is not None and self.validator.inventory_digest != self.executor.inventory_digest:
            raise ValueError("executor와 validator는 같은 model inventory에 결속돼야 합니다.")
        return self

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class TaskExecutionSpecRevision(EngineModel):
    schema_version: Literal[ENGINE_SCHEMA_VERSION] = ENGINE_SCHEMA_VERSION
    execution_spec_revision_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    revision_no: int = Field(ge=1)
    definition: TaskExecutionSpecDefinition
    definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    supersedes_execution_spec_revision_id: str | None = Field(
        default=None,
        pattern=_ENTITY_ID_PATTERN,
    )
    created_at: datetime

    _created_at_is_aware = field_validator("created_at")(_aware)

    @model_validator(mode="after")
    def execution_spec_revision_is_bound(self) -> "TaskExecutionSpecRevision":
        if self.task_id != self.definition.task_id:
            raise ValueError("ExecutionSpec revision과 definition의 task_id가 다릅니다.")
        if self.definition_digest != self.definition.definition_digest:
            raise ValueError("ExecutionSpec definition digest가 실제 내용과 다릅니다.")
        if self.supersedes_execution_spec_revision_id == self.execution_spec_revision_id:
            raise ValueError("ExecutionSpec revision은 자신을 supersede할 수 없습니다.")
        return self

    @property
    def revision_digest(self) -> str:
        return sha256_digest(self)


class AttemptKind(StrEnum):
    EXECUTION = "execution"
    VALIDATION = "validation"


class TaskRuntimeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    MATERIALIZED = "materialized"
    RESERVED = "reserved"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    ABANDONED = "abandoned"


class RunOnceAction(StrEnum):
    MATERIALIZED = "materialized"
    DISPATCHED = "dispatched"
    OBSERVED = "observed"
    VALIDATED = "validated"
    COMPLETED = "completed"
    RECOVERED = "recovered"
    BLOCKED = "blocked"
    IDLE = "idle"


class RuntimeJobKind(StrEnum):
    """Plan 활성화 뒤 Core가 예약할 수 있는 provider 역할 경계."""

    EXECUTION_SPEC_PREPARE = "execution_spec_prepare"
    WORKER_TURN = "worker_turn"
    TASK_SEMANTIC_VALIDATE = "task_semantic_validate"
    GOAL_TEST_PREPARE = "goal_test_prepare"
    GOAL_SEMANTIC_VALIDATE = "goal_semantic_validate"
    RECOVERY = "recovery"
    REPLANNING = "replanning"


class RuntimeJobStatus(StrEnum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    PROVIDER_TERMINAL = "provider_terminal"
    COLLECTOR_LOST = "collector_lost"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"


class RuntimeJobObservationKind(StrEnum):
    SCHEDULED = "scheduled"
    STARTED = "started"
    PROVIDER_PROGRESS = "provider_progress"
    PROVIDER_TERMINAL = "provider_terminal"
    INTERRUPT_REQUESTED = "interrupt_requested"
    INTERRUPT_RECEIPT = "interrupt_receipt"
    COLLECTOR_LOST = "collector_lost"
    COLLECTOR_REATTACHED = "collector_reattached"
    CONSUMED = "consumed"


class RuntimeJob(EngineModel):
    """provider 실행 수명과 Core 상태 판정을 분리한 durable job."""

    job_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    kind: RuntimeJobKind
    status: RuntimeJobStatus
    request_digest: str = Field(pattern=_DIGEST_PATTERN)
    request: dict[str, Any]
    checkpoint_key: str = Field(min_length=1, max_length=1000)
    attempt_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    thread_id: str | None = Field(default=None, max_length=300)
    turn_id: str | None = Field(default=None, max_length=300)
    absolute_deadline_at: datetime
    provider_terminal_status: str | None = Field(default=None, max_length=100)
    result_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    updated_at: datetime

    _absolute_deadline_at_is_aware = field_validator("absolute_deadline_at")(_aware)
    _created_at_is_aware = field_validator("created_at")(_aware)
    _started_at_is_aware = field_validator("started_at")(
        lambda value: None if value is None else _aware(value)
    )
    _ended_at_is_aware = field_validator("ended_at")(
        lambda value: None if value is None else _aware(value)
    )
    _updated_at_is_aware = field_validator("updated_at")(_aware)

    @model_validator(mode="after")
    def terminal_is_provider_observed(self) -> "RuntimeJob":
        terminal = self.status in {
            RuntimeJobStatus.PROVIDER_TERMINAL,
            RuntimeJobStatus.CONSUMED,
        }
        if terminal != (self.provider_terminal_status is not None):
            raise ValueError("provider terminal 상태와 관측값이 일치해야 합니다.")
        if self.result_digest is not None and not terminal:
            raise ValueError("provider terminal 전에는 job 결과 digest를 둘 수 없습니다.")
        return self


class RuntimeJobObservation(EngineModel):
    observation_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    job_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    kind: RuntimeJobObservationKind
    provider_terminal: bool = False
    terminal_status: str | None = Field(default=None, max_length=100)
    payload: dict[str, Any]
    payload_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    _observed_at_is_aware = field_validator("observed_at")(_aware)

    @model_validator(mode="after")
    def observation_is_bound(self) -> "RuntimeJobObservation":
        if self.payload_digest != sha256_digest(self.payload):
            raise ValueError("runtime job observation digest가 payload와 다릅니다.")
        if self.provider_terminal != (self.terminal_status is not None):
            raise ValueError("provider terminal 관측에만 terminal_status가 필요합니다.")
        return self


class FailureClass(StrEnum):
    UNCLASSIFIED = "unclassified"
    IMPLEMENTATION = "implementation"
    CONTEXT = "context"
    TASK_CONTRACT = "task_contract"
    DEPENDENCY = "dependency"
    ENVIRONMENT = "environment"
    REQUIREMENT_CHANGE = "requirement_change"
    EXTERNAL_UNKNOWN = "external_unknown"


class RepairAction(StrEnum):
    CONTINUE = "continue"
    TASK_REPAIR = "task_repair"
    EXECUTION_SPEC_REVISION = "execution_spec_revision"
    SUBGRAPH_REPLAN = "subgraph_replan"
    GOAL_REVISION = "goal_revision"
    WAIT_EXTERNAL = "wait_external"
    ABANDON = "abandon"


class RunOnceOutcome(EngineModel):
    action: RunOnceAction
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    attempt_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    execution_spec_revision_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    validation_result_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    goal_verdict_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    evidence_ids: tuple[str, ...] = ()
    blocker_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    failure_class: FailureClass | None = None
    suggested_repair_action: RepairAction | None = None
    checkpoint_required: bool = False
    detail: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def outcome_is_consistent(self) -> "RunOnceOutcome":
        _unique(self.evidence_ids, "run once evidence")
        if self.action is RunOnceAction.BLOCKED and self.blocker_code is None:
            raise ValueError("blocked RunOnceOutcome에는 blocker_code가 필요합니다.")
        if self.action is not RunOnceAction.BLOCKED and self.blocker_code is not None:
            raise ValueError("blocked가 아닌 RunOnceOutcome에는 blocker_code를 둘 수 없습니다.")
        if (self.failure_class is None) != (self.suggested_repair_action is None):
            raise ValueError("failure_class와 suggested_repair_action은 함께 기록해야 합니다.")
        if self.failure_class is not None and self.action is not RunOnceAction.BLOCKED:
            raise ValueError("실패 repair 제안은 blocked RunOnceOutcome에만 기록합니다.")
        if self.checkpoint_required and self.suggested_repair_action is None:
            raise ValueError("checkpoint_required에는 repair 제안이 필요합니다.")
        return self


class RunOnceResult(RunOnceOutcome):
    """한 번의 bounded scheduler tick 결과.

    ``outcome``의 완료 의미는 계속 Core가 판정하며, job 필드는 그 tick이 예약·시작·
    관측 소비한 provider 작업만 설명한다.
    """

    runtime_job_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    runtime_job_kind: RuntimeJobKind | None = None
    runtime_job_status: RuntimeJobStatus | None = None
    tick_elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def job_fields_are_atomic(self) -> "RunOnceResult":
        fields = (self.runtime_job_id, self.runtime_job_kind, self.runtime_job_status)
        if any(value is not None for value in fields) and not all(value is not None for value in fields):
            raise ValueError("run once job 결속 필드는 함께 기록해야 합니다.")
        return self


class ThreadBinding(EngineModel):
    thread_id: str = Field(min_length=1, max_length=300)
    turn_id: str | None = Field(default=None, max_length=300)
    host_id: str | None = Field(default=None, max_length=300)
    bound_at: datetime

    _bound_at_is_aware = field_validator("bound_at")(_aware)


class AttemptRecord(EngineModel):
    attempt_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    task_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    execution_spec_digest: str = Field(pattern=_DIGEST_PATTERN)
    attempt_no: int = Field(ge=1)
    kind: AttemptKind
    status: AttemptStatus
    binding: ThreadBinding | None = None
    failure_class: FailureClass | None = None
    failure_detail: str | None = Field(default=None, max_length=5000)
    started_at: datetime | None = None
    ended_at: datetime | None = None

    _started_at_is_aware = field_validator("started_at")(
        lambda value: None if value is None else _aware(value)
    )
    _ended_at_is_aware = field_validator("ended_at")(
        lambda value: None if value is None else _aware(value)
    )

    @model_validator(mode="after")
    def attempt_state_is_consistent(self) -> "AttemptRecord":
        terminal = {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.INTERRUPTED,
            AttemptStatus.ABANDONED,
        }
        if self.status in terminal and self.ended_at is None:
            raise ValueError("종료된 Attempt에는 ended_at이 필요합니다.")
        if self.status is AttemptStatus.FAILED and self.failure_class is None:
            raise ValueError("실패 Attempt에는 failure_class가 필요합니다.")
        if self.failure_class is not None and self.status not in {
            AttemptStatus.FAILED,
            AttemptStatus.UNKNOWN,
        }:
            raise ValueError("failure_class는 failed/unknown Attempt에만 기록합니다.")
        return self


class RuntimeIntentKind(StrEnum):
    CREATE_THREAD = "create_thread"
    START_TURN = "start_turn"
    RESUME_TURN = "resume_turn"
    INTERRUPT_TURN = "interrupt_turn"
    EXTERNAL_EFFECT = "external_effect"


class RuntimeIntentStatus(StrEnum):
    PREPARED = "prepared"
    RECEIVED = "received"
    UNKNOWN = "unknown"
    ABANDONED = "abandoned"


class RuntimeIntentRecord(EngineModel):
    intent_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    attempt_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    kind: RuntimeIntentKind
    idempotency_key: str = Field(min_length=16, max_length=200)
    request_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: RuntimeIntentStatus
    prepared_at: datetime

    _prepared_at_is_aware = field_validator("prepared_at")(_aware)


class RuntimeReceipt(EngineModel):
    receipt_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    intent_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    provider_operation_id: str = Field(min_length=1, max_length=500)
    response_digest: str = Field(pattern=_DIGEST_PATTERN)
    binding: ThreadBinding | None = None
    response_payload: dict[str, Any] | None = None
    received_at: datetime

    _received_at_is_aware = field_validator("received_at")(_aware)

    @model_validator(mode="after")
    def response_matches_digest(self) -> "RuntimeReceipt":
        if self.response_payload is not None and sha256_digest(self.response_payload) != self.response_digest:
            raise ValueError("runtime receipt 본문과 digest가 다릅니다.")
        return self


class EvidenceRecord(EngineModel):
    evidence_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    attempt_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    kind: EvidenceKind
    source_ref: str = Field(min_length=1, max_length=2000)
    observation: str = Field(min_length=1, max_length=10_000)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    _observed_at_is_aware = field_validator("observed_at")(_aware)


class ValidationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


class ValidationResult(EngineModel):
    validation_result_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    goal_validation_binding_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    status: ValidationStatus
    evidence_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=5000)
    evaluated_at: datetime

    _evaluated_at_is_aware = field_validator("evaluated_at")(_aware)

    @model_validator(mode="after")
    def validation_claim_has_evidence(self) -> "ValidationResult":
        _unique(self.evidence_ids, "validation evidence")
        if self.task_id is not None and self.goal_validation_binding_digest is not None:
            raise ValueError("Task validation에는 Goal Test binding을 결속할 수 없습니다.")
        if self.status in {ValidationStatus.PASS, ValidationStatus.FAIL} and not self.evidence_ids:
            raise ValueError("PASS/FAIL validation에는 실제 evidence가 필요합니다.")
        if self.status is ValidationStatus.NOT_RUN and self.evidence_ids:
            raise ValueError("실행하지 않은 validation에는 evidence를 결속할 수 없습니다.")
        return self


class DeterministicValidationObservation(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str = Field(min_length=1, max_length=2000)
    timeout_seconds: int = Field(ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = Field(min_length=1)
    actual_exit_code: int | None = None
    timed_out: bool = False
    stdout: str = Field(default="", max_length=100_000)
    stderr: str = Field(default="", max_length=100_000)
    observed_at: datetime

    _deterministic_observed_at_is_aware = field_validator("observed_at")(_aware)

    @model_validator(mode="after")
    def deterministic_observation_is_consistent(self) -> "DeterministicValidationObservation":
        _unique(self.expected_exit_codes, "expected exit code")
        if self.timed_out and self.actual_exit_code is not None:
            raise ValueError("timeout 관측에는 실제 종료 코드를 둘 수 없습니다.")
        if not self.timed_out and self.actual_exit_code is None:
            raise ValueError("완료된 command 관측에는 실제 종료 코드가 필요합니다.")
        return self

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.actual_exit_code in self.expected_exit_codes


class SemanticValidationObservation(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    reviewer_role: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    passed: bool
    rationale: str = Field(min_length=1, max_length=5000)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    observed_at: datetime

    _semantic_observed_at_is_aware = field_validator("observed_at")(_aware)

    @field_validator("evidence_refs")
    @classmethod
    def semantic_refs_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "semantic evidence ref")


class ManualValidationObservation(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    observer: str = Field(min_length=1, max_length=300)
    passed: bool | None = None
    observation: str = Field(min_length=1, max_length=10_000)
    source_ref: str = Field(min_length=1, max_length=2000)
    observed_at: datetime

    _manual_observed_at_is_aware = field_validator("observed_at")(_aware)


class ExternalValidationObservation(EngineModel):
    validation_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    task_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    provider: str = Field(min_length=1, max_length=300)
    selector: str = Field(min_length=1, max_length=3000)
    passed: bool | None = None
    observation: str = Field(min_length=1, max_length=10_000)
    receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    observed_at: datetime

    _external_observed_at_is_aware = field_validator("observed_at")(_aware)


class CriterionVerdict(EngineModel):
    criterion_id: str = Field(pattern=_LOCAL_ID_PATTERN)
    status: ValidationStatus
    evidence_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def criterion_claim_has_evidence(self) -> "CriterionVerdict":
        _unique(self.evidence_ids, "criterion evidence")
        if self.status in {ValidationStatus.PASS, ValidationStatus.FAIL} and not self.evidence_ids:
            raise ValueError("Goal criterion PASS/FAIL에는 evidence가 필요합니다.")
        return self


class GoalVerdictStatus(StrEnum):
    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    INCONCLUSIVE = "inconclusive"


class GoalVerdict(EngineModel):
    goal_verdict_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    plan_activation_digest: str = Field(pattern=_DIGEST_PATTERN)
    status: GoalVerdictStatus
    criteria: tuple[CriterionVerdict, ...] = Field(min_length=1)
    integration_validation_result_ids: tuple[str, ...] = Field(min_length=1)
    evaluated_at: datetime

    _evaluated_at_is_aware = field_validator("evaluated_at")(_aware)

    @model_validator(mode="after")
    def goal_verdict_is_derived_from_criteria(self) -> "GoalVerdict":
        _unique(tuple(item.criterion_id for item in self.criteria), "Goal criterion verdict")
        _unique(self.integration_validation_result_ids, "Goal integration result")
        statuses = {item.status for item in self.criteria}
        if self.status is GoalVerdictStatus.SATISFIED and statuses != {ValidationStatus.PASS}:
            raise ValueError("모든 Goal criterion이 PASS일 때만 satisfied일 수 있습니다.")
        if self.status is GoalVerdictStatus.NOT_SATISFIED and ValidationStatus.FAIL not in statuses:
            raise ValueError("FAIL criterion 없이 not_satisfied일 수 없습니다.")
        if self.status is GoalVerdictStatus.INCONCLUSIVE and ValidationStatus.FAIL in statuses:
            raise ValueError("FAIL criterion이 있으면 inconclusive가 아니라 not_satisfied입니다.")
        return self


class BudgetStage(StrEnum):
    GOAL_NORMALIZATION = "goal_normalization"
    GOAL_REVIEW = "goal_review"
    STATE_PROJECTION = "state_projection"
    SKELETON_GENERATION = "skeleton_generation"
    SKELETON_REVIEW = "skeleton_review"
    PLAN_EXPANSION = "plan_expansion"
    PLAN_REVIEW = "plan_review"
    ASSIGNMENT = "assignment"
    EXECUTION_PREPARATION = "execution_preparation"
    EXECUTION = "execution"
    VALIDATION = "validation"
    REPLAN = "replan"


class UsageObservation(EngineModel):
    """provider 실행 상태와 분리된 append-only 사용량 관측.

    ``unavailable``은 측정값 0이 아니다. 늦게 도착한 실측은 새 관측으로
    연결하며 원래 receipt나 실행 완료 시각을 수정하지 않는다.
    """

    schema_version: Literal["1.0"] = "1.0"
    observation_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    provider_call_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    source: Literal["role_receipt", "runtime_observation", "worker_terminal", "validator_terminal"]
    measurement_status: Literal["measured", "unavailable"]
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    usage_scope: Literal["unspecified", "turn", "thread", "unavailable"] = "unavailable"
    attribution_basis: Literal["provider_turn", "first_empty_thread", "unavailable"] = "unavailable"
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)
    raw_observation_digest: str = Field(pattern=_DIGEST_PATTERN)
    original_receipt_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    previous_observation_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    late: bool = False
    observed_at: datetime

    _observed_at_is_aware = field_validator("observed_at")(_aware)

    @model_validator(mode="after")
    def measurement_is_not_estimated(self) -> "UsageObservation":
        counts = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
        )
        if self.measurement_status == "measured":
            if any(value is None for value in counts):
                raise ValueError("실측 UsageObservation에는 모든 token 필드가 필요합니다.")
            if self.unavailable_reason is not None or self.attribution_basis == "unavailable":
                raise ValueError("실측 usage와 unavailable provenance를 혼합할 수 없습니다.")
        elif any(value is not None for value in counts) or self.unavailable_reason is None:
            raise ValueError("미확인 UsageObservation은 null token과 이유를 보존해야 합니다.")
        if (
            self.cached_input_tokens is not None
            and self.input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("cached input token은 input token보다 클 수 없습니다.")
        if (
            self.reasoning_tokens is not None
            and self.output_tokens is not None
            and self.reasoning_tokens > self.output_tokens
        ):
            raise ValueError("reasoning token은 output token보다 클 수 없습니다.")
        if self.late and self.previous_observation_id is None:
            raise ValueError("late UsageObservation에는 직전 관측 연결이 필요합니다.")
        return self


class BudgetUsageRecord(EngineModel):
    usage_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    goal_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    stage: BudgetStage
    logical_call_ref: str = Field(min_length=1, max_length=300)
    role: str = Field(min_length=1, max_length=100)
    call_status: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(min_length=1, max_length=50)
    permission_profile: str = Field(min_length=1, max_length=100)
    approval_policy: str = Field(min_length=1, max_length=100)
    thread_id: str | None = Field(default=None, max_length=500)
    turn_ids: tuple[str, ...] = ()
    input_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    runner_receipt_digest: str = Field(pattern=_DIGEST_PATTERN)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    usage_available: bool = True
    attempt_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    runtime_intent_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    runtime_receipt_id: str | None = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    execution_spec_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    prompt_binding_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    prompt_token_estimate: int | None = Field(default=None, ge=0)
    usage_scope: Literal["unspecified", "turn", "thread", "unavailable"] = "unspecified"
    usage_source: str | None = Field(default=None, min_length=1, max_length=300)
    attribution_basis: Literal["provider_turn", "first_empty_thread", "unavailable"] | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)
    provider_observation: dict[str, Any] | None = None
    retry_count: int = Field(default=0, ge=0)
    discarded_output: bool = False
    recorded_at: datetime

    _recorded_at_is_aware = field_validator("recorded_at")(_aware)

    @model_validator(mode="after")
    def token_counts_are_consistent(self) -> "BudgetUsageRecord":
        _unique(self.turn_ids, "budget usage turn")
        counts = (self.input_tokens, self.cached_input_tokens, self.output_tokens, self.reasoning_tokens)
        if self.usage_available and any(value is None for value in counts):
            raise ValueError("실측 usage에는 모든 token 필드가 필요합니다.")
        if self.cached_input_tokens is not None and self.input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input token은 input token보다 클 수 없습니다.")
        if self.reasoning_tokens is not None and self.output_tokens is not None and self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning token은 output token보다 클 수 없습니다.")
        if not self.usage_available and any(value not in (None, 0) for value in counts):
            raise ValueError("usage unavailable 레코드에는 token 수를 추정해 넣지 않습니다.")
        if self.attempt_id is not None:
            if self.stage is not BudgetStage.EXECUTION or len(self.turn_ids) != 1 or not self.thread_id:
                raise ValueError("Worker usage는 실행 Attempt와 provider turn 하나에 결속합니다.")
            if any(value is None for value in (
                self.runtime_intent_id, self.runtime_receipt_id, self.execution_spec_digest,
                self.prompt_binding_digest, self.prompt_token_estimate, self.usage_source,
                self.attribution_basis, self.provider_observation,
            )):
                raise ValueError("Worker usage의 Prompt/receipt 근거가 누락됐습니다.")
            if not self.usage_available and (any(value is not None for value in counts) or not self.unavailable_reason):
                raise ValueError("새 Worker unavailable은 null token과 명시적 이유로 보존합니다.")
            if self.usage_available and (self.attribution_basis == "unavailable" or self.unavailable_reason is not None):
                raise ValueError("실측 usage와 unavailable 근거를 혼합할 수 없습니다.")
            if self.usage_available and (self.usage_scope, self.attribution_basis) not in {
                ("turn", "provider_turn"), ("thread", "first_empty_thread"),
            }:
                raise ValueError("실측 usage의 원시 scope와 귀속 근거가 다릅니다.")
            if sha256_digest(self.provider_observation) != self.runner_receipt_digest:
                raise ValueError("Worker usage 원시 관측과 digest가 다릅니다.")
            if (self.provider_observation.get("thread_id") != self.thread_id or
                    self.provider_observation.get("turn_id") != self.turn_ids[0]):
                raise ValueError("Worker usage 원시 관측의 provider turn이 다릅니다.")
        return self

    @property
    def uncached_input_tokens(self) -> int | None:
        if not self.usage_available:
            return None
        assert self.input_tokens is not None and self.cached_input_tokens is not None
        return self.input_tokens - self.cached_input_tokens

    @property
    def optimization_tokens(self) -> int | None:
        uncached = self.uncached_input_tokens
        if uncached is None or self.output_tokens is None:
            return None
        return uncached + self.output_tokens


class RecoveryAssessment(EngineModel):
    assessment_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    attempt_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    failure_class: FailureClass
    action: RepairAction
    rationale: str = Field(min_length=1, max_length=5000)
    failure_fingerprint: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    new_evidence_ids: tuple[str, ...] = ()
    same_failure_replan_count: int = Field(ge=0)
    goal_replan_count: int = Field(ge=0)

    @model_validator(mode="after")
    def recovery_has_new_evidence_when_repeating(self) -> "RecoveryAssessment":
        _unique(self.new_evidence_ids, "recovery evidence")
        return self
