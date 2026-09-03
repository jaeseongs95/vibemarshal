from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..canonical import canonical_json, sha256_digest
from ..time import SystemClock
from .domain import CORE_SCHEMA_ID, CORE_SCHEMA_REVISION, CoreDomainError, new_id


SQLITE_APPLICATION_ID = 0x464D4331  # ASCII "FMC1"


SCHEMA_SQL = r"""
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    context_sources_json TEXT NOT NULL,
    default_validations_json TEXT NOT NULL,
    runtime_requirements_json TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    run_state TEXT NOT NULL CHECK (run_state IN ('active','recovery_required')),
    recovery_reason TEXT,
    active_revision_id TEXT REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE plan_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    parent_revision_id TEXT REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('draft','active','superseded','completed')),
    canonical_digest TEXT NOT NULL,
    request_summary TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    activation_source TEXT CHECK (activation_source IN ('cli','ui','api')),
    superseded_at TEXT,
    completed_at TEXT,
    UNIQUE (project_id, revision_no),
    UNIQUE (project_id, canonical_digest)
) STRICT;

CREATE UNIQUE INDEX uq_core_one_active_revision
ON plan_revisions(project_id) WHERE status = 'active';

CREATE TABLE work_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    client_ref TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    stored_status TEXT NOT NULL CHECK (stored_status IN
        ('pending','dispatching','running','validating','completed','retryable',
         'blocked','failed','superseded')),
    definition_digest TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    assignment_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (revision_id, client_ref),
    UNIQUE (revision_id, position)
) STRICT;

CREATE TABLE work_item_dependencies (
    revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
    depends_on_work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
    PRIMARY KEY (revision_id, work_item_id, depends_on_work_item_id),
    CHECK (work_item_id <> depends_on_work_item_id)
) STRICT;

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    status TEXT NOT NULL CHECK (status IN
        ('reserved','running','awaiting_validation','completed','retryable',
         'blocked','failed','cancelled','external_unknown')),
    model_id TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    validation_model_id TEXT,
    validation_reasoning_effort TEXT,
    failure_class TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (revision_id, work_item_id, attempt_no),
    CHECK ((validation_model_id IS NULL) = (validation_reasoning_effort IS NULL))
) STRICT;

CREATE UNIQUE INDEX uq_core_one_active_attempt_per_work_item
ON attempts(work_item_id)
WHERE status IN ('reserved','running','awaiting_validation');

CREATE UNIQUE INDEX uq_core_one_active_attempt_per_project
ON attempts(project_id)
WHERE status IN ('reserved','running','awaiting_validation');

CREATE TABLE runtime_action_intents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    kind TEXT NOT NULL CHECK (kind IN
        ('create_thread','start_turn','interrupt_turn','observe_thread',
         'run_validation','recover_bind','recover_abandon')),
    status TEXT NOT NULL CHECK (status IN
        ('reserved','executing','succeeded','confirmed_no_effect','unknown',
         'cancelled','reconciled')),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    recovery_of_intent_id TEXT REFERENCES runtime_action_intents(id) ON DELETE RESTRICT,
    external_id TEXT,
    receipt_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (attempt_id, ordinal),
    CHECK (recovery_of_intent_id IS NULL OR recovery_of_intent_id <> id)
) STRICT;

CREATE UNIQUE INDEX uq_core_one_active_intent_per_attempt
ON runtime_action_intents(attempt_id)
WHERE status IN ('reserved','executing');

CREATE TABLE runtime_bindings (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(id) ON DELETE RESTRICT,
    thread_intent_id TEXT NOT NULL UNIQUE REFERENCES runtime_action_intents(id) ON DELETE RESTRICT,
    turn_intent_id TEXT UNIQUE REFERENCES runtime_action_intents(id) ON DELETE RESTRICT,
    thread_id TEXT NOT NULL UNIQUE,
    turn_id TEXT UNIQUE,
    cwd TEXT NOT NULL,
    instruction_sources_json TEXT NOT NULL,
    thread_receipt_json TEXT NOT NULL,
    turn_receipt_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((turn_intent_id IS NULL) = (turn_id IS NULL)),
    CHECK ((turn_id IS NULL) = (turn_receipt_json IS NULL))
) STRICT;

CREATE TABLE evidence_records (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    evidence_kind TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, evidence_kind, digest)
) STRICT;

CREATE TABLE validation_results (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    criterion_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    check_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed','failed','error')),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, criterion_id, ordinal)
) STRICT;

CREATE TABLE history_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, sequence)
) STRICT;

CREATE TRIGGER tr_core_plan_content_immutable
BEFORE UPDATE ON plan_revisions
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_no <> OLD.revision_no
  OR COALESCE(NEW.parent_revision_id, '') <> COALESCE(OLD.parent_revision_id, '')
  OR NEW.canonical_digest <> OLD.canonical_digest
  OR NEW.request_summary <> OLD.request_summary
  OR NEW.snapshot_json <> OLD.snapshot_json
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_PLAN_CONTENT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_project_definition_immutable
BEFORE UPDATE ON projects
WHEN NEW.name <> OLD.name
  OR NEW.root <> OLD.root
  OR NEW.context_sources_json <> OLD.context_sources_json
  OR NEW.default_validations_json <> OLD.default_validations_json
  OR NEW.runtime_requirements_json <> OLD.runtime_requirements_json
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_PROJECT_DEFINITION_IMMUTABLE');
END;

CREATE TRIGGER tr_core_project_no_delete
BEFORE DELETE ON projects
BEGIN
    SELECT RAISE(ABORT, 'CORE_PROJECT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_plan_no_delete
BEFORE DELETE ON plan_revisions
BEGIN
    SELECT RAISE(ABORT, 'CORE_PLAN_IMMUTABLE');
END;

CREATE TRIGGER tr_core_plan_status_transition
BEFORE UPDATE OF status ON plan_revisions
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'draft' AND NEW.status = 'active') OR
    (OLD.status = 'active' AND NEW.status IN ('superseded','completed'))
)
BEGIN
    SELECT RAISE(ABORT, 'CORE_INVALID_PLAN_TRANSITION');
END;

CREATE TRIGGER tr_core_work_item_definition_immutable
BEFORE UPDATE ON work_items
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_id <> OLD.revision_id
  OR NEW.client_ref <> OLD.client_ref
  OR NEW.position <> OLD.position
  OR NEW.title <> OLD.title
  OR NEW.objective <> OLD.objective
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.definition_json <> OLD.definition_json
  OR COALESCE(NEW.assignment_json, '') <> COALESCE(OLD.assignment_json, '')
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_WORK_ITEM_DEFINITION_IMMUTABLE');
END;

CREATE TRIGGER tr_core_work_item_status_transition
BEFORE UPDATE OF stored_status ON work_items
WHEN NEW.stored_status <> OLD.stored_status AND NOT (
    (OLD.stored_status = 'pending' AND NEW.stored_status IN ('dispatching','superseded')) OR
    (OLD.stored_status = 'dispatching' AND NEW.stored_status IN
        ('running','retryable','blocked','failed','superseded')) OR
    (OLD.stored_status = 'running' AND NEW.stored_status IN
        ('validating','retryable','blocked','failed','superseded')) OR
    (OLD.stored_status = 'validating' AND NEW.stored_status IN
        ('completed','retryable','blocked','failed','superseded')) OR
    (OLD.stored_status = 'retryable' AND NEW.stored_status IN ('dispatching','superseded')) OR
    (OLD.stored_status = 'blocked' AND NEW.stored_status IN ('pending','superseded')) OR
    (OLD.stored_status = 'failed' AND NEW.stored_status = 'superseded')
)
BEGIN
    SELECT RAISE(ABORT, 'CORE_INVALID_WORK_ITEM_TRANSITION');
END;

CREATE TRIGGER tr_core_work_item_no_delete
BEFORE DELETE ON work_items
BEGIN
    SELECT RAISE(ABORT, 'CORE_WORK_ITEM_IMMUTABLE');
END;

CREATE TRIGGER tr_core_dependency_no_update
BEFORE UPDATE ON work_item_dependencies
BEGIN
    SELECT RAISE(ABORT, 'CORE_DEPENDENCY_IMMUTABLE');
END;

CREATE TRIGGER tr_core_dependency_no_delete
BEFORE DELETE ON work_item_dependencies
BEGIN
    SELECT RAISE(ABORT, 'CORE_DEPENDENCY_IMMUTABLE');
END;

CREATE TRIGGER tr_core_attempt_status_transition
BEFORE UPDATE OF status ON attempts
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'reserved' AND NEW.status IN
        ('running','retryable','blocked','failed','cancelled','external_unknown')) OR
    (OLD.status = 'running' AND NEW.status IN
        ('awaiting_validation','retryable','blocked','failed','cancelled','external_unknown')) OR
    (OLD.status = 'awaiting_validation' AND NEW.status IN
        ('completed','retryable','blocked','failed','cancelled','external_unknown'))
)
BEGIN
    SELECT RAISE(ABORT, 'CORE_INVALID_ATTEMPT_TRANSITION');
END;

CREATE TRIGGER tr_core_attempt_identity_immutable
BEFORE UPDATE ON attempts
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_id <> OLD.revision_id
  OR NEW.work_item_id <> OLD.work_item_id
  OR NEW.attempt_no <> OLD.attempt_no
  OR NEW.model_id <> OLD.model_id
  OR NEW.reasoning_effort <> OLD.reasoning_effort
  OR COALESCE(NEW.validation_model_id, '') <> COALESCE(OLD.validation_model_id, '')
  OR COALESCE(NEW.validation_reasoning_effort, '') <>
     COALESCE(OLD.validation_reasoning_effort, '')
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_ATTEMPT_IDENTITY_IMMUTABLE');
END;

CREATE TRIGGER tr_core_terminal_attempt_immutable
BEFORE UPDATE ON attempts
WHEN OLD.status IN
    ('completed','retryable','blocked','failed','cancelled','external_unknown')
BEGIN
    SELECT RAISE(ABORT, 'CORE_TERMINAL_ATTEMPT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_attempt_no_delete
BEFORE DELETE ON attempts
BEGIN
    SELECT RAISE(ABORT, 'CORE_ATTEMPT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_intent_status_transition
BEFORE UPDATE OF status ON runtime_action_intents
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'reserved' AND NEW.status IN ('executing','cancelled')) OR
    (OLD.status = 'executing' AND NEW.status IN
        ('succeeded','confirmed_no_effect','unknown')) OR
    (OLD.status = 'unknown' AND NEW.status = 'reconciled')
)
BEGIN
    SELECT RAISE(ABORT, 'CORE_INVALID_INTENT_TRANSITION');
END;

CREATE TRIGGER tr_core_intent_request_immutable
BEFORE UPDATE ON runtime_action_intents
WHEN NEW.project_id <> OLD.project_id
  OR NEW.attempt_id <> OLD.attempt_id
  OR NEW.ordinal <> OLD.ordinal
  OR NEW.kind <> OLD.kind
  OR NEW.request_digest <> OLD.request_digest
  OR NEW.request_json <> OLD.request_json
  OR COALESCE(NEW.recovery_of_intent_id, '') <>
     COALESCE(OLD.recovery_of_intent_id, '')
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_INTENT_REQUEST_IMMUTABLE');
END;

CREATE TRIGGER tr_core_terminal_intent_immutable
BEFORE UPDATE ON runtime_action_intents
WHEN OLD.status IN ('succeeded','confirmed_no_effect','cancelled','reconciled')
BEGIN
    SELECT RAISE(ABORT, 'CORE_TERMINAL_INTENT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_intent_no_delete
BEFORE DELETE ON runtime_action_intents
BEGIN
    SELECT RAISE(ABORT, 'CORE_INTENT_IMMUTABLE');
END;

CREATE TRIGGER tr_core_binding_identity_immutable
BEFORE UPDATE ON runtime_bindings
WHEN NEW.attempt_id <> OLD.attempt_id
  OR NEW.thread_intent_id <> OLD.thread_intent_id
  OR NEW.thread_id <> OLD.thread_id
  OR NEW.cwd <> OLD.cwd
  OR NEW.instruction_sources_json <> OLD.instruction_sources_json
  OR NEW.thread_receipt_json <> OLD.thread_receipt_json
  OR OLD.turn_id IS NOT NULL
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'CORE_BINDING_IDENTITY_IMMUTABLE');
END;

CREATE TRIGGER tr_core_binding_no_delete
BEFORE DELETE ON runtime_bindings
BEGIN
    SELECT RAISE(ABORT, 'CORE_BINDING_IMMUTABLE');
END;

CREATE TRIGGER tr_core_history_no_update
BEFORE UPDATE ON history_events
BEGIN
    SELECT RAISE(ABORT, 'CORE_HISTORY_APPEND_ONLY');
END;

CREATE TRIGGER tr_core_history_no_delete
BEFORE DELETE ON history_events
BEGIN
    SELECT RAISE(ABORT, 'CORE_HISTORY_APPEND_ONLY');
END;

CREATE TRIGGER tr_core_evidence_no_update
BEFORE UPDATE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'CORE_EVIDENCE_APPEND_ONLY');
END;

CREATE TRIGGER tr_core_evidence_no_delete
BEFORE DELETE ON evidence_records
BEGIN
    SELECT RAISE(ABORT, 'CORE_EVIDENCE_APPEND_ONLY');
END;

CREATE TRIGGER tr_core_validation_no_update
BEFORE UPDATE ON validation_results
BEGIN
    SELECT RAISE(ABORT, 'CORE_VALIDATION_APPEND_ONLY');
END;

CREATE TRIGGER tr_core_validation_no_delete
BEFORE DELETE ON validation_results
BEGIN
    SELECT RAISE(ABORT, 'CORE_VALIDATION_APPEND_ONLY');
END;
"""


