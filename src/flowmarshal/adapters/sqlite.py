from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..canonical import canonical_json, sha256_digest
from ..domain import (
    ACTIVE_ATTEMPT_STATUSES,
    GATE0B_SCHEMA_REVISION,
    AccessMode,
    AttemptStatus,
    CommandEnvelope,
    Decision,
    DomainError,
    IntentKind,
    IntentStatus,
    PlanDraft,
    ProjectDefinition,
    ResourceDefinition,
    ResourceKind,
    RevisionStatus,
    WorkItemState,
    new_id,
)


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    canonical_root TEXT NOT NULL UNIQUE,
    execution_slots INTEGER NOT NULL CHECK (execution_slots BETWEEN 1 AND 64),
    state TEXT NOT NULL CHECK (state IN ('active', 'quarantined')),
    active_revision_id TEXT,
    quarantine_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS project_execution_slots (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    slot_no INTEGER NOT NULL CHECK (slot_no >= 1),
    attempt_id TEXT UNIQUE,
    acquired_at TEXT,
    PRIMARY KEY (project_id, slot_no),
    CHECK ((attempt_id IS NULL AND acquired_at IS NULL) OR
           (attempt_id IS NOT NULL AND acquired_at IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('workspace', 'reference', 'runtime_dependency')),
    canonical_path TEXT NOT NULL,
    max_access TEXT NOT NULL CHECK (max_access IN ('read', 'write')),
    include_json TEXT NOT NULL,
    exclude_json TEXT NOT NULL,
    file_identity TEXT,
    manifest_digest TEXT,
    definition_digest TEXT NOT NULL,
    source_access_grant_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, canonical_path),
    CHECK (kind = 'workspace' OR max_access = 'read')
) STRICT;

CREATE TABLE IF NOT EXISTS protected_paths (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    canonical_path TEXT NOT NULL,
    reason TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, canonical_path)
) STRICT;

CREATE TABLE IF NOT EXISTS plan_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    status TEXT NOT NULL CHECK (status IN
        ('candidate', 'approved_pending_activation', 'active', 'retired', 'rejected')),
    content_digest TEXT NOT NULL,
    summary TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    activated_at TEXT,
    retired_at TEXT,
    UNIQUE (project_id, revision_no),
    UNIQUE (project_id, content_digest)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_revision
