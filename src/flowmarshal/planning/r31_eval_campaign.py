from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ..canonical import canonical_json, sha256_digest
from .r31_domain import ModelCallReceipt, ModelCallStatus
from .r31_eval_runner import FixtureAssessmentTrace, LiveEvaluationResult
from .r31_evaluation import (
    EvaluationFixture,
    EvaluationModel,
    EvaluationObservation,
    EvaluationObservationBatch,
    EvaluationScope,
    RepeatedEvaluationReport,
    evaluate_repeated_runs,
    ordered_model_inputs,
)
from .r31_models import ExecutionPolicyEvidence, ResolvedPlanningModel


CAMPAIGN_MANIFEST_SCHEMA = "flowmarshal.planner-r31.role-fixture-campaign-manifest.v3"
CAMPAIGN_MODEL_LOCK_SCHEMA = "flowmarshal.planner-r31.role-fixture-model-lock.v2"
CAMPAIGN_CELL_SCHEMA = "flowmarshal.planner-r31.role-fixture-campaign-cell.v2"
CAMPAIGN_ATTEMPT_SCHEMA = "flowmarshal.planner-r31.role-fixture-campaign-attempt.v2"


class CampaignStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    PAUSED_RATE_LIMIT = "PAUSED_RATE_LIMIT"
    ERROR = "ERROR"


class CampaignManifest(EvaluationModel):
    receipt_schema: Literal[
        "flowmarshal.planner-r31.role-fixture-campaign-manifest.v3"
    ] = CAMPAIGN_MANIFEST_SCHEMA
    configuration_id: str = Field(min_length=1, max_length=200)
    configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_scope: EvaluationScope
    fixture_catalog_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_ids: tuple[str, ...] = Field(min_length=1)
    order_seeds: tuple[int, ...] = Field(min_length=1)
    expected_cell_count: int = Field(gt=0)

    @model_validator(mode="after")
    def identifiers_are_consistent(self) -> "CampaignManifest":
        if len(self.fixture_ids) != len(set(self.fixture_ids)):
            raise ValueError("campaign fixture ID가 중복됐습니다.")
        if len(self.order_seeds) != len(set(self.order_seeds)):
            raise ValueError("campaign order seed가 중복됐습니다.")
        if self.expected_cell_count != len(self.fixture_ids) * len(self.order_seeds):
            raise ValueError("campaign 예상 cell 수가 fixture×seed와 다릅니다.")
        return self


