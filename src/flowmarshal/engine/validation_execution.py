from __future__ import annotations

from .model_lock import OperationalBinding, verify_binding

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from pydantic import Field, model_validator

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    DeterministicValidationObservation, EngineModel, EvidenceKind, EvidenceRecord,
    FailureClass, PlanContractRevision, RepairAction, RunOnceAction, RunOnceOutcome,
    RuntimeIntentKind, RuntimeJobKind, RuntimeJobObservationKind, RuntimeJobStatus, ThreadBinding,
    SemanticValidationObservation, ValidationExecutionStep, ValidationResult, ValidationStatus, new_id, utc_now,
    TaskExecutionSpecRevision,
)
from .execution import execution_context, goal_validation_attempt_scope
from .operations import CoreOperations, ExternalOperationUnknown
from .models import AssignmentResolutionError
from .role_observations import RoleCallReceipt
from .service import EngineService, EngineServiceError


def blocked(project_id: str, code: str, detail: str, task_id: str | None = None):
    return RunOnceOutcome(
        action=RunOnceAction.BLOCKED, project_id=project_id, task_id=task_id,
        blocker_code=code, detail=detail,
        failure_class=FailureClass.EXTERNAL_UNKNOWN if code == "EXTERNAL_EFFECT_UNKNOWN" else None,
        suggested_repair_action=RepairAction.WAIT_EXTERNAL if code == "EXTERNAL_EFFECT_UNKNOWN" else None,
        checkpoint_required=code == "EXTERNAL_EFFECT_UNKNOWN",
    )


