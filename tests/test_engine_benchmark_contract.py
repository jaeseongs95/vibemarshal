from __future__ import annotations

import unittest

from flowmarshal.engine.benchmark import IMPLEMENTATION_RUNTIME_CONTRACT
from flowmarshal.engine.evaluation import (
    BenchmarkCell,
    BenchmarkLifecycleObservation,
    BenchmarkTaskLifecycleObservation,
    evaluate_token_latency_gate,
)


def lifecycle_observation(model_lock: str, neutral: str, plan: str):
    return BenchmarkLifecycleObservation(
        project_id="project_" + "1" * 32,
        plan_revision_id="plan_revision_" + "2" * 32,
        plan_activation_digest=plan,
        model_lock_digest=model_lock,
        neutral_input_digest=neutral,
        tasks=(BenchmarkTaskLifecycleObservation(
            task_id="task_" + "3" * 32,
            execution_spec_revision_id="execution_spec_" + "4" * 32,
            execution_spec_digest="sha256:" + "5" * 64,
            attempt_id="attempt_" + "6" * 32,
            runtime_receipt_digest="sha256:" + "7" * 64,
            validation_result_digests=("sha256:" + "8" * 64,),
        ),),
        integration_validation_result_digests=("sha256:" + "9" * 64,),
        state_before_digest="sha256:" + "a" * 64,
        state_after_digest="sha256:" + "b" * 64,
        state_reobservation_event_digest="sha256:" + "c" * 64,
        goal_verdict_digest="sha256:" + "d" * 64,
        history_head_digest="sha256:" + "e" * 64,
    )


def cell(scenario, implementation, *, expected="selected", actual=None, elapsed=1000):
    disposition = actual or expected
    lifecycle_observed = implementation == "skeleton_engine" and disposition == "selected"
    model_lock = "sha256:" + "b" * 64
    neutral = "sha256:" + "f" * 64
    plan = "sha256:" + "1" * 64
    observation = lifecycle_observation(model_lock, neutral, plan) if lifecycle_observed else None
    return BenchmarkCell(
        scenario_id=scenario,
        scenario_digest="sha256:" + "a" * 64,
        order_seed=17,
        path_kind="single_path" if scenario == "single" else "multi_path",
        implementation=implementation,
        neutral_input_digest=neutral,
        model_lock_digest=model_lock,
        functional_result_digest="sha256:" + "c" * 64,
        runner_receipt_digest="sha256:" + "d" * 64,
        expected_disposition=expected,
        disposition=disposition,
        uncached_input_tokens=1000 if implementation == "r31_baseline" else 500,
        output_tokens=100,
        latency_ms_to_first_feasible=elapsed if disposition == "selected" else None,
        latency_ms_to_disposition=elapsed,
        selected_plan_activation_digest=(plan if implementation == "skeleton_engine" and disposition == "selected" else None),
        lifecycle_observation=observation,
        lifecycle_evidence_digest=(observation.observation_digest if observation is not None else None),
        detailed_task_count=(1 if disposition == "selected" else
                             (None if implementation == "skeleton_engine" else 0)),
        unexecuted_detailed_task_count=(0 if disposition == "selected" else
                                        (None if implementation == "skeleton_engine" else 0)),
        candidate_output_tokens=100,
        discarded_candidate_output_tokens=0,
    )