class CampaignModelLock(EvaluationModel):
    receipt_schema: Literal[
        "flowmarshal.planner-r31.role-fixture-model-lock.v2"
    ] = CAMPAIGN_MODEL_LOCK_SCHEMA
    configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolved_models_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolved_models: tuple[ResolvedPlanningModel, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def digest_matches_models(self) -> "CampaignModelLock":
        if self.resolved_models_digest != sha256_digest(self.resolved_models):
            raise ValueError("campaign model lock digest가 실제 역할 해석과 다릅니다.")
        return self


class CampaignCellRecord(EvaluationModel):
    receipt_schema: Literal[
        "flowmarshal.planner-r31.role-fixture-campaign-cell.v2"
    ] = CAMPAIGN_CELL_SCHEMA
    configuration_id: str = Field(min_length=1, max_length=200)
    configuration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluation_contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolved_models_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    fixture_id: str = Field(min_length=1, max_length=200)
    fixture_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    model_case_ref: str = Field(pattern=r"^case-[0-9a-f]{16}$")
    model_input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    order_seed: int
    observation: EvaluationObservation
    assessment_trace: FixtureAssessmentTrace
    model_call_receipts: tuple[ModelCallReceipt, ...] = Field(min_length=1)
    execution_policy_evidence: tuple[ExecutionPolicyEvidence, ...]

    @model_validator(mode="after")
    def bindings_are_consistent(self) -> "CampaignCellRecord":
        if self.observation.case_id != self.fixture_id:
            raise ValueError("campaign cell observation과 fixture ID가 다릅니다.")
        if (
            self.assessment_trace.case_id != self.fixture_id
            or self.assessment_trace.model_case_ref != self.model_case_ref
            or self.assessment_trace.model_input_digest != self.model_input_digest
        ):
            raise ValueError("campaign cell assessment trace binding이 다릅니다.")
        receipt_threads = [
            item.thread_id
            for item in self.model_call_receipts
            if item.thread_id is not None
        ]
        evidence_threads = [
            item.thread_id
            for item in self.execution_policy_evidence
            if item.thread_id is not None
        ]
        if (
            len(receipt_threads) != len(set(receipt_threads))
            or len(evidence_threads) != len(set(evidence_threads))
            or set(receipt_threads) != set(evidence_threads)
        ):
            raise ValueError("campaign cell의 model call과 권한 증거가 일대일이 아닙니다.")
        return self


@dataclass(frozen=True)
class LiveEvaluationCampaignResult:
    status: CampaignStatus
    manifest: CampaignManifest
    manifest_digest: str
    completed_cell_count: int
    total_cell_count: int
    newly_completed_cell_count: int
    remaining_cell_count: int
    report: RepeatedEvaluationReport | None
    batches: tuple[EvaluationObservationBatch, ...]
    model_call_receipts: tuple[ModelCallReceipt, ...]
    execution_policy_evidence: tuple[ExecutionPolicyEvidence, ...]
    resolved_models: tuple[ResolvedPlanningModel, ...]
    pause_reason: str | None = None
    attempt_receipt_path: str | None = None


CellExecutor = Callable[
    [EvaluationFixture, int, dict[str, Any], Path],
    LiveEvaluationResult,
]


_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "usage_limit",
    "quota",
    "limit reached",
    "weekly limit",
    "사용량 한도",
    "한도에 도달",
)


def _contains_rate_limit(value: str | None) -> bool:
    normalized = (value or "").casefold()
    return any(marker in normalized for marker in _RATE_LIMIT_MARKERS)


def _result_hit_rate_limit(result: LiveEvaluationResult) -> bool:
    return any(
        item.status
        in {
            ModelCallStatus.FAILED,
            ModelCallStatus.TIMED_OUT,
            ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
        }
        and _contains_rate_limit(item.error_summary)
        for item in result.model_call_receipts
    )


def _write_immutable_json(path: Path, value: object) -> None:
    encoded = canonical_json(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError(f"기존 campaign artifact와 현재 계약이 다릅니다: {path}")


def _read_model(path: Path, model_type: type[EvaluationModel]):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"campaign artifact를 읽을 수 없습니다: {path}") from exc
    return model_type.model_validate(raw)


def _cell_directory(root: Path, seed: int, fixture: EvaluationFixture) -> Path:
    return root / "cells" / f"seed-{seed}" / fixture.model_case_ref


def _expected_model_input(
    fixture: EvaluationFixture,
    seed: int,
) -> dict[str, Any]:
    inputs = ordered_model_inputs((fixture,), order_seed=seed)
    if len(inputs) != 1:
        raise RuntimeError("단일 fixture의 순서 입력이 하나가 아닙니다.")
    return inputs[0]


