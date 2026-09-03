from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator

from ..canonical import canonical_json, sha256_digest
from ..context import ContextBundle, RuntimeRole, Submission


GATE0C_LEDGER_SCHEMA_REVISION = 3


class Gate0CLedgerError(RuntimeError):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class TaskState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class AttemptStage(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    AWAITING_SUBMISSION = "awaiting_submission"
    AWAITING_VALIDATION = "awaiting_validation"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


class ExecutionStage(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    RECONCILED = "reconciled"


_TASK_TRANSITIONS = {
    None: {TaskState.PENDING, TaskState.READY},
    TaskState.PENDING: {TaskState.READY, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.READY: {TaskState.ACTIVE, TaskState.BLOCKED, TaskState.FAILED},
    TaskState.ACTIVE: {TaskState.COMPLETED, TaskState.FAILED, TaskState.BLOCKED},
    TaskState.FAILED: {TaskState.READY, TaskState.BLOCKED},
    TaskState.BLOCKED: {TaskState.READY, TaskState.FAILED},
    TaskState.COMPLETED: set(),
}
_ATTEMPT_TRANSITIONS = {
    None: {AttemptStage.RESERVED},
    AttemptStage.RESERVED: {AttemptStage.DISPATCHED, AttemptStage.FAILED},
    AttemptStage.DISPATCHED: {
        AttemptStage.AWAITING_SUBMISSION,
        AttemptStage.AWAITING_VALIDATION,
        AttemptStage.SUCCEEDED,
        AttemptStage.FAILED,
        AttemptStage.UNKNOWN,
    },
    AttemptStage.AWAITING_SUBMISSION: {
        AttemptStage.AWAITING_VALIDATION,
        AttemptStage.FAILED,
        AttemptStage.UNKNOWN,
    },
    AttemptStage.AWAITING_VALIDATION: {
        AttemptStage.SUCCEEDED,
        AttemptStage.FAILED,
        AttemptStage.UNKNOWN,
    },
    AttemptStage.UNKNOWN: {AttemptStage.RECONCILED, AttemptStage.FAILED},
    AttemptStage.SUCCEEDED: set(),
    AttemptStage.FAILED: set(),
    AttemptStage.RECONCILED: set(),
}
_EXECUTION_TRANSITIONS = {
    None: {ExecutionStage.RESERVED},
    ExecutionStage.RESERVED: {
        ExecutionStage.DISPATCHED,
        ExecutionStage.FAILED,
    },
    ExecutionStage.DISPATCHED: {
        ExecutionStage.SUCCEEDED,
        ExecutionStage.FAILED,
        ExecutionStage.UNKNOWN,
    },
    ExecutionStage.UNKNOWN: {ExecutionStage.RECONCILED, ExecutionStage.FAILED},
    ExecutionStage.SUCCEEDED: set(),
    ExecutionStage.FAILED: set(),
    ExecutionStage.RECONCILED: set(),
}


APPROVED_TASKS: tuple[dict[str, Any], ...] = (
    {
        "task_id": "FM-0C-1",
        "goal": "permission profile 선택과 provenance를 실제 App Server에서 검증한다.",
        "dependencies": (),
        "executor": ("gpt-5.6-sol", "high"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-2",
        "goal": "Windows path boundary와 object identity 검사를 구현한다.",
        "dependencies": ("FM-0C-1",),
        "executor": ("gpt-5.6-sol", "xhigh"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-3",
        "goal": "ContextBundle과 strict submission 계약을 구현한다.",
        "dependencies": ("FM-0C-1",),
        "executor": ("gpt-5.6-sol", "high"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-4",
        "goal": "역할별 runtime adapter와 Gate 0C 원장 binding을 구현한다.",
        "dependencies": ("FM-0C-2", "FM-0C-3"),
        "executor": ("gpt-5.6-sol", "xhigh"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-5",
        "goal": "별도 Validation Runner와 완료 판정을 연결한다.",
        "dependencies": ("FM-0C-4",),
        "executor": ("gpt-5.6-sol", "xhigh"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-6",
        "goal": "path와 prompt injection 공격 suite를 구현한다.",
        "dependencies": ("FM-0C-5",),
        "executor": ("gpt-5.6-sol", "xhigh"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-7",
        "goal": "실제 Codex 합성 E2E와 crash 회귀를 실행한다.",
        "dependencies": ("FM-0C-6",),
        "executor": ("gpt-5.6-sol", "high"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
    {
        "task_id": "FM-0C-8",
        "goal": "독립 verifier로 최종 Gate를 판정하고 문서화한다.",
        "dependencies": ("FM-0C-7",),
        "executor": ("gpt-5.6-terra", "high"),
        "validator": ("gpt-5.6-sol", "xhigh"),
    },
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_SCHEMA = f"""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS gate0c_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_revisions(
  plan_revision_id TEXT PRIMARY KEY,
  document_digest TEXT NOT NULL,
  approval_digest TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(plan_revision_id),
  definition_json TEXT NOT NULL,
  definition_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS task_events(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  state TEXT NOT NULL,
  reason_code TEXT,
  detail_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  role TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  definition_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS attempt_events(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  stage TEXT NOT NULL,
  reason_code TEXT,
  detail_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS context_bundles(
  bundle_id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  role TEXT NOT NULL,
  bundle_json TEXT NOT NULL,
  bundle_digest TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_executions(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  execution_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  role TEXT NOT NULL,
  action_kind TEXT NOT NULL,
  stage TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  profile_digest TEXT NOT NULL,
  thread_id TEXT,
  turn_id TEXT,
  receipt_digest TEXT,
  detail_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE,
  UNIQUE(execution_id, stage)
);
CREATE TABLE IF NOT EXISTS agent_submissions(
  submission_id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  bundle_id TEXT NOT NULL REFERENCES context_bundles(bundle_id),
  role TEXT NOT NULL,
  submission_json TEXT NOT NULL,
  submission_digest TEXT NOT NULL UNIQUE,
  accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
  reason_code TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS core_validations(
  validation_id TEXT PRIMARY KEY,
  runner_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  validator_attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  named_checks_digest TEXT NOT NULL,
  artifact_digest TEXT NOT NULL,
  validator_submission_digest TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS evidence_records(
  evidence_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_digest TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  row_digest TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS history(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  previous_hash TEXT,
  entry_hash TEXT NOT NULL UNIQUE,
  occurred_at TEXT NOT NULL
);
INSERT OR IGNORE INTO gate0c_meta(key,value) VALUES('schema_revision','{GATE0C_LEDGER_SCHEMA_REVISION}');
"""


_APPEND_ONLY_TABLES = (
    "plan_revisions",
    "tasks",
    "task_events",
    "attempts",
    "attempt_events",
    "context_bundles",
    "role_executions",
    "agent_submissions",
    "core_validations",
    "evidence_records",
    "history",
)


class Gate0CLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(_SCHEMA)
        revision_row = self.connection.execute(
            "SELECT value FROM gate0c_meta WHERE key='schema_revision'"
        ).fetchone()
        try:
            revision = int(revision_row[0]) if revision_row is not None else None
        except (TypeError, ValueError) as error:
            raise Gate0CLedgerError(
                "SCHEMA_REVISION_INVALID", "Gate 0C schema revision을 해석할 수 없습니다."
            ) from error
        if revision not in {2, GATE0C_LEDGER_SCHEMA_REVISION}:
            raise Gate0CLedgerError(
                "SCHEMA_REVISION_UNSUPPORTED",
                f"지원하지 않는 Gate 0C schema revision입니다: {revision}",
            )
        if revision == 2:
            self.connection.execute(
                "UPDATE gate0c_meta SET value=? WHERE key='schema_revision'",
                (str(GATE0C_LEDGER_SCHEMA_REVISION),),
            )
        for table in _APPEND_ONLY_TABLES:
            self.connection.executescript(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;
                """
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Gate0CLedger":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def _append_history(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any],
        occurred_at: str,
    ) -> str:
        last = connection.execute(
            "SELECT sequence, entry_hash FROM history ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = None if last is None else str(last["entry_hash"])
        next_sequence = 1 if last is None else int(last["sequence"]) + 1
        entry_hash = sha256_digest(
            {
                "sequence": next_sequence,
                "event_type": event_type,
                "entity_id": entity_id,
                "payload": payload,
                "previous_hash": previous_hash,
                "occurred_at": occurred_at,
            }
        )
        connection.execute(
            "INSERT INTO history(event_type,entity_id,payload_json,previous_hash,entry_hash,occurred_at) VALUES(?,?,?,?,?,?)",
            (
                event_type,
                entity_id,
                canonical_json(payload),
                previous_hash,
                entry_hash,
                occurred_at,
            ),
        )
        return entry_hash

    def import_approved_plan(self, artifact: dict[str, Any]) -> None:
        if artifact.get("status") != "approved":
            raise Gate0CLedgerError("PLAN_NOT_APPROVED", "승인된 plan revision만 가져올 수 있습니다.")
        plan_id = str(artifact.get("plan_revision_id"))
        document = artifact.get("document")
        approval = artifact.get("approval_source")
        if not isinstance(document, dict) or not isinstance(approval, dict):
            raise Gate0CLedgerError("PLAN_INVALID", "plan artifact의 document/approval이 없습니다.")
        payload_json = canonical_json(artifact)
        row_digest = sha256_digest(artifact)
        occurred_at = str(artifact.get("approved_at"))
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO plan_revisions VALUES(?,?,?,?,?,?)",
                (
                    plan_id,
                    document["sha256"],
                    sha256_digest(approval),
                    occurred_at,
                    payload_json,
                    row_digest,
                ),
            )
            self._append_history(
                connection,
                event_type="plan_revision_imported",
                entity_id=plan_id,
                payload={"row_digest": row_digest},
                occurred_at=occurred_at,
            )

    def register_approved_tasks(self, plan_revision_id: str) -> None:
        with self._transaction() as connection:
            for definition in APPROVED_TASKS:
                payload = dict(definition)
                digest = sha256_digest(payload)
                connection.execute(
                    "INSERT INTO tasks(task_id,plan_revision_id,definition_json,definition_digest) VALUES(?,?,?,?)",
                    (
                        definition["task_id"],
                        plan_revision_id,
                        canonical_json(payload),
                        digest,
                    ),
                )
                self._append_history(
                    connection,
                    event_type="task_registered",
                    entity_id=definition["task_id"],
                    payload={"definition_digest": digest},
                    occurred_at=_now(),
                )

    def _latest_value(self, table: str, id_column: str, identifier: str, column: str) -> str | None:
        row = self.connection.execute(
            f"SELECT {column} FROM {table} WHERE {id_column}=? ORDER BY sequence DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        return None if row is None else str(row[column])

    def append_task_state(
        self,
        task_id: str,
        state: TaskState,
        *,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        previous_raw = self._latest_value("task_events", "task_id", task_id, "state")
        previous = None if previous_raw is None else TaskState(previous_raw)
        if state not in _TASK_TRANSITIONS[previous]:
            raise Gate0CLedgerError(
                "INVALID_TASK_TRANSITION",
                f"task 상태 전이가 허용되지 않습니다: {previous} -> {state}",
            )
        occurred_at = _now()
        payload = {"state": state, "reason_code": reason_code, "detail": detail or {}}
        row_digest = sha256_digest({"task_id": task_id, **payload, "occurred_at": occurred_at})
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO task_events(task_id,state,reason_code,detail_json,occurred_at,row_digest) VALUES(?,?,?,?,?,?)",
                (task_id, state, reason_code, canonical_json(detail or {}), occurred_at, row_digest),
            )
            self._append_history(
                connection,
                event_type="task_state",
                entity_id=task_id,
                payload={"row_digest": row_digest, **payload},
                occurred_at=occurred_at,
            )

    def create_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        role: RuntimeRole,
        model: str,
        effort: str,
        profile_digest: str,
    ) -> None:
        occurred_at = _now()
        definition = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "role": role,
            "model": model,
            "effort": effort,
            "profile_digest": profile_digest,
            "created_at": occurred_at,
        }
        digest = sha256_digest(definition)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, task_id, role, model, effort, profile_digest, occurred_at, digest),
            )
            self._append_history(
                connection,
                event_type="attempt_created",
                entity_id=attempt_id,
                payload={"definition_digest": digest},
                occurred_at=occurred_at,
            )
        self.append_attempt_stage(attempt_id, AttemptStage.RESERVED)

    def append_attempt_stage(
        self,
        attempt_id: str,
        stage: AttemptStage,
        *,
        reason_code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        previous_raw = self._latest_value("attempt_events", "attempt_id", attempt_id, "stage")
        previous = None if previous_raw is None else AttemptStage(previous_raw)
        if stage not in _ATTEMPT_TRANSITIONS[previous]:
            raise Gate0CLedgerError(
                "INVALID_ATTEMPT_TRANSITION",
                f"Attempt 상태 전이가 허용되지 않습니다: {previous} -> {stage}",
            )
        occurred_at = _now()
        payload = {"stage": stage, "reason_code": reason_code, "detail": detail or {}}
        row_digest = sha256_digest({"attempt_id": attempt_id, **payload, "occurred_at": occurred_at})
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO attempt_events(attempt_id,stage,reason_code,detail_json,occurred_at,row_digest) VALUES(?,?,?,?,?,?)",
                (attempt_id, stage, reason_code, canonical_json(detail or {}), occurred_at, row_digest),
            )
            self._append_history(
                connection,
                event_type="attempt_stage",
                entity_id=attempt_id,
                payload={"row_digest": row_digest, **payload},
                occurred_at=occurred_at,
            )

    def store_context_bundle(self, attempt_id: str, bundle: ContextBundle) -> None:
        attempt = self.connection.execute(
            "SELECT role FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if attempt is None or attempt["role"] != bundle.role:
            raise Gate0CLedgerError(
                "ROLE_BINDING_MISMATCH",
                "ContextBundle role이 Attempt role과 다릅니다.",
            )
        occurred_at = _now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO context_bundles VALUES(?,?,?,?,?,?)",
                (
                    bundle.bundle_id,
                    attempt_id,
                    bundle.role,
                    canonical_json(bundle),
                    bundle.digest,
                    occurred_at,
                ),
            )
            self._append_history(
                connection,
                event_type="context_bundle_stored",
                entity_id=bundle.bundle_id,
                payload={"bundle_digest": bundle.digest, "attempt_id": attempt_id},
                occurred_at=occurred_at,
            )

    def append_execution(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        role: RuntimeRole,
        action_kind: str,
        stage: ExecutionStage,
        request_digest: str,
        profile_digest: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        receipt_digest: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        attempt = self.connection.execute(
            "SELECT role,profile_digest FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if attempt is None or attempt["role"] != role or attempt["profile_digest"] != profile_digest:
            raise Gate0CLedgerError(
                "ROLE_BINDING_MISMATCH",
                "runtime execution의 role/profile이 Attempt와 다릅니다.",
            )
        previous_raw = self._latest_value(
            "role_executions", "execution_id", execution_id, "stage"
        )
        previous = None if previous_raw is None else ExecutionStage(previous_raw)
        if stage not in _EXECUTION_TRANSITIONS[previous]:
            raise Gate0CLedgerError(
                "INVALID_EXECUTION_TRANSITION",
                f"runtime execution 전이가 허용되지 않습니다: {previous} -> {stage}",
            )
        if stage is ExecutionStage.SUCCEEDED and (thread_id is None or receipt_digest is None):
            raise Gate0CLedgerError(
                "RECEIPT_INCOMPLETE",
                "성공 runtime receipt에는 thread ID와 digest가 필요합니다.",
            )
        if thread_id is not None:
            conflict = self.connection.execute(
                "SELECT role,execution_id FROM role_executions WHERE thread_id=? AND role<>? LIMIT 1",
                (thread_id, role),
            ).fetchone()
            if conflict is not None:
                raise Gate0CLedgerError(
                    "CROSS_ROLE_THREAD_REUSE",
                    "다른 role의 thread를 재사용할 수 없습니다.",
                )
        if turn_id is not None:
            conflict = self.connection.execute(
                "SELECT role,execution_id FROM role_executions WHERE turn_id=? AND execution_id<>? LIMIT 1",
                (turn_id, execution_id),
            ).fetchone()
            if conflict is not None:
                raise Gate0CLedgerError(
                    "CROSS_ROLE_TURN_REUSE",
                    "다른 execution의 turn을 재사용할 수 없습니다.",
                )
        occurred_at = _now()
        payload = {
            "execution_id": execution_id,
            "attempt_id": attempt_id,
            "role": role,
            "action_kind": action_kind,
            "stage": stage,
            "request_digest": request_digest,
            "profile_digest": profile_digest,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "receipt_digest": receipt_digest,
            "detail": detail or {},
            "occurred_at": occurred_at,
        }
        row_digest = sha256_digest(payload)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO role_executions(execution_id,attempt_id,role,action_kind,stage,request_digest,profile_digest,thread_id,turn_id,receipt_digest,detail_json,occurred_at,row_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_id,
                    attempt_id,
                    role,
                    action_kind,
                    stage,
                    request_digest,
                    profile_digest,
                    thread_id,
                    turn_id,
                    receipt_digest,
                    canonical_json(detail or {}),
                    occurred_at,
                    row_digest,
                ),
            )
            self._append_history(
                connection,
                event_type="role_execution",
                entity_id=execution_id,
                payload={"row_digest": row_digest, "stage": stage},
                occurred_at=occurred_at,
            )

    def store_submission(
        self,
        *,
        submission_id: str,
        attempt_id: str,
        bundle_id: str,
        role: RuntimeRole,
        submission: Submission,
        accepted: bool,
        reason_code: str | None = None,
    ) -> str:
        attempt = self.connection.execute(
            "SELECT role FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        bundle = self.connection.execute(
            "SELECT role,bundle_digest FROM context_bundles WHERE bundle_id=? AND attempt_id=?",
            (bundle_id, attempt_id),
        ).fetchone()
        if (
            attempt is None
            or bundle is None
            or attempt["role"] != role
            or bundle["role"] != role
            or submission.bundle_id != bundle_id
            or submission.bundle_digest != bundle["bundle_digest"]
        ):
            raise Gate0CLedgerError(
                "SUBMISSION_BINDING_MISMATCH",
                "submission role/bundle/Attempt binding이 다릅니다.",
            )
        document = canonical_json(submission)
        digest = sha256_digest(submission)
        occurred_at = _now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO agent_submissions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    submission_id,
                    attempt_id,
                    bundle_id,
                    role,
                    document,
                    digest,
                    int(accepted),
                    reason_code,
                    occurred_at,
                ),
            )
            self._append_history(
                connection,
                event_type="agent_submission",
                entity_id=submission_id,
                payload={"submission_digest": digest, "accepted": accepted},
                occurred_at=occurred_at,
            )
        return digest

    def record_core_validation(
        self,
        *,
        validation_id: str,
        runner_attempt_id: str,
        validator_attempt_id: str,
        named_checks_digest: str,
        artifact_digest: str,
        validator_submission_digest: str,
        passed: bool,
        detail: dict[str, Any],
    ) -> None:
        roles = {
            row["attempt_id"]: row["role"]
            for row in self.connection.execute(
                "SELECT attempt_id,role FROM attempts WHERE attempt_id IN (?,?)",
                (runner_attempt_id, validator_attempt_id),
            )
        }
        if roles.get(runner_attempt_id) != RuntimeRole.RUNNER or roles.get(
            validator_attempt_id
        ) != RuntimeRole.VALIDATOR:
            raise Gate0CLedgerError(
                "ROLE_BINDING_MISMATCH",
                "Core validation은 별도 Runner와 Validator Attempt를 요구합니다.",
            )
        occurred_at = _now()
        payload = {
            "validation_id": validation_id,
            "runner_attempt_id": runner_attempt_id,
            "validator_attempt_id": validator_attempt_id,
            "named_checks_digest": named_checks_digest,
            "artifact_digest": artifact_digest,
            "validator_submission_digest": validator_submission_digest,
            "passed": passed,
            "detail": detail,
            "created_at": occurred_at,
        }
        row_digest = sha256_digest(payload)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO core_validations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    validation_id,
                    runner_attempt_id,
                    validator_attempt_id,
                    named_checks_digest,
                    artifact_digest,
                    validator_submission_digest,
                    int(passed),
                    canonical_json(detail),
                    occurred_at,
                    row_digest,
                ),
            )
            self._append_history(
                connection,
                event_type="core_validation",
                entity_id=validation_id,
                payload={"row_digest": row_digest, "passed": passed},
                occurred_at=occurred_at,
            )

    def record_evidence(
        self,
        *,
        evidence_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str:
        """비밀 원문 대신 구조화된 합성 evidence를 content-addressed로 보존한다."""

        document = canonical_json(payload)
        payload_digest = sha256_digest(payload)
        occurred_at = _now()
        row_material = {
            "evidence_id": evidence_id,
            "kind": kind,
            "payload_digest": payload_digest,
            "created_at": occurred_at,
        }
        row_digest = sha256_digest(row_material)
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO evidence_records VALUES(?,?,?,?,?,?)",
                (
                    evidence_id,
                    kind,
                    document,
                    payload_digest,
                    occurred_at,
                    row_digest,
                ),
            )
            self._append_history(
                connection,
                event_type="evidence_recorded",
                entity_id=evidence_id,
                payload={
                    "kind": kind,
                    "payload_digest": payload_digest,
                    "row_digest": row_digest,
                },
                occurred_at=occurred_at,
            )
        return payload_digest

    def quarantine_unsettled_executions(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT r.* FROM role_executions r
            JOIN (
              SELECT execution_id, MAX(sequence) AS max_sequence
              FROM role_executions GROUP BY execution_id
            ) latest ON latest.max_sequence=r.sequence
            WHERE r.stage='dispatched'
            """
        ).fetchall()
        quarantined: list[str] = []
        for row in rows:
            self.append_execution(
                execution_id=row["execution_id"],
                attempt_id=row["attempt_id"],
                role=RuntimeRole(row["role"]),
                action_kind=row["action_kind"],
                stage=ExecutionStage.UNKNOWN,
                request_digest=row["request_digest"],
                profile_digest=row["profile_digest"],
                detail={"reason": "프로세스 재시작 시 외부 효과 receipt가 없음"},
            )
            quarantined.append(str(row["execution_id"]))
        return tuple(quarantined)


def bootstrap_approved_ledger(database: Path, plan_artifact: Path) -> None:
    artifact = json.loads(plan_artifact.read_text(encoding="utf-8"))
    with Gate0CLedger(database) as ledger:
        count = ledger.connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0]
        if count == 0:
            ledger.import_approved_plan(artifact)
            ledger.register_approved_tasks(str(artifact["plan_revision_id"]))
            for task in APPROVED_TASKS:
                ledger.append_task_state(task["task_id"], TaskState.PENDING)
