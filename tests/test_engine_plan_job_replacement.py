from __future__ import annotations

import unittest
from datetime import timedelta

from flowmarshal.engine.domain import (
    RuntimeJobKind,
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    utc_now,
)
from flowmarshal.engine.service import EngineServiceError

from tests import test_engine_goal_authorization as authorization_tests
from tests.test_engine_ledger_service import EngineServiceFixture


class EnginePlanJobReplacementTests(EngineServiceFixture):
    """Attempt가 없는 역할 작업도 관측·소비 전에는 Plan 교체로 유실되지 않는다."""

    # 필요한 fixture 도우미만 재사용해 원본 테스트를 중복 실행하지 않는다.
    authorize = authorization_tests.EngineGoalAuthorizationTests.authorize
    internal_revision_evaluation = (
        authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    )
    register_internal_revision = (
        authorization_tests.EngineGoalAuthorizationTests.register_internal_revision
    )

    def setUp(self) -> None:
        super().setUp()
        self.authorize()
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.job = self.service.schedule_runtime_job(
            project_id=self.project_id,
            kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
            checkpoint_key="prepare-before-plan-replacement",
            request={"plan_revision_id": self.plan.plan_revision_id, "task_id": self.task.task_id},
            task_id=self.task.task_id,
            absolute_deadline_at=utc_now() + timedelta(minutes=10),
        )
        self.assertIsNone(self.job.attempt_id)
        with self.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
        self.revision, self.next_task = self.register_internal_revision()

    def ledger_state(self) -> tuple[str, ...]:
        # 전체 테이블을 읽어 binding·receipt·job 관측·History까지 함께 보존하는지 검사한다.
        with self.ledger.read() as connection:
            return tuple(connection.iterdump())

    def start_job(self) -> None:
        job = self.service.start_runtime_job(
            self.job.job_id,
            thread_id="thread-plan-job-replacement",
            turn_id="turn-plan-job-replacement",
        )
        self.assertEqual("thread-plan-job-replacement", job.thread_id)
        self.assertEqual("turn-plan-job-replacement", job.turn_id)

    def record_terminal(self) -> None:
        self.service.record_runtime_job_observation(
            self.job.job_id,
            kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
            payload={"result": {"prepared": True}, "thread_id": "thread-plan-job-replacement",
                     "turn_id": "turn-plan-job-replacement"},
            provider_terminal=True,
            terminal_status="completed",
        )

    def assert_replacement_blocked(self, status: RuntimeJobStatus) -> None:
        job_before = self.service.load_runtime_job(self.job.job_id)
        self.assertEqual(status, job_before.status)
        before = self.ledger_state()

        with self.assertRaisesRegex(EngineServiceError, "PLAN_REPLACEMENT_IN_FLIGHT"):
            self.service.activate_authorized_plan(plan_revision_id=self.revision.plan_revision_id)

        self.assertEqual(before, self.ledger_state())
        self.assertEqual(job_before, self.service.load_runtime_job(self.job.job_id))

    def assert_replacement_allowed(self, status: RuntimeJobStatus) -> None:
        job_before = self.service.load_runtime_job(self.job.job_id)
        self.assertEqual(status, job_before.status)

        self.service.activate_authorized_plan(plan_revision_id=self.revision.plan_revision_id)

        self.assertEqual((self.next_task.task_id,), self.service.list_ready_tasks(self.project_id))
        self.assertEqual(job_before, self.service.load_runtime_job(self.job.job_id))
        with self.ledger.read() as connection:
            active_revision = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id = ?", (self.project_id,)
            ).fetchone()[0]
        self.assertEqual(self.revision.plan_revision_id, active_revision)

    def test_scheduled_preparation_job_blocks_replacement_without_ledger_mutation(self) -> None:
        self.assert_replacement_blocked(RuntimeJobStatus.SCHEDULED)

    def test_running_preparation_job_preserves_actual_thread_turn_binding(self) -> None:
        self.start_job()
        self.assert_replacement_blocked(RuntimeJobStatus.RUNNING)

    def test_interrupt_receipt_does_not_allow_plan_replacement(self) -> None:
        self.start_job()
        self.assertTrue(self.service.begin_runtime_job_interrupt(
            self.job.job_id, payload={"reason": "absolute deadline"}
        ))
        self.service.record_runtime_job_observation(
            self.job.job_id,
            kind=RuntimeJobObservationKind.INTERRUPT_RECEIPT,
            payload={"interrupt_acknowledged": True},
        )
        self.assert_replacement_blocked(RuntimeJobStatus.INTERRUPTING)

    def test_collector_loss_does_not_allow_plan_replacement(self) -> None:
        self.start_job()
        self.service.record_runtime_job_observation(
            self.job.job_id,
            kind=RuntimeJobObservationKind.COLLECTOR_LOST,
            payload={"reason": "collector disconnected"},
        )
        self.assert_replacement_blocked(RuntimeJobStatus.COLLECTOR_LOST)

    def test_provider_terminal_result_must_be_consumed_before_replacement(self) -> None:
        self.start_job()
        self.record_terminal()
        self.assert_replacement_blocked(RuntimeJobStatus.PROVIDER_TERMINAL)

    def test_consumed_preparation_job_allows_replacement_and_preserves_job_evidence(self) -> None:
        self.start_job()
        self.record_terminal()
        self.assertEqual({"prepared": True}, self.service.consume_runtime_job(self.job.job_id))
        self.assert_replacement_allowed(RuntimeJobStatus.CONSUMED)

    def test_cancelled_scheduled_job_allows_replacement(self) -> None:
        self.service.cancel_runtime_job(self.job.job_id, reason="계획 교체 전 미실행 준비 취소")
        self.assert_replacement_allowed(RuntimeJobStatus.CANCELLED)

    def test_cancelled_started_job_preserves_binding_and_blocks_replacement(self) -> None:
        self.start_job()
        self.service.cancel_runtime_job(self.job.job_id, reason="실행 시작 후 취소 요청")
        self.assert_replacement_blocked(RuntimeJobStatus.CANCELLED)

    def test_cancelled_started_job_without_binding_still_blocks_replacement(self) -> None:
        started = self.service.start_runtime_job(self.job.job_id)
        self.assertIsNotNone(started.started_at)
        self.assertIsNone(started.thread_id)
        self.assertIsNone(started.turn_id)
        self.service.cancel_runtime_job(self.job.job_id, reason="binding 수집 전 취소 요청")
        self.assert_replacement_blocked(RuntimeJobStatus.CANCELLED)

    def test_cancelled_started_job_allows_replacement_only_after_terminal_consumption(self) -> None:
        self.start_job()
        self.service.cancel_runtime_job(self.job.job_id, reason="실행 시작 후 취소 요청")
        self.assert_replacement_blocked(RuntimeJobStatus.CANCELLED)

        self.record_terminal()
        self.assert_replacement_blocked(RuntimeJobStatus.PROVIDER_TERMINAL)

        self.assertEqual({"prepared": True}, self.service.consume_runtime_job(self.job.job_id))
        self.assert_replacement_allowed(RuntimeJobStatus.CONSUMED)


if __name__ == "__main__":
    unittest.main()
