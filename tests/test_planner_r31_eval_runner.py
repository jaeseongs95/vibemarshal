from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.planning.r31_domain import (
    ModelCallReceipt,
    ModelCallStatus,
    PlanningRole,
)
from flowmarshal.planning.r31_eval_runner import (
    EvaluationRoleInstructions,
    PlanFixtureAssessment,
    RoleFixtureEvaluator,
    evaluation_contract_digest,
)
from flowmarshal.planning.r31_evaluation import (
    EvaluationScope,
    build_builtin_fixtures,
    evaluate_fixture_observations,
)
from flowmarshal.planning.r31_models import (
    ResolvedPlanningModel,
    StructuredRoleResult,
)


class _FixtureRunner:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request, *, validator=None):
        self.requests.append(request)
        fixture = request.payload["fixture"]
        if fixture["suite"] == "mission":
            clauses = fixture["input"]["source_clauses"]
            dispositions = {
                "exc.deploy": "exclusion",
            }
            payload = {
                "observed_mission": "feature_extension",
                "clause_assessments": [
                    {
                        "clause_id": item["clause_id"],
                        "disposition": dispositions.get(
                            item["clause_id"], "requirement"
                        ),
                        "rationale": "사용자 원문의 명시 clause다.",
                    }
                    for item in clauses
                ],
                "question_asked": False,
                "blocked": False,
                "mission_resolved": True,
                "candidate_generated": False,
                "scored_candidate_count": 0,
                "mixed_mission_ranking": False,
                "request_precedence_ok": True,
                "stale_or_digest_mismatch_detected": False,
                "previous_mission_context_leaked": False,
                "detected_major_defects": [],
                "control_evidence_codes": ["EXPLICIT_REQUEST_PRESERVED"],
            }
        else:
            payload = {
                "status": "needs_revision",
                "analyzed_clause_ids": ["req.plan"],
                "detected_structural_defects": ["DEPENDENCY_MISSING"],
                "detected_major_defects": ["CONTRACT_BREAK"],
                "question_asked": False,
                "scored_candidate_count": 0,
                "hard_fail_candidate_scored": False,
                "criterion_validation_complete": True,
                "consumes_dependencies_complete": False,
                "dependency_cycle_present": False,
                "distinct_candidate_count": 0,
                "duplicate_cluster_in_top2": False,
                "unsupported_validation_success_claimed": False,
                "selected_candidate_ref": None,
                "reconstructed_mission": None,
                "reconstructed_work_item_refs": [],
                "reconstructed_dependency_edges": [],
                "reconstructed_failure_policy_refs": [],
                "reconstructed_validation_refs": [],
                "hidden_context_required": False,
            }
        if validator is not None:
            validator(payload)
        return StructuredRoleResult(
            payload=payload,
            receipt=ModelCallReceipt(
                call_id=f"model_call_{len(self.requests)}",
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=request.inventory_digest,
                input_digest=request.request_digest,
                output_schema_digest=sha256_digest(request.output_schema),
                output_digest=sha256_digest(payload),
                status=ModelCallStatus.SUCCEEDED,
                thread_id=f"thread_{len(self.requests)}",
                turn_ids=(f"turn_{len(self.requests)}",),
                token_count=10,
                latency_ms=20,
            ),
        )


def _models() -> dict[PlanningRole, ResolvedPlanningModel]:
    return {
        role: ResolvedPlanningModel(
            role=role,
            model_id=f"model-{role.value}",
            reasoning_effort="medium",
            inventory_digest=sha256_bytes(b"inventory"),
        )
        for role in (
            PlanningRole.PURPOSE_RESOLVER,
            PlanningRole.INTENT_REVIEWER,
            PlanningRole.HARD_GATE_REVIEWER,
            PlanningRole.CRITICAL_REVIEWER,
        )
    }


