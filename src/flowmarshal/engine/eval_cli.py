from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from openai_codex.errors import CodexError

from .e2e_qualification import run_project_e2e
from .evaluation import (
    BenchmarkCell,
    EvaluationContract,
    EvaluationScope,
    ScopeGateResult,
    TokenLatencyGateReport,
    evaluate_cutover_gate,
    evaluate_token_latency_gate,
)
from .models import EngineRoleConfiguration
from .models import AssignmentResolutionError
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


def _run(arguments: argparse.Namespace) -> int:
    root = Path(arguments.project_root).resolve(strict=True)
    destination = None if arguments.run_root is None else Path(arguments.run_root).resolve()
    if arguments.scope == "deterministic":
        run_root, report = run_deterministic(root=root, run_root=destination)
    else:
        roles = _roles(arguments.role_config, root)
        if arguments.scope == "role-fixture":
            run_root, report = run_role_fixture(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
            )
        elif arguments.scope == "full-planning-pipeline":
            run_root, report = run_full_planning_pipeline(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
            )
        else:
            run_root, report = run_project_e2e(
                root=root,
                run_root=destination,
                role_configuration=roles,
                codex_bin=arguments.codex_bin,
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
        run_root, report = run_benchmark(
            root=Path(metadata["project_root"]), run_root=Path(arguments.run_root),
            role_configuration=EngineRoleConfiguration.model_validate(metadata["role_configuration"]),
            codex_bin=metadata.get("codex_bin"),
            scope_reports=tuple(ScopeQualificationReport.model_validate(item) for item in metadata.get("scope_reports", [])),
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


def _bound_scope_reports(paths: list[str] | None, root: Path) -> tuple[ScopeQualificationReport, ...]:
    reports = _scope_reports(paths)
    locks = []
    for path, report in zip(paths or [], reports, strict=True):
        contract = EvaluationContract.model_validate(_json(Path(path).parent / "evaluation-contract.json"))
        if contract.contract_digest != report.contract_digest or contract.scope != report.scope:
            raise QualificationRunError("scope report와 원본 evaluation 계약이 다릅니다.")
        if contract.source_manifest_digest != source_manifest_digest(root):
            raise QualificationRunError("현재 source와 다른 scope report는 cutover 근거가 아닙니다.")
        if report.scope is not EvaluationScope.DETERMINISTIC:
            locks.append((contract.role_configuration_digest, contract.model_lock_digest))
    if len(set(locks)) > 1:
        raise QualificationRunError("기능 scope의 역할·model lock이 서로 다릅니다.")
    return reports


def _benchmark(arguments: argparse.Namespace) -> int:
    if arguments.cells_file is None:
        from .benchmark import run_benchmark
        root = Path(arguments.project_root).resolve(strict=True)
        run_root, report = run_benchmark(
            root=root, run_root=None if arguments.run_root is None else Path(arguments.run_root),
            role_configuration=_roles(arguments.role_config, root), codex_bin=arguments.codex_bin,
            scope_reports=_bound_scope_reports(arguments.scope_report, root),
        )
        _emit({"run_root": str(run_root), "report_digest": report.report_digest, "report": report.model_dump(mode="json")})
        return 0 if report.passed else 1
    cells = _load_cells(arguments.cells_file)
    catalog = PlanningScenarioCatalog.load(
        Path(arguments.project_root) / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    validate_benchmark_matrix(cells, catalog)
    reports = _bound_scope_reports(arguments.scope_report, Path(arguments.project_root).resolve(strict=True))
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


def _cutover(arguments: argparse.Namespace) -> int:
    root = Path(arguments.project_root).resolve(strict=True)
    reports = _bound_scope_reports(arguments.scope_report, root)
    token_report = TokenLatencyGateReport.model_validate(_json(arguments.benchmark_report))
    benchmark_root = Path(arguments.benchmark_report).parent
    benchmark_contract = EvaluationContract.model_validate(_json(benchmark_root / "evaluation-contract.json"))
    if benchmark_contract.source_manifest_digest != source_manifest_digest(root):
        raise QualificationRunError("현재 source와 다른 benchmark는 cutover 근거가 아닙니다.")
    cells = _load_cells(benchmark_root / "benchmark-cells.json")
    catalog = PlanningScenarioCatalog.load(root / "tests/fixtures/engine/planning-scenarios.json")
    validate_benchmark_matrix(cells, catalog)
    if any(item.model_lock_digest != benchmark_contract.model_lock_digest for item in cells):
        raise QualificationRunError("benchmark model lock과 cell이 다릅니다.")
    recalculated = evaluate_token_latency_gate(cells, functional_gate_passed=(len(reports) == 4 and all(item.passed for item in reports)))
    if recalculated != token_report:
        raise QualificationRunError("benchmark report가 원본 cell의 재계산 결과와 다릅니다.")
    scope_results = tuple(
        ScopeGateResult(
            scope=report.scope,
            contract_digest=report.contract_digest,
            artifact_digest=report.report_digest,
            passed=report.passed,
            failures=report.failures,
        )
        for report in reports
    )
    result = evaluate_cutover_gate(
        scope_results=scope_results,
        token_latency_gate=token_report,
    )
    if arguments.output:
        destination = Path(arguments.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
    _emit(result)
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
    benchmark.add_argument(
        "--scope-report",
        action="append",
        help="기능 비회귀를 입증하는 네 scope qualification-report.json (4회 지정)",
    )
    benchmark.add_argument("--output")
    benchmark.set_defaults(handler=_benchmark)

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
