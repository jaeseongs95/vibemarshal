"""Goal 완료 쓰기는 제출된 PASS보다 원장의 최신 검사 결과를 우선한다."""
from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    CriterionVerdict, EvidenceKind, EvidenceRecord, GoalVerdict,
    GoalVerdictStatus, ValidationResult, ValidationStatus, new_id, utc_now,
)
from flowmarshal.engine.service import EngineServiceError
from tests.test_engine_ledger_service import EngineServiceFixture
from tests import test_engine_goal_authorization as authorization_tests


class GoalVerdictAuthorityTests(EngineServiceFixture):
    internal_revision_evaluation = authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = authorization_tests.EngineGoalAuthorizationTests.register_internal_revision

    def setUp(self):
        super().setUp()
        self.activate()
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        self.evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=self.project_id,
            task_id=self.task.task_id, attempt_id=attempt.attempt_id,
            kind=EvidenceKind.TEST, source_ref="synthetic:validator-observation",
            observation="합성 검사 관측", content_digest=sha256_digest("합성 검사 관측"),
            observed_at=utc_now(),
        )
        self.service.record_evidence(self.evidence)
        self.record_result(task=True)
        self.service.complete_task(self.task.task_id)
        self.integration = self.record_result(task=False)

    def record_result(self, *, task, status=ValidationStatus.PASS):
        result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id=(self.task.validations[0].validation_id if task else
                           self.plan.definition.integration_validations[0].validation_id),
            task_id=self.task.task_id if task else None, status=status,
            evidence_ids=() if status is ValidationStatus.NOT_RUN else (self.evidence.evidence_id,),
            rationale="원장 순서에 결속한 합성 검사 결과", evaluated_at=utc_now(),
        )
        self.service.record_validation(project_id=self.project_id,
            plan_revision_id=self.plan.plan_revision_id, result=result)
        return result

    def verdict(self, result_ids=None):
        return GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"),
            goal_contract_digest=self.goal.definition_digest,
            plan_activation_digest=self.plan.activation_digest,
            status=GoalVerdictStatus.SATISFIED,
            criteria=(CriterionVerdict(criterion_id="ac_one", status=ValidationStatus.PASS,
                evidence_ids=(self.evidence.evidence_id,), rationale="제출된 완료 주장"),),
            integration_validation_result_ids=(self.integration.validation_result_id,)
                if result_ids is None else result_ids, evaluated_at=utc_now(),
        )

    def record_verdict(self, verdict):
        self.service.record_goal_verdict(project_id=self.project_id,
            plan_revision_id=self.plan.plan_revision_id, verdict=verdict)

    def assert_rejected_atomically(self, verdict):
        with self.ledger.read() as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("goal_verdicts", "history_events")}
        with self.assertRaises(EngineServiceError):
            self.record_verdict(verdict)
        with self.ledger.read() as connection:
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
            self.assertEqual("active", connection.execute(
                "SELECT status FROM plan_revisions WHERE id=?", (self.plan.plan_revision_id,)).fetchone()[0])
        self.assertEqual(before, after)

    def test_current_task_and_integration_pass_can_complete(self):
        self.record_verdict(self.verdict())
        with self.ledger.read() as connection:
            self.assertEqual("completed", connection.execute(
                "SELECT status FROM plan_revisions WHERE id=?", (self.plan.plan_revision_id,)).fetchone()[0])

    def test_latest_integration_fail_cannot_be_overridden_by_old_pass(self):
        self.record_result(task=False, status=ValidationStatus.FAIL)
        self.assert_rejected_atomically(self.verdict())

    def test_latest_integration_inconclusive_cannot_be_overridden_by_old_pass(self):
        self.record_result(task=False, status=ValidationStatus.INCONCLUSIVE)
        self.assert_rejected_atomically(self.verdict())

    def test_latest_task_fail_cannot_be_overridden_by_completed_status(self):
        self.record_result(task=True, status=ValidationStatus.FAIL)
        self.assert_rejected_atomically(self.verdict())

    def test_latest_task_not_run_cannot_be_overridden_by_completed_status(self):
        self.record_result(task=True, status=ValidationStatus.NOT_RUN)
        self.assert_rejected_atomically(self.verdict())

    def test_old_pass_id_cannot_replace_latest_integration_pass_id(self):
        self.record_result(task=False)
        self.assert_rejected_atomically(self.verdict())

    def test_latest_integration_pass_can_supersede_fail(self):
        self.record_result(task=False, status=ValidationStatus.FAIL)
        self.integration = self.record_result(task=False)
        self.record_verdict(self.verdict())

    def test_superseded_plan_verdict_cannot_clear_current_active_plan(self):
        replacement, _ = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=replacement.plan_revision_id)
        with self.ledger.read() as connection:
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("goal_verdicts", "history_events")}
        with self.assertRaises(EngineServiceError):
            self.record_verdict(self.verdict())
        with self.ledger.read() as connection:
            project = connection.execute("SELECT active_plan_revision_id,run_state FROM projects").fetchone()
            self.assertEqual(replacement.plan_revision_id, project["active_plan_revision_id"])
            self.assertNotEqual("completed", project["run_state"])
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
