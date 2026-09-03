from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.planning.r31_domain import (
    ModelCallReceipt,
    ModelCallStatus,
    PlanningRole,
)
from flowmarshal.planning.r31_eval_campaign import (
    CampaignStatus,
    run_live_evaluation_campaign,
)
from flowmarshal.planning.r31_eval_runner import (
    FixtureAssessmentTrace,
    LiveEvaluationResult,
)
from flowmarshal.planning.r31_evaluation import (
    EvaluationObservation,
    EvaluationObservationBatch,
    EvaluationScope,
    build_builtin_fixtures,
    evaluate_repeated_runs,
)
from flowmarshal.planning.r31_models import (
    ExecutionPolicyEvidence,
    ResolvedPlanningModel,
)


_DIGEST = sha256_bytes(b"campaign-test")
_CONFIGURATION_DIGEST = sha256_bytes(b"campaign-configuration")
_CONTRACT_DIGEST = sha256_bytes(b"campaign-contract")


def _fixture():
    return next(
        item for item in build_builtin_fixtures() if item.case_id == "M01-clean"
    )


def _observation() -> EvaluationObservation:
    return EvaluationObservation(
        case_id="M01-clean",
        observed_mission="feature_extension",
        matched_requirement_ids=("req.export", "req.compat"),
        matched_exclusion_ids=("exc.deploy",),
        mission_resolved=True,
        admitted=True,
        stale_or_digest_mismatch_detected=False,
    )


def _resolved_models() -> tuple[ResolvedPlanningModel, ...]:
    return (
        ResolvedPlanningModel(
            role=PlanningRole.PURPOSE_RESOLVER,
            model_id="test-model",
            reasoning_effort="medium",
            inventory_digest=_DIGEST,
        ),
    )


def _successful_result(
    fixture,
    seed: int,
    call_number: int,
    model_input,
) -> LiveEvaluationResult:
    observation = _observation()
    batch = EvaluationObservationBatch(
        run_id=f"live-role-eval-{seed}",
        order_seed=seed,
        observations=(observation,),
    )
    thread_id = f"thread-{call_number}"
    receipt = ModelCallReceipt(
        call_id=f"model-call-{call_number}",
        role=PlanningRole.PURPOSE_RESOLVER,
        model_id="test-model",
        reasoning_effort="medium",
        inventory_digest=_DIGEST,
        input_digest=sha256_digest({"seed": seed}),
        output_schema_digest=_DIGEST,
        output_digest=sha256_digest(observation),
        status=ModelCallStatus.SUCCEEDED,
        thread_id=thread_id,
        turn_ids=(f"turn-{call_number}",),
        token_count=10,
        latency_ms=20,
    )
    evidence = ExecutionPolicyEvidence(
        config_digest=_DIGEST,
        profile_catalog_digest=_DIGEST,
        thread_id=thread_id,
    )
    return LiveEvaluationResult(
        configuration_id="test-configuration",
        configuration_digest=_CONFIGURATION_DIGEST,
        fixture_ids=(fixture.case_id,),
        batches=(batch,),
        report=evaluate_repeated_runs(
            (fixture,),
            (batch,),
            evaluation_scope=EvaluationScope.ROLE_FIXTURE_PROBE,
        ),
        model_call_receipts=(receipt,),
        execution_policy_evidence=(evidence,),
        resolved_models=_resolved_models(),
        assessment_traces=(
            FixtureAssessmentTrace(
                case_id=fixture.case_id,
                model_case_ref=fixture.model_case_ref,
                model_input_digest=sha256_digest(model_input),
                suite=fixture.suite.value,
                proposal={"status": "proposal"},
                reviewed_assessment={"status": "reviewed"},
            ),
        ),
    )


def _rate_limited_result(fixture, seed: int, model_input) -> LiveEvaluationResult:
    observation = EvaluationObservation(
        case_id=fixture.case_id,
        blocked=True,
        mission_resolved=False,
        admitted=False,
        first_pass_schema_valid=False,
        forward_reconstruction_complete=False,
    )
    batch = EvaluationObservationBatch(
        run_id=f"live-role-eval-{seed}",
        order_seed=seed,
        observations=(observation,),
    )
    receipt = ModelCallReceipt(
        call_id="model-call-rate-limit",
        role=PlanningRole.PURPOSE_RESOLVER,
        model_id="test-model",
        reasoning_effort="medium",
        inventory_digest=_DIGEST,
        input_digest=sha256_digest({"seed": seed}),
        output_schema_digest=_DIGEST,
        status=ModelCallStatus.FAILED,
        error_summary="You have hit your usage limit.",
    )
    return LiveEvaluationResult(
        configuration_id="test-configuration",
        configuration_digest=_CONFIGURATION_DIGEST,
        fixture_ids=(fixture.case_id,),
        batches=(batch,),
        report=evaluate_repeated_runs(
            (fixture,),
            (batch,),
            evaluation_scope=EvaluationScope.ROLE_FIXTURE_PROBE,
        ),
        model_call_receipts=(receipt,),
        execution_policy_evidence=(),
        resolved_models=_resolved_models(),
        assessment_traces=(
            FixtureAssessmentTrace(
                case_id=fixture.case_id,
                model_case_ref=fixture.model_case_ref,
                model_input_digest=sha256_digest(model_input),
                suite=fixture.suite.value,
                error_summary="You have hit your usage limit.",
            ),
        ),
    )


