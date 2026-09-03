from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any, Literal, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import canonical_json, sha256_digest


CONTEXT_SCHEMA_VERSION = "1.0"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_RELATIVE_FORBIDDEN = re.compile(r"(^|[\\/])\.\.($|[\\/])")


class ContextContractError(ValueError):
    """Gate 0C context 또는 submission 계약 위반."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


class RuntimeRole(StrEnum):
    PLANNER = "planner"
    RUNNER = "runner"
    VALIDATOR = "validator"


class UntrustedSourceKind(StrEnum):
    DOCUMENT = "document"
    SOURCE = "source"
    TOOL_OUTPUT = "tool_output"


class SubmissionStatus(StrEnum):
    READY_FOR_VALIDATION = "ready_for_validation"
    NEEDS_ACCESS = "needs_access"
    FAILED = "failed"


class ValidationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"


def _validate_identifier(value: str) -> str:
    if _ID_RE.fullmatch(value) is None:
        raise ValueError("식별자 형식이 유효하지 않습니다.")
    return value


def _validate_digest(value: str) -> str:
    if _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("SHA-256 digest 형식이 유효하지 않습니다.")
    return value


def _validate_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ValueError("상대 경로만 허용합니다.")
    if re.match(r"^[A-Za-z]:", normalized) or normalized.startswith("//"):
        raise ValueError("절대 경로와 UNC 경로는 허용하지 않습니다.")
    if _RELATIVE_FORBIDDEN.search(normalized):
        raise ValueError("상대 경로에 상위 이동을 사용할 수 없습니다.")
    return normalized


class ScopeSnapshot(StrictFrozenModel):
    resource_id: str
    relative_path: str = "."
    snapshot_digest: str

    _id = field_validator("resource_id")(_validate_identifier)
    _digest = field_validator("snapshot_digest")(_validate_digest)

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        if value == ".":
            return value
        return _validate_relative_path(value)


class CompletionCriterionReference(StrictFrozenModel):
    criterion_id: str
    description: str = Field(min_length=1, max_length=2000)
    verification_digest: str

    _id = field_validator("criterion_id")(_validate_identifier)
    _digest = field_validator("verification_digest")(_validate_digest)


class NamedCheckReference(StrictFrozenModel):
    check_id: str
    definition_digest: str

    _id = field_validator("check_id")(_validate_identifier)
    _digest = field_validator("definition_digest")(_validate_digest)


class UntrustedDataBlock(StrictFrozenModel):
    kind: Literal["untrusted_data"] = "untrusted_data"
    block_id: str
    source_kind: UntrustedSourceKind
    source_resource_id: str
    relative_path: str
    media_type: str = Field(min_length=1, max_length=200)
    content_digest: str
    content: str = Field(max_length=500_000)

    _block_id = field_validator("block_id")(_validate_identifier)
    _resource_id = field_validator("source_resource_id")(_validate_identifier)
    _digest = field_validator("content_digest")(_validate_digest)
    _path = field_validator("relative_path")(_validate_relative_path)

    @model_validator(mode="after")
    def content_digest_matches(self) -> "UntrustedDataBlock":
        expected = sha256_digest(self.content)
        if self.content_digest != expected:
            raise ValueError("비신뢰 본문의 content_digest가 실제 본문과 다릅니다.")
        return self


class ContextBundle(StrictFrozenModel):
    schema_version: Literal["1.0"] = CONTEXT_SCHEMA_VERSION
    bundle_id: str
    role: RuntimeRole
    project_id: str
    revision_id: str
    plan_revision_digest: str
    work_item_id: str
    attempt_id: str
    goal: str = Field(min_length=1, max_length=5000)
    write_scope: ScopeSnapshot | None = None
    read_scopes: tuple[ScopeSnapshot, ...] = ()
    completion_criteria: tuple[CompletionCriterionReference, ...] = ()
    named_checks: tuple[NamedCheckReference, ...] = ()
    policy_digest: str
    untrusted_data: tuple[UntrustedDataBlock, ...] = ()

    _ids = field_validator(
        "bundle_id",
        "project_id",
        "revision_id",
        "work_item_id",
        "attempt_id",
    )(_validate_identifier)
    _digests = field_validator("plan_revision_digest", "policy_digest")(
        _validate_digest
    )

    @model_validator(mode="after")
    def validate_role_shape_and_uniqueness(self) -> "ContextBundle":
        if self.role is RuntimeRole.RUNNER and self.write_scope is None:
            raise ValueError("Runner bundle에는 write_scope가 필요합니다.")
        if self.role is not RuntimeRole.RUNNER and self.write_scope is not None:
            raise ValueError("Planner와 Validator bundle에는 write_scope를 둘 수 없습니다.")
        resource_keys = [
            (scope.resource_id, scope.relative_path) for scope in self.read_scopes
        ]
        if self.write_scope is not None:
            resource_keys.append(
                (self.write_scope.resource_id, self.write_scope.relative_path)
            )
        if len(resource_keys) != len(set(resource_keys)):
            raise ValueError("ContextBundle의 resource scope가 중복됐습니다.")
        criterion_ids = [item.criterion_id for item in self.completion_criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("완료 조건 ID가 중복됐습니다.")
        check_ids = [item.check_id for item in self.named_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("named check ID가 중복됐습니다.")
        block_ids = [item.block_id for item in self.untrusted_data]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("비신뢰 block ID가 중복됐습니다.")
        return self

    @property
    def digest(self) -> str:
        return sha256_digest(self)

    def control_payload(self) -> dict[str, Any]:
        """비신뢰 본문을 제외한, 서명·binding 대상 control payload."""

        payload = self.model_dump(mode="json", exclude={"untrusted_data"})
        # Agent가 strict submission에 되돌려야 하는 digest를 명시하되,
        # digest 자체는 이 파생 field를 제외한 immutable bundle에 대해 계산한다.
        payload["bundle_digest"] = self.digest
        return payload

    def prompt_envelope(self) -> str:
        """control과 비신뢰 데이터를 서로 다른 typed field로 직렬화한다."""

        return canonical_json(
            {
                "envelope_schema": "flowmarshal.context-envelope/1",
                "control": self.control_payload(),
                "untrusted_data": [
                    item.model_dump(mode="json") for item in self.untrusted_data
                ],
            }
        )


class ContextBinding(StrictFrozenModel):
    role: RuntimeRole
    project_id: str
    revision_id: str
    plan_revision_digest: str
    work_item_id: str
    attempt_id: str
    policy_digest: str
    write_snapshot_digest: str | None = None
    read_snapshot_digests: tuple[str, ...] = ()

    _ids = field_validator(
        "project_id", "revision_id", "work_item_id", "attempt_id"
    )(_validate_identifier)
    _digests = field_validator("plan_revision_digest", "policy_digest")(
        _validate_digest
    )

    @field_validator("write_snapshot_digest")
    @classmethod
    def optional_digest(cls, value: str | None) -> str | None:
        return None if value is None else _validate_digest(value)

    @field_validator("read_snapshot_digests")
    @classmethod
    def digest_tuple(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_digest(item) for item in value)


def build_context_bundle(
    *,
    role: RuntimeRole,
    project_id: str,
    revision_id: str,
    plan_revision_digest: str,
    work_item_id: str,
    attempt_id: str,
    goal: str,
    policy_digest: str,
    write_scope: ScopeSnapshot | None = None,
    read_scopes: tuple[ScopeSnapshot, ...] = (),
    completion_criteria: tuple[CompletionCriterionReference, ...] = (),
    named_checks: tuple[NamedCheckReference, ...] = (),
    untrusted_data: tuple[UntrustedDataBlock, ...] = (),
) -> ContextBundle:
    material = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "role": role,
        "project_id": project_id,
        "revision_id": revision_id,
        "plan_revision_digest": plan_revision_digest,
        "work_item_id": work_item_id,
        "attempt_id": attempt_id,
        "goal": goal,
        "write_scope": write_scope,
        "read_scopes": read_scopes,
        "completion_criteria": completion_criteria,
        "named_checks": named_checks,
        "policy_digest": policy_digest,
        "untrusted_data": untrusted_data,
    }
    bundle_id = "context_" + sha256_digest(material).removeprefix("sha256:")[:32]
    return ContextBundle(bundle_id=bundle_id, **material)


def validate_context_binding(bundle: ContextBundle, expected: ContextBinding) -> None:
    scalar_pairs = {
        "role": (bundle.role, expected.role),
        "project_id": (bundle.project_id, expected.project_id),
        "revision_id": (bundle.revision_id, expected.revision_id),
        "plan_revision_digest": (
            bundle.plan_revision_digest,
            expected.plan_revision_digest,
        ),
        "work_item_id": (bundle.work_item_id, expected.work_item_id),
        "attempt_id": (bundle.attempt_id, expected.attempt_id),
        "policy_digest": (bundle.policy_digest, expected.policy_digest),
    }
    for name, (actual, wanted) in scalar_pairs.items():
        if actual != wanted:
            raise ContextContractError(
                "CONTEXT_BINDING_MISMATCH",
                f"ContextBundle의 {name} binding이 권위 값과 다릅니다.",
            )
    actual_write = (
        None if bundle.write_scope is None else bundle.write_scope.snapshot_digest
    )
    if actual_write != expected.write_snapshot_digest:
        raise ContextContractError(
            "RESOURCE_SNAPSHOT_MISMATCH",
            "write scope snapshot digest가 권위 값과 다릅니다.",
        )
    actual_reads = tuple(item.snapshot_digest for item in bundle.read_scopes)
    if actual_reads != expected.read_snapshot_digests:
        raise ContextContractError(
            "RESOURCE_SNAPSHOT_MISMATCH",
            "read scope snapshot digest가 권위 값과 다릅니다.",
        )


class PlanWorkCandidate(StrictFrozenModel):
    client_ref: str
    goal: str = Field(min_length=1, max_length=5000)
    dependencies: tuple[str, ...] = ()
    completion_labels: tuple[str, ...] = Field(min_length=1)

    _id = field_validator("client_ref")(_validate_identifier)


class PlanDraftCandidate(StrictFrozenModel):
    schema_version: Literal["1.0"] = CONTEXT_SCHEMA_VERSION
    bundle_id: str
    bundle_digest: str
    summary: str = Field(min_length=1, max_length=5000)
    work_items: tuple[PlanWorkCandidate, ...] = Field(min_length=1)

    _id = field_validator("bundle_id")(_validate_identifier)
    _digest = field_validator("bundle_digest")(_validate_digest)


class AccessRequestCandidate(StrictFrozenModel):
    resource_label: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    requested_access: Literal["read"] = "read"


class WorkSubmission(StrictFrozenModel):
    schema_version: Literal["1.0"] = CONTEXT_SCHEMA_VERSION
    bundle_id: str
    bundle_digest: str
    status: SubmissionStatus
    changed_files: tuple[str, ...] = ()
    executed_check_ids: tuple[str, ...] = ()
    access_requests: tuple[AccessRequestCandidate, ...] = ()
    summary: str = Field(min_length=1, max_length=5000)

    _id = field_validator("bundle_id")(_validate_identifier)
    _digest = field_validator("bundle_digest")(_validate_digest)

    @field_validator("changed_files")
    @classmethod
    def changed_paths_are_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_relative_path(item) for item in value)

    @field_validator("executed_check_ids")
    @classmethod
    def check_ids_are_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_identifier(item) for item in value)


class CriterionObservation(StrictFrozenModel):
    criterion_id: str
    passed: bool
    observation_digest: str
    risk: str = Field(max_length=2000)
    basis: str = Field(min_length=1, max_length=5000)

    _id = field_validator("criterion_id")(_validate_identifier)
    _digest = field_validator("observation_digest")(_validate_digest)


class ValidationSubmission(StrictFrozenModel):
    schema_version: Literal["1.0"] = CONTEXT_SCHEMA_VERSION
    bundle_id: str
    bundle_digest: str
    verdict: ValidationVerdict
    criteria: tuple[CriterionObservation, ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=5000)

    _id = field_validator("bundle_id")(_validate_identifier)
    _digest = field_validator("bundle_digest")(_validate_digest)

    @model_validator(mode="after")
    def unique_criteria(self) -> "ValidationSubmission":
        identifiers = [item.criterion_id for item in self.criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Validator criterion ID가 중복됐습니다.")
        return self


Submission = PlanDraftCandidate | WorkSubmission | ValidationSubmission
_SubmissionT = TypeVar("_SubmissionT", bound=StrictFrozenModel)


def parse_submission(
    role: RuntimeRole,
    payload: str | bytes | Mapping[str, Any],
) -> Submission:
    try:
        document: Any
        if isinstance(payload, (str, bytes)):
            document = json.loads(payload)
        else:
            document = dict(payload)
        model: type[Submission]
        if role is RuntimeRole.PLANNER:
            model = PlanDraftCandidate
        elif role is RuntimeRole.RUNNER:
            model = WorkSubmission
        else:
            model = ValidationSubmission
        return model.model_validate(document)
    except (ValueError, TypeError) as error:
        raise ContextContractError(
            "SUBMISSION_SCHEMA_INVALID",
            f"{role.value} submission이 엄격한 schema를 통과하지 못했습니다: {error}",
        ) from error


def bind_submission(submission: Submission, bundle: ContextBundle) -> None:
    if submission.bundle_id != bundle.bundle_id or submission.bundle_digest != bundle.digest:
        raise ContextContractError(
            "SUBMISSION_BINDING_MISMATCH",
            "submission이 실행에 사용한 ContextBundle과 결합되지 않았습니다.",
        )