def _build_cell_record(
    *,
    fixture: EvaluationFixture,
    seed: int,
    model_input: dict[str, Any],
    result: LiveEvaluationResult,
    evaluation_contract_digest: str,
) -> CampaignCellRecord:
    if result.fixture_ids != (fixture.case_id,):
        raise RuntimeError("cell 실행 결과의 fixture binding이 다릅니다.")
    if len(result.batches) != 1 or len(result.batches[0].observations) != 1:
        raise RuntimeError("cell 실행 결과는 하나의 seed와 observation이어야 합니다.")
    if len(result.assessment_traces) != 1:
        raise RuntimeError("cell 실행 결과는 원시 assessment trace 하나를 가져야 합니다.")
    batch = result.batches[0]
    if batch.order_seed != seed:
        raise RuntimeError("cell 실행 결과의 order seed가 다릅니다.")
    resolved_models_digest = sha256_digest(result.resolved_models)
    return CampaignCellRecord(
        configuration_id=result.configuration_id,
        configuration_digest=result.configuration_digest,
        evaluation_contract_digest=evaluation_contract_digest,
        resolved_models_digest=resolved_models_digest,
        fixture_id=fixture.case_id,
        fixture_digest=sha256_digest(fixture),
        model_case_ref=fixture.model_case_ref,
        model_input_digest=sha256_digest(model_input),
        order_seed=seed,
        observation=batch.observations[0],
        assessment_trace=result.assessment_traces[0],
        model_call_receipts=result.model_call_receipts,
        execution_policy_evidence=result.execution_policy_evidence,
    )


def _validate_cell_record(
    record: CampaignCellRecord,
    *,
    fixture: EvaluationFixture,
    seed: int,
    model_input: dict[str, Any],
    manifest: CampaignManifest,
    model_lock: CampaignModelLock | None,
) -> None:
    expected = {
        "configuration_id": manifest.configuration_id,
        "configuration_digest": manifest.configuration_digest,
        "evaluation_contract_digest": manifest.evaluation_contract_digest,
        "fixture_id": fixture.case_id,
        "fixture_digest": sha256_digest(fixture),
        "model_case_ref": fixture.model_case_ref,
        "model_input_digest": sha256_digest(model_input),
        "order_seed": seed,
    }
    actual = {
        "configuration_id": record.configuration_id,
        "configuration_digest": record.configuration_digest,
        "evaluation_contract_digest": record.evaluation_contract_digest,
        "fixture_id": record.fixture_id,
        "fixture_digest": record.fixture_digest,
        "model_case_ref": record.model_case_ref,
        "model_input_digest": record.model_input_digest,
        "order_seed": record.order_seed,
    }
    if actual != expected:
        raise RuntimeError("기존 campaign cell이 현재 fixture·seed·설정과 다릅니다.")
    if (
        model_lock is not None
        and record.resolved_models_digest != model_lock.resolved_models_digest
    ):
        raise RuntimeError("기존 campaign cell의 model inventory lock이 다릅니다.")


def _bind_model_lock(
    path: Path,
    manifest: CampaignManifest,
    result: LiveEvaluationResult,
) -> CampaignModelLock:
    proposed = CampaignModelLock(
        configuration_digest=manifest.configuration_digest,
        evaluation_contract_digest=manifest.evaluation_contract_digest,
        resolved_models_digest=sha256_digest(result.resolved_models),
        resolved_models=result.resolved_models,
    )
    _write_immutable_json(path, proposed)
    locked = _read_model(path, CampaignModelLock)
    if locked != proposed:
        raise RuntimeError("campaign role model lock이 기존 값과 다릅니다.")
    return locked


def _write_interrupted_attempt(
    cell_dir: Path,
    *,
    fixture: EvaluationFixture,
    seed: int,
    model_input: dict[str, Any],
    evaluation_contract_digest: str,
    reason: str,
    result: LiveEvaluationResult | None = None,
) -> Path:
    path = cell_dir / "attempts" / f"attempt-{uuid4().hex}.json"
    payload: dict[str, Any] = {
        "receipt_schema": CAMPAIGN_ATTEMPT_SCHEMA,
        "fixture_id": fixture.case_id,
        "fixture_digest": sha256_digest(fixture),
        "model_case_ref": fixture.model_case_ref,
        "model_input_digest": sha256_digest(model_input),
        "evaluation_contract_digest": evaluation_contract_digest,
        "order_seed": seed,
        "status": "INTERRUPTED",
        "reason": reason[:1000],
    }
    if result is not None:
        payload.update(
            {
                "configuration_id": result.configuration_id,
                "configuration_digest": result.configuration_digest,
                "resolved_models": [
                    item.model_dump(mode="json") for item in result.resolved_models
                ],
                "model_call_receipts": [
                    item.model_dump(mode="json")
                    for item in result.model_call_receipts
                ],
                "execution_policy_evidence": [
                    item.model_dump(mode="json")
                    for item in result.execution_policy_evidence
                ],
                "assessment_traces": [
                    item.model_dump(mode="json")
                    for item in result.assessment_traces
                ],
            }
        )
    _write_immutable_json(path, payload)
    return path


