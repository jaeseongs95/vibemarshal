from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.goal import GoalNormalizationProposal
from flowmarshal.engine.qualification import PlanningScenarioCatalog, _planning_cell, default_role_configuration
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import FakeCodexRuntime
from tests.test_engine_goal_feedback import _conflict_review, _proposal, _ratings, _revised_proposal
from tests.test_engine_qualification import ROOT, qualification_inventory


class PlanningGoalLineageTests(unittest.TestCase):
    def test_feedback_preserves_original_revision_before_registering_result(self):
        catalog = PlanningScenarioCatalog.load(ROOT / 'tests/fixtures/engine/planning-scenarios.json')
        scenario = catalog.scenarios[0]
        inventory = qualification_inventory()
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000_000, call_reservation_tokens=100_000),
            role_timeouts=RoleTimeoutPolicy(),
        )
        for result in ('ready', 'conflict', 'disputed'):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary) / 'cell'
                refinement = {
                    'action': 'disputed' if result == 'disputed' else 'revision',
                    'rationale': '원문과 원본 검토를 대조했다.',
                    'evidence_refs': ['source:user_request', 'artifact:goal_review'],
                    'proposal': None if result == 'disputed' else GoalNormalizationProposal.model_validate(_revised_proposal()).model_dump(mode='json'),
                }
                reviews = [_conflict_review()]
                if result != 'disputed':
                    reviews.append({'findings': [], 'ratings': _ratings()} if result == 'ready' else _conflict_review())
                class RecordingRunner(ScriptedStructuredRoleRunner):
                    def run(self, request, *, validator=None):
                        result = super().run(request, validator=validator)
                        self.receipts.append(result.receipt)
                        return result

                runner = RecordingRunner({
                    'goal_normalizer': [_proposal()],
                    'goal_reviewer': reviews,
                    'goal_refiner': [refinement],
                })
                runner.receipts = []
                class PlanningReached(Exception):
                    pass
                with (
                    patch('flowmarshal.engine.qualification.budgeted_role_runner', return_value=runner),
                    patch('flowmarshal.engine.qualification.SkeletonFirstPlanner.search', side_effect=PlanningReached) as search,
                ):
                    arguments = dict(scenario=scenario, seed=17,
                        fixture_root=ROOT / 'tests/fixtures/engine/live-smoke-project',
                        runtime=FakeCodexRuntime(inventory), inventory=inventory,
                        roles=default_role_configuration(ROOT), work_root=work,
                        evaluation_policies=policies)
                    if result == 'ready':
                        with self.assertRaises(PlanningReached):
                            _planning_cell(**arguments)
                        self.assertEqual(10, search.call_args.kwargs['budget'].max_logical_role_calls)
                    else:
                        cell, receipts = _planning_cell(**arguments)
                        self.assertFalse(cell['selected'])
                        self.assertEqual('conflict', cell['goal_status'])
                        self.assertEqual(3 if result == 'disputed' else 4, cell['logical_role_calls'])
                        search.assert_not_called()
                with closing(sqlite3.connect(work / 'budget-state/flowmarshal-engine.sqlite3')) as connection:
                    rows = connection.execute('SELECT id, revision_no, supersedes_id, status, payload_json FROM goal_revisions ORDER BY revision_no').fetchall()
                    active = connection.execute('SELECT active_goal_revision_id FROM projects').fetchone()[0]
                self.assertEqual([1] if result == 'disputed' else [1, 2], [row[1] for row in rows])
                self.assertEqual('conflict', rows[0][3])
                self.assertIsNone(rows[0][2])
                if result != 'disputed':
                    self.assertEqual(rows[0][0], rows[1][2])
                    self.assertEqual(json.loads(rows[0][4])['goal_id'], json.loads(rows[1][4])['goal_id'])
                self.assertEqual(rows[-1][0] if result == 'ready' else None, active)


if __name__ == '__main__':
    unittest.main()