def normalize_artifact_paths(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        raise EngineServiceError("직접 file·diff 검증에는 관측할 artifact 경로가 필요합니다.")
    result = []
    for path in paths:
        resolved = (root / path).resolve()
        if resolved == root or root not in resolved.parents:
            raise EngineServiceError("validation artifact는 프로젝트 내부 파일이어야 합니다.")
        result.append(resolved.relative_to(root).as_posix())
    if len(set(result)) != len(result):
        raise EngineServiceError("validation artifact가 같은 파일을 중복 참조합니다.")
    return tuple(result)


def run_command_validation(service: EngineService, task: Any, step: ValidationExecutionStep,
                           *, fault_hook: Callable[[str], None] | None = None) -> RunOnceOutcome:
    """명령 시작 intent와 직접 완료 관측을 보존해 중단 뒤 중복 실행하지 않는다."""
    project_id, task_id, plan_id = task["project_id"], task["id"], task["plan_revision_id"]
    if set(step.required_evidence_kinds) - {"command", "test", "build", "file", "diff"}:
        return blocked(project_id, "VALIDATION_EVIDENCE_KIND_MISMATCH",
                       "명령 관측을 model_review·수동·외부 증거로 바꿔 표시할 수 없습니다.", task_id)
    if fault_hook:
        fault_hook("before_validation")
    with service.ledger.read() as connection:
        spec = None if task_id is None else connection.execute(
            "SELECT definition_digest, payload_json FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
            (task_id,),
        ).fetchone()
        worker = (
            None
            if task_id is None or spec is None
            else service.resolve_task_validation_worker(
                connection,
                task_id=task_id,
                current_spec_digest=spec["definition_digest"],
            )
        )
        retry_sequence = 0 if task_id is None else int(connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM history_events WHERE project_id = ? "
            "AND event_type = 'task.retry_enabled' AND entity_id = ?",
            (project_id, task_id),
        ).fetchone()[0])
        root = Path(connection.execute("SELECT root FROM projects WHERE id = ?", (project_id,)).fetchone()[0]).resolve()
        map_row = connection.execute("SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
                                     (project_id,)).fetchone()
    if task_id is not None and worker is None:
        raise EngineServiceError("Task deterministic validation에는 현재 성공 Worker Attempt가 필요합니다.")
    artifacts = ()
    if set(step.required_evidence_kinds) & {"file", "diff"}:
        paths = step.artifact_paths
        targets = () if spec is None else TaskExecutionSpecRevision.model_validate_json(spec["payload_json"]).definition.resolved_targets
        paths = normalize_artifact_paths(root, paths or tuple(item.path for item in targets))
        target_by_path = {(root / item.path).resolve(): item for item in targets}
        mapped_digests = {} if map_row is None else {
            (root / item["path"]).resolve(): item["content_digest"]
            for item in json.loads(map_row["payload_json"])["entries"]
        }
        if task_id is not None and any(root / path not in target_by_path for path in paths):
            return blocked(project_id, "VALIDATION_TARGET_BINDING_MISMATCH", "검증 artifact가 Task target에 없습니다.", task_id)
        artifacts = tuple({
            "path": path,
            "before_digest": (target_by_path[root / path].expected_content_digest
                              if root / path in target_by_path else
                              mapped_digests.get(root / path)),
            "expected_presence": not (root / path in target_by_path and target_by_path[root / path].access == "delete"),
        } for path in paths)
    request = {
        "plan_revision_id": plan_id, "task_id": task_id,
        "execution_spec_digest": None if spec is None else spec["definition_digest"],
        "goal_binding": task.get("goal_binding") if isinstance(task, dict) else None,
        "step": step.model_dump(mode="json"),
        "artifacts": artifacts,
    }
    if worker is not None:
        request.update({
            "worker_attempt_id": worker["id"],
            "worker_succeeded_sequence": int(worker["sequence"]),
            "validation_epoch_sequence": max(int(worker["sequence"]), retry_sequence),
            "validation_execution_spec_digest": spec["definition_digest"],
            "source_worker_execution_spec_digest": worker["execution_spec_digest"],
        })

    def execute():
        service.assert_project_authorized(project_id)
        stdout, stderr, code, timed_out = b"", b"", None, False
        try:
            result = subprocess.run(step.argv, cwd=step.working_directory, capture_output=True,
                                    check=False, timeout=step.timeout_seconds, shell=False)
            stdout, stderr, code = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            stdout, stderr, timed_out = error.stdout or b"", error.stderr or b"", True
        except OSError as error:
            return {"launch_error": str(error)}
        observed = DeterministicValidationObservation(
            validation_id=step.validation_id, task_id=task_id, argv=step.argv,
            working_directory=step.working_directory, timeout_seconds=step.timeout_seconds,
            expected_exit_codes=step.expected_exit_codes, actual_exit_code=code, timed_out=timed_out,
            stdout=stdout.decode("utf-8", errors="replace")[:100_000],
            stderr=stderr.decode("utf-8", errors="replace")[:100_000], observed_at=utc_now(),
        )
        file_observations = []
        for artifact in artifacts:
            path = root / artifact["path"]
            exists = path.is_file()
            observed_bytes = path.read_bytes() if exists else None
            file_observations.append(artifact | {
                "exists": exists, "after_digest": sha256_bytes(observed_bytes) if observed_bytes is not None else None,
                "file_excerpt": None if observed_bytes is None else observed_bytes[:6000].decode("utf-8", errors="replace")[:3000],
                "excerpt_truncated": observed_bytes is not None and (len(observed_bytes) > 6000 or len(observed_bytes.decode("utf-8", errors="replace")) > 3000),
            })
        return {"observation": observed.model_dump(mode="json"), "artifacts": file_observations}

    try:
        response = CoreOperations(service, fault_hook).invoke(
            project_id=project_id, kind="validation_command", request=request, execute=execute,
        )
    except ExternalOperationUnknown as error:
        return blocked(project_id, "EXTERNAL_EFFECT_UNKNOWN", str(error), task_id)
    if "launch_error" in response:
        return blocked(project_id, "VALIDATION_ENVIRONMENT_ERROR", response["launch_error"], task_id)
    observation = DeterministicValidationObservation.model_validate(response["observation"])
    operation_request_digest = sha256_digest({"kind": "validation_command", "request": request})
    operation_id = "operation_" + sha256_digest(
        {"project_id": project_id, "request_digest": operation_request_digest}
    )[7:39]
    operation_binding = None if worker is None else {
        "format": "task-validation-operation-v1",
        "operation_id": operation_id,
        "request_digest": operation_request_digest,
        "result_digest": sha256_digest(response),
        "worker_attempt_id": worker["id"],
        "worker_succeeded_sequence": int(worker["sequence"]),
        "validation_epoch_sequence": max(int(worker["sequence"]), retry_sequence),
        "validation_execution_spec_digest": spec["definition_digest"],
        "source_worker_execution_spec_digest": worker["execution_spec_digest"],
    }
    evidence_ids = []
    for kind in step.required_evidence_kinds:
        if kind in {"file", "diff"}:
            for document in response["artifacts"]:
                evidence = EvidenceRecord(
                    evidence_id=new_id("evidence"), project_id=project_id, task_id=task_id,
                    attempt_id=None if worker is None else worker["id"],
                    kind=EvidenceKind(kind), source_ref=document["path"],
                    observation=json.dumps(document, ensure_ascii=False, sort_keys=True),
                    content_digest=sha256_digest({"kind": kind, "direct_file_observation": document}),
                    observed_at=observation.observed_at,
                )
                service.record_evidence(evidence)
                evidence_ids.append(evidence.evidence_id)
            continue
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=project_id, task_id=task_id,
            attempt_id=None if worker is None else worker["id"],
            kind=EvidenceKind(kind), source_ref=f"validation:{step.validation_id}:{step.argv[0]}",
            observation=observation.model_dump_json()[:10_000],
            content_digest=sha256_digest({"kind": kind, "observation": observation}),
            observed_at=observation.observed_at,
        )
        service.record_evidence(evidence)
        evidence_ids.append(evidence.evidence_id)
    result = ValidationResult(
        validation_result_id=new_id("validation_result"), validation_id=step.validation_id,
        task_id=task_id, status=(ValidationStatus.PASS if observation.passed and all(
            item["exists"] == item["expected_presence"] for item in response["artifacts"]
        ) else ValidationStatus.FAIL),
        goal_validation_binding_digest=(None if task_id is not None else task.get("goal_binding_digest")),
        evidence_ids=tuple(evidence_ids),
        rationale=f"직접 명령 관측: exit={observation.actual_exit_code}, timeout={observation.timed_out}",
        evaluated_at=observation.observed_at,
    )
    service.record_validation(
        project_id=project_id,
        plan_revision_id=plan_id,
        result=result,
        operation_binding=operation_binding,
    )
    if fault_hook:
        fault_hook("after_validation_observed")
    return RunOnceOutcome(
        action=RunOnceAction.VALIDATED, project_id=project_id, task_id=task_id,
        validation_result_id=result.validation_result_id, evidence_ids=tuple(evidence_ids),
        detail=f"결정적 validation 직접 관측: {result.status.value}",
    )