ON plan_revisions(project_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    client_ref TEXT NOT NULL,
    previous_work_item_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
    definition_digest TEXT NOT NULL,
    goal TEXT NOT NULL,
    write_resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE RESTRICT,
    execution_profile_json TEXT,
    validation_profile_json TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS revision_work_items (
    revision_id TEXT NOT NULL REFERENCES plan_revisions(id) ON DELETE RESTRICT,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL CHECK (position >= 0),
    projection_state TEXT NOT NULL CHECK (projection_state IN
        ('pending', 'ready', 'active', 'needs_access', 'completed',
         'recovery_required', 'superseded')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (revision_id, work_item_id),
    UNIQUE (revision_id, position)
) STRICT;

CREATE TABLE IF NOT EXISTS work_item_dependencies (
    revision_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    depends_on_work_item_id TEXT NOT NULL,
    PRIMARY KEY (revision_id, work_item_id, depends_on_work_item_id),
    FOREIGN KEY (revision_id, work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT,
    FOREIGN KEY (revision_id, depends_on_work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT,
    CHECK (work_item_id <> depends_on_work_item_id)
) STRICT;

CREATE TABLE IF NOT EXISTS work_item_read_scopes (
    revision_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE RESTRICT,
    relative_path TEXT NOT NULL,
    PRIMARY KEY (revision_id, work_item_id, resource_id, relative_path),
    FOREIGN KEY (revision_id, work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS completion_criteria (
    revision_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    description TEXT NOT NULL,
    verification_type TEXT NOT NULL CHECK (verification_type IN
        ('check', 'artifact', 'workspace_predicate', 'human_review')),
    verification_json TEXT NOT NULL,
    PRIMARY KEY (revision_id, work_item_id, criterion_id),
    FOREIGN KEY (revision_id, work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    status TEXT NOT NULL CHECK (status IN
        ('reserved', 'running', 'awaiting_validation', 'awaiting_human_review',
         'completed', 'blocked', 'failed', 'cancelled',
         'abandoned_external_unknown')),
    failure_code TEXT,
    error_json TEXT,
    execution_profile_json TEXT,
    validation_profile_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (revision_id, work_item_id, attempt_no),
    FOREIGN KEY (revision_id, work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_attempt_per_work_item
ON attempts(revision_id, work_item_id)
WHERE status IN ('reserved', 'running', 'awaiting_validation', 'awaiting_human_review');

CREATE TABLE IF NOT EXISTS resource_leases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE RESTRICT,
    access_mode TEXT NOT NULL CHECK (access_mode = 'write'),
    acquired_at TEXT NOT NULL,
    released_at TEXT
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_write_lease
ON resource_leases(resource_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS runtime_action_intents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    kind TEXT NOT NULL CHECK (kind IN
        ('create_thread', 'start_turn', 'interrupt_turn', 'run_check',
         'recover_bind', 'recover_abandon')),
    status TEXT NOT NULL CHECK (status IN
        ('reserved', 'executing', 'succeeded', 'confirmed_no_effect',
         'unknown', 'cancelled', 'reconciled')),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    recovery_of_intent_id TEXT REFERENCES runtime_action_intents(id) ON DELETE RESTRICT,
    external_id TEXT,
    receipt_json TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (attempt_id, ordinal),
    CHECK (recovery_of_intent_id IS NULL OR recovery_of_intent_id <> id)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_intent_per_attempt
ON runtime_action_intents(attempt_id)
WHERE status IN ('reserved', 'executing');

CREATE TABLE IF NOT EXISTS runtime_bindings (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    source_intent_id TEXT NOT NULL REFERENCES runtime_action_intents(id) ON DELETE RESTRICT,
    thread_id TEXT,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (thread_id IS NOT NULL OR turn_id IS NOT NULL)
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_thread_binding
ON runtime_bindings(attempt_id, thread_id) WHERE thread_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_turn_binding
ON runtime_bindings(attempt_id, turn_id) WHERE turn_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS access_requests (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    revision_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    exact_path TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode = 'read'),
    resource_kind TEXT NOT NULL CHECK (resource_kind IN ('reference', 'runtime_dependency')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'granted', 'rejected')),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (revision_id, work_item_id)
        REFERENCES revision_work_items(revision_id, work_item_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS access_request_decisions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES access_requests(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK (decision IN ('granted', 'rejected')),
    authority_use_id TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (request_id)
) STRICT;

CREATE TABLE IF NOT EXISTS access_grants (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE REFERENCES access_requests(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
    resource_id TEXT NOT NULL UNIQUE REFERENCES resources(id) ON DELETE RESTRICT,
    exact_path TEXT NOT NULL,
    access_mode TEXT NOT NULL CHECK (access_mode = 'read'),
    file_identity TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS access_grant_invalidations (
    id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL UNIQUE REFERENCES access_grants(id) ON DELETE RESTRICT,
    observed_file_identity TEXT,
    observed_manifest_digest TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    media_type TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (digest, size)
) STRICT;

CREATE TABLE IF NOT EXISTS attempt_evidence (
    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE RESTRICT,
    criterion_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (attempt_id, criterion_id)
) STRICT;

CREATE TABLE IF NOT EXISTS authority_uses (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_digest TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    proof_digest TEXT NOT NULL,
    used_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS decision_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    decision_type TEXT NOT NULL,
    work_item_id TEXT,
    attempt_id TEXT,
    intent_id TEXT,
    reason_code TEXT NOT NULL,
    inputs_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS command_envelopes (
    command_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE REFERENCES decision_records(id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE IF NOT EXISTS history_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, sequence)
) STRICT;

CREATE TABLE IF NOT EXISTS state_attestations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    history_event_id TEXT NOT NULL REFERENCES history_events(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_attestation_entity
ON state_attestations(entity_type, entity_id, created_at, id);

CREATE TRIGGER IF NOT EXISTS tr_plan_content_immutable
BEFORE UPDATE ON plan_revisions
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_no <> OLD.revision_no
  OR NEW.content_digest <> OLD.content_digest
  OR NEW.summary <> OLD.summary
  OR NEW.draft_json <> OLD.draft_json
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'PLAN_REVISION_CONTENT_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_plan_status_transition
BEFORE UPDATE OF status ON plan_revisions
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'candidate' AND NEW.status IN ('approved_pending_activation', 'rejected')) OR
    (OLD.status = 'approved_pending_activation' AND NEW.status = 'active') OR
    (OLD.status = 'active' AND NEW.status = 'retired')
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_PLAN_REVISION_TRANSITION');
END;

CREATE TRIGGER IF NOT EXISTS tr_attempt_status_transition
BEFORE UPDATE OF status ON attempts
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'reserved' AND NEW.status IN
        ('running', 'blocked', 'failed', 'cancelled',
         'abandoned_external_unknown')) OR
    (OLD.status = 'running' AND NEW.status IN
        ('awaiting_validation', 'blocked', 'failed', 'cancelled',
         'abandoned_external_unknown')) OR
    (OLD.status = 'awaiting_validation' AND NEW.status IN
        ('completed', 'awaiting_human_review', 'blocked', 'failed')) OR
    (OLD.status = 'awaiting_human_review' AND NEW.status IN
        ('completed', 'blocked', 'cancelled'))
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_ATTEMPT_TRANSITION');
END;

CREATE TRIGGER IF NOT EXISTS tr_intent_status_transition
BEFORE UPDATE OF status ON runtime_action_intents
WHEN NEW.status <> OLD.status AND NOT (
    (OLD.status = 'reserved' AND NEW.status IN ('executing', 'cancelled')) OR
    (OLD.status = 'executing' AND NEW.status IN
        ('succeeded', 'confirmed_no_effect', 'unknown')) OR
    (OLD.status = 'unknown' AND NEW.status = 'reconciled')
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_INTENT_TRANSITION');
END;

CREATE TRIGGER IF NOT EXISTS tr_terminal_attempt_immutable
BEFORE UPDATE ON attempts
WHEN OLD.status IN ('completed', 'blocked', 'failed', 'cancelled',
                    'abandoned_external_unknown')
BEGIN
    SELECT RAISE(ABORT, 'TERMINAL_ATTEMPT_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_terminal_intent_immutable
BEFORE UPDATE ON runtime_action_intents
WHEN OLD.status IN ('succeeded', 'confirmed_no_effect', 'cancelled', 'reconciled')
BEGIN
    SELECT RAISE(ABORT, 'TERMINAL_INTENT_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_intent_request_immutable
BEFORE UPDATE ON runtime_action_intents
WHEN NEW.project_id <> OLD.project_id
  OR NEW.attempt_id <> OLD.attempt_id
  OR NEW.ordinal <> OLD.ordinal
  OR NEW.kind <> OLD.kind
  OR NEW.request_digest <> OLD.request_digest
  OR NEW.request_json <> OLD.request_json
  OR NEW.recovery_of_intent_id IS NOT OLD.recovery_of_intent_id
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'INTENT_REQUEST_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_attempt_identity_immutable
BEFORE UPDATE ON attempts
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_id <> OLD.revision_id
  OR NEW.work_item_id <> OLD.work_item_id
  OR NEW.attempt_no <> OLD.attempt_no
  OR NEW.execution_profile_json IS NOT OLD.execution_profile_json
  OR NEW.validation_profile_json IS NOT OLD.validation_profile_json
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'ATTEMPT_IDENTITY_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_project_definition_immutable
BEFORE UPDATE ON projects
WHEN NEW.name <> OLD.name
  OR NEW.canonical_root <> OLD.canonical_root
  OR NEW.execution_slots <> OLD.execution_slots
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'PROJECT_DEFINITION_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_access_request_content_immutable
BEFORE UPDATE ON access_requests
WHEN NEW.project_id <> OLD.project_id
  OR NEW.revision_id <> OLD.revision_id
  OR NEW.work_item_id <> OLD.work_item_id
  OR NEW.attempt_id <> OLD.attempt_id
  OR NEW.exact_path <> OLD.exact_path
  OR NEW.access_mode <> OLD.access_mode
  OR NEW.resource_kind <> OLD.resource_kind
  OR NEW.reason <> OLD.reason
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'ACCESS_REQUEST_CONTENT_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_revision_work_item_identity_immutable
BEFORE UPDATE ON revision_work_items
WHEN NEW.revision_id <> OLD.revision_id
  OR NEW.work_item_id <> OLD.work_item_id
  OR NEW.position <> OLD.position
BEGIN
    SELECT RAISE(ABORT, 'REVISION_WORK_ITEM_IDENTITY_IMMUTABLE');
END;

CREATE TRIGGER IF NOT EXISTS tr_work_item_projection_transition
BEFORE UPDATE OF projection_state ON revision_work_items
WHEN NEW.projection_state <> OLD.projection_state AND NOT (
    (OLD.projection_state = 'pending' AND NEW.projection_state IN ('ready','superseded')) OR
    (OLD.projection_state = 'ready' AND NEW.projection_state IN
        ('active','needs_access','superseded')) OR
    (OLD.projection_state = 'active' AND NEW.projection_state IN
        ('completed','needs_access','recovery_required','superseded')) OR
    (OLD.projection_state = 'needs_access' AND NEW.projection_state IN
        ('ready','superseded')) OR
    (OLD.projection_state = 'recovery_required' AND NEW.projection_state IN
        ('active','ready','superseded'))
)
BEGIN
    SELECT RAISE(ABORT, 'INVALID_WORK_ITEM_PROJECTION_TRANSITION');
END;

CREATE TRIGGER IF NOT EXISTS tr_unknown_blocks_normal_intent
BEFORE INSERT ON runtime_action_intents
WHEN NEW.recovery_of_intent_id IS NULL
 AND EXISTS (
     SELECT 1 FROM runtime_action_intents
     WHERE attempt_id = NEW.attempt_id AND status = 'unknown'
 )
BEGIN
    SELECT RAISE(ABORT, 'UNKNOWN_INTENT_REQUIRES_RECOVERY');
END;

CREATE TRIGGER IF NOT EXISTS tr_recovery_requires_unknown
BEFORE INSERT ON runtime_action_intents
WHEN NEW.recovery_of_intent_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM runtime_action_intents
     WHERE id = NEW.recovery_of_intent_id
       AND attempt_id = NEW.attempt_id
       AND status = 'unknown'
 )
BEGIN
    SELECT RAISE(ABORT, 'RECOVERY_MUST_REFERENCE_UNKNOWN_INTENT');
END;

CREATE TRIGGER IF NOT EXISTS tr_read_only_grant_resource
BEFORE INSERT ON access_grants
WHEN NEW.access_mode <> 'read'
BEGIN
    SELECT RAISE(ABORT, 'ACCESS_GRANT_MUST_BE_READ_ONLY');
END;

CREATE TRIGGER IF NOT EXISTS tr_no_update_access_grants
BEFORE UPDATE ON access_grants BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_access_grants
BEFORE DELETE ON access_grants BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_grant_invalidations
BEFORE UPDATE ON access_grant_invalidations BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_grant_invalidations
BEFORE DELETE ON access_grant_invalidations BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_access_decisions
BEFORE UPDATE ON access_request_decisions BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_access_decisions
BEFORE DELETE ON access_request_decisions BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_evidence_records
BEFORE UPDATE ON evidence_records BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_evidence_records
BEFORE DELETE ON evidence_records BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_attempt_evidence
BEFORE UPDATE ON attempt_evidence BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_attempt_evidence
BEFORE DELETE ON attempt_evidence BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_authority_uses
BEFORE UPDATE ON authority_uses BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_authority_uses
BEFORE DELETE ON authority_uses BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_decision_records
BEFORE UPDATE ON decision_records BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_decision_records
BEFORE DELETE ON decision_records BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_command_envelopes
BEFORE UPDATE ON command_envelopes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_command_envelopes
BEFORE DELETE ON command_envelopes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_history_events
BEFORE UPDATE ON history_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_history_events
BEFORE DELETE ON history_events BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_state_attestations
BEFORE UPDATE ON state_attestations BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_state_attestations
BEFORE DELETE ON state_attestations BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_protected_paths
BEFORE UPDATE ON protected_paths BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_protected_paths
BEFORE DELETE ON protected_paths BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_resources
BEFORE UPDATE ON resources BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_resources
BEFORE DELETE ON resources BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_work_items
BEFORE UPDATE ON work_items BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_work_items
BEFORE DELETE ON work_items BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_dependencies
BEFORE UPDATE ON work_item_dependencies BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_dependencies
BEFORE DELETE ON work_item_dependencies BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_read_scopes
BEFORE UPDATE ON work_item_read_scopes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_read_scopes
BEFORE DELETE ON work_item_read_scopes BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_completion_criteria
BEFORE UPDATE ON completion_criteria BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_completion_criteria
BEFORE DELETE ON completion_criteria BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_update_runtime_bindings
BEFORE UPDATE ON runtime_bindings BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS tr_no_delete_runtime_bindings
BEFORE DELETE ON runtime_bindings BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
"""


ATTESTED_TABLES: dict[str, tuple[str, str]] = {
    "project": ("projects", "id"),
    "resource": ("resources", "id"),
    "plan_revision": ("plan_revisions", "id"),
    "work_item": ("work_items", "id"),
    "attempt": ("attempts", "id"),
    "intent": ("runtime_action_intents", "id"),
    "lease": ("resource_leases", "id"),
    "access_request": ("access_requests", "id"),
}


class SQLiteLedger:
    """Gate 0B의 단일 권위 원장.

    모든 쓰기는 ``BEGIN IMMEDIATE`` 안에서 수행한다. 이 규칙과 부분 UNIQUE
    인덱스가 여러 run-once 호출 중 하나만 실행 슬롯을 차지하게 만든다.
    """

    def __init__(self, path: Path | str, *, clock: Any) -> None:
        self.path = Path(path)
        self.clock = clock

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10.0)
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
        connection = self._connect()
        try:
            connection.executescript(SCHEMA_SQL)
            existing = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_revision'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_revision', ?)",
                    (str(GATE0B_SCHEMA_REVISION),),
                )
            elif int(existing["value"]) != GATE0B_SCHEMA_REVISION:
                raise RuntimeError(
                    f"지원하지 않는 원장 schema revision: {existing['value']}"
                )
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator["SQLiteTransaction"]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield SQLiteTransaction(connection, self.clock)
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


class SQLiteTransaction:
    def __init__(self, connection: sqlite3.Connection, clock: Any) -> None:
        self.connection = connection
        self.clock = clock

    @property
    def now(self) -> str:
        return self.clock.now()

    def one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row:
        row = self.connection.execute(sql, parameters).fetchone()
        if row is None:
            raise DomainError("요청한 원장 항목을 찾을 수 없습니다.")
        return row

    def all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, parameters).fetchall())

    def event(
        self,
        project_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        previous = self.connection.execute(
            "SELECT sequence, event_hash FROM history_events "
            "WHERE project_id = ? ORDER BY sequence DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        sequence = 1 if previous is None else int(previous["sequence"]) + 1
        previous_hash = None if previous is None else previous["event_hash"]
        event_id = new_id("event")
        body = {
            "id": event_id,
            "project_id": project_id,
            "sequence": sequence,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at": self.now,
        }
        event_hash = sha256_digest(body)
        self.connection.execute(
            "INSERT INTO history_events "
            "(id, project_id, sequence, event_type, entity_type, entity_id, "
            " payload_json, previous_hash, event_hash, created_at) "
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
                body["created_at"],
            ),
        )
        return event_id

    def attest(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
        history_event_id: str,
    ) -> str:
        if entity_type == "revision_work_item":
            try:
                revision_id, work_item_id = entity_id.split(":", 1)
            except ValueError as error:
                raise ValueError("revision_work_item 복합 ID가 올바르지 않습니다.") from error
            row = self.one(
                "SELECT * FROM revision_work_items "
                "WHERE revision_id = ? AND work_item_id = ?",
                (revision_id, work_item_id),
            )
        else:
            try:
                table, key = ATTESTED_TABLES[entity_type]
            except KeyError as error:
                raise ValueError(f"attestation을 지원하지 않는 항목: {entity_type}") from error
            row = self.one(f"SELECT * FROM {table} WHERE {key} = ?", (entity_id,))
        digest = sha256_digest(dict(row))
        self.connection.execute(
            "INSERT INTO state_attestations "
            "(id, project_id, entity_type, entity_id, state_digest, "
            " history_event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("attestation"),
                project_id,
                entity_type,
                entity_id,
                digest,
                history_event_id,
                self.now,
            ),
        )
        return digest

    def event_and_attest(
        self,
        project_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        event_id = self.event(project_id, event_type, entity_type, entity_id, payload)
        self.attest(project_id, entity_type, entity_id, event_id)
        return event_id

    def register_project(
        self,
        definition: ProjectDefinition,
        resources: tuple[ResourceDefinition, ...],
    ) -> str:
        workspaces = [item for item in resources if item.kind is ResourceKind.WORKSPACE]
        if len(workspaces) != 1:
            raise DomainError("프로젝트에는 정확히 하나의 workspace 리소스가 필요합니다.")
        workspace = workspaces[0]
        if workspace.max_access is not AccessMode.WRITE:
            raise DomainError("workspace 리소스는 쓰기 가능해야 합니다.")
        if workspace.canonical_path != definition.canonical_root:
            raise DomainError("workspace 경로와 프로젝트 canonical_root가 다릅니다.")

        project_id = new_id("project")
        now = self.now
        self.connection.execute(
            "INSERT INTO projects "
            "(id, name, canonical_root, execution_slots, state, active_revision_id, "
            " quarantine_reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'active', NULL, NULL, ?, ?)",
            (
                project_id,
                definition.name,
                definition.canonical_root,
                definition.execution_slots,
                now,
                now,
            ),
        )
        event_id = self.event(
            project_id,
            "project.registered",
            "project",
            project_id,
            definition.model_dump(mode="json"),
        )
        self.attest(project_id, "project", project_id, event_id)
        for slot_no in range(1, definition.execution_slots + 1):
            self.connection.execute(
                "INSERT INTO project_execution_slots(project_id, slot_no) VALUES (?, ?)",
                (project_id, slot_no),
            )

        for resource in resources:
            resource_id = resource.id or new_id("resource")
            payload = resource.model_dump(mode="json", exclude={"id"}, exclude_none=True)
            self.connection.execute(
                "INSERT INTO resources "
                "(id, project_id, kind, canonical_path, max_access, include_json, "
                " exclude_json, file_identity, manifest_digest, definition_digest, "
                " source_access_grant_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    resource_id,
                    project_id,
                    resource.kind.value,
                    resource.canonical_path,
                    resource.max_access.value,
                    canonical_json(resource.include),
                    canonical_json(resource.exclude),
                    resource.file_identity,
                    resource.manifest_digest,
                    sha256_digest(payload),
                    now,
                ),
            )
            self.event_and_attest(
                project_id,
                "resource.registered",
                "resource",
                resource_id,
                payload,
            )
        return project_id

    def import_candidate_plan(self, draft: PlanDraft) -> str:
        project = self.one("SELECT * FROM projects WHERE id = ?", (draft.project_id,))
        if project["state"] == "quarantined":
            raise DomainError("격리된 프로젝트에는 계획을 가져올 수 없습니다.")
        existing = self.connection.execute(
            "SELECT id FROM plan_revisions WHERE project_id = ? AND content_digest = ?",
            (draft.project_id, draft.content_digest),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        revision_no = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_no "
                "FROM plan_revisions WHERE project_id = ?",
                (draft.project_id,),
            ).fetchone()["next_no"]
        )
        revision_id = new_id("revision")
        now = self.now
        self.connection.execute(
            "INSERT INTO plan_revisions "
            "(id, project_id, revision_no, status, content_digest, summary, "
            " draft_json, created_at) VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?)",
            (
                revision_id,
                draft.project_id,
                revision_no,
                draft.content_digest,
                draft.summary,
                canonical_json(draft),
                now,
            ),
        )
        event_id = self.event_and_attest(
            draft.project_id,
            "plan.candidate_imported",
            "plan_revision",
            revision_id,
            {"revision_no": revision_no, "content_digest": draft.content_digest},
        )
        del event_id

        resource_rows = self.all(
            "SELECT id, kind, max_access FROM resources WHERE project_id = ?",
            (draft.project_id,),
        )
        resource_map = {row["id"]: row for row in resource_rows}
        work_ids: dict[str, str] = {}
        for position, item in enumerate(draft.work_items):
            write_resource = resource_map.get(item.write_resource_id)
            if write_resource is None or write_resource["kind"] != "workspace":
                raise DomainError("WorkItem 쓰기 리소스가 프로젝트 workspace가 아닙니다.")
            if write_resource["max_access"] != "write":
                raise DomainError("WorkItem 쓰기 리소스에 쓰기 권한이 없습니다.")
            for scope in item.read_scopes:
                if scope.resource_id not in resource_map:
                    raise DomainError("등록되지 않은 읽기 리소스가 계획에 포함됐습니다.")
            if item.previous_work_item_id:
                previous = self.one(
                    "SELECT project_id FROM work_items WHERE id = ?",
                    (item.previous_work_item_id,),
                )
                if previous["project_id"] != draft.project_id:
                    raise DomainError("다른 프로젝트 WorkItem을 lineage로 사용할 수 없습니다.")
            work_id = new_id("work")
            work_ids[item.client_ref] = work_id
            self.connection.execute(
                "INSERT INTO work_items "
                "(id, project_id, client_ref, previous_work_item_id, definition_digest, "
                " goal, write_resource_id, execution_profile_json, "
                " validation_profile_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    work_id,
                    draft.project_id,
                    item.client_ref,
                    item.previous_work_item_id,
                    item.definition_digest,
                    item.goal,
                    item.write_resource_id,
                    None
                    if item.execution_profile is None
                    else canonical_json(item.execution_profile),
                    None
                    if item.validation_profile is None
                    else canonical_json(item.validation_profile),
                    now,
                ),
            )
            self.connection.execute(
                "INSERT INTO revision_work_items "
                "(revision_id, work_item_id, position, projection_state, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (revision_id, work_id, position, now),
            )
            work_event_id = self.event_and_attest(
                draft.project_id,
                "work_item.created",
                "work_item",
                work_id,
                {
                    "revision_id": revision_id,
                    "client_ref": item.client_ref,
                    "definition_digest": item.definition_digest,
                },
            )
            self.attest(
                draft.project_id,
                "revision_work_item",
                f"{revision_id}:{work_id}",
                work_event_id,
            )
            for scope in item.read_scopes:
                self.connection.execute(
                    "INSERT INTO work_item_read_scopes "
                    "(revision_id, work_item_id, resource_id, relative_path) "
                    "VALUES (?, ?, ?, ?)",
                    (revision_id, work_id, scope.resource_id, scope.relative_path),
                )
            for criterion in item.completion_criteria:
                self.connection.execute(
                    "INSERT INTO completion_criteria "
                    "(revision_id, work_item_id, criterion_id, description, "
                    " verification_type, verification_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        revision_id,
                        work_id,
                        criterion.criterion_id,
                        criterion.description,
                        criterion.verification.type.value,
                        canonical_json(criterion.verification),
                    ),
                )
        for item in draft.work_items:
            for dependency in item.dependencies:
                self.connection.execute(
                    "INSERT INTO work_item_dependencies "
                    "(revision_id, work_item_id, depends_on_work_item_id) "
                    "VALUES (?, ?, ?)",
                    (revision_id, work_ids[item.client_ref], work_ids[dependency]),
                )
        return revision_id

    def consume_authority(
        self,
        *,
        action: str,
        target_digest: str,
        nonce: str,
        issued_at: str,
        expires_at: str,
        proof_digest: str,
    ) -> str:
        authority_use_id = new_id("authority")
        self.connection.execute(
            "INSERT INTO authority_uses "
            "(id, action, target_digest, nonce, issued_at, expires_at, proof_digest, used_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                authority_use_id,
                action,
                target_digest,
                nonce,
                issued_at,
                expires_at,
                proof_digest,
                self.now,
            ),
        )
        return authority_use_id

    def record_decision(self, decision: Decision, envelope: CommandEnvelope) -> None:
        decision_id = new_id("decision")
        self.connection.execute(
            "INSERT INTO decision_records "
            "(id, project_id, decision_type, work_item_id, attempt_id, intent_id, "
            " reason_code, inputs_digest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                decision.project_id,
                decision.decision_type.value,
                decision.work_item_id,
                decision.attempt_id,
                decision.intent_id,
                decision.reason_code,
                decision.inputs_digest,
                self.now,
            ),
        )
        self.connection.execute(
            "INSERT INTO command_envelopes "
            "(command_id, correlation_id, observed_at, decision_id) VALUES (?, ?, ?, ?)",
            (
                envelope.command_id,
                envelope.correlation_id,
                envelope.observed_at,
                decision_id,
            ),
        )

    def next_intent_ordinal(self, attempt_id: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS next_ordinal "
                "FROM runtime_action_intents WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()["next_ordinal"]
        )

    def create_intent(
        self,
        *,
        project_id: str,
        attempt_id: str,
        kind: IntentKind,
        request: dict[str, Any],
        recovery_of_intent_id: str | None = None,
    ) -> str:
        intent_id = new_id("intent")
        ordinal = self.next_intent_ordinal(attempt_id)
        self.connection.execute(
            "INSERT INTO runtime_action_intents "
            "(id, project_id, attempt_id, ordinal, kind, status, request_digest, "
            " request_json, recovery_of_intent_id, created_at, updated_at) "
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
                self.now,
                self.now,
            ),
        )
        self.event_and_attest(
            project_id,
            "intent.reserved",
            "intent",
            intent_id,
            {
                "attempt_id": attempt_id,
                "kind": kind.value,
                "recovery_of_intent_id": recovery_of_intent_id,
            },
        )
        return intent_id

    def set_project_state(
        self, project_id: str, state: str, reason: str | None
    ) -> None:
        self.connection.execute(
            "UPDATE projects SET state = ?, quarantine_reason = ?, updated_at = ? "
            "WHERE id = ?",
            (state, reason, self.now, project_id),
        )
        self.event_and_attest(
            project_id,
            f"project.{state}",
            "project",
            project_id,
            {"reason": reason},
        )

    def set_intent_status(
        self,
        intent_id: str,
        status: IntentStatus,
        *,
        external_id: str | None = None,
        receipt: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = self.one("SELECT * FROM runtime_action_intents WHERE id = ?", (intent_id,))
        terminal = status in {
            IntentStatus.SUCCEEDED,
            IntentStatus.CONFIRMED_NO_EFFECT,
            IntentStatus.CANCELLED,
            IntentStatus.RECONCILED,
        }
        self.connection.execute(
            "UPDATE runtime_action_intents SET status = ?, external_id = COALESCE(?, external_id), "
            "receipt_json = COALESCE(?, receipt_json), error_json = COALESCE(?, error_json), "
            "updated_at = ?, finished_at = CASE WHEN ? THEN ? ELSE finished_at END "
            "WHERE id = ?",
            (
                status.value,
                external_id,
                None if receipt is None else canonical_json(receipt),
                None if error is None else canonical_json(error),
                self.now,
                int(terminal),
                self.now,
                intent_id,
            ),
        )
        self.event_and_attest(
            row["project_id"],
            f"intent.{status.value}",
            "intent",
            intent_id,
            {"previous_status": row["status"], "external_id": external_id},
        )

    def set_attempt_status(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        failure_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        row = self.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
        terminal = status not in ACTIVE_ATTEMPT_STATUSES
        self.connection.execute(
            "UPDATE attempts SET status = ?, failure_code = ?, error_json = ?, "
            "started_at = CASE WHEN ? = 'running' AND started_at IS NULL THEN ? ELSE started_at END, "
            "finished_at = CASE WHEN ? THEN ? ELSE finished_at END, updated_at = ? "
            "WHERE id = ?",
            (
                status.value,
                failure_code,
                None if error is None else canonical_json(error),
                status.value,
                self.now,
                int(terminal),
                self.now,
                self.now,
                attempt_id,
            ),
        )
        self.event_and_attest(
            row["project_id"],
            f"attempt.{status.value}",
            "attempt",
            attempt_id,
            {"previous_status": row["status"], "failure_code": failure_code},
        )

    def release_attempt_resources(self, attempt_id: str) -> None:
        attempt = self.one("SELECT project_id FROM attempts WHERE id = ?", (attempt_id,))
        now = self.now
        leases = self.all(
            "SELECT id FROM resource_leases WHERE attempt_id = ? AND released_at IS NULL",
            (attempt_id,),
        )
        for lease in leases:
            self.connection.execute(
                "UPDATE resource_leases SET released_at = ? WHERE id = ?",
                (now, lease["id"]),
            )
            self.event_and_attest(
                attempt["project_id"],
                "lease.released",
                "lease",
                lease["id"],
                {"attempt_id": attempt_id},
            )
        self.connection.execute(
            "UPDATE project_execution_slots SET attempt_id = NULL, acquired_at = NULL "
            "WHERE attempt_id = ?",
            (attempt_id,),
        )

    def set_work_item_state(
        self,
        revision_id: str,
        work_item_id: str,
        state: WorkItemState,
        *,
        project_id: str,
    ) -> None:
        previous = self.one(
            "SELECT projection_state FROM revision_work_items "
            "WHERE revision_id = ? AND work_item_id = ?",
            (revision_id, work_item_id),
        )["projection_state"]
        self.connection.execute(
            "UPDATE revision_work_items SET projection_state = ?, updated_at = ? "
            "WHERE revision_id = ? AND work_item_id = ?",
            (state.value, self.now, revision_id, work_item_id),
        )
        event_id = self.event(
            project_id,
            f"work_item.{state.value}",
            "revision_work_item",
            f"{revision_id}:{work_item_id}",
            {"previous_state": previous},
        )
        self.attest(
            project_id,
            "revision_work_item",
            f"{revision_id}:{work_item_id}",
            event_id,
        )

    def bind_runtime(
        self,
        *,
        attempt_id: str,
        source_intent_id: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        binding_id = new_id("binding")
        self.connection.execute(
            "INSERT INTO runtime_bindings "
            "(id, attempt_id, source_intent_id, thread_id, turn_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (binding_id, attempt_id, source_intent_id, thread_id, turn_id, self.now),
        )
        return binding_id

    def reserve_attempt(
        self, *, project_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row] | None:
        project = self.one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if project["state"] == "quarantined":
            return None
        revision_id = project["active_revision_id"]
        if revision_id is None:
            return None
        if self.connection.execute(
            "SELECT 1 FROM runtime_action_intents "
            "WHERE project_id = ? AND status = 'unknown' LIMIT 1",
            (project_id,),
        ).fetchone():
            self.set_project_state(project_id, "quarantined", "UNKNOWN_RUNTIME_EFFECT")
            return None

        candidates = self.all(
            "SELECT rwi.work_item_id, rwi.projection_state, rwi.position, "
            "       wi.goal, wi.write_resource_id, wi.execution_profile_json, "
            "       wi.validation_profile_json "
            "FROM revision_work_items rwi "
            "JOIN work_items wi ON wi.id = rwi.work_item_id "
            "WHERE rwi.revision_id = ? AND rwi.projection_state IN ('pending', 'ready') "
            "ORDER BY rwi.position",
            (revision_id,),
        )
        chosen: sqlite3.Row | None = None
        for candidate in candidates:
            unsatisfied = self.connection.execute(
                "SELECT 1 FROM work_item_dependencies d "
                "JOIN revision_work_items dep ON dep.revision_id = d.revision_id "
                " AND dep.work_item_id = d.depends_on_work_item_id "
                "WHERE d.revision_id = ? AND d.work_item_id = ? "
                "AND dep.projection_state <> 'completed' LIMIT 1",
                (revision_id, candidate["work_item_id"]),
            ).fetchone()
            if unsatisfied is None:
                if candidate["projection_state"] == WorkItemState.PENDING.value:
                    self.set_work_item_state(
                        revision_id,
                        candidate["work_item_id"],
                        WorkItemState.READY,
                        project_id=project_id,
                    )
                chosen = candidate
                break
        if chosen is None:
            return None
        slot = self.connection.execute(
            "SELECT slot_no FROM project_execution_slots "
            "WHERE project_id = ? AND attempt_id IS NULL ORDER BY slot_no LIMIT 1",
            (project_id,),
        ).fetchone()
        if slot is None:
            return None

        attempt_no = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no FROM attempts "
                "WHERE revision_id = ? AND work_item_id = ?",
                (revision_id, chosen["work_item_id"]),
            ).fetchone()["next_no"]
        )
        attempt_id = new_id("attempt")
        self.connection.execute(
            "INSERT INTO attempts "
            "(id, project_id, revision_id, work_item_id, attempt_no, status, "
            " execution_profile_json, validation_profile_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)",
            (
                attempt_id,
                project_id,
                revision_id,
                chosen["work_item_id"],
                attempt_no,
                chosen["execution_profile_json"],
                chosen["validation_profile_json"],
                self.now,
                self.now,
            ),
        )
        cursor = self.connection.execute(
            "UPDATE project_execution_slots SET attempt_id = ?, acquired_at = ? "
            "WHERE project_id = ? AND slot_no = ? AND attempt_id IS NULL",
            (attempt_id, self.now, project_id, slot["slot_no"]),
        )
        if cursor.rowcount != 1:
            raise DomainError("실행 슬롯을 원자적으로 확보하지 못했습니다.")
        lease_id = new_id("lease")
        self.connection.execute(
            "INSERT INTO resource_leases "
            "(id, project_id, attempt_id, resource_id, access_mode, acquired_at) "
            "VALUES (?, ?, ?, ?, 'write', ?)",
            (
                lease_id,
                project_id,
                attempt_id,
                chosen["write_resource_id"],
                self.now,
            ),
        )
        self.set_work_item_state(
            revision_id,
            chosen["work_item_id"],
            WorkItemState.ACTIVE,
            project_id=project_id,
        )
        attempt_event = self.event_and_attest(
            project_id,
            "attempt.reserved",
            "attempt",
            attempt_id,
            {
                "revision_id": revision_id,
                "work_item_id": chosen["work_item_id"],
                "attempt_no": attempt_no,
                "slot_no": slot["slot_no"],
            },
        )
        del attempt_event
        self.event_and_attest(
            project_id,
            "lease.acquired",
            "lease",
            lease_id,
            {"attempt_id": attempt_id, "resource_id": chosen["write_resource_id"]},
        )
        request = {
            "workspace": project["canonical_root"],
            "title": f"FlowMarshal: {chosen['goal'][:80]}",
            "goal": chosen["goal"],
        }
        intent_id = self.create_intent(
            project_id=project_id,
            attempt_id=attempt_id,
            kind=IntentKind.CREATE_THREAD,
            request=request,
        )
        return (
            self.one("SELECT * FROM attempts WHERE id = ?", (attempt_id,)),
            self.one("SELECT * FROM runtime_action_intents WHERE id = ?", (intent_id,)),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)
