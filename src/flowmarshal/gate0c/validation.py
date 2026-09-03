from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..canonical import sha256_digest
from ..context import (
    SubmissionStatus,
    ValidationSubmission,
    ValidationVerdict,
    WorkSubmission,
)
from ..path_policy import PathInspection, inspect_resource


class ValidationContractError(ValueError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class NamedCheckKind(StrEnum):
    FILE_EXISTS = "file_exists"
    FILE_SHA256 = "file_sha256"
    JSON_VALUE = "json_value"


class NamedCheckDefinition(StrictFrozenModel):
    check_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    kind: NamedCheckKind
    relative_path: str
    expected_sha256: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    json_pointer: str | None = None
    expected_json_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_shape(self) -> "NamedCheckDefinition":
        _safe_relative_path(self.relative_path)
        if self.kind is NamedCheckKind.FILE_SHA256 and self.expected_sha256 is None:
            raise ValueError("file_sha256 check에는 expected_sha256이 필요합니다.")
        if self.kind is NamedCheckKind.JSON_VALUE and (
            self.json_pointer is None or self.expected_json_digest is None
        ):
            raise ValueError("json_value check에는 pointer와 expected digest가 필요합니다.")
        if self.kind is not NamedCheckKind.JSON_VALUE and self.json_pointer is not None:
            raise ValueError("json_pointer는 json_value check에서만 허용합니다.")
        return self

    @property
    def digest(self) -> str:
        return sha256_digest(self)


class CoreCriterion(StrictFrozenModel):
    criterion_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    check: NamedCheckDefinition


class NamedCheckResult(StrictFrozenModel):
    check_id: str
    passed: bool
    observation_digest: str
    reason_code: str
    detail: str


class CoreValidationDecision(StrictFrozenModel):
    passed: bool
    reason_codes: tuple[str, ...]
    named_check_results: tuple[NamedCheckResult, ...]
    actual_changed_files: tuple[str, ...]
    workspace_before_digest: str
    workspace_after_digest: str
    validator_advisory_digest: str
    runner_submission_digest: str

    @property
    def digest(self) -> str:
        return sha256_digest(self)


def _safe_relative_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:", normalized)
        or ".." in normalized.split("/")
    ):
        raise ValidationContractError(
            "CHECK_PATH_INVALID",
            "named check는 workspace 내부 상대 경로만 사용할 수 있습니다.",
        )
    return normalized


def _read_json_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValidationContractError(
            "CHECK_DEFINITION_INVALID", "JSON pointer는 빈 값 또는 /로 시작해야 합니다."
        )
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise ValidationContractError(
                "CHECK_OBSERVATION_FAILED", f"JSON pointer를 찾을 수 없습니다: {pointer}"
            )
    return current


def run_named_check(definition: NamedCheckDefinition, workspace: Path) -> NamedCheckResult:
    relative = _safe_relative_path(definition.relative_path)
    target = workspace / Path(relative)
    try:
        target.resolve(strict=False).relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise ValidationContractError(
            "CHECK_PATH_INVALID", "named check 대상이 workspace 밖입니다."
        ) from error

    if definition.kind is NamedCheckKind.FILE_EXISTS:
        passed = target.is_file()
        observation = {"exists": passed, "relative_path": relative}
        return NamedCheckResult(
            check_id=definition.check_id,
            passed=passed,
            observation_digest=sha256_digest(observation),
            reason_code="CHECK_PASS" if passed else "FILE_MISSING",
            detail="파일이 존재합니다." if passed else "파일이 존재하지 않습니다.",
        )

    try:
        data = target.read_bytes()
    except OSError as error:
        return NamedCheckResult(
            check_id=definition.check_id,
            passed=False,
            observation_digest=sha256_digest({"error": type(error).__name__}),
            reason_code="FILE_READ_FAILED",
            detail=f"파일을 읽지 못했습니다: {type(error).__name__}",
        )
    content_digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if definition.kind is NamedCheckKind.FILE_SHA256:
        passed = content_digest == definition.expected_sha256
        return NamedCheckResult(
            check_id=definition.check_id,
            passed=passed,
            observation_digest=sha256_digest(
                {"relative_path": relative, "content_digest": content_digest}
            ),
            reason_code="CHECK_PASS" if passed else "CONTENT_DIGEST_MISMATCH",
            detail="파일 digest가 일치합니다." if passed else "파일 digest가 다릅니다.",
        )

    try:
        document = json.loads(data)
        observed = _read_json_pointer(document, definition.json_pointer or "")
        observed_digest = sha256_digest(observed)
    except (ValueError, UnicodeDecodeError, ValidationContractError) as error:
        return NamedCheckResult(
            check_id=definition.check_id,
            passed=False,
            observation_digest=sha256_digest({"error": type(error).__name__}),
            reason_code="JSON_OBSERVATION_FAILED",
            detail=f"JSON 값을 관측하지 못했습니다: {type(error).__name__}",
        )
    passed = observed_digest == definition.expected_json_digest
    return NamedCheckResult(
        check_id=definition.check_id,
        passed=passed,
        observation_digest=sha256_digest(
            {"relative_path": relative, "pointer": definition.json_pointer, "value": observed}
        ),
        reason_code="CHECK_PASS" if passed else "JSON_VALUE_MISMATCH",
        detail="JSON 값이 일치합니다." if passed else "JSON 값이 다릅니다.",
    )


