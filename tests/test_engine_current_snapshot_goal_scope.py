"""current StateSnapshot 조회가 Goal digest 단위로 한정되는지 고정한다.

current StateSnapshot은 (project_id, goal_contract_digest)마다 따로 남는다
(`uq_engine_current_state_scope`). 같은 Goal의 revision 뒤에는 두 revision의 current가
함께 있으므로, project 단위로만 읽으면 행 순서에 따라 다른 Goal의 snapshot을 대조한다.
digest 순서는 실행마다 달라지므로 각 방향을 명시적으로 골라 둘 다 확인한다.
"""
from __future__ import annotations

import json
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    EvidenceFreshness,
    GoalContractRevision,
    ReviewerSubmission,
    RevisionStatus,
    StateFact,
    ValidationResult,
    ValidationStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.plan_reuse import observation_checkpoint
from flowmarshal.engine.planning import (
    PlanningSearchOutcome,
    SkeletonFirstPlanner,
    plan_review_evidence_catalog,
)
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineServiceError
from tests import test_engine_plan_completion_reuse as reuse_tests
from tests import test_engine_planning_feedback as feedback_tests
from tests.engine_helpers import skeleton, state
from tests.test_engine_ledger_service import EngineServiceFixture


def revised_goal(base: GoalContractRevision, *, larger: bool) -> GoalContractRevision:
    """base의 다음 revision 중 definition digest가 base보다 큰(larger) 또는 작은 것을 고른다."""
    for index in range(256):
        definition = base.definition.model_copy(
            update={"observable_outcome": f"개정된 evidence가 존재한다. ({index})"}
        )
        if (definition.definition_digest > base.definition_digest) is larger:
            return GoalContractRevision(
                goal_revision_id=new_id("goal_revision"),
                goal_id=base.goal_id,
                revision_no=base.revision_no + 1,
                definition=definition,
                definition_digest=definition.definition_digest,
                status=RevisionStatus.READY,
                supersedes_goal_revision_id=base.goal_revision_id,
                created_at=utc_now(),
            )
    raise AssertionError("digest 방향 조건에 맞는 Goal revision을 찾지 못했다.")


class CleanPlanReviewer:
    def review(self, *, plan, goal, state, project_map, **_):
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        return ReviewerSubmission(
            reviewer_role="compact-plan-reviewer",
            candidate_digest=plan.activation_digest,
            ratings=feedback_tests._clean_ratings(),
            evidence_catalog_digest=sha256_digest(catalog),
        )


