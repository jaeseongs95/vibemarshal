from __future__ import annotations

import json
import unittest

from flowmarshal.engine.domain import RunOnceAction, ThreadBinding, new_id, utc_now
from flowmarshal.engine.runtime import (
    EngineDispatcher,
    FakeCodexRuntime,
    RuntimePolicyError,
)
from flowmarshal.engine.service import EngineService, EngineServiceError
from flowmarshal.engine.worker_prompt import PromptArtifactStore
from tests import test_engine_execution_automation as fixtures


class _MutablePolicyRuntime(FakeCodexRuntime):
    policy_changed = False

    def verify_execution_policy(self, cwd):
        evidence = super().verify_execution_policy(cwd)
        if self.policy_changed:
            return evidence.model_copy(update={"approval_policy": "on-request"})
        return evidence


class EngineEffectCheckpointRecoveryTests(unittest.TestCase):
    setUp = fixtures.ExecutionAutomationTests.setUp
    prepared = fixtures.ExecutionAutomationTests.prepared

    def _materialized(self, name: str, *, runtime=None):
        prepared, default_runtime = self.prepared(name)
        selected_runtime = default_runtime if runtime is None else runtime(self.inventory)
        dispatcher = EngineDispatcher(prepared.service, selected_runtime)
        result = dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        self.assertEqual(RunOnceAction.MATERIALIZED, result.action)
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions "
                "WHERE task_id = ? AND is_current = 1",
                (prepared.task_id,),
            ).fetchone()
        return prepared, selected_runtime, json.loads(row["payload_json"])

    def _change_effect_input(self, case, prepared, runtime, spec):
        if case == "target":
            (prepared.workspace / "app.py").write_text(
                "def add(left, right): return left + right\n", encoding="utf-8"
            )
        elif case == "context":
            (prepared.workspace / "AGENTS.md").write_text(
                "예약 뒤 바뀐 프로젝트 지침", encoding="utf-8"
            )
        elif case == "state":
            current = prepared.service.load_current_state(
                prepared.project_id,
                prepared.service.load_active_goal(prepared.project_id).definition_digest,
            )
            prepared.service.record_state_snapshot(
                current.model_copy(
                    update={
                        "snapshot_id": new_id("snapshot"),
                        "version": current.version + 1,
                        "observed_at": utc_now(),
                    }
                )
            )
        elif case == "prompt":
            binding = spec["definition"]["context_manifest"]["prompt_binding"]
            from flowmarshal.engine.domain import PromptBinding

            PromptArtifactStore(prepared.service.ledger.artifact_root).path_for(
                PromptBinding.model_validate(binding)
            ).write_bytes(b"broken-after-intent")
        elif case == "model":
            selected = spec["definition"]["executor"]["model"]
            runtime.inventory = runtime.inventory.model_copy(
                update={
                    "models": tuple(
                        item for item in runtime.inventory.models if item.model != selected
                    )
                }
            )
        else:
            runtime.policy_changed = True

    def test_each_effect_time_binding_change_blocks_before_create(self):
        cases = ("target", "context", "state", "prompt", "model", "policy")
        for case in cases:
            with self.subTest(case=case):
                runtime_factory = _MutablePolicyRuntime if case == "policy" else None
                prepared, runtime, spec = self._materialized(
                    f"effect-preflight-{case}", runtime=runtime_factory
                )
                changed = False

                def mutate(point: str) -> None:
                    nonlocal changed
                    if point != "after_thread_intent" or changed:
                        return
                    changed = True
                    self._change_effect_input(case, prepared, runtime, spec)

                with self.assertRaises((EngineServiceError, RuntimePolicyError)):
                    EngineDispatcher(
                        prepared.service, runtime, fault_hook=mutate
                    ).run_once(prepared.project_id)
                self.assertTrue(changed)
                self.assertEqual(0, runtime.create_calls)
                with prepared.service.ledger.read() as connection:
                    intent = connection.execute(
                        "SELECT id,status FROM runtime_intents WHERE kind = 'create_thread'"
                    ).fetchone()
                    marker_count = connection.execute(
                        "SELECT COUNT(*) FROM history_events WHERE entity_id = ? "
                        "AND event_type = 'runtime.effect_not_started'",
                        (intent["id"],),
                    ).fetchone()[0]
                self.assertEqual("prepared", intent["status"])
                self.assertEqual(1, marker_count)

    def test_each_effect_time_binding_change_blocks_before_start(self):
        cases = ("target", "context", "state", "prompt", "model", "policy")
        for case in cases:
            with self.subTest(case=case):
                runtime_factory = _MutablePolicyRuntime if case == "policy" else None
                prepared, runtime, spec = self._materialized(
                    f"start-preflight-{case}", runtime=runtime_factory
                )
                changed = False

                def mutate(point: str) -> None:
                    nonlocal changed
                    if point != "after_turn_intent" or changed:
                        return
                    changed = True
                    self._change_effect_input(case, prepared, runtime, spec)

                with self.assertRaises((EngineServiceError, RuntimePolicyError)):
                    EngineDispatcher(
                        prepared.service, runtime, fault_hook=mutate
                    ).run_once(prepared.project_id)
                self.assertTrue(changed)
                self.assertEqual(1, runtime.create_calls)
                self.assertEqual(0, runtime.turn_calls)
                with prepared.service.ledger.read() as connection:
                    intent = connection.execute(
                        "SELECT id,status FROM runtime_intents WHERE kind = 'start_turn'"
                    ).fetchone()
                    marker_count = connection.execute(
                        "SELECT COUNT(*) FROM history_events WHERE entity_id = ? "
                        "AND event_type = 'runtime.effect_not_started'",
                        (intent["id"],),
                    ).fetchone()[0]
                self.assertEqual("prepared", intent["status"])
                self.assertEqual(1, marker_count)

    def test_each_immutable_effect_time_binding_change_blocks_before_resume(self):
        cases = ("context", "state", "prompt", "model", "policy")
        for case in cases:
            with self.subTest(case=case):
                runtime_factory = _MutablePolicyRuntime if case == "policy" else None
                prepared, runtime, spec = self._materialized(
                    f"resume-preflight-{case}", runtime=runtime_factory
                )
                dispatcher = EngineDispatcher(prepared.service, runtime)
                dispatched = dispatcher.run_once(prepared.project_id)
                with prepared.service.ledger.read() as connection:
                    binding = ThreadBinding.model_validate_json(
                        connection.execute(
                            "SELECT binding_json FROM attempts WHERE id = ?",
                            (dispatched.attempt_id,),
                        ).fetchone()[0]
                    )
                runtime.interrupt(
                    thread_id=binding.thread_id, turn_id=binding.turn_id
                )
                changed = False

                def mutate(point: str) -> None:
                    nonlocal changed
                    if point != "after_resume_intent" or changed:
                        return
                    changed = True
                    self._change_effect_input(case, prepared, runtime, spec)

                blocked = None
                try:
                    blocked = EngineDispatcher(
                        EngineService(prepared.service.ledger),
                        runtime,
                        fault_hook=mutate,
                    ).run_once(prepared.project_id)
                except (EngineServiceError, RuntimePolicyError):
                    pass
                if blocked is not None:
                    self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
                self.assertTrue(changed)
                self.assertEqual(0, runtime.resume_calls)
                self.assertEqual(1, runtime.turn_calls)
                with prepared.service.ledger.read() as connection:
                    intent = connection.execute(
                        "SELECT id,status FROM runtime_intents "
                        "WHERE kind = 'resume_turn'"
                    ).fetchone()
                    marker_count = connection.execute(
                        "SELECT COUNT(*) FROM history_events WHERE entity_id = ? "
                        "AND event_type = 'runtime.effect_not_started'",
                        (intent["id"],),
                    ).fetchone()[0]
                self.assertEqual("prepared", intent["status"])
                self.assertEqual(1, marker_count)

    def test_lost_receipt_is_recovered_from_exact_trace_without_blind_effect(self):
        for point, expected_create, expected_turn in (
            ("after_thread_effect", 1, 0),
            ("after_turn_effect", 1, 1),
        ):
            with self.subTest(point=point):
                prepared, runtime, _ = self._materialized(f"lost-{point}")

                def crash(actual: str) -> None:
                    if actual == point:
                        raise RuntimeError("abrupt exit after provider effect")

                with self.assertRaisesRegex(RuntimeError, "abrupt exit"):
                    EngineDispatcher(
                        prepared.service, runtime, fault_hook=crash
                    ).run_once(prepared.project_id)
                before = (runtime.create_calls, runtime.turn_calls)
                restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
                recovered = restarted.run_once(prepared.project_id)
                self.assertEqual(RunOnceAction.OBSERVED, recovered.action)
                self.assertEqual(before, (runtime.create_calls, runtime.turn_calls))
                self.assertEqual((expected_create, expected_turn), before)
                with prepared.service.ledger.read() as connection:
                    intents = connection.execute(
                        "SELECT kind,status FROM runtime_intents ORDER BY rowid"
                    ).fetchall()
                    receipt_count = connection.execute(
                        "SELECT COUNT(*) FROM runtime_receipts"
                    ).fetchone()[0]
                self.assertTrue(all(row["status"] == "received" for row in intents))
                self.assertEqual(len(intents), receipt_count)

    def test_prepared_intent_without_trace_never_blindly_creates(self):
        prepared, runtime, _ = self._materialized("prepared-no-trace")

        def crash(point: str) -> None:
            if point == "after_thread_intent":
                raise RuntimeError("exit before provider call")

        with self.assertRaisesRegex(RuntimeError, "before provider"):
            EngineDispatcher(
                prepared.service, runtime, fault_hook=crash
            ).run_once(prepared.project_id)
        restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
        blocked = restarted.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", blocked.blocker_code)
        self.assertEqual(0, runtime.create_calls)
        self.assertEqual(0, runtime.turn_calls)

    def test_lost_resume_receipt_is_observed_then_starts_without_second_resume(self):
        prepared, runtime, _ = self._materialized("lost-resume")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(
                connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?",
                    (dispatched.attempt_id,),
                ).fetchone()[0]
            )
        runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)

        def crash(point: str) -> None:
            if point == "after_resume_effect":
                raise RuntimeError("resume receipt lost")

        with self.assertRaisesRegex(RuntimeError, "resume receipt lost"):
            EngineDispatcher(
                prepared.service, runtime, fault_hook=crash
            ).run_once(prepared.project_id)
        self.assertEqual(1, runtime.resume_calls)
        restarted = EngineDispatcher(EngineService(prepared.service.ledger), runtime)
        recovered = restarted.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, recovered.action)
        self.assertEqual(1, runtime.resume_calls)
        continued = restarted.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, continued.action)
        self.assertEqual(1, runtime.resume_calls)
        self.assertEqual(2, runtime.turn_calls)

    def test_public_resume_reuses_received_intent_without_second_provider_call(self):
        prepared, runtime, _ = self._materialized("public-resume")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(
                connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?",
                    (dispatched.attempt_id,),
                ).fetchone()[0]
            )
        runtime.interrupt(thread_id=binding.thread_id, turn_id=binding.turn_id)
        first = dispatcher.resume_attempt(dispatched.attempt_id)
        second = EngineDispatcher(
            EngineService(prepared.service.ledger), runtime
        ).resume_attempt(dispatched.attempt_id)
        self.assertEqual(1, runtime.resume_calls)
        self.assertEqual(first.binding, second.binding)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_intents "
                    "WHERE kind = 'resume_turn' AND status = 'received'"
                ).fetchone()[0],
            )

    def test_partial_write_resume_is_allowed_but_immutable_change_is_rejected(self):
        for changed_path, allowed in (("app.py", True), ("AGENTS.md", False)):
            with self.subTest(changed_path=changed_path):
                prepared, runtime, _ = self._materialized(
                    f"resume-{changed_path.replace('.', '-')}"
                )
                dispatcher = EngineDispatcher(prepared.service, runtime)
                dispatched = dispatcher.run_once(prepared.project_id)
                with prepared.service.ledger.read() as connection:
                    binding = ThreadBinding.model_validate_json(
                        connection.execute(
                            "SELECT binding_json FROM attempts WHERE id = ?",
                            (dispatched.attempt_id,),
                        ).fetchone()[0]
                    )
                runtime.interrupt(
                    thread_id=binding.thread_id, turn_id=binding.turn_id
                )
                (prepared.workspace / changed_path).write_text(
                    "Worker partial write\n" if allowed else "immutable policy changed\n",
                    encoding="utf-8",
                )
                result = EngineDispatcher(
                    EngineService(prepared.service.ledger), runtime
                ).run_once(prepared.project_id)
                if allowed:
                    self.assertEqual(RunOnceAction.DISPATCHED, result.action)
                    self.assertEqual(1, runtime.resume_calls)
                    self.assertEqual(2, runtime.turn_calls)
                else:
                    self.assertEqual(RunOnceAction.BLOCKED, result.action)
                    self.assertEqual("STALE_EXECUTION_INPUT", result.blocker_code)
                    self.assertEqual(0, runtime.resume_calls)
                    self.assertEqual(1, runtime.turn_calls)


if __name__ == "__main__":
    unittest.main()
