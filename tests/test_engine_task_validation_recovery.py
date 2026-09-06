from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    RecoveryAssessment,
    RepairAction,
    RunOnceAction,
    RunOnceOutcome,
    ValidationResult,
    ValidationStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineServiceError
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


class TaskValidationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.workspace, _ = _copy_fixture(ROOT, base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(
            workspace=self.workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=default_role_configuration(ROOT),
        )
        self.service = self.prepared.service
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)

    def evidence(
        self,
        *,
        kind: EvidenceKind = EvidenceKind.TEST,
        attempt_id: str | None = None,
        observed_at=None,
    ) -> EvidenceRecord:
        observed_at = observed_at or utc_now()
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=self.prepared.project_id,
            task_id=self.prepared.task_id,
            attempt_id=attempt_id,
            kind=kind,
            source_ref="task-validation-recovery",
            observation="직접 실패 또는 통과 관측",
            content_digest=sha256_digest({"kind": kind.value, "at": observed_at}),
            observed_at=observed_at,
        )
        self.service.record_evidence(evidence)
        return evidence

    def validation(
        self,
        status: ValidationStatus,
        evidence_ids: tuple[str, ...],
        *,
        evaluated_at,
    ) -> ValidationResult:
        result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id="validation_unittest",
            task_id=self.prepared.task_id,
            status=status,
            evidence_ids=evidence_ids,
            rationale=f"직접 검증 {status.value}",
            evaluated_at=evaluated_at,
        )
        self.service.record_validation(
            project_id=self.prepared.project_id,
            plan_revision_id=self.prepared.plan_revision_id,
            result=result,
        )
        return result

    def successful_worker_then_failed_validation(self):
        attempt = self.service.reserve_attempt(task_id=self.prepared.task_id)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        tied_time = utc_now()
        failure_evidence = self.evidence(attempt_id=attempt.attempt_id, observed_at=tied_time)
        failed = self.validation(
            ValidationStatus.FAIL,
            (failure_evidence.evidence_id,),
            evaluated_at=tied_time,
        )
        self.service.block_task_from_validation(
            task_id=self.prepared.task_id,
            detail="validation FAIL: validation_unittest",
        )
        return attempt, failed, failure_evidence, tied_time

    @staticmethod
    def assessment(attempt_id: str, evidence_id: str) -> RecoveryAssessment:
        return RecoveryAssessment(
            assessment_id=new_id("recovery_assessment"),
            attempt_id=attempt_id,
            failure_class=FailureClass.IMPLEMENTATION,
            action=RepairAction.TASK_REPAIR,
            rationale="직접 test evidence가 Worker 결과의 구현 결함을 입증한다.",
            new_evidence_ids=(evidence_id,),
            same_failure_replan_count=0,
            goal_replan_count=0,
        )

    def test_validation_failure_retry_preserves_history_and_requires_new_worker_validation(self):
        first, failed, evidence, tied_time = self.successful_worker_then_failed_validation()
        assessment = self.assessment(first.attempt_id, evidence.evidence_id)
        self.service.retry_task(
            task_id=self.prepared.task_id,
            recovery_assessment=assessment,
            failed_validation_result_id=failed.validation_result_id,
        )

        # retry event 뒤라도 새 Worker 성공 전 결과는 그 Worker의 검증으로 재사용하지 않는다.
        premature_evidence = self.evidence(observed_at=tied_time)
        premature = self.validation(
            ValidationStatus.PASS,
            (premature_evidence.evidence_id,),
            evaluated_at=tied_time,
        )
        second = self.service.reserve_attempt(task_id=self.prepared.task_id)
        self.service.finish_attempt(attempt_id=second.attempt_id, succeeded=True)
        stale_after_worker = self.validation(
            ValidationStatus.PASS,
            (evidence.evidence_id,),
            evaluated_at=tied_time,
        )

        with self.service.ledger.read() as connection:
            current = self.service.effective_task_validation_results(
                connection, self.prepared.task_id
            )
            task = connection.execute(
                "SELECT * FROM task_contracts WHERE id = ?", (self.prepared.task_id,)
            ).fetchone()
        self.assertEqual((), current)
        with self.assertRaisesRegex(EngineServiceError, "모든 Task validation"):
            self.service.complete_task(self.prepared.task_id)

        dispatcher = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory))
        sentinel = RunOnceOutcome(
            action=RunOnceAction.VALIDATED,
            project_id=self.prepared.project_id,
            task_id=self.prepared.task_id,
            detail="fresh validation dispatched",
        )
        with patch.object(dispatcher, "_run_deterministic_validation", return_value=sentinel) as run:
            self.assertEqual(sentinel, dispatcher._advance_validation(task))
            run.assert_called_once()

        fresh_evidence = self.evidence(attempt_id=second.attempt_id, observed_at=tied_time)
        passed = self.validation(
            ValidationStatus.PASS,
            (fresh_evidence.evidence_id,),
            evaluated_at=tied_time,
        )
        self.assertEqual((), self.service.complete_task(self.prepared.task_id))

        with self.service.ledger.read() as connection:
            results = connection.execute(
                "SELECT id, status FROM validation_results WHERE task_id = ? ORDER BY rowid",
                (self.prepared.task_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT id, status, failure_class FROM attempts WHERE task_id = ? "
                "AND kind = 'execution' ORDER BY attempt_no",
                (self.prepared.task_id,),
            ).fetchall()
            retry = connection.execute(
                "SELECT payload_json FROM history_events WHERE project_id = ? "
                "AND event_type = 'task.retry_enabled' AND entity_id = ?",
                (self.prepared.project_id, self.prepared.task_id),
            ).fetchone()
        self.assertEqual(
            [(failed.validation_result_id, "fail"), (premature.validation_result_id, "pass"),
             (stale_after_worker.validation_result_id, "pass"), (passed.validation_result_id, "pass")],
            [(row["id"], row["status"]) for row in results],
        )
        self.assertEqual(
            [(first.attempt_id, "succeeded", None), (second.attempt_id, "succeeded", None)],
            [(row["id"], row["status"], row["failure_class"]) for row in attempts],
        )
        self.assertEqual(
            assessment.assessment_id,
            json.loads(retry["payload_json"])["recovery_assessment_id"],
        )
        self.assertTrue(self.service.ledger.verify_history(self.prepared.project_id))

    def test_retry_rejects_unbound_or_non_direct_recovery_evidence(self):
        first, failed, direct, _ = self.successful_worker_then_failed_validation()
        model = self.evidence(kind=EvidenceKind.MODEL_REVIEW)
        # 실패 결과에 model evidence를 함께 보존해도 그것만으로 직접 복구 근거가 되지 않는다.
        failed = self.validation(
            ValidationStatus.FAIL,
            (direct.evidence_id, model.evidence_id),
            evaluated_at=utc_now(),
        )
        assessment = self.assessment(first.attempt_id, model.evidence_id)
        with self.assertRaisesRegex(EngineServiceError, "직접 evidence"):
            self.service.retry_task(
                task_id=self.prepared.task_id,
                recovery_assessment=assessment,
                failed_validation_result_id=failed.validation_result_id,
            )

    def test_unknown_effect_blocks_validation_recovery_without_mutation(self):
        first, failed, evidence, _ = self.successful_worker_then_failed_validation()
        assessment = self.assessment(first.attempt_id, evidence.evidence_id)
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO runtime_intents "
                "(id,attempt_id,kind,idempotency_key,request_digest,request_json,status,prepared_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'unknown',?,?)",
                (
                    "runtime_intent_" + "1" * 32,
                    first.attempt_id,
                    "external_effect",
                    "task-validation-recovery-unknown",
                    "sha256:" + "1" * 64,
                    "{}",
                    tx.now,
                    tx.now,
                ),
            )
        with self.assertRaisesRegex(EngineServiceError, "unknown 외부 효과"):
            self.service.retry_task(
                task_id=self.prepared.task_id,
                recovery_assessment=assessment,
                failed_validation_result_id=failed.validation_result_id,
            )
        with self.service.ledger.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM recovery_assessments").fetchone()[0]
        self.assertEqual(0, count)

    def test_environment_recovery_combines_validation_and_worker_cause_evidence(self):
        attempt = self.service.reserve_attempt(task_id=self.prepared.task_id)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        validation_evidence = self.evidence(kind=EvidenceKind.TEST)
        worker_error = self.evidence(
            kind=EvidenceKind.EXTERNAL_OBSERVATION,
            attempt_id=attempt.attempt_id,
        )
        failed = self.validation(
            ValidationStatus.FAIL,
            (validation_evidence.evidence_id,),
            evaluated_at=utc_now(),
        )
        self.service.block_task_from_validation(
            task_id=self.prepared.task_id,
            detail="validation FAIL: validation_unittest",
        )
        assessment = RecoveryAssessment(
            assessment_id=new_id("recovery_assessment"),
            attempt_id=attempt.attempt_id,
            failure_class=FailureClass.ENVIRONMENT,
            action=RepairAction.CONTINUE,
            rationale="FAIL 파일 관측과 같은 Worker의 실행 호스트 누락 관측을 함께 분류했다.",
            new_evidence_ids=(validation_evidence.evidence_id, worker_error.evidence_id),
            same_failure_replan_count=0,
            goal_replan_count=0,
        )

        self.service.retry_task(
            task_id=self.prepared.task_id,
            recovery_assessment=assessment,
            failed_validation_result_id=failed.validation_result_id,
        )
        with self.service.ledger.read() as connection:
            task = connection.execute(
                "SELECT status FROM task_contracts WHERE id = ?", (self.prepared.task_id,)
            ).fetchone()
        self.assertEqual("materialized", task["status"])

    def test_run_once_reports_typed_validation_recovery_blocker(self):
        attempt, failed, evidence, _ = self.successful_worker_then_failed_validation()
        dispatcher = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory))

        outcome = dispatcher.run_once(self.prepared.project_id)

        self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
        self.assertEqual("TASK_VALIDATION_RECOVERY_REQUIRED", outcome.blocker_code)
        self.assertEqual(self.prepared.task_id, outcome.task_id)
        self.assertEqual(attempt.attempt_id, outcome.attempt_id)
        self.assertEqual(failed.validation_result_id, outcome.validation_result_id)
        self.assertEqual((evidence.evidence_id,), outcome.evidence_ids)
        self.assertIsNone(outcome.failure_class)
        self.assertIsNone(outcome.suggested_repair_action)

    def test_ledger_retry_limit_blocks_before_assessment_write(self):
        first, failed, evidence, _ = self.successful_worker_then_failed_validation()
        assessment = self.assessment(first.attempt_id, evidence.evidence_id)
        with self.service.ledger.transaction() as tx:
            for ordinal in (1, 2):
                tx.history(
                    self.prepared.project_id,
                    "task.retry_enabled",
                    "task_contract",
                    self.prepared.task_id,
                    {"fixture_prior_retry": ordinal},
                )
        before = self.service.status(self.prepared.project_id)["history_count"]
        with self.assertRaisesRegex(EngineServiceError, "recovery 한도"):
            self.service.retry_task(
                task_id=self.prepared.task_id,
                recovery_assessment=assessment,
                failed_validation_result_id=failed.validation_result_id,
            )
        self.assertEqual(before, self.service.status(self.prepared.project_id)["history_count"])
        with self.service.ledger.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM recovery_assessments").fetchone()[0]
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
