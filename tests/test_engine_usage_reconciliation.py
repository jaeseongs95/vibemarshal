from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import BudgetStage, utc_now
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.roles import RoleCallReceipt
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
        self.assertTrue(any(item.code == "PROVIDER_CALL_NOT_SETTLED" for item in summary.incomplete_reasons))
        self.assertTrue(any(item.code == "USAGE_UNAVAILABLE" for item in summary.incomplete_reasons))

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

    def test_later_actual_usage_overrides_adjustment_for_budget_math_without_erasing_history(self) -> None:
        call_id, receipt = self._reserve_timeout("adjusted-then-observed", status="failed")
        self.assertEqual("usage_unknown", self._call_row(call_id)["status"])
        self.manager.adjust_unknown(call_id=call_id, charge_tokens=77, reason="초기 관측에 usage가 없음")
        effective = self.manager.observe_role_terminal(call_id, self._terminal(receipt))

        status = self.manager.status(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual(0, effective.input_tokens)
        self.assertEqual((0, 0), (status.measured_token_subtotal, status.explicit_adjustment_tokens))
        with self.ledger.read() as connection:
            adjustment = connection.execute("SELECT charge_tokens FROM budget_adjustments WHERE call_id=?", (call_id,)).fetchone()
        assert adjustment is not None
        self.assertEqual(77, adjustment["charge_tokens"])


if __name__ == "__main__":
    unittest.main()
