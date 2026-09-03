from __future__ import annotations

import re
import uuid
from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import sha256_digest


GATE0B_SCHEMA_REVISION = 1


class DomainError(ValueError):
    """도메인 불변식을 위반한 요청."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class ProjectState(StrEnum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"


class ResourceKind(StrEnum):
    WORKSPACE = "workspace"
    REFERENCE = "reference"
    RUNTIME_DEPENDENCY = "runtime_dependency"


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class RevisionStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED_PENDING_ACTIVATION = "approved_pending_activation"
    ACTIVE = "active"
    RETIRED = "retired"
    REJECTED = "rejected"


class WorkItemState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    NEEDS_ACCESS = "needs_access"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"
    SUPERSEDED = "superseded"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    AWAITING_VALIDATION = "awaiting_validation"
    AWAITING_HUMAN_REVIEW = "awaiting_human_review"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED_EXTERNAL_UNKNOWN = "abandoned_external_unknown"


ACTIVE_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.RESERVED,
        AttemptStatus.RUNNING,
        AttemptStatus.AWAITING_VALIDATION,
        AttemptStatus.AWAITING_HUMAN_REVIEW,
    }
)
TERMINAL_ATTEMPT_STATUSES = frozenset(set(AttemptStatus) - ACTIVE_ATTEMPT_STATUSES)


class IntentKind(StrEnum):
    CREATE_THREAD = "create_thread"
    START_TURN = "start_turn"
    INTERRUPT_TURN = "interrupt_turn"
    RUN_CHECK = "run_check"
    RECOVER_BIND = "recover_bind"
    RECOVER_ABANDON = "recover_abandon"


RECOVERY_INTENT_KINDS = frozenset({IntentKind.RECOVER_BIND, IntentKind.RECOVER_ABANDON})


class IntentStatus(StrEnum):
    RESERVED = "reserved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    CONFIRMED_NO_EFFECT = "confirmed_no_effect"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    RECONCILED = "reconciled"


ACTIVE_INTENT_STATUSES = frozenset({IntentStatus.RESERVED, IntentStatus.EXECUTING})
SETTLED_INTENT_STATUSES = frozenset(
    {
        IntentStatus.SUCCEEDED,
        IntentStatus.CONFIRMED_NO_EFFECT,
        IntentStatus.CANCELLED,
        IntentStatus.RECONCILED,
    }
)


class VerificationType(StrEnum):
    CHECK = "check"
    ARTIFACT = "artifact"
    WORKSPACE_PREDICATE = "workspace_predicate"
    HUMAN_REVIEW = "human_review"


class DecisionType(StrEnum):
    DISPATCH_NEXT_ATTEMPT = "dispatch_next_attempt"
    CONTINUE_RESERVED_INTENT = "continue_reserved_intent"
    START_TURN = "start_turn"
    VALIDATE_ATTEMPT = "validate_attempt"
    ACTIVATE_REVISION = "activate_revision"
    HALT_NO_READY_WORK = "halt_no_ready_work"
    HALT_RECOVERY_REQUIRED = "halt_recovery_required"
    HALT_PROJECT_QUARANTINED = "halt_project_quarantined"


class ProjectDefinition(FrozenModel):
    name: str = Field(min_length=1, max_length=120)
    canonical_root: str = Field(min_length=3)
    execution_slots: int = Field(default=1, ge=1, le=64)


class ResourceDefinition(FrozenModel):
    id: str | None = None
    kind: ResourceKind
    canonical_path: str = Field(min_length=3)
    max_access: AccessMode
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    file_identity: str | None = None
    manifest_digest: str | None = None

    @model_validator(mode="after")
    def validate_kind_access(self) -> "ResourceDefinition":
        if self.kind in {ResourceKind.REFERENCE, ResourceKind.RUNTIME_DEPENDENCY}:
            if self.max_access is not AccessMode.READ:
                raise ValueError(f"{self.kind.value} 리소스는 읽기 전용이어야 합니다.")
        return self


class ReadScope(FrozenModel):
    resource_id: str
    relative_path: str = "."


class VerificationSpec(FrozenModel):
    type: VerificationType
    check_id: str | None = None
    relative_path: str | None = None
    predicate: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "VerificationSpec":
        if self.type is VerificationType.CHECK and not self.check_id:
            raise ValueError("check 완료 조건에는 check_id가 필요합니다.")
        if self.type in {VerificationType.ARTIFACT, VerificationType.WORKSPACE_PREDICATE}:
            if not self.relative_path or not self.predicate:
                raise ValueError("파일 완료 조건에는 relative_path와 predicate가 필요합니다.")
        return self


class CompletionCriterion(FrozenModel):
    criterion_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    verification: VerificationSpec


class WorkItemDefinition(FrozenModel):
    client_ref: str = Field(min_length=1, max_length=100)
    previous_work_item_id: str | None = None
    goal: str = Field(min_length=1, max_length=5000)
    dependencies: tuple[str, ...] = ()
    write_resource_id: str
    read_scopes: tuple[ReadScope, ...] = ()
    completion_criteria: tuple[CompletionCriterion, ...] = Field(min_length=1)
    execution_profile: dict[str, Any] | None = None
    validation_profile: dict[str, Any] | None = None

    @field_validator("dependencies")
    @classmethod
    def unique_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("dependency가 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def unique_criteria(self) -> "WorkItemDefinition":
        identifiers = [item.criterion_id for item in self.completion_criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("완료 조건 ID가 중복됐습니다.")
        return self

    def definition_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"client_ref", "previous_work_item_id"},
            exclude_none=True,
        )

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self.definition_payload())


class PlanDraft(FrozenModel):
    project_id: str
    summary: str = Field(min_length=1, max_length=5000)
    work_items: tuple[WorkItemDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> "PlanDraft":
        refs = [item.client_ref for item in self.work_items]
        if len(refs) != len(set(refs)):
            raise ValueError("WorkItem client_ref가 중복됐습니다.")
        known = set(refs)
        graph: dict[str, list[str]] = {}
        for item in self.work_items:
            if item.client_ref in item.dependencies:
                raise ValueError("WorkItem은 자신에게 의존할 수 없습니다.")
            missing = set(item.dependencies) - known
            if missing:
                raise ValueError(f"존재하지 않는 dependency입니다: {sorted(missing)}")
            graph[item.client_ref] = list(item.dependencies)
        _assert_acyclic(graph)
        return self

    @property
    def content_digest(self) -> str:
        return sha256_digest(self)


class Decision(FrozenModel):
    decision_type: DecisionType
    project_id: str
    work_item_id: str | None = None
    attempt_id: str | None = None
    intent_id: str | None = None
    reason_code: str
    inputs_digest: str


class CommandEnvelope(FrozenModel):
    command_id: str
    correlation_id: str
    observed_at: str
    decision: Decision


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}_[0-9a-f]{32}$")


def new_id(prefix: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", prefix):
        raise ValueError(f"안전하지 않은 ID 접두사입니다: {prefix}")
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_id(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise DomainError(f"유효하지 않은 FlowMarshal ID입니다: {value}")
    return value


REVISION_TRANSITIONS: dict[RevisionStatus, frozenset[RevisionStatus]] = {
    RevisionStatus.CANDIDATE: frozenset(
        {
            RevisionStatus.APPROVED_PENDING_ACTIVATION,
            RevisionStatus.REJECTED,
        }
    ),
    RevisionStatus.APPROVED_PENDING_ACTIVATION: frozenset({RevisionStatus.ACTIVE}),
    RevisionStatus.ACTIVE: frozenset({RevisionStatus.RETIRED}),
    RevisionStatus.RETIRED: frozenset(),
    RevisionStatus.REJECTED: frozenset(),
}


ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.RESERVED: frozenset(
        {
            AttemptStatus.RUNNING,
            AttemptStatus.BLOCKED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.ABANDONED_EXTERNAL_UNKNOWN,
        }
    ),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.AWAITING_VALIDATION,
            AttemptStatus.BLOCKED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.ABANDONED_EXTERNAL_UNKNOWN,
        }
    ),
    AttemptStatus.AWAITING_VALIDATION: frozenset(
        {
            AttemptStatus.COMPLETED,
            AttemptStatus.AWAITING_HUMAN_REVIEW,
            AttemptStatus.BLOCKED,
            AttemptStatus.FAILED,
        }
    ),
    AttemptStatus.AWAITING_HUMAN_REVIEW: frozenset(
        {AttemptStatus.COMPLETED, AttemptStatus.BLOCKED, AttemptStatus.CANCELLED}
    ),
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.BLOCKED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.ABANDONED_EXTERNAL_UNKNOWN: frozenset(),
}


INTENT_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.RESERVED: frozenset(
        {IntentStatus.EXECUTING, IntentStatus.CANCELLED}
    ),
    IntentStatus.EXECUTING: frozenset(
        {
            IntentStatus.SUCCEEDED,
            IntentStatus.CONFIRMED_NO_EFFECT,
            IntentStatus.UNKNOWN,
        }
    ),
    IntentStatus.UNKNOWN: frozenset({IntentStatus.RECONCILED}),
    IntentStatus.SUCCEEDED: frozenset(),
    IntentStatus.CONFIRMED_NO_EFFECT: frozenset(),
    IntentStatus.CANCELLED: frozenset(),
    IntentStatus.RECONCILED: frozenset(),
}


def require_transition(current: StrEnum, target: StrEnum, table: dict[Any, frozenset[Any]]) -> None:
    allowed = table.get(current, frozenset())
    if target not in allowed:
        raise DomainError(f"허용되지 않은 상태 전이입니다: {current.value} → {target.value}")


def require_revision_transition(current: RevisionStatus, target: RevisionStatus) -> None:
    require_transition(current, target, REVISION_TRANSITIONS)


def require_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    require_transition(current, target, ATTEMPT_TRANSITIONS)


def require_intent_transition(current: IntentStatus, target: IntentStatus) -> None:
    require_transition(current, target, INTENT_TRANSITIONS)


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    indegree = {node: 0 for node in graph}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node, dependencies in graph.items():
        indegree[node] = len(dependencies)
        for dependency in dependencies:
            dependents[dependency].append(node)
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    seen = 0
    while ready:
        node = ready.popleft()
        seen += 1
        for dependent in sorted(dependents[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if seen != len(graph):
        raise DomainError("WorkItem dependency graph에 순환이 있습니다.")


def dependency_order(work_items: Iterable[WorkItemDefinition]) -> tuple[str, ...]:
    items = tuple(work_items)
    graph = {item.client_ref: list(item.dependencies) for item in items}
    _assert_acyclic(graph)
    indegree = {node: len(dependencies) for node, dependencies in graph.items()}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node, dependencies in graph.items():
        for dependency in dependencies:
            dependents[dependency].append(node)
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    result: list[str] = []
    while ready:
        node = ready.popleft()
        result.append(node)
        for dependent in sorted(dependents[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return tuple(result)
