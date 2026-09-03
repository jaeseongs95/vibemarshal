from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.context import (
    CriterionObservation,
    SubmissionStatus,
    ValidationSubmission,
    ValidationVerdict,
    WorkSubmission,
)
from flowmarshal.gate0c.validation import (
    CoreCriterion,
    NamedCheckDefinition,
    NamedCheckKind,
    evaluate_completion,
)
from flowmarshal.path_policy import inspect_resource


@unittest.skipUnless(os.name == "nt", "native Windows path identity가 필요합니다.")
class Gate0CValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="flowmarshal-validation-")
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".flowmarshal-synthetic-root").write_text("1", encoding="utf-8")
        self.before = inspect_resource(self.workspace)
        self.bundle_id = "context_validation"
        self.bundle_digest = sha256_digest("bundle")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submissions(self, *, claimed=("result.txt",), validator_pass=True):
        runner = WorkSubmission(
            bundle_id=self.bundle_id,
            bundle_digest=self.bundle_digest,
            status=SubmissionStatus.READY_FOR_VALIDATION,
            changed_files=claimed,
            summary="작업 완료 후보",
        )
        validator = ValidationSubmission(
            bundle_id="context_validator",
            bundle_digest=sha256_digest("validator-bundle"),
            verdict=ValidationVerdict.PASS if validator_pass else ValidationVerdict.FAIL,
            criteria=(
                CriterionObservation(
                    criterion_id="result_content",
                    passed=validator_pass,
                    observation_digest=sha256_digest("observation"),
                    risk="없음",
                    basis="result.txt를 읽어 확인함",
                ),
            ),
            summary="검사 후보",
        )
        return runner, validator

    def criterion(self, expected: bytes) -> tuple[CoreCriterion, ...]:
        return (
            CoreCriterion(
                criterion_id="result_content",
                check=NamedCheckDefinition(
                    check_id="result_sha256",
                    kind=NamedCheckKind.FILE_SHA256,
                    relative_path="result.txt",
                    expected_sha256="sha256:" + hashlib.sha256(expected).hexdigest(),
                ),
            ),
        )

    def test_separate_advisory_and_core_checks_must_both_pass(self) -> None:
        content = "정상 결과".encode()
        (self.workspace / "result.txt").write_bytes(content)
        after = inspect_resource(self.workspace)
        runner, validator = self.submissions()
        decision = evaluate_completion(
            workspace=self.workspace,
            before=self.before,
            after=after,
            runner_submission=runner,
            validator_submission=validator,
            criteria=self.criterion(content),
            allowed_changed_files=("result.txt",),
        )
        self.assertTrue(decision.passed, decision.reason_codes)

    def test_false_completion_and_unexpected_change_fail(self) -> None:
        content = b"actual"
        (self.workspace / "result.txt").write_bytes(content)
        (self.workspace / "unexpected.txt").write_text("x", encoding="utf-8")
        after = inspect_resource(self.workspace)
        runner, validator = self.submissions(claimed=("result.txt",))
        decision = evaluate_completion(
            workspace=self.workspace,
            before=self.before,
            after=after,
            runner_submission=runner,
            validator_submission=validator,
            criteria=self.criterion(content),
            allowed_changed_files=("result.txt",),
        )
        self.assertFalse(decision.passed)
        self.assertIn("RUNNER_CHANGE_CLAIM_MISMATCH", decision.reason_codes)
        self.assertIn("UNEXPECTED_FILE_CHANGE", decision.reason_codes)

    def test_validator_pass_cannot_override_core_failure(self) -> None:
        (self.workspace / "result.txt").write_bytes(b"wrong")
        after = inspect_resource(self.workspace)
        runner, validator = self.submissions()
        decision = evaluate_completion(
            workspace=self.workspace,
            before=self.before,
            after=after,
            runner_submission=runner,
            validator_submission=validator,
            criteria=self.criterion(b"expected"),
            allowed_changed_files=("result.txt",),
        )
        self.assertFalse(decision.passed)
        self.assertIn("CORE_NAMED_CHECK_FAILED", decision.reason_codes)

    def test_model_cannot_add_raw_command_to_named_check(self) -> None:
        with self.assertRaises(ValidationError):
            NamedCheckDefinition.model_validate(
                {
                    "check_id": "injected",
                    "kind": "file_exists",
                    "relative_path": "result.txt",
                    "raw_command": "Remove-Item -Recurse C:\\\\",
                }
            )


if __name__ == "__main__":
    unittest.main()
