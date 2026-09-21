"""E2E-04: 직접 실행한 결정적 검사 실패의 typed implementation 분류와 retryable 계약.

분류기는 evidence 본문을 문자열로 검색하지 않는다. strict
`DeterministicValidationObservation`으로 파싱되고, timeout이 아니며, 종류가
test·command·build이고, 종료 코드가 기대 밖이며, evidence record의 Task·Attempt·
validation 결속이 안의 관측과 맞을 때만 implementation으로 분류한다.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    DeterministicValidationObservation,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    RecoveryEnvelope,
    RepairAction,
)
from flowmarshal.engine.planner_roles import PlanExpansionDraft
from flowmarshal.engine.recovery import (
    EvidenceFirstFailureClassifier,
    FailureSignal,
    recovery_route,
)
from tests.test_engine_fm08_recovery_integration import _plan_expansion


PROJECT_ID = "project_" + "1" * 32
TASK_ID = "task_" + "2" * 32
ATTEMPT_ID = "attempt_" + "3" * 32
OBSERVED_AT = datetime(2026, 9, 21, tzinfo=timezone.utc)


def _observation(**changes) -> DeterministicValidationObservation:
    values = {
        "validation_id": "validation_task",
        "task_id": TASK_ID,
        "argv": ("python", "-c", "raise SystemExit(1)"),
        "working_directory": "/workspace",
        "timeout_seconds": 30,
        "expected_exit_codes": (0,),
        "actual_exit_code": 1,
        "observed_at": OBSERVED_AT,
    } | changes
    return DeterministicValidationObservation(**values)


def _document(
    observation: DeterministicValidationObservation,
    *,
    kind: str = "test",
    serialized: str | None = None,
    **changes,
) -> dict:
    """validation_execution이 남기는 evidence record와 같은 모양의 원장 문서."""

    record = EvidenceRecord(
        evidence_id="evidence_" + "4" * 32,
        project_id=PROJECT_ID,
        task_id=observation.task_id,
        attempt_id=ATTEMPT_ID,
        kind=EvidenceKind(kind),
        source_ref=f"validation:{observation.validation_id}:{observation.argv[0]}",
        observation=(
            observation.model_dump_json()[:10_000] if serialized is None else serialized
        ),
        content_digest=sha256_digest({"kind": kind, "observation": observation}),
        observed_at=observation.observed_at,
    )
    return record.model_dump(mode="json") | changes


def _classify(
    *documents: dict,
    final_response: str | None = None,
    failed_attempt_id: str | None = ATTEMPT_ID,
    failed_validation_id: str | None = "validation_task",
):
    return EvidenceFirstFailureClassifier.classify(FailureSignal(
        terminal_status="validation_failed",
        final_response=final_response,
        provider_payload={"validation_result_id": "validation_result_" + "5" * 32},
        evidence_ids=tuple(item["evidence_id"] for item in documents),
        evidence_documents=documents,
        failed_attempt_id=failed_attempt_id,
        failed_validation_id=failed_validation_id,
    ))


class DirectValidationFailureClassificationTests(unittest.TestCase):
    def test_unexpected_exit_code_of_a_direct_check_is_implementation(self) -> None:
        for kind in ("test", "command", "build"):
            with self.subTest(kind=kind):
                document = _document(_observation(), kind=kind)
                diagnosis = _classify(document)
                self.assertIs(FailureClass.IMPLEMENTATION, diagnosis.failure_class)
                self.assertIs(RepairAction.TASK_REPAIR, diagnosis.repair_action)
                self.assertEqual("direct_evidence", diagnosis.source)
                self.assertEqual((document["evidence_id"],), diagnosis.evidence_ids)
                self.assertIn("validation_task", diagnosis.rationale)
                self.assertEqual("automatic", recovery_route(
                    diagnosis, validation_result_id="validation_result_" + "5" * 32
                ).mode)

    def test_timeout_and_expected_exit_codes_are_not_implementation(self) -> None:
        """timeout은 환경 증거이고, 종료 코드가 기대 안이면 artifact 결함만으로 분류하지 않는다."""

        cases = {
            "timeout": _observation(actual_exit_code=None, timed_out=True),
            "expected_exit": _observation(actual_exit_code=0),
            "other_expected_exit": _observation(
                expected_exit_codes=(0, 1), actual_exit_code=1
            ),
        }
        for label, observation in cases.items():
            with self.subTest(label=label):
                diagnosis = _classify(_document(observation))
                self.assertIsNone(diagnosis.failure_class)
                self.assertEqual("unclassified", diagnosis.source)

    def test_truncated_or_incomplete_observation_is_not_classified(self) -> None:
        """기록 한도에서 잘린 관측과 필수 필드가 빠진 관측은 분류 근거가 아니다."""

        long_output = _observation(stdout="x" * 20_000)
        truncated = long_output.model_dump_json()[:10_000]
        with self.assertRaises(ValueError):
            json.loads(truncated)
        incomplete = json.loads(_observation().model_dump_json())
        incomplete.pop("expected_exit_codes")
        legacy_shape = json.dumps({"passed": False, "exit_code": 1})
        for label, serialized in {
            "truncated": truncated,
            "missing_field": json.dumps(incomplete),
            "legacy_substring_shape": legacy_shape,
        }.items():
            with self.subTest(label=label):
                diagnosis = _classify(_document(_observation(), serialized=serialized))
                self.assertIsNone(diagnosis.failure_class)
                self.assertEqual("unclassified", diagnosis.source)

    def test_other_evidence_kinds_and_model_text_are_not_implementation(self) -> None:
        """file·diff·external_observation과 모델 자칭 문구는 검사 종료 코드 근거가 아니다."""

        observation = _observation()
        for kind in ("file", "diff", "external_observation"):
            with self.subTest(kind=kind):
                diagnosis = _classify(_document(observation, kind=kind))
                self.assertIsNone(diagnosis.failure_class)
        for text in ("target contract violation", "구현 실패"):
            with self.subTest(text=text):
                diagnosis = _classify(_document(
                    observation, kind="file", serialized=json.dumps({"text": text}),
                ))
                self.assertIsNone(diagnosis.failure_class)
        passing = _classify(
            _document(_observation(actual_exit_code=0)),
            final_response="IMPLEMENTATION_ERROR: 모델이 자칭한 원인",
        )
        self.assertIsNone(passing.failure_class)
        self.assertEqual(("IMPLEMENTATION_ERROR",), passing.model_reported_codes)

    def test_evidence_bound_to_another_attempt_task_or_validation_is_not_used(self) -> None:
        """evidence record의 결속이 안의 관측과 다르면 다른 Attempt·검사의 관측으로 보고 버린다."""

        observation = _observation()
        cases = {
            "no_attempt": {"attempt_id": None},
            "other_attempt": {"attempt_id": "attempt_" + "7" * 32},
            "other_task": {"task_id": "task_" + "6" * 32},
            "other_validation": {
                "source_ref": f"validation:validation_other:{observation.argv[0]}"
            },
            "other_argv": {"source_ref": "validation:validation_task:pytest"},
        }
        for label, changes in cases.items():
            with self.subTest(label=label):
                diagnosis = _classify(_document(observation, **changes))
                self.assertIsNone(diagnosis.failure_class)
                self.assertEqual("unclassified", diagnosis.source)
        # 실패한 Attempt·validation을 밝히지 않은 신호(Worker 실패 경로)나 다른 검사의
        # FAIL을 분류할 때는 결속이 맞는 관측이라도 쓰지 않는다.
        for label, binding in {
            "unbound_signal": {"failed_attempt_id": None, "failed_validation_id": None},
            "failed_other_attempt": {"failed_attempt_id": "attempt_" + "7" * 32},
            "failed_other_validation": {"failed_validation_id": "validation_other"},
        }.items():
            with self.subTest(label=label):
                diagnosis = _classify(_document(observation), **binding)
                self.assertIsNone(diagnosis.failure_class)
                self.assertEqual("unclassified", diagnosis.source)

    def test_explicit_codes_still_precede_direct_check_evidence(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier.classify(FailureSignal(
            terminal_status="validation_failed",
            final_response=None,
            provider_payload={"error_code": "CONTEXT_REQUIRED"},
            evidence_ids=("evidence_" + "4" * 32,),
            evidence_documents=(_document(_observation()),),
        ))
        self.assertIs(FailureClass.CONTEXT, diagnosis.failure_class)
        self.assertEqual("explicit_code", diagnosis.source)


class RetryableFailureClassContractTests(unittest.TestCase):
    def test_writer_accepts_only_exact_failure_class_values(self) -> None:
        envelope = RecoveryEnvelope(retryable_failure_classes=("implementation", "context"))
        self.assertEqual(
            (FailureClass.IMPLEMENTATION, FailureClass.CONTEXT),
            envelope.retryable_failure_classes,
        )
        # 원장 reader는 JSON 문자열로 비교하므로 직렬화 값은 그대로 FailureClass 값이다.
        self.assertEqual(
            ["implementation", "context"],
            envelope.model_dump(mode="json")["retryable_failure_classes"],
        )
        for value in ("IMPLEMENTATION", "Implementation", "test_failure", " implementation"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    RecoveryEnvelope(retryable_failure_classes=(value,))
        with self.assertRaises(ValidationError):
            RecoveryEnvelope(retryable_failure_classes=("implementation", "implementation"))

    def test_plan_expander_draft_rejects_unnormalized_failure_classes(self) -> None:
        """Plan 생성 writer는 모델 출력의 대소문자·오타를 조용히 정규화하지 않는다."""

        draft = _plan_expansion(acceptance=["Task 검사가 PASS다."], statement="값을 검사한다.")
        parsed = PlanExpansionDraft.model_validate(draft)
        self.assertIn(
            FailureClass.IMPLEMENTATION, parsed.tasks[0].recovery.retryable_failure_classes
        )
        for value in ("IMPLEMENTATION", "test_failure"):
            with self.subTest(value=value):
                broken = json.loads(json.dumps(draft))
                broken["tasks"][0]["recovery"]["retryable_failure_classes"] = [value]
                with self.assertRaises(ValidationError):
                    PlanExpansionDraft.model_validate(broken)

    def test_provider_schema_limits_values_to_failure_class(self) -> None:
        schema = PlanExpansionDraft.model_json_schema()
        envelope = schema["$defs"]["RecoveryEnvelope"]["properties"]["retryable_failure_classes"]
        self.assertEqual({"$ref": "#/$defs/FailureClass"}, envelope["items"])
        self.assertIn("implementation", envelope["description"])
        self.assertEqual(
            [item.value for item in FailureClass], schema["$defs"]["FailureClass"]["enum"]
        )


if __name__ == "__main__":
    unittest.main()
