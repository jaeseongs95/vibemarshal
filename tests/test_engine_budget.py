from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import BudgetStage, new_id, utc_now
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import goal, profile


def _digest(value: str) -> str:
    return sha256_digest(value)


class BudgetFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.ledger = SQLiteEngineLedger(Path(self.temp.name) / "engine.sqlite3")
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="예산", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.manager = BudgetManager(self.service)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def receipt(
        self,
        call_key: str,
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
        available: bool = True,
        status: str = "succeeded",
    ) -> RoleCallReceipt:
        return RoleCallReceipt(
            call_id=call_key,
            role="goal_reviewer",
            status=status,
            model="test-model",
            effort="medium",
            inventory_digest=_digest("inventory"),
            permission_profile=":danger-full-access",
            approval_policy="never",
            input_digest=_digest("input:" + call_key),
            output_digest=_digest("output:" + call_key),
            output_schema_digest=_digest("schema"),
            input_tokens=input_tokens,
            cached_input_tokens=0,
            output_tokens=output_tokens,
            reasoning_tokens=0,
            usage_available=available,
            latency_ms=0,
            recorded_at=utc_now(),
        )

    def reserve(
        self,
        call_key: str,
        *,
        goal_digest: str | None = None,
        stage: BudgetStage = BudgetStage.GOAL_REVIEW,
        goal_id: str | None = None,
    ) -> str:
        return self.manager.reserve(
            project_id=self.project_id,
            goal_id=goal_id or self.goal.goal_id,
            goal_digest=self.goal.definition_digest if goal_digest is None else goal_digest,
            call_key=call_key,
            role="goal_reviewer",
            stage=stage,
            request={"call_key": call_key},
        )

    def provider_call(self, call_id: str):
        with self.ledger.read() as connection:
            return connection.execute("SELECT * FROM provider_calls WHERE id = ?", (call_id,)).fetchone()

    def test_project_default_and_goal_override_choose_the_right_policy(self) -> None:
        default = GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10)
        override = GoalBudgetPolicy(total_tokens=40, call_reservation_tokens=7)
        self.manager.configure(self.project_id, default)
        self.manager.configure(self.project_id, override, goal_id=self.goal.goal_id)

        call = self.reserve("goal-override")

        row = self.provider_call(call)
        assert row is not None
        self.assertEqual(0, row["estimated_tokens"])
        self.assertEqual(sha256_digest(override), row["policy_digest"])
        with self.ledger.read() as connection:
            revisions = connection.execute(
                "SELECT scope_key, revision_no FROM budget_policy_revisions WHERE project_id = ? ORDER BY scope_key",
                (self.project_id,),
            ).fetchall()
        self.assertEqual([( "", 1), (self.goal.goal_id, 1)], [(item["scope_key"], item["revision_no"]) for item in revisions])

    def test_default_policy_applies_when_goal_override_is_absent(self) -> None:
        policy = GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=11)
        self.manager.configure(self.project_id, policy)

        call = self.reserve("default")

        row = self.provider_call(call)
        assert row is not None
        self.assertEqual(0, row["estimated_tokens"])
        self.assertEqual(sha256_digest(policy), row["policy_digest"])

    def test_normal_reserve_keeps_replan_quarter_but_replan_can_use_it(self) -> None:
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        first = self.reserve("first")
        self.manager.settle(first, self.receipt("first", input_tokens=65, output_tokens=0))
        second = self.reserve("second")
        self.manager.settle(second, self.receipt("second", input_tokens=10, output_tokens=0))

        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_BLOCKED"):
            self.reserve("normal-over-quarter")
        replan = self.reserve("replan", stage=BudgetStage.REPLAN)

        row = self.provider_call(replan)
        assert row is not None
        self.assertEqual("replan", row["stage"])
        self.assertEqual("reserved", row["status"])

    def test_measured_zero_and_unavailable_usage_both_allow_next_reservation(self) -> None:
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        zero = self.reserve("measured-zero")
        self.manager.settle(zero, self.receipt("measured-zero", input_tokens=0, output_tokens=0, available=True))
        zero_row = self.provider_call(zero)
        assert zero_row is not None
        self.assertEqual(("settled", 0), (zero_row["status"], zero_row["actual_tokens"]))

        unavailable = self.reserve("unavailable")
        self.manager.settle(unavailable, self.receipt("unavailable", available=False))
        unknown_row = self.provider_call(unavailable)
        assert unknown_row is not None
        self.assertEqual(("usage_unknown", None), (unknown_row["status"], unknown_row["actual_tokens"]))
        following = self.reserve("after-unavailable")
        self.assertEqual("reserved", self.provider_call(following)["execution_status"])

    def test_timeout_without_receipt_stays_reserved_across_service_reopen(self) -> None:
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        pending = self.reserve("timeout")
        self.manager.settle(pending, None)
        row = self.provider_call(pending)
        assert row is not None
        self.assertEqual(("reserved", None, None), (row["status"], row["receipt_json"], row["completed_at"]))

        reopened = BudgetManager(EngineService(self.ledger))
        with self.assertRaisesRegex(BudgetBlocked, "PROVIDER_EFFECT_UNKNOWN"):
            reopened.reserve(
                project_id=self.project_id,
                goal_id=self.goal.goal_id,
                goal_digest=self.goal.definition_digest,
                call_key="after-restart",
                role="goal_reviewer",
                request={"call_key": "after-restart"},
            )
        self.assertEqual("reserved", self.provider_call(pending)["status"])

    def test_duplicate_call_key_and_receipt_settlement_are_idempotent_but_conflict_is_rejected(self) -> None:
        call = self.reserve("same-key")
        with self.assertRaisesRegex(BudgetBlocked, "PROVIDER_CALL_ALREADY_RESERVED"):
            self.reserve("same-key")
        receipt = self.receipt("same-key")
        self.manager.settle(call, receipt)
        self.manager.settle(call, receipt)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECEIPT_CONFLICT"):
            self.manager.settle(call, self.receipt("same-key", input_tokens=11))
        with self.ledger.read() as connection:
            count = connection.execute("SELECT COUNT(*) FROM budget_usage WHERE project_id = ?", (self.project_id,)).fetchone()[0]
        self.assertEqual(1, count)

    def test_reserved_original_receipt_is_idempotent_and_cannot_be_overwritten(self) -> None:
        call = self.reserve("pending-original")
        receipt = self.receipt("pending-original", status="timed_out", available=False).model_copy(
            update={"input_tokens": None, "cached_input_tokens": None,
                    "output_tokens": None, "reasoning_tokens": None}
        )
        self.manager.settle(call, receipt)
        row = self.provider_call(call)
        assert row is not None
        self.assertEqual(("reserved", None), (row["status"], row["actual_tokens"]))
        with self.ledger.read() as connection:
            usage = connection.execute(
                "SELECT payload_json FROM budget_usage WHERE id=?", (row["usage_id"],)
            ).fetchone()
        assert usage is not None
        self.assertFalse(json.loads(usage["payload_json"])["usage_available"])

        self.manager.settle(call, receipt)
        restarted_receipt = RoleCallReceipt.model_validate_json(self.provider_call(call)["receipt_json"])
        self.assertEqual((None, None, None, None), (
            restarted_receipt.input_tokens, restarted_receipt.cached_input_tokens,
            restarted_receipt.output_tokens, restarted_receipt.reasoning_tokens,
        ))
        # 재시작 deserialize도 미제공 token을 null로 보존한다.
        BudgetManager(EngineService(self.ledger)).settle(call, restarted_receipt)
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECEIPT_CONFLICT"):
            self.manager.settle(call, receipt.model_copy(update={"output_tokens": 6}))
        with self.ledger.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                "AND event_type='budget.call_observed'", (call,)
            ).fetchone()[0])

    def test_attach_goal_binds_a_pending_receipt_to_exact_goal_revision(self) -> None:
        pending_goal_id = new_id("goal")
        call = self.manager.reserve(
            project_id=self.project_id, goal_id=pending_goal_id, goal_digest=None,
            call_key="pre-goal", role="goal_reviewer", request={"call_key": "pre-goal"},
        )
        self.manager.settle(call, self.receipt("pre-goal", input_tokens=13, output_tokens=2))
        before = self.provider_call(call)
        assert before is not None
        self.assertEqual((None, None, "settled"), (before["goal_contract_digest"], before["usage_id"], before["status"]))

        pending_goal = goal(self.project_id, self.profile.definition_digest)
        definition = pending_goal.definition.model_copy(update={"observable_outcome": "사전 예약 receipt를 귀속한다."})
        pending_goal = pending_goal.model_copy(update={
            "goal_id": pending_goal_id,
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        # 프로젝트에는 active Goal이 있으므로, 테스트의 pending goal만 원장에 직접 등록한다.
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO goal_revisions (id,goal_id,project_id,revision_no,definition_digest,payload_json,status,supersedes_id,created_at,activated_at) "
                "VALUES (?,?,?,?,?,?, 'ready',NULL,?,NULL)",
                (pending_goal.goal_revision_id, pending_goal.goal_id, self.project_id, 1,
                 pending_goal.definition_digest, pending_goal.model_dump_json(), pending_goal.created_at.isoformat()),
            )
        self.manager.attach_goal(self.project_id, pending_goal_id, pending_goal.definition_digest)

        after = self.provider_call(call)
        assert after is not None
        self.assertEqual(pending_goal.definition_digest, after["goal_contract_digest"])
        self.assertIsNotNone(after["usage_id"])
        with self.ledger.read() as connection:
            usage = connection.execute("SELECT goal_contract_digest FROM budget_usage WHERE id = ?", (after["usage_id"],)).fetchone()
        assert usage is not None
        self.assertEqual(pending_goal.definition_digest, usage["goal_contract_digest"])

    def test_unknown_adjustment_is_prohibited_and_tokens_stay_null(self) -> None:
        call = self.reserve("unknown-adjustment")
        self.manager.settle(call, self.receipt("unknown-adjustment", available=False))
        with self.assertRaisesRegex(BudgetBlocked, "USAGE_ESTIMATION_PROHIBITED"):
            self.manager.adjust_unknown(call_id=call, charge_tokens=77, reason="provider가 usage를 제공하지 않음")

        row = self.provider_call(call)
        assert row is not None
        self.assertEqual(("usage_unknown", None), (row["status"], row["actual_tokens"]))
        with self.ledger.read() as connection:
            adjustment = connection.execute("SELECT charge_tokens FROM budget_adjustments WHERE call_id = ?", (call,)).fetchone()
        self.assertIsNone(adjustment)

    def test_unresolved_call_cannot_be_bypassed_by_another_goal_id(self) -> None:
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        self.reserve("pending")
        with self.assertRaisesRegex(BudgetBlocked, "PROVIDER_EFFECT_UNKNOWN"):
            self.manager.reserve(project_id=self.project_id, goal_id=new_id("goal"), goal_digest=None,
                                 call_key="bypass", role="goal_reviewer", request={})

    def test_actual_overrun_is_preserved_and_blocks_the_next_call(self) -> None:
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        call = self.reserve("overrun")
        self.manager.settle(call, self.receipt("overrun", input_tokens=101, output_tokens=0))
        self.assertEqual(101, self.provider_call(call)["actual_tokens"])
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_BLOCKED"):
            self.reserve("after-overrun", stage=BudgetStage.REPLAN)

    def test_receipt_for_another_role_request_cannot_settle_a_reservation(self) -> None:
        request = RoleCallRequest(role="goal_reviewer", instructions="검토", payload={},
                                  output_schema={"type": "object", "properties": {}},
                                  model="test-model", effort="medium", inventory_digest=_digest("inventory"),
                                  cwd=str(self.root))
        call = self.manager.reserve(project_id=self.project_id, goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest, call_key="bound-call", role=request.role,
            request=request.model_dump(mode="json"))
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECEIPT_BINDING_MISMATCH"):
            self.manager.settle(call, self.receipt("unrelated-call"))
        self.assertEqual("reserved", self.provider_call(call)["status"])
        self.assertIsNone(self.provider_call(call)["actual_tokens"])

    def test_checkpoint_preserves_timeout_and_requires_exact_terminal_before_adjustment(self) -> None:
        request = RoleCallRequest(role="goal_reviewer", instructions="검증", payload={"goal": "원본"},
            output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
            inventory_digest=_digest("inventory"), cwd=str(self.root))
        receipt = self.receipt("old-timeout", available=False, status="timed_out").model_copy(update={
            "thread_id": "original-thread", "turn_ids": ("original-turn",), "input_digest": request.request_digest,
            "output_schema_digest": sha256_digest(strict_json_output_schema(request.output_schema))})
        (call,) = self.manager.import_role_checkpoint(project_id=self.project_id, goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest, entries=({"request": request.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json")},), source_ref="immutable-run", source_digest=_digest("source"))
        original = self.provider_call(call)["receipt_json"]
        with self.assertRaisesRegex(BudgetBlocked, "USAGE_ESTIMATION_PROHIBITED"):
            self.manager.adjust_unknown(call_id=call, charge_tokens=10, reason="종료 관측 전")
        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_OBSERVATION_BINDING_MISMATCH"):
            self.manager.observe_role_terminal(call, RuntimeObservation(thread_id="other", turn_id="original-turn",
                active=False, terminal_status="interrupted", payload={}))
        self.manager.observe_role_terminal(call, RuntimeObservation(thread_id="original-thread", turn_id="original-turn",
            active=False, terminal_status="interrupted", payload={"usage": None}))
        with self.assertRaisesRegex(BudgetBlocked, "USAGE_ESTIMATION_PROHIBITED"):
            self.manager.adjust_unknown(call_id=call, charge_tokens=10, reason="명시 잠정 차감")
        row = self.provider_call(call)
        self.assertEqual(("usage_unknown", None, original), (row["status"], row["actual_tokens"], row["receipt_json"]))
        self.manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10))
        self.reserve("continued")
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_reconciliation_uses_the_existing_usage_id_without_duplicate_insertion(self) -> None:
        from flowmarshal.engine.budget import receipt_usage
        call = self.reserve("prior-record")
        receipt = self.receipt("prior-record")
        usage = receipt_usage(receipt, project_id=self.project_id, goal_digest=self.goal.definition_digest)
        self.service.record_budget_usage(usage)
        self.manager.settle(call, receipt)
        self.assertEqual(usage.usage_id, self.provider_call(call)["usage_id"])


if __name__ == "__main__":
    unittest.main()
