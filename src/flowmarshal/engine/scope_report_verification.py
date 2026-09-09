"""완료된 qualification scope report를 저장 checkpoint에서 읽기 전용으로 재검증한다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canonical import sha256_digest
from .e2e_qualification import E2E_SCENARIOS
from .evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    ImmutableCheckpointStore,
    evaluate_role_fixtures,
)
from .evaluation_budget import verify_metadata_digest
from .role_observations import RoleCallReceipt
from .qualification import (
    ORDER_SEEDS,
    PlanningScenarioCatalog,
    ScopeQualificationReport,
    _combined_role_catalog,
    _deterministic_contract,
    _checkpoint_fixture_result,
    source_manifest_digest,
    qualification_suite_manifest,
)
from .qualification_manifest import (
    QualificationCellOutcome,
    evaluate_qualification_responsibilities,
    verify_candidate_wheel_metadata,
    verify_qualification_reproduction_bundle,
)


@dataclass(frozen=True)
class ScopeReportVerification:
    """원본 checkpoint 재집계와 scope report의 대조 결과다."""

    valid: bool
    errors: tuple[str, ...]
    recalculated_metrics: dict[str, Any]
    recalculated_failures: tuple[str, ...]
    recalculated_passed: bool


def _error(errors: list[str], code: str) -> None:
    errors.append(code)


def _same(actual: Any, expected: Any, *, field: str, errors: list[str]) -> None:
    if actual != expected:
        _error(errors, f"SCOPE_REPORT_{field}_MISMATCH")


def _completed_cells(
    *, run_root: Path, contract: EvaluationContract, errors: list[str]
) -> tuple[EvaluationCellCheckpoint, ...]:
    """계약의 정확한 matrix와 run-state를 확인하고 cell을 읽는다.

    ``ImmutableCheckpointStore.initialize``는 상태 파일을 쓸 수 있으므로 호출하지 않는다.
    """
    store = ImmutableCheckpointStore(run_root, contract)
    try:
        state = store.state()
    except Exception:
        _error(errors, "SCOPE_REPORT_RUN_STATE_INVALID")
        return ()
    if state.status is not EvaluationRunStatus.COMPLETED:
        _error(errors, "SCOPE_REPORT_RUN_NOT_COMPLETED")
    if state.expected_cell_count != contract.expected_cell_count:
        _error(errors, "SCOPE_REPORT_RUN_STATE_EXPECTED_COUNT_MISMATCH")
    if state.completed_cell_count != contract.expected_cell_count:
        _error(errors, "SCOPE_REPORT_RUN_STATE_COMPLETED_COUNT_MISMATCH")

    expected_paths: set[Path] = set()
    result: list[EvaluationCellCheckpoint] = []
    for seed in contract.order_seeds:
        for digest in contract.fixture_digests:
            path = store._path(digest, seed)
            expected_paths.add(path.resolve())
            try:
                checkpoint = store.completed(digest, seed)
            except Exception:
                checkpoint = None
                _error(errors, "SCOPE_REPORT_CHECKPOINT_INVALID")
            if checkpoint is None:
                _error(errors, "SCOPE_REPORT_EXPECTED_CHECKPOINT_MISSING")
                continue
            if (
                checkpoint.model_lock_format != contract.model_lock_format
                or checkpoint.contract_digest != contract.contract_digest
                or checkpoint.fixture_digest != digest
                or checkpoint.order_seed != seed
                or not checkpoint.completed
            ):
                _error(errors, "SCOPE_REPORT_CHECKPOINT_BINDING_MISMATCH")
            if not checkpoint.runner_receipts:
                _error(errors, "SCOPE_REPORT_RECEIPT_MISSING")
            result.append(checkpoint)

    cells_root = run_root / "cells"
    if cells_root.is_dir():
        for path in cells_root.rglob("*.json"):
            if path.resolve() not in expected_paths:
                _error(errors, "SCOPE_REPORT_UNEXPECTED_CHECKPOINT")
                break
    return tuple(result)


def _deterministic(
    cells: tuple[EvaluationCellCheckpoint, ...], errors: list[str]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    names = (
        "compileall", "full-test-suite", "pip-check", "synthetic-lifecycle", "legacy-freeze-manifest",
    )
    observations: list[dict[str, Any]] = []
    for checkpoint, name in zip(cells, names, strict=False):
        raw = checkpoint.raw_structured_assessment
        if raw.get("name") != name or not isinstance(raw.get("passed"), bool):
            _error(errors, "SCOPE_REPORT_DETERMINISTIC_RAW_INVALID")
            continue
        receipts = checkpoint.runner_receipts
        if len(receipts) != 1 or receipts[0] != {"runner": "local", "check": name, "completed": True}:
            _error(errors, "SCOPE_REPORT_DETERMINISTIC_RECEIPT_BINDING_MISMATCH")
        if name == "legacy-freeze-manifest":
            report = raw.get("report")
            if not isinstance(report, dict) or not isinstance(report.get("passed"), bool):
                _error(errors, "SCOPE_REPORT_FREEZE_OBSERVATION_INVALID")
        else:
            if not isinstance(raw.get("returncode"), int):
                _error(errors, "SCOPE_REPORT_DETERMINISTIC_EXIT_INVALID")
            elif bool(raw["passed"]) != (raw["returncode"] == 0):
                _error(errors, "SCOPE_REPORT_DETERMINISTIC_EXIT_PASS_MISMATCH")
        observations.append(raw)
    if len(cells) != len(names):
        _error(errors, "SCOPE_REPORT_DETERMINISTIC_CELL_COUNT_INVALID")
    failures = tuple(f"{item['name']}: FAIL" for item in observations if not item["passed"])
    return {"check_count": len(observations), "failure_count": len(failures)}, failures


def _role(
    *, root: Path, contract: EvaluationContract,
    cells: tuple[EvaluationCellCheckpoint, ...], errors: list[str]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    try:
        for checkpoint in cells:
            for receipt in checkpoint.runner_receipts:
                RoleCallReceipt.model_validate(receipt)
        results = tuple(_checkpoint_fixture_result(item) for item in cells)
        report = evaluate_role_fixtures(
            catalog=_combined_role_catalog(root), results=results,
            prompt_digest=contract.prompt_digest,
            output_schema_digest=contract.output_schema_digest,
            model_lock_digest=contract.model_lock_digest,
        )
    except Exception:
        _error(errors, "SCOPE_REPORT_ROLE_RAW_INVALID")
        return {}, ()
    return report.metrics.model_dump(mode="json"), report.failures


def _planning(
    *, root: Path, cells: tuple[EvaluationCellCheckpoint, ...], errors: list[str]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    catalog = PlanningScenarioCatalog.load(
        root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    for checkpoint in cells:
        for receipt in checkpoint.runner_receipts:
            try:
                RoleCallReceipt.model_validate(receipt)
            except Exception:
                _error(errors, "SCOPE_REPORT_PLANNING_RECEIPT_BINDING_MISMATCH")
    by_digest = {item.scenario_digest: item for item in catalog.scenarios}
    raws: list[dict[str, Any]] = []
    for checkpoint in cells:
        raw = checkpoint.raw_structured_assessment
        scenario = by_digest.get(checkpoint.fixture_digest)
        if scenario is None or (
            raw.get("scenario_id") != scenario.scenario_id
            or raw.get("scenario_digest") != scenario.scenario_digest
            or raw.get("order_seed") != checkpoint.order_seed
            or raw.get("expected_disposition") != scenario.expected_disposition
            or not isinstance(raw.get("passed"), bool)
        ):
            _error(errors, "SCOPE_REPORT_PLANNING_RAW_BINDING_MISMATCH")
            continue
        raws.append(raw)
    failures = tuple(
        f"{item['scenario_id']}/seed-{item['order_seed']}: {item.get('failure') or 'FAIL'}"
        for item in raws if not item["passed"]
    )
    metrics = {
        "cell_count": len(raws),
        "clean_selected_count": sum(1 for item in raws if item["expected_disposition"] == "selected" and item.get("selected")),
        "adversarial_blocked_count": sum(1 for item in raws if item["expected_disposition"] == "blocked" and item["passed"] and not item.get("selected", False)),
        "max_logical_role_calls": max((int(item.get("logical_role_calls", 0)) for item in raws), default=0),
        "max_candidate_versions": max((int(item.get("candidate_versions", 0)) for item in raws), default=0),
        "schema_failure_count": sum(1 for item in raws if item.get("schema_valid") is False),
    }
    return metrics, failures


def _project_e2e(
    *,
    root: Path,
    run_root: Path,
    contract: EvaluationContract,
    cells: tuple[EvaluationCellCheckpoint, ...],
    errors: list[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    raws: list[dict[str, Any]] = []
    outcomes: list[QualificationCellOutcome] = []
    expected_cell_bindings: dict[str, tuple[str, int]] = {}
    for checkpoint, scenario in zip(cells, E2E_SCENARIOS, strict=False):
        raw = checkpoint.raw_structured_assessment
        if raw.get("scenario") != scenario or raw.get("order_seed") != 0 or not isinstance(raw.get("passed"), bool):
            _error(errors, "SCOPE_REPORT_E2E_RAW_BINDING_MISMATCH")
            continue
        receipt = checkpoint.runner_receipts[0] if checkpoint.runner_receipts else {}
        events = receipt.get("events") if isinstance(receipt, dict) else None
        if not isinstance(events, list) or receipt.get("events_digest") != sha256_digest(events):
            _error(errors, "SCOPE_REPORT_E2E_RECEIPT_BINDING_MISMATCH")
        try:
            outcome = QualificationCellOutcome.model_validate(
                raw["qualification_outcome"]
            )
            if outcome.cell_id != scenario:
                _error(errors, "SCOPE_REPORT_E2E_EVIDENCE_CELL_MISMATCH")
            outcomes.append(outcome)
            expected_cell_bindings[outcome.cell_id] = (
                checkpoint.fixture_digest,
                checkpoint.order_seed,
            )
        except Exception:
            _error(errors, "SCOPE_REPORT_E2E_RESPONSIBILITY_EVIDENCE_MISSING")
        raws.append(raw)
    cell_failures = tuple(
        f"{item['scenario']}: {item.get('failure') or item.get('error') or 'FAIL'}"
        for item in raws if not item["passed"]
    )
    try:
        freeze_bundle_digest = verify_qualification_reproduction_bundle(
            run_root / "reproduction-bundle"
        ).bundle_digest
    except Exception:
        freeze_bundle_digest = None
        _error(errors, "SCOPE_REPORT_E2E_FREEZE_BUNDLE_INVALID")
    candidate_binding = None
    try:
        metadata = json.loads(
            (run_root / "run-metadata.json").read_text(encoding="utf-8")
        )
        verify_metadata_digest(metadata)
        candidate_binding = verify_candidate_wheel_metadata(metadata)
        candidate_wheel_digest = candidate_binding.wheel_digest
    except Exception:
        candidate_wheel_digest = None
        _error(errors, "SCOPE_REPORT_E2E_CANDIDATE_WHEEL_INVALID")
    responsibility_report = evaluate_qualification_responsibilities(
        qualification_suite_manifest(root),
        tuple(outcomes),
        evaluation_contract_digest=contract.contract_digest,
        run_root=run_root,
        expected_cell_bindings=expected_cell_bindings,
        expected_freeze_bundle_digest=freeze_bundle_digest,
        expected_candidate_wheel_digest=candidate_wheel_digest,
        expected_candidate_wheel_binding_digest=(
            None if candidate_binding is None else candidate_binding.binding_digest
        ),
        expected_candidate_distribution_name=(
            None if candidate_binding is None else candidate_binding.distribution_name
        ),
        expected_candidate_distribution_version=(
            None if candidate_binding is None else candidate_binding.distribution_version
        ),
    )
    failures = tuple((*cell_failures, *responsibility_report.failures))
    metrics = {
        "cell_count": len(raws),
        "passed_cell_count": sum(1 for item in raws if item["passed"]),
        "actual_codex_cell_count": len(raws),
        "duplicate_effect_count": sum(1 for item in raws if item["scenario"] == "unknown-receipt-no-duplicate" and item.get("thread_create_count") != 1),
        "responsibility_count": responsibility_report.responsibility_count,
        "passed_responsibility_count": responsibility_report.passed_responsibility_count,
        "not_run_responsibility_count": len(
            responsibility_report.not_run_responsibility_ids
        ),
    }
    return metrics, failures


def verify_scope_report(
    *, root: Path | str, run_root: Path | str, report: ScopeQualificationReport
) -> ScopeReportVerification:
    """저장된 run의 원본 cell을 재집계한다. 이 함수는 파일·provider를 변경하지 않는다."""
    errors: list[str] = []
    base = Path(root).resolve(strict=True)
    destination = Path(run_root).resolve(strict=True)
    try:
        contract = EvaluationContract.model_validate_json(
            (destination / "evaluation-contract.json").read_text(encoding="utf-8")
        )
    except Exception:
        return ScopeReportVerification(False, ("SCOPE_REPORT_CONTRACT_INVALID",), {}, (), False)
    if contract.contract_digest != report.contract_digest or contract.scope != report.scope:
        _error(errors, "SCOPE_REPORT_CONTRACT_BINDING_MISMATCH")
    if contract.source_manifest_digest != source_manifest_digest(base):
        _error(errors, "SCOPE_REPORT_SOURCE_MANIFEST_MISMATCH")
    if contract.expected_cell_count != len(contract.fixture_digests) * len(contract.order_seeds):
        _error(errors, "SCOPE_REPORT_EXPECTED_MATRIX_INVALID")
    cells = _completed_cells(run_root=destination, contract=contract, errors=errors)
    if report.status is not EvaluationRunStatus.COMPLETED:
        _error(errors, "SCOPE_REPORT_STATUS_NOT_COMPLETED")

    if report.scope.value == "deterministic_schema_dag_ledger":
        expected = _deterministic_contract(base)
        if contract.contract_digest != expected.contract_digest:
            _error(errors, "SCOPE_REPORT_DETERMINISTIC_CONTRACT_MISMATCH")
        metrics, failures = _deterministic(cells, errors)
    elif report.scope.value == "goal_reviewer_role_fixture":
        metrics, failures = _role(root=base, contract=contract, cells=cells, errors=errors)
    elif report.scope.value == "full_skeleton_to_selection_pipeline":
        if contract.order_seeds != ORDER_SEEDS:
            _error(errors, "SCOPE_REPORT_PLANNING_ORDER_SEEDS_MISMATCH")
        metrics, failures = _planning(root=base, cells=cells, errors=errors)
    elif report.scope.value == "activation_execution_validation_restart_e2e":
        metrics, failures = _project_e2e(
            root=base,
            run_root=destination,
            contract=contract,
            cells=cells,
            errors=errors,
        )
    else:  # pydantic EvaluationScope가 막지만 미래 enum 변경에는 fail closed 한다.
        _error(errors, "SCOPE_REPORT_SCOPE_UNSUPPORTED")
        metrics, failures = {}, ()
    passed = not failures
    _same(report.metrics, metrics, field="METRICS", errors=errors)
    _same(report.failures, failures, field="FAILURES", errors=errors)
    _same(report.passed, passed, field="PASSED", errors=errors)
    return ScopeReportVerification(not errors, tuple(errors), metrics, failures, passed)
