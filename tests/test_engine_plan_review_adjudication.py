from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    FindingSeverity,
    GateName,
    ReviewFinding,
    ReviewerSubmission,
    derive_candidate_decision,
)
from flowmarshal.engine.planning import ExpandedPlanEvaluation, plan_review_evidence_catalog
from flowmarshal.engine.plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
from flowmarshal.engine.plan_review_adjudication import (
    PlanReviewAdjudicationError,
    adjudicate_plan_review,
)
from flowmarshal.engine.planning_feedback import PlanRefinementProposal
from flowmarshal.engine.planner_roles import inspection_source_catalog
from flowmarshal.engine.roles import RoleCallResult, ScriptedStructuredRoleRunner
from tests.engine_helpers import skeleton
from tests.test_engine_plan_inspection import inputs


RATINGS = {
    "goal_fit": 4,
    "grounding": 4,
    "engineering": 4,
    "verification": 4,
    "execution_safety": 4,
}


def adjudication_fixture():
    plan, goal, state, project_map = inputs("clean")
    finding = ReviewFinding(
        finding_code="REVIEW_DISPUTE_TARGET",
        gate=GateName.VERIFICATION,
        severity=FindingSeverity.ERROR,
        summary="AC 검사 연결을 다시 확인해야 합니다.",
        evidence_refs=("source:goal", "artifact:plan_contract"),
        affected_task_refs=(plan.definition.tasks[0].task_ref,),
        remediable=True,
    )
    catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
    submission = ReviewerSubmission(
        reviewer_role="compact_plan_reviewer",
        candidate_digest=plan.activation_digest,
        findings=(finding,),
        ratings=None,
        evidence_catalog_digest=sha256_digest(catalog),
    )
    evaluation = ExpandedPlanEvaluation(
        plan=plan,
        semantic_submissions=(submission,),
        decision=derive_candidate_decision(
            candidate_digest=plan.activation_digest,
            findings=(finding,),
            ratings=None,
        ),
    )
    proposal = PlanRefinementProposal(
        action="disputed",
        rationale="Goal 원문은 해당 Task phase 검사를 요구하지 않습니다.",
        evidence_refs=("source:goal", "artifact:plan_contract"),
    )
    return plan, goal, state, project_map, finding, catalog, submission, evaluation, proposal


def adapter_for(response):
    class UsageAwareScriptedRunner(ScriptedStructuredRoleRunner):
        def run(self, request, *, validator=None):
            result = super().run(request, validator=validator)
            return RoleCallResult(
                payload=result.payload,
                receipt=result.receipt.model_copy(update={
                    "thread_id": "thread_adjudication_test",
                    "turn_ids": ("turn_adjudication_test",),
                    "usage_available": True,
                }),
            )

    runner = UsageAwareScriptedRunner({"compact_plan_reviewer": [response]})
    return SimpleNamespace(
        runner=runner,
        model="reviewer-model",
        effort="high",
        inventory_digest="sha256:" + "0" * 64,
        cwd=".",
        inventory=None,
        allowed_fallbacks=(),
        critical_model=None,
        critical_effort=None,
        critical_allowed_fallbacks=(),
        inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2,
        receipts=[],
    )