class SQLiteCoreLedger:
    """오케스트레이션 MVP 전용 SQLite 원장.

    Gate 0B DB를 발견하면 migration을 시도하지 않고 거부한다. 따라서 과거
    artifact를 열기만 한 뒤 새 table을 섞는 사고도 만들지 않는다.
    """

    def __init__(self, path: Path | str, *, clock: Any | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or SystemClock()

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=10.0,
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        if not readonly:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _assert_existing_identity(self) -> None:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        connection = self._connect(readonly=True)
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if not tables:
                return
            if "schema_meta" not in tables:
                raise CoreDomainError(
                    "기존 DB가 FlowMarshal Core 원장으로 식별되지 않습니다."
                )
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM schema_meta")
            }
            if metadata.get("schema_id") != CORE_SCHEMA_ID:
                raise CoreDomainError(
                    "기존 Gate 또는 외부 DB는 새 Core 원장으로 열 수 없습니다."
                )
            if metadata.get("schema_revision") != str(CORE_SCHEMA_REVISION):
                raise CoreDomainError(
                    f"지원하지 않는 Core schema revision입니다: {metadata.get('schema_revision')!r}"
                )
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id != SQLITE_APPLICATION_ID:
                raise CoreDomainError("SQLite application_id가 FlowMarshal Core와 다릅니다.")
        finally:
            connection.close()

    def initialize(self) -> None:
        self._assert_existing_identity()
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        connection = self._connect()
        try:
            if is_new:
                connection.executescript(SCHEMA_SQL)
                connection.executemany(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    (
                        ("schema_id", CORE_SCHEMA_ID),
                        ("schema_revision", str(CORE_SCHEMA_REVISION)),
                    ),
                )
                connection.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {CORE_SCHEMA_REVISION}")
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator["CoreTransaction"]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield CoreTransaction(connection, self.clock)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def raw_connection(self, *, readonly: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect(readonly=readonly)
        try:
            yield connection
        finally:
            connection.close()

    def table_names(self) -> tuple[str, ...]:
        with self.raw_connection() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            return tuple(row["name"] for row in rows)

    def integrity_check(self) -> tuple[str, ...]:
        with self.raw_connection() as connection:
            return tuple(row[0] for row in connection.execute("PRAGMA integrity_check"))

    def verify_history(self, project_id: str) -> bool:
        with self.raw_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM history_events WHERE project_id = ? ORDER BY sequence",
                (project_id,),
            ).fetchall()
        previous: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            if row["sequence"] != expected_sequence or row["previous_hash"] != previous:
                return False
            material = {
                "project_id": row["project_id"],
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"],
                "created_at": row["created_at"],
            }
            if row["event_hash"] != sha256_digest(material):
                return False
            previous = row["event_hash"]
        return bool(rows)


class CoreTransaction:
    def __init__(self, connection: sqlite3.Connection, clock: Any) -> None:
        self.connection = connection
        self.clock = clock

    @property
    def now(self) -> str:
        return self.clock.now()

    def one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row:
        row = self.connection.execute(sql, parameters).fetchone()
        if row is None:
            raise CoreDomainError("필수 원장 행을 찾을 수 없습니다.")
        return row

    def all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(sql, parameters).fetchall()

    def history(
        self,
        project_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        previous = self.connection.execute(
            "SELECT sequence, event_hash FROM history_events WHERE project_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_hash = None if previous is None else previous["event_hash"]
        created_at = self.now
        material = {
            "project_id": project_id,
            "sequence": sequence,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = sha256_digest(material)
        event_id = new_id("history")
        self.connection.execute(
            "INSERT INTO history_events "
            "(id, project_id, sequence, event_type, entity_type, entity_id, "
            "payload_json, previous_hash, event_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                project_id,
                sequence,
                event_type,
                entity_type,
                entity_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                created_at,
            ),
        )
        return event_id
