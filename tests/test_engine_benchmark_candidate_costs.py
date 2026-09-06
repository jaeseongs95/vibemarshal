from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark_candidate_costs import (
    CandidateOutputBinding, CandidateOutputRecorder, candidate_output_costs,
)
from flowmarshal.engine.domain import new_id, utc_now
from flowmarshal.engine.planning_feedback import PlanRefinementProposal, PlanRefinementProvenance
from flowmarshal.engine.roles import RoleCallReceipt
from tests.engine_helpers import goal, inventory, plan, profile, skeleton, state


class BenchmarkCandidateCostsTests(unittest.TestCase):
    def setUp(self):
        project_id = new_id("project")
        goal_revision = goal(project_id, profile(project_id).definition_digest)
        map_digest = sha256_digest({"map": "synthetic"})
        snapshot = state(project_id, goal_revision.definition_digest, map_digest)
        self.initial = plan(project_id, goal_revision, snapshot, map_digest,
                            skeleton(goal_revision, snapshot), inventory())[0]
        self.revised = self.initial.model_copy(update={
            "plan_revision_id": new_id("plan_revision"),
            "revision_no": self.initial.revision_no + 1,
            "supersedes_plan_revision_id": self.initial.plan_revision_id,
        })

    def receipt(self, call_id, role, outputs):
        return RoleCallReceipt(
            call_id=call_id, role=role, status="succeeded", model="model-standard", effort="medium",
            inventory_digest=inventory().inventory_digest, permission_profile=":danger-full-access",
            approval_policy="never", thread_id="thread-" + call_id, turn_ids=("turn-" + call_id,),
            input_digest=sha256_digest({"request": call_id}),
            output_digest=sha256_digest({"output": call_id}),
            output_schema_digest=sha256_digest({"schema": role}),
            input_tokens=25, cached_input_tokens=0, output_tokens=outputs,
            reasoning_tokens=0, usage_available=True, latency_ms=20, recorded_at=utc_now(),
        )

    def setup_records(self, action="detail_revision"):
        first = self.receipt("expand-call", "plan_expander", 100)
        second = self.receipt("refine-call", "plan_refiner", 40)
        proposal = PlanRefinementProposal(
            action=action, rationale="직접 근거에 따른 수정", evidence_refs=("source:goal",),
            plan=self.revised if action == "detail_revision" else None,
        )
        proposal = proposal.model_copy(update={"provenance": PlanRefinementProvenance(
            source_evaluation_digest=sha256_digest({"evaluation": "first"}),
            proposal_digest=sha256_digest(proposal.model_dump(mode="json", exclude={"provenance"})),
            call_id=second.call_id, request_digest=second.input_digest,
            output_digest=second.output_digest, receipt_digest=sha256_digest(second),
        )})
        adapter = SimpleNamespace(receipts=[])

        def expand(**_):
            adapter.receipts.append(first)
            return self.initial

        def refine(**_):
            adapter.receipts.append(second)
            return proposal

        adapter.expand, adapter.refine = expand, refine
        recorder = CandidateOutputRecorder(adapter)
        self.assertIs(self.initial, recorder.expand())
        self.assertIs(proposal, recorder.refine())
        evaluated = [self.initial] + ([self.revised] if action == "detail_revision" else [])
        return {
            "receipts": [first.model_dump(mode="json"), second.model_dump(mode="json")],
            "candidate_output_bindings": [value.model_dump(mode="json") for value in recorder.bindings],
            "selected_activation_digest": self.revised.activation_digest if action == "detail_revision" else None,
            "planning_outcome": {
                "plan_evaluations": [{"plan": item.model_dump(mode="json")} for item in evaluated],
                "plan_refinements": [{"proposal": proposal.model_dump(mode="json")}],
            },
        }

    def test_refiner_output_is_counted_and_receipt_order_is_irrelevant(self):
        raw = self.setup_records()
        self.assertEqual((140, 100), candidate_output_costs(raw))
        raw["receipts"].reverse()
        self.assertEqual((140, 100), candidate_output_costs(raw))

    def test_no_candidate_refinement_is_excluded_only_from_candidate_output(self):
        raw = self.setup_records("disputed")
        self.assertEqual((100, 100), candidate_output_costs(raw))
        self.assertEqual(140, sum(item["output_tokens"] for item in raw["receipts"]))

    def test_missing_or_tampered_binding_and_duplicate_receipt_are_rejected(self):
        raw = self.setup_records()
        changes = []
        missing = copy.deepcopy(raw)
        missing["candidate_output_bindings"].pop()
        changes.append(missing)
        duplicate = copy.deepcopy(raw)
        duplicate["receipts"].append(duplicate["receipts"][0])
        changes.append(duplicate)
        tampered = copy.deepcopy(raw)
        tampered["candidate_output_bindings"][1]["plan_activation_digest"] = self.initial.activation_digest
        changes.append(tampered)
        unknown = copy.deepcopy(raw)
        unknown["receipts"][1]["output_tokens"] = None
        changes.append(unknown)
        for value in changes:
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate_output_costs(value)

    def test_actual_zero_output_is_preserved(self):
        receipt = self.receipt("zero-call", "plan_expander", 0)
        binding = CandidateOutputBinding(
            call_id=receipt.call_id, role=receipt.role, receipt_digest=sha256_digest(receipt),
            output_digest=receipt.output_digest, output_kind="detail_plan",
            plan_activation_digest=self.initial.activation_digest,
        )
        raw = {
            "receipts": [receipt.model_dump(mode="json")],
            "candidate_output_bindings": [binding.model_dump(mode="json")],
            "selected_activation_digest": self.initial.activation_digest,
            "planning_outcome": {"plan_evaluations": [{"plan": self.initial.model_dump(mode="json")} ]},
        }
        self.assertEqual((0, 0), candidate_output_costs(raw))


if __name__ == "__main__":
    unittest.main()