class GoalValidationRetryRequest(EngineModel):
    """실패한 독립 deterministic Goal Test의 환경 복구 재시도 요청."""

    step: ValidationExecutionStep
    failed_validation_result_id: str = Field(pattern=r"^validation_result_[0-9a-f]{32}$")
    failure_class: FailureClass
    failure_evidence_id: str = Field(pattern=r"^evidence_[0-9a-f]{32}$")
    rationale: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def only_environment_recovery_is_supported(self) -> "GoalValidationRetryRequest":
        if self.failure_class is not FailureClass.ENVIRONMENT:
            raise ValueError("Goal Test 운영 상세 재시도에는 environment 원인만 허용합니다.")
        return self


class GoalValidationRetryBinding(EngineModel):
    failed_validation_result_id: str = Field(pattern=r"^validation_result_[0-9a-f]{32}$")
    failure_class: FailureClass
    failure_evidence_id: str = Field(pattern=r"^evidence_[0-9a-f]{32}$")
    rationale: str = Field(min_length=1, max_length=5000)
    prior_binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retry_no: int = Field(ge=1, le=2)

    @model_validator(mode="after")
    def only_environment_recovery_is_supported(self) -> "GoalValidationRetryBinding":
        if self.failure_class is not FailureClass.ENVIRONMENT:
            raise ValueError("Goal Test 운영 상세 재시도에는 environment 원인만 허용합니다.")
        return self


class GoalValidationBinding(EngineModel):
    goal_validation_binding_id: str | None = Field(default=None, pattern=r"^goal_binding_[0-9a-f]{32}$")
    plan_revision_id: str = Field(pattern=r"^plan_revision_[0-9a-f]{32}$")
    plan_activation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    role_configuration_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    model_inventory_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    operational_binding: OperationalBinding | None = None
    retry: GoalValidationRetryBinding | None = None
    step: ValidationExecutionStep

    def payload(self) -> dict[str, Any]:
        """이전 binding을 다시 실행할 때 기존 Core operation 입력을 바꾸지 않는다."""
        payload = self.model_dump(mode="json")
        if self.operational_binding is None:
            payload.pop("operational_binding")
        if self.goal_validation_binding_id is None:
            payload.pop("goal_validation_binding_id")
            payload.pop("retry")
        return payload

    @property
    def binding_digest(self) -> str:
        return sha256_digest(self.payload())


def _goal_bindings(service: EngineService, project_id: str, plan_revision_id: str,
                   validation_id: str) -> list[tuple[GoalValidationBinding, Any]]:
    with service.ledger.read() as connection:
        rows = connection.execute(
            "SELECT sequence, payload_json, created_at FROM history_events WHERE project_id = ? "
            "AND event_type IN ('goal_test.bound', 'goal_test.retry_bound') ORDER BY sequence DESC",
            (project_id,),
        ).fetchall()
    return [
        (binding, row)
        for row in rows
        for binding in (GoalValidationBinding.model_validate_json(row["payload_json"]),)
        if binding.plan_revision_id == plan_revision_id and binding.step.validation_id == validation_id
    ]


