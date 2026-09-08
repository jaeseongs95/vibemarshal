from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

from flowmarshal.engine.domain import (
    RunOnceAction,
    RuntimeJobKind,
    RuntimeJobStatus,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import ExecutionPreparation
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeJobSupervisor
from tests.test_engine_qualification import qualification_inventory


ROOT = Path(__file__).resolve().parents[1]


class RuntimeJobSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        workspace, _ = _copy_fixture(ROOT, base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=default_role_configuration(ROOT),
        )
        self.runtime = FakeCodexRuntime(self.inventory)

    def test_every_post_activation_role_kind_starts_with_a_short_tick(self) -> None:
        supervisor = RuntimeJobSupervisor(
            self.prepared.service, self.runtime, handoff_wait_seconds=0.002,
        )
        for index, kind in enumerate(RuntimeJobKind):
            release = threading.Event()
            started = time.monotonic()
            job = supervisor.schedule(
                project_id=self.prepared.project_id,
                kind=kind,
                checkpoint_key=f"test:{index}:{kind.value}",
                request={"kind": kind.value},
                timeout_seconds=5,
                target=lambda current=kind: (
                    release.wait(1), {"kind": current.value}
                )[1],
            )
            self.assertLess(time.monotonic() - started, 0.15)
            self.assertEqual(RuntimeJobStatus.RUNNING, job.status)
            release.set()
            deadline = time.monotonic() + 1
            while job.status is RuntimeJobStatus.RUNNING and time.monotonic() < deadline:
                time.sleep(0.005)
                job = supervisor.tick(job.job_id)
            self.assertEqual(RuntimeJobStatus.PROVIDER_TERMINAL, job.status)
            self.assertEqual({"kind": kind.value}, self.prepared.service.consume_runtime_job(job.job_id))

    def test_slow_execution_spec_provider_does_not_block_run_once(self) -> None:
        provider_started = threading.Event()
        provider_release = threading.Event()
        provider_finished = threading.Event()

        class SlowProvider:
            def prepare_task(inner, *, project_id, task_id, inventory):
                del inner, project_id, task_id, inventory
                provider_started.set()
                if not provider_release.wait(1):
                    raise TimeoutError("테스트 provider 해제 신호를 기다리지 못했습니다.")
                provider_finished.set()
                return ExecutionPreparation(proposal=self.prepared.proposal)

        supervisor = RuntimeJobSupervisor(
            self.prepared.service, self.runtime, handoff_wait_seconds=0.002,
        )
        dispatcher = EngineDispatcher(
            self.prepared.service, self.runtime,
            proposal_provider=SlowProvider(), supervisor=supervisor,
        )
        result = dispatcher.run_once(self.prepared.project_id)
        self.assertTrue(provider_started.wait(0.5))
        self.assertFalse(provider_finished.is_set())
        self.assertEqual(RunOnceAction.DISPATCHED, result.action)
        self.assertEqual(RuntimeJobKind.EXECUTION_SPEC_PREPARE, result.runtime_job_kind)
        self.assertEqual(RuntimeJobStatus.RUNNING, result.runtime_job_status)
        provider_release.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result = dispatcher.run_once(self.prepared.project_id)
            if result.action is RunOnceAction.MATERIALIZED:
                break
            time.sleep(0.005)
        self.assertEqual(RunOnceAction.MATERIALIZED, result.action)
        self.assertEqual(RuntimeJobStatus.CONSUMED, result.runtime_job_status)

    def test_collector_loss_and_restart_are_not_provider_terminal(self) -> None:
        thread = self.runtime.create_thread(
            cwd=self.prepared.workspace, title="test", model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = self.runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace,
            prompt="test", model="gpt-5.6-sol", effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id, kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="restart-test", request={"test": True},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        job = self.prepared.service.start_runtime_job(
            job.job_id, thread_id=thread.binding.thread_id, turn_id=turn.binding.turn_id,
        )
        first = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        job = first.mark_collector_lost(job.job_id, reason="fault injection")
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, job.status)
        self.assertIsNone(job.provider_terminal_status)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                "ready",
                connection.execute(
                    "SELECT status FROM task_contracts WHERE id=?", (self.prepared.task_id,),
                ).fetchone()[0],
            )

        self.runtime.complete(thread.binding.thread_id, response="stored terminal")
        restarted = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        job = restarted.reattach(job.job_id)
        self.assertEqual(RuntimeJobStatus.PROVIDER_TERMINAL, job.status)
        self.assertEqual("completed", job.provider_terminal_status)
        self.prepared.service.consume_runtime_job(job.job_id)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                "ready",
                connection.execute(
                    "SELECT status FROM task_contracts WHERE id=?", (self.prepared.task_id,),
                ).fetchone()[0],
            )
            kinds = [
                row[0] for row in connection.execute(
                    "SELECT kind FROM runtime_job_observations WHERE job_id=? ORDER BY rowid",
                    (job.job_id,),
                )
            ]
        self.assertIn("collector_lost", kinds)
        self.assertIn("collector_reattached", kinds)
        self.assertIn("provider_terminal", kinds)

    def test_deadline_interrupt_is_bounded_and_not_terminal(self) -> None:
        class SlowInterruptRuntime(FakeCodexRuntime):
            def interrupt(inner, **kwargs):
                time.sleep(0.3)
                return super(SlowInterruptRuntime, inner).interrupt(**kwargs)

        runtime = SlowInterruptRuntime(self.inventory)
        thread = runtime.create_thread(
            cwd=self.prepared.workspace, title="test", model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace,
            prompt="test", model="gpt-5.6-sol", effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id, kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="deadline-test", request={"test": True},
            absolute_deadline_at=utc_now() + timedelta(milliseconds=10),
            task_id=self.prepared.task_id,
        )
        self.prepared.service.start_runtime_job(
            job.job_id, thread_id=thread.binding.thread_id, turn_id=turn.binding.turn_id,
        )
        time.sleep(0.02)
        supervisor = RuntimeJobSupervisor(
            self.prepared.service, runtime, interrupt_timeout_seconds=0.03,
        )
        started = time.monotonic()
        observed = supervisor.tick(job.job_id)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(RuntimeJobStatus.INTERRUPTING, observed.status)
        self.assertIsNone(observed.provider_terminal_status)
        with self.prepared.service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT kind,provider_terminal FROM runtime_job_observations WHERE job_id=? ORDER BY rowid",
                (job.job_id,),
            ).fetchall()
        self.assertIn(("interrupt_requested", 0), [tuple(row) for row in rows])
        self.assertIn(("interrupt_receipt", 0), [tuple(row) for row in rows])
        supervisor.tick(job.job_id)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_job_observations WHERE job_id=? AND kind='interrupt_requested'",
                    (job.job_id,),
                ).fetchone()[0],
            )

    def test_sdk_close_is_bounded_and_does_not_decide_completion(self) -> None:
        class SlowCloseRuntime(FakeCodexRuntime):
            def close(inner, *, timeout_seconds=5.0):
                del timeout_seconds
                time.sleep(0.3)

        runtime = SlowCloseRuntime(self.inventory)
        supervisor = RuntimeJobSupervisor(
            self.prepared.service, runtime, interrupt_timeout_seconds=0.03,
            handoff_wait_seconds=0.001,
        )
        release = threading.Event()
        job = supervisor.schedule(
            project_id=self.prepared.project_id, kind=RuntimeJobKind.RECOVERY,
            checkpoint_key="close-test", request={"test": True}, timeout_seconds=5,
            target=lambda: (release.wait(1), {"done": True})[1],
        )
        started = time.monotonic()
        supervisor.close()
        self.assertLess(time.monotonic() - started, 0.15)
        job = self.prepared.service.load_runtime_job(job.job_id)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, job.status)
        self.assertIsNone(job.provider_terminal_status)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                "ready",
                connection.execute(
                    "SELECT status FROM task_contracts WHERE id=?", (self.prepared.task_id,),
                ).fetchone()[0],
            )
        release.set()


if __name__ == "__main__":
    unittest.main()
