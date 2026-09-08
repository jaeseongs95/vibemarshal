"""지연 사용량을 진단 비용에 반영하면서 원래 실패를 보존하는 통합 회귀."""
from pathlib import Path
import tempfile
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.inspection_diagnostic_budget import diagnostic_budget_ledger_observation
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.runtime import RuntimeObservation
from flowmarshal.engine.service import EngineService
from flowmarshal.engine.roles import make_role_request
from scripts.diagnostics.r_s06_10 import effective_runtime_usage, apply_runtime_usage_observations
from tests.test_engine_inspection_diagnostic_budget import _UnknownUsageRunner


class DiagnosticObservationSummaryTests(unittest.TestCase):
    def test_late_usage_restores_cost_but_keeps_original_timeout_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            service = EngineService(SQLiteEngineLedger(run / "budget-state/case/flowmarshal-engine.sqlite3"))
            service.initialize()
            project = service.create_project(name="관측 회귀", root=run)
            manager = BudgetManager(service)
            manager.configure(project, GoalBudgetPolicy(total_tokens=1_500_000, call_reservation_tokens=100_000))
            request = make_role_request(role="test", instructions="검사", payload={},
                output_schema={"type": "object", "properties": {}}, model="test-model", effort="medium",
                inventory_digest=sha256_digest("inventory"), cwd=str(run))
            call_id = manager.reserve(project_id=project, goal_id="goal-fixture", goal_digest=None,
                                      call_key="one", role="test", request=request.model_dump(mode="json"))
            receipt = _UnknownUsageRunner().run(request).receipt.model_copy(update={"status": "timed_out"})
            manager.settle(call_id, receipt)
            raw = receipt.model_dump(mode="json")
            unknown = RuntimeObservation(thread_id=receipt.thread_id, turn_id=receipt.turn_ids[0],
                active=False, terminal_status="interrupted", payload={"usage": None})
            manager.observe_role_terminal(call_id, unknown)
            ledger = diagnostic_budget_ledger_observation(run)
            self.assertEqual({}, effective_runtime_usage(ledger, [raw]))
            observed = unknown.model_copy(update={"payload": {"usage_scope": "turn", "usage": {
                "inputTokens": 7, "cachedInputTokens": 2, "outputTokens": 5, "reasoningOutputTokens": 3,
                "totalTokens": 12}}})
            manager.observe_role_terminal(call_id, observed)
            ledger = diagnostic_budget_ledger_observation(run)
            effective = effective_runtime_usage(ledger, [raw])
            self.assertEqual(12, effective[receipt.call_id]["usage_total"]["totalTokens"])
            calls = [{"receipt_call_id": receipt.call_id, "capture": "clean", "outcome": "external_unknown"}]
            turns = apply_runtime_usage_observations([], calls, effective)
            self.assertEqual(12, turns[0]["usage_total"]["totalTokens"])
            self.assertEqual("external_unknown", calls[0]["outcome"])
            self.assertFalse(raw["usage_available"])
            self.assertEqual("timed_out", raw["status"])
            self.assertEqual({}, effective_runtime_usage(ledger, [raw | {"thread_id": "other"}]))


if __name__ == "__main__":
    unittest.main()