def _collect_completed_records(
    root: Path,
    fixtures: tuple[EvaluationFixture, ...],
    order_seeds: tuple[int, ...],
    manifest: CampaignManifest,
    model_lock: CampaignModelLock | None,
) -> dict[tuple[int, str], CampaignCellRecord]:
    records: dict[tuple[int, str], CampaignCellRecord] = {}
    for seed in order_seeds:
        for fixture in fixtures:
            path = _cell_directory(root, seed, fixture) / "result.json"
            if not path.exists():
                continue
            model_input = _expected_model_input(fixture, seed)
            record = _read_model(path, CampaignCellRecord)
            _validate_cell_record(
                record,
                fixture=fixture,
                seed=seed,
                model_input=model_input,
                manifest=manifest,
                model_lock=model_lock,
            )
            records[(seed, fixture.case_id)] = record
    return records


def _aggregate_complete_campaign(
    fixtures: tuple[EvaluationFixture, ...],
    order_seeds: tuple[int, ...],
    records: dict[tuple[int, str], CampaignCellRecord],
    model_lock: CampaignModelLock,
    evaluation_scope: EvaluationScope,
) -> tuple[
    tuple[EvaluationObservationBatch, ...],
    RepeatedEvaluationReport,
    tuple[ModelCallReceipt, ...],
    tuple[ExecutionPolicyEvidence, ...],
]:
    fixture_by_ref = {item.model_case_ref: item for item in fixtures}
    batches: list[EvaluationObservationBatch] = []
    receipts: list[ModelCallReceipt] = []
    policy_evidence: list[ExecutionPolicyEvidence] = []
    for seed in order_seeds:
        observations = []
        for model_input in ordered_model_inputs(fixtures, order_seed=seed):
            fixture = fixture_by_ref[model_input["case_ref"]]
            record = records[(seed, fixture.case_id)]
            if record.resolved_models_digest != model_lock.resolved_models_digest:
                raise RuntimeError("campaign cell 사이의 model inventory lock이 다릅니다.")
            observations.append(record.observation)
            receipts.extend(record.model_call_receipts)
            policy_evidence.extend(record.execution_policy_evidence)
        batches.append(
            EvaluationObservationBatch(
                run_id=f"live-role-eval-{seed}",
                order_seed=seed,
                observations=tuple(observations),
            )
        )
    receipt_threads = [item.thread_id for item in receipts if item.thread_id is not None]
    evidence_threads = [
        item.thread_id for item in policy_evidence if item.thread_id is not None
    ]
    if (
        len(receipt_threads) != len(set(receipt_threads))
        or len(evidence_threads) != len(set(evidence_threads))
        or set(receipt_threads) != set(evidence_threads)
    ):
        raise RuntimeError("campaign 전체 model call과 권한 증거가 일대일이 아닙니다.")
    batch_tuple = tuple(batches)
    return (
        batch_tuple,
        evaluate_repeated_runs(
            fixtures,
            batch_tuple,
            evaluation_scope=evaluation_scope,
        ),
        tuple(receipts),
        tuple(policy_evidence),
    )


