from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .context import ProjectMapper, goal_context_observations
from .domain import (
    Criticality,
    EngineModel,
    FindingSeverity,
    GateName,
    LifecycleStage,
    ModelAssignmentContract,
    PlanningBudgetPolicy,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RevisionStatus,
    RoleAssignmentPolicy,
    StateFact,
    StateSnapshot,
    ReviewFinding,
    ReviewerSubmission,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from .evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunStatus,
    EvaluationScope,
    FixtureResult,
    ImmutableCheckpointStore,
    QualificationReport,
    RegressionCatalog,
    RegressionFixture,
    evaluate_role_fixtures,
)
from .freeze import LegacyFreezeManifest, verify_legacy_freeze
from .goal import GoalNormalizerAdapter, GoalPreparationPipeline, GoalReviewerAdapter, ReviewDraft
from .models import AssignmentResolver, EngineRoleConfiguration, ModelInventory
from .planner_roles import (
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from .planning import SkeletonFirstPlanner
from .roles import (
    CodexStructuredRoleRunner,
    RoleCallRequest,
    RoleCallReceipt,
    StructuredRoleError,
    strict_json_output_schema,
)
from .runtime import CodexAppServerRuntime


ORDER_SEEDS = (17, 43, 89)
ROLE_INSTRUCTIONS = (
    "후보를 독립 검토한다. 직접 evidence가 있는 최소 finding만 제출하고, 같은 증상에서 "
    "상관 finding을 늘리지 않는다. finding code는 명확하고 재현 가능해야 한다. "
    "finding이 없을 때만 다섯 축 rating을 제출한다. status, admission, 최종 score는 선언하지 않는다. "
    "제공된 finding taxonomy 안의 코드만 사용한다."
    "입력은 source와 후보의 특정 의미를 검사하는 부분 발췌다. 생략된 필드나 문맥을 "
    "존재한다고 발명하지 않으며, 생략 자체를 명시적 결함으로 간주하지도 않는다. "
    "대상·자료가 없다고 명시된 경우는 정보 부족을 지적한다. 발췌에 없는 프로젝트 파일이나 "
    "외부 자료를 탐색하지 않는다. taxonomy의 코드 구분을 따르고 같은 증상에 넓은 코드와 "
    "구체적인 코드를 함께 붙이지 않는다. 서로 다른 요구·선택 집합 등 독립 증거가 있는 "
    "결함은 각각 제출한다."
    "아직 정하지 않은 설계 대안·새 산출물 배치·테스트 명령을 반드시 외부에서 제공받을 사실로 "
    "취급하지 않는다. source에 없는 가상의 동명 함수·다른 프로젝트가 있을 수 있다는 추측은 "
    "직접 evidence가 아니다. 제공된 ProjectProfile의 호환성·최소 변경 정책도 Goal의 근거다."
)


class PlanningScenario(EngineModel):
    scenario_id: str = Field(pattern=r"^S[0-9]{2}-[a-z0-9-]+$")
    path_kind: Literal["multi_path", "single_path"]
    source_request: str = Field(min_length=1, max_length=10_000)
    expected_disposition: Literal["selected", "blocked"]
    candidate_count: int = Field(ge=1, le=3)

    @property
    def scenario_digest(self) -> str:
        return sha256_digest(self)


class PlanningScenarioCatalog(EngineModel):
    schema_version: Literal["1.0"] = "1.0"
    scenarios: tuple[PlanningScenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def scenarios_are_unique(self) -> "PlanningScenarioCatalog":
        ids = tuple(item.scenario_id for item in self.scenarios)
        if len(ids) != len(set(ids)):
            raise ValueError("planning scenario ID가 중복됐습니다.")
        return self

    @property
    def catalog_digest(self) -> str:
        return sha256_digest(self)

    @classmethod
    def load(cls, path: Path | str) -> "PlanningScenarioCatalog":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class ScopeQualificationReport(EngineModel):
    scope: EvaluationScope
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: EvaluationRunStatus
    passed: bool
    metrics: dict[str, Any]
    failures: tuple[str, ...] = ()
    generated_at: datetime

    @model_validator(mode="after")
    def completed_evidence_is_required_for_pass(self) -> "ScopeQualificationReport":
        if self.passed:
            if self.status is not EvaluationRunStatus.COMPLETED or self.failures:
                raise ValueError("미완료 또는 실패 finding이 있는 scope는 PASS가 아닙니다.")
            metric, count = {
                EvaluationScope.DETERMINISTIC: ("check_count", 5),
                EvaluationScope.ROLE_FIXTURE: ("cell_count", 48),
                EvaluationScope.FULL_PLANNING_PIPELINE: ("cell_count", 18),
                EvaluationScope.PROJECT_E2E: ("actual_codex_cell_count", 4),
            }[self.scope]
            if self.metrics.get(metric) != count:
                raise ValueError(f"PASS에 필요한 {metric}={count} 증거가 부족합니다.")
        return self

    @property
    def report_digest(self) -> str:
        return sha256_digest(self)


class QualificationRunError(RuntimeError):
    pass


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_role_configuration(root: Path | None = None) -> EngineRoleConfiguration:
    base = root or project_root()
    return EngineRoleConfiguration.model_validate_json(
        (base / "config" / "qualification-roles.json").read_text(encoding="utf-8")
    )


def _manifest(root: Path) -> LegacyFreezeManifest:
    return LegacyFreezeManifest.load(root / "config" / "legacy-freeze-manifest.json")


def source_manifest_files(root: Path) -> dict[str, str]:
    """결정적 Gate가 실행하는 source·test·fixture 입력의 실제 결속을 반환한다."""

    paths: set[Path] = {
        root / "AGENTS.md",
        root / "pyproject.toml",
        root / "config" / "legacy-freeze-manifest.json",
        root / "config" / "qualification-roles.json",
        root / "config" / "qualification-finding-taxonomy.json",
        root / "docs" / "orchestration-redesign.md",
        root / "docs" / "engine-cutover-adr.md",
        root / "docs" / "r31-frozen-baseline.md",
        root / "tests" / "engine_helpers.py",
        root / "src" / "flowmarshal" / "benchmark_legacy.py",
    }
    paths.update((root / "src").rglob("*.py"))
    paths.update((root / "tests").rglob("*.py"))
    paths.update((root / "scripts" / "diagnostics").glob("*.py"))
    paths.update((root / "tests" / "fixtures").rglob("*"))
    excluded_parts = {
        ".flowmarshal-engine",
        ".flowmarshal-engine-eval",
        ".git",
        ".pytest_cache",
        ".venv",
        "__pycache__",
    }
    excluded_names = {
        "auth.json",
        "api-key.json",
        "api-keys.json",
        "credential.json",
        "credentials.json",
        "oauth.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
    excluded_suffixes = {
        ".db",
        ".db-journal",
        ".db-shm",
        ".db-wal",
        ".kdbx",
        ".key",
        ".p12",
        ".pem",
        ".pfx",
        ".pyc",
        ".pyo",
        ".sqlite",
        ".sqlite-journal",
        ".sqlite-shm",
        ".sqlite-wal",
        ".sqlite3",
        ".sqlite3-journal",
        ".sqlite3-shm",
        ".sqlite3-wal",
    }
    files = sorted(
        (
            path
            for path in paths
            if path.is_file()
            and not path.is_symlink()
            and not excluded_parts.intersection(part.casefold() for part in path.parts)
            and path.name.casefold() not in excluded_names
            and not path.name.casefold().startswith(".env")
            and path.suffix.casefold() not in excluded_suffixes
        ),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    return {path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes()) for path in files}


def source_manifest_digest(root: Path) -> str:
    return sha256_digest(source_manifest_files(root))


def _default_run_root(root: Path, scope_name: str, contract_hint: str) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return root / ".flowmarshal-engine-eval" / "runs" / f"{scope_name}-{stamp}-{contract_hint}"


def _write_json(path: Path, value: Any) -> None:
    if isinstance(value, EngineModel):
        document = value.model_dump(mode="json")
    else:
        document = value
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def _role_progress(root: Path, **context: Any) -> Callable[[dict[str, Any]], None]:
    def record(event: dict[str, Any]) -> None:
        # 미완료 역할의 진행 위치는 진단 자료이며 완료 cell이 아니다.
        _write_json(root / "role-progress" / f"{new_id('progress')}.json", context | event)
    return record


def _command_observation(root: Path, name: str, argv: tuple[str, ...]) -> dict[str, Any]:
    completed = subprocess.run(
        argv,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "name": name,
        "argv": list(argv),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-20_000:],
        "passed": completed.returncode == 0,
    }


def deterministic_observations(root: Path) -> tuple[dict[str, Any], ...]:
    python = str(Path(sys.executable).resolve())
    observations = [
        _command_observation(
            root,
            "compileall",
            (python, "-m", "compileall", "-q", str(root / "src" / "flowmarshal" / "engine")),
        ),
        _command_observation(
            root,
            "full-test-suite",
            (python, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"),
        ),
        _command_observation(root, "pip-check", (python, "-m", "pip", "check")),
        _command_observation(
            root,
            "synthetic-lifecycle",
            (python, "-m", "flowmarshal.engine.smoke", "--project-root",
             str(root / "tests" / "fixtures" / "engine" / "synthetic-lifecycle-project")),
        ),
    ]
    manifest = _manifest(root)
    freeze = verify_legacy_freeze(manifest, project_root=root)
    observations.append(
        {
            "name": "legacy-freeze-manifest",
            "passed": freeze.passed,
            "report": freeze.model_dump(mode="json"),
        }
    )
    return tuple(observations)


def _deterministic_contract(root: Path) -> EvaluationContract:
    names = (
        "compileall",
        "full-test-suite",
        "pip-check",
        "synthetic-lifecycle",
        "legacy-freeze-manifest",
    )
    synthetic_root = (root / "tests" / "fixtures" / "engine" / "synthetic-lifecycle-project").resolve(strict=True)
    synthetic_inputs = {
        path.relative_to(synthetic_root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(synthetic_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }
    fixture_digests = tuple(
        sha256_digest({"deterministic_check": name} | (
            {"project_files": synthetic_inputs} if name == "synthetic-lifecycle" else {}
        )) for name in names
    )
    roles = default_role_configuration(root)
    return EvaluationContract(
        scope=EvaluationScope.DETERMINISTIC,
        fixture_digests=fixture_digests,
        scenario_set_digest=sha256_digest(names),
        order_seeds=(0,),
        expected_cell_count=len(names),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest({"commands": names, "all_must_pass": True,
                                   "synthetic_project": "tests/fixtures/engine/synthetic-lifecycle-project"}),
        threshold_digest=sha256_digest({"failure_count": 0}),
        taxonomy_digest=sha256_digest({"gates": ["schema", "dag", "history", "intent", "freeze"]}),
        prompt_digest=sha256_digest({"model_calls": False}),
        output_schema_digest=sha256_digest({"observation": "command-or-freeze-report-v1"}),
        model_lock_digest=roles.configuration_digest,
    )


def run_deterministic(
    *, root: Path | None = None, run_root: Path | None = None
) -> tuple[Path, ScopeQualificationReport]:
    base = (root or project_root()).resolve(strict=True)
    contract = _deterministic_contract(base)
    destination = (run_root or _default_run_root(base, "deterministic", contract.contract_digest[7:15])).resolve()
    store = ImmutableCheckpointStore(destination, contract)
    store.initialize()
    observations = deterministic_observations(base)
    by_name = {item["name"]: item for item in observations}
    for name, fixture_digest in zip(
        ("compileall", "full-test-suite", "pip-check", "synthetic-lifecycle", "legacy-freeze-manifest"),
        contract.fixture_digests,
        strict=True,
    ):
        if store.completed(fixture_digest, 0) is not None:
            continue
        store.put(
            EvaluationCellCheckpoint(
                contract_digest=contract.contract_digest,
                fixture_digest=fixture_digest,
                order_seed=0,
                raw_structured_assessment=by_name[name],
                runner_receipts=({"runner": "local", "check": name, "completed": True},),
            )
        )
    failures = tuple(
        f"{item['name']}: FAIL" for item in observations if not bool(item.get("passed"))
    )
    store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
    report = ScopeQualificationReport(
        scope=contract.scope,
        contract_digest=contract.contract_digest,
        status=EvaluationRunStatus.COMPLETED,
        passed=not failures,
        metrics={"check_count": len(observations), "failure_count": len(failures)},
        failures=failures,
        generated_at=utc_now(),
    )
    _write_json(destination / "qualification-report.json", report)
    _write_json(
        destination / "run-metadata.json",
        {"scope": "deterministic", "project_root": str(base)},
    )
    return destination, report


def _preflight(root: Path) -> tuple[str, ...]:
    return tuple(
        f"deterministic preflight {item['name']}: FAIL"
        for item in deterministic_observations(root)
        if not bool(item.get("passed"))
    )


def _combined_role_catalog(root: Path) -> RegressionCatalog:
    fixture_root = root / "tests" / "fixtures" / "engine"
    plan = RegressionCatalog.load(fixture_root / "r31-reviewer-regressions.json")
    goal = RegressionCatalog.load(fixture_root / "goal-reviewer-regressions.json")
    return RegressionCatalog(
        source_baseline=f"{plan.source_baseline}; {goal.source_baseline}",
        fixtures=plan.fixtures + goal.fixtures,
    )


def _model_lock(inventory: ModelInventory, roles: EngineRoleConfiguration) -> str:
    return sha256_digest(
        {"inventory_digest": inventory.inventory_digest, "roles": roles.model_dump(mode="json")}
    )


def _role_contract(
    root: Path,
    catalog: RegressionCatalog,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
) -> EvaluationContract:
    taxonomy = json.loads(
        (root / "config" / "qualification-finding-taxonomy.json").read_text(
            encoding="utf-8"
        )
    )
    fixture_codes = {
        code
        for fixture in catalog.fixtures
        for code in fixture.required_finding_codes + fixture.allowed_correlated_codes
    }
    if not isinstance(taxonomy, dict) or not fixture_codes.issubset(set(taxonomy)):
        raise QualificationRunError("finding taxonomy가 fixture oracle 코드를 모두 정의하지 않습니다.")
    return EvaluationContract(
        scope=EvaluationScope.ROLE_FIXTURE,
        fixture_digests=tuple(item.fixture_digest for item in catalog.fixtures),
        scenario_set_digest=catalog.catalog_digest,
        order_seeds=ORDER_SEEDS,
        expected_cell_count=len(catalog.fixtures) * len(ORDER_SEEDS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest({"instructions": ROLE_INSTRUCTIONS, "oracle_hidden": True}),
        threshold_digest=sha256_digest(
            {"recall": 0.90, "precision": 0.85, "critical_false_admission": 0, "clean_false_block": 0}
        ),
        taxonomy_digest=sha256_digest(taxonomy),
        prompt_digest=sha256_digest(ROLE_INSTRUCTIONS),
        output_schema_digest=sha256_digest(strict_json_output_schema(ReviewDraft.model_json_schema())),
        model_lock_digest=_model_lock(inventory, roles),
    )


def _review_result(
    fixture: RegressionFixture,
    *,
    order_seed: int,
    raw: dict[str, Any],
    reviewer_role: str,
) -> FixtureResult:
    draft = ReviewDraft.model_validate(raw)
    evidence_catalog = {"artifact:candidate": fixture.artifact}
    findings = tuple(
        ReviewFinding(
            finding_code=item.finding_code,
            gate=item.gate,
            severity=item.severity,
            summary=item.summary,
            evidence_refs=item.evidence_refs,
            affected_task_refs=item.affected_task_refs,
            remediable=item.remediable,
        )
        for item in draft.findings
    )
    submission = ReviewerSubmission(
        reviewer_role=reviewer_role,
        candidate_digest=sha256_digest(fixture.artifact),
        findings=findings,
        ratings=draft.ratings,
        evidence_catalog_digest=sha256_digest(evidence_catalog),
    )
    return FixtureResult(
        opaque_case_ref=fixture.opaque_case_ref,
        order_seed=order_seed,
        submission=submission,
        decision=derive_candidate_decision(
            candidate_digest=submission.candidate_digest,
            findings=submission.findings,
            ratings=submission.ratings,
        ),
    )


def _checkpoint_fixture_result(checkpoint: EvaluationCellCheckpoint) -> FixtureResult:
    return FixtureResult.model_validate(checkpoint.raw_structured_assessment["fixture_result"])


def _is_rate_limit(error: BaseException) -> bool:
    rendered = str(error).casefold()
    return any(
        marker in rendered
        for marker in ("rate limit", "rate_limit", "usage limit", "usage_limit", "quota")
    )


def run_role_fixture(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    codex_bin: Path | str | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    base = (root or project_root()).resolve(strict=True)
    preflight_failures = _preflight(base)
    if preflight_failures:
        raise QualificationRunError("; ".join(preflight_failures))
    roles = role_configuration or default_role_configuration(base)
    catalog = _combined_role_catalog(base)
    taxonomy = json.loads(
        (base / "config" / "qualification-finding-taxonomy.json").read_text(
            encoding="utf-8"
        )
    )
    with CodexAppServerRuntime(codex_bin=codex_bin) as runtime:
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _role_contract(base, catalog, inventory, roles)
        destination = (
            run_root or _default_run_root(base, "role-fixture", contract.contract_digest[7:15])
        ).resolve()
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        if store.state().status is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        _write_json(
            destination / "run-metadata.json",
            {
                "scope": "role-fixture",
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
            },
        )
        runner = CodexStructuredRoleRunner(runtime, progress_sink=_role_progress(destination))
        results: list[FixtureResult] = []
        by_digest = {item.fixture_digest: item for item in catalog.fixtures}
        try:
            for seed in contract.order_seeds:
                ordered = list(catalog.fixtures)
                random.Random(seed).shuffle(ordered)
                for fixture in ordered:
                    existing = store.completed(fixture.fixture_digest, seed)
                    if existing is not None:
                        results.append(_checkpoint_fixture_result(existing))
                        continue
                    binding = roles.critical_reviewer if fixture.critical else roles.general_reviewer
                    reviewer_role = "critical_reviewer" if fixture.critical else "general_reviewer"
                    evidence_catalog = {"artifact:candidate": fixture.artifact}
                    request = RoleCallRequest(
                        role=reviewer_role,
                        instructions=ROLE_INSTRUCTIONS,
                        payload=fixture.model_input()
                        | {
                            "evidence_catalog": evidence_catalog,
                            "finding_taxonomy": taxonomy,
                        },
                        output_schema=ReviewDraft.model_json_schema(),
                        model=binding.model,
                        effort=binding.effort,
                        inventory_digest=inventory.inventory_digest,
                        cwd=str(base),
                    )

                    def validate(raw: dict[str, Any]) -> ReviewDraft:
                        draft = ReviewDraft.model_validate(raw)
                        unknown = {
                            ref
                            for finding in draft.findings
                            for ref in finding.evidence_refs
                            if ref not in evidence_catalog
                        }
                        if unknown:
                            raise ValueError(f"제공되지 않은 evidence ref: {sorted(unknown)}")
                        if any(item.affected_task_refs for item in draft.findings):
                            raise ValueError("generic fixture review에는 affected Task ref를 둘 수 없습니다.")
                        unknown_codes = {
                            item.finding_code for item in draft.findings if item.finding_code not in taxonomy
                        }
                        if unknown_codes:
                            raise ValueError(f"taxonomy 밖의 finding code: {sorted(unknown_codes)}")
                        return draft

                    start = len(runner.receipts)
                    try:
                        result = runner.run(request, validator=validate)
                        fixture_result = _review_result(
                            fixture,
                            order_seed=seed,
                            raw=result.payload,
                            reviewer_role=reviewer_role,
                        )
                        receipts = runner.receipts[start:]
                    except StructuredRoleError as error:
                        _write_json(
                            destination / "last-error.json",
                            {
                                "error": type(error).__name__,
                                "message": str(error),
                                "receipts": [
                                    item.model_dump(mode="json") for item in error.receipts
                                ],
                            },
                        )
                        if _is_rate_limit(error):
                            store.set_state(
                                EvaluationRunStatus.PAUSED_RATE_LIMIT,
                                updated_at=utc_now(),
                                reason=str(error),
                            )
                            raise
                        receipts = list(error.receipts)
                        if not receipts or receipts[-1].status != "schema_failed":
                            store.set_state(
                                EvaluationRunStatus.FAILED,
                                updated_at=utc_now(),
                                reason=str(error),
                            )
                            raise
                        fixture_result = FixtureResult(
                            opaque_case_ref=fixture.opaque_case_ref,
                            order_seed=seed,
                            schema_valid=False,
                        )
                    checkpoint = EvaluationCellCheckpoint(
                        contract_digest=contract.contract_digest,
                        fixture_digest=fixture.fixture_digest,
                        order_seed=seed,
                        raw_structured_assessment={
                            "fixture_result": fixture_result.model_dump(mode="json")
                        },
                        runner_receipts=tuple(item.model_dump(mode="json") for item in receipts),
                    )
                    store.put(checkpoint)
                    results.append(fixture_result)
        except Exception:
            if store.state().status is EvaluationRunStatus.RUNNING:
                store.set_state(
                    EvaluationRunStatus.FAILED,
                    updated_at=utc_now(),
                    reason="role fixture 실행 중 복구 불가능한 오류",
                )
            raise
        # resume에서 순회 순서와 무관하게 모든 완료 cell을 정확히 다시 읽는다.
        results = [
            _checkpoint_fixture_result(store.completed(digest, seed))  # type: ignore[arg-type]
            for seed in contract.order_seeds
            for digest in contract.fixture_digests
            if store.completed(digest, seed) is not None
        ]
        if len(results) != contract.expected_cell_count:
            raise QualificationRunError("role fixture 완료 cell 수가 계약과 다릅니다.")
        report = evaluate_role_fixtures(
            catalog=catalog,
            results=tuple(results),
            prompt_digest=contract.prompt_digest,
            output_schema_digest=contract.output_schema_digest,
            model_lock_digest=contract.model_lock_digest,
        )
        store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
        scope_report = ScopeQualificationReport(
            scope=contract.scope,
            contract_digest=contract.contract_digest,
            status=EvaluationRunStatus.COMPLETED,
            passed=report.passed,
            metrics=report.metrics.model_dump(mode="json"),
            failures=report.failures,
            generated_at=utc_now(),
        )
        _write_json(destination / "role-qualification-report.json", report)
        _write_json(destination / "qualification-report.json", scope_report)
        return destination, scope_report


def _planning_contract(
    root: Path,
    catalog: PlanningScenarioCatalog,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
) -> EvaluationContract:
    import inspect
    from . import goal as goal_roles, planner_roles
    from .goal import GoalNormalizationProposal
    from .planner_roles import (
        PlanExpansionEnvelope, PlanReviewEnvelope, SkeletonBatchDraft, SkeletonCandidateDraft,
    )

    return EvaluationContract(
        scope=EvaluationScope.FULL_PLANNING_PIPELINE,
        fixture_digests=tuple(item.scenario_digest for item in catalog.scenarios),
        scenario_set_digest=catalog.catalog_digest,
        order_seeds=ORDER_SEEDS,
        expected_cell_count=len(catalog.scenarios) * len(ORDER_SEEDS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest({"pipeline": "goal-to-selected-plan", "budget_calls": 14, "versions": 5}),
        threshold_digest=sha256_digest({"clean_selected": True, "adversarial_blocked": True, "defect_count": 0}),
        taxonomy_digest=sha256_digest({"dispositions": ["selected", "blocked"],
                                      "review_findings": ReviewDraft.model_json_schema()}),
        prompt_digest=sha256_digest(
            {
                role.__name__: inspect.getsource(role)
                for role in (GoalNormalizerAdapter, GoalReviewerAdapter, SkeletonGeneratorAdapter,
                             SkeletonReviewerAdapter, PlanExpanderAdapter, PlanReviewerAdapter)
            } | {
                "shared_instructions": {
                    name: value for module in (goal_roles, planner_roles)
                    for name, value in vars(module).items()
                    if name.endswith("_INSTRUCTIONS") and isinstance(value, str)
                },
                "inspection_input_projection": inspect.getsource(planner_roles.inspection_source_catalog),
                "inspection_source_verification": inspect.getsource(planner_roles.inspection_file_content),
            }
        ),
        output_schema_digest=sha256_digest(
            {
                model.__name__: strict_json_output_schema(model.model_json_schema())
                for model in (GoalNormalizationProposal, SkeletonBatchDraft, SkeletonCandidateDraft,
                              PlanExpansionEnvelope, PlanReviewEnvelope, ReviewDraft)
            }
        ),
        model_lock_digest=_model_lock(inventory, roles),
    )


def _profile(project_id: str) -> ProjectProfileRevision:
    definition = ProjectProfileDefinition(
        product_goal="FlowMarshal qualification fixture를 안전하고 검증 가능하게 처리한다.",
        lifecycle_stage=LifecycleStage.DEVELOPMENT,
        criticality=Criticality.HIGH,
        compatibility_policy=(
            "공개 Python API 이름과 호출 계약을 보존한다. 현재 프로젝트는 원본 fixture의 작업용 "
            "복사본이며 Goal이 허용한 수정은 가능하다. 저장소에 있는 별도 원본 fixture는 수정하지 않는다."
        ),
        validation_policy=("Task validation과 plan-level Goal Test를 분리한다.",),
        runtime_requirements=("local", "Python 3.10+"),
    )
    return ProjectProfileRevision(
        profile_revision_id=new_id("profile_revision"),
        project_id=project_id,
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )


def _planning_cell(
    *,
    scenario: PlanningScenario,
    seed: int,
    fixture_root: Path,
    runtime: CodexAppServerRuntime,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    work_root: Path | None = None,
    progress_sink: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], tuple[RoleCallReceipt, ...]]:
    started = time.monotonic()
    first_feasible: list[int] = []
    workspace_context = (
        tempfile.TemporaryDirectory(
            prefix=f"flowmarshal-{scenario.scenario_id}-", ignore_cleanup_errors=True
        )
        if work_root is None
        else nullcontext(str(work_root.resolve()))
    )
    with workspace_context as temp:
        workspace = Path(temp) / "project"
        if workspace.exists():
            def file_digests(directory: Path) -> dict[str, str]:
                return {
                    path.relative_to(directory).as_posix(): sha256_bytes(path.read_bytes())
                    for path in directory.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts
                }
            if file_digests(workspace) != file_digests(fixture_root):
                raise QualificationRunError("미완료 planning workspace가 원본 fixture와 다릅니다.")
        else:
            shutil.copytree(
                fixture_root,
                workspace,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        project_id = new_id("project")
        profile = _profile(project_id)
        project_map = ProjectMapper().build(project_id=project_id, root=workspace, revision_no=1)
        runner = (CodexStructuredRoleRunner(runtime) if progress_sink is None else
                  CodexStructuredRoleRunner(runtime, progress_sink=progress_sink))
        normalizer = GoalNormalizerAdapter(
            runner,
            model=roles.normalizer.model,
            effort=roles.normalizer.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
        )
        goal_reviewer = GoalReviewerAdapter(
            runner,
            model=roles.critical_reviewer.model,
            effort=roles.critical_reviewer.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
        )
        prepared = GoalPreparationPipeline(normalizer, goal_reviewer).prepare(
            project_id=project_id,
            profile=profile,
            source_request=scenario.source_request,
            observed_facts=goal_context_observations(project_map, scenario.source_request),
        )
        goal = prepared.goal_contract
        blocking_questions = [
            item.model_dump(mode="json") for item in prepared.proposal.unresolved_questions
            if item.blocking
        ]
        cell: dict[str, Any] = {
            "scenario_id": scenario.scenario_id,
            "scenario_digest": scenario.scenario_digest,
            "workspace": str(workspace),
            "order_seed": seed,
            "expected_disposition": scenario.expected_disposition,
            "goal_status": goal.status.value,
            "selected": False,
            "logical_role_calls": 2,
            "candidate_versions": 0,
            "finding_codes": list(goal.preparation_binding.finding_codes),
            "passed": False,
            "failure": None,
            "goal_preparation": prepared.model_dump(mode="json"),
            "blocking_questions": blocking_questions,
            "latency_ms_to_first_feasible": None,
            "latency_ms_to_disposition": max(1, int((time.monotonic() - started) * 1000)),
        }
        if goal.status is not RevisionStatus.READY:
            cell["passed"] = scenario.expected_disposition == "blocked" and bool(blocking_questions)
            if not cell["passed"]:
                cell["failure"] = (
                    f"clean scenario Goal이 {goal.status.value}로 차단됨"
                    if scenario.expected_disposition == "selected"
                    else "정보 부족 입력에 구조화된 blocking 질문이 없음"
                )
            return cell, tuple(runner.receipts)

        state = StateSnapshot(
            snapshot_id=new_id("snapshot"),
            project_id=project_id,
            goal_contract_digest=goal.definition_digest,
            version=1,
            scope_fingerprint=sha256_digest(
                {"goal": goal.definition_digest, "map": project_map.revision_digest}
            ),
            facts=(
                StateFact(
                    fact_id="fact_project_map",
                    predicate="qualification project map is available",
                    value=project_map.revision_digest,
                    source_ref="project-map",
                    evidence_digest=project_map.revision_digest,
                ),
            ),
            observed_at=utc_now(),
        )
        assignment = ModelAssignmentContract(
            executor=RoleAssignmentPolicy(
                role="executor",
                preferred_model=roles.executor.model,
                preferred_effort=roles.executor.effort,
            ),
            validator=RoleAssignmentPolicy(
                role="validator",
                preferred_model=roles.validator.model,
                preferred_effort=roles.validator.effort,
            ),
            independence_required=True,
        )
        AssignmentResolver().resolve_contract(assignment, inventory)
        assigner = RuleBasedTaskAssigner(
            inspect_assignment=assignment,
            standard_assignment=assignment,
            critical_assignment=assignment,
        )
        generator = SkeletonGeneratorAdapter(
            runner,
            model=roles.skeleton_generator.model,
            effort=roles.skeleton_generator.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
        )
        skeleton_reviewer = SkeletonReviewerAdapter(
            runner,
            model=roles.general_reviewer.model,
            effort=roles.general_reviewer.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
        )
        expander = PlanExpanderAdapter(
            runner,
            assigner,
            model=roles.plan_expander.model,
            effort=roles.plan_expander.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
        )
        plan_reviewer = PlanReviewerAdapter(
            runner,
            model=roles.general_reviewer.model,
            effort=roles.general_reviewer.effort,
            inventory_digest=inventory.inventory_digest,
            cwd=workspace,
            critical_model=roles.critical_reviewer.model,
            critical_effort=roles.critical_reviewer.effort,
        )
        outcome = SkeletonFirstPlanner(
            generator, skeleton_reviewer, expander, plan_reviewer
        ).search(
            goal=goal,
            state=state,
            project_map=project_map,
            budget=PlanningBudgetPolicy(max_logical_role_calls=12),
            candidate_count=scenario.candidate_count,
            feasible_observer=lambda _evaluation: first_feasible.append(max(1, int((time.monotonic() - started) * 1000))),
        )
        selected = outcome.selected_activation_digest is not None
        findings = sorted(
            {
                item.finding_code
                for evaluation in outcome.skeleton_evaluations
                for item in evaluation.deterministic_findings
            }
            | {
                item.finding_code
                for evaluation in outcome.plan_evaluations
                for item in evaluation.deterministic_findings
            }
        )
        cell.update(
            {
                "selected": selected,
                "logical_role_calls": 2 + outcome.logical_role_calls,
                "candidate_versions": outcome.candidate_versions,
                "finding_codes": sorted(set(cell["finding_codes"]) | set(findings)),
                "selected_activation_digest": outcome.selected_activation_digest,
                "planning_outcome": outcome.model_dump(mode="json"),
                "latency_ms_to_first_feasible": first_feasible[0] if first_feasible else None,
                "latency_ms_to_disposition": max(1, int((time.monotonic() - started) * 1000)),
            }
        )
        failures: list[str] = []
        if scenario.expected_disposition == "selected" and not selected:
            failures.append("clean scenario에서 실행 가능한 Plan을 선택하지 못함")
        if scenario.expected_disposition == "blocked":
            failures.append("정보 부족 입력에서 필수 정보 요청 없이 ready planning에 진입함")
        if 2 + outcome.logical_role_calls > 14:
            failures.append("logical 역할 호출이 14회를 초과함")
        if outcome.candidate_versions > 5:
            failures.append("candidate version이 5개를 초과함")
        selected_evaluation = next(
            (
                item
                for item in outcome.plan_evaluations
                if item.plan.activation_digest == outcome.selected_activation_digest
            ),
            None,
        )
        if selected_evaluation is not None and selected_evaluation.deterministic_findings:
            failures.append("선택 Plan에 deterministic finding이 남음")
        cell["passed"] = not failures
        cell["failure"] = "; ".join(failures) or None
        return cell, tuple(runner.receipts)


def run_full_planning_pipeline(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    codex_bin: Path | str | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    base = (root or project_root()).resolve(strict=True)
    preflight_failures = _preflight(base)
    if preflight_failures:
        raise QualificationRunError("; ".join(preflight_failures))
    roles = role_configuration or default_role_configuration(base)
    catalog = PlanningScenarioCatalog.load(
        base / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    with CodexAppServerRuntime(codex_bin=codex_bin) as runtime:
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _planning_contract(base, catalog, inventory, roles)
        destination = (
            run_root
            or _default_run_root(base, "full-planning-pipeline", contract.contract_digest[7:15])
        ).resolve()
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        if store.state().status is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        _write_json(
            destination / "run-metadata.json",
            {
                "scope": "full-planning-pipeline",
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
            },
        )
        fixture_root = base / "tests" / "fixtures" / "engine" / "live-smoke-project"
        scenarios_by_digest = {item.scenario_digest: item for item in catalog.scenarios}
        try:
            for seed in contract.order_seeds:
                ordered = list(catalog.scenarios)
                random.Random(seed).shuffle(ordered)
                for scenario in ordered:
                    if store.completed(scenario.scenario_digest, seed) is not None:
                        continue
                    try:
                        cell, receipts = _planning_cell(
                            scenario=scenario,
                            seed=seed,
                            fixture_root=fixture_root,
                            runtime=runtime,
                            inventory=inventory,
                            roles=roles,
                            work_root=destination / "work" / f"seed-{seed}" / scenario.scenario_id,
                            progress_sink=_role_progress(destination, scenario_id=scenario.scenario_id, order_seed=seed),
                        )
                    except StructuredRoleError as error:
                        _write_json(
                            destination / "last-error.json",
                            {
                                "error": type(error).__name__,
                                "message": str(error),
                                "receipts": [
                                    item.model_dump(mode="json") for item in error.receipts
                                ],
                                "scenario_id": scenario.scenario_id,
                                "order_seed": seed,
                            },
                        )
                        if _is_rate_limit(error):
                            store.set_state(
                                EvaluationRunStatus.PAUSED_RATE_LIMIT,
                                updated_at=utc_now(),
                                reason=str(error),
                            )
                            raise
                        receipts = error.receipts
                        if not receipts or receipts[-1].status != "schema_failed":
                            raise
                        cell = {
                            "scenario_id": scenario.scenario_id,
                            "scenario_digest": scenario.scenario_digest,
                            "order_seed": seed,
                            "expected_disposition": scenario.expected_disposition,
                            "passed": False,
                            "schema_valid": False,
                            "failure": str(error),
                        }
                    store.put(
                        EvaluationCellCheckpoint(
                            contract_digest=contract.contract_digest,
                            fixture_digest=scenario.scenario_digest,
                            order_seed=seed,
                            raw_structured_assessment=cell,
                            runner_receipts=tuple(item.model_dump(mode="json") for item in receipts),
                        )
                    )
        except Exception:
            if store.state().status is EvaluationRunStatus.RUNNING:
                store.set_state(
                    EvaluationRunStatus.FAILED,
                    updated_at=utc_now(),
                    reason="full planning pipeline 실행 중 복구 불가능한 오류",
                )
            raise
        cells = [
            store.completed(digest, seed).raw_structured_assessment  # type: ignore[union-attr]
            for seed in contract.order_seeds
            for digest in contract.fixture_digests
        ]
        failures = tuple(
            f"{item['scenario_id']}/seed-{item['order_seed']}: {item.get('failure') or 'FAIL'}"
            for item in cells
            if not bool(item.get("passed"))
        )
        clean_selected = sum(
            1
            for item in cells
            if item.get("expected_disposition") == "selected" and item.get("selected")
        )
        adversarial_blocked = sum(
            1
            for item in cells
            if item.get("expected_disposition") == "blocked" and item.get("passed") and not item.get("selected", False)
        )
        store.set_state(EvaluationRunStatus.COMPLETED, updated_at=utc_now())
        report = ScopeQualificationReport(
            scope=contract.scope,
            contract_digest=contract.contract_digest,
            status=EvaluationRunStatus.COMPLETED,
            passed=not failures,
            metrics={
                "cell_count": len(cells),
                "clean_selected_count": clean_selected,
                "adversarial_blocked_count": adversarial_blocked,
                "max_logical_role_calls": max(int(item.get("logical_role_calls", 0)) for item in cells),
                "max_candidate_versions": max(int(item.get("candidate_versions", 0)) for item in cells),
                "schema_failure_count": sum(1 for item in cells if item.get("schema_valid") is False),
            },
            failures=failures,
            generated_at=utc_now(),
        )
        _write_json(destination / "qualification-report.json", report)
        return destination, report


def load_run_metadata(run_root: Path | str) -> dict[str, Any]:
    path = Path(run_root).resolve() / "run-metadata.json"
    if not path.is_file():
        raise QualificationRunError("resume할 run-metadata.json이 없습니다.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise QualificationRunError("run metadata는 JSON object여야 합니다.")
    return value


def resume_run(run_root: Path | str) -> tuple[Path, ScopeQualificationReport]:
    destination = Path(run_root).resolve(strict=True)
    completed_report = destination / "qualification-report.json"
    if completed_report.is_file():
        report = ScopeQualificationReport.model_validate_json(
            completed_report.read_text(encoding="utf-8")
        )
        if report.status is EvaluationRunStatus.COMPLETED:
            metadata = load_run_metadata(destination)
            contract = EvaluationContract.model_validate_json(
                (destination / "evaluation-contract.json").read_text(encoding="utf-8")
            )
            base = Path(metadata["project_root"]).resolve(strict=True)
            if source_manifest_digest(base) != contract.source_manifest_digest:
                raise QualificationRunError("완료 run 이후 source 계약이 변경됐습니다. 새 run root를 사용하세요.")
            return destination, report
    metadata = load_run_metadata(destination)
    base = Path(metadata["project_root"]).resolve(strict=True)
    scope = metadata["scope"]
    if scope == "deterministic":
        return run_deterministic(root=base, run_root=destination)
    roles = EngineRoleConfiguration.model_validate(metadata["role_configuration"])
    codex_bin = metadata.get("codex_bin")
    if scope == "role-fixture":
        return run_role_fixture(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
        )
    if scope == "full-planning-pipeline":
        return run_full_planning_pipeline(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
        )
    if scope == "project-e2e":
        from .e2e_qualification import run_project_e2e

        return run_project_e2e(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
        )
    raise QualificationRunError(f"지원하지 않는 resume scope입니다: {scope}")
