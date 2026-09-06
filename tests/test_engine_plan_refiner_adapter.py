from __future__ import annotations

from copy import deepcopy
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import derive_candidate_decision
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter, PlanExpansionDraft, PlannerRoleAdapterError, RuleBasedTaskAssigner,
)
from flowmarshal.engine.planning import ExpandedPlanEvaluation
from flowmarshal.engine.planning_feedback import PlanRefinementProposal, validate_plan_revision
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner, StructuredRoleError
from tests.engine_helpers import assignment, skeleton
from tests import test_engine_role_adapters as role_fixtures
from tests.test_engine_planning_feedback import FindingThenCleanPlanReviewer


class PlanRefinerAdapterTests(unittest.TestCase):
    setUp = role_fixtures.EngineRoleAdapterTests.setUp
    tearDown = role_fixtures.EngineRoleAdapterTests.tearDown

    def _adapter(self, response, runner_type=ScriptedStructuredRoleRunner):
        runner = runner_type({"plan_refiner": [response]})
        adapter = PlanExpanderAdapter(
            runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
            model="worker", effort="medium", inventory_digest=self.inventory.inventory_digest,
            cwd=self.root, inventory=self.inventory,
        )
        candidate = skeleton(self.goal, self.state)
        plan = adapter._compile(
            PlanExpansionDraft.model_validate(role_fixtures._plan_response()), candidate=candidate,
            goal=self.goal, state=self.state, project_map=self.map, planning_budget=adapter.planning_budget,
        )
        submission = FindingThenCleanPlanReviewer().review(
            plan=plan, goal=self.goal, state=self.state, project_map=self.map,
        )
        evaluation = ExpandedPlanEvaluation(
            plan=plan, semantic_submissions=(submission,),
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest, findings=submission.findings, ratings=None,
            ),
        )
        return adapter, runner, evaluation, candidate

    def _refine(self, adapter, evaluation, candidate, *, allow_skeleton=True):
        return adapter.refine(
            evaluation=evaluation, candidate=candidate, goal=self.goal, state=self.state,
            project_map=self.map, planning_budget=adapter.planning_budget,
            allow_skeleton_revision=allow_skeleton,
        )

    @staticmethod
    def _proposal():
        plan = role_fixtures._plan_response()
        plan["tasks"][0]["validations"][0]["statement"] = "기존 테스트를 실행하고 실제 결과와 기대값을 대조한다."
        return {"action": "detail_revision", "rationale": "원본 검사 문장에 빠진 실제 결과 대조를 보존한다.",
                "evidence_refs": ["source:goal", "artifact:plan_contract"], "plan": plan, "skeleton": None}

    def test_refiner_binds_failure_and_compiles_new_revision_with_stable_task_refs(self):
        adapter, runner, evaluation, candidate = self._adapter(self._proposal())
        proposal = self._refine(adapter, evaluation, candidate)
        request = runner.calls[0]
        self.assertEqual("plan_refiner", request.role)
        self.assertEqual(sha256_digest(evaluation), request.payload["source_evaluation_digest"])
        self.assertEqual(evaluation.plan.model_dump(mode="json"),
                         request.payload["evidence_catalog"]["artifact:plan_contract"])
        self.assertEqual(evaluation.semantic_submissions[0].findings[0].model_dump(mode="json"),
                         request.payload["findings"][0])
        self.assertEqual(evaluation.plan.plan_id, proposal.plan.plan_id)
        self.assertEqual(2, proposal.plan.revision_no)
        self.assertEqual(evaluation.plan.plan_revision_id, proposal.plan.supersedes_plan_revision_id)
        self.assertEqual(evaluation.plan.definition.tasks[0].task_ref, proposal.plan.definition.tasks[0].task_ref)
        self.assertNotEqual(evaluation.plan.definition.tasks[0].task_id, proposal.plan.definition.tasks[0].task_id)
        self.assertEqual(1, len(adapter.receipts))
        self.assertEqual(request.request_digest, proposal.provenance.request_digest)
        self.assertEqual(sha256_digest(adapter.receipts[0]), proposal.provenance.receipt_digest)
        altered = proposal.model_dump(mode="json")
        altered["rationale"] = "생성 뒤 바뀐 근거"
        with self.assertRaisesRegex(ValueError, "생성 관측의 digest"):
            PlanRefinementProposal.model_validate(altered)

    def test_refinement_cannot_replace_locked_model_inventory(self):
        adapter, _, evaluation, candidate = self._adapter(self._proposal())
        proposal = self._refine(adapter, evaluation, candidate)
        definition = proposal.plan.definition.model_copy(update={"model_inventory_digest": "sha256:" + "9" * 64})
        altered = proposal.plan.model_copy(update={"definition": definition, "definition_digest": definition.definition_digest})
        with self.assertRaisesRegex(ValueError, "model_inventory_digest"):
            validate_plan_revision(evaluation.plan, altered)

    def test_detail_revision_cannot_change_skeleton_objective(self):
        response = self._proposal()
        response["plan"]["tasks"][0]["objective"] = "Goal 대신 다른 작업을 한다."
        adapter, _, evaluation, candidate = self._adapter(response)
        with self.assertRaisesRegex(PlannerRoleAdapterError, "Skeleton 의미"):
            self._refine(adapter, evaluation, candidate)
        self.assertEqual([], adapter.receipts)

    def test_unknown_counterevidence_is_rejected_before_a_dispute_is_recorded(self):
        response = {"action": "disputed", "rationale": "원문과 지적이 충돌한다.",
                    "evidence_refs": ["source:invented"], "plan": None, "skeleton": None}
        adapter, _, evaluation, candidate = self._adapter(response)
        with self.assertRaisesRegex(PlannerRoleAdapterError, "제공되지 않은 evidence"):
            self._refine(adapter, evaluation, candidate)

    def test_receipt_mismatch_cannot_publish_cached_refinement(self):
        class TamperingRunner(ScriptedStructuredRoleRunner):
            def run(self, request, *, validator=None):
                result = super().run(request, validator=validator)
                payload = deepcopy(result.payload)
                payload["rationale"] = "원본 응답 뒤 바뀐 설명"
                return result.model_copy(update={"payload": payload})

        adapter, _, evaluation, candidate = self._adapter(self._proposal(), TamperingRunner)
        with self.assertRaisesRegex(StructuredRoleError, "ROLE_RECEIPT_BINDING_MISMATCH"):
            self._refine(adapter, evaluation, candidate)
        self.assertEqual([], adapter.receipts)


if __name__ == "__main__":
    unittest.main()
