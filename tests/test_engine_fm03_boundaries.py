from __future__ import annotations

import json
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.domain import EvidenceKind, EvidenceRecord, new_id, utc_now
from flowmarshal.engine.service import GoalAuthorizationRequired
from tests.test_engine_ledger_service import EngineServiceFixture
from tests import test_engine_goal_authorization as authorization_tests
from tests import test_engine_plan_completion_reuse as reuse_tests


class FM03AuthorizationBoundaryTests(EngineServiceFixture):
    internal_revision_evaluation = authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = authorization_tests.EngineGoalAuthorizationTests.register_internal_revision

    def test_legacy_reservation_change_does_not_expand_authorization(self):
        manager = BudgetManager(self.service)
        manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=1000, call_reservation_tokens=10))
        authorization = self.service.authorize_goal(project_id=self.project_id, source="user")
        manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=1000, call_reservation_tokens=999))
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        with self.ledger.read() as connection:
            row = connection.execute("SELECT authorization_id FROM plan_activations").fetchone()
            self.assertEqual(authorization.authorization_id, row[0])
            stored = json.loads(connection.execute("SELECT payload_json FROM goal_authorizations").fetchone()[0])
        self.assertEqual(10, json.loads(stored["budget_policies"][0])["policy"]["call_reservation_tokens"])

    def test_real_budget_expansion_still_requires_authorization(self):
        manager = BudgetManager(self.service)
        manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=1000, call_reservation_tokens=10))
        self.service.authorize_goal(project_id=self.project_id, source="user")
        manager.configure(self.project_id, GoalBudgetPolicy(total_tokens=1001, call_reservation_tokens=999))
        with self.assertRaises(GoalAuthorizationRequired) as caught:
            self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)
        self.assertTrue(any(change["boundary"] == "policy" for change in caught.exception.changes))


class FM03MinimalEvidenceTests(EngineServiceFixture):
    internal_revision_evaluation = authorization_tests.EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = authorization_tests.EngineGoalAuthorizationTests.register_internal_revision
    finish_worker_and_validate = reuse_tests.EnginePlanCompletionReuseTests.finish_worker_and_validate
    source_rows = reuse_tests.EnginePlanCompletionReuseTests.source_rows
    status_of = reuse_tests.EnginePlanCompletionReuseTests.status_of

    def test_only_validation_bound_and_verified_current_worker_files_are_reused(self):
        worker_id = self.finish_worker_and_validate()
        unrelated = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=self.project_id, task_id=self.task.task_id,
            kind=EvidenceKind.FILE, source_ref="app.py", observation="unverified late submission",
            content_digest=sha256_digest("unverified late submission"), observed_at=utc_now(),
        )
        self.service.record_evidence(unrelated)
        before = self.source_rows()
        with self.ledger.read() as connection:
            results = self.service.effective_task_validation_results(connection, self.task.task_id)
            required = {ref for result in results for ref in json.loads(result["payload_json"])["evidence_ids"]}
            worker_files = {row[0] for row in connection.execute(
                "SELECT id FROM evidence_records WHERE attempt_id = ? AND kind = 'file'", (worker_id,),
            )}
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("completed", self.status_of(task.task_id))
        with self.ledger.read() as connection:
            reused = {row["id"] for row in self.service.task_evidence_rows(connection, task.task_id)}
        self.assertEqual(required | worker_files, reused)
        self.assertNotIn(unrelated.evidence_id, reused)
        self.assertEqual(before, self.source_rows())
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_unverifiable_required_evidence_prevents_reuse_without_rewriting_completion(self):
        self.finish_worker_and_validate()
        # 손상된 저장소를 합성 DB에만 주입한다. 정상 API의 append-only 제약은 유지한다.
        with self.ledger.transaction() as tx:
            tx.connection.execute("DROP TRIGGER tr_engine_evidence_no_update")
            tx.connection.execute("UPDATE evidence_records SET observation = 'corrupted' WHERE kind = 'test'")
        before = self.source_rows()
        revision, task = self.register_internal_revision()
        self.service.activate_authorized_plan(plan_revision_id=revision.plan_revision_id)
        self.assertEqual("ready", self.status_of(task.task_id))
        self.assertEqual("completed", self.status_of(self.task.task_id))
        self.assertEqual(before, self.source_rows())
        with self.ledger.read() as connection:
            decision = connection.execute(
                "SELECT payload_json FROM history_events "
                "WHERE entity_id=? AND event_type='task.completion_reuse_rejected' "
                "ORDER BY sequence DESC LIMIT 1",
                (task.task_id,),
            ).fetchone()
        self.assertIsNotNone(decision)
        self.assertEqual(
            "EVIDENCE_INVALID_OR_INSUFFICIENT",
            json.loads(decision[0])["reason"],
        )


if __name__ == "__main__":
    unittest.main()
