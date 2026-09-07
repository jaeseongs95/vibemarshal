from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from openai_codex.errors import CodexError

from ..canonical import sha256_digest
from .benchmark_observation import (
    BenchmarkObservationError,
    observe_benchmark_execution_checkpoint,
)
from .e2e_qualification import run_project_e2e
from .evaluation import (
    BenchmarkCell,
    EvaluationContract,
    EvaluationScope,
    ImmutableCheckpointStore,
    CheckpointContractError,
    ScopeGateResult,
    TokenLatencyGateReport,
    evaluate_cutover_gate,
    evaluate_token_latency_gate,
)
from .models import EngineRoleConfiguration
from .models import AssignmentResolutionError
from .plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V1,
    PLAN_INSPECTION_PROVIDER_V2,
)
from .qualification import (
    QualificationRunError,
    ORDER_SEEDS,
    PlanningScenarioCatalog,
    ScopeQualificationReport,
    default_role_configuration,
    project_root,
    resume_run,
    run_deterministic,
    run_full_planning_pipeline,
    run_role_fixture,
    source_manifest_digest,
)
from .roles import StructuredRoleError
from .runtime import RuntimePolicyError
from .evaluation_budget import (
    EvaluationPolicies,
    load_evaluation_policies,
    policies_from_metadata,
    verify_metadata_digest,
)


def _json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _emit(value: Any) -> None:
    document = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    print(json.dumps(document, ensure_ascii=False, indent=2))


def _roles(path: str | None, root: Path) -> EngineRoleConfiguration:
    return (
        default_role_configuration(root)
        if path is None
        else EngineRoleConfiguration.model_validate_json(Path(path).read_text(encoding="utf-8"))
    )


def _evaluation_policies(arguments: argparse.Namespace) -> EvaluationPolicies:
    if not arguments.budget_policy or not arguments.role_timeout_policy:
        raise QualificationRunError(
            "실제 qualification에는 --budget-policy와 --role-timeout-policy가 필요합니다."
        )
    return load_evaluation_policies(
        budget_policy_path=arguments.budget_policy,
        role_timeout_policy_path=arguments.role_timeout_policy,
        codex_project_binding_path=getattr(arguments, "codex_project_binding", None),
    )


def _run(arguments: argparse.Namespace) -> int:
    root = Path(arguments.project_root).resolve(strict=True)
    destination = None if arguments.run_root is None else Path(arguments.run_root).resolve()
    if arguments.scope == "deterministic":
        run_root, report = run_deterministic(root=root, run_root=destination)
    else:
        roles = _roles(arguments.role_config, root)
        policies = _evaluation_policies(arguments)
        if arguments.scope == "role-fixture":
            run_root, report = run_role_fixture(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
                evaluation_policies=policies,
            )
        elif arguments.scope == "full-planning-pipeline":
            run_root, report = run_full_planning_pipeline(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
                inspection_provider_contract=arguments.inspection_contract,
                evaluation_policies=policies,
            )
        else:
            run_root, report = run_project_e2e(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
                evaluation_policies=policies,
            )
    _emit(
        {
            "run_root": str(run_root),
            "report_digest": report.report_digest,
            "report": report.model_dump(mode="json"),
        }
    )
    return 0 if report.passed else 1


