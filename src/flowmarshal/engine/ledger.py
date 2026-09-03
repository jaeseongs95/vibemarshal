from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..canonical import canonical_json, sha256_digest
from ..time import SystemClock


ENGINE_SCHEMA_ID = "flowmarshal.engine"
ENGINE_SCHEMA_REVISION = 2
SQLITE_APPLICATION_ID = 0x464D4531  # ASCII "FME1"
DEFAULT_DB_NAME = "flowmarshal-engine.sqlite3"
DEFAULT_ARTIFACT_DIRECTORY = "artifacts"


class EngineLedgerError(RuntimeError):
    pass


SCHEMA_SQL = r"""
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root TEXT NOT NULL UNIQUE,
    artifact_root TEXT NOT NULL UNIQUE,
    active_profile_revision_id TEXT,
    active_goal_revision_id TEXT,
    active_plan_revision_id TEXT,
    run_state TEXT NOT NULL CHECK (run_state IN ('idle','active','recovery_required','completed')),
    recovery_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE profile_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    definition_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','ready','active','superseded')),
    supersedes_id TEXT REFERENCES profile_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(project_id, revision_no),
    UNIQUE(project_id, definition_digest)
) STRICT;

CREATE TABLE context_source_registrations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('reference','instruction')),
    path TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    UNIQUE(project_id, path),
    UNIQUE(project_id, content_digest, kind)
) STRICT;

CREATE TABLE goal_revisions (
    id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    definition_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','ready','active','superseded','needs_input','conflict')),
    supersedes_id TEXT REFERENCES goal_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(goal_id, revision_no),
    UNIQUE(project_id, definition_digest)
) STRICT;

CREATE TABLE state_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    goal_contract_digest TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    snapshot_digest TEXT NOT NULL UNIQUE,
    semantic_digest TEXT NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
    UNIQUE(project_id, goal_contract_digest, version)
) STRICT;

CREATE UNIQUE INDEX uq_engine_current_state_scope
ON state_snapshots(project_id, goal_contract_digest)
WHERE is_current = 1;

CREATE TABLE project_map_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    revision_digest TEXT NOT NULL UNIQUE,
    semantic_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
    UNIQUE(project_id, revision_no)
) STRICT;

CREATE UNIQUE INDEX uq_engine_current_project_map
ON project_map_revisions(project_id) WHERE is_current = 1;

CREATE TABLE skeleton_candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    goal_contract_digest TEXT NOT NULL,
    candidate_digest TEXT NOT NULL UNIQUE,
    graph_signature TEXT NOT NULL,
    parent_candidate_id TEXT REFERENCES skeleton_candidates(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL CHECK (version BETWEEN 1 AND 5),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE candidate_reviews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('skeleton','plan')),
    artifact_digest TEXT NOT NULL,
    reviewer_role TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE candidate_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('skeleton','plan')),
    artifact_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('generated','needs_revision','blocked','rejected','admissible','selected')),
    fitness_score INTEGER CHECK (fitness_score BETWEEN 0 AND 100),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_kind, artifact_digest)
) STRICT;

CREATE TABLE plan_revisions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    definition_digest TEXT NOT NULL,
    activation_digest TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','ready','active','superseded','completed')),
    supersedes_id TEXT REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    activation_source TEXT,
    completed_at TEXT,
    UNIQUE(plan_id, revision_no),
    UNIQUE(project_id, definition_digest)
) STRICT;

CREATE UNIQUE INDEX uq_engine_one_active_plan
ON plan_revisions(project_id) WHERE status = 'active';

CREATE TABLE plan_activations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    plan_revision_id TEXT NOT NULL UNIQUE REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    activation_digest TEXT NOT NULL,
    source TEXT NOT NULL,
    activated_at TEXT NOT NULL
) STRICT;

CREATE TABLE task_contracts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    task_ref TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    contract_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','ready','materialized','reserved','running','validating','completed','failed','blocked','superseded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_revision_id, task_ref),
    UNIQUE(plan_revision_id, position)
) STRICT;

CREATE TABLE task_dependencies (
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    producer_task_id TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    consumer_task_id TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    dependency_type TEXT NOT NULL,
    products_json TEXT NOT NULL,
    PRIMARY KEY(plan_revision_id, producer_task_id, consumer_task_id, dependency_type),
    CHECK(producer_task_id <> consumer_task_id)
) STRICT;

CREATE TABLE execution_spec_revisions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    definition_digest TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    supersedes_id TEXT REFERENCES execution_spec_revisions(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    is_current INTEGER NOT NULL CHECK (is_current IN (0,1)),
    UNIQUE(task_id, revision_no)
) STRICT;

CREATE UNIQUE INDEX uq_engine_current_execution_spec
ON execution_spec_revisions(task_id) WHERE is_current = 1;

CREATE TABLE effect_checkpoints (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    effect_id TEXT NOT NULL,
    execution_spec_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    UNIQUE(task_id, effect_id, execution_spec_digest)
) STRICT;

CREATE TABLE attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES task_contracts(id) ON DELETE RESTRICT,
    execution_spec_digest TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    kind TEXT NOT NULL CHECK (kind IN ('execution','validation')),
    status TEXT NOT NULL CHECK (status IN ('reserved','starting','running','succeeded','failed','interrupted','unknown','abandoned')),
    binding_json TEXT,
    failure_class TEXT,
    failure_detail TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, attempt_no, kind)
) STRICT;

CREATE UNIQUE INDEX uq_engine_one_active_attempt_per_task
ON attempts(task_id) WHERE status IN ('reserved','starting','running');

CREATE UNIQUE INDEX uq_engine_one_active_attempt_per_project
ON attempts(project_id) WHERE status IN ('reserved','starting','running');

CREATE TABLE runtime_intents (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('prepared','received','unknown','abandoned')),
    prepared_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE runtime_receipts (
    id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES runtime_intents(id) ON DELETE RESTRICT,
    provider_operation_id TEXT NOT NULL UNIQUE,
    response_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    binding_json TEXT,
    received_at TEXT NOT NULL
) STRICT;

CREATE TABLE evidence_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES task_contracts(id) ON DELETE RESTRICT,
    attempt_id TEXT REFERENCES attempts(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    observation TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
) STRICT;

CREATE TABLE validation_results (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES task_contracts(id) ON DELETE RESTRICT,
    validation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pass','fail','inconclusive','not_run')),
    payload_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
) STRICT;

CREATE TABLE goal_verdicts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    plan_revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    goal_contract_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('satisfied','not_satisfied','inconclusive')),
    payload_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
) STRICT;

CREATE TABLE budget_usage (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    goal_contract_digest TEXT NOT NULL,
    stage TEXT NOT NULL,
    logical_call_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(project_id, goal_contract_digest, logical_call_ref)
) STRICT;

CREATE TABLE recovery_assessments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    failure_class TEXT NOT NULL,
    action TEXT NOT NULL,
    same_failure_replan_count INTEGER NOT NULL CHECK (same_failure_replan_count >= 0),
    goal_replan_count INTEGER NOT NULL CHECK (goal_replan_count >= 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    UNIQUE(project_id, sequence)
) STRICT;

CREATE TRIGGER tr_engine_profile_content_immutable
BEFORE UPDATE ON profile_revisions
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_no <> OLD.revision_no
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.payload_json <> OLD.payload_json
  OR COALESCE(NEW.supersedes_id, '') <> COALESCE(OLD.supersedes_id, '')
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'ENGINE_PROFILE_CONTENT_IMMUTABLE'); END;

CREATE TRIGGER tr_engine_context_source_no_update
BEFORE UPDATE ON context_source_registrations
BEGIN SELECT RAISE(ABORT, 'ENGINE_CONTEXT_SOURCE_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_context_source_no_delete
BEFORE DELETE ON context_source_registrations
BEGIN SELECT RAISE(ABORT, 'ENGINE_CONTEXT_SOURCE_APPEND_ONLY'); END;

CREATE TRIGGER tr_engine_goal_content_immutable
BEFORE UPDATE ON goal_revisions
WHEN NEW.goal_id <> OLD.goal_id
  OR NEW.project_id <> OLD.project_id
  OR NEW.revision_no <> OLD.revision_no
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.payload_json <> OLD.payload_json
  OR COALESCE(NEW.supersedes_id, '') <> COALESCE(OLD.supersedes_id, '')
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'ENGINE_GOAL_CONTENT_IMMUTABLE'); END;

CREATE TRIGGER tr_engine_plan_content_immutable
BEFORE UPDATE ON plan_revisions
WHEN NEW.plan_id <> OLD.plan_id
  OR NEW.project_id <> OLD.project_id
  OR NEW.revision_no <> OLD.revision_no
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.activation_digest <> OLD.activation_digest
  OR NEW.payload_json <> OLD.payload_json
  OR COALESCE(NEW.supersedes_id, '') <> COALESCE(OLD.supersedes_id, '')
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'ENGINE_PLAN_CONTENT_IMMUTABLE'); END;

CREATE TRIGGER tr_engine_task_content_immutable
BEFORE UPDATE ON task_contracts
WHEN NEW.project_id <> OLD.project_id
  OR NEW.plan_revision_id <> OLD.plan_revision_id
  OR NEW.task_ref <> OLD.task_ref
  OR NEW.position <> OLD.position
  OR NEW.contract_digest <> OLD.contract_digest
  OR NEW.payload_json <> OLD.payload_json
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'ENGINE_TASK_CONTENT_IMMUTABLE'); END;

CREATE TRIGGER tr_engine_spec_content_immutable
BEFORE UPDATE ON execution_spec_revisions
WHEN NEW.task_id <> OLD.task_id
  OR NEW.revision_no <> OLD.revision_no
  OR NEW.definition_digest <> OLD.definition_digest
  OR NEW.payload_json <> OLD.payload_json
  OR COALESCE(NEW.supersedes_id, '') <> COALESCE(OLD.supersedes_id, '')
  OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'ENGINE_SPEC_CONTENT_IMMUTABLE'); END;

CREATE TRIGGER tr_engine_history_no_update BEFORE UPDATE ON history_events
BEGIN SELECT RAISE(ABORT, 'ENGINE_HISTORY_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_history_no_delete BEFORE DELETE ON history_events
BEGIN SELECT RAISE(ABORT, 'ENGINE_HISTORY_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_evidence_no_update BEFORE UPDATE ON evidence_records
BEGIN SELECT RAISE(ABORT, 'ENGINE_EVIDENCE_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_evidence_no_delete BEFORE DELETE ON evidence_records
BEGIN SELECT RAISE(ABORT, 'ENGINE_EVIDENCE_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_validation_no_update BEFORE UPDATE ON validation_results
BEGIN SELECT RAISE(ABORT, 'ENGINE_VALIDATION_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_validation_no_delete BEFORE DELETE ON validation_results
BEGIN SELECT RAISE(ABORT, 'ENGINE_VALIDATION_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_receipt_no_update BEFORE UPDATE ON runtime_receipts
BEGIN SELECT RAISE(ABORT, 'ENGINE_RECEIPT_APPEND_ONLY'); END;
CREATE TRIGGER tr_engine_receipt_no_delete BEFORE DELETE ON runtime_receipts
BEGIN SELECT RAISE(ABORT, 'ENGINE_RECEIPT_APPEND_ONLY'); END;
"""


