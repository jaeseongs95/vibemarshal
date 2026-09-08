from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import BudgetStage, new_id, utc_now
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import goal, profile


def _digest(value: str) -> str:
    return sha256_digest(value)


class RoleUsageReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.ledger = SQLiteEngineLedger(Path(self.temp.name) / "engine.sqlite3")
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="재관측", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.manager = BudgetManager(self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _reserve_timeout(self, key: str, *, status: str = "timed_out") -> tuple[str, RoleCallReceipt]:
        call_id = self.manager.reserve(
            project_id=self.project_id, goal_id=self.goal.goal_id, goal_digest=self.goal.definition_digest,
            call_key=key, role="goal_reviewer", stage=BudgetStage.GOAL_REVIEW, request={"call_key": key},
        )
        receipt = RoleCallReceipt(
            call_id=key, role="goal_reviewer", status=status, model="test-model", effort="medium",
            inventory_digest=_digest("inventory"), permission_profile=":danger-full-access",
            approval_policy="never", thread_id="thread:" + key, turn_ids=("turn:" + key,),
            input_digest=_digest("input:" + key), output_digest=_digest("output:" + key),
            output_schema_digest=_digest("schema"), input_tokens=0, cached_input_tokens=0,
            output_tokens=0, reasoning_tokens=0, usage_available=False, latency_ms=0, recorded_at=utc_now(),
        )
        self.manager.settle(call_id, receipt)
        return call_id, receipt

    @staticmethod
    def _terminal(receipt: RoleCallReceipt, *, payload: dict | None = None) -> RuntimeObservation:
        return RuntimeObservation(
            thread_id=receipt.thread_id or "", turn_id=receipt.turn_ids[-1], active=False,
            terminal_status="completed", final_response="완료",
            payload=payload if payload is not None else {
                "usage_scope": "turn",
                "usage": {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0,
                          "reasoningOutputTokens": 0, "totalTokens": 0},
            },
        )

    def _call_row(self, call_id: str):
        with self.ledger.read() as connection:
            return connection.execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()

    def test_reconciliation_preserves_timeout_usage_and_counts_effective_zero_once(self) -> None:
        call_id, receipt = self._reserve_timeout("timeout-zero")
        before = self._call_row(call_id)
        assert before is not None
        original_usage_id, original_receipt = before["usage_id"], before["receipt_json"]

        effective = self.manager.observe_role_terminal(call_id, self._terminal(receipt))

        after = self._call_row(call_id)
        assert after is not None
        self.assertEqual(("settled", 0, original_receipt), (after["status"], after["actual_tokens"], after["receipt_json"]))
        self.assertNotEqual(original_usage_id, effective.usage_id)
        with self.ledger.read() as connection:
            usages = connection.execute("SELECT id,payload_json FROM budget_usage WHERE project_id=? ORDER BY rowid", (self.project_id,)).fetchall()
            reconciliation = connection.execute("SELECT * FROM usage_reconciliations WHERE call_id=?", (call_id,)).fetchone()
        assert reconciliation is not None
        self.assertEqual((original_usage_id, effective.usage_id),
                         (reconciliation["prior_usage_id"], reconciliation["effective_usage_id"]))
        self.assertEqual(original_receipt, reconciliation["original_receipt_json"])
        self.assertEqual(2, len(usages))
        self.assertEqual(original_usage_id, usages[0]["id"])
        self.assertFalse(EngineApplication(self.service).usage_summary(
            self.project_id, goal_id=self.goal.goal_id
        ).usage_records[0].usage_id == original_usage_id)

        summary = EngineApplication(self.service).usage_summary(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual((original_usage_id,), summary.superseded_usage_ids)
        self.assertEqual(1, summary.logical_call_count)
        self.assertEqual((0, 0), (summary.input_tokens.known_subtotal, summary.input_tokens.total))
        self.assertEqual((0, 0), (summary.output_tokens.known_subtotal, summary.output_tokens.total))
        self.assertEqual(1, len(summary.reconciliations))
        self.assertEqual((effective.usage_id,), tuple(item.usage_id for item in summary.usage_records))
        self.assertIsNone(summary.latency_ms.total)

    def test_same_observation_is_idempotent_and_different_observation_conflicts(self) -> None:
        call_id, receipt = self._reserve_timeout("reconcile-idempotent")
        observation = self._terminal(receipt)
        first = self.manager.observe_role_terminal(call_id, observation)
        second = self.manager.observe_role_terminal(call_id, observation)
        self.assertEqual(first.usage_id, second.usage_id)
        with self.ledger.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM usage_reconciliations WHERE call_id=?", (call_id,)).fetchone()[0]
            usages = connection.execute("SELECT COUNT(*) FROM budget_usage WHERE project_id=?", (self.project_id,)).fetchone()[0]
        self.assertEqual(1, count)
        self.assertEqual(2, usages)
        changed = self._terminal(receipt, payload={
            "usage_scope": "turn", "usage": {"inputTokens": 1, "cachedInputTokens": 0,
            "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 1},
        })
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECONCILIATION_CONFLICT"):
            self.manager.observe_role_terminal(call_id, changed)

    def test_wrong_scope_stays_unavailable(self) -> None:
        call_id, receipt = self._reserve_timeout("thread-scope")
        effective = self.manager.observe_role_terminal(call_id, self._terminal(receipt, payload={
            "usage_scope": "thread", "usage": {"total": {"inputTokens": 9, "cachedInputTokens": 0,
            "outputTokens": 2, "reasoningOutputTokens": 0, "totalTokens": 11}},
        }))
        row = self._call_row(call_id)
        assert row is not None
        self.assertEqual(("usage_unknown", None), (row["status"], row["actual_tokens"]))
        self.assertFalse(effective.usage_available)
        self.assertEqual((None, None, None, None), (effective.input_tokens, effective.cached_input_tokens,
                                                     effective.output_tokens, effective.reasoning_tokens))
        summary = EngineApplication(self.service).usage_summary(self.project_id, goal_id=self.goal.goal_id)
        self.assertIsNone(summary.input_tokens.total)
        self.assertTrue(any(item.code == "USAGE_UNAVAILABLE" for item in summary.incomplete_reasons))

    def test_thread_scope_requires_bound_empty_thread_first_turn_receipts(self) -> None:
        call_id, receipt = self._reserve_timeout("thread-first-turn")
        proofs = {
            "thread_creation_receipt": {
                "operation_id": receipt.thread_id,
                "payload": {"thread": {"id": receipt.thread_id, "turns": []}},
                "binding": {"thread_id": receipt.thread_id, "turn_id": None},
            },
            "turn_start_receipt": {
                "operation_id": receipt.turn_ids[0],
                "payload": {"thread_id": receipt.thread_id, "turn_id": receipt.turn_ids[0],
                            "first_empty_thread": True},
                "binding": {"thread_id": receipt.thread_id, "turn_id": receipt.turn_ids[0]},
            },
        }
        observation = self._terminal(receipt, payload={
            "usage_scope": "thread", "usage": {"total": {
                "inputTokens": 9, "cachedInputTokens": 2, "outputTokens": 3,
                "reasoningOutputTokens": 1, "totalTokens": 12,
            }},
            "role_call_proofs": proofs, "role_call_proofs_digest": sha256_digest(proofs),
        })
        effective = self.manager.observe_role_terminal(call_id, observation)
        assert effective is not None
        self.assertEqual((True, "thread", "first_empty_thread", 12), (
            effective.usage_available, effective.usage_scope,
            effective.attribution_basis, self._call_row(call_id)["actual_tokens"],
        ))

        other_call, other_receipt = self._reserve_timeout("thread-proof-tampered")
        tampered = {**proofs, "turn_start_receipt": {
            **proofs["turn_start_receipt"], "operation_id": "other-turn",
        }}
        unavailable = self.manager.observe_role_terminal(other_call, self._terminal(
            other_receipt, payload={
                "usage_scope": "thread", "usage": {"total": {
                    "inputTokens": 9, "cachedInputTokens": 2, "outputTokens": 3,
                    "reasoningOutputTokens": 1, "totalTokens": 12,
                }}, "role_call_proofs": tampered,
                "role_call_proofs_digest": sha256_digest(tampered),
            },
        ))
        assert unavailable is not None
        self.assertFalse(unavailable.usage_available)
        self.assertEqual(("usage_unknown", None), (
            self._call_row(other_call)["status"], self._call_row(other_call)["actual_tokens"],
        ))

    def test_turn_scope_does_not_settle_multi_turn_or_schema_recovery_call(self) -> None:
        for key, turn_ids, recovery_attempts in (
            ("multi-turn", ("turn:first", "turn:last"), 0),
            ("schema-recovery", ("turn:recovery",), 1),
        ):
            with self.subTest(key=key):
                call_id = self.manager.reserve(
                    project_id=self.project_id, goal_id=new_id("goal"), goal_digest=None,
                    call_key=key, role="goal_reviewer", stage=BudgetStage.GOAL_REVIEW,
                    request={"call_key": key},
                )
                receipt = RoleCallReceipt(
                    call_id=key, role="goal_reviewer", status="timed_out",
                    model="test-model", effort="medium", inventory_digest=_digest("inventory"),
                    permission_profile=":danger-full-access", approval_policy="never",
                    thread_id="thread:" + key, turn_ids=turn_ids,
                    input_digest=_digest("input:" + key), output_digest=_digest("output:" + key),
                    output_schema_digest=_digest("schema"), input_tokens=None,
                    cached_input_tokens=None, output_tokens=None, reasoning_tokens=None,
                    usage_available=False, latency_ms=0,
                    schema_recovery_attempts=recovery_attempts, recorded_at=utc_now(),
                )
                self.manager.settle(call_id, receipt)
                observation = RuntimeObservation(
                    thread_id=receipt.thread_id or "", turn_id=receipt.turn_ids[-1], active=False,
                    terminal_status="completed", final_response="완료",
                    payload={"usage_scope": "turn", "usage": {
                        "inputTokens": 5, "cachedInputTokens": 1, "outputTokens": 2,
                        "reasoningOutputTokens": 1, "totalTokens": 7,
                    }},
                )
                self.assertIsNone(self.manager.observe_role_terminal(call_id, observation))
                row = self._call_row(call_id)
                self.assertEqual(("usage_unknown", None), (row["status"], row["actual_tokens"]))
                with self.ledger.read() as connection:
                    event = connection.execute(
                        "SELECT payload_json FROM history_events WHERE entity_id=? "
                        "AND event_type='budget.call_observed' ORDER BY sequence DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                assert event is not None
                self.assertIn('"usage_available":false', event["payload_json"])

    def test_unavailable_chain_can_later_settle_once_but_known_measurement_cannot_change(self) -> None:
        call_id, receipt = self._reserve_timeout("unavailable-then-known")
        original_usage_id = self._call_row(call_id)["usage_id"]
        unavailable_observation = self._terminal(receipt, payload={"usage_scope": "thread", "usage": {}})
        first_unavailable = self.manager.observe_role_terminal(call_id, unavailable_observation)
        self.assertFalse(first_unavailable.usage_available)
        self.assertEqual(first_unavailable.usage_id,
                         self.manager.observe_role_terminal(call_id, unavailable_observation).usage_id)

        known_observation = self._terminal(receipt)
        known = self.manager.observe_role_terminal(call_id, known_observation)
        self.assertTrue(known.usage_available)
        self.assertEqual(known.usage_id, self.manager.observe_role_terminal(call_id, known_observation).usage_id)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECONCILIATION_CONFLICT"):
            self.manager.observe_role_terminal(call_id, self._terminal(receipt, payload={
                "usage_scope": "turn", "usage": {"inputTokens": 1, "cachedInputTokens": 0,
                "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 1},
            }))

        summary = EngineApplication(self.service).usage_summary(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual(1, summary.logical_call_count)
        self.assertEqual((0, 0), (summary.input_tokens.known_subtotal, summary.input_tokens.total))
        self.assertEqual({original_usage_id, first_unavailable.usage_id}, set(summary.superseded_usage_ids))
        self.assertEqual(2, len(summary.superseded_usage_ids))

    def test_later_actual_usage_is_observed_without_estimated_adjustment(self) -> None:
        call_id, receipt = self._reserve_timeout("adjusted-then-observed", status="failed")
        self.assertEqual("usage_unknown", self._call_row(call_id)["status"])
        with self.assertRaisesRegex(BudgetBlocked, "USAGE_ESTIMATION_PROHIBITED"):
            self.manager.adjust_unknown(call_id=call_id, charge_tokens=77, reason="초기 관측에 usage가 없음")
        effective = self.manager.observe_role_terminal(call_id, self._terminal(receipt))

        status = self.manager.status(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual(0, effective.input_tokens)
        self.assertEqual((0, 0), (status.measured_token_subtotal, status.explicit_adjustment_tokens))
        with self.ledger.read() as connection:
            adjustment = connection.execute("SELECT charge_tokens FROM budget_adjustments WHERE call_id=?", (call_id,)).fetchone()
        self.assertIsNone(adjustment)

    def test_pre_goal_timeout_observation_survives_restart_and_attaches_latest_known_once(self) -> None:
        pending_goal_id = new_id("goal")
        self.manager.configure(
            self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10)
        )
        request = RoleCallRequest(
            role="goal_reviewer", instructions="pre-Goal timeout 관측", payload={},
            output_schema={"type": "object", "properties": {}}, model="test-model",
            effort="medium", inventory_digest=_digest("inventory"), cwd=str(self.root),
        )
        call_id = self.manager.reserve(
            project_id=self.project_id, goal_id=pending_goal_id, goal_digest=None,
            call_key="pre-goal-timeout", role="goal_reviewer", stage=BudgetStage.GOAL_REVIEW,
            request=request.model_dump(mode="json"),
        )
        receipt = RoleCallReceipt(
            call_id="pre-goal-timeout", role="goal_reviewer", status="timed_out",
            model="test-model", effort="medium", inventory_digest=_digest("inventory"),
            permission_profile=":danger-full-access", approval_policy="never",
            thread_id="thread:pre-goal", turn_ids=("turn:pre-goal",),
            input_digest=request.request_digest, output_digest=_digest("output:pre-goal"),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            input_tokens=None, cached_input_tokens=None,
            output_tokens=None, reasoning_tokens=None, usage_available=False, latency_ms=0,
            recorded_at=utc_now(),
        )
        self.manager.settle(call_id, receipt)
        original_receipt = self._call_row(call_id)["receipt_json"]

        active = RuntimeObservation(
            thread_id="thread:pre-goal", turn_id="turn:pre-goal", active=True,
            terminal_status=None, payload={"usage_scope": "turn", "usage": {}},
        )
        self.assertIsNone(self.manager.observe_role_terminal(call_id, active))
        reopened = BudgetManager(EngineService(self.ledger))
        self.assertIsNone(reopened.observe_role_terminal(call_id, active))
        self.assertEqual(("reserved", None, None), (
            self._call_row(call_id)["status"], self._call_row(call_id)["actual_tokens"],
            self._call_row(call_id)["usage_id"],
        ))

        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_OBSERVATION_BINDING_MISMATCH"):
            self.manager.observe_role_terminal(
                call_id, RuntimeObservation(thread_id="wrong", turn_id="turn:pre-goal",
                                            active=False, terminal_status="completed", payload={})
            )
        unknown = RuntimeObservation(
            thread_id="thread:pre-goal", turn_id="turn:pre-goal", active=False,
            terminal_status="completed", final_response="완료",
            payload={"usage_scope": "thread", "usage": {}},
        )
        self.assertIsNone(self.manager.observe_role_terminal(call_id, unknown))
        self.assertIsNone(reopened.observe_role_terminal(call_id, unknown))
        self.assertEqual(("usage_unknown", None, None), (
            self._call_row(call_id)["status"], self._call_row(call_id)["actual_tokens"],
            self._call_row(call_id)["usage_id"],
        ))
        following = reopened.reserve(
            project_id=self.project_id, goal_id=pending_goal_id, goal_digest=None,
            call_key="allowed-after-unknown-usage", role="goal_reviewer", request={},
        )
        reopened.release_before_effect(following, reason="테스트의 후속 예약 정리")
        unknown_summary = EngineApplication(self.service).usage_summary(
            self.project_id, goal_id=pending_goal_id
        )
        self.assertTrue(unknown_summary.provider_receipt_usage, unknown_summary)
        self.assertEqual("runtime_observation", unknown_summary.provider_receipt_usage[0].projection_source)
        self.assertIsNone(unknown_summary.input_tokens.total)

        known = RuntimeObservation(
            thread_id="thread:pre-goal", turn_id="turn:pre-goal", active=False,
            terminal_status="completed", final_response="완료",
            payload={"usage_scope": "turn", "usage": {
                "inputTokens": 5, "cachedInputTokens": 1, "outputTokens": 2,
                "reasoningOutputTokens": 1, "totalTokens": 7,
            }},
        )
        self.assertIsNone(reopened.observe_role_terminal(call_id, known))
        public = EngineApplication(self.service).usage_summary(
            self.project_id, goal_id=pending_goal_id
        )
        self.assertEqual((5, 2, 1), (
            public.input_tokens.total, public.output_tokens.total,
            public.cached_input_tokens.total,
        ))
        self.assertEqual("runtime_observation", public.provider_receipt_usage[0].projection_source)
        self.assertEqual(sha256_digest(known),
                         public.provider_receipt_usage[0].runtime_observation_digest)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECONCILIATION_CONFLICT"):
            reopened.observe_role_terminal(call_id, known.model_copy(update={
                "payload": {"usage_scope": "turn", "usage": {
                    "inputTokens": 6, "cachedInputTokens": 1, "outputTokens": 2,
                    "reasoningOutputTokens": 1, "totalTokens": 8,
                }},
            }))

        pending_goal = goal(self.project_id, self.profile.definition_digest)
        pending_definition = pending_goal.definition.model_copy(
            update={"observable_outcome": "pre-Goal 재관측을 실제 Goal에 귀속한다."}
        )
        pending_goal = pending_goal.model_copy(update={
            "goal_id": pending_goal_id, "definition": pending_definition,
            "definition_digest": pending_definition.definition_digest,
        })
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_revisions (id,goal_id,project_id,revision_no,definition_digest,payload_json,"
                "status,supersedes_id,created_at,activated_at) VALUES (?,?,?,?,?,?,'ready',NULL,?,NULL)",
                (pending_goal.goal_revision_id, pending_goal.goal_id, self.project_id, 1,
                 pending_goal.definition_digest, pending_goal.model_dump_json(),
                 pending_goal.created_at.isoformat()),
            )
        reopened.attach_goal(self.project_id, pending_goal_id, pending_goal.definition_digest)
        reopened.attach_goal(self.project_id, pending_goal_id, pending_goal.definition_digest)
        row = self._call_row(call_id)
        self.assertEqual(("settled", 7, original_receipt),
                         (row["status"], row["actual_tokens"], row["receipt_json"]))
        with self.ledger.read() as connection:
            usage_count = connection.execute(
                "SELECT COUNT(*) FROM budget_usage WHERE project_id=? AND logical_call_ref=?",
                (self.project_id, receipt.call_id),
            ).fetchone()[0]
            runtime_observed_count = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type='budget.call_observed' "
                "AND json_extract(payload_json,'$.observation_kind')='runtime_observation'",
                (call_id,),
            ).fetchone()[0]
            settled_count = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type='budget.call_settled'",
                (call_id,),
            ).fetchone()[0]
        self.assertEqual((1, 3, 1), (usage_count, runtime_observed_count, settled_count))
        attached = EngineApplication(self.service).usage_summary(
            self.project_id, goal_id=pending_goal_id
        )
        self.assertEqual((5, 2, 1), (
            attached.input_tokens.total, attached.output_tokens.total,
            attached.cached_input_tokens.total,
        ))
        self.assertEqual((), attached.provider_receipt_usage)


if __name__ == "__main__":
    unittest.main()