def run_live_evaluation_campaign(
    fixtures: tuple[EvaluationFixture, ...],
    *,
    order_seeds: tuple[int, ...],
    artifact_root: str | Path,
    configuration_id: str,
    configuration_digest: str,
    evaluation_contract_digest: str,
    execute_cell: CellExecutor,
    evaluation_scope: EvaluationScope = EvaluationScope.ROLE_FIXTURE_PROBE,
    resume: bool = False,
    max_new_cells: int | None = None,
) -> LiveEvaluationCampaignResult:
    if not fixtures:
        raise ValueError("campaign에는 fixture가 하나 이상 필요합니다.")
    if not order_seeds or len(order_seeds) != len(set(order_seeds)):
        raise ValueError("campaign order seed는 하나 이상의 중복 없는 값이어야 합니다.")
    if max_new_cells is not None and max_new_cells < 1:
        raise ValueError("max_new_cells는 1 이상이어야 합니다.")
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "campaign-manifest.json"
    if manifest_path.exists() and not resume:
        raise RuntimeError("기존 campaign은 --resume 없이 이어서 실행할 수 없습니다.")
    if resume and not manifest_path.exists():
        raise RuntimeError("재개할 campaign manifest가 없습니다.")
    manifest = CampaignManifest(
        configuration_id=configuration_id,
        configuration_digest=configuration_digest,
        evaluation_contract_digest=evaluation_contract_digest,
        evaluation_scope=evaluation_scope,
        fixture_catalog_digest=sha256_digest(fixtures),
        fixture_ids=tuple(sorted(item.case_id for item in fixtures)),
        order_seeds=order_seeds,
        expected_cell_count=len(fixtures) * len(order_seeds),
    )
    _write_immutable_json(manifest_path, manifest)
    manifest_digest = sha256_digest(manifest)
    lock_path = root / "model-lock.json"
    model_lock = (
        _read_model(lock_path, CampaignModelLock) if lock_path.exists() else None
    )
    if (
        model_lock is not None
        and (
            model_lock.configuration_digest != manifest.configuration_digest
            or model_lock.evaluation_contract_digest
            != manifest.evaluation_contract_digest
        )
    ):
        raise RuntimeError("campaign model lock의 역할·평가 계약 digest가 다릅니다.")
    records = _collect_completed_records(
        root,
        fixtures,
        order_seeds,
        manifest,
        model_lock,
    )
    total = manifest.expected_cell_count
    newly_completed = 0
    fixture_by_ref = {item.model_case_ref: item for item in fixtures}

    for seed in order_seeds:
        for model_input in ordered_model_inputs(fixtures, order_seed=seed):
            fixture = fixture_by_ref[model_input["case_ref"]]
            key = (seed, fixture.case_id)
            if key in records:
                continue
            if max_new_cells is not None and newly_completed >= max_new_cells:
                return LiveEvaluationCampaignResult(
                    status=CampaignStatus.PARTIAL,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    completed_cell_count=len(records),
                    total_cell_count=total,
                    newly_completed_cell_count=newly_completed,
                    remaining_cell_count=total - len(records),
                    report=None,
                    batches=(),
                    model_call_receipts=tuple(
                        receipt
                        for record in records.values()
                        for receipt in record.model_call_receipts
                    ),
                    execution_policy_evidence=tuple(
                        evidence
                        for record in records.values()
                        for evidence in record.execution_policy_evidence
                    ),
                    resolved_models=(
                        model_lock.resolved_models if model_lock is not None else ()
                    ),
                    pause_reason="max_new_cells에 도달했습니다.",
                )
            cell_dir = _cell_directory(root, seed, fixture)
            try:
                result = execute_cell(fixture, seed, model_input, cell_dir)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                attempt_path = _write_interrupted_attempt(
                    cell_dir,
                    fixture=fixture,
                    seed=seed,
                    model_input=model_input,
                    evaluation_contract_digest=manifest.evaluation_contract_digest,
                    reason=reason,
                )
                status = (
                    CampaignStatus.PAUSED_RATE_LIMIT
                    if _contains_rate_limit(reason)
                    else CampaignStatus.ERROR
                )
                return LiveEvaluationCampaignResult(
                    status=status,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    completed_cell_count=len(records),
                    total_cell_count=total,
                    newly_completed_cell_count=newly_completed,
                    remaining_cell_count=total - len(records),
                    report=None,
                    batches=(),
                    model_call_receipts=tuple(
                        receipt
                        for record in records.values()
                        for receipt in record.model_call_receipts
                    ),
                    execution_policy_evidence=tuple(
                        evidence
                        for record in records.values()
                        for evidence in record.execution_policy_evidence
                    ),
                    resolved_models=(
                        model_lock.resolved_models if model_lock is not None else ()
                    ),
                    pause_reason=reason[:1000],
                    attempt_receipt_path=str(attempt_path),
                )
            if (
                result.configuration_id != manifest.configuration_id
                or result.configuration_digest != manifest.configuration_digest
            ):
                raise RuntimeError("cell 실행 결과의 역할 설정이 campaign과 다릅니다.")
            if result.report.evaluation_scope is not manifest.evaluation_scope:
                raise RuntimeError("cell 실행 결과의 평가 범위가 campaign과 다릅니다.")
            model_lock = _bind_model_lock(lock_path, manifest, result)
            if _result_hit_rate_limit(result):
                attempt_path = _write_interrupted_attempt(
                    cell_dir,
                    fixture=fixture,
                    seed=seed,
                    model_input=model_input,
                    evaluation_contract_digest=manifest.evaluation_contract_digest,
                    reason="실제 역할 호출이 계정 사용량 한도에 도달했습니다.",
                    result=result,
                )
                return LiveEvaluationCampaignResult(
                    status=CampaignStatus.PAUSED_RATE_LIMIT,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    completed_cell_count=len(records),
                    total_cell_count=total,
                    newly_completed_cell_count=newly_completed,
                    remaining_cell_count=total - len(records),
                    report=None,
                    batches=(),
                    model_call_receipts=tuple(
                        receipt
                        for record in records.values()
                        for receipt in record.model_call_receipts
                    ),
                    execution_policy_evidence=tuple(
                        evidence
                        for record in records.values()
                        for evidence in record.execution_policy_evidence
                    ),
                    resolved_models=model_lock.resolved_models,
                    pause_reason="Codex 사용량 한도 도달",
                    attempt_receipt_path=str(attempt_path),
                )
            record = _build_cell_record(
                fixture=fixture,
                seed=seed,
                model_input=model_input,
                result=result,
                evaluation_contract_digest=manifest.evaluation_contract_digest,
            )
            _validate_cell_record(
                record,
                fixture=fixture,
                seed=seed,
                model_input=model_input,
                manifest=manifest,
                model_lock=model_lock,
            )
            _write_immutable_json(cell_dir / "result.json", record)
            records[key] = record
            newly_completed += 1

    if model_lock is None:
        raise RuntimeError("완료된 campaign에 model lock이 없습니다.")
    batches, report, receipts, policy_evidence = _aggregate_complete_campaign(
        fixtures,
        order_seeds,
        records,
        model_lock,
        manifest.evaluation_scope,
    )
    return LiveEvaluationCampaignResult(
        status=CampaignStatus.PASS if report.passed else CampaignStatus.FAIL,
        manifest=manifest,
        manifest_digest=manifest_digest,
        completed_cell_count=len(records),
        total_cell_count=total,
        newly_completed_cell_count=newly_completed,
        remaining_cell_count=0,
        report=report,
        batches=batches,
        model_call_receipts=receipts,
        execution_policy_evidence=policy_evidence,
        resolved_models=model_lock.resolved_models,
    )


__all__ = [
    "CAMPAIGN_ATTEMPT_SCHEMA",
    "CAMPAIGN_CELL_SCHEMA",
    "CAMPAIGN_MANIFEST_SCHEMA",
    "CAMPAIGN_MODEL_LOCK_SCHEMA",
    "CampaignCellRecord",
    "CampaignManifest",
    "CampaignModelLock",
    "CampaignStatus",
    "LiveEvaluationCampaignResult",
    "run_live_evaluation_campaign",
]