def _file_map(inspection: PathInspection) -> dict[str, str]:
    return {
        entry.relative_path: sha256_digest(
            {
                "object": entry.identity.object_key,
                "content": entry.content_digest,
                "size": entry.identity.size,
            }
        )
        for entry in inspection.entries
        if not entry.identity.is_directory
    }


def changed_files(before: PathInspection, after: PathInspection) -> tuple[str, ...]:
    left = _file_map(before)
    right = _file_map(after)
    return tuple(
        sorted(
            {
                path
                for path in set(left) | set(right)
                if left.get(path) != right.get(path)
            },
            key=str.casefold,
        )
    )


def evaluate_completion(
    *,
    workspace: Path,
    before: PathInspection,
    after: PathInspection,
    runner_submission: WorkSubmission,
    validator_submission: ValidationSubmission,
    criteria: tuple[CoreCriterion, ...],
    allowed_changed_files: tuple[str, ...],
) -> CoreValidationDecision:
    # 검증 직전에 path identity, reparse, ADS, hardlink와 현재 manifest를 다시 계산한다.
    current = inspect_resource(workspace)
    if current.snapshot_digest != after.snapshot_digest:
        raise ValidationContractError(
            "PATH_IDENTITY_DRIFT", "Validator 이후 workspace가 다시 바뀌었습니다."
        )
    actual = changed_files(before, after)
    expected_criteria = tuple(item.criterion_id for item in criteria)
    observed_criteria = tuple(item.criterion_id for item in validator_submission.criteria)
    reasons: list[str] = []
    if runner_submission.status is not SubmissionStatus.READY_FOR_VALIDATION:
        reasons.append("RUNNER_NOT_READY")
    if tuple(sorted(runner_submission.changed_files, key=str.casefold)) != actual:
        reasons.append("RUNNER_CHANGE_CLAIM_MISMATCH")
    allowed = tuple(sorted((_safe_relative_path(item) for item in allowed_changed_files), key=str.casefold))
    if any(path not in allowed for path in actual):
        reasons.append("UNEXPECTED_FILE_CHANGE")
    if validator_submission.verdict is not ValidationVerdict.PASS:
        reasons.append("VALIDATOR_DID_NOT_PASS")
    if observed_criteria != expected_criteria:
        reasons.append("VALIDATOR_CRITERIA_MISMATCH")
    elif any(not item.passed for item in validator_submission.criteria):
        reasons.append("VALIDATOR_CRITERION_FAILED")

    check_results = tuple(run_named_check(item.check, workspace) for item in criteria)
    if any(not item.passed for item in check_results):
        # Validator가 pass라고 해도 Core의 고정 check 실패가 우선한다.
        reasons.append("CORE_NAMED_CHECK_FAILED")
    return CoreValidationDecision(
        passed=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        named_check_results=check_results,
        actual_changed_files=actual,
        workspace_before_digest=before.snapshot_digest,
        workspace_after_digest=after.snapshot_digest,
        validator_advisory_digest=sha256_digest(validator_submission),
        runner_submission_digest=sha256_digest(runner_submission),
    )
