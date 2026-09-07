from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark_candidate_costs import (
    CandidateOutputRecorder,
    candidate_output_costs,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner
from flowmarshal.engine.planning_recovery import PlanningRecoveryPolicy
from flowmarshal.engine.role_observations import StructuredRoleError
from tests.engine_helpers import goal, inventory, plan, profile, project_map, state, skeleton
from tests.test_engine_candidate_schema_isolation import (
    _CleanSkeletonReviewer,
    _Generator,
    _Reviewer,
    _receipt,
)


class _ObservedExpander:
    def __init__(self, *, project_id, goal_revision, snapshot, map_digest,
                 candidates, fail_second):
        self.project_id = project_id
        self.goal_revision = goal_revision
        self.snapshot = snapshot
        self.map_digest = map_digest
        self.model_inventory = inventory()
        self.candidates = candidates
        self.fail_second = fail_second
        self.receipts = []
        self.calls = 0

    def expand(self, *, candidate, **_kwargs):
        self.calls += 1
        call_id = f"model_call_expand_{self.calls}"
        if self.fail_second and self.calls == 2:
            receipt = _receipt(call_id=call_id)
            self.receipts.append(receipt)
            raise StructuredRoleError(
                "schema failure",
                receipt=receipt,
                settled_provider_call_id="provider_call_expand_2",
            )
        receipt = _receipt(call_id=call_id, status="succeeded").model_copy(update={
            "output_digest": sha256_digest({"output": call_id}),
        })
        self.receipts.append(receipt)
        return plan(
            self.project_id,
            self.goal_revision,
            self.snapshot,
            self.map_digest,
            candidate,
            self.model_inventory,
        )[0]


class FailedCandidateCostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "AGENTS.md").write_text("지침\n", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "7" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(
            self.project_id, self.goal.definition_digest, self.map.revision_digest
        )
        first = skeleton(self.goal, self.state)
        second = first.model_copy(update={
            "candidate_id": "candidate_" + "6" * 32,
            "approach": first.approach.model_copy(
                update={"strategy_family": "independent-alternative"}
            ),
        })
        self.candidates = (first, second)

    def tearDown(self):
        self.temporary.cleanup()

    def raw(self, *, fail_second):
        adapter = _ObservedExpander(
            project_id=self.project_id,
            goal_revision=self.goal,
            snapshot=self.state,
            map_digest=self.map.revision_digest,
            candidates=self.candidates,
            fail_second=fail_second,
        )
        recorder = CandidateOutputRecorder(adapter)
        outcome = SkeletonFirstPlanner(
            generator=_Generator(self.candidates),
            skeleton_reviewer=_CleanSkeletonReviewer(),
            expander=recorder,
            plan_reviewer=_Reviewer(),
        ).search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
            recovery_policy=PlanningRecoveryPolicy(),
        )
        return {
            "receipts": [item.model_dump(mode="json") for item in adapter.receipts],
            "candidate_output_bindings": [
                item.model_dump(mode="json") for item in recorder.bindings
            ],
            "selected_activation_digest": outcome.selected_activation_digest,
            "planning_outcome": outcome.model_dump(mode="json"),
        }

    def test_settled_schema_failure_stays_in_raw_cost_but_not_valid_candidate_cost(self):
        normal = self.raw(fail_second=False)
        failed = self.raw(fail_second=True)

        self.assertEqual((14, 7), candidate_output_costs(normal))
        self.assertEqual((7, 0), candidate_output_costs(failed))
        self.assertEqual(14, sum(item["output_tokens"] for item in failed["receipts"]))
        self.assertEqual(1, len(failed["planning_outcome"]["candidate_schema_failures"]))
        self.assertEqual(1, len(failed["candidate_output_bindings"]))

    def test_missing_or_tampered_failure_receipt_and_outcome_are_rejected(self):
        raw = self.raw(fail_second=True)
        cases = []

        missing = copy.deepcopy(raw)
        missing["planning_outcome"].pop("candidate_schema_failures")
        cases.append(missing)

        tampered_usage = copy.deepcopy(raw)
        tampered_usage["receipts"][1]["output_tokens"] += 1
        cases.append(tampered_usage)

        unknown_call = copy.deepcopy(raw)
        unknown_call["planning_outcome"]["candidate_schema_failures"][0]["receipt"][
            "call_id"
        ] = "model_call_unknown"
        cases.append(unknown_call)

        failed_status = copy.deepcopy(raw)
        failed_status["receipts"][1]["status"] = "failed"
        failed_status["planning_outcome"]["candidate_schema_failures"][0]["receipt"][
            "status"
        ] = "failed"
        cases.append(failed_status)

        forged_operation = copy.deepcopy(raw)
        forged_operation["planning_outcome"]["candidate_schema_failures"][0][
            "operation"
        ] = "review"
        cases.append(forged_operation)

        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    candidate_output_costs(value)


if __name__ == "__main__":
    unittest.main()
