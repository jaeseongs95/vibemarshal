from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import (
    CandidateStatus,
    GoalPreparationBinding,
    GoalReviewRatingsBinding,
    PlanContractRevision,
    PlanningBudgetPolicy,
    derive_candidate_decision,
)
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    PlanningSearchOutcome,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.qualification import (
    PlanningScenarioCatalog,
    _planning_cell,
    default_role_configuration,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.runtime import FakeCodexRuntime

from tests.engine_helpers import clean_review, goal as make_goal, plan as make_plan, skeleton
from tests.test_engine_qualification import ROOT, qualification_inventory


class BenchmarkExecutionCheckpointTests(unittest.TestCase):
    """계획 checkpoint는 실제 Core 등록만 남기고 Plan을 활성화하지 않는다."""

    def setUp(self) -> None:
        self.roles = default_role_configuration(ROOT)
        self.inventory = qualification_inventory()
        catalog = PlanningScenarioCatalog.load(
            ROOT / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        )
        self.scenario = next(
            item for item in catalog.scenarios if item.scenario_id == "S01-single-bugfix"
        )
        self.fixture_root = ROOT / "tests" / "fixtures" / "engine" / "synthetic-lifecycle-project"
        self.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1000, call_reservation_tokens=50),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )

    def _prepared_goal(self, **kwargs):
        raw_goal = make_goal(kwargs["project_id"], kwargs["profile"].definition_digest)
        prepared_goal = raw_goal.model_copy(
            update={
                "preparation_binding": GoalPreparationBinding(
                    normalization_proposal_digest=sha256_digest({"fake": "normalization"}),
                    reviewer_submission_digest=sha256_digest({"fake": "review"}),
                    reviewer_role="critical_reviewer",
                    ratings=GoalReviewRatingsBinding(
                        goal_fit=4,
                        grounding=4,
                        engineering=4,
                        verification=4,
                        execution_safety=4,
                    ),
                )
            }
        )
        return SimpleNamespace(
            goal_contract=prepared_goal,
            proposal=SimpleNamespace(unresolved_questions=()),
            model_dump=lambda **_values: {"fake_goal_preparation": True},
        )

    def _selected_outcome(self, **kwargs) -> PlanningSearchOutcome:
        goal = kwargs["goal"]
        state = kwargs["state"]
        project_map = kwargs["project_map"]
        budget = kwargs["budget"]
        candidate = skeleton(goal, state)
        skeleton_digest = sha256_digest(candidate)
        skeleton_submission = clean_review(
            skeleton_digest,
            role="general_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, goal, state, project_map),
        )
        skeleton_evaluation = CandidateEvaluation(
            candidate=candidate,
            deterministic_findings=(),
            semantic_submission=skeleton_submission,
            decision=derive_candidate_decision(
                candidate_digest=skeleton_digest,
                findings=(),
                ratings=skeleton_submission.ratings,
            ),
        )

        generated_plan, _task, _decision = make_plan(
            project_map.project_id,
            goal,
            state,
            project_map.revision_digest,
            candidate,
            self.inventory,
        )
        definition = generated_plan.definition.model_copy(
            update={"planning_budget": budget}
        )
        selected_plan = PlanContractRevision.model_validate(
            generated_plan.model_dump(mode="json")
            | {
                "definition": definition.model_dump(mode="json"),
                "definition_digest": definition.definition_digest,
            }
        )
        plan_submission = clean_review(
            selected_plan.activation_digest,
            role="general_reviewer",
            evidence_catalog=plan_review_evidence_catalog(
                selected_plan, goal, state, project_map
            ),
        )
        plan_evaluation = ExpandedPlanEvaluation(
            plan=selected_plan,
            deterministic_findings=(),
            semantic_submissions=(plan_submission,),
            decision=derive_candidate_decision(
                candidate_digest=selected_plan.activation_digest,
                findings=(),
                ratings=plan_submission.ratings,
            ),
        )
        self.assertEqual(CandidateStatus.ADMISSIBLE, plan_evaluation.decision.status)
        return PlanningSearchOutcome(
            goal_contract_digest=goal.definition_digest,
            state_snapshot_digest=state.snapshot_digest,
            budget_policy=budget,
            skeleton_evaluations=(skeleton_evaluation,),
            shortlist_digests=(skeleton_digest,),
            plan_evaluations=(plan_evaluation,),
            selected_activation_digest=selected_plan.activation_digest,
            logical_role_calls=4,
            candidate_versions=1,
            budget_exhausted=False,
        )

    def test_retained_selected_cell_registers_exact_planning_evidence_without_activation(self) -> None:
        """checkpoint은 재개용 원장을 만들되 사용자 활성화 전에는 ready Plan만 남긴다."""
        with tempfile.TemporaryDirectory() as temporary:
            work_root = Path(temporary) / "cell"
            with (
                patch(
                    "flowmarshal.engine.qualification.GoalPreparationPipeline.prepare",
                    side_effect=self._prepared_goal,
                ),
                patch(
                    "flowmarshal.engine.qualification.SkeletonFirstPlanner.search",
                    side_effect=lambda **kwargs: self._selected_outcome(**kwargs),
                ),
            ):
                cell, receipts = _planning_cell(
                    scenario=self.scenario,
                    seed=701,
                    fixture_root=self.fixture_root,
                    runtime=FakeCodexRuntime(self.inventory),
                    inventory=self.inventory,
                    roles=self.roles,
                    work_root=work_root,
                    evaluation_policies=self.policies,
                    retain_execution_checkpoint=True,
                )

            self.assertTrue(cell["selected"])
            self.assertEqual((), receipts)
            checkpoint = cell["execution_checkpoint"]
            selected_path = work_root / "selected-plan.json"
            checkpoint_path = work_root / "execution-checkpoint.json"
            self.assertEqual(checkpoint, json.loads(checkpoint_path.read_text(encoding="utf-8")))
            selected_document = json.loads(selected_path.read_text(encoding="utf-8"))
            selected_plan = PlanContractRevision.model_validate(selected_document)
            self.assertEqual(checkpoint["plan_revision_id"], selected_plan.plan_revision_id)
            self.assertEqual(checkpoint["activation_digest"], selected_plan.activation_digest)
            outcome = PlanningSearchOutcome.model_validate(cell["planning_outcome"])
            self.assertEqual(checkpoint["planning_outcome_digest"], sha256_digest(outcome))
            self.assertEqual(
                selected_plan.activation_digest, outcome.selected_activation_digest
            )

            ledger = SQLiteEngineLedger(
                checkpoint["database_path"], artifact_root=checkpoint["artifact_root"])
            ledger.verify_history(checkpoint["project_id"])
            with ledger.read() as connection:
                project = connection.execute(
                    "SELECT active_goal_revision_id, active_plan_revision_id FROM projects WHERE id = ?",
                    (checkpoint["project_id"],),
                ).fetchone()
                self.assertIsNotNone(project["active_goal_revision_id"])
                self.assertIsNone(project["active_plan_revision_id"])
                self.assertEqual(
                    0,
                    connection.execute(
                        "SELECT COUNT(*) FROM plan_activations WHERE project_id = ?",
                        (checkpoint["project_id"],),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM project_map_revisions WHERE project_id = ?",
                        (checkpoint["project_id"],),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM state_snapshots WHERE project_id = ?",
                        (checkpoint["project_id"],),
                    ).fetchone()[0],
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM skeleton_candidates WHERE project_id = ?",
                        (checkpoint["project_id"],),
                    ).fetchone()[0],
                )
                plan = connection.execute(
                    "SELECT status, payload_json FROM plan_revisions WHERE id = ?",
                    (checkpoint["plan_revision_id"],),
                ).fetchone()
                self.assertEqual("ready", plan["status"])
                self.assertEqual(
                    selected_plan,
                    PlanContractRevision.model_validate_json(plan["payload_json"]),
                )
                self.assertEqual(
                    1,
                    connection.execute(
                        "SELECT COUNT(*) FROM task_contracts WHERE plan_revision_id = ?",
                        (checkpoint["plan_revision_id"],),
                    ).fetchone()[0],
                )
                search_events = connection.execute(
                    "SELECT payload_json FROM history_events WHERE project_id = ? "
                    "AND event_type = 'planning.search_recorded'",
                    (checkpoint["project_id"],),
                ).fetchall()
                self.assertEqual(1, len(search_events))
                self.assertEqual(
                    checkpoint["planning_outcome_digest"],
                    json.loads(search_events[0]["payload_json"])["outcome_digest"],
                )
