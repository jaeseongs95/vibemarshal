from __future__ import annotations

from .domain import ModelFallback

from .roles import make_role_request, verify_role_receipt

import json
import os
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

from pydantic import BaseModel, Field, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .context import ProjectMapper, goal_context_observations
from .domain import (
    BehaviorPolicy,
    Criticality,
    EffectPolicy,
    EngineModel,
    FindingSeverity,
    GateName,
    GoalContractDefinition,
    GoalContractRevision,
    GoalCriterion,
    LifecycleStage,
    MissionClass,
    ModelAssignmentContract,
    MutationPolicy,
    PlanningBudgetPolicy,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RevisionStatus,
    RoleAssignmentPolicy,
    SourceTrace,
    StateFact,
    StateSnapshot,
    ReviewFinding,
    ReviewerSubmission,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from .evaluation_budget import (
    EvaluationPolicies,
    budgeted_role_runner,
    evaluation_cell_provider_calls,
    initialize_cell_budget,
    policies_from_metadata,
    policy_contract_fragment,
    register_and_attach_goal,
    verify_metadata_digest,
    write_immutable_run_metadata,
)
from .evaluation import (
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationRunState,
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
from .goal import (
    FindingDraft,
    GoalNormalizerAdapter,
    GoalPreparationPipeline,
    GoalReviewerAdapter,
    ReviewDraft,
)
from .models import AssignmentResolver, EngineRoleConfiguration, ModelInventory
from .plan_inspection_provider import (
    PLAN_INSPECTION_PROVIDER_V1,
    PLAN_INSPECTION_PROVIDER_V2,
    PlanInspectionProviderVersion,
)
from .planner_roles import (
    PlanExpanderAdapter,
    PlanReviewerAdapter,
    RuleBasedTaskAssigner,
    SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter,
)
from .planning import PlanningSearchOutcome, SkeletonFirstPlanner
from .roles import (
    CodexStructuredRoleRunner,
    RoleCallRequest,
    RoleCallReceipt,
    StructuredRoleError,
    strict_json_output_schema,
)
from .runtime import CodexAppServerRuntime
from .role_execution import use_role_timeout_policy
from .qualification_manifest import (
    EvidenceProvenance,
    QualificationCellOutcome,
    QualificationCellStatus,
    QualificationFailureClass,
    QualificationFreezeManifest,
    QualificationHarnessReport,
    QualificationManifestError,
    QualificationSuiteManifest,
    classify_qualification_failure,
    evaluate_qualification_responsibilities,
    verify_candidate_wheel_metadata,
    verify_qualification_reproduction_bundle,
    write_qualification_reproduction_bundle,
)


ORDER_SEEDS = (17, 43, 89)
ROLE_INSTRUCTIONS = (
    "후보를 독립 검토한다. 직접 evidence가 있는 최소 finding만 제출하고, 같은 증상에서 "
    "상관 finding을 늘리지 않는다. finding code는 명확하고 재현 가능해야 한다. "
    "finding이 없을 때만 다섯 축 rating을 제출한다. status, admission, 최종 score는 선언하지 않는다. "
    "제공된 finding taxonomy 안의 코드만 사용한다. "
    "이 generic fixture 입력에는 권위 Task ref catalog가 없으므로 모든 finding의 "
    "affected_task_refs는 빈 배열이어야 하며 후보 ref를 넣지 않는다. "
    "입력은 source와 후보의 특정 의미를 검사하는 부분 발췌다. 생략된 필드나 문맥을 "
    "존재한다고 발명하지 않으며, 생략 자체를 명시적 결함으로 간주하지도 않는다. "
    "대상·자료가 없다고 명시된 경우는 정보 부족을 지적한다. 발췌에 없는 프로젝트 파일이나 "
    "외부 자료를 탐색하지 않는다. taxonomy의 코드 구분을 따르고 같은 증상에 넓은 코드와 "
    "구체적인 코드를 함께 붙이지 않는다. 서로 다른 요구·선택 집합 등 독립 증거가 있는 "
    "결함은 각각 제출한다."
    "아직 정하지 않은 설계 대안·새 산출물 배치·테스트 명령을 반드시 외부에서 제공받을 사실로 "
    "취급하지 않는다. source에 없는 가상의 동명 함수·다른 프로젝트가 있을 수 있다는 추측은 "
    "직접 evidence가 아니다. 제공된 ProjectProfile의 호환성·최소 변경 정책도 Goal의 근거다."
    "finding을 정하기 전에 같은 검토 객체에 관한 artifact 전체의 request, outcome, acceptance, "
    "constraint, context, profile, 관찰 파일·테스트와 plan 필드를 함께 대조한다. 한 필드가 짧은 "
    "label이거나 다른 필드의 세부를 반복하지 않아도, 제공된 다른 필드가 같은 대상·기대 결과·검사 "
    "근거를 구체화하면 그 의미를 누락으로 판정하지 않는다. 서로 다른 필드의 정보는 같은 객체와 "
    "요구에 속하고 서로 충돌하지 않을 때만 결합하며, 제공되지 않은 내용을 보충하지 않는다. "
    "key 자체가 없는 부분 발췌와 key가 존재하지만 빈 배열·null·빈 문자열인 명시적 관측을 "
    "구분한다. 명시적 빈 값도 해당 필드가 제공된 객체에 적용되고 필요하다는 근거가 있을 때만 "
    "부재 증거로 사용한다. acceptance의 존재·검증 가능성과 그 source binding의 존재·정합성은 "
    "독립적으로 검사하며 한 축의 finding으로 다른 축의 직접 결함을 대신하지 않는다. "
    "부분 발췌의 plan에 선언된 선택·방식은 요청과의 의미 정합성을 검토한다. 실행 전 계획에 "
    "실행 후 실측 증빙이 없다는 이유만으로 그 선언을 미충족으로 바꾸지 않는다. 특정 목적의 "
    "validation 설명을 전체 검사 목록으로 간주하여 다른 계획 필드마다 별도 검사가 없다고 "
    "추론하지 않는다. 검사 목록의 완결성이나 별도 증명 의무가 원문에 명시된 경우, 또는 "
    "제공된 검사 방식이 해당 요구를 검증할 수 없다는 직접 근거가 있는 경우에는 결함을 제출한다."
)


class GenericFixtureFindingDraft(FindingDraft):
    """Task catalog가 없는 generic 회귀 fixture 전용 finding 계약이다."""

    affected_task_refs: tuple[str, ...] = Field(default=(), max_length=0)


class GenericFixtureReviewDraft(ReviewDraft):
    """실제 Goal/Plan Reviewer의 Task ref 계약과 분리된 fixture 출력이다."""

    findings: tuple[GenericFixtureFindingDraft, ...] = ()


class PlanningScenario(EngineModel):
    scenario_id: str = Field(pattern=r"^S[0-9]{2}-[a-z0-9-]+$")
    path_kind: Literal["multi_path", "single_path"]
    source_request: str = Field(min_length=1, max_length=10_000)
    expected_disposition: Literal["selected", "blocked"]
    candidate_count: int = Field(ge=1, le=3)

    @property
    def scenario_digest(self) -> str:
        return sha256_digest(self)


class PlanningPartialFeasibleObservation(EngineModel):
    """schema 실패 전 처음 관측한 admissible Plan만 보존한다."""

    observed: bool
    latency_ms: int | None = Field(default=None, ge=1)
    plan_activation_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def observed_values_are_complete(self) -> "PlanningPartialFeasibleObservation":
        values = (self.latency_ms, self.plan_activation_digest)
        if self.observed and any(value is None for value in values):
            raise ValueError("관측된 feasible Plan에는 latency와 activation digest가 필요합니다.")
        if not self.observed and any(value is not None for value in values):
            raise ValueError("관측되지 않은 feasible Plan에는 값이 있으면 안 됩니다.")
        return self


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


DEVELOPER_SOURCE_ROOT_ENV = "FLOWMARSHAL_ENGINE_SOURCE_ROOT"


def project_root() -> Path:
    """평가/diagnostic 입력의 명시적 developer source root를 반환한다.

    사용자 wheel에는 config·fixture·legacy source가 포함되지 않는다. 따라서
    qualification은 설치 위치나 checkout 깊이를 역추적하지 않고, 호출자가
    ``FLOWMARSHAL_ENGINE_SOURCE_ROOT``로 지정한 source root에서만 실행한다.
    """
    configured = os.environ.get(DEVELOPER_SOURCE_ROOT_ENV)
    if not configured:
        raise QualificationRunError(
            f"{DEVELOPER_SOURCE_ROOT_ENV}를 승인된 개발 source root로 지정해야 합니다."
        )
    root = Path(configured).expanduser().resolve()
    required = (
        root / "pyproject.toml",
        root / "config" / "qualification-roles.json",
        root / "tests" / "fixtures" / "engine",
    )
    if not root.is_dir() or any(not path.exists() for path in required):
        raise QualificationRunError(
            f"{DEVELOPER_SOURCE_ROOT_ENV}가 재현 입력을 갖춘 source root가 아닙니다: {root}"
        )
    return root


def default_role_configuration(root: Path | None = None) -> EngineRoleConfiguration:
    base = root or project_root()
    return EngineRoleConfiguration.model_validate_json(
        (base / "config" / "qualification-roles.json").read_text(encoding="utf-8")
    )


def qualification_suite_manifest(
    root: Path | None = None,
) -> QualificationSuiteManifest:
    base = root or project_root()
    return QualificationSuiteManifest.load(base / "config" / "qualification-suite.json")


def _manifest(root: Path) -> LegacyFreezeManifest:
    return LegacyFreezeManifest.load(root / "config" / "legacy-freeze-manifest.json")


def source_manifest_files(root: Path) -> dict[str, str]:
    """결정적 Gate가 실행하는 source·test·fixture 입력의 실제 결속을 반환한다."""

    paths: set[Path] = {
        root / "AGENTS.md",
        root / "README.md",
        root / "pyproject.toml",
        root / "requirements.lock",
        root / "setup.py",
        root / "scripts" / "installed_candidate_qualification.py",
        root / "config" / "legacy-freeze-manifest.json",
        root / "config" / "pre-1.0-performance-thresholds.json",
        root / "config" / "qualification-roles.json",
        root / "config" / "qualification-finding-taxonomy.json",
        root / "config" / "qualification-suite.json",
        root / "docs" / "orchestration-redesign.md",
        root / "docs" / "engine-cutover-adr.md",
        root / "docs" / "engine-package-install.md",
        root / "docs" / "engine-user-workflow.md",
        root / "docs" / "performance-release-floor.md",
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


def build_qualification_reproduction_bundle(
    *,
    root: Path,
    destination: Path,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    contracts: tuple[EvaluationContract, ...],
) -> QualificationFreezeManifest:
    """qualification 전 입력 축을 clean-install 전달용 bundle로 동결한다."""

    base = root.resolve(strict=True)
    if not contracts:
        raise QualificationManifestError("동결할 evaluation 계약이 없습니다.")
    suite = qualification_suite_manifest(base)
    files = source_manifest_files(base)
    fixture_documents = {
        f"contract:{item.scope.value}": {
            "fixture_digests": item.fixture_digests,
            "scenario_set_digest": item.scenario_set_digest,
            "order_seeds": item.order_seeds,
            "expected_cell_count": item.expected_cell_count,
        }
        for item in contracts
    }
    prompt_documents = {
        f"contract:{item.scope.value}": item.prompt_digest for item in contracts
    }
    schema_documents = {
        f"contract:{item.scope.value}": item.output_schema_digest for item in contracts
    }
    threshold_documents = {
        "suite": {
            "role_gate": suite.role_gate.model_dump(mode="json"),
            "planning_gate": suite.planning_gate.model_dump(mode="json"),
            "non_blocking_comparisons": suite.non_blocking_comparisons.model_dump(
                mode="json"
            ),
        },
        **{
            f"contract:{item.scope.value}": item.threshold_digest for item in contracts
        },
    }
    taxonomy_documents = {
        "suite_e2e_responsibilities": [
            item.model_dump(mode="json") for item in suite.e2e_responsibilities
        ],
        **{
            f"contract:{item.scope.value}": item.taxonomy_digest for item in contracts
        },
    }
    return write_qualification_reproduction_bundle(
        source_root=base,
        destination=destination,
        suite=suite,
        source_files=files,
        source_manifest_digest=source_manifest_digest(base),
        fixture_documents=fixture_documents,
        prompt_documents=prompt_documents,
        schema_documents=schema_documents,
        threshold_documents=threshold_documents,
        taxonomy_documents=taxonomy_documents,
        model_inventory_document=inventory.model_dump(mode="json"),
        model_lock_document=roles.operational_binding(inventory).model_dump(mode="json"),
        evaluator_files=(
            "scripts/installed_candidate_qualification.py",
            "src/flowmarshal/engine/evaluation.py",
            "src/flowmarshal/engine/qualification.py",
            "src/flowmarshal/engine/qualification_manifest.py",
            "src/flowmarshal/engine/e2e_qualification.py",
            "src/flowmarshal/engine/benchmark_safety.py",
        ),
    )


def _default_run_root(root: Path, scope_name: str, contract_hint: str) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return root / ".flowmarshal-engine-eval" / "runs" / f"{scope_name}-{stamp}-{contract_hint}"


_WINDOWS_LEGACY_MAX_PATH = 259
# 역할 runner의 new_id("model_call")와 실제 trace suffix에 결속한다.
_OPERATION_TRACE_FILENAME_BUDGET = len(
    "model_call_" + ("x" * 32) + ".operation-trace.jsonl"
)


def _role_cell_state_root(run_root: Path, *, order_seed: int, catalog_index: int) -> Path:
    """role fixture 원장을 짧고 계약 순서에 결속된 경로에 materialize한다.

    전체 fixture digest는 evaluation contract/checkpoint에 이미 결속된다. 파일시스템
    경로에는 catalog index를 사용해 충돌 가능성 없이 Windows MAX_PATH 여유를 확보한다.
    """

    if catalog_index < 0:
        raise QualificationRunError("role fixture catalog index는 음수일 수 없습니다.")
    state_root = run_root / "w" / f"s{order_seed}" / f"c{catalog_index:02d}"
    if sys.platform == "win32":
        trace_probe = (
            state_root
            / "artifacts"
            / "operation-traces"
            / ("x" * _OPERATION_TRACE_FILENAME_BUDGET)
        ).resolve()
        if len(str(trace_probe)) > _WINDOWS_LEGACY_MAX_PATH:
            raise QualificationRunError(
                "EVALUATION_ARTIFACT_PATH_TOO_LONG: provider 예약 전에 더 짧은 "
                f"--run-root를 선택해야 합니다: projected_length={len(str(trace_probe))}; "
                f"max={_WINDOWS_LEGACY_MAX_PATH}; state_root={state_root.resolve()}"
            )
    return state_root


def _planning_cell_work_root(run_root: Path, *, order_seed: int, catalog_index: int) -> Path:
    """Planning cell을 Windows operation-trace 경로 예산 안에 배치한다."""

    if catalog_index < 0:
        raise QualificationRunError("planning scenario catalog index는 음수일 수 없습니다.")
    work_root = run_root / "p" / f"s{order_seed}" / f"c{catalog_index:02d}"
    if sys.platform == "win32":
        trace_probe = (
            work_root
            / "budget-state"
            / "artifacts"
            / "operation-traces"
            / ("x" * _OPERATION_TRACE_FILENAME_BUDGET)
        ).resolve()
        if len(str(trace_probe)) > _WINDOWS_LEGACY_MAX_PATH:
            raise QualificationRunError(
                "EVALUATION_ARTIFACT_PATH_TOO_LONG: provider 예약 전에 더 짧은 "
                f"--run-root를 선택해야 합니다: projected_length={len(str(trace_probe))}; "
                f"max={_WINDOWS_LEGACY_MAX_PATH}; work_root={work_root.resolve()}"
            )
    return work_root


def _write_json(path: Path, value: Any) -> None:
    if isinstance(value, BaseModel):
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
        model_lock_format="flowmarshal-model-lock-v2",
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
                model_lock_format="flowmarshal-model-lock-v2",
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
    return roles.operational_binding(inventory).lock_digest


def _role_contract(
    root: Path,
    catalog: RegressionCatalog,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    policies: EvaluationPolicies,
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
        model_lock_format="flowmarshal-model-lock-v2",
        scope=EvaluationScope.ROLE_FIXTURE,
        fixture_digests=tuple(item.fixture_digest for item in catalog.fixtures),
        scenario_set_digest=catalog.catalog_digest,
        order_seeds=ORDER_SEEDS,
        expected_cell_count=len(catalog.fixtures) * len(ORDER_SEEDS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest(
            {"instructions": ROLE_INSTRUCTIONS, "oracle_hidden": True}
            | policy_contract_fragment(policies)
        ),
        threshold_digest=sha256_digest(
            {"recall": 0.90, "precision": 0.85, "critical_false_admission": 0, "clean_false_block": 0}
        ),
        taxonomy_digest=sha256_digest(taxonomy),
        prompt_digest=sha256_digest(ROLE_INSTRUCTIONS),
        output_schema_digest=sha256_digest(
            strict_json_output_schema(GenericFixtureReviewDraft.model_json_schema())
        ),
        model_lock_digest=_model_lock(inventory, roles),
    )


def _review_result(
    fixture: RegressionFixture,
    *,
    order_seed: int,
    raw: dict[str, Any],
    reviewer_role: str,
) -> FixtureResult:
    draft = GenericFixtureReviewDraft.model_validate(raw)
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


_FULL_PLANNING_PARTIAL_CELL_NON_RESUMABLE = (
    "FULL_PLANNING_PARTIAL_CELL_NON_RESUMABLE"
)
_FULL_PLANNING_PRE_PROVIDER_RATE_LIMIT = (
    "FULL_PLANNING_RATE_LIMIT_BEFORE_PROVIDER_CALL"
)


def _full_planning_rate_limit_reason(
    *,
    state_root: Path,
    scenario_id: str,
    order_seed: int,
    provider_calls: tuple[tuple[str, str], ...],
    original_reason: str,
    codex_bin: Path | str | None,
) -> tuple[EvaluationRunStatus, str]:
    database = (state_root / "flowmarshal-engine.sqlite3").resolve()
    artifacts = (state_root / "artifacts").resolve()
    if not provider_calls:
        reason = (
            f"{_FULL_PLANNING_PRE_PROVIDER_RATE_LIMIT}: scenario_id={scenario_id}; "
            f"order_seed={order_seed}; provider_calls=[]; provider effect가 시작되지 않아 "
            "동일 run root를 resume할 수 있습니다; next_action=resume_same_run_root; "
            f"original_reason={original_reason}"
        )
        return EvaluationRunStatus.PAUSED_RATE_LIMIT, reason[:5000]

    calls = ",".join(f"{call_id}={status}" for call_id, status in provider_calls)
    observe_command = (
        f'flowmarshal-engine --db "{database}" --artifacts "{artifacts}" '
        "project budget observe-role --call-id <unresolved-call-id>"
    )
    if codex_bin is not None:
        observe_command += f' --codex-bin "{Path(codex_bin).resolve()}"'
    reason = (
        f"{_FULL_PLANNING_PARTIAL_CELL_NON_RESUMABLE}: scenario_id={scenario_id}; "
        f"order_seed={order_seed}; provider_calls=[{calls}]; ledger={database}; "
        f"artifacts={artifacts}; 중간 cell 역할 산출물 checkpoint가 없어 이 run은 "
        "재개할 수 없습니다. 기존 thread의 terminal 관측이 필요한 unresolved 호출은 "
        f"연결 원장에서 먼저 관측하십시오; next_action={observe_command}; "
        "원장과 실패 run을 보존하고 미확인 효과와 예산을 대조한 뒤 "
        "승인된 반복 검증 범위에서 새 run root를 사용하십시오; "
        f"original_reason={original_reason}"
    )
    return EvaluationRunStatus.FAILED, reason[:5000]


def _record_full_planning_rate_limit(
    store: ImmutableCheckpointStore,
    *,
    state_root: Path,
    scenario_id: str,
    order_seed: int,
    original_reason: str,
    codex_bin: Path | str | None,
) -> EvaluationRunState:
    status, reason = _full_planning_rate_limit_reason(
        state_root=state_root,
        scenario_id=scenario_id,
        order_seed=order_seed,
        provider_calls=evaluation_cell_provider_calls(state_root),
        original_reason=original_reason,
        codex_bin=codex_bin,
    )
    return store.set_state(status, updated_at=utc_now(), reason=reason)


def _guard_full_planning_resume(
    destination: Path,
    *,
    base: Path,
    codex_bin: Path | str | None,
) -> None:
    """provider 호출이 시작된 미완료 cell을 런타임 진입 전에 차단한다."""

    contract_path = destination / "evaluation-contract.json"
    state_path = destination / "run-state.json"
    if not contract_path.is_file() or not state_path.is_file():
        return
    contract = EvaluationContract.model_validate_json(
        contract_path.read_text(encoding="utf-8")
    )
    if contract.scope is not EvaluationScope.FULL_PLANNING_PIPELINE:
        return
    if source_manifest_digest(base) != contract.source_manifest_digest:
        raise QualificationRunError(
            "resume 대상 source 계약이 변경됐습니다. 기존 run을 보존하고 새 run root를 사용하세요."
        )
    store = ImmutableCheckpointStore(destination, contract)
    state = store.state()
    if (
        state.status is EvaluationRunStatus.FAILED
        and state.reason is not None
        and state.reason.startswith(_FULL_PLANNING_PARTIAL_CELL_NON_RESUMABLE)
    ):
        raise QualificationRunError(state.reason)
    if state.status is EvaluationRunStatus.COMPLETED:
        return

    catalog = PlanningScenarioCatalog.load(
        base / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    if (
        catalog.catalog_digest != contract.scenario_set_digest
        or tuple(item.scenario_digest for item in catalog.scenarios)
        != contract.fixture_digests
    ):
        raise QualificationRunError("resume 대상 planning scenario 계약이 다릅니다.")
    for seed in contract.order_seeds:
        for catalog_index, scenario in enumerate(catalog.scenarios):
            if store.completed(scenario.scenario_digest, seed) is not None:
                continue
            state_root = (
                _planning_cell_work_root(
                    destination,
                    order_seed=seed,
                    catalog_index=catalog_index,
                )
                / "budget-state"
            )
            if not evaluation_cell_provider_calls(state_root):
                continue
            failed = _record_full_planning_rate_limit(
                store,
                state_root=state_root,
                scenario_id=scenario.scenario_id,
                order_seed=seed,
                original_reason=state.reason or "완료 checkpoint가 없는 provider 호출이 발견됐습니다.",
                codex_bin=codex_bin,
            )
            raise QualificationRunError(
                failed.reason or _FULL_PLANNING_PARTIAL_CELL_NON_RESUMABLE
            )


def run_role_fixture(
    *,
    root: Path | None = None,
    run_root: Path | None = None,
    role_configuration: EngineRoleConfiguration | None = None,
    codex_bin: Path | str | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    if evaluation_policies is None:
        raise QualificationRunError("EVALUATION_POLICY_REQUIRED")
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
    with CodexAppServerRuntime(
        codex_bin=codex_bin, project_binding=evaluation_policies.codex_project
    ) as runtime:
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _role_contract(base, catalog, inventory, roles, evaluation_policies)
        destination = (
            run_root or _default_run_root(base, "role-fixture", contract.contract_digest[7:15])
        ).resolve()
        catalog_indexes = {
            fixture.fixture_digest: index for index, fixture in enumerate(catalog.fixtures)
        }
        cell_state_roots = {
            (seed, fixture.fixture_digest): _role_cell_state_root(
                destination,
                order_seed=seed,
                catalog_index=catalog_indexes[fixture.fixture_digest],
            )
            for seed in contract.order_seeds
            for fixture in catalog.fixtures
        }
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        if store.state().status is EvaluationRunStatus.FAILED:
            raise QualificationRunError(
                "ROLE_FIXTURE_FAILED_RUN_REQUIRES_NEW_RUN_ROOT: 실패한 run의 원장과 "
                "provider effect evidence를 보존하고 새 Attempt에는 새 --run-root를 사용하세요."
            )
        qualification_suite_manifest(base)
        build_qualification_reproduction_bundle(
            root=base,
            destination=destination / "reproduction-bundle",
            inventory=inventory,
            roles=roles,
            contracts=(contract,),
        )
        audit_path = destination / ("inventory-observation-" + inventory.inventory_digest[7:] + ".json")
        if not audit_path.exists():
            _write_json(audit_path, roles.operational_binding(inventory))
        if store.state().status is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        write_immutable_run_metadata(
            destination / "run-metadata.json",
            {
                "scope": "role-fixture",
                "evaluation_contract_digest": contract.contract_digest,
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
            },
            evaluation_policies,
        )
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
                    cell_ref = {
                        "contract": contract.contract_digest,
                        "fixture": fixture.fixture_digest,
                        "seed": seed,
                    }
                    project_id = _qualification_entity("project", cell_ref)
                    profile = _profile(project_id).model_copy(
                        update={
                            "profile_revision_id": _qualification_entity(
                                "profile_revision", cell_ref
                            )
                        }
                    )
                    state_root = cell_state_roots[(seed, fixture.fixture_digest)]
                    service, manager = initialize_cell_budget(
                        state_root=state_root,
                        workspace=base,
                        project_id=project_id,
                        profile=profile,
                        policies=evaluation_policies,
                    )
                    goal_id = _qualification_entity("goal", cell_ref)
                    fixture_goal = _fixture_goal(
                        project_id=project_id,
                        profile=profile,
                        goal_id=goal_id,
                        fixture=fixture,
                    )
                    register_and_attach_goal(service, manager, fixture_goal)
                    runner = budgeted_role_runner(
                        runtime,
                        service,
                        project_id=project_id,
                        goal_id=goal_id,
                        goal_digest=fixture_goal.definition_digest,
                        progress_sink=_role_progress(destination),
                        operational_binding=roles.operational_binding(inventory),
                    )
                    binding = roles.critical_reviewer if fixture.critical else roles.general_reviewer
                    reviewer_role = "critical_reviewer" if fixture.critical else "general_reviewer"
                    evidence_catalog = {"artifact:candidate": fixture.artifact}
                    with use_role_timeout_policy(evaluation_policies.role_timeouts):
                        request = make_role_request(
                            role=reviewer_role,
                            instructions=ROLE_INSTRUCTIONS,
                            payload=fixture.model_input()
                            | {
                                "evidence_catalog": evidence_catalog,
                                "finding_taxonomy": taxonomy,
                            },
                            output_schema=GenericFixtureReviewDraft.model_json_schema(),
                            model=binding.model,
                            effort=binding.effort, allowed_fallbacks=binding.allowed_fallbacks,
                            inventory_digest=inventory.inventory_digest, inventory=inventory,
                            cwd=str(base),
                        )

                    def validate(raw: dict[str, Any]) -> GenericFixtureReviewDraft:
                        draft = GenericFixtureReviewDraft.model_validate(raw)
                        unknown = {
                            ref
                            for finding in draft.findings
                            for ref in finding.evidence_refs
                            if ref not in evidence_catalog
                        }
                        if unknown:
                            raise ValueError(f"제공되지 않은 evidence ref: {sorted(unknown)}")
                        unknown_codes = {
                            item.finding_code for item in draft.findings if item.finding_code not in taxonomy
                        }
                        if unknown_codes:
                            raise ValueError(f"taxonomy 밖의 finding code: {sorted(unknown_codes)}")
                        return draft

                    start = len(runner.receipts)
                    try:
                        with use_role_timeout_policy(evaluation_policies.role_timeouts):
                            result = runner.run(request, validator=validate)
                        verify_role_receipt(request, result)
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
                                "failure_class": classify_qualification_failure(error).value,
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
                        model_lock_format="flowmarshal-model-lock-v2",
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
    policies: EvaluationPolicies,
    *,
    inspection_provider_contract: PlanInspectionProviderVersion = PLAN_INSPECTION_PROVIDER_V1,
) -> EvaluationContract:
    import inspect
    from . import goal as goal_roles, planner_roles
    from .goal import GoalNormalizationProposal
    from .goal_feedback import GoalPreparationRefiner, GoalRefinementProposal
    from .planner_roles import (
        PlanExpansionEnvelope, PlanExpansionEnvelopeV2,
        PlanReviewEnvelope, PlanReviewEnvelopeV2,
        SkeletonBatchDraft, SkeletonCandidateDraft, SkeletonRefinementDraft,
    )
    from .plan_inspection_v2 import FINDING_TAXONOMY_V2, PLAN_INSPECTION_V2_INSTRUCTIONS
    from .planning_recovery import PlanningRecoveryPolicy
    from .plan_review_adjudication import PlanReviewAdjudicationDraft, adjudicate_plan_review
    from .planner_roles import PlanRefinementDraft

    if inspection_provider_contract == PLAN_INSPECTION_PROVIDER_V1:
        expansion_envelope, review_envelope = PlanExpansionEnvelope, PlanReviewEnvelope
        provider_contract_values: dict[str, Any] = {}
    elif inspection_provider_contract == PLAN_INSPECTION_PROVIDER_V2:
        expansion_envelope, review_envelope = PlanExpansionEnvelopeV2, PlanReviewEnvelopeV2
        provider_contract_values = {
            "inspection_provider_contract": inspection_provider_contract,
            "inspection_provider_instructions": PLAN_INSPECTION_V2_INSTRUCTIONS,
            "planning_recovery_policy": PlanningRecoveryPolicy().model_dump(mode="json"),
            "adjudication_request_builder": inspect.getsource(adjudicate_plan_review),
            "inspection_provider_taxonomy": {
                key: (gate.value, severity.value)
                for key, (gate, severity) in FINDING_TAXONOMY_V2.items()
            },
        }
    else:
        raise QualificationRunError("INSPECTION_PROVIDER_CONTRACT_UNSUPPORTED")

    return EvaluationContract(
        model_lock_format="flowmarshal-model-lock-v2",
        scope=EvaluationScope.FULL_PLANNING_PIPELINE,
        fixture_digests=tuple(item.scenario_digest for item in catalog.scenarios),
        scenario_set_digest=catalog.catalog_digest,
        order_seeds=ORDER_SEEDS,
        expected_cell_count=len(catalog.scenarios) * len(ORDER_SEEDS),
        role_configuration_digest=roles.configuration_digest,
        source_manifest_digest=source_manifest_digest(root),
        rules_digest=sha256_digest(
            {"pipeline": "goal-to-selected-plan", "budget_calls": 14, "versions": 5,
             "goal_feedback": "bounded-goal-feedback-v1"}
            | policy_contract_fragment(policies)
            | ({"inspection_provider_contract": inspection_provider_contract}
               if provider_contract_values else {})
            | ({"planning_recovery_policy": PlanningRecoveryPolicy().model_dump(mode="json")}
               if inspection_provider_contract == PLAN_INSPECTION_PROVIDER_V2 else {})
        ),
        threshold_digest=sha256_digest({"clean_selected": True, "adversarial_blocked": True, "defect_count": 0}),
        taxonomy_digest=sha256_digest({"dispositions": ["selected", "blocked"],
                                      "review_findings": ReviewDraft.model_json_schema()}),
        prompt_digest=sha256_digest(
            ({
                role.__name__: inspect.getsource(role)
                for role in (GoalNormalizerAdapter, GoalReviewerAdapter, GoalPreparationRefiner,
                             SkeletonGeneratorAdapter,
                             SkeletonReviewerAdapter, PlanExpanderAdapter, PlanReviewerAdapter)
            } | {
                "shared_instructions": {
                    name: value for module in (goal_roles, planner_roles)
                    for name, value in vars(module).items()
                    if name.endswith("_INSTRUCTIONS") and isinstance(value, str)
                },
                "inspection_input_projection": inspect.getsource(planner_roles.inspection_source_catalog),
                "inspection_source_verification": inspect.getsource(planner_roles.inspection_file_content),
            } | provider_contract_values)
        ),
        output_schema_digest=sha256_digest(
            {
                model.__name__: strict_json_output_schema(model.model_json_schema())
                for model in (GoalNormalizationProposal, GoalRefinementProposal,
                              SkeletonBatchDraft, SkeletonCandidateDraft,
                              SkeletonRefinementDraft,
                              expansion_envelope, review_envelope, ReviewDraft,
                              PlanningPartialFeasibleObservation, PlanRefinementDraft,
                              PlanningSearchOutcome)
                + ((PlanReviewAdjudicationDraft,) if inspection_provider_contract == PLAN_INSPECTION_PROVIDER_V2 else ())
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


def _qualification_entity(prefix: str, value: Any) -> str:
    return prefix + "_" + sha256_digest(value).split(":", 1)[1][:32]


def _fixture_goal(
    *, project_id: str, profile: ProjectProfileRevision, goal_id: str, fixture: RegressionFixture
) -> GoalContractRevision:
    source = json.dumps(fixture.model_input(), ensure_ascii=False, sort_keys=True)
    source_digest = sha256_bytes(source.encode("utf-8"))
    definition = GoalContractDefinition(
        project_id=project_id,
        source_request=source,
        source_request_digest=source_digest,
        mission_class=MissionClass.ANALYSIS_AUDIT,
        observable_outcome="동결 fixture 후보를 직접 evidence와 taxonomy로 독립 평가한다.",
        hard_acceptance=(GoalCriterion(
            criterion_id="ac_fixture_review",
            statement="fixture의 기대 finding 또는 clean 판정을 정확히 제출한다.",
            validation_intent="원시 역할 receipt와 fixture oracle을 분리해 대조한다.",
            trace_refs=("trace_fixture",),
        ),),
        source_traces=(SourceTrace(
            trace_id="trace_fixture",
            source_ref=f"qualification-fixture:{fixture.opaque_case_ref}",
            statement=source,
            source_digest=source_digest,
        ),),
        effect_policy=EffectPolicy(
            mutation_policy=MutationPolicy.READ_ONLY,
            behavior_policy=BehaviorPolicy.NOT_APPLICABLE,
        ),
        profile_definition_digest=profile.definition_digest,
    )
    return GoalContractRevision(
        goal_revision_id=_qualification_entity(
            "goal_revision", {"goal_id": goal_id, "fixture": fixture.fixture_digest}
        ),
        goal_id=goal_id,
        revision_no=1,
        definition=definition,
        definition_digest=definition.definition_digest,
        status=RevisionStatus.READY,
        created_at=utc_now(),
    )


def _bind_planning_runner_goal(runner: Any, goal: GoalContractRevision) -> None:
    """Goal 등록 뒤의 planning 호출만 확정된 Goal revision으로 예약한다."""

    runner.goal_digest = goal.definition_digest


def _planning_cell(
    *,
    scenario: PlanningScenario,
    seed: int,
    fixture_root: Path,
    runtime: CodexAppServerRuntime,
    inventory: ModelInventory,
    roles: EngineRoleConfiguration,
    inspection_provider_contract: PlanInspectionProviderVersion = PLAN_INSPECTION_PROVIDER_V1,
    work_root: Path | None = None,
    progress_sink: Callable[[dict[str, Any]], None] | None = None,
    evaluation_policies: EvaluationPolicies | None = None,
    retain_execution_checkpoint: bool = False,
    partial_feasible_observer: Callable[[PlanningPartialFeasibleObservation], None] | None = None,
) -> tuple[dict[str, Any], tuple[RoleCallReceipt, ...]]:
    from .goal_feedback import GoalPreparationRefiner
    from .planning_recovery import PlanningRecoveryPolicy
    if retain_execution_checkpoint and (work_root is None or evaluation_policies is None):
        raise QualificationRunError("실행 lifecycle 연결에는 영속 cell 경로와 예산 정책이 필요합니다.")
    started = time.monotonic()
    first_feasible: PlanningPartialFeasibleObservation | None = None

    def observe_first_feasible(evaluation: Any) -> None:
        nonlocal first_feasible
        if first_feasible is not None:
            return
        observation = PlanningPartialFeasibleObservation(
            observed=True,
            latency_ms=max(1, int((time.monotonic() - started) * 1000)),
            plan_activation_digest=evaluation.plan.activation_digest,
        )
        first_feasible = observation
        if partial_feasible_observer is not None:
            partial_feasible_observer(observation)
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
        cell_ref = {
            "scenario": scenario.scenario_digest,
            "seed": seed,
            "policy": None if evaluation_policies is None else evaluation_policies.policy_digest,
        }
        project_id = _qualification_entity("project", cell_ref)
        profile = _profile(project_id).model_copy(
            update={
                "profile_revision_id": _qualification_entity("profile_revision", cell_ref)
            }
        )
        project_map = ProjectMapper().build(project_id=project_id, root=workspace, revision_no=1)
        goal_id = _qualification_entity("goal", cell_ref)
        service = manager = None
        if evaluation_policies is None:
            runner = CodexStructuredRoleRunner(
                runtime,
                progress_sink=progress_sink,
                operational_binding=roles.operational_binding(inventory),
                max_schema_recovery_attempts=0,
                ephemeral_threads=False,
            )
        else:
            service, manager = initialize_cell_budget(
                state_root=(Path(temp) / "budget-state"),
                workspace=workspace,
                project_id=project_id,
                profile=profile,
                policies=evaluation_policies,
            )
            runner = budgeted_role_runner(
                runtime,
                service,
                project_id=project_id,
                goal_id=goal_id,
                goal_digest=None,
                progress_sink=progress_sink,
                operational_binding=roles.operational_binding(inventory),
            )
        normalizer = GoalNormalizerAdapter(
            runner,
            model=roles.normalizer.model,
            effort=roles.normalizer.effort,
            allowed_fallbacks=roles.normalizer.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
        )
        goal_reviewer = GoalReviewerAdapter(
            runner,
            model=roles.critical_reviewer.model,
            effort=roles.critical_reviewer.effort,
            allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
        )
        timeout_context = (
            nullcontext()
            if evaluation_policies is None
            else use_role_timeout_policy(evaluation_policies.role_timeouts)
        )
        observed_facts = goal_context_observations(project_map, scenario.source_request)
        with timeout_context:
            prepared = GoalPreparationPipeline(normalizer, goal_reviewer).prepare(
                project_id=project_id,
                profile=profile,
                source_request=scenario.source_request,
                observed_facts=observed_facts,
                goal_id=goal_id,
            )
            goal_refinement = None
            if (
                prepared.goal_contract.status is RevisionStatus.CONFLICT
                and prepared.review.findings
                and all(item.remediable for item in prepared.review.findings)
                and not any(item.blocking for item in prepared.proposal.unresolved_questions)
            ):
                goal_refinement = GoalPreparationRefiner(
                    normalizer, goal_reviewer
                ).refine(
                    previous=prepared,
                    profile=profile,
                    observed_facts=observed_facts,
                )
                if goal_refinement.revised_outcome is not None:
                    prepared = goal_refinement.revised_outcome
        goal = prepared.goal_contract
        preparation_calls = len(runner.receipts)
        if service is not None and manager is not None:
            if goal_refinement is not None and goal_refinement.revised_outcome is not None:
                # feedback의 rev2보다 원본 거절 rev1을 먼저 보존한다. 사용량은
                # 아래에서 최종 revision에 한 번만 연결한다.
                service.register_goal(
                    goal_refinement.original_outcome.goal_contract, activate=False
                )
            register_and_attach_goal(service, manager, goal)
            _bind_planning_runner_goal(runner, goal)
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
            "logical_role_calls": preparation_calls,
            "candidate_versions": 0,
            "finding_codes": list(goal.preparation_binding.finding_codes),
            "passed": False,
            "failure": None,
            "goal_preparation": prepared.model_dump(mode="json"),
            "goal_refinement": (
                None if goal_refinement is None else goal_refinement.model_dump(mode="json")
            ),
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
                allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.executor.allowed_fallbacks),
            ),
            validator=RoleAssignmentPolicy(
                role="validator",
                preferred_model=roles.validator.model,
                preferred_effort=roles.validator.effort,
                allowed_fallbacks=tuple(ModelFallback(model=x.model, effort=x.effort) for x in roles.validator.allowed_fallbacks),
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
            allowed_fallbacks=roles.skeleton_generator.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
        )
        skeleton_reviewer = SkeletonReviewerAdapter(
            runner,
            model=roles.general_reviewer.model,
            effort=roles.general_reviewer.effort,
            allowed_fallbacks=roles.general_reviewer.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
        )
        expander = PlanExpanderAdapter(
            runner,
            assigner,
            model=roles.plan_expander.model,
            effort=roles.plan_expander.effort,
            allowed_fallbacks=roles.plan_expander.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
            inspection_provider_contract=inspection_provider_contract,
        )
        plan_reviewer = PlanReviewerAdapter(
            runner,
            model=roles.general_reviewer.model,
            effort=roles.general_reviewer.effort,
            allowed_fallbacks=roles.general_reviewer.allowed_fallbacks,
            inventory_digest=inventory.inventory_digest, inventory=inventory,
            cwd=workspace,
            critical_model=roles.critical_reviewer.model,
            critical_effort=roles.critical_reviewer.effort,
            critical_allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks,
            inspection_provider_contract=inspection_provider_contract,
        )
        if retain_execution_checkpoint:
            from .benchmark_candidate_costs import CandidateOutputRecorder
            expander = CandidateOutputRecorder(expander)
        timeout_context = (
            nullcontext()
            if evaluation_policies is None
            else use_role_timeout_policy(evaluation_policies.role_timeouts)
        )
        with timeout_context:
            outcome = SkeletonFirstPlanner(
                generator, skeleton_reviewer, expander, plan_reviewer
            ).search(
                goal=goal,
                state=state,
                project_map=project_map,
                budget=PlanningBudgetPolicy(max_logical_role_calls=14 - preparation_calls),
                candidate_count=scenario.candidate_count,
                feasible_observer=observe_first_feasible,
                recovery_policy=(PlanningRecoveryPolicy()
                                 if inspection_provider_contract == PLAN_INSPECTION_PROVIDER_V2 else None),
            )
        selected = outcome.selected_activation_digest is not None
        if retain_execution_checkpoint:
            assert service is not None
            service.record_project_map(project_map)
            service.record_state_snapshot(state)
            for evaluation in outcome.skeleton_evaluations:
                service.record_skeleton_evaluation(evaluation)
            for evaluation in outcome.plan_evaluations:
                service.register_plan_evaluation(evaluation)
            service.record_planning_search(outcome)
            cell["candidate_output_bindings"] = [
                item.model_dump(mode="json") for item in expander.bindings
            ]
            selected_plan = next((item.plan for item in outcome.plan_evaluations
                                  if item.plan.activation_digest == outcome.selected_activation_digest), None)
            if selected_plan is not None:
                checkpoint = {
                    "format": "flowmarshal-benchmark-execution-checkpoint-v1",
                    "project_id": project_id,
                    "goal_id": goal.goal_id,
                    "goal_contract_digest": goal.definition_digest,
                    "plan_revision_id": selected_plan.plan_revision_id,
                    "activation_digest": selected_plan.activation_digest,
                    "workspace": str(workspace.resolve()),
                    "database_path": str(service.ledger.path.resolve()),
                    "artifact_root": str(service.ledger.artifact_root.resolve()),
                    "planning_outcome_digest": sha256_digest(outcome),
                }
                cell["execution_checkpoint"] = checkpoint
                _write_json(Path(temp) / "execution-checkpoint.json", checkpoint)
                _write_json(Path(temp) / "selected-plan.json", selected_plan)
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
                "logical_role_calls": preparation_calls + outcome.logical_role_calls,
                "candidate_versions": outcome.candidate_versions,
                "finding_codes": sorted(set(cell["finding_codes"]) | set(findings)),
                "selected_activation_digest": outcome.selected_activation_digest,
                "planning_outcome": outcome.model_dump(mode="json"),
                "latency_ms_to_first_feasible": (
                    first_feasible.latency_ms if first_feasible is not None else None
                ),
                "latency_ms_to_disposition": max(1, int((time.monotonic() - started) * 1000)),
            }
        )
        failures: list[str] = []
        if outcome.candidate_schema_failures:
            cell["schema_valid"] = False
            failures.append(f"후보 schema 실패 {len(outcome.candidate_schema_failures)}회를 보존함")
        if scenario.expected_disposition == "selected" and not selected:
            failures.append("clean scenario에서 실행 가능한 Plan을 선택하지 못함")
        if scenario.expected_disposition == "blocked":
            failures.append("정보 부족 입력에서 필수 정보 요청 없이 ready planning에 진입함")
        if preparation_calls + outcome.logical_role_calls > 14:
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
    inspection_provider_contract: PlanInspectionProviderVersion = PLAN_INSPECTION_PROVIDER_V1,
    evaluation_policies: EvaluationPolicies | None = None,
) -> tuple[Path, ScopeQualificationReport]:
    if evaluation_policies is None:
        raise QualificationRunError("EVALUATION_POLICY_REQUIRED")
    base = (root or project_root()).resolve(strict=True)
    if run_root is not None:
        _guard_full_planning_resume(
            Path(run_root).resolve(), base=base, codex_bin=codex_bin
        )
    preflight_failures = _preflight(base)
    if preflight_failures:
        raise QualificationRunError("; ".join(preflight_failures))
    roles = role_configuration or default_role_configuration(base)
    catalog = PlanningScenarioCatalog.load(
        base / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
    )
    with CodexAppServerRuntime(
        codex_bin=codex_bin, project_binding=evaluation_policies.codex_project
    ) as runtime:
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        contract = _planning_contract(
            base, catalog, inventory, roles,
            evaluation_policies,
            inspection_provider_contract=inspection_provider_contract,
        )
        destination = (
            run_root
            or _default_run_root(base, "full-planning-pipeline", contract.contract_digest[7:15])
        ).resolve()
        cell_work_roots = {
            (seed, scenario.scenario_digest): _planning_cell_work_root(
                destination, order_seed=seed, catalog_index=catalog_index,
            )
            for seed in contract.order_seeds
            for catalog_index, scenario in enumerate(catalog.scenarios)
        }
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        qualification_suite_manifest(base)
        build_qualification_reproduction_bundle(
            root=base,
            destination=destination / "reproduction-bundle",
            inventory=inventory,
            roles=roles,
            contracts=(contract,),
        )
        audit_path = destination / ("inventory-observation-" + inventory.inventory_digest[7:] + ".json")
        if not audit_path.exists():
            _write_json(audit_path, roles.operational_binding(inventory))
        if store.state().status is EvaluationRunStatus.PAUSED_RATE_LIMIT:
            store.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())
        write_immutable_run_metadata(
            destination / "run-metadata.json",
            {
                "scope": "full-planning-pipeline",
                "evaluation_contract_digest": contract.contract_digest,
                "project_root": str(base),
                "role_configuration": roles.model_dump(mode="json"),
                "codex_bin": None if codex_bin is None else str(Path(codex_bin).resolve()),
                "inspection_provider_contract": inspection_provider_contract,
            },
            evaluation_policies,
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
                        partial_feasible = PlanningPartialFeasibleObservation(observed=False)

                        def collect_partial_feasible(
                            observation: PlanningPartialFeasibleObservation,
                        ) -> None:
                            nonlocal partial_feasible
                            if not partial_feasible.observed:
                                partial_feasible = observation

                        cell, receipts = _planning_cell(
                            scenario=scenario,
                            seed=seed,
                            fixture_root=fixture_root,
                            runtime=runtime,
                            inventory=inventory,
                            roles=roles,
                            inspection_provider_contract=inspection_provider_contract,
                            work_root=cell_work_roots[(seed, scenario.scenario_digest)],
                            progress_sink=_role_progress(destination, scenario_id=scenario.scenario_id, order_seed=seed),
                            evaluation_policies=evaluation_policies,
                            partial_feasible_observer=collect_partial_feasible,
                        )
                    except StructuredRoleError as error:
                        _write_json(
                            destination / "last-error.json",
                            {
                                "error": type(error).__name__,
                                "message": str(error),
                                "failure_class": classify_qualification_failure(error).value,
                                "receipts": [
                                    item.model_dump(mode="json") for item in error.receipts
                                ],
                                "scenario_id": scenario.scenario_id,
                                "order_seed": seed,
                            },
                        )
                        if _is_rate_limit(error):
                            _record_full_planning_rate_limit(
                                store,
                                state_root=(
                                    cell_work_roots[(seed, scenario.scenario_digest)]
                                    / "budget-state"
                                ),
                                scenario_id=scenario.scenario_id,
                                order_seed=seed,
                                original_reason=str(error),
                                codex_bin=codex_bin,
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
                            "selected": False,
                            "failure": str(error),
                            "partial_feasible_observation": partial_feasible.model_dump(mode="json"),
                        }
                    store.put(
                        EvaluationCellCheckpoint(
                            model_lock_format="flowmarshal-model-lock-v2",
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
    if value.get("scope") != "deterministic":
        verify_metadata_digest(value)
        contract_path = path.parent / "evaluation-contract.json"
        if not contract_path.is_file():
            raise QualificationRunError("resume할 evaluation-contract.json이 없습니다.")
        contract = EvaluationContract.model_validate_json(
            contract_path.read_text(encoding="utf-8")
        )
        if value.get("evaluation_contract_digest") != contract.contract_digest:
            raise QualificationRunError("run metadata와 evaluation contract digest가 다릅니다.")
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
            if metadata.get("scope") != "deterministic":
                policies_from_metadata(metadata)
            if metadata.get("scope") == "project-e2e":
                try:
                    verify_candidate_wheel_metadata(metadata)
                except QualificationManifestError as error:
                    raise QualificationRunError(str(error)) from error
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
    policies = policies_from_metadata(metadata)
    codex_bin = metadata.get("codex_bin")
    if scope == "role-fixture":
        return run_role_fixture(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
            evaluation_policies=policies,
        )
    if scope == "full-planning-pipeline":
        _guard_full_planning_resume(
            destination, base=base, codex_bin=codex_bin
        )
        return run_full_planning_pipeline(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
            inspection_provider_contract=metadata.get(
                "inspection_provider_contract", PLAN_INSPECTION_PROVIDER_V1
            ),
            evaluation_policies=policies,
        )
    if scope == "project-e2e":
        from .e2e_qualification import run_project_e2e

        try:
            candidate_binding = verify_candidate_wheel_metadata(metadata)
        except QualificationManifestError as error:
            raise QualificationRunError(str(error)) from error

        return run_project_e2e(
            root=base,
            run_root=destination,
            role_configuration=roles,
            codex_bin=codex_bin,
            evaluation_policies=policies,
            candidate_wheel=candidate_binding.wheel_path,
        )
    raise QualificationRunError(f"지원하지 않는 resume scope입니다: {scope}")
