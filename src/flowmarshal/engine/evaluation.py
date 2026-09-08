from __future__ import annotations

import json
import statistics
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from ..canonical import sha256_digest
from .domain import CandidateDecision, CandidateStatus, EngineModel, ReviewerSubmission


class EvaluationScope(StrEnum):
    DETERMINISTIC = "deterministic_schema_dag_ledger"
    ROLE_FIXTURE = "goal_reviewer_role_fixture"
    FULL_PLANNING_PIPELINE = "full_skeleton_to_selection_pipeline"
    PROJECT_E2E = "activation_execution_validation_restart_e2e"


class EvaluationContract(EngineModel):
    model_lock_format: Literal["flowmarshal-model-lock-v2"]
    scope: EvaluationScope
    fixture_digests: tuple[str, ...] = Field(min_length=1)
    scenario_set_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    order_seeds: tuple[int, ...] = Field(min_length=1)
    expected_cell_count: int = Field(ge=1)
    role_configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_manifest_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    rules_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    threshold_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    taxonomy_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("fixture_digests")
    @classmethod
    def fixtures_are_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            len(item) != 71
            or not item.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in item[7:])
            for item in value
        ):
            raise ValueError("evaluation fixture ref는 sha256 digest여야 합니다.")
        return value

    @model_validator(mode="after")
    def fixture_digests_are_unique(self) -> "EvaluationContract":
        if len(self.fixture_digests) != len(set(self.fixture_digests)):
            raise ValueError("evaluation fixture digest가 중복됐습니다.")
        if len(self.order_seeds) != len(set(self.order_seeds)):
            raise ValueError("evaluation order seed가 중복됐습니다.")
        if any(seed < 0 for seed in self.order_seeds):
            raise ValueError("evaluation order seed는 0 이상이어야 합니다.")
        if self.expected_cell_count != len(self.fixture_digests) * len(self.order_seeds):
            raise ValueError("expected cell 수가 fixture × order seed와 다릅니다.")
        return self

    @property
    def contract_digest(self) -> str:
        return sha256_digest(self)


class EvaluationCellCheckpoint(EngineModel):
    model_lock_format: Literal["flowmarshal-model-lock-v2"]
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    order_seed: int = Field(ge=0)
    raw_structured_assessment: dict[str, Any]
    runner_receipts: tuple[dict[str, Any], ...] = Field(min_length=1)
    completed: bool = True

    @property
    def checkpoint_digest(self) -> str:
        return sha256_digest(self)


class EvaluationRunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED_RATE_LIMIT = "PAUSED_RATE_LIMIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvaluationRunState(EngineModel):
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: EvaluationRunStatus
    completed_cell_count: int = Field(ge=0)
    expected_cell_count: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=5000)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluation run timestamp에는 timezone이 필요합니다.")
        return value


class CheckpointContractError(RuntimeError):
    pass


