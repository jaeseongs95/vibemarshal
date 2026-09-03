from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from pydantic import Field

from ..canonical import sha256_bytes, sha256_digest
from .domain import (
    DeterministicValidationObservation, EngineModel, EvidenceKind, EvidenceRecord,
    FailureClass, PlanContractRevision, RepairAction, RunOnceAction, RunOnceOutcome,
    SemanticValidationObservation, ValidationExecutionStep, ValidationResult, ValidationStatus, new_id, utc_now,
    TaskExecutionSpecRevision,
)
from .execution import execution_context
from .operations import CoreOperations, ExternalOperationUnknown
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
        root = Path(connection.execute("SELECT root FROM projects WHERE id = ?", (project_id,)).fetchone()[0]).resolve()
        map_row = connection.execute("SELECT payload_json FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
                                     (project_id,)).fetchone()
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

    def execute():
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
    evidence_ids = []
    for kind in step.required_evidence_kinds:
        if kind in {"file", "diff"}:
            for document in response["artifacts"]:
                evidence = EvidenceRecord(
                    evidence_id=new_id("evidence"), project_id=project_id, task_id=task_id,
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
        evidence_ids=tuple(evidence_ids),
        rationale=f"직접 명령 관측: exit={observation.actual_exit_code}, timeout={observation.timed_out}",
        evaluated_at=observation.observed_at,
    )
    service.record_validation(project_id=project_id, plan_revision_id=plan_id, result=result)
    if fault_hook:
        fault_hook("after_validation_observed")
    return RunOnceOutcome(
        action=RunOnceAction.VALIDATED, project_id=project_id, task_id=task_id,
        validation_result_id=result.validation_result_id, evidence_ids=tuple(evidence_ids),
        detail=f"결정적 validation 직접 관측: {result.status.value}",
    )


class GoalValidationBinding(EngineModel):
    plan_revision_id: str = Field(pattern=r"^plan_revision_[0-9a-f]{32}$")
    plan_activation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    role_configuration_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    model_inventory_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    step: ValidationExecutionStep


