"""단일 자연어 Goal의 제한된 실제 planning 관측 driver.

Plan 활성화, Worker 실행, 외부 효과와 qualification/cutover 판정은 하지 않는다.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

from pydantic import ValidationError

from flowmarshal.canonical import canonical_json, json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.context import ProjectMapper, goal_context_observations
from flowmarshal.engine.domain import (
    ModelAssignmentContract, ModelFallback, PlanningBudgetPolicy, RevisionStatus,
    RoleAssignmentPolicy,
)
from flowmarshal.engine.goal import (
    GoalNormalizerAdapter, GoalPreparationOutcome, GoalPreparationPipeline,
    GoalReviewerAdapter, ReviewDraft, goal_review_evidence_catalog,
)
from flowmarshal.engine.domain import ProjectProfileRevision
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.model_lock import OperationalBinding, verify_binding
from flowmarshal.engine.models import AssignmentResolver, EngineRoleConfiguration
from flowmarshal.engine.plan_inspection_provider import PLAN_INSPECTION_PROVIDER_V2
from flowmarshal.engine.planner_roles import (
    PlanExpanderAdapter, PlanReviewerAdapter, RuleBasedTaskAssigner, SkeletonBatchDraft,
    SkeletonGeneratorAdapter, SkeletonReviewerAdapter,
)
from flowmarshal.engine.planning import SkeletonFirstPlanner
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
        if source_manifest_files(project_root()) != self.lock["source_manifest"]:
            raise RuntimeError("SOURCE_LOCK_CHANGED")
        if _project_files(Path(self.lock.get("project_root", self.run_root / "project"))) != self.lock["project_files"]:
            raise RuntimeError("PLANNING_PROJECT_INPUT_CHANGED")
        if "resume_source" in self.lock:
            source = self.lock["resume_source"]
            if _project_files(Path(source["run_root"])) != source["files"]:
                raise RuntimeError("GOAL_REPAIR_ORIGINAL_ARTIFACT_CHANGED")

    def run(self, request: RoleCallRequest, *, validator: Any = None) -> RoleCallResult:
        self._verify_lock()
        call_number = len(list((self.run_root / "calls").glob("*/turn.intent.json"))) + 1
        if call_number > self.lock.get("maximum_provider_calls", MAXIMUM_CALLS):
            raise RuntimeError("MAXIMUM_PROVIDER_CALLS_EXCEEDED")
        capture = self.run_root / "calls" / f"{call_number:02d}-{request.role}"
        capture.mkdir(parents=True, exist_ok=False)
        _write_new(capture / "request.json", request)
        _write_new(capture / "strict-schema.json", strict_json_output_schema(request.output_schema))
        _write_new(capture / "request-binding.json", {
            "request_digest": request.request_digest, "role": request.role,
            "model": request.model, "effort": request.effort,
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
        if resume_path is not None and planning_resume_path is not None:
            raise ValueError("Goal 후속과 planning 후속은 동시에 지정할 수 없습니다.")
        previous = resume_source = resume_profile = resume_facts = None
        if planning_resume_path is not None:
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
        prior_calls = 0 if resume_source is None else resume_source["prior_provider_calls"]
        planning_role_budget = MAXIMUM_CALLS - prior_calls - (0 if planning_resume_path else 2)
        workspace_binding = capture_workspace_binding(root)
        with CaptureRuntime(run_root=run_root, codex_bin=codex_bin) as runtime:
            policy = runtime.verify_execution_policy(target_project)
            inventory = runtime.list_models()
            roles.validate_inventory(inventory)
            binding = roles.operational_binding(inventory)
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
                "policy": policy, "inventory": inventory, "operational_binding": binding,
                "maximum_provider_calls": MAXIMUM_CALLS - prior_calls, "planning_role_budget": planning_role_budget,
                "prior_provider_calls": prior_calls,
                "activation": "NOT_CALLED", "worker": "NOT_CALLED", "external_effect": "NOT_CALLED",
            }
            if resume_source is not None:
                lock["resume_source"] = resume_source
            _write_bytes_new(run_root / "inputs" / "role-config.json", roles_bytes)
            _write_new(run_root / "preflight.json", lock | {"lock_digest": sha256_digest(lock)})
            if resume_source is not None:
                claim_directory = "planning-feedback-claims" if planning_resume_path else "goal-feedback-claims"
                claim = Path(resume_source["run_root"]).parent / claim_directory / (resume_source["outcome_digest"][7:] + ".json")
                _write_new(claim, {"source_outcome_digest": resume_source["outcome_digest"],
                                  "run_root": str(run_root), "preflight_digest": sha256_digest(lock)})
            ledger = SQLiteEngineLedger(run_root / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=run_root / "ledger" / "artifacts")
            service = EngineService(ledger)
            service.initialize()
            if previous is None:
                project_id = service.create_project(name=f"실제 계획 {scenario.scenario_id}", root=target_project)
                profile = _profile(project_id)
                service.register_profile(profile)
            else:
                project_id = previous.goal_contract.definition.project_id
                profile = resume_profile
                if planning_resume_path is None:
                    service.register_goal(previous.goal_contract, activate=False)
            runner = RecordedRunner(runtime, run_root, lock)
            normalizer = GoalNormalizerAdapter(runner, model=roles.normalizer.model, effort=roles.normalizer.effort,
                allowed_fallbacks=roles.normalizer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            goal_reviewer = GoalReviewerAdapter(runner, model=roles.critical_reviewer.model, effort=roles.critical_reviewer.effort,
                allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks, inventory_digest=inventory.inventory_digest, inventory=inventory, cwd=target_project)
            project_map = ProjectMapper().build(project_id=project_id, root=target_project, revision_no=1,
                excluded_paths=(ledger.artifact_root.resolve(),))
            if planning_resume_path is not None:
                prepared = previous
            elif previous is None:
                prepared = GoalPreparationPipeline(normalizer, goal_reviewer).prepare(project_id=project_id, profile=profile,
                    source_request=scenario.source_request, observed_facts=goal_context_observations(project_map, scenario.source_request))
            else:
                from flowmarshal.engine.goal_feedback import GoalPreparationRefiner
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
            if planning_resume_path is None:
                service.register_goal(prepared.goal_contract)
            project_map, state = service.reobserve_project(project_id)
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
    parser.add_argument("--resume-goal-run", help="첫 Goal 실패의 원본 실행 절대 경로. 같은 요청에 한 번만 피드백한다.")
    parser.add_argument("--resume-planning-run", help="READY Goal 뒤의 첫 Skeleton schema 실패 경로. 변경된 출력 계약에서 한 번 이어간다.")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