class CurrentSnapshotGoalScopeTests(EngineServiceFixture):
    finish_worker_and_validate = reuse_tests.EnginePlanCompletionReuseTests.finish_worker_and_validate

    def revise_goal(self, *, larger: bool, extra_facts=()):
        """같은 Goal의 다음 revision을 활성화하고 그 Goal의 StateSnapshot을 기록한다."""
        revised = revised_goal(self.goal, larger=larger)
        self.service.register_goal(revised)
        snapshot = state(self.project_id, revised.definition_digest, self.map.revision_digest)
        snapshot = snapshot.model_copy(update={"facts": (*snapshot.facts, *extra_facts)})
        self.service.record_state_snapshot(snapshot)
        with self.ledger.read() as connection:
            current = {
                row[0]
                for row in connection.execute(
                    "SELECT goal_contract_digest FROM state_snapshots "
                    "WHERE project_id = ? AND is_current = 1",
                    (self.project_id,),
                )
            }
        # 전제: 두 Goal revision의 current snapshot이 함께 남아 있다.
        self.assertEqual({self.goal.definition_digest, revised.definition_digest}, current)
        self.assertEqual(larger, revised.definition_digest > self.goal.definition_digest)
        return revised, snapshot

    def planning_search(self, goal_revision, snapshot) -> PlanningSearchOutcome:
        outcome = SkeletonFirstPlanner(
            generator=feedback_tests.Generator(skeleton(goal_revision, snapshot)),
            skeleton_reviewer=feedback_tests.CleanSkeletonReviewer(),
            expander=feedback_tests.FeedbackExpander(
                self.project_id,
                goal_revision,
                snapshot,
                self.map.revision_digest,
                self.inventory,
            ),
            plan_reviewer=CleanPlanReviewer(),
        ).search(goal=goal_revision, state=snapshot, project_map=self.map)
        for evaluation in outcome.skeleton_evaluations:
            self.service.record_skeleton_evaluation(evaluation)
        for evaluation in outcome.plan_evaluations:
            self.service.register_plan_evaluation(evaluation)
        return outcome

    def assert_planning_search_uses_active_goal_snapshot(self, *, larger: bool) -> None:
        revised, snapshot = self.revise_goal(larger=larger)
        outcome = self.planning_search(revised, snapshot)

        search_id = self.service.record_planning_search(outcome)
        with self.ledger.read() as connection:
            recorded = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id = ? "
                "AND event_type = 'planning.search_recorded' AND entity_id = ?",
                (self.project_id, search_id),
            ).fetchone()[0]
        self.assertEqual(1, recorded)

        # 가드 유지: 다른 Goal(직전 revision)의 current snapshot digest는 거절한다.
        forged = PlanningSearchOutcome.model_validate(
            outcome.model_dump(mode="json") | {"state_snapshot_digest": self.state.snapshot_digest}
        )
        with self.assertRaisesRegex(
            EngineServiceError, r"^Planning search가 current StateSnapshot과 다릅니다\.$"
        ):
            self.service.record_planning_search(forged)

    def test_planning_search_active_goal_digest_larger(self) -> None:
        # project 단위 조회가 digest가 작은 직전 Goal의 snapshot을 돌려주던 방향이다.
        self.assert_planning_search_uses_active_goal_snapshot(larger=True)

    def test_planning_search_active_goal_digest_smaller(self) -> None:
        self.assert_planning_search_uses_active_goal_snapshot(larger=False)

    def assert_checkpoint_uses_task_plan_goal_snapshot(self, *, larger: bool) -> None:
        self.finish_worker_and_validate()
        # 같은 Goal의 다음 revision을 활성화하려면 첫 Goal을 SATISFIED로 닫아 active Plan을 비운다.
        with self.ledger.read() as connection:
            results = self.service.effective_task_validation_results(connection, self.task.task_id)
        integration = self.plan.definition.integration_validations[0]
        self.service.record_validation(
            project_id=self.project_id,
            plan_revision_id=self.plan.plan_revision_id,
            result=ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=integration.validation_id,
                status=ValidationStatus.PASS,
                evidence_ids=tuple(json.loads(results[0]["payload_json"])["evidence_ids"]),
                rationale="독립 통합 검증 합성 관측",
                evaluated_at=utc_now(),
            ),
        )
        outcome = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory)).run_once(self.project_id)
        self.assertEqual("completed", outcome.action.value)
        with self.ledger.read() as connection:
            before = observation_checkpoint(self.service, connection, self.task.task_id)
        self.assertIsNotNone(before)

        # 새 active Goal의 snapshot에는 current가 아닌 사실을 둔다. 이 snapshot을 보면 None이 된다.
        stale = StateFact(
            fact_id="fact_other_goal_stale",
            predicate="다른 Goal 범위의 오래된 관측",
            value=True,
            source_ref="environment",
            evidence_digest=sha256_digest(True),
            freshness=EvidenceFreshness.STALE,
        )
        self.revise_goal(larger=larger, extra_facts=(stale,))
        with self.ledger.read() as connection:
            after = observation_checkpoint(self.service, connection, self.task.task_id)
        # task가 결속된 Plan의 Goal(첫 revision) snapshot으로 계산해 active Goal이 바뀌어도 같다.
        self.assertEqual(before, after)

    def test_checkpoint_after_revision_with_larger_digest(self) -> None:
        self.assert_checkpoint_uses_task_plan_goal_snapshot(larger=True)

    def test_checkpoint_after_revision_with_smaller_digest(self) -> None:
        # project 단위 조회가 digest가 작은 새 active Goal의 snapshot을 돌려주던 방향이다.
        self.assert_checkpoint_uses_task_plan_goal_snapshot(larger=False)


if __name__ == "__main__":
    unittest.main()