class PlannerR31EvalCampaignTests(unittest.TestCase):
    def test_campaign_checkpoints_each_cell_and_resumes_without_repeating_it(self):
        fixture = _fixture()
        calls: list[int] = []

        def execute_cell(selected, seed, model_input, cell_root):
            del cell_root
            calls.append(seed)
            return _successful_result(selected, seed, len(calls), model_input)

        with tempfile.TemporaryDirectory() as directory:
            first = run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1, 2),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
                max_new_cells=1,
            )
            self.assertEqual(CampaignStatus.PARTIAL, first.status)
            self.assertEqual(1, first.completed_cell_count)
            self.assertEqual(1, first.remaining_cell_count)
            cell_path = (
                Path(directory)
                / "cells"
                / "seed-1"
                / fixture.model_case_ref
                / "result.json"
            )
            self.assertIn('"assessment_trace":', cell_path.read_text(encoding="utf-8"))

            completed = run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1, 2),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
                resume=True,
            )

            self.assertEqual(CampaignStatus.PASS, completed.status)
            self.assertEqual(2, completed.completed_cell_count)
            self.assertEqual(0, completed.remaining_cell_count)
            self.assertEqual([1, 2], calls)
            self.assertTrue((Path(directory) / "campaign-manifest.json").is_file())
            self.assertTrue((Path(directory) / "model-lock.json").is_file())

    def test_changed_evaluation_contract_cannot_be_mixed_on_resume(self):
        fixture = _fixture()

        def execute_cell(selected, seed, model_input, cell_root):
            del cell_root
            return _successful_result(selected, seed, seed, model_input)

        with tempfile.TemporaryDirectory() as directory:
            run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1, 2),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
                max_new_cells=1,
            )
            with self.assertRaisesRegex(RuntimeError, "campaign artifact"):
                run_live_evaluation_campaign(
                    (fixture,),
                    order_seeds=(1, 2),
                    artifact_root=directory,
                    configuration_id="test-configuration",
                    configuration_digest=_CONFIGURATION_DIGEST,
                    evaluation_contract_digest=sha256_bytes(b"changed-contract"),
                    execute_cell=execute_cell,
                    resume=True,
                )

    def test_existing_campaign_requires_explicit_resume(self):
        fixture = _fixture()

        def execute_cell(selected, seed, model_input, cell_root):
            del cell_root
            return _successful_result(selected, seed, 1, model_input)

        with tempfile.TemporaryDirectory() as directory:
            run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1,),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
            )
            with self.assertRaisesRegex(RuntimeError, "--resume"):
                run_live_evaluation_campaign(
                    (fixture,),
                    order_seeds=(1,),
                    artifact_root=directory,
                    configuration_id="test-configuration",
                    configuration_digest=_CONFIGURATION_DIGEST,
                    evaluation_contract_digest=_CONTRACT_DIGEST,
                    execute_cell=execute_cell,
                )

    def test_rate_limit_attempt_is_preserved_but_cell_remains_resumable(self):
        fixture = _fixture()
        attempts = 0

        def execute_cell(selected, seed, model_input, cell_root):
            nonlocal attempts
            del cell_root
            attempts += 1
            if attempts == 1:
                return _rate_limited_result(selected, seed, model_input)
            return _successful_result(selected, seed, attempts, model_input)

        with tempfile.TemporaryDirectory() as directory:
            paused = run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1,),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
            )
            self.assertEqual(CampaignStatus.PAUSED_RATE_LIMIT, paused.status)
            self.assertEqual(0, paused.completed_cell_count)
            self.assertIsNotNone(paused.attempt_receipt_path)
            self.assertTrue(Path(paused.attempt_receipt_path).is_file())

            resumed = run_live_evaluation_campaign(
                (fixture,),
                order_seeds=(1,),
                artifact_root=directory,
                configuration_id="test-configuration",
                configuration_digest=_CONFIGURATION_DIGEST,
                evaluation_contract_digest=_CONTRACT_DIGEST,
                execute_cell=execute_cell,
                resume=True,
            )
            self.assertEqual(CampaignStatus.PASS, resumed.status)
            self.assertEqual(1, resumed.completed_cell_count)
            self.assertEqual(2, attempts)


if __name__ == "__main__":
    unittest.main()
