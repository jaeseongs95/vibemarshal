from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..canonical import canonical_json, sha256_bytes, sha256_digest
from .domain import (
    ACTIVE_ATTEMPT_STATUSES,
    ActivationSource,
    Assignment,
    AttemptReservation,
    AttemptStatus,
    CoreDomainError,
    EffectiveWorkItemStatus,
    PlanDraft,
    PlanRevisionStatus,
    ProjectDefinition,
    RuntimeBindingReceipt,
    RuntimeIntentKind,
    RuntimeIntentStatus,
    StoredWorkItemStatus,
    ValidationResultInput,
    ValidationStatus,
    new_id,
    validate_id,
)
from .ledger import CoreTransaction, SQLiteCoreLedger


@dataclass(frozen=True)
class ActivationResult:
    project_id: str
    revision_id: str
    canonical_digest: str
    activation_source: str
    superseded_revision_id: str | None
    idempotent: bool = False


def _canonical_path(path: Path | str, *, strict: bool) -> str:
    resolved = Path(path).resolve(strict=strict)
    return os.path.normcase(os.path.normpath(str(resolved)))


def _same_path(left: Path | str, right: Path | str) -> bool:
    return _canonical_path(left, strict=False) == _canonical_path(right, strict=False)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


class FlowMarshalCore:
    """사용자 활성화와 오케스트레이션 상태만 권위적으로 변경하는 MVP Core.

    이 클래스는 HumanControlAuthority, HMAC proof, AccessGrant에 의존하지 않는다.
    계획 승인은 정확한 revision digest를 대상으로 한 ``activate_plan`` 호출이다.
    """

    def __init__(self, ledger: SQLiteCoreLedger) -> None:
        self.ledger = ledger

    def register_project(self, definition: ProjectDefinition) -> str:
        root_path = Path(definition.root).resolve(strict=True)
        if not root_path.is_dir():
            raise CoreDomainError("프로젝트 root는 존재하는 디렉터리여야 합니다.")
        ledger_path = self.ledger.path.resolve(strict=False)
        if _is_within(ledger_path, root_path):
            raise CoreDomainError("Core 원장은 Worker 프로젝트 root 밖에 있어야 합니다.")
        sources = tuple(
            _canonical_path(source, strict=True) for source in definition.context_sources
        )
        normalized = ProjectDefinition(
            name=definition.name,
            root=_canonical_path(root_path, strict=True),
            context_sources=sources,
            default_validations=definition.default_validations,
            runtime_requirements=definition.runtime_requirements,
        )
        digest = sha256_digest(normalized)
        with self.ledger.transaction() as tx:
            existing = tx.connection.execute(
                "SELECT id, definition_digest FROM projects WHERE root = ?",
                (normalized.root,),
            ).fetchone()
            if existing is not None:
                if existing["definition_digest"] != digest:
                    raise CoreDomainError(
                        "같은 root가 다른 Project 정의로 이미 등록돼 있습니다."
                    )
                return str(existing["id"])
            project_id = new_id("project")
            now = tx.now
            tx.connection.execute(
                "INSERT INTO projects "
                "(id, name, root, context_sources_json, default_validations_json, "
                "runtime_requirements_json, definition_digest, run_state, recovery_reason, "
                "active_revision_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?)",
                (
                    project_id,
                    normalized.name,
                    normalized.root,
                    canonical_json(normalized.context_sources),
                    canonical_json(normalized.default_validations),
                    canonical_json(normalized.runtime_requirements),
                    digest,
                    now,
                    now,
                ),
            )
            tx.history(
                project_id,
                "project.registered",
                "project",
                project_id,
                {"definition_digest": digest, "root": normalized.root},
            )
            return project_id

    def create_plan_draft(self, draft: PlanDraft) -> str:
        validate_id(draft.project_id)
        digest = draft.canonical_digest
        with self.ledger.transaction() as tx:
            tx.one("SELECT id FROM projects WHERE id = ?", (draft.project_id,))
            existing = tx.connection.execute(
                "SELECT id FROM plan_revisions WHERE project_id = ? AND canonical_digest = ?",
                (draft.project_id, digest),
            ).fetchone()
            if existing is not None:
                return str(existing["id"])
            if draft.parent_revision_id is not None:
                parent = tx.one(
                    "SELECT project_id FROM plan_revisions WHERE id = ?",
                    (draft.parent_revision_id,),
                )
                if parent["project_id"] != draft.project_id:
                    raise CoreDomainError("parent revision이 다른 프로젝트에 속합니다.")
            revision_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM plan_revisions "
                    "WHERE project_id = ?",
                    (draft.project_id,),
                ).fetchone()[0]
            )
            revision_id = new_id("revision")
            now = tx.now
            tx.connection.execute(
                "INSERT INTO plan_revisions "
                "(id, project_id, revision_no, parent_revision_id, status, canonical_digest, "
                "request_summary, snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)",
                (
                    revision_id,
                    draft.project_id,
                    revision_no,
                    draft.parent_revision_id,
                    digest,
                    draft.request_summary,
                    canonical_json(draft),
                    now,
                ),
            )
            by_ref: dict[str, str] = {}
            for position, definition in enumerate(draft.work_items):
                work_item_id = new_id("work_item")
                by_ref[definition.client_ref] = work_item_id
                assignment_json = (
                    None
                    if definition.assignment is None
                    else canonical_json(definition.assignment)
                )
                tx.connection.execute(
                    "INSERT INTO work_items "
                    "(id, project_id, revision_id, client_ref, position, title, objective, "
                    "stored_status, definition_digest, definition_json, assignment_json, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                    (
                        work_item_id,
                        draft.project_id,
                        revision_id,
                        definition.client_ref,
                        position,
                        definition.title,
                        definition.objective,
                        definition.definition_digest,
                        canonical_json(definition),
                        assignment_json,
                        now,
                        now,
                    ),
                )
            for definition in draft.work_items:
                for dependency in definition.dependencies:
                    tx.connection.execute(
                        "INSERT INTO work_item_dependencies "
                        "(revision_id, work_item_id, depends_on_work_item_id) VALUES (?, ?, ?)",
                        (
                            revision_id,
                            by_ref[definition.client_ref],
                            by_ref[dependency],
                        ),
                    )
            tx.history(
                draft.project_id,
                "plan.drafted",
                "plan_revision",
                revision_id,
                {
                    "canonical_digest": digest,
                    "parent_revision_id": draft.parent_revision_id,
                    "revision_no": revision_no,
                    "work_item_count": len(draft.work_items),
                },
            )
            return revision_id

    def activate_plan(
        self,
        revision_id: str,
        *,
        expected_digest: str,
        source: ActivationSource | str,
    ) -> ActivationResult:
        validate_id(revision_id)
        activation_source = ActivationSource(source)
        with self.ledger.transaction() as tx:
            revision = tx.one(
                "SELECT * FROM plan_revisions WHERE id = ?", (revision_id,)
            )
            if revision["canonical_digest"] != expected_digest:
                raise CoreDomainError("활성화 대상 revision digest가 요청과 다릅니다.")
            project = tx.one(
                "SELECT * FROM projects WHERE id = ?", (revision["project_id"],)
            )
            if (
                revision["status"] == PlanRevisionStatus.ACTIVE.value
                and project["active_revision_id"] == revision_id
            ):
                return ActivationResult(
                    project_id=revision["project_id"],
                    revision_id=revision_id,
                    canonical_digest=expected_digest,
                    activation_source=str(revision["activation_source"]),
                    superseded_revision_id=None,
                    idempotent=True,
                )
            if revision["status"] != PlanRevisionStatus.DRAFT.value:
                raise CoreDomainError("draft revision만 활성화할 수 있습니다.")
            missing_assignment = tx.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE revision_id = ? "
                "AND assignment_json IS NULL",
                (revision_id,),
            ).fetchone()[0]
            if missing_assignment:
                raise CoreDomainError("모든 WorkItem에 확정 assignment가 있어야 합니다.")
            active_id = project["active_revision_id"]
            if active_id is not None:
                if revision["parent_revision_id"] != active_id:
                    raise CoreDomainError(
                        "새 revision의 parent가 현재 active revision과 다릅니다."
                    )
                active_attempts = tx.connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE project_id = ? "
                    "AND status IN ('reserved','running','awaiting_validation')",
                    (revision["project_id"],),
                ).fetchone()[0]
                if active_attempts:
                    raise CoreDomainError(
                        "실행 중 Attempt가 있는 동안 새 revision을 활성화할 수 없습니다."
                    )
            now = tx.now
            if active_id is not None:
                tx.connection.execute(
                    "UPDATE plan_revisions SET status = 'superseded', superseded_at = ? "
                    "WHERE id = ?",
                    (now, active_id),
                )
                superseded_work_items = tx.connection.execute(
                    "UPDATE work_items SET stored_status = 'superseded', updated_at = ? "
                    "WHERE revision_id = ? AND stored_status <> 'completed'",
                    (now, active_id),
                ).rowcount
                tx.history(
                    revision["project_id"],
                    "plan.superseded",
                    "plan_revision",
                    active_id,
                    {
                        "superseded_by": revision_id,
                        "superseded_work_item_count": superseded_work_items,
                    },
                )
            tx.connection.execute(
                "UPDATE plan_revisions SET status = 'active', activated_at = ?, "
                "activation_source = ? WHERE id = ?",
                (now, activation_source.value, revision_id),
            )
            tx.connection.execute(
                "UPDATE projects SET active_revision_id = ?, updated_at = ? WHERE id = ?",
                (revision_id, now, revision["project_id"]),
            )
            tx.history(
                revision["project_id"],
                "plan.activated",
                "plan_revision",
                revision_id,
                {
                    "canonical_digest": expected_digest,
                    "activation_source": activation_source.value,
                    "superseded_revision_id": active_id,
                },
            )
            return ActivationResult(
                project_id=revision["project_id"],
                revision_id=revision_id,
                canonical_digest=expected_digest,
                activation_source=activation_source.value,
                superseded_revision_id=active_id,
            )

    def list_work_items(self, project_id: str) -> tuple[dict[str, Any], ...]:
        validate_id(project_id)
        with self.ledger.raw_connection() as connection:
            project = connection.execute(
                "SELECT active_revision_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise CoreDomainError("프로젝트를 찾을 수 없습니다.")
            revision_id = project["active_revision_id"]
            if revision_id is None:
                return ()
            rows = connection.execute(
                "SELECT * FROM work_items WHERE revision_id = ? ORDER BY position",
                (revision_id,),
            ).fetchall()
            dependency_rows = connection.execute(
                "SELECT d.work_item_id, d.depends_on_work_item_id, dep.client_ref, "
                "dep.stored_status FROM work_item_dependencies d "
                "JOIN work_items dep ON dep.id = d.depends_on_work_item_id "
                "WHERE d.revision_id = ? ORDER BY dep.position",
                (revision_id,),
            ).fetchall()
        dependencies: dict[str, list[dict[str, str]]] = {}
        for row in dependency_rows:
            dependencies.setdefault(row["work_item_id"], []).append(
                {
                    "work_item_id": row["depends_on_work_item_id"],
                    "client_ref": row["client_ref"],
                    "status": row["stored_status"],
                }
            )
        result: list[dict[str, Any]] = []
        for row in rows:
            deps = dependencies.get(row["id"], [])
            stored = row["stored_status"]
            effective = stored
            if stored == StoredWorkItemStatus.PENDING.value and all(
                item["status"] == StoredWorkItemStatus.COMPLETED.value for item in deps
            ):
                effective = EffectiveWorkItemStatus.READY.value
            result.append(
                {
                    "work_item_id": row["id"],
                    "revision_id": row["revision_id"],
                    "client_ref": row["client_ref"],
                    "title": row["title"],
                    "position": row["position"],
                    "stored_status": stored,
                    "effective_status": effective,
                    "dependencies": deps,
                    "definition_digest": row["definition_digest"],
                }
            )
        return tuple(result)

    def ready_work_items(self, project_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            item
            for item in self.list_work_items(project_id)
            if item["effective_status"] == EffectiveWorkItemStatus.READY.value
        )

    def reserve_attempt(
        self,
        work_item_id: str,
        *,
        thread_request: dict[str, Any],
        retry: bool = False,
    ) -> AttemptReservation:
        validate_id(work_item_id)
        with self.ledger.transaction() as tx:
            work = tx.one(
                "SELECT wi.*, p.active_revision_id, p.run_state FROM work_items wi "
                "JOIN projects p ON p.id = wi.project_id WHERE wi.id = ?",
                (work_item_id,),
            )
            if work["run_state"] != "active":
                raise CoreDomainError("프로젝트가 recovery_required 상태입니다.")
            if work["revision_id"] != work["active_revision_id"]:
                raise CoreDomainError("active revision의 WorkItem만 실행할 수 있습니다.")
            dependency_states = tx.all(
                "SELECT dep.stored_status FROM work_item_dependencies d "
                "JOIN work_items dep ON dep.id = d.depends_on_work_item_id "
                "WHERE d.work_item_id = ?",
                (work_item_id,),
            )
            dependencies_done = all(
                row["stored_status"] == StoredWorkItemStatus.COMPLETED.value
                for row in dependency_states
            )
            status = StoredWorkItemStatus(work["stored_status"])
            eligible = (
                status is StoredWorkItemStatus.PENDING and dependencies_done and not retry
            ) or (status is StoredWorkItemStatus.RETRYABLE and retry)
            if not eligible:
                raise CoreDomainError("WorkItem이 현재 새 Attempt를 예약할 수 없습니다.")
            project_active_attempt = tx.connection.execute(
                "SELECT id FROM attempts WHERE project_id = ? "
                "AND status IN ('reserved','running','awaiting_validation') LIMIT 1",
                (work["project_id"],),
            ).fetchone()
            if project_active_attempt is not None:
                raise CoreDomainError("초기 Core는 프로젝트별 active Attempt를 하나만 허용합니다.")
            if work["assignment_json"] is None:
                raise CoreDomainError("WorkItem assignment가 없습니다.")
            assignment = Assignment.model_validate_json(work["assignment_json"])
            attempt_no = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts "
                    "WHERE work_item_id = ?",
                    (work_item_id,),
                ).fetchone()[0]
            )
            attempt_id = new_id("attempt")
            now = tx.now
            tx.connection.execute(
                "INSERT INTO attempts "
                "(id, project_id, revision_id, work_item_id, attempt_no, status, "
                "model_id, reasoning_effort, validation_model_id, "
                "validation_reasoning_effort, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    work["project_id"],
                    work["revision_id"],
                    work_item_id,
                    attempt_no,
                    assignment.execution_model_id,
                    assignment.execution_reasoning_effort,
                    assignment.validation_model_id,
                    assignment.validation_reasoning_effort,
                    now,
                    now,
                ),
            )
            tx.connection.execute(
                "UPDATE work_items SET stored_status = 'dispatching', updated_at = ? "
                "WHERE id = ?",
                (now, work_item_id),
            )
            intent_id = self._create_intent(
                tx,
                project_id=work["project_id"],
                attempt_id=attempt_id,
                kind=RuntimeIntentKind.CREATE_THREAD,
                request=thread_request,
            )
            tx.history(
                work["project_id"],
                "attempt.reserved",
                "attempt",
                attempt_id,
                {
                    "attempt_no": attempt_no,
                    "intent_id": intent_id,
                    "model_id": assignment.execution_model_id,
                    "reasoning_effort": assignment.execution_reasoning_effort,
                    "work_item_id": work_item_id,
                },
            )
            return AttemptReservation(
                project_id=work["project_id"],
                revision_id=work["revision_id"],
                work_item_id=work_item_id,
                attempt_id=attempt_id,
                attempt_no=attempt_no,
                intent_id=intent_id,
                model_id=assignment.execution_model_id,
                reasoning_effort=assignment.execution_reasoning_effort,
            )

    def _create_intent(
        self,
        tx: CoreTransaction,
        *,
        project_id: str,
        attempt_id: str,
        kind: RuntimeIntentKind,
        request: dict[str, Any],
        recovery_of_intent_id: str | None = None,
    ) -> str:
        active = tx.connection.execute(
            "SELECT id FROM runtime_action_intents WHERE attempt_id = ? "
            "AND status IN ('reserved','executing') LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if active is not None:
            raise CoreDomainError("Attempt에 이미 active RuntimeActionIntent가 있습니다.")
        ordinal = int(
            tx.connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM runtime_action_intents "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()[0]
        )
        intent_id = new_id("intent")
        now = tx.now
        tx.connection.execute(
            "INSERT INTO runtime_action_intents "
            "(id, project_id, attempt_id, ordinal, kind, status, request_digest, "
            "request_json, recovery_of_intent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)",
            (
                intent_id,
                project_id,
                attempt_id,
                ordinal,
                kind.value,
                sha256_digest(request),
                canonical_json(request),
                recovery_of_intent_id,
                now,
                now,
            ),
        )
        tx.history(
            project_id,
            "intent.reserved",
            "runtime_action_intent",
            intent_id,
            {
                "attempt_id": attempt_id,
                "kind": kind.value,
                "ordinal": ordinal,
                "request_digest": sha256_digest(request),
            },
        )
        return intent_id

    def begin_intent(self, intent_id: str) -> dict[str, Any]:
        validate_id(intent_id)
        with self.ledger.transaction() as tx:
            intent = tx.one(
                "SELECT * FROM runtime_action_intents WHERE id = ?", (intent_id,)
            )
            if intent["status"] != RuntimeIntentStatus.RESERVED.value:
                raise CoreDomainError("reserved intent만 실행을 시작할 수 있습니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE runtime_action_intents SET status = 'executing', updated_at = ? "
                "WHERE id = ?",
                (now, intent_id),
            )
            tx.history(
                intent["project_id"],
                "intent.executing",
                "runtime_action_intent",
                intent_id,
                {"attempt_id": intent["attempt_id"], "kind": intent["kind"]},
            )
            return {
                "intent_id": intent_id,
                "attempt_id": intent["attempt_id"],
                "kind": intent["kind"],
                "request": json.loads(intent["request_json"]),
            }

    def record_thread_binding(
        self,
        intent_id: str,
        receipt: RuntimeBindingReceipt,
    ) -> str:
        if receipt.turn_id is not None:
            raise CoreDomainError("thread 생성 receipt에는 turn_id가 없어야 합니다.")
        with self.ledger.transaction() as tx:
            intent = tx.one(
                "SELECT i.*, p.root FROM runtime_action_intents i "
                "JOIN projects p ON p.id = i.project_id WHERE i.id = ?",
                (intent_id,),
            )
            if (
                intent["kind"] != RuntimeIntentKind.CREATE_THREAD.value
                or intent["status"] != RuntimeIntentStatus.EXECUTING.value
            ):
                raise CoreDomainError("executing CREATE_THREAD intent만 binding할 수 있습니다.")
            if not _same_path(receipt.cwd, intent["root"]):
                raise CoreDomainError("thread receipt의 cwd가 프로젝트 root와 다릅니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE runtime_action_intents SET status = 'succeeded', external_id = ?, "
                "receipt_json = ?, updated_at = ?, ended_at = ? WHERE id = ?",
                (
                    receipt.thread_id,
                    canonical_json(receipt.runtime_receipt),
                    now,
                    now,
                    intent_id,
                ),
            )
            binding_id = new_id("binding")
            tx.connection.execute(
                "INSERT INTO runtime_bindings "
                "(id, attempt_id, thread_intent_id, thread_id, cwd, "
                "instruction_sources_json, thread_receipt_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding_id,
                    intent["attempt_id"],
                    intent_id,
                    receipt.thread_id,
                    str(Path(receipt.cwd).resolve(strict=False)),
                    canonical_json(receipt.instruction_sources),
                    canonical_json(receipt.runtime_receipt),
                    now,
                    now,
                ),
            )
            tx.history(
                intent["project_id"],
                "runtime.thread_bound",
                "runtime_binding",
                binding_id,
                {
                    "attempt_id": intent["attempt_id"],
                    "instruction_sources": list(receipt.instruction_sources),
                    "thread_id": receipt.thread_id,
                    "thread_intent_id": intent_id,
                },
            )
            return binding_id

    def reserve_turn_intent(
        self,
        attempt_id: str,
        *,
        turn_request: dict[str, Any],
    ) -> str:
        validate_id(attempt_id)
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["status"] != AttemptStatus.RESERVED.value:
                raise CoreDomainError("reserved Attempt에서만 turn intent를 만들 수 있습니다.")
            binding = tx.one(
                "SELECT * FROM runtime_bindings WHERE attempt_id = ?", (attempt_id,)
            )
            if binding["turn_id"] is not None:
                raise CoreDomainError("Attempt에 이미 turn binding이 있습니다.")
            return self._create_intent(
                tx,
                project_id=attempt["project_id"],
                attempt_id=attempt_id,
                kind=RuntimeIntentKind.START_TURN,
                request=turn_request,
            )

    def record_turn_binding(
        self,
        intent_id: str,
        receipt: RuntimeBindingReceipt,
    ) -> None:
        if receipt.turn_id is None:
            raise CoreDomainError("turn receipt에는 turn_id가 필요합니다.")
        with self.ledger.transaction() as tx:
            intent = tx.one(
                "SELECT * FROM runtime_action_intents WHERE id = ?", (intent_id,)
            )
            if (
                intent["kind"] != RuntimeIntentKind.START_TURN.value
                or intent["status"] != RuntimeIntentStatus.EXECUTING.value
            ):
                raise CoreDomainError("executing START_TURN intent만 binding할 수 있습니다.")
            attempt = tx.one(
                "SELECT * FROM attempts WHERE id = ?", (intent["attempt_id"],)
            )
            binding = tx.one(
                "SELECT * FROM runtime_bindings WHERE attempt_id = ?",
                (intent["attempt_id"],),
            )
            if binding["thread_id"] != receipt.thread_id:
                raise CoreDomainError("turn receipt의 thread ID가 기존 binding과 다릅니다.")
            existing_sources = tuple(json.loads(binding["instruction_sources_json"]))
            if receipt.instruction_sources and tuple(receipt.instruction_sources) != existing_sources:
                raise CoreDomainError("turn receipt의 instructionSources가 thread와 다릅니다.")
            if not _same_path(receipt.cwd, binding["cwd"]):
                raise CoreDomainError("turn receipt의 cwd가 thread binding과 다릅니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE runtime_action_intents SET status = 'succeeded', external_id = ?, "
                "receipt_json = ?, updated_at = ?, ended_at = ? WHERE id = ?",
                (
                    receipt.turn_id,
                    canonical_json(receipt.runtime_receipt),
                    now,
                    now,
                    intent_id,
                ),
            )
            tx.connection.execute(
                "UPDATE runtime_bindings SET turn_intent_id = ?, turn_id = ?, "
                "turn_receipt_json = ?, updated_at = ? WHERE attempt_id = ?",
                (
                    intent_id,
                    receipt.turn_id,
                    canonical_json(receipt.runtime_receipt),
                    now,
                    intent["attempt_id"],
                ),
            )
            tx.connection.execute(
                "UPDATE attempts SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, intent["attempt_id"]),
            )
            tx.connection.execute(
                "UPDATE work_items SET stored_status = 'running', updated_at = ? WHERE id = ?",
                (now, attempt["work_item_id"]),
            )
            tx.history(
                intent["project_id"],
                "runtime.turn_bound",
                "runtime_binding",
                binding["id"],
                {
                    "attempt_id": intent["attempt_id"],
                    "thread_id": receipt.thread_id,
                    "turn_id": receipt.turn_id,
                    "turn_intent_id": intent_id,
                },
            )

    def mark_worker_finished(self, attempt_id: str, *, summary: str) -> None:
        validate_id(attempt_id)
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["status"] != AttemptStatus.RUNNING.value:
                raise CoreDomainError("running Attempt만 validation 대기로 전이할 수 있습니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE attempts SET status = 'awaiting_validation', summary = ?, "
                "updated_at = ? WHERE id = ?",
                (summary, now, attempt_id),
            )
            tx.connection.execute(
                "UPDATE work_items SET stored_status = 'validating', updated_at = ? WHERE id = ?",
                (now, attempt["work_item_id"]),
            )
            tx.history(
                attempt["project_id"],
                "attempt.awaiting_validation",
                "attempt",
                attempt_id,
                {"summary": summary, "work_item_id": attempt["work_item_id"]},
            )

    def record_validation_result(
        self,
        attempt_id: str,
        result: ValidationResultInput,
    ) -> str:
        validate_id(attempt_id)
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["status"] != AttemptStatus.AWAITING_VALIDATION.value:
                raise CoreDomainError(
                    "awaiting_validation Attempt에만 검사 결과를 기록할 수 있습니다."
                )
            work = tx.one(
                "SELECT definition_json FROM work_items WHERE id = ?",
                (attempt["work_item_id"],),
            )
            definition = json.loads(work["definition_json"])
            expected = {
                item["criterion_id"]: item["check_type"]
                for item in definition.get("validations", [])
            }
            if expected.get(result.criterion_id) != result.check_type:
                raise CoreDomainError("활성 WorkItem 계약에 없는 validation 결과입니다.")
            ordinal = int(
                tx.connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM validation_results "
                    "WHERE attempt_id = ? AND criterion_id = ?",
                    (attempt_id, result.criterion_id),
                ).fetchone()[0]
            )
            payload_json = canonical_json(result.evidence)
            payload_bytes = payload_json.encode("utf-8")
            evidence_id = new_id("evidence")
            evidence_kind = f"validation:{result.criterion_id}:{ordinal}"
            now = tx.now
            tx.connection.execute(
                "INSERT INTO evidence_records "
                "(id, attempt_id, evidence_kind, digest, size, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    attempt_id,
                    evidence_kind,
                    sha256_bytes(payload_bytes),
                    len(payload_bytes),
                    payload_json,
                    now,
                ),
            )
            validation_id = new_id("validation")
            tx.connection.execute(
                "INSERT INTO validation_results "
                "(id, attempt_id, criterion_id, ordinal, check_type, status, "
                "evidence_id, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    validation_id,
                    attempt_id,
                    result.criterion_id,
                    ordinal,
                    result.check_type,
                    result.status.value,
                    evidence_id,
                    result.summary,
                    now,
                ),
            )
            tx.history(
                attempt["project_id"],
                "validation.recorded",
                "validation_result",
                validation_id,
                {
                    "attempt_id": attempt_id,
                    "criterion_id": result.criterion_id,
                    "evidence_digest": sha256_bytes(payload_bytes),
                    "status": result.status.value,
                },
            )
            return validation_id

    def complete_attempt(self, attempt_id: str) -> bool:
        validate_id(attempt_id)
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if attempt["status"] != AttemptStatus.AWAITING_VALIDATION.value:
                raise CoreDomainError("awaiting_validation Attempt만 완료할 수 있습니다.")
            work = tx.one(
                "SELECT definition_json FROM work_items WHERE id = ?",
                (attempt["work_item_id"],),
            )
            definition = json.loads(work["definition_json"])
            expected = {
                item["criterion_id"] for item in definition.get("validations", [])
            }
            rows = tx.all(
                "SELECT criterion_id, status, ordinal FROM validation_results "
                "WHERE attempt_id = ? ORDER BY criterion_id, ordinal",
                (attempt_id,),
            )
            latest = {row["criterion_id"]: row["status"] for row in rows}
            if set(latest) != expected or any(
                latest[criterion] != ValidationStatus.PASSED.value
                for criterion in expected
            ):
                raise CoreDomainError("모든 validation의 최신 결과가 passed여야 합니다.")
            now = tx.now
            tx.connection.execute(
                "UPDATE attempts SET status = 'completed', ended_at = ?, updated_at = ? "
                "WHERE id = ?",
                (now, now, attempt_id),
            )
            tx.connection.execute(
                "UPDATE work_items SET stored_status = 'completed', updated_at = ? WHERE id = ?",
                (now, attempt["work_item_id"]),
            )
            tx.history(
                attempt["project_id"],
                "attempt.completed",
                "attempt",
                attempt_id,
                {"work_item_id": attempt["work_item_id"]},
            )
            remaining = tx.connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE revision_id = ? "
                "AND stored_status <> 'completed'",
                (attempt["revision_id"],),
            ).fetchone()[0]
            plan_completed = remaining == 0
            if plan_completed:
                tx.connection.execute(
                    "UPDATE plan_revisions SET status = 'completed', completed_at = ? "
                    "WHERE id = ?",
                    (now, attempt["revision_id"]),
                )
                tx.connection.execute(
                    "UPDATE projects SET active_revision_id = NULL, updated_at = ? WHERE id = ?",
                    (now, attempt["project_id"]),
                )
                tx.history(
                    attempt["project_id"],
                    "plan.completed",
                    "plan_revision",
                    attempt["revision_id"],
                    {"last_attempt_id": attempt_id},
                )
            return plan_completed

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        failure_class: str,
        summary: str,
        disposition: AttemptStatus,
    ) -> None:
        if disposition not in {
            AttemptStatus.RETRYABLE,
            AttemptStatus.BLOCKED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
        }:
            raise CoreDomainError("지원하지 않는 Attempt 종료 disposition입니다.")
        with self.ledger.transaction() as tx:
            attempt = tx.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
            if AttemptStatus(attempt["status"]) not in ACTIVE_ATTEMPT_STATUSES:
                raise CoreDomainError("active Attempt만 실패 상태로 종료할 수 있습니다.")
            executing_intent = tx.connection.execute(
                "SELECT id FROM runtime_action_intents WHERE attempt_id = ? "
                "AND status = 'executing' LIMIT 1",
                (attempt_id,),
            ).fetchone()
            if executing_intent is not None:
                raise CoreDomainError(
                    "외부 효과가 실행 중인 Attempt는 일반 종료할 수 없습니다. "
                    "먼저 crash recovery로 효과를 판정해야 합니다."
                )
            work_status = {
                AttemptStatus.RETRYABLE: StoredWorkItemStatus.RETRYABLE,
                AttemptStatus.BLOCKED: StoredWorkItemStatus.BLOCKED,
                AttemptStatus.FAILED: StoredWorkItemStatus.FAILED,
                AttemptStatus.CANCELLED: StoredWorkItemStatus.BLOCKED,
            }[disposition]
            now = tx.now
            reserved_intents = tx.all(
                "SELECT id, kind FROM runtime_action_intents WHERE attempt_id = ? "
                "AND status = 'reserved' ORDER BY ordinal",
                (attempt_id,),
            )
            for intent in reserved_intents:
                tx.connection.execute(
                    "UPDATE runtime_action_intents SET status = 'cancelled', updated_at = ?, "
                    "ended_at = ? WHERE id = ?",
                    (now, now, intent["id"]),
                )
                tx.history(
                    attempt["project_id"],
                    "intent.cancelled",
                    "runtime_action_intent",
                    intent["id"],
                    {"attempt_id": attempt_id, "kind": intent["kind"]},
                )
            tx.connection.execute(
                "UPDATE attempts SET status = ?, failure_class = ?, summary = ?, "
                "ended_at = ?, updated_at = ? WHERE id = ?",
                (
                    disposition.value,
                    failure_class,
                    summary,
                    now,
                    now,
                    attempt_id,
                ),
            )
            tx.connection.execute(
                "UPDATE work_items SET stored_status = ?, updated_at = ? WHERE id = ?",
                (work_status.value, now, attempt["work_item_id"]),
            )
            tx.history(
                attempt["project_id"],
                f"attempt.{disposition.value}",
                "attempt",
                attempt_id,
                {
                    "failure_class": failure_class,
                    "summary": summary,
                    "work_item_id": attempt["work_item_id"],
                },
            )

    def mark_executing_intents_unknown(self) -> tuple[str, ...]:
        """crash window의 외부 효과를 추측 재실행하지 않도록 격리한다."""

        unknown: list[str] = []
        with self.ledger.transaction() as tx:
            rows = tx.all(
                "SELECT i.*, a.work_item_id, a.revision_id, a.status AS attempt_status "
                "FROM runtime_action_intents i JOIN attempts a ON a.id = i.attempt_id "
                "WHERE i.status = 'executing' ORDER BY i.created_at, i.rowid"
            )
            for row in rows:
                now = tx.now
                error = {
                    "code": "EXTERNAL_EFFECT_UNKNOWN",
                    "message": "프로세스 중단 시점에 외부 효과 receipt가 없었습니다.",
                }
                tx.connection.execute(
                    "UPDATE runtime_action_intents SET status = 'unknown', error_json = ?, "
                    "updated_at = ?, ended_at = ? WHERE id = ?",
                    (canonical_json(error), now, now, row["id"]),
                )
                if row["attempt_status"] in {
                    status.value for status in ACTIVE_ATTEMPT_STATUSES
                }:
                    tx.connection.execute(
                        "UPDATE attempts SET status = 'external_unknown', failure_class = ?, "
                        "summary = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                        (
                            "external_effect_unknown",
                            error["message"],
                            now,
                            now,
                            row["attempt_id"],
                        ),
                    )
                    tx.connection.execute(
                        "UPDATE work_items SET stored_status = 'blocked', updated_at = ? "
                        "WHERE id = ?",
                        (now, row["work_item_id"]),
                    )
                tx.connection.execute(
                    "UPDATE projects SET run_state = 'recovery_required', recovery_reason = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("EXTERNAL_EFFECT_UNKNOWN", now, row["project_id"]),
                )
                tx.history(
                    row["project_id"],
                    "intent.unknown",
                    "runtime_action_intent",
                    row["id"],
                    {
                        "attempt_id": row["attempt_id"],
                        "kind": row["kind"],
                        "reason": "EXTERNAL_EFFECT_UNKNOWN",
                    },
                )
                unknown.append(row["id"])
        return tuple(unknown)

    def project_snapshot(self, project_id: str) -> dict[str, Any]:
        validate_id(project_id)
        with self.ledger.raw_connection() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise CoreDomainError("프로젝트를 찾을 수 없습니다.")
            revisions = connection.execute(
                "SELECT id, revision_no, parent_revision_id, status, canonical_digest, "
                "activated_at, activation_source, superseded_at, completed_at "
                "FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                (project_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT id, revision_id, work_item_id, attempt_no, status, model_id, "
                "reasoning_effort, failure_class, summary FROM attempts "
                "WHERE project_id = ? ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
            bindings = connection.execute(
                "SELECT b.* FROM runtime_bindings b JOIN attempts a ON a.id = b.attempt_id "
                "WHERE a.project_id = ? ORDER BY b.created_at, b.rowid",
                (project_id,),
            ).fetchall()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        return {
            "project": {
                "id": project["id"],
                "name": project["name"],
                "root": project["root"],
                "run_state": project["run_state"],
                "recovery_reason": project["recovery_reason"],
                "active_revision_id": project["active_revision_id"],
            },
            "revisions": [dict(row) for row in revisions],
            "work_items": list(self.list_work_items(project_id)),
            "attempts": [dict(row) for row in attempts],
            "bindings": [
                {
                    "id": row["id"],
                    "attempt_id": row["attempt_id"],
                    "thread_id": row["thread_id"],
                    "turn_id": row["turn_id"],
                    "cwd": row["cwd"],
                    "instruction_sources": json.loads(row["instruction_sources_json"]),
                }
                for row in bindings
            ],
            "history_count": history_count,
            "history_valid": self.ledger.verify_history(project_id),
        }
