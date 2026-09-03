from __future__ import annotations

import re
import uuid
from collections import defaultdict, deque
from enum import StrEnum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..canonical import sha256_digest


CORE_SCHEMA_ID = "flowmarshal-core"
CORE_SCHEMA_REVISION = 1


class CoreDomainError(ValueError):
    """새 제품 Core의 도메인 불변식을 위반한 요청."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class ActivationSource(StrEnum):
    CLI = "cli"
    UI = "ui"
    API = "api"


class PlanRevisionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    COMPLETED = "completed"


class StoredWorkItemStatus(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class EffectiveWorkItemStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    AWAITING_VALIDATION = "awaiting_validation"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXTERNAL_UNKNOWN = "external_unknown"


ACTIVE_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.RESERVED,
        AttemptStatus.RUNNING,
        AttemptStatus.AWAITING_VALIDATION,
    }
)


class RuntimeIntentKind(StrEnum):
    CREATE_THREAD = "create_thread"
    START_TURN = "start_turn"
    INTERRUPT_TURN = "interrupt_turn"
    OBSERVE_THREAD = "observe_thread"
    RUN_VALIDATION = "run_validation"
    RECOVER_BIND = "recover_bind"
    RECOVER_ABANDON = "recover_abandon"


class RuntimeIntentStatus(StrEnum):
    RESERVED = "reserved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    CONFIRMED_NO_EFFECT = "confirmed_no_effect"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"
    RECONCILED = "reconciled"


class ValidationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class RequirementDisposition(StrEnum):
    WORK_ITEMS = "work_items"
    EXCLUDED = "excluded"
    CLARIFICATION = "clarification"


class ProjectDefinition(FrozenModel):
    name: str = Field(min_length=1, max_length=120)
    root: str = Field(min_length=3)
    context_sources: tuple[str, ...] = ()
    default_validations: tuple[str, ...] = ()
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)

    @field_validator("context_sources", "default_validations")
    @classmethod
    def unique_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("항목이 중복됐습니다.")
        return value


class Assignment(FrozenModel):
    execution_model_id: str = Field(min_length=1, max_length=200)
    execution_reasoning_effort: str = Field(min_length=1, max_length=40)
    validation_model_id: str | None = Field(default=None, max_length=200)
    validation_reasoning_effort: str | None = Field(default=None, max_length=40)
    selection_reason: str = Field(min_length=1, max_length=2000)
    fallback_policy: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validation_pair_is_complete(self) -> "Assignment":
        if (self.validation_model_id is None) != (
            self.validation_reasoning_effort is None
        ):
            raise ValueError("검사 모델과 검사 추론 수준은 함께 지정해야 합니다.")
        return self


class ValidationDefinition(FrozenModel):
    criterion_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    check_type: str = Field(min_length=1, max_length=80)
    capability_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
    )
    specification: dict[str, Any] = Field(default_factory=dict)


class WorkItemDefinition(FrozenModel):
    client_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=5000)
    dependencies: tuple[str, ...] = ()
    context_sources: tuple[str, ...] = ()
    expected_changes: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    deliverables: tuple[str, ...] = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    validations: tuple[ValidationDefinition, ...] = Field(min_length=1)
    execution_requirements: dict[str, Any] = Field(default_factory=dict)
    assignment: Assignment | None = None

    @field_validator(
        "dependencies",
        "context_sources",
        "expected_changes",
        "out_of_scope",
        "deliverables",
        "acceptance_criteria",
    )
    @classmethod
    def unique_tuple_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("WorkItem tuple 항목이 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def validate_local_shape(self) -> "WorkItemDefinition":
        if self.client_ref in self.dependencies:
            raise ValueError("WorkItem은 자신에게 의존할 수 없습니다.")
        criteria = [item.criterion_id for item in self.validations]
        if len(criteria) != len(set(criteria)):
            raise ValueError("validation criterion_id가 중복됐습니다.")
        return self

    @property
    def definition_digest(self) -> str:
        return sha256_digest(self)


class RequirementCoverage(FrozenModel):
    requirement_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    disposition: RequirementDisposition
    work_item_refs: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=2000)
    clarification_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
    )
    requires_user_confirmation: bool = False

    @field_validator("work_item_refs")
    @classmethod
    def unique_work_item_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("coverage의 WorkItem 참조가 중복됐습니다.")
        return value

    @model_validator(mode="after")
    def validate_disposition_shape(self) -> "RequirementCoverage":
        if self.disposition is RequirementDisposition.WORK_ITEMS:
            if not self.work_item_refs:
                raise ValueError("work_items coverage에는 WorkItem 참조가 필요합니다.")
            if self.clarification_id is not None or self.requires_user_confirmation:
                raise ValueError("work_items coverage는 확인 대기 상태일 수 없습니다.")
        elif self.disposition is RequirementDisposition.EXCLUDED:
            if self.work_item_refs or self.clarification_id is not None:
                raise ValueError("excluded coverage에는 작업 또는 질문 참조를 둘 수 없습니다.")
            if not self.requires_user_confirmation:
                raise ValueError("요구사항 제외는 사용자 확인 대상으로 표시해야 합니다.")
        else:
            if self.work_item_refs or self.clarification_id is None:
                raise ValueError("clarification coverage에는 질문 참조만 필요합니다.")
            if not self.requires_user_confirmation:
                raise ValueError("미결정 항목은 사용자 확인 대상으로 표시해야 합니다.")
        return self


class ClarificationRequest(FrozenModel):
    clarification_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    question: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    blocking: bool = True

    @field_validator("requirement_ids")
    @classmethod
    def unique_requirement_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("clarification의 requirement 참조가 중복됐습니다.")
        return value


class PlanDraft(FrozenModel):
    project_id: str
    request_summary: str = Field(min_length=1, max_length=5000)
    parent_revision_id: str | None = None
    request_spec_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    work_items: tuple[WorkItemDefinition, ...] = Field(min_length=1)
    requirement_coverage: tuple[RequirementCoverage, ...] = ()
    clarifications: tuple[ClarificationRequest, ...] = ()
    planning_notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self) -> "PlanDraft":
        refs = [item.client_ref for item in self.work_items]
        if len(refs) != len(set(refs)):
            raise ValueError("WorkItem client_ref가 중복됐습니다.")
        known = set(refs)
        graph: dict[str, list[str]] = {}
        for item in self.work_items:
            missing = set(item.dependencies) - known
            if missing:
                raise ValueError(f"존재하지 않는 dependency입니다: {sorted(missing)}")
            graph[item.client_ref] = list(item.dependencies)
        _assert_acyclic(graph)
        coverage_ids = [item.requirement_id for item in self.requirement_coverage]
        if len(coverage_ids) != len(set(coverage_ids)):
            raise ValueError("requirement coverage가 중복됐습니다.")
        clarification_ids = [item.clarification_id for item in self.clarifications]
        if len(clarification_ids) != len(set(clarification_ids)):
            raise ValueError("clarification ID가 중복됐습니다.")
        known_clarifications = set(clarification_ids)
        clarifications_by_id = {
            item.clarification_id: item for item in self.clarifications
        }
        for coverage in self.requirement_coverage:
            missing_work = set(coverage.work_item_refs) - known
            if missing_work:
                raise ValueError(
                    f"coverage가 존재하지 않는 WorkItem을 참조합니다: {sorted(missing_work)}"
                )
            if (
                coverage.clarification_id is not None
                and coverage.clarification_id not in known_clarifications
            ):
                raise ValueError(
                    "coverage가 존재하지 않는 clarification을 참조합니다: "
                    f"{coverage.clarification_id}"
                )
            if coverage.clarification_id is not None:
                clarification = clarifications_by_id[coverage.clarification_id]
                if coverage.requirement_id not in clarification.requirement_ids:
                    raise ValueError(
                        "clarification coverage의 requirement가 질문의 requirement_ids에 "
                        f"없습니다: {coverage.requirement_id}"
                    )
        for clarification in self.clarifications:
            for requirement_id in clarification.requirement_ids:
                matching = next(
                    (
                        coverage
                        for coverage in self.requirement_coverage
                        if coverage.requirement_id == requirement_id
                    ),
                    None,
                )
                if (
                    matching is None
                    or matching.clarification_id != clarification.clarification_id
                ):
                    raise ValueError(
                        "clarification의 requirement가 같은 coverage에서 질문을 "
                        f"참조하지 않습니다: {requirement_id}"
                    )
        if len(self.planning_notes) != len(set(self.planning_notes)):
            raise ValueError("planning note가 중복됐습니다.")
        return self

    @property
    def canonical_digest(self) -> str:
        return sha256_digest(self)


class RuntimeBindingReceipt(FrozenModel):
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str | None = Field(default=None, max_length=200)
    cwd: str = Field(min_length=3)
    instruction_sources: tuple[str, ...] = ()
    runtime_receipt: dict[str, Any] = Field(default_factory=dict)


class ValidationResultInput(FrozenModel):
    criterion_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    check_type: str = Field(min_length=1, max_length=80)
    status: ValidationStatus
    summary: str = Field(min_length=1, max_length=5000)
    evidence: dict[str, Any]


class AttemptReservation(FrozenModel):
    project_id: str
    revision_id: str
    work_item_id: str
    attempt_id: str
    attempt_no: int = Field(ge=1)
    intent_id: str
    model_id: str
    reasoning_effort: str


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}_[0-9a-f]{32}$")


def new_id(prefix: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", prefix):
        raise ValueError(f"안전하지 않은 ID 접두사입니다: {prefix}")
    return f"{prefix}_{uuid.uuid4().hex}"


def validate_id(value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise CoreDomainError(f"유효하지 않은 FlowMarshal Core ID입니다: {value}")
    return value


def _assert_acyclic(graph: dict[str, list[str]]) -> None:
    indegree = {node: len(dependencies) for node, dependencies in graph.items()}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node, dependencies in graph.items():
        for dependency in dependencies:
            dependents[dependency].append(node)
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    visited = 0
    while ready:
        node = ready.popleft()
        visited += 1
        for dependent in sorted(dependents[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if visited != len(graph):
        raise CoreDomainError("WorkItem dependency graph에 순환이 있습니다.")


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
    ordered: list[str] = []
    while ready:
        node = ready.popleft()
        ordered.append(node)
        for dependent in sorted(dependents[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return tuple(ordered)