def _normalize_goal_step(context: dict[str, Any], contract: Any,
                         step: ValidationExecutionStep) -> ValidationExecutionStep:
    if (step.validation_id != contract.validation_id or step.method != contract.method
            or set(step.required_evidence_kinds) != set(contract.required_evidence_kinds)):
        raise EngineServiceError("Goal Test step의 ID·method·evidence kind가 PlanContract와 다릅니다.")
    if step.method != "deterministic":
        return step
    root = Path(context["project_map"]["root"]).resolve(strict=True)
    cwd = (root / (step.working_directory or "")).resolve(strict=True)
    if not cwd.is_dir() or (cwd != root and root not in cwd.parents):
        raise EngineServiceError("Goal Test cwd는 프로젝트 내부여야 합니다.")
    step = step.model_copy(update={"working_directory": str(cwd)})
    if set(step.required_evidence_kinds) & {"file", "diff"}:
        step = step.model_copy(update={"artifact_paths": normalize_artifact_paths(root, step.artifact_paths)})
    return step


def _validate_goal_retry(
    service: EngineService, project_id: str, plan: PlanContractRevision, contract: Any,
    binding: GoalValidationBinding, binding_row: Any, retry: GoalValidationRetryRequest,
    retry_step: ValidationExecutionStep,
) -> GoalValidationRetryBinding:
    if contract.evidence_mode != "independent" or contract.method != "deterministic":
        raise EngineServiceError("독립 deterministic Goal Test만 운영 상세 재시도할 수 있습니다.")
    with service.ledger.read() as connection:
        result_row = connection.execute(
            "SELECT payload_json FROM validation_results WHERE id = ? AND project_id = ? "
            "AND plan_revision_id = ? AND task_id IS NULL",
            (retry.failed_validation_result_id, project_id, plan.plan_revision_id),
        ).fetchone()
        latest_row = connection.execute(
            "SELECT id FROM validation_results WHERE project_id = ? AND plan_revision_id = ? "
            "AND task_id IS NULL AND validation_id = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
            (project_id, plan.plan_revision_id, contract.validation_id),
        ).fetchone()
        result_event = connection.execute(
            "SELECT sequence FROM history_events WHERE project_id = ? AND event_type = 'validation.recorded' "
            "AND entity_id = ?",
            (project_id, retry.failed_validation_result_id),
        ).fetchone()
    if result_row is None or result_event is None:
        raise EngineServiceError("Goal Test 재시도 대상 실패 결과가 원장에 없습니다.")
    if latest_row is None or latest_row["id"] != retry.failed_validation_result_id:
        raise EngineServiceError("Goal Test 재시도 대상은 현재 최신 실패 결과여야 합니다.")
    result = ValidationResult.model_validate_json(result_row["payload_json"])
    if result.validation_id != contract.validation_id or result.status is not ValidationStatus.FAIL:
        raise EngineServiceError("Goal Test 재시도 대상은 현재 integration validation의 FAIL이어야 합니다.")
    if result.goal_validation_binding_digest is not None:
        if result.goal_validation_binding_digest != binding.binding_digest:
            raise EngineServiceError("Goal Test FAIL 결과가 현재 binding에 결속되지 않았습니다.")
    elif binding_row["sequence"] >= result_event["sequence"]:
        raise EngineServiceError("legacy Goal Test FAIL 결과와 이전 binding의 순서가 맞지 않습니다.")
    if retry.failure_evidence_id not in result.evidence_ids:
        raise EngineServiceError("Goal Test 재시도 evidence가 실패 결과에 결속되지 않았습니다.")
    with service.ledger.read() as connection:
        evidence_row = connection.execute(
            "SELECT kind, observation FROM evidence_records WHERE id = ? AND project_id = ? AND task_id IS NULL",
            (retry.failure_evidence_id, project_id),
        ).fetchone()
    if evidence_row is None or evidence_row["kind"] not in {"command", "test", "build"}:
        raise EngineServiceError("Goal Test 재시도에는 직접 command/test/build 실패 evidence가 필요합니다.")
    observation = DeterministicValidationObservation.model_validate_json(evidence_row["observation"])
    if observation.validation_id != contract.validation_id or observation.passed:
        raise EngineServiceError("Goal Test 재시도 evidence가 직접 실패 관측이 아닙니다.")
    if retry_step.expected_exit_codes != binding.step.expected_exit_codes:
        raise EngineServiceError("Goal Test 재시도는 예상 종료 코드 계약을 바꿀 수 없습니다.")
    if retry_step.artifact_paths != binding.step.artifact_paths:
        raise EngineServiceError("Goal Test 재시도는 artifact 관측 집합을 바꿀 수 없습니다.")
    retry_count = sum(
        bound.retry is not None
        for bound, _ in _goal_bindings(service, project_id, plan.plan_revision_id, contract.validation_id)
    )
    if retry_count >= 2:
        raise EngineServiceError("동일 Goal Test 운영 상세 재시도 한도 2회를 넘었습니다.")
    return GoalValidationRetryBinding(
        failed_validation_result_id=retry.failed_validation_result_id,
        failure_class=retry.failure_class,
        failure_evidence_id=retry.failure_evidence_id,
        rationale=retry.rationale,
        prior_binding_digest=binding.binding_digest,
        retry_no=int(retry_count) + 1,
    )