class PlanReviewAdjudicationTests(unittest.TestCase):
    def test_withdrawal_runs_bound_role_and_produces_clean_core_submission(self):
        plan, goal, state, project_map, finding, catalog, original, evaluation, proposal = (
            adjudication_fixture()
        )
        finding_digest = sha256_digest(finding)
        response = {
            "dispositions": [{
                "original_finding_digest": finding_digest,
                "disposition": "withdrawn",
                "rationale": "AC는 Goal phase 검사만 명시합니다.",
                "evidence_refs": ["source:goal", "artifact:plan_contract"],
            }],
            "additional_findings": [],
            "ratings": RATINGS,
        }
        adapter = adapter_for(response)

        result = adjudicate_plan_review(
            adapter=adapter,
            evaluation=evaluation,
            proposal=proposal,
            goal=goal,
            state=state,
            project_map=project_map,
        )

        self.assertEqual((), result.submission.findings)
        self.assertIsNotNone(result.submission.ratings)
        self.assertEqual(plan.activation_digest, result.submission.candidate_digest)
        self.assertEqual(original.reviewer_role, result.submission.reviewer_role)
        self.assertEqual(sha256_digest(catalog), result.submission.evidence_catalog_digest)
        self.assertEqual(1, len(adapter.runner.calls))
        request = adapter.runner.calls[0]
        self.assertEqual("compact_plan_reviewer", request.role)
        self.assertEqual(sha256_digest(evaluation), request.payload["source_evaluation_digest"])
        self.assertNotIn("immutable_plan", request.payload)
        self.assertNotIn("goal", request.payload)
        self.assertNotIn("state", request.payload)
        self.assertNotIn("project_map", request.payload)
        self.assertNotIn("dispute_evidence_catalog", request.payload)
        expected_sources = inspection_source_catalog(project_map, {
            key: f"payload.evidence_catalog.{key}" for key in catalog
        })
        self.assertEqual(expected_sources, request.payload["inspection_source_catalog"])
        self.assertEqual(
            sha256_digest(expected_sources),
            request.payload["immutable_input_digests"][
                "inspection_source_catalog_digest"
            ],
        )
        self.assertEqual(
            sha256_digest(catalog),
            request.payload["immutable_input_digests"]["evidence_catalog_digest"],
        )
        self.assertEqual(
            sha256_digest(original), request.payload["original_submission_digest"]
        )
        registered_sources = [
            value for key, value in expected_sources.items() if key.startswith("project:")
        ]
        self.assertTrue(registered_sources)
        self.assertTrue(any(value.get("content") for value in registered_sources))
        self.assertEqual([finding_digest], request.payload["original_finding_digests"])
        self.assertIn("필수 AC 검사 연결의 양성 기준은 하나다", request.instructions)
        self.assertEqual(
            1, request.instructions.count("필수 AC 검사 연결의 양성 기준은 하나다")
        )
        self.assertIn("State·ProjectMap의 revision, digest, root, freshness", request.instructions)
        self.assertIn("검사 수단과 검사 주장은 다음 순서로 대조한다", request.instructions)
        self.assertIn("Task 실행 순서는 Worker의 작업·응답 제출", request.instructions)
        self.assertNotIn("citations에 원문 근거를 한 번 등록", request.instructions)
        self.assertEqual(
            {"dispositions", "additional_findings", "ratings"},
            set(request.output_schema["properties"]),
        )
        self.assertEqual(request.request_digest, result.request_digest)
        self.assertEqual(sha256_digest(response), result.output_digest)
        self.assertEqual([result.receipt], adapter.receipts)
        result.validate_source(
            sha256_digest(evaluation),
            original,
            catalog,
            {task.task_ref for task in plan.definition.tasks},
        )

    def test_upheld_finding_is_preserved_as_exact_original_object(self):
        plan, goal, state, project_map, finding, _catalog, _original, evaluation, proposal = (
            adjudication_fixture()
        )
        response = {
            "dispositions": [{
                "original_finding_digest": sha256_digest(finding),
                "disposition": "upheld",
                "rationale": "반박이 원 finding을 뒤집지 못합니다.",
                "evidence_refs": ["source:goal"],
            }],
            "additional_findings": [],
            "ratings": None,
        }

        result = adjudicate_plan_review(
            adapter=adapter_for(response), evaluation=evaluation, proposal=proposal,
            goal=goal, state=state, project_map=project_map,
        )

        self.assertEqual((finding,), result.submission.findings)
        self.assertEqual(sha256_digest(finding), sha256_digest(result.submission.findings[0]))

    def test_unknown_duplicate_and_reincluded_findings_are_rejected(self):
        _plan, goal, state, project_map, finding, _catalog, _original, evaluation, proposal = (
            adjudication_fixture()
        )
        digest = sha256_digest(finding)
        baseline = {
            "dispositions": [{
                "original_finding_digest": digest,
                "disposition": "withdrawn",
                "rationale": "반박을 수용합니다.",
                "evidence_refs": ["source:goal"],
            }],
            "additional_findings": [],
            "ratings": RATINGS,
        }
        cases = []
        unknown_digest = deepcopy(baseline)
        unknown_digest["dispositions"][0]["original_finding_digest"] = "sha256:" + "f" * 64
        cases.append(unknown_digest)
        duplicate = deepcopy(baseline)
        duplicate["dispositions"].append(deepcopy(duplicate["dispositions"][0]))
        cases.append(duplicate)
        unknown_ref = deepcopy(baseline)
        unknown_ref["dispositions"][0]["evidence_refs"] = ["source:unknown"]
        cases.append(unknown_ref)
        reincluded = deepcopy(baseline)
        reincluded["additional_findings"] = [finding.model_dump(mode="json")]
        reincluded["ratings"] = None
        cases.append(reincluded)

        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises((ValueError, PlanReviewAdjudicationError)):
                    adjudicate_plan_review(
                        adapter=adapter_for(response), evaluation=evaluation, proposal=proposal,
                        goal=goal, state=state, project_map=project_map,
                    )

    def test_wrong_candidate_catalog_and_deterministic_dispute_are_rejected(self):
        plan, goal, state, project_map, finding, catalog, original, evaluation, proposal = (
            adjudication_fixture()
        )
        response = {
            "dispositions": [{
                "original_finding_digest": sha256_digest(finding),
                "disposition": "withdrawn",
                "rationale": "반박을 수용합니다.",
                "evidence_refs": ["source:goal"],
            }],
            "additional_findings": [],
            "ratings": RATINGS,
        }
        result = adjudicate_plan_review(
            adapter=adapter_for(response), evaluation=evaluation, proposal=proposal,
            goal=goal, state=state, project_map=project_map,
        )
        wrong_candidate = result.model_copy(update={
            "submission": result.submission.model_copy(
                update={"candidate_digest": "sha256:" + "f" * 64}
            )
        })
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "immutable Plan"):
            wrong_candidate.validate_source(
                sha256_digest(evaluation), original, catalog,
                {task.task_ref for task in plan.definition.tasks},
            )
        wrong_catalog = result.model_copy(update={
            "submission": result.submission.model_copy(
                update={"evidence_catalog_digest": "sha256:" + "f" * 64}
            )
        })
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "catalog digest"):
            wrong_catalog.validate_source(
                sha256_digest(evaluation), original, catalog,
                {task.task_ref for task in plan.definition.tasks},
            )

        deterministic = evaluation.model_copy(update={"deterministic_findings": (finding,)})
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "deterministic"):
            adjudicate_plan_review(
                adapter=adapter_for(response), evaluation=deterministic, proposal=proposal,
                goal=goal, state=state, project_map=project_map,
            )

        bad_original = original.model_copy(
            update={"evidence_catalog_digest": "sha256:" + "e" * 64}
        )
        bad_evaluation = evaluation.model_copy(
            update={"semantic_submissions": (bad_original,)}
        )
        preflight_adapter = adapter_for(response)
        with self.assertRaisesRegex(ValueError, "catalog digest"):
            adjudicate_plan_review(
                adapter=preflight_adapter,
                evaluation=bad_evaluation,
                proposal=proposal,
                goal=goal,
                state=state,
                project_map=project_map,
            )
        self.assertEqual([], preflight_adapter.runner.calls)

        unresolved = PlanRefinementProposal(
            action="unresolved",
            rationale="반박에 필요한 근거가 없습니다.",
            evidence_refs=("source:goal",),
        )
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "disputed"):
            adjudicate_plan_review(
                adapter=adapter_for(response), evaluation=evaluation, proposal=unresolved,
                goal=goal, state=state, project_map=project_map,
            )

        v1_adapter = adapter_for(response)
        v1_adapter.inspection_provider_contract = "plan-inspection-v1"
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "v2"):
            adjudicate_plan_review(
                adapter=v1_adapter, evaluation=evaluation, proposal=proposal,
                goal=goal, state=state, project_map=project_map,
            )

    def test_skeleton_evidence_is_limited_to_dispute_and_bound_to_plan_source(self):
        plan, goal, state, project_map, finding, _catalog, _original, _evaluation, _proposal = (
            adjudication_fixture()
        )
        candidate = skeleton(goal, state)
        definition = plan.definition.model_copy(
            update={"source_skeleton_digest": sha256_digest(candidate)}
        )
        plan = plan.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        catalog = plan_review_evidence_catalog(plan, goal, state, project_map)
        original = ReviewerSubmission(
            reviewer_role="compact_plan_reviewer",
            candidate_digest=plan.activation_digest,
            findings=(finding,),
            evidence_catalog_digest=sha256_digest(catalog),
        )
        evaluation = ExpandedPlanEvaluation(
            plan=plan,
            semantic_submissions=(original,),
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest,
                findings=(finding,),
                ratings=None,
            ),
        )
        proposal = PlanRefinementProposal(
            action="disputed",
            rationale="원 Skeleton 책임과 비교하면 원 finding의 전제가 성립하지 않습니다.",
            evidence_refs=("artifact:plan_contract", "artifact:skeleton"),
        )
        response = {
            "dispositions": [{
                "original_finding_digest": sha256_digest(finding),
                "disposition": "withdrawn",
                "rationale": "원 artifact를 함께 대조했습니다.",
                "evidence_refs": ["artifact:plan_contract"],
            }],
            "additional_findings": [],
            "ratings": RATINGS,
        }
        adapter = adapter_for(response)

        result = adjudicate_plan_review(
            adapter=adapter,
            evaluation=evaluation,
            proposal=proposal,
            goal=goal,
            state=state,
            project_map=project_map,
            candidate=candidate,
        )

        request = adapter.runner.calls[0]
        self.assertNotIn("artifact:skeleton", request.payload["evidence_catalog"])
        self.assertEqual(
            candidate.model_dump(mode="json"),
            request.payload["extra_evidence_catalog"]["artifact:skeleton"],
        )
        dispute_catalog = (
            request.payload["evidence_catalog"]
            | request.payload["extra_evidence_catalog"]
        )
        result.validate_source(
            sha256_digest(evaluation),
            original,
            catalog,
            {task.task_ref for task in plan.definition.tasks},
            dispute_catalog,
        )
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "제공되지 않은 evidence"):
            adjudicate_plan_review(
                adapter=adapter_for(response),
                evaluation=evaluation,
                proposal=proposal,
                goal=goal,
                state=state,
                project_map=project_map,
            )
        wrong_candidate = candidate.model_copy(
            update={"candidate_id": "candidate_" + "f" * 32}
        )
        with self.assertRaisesRegex(PlanReviewAdjudicationError, "Skeleton"):
            adjudicate_plan_review(
                adapter=adapter_for(response),
                evaluation=evaluation,
                proposal=proposal,
                goal=goal,
                state=state,
                project_map=project_map,
                candidate=wrong_candidate,
            )

    def test_receipt_must_be_a_successful_policy_bound_turn(self):
        plan, goal, state, project_map, finding, _catalog, _original, evaluation, proposal = (
            adjudication_fixture()
        )
        response = {
            "dispositions": [{
                "original_finding_digest": sha256_digest(finding),
                "disposition": "withdrawn",
                "rationale": "원문 근거가 원 finding을 반박합니다.",
                "evidence_refs": ["source:goal"],
            }],
            "additional_findings": [],
            "ratings": RATINGS,
        }
        result = adjudicate_plan_review(
            adapter=adapter_for(response), evaluation=evaluation, proposal=proposal,
            goal=goal, state=state, project_map=project_map,
        )
        for field, value in (
            ("status", "failed"),
            ("permission_profile", "read-only"),
            ("approval_policy", "on-request"),
            ("thread_id", None),
            ("turn_ids", ()),
            ("turn_ids", ("",)),
            ("schema_recovery_attempts", 1),
        ):
            with self.subTest(field=field):
                payload = result.model_dump(mode="json")
                payload["receipt"][field] = value
                with self.assertRaisesRegex(ValueError, "독립 turn"):
                    type(result).model_validate(payload)


if __name__ == "__main__":
    unittest.main()