class PlannerR31EvalRunnerTests(unittest.TestCase):
    def _evaluator(self, directory: str, runner, receipts, traces=None):
        instructions = EvaluationRoleInstructions(
            purpose_resolver="purpose",
            intent_reviewer="intent",
            hard_gate_reviewer="hard",
            critical_reviewer="critical",
        )
        return RoleFixtureEvaluator(
            runner=runner,
            models=_models(),
            cwd=directory,
            instructions=instructions,
            receipt_sink=receipts.append,
            trace_sink=(traces.append if traces is not None else None),
        )

    def test_mission_fixture_uses_proposer_and_independent_reviewer_without_oracle(self):
        fixture = next(
            item for item in build_builtin_fixtures() if item.case_id == "M01-clean"
        )
        runner = _FixtureRunner()
        receipts = []
        traces = []
        with tempfile.TemporaryDirectory() as directory:
            observation, observed_receipts = self._evaluator(
                directory, runner, receipts, traces
            ).evaluate(fixture, fixture.model_input())

        self.assertEqual(PlanningRole.PURPOSE_RESOLVER, runner.requests[0].role)
        self.assertEqual(PlanningRole.INTENT_REVIEWER, runner.requests[1].role)
        self.assertEqual(2, len(observed_receipts))
        self.assertEqual(1, len(traces))
        self.assertIsNotNone(traces[0].proposal)
        self.assertIsNotNone(traces[0].reviewed_assessment)
        self.assertEqual(("exc.deploy",), observation.matched_exclusion_ids)
        serialized = json.dumps(
            [item.payload for item in runner.requests],
            ensure_ascii=False,
        )
        self.assertNotIn("oracle", serialized)
        self.assertNotIn("M01-clean", serialized)
        self.assertNotIn("adversarial", serialized)

    def test_detected_dependency_mutant_is_not_counted_as_runner_failure(self):
        fixture = next(
            item
            for item in build_builtin_fixtures()
            if item.case_id == "P02-adversarial"
        )
        runner = _FixtureRunner()
        receipts = []
        with tempfile.TemporaryDirectory() as directory:
            observation, _ = self._evaluator(directory, runner, receipts).evaluate(
                fixture,
                fixture.model_input(),
            )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertFalse(observation.admitted)
        self.assertFalse(observation.consumes_dependencies_complete)
        self.assertTrue(report.passed)

    def test_forward_reconstruction_is_computed_from_selected_artifact(self):
        fixture = next(
            item for item in build_builtin_fixtures() if item.case_id == "P12-clean"
        )
        model_input = fixture.model_input()
        selected = model_input["input"]["candidate_set"][0]
        refs = tuple(item["work_item_ref"] for item in selected["work_items"])
        edges = tuple(
            f"{dependency}->{item['work_item_ref']}"
            for item in selected["work_items"]
            for dependency in item["dependencies"]
        )
        assessment = PlanFixtureAssessment(
            status="admissible",
            analyzed_clause_ids=("req.plan",),
            detected_structural_defects=(),
            detected_major_defects=(),
            question_asked=False,
            scored_candidate_count=1,
            hard_fail_candidate_scored=False,
            criterion_validation_complete=True,
            consumes_dependencies_complete=True,
            dependency_cycle_present=False,
            distinct_candidate_count=1,
            duplicate_cluster_in_top2=False,
            unsupported_validation_success_claimed=False,
            selected_candidate_ref=selected["candidate_ref"],
            reconstructed_mission="feature_extension",
            reconstructed_work_item_refs=refs,
            reconstructed_dependency_edges=edges,
            reconstructed_failure_policy_refs=refs,
            reconstructed_validation_refs=refs,
            hidden_context_required=False,
        )
        self.assertTrue(
            RoleFixtureEvaluator._reconstruction_complete(model_input, assessment)
        )

    def test_plan_assessment_rejects_incoherent_hard_fail_score(self):
        values = {
            "status": "needs_revision",
            "analyzed_clause_ids": ("req.plan",),
            "detected_structural_defects": ("OVER_GATED",),
            "detected_major_defects": ("EXECUTION_FRICTION",),
            "question_asked": False,
            "scored_candidate_count": 0,
            "hard_fail_candidate_scored": True,
            "criterion_validation_complete": True,
            "consumes_dependencies_complete": True,
            "dependency_cycle_present": False,
            "distinct_candidate_count": 0,
            "duplicate_cluster_in_top2": False,
            "unsupported_validation_success_claimed": False,
            "selected_candidate_ref": None,
            "reconstructed_mission": None,
            "reconstructed_work_item_refs": (),
            "reconstructed_dependency_edges": (),
            "reconstructed_failure_policy_refs": (),
            "reconstructed_validation_refs": (),
            "hidden_context_required": False,
        }
        with self.assertRaisesRegex(ValueError, "점수화 후보"):
            PlanFixtureAssessment(**values)

    def test_contract_digest_binds_evaluation_scope(self):
        instructions = EvaluationRoleInstructions(
            purpose_resolver="purpose",
            intent_reviewer="intent",
            hard_gate_reviewer="hard",
            critical_reviewer="critical",
        )
        role_probe = evaluation_contract_digest(instructions)
        full_pipeline = evaluation_contract_digest(
            instructions,
            evaluation_scope=EvaluationScope.FULL_PIPELINE,
        )
        self.assertNotEqual(role_probe, full_pipeline)


if __name__ == "__main__":
    unittest.main()