class BenchmarkContractTests(unittest.TestCase):
    def test_each_implementation_binds_its_actual_thread_and_recovery_policy(self):
        self.assertEqual(
            {"ephemeral_threads": False, "max_schema_recovery_attempts": 0},
            IMPLEMENTATION_RUNTIME_CONTRACT["skeleton_engine"],
        )
        self.assertEqual(
            {"ephemeral_threads": True, "max_schema_recovery_attempts": 0},
            IMPLEMENTATION_RUNTIME_CONTRACT["r31_baseline"],
        )

    def test_missing_plan_cannot_be_encoded_as_zero_or_block_latency(self):
        blocked = cell("blocked", "skeleton_engine", expected="blocked")
        for made_up_time in (0, 10, 1000):
            with self.assertRaises(ValueError):
                BenchmarkCell.model_validate({
                    **blocked.model_dump(), "latency_ms_to_first_feasible": made_up_time,
                })
        selected = cell("single", "skeleton_engine")
        with self.assertRaises(ValueError):
            BenchmarkCell.model_validate({**selected.model_dump(), "latency_ms_to_first_feasible": None})

    def test_fast_blocks_cannot_hide_slow_feasible_plan_generation(self):
        cells = []
        for scenario in ("single", "multi", "blocked-a", "blocked-b", "blocked-c"):
            blocked = scenario.startswith("blocked")
            for implementation in ("r31_baseline", "skeleton_engine"):
                elapsed = 1000 if implementation == "r31_baseline" else (1 if blocked else 900)
                cells.append(cell(scenario, implementation, expected="blocked" if blocked else "selected", elapsed=elapsed))
        report = evaluate_token_latency_gate(tuple(cells), functional_gate_passed=True)
        self.assertFalse(report.passed)
        self.assertAlmostEqual(0.10, report.time_to_first_feasible_median_improvement)
        self.assertEqual(2, report.feasible_plan_pair_count)
        self.assertEqual(3, report.blocked_pair_count)
        self.assertEqual(1000, report.blocked_latency_ms_baseline_median)
        self.assertEqual(1, report.blocked_latency_ms_engine_median)

    def test_blocked_scenarios_still_contribute_token_cost(self):
        cells = []
        for scenario in ("single", "multi", "blocked"):
            for implementation in ("r31_baseline", "skeleton_engine"):
                value = cell(scenario, implementation, expected="blocked" if scenario == "blocked" else "selected", elapsed=1000 if implementation == "r31_baseline" else 500)
                if scenario == "blocked" and implementation == "skeleton_engine":
                    value = value.model_copy(update={"uncached_input_tokens": 10000})
                cells.append(value)
        report = evaluate_token_latency_gate(tuple(cells), functional_gate_passed=True)
        self.assertFalse(report.passed)
        self.assertLess(report.overall_mean_reduction, 0)
        self.assertEqual(3, report.matched_scenario_count)

    def test_clean_failure_is_a_gate_failure_not_a_missing_timing_sample(self):
        cells = tuple(
            cell(scenario, implementation, actual="blocked" if scenario == "single" else None,
                 elapsed=1000 if implementation == "r31_baseline" else 500)
            for scenario in ("single", "multi")
            for implementation in ("r31_baseline", "skeleton_engine")
        )
        report = evaluate_token_latency_gate(cells, functional_gate_passed=True)
        self.assertFalse(report.passed)
        self.assertTrue(any("예상된" in reason for reason in report.failures))

    def test_planning_only_detail_is_not_observed_or_zero_percent(self):
        baseline = cell("single", "r31_baseline")
        planning_only = cell("single", "skeleton_engine").model_copy(
            update={
                "lifecycle_evidence_digest": None,
                "lifecycle_observation": None,
                "detailed_task_count": None,
                "unexecuted_detailed_task_count": None,
            }
        )
        planning_only = BenchmarkCell.model_validate(planning_only.model_dump())
        report = evaluate_token_latency_gate(
            (baseline, planning_only), functional_gate_passed=True
        )
        self.assertFalse(report.passed)
        self.assertIsNone(report.unexecuted_detail_ratio)
        self.assertIsNone(report.discarded_candidate_output_ratio)
        self.assertEqual(0, report.lifecycle_observed_engine_cell_count)
        self.assertEqual(1, report.lifecycle_required_engine_cell_count)
        self.assertTrue(any("NOT_OBSERVED" in reason for reason in report.failures))

    def test_zero_candidate_denominator_is_not_reported_as_zero_percent(self):
        baseline = cell("single", "r31_baseline")
        engine = cell("single", "skeleton_engine").model_copy(
            update={"candidate_output_tokens": 0, "discarded_candidate_output_tokens": 0}
        )
        report = evaluate_token_latency_gate((baseline, engine), functional_gate_passed=True)
        self.assertFalse(report.passed)
        self.assertIsNone(report.discarded_candidate_output_ratio)
        self.assertTrue(any("denominator" in reason for reason in report.failures))

    def test_lifecycle_digest_without_bound_evidence_body_is_rejected(self):
        engine = cell("single", "skeleton_engine")
        with self.assertRaisesRegex(ValueError, "본문과 digest"):
            BenchmarkCell.model_validate(
                engine.model_dump() | {"lifecycle_observation": None}
            )


if __name__ == "__main__":
    unittest.main()
