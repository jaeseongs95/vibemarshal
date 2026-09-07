from __future__ import annotations

import statistics
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from ..canonical import sha256_digest
from .domain import EngineModel
from .evaluation import BenchmarkCell, EvaluationContract, EvaluationScope, ScopeGateResult


_DIGEST = r"^sha256:[0-9a-f]{64}$"

OptimizationMetric = Literal[
    "multi_path_median_reduction",
    "overall_mean_reduction",
    "worst_single_path_regression",
    "unexecuted_detail_ratio",
    "discarded_candidate_output_ratio",
    "time_to_first_feasible_median_improvement",
]
_OPTIMIZATION_METRIC_ORDER: tuple[OptimizationMetric, ...] = (
    "multi_path_median_reduction",
    "overall_mean_reduction",
    "worst_single_path_regression",
    "unexecuted_detail_ratio",
    "discarded_candidate_output_ratio",
    "time_to_first_feasible_median_improvement",
)
_SEEDS = (17, 43, 89)
_IMPLEMENTATIONS = ("r31_baseline", "skeleton_engine")


class PerformanceModel(EngineModel):
    """성능 qualification 계약은 coercion 없이 읽는 불변 모델이다."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_default=True,
        strict=True,
    )


class PerformanceThresholdPolicy(PerformanceModel):
    schema_version: Literal["1.0"] = "1.0"
    format: Literal["flowmarshal-pre-1.0-performance-thresholds-v1"]
    token_measure: Literal["planning_uncached_input_plus_output"]
    aggregation: Literal["pairwise_relative_mean_and_median"]
    missing_measurement: Literal["not_observed"]
    minimum_overall_mean_reduction: Decimal
    minimum_multi_path_median_reduction: Decimal
    maximum_worst_single_path_regression: Decimal
    maximum_worst_cell_token_regression: Decimal
    minimum_time_to_first_feasible_median_improvement: Decimal
    minimum_all_pair_disposition_median_improvement: Decimal
    optimization_minimum_multi_path_median_reduction: Decimal
    optimization_minimum_overall_mean_reduction: Decimal
    optimization_maximum_worst_single_path_regression: Decimal
    optimization_maximum_unexecuted_detail_ratio: Decimal
    optimization_maximum_discarded_candidate_output_ratio: Decimal
    optimization_minimum_time_to_first_feasible_median_improvement: Decimal

    @model_validator(mode="after")
    def values_are_the_frozen_pre_1_0_policy(self) -> "PerformanceThresholdPolicy":
        expected = {
            "minimum_overall_mean_reduction": Decimal("-0.20"),
            "minimum_multi_path_median_reduction": Decimal("-0.25"),
            "maximum_worst_single_path_regression": Decimal("0.50"),
            "maximum_worst_cell_token_regression": Decimal("1.00"),
            "minimum_time_to_first_feasible_median_improvement": Decimal("-0.25"),
            "minimum_all_pair_disposition_median_improvement": Decimal("-0.25"),
            "optimization_minimum_multi_path_median_reduction": Decimal("0.30"),
            "optimization_minimum_overall_mean_reduction": Decimal("0.20"),
            "optimization_maximum_worst_single_path_regression": Decimal("0.05"),
            "optimization_maximum_unexecuted_detail_ratio": Decimal("0.10"),
            "optimization_maximum_discarded_candidate_output_ratio": Decimal("0.25"),
            "optimization_minimum_time_to_first_feasible_median_improvement": Decimal("0.20"),
        }
        changed = [name for name, value in expected.items() if getattr(self, name) != value]
        if changed:
            raise ValueError(f"동결된 pre-1.0 performance threshold가 바뀌었습니다: {changed}")
        return self

    @property
    def policy_digest(self) -> str:
        return sha256_digest(self)

    @classmethod
    def load(cls, path: Path | str) -> "PerformanceThresholdPolicy":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class PerformanceScenarioIdentity(PerformanceModel):
    scenario_id: str = Field(min_length=1, max_length=200)
    scenario_digest: str = Field(pattern=_DIGEST)
    path_kind: Literal["multi_path", "single_path"]
    neutral_input_digest: str = Field(pattern=_DIGEST)
    expected_disposition: Literal["selected", "blocked"]


class PerformanceExpectedManifestIdentity(PerformanceModel):
    format: Literal["flowmarshal-performance-manifest-v1"] = "flowmarshal-performance-manifest-v1"
    scenario_set_digest: str = Field(pattern=_DIGEST)
    scenarios: tuple[PerformanceScenarioIdentity, ...]
    order_seeds: tuple[int, ...] = _SEEDS
    implementations: tuple[Literal["r31_baseline", "skeleton_engine"], ...] = _IMPLEMENTATIONS
    expected_cell_count: Literal[36] = 36
    expected_pair_count: Literal[18] = 18
    expected_selected_pair_count: Literal[12] = 12
    expected_blocked_pair_count: Literal[6] = 6

    @model_validator(mode="after")
    def topology_is_the_frozen_release_matrix(self) -> "PerformanceExpectedManifestIdentity":
        if len(self.scenarios) != 6:
            raise ValueError("performance manifest에는 정확히 6개 scenario가 필요합니다.")
        if self.order_seeds != _SEEDS:
            raise ValueError("performance manifest seed는 (17, 43, 89)로 동결됩니다.")
        if self.implementations != _IMPLEMENTATIONS:
            raise ValueError("performance manifest implementation 순서가 동결값과 다릅니다.")
        ids = tuple(item.scenario_id for item in self.scenarios)
        digests = tuple(item.scenario_digest for item in self.scenarios)
        if len(ids) != len(set(ids)) or len(digests) != len(set(digests)):
            raise ValueError("performance manifest scenario identity가 중복됐습니다.")
        selected = sum(item.expected_disposition == "selected" for item in self.scenarios) * len(self.order_seeds)
        blocked = sum(item.expected_disposition == "blocked" for item in self.scenarios) * len(self.order_seeds)
        if selected != self.expected_selected_pair_count or blocked != self.expected_blocked_pair_count:
            raise ValueError("performance manifest selected/blocked pair 분모가 동결값과 다릅니다.")
        if len(self.scenarios) * len(self.order_seeds) != self.expected_pair_count:
            raise ValueError("performance manifest pair 분모가 동결값과 다릅니다.")
        if self.expected_pair_count * len(self.implementations) != self.expected_cell_count:
            raise ValueError("performance manifest cell 분모가 동결값과 다릅니다.")
        return self

    @property
    def manifest_digest(self) -> str:
        return sha256_digest(self)


class PerformanceSafetyCounters(PerformanceModel):
    timeout_count: int | None = Field(default=None, ge=0)
    duplicate_provider_call_count: int | None = Field(default=None, ge=0)
    duplicate_interrupt_count: int | None = Field(default=None, ge=0)
    unknown_effect_count: int | None = Field(default=None, ge=0)
    unresolved_or_usage_unknown_count: int | None = Field(default=None, ge=0)
    unapproved_retry_or_resume_count: int | None = Field(default=None, ge=0)
    budget_policy_violation_count: int | None = Field(default=None, ge=0)
    deadline_violation_count: int | None = Field(default=None, ge=0)

    @property
    def complete(self) -> bool:
        return all(value is not None for value in self.model_dump().values())

    @property
    def passed(self) -> bool:
        return self.complete and all(value == 0 for value in self.model_dump().values())


class PerformanceUsageCounters(PerformanceModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    uncached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def uncached_usage_is_reconciled(self) -> "PerformanceUsageCounters":
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.uncached_input_tokens,
            self.output_tokens,
        )
        present = tuple(value is not None for value in values)
        if all(present):
            assert self.input_tokens is not None
            assert self.cached_input_tokens is not None
            assert self.uncached_input_tokens is not None
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError("cached input token이 전체 input token보다 큽니다.")
            if self.uncached_input_tokens != self.input_tokens - self.cached_input_tokens:
                raise ValueError("uncached input token이 input-cached와 다릅니다.")
        return self

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.input_tokens,
                self.cached_input_tokens,
                self.uncached_input_tokens,
                self.output_tokens,
            )
        )

    @property
    def optimization_tokens(self) -> int | None:
        if self.uncached_input_tokens is None or self.output_tokens is None:
            return None
        return self.uncached_input_tokens + self.output_tokens

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class PerformanceSafetyObservation(PerformanceModel):
    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str
    scenario_digest: str = Field(pattern=_DIGEST)
    order_seed: int = Field(ge=0)
    implementation: Literal["r31_baseline", "skeleton_engine"]
    cell_digest: str = Field(pattern=_DIGEST)
    source_evidence_digest: str = Field(pattern=_DIGEST)
    assessment_stage: Literal["planning", "final"]
    complete: bool
    functional_passed: bool | None = None
    safety_passed: bool | None = None
    failures: tuple[str, ...] = ()
    not_observed: tuple[str, ...] = ()
    planning_counters: PerformanceSafetyCounters
    lifecycle_counters: PerformanceSafetyCounters
    planning_usage: PerformanceUsageCounters
    lifecycle_usage: PerformanceUsageCounters

    @model_validator(mode="after")
    def completion_claim_has_stage_evidence(self) -> "PerformanceSafetyObservation":
        planning_complete = (
            self.functional_passed is not None
            and self.safety_passed is not None
            and self.planning_counters.complete
            and self.planning_usage.complete
        )
        lifecycle_complete = self.lifecycle_counters.complete and self.lifecycle_usage.complete
        required_complete = planning_complete and (
            self.assessment_stage == "planning" or lifecycle_complete
        )
        if self.complete and (
            not required_complete
            or self.failures
            or (self.assessment_stage == "final" and self.not_observed)
        ):
            raise ValueError("complete safety 관측에는 단계별 필수 evidence와 무결함 상태가 필요합니다.")
        return self

    @property
    def identity(self) -> tuple[str, int, str]:
        return (self.scenario_id, self.order_seed, self.implementation)

    @property
    def observation_digest(self) -> str:
        return sha256_digest(self)


class PerformancePairResult(PerformanceModel):
    scenario_id: str
    order_seed: int
    path_kind: Literal["multi_path", "single_path"]
    expected_disposition: Literal["selected", "blocked"]
    baseline_cell_digest: str = Field(pattern=_DIGEST)
    engine_cell_digest: str = Field(pattern=_DIGEST)
    baseline_safety_observation_digest: str = Field(pattern=_DIGEST)
    engine_safety_observation_digest: str = Field(pattern=_DIGEST)
    baseline_optimization_tokens: int = Field(ge=0)
    engine_optimization_tokens: int = Field(ge=0)
    baseline_total_tokens: int | None = Field(ge=0)
    engine_total_tokens: int | None = Field(ge=0)
    baseline_latency_ms_to_first_feasible: int | None = Field(default=None, gt=0)
    engine_latency_ms_to_first_feasible: int | None = Field(default=None, gt=0)
    baseline_latency_ms_to_disposition: int = Field(gt=0)
    engine_latency_ms_to_disposition: int = Field(gt=0)
    token_reduction: float
    token_regression: float
    time_to_first_feasible_improvement: float | None
    time_to_disposition_improvement: float


class PerformanceQualificationReport(PerformanceModel):
    schema_version: Literal["4.0"] = "4.0"
    assessment_stage: Literal["planning", "final"]
    contract_digest: str = Field(pattern=_DIGEST)
    source_manifest_digest: str = Field(pattern=_DIGEST)
    scenario_set_digest: str = Field(pattern=_DIGEST)
    expected_manifest_digest: str = Field(pattern=_DIGEST)
    threshold_policy: PerformanceThresholdPolicy
    performance_threshold_policy_digest: str = Field(pattern=_DIGEST)
    expected_cell_count: Literal[36] = 36
    observed_cell_count: int = Field(ge=0)
    expected_pair_count: Literal[18] = 18
    observed_pair_count: int = Field(ge=0)
    expected_selected_pair_count: Literal[12] = 12
    observed_selected_pair_count: int = Field(ge=0)
    expected_blocked_pair_count: Literal[6] = 6
    observed_blocked_pair_count: int = Field(ge=0)
    cell_digests: tuple[str, ...]
    safety_observation_digests: tuple[str, ...]
    source_evidence_digests: tuple[str, ...]
    scope_results: tuple[ScopeGateResult, ...]
    pair_results: tuple[PerformancePairResult, ...]
    overall_mean_reduction: float | None
    multi_path_median_reduction: float | None
    worst_single_path_regression: float | None
    worst_cell_token_regression: float | None
    time_to_first_feasible_median_improvement: float | None
    all_pair_disposition_median_improvement: float | None
    unexecuted_detail_ratio: float | None
    discarded_candidate_output_ratio: float | None
    manifest_complete: bool
    functional_safety_passed: bool
    minimum_performance_floor_passed: bool
    release_floor_passed: bool
    optimization_targets_passed: bool
    optimization_followups_required: bool
    optimization_misses: tuple[OptimizationMetric, ...]
    planning_assessment_passed: bool
    cutover_eligible: bool
    failures: tuple[str, ...]
    not_observed: tuple[str, ...]

    @model_validator(mode="after")
    def derived_release_decisions_are_reproducible(self) -> "PerformanceQualificationReport":
        if self.performance_threshold_policy_digest != self.threshold_policy.policy_digest:
            raise ValueError("performance report의 threshold policy digest가 본문과 다릅니다.")
        if self.observed_pair_count != len(self.pair_results):
            raise ValueError("performance report pair count가 raw pair 결과와 다릅니다.")
        selected = sum(item.expected_disposition == "selected" for item in self.pair_results)
        blocked = sum(item.expected_disposition == "blocked" for item in self.pair_results)
        if self.observed_selected_pair_count != selected or self.observed_blocked_pair_count != blocked:
            raise ValueError("performance report disposition pair count가 raw pair 결과와 다릅니다.")
        raw = _release_metrics_from_pairs(self.pair_results)
        exposed = (
            self.overall_mean_reduction,
            self.multi_path_median_reduction,
            self.worst_single_path_regression,
            self.worst_cell_token_regression,
            self.time_to_first_feasible_median_improvement,
            self.all_pair_disposition_median_improvement,
        )
        expected = (
            (None, None, None, None, None, None)
            if len(self.pair_results) != self.expected_pair_count
            else tuple(None if item is None else float(item) for item in raw)
        )
        if exposed != expected:
            raise ValueError("performance report metric이 raw pair 결과의 재계산값과 다릅니다.")
        minimum_floor = len(self.pair_results) == self.expected_pair_count and _release_floor_passes(
            raw, self.threshold_policy
        )
        if self.minimum_performance_floor_passed != minimum_floor:
            raise ValueError("performance report 수치 최소선 판정이 raw pair 결과와 다릅니다.")
        pair_totals_observed = (
            len(self.pair_results) == self.expected_pair_count
            and all(
                item.baseline_total_tokens is not None and item.engine_total_tokens is not None
                for item in self.pair_results
            )
        )
        planning = (
            self.manifest_complete
            and self.functional_safety_passed
            and minimum_floor
            and pair_totals_observed
        )
        if self.planning_assessment_passed != planning:
            raise ValueError("performance planning 판정의 파생값이 일치하지 않습니다.")
        optimization_observed = all(
            value is not None
            for value in (
                self.multi_path_median_reduction,
                self.overall_mean_reduction,
                self.worst_single_path_regression,
                self.unexecuted_detail_ratio,
                self.discarded_candidate_output_ratio,
                self.time_to_first_feasible_median_improvement,
            )
        )
        release_floor = self.assessment_stage == "final" and planning and optimization_observed
        if self.release_floor_passed != release_floor:
            raise ValueError("performance release floor는 final 완결 판정과 일치해야 합니다.")
        scope_gate, _ = _all_scope_gate(self.scope_results)
        cutover = release_floor and scope_gate
        if self.cutover_eligible != cutover:
            raise ValueError("performance cutover 판정의 파생값이 일치하지 않습니다.")
        if len(set(self.optimization_misses)) != len(self.optimization_misses):
            raise ValueError("optimization miss metric이 중복됐습니다.")
        expected_order = tuple(
            metric for metric in _OPTIMIZATION_METRIC_ORDER if metric in self.optimization_misses
        )
        if self.optimization_misses != expected_order:
            raise ValueError("optimization miss metric 순서가 고정 순서와 다릅니다.")
        exact_pair_misses = _optimization_misses(
            multi_path_median_reduction=(
                raw[1] if len(self.pair_results) == self.expected_pair_count else None
            ),
            overall_mean_reduction=(
                raw[0] if len(self.pair_results) == self.expected_pair_count else None
            ),
            worst_single_path_regression=(
                raw[2] if len(self.pair_results) == self.expected_pair_count else None
            ),
            unexecuted_detail_ratio=None,
            discarded_candidate_output_ratio=None,
            time_to_first_feasible_median_improvement=(
                raw[4] if len(self.pair_results) == self.expected_pair_count else None
            ),
            policy=self.threshold_policy,
        )
        exact_pair_metrics = {
            "multi_path_median_reduction",
            "overall_mean_reduction",
            "worst_single_path_regression",
            "time_to_first_feasible_median_improvement",
        }
        if {
            metric for metric in self.optimization_misses if metric in exact_pair_metrics
        } != set(exact_pair_misses):
            raise ValueError("pair 기반 optimization miss가 raw pair의 exact 계산과 다릅니다.")
        observed_values = {
            "multi_path_median_reduction": self.multi_path_median_reduction,
            "overall_mean_reduction": self.overall_mean_reduction,
            "worst_single_path_regression": self.worst_single_path_regression,
            "unexecuted_detail_ratio": self.unexecuted_detail_ratio,
            "discarded_candidate_output_ratio": self.discarded_candidate_output_ratio,
            "time_to_first_feasible_median_improvement": self.time_to_first_feasible_median_improvement,
        }
        if any(observed_values[metric] is None for metric in self.optimization_misses):
            raise ValueError("미관측 optimization metric은 miss로 기록할 수 없습니다.")
        optimization_observed = all(value is not None for value in observed_values.values())
        if self.optimization_targets_passed != (
            optimization_observed and not self.optimization_misses
        ):
            raise ValueError("optimization target 판정이 관측 상태와 exact miss 목록에 맞지 않습니다.")
        if self.optimization_followups_required != (
            self.release_floor_passed and bool(self.optimization_misses)
        ):
            raise ValueError("optimization follow-up 판정이 final 최소선과 exact miss 목록에 맞지 않습니다.")
        return self

    @property
    def report_digest(self) -> str:
        return sha256_digest(self)


class ReleaseCutoverReport(PerformanceModel):
    schema_version: Literal["4.0"] = "4.0"
    performance_report_digest: str = Field(pattern=_DIGEST)
    performance_report: PerformanceQualificationReport
    missing_scopes: tuple[EvaluationScope, ...]
    passed: bool
    failures: tuple[str, ...]


def _fraction(value: Decimal) -> Fraction:
    return Fraction(value)


def _improvement(old: int, new: int) -> Fraction | None:
    if old == 0:
        return None
    return Fraction(old - new, old)


def _median(values: list[Fraction]) -> Fraction | None:
    if not values:
        return None
    return statistics.median(values)


def _mean(values: list[Fraction]) -> Fraction | None:
    if not values:
        return None
    return sum(values, Fraction(0, 1)) / len(values)


def _sum_observed(values: tuple[int | None, ...]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _release_metrics_from_pairs(
    pairs: tuple[PerformancePairResult, ...],
) -> tuple[
    Fraction | None,
    Fraction | None,
    Fraction | None,
    Fraction | None,
    Fraction | None,
    Fraction | None,
]:
    reductions = [
        _improvement(item.baseline_optimization_tokens, item.engine_optimization_tokens)
        for item in pairs
    ]
    token_values = [item for item in reductions if item is not None]
    multi = [
        value
        for item, value in zip(pairs, reductions, strict=True)
        if item.path_kind == "multi_path" and value is not None
    ]
    single_regressions = [
        -value
        for item, value in zip(pairs, reductions, strict=True)
        if item.path_kind == "single_path" and value is not None
    ]
    all_regressions = [-value for value in token_values]
    first_feasible = [
        _improvement(
            item.baseline_latency_ms_to_first_feasible,
            item.engine_latency_ms_to_first_feasible,
        )
        for item in pairs
        if item.expected_disposition == "selected"
        and item.baseline_latency_ms_to_first_feasible is not None
        and item.engine_latency_ms_to_first_feasible is not None
    ]
    dispositions = [
        _improvement(item.baseline_latency_ms_to_disposition, item.engine_latency_ms_to_disposition)
        for item in pairs
    ]
    return (
        _mean(token_values),
        _median(multi),
        max(single_regressions) if single_regressions else None,
        max(all_regressions) if all_regressions else None,
        _median([item for item in first_feasible if item is not None]),
        _median([item for item in dispositions if item is not None]),
    )


def _release_floor_passes(
    metrics: tuple[
        Fraction | None,
        Fraction | None,
        Fraction | None,
        Fraction | None,
        Fraction | None,
        Fraction | None,
    ],
    policy: PerformanceThresholdPolicy,
) -> bool:
    overall, multi, worst_single, worst_cell, feasible, disposition = metrics
    return all(
        (
            overall is not None and overall >= _fraction(policy.minimum_overall_mean_reduction),
            multi is not None and multi >= _fraction(policy.minimum_multi_path_median_reduction),
            worst_single is not None and worst_single <= _fraction(policy.maximum_worst_single_path_regression),
            worst_cell is not None and worst_cell <= _fraction(policy.maximum_worst_cell_token_regression),
            feasible is not None and feasible >= _fraction(policy.minimum_time_to_first_feasible_median_improvement),
            disposition is not None
            and disposition
            >= _fraction(policy.minimum_all_pair_disposition_median_improvement),
        )
    )


def _optimization_misses(
    *,
    multi_path_median_reduction: Fraction | None,
    overall_mean_reduction: Fraction | None,
    worst_single_path_regression: Fraction | None,
    unexecuted_detail_ratio: Fraction | None,
    discarded_candidate_output_ratio: Fraction | None,
    time_to_first_feasible_median_improvement: Fraction | None,
    policy: PerformanceThresholdPolicy,
) -> tuple[OptimizationMetric, ...]:
    checks: tuple[tuple[OptimizationMetric, bool | None], ...] = (
        (
            "multi_path_median_reduction",
            None
            if multi_path_median_reduction is None
            else multi_path_median_reduction
            >= _fraction(policy.optimization_minimum_multi_path_median_reduction),
        ),
        (
            "overall_mean_reduction",
            None
            if overall_mean_reduction is None
            else overall_mean_reduction
            >= _fraction(policy.optimization_minimum_overall_mean_reduction),
        ),
        (
            "worst_single_path_regression",
            None
            if worst_single_path_regression is None
            else worst_single_path_regression
            <= _fraction(policy.optimization_maximum_worst_single_path_regression),
        ),
        (
            "unexecuted_detail_ratio",
            None
            if unexecuted_detail_ratio is None
            else unexecuted_detail_ratio
            <= _fraction(policy.optimization_maximum_unexecuted_detail_ratio),
        ),
        (
            "discarded_candidate_output_ratio",
            None
            if discarded_candidate_output_ratio is None
            else discarded_candidate_output_ratio
            <= _fraction(policy.optimization_maximum_discarded_candidate_output_ratio),
        ),
        (
            "time_to_first_feasible_median_improvement",
            None
            if time_to_first_feasible_median_improvement is None
            else time_to_first_feasible_median_improvement
            >= _fraction(policy.optimization_minimum_time_to_first_feasible_median_improvement),
        ),
    )
    return tuple(metric for metric, passed in checks if passed is False)


def _all_scope_gate(scope_results: tuple[ScopeGateResult, ...]) -> tuple[bool, tuple[EvaluationScope, ...]]:
    by_scope = {item.scope: item for item in scope_results}
    missing = tuple(scope for scope in EvaluationScope if scope not in by_scope)
    return (
        len(by_scope) == len(scope_results)
        and not missing
        and all(by_scope[scope].passed for scope in EvaluationScope),
        missing,
    )


def evaluate_performance_qualification(
    *,
    expected_manifest: PerformanceExpectedManifestIdentity,
    contract: EvaluationContract,
    policy: PerformanceThresholdPolicy,
    cells: tuple[BenchmarkCell, ...],
    safety_observations: tuple[PerformanceSafetyObservation, ...],
    scope_results: tuple[ScopeGateResult, ...],
    assessment_stage: Literal["planning", "final"],
) -> PerformanceQualificationReport:
    failures: list[str] = []
    not_observed: list[str] = []
    binding_valid = True
    scenario_by_id = {item.scenario_id: item for item in expected_manifest.scenarios}
    expected_keys = {
        (scenario.scenario_id, seed, implementation)
        for scenario in expected_manifest.scenarios
        for seed in expected_manifest.order_seeds
        for implementation in expected_manifest.implementations
    }
    if contract.scope is not EvaluationScope.FULL_PLANNING_PIPELINE:
        failures.append("evaluation contract scope가 full planning pipeline이 아닙니다.")
        binding_valid = False
    if contract.scenario_set_digest != expected_manifest.scenario_set_digest:
        failures.append("evaluation contract와 expected manifest의 scenario set digest가 다릅니다.")
        binding_valid = False
    expected_fixture_digests = tuple(
        sha256_digest(
            {
                "scenario": scenario.scenario_digest,
                "implementation": implementation,
                "neutral": scenario.neutral_input_digest,
            }
        )
        for scenario in expected_manifest.scenarios
        for implementation in expected_manifest.implementations
    )
    if (
        len(contract.fixture_digests) != len(expected_fixture_digests)
        or set(contract.fixture_digests) != set(expected_fixture_digests)
    ):
        failures.append("evaluation contract의 implementation fixture identity가 expected manifest와 다릅니다.")
        binding_valid = False
    if (
        contract.order_seeds != expected_manifest.order_seeds
        or contract.expected_cell_count != expected_manifest.expected_cell_count
    ):
        failures.append("evaluation contract의 seed 또는 performance cell 분모가 expected manifest와 다릅니다.")
        binding_valid = False
    if len({item.scope for item in scope_results}) != len(scope_results):
        failures.append("qualification scope 결과가 중복됐습니다.")

    cells_by_key: dict[tuple[str, int, str], BenchmarkCell] = {}
    duplicate_cell_keys: set[tuple[str, int, str]] = set()
    for cell in cells:
        key = (cell.scenario_id, cell.order_seed, cell.implementation)
        if key in cells_by_key:
            duplicate_cell_keys.add(key)
        else:
            cells_by_key[key] = cell
    if duplicate_cell_keys:
        failures.append(f"benchmark cell identity가 중복됐습니다: {sorted(duplicate_cell_keys)}")
    missing_cells = sorted(expected_keys - set(cells_by_key))
    unexpected_cells = sorted(set(cells_by_key) - expected_keys)
    if missing_cells:
        not_observed.append(f"NOT_OBSERVED: expected benchmark cell 누락: {missing_cells}")
    if unexpected_cells:
        failures.append(f"expected manifest 밖의 benchmark cell이 있습니다: {unexpected_cells}")

    observations_by_key: dict[tuple[str, int, str], PerformanceSafetyObservation] = {}
    duplicate_observation_keys: set[tuple[str, int, str]] = set()
    for observation in safety_observations:
        if observation.identity in observations_by_key:
            duplicate_observation_keys.add(observation.identity)
        else:
            observations_by_key[observation.identity] = observation
    if duplicate_observation_keys:
        failures.append(f"safety observation identity가 중복됐습니다: {sorted(duplicate_observation_keys)}")
    missing_observations = sorted(expected_keys - set(observations_by_key))
    unexpected_observations = sorted(set(observations_by_key) - expected_keys)
    if missing_observations:
        not_observed.append(f"NOT_OBSERVED: expected safety observation 누락: {missing_observations}")
    if unexpected_observations:
        failures.append(f"expected manifest 밖의 safety observation이 있습니다: {unexpected_observations}")

    valid_keys: set[tuple[str, int, str]] = set()
    functional_safety = True
    for key in sorted(expected_keys & set(cells_by_key) & set(observations_by_key)):
        cell = cells_by_key[key]
        observation = observations_by_key[key]
        scenario = scenario_by_id[cell.scenario_id]
        cell_digest = sha256_digest(cell)
        identity_matches = (
            cell.scenario_digest == scenario.scenario_digest
            and cell.path_kind == scenario.path_kind
            and cell.neutral_input_digest == scenario.neutral_input_digest
            and cell.expected_disposition == scenario.expected_disposition
            and cell.order_seed in expected_manifest.order_seeds
            and cell.model_lock_digest == contract.model_lock_digest
            and observation.scenario_digest == scenario.scenario_digest
            and observation.cell_digest == cell_digest
            and observation.assessment_stage == assessment_stage
        )
        if not identity_matches:
            failures.append(f"cell/observation manifest 또는 digest 결속이 다릅니다: {key}")
            functional_safety = False
            continue
        if cell.disposition != cell.expected_disposition:
            failures.append(f"expected disposition을 충족하지 못했습니다: {key}")
            functional_safety = False
        planning_ok = (
            observation.complete
            and observation.functional_passed is True
            and observation.safety_passed is True
            and observation.planning_counters.passed
            and observation.planning_usage.complete
            and not observation.failures
            and (assessment_stage == "planning" or not observation.not_observed)
        )
        final_ok = planning_ok and observation.lifecycle_counters.passed and observation.lifecycle_usage.complete
        if not planning_ok or (assessment_stage == "final" and not final_ok):
            functional_safety = False
            if observation.failures:
                failures.extend(f"{key}: {item}" for item in observation.failures)
            if observation.not_observed:
                not_observed.extend(f"{key}: {item}" for item in observation.not_observed)
            if not observation.complete and not observation.failures and not observation.not_observed:
                not_observed.append(f"NOT_OBSERVED: safety observation이 완결되지 않았습니다: {key}")
        elif observation.not_observed:
            not_observed.extend(f"{key}: {item}" for item in observation.not_observed)
        valid_keys.add(key)

    pair_results: list[PerformancePairResult] = []
    token_reductions: list[Fraction] = []
    multi_reductions: list[Fraction] = []
    single_regressions: list[Fraction] = []
    all_regressions: list[Fraction] = []
    feasible_improvements: list[Fraction] = []
    disposition_improvements: list[Fraction] = []
    selected_pairs = 0
    blocked_pairs = 0
    for scenario in expected_manifest.scenarios:
        for seed in expected_manifest.order_seeds:
            baseline_key = (scenario.scenario_id, seed, "r31_baseline")
            engine_key = (scenario.scenario_id, seed, "skeleton_engine")
            if baseline_key not in valid_keys or engine_key not in valid_keys:
                continue
            baseline = cells_by_key[baseline_key]
            engine = cells_by_key[engine_key]
            baseline_observation = observations_by_key[baseline_key]
            engine_observation = observations_by_key[engine_key]
            if baseline.functional_result_digest != engine.functional_result_digest:
                failures.append(
                    f"baseline/engine functional result digest가 다릅니다: "
                    f"{(scenario.scenario_id, seed)}"
                )
                continue
            baseline_planning = baseline_observation.planning_usage.optimization_tokens
            engine_planning = engine_observation.planning_usage.optimization_tokens
            if baseline_planning != baseline.optimization_tokens or engine_planning != engine.optimization_tokens:
                failures.append(f"collector planning usage가 benchmark cell과 다릅니다: {(scenario.scenario_id, seed)}")
                continue
            baseline_tokens = baseline_planning
            engine_tokens = engine_planning
            reduction = _improvement(baseline_tokens, engine_tokens)
            disposition = _improvement(baseline.latency_ms_to_disposition, engine.latency_ms_to_disposition)
            if reduction is None or disposition is None:
                not_observed.append(
                    "NOT_OBSERVED: pair relative ratio denominator가 0입니다: "
                    f"{(scenario.scenario_id, seed)}"
                )
                continue
            first_feasible: Fraction | None = None
            if scenario.expected_disposition == "selected":
                if baseline.latency_ms_to_first_feasible is None or engine.latency_ms_to_first_feasible is None:
                    not_observed.append(f"NOT_OBSERVED: first feasible latency가 없습니다: {(scenario.scenario_id, seed)}")
                    continue
                first_feasible = _improvement(
                    baseline.latency_ms_to_first_feasible,
                    engine.latency_ms_to_first_feasible,
                )
                if first_feasible is None:
                    not_observed.append(
                        "NOT_OBSERVED: first feasible baseline denominator가 0입니다: "
                        f"{(scenario.scenario_id, seed)}"
                    )
                    continue
                feasible_improvements.append(first_feasible)
                selected_pairs += 1
            else:
                blocked_pairs += 1
            token_reductions.append(reduction)
            disposition_improvements.append(disposition)
            regression = -reduction
            all_regressions.append(regression)
            if scenario.path_kind == "multi_path":
                multi_reductions.append(reduction)
            else:
                single_regressions.append(regression)
            baseline_total = _sum_observed(
                (baseline_observation.planning_usage.total_tokens,)
                + (
                    (baseline_observation.lifecycle_usage.total_tokens,)
                    if assessment_stage == "final"
                    else ()
                )
            )
            engine_total = _sum_observed(
                (engine_observation.planning_usage.total_tokens,)
                + (
                    (engine_observation.lifecycle_usage.total_tokens,)
                    if assessment_stage == "final"
                    else ()
                )
            )
            if baseline_total is None or engine_total is None:
                not_observed.append(
                    "NOT_OBSERVED: pair의 단계별 실제 input+output token 총량이 완결되지 않았습니다: "
                    f"{(scenario.scenario_id, seed)}"
                )
            pair_results.append(
                PerformancePairResult(
                    scenario_id=scenario.scenario_id,
                    order_seed=seed,
                    path_kind=scenario.path_kind,
                    expected_disposition=scenario.expected_disposition,
                    baseline_cell_digest=sha256_digest(baseline),
                    engine_cell_digest=sha256_digest(engine),
                    baseline_safety_observation_digest=baseline_observation.observation_digest,
                    engine_safety_observation_digest=engine_observation.observation_digest,
                    baseline_optimization_tokens=baseline_tokens,
                    engine_optimization_tokens=engine_tokens,
                    baseline_total_tokens=baseline_total,
                    engine_total_tokens=engine_total,
                    baseline_latency_ms_to_first_feasible=baseline.latency_ms_to_first_feasible,
                    engine_latency_ms_to_first_feasible=engine.latency_ms_to_first_feasible,
                    baseline_latency_ms_to_disposition=baseline.latency_ms_to_disposition,
                    engine_latency_ms_to_disposition=engine.latency_ms_to_disposition,
                    token_reduction=float(reduction),
                    token_regression=float(regression),
                    time_to_first_feasible_improvement=None if first_feasible is None else float(first_feasible),
                    time_to_disposition_improvement=float(disposition),
                )
            )

    overall_mean = _mean(token_reductions)
    multi_median = _median(multi_reductions)
    worst_single = max(single_regressions) if single_regressions else None
    worst_cell = max(all_regressions) if all_regressions else None
    feasible_median = _median(feasible_improvements)
    disposition_median = _median(disposition_improvements)
    complete_pairs = len(pair_results) == expected_manifest.expected_pair_count
    if not complete_pairs:
        not_observed.append("NOT_OBSERVED: 18개 baseline/engine whole pair가 모두 완결되지 않았습니다.")
        overall_mean = None
        multi_median = None
        worst_single = None
        worst_cell = None
        feasible_median = None
        disposition_median = None

    selected_engine_cells = [
        cells_by_key[(scenario.scenario_id, seed, "skeleton_engine")]
        for scenario in expected_manifest.scenarios
        if scenario.expected_disposition == "selected"
        for seed in expected_manifest.order_seeds
        if (scenario.scenario_id, seed, "skeleton_engine") in valid_keys
    ]
    lifecycle_complete = all(
        item.lifecycle_observation is not None
        and item.lifecycle_evidence_digest is not None
        and item.lifecycle_observation.schema_version == "2.0"
        for item in selected_engine_cells
    ) and len(selected_engine_cells) == expected_manifest.expected_selected_pair_count
    if assessment_stage == "final" and lifecycle_complete:
        detailed = _sum_observed(tuple(item.detailed_task_count for item in selected_engine_cells))
        unexecuted = _sum_observed(
            tuple(item.unexecuted_detailed_task_count for item in selected_engine_cells)
        )
        unknown = any(
            item.lifecycle_observation is not None and item.lifecycle_observation.has_unknown_execution_effect
            for item in selected_engine_cells
        )
        candidate = sum(item.candidate_output_tokens for item in selected_engine_cells)
        discarded = sum(item.discarded_candidate_output_tokens for item in selected_engine_cells)
        unexecuted_ratio = (
            None
            if detailed is None or unexecuted is None or detailed == 0 or unknown
            else Fraction(unexecuted, detailed)
        )
        discarded_ratio = None if candidate == 0 else Fraction(discarded, candidate)
        if detailed is None or unexecuted is None:
            not_observed.append(
                "NOT_OBSERVED: 상세 Task 실행 비율의 분자 또는 분모가 일부 누락됐습니다."
            )
        elif unknown:
            not_observed.append(
                "NOT_OBSERVED: 상세 Task 실행 효과가 확정되지 않았습니다."
            )
    else:
        unexecuted_ratio = None
        discarded_ratio = None
    if unexecuted_ratio is None:
        not_observed.append("NOT_OBSERVED: 상세 Task 실행 비율의 동결 denominator가 없습니다.")
    if discarded_ratio is None:
        not_observed.append("NOT_OBSERVED: 폐기 후보 출력 비율의 동결 denominator가 없습니다.")

    floor_checks = (
        overall_mean is not None and overall_mean >= _fraction(policy.minimum_overall_mean_reduction),
        multi_median is not None and multi_median >= _fraction(policy.minimum_multi_path_median_reduction),
        worst_single is not None and worst_single <= _fraction(policy.maximum_worst_single_path_regression),
        worst_cell is not None and worst_cell <= _fraction(policy.maximum_worst_cell_token_regression),
        feasible_median is not None
        and feasible_median
        >= _fraction(policy.minimum_time_to_first_feasible_median_improvement),
        disposition_median is not None
        and disposition_median
        >= _fraction(policy.minimum_all_pair_disposition_median_improvement),
    )
    if complete_pairs:
        labels = (
            "overall mean reduction release floor 미달",
            "multi-path median reduction release floor 미달",
            "worst single-path regression release floor 초과",
            "worst cell token regression release floor 초과",
            "first feasible median improvement release floor 미달",
            "all-pair disposition median improvement release floor 미달",
        )
        failures.extend(label for label, passed in zip(labels, floor_checks, strict=True) if not passed)
    minimum_floor = complete_pairs and all(floor_checks)
    optimization_misses = _optimization_misses(
        multi_path_median_reduction=multi_median,
        overall_mean_reduction=overall_mean,
        worst_single_path_regression=worst_single,
        unexecuted_detail_ratio=unexecuted_ratio,
        discarded_candidate_output_ratio=discarded_ratio,
        time_to_first_feasible_median_improvement=feasible_median,
        policy=policy,
    )
    manifest_complete = (
        not duplicate_cell_keys
        and not duplicate_observation_keys
        and not missing_cells
        and not unexpected_cells
        and not missing_observations
        and not unexpected_observations
        and len(valid_keys) == expected_manifest.expected_cell_count
        and complete_pairs
    )
    pair_totals_observed = complete_pairs and all(
        item.baseline_total_tokens is not None and item.engine_total_tokens is not None
        for item in pair_results
    )
    planning_passed = (
        binding_valid
        and manifest_complete
        and functional_safety
        and minimum_floor
        and pair_totals_observed
    )
    optimization_observed = all(
        value is not None
        for value in (
            multi_median,
            overall_mean,
            worst_single,
            unexecuted_ratio,
            discarded_ratio,
            feasible_median,
        )
    )
    optimization_passed = optimization_observed and not optimization_misses
    release_floor = assessment_stage == "final" and planning_passed and optimization_observed
    scope_gate, _ = _all_scope_gate(scope_results)
    cutover_eligible = release_floor and scope_gate
    return PerformanceQualificationReport(
        assessment_stage=assessment_stage,
        contract_digest=contract.contract_digest,
        source_manifest_digest=contract.source_manifest_digest,
        scenario_set_digest=expected_manifest.scenario_set_digest,
        expected_manifest_digest=expected_manifest.manifest_digest,
        threshold_policy=policy,
        performance_threshold_policy_digest=policy.policy_digest,
        observed_cell_count=len(cells_by_key),
        observed_pair_count=len(pair_results),
        observed_selected_pair_count=selected_pairs,
        observed_blocked_pair_count=blocked_pairs,
        cell_digests=tuple(sorted(sha256_digest(item) for item in cells)),
        safety_observation_digests=tuple(sorted(item.observation_digest for item in safety_observations)),
        source_evidence_digests=tuple(sorted({item.source_evidence_digest for item in safety_observations})),
        scope_results=scope_results,
        pair_results=tuple(pair_results),
        overall_mean_reduction=None if overall_mean is None else float(overall_mean),
        multi_path_median_reduction=None if multi_median is None else float(multi_median),
        worst_single_path_regression=None if worst_single is None else float(worst_single),
        worst_cell_token_regression=None if worst_cell is None else float(worst_cell),
        time_to_first_feasible_median_improvement=None if feasible_median is None else float(feasible_median),
        all_pair_disposition_median_improvement=None if disposition_median is None else float(disposition_median),
        unexecuted_detail_ratio=None if unexecuted_ratio is None else float(unexecuted_ratio),
        discarded_candidate_output_ratio=None if discarded_ratio is None else float(discarded_ratio),
        manifest_complete=manifest_complete,
        functional_safety_passed=(
            binding_valid
            and functional_safety
            and len(valid_keys) == expected_manifest.expected_cell_count
        ),
        minimum_performance_floor_passed=minimum_floor,
        release_floor_passed=release_floor,
        optimization_targets_passed=optimization_passed,
        optimization_followups_required=(release_floor and bool(optimization_misses)),
        optimization_misses=optimization_misses,
        planning_assessment_passed=planning_passed,
        cutover_eligible=cutover_eligible,
        failures=tuple(dict.fromkeys(failures)),
        not_observed=tuple(dict.fromkeys(not_observed)),
    )


def evaluate_release_cutover_gate(
    *, performance_report: PerformanceQualificationReport
) -> ReleaseCutoverReport:
    performance_report = PerformanceQualificationReport.model_validate(
        performance_report.model_dump()
    )
    scope_gate, missing = _all_scope_gate(performance_report.scope_results)
    failures: list[str] = []
    if performance_report.assessment_stage != "final":
        failures.append("final PerformanceQualificationReport v4.0만 release cutover 근거입니다.")
    if not performance_report.manifest_complete:
        failures.append("performance manifest가 완결되지 않았습니다.")
    if not performance_report.functional_safety_passed:
        failures.append("performance 기능·안전 검증이 통과하지 않았습니다.")
    if not performance_report.release_floor_passed:
        failures.append("필수 performance release floor가 통과하지 않았습니다.")
    if not scope_gate:
        failures.append("네 qualification scope가 모두 PASS가 아닙니다.")
    if performance_report.failures:
        failures.extend(performance_report.failures)
    passed = not failures and performance_report.cutover_eligible
    return ReleaseCutoverReport(
        performance_report_digest=sha256_digest(performance_report),
        performance_report=performance_report,
        missing_scopes=missing,
        passed=passed,
        failures=tuple(dict.fromkeys(failures)),
    )
