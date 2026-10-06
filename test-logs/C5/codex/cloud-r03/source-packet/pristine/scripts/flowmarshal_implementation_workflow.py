"""승인된 FlowMarshal 구현 DAG를 기존 예약 원장 위에서 실행하는 보존적 레지스트리.

이 모듈은 Engine 제품 원장이 아니다. 기존 v1 예약 원장의 테이블과 이력을
그대로 두고, 승인된 구현 명세·시도·검토 증거를 별도 additive 테이블에 기록한다.
Codex 작업은 명시적 full-access/never 옵션을 가진 로컬 CLI transport로 시작할 수 있다.
생성 직후 로컬 Codex 원자료에서 실제 권한·프로젝트 결속을 검증한 receipt만 만든다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


IMPLEMENTATION_SCHEMA_VERSION = 3
WRITER_CONTRACT_VERSION = 3
LOCK_NAME = "scheduler"
WORKFLOW_ID = "flowmarshal-1.0-redesign"
TERMINAL_DISPATCH_STATUSES = {
    "completed", "failed", "cancelled", "blocked", "needs_attention", "creation_failed",
    "interrupted",
}
INTERRUPT_ORIGINS = {"user", "app_lifecycle", "safety_policy", "scheduler", "unknown"}
TASK_STATUSES = {
    "PENDING", "RESERVED", "DISPATCHED", "AWAITING_REVIEW", "SUCCEEDED", "FAILED"
}
FAILURE_CLASSES = {
    "implementation", "context", "task_contract", "dependency", "environment",
    "requirement_change", "external_unknown",
}
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_PERMISSION_PROFILE = ":danger-full-access"
REQUIRED_APPROVAL_POLICY = "never"
UNCONFIRMED_DISPATCH_STATUS = "launch_ready_v3"
MIN_LAUNCH_LEASE_HEADROOM_SECONDS = 5
POLICY_REMEDIATION_ARCHIVE_DIR = ".flowmarshal-policy-remediation-snapshots"
FRESHNESS_SCOPES = {"dependency", "completion"}
EVALUATION_FRESHNESS_KINDS = {"source", "fixture", "prompt", "schema", "lock", "evaluator"}
# revision 1이 evidence_contract 필드를 지원하기 전에 등록된 명세의 명시적 호환 정책.
# 자연어를 재해석하지 않고 등록된 immutable task spec digest에만 정확히 결속한다.
LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256 = {
    "d7b3c38d652b0d616ba04faab0997f7c0e942e0fce9bbb837518092721cd8250": "evaluation",  # FM-09
    "7248d2f6e1e3ff536e1b5c02489cf20128d6c8ac49e0a7e762b7e1d1a1009c12": "evaluation",  # FM-11
    "b2d70d382d320620bf5f1abe4e83ff7d246c13c2f987c0a4e7a9208a28ee74c0": "evaluation",  # FM-13
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS implementation_schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    backup_sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS implementation_workflows (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    approval_plan_path TEXT NOT NULL,
    approval_plan_sha256 TEXT NOT NULL,
    baseline_commit TEXT NOT NULL,
    source_root TEXT NOT NULL,
    artifact_root TEXT NOT NULL,
    automation_id TEXT NOT NULL,
    parent_thread_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE',
    registered_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (workflow_id, workflow_revision),
    UNIQUE (workflow_id, spec_sha256)
);

CREATE TABLE IF NOT EXISTS implementation_tasks (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL,
    title TEXT NOT NULL,
    lane TEXT NOT NULL,
    project_id TEXT,
    model TEXT,
    reasoning_effort TEXT,
    model_selection_reason TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    active_attempt_no INTEGER,
    failure_fingerprint TEXT,
    recovery_for_task_id TEXT,
    recovery_for_fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, workflow_revision, task_id, task_revision),
    UNIQUE (workflow_id, workflow_revision, order_index, task_id),
    FOREIGN KEY (workflow_id, workflow_revision)
        REFERENCES implementation_workflows(workflow_id, workflow_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_dependencies (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    depends_on_task_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        workflow_id, workflow_revision, task_id, task_revision,
        depends_on_task_id, depends_on_task_revision
    ),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision),
    FOREIGN KEY (workflow_id, workflow_revision, depends_on_task_id, depends_on_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_checks (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    check_id TEXT NOT NULL,
    criterion TEXT NOT NULL,
    method TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, workflow_revision, task_id, task_revision, check_id),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_attempts (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    purpose_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    finished_at TEXT,
    failure_fingerprint TEXT,
    evidence_sha256 TEXT,
    PRIMARY KEY (workflow_id, workflow_revision, task_id, task_revision, attempt_no),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    purpose_key TEXT NOT NULL UNIQUE,
    assignment_prompt TEXT NOT NULL,
    assignment_sha256 TEXT NOT NULL,
    receipt_path TEXT,
    receipt_sha256 TEXT,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    terminal_observed_at TEXT,
    UNIQUE (workflow_id, workflow_revision, task_id, task_revision, attempt_no),
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision, attempt_no)
        REFERENCES implementation_task_attempts(
            workflow_id, workflow_revision, task_id, task_revision, attempt_no
        )
);

CREATE TABLE IF NOT EXISTS implementation_launch_claims (
    dispatch_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    assignment_sha256 TEXT NOT NULL CHECK (length(assignment_sha256) = 64),
    claim_token TEXT NOT NULL UNIQUE,
    owner_run_id TEXT NOT NULL,
    lease_acquired_at TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    intent_origin TEXT NOT NULL CHECK (intent_origin = 'fresh'),
    process_started_at TEXT,
    process_id INTEGER,
    thread_id TEXT,
    receipt_path TEXT,
    receipt_sha256 TEXT CHECK (receipt_sha256 IS NULL OR length(receipt_sha256) = 64),
    resolved_by_run_id TEXT,
    resolved_at TEXT,
    FOREIGN KEY (dispatch_id) REFERENCES implementation_task_dispatches(dispatch_id),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision, attempt_no)
        REFERENCES implementation_task_attempts(
            workflow_id, workflow_revision, task_id, task_revision, attempt_no
        ),
    FOREIGN KEY (owner_run_id) REFERENCES orchestration_runs(run_id),
    FOREIGN KEY (resolved_by_run_id) REFERENCES orchestration_runs(run_id),
    CHECK ((process_started_at IS NULL) = (process_id IS NULL)),
    CHECK ((resolved_at IS NULL) = (thread_id IS NULL)),
    CHECK ((resolved_at IS NULL) = (receipt_path IS NULL)),
    CHECK ((resolved_at IS NULL) = (receipt_sha256 IS NULL)),
    CHECK ((resolved_at IS NULL) = (resolved_by_run_id IS NULL))
);

CREATE TABLE IF NOT EXISTS implementation_evidence (
    evidence_sha256 TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    evidence_path TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision, attempt_no)
        REFERENCES implementation_task_attempts(
            workflow_id, workflow_revision, task_id, task_revision, attempt_no
        )
);

CREATE TABLE IF NOT EXISTS implementation_check_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_sha256 TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    check_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'NOT_RUN')),
    detail TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (evidence_sha256, check_id),
    FOREIGN KEY (evidence_sha256) REFERENCES implementation_evidence(evidence_sha256),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision, check_id)
        REFERENCES implementation_task_checks(workflow_id, workflow_revision, task_id, task_revision, check_id)
);

CREATE TABLE IF NOT EXISTS implementation_recoveries (
    recovery_task_id TEXT NOT NULL,
    recovery_task_revision INTEGER NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    original_task_id TEXT NOT NULL,
    original_task_revision INTEGER NOT NULL,
    lineage_root_task_id TEXT NOT NULL,
    failure_fingerprint TEXT NOT NULL,
    registration_no INTEGER NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    direct_evidence_digest TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, workflow_revision, recovery_task_id, recovery_task_revision),
    UNIQUE (workflow_id, workflow_revision, lineage_root_task_id, failure_fingerprint, registration_no),
    UNIQUE (workflow_id, workflow_revision, lineage_root_task_id, failure_fingerprint, direct_evidence_digest),
    FOREIGN KEY (workflow_id, workflow_revision, recovery_task_id, recovery_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE INDEX IF NOT EXISTS idx_impl_tasks_ready
    ON implementation_tasks(workflow_id, workflow_revision, status, order_index, task_id);
CREATE INDEX IF NOT EXISTS idx_impl_dispatch_task
    ON implementation_task_dispatches(workflow_id, workflow_revision, task_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_impl_launch_claim_owner
    ON implementation_launch_claims(owner_run_id, claimed_at);
CREATE INDEX IF NOT EXISTS idx_impl_check_history
    ON implementation_check_results(workflow_id, workflow_revision, task_id, result_id);
"""


REVISION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS implementation_writer_contract (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    minimum_writer_version INTEGER NOT NULL,
    installed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS implementation_workflow_heads (
    workflow_id TEXT PRIMARY KEY,
    active_workflow_revision INTEGER NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    writer_contract_version INTEGER NOT NULL,
    previous_workflow_revision INTEGER,
    activation_decision_sha256 TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    FOREIGN KEY (workflow_id, active_workflow_revision)
        REFERENCES implementation_workflows(workflow_id, workflow_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_lineage (
    workflow_id TEXT NOT NULL,
    from_workflow_revision INTEGER NOT NULL,
    from_task_id TEXT NOT NULL,
    from_task_revision INTEGER NOT NULL,
    to_workflow_revision INTEGER NOT NULL,
    to_task_id TEXT NOT NULL,
    to_task_revision INTEGER NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('unchanged', 'split', 'replacement')),
    decision TEXT NOT NULL,
    decision_sha256 TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (
        workflow_id, from_workflow_revision, from_task_id, from_task_revision,
        to_workflow_revision, to_task_id, to_task_revision
    ),
    UNIQUE (
        workflow_id, to_workflow_revision, to_task_id, to_task_revision
    ),
    CHECK (from_workflow_revision < to_workflow_revision),
    FOREIGN KEY (workflow_id, from_workflow_revision, from_task_id, from_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision),
    FOREIGN KEY (workflow_id, to_workflow_revision, to_task_id, to_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE TABLE IF NOT EXISTS implementation_task_carry_forwards (
    workflow_id TEXT NOT NULL,
    to_workflow_revision INTEGER NOT NULL,
    to_task_id TEXT NOT NULL,
    to_task_revision INTEGER NOT NULL,
    from_workflow_revision INTEGER NOT NULL,
    from_task_id TEXT NOT NULL,
    from_task_revision INTEGER NOT NULL,
    decision TEXT NOT NULL,
    decision_sha256 TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    invalidated_at TEXT,
    invalidated_by_attempt_no INTEGER,
    PRIMARY KEY (workflow_id, to_workflow_revision,to_task_id,to_task_revision),
    FOREIGN KEY (workflow_id, from_workflow_revision, from_task_id, from_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision),
    FOREIGN KEY (workflow_id, to_workflow_revision, to_task_id, to_task_revision)
        REFERENCES implementation_tasks(workflow_id, workflow_revision, task_id, task_revision)
);

CREATE TABLE IF NOT EXISTS implementation_check_carry_forwards (
    workflow_id TEXT NOT NULL,
    to_workflow_revision INTEGER NOT NULL,
    to_task_id TEXT NOT NULL,
    to_task_revision INTEGER NOT NULL,
    to_check_id TEXT NOT NULL,
    from_workflow_revision INTEGER NOT NULL,
    from_task_id TEXT NOT NULL,
    from_task_revision INTEGER NOT NULL,
    from_check_id TEXT NOT NULL,
    source_evidence_sha256 TEXT NOT NULL,
    source_result_id INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (
        workflow_id,to_workflow_revision,to_task_id,to_task_revision,to_check_id
    ),
    FOREIGN KEY (workflow_id,to_workflow_revision,to_task_id,to_task_revision)
        REFERENCES implementation_task_carry_forwards(
            workflow_id,to_workflow_revision,to_task_id,to_task_revision
        ),
    FOREIGN KEY (source_evidence_sha256) REFERENCES implementation_evidence(evidence_sha256),
    FOREIGN KEY (source_result_id) REFERENCES implementation_check_results(result_id),
    FOREIGN KEY (workflow_id,to_workflow_revision,to_task_id,to_task_revision,to_check_id)
        REFERENCES implementation_task_checks(
            workflow_id,workflow_revision,task_id,task_revision,check_id
        ),
    FOREIGN KEY (workflow_id,from_workflow_revision,from_task_id,from_task_revision,from_check_id)
        REFERENCES implementation_task_checks(
            workflow_id,workflow_revision,task_id,task_revision,check_id
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_impl_one_active_workflow
    ON implementation_workflows(workflow_id) WHERE state='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_impl_one_task_identity_per_workflow_revision
    ON implementation_tasks(workflow_id,workflow_revision,task_id);
CREATE INDEX IF NOT EXISTS idx_impl_lineage_source
    ON implementation_task_lineage(
        workflow_id,from_workflow_revision,from_task_id,from_task_revision
    );
CREATE INDEX IF NOT EXISTS idx_impl_carry_source
    ON implementation_task_carry_forwards(
        workflow_id,from_workflow_revision,from_task_id,from_task_revision
    );

CREATE TRIGGER IF NOT EXISTS implementation_task_revision_one_insert
BEFORE INSERT ON implementation_tasks
WHEN NEW.task_revision <> 1
BEGIN
    SELECT RAISE(ABORT, 'FLOWMARSHAL_TASK_REVISION_MUST_BE_ONE');
END;

CREATE TRIGGER IF NOT EXISTS implementation_task_revision_one_update
BEFORE UPDATE OF task_revision ON implementation_tasks
WHEN NEW.task_revision <> 1
BEGIN
    SELECT RAISE(ABORT, 'FLOWMARSHAL_TASK_REVISION_MUST_BE_ONE');
END;

CREATE TRIGGER IF NOT EXISTS implementation_workflow_state_insert
BEFORE INSERT ON implementation_workflows
WHEN NEW.state NOT IN ('DRAFT','ACTIVE','RETIRED','COMPLETE')
BEGIN
    SELECT RAISE(ABORT, 'FLOWMARSHAL_WORKFLOW_STATE_INVALID');
END;

CREATE TRIGGER IF NOT EXISTS implementation_workflow_state_update
BEFORE UPDATE OF state ON implementation_workflows
WHEN NEW.state NOT IN ('DRAFT','ACTIVE','RETIRED','COMPLETE')
BEGIN
    SELECT RAISE(ABORT, 'FLOWMARSHAL_WORKFLOW_STATE_INVALID');
END;
"""


WRITER_FENCE_TABLES = (
    "metadata", "orchestration_state", "orchestration_runs", "dispatches",
    "orchestration_events", "source_refs", "orchestration_locks",
    "implementation_schema_migrations", "implementation_workflows", "implementation_tasks",
    "implementation_task_dependencies", "implementation_task_checks",
    "implementation_task_attempts", "implementation_task_dispatches", "implementation_evidence",
    "implementation_launch_claims",
    "implementation_check_results", "implementation_recoveries",
    "implementation_reasoning_policies", "implementation_dispatch_model_bindings",
    "implementation_writer_contract", "implementation_workflow_heads",
    "implementation_task_lineage", "implementation_task_carry_forwards",
    "implementation_check_carry_forwards",
)


REASONING_POLICY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS implementation_reasoning_policies (
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    policy_scope TEXT NOT NULL CHECK (policy_scope IN ('task', 'architecture_recovery')),
    policy_key TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    model TEXT NOT NULL,
    initial_effort TEXT NOT NULL,
    effort_ladder_json TEXT NOT NULL,
    escalation_trigger TEXT NOT NULL CHECK (
        escalation_trigger IN ('independent_review_failure', 'same_failure_with_new_evidence')
    ),
    decision_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        workflow_id, workflow_revision, policy_scope, policy_key, policy_revision
    ),
    FOREIGN KEY (workflow_id, workflow_revision)
        REFERENCES implementation_workflows(workflow_id, workflow_revision)
);

CREATE TABLE IF NOT EXISTS implementation_dispatch_model_bindings (
    dispatch_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    workflow_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    task_revision INTEGER NOT NULL,
    attempt_no INTEGER NOT NULL,
    model TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    escalation_step INTEGER NOT NULL,
    policy_scope TEXT,
    policy_key TEXT,
    policy_revision INTEGER,
    binding_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (dispatch_id) REFERENCES dispatches(dispatch_id),
    FOREIGN KEY (workflow_id, workflow_revision, task_id, task_revision, attempt_no)
        REFERENCES implementation_task_attempts(
            workflow_id, workflow_revision, task_id, task_revision, attempt_no
        )
);

CREATE INDEX IF NOT EXISTS idx_impl_reasoning_policy_latest
    ON implementation_reasoning_policies(
        workflow_id, workflow_revision, policy_scope, policy_key, policy_revision DESC
    );
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_evidence_path(source_root: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(source_root) / path
    return path.resolve()


def json_output(value: Any) -> None:
    # Windows의 호출 shell/code page가 UTF-8이 아니어도 JSON transport가
    # 손상되지 않게 stdout은 ASCII escape만 사용한다. JSON parser는 원문
    # Unicode를 복원하므로 title/prompt의 의미는 바뀌지 않는다.
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2))


def _require_unicode_integrity(value: Any, label: str) -> None:
    if "\ufffd" in canonical_json(value):
        raise ValueError(f"{label}에 Unicode replacement character(U+FFFD)가 있습니다")


def _validate_create_thread_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("create_thread request는 JSON 객체여야 합니다")
    required = {"prompt", "title", "model", "thinking", "target"}
    _expect_keys(request, required, required, "create_thread request")
    for field in ("prompt", "title", "model", "thinking"):
        _nonempty_string(request[field], f"create_thread request.{field}")
    target = request["target"]
    if not isinstance(target, dict):
        raise ValueError("create_thread request.target은 객체여야 합니다")
    _expect_keys(
        target,
        {"type", "projectId", "environment"},
        {"type", "projectId", "environment"},
        "create_thread request.target",
    )
    if target["type"] != "project":
        raise ValueError("create_thread request.target.type은 project여야 합니다")
    _nonempty_string(target["projectId"], "create_thread request.target.projectId")
    if target["environment"] != {"type": "local"}:
        raise ValueError("create_thread request는 saved project의 local 환경만 허용합니다")
    _require_unicode_integrity(request, "create_thread request")
    return request


def _create_thread_request_sha256(request: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(_validate_create_thread_request(request)).encode("utf-8"))


def _without_windows_extended_path_prefix(value: str | Path) -> str | None:
    path = str(value)
    if path[:8].upper() == "\\\\?\\UNC\\":
        remainder = path[8:]
        components = remainder.split("\\")
        if len(components) < 2 or not components[0] or not components[1]:
            return None
        return "\\\\" + remainder
    if path.startswith("\\\\?\\"):
        drive_path = path[4:]
        if re.match(r"^[A-Za-z]:[\\/]", drive_path):
            return drive_path
        return None
    return path


def _same_path(left: str | Path, right: str | Path) -> bool:
    normalized_left = _without_windows_extended_path_prefix(left)
    normalized_right = _without_windows_extended_path_prefix(right)
    if normalized_left is None or normalized_right is None:
        return False
    return os.path.normcase(os.path.abspath(normalized_left)) == os.path.normcase(
        os.path.abspath(normalized_right)
    )


def _validate_execution_policy_observation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("execution_policy observation은 JSON 객체여야 합니다")
    required = {
        "source", "state_db_path", "thread_id", "observed_at", "approval_policy",
        "sandbox_policy", "cwd", "project_binding", "thread_row_sha256",
    }
    _expect_keys(value, required, required, "execution_policy observation")
    if value["source"] != "codex_state_db_v1":
        raise ValueError("execution_policy observation source가 유효하지 않습니다")
    state_db_path = Path(_nonempty_string(
        value["state_db_path"], "execution_policy observation.state_db_path"
    ))
    if not state_db_path.is_absolute():
        raise ValueError("execution_policy observation.state_db_path는 절대 경로여야 합니다")
    _nonempty_string(value["thread_id"], "execution_policy observation.thread_id")
    parse_time(_nonempty_string(value["observed_at"], "execution_policy observation.observed_at"))
    if value["approval_policy"] != REQUIRED_APPROVAL_POLICY:
        raise ValueError("PERMISSION_POLICY_MISMATCH: created thread approval policy가 never가 아닙니다")
    if value["sandbox_policy"] != {"type": "disabled"}:
        raise ValueError(
            "PERMISSION_POLICY_MISMATCH: created thread sandbox가 danger-full-access가 아닙니다"
        )
    _nonempty_string(value["cwd"], "execution_policy observation.cwd")
    binding = value["project_binding"]
    binding_required = {
        "external_project_id", "internal_project_id", "project_name", "primary_root"
    }
    if not isinstance(binding, dict):
        raise ValueError("execution_policy observation.project_binding은 객체여야 합니다")
    _expect_keys(binding, binding_required, binding_required, "execution_policy project binding")
    for field in binding_required:
        _nonempty_string(binding[field], f"execution_policy project_binding.{field}")
    if not _same_path(value["cwd"], binding["primary_root"]):
        raise ValueError("PROJECT_BINDING_MISMATCH: created thread cwd가 프로젝트 기본 root와 다릅니다")
    if not SHA256_RE.fullmatch(value["thread_row_sha256"]):
        raise ValueError("execution_policy observation.thread_row_sha256이 유효하지 않습니다")
    row_projection = {
        "thread_id": value["thread_id"],
        "approval_policy": value["approval_policy"],
        "sandbox_policy": value["sandbox_policy"],
        "cwd": value["cwd"],
        "project_binding": binding,
    }
    if value["thread_row_sha256"] != sha256_bytes(
        canonical_json(row_projection).encode("utf-8")
    ):
        raise ValueError("execution_policy observation row digest가 실제 값과 다릅니다")
    return value


def _observe_created_thread_policy(
    state_db_path: str | Path, *, thread_id: str, external_project_id: str,
) -> dict[str, Any]:
    state_path = Path(state_db_path).resolve()
    if not state_path.is_file():
        raise ValueError(f"Codex state DB가 없습니다: {state_path}")
    uri = f"file:{state_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        thread = connection.execute(
            "SELECT id,cwd,sandbox_policy,approval_mode FROM threads WHERE id=?",
            (thread_id,),
        ).fetchone()
        if thread is None:
            raise ValueError("created thread가 Codex state DB에 아직 기록되지 않았습니다")
        project = connection.execute(
            """SELECT p.id internal_project_id,p.name,r.path primary_root
               FROM project_idempotency_keys k
               JOIN projects p ON p.id=k.project_id
               JOIN project_roots r ON r.project_id=p.id AND r.position=0
               WHERE k.key=?""",
            (external_project_id,),
        ).fetchone()
        if project is None:
            raise ValueError("PROJECT_BINDING_MISMATCH: saved project ID를 Codex state DB에서 찾지 못했습니다")
        try:
            sandbox_policy = json.loads(thread["sandbox_policy"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("created thread sandbox_policy가 유효한 JSON이 아닙니다") from error
        binding = {
            "external_project_id": external_project_id,
            "internal_project_id": project["internal_project_id"],
            "project_name": project["name"],
            "primary_root": project["primary_root"],
        }
        projection = {
            "thread_id": thread["id"],
            "approval_policy": thread["approval_mode"],
            "sandbox_policy": sandbox_policy,
            "cwd": thread["cwd"],
            "project_binding": binding,
        }
        observation = {
            "source": "codex_state_db_v1",
            "state_db_path": str(state_path),
            **projection,
            "observed_at": isoformat(),
            "thread_row_sha256": sha256_bytes(canonical_json(projection).encode("utf-8")),
        }
        return _validate_execution_policy_observation(observation)
    finally:
        connection.close()


def _saved_project_primary_root(
    state_db_path: str | Path, *, external_project_id: str,
) -> Path:
    state_path = Path(state_db_path).resolve()
    if not state_path.is_file():
        raise ValueError(f"Codex state DB가 없습니다: {state_path}")
    connection = sqlite3.connect(f"file:{state_path.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """SELECT r.path FROM project_idempotency_keys k
               JOIN project_roots r ON r.project_id=k.project_id AND r.position=0
               WHERE k.key=?""",
            (external_project_id,),
        ).fetchone()
        if row is None:
            raise ValueError("PROJECT_BINDING_MISMATCH: saved project 기본 root가 없습니다")
        root = Path(row[0]).resolve()
        if not root.is_dir():
            raise ValueError(f"PROJECT_BINDING_MISMATCH: saved project 기본 root가 없습니다: {root}")
        return root
    finally:
        connection.close()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def require_existing_db(path: str) -> Path:
    result = Path(path).resolve()
    if not result.is_file():
        raise ValueError(f"존재하는 DB 파일이 필요합니다: {result}")
    return result


@contextmanager
def open_readonly(path: str | Path):
    db_path = require_existing_db(str(path))
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def open_write(
    path: str | Path, *, allow_schema_upgrade: bool = False,
) -> sqlite3.Connection:
    db_path = require_existing_db(str(path))
    connection = sqlite3.connect(db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "flowmarshal_writer_contract_version", 0, lambda: WRITER_CONTRACT_VERSION,
        deterministic=True,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    if table_exists(connection, "implementation_writer_contract"):
        installed = connection.execute(
            "SELECT minimum_writer_version FROM implementation_writer_contract WHERE singleton=1"
        ).fetchone()
        if (installed is None or installed["minimum_writer_version"] != WRITER_CONTRACT_VERSION):
            if not allow_schema_upgrade:
                connection.close()
                raise RuntimeError(
                    f"writer contract version 불일치: installed="
                    f"{None if installed is None else installed['minimum_writer_version']}, "
                    f"current={WRITER_CONTRACT_VERSION}; migrate가 먼저 필요합니다"
                )
    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _require_current_schema(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "implementation_writer_contract"):
        raise RuntimeError(
            f"implementation schema v{IMPLEMENTATION_SCHEMA_VERSION} migration이 필요합니다"
        )
    row = connection.execute(
        "SELECT minimum_writer_version FROM implementation_writer_contract WHERE singleton=1"
    ).fetchone()
    if row is None or row["minimum_writer_version"] != WRITER_CONTRACT_VERSION:
        raise RuntimeError(
            f"writer contract version이 schema v{IMPLEMENTATION_SCHEMA_VERSION}와 일치하지 않습니다"
        )


def _install_writer_fence_triggers(connection: sqlite3.Connection) -> None:
    """등록되지 않은 구형 writer의 모든 원장 mutation을 DB 자체에서 차단한다."""
    for table in WRITER_FENCE_TABLES:
        if not table_exists(connection, table):
            continue
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"flowmarshal_writer_fence_{table}_{operation.lower()}"
            connection.execute(
                f'''CREATE TRIGGER IF NOT EXISTS "{trigger}"
                    BEFORE {operation} ON "{table}"
                    BEGIN
                        SELECT CASE
                            WHEN COALESCE(flowmarshal_writer_contract_version(), 0) <
                                 (SELECT minimum_writer_version
                                    FROM implementation_writer_contract WHERE singleton=1)
                            THEN RAISE(ABORT, 'FLOWMARSHAL_WRITER_CONTRACT_MISMATCH')
                        END;
                    END'''
            )


def insert_event(
    connection: sqlite3.Connection,
    event_type: str,
    *,
    run_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: Any = None,
) -> None:
    connection.execute(
        """INSERT INTO orchestration_events(
               run_id, occurred_at, event_type, entity_type, entity_id, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, isoformat(), event_type, entity_type, entity_id,
         canonical_json({} if payload is None else payload)),
    )


def _expect_keys(value: dict[str, Any], required: set[str], allowed: set[str], where: str) -> None:
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise ValueError(f"{where} 필수 필드 누락: {sorted(missing)}")
    if unknown:
        raise ValueError(f"{where} 알 수 없는 필드: {sorted(unknown)}")


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}는 비어 있지 않은 문자열이어야 합니다")
    return value


def _string_list(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{where}는 문자열 배열이어야 합니다")
    for item in value:
        _nonempty_string(item, where)
    return value


def _validate_effort_ladder(
    model: str, initial_effort: str, value: Any, where: str,
) -> list[str]:
    ladder = _string_list(value, where, nonempty=True)
    if initial_effort not in EFFORTS:
        raise ValueError(f"{where}의 initial effort가 유효하지 않습니다")
    if ladder[0] != initial_effort:
        raise ValueError(f"{where}는 initial effort로 시작해야 합니다")
    if len(set(ladder)) != len(ladder):
        raise ValueError(f"{where}에는 중복 effort를 둘 수 없습니다")
    indexes = [EFFORTS.index(effort) if effort in EFFORTS else -1 for effort in ladder]
    if -1 in indexes or indexes != sorted(indexes):
        raise ValueError(f"{where}는 지원되는 effort의 오름차순이어야 합니다")
    if "luna" in model.lower() and any(index < EFFORTS.index("high") for index in indexes):
        raise ValueError(f"{where}: Luna에는 high 이상의 effort만 허용됩니다")
    return ladder


def _latest_reasoning_policy(
    connection: sqlite3.Connection, workflow: sqlite3.Row, policy_scope: str, policy_key: str,
) -> sqlite3.Row | None:
    if not table_exists(connection, "implementation_reasoning_policies"):
        return None
    return connection.execute(
        """SELECT * FROM implementation_reasoning_policies
           WHERE workflow_id=? AND workflow_revision=? AND policy_scope=? AND policy_key=?
           ORDER BY policy_revision DESC LIMIT 1""",
        (workflow["workflow_id"], workflow["workflow_revision"], policy_scope, policy_key),
    ).fetchone()


def _insert_reasoning_policy(
    connection: sqlite3.Connection,
    workflow: sqlite3.Row | dict[str, Any],
    *,
    policy_scope: str,
    policy_key: str,
    model: str,
    effort_ladder: list[str],
    escalation_trigger: str,
    decision_reason: str,
    created_at: str,
) -> dict[str, Any]:
    if policy_scope not in {"task", "architecture_recovery"}:
        raise ValueError("policy_scope가 유효하지 않습니다")
    _nonempty_string(policy_key, "policy_key")
    _nonempty_string(model, "model")
    _nonempty_string(decision_reason, "decision_reason")
    if escalation_trigger not in {
        "independent_review_failure", "same_failure_with_new_evidence",
    }:
        raise ValueError("escalation_trigger가 유효하지 않습니다")
    if not isinstance(effort_ladder, list) or not effort_ladder:
        raise ValueError("effort_ladder는 비어 있지 않은 배열이어야 합니다")
    ladder = _validate_effort_ladder(model, effort_ladder[0], effort_ladder, "effort_ladder")
    current = connection.execute(
        """SELECT policy_revision,model,initial_effort,effort_ladder_json,escalation_trigger,
                  decision_reason
           FROM implementation_reasoning_policies
           WHERE workflow_id=? AND workflow_revision=? AND policy_scope=? AND policy_key=?
           ORDER BY policy_revision DESC LIMIT 1""",
        (workflow["workflow_id"], workflow["workflow_revision"], policy_scope, policy_key),
    ).fetchone()
    normalized = {
        "model": model,
        "initial_effort": ladder[0],
        "effort_ladder_json": canonical_json(ladder),
        "escalation_trigger": escalation_trigger,
        "decision_reason": decision_reason,
    }
    if current is not None and all(current[key] == value for key, value in normalized.items()):
        return {"inserted": False, "policy_revision": current["policy_revision"], **normalized}
    revision = 1 if current is None else current["policy_revision"] + 1
    connection.execute(
        """INSERT INTO implementation_reasoning_policies(
               workflow_id,workflow_revision,policy_scope,policy_key,policy_revision,model,
               initial_effort,effort_ladder_json,escalation_trigger,decision_reason,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (workflow["workflow_id"], workflow["workflow_revision"], policy_scope, policy_key,
         revision, model, ladder[0], normalized["effort_ladder_json"], escalation_trigger,
         decision_reason, created_at),
    )
    return {"inserted": True, "policy_revision": revision, **normalized}


def _task_model_binding(
    connection: sqlite3.Connection, workflow: sqlite3.Row, task: sqlite3.Row,
) -> dict[str, Any]:
    policy = _latest_reasoning_policy(connection, workflow, "task", task["task_id"])
    if policy is None:
        return {
            "model": task["model"], "reasoning_effort": task["reasoning_effort"],
            "escalation_step": 0, "policy_scope": None, "policy_key": None,
            "policy_revision": None, "binding_reason": task["model_selection_reason"],
        }
    ladder = json.loads(policy["effort_ladder_json"])
    failure_rows = connection.execute(
        """SELECT e.evidence_json FROM implementation_task_attempts a
           JOIN implementation_evidence e ON e.evidence_sha256=a.evidence_sha256
           WHERE a.workflow_id=? AND a.workflow_revision=? AND a.task_id=? AND a.task_revision=?
             AND a.status='FAILED' AND e.outcome='FAIL'""",
        (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
    ).fetchall()
    escalation_failure_classes = {"implementation", "task_contract", "requirement_change"}
    reviewed_failures = sum(
        1 for row in failure_rows
        if json.loads(row["evidence_json"]).get("finding", {}).get("failure_class")
        in escalation_failure_classes
    )
    step = min(reviewed_failures, len(ladder) - 1)
    return {
        "model": policy["model"], "reasoning_effort": ladder[step],
        "escalation_step": step, "policy_scope": policy["policy_scope"],
        "policy_key": policy["policy_key"], "policy_revision": policy["policy_revision"],
        "binding_reason": (
            f"{policy['decision_reason']} (step {step + 1}/{len(ladder)}; "
            f"reviewed failures={reviewed_failures})"
        ),
    }


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ValueError("manifest 최상위 값은 객체여야 합니다")
    required = {
        "workflow_id", "revision", "title", "approval", "baseline_commit", "source_root",
        "artifact_root", "automation_id", "parent_thread_id", "policies", "tasks",
    }
    if not isinstance(manifest.get("revision"), int) or manifest["revision"] < 1:
        raise ValueError("manifest revision은 양의 정수여야 합니다")
    revision_keys = {
        "parent_revision", "parent_manifest", "lineage", "carry_forward",
        "registration_status", "registration_guard",
    }
    expected = required | (revision_keys if manifest["revision"] > 1 else set())
    _expect_keys(manifest, expected, expected, "manifest")
    if manifest["workflow_id"] != WORKFLOW_ID:
        raise ValueError(f"workflow_id는 {WORKFLOW_ID!r}여야 합니다")
    for field in (
        "title", "baseline_commit", "source_root", "artifact_root", "automation_id",
        "parent_thread_id",
    ):
        _nonempty_string(manifest[field], field)
    if not Path(manifest["source_root"]).is_absolute():
        raise ValueError("source_root는 절대 경로여야 합니다")
    if not Path(manifest["artifact_root"]).is_absolute():
        raise ValueError("artifact_root는 절대 경로여야 합니다")
    approval = manifest["approval"]
    if not isinstance(approval, dict):
        raise ValueError("approval은 객체여야 합니다")
    approval_keys = {"user_request", "plan_document", "plan_sha256"}
    _expect_keys(approval, approval_keys, approval_keys, "approval")
    for field in approval_keys:
        _nonempty_string(approval[field], f"approval.{field}")
    if not SHA256_RE.fullmatch(approval["plan_sha256"]):
        raise ValueError("approval.plan_sha256은 소문자 SHA-256이어야 합니다")
    plan_path = Path(approval["plan_document"])
    if not plan_path.is_file() or sha256_file(plan_path) != approval["plan_sha256"]:
        raise ValueError("승인 계획 문서가 없거나 SHA-256이 일치하지 않습니다")
    policies = manifest["policies"]
    if not isinstance(policies, dict):
        raise ValueError("policies는 객체여야 합니다")
    if (policies.get("fast_mode") not in (None, False)
            or policies.get("fast_requested") not in (None, False)):
        raise ValueError("Fast mode는 승인되지 않았습니다")
    if policies.get("usage_missing_blocks_execution") not in (None, False):
        raise ValueError("누락 usage를 전역 실행 차단 조건으로 사용할 수 없습니다")
    if policies.get("performance_comparison_blocks_release") not in (None, False):
        raise ValueError("비교 성능을 필수 릴리스 차단 조건으로 사용할 수 없습니다")
    required_git_branch = policies.get("required_git_branch")
    if required_git_branch is not None:
        _nonempty_string(required_git_branch, "policies.required_git_branch")
    main_checkout_only = policies.get("main_checkout_only")
    if main_checkout_only is not None and not isinstance(main_checkout_only, bool):
        raise ValueError("policies.main_checkout_only는 boolean이어야 합니다")
    if main_checkout_only is True and required_git_branch != "main":
        raise ValueError("main_checkout_only에는 required_git_branch='main'이 필요합니다")
    architecture_ladder = policies.get("architecture_recovery_effort_ladder")
    if architecture_ladder is not None:
        _validate_effort_ladder(
            _nonempty_string(policies.get("architecture_recovery_model"),
                             "policies.architecture_recovery_model"),
            _nonempty_string(policies.get("architecture_recovery_effort"),
                             "policies.architecture_recovery_effort"),
            architecture_ladder,
            "policies.architecture_recovery_effort_ladder",
        )
        if policies.get("architecture_recovery_escalation_trigger") != "same_failure_with_new_evidence":
            raise ValueError(
                "architecture recovery ladder에는 same_failure_with_new_evidence trigger가 필요합니다"
            )
    if not isinstance(manifest["tasks"], list) or not manifest["tasks"]:
        raise ValueError("tasks는 비어 있지 않은 배열이어야 합니다")

    task_required = {
        "task_id", "order_index", "title", "objective", "instructions", "inputs", "outputs",
        "prohibited_effects", "lane", "project_id", "model", "reasoning_effort",
        "model_selection_reason", "depends_on", "checks",
    }
    task_allowed = task_required | {
        "evidence_contract", "input_task_refs", "reasoning_effort_ladder",
        "reasoning_escalation_trigger",
    }
    check_required = {"check_id", "criterion", "method", "required"}
    ids: set[str] = set()
    orders: set[int] = set()
    for index, task in enumerate(manifest["tasks"]):
        where = f"tasks[{index}]"
        if not isinstance(task, dict):
            raise ValueError(f"{where}는 객체여야 합니다")
        _expect_keys(task, task_required, task_allowed, where)
        task_id = _nonempty_string(task["task_id"], f"{where}.task_id")
        if task_id in ids:
            raise ValueError(f"중복 task_id: {task_id}")
        ids.add(task_id)
        if not isinstance(task["order_index"], int) or task["order_index"] < 0:
            raise ValueError(f"{where}.order_index는 0 이상의 정수여야 합니다")
        if task["order_index"] in orders:
            raise ValueError(f"중복 order_index: {task['order_index']}")
        orders.add(task["order_index"])
        for field in ("title", "objective", "model_selection_reason"):
            _nonempty_string(task[field], f"{where}.{field}")
        for field in ("instructions", "inputs", "outputs", "prohibited_effects", "depends_on"):
            _string_list(task[field], f"{where}.{field}", nonempty=field == "instructions")
        if task["lane"] not in {"bootstrap", "development", "test", "recovery"}:
            raise ValueError(f"{where}.lane이 유효하지 않습니다")
        optional_strings = ("project_id", "model", "reasoning_effort")
        for field in optional_strings:
            if task[field] is not None:
                _nonempty_string(task[field], f"{where}.{field}")
        evidence_contract = task.get("evidence_contract")
        if evidence_contract is not None and evidence_contract not in {"bound", "evaluation"}:
            raise ValueError(f"{where}.evidence_contract는 bound 또는 evaluation이어야 합니다")
        input_task_refs = task.get("input_task_refs")
        if input_task_refs is not None:
            _string_list(input_task_refs, f"{where}.input_task_refs")
            if len(input_task_refs) != len(set(input_task_refs)):
                raise ValueError(f"{where}.input_task_refs가 중복됐습니다")
        if task["lane"] == "bootstrap":
            if task_id != "FM-00":
                raise ValueError("bootstrap lane은 FM-00에만 허용됩니다")
        elif any(task[field] is None for field in optional_strings):
            raise ValueError(f"{where}: 비-bootstrap task는 project/model/effort가 필요합니다")
        if (task["model"] is None) != (task["reasoning_effort"] is None):
            raise ValueError(f"{where}: model과 reasoning_effort는 함께 지정해야 합니다")
        if task["reasoning_effort"] is not None and task["reasoning_effort"] not in EFFORTS:
            raise ValueError(f"{where}.reasoning_effort가 유효하지 않습니다")
        if task["model"] and "luna" in task["model"].lower():
            if EFFORTS.index(task["reasoning_effort"]) < EFFORTS.index("high"):
                raise ValueError(f"{where}: Luna에는 high 이상의 reasoning_effort가 필요합니다")
        reasoning_ladder = task.get("reasoning_effort_ladder")
        if reasoning_ladder is not None:
            if task["model"] is None or task["reasoning_effort"] is None:
                raise ValueError(f"{where}: reasoning ladder에는 model/initial effort가 필요합니다")
            _validate_effort_ladder(
                task["model"], task["reasoning_effort"], reasoning_ladder,
                f"{where}.reasoning_effort_ladder",
            )
            if task.get("reasoning_escalation_trigger") != "independent_review_failure":
                raise ValueError(
                    f"{where}: task reasoning ladder에는 independent_review_failure trigger가 필요합니다"
                )
        elif task.get("reasoning_escalation_trigger") is not None:
            raise ValueError(f"{where}: reasoning ladder 없이 escalation trigger를 둘 수 없습니다")
        if not isinstance(task["checks"], list) or not task["checks"]:
            raise ValueError(f"{where}.checks는 비어 있지 않아야 합니다")
        check_ids: set[str] = set()
        for check_index, check in enumerate(task["checks"]):
            check_where = f"{where}.checks[{check_index}]"
            if not isinstance(check, dict):
                raise ValueError(f"{check_where}는 객체여야 합니다")
            _expect_keys(check, check_required, check_required, check_where)
            check_id = _nonempty_string(check["check_id"], f"{check_where}.check_id")
            if check_id in check_ids:
                raise ValueError(f"{where} 중복 check_id: {check_id}")
            check_ids.add(check_id)
            _nonempty_string(check["criterion"], f"{check_where}.criterion")
            _nonempty_string(check["method"], f"{check_where}.method")
            if check["required"] is not True:
                raise ValueError(f"{check_where}.required는 true여야 합니다")
    if "FM-00" not in ids:
        raise ValueError("FM-00 bootstrap task가 필요합니다")
    task_map = {task["task_id"]: task for task in manifest["tasks"]}
    if task_map["FM-00"]["depends_on"]:
        raise ValueError("FM-00은 선행 task를 가질 수 없습니다")
    for task in manifest["tasks"]:
        for dependency in task["depends_on"]:
            if dependency not in task_map:
                raise ValueError(f"{task['task_id']}의 알 수 없는 dependency: {dependency}")
            if dependency == task["task_id"]:
                raise ValueError(f"{task['task_id']}의 자기 dependency")
        if task["task_id"] != "FM-00" and "FM-00" not in _transitive_dependencies(
            task["task_id"], task_map
        ):
            raise ValueError(f"{task['task_id']}는 FM-00 성공에 직·간접 의존해야 합니다")
    if manifest["revision"] >= 5:
        ordered_task_ids = sorted(task_map, key=len, reverse=True)
        task_ref_pattern = re.compile(
            r"(?<![A-Za-z0-9-])FM-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9-])"
        )
        for task in manifest["tasks"]:
            task_id = task["task_id"]
            if task_id == "FM-00":
                continue
            if "input_task_refs" not in task:
                raise ValueError(f"{task_id}: revision 5부터 input_task_refs가 필요합니다")
            declared_refs = set(task["input_task_refs"])
            unknown_refs = declared_refs - set(task_map)
            if unknown_refs:
                raise ValueError(
                    f"{task_id}: 알 수 없는 input_task_refs: {sorted(unknown_refs)}"
                )
            detected_refs: set[str] = set()
            for input_value in task["inputs"]:
                unknown_input_refs = set(task_ref_pattern.findall(input_value)) - set(task_map)
                if unknown_input_refs:
                    raise ValueError(
                        f"{task_id}: inputs에 알 수 없는 Task 참조가 있습니다: "
                        f"{sorted(unknown_input_refs)}"
                    )
                for candidate_id in ordered_task_ids:
                    pattern = rf"(?<![A-Za-z0-9-]){re.escape(candidate_id)}(?![A-Za-z0-9-])"
                    if re.search(pattern, input_value):
                        detected_refs.add(candidate_id)
            if declared_refs != detected_refs:
                raise ValueError(
                    f"{task_id}: input_task_refs가 inputs의 Task 참조와 다릅니다 "
                    f"(declared={sorted(declared_refs)}, detected={sorted(detected_refs)})"
                )
            ancestors = _transitive_dependencies(task_id, task_map)
            off_ancestry = declared_refs - ancestors
            if off_ancestry:
                raise ValueError(
                    f"{task_id}: Task 산출물 input은 dependency ancestry에 있어야 합니다: "
                    f"{sorted(off_ancestry)}"
                )
            if task.get("evidence_contract") not in {"bound", "evaluation"}:
                raise ValueError(
                    f"{task_id}: revision 5부터 evidence_contract가 필요합니다"
                )
    _assert_acyclic(task_map)
    if manifest["revision"] > 1:
        if manifest["registration_status"] not in {
            "DRAFT_NOT_FOR_REGISTRATION", "READY_FOR_REGISTRATION",
        }:
            raise ValueError("registration_status가 유효하지 않습니다")
        guard = manifest["registration_guard"]
        guard_keys = {
            "database_registration_allowed", "activation_allowed",
            "automation_change_allowed", "reason",
        }
        if not isinstance(guard, dict):
            raise ValueError("registration_guard는 객체여야 합니다")
        _expect_keys(guard, guard_keys, guard_keys, "registration_guard")
        for flag in (
            "database_registration_allowed", "activation_allowed",
            "automation_change_allowed",
        ):
            if not isinstance(guard[flag], bool):
                raise ValueError(f"registration_guard.{flag}는 boolean이어야 합니다")
        _nonempty_string(guard["reason"], "registration_guard.reason")
        if manifest["parent_revision"] != manifest["revision"] - 1:
            raise ValueError("parent_revision은 바로 이전 revision이어야 합니다")
        parent_manifest = manifest["parent_manifest"]
        parent_keys = {"revision", "manifest_path", "manifest_sha256"}
        if not isinstance(parent_manifest, dict):
            raise ValueError("parent_manifest는 객체여야 합니다")
        _expect_keys(parent_manifest, parent_keys, parent_keys, "parent_manifest")
        if parent_manifest["revision"] != manifest["parent_revision"]:
            raise ValueError("parent_manifest revision binding 불일치")
        parent_path = Path(_nonempty_string(
            parent_manifest["manifest_path"], "parent_manifest.manifest_path"
        ))
        parent_sha = _nonempty_string(
            parent_manifest["manifest_sha256"], "parent_manifest.manifest_sha256"
        )
        if not SHA256_RE.fullmatch(parent_sha):
            raise ValueError("parent_manifest.manifest_sha256이 유효하지 않습니다")
        if not parent_path.is_file() or sha256_file(parent_path) != parent_sha:
            raise ValueError("parent manifest 파일이 없거나 SHA-256이 일치하지 않습니다")
        lineage = manifest["lineage"]
        if not isinstance(lineage, list) or not lineage:
            raise ValueError("revision lineage는 비어 있지 않은 배열이어야 합니다")
        lineage_keys = {"from_task_id", "to_task_id", "relation", "decision"}
        lineage_targets: set[str] = set()
        lineage_pairs: set[tuple[str, str]] = set()
        for index, item in enumerate(lineage):
            where = f"lineage[{index}]"
            if not isinstance(item, dict):
                raise ValueError(f"{where}는 객체여야 합니다")
            _expect_keys(item, lineage_keys, lineage_keys, where)
            source_id = _nonempty_string(item["from_task_id"], f"{where}.from_task_id")
            target_id = _nonempty_string(item["to_task_id"], f"{where}.to_task_id")
            _nonempty_string(item["decision"], f"{where}.decision")
            if item["relation"] not in {"unchanged", "split", "replacement"}:
                raise ValueError(f"{where}.relation이 유효하지 않습니다")
            if item["relation"] == "unchanged" and source_id != target_id:
                raise ValueError(f"{where}: unchanged lineage는 같은 task_id여야 합니다")
            if target_id not in task_map:
                raise ValueError(f"{where}: target task가 manifest에 없습니다")
            if target_id in lineage_targets or (source_id, target_id) in lineage_pairs:
                raise ValueError(f"{where}: 중복 lineage target/pair")
            lineage_targets.add(target_id)
            lineage_pairs.add((source_id, target_id))
        if lineage_targets != set(task_map):
            raise ValueError("모든 revision task는 정확히 하나의 lineage target이어야 합니다")
        carry = manifest["carry_forward"]
        if not isinstance(carry, list):
            raise ValueError("carry_forward는 배열이어야 합니다")
        carry_keys = {"from_task_id", "to_task_id", "decision"}
        carry_targets: set[str] = set()
        for index, item in enumerate(carry):
            where = f"carry_forward[{index}]"
            if not isinstance(item, dict):
                raise ValueError(f"{where}는 객체여야 합니다")
            _expect_keys(item, carry_keys, carry_keys, where)
            source_id = _nonempty_string(item["from_task_id"], f"{where}.from_task_id")
            target_id = _nonempty_string(item["to_task_id"], f"{where}.to_task_id")
            _nonempty_string(item["decision"], f"{where}.decision")
            if target_id in carry_targets:
                raise ValueError(f"{where}: 중복 carry target")
            if (source_id, target_id) not in lineage_pairs:
                raise ValueError(f"{where}: lineage가 없는 carry-forward")
            relation = next(
                row["relation"] for row in lineage
                if row["from_task_id"] == source_id and row["to_task_id"] == target_id
            )
            if relation != "unchanged" or source_id != target_id:
                raise ValueError(f"{where}: unchanged task만 carry-forward할 수 있습니다")
            carry_targets.add(target_id)
    return manifest


def _transitive_dependencies(task_id: str, tasks: dict[str, dict[str, Any]]) -> set[str]:
    found: set[str] = set()
    pending = list(tasks[task_id]["depends_on"])
    while pending:
        current = pending.pop()
        if current in found or current not in tasks:
            continue
        found.add(current)
        pending.extend(tasks[current]["depends_on"])
    return found


def _assert_acyclic(tasks: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError(f"task DAG cycle 발견: {task_id}")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def _materialize_revision_manifest(raw_manifest: Any) -> Any:
    """간결한 revision 후보를 기존 full manifest 계약으로 결정적으로 확장한다."""
    if not isinstance(raw_manifest, dict) or raw_manifest.get("revision", 1) <= 1:
        return raw_manifest
    if "approval" in raw_manifest:
        return raw_manifest
    parent_ref = raw_manifest.get("parent_manifest")
    if not isinstance(parent_ref, dict):
        raise ValueError("revision 후보에는 parent_manifest binding이 필요합니다")
    parent_path = Path(str(parent_ref.get("manifest_path", ""))).resolve()
    parent_sha = parent_ref.get("manifest_sha256")
    if not parent_path.is_file() or not isinstance(parent_sha, str) or sha256_file(parent_path) != parent_sha:
        raise ValueError("parent manifest 파일이 없거나 SHA-256이 일치하지 않습니다")
    parent = validate_manifest(json.loads(parent_path.read_text(encoding="utf-8")))
    carry = raw_manifest.get("carry_forward")
    new_tasks = raw_manifest.get("tasks")
    if not isinstance(carry, list) or not isinstance(new_tasks, list):
        raise ValueError("revision 후보의 tasks/carry_forward 형식이 유효하지 않습니다")
    parent_tasks = {task["task_id"]: task for task in parent["tasks"]}
    carried_tasks: list[dict[str, Any]] = []
    for item in carry:
        if not isinstance(item, dict) or item.get("from_task_id") not in parent_tasks:
            raise ValueError("carry_forward source task가 parent manifest에 없습니다")
        if item.get("from_task_id") != item.get("to_task_id"):
            raise ValueError("간결 manifest의 carry-forward는 같은 task_id만 지원합니다")
        carried_tasks.append(parent_tasks[item["from_task_id"]])
    baseline = raw_manifest.get("baseline") if isinstance(raw_manifest.get("baseline"), dict) else {}
    normalized_new_tasks: list[dict[str, Any]] = []
    for task in new_tasks:
        if not isinstance(task, dict):
            raise ValueError("revision 후보 task는 객체여야 합니다")
        normalized = dict(task)
        normalized.setdefault("prohibited_effects", [
            "예약 자동화를 활성화하지 않는다.",
            "범위 밖 source·원장 history·evidence를 수정하거나 삭제하지 않는다.",
        ])
        normalized_new_tasks.append(normalized)
    return {
        "workflow_id": raw_manifest.get("workflow_id"),
        "revision": raw_manifest.get("revision"),
        "title": raw_manifest.get("title"),
        "approval": parent["approval"],
        "baseline_commit": baseline.get("observed_source_commit", parent["baseline_commit"]),
        "source_root": baseline.get("source_root", parent["source_root"]),
        "artifact_root": parent["artifact_root"],
        "automation_id": parent["automation_id"],
        "parent_thread_id": parent["parent_thread_id"],
        "policies": parent["policies"],
        "tasks": [*carried_tasks, *normalized_new_tasks],
        "parent_revision": raw_manifest.get("parent_revision"),
        "parent_manifest": parent_ref,
        "lineage": raw_manifest.get("lineage"),
        "carry_forward": carry,
        "registration_status": raw_manifest.get("status"),
        "registration_guard": raw_manifest.get("registration_guard"),
    }


def load_manifest(path: str) -> tuple[dict[str, Any], str, str]:
    manifest_path = Path(path).resolve()
    if not manifest_path.is_file():
        raise ValueError(f"manifest가 없습니다: {manifest_path}")
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = validate_manifest(_materialize_revision_manifest(json.loads(raw)))
    canonical = canonical_json(manifest)
    return manifest, canonical, sha256_bytes(canonical.encode("utf-8"))


def _ensure_v1(connection: sqlite3.Connection) -> None:
    required = {
        "metadata", "orchestration_state", "orchestration_runs", "dispatches",
        "orchestration_events", "source_refs", "orchestration_locks",
    }
    present = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = required - present
    if missing:
        raise RuntimeError(f"기존 v1 원장 테이블 누락: {sorted(missing)}")


def _active_migration_blocker(connection: sqlite3.Connection) -> str | None:
    now = isoformat()
    lock = connection.execute(
        "SELECT owner_run_id, expires_at FROM orchestration_locks WHERE lock_name=?", (LOCK_NAME,)
    ).fetchone()
    if lock is not None and lock["expires_at"] > now:
        return f"active lease: {lock['owner_run_id']} until {lock['expires_at']}"
    state = connection.execute(
        "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
    ).fetchone()
    if state and state["active_dispatch_id"]:
        return f"active dispatch: {state['active_dispatch_id']}"
    unresolved = connection.execute(
        "SELECT dispatch_id FROM dispatches WHERE status NOT IN (%s) LIMIT 1"
        % ",".join("?" for _ in TERMINAL_DISPATCH_STATUSES),
        tuple(sorted(TERMINAL_DISPATCH_STATUSES)),
    ).fetchone()
    if unresolved:
        return f"unresolved dispatch: {unresolved['dispatch_id']}"
    claims=_unconsumed_direct_claims(connection)
    if claims:
        return "unconsumed direct preparation: event " + str(claims[0]["prepared_event_id"])
    return None


def _backup_database(db_path: Path, backup_dir: Path, manifest_sha: str) -> tuple[Path, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"{db_path.stem}-pre-implementation-v1-{stamp}-{manifest_sha[:12]}.sqlite3"
    temp_path = backup_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    destination = sqlite3.connect(temp_path)
    try:
        with open_readonly(db_path) as source:
            source.backup(destination)
        destination.commit()
        integrity = [row[0] for row in destination.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise RuntimeError(f"backup integrity_check 실패: {integrity}")
    finally:
        destination.close()
    backup_sha = sha256_file(temp_path)
    os.replace(temp_path, final_path)
    return final_path, backup_sha


def _command_migrate_legacy(args: argparse.Namespace) -> dict[str, Any]:
    db_path = require_existing_db(args.db)
    manifest, manifest_json, manifest_sha = load_manifest(args.manifest)
    with open_readonly(db_path) as probe:
        _ensure_v1(probe)
        already_migrated = table_exists(probe, "implementation_schema_migrations") and probe.execute(
            "SELECT 1 FROM implementation_schema_migrations WHERE version=?",
            (IMPLEMENTATION_SCHEMA_VERSION,),
        ).fetchone() is not None
        if already_migrated:
            existing = probe.execute(
                "SELECT spec_sha256 FROM implementation_workflows WHERE workflow_id=? AND workflow_revision=?",
                (manifest["workflow_id"], manifest["revision"]),
            ).fetchone()
            if existing and existing["spec_sha256"] == manifest_sha:
                return {"ok": True, "migrated": False, "idempotent": True,
                        "workflow_id": manifest["workflow_id"], "spec_sha256": manifest_sha}
            if existing:
                raise RuntimeError("같은 workflow revision의 다른 manifest는 덮어쓸 수 없습니다")
            raise RuntimeError("schema는 이미 이행되었지만 요청한 workflow revision이 없습니다")
        blocker = _active_migration_blocker(probe)
        if blocker:
            raise RuntimeError(f"안전한 migration을 위해 활성 작업/lease가 없어야 합니다: {blocker}")

    backup_path, backup_sha = _backup_database(db_path, Path(args.backup_dir).resolve(), manifest_sha)
    now = isoformat()
    connection = open_write(db_path, allow_schema_upgrade=True)
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + REASONING_POLICY_SCHEMA_SQL)
        # 백업 사이 다른 실행이 활성화되었으면 DDL도 같은 트랜잭션으로 되돌린다.
        blocker = _active_migration_blocker(connection)
        if blocker:
            raise RuntimeError(f"migration 쓰기 직전 활성 작업/lease 발견: {blocker}")
        connection.execute(
            "INSERT INTO implementation_schema_migrations(version,applied_at,backup_path,backup_sha256) VALUES(?,?,?,?)",
            (IMPLEMENTATION_SCHEMA_VERSION, now, str(backup_path), backup_sha),
        )
        connection.execute(
            """INSERT INTO implementation_workflows(
                workflow_id,workflow_revision,title,spec_json,spec_sha256,approval_plan_path,
                approval_plan_sha256,baseline_commit,source_root,artifact_root,automation_id,
                parent_thread_id,state,registered_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE',?)""",
            (
                manifest["workflow_id"], manifest["revision"], manifest["title"], manifest_json,
                manifest_sha, manifest["approval"]["plan_document"],
                manifest["approval"]["plan_sha256"], manifest["baseline_commit"],
                manifest["source_root"], manifest["artifact_root"], manifest["automation_id"],
                manifest["parent_thread_id"], now,
            ),
        )
        for task in manifest["tasks"]:
            task_json = canonical_json(task)
            task_sha = sha256_bytes(task_json.encode("utf-8"))
            connection.execute(
                """INSERT INTO implementation_tasks(
                    workflow_id,workflow_revision,task_id,task_revision,order_index,title,lane,
                    project_id,model,reasoning_effort,model_selection_reason,spec_json,spec_sha256,
                    status,created_at,updated_at
                ) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,'PENDING',?,?)""",
                (
                    manifest["workflow_id"], manifest["revision"], task["task_id"],
                    task["order_index"], task["title"], task["lane"], task["project_id"],
                    task["model"], task["reasoning_effort"], task["model_selection_reason"],
                    task_json, task_sha, now, now,
                ),
            )
            for check in task["checks"]:
                connection.execute(
                    """INSERT INTO implementation_task_checks(
                        workflow_id,workflow_revision,task_id,task_revision,check_id,
                        criterion,method,required,created_at
                    ) VALUES(?,?,?,1,?,?,?,?,?)""",
                    (manifest["workflow_id"], manifest["revision"], task["task_id"],
                     check["check_id"], check["criterion"], check["method"], 1, now),
                )
        for task in manifest["tasks"]:
            for dependency in task["depends_on"]:
                connection.execute(
                    """INSERT INTO implementation_task_dependencies(
                        workflow_id,workflow_revision,task_id,task_revision,
                        depends_on_task_id,depends_on_task_revision,created_at
                    ) VALUES(?,?,?,1,?,1,?)""",
                    (manifest["workflow_id"], manifest["revision"], task["task_id"], dependency, now),
                )
        workflow_binding = {
            "workflow_id": manifest["workflow_id"],
            "workflow_revision": manifest["revision"],
        }
        architecture_ladder = manifest["policies"].get("architecture_recovery_effort_ladder")
        if architecture_ladder is not None:
            _insert_reasoning_policy(
                connection, workflow_binding,
                policy_scope="architecture_recovery", policy_key="default",
                model=manifest["policies"]["architecture_recovery_model"],
                effort_ladder=architecture_ladder,
                escalation_trigger=manifest["policies"]["architecture_recovery_escalation_trigger"],
                decision_reason="registered workflow architecture recovery policy",
                created_at=now,
            )
        for task in manifest["tasks"]:
            if task.get("reasoning_effort_ladder") is None:
                continue
            _insert_reasoning_policy(
                connection, workflow_binding,
                policy_scope="task", policy_key=task["task_id"], model=task["model"],
                effort_ladder=task["reasoning_effort_ladder"],
                escalation_trigger=task["reasoning_escalation_trigger"],
                decision_reason=f"registered workflow task policy: {task['task_id']}",
                created_at=now,
            )
        state = connection.execute(
            "SELECT lifecycle_status,current_phase,blocker_code,blocker_fingerprint FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        if state and state["lifecycle_status"] == "WAIT_EXTERNAL" and state["current_phase"] == "WAIT_EXTERNAL_USAGE":
            previous = dict(state)
            connection.execute(
                """UPDATE orchestration_state SET lifecycle_status='ACTIVE',current_phase='IMPLEMENTATION',
                   current_lane='bootstrap',blocker_code=NULL,blocker_fingerprint=NULL,
                   updated_at=?,version=version+1 WHERE singleton=1""",
                (now,),
            )
            insert_event(
                connection, "policy_superseded", entity_type="orchestration_state", entity_id="1",
                payload={"previous": previous, "approved_policy": "usage_unknown_is_non_blocking",
                         "usage_receipts_modified": False, "manifest_sha256": manifest_sha},
            )
        legacy_health = connection.execute(
            "SELECT value_json FROM metadata WHERE key='scheduler_health'"
        ).fetchone()
        if legacy_health is not None:
            connection.execute(
                "UPDATE metadata SET value_json=?,updated_at=? WHERE key='scheduler_health'",
                (canonical_json({"status": "CONFIGURED_PENDING_SCHEDULED_OBSERVATION",
                                 "configured_interval_minutes": manifest["policies"].get("interval_minutes"),
                                 "legacy_snapshot_preserved_in_event": True}), now),
            )
            insert_event(
                connection, "scheduler_health.legacy_snapshot_superseded",
                entity_type="metadata", entity_id="scheduler_health",
                payload={"legacy_value_json": legacy_health["value_json"],
                         "new_status": "CONFIGURED_PENDING_SCHEDULED_OBSERVATION"},
            )
        insert_event(
            connection, "implementation_workflow.registered", entity_type="implementation_workflow",
            entity_id=manifest["workflow_id"],
            payload={"revision": manifest["revision"], "spec_sha256": manifest_sha,
                     "backup_path": str(backup_path), "backup_sha256": backup_sha},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "migrated": True, "workflow_id": manifest["workflow_id"],
            "revision": manifest["revision"], "spec_sha256": manifest_sha,
            "backup_path": str(backup_path), "backup_sha256": backup_sha}


def _insert_workflow_manifest(
    connection: sqlite3.Connection, manifest: dict[str, Any], manifest_json: str,
    manifest_sha: str, *, state: str, registered_at: str,
) -> None:
    connection.execute(
        """INSERT INTO implementation_workflows(
            workflow_id,workflow_revision,title,spec_json,spec_sha256,approval_plan_path,
            approval_plan_sha256,baseline_commit,source_root,artifact_root,automation_id,
            parent_thread_id,state,registered_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            manifest["workflow_id"], manifest["revision"], manifest["title"], manifest_json,
            manifest_sha, manifest["approval"]["plan_document"],
            manifest["approval"]["plan_sha256"], manifest["baseline_commit"],
            manifest["source_root"], manifest["artifact_root"], manifest["automation_id"],
            manifest["parent_thread_id"], state, registered_at,
        ),
    )
    for task in manifest["tasks"]:
        task_json = canonical_json(task)
        task_sha = sha256_bytes(task_json.encode("utf-8"))
        connection.execute(
            """INSERT INTO implementation_tasks(
                workflow_id,workflow_revision,task_id,task_revision,order_index,title,lane,
                project_id,model,reasoning_effort,model_selection_reason,spec_json,spec_sha256,
                status,created_at,updated_at
            ) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,'PENDING',?,?)""",
            (
                manifest["workflow_id"], manifest["revision"], task["task_id"],
                task["order_index"], task["title"], task["lane"], task["project_id"],
                task["model"], task["reasoning_effort"], task["model_selection_reason"],
                task_json, task_sha, registered_at, registered_at,
            ),
        )
        for check in task["checks"]:
            connection.execute(
                """INSERT INTO implementation_task_checks(
                    workflow_id,workflow_revision,task_id,task_revision,check_id,
                    criterion,method,required,created_at
                ) VALUES(?,?,?,1,?,?,?,?,?)""",
                (manifest["workflow_id"], manifest["revision"], task["task_id"],
                 check["check_id"], check["criterion"], check["method"], 1, registered_at),
            )
    for task in manifest["tasks"]:
        for dependency in task["depends_on"]:
            connection.execute(
                """INSERT INTO implementation_task_dependencies(
                    workflow_id,workflow_revision,task_id,task_revision,
                    depends_on_task_id,depends_on_task_revision,created_at
                ) VALUES(?,?,?,1,?,1,?)""",
                (manifest["workflow_id"], manifest["revision"], task["task_id"],
                 dependency, registered_at),
            )
    workflow_binding = {
        "workflow_id": manifest["workflow_id"], "workflow_revision": manifest["revision"],
    }
    architecture_ladder = manifest["policies"].get("architecture_recovery_effort_ladder")
    if architecture_ladder is not None:
        _insert_reasoning_policy(
            connection, workflow_binding, policy_scope="architecture_recovery",
            policy_key="default", model=manifest["policies"]["architecture_recovery_model"],
            effort_ladder=architecture_ladder,
            escalation_trigger=manifest["policies"]["architecture_recovery_escalation_trigger"],
            decision_reason="registered workflow architecture recovery policy",
            created_at=registered_at,
        )
    for task in manifest["tasks"]:
        if task.get("reasoning_effort_ladder") is None:
            continue
        _insert_reasoning_policy(
            connection, workflow_binding, policy_scope="task", policy_key=task["task_id"],
            model=task["model"], effort_ladder=task["reasoning_effort_ladder"],
            escalation_trigger=task["reasoning_escalation_trigger"],
            decision_reason=f"registered workflow task policy: {task['task_id']}",
            created_at=registered_at,
        )


def command_migrate(args: argparse.Namespace) -> dict[str, Any]:
    """v1 예약 원장 위에 최신 additive implementation schema를 설치한다."""
    db_path = require_existing_db(args.db)
    manifest, manifest_json, manifest_sha = load_manifest(args.manifest)
    if manifest["revision"] != 1:
        raise ValueError("migrate에는 bootstrap revision 1 manifest가 필요합니다")
    with open_readonly(db_path) as probe:
        _ensure_v1(probe)
        schema_current = table_exists(probe, "implementation_schema_migrations") and probe.execute(
            "SELECT 1 FROM implementation_schema_migrations WHERE version=?",
            (IMPLEMENTATION_SCHEMA_VERSION,),
        ).fetchone() is not None
        existing = None
        if table_exists(probe, "implementation_workflows"):
            existing = probe.execute(
                """SELECT spec_sha256 FROM implementation_workflows
                   WHERE workflow_id=? AND workflow_revision=1""",
                (manifest["workflow_id"],),
            ).fetchone()
        if existing is not None and existing["spec_sha256"] != manifest_sha:
            raise RuntimeError("등록된 revision 1 manifest digest가 요청과 다릅니다")
        if schema_current:
            return {"ok": True, "migrated": False, "idempotent": True,
                    "workflow_id": manifest["workflow_id"], "revision": 1,
                    "spec_sha256": manifest_sha,
                    "schema_version": IMPLEMENTATION_SCHEMA_VERSION}
        blocker = _active_migration_blocker(probe)
        if blocker:
            raise RuntimeError(f"안전한 migration을 위해 활성 작업/lease가 없어야 합니다: {blocker}")
        bootstrap_required = existing is None

    backup_path, backup_sha = _backup_database(
        db_path, Path(args.backup_dir).resolve(), manifest_sha
    )
    now = isoformat()
    connection = open_write(db_path, allow_schema_upgrade=True)
    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n" + SCHEMA_SQL + REASONING_POLICY_SCHEMA_SQL + REVISION_SCHEMA_SQL
        )
        blocker = _active_migration_blocker(connection)
        if blocker:
            raise RuntimeError(f"migration 쓰기 직전 활성 작업/lease 발견: {blocker}")
        if bootstrap_required:
            _insert_workflow_manifest(
                connection, manifest, manifest_json, manifest_sha,
                state="ACTIVE", registered_at=now,
            )
        connection.execute(
            """INSERT INTO implementation_schema_migrations(
                   version,applied_at,backup_path,backup_sha256
               ) VALUES(?,?,?,?)""",
            (IMPLEMENTATION_SCHEMA_VERSION, now, str(backup_path), backup_sha),
        )
        if bootstrap_required:
            for historical_version in range(1, IMPLEMENTATION_SCHEMA_VERSION):
                connection.execute(
                    """INSERT OR IGNORE INTO implementation_schema_migrations(
                           version,applied_at,backup_path,backup_sha256
                       ) VALUES(?,?,?,?)""",
                    (historical_version, now, str(backup_path), backup_sha),
                )
        connection.execute(
            """INSERT INTO implementation_writer_contract(
                   singleton,minimum_writer_version,installed_at
               ) VALUES(1,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                   minimum_writer_version=excluded.minimum_writer_version,
                   installed_at=excluded.installed_at""",
            (WRITER_CONTRACT_VERSION, now),
        )
        active = connection.execute(
            """SELECT workflow_revision FROM implementation_workflows
               WHERE workflow_id=? AND state='ACTIVE' ORDER BY workflow_revision DESC LIMIT 1""",
            (manifest["workflow_id"],),
        ).fetchone()
        if active is None:
            raise RuntimeError("head 초기화에 사용할 ACTIVE workflow가 없습니다")
        bootstrap_decision = sha256_bytes(canonical_json({
            "kind": f"schema-v{IMPLEMENTATION_SCHEMA_VERSION}-head-bootstrap",
            "workflow_id": manifest["workflow_id"],
            "revision": active["workflow_revision"],
            "writer_contract_version": WRITER_CONTRACT_VERSION,
        }).encode("utf-8"))
        connection.execute(
            """INSERT OR IGNORE INTO implementation_workflow_heads(
                   workflow_id,active_workflow_revision,generation,writer_contract_version,
                   previous_workflow_revision,activation_decision_sha256,activated_at
               ) VALUES(?,?,1,?,NULL,?,?)""",
            (manifest["workflow_id"], active["workflow_revision"], WRITER_CONTRACT_VERSION,
             bootstrap_decision, now),
        )
        connection.execute(
            """UPDATE implementation_workflow_heads SET writer_contract_version=?
               WHERE workflow_id=?""",
            (WRITER_CONTRACT_VERSION, manifest["workflow_id"]),
        )
        state = connection.execute(
            """SELECT lifecycle_status,current_phase,blocker_code,blocker_fingerprint
               FROM orchestration_state WHERE singleton=1"""
        ).fetchone()
        if (state and state["lifecycle_status"] == "WAIT_EXTERNAL"
                and state["current_phase"] == "WAIT_EXTERNAL_USAGE"):
            previous = dict(state)
            connection.execute(
                """UPDATE orchestration_state
                   SET lifecycle_status='ACTIVE',current_phase='IMPLEMENTATION',
                       current_lane='bootstrap',blocker_code=NULL,blocker_fingerprint=NULL,
                       updated_at=?,version=version+1 WHERE singleton=1""",
                (now,),
            )
            insert_event(
                connection, "policy_superseded", entity_type="orchestration_state", entity_id="1",
                payload={"previous": previous, "approved_policy": "usage_unknown_is_non_blocking",
                         "usage_receipts_modified": False, "manifest_sha256": manifest_sha},
            )
        legacy_health = connection.execute(
            "SELECT value_json FROM metadata WHERE key='scheduler_health'"
        ).fetchone()
        if legacy_health is not None:
            connection.execute(
                "UPDATE metadata SET value_json=?,updated_at=? WHERE key='scheduler_health'",
                (canonical_json({"status": "CONFIGURED_PENDING_SCHEDULED_OBSERVATION",
                                 "configured_interval_minutes": manifest["policies"].get(
                                     "interval_minutes"
                                 ),
                                 "legacy_snapshot_preserved_in_event": True}), now),
            )
            insert_event(
                connection, "scheduler_health.legacy_snapshot_superseded",
                entity_type="metadata", entity_id="scheduler_health",
                payload={"legacy_value_json": legacy_health["value_json"],
                         "new_status": "CONFIGURED_PENDING_SCHEDULED_OBSERVATION"},
            )
        _install_writer_fence_triggers(connection)
        insert_event(
            connection, f"implementation_schema.v{IMPLEMENTATION_SCHEMA_VERSION}_migrated",
            entity_type="implementation_workflow", entity_id=manifest["workflow_id"],
            payload={"schema_version": IMPLEMENTATION_SCHEMA_VERSION,
                     "head_revision": active["workflow_revision"],
                     "writer_contract_version": WRITER_CONTRACT_VERSION,
                     "backup_path": str(backup_path), "backup_sha256": backup_sha},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"ok": True, "migrated": True, "workflow_id": manifest["workflow_id"],
            "revision": 1, "spec_sha256": manifest_sha,
            "schema_version": IMPLEMENTATION_SCHEMA_VERSION,
            "backup_path": str(backup_path), "backup_sha256": backup_sha}


def _workflow_by_revision(connection: sqlite3.Connection, revision: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM implementation_workflows WHERE workflow_id=? AND workflow_revision=?",
        (WORKFLOW_ID, revision),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"등록되지 않은 workflow revision: {revision}")
    return row


def _workflow(connection: sqlite3.Connection) -> sqlite3.Row:
    if not table_exists(connection, "implementation_workflows"):
        raise RuntimeError("implementation workflow migration이 필요합니다")
    if table_exists(connection, "implementation_workflow_heads"):
        head = connection.execute(
            "SELECT active_workflow_revision FROM implementation_workflow_heads WHERE workflow_id=?",
            (WORKFLOW_ID,),
        ).fetchone()
        if head is None:
            raise RuntimeError("implementation workflow head가 없습니다")
        workflow = _workflow_by_revision(connection, head["active_workflow_revision"])
        if workflow["state"] not in {"ACTIVE", "COMPLETE"}:
            raise RuntimeError("workflow head가 ACTIVE/COMPLETE revision을 가리키지 않습니다")
        return workflow
    rows = connection.execute(
        "SELECT * FROM implementation_workflows WHERE workflow_id=? AND state='ACTIVE'",
        (WORKFLOW_ID,),
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("ACTIVE implementation workflow가 정확히 하나여야 합니다")
    return rows[0]


def _source_check_result_for_carry(
    connection: sqlite3.Connection, source_task: sqlite3.Row, check_id: str,
) -> sqlite3.Row | None:
    if source_task["active_attempt_no"] is not None:
        return connection.execute(
            """SELECT r.result_id,r.evidence_sha256,r.status,e.outcome,e.evidence_json
               FROM implementation_check_results r
               JOIN implementation_evidence e ON e.evidence_sha256=r.evidence_sha256
               WHERE r.workflow_id=? AND r.workflow_revision=? AND r.task_id=?
                 AND r.task_revision=? AND r.attempt_no=? AND r.check_id=?""",
            (source_task["workflow_id"], source_task["workflow_revision"],
             source_task["task_id"], source_task["task_revision"],
             source_task["active_attempt_no"], check_id),
        ).fetchone()
    if table_exists(connection, "implementation_check_carry_forwards"):
        return connection.execute(
            """SELECT r.result_id,r.evidence_sha256,r.status,e.outcome,e.evidence_json
               FROM implementation_check_carry_forwards c
               JOIN implementation_check_results r ON r.result_id=c.source_result_id
               JOIN implementation_evidence e ON e.evidence_sha256=r.evidence_sha256
               JOIN implementation_task_carry_forwards h
                 ON h.workflow_id=c.workflow_id
                AND h.to_workflow_revision=c.to_workflow_revision
                AND h.to_task_id=c.to_task_id AND h.to_task_revision=c.to_task_revision
               WHERE c.workflow_id=? AND c.to_workflow_revision=? AND c.to_task_id=?
                 AND c.to_task_revision=? AND c.to_check_id=? AND h.invalidated_at IS NULL""",
            (source_task["workflow_id"], source_task["workflow_revision"],
             source_task["task_id"], source_task["task_revision"], check_id),
        ).fetchone()
    return None


def command_register_revision(args: argparse.Namespace) -> dict[str, Any]:
    manifest, manifest_json, manifest_sha = load_manifest(args.manifest)
    if manifest["revision"] <= 1:
        raise ValueError("register-revision에는 revision 2 이상 manifest가 필요합니다")
    guard = manifest["registration_guard"]
    if (manifest["registration_status"] != "READY_FOR_REGISTRATION"
            or guard["database_registration_allowed"] is not True):
        raise RuntimeError("registration_guard가 database registration을 허용하지 않습니다")
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_current_schema(connection)
        blocker = _active_migration_blocker(connection)
        if blocker:
            raise RuntimeError(f"revision 등록 전 활성 작업/lease를 정리해야 합니다: {blocker}")
        existing = connection.execute(
            """SELECT spec_sha256,state FROM implementation_workflows
               WHERE workflow_id=? AND workflow_revision=?""",
            (manifest["workflow_id"], manifest["revision"]),
        ).fetchone()
        if existing is not None:
            if existing["spec_sha256"] != manifest_sha:
                raise RuntimeError("같은 workflow revision의 다른 manifest는 덮어쓸 수 없습니다")
            connection.rollback()
            return {"ok": True, "registered": False, "idempotent": True,
                    "workflow_id": manifest["workflow_id"], "revision": manifest["revision"],
                    "state": existing["state"], "spec_sha256": manifest_sha}
        head = connection.execute(
            "SELECT * FROM implementation_workflow_heads WHERE workflow_id=?",
            (manifest["workflow_id"],),
        ).fetchone()
        if head is None or head["active_workflow_revision"] != manifest["parent_revision"]:
            raise RuntimeError("revision parent는 현재 workflow head여야 합니다")
        parent_workflow = _workflow_by_revision(connection, manifest["parent_revision"])
        now = isoformat()
        _insert_workflow_manifest(
            connection, manifest, manifest_json, manifest_sha,
            state="DRAFT", registered_at=now,
        )
        for item in manifest["lineage"]:
            source = connection.execute(
                """SELECT 1 FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=1""",
                (manifest["workflow_id"], manifest["parent_revision"], item["from_task_id"]),
            ).fetchone()
            if source is None:
                raise ValueError(f"lineage source task가 parent revision에 없습니다: {item['from_task_id']}")
            decision_sha = sha256_bytes(item["decision"].encode("utf-8"))
            connection.execute(
                """INSERT INTO implementation_task_lineage(
                       workflow_id,from_workflow_revision,from_task_id,from_task_revision,
                       to_workflow_revision,to_task_id,to_task_revision,relation,decision,
                       decision_sha256,registered_at
                   ) VALUES(?,?,?,1,?,?,1,?,?,?,?)""",
                (manifest["workflow_id"], manifest["parent_revision"], item["from_task_id"],
                 manifest["revision"], item["to_task_id"], item["relation"],
                 item["decision"], decision_sha, now),
            )
        carried: list[str] = []
        skipped_carry: list[dict[str, str]] = []
        requested_carry_targets = {item["to_task_id"] for item in manifest["carry_forward"]}
        revision_task_map = {task["task_id"]: task for task in manifest["tasks"]}
        ordered_carry = sorted(
            manifest["carry_forward"],
            key=lambda item: (
                len(_transitive_dependencies(item["to_task_id"], revision_task_map)),
                item["to_task_id"],
            ),
        )
        for item in ordered_carry:
            source = connection.execute(
                """SELECT * FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=1""",
                (manifest["workflow_id"], manifest["parent_revision"], item["from_task_id"]),
            ).fetchone()
            target = connection.execute(
                """SELECT * FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=1""",
                (manifest["workflow_id"], manifest["revision"], item["to_task_id"]),
            ).fetchone()
            if source is None or target is None or source["spec_sha256"] != target["spec_sha256"]:
                raise ValueError("carry-forward task spec은 parent와 byte-identical해야 합니다")
            check_state = _required_check_state(
                connection, source, scope="completion", source_root=parent_workflow["source_root"]
            )
            if source["status"] != "SUCCEEDED" or check_state != "passed":
                skipped_carry.append({"task_id": source["task_id"],
                                      "reason": f"source_{source['status'].lower()}_{check_state}"})
                continue
            target_spec = json.loads(target["spec_json"])
            unavailable_dependencies = [
                dependency for dependency in target_spec.get("depends_on", [])
                if dependency in requested_carry_targets and dependency not in carried
            ]
            if unavailable_dependencies:
                skipped_carry.append({
                    "task_id": source["task_id"],
                    "reason": "dependency_not_carried:" + ",".join(unavailable_dependencies),
                })
                continue
            decision_sha = sha256_bytes(item["decision"].encode("utf-8"))
            connection.execute(
                """INSERT INTO implementation_task_carry_forwards(
                       workflow_id,to_workflow_revision,to_task_id,to_task_revision,
                       from_workflow_revision,from_task_id,from_task_revision,decision,
                       decision_sha256,registered_at
                   ) VALUES(?,?,?,1,?,?,1,?,?,?)""",
                (manifest["workflow_id"], manifest["revision"], item["to_task_id"],
                 manifest["parent_revision"], item["from_task_id"], item["decision"],
                 decision_sha, now),
            )
            target_checks = connection.execute(
                """SELECT check_id FROM implementation_task_checks WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=1 AND required=1
                   ORDER BY check_id""",
                (manifest["workflow_id"], manifest["revision"], item["to_task_id"]),
            ).fetchall()
            for check in target_checks:
                result = _source_check_result_for_carry(connection, source, check["check_id"])
                if result is None or result["status"] != "PASS" or result["outcome"] != "PASS":
                    raise RuntimeError(
                        f"carry-forward source check PASS 누락: {source['task_id']}:{check['check_id']}"
                    )
                connection.execute(
                    """INSERT INTO implementation_check_carry_forwards(
                           workflow_id,to_workflow_revision,to_task_id,to_task_revision,to_check_id,
                           from_workflow_revision,from_task_id,from_task_revision,from_check_id,
                           source_evidence_sha256,source_result_id,registered_at
                       ) VALUES(?,?,?,1,?,?,?,1,?,?,?,?)""",
                    (manifest["workflow_id"], manifest["revision"], item["to_task_id"],
                     check["check_id"], manifest["parent_revision"], item["from_task_id"],
                     check["check_id"], result["evidence_sha256"], result["result_id"], now),
                )
            connection.execute(
                """UPDATE implementation_tasks SET status='SUCCEEDED',updated_at=?
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=1""",
                (now, manifest["workflow_id"], manifest["revision"], item["to_task_id"]),
            )
            carried.append(item["to_task_id"])
        insert_event(
            connection, "implementation_workflow.revision_registered",
            entity_type="implementation_workflow", entity_id=manifest["workflow_id"],
            payload={"revision": manifest["revision"], "parent_revision": manifest["parent_revision"],
                     "state": "DRAFT", "spec_sha256": manifest_sha,
                     "lineage_count": len(manifest["lineage"]), "carried_task_ids": carried,
                     "skipped_carry": skipped_carry,
                     "evidence_copied": False, "attempts_created": False},
        )
        connection.commit()
        return {"ok": True, "registered": True, "workflow_id": manifest["workflow_id"],
                "revision": manifest["revision"], "state": "DRAFT",
                "spec_sha256": manifest_sha, "lineage_count": len(manifest["lineage"]),
                "carried_task_ids": carried, "skipped_carry": skipped_carry}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_workflow_head(args: argparse.Namespace) -> dict[str, Any]:
    with open_readonly(args.db) as connection:
        _require_current_schema(connection)
        head = connection.execute(
            "SELECT * FROM implementation_workflow_heads WHERE workflow_id=?", (WORKFLOW_ID,)
        ).fetchone()
        if head is None:
            raise RuntimeError("workflow head가 없습니다")
        revisions = [dict(row) for row in connection.execute(
            """SELECT workflow_revision,state,spec_sha256,registered_at,completed_at
               FROM implementation_workflows WHERE workflow_id=? ORDER BY workflow_revision""",
            (WORKFLOW_ID,),
        )]
        return {"ok": True, **dict(head), "revisions": revisions}


def _capture_source_snapshot(source_root: str, baseline_commit: str) -> dict[str, str]:
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError(f"source_root가 없습니다: {root}")

    def git(*arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return completed.stdout

    try:
        head = git("rev-parse", "HEAD").decode("ascii").strip()
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
        diff = git("diff", "--binary", "HEAD", "--", ".")
        untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", ".")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        # 비-Git fixture를 위한 명시적 제한 모드다. 운영 Git source에서는 위 digest들이 필수다.
        return {
            "kind": "non-git-baseline-v1", "source_root": str(root),
            "baseline_commit": baseline_commit,
        }
    untracked_digest = hashlib.sha256()
    for raw_path in sorted(part for part in untracked.split(b"\0") if part):
        candidate = root / os.fsdecode(raw_path)
        untracked_digest.update(raw_path)
        untracked_digest.update(b"\0")
        if candidate.is_file():
            untracked_digest.update(bytes.fromhex(sha256_file(candidate)))
        else:
            untracked_digest.update(b"NON_FILE")
        untracked_digest.update(b"\0")
    return {
        "kind": "git-worktree-v1", "source_root": str(root), "git_head": head,
        "status_sha256": sha256_bytes(status), "diff_sha256": sha256_bytes(diff),
        "untracked_sha256": untracked_digest.hexdigest(),
    }


def _capture_execution_source_snapshot(workflow: sqlite3.Row) -> dict[str, Any]:
    """Capture and enforce ready-time source policy before creating an Attempt."""
    manifest = json.loads(workflow["spec_json"])
    policies = manifest.get("policies", {})
    required_branch = policies.get("required_git_branch")
    main_checkout_only = policies.get("main_checkout_only") is True
    snapshot: dict[str, Any] = dict(_capture_source_snapshot(
        workflow["source_root"], workflow["baseline_commit"]
    ))
    if required_branch is None and not main_checkout_only:
        return snapshot
    if snapshot.get("kind") != "git-worktree-v1":
        raise RuntimeError("EXECUTION_SOURCE_POLICY_MISMATCH: Git source_root가 필요합니다")

    root = Path(workflow["source_root"]).resolve()

    def git_text(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            return completed.stdout.decode("utf-8").strip()
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
            raise RuntimeError(
                "EXECUTION_SOURCE_POLICY_MISMATCH: Git checkout 정보를 확인할 수 없습니다"
            ) from error

    top_level = Path(git_text("rev-parse", "--show-toplevel")).resolve()
    branch = git_text("branch", "--show-current")
    git_dir_text = git_text("rev-parse", "--git-dir")
    common_dir_text = git_text("rev-parse", "--git-common-dir")
    git_dir = Path(git_dir_text)
    common_dir = Path(common_dir_text)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    git_dir = git_dir.resolve()
    common_dir = common_dir.resolve()
    is_main_checkout = git_dir == common_dir

    if top_level != root:
        raise RuntimeError(
            "EXECUTION_SOURCE_POLICY_MISMATCH: source_root는 Git 최상위 checkout이어야 합니다"
        )
    if required_branch is not None and branch != required_branch:
        raise RuntimeError(
            f"EXECUTION_SOURCE_POLICY_MISMATCH: branch={branch!r}, required={required_branch!r}"
        )
    if main_checkout_only and not is_main_checkout:
        raise RuntimeError(
            "EXECUTION_SOURCE_POLICY_MISMATCH: linked worktree가 아닌 main checkout이 필요합니다"
        )
    snapshot.update({
        "branch": branch,
        "top_level": str(top_level),
        "git_dir": str(git_dir),
        "git_common_dir": str(common_dir),
        "main_checkout": is_main_checkout,
    })
    return snapshot


def _load_activation_decision(
    path: str, *, from_revision: int, to_revision: int, expected_generation: int,
    target_workflow: sqlite3.Row,
) -> tuple[Path, str, dict[str, Any]]:
    decision_path = Path(path).resolve()
    if not decision_path.is_file():
        raise ValueError("activation decision file이 없습니다")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "workflow_id", "from_revision", "to_revision",
        "expected_generation", "target_spec_sha256", "source_snapshot", "outcome",
        "reviewer", "observed_at", "findings", "files",
    }
    if not isinstance(decision, dict):
        raise ValueError("activation decision은 JSON 객체여야 합니다")
    _expect_keys(decision, required, required, "activation decision")
    if decision["schema_version"] != 1 or decision["workflow_id"] != WORKFLOW_ID:
        raise ValueError("activation decision schema/workflow binding 불일치")
    if (decision["from_revision"], decision["to_revision"], decision["expected_generation"]) != (
        from_revision, to_revision, expected_generation
    ):
        raise ValueError("activation decision revision/generation binding 불일치")
    if decision["target_spec_sha256"] != target_workflow["spec_sha256"]:
        raise ValueError("activation decision target spec binding 불일치")
    expected_snapshot = _capture_source_snapshot(
        target_workflow["source_root"], target_workflow["baseline_commit"],
    )
    if decision["source_snapshot"] != expected_snapshot:
        raise ValueError("activation decision source snapshot이 현재 source와 다릅니다")
    if (expected_snapshot["kind"] == "git-worktree-v1"
            and expected_snapshot["git_head"] != target_workflow["baseline_commit"]):
        raise ValueError("activation 대상 baseline commit이 현재 Git HEAD와 다릅니다")
    if decision["outcome"] != "PASS" or decision["findings"] != []:
        raise RuntimeError("finding 없는 독립 PASS decision만 activation에 사용할 수 있습니다")
    reviewer = decision["reviewer"]
    if not isinstance(reviewer, dict) or set(reviewer) != {"kind", "id"}:
        raise ValueError("activation decision reviewer 형식이 유효하지 않습니다")
    _nonempty_string(reviewer["kind"], "activation decision.reviewer.kind")
    _nonempty_string(reviewer["id"], "activation decision.reviewer.id")
    parse_time(_nonempty_string(decision["observed_at"], "activation decision.observed_at"))
    if not isinstance(decision["files"], list) or not decision["files"]:
        raise ValueError("activation decision.files는 비어 있지 않은 배열이어야 합니다")
    for index, item in enumerate(decision["files"]):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError(f"activation decision.files[{index}] 형식이 유효하지 않습니다")
        candidate = Path(item["path"]).resolve()
        if not candidate.is_file() or sha256_file(candidate) != item["sha256"]:
            raise ValueError(f"activation audit file이 없거나 변조되었습니다: {candidate}")
    return decision_path, sha256_file(decision_path), decision


def _activation_blockers(
    connection: sqlite3.Connection, target_workflow: sqlite3.Row, *, forward: bool,
) -> list[str]:
    blockers: list[str] = []
    blocker = _active_migration_blocker(connection)
    if blocker:
        blockers.append(blocker)
    open_run = connection.execute(
        "SELECT run_id FROM orchestration_runs WHERE finished_at IS NULL LIMIT 1"
    ).fetchone()
    if open_run:
        blockers.append(f"open run: {open_run['run_id']}")
    unfinished = connection.execute(
        """SELECT workflow_revision,task_id,attempt_no,status
           FROM implementation_task_attempts
           WHERE status IN ('RESERVED','DISPATCHED','RUNNING','AWAITING_REVIEW','FAILED_OBSERVED')
           LIMIT 1"""
    ).fetchone()
    if unfinished:
        blockers.append(
            f"unfinished attempt: r{unfinished['workflow_revision']}:{unfinished['task_id']}:"
            f"{unfinished['attempt_no']}:{unfinished['status']}"
        )
    if forward:
        target_spec = json.loads(target_workflow["spec_json"])
        if (target_spec["registration_status"] != "READY_FOR_REGISTRATION"
                or target_spec["registration_guard"]["activation_allowed"] is not True):
            blockers.append("registration_guard가 activation을 허용하지 않습니다")
        target_attempt = connection.execute(
            """SELECT task_id FROM implementation_task_attempts
               WHERE workflow_id=? AND workflow_revision=? LIMIT 1""",
            (target_workflow["workflow_id"], target_workflow["workflow_revision"]),
        ).fetchone()
        if target_attempt:
            blockers.append(f"target revision already has attempts: {target_attempt['task_id']}")
    for task in connection.execute(
        """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
           AND status='SUCCEEDED'""",
        (target_workflow["workflow_id"], target_workflow["workflow_revision"]),
    ):
        if _required_check_state(
            connection, task, scope="completion", source_root=target_workflow["source_root"]
        ) != "passed":
            blockers.append(f"carried task is not PASS/fresh: {task['task_id']}")
    return blockers


def command_activate_revision(args: argparse.Namespace) -> dict[str, Any]:
    if args.revision < 1 or args.expected_generation < 1:
        raise ValueError("revision과 expected-generation은 양수여야 합니다")
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_current_schema(connection)
        head = connection.execute(
            "SELECT * FROM implementation_workflow_heads WHERE workflow_id=?", (WORKFLOW_ID,)
        ).fetchone()
        if head is None or head["generation"] != args.expected_generation:
            raise RuntimeError("workflow head generation CAS가 실패했습니다")
        current_revision = head["active_workflow_revision"]
        target = _workflow_by_revision(connection, args.revision)
        if args.revision == current_revision:
            connection.rollback()
            return {"ok": True, "activated": False, "idempotent": True,
                    "active_workflow_revision": current_revision,
                    "generation": head["generation"], "state": "ACTIVE"}
        if target["state"] not in {"DRAFT", "RETIRED"}:
            raise RuntimeError("DRAFT 또는 RETIRED revision만 activation할 수 있습니다")
        if args.revision > current_revision:
            spec = json.loads(target["spec_json"])
            if spec.get("parent_revision") != current_revision:
                raise RuntimeError("forward activation은 현재 head의 직계 successor만 허용합니다")
        else:
            current_attempt = connection.execute(
                """SELECT 1 FROM implementation_task_attempts
                   WHERE workflow_id=? AND workflow_revision=? LIMIT 1""",
                (WORKFLOW_ID, current_revision),
            ).fetchone()
            current_dispatch = connection.execute(
                """SELECT 1 FROM implementation_task_dispatches
                   WHERE workflow_id=? AND workflow_revision=? LIMIT 1""",
                (WORKFLOW_ID, current_revision),
            ).fetchone()
            if current_attempt or current_dispatch:
                raise RuntimeError(
                    "현재 revision에 dispatch/attempt가 생긴 뒤에는 rollback할 수 없고 forward revision이 필요합니다"
                )
        decision_path, decision_sha, _ = _load_activation_decision(
            args.decision_file, from_revision=current_revision, to_revision=args.revision,
            expected_generation=args.expected_generation, target_workflow=target,
        )
        blockers = _activation_blockers(
            connection, target, forward=args.revision > current_revision,
        )
        if blockers:
            raise RuntimeError(f"revision activation blocker: {blockers}")
        now = isoformat()
        leaving_state = "RETIRED" if args.revision > current_revision else "DRAFT"
        connection.execute(
            """UPDATE implementation_workflows SET state=?
               WHERE workflow_id=? AND workflow_revision=? AND state='ACTIVE'""",
            (leaving_state, WORKFLOW_ID, current_revision),
        )
        connection.execute(
            """UPDATE implementation_workflows SET state='ACTIVE'
               WHERE workflow_id=? AND workflow_revision=?""",
            (WORKFLOW_ID, args.revision),
        )
        updated = connection.execute(
            """UPDATE implementation_workflow_heads
               SET active_workflow_revision=?,generation=generation+1,
                   writer_contract_version=?,previous_workflow_revision=?,
                   activation_decision_sha256=?,activated_at=?
               WHERE workflow_id=? AND generation=? AND active_workflow_revision=?""",
            (args.revision, WRITER_CONTRACT_VERSION, current_revision, decision_sha, now,
             WORKFLOW_ID, args.expected_generation, current_revision),
        ).rowcount
        if updated != 1:
            raise RuntimeError("workflow head activation CAS가 실패했습니다")
        insert_event(
            connection, "implementation_workflow.revision_activated",
            entity_type="implementation_workflow", entity_id=WORKFLOW_ID,
            payload={"from_revision": current_revision, "to_revision": args.revision,
                     "previous_generation": args.expected_generation,
                     "generation": args.expected_generation + 1,
                     "activation_decision_path": str(decision_path),
                     "activation_decision_sha256": decision_sha,
                     "scheduled_automation_status_changed": False},
        )
        connection.commit()
        return {"ok": True, "activated": True,
                "active_workflow_revision": args.revision,
                "generation": args.expected_generation + 1, "state": "ACTIVE",
                "previous_workflow_revision": current_revision,
                "activation_decision_sha256": decision_sha,
                "scheduled_automation_status_changed": False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _carried_required_check_state(
    connection: sqlite3.Connection, task: sqlite3.Row, *, scope: str | None,
    source_root: str | None,
) -> str:
    if not table_exists(connection, "implementation_task_carry_forwards"):
        return "missing"
    header = connection.execute(
        """SELECT * FROM implementation_task_carry_forwards
           WHERE workflow_id=? AND to_workflow_revision=? AND to_task_id=?
             AND to_task_revision=? AND invalidated_at IS NULL""",
        (task["workflow_id"], task["workflow_revision"], task["task_id"],
         task["task_revision"]),
    ).fetchone()
    if header is None:
        return "missing"
    rows = connection.execute(
        """SELECT c.check_id,m.source_result_id,r.status,e.outcome,e.evidence_json
           FROM implementation_task_checks c
           LEFT JOIN implementation_check_carry_forwards m
             ON m.workflow_id=c.workflow_id
            AND m.to_workflow_revision=c.workflow_revision
            AND m.to_task_id=c.task_id AND m.to_task_revision=c.task_revision
            AND m.to_check_id=c.check_id
           LEFT JOIN implementation_check_results r ON r.result_id=m.source_result_id
           LEFT JOIN implementation_evidence e ON e.evidence_sha256=r.evidence_sha256
           WHERE c.workflow_id=? AND c.workflow_revision=? AND c.task_id=?
             AND c.task_revision=? AND c.required=1""",
        (task["workflow_id"], task["workflow_revision"], task["task_id"],
         task["task_revision"]),
    ).fetchall()
    if not rows:
        return "passed"
    if any(row["source_result_id"] is None or row["status"] != "PASS"
           or row["outcome"] != "PASS" or row["evidence_json"] is None for row in rows):
        return "missing"
    if source_root is None:
        source = connection.execute(
            """SELECT source_root FROM implementation_workflows
               WHERE workflow_id=? AND workflow_revision=?""",
            (task["workflow_id"], task["workflow_revision"]),
        ).fetchone()
        if source is None:
            return "missing"
        source_root = source["source_root"]
    required_contract = _required_evidence_contract(task)
    return "passed" if all(
        _evidence_fresh_for_scope(
            row["evidence_json"], source_root=source_root, scope=scope,
            required_contract=required_contract,
        ) for row in rows
    ) else "stale"


def _required_check_state(
    connection: sqlite3.Connection, task: sqlite3.Row, *, scope: str | None = None,
    source_root: str | None = None,
) -> str:
    if task["active_attempt_no"] is None:
        return _carried_required_check_state(
            connection, task, scope=scope, source_root=source_root,
        )
    rows = connection.execute(
        """SELECT c.check_id,r.status,e.outcome,e.evidence_json
           FROM implementation_task_checks c
           LEFT JOIN implementation_check_results r
             ON r.workflow_id=c.workflow_id AND r.workflow_revision=c.workflow_revision
            AND r.task_id=c.task_id AND r.task_revision=c.task_revision
            AND r.attempt_no=? AND r.check_id=c.check_id
           LEFT JOIN implementation_evidence e ON e.evidence_sha256=r.evidence_sha256
           WHERE c.workflow_id=? AND c.workflow_revision=? AND c.task_id=? AND c.task_revision=?
             AND c.required=1""",
        (task["active_attempt_no"], task["workflow_id"], task["workflow_revision"],
         task["task_id"], task["task_revision"]),
    ).fetchall()
    if any(row["status"] != "PASS" or row["outcome"] != "PASS" for row in rows):
        return "missing"
    if not rows:
        return "passed"
    if source_root is None:
        source = connection.execute(
            """SELECT source_root FROM implementation_workflows
               WHERE workflow_id=? AND workflow_revision=?""",
            (task["workflow_id"], task["workflow_revision"]),
        ).fetchone()
        if source is None:
            return "missing"
        source_root = source["source_root"]
    required_contract = _required_evidence_contract(task)
    if any(row["evidence_json"] is None for row in rows):
        return "missing"
    return "passed" if all(
        row["evidence_json"] is not None and _evidence_fresh_for_scope(
            row["evidence_json"], source_root=source_root, scope=scope,
            required_contract=required_contract,
        )
        for row in rows
    ) else "stale"


def _required_checks_passed(
    connection: sqlite3.Connection, task: sqlite3.Row, *, scope: str | None = None,
    source_root: str | None = None,
) -> bool:
    return _required_check_state(
        connection, task, scope=scope, source_root=source_root,
    ) == "passed"


def _stale_succeeded_tasks(
    connection: sqlite3.Connection, workflow: sqlite3.Row,
) -> list[sqlite3.Row]:
    tasks = connection.execute(
        """SELECT * FROM implementation_tasks
           WHERE workflow_id=? AND workflow_revision=? AND status='SUCCEEDED'
           ORDER BY order_index,task_id""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    return [
        task for task in tasks
        if _required_check_state(
            connection, task, scope="completion", source_root=workflow["source_root"],
        ) == "stale"
    ]


def _ready_tasks(connection: sqlite3.Connection, workflow: sqlite3.Row) -> list[sqlite3.Row]:
    pending = connection.execute(
        """SELECT t.* FROM implementation_tasks t
           WHERE t.workflow_id=? AND t.workflow_revision=? AND t.status='PENDING'
           ORDER BY t.order_index,t.task_id""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    ready: list[sqlite3.Row] = []
    for task in pending:
        dependencies = connection.execute(
            """SELECT p.* FROM implementation_task_dependencies d
               JOIN implementation_tasks p
                 ON p.workflow_id=d.workflow_id AND p.workflow_revision=d.workflow_revision
                AND p.task_id=d.depends_on_task_id AND p.task_revision=d.depends_on_task_revision
               WHERE d.workflow_id=? AND d.workflow_revision=? AND d.task_id=? AND d.task_revision=?""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
        ).fetchall()
        if all(
            dependency["status"] == "SUCCEEDED" and _required_checks_passed(
                connection, dependency, scope="dependency", source_root=workflow["source_root"],
            )
            for dependency in dependencies
        ):
            ready.append(task)
    return ready


def _active_dispatch(connection: sqlite3.Connection) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                  j.attempt_no,j.assignment_sha256
           FROM orchestration_state s JOIN dispatches d ON d.dispatch_id=s.active_dispatch_id
           LEFT JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
           WHERE s.singleton=1"""
    ).fetchone()


def _decision(connection: sqlite3.Connection, workflow: sqlite3.Row) -> dict[str, Any]:
    active = _active_dispatch(connection)
    if active is not None:
        if active["status"] == "interrupted":
            interrupt = _interrupt_observation(connection, active["dispatch_id"])
            return {
                "decision": "USER_DECISION_REQUIRED",
                "action": "INTERRUPTION_REQUIRES_DECISION",
                "reason": (
                    "interrupted turn의 호출 출처와 부분 효과를 자동으로 추정하거나 "
                    "새 task로 재실행할 수 없습니다"
                ),
                "dispatch": dict(active),
                "interrupt": interrupt,
            }
        if active["status"] in {"failed", "needs_attention", "creation_failed", "blocked", "cancelled"}:
            return {"decision": "OBSERVE", "action": "REVIEW_REQUIRED",
                    "reason": "terminal failure/no-effect rejection must receive independent review first",
                    "dispatch": dict(active)}
        return {"decision": "OBSERVE", "reason": "active dispatch must be observed/reviewed first",
                "dispatch": dict(active)}
    # 실패 task가 남아 있어도 그 실패를 해소하도록 등록된 recovery는 먼저 실행해야 한다.
    # 반대 순서는 원 task의 실패 이력을 보존하는 설계에서 영구 교착을 만든다.
    recovery_ready = [task for task in _ready_tasks(connection, workflow) if task["lane"] == "recovery"]
    if recovery_ready:
        return {"decision": "READY", "reason": "evidence-backed recovery is ready",
                "task_id": recovery_ready[0]["task_id"], "lane": "recovery"}
    failed = connection.execute(
        """SELECT * FROM implementation_tasks
           WHERE workflow_id=? AND workflow_revision=? AND status='FAILED'
           ORDER BY CASE lane WHEN 'recovery' THEN 0 ELSE 1 END,order_index DESC,task_id""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    if failed:
        budgets = [_recovery_budget_state(connection, workflow, task) for task in failed]
        for task, budget in zip(failed, budgets):
            if budget["available"]:
                return {
                    "decision": "RECOVERY_REQUIRED",
                    "reason": "failed task requires evidence-backed recovery",
                    "task_id": task["task_id"],
                    "failure_fingerprint": task["failure_fingerprint"],
                    "recovery_budget": budget,
                }
        return {
            "decision": "USER_DECISION_REQUIRED",
            "action": "RECOVERY_LIMIT_REACHED",
            "reason": (
                "승인된 workflow-lifetime recovery 한도가 소진되어 자동 복구를 "
                "등록할 수 없습니다"
            ),
            "task_id": failed[0]["task_id"],
            "failure_fingerprint": failed[0]["failure_fingerprint"],
            "recovery_budget": budgets[0],
            "blocked_task_ids": [task["task_id"] for task in failed],
        }
    stale = _stale_succeeded_tasks(connection, workflow)
    if stale:
        return {
            "decision": "REVALIDATION_REQUIRED",
            "action": "REVALIDATE_SUCCESS",
            "reason": "a previously successful task has evidence that is stale for current inputs",
            "task_id": stale[0]["task_id"],
            "stale_task_ids": [task["task_id"] for task in stale],
        }
    remaining = connection.execute(
        "SELECT COUNT(*) FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=? AND status!='SUCCEEDED'",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchone()[0]
    if remaining == 0:
        tasks = connection.execute(
            "SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        ).fetchall()
        if all(_required_checks_passed(
            connection, task, scope="completion", source_root=workflow["source_root"]
        ) for task in tasks):
            return {"decision": "COMPLETE", "reason": "all registered tasks and current-attempt required checks succeeded"}
        return {"decision": "WAIT", "reason": "SUCCEEDED projection lacks current-attempt required PASS evidence"}
    ready = _ready_tasks(connection, workflow)
    if ready:
        return {"decision": "READY", "reason": "dependency and required-check gates passed",
                "task_id": ready[0]["task_id"], "lane": ready[0]["lane"]}
    return {"decision": "WAIT", "reason": "no active, failed, complete, or ready task"}


def _status(connection: sqlite3.Connection) -> dict[str, Any]:
    workflow = _workflow(connection)
    counts = {
        row["status"]: row["count"] for row in connection.execute(
            """SELECT status,COUNT(*) count FROM implementation_tasks
               WHERE workflow_id=? AND workflow_revision=? GROUP BY status""",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        )
    }
    workflow_summary = {key: workflow[key] for key in workflow.keys() if key != "spec_json"}
    return {"ok": True, "workflow": workflow_summary, "task_counts": counts,
            "active_dispatch": row_dict(_active_dispatch(connection)),
            **_decision(connection, workflow)}


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    with open_readonly(args.db) as connection:
        return _status(connection)


def command_reopen_stale(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        if _active_dispatch(connection) is not None:
            raise RuntimeError("활성 dispatch를 먼저 관찰·검토해야 합니다")
        task = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=1""",
            (workflow["workflow_id"], workflow["workflow_revision"], args.task_id),
        ).fetchone()
        if task is None:
            raise ValueError(f"알 수 없는 task: {args.task_id}")
        if task["status"] != "SUCCEEDED":
            raise RuntimeError("SUCCEEDED task만 stale revalidation을 위해 다시 열 수 있습니다")
        state = _required_check_state(
            connection, task, scope="completion", source_root=workflow["source_root"],
        )
        if state != "stale":
            raise RuntimeError(f"task evidence가 stale 상태가 아닙니다: {state}")
        now = isoformat()
        connection.execute(
            """UPDATE implementation_tasks
               SET status='PENDING',active_attempt_no=NULL,failure_fingerprint=NULL,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (now, task["workflow_id"], task["workflow_revision"], task["task_id"],
             task["task_revision"]),
        )
        insert_event(
            connection, "implementation_task.revalidation_requested", run_id=args.run_id,
            entity_type="implementation_task", entity_id=task["task_id"],
            payload={
                "workflow_id": task["workflow_id"],
                "workflow_revision": task["workflow_revision"],
                "task_revision": task["task_revision"],
                "previous_status": "SUCCEEDED", "new_status": "PENDING",
                "previous_attempt_no": task["active_attempt_no"],
                "reason": "completion-scope evidence became stale after bound input changes",
                "history_preserved": True,
            },
        )
        revalidation_event_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.commit()
        return {
            "ok": True, "reopened": True, "task_id": task["task_id"],
            "previous_attempt_no": task["active_attempt_no"],
            "next_attempt_no": task["attempt_count"] + 1,
            "revalidation_event_id": revalidation_event_id,
            **_decision(connection, workflow),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_task(args: argparse.Namespace) -> dict[str, Any]:
    with open_readonly(args.db) as connection:
        workflow = _workflow(connection)
        task = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=1""",
            (workflow["workflow_id"], workflow["workflow_revision"], args.task_id),
        ).fetchone()
        if not task:
            raise RuntimeError(f"알 수 없는 task: {args.task_id}")
        dependencies = [dict(row) for row in connection.execute(
            """SELECT d.depends_on_task_id,p.status,p.spec_sha256 FROM implementation_task_dependencies d
               JOIN implementation_tasks p ON p.workflow_id=d.workflow_id
                AND p.workflow_revision=d.workflow_revision AND p.task_id=d.depends_on_task_id
                AND p.task_revision=d.depends_on_task_revision
               WHERE d.workflow_id=? AND d.workflow_revision=? AND d.task_id=? AND d.task_revision=?
               ORDER BY p.order_index,p.task_id""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
        )]
        checks = [dict(row) for row in connection.execute(
            """SELECT * FROM implementation_task_checks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=? ORDER BY check_id""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
        )]
        attempts = [dict(row) for row in connection.execute(
            """SELECT * FROM implementation_task_attempts WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=? ORDER BY attempt_no""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
        )]
        current_dispatch = row_dict(connection.execute(
            """SELECT d.*,j.assignment_sha256,j.attempt_no,j.receipt_path,j.receipt_sha256
               FROM implementation_task_dispatches j JOIN dispatches d ON d.dispatch_id=j.dispatch_id
               WHERE j.workflow_id=? AND j.workflow_revision=? AND j.task_id=? AND j.task_revision=?
               ORDER BY j.attempt_no DESC LIMIT 1""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
        ).fetchone())
        effective_binding = _task_model_binding(connection, workflow, task)
        return {"ok": True, "task": dict(task), "spec": json.loads(task["spec_json"]),
                "dependencies": dependencies, "checks": checks, "attempts": attempts,
                "current_dispatch": current_dispatch, "effective_model_binding": effective_binding}


def command_configure_reasoning_policy(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + REASONING_POLICY_SCHEMA_SQL)
        workflow = _workflow(connection)
        lock = connection.execute(
            "SELECT owner_run_id,expires_at FROM orchestration_locks WHERE lock_name=?",
            (LOCK_NAME,),
        ).fetchone()
        if lock is not None and lock["expires_at"] > isoformat():
            raise RuntimeError(
                f"활성 scheduler lease 중에는 정책을 바꿀 수 없습니다: {lock['owner_run_id']}"
            )
        if args.scope == "task":
            task = connection.execute(
                """SELECT * FROM implementation_tasks
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=1""",
                (workflow["workflow_id"], workflow["workflow_revision"], args.key),
            ).fetchone()
            if task is None:
                raise ValueError(f"알 수 없는 task policy key: {args.key}")
            if task["status"] != "PENDING" or task["attempt_count"] != 0:
                raise RuntimeError("task reasoning 초기 정책은 미실행 PENDING task에만 설정할 수 있습니다")
            if task["model"] != args.model:
                raise ValueError("task의 등록 model과 policy model이 다릅니다")
        elif args.key != "default":
            raise ValueError("architecture_recovery policy key는 default여야 합니다")
        result = _insert_reasoning_policy(
            connection, workflow, policy_scope=args.scope, policy_key=args.key,
            model=args.model, effort_ladder=args.effort_ladder,
            escalation_trigger=args.escalation_trigger,
            decision_reason=args.reason, created_at=isoformat(),
        )
        insert_event(
            connection, "implementation_reasoning_policy.configured",
            entity_type="reasoning_policy", entity_id=f"{args.scope}:{args.key}",
            payload={
                "policy_scope": args.scope, "policy_key": args.key,
                "policy_revision": result["policy_revision"], "model": args.model,
                "initial_effort": result["initial_effort"],
                "effort_ladder": json.loads(result["effort_ladder_json"]),
                "escalation_trigger": args.escalation_trigger,
                "decision_reason": args.reason, "inserted": result["inserted"],
            },
        )
        connection.commit()
        return {
            "ok": True, "scope": args.scope, "key": args.key, "model": args.model,
            "initial_effort": result["initial_effort"],
            "effort_ladder": json.loads(result["effort_ladder_json"]),
            "escalation_trigger": args.escalation_trigger,
            "policy_revision": result["policy_revision"], "inserted": result["inserted"],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_reasoning_policies(args: argparse.Namespace) -> dict[str, Any]:
    with open_readonly(args.db) as connection:
        workflow = _workflow(connection)
        if not table_exists(connection, "implementation_reasoning_policies"):
            return {"ok": True, "policies": []}
        rows = [dict(row) for row in connection.execute(
            """SELECT * FROM implementation_reasoning_policies
               WHERE workflow_id=? AND workflow_revision=?
               ORDER BY policy_scope,policy_key,policy_revision""",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        )]
        for row in rows:
            row["effort_ladder"] = json.loads(row.pop("effort_ladder_json"))
        return {"ok": True, "policies": rows}


def _lease_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    lock = connection.execute(
        "SELECT * FROM orchestration_locks WHERE lock_name=?", (LOCK_NAME,)
    ).fetchone()
    if lock is None or lock["owner_run_id"] != run_id:
        raise RuntimeError(f"run {run_id!r}은 현재 lease owner가 아닙니다")
    if lock["expires_at"] <= isoformat():
        raise RuntimeError(f"run {run_id!r}의 lease가 만료되었습니다")
    return lock


def command_begin(args: argparse.Namespace) -> dict[str, Any]:
    if args.lease_seconds <= 0:
        raise ValueError("lease-seconds는 양수여야 합니다")
    connection = open_write(args.db)
    now_value = utc_now()
    now = isoformat(now_value)
    run_id = f"run_{uuid.uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        workflow = _workflow(connection)
        if args.automation_id != workflow["automation_id"]:
            raise RuntimeError("automation-id가 manifest binding과 일치하지 않습니다")
        current = connection.execute(
            "SELECT * FROM orchestration_locks WHERE lock_name=?", (LOCK_NAME,)
        ).fetchone()
        if current and current["expires_at"] > now:
            reason = f"active lease owned by {current['owner_run_id']} until {current['expires_at']}"
            connection.execute(
                """INSERT INTO orchestration_runs(
                   run_id,automation_id,started_at,finished_at,outcome,decision_code,
                   decision_reason,snapshot_json) VALUES(?,?,?,?, 'SKIPPED_LOCKED','WAIT',?,?)""",
                (run_id, args.automation_id, now, now, reason,
                 canonical_json({"owner_run_id": current["owner_run_id"],
                                 "expires_at": current["expires_at"]})),
            )
            insert_event(connection, "implementation_run.skipped_locked", run_id=run_id,
                         entity_type="lock", entity_id=LOCK_NAME,
                         payload={"owner_run_id": current["owner_run_id"],
                                  "expires_at": current["expires_at"],
                                  "existing_lease_preserved": True})
            connection.commit()
            return {"ok": True, "acquired": False, "decision": "WAIT",
                    "run_id": run_id, "outcome": "SKIPPED_LOCKED", "reason": "ACTIVE_LEASE",
                    "owner_run_id": current["owner_run_id"],
                    "expires_at": current["expires_at"]}
        stale_owner = current["owner_run_id"] if current else None
        if stale_owner:
            connection.execute(
                """UPDATE orchestration_runs SET finished_at=COALESCE(finished_at,?),
                   outcome=COALESCE(outcome,'ABANDONED'),decision_code=COALESCE(decision_code,'LEASE_EXPIRED'),
                   decision_reason=COALESCE(decision_reason,'lease expired; external dispatch intent preserved')
                   WHERE run_id=?""",
                (now, stale_owner),
            )
            insert_event(connection, "run.abandoned_lease_expired", run_id=stale_owner,
                         entity_type="run", entity_id=stale_owner,
                         payload={"external_intent_preserved": _active_dispatch(connection) is not None})
        connection.execute("DELETE FROM orchestration_locks WHERE lock_name=?", (LOCK_NAME,))
        expires = isoformat(now_value + timedelta(seconds=args.lease_seconds))
        connection.execute(
            "INSERT INTO orchestration_locks(lock_name,owner_run_id,acquired_at,expires_at) VALUES(?,?,?,?)",
            (LOCK_NAME, run_id, now, expires),
        )
        connection.execute(
            "INSERT INTO orchestration_runs(run_id,automation_id,started_at,snapshot_json) VALUES(?,?,?,'{}')",
            (run_id, args.automation_id, now),
        )
        connection.execute(
            "UPDATE orchestration_state SET last_run_id=?,updated_at=?,version=version+1 WHERE singleton=1",
            (run_id, now),
        )
        decision = _decision(connection, workflow)
        connection.execute(
            "UPDATE orchestration_runs SET decision_code=?,decision_lane=?,decision_reason=?,snapshot_json=? WHERE run_id=?",
            (decision["decision"], decision.get("lane"), decision["reason"],
             canonical_json(decision), run_id),
        )
        insert_event(connection, "implementation_run.started", run_id=run_id,
                     entity_type="implementation_workflow", entity_id=workflow["workflow_id"],
                     payload={"lease_expires_at": expires, "replaced_stale_owner": stale_owner,
                              **decision})
        connection.commit()
        return {"ok": True, "acquired": True, "run_id": run_id, "expires_at": expires,
                **decision}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _make_prompt(
    workflow: sqlite3.Row, task: sqlite3.Row, dispatch_id: str, db_path: str,
    model_binding: dict[str, Any], execution_source_snapshot: dict[str, Any],
) -> str:
    spec = json.loads(task["spec_json"])
    handoff = {
        "schema_version": 1, "workflow_id": workflow["workflow_id"],
        "workflow_revision": workflow["workflow_revision"],
        "task_id": task["task_id"], "task_revision": task["task_revision"],
        "task_spec_sha256": task["spec_sha256"], "dispatch_id": dispatch_id,
        "assignment_sha256": "<read current immutable value from task command>",
        "status": "completed|failed|needs_attention", "summary": "<concise result>",
        "report_path": "<absolute path>", "evidence_paths": ["<absolute path>"],
    }
    return (
        "FlowMarshal 1.0 승인 구현 workflow의 단일 task를 수행하십시오.\n"
        f"workflow_id: {workflow['workflow_id']} revision: {workflow['workflow_revision']}\n"
        f"task full spec (immutable):\n{json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
        "effective execution model binding (user-approved policy; this supersedes only the "
        "immutable spec's model/reasoning_effort fields for this Attempt):\n"
        f"{json.dumps(model_binding, ensure_ascii=False, sort_keys=True, indent=2)}\n\n"
        f"task_spec_sha256: {task['spec_sha256']}\n"
        f"dispatch_id: {dispatch_id}\n"
        f"registry DB: {Path(db_path).resolve()}\n"
        f"registry helper: {Path(__file__).resolve()}\n"
        "assignment_sha256는 prompt 자기참조를 피하기 위해 등록 직후 계산됩니다. 부모 또는 reviewer는 "
        f"helper의 `--db <registry DB> task --task-id {task['task_id']}` 읽기 전용 출력에서 "
        "current_dispatch.assignment_sha256를 조회해 handoff/evidence에 결속하십시오.\n"
        f"승인 계획: {workflow['approval_plan_path']} (sha256 {workflow['approval_plan_sha256']})\n"
        f"runtime source_root: {workflow['source_root']}\n"
        "ready-time execution source snapshot (immutable assignment binding):\n"
        f"{json.dumps(execution_source_snapshot, ensure_ascii=False, sort_keys=True, indent=2)}\n"
        f"artifact_root: {workflow['artifact_root']}\n"
        "현재 사용자 승인 계획은 기존 AGENTS.md의 낡은 usage-missing 전역 차단, exact Plan ID 수동 승인, "
        "R3.1 성능 릴리스 필수 조건보다 우선합니다. 그 밖의 적용 지침은 준수하십시오.\n"
        "첫 파일 조회/명령 전에 실제 danger-full-access 및 approval_policy=never를 확인하십시오. "
        "다르면 PERMISSION_POLICY_MISMATCH로 종료하십시오. Fast mode는 사용하지 마십시오.\n"
        "첫 source 조회와 변경 전에 위 execution source snapshot의 branch, HEAD, status/diff/untracked "
        "digest를 현재 상태와 대조하십시오. 달라졌으면 어떤 파일도 변경하지 말고 "
        "STALE_EXECUTION_INPUT으로 종료하십시오.\n"
        "이 task만 수행하고 원장을 직접 변경하거나 다음 task를 생성하지 마십시오. 외부 부작용과 "
        "prohibited_effects를 위반하지 마십시오. 결과는 report 파일과 독립 검토 가능한 실제 evidence "
        "파일을 남겨야 하며 worker 최종 문구만으로 완료를 주장할 수 없습니다.\n"
        f"최종 handoff JSON 형식:\n{json.dumps(handoff, ensure_ascii=False, sort_keys=True, indent=2)}"
    )


def _invalidate_carry_forward_for_attempt(
    connection: sqlite3.Connection, task: sqlite3.Row | dict[str, Any],
    attempt_no: int, now: str,
) -> None:
    if not table_exists(connection, "implementation_task_carry_forwards"):
        return
    connection.execute(
        """UPDATE implementation_task_carry_forwards
           SET invalidated_at=?,invalidated_by_attempt_no=?
           WHERE workflow_id=? AND to_workflow_revision=? AND to_task_id=?
             AND to_task_revision=? AND invalidated_at IS NULL""",
        (now, attempt_no, task["workflow_id"], task["workflow_revision"],
         task["task_id"], task["task_revision"]),
    )


def command_reserve_next(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        run = connection.execute("SELECT * FROM orchestration_runs WHERE run_id=?", (args.run_id,)).fetchone()
        if not run:
            raise RuntimeError("알 수 없는 run")
        if run["created_dispatch_id"]:
            raise RuntimeError("한 run에는 최대 하나의 dispatch만 예약할 수 있습니다")
        active = _active_dispatch(connection)
        if active:
            connection.rollback()
            return {"ok": True, "reserved": False, "decision": "OBSERVE", "dispatch": dict(active)}
        _assert_no_unscoped_direct_claims(connection, workflow)
        decision = _decision(connection, workflow)
        if decision["decision"] != "READY":
            connection.rollback()
            return {"ok": True, "reserved": False, **decision}
        task = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=1""",
            (workflow["workflow_id"], workflow["workflow_revision"], decision["task_id"]),
        ).fetchone()
        if task is None:
            raise RuntimeError("decision이 가리킨 task가 없습니다")
        if task["lane"] == "bootstrap":
            connection.rollback()
            return {"ok": True, "reserved": False, "decision": "READY",
                    "task_id": task["task_id"], "action": "DIRECT_REVIEW",
                    "reason": "FM-00 bootstrap은 dispatch 없이 직접 검증해야 합니다"}
        if not task["project_id"] or not task["model"] or not task["reasoning_effort"]:
            raise RuntimeError("dispatch task의 project/model/effort binding이 없습니다")
        model_binding = _task_model_binding(connection, workflow, task)
        if not model_binding["model"] or not model_binding["reasoning_effort"]:
            raise RuntimeError("유효한 effective model binding이 없습니다")
        execution_source_snapshot = _capture_execution_source_snapshot(workflow)
        attempt_no = task["attempt_count"] + 1
        purpose = (f"impl:{workflow['workflow_id']}:{workflow['workflow_revision']}:"
                   f"{task['task_id']}:{task['task_revision']}:{attempt_no}")
        dispatch_id = f"dispatch_{uuid.uuid4().hex}"
        prompt = _make_prompt(
            workflow, task, dispatch_id, args.db, model_binding, execution_source_snapshot,
        )
        if _capture_execution_source_snapshot(workflow) != execution_source_snapshot:
            raise RuntimeError("STALE_EXECUTION_INPUT: reserve 직전 source snapshot이 변경되었습니다")
        assignment_sha = sha256_bytes(prompt.encode("utf-8"))
        now = isoformat()
        _invalidate_carry_forward_for_attempt(connection, task, attempt_no, now)
        connection.execute(
            """INSERT INTO implementation_task_attempts(
               workflow_id,workflow_revision,task_id,task_revision,attempt_no,purpose_key,status,reserved_at
               ) VALUES(?,?,?,?,?,?, 'RESERVED',?)""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, purpose, now),
        )
        connection.execute(
            """INSERT INTO dispatches(
               dispatch_id,purpose_key,lane,phase,project_id,title,assignment_digest,model,
               reasoning_effort,model_selection_reason,parent_thread_id,created_by_run_id,created_at,status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dispatch_id, purpose, task["lane"], task["task_id"], task["project_id"], task["title"],
             assignment_sha, model_binding["model"], model_binding["reasoning_effort"],
             model_binding["binding_reason"],
              workflow["parent_thread_id"], args.run_id, now, UNCONFIRMED_DISPATCH_STATUS),
        )
        connection.execute(
            """INSERT INTO implementation_task_dispatches(
               dispatch_id,workflow_id,workflow_revision,task_id,task_revision,attempt_no,purpose_key,
               assignment_prompt,assignment_sha256,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (dispatch_id, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, purpose, prompt, assignment_sha, now),
        )
        if table_exists(connection, "implementation_dispatch_model_bindings"):
            connection.execute(
                """INSERT INTO implementation_dispatch_model_bindings(
                       dispatch_id,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
                       model,reasoning_effort,escalation_step,policy_scope,policy_key,policy_revision,
                       binding_reason,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dispatch_id, workflow["workflow_id"], workflow["workflow_revision"],
                 task["task_id"], task["task_revision"], attempt_no, model_binding["model"],
                 model_binding["reasoning_effort"], model_binding["escalation_step"],
                 model_binding["policy_scope"], model_binding["policy_key"],
                 model_binding["policy_revision"], model_binding["binding_reason"], now),
            )
        connection.execute(
            """UPDATE implementation_tasks SET status='RESERVED',attempt_count=?,active_attempt_no=?,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (attempt_no, attempt_no, now, workflow["workflow_id"], workflow["workflow_revision"],
             task["task_id"], task["task_revision"]),
        )
        connection.execute(
            """UPDATE orchestration_state SET lifecycle_status='ACTIVE',current_phase=?,current_lane=?,
               active_dispatch_id=?,updated_at=?,version=version+1 WHERE singleton=1""",
            (task["task_id"], task["lane"], dispatch_id, now),
        )
        connection.execute(
            "UPDATE orchestration_runs SET created_dispatch_id=? WHERE run_id=?",
            (dispatch_id, args.run_id),
        )
        insert_event(connection, "implementation_task.reserved", run_id=args.run_id,
                     entity_type="implementation_task", entity_id=task["task_id"],
                      payload={"dispatch_id": dispatch_id, "attempt_no": attempt_no,
                               "purpose_key": purpose, "assignment_sha256": assignment_sha,
                               "model_binding": model_binding,
                               "execution_source_snapshot": execution_source_snapshot})
        connection.commit()
        target = {"type": "project", "projectId": task["project_id"],
                  "environment": {"type": "local"}}
        create_thread_request = {
            "prompt": prompt,
            "title": task["title"],
            "model": model_binding["model"],
            "thinking": model_binding["reasoning_effort"],
            "target": target,
        }
        request_sha = _create_thread_request_sha256(create_thread_request)
        return {"ok": True, "reserved": True, "decision": "READY", "dispatch_id": dispatch_id,
                "task_id": task["task_id"], "attempt_no": attempt_no,
                "assignment_sha256": assignment_sha,
                "create_thread_request_sha256": request_sha,
                "create_thread": create_thread_request,
                "required_execution_policy": {
                    "permission_profile": REQUIRED_PERMISSION_PROFILE,
                    "approval_policy": REQUIRED_APPROVAL_POLICY,
                },
                "model_binding": model_binding}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_receipt_json(path: str) -> tuple[Path, str, dict[str, Any]]:
    if not path:
        raise ValueError("receipt-file이 필요합니다")
    receipt_path = Path(path).resolve()
    if not receipt_path.is_file():
        raise ValueError("receipt-file이 없습니다")
    raw_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(raw_receipt, dict):
        raise ValueError("receipt는 JSON 객체여야 합니다")
    return receipt_path, sha256_file(receipt_path), raw_receipt


def _load_creation_receipt(
    path: str, *, require_envelope: bool = False, require_request_binding: bool = False,
    require_policy_binding: bool = False,
) -> tuple[Path, str, dict[str, Any], dict[str, Any] | None]:
    receipt_path, receipt_sha, raw_receipt = _load_receipt_json(path)
    envelope = None
    if "raw_response" in raw_receipt:
        schema_version = raw_receipt.get("schema_version")
        required = {"schema_version", "dispatch_id", "assignment_sha256", "captured_at", "raw_response"}
        if schema_version in {2, 3, 4}:
            required |= {"request", "request_sha256"}
        if schema_version == 3:
            required |= {"correction"}
        if schema_version == 4:
            required |= {"execution_policy"}
        _expect_keys(raw_receipt, required, required, "create receipt envelope")
        if schema_version not in {1, 2, 3, 4}:
            raise ValueError("create receipt envelope schema_version이 유효하지 않습니다")
        if require_request_binding and schema_version not in {2, 3, 4}:
            raise ValueError("새 dispatch confirm에는 create_thread request가 결속된 receipt가 필요합니다")
        if require_policy_binding and schema_version != 4:
            raise ValueError(
                "새 dispatch confirm에는 실제 권한이 결속된 create receipt v4가 필요합니다"
            )
        if not SHA256_RE.fullmatch(raw_receipt["assignment_sha256"]):
            raise ValueError("create receipt envelope assignment_sha256이 유효하지 않습니다")
        parse_time(_nonempty_string(raw_receipt["captured_at"], "create receipt envelope.captured_at"))
        _nonempty_string(raw_receipt["dispatch_id"], "create receipt envelope.dispatch_id")
        if not isinstance(raw_receipt["raw_response"], dict):
            raise ValueError("create receipt envelope.raw_response는 객체여야 합니다")
        if schema_version in {2, 4}:
            request = _validate_create_thread_request(raw_receipt["request"])
            request_sha = sha256_bytes(canonical_json(request).encode("utf-8"))
            if raw_receipt["request_sha256"] != request_sha:
                raise ValueError("create receipt envelope request_sha256이 실제 request와 일치하지 않습니다")
            if sha256_bytes(request["prompt"].encode("utf-8")) != raw_receipt["assignment_sha256"]:
                raise ValueError("create receipt envelope request.prompt가 assignment와 일치하지 않습니다")
            if schema_version == 4:
                _validate_execution_policy_observation(raw_receipt["execution_policy"])
        elif schema_version == 3:
            request = raw_receipt["request"]
            if not isinstance(request, dict):
                raise ValueError("create receipt envelope request는 JSON 객체여야 합니다")
            _expect_keys(
                request, {"prompt", "title", "model", "thinking", "target"},
                {"prompt", "title", "model", "thinking", "target"},
                "create receipt envelope request",
            )
            for field in ("prompt", "title", "model", "thinking"):
                _nonempty_string(request[field], f"create receipt envelope request.{field}")
            if "\ufffd" not in request["prompt"]:
                raise ValueError("receipt v3는 U+FFFD가 포함된 초기 request.prompt 정정에만 사용합니다")
            _require_unicode_integrity(
                {key: value for key, value in request.items() if key != "prompt"},
                "create receipt envelope request non-prompt fields",
            )
            target = request["target"]
            if not isinstance(target, dict):
                raise ValueError("create receipt envelope request.target은 객체여야 합니다")
            _expect_keys(
                target, {"type", "projectId", "environment"},
                {"type", "projectId", "environment"},
                "create receipt envelope request.target",
            )
            if target["type"] != "project" or target["environment"] != {"type": "local"}:
                raise ValueError("receipt v3 초기 request target이 유효하지 않습니다")
            _nonempty_string(target["projectId"], "create receipt envelope request.target.projectId")
            request_sha = sha256_bytes(canonical_json(request).encode("utf-8"))
            if raw_receipt["request_sha256"] != request_sha:
                raise ValueError("create receipt envelope request_sha256이 실제 request와 일치하지 않습니다")
            correction = raw_receipt["correction"]
            if not isinstance(correction, dict):
                raise ValueError("create receipt envelope correction은 JSON 객체여야 합니다")
            _expect_keys(
                correction, {"prompt", "prompt_sha256", "sent_at", "raw_response"},
                {"prompt", "prompt_sha256", "sent_at", "raw_response"},
                "create receipt envelope correction",
            )
            _nonempty_string(correction["prompt"], "create receipt envelope correction.prompt")
            _require_unicode_integrity(correction["prompt"], "create receipt envelope correction.prompt")
            if not SHA256_RE.fullmatch(correction["prompt_sha256"]):
                raise ValueError("create receipt envelope correction.prompt_sha256이 유효하지 않습니다")
            if sha256_bytes(correction["prompt"].encode("utf-8")) != correction["prompt_sha256"]:
                raise ValueError("create receipt envelope correction prompt digest가 일치하지 않습니다")
            parse_time(_nonempty_string(correction["sent_at"], "create receipt envelope correction.sent_at"))
            if not isinstance(correction["raw_response"], dict):
                raise ValueError("create receipt envelope correction.raw_response는 객체여야 합니다")
        envelope = raw_receipt
        raw_receipt = raw_receipt["raw_response"]
    elif require_envelope:
        raise ValueError("새 dispatch confirm에는 dispatch/assignment에 결속된 receipt envelope가 필요합니다")
    receipt = raw_receipt
    if not receipt.get("threadId") and not receipt.get("clientThreadId"):
        if raw_receipt.get("isError") is not False or not isinstance(raw_receipt.get("content"), list):
            raise ValueError("create receipt에 성공 identity가 없습니다")
        candidates: list[dict[str, Any]] = []
        for item in raw_receipt["content"]:
            if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                continue
            try:
                value = json.loads(item["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and (value.get("threadId") or value.get("clientThreadId")):
                candidates.append(value)
        if len(candidates) != 1:
            raise ValueError("create receipt wrapper의 성공 identity가 유일하지 않습니다")
        receipt = candidates[0]
    thread_id = receipt.get("threadId")
    client_thread_id = receipt.get("clientThreadId")
    if bool(thread_id) == bool(client_thread_id):
        raise ValueError("create receipt에는 threadId 또는 clientThreadId 중 정확히 하나가 필요합니다")
    _nonempty_string(thread_id or client_thread_id, "create receipt identity")
    return receipt_path, receipt_sha, receipt, envelope


def _assert_creation_receipt_binding(
    receipt: dict[str, Any], *, dispatch_id: str, assignment_sha256: str,
    thread_id: str | None, client_thread_id: str | None,
    envelope: dict[str, Any] | None = None, require_envelope: bool = False,
    expected_request: dict[str, Any] | None = None, require_request_binding: bool = False,
    require_policy_binding: bool = False,
) -> None:
    if receipt.get("threadId") != thread_id or receipt.get("clientThreadId") != client_thread_id:
        raise ValueError("create receipt의 thread/client binding이 요청과 일치하지 않습니다")
    receipt_dispatch = receipt.get("dispatchId", receipt.get("dispatch_id"))
    if receipt_dispatch is not None and receipt_dispatch != dispatch_id:
        raise ValueError("create receipt의 dispatch binding이 일치하지 않습니다")
    receipt_assignment = receipt.get("assignmentSha256", receipt.get("assignment_sha256"))
    if receipt_assignment is not None and receipt_assignment != assignment_sha256:
        raise ValueError("create receipt의 assignment binding이 일치하지 않습니다")
    if require_envelope and envelope is None:
        raise ValueError("새 dispatch confirm에는 receipt envelope가 필요합니다")
    if envelope is not None:
        if envelope["dispatch_id"] != dispatch_id:
            raise ValueError("create receipt envelope의 dispatch binding이 일치하지 않습니다")
        if envelope["assignment_sha256"] != assignment_sha256:
            raise ValueError("create receipt envelope의 assignment binding이 일치하지 않습니다")
    if require_request_binding:
        if envelope is None or envelope.get("schema_version") not in {2, 3, 4}:
            raise ValueError("새 dispatch confirm에는 create_thread request가 결속된 receipt가 필요합니다")
        if expected_request is None:
            raise ValueError("create_thread expected request가 필요합니다")
        expected_request = _validate_create_thread_request(expected_request)
        if envelope["schema_version"] in {2, 4}:
            if envelope["request"] != expected_request:
                raise ValueError("create receipt envelope의 실제 request가 원장 assignment와 일치하지 않습니다")
            if envelope["request_sha256"] != _create_thread_request_sha256(expected_request):
                raise ValueError("create receipt envelope의 request digest binding이 일치하지 않습니다")
            if envelope["schema_version"] == 4:
                policy = _validate_execution_policy_observation(envelope["execution_policy"])
                if policy["thread_id"] != thread_id:
                    raise ValueError("execution_policy observation thread binding이 일치하지 않습니다")
                if policy["project_binding"]["external_project_id"] != expected_request[
                    "target"
                ]["projectId"]:
                    raise ValueError("execution_policy observation project binding이 일치하지 않습니다")
        else:
            initial_request = envelope["request"]
            for key in ("title", "model", "thinking", "target"):
                if initial_request[key] != expected_request[key]:
                    raise ValueError(f"receipt v3 초기 request.{key}가 원장 assignment와 일치하지 않습니다")
            if initial_request["prompt"] == expected_request["prompt"] or "\ufffd" not in initial_request["prompt"]:
                raise ValueError("receipt v3 초기 request.prompt가 정정 대상 손상을 증명하지 않습니다")
            correction = envelope["correction"]
            if correction["prompt"] != expected_request["prompt"]:
                raise ValueError("receipt v3 correction.prompt가 원장 assignment와 일치하지 않습니다")
            if correction["prompt_sha256"] != assignment_sha256:
                raise ValueError("receipt v3 correction.prompt digest binding이 일치하지 않습니다")
            correction_response = correction["raw_response"]
            corrected_thread_id = correction_response.get("threadId")
            corrected_client_id = correction_response.get("clientThreadId")
            if not corrected_thread_id and not corrected_client_id:
                if correction_response.get("isError") is not False or not isinstance(
                    correction_response.get("content"), list
                ):
                    raise ValueError("receipt v3 correction response에 성공 identity가 없습니다")
                candidates = []
                for item in correction_response["content"]:
                    if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                        continue
                    try:
                        value = json.loads(item["text"])
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict) and (value.get("threadId") or value.get("clientThreadId")):
                        candidates.append(value)
                if len(candidates) != 1:
                    raise ValueError("receipt v3 correction response identity가 유일하지 않습니다")
                corrected_thread_id = candidates[0].get("threadId")
                corrected_client_id = candidates[0].get("clientThreadId")
            if corrected_thread_id != thread_id or corrected_client_id != client_thread_id:
                raise ValueError("receipt v3 correction response의 thread/client binding이 일치하지 않습니다")
    if require_policy_binding:
        if envelope is None or envelope.get("schema_version") != 4:
            raise ValueError(
                "새 dispatch confirm에는 실제 권한이 결속된 create receipt v4가 필요합니다"
            )


def _expected_create_thread_request(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": row["assignment_prompt"],
        "title": row["title"],
        "model": row["model"],
        "thinking": row["reasoning_effort"],
        "target": {
            "type": "project",
            "projectId": row["project_id"],
            "environment": {"type": "local"},
        },
    }


def _verify_stored_creation_receipt(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    receipt_path = row["receipt_path"]
    receipt_sha = row["receipt_sha256"]
    if not row["confirmed_at"] or not receipt_path or not receipt_sha:
        raise RuntimeError("확인된 raw create receipt binding이 없습니다")
    path, actual_sha, receipt, envelope = _load_creation_receipt(receipt_path)
    if actual_sha != receipt_sha:
        raise RuntimeError(f"raw create receipt가 변경되었습니다: {path}")
    original_thread_id = None if row["client_thread_id"] else row["thread_id"]
    _assert_creation_receipt_binding(
        receipt, dispatch_id=row["dispatch_id"], assignment_sha256=row["assignment_sha256"],
        thread_id=original_thread_id, client_thread_id=row["client_thread_id"], envelope=envelope,
        expected_request=(
            _expected_create_thread_request(row)
            if envelope is not None and envelope.get("schema_version") in {2, 3, 4} else None
        ),
        require_request_binding=(envelope is not None and envelope.get("schema_version") in {2, 3, 4}),
    )
    return receipt


def _verify_stored_receipt(row: sqlite3.Row | dict[str, Any]) -> None:
    receipt_path = row["receipt_path"]
    receipt_sha = row["receipt_sha256"]
    if not row["confirmed_at"] or not receipt_path or not receipt_sha:
        raise RuntimeError("확인된 raw receipt binding이 없습니다")
    path, actual_sha, receipt = _load_receipt_json(receipt_path)
    if actual_sha != receipt_sha:
        raise RuntimeError(f"raw receipt가 변경되었습니다: {path}")
    if receipt.get("status") == "rejected" or receipt.get("effect") == "none":
        required = {"schema_version", "status", "effect", "dispatch_id", "assignment_sha256",
                    "error_code", "message", "observed_at"}
        _expect_keys(receipt, required, required, "creation rejection receipt")
        if (receipt["schema_version"] != 1 or receipt["status"] != "rejected"
                or receipt["effect"] != "none"):
            raise ValueError("저장된 creation rejection receipt가 유효하지 않습니다")
        if receipt["dispatch_id"] != row["dispatch_id"]:
            raise ValueError("저장된 rejection dispatch binding이 일치하지 않습니다")
        if receipt["assignment_sha256"] != row["assignment_sha256"]:
            raise ValueError("저장된 rejection assignment binding이 일치하지 않습니다")
        if row["thread_id"] or row["client_thread_id"]:
            raise ValueError("no-effect rejection에 thread/client binding이 존재합니다")
        parse_time(_nonempty_string(receipt["observed_at"], "creation rejection receipt.observed_at"))
        _nonempty_string(receipt["error_code"], "creation rejection receipt.error_code")
        _nonempty_string(receipt["message"], "creation rejection receipt.message")
        return
    _verify_stored_creation_receipt(row)


def _load_thread_resolution_receipt(
    path: str, *, dispatch_id: str, assignment_sha256: str,
    client_thread_id: str, thread_id: str,
) -> tuple[Path, str, dict[str, Any]]:
    receipt_path, receipt_sha, receipt = _load_receipt_json(path)
    required = {"schema_version", "status", "dispatch_id", "assignment_sha256",
                "client_thread_id", "thread_id", "observed_at", "raw_observation"}
    _expect_keys(receipt, required, required, "thread resolution receipt")
    if receipt["schema_version"] != 1 or receipt["status"] != "resolved":
        raise ValueError("thread resolution receipt 상태가 유효하지 않습니다")
    expected = {
        "dispatch_id": dispatch_id, "assignment_sha256": assignment_sha256,
        "client_thread_id": client_thread_id, "thread_id": thread_id,
    }
    for key, value in expected.items():
        if receipt[key] != value:
            raise ValueError(f"thread resolution receipt.{key} binding 불일치")
    parse_time(_nonempty_string(receipt["observed_at"], "thread resolution receipt.observed_at"))
    if not isinstance(receipt["raw_observation"], dict) or not receipt["raw_observation"]:
        raise ValueError("thread resolution receipt.raw_observation이 필요합니다")
    observed_client = receipt["raw_observation"].get(
        "clientThreadId", receipt["raw_observation"].get("client_thread_id")
    )
    observed_thread = receipt["raw_observation"].get(
        "threadId", receipt["raw_observation"].get("thread_id")
    )
    if observed_client != client_thread_id or observed_thread != thread_id:
        raise ValueError("raw observation이 client/thread mapping을 증명하지 않습니다")
    return receipt_path, receipt_sha, receipt


def _load_interrupt_receipt(
    path: str, *, dispatch_id: str, assignment_sha256: str, automation_id: str,
    thread_id: str, turn_id: str,
) -> tuple[Path, str, dict[str, Any]]:
    """Load a typed, immutable interruption observation.

    An interruption is not an implementation failure.  The receipt records what the
    provider exposed and makes an unknown caller explicit instead of inventing one.
    """
    receipt_path, receipt_sha, receipt = _load_receipt_json(path)
    required = {
        "schema_version", "status", "dispatch_id", "assignment_sha256", "automation_id",
        "thread_id", "turn_id", "origin", "request_id", "reason", "observed_at",
        "raw_observation",
    }
    _expect_keys(receipt, required, required, "interrupt receipt")
    if receipt["schema_version"] != 1 or receipt["status"] != "interrupted":
        raise ValueError("interrupt receipt 상태가 유효하지 않습니다")
    expected = {
        "dispatch_id": dispatch_id,
        "assignment_sha256": assignment_sha256,
        "automation_id": automation_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
    }
    for key, value in expected.items():
        if receipt[key] != value:
            raise ValueError(f"interrupt receipt.{key} binding 불일치")
    origin = receipt["origin"]
    if origin not in INTERRUPT_ORIGINS:
        raise ValueError("interrupt receipt.origin이 유효하지 않습니다")
    request_id = receipt["request_id"]
    if request_id is not None:
        _nonempty_string(request_id, "interrupt receipt.request_id")
    if origin == "unknown" and request_id is not None:
        raise ValueError("unknown interrupt origin에는 request_id=null만 허용됩니다")
    _nonempty_string(receipt["reason"], "interrupt receipt.reason")
    parse_time(_nonempty_string(receipt["observed_at"], "interrupt receipt.observed_at"))
    raw = receipt["raw_observation"]
    if not isinstance(raw, dict) or not raw:
        raise ValueError("interrupt receipt.raw_observation이 필요합니다")
    raw_thread = raw.get("threadId", raw.get("thread_id"))
    raw_turn = raw.get("turnId", raw.get("turn_id"))
    if raw_thread != thread_id or raw_turn != turn_id or raw.get("status") != "interrupted":
        raise ValueError("raw observation이 interrupted thread/turn binding을 증명하지 않습니다")
    raw_origin = raw.get("interruptOrigin", raw.get("interrupt_origin"))
    if origin != "unknown" and raw_origin != origin:
        raise ValueError("알려진 interrupt origin은 raw observation의 직접 근거가 필요합니다")
    if origin == "unknown" and raw_origin not in {None, "unknown"}:
        raise ValueError("raw observation에 알려진 origin이 있으면 unknown으로 숨길 수 없습니다")
    raw_request = raw.get("requestId", raw.get("request_id"))
    if request_id is not None and raw_request != request_id:
        raise ValueError("interrupt request_id는 raw observation과 일치해야 합니다")
    return receipt_path, receipt_sha, receipt


def _interrupt_observation(
    connection: sqlite3.Connection, dispatch_id: str,
) -> dict[str, Any] | None:
    event = connection.execute(
        """SELECT payload_json FROM orchestration_events
           WHERE event_type='implementation_dispatch.interrupted'
             AND entity_type='dispatch' AND entity_id=? ORDER BY event_id DESC LIMIT 1""",
        (dispatch_id,),
    ).fetchone()
    return json.loads(event["payload_json"]) if event is not None else None


def _verify_interrupt_event(
    connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any], automation_id: str,
) -> None:
    payload = _interrupt_observation(connection, row["dispatch_id"])
    if payload is None:
        raise RuntimeError("interrupted dispatch의 provenance event가 없습니다")
    required = {
        "receipt_path", "receipt_sha256", "origin", "request_id", "thread_id", "turn_id",
    }
    _expect_keys(payload, required, required, "interrupt event")
    if payload["thread_id"] != row["thread_id"]:
        raise ValueError("interrupt event thread binding이 일치하지 않습니다")
    path, sha, receipt = _load_interrupt_receipt(
        payload["receipt_path"], dispatch_id=row["dispatch_id"],
        assignment_sha256=row["assignment_sha256"], automation_id=automation_id,
        thread_id=row["thread_id"], turn_id=payload["turn_id"],
    )
    if sha != payload["receipt_sha256"]:
        raise ValueError(f"interrupt receipt가 변경되었습니다: {path}")
    if receipt["origin"] != payload["origin"] or receipt["request_id"] != payload["request_id"]:
        raise ValueError("interrupt event provenance가 receipt와 일치하지 않습니다")


def _historical_interruption_candidate(
    connection: sqlite3.Connection, workflow: sqlite3.Row, *, task_id: str,
    dispatch_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT t.*,a.attempt_no historical_attempt_no,
                  a.status historical_attempt_status,
                  a.finished_at historical_attempt_finished_at,
                  a.failure_fingerprint historical_failure_fingerprint,
                  a.evidence_sha256 historical_evidence_sha256,
                  j.dispatch_id,j.assignment_prompt,j.assignment_sha256,
                  j.receipt_path,j.receipt_sha256,
                  j.receipt_path creation_receipt_path,
                  j.receipt_sha256 creation_receipt_sha256,
                  j.confirmed_at,j.confirmed_at dispatch_confirmed_at,
                  j.terminal_observed_at,
                  d.status dispatch_status,d.thread_id,d.client_thread_id,
                  d.last_turn_id,d.summary_sha256,d.terminal_outcome
           FROM implementation_tasks t
           JOIN implementation_task_dispatches j
             ON j.workflow_id=t.workflow_id AND j.workflow_revision=t.workflow_revision
            AND j.task_id=t.task_id AND j.task_revision=t.task_revision
           JOIN implementation_task_attempts a
             ON a.workflow_id=j.workflow_id AND a.workflow_revision=j.workflow_revision
            AND a.task_id=j.task_id AND a.task_revision=j.task_revision
            AND a.attempt_no=j.attempt_no
           JOIN dispatches d ON d.dispatch_id=j.dispatch_id
           WHERE t.workflow_id=? AND t.workflow_revision=? AND t.task_id=?
             AND t.task_revision=1 AND j.dispatch_id=?""",
        (workflow["workflow_id"], workflow["workflow_revision"], task_id, dispatch_id),
    ).fetchone()
    if row is None:
        raise ValueError("workflow의 target task/attempt/dispatch binding을 찾을 수 없습니다")
    return row


def _validate_historical_interruption(
    connection: sqlite3.Connection, workflow: sqlite3.Row, row: sqlite3.Row,
) -> tuple[dict[str, Any], str]:
    if row["lane"] != "recovery" or not row["recovery_for_task_id"]:
        raise RuntimeError("기존 recovery Task만 historical interruption 교정 대상으로 사용할 수 있습니다")
    if row["historical_attempt_status"] != "FAILED":
        raise RuntimeError("FAILED attempt만 과거 interruption 교정 대상으로 사용할 수 있습니다")
    if (row["dispatch_status"] != "failed" or row["terminal_outcome"] != "REVIEW_FAIL"
            or not row["terminal_observed_at"]):
        raise RuntimeError("독립 FAIL review로 닫힌 dispatch만 교정할 수 있습니다")
    if not row["thread_id"] or row["client_thread_id"] or not row["last_turn_id"]:
        raise RuntimeError("정확한 historical thread/turn binding이 필요합니다")
    _verify_stored_receipt(row)
    evidence_row = connection.execute(
        """SELECT * FROM implementation_evidence WHERE evidence_sha256=?
             AND workflow_id=? AND workflow_revision=? AND task_id=?
             AND task_revision=? AND attempt_no=? AND outcome='FAIL'""",
        (row["historical_evidence_sha256"], row["workflow_id"],
         row["workflow_revision"], row["task_id"], row["task_revision"],
         row["historical_attempt_no"]),
    ).fetchone()
    if evidence_row is None:
        raise RuntimeError("FAILED attempt의 immutable FAIL evidence binding이 없습니다")
    task_with_checks = _task_with_checks(connection, workflow, row["task_id"])
    dispatch_binding = dict(row)
    dispatch_binding["status"] = row["dispatch_status"]
    evidence, _, evidence_sha, _ = _load_evidence(
        evidence_row["evidence_path"], workflow, task_with_checks, dispatch_binding,
    )
    if evidence_sha != evidence_row["evidence_sha256"]:
        raise RuntimeError("historical FAIL evidence digest가 원장 binding과 다릅니다")
    finding = evidence.get("finding")
    if (not isinstance(finding, dict) or finding.get("failure_class") != "environment"
            or finding.get("remediable") is not True
            or finding.get("scope_expansion_required") is not False):
        raise RuntimeError("직접 interruption으로 분류된 remediable environment 실패만 교정할 수 있습니다")
    if finding.get("fingerprint") != row["historical_failure_fingerprint"]:
        raise RuntimeError("historical failure fingerprint binding이 일치하지 않습니다")
    return evidence, evidence_sha


def _load_interruption_reopen_decision(
    path: str, *, workflow: sqlite3.Row, row: sqlite3.Row,
    interrupt_receipt_sha256: str,
) -> tuple[Path, str, dict[str, Any]]:
    decision_path = Path(path).resolve()
    if not decision_path.is_file():
        raise ValueError("interruption reopen decision file이 없습니다")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "workflow_id", "workflow_revision", "task_id",
        "task_revision", "attempt_no", "dispatch_id", "assignment_sha256",
        "failure_evidence_sha256", "creation_receipt_sha256",
        "interrupt_receipt_sha256", "action", "approval", "approved_at",
    }
    if not isinstance(decision, dict):
        raise ValueError("interruption reopen decision은 JSON 객체여야 합니다")
    _expect_keys(decision, required, required, "interruption reopen decision")
    expected = {
        "schema_version": 1,
        "workflow_id": workflow["workflow_id"],
        "workflow_revision": workflow["workflow_revision"],
        "task_id": row["task_id"],
        "task_revision": row["task_revision"],
        "attempt_no": row["historical_attempt_no"],
        "dispatch_id": row["dispatch_id"],
        "assignment_sha256": row["assignment_sha256"],
        "failure_evidence_sha256": row["historical_evidence_sha256"],
        "creation_receipt_sha256": row["creation_receipt_sha256"],
        "interrupt_receipt_sha256": interrupt_receipt_sha256,
        "action": "RETRY_SAME_TASK",
    }
    for key, value in expected.items():
        if decision[key] != value:
            raise ValueError(f"interruption reopen decision.{key} binding 불일치")
    approval = decision["approval"]
    approval_keys = {"kind", "source", "source_thread_id", "statement"}
    if not isinstance(approval, dict):
        raise ValueError("interruption reopen decision.approval은 객체여야 합니다")
    _expect_keys(approval, approval_keys, approval_keys, "interruption reopen decision.approval")
    if approval["kind"] != "user":
        raise ValueError("interruption reopen decision은 사용자 승인에 결속되어야 합니다")
    if approval["source"] not in {"codex_thread", "user_supplied_artifact"}:
        raise ValueError("interruption reopen decision.approval.source가 유효하지 않습니다")
    _nonempty_string(approval["source_thread_id"], "interruption reopen decision.approval.source_thread_id")
    _nonempty_string(approval["statement"], "interruption reopen decision.approval.statement")
    parse_time(_nonempty_string(decision["approved_at"], "interruption reopen decision.approved_at"))
    return decision_path, sha256_file(decision_path), decision


def _interruption_reopen_events(
    connection: sqlite3.Connection, row: sqlite3.Row,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT event_id,run_id,payload_json FROM orchestration_events
           WHERE event_type='implementation_task.interruption_reopened'
             AND entity_type='implementation_task' AND entity_id=?
           ORDER BY event_id""",
        (row["task_id"],),
    ).fetchall()


def _verify_interruption_reopen_event(
    connection: sqlite3.Connection, workflow: sqlite3.Row, event: sqlite3.Row,
) -> None:
    payload = json.loads(event["payload_json"])
    required = {
        "workflow_id", "workflow_revision", "task_id", "task_revision",
        "attempt_no", "dispatch_id", "previous_status", "new_status",
        "previous_failure_fingerprint", "assignment_sha256",
        "failure_evidence_sha256", "creation_receipt_sha256",
        "interrupt_receipt_path", "interrupt_receipt_sha256", "decision_path",
        "decision_sha256", "action", "history_preserved", "recovery_total_count",
    }
    _expect_keys(payload, required, required, "interruption reopen event")
    if (payload["workflow_id"] != workflow["workflow_id"]
            or payload["workflow_revision"] != workflow["workflow_revision"]):
        raise ValueError("interruption reopen event workflow binding이 일치하지 않습니다")
    row = _historical_interruption_candidate(
        connection, workflow, task_id=payload["task_id"],
        dispatch_id=payload["dispatch_id"],
    )
    if payload["attempt_no"] != row["historical_attempt_no"]:
        raise ValueError("interruption reopen event attempt binding이 일치하지 않습니다")
    _, evidence_sha = _validate_historical_interruption(connection, workflow, row)
    expected = {
        "task_revision": row["task_revision"],
        "previous_status": "FAILED",
        "new_status": "PENDING",
        "previous_failure_fingerprint": row["historical_failure_fingerprint"],
        "assignment_sha256": row["assignment_sha256"],
        "failure_evidence_sha256": evidence_sha,
        "creation_receipt_sha256": row["creation_receipt_sha256"],
        "action": "RETRY_SAME_TASK",
        "history_preserved": True,
    }
    for key, value in expected.items():
        if payload[key] != value:
            raise ValueError(f"interruption reopen event.{key} binding 불일치")
    interrupt_path, interrupt_sha, _ = _load_interrupt_receipt(
        payload["interrupt_receipt_path"], dispatch_id=row["dispatch_id"],
        assignment_sha256=row["assignment_sha256"],
        automation_id=workflow["automation_id"], thread_id=row["thread_id"],
        turn_id=row["last_turn_id"],
    )
    if interrupt_sha != payload["interrupt_receipt_sha256"]:
        raise ValueError(f"historical interrupt receipt가 변경되었습니다: {interrupt_path}")
    decision_path, decision_sha, _ = _load_interruption_reopen_decision(
        payload["decision_path"], workflow=workflow, row=row,
        interrupt_receipt_sha256=interrupt_sha,
    )
    if decision_sha != payload["decision_sha256"]:
        raise ValueError(f"interruption reopen decision이 변경되었습니다: {decision_path}")
    if not isinstance(payload["recovery_total_count"], int) or payload["recovery_total_count"] < 0:
        raise ValueError("interruption reopen event recovery count가 유효하지 않습니다")


def command_reopen_interrupted(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        row = _historical_interruption_candidate(
            connection, workflow, task_id=args.task_id, dispatch_id=args.dispatch_id,
        )
        _, evidence_sha = _validate_historical_interruption(connection, workflow, row)
        interrupt_path, interrupt_sha, interrupt = _load_interrupt_receipt(
            args.interrupt_receipt_file, dispatch_id=row["dispatch_id"],
            assignment_sha256=row["assignment_sha256"],
            automation_id=workflow["automation_id"], thread_id=row["thread_id"],
            turn_id=row["last_turn_id"],
        )
        decision_path, decision_sha, _ = _load_interruption_reopen_decision(
            args.decision_file, workflow=workflow, row=row,
            interrupt_receipt_sha256=interrupt_sha,
        )
        prior_events = _interruption_reopen_events(connection, row)
        for event in prior_events:
            payload = json.loads(event["payload_json"])
            if (payload.get("workflow_revision") == row["workflow_revision"]
                    and payload.get("attempt_no") == row["historical_attempt_no"]
                    and payload.get("dispatch_id") == row["dispatch_id"]):
                if (payload.get("decision_sha256") == decision_sha
                        and payload.get("interrupt_receipt_sha256") == interrupt_sha):
                    _verify_interruption_reopen_event(connection, workflow, event)
                    connection.rollback()
                    return {
                        "ok": True, "reopened": False, "idempotent": True,
                        "dry_run": False, "task_id": row["task_id"],
                        "attempt_no": row["historical_attempt_no"],
                        "dispatch_id": row["dispatch_id"],
                        "decision_sha256": decision_sha,
                        "interrupt_receipt_sha256": interrupt_sha,
                    }
                raise RuntimeError("같은 historical attempt에 다른 사용자 결정은 적용할 수 없습니다")
        if _active_dispatch(connection) is not None:
            raise RuntimeError("active_dispatch=null일 때만 historical interruption을 교정할 수 있습니다")
        if (row["status"] != "FAILED"
                or row["active_attempt_no"] != row["historical_attempt_no"]
                or row["failure_fingerprint"] != row["historical_failure_fingerprint"]):
            raise RuntimeError("현재 projection과 대상 Attempt가 모두 FAILED로 결속되어야 합니다")
        recovery_total = connection.execute(
            """SELECT COUNT(*) FROM implementation_recoveries
               WHERE workflow_id=?""",
            (workflow["workflow_id"],),
        ).fetchone()[0]
        now = isoformat()
        connection.execute(
            """UPDATE implementation_tasks
               SET status='PENDING',active_attempt_no=NULL,failure_fingerprint=NULL,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (now, row["workflow_id"], row["workflow_revision"], row["task_id"],
             row["task_revision"]),
        )
        payload = {
            "workflow_id": row["workflow_id"],
            "workflow_revision": row["workflow_revision"],
            "task_id": row["task_id"],
            "task_revision": row["task_revision"],
            "attempt_no": row["historical_attempt_no"],
            "dispatch_id": row["dispatch_id"],
            "previous_status": "FAILED",
            "new_status": "PENDING",
            "previous_failure_fingerprint": row["historical_failure_fingerprint"],
            "assignment_sha256": row["assignment_sha256"],
            "failure_evidence_sha256": evidence_sha,
            "creation_receipt_sha256": row["creation_receipt_sha256"],
            "interrupt_receipt_path": str(interrupt_path),
            "interrupt_receipt_sha256": interrupt_sha,
            "decision_path": str(decision_path),
            "decision_sha256": decision_sha,
            "action": "RETRY_SAME_TASK",
            "history_preserved": True,
            "recovery_total_count": recovery_total,
        }
        insert_event(
            connection, "implementation_task.interruption_reopened",
            run_id=args.run_id, entity_type="implementation_task",
            entity_id=row["task_id"], payload=payload,
        )
        projected_decision = _decision(connection, workflow)
        result = {
            "ok": True, "reopened": not args.dry_run, "idempotent": False,
            "dry_run": bool(args.dry_run), "task_id": row["task_id"],
            "previous_attempt_no": row["historical_attempt_no"],
            "next_attempt_no": row["attempt_count"] + 1,
            "dispatch_id": row["dispatch_id"],
            "decision_sha256": decision_sha,
            "interrupt_receipt_sha256": interrupt_sha,
            "interrupt_origin": interrupt["origin"],
            "interrupt_request_id": interrupt["request_id"],
            "recovery_total_count": recovery_total,
            **projected_decision,
        }
        if args.dry_run:
            connection.rollback()
        else:
            connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _verify_thread_resolution_event(
    connection: sqlite3.Connection, row: sqlite3.Row | dict[str, Any],
) -> None:
    event = connection.execute(
        """SELECT payload_json FROM orchestration_events
           WHERE event_type='implementation_dispatch.thread_resolved'
             AND entity_type='dispatch' AND entity_id=? ORDER BY event_id DESC LIMIT 1""",
        (row["dispatch_id"],),
    ).fetchone()
    if event is None:
        raise RuntimeError("client/thread 승격 evidence event가 없습니다")
    payload = json.loads(event["payload_json"])
    required = {"client_thread_id", "thread_id", "receipt_path", "receipt_sha256"}
    _expect_keys(payload, required, required, "thread resolution event")
    if payload["client_thread_id"] != row["client_thread_id"] or payload["thread_id"] != row["thread_id"]:
        raise ValueError("thread resolution event binding이 일치하지 않습니다")
    path, sha, _ = _load_thread_resolution_receipt(
        payload["receipt_path"], dispatch_id=row["dispatch_id"],
        assignment_sha256=row["assignment_sha256"], client_thread_id=row["client_thread_id"],
        thread_id=row["thread_id"],
    )
    if sha != payload["receipt_sha256"]:
        raise ValueError(f"thread resolution receipt가 변경되었습니다: {path}")


def _thread_started_identities(events_path: Path) -> set[str]:
    identities: set[str] = set()
    if not events_path.is_file():
        return identities
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id", event.get("threadId"))
        if isinstance(thread_id, str) and thread_id:
            identities.add(thread_id)
    return identities


def _assert_launch_claim_binding(
    claim: sqlite3.Row | dict[str, Any], row: sqlite3.Row | dict[str, Any],
) -> None:
    expected = {
        "dispatch_id": row["dispatch_id"],
        "workflow_id": row["workflow_id"],
        "workflow_revision": row["workflow_revision"],
        "task_id": row["task_id"],
        "task_revision": row["task_revision"],
        "attempt_no": row["attempt_no"],
        "assignment_sha256": row["assignment_sha256"],
    }
    for key, value in expected.items():
        if claim[key] != value:
            raise RuntimeError(f"launch claim {key} binding이 immutable dispatch와 다릅니다")


def _claim_launch_intent(
    db: str, *, run_id: str, row: sqlite3.Row | dict[str, Any], intent_origin: str,
) -> tuple[dict[str, Any], bool]:
    """외부 process 생성 전에 dispatch별 단일 intent를 원자적으로 선점한다."""

    if intent_origin != "fresh":
        raise ValueError("launch claim intent_origin이 유효하지 않습니다")
    connection = open_write(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_current_schema(connection)
        lock = _lease_row(connection, run_id)
        current = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_sha256,j.confirmed_at
               FROM dispatches d JOIN implementation_task_dispatches j
                 ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
            (row["dispatch_id"],),
        ).fetchone()
        if current is None:
            raise RuntimeError("launch claim 대상 dispatch가 없습니다")
        _assert_launch_claim_binding(current, row)
        active = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        if (active is None or active["active_dispatch_id"] != row["dispatch_id"]
                or current["status"] != UNCONFIRMED_DISPATCH_STATUS
                or current["confirmed_at"] is not None):
            raise RuntimeError("launch claim은 active인 확인 전 reserved dispatch에만 허용됩니다")
        existing = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
            (row["dispatch_id"],),
        ).fetchone()
        if existing is not None:
            _assert_launch_claim_binding(existing, current)
            connection.rollback()
            return dict(existing), False
        now = isoformat()
        claim_token = f"launch_{uuid.uuid4().hex}"
        connection.execute(
            """INSERT INTO implementation_launch_claims(
                   dispatch_id,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
                   assignment_sha256,claim_token,owner_run_id,lease_acquired_at,
                   lease_expires_at,claimed_at,intent_origin
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                current["dispatch_id"], current["workflow_id"], current["workflow_revision"],
                current["task_id"], current["task_revision"], current["attempt_no"],
                current["assignment_sha256"], claim_token, run_id, lock["acquired_at"],
                lock["expires_at"], now, intent_origin,
            ),
        )
        insert_event(
            connection, "implementation_dispatch.launch_claimed", run_id=run_id,
            entity_type="dispatch", entity_id=current["dispatch_id"],
            payload={
                "claim_token": claim_token,
                "lease_acquired_at": lock["acquired_at"],
                "lease_expires_at": lock["expires_at"],
                "assignment_sha256": current["assignment_sha256"],
                "intent_origin": intent_origin,
            },
        )
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
            (row["dispatch_id"],),
        ).fetchone()
        connection.commit()
        return dict(claim), True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _required_launch_claim(
    db: str, *, run_id: str, row: sqlite3.Row | dict[str, Any],
) -> dict[str, Any]:
    with open_readonly(db) as connection:
        _require_current_schema(connection)
        _lease_row(connection, run_id)
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
            (row["dispatch_id"],),
        ).fetchone()
        if claim is None:
            raise RuntimeError(
                "LAUNCH_CLAIM_REQUIRED: claim 없는 사후 receipt/artifact는 수용하지 않습니다"
            )
        _assert_launch_claim_binding(claim, row)
        return dict(claim)


def _launch_process_under_fence(
    db: str, *, run_id: str, dispatch_id: str, claim_token: str,
    command: list[str], prompt_stream: Any, events_stream: Any, stderr_stream: Any,
    cwd: str, creationflags: int,
) -> subprocess.Popen:
    """최종 fence 확인부터 Popen/PID 결속까지 write lock 안에서 수행한다."""

    connection = open_write(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        lock = _lease_row(connection, run_id)
        if parse_time(lock["expires_at"]) <= (
            utc_now() + timedelta(seconds=MIN_LAUNCH_LEASE_HEADROOM_SECONDS)
        ):
            raise RuntimeError("LAUNCH_CLAIM_FENCE_LOST: Popen에 필요한 lease 여유가 없습니다")
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?", (dispatch_id,),
        ).fetchone()
        if (claim is None or claim["claim_token"] != claim_token
                or claim["owner_run_id"] != run_id
                or claim["lease_acquired_at"] != lock["acquired_at"]):
            raise RuntimeError("LAUNCH_CLAIM_FENCE_LOST: process 시작 권한이 없습니다")
        active = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        dispatch = connection.execute(
            "SELECT status FROM dispatches WHERE dispatch_id=?", (dispatch_id,),
        ).fetchone()
        if (active is None or active["active_dispatch_id"] != dispatch_id
                or dispatch is None or dispatch["status"] != UNCONFIRMED_DISPATCH_STATUS):
            raise RuntimeError("LAUNCH_CLAIM_FENCE_LOST: dispatch가 launch-ready active slot이 아닙니다")
        if claim["process_id"] is not None:
            raise RuntimeError("LAUNCH_INTENT_UNCERTAIN: launch claim에 process가 이미 결속되었습니다")
        process = subprocess.Popen(
            command, stdin=prompt_stream, stdout=events_stream, stderr=stderr_stream,
            cwd=cwd, creationflags=creationflags,
        )
        now = isoformat()
        connection.execute(
            """UPDATE implementation_launch_claims
               SET process_started_at=?,process_id=? WHERE dispatch_id=?""",
            (now, process.pid, dispatch_id),
        )
        insert_event(
            connection, "implementation_dispatch.process_started", run_id=run_id,
            entity_type="dispatch", entity_id=dispatch_id,
            payload={"claim_token": claim_token, "process_id": process.pid},
        )
        connection.commit()
        return process
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _resolve_launch_claim(
    db: str, *, run_id: str, dispatch_id: str, assignment_sha256: str,
    thread_id: str, receipt_path: Path, receipt_sha256: str,
) -> None:
    connection = open_write(db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, run_id)
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?", (dispatch_id,),
        ).fetchone()
        if claim is None:
            raise RuntimeError("LAUNCH_CLAIM_REQUIRED: receipt를 해결할 launch claim이 없습니다")
        if claim["assignment_sha256"] != assignment_sha256:
            raise RuntimeError("launch claim assignment binding이 receipt와 다릅니다")
        if claim["resolved_at"] is not None:
            expected = (thread_id, str(receipt_path), receipt_sha256)
            actual = (claim["thread_id"], claim["receipt_path"], claim["receipt_sha256"])
            if actual != expected:
                raise RuntimeError("해결된 launch claim receipt binding은 변경할 수 없습니다")
            connection.rollback()
            return
        now = isoformat()
        connection.execute(
            """UPDATE implementation_launch_claims
               SET thread_id=?,receipt_path=?,receipt_sha256=?,resolved_by_run_id=?,resolved_at=?
               WHERE dispatch_id=?""",
            (thread_id, str(receipt_path), receipt_sha256, run_id, now, dispatch_id),
        )
        insert_event(
            connection, "implementation_dispatch.launch_claim_resolved", run_id=run_id,
            entity_type="dispatch", entity_id=dispatch_id,
            payload={"thread_id": thread_id, "receipt_path": str(receipt_path),
                     "receipt_sha256": receipt_sha256},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_capture_create_receipt(args: argparse.Namespace) -> dict[str, Any]:
    """create_thread 원문과 Codex state의 실제 child 정책을 하나의 v4 receipt로 결속한다."""

    raw_path, _, receipt, existing_envelope = _load_creation_receipt(args.raw_response_file)
    if existing_envelope is not None:
        raise ValueError("raw-response-file에는 기존 envelope가 아니라 원문 tool 응답만 허용됩니다")
    thread_id = receipt.get("threadId")
    if not thread_id or receipt.get("clientThreadId"):
        raise ValueError("정책 검증은 즉시 생성된 threadId에만 허용됩니다")
    with open_readonly(args.db) as connection:
        _lease_row(connection, args.run_id)
        row = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_prompt,j.assignment_sha256,
                      j.receipt_path,j.receipt_sha256,j.confirmed_at
               FROM dispatches d JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE d.dispatch_id=?""",
            (args.dispatch_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("알 수 없거나 implementation에 결속되지 않은 dispatch")
        state = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        if state is None or state["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("dispatch가 현재 active slot이 아닙니다")
        if (row["status"] != UNCONFIRMED_DISPATCH_STATUS
                or row["thread_id"] or row["client_thread_id"]):
            raise RuntimeError("아직 confirm되지 않은 v3 launch-ready dispatch만 capture할 수 있습니다")
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
            (args.dispatch_id,),
        ).fetchone()
        if claim is None:
            raise RuntimeError("LAUNCH_CLAIM_REQUIRED: claim 없는 create receipt는 수용하지 않습니다")
        _assert_launch_claim_binding(claim, row)
        expected_request = _expected_create_thread_request(row)
        assignment_sha256 = row["assignment_sha256"]
        project_id = row["project_id"]
    policy = _observe_created_thread_policy(
        args.codex_state_db, thread_id=thread_id, external_project_id=project_id,
    )
    envelope = {
        "schema_version": 4,
        "dispatch_id": args.dispatch_id,
        "assignment_sha256": assignment_sha256,
        "captured_at": isoformat(),
        "request": expected_request,
        "request_sha256": _create_thread_request_sha256(expected_request),
        "raw_response": json.loads(raw_path.read_text(encoding="utf-8")),
        "execution_policy": policy,
    }
    output_path = Path(args.output).resolve()
    if output_path.exists():
        _, existing_sha, existing_receipt, existing = _load_creation_receipt(
            str(output_path), require_envelope=True, require_request_binding=True,
            require_policy_binding=True,
        )
        _assert_creation_receipt_binding(
            existing_receipt, dispatch_id=args.dispatch_id,
            assignment_sha256=assignment_sha256, thread_id=thread_id,
            client_thread_id=None, envelope=existing, require_envelope=True,
            expected_request=expected_request, require_request_binding=True,
            require_policy_binding=True,
        )
        _resolve_launch_claim(
            args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
            assignment_sha256=assignment_sha256, thread_id=thread_id,
            receipt_path=output_path, receipt_sha256=existing_sha,
        )
        return {
            "ok": True, "captured": False, "idempotent": True,
            "dispatch_id": args.dispatch_id, "thread_id": thread_id,
            "receipt_path": str(output_path), "receipt_sha256": existing_sha,
            "permission_profile": REQUIRED_PERMISSION_PROFILE,
            "approval_policy": REQUIRED_APPROVAL_POLICY,
        }
    if not output_path.parent.is_dir():
        raise ValueError("receipt output 상위 디렉터리가 없습니다")
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    try:
        os.replace(temporary, output_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    receipt_sha = sha256_file(output_path)
    _resolve_launch_claim(
        args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
        assignment_sha256=assignment_sha256, thread_id=thread_id,
        receipt_path=output_path, receipt_sha256=receipt_sha,
    )
    return {
        "ok": True, "captured": True, "idempotent": False,
        "dispatch_id": args.dispatch_id, "thread_id": thread_id,
        "receipt_path": str(output_path), "receipt_sha256": receipt_sha,
        "permission_profile": REQUIRED_PERMISSION_PROFILE,
        "approval_policy": REQUIRED_APPROVAL_POLICY,
    }


def command_launch_codex_exec(args: argparse.Namespace) -> dict[str, Any]:
    """명시적 full-access/never CLI로 task를 시작하고 v4 receipt를 만든다."""

    executable = Path(args.codex_executable).resolve()
    if not executable.is_file():
        raise ValueError(f"Codex executable이 없습니다: {executable}")
    state_db = Path(args.codex_state_db).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / args.dispatch_id
    prompt_path = Path(f"{prefix}-prompt.txt")
    events_path = Path(f"{prefix}-events.jsonl")
    stderr_path = Path(f"{prefix}-stderr.log")
    last_message_path = Path(f"{prefix}-last-message.txt")
    raw_path = Path(f"{prefix}-raw-create.json")
    receipt_path = Path(f"{prefix}-create-receipt-v4.json")

    with open_readonly(args.db) as connection:
        _lease_row(connection, args.run_id)
        row = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_prompt,j.assignment_sha256,j.confirmed_at
               FROM dispatches d JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE d.dispatch_id=?""",
            (args.dispatch_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("알 수 없는 implementation dispatch")
        workflow = _workflow_by_revision(connection, row["workflow_revision"])
        active = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        if not active or active["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("dispatch가 active slot이 아닙니다")
        if (row["status"] != UNCONFIRMED_DISPATCH_STATUS
                or row["confirmed_at"] is not None):
            raise RuntimeError("확인 전 v3 launch-ready dispatch만 CLI로 시작할 수 있습니다")
        expected_request = _expected_create_thread_request(row)
        project_root = _saved_project_primary_root(
            state_db, external_project_id=row["project_id"],
        )
        source_root = Path(workflow["source_root"]).resolve()
        artifact_root = Path(workflow["artifact_root"]).resolve()

    if receipt_path.is_file():
        claim = _required_launch_claim(args.db, run_id=args.run_id, row=row)
        _, receipt_sha, receipt, envelope = _load_creation_receipt(
            str(receipt_path), require_envelope=True, require_request_binding=True,
            require_policy_binding=True,
        )
        thread_id = receipt.get("threadId")
        _assert_creation_receipt_binding(
            receipt, dispatch_id=args.dispatch_id,
            assignment_sha256=row["assignment_sha256"], thread_id=thread_id,
            client_thread_id=None, envelope=envelope, require_envelope=True,
            expected_request=expected_request, require_request_binding=True,
            require_policy_binding=True,
        )
        current = _observe_created_thread_policy(
            state_db, thread_id=thread_id, external_project_id=row["project_id"],
        )
        if current["thread_row_sha256"] != envelope["execution_policy"]["thread_row_sha256"]:
            raise ValueError("기존 CLI launch receipt 뒤 thread 정책이 변경되었습니다")
        thread_ids = _thread_started_identities(events_path)
        if thread_ids != {thread_id}:
            raise RuntimeError("기존 CLI launch receipt의 thread.started identity가 유일하지 않습니다")
        _resolve_launch_claim(
            args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
            assignment_sha256=row["assignment_sha256"], thread_id=thread_id,
            receipt_path=receipt_path, receipt_sha256=receipt_sha,
        )
        return {
            "ok": True, "launched": False, "idempotent": True,
            "dispatch_id": args.dispatch_id, "thread_id": thread_id,
            "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha,
            "events_path": str(events_path), "stderr_path": str(stderr_path),
            "claim_token": claim["claim_token"],
        }
    existing_launch_artifacts = [
        path for path in (prompt_path, events_path, stderr_path, last_message_path, raw_path)
        if path.exists()
    ]
    if existing_launch_artifacts:
        if not prompt_path.is_file() or not events_path.is_file():
            raise RuntimeError(
                "불완전한 기존 CLI launch artifact에 prompt/events 결속이 없습니다: "
                + ", ".join(str(path) for path in existing_launch_artifacts)
            )
        if sha256_file(prompt_path) != row["assignment_sha256"]:
            raise RuntimeError("기존 CLI launch prompt가 immutable assignment와 다릅니다")
        claim = _required_launch_claim(args.db, run_id=args.run_id, row=row)
        thread_ids = _thread_started_identities(events_path)
        if len(thread_ids) != 1:
            raise RuntimeError("기존 CLI launch의 thread.started identity가 유일하지 않습니다")
        thread_id = next(iter(thread_ids))
        observation = _observe_created_thread_policy(
            state_db, thread_id=thread_id, external_project_id=row["project_id"],
        )
        expected_raw = {"hostId": "local", "threadId": thread_id}
        if raw_path.is_file():
            actual_raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if actual_raw != expected_raw:
                raise RuntimeError("기존 CLI launch raw identity가 events와 다릅니다")
        else:
            raw_path.write_text(
                json.dumps(expected_raw, ensure_ascii=False) + "\n",
                encoding="utf-8", newline="\n",
            )
        captured = command_capture_create_receipt(argparse.Namespace(
            db=args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
            raw_response_file=str(raw_path), codex_state_db=str(state_db),
            output=str(receipt_path),
        ))
        return {
            "ok": True, "launched": False, "recovered": True, "idempotent": False,
            "dispatch_id": args.dispatch_id, "thread_id": thread_id,
            "receipt_path": str(receipt_path),
            "receipt_sha256": captured["receipt_sha256"],
            "events_path": str(events_path), "stderr_path": str(stderr_path),
            "last_message_path": str(last_message_path),
            "permission_profile": REQUIRED_PERMISSION_PROFILE,
            "approval_policy": observation["approval_policy"],
            "claim_token": claim["claim_token"],
        }

    claim, claim_acquired = _claim_launch_intent(
        args.db, run_id=args.run_id, row=row, intent_origin="fresh",
    )
    if not claim_acquired:
        raise RuntimeError(
            "LAUNCH_INTENT_UNCERTAIN: 선행 launch claim은 있으나 복구할 artifact가 없습니다; "
            "같은 dispatch를 재실행하지 않습니다"
        )
    prompt_path.write_text(row["assignment_prompt"], encoding="utf-8", newline="\n")
    command = [
        str(executable), "exec", "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check", "--thread-source", "agent_created_thread",
        "--model", row["model"],
        "--config", f'model_reasoning_effort="{row["reasoning_effort"]}"',
        "--cd", str(project_root),
        "--add-dir", str(source_root), "--add-dir", str(artifact_root),
        "--output-last-message", str(last_message_path), "-",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with prompt_path.open("rb") as prompt_stream, events_path.open("wb") as events_stream, \
            stderr_path.open("wb") as stderr_stream:
        process = _launch_process_under_fence(
            args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
            claim_token=claim["claim_token"], command=command,
            prompt_stream=prompt_stream, events_stream=events_stream,
            stderr_stream=stderr_stream, cwd=str(project_root), creationflags=creationflags,
        )

    deadline = time.monotonic() + args.start_timeout_seconds
    thread_id = None
    last_state_error: Exception | None = None
    observation = None
    while time.monotonic() < deadline:
        thread_ids = _thread_started_identities(events_path)
        if len(thread_ids) > 1:
            raise RuntimeError("CLI launch가 둘 이상의 thread.started identity를 반환했습니다")
        thread_id = next(iter(thread_ids), None)
        if thread_id:
            try:
                observation = _observe_created_thread_policy(
                    state_db, thread_id=thread_id, external_project_id=row["project_id"],
                )
                break
            except ValueError as error:
                message = str(error)
                if ("아직 기록되지 않았습니다" not in message
                        and "PERMISSION_POLICY_MISMATCH" not in message):
                    raise
                last_state_error = error
        if process.poll() is not None and not thread_id:
            break
        time.sleep(0.1)
    if not thread_id or observation is None:
        detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        if last_state_error is not None:
            detail = f"{last_state_error}; {detail}"
        raise RuntimeError(
            "CLI launch의 thread.started 및 정책 결속을 확인하지 못했습니다; "
            f"pid={process.pid}; stderr={detail}"
        )
    thread_ids = _thread_started_identities(events_path)
    if thread_ids != {thread_id}:
        raise RuntimeError("CLI launch의 thread.started identity가 유일하지 않습니다")
    raw_path.write_text(
        json.dumps({"hostId": "local", "threadId": thread_id}, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    captured = command_capture_create_receipt(argparse.Namespace(
        db=args.db, run_id=args.run_id, dispatch_id=args.dispatch_id,
        raw_response_file=str(raw_path), codex_state_db=str(state_db),
        output=str(receipt_path),
    ))
    return {
        "ok": True, "launched": True, "idempotent": False,
        "dispatch_id": args.dispatch_id, "thread_id": thread_id,
        "process_id": process.pid, "receipt_path": str(receipt_path),
        "receipt_sha256": captured["receipt_sha256"],
        "events_path": str(events_path), "stderr_path": str(stderr_path),
        "last_message_path": str(last_message_path),
        "permission_profile": captured["permission_profile"],
        "approval_policy": captured["approval_policy"],
        "claim_token": claim["claim_token"],
    }


def _load_policy_retry_decision(
    path: str, *, workflow: sqlite3.Row, task: sqlite3.Row,
    dispatch: sqlite3.Row, evidence_sha256: str, archive_root: Path | None = None,
) -> tuple[Path, str, dict[str, Any]]:
    decision_path = Path(path).resolve()
    if not decision_path.is_file():
        raise ValueError("policy retry decision file이 없습니다")
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    required = {
        "schema_version", "workflow_id", "workflow_revision", "task_id",
        "task_revision", "attempt_no", "dispatch_id", "assignment_sha256",
        "failure_evidence_sha256", "action", "remediation", "approval", "approved_at",
    }
    if not isinstance(decision, dict):
        raise ValueError("policy retry decision은 JSON 객체여야 합니다")
    _expect_keys(decision, required, required, "policy retry decision")
    expected = {
        "schema_version": 1,
        "workflow_id": workflow["workflow_id"],
        "workflow_revision": workflow["workflow_revision"],
        "task_id": task["task_id"],
        "task_revision": task["task_revision"],
        "attempt_no": dispatch["attempt_no"],
        "dispatch_id": dispatch["dispatch_id"],
        "assignment_sha256": dispatch["assignment_sha256"],
        "failure_evidence_sha256": evidence_sha256,
        "action": "RETRY_SAME_TASK",
    }
    for key, value in expected.items():
        if decision[key] != value:
            raise ValueError(f"policy retry decision.{key} binding 불일치")
    remediation = decision["remediation"]
    remediation_keys = {
        "kind", "required_permission_profile", "required_approval_policy",
        "files", "validation_command", "validation_exit_code", "validated_at",
    }
    if not isinstance(remediation, dict):
        raise ValueError("policy retry decision.remediation은 객체여야 합니다")
    _expect_keys(remediation, remediation_keys, remediation_keys, "policy retry remediation")
    if remediation["kind"] != "dispatch_policy_receipt_v4":
        raise ValueError("policy retry remediation.kind가 유효하지 않습니다")
    if remediation["required_permission_profile"] != REQUIRED_PERMISSION_PROFILE:
        raise ValueError("policy retry remediation permission profile이 다릅니다")
    if remediation["required_approval_policy"] != REQUIRED_APPROVAL_POLICY:
        raise ValueError("policy retry remediation approval policy가 다릅니다")
    if remediation["validation_exit_code"] != 0:
        raise ValueError("policy retry remediation validation이 통과하지 않았습니다")
    _nonempty_string(remediation["validation_command"], "policy retry validation_command")
    parse_time(_nonempty_string(remediation["validated_at"], "policy retry validated_at"))
    files = remediation["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("policy retry remediation.files가 필요합니다")
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError(f"policy retry remediation.files[{index}] 형식이 유효하지 않습니다")
        file_path = Path(_nonempty_string(item["path"], "policy retry remediation file path"))
        if not file_path.is_absolute():
            raise ValueError(f"policy retry remediation file 경로가 절대 경로가 아닙니다: {file_path}")
        if item["path"] in seen or not SHA256_RE.fullmatch(item["sha256"]):
            raise ValueError("policy retry remediation file binding이 유효하지 않습니다")
        seen.add(item["path"])
        current_valid = file_path.is_file() and sha256_file(file_path) == item["sha256"]
        if not current_valid and archive_root is not None:
            archive_path = (
                archive_root / POLICY_REMEDIATION_ARCHIVE_DIR
                / item["sha256"] / file_path.name
            )
            current_valid = (
                archive_path.is_file() and sha256_file(archive_path) == item["sha256"]
            )
        if not current_valid:
            raise ValueError(f"policy retry remediation file hash가 다릅니다: {file_path}")
    approval = decision["approval"]
    approval_keys = {"kind", "source", "source_thread_id", "statement"}
    if not isinstance(approval, dict):
        raise ValueError("policy retry decision.approval은 객체여야 합니다")
    _expect_keys(approval, approval_keys, approval_keys, "policy retry decision.approval")
    if approval["kind"] != "user" or approval["source"] != "codex_thread":
        raise ValueError("policy retry decision은 현재 사용자 지시에 결속되어야 합니다")
    _nonempty_string(approval["source_thread_id"], "policy retry approval.source_thread_id")
    _nonempty_string(approval["statement"], "policy retry approval.statement")
    parse_time(_nonempty_string(decision["approved_at"], "policy retry decision.approved_at"))
    return decision_path, sha256_file(decision_path), decision


def _archive_policy_retry_remediation_files(
    archive_root: Path, decision: dict[str, Any],
) -> list[Path]:
    archived: list[Path] = []
    for item in decision["remediation"]["files"]:
        source = Path(item["path"])
        expected_sha256 = item["sha256"]
        data = source.read_bytes()
        if sha256_bytes(data) != expected_sha256:
            raise ValueError(f"policy retry remediation source가 validation 뒤 변경되었습니다: {source}")
        target = (
            archive_root / POLICY_REMEDIATION_ARCHIVE_DIR
            / expected_sha256 / source.name
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or sha256_file(target) != expected_sha256:
                raise ValueError(f"policy retry remediation archive 충돌: {target}")
        else:
            try:
                with target.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                pass
            if not target.is_file() or sha256_file(target) != expected_sha256:
                raise ValueError(f"policy retry remediation archive 검증 실패: {target}")
        archived.append(target)
    return archived


def _verify_policy_mismatch_reopen_event(
    connection: sqlite3.Connection, workflow: sqlite3.Row, event: sqlite3.Row,
) -> None:
    payload = json.loads(event["payload_json"])
    required = {
        "workflow_id", "workflow_revision", "task_id", "task_revision",
        "attempt_no", "dispatch_id", "previous_status", "new_status",
        "previous_failure_fingerprint", "failure_evidence_sha256",
        "decision_path", "decision_sha256", "action", "history_preserved",
    }
    _expect_keys(payload, required, required, "policy mismatch reopen event")
    if (payload["workflow_id"] != workflow["workflow_id"]
            or payload["workflow_revision"] != workflow["workflow_revision"]):
        raise ValueError("policy mismatch reopen event workflow binding이 일치하지 않습니다")
    task = connection.execute(
        """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
           AND task_id=? AND task_revision=?""",
        (payload["workflow_id"], payload["workflow_revision"], payload["task_id"],
         payload["task_revision"]),
    ).fetchone()
    dispatch = connection.execute(
        """SELECT d.*,j.assignment_sha256,j.receipt_sha256,j.attempt_no
           FROM implementation_task_dispatches j JOIN dispatches d ON d.dispatch_id=j.dispatch_id
           WHERE j.workflow_id=? AND j.workflow_revision=? AND j.task_id=?
             AND j.task_revision=? AND j.attempt_no=? AND j.dispatch_id=?""",
        (payload["workflow_id"], payload["workflow_revision"], payload["task_id"],
         payload["task_revision"], payload["attempt_no"], payload["dispatch_id"]),
    ).fetchone()
    attempt = connection.execute(
        """SELECT * FROM implementation_task_attempts WHERE workflow_id=?
           AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
        (payload["workflow_id"], payload["workflow_revision"], payload["task_id"],
         payload["task_revision"], payload["attempt_no"]),
    ).fetchone()
    if task is None or dispatch is None or attempt is None:
        raise ValueError("policy mismatch reopen historical binding이 없습니다")
    if dispatch["status"] != "failed" or attempt["status"] != "FAILED":
        raise ValueError("policy mismatch reopen 대상 history가 terminal FAIL이 아닙니다")
    evidence_row = connection.execute(
        """SELECT * FROM implementation_evidence WHERE evidence_sha256=?
           AND workflow_id=? AND workflow_revision=? AND task_id=?
           AND task_revision=? AND attempt_no=? AND outcome='FAIL'""",
        (attempt["evidence_sha256"], payload["workflow_id"], payload["workflow_revision"],
         payload["task_id"], payload["task_revision"], payload["attempt_no"]),
    ).fetchone()
    if evidence_row is None:
        raise ValueError("policy mismatch reopen FAIL evidence binding이 없습니다")
    evidence = json.loads(evidence_row["evidence_json"])
    finding = evidence.get("finding")
    if (not isinstance(finding, dict)
            or finding.get("failure_class") != "environment"
            or finding.get("remediable") is not True
            or finding.get("scope_expansion_required") is not False
            or "PERMISSION_POLICY_MISMATCH" not in finding.get("summary", "")
            or finding.get("fingerprint") != attempt["failure_fingerprint"]):
        raise ValueError("policy mismatch reopen evidence가 허용된 실패와 일치하지 않습니다")
    expected = {
        "previous_status": "FAILED",
        "new_status": "PENDING",
        "previous_failure_fingerprint": attempt["failure_fingerprint"],
        "failure_evidence_sha256": attempt["evidence_sha256"],
        "action": "RETRY_SAME_TASK",
        "history_preserved": True,
    }
    for key, value in expected.items():
        if payload[key] != value:
            raise ValueError(f"policy mismatch reopen event.{key} binding 불일치")
    decision_path, decision_sha, _ = _load_policy_retry_decision(
        payload["decision_path"], workflow=workflow, task=task, dispatch=dispatch,
        evidence_sha256=attempt["evidence_sha256"],
        archive_root=Path(workflow["artifact_root"]),
    )
    if decision_sha != payload["decision_sha256"]:
        raise ValueError(f"policy retry decision이 변경되었습니다: {decision_path}")


def command_reopen_policy_mismatch(args: argparse.Namespace) -> dict[str, Any]:
    """검증된 dispatch gate 수정 뒤 권한 preflight 실패 Task 자체를 append-only 재시도한다."""

    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        prior = connection.execute(
            """SELECT event_id,run_id,payload_json FROM orchestration_events
               WHERE event_type='implementation_task.policy_mismatch_reopened'
                 AND entity_type='implementation_task' AND entity_id=?
               ORDER BY event_id""",
            (args.task_id,),
        ).fetchall()
        for event in prior:
            payload = json.loads(event["payload_json"])
            if (payload.get("workflow_revision") == workflow["workflow_revision"]
                    and payload.get("dispatch_id") == args.dispatch_id):
                _verify_policy_mismatch_reopen_event(connection, workflow, event)
                supplied_sha = sha256_file(Path(args.decision_file).resolve())
                if payload.get("decision_sha256") == supplied_sha:
                    connection.rollback()
                    return {"ok": True, "reopened": False, "idempotent": True,
                            "task_id": args.task_id, "decision_sha256": supplied_sha}
                raise RuntimeError("같은 failed Attempt에 다른 policy retry 결정은 적용할 수 없습니다")
        task = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=1""",
            (workflow["workflow_id"], workflow["workflow_revision"], args.task_id),
        ).fetchone()
        if task is None or task["status"] != "FAILED" or task["active_attempt_no"] is None:
            raise RuntimeError("현재 FAILED projection의 Task만 policy retry할 수 있습니다")
        dispatch = connection.execute(
            """SELECT d.*,j.assignment_sha256,j.receipt_sha256,j.attempt_no
               FROM implementation_task_dispatches j JOIN dispatches d ON d.dispatch_id=j.dispatch_id
               WHERE j.workflow_id=? AND j.workflow_revision=? AND j.task_id=?
                 AND j.task_revision=? AND j.attempt_no=?""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"]),
        ).fetchone()
        if dispatch is None or dispatch["dispatch_id"] != args.dispatch_id:
            raise RuntimeError("policy retry 대상 dispatch binding이 일치하지 않습니다")
        if dispatch["status"] != "failed" or _active_dispatch(connection) is not None:
            raise RuntimeError("terminal failed dispatch이고 active slot이 비었을 때만 재시도할 수 있습니다")
        attempt = connection.execute(
            """SELECT * FROM implementation_task_attempts WHERE workflow_id=?
               AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            (task["workflow_id"], task["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"]),
        ).fetchone()
        if attempt is None or attempt["status"] != "FAILED" or not attempt["evidence_sha256"]:
            raise RuntimeError("policy retry에는 immutable FAIL evidence가 필요합니다")
        evidence_row = connection.execute(
            "SELECT * FROM implementation_evidence WHERE evidence_sha256=? AND outcome='FAIL'",
            (attempt["evidence_sha256"],),
        ).fetchone()
        if evidence_row is None:
            raise RuntimeError("policy retry FAIL evidence binding이 없습니다")
        evidence = json.loads(evidence_row["evidence_json"])
        finding = evidence.get("finding")
        if (not isinstance(finding, dict)
                or finding.get("failure_class") != "environment"
                or finding.get("remediable") is not True
                or finding.get("scope_expansion_required") is not False
                or "PERMISSION_POLICY_MISMATCH" not in finding.get("summary", "")
                or finding.get("fingerprint") != task["failure_fingerprint"]):
            raise RuntimeError("직접 확인된 remediable PERMISSION_POLICY_MISMATCH만 같은 Task로 재시도합니다")
        decision_path, decision_sha, decision = _load_policy_retry_decision(
            args.decision_file, workflow=workflow, task=task, dispatch=dispatch,
            evidence_sha256=attempt["evidence_sha256"],
        )
        _archive_policy_retry_remediation_files(Path(workflow["artifact_root"]), decision)
        now = isoformat()
        connection.execute(
            """UPDATE implementation_tasks SET status='PENDING',active_attempt_no=NULL,
               failure_fingerprint=NULL,updated_at=? WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=?""",
            (now, task["workflow_id"], task["workflow_revision"], task["task_id"],
             task["task_revision"]),
        )
        payload = {
            "workflow_id": task["workflow_id"],
            "workflow_revision": task["workflow_revision"],
            "task_id": task["task_id"],
            "task_revision": task["task_revision"],
            "attempt_no": task["active_attempt_no"],
            "dispatch_id": dispatch["dispatch_id"],
            "previous_status": "FAILED",
            "new_status": "PENDING",
            "previous_failure_fingerprint": task["failure_fingerprint"],
            "failure_evidence_sha256": attempt["evidence_sha256"],
            "decision_path": str(decision_path),
            "decision_sha256": decision_sha,
            "action": "RETRY_SAME_TASK",
            "history_preserved": True,
        }
        insert_event(
            connection, "implementation_task.policy_mismatch_reopened",
            run_id=args.run_id, entity_type="implementation_task",
            entity_id=task["task_id"], payload=payload,
        )
        connection.commit()
        return {
            "ok": True, "reopened": True, "idempotent": False,
            "task_id": task["task_id"], "previous_attempt_no": task["active_attempt_no"],
            "next_attempt_no": task["attempt_count"] + 1,
            "decision_sha256": decision_sha,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_confirm(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.thread_id) == bool(args.client_thread_id):
        raise ValueError("thread-id 또는 client-thread-id 중 정확히 하나가 필요합니다")
    receipt_path, receipt_sha, receipt, envelope = _load_creation_receipt(
        args.receipt_file, require_envelope=True, require_request_binding=True,
        require_policy_binding=True,
    )
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        row = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_prompt,j.assignment_sha256,
                      j.receipt_path,j.receipt_sha256,j.confirmed_at
               FROM dispatches d JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE d.dispatch_id=?""",
            (args.dispatch_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("알 수 없거나 implementation에 결속되지 않은 dispatch")
        _assert_creation_receipt_binding(
            receipt, dispatch_id=args.dispatch_id, assignment_sha256=row["assignment_sha256"],
            thread_id=args.thread_id, client_thread_id=args.client_thread_id,
            envelope=envelope, require_envelope=True,
            expected_request=_expected_create_thread_request(row), require_request_binding=True,
            require_policy_binding=True,
        )
        captured_policy = envelope["execution_policy"]
        current_policy = _observe_created_thread_policy(
            captured_policy["state_db_path"], thread_id=args.thread_id,
            external_project_id=row["project_id"],
        )
        if current_policy["thread_row_sha256"] != captured_policy["thread_row_sha256"]:
            raise ValueError("created thread 정책 또는 project binding이 capture 뒤 변경되었습니다")
        claim = connection.execute(
            "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
            (args.dispatch_id,),
        ).fetchone()
        if claim is None:
            raise RuntimeError("LAUNCH_CLAIM_REQUIRED: claim 없는 dispatch는 confirm할 수 없습니다")
        _assert_launch_claim_binding(claim, row)
        if (claim["resolved_at"] is None or claim["thread_id"] != args.thread_id
                or claim["receipt_path"] != str(receipt_path)
                or claim["receipt_sha256"] != receipt_sha):
            raise RuntimeError("confirm 전에 launch claim이 같은 v4 receipt로 해결되어야 합니다")
        state = connection.execute("SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1").fetchone()
        if not state or state["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("dispatch가 현재 active slot이 아닙니다")
        if row["status"] in TERMINAL_DISPATCH_STATUSES:
            raise RuntimeError("닫힌 dispatch binding은 변경할 수 없습니다")
        requested = (args.thread_id, args.client_thread_id)
        existing = (row["thread_id"], row["client_thread_id"])
        if existing != (None, None):
            if existing != requested or row["receipt_sha256"] != receipt_sha:
                raise RuntimeError("기존 thread/client/receipt binding은 변경할 수 없습니다")
            connection.rollback()
            return {"ok": True, "confirmed": False, "idempotent": True, "dispatch": dict(row)}
        now = isoformat()
        status = "active" if args.thread_id else "queued"
        connection.execute(
            """UPDATE dispatches SET thread_id=?,client_thread_id=?,status=?,last_observed_at=?
               WHERE dispatch_id=?""",
            (args.thread_id, args.client_thread_id, status, now, args.dispatch_id),
        )
        connection.execute(
            """UPDATE implementation_task_dispatches SET receipt_path=?,receipt_sha256=?,confirmed_at=?
               WHERE dispatch_id=?""",
            (str(receipt_path), receipt_sha, now, args.dispatch_id),
        )
        connection.execute(
            """UPDATE implementation_tasks SET status='DISPATCHED',updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (now, row["workflow_id"], row["workflow_revision"],
             row["task_id"], row["task_revision"]),
        )
        connection.execute(
            """UPDATE implementation_task_attempts SET status='DISPATCHED'
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            (row["workflow_id"], row["workflow_revision"], row["task_id"],
             row["task_revision"], row["attempt_no"]),
        )
        connection.execute(
            """UPDATE orchestration_runs SET created_thread_id=?,created_client_thread_id=? WHERE run_id=?""",
            (args.thread_id, args.client_thread_id, args.run_id),
        )
        insert_event(connection, "implementation_dispatch.confirmed", run_id=args.run_id,
                     entity_type="dispatch", entity_id=args.dispatch_id,
                     payload={"thread_id": args.thread_id, "client_thread_id": args.client_thread_id,
                              "receipt_path": str(receipt_path), "receipt_sha256": receipt_sha})
        connection.commit()
        return {"ok": True, "confirmed": True, "dispatch_id": args.dispatch_id,
                "thread_id": args.thread_id, "client_thread_id": args.client_thread_id,
                "receipt_sha256": receipt_sha}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_reject_creation(args: argparse.Namespace) -> dict[str, Any]:
    """명시적인 no-effect validation rejection만 실패로 확정한다.

    timeout, 빈 응답, 네트워크 오류처럼 외부 효과가 불명확한 경우에는 이 명령을
    사용하지 않는다. 그런 intent는 active slot과 binding을 그대로 보존한다.
    """
    receipt_path = Path(args.receipt_file).resolve()
    if not receipt_path.is_file():
        raise ValueError("receipt-file이 없습니다")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    required = {"schema_version", "status", "effect", "dispatch_id", "assignment_sha256",
                "error_code", "message", "observed_at"}
    if not isinstance(receipt, dict):
        raise ValueError("creation rejection receipt는 JSON 객체여야 합니다")
    _expect_keys(receipt, required, required, "creation rejection receipt")
    if receipt["schema_version"] != 1 or receipt["status"] != "rejected" or receipt["effect"] != "none":
        raise ValueError("명시적 rejected/no-effect receipt만 허용됩니다")
    if receipt["dispatch_id"] != args.dispatch_id:
        raise ValueError("receipt dispatch binding 불일치")
    _nonempty_string(receipt["error_code"], "receipt.error_code")
    _nonempty_string(receipt["message"], "receipt.message")
    parse_time(_nonempty_string(receipt["observed_at"], "receipt.observed_at"))
    receipt_sha = sha256_file(receipt_path)
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        row = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_prompt,j.assignment_sha256,
                      j.confirmed_at
               FROM dispatches d JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE d.dispatch_id=?""", (args.dispatch_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("알 수 없는 implementation dispatch")
        workflow = _workflow_by_revision(connection, row["workflow_revision"])
        state = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
        ).fetchone()
        if not state or state["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("dispatch가 active slot이 아닙니다")
        if row["thread_id"] or row["client_thread_id"] or row["confirmed_at"]:
            raise RuntimeError(
                "thread/client가 결속된 후에는 creation no-effect rejection을 적용할 수 없습니다"
            )
        if receipt["assignment_sha256"] != row["assignment_sha256"]:
            raise ValueError("receipt assignment binding 불일치")
        fingerprint = sha256_bytes(canonical_json({
            "task_id": row["task_id"], "error_code": receipt["error_code"],
            "assignment_sha256": row["assignment_sha256"], "effect": "none",
        }).encode("utf-8"))
        now = isoformat()
        connection.execute(
            """UPDATE dispatches SET status='creation_failed',terminal_outcome=?,last_observed_at=?
               WHERE dispatch_id=?""",
            (receipt["error_code"], now, args.dispatch_id),
        )
        connection.execute(
            """UPDATE implementation_task_dispatches SET receipt_path=?,receipt_sha256=?,
               confirmed_at=?,terminal_observed_at=? WHERE dispatch_id=?""",
            (str(receipt_path), receipt_sha, now, now, args.dispatch_id),
        )
        connection.execute(
            """UPDATE implementation_task_attempts SET status='FAILED_OBSERVED',failure_fingerprint=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            (fingerprint, workflow["workflow_id"], workflow["workflow_revision"], row["task_id"],
             row["task_revision"], row["attempt_no"]),
        )
        connection.execute(
            """UPDATE implementation_tasks SET status='FAILED',failure_fingerprint=?,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (fingerprint, now, workflow["workflow_id"], workflow["workflow_revision"], row["task_id"],
             row["task_revision"]),
        )
        insert_event(connection, "implementation_dispatch.creation_rejected_no_effect",
                     run_id=args.run_id, entity_type="dispatch", entity_id=args.dispatch_id,
                     payload={"receipt_path": str(receipt_path), "receipt_sha256": receipt_sha,
                              "error_code": receipt["error_code"], "effect": "none",
                              "failure_fingerprint": fingerprint,
                              "active_slot_preserved_until_review": True})
        connection.commit()
        return {"ok": True, "rejected": True, "decision": "OBSERVE", "action": "REVIEW_REQUIRED",
                "dispatch_id": args.dispatch_id, "task_id": row["task_id"],
                "failure_fingerprint": fingerprint, "receipt_sha256": receipt_sha,
                "active_slot_preserved": True}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_observe(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        row = connection.execute(
            """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                      j.attempt_no,j.assignment_prompt,j.assignment_sha256,
                      j.receipt_path,j.receipt_sha256,j.confirmed_at FROM dispatches d
               JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE d.dispatch_id=?""",
            (args.dispatch_id,),
        ).fetchone()
        if not row:
            raise RuntimeError("알 수 없는 implementation dispatch")
        workflow = _workflow_by_revision(connection, row["workflow_revision"])
        state = connection.execute("SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1").fetchone()
        if not state or state["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("먼저 active dispatch를 관찰해야 합니다")
        _verify_stored_creation_receipt(row)
        promoted_thread_id = None
        resolution_receipt: tuple[Path, str, dict[str, Any]] | None = None
        interrupt_receipt: tuple[Path, str, dict[str, Any]] | None = None
        if row["thread_id"]:
            if args.thread_id != row["thread_id"]:
                raise RuntimeError("관찰 thread_id가 immutable binding과 정확히 일치해야 합니다")
            if args.client_thread_id and args.client_thread_id != row["client_thread_id"]:
                raise RuntimeError("client thread binding 변경은 허용되지 않습니다")
        elif row["client_thread_id"]:
            if args.client_thread_id != row["client_thread_id"]:
                raise RuntimeError("client-only dispatch는 정확한 client_thread_id로 관찰해야 합니다")
            if args.thread_id:
                promoted_thread_id = args.thread_id
                resolution_receipt = _load_thread_resolution_receipt(
                    getattr(args, "binding_receipt_file", None),
                    dispatch_id=args.dispatch_id, assignment_sha256=row["assignment_sha256"],
                    client_thread_id=row["client_thread_id"], thread_id=promoted_thread_id,
                )
        else:
            raise RuntimeError("raw receipt로 확인된 thread/client binding이 없습니다")
        if args.status in {"completed", "failed", "needs_attention", "interrupted"}:
            resolved_thread_id = row["thread_id"] or promoted_thread_id
            if not resolved_thread_id:
                raise RuntimeError("terminal 관찰에는 client binding과 연결된 실제 thread_id가 필요합니다")
            if not args.turn_id:
                raise ValueError("terminal 관찰에는 turn-id가 필요합니다")
            if args.status == "interrupted":
                if args.summary_sha256 is not None and not SHA256_RE.fullmatch(args.summary_sha256):
                    raise ValueError("summary-sha256이 있으면 유효한 SHA-256이어야 합니다")
                if not getattr(args, "interrupt_receipt_file", None):
                    raise ValueError("interrupted 관찰에는 interrupt-receipt-file이 필요합니다")
                interrupt_receipt = _load_interrupt_receipt(
                    getattr(args, "interrupt_receipt_file", None),
                    dispatch_id=args.dispatch_id,
                    assignment_sha256=row["assignment_sha256"],
                    automation_id=workflow["automation_id"],
                    thread_id=resolved_thread_id,
                    turn_id=args.turn_id,
                )
            elif not args.summary_sha256 or not SHA256_RE.fullmatch(args.summary_sha256):
                raise ValueError("terminal 관찰에는 turn-id와 유효한 summary-sha256이 필요합니다")
        elif getattr(args, "interrupt_receipt_file", None) is not None:
            raise ValueError("interrupt-receipt-file은 interrupted 관찰에만 사용할 수 있습니다")
        if row["status"] in TERMINAL_DISPATCH_STATUSES:
            same_terminal = row["status"] == args.status
            same_turn = row["last_turn_id"] == args.turn_id
            same_summary = row["summary_sha256"] == args.summary_sha256
            same_cursor = row["wait_cursor"] == args.cursor
            if not (same_terminal and same_turn and same_summary and same_cursor):
                raise RuntimeError(
                    "STALE_DISPATCH_OBSERVATION: terminal dispatch 상태와 결속은 역행하거나 "
                    "다른 terminal 값으로 바꿀 수 없습니다"
                )
            task = connection.execute(
                """SELECT status,failure_fingerprint FROM implementation_tasks
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
                (row["workflow_id"], row["workflow_revision"], row["task_id"],
                 row["task_revision"]),
            ).fetchone()
            connection.rollback()
            return {
                "ok": True, "idempotent": True, "dispatch_id": args.dispatch_id,
                "task_id": row["task_id"], "dispatch_status": row["status"],
                "task_status": task["status"],
                "failure_fingerprint": task["failure_fingerprint"],
                "active_slot_preserved": True,
            }
        now = isoformat()
        connection.execute(
            """UPDATE dispatches SET status=?,thread_id=COALESCE(thread_id,?),last_observed_at=?,
               last_turn_id=COALESCE(?,last_turn_id),
               wait_cursor=COALESCE(?,wait_cursor),summary_sha256=COALESCE(?,summary_sha256)
               WHERE dispatch_id=?""",
            (args.status, promoted_thread_id, now, args.turn_id, args.cursor, args.summary_sha256,
             args.dispatch_id),
        )
        task_status = "DISPATCHED"
        attempt_status = "RUNNING"
        fingerprint = None
        if args.status == "completed":
            task_status, attempt_status = "AWAITING_REVIEW", "AWAITING_REVIEW"
            connection.execute(
                "UPDATE implementation_task_dispatches SET terminal_observed_at=? WHERE dispatch_id=?",
                (now, args.dispatch_id),
            )
        elif args.status in {"failed", "needs_attention"}:
            fingerprint = sha256_bytes(
                canonical_json({"task_id": row["task_id"], "status": args.status,
                                "summary_sha256": args.summary_sha256 or "unknown"}).encode("utf-8")
            )
            task_status, attempt_status = "FAILED", "FAILED_OBSERVED"
            connection.execute(
                "UPDATE implementation_task_dispatches SET terminal_observed_at=? WHERE dispatch_id=?",
                (now, args.dispatch_id),
            )
        elif args.status == "interrupted":
            # Caller provenance와 부분 효과가 확인되지 않은 중단은 구현 실패가 아니다.
            # 같은 thread/turn binding을 보존하고 사용자 판단 전 새 recovery를 만들지 않는다.
            task_status, attempt_status = "DISPATCHED", "INTERRUPTED_OBSERVED"
            connection.execute(
                "UPDATE implementation_task_dispatches SET terminal_observed_at=? WHERE dispatch_id=?",
                (now, args.dispatch_id),
            )
        connection.execute(
            """UPDATE implementation_tasks SET status=?,failure_fingerprint=COALESCE(?,failure_fingerprint),
               updated_at=? WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (task_status, fingerprint, now, row["workflow_id"], row["workflow_revision"],
             row["task_id"], row["task_revision"]),
        )
        connection.execute(
            """UPDATE implementation_task_attempts SET status=?,failure_fingerprint=COALESCE(?,failure_fingerprint)
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            (attempt_status, fingerprint, row["workflow_id"], row["workflow_revision"],
             row["task_id"], row["task_revision"], row["attempt_no"]),
        )
        connection.execute(
            """UPDATE orchestration_runs SET observed_dispatch_id=?,observed_thread_id=?,observed_turn_id=?,
               observed_status=? WHERE run_id=?""",
            (args.dispatch_id, row["thread_id"] or promoted_thread_id, args.turn_id, args.status,
             args.run_id),
        )
        if resolution_receipt is not None:
            resolution_path, resolution_sha, _ = resolution_receipt
            insert_event(
                connection, "implementation_dispatch.thread_resolved", run_id=args.run_id,
                entity_type="dispatch", entity_id=args.dispatch_id,
                payload={"client_thread_id": row["client_thread_id"],
                         "thread_id": promoted_thread_id,
                         "receipt_path": str(resolution_path),
                         "receipt_sha256": resolution_sha},
            )
        interrupt_payload = None
        if interrupt_receipt is not None:
            interrupt_path, interrupt_sha, interrupt_document = interrupt_receipt
            interrupt_payload = {
                "receipt_path": str(interrupt_path),
                "receipt_sha256": interrupt_sha,
                "origin": interrupt_document["origin"],
                "request_id": interrupt_document["request_id"],
                "thread_id": row["thread_id"] or promoted_thread_id,
                "turn_id": args.turn_id,
            }
            insert_event(
                connection, "implementation_dispatch.interrupted", run_id=args.run_id,
                entity_type="dispatch", entity_id=args.dispatch_id, payload=interrupt_payload,
            )
        insert_event(connection, "implementation_dispatch.observed", run_id=args.run_id,
                     entity_type="dispatch", entity_id=args.dispatch_id,
                     payload={"status": args.status, "cursor": args.cursor, "turn_id": args.turn_id,
                               "summary_sha256": args.summary_sha256, "task_status": task_status,
                              "failure_fingerprint": fingerprint, "active_slot_preserved": True,
                              "promoted_thread_id": promoted_thread_id})
        connection.commit()
        result = {"ok": True, "dispatch_id": args.dispatch_id, "task_id": row["task_id"],
                  "dispatch_status": args.status, "task_status": task_status,
                  "failure_fingerprint": fingerprint, "active_slot_preserved": True}
        if interrupt_payload is not None:
            result.update({
                "decision": "USER_DECISION_REQUIRED",
                "action": "INTERRUPTION_REQUIRES_DECISION",
                "interrupt": interrupt_payload,
            })
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _required_evidence_contract(task: sqlite3.Row | dict[str, Any]) -> str | None:
    spec = json.loads(task["spec_json"])
    declared = spec.get("evidence_contract")
    if declared is not None:
        if declared not in {"bound", "evaluation"}:
            raise RuntimeError("등록된 task evidence_contract가 유효하지 않습니다")
        return declared
    return LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256.get(task["spec_sha256"])


def _validate_freshness_contract(
    freshness: Any, *, registered_files: dict[str, str], source_root: str,
) -> dict[str, Any]:
    if not isinstance(freshness, dict):
        raise ValueError("freshness는 객체여야 합니다")
    required = {"contract", "scopes", "bindings"}
    _expect_keys(freshness, required, required, "freshness")
    if freshness["contract"] not in {"bound", "evaluation"}:
        raise ValueError("freshness.contract가 유효하지 않습니다")
    scopes = _string_list(freshness["scopes"], "freshness.scopes", nonempty=True)
    if len(scopes) != len(set(scopes)) or not set(scopes) <= FRESHNESS_SCOPES:
        raise ValueError("freshness.scopes가 유효하지 않습니다")
    bindings = freshness["bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("freshness.bindings가 필요합니다")
    seen: set[tuple[str, str]] = set()
    kinds: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, dict):
            raise ValueError(f"freshness.bindings[{index}]는 객체여야 합니다")
        _expect_keys(binding, {"kind", "path", "sha256"}, {"kind", "path", "sha256"},
                     f"freshness.bindings[{index}]")
        kind = _nonempty_string(binding["kind"], f"freshness.bindings[{index}].kind")
        path = _nonempty_string(binding["path"], f"freshness.bindings[{index}].path")
        sha = binding["sha256"]
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
            raise ValueError(f"freshness.bindings[{index}].sha256이 유효하지 않습니다")
        if registered_files.get(path) != sha:
            raise ValueError("freshness binding은 같은 path/hash의 files 항목을 참조해야 합니다")
        key = (kind, path)
        if key in seen:
            raise ValueError(f"중복 freshness binding: {kind}:{path}")
        seen.add(key)
        kinds.add(kind)
        actual_path = _resolve_evidence_path(source_root, path)
        if not actual_path.is_file() or sha256_file(actual_path) != sha:
            raise ValueError(f"freshness 파일이 없거나 hash가 다릅니다: {actual_path}")
        normalized.append({"kind": kind, "path": path, "sha256": sha})
    if freshness["contract"] == "evaluation":
        missing = EVALUATION_FRESHNESS_KINDS - kinds
        if missing:
            raise ValueError(f"evaluation freshness kind 누락: {sorted(missing)}")
    return {"contract": freshness["contract"], "scopes": scopes, "bindings": normalized}


def _evidence_fresh_for_scope(
    evidence_json: str, *, source_root: str, scope: str | None,
    required_contract: str | None = None,
) -> bool:
    try:
        evidence = json.loads(evidence_json)
        files = evidence.get("files")
        if not isinstance(files, list) or not files:
            return False
        registered: dict[str, str] = {}
        for item in files:
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"}
                    or not isinstance(item["path"], str)
                    or not isinstance(item["sha256"], str)
                    or not SHA256_RE.fullmatch(item["sha256"])):
                return False
            if item["path"] in registered:
                return False
            registered[item["path"]] = item["sha256"]
            actual_path = _resolve_evidence_path(source_root, item["path"])
            if not actual_path.is_file() or sha256_file(actual_path) != item["sha256"]:
                return False
        freshness = evidence.get("freshness")
        if freshness is None:
            return required_contract is None
        normalized = _validate_freshness_contract(
            freshness, registered_files=registered, source_root=source_root,
        )
        if required_contract and normalized["contract"] != required_contract:
            return False
        if scope is not None and scope not in normalized["scopes"]:
            return True
        for binding in normalized["bindings"]:
            path = _resolve_evidence_path(source_root, binding["path"])
            if not path.is_file() or sha256_file(path) != binding["sha256"]:
                return False
        return True
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False


def _load_evidence(path: str, workflow: sqlite3.Row, task: sqlite3.Row,
                   dispatch: sqlite3.Row | None) -> tuple[dict[str, Any], str, str, list[dict[str, Any]]]:
    evidence_path = Path(path).resolve()
    if not evidence_path.is_file():
        raise ValueError("evidence-file이 없습니다")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise ValueError("evidence는 JSON 객체여야 합니다")
    required = {
        "schema_version", "workflow_id", "task_id", "task_revision", "task_spec_sha256",
        "dispatch_id", "assignment_sha256", "reviewer", "observed_at", "files", "checks", "finding",
    }
    allowed = required | {"workflow_revision", "outcome", "severity", "notes", "freshness"}
    _expect_keys(evidence, required, allowed, "evidence")
    if (workflow["workflow_revision"] > 1
            and evidence.get("workflow_revision") != workflow["workflow_revision"]):
        raise ValueError("schema v2 evidence.workflow_revision binding 불일치")
    expected = {
        "schema_version": 1, "workflow_id": workflow["workflow_id"], "task_id": task["task_id"],
        "task_revision": task["task_revision"], "task_spec_sha256": task["spec_sha256"],
        "dispatch_id": dispatch["dispatch_id"] if dispatch else None,
        "assignment_sha256": dispatch["assignment_sha256"] if dispatch else None,
    }
    for key, value in expected.items():
        if evidence[key] != value:
            raise ValueError(f"evidence.{key} binding 불일치")
    reviewer = evidence["reviewer"]
    if not isinstance(reviewer, dict) or set(reviewer) != {"kind", "id"}:
        raise ValueError("reviewer는 kind/id만 가진 객체여야 합니다")
    if reviewer["kind"] not in {"human", "agent", "deterministic"}:
        raise ValueError("reviewer.kind가 유효하지 않습니다")
    _nonempty_string(reviewer["id"], "reviewer.id")
    _nonempty_string(evidence["observed_at"], "observed_at")
    try:
        parse_time(evidence["observed_at"])
    except ValueError as error:
        raise ValueError("observed_at은 RFC3339 시간이어야 합니다") from error
    if not isinstance(evidence["files"], list) or not evidence["files"]:
        raise ValueError("worker 최종 텍스트가 아닌 하나 이상의 파일 evidence가 필요합니다")
    registered_paths: set[str] = set()
    registered_files: dict[str, str] = {}
    verified_files: list[dict[str, Any]] = []
    for index, item in enumerate(evidence["files"]):
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError(f"files[{index}]는 path/sha256 객체여야 합니다")
        source_path = _nonempty_string(item["path"], f"files[{index}].path")
        if not SHA256_RE.fullmatch(item["sha256"]):
            raise ValueError(f"files[{index}].sha256이 유효하지 않습니다")
        actual_path = _resolve_evidence_path(workflow["source_root"], source_path)
        if not actual_path.is_file() or sha256_file(actual_path) != item["sha256"]:
            raise ValueError(f"evidence 파일이 없거나 hash가 다릅니다: {actual_path}")
        if source_path in registered_paths:
            raise ValueError(f"중복 evidence path: {source_path}")
        registered_paths.add(source_path)
        registered_files[source_path] = item["sha256"]
        verified_files.append({"path": source_path, "resolved_path": str(actual_path),
                               "sha256": item["sha256"]})
    required_contract = _required_evidence_contract(task)
    if required_contract and "freshness" not in evidence:
        raise ValueError(f"task 결과에는 {required_contract} freshness contract가 필요합니다")
    if "freshness" in evidence:
        normalized_freshness = _validate_freshness_contract(
            evidence["freshness"], registered_files=registered_files,
            source_root=workflow["source_root"],
        )
        if required_contract and normalized_freshness["contract"] != required_contract:
            raise ValueError(f"task 결과에는 {required_contract} freshness contract가 필요합니다")
    if not isinstance(evidence["checks"], list) or not evidence["checks"]:
        raise ValueError("checks evidence가 필요합니다")
    check_rows = {row["check_id"]: row for row in task["checks"]}
    seen: set[str] = set()
    for index, check in enumerate(evidence["checks"]):
        allowed_check = {"check_id", "status", "method", "detail", "evidence_refs"}
        if not isinstance(check, dict):
            raise ValueError(f"checks[{index}]는 객체여야 합니다")
        _expect_keys(check, allowed_check, allowed_check, f"checks[{index}]")
        if check["check_id"] not in check_rows or check["check_id"] in seen:
            raise ValueError(f"알 수 없거나 중복 check_id: {check['check_id']}")
        seen.add(check["check_id"])
        if check["status"] not in {"PASS", "FAIL", "NOT_RUN"}:
            raise ValueError(f"checks[{index}].status가 유효하지 않습니다")
        _nonempty_string(check["method"], f"checks[{index}].method")
        _nonempty_string(check["detail"], f"checks[{index}].detail")
        refs = _string_list(check["evidence_refs"], f"checks[{index}].evidence_refs", nonempty=True)
        if not set(refs) <= registered_paths:
            raise ValueError("evidence_refs는 files에 등록된 path만 참조할 수 있습니다")
    required_ids = {key for key, row in check_rows.items() if row["required"]}
    if required_ids - seen:
        raise ValueError(f"필수 check 결과 누락: {sorted(required_ids - seen)}")
    finding = evidence["finding"]
    if finding is not None:
        finding_keys = {"failure_class", "fingerprint", "summary", "remediable", "scope_expansion_required"}
        if not isinstance(finding, dict):
            raise ValueError("finding은 null 또는 객체여야 합니다")
        _expect_keys(finding, finding_keys, finding_keys, "finding")
        if finding["failure_class"] not in FAILURE_CLASSES:
            raise ValueError("finding.failure_class가 유효하지 않습니다")
        if not SHA256_RE.fullmatch(finding["fingerprint"]):
            raise ValueError("finding.fingerprint는 SHA-256이어야 합니다")
        _nonempty_string(finding["summary"], "finding.summary")
        if not isinstance(finding["remediable"], bool) or not isinstance(finding["scope_expansion_required"], bool):
            raise ValueError("finding boolean 필드가 유효하지 않습니다")
    outcome = "FAIL" if finding is not None else "PASS"
    if dispatch is not None and dispatch["status"] in {
        "failed", "needs_attention", "creation_failed", "blocked", "cancelled"
    } and outcome != "FAIL":
        raise ValueError("terminal failure/no-effect rejection은 finding 없는 PASS review로 뒤집을 수 없습니다")
    if evidence.get("outcome", outcome) != outcome:
        raise ValueError("outcome과 finding이 모순됩니다")
    if outcome == "PASS":
        failed = [check["check_id"] for check in evidence["checks"]
                  if check["check_id"] in required_ids and check["status"] != "PASS"]
        if failed:
            raise ValueError(f"PASS에는 모든 필수 check PASS가 필요합니다: {failed}")
    canonical = canonical_json(evidence)
    evidence_sha = sha256_bytes(canonical.encode("utf-8"))
    return evidence, canonical, evidence_sha, verified_files


def _task_with_checks(connection: sqlite3.Connection, workflow: sqlite3.Row, task_id: str) -> dict[str, Any]:
    task = connection.execute(
        """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
           AND task_id=? AND task_revision=1""",
        (workflow["workflow_id"], workflow["workflow_revision"], task_id),
    ).fetchone()
    if not task:
        raise RuntimeError(f"알 수 없는 task: {task_id}")
    result = dict(task)
    result["checks"] = [dict(row) for row in connection.execute(
        """SELECT * FROM implementation_task_checks WHERE workflow_id=? AND workflow_revision=?
           AND task_id=? AND task_revision=? ORDER BY check_id""",
        (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
    )]
    return result


def command_review(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        active_binding = _active_dispatch(connection)
        if (active_binding is not None and active_binding["task_id"] == args.task_id
                and active_binding["workflow_revision"] is not None):
            workflow = _workflow_by_revision(
                connection, active_binding["workflow_revision"]
            )
        else:
            workflow = _workflow(connection)
        task_data = _task_with_checks(connection, workflow, args.task_id)
        task = task_data
        dispatch = connection.execute(
            """SELECT d.*,j.assignment_sha256,j.attempt_no FROM dispatches d
               JOIN implementation_task_dispatches j ON j.dispatch_id=d.dispatch_id
               WHERE j.workflow_id=? AND j.workflow_revision=? AND j.task_id=? AND j.task_revision=?
                 AND j.attempt_no=?""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"]),
        ).fetchone() if task["active_attempt_no"] else None
        if task["lane"] == "bootstrap":
            if dispatch is not None:
                raise RuntimeError("FM-00 bootstrap review에는 dispatch가 없어야 합니다")
            if task["status"] not in {"PENDING", "FAILED"}:
                raise RuntimeError("FM-00은 pending/failed 상태에서만 직접 review할 수 있습니다")
            attempt_no = task["attempt_count"] + 1
            purpose = (f"impl:{workflow['workflow_id']}:{workflow['workflow_revision']}:"
                       f"{task['task_id']}:{task['task_revision']}:{attempt_no}:direct-review")
            now = isoformat()
            _invalidate_carry_forward_for_attempt(connection, task, attempt_no, now)
            connection.execute(
                """INSERT INTO implementation_task_attempts(
                   workflow_id,workflow_revision,task_id,task_revision,attempt_no,purpose_key,status,reserved_at
                   ) VALUES(?,?,?,?,?,?, 'AWAITING_REVIEW',?)""",
                (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
                 task["task_revision"], attempt_no, purpose, now),
            )
            connection.execute(
                """UPDATE implementation_tasks SET attempt_count=?,active_attempt_no=?,updated_at=?
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
                (attempt_no, attempt_no, now, workflow["workflow_id"], workflow["workflow_revision"],
                 task["task_id"], task["task_revision"]),
            )
            task["attempt_count"], task["active_attempt_no"] = attempt_no, attempt_no
        else:
            if task["status"] not in {"AWAITING_REVIEW", "FAILED"} or dispatch is None:
                raise RuntimeError("worker terminal 관찰과 immutable dispatch binding 후 review할 수 있습니다")
            state = connection.execute("SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1").fetchone()
            if not state or state["active_dispatch_id"] != dispatch["dispatch_id"]:
                raise RuntimeError("review 대상 dispatch가 active slot이 아닙니다")
        evidence, evidence_json, evidence_sha, verified_files = _load_evidence(
            args.evidence_file, workflow, task, dispatch
        )
        existing = connection.execute(
            "SELECT outcome FROM implementation_evidence WHERE evidence_sha256=?", (evidence_sha,)
        ).fetchone()
        if existing:
            connection.rollback()
            return {"ok": True, "reviewed": False, "idempotent": True,
                    "outcome": existing["outcome"], "evidence_sha256": evidence_sha}
        existing_attempt = connection.execute(
            """SELECT evidence_sha256 FROM implementation_task_attempts WHERE workflow_id=?
               AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"]),
        ).fetchone()
        if existing_attempt and existing_attempt["evidence_sha256"]:
            raise RuntimeError("같은 attempt에는 하나의 immutable review evidence만 허용됩니다")
        outcome = "FAIL" if evidence["finding"] else "PASS"
        now = isoformat()
        connection.execute(
            """INSERT INTO implementation_evidence(
               evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
               evidence_path,evidence_json,outcome,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"], str(Path(args.evidence_file).resolve()),
             evidence_json, outcome, now),
        )
        for check in evidence["checks"]:
            connection.execute(
                """INSERT INTO implementation_check_results(
                   evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
                   check_id,status,detail,evidence_refs_json,recorded_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
                 task["task_revision"], task["active_attempt_no"], check["check_id"], check["status"],
                 check["detail"], canonical_json(check["evidence_refs"]), now),
            )
        fingerprint = evidence["finding"]["fingerprint"] if evidence["finding"] else None
        connection.execute(
            """UPDATE implementation_task_attempts SET status=?,finished_at=?,failure_fingerprint=?,evidence_sha256=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
            ("SUCCEEDED" if outcome == "PASS" else "FAILED", now, fingerprint, evidence_sha,
             workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"]),
        )
        connection.execute(
            """UPDATE implementation_tasks SET status=?,failure_fingerprint=?,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            ("SUCCEEDED" if outcome == "PASS" else "FAILED", fingerprint, now,
             workflow["workflow_id"], workflow["workflow_revision"], task["task_id"], task["task_revision"]),
        )
        if dispatch:
            connection.execute(
                """UPDATE dispatches SET status=?,terminal_outcome=?,last_observed_at=? WHERE dispatch_id=?""",
                ("completed" if outcome == "PASS" else "failed", f"REVIEW_{outcome}", now,
                 dispatch["dispatch_id"]),
            )
            connection.execute(
                """UPDATE orchestration_state SET active_dispatch_id=NULL,updated_at=?,version=version+1
                   WHERE singleton=1 AND active_dispatch_id=?""",
                (now, dispatch["dispatch_id"]),
            )
        if outcome == "PASS" and task["recovery_for_task_id"]:
            connection.execute(
                """UPDATE implementation_tasks SET status='PENDING',active_attempt_no=NULL,updated_at=?
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?
                     AND status='FAILED'""",
                (now, workflow["workflow_id"], workflow["workflow_revision"],
                 task["recovery_for_task_id"], task["task_revision"]),
            )
        insert_event(connection, "implementation_task.reviewed", run_id=args.run_id,
                     entity_type="implementation_task", entity_id=task["task_id"],
                     payload={"outcome": outcome, "evidence_sha256": evidence_sha,
                              "verified_files": verified_files, "failure_fingerprint": fingerprint})
        connection.commit()
        return {"ok": True, "reviewed": True, "task_id": task["task_id"], "outcome": outcome,
                "task_status": "SUCCEEDED" if outcome == "PASS" else "FAILED",
                "evidence_sha256": evidence_sha, "verified_files": verified_files}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_revalidate_success(args: argparse.Namespace) -> dict[str, Any]:
    """Append fresh evidence for a previously successful task without launching a worker."""
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        if _active_dispatch(connection) is not None:
            raise RuntimeError("활성 dispatch를 먼저 관찰·검토해야 합니다")
        workflow = _workflow(connection)
        task = _task_with_checks(connection, workflow, args.task_id)
        evidence, evidence_json, evidence_sha, verified_files = _load_evidence(
            args.evidence_file, workflow, task, None
        )
        if evidence["finding"] is not None:
            raise ValueError("성공 evidence 재검증에는 finding 없는 PASS evidence가 필요합니다")

        existing = connection.execute(
            """SELECT e.outcome,a.status FROM implementation_evidence e
               JOIN implementation_task_attempts a
                 ON a.workflow_id=e.workflow_id AND a.workflow_revision=e.workflow_revision
                AND a.task_id=e.task_id AND a.task_revision=e.task_revision
                AND a.attempt_no=e.attempt_no
               WHERE e.evidence_sha256=? AND e.workflow_id=? AND e.workflow_revision=?
                 AND e.task_id=? AND e.task_revision=?""",
            (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"],
             task["task_id"], task["task_revision"]),
        ).fetchone()
        if existing and existing["outcome"] == "PASS" and existing["status"] == "SUCCEEDED":
            connection.rollback()
            return {"ok": True, "revalidated": False, "idempotent": True,
                    "task_id": task["task_id"], "evidence_sha256": evidence_sha}

        state = _required_check_state(
            connection, task, scope="completion", source_root=workflow["source_root"],
        )
        legacy_reopened = False
        if task["status"] == "SUCCEEDED":
            if state != "stale":
                raise RuntimeError(f"task evidence가 stale 상태가 아닙니다: {state}")
        elif task["status"] == "PENDING" and task["active_attempt_no"] is None:
            legacy_event_id = getattr(args, "legacy_reopen_event_id", None)
            if legacy_event_id is None:
                raise RuntimeError(
                    "legacy reopen 복구에는 정확한 --legacy-reopen-event-id가 필요합니다"
                )
            latest_attempt = connection.execute(
                """SELECT attempt_no,status,finished_at FROM implementation_task_attempts
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?
                   ORDER BY attempt_no DESC LIMIT 1""",
                (workflow["workflow_id"], workflow["workflow_revision"],
                 task["task_id"], task["task_revision"]),
            ).fetchone()
            reopen_event = connection.execute(
                """SELECT event_id,occurred_at,payload_json FROM orchestration_events
                   WHERE event_id=? AND event_type='implementation_task.revalidation_requested'
                     AND entity_type='implementation_task' AND entity_id=?""",
                (legacy_event_id, task["task_id"]),
            ).fetchone()
            if latest_attempt is None or latest_attempt["status"] != "SUCCEEDED" or reopen_event is None:
                raise RuntimeError("일반 PENDING task는 성공 evidence 재검증 경로를 사용할 수 없습니다")
            payload = json.loads(reopen_event["payload_json"])
            expected_payload = {
                "previous_status": "SUCCEEDED",
                "new_status": "PENDING",
                "previous_attempt_no": latest_attempt["attempt_no"],
                "reason": "completion-scope evidence became stale after bound input changes",
                "history_preserved": True,
            }
            for key, value in expected_payload.items():
                if payload.get(key) != value:
                    raise RuntimeError("legacy reopen event가 최신 성공 Attempt와 일치하지 않습니다")
            optional_binding = {
                "workflow_id": workflow["workflow_id"],
                "workflow_revision": workflow["workflow_revision"],
                "task_revision": task["task_revision"],
            }
            for key, value in optional_binding.items():
                if key in payload and payload[key] != value:
                    raise RuntimeError("legacy reopen event의 workflow/task binding이 일치하지 않습니다")
            try:
                reopen_observed_at = parse_time(_nonempty_string(
                    reopen_event["occurred_at"], "legacy reopen event.occurred_at"
                ))
                latest_finished_at = parse_time(_nonempty_string(
                    latest_attempt["finished_at"], "latest successful attempt.finished_at"
                ))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "legacy reopen event와 최신 성공 Attempt에 유효한 timestamp가 필요합니다"
                ) from exc
            if reopen_observed_at <= latest_finished_at:
                raise RuntimeError("legacy reopen event가 최신 성공 Attempt 이후 관찰이 아닙니다")
            legacy_reopened = True
        else:
            raise RuntimeError("SUCCEEDED stale task만 worker 없이 재검증할 수 있습니다")

        attempt_no = task["attempt_count"] + 1
        purpose = (f"impl:{workflow['workflow_id']}:{workflow['workflow_revision']}:"
                   f"{task['task_id']}:{task['task_revision']}:{attempt_no}:direct-revalidation")
        now = isoformat()
        _invalidate_carry_forward_for_attempt(connection, task, attempt_no, now)
        connection.execute(
            """INSERT INTO implementation_task_attempts(
               workflow_id,workflow_revision,task_id,task_revision,attempt_no,purpose_key,
               status,reserved_at,finished_at,evidence_sha256
               ) VALUES(?,?,?,?,?,?, 'SUCCEEDED',?,?,?)""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, purpose, now, now, evidence_sha),
        )
        connection.execute(
            """INSERT INTO implementation_evidence(
               evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
               evidence_path,evidence_json,outcome,recorded_at) VALUES(?,?,?,?,?,?,?,?, 'PASS',?)""",
            (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, str(Path(args.evidence_file).resolve()),
             evidence_json, now),
        )
        for check in evidence["checks"]:
            connection.execute(
                """INSERT INTO implementation_check_results(
                   evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
                   check_id,status,detail,evidence_refs_json,recorded_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"],
                 task["task_id"], task["task_revision"], attempt_no, check["check_id"],
                 check["status"], check["detail"], canonical_json(check["evidence_refs"]), now),
            )
        connection.execute(
            """UPDATE implementation_tasks
               SET status='SUCCEEDED',attempt_count=?,active_attempt_no=?,failure_fingerprint=NULL,
                   updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (attempt_no, attempt_no, now, workflow["workflow_id"], workflow["workflow_revision"],
             task["task_id"], task["task_revision"]),
        )
        insert_event(
            connection, "implementation_task.success_revalidated", run_id=args.run_id,
            entity_type="implementation_task", entity_id=task["task_id"],
            payload={"attempt_no": attempt_no, "evidence_sha256": evidence_sha,
                     "verified_files": verified_files, "worker_launched": False,
                     "dispatch_created": False, "legacy_reopened_repaired": legacy_reopened},
        )
        connection.commit()
        return {"ok": True, "revalidated": True, "task_id": task["task_id"],
                "attempt_no": attempt_no, "evidence_sha256": evidence_sha,
                "worker_launched": False, "dispatch_created": False,
                "legacy_reopened_repaired": legacy_reopened,
                **_decision(connection, workflow)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _lineage_root_task_id(
    connection: sqlite3.Connection, workflow_revision: int, task: dict[str, Any],
) -> str:
    current_revision = workflow_revision
    current = task
    seen: set[tuple[int, str]] = set()
    while True:
        key = (current_revision, current["task_id"])
        if key in seen:
            raise RuntimeError("task/recovery lineage cycle이 발견되었습니다")
        seen.add(key)
        if current.get("recovery_for_task_id"):
            parent_id = current["recovery_for_task_id"]
            parent = connection.execute(
                """SELECT * FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=?""",
                (WORKFLOW_ID, current_revision, parent_id, current["task_revision"]),
            ).fetchone()
            if parent is None:
                raise RuntimeError("recovery lineage parent가 없습니다")
            current = dict(parent)
            continue
        if not table_exists(connection, "implementation_task_lineage"):
            return current["task_id"]
        parent_line = connection.execute(
            """SELECT from_workflow_revision,from_task_id,from_task_revision
               FROM implementation_task_lineage WHERE workflow_id=?
                 AND to_workflow_revision=? AND to_task_id=? AND to_task_revision=?""",
            (WORKFLOW_ID, current_revision, current["task_id"], current["task_revision"]),
        ).fetchone()
        if parent_line is None:
            return current["task_id"]
        current_revision = parent_line["from_workflow_revision"]
        parent = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=?
               AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (WORKFLOW_ID, current_revision, parent_line["from_task_id"],
             parent_line["from_task_revision"]),
        ).fetchone()
        if parent is None:
            raise RuntimeError("cross-revision lineage parent가 없습니다")
        current = dict(parent)


def _recovery_budget_state(
    connection: sqlite3.Connection, workflow: sqlite3.Row,
    task: sqlite3.Row | dict[str, Any], *, evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the approved workflow-lifetime recovery budget for a failed task."""
    task_data = dict(task)
    lineage_root_task_id = _lineage_root_task_id(
        connection, workflow["workflow_revision"], task_data,
    )
    if evidence is None and task_data.get("active_attempt_no") is not None:
        row = connection.execute(
            """SELECT e.evidence_json FROM implementation_task_attempts a
               JOIN implementation_evidence e ON e.evidence_sha256=a.evidence_sha256
               WHERE a.workflow_id=? AND a.workflow_revision=? AND a.task_id=?
                 AND a.task_revision=? AND a.attempt_no=? AND a.status='FAILED'
                 AND e.outcome='FAIL'""",
            (workflow["workflow_id"], workflow["workflow_revision"], task_data["task_id"],
             task_data["task_revision"], task_data["active_attempt_no"]),
        ).fetchone()
        if row is not None:
            evidence = json.loads(row["evidence_json"])
    evidence = evidence if isinstance(evidence, dict) else {}
    finding = evidence.get("finding") if isinstance(evidence.get("finding"), dict) else {}
    severe = evidence.get("severity") == "architecture" or finding.get("failure_class") in {
        "task_contract", "requirement_change",
    }
    policies = json.loads(workflow["spec_json"])["policies"]
    architecture_policy = (
        _latest_reasoning_policy(connection, workflow, "architecture_recovery", "default")
        if severe else None
    )
    same_limit = int(policies.get("max_same_failure_recoveries", 2))
    if architecture_policy is not None:
        same_limit = max(same_limit, len(json.loads(architecture_policy["effort_ladder_json"])))
    total_limit = int(policies.get("max_total_recoveries", 5))
    same_count = connection.execute(
        """SELECT COUNT(*) FROM implementation_recoveries WHERE workflow_id=?
           AND lineage_root_task_id=? AND failure_fingerprint=?""",
        (workflow["workflow_id"], lineage_root_task_id, task_data["failure_fingerprint"]),
    ).fetchone()[0]
    total_count = connection.execute(
        "SELECT COUNT(*) FROM implementation_recoveries WHERE workflow_id=?",
        (workflow["workflow_id"],),
    ).fetchone()[0]
    exhausted_by = []
    if same_count >= same_limit:
        exhausted_by.append("same_failure")
    if total_count >= total_limit:
        exhausted_by.append("workflow_total")
    return {
        "scope": "workflow_lifetime",
        "lineage_root_task_id": lineage_root_task_id,
        "same_count": same_count,
        "same_limit": same_limit,
        "total_count": total_count,
        "total_limit": total_limit,
        "remaining_same": max(same_limit - same_count, 0),
        "remaining_total": max(total_limit - total_count, 0),
        "available": not exhausted_by,
        "exhausted_by": exhausted_by,
    }


def _direct_dependencies(connection: sqlite3.Connection, task: dict[str, Any]) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT p.* FROM implementation_task_dependencies d JOIN implementation_tasks p
           ON p.workflow_id=d.workflow_id AND p.workflow_revision=d.workflow_revision
           AND p.task_id=d.depends_on_task_id AND p.task_revision=d.depends_on_task_revision
           WHERE d.workflow_id=? AND d.workflow_revision=? AND d.task_id=? AND d.task_revision=?
           ORDER BY p.task_id""",
        (task["workflow_id"], task["workflow_revision"], task["task_id"], task["task_revision"]),
    ).fetchall()
    result = []
    for row in rows:
        if row["active_attempt_no"] is None:
            hashes = connection.execute(
                """SELECT DISTINCT source_evidence_sha256 FROM implementation_check_carry_forwards
                   WHERE workflow_id=? AND to_workflow_revision=? AND to_task_id=?
                   AND to_task_revision=? ORDER BY source_evidence_sha256""",
                (row["workflow_id"], row["workflow_revision"], row["task_id"], row["task_revision"]),
            ).fetchall()
        else:
            hashes = connection.execute(
                """SELECT DISTINCT evidence_sha256 FROM implementation_check_results
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=?
                   AND task_revision=? AND attempt_no=? ORDER BY evidence_sha256""",
                (row["workflow_id"], row["workflow_revision"], row["task_id"],
                 row["task_revision"], row["active_attempt_no"]),
            ).fetchall()
        result.append({"task_id": row["task_id"], "task_revision": row["task_revision"],
                       "task_spec_sha256": row["spec_sha256"],
                       "active_attempt_no": row["active_attempt_no"],
                       "evidence_sha256": [h[0] for h in hashes]})
    return result


def _direct_failures(connection: sqlite3.Connection, workflow: sqlite3.Row) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT t.*,e.evidence_json FROM implementation_tasks t
           LEFT JOIN implementation_task_attempts a
             ON a.workflow_id=t.workflow_id AND a.workflow_revision=t.workflow_revision
            AND a.task_id=t.task_id AND a.task_revision=t.task_revision AND a.attempt_no=t.active_attempt_no
           LEFT JOIN implementation_evidence e ON e.evidence_sha256=a.evidence_sha256
           WHERE t.workflow_id=? AND t.workflow_revision=? AND t.status='FAILED' ORDER BY t.task_id""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    result = []
    for row in rows:
        finding = json.loads(row["evidence_json"])["finding"] if row["evidence_json"] else None
        result.append({"task_id": row["task_id"], "task_revision": row["task_revision"],
                       "attempt_no": row["active_attempt_no"],
                       "failure_fingerprint": row["failure_fingerprint"],
                       "failure_class": finding["failure_class"] if finding else None})
    return result


def command_ready_tasks(args: argparse.Namespace) -> dict[str, Any]:
    """Expose dependency readiness, never execution permission or a replacement scheduler."""
    with open_readonly(args.db) as connection:
        connection.execute("BEGIN")
        workflow = _workflow(connection)
        head = connection.execute("SELECT generation FROM implementation_workflow_heads WHERE workflow_id=?",
                                  (workflow["workflow_id"],)).fetchone()
        failed = _direct_failures(connection, workflow)
        ready = _ready_tasks(connection, workflow)
        blocked_recoveries = []
        for recovery in connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND status='PENDING' AND lane='recovery' ORDER BY order_index,task_id""",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        ):
            blockers = []
            for dependency in _direct_dependencies(connection, recovery):
                task = _task_with_checks(connection, workflow, dependency["task_id"])
                check_state = _required_check_state(connection, task, scope="dependency",
                                                    source_root=workflow["source_root"])
                if task["status"] != "SUCCEEDED" or check_state != "passed":
                    blockers.append({"task_id": task["task_id"], "status": task["status"],
                                     "required_check_state": check_state})
            if blockers:
                blocked_recoveries.append({"task_id": recovery["task_id"], "dependency_blockers": blockers})
        return {"ok": True, "execution_authorized": False,
                "workflow_id": workflow["workflow_id"], "workflow_revision": workflow["workflow_revision"],
                "generation": head["generation"], "global_decision": _decision(connection, workflow),
                "failed_tasks": failed,
                "stale_succeeded_tasks": [t["task_id"] for t in _stale_succeeded_tasks(connection, workflow)],
                "blocked_recoveries": blocked_recoveries,
                "recovery_budgets": {f["task_id"]: _recovery_budget_state(
                    connection, workflow, _task_with_checks(connection, workflow, f["task_id"])) for f in failed},
                "ready_tasks": [{"task_id": t["task_id"], "task_revision": t["task_revision"],
                                 "task_spec_sha256": t["spec_sha256"], "task_attempt_count": t["attempt_count"],
                                 "lane": t["lane"], "dependencies": _direct_dependencies(connection, t)}
                                for t in ready]}



def _validate_parallel_assignment(manifest, ready_tasks, selected_task_id, assignment):
    """Consume explicit coordinator envelopes, not native actor/GO authority.

    Existing selection/hash/lease/freshness checks remain mandatory. Each worker
    needs its own prepared selection; this does not create a dispatch or Attempt.
    """
    policies=manifest["policies"]
    if policies.get("development_dispatch_mode") != "isolated-disjoint-parallel":
        if policies.get("serial_source_changes") is False:
            raise ValueError("parallel source policy needs a supported dispatch mode")
        return []
    if policies.get("serial_source_changes") is not False or policies.get("serial_integration") is not True:
        raise ValueError("parallel implementation requires serialized integration")
    if not isinstance(assignment,dict):
        raise ValueError("parallel assignment needs structured, reviewed envelopes")
    if assignment.get("workflow_id") != manifest["workflow_id"] or assignment.get("workflow_revision") != manifest["revision"]:
        raise ValueError("parallel assignment workflow binding differs")
    plan=assignment.get("parallel_execution")
    if not isinstance(plan,dict) or plan.get("integration_mode") != "serial-main":
        raise ValueError("parallel assignment must preserve serial main integration")
    members=plan.get("members")
    if not isinstance(members,list) or not members:
        raise ValueError("parallel assignment members missing")
    ready={t["task_id"]:dict(t) for t in ready_tasks}
    roots=[]; owners=set(); ids=set(); scopes=[]
    main_root=Path(manifest["source_root"]).resolve()
    def overlaps(a,b):
        return a==b or a.startswith(b+"/") or b.startswith(a+"/")
    for member in members:
        if not isinstance(member,dict):
            raise ValueError("parallel assignment member invalid")
        task_id=member.get("task_id")
        if task_id not in ready or task_id in ids or ready[task_id]["lane"] in {"bootstrap","recovery"}:
            raise ValueError("parallel member is duplicate or not dependency-ready")
        if member.get("task_spec_sha256") != ready[task_id]["spec_sha256"]:
            raise ValueError("parallel member current spec hash differs")
        # This is a coordinator claim whose native truth is checked by the caller's admission port.
        if member.get("individual_gate_status") != "ADMITTED":
            raise ValueError("individual Task gate not admitted")
        receipt=member.get("individual_gate_ref")
        if not isinstance(receipt,dict) or not isinstance(receipt.get("path"),str) or not SHA256_RE.fullmatch(str(receipt.get("sha256",""))):
            raise ValueError("individual gate evidence reference missing")
        owner=member.get("writer_session_id")
        if not isinstance(owner,str) or not owner.strip() or owner in owners:
            raise ValueError("parallel writer ownership missing or conflicting")
        worktree=member.get("worktree")
        if not isinstance(worktree,str) or not Path(worktree).is_absolute():
            raise ValueError("parallel worktree missing")
        root=Path(worktree).resolve()
        if root==main_root or root.is_relative_to(main_root) or main_root.is_relative_to(root):
            raise ValueError("parallel worker cannot write the canonical main checkout")
        if any(root==p or root.is_relative_to(p) or p.is_relative_to(root) for p in roots):
            raise ValueError("parallel workers need separate isolated worktrees")
        files=member.get("write_files")
        if not isinstance(files,list) or not files:
            raise ValueError("parallel write envelope missing")
        scope=[]
        for name in files:
            if not isinstance(name,str) or not name or "\\" in name or ":" in name or name.startswith("/") or any(p in {"",".","..",".git"} for p in name.split("/")):
                raise ValueError("parallel write path invalid")
            scope.append(name.casefold())
        if len(set(scope)) != len(scope) or any(overlaps(a,b) for prior in scopes for a in scope for b in prior):
            raise ValueError("parallel write scopes overlap")
        ids.add(task_id);owners.add(owner);roots.append(root);scopes.append(scope)
    if selected_task_id not in ids:
        raise ValueError("selected Task not admitted by parallel assignment")
    if ready[selected_task_id].get("status","PENDING") != "PENDING":
        raise ValueError("selected Task must remain PENDING")
    return members


def _validate_parallel_claims(members, assignment_ref, active_claims, current_selection=None):
    """Compare all current unconsumed preparations, inside the writer transaction."""
    def overlap(a,b):
        return a==b or a.startswith(b+"/") or b.startswith(a+"/")
    for claim in active_claims:
        if claim.get("member") is None:
            raise RuntimeError("unconsumed direct preparation has unknown ownership scope")
        prior=claim["member"]
        for member in members:
            same_task=member["task_id"]==prior["task_id"]
            if (same_task and current_selection is not None and claim.get("selection")==current_selection
                    and claim["assignment"]==assignment_ref and canonical_json(member)==canonical_json(prior)):
                continue  # Only the exact immutable preparation can be repeated/consumed.
            root=Path(member["worktree"]).resolve();old_root=Path(prior["worktree"]).resolve()
            if (same_task or member["writer_session_id"]==prior["writer_session_id"]
                    or root==old_root or root.is_relative_to(old_root) or old_root.is_relative_to(root)
                    or any(overlap(a.casefold(),b.casefold()) for a in member["write_files"] for b in prior["write_files"])):
                raise RuntimeError("parallel assignment conflicts with an unconsumed Task/writer/worktree/file claim")


def _serial_direct_lineage(workflow, selection, preparation=None):
    """원 workflow 정책과 준비 event로 확인한 serial 실행 계보만 비교한다."""
    if workflow is None:
        return None
    manifest = json.loads(workflow["spec_json"])
    policies = manifest["policies"]
    if (policies.get("development_dispatch_mode") == "isolated-disjoint-parallel"
            or policies.get("serial_source_changes") is False):
        return None
    root = str(Path(workflow["source_root"]).resolve())
    if str(Path(manifest["source_root"]).resolve()) != root:
        return None
    if preparation is not None:
        snapshot = preparation.get("preparation_source_snapshot", {})
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("source_root"), str)
                or str(Path(snapshot["source_root"]).resolve()) != root
                or preparation.get("worker_launched") is not False
                or preparation.get("dispatch_created") is not False):
            return None
    decision = selection["decision"]
    if (decision.get("workflow_id") != manifest["workflow_id"]
            or decision.get("workflow_revision") != manifest["revision"]):
        return None
    # coordinator 표식만 갱신할 수 있다. writer/assignment/policy/입력 변경은 다른 계보다.
    return canonical_json({"source_root": root, "policies": policies,
                           "decision": {k: v for k, v in decision.items() if k != "coordinator_id"}})


def _unconsumed_direct_claims(connection, source_root=None):
    """History is the claim authority; epoch, lease and task status never release it.

    Only an accepted, matching terminal record consumes a preparation. Unknown
    outcomes retain ownership. No native identity is inferred from these claims.
    """
    released=set()
    for row in connection.execute("SELECT payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_attempt_recorded'"):
        result=json.loads(row["payload_json"])
        selection=result.get("direct_selection",{})
        prepared_id=selection.get("prepared_event_id")
        if prepared_id is None:
            continue
        original=connection.execute("SELECT payload_json FROM orchestration_events WHERE event_id=? AND event_type='implementation_task.direct_selection_prepared'",(prepared_id,)).fetchone()
        evidence=connection.execute("SELECT evidence_json,outcome FROM implementation_evidence WHERE evidence_sha256=?",(result.get("evidence_sha256"),)).fetchone()
        if original is None or evidence is None:
            continue
        prepared=json.loads(original["payload_json"])["direct_selection"]
        if any(selection.get(k)!=prepared.get(k) for k in ("path","sha256","decision")):
            continue
        body=json.loads(evidence["evidence_json"])
        decision=prepared["decision"]
        if any(body.get(k)!=decision.get(k) for k in ("workflow_id","workflow_revision","task_id","task_revision","task_spec_sha256")):
            continue
        finding=body.get("finding")
        if result.get("outcome")!=evidence["outcome"]:
            continue
        if evidence["outcome"]=="PASS" and finding is None:
            released.add(prepared_id)
        elif evidence["outcome"]=="FAIL" and isinstance(finding,dict) and finding.get("failure_class") not in {None,"external_unknown"}:
            released.add(prepared_id)
    claims=[]
    serial_latest={}
    for row in connection.execute("SELECT event_id,payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_selection_prepared' ORDER BY event_id"):
        preparation=json.loads(row["payload_json"])
        selection=preparation["direct_selection"]
        previous=selection["decision"]
        workflow=connection.execute("SELECT source_root,spec_json FROM implementation_workflows WHERE workflow_id=? AND workflow_revision=?",(previous["workflow_id"],previous["workflow_revision"])).fetchone()
        root=workflow["source_root"] if workflow else None
        if source_root is not None and root is not None and Path(root).resolve()!=Path(source_root).resolve():
            continue
        lineage=_serial_direct_lineage(workflow, selection, preparation)
        if lineage is not None:
            serial_latest[lineage]=row["event_id"]
        if row["event_id"] in released:
            continue
        prior_ref=previous["assignment"]
        member=None
        if root is not None:
            path=_resolve_evidence_path(root,prior_ref["path"])
            if path.is_file() and sha256_file(path)==prior_ref["sha256"]:
                try:
                    assignment=json.loads(path.read_bytes())
                except (ValueError,UnicodeError):
                    assignment={}
                matches=[m for m in assignment.get("parallel_execution",{}).get("members",[]) if isinstance(m,dict) and m.get("task_id")==previous["task_id"]]
                if len(matches)==1:
                    member=matches[0]
        claims.append({"member":member,"assignment":prior_ref,"prepared_event_id":row["event_id"],"decision":previous,"selection":selection,"source_root":root,"serial_lineage":lineage})
    # 원 event는 보존한다. 동일 serial 계보의 마지막 준비만 자원을 점유한다.
    return [claim for claim in claims if claim["serial_lineage"] is None
            or serial_latest[claim["serial_lineage"]] == claim["prepared_event_id"]]


def _assert_no_unscoped_direct_claims(connection, workflow):
    if _unconsumed_direct_claims(connection,workflow["source_root"]):
        raise RuntimeError("unconsumed direct preparation requires scoped selection consumption; unscoped admission blocked")


def _load_direct_selection(connection: sqlite3.Connection, workflow: sqlite3.Row,
                           task: dict[str, Any], path: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    selection_path = Path(path).resolve()
    raw = selection_path.read_bytes()
    data = json.loads(raw)
    keys = {"schema_version", "mode", "workflow_id", "workflow_revision", "generation", "task_id",
            "task_revision", "task_spec_sha256", "task_attempt_count", "coordinator_id", "assignment",
            "policy", "dependencies", "excluded_failures"}
    if not isinstance(data, dict):
        raise ValueError("direct selection은 객체여야 합니다")
    _expect_keys(data, keys, keys, "direct selection")
    head = connection.execute("SELECT generation FROM implementation_workflow_heads WHERE workflow_id=?",
                              (workflow["workflow_id"],)).fetchone()
    expected = {"schema_version": 1, "mode": "dependency-ready", "workflow_id": workflow["workflow_id"],
                "workflow_revision": workflow["workflow_revision"], "generation": head["generation"],
                "task_id": task["task_id"], "task_revision": task["task_revision"],
                "task_spec_sha256": task["spec_sha256"], "task_attempt_count": task["attempt_count"]}
    if any(type(data[k]) is not type(v) or data[k] != v for k, v in expected.items()):
        raise ValueError("direct selection workflow/generation/task binding 불일치")
    _nonempty_string(data["coordinator_id"], "coordinator_id")
    if task["lane"] in {"bootstrap", "recovery"}:
        raise RuntimeError("bootstrap/recovery는 기존 순서 경로만 사용합니다")
    ready = _ready_tasks(connection, workflow)
    if task["task_id"] not in {t["task_id"] for t in ready}:
        raise RuntimeError("선택 task가 현재 dependency-ready가 아닙니다")
    if any(t["lane"] == "recovery" for t in ready):
        raise RuntimeError("READY recovery를 먼저 처리해야 합니다")
    if _stale_succeeded_tasks(connection, workflow):
        raise RuntimeError("stale success를 먼저 재검증해야 합니다")
    if canonical_json(data["dependencies"]) != canonical_json(_direct_dependencies(connection, task)):
        raise ValueError("direct selection dependency identity 불일치")
    failures = data["excluded_failures"]
    if not isinstance(failures, list):
        raise ValueError("excluded_failures는 배열이어야 합니다")
    declared = []
    refs = [data["assignment"], data["policy"]]
    failure_keys = {"task_id", "task_revision", "attempt_no", "failure_fingerprint", "failure_class"}
    for failure in failures:
        if not isinstance(failure, dict):
            raise ValueError("excluded_failures 항목은 객체여야 합니다")
        all_keys = failure_keys | {"shared_inputs_verdict", "impact_review"}
        _expect_keys(failure, all_keys, all_keys, "excluded_failures")
        if failure["shared_inputs_verdict"] != "independent":
            raise ValueError("총괄의 명시적 shared-input 비영향 판정이 필요합니다")
        declared.append({k: failure[k] for k in failure_keys})
        refs.append(failure["impact_review"])
    current = _direct_failures(connection, workflow)
    if canonical_json(declared) != canonical_json(current):
        raise RuntimeError("direct selection FAILED snapshot 불일치")
    ancestors = {row[0] for row in connection.execute(
        """WITH RECURSIVE ancestors(task_id) AS (
             SELECT depends_on_task_id FROM implementation_task_dependencies
             WHERE workflow_id=? AND workflow_revision=? AND task_id=?
             UNION SELECT d.depends_on_task_id FROM implementation_task_dependencies d
             JOIN ancestors a ON d.task_id=a.task_id WHERE d.workflow_id=? AND d.workflow_revision=?
           ) SELECT task_id FROM ancestors""",
        (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
         workflow["workflow_id"], workflow["workflow_revision"]),
    )}
    if ancestors & {f["task_id"] for f in current}:
        raise RuntimeError("FAILED 선행 Task는 우회할 수 없습니다")
    if any(f["failure_class"] in {None, "external_unknown"} for f in current):
        raise RuntimeError("external_unknown 또는 근거 없는 FAILED는 우회할 수 없습니다")
    # ponytail: the coordinator owns shared-source analysis; hashes bind that review, not its truth.
    selection_sha = sha256_bytes(raw)
    refs.append({"path": str(selection_path), "sha256": selection_sha})
    registered = ({str(_resolve_evidence_path(workflow["source_root"], f["path"])): f["sha256"]
                   for f in evidence["files"]} if evidence is not None else None)
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError("selection file ref는 객체여야 합니다")
        _expect_keys(ref, {"path", "sha256"}, {"path", "sha256"}, "selection file ref")
        actual = _resolve_evidence_path(workflow["source_root"], _nonempty_string(ref["path"], "ref.path"))
        if not isinstance(ref["sha256"], str) or not SHA256_RE.fullmatch(ref["sha256"]):
            raise ValueError("selection ref sha256 형식 오류")
        if not actual.is_file() or sha256_file(actual) != ref["sha256"]:
            raise ValueError("selection ref 파일/hash 불일치")
        if registered is not None and registered.get(str(actual)) != ref["sha256"]:
            raise ValueError("selection과 참조 파일을 evidence.files에 결속해야 합니다")
    manifest=json.loads(workflow["spec_json"])
    if manifest["policies"].get("development_dispatch_mode") == "isolated-disjoint-parallel":
        assignment_path=_resolve_evidence_path(workflow["source_root"],data["assignment"]["path"])
        assignment=json.loads(assignment_path.read_bytes())
        # A sibling may have completed during serialized integration. Existing
        # global stale-success checks above still apply; no prior success is synthesized.
        completed_rows=list(connection.execute(
            "SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=? AND status='SUCCEEDED'",
            (workflow["workflow_id"],workflow["workflow_revision"])))
        admitted_rows=list(ready)+[row for row in completed_rows if _required_checks_passed(
            connection,row,scope="completion",source_root=workflow["source_root"])]
        members=_validate_parallel_assignment(manifest,admitted_rows,task["task_id"],assignment)
        for member in members:
            gate=member["individual_gate_ref"]
            gate_path=_resolve_evidence_path(workflow["source_root"],gate["path"])
            if not gate_path.is_file() or sha256_file(gate_path)!=gate["sha256"]:
                raise ValueError("parallel individual gate file/hash differs")
            if registered is not None and registered.get(str(gate_path)) != gate["sha256"]:
                raise ValueError("parallel individual gate must be bound to completion evidence.files")
            peer=next(row for row in admitted_rows if row["task_id"]==member["task_id"])
            if peer["status"] == "SUCCEEDED":
                matched=False
                for terminal in connection.execute(
                    "SELECT payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_attempt_recorded' AND entity_id=? ORDER BY event_id DESC",
                    (peer["task_id"],)):
                    terminal=json.loads(terminal["payload_json"])
                    prior=terminal.get("direct_selection",{}).get("decision",{})
                    if (terminal.get("outcome")=="PASS" and terminal.get("attempt_no")==peer["active_attempt_no"]
                            and prior.get("assignment")==data["assignment"] and prior.get("task_spec_sha256")==peer["spec_sha256"]
                            and all(prior.get(k)==data[k] for k in ("workflow_id","workflow_revision","generation"))):
                        matched=True;break
                if not matched:
                    raise RuntimeError("completed cohort peer has no matching consumed preparation/PASS record")
        active_claims=_unconsumed_direct_claims(connection,workflow["source_root"])
        # Admission acquires pending resources; result consumption never reacquires
        # a completed peer's writer, files or worktree.
        claimed_members=[m for m in members if m["task_id"]==task["task_id"]]
        _validate_parallel_claims(claimed_members,data["assignment"],active_claims,
            {"path":str(selection_path),"sha256":selection_sha,"decision":data})
    elif manifest["policies"].get("serial_source_changes") is False:
        raise ValueError("parallel source policy has no supported dispatch mode")
    selection = {"path": str(selection_path), "sha256": selection_sha, "decision": data}
    if manifest["policies"].get("development_dispatch_mode") != "isolated-disjoint-parallel":
        lineage = _serial_direct_lineage(workflow, selection)
        for claim in _unconsumed_direct_claims(connection, workflow["source_root"]):
            if lineage is None or claim["serial_lineage"] != lineage:
                raise RuntimeError("serial scoped selection conflicts with an unconsumed direct preparation")
    return selection


def _latest_direct_preparation(connection: sqlite3.Connection, selection: dict[str, Any]) -> sqlite3.Row | None:
    data = selection["decision"]
    for row in connection.execute(
        """SELECT * FROM orchestration_events WHERE event_type='implementation_task.direct_selection_prepared'
           AND entity_id=? ORDER BY event_id DESC""", (data["task_id"],)
    ):
        previous = json.loads(row["payload_json"])["direct_selection"]["decision"]
        if all(previous[k] == data[k] for k in ("workflow_id", "workflow_revision", "generation", "task_id", "task_revision",
                                               "task_spec_sha256", "task_attempt_count")):
            return row
    return None


def command_prepare_direct_selection(args: argparse.Namespace) -> dict[str, Any]:
    """Record coordinator admission before work, without an Attempt, dispatch, or new claim table."""
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        if _active_dispatch(connection) is not None:
            raise RuntimeError("활성 dispatch를 먼저 관찰·검토해야 합니다")
        workflow = _workflow(connection)
        task = _task_with_checks(connection, workflow, args.task_id)
        selection = _load_direct_selection(connection, workflow, task, args.selection_file)
        previous = _latest_direct_preparation(connection, selection)
        if previous and json.loads(previous["payload_json"])["direct_selection"] == selection:
            connection.rollback()
            return {"ok": True, "prepared": False, "idempotent": True,
                    "prepared_event_id": previous["event_id"], "prepared_at": previous["occurred_at"]}
        snapshot = _capture_execution_source_snapshot(workflow)
        insert_event(connection, "implementation_task.direct_selection_prepared", run_id=args.run_id,
                     entity_type="implementation_task", entity_id=task["task_id"],
                     payload={"direct_selection": selection, "preparation_source_snapshot": snapshot,
                              "worker_launched": False, "dispatch_created": False})
        event = connection.execute("SELECT event_id,occurred_at FROM orchestration_events WHERE event_id=last_insert_rowid()").fetchone()
        connection.commit()
        return {"ok": True, "prepared": True, "prepared_event_id": event["event_id"],
                "prepared_at": event["occurred_at"], "worker_launched": False, "dispatch_created": False}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_record_direct_attempt(args: argparse.Namespace) -> dict[str, Any]:
    """READY task를 Codex dispatch 없이 직접 실행한 결과 evidence를 기록한다.

    worker는 tick 실행 주체가 띄운 서브에이전트처럼 Codex 밖의 실행자다. 원장에는
    dispatch·thread·receipt 대신 dispatch 없는 Attempt와 evidence만 남기며, 원장의
    task model/reasoning_effort는 바꾸지 않고 실제 실행자 정보는 event payload에만 둔다.
    """
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        if _active_dispatch(connection) is not None:
            raise RuntimeError("활성 dispatch를 먼저 관찰·검토해야 합니다")
        workflow = _workflow(connection)
        run = connection.execute(
            "SELECT * FROM orchestration_runs WHERE run_id=?", (args.run_id,)
        ).fetchone()
        if not run:
            raise RuntimeError("알 수 없는 run")
        if run["created_dispatch_id"]:
            raise RuntimeError("한 run에는 하나의 dispatch 또는 direct attempt만 기록할 수 있습니다")
        task = _task_with_checks(connection, workflow, args.task_id)
        if task["lane"] == "bootstrap":
            raise RuntimeError("FM-00 bootstrap은 review로 직접 검증해야 합니다")
        executor_model = _nonempty_string(args.executor_model, "executor-model")
        executor_kind = _nonempty_string(args.executor_kind, "executor-kind")
        evidence, evidence_json, evidence_sha, verified_files = _load_evidence(
            args.evidence_file, workflow, task, None
        )
        # 같은 task에 이미 기록된 동일 evidence는 상태 게이트와 무관하게 멱등 반환한다.
        existing = connection.execute(
            """SELECT outcome FROM implementation_evidence
               WHERE evidence_sha256=? AND workflow_id=? AND workflow_revision=?
                 AND task_id=? AND task_revision=?""",
            (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"],
             task["task_id"], task["task_revision"]),
        ).fetchone()
        if existing:
            connection.rollback()
            return {"ok": True, "recorded": False, "idempotent": True,
                    "outcome": existing["outcome"], "evidence_sha256": evidence_sha}
        decision = _decision(connection, workflow)
        direct_selection = None
        if getattr(args, "selection_file", None):
            direct_selection = _load_direct_selection(connection, workflow, task, args.selection_file, evidence)
            prepared = _latest_direct_preparation(connection, direct_selection)
            if prepared is None or json.loads(prepared["payload_json"])["direct_selection"] != direct_selection:
                raise RuntimeError("동일한 최신 prepare-direct-selection 기록이 필요합니다")
            for row in connection.execute(
                "SELECT payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_attempt_recorded'"
            ):
                consumed = json.loads(row["payload_json"]).get("direct_selection", {})
                if consumed.get("prepared_event_id") == prepared["event_id"]:
                    raise RuntimeError("이미 소비한 direct selection입니다")
            direct_selection.update(prepared_event_id=prepared["event_id"], prepared_at=prepared["occurred_at"])
        elif decision["decision"] != "READY" or decision.get("task_id") != args.task_id:
            raise RuntimeError(
                f"task {args.task_id!r}는 현재 READY 결정 대상이 아닙니다: "
                f"{decision['decision']}/{decision.get('task_id')}"
            )
        if direct_selection is None:
            _assert_no_unscoped_direct_claims(connection, workflow)
        if task["status"] != "PENDING":
            raise RuntimeError(f"PENDING task만 direct attempt로 기록할 수 있습니다: {task['status']}")
        model_binding = _task_model_binding(connection, workflow, task)
        execution_source_snapshot = _capture_execution_source_snapshot(workflow)
        outcome = "FAIL" if evidence["finding"] else "PASS"
        fingerprint = evidence["finding"]["fingerprint"] if evidence["finding"] else None
        attempt_status = "SUCCEEDED" if outcome == "PASS" else "FAILED"
        attempt_no = task["attempt_count"] + 1
        purpose = (f"impl:{workflow['workflow_id']}:{workflow['workflow_revision']}:"
                   f"{task['task_id']}:{task['task_revision']}:{attempt_no}:direct-attempt")
        now = isoformat()
        _invalidate_carry_forward_for_attempt(connection, task, attempt_no, now)
        connection.execute(
            """INSERT INTO implementation_task_attempts(
               workflow_id,workflow_revision,task_id,task_revision,attempt_no,purpose_key,
               status,reserved_at,finished_at,failure_fingerprint,evidence_sha256
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, purpose, attempt_status, now, now,
             fingerprint, evidence_sha),
        )
        connection.execute(
            """INSERT INTO implementation_evidence(
               evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
               evidence_path,evidence_json,outcome,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], attempt_no, str(Path(args.evidence_file).resolve()),
             evidence_json, outcome, now),
        )
        for check in evidence["checks"]:
            connection.execute(
                """INSERT INTO implementation_check_results(
                   evidence_sha256,workflow_id,workflow_revision,task_id,task_revision,attempt_no,
                   check_id,status,detail,evidence_refs_json,recorded_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_sha, workflow["workflow_id"], workflow["workflow_revision"],
                 task["task_id"], task["task_revision"], attempt_no, check["check_id"],
                 check["status"], check["detail"], canonical_json(check["evidence_refs"]), now),
            )
        connection.execute(
            """UPDATE implementation_tasks
               SET status=?,attempt_count=?,active_attempt_no=?,failure_fingerprint=?,updated_at=?
               WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?""",
            (attempt_status, attempt_no, attempt_no, fingerprint, now,
             workflow["workflow_id"], workflow["workflow_revision"],
             task["task_id"], task["task_revision"]),
        )
        if outcome == "PASS" and task["recovery_for_task_id"]:
            connection.execute(
                """UPDATE implementation_tasks SET status='PENDING',active_attempt_no=NULL,updated_at=?
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? AND task_revision=?
                     AND status='FAILED'""",
                (now, workflow["workflow_id"], workflow["workflow_revision"],
                 task["recovery_for_task_id"], task["task_revision"]),
            )
        connection.execute(
            """UPDATE orchestration_state SET lifecycle_status='ACTIVE',current_phase=?,current_lane=?,
               updated_at=?,version=version+1 WHERE singleton=1""",
            (task["task_id"], task["lane"], now),
        )
        insert_event(
            connection, "implementation_task.direct_attempt_recorded", run_id=args.run_id,
            entity_type="implementation_task", entity_id=task["task_id"],
            payload={"attempt_no": attempt_no, "purpose_key": purpose, "outcome": outcome,
                     "evidence_sha256": evidence_sha, "verified_files": verified_files,
                     "failure_fingerprint": fingerprint,
                     "ledger_model_binding": model_binding,
                     "executor": {"kind": executor_kind, "model": executor_model},
                     "execution_source_snapshot": execution_source_snapshot,
                      **({"direct_selection": direct_selection} if direct_selection else {}),
                     "worker_launched": False, "dispatch_created": False},
        )
        connection.commit()
        return {"ok": True, "recorded": True, "task_id": task["task_id"],
                "attempt_no": attempt_no, "outcome": outcome, "task_status": attempt_status,
                "evidence_sha256": evidence_sha, "verified_files": verified_files,
                "executor": {"kind": executor_kind, "model": executor_model},
                "worker_launched": False, "dispatch_created": False,
                **_decision(connection, workflow)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_register_recovery(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        task = _task_with_checks(connection, workflow, args.task_id)
        if task["status"] != "FAILED" or not task["failure_fingerprint"]:
            raise RuntimeError("실패 관찰과 fingerprint가 있는 FAILED task만 recovery를 등록할 수 있습니다")
        lineage_root_task_id = _lineage_root_task_id(
            connection, workflow["workflow_revision"], task
        )
        evidence_path = Path(args.evidence_file).resolve()
        if not evidence_path.is_file():
            raise ValueError("evidence-file이 없습니다")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        finding = evidence.get("finding") if isinstance(evidence, dict) else None
        if not isinstance(finding, dict) or finding.get("fingerprint") != task["failure_fingerprint"]:
            raise ValueError("recovery evidence finding이 task failure fingerprint와 일치해야 합니다")
        if finding.get("remediable") is not True:
            connection.rollback()
            return {"ok": True, "registered": False, "decision": "WAIT",
                    "reason": "failure is not marked remediable"}
        if finding.get("scope_expansion_required") is True:
            connection.rollback()
            return {"ok": True, "registered": False, "decision": "WAIT",
                    "reason": "scope expansion requires a new user decision"}
        evidence_json = canonical_json(evidence)
        evidence_sha = sha256_bytes(evidence_json.encode("utf-8"))
        reviewed_failure = connection.execute(
            """SELECT 1 FROM implementation_task_attempts a
               JOIN implementation_evidence e ON e.evidence_sha256=a.evidence_sha256
               WHERE a.workflow_id=? AND a.workflow_revision=? AND a.task_id=? AND a.task_revision=?
                 AND a.attempt_no=? AND a.status='FAILED' AND e.outcome='FAIL'
                 AND e.evidence_sha256=?""",
            (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], task["active_attempt_no"], evidence_sha),
        ).fetchone()
        if not reviewed_failure:
            raise ValueError("최신 FAIL review에 immutable하게 결속된 evidence만 recovery 근거가 됩니다")
        files = evidence.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("recovery에는 직접 파일 evidence가 필요합니다")
        direct_items: list[dict[str, str]] = []
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ValueError("recovery evidence files 형식이 유효하지 않습니다")
            source_path = item["path"]
            actual_path = Path(source_path)
            if not actual_path.is_absolute():
                actual_path = Path(workflow["source_root"]) / actual_path
            actual_path = actual_path.resolve()
            if not actual_path.is_file() or sha256_file(actual_path) != item["sha256"]:
                raise ValueError(f"recovery 직접 evidence 파일이 없거나 변조되었습니다: {actual_path}")
            direct_items.append({"path": source_path, "sha256": item["sha256"]})
        direct_evidence_digest = sha256_bytes(
            canonical_json(sorted(direct_items, key=lambda item: (item["path"], item["sha256"]))).encode("utf-8")
        )
        policies = json.loads(workflow["spec_json"])["policies"]
        budget = _recovery_budget_state(connection, workflow, task, evidence=evidence)
        same_count = budget["same_count"]
        severe = evidence.get("severity") == "architecture" or finding.get("failure_class") in {
            "task_contract", "requirement_change"
        }
        architecture_policy = (
            _latest_reasoning_policy(connection, workflow, "architecture_recovery", "default")
            if severe else None
        )
        if not budget["available"]:
            connection.rollback()
            return {
                "ok": True,
                "registered": False,
                "decision": "USER_DECISION_REQUIRED",
                "action": "RECOVERY_LIMIT_REACHED",
                "reason": "approved workflow-lifetime recovery retry limit reached",
                "recovery_budget": budget,
            }
        previous = connection.execute(
            """SELECT 1 FROM implementation_recoveries WHERE workflow_id=?
               AND lineage_root_task_id=? AND failure_fingerprint=? AND direct_evidence_digest=?""",
            (workflow["workflow_id"], lineage_root_task_id, task["failure_fingerprint"],
             direct_evidence_digest),
        ).fetchone()
        if previous:
            connection.rollback()
            return {"ok": True, "registered": False, "decision": "WAIT",
                    "reason": "first retry 이후에는 새 evidence 없는 동일 실패 반복을 차단합니다"}
        prefix = "architecture_recovery" if severe else "recovery"
        if architecture_policy is not None:
            ladder = json.loads(architecture_policy["effort_ladder_json"])
            escalation_step = min(same_count, len(ladder) - 1)
            model = architecture_policy["model"]
            effort = ladder[escalation_step]
            model_reason = (
                f"{architecture_policy['decision_reason']} "
                f"(step {escalation_step + 1}/{len(ladder)}; same failure recoveries={same_count})"
            )
        else:
            model = policies.get(f"{prefix}_model")
            effort = policies.get(f"{prefix}_effort")
            escalation_step = 0
            model_reason = (
                "architecture-severity recovery policy" if severe else "manifest recovery policy"
            )
        if not model or not effort:
            raise RuntimeError(f"manifest policies에 {prefix}_model/effort가 필요합니다")
        if "luna" in str(model).lower() and EFFORTS.index(effort) < EFFORTS.index("high"):
            raise RuntimeError("Luna recovery effort는 high 이상이어야 합니다")
        registration_no = same_count + 1
        safe_original = re.sub(r"[^A-Za-z0-9_.-]", "-", task["task_id"])
        recovery_id = f"REC-{safe_original}-{task['failure_fingerprint'][:16]}-{registration_no}"
        now = isoformat()
        max_order = connection.execute(
            "SELECT MAX(order_index) FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        ).fetchone()[0]
        original_spec = json.loads(task["spec_json"])
        recovery_check = {
            "check_id": f"{recovery_id}-C1", "criterion": "실패 원인을 제거하고 원 task 재시도를 안전하게 연다",
            "method": "실제 변경·검사 파일을 독립 검토하여 같은 failure fingerprint의 재발 방지를 확인한다",
            "required": True,
        }
        recovery_spec = {
            "task_id": recovery_id, "order_index": max_order + 1,
            "title": f"{task['task_id']} 복구 {registration_no}",
            "objective": finding["summary"],
            "instructions": [
                "등록된 직접 증거와 실패 이력을 확인한다.",
                "승인 범위 안에서 실패 원인만 수정하고 원 task 자체를 완료 처리하지 않는다.",
                "복구 변경과 검증의 파일 evidence 및 report를 남긴다.",
            ],
            "inputs": [str(evidence_path), *original_spec.get("inputs", [])],
            "outputs": ["복구 report", "독립 검토 가능한 파일 evidence"],
            "prohibited_effects": original_spec.get("prohibited_effects", []),
            "lane": "recovery", "project_id": policies.get("recovery_project_id"), "model": model,
            "reasoning_effort": effort,
            "model_selection_reason": model_reason,
            "depends_on": [row["depends_on_task_id"] for row in connection.execute(
                """SELECT depends_on_task_id FROM implementation_task_dependencies WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=? ORDER BY depends_on_task_id""",
                (workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
                 task["task_revision"]),
            )],
            "checks": [recovery_check],
        }
        spec_json = canonical_json(recovery_spec)
        spec_sha = sha256_bytes(spec_json.encode("utf-8"))
        connection.execute(
            """INSERT INTO implementation_tasks(
               workflow_id,workflow_revision,task_id,task_revision,order_index,title,lane,project_id,
               model,reasoning_effort,model_selection_reason,spec_json,spec_sha256,status,
               recovery_for_task_id,recovery_for_fingerprint,created_at,updated_at
               ) VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,'PENDING',?,?,?,?)""",
            (workflow["workflow_id"], workflow["workflow_revision"], recovery_id, max_order + 1,
             recovery_spec["title"], "recovery", recovery_spec["project_id"], model, effort,
             recovery_spec["model_selection_reason"], spec_json, spec_sha, task["task_id"],
             task["failure_fingerprint"], now, now),
        )
        connection.execute(
            """INSERT INTO implementation_task_checks(workflow_id,workflow_revision,task_id,task_revision,
               check_id,criterion,method,required,created_at) VALUES(?,?,?,1,?,?,?,?,?)""",
            (workflow["workflow_id"], workflow["workflow_revision"], recovery_id,
             recovery_check["check_id"], recovery_check["criterion"], recovery_check["method"], 1, now),
        )
        for dependency in recovery_spec["depends_on"]:
            connection.execute(
                """INSERT INTO implementation_task_dependencies(workflow_id,workflow_revision,task_id,task_revision,
                   depends_on_task_id,depends_on_task_revision,created_at) VALUES(?,?,?,1,?,1,?)""",
                (workflow["workflow_id"], workflow["workflow_revision"], recovery_id, dependency, now),
            )
        connection.execute(
            """INSERT INTO implementation_recoveries(recovery_task_id,recovery_task_revision,workflow_id,workflow_revision,
               original_task_id,original_task_revision,lineage_root_task_id,failure_fingerprint,registration_no,
               evidence_sha256,direct_evidence_digest,registered_at) VALUES(?,1,?,?,?,?,?,?,?,?,?,?)""",
            (recovery_id, workflow["workflow_id"], workflow["workflow_revision"], task["task_id"],
             task["task_revision"], lineage_root_task_id, task["failure_fingerprint"], registration_no, evidence_sha,
             direct_evidence_digest, now),
        )
        insert_event(connection, "implementation_recovery.registered", run_id=args.run_id,
                     entity_type="implementation_task", entity_id=recovery_id,
                     payload={"original_task_id": task["task_id"],
                              "lineage_root_task_id": lineage_root_task_id,
                              "failure_fingerprint": task["failure_fingerprint"],
                              "evidence_sha256": evidence_sha,
                              "direct_evidence_digest": direct_evidence_digest,
                              "registration_no": registration_no,
                              "model": model, "reasoning_effort": effort,
                              "escalation_step": escalation_step, "severe": severe,
                              "recovery_barrier": "FAILED original remains preserved; ready recovery is prioritized"})
        connection.commit()
        return {"ok": True, "registered": True, "decision": "READY",
                "recovery_task_id": recovery_id, "original_task_id": task["task_id"],
                "spec_sha256": spec_sha, "model": model, "reasoning_effort": effort}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    connection = open_write(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _lease_row(connection, args.run_id)
        workflow = _workflow(connection)
        run = connection.execute("SELECT * FROM orchestration_runs WHERE run_id=?", (args.run_id,)).fetchone()
        if not run or run["finished_at"]:
            raise RuntimeError("알 수 없거나 이미 종료된 run")
        decision = _decision(connection, workflow)
        if args.outcome == "COMPLETE" and decision["decision"] != "COMPLETE":
            raise RuntimeError("모든 task와 required check가 SUCCEEDED가 아니므로 COMPLETE할 수 없습니다")
        now = isoformat()
        connection.execute(
            """UPDATE orchestration_runs SET finished_at=?,outcome=?,decision_code=?,decision_reason=?,
               snapshot_json=? WHERE run_id=?""",
            (now, args.outcome, decision["decision"], args.reason,
             canonical_json(decision), args.run_id),
        )
        if decision["decision"] == "COMPLETE":
            connection.execute(
                "UPDATE implementation_workflows SET state='COMPLETE',completed_at=? WHERE workflow_id=? AND workflow_revision=?",
                (now, workflow["workflow_id"], workflow["workflow_revision"]),
            )
            connection.execute(
                """UPDATE orchestration_state SET lifecycle_status='COMPLETE',current_phase='COMPLETE',
                   current_lane=NULL,blocker_code=NULL,blocker_fingerprint=NULL,completed_at=?,
                   updated_at=?,version=version+1 WHERE singleton=1""",
                (now, now),
            )
        insert_event(connection, "implementation_run.finished", run_id=args.run_id,
                     entity_type="run", entity_id=args.run_id,
                     payload={"outcome": args.outcome, "reason": args.reason, **decision})
        connection.execute(
            "DELETE FROM orchestration_locks WHERE lock_name=? AND owner_run_id=?",
            (LOCK_NAME, args.run_id),
        )
        connection.commit()
        return {"ok": True, "finished": True, "run_id": args.run_id,
                "outcome": args.outcome, **decision}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _append_unique_error(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def _verify_static_task_and_lineage_bindings(
    connection: sqlite3.Connection, workflow: sqlite3.Row, errors: list[str],
) -> None:
    manifest = json.loads(workflow["spec_json"])
    manifest_tasks = {task["task_id"]: task for task in manifest["tasks"]}
    static_rows = connection.execute(
        """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
           AND recovery_for_task_id IS NULL AND recovery_for_fingerprint IS NULL""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    static_by_id = {row["task_id"]: row for row in static_rows}
    if set(static_by_id) != set(manifest_tasks):
        _append_unique_error(errors, "task_manifest_coverage")

    bound_fields = (
        "task_id", "order_index", "title", "lane", "project_id", "model",
        "reasoning_effort", "model_selection_reason",
    )
    for task_id in sorted(set(static_by_id) & set(manifest_tasks)):
        row = static_by_id[task_id]
        spec = manifest_tasks[task_id]
        expected_spec_json = canonical_json(spec)
        if (row["task_revision"] != 1
                or row["spec_json"] != expected_spec_json
                or row["spec_sha256"] != sha256_bytes(expected_spec_json.encode("utf-8"))
                or any(row[field] != spec[field] for field in bound_fields)):
            _append_unique_error(errors, f"task_manifest_binding:{task_id}")

    if workflow["workflow_revision"] <= 1 or not table_exists(
        connection, "implementation_task_lineage"
    ):
        return
    expected_lineage = {
        (
            manifest["parent_revision"], item["from_task_id"], 1,
            workflow["workflow_revision"], item["to_task_id"], 1,
            item["relation"], item["decision"],
            sha256_bytes(item["decision"].encode("utf-8")),
        )
        for item in manifest["lineage"]
    }
    lineage_columns = (
        "from_workflow_revision", "from_task_id", "from_task_revision",
        "to_workflow_revision", "to_task_id", "to_task_revision",
        "relation", "decision", "decision_sha256",
    )
    actual_lineage = {
        tuple(row[column] for column in lineage_columns)
        for row in connection.execute(
            """SELECT * FROM implementation_task_lineage
               WHERE workflow_id=? AND to_workflow_revision=?""",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        )
    }
    if actual_lineage != expected_lineage:
        _append_unique_error(errors, "task_lineage_coverage")


def _verify_recovery_bindings(
    connection: sqlite3.Connection, workflow: sqlite3.Row, errors: list[str],
) -> None:
    marker_rows = connection.execute(
        """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
           AND (recovery_for_task_id IS NOT NULL OR recovery_for_fingerprint IS NOT NULL)""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    marker_by_id = {row["task_id"]: row for row in marker_rows}
    recovery_rows = connection.execute(
        """SELECT * FROM implementation_recoveries
           WHERE workflow_id=? AND workflow_revision=?""",
        (workflow["workflow_id"], workflow["workflow_revision"]),
    ).fetchall()
    recovery_by_id = {row["recovery_task_id"]: row for row in recovery_rows}
    all_ids = sorted(set(marker_by_id) | set(recovery_by_id))

    for recovery_id in all_ids:
        code = f"recovery_integrity:{recovery_id}"
        task = marker_by_id.get(recovery_id)
        recovery = recovery_by_id.get(recovery_id)
        invalid = task is None or recovery is None
        if invalid:
            _append_unique_error(errors, code)
            continue
        if (task["recovery_for_task_id"] is None
                or task["recovery_for_fingerprint"] is None
                or task["lane"] != "recovery"
                or task["task_revision"] != recovery["recovery_task_revision"]
                or task["recovery_for_task_id"] != recovery["original_task_id"]
                or task["recovery_for_fingerprint"] != recovery["failure_fingerprint"]
                or recovery["registration_no"] <= 0):
            invalid = True

        original = connection.execute(
            """SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?
               AND task_id=? AND task_revision=?""",
            (workflow["workflow_id"], workflow["workflow_revision"],
             recovery["original_task_id"], recovery["original_task_revision"]),
        ).fetchone()
        evidence = connection.execute(
            """SELECT * FROM implementation_evidence WHERE evidence_sha256=?
               AND workflow_id=? AND workflow_revision=?""",
            (recovery["evidence_sha256"], workflow["workflow_id"],
             workflow["workflow_revision"]),
        ).fetchone()
        if original is None or evidence is None:
            invalid = True
        else:
            attempt = connection.execute(
                """SELECT * FROM implementation_task_attempts WHERE workflow_id=?
                   AND workflow_revision=? AND task_id=? AND task_revision=? AND attempt_no=?""",
                (workflow["workflow_id"], workflow["workflow_revision"],
                 recovery["original_task_id"], recovery["original_task_revision"],
                 evidence["attempt_no"]),
            ).fetchone()
            files: list[Any] = []
            direct_items: list[dict[str, str]] = []
            try:
                evidence_document = json.loads(evidence["evidence_json"])
                files = evidence_document["files"]
                direct_items = [
                    {"path": item["path"], "sha256": item["sha256"]}
                    for item in files
                    if isinstance(item, dict) and set(item) == {"path", "sha256"}
                ]
                direct_digest = sha256_bytes(canonical_json(sorted(
                    direct_items, key=lambda item: (item["path"], item["sha256"])
                )).encode("utf-8"))
                finding_fingerprint = evidence_document["finding"]["fingerprint"]
            except (KeyError, TypeError, json.JSONDecodeError):
                invalid = True
                direct_digest = None
                finding_fingerprint = None
            if (sha256_bytes(evidence["evidence_json"].encode("utf-8"))
                    != evidence["evidence_sha256"]
                    or evidence["task_id"] != recovery["original_task_id"]
                    or evidence["task_revision"] != recovery["original_task_revision"]
                    or evidence["outcome"] != "FAIL"
                    or not files or len(direct_items) != len(files)
                    or direct_digest != recovery["direct_evidence_digest"]
                    or finding_fingerprint != recovery["failure_fingerprint"]
                    or attempt is None
                    or attempt["status"] != "FAILED"
                    or attempt["failure_fingerprint"] != recovery["failure_fingerprint"]
                    or attempt["evidence_sha256"] != recovery["evidence_sha256"]):
                invalid = True
            try:
                root_task_id = _lineage_root_task_id(
                    connection, workflow["workflow_revision"], dict(original),
                )
            except RuntimeError:
                invalid = True
                root_task_id = None
            if root_task_id != recovery["lineage_root_task_id"]:
                invalid = True

        safe_original = re.sub(r"[^A-Za-z0-9_.-]", "-", recovery["original_task_id"])
        expected_id = (
            f"REC-{safe_original}-{recovery['failure_fingerprint'][:16]}-"
            f"{recovery['registration_no']}"
        )
        if recovery_id != expected_id:
            invalid = True
        if invalid:
            _append_unique_error(errors, code)


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    with open_readonly(args.db) as connection:
        requested_revision = getattr(args, "revision", None)
        workflow = (
            _workflow_by_revision(connection, requested_revision)
            if requested_revision is not None else _workflow(connection)
        )
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        errors: list[str] = []
        warnings: list[str] = []
        revalidation_required: list[str] = []
        if integrity != ["ok"]:
            errors.append("integrity_check")
        if foreign_keys:
            errors.append("foreign_key_check")
        if table_exists(connection, "implementation_workflow_heads"):
            try:
                _require_current_schema(connection)
            except RuntimeError:
                errors.append("writer_contract_version")
            head = connection.execute(
                "SELECT * FROM implementation_workflow_heads WHERE workflow_id=?",
                (workflow["workflow_id"],),
            ).fetchone()
            active_rows = connection.execute(
                """SELECT workflow_revision FROM implementation_workflows
                   WHERE workflow_id=? AND state='ACTIVE'""",
                (workflow["workflow_id"],),
            ).fetchall()
            head_workflow = (
                _workflow_by_revision(connection, head["active_workflow_revision"])
                if head is not None else None
            )
            expected_active_count = 1 if head_workflow is not None and head_workflow["state"] == "ACTIVE" else 0
            if head is None or len(active_rows) != expected_active_count:
                errors.append("workflow_head_consistency")
            elif (expected_active_count == 1
                  and active_rows[0]["workflow_revision"] != head["active_workflow_revision"]):
                errors.append("workflow_head_active_revision")
            missing_fences = []
            trigger_names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            for table in WRITER_FENCE_TABLES:
                if not table_exists(connection, table):
                    continue
                for operation in ("insert", "update", "delete"):
                    expected = f"flowmarshal_writer_fence_{table}_{operation}"
                    if expected not in trigger_names:
                        missing_fences.append(expected)
            if missing_fences:
                errors.append("writer_fence_triggers")
        if workflow["state"] not in {"DRAFT", "ACTIVE", "RETIRED", "COMPLETE"}:
            errors.append("workflow_state")
        if sha256_bytes(workflow["spec_json"].encode("utf-8")) != workflow["spec_sha256"]:
            errors.append("workflow_spec_digest")
        for task in connection.execute(
            "SELECT * FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=?",
            (workflow["workflow_id"], workflow["workflow_revision"]),
        ):
            if sha256_bytes(task["spec_json"].encode("utf-8")) != task["spec_sha256"]:
                errors.append(f"task_spec_digest:{task['task_id']}")
            if task["task_revision"] != 1:
                errors.append(f"task_revision:{task['task_id']}")
            if task["status"] not in TASK_STATUSES:
                errors.append(f"task_status:{task['task_id']}")
            if task["status"] == "SUCCEEDED":
                check_state = _required_check_state(
                    connection, task, scope="completion", source_root=workflow["source_root"]
                )
                if check_state == "missing":
                    errors.append(f"required_checks:{task['task_id']}")
                elif check_state == "stale":
                    warnings.append(f"stale_evidence:{task['task_id']}")
                    revalidation_required.append(task["task_id"])
        _verify_static_task_and_lineage_bindings(connection, workflow, errors)
        _verify_recovery_bindings(connection, workflow, errors)
        if workflow["workflow_revision"] > 1 and table_exists(
            connection, "implementation_task_lineage"
        ):
            for carry in connection.execute(
                """SELECT h.*,t.status FROM implementation_task_carry_forwards h
                   JOIN implementation_tasks t ON t.workflow_id=h.workflow_id
                    AND t.workflow_revision=h.to_workflow_revision
                    AND t.task_id=h.to_task_id AND t.task_revision=h.to_task_revision
                   WHERE h.workflow_id=? AND h.to_workflow_revision=?""",
                (workflow["workflow_id"], workflow["workflow_revision"]),
            ):
                if carry["invalidated_at"] is None and carry["status"] == "SUCCEEDED":
                    carried_task = connection.execute(
                        """SELECT * FROM implementation_tasks WHERE workflow_id=?
                           AND workflow_revision=? AND task_id=? AND task_revision=?""",
                        (carry["workflow_id"], carry["to_workflow_revision"],
                         carry["to_task_id"], carry["to_task_revision"]),
                    ).fetchone()
                    if _required_check_state(
                        connection, carried_task, scope="completion",
                        source_root=workflow["source_root"],
                    ) != "passed":
                        errors.append(f"carry_forward_checks:{carry['to_task_id']}")
                if carry["invalidated_at"] is not None and carry["invalidated_by_attempt_no"] is None:
                    errors.append(f"carry_forward_invalidation:{carry['to_task_id']}")
        dispatch_rows = connection.execute(
            """SELECT d.dispatch_id,d.purpose_key dispatch_purpose,d.assignment_digest,d.model,
                      d.reasoning_effort,d.model_selection_reason,d.thread_id,d.client_thread_id,d.status,
                      d.project_id,d.title,
                      j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,j.attempt_no,
                      j.purpose_key task_purpose,j.assignment_prompt,j.assignment_sha256,
                      j.receipt_path,j.receipt_sha256,j.confirmed_at,
                      a.status attempt_status,t.model task_model,t.reasoning_effort task_effort,
                      t.model_selection_reason task_model_reason
               FROM implementation_task_dispatches j
               JOIN dispatches d ON d.dispatch_id=j.dispatch_id
               JOIN implementation_task_attempts a
                 ON a.workflow_id=j.workflow_id AND a.workflow_revision=j.workflow_revision
                AND a.task_id=j.task_id AND a.task_revision=j.task_revision
                AND a.attempt_no=j.attempt_no
               JOIN implementation_tasks t
                 ON t.workflow_id=j.workflow_id AND t.workflow_revision=j.workflow_revision
                AND t.task_id=j.task_id AND t.task_revision=j.task_revision"""
        ).fetchall()
        for row in dispatch_rows:
            dispatch_id = row["dispatch_id"]
            expected_purpose = (
                f"impl:{row['workflow_id']}:{row['workflow_revision']}:"
                f"{row['task_id']}:{row['task_revision']}:{row['attempt_no']}"
            )
            if row["dispatch_purpose"] != expected_purpose or row["task_purpose"] != expected_purpose:
                errors.append(f"dispatch_purpose:{dispatch_id}")
            if sha256_bytes(row["assignment_prompt"].encode("utf-8")) != row["assignment_sha256"]:
                errors.append(f"assignment_prompt_digest:{dispatch_id}")
            if row["assignment_digest"] != row["assignment_sha256"]:
                errors.append(f"assignment_dispatch_digest:{dispatch_id}")
            model_binding = None
            if table_exists(connection, "implementation_dispatch_model_bindings"):
                model_binding = connection.execute(
                    "SELECT * FROM implementation_dispatch_model_bindings WHERE dispatch_id=?",
                    (dispatch_id,),
                ).fetchone()
            expected_binding = (
                (model_binding["model"], model_binding["reasoning_effort"],
                 model_binding["binding_reason"])
                if model_binding is not None else
                (row["task_model"], row["task_effort"], row["task_model_reason"])
            )
            if (row["model"], row["reasoning_effort"], row["model_selection_reason"]) != expected_binding:
                errors.append(f"assignment_model_binding:{dispatch_id}")
            bound = bool(row["thread_id"] or row["client_thread_id"] or row["confirmed_at"])
            if bound:
                try:
                    _verify_stored_receipt(row)
                    if row["thread_id"] and row["client_thread_id"]:
                        _verify_thread_resolution_event(connection, row)
                except (ValueError, RuntimeError, OSError, json.JSONDecodeError):
                    errors.append(f"create_receipt_binding:{dispatch_id}")
            elif row["status"] != UNCONFIRMED_DISPATCH_STATUS:
                errors.append(f"unconfirmed_dispatch_state:{dispatch_id}")
            if row["status"] == "interrupted":
                try:
                    dispatch_workflow = _workflow_by_revision(
                        connection, row["workflow_revision"],
                    )
                    _verify_interrupt_event(
                        connection, row, dispatch_workflow["automation_id"],
                    )
                except (ValueError, RuntimeError, OSError, json.JSONDecodeError):
                    errors.append(f"interrupt_receipt_binding:{dispatch_id}")
        if table_exists(connection, "implementation_launch_claims"):
            for claim in connection.execute(
                "SELECT * FROM implementation_launch_claims ORDER BY claimed_at,dispatch_id"
            ):
                dispatch = connection.execute(
                    """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                              j.attempt_no,j.assignment_prompt,j.assignment_sha256
                       FROM dispatches d JOIN implementation_task_dispatches j
                         ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
                    (claim["dispatch_id"],),
                ).fetchone()
                try:
                    if dispatch is None:
                        raise RuntimeError("launch claim dispatch가 없습니다")
                    _assert_launch_claim_binding(claim, dispatch)
                    if claim["resolved_at"] is not None:
                        receipt_path, receipt_sha, receipt, envelope = _load_creation_receipt(
                            claim["receipt_path"], require_envelope=True,
                            require_request_binding=True, require_policy_binding=True,
                        )
                        if receipt_sha != claim["receipt_sha256"]:
                            raise RuntimeError("launch claim receipt hash가 다릅니다")
                        _assert_creation_receipt_binding(
                            receipt, dispatch_id=claim["dispatch_id"],
                            assignment_sha256=claim["assignment_sha256"],
                            thread_id=claim["thread_id"], client_thread_id=None,
                            envelope=envelope, require_envelope=True,
                            expected_request=_expected_create_thread_request(dispatch),
                            require_request_binding=True, require_policy_binding=True,
                        )
                except (ValueError, RuntimeError, OSError, json.JSONDecodeError):
                    errors.append(f"launch_claim_binding:{claim['dispatch_id']}")
        for event in connection.execute(
            """SELECT event_id,payload_json FROM orchestration_events
               WHERE event_type='implementation_task.interruption_reopened'
               ORDER BY event_id"""
        ):
            try:
                payload = json.loads(event["payload_json"])
                event_workflow = _workflow_by_revision(
                    connection, int(payload["workflow_revision"]),
                )
                _verify_interruption_reopen_event(connection, event_workflow, event)
            except (KeyError, TypeError, ValueError, RuntimeError, OSError, json.JSONDecodeError):
                errors.append(f"interruption_reopen_binding:{event['event_id']}")
        for event in connection.execute(
            """SELECT event_id,payload_json FROM orchestration_events
               WHERE event_type='implementation_task.policy_mismatch_reopened'
               ORDER BY event_id"""
        ):
            try:
                payload = json.loads(event["payload_json"])
                event_workflow = _workflow_by_revision(
                    connection, int(payload["workflow_revision"]),
                )
                _verify_policy_mismatch_reopen_event(connection, event_workflow, event)
            except (KeyError, TypeError, ValueError, RuntimeError, OSError, json.JSONDecodeError):
                errors.append(f"policy_mismatch_reopen_binding:{event['event_id']}")
        active_count = connection.execute(
            "SELECT COUNT(*) FROM orchestration_state WHERE singleton=1 AND active_dispatch_id IS NOT NULL"
        ).fetchone()[0]
        dangling_active = connection.execute(
            """SELECT 1 FROM orchestration_state s LEFT JOIN dispatches d ON d.dispatch_id=s.active_dispatch_id
               WHERE s.singleton=1 AND s.active_dispatch_id IS NOT NULL AND d.dispatch_id IS NULL"""
        ).fetchone()
        if active_count > 1 or dangling_active:
            errors.append("active_dispatch_consistency")
        duplicate_attempt = connection.execute(
            """SELECT 1 FROM implementation_task_dispatches GROUP BY workflow_id,workflow_revision,
               task_id,task_revision,attempt_no HAVING COUNT(*)>1 LIMIT 1"""
        ).fetchone()
        if duplicate_attempt:
            errors.append("duplicate_task_attempt_dispatch")
        count_tables = (
            "implementation_workflows", "implementation_tasks", "implementation_task_dependencies",
            "implementation_task_checks", "implementation_task_attempts",
            "implementation_task_dispatches", "implementation_evidence",
            "implementation_check_results", "implementation_recoveries",
            "implementation_launch_claims",
            "implementation_reasoning_policies", "implementation_dispatch_model_bindings",
            "implementation_writer_contract", "implementation_workflow_heads",
            "implementation_task_lineage", "implementation_task_carry_forwards",
            "implementation_check_carry_forwards",
        )
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in count_tables if table_exists(connection, table)
        }
        return {"ok": not errors, "integrity_check": integrity, "foreign_key_check": foreign_keys,
                "errors": errors, "warnings": warnings,
                "revalidation_required": revalidation_required,
                "counts": counts,
                "decision": (
                    _decision(connection, workflow) if workflow["state"] == "ACTIVE"
                    else {"decision": workflow["state"],
                          "reason": "non-head workflow revisions are not runnable"}
                )}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate")
    migrate.add_argument("--manifest", required=True)
    migrate.add_argument("--backup-dir", required=True)
    migrate.set_defaults(handler=command_migrate)

    register_revision = sub.add_parser("register-revision")
    register_revision.add_argument("--manifest", required=True)
    register_revision.set_defaults(handler=command_register_revision)
    workflow_head = sub.add_parser("workflow-head")
    workflow_head.set_defaults(handler=command_workflow_head)
    activate_revision = sub.add_parser("activate-revision")
    activate_revision.add_argument("--revision", type=int, required=True)
    activate_revision.add_argument("--expected-generation", type=int, required=True)
    activate_revision.add_argument("--decision-file", required=True)
    activate_revision.set_defaults(handler=command_activate_revision)

    status = sub.add_parser("status")
    status.set_defaults(handler=command_status)
    ready_tasks = sub.add_parser("ready-tasks")
    ready_tasks.set_defaults(handler=command_ready_tasks)
    prepare_direct = sub.add_parser("prepare-direct-selection")
    prepare_direct.add_argument("--run-id", required=True)
    prepare_direct.add_argument("--task-id", required=True)
    prepare_direct.add_argument("--selection-file", required=True)
    prepare_direct.set_defaults(handler=command_prepare_direct_selection)
    reopen_stale = sub.add_parser("reopen-stale")
    reopen_stale.add_argument("--run-id", required=True)
    reopen_stale.add_argument("--task-id", required=True)
    reopen_stale.set_defaults(handler=command_reopen_stale)
    reopen_interrupted = sub.add_parser("reopen-interrupted")
    reopen_interrupted.add_argument("--run-id", required=True)
    reopen_interrupted.add_argument("--task-id", required=True)
    reopen_interrupted.add_argument("--dispatch-id", required=True)
    reopen_interrupted.add_argument("--interrupt-receipt-file", required=True)
    reopen_interrupted.add_argument("--decision-file", required=True)
    reopen_interrupted.add_argument("--dry-run", action="store_true")
    reopen_interrupted.set_defaults(handler=command_reopen_interrupted)
    task = sub.add_parser("task")
    task.add_argument("--task-id", required=True)
    task.set_defaults(handler=command_task)
    configure_reasoning = sub.add_parser("configure-reasoning-policy")
    configure_reasoning.add_argument(
        "--scope", choices=("task", "architecture_recovery"), required=True
    )
    configure_reasoning.add_argument("--key", required=True)
    configure_reasoning.add_argument("--model", required=True)
    configure_reasoning.add_argument("--effort-ladder", nargs="+", required=True)
    configure_reasoning.add_argument(
        "--escalation-trigger",
        choices=("independent_review_failure", "same_failure_with_new_evidence"),
        required=True,
    )
    configure_reasoning.add_argument("--reason", required=True)
    configure_reasoning.set_defaults(handler=command_configure_reasoning_policy)
    reasoning_policies = sub.add_parser("reasoning-policies")
    reasoning_policies.set_defaults(handler=command_reasoning_policies)
    begin = sub.add_parser("begin")
    begin.add_argument("--automation-id", required=True)
    begin.add_argument("--lease-seconds", type=int, default=900)
    begin.set_defaults(handler=command_begin)
    reserve = sub.add_parser("reserve-next")
    reserve.add_argument("--run-id", required=True)
    reserve.set_defaults(handler=command_reserve_next)
    confirm = sub.add_parser("confirm")
    confirm.add_argument("--run-id", required=True)
    confirm.add_argument("--dispatch-id", required=True)
    confirm.add_argument("--thread-id")
    confirm.add_argument("--client-thread-id")
    confirm.add_argument("--receipt-file", required=True)
    confirm.set_defaults(handler=command_confirm)
    capture = sub.add_parser("capture-create-receipt")
    capture.add_argument("--run-id", required=True)
    capture.add_argument("--dispatch-id", required=True)
    capture.add_argument("--raw-response-file", required=True)
    capture.add_argument("--codex-state-db", required=True)
    capture.add_argument("--output", required=True)
    capture.set_defaults(handler=command_capture_create_receipt)
    launch = sub.add_parser("launch-codex-exec")
    launch.add_argument("--run-id", required=True)
    launch.add_argument("--dispatch-id", required=True)
    launch.add_argument("--codex-executable", required=True)
    launch.add_argument("--codex-state-db", required=True)
    launch.add_argument("--output-dir", required=True)
    launch.add_argument("--start-timeout-seconds", type=float, default=20.0)
    launch.set_defaults(handler=command_launch_codex_exec)
    reopen_policy = sub.add_parser("reopen-policy-mismatch")
    reopen_policy.add_argument("--run-id", required=True)
    reopen_policy.add_argument("--task-id", required=True)
    reopen_policy.add_argument("--dispatch-id", required=True)
    reopen_policy.add_argument("--decision-file", required=True)
    reopen_policy.set_defaults(handler=command_reopen_policy_mismatch)
    reject = sub.add_parser("reject-creation")
    reject.add_argument("--run-id", required=True)
    reject.add_argument("--dispatch-id", required=True)
    reject.add_argument("--receipt-file", required=True)
    reject.set_defaults(handler=command_reject_creation)
    observe = sub.add_parser("observe")
    observe.add_argument("--run-id", required=True)
    observe.add_argument("--dispatch-id", required=True)
    observe.add_argument(
        "--status",
        choices=("running", "completed", "failed", "needs_attention", "interrupted"),
        required=True,
    )
    observe.add_argument("--cursor")
    observe.add_argument("--turn-id")
    observe.add_argument("--summary-sha256")
    observe.add_argument("--thread-id")
    observe.add_argument("--client-thread-id")
    observe.add_argument("--binding-receipt-file")
    observe.add_argument("--interrupt-receipt-file")
    observe.set_defaults(handler=command_observe)
    review = sub.add_parser("review")
    review.add_argument("--run-id", required=True)
    review.add_argument("--task-id", required=True)
    review.add_argument("--evidence-file", required=True)
    review.set_defaults(handler=command_review)
    revalidate = sub.add_parser("revalidate-success")
    revalidate.add_argument("--run-id", required=True)
    revalidate.add_argument("--task-id", required=True)
    revalidate.add_argument("--evidence-file", required=True)
    revalidate.add_argument("--legacy-reopen-event-id", type=int)
    revalidate.set_defaults(handler=command_revalidate_success)
    direct = sub.add_parser("record-direct-attempt")
    direct.add_argument("--run-id", required=True)
    direct.add_argument("--task-id", required=True)
    direct.add_argument("--evidence-file", required=True)
    direct.add_argument("--executor-model", required=True)
    direct.add_argument("--executor-kind", default="claude-subagent")
    direct.add_argument("--selection-file")
    direct.set_defaults(handler=command_record_direct_attempt)
    recovery = sub.add_parser("register-recovery")
    recovery.add_argument("--run-id", required=True)
    recovery.add_argument("--task-id", required=True)
    recovery.add_argument("--evidence-file", required=True)
    recovery.set_defaults(handler=command_register_recovery)
    finish = sub.add_parser("finish")
    finish.add_argument("--run-id", required=True)
    finish.add_argument("--outcome", required=True)
    finish.add_argument("--reason", required=True)
    finish.set_defaults(handler=command_finish)
    verify = sub.add_parser("verify")
    verify.add_argument("--revision", type=int)
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
        json_output(result)
        if args.command == "verify" and not result["ok"]:
            return 1
        return 0
    except (ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError, OSError) as error:
        json_output({"ok": False, "error": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