class EngineTransaction:
    def __init__(self, connection: sqlite3.Connection, clock: Any) -> None:
        self.connection = connection
        self.clock = clock

    @property
    def now(self) -> str:
        return self.clock.now()

    def one(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row:
        row = self.connection.execute(query, parameters).fetchone()
        if row is None:
            raise EngineLedgerError("요청한 원장 레코드를 찾을 수 없습니다.")
        return row

    def maybe_one(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, parameters).fetchone()

    def all(self, query: str, parameters: tuple[Any, ...] = ()) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute(query, parameters).fetchall())

    def history(
        self,
        project_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Any,
    ) -> str:
        previous = self.connection.execute(
            "SELECT sequence, event_hash FROM history_events WHERE project_id = ? "
            "ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_hash = None if previous is None else previous["event_hash"]
        created_at = self.now
        event_id = _new_row_id("history")
        body = {
            "id": event_id,
            "project_id": project_id,
            "sequence": sequence,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": created_at,
        }
        event_hash = sha256_digest(body)
        self.connection.execute(
            "INSERT INTO history_events "
            "(id, project_id, sequence, event_type, entity_type, entity_id, payload_json, "
            "previous_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def _new_row_id(prefix: str) -> str:
    from .domain import new_id

    return new_id(prefix)


class SQLiteEngineLedger:
    """기존 prototype DB와 식별자·파일을 공유하지 않는 Engine 원장."""

    def __init__(
        self,
        path: Path | str,
        *,
        artifact_root: Path | str | None = None,
        clock: Any | None = None,
    ) -> None:
        self.path = Path(path)
        self.artifact_root = Path(artifact_root) if artifact_root else self.path.parent / DEFAULT_ARTIFACT_DIRECTORY
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

    def initialize(self) -> None:
        is_new = not self.path.exists() or self.path.stat().st_size == 0
        if not is_new:
            self._assert_identity()
        connection = self._connect()
        try:
            if is_new:
                connection.executescript(SCHEMA_SQL)
                connection.executemany(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    (
                        ("schema_id", ENGINE_SCHEMA_ID),
                        ("schema_revision", str(ENGINE_SCHEMA_REVISION)),
                    ),
                )
                connection.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {ENGINE_SCHEMA_REVISION}")
            connection.commit()
        finally:
            connection.close()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def _assert_identity(self) -> None:
        connection = self._connect(readonly=True)
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "schema_meta" not in tables:
                raise EngineLedgerError("기존 DB가 FlowMarshal Engine 원장으로 식별되지 않습니다.")
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM schema_meta")
            }
            if metadata.get("schema_id") != ENGINE_SCHEMA_ID:
                raise EngineLedgerError("prototype 또는 외부 DB를 Engine 원장으로 열 수 없습니다.")
            if metadata.get("schema_revision") != str(ENGINE_SCHEMA_REVISION):
                raise EngineLedgerError("지원하지 않는 Engine schema revision입니다.")
            if connection.execute("PRAGMA application_id").fetchone()[0] != SQLITE_APPLICATION_ID:
                raise EngineLedgerError("SQLite application_id가 FlowMarshal Engine과 다릅니다.")
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[EngineTransaction]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield EngineTransaction(connection, self.clock)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(readonly=True)
        try:
            yield connection
        finally:
            connection.close()

    def verify_history(self, project_id: str) -> bool:
        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM history_events WHERE project_id = ? ORDER BY sequence",
                (project_id,),
            ).fetchall()
        previous_hash: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            payload = json.loads(row["payload_json"])
            body = {
                "id": row["id"],
                "project_id": row["project_id"],
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": payload,
                "previous_hash": row["previous_hash"],
                "created_at": row["created_at"],
            }
            if (
                row["sequence"] != expected_sequence
                or row["previous_hash"] != previous_hash
                or row["event_hash"] != sha256_digest(body)
            ):
                return False
            previous_hash = row["event_hash"]
        return True

    def project_snapshot(self, project_id: str) -> dict[str, Any]:
        with self.read() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise EngineLedgerError("프로젝트를 찾을 수 없습니다.")
            plans = connection.execute(
                "SELECT id, plan_id, revision_no, activation_digest, status, activated_at, completed_at "
                "FROM plan_revisions WHERE project_id = ? ORDER BY plan_id, revision_no",
                (project_id,),
            ).fetchall()
            tasks = connection.execute(
                "SELECT id, plan_revision_id, task_ref, position, contract_digest, status "
                "FROM task_contracts WHERE project_id = ? ORDER BY plan_revision_id, position",
                (project_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT id, task_id, attempt_no, kind, status, failure_class, failure_detail "
                "FROM attempts WHERE project_id = ? ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
            context_sources = connection.execute(
                "SELECT id, kind, path, content_digest, registered_at "
                "FROM context_source_registrations WHERE project_id = ? ORDER BY registered_at, rowid",
                (project_id,),
            ).fetchall()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
        return {
            "project": dict(project),
            "plans": [dict(row) for row in plans],
            "tasks": [dict(row) for row in tasks],
            "attempts": [dict(row) for row in attempts],
            "context_sources": [dict(row) for row in context_sources],
            "history_count": history_count,
            "history_valid": self.verify_history(project_id),
        }
