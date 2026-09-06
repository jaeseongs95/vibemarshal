"""불변 planning checkpoint에 완료 lifecycle 관측을 덧붙이는 읽기 전용 경로."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..canonical import sha256_digest
from .benchmark_lifecycle import collect_lifecycle_observation
from .domain import EngineModel, PlanContractRevision
from .evaluation import BenchmarkCell, EvaluationCellCheckpoint, EvaluationContract
from .ledger import SQLiteEngineLedger


class BenchmarkObservationError(RuntimeError):
    """checkpoint/원장 결속이 달라 재관측할 수 없음을 나타낸다."""


class BenchmarkObservationBlocked(EngineModel):
    code: str
    reason: str
    next_actions: tuple[str, ...] = Field(min_length=1)
    references: dict[str, str]


class BenchmarkObservationResult(EngineModel):
    status: Literal["observed", "pending"]
    checkpoint_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cell: BenchmarkCell
    lifecycle_evidence_path: str | None = None
    blocked: BenchmarkObservationBlocked | None = None


_FORMAT = "flowmarshal-benchmark-execution-checkpoint-v1"


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BenchmarkObservationError(code) from error
    if not isinstance(value, dict):
        raise BenchmarkObservationError(code)
    return value


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=True))
    except ValueError:
        return False
    return True


def _checkpoint_context(
    checkpoint: EvaluationCellCheckpoint,
    run_root: Path,
    contract: EvaluationContract,
) -> tuple[BenchmarkCell, dict[str, Any], dict[str, Any], Path, Path, Path]:
    if checkpoint.contract_digest != contract.contract_digest:
        raise BenchmarkObservationError("OBSERVATION_CONTRACT_DIGEST_MISMATCH")
    if checkpoint.fixture_digest not in contract.fixture_digests or checkpoint.order_seed not in contract.order_seeds:
        raise BenchmarkObservationError("OBSERVATION_CHECKPOINT_SCOPE_MISMATCH")
    manifest = _read_json(run_root / "evaluation-contract.json", "OBSERVATION_CONTRACT_MANIFEST_MISSING")
    if EvaluationContract.model_validate(manifest).contract_digest != contract.contract_digest:
        raise BenchmarkObservationError("OBSERVATION_CONTRACT_MANIFEST_MISMATCH")
    metadata = _read_json(run_root / "run-metadata.json", "OBSERVATION_RUN_METADATA_MISSING")
    if metadata.get("evaluation_contract_digest") != contract.contract_digest:
        raise BenchmarkObservationError("OBSERVATION_SOURCE_CONTRACT_MISMATCH")
    assessment = checkpoint.raw_structured_assessment
    raw = assessment.get("raw")
    raw_cell = assessment.get("benchmark_cell")
    if not isinstance(raw, dict) or not isinstance(raw_cell, dict):
        raise BenchmarkObservationError("OBSERVATION_ORIGINAL_CELL_MISSING")
    original = BenchmarkCell.model_validate(raw_cell)
    if original.implementation != "skeleton_engine" or original.disposition != "selected":
        raise BenchmarkObservationError("OBSERVATION_SELECTED_ENGINE_CELL_REQUIRED")
    execution = raw.get("execution_checkpoint")
    if not isinstance(execution, dict) or execution.get("format") != _FORMAT:
        raise BenchmarkObservationError("OBSERVATION_EXECUTION_CHECKPOINT_MISSING")
    required = (
        "project_id", "goal_id", "goal_contract_digest", "plan_revision_id", "activation_digest",
        "workspace", "database_path", "artifact_root", "planning_outcome_digest",
    )
    if any(not isinstance(execution.get(key), str) or not execution[key] for key in required):
        raise BenchmarkObservationError("OBSERVATION_EXECUTION_CHECKPOINT_INVALID")
    if (
        original.selected_plan_activation_digest != execution["activation_digest"]
        or original.neutral_input_digest != raw.get("neutral_input_digest")
        or raw.get("selected_activation_digest") != execution["activation_digest"]
        or sha256_digest(raw.get("planning_outcome")) != execution["planning_outcome_digest"]
    ):
        raise BenchmarkObservationError("OBSERVATION_ORIGINAL_BINDING_MISMATCH")
    scenario = raw.get("scenario_id", original.scenario_id)
    if scenario != original.scenario_id or raw.get("order_seed", original.order_seed) != checkpoint.order_seed:
        raise BenchmarkObservationError("OBSERVATION_FIXTURE_SEED_MISMATCH")
    work_base = run_root / "work" / f"seed-{checkpoint.order_seed}" / original.scenario_id / "skeleton_engine"
    workspace = Path(execution["workspace"])
    if not _path_within(workspace, work_base) or workspace.name != "project":
        raise BenchmarkObservationError("OBSERVATION_WORKSPACE_PATH_MISMATCH")
    attempt_root = workspace.parent
    state_root = attempt_root / "budget-state"
    database, artifacts = Path(execution["database_path"]), Path(execution["artifact_root"])
    if database.resolve(strict=False) != (state_root / "flowmarshal-engine.sqlite3").resolve(strict=False):
        raise BenchmarkObservationError("OBSERVATION_DATABASE_PATH_MISMATCH")
    if artifacts.resolve(strict=False) != (state_root / "artifacts").resolve(strict=False):
        raise BenchmarkObservationError("OBSERVATION_ARTIFACT_PATH_MISMATCH")
    recorded = _read_json(attempt_root / "execution-checkpoint.json", "OBSERVATION_CHECKPOINT_FILE_MISSING")
    if recorded != execution:
        raise BenchmarkObservationError("OBSERVATION_CHECKPOINT_FILE_MISMATCH")
    selected = _read_json(attempt_root / "selected-plan.json", "OBSERVATION_SELECTED_PLAN_FILE_MISSING")
    selected_plan = PlanContractRevision.model_validate(selected)
    if selected_plan.plan_revision_id != execution["plan_revision_id"] or selected_plan.activation_digest != execution["activation_digest"]:
        raise BenchmarkObservationError("OBSERVATION_SELECTED_PLAN_FILE_MISMATCH")
    return original, raw, execution, workspace, database, artifacts


def _verify_ledger_binding(
    ledger: SQLiteEngineLedger, execution: dict[str, Any], workspace: Path
) -> str:
    with ledger.read() as connection:
        project = connection.execute("SELECT root,run_state FROM projects WHERE id=?", (execution["project_id"],)).fetchone()
        if project is None or Path(project["root"]).resolve(strict=False) != workspace.resolve(strict=False):
            raise BenchmarkObservationError("OBSERVATION_PROJECT_WORKSPACE_MISMATCH")
        goal = connection.execute(
            "SELECT id FROM goal_revisions WHERE project_id=? AND goal_id=? AND definition_digest=?",
            (execution["project_id"], execution["goal_id"], execution["goal_contract_digest"]),
        ).fetchone()
        plan_row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id=? AND project_id=?", (execution["plan_revision_id"], execution["project_id"])).fetchone()
        if goal is None or plan_row is None:
            raise BenchmarkObservationError("OBSERVATION_GOAL_OR_PLAN_MISMATCH")
        plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
        if plan.activation_digest != execution["activation_digest"] or plan.definition.goal_contract_digest != execution["goal_contract_digest"]:
            raise BenchmarkObservationError("OBSERVATION_EXACT_PLAN_MISMATCH")
        if not ledger.verify_history(execution["project_id"]):
            raise BenchmarkObservationError("OBSERVATION_HISTORY_INVALID")
        return project["run_state"]


def _write_evidence(run_root: Path, checkpoint: EvaluationCellCheckpoint, cell: BenchmarkCell) -> Path:
    observation = cell.lifecycle_observation
    if observation is None:
        raise BenchmarkObservationError("OBSERVATION_LIFECYCLE_MISSING")
    target = (
        run_root / "lifecycle-observations" / checkpoint.checkpoint_digest[7:]
        / f"{observation.observation_digest[7:]}.json"
    )
    body = {
        "format": "flowmarshal-benchmark-lifecycle-observation-v1",
        "checkpoint_digest": checkpoint.checkpoint_digest,
        "source_cell_digest": sha256_digest(checkpoint.raw_structured_assessment["benchmark_cell"]),
        "observation": observation.model_dump(mode="json"),
        "observed_cell": cell.model_dump(mode="json"),
    }
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
    except FileExistsError:
        if target.read_text(encoding="utf-8") != payload:
            raise BenchmarkObservationError("OBSERVATION_EVIDENCE_CONFLICT")
    return target


def observe_benchmark_execution_checkpoint(
    checkpoint: EvaluationCellCheckpoint,
    *,
    run_root: Path | str,
    contract: EvaluationContract,
) -> BenchmarkObservationResult:
    """기존 checkpoint를 덮어쓰지 않고 같은 SQLite 원장에서 lifecycle을 재관측한다."""

    root = Path(run_root).resolve(strict=True)
    original, raw, execution, workspace, database, artifacts = _checkpoint_context(checkpoint, root, contract)
    if not database.is_file() or not artifacts.is_dir():
        raise BenchmarkObservationError("OBSERVATION_LEDGER_OR_ARTIFACT_MISSING")
    ledger = SQLiteEngineLedger(database, artifact_root=artifacts)
    state = _verify_ledger_binding(ledger, execution, workspace)
    if state != "completed":
        blocked = BenchmarkObservationBlocked(
            code="LIFECYCLE_PENDING",
            reason="원장이 아직 completed가 아니므로 planning checkpoint를 lifecycle evidence로 바꿀 수 없습니다.",
            next_actions=(
                f"Plan activation digest {execution['activation_digest']}로 사용자가 정확한 Plan을 활성화합니다.",
                "Core run once를 완료 상태까지 실행합니다.",
                "동일 checkpoint를 다시 observe하여 lifecycle evidence를 추가합니다.",
            ),
            references={key: execution[key] for key in ("project_id", "goal_id", "goal_contract_digest", "plan_revision_id", "activation_digest")},
        )
        return BenchmarkObservationResult(status="pending", checkpoint_digest=checkpoint.checkpoint_digest, cell=original, blocked=blocked)
    observation = collect_lifecycle_observation(
        ledger, project_id=execution["project_id"], plan_activation_digest=execution["activation_digest"],
        model_lock_digest=original.model_lock_digest, neutral_input_digest=original.neutral_input_digest,
    )
    cell = original.model_copy(update={
        "lifecycle_observation": observation,
        "lifecycle_evidence_digest": observation.observation_digest,
        "detailed_task_count": observation.detailed_execution_spec_count,
        "unexecuted_detailed_task_count": observation.unexecuted_execution_spec_count,
    })
    evidence = _write_evidence(root, checkpoint, cell)
    return BenchmarkObservationResult(status="observed", checkpoint_digest=checkpoint.checkpoint_digest, cell=cell, lifecycle_evidence_path=str(evidence))