class ImmutableCheckpointStore:
    """(fixture, order_seed) cell을 evaluation 계약에 불변 결속한다."""

    def __init__(self, root: Path | str, contract: EvaluationContract) -> None:
        self.root = Path(root)
        self.contract = contract

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = self.root / "evaluation-contract.json"
        payload = json.dumps(
            self.contract.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        if manifest.exists():
            if json.loads(manifest.read_text(encoding="utf-8")).get("model_lock_format") != self.contract.model_lock_format:
                raise CheckpointContractError("MODEL_LOCK_VERSION_UNSUPPORTED: v1 checkpoint를 v2로 재사용할 수 없습니다.")
            existing = EvaluationContract.model_validate_json(manifest.read_text(encoding="utf-8"))
            if existing.contract_digest != self.contract.contract_digest:
                raise CheckpointContractError("기존 checkpoint의 evaluation 계약이 다릅니다.")
            self._ensure_state()
            return
        with manifest.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        self._ensure_state()

    def _ensure_state(self) -> None:
        path = self.root / "run-state.json"
        if path.exists():
            state = EvaluationRunState.model_validate_json(path.read_text(encoding="utf-8"))
            if state.contract_digest != self.contract.contract_digest:
                raise CheckpointContractError("run state의 evaluation 계약이 다릅니다.")
            return
        from .domain import utc_now

        self.set_state(EvaluationRunStatus.RUNNING, updated_at=utc_now())

    def _completed_count(self) -> int:
        cells = self.root / "cells"
        return 0 if not cells.is_dir() else sum(1 for _ in cells.rglob("*.json"))

    def set_state(
        self,
        status: EvaluationRunStatus,
        *,
        updated_at: datetime,
        reason: str | None = None,
    ) -> EvaluationRunState:
        completed = self._completed_count()
        if status is EvaluationRunStatus.COMPLETED and completed != self.contract.expected_cell_count:
            raise CheckpointContractError("모든 예상 cell이 없으면 run을 COMPLETED로 표시할 수 없습니다.")
        state = EvaluationRunState(
            contract_digest=self.contract.contract_digest,
            status=status,
            completed_cell_count=completed,
            expected_cell_count=self.contract.expected_cell_count,
            reason=reason,
            updated_at=updated_at,
        )
        path = self.root / "run-state.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return state

    def state(self) -> EvaluationRunState:
        path = self.root / "run-state.json"
        if not path.is_file():
            raise CheckpointContractError("run state가 없습니다.")
        state = EvaluationRunState.model_validate_json(path.read_text(encoding="utf-8"))
        if state.contract_digest != self.contract.contract_digest:
            raise CheckpointContractError("run state의 evaluation 계약이 다릅니다.")
        return state

    def _path(self, fixture_digest: str, order_seed: int) -> Path:
        fixture_key = fixture_digest.split(":", 1)[-1][:24]
        return self.root / "cells" / f"seed-{order_seed}" / f"{fixture_key}.json"

    def put(self, checkpoint: EvaluationCellCheckpoint) -> Path:
        if checkpoint.contract_digest != self.contract.contract_digest:
            raise CheckpointContractError("cell이 현재 evaluation 계약과 다릅니다.")
        if checkpoint.fixture_digest not in self.contract.fixture_digests:
            raise CheckpointContractError("cell fixture가 evaluation manifest에 없습니다.")
        if checkpoint.order_seed not in self.contract.order_seeds:
            raise CheckpointContractError("cell order seed가 evaluation 계약에 없습니다.")
        if not checkpoint.completed:
            raise CheckpointContractError("미완료 cell은 checkpoint로 저장하지 않습니다.")
        path = self._path(checkpoint.fixture_digest, checkpoint.order_seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            checkpoint.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        if path.exists():
            existing = EvaluationCellCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.checkpoint_digest != checkpoint.checkpoint_digest:
                raise CheckpointContractError("완료 checkpoint를 다른 결과로 덮어쓸 수 없습니다.")
            return path
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
        return path

    def completed(self, fixture_digest: str, order_seed: int) -> EvaluationCellCheckpoint | None:
        path = self._path(fixture_digest, order_seed)
        if not path.is_file():
            return None
        checkpoint = EvaluationCellCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        if checkpoint.contract_digest != self.contract.contract_digest:
            raise CheckpointContractError("resume 대상 cell의 evaluation 계약이 다릅니다.")
        return checkpoint


class RegressionFixture(EngineModel):
    fixture_id: str = Field(pattern=r"^[A-Z][0-9]{2}-(?:clean|adversarial|mutant)$")
    opaque_case_ref: str = Field(pattern=r"^case-[0-9a-f]{16}$")
    suite: str = Field(min_length=1, max_length=100)
    artifact: dict[str, Any]
    expected_admissible: bool
    critical: bool = False
    required_finding_codes: tuple[str, ...] = ()
    allowed_correlated_codes: tuple[str, ...] = ()
    known_r31_failure: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def oracle_sets_do_not_overlap(self) -> "RegressionFixture":
        if set(self.required_finding_codes) & set(self.allowed_correlated_codes):
            raise ValueError("required와 allowed correlated finding이 겹칩니다.")
        if self.expected_admissible and self.required_finding_codes:
            raise ValueError("clean/admissible fixture에는 필수 결함 코드를 둘 수 없습니다.")
        return self

    @property
    def fixture_digest(self) -> str:
        return sha256_digest(self)

    def model_input(self) -> dict[str, Any]:
        """내부 case ID·variant·oracle을 제외한 모델 입력."""

        return {
            "case_ref": self.opaque_case_ref,
            "suite": self.suite,
            "input_kind": "semantic_excerpt_not_execution_spec",
            "artifact": self.artifact,
        }


class RegressionCatalog(EngineModel):
    schema_version: Literal["1.0"] = "1.0"
    source_baseline: str
    fixtures: tuple[RegressionFixture, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def fixtures_are_unique(self) -> "RegressionCatalog":
        fixture_ids = tuple(item.fixture_id for item in self.fixtures)
        opaque_refs = tuple(item.opaque_case_ref for item in self.fixtures)
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("fixture ID가 중복됐습니다.")
        if len(opaque_refs) != len(set(opaque_refs)):
            raise ValueError("opaque case ref가 중복됐습니다.")
        return self

    @property
    def catalog_digest(self) -> str:
        return sha256_digest(self)

    @classmethod
    def load(cls, path: Path | str) -> "RegressionCatalog":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def model_batch(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.model_input() for item in self.fixtures)


class FixtureResult(EngineModel):
    opaque_case_ref: str
    order_seed: int = Field(default=0, ge=0)
    submission: ReviewerSubmission | None = None
    decision: CandidateDecision | None = None
    schema_valid: bool = True


class EvaluationMetrics(EngineModel):
    fixture_count: int = Field(ge=0)
    cell_count: int = Field(default=0, ge=0)
    required_finding_recall: float = Field(ge=0, le=1)
    finding_precision: float = Field(ge=0, le=1)
    critical_false_admission_count: int = Field(ge=0)
    clean_false_block_count: int = Field(ge=0)
    schema_failure_count: int = Field(ge=0)
    critical_verdict_inconsistency_count: int = Field(default=0, ge=0)


class QualificationReport(EngineModel):
    scope: EvaluationScope
    catalog_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    prompt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    metrics: EvaluationMetrics
    failures: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    passed: bool


def evaluate_role_fixtures(
    *,
    catalog: RegressionCatalog,
    results: tuple[FixtureResult, ...],
    prompt_digest: str,
    output_schema_digest: str,
    model_lock_digest: str,
    recall_threshold: float = 0.90,
    precision_threshold: float = 0.85,
) -> QualificationReport:
    by_ref: dict[str, list[FixtureResult]] = {}
    seen: set[tuple[str, int]] = set()
    for item in results:
        key = (item.opaque_case_ref, item.order_seed)
        if key in seen:
            raise ValueError(f"fixture result가 중복됐습니다: {key}")
        seen.add(key)
        by_ref.setdefault(item.opaque_case_ref, []).append(item)
    required_total = 0
    required_detected = 0
    detected_total = 0
    accepted_detected = 0
    critical_false_admissions = 0
    clean_false_blocks = 0
    schema_failures = 0
    failures: list[str] = []
    diagnostics: list[str] = []
    for fixture in catalog.fixtures:
        fixture_results = by_ref.get(fixture.opaque_case_ref, [])
        if not fixture_results:
            schema_failures += 1
            failures.append(f"{fixture.fixture_id}: structured assessment 없음")
            continue
        for result in fixture_results:
            if not result.schema_valid or result.submission is None or result.decision is None:
                schema_failures += 1
                failures.append(
                    f"{fixture.fixture_id}/seed-{result.order_seed}: structured assessment 없음"
                )
                continue
            detected = {item.finding_code for item in result.submission.findings}
            required = set(fixture.required_finding_codes)
            allowed = required | set(fixture.allowed_correlated_codes)
            required_total += len(required)
            required_detected += len(required & detected)
            detected_total += len(detected)
            accepted_detected += len(detected & allowed)
            admitted = result.decision.status in {
                CandidateStatus.ADMISSIBLE,
                CandidateStatus.SELECTED,
            }
            if fixture.critical and not fixture.expected_admissible and admitted:
                critical_false_admissions += 1
                failures.append(
                    f"{fixture.fixture_id}/seed-{result.order_seed}: critical false admission"
                )
            if fixture.expected_admissible and not admitted:
                clean_false_blocks += 1
                failures.append(
                    f"{fixture.fixture_id}/seed-{result.order_seed}: clean false block"
                )
            if required - detected:
                diagnostics.append(
                    f"{fixture.fixture_id}/seed-{result.order_seed}: 필수 finding 누락 "
                    f"{sorted(required-detected)}"
                )
    inconsistencies = 0
    for fixture in catalog.fixtures:
        if not fixture.critical:
            continue
        verdicts = {
            item.decision.status in {CandidateStatus.ADMISSIBLE, CandidateStatus.SELECTED}
            for item in by_ref.get(fixture.opaque_case_ref, [])
            if item.schema_valid and item.decision is not None
        }
        if len(verdicts) > 1:
            inconsistencies += 1
            failures.append(f"{fixture.fixture_id}: seed 간 critical verdict 불일치")
    recall = 1.0 if required_total == 0 else required_detected / required_total
    precision = 1.0 if detected_total == 0 else accepted_detected / detected_total
    if recall < recall_threshold:
        failures.append(f"주요 의미 결함 recall {recall:.3f} < {recall_threshold:.3f}")
    if precision < precision_threshold:
        failures.append(f"finding precision {precision:.3f} < {precision_threshold:.3f}")
    if critical_false_admissions:
        failures.append("critical false admission이 0건이 아닙니다.")
    if clean_false_blocks:
        failures.append("명확한 clean 요청을 불필요하게 차단했습니다.")
    if schema_failures:
        failures.append("schema 복구 실패가 남았습니다.")
    if inconsistencies:
        failures.append("seed 간 critical verdict 불일치가 0건이 아닙니다.")
    metrics = EvaluationMetrics(
        fixture_count=len(catalog.fixtures),
        cell_count=len(results),
        required_finding_recall=recall,
        finding_precision=precision,
        critical_false_admission_count=critical_false_admissions,
        clean_false_block_count=clean_false_blocks,
        schema_failure_count=schema_failures,
        critical_verdict_inconsistency_count=inconsistencies,
    )
    return QualificationReport(
        scope=EvaluationScope.ROLE_FIXTURE,
        catalog_digest=catalog.catalog_digest,
        prompt_digest=prompt_digest,
        output_schema_digest=output_schema_digest,
        model_lock_digest=model_lock_digest,
        metrics=metrics,
        failures=tuple(failures),
        diagnostics=tuple(diagnostics),
        passed=not failures,
    )


_BENCHMARK_DIGEST = r"^sha256:[0-9a-f]{64}$"
_BENCHMARK_ENTITY_ID = r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$"
BenchmarkDigest = Annotated[str, Field(pattern=_BENCHMARK_DIGEST)]


class BenchmarkTaskLifecycleObservation(EngineModel):
    task_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    execution_spec_revision_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    execution_spec_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    attempt_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    runtime_receipt_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    validation_result_digests: tuple[BenchmarkDigest, ...] = Field(min_length=1)
    attempt_status: Literal["succeeded"] = "succeeded"

    @model_validator(mode="after")
    def validation_results_are_unique(self) -> "BenchmarkTaskLifecycleObservation":
        if len(self.validation_result_digests) != len(set(self.validation_result_digests)):
            raise ValueError("lifecycle Task validation result digest가 중복됐습니다.")
        return self


class BenchmarkMaterializedExecutionSpecObservation(EngineModel):
    """수명주기 v2에서 원장으로 재계산한 모든 materialized Worker 명세."""

    plan_revision_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    task_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    execution_spec_revision_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    execution_spec_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    materialization_event_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    execution_state: Literal["executed", "unexecuted", "unknown_effect"]
    provenance: Literal[
        "worker_turn_receipt", "validator_rebind_worker_turn", "no_effect", "unknown_effect"
    ]
    worker_attempt_ids: tuple[str, ...] = ()
    runtime_receipt_digests: tuple[BenchmarkDigest, ...] = ()
    reused_worker_execution_spec_digest: str | None = Field(default=None, pattern=_BENCHMARK_DIGEST)

    @model_validator(mode="after")
    def execution_provenance_is_complete(self) -> "BenchmarkMaterializedExecutionSpecObservation":
        if len(self.worker_attempt_ids) != len(set(self.worker_attempt_ids)):
            raise ValueError("materialized ExecutionSpec Worker Attempt가 중복됐습니다.")
        if len(self.runtime_receipt_digests) != len(set(self.runtime_receipt_digests)):
            raise ValueError("materialized ExecutionSpec runtime receipt가 중복됐습니다.")
        if self.execution_state == "executed":
            if self.provenance not in {"worker_turn_receipt", "validator_rebind_worker_turn"}:
                raise ValueError("실행된 ExecutionSpec에는 Worker turn provenance가 필요합니다.")
            if not self.worker_attempt_ids or not self.runtime_receipt_digests:
                raise ValueError("실행된 ExecutionSpec에는 Worker Attempt와 runtime receipt가 필요합니다.")
            if self.provenance == "validator_rebind_worker_turn" and self.reused_worker_execution_spec_digest is None:
                raise ValueError("validator-only 재결속에는 이전 Worker Spec digest가 필요합니다.")
        elif self.execution_state == "unexecuted":
            if self.provenance != "no_effect" or self.worker_attempt_ids or self.runtime_receipt_digests:
                raise ValueError("미실행 ExecutionSpec은 no-effect 증명만 기록합니다.")
        elif self.provenance != "unknown_effect":
            raise ValueError("외부 효과 미확인 ExecutionSpec은 unknown-effect provenance가 필요합니다.")
        return self


class BenchmarkLifecycleObservation(EngineModel):
    # v1 원시 관측은 checkpoint 재해석 없이 그대로 읽는다. v2만 모든 명세를 집계한다.
    schema_version: Literal["1.0", "2.0"] = "1.0"
    collector: Literal[
        "flowmarshal.engine.lifecycle-ledger-v1", "flowmarshal.engine.lifecycle-ledger-v2"
    ] = "flowmarshal.engine.lifecycle-ledger-v1"
    project_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    plan_revision_id: str = Field(pattern=_BENCHMARK_ENTITY_ID)
    plan_activation_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    model_lock_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    neutral_input_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    tasks: tuple[BenchmarkTaskLifecycleObservation, ...] = Field(min_length=1)
    materialized_execution_specs: tuple[BenchmarkMaterializedExecutionSpecObservation, ...] = ()
    integration_validation_result_digests: tuple[BenchmarkDigest, ...] = Field(min_length=1)
    state_before_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    state_after_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    state_reobservation_event_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    goal_verdict_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    history_head_digest: str = Field(pattern=_BENCHMARK_DIGEST)
    run_state: Literal["completed"] = "completed"
    history_valid: Literal[True] = True

    @model_validator(mode="after")
    def lifecycle_is_complete_and_unique(self) -> "BenchmarkLifecycleObservation":
        for label, values in (
            ("Task", tuple(item.task_id for item in self.tasks)),
            ("ExecutionSpec", tuple(item.execution_spec_revision_id for item in self.tasks)),
            ("Attempt", tuple(item.attempt_id for item in self.tasks)),
            ("integration validation", self.integration_validation_result_digests),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"lifecycle {label} evidence가 중복됐습니다.")
        if self.state_before_digest == self.state_after_digest:
            raise ValueError("lifecycle State 재관측은 실행 전과 다른 snapshot revision이어야 합니다.")
        if self.schema_version == "1.0":
            if self.collector != "flowmarshal.engine.lifecycle-ledger-v1" or self.materialized_execution_specs:
                raise ValueError("lifecycle v1 원시는 v1 collector와 빈 materialized 목록만 허용합니다.")
        else:
            if self.collector != "flowmarshal.engine.lifecycle-ledger-v2" or not self.materialized_execution_specs:
                raise ValueError("lifecycle v2에는 v2 collector와 materialized ExecutionSpec 목록이 필요합니다.")
            ids = tuple(item.execution_spec_revision_id for item in self.materialized_execution_specs)
            if len(ids) != len(set(ids)):
                raise ValueError("lifecycle v2 materialized ExecutionSpec이 중복됐습니다.")
            by_spec = {item.execution_spec_revision_id: item for item in self.materialized_execution_specs}
            for task in self.tasks:
                materialized = by_spec.get(task.execution_spec_revision_id)
                if (
                    materialized is None
                    or materialized.task_id != task.task_id
                    or materialized.execution_spec_digest != task.execution_spec_digest
                    or materialized.execution_state != "executed"
                    or task.attempt_id not in materialized.worker_attempt_ids
                ):
                    raise ValueError(
                        "lifecycle v2 Task 성공 Worker Spec/Attempt는 materialized 목록에 그대로 있어야 합니다."
                    )
        return self

    @property
    def detailed_execution_spec_count(self) -> int:
        return len(self.materialized_execution_specs) if self.schema_version == "2.0" else len(self.tasks)

    @property
    def has_unknown_execution_effect(self) -> bool:
        return any(item.execution_state == "unknown_effect" for item in self.materialized_execution_specs)

    @property
    def unexecuted_execution_spec_count(self) -> int | None:
        if self.schema_version == "1.0":
            return 0
        if self.has_unknown_execution_effect:
            return None
        return sum(item.execution_state == "unexecuted" for item in self.materialized_execution_specs)

    @property
    def observation_digest(self) -> str:
        return sha256_digest(self)


class BenchmarkCell(EngineModel):
    schema_version: Literal["3.0"] = "3.0"
    scenario_id: str
    scenario_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    neutral_input_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    order_seed: int = Field(ge=0)
    path_kind: Literal["multi_path", "single_path"]
    implementation: Literal["r31_baseline", "skeleton_engine"]
    model_lock_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    functional_result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_receipt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_disposition: Literal["selected", "blocked"]
    disposition: Literal["selected", "blocked", "failed"]
    uncached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms_to_first_feasible: int | None = Field(gt=0)
    latency_ms_to_disposition: int = Field(gt=0)
    selected_plan_activation_digest: str | None = Field(
        default=None, pattern=_BENCHMARK_DIGEST
    )
    lifecycle_observation: BenchmarkLifecycleObservation | None = None
    lifecycle_evidence_digest: str | None = Field(
        default=None, pattern=_BENCHMARK_DIGEST
    )
    detailed_task_count: int | None = Field(default=None, ge=0)
    unexecuted_detailed_task_count: int | None = Field(default=None, ge=0)
    candidate_output_tokens: int = Field(ge=0)
    discarded_candidate_output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_are_consistent(self) -> "BenchmarkCell":
        if self.disposition == "selected":
            if self.latency_ms_to_first_feasible is None:
                raise ValueError("선택된 Plan에는 최초 feasible plan 시간이 필요합니다.")
            if self.latency_ms_to_first_feasible > self.latency_ms_to_disposition:
                raise ValueError("최초 feasible plan 시간이 최종 판정 시간보다 늦습니다.")
        elif self.latency_ms_to_first_feasible is not None:
            raise ValueError("Plan이 없는 질문·차단·실패에는 최초 feasible plan 시간이 없습니다.")
        detail_values = (self.detailed_task_count, self.unexecuted_detailed_task_count)
        if self.implementation == "skeleton_engine":
            selected = self.disposition == "selected"
            if selected != (self.selected_plan_activation_digest is not None):
                raise ValueError("Engine 선택 결과와 Plan activation digest가 함께 기록돼야 합니다.")
            observed = self.lifecycle_observation is not None
            if observed != (self.lifecycle_evidence_digest is not None):
                raise ValueError("lifecycle evidence 본문과 digest가 함께 기록돼야 합니다.")
            if observed:
                lifecycle = self.lifecycle_observation
                if lifecycle is None:
                    raise ValueError("lifecycle evidence 본문이 없습니다.")
                if lifecycle.schema_version == "2.0":
                    expected_unexecuted = lifecycle.unexecuted_execution_spec_count
                    if self.detailed_task_count is None or self.detailed_task_count != lifecycle.detailed_execution_spec_count:
                        raise ValueError("Engine detail 수치는 lifecycle materialized 목록에서 다시 계산돼야 합니다.")
                    if self.unexecuted_detailed_task_count != expected_unexecuted:
                        raise ValueError("미실행 detail 수치는 lifecycle effect 관측과 다릅니다.")
                elif not all(value is not None for value in detail_values):
                    raise ValueError("Engine detail 수치는 완결된 lifecycle evidence와 함께 기록해야 합니다.")
            elif any(value is not None for value in detail_values):
                raise ValueError("Engine detail 수치는 완결된 lifecycle evidence와 함께 기록해야 합니다.")
            if observed and self.disposition != "selected":
                raise ValueError("선택되지 않은 Engine cell에는 실행 lifecycle evidence를 둘 수 없습니다.")
            if observed and self.detailed_task_count == 0:
                raise ValueError("lifecycle evidence에는 하나 이상의 materialized Task가 필요합니다.")
            if observed:
                lifecycle = self.lifecycle_observation
                if lifecycle is None:
                    raise ValueError("lifecycle evidence 본문이 없습니다.")
                if self.lifecycle_evidence_digest != lifecycle.observation_digest:
                    raise ValueError("lifecycle evidence digest가 본문과 다릅니다.")
                if (
                    lifecycle.plan_activation_digest != self.selected_plan_activation_digest
                    or lifecycle.model_lock_digest != self.model_lock_digest
                    or lifecycle.neutral_input_digest != self.neutral_input_digest
                ):
                    raise ValueError("lifecycle evidence가 Plan/model lock/neutral input과 다릅니다.")
        if (
            self.detailed_task_count is not None
            and self.unexecuted_detailed_task_count is not None
            and self.unexecuted_detailed_task_count > self.detailed_task_count
        ):
            raise ValueError("미실행 상세 Task 수가 전체 상세 Task 수보다 큽니다.")
        if self.discarded_candidate_output_tokens > self.candidate_output_tokens:
            raise ValueError("폐기 후보 token이 전체 후보 출력 token보다 큽니다.")
        return self

    @property
    def optimization_tokens(self) -> int:
        return self.uncached_input_tokens + self.output_tokens


class TokenLatencyGateReport(EngineModel):
    schema_version: Literal["3.0"] = "3.0"
    matched_scenario_count: int
    feasible_plan_pair_count: int
    blocked_pair_count: int
    blocked_latency_ms_baseline_median: float | None
    blocked_latency_ms_engine_median: float | None
    multi_path_median_reduction: float
    overall_mean_reduction: float
    worst_single_path_regression: float
    lifecycle_observed_engine_cell_count: int = Field(ge=0)
    lifecycle_required_engine_cell_count: int = Field(ge=0)
    unexecuted_detail_ratio: float | None
    discarded_candidate_output_ratio: float | None
    time_to_first_feasible_median_improvement: float
    functional_gate_passed: bool
    passed: bool
    failures: tuple[str, ...]


class ScopeGateResult(EngineModel):
    scope: EvaluationScope
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    passed: bool
    failures: tuple[str, ...] = ()


class CutoverGateReport(EngineModel):
    scope_results: tuple[ScopeGateResult, ...]
    token_latency_gate: TokenLatencyGateReport
    missing_scopes: tuple[EvaluationScope, ...]
    passed: bool
    failures: tuple[str, ...]


def evaluate_cutover_gate(
    *,
    scope_results: tuple[ScopeGateResult, ...],
    token_latency_gate: TokenLatencyGateReport,
) -> CutoverGateReport:
    by_scope = {item.scope: item for item in scope_results}
    if len(by_scope) != len(scope_results):
        raise ValueError("cutover scope 결과가 중복됐습니다.")
    required = tuple(EvaluationScope)
    missing = tuple(scope for scope in required if scope not in by_scope)
    failures: list[str] = []
    for result in scope_results:
        if not result.passed:
            failures.append(f"{result.scope.value}: FAIL")
            failures.extend(f"{result.scope.value}: {item}" for item in result.failures)
    if missing:
        failures.append(f"평가 scope 누락: {[item.value for item in missing]}")
    if not token_latency_gate.passed:
        failures.append("token/latency Gate: FAIL")
        failures.extend(token_latency_gate.failures)
    return CutoverGateReport(
        scope_results=scope_results,
        token_latency_gate=token_latency_gate,
        missing_scopes=missing,
        passed=not failures,
        failures=tuple(failures),
    )


def _reduction(old: float, new: float) -> float:
    return 0.0 if old == 0 else (old - new) / old


def evaluate_token_latency_gate(
    cells: tuple[BenchmarkCell, ...],
    *,
    functional_gate_passed: bool,
) -> TokenLatencyGateReport:
    grouped: dict[tuple[str, int], dict[str, BenchmarkCell]] = {}
    for cell in cells:
        key = (cell.scenario_id, cell.order_seed)
        group = grouped.setdefault(key, {})
        if cell.implementation in group:
            raise ValueError(
                f"benchmark cell이 중복됐습니다: {cell.scenario_id}/seed-{cell.order_seed}/"
                f"{cell.implementation}"
            )
        group[cell.implementation] = cell
    incomplete = sorted(str(key) for key, value in grouped.items() if len(value) != 2)
    if incomplete:
        raise ValueError(f"baseline/engine pair가 불완전합니다: {incomplete}")
    pairs = list(grouped.values())
    if not pairs:
        raise ValueError("동일 입력의 baseline/engine benchmark pair가 없습니다.")
    mismatched = sorted(
        str(key)
        for key, pair in grouped.items()
        if pair["r31_baseline"].path_kind != pair["skeleton_engine"].path_kind
        or pair["r31_baseline"].expected_disposition != pair["skeleton_engine"].expected_disposition
        or pair["r31_baseline"].scenario_digest != pair["skeleton_engine"].scenario_digest
        or pair["r31_baseline"].model_lock_digest != pair["skeleton_engine"].model_lock_digest
        or pair["r31_baseline"].neutral_input_digest != pair["skeleton_engine"].neutral_input_digest
        or pair["r31_baseline"].functional_result_digest
        != pair["skeleton_engine"].functional_result_digest
    )
    if mismatched:
        raise ValueError(
            "같은 scenario의 path_kind/scenario/model/functionality 결속이 다릅니다: "
            f"{mismatched}"
        )
    token_reductions = [
        _reduction(pair["r31_baseline"].optimization_tokens, pair["skeleton_engine"].optimization_tokens)
        for pair in pairs
    ]
    multi = [
        _reduction(pair["r31_baseline"].optimization_tokens, pair["skeleton_engine"].optimization_tokens)
        for pair in pairs
        if pair["r31_baseline"].path_kind == "multi_path"
    ]
    single_regressions = [
        -_reduction(pair["r31_baseline"].optimization_tokens, pair["skeleton_engine"].optimization_tokens)
        for pair in pairs
        if pair["r31_baseline"].path_kind == "single_path"
    ]
    feasible_pairs = [
        pair for pair in pairs
        if all(
            cell.expected_disposition == "selected" and cell.disposition == "selected"
            for cell in pair.values()
        )
    ]
    blocked_pairs = [
        pair for pair in pairs
        if all(
            cell.expected_disposition == "blocked" and cell.disposition == "blocked"
            for cell in pair.values()
        )
    ]
    latency_improvements = [
        _reduction(
            pair["r31_baseline"].latency_ms_to_first_feasible,
            pair["skeleton_engine"].latency_ms_to_first_feasible,
        )
        for pair in feasible_pairs
    ]
    engine_cells = [pair["skeleton_engine"] for pair in pairs]
    lifecycle_required = [
        item for item in engine_cells
        if item.expected_disposition == "selected" and item.disposition == "selected"
    ]
    lifecycle_observed = [
        item for item in lifecycle_required
        if item.lifecycle_evidence_digest is not None
        and item.lifecycle_observation is not None
        and item.lifecycle_observation.schema_version == "2.0"
    ]
    lifecycle_complete = (
        bool(lifecycle_required) and len(lifecycle_observed) == len(lifecycle_required)
    )
    multi_median = statistics.median(multi) if multi else 0.0
    overall_mean = statistics.mean(token_reductions)
    worst_single = max(single_regressions, default=0.0)
    if lifecycle_complete:
        detailed = sum(item.detailed_task_count or 0 for item in lifecycle_observed)
        unknown_effect = any(
            item.lifecycle_observation is not None
            and item.lifecycle_observation.has_unknown_execution_effect
            for item in lifecycle_observed
        )
        unexecuted = sum(item.unexecuted_detailed_task_count or 0 for item in lifecycle_observed)
        candidate_tokens = sum(item.candidate_output_tokens for item in lifecycle_observed)
        discarded_tokens = sum(item.discarded_candidate_output_tokens for item in lifecycle_observed)
        unexecuted_ratio = None if detailed == 0 or unknown_effect else unexecuted / detailed
        discarded_ratio = None if candidate_tokens == 0 else discarded_tokens / candidate_tokens
    else:
        unexecuted_ratio = None
        discarded_ratio = None
    latency_median = statistics.median(latency_improvements) if latency_improvements else 0.0
    failures: list[str] = []
    if any(cell.disposition != cell.expected_disposition for cell in cells):
        failures.append("예상된 Plan 선택 또는 질문·차단 결과를 얻지 못한 benchmark cell이 있습니다.")
    if not feasible_pairs:
        failures.append("정상 시나리오의 최초 feasible plan 시간 비교가 없습니다.")
    if multi_median < 0.30:
        failures.append("multi-path token 중앙값 감소가 30% 미만입니다.")
    if not single_regressions:
        failures.append("single-path 비회귀 benchmark가 없습니다.")
    if overall_mean < 0.20:
        failures.append("전체 benchmark 평균 token 감소가 20% 미만입니다.")
    if worst_single > 0.05:
        failures.append("single-path token 회귀가 5%를 넘습니다.")
    if len(lifecycle_observed) != len(lifecycle_required):
        failures.append(
            "NOT_OBSERVED: selected Engine cell의 ExecutionSpec→runtime 완료→validation→"
            "State 재관측 lifecycle evidence가 없습니다."
        )
    if unexecuted_ratio is None:
        failures.append("NOT_OBSERVED: 상세 Task 실행 비율의 관측 가능한 denominator가 없습니다.")
    elif unexecuted_ratio > 0.10:
        failures.append("상세화 후 실행되지 않은 Task 비율이 10%를 넘습니다.")
    if discarded_ratio is None:
        failures.append("NOT_OBSERVED: 폐기 후보 출력 비율의 관측 가능한 denominator가 없습니다.")
    elif discarded_ratio > 0.25:
        failures.append("폐기 후보 상세 출력 비율이 25%를 넘습니다.")
    if latency_median < 0.20:
        failures.append("Time to First Feasible Plan 개선이 20% 미만입니다.")
    if not functional_gate_passed:
        failures.append("기능·안전 Gate 비회귀가 확인되지 않았습니다.")
    return TokenLatencyGateReport(
        matched_scenario_count=len(pairs),
        feasible_plan_pair_count=len(feasible_pairs),
        blocked_pair_count=len(blocked_pairs),
        blocked_latency_ms_baseline_median=(
            statistics.median(pair["r31_baseline"].latency_ms_to_disposition for pair in blocked_pairs)
            if blocked_pairs else None
        ),
        blocked_latency_ms_engine_median=(
            statistics.median(pair["skeleton_engine"].latency_ms_to_disposition for pair in blocked_pairs)
            if blocked_pairs else None
        ),
        multi_path_median_reduction=multi_median,
        overall_mean_reduction=overall_mean,
        worst_single_path_regression=worst_single,
        lifecycle_observed_engine_cell_count=len(lifecycle_observed),
        lifecycle_required_engine_cell_count=len(lifecycle_required),
        unexecuted_detail_ratio=unexecuted_ratio,
        discarded_candidate_output_ratio=discarded_ratio,
        time_to_first_feasible_median_improvement=latency_median,
        functional_gate_passed=functional_gate_passed,
        passed=not failures,
        failures=tuple(failures),
    )


def write_qualification_report(report: QualificationReport, path: Path | str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# v4 performance qualification은 별도 모듈에 있으며 이 module에서 공개 API를 지연 재노출한다.
# 지연 import는 performance.py를 직접 import할 때의 순환을 피한다. 기존 v3 API는 그대로 둔다.
_PERFORMANCE_EXPORTS = frozenset(
    {
        "PerformanceExpectedManifestIdentity",
        "PerformancePairResult",
        "PerformanceQualificationReport",
        "PerformanceSafetyCounters",
        "PerformanceSafetyObservation",
        "PerformanceScenarioIdentity",
        "PerformanceThresholdPolicy",
        "PerformanceUsageCounters",
        "ReleaseCutoverReport",
        "evaluate_performance_qualification",
        "evaluate_release_cutover_gate",
    }
)


def __getattr__(name: str) -> Any:
    if name in _PERFORMANCE_EXPORTS:
        from . import performance

        return getattr(performance, name)
    raise AttributeError(name)
