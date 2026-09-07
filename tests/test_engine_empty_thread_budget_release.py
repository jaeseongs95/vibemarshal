from __future__ import annotations

import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetManager
from flowmarshal.engine.domain import RunOnceAction, RuntimeIntentKind
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.evaluation_budget import load_evaluation_policies
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeObservation
from flowmarshal.engine.service import EngineServiceError


ROOT = Path(__file__).resolve().parents[1]


def _inventory() -> ModelInventory:
    roles = default_role_configuration(ROOT)
    grouped: dict[str, set[str]] = defaultdict(set)
    for role_name in type(roles).model_fields:
        binding = roles.binding_for(role_name)
        grouped[binding.model].add(binding.effort)
    return ModelInventory(
        executable_digest="sha256:" + "0" * 64,
        runtime_capabilities=RUNTIME_CAPABILITIES,
        source="empty-thread-budget-release-test",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
            for model, efforts in sorted(grouped.items())
        ),
    )


class _ReceiptCapturingRuntime(FakeCodexRuntime):
    created_receipt = None

    def __init__(self, inventory, *, thread_updates=None):
        super().__init__(inventory)
        self.thread_updates = thread_updates or {}

    def create_thread(self, **arguments):
        receipt = super().create_thread(**arguments)
        cwd = Path(arguments["cwd"]).resolve()
        thread = receipt.payload["thread"] | {
            "projectId": "test-codex-project",
            "ephemeral": False,
            "cwd": str(cwd),
            "path": str(cwd / "unstarted-rollout.jsonl"),
        } | self.thread_updates
        self.created_receipt = receipt.model_copy(
            update={"payload": {"thread": thread}}
        )
        return self.created_receipt


class EmptyThreadBudgetReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = default_role_configuration(ROOT)
        self.inventory = _inventory()
        self.policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )

    def _unknown_create(self, root: Path, *, thread_updates=None):
        workspace, _ = _copy_fixture(ROOT, root)
        prepared = _prepare(
            workspace=workspace,
            state_root=root / "state",
            inventory=self.inventory,
            roles=self.roles,
            evaluation_policies=self.policies,
            evaluation_contract_digest=sha256_digest({"contract": "empty-thread"}),
            fixture_digest=sha256_digest({"fixture": "empty-thread"}),
        )
        runtime = _ReceiptCapturingRuntime(
            self.inventory, thread_updates=thread_updates
        )
        EngineDispatcher(prepared.service, runtime).run_once(
            prepared.project_id, proposal=prepared.proposal
        )

        def fault(point: str) -> None:
            if point == "after_thread_effect":
                raise RuntimeError("receipt lost after create_thread")

        with self.assertRaisesRegex(RuntimeError, "receipt lost"):
            EngineDispatcher(
                prepared.service, runtime, fault_hook=fault
            ).run_once(prepared.project_id)
        unknown = prepared.service.recover_inspect(prepared.project_id)
        self.assertEqual(1, len(unknown))
        self.assertIsNotNone(runtime.created_receipt)
        created = runtime.created_receipt
        self.assertIsNotNone(created.binding)
        receipt = prepared.service.record_runtime_receipt(
            intent_id=unknown[0],
            provider_operation_id=created.operation_id,
            response=created.payload,
            binding=created.binding,
            allow_reconcile_unknown=True,
        )
        observation = runtime.read_stored(thread_id=created.binding.thread_id)
        observation = observation.model_copy(
            update={
                "payload": observation.payload | {
                    "turn_history_available": True,
                    "turn_history_source": "thread/read(includeTurns=true)",
                }
            }
        )
        with prepared.service.ledger.read() as connection:
            call = connection.execute(
                "SELECT id FROM provider_calls WHERE attempt_id = "
                "(SELECT attempt_id FROM runtime_intents WHERE id = ?)",
                (unknown[0],),
            ).fetchone()
            attempt = connection.execute(
                "SELECT attempt_id FROM runtime_intents WHERE id = ?", (unknown[0],)
            ).fetchone()
        return prepared, runtime, receipt, observation, call["id"], attempt["attempt_id"]

    @staticmethod
    def _empty_turns_list_observation(receipt, *, updates=None) -> RuntimeObservation:
        thread = receipt.response_payload["thread"]
        payload = {
            "thread_id": receipt.binding.thread_id,
            "turn_count": 0,
            "turn_status": None,
            "usage": None,
            "usage_source": "thread/turns/list",
            "turn_history_available": True,
            "turn_history_source": "thread/turns/list",
            "turn_history_error": None,
            "turn_history_params": {
                "threadId": receipt.binding.thread_id,
                "limit": 1,
                "sortDirection": "asc",
                "itemsView": "full",
            },
            "turn_history_response": {
                "data": [],
                "nextCursor": None,
                "backwardsCursor": None,
            },
            "read_retry_errors": [],
            "turn_history_connection": "independent_app_server",
            "independent_reader_executable_digest": "sha256:" + "0" * 64,
            "materialization_read": {
                "method": "thread/read",
                "params": {
                    "threadId": receipt.binding.thread_id,
                    "includeTurns": True,
                },
                "response": None,
                "error": {
                    "type": "MethodNotFoundError",
                    "code": -32601,
                    "message": "list_turns is not supported yet",
                },
            },
            "project_binding_verification": {
                "source": "same_connection_thread_start_receipt_and_empty_turns_list",
                "creation_receipt_digest": sha256_digest(receipt.response_payload),
                "creation_project_id": thread["projectId"],
                "rollout_path": thread["path"],
                "raw_metadata_source": "thread/read(includeTurns=false)",
                "raw_metadata_connection": "independent_app_server",
                "raw_metadata_confirmation_count": 2,
            },
        }
        if updates:
            payload |= updates
        return RuntimeObservation(
            thread_id=receipt.binding.thread_id,
            active=False,
            payload=payload,
        )

    def test_exact_empty_thread_proof_releases_reservation_and_closes_attempt_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, runtime, receipt, observation, call_id, attempt_id = (
                self._unknown_create(Path(raw) / "cell")
            )
            manager = BudgetManager(prepared.service)
            manager.release_empty_created_thread(
                call_id, receipt=receipt, observation=observation
            )
            manager.release_empty_created_thread(
                call_id, receipt=receipt, observation=observation
            )

            with prepared.service.ledger.read() as connection:
                call = connection.execute(
                    "SELECT status, actual_tokens, receipt_json, usage_id "
                    "FROM provider_calls WHERE id = ?", (call_id,)
                ).fetchone()
                attempt = connection.execute(
                    "SELECT status, failure_class FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                task_status = connection.execute(
                    "SELECT status FROM task_contracts WHERE id = ?", (prepared.task_id,)
                ).fetchone()[0]
                history_count = connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE "
                    "event_type = 'budget.empty_thread_reservation_released' "
                    "AND entity_id = ?", (call_id,)
                ).fetchone()[0]
                usage_count = connection.execute(
                    "SELECT COUNT(*) FROM budget_usage WHERE project_id = ?",
                    (prepared.project_id,),
                ).fetchone()[0]
                release_sequence = connection.execute(
                    "SELECT sequence FROM history_events WHERE "
                    "event_type = 'budget.empty_thread_reservation_released' "
                    "AND entity_id = ?", (call_id,)
                ).fetchone()[0]
                attempt_sequence = connection.execute(
                    "SELECT sequence FROM history_events WHERE event_type = 'attempt.failed' "
                    "AND entity_id = ?", (attempt_id,)
                ).fetchone()[0]
            self.assertEqual("released", call["status"])
            self.assertIsNone(call["actual_tokens"])
            self.assertIsNone(call["receipt_json"])
            self.assertIsNone(call["usage_id"])
            self.assertEqual("failed", attempt["status"])
            self.assertEqual("external_unknown", attempt["failure_class"])
            self.assertEqual("failed", task_status)
            self.assertEqual(1, history_count)
            self.assertEqual(release_sequence + 1, attempt_sequence)
            self.assertEqual(0, usage_count)
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(0, runtime.turn_calls)
            self.assertEqual(0, runtime.resume_calls)

            blocked = EngineDispatcher(prepared.service, runtime).run_once(
                prepared.project_id
            )
            self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
            with prepared.service.ledger.read() as connection:
                provider_call_count = connection.execute(
                    "SELECT COUNT(*) FROM provider_calls WHERE project_id = ?",
                    (prepared.project_id,),
                ).fetchone()[0]
            self.assertEqual(1, provider_call_count)
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(0, runtime.turn_calls)
            self.assertEqual(0, runtime.resume_calls)
            manager.release_empty_created_thread(
                call_id, receipt=receipt, observation=observation
            )
            self.assertTrue(prepared.service.ledger.verify_history(prepared.project_id))

    def test_start_turn_intent_blocks_empty_thread_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, _, receipt, observation, call_id, attempt_id = (
                self._unknown_create(Path(raw) / "cell")
            )
            prepared.service.prepare_runtime_intent(
                attempt_id=attempt_id,
                kind=RuntimeIntentKind.START_TURN,
                idempotency_key="empty-thread-test:start-turn",
                request={"thread_id": receipt.binding.thread_id, "prompt_digest": "unknown"},
            )
            unknown = prepared.service.recover_inspect(prepared.project_id)
            self.assertEqual(1, len(unknown))

            with self.assertRaisesRegex(
                EngineServiceError, "EMPTY_THREAD_RUNTIME_EFFECT_NOT_EMPTY"
            ):
                BudgetManager(prepared.service).release_empty_created_thread(
                    call_id, receipt=receipt, observation=observation
                )
            with prepared.service.ledger.read() as connection:
                status = connection.execute(
                    "SELECT status FROM provider_calls WHERE id = ?", (call_id,)
                ).fetchone()[0]
            self.assertEqual("reserved", status)

    def test_history_evidence_must_be_explicit_and_turns_list_must_be_complete(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, _, receipt, observation, call_id, _ = self._unknown_create(
                Path(raw) / "cell"
            )
            manager = BudgetManager(prepared.service)
            for label, candidate in (
                (
                    "missing",
                    observation.model_copy(
                        update={
                            "payload": {
                                key: value
                                for key, value in observation.payload.items()
                                if key != "turn_history_available"
                            }
                        }
                    ),
                ),
                (
                    "incomplete-turns-list",
                    self._empty_turns_list_observation(
                        receipt, updates={"project_binding_verification": {}}
                    ),
                ),
                (
                    "malformed-data",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={
                            "turn_history_response": {
                                "data": [{"id": "unexpected"}],
                                "nextCursor": None,
                                "backwardsCursor": None,
                            }
                        },
                    ),
                ),
                (
                    "cursor",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={
                            "turn_history_response": {
                                "data": [],
                                "nextCursor": "unexpected",
                                "backwardsCursor": None,
                            }
                        },
                    ),
                ),
                (
                    "params",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={
                            "turn_history_params": {
                                "threadId": receipt.binding.thread_id,
                                "limit": 2,
                                "sortDirection": "asc",
                                "itemsView": "full",
                            }
                        },
                    ),
                ),
                (
                    "project",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={
                            "project_binding_verification": {
                                **self._empty_turns_list_observation(
                                    receipt
                                ).payload["project_binding_verification"],
                                "creation_project_id": "foreign-project",
                            }
                        },
                    ),
                ),
                (
                    "source",
                    self._empty_turns_list_observation(
                        receipt, updates={"turn_history_source": "thread/read"}
                    ),
                ),
                (
                    "false",
                    self._empty_turns_list_observation(
                        receipt, updates={"turn_history_available": False}
                    ),
                ),
                (
                    "connection",
                    self._empty_turns_list_observation(
                        receipt, updates={"turn_history_connection": "owner"}
                    ),
                ),
                (
                    "reader-digest",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={"independent_reader_executable_digest": "sha256:" + "1" * 64},
                    ),
                ),
                (
                    "materialization",
                    self._empty_turns_list_observation(
                        receipt,
                        updates={
                            "materialization_read": {
                                "method": "thread/read",
                                "params": {"threadId": receipt.binding.thread_id, "includeTurns": True},
                                "response": None,
                                "error": None,
                            }
                        },
                    ),
                ),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(
                    EngineServiceError, "EMPTY_THREAD_(HISTORY|TURNS_LIST)_EVIDENCE_MISMATCH"
                ):
                    manager.release_empty_created_thread(
                        call_id, receipt=receipt, observation=candidate
                    )
            with prepared.service.ledger.read() as connection:
                self.assertEqual(
                    "reserved",
                    connection.execute(
                        "SELECT status FROM provider_calls WHERE id=?", (call_id,)
                    ).fetchone()[0],
                )
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM history_events WHERE "
                        "event_type='budget.empty_thread_reservation_released' AND entity_id=?",
                        (call_id,),
                    ).fetchone()[0],
                )

    def test_turns_list_binds_original_create_receipt_path_cwd_and_project(self) -> None:
        for label, updates in (
            ("cwd", {"cwd": "relative-cwd"}),
            ("path", {"path": "relative-rollout.jsonl"}),
            ("project", {"projectId": ""}),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw:
                prepared, _, receipt, _, call_id, _ = self._unknown_create(
                    Path(raw) / "cell", thread_updates=updates
                )
                with self.assertRaisesRegex(
                    EngineServiceError, "EMPTY_THREAD_TURNS_LIST_EVIDENCE_MISMATCH"
                ):
                    BudgetManager(prepared.service).release_empty_created_thread(
                        call_id,
                        receipt=receipt,
                        observation=self._empty_turns_list_observation(receipt),
                    )
                with prepared.service.ledger.read() as connection:
                    self.assertEqual(
                        "reserved",
                        connection.execute(
                            "SELECT status FROM provider_calls WHERE id=?", (call_id,)
                        ).fetchone()[0],
                    )

    def test_complete_empty_turns_list_evidence_releases_without_usage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, _, receipt, _, call_id, _ = self._unknown_create(Path(raw) / "cell")
            observation = self._empty_turns_list_observation(
                receipt,
                updates={
                    "materialization_read": {
                        "method": "thread/read",
                        "params": {
                            "threadId": receipt.binding.thread_id,
                            "includeTurns": True,
                        },
                        "response": {
                            "thread": {
                                "id": receipt.binding.thread_id,
                                "turns": [],
                            }
                        },
                        "error": None,
                    }
                },
            )
            BudgetManager(prepared.service).release_empty_created_thread(
                call_id, receipt=receipt, observation=observation
            )
            with prepared.service.ledger.read() as connection:
                proof = connection.execute(
                    "SELECT payload_json FROM history_events WHERE event_type="
                    "'budget.empty_thread_reservation_released' AND entity_id=?",
                    (call_id,),
                ).fetchone()
                call = connection.execute(
                    "SELECT status,actual_tokens,usage_id FROM provider_calls WHERE id=?",
                    (call_id,),
                ).fetchone()
            self.assertEqual(
                "same_connection_thread_turns_list_empty",
                json.loads(proof["payload_json"])["turn_zero_evidence_type"],
            )
            self.assertEqual("released", call["status"])
            self.assertIsNone(call["actual_tokens"])
            self.assertIsNone(call["usage_id"])

    def test_nonempty_or_changed_observation_cannot_release_or_rewrite_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, _, receipt, observation, call_id, _ = self._unknown_create(
                Path(raw) / "cell"
            )
            mismatched_receipt = receipt.model_copy(
                update={"provider_operation_id": "thread_other"}
            )
            manager = BudgetManager(prepared.service)
            with self.assertRaisesRegex(
                EngineServiceError, "EMPTY_THREAD_CREATE_RECEIPT_MISMATCH"
            ):
                manager.release_empty_created_thread(
                    call_id, receipt=mismatched_receipt, observation=observation
                )
            implicit_empty = observation.model_copy(
                update={
                    "payload": {
                        key: value
                        for key, value in observation.payload.items()
                        if key != "turn_count"
                    }
                }
            )
            with self.assertRaisesRegex(
                EngineServiceError, "EMPTY_THREAD_TERMINAL_OBSERVATION_MISMATCH"
            ):
                manager.release_empty_created_thread(
                    call_id, receipt=receipt, observation=implicit_empty
                )
            nonempty = RuntimeObservation(
                thread_id=observation.thread_id,
                turn_id="turn_unexpected",
                active=False,
                terminal_status="completed",
                final_response="unexpected",
                payload={
                    "thread_id": observation.thread_id,
                    "turn_id": "turn_unexpected",
                },
            )
            with self.assertRaisesRegex(
                EngineServiceError, "EMPTY_THREAD_TERMINAL_OBSERVATION_MISMATCH"
            ):
                manager.release_empty_created_thread(
                    call_id, receipt=receipt, observation=nonempty
                )
            manager.release_empty_created_thread(
                call_id, receipt=receipt, observation=observation
            )
            changed = observation.model_copy(
                update={"payload": observation.payload | {"proof_revision": 2}}
            )
            with self.assertRaisesRegex(
                EngineServiceError, "EMPTY_THREAD_RELEASE_PROOF_MISMATCH"
            ):
                manager.release_empty_created_thread(
                    call_id, receipt=receipt, observation=changed
                )


if __name__ == "__main__":
    unittest.main()
