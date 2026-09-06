"""단일 자연어 Goal의 제한된 실제 planning 관측 driver.

Plan 활성화, Worker 실행, 외부 효과와 qualification/cutover 판정은 하지 않는다.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any

from pydantic import ValidationError

from flowmarshal.canonical import canonical_json, json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.context import ProjectMapper, goal_context_observations
from flowmarshal.engine.budget import (
    BudgetedRoleRunner, BudgetManager, GoalBudgetPolicy,
)
from flowmarshal.engine.domain import (
    GoalContractRevision, ModelAssignmentContract, ModelFallback, PlanContractRevision,
    PlanSkeletonCandidate, PlanningBudgetPolicy, ProjectMapRevision,
    ReviewerSubmission, RevisionStatus, RoleAssignmentPolicy, StateSnapshot,
    validate_reviewer_submission_evidence,
)
from flowmarshal.engine.goal import (
    GoalNormalizerAdapter, GoalPreparationOutcome, GoalPreparationPipeline,
    GoalReviewerAdapter, ReviewDraft, goal_review_evidence_catalog,
)
from flowmarshal.engine.domain import ProjectProfileRevision
from flowmarshal.engine.ledger import (
    ENGINE_SCHEMA_ID, SQLITE_APPLICATION_ID, SQLiteEngineLedger,
)
from flowmarshal.engine.model_lock import OperationalBinding, verify_binding
from flowmarshal.engine.models import AssignmentResolver, EngineRoleConfiguration
from flowmarshal.engine.plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter, PlanExpansionEnvelopeV2, PlanReviewerAdapter,
    RuleBasedTaskAssigner, SkeletonBatchDraft, SkeletonGeneratorAdapter,
    SkeletonReviewerAdapter, _review_submission,
)
from flowmarshal.engine.planning import (
    SkeletonFirstPlanner, plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.role_execution import (
    RoleTimeoutPolicy, use_role_timeout_policy, verify_role_timeout_binding,
)
from flowmarshal.engine.qualification import (
    PlanningScenarioCatalog, _profile, project_root, source_manifest_files,
)
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner, RoleCallReceipt, RoleCallRequest, RoleCallResult, StructuredRoleError,
    strict_json_output_schema,
    verify_role_receipt,
)
from flowmarshal.engine.runtime import CodexAppServerRuntime, RuntimeObservation
from flowmarshal.engine.service import EngineService
try:  # `python scripts/diagnostics/...py`와 module 실행을 모두 지원한다.
    from scripts.diagnostics.inspection_workspace import (
        capture_workspace_binding, verify_workspace_binding,
    )
except ModuleNotFoundError:  # pragma: no cover - 직접 파일 실행 경로
    from inspection_workspace import capture_workspace_binding, verify_workspace_binding


MAXIMUM_CALLS = 14
ROLE_CONFIGURATION_FOR_REQUEST = {
    "goal_normalizer": "normalizer", "goal_refiner": "normalizer", "goal_reviewer": "critical_reviewer",
    "skeleton_generator": "skeleton_generator", "skeleton_refiner": "skeleton_generator",
    "skeleton_reviewer": "general_reviewer", "plan_expander": "plan_expander",
    "plan_refiner": "plan_expander", "compact_plan_reviewer": "general_reviewer",
    "critical_effect_reviewer": "critical_reviewer", "high_risk_reviewer": "critical_reviewer",
    "external_effect_reviewer": "critical_reviewer",
}


def _write_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_bytes_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _project_files(root: Path) -> dict[str, str]:
    """계획 중 입력 파일 변경을 탐지한다. Python bytecode만 제외한다."""
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError("PROJECT_SYMLINK_NOT_ALLOWED")
        if path.is_file():
            files[relative.as_posix()] = sha256_bytes(path.read_bytes())
    return files


def _artifact_files(root: Path) -> dict[str, str]:
    """SQLite의 비활성 sidecar만 제외한 append-only artifact snapshot."""
    files = _project_files(root)
    for name in files:
        if name.endswith(".sqlite3-wal") and (root / name).stat().st_size != 0:
            raise RuntimeError("ARTIFACT_SQLITE_WAL_NOT_EMPTY")
    return {
        name: digest for name, digest in files.items()
        if not name.endswith((".sqlite3-wal", ".sqlite3-shm"))
    }


class CaptureRuntime(CodexAppServerRuntime):
    """반환된 transport 관측만 호출별 append-only 파일에 남긴다."""

    def __init__(self, *, run_root: Path, **kwargs: Any) -> None:
        self.run_root = run_root
        self.capture: Path | None = None
        self.indices: dict[tuple[str, str], int] = {}
        super().__init__(**kwargs)

    @contextmanager
    def capture_call(self, capture: Path):
        if self.capture is not None or not capture.resolve().is_relative_to((self.run_root / "calls").resolve()):
            raise RuntimeError("CALL_CAPTURE_BOUNDARY_INVALID")
        self.capture = capture
        try:
            yield
        finally:
            self.capture = None

    def _record(self, kind: str, value: Any) -> None:
        if self.capture is None:
            return
        key = (str(self.capture), kind)
        number = self.indices.get(key, 0) + 1
        self.indices[key] = number
        _write_new(self.capture / f"{kind}-{number:02d}.json", value)

    def verify_execution_policy(self, cwd: Path | str):
        value = super().verify_execution_policy(cwd)
        self._record("policy", value)
        return value

    def list_models(self):
        value = super().list_models()
        self._record("inventory", value)
        return value

    def create_thread(self, **kwargs: Any):
        if kwargs.get("ephemeral") is not False or self.capture is None:
            raise RuntimeError("PERSISTENT_ROLE_THREAD_REQUIRED")
        _write_new(self.capture / "thread.intent.json", kwargs)
        value = super().create_thread(**kwargs)
        _write_new(self.capture / "thread.receipt.json", value)
        thread = value.payload.get("thread", {})
        if thread.get("ephemeral") is not False or thread.get("turns") != []:
            raise RuntimeError("ACTUAL_ROLE_THREAD_PERSISTENCE_OR_EMPTY_STATE_MISMATCH")
        return value

    def start_turn(self, **kwargs: Any):
        if self.capture is None:
            raise RuntimeError("TURN_CAPTURE_REQUIRED")
        request = RoleCallRequest.model_validate_json((self.capture / "request.json").read_text(encoding="utf-8"))
        thread = json.loads((self.capture / "thread.receipt.json").read_text(encoding="utf-8"))
        expected_thread_id = thread["binding"]["thread_id"]
        expected = {
            "thread_id": expected_thread_id, "cwd": Path(request.cwd),
            "prompt": canonical_json(request.payload), "model": request.model,
            "effort": request.effort, "output_schema": strict_json_output_schema(request.output_schema),
        }
        if set(kwargs) != set(expected) or any(kwargs[key] != value for key, value in expected.items() if key != "cwd") or Path(kwargs["cwd"]).resolve() != expected["cwd"].resolve():
            raise RuntimeError("ACTUAL_TURN_REQUEST_BINDING_MISMATCH")
        _write_new(self.capture / "turn.intent.json", kwargs)
        value = super().start_turn(**kwargs)
        _write_new(self.capture / "turn.receipt.json", value)
        return value

    def read(self, **kwargs: Any):
        value = super().read(**kwargs)
        if self.capture is not None and not value.active and not (self.capture / "terminal.json").exists():
            _write_new(self.capture / "terminal.json", value)
        return value

    def interrupt(self, **kwargs: Any):
        if self.capture is None:
            raise RuntimeError("INTERRUPT_CAPTURE_REQUIRED")
        _write_new(self.capture / "interrupt.intent.json", kwargs)
        value = super().interrupt(**kwargs)
        _write_new(self.capture / "interrupt.receipt.json", value)
        return value


class RecordedRunner:
    def __init__(self, runtime: CaptureRuntime, run_root: Path, lock: dict[str, Any]) -> None:
        self.runtime, self.run_root, self.lock = runtime, run_root, lock
        self.runner = CodexStructuredRoleRunner(
            runtime, max_schema_recovery_attempts=0, ephemeral_threads=False,
            operational_binding=OperationalBinding.model_validate(lock["operational_binding"]),
        )

    def _verify_lock(self) -> None:
        verify_workspace_binding(project_root(), self.lock["workspace_binding"])
        if sha256_bytes(Path(self.lock["codex_bin"]).read_bytes()) != self.lock["codex_bin_digest"]:
            raise RuntimeError("CODEX_EXECUTABLE_CHANGED")
        if sha256_bytes(Path(self.lock["role_config"]).read_bytes()) != self.lock["role_config_digest"]:
            raise RuntimeError("ROLE_CONFIGURATION_CHANGED")
        timeout_path = self.lock.get("role_timeout_policy_path")
        if (
            timeout_path is not None
            and sha256_bytes(Path(timeout_path).read_bytes())
            != self.lock["role_timeout_policy_bytes_digest"]
        ):
            raise RuntimeError("ROLE_TIMEOUT_POLICY_CHANGED")
        for path_key, digest_key, error_code in (
            ("budget_policy_path", "budget_policy_bytes_digest", "BUDGET_POLICY_CHANGED"),
            (
                "timeout_usage_adjustment_path",
                "timeout_usage_adjustment_bytes_digest",
                "TIMEOUT_USAGE_ADJUSTMENT_CHANGED",
            ),
        ):
            configured_path = self.lock.get(path_key)
            if (
                configured_path is not None
                and sha256_bytes(Path(configured_path).read_bytes())
                != self.lock[digest_key]
            ):
                raise RuntimeError(error_code)
        if source_manifest_files(project_root()) != self.lock["source_manifest"]:
            raise RuntimeError("SOURCE_LOCK_CHANGED")
        if _project_files(Path(self.lock.get("project_root", self.run_root / "project"))) != self.lock["project_files"]:
            raise RuntimeError("PLANNING_PROJECT_INPUT_CHANGED")
        if "resume_source" in self.lock:
            source = self.lock["resume_source"]
            snapshot = (
                _artifact_files
                if source.get("resume_mode") == "plan_review_timeout_continuation"
                else _project_files
            )
            if snapshot(Path(source["run_root"])) != source["files"]:
                raise RuntimeError("GOAL_REPAIR_ORIGINAL_ARTIFACT_CHANGED")
        retry = self.lock.get("retry_preflight")
        if retry is not None:
            failed_run = Path(retry["failed_run_root"])
            failed_preflight_path = failed_run / "preflight.json"
            failed_preflight = json.loads(failed_preflight_path.read_text(encoding="utf-8"))
            failed_preflight_body = {
                key: value for key, value in failed_preflight.items() if key != "lock_digest"
            }
            if (
                sha256_bytes(failed_preflight_path.read_bytes())
                != retry["failed_preflight_file_digest"]
                or failed_preflight.get("lock_digest")
                != sha256_digest(failed_preflight_body)
                or failed_preflight.get("lock_digest") != retry["failed_preflight_digest"]
                or sha256_digest(json.loads(
                    (failed_run / "summary.json").read_text(encoding="utf-8")
                )) != retry["failed_summary_digest"]
                or any(
                    sha256_bytes((failed_run / relative).read_bytes()) != digest
                    for relative, digest in retry["failed_protected_files"].items()
                )
                or sha256_bytes(Path(retry["claim_path"]).read_bytes())
                != retry["claim_file_digest"]
                or (failed_run / "calls").exists()
            ):
                raise RuntimeError("RETRY_PREFLIGHT_PROVENANCE_CHANGED")
            followup = {
                "claim_digest": retry["claim_digest"],
                "source_outcome_digest": retry["source_outcome_digest"],
                "failed_run_root": retry["failed_run_root"],
                "failed_preflight_digest": retry["failed_preflight_digest"],
                "released_provider_call_id": retry["released_provider_call_id"],
                "run_root": str(self.run_root.resolve()),
                "preflight_digest": sha256_digest(self.lock),
            }
            followup_path = Path(retry["claim_followup_path"])
            if (
                not followup_path.is_file()
                or json.loads(followup_path.read_text(encoding="utf-8")) != followup
            ):
                raise RuntimeError("RETRY_PREFLIGHT_FOLLOWUP_CLAIM_CHANGED")
            ledger = SQLiteEngineLedger(
                failed_run / "ledger" / "flowmarshal-engine.sqlite3",
                artifact_root=failed_run / "ledger" / "artifacts",
            )
            with ledger.read() as connection:
                released = connection.execute(
                    "SELECT status,receipt_json,usage_id,attempt_id FROM provider_calls WHERE id=?",
                    (retry["released_provider_call_id"],),
                ).fetchone()
            if (
                released is None
                or released["status"] != "released"
                or any(released[key] is not None for key in (
                    "receipt_json", "usage_id", "attempt_id",
                ))
            ):
                raise RuntimeError("RETRY_PREFLIGHT_RELEASE_CHANGED")

    def run(self, request: RoleCallRequest, *, validator: Any = None) -> RoleCallResult:
        try:
            self._verify_lock()
            call_number = len(list((self.run_root / "calls").glob("*/turn.intent.json"))) + 1
            if call_number > self.lock.get("maximum_provider_calls", MAXIMUM_CALLS):
                raise RuntimeError("MAXIMUM_PROVIDER_CALLS_EXCEEDED")
        except Exception as error:
            raise StructuredRoleError(
                str(error), receipts=tuple(self.runner.receipts), effects_started=False,
            ) from error
        capture = self.run_root / "calls" / f"{call_number:02d}-{request.role}"
        capture.mkdir(parents=True, exist_ok=False)
        _write_new(capture / "request.json", request)
        _write_new(capture / "strict-schema.json", strict_json_output_schema(request.output_schema))
        _write_new(capture / "request-binding.json", {
            "request_digest": request.request_digest, "role": request.role,
            "model": request.model, "effort": request.effort,
            "timeout_seconds": request.timeout_seconds,
            "timeout_policy_digest": request.timeout_policy_digest,
            "operational_binding": request.operational_binding,
        })
        current = self.runtime.list_models()
        binding = OperationalBinding.model_validate(self.lock["operational_binding"])
        verify_binding(binding, current)
        roles = EngineRoleConfiguration.model_validate_json(Path(self.lock["role_config"]).read_text(encoding="utf-8"))
        roles.validate_inventory(current)
        if roles.operational_binding(current).lock_digest != binding.lock_digest:
            raise RuntimeError("MODEL_LOCK_CHANGED")
        configuration_name = ROLE_CONFIGURATION_FOR_REQUEST.get(request.role)
        if configuration_name is None:
            raise RuntimeError(f"UNMAPPED_ROLE_CONFIGURATION: {request.role}")
        configured = getattr(roles, configuration_name)
        if "role_timeout_policy" in self.lock:
            timeout_policy = RoleTimeoutPolicy.model_validate(self.lock["role_timeout_policy"])
            verify_role_timeout_binding(
                role=request.role, timeout_seconds=request.timeout_seconds,
                timeout_policy_digest=request.timeout_policy_digest, policy=timeout_policy,
            )
        request_binding = verify_binding(request.operational_binding, current,
            role=request.role, model=request.model, effort=request.effort)
        request_role = next(item for item in request_binding.lock.roles if item.role == request.role)
        if (request.model, request.effort, tuple((item.model, item.effort) for item in request_role.allowed_fallbacks)) != (
            configured.model, configured.effort, tuple((item.model, item.effort) for item in configured.allowed_fallbacks),
        ):
            raise RuntimeError("ROLE_CONFIGURATION_REQUEST_MISMATCH")
        try:
            with self.runtime.capture_call(capture):
                result = self.runner.run(request, validator=validator)
            self._verify_lock()
            _write_new(capture / "result.json", result)
            return result
        except Exception as error:
            receipts = error.receipts if isinstance(error, StructuredRoleError) else self.runner.receipts
            try:
                self._verify_lock()
                lock_error = None
            except Exception as verification_error:
                lock_error = str(verification_error)
            _write_new(capture / "failed.json", {
                "error_type": type(error).__name__, "error": str(error),
                "receipts": receipts, "post_call_lock_error": lock_error,
            })
            raise

    @property
    def receipts(self):
        return self.runner.receipts


def _selected_plan(outcome: Any) -> Any:
    digest = outcome.selected_activation_digest
    if digest is None:
        return None
    return next((item.plan for item in outcome.plan_evaluations if item.plan.activation_digest == digest), None)


@dataclass(frozen=True)
class StoredPlanningCheckpoint:
    profile: ProjectProfileRevision
    goal_revisions: tuple[GoalContractRevision, ...]
    state: StateSnapshot
    project_map: ProjectMapRevision
    candidate: PlanSkeletonCandidate
    skeleton_submission: ReviewerSubmission
    plan: PlanContractRevision
    source_requests: tuple[RoleCallRequest, ...]
    source_receipts: tuple[RoleCallReceipt, ...]
    timed_out_request: RoleCallRequest
    timed_out_receipt: RoleCallReceipt
    terminal_observation: RuntimeObservation
    import_entries: tuple[dict[str, Any], ...]


@dataclass
class StoredGenerator:
    checkpoint: StoredPlanningCheckpoint
    delegate: Any

    def generate(self, *, candidate_count: int, **_: Any):
        if candidate_count != 1:
            raise ValueError("저장된 planning checkpoint는 단일 후보에만 결속됩니다.")
        return (self.checkpoint.candidate,)

    def refine(self, **kwargs: Any):
        return self.delegate.refine(**kwargs)


@dataclass
class StoredSkeletonReviewer:
    checkpoint: StoredPlanningCheckpoint
    delegate: Any

    def review(self, *, candidate: PlanSkeletonCandidate, goal, state, project_map, **kwargs: Any):
        if sha256_digest(candidate) == sha256_digest(self.checkpoint.candidate):
            catalog = skeleton_review_evidence_catalog(candidate, goal, state, project_map)
            return self.checkpoint.skeleton_submission.model_copy(update={
                "candidate_digest": sha256_digest(candidate),
                "evidence_catalog_digest": sha256_digest(catalog),
            })
        return self.delegate.review(
            candidate=candidate, goal=goal, state=state, project_map=project_map, **kwargs
        )


@dataclass
class StoredPlanExpander:
    checkpoint: StoredPlanningCheckpoint
    delegate: Any

    def expand(
        self, *, candidate: PlanSkeletonCandidate,
        planning_budget: PlanningBudgetPolicy, previous_plan=None, **kwargs: Any,
    ):
        if (
            previous_plan is None
            and sha256_digest(candidate) == sha256_digest(self.checkpoint.candidate)
        ):
            if self.checkpoint.plan.definition.planning_budget != planning_budget:
                raise ValueError("저장된 Plan과 continuation logical budget이 다릅니다.")
            return self.checkpoint.plan
        return self.delegate.expand(
            candidate=candidate, planning_budget=planning_budget,
            previous_plan=previous_plan, **kwargs,
        )

    def refine(self, **kwargs: Any):
        return self.delegate.refine(**kwargs)


def _planning_continuation_target(original: Path) -> Path:
    if not original.is_absolute() or not original.is_dir():
        raise ValueError("--continue-planning-run은 존재하는 절대 경로여야 합니다.")
    preflight = json.loads((original / "preflight.json").read_text(encoding="utf-8"))
    return Path(preflight["project_root"]).resolve(strict=True)


def _load_goal_resume(original: Path, run_root: Path, scenario: Any, roles_bytes: bytes, codex_bin: Path):
    """완료된 첫 Goal 실패의 입력·DB를 복제한다. 원본에는 쓰지 않는다."""
    if not original.is_absolute() or not original.is_dir():
        raise ValueError("--resume-goal-run은 존재하는 절대 경로여야 합니다.")
    if run_root.resolve().is_relative_to(original.resolve()):
        raise ValueError("후속 실행은 원본 artifact 밖에 있어야 합니다.")
    original_files = _project_files(original)
    summary = json.loads((original / "summary.json").read_text(encoding="utf-8"))
    preflight = json.loads((original / "preflight.json").read_text(encoding="utf-8"))
    previous = GoalPreparationOutcome.model_validate_json((original / "goal-preparation.json").read_text(encoding="utf-8"))
    if (summary["status"] != "GOAL_BLOCKED" or summary["role_calls"] != 2
            or "resume_source" in preflight or (original / "goal-refinement.json").exists()
            or previous.goal_contract.revision_no != 1):
        raise ValueError("완료된 첫 Goal 준비 실패에만 한 번 후속 정제를 허용합니다.")
    if (preflight["scenario"]["source_request"] != scenario.source_request
            or preflight["role_config_digest"] != sha256_bytes(roles_bytes)
            or preflight["codex_bin_digest"] != sha256_bytes(codex_bin.read_bytes())):
        raise ValueError("후속 Goal 정제의 요청·역할·실행 파일이 원본과 다릅니다.")
    calls = sorted((original / "calls").iterdir())
    if len(calls) != 2:
        raise ValueError("원본 Goal 호출 수가 다릅니다.")
    verified = []
    for directory, role in zip(calls, ("goal_normalizer", "goal_reviewer"), strict=True):
        request = RoleCallRequest.model_validate_json((directory / "request.json").read_text(encoding="utf-8"))
        result = RoleCallResult.model_validate_json((directory / "result.json").read_text(encoding="utf-8"))
        verify_role_receipt(request, result)
        if request.role != role or result.receipt.status != "succeeded" or result.receipt.schema_recovery_attempts != 0:
            raise ValueError("원본 Goal 역할의 완료 결속이 다릅니다.")
        verified.append((request, result))
    normalizer_request, normalized = verified[0]
    actual_review = ReviewDraft.model_validate(verified[1][1].payload)
    if (normalized.receipt != previous.normalizer_receipt or verified[1][1].receipt != previous.reviewer_receipt
            or normalized.payload != previous.proposal.model_dump(mode="json")
            or normalizer_request.payload["source_request"] != scenario.source_request
            or previous.review.reviewer_role != "goal_reviewer"
            or previous.review.ratings != actual_review.ratings
            or [item.model_dump(mode="json") for item in previous.review.findings]
               != [item.model_dump(mode="json") for item in actual_review.findings]):
        raise ValueError("원본 Goal Outcome이 실제 호출 결과와 다릅니다.")
    target = Path(normalizer_request.cwd).resolve(strict=True)
    if target != (original / "project").resolve() or _project_files(target) != preflight["project_files"]:
        raise ValueError("원본 Goal의 대상 프로젝트가 변경됐습니다.")
    shutil.copytree(original / "ledger", run_root / "ledger")
    ledger = SQLiteEngineLedger(run_root / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=run_root / "ledger" / "artifacts")
    with ledger.read() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (previous.goal_contract.definition.project_id,)).fetchone()
        row = connection.execute("SELECT payload_json FROM profile_revisions WHERE id = ?", (project["active_profile_revision_id"],)).fetchone()
        profile = ProjectProfileRevision.model_validate_json(row["payload_json"])
        if (Path(project["root"]).resolve() != target
                or connection.execute("SELECT COUNT(*) FROM goal_revisions").fetchone()[0] != 0
                or connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0] != 0):
            raise ValueError("원본은 Plan 활성화·Goal 등록 전의 준비 실패여야 합니다.")
    if normalizer_request.payload["project_profile"] != profile.definition.model_dump(mode="json"):
        raise ValueError("원본 정규화가 등록된 Profile과 다릅니다.")
    if _project_files(original) != original_files:
        raise ValueError("원본 artifact가 입력 복제 중 변경됐습니다.")
    source = {"run_root": str(original.resolve()), "files": original_files,
              "outcome_digest": sha256_digest(previous), "prior_provider_calls": 2}
    return previous, profile, target, tuple(normalizer_request.payload["observed_facts"]), source


def _load_completed_role(directory: Path, role: str) -> tuple[RoleCallRequest, RoleCallResult]:
    """성공 receipt를 단일 실제 terminal의 JSON 출력에 다시 결속한다."""
    request = RoleCallRequest.model_validate_json((directory / "request.json").read_text(encoding="utf-8"))
    result = RoleCallResult.model_validate_json((directory / "result.json").read_text(encoding="utf-8"))
    terminal = RuntimeObservation.model_validate_json((directory / "terminal.json").read_text(encoding="utf-8"))
    verify_role_receipt(request, result)
    try:
        terminal_payload = json.loads(terminal.final_response or "")
    except (TypeError, ValueError) as error:
        raise ValueError("성공 역할의 terminal 출력이 JSON object가 아닙니다.") from error
    if (
        request.role != role
        or result.receipt.status != "succeeded"
        or result.receipt.schema_recovery_attempts != 0
        or len(result.receipt.turn_ids) != 1
        or terminal.active
        or terminal.terminal_status != "completed"
        or terminal.thread_id != result.receipt.thread_id
        or tuple(result.receipt.turn_ids) != (terminal.turn_id,)
        or not isinstance(terminal_payload, dict)
        or terminal_payload != result.payload
    ):
        raise ValueError("READY Goal의 실제 단일 역할 완료 결속이 다릅니다.")
    return request, result


def _previous_skeleton_provider_schema() -> dict[str, Any]:
    """Core TaskSkeleton 제약을 전달하기 직전 provider schema를 현재 계약에서 도출한다."""
    schema = deepcopy(strict_json_output_schema(SkeletonBatchDraft.model_json_schema()))
    properties = schema["$defs"]["SkeletonTaskDraft"]["properties"]
    properties["task_ref"].pop("pattern")
    properties["objective"].pop("minLength")
    properties["objective"].pop("maxLength")
    properties["contributes_to"].pop("minItems")
    properties["produces"].pop("minItems")
    return schema


def _validate_skeleton_contract_failure(
    request: RoleCallRequest,
    receipt: RoleCallReceipt,
    terminal: RuntimeObservation,
) -> None:
    """원시 출력이 구 provider 계약만 통과한 Core 최소 길이 불일치인지 확인한다."""
    if strict_json_output_schema(request.output_schema) != _previous_skeleton_provider_schema():
        raise ValueError("첫 Skeleton 실패가 수정 직전 provider schema에 결속되지 않았습니다.")
    try:
        payload = json.loads(terminal.final_response or "")
    except (TypeError, ValueError) as error:
        raise ValueError("첫 Skeleton 실패의 terminal 출력이 JSON object가 아닙니다.") from error
    if not isinstance(payload, dict):
        raise ValueError("첫 Skeleton 실패의 terminal 출력이 JSON object가 아닙니다.")
    try:
        SkeletonBatchDraft.model_validate(payload)
    except ValidationError:
        pass
    else:
        raise ValueError("첫 Skeleton 출력이 현재 Core 계약에서도 유효합니다.")
    locations: list[tuple[int, int, str]] = []
    try:
        for candidate_index, candidate in enumerate(payload["candidates"]):
            for task_index, task in enumerate(candidate["tasks"]):
                for field_name in ("contributes_to", "produces"):
                    if task[field_name] == []:
                        locations.append((candidate_index, task_index, field_name))
    except (KeyError, TypeError) as error:
        raise ValueError("첫 Skeleton 출력이 구 provider schema의 필수 구조와 다릅니다.") from error
    if not locations:
        raise ValueError("첫 Skeleton 출력에 Core 최소 길이 불일치가 없습니다.")
    repaired = deepcopy(payload)
    for candidate_index, task_index, field_name in locations:
        repaired["candidates"][candidate_index]["tasks"][task_index][field_name] = [
            f"contract-probe:{field_name}"
        ]
    try:
        normalized = SkeletonBatchDraft.model_validate(repaired).model_dump(mode="json")
    except ValidationError as error:
        raise ValueError("첫 Skeleton 출력에 최소 길이 외의 계약 불일치가 있습니다.") from error
    if normalized != repaired:
        raise ValueError("첫 Skeleton 출력이 구 provider strict schema의 전체 필드를 보존하지 않았습니다.")
    summary = receipt.error_summary or ""
    if any(field_name not in summary for _, _, field_name in locations):
        raise ValueError("Skeleton 실패 receipt가 terminal의 Core 최소 길이 불일치와 다릅니다.")


def _load_planning_resume(original: Path, run_root: Path, scenario: Any, roles_bytes: bytes, codex_bin: Path):
    """확정된 Goal 뒤의 첫 Skeleton schema 실패를 새 계약에서 한 번 이어간다."""
    if not original.is_absolute() or not original.is_dir() or run_root.resolve().is_relative_to(original.resolve()):
        raise ValueError("후속 planning은 존재하는 원본 밖의 새 절대 실행 경로여야 합니다.")
    original_files = _project_files(original)
    preflight = json.loads((original / "preflight.json").read_text(encoding="utf-8"))
    summary = json.loads((original / "summary.json").read_text(encoding="utf-8"))
    previous = GoalPreparationOutcome.model_validate_json((original / "goal-preparation.json").read_text(encoding="utf-8"))
    if (summary["status"] != "FAIL" or summary.get("error_type") != "StructuredRoleError"
            or previous.goal_contract.status is not RevisionStatus.READY
            or preflight.get("resume_source", {}).get("resume_mode") == "planning"
            or (original / "planning-outcome.json").exists()):
        raise ValueError("READY Goal 뒤의 첫 schema 실패만 별도 planning으로 이어갈 수 있습니다.")
    if (scenario.source_request != previous.goal_contract.definition.source_request
            or preflight["role_config_digest"] != sha256_bytes(roles_bytes)
            or preflight["codex_bin_digest"] != sha256_bytes(codex_bin.read_bytes())):
        raise ValueError("후속 planning의 원문·역할·실행 파일이 다릅니다.")
    calls = sorted((original / "calls").iterdir())
    if len(calls) != 3:
        raise ValueError("Goal 수정·검토 뒤 첫 Skeleton 실패의 세 호출만 허용합니다.")
    successful: list[tuple[RoleCallRequest, RoleCallResult]] = []
    for directory, role in zip(calls[:2], ("goal_refiner", "goal_reviewer"), strict=True):
        successful.append(_load_completed_role(directory, role))
    from flowmarshal.engine.goal_feedback import (
        GoalRefinementOutcome,
        goal_refinement_evidence_catalog,
    )
    refinement = GoalRefinementOutcome.model_validate_json((original / "goal-refinement.json").read_text(encoding="utf-8"))
    ancestor = preflight.get("resume_source", {})
    ancestor_root = Path(ancestor["run_root"])
    ancestor_calls = sorted((ancestor_root / "calls").iterdir())
    if (preflight.get("prior_provider_calls") != 2 or ancestor.get("prior_provider_calls") != 2
            or ancestor.get("outcome_digest") != sha256_digest(refinement.original_outcome)
            or _project_files(ancestor_root) != ancestor["files"]
            or len(ancestor_calls) != 2):
        raise ValueError("원본 Goal 두 호출의 계보·artifact·예산 결속이 다릅니다.")
    ancestor_outcome = GoalPreparationOutcome.model_validate_json(
        (ancestor_root / "goal-preparation.json").read_text(encoding="utf-8")
    )
    ancestor_normalizer_request = RoleCallRequest.model_validate_json(
        (ancestor_calls[0] / "request.json").read_text(encoding="utf-8")
    )
    observed_facts = tuple(ancestor_normalizer_request.payload["observed_facts"])
    actual_review = ReviewDraft.model_validate(successful[1][1].payload)
    if (refinement.original_outcome != ancestor_outcome
            or refinement.revised_outcome != previous
            or refinement.refiner_receipt != successful[0][1].receipt
            or previous.reviewer_receipt != successful[1][1].receipt
            or previous.review.ratings != actual_review.ratings
            or previous.review.findings or actual_review.findings):
        raise ValueError("READY Goal이 실제 수정·독립 검토 결과와 다릅니다.")
    failed_request = RoleCallRequest.model_validate_json((calls[-1] / "request.json").read_text(encoding="utf-8"))
    failure = json.loads((calls[-1] / "failed.json").read_text(encoding="utf-8"))
    failed_receipt = RoleCallReceipt.model_validate(failure["receipts"][-1])
    terminal = RuntimeObservation.model_validate_json((calls[-1] / "terminal.json").read_text(encoding="utf-8"))
    if (failed_request.role != "skeleton_generator" or failed_receipt.role != failed_request.role
            or failed_receipt.status != "schema_failed" or failed_receipt.schema_recovery_attempts != 0
            or failed_receipt.input_digest != failed_request.request_digest
            or failed_receipt.output_schema_digest != sha256_digest(strict_json_output_schema(failed_request.output_schema))
            or terminal.active or terminal.terminal_status != "completed"
            or terminal.thread_id != failed_receipt.thread_id
            or tuple(failed_receipt.turn_ids) != (terminal.turn_id,)):
        raise ValueError("완료·귀속이 확인된 첫 Skeleton schema 실패가 아닙니다.")
    verify_binding(failed_request.operational_binding, failed_receipt.observed_binding.inventory,
                   role=failed_request.role, model=failed_receipt.model, effort=failed_receipt.effort)
    _validate_skeleton_contract_failure(failed_request, failed_receipt, terminal)
    target = Path(preflight["project_root"]).resolve(strict=True)
    if _project_files(target) != preflight["project_files"]:
        raise ValueError("READY Goal의 대상 프로젝트가 변경됐습니다.")
    shutil.copytree(original / "ledger", run_root / "ledger")
    ledger = SQLiteEngineLedger(run_root / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=run_root / "ledger" / "artifacts")
    with ledger.read() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (previous.goal_contract.definition.project_id,)).fetchone()
        goal_row = connection.execute("SELECT payload_json FROM goal_revisions WHERE id = ?", (project["active_goal_revision_id"],)).fetchone()
        profile_row = connection.execute("SELECT payload_json FROM profile_revisions WHERE id = ?", (project["active_profile_revision_id"],)).fetchone()
        profile = ProjectProfileRevision.model_validate_json(profile_row["payload_json"])
        if (goal_row["payload_json"] != canonical_json(previous.goal_contract)
                or Path(project["root"]).resolve() != target
                or connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0] != 0
                or connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0] != 0):
            raise ValueError("원장에 같은 READY Goal만 있고 Plan 실행 이력이 없어야 합니다.")
    refiner_request, _ = successful[0]
    reviewer_request, _ = successful[1]
    expected_refiner_payload = {
        "feedback_contract": "bounded-goal-feedback-v1",
        "source_outcome_digest": sha256_digest(refinement.original_outcome),
        "evidence_catalog": goal_refinement_evidence_catalog(
            previous=refinement.original_outcome,
            profile=profile,
            observed_facts=observed_facts,
        ),
        "findings": [
            finding.model_dump(mode="json")
            for finding in refinement.original_outcome.review.findings
        ],
    }
    expected_reviewer_payload = {
        "case_ref": "goal-review",
        "evidence_catalog": goal_review_evidence_catalog(
            source_request=previous.goal_contract.definition.source_request,
            profile=profile,
            proposal=previous.proposal,
            observed_facts=observed_facts,
        ),
    }
    if (
        ancestor_normalizer_request.role != "goal_normalizer"
        or ancestor_normalizer_request.payload["source_request"]
        != refinement.original_outcome.goal_contract.definition.source_request
        or ancestor_normalizer_request.payload["project_profile"]
        != profile.definition.model_dump(mode="json")
        or Path(ancestor_normalizer_request.cwd).resolve() != target
        or refiner_request.payload != expected_refiner_payload
        or reviewer_request.payload != expected_reviewer_payload
        or Path(refiner_request.cwd).resolve() != target
        or Path(reviewer_request.cwd).resolve() != target
    ):
        raise ValueError("Goal 수정·독립 검토 request가 원본 outcome·Profile·관측과 다릅니다.")
    if _project_files(original) != original_files:
        raise ValueError("원본 planning artifact가 복제 중 변경됐습니다.")
    prior_calls = preflight.get("prior_provider_calls", 0) + len(calls)
    if prior_calls >= MAXIMUM_CALLS:
        raise ValueError("원본 계보의 전체 provider 호출 예산이 소진됐습니다.")
    source = {"run_root": str(original.resolve()), "files": original_files,
              "outcome_digest": sha256_digest(previous), "prior_provider_calls": prior_calls,
              "resume_mode": "planning", "failed_request_digest": failed_request.request_digest,
              "failed_output_schema_digest": failed_receipt.output_schema_digest}
    return previous, profile, target, (), source


def _role_checkpoint_entry(
    request: RoleCallRequest,
    receipt: RoleCallReceipt,
    goal_contract_digest: str,
) -> dict[str, Any]:
    return {
        "request": request.model_dump(mode="json"),
        "receipt": receipt.model_dump(mode="json"),
        "goal_contract_digest": goal_contract_digest,
    }


def _load_prior_role_lineage(
    *, preflight: dict[str, Any], previous: GoalPreparationOutcome,
    profile: ProjectProfileRevision, target: Path,
) -> tuple[tuple[RoleCallRequest, ...], tuple[RoleCallReceipt, ...], tuple[dict[str, Any], ...]]:
    """v1 Goal 2회와 v2 Goal 복구·첫 Skeleton 3회의 실제 결속을 검증한다."""
    from flowmarshal.engine.goal_feedback import (
        GoalRefinementOutcome,
        goal_refinement_evidence_catalog,
    )

    ancestor = preflight.get("resume_source", {})
    ancestor_root = Path(ancestor.get("run_root", ""))
    if (
        ancestor.get("resume_mode") != "planning"
        or ancestor.get("prior_provider_calls") != 5
        or not ancestor_root.is_absolute()
        or not ancestor_root.is_dir()
        or _artifact_files(ancestor_root) != ancestor.get("files")
    ):
        raise ValueError("v2 Goal 복구 실행의 artifact·5호출 계보가 다릅니다.")
    ancestor_preflight = json.loads(
        (ancestor_root / "preflight.json").read_text(encoding="utf-8")
    )
    ancestor_summary = json.loads(
        (ancestor_root / "summary.json").read_text(encoding="utf-8")
    )
    ancestor_prepared = GoalPreparationOutcome.model_validate_json(
        (ancestor_root / "goal-preparation.json").read_text(encoding="utf-8")
    )
    refinement = GoalRefinementOutcome.model_validate_json(
        (ancestor_root / "goal-refinement.json").read_text(encoding="utf-8")
    )
    ancestor_calls = sorted((ancestor_root / "calls").iterdir())
    if (
        ancestor.get("outcome_digest") != sha256_digest(previous)
        or ancestor_summary.get("status") != "FAIL"
        or ancestor_summary.get("error_type") != "StructuredRoleError"
        or ancestor_prepared != previous
        or refinement.revised_outcome != previous
        or ancestor_preflight.get("prior_provider_calls") != 2
        or ancestor_preflight.get("maximum_provider_calls") != 12
        or len(ancestor_calls) != 3
    ):
        raise ValueError("v2 READY Goal·schema 실패·호출 예산 결속이 다릅니다.")

    refiner_request, refiner_result = _load_completed_role(
        ancestor_calls[0], "goal_refiner"
    )
    reviewer_request, reviewer_result = _load_completed_role(
        ancestor_calls[1], "goal_reviewer"
    )
    failed_request = RoleCallRequest.model_validate_json(
        (ancestor_calls[2] / "request.json").read_text(encoding="utf-8")
    )
    failure = json.loads(
        (ancestor_calls[2] / "failed.json").read_text(encoding="utf-8")
    )
    failed_receipt = RoleCallReceipt.model_validate(failure["receipts"][-1])
    failed_terminal = RuntimeObservation.model_validate_json(
        (ancestor_calls[2] / "terminal.json").read_text(encoding="utf-8")
    )
    if (
        tuple(RoleCallReceipt.model_validate(item) for item in failure["receipts"][:-1])
        != (refiner_result.receipt, reviewer_result.receipt)
        or failed_request.role != "skeleton_generator"
        or failed_receipt.role != failed_request.role
        or failed_receipt.status != "schema_failed"
        or failed_receipt.schema_recovery_attempts != 0
        or failed_receipt.input_digest != failed_request.request_digest
        or failed_receipt.output_schema_digest
        != sha256_digest(strict_json_output_schema(failed_request.output_schema))
        or failed_terminal.active
        or failed_terminal.terminal_status != "completed"
        or failed_terminal.thread_id != failed_receipt.thread_id
        or tuple(failed_receipt.turn_ids) != (failed_terminal.turn_id,)
    ):
        raise ValueError("v2 첫 Skeleton schema 실패 receipt·terminal 결속이 다릅니다.")
    _validate_skeleton_contract_failure(failed_request, failed_receipt, failed_terminal)

    original = ancestor_preflight.get("resume_source", {})
    original_root = Path(original.get("run_root", ""))
    original_calls = (
        sorted((original_root / "calls").iterdir())
        if original_root.is_absolute() and original_root.is_dir()
        else []
    )
    if (
        original.get("prior_provider_calls") != 2
        or ancestor_preflight.get("prior_provider_calls") != 2
        or original.get("outcome_digest") != sha256_digest(refinement.original_outcome)
        or _artifact_files(original_root) != original.get("files")
        or len(original_calls) != 2
    ):
        raise ValueError("v1 Goal 두 호출의 artifact·예산 계보가 다릅니다.")
    normalizer_request, normalizer_result = _load_completed_role(
        original_calls[0], "goal_normalizer"
    )
    original_reviewer_request, original_reviewer_result = _load_completed_role(
        original_calls[1], "goal_reviewer"
    )
    original_outcome = GoalPreparationOutcome.model_validate_json(
        (original_root / "goal-preparation.json").read_text(encoding="utf-8")
    )
    if (
        original_outcome != refinement.original_outcome
        or original_outcome.normalizer_receipt != normalizer_result.receipt
        or original_outcome.reviewer_receipt != original_reviewer_result.receipt
        or refinement.refiner_receipt != refiner_result.receipt
        or previous.reviewer_receipt != reviewer_result.receipt
        or Path(normalizer_request.cwd).resolve() != target
        or Path(refiner_request.cwd).resolve() != target
        or Path(reviewer_request.cwd).resolve() != target
        or Path(failed_request.cwd).resolve() != target
    ):
        raise ValueError("v1→v2 Goal revision과 실제 역할 receipt 결속이 다릅니다.")
    observed_facts = tuple(normalizer_request.payload["observed_facts"])
    expected_refiner_payload = {
        "feedback_contract": "bounded-goal-feedback-v1",
        "source_outcome_digest": sha256_digest(refinement.original_outcome),
        "evidence_catalog": goal_refinement_evidence_catalog(
            previous=refinement.original_outcome,
            profile=profile,
            observed_facts=observed_facts,
        ),
        "findings": [
            finding.model_dump(mode="json")
            for finding in refinement.original_outcome.review.findings
        ],
    }
    expected_reviewer_payload = {
        "case_ref": "goal-review",
        "evidence_catalog": goal_review_evidence_catalog(
            source_request=previous.goal_contract.definition.source_request,
            profile=profile,
            proposal=previous.proposal,
            observed_facts=observed_facts,
        ),
    }
    if (
        normalizer_request.payload.get("project_profile")
        != profile.definition.model_dump(mode="json")
        or refiner_request.payload != expected_refiner_payload
        or reviewer_request.payload != expected_reviewer_payload
        or failed_request.payload.get("goal")
        != previous.goal_contract.definition.model_dump(mode="json")
    ):
        raise ValueError("v2 Goal 복구·Skeleton request가 Goal 계보와 다릅니다.")

    revision_one = refinement.original_outcome.goal_contract.definition_digest
    revision_two = previous.goal_contract.definition_digest
    requests = (
        normalizer_request, original_reviewer_request,
        refiner_request, reviewer_request, failed_request,
    )
    receipts = (
        normalizer_result.receipt, original_reviewer_result.receipt,
        refiner_result.receipt, reviewer_result.receipt, failed_receipt,
    )
    entries = tuple(
        _role_checkpoint_entry(request, receipt, digest)
        for request, receipt, digest in zip(
            requests, receipts,
            (revision_one, revision_one, revision_one, revision_two, revision_two),
            strict=True,
        )
    )
    return requests, receipts, entries


def _load_planning_continuation(
    original: Path,
    run_root: Path,
    scenario: Any,
    roles_bytes: bytes,
    codex_bin: Path,
    runtime: Any,
    timeout_policy: RoleTimeoutPolicy,
):
    """timeout된 상세 Plan review를 terminal 확인 뒤 저장 checkpoint에서 잇는다."""
    if (
        not original.is_absolute()
        or not original.is_dir()
        or run_root.resolve().is_relative_to(original.resolve())
    ):
        raise ValueError("planning continuation은 원본 밖의 새 절대 실행 경로여야 합니다.")
    original_files = _artifact_files(original)
    preflight = json.loads((original / "preflight.json").read_text(encoding="utf-8"))
    summary = json.loads((original / "summary.json").read_text(encoding="utf-8"))
    previous = GoalPreparationOutcome.model_validate_json(
        (original / "goal-preparation.json").read_text(encoding="utf-8")
    )
    if (
        summary.get("status") != "FAIL"
        or summary.get("error_type") != "StructuredRoleError"
        or summary.get("error") != "role turn timeout"
        or previous.goal_contract.status is not RevisionStatus.READY
        or (original / "planning-outcome.json").exists()
        or (original / "selected-plan.json").exists()
    ):
        raise ValueError("READY Goal의 미완료 Plan reviewer timeout만 이어갈 수 있습니다.")
    if (
        scenario.source_request != previous.goal_contract.definition.source_request
        or preflight["role_config_digest"] != sha256_bytes(roles_bytes)
        or preflight["codex_bin_digest"] != sha256_bytes(codex_bin.read_bytes())
    ):
        raise ValueError("planning continuation의 원문·역할·실행 파일이 다릅니다.")
    calls = sorted((original / "calls").iterdir())
    if len(calls) != 4:
        raise ValueError("저장 checkpoint는 성공 세 호출과 timeout review 한 호출이어야 합니다.")
    expected_roles = (
        "skeleton_generator", "skeleton_reviewer", "plan_expander"
    )
    successful = tuple(
        _load_completed_role(directory, role)
        for directory, role in zip(calls[:3], expected_roles, strict=True)
    )
    failed_request = RoleCallRequest.model_validate_json(
        (calls[3] / "request.json").read_text(encoding="utf-8")
    )
    failure = json.loads((calls[3] / "failed.json").read_text(encoding="utf-8"))
    failure_receipts = tuple(RoleCallReceipt.model_validate(item) for item in failure["receipts"])
    failed_receipt = failure_receipts[-1]
    source_receipts = tuple(item[1].receipt for item in successful)
    if (
        failure.get("error") != "role turn timeout"
        or failure_receipts[:-1] != source_receipts
        or failed_request.role != "compact_plan_reviewer"
        or failed_receipt.role != failed_request.role
        or failed_receipt.status != "timed_out"
        or failed_receipt.input_digest != failed_request.request_digest
        or failed_receipt.output_schema_digest
        != sha256_digest(strict_json_output_schema(failed_request.output_schema))
        or failed_receipt.schema_recovery_attempts != 0
        or len(failed_receipt.turn_ids) != 1
        or failed_request.timeout_seconds != 900
        or failed_request.timeout_policy_digest is not None
        or failed_receipt.timeout_policy_digest is not None
    ):
        raise ValueError("900초에 끝난 원본 compact Plan review 결속이 다릅니다.")

    # 외부 상태를 바꾸거나 새 provider call을 만들기 전에 기존 turn을 먼저 관측한다.
    terminal = runtime.read(thread_id=failed_receipt.thread_id)
    _write_new(run_root / "source-terminal-observation.json", terminal)
    if (
        terminal.active
        or terminal.terminal_status != "interrupted"
        or terminal.thread_id != failed_receipt.thread_id
        or terminal.turn_id != failed_receipt.turn_ids[0]
        or terminal.final_response is not None
    ):
        raise ValueError("timeout turn의 사후 interrupted terminal이 확정되지 않았습니다.")

    override = timeout_policy.override_for(failed_request.role)
    if (
        override is None
        or override.replaces_timeout_seconds != failed_request.timeout_seconds
        or override.timeout_seconds <= failed_request.timeout_seconds
    ):
        raise ValueError("같은 reviewer를 잇기 위한 명시적 상향 timeout 변경 근거가 없습니다.")

    generator_request, generator_result = successful[0]
    skeleton_request, skeleton_result = successful[1]
    expander_request, expander_result = successful[2]
    generator_batch = SkeletonBatchDraft.model_validate(generator_result.payload)
    if len(generator_batch.candidates) != 1:
        raise ValueError("완료된 Skeleton 결과가 단일 후보가 아닙니다.")
    try:
        skeleton_catalog = skeleton_request.payload["evidence_catalog"]
        candidate = PlanSkeletonCandidate.model_validate(
            skeleton_catalog["artifact:skeleton"]
        )
    except (KeyError, ValidationError) as error:
        raise ValueError("저장된 Skeleton review 입력에 완전한 후보가 없습니다.") from error
    if (
        generator_request.role != "skeleton_generator"
        or expander_request.payload.get("skeleton") != candidate.model_dump(mode="json")
    ):
        raise ValueError("저장된 Skeleton과 상세화 request가 다릅니다.")
    skeleton_submission = _review_submission(
        role="skeleton_reviewer", artifact_digest=sha256_digest(candidate),
        evidence_catalog=skeleton_catalog,
        draft=ReviewDraft.model_validate(skeleton_result.payload),
    )
    validate_reviewer_submission_evidence(
        skeleton_submission, evidence_catalog=skeleton_catalog,
        known_task_refs={item.task_ref for item in candidate.tasks},
    )
    if skeleton_submission.findings:
        raise ValueError("저장된 Skeleton이 admissible review 결과가 아닙니다.")
    PlanExpansionEnvelopeV2.model_validate(expander_result.payload)
    try:
        review_catalog = failed_request.payload["evidence_catalog"]
        plan = PlanContractRevision.model_validate(review_catalog["artifact:plan_contract"])
    except (KeyError, ValidationError) as error:
        raise ValueError("timeout review에 완전한 상세 Plan 후보가 없습니다.") from error
    if (
        review_catalog.get("source:goal")
        != previous.goal_contract.definition.model_dump(mode="json")
        or plan.definition.source_skeleton_digest != sha256_digest(candidate)
        or failed_request.payload.get("case_ref")
        != "case-" + plan.activation_digest.split(":", 1)[1][:16]
        or plan.definition.planning_budget.max_logical_role_calls != 9
    ):
        raise ValueError("timeout review가 저장된 Goal·Skeleton·상세 Plan과 다릅니다.")

    target = Path(preflight["project_root"]).resolve(strict=True)
    if _project_files(target) != preflight["project_files"]:
        raise ValueError("continuation 대상 프로젝트가 변경됐습니다.")
    prior_base = preflight.get("prior_provider_calls")
    if prior_base != 5 or preflight.get("maximum_provider_calls") != 9:
        raise ValueError("원본 5호출과 현재 4호출의 예산 계보가 다릅니다.")
    prior_calls = prior_base + len(calls)
    if prior_calls != 9 or MAXIMUM_CALLS - prior_calls != 5:
        raise ValueError("planning continuation의 잔여 provider 호출 예산이 5가 아닙니다.")

    # 구 schema DB를 새 Engine DB로 복사하거나 제자리 migration하지 않는다.
    # 읽기 전용 provenance 확인 뒤 run()이 새 DB에 명시적으로 등록한다.
    source_db = original / "ledger" / "flowmarshal-engine.sqlite3"
    connection = sqlite3.connect(
        source_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        metadata = dict(connection.execute("SELECT key, value FROM schema_meta"))
        if (
            metadata != {"schema_id": ENGINE_SCHEMA_ID, "schema_revision": "2"}
            or connection.execute("PRAGMA application_id").fetchone()[0]
            != SQLITE_APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 2
        ):
            raise ValueError("원본은 읽기 전용 Engine schema revision 2 원장이어야 합니다.")
        project = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (previous.goal_contract.definition.project_id,),
        ).fetchone()
        if project is None:
            raise ValueError("원본 원장에 checkpoint Project가 없습니다.")
        goal_row = connection.execute(
            "SELECT payload_json FROM goal_revisions WHERE id = ?",
            (project["active_goal_revision_id"],),
        ).fetchone()
        profile_row = connection.execute(
            "SELECT payload_json FROM profile_revisions WHERE id = ?",
            (project["active_profile_revision_id"],),
        ).fetchone()
        goal_revisions = tuple(
            GoalContractRevision.model_validate_json(row["payload_json"])
            for row in connection.execute(
                "SELECT payload_json FROM goal_revisions WHERE goal_id = ? ORDER BY revision_no",
                (previous.goal_contract.goal_id,),
            ).fetchall()
        )
        state_row = connection.execute(
            "SELECT payload_json FROM state_snapshots WHERE project_id = ? AND is_current = 1",
            (previous.goal_contract.definition.project_id,),
        ).fetchone()
        map_row = connection.execute(
            "SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
            (previous.goal_contract.definition.project_id,),
        ).fetchone()
        if (
            goal_row is None
            or profile_row is None
            or state_row is None
            or map_row is None
        ):
            raise ValueError("원본 원장에 활성 Profile·Goal·State·Project Map이 없습니다.")
        profile = ProjectProfileRevision.model_validate_json(profile_row["payload_json"])
        state = StateSnapshot.model_validate_json(state_row["payload_json"])
        project_map = ProjectMapRevision.model_validate_json(map_row["payload_json"])
        if (
            goal_row["payload_json"] != canonical_json(previous.goal_contract)
            or not goal_revisions
            or goal_revisions[-1] != previous.goal_contract
            or Path(project["root"]).resolve() != target
            or connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0] != 0
            or connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0] != 0
        ):
            raise ValueError("원장에 같은 READY Goal만 있고 Plan 이력이 없어야 합니다.")
    finally:
        connection.close()
    if (
        state.snapshot_digest != plan.definition.base_state_snapshot_digest
        or project_map.revision_digest != plan.definition.project_map_digest
        or state.semantic_digest != candidate.state_signature
        or _project_files(Path(project_map.root).resolve(strict=True)) != preflight["project_files"]
    ):
        raise ValueError("저장된 State·Project Map이 Skeleton·Plan·현재 파일과 다릅니다.")
    if _artifact_files(original) != original_files:
        raise ValueError("원본 planning artifact가 continuation 준비 중 변경됐습니다.")
    prior_requests, prior_receipts, prior_entries = _load_prior_role_lineage(
        preflight=preflight, previous=previous, profile=profile, target=target,
    )
    source_requests = prior_requests + tuple(item[0] for item in successful) + (failed_request,)
    source_receipts = prior_receipts + source_receipts + (failed_receipt,)
    import_entries = prior_entries + tuple(
        _role_checkpoint_entry(
            request, result.receipt, previous.goal_contract.definition_digest,
        )
        for request, result in successful
    ) + (
        _role_checkpoint_entry(
            failed_request, failed_receipt,
            previous.goal_contract.definition_digest,
        ),
    )
    if len(source_requests) != 9 or len(source_receipts) != 9 or len(import_entries) != 9:
        raise ValueError("원본 Goal 5호출과 v3 planning 4호출 계보가 다릅니다.")
    checkpoint = StoredPlanningCheckpoint(
        profile=profile, goal_revisions=goal_revisions, state=state,
        project_map=project_map, candidate=candidate,
        skeleton_submission=skeleton_submission, plan=plan,
        source_requests=source_requests, source_receipts=source_receipts,
        timed_out_request=failed_request, timed_out_receipt=failed_receipt,
        terminal_observation=terminal, import_entries=import_entries,
    )
    checkpoint_digest = sha256_digest({
        "candidate": candidate, "plan": plan,
        "source_requests": source_requests,
        "source_receipts": source_receipts,
        "timed_out_receipt": failed_receipt,
        "terminal_observation": terminal,
    })
    source = {
        "run_root": str(original.resolve()), "files": original_files,
        "outcome_digest": checkpoint_digest, "claim_digest": checkpoint_digest,
        "resume_mode": "plan_review_timeout_continuation",
        "prior_provider_calls": prior_calls,
        "remaining_provider_calls": 5,
        "planner_logical_budget": plan.definition.planning_budget.max_logical_role_calls,
        "failed_request_digest": failed_request.request_digest,
        "failed_receipt_digest": sha256_digest(failed_receipt),
        "terminal_observation_digest": sha256_digest(terminal),
        "timeout_policy_transition": {
            "source_timeout_seconds": failed_request.timeout_seconds,
            "new_timeout_seconds": override.timeout_seconds,
            "reason": override.reason,
            "new_policy_digest": timeout_policy.policy_digest,
        },
    }
    return previous, profile, target, (), source, checkpoint


def _load_preflight_retry(
    failed_run: Path,
    run_root: Path,
    *,
    scenario: Any,
    roles_bytes: bytes,
    codex_bin: Path,
    timeout_policy: RoleTimeoutPolicy,
    timeout_policy_bytes: bytes,
    budget_policy: GoalBudgetPolicy,
    budget_policy_bytes: bytes,
    adjustment: dict[str, Any],
    adjustment_bytes: bytes,
) -> dict[str, Any]:
    """provider 효과 전 실패한 continuation 예약만 해제하고 새 run provenance를 만든다."""
    if (
        not failed_run.is_absolute()
        or not failed_run.is_dir()
        or run_root.resolve().is_relative_to(failed_run.resolve())
    ):
        raise ValueError("--retry-preflight-run은 새 실행 밖의 기존 절대 경로여야 합니다.")
    required = (
        "summary.json", "preflight.json", "goal-preparation.json",
        "imported-role-checkpoint.json", "source-terminal-observation.json",
        "inputs/role-config.json", "inputs/role-timeout-policy.json",
        "inputs/budget-policy.json", "inputs/timeout-usage-adjustment.json",
        "ledger/flowmarshal-engine.sqlite3",
    )
    if any(not (failed_run / relative).is_file() for relative in required):
        raise ValueError("pre-call 실패 continuation의 필수 artifact가 없습니다.")
    protected = {
        relative: sha256_bytes((failed_run / relative).read_bytes())
        for relative in required if not relative.startswith("ledger/")
    }
    summary = json.loads((failed_run / "summary.json").read_text(encoding="utf-8"))
    failed_preflight_bytes = (failed_run / "preflight.json").read_bytes()
    preflight = json.loads(failed_preflight_bytes.decode("utf-8"))
    imported_checkpoint = json.loads(
        (failed_run / "imported-role-checkpoint.json").read_text(encoding="utf-8")
    )
    if (
        summary.get("status") != "FAIL"
        or summary.get("error_type") != "RuntimeError"
        or summary.get("error") != "GOAL_REPAIR_ORIGINAL_ARTIFACT_CHANGED"
        or summary.get("plan_activated") is not False
        or summary.get("worker_executed") is not False
    ):
        raise ValueError("허용된 continuation pre-call 실패 summary가 아닙니다.")
    lock_without_digest = {key: value for key, value in preflight.items() if key != "lock_digest"}
    if preflight.get("lock_digest") != sha256_digest(lock_without_digest):
        raise ValueError("실패 run의 preflight lock digest가 다릅니다.")
    source = preflight.get("resume_source")
    if (
        not isinstance(source, dict)
        or source.get("resume_mode") != "plan_review_timeout_continuation"
        or preflight.get("scenario", {}).get("scenario_id") != scenario.scenario_id
        or preflight.get("prior_provider_calls") != 9
        or preflight.get("maximum_provider_calls") != 5
        or preflight.get("planning_role_budget") != 9
    ):
        raise ValueError("실패 run이 원본 9호출·잔여 5호출 continuation에 결속되지 않았습니다.")
    source_root = Path(source["run_root"])
    if (
        not source_root.is_absolute()
        or not source_root.is_dir()
        or _artifact_files(source_root) != source.get("files")
    ):
        raise ValueError("실패 run의 원본 planning artifact 결속이 다릅니다.")
    if (
        preflight.get("codex_bin_digest") != sha256_bytes(codex_bin.read_bytes())
        or preflight.get("role_config_digest") != sha256_bytes(roles_bytes)
        or (failed_run / "inputs" / "role-config.json").read_bytes() != roles_bytes
        or preflight.get("role_timeout_policy_digest") != timeout_policy.policy_digest
        or preflight.get("role_timeout_policy_bytes_digest") != sha256_bytes(timeout_policy_bytes)
        or RoleTimeoutPolicy.model_validate_json(
            (failed_run / "inputs" / "role-timeout-policy.json").read_text(encoding="utf-8")
        ) != timeout_policy
        or preflight.get("budget_policy") != budget_policy.model_dump(mode="json")
        or preflight.get("budget_policy_bytes_digest") != sha256_bytes(budget_policy_bytes)
        or (failed_run / "inputs" / "budget-policy.json").read_bytes() != budget_policy_bytes
        or preflight.get("timeout_usage_adjustment_bytes_digest") != sha256_bytes(adjustment_bytes)
        or (failed_run / "inputs" / "timeout-usage-adjustment.json").read_bytes() != adjustment_bytes
        or adjustment.get("charge_tokens") != budget_policy.call_reservation_tokens
    ):
        raise ValueError("실패 run과 재시도의 executable·role·timeout·budget·조정 입력이 다릅니다.")
    calls_root = failed_run / "calls"
    lifecycle_names = {
        "thread.intent.json", "thread.receipt.json", "turn.intent.json",
        "turn.receipt.json", "terminal.json", "interrupt.intent.json",
        "interrupt.receipt.json",
    }
    if calls_root.exists() or any(path.name in lifecycle_names for path in failed_run.rglob("*.json")):
        raise ValueError("실패 run에 provider call/thread/turn 효과 artifact가 있습니다.")

    claim_digest = source.get("claim_digest")
    if not isinstance(claim_digest, str) or not claim_digest.startswith("sha256:"):
        raise ValueError("원본 continuation claim digest가 없습니다.")
    claim = source_root.parent / "planning-feedback-claims" / (claim_digest[7:] + ".json")
    claim_bytes = claim.read_bytes()
    claim_value = json.loads(claim_bytes.decode("utf-8"))
    if claim_value != {
        "source_outcome_digest": source["outcome_digest"],
        "run_root": str(failed_run.resolve()),
        "preflight_digest": preflight["lock_digest"],
    }:
        raise ValueError("기존 claim이 실패 run과 정확히 연결되지 않았습니다.")
    followup_path = (
        claim.parent / (claim.stem + ".followups")
        / (preflight["lock_digest"][7:] + ".json")
    )
    if followup_path.exists():
        raise ValueError("PRECALL_RETRY_ALREADY_CLAIMED: 이 실패 run은 이미 후속 continuation이 있습니다.")

    ledger = SQLiteEngineLedger(
        failed_run / "ledger" / "flowmarshal-engine.sqlite3",
        artifact_root=failed_run / "ledger" / "artifacts",
    )
    with ledger.read() as connection:
        rows = tuple(connection.execute("SELECT * FROM provider_calls ORDER BY rowid"))
        histories = tuple(connection.execute(
            "SELECT event_type,entity_id,payload_json FROM history_events ORDER BY sequence"
        ))
        adjustments = tuple(connection.execute(
            "SELECT a.*,c.status AS call_status FROM budget_adjustments a "
            "JOIN provider_calls c ON c.id=a.call_id"
        ))
    pending = tuple(row for row in rows if row["status"] in {"reserved", "released"})
    imported = tuple(row for row in rows if row["status"] in {"settled", "usage_unknown"})
    imported_history = tuple(row for row in histories if row["event_type"] == "budget.checkpoint_call_imported")
    if (
        len(rows) != 10
        or len(imported) != 9
        or sum(row["status"] == "settled" for row in imported) != 8
        or sum(row["status"] == "usage_unknown" for row in imported) != 1
        or len(imported_history) != 9
        or imported_checkpoint.get("provider_calls") != 9
        or tuple(imported_checkpoint.get("provider_call_ids", ()))
        != tuple(row["id"] for row in imported)
        or imported_checkpoint.get("source_digest") != source["outcome_digest"]
        or imported_checkpoint.get("remaining_provider_calls") != 5
        or len(pending) != 1
        or len(adjustments) != 1
        or adjustments[0]["call_status"] != "usage_unknown"
        or adjustments[0]["call_id"] != imported[-1]["id"]
        or adjustments[0]["charge_tokens"] != adjustment["charge_tokens"]
    ):
        raise ValueError("실패 원장의 원본 9호출·조정·효과 전 예약 계보가 다릅니다.")
    reserved = pending[0]
    reserved_request = RoleCallRequest.model_validate_json(reserved["request_json"])
    retry_override = timeout_policy.override_for("compact_plan_reviewer")
    if (
        retry_override is None
        or reserved["role"] != "compact_plan_reviewer"
        or reserved_request.role != reserved["role"]
        or reserved["request_digest"] != sha256_digest(reserved_request.model_dump(mode="json"))
        or reserved_request.timeout_policy_digest != timeout_policy.policy_digest
        or reserved_request.timeout_seconds != retry_override.timeout_seconds
        or reserved["goal_contract_digest"] is None
        or reserved["estimated_tokens"] != budget_policy.call_reservation_tokens
        or reserved["policy_digest"] != sha256_digest(budget_policy)
        or any(reserved[key] is not None for key in (
            "receipt_json", "usage_id", "attempt_id", "actual_tokens",
        ))
        or (reserved["status"] == "reserved" and reserved["completed_at"] is not None)
    ):
        raise ValueError("실패 예약에 provider 효과가 없다는 결속을 확인할 수 없습니다.")
    release_reason = "RETRY_PREFLIGHT_NO_PROVIDER_EFFECT: GOAL_REPAIR_ORIGINAL_ARTIFACT_CHANGED"
    released_already = reserved["status"] == "released"
    if released_already:
        matching_release = tuple(
            row for row in histories
            if row["event_type"] == "budget.released_before_effect"
            and row["entity_id"] == reserved["id"]
            and json.loads(row["payload_json"]).get("reason") == release_reason
        )
        if len(matching_release) != 1:
            raise ValueError("기존 released 예약의 no-effect History가 다릅니다.")
    else:
        BudgetManager(EngineService(ledger)).release_before_effect(
            reserved["id"], reason=release_reason,
        )
    with ledger.read() as connection:
        released = connection.execute(
            "SELECT status,receipt_json,usage_id,attempt_id FROM provider_calls WHERE id=?",
            (reserved["id"],),
        ).fetchone()
        release_count = connection.execute(
            "SELECT COUNT(*) FROM history_events WHERE event_type='budget.released_before_effect' "
            "AND entity_id=?", (reserved["id"],),
        ).fetchone()[0]
    if (
        released is None
        or released["status"] != "released"
        or any(released[key] is not None for key in ("receipt_json", "usage_id", "attempt_id"))
        or release_count != 1
        or any(
            sha256_bytes((failed_run / relative).read_bytes()) != digest
            for relative, digest in protected.items()
        )
        or claim.read_bytes() != claim_bytes
    ):
        raise RuntimeError("pre-call 예약 해제 중 원본 failure/claim 보존 검증에 실패했습니다.")
    return {
        "failed_run_root": str(failed_run.resolve()),
        "failed_summary_digest": sha256_digest(summary),
        "failed_preflight_digest": preflight["lock_digest"],
        "failed_preflight_file_digest": sha256_bytes(failed_preflight_bytes),
        "failed_protected_files": protected,
        "source_run_root": str(source_root.resolve()),
        "source_outcome_digest": source["outcome_digest"],
        "claim_digest": claim_digest,
        "claim_path": str(claim.resolve()),
        "claim_file_digest": sha256_bytes(claim_bytes),
        "claim_followup_path": str(followup_path.resolve()),
        "released_provider_call_id": reserved["id"],
        "release_reason": release_reason,
        "prior_provider_calls": 9,
        "remaining_provider_calls": 5,
        "timeout_adjustment_digest": sha256_bytes(adjustment_bytes),
    }


def _append_preflight_retry_claim(
    retry: dict[str, Any], resume_source: dict[str, Any],
    run_root: Path, lock: dict[str, Any],
) -> tuple[Path, str]:
    claim = Path(retry["claim_path"])
    if (
        resume_source.get("claim_digest") != retry["claim_digest"]
        or resume_source.get("outcome_digest") != retry["source_outcome_digest"]
        or sha256_bytes(claim.read_bytes()) != retry["claim_file_digest"]
    ):
        raise ValueError("기존 continuation claim이 재시도 준비 중 변경됐습니다.")
    followup = {
        "claim_digest": retry["claim_digest"],
        "source_outcome_digest": resume_source["outcome_digest"],
        "failed_run_root": retry["failed_run_root"],
        "failed_preflight_digest": retry["failed_preflight_digest"],
        "released_provider_call_id": retry["released_provider_call_id"],
        "run_root": str(run_root.resolve()),
        "preflight_digest": sha256_digest(lock),
    }
    followup_digest = sha256_digest(followup)
    followup_path = Path(retry["claim_followup_path"])
    expected_path = (
        claim.parent / (claim.stem + ".followups")
        / (retry["failed_preflight_digest"][7:] + ".json")
    ).resolve()
    if followup_path.resolve() != expected_path:
        raise ValueError("고정된 pre-call retry follow-up claim 경로가 다릅니다.")
    _write_new(followup_path, followup)
    return followup_path, followup_digest


def _review_markdown(*, scenario: Any, goal: Any, plan: Any, outcome: Any) -> str:
    tasks = "\n".join(
        f"- `{task.task_ref}`: {task.objective}\n"
        f"  - 검사: {'; '.join(item.statement for item in task.validations)}\n"
        f"  - 경로/대상: {'; '.join(task.consumes)}"
        for task in plan.definition.tasks
    )
    goal_checks = "\n".join(
        f"- `{item.validation_id}`: {item.statement}" for item in plan.definition.integration_validations
    )
    return (
        "# 사용자 활성화 검토용 Plan\n\n"
        f"- 시나리오: `{scenario.scenario_id}`\n"
        f"- 자연어 요청: {scenario.source_request}\n"
        f"- Goal digest: `{goal.definition_digest}`\n"
        f"- Plan revision ID: `{plan.plan_revision_id}`\n"
        f"- 활성화 digest: `{plan.activation_digest}`\n"
        f"- Planner 논리 역할 호출: {outcome.logical_role_calls}\n\n"
        "## Task\n\n"
        f"{tasks}\n\n"
        "## Goal 검사\n\n"
        f"{goal_checks}\n\n"
        "## 효과와 승인 범위\n\n"
        f"- 예상 효과: {'; '.join(plan.definition.expected_effects)}\n"
        f"- 금지 효과: {'; '.join(plan.definition.prohibited_effects)}\n"
        "- 이 문서가 보여 주는 정확한 `plan_revision_id`와 `activation_digest`만 사용자가 승인할 수 있다.\n"
        "- 이 driver는 Plan 활성화·Worker 실행·외부 효과를 수행하지 않았다.\n"
    )


def run(arguments: argparse.Namespace) -> int:
    root = project_root().resolve(strict=True)
    run_root = Path(arguments.run_root)
    if not run_root.is_absolute() or run_root.exists():
        raise ValueError("--run-root는 아직 존재하지 않는 절대 경로여야 합니다.")
    codex_bin = Path(arguments.codex_bin)
    role_config = Path(arguments.role_config)
    if not codex_bin.is_absolute() or not role_config.is_absolute():
        raise ValueError("--codex-bin과 --role-config는 절대 경로여야 합니다.")
    roles_bytes = role_config.read_bytes()
    roles = EngineRoleConfiguration.model_validate_json(roles_bytes.decode("utf-8"))
    timeout_policy_path_value = getattr(arguments, "role_timeout_policy", None)
    timeout_policy_path = None if timeout_policy_path_value is None else Path(timeout_policy_path_value)
    if timeout_policy_path is None:
        timeout_policy_bytes = None
        timeout_policy = RoleTimeoutPolicy()
    else:
        if not timeout_policy_path.is_absolute():
            raise ValueError("--role-timeout-policy는 절대 경로여야 합니다.")
        timeout_policy_bytes = timeout_policy_path.read_bytes()
        timeout_policy = RoleTimeoutPolicy.model_validate_json(timeout_policy_bytes.decode("utf-8"))
    budget_policy_path_value = getattr(arguments, "budget_policy", None)
    budget_policy_path = None if budget_policy_path_value is None else Path(budget_policy_path_value)
    budget_policy_bytes = None
    budget_policy = None
    if budget_policy_path is not None:
        if not budget_policy_path.is_absolute():
            raise ValueError("--budget-policy는 절대 경로여야 합니다.")
        budget_policy_bytes = budget_policy_path.read_bytes()
        budget_policy = GoalBudgetPolicy.model_validate_json(
            budget_policy_bytes.decode("utf-8")
        )
    adjustment_path_value = getattr(arguments, "timeout_usage_adjustment", None)
    adjustment_path = None if adjustment_path_value is None else Path(adjustment_path_value)
    adjustment_bytes = None
    adjustment = None
    if adjustment_path is not None:
        if not adjustment_path.is_absolute():
            raise ValueError("--timeout-usage-adjustment는 절대 경로여야 합니다.")
        adjustment_bytes = adjustment_path.read_bytes()
        adjustment = json.loads(adjustment_bytes.decode("utf-8"))
        if (
            set(adjustment) != {"source_receipt_digest", "charge_tokens", "reason"}
            or not isinstance(adjustment["charge_tokens"], int)
            or isinstance(adjustment["charge_tokens"], bool)
            or adjustment["charge_tokens"] < 0
            or not isinstance(adjustment["reason"], str)
            or not adjustment["reason"].strip()
        ):
            raise ValueError("timeout 사용량 조정 파일의 필드·값이 유효하지 않습니다.")
    catalog = PlanningScenarioCatalog.load(root / "tests" / "fixtures" / "engine" / "planning-scenarios.json")
    scenario = next((item for item in catalog.scenarios if item.scenario_id == arguments.scenario_id), None)
    if scenario is None:
        raise ValueError(f"scenario를 찾을 수 없습니다: {arguments.scenario_id}")
    fixture = root / "tests" / "fixtures" / "engine" / "live-smoke-project"
    run_root.mkdir(parents=True)
    started = time.monotonic()
    first_feasible: list[int] = []
    try:
        resume_path = getattr(arguments, "resume_goal_run", None)
        planning_resume_path = getattr(arguments, "resume_planning_run", None)
        continuation_path = getattr(arguments, "continue_planning_run", None)
        retry_preflight_path = getattr(arguments, "retry_preflight_run", None)
        if sum(value is not None for value in (
            resume_path, planning_resume_path, continuation_path, retry_preflight_path,
        )) > 1:
            raise ValueError("Goal 후속·schema 후속·timeout continuation·pre-call 재시도는 동시에 지정할 수 없습니다.")
        previous = resume_source = resume_profile = resume_facts = None
        checkpoint = None
        retry_preflight = None
        if retry_preflight_path is not None:
            if (
                timeout_policy_bytes is None
                or budget_policy is None
                or budget_policy_bytes is None
                or adjustment is None
                or adjustment_bytes is None
            ):
                raise ValueError(
                    "pre-call 재시도에는 기존과 같은 timeout·budget·사용량 조정 입력이 필요합니다."
                )
            retry_preflight = _load_preflight_retry(
                Path(retry_preflight_path), run_root,
                scenario=scenario, roles_bytes=roles_bytes, codex_bin=codex_bin,
                timeout_policy=timeout_policy, timeout_policy_bytes=timeout_policy_bytes,
                budget_policy=budget_policy, budget_policy_bytes=budget_policy_bytes,
                adjustment=adjustment, adjustment_bytes=adjustment_bytes,
            )
            continuation_path = retry_preflight["source_run_root"]
        if continuation_path is not None:
            if timeout_policy_path is None:
                raise ValueError("timeout continuation에는 --role-timeout-policy가 필요합니다.")
            if budget_policy is None:
                raise ValueError("timeout continuation에는 --budget-policy가 필요합니다.")
            target_project = _planning_continuation_target(Path(continuation_path))
        elif planning_resume_path is not None:
            previous, resume_profile, target_project, resume_facts, resume_source = _load_planning_resume(
                Path(planning_resume_path), run_root, scenario, roles_bytes, codex_bin,
            )
        elif resume_path is None:
            target_project = run_root / "project"
            shutil.copytree(fixture, target_project, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            previous, resume_profile, target_project, resume_facts, resume_source = _load_goal_resume(
                Path(resume_path), run_root, scenario, roles_bytes, codex_bin,
            )
        if continuation_path is None and (budget_policy is not None or adjustment is not None):
            raise ValueError("예산 정책·timeout 사용량 조정은 timeout continuation에만 적용됩니다.")
        workspace_binding = capture_workspace_binding(root)
        with CaptureRuntime(run_root=run_root, codex_bin=codex_bin) as runtime:
            policy = runtime.verify_execution_policy(target_project)
            inventory = runtime.list_models()
            roles.validate_inventory(inventory)
            binding = roles.operational_binding(inventory)
            if continuation_path is not None:
                previous, resume_profile, target_project, resume_facts, resume_source, checkpoint = _load_planning_continuation(
                    Path(continuation_path), run_root, scenario, roles_bytes, codex_bin,
                    runtime, timeout_policy,
                )
            prior_calls = 0 if resume_source is None else resume_source["prior_provider_calls"]
            planning_role_budget = (
                resume_source["planner_logical_budget"] if checkpoint is not None
                else MAXIMUM_CALLS - prior_calls - (0 if planning_resume_path else 2)
            )
            lock = {
                "scope": "single-live-planning-observation",
                "scenario": scenario, "workspace_binding": workspace_binding,
                "source_manifest": source_manifest_files(root),
                "project_root": str(target_project), "project_files": _project_files(target_project),
                "codex_bin": str(codex_bin.resolve(strict=True)),
                "codex_bin_digest": sha256_bytes(codex_bin.resolve(strict=True).read_bytes()),
                "role_config": str(role_config.resolve(strict=True)),
                "role_config_digest": sha256_bytes(roles_bytes),
                "role_config_bytes_digest": sha256_bytes(roles_bytes),
                "role_timeout_policy": timeout_policy,
                "role_timeout_policy_digest": timeout_policy.policy_digest,
                "policy": policy, "inventory": inventory, "operational_binding": binding,
                "maximum_provider_calls": MAXIMUM_CALLS - prior_calls, "planning_role_budget": planning_role_budget,
                "prior_provider_calls": prior_calls,
                "activation": "NOT_CALLED", "worker": "NOT_CALLED", "external_effect": "NOT_CALLED",
            }
            if resume_source is not None:
                lock["resume_source"] = resume_source
            if retry_preflight is not None:
                lock["retry_preflight"] = retry_preflight
            if timeout_policy_path is not None:
                lock["role_timeout_policy_path"] = str(timeout_policy_path.resolve(strict=True))
                lock["role_timeout_policy_bytes_digest"] = sha256_bytes(timeout_policy_bytes)
            if budget_policy_path is not None:
                lock["budget_policy"] = budget_policy
                lock["budget_policy_path"] = str(budget_policy_path.resolve(strict=True))
                lock["budget_policy_bytes_digest"] = sha256_bytes(budget_policy_bytes)
            if adjustment_path is not None:
                lock["timeout_usage_adjustment_path"] = str(adjustment_path.resolve(strict=True))
                lock["timeout_usage_adjustment_bytes_digest"] = sha256_bytes(adjustment_bytes)
            _write_bytes_new(run_root / "inputs" / "role-config.json", roles_bytes)
            _write_new(run_root / "inputs" / "role-timeout-policy.json", timeout_policy)
            if budget_policy_bytes is not None:
                _write_bytes_new(run_root / "inputs" / "budget-policy.json", budget_policy_bytes)
            if adjustment_bytes is not None:
                _write_bytes_new(
                    run_root / "inputs" / "timeout-usage-adjustment.json",
                    adjustment_bytes,
                )
            _write_new(run_root / "preflight.json", lock | {"lock_digest": sha256_digest(lock)})
            if resume_source is not None and continuation_path is None:
                claim_directory = "planning-feedback-claims" if (planning_resume_path or continuation_path) else "goal-feedback-claims"
                claim_digest = resume_source.get("claim_digest", resume_source["outcome_digest"])
                claim = Path(resume_source["run_root"]).parent / claim_directory / (claim_digest[7:] + ".json")
                _write_new(claim, {"source_outcome_digest": resume_source["outcome_digest"],
                                  "run_root": str(run_root), "preflight_digest": sha256_digest(lock)})
            ledger = SQLiteEngineLedger(run_root / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=run_root / "ledger" / "artifacts")
            service = EngineService(ledger)
            service.initialize()
            if checkpoint is not None:
                project_id = service.create_project(
                    name=f"planning continuation {scenario.scenario_id}",
                    root=target_project,
                    project_id=previous.goal_contract.definition.project_id,
                )
                profile = checkpoint.profile
                service.register_profile(profile)
                for index, goal_revision in enumerate(checkpoint.goal_revisions):
                    service.register_goal(
                        goal_revision,
                        activate=index == len(checkpoint.goal_revisions) - 1,
                    )
                service.record_project_map(checkpoint.project_map)
                service.record_state_snapshot(checkpoint.state)
                manager = BudgetManager(service)
                imported_call_ids = manager.import_role_checkpoint(
                    project_id=project_id,
                    goal_id=previous.goal_contract.goal_id,
                    goal_digest=previous.goal_contract.definition_digest,
                    entries=checkpoint.import_entries,
                    source_ref=str(Path(continuation_path).resolve()),
                    source_digest=resume_source["outcome_digest"],
                )
                manager.observe_role_terminal(
                    imported_call_ids[-1], checkpoint.terminal_observation,
                )
                manager.configure(
                    project_id, budget_policy,
                    goal_id=previous.goal_contract.goal_id,
                )
                _write_new(run_root / "imported-role-checkpoint.json", {
                    "source_digest": resume_source["outcome_digest"],
                    "provider_call_ids": imported_call_ids,
                    "provider_calls": len(imported_call_ids),
                    "remaining_provider_calls": resume_source["remaining_provider_calls"],
                    "terminal_observation_digest": resume_source["terminal_observation_digest"],
                })
                if adjustment is None:
                    _write_new(run_root / "role-receipts.json", ())
                    _write_new(run_root / "summary.json", {
                        "status": "BUDGET_USAGE_UNKNOWN",
                        "detail": "timeout 호출의 사용량을 재관측하거나 명시적으로 정산해야 합니다.",
                        "project_id": project_id,
                        "plan_activated": False, "worker_executed": False,
                        "full_qualification": "NOT_RUN", "cutover": "NO-GO",
                        "role_calls": 0, "prior_provider_calls": prior_calls,
                        "remaining_provider_calls": resume_source["remaining_provider_calls"],
                        "claim_created": False,
                    })
                    return 1
                if adjustment["source_receipt_digest"] != sha256_digest(
                    checkpoint.timed_out_receipt
                ):
                    raise ValueError("timeout 사용량 조정이 원본 receipt와 다릅니다.")
                manager.adjust_unknown(
                    call_id=imported_call_ids[-1],
                    charge_tokens=adjustment["charge_tokens"],
                    reason=adjustment["reason"],
                )
                claim_digest = resume_source["claim_digest"]
                claim = Path(resume_source["run_root"]).parent / "planning-feedback-claims" / (claim_digest[7:] + ".json")
                if retry_preflight is None:
                    _write_new(claim, {
                        "source_outcome_digest": resume_source["outcome_digest"],
                        "run_root": str(run_root), "preflight_digest": sha256_digest(lock),
                    })
                else:
                    followup_path, followup_digest = _append_preflight_retry_claim(
                        retry_preflight, resume_source, run_root, lock,
                    )
                    _write_new(run_root / "retry-preflight.json", retry_preflight | {
                        "claim_followup_path": str(followup_path.resolve()),
                        "claim_followup_digest": followup_digest,
                    })
            elif previous is None:
                project_id = service.create_project(name=f"실제 계획 {scenario.scenario_id}", root=target_project)
                profile = _profile(project_id)
                service.register_profile(profile)
            else:
                project_id = previous.goal_contract.definition.project_id
                profile = resume_profile
                if planning_resume_path is None and continuation_path is None:
                    service.register_goal(previous.goal_contract, activate=False)
            runner = RecordedRunner(runtime, run_root, lock)
            if checkpoint is not None:
                runner = BudgetedRoleRunner(
                    runner, service, project_id=project_id,
                    goal_id=previous.goal_contract.goal_id,
                    goal_digest=previous.goal_contract.definition_digest,
                )
            normalizer = GoalNormalizerAdapter(runner, model=roles.normalizer.model, effort=roles.normalizer.effort,
                allowed_fallbacks=roles.normalizer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            goal_reviewer = GoalReviewerAdapter(runner, model=roles.critical_reviewer.model, effort=roles.critical_reviewer.effort,
                allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            project_map = checkpoint.project_map if checkpoint is not None else ProjectMapper().build(
                project_id=project_id, root=target_project, revision_no=1,
                excluded_paths=(ledger.artifact_root.resolve(),),
            )
            if planning_resume_path is not None or continuation_path is not None:
                prepared = previous
            elif previous is None:
                with use_role_timeout_policy(timeout_policy):
                    prepared = GoalPreparationPipeline(normalizer, goal_reviewer).prepare(project_id=project_id, profile=profile,
                        source_request=scenario.source_request, observed_facts=goal_context_observations(project_map, scenario.source_request))
            else:
                from flowmarshal.engine.goal_feedback import GoalPreparationRefiner
                with use_role_timeout_policy(timeout_policy):
                    refinement = GoalPreparationRefiner(normalizer, goal_reviewer).refine(
                        previous=previous, profile=profile, observed_facts=resume_facts,
                    )
                _write_new(run_root / "goal-refinement.json", refinement)
                prepared = refinement.revised_outcome or previous
                if prepared is not previous and prepared.goal_contract.status is not RevisionStatus.READY:
                    service.register_goal(prepared.goal_contract, activate=False)
            _write_new(run_root / "goal-preparation.json", prepared)
            if prepared.goal_contract.status is not RevisionStatus.READY:
                _write_new(run_root / "role-receipts.json", runner.receipts)
                _write_new(run_root / "summary.json", {"status": "GOAL_BLOCKED", "goal_status": prepared.goal_contract.status,
                    "plan_activated": False, "worker_executed": False, "full_qualification": "NOT_RUN", "cutover": "NO-GO",
                    "role_calls": len(runner.receipts), "prior_provider_calls": prior_calls,
                    "latency_ms_to_first_feasible": None})
                return 1
            if planning_resume_path is None and continuation_path is None:
                service.register_goal(prepared.goal_contract)
            if checkpoint is None:
                project_map, state = service.reobserve_project(project_id)
            else:
                state = checkpoint.state
            assignment = ModelAssignmentContract(
                executor=RoleAssignmentPolicy(role="executor", preferred_model=roles.executor.model, preferred_effort=roles.executor.effort,
                    allowed_fallbacks=tuple(ModelFallback(model=item.model, effort=item.effort) for item in roles.executor.allowed_fallbacks)),
                validator=RoleAssignmentPolicy(role="validator", preferred_model=roles.validator.model, preferred_effort=roles.validator.effort,
                    allowed_fallbacks=tuple(ModelFallback(model=item.model, effort=item.effort) for item in roles.validator.allowed_fallbacks)), independence_required=True)
            AssignmentResolver().resolve_contract(assignment, inventory)
            assigner = RuleBasedTaskAssigner(assignment, assignment, assignment)
            generator = SkeletonGeneratorAdapter(runner, model=roles.skeleton_generator.model, effort=roles.skeleton_generator.effort,
                allowed_fallbacks=roles.skeleton_generator.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            skeleton_reviewer = SkeletonReviewerAdapter(runner, model=roles.general_reviewer.model, effort=roles.general_reviewer.effort,
                allowed_fallbacks=roles.general_reviewer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            expander = PlanExpanderAdapter(runner, assigner, model=roles.plan_expander.model, effort=roles.plan_expander.effort,
                allowed_fallbacks=roles.plan_expander.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project, inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2)
            reviewer = PlanReviewerAdapter(runner, model=roles.general_reviewer.model, effort=roles.general_reviewer.effort,
                allowed_fallbacks=roles.general_reviewer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project, critical_model=roles.critical_reviewer.model, critical_effort=roles.critical_reviewer.effort, critical_allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks, inspection_provider_contract=PLAN_INSPECTION_PROVIDER_V2)
            if checkpoint is not None:
                if (
                    checkpoint.candidate.state_signature != state.semantic_digest
                    or checkpoint.plan.definition.base_state_snapshot_digest != state.snapshot_digest
                    or checkpoint.plan.definition.project_map_digest != project_map.revision_digest
                ):
                    raise ValueError("저장된 상세 Plan checkpoint가 현재 State·Project Map과 다릅니다.")
                generator = StoredGenerator(checkpoint, generator)
                skeleton_reviewer = StoredSkeletonReviewer(checkpoint, skeleton_reviewer)
                expander = StoredPlanExpander(checkpoint, expander)
            with use_role_timeout_policy(timeout_policy):
                outcome = SkeletonFirstPlanner(generator, skeleton_reviewer, expander, reviewer).search(goal=prepared.goal_contract, state=state, project_map=project_map,
                    budget=PlanningBudgetPolicy(max_logical_role_calls=planning_role_budget), candidate_count=scenario.candidate_count,
                    feasible_observer=lambda _: first_feasible.append(max(1, int((time.monotonic() - started) * 1000))))
            _write_new(run_root / "planning-outcome.json", outcome)
            _write_new(run_root / "role-receipts.json", runner.receipts)
            for evaluation in outcome.skeleton_evaluations:
                service.record_skeleton_evaluation(evaluation)
            for evaluation in outcome.plan_evaluations:
                service.register_plan_evaluation(evaluation)
            service.record_planning_search(outcome)
            plan = _selected_plan(outcome)
            if plan is None:
                _write_new(run_root / "summary.json", {"status": "NO_SELECTED_PLAN", "plan_activated": False, "worker_executed": False,
                    "full_qualification": "NOT_RUN", "cutover": "NO-GO", "role_calls": len(runner.receipts), "prior_provider_calls": prior_calls,
                    "latency_ms_to_first_feasible": first_feasible[0] if first_feasible else None,
                    "planning_outcome_digest": sha256_digest(outcome)})
                return 1
            _write_new(run_root / "selected-plan.json", plan)
            _write_bytes_new(run_root / "selected-plan-review.md", _review_markdown(
                scenario=scenario, goal=prepared.goal_contract, plan=plan, outcome=outcome,
            ).encode("utf-8"))
            _write_new(run_root / "summary.json", {"status": "PLAN_READY_FOR_USER_REVIEW", "project_id": project_id,
                "plan_revision_id": plan.plan_revision_id, "activation_digest": plan.activation_digest,
                "plan_activated": False, "worker_executed": False, "full_qualification": "NOT_RUN", "cutover": "NO-GO",
                "role_calls": len(runner.receipts), "prior_provider_calls": prior_calls,
                "planning_outcome_digest": sha256_digest(outcome),
                "latency_scope": "this_run_excluding_prior_run",
                "latency_ms_to_first_feasible": first_feasible[0] if first_feasible else None,
                "latency_ms_total": max(1, int((time.monotonic() - started) * 1000))})
            return 0
    except Exception as error:
        if run_root.exists() and not (run_root / "summary.json").exists():
            _write_new(run_root / "summary.json", {"status": "FAIL", "error_type": type(error).__name__, "error": str(error),
                "plan_activated": False, "worker_executed": False, "full_qualification": "NOT_RUN", "cutover": "NO-GO"})
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--role-config", required=True)
    parser.add_argument("--scenario-id", default="S01-single-bugfix")
    parser.add_argument("--role-timeout-policy", help="역할별 timeout과 변경 근거를 담은 절대 JSON 경로")
    parser.add_argument("--budget-policy", help="Goal token 예산을 담은 절대 JSON 경로")
    parser.add_argument(
        "--timeout-usage-adjustment",
        help="timeout 사용량의 명시적 잠정 차감과 원본 receipt 결속을 담은 절대 JSON 경로",
    )
    parser.add_argument("--resume-goal-run", help="첫 Goal 실패의 원본 실행 절대 경로. 같은 요청에 한 번만 피드백한다.")
    parser.add_argument("--resume-planning-run", help="READY Goal 뒤의 첫 Skeleton schema 실패 경로. 변경된 출력 계약에서 한 번 이어간다.")
    parser.add_argument("--continue-planning-run", help="terminal이 확정된 상세 Plan reviewer timeout 실행 절대 경로")
    parser.add_argument(
        "--retry-preflight-run",
        help="provider 효과 전에 lock 검증으로 실패한 timeout continuation 실행 절대 경로",
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
