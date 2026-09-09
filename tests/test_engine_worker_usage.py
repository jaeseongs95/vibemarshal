from __future__ import annotations

import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.domain import RunOnceAction, ThreadBinding
from flowmarshal.engine.runtime import CodexAppServerRuntime, EngineDispatcher
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests import test_engine_execution_automation as fixtures


class WorkerUsageTests(unittest.TestCase):
    """Worker provider usage는 execution Attempt의 종료 관측에만 귀속한다."""

    setUp = fixtures.ExecutionAutomationTests.setUp
    prepared = fixtures.ExecutionAutomationTests.prepared

    def dispatched(self, name: str):
        prepared, runtime = self.prepared(name=name)
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self.assertEqual(
            RunOnceAction.MATERIALIZED,
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal).action,
        )
        outcome = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, outcome.action)
        assert outcome.attempt_id is not None
        return prepared, runtime, dispatcher, outcome.attempt_id

    def turn(self, service: EngineService, attempt_id: str) -> tuple[str, str, dict[str, object]]:
        with service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["binding_json"])
            row = connection.execute(
                "SELECT request_json FROM runtime_intents WHERE attempt_id = ? AND kind = 'start_turn' "
                "ORDER BY rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        assert binding.turn_id is not None
        return binding.thread_id, binding.turn_id, json.loads(row["request_json"])

    @staticmethod
    def payload(thread_id: str, turn_id: str, request: dict[str, object], *, usage=None,
                usage_scope: str = "turn", **extra: object) -> dict[str, object]:
        result: dict[str, object] = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "prompt_digest": request["prompt_digest"],
            "usage_scope": usage_scope,
        }
        if usage is not None:
            result["usage"] = usage
        result.update(extra)
        return result

    @staticmethod
    def usage(input_tokens=120, cached_input_tokens=20, output_tokens=50, reasoning_tokens=10):
        return {
            "inputTokens": input_tokens,
            "cachedInputTokens": cached_input_tokens,
            "outputTokens": output_tokens,
            "reasoningOutputTokens": reasoning_tokens,
            "totalTokens": input_tokens + output_tokens,
        }

    def worker_rows(self, service: EngineService):
        with service.ledger.read() as connection:
            return [json.loads(row["payload_json"]) for row in connection.execute(
                "SELECT payload_json FROM budget_usage WHERE stage = 'execution' ORDER BY rowid"
            )]

    def completed_adapter(self, service: EngineService, attempt_id: str, *, close_calls: list[str]):
        thread_id, turn_id, request = self.turn(service, attempt_id)
        future: Future[object] = Future()
        adapter = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        adapter._turn_futures = {thread_id: (SimpleNamespace(id=turn_id, interrupt=lambda: None), future)}
        adapter._completion_observers = {}
        adapter._ephemeral_thread_ids = set()
        adapter._turn_usage_context = {thread_id: {"prompt_digest": request["prompt_digest"]}}
        adapter._codex = SimpleNamespace(close=lambda: close_calls.append("closed"))
        adapter.register_completion_observer(
            thread_id=thread_id,
            turn_id=turn_id,
            observer=lambda observation: service.record_worker_usage(
                attempt_id=attempt_id,
                thread_id=observation.thread_id,
                turn_id=turn_id,
                terminal_status=observation.terminal_status or "failed",
                provider_payload=observation.payload,
                output_digest=sha256_digest(observation.final_response or ""),
            ),
        )
        future.set_result(SimpleNamespace(
            id=turn_id,
            status="completed",
            final_response="callback result",
            usage=SimpleNamespace(model_dump=lambda **_kwargs: {"total": self.usage(30, 5, 8, 2)}),
            duration_ms=12,
            items=[],
        ))
        return adapter

    def test_turn_usage_preserves_raw_receipt_and_prompt_estimate_separately(self):
        prepared, _, _, attempt_id = self.dispatched("normal")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        raw = self.usage()
        provider_payload = self.payload(
            thread_id, turn_id, request, usage=raw, duration_ms=37, usage_source="turn/read"
        )

        recorded = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed", provider_payload=provider_payload,
            output_digest=sha256_digest("worker final report"),
        )

        assert recorded is not None
        self.assertTrue(recorded.usage_available)
        self.assertEqual((120, 20, 50, 10), (
            recorded.input_tokens, recorded.cached_input_tokens,
            recorded.output_tokens, recorded.reasoning_tokens,
        ))
        self.assertEqual("turn", recorded.usage_scope)
        self.assertEqual("provider_turn", recorded.attribution_basis)
        self.assertEqual(raw, recorded.provider_observation["payload"]["usage"])
        self.assertEqual(provider_payload, recorded.provider_observation["payload"])
        self.assertEqual(request["execution_spec_digest"], recorded.execution_spec_digest)
        self.assertEqual(request["prompt_binding_digest"], recorded.prompt_binding_digest)
        self.assertGreater(recorded.prompt_token_estimate or 0, 1)
        self.assertNotEqual(recorded.prompt_token_estimate, recorded.input_tokens)
        self.assertEqual(sha256_digest("worker final report"), recorded.output_digest)
        projection = recorded.model_dump(mode="json")
        self.assertEqual("2.0", projection["binding_provenance_version"])
        self.assertEqual(request["model"], projection["requested_model"])
        self.assertEqual(request["effort"], projection["requested_effort"])
        self.assertIsNone(projection["observed_model"])
        self.assertIsNone(projection["observed_effort"])
        self.assertIsNotNone(projection["provider_inventory_digest"])
        self.assertIsNotNone(projection["adapter_capability_digest"])
        with prepared.service.ledger.read() as connection:
            stored = json.loads(connection.execute(
                "SELECT payload_json FROM budget_usage WHERE id=?", (recorded.usage_id,)
            ).fetchone()["payload_json"])
        self.assertEqual(projection["requested_model"], stored["requested_model"])
        self.assertIn("observed_model", stored)

    def test_observed_model_requires_complete_raw_provider_provenance(self):
        cases = (
            ("marker-missing", {"observed_model": "echo-model", "observed_effort": "high"}, (None, None)),
            ("request-echo", {
                "model_observation_source": "request_echo",
                "observed_model": "echo-model", "observed_effort": "high",
            }, (None, None)),
            ("partial", {
                "model_observation_source": "provider_raw_response",
                "observed_model": "provider-model",
            }, (None, None)),
            ("provider-raw", {
                "model_observation_source": "provider_raw_response",
                "observed_model": "provider-model", "observed_effort": "high",
            }, ("provider-model", "high")),
        )
        for name, model_fields, expected in cases:
            with self.subTest(name=name):
                prepared, _, _, attempt_id = self.dispatched("model-observation-" + name)
                thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
                recorded = prepared.service.record_worker_usage(
                    attempt_id=attempt_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    terminal_status="completed",
                    provider_payload=self.payload(
                        thread_id,
                        turn_id,
                        request,
                        usage=self.usage(),
                        **model_fields,
                    ),
                )
                assert recorded is not None
                self.assertEqual(expected, (recorded.observed_model, recorded.observed_effort))
                self.assertEqual(
                    "provider_raw_response" if expected[0] is not None else None,
                    recorded.binding_provenance["observed"],
                )

    def test_measured_zero_and_unavailable_usage_remain_distinct(self):
        measured, _, _, measured_attempt = self.dispatched("measured-zero")
        thread_id, turn_id, request = self.turn(measured.service, measured_attempt)
        zero = measured.service.record_worker_usage(
            attempt_id=measured_attempt, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(thread_id, turn_id, request, usage=self.usage(0, 0, 0, 0)),
        )
        assert zero is not None
        self.assertTrue(zero.usage_available)
        self.assertEqual((0, 0, 0, 0), (
            zero.input_tokens, zero.cached_input_tokens, zero.output_tokens, zero.reasoning_tokens,
        ))

        unavailable, _, _, unavailable_attempt = self.dispatched("unavailable")
        thread_id, turn_id, request = self.turn(unavailable.service, unavailable_attempt)
        missing = unavailable.service.record_worker_usage(
            attempt_id=unavailable_attempt, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(thread_id, turn_id, request, usage_scope="unavailable"),
        )
        assert missing is not None
        self.assertFalse(missing.usage_available)
        self.assertEqual((None, None, None, None), (
            missing.input_tokens, missing.cached_input_tokens, missing.output_tokens, missing.reasoning_tokens,
        ))
        self.assertEqual("PROVIDER_USAGE_UNAVAILABLE", missing.unavailable_reason)

    def test_late_worker_usage_appends_observation_without_changing_execution_state(self):
        prepared, _, _, attempt_id = self.dispatched("late-worker-usage")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        first = prepared.service.record_worker_usage(
            attempt_id=attempt_id,
            thread_id=thread_id,
            turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(
                thread_id, turn_id, request, usage_scope="unavailable"
            ),
        )
        assert first is not None
        with prepared.service.ledger.read() as connection:
            call_before = dict(connection.execute(
                "SELECT * FROM provider_calls WHERE attempt_id=?", (attempt_id,)
            ).fetchone())
            attempt_before = dict(connection.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone())

        same_usage = prepared.service.record_worker_usage(
            attempt_id=attempt_id,
            thread_id=thread_id,
            turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(
                thread_id, turn_id, request, usage=self.usage()
            ),
        )

        self.assertEqual(first.usage_id, same_usage.usage_id)
        with prepared.service.ledger.read() as connection:
            call_after = dict(connection.execute(
                "SELECT * FROM provider_calls WHERE attempt_id=?", (attempt_id,)
            ).fetchone())
            attempt_after = dict(connection.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone())
            observations = [json.loads(row["payload_json"]) for row in connection.execute(
                "SELECT payload_json FROM usage_observations WHERE provider_call_id=? ORDER BY rowid",
                (call_after["id"],),
            )]
        for key in (
            "execution_status", "effect_status", "result_status", "new_turn_count",
            "completed_at", "receipt_json", "raw_receipt_digest", "usage_id",
        ):
            self.assertEqual(call_before[key], call_after[key], key)
        self.assertEqual(attempt_before, attempt_after)
        self.assertEqual(("unavailable", "measured"), tuple(
            item["measurement_status"] for item in observations
        ))
        self.assertTrue(observations[-1]["late"])
        self.assertEqual(observations[0]["observation_id"], observations[-1]["previous_observation_id"])
        self.assertEqual(170, call_after["actual_tokens"])
        self.assertEqual("settled", call_after["status"])

    def test_first_empty_thread_total_is_attributed_once_from_actual_receipts(self):
        prepared, _, _, attempt_id = self.dispatched("first-empty-thread")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        total = self.usage(240, 40, 90, 15)
        recorded = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(
                thread_id, turn_id, request, usage_scope="thread",
                usage={"last": self.usage(), "total": total},
            ),
        )

        assert recorded is not None
        self.assertTrue(recorded.usage_available)
        self.assertEqual("thread", recorded.usage_scope)
        self.assertEqual("first_empty_thread", recorded.attribution_basis)
        self.assertEqual((240, 40, 90, 15), (
            recorded.input_tokens, recorded.cached_input_tokens,
            recorded.output_tokens, recorded.reasoning_tokens,
        ))

    def test_same_turn_is_idempotent_across_service_reopen_but_conflicting_actual_is_rejected(self):
        prepared, _, _, attempt_id = self.dispatched("idempotent")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        first_payload = self.payload(thread_id, turn_id, request, usage=self.usage())
        first = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed", provider_payload=first_payload,
        )
        assert first is not None
        duplicate = EngineService(prepared.service.ledger).record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed", provider_payload=first_payload,
        )
        self.assertEqual(first.usage_id, duplicate.usage_id)
        self.assertEqual(1, len(self.worker_rows(prepared.service)))

        with self.assertRaisesRegex(EngineServiceError, "WORKER_USAGE_CONFLICT"):
            prepared.service.record_worker_usage(
                attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
                terminal_status="completed",
                provider_payload=self.payload(thread_id, turn_id, request, usage=self.usage(input_tokens=121)),
            )

    def test_interrupted_attempt_keeps_explicit_turn_and_resumed_cumulative_usage_separate(self):
        prepared, runtime, dispatcher, attempt_id = self.dispatched("resumed-cumulative")
        thread_id, first_turn_id, first_request = self.turn(prepared.service, attempt_id)
        first = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=first_turn_id,
            terminal_status="interrupted",
            provider_payload=self.payload(thread_id, first_turn_id, first_request, usage=self.usage()),
        )
        assert first is not None
        runtime.interrupt(thread_id=thread_id, turn_id=first_turn_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatcher.run_once(prepared.project_id).action)
        resumed_thread_id, resumed_turn_id, resumed_request = self.turn(prepared.service, attempt_id)
        self.assertEqual(thread_id, resumed_thread_id)
        self.assertNotEqual(first_turn_id, resumed_turn_id)
        resumed = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=resumed_thread_id, turn_id=resumed_turn_id,
            terminal_status="interrupted",
            provider_payload=self.payload(
                resumed_thread_id, resumed_turn_id, resumed_request, usage_scope="thread",
                usage={"last": self.usage(), "total": self.usage(240, 40, 90, 15)},
            ),
        )
        assert resumed is not None
        self.assertTrue(first.usage_available)
        self.assertFalse(resumed.usage_available)
        self.assertEqual("CUMULATIVE_USAGE_NOT_ATTRIBUTABLE_TO_TURN", resumed.unavailable_reason)
        self.assertEqual(2, len(self.worker_rows(prepared.service)))

    def test_cancelled_spellings_are_terminal_failures_and_never_resume(self):
        for terminal in ("cancelled", "canceled"):
            with self.subTest(terminal=terminal):
                prepared, runtime, dispatcher, attempt_id = self.dispatched(
                    "terminal-" + terminal
                )
                thread_id, _, _ = self.turn(prepared.service, attempt_id)
                runtime.threads[thread_id].terminal_status = terminal
                runtime.threads[thread_id].final_response = "provider cancellation"

                outcome = dispatcher.run_once(prepared.project_id)

                self.assertEqual(RunOnceAction.OBSERVED, outcome.action)
                self.assertEqual(0, runtime.resume_calls)
                with prepared.service.ledger.read() as connection:
                    attempt = connection.execute(
                        "SELECT status FROM attempts WHERE id=?", (attempt_id,)
                    ).fetchone()
                    call = connection.execute(
                        "SELECT execution_status,effect_status,result_status,status,actual_tokens "
                        "FROM provider_calls WHERE attempt_id=?", (attempt_id,)
                    ).fetchone()
                self.assertEqual("failed", attempt["status"])
                self.assertEqual(
                    ("terminal", "none", "invalid", "usage_unknown", None),
                    tuple(call),
                )

    def test_provider_binding_mismatches_are_rejected(self):
        prepared, _, _, attempt_id = self.dispatched("mismatch")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        for name, payload in (
            ("thread", self.payload("other-thread", turn_id, request, usage=self.usage())),
            ("turn", self.payload(thread_id, "other-turn", request, usage=self.usage())),
            ("prompt", self.payload(
                thread_id, turn_id, request, usage=self.usage(), prompt_digest=sha256_digest("other prompt")
            )),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(EngineServiceError, "WORKER_USAGE_BINDING_MISMATCH"):
                prepared.service.record_worker_usage(
                    attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
                    terminal_status="completed", provider_payload=payload,
                )
        self.assertEqual([], self.worker_rows(prepared.service))

    def test_partial_usage_preserves_components_and_invalid_total_is_unavailable(self):
        cases = {
            "partial": (
                {"inputTokens": 10, "cachedInputTokens": 1},
                True, (10, 1, None, None), "PROVIDER_USAGE_PARTIAL",
            ),
            "negative": (
                self.usage(input_tokens=-1),
                True, (None, 20, 50, 10), "PROVIDER_USAGE_PARTIAL",
            ),
            "invalid-total": (
                self.usage() | {"totalTokens": 1},
                False, (None, None, None, None),
                "PROVIDER_USAGE_FIELDS_INCOMPLETE_OR_INVALID",
            ),
            "wrong-type": (
                self.usage() | {"outputTokens": "50"},
                True, (120, 20, None, 10), "PROVIDER_USAGE_PARTIAL",
            ),
        }
        for name, (raw, available, values, reason) in cases.items():
            with self.subTest(name=name):
                prepared, _, _, attempt_id = self.dispatched("malformed-" + name)
                thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
                result = prepared.service.record_worker_usage(
                    attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
                    terminal_status="failed",
                    provider_payload=self.payload(thread_id, turn_id, request, usage=raw),
                )
                assert result is not None
                self.assertEqual(available, result.usage_available)
                self.assertEqual(reason, result.unavailable_reason)
                self.assertEqual(values, (
                    result.input_tokens, result.cached_input_tokens,
                    result.output_tokens, result.reasoning_tokens,
                ))
                self.assertEqual(raw, result.provider_observation["payload"]["usage"])

    def test_usage_is_recorded_before_terminal_attempt_state_and_legacy_intent_is_not_backfilled(self):
        prepared, _, _, attempt_id = self.dispatched("before-finish")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        recorded = prepared.service.record_worker_usage(
            attempt_id=attempt_id, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(thread_id, turn_id, request, usage=self.usage()),
        )
        assert recorded is not None
        with prepared.service.ledger.read() as connection:
            self.assertEqual("running", connection.execute(
                "SELECT status FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["status"])

        legacy, _, _, legacy_attempt = self.dispatched("legacy-marker")
        thread_id, turn_id, request = self.turn(legacy.service, legacy_attempt)
        request.pop("worker_usage_contract")
        with legacy.service.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE runtime_intents SET request_json = ?, request_digest = ? "
                "WHERE attempt_id = ? AND kind = 'start_turn'",
                (canonical_json(request), sha256_digest(request), legacy_attempt),
            )
        self.assertIsNone(legacy.service.record_worker_usage(
            attempt_id=legacy_attempt, thread_id=thread_id, turn_id=turn_id,
            terminal_status="completed",
            provider_payload=self.payload(thread_id, turn_id, request, usage=self.usage()),
        ))
        self.assertEqual([], self.worker_rows(legacy.service))

    def test_wait_completion_callback_records_usage_without_completing_attempt(self):
        prepared, _, _, attempt_id = self.dispatched("wait-callback")
        closed: list[str] = []
        adapter = self.completed_adapter(prepared.service, attempt_id, close_calls=closed)

        self.assertTrue(adapter.wait_for_active_turns(timeout_seconds=0))

        rows = self.worker_rows(prepared.service)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["usage_available"])
        self.assertEqual(1, len(rows[0]["turn_ids"]))
        with prepared.service.ledger.read() as connection:
            self.assertEqual(("running", "running"), tuple(connection.execute(
                "SELECT a.status AS attempt_status, t.status AS task_status FROM attempts a "
                "JOIN task_contracts t ON t.id = a.task_id WHERE a.id = ?", (attempt_id,)
            ).fetchone()))
        self.assertEqual([], closed)

    def test_close_flushes_completed_callback_before_adapter_shutdown(self):
        prepared, _, _, attempt_id = self.dispatched("close-callback")
        closed: list[str] = []
        adapter = self.completed_adapter(prepared.service, attempt_id, close_calls=closed)

        adapter.close()

        self.assertEqual(1, len(self.worker_rows(prepared.service)))
        self.assertEqual(["closed"], closed)
        with prepared.service.ledger.read() as connection:
            self.assertEqual("running", connection.execute(
                "SELECT status FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["status"])

    def test_close_drains_interrupt_race_and_persists_completion_usage(self):
        prepared, _, _, attempt_id = self.dispatched("close-interrupt-race")
        thread_id, turn_id, request = self.turn(prepared.service, attempt_id)
        future: Future[object] = Future()
        interrupted: list[str] = []
        closed: list[str] = []

        def interrupt() -> None:
            interrupted.append(turn_id)
            future.set_result(SimpleNamespace(
                id=turn_id,
                status="completed",
                final_response="interrupt completion",
                usage=SimpleNamespace(model_dump=lambda **_kwargs: {"total": self.usage(41, 6, 9, 2)}),
                duration_ms=13,
                items=[],
            ))

        adapter = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        adapter._turn_futures = {thread_id: (SimpleNamespace(id=turn_id, interrupt=interrupt), future)}
        adapter._completion_observers = {}
        adapter._ephemeral_thread_ids = set()
        adapter._turn_usage_context = {thread_id: {"prompt_digest": request["prompt_digest"]}}
        adapter._codex = SimpleNamespace(close=lambda: closed.append("closed"))
        adapter.register_completion_observer(
            thread_id=thread_id,
            turn_id=turn_id,
            observer=lambda observation: prepared.service.record_worker_usage(
                attempt_id=attempt_id,
                thread_id=observation.thread_id,
                turn_id=turn_id,
                terminal_status=observation.terminal_status or "failed",
                provider_payload=observation.payload,
                output_digest=sha256_digest(observation.final_response or ""),
            ),
        )

        adapter.close()

        self.assertEqual([turn_id], interrupted)
        self.assertEqual(["closed"], closed)
        self.assertEqual(1, len(self.worker_rows(prepared.service)))
        with prepared.service.ledger.read() as connection:
            self.assertEqual("running", connection.execute(
                "SELECT status FROM attempts WHERE id = ?", (attempt_id,)
            ).fetchone()["status"])


if __name__ == "__main__":
    unittest.main()