def advance_independent_goal_test(
    service: EngineService, runtime: Any, project_id: str, plan: PlanContractRevision, contract: Any,
    *, supplied_step: ValidationExecutionStep | None = None, provider: Any | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> RunOnceOutcome:
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
        rows = connection.execute(
            "SELECT payload_json FROM history_events WHERE project_id = ? AND event_type = 'goal_test.bound' "
            "ORDER BY sequence DESC", (project_id,),
        ).fetchall()
    binding = next((item for item in (GoalValidationBinding.model_validate_json(row["payload_json"]) for row in rows)
                    if item.plan_revision_id == plan.plan_revision_id and item.step.validation_id == contract.validation_id), None)
    if binding is not None and supplied_step == binding.step:
        supplied_step = None
    if supplied_step is not None or binding is None:
        step = supplied_step
        if step is None:
            if provider is None:
                return blocked(project_id, "GOAL_VALIDATION_SPEC_REQUIRED",
                               f"독립 Goal Test의 실행 binding이 필요합니다: {contract.validation_id}")
            try:
                step = provider.prepare_goal(project_id=project_id, validation_id=contract.validation_id,
                                             inventory=runtime.list_models())
            except ExternalOperationUnknown as error:
                return blocked(project_id, "EXTERNAL_EFFECT_UNKNOWN", str(error))
        if (step.validation_id != contract.validation_id or step.method != contract.method
                or set(step.required_evidence_kinds) != set(contract.required_evidence_kinds)):
            raise EngineServiceError("Goal Test step의 ID·method·evidence kind가 PlanContract와 다릅니다.")
        if step.method == "deterministic":
            root = Path(context["project_map"]["root"]).resolve(strict=True)
            cwd = (root / (step.working_directory or "")).resolve(strict=True)
            if not cwd.is_dir() or (cwd != root and root not in cwd.parents):
                raise EngineServiceError("Goal Test cwd는 프로젝트 내부여야 합니다.")
            step = step.model_copy(update={"working_directory": str(cwd)})
            if set(step.required_evidence_kinds) & {"file", "diff"}:
                step = step.model_copy(update={"artifact_paths": normalize_artifact_paths(root, step.artifact_paths)})
        binding = GoalValidationBinding(plan_revision_id=plan.plan_revision_id,
                                        plan_activation_digest=plan.activation_digest,
                                        context_digest=sha256_digest(context), step=step,
                                        model_inventory_digest=(runtime.list_models().inventory_digest if step.method == "semantic" else None),
                                        role_configuration_digest=(None if provider is None else provider.roles.configuration_digest))
        with service.ledger.transaction() as tx:
            tx.history(project_id, "goal_test.bound", "goal_validation_binding", new_id("goal_binding"),
                       binding.model_dump(mode="json"))
        return RunOnceOutcome(action=RunOnceAction.MATERIALIZED, project_id=project_id,
                              detail=f"독립 Goal Test 실행 binding을 고정했습니다: {step.validation_id}")
    if binding.plan_activation_digest != plan.activation_digest or binding.context_digest != sha256_digest(context):
        return blocked(project_id, "STALE_EXECUTION_INPUT", "Goal Test materialization 이후 입력이 바뀌었습니다.")
    if binding.step.method == "deterministic":
        return run_command_validation(
            service, {"project_id": project_id, "id": None, "plan_revision_id": plan.plan_revision_id,
                      "goal_binding": binding.model_dump(mode="json")}, binding.step, fault_hook=fault_hook,
        )
    if binding.step.method == "semantic" and provider is not None:
        if binding.role_configuration_digest != provider.roles.configuration_digest:
            return blocked(project_id, "MODEL_BINDING_CHANGED", "Goal Test의 역할 설정이 바뀌었습니다.")
        inventory = runtime.list_models()
        if binding.model_inventory_digest != inventory.inventory_digest:
            return blocked(project_id, "MODEL_INVENTORY_CHANGED", "Goal Test의 model inventory가 바뀌었습니다.")
        with service.ledger.read() as connection:
            rows = connection.execute(
                "SELECT e.* FROM evidence_records e JOIN task_contracts t ON t.id = e.task_id "
                "WHERE e.project_id = ? AND t.plan_revision_id = ? ORDER BY e.observed_at",
                (project_id, plan.plan_revision_id),
            ).fetchall()
        catalog = {row["id"]: json.loads(row["payload_json"]) for row in rows}
        if not catalog:
            return blocked(project_id, "GOAL_TEST_INPUT_INCOMPLETE", "독립 검사에 필요한 직접 evidence가 없습니다.")
        try:
            result = provider.validate_goal(project_id=project_id, inventory=inventory,
                                            context=context, evidence_catalog=catalog, step=binding.step)
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
            source_ref=f"codex-validator:{receipt['thread_id']}", observation=observed.model_dump_json()[:10_000],
            content_digest=sha256_digest({"observation": observed, "receipt": receipt}),
            observed_at=observed.observed_at,
        )
        service.record_evidence(evidence)
        evidence_ids = (evidence.evidence_id, *observed.evidence_refs)
        validation = ValidationResult(
            validation_result_id=new_id("validation_result"), validation_id=contract.validation_id,
            status=ValidationStatus.PASS if observed.passed else ValidationStatus.FAIL,
            evidence_ids=evidence_ids, rationale=observed.rationale, evaluated_at=observed.observed_at,
        )
        service.record_validation(project_id=project_id, plan_revision_id=plan.plan_revision_id, result=validation)
        return RunOnceOutcome(action=RunOnceAction.VALIDATED, project_id=project_id,
                              validation_result_id=validation.validation_result_id, evidence_ids=evidence_ids,
                              detail="독립 Goal Validator의 typed observation을 기록했습니다.")
    return blocked(project_id, "GOAL_VALIDATION_OBSERVATION_REQUIRED",
                   f"독립 {binding.step.method} typed observation이 필요합니다: {contract.validation_id}")
