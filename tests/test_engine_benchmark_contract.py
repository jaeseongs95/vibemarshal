from __future__ import annotations

import unittest

from flowmarshal.engine.evaluation import BenchmarkCell, evaluate_token_latency_gate


def cell(scenario, implementation, *, expected="selected", actual=None, elapsed=1000):
    disposition = actual or expected
    return BenchmarkCell(
        scenario_id=scenario,
        scenario_digest="sha256:" + "a" * 64,
        order_seed=17,
        path_kind="single_path" if scenario == "single" else "multi_path",
        implementation=implementation,
        model_lock_digest="sha256:" + "b" * 64,
        functional_result_digest="sha256:" + "c" * 64,
        runner_receipt_digest="sha256:" + "d" * 64,
        expected_disposition=expected,
        disposition=disposition,
        uncached_input_tokens=1000 if implementation == "r31_baseline" else 500,
        output_tokens=100,
        latency_ms_to_first_feasible=elapsed if disposition == "selected" else None,
        latency_ms_to_disposition=elapsed,
        detailed_task_count=1 if disposition == "selected" else 0,
        unexecuted_detailed_task_count=0,
        candidate_output_tokens=100,
        discarded_candidate_output_tokens=0,
    )


class BenchmarkContractTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
