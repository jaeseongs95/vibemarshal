from __future__ import annotations

import unittest
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.evaluation import (
    BenchmarkCell,
    BenchmarkLifecycleObservation,
    BenchmarkMaterializedExecutionSpecObservation,
    BenchmarkTaskLifecycleObservation,
    EvaluationContract,
    EvaluationScope,
    PerformanceExpectedManifestIdentity,
    PerformanceQualificationReport,
    PerformanceSafetyCounters,
    PerformanceSafetyObservation,
    PerformanceScenarioIdentity,
    PerformanceThresholdPolicy,
    PerformanceUsageCounters,
    ScopeGateResult,
    TokenLatencyGateReport,
    evaluate_performance_qualification,
    evaluate_release_cutover_gate,
    evaluate_token_latency_gate,
)
from flowmarshal.engine.performance_assessment import optimization_followups


DIGEST = "sha256:" + "a" * 64
ROOT = Path(__file__).resolve().parents[1]


def _digest(label: str) -> str:
    return "sha256:" + sha256(label.encode()).hexdigest()


def _entity(prefix: str, label: str) -> str:
    return f"{prefix}_{sha256(label.encode()).hexdigest()[:32]}"


def _usage(tokens: int | None) -> PerformanceUsageCounters:
    if tokens is None:
        return PerformanceUsageCounters()
    return PerformanceUsageCounters(
        input_tokens=max(tokens - 10, 0),
        cached_input_tokens=0,
        uncached_input_tokens=max(tokens - 10, 0),
        output_tokens=min(tokens, 10),
    )


def _counters(observed: bool) -> PerformanceSafetyCounters:
    values = {
        "timeout_count": 0,
        "duplicate_provider_call_count": 0,
        "duplicate_interrupt_count": 0,
        "unknown_effect_count": 0,
        "unresolved_or_usage_unknown_count": 0,
        "unapproved_retry_or_resume_count": 0,
        "budget_policy_violation_count": 0,
        "deadline_violation_count": 0,
    }
    return PerformanceSafetyCounters(**values) if observed else PerformanceSafetyCounters()


class PerformanceEvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PerformanceThresholdPolicy.load(
            ROOT / "config" / "pre-1.0-performance-thresholds.json"
        )
        self.scenarios = tuple(
            PerformanceScenarioIdentity(
                scenario_id=f"S0{index + 1}-case",
                scenario_digest=_digest(f"scenario-{index}"),
                path_kind="multi_path" if index < 3 else "single_path",
                neutral_input_digest=_digest(f"neutral-{index}"),
                expected_disposition="selected" if index < 4 else "blocked",
            )
            for index in range(6)
        )
        self.manifest = PerformanceExpectedManifestIdentity(
            scenario_set_digest=_digest("scenario-set"),
            scenarios=self.scenarios,
        )
        self.contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2",
            scope=EvaluationScope.FULL_PLANNING_PIPELINE,
            fixture_digests=tuple(
                sha256_digest(
                    {
                        "scenario": scenario.scenario_digest,
                        "implementation": implementation,
                        "neutral": scenario.neutral_input_digest,
                    }
                )
                for scenario in self.scenarios
                for implementation in ("r31_baseline", "skeleton_engine")
            ),
            scenario_set_digest=self.manifest.scenario_set_digest,
            order_seeds=(17, 43, 89),
            expected_cell_count=36,
            role_configuration_digest=_digest("roles"),
            source_manifest_digest=_digest("source"),
            rules_digest=_digest("rules"),
            threshold_digest=self.policy.policy_digest,
            taxonomy_digest=_digest("taxonomy"),
            prompt_digest=_digest("prompt"),
            output_schema_digest=_digest("schema"),
            model_lock_digest=_digest("model-lock"),
        )

    def _lifecycle(self, scenario: PerformanceScenarioIdentity, seed: int) -> BenchmarkLifecycleObservation:
        label = f"{scenario.scenario_id}-{seed}"
        task_id = _entity("task", label)
        spec_id = _entity("execution_spec", label)
        spec_digest = _digest(f"spec-{label}")
        attempt_id = _entity("attempt", label)
        receipt = _digest(f"receipt-{label}")
        task = BenchmarkTaskLifecycleObservation(
            task_id=task_id,
            execution_spec_revision_id=spec_id,
            execution_spec_digest=spec_digest,
            attempt_id=attempt_id,
            runtime_receipt_digest=receipt,
            validation_result_digests=(_digest(f"validation-{label}"),),
        )
        materialized = BenchmarkMaterializedExecutionSpecObservation(
            plan_revision_id=_entity("plan", label),
            task_id=task_id,
            execution_spec_revision_id=spec_id,
            execution_spec_digest=spec_digest,
            materialization_event_digest=_digest(f"materialized-{label}"),
            execution_state="executed",
            provenance="worker_turn_receipt",
            worker_attempt_ids=(attempt_id,),
            runtime_receipt_digests=(receipt,),
        )
        return BenchmarkLifecycleObservation(
            schema_version="2.0",
            collector="flowmarshal.engine.lifecycle-ledger-v2",
            project_id=_entity("project", label),
            plan_revision_id=materialized.plan_revision_id,
            plan_activation_digest=_digest(f"activation-{label}"),
            model_lock_digest=self.contract.model_lock_digest,
            neutral_input_digest=scenario.neutral_input_digest,
            tasks=(task,),
            materialized_execution_specs=(materialized,),
            integration_validation_result_digests=(_digest(f"integration-{label}"),),
            state_before_digest=_digest(f"before-{label}"),
            state_after_digest=_digest(f"after-{label}"),
            state_reobservation_event_digest=_digest(f"reobserve-{label}"),
            goal_verdict_digest=_digest(f"verdict-{label}"),
            history_head_digest=_digest(f"history-{label}"),
        )

    def _matrix(
        self,
        *,
        engine_tokens: int,
        engine_latency: int,
        stage: str,
    ) -> tuple[tuple[BenchmarkCell, ...], tuple[PerformanceSafetyObservation, ...]]:
        cells: list[BenchmarkCell] = []
        observations: list[PerformanceSafetyObservation] = []
        for scenario in self.scenarios:
            for seed in (17, 43, 89):
                for implementation, tokens in (
                    ("r31_baseline", 100),
                    ("skeleton_engine", engine_tokens),
                ):
                    selected_engine = (
                        stage == "final"
                        and implementation == "skeleton_engine"
                        and scenario.expected_disposition == "selected"
                    )
                    lifecycle = self._lifecycle(scenario, seed) if selected_engine else None
                    disposition = scenario.expected_disposition
                    cell = BenchmarkCell(
                        scenario_id=scenario.scenario_id,
                        scenario_digest=scenario.scenario_digest,
                        neutral_input_digest=scenario.neutral_input_digest,
                        order_seed=seed,
                        path_kind=scenario.path_kind,
                        implementation=implementation,
                        model_lock_digest=self.contract.model_lock_digest,
                        functional_result_digest=_digest(f"functional-{scenario.scenario_id}-{seed}"),
                        runner_receipt_digest=_digest(f"runner-{scenario.scenario_id}-{seed}-{implementation}"),
                        expected_disposition=scenario.expected_disposition,
                        disposition=disposition,
                        uncached_input_tokens=max(tokens - 10, 0),
                        output_tokens=min(tokens, 10),
                        latency_ms_to_first_feasible=(
                            (100 if implementation == "r31_baseline" else engine_latency)
                            if disposition == "selected"
                            else None
                        ),
                        latency_ms_to_disposition=(
                            100 if implementation == "r31_baseline" else engine_latency
                        ),
                        selected_plan_activation_digest=(
                            lifecycle.plan_activation_digest
                            if lifecycle is not None
                            else (_digest(f"plan-{scenario.scenario_id}-{seed}")
                                  if implementation == "skeleton_engine" and disposition == "selected"
                                  else None)
                        ),
                        lifecycle_observation=lifecycle,
                        lifecycle_evidence_digest=(
                            lifecycle.observation_digest if lifecycle is not None else None
                        ),
                        detailed_task_count=1 if lifecycle is not None else None,
                        unexecuted_detailed_task_count=0 if lifecycle is not None else None,
                        candidate_output_tokens=10,
                        discarded_candidate_output_tokens=0,
                    )
                    cells.append(cell)
                    lifecycle_tokens = 1 if selected_engine else 0
                    lifecycle_observed = stage == "final"
                    observations.append(
                        PerformanceSafetyObservation(
                            scenario_id=scenario.scenario_id,
                            scenario_digest=scenario.scenario_digest,
                            order_seed=seed,
                            implementation=implementation,
                            cell_digest=sha256_digest(cell),
                            source_evidence_digest=_digest(
                                f"evidence-{scenario.scenario_id}-{seed}-{implementation}-{stage}"
                            ),
                            assessment_stage=stage,
                            complete=True,
                            functional_passed=True,
                            safety_passed=True,
                            not_observed=(
                                ("NOT_OBSERVED: lifecycle은 final 단계에서 수집합니다.",)
                                if stage == "planning"
                                else ()
                            ),
                            planning_counters=_counters(True),
                            lifecycle_counters=_counters(lifecycle_observed),
                            planning_usage=_usage(tokens),
                            lifecycle_usage=_usage(lifecycle_tokens) if lifecycle_observed else _usage(None),
                        )
                    )
        return tuple(cells), tuple(observations)

    def _scopes(self) -> tuple[ScopeGateResult, ...]:
        return tuple(
            ScopeGateResult(
                scope=scope,
                contract_digest=_digest(f"contract-{scope.value}"),
                artifact_digest=_digest(f"artifact-{scope.value}"),
                passed=True,
            )
            for scope in EvaluationScope
        )

    def test_planning_stage_accepts_exact_negative_release_boundaries(self) -> None:
        cells, observations = self._matrix(
            engine_tokens=120,
            engine_latency=125,
            stage="planning",
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertTrue(report.planning_assessment_passed, report.failures)
        self.assertTrue(report.minimum_performance_floor_passed)
        self.assertFalse(report.release_floor_passed)
        self.assertEqual(-0.2, report.overall_mean_reduction)
        self.assertEqual(-0.25, report.time_to_first_feasible_median_improvement)
        self.assertEqual(-0.25, report.all_pair_disposition_median_improvement)
        self.assertFalse(report.optimization_targets_passed)
        self.assertFalse(report.optimization_followups_required)
        self.assertEqual(
            (
                "multi_path_median_reduction",
                "overall_mean_reduction",
                "worst_single_path_regression",
                "time_to_first_feasible_median_improvement",
            ),
            report.optimization_misses,
        )
        self.assertFalse(report.cutover_eligible)
        self.assertTrue(any("lifecycle" in item for item in report.not_observed))

    def test_exact_worst_regression_upper_boundaries_are_inclusive(self) -> None:
        cells, observations = self._matrix(engine_tokens=0, engine_latency=125, stage="planning")
        observations_by_identity = {item.identity: item for item in observations}
        rewritten: list[BenchmarkCell] = []
        for cell in cells:
            tokens = cell.optimization_tokens
            if cell.implementation == "skeleton_engine":
                if cell.path_kind == "single_path":
                    tokens = 150
                elif cell.scenario_id == "S03-case" and cell.order_seed == 17:
                    tokens = 200
                else:
                    tokens = 0
                changed = cell.model_copy(
                    update={
                        "uncached_input_tokens": max(tokens - 10, 0),
                        "output_tokens": min(tokens, 10),
                    }
                )
                rewritten.append(changed)
                key = (cell.scenario_id, cell.order_seed, cell.implementation)
                observations_by_identity[key] = observations_by_identity[key].model_copy(
                    update={"cell_digest": sha256_digest(changed), "planning_usage": _usage(tokens)}
                )
            else:
                rewritten.append(cell)
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=tuple(rewritten),
            safety_observations=tuple(observations_by_identity[item.identity] for item in observations),
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertTrue(report.minimum_performance_floor_passed, report.failures)
        self.assertEqual(0.5, report.worst_single_path_regression)
        self.assertEqual(1.0, report.worst_cell_token_regression)

    def test_final_minimum_floor_allows_cutover_when_optimization_is_a_followup(self) -> None:
        cells, observations = self._matrix(
            engine_tokens=110,
            engine_latency=90,
            stage="final",
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertTrue(report.release_floor_passed, report.failures)
        self.assertFalse(report.optimization_targets_passed)
        self.assertTrue(report.cutover_eligible, report.failures)
        cutover = evaluate_release_cutover_gate(performance_report=report)
        self.assertTrue(cutover.passed, cutover.failures)

    def test_final_floor_failure_preserves_misses_without_requiring_followup(self) -> None:
        cells, observations = self._matrix(
            engine_tokens=200,
            engine_latency=200,
            stage="final",
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertFalse(report.release_floor_passed)
        self.assertTrue(report.optimization_misses)
        self.assertFalse(report.optimization_followups_required)
        self.assertTrue(optimization_followups(report)["items"])

    def test_unobserved_optimization_denominator_blocks_final_release_floor(self) -> None:
        cells, observations = self._matrix(engine_tokens=50, engine_latency=70, stage="final")
        rewritten: list[BenchmarkCell] = []
        observations_by_identity = {item.identity: item for item in observations}
        for cell in cells:
            if cell.implementation == "skeleton_engine" and cell.expected_disposition == "selected":
                changed = cell.model_copy(
                    update={"candidate_output_tokens": 0, "discarded_candidate_output_tokens": 0}
                )
                rewritten.append(changed)
                key = (cell.scenario_id, cell.order_seed, cell.implementation)
                observations_by_identity[key] = observations_by_identity[key].model_copy(
                    update={"cell_digest": sha256_digest(changed)}
                )
            else:
                rewritten.append(cell)
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=tuple(rewritten),
            safety_observations=tuple(observations_by_identity[item.identity] for item in observations),
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertFalse(report.release_floor_passed)
        self.assertFalse(report.optimization_targets_passed)
        self.assertFalse(report.optimization_followups_required)
        self.assertEqual((), report.optimization_misses)
        self.assertIsNone(report.discarded_candidate_output_ratio)
        self.assertFalse(evaluate_release_cutover_gate(performance_report=report).passed)

    def test_exact_optimization_miss_survives_float_boundary_in_followup(self) -> None:
        cells, observations = self._matrix(engine_tokens=50, engine_latency=70, stage="final")
        rewritten: list[BenchmarkCell] = []
        observations_by_identity = {item.identity: item for item in observations}
        for cell in cells:
            if cell.implementation == "skeleton_engine" and cell.expected_disposition == "selected":
                changed = cell.model_copy(
                    update={
                        "candidate_output_tokens": 10**18,
                        "discarded_candidate_output_tokens": 25 * 10**16 + 1,
                    }
                )
                rewritten.append(changed)
                key = (cell.scenario_id, cell.order_seed, cell.implementation)
                observations_by_identity[key] = observations_by_identity[key].model_copy(
                    update={"cell_digest": sha256_digest(changed)}
                )
            else:
                rewritten.append(cell)
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=tuple(rewritten),
            safety_observations=tuple(observations_by_identity[item.identity] for item in observations),
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertEqual(0.25, report.discarded_candidate_output_ratio)
        self.assertEqual(("discarded_candidate_output_ratio",), report.optimization_misses)
        self.assertTrue(report.optimization_followups_required)
        followups = optimization_followups(report)
        self.assertEqual(
            ["discarded_candidate_output_ratio"],
            [item["metric"] for item in followups["items"]],
        )

    def test_partial_detail_count_is_not_summed_as_zero(self) -> None:
        cells, observations = self._matrix(engine_tokens=50, engine_latency=70, stage="final")
        rewritten: list[BenchmarkCell] = []
        observations_by_identity = {item.identity: item for item in observations}
        changed_cell: BenchmarkCell | None = None
        for cell in cells:
            if (
                changed_cell is None
                and cell.implementation == "skeleton_engine"
                and cell.expected_disposition == "selected"
            ):
                changed_cell = cell.model_copy(update={"unexecuted_detailed_task_count": None})
                rewritten.append(changed_cell)
                key = (cell.scenario_id, cell.order_seed, cell.implementation)
                observations_by_identity[key] = observations_by_identity[key].model_copy(
                    update={"cell_digest": sha256_digest(changed_cell)}
                )
            else:
                rewritten.append(cell)
        assert changed_cell is not None
        with self.assertRaisesRegex(ValidationError, "미실행 detail"):
            BenchmarkCell.model_validate(changed_cell.model_dump())
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=tuple(rewritten),
            safety_observations=tuple(observations_by_identity[item.identity] for item in observations),
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertIsNone(report.unexecuted_detail_ratio)
        self.assertFalse(report.release_floor_passed)
        self.assertTrue(any("분자 또는 분모가 일부 누락" in item for item in report.not_observed))

    def test_missing_whole_pair_returns_null_safe_rejection(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="planning")
        missing_keys = {("S01-case", 17, "r31_baseline"), ("S01-case", 17, "skeleton_engine")}
        cells = tuple(
            item for item in cells
            if (item.scenario_id, item.order_seed, item.implementation) not in missing_keys
        )
        observations = tuple(item for item in observations if item.identity not in missing_keys)
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertFalse(report.manifest_complete)
        self.assertFalse(report.release_floor_passed)
        self.assertFalse(report.planning_assessment_passed)
        self.assertIsNone(report.overall_mean_reduction)
        self.assertIsNone(report.multi_path_median_reduction)
        self.assertIsNone(report.all_pair_disposition_median_improvement)
        self.assertTrue(any("whole pair" in item for item in report.not_observed))

    def test_tampered_cell_digest_is_preserved_as_a_binding_failure(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="planning")
        observations = (
            observations[0].model_copy(update={"cell_digest": _digest("tampered")}),
            *observations[1:],
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertFalse(report.manifest_complete)
        self.assertTrue(any("결속" in item for item in report.failures))

    def test_partial_usage_is_reported_as_not_observed_without_input_exception(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="planning")
        partial = PerformanceUsageCounters(input_tokens=100)
        observations = (
            observations[0].model_copy(
                update={
                    "complete": False,
                    "planning_usage": partial,
                    "not_observed": ("NOT_OBSERVED: output token counter가 없습니다.",),
                }
            ),
            *observations[1:],
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertFalse(report.planning_assessment_passed)
        self.assertTrue(any("output token" in item for item in report.not_observed))

    def test_partial_planning_total_tokens_remain_null_in_pair_result(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="planning")
        partial = PerformanceUsageCounters(uncached_input_tokens=90, output_tokens=10)
        observations = (
            observations[0].model_copy(
                update={
                    "complete": False,
                    "planning_usage": partial,
                    "not_observed": ("NOT_OBSERVED: actual input token counter가 없습니다.",),
                }
            ),
            *observations[1:],
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        pair = next(
            item
            for item in report.pair_results
            if item.scenario_id == "S01-case" and item.order_seed == 17
        )
        self.assertIsNone(pair.baseline_total_tokens)
        self.assertEqual(90, pair.engine_total_tokens)
        self.assertFalse(report.planning_assessment_passed)
        self.assertTrue(any("단계별 실제 input+output token" in item for item in report.not_observed))

    def test_partial_final_lifecycle_total_does_not_reuse_planning_total(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="final")
        rewritten: list[PerformanceSafetyObservation] = []
        target_identity = ("S01-case", 17, "skeleton_engine")
        for observation in observations:
            if observation.identity == target_identity:
                rewritten.append(
                    observation.model_copy(
                        update={
                            "complete": False,
                            "lifecycle_usage": PerformanceUsageCounters(input_tokens=1),
                            "not_observed": (
                                "NOT_OBSERVED: lifecycle output token counter가 없습니다.",
                            ),
                        }
                    )
                )
            else:
                rewritten.append(observation)
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=tuple(rewritten),
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        pair = next(
            item
            for item in report.pair_results
            if item.scenario_id == "S01-case" and item.order_seed == 17
        )
        self.assertEqual(100, pair.baseline_total_tokens)
        self.assertIsNone(pair.engine_total_tokens)
        self.assertFalse(report.release_floor_passed)
        self.assertTrue(any("단계별 실제 input+output token" in item for item in report.not_observed))

    def test_zero_baseline_denominator_is_not_observed_and_fails_floor(self) -> None:
        cells, observations = self._matrix(engine_tokens=90, engine_latency=90, stage="planning")
        baseline = cells[0].model_copy(update={"uncached_input_tokens": 0, "output_tokens": 0})
        cells = (baseline, *cells[1:])
        observations = (
            observations[0].model_copy(
                update={"cell_digest": sha256_digest(baseline), "planning_usage": _usage(0)}
            ),
            *observations[1:],
        )
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=(),
            assessment_stage="planning",
        )
        self.assertFalse(report.release_floor_passed)
        self.assertTrue(any("denominator" in item for item in report.not_observed))

    def test_strict_frozen_policy_rejects_coercion_and_threshold_change(self) -> None:
        body = self.policy.model_dump()
        body["maximum_worst_cell_token_regression"] = type(
            self.policy.maximum_worst_cell_token_regression
        )("1.01")
        with self.assertRaisesRegex(ValidationError, "동결"):
            PerformanceThresholdPolicy.model_validate(body)
        body = self.policy.model_dump()
        body["schema_version"] = 1
        with self.assertRaises(ValidationError):
            PerformanceThresholdPolicy.model_validate(body)

    def test_v3_token_latency_report_remains_readable_and_calculable(self) -> None:
        cells, _ = self._matrix(engine_tokens=50, engine_latency=70, stage="planning")
        pair = tuple(
            item for item in cells if item.scenario_id == "S01-case" and item.order_seed == 17
        )
        report = evaluate_token_latency_gate(pair, functional_gate_passed=True)
        self.assertIsInstance(report, TokenLatencyGateReport)
        restored = TokenLatencyGateReport.model_validate_json(report.model_dump_json())
        self.assertEqual("3.0", restored.schema_version)

    def test_planning_report_is_never_accepted_for_release_cutover(self) -> None:
        cells, observations = self._matrix(engine_tokens=50, engine_latency=70, stage="planning")
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=self._scopes(),
            assessment_stage="planning",
        )
        cutover = evaluate_release_cutover_gate(performance_report=report)
        self.assertFalse(cutover.passed)
        self.assertIn("final PerformanceQualificationReport v4.0", cutover.failures[0])

    def test_report_metric_tampering_is_rejected_from_raw_pair_values(self) -> None:
        cells, observations = self._matrix(engine_tokens=110, engine_latency=90, stage="final")
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        body = report.model_dump()
        body["overall_mean_reduction"] = 0.99
        with self.assertRaisesRegex(ValidationError, "재계산"):
            PerformanceQualificationReport.model_validate(body)

    def test_report_null_pair_total_cannot_preserve_a_pass_claim(self) -> None:
        cells, observations = self._matrix(engine_tokens=50, engine_latency=70, stage="final")
        report = evaluate_performance_qualification(
            expected_manifest=self.manifest,
            contract=self.contract,
            policy=self.policy,
            cells=cells,
            safety_observations=observations,
            scope_results=self._scopes(),
            assessment_stage="final",
        )
        self.assertTrue(report.release_floor_passed, report.failures)
        body = report.model_dump()
        body["pair_results"][0]["baseline_total_tokens"] = None
        with self.assertRaisesRegex(ValidationError, "planning 판정"):
            PerformanceQualificationReport.model_validate(body)


if __name__ == "__main__":
    unittest.main()