def _resume(arguments: argparse.Namespace) -> int:
    metadata = _json(Path(arguments.run_root) / "run-metadata.json")
    if metadata.get("scope") == "benchmark":
        from .benchmark import run_benchmark
        verify_metadata_digest(metadata)
        contract = EvaluationContract.model_validate(
            _json(Path(arguments.run_root) / "evaluation-contract.json")
        )
        if metadata.get("evaluation_contract_digest") != contract.contract_digest:
            raise QualificationRunError(
                "benchmark metadata와 evaluation contract digest가 다릅니다."
            )
        policies = policies_from_metadata(metadata)
        run_root, report = run_benchmark(
            root=Path(metadata["project_root"]), run_root=Path(arguments.run_root),
            role_configuration=EngineRoleConfiguration.model_validate(metadata["role_configuration"]),
            codex_bin=metadata.get("codex_bin"),
            scope_reports=tuple(ScopeQualificationReport.model_validate(item) for item in metadata.get("scope_reports", [])),
            inspection_provider_contract=metadata.get(
                "inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1
            ),
            evaluation_policies=policies,
        )
        _emit({"run_root": str(run_root), "report_digest": report.report_digest, "report": report.model_dump(mode="json")})
        return 0 if report.passed else 1
    run_root, report = resume_run(arguments.run_root)
    _emit(
        {
            "run_root": str(run_root),
            "report_digest": report.report_digest,
            "report": report.model_dump(mode="json"),
        }
    )
    return 0 if report.passed else 1


def _load_cells(path: Path | str) -> tuple[BenchmarkCell, ...]:
    document = _json(path)
    values = document.get("cells") if isinstance(document, dict) else document
    if not isinstance(values, list):
        raise QualificationRunError("benchmark 입력은 cell 배열 또는 {cells:[...]}여야 합니다.")
    return tuple(BenchmarkCell.model_validate(item) for item in values)


def _scope_reports(paths: list[str] | None) -> tuple[ScopeQualificationReport, ...]:
    return tuple(
        ScopeQualificationReport.model_validate(_json(path)) for path in (paths or [])
    )


def _bound_scope_reports(
    paths: list[str] | None,
    root: Path,
    *,
    inspection_provider_contract: str = PLAN_INSPECTION_PROVIDER_V1,
    require_checkpoint_evidence: bool = False,
) -> tuple[ScopeQualificationReport, ...]:
    reports = _scope_reports(paths)
    if len({report.scope for report in reports}) != len(reports):
        raise QualificationRunError("같은 scope의 보고서를 중복해 선행 Gate를 충족할 수 없습니다.")
    locks = []
    for path, report in zip(paths or [], reports, strict=True):
        contract = EvaluationContract.model_validate(_json(Path(path).parent / "evaluation-contract.json"))
        if contract.contract_digest != report.contract_digest or contract.scope != report.scope:
            raise QualificationRunError("scope report와 원본 evaluation 계약이 다릅니다.")
        if contract.source_manifest_digest != source_manifest_digest(root):
            raise QualificationRunError("현재 source와 다른 scope report는 cutover 근거가 아닙니다.")
        if require_checkpoint_evidence:
            from .scope_report_verification import verify_scope_report
            verified = verify_scope_report(root=root, run_root=Path(path).parent, report=report)
            if not verified.valid:
                raise QualificationRunError("SCOPE_REPORT_RECALCULATION_FAILED: " + "; ".join(verified.errors))
        if report.scope is EvaluationScope.FULL_PLANNING_PIPELINE:
            metadata = _json(Path(path).parent / "run-metadata.json")
            try:
                verify_metadata_digest(metadata)
            except ValueError as error:
                raise QualificationRunError("full planning run metadata 결속이 다릅니다.") from error
            observed = metadata.get(
                "inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1
            )
            if observed != inspection_provider_contract:
                raise QualificationRunError(
                    "full planning inspection provider가 benchmark 후보와 다릅니다."
                )
        if report.scope is not EvaluationScope.DETERMINISTIC:
            locks.append((contract.role_configuration_digest, contract.model_lock_digest))
    if len(set(locks)) > 1:
        raise QualificationRunError("기능 scope의 역할·model lock이 서로 다릅니다.")
    return reports


