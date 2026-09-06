"""활성 Plan envelope 안의 사용자 선택을 새 실행 binding으로 원자적으로 기록한다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .domain import (
    AttemptKind,
    AttemptRecord,
    AttemptStatus,
    EngineModel,
    PlanContractRevision,
    ProjectProfileRevision,
    TaskExecutionSpecRevision,
    new_id,
    utc_now,
)
from .ledger import SQLiteEngineLedger
from .model_lock import ModelChoice, ModelInventory
from .models import AssignmentResolutionError, AssignmentResolver
from .worker_prompt import PromptArtifactStore, assemble_worker_prompt


_DIGEST = r"^sha256:[0-9a-f]{64}$"
_ENTITY_ID = r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$"
_ACTIVE_ATTEMPT_STATUSES = ("reserved", "starting", "running")


class ModelRebindingError(RuntimeError):
    """재결속이 권위 계약이나 현재 외부 효과 상태와 맞지 않을 때 발생한다."""


class ModelRebindRequest(EngineModel):
    project_id: str = Field(pattern=_ENTITY_ID)
    plan_revision_id: str = Field(pattern=_ENTITY_ID)
    plan_activation_digest: str = Field(pattern=_DIGEST)
    task_id: str = Field(pattern=_ENTITY_ID)
    current_execution_spec_revision_id: str = Field(pattern=_ENTITY_ID)
    current_execution_spec_digest: str = Field(pattern=_DIGEST)
    role: Literal["executor", "validator"]
    selection: ModelChoice
    reason: str = Field(min_length=1, max_length=3000)

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)


class ModelRebindRecord(EngineModel):
    selection_id: str = Field(pattern=_ENTITY_ID)
    request: ModelRebindRequest
    request_digest: str = Field(pattern=_DIGEST)
    inventory_digest: str = Field(pattern=_DIGEST)
    operational_lock_digest: str = Field(pattern=_DIGEST)
    new_execution_spec_revision_id: str = Field(pattern=_ENTITY_ID)
    new_execution_spec_digest: str = Field(pattern=_DIGEST)
    attempt_id: str = Field(pattern=_ENTITY_ID)
    attempt_kind: AttemptKind

    @model_validator(mode="after")
    def record_is_bound(self) -> "ModelRebindRecord":
        if self.request_digest != self.request.request_digest:
            raise ValueError("MODEL_REBIND_REQUEST_DIGEST_MISMATCH")
        return self


class ModelRebindResult(EngineModel):
    record: ModelRebindRecord
    execution_spec: TaskExecutionSpecRevision
    attempt: AttemptRecord

    @model_validator(mode="after")
    def result_is_bound(self) -> "ModelRebindResult":
        if (
            self.record.new_execution_spec_revision_id
            != self.execution_spec.execution_spec_revision_id
            or self.record.new_execution_spec_digest
            != self.execution_spec.definition_digest
            or self.record.attempt_id != self.attempt.attempt_id
            or self.record.attempt_kind != self.attempt.kind
            or self.execution_spec.definition_digest != self.attempt.execution_spec_digest
        ):
            raise ValueError("MODEL_REBIND_RESULT_BINDING_MISMATCH")
        return self


class ModelRebindingService:
    """새 ExecutionSpec·Attempt·선택 이력을 한 SQLite transaction에 결속한다."""

    def __init__(
        self,
        ledger: SQLiteEngineLedger,
        *,
        assignment_resolver: AssignmentResolver | None = None,
    ) -> None:
        self.ledger = ledger
        self.assignment_resolver = assignment_resolver or AssignmentResolver()

    def rebind_and_reserve(
        self,
        request: ModelRebindRequest,
        inventory: ModelInventory,
    ) -> ModelRebindResult:
        # model_construct/model_copy로 strict model/list v2 검사를 우회하지 못하게 재검증한다.
        request = ModelRebindRequest.model_validate_json(request.model_dump_json())
        inventory = ModelInventory.model_validate_json(inventory.model_dump_json())
        if inventory.executable_digest is None:
            raise ModelRebindingError("MODEL_LOCK_V2_RUNTIME_IDENTITY_REQUIRED")

        with self.ledger.transaction() as tx:
            self._require_schema(tx.connection)
            task_row = tx.one("SELECT * FROM task_contracts WHERE id = ?", (request.task_id,))
            if (
                task_row["project_id"] != request.project_id
                or task_row["plan_revision_id"] != request.plan_revision_id
            ):
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: project/Plan/Task")
            self._reject_unresolved_effects(tx, request.project_id)
            project_row = tx.one("SELECT * FROM projects WHERE id = ?", (request.project_id,))
            if (
                project_row["active_plan_revision_id"] != request.plan_revision_id
                or project_row["run_state"] == "recovery_required"
            ):
                raise ModelRebindingError("MODEL_REBIND_EFFECT_UNRESOLVED: active Plan/recovery 상태")
            plan_row = tx.one(
                "SELECT * FROM plan_revisions WHERE id = ? AND project_id = ?",
                (request.plan_revision_id, request.project_id),
            )
            if plan_row["status"] != "active":
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: Plan이 active가 아닙니다.")
            plan = PlanContractRevision.model_validate_json(plan_row["payload_json"])
            if request.plan_activation_digest != plan.activation_digest:
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: Plan activation digest")

            spec_row = tx.one(
                "SELECT * FROM execution_spec_revisions WHERE task_id = ? AND is_current = 1",
                (request.task_id,),
            )
            if (
                spec_row["id"] != request.current_execution_spec_revision_id
                or spec_row["definition_digest"] != request.current_execution_spec_digest
            ):
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: current ExecutionSpec")
            current_spec = TaskExecutionSpecRevision.model_validate_json(spec_row["payload_json"])
            if (
                current_spec.definition.plan_activation_digest != plan.activation_digest
                or current_spec.definition.task_contract_digest != task_row["contract_digest"]
            ):
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: ExecutionSpec authority")

            task = next((item for item in plan.definition.tasks if item.task_id == request.task_id), None)
            if task is None or task.contract_digest != task_row["contract_digest"]:
                raise ModelRebindingError("MODEL_REBIND_BINDING_MISMATCH: Plan Task contract")
            attempt_kind = self._require_role_state(
                tx,
                task_row=task_row,
                task=task,
                current_spec_digest=current_spec.definition_digest,
                role=request.role,
            )

            executor_choice = ModelChoice(
                model=current_spec.definition.executor.model,
                effort=current_spec.definition.executor.effort,
            )
            validator_choice = (
                ModelChoice(
                    model=current_spec.definition.validator.model,
                    effort=current_spec.definition.validator.effort,
                )
                if current_spec.definition.validator is not None
                else None
            )
            if request.role == "executor":
                self._verify_execution_inputs(tx, project_row, current_spec)
                self._reject_irreversible_checkpoint_gap(task)
                executor_choice = request.selection
            elif task.assignment.validator is None or validator_choice is None:
                raise ModelRebindingError(
                    "PLAN_MODEL_REVISION_REQUIRED: validator가 없는 Task에는 validator를 재결속할 수 없습니다."
                )
            else:
                validator_choice = request.selection
            try:
                executor, validator = self.assignment_resolver.resolve_contract(
                    task.assignment,
                    inventory,
                    executor_selection=executor_choice,
                    validator_selection=validator_choice,
                )
            except AssignmentResolutionError as error:
                raise ModelRebindingError(str(error)) from error
            selected = executor if request.role == "executor" else validator
            if selected is None or selected.operational_binding is None:
                raise ModelRebindingError("MODEL_REBIND_OPERATIONAL_BINDING_MISSING")

            definition = current_spec.definition.model_copy(
                update={
                    "executor": executor,
                    "validator": validator,
                    "idempotency_key": "model-rebind-" + request.request_digest.split(":", 1)[1],
                }
            )
            if request.role == "executor":
                profile_row = tx.one(
                    "SELECT r.payload_json FROM profile_revisions r "
                    "JOIN projects p ON p.active_profile_revision_id = r.id WHERE p.id = ?",
                    (request.project_id,),
                )
                profile = ProjectProfileRevision.model_validate_json(profile_row["payload_json"])
                bundle = assemble_worker_prompt(
                    task=task,
                    definition=definition,
                    profile=profile.definition,
                    root=Path(project_row["root"]),
                )
                definition = definition.model_copy(
                    update={
                        "context_manifest": definition.context_manifest.model_copy(
                            update={"prompt_binding": bundle.binding}
                        )
                    }
                )
                # binding을 넣은 뒤에도 자기참조 제외 projection이 같은 본문을 만드는지 확인한다.
                final_bundle = assemble_worker_prompt(
                    task=task,
                    definition=definition,
                    profile=profile.definition,
                    root=Path(project_row["root"]),
                )
                if final_bundle != bundle:
                    raise ModelRebindingError("MODEL_REBIND_PROMPT_BINDING_MISMATCH")
                PromptArtifactStore(self.ledger.artifact_root).put(final_bundle)

            new_spec = TaskExecutionSpecRevision(
                execution_spec_revision_id=new_id("execution_spec"),
                task_id=request.task_id,
                revision_no=int(spec_row["revision_no"]) + 1,
                definition=definition,
                definition_digest=definition.definition_digest,
                supersedes_execution_spec_revision_id=current_spec.execution_spec_revision_id,
                created_at=utc_now(),
            )
            attempt_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts "
                    "WHERE task_id = ? AND kind = ?",
                    (request.task_id, attempt_kind.value),
                ).fetchone()[0]
            )
            attempt = AttemptRecord(
                attempt_id=new_id("attempt"),
                task_id=request.task_id,
                execution_spec_digest=new_spec.definition_digest,
                attempt_no=attempt_no,
                kind=attempt_kind,
                status=AttemptStatus.RESERVED,
            )
            selection_id = new_id("model_rebinding")
            record = ModelRebindRecord(
                selection_id=selection_id,
                request=request,
                request_digest=request.request_digest,
                inventory_digest=inventory.inventory_digest,
                operational_lock_digest=selected.operational_binding.lock_digest,
                new_execution_spec_revision_id=new_spec.execution_spec_revision_id,
                new_execution_spec_digest=new_spec.definition_digest,
                attempt_id=attempt.attempt_id,
                attempt_kind=attempt_kind,
            )

            tx.connection.execute(
                "UPDATE execution_spec_revisions SET is_current = 0 WHERE id = ?",
                (current_spec.execution_spec_revision_id,),
            )
            tx.connection.execute(
                "INSERT INTO execution_spec_revisions "
                "(id, task_id, revision_no, definition_digest, payload_json, supersedes_id, "
                "created_at, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    new_spec.execution_spec_revision_id,
                    new_spec.task_id,
                    new_spec.revision_no,
                    new_spec.definition_digest,
                    canonical_json(new_spec),
                    new_spec.supersedes_execution_spec_revision_id,
                    new_spec.created_at.isoformat(),
                ),
            )
            now = tx.now
            tx.connection.execute(
                "INSERT INTO attempts "
                "(id, project_id, plan_revision_id, task_id, execution_spec_digest, attempt_no, "
                "kind, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                "'reserved', ?, ?)",
                (
                    attempt.attempt_id,
                    request.project_id,
                    request.plan_revision_id,
                    request.task_id,
                    new_spec.definition_digest,
                    attempt.attempt_no,
                    attempt_kind.value,
                    now,
                    now,
                ),
            )
            tx.connection.execute(
                "INSERT INTO model_rebinding_selections "
                "(id, project_id, plan_revision_id, task_id, role, request_digest, "
                "plan_activation_digest, previous_execution_spec_digest, selected_model, "
                "selected_effort, reason, inventory_digest, operational_lock_digest, "
                "new_execution_spec_revision_id, new_execution_spec_digest, attempt_id, "
                "payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    selection_id,
                    request.project_id,
                    request.plan_revision_id,
                    request.task_id,
                    request.role,
                    request.request_digest,
                    request.plan_activation_digest,
                    request.current_execution_spec_digest,
                    request.selection.model,
                    request.selection.effort,
                    request.reason,
                    inventory.inventory_digest,
                    selected.operational_binding.lock_digest,
                    new_spec.execution_spec_revision_id,
                    new_spec.definition_digest,
                    attempt.attempt_id,
                    canonical_json(record),
                    now,
                ),
            )
            tx.connection.execute(
                "UPDATE task_contracts SET status = 'reserved', updated_at = ? WHERE id = ?",
                (now, request.task_id),
            )
            tx.history(
                request.project_id,
                "model.rebound",
                "model_rebinding_selection",
                selection_id,
                {
                    "request_digest": request.request_digest,
                    "task_id": request.task_id,
                    "role": request.role,
                    "previous_execution_spec_digest": request.current_execution_spec_digest,
                    "new_execution_spec_digest": new_spec.definition_digest,
                    "attempt_id": attempt.attempt_id,
                    "attempt_kind": attempt_kind.value,
                },
            )
            tx.history(
                request.project_id,
                "attempt.reserved",
                "attempt",
                attempt.attempt_id,
                {
                    "task_id": request.task_id,
                    "attempt_no": attempt.attempt_no,
                    "kind": attempt_kind.value,
                },
            )
            return ModelRebindResult(record=record, execution_spec=new_spec, attempt=attempt)

    @staticmethod
    def _require_schema(connection) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'model_rebinding_selections'"
        ).fetchone()
        if row is None:
            raise ModelRebindingError("MODEL_REBIND_SCHEMA_REQUIRED: Engine schema revision 3이 필요합니다.")

    @staticmethod
    def _reject_unresolved_effects(tx, project_id: str) -> None:
        active = tx.connection.execute(
            "SELECT id FROM attempts WHERE project_id = ? AND status IN ('reserved','starting','running') "
            "LIMIT 1",
            (project_id,),
        ).fetchone()
        if active is not None:
            raise ModelRebindingError("MODEL_REBIND_EFFECT_UNRESOLVED: active Attempt가 있습니다.")
        unknown = tx.connection.execute(
            "SELECT a.id FROM attempts a LEFT JOIN runtime_intents i ON i.attempt_id = a.id "
            "WHERE a.project_id = ? AND (a.status = 'unknown' "
            "OR i.status IN ('prepared','unknown')) LIMIT 1",
            (project_id,),
        ).fetchone()
        if unknown is not None:
            raise ModelRebindingError(
                "MODEL_REBIND_EFFECT_UNRESOLVED: prepared/unknown runtime 효과가 있습니다."
            )
        provider = tx.connection.execute(
            "SELECT id FROM provider_calls WHERE project_id = ? AND status = 'reserved' LIMIT 1",
            (project_id,),
        ).fetchone()
        if provider is not None:
            raise ModelRebindingError(
                "MODEL_REBIND_EFFECT_UNRESOLVED: reserved provider call이 있습니다."
            )

    @classmethod
    def _require_role_state(
        cls,
        tx,
        *,
        task_row,
        task,
        current_spec_digest: str,
        role: str,
    ) -> AttemptKind:
        if role == "executor":
            if task_row["status"] != "materialized":
                raise ModelRebindingError(
                    "MODEL_REBIND_STATE_INVALID: executor는 Worker 실행 전 materialized Task에서만 "
                    "재결속할 수 있습니다."
                )
            return AttemptKind.EXECUTION

        if not any(step.method == "semantic" for step in task.validations):
            raise ModelRebindingError(
                "PLAN_MODEL_REVISION_REQUIRED: semantic validation이 없는 Task의 validator는 "
                "재결속할 수 없습니다."
            )
        worker = cls._successful_worker_attempt(tx, task_row["id"], current_spec_digest)
        if worker is None:
            raise ModelRebindingError(
                "MODEL_REBIND_STATE_INVALID: 성공한 Worker 원본이 없는 Task는 validator로 "
                "재결속할 수 없습니다."
            )
        latest_validation_attempt = tx.connection.execute(
            "SELECT id, status FROM attempts WHERE task_id = ? AND kind = 'validation' "
            "ORDER BY attempt_no DESC LIMIT 1",
            (task_row["id"],),
        ).fetchone()
        latest_result = tx.connection.execute(
            "SELECT payload_json FROM validation_results WHERE task_id = ? "
            "ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
            (task_row["id"],),
        ).fetchone()
        latest_result_document = (
            json.loads(latest_result["payload_json"]) if latest_result is not None else None
        )
        status = task_row["status"]
        if status == "validating":
            if (
                latest_result_document is not None
                and latest_result_document.get("status") == "fail"
            ):
                raise ModelRebindingError(
                    "MODEL_REBIND_STATE_INVALID: 실패 validation 결과를 먼저 Core 상태에 "
                    "반영해야 합니다."
                )
            return AttemptKind.VALIDATION
        if (
            status == "failed"
            and latest_validation_attempt is not None
            and latest_validation_attempt["status"] == "failed"
        ):
            return AttemptKind.VALIDATION
        if (
            status == "blocked"
            and latest_validation_attempt is not None
            and latest_validation_attempt["status"] == "succeeded"
            and latest_result_document is not None
        ):
            semantic_ids = {
                step.validation_id for step in task.validations if step.method == "semantic"
            }
            evidence_ids = tuple(latest_result_document.get("evidence_ids", ()))
            model_review_bound = False
            if evidence_ids:
                placeholders = ",".join("?" for _ in evidence_ids)
                model_review_bound = tx.connection.execute(
                    f"SELECT 1 FROM evidence_records WHERE id IN ({placeholders}) "
                    "AND attempt_id = ? AND kind = 'model_review' LIMIT 1",
                    (*evidence_ids, latest_validation_attempt["id"]),
                ).fetchone() is not None
            if (
                latest_result_document.get("status") == "fail"
                and latest_result_document.get("validation_id") in semantic_ids
                and model_review_bound
            ):
                return AttemptKind.VALIDATION
        raise ModelRebindingError(
            "MODEL_REBIND_STATE_INVALID: validator는 성공한 Worker 뒤 validating 상태 또는 "
            "실패한 validator의 failed/blocked 상태에서만 재결속할 수 있습니다."
        )

    @staticmethod
    def _successful_worker_attempt(tx, task_id: str, current_spec_digest: str):
        """validator-only Spec 계보를 거슬러 성공한 Worker Attempt를 찾는다."""

        digest = current_spec_digest
        seen: set[str] = set()
        while digest not in seen:
            seen.add(digest)
            worker = tx.connection.execute(
                "SELECT id, execution_spec_digest FROM attempts WHERE task_id = ? "
                "AND kind = 'execution' AND status = 'succeeded' "
                "AND execution_spec_digest = ? ORDER BY attempt_no DESC LIMIT 1",
                (task_id, digest),
            ).fetchone()
            if worker is not None:
                return worker
            selection = tx.connection.execute(
                "SELECT s.previous_execution_spec_digest FROM model_rebinding_selections s "
                "JOIN attempts a ON a.id = s.attempt_id AND a.task_id = s.task_id "
                "AND a.kind = 'validation' "
                "AND a.execution_spec_digest = s.new_execution_spec_digest "
                "WHERE s.task_id = ? AND s.role = 'validator' "
                "AND s.new_execution_spec_digest = ? "
                "ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1",
                (task_id, digest),
            ).fetchone()
            if selection is None:
                return None
            digest = selection["previous_execution_spec_digest"]
        return None

    @staticmethod
    def _verify_execution_inputs(tx, project_row, spec: TaskExecutionSpecRevision) -> None:
        current_map = tx.one(
            "SELECT revision_digest FROM project_map_revisions WHERE project_id = ? AND is_current = 1",
            (project_row["id"],),
        )
        if current_map["revision_digest"] != spec.definition.project_map_digest:
            raise ModelRebindingError("STALE_EXECUTION_INPUT: Project Map이 변경됐습니다.")
        snapshot = tx.one(
            "SELECT is_current FROM state_snapshots WHERE project_id = ? AND snapshot_digest = ?",
            (project_row["id"], spec.definition.snapshot_digest),
        )
        if snapshot["is_current"] != 1:
            raise ModelRebindingError("STALE_EXECUTION_INPUT: StateSnapshot이 변경됐습니다.")
        checks: dict[str, str] = {}
        for target in spec.definition.resolved_targets:
            if target.expected_content_digest is not None:
                checks[target.path] = target.expected_content_digest
        for fragment in spec.definition.context_manifest.fragments:
            if fragment.source_kind.value in {"code", "test", "reference", "policy", "project"}:
                checks[fragment.source_ref] = fragment.content_digest
        root = Path(project_row["root"])
        for raw_path, expected in checks.items():
            path = Path(raw_path)
            if not path.is_absolute():
                path = root / path
            try:
                actual = sha256_bytes(path.read_bytes())
            except OSError as error:
                raise ModelRebindingError(
                    f"STALE_EXECUTION_INPUT: 파일을 확인할 수 없습니다: {raw_path}"
                ) from error
            if actual != expected:
                raise ModelRebindingError(f"STALE_EXECUTION_INPUT: 파일이 바뀌었습니다: {raw_path}")

    @staticmethod
    def _reject_irreversible_checkpoint_gap(task) -> None:
        if any(item.external and not item.reversible for item in task.expected_effects):
            raise ModelRebindingError(
                "MODEL_REBIND_EFFECT_CHECKPOINT_REQUIRED: 새 ExecutionSpec digest에 대한 "
                "실행 직전 checkpoint 뒤 Attempt를 예약해야 합니다."
            )
