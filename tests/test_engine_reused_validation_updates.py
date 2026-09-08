from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import EvidenceKind, EvidenceRecord, ValidationResult, ValidationStatus, new_id, utc_now
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from tests.test_engine_ledger_service import EngineServiceFixture
from tests import test_engine_plan_completion_reuse as reuse_tests


class EngineReusedValidationUpdateTests(EngineServiceFixture):
    internal_revision_evaluation = reuse_tests.EnginePlanCompletionReuseTests.internal_revision_evaluation
    register_internal_revision = reuse_tests.EnginePlanCompletionReuseTests.register_internal_revision
    finish_worker_and_validate = reuse_tests.EnginePlanCompletionReuseTests.finish_worker_and_validate
    status_of = reuse_tests.EnginePlanCompletionReuseTests.status_of
    source_rows = reuse_tests.EnginePlanCompletionReuseTests.source_rows

    def reused_revision(self):
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        return revision, task

    def record_failure(self, revision, task):
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=self.project_id, task_id=task.task_id,
            kind=EvidenceKind.TEST, source_ref="synthetic:independent-recheck",
            observation="재사용 후 독립 재검사에서 실패를 관측했다.",
            content_digest=sha256_digest({"passed": False}), observed_at=utc_now(),
        )
        self.service.record_evidence(evidence)
        result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id=task.validations[0].validation_id, task_id=task.task_id,
            status=ValidationStatus.FAIL, evidence_ids=(evidence.evidence_id,),
            rationale="새 검사 실패는 이전 재사용 PASS보다 최신 관측이다.", evaluated_at=utc_now(),
        )
        self.service.record_validation(
            project_id=self.project_id, plan_revision_id=revision.plan_revision_id, result=result,
        )
        return result

    def test_unchanged_completion_can_be_reused_across_multiple_revisions(self):
        self.finish_worker_and_validate()
        original = self.source_rows()
        for _ in range(2):
            _, task = self.reused_revision()
            self.assertEqual("completed", self.status_of(task.task_id))
        self.assertEqual(original, self.source_rows())
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_new_failure_on_reused_task_is_effective_and_blocks_next_reuse(self):
        self.finish_worker_and_validate()
        revision, task = self.reused_revision()
        failure = self.record_failure(revision, task)
        before = self.source_rows()
        with self.ledger.read() as connection:
            results = self.service.effective_task_validation_results(connection, task.task_id)
            latest = {row["validation_id"]: row for row in results}
            self.assertEqual(failure.validation_result_id, latest[failure.validation_id]["id"])
            self.assertEqual("fail", latest[failure.validation_id]["status"])
        _, replacement = self.reused_revision()
        self.assertEqual("ready", self.status_of(replacement.task_id))
        self.assertEqual("completed", self.status_of(task.task_id))
        self.assertEqual(before, self.source_rows())

    def assert_late_failure_blocks_goal(self, location):
        self.finish_worker_and_validate()
        middle_plan, middle_task = self.reused_revision()
        current_plan, current_task = self.reused_revision()
        failed_plan, failed_task = {
            "source": (self.plan, self.task), "middle": (middle_plan, middle_task),
            "current": (current_plan, current_task),
        }[location]
        failure = self.record_failure(failed_plan, failed_task)
        # 통합 PASS가 이미 있어도 최신 Task FAIL을 덮어쓸 수 없다.
        self.service.record_validation(
            project_id=self.project_id, plan_revision_id=current_plan.plan_revision_id,
            result=ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=current_plan.definition.integration_validations[0].validation_id,
                status=ValidationStatus.PASS, evidence_ids=failure.evidence_ids,
                rationale="Goal 판정 순서를 검증하는 합성 통합 관측", evaluated_at=utc_now(),
            ),
        )
        before = self.source_rows()
        outcome = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory)).run_once(self.project_id)
        self.assertNotEqual("completed", outcome.action.value)
        with self.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM goal_verdicts WHERE status = 'satisfied'",
            ).fetchone()[0])
        self.assertEqual(before, self.source_rows())
        self.assertEqual("completed", self.status_of(current_task.task_id))

    def test_source_failure_after_reuse_blocks_current_goal_completion(self):
        self.assert_late_failure_blocks_goal("source")

    def test_intermediate_failure_after_reuse_blocks_current_goal_completion(self):
        self.assert_late_failure_blocks_goal("middle")

    def test_current_failure_is_not_overwritten_by_inherited_pass_at_goal_test(self):
        self.assert_late_failure_blocks_goal("current")


if __name__ == "__main__":
    unittest.main()