def _benchmark(arguments: argparse.Namespace) -> int:
    if arguments.cells_file is None:
        from .benchmark import run_benchmark
        root = Path(arguments.project_root).resolve(strict=True)
        policies = _evaluation_policies(arguments)
        run_root, report = run_benchmark(
            root=root, run_root=None if arguments.run_root is None else Path(arguments.run_root),
            role_configuration=_roles(arguments.role_config, root), codex_bin=arguments.codex_bin,
            scope_reports=_bound_scope_reports(
                arguments.scope_report, root,
                inspection_provider_contract=arguments.inspection_contract,
                require_checkpoint_evidence=True,
            ),
            inspection_provider_contract=arguments.inspection_contract,
            evaluation_policies=policies,
        )
        _emit({"run_root": str(run_root), "report_digest": report.report_digest, "report": report.model_dump(mode="json")})
        return 0 if report.passed else 1
    cells = _load_cells(arguments.cells_file)
    catalog = PlanningScenarioCatalog.load(
        Path(arguments.project_root) / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    validate_benchmark_matrix(cells, catalog)
    reports = _bound_scope_reports(
        arguments.scope_report, Path(arguments.project_root).resolve(strict=True),
        inspection_provider_contract=arguments.inspection_contract,
    )
    functional = (
        len(reports) == 4
        and {item.scope for item in reports} == set(EvaluationScope)
        and all(item.passed for item in reports)
    )
    report = evaluate_token_latency_gate(cells, functional_gate_passed=functional)
    if arguments.output:
        destination = Path(arguments.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
    _emit(report)
    return 0 if report.passed else 1


def validate_benchmark_matrix(
    cells: tuple[BenchmarkCell, ...], catalog: PlanningScenarioCatalog
) -> None:
    expected = {
        (scenario.scenario_id, seed, implementation)
        for scenario in catalog.scenarios
        for seed in ORDER_SEEDS
        for implementation in ("r31_baseline", "skeleton_engine")
    }
    actual = {(item.scenario_id, item.order_seed, item.implementation) for item in cells}
    if len(catalog.scenarios) != 6 or actual != expected or len(cells) != len(expected):
        raise QualificationRunError(
            "benchmark는 고정된 6개 scenario × 필수 seed 3개 × baseline/engine 36 cell이 필요합니다."
        )
    by_id = {item.scenario_id: item for item in catalog.scenarios}
    if any(
        item.scenario_digest != by_id[item.scenario_id].scenario_digest
        or item.path_kind != by_id[item.scenario_id].path_kind
        or item.expected_disposition != by_id[item.scenario_id].expected_disposition
        for item in cells
    ):
        raise QualificationRunError("benchmark scenario digest, path kind 또는 기대 판정이 고정 입력과 다릅니다.")
    if len({item.model_lock_digest for item in cells}) != 1:
        raise QualificationRunError("benchmark 전체 cell의 model lock이 동일하지 않습니다.")
    if any(item.neutral_input_digest is None for item in cells):
        raise QualificationRunError("qualification benchmark는 중립 입력 파일·정책 digest가 필요합니다.")


def _write_immutable_assessment(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != payload:
            raise QualificationRunError("기존 lifecycle assessment의 내용이 다릅니다.")


def _observe_benchmark_lifecycle_v3(arguments: argparse.Namespace) -> int:
    """모델 호출 없이 원래 cell 원장을 읽고 별도 불변 평가를 추가한다."""
    root = Path(arguments.project_root).resolve(strict=True)
    run_root = Path(arguments.run_root).resolve(strict=True)
    contract = EvaluationContract.model_validate(_json(run_root / "evaluation-contract.json"))
    metadata = _json(run_root / "run-metadata.json")
    verify_metadata_digest(metadata)
    policies_from_metadata(metadata)
    if (
        metadata.get("scope") != "benchmark"
        or metadata.get("evaluation_contract_digest") != contract.contract_digest
        or contract.source_manifest_digest != source_manifest_digest(root)
        or sha256_digest(EngineRoleConfiguration.model_validate(metadata["role_configuration"]))
        != contract.role_configuration_digest
    ):
        raise QualificationRunError("재관측 source·역할·benchmark 계약이 원래 실행과 다릅니다.")
    inspection_provider_contract = metadata.get(
        "inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1
    )
    if inspection_provider_contract not in {
        PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2,
    }:
        raise QualificationRunError("재관측 benchmark inspection provider가 유효하지 않습니다.")
    neutral = _json(run_root / "neutral-inputs.json")
    if sha256_digest(neutral) != contract.scenario_set_digest:
        raise QualificationRunError("재관측 중립 입력이 evaluation 계약과 다릅니다.")
    catalog = PlanningScenarioCatalog.load(root / "tests/fixtures/engine/planning-scenarios.json")
    expected = {
        sha256_digest({"scenario": scenario.scenario_digest, "implementation": implementation,
                       "neutral": sha256_digest(neutral[scenario.scenario_id])}): (scenario, implementation)
        for scenario in catalog.scenarios for implementation in ("r31_baseline", "skeleton_engine")
    }
    if set(expected) != set(contract.fixture_digests) or tuple(contract.order_seeds) != ORDER_SEEDS:
        raise QualificationRunError("재관측 fixture·seed가 고정 benchmark matrix와 다릅니다.")
    reports = _bound_scope_reports(
        arguments.scope_report,
        root,
        inspection_provider_contract=inspection_provider_contract,
    )
    store = ImmutableCheckpointStore(run_root, contract)
    cells, observations, missing = [], [], []
    for seed in contract.order_seeds:
        for identity in contract.fixture_digests:
            checkpoint = store.completed(identity, seed)
            if checkpoint is None:
                missing.append({"fixture_digest": identity, "order_seed": seed})
                continue
            if not checkpoint.completed or checkpoint.fixture_digest != identity or checkpoint.order_seed != seed:
                raise QualificationRunError("저장 경로와 checkpoint fixture·seed가 다릅니다.")
            original = BenchmarkCell.model_validate(checkpoint.raw_structured_assessment["benchmark_cell"])
            raw = checkpoint.raw_structured_assessment["raw"]
            scenario, implementation = expected[identity]
            if (
                original.scenario_id != scenario.scenario_id or original.scenario_digest != scenario.scenario_digest
                or original.implementation != implementation or original.order_seed != seed
                or original.model_lock_digest != contract.model_lock_digest
                or original.neutral_input_digest != sha256_digest(neutral[scenario.scenario_id])
                or tuple(raw["receipts"]) != checkpoint.runner_receipts
                or original.runner_receipt_digest != sha256_digest(raw["receipts"])
            ):
                raise QualificationRunError("원래 benchmark cell의 fixture·receipt 결속이 다릅니다.")
            if original.implementation == "skeleton_engine" and original.disposition == "selected":
                observation = observe_benchmark_execution_checkpoint(checkpoint, run_root=run_root, contract=contract)
                cells.append(observation.cell)
                observations.append(observation.model_dump(mode="json", exclude={"cell"}))
            else:
                cells.append(original)
    token_report = None
    failures = []
    try:
        validate_benchmark_matrix(tuple(cells), catalog)
    except QualificationRunError as error:
        failures.append(str(error))
    else:
        functional = len(reports) == 4 and {item.scope for item in reports} == set(EvaluationScope) and all(item.passed for item in reports)
        token_report = evaluate_token_latency_gate(tuple(cells), functional_gate_passed=functional)
        failures.extend(token_report.failures)
    assessment = {
        "format": "flowmarshal-benchmark-lifecycle-assessment-v1", "source_run_root": str(run_root),
        "contract_digest": contract.contract_digest, "observations": observations, "missing_cells": missing,
        "scope_report_digests": [item.report_digest for item in reports],
        "cells_digest": sha256_digest(cells), "failures": failures,
        "passed": bool(token_report and token_report.passed and not failures),
    }
    destination = run_root / "lifecycle-assessments" / sha256_digest(assessment)[7:]
    _write_immutable_assessment(destination / "evaluation-contract.json", contract.model_dump(mode="json"))
    _write_immutable_assessment(destination / "benchmark-cells.json", {"cells": [cell.model_dump(mode="json") for cell in cells]})
    if token_report is not None:
        _write_immutable_assessment(destination / "token-latency-report.json", token_report.model_dump(mode="json"))
    _write_immutable_assessment(destination / "assessment.json", assessment)
    _emit({"assessment_root": str(destination), "assessment": assessment,
           "release_floor_status": "NOT_OBSERVED", "cutover_eligible": False})
    return 0 if assessment["passed"] else 1


def _observe_benchmark_lifecycle(arguments: argparse.Namespace) -> int:
    """v4 run은 최종 최소선을 판정하고 v3는 역사 관측 형식으로만 읽는다."""
    from .performance_assessment import build_performance_assessment, write_performance_assessment
    root = Path(arguments.project_root).resolve(strict=True)
    run_root = Path(arguments.run_root).resolve(strict=True)
    metadata = _json(run_root / "run-metadata.json")
    if "performance_threshold_policy" not in metadata:
        return _observe_benchmark_lifecycle_v3(arguments)
    reports = _bound_scope_reports(arguments.scope_report, root,
        inspection_provider_contract=metadata.get("inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1),
        require_checkpoint_evidence=True)
    report, bundle = build_performance_assessment(root=root, run_root=run_root, scope_reports=reports, assessment_stage="final")
    destination = write_performance_assessment(run_root, report, bundle)
    _emit({"assessment_root": str(destination), "report": report.model_dump(mode="json"),
           "report_digest": sha256_digest(report), "release_floor_passed": report.release_floor_passed,
           "optimization_targets_passed": report.optimization_targets_passed,
           "optimization_followups_required": report.optimization_followups_required,
           "cutover_eligible": report.cutover_eligible})
    return 0 if report.release_floor_passed else 1


def _cutover(arguments: argparse.Namespace) -> int:
    """원본 근거에서 재계산한 final v4 보고서만 새 cutover에 사용한다."""
    from .performance import PerformanceQualificationReport, evaluate_release_cutover_gate
    from .performance_assessment import build_performance_assessment
    root = Path(arguments.project_root).resolve(strict=True)
    path = Path(arguments.benchmark_report).resolve(strict=True)
    document = _json(path)
    if document.get("schema_version") != "4.0":
        raise QualificationRunError("PERFORMANCE_V4_REPORT_REQUIRED: v3.0은 표시용이며 새 cutover 근거가 아닙니다.")
    report = PerformanceQualificationReport.model_validate_json(json.dumps(document))
    if report.assessment_stage != "final":
        raise QualificationRunError("PERFORMANCE_FINAL_ASSESSMENT_REQUIRED")
    bundle = _json(path.parent / "assessment.json")
    if (bundle.get("format") != "flowmarshal-performance-assessment-v1"
            or bundle.get("report_digest") != sha256_digest(report)
            or path.parent.name != sha256_digest(bundle)[7:]):
        raise QualificationRunError("PERFORMANCE_ASSESSMENT_DIGEST_MISMATCH")
    run_root = Path(bundle["source_run_root"]).resolve(strict=True)
    metadata = _json(run_root / "run-metadata.json")
    reports = _bound_scope_reports(arguments.scope_report, root,
        inspection_provider_contract=metadata.get("inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1),
        require_checkpoint_evidence=True)
    recalculated, original_bundle = build_performance_assessment(root=root, run_root=run_root,
        scope_reports=reports, assessment_stage="final")
    if recalculated != report or original_bundle != bundle:
        raise QualificationRunError("PERFORMANCE_REPORT_RECALCULATION_MISMATCH")
    result = evaluate_release_cutover_gate(performance_report=recalculated)
    if arguments.output:
        _write_immutable_assessment(Path(arguments.output), result.model_dump(mode="json"))
    _emit({"report": result.model_dump(mode="json"), "release_floor_passed": report.release_floor_passed,
           "optimization_targets_passed": report.optimization_targets_passed,
           "optimization_followups_required": report.optimization_followups_required,
           "cutover_eligible": result.passed})
    return 0 if result.passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowmarshal-engine-eval",
        description="FlowMarshal Engine 개발 전용 qualification runner",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument(
        "--scope",
        choices=(
            "deterministic",
            "role-fixture",
            "full-planning-pipeline",
            "project-e2e",
        ),
        required=True,
    )
    run.add_argument("--project-root", default=str(project_root()))
    run.add_argument("--run-root")
    run.add_argument("--role-config")
    run.add_argument("--codex-bin")
    run.add_argument("--codex-project-binding", help="App Server 프로젝트 ID·예상 root 결속 JSON")
    run.add_argument("--budget-policy")
    run.add_argument("--role-timeout-policy")
    run.add_argument(
        "--inspection-contract",
        choices=(PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2),
        default=PLAN_INSPECTION_PROVIDER_V1,
        help="full-planning-pipeline의 Plan inspection provider. 기본값은 v1입니다.",
    )
    run.set_defaults(handler=_run)

    resume = commands.add_parser("resume")
    resume.add_argument("--run-root", required=True)
    resume.set_defaults(handler=_resume)

    benchmark = commands.add_parser("benchmark")
    benchmark.add_argument("--project-root", default=str(project_root()))
    benchmark.add_argument("--cells-file", help="기존 36-cell 결과를 수입한다. 생략하면 실제 비교를 수집한다.")
    benchmark.add_argument("--run-root")
    benchmark.add_argument("--role-config")
    benchmark.add_argument("--codex-bin")
    benchmark.add_argument("--codex-project-binding", help="App Server 프로젝트 ID·예상 root 결속 JSON")
    benchmark.add_argument("--budget-policy")
    benchmark.add_argument("--role-timeout-policy")
    benchmark.add_argument(
        "--inspection-contract",
        choices=(PLAN_INSPECTION_PROVIDER_V1, PLAN_INSPECTION_PROVIDER_V2),
        default=PLAN_INSPECTION_PROVIDER_V1,
        help="benchmark skeleton engine의 Plan inspection provider. 기본값은 v1입니다.",
    )
    benchmark.add_argument(
        "--scope-report",
        action="append",
        help="기능 비회귀를 입증하는 네 scope qualification-report.json (4회 지정)",
    )
    benchmark.add_argument("--output")
    benchmark.set_defaults(handler=_benchmark)

    observe = commands.add_parser("observe-benchmark-lifecycle")
    observe.add_argument("--project-root", default=str(project_root()))
    observe.add_argument("--run-root", required=True)
    observe.add_argument("--scope-report", action="append")
    observe.set_defaults(handler=_observe_benchmark_lifecycle)

    cutover = commands.add_parser("cutover")
    cutover.add_argument("--project-root", default=str(project_root()))
    cutover.add_argument("--scope-report", action="append", required=True)
    cutover.add_argument("--benchmark-report", required=True)
    cutover.add_argument("--output")
    cutover.set_defaults(handler=_cutover)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (
        QualificationRunError,
        BenchmarkObservationError,
        CheckpointContractError,
        StructuredRoleError,
        AssignmentResolutionError,
        RuntimePolicyError,
        CodexError,
        ValidationError,
        ValueError,
        OSError,
    ) as error:
        payload: dict[str, Any] = {"error": type(error).__name__, "message": str(error)}
        if isinstance(error, StructuredRoleError):
            payload["receipts"] = [
                item.model_dump(mode="json") for item in error.receipts
            ]
        _emit(payload)
        return 2
    except KeyboardInterrupt:
        _emit({"error": "Interrupted", "message": "evaluation이 중단됐습니다. run root로 resume하세요."})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
