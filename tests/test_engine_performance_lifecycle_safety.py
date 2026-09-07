from __future__ import annotations

import copy
import json
import unittest

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.domain import RuntimeReceipt, ThreadBinding, utc_now
from flowmarshal.engine.performance_lifecycle_safety import audit_runtime_lifecycle


class RuntimeLifecycleSafetyTests(unittest.TestCase):
    def _runtime_snapshot(self):
        attempt_id = "attempt_" + "1" * 32
        task_id = "task_" + "1" * 32
        thread_id = "thread-1"
        turn_id = "turn-1"
        call_id = "provider-call-1"
        create_request = {"cwd": "C:/project", "task_id": task_id}
        start_request = {"thread_id": thread_id, "prompt_digest": "sha256:" + "2" * 64}
        create_intent = self._intent("intent_" + "4" * 32, attempt_id, "create_thread", create_request)
        start_intent = self._intent("intent_" + "5" * 32, attempt_id, "start_turn", start_request)
        create_response = {"thread_id": thread_id, "thread": {"id": thread_id, "turns": []}}
        start_response = {
            "thread_id": thread_id, "turn_id": turn_id,
            "prompt_digest": start_request["prompt_digest"],
        }
        create_receipt = self._receipt(
            "receipt_" + "6" * 32, create_intent["id"], thread_id, None, create_response,
        )
        start_receipt = self._receipt(
            "receipt_" + "7" * 32, start_intent["id"], turn_id, turn_id, start_response,
            thread_id=thread_id,
        )
        history = [
            self._history(1, "attempt.reserved", attempt_id, {
                "task_id": task_id, "attempt_no": 1, "kind": "execution",
            }),
            self._history(2, "runtime.intent_prepared", create_intent["id"], {
                "attempt_id": attempt_id, "kind": "create_thread",
                "request_digest": create_intent["request_digest"],
            }),
            self._history(3, "runtime.receipt_recorded", create_receipt["id"], {
                "intent_id": create_intent["id"],
                "provider_operation_id": thread_id,
            }),
            self._history(4, "runtime.intent_prepared", start_intent["id"], {
                "attempt_id": attempt_id, "kind": "start_turn",
                "request_digest": start_intent["request_digest"],
            }),
            self._history(5, "runtime.receipt_recorded", start_receipt["id"], {
                "intent_id": start_intent["id"],
                "provider_operation_id": turn_id,
            }),
        ]
        snapshot = {
            "attempts": [{
                "id": attempt_id, "task_id": task_id, "kind": "execution",
                "attempt_no": 1, "status": "succeeded", "failure_class": None,
                "execution_spec_digest": "sha256:" + "3" * 64,
            }],
            "provider_calls": [{
                "id": call_id, "call_key": "worker-call", "attempt_id": attempt_id,
                "receipt_json": canonical_json({"terminal_status": "completed"}),
            }],
            "runtime_intents": [create_intent, start_intent],
            "runtime_receipts": [create_receipt, start_receipt],
            "history_events": history,
            "recovery_assessments": [],
        }
        traces = [
            self._trace_row(
                "operation-create", "create", call_id, attempt_id,
                create_intent, thread_id, None, thread_id,
            ),
            self._trace_row(
                "operation-start", "start", call_id, attempt_id,
                start_intent, thread_id, turn_id, turn_id,
            ),
        ]
        return snapshot, traces

    @staticmethod
    def _intent(identifier, attempt_id, kind, request):
        return {
            "id": identifier, "attempt_id": attempt_id, "kind": kind,
            "idempotency_key": identifier + "-idempotency",
            "request_digest": sha256_digest(request),
            "request_json": canonical_json(request), "status": "received",
        }

    @staticmethod
    def _receipt(
        identifier, intent_id, provider_operation_id, turn_id, response,
        *, thread_id=None,
    ):
        bound_thread = provider_operation_id if thread_id is None else thread_id
        binding = ThreadBinding(
            thread_id=bound_thread, turn_id=turn_id, bound_at=utc_now(),
        )
        receipt = RuntimeReceipt(
            receipt_id=identifier, intent_id=intent_id,
            provider_operation_id=provider_operation_id,
            response_digest=sha256_digest(response), response_payload=response,
            binding=binding, received_at=utc_now(),
        )
        return {
            "id": identifier, "intent_id": intent_id,
            "provider_operation_id": provider_operation_id,
            "response_digest": receipt.response_digest,
            "payload_json": receipt.model_dump_json(),
            "binding_json": canonical_json(binding),
        }

    @staticmethod
    def _history(sequence, event_type, entity_id, payload):
        return {
            "id": f"history-{sequence}", "sequence": sequence,
            "event_type": event_type, "entity_id": entity_id,
            "payload_json": canonical_json(payload),
        }

    @staticmethod
    def _trace_row(
        operation_id, kind, call_id, attempt_id, intent, thread_id, turn_id,
        provider_operation_id,
    ):
        return {
            "operation_id": operation_id, "kind": kind, "call_id": call_id,
            "category": "logical", "parent_operation_id": None,
            "attempt_id": attempt_id, "intent_id": intent["id"],
            "thread_id": thread_id, "turn_id": turn_id,
            "request": json.loads(intent["request_json"]),
            "request_digest": intent["request_digest"],
            "response": {"operation_id": provider_operation_id},
        }

    def test_registered_create_and_start_are_fully_bound(self):
        snapshot, traces = self._runtime_snapshot()

        unapproved, failures, missing, evidence = audit_runtime_lifecycle(snapshot, traces)

        self.assertEqual(0, unapproved)
        self.assertEqual([], failures)
        self.assertEqual([], missing)
        self.assertEqual(2, evidence["runtime_receipt_count"])

    def test_registered_resume_then_second_start_is_approved(self):
        snapshot, traces = self._runtime_snapshot()
        attempt_id = snapshot["attempts"][0]["id"]
        second_call_id = "provider-call-2"
        resume_request = {
            "thread_id": "thread-1", "cwd": "C:/project",
            "model_observation": {"inventory_digest": "sha256:" + "8" * 64},
        }
        start_request = {
            "thread_id": "thread-1", "prompt_digest": "sha256:" + "9" * 64,
        }
        resume_intent = self._intent(
            "intent_" + "8" * 32, attempt_id, "resume_turn", resume_request,
        )
        start_intent = self._intent(
            "intent_" + "9" * 32, attempt_id, "start_turn", start_request,
        )
        resume_operation_id = "thread-1"
        resume_provider_id = f"resume:{resume_operation_id}:{attempt_id}"
        resume_binding = ThreadBinding(thread_id="thread-1", bound_at=utc_now())
        resume_receipt = RuntimeReceipt(
            receipt_id="receipt_" + "8" * 32, intent_id=resume_intent["id"],
            provider_operation_id=resume_provider_id,
            response_digest=sha256_digest({"thread_id": "thread-1", "resumed": True}),
            binding=resume_binding, received_at=utc_now(),
        )
        resume_row = {
            "id": resume_receipt.receipt_id, "intent_id": resume_intent["id"],
            "provider_operation_id": resume_provider_id,
            "response_digest": resume_receipt.response_digest,
            "payload_json": resume_receipt.model_dump_json(),
            "binding_json": canonical_json(resume_binding),
        }
        start_receipt = self._receipt(
            "receipt_" + "9" * 32, start_intent["id"], "turn-2", "turn-2",
            {"thread_id": "thread-1", "turn_id": "turn-2",
             "prompt_digest": start_request["prompt_digest"]},
            thread_id="thread-1",
        )
        snapshot["provider_calls"].append({
            "id": second_call_id, "call_key": "worker-call-resumed",
            "attempt_id": attempt_id,
            "receipt_json": canonical_json({"terminal_status": "completed"}),
        })
        snapshot["runtime_intents"].extend([resume_intent, start_intent])
        snapshot["runtime_receipts"].extend([resume_row, start_receipt])
        snapshot["history_events"].extend([
            self._history(6, "runtime.intent_prepared", resume_intent["id"], {
                "attempt_id": attempt_id, "kind": "resume_turn",
                "request_digest": resume_intent["request_digest"],
            }),
            self._history(7, "runtime.receipt_recorded", resume_row["id"], {
                "intent_id": resume_intent["id"],
                "provider_operation_id": resume_provider_id,
            }),
            self._history(8, "runtime.intent_prepared", start_intent["id"], {
                "attempt_id": attempt_id, "kind": "start_turn",
                "request_digest": start_intent["request_digest"],
            }),
            self._history(9, "runtime.receipt_recorded", start_receipt["id"], {
                "intent_id": start_intent["id"],
                "provider_operation_id": "turn-2",
            }),
        ])
        traces.extend([
            {
                "operation_id": "operation-resume", "kind": "resume",
                "category": "logical", "parent_operation_id": None,
                "call_id": second_call_id, "attempt_id": attempt_id,
                "intent_id": resume_intent["id"], "thread_id": "thread-1",
                "turn_id": None,
                "request": {"thread_id": "thread-1", "cwd": "C:/project"},
                "response": {"operation_id": resume_operation_id},
            },
            self._trace_row(
                "operation-second-start", "start", second_call_id, attempt_id,
                start_intent, "thread-1", "turn-2", "turn-2",
            ),
        ])

        unapproved, failures, missing, _evidence = audit_runtime_lifecycle(snapshot, traces)

        self.assertEqual((0, [], []), (unapproved, failures, missing))

    def test_unregistered_resume_and_start_are_rejected(self):
        snapshot, traces = self._runtime_snapshot()
        attempt_id = snapshot["attempts"][0]["id"]
        call_id = snapshot["provider_calls"][0]["id"]
        traces.extend([
            {
                "operation_id": "operation-resume", "kind": "resume",
                "category": "logical", "parent_operation_id": None,
                "call_id": call_id, "attempt_id": attempt_id, "intent_id": None,
                "thread_id": "thread-1", "turn_id": None,
                "request": {"thread_id": "thread-1", "cwd": "C:/project"},
                "response": {"operation_id": "thread-1"},
            },
            {
                "operation_id": "operation-extra-start", "kind": "start",
                "category": "logical", "parent_operation_id": None,
                "call_id": call_id, "attempt_id": attempt_id, "intent_id": None,
                "thread_id": "thread-1", "turn_id": "turn-2", "request": {},
                "response": {"operation_id": "turn-2"},
            },
        ])

        unapproved, failures, _missing, _evidence = audit_runtime_lifecycle(snapshot, traces)

        self.assertEqual(2, unapproved)
        self.assertEqual(
            2,
            sum(item.startswith("PERFORMANCE_RUNTIME_OPERATION_WITHOUT_RECEIVED_INTENT")
                for item in failures),
        )

    def test_runtime_attempt_cannot_hide_behind_role_call_key(self):
        snapshot, traces = self._runtime_snapshot()
        role_call_key = "planning-role-call"
        snapshot["provider_calls"].append({
            "id": "provider-role", "call_key": role_call_key, "attempt_id": None,
            "receipt_json": canonical_json({
                "role": "goal_normalizer", "call_id": role_call_key,
            }),
        })
        traces.append({
            "operation_id": "operation-hidden-resume", "kind": "resume",
            "category": "logical", "parent_operation_id": None,
            "call_id": role_call_key,
            "attempt_id": snapshot["attempts"][0]["id"], "intent_id": None,
            "thread_id": "thread-1", "turn_id": None,
            "request": {"thread_id": "thread-1", "cwd": "C:/project"},
            "response": {"operation_id": "thread-1"},
        })

        unapproved, failures, _missing, _evidence = audit_runtime_lifecycle(snapshot, traces)

        self.assertEqual(1, unapproved)
        self.assertTrue(any(
            item.startswith("PERFORMANCE_RUNTIME_OPERATION_WITHOUT_RECEIVED_INTENT")
            for item in failures
        ))

    def test_receipt_column_and_history_tampering_are_rejected(self):
        snapshot, traces = self._runtime_snapshot()
        altered = copy.deepcopy(snapshot)
        altered["runtime_receipts"][0]["binding_json"] = canonical_json({
            "thread_id": "different-thread", "bound_at": utc_now(),
        })
        prepared = next(
            row for row in altered["history_events"]
            if row["event_type"] == "runtime.intent_prepared"
        )
        payload = json.loads(prepared["payload_json"])
        payload["request_digest"] = "sha256:" + "f" * 64
        prepared["payload_json"] = canonical_json(payload)

        _unapproved, failures, missing, _evidence = audit_runtime_lifecycle(altered, traces)

        self.assertIn("PERFORMANCE_RUNTIME_RECEIPT_COLUMN_BINDING_MISMATCH", failures)
        self.assertIn("PERFORMANCE_RUNTIME_INTENT_HISTORY_BINDING_MISMATCH", failures)
        self.assertIn("PERFORMANCE_RUNTIME_RECEIPT_MISSING", missing)

    def test_failed_attempt_retry_is_bound_by_task_retry_event(self):
        prior_id = "attempt_" + "a" * 32
        current_id = "attempt_" + "b" * 32
        task_id = "task_" + "c" * 32
        snapshot = self._retry_snapshot(
            prior_id, current_id, task_id,
            prior_status="failed", prior_failure="implementation",
            retry_payload={
                "previous_attempt_id": prior_id, "failure_class": "implementation",
                "new_evidence_ids": [], "recovery_assessment_id": None,
                "failed_validation_result_id": None,
            },
        )

        unapproved, failures, missing, _evidence = audit_runtime_lifecycle(snapshot, [])

        self.assertEqual((0, [], []), (unapproved, failures, missing))

    def test_validation_failure_retry_binds_assessment_to_prior_worker(self):
        prior_id = "attempt_" + "d" * 32
        current_id = "attempt_" + "e" * 32
        task_id = "task_" + "f" * 32
        assessment_id = "recovery_assessment_" + "1" * 32
        retry_payload = {
            "previous_attempt_id": prior_id, "failure_class": "implementation",
            "new_evidence_ids": ["evidence-1"],
            "recovery_assessment_id": assessment_id,
            "failed_validation_result_id": "validation-result-1",
        }
        snapshot = self._retry_snapshot(
            prior_id, current_id, task_id,
            prior_status="succeeded", prior_failure=None,
            retry_payload=retry_payload, retry_sequence=4, current_sequence=5,
        )
        assessment_body = {
            "assessment_id": assessment_id, "attempt_id": prior_id,
            "failure_class": "implementation", "action": "task_repair",
            "rationale": "validation failure", "new_evidence_ids": ["evidence-1"],
            "same_failure_replan_count": 0, "goal_replan_count": 0,
        }
        snapshot["recovery_assessments"] = [{
            "id": assessment_id, "attempt_id": prior_id,
            "failure_class": "implementation", "action": "task_repair",
            "payload_json": canonical_json(assessment_body),
        }]
        snapshot["history_events"].insert(1, self._history(
            3, "recovery.assessed", assessment_id,
            {"attempt_id": prior_id, "action": "task_repair"},
        ))

        unapproved, failures, missing, _evidence = audit_runtime_lifecycle(snapshot, [])

        self.assertEqual((0, [], []), (unapproved, failures, missing))

    def test_distinct_semantic_validation_steps_are_not_counted_as_retry(self):
        snapshot, _traces = self._runtime_snapshot()
        first = "attempt_" + "a" * 32
        second = "attempt_" + "b" * 32
        task_id = "task_" + "c" * 32
        snapshot["attempts"] = [
            {
                "id": first, "task_id": task_id, "kind": "validation",
                "attempt_no": 1, "status": "succeeded", "failure_class": None,
                "execution_spec_digest": "sha256:" + "1" * 64,
            },
            {
                "id": second, "task_id": task_id, "kind": "validation",
                "attempt_no": 2, "status": "succeeded", "failure_class": None,
                "execution_spec_digest": "sha256:" + "1" * 64,
            },
        ]
        snapshot["provider_calls"] = []
        snapshot["runtime_receipts"] = []
        first_request = {"validation_id": "semantic-a"}
        second_request = {"validation_id": "semantic-b"}
        snapshot["runtime_intents"] = [
            self._intent("intent_" + "a" * 32, first, "create_thread", first_request),
            self._intent("intent_" + "b" * 32, second, "create_thread", second_request),
        ]
        for intent in snapshot["runtime_intents"]:
            intent["status"] = "abandoned"
        snapshot["history_events"] = [
            self._history(1, "attempt.reserved", first, {
                "task_id": task_id, "attempt_no": 1, "kind": "validation",
            }),
            self._history(2, "runtime.intent_prepared", snapshot["runtime_intents"][0]["id"], {
                "attempt_id": first, "kind": "create_thread",
                "request_digest": snapshot["runtime_intents"][0]["request_digest"],
            }),
            self._history(3, "attempt.reserved", second, {
                "task_id": task_id, "attempt_no": 2, "kind": "validation",
            }),
            self._history(4, "runtime.intent_prepared", snapshot["runtime_intents"][1]["id"], {
                "attempt_id": second, "kind": "create_thread",
                "request_digest": snapshot["runtime_intents"][1]["request_digest"],
            }),
        ]

        unapproved, failures, missing, _evidence = audit_runtime_lifecycle(snapshot, [])

        self.assertEqual(0, unapproved)
        self.assertEqual([], failures)
        self.assertEqual([], missing)

    def _retry_snapshot(
        self, prior_id, current_id, task_id, *, prior_status, prior_failure,
        retry_payload, retry_sequence=2, current_sequence=3,
    ):
        return {
            "attempts": [
                {
                    "id": prior_id, "task_id": task_id, "kind": "execution",
                    "attempt_no": 1, "status": prior_status,
                    "failure_class": prior_failure,
                    "execution_spec_digest": "sha256:" + "4" * 64,
                },
                {
                    "id": current_id, "task_id": task_id, "kind": "execution",
                    "attempt_no": 2, "status": "succeeded", "failure_class": None,
                    "execution_spec_digest": "sha256:" + "4" * 64,
                },
            ],
            "provider_calls": [], "runtime_intents": [], "runtime_receipts": [],
            "recovery_assessments": [],
            "history_events": [
                self._history(1, "attempt.reserved", prior_id, {
                    "task_id": task_id, "attempt_no": 1, "kind": "execution",
                }),
                self._history(retry_sequence, "task.retry_enabled", task_id, retry_payload),
                self._history(current_sequence, "attempt.reserved", current_id, {
                    "task_id": task_id, "attempt_no": 2, "kind": "execution",
                }),
            ],
        }


if __name__ == "__main__":
    unittest.main()
