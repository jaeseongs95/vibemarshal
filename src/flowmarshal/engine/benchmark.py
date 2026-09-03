"""중립 입력으로 두 planner를 순차 호출하고 완료 cell만 보존한다."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..canonical import sha256_bytes, sha256_digest
from .domain import EngineModel, PlanContractRevision, new_id, utc_now
from .evaluation import (
    BenchmarkCell, EvaluationCellCheckpoint, EvaluationContract, EvaluationRunStatus,
    ImmutableCheckpointStore, TokenLatencyGateReport, evaluate_token_latency_gate,
)
from .models import EngineRoleConfiguration
from .qualification import (
    ORDER_SEEDS, PlanningScenarioCatalog, QualificationRunError, ScopeQualificationReport,
    _default_run_root, _is_rate_limit, _manifest, _planning_cell, _planning_contract, _preflight,
    _profile, _role_progress, _write_json, default_role_configuration, project_root, source_manifest_digest,
)
from .roles import StructuredRoleError
from .runtime import CodexAppServerRuntime


MEASUREMENT_RULES = {
    "version": "neutral-planning-window-v1",
    "token": "uncached_input_plus_output; six scenarios including expected blocks",
    "latency": "monotonic wall time to Core-admitted feasible Plan; blocks recorded separately",
    "detail": "operational file/command ExecutionSpec, not semantic TaskContract; no execution before activation",
    "discarded_output": "candidate expander/refiner receipt output; exclude batched Skeleton output",
    "order": "seed-shuffled scenario/implementation cells executed serially",
    "thresholds": {"multi": .30, "overall": .20, "single_regression": .05,
                   "unexecuted_detail": .10, "discarded_output": .25, "first_feasible": .20},
}


class BenchmarkRunReport(EngineModel):
    contract_digest: str
    status: EvaluationRunStatus
    completed_cell_count: int
    expected_cell_count: int = 36
    passed: bool
    failures: tuple[str, ...] = ()
    token_latency: TokenLatencyGateReport | None = None

    @property
    def report_digest(self):
        return sha256_digest(self)


def neutral_input(root: Path, scenario) -> dict[str, Any]:
    fixture = root / "tests/fixtures/engine/live-smoke-project"
    files = [{"path": path.relative_to(fixture).as_posix(), "content": path.read_text(encoding="utf-8"),
              "digest": sha256_bytes(path.read_bytes())}
             for path in sorted(fixture.rglob("*")) if path.is_file() and "__pycache__" not in path.parts]
    return {"source_request": scenario.source_request, "files": files,
            "profile": _profile("project_" + "0" * 32).definition.model_dump(mode="json")}


def _legacy_process(root: Path, arguments: list[str]) -> None:
    completed = subprocess.run([sys.executable, "-m", "flowmarshal.benchmark_legacy", *arguments],
                               cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise QualificationRunError("독립 legacy benchmark harness 실패: " + completed.stderr[-5000:])


def receipt_cost(receipt: dict[str, Any], implementation: str) -> tuple[int, int]:
    if implementation == "skeleton_engine":
        if not receipt.get("usage_available"):
            raise QualificationRunError("실제 Runner token usage가 없습니다. 0으로 추정하지 않습니다.")
        inputs, cached, outputs = (receipt[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
    else:
        usage = {item["name"]: item["value"] for item in receipt.get("usage", ())}
        def metric(names):
            found = next((usage[name] for name in names if name in usage), None)
            if found is None:
                raise QualificationRunError("R3.1 Runner token usage가 없습니다: " + "/".join(names))
            return found
        inputs = metric(("input_tokens", "inputTokens", "total.input_tokens", "total.inputTokens"))
        cached = metric(("cached_input_tokens", "cachedInputTokens", "total.cached_input_tokens", "total.cachedInputTokens"))
        outputs = metric(("output_tokens", "outputTokens", "total.output_tokens", "total.outputTokens"))
    if cached > inputs:
        raise QualificationRunError("cached input이 전체 input token보다 큽니다.")
    return inputs - cached, outputs


def benchmark_cell(scenario, seed: int, implementation: str, model_lock: str, raw: dict[str, Any]) -> BenchmarkCell:
    receipts = raw["receipts"]
    if not receipts:
        raise QualificationRunError("완료 Runner receipt 없는 benchmark cell은 저장할 수 없습니다.")
    costs = [receipt_cost(item, implementation) for item in receipts]
    if implementation == "skeleton_engine":
        disposition = "selected" if raw.get("selected") else ("blocked" if raw.get("blocking_questions") else "failed")
        candidate_receipts = [item for item in receipts if item["role"] == "plan_expander"]
        expanded = raw.get("planning_outcome", {}).get("plan_evaluations", [])
        selected = raw.get("selected_activation_digest")
        # 별도 expander 한 호출이 Plan 후보 하나를 출력한다. 대응이 없으면 token을 배분하지 않는다.
        if len(candidate_receipts) != len(expanded):
            raise QualificationRunError("Engine 상세 후보와 출력 receipt가 일대일로 결속되지 않았습니다.")
        candidate_tokens = sum(item["output_tokens"] for item in candidate_receipts)
        discarded = sum(receipt["output_tokens"] for receipt, item in zip(candidate_receipts, expanded, strict=True)
                        if PlanContractRevision.model_validate(item["plan"]).activation_digest != selected)
        detailed = 0  # 이 호출 경로는 ExecutionSpec을 생성하지 않는다. semantic Task 수가 아니다.
        correct = bool(raw.get("passed"))
    else:
        disposition = raw["disposition"]
        candidate_ids = {record["candidate_id"]: record for record in raw["candidate_records"]}
        by_receipt = {item["call_id"]: receipt_cost(item, implementation)[1] for item in receipts}
        candidate_tokens = sum(by_receipt[ref] for record in candidate_ids.values() for ref in record["receipt_ids"])
        discarded = sum(by_receipt[ref] for record in candidate_ids.values()
                        if record["candidate_id"] != raw["selected_candidate_id"] for ref in record["receipt_ids"])
        detailed = sum(item["task_count"] for item in candidate_ids.values())
        correct = disposition == scenario.expected_disposition
    return BenchmarkCell(
        scenario_id=scenario.scenario_id, scenario_digest=scenario.scenario_digest, order_seed=seed,
        path_kind=scenario.path_kind, implementation=implementation, model_lock_digest=model_lock,
        neutral_input_digest=raw["neutral_input_digest"],
        functional_result_digest=sha256_digest({"expected": scenario.expected_disposition,
                                               "disposition": disposition, "contract_satisfied": correct}),
        runner_receipt_digest=sha256_digest(receipts), expected_disposition=scenario.expected_disposition,
        disposition=disposition, uncached_input_tokens=sum(item[0] for item in costs),
        output_tokens=sum(item[1] for item in costs),
        latency_ms_to_first_feasible=raw.get("latency_ms_to_first_feasible") if disposition == "selected" else None,
        latency_ms_to_disposition=raw["latency_ms_to_disposition"], detailed_task_count=detailed,
        unexecuted_detailed_task_count=detailed, candidate_output_tokens=candidate_tokens,
        discarded_candidate_output_tokens=discarded,
    )


def run_benchmark(*, root: Path | None = None, run_root: Path | None = None,
                  role_configuration: EngineRoleConfiguration | None = None, codex_bin: str | None = None,
                  scope_reports: tuple[ScopeQualificationReport, ...] = ()) -> tuple[Path, BenchmarkRunReport]:
    base = (root or project_root()).resolve(strict=True)
    failures = _preflight(base)
    if failures:
        raise QualificationRunError("; ".join(failures))
    roles = role_configuration or default_role_configuration(base)
    catalog = PlanningScenarioCatalog.load(base / "tests/fixtures/engine/planning-scenarios.json")
    manifest = _manifest(base)
    skill = next((base / item.path).resolve() for item in manifest.roots if item.scope == "prototype_planner_skill")
    with tempfile.TemporaryDirectory(prefix="flowmarshal-benchmark-contract-") as temporary:
        description_path = Path(temporary) / "legacy-description.json"
        _legacy_process(base, ["--describe", "--skill-root", str(skill), "--output", str(description_path)])
        description = json.loads(description_path.read_text(encoding="utf-8"))
    with CodexAppServerRuntime(codex_bin=codex_bin) as runtime:
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        planning_contract = _planning_contract(base, catalog, inventory, roles)
        model_lock = sha256_digest({"engine_lock": planning_contract.model_lock_digest, "legacy_role_map": description["role_map"]})
        neutral = {scenario.scenario_id: neutral_input(base, scenario) for scenario in catalog.scenarios}
        identities = {(scenario.scenario_id, implementation): sha256_digest({
            "scenario": scenario.scenario_digest, "implementation": implementation,
            "neutral": sha256_digest(neutral[scenario.scenario_id]),
        }) for scenario in catalog.scenarios for implementation in ("r31_baseline", "skeleton_engine")}
        contract = EvaluationContract.model_validate(planning_contract.model_dump() | {
            "fixture_digests": tuple(identities.values()), "expected_cell_count": 36,
            "scenario_set_digest": sha256_digest(neutral), "rules_digest": sha256_digest(MEASUREMENT_RULES),
            "threshold_digest": sha256_digest(MEASUREMENT_RULES["thresholds"]),
            "model_lock_digest": model_lock,
            "prompt_digest": sha256_digest({"engine": planning_contract.prompt_digest, "legacy": description["prompt_digest"]}),
            "output_schema_digest": sha256_digest({"engine": planning_contract.output_schema_digest,
                                                    "legacy": description["schema_digest"], "cell": BenchmarkCell.model_json_schema()}),
        })
        destination = (run_root or _default_run_root(base, "benchmark", contract.contract_digest[7:15])).resolve()
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        _write_json(destination / "run-metadata.json", {
            "scope": "benchmark", "project_root": str(base), "codex_bin": codex_bin,
            "role_configuration": roles.model_dump(mode="json"),
            "scope_reports": [item.model_dump(mode="json") for item in scope_reports],
        })
        _write_json(destination / "neutral-inputs.json", neutral)
        status, failures = EvaluationRunStatus.COMPLETED, []
        try:
            for seed in ORDER_SEEDS:
                order = list(identities)
                random.Random(seed).shuffle(order)
                for scenario_id, implementation in order:
                    identity = identities[scenario_id, implementation]
                    if store.completed(identity, seed) is not None:
                        continue
                    if source_manifest_digest(base) != contract.source_manifest_digest:
                        raise QualificationRunError("실행 중 source 계약이 변경됐습니다.")
                    scenario = next(item for item in catalog.scenarios if item.scenario_id == scenario_id)
                    work = destination / "work" / f"seed-{seed}" / scenario_id / implementation / new_id("attempt")
                    work.mkdir(parents=True)
                    started = time.monotonic()
                    if implementation == "skeleton_engine":
                        raw, receipts = _planning_cell(
                            scenario=scenario, seed=seed, fixture_root=base / "tests/fixtures/engine/live-smoke-project",
                            runtime=runtime, inventory=inventory, roles=roles, work_root=work,
                            progress_sink=_role_progress(destination, scenario_id=scenario_id, order_seed=seed, implementation=implementation),
                        )
                        raw["receipts"] = [item.model_dump(mode="json") for item in receipts]
                        raw["neutral_input_digest"] = sha256_digest(neutral[scenario_id])
                    else:
                        shutil.copytree(base / "tests/fixtures/engine/live-smoke-project", work / "project",
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                        _write_json(work / "request.json", {
                            "workspace": str(work / "project"), "state_root": str(work / "legacy-state"),
                            "neutral_input": neutral[scenario_id], "neutral_input_digest": sha256_digest(neutral[scenario_id]),
                            "roles": roles.model_dump(mode="json"), "codex_bin": codex_bin,
                            "skill_root": str(skill), "python_executable": sys.executable,
                        })
                        _legacy_process(base, ["--request-file", str(work / "request.json"), "--output", str(work / "raw-result.json")])
                        raw = json.loads((work / "raw-result.json").read_text(encoding="utf-8"))
                    if raw.get("message") and _is_rate_limit(QualificationRunError(raw["message"])):
                        raise QualificationRunError(raw["message"])
                    # 부모 프로세스 준비 시간은 진단으로 보존하고 각 pipeline의 monotonic 지표를 사용한다.
                    raw["collector_latency_ms"] = max(1, int((time.monotonic() - started) * 1000))
                    _write_json(work / "raw-result.json", raw)
                    cell = benchmark_cell(scenario, seed, implementation, model_lock, raw)
                    store.put(EvaluationCellCheckpoint(
                        contract_digest=contract.contract_digest, fixture_digest=identity, order_seed=seed,
                        raw_structured_assessment={"benchmark_cell": cell.model_dump(mode="json"), "raw": raw},
                        runner_receipts=tuple(raw["receipts"]),
                    ))
        except Exception as error:
            status = EvaluationRunStatus.PAUSED_RATE_LIMIT if _is_rate_limit(error) else EvaluationRunStatus.FAILED
            failures.append(f"{type(error).__name__}: {error}")
            _write_json(destination / "last-error.json", {"error": type(error).__name__, "message": str(error),
                        "receipts": [item.model_dump(mode="json") for item in getattr(error, "receipts", ())]})
        cells = tuple(BenchmarkCell.model_validate(checkpoint.raw_structured_assessment["benchmark_cell"])
                      for seed in ORDER_SEEDS for identity in identities.values()
                      if (checkpoint := store.completed(identity, seed)) is not None)
        token_report = None
        if len(cells) == 36:
            from .eval_cli import validate_benchmark_matrix
            try:
                validate_benchmark_matrix(cells, catalog)
                functional = len(scope_reports) == 4 and len({item.scope for item in scope_reports}) == 4 and all(item.passed for item in scope_reports)
                token_report = evaluate_token_latency_gate(cells, functional_gate_passed=functional)
                failures.extend(token_report.failures)
                _write_json(destination / "token-latency-report.json", token_report)
            except ValueError as error:
                failures.append(str(error))
        store.set_state(status, updated_at=utc_now(), reason="; ".join(failures)[:5000] or None)
        report = BenchmarkRunReport(contract_digest=contract.contract_digest, status=status,
                                    completed_cell_count=len(cells), passed=bool(token_report and token_report.passed and not failures),
                                    failures=tuple(failures), token_latency=token_report)
        _write_json(destination / "benchmark-cells.json", {"cells": [item.model_dump(mode="json") for item in cells]})
        _write_json(destination / "benchmark-run-report.json", report)
        return destination, report
