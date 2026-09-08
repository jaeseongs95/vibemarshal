from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.budget import BudgetBlocked, BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.benchmark import _legacy_hard_timeout_contract
from flowmarshal.engine.domain import GoalAuthorization, GoalOperatingPolicy, UsageObservation, utc_now
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.ledger import (
    ENGINE_SCHEMA_REVISION,
    SQLITE_APPLICATION_ID,
    EngineLedgerError,
    SQLiteEngineHistoryReader,
    SQLiteEngineLedger,
)
from flowmarshal.engine.roles import RoleCallReceipt
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import goal, profile


def _digest(value: str) -> str:
    return sha256_digest(value)


class ExecutionUsageSeparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        self.ledger = SQLiteEngineLedger(base / "engine.sqlite3")
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="FM-02", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.manager = BudgetManager(self.service)
        self.manager.configure(
            self.project_id,
            GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _reserve(self, key: str, *, goal_id: str | None = None) -> str:
        return self.manager.reserve(
            project_id=self.project_id,
            goal_id=goal_id or self.goal.goal_id,
            goal_digest=self.goal.definition_digest if goal_id is None else None,
            call_key=key,
            role="goal_reviewer",
            request={"call_key": key},
        )

    def _receipt(
        self,
        key: str,
        *,
        status: str = "succeeded",
        usage_available: bool = False,
    ) -> RoleCallReceipt:
        values = (3, 1, 2, 1) if usage_available else (None, None, None, None)
        return RoleCallReceipt(
            call_id=key,
            role="goal_reviewer",
            status=status,
            model="test-model",
            effort="medium",
            inventory_digest=_digest("inventory"),
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id=f"thread:{key}",
            turn_ids=(f"turn:{key}",),
            input_digest=_digest(f"input:{key}"),
            output_digest=_digest(f"output:{key}"),
            output_schema_digest=_digest("schema"),
            input_tokens=values[0],
            cached_input_tokens=values[1],
            output_tokens=values[2],
            reasoning_tokens=values[3],
            usage_available=usage_available,
            latency_ms=1,
            recorded_at=utc_now(),
        )

    def _row(self, call_id: str):
        with self.ledger.read() as connection:
            return connection.execute(
                "SELECT * FROM provider_calls WHERE id=?", (call_id,)
            ).fetchone()

    def test_fm_02_c1_valid_terminal_without_usage_does_not_block_next_goal(self) -> None:
        """유효 결과의 usage null은 같은 Goal과 독립 Goal의 다음 호출을 막지 않는다."""
        first = self._reserve("valid-without-usage")
        self.manager.settle(first, self._receipt("valid-without-usage"))
        row = self._row(first)
        self.assertEqual(
            ("terminal", "none", "valid", "usage_unknown", None),
            (
                row["execution_status"], row["effect_status"], row["result_status"],
                row["status"], row["actual_tokens"],
            ),
        )
        status = self.manager.status(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual((), status.unresolved_call_ids)
        self.assertIsNone(status.error_code)
        self.assertIsNone(status.remaining_total_tokens)

        same_goal = self._reserve("same-goal-next")
        self.manager.settle(same_goal, self._receipt("same-goal-next"))
        independent_root = Path(self.temp.name) / "independent-project"
        independent_root.mkdir()
        independent_project = self.service.create_project(
            name="FM-02-independent", root=independent_root
        )
        independent_profile = profile(independent_project)
        self.service.register_profile(independent_profile)
        independent = goal(independent_project, independent_profile.definition_digest)
        self.service.register_goal(independent)
        independent_manager = BudgetManager(self.service)
        independent_manager.configure(
            independent_project,
            GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
        )
        other_goal = independent_manager.reserve(
            project_id=independent_project,
            goal_id=independent.goal_id,
            goal_digest=independent.definition_digest,
            call_key="other-goal-next",
            role="goal_reviewer",
            request={"call_key": "other-goal-next"},
        )
        self.assertEqual("reserved", self._row(other_goal)["execution_status"])

    def test_fm_02_c2_unknown_effect_is_observe_first_but_unknown_usage_is_not(self) -> None:
        """effect unknown만 admission을 차단하며 미확인 token은 null로 남는다."""
        call_id = self._reserve("unknown-effect")
        receipt = self._receipt("unknown-effect", status="external_unknown")
        self.manager.settle(call_id, receipt)
        row = self._row(call_id)
        self.assertEqual(("unknown", "unknown", "unknown"), (
            row["execution_status"], row["effect_status"], row["result_status"],
        ))
        with self.ledger.read() as connection:
            stored = connection.execute(
                "SELECT payload_json FROM usage_observations WHERE provider_call_id=?", (call_id,)
            ).fetchone()
        observation = UsageObservation.model_validate_json(stored["payload_json"])
        self.assertEqual("unavailable", observation.measurement_status)
        self.assertEqual((None, None, None, None), (
            observation.input_tokens, observation.cached_input_tokens,
            observation.output_tokens, observation.reasoning_tokens,
        ))
        with self.assertRaisesRegex(BudgetBlocked, "PROVIDER_EFFECT_UNKNOWN"):
            self._reserve("must-observe-first", goal_id="goal_independent")

    def test_partial_usage_preserves_available_components_without_blocking(self) -> None:
        call_id = self._reserve("partial-usage")
        receipt = self._receipt("partial-usage", usage_available=True).model_copy(update={
            "cached_input_tokens": None,
            "reasoning_tokens": None,
        })
        self.manager.settle(call_id, receipt)
        row = self._row(call_id)
        self.assertEqual(
            ("terminal", "none", "valid", "settled", 5),
            (
                row["execution_status"], row["effect_status"], row["result_status"],
                row["status"], row["actual_tokens"],
            ),
        )
        with self.ledger.read() as connection:
            stored = connection.execute(
                "SELECT payload_json FROM usage_observations WHERE provider_call_id=?",
                (call_id,),
            ).fetchone()
        observation = UsageObservation.model_validate_json(stored["payload_json"])
        self.assertEqual("measured", observation.measurement_status)
        self.assertEqual((3, None, 2, None), (
            observation.input_tokens,
            observation.cached_input_tokens,
            observation.output_tokens,
            observation.reasoning_tokens,
        ))
        self.assertIsNone(self.manager.status(
            self.project_id, goal_id=self.goal.goal_id
        ).error_code)

        before = dict(row)
        effective = self.manager.observe_role_terminal(
            call_id,
            RuntimeObservation(
                thread_id=receipt.thread_id or "",
                turn_id=receipt.turn_ids[0],
                active=False,
                terminal_status="completed",
                final_response="유효 결과",
                payload={
                    "usage_scope": "turn",
                    "usage": {
                        "inputTokens": 3,
                        "cachedInputTokens": 1,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 5,
                    },
                },
            ),
        )
        after = dict(self._row(call_id))
        self.assertEqual((3, 1, 2, 1), (
            effective.input_tokens, effective.cached_input_tokens,
            effective.output_tokens, effective.reasoning_tokens,
        ))
        for key in (
            "execution_status", "effect_status", "result_status", "status",
            "actual_tokens", "completed_at", "receipt_json", "raw_receipt_digest",
        ):
            self.assertEqual(before[key], after[key], key)

        with self.assertRaisesRegex(BudgetBlocked, "BUDGET_RECONCILIATION_CONFLICT"):
            self.manager.observe_role_terminal(
                call_id,
                RuntimeObservation(
                    thread_id=receipt.thread_id or "",
                    turn_id=receipt.turn_ids[0],
                    active=False,
                    terminal_status="completed",
                    final_response="충돌",
                    payload={
                        "usage_scope": "turn",
                        "usage": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6},
                    },
                ),
            )

    def test_fm_02_c2_pending_effect_is_observe_first(self) -> None:
        """terminal 실행이어도 effect pending이면 프로젝트 전체 admission을 차단한다."""
        call_id = self._reserve("pending-effect")
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET execution_status='terminal',effect_status='pending',"
                "result_status='unknown' WHERE id=?",
                (call_id,),
            )
        status = self.manager.status(self.project_id, goal_id=self.goal.goal_id)
        self.assertEqual((call_id,), status.unresolved_call_ids)
        self.assertEqual("PROVIDER_EFFECT_UNKNOWN", status.error_code)
        with self.assertRaisesRegex(BudgetBlocked, "PROVIDER_EFFECT_UNKNOWN"):
            self._reserve("pending-effect-blocks-next")

    def test_fm_02_c3_late_usage_only_appends_accounting_observation(self) -> None:
        """late usage가 실행 슬롯·turn 횟수·완료 시각·원본 receipt를 바꾸지 않는다."""
        call_id = self._reserve("late-usage")
        receipt = self._receipt("late-usage")
        self.manager.settle(call_id, receipt)
        before = dict(self._row(call_id))
        before_call_count = 1
        with self.ledger.read() as connection:
            before_call_count = connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE project_id=?", (self.project_id,)
            ).fetchone()[0]

        effective = self.manager.observe_role_terminal(
            call_id,
            RuntimeObservation(
                thread_id=receipt.thread_id or "",
                turn_id=receipt.turn_ids[0],
                active=False,
                terminal_status="completed",
                final_response="유효 결과",
                payload={
                    "usage_scope": "turn",
                    "usage": {
                        "inputTokens": 3,
                        "cachedInputTokens": 1,
                        "outputTokens": 2,
                        "reasoningOutputTokens": 1,
                        "totalTokens": 5,
                    },
                },
            ),
        )
        self.assertTrue(effective.usage_available)
        after = dict(self._row(call_id))
        for key in (
            "execution_status", "effect_status", "result_status", "new_turn_count",
            "completed_at", "receipt_json", "raw_receipt_digest",
        ):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(sha256_digest(receipt), after["raw_receipt_digest"])
        with self.ledger.read() as connection:
            observations = connection.execute(
                "SELECT payload_json FROM usage_observations WHERE provider_call_id=? ORDER BY rowid",
                (call_id,),
            ).fetchall()
            after_call_count = connection.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE project_id=?", (self.project_id,)
            ).fetchone()[0]
        first, late = (UsageObservation.model_validate_json(row["payload_json"]) for row in observations)
        self.assertEqual(("unavailable", "measured"), (
            first.measurement_status, late.measurement_status,
        ))
        self.assertTrue(late.late)
        self.assertEqual(first.observation_id, late.previous_observation_id)
        self.assertEqual(before_call_count, after_call_count)
        self.assertTrue(self.ledger.verify_history(self.project_id))

        with self.ledger.read() as connection:
            observation_id = connection.execute(
                "SELECT id FROM usage_observations WHERE provider_call_id=? ORDER BY rowid LIMIT 1",
                (call_id,),
            ).fetchone()["id"]
            reconciliation_id = connection.execute(
                "SELECT id FROM usage_reconciliations WHERE call_id=? ORDER BY rowid LIMIT 1",
                (call_id,),
            ).fetchone()["id"]
        for table, row_id in (
            ("usage_observations", observation_id),
            ("usage_reconciliations", reconciliation_id),
        ):
            with self.subTest(table=table, operation="update"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "APPEND_ONLY"):
                    with self.ledger.transaction() as tx:
                        tx.connection.execute(
                            f"UPDATE {table} SET id=id WHERE id=?", (row_id,)
                        )
            with self.subTest(table=table, operation="delete"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "APPEND_ONLY"):
                    with self.ledger.transaction() as tx:
                        tx.connection.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))

    def test_fm_02_c4_schema4_history_reader_and_input_contract_terminal(self) -> None:
        """schema 4 신규 생성, schema 3 read-only, input_contract_failed terminal을 회귀한다."""
        with self.ledger.read() as connection:
            self.assertEqual(ENGINE_SCHEMA_REVISION, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_observations'"
            ).fetchone())

        call_id = self._reserve("input-contract")
        self.manager.settle(
            call_id, self._receipt("input-contract", status="input_contract_failed")
        )
        row = self._row(call_id)
        self.assertEqual(("terminal", "none", "invalid"), (
            row["execution_status"], row["effect_status"], row["result_status"],
        ))
        following = self._reserve("after-input-contract")
        self.assertEqual("reserved", self._row(following)["execution_status"])

        historical = Path(self.temp.name) / "schema3.sqlite3"
        connection = sqlite3.connect(historical)
        try:
            connection.executescript(
                "CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "INSERT INTO schema_meta VALUES('schema_id','flowmarshal.engine');"
                "INSERT INTO schema_meta VALUES('schema_revision','3');"
                "CREATE TABLE provider_calls("
                "id TEXT PRIMARY KEY,project_id TEXT NOT NULL,status TEXT NOT NULL,actual_tokens INTEGER);"
                "INSERT INTO provider_calls VALUES('historical_call','historical_project','usage_unknown',NULL);"
            )
            connection.execute(f"PRAGMA application_id={SQLITE_APPLICATION_ID}")
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        finally:
            connection.close()
        before_digest = hashlib.sha256(historical.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EngineLedgerError, "HistoryReader"):
            SQLiteEngineLedger(historical).initialize()
        reader = SQLiteEngineHistoryReader(historical)
        history = reader.provider_call_history("historical_project")
        self.assertEqual(3, reader.schema_revision)
        self.assertEqual(("usage_unknown", "unavailable", None), (
            history[0]["historical_status"], history[0]["usage_measurement_status"],
            history[0]["execution_status"],
        ))
        self.assertEqual(before_digest, hashlib.sha256(historical.read_bytes()).hexdigest())

        inconsistent = Path(self.temp.name) / "schema3-inconsistent.sqlite3"
        inconsistent.write_bytes(historical.read_bytes())
        connection = sqlite3.connect(inconsistent)
        try:
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        finally:
            connection.close()
        inconsistent_digest = hashlib.sha256(inconsistent.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EngineLedgerError, "user_version"):
            SQLiteEngineHistoryReader(inconsistent)
        self.assertEqual(
            inconsistent_digest, hashlib.sha256(inconsistent.read_bytes()).hexdigest()
        )

    def test_benchmark_limits_are_explicit_and_not_token_reservation_conversions(self) -> None:
        first = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=100, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
            max_provider_calls=7,
            wall_timeout_seconds=321,
        )
        second = first.model_copy(update={
            "budget": GoalBudgetPolicy(total_tokens=9_999, call_reservation_tokens=3),
        })
        first_contract = _legacy_hard_timeout_contract(first)
        second_contract = _legacy_hard_timeout_contract(second)
        for contract in (first_contract, second_contract):
            self.assertEqual("explicit_evaluation_policy", contract["derivation"])
            self.assertEqual((7, 321), (
                contract["maximum_provider_calls"], contract["timeout_seconds"],
            ))
            self.assertNotIn("maximum_reserved_calls", contract)
            self.assertNotIn("budget_policy_digest", contract)

    def test_goal_provider_call_limit_is_hard_and_usage_independent(self) -> None:
        authorization = self.service.authorize_goal(
            project_id=self.project_id,
            source="test",
            operating_policy=GoalOperatingPolicy(
                max_provider_calls=2, absolute_deadline_seconds=3600,
            ),
        )
        for index in range(2):
            call_id = self._reserve(f"hard-call-{index}")
            self.manager.settle(call_id, self._receipt(f"hard-call-{index}"))
        with self.assertRaisesRegex(BudgetBlocked, "GOAL_PROVIDER_CALL_LIMIT_REACHED"):
            self._reserve("hard-call-2")
        with self.ledger.read() as connection:
            events = connection.execute(
                "SELECT payload_json FROM history_events WHERE event_type='budget.call_reserved' "
                "ORDER BY sequence DESC LIMIT 2"
            ).fetchall()
        self.assertEqual(
            {1, 2},
            {json.loads(row["payload_json"])["execution_guard"]["provider_call_ordinal"]
             for row in events},
        )
        self.assertTrue(all(
            json.loads(row["payload_json"])["execution_guard"]["authorization_id"]
            == authorization.authorization_id for row in events
        ))

    def test_goal_absolute_deadline_blocks_new_provider_call(self) -> None:
        class Clock:
            current = datetime(2030, 1, 1, tzinfo=timezone.utc)

            def now(self) -> str:
                return self.current.isoformat()

        clock = Clock()
        self.ledger.clock = clock
        authorization = self.service.authorize_goal(
            project_id=self.project_id,
            source="test",
            operating_policy=GoalOperatingPolicy(
                max_provider_calls=25, absolute_deadline_seconds=1,
            ),
        )
        clock.current = clock.current + timedelta(seconds=1)
        with self.assertRaisesRegex(BudgetBlocked, "GOAL_ABSOLUTE_DEADLINE_EXCEEDED"):
            self._reserve("after-deadline")
        self.assertEqual(
            datetime(2030, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            authorization.absolute_deadline_at,
        )

    def test_schema4_authorization_without_execution_guard_keeps_frozen_digest(self) -> None:
        authorization = self.service.authorize_goal(
            project_id=self.project_id, source="new guard",
        )
        legacy = json.loads(canonical_json(authorization))
        legacy.pop("absolute_deadline_at")
        legacy["operating_policy"].pop("execution_guard_version")
        legacy["operating_policy"].pop("max_provider_calls")
        legacy["operating_policy"].pop("absolute_deadline_seconds")

        restored = GoalAuthorization.model_validate(legacy)

        self.assertEqual(legacy, json.loads(canonical_json(restored)))
        self.assertEqual(sha256_digest(legacy), restored.authorization_digest)


if __name__ == "__main__":
    unittest.main()