def _prepare_goal_test_job(
    runtime: Any,
    provider: Any,
    *,
    project_id: str,
    validation_id: str,
    supplied_step: ValidationExecutionStep | None,
) -> dict[str, Any]:
    """model inventory와 준비 역할을 같은 RuntimeJob owner 안에서 관측한다."""

    inventory = runtime.list_models()
    if supplied_step is None:
        try:
            step = provider.prepare_goal(
                project_id=project_id,
                validation_id=validation_id,
                inventory=inventory,
            )
        except AssignmentResolutionError as error:
            return {
                "model_binding_error": f"{type(error).__name__}: {error}",
            }
    else:
        step = supplied_step
    try:
        operational_binding = (
            provider.roles.operational_binding(inventory)
            if step.method == "semantic"
            else None
        )
    except (AttributeError, ValueError) as error:
        return {
            "step": step.model_dump(mode="json"),
            "model_binding_error": f"{type(error).__name__}: {error}",
        }
    return {
        "step": step.model_dump(mode="json"),
        "model_inventory_digest": (
            inventory.inventory_digest if operational_binding is not None else None
        ),
        "operational_binding": (
            None
            if operational_binding is None
            else operational_binding.model_dump(mode="json")
        ),
    }


def _run_goal_semantic_validation_job(
    service: EngineService,
    runtime: Any,
    provider: Any,
    *,
    project_id: str,
    attempt_id: str,
    binding: GoalValidationBinding,
    context: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """inventory 재검사와 semantic role turn을 같은 RuntimeJob에서 수행한다."""

    inventory = runtime.list_models()
    try:
        if (
            binding.operational_binding is not None
            and binding.model_inventory_digest
            != binding.operational_binding.inventory_digest
        ):
            raise ValueError("MODEL_LOCK_EVIDENCE_DIGEST_MISMATCH")
        verify_binding(binding.operational_binding, inventory)
        if (
            provider.roles.operational_binding(inventory).lock_digest
            != binding.operational_binding.lock_digest
        ):
            raise ValueError("MODEL_LOCK_ROLE_BINDING_MISMATCH")
    except (AttributeError, ValueError) as error:
        return {"model_binding_error": f"{type(error).__name__}: {error}"}
    intent = service.prepare_runtime_intent(
        attempt_id=attempt_id,
        kind=RuntimeIntentKind.START_TURN,
        idempotency_key=f"goal-semantic-validator:{attempt_id}:turn",
        request={
            "goal_validation_contract": 1,
            "validation_id": binding.step.validation_id,
            "goal_validation_binding_digest": binding.binding_digest,
            "semantic_evidence_ids": sorted(catalog),
        },
    )
    with goal_validation_attempt_scope(attempt_id):
        validation_result = provider.validate_goal(
            project_id=project_id,
            inventory=inventory,
            context=context,
            evidence_catalog=catalog,
            step=binding.step,
        )
    receipt = RoleCallReceipt.model_validate(validation_result["receipt"])
    if (
        receipt.status != "succeeded"
        or receipt.thread_id is None
        or len(receipt.turn_ids) != 1
    ):
        raise EngineServiceError("Goal semantic validator에는 terminal exact thread/turn receipt가 필요합니다.")
    turn_id = receipt.turn_ids[0]
    from .runtime import active_runtime_job_id
    job_id = active_runtime_job_id()
    if job_id is None:
        with service.ledger.read() as connection:
            row = connection.execute(
                "SELECT id FROM runtime_jobs WHERE attempt_id=? "
                "AND kind='goal_semantic_validate' ORDER BY rowid DESC LIMIT 1",
                (attempt_id,),
            ).fetchone()
        job_id = None if row is None else row["id"]
    if job_id is None:
        raise EngineServiceError("Goal semantic validator RuntimeJob binding이 없습니다.")
    service.bind_runtime_job_provider(
        job_id, thread_id=receipt.thread_id, turn_id=turn_id,
    )
    service.record_runtime_receipt(
        intent_id=intent.intent_id,
        provider_operation_id=turn_id,
        response={"role_receipt": receipt.model_dump(mode="json")},
        binding=ThreadBinding(
            thread_id=receipt.thread_id,
            turn_id=turn_id,
            bound_at=receipt.recorded_at,
        ),
    )
    return {"validation_result": validation_result}


def advance_independent_goal_test(
    service: EngineService, runtime: Any, project_id: str, plan: PlanContractRevision, contract: Any,
    *, supplied_step: ValidationExecutionStep | None = None, provider: Any | None = None,
    retry_request: GoalValidationRetryRequest | None = None,
    fault_hook: Callable[[str], None] | None = None,
    supervisor: Any | None = None,
) -> RunOnceOutcome:
    service.assert_project_authorized(project_id)
    try:
        context = execution_context(service, project_id)
    except EngineServiceError as error:
        if "STALE_EXECUTION_INPUT" in str(error):
            return blocked(project_id, "STALE_EXECUTION_INPUT", str(error))
        raise
    if context["plan"]["plan_revision_id"] != plan.plan_revision_id:
        raise EngineServiceError("Goal Test의 활성 Plan이 다릅니다.")
    with service.ledger.read() as connection:
        if connection.execute("SELECT COUNT(*) FROM task_contracts WHERE plan_revision_id = ? AND status <> 'completed'",
                              (plan.plan_revision_id,)).fetchone()[0]:
            return blocked(project_id, "GOAL_TEST_INPUT_INCOMPLETE", "모든 Task 완료 후 Goal Test를 준비합니다.")
    bindings = _goal_bindings(service, project_id, plan.plan_revision_id, contract.validation_id)
    binding, binding_row = (bindings[0] if bindings else (None, None))
    if retry_request is not None:
        supplied_step = retry_request.step
        if binding is not None and (
            binding.plan_activation_digest != plan.activation_digest
            or binding.context_digest != sha256_digest(context)
        ):
            return blocked(project_id, "STALE_EXECUTION_INPUT",
                           "실패한 Goal Test binding 이후 입력이 바뀌어 재시도할 수 없습니다.")
    if retry_request is None and binding is not None and supplied_step == binding.step:
        supplied_step = None
    if supplied_step is not None or binding is None:
        step = supplied_step
        if step is None and provider is None:
            return blocked(project_id, "GOAL_VALIDATION_SPEC_REQUIRED",
                           f"독립 Goal Test의 실행 binding이 필요합니다: {contract.validation_id}")
        inventory = None
        model_binding = None
        inventory_digest = None
        if provider is not None and (step is None or step.method == "semantic"):
            try:
                service.assert_project_authorized(project_id)
                if supervisor is None:
                    inventory = runtime.list_models()
                    if step is None:
                        step = provider.prepare_goal(
                            project_id=project_id,
                            validation_id=contract.validation_id,
                            inventory=inventory,
                        )
                    if step.method == "semantic":
                        model_binding = provider.roles.operational_binding(inventory)
                else:
                    checkpoint = (
                        f"goal_test_prepare:{plan.plan_revision_id}:{contract.validation_id}:"
                        f"{0 if retry_request is None else retry_request.failed_validation_result_id}"
                    )
                    with service.ledger.read() as connection:
                        row = connection.execute(
                            "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                            (project_id, checkpoint),
                        ).fetchone()
                    if row is None:
                        job = supervisor.schedule(
                            project_id=project_id,
                            kind=RuntimeJobKind.GOAL_TEST_PREPARE,
                            checkpoint_key=checkpoint,
                            request={
                                "validation_id": contract.validation_id,
                                "supplied_step_digest": (
                                    None if step is None else sha256_digest(step)
                                ),
                                "inventory_observation": "runtime_job_owned",
                                "role_configuration_digest": (
                                    provider.roles.configuration_digest
                                ),
                            },
                            timeout_seconds=900,
                            target=lambda: _prepare_goal_test_job(
                                runtime,
                                provider,
                                project_id=project_id,
                                validation_id=contract.validation_id,
                                supplied_step=step,
                            ),
                        )
                    else:
                        job = service._runtime_job_from_row(row)
                        if job.status in {
                            RuntimeJobStatus.SCHEDULED,
                            RuntimeJobStatus.RUNNING,
                        }:
                            job = supervisor.tick(job.job_id)
                    if job.status not in {
                        RuntimeJobStatus.PROVIDER_TERMINAL,
                        RuntimeJobStatus.CONSUMED,
                    }:
                        return RunOnceOutcome(
                            action=RunOnceAction.DISPATCHED,
                            project_id=project_id,
                            detail=(
                                "Goal Test 준비 job을 예약·관측했습니다: "
                                f"{job.job_id}"
                            ),
                        )
                    result = service.consume_runtime_job_required_result(job.job_id)
                    if "model_binding_error" in result:
                        return blocked(
                            project_id,
                            "MODEL_BINDING_CHANGED",
                            result["model_binding_error"],
                        )
                    step = ValidationExecutionStep.model_validate(result["step"])
                    model_binding = (
                        None
                        if result["operational_binding"] is None
                        else OperationalBinding.model_validate(
                            result["operational_binding"]
                        )
                    )
                    inventory_digest = result["model_inventory_digest"]
            except ExternalOperationUnknown as error:
                return blocked(project_id, "EXTERNAL_EFFECT_UNKNOWN", str(error))
        step = _normalize_goal_step(context, contract, step)
        if retry_request is not None:
            if binding is None or binding_row is None:
                raise EngineServiceError("Goal Test 재시도에는 기존 실행 binding이 필요합니다.")
            if step == binding.step:
                raise EngineServiceError("Goal Test 재시도 step은 현재 binding과 달라야 합니다.")
            retry = _validate_goal_retry(
                service, project_id, plan, contract, binding, binding_row, retry_request, step,
            )
        else:
            retry = None
        if inventory is not None and model_binding is not None:
            inventory_digest = inventory.inventory_digest
        binding = GoalValidationBinding(
            goal_validation_binding_id=new_id("goal_binding"), plan_revision_id=plan.plan_revision_id,
                                        plan_activation_digest=plan.activation_digest,
                                        context_digest=sha256_digest(context), step=step,
                                        model_inventory_digest=inventory_digest,
                                        operational_binding=model_binding,
                                        role_configuration_digest=(None if provider is None else provider.roles.configuration_digest),
                                        retry=retry)
        with service.ledger.transaction() as tx:
            tx.history(project_id, "goal_test.retry_bound" if retry is not None else "goal_test.bound",
                       "goal_validation_binding", binding.goal_validation_binding_id,
                       binding.payload())
        return RunOnceOutcome(action=RunOnceAction.MATERIALIZED, project_id=project_id,
                              detail=(f"독립 Goal Test 운영 상세 재시도 binding을 고정했습니다: {step.validation_id}"
                                      if retry is not None else f"독립 Goal Test 실행 binding을 고정했습니다: {step.validation_id}"))
    if binding.plan_activation_digest != plan.activation_digest or binding.context_digest != sha256_digest(context):
        return blocked(project_id, "STALE_EXECUTION_INPUT", "Goal Test materialization 이후 입력이 바뀌었습니다.")
    if binding.step.method == "deterministic":
        return run_command_validation(
            service, {"project_id": project_id, "id": None, "plan_revision_id": plan.plan_revision_id,
                      "goal_binding": binding.payload(),
                      "goal_binding_digest": binding.binding_digest}, binding.step, fault_hook=fault_hook,
        )
    if binding.step.method == "semantic" and provider is not None:
        if binding.role_configuration_digest != provider.roles.configuration_digest:
            return blocked(project_id, "MODEL_BINDING_CHANGED", "Goal Test의 역할 설정이 바뀌었습니다.")
        with service.ledger.read() as connection:
            rows = [row for task in plan.definition.tasks
                    for row in service.task_evidence_rows(connection, task.task_id)]
        catalog = {
            row["id"]: json.loads(row["payload_json"]) for row in rows
            if not (row["kind"] == "external_observation"
                    and row["source_ref"].startswith("codex-thread:")
                    and row["source_ref"].endswith(":truncated"))
        }
        if not catalog:
            return blocked(project_id, "GOAL_TEST_INPUT_INCOMPLETE", "독립 검사에 필요한 직접 evidence가 없습니다.")
        checkpoint = (
            f"goal_semantic_validate:{plan.plan_revision_id}:"
            f"{contract.validation_id}:{binding.binding_digest}"
        )
        try:
            service.assert_project_authorized(project_id)
            with service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                    (project_id, checkpoint),
                ).fetchone()
            if row is None:
                attempt = service.reserve_goal_validation_attempt(
                    project_id=project_id, plan_revision_id=plan.plan_revision_id,
                )
                attempt_id = attempt.attempt_id
                request = {
                    "validation_id": contract.validation_id,
                    "binding_digest": binding.binding_digest,
                    "context_digest": sha256_digest(context),
                    "evidence_catalog_digest": sha256_digest(catalog),
                    "expected_inventory_digest": binding.model_inventory_digest,
                    "expected_lock_digest": (
                        None
                        if binding.operational_binding is None
                        else binding.operational_binding.lock_digest
                    ),
                    "inventory_observation": "runtime_job_owned",
                    "attempt_id": attempt_id,
                }
                if supervisor is None:
                    job = service.schedule_runtime_job(
                        project_id=project_id,
                        kind=RuntimeJobKind.GOAL_SEMANTIC_VALIDATE,
                        checkpoint_key=checkpoint,
                        request=request,
                        absolute_deadline_at=utc_now() + timedelta(seconds=900),
                        attempt_id=attempt_id,
                        task_id=None,
                    )
                    job = service.start_runtime_job(job.job_id)
                    job_result = _run_goal_semantic_validation_job(
                        service,
                        runtime,
                        provider,
                        project_id=project_id,
                        attempt_id=attempt_id,
                        binding=binding,
                        context=context,
                        catalog=catalog,
                    )
                    service.record_runtime_job_observation(
                        job.job_id,
                        kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
                        payload={"result": job_result},
                        provider_terminal=True,
                        terminal_status="completed",
                    )
                    job = service.load_runtime_job(job.job_id)
                else:
                    job = supervisor.schedule(
                        project_id=project_id,
                        kind=RuntimeJobKind.GOAL_SEMANTIC_VALIDATE,
                        checkpoint_key=checkpoint,
                        request=request,
                        timeout_seconds=900,
                        target=lambda: _run_goal_semantic_validation_job(
                            service,
                            runtime,
                            provider,
                            project_id=project_id,
                            attempt_id=attempt_id,
                            binding=binding,
                            context=context,
                            catalog=catalog,
                        ),
                        attempt_id=attempt_id,
                        task_id=None,
                    )
            else:
                job = service._runtime_job_from_row(row)
                attempt_id = job.attempt_id
                if attempt_id is None:
                    raise EngineServiceError("Goal semantic RuntimeJob에 validation Attempt가 없습니다.")
                if supervisor is not None and job.status in {
                    RuntimeJobStatus.SCHEDULED, RuntimeJobStatus.RUNNING,
                }:
                    job = supervisor.tick(job.job_id)
            if job.status not in {
                RuntimeJobStatus.PROVIDER_TERMINAL,
                RuntimeJobStatus.CONSUMED,
            }:
                return RunOnceOutcome(
                    action=RunOnceAction.DISPATCHED,
                    project_id=project_id,
                    attempt_id=attempt_id,
                    detail=f"Goal semantic validation job을 예약·관측했습니다: {job.job_id}",
                )
            job_result = service.consume_runtime_job_required_result(job.job_id)
            if "model_binding_error" in job_result:
                return blocked(
                    project_id,
                    "MODEL_BINDING_CHANGED",
                    job_result["model_binding_error"],
                )
            result = job_result["validation_result"]
        except ExternalOperationUnknown as error:
            return blocked(project_id, "EXTERNAL_EFFECT_UNKNOWN", str(error))
        if sha256_digest(execution_context(service, project_id)) != binding.context_digest:
            return blocked(project_id, "STALE_EXECUTION_INPUT", "독립 검사 중 프로젝트 입력이 바뀌었습니다.")
        receipt = result["receipt"]
        observed = SemanticValidationObservation(
            validation_id=contract.validation_id, reviewer_role="goal_validator",
            model=receipt["model"], effort=receipt["effort"],
            observed_at=receipt["recorded_at"], **result["payload"],
        )
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=project_id, kind=EvidenceKind.MODEL_REVIEW,
            attempt_id=attempt_id,
            source_ref=f"codex-validator:{receipt['thread_id']}", observation=observed.model_dump_json()[:10_000],
            content_digest=sha256_digest(observed),
            observed_at=observed.observed_at,
        )
        service.record_evidence(evidence)
        evidence_ids = (evidence.evidence_id, *observed.evidence_refs)
        validation = ValidationResult(
            validation_result_id=new_id("validation_result"), validation_id=contract.validation_id,
            status=ValidationStatus.PASS if observed.passed else ValidationStatus.FAIL,
            goal_validation_binding_digest=binding.binding_digest,
            evidence_ids=evidence_ids, rationale=observed.rationale, evaluated_at=observed.observed_at,
        )
        with service.ledger.read() as connection:
            attempt_status = connection.execute(
                "SELECT status FROM attempts WHERE id=?", (attempt_id,),
            ).fetchone()["status"]
        if attempt_status != "succeeded":
            service.finish_goal_validation_attempt(attempt_id=attempt_id, succeeded=True)
        service.record_validation(project_id=project_id, plan_revision_id=plan.plan_revision_id, result=validation)
        return RunOnceOutcome(action=RunOnceAction.VALIDATED, project_id=project_id,
                              validation_result_id=validation.validation_result_id, evidence_ids=evidence_ids,
                              detail="독립 Goal Validator의 typed observation을 기록했습니다.")
    return blocked(project_id, "GOAL_VALIDATION_OBSERVATION_REQUIRED",
                   f"독립 {binding.step.method} typed observation이 필요합니다: {contract.validation_id}")
