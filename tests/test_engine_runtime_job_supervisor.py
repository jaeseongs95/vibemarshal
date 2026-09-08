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
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import ExecutionPreparation, ExecutionProposalAdapter
from flowmarshal.engine.operations import ExternalOperationUnknown
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeJobSupervisor
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.service import EngineServiceError
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

    def _core_completion_snapshot(self) -> dict[str, object]:
        with self.prepared.service.ledger.read() as connection:
            return {
                "attempts": [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT id,status FROM attempts WHERE project_id=? ORDER BY rowid",
                        (self.prepared.project_id,),
                    )
                ],
                "tasks": [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT id,status FROM task_contracts WHERE project_id=? ORDER BY position",
                        (self.prepared.project_id,),
                    )
                ],
                "project": tuple(connection.execute(
                    "SELECT run_state,active_plan_revision_id FROM projects WHERE id=?",
                    (self.prepared.project_id,),
                ).fetchone()),
                "goal_verdict_count": connection.execute(
                    "SELECT COUNT(*) FROM goal_verdicts WHERE project_id=?",
                    (self.prepared.project_id,),
                ).fetchone()[0],
            }

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

    def test_execution_preparation_job_never_starts_a_second_role_turn(self) -> None:
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": [
                {
                    "proposal": None,
                    "context_request": {
                        "task_id": self.prepared.task_id,
                        "missing_needs": [{
                            "need_id": "source",
                            "description": "구현 파일이 필요합니다.",
                            "path_hints": ["app.py"],
                            "symbol_hints": [],
                            "tag_hints": [],
                            "required": True,
                        }],
                        "reason": "소스 본문이 필요합니다.",
                    },
                },
                {"proposal": self.prepared.proposal.model_dump(mode="json"),
                 "context_request": None},
            ]
        })
        provider = ExecutionProposalAdapter(
            self.prepared.service,
            runner,
            default_role_configuration(ROOT),
        )
        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        job = supervisor.schedule(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
            checkpoint_key="single-role-turn-per-job",
            request={"task_id": self.prepared.task_id},
            timeout_seconds=5,
            target=lambda: provider.prepare_task(
                project_id=self.prepared.project_id,
                task_id=self.prepared.task_id,
                inventory=self.inventory,
            ),
            task_id=self.prepared.task_id,
        )
        deadline = time.monotonic() + 1
        while job.status is RuntimeJobStatus.RUNNING and time.monotonic() < deadline:
            time.sleep(0.005)
            job = supervisor.tick(job.job_id)
        self.assertEqual(RuntimeJobStatus.PROVIDER_TERMINAL, job.status)
        result = ExecutionPreparation.model_validate(
            self.prepared.service.consume_runtime_job_required_result(job.job_id)
        )
        self.assertIsNotNone(result.context_request)
        self.assertEqual(1, len(runner.calls))

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

    def test_restart_terminal_for_typed_job_requires_explicit_recovery(self) -> None:
        thread = self.runtime.create_thread(
            cwd=self.prepared.workspace, title="typed-restart", model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = self.runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace,
            prompt="test", model="gpt-5.6-sol", effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
            checkpoint_key="typed-result-restart",
            request={"task_id": self.prepared.task_id},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        self.prepared.service.start_runtime_job(
            job.job_id,
            thread_id=thread.binding.thread_id,
            turn_id=turn.binding.turn_id,
        )
        self.runtime.complete(thread.binding.thread_id, response='{"proposal":null}')

        terminal = RuntimeJobSupervisor(
            self.prepared.service, self.runtime,
        ).reattach(job.job_id)
        self.assertEqual(RuntimeJobStatus.PROVIDER_TERMINAL, terminal.status)
        with self.assertRaises(ExternalOperationUnknown):
            self.prepared.service.consume_runtime_job_required_result(job.job_id)

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

    def test_deadline_before_provider_binding_delivers_interrupt_after_late_start(self) -> None:
        thread = self.runtime.create_thread(
            cwd=self.prepared.workspace, title="late-binding", model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = self.runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace,
            prompt="test", model="gpt-5.6-sol", effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="deadline-before-binding",
            request={"test": True},
            absolute_deadline_at=utc_now() - timedelta(milliseconds=1),
            task_id=self.prepared.task_id,
        )
        job = self.prepared.service.start_runtime_job(job.job_id)
        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)

        interrupting = supervisor.tick(job.job_id)
        self.assertEqual(RuntimeJobStatus.INTERRUPTING, interrupting.status)
        self.assertEqual(0, self.runtime.interrupt_calls)

        supervisor.record_role_progress(
            job.job_id,
            {
                "event": "turn_started",
                "thread_id": thread.binding.thread_id,
                "turn_id": turn.binding.turn_id,
            },
        )
        self.assertEqual(1, self.runtime.interrupt_calls)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_job_observations "
                    "WHERE job_id=? AND kind='interrupt_receipt'",
                    (job.job_id,),
                ).fetchone()[0],
            )

    def test_deadline_terminal_observation_grace_ends_as_collector_lost(self) -> None:
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="deadline-grace",
            request={"test": True},
            absolute_deadline_at=utc_now() - timedelta(milliseconds=1),
            task_id=self.prepared.task_id,
        )
        self.prepared.service.start_runtime_job(job.job_id)
        supervisor = RuntimeJobSupervisor(
            self.prepared.service,
            self.runtime,
            terminal_observation_grace_seconds=0,
        )

        observed = supervisor.tick(job.job_id)

        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, observed.status)
        self.assertIsNone(observed.provider_terminal_status)
        with self.prepared.service.ledger.read() as connection:
            terminal_count = connection.execute(
                "SELECT COUNT(*) FROM runtime_job_observations "
                "WHERE job_id=? AND provider_terminal=1",
                (job.job_id,),
            ).fetchone()[0]
        self.assertEqual(0, terminal_count)

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

    def test_restarted_supervisor_close_marks_and_interrupts_bound_job(self) -> None:
        thread = self.runtime.create_thread(
            cwd=self.prepared.workspace, title="close-restart", model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = self.runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace,
            prompt="test", model="gpt-5.6-sol", effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="restart-close-bound-job",
            request={"test": True},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        self.prepared.service.start_runtime_job(
            job.job_id,
            thread_id=thread.binding.thread_id,
            turn_id=turn.binding.turn_id,
        )

        restarted = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        restarted.reattach(job.job_id)
        restarted.close()

        closed = self.prepared.service.load_runtime_job(job.job_id)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, closed.status)
        self.assertEqual(1, self.runtime.interrupt_calls)

    def test_restarted_supervisor_observes_exact_running_turn_before_terminal(self) -> None:
        class TrackingRuntime(FakeCodexRuntime):
            def __init__(inner, inventory):
                super().__init__(inventory)
                inner.create_calls = 0
                inner.start_calls = 0
                inner.resume_calls = 0
                inner.stored_reads = []

            def create_thread(inner, **kwargs):
                inner.create_calls += 1
                return super(TrackingRuntime, inner).create_thread(**kwargs)

            def start_turn(inner, **kwargs):
                inner.start_calls += 1
                return super(TrackingRuntime, inner).start_turn(**kwargs)

            def resume(inner, **kwargs):
                inner.resume_calls += 1
                return super(TrackingRuntime, inner).resume(**kwargs)

            def read_stored(inner, *, thread_id, turn_id=None, timeout_seconds=5.0):
                inner.stored_reads.append((thread_id, turn_id))
                return super(TrackingRuntime, inner).read_stored(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    timeout_seconds=timeout_seconds,
                )

        runtime = TrackingRuntime(self.inventory)
        thread = runtime.create_thread(
            cwd=self.prepared.workspace,
            title="restart",
            model="gpt-5.6-sol",
            developer_instructions="test",
        )
        turn = runtime.start_turn(
            thread_id=thread.binding.thread_id,
            cwd=self.prepared.workspace,
            prompt="test",
            model="gpt-5.6-sol",
            effort="high",
        )
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="running-restart-exact-turn",
            request={"test": True},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        job = self.prepared.service.start_runtime_job(
            job.job_id,
            thread_id=thread.binding.thread_id,
            turn_id=turn.binding.turn_id,
        )
        runtime.create_calls = runtime.start_calls = runtime.resume_calls = 0

        restarted = RuntimeJobSupervisor(self.prepared.service, runtime)
        active = restarted.tick(job.job_id)
        self.assertEqual(RuntimeJobStatus.RUNNING, active.status)
        self.assertIsNone(active.provider_terminal_status)
        self.assertEqual(
            [(thread.binding.thread_id, turn.binding.turn_id)], runtime.stored_reads
        )
        self.assertEqual((0, 0, 0), (
            runtime.create_calls, runtime.start_calls, runtime.resume_calls,
        ))

        runtime.complete(thread.binding.thread_id, response="stored terminal")
        terminal = restarted.tick(job.job_id)
        self.assertEqual(RuntimeJobStatus.PROVIDER_TERMINAL, terminal.status)
        self.assertEqual("completed", terminal.provider_terminal_status)
        self.assertEqual(
            [
                (thread.binding.thread_id, turn.binding.turn_id),
                (thread.binding.thread_id, turn.binding.turn_id),
            ],
            runtime.stored_reads,
        )
        self.assertEqual((0, 0, 0), (
            runtime.create_calls, runtime.start_calls, runtime.resume_calls,
        ))

    def test_slow_create_and_start_turn_keep_dispatch_ticks_bounded_and_idempotent(self) -> None:
        create_started = threading.Event()
        create_release = threading.Event()
        start_started = threading.Event()
        start_release = threading.Event()

        class SlowDispatchRuntime(FakeCodexRuntime):
            def create_thread(inner, **kwargs):
                create_started.set()
                if not create_release.wait(1):
                    raise TimeoutError("create_thread release를 기다리지 못했습니다.")
                return super(SlowDispatchRuntime, inner).create_thread(**kwargs)

            def start_turn(inner, **kwargs):
                start_started.set()
                if not start_release.wait(1):
                    raise TimeoutError("start_turn release를 기다리지 못했습니다.")
                return super(SlowDispatchRuntime, inner).start_turn(**kwargs)

        runtime = SlowDispatchRuntime(self.inventory)
        supervisor = RuntimeJobSupervisor(
            self.prepared.service, runtime, handoff_wait_seconds=0.002,
        )
        dispatcher = EngineDispatcher(
            self.prepared.service, runtime, supervisor=supervisor,
        )
        self.prepared.service.compile_execution_spec(
            self.prepared.proposal, inventory=self.inventory,
        )
        safety_release = threading.Timer(
            0.6, lambda: (create_release.set(), start_release.set())
        )
        safety_release.daemon = True
        safety_release.start()
        self.addCleanup(create_release.set)
        self.addCleanup(start_release.set)
        self.addCleanup(safety_release.cancel)

        started = time.monotonic()
        first = dispatcher.run_once(self.prepared.project_id)
        worker = supervisor._workers[first.runtime_job_id]
        self.addCleanup(lambda: worker.join(1))
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertTrue(create_started.wait(0.2))
        self.assertEqual(RunOnceAction.DISPATCHED, first.action)
        self.assertEqual(RuntimeJobKind.WORKER_TURN, first.runtime_job_kind)
        self.assertIn(first.runtime_job_status, {
            RuntimeJobStatus.SCHEDULED, RuntimeJobStatus.RUNNING,
        })
        create_release.set()
        self.assertTrue(start_started.wait(0.5))

        started = time.monotonic()
        second = dispatcher.run_once(self.prepared.project_id)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertIn(second.action, {RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED})
        with self.prepared.service.ledger.read() as connection:
            attempts = connection.execute(
                "SELECT id FROM attempts WHERE project_id=?",
                (self.prepared.project_id,),
            ).fetchall()
            jobs = connection.execute(
                "SELECT id,checkpoint_key FROM runtime_jobs "
                "WHERE project_id=? AND kind='worker_turn'",
                (self.prepared.project_id,),
            ).fetchall()
        self.assertEqual(1, len(attempts))
        self.assertEqual(first.attempt_id, attempts[0]["id"])
        self.assertEqual(1, len(jobs))
        self.assertEqual(1, len({row["checkpoint_key"] for row in jobs}))
        start_release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_bindingless_collector_loss_yields_to_prepared_effect_recovery(self) -> None:
        self.prepared.service.compile_execution_spec(
            self.prepared.proposal, inventory=self.inventory,
        )

        def crash(point: str) -> None:
            if point == "after_thread_intent":
                raise RuntimeError("exit before provider effect")

        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        dispatcher = EngineDispatcher(
            self.prepared.service,
            self.runtime,
            supervisor=supervisor,
            fault_hook=crash,
        )
        dispatched = dispatcher.run_once(self.prepared.project_id)
        worker = supervisor._workers[dispatched.runtime_job_id]
        worker.join(1)
        self.assertFalse(worker.is_alive())

        lost = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, lost.runtime_job_status)
        recovered = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, recovered.action)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", recovered.blocker_code)
        self.assertEqual(
            RuntimeJobStatus.CANCELLED,
            self.prepared.service.load_runtime_job(dispatched.runtime_job_id).status,
        )
        self.assertEqual(0, self.runtime.create_calls)

    def test_background_lost_create_receipt_is_recovered_without_duplicate_effect(self) -> None:
        self.prepared.service.compile_execution_spec(
            self.prepared.proposal, inventory=self.inventory,
        )

        def crash(point: str) -> None:
            if point == "after_thread_effect":
                raise RuntimeError("exit after provider effect")

        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        dispatcher = EngineDispatcher(
            self.prepared.service,
            self.runtime,
            supervisor=supervisor,
            fault_hook=crash,
        )
        dispatched = dispatcher.run_once(self.prepared.project_id)
        supervisor._workers[dispatched.runtime_job_id].join(1)
        lost = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, lost.runtime_job_status)

        recovered = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, recovered.action)
        self.assertEqual(1, self.runtime.create_calls)
        self.assertEqual(0, self.runtime.turn_calls)
        self.assertEqual(
            RuntimeJobStatus.CANCELLED,
            self.prepared.service.load_runtime_job(dispatched.runtime_job_id).status,
        )
        with self.prepared.service.ledger.read() as connection:
            intent = connection.execute(
                "SELECT status FROM runtime_intents WHERE kind='create_thread'"
            ).fetchone()
            receipts = connection.execute(
                "SELECT COUNT(*) FROM runtime_receipts"
            ).fetchone()[0]
        self.assertEqual("received", intent["status"])
        self.assertEqual(1, receipts)

    def test_slow_runtime_read_keeps_run_once_bounded_and_job_nonterminal(self) -> None:
        read_started = threading.Event()
        read_release = threading.Event()
        read_finished = threading.Event()

        class SlowReadRuntime(FakeCodexRuntime):
            slow_reads = False

            def _wait_for_read(inner):
                if inner.slow_reads:
                    read_started.set()
                    try:
                        if not read_release.wait(1):
                            raise TimeoutError("runtime read release를 기다리지 못했습니다.")
                    finally:
                        read_finished.set()

            def read(inner, **kwargs):
                inner._wait_for_read()
                return super(SlowReadRuntime, inner).read(**kwargs)

            def read_stored(inner, **kwargs):
                inner._wait_for_read()
                return super(SlowReadRuntime, inner).read_stored(**kwargs)

        runtime = SlowReadRuntime(self.inventory)
        supervisor = RuntimeJobSupervisor(
            self.prepared.service, runtime,
            interrupt_timeout_seconds=0.03,
            handoff_wait_seconds=0.002,
            observation_timeout_seconds=0.03,
        )
        dispatcher = EngineDispatcher(
            self.prepared.service, runtime, supervisor=supervisor,
        )
        self.prepared.service.compile_execution_spec(
            self.prepared.proposal, inventory=self.inventory,
        )
        dispatched = dispatcher.run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action)
        dispatch_worker = supervisor._workers[dispatched.runtime_job_id]
        dispatch_worker.join(1)
        self.assertFalse(dispatch_worker.is_alive())
        runtime.slow_reads = True
        safety_release = threading.Timer(0.5, read_release.set)
        safety_release.daemon = True
        safety_release.start()
        self.addCleanup(read_release.set)
        self.addCleanup(safety_release.cancel)
        self.addCleanup(lambda: (read_release.set(), read_finished.wait(1)))

        started = time.monotonic()
        observed = dispatcher.run_once(self.prepared.project_id)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(read_started.wait(0.2))
        self.assertIn(observed.action, {
            RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED, RunOnceAction.BLOCKED,
        })
        self.assertEqual(dispatched.runtime_job_id, observed.runtime_job_id)
        self.assertEqual(RuntimeJobKind.WORKER_TURN, observed.runtime_job_kind)
        with self.prepared.service.ledger.read() as connection:
            attempt = connection.execute(
                "SELECT status FROM attempts WHERE id=?", (dispatched.attempt_id,),
            ).fetchone()
            job = connection.execute(
                "SELECT status,provider_terminal_status FROM runtime_jobs "
                "WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1",
                (dispatched.attempt_id,),
            ).fetchone()
        self.assertIn(attempt["status"], {"reserved", "starting", "running"})
        self.assertIn(job["status"], {
            RuntimeJobStatus.SCHEDULED.value,
            RuntimeJobStatus.RUNNING.value,
            RuntimeJobStatus.COLLECTOR_LOST.value,
            RuntimeJobStatus.INTERRUPTING.value,
        })
        self.assertIsNone(job["provider_terminal_status"])
        read_release.set()
        self.assertTrue(read_finished.wait(0.5))

    def test_unrelated_blocked_tick_does_not_report_a_consumed_job(self) -> None:
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.RECOVERY,
            checkpoint_key="old-consumed-job",
            request={"old": True},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
        )
        self.prepared.service.start_runtime_job(job.job_id)
        self.prepared.service.record_runtime_job_observation(
            job.job_id,
            kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
            payload={"result": {"old": True}},
            provider_terminal=True,
            terminal_status="completed",
        )
        self.prepared.service.consume_runtime_job(job.job_id)

        result = EngineDispatcher(
            self.prepared.service, self.runtime,
        ).run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, result.action)
        self.assertEqual("EXECUTION_SPEC_PROPOSAL_REQUIRED", result.blocker_code)
        self.assertIsNone(result.runtime_job_id)
        self.assertIsNone(result.runtime_job_kind)
        self.assertIsNone(result.runtime_job_status)

    def test_runtime_job_lifecycle_and_close_do_not_decide_core_completion(self) -> None:
        baseline = self._core_completion_snapshot()
        self.assertEqual([], baseline["attempts"])
        self.assertEqual(
            [(self.prepared.task_id, "ready")], baseline["tasks"]
        )
        self.assertEqual("active", baseline["project"][0])
        self.assertEqual(0, baseline["goal_verdict_count"])

        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.RECOVERY,
            checkpoint_key="core-completion-ownership",
            request={"test": "core-ownership"},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        self.assertEqual(baseline, self._core_completion_snapshot())

        job = self.prepared.service.start_runtime_job(job.job_id)
        self.assertEqual(RuntimeJobStatus.RUNNING, job.status)
        self.assertEqual(baseline, self._core_completion_snapshot())

        self.prepared.service.record_runtime_job_observation(
            job.job_id,
            kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
            payload={"stage": "provider-active"},
        )
        self.assertEqual(baseline, self._core_completion_snapshot())

        self.prepared.service.record_runtime_job_observation(
            job.job_id,
            kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
            payload={"result": {"status": "completed"}},
            provider_terminal=True,
            terminal_status="completed",
        )
        self.assertEqual(baseline, self._core_completion_snapshot())

        self.assertEqual(
            {"status": "completed"},
            self.prepared.service.consume_runtime_job(job.job_id),
        )
        self.assertEqual(baseline, self._core_completion_snapshot())
        supervisor.close()
        self.assertEqual(baseline, self._core_completion_snapshot())

    def test_turn_started_progress_binds_once_and_rejects_a_different_turn(self) -> None:
        supervisor = RuntimeJobSupervisor(self.prepared.service, self.runtime)
        job = self.prepared.service.schedule_runtime_job(
            project_id=self.prepared.project_id,
            kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key="role-progress-exact-binding",
            request={"test": "role-progress"},
            absolute_deadline_at=utc_now() + timedelta(seconds=5),
            task_id=self.prepared.task_id,
        )
        job = self.prepared.service.start_runtime_job(job.job_id)
        event = {
            "event": "turn_started",
            "thread_id": "thread_exact_progress",
            "turn_id": "turn_exact_progress",
        }

        supervisor.record_role_progress(job.job_id, event)
        bound = self.prepared.service.load_runtime_job(job.job_id)
        self.assertEqual(RuntimeJobStatus.RUNNING, bound.status)
        self.assertEqual("thread_exact_progress", bound.thread_id)
        self.assertEqual("turn_exact_progress", bound.turn_id)
        with self.prepared.service.ledger.read() as connection:
            first_observations = connection.execute(
                "SELECT COUNT(*) FROM runtime_job_observations WHERE job_id=?",
                (job.job_id,),
            ).fetchone()[0]
            first_history = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                "AND event_type='runtime_job.provider_progress'",
                (job.job_id,),
            ).fetchone()[0]

        supervisor.record_role_progress(job.job_id, dict(event))
        rebound = self.prepared.service.load_runtime_job(job.job_id)
        self.assertEqual(bound, rebound)
        with self.prepared.service.ledger.read() as connection:
            self.assertEqual(
                first_observations,
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_job_observations WHERE job_id=?",
                    (job.job_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                first_history,
                connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                    "AND event_type='runtime_job.provider_progress'",
                    (job.job_id,),
                ).fetchone()[0],
            )

        with self.assertRaisesRegex(
            EngineServiceError, "기존 exact thread/turn과 다릅니다"
        ):
            supervisor.record_role_progress(
                job.job_id,
                event | {"turn_id": "turn_conflicting_progress"},
            )
        self.assertEqual(rebound, self.prepared.service.load_runtime_job(job.job_id))


if __name__ == "__main__":
    unittest.main()
