"""성능 원본 run을 재검증하고 단계별 불변 assessment를 만드는 조합기."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..canonical import sha256_digest
from .benchmark_observation import observe_benchmark_execution_checkpoint
from .benchmark_safety import observe_benchmark_safety
from .evaluation import (
    BenchmarkCell, EvaluationContract, ImmutableCheckpointStore, ScopeGateResult,
)
from .evaluation_budget import policies_from_metadata, policy_contract_fragment, verify_metadata_digest
from .models import EngineRoleConfiguration
from .performance import (
    PerformanceExpectedManifestIdentity, PerformanceQualificationReport,
    PerformanceScenarioIdentity, PerformanceThresholdPolicy, evaluate_performance_qualification,
)
from .qualification import ORDER_SEEDS, PlanningScenarioCatalog, ScopeQualificationReport, source_manifest_digest


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("PERFORMANCE_OBJECT_REQUIRED")
    return value


def performance_threshold_digest(policies: Any, performance_policy: PerformanceThresholdPolicy) -> str:
    from .benchmark import MEASUREMENT_RULES, _implementation_runtime_contract
    return sha256_digest(
        MEASUREMENT_RULES["thresholds"] | policy_contract_fragment(policies)
        | {"implementation_runtime": _implementation_runtime_contract(policies),
           "performance_threshold_policy_digest": performance_policy.policy_digest}
    )


def performance_manifest(catalog: PlanningScenarioCatalog, neutral: dict[str, Any], contract: EvaluationContract) -> PerformanceExpectedManifestIdentity:
    if set(neutral) != {scenario.scenario_id for scenario in catalog.scenarios} or sha256_digest(neutral) != contract.scenario_set_digest:
        raise ValueError("PERFORMANCE_NEUTRAL_INPUT_MANIFEST_MISMATCH")
    manifest = PerformanceExpectedManifestIdentity(
        scenario_set_digest=contract.scenario_set_digest,
        scenarios=tuple(PerformanceScenarioIdentity(
            scenario_id=scenario.scenario_id, scenario_digest=scenario.scenario_digest,
            path_kind=scenario.path_kind, expected_disposition=scenario.expected_disposition,
            neutral_input_digest=sha256_digest(neutral[scenario.scenario_id]),
        ) for scenario in catalog.scenarios),
    )
    fixture_ids = {sha256_digest({"scenario": scenario.scenario_digest, "implementation": implementation,
                                 "neutral": sha256_digest(neutral[scenario.scenario_id])})
                   for scenario in catalog.scenarios for implementation in manifest.implementations}
    if set(contract.fixture_digests) != fixture_ids or contract.order_seeds != ORDER_SEEDS or contract.expected_cell_count != manifest.expected_cell_count:
        raise ValueError("PERFORMANCE_CONTRACT_MATRIX_MISMATCH")
    return manifest


def load_bound_performance_policy(metadata: dict[str, Any], contract: EvaluationContract, root: Path) -> PerformanceThresholdPolicy:
    from .benchmark import MEASUREMENT_RULES
    verify_metadata_digest(metadata)
    policies = policies_from_metadata(metadata)
    body = metadata.get("performance_threshold_policy")
    if not isinstance(body, dict):
        raise ValueError("PERFORMANCE_POLICY_NOT_OBSERVED: v3.0 결과는 새 최소선으로 승격할 수 없습니다.")
    policy = PerformanceThresholdPolicy.model_validate_json(json.dumps(body))
    source_policy = PerformanceThresholdPolicy.load(root / "config" / "pre-1.0-performance-thresholds.json")
    if (metadata.get("performance_threshold_policy_digest") != policy.policy_digest
            or policy != source_policy or contract.threshold_digest != performance_threshold_digest(policies, policy)
            or contract.rules_digest != sha256_digest(MEASUREMENT_RULES)):
        raise ValueError("PERFORMANCE_POLICY_CONTRACT_DIGEST_MISMATCH")
    return policy


def build_performance_assessment(*, root: Path, run_root: Path, scope_reports: tuple[ScopeQualificationReport, ...], assessment_stage: str) -> tuple[PerformanceQualificationReport, dict[str, Any]]:
    """원래 checkpoint와 현재 원장을 대조한다. provider를 호출하지 않는다."""
    from .benchmark import benchmark_cell
    root, run_root = root.resolve(strict=True), run_root.resolve(strict=True)
    contract = EvaluationContract.model_validate(_json(run_root / "evaluation-contract.json"))
    metadata = _json(run_root / "run-metadata.json")
    policy = load_bound_performance_policy(metadata, contract, root)
    policies = policies_from_metadata(metadata)
    if (metadata.get("scope") != "benchmark" or metadata.get("evaluation_contract_digest") != contract.contract_digest
            or contract.source_manifest_digest != source_manifest_digest(root)
            or sha256_digest(EngineRoleConfiguration.model_validate(metadata["role_configuration"])) != contract.role_configuration_digest):
        raise ValueError("PERFORMANCE_RUN_SOURCE_OR_ROLE_BINDING_MISMATCH")
    catalog = PlanningScenarioCatalog.load(root / "tests/fixtures/engine/planning-scenarios.json")
    neutral = _json(run_root / "neutral-inputs.json")
    manifest = performance_manifest(catalog, neutral, contract)
    expected = {sha256_digest({"scenario": scenario.scenario_digest, "implementation": implementation,
                               "neutral": sha256_digest(neutral[scenario.scenario_id])}): (scenario, implementation)
                for scenario in catalog.scenarios for implementation in manifest.implementations}
    store = ImmutableCheckpointStore(run_root, contract)
    cells, safety, evidence, lifecycle, missing_cells = [], [], [], [], []
    for seed in manifest.order_seeds:
        for identity in contract.fixture_digests:
            scenario, implementation = expected[identity]
            checkpoint = store.completed(identity, seed)
            if checkpoint is None:
                missing_cells.append({"scenario_id": scenario.scenario_id, "fixture_digest": identity, "order_seed": seed, "implementation": implementation})
                continue
            if checkpoint.contract_digest != contract.contract_digest or checkpoint.fixture_digest != identity or checkpoint.order_seed != seed or not checkpoint.completed:
                raise ValueError("PERFORMANCE_CHECKPOINT_BINDING_MISMATCH")
            raw = checkpoint.raw_structured_assessment["raw"]
            original = BenchmarkCell.model_validate(checkpoint.raw_structured_assessment["benchmark_cell"])
            if benchmark_cell(scenario, seed, implementation, contract.model_lock_digest, raw) != original or tuple(raw["receipts"]) != checkpoint.runner_receipts:
                raise ValueError("PERFORMANCE_CELL_RECALCULATION_MISMATCH")
            cell = original
            lifecycle_error = None
            if assessment_stage == "final" and implementation == "skeleton_engine" and original.disposition == "selected":
                try:
                    observation = observe_benchmark_execution_checkpoint(checkpoint, run_root=run_root, contract=contract)
                    cell = observation.cell
                    lifecycle.append(observation.model_dump(mode="json", exclude={"cell"}))
                except (ValueError, RuntimeError, OSError, KeyError) as error:
                    lifecycle_error = str(error)
                    lifecycle.append({"checkpoint_digest": checkpoint.checkpoint_digest, "status": "not_observed", "reason": lifecycle_error})
            observation, raw_evidence = observe_benchmark_safety(checkpoint=checkpoint, cell=cell, run_root=run_root, policies=policies, assessment_stage=assessment_stage)
            if lifecycle_error is not None:
                raw_evidence["lifecycle_error"] = lifecycle_error
                observation = observation.model_copy(update={
                    "complete": False, "safety_passed": False,
                    "not_observed": (*observation.not_observed, "PERFORMANCE_LIFECYCLE_NOT_OBSERVED: " + lifecycle_error),
                    "source_evidence_digest": sha256_digest(raw_evidence),
                })
            cells.append(cell)
            safety.append(observation)
            evidence.append(raw_evidence)
    scopes = tuple(ScopeGateResult(scope=report.scope, contract_digest=report.contract_digest,
                                  artifact_digest=report.report_digest, passed=report.passed, failures=report.failures)
                   for report in scope_reports)
    report = evaluate_performance_qualification(expected_manifest=manifest, contract=contract, policy=policy,
        cells=tuple(cells), safety_observations=tuple(safety), scope_results=scopes, assessment_stage=assessment_stage)
    bundle = {
        "format": "flowmarshal-performance-assessment-v1", "assessment_stage": assessment_stage,
        "source_run_root": str(run_root), "contract_digest": contract.contract_digest,
        "source_manifest_digest": contract.source_manifest_digest,
        "performance_threshold_policy_digest": policy.policy_digest,
        "metadata_digest": metadata["metadata_digest"], "expected_manifest": manifest.model_dump(mode="json"),
        "cells": [cell.model_dump(mode="json") for cell in cells],
        "safety_observations": [item.model_dump(mode="json") for item in safety],
        "safety_evidence": evidence, "lifecycle_observations": lifecycle,
        "missing_cells": missing_cells, "scope_report_digests": [item.report_digest for item in scope_reports],
        "report_digest": sha256_digest(report),
    }
    return report, bundle


def optimization_followups(report: PerformanceQualificationReport) -> dict[str, Any]:
    """evaluator가 exact 수치로 고정한 miss만 1.0.x 후속으로 투영한다."""
    policy = report.threshold_policy
    metrics = {
        "multi_path_median_reduction": (policy.optimization_minimum_multi_path_median_reduction, ">="),
        "overall_mean_reduction": (policy.optimization_minimum_overall_mean_reduction, ">="),
        "worst_single_path_regression": (policy.optimization_maximum_worst_single_path_regression, "<="),
        "unexecuted_detail_ratio": (policy.optimization_maximum_unexecuted_detail_ratio, "<="),
        "discarded_candidate_output_ratio": (policy.optimization_maximum_discarded_candidate_output_ratio, "<="),
        "time_to_first_feasible_median_improvement": (policy.optimization_minimum_time_to_first_feasible_median_improvement, ">="),
    }
    items = []
    for name in report.optimization_misses:
        threshold, direction = metrics[name]
        value = getattr(report, name)
        if value is None:
            raise ValueError("미관측 optimization metric은 follow-up miss가 될 수 없습니다.")
        items.append({"metric": name, "measured": value, "target": str(threshold), "direction": direction,
            "gap": value-float(threshold), "target_release": "1.0.x",
            "release_assignment_rule": "계약·설계 변경이 필요한 개선으로 확정되면 1.1로 분류한다.",
            "cell_digests": list(report.cell_digests), "pair_results": [item.model_dump(mode="json") for item in report.pair_results],
            "safety_passed": report.functional_safety_passed,
            "proposal": "해당 cell의 호출·후보·지연 근거로 개선 원인을 분리하고 별도 변경으로 검증한다.",
            "remeasurement": "동일 고정 정책·manifest의 전체 performance36 및 lifecycle12를 새 동결 소스로 재측정한다."})
    return {"format": "flowmarshal-performance-optimization-followups-v1", "required": report.optimization_followups_required,
            "performance_report_digest": sha256_digest(report), "items": items}


def write_performance_assessment(run_root: Path, report: PerformanceQualificationReport, bundle: dict[str, Any]) -> Path:
    from .eval_cli import _write_immutable_assessment
    directory = "lifecycle-assessments" if report.assessment_stage == "final" else "performance-assessments"
    destination = run_root / directory / sha256_digest(bundle)[7:]
    _write_immutable_assessment(destination / "assessment.json", bundle)
    _write_immutable_assessment(destination / "performance-qualification-report.json", report.model_dump(mode="json"))
    _write_immutable_assessment(destination / "optimization-followups.json", optimization_followups(report))
    return destination
