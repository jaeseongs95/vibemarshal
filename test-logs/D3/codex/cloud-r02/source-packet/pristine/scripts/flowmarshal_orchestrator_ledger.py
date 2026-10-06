"""FlowMarshal 1.0 예약 오케스트레이터의 SQLite 메타데이터 원장 도구.

이 원장은 FlowMarshal Engine의 권위 원장과 분리되어 있다. 예약 실행의 관찰,
라우팅 결정, 생성한 Codex 작업과 중복 방지 상태만 저장한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
WRITER_CONTRACT_VERSION = 2
LOCK_NAME = "scheduler"
TERMINAL_DISPATCH_STATUSES = {
    "completed",
    "failed",
    "cancelled",
    "blocked",
    "needs_attention",
    "creation_failed",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    lifecycle_status TEXT NOT NULL,
    current_phase TEXT NOT NULL,
    current_lane TEXT,
    blocker_code TEXT,
    blocker_fingerprint TEXT,
    active_dispatch_id TEXT,
    last_run_id TEXT,
    no_progress_count INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    decision_code TEXT,
    decision_lane TEXT,
    decision_reason TEXT,
    observed_dispatch_id TEXT,
    observed_thread_id TEXT,
    observed_turn_id TEXT,
    observed_status TEXT,
    created_dispatch_id TEXT,
    created_thread_id TEXT,
    created_client_thread_id TEXT,
    error_text TEXT,
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT PRIMARY KEY,
    purpose_key TEXT NOT NULL UNIQUE,
    lane TEXT NOT NULL,
    phase TEXT NOT NULL,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    assignment_digest TEXT,
    model TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    model_selection_reason TEXT NOT NULL,
    thread_id TEXT UNIQUE,
    client_thread_id TEXT UNIQUE,
    parent_thread_id TEXT,
    created_by_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    last_observed_at TEXT,
    last_turn_id TEXT,
    wait_cursor TEXT,
    terminal_outcome TEXT,
    summary_sha256 TEXT,
    FOREIGN KEY (created_by_run_id) REFERENCES orchestration_runs(run_id)
);

CREATE TABLE IF NOT EXISTS orchestration_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES orchestration_runs(run_id)
);

CREATE TABLE IF NOT EXISTS source_refs (
    source_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    sha256 TEXT,
    metadata_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_locks (
    lock_name TEXT PRIMARY KEY,
    owner_run_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON orchestration_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_dispatches_status
    ON dispatches(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_run
    ON orchestration_events(run_id, event_id);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def open_database(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "flowmarshal_writer_contract_version", 0, lambda: WRITER_CONTRACT_VERSION,
        deterministic=True,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)


def insert_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    run_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: Any | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO orchestration_events(
            run_id, occurred_at, event_type, entity_type, entity_id, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            isoformat(),
            event_type,
            entity_type,
            entity_id,
            canonical_json(payload if payload is not None else {}),
        ),
    )


def require_lock(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute(
        "SELECT owner_run_id, expires_at FROM orchestration_locks WHERE lock_name = ?",
        (LOCK_NAME,),
    ).fetchone()
    if row is None or row["owner_run_id"] != run_id:
        raise RuntimeError(f"run {run_id!r} does not own the orchestration lease")
    if row["expires_at"] <= isoformat():
        raise RuntimeError(f"orchestration lease for run {run_id!r} has expired")


def load_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for row in connection.execute("SELECT key, value_json FROM metadata ORDER BY key"):
        result[row["key"]] = json.loads(row["value_json"])
    return result


def load_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    state = row_to_dict(
        connection.execute(
            "SELECT * FROM orchestration_state WHERE singleton = 1"
        ).fetchone()
    )
    active_dispatch = None
    if state and state.get("active_dispatch_id"):
        active_dispatch = row_to_dict(
            connection.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?",
                (state["active_dispatch_id"],),
            ).fetchone()
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": load_metadata(connection),
        "state": state,
        "active_dispatch": active_dispatch,
    }


def command_init(args: argparse.Namespace) -> None:
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "automation_id": args.automation_id,
            "automation_name": args.automation_name,
            "goal": args.goal,
            "orchestrator_project_id": args.orchestrator_project_id,
            "development_project_id": args.development_project_id,
            "recovery_project_id": args.recovery_project_id,
            "test_project_id": args.test_project_id,
            "source_thread_id": args.source_thread_id,
            "test_example_thread_id": args.test_thread_id,
            "source_report": args.source_report,
            "source_report_sha256": args.source_report_sha256,
            "model_routing_policy": {
                "bounded_collection_or_repetitive_test": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "high",
                },
                "routine_implementation_or_integration_test": {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "high",
                },
                "multi_module_implementation_or_semantic_review": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                },
                "complex_root_cause_or_usage_ledger_recovery": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "max",
                },
                "release_architecture_or_final_cutover_audit": {
                    "model": "gpt-6-astra",
                    "reasoning_effort": "max",
                },
                "fast_mode": False,
                "silent_fallback": False,
            },
        }
        for key, value in metadata.items():
            connection.execute(
                """
                INSERT INTO metadata(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, canonical_json(value), now),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO orchestration_state(
                singleton, lifecycle_status, current_phase, current_lane,
                blocker_code, blocker_fingerprint, active_dispatch_id,
                last_run_id, no_progress_count, completed_at, updated_at, version
            ) VALUES (1, 'ACTIVE', ?, 'recovery', ?, ?, NULL, NULL, 0, NULL, ?, 1)
            """,
            (
                args.initial_phase,
                args.blocker_code,
                args.blocker_fingerprint,
                now,
            ),
        )
        sources = [
            (
                "implementation-report",
                "file",
                args.source_report,
                args.source_report_sha256,
                {"role": "handoff_evidence"},
            ),
            (
                "source-thread",
                "codex_thread",
                f"codex://threads/{args.source_thread_id}",
                None,
                {"role": "implementation_history"},
            ),
            (
                "test-example-thread",
                "codex_thread",
                f"codex://threads/{args.test_thread_id}",
                None,
                {"role": "test_session_example"},
            ),
        ]
        for source_id, source_kind, source_ref, sha256, source_metadata in sources:
            connection.execute(
                """
                INSERT INTO source_refs(
                    source_id, source_kind, source_ref, sha256, metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_kind = excluded.source_kind,
                    source_ref = excluded.source_ref,
                    sha256 = excluded.sha256,
                    metadata_json = excluded.metadata_json,
                    recorded_at = excluded.recorded_at
                """,
                (
                    source_id,
                    source_kind,
                    source_ref,
                    sha256,
                    canonical_json(source_metadata),
                    now,
                ),
            )
        insert_event(
            connection,
            event_type="ledger.initialized",
            entity_type="automation",
            entity_id=args.automation_id,
            payload={
                "schema_version": SCHEMA_VERSION,
                "initial_phase": args.initial_phase,
                "blocker_code": args.blocker_code,
            },
        )
        json_output(load_snapshot(connection))


def command_begin_run(args: argparse.Namespace) -> None:
    now_value = utc_now()
    now = isoformat(now_value)
    run_id = args.run_id or f"run_{uuid.uuid4().hex}"
    expires_at = isoformat(now_value + timedelta(seconds=args.lease_seconds))
    connection = open_database(args.db)
    try:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT * FROM orchestration_locks WHERE lock_name = ?",
            (LOCK_NAME,),
        ).fetchone()
        if current is not None and current["expires_at"] > now:
            connection.execute(
                """
                INSERT INTO orchestration_runs(
                    run_id, automation_id, started_at, finished_at, outcome,
                    decision_code, decision_reason
                ) VALUES (?, ?, ?, ?, 'SKIPPED_LOCKED', 'ACTIVE_LEASE', ?)
                """,
                (
                    run_id,
                    args.automation_id,
                    now,
                    now,
                    f"active lease owned by {current['owner_run_id']}",
                ),
            )
            insert_event(
                connection,
                run_id=run_id,
                event_type="run.skipped_locked",
                entity_type="lock",
                entity_id=LOCK_NAME,
                payload={
                    "owner_run_id": current["owner_run_id"],
                    "expires_at": current["expires_at"],
                },
            )
            connection.commit()
            json_output(
                {
                    "acquired": False,
                    "run_id": run_id,
                    "owner_run_id": current["owner_run_id"],
                    "expires_at": current["expires_at"],
                }
            )
            return
        stale_owner = current["owner_run_id"] if current is not None else None
        connection.execute(
            "DELETE FROM orchestration_locks WHERE lock_name = ?", (LOCK_NAME,)
        )
        connection.execute(
            """
            INSERT INTO orchestration_locks(lock_name, owner_run_id, acquired_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (LOCK_NAME, run_id, now, expires_at),
        )
        connection.execute(
            """
            INSERT INTO orchestration_runs(run_id, automation_id, started_at, snapshot_json)
            VALUES (?, ?, ?, '{}')
            """,
            (run_id, args.automation_id, now),
        )
        connection.execute(
            """
            UPDATE orchestration_state
            SET last_run_id = ?, updated_at = ?, version = version + 1
            WHERE singleton = 1
            """,
            (run_id, now),
        )
        insert_event(
            connection,
            run_id=run_id,
            event_type="run.started",
            entity_type="automation",
            entity_id=args.automation_id,
            payload={
                "lease_expires_at": expires_at,
                "replaced_stale_owner": stale_owner,
            },
        )
        snapshot = load_snapshot(connection)
        connection.execute(
            "UPDATE orchestration_runs SET snapshot_json = ? WHERE run_id = ?",
            (canonical_json(snapshot), run_id),
        )
        connection.commit()
        json_output(
            {
                "acquired": True,
                "run_id": run_id,
                "expires_at": expires_at,
                "snapshot": snapshot,
            }
        )
    finally:
        connection.close()


def command_show(args: argparse.Namespace) -> None:
    with open_database(args.db) as connection:
        create_schema(connection)
        result = load_snapshot(connection)
        result["recent_runs"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM orchestration_runs ORDER BY started_at DESC LIMIT ?",
                (args.run_limit,),
            )
        ]
        result["recent_dispatches"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM dispatches ORDER BY created_at DESC LIMIT ?",
                (args.dispatch_limit,),
            )
        ]
        json_output(result)


def command_reserve_dispatch(args: argparse.Namespace) -> None:
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        existing = connection.execute(
            "SELECT * FROM dispatches WHERE purpose_key = ?", (args.purpose_key,)
        ).fetchone()
        if existing is not None:
            connection.commit()
            json_output(
                {
                    "reserved": False,
                    "reason": "DUPLICATE_PURPOSE_KEY",
                    "dispatch": dict(existing),
                }
            )
            return
        state = connection.execute(
            "SELECT * FROM orchestration_state WHERE singleton = 1"
        ).fetchone()
        if state is None:
            raise RuntimeError("orchestration state is not initialized")
        if state["active_dispatch_id"]:
            active = connection.execute(
                "SELECT * FROM dispatches WHERE dispatch_id = ?",
                (state["active_dispatch_id"],),
            ).fetchone()
            connection.commit()
            json_output(
                {
                    "reserved": False,
                    "reason": "ACTIVE_DISPATCH_EXISTS",
                    "dispatch": dict(active) if active is not None else None,
                }
            )
            return
        dispatch_id = f"dispatch_{uuid.uuid4().hex}"
        connection.execute(
            """
            INSERT INTO dispatches(
                dispatch_id, purpose_key, lane, phase, project_id, title,
                assignment_digest, model, reasoning_effort, model_selection_reason,
                parent_thread_id, created_by_run_id, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
            """,
            (
                dispatch_id,
                args.purpose_key,
                args.lane,
                args.phase,
                args.project_id,
                args.title,
                args.assignment_digest,
                args.model,
                args.reasoning_effort,
                args.model_selection_reason,
                args.parent_thread_id,
                args.run_id,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE orchestration_state
            SET current_phase = ?, current_lane = ?, active_dispatch_id = ?,
                updated_at = ?, version = version + 1
            WHERE singleton = 1
            """,
            (args.phase, args.lane, dispatch_id, now),
        )
        connection.execute(
            "UPDATE orchestration_runs SET created_dispatch_id = ? WHERE run_id = ?",
            (dispatch_id, args.run_id),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="dispatch.reserved",
            entity_type="dispatch",
            entity_id=dispatch_id,
            payload={
                "purpose_key": args.purpose_key,
                "lane": args.lane,
                "phase": args.phase,
                "project_id": args.project_id,
                "title": args.title,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "model_selection_reason": args.model_selection_reason,
            },
        )
        connection.commit()
        json_output({"reserved": True, "dispatch_id": dispatch_id})


def command_confirm_dispatch(args: argparse.Namespace) -> None:
    if not args.thread_id and not args.client_thread_id:
        raise ValueError("--thread-id or --client-thread-id is required")
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        dispatch = connection.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ?", (args.dispatch_id,)
        ).fetchone()
        if dispatch is None:
            raise RuntimeError(f"unknown dispatch {args.dispatch_id!r}")
        connection.execute(
            """
            UPDATE dispatches
            SET thread_id = COALESCE(?, thread_id),
                client_thread_id = COALESCE(?, client_thread_id),
                status = ?, last_observed_at = ?
            WHERE dispatch_id = ?
            """,
            (
                args.thread_id,
                args.client_thread_id,
                args.status,
                now,
                args.dispatch_id,
            ),
        )
        connection.execute(
            """
            UPDATE orchestration_runs
            SET created_thread_id = COALESCE(?, created_thread_id),
                created_client_thread_id = COALESCE(?, created_client_thread_id)
            WHERE run_id = ?
            """,
            (args.thread_id, args.client_thread_id, args.run_id),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="dispatch.confirmed",
            entity_type="dispatch",
            entity_id=args.dispatch_id,
            payload={
                "thread_id": args.thread_id,
                "client_thread_id": args.client_thread_id,
                "status": args.status,
            },
        )
        connection.commit()
        json_output(
            row_to_dict(
                connection.execute(
                    "SELECT * FROM dispatches WHERE dispatch_id = ?",
                    (args.dispatch_id,),
                ).fetchone()
            )
        )


def command_observe_dispatch(args: argparse.Namespace) -> None:
    now = isoformat()
    summary_sha256 = args.summary_sha256
    if args.summary is not None:
        summary_sha256 = hashlib.sha256(args.summary.encode("utf-8")).hexdigest()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        dispatch = connection.execute(
            "SELECT * FROM dispatches WHERE dispatch_id = ?", (args.dispatch_id,)
        ).fetchone()
        if dispatch is None:
            raise RuntimeError(f"unknown dispatch {args.dispatch_id!r}")
        new_status = args.status or dispatch["status"]
        connection.execute(
            """
            UPDATE dispatches
            SET thread_id = COALESCE(?, thread_id),
                client_thread_id = COALESCE(?, client_thread_id),
                status = ?, last_observed_at = ?,
                last_turn_id = COALESCE(?, last_turn_id),
                wait_cursor = COALESCE(?, wait_cursor),
                terminal_outcome = COALESCE(?, terminal_outcome),
                summary_sha256 = COALESCE(?, summary_sha256)
            WHERE dispatch_id = ?
            """,
            (
                args.thread_id,
                args.client_thread_id,
                new_status,
                now,
                args.turn_id,
                args.cursor,
                args.terminal_outcome,
                summary_sha256,
                args.dispatch_id,
            ),
        )
        connection.execute(
            """
            UPDATE orchestration_runs
            SET observed_dispatch_id = ?,
                observed_thread_id = COALESCE(?, observed_thread_id),
                observed_turn_id = COALESCE(?, observed_turn_id),
                observed_status = ?
            WHERE run_id = ?
            """,
            (
                args.dispatch_id,
                args.thread_id or dispatch["thread_id"],
                args.turn_id,
                new_status,
                args.run_id,
            ),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="dispatch.observed",
            entity_type="dispatch",
            entity_id=args.dispatch_id,
            payload={
                "status": new_status,
                "thread_id": args.thread_id or dispatch["thread_id"],
                "turn_id": args.turn_id,
                "cursor": args.cursor,
                "terminal_outcome": args.terminal_outcome,
                "summary_sha256": summary_sha256,
            },
        )
        connection.commit()
        json_output(
            row_to_dict(
                connection.execute(
                    "SELECT * FROM dispatches WHERE dispatch_id = ?",
                    (args.dispatch_id,),
                ).fetchone()
            )
        )


def command_close_dispatch(args: argparse.Namespace) -> None:
    if args.status not in TERMINAL_DISPATCH_STATUSES:
        raise ValueError(
            f"terminal status must be one of {sorted(TERMINAL_DISPATCH_STATUSES)}"
        )
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        state = connection.execute(
            "SELECT active_dispatch_id FROM orchestration_state WHERE singleton = 1"
        ).fetchone()
        if state is None or state["active_dispatch_id"] != args.dispatch_id:
            raise RuntimeError("dispatch is not the active orchestration dispatch")
        connection.execute(
            """
            UPDATE dispatches
            SET status = ?, terminal_outcome = ?, last_observed_at = ?
            WHERE dispatch_id = ?
            """,
            (args.status, args.terminal_outcome, now, args.dispatch_id),
        )
        connection.execute(
            """
            UPDATE orchestration_state
            SET active_dispatch_id = NULL, updated_at = ?, version = version + 1
            WHERE singleton = 1
            """,
            (now,),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="dispatch.closed",
            entity_type="dispatch",
            entity_id=args.dispatch_id,
            payload={"status": args.status, "terminal_outcome": args.terminal_outcome},
        )
        connection.commit()
        json_output({"closed": True, "dispatch_id": args.dispatch_id})


def command_set_state(args: argparse.Namespace) -> None:
    changes = {
        "lifecycle_status": args.lifecycle_status,
        "current_phase": args.current_phase,
        "current_lane": args.current_lane,
        "blocker_code": args.blocker_code,
        "blocker_fingerprint": args.blocker_fingerprint,
        "no_progress_count": args.no_progress_count,
        "completed_at": args.completed_at,
    }
    selected = {key: value for key, value in changes.items() if value is not None}
    if not selected:
        raise ValueError("at least one state field is required")
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        assignments = [f"{key} = ?" for key in selected]
        values = list(selected.values())
        assignments.extend(["updated_at = ?", "version = version + 1"])
        values.extend([now])
        connection.execute(
            f"UPDATE orchestration_state SET {', '.join(assignments)} WHERE singleton = 1",
            values,
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="state.updated",
            entity_type="state",
            entity_id="1",
            payload=selected,
        )
        connection.commit()
        json_output(load_snapshot(connection)["state"])


def command_set_metadata(args: argparse.Namespace) -> None:
    value = json.loads(args.value_json)
    now = isoformat()
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        connection.execute(
            """
            INSERT INTO metadata(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (args.key, canonical_json(value), now),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="metadata.updated",
            entity_type="metadata",
            entity_id=args.key,
            payload={"value": value},
        )
        connection.commit()
        json_output({"updated": True, "key": args.key, "value": value})


def command_event(args: argparse.Namespace) -> None:
    payload = json.loads(args.payload_json)
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        insert_event(
            connection,
            run_id=args.run_id,
            event_type=args.event_type,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            payload=payload,
        )
        connection.commit()
        json_output({"recorded": True, "event_type": args.event_type})


def command_finish_run(args: argparse.Namespace) -> None:
    now = isoformat()
    snapshot = json.loads(args.snapshot_json)
    with open_database(args.db) as connection:
        create_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        require_lock(connection, args.run_id)
        connection.execute(
            """
            UPDATE orchestration_runs
            SET finished_at = ?, outcome = ?, decision_code = ?,
                decision_lane = ?, decision_reason = ?, error_text = ?,
                snapshot_json = ?
            WHERE run_id = ?
            """,
            (
                now,
                args.outcome,
                args.decision_code,
                args.decision_lane,
                args.decision_reason,
                args.error_text,
                canonical_json(snapshot),
                args.run_id,
            ),
        )
        insert_event(
            connection,
            run_id=args.run_id,
            event_type="run.finished",
            entity_type="run",
            entity_id=args.run_id,
            payload={
                "outcome": args.outcome,
                "decision_code": args.decision_code,
                "decision_lane": args.decision_lane,
                "decision_reason": args.decision_reason,
                "error_text": args.error_text,
            },
        )
        connection.execute(
            "DELETE FROM orchestration_locks WHERE lock_name = ? AND owner_run_id = ?",
            (LOCK_NAME, args.run_id),
        )
        connection.commit()
        json_output(
            row_to_dict(
                connection.execute(
                    "SELECT * FROM orchestration_runs WHERE run_id = ?", (args.run_id,)
                ).fetchone()
            )
        )


def command_verify(args: argparse.Namespace) -> None:
    with open_database(args.db) as connection:
        create_schema(connection)
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "metadata",
                "orchestration_state",
                "orchestration_runs",
                "dispatches",
                "orchestration_events",
                "source_refs",
            )
        }
        result = {
            "ok": integrity == ["ok"] and not foreign_keys,
            "integrity_check": integrity,
            "foreign_key_check": foreign_keys,
            "counts": counts,
        }
        json_output(result)
        if not result["ok"]:
            raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="오케스트레이션 SQLite 파일")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="schema와 최초 상태를 초기화한다")
    init_parser.add_argument("--automation-id", required=True)
    init_parser.add_argument("--automation-name", required=True)
    init_parser.add_argument("--goal", required=True)
    init_parser.add_argument("--initial-phase", required=True)
    init_parser.add_argument("--blocker-code", required=True)
    init_parser.add_argument("--blocker-fingerprint", required=True)
    init_parser.add_argument("--orchestrator-project-id", required=True)
    init_parser.add_argument("--development-project-id", required=True)
    init_parser.add_argument("--recovery-project-id", required=True)
    init_parser.add_argument("--test-project-id", required=True)
    init_parser.add_argument("--source-thread-id", required=True)
    init_parser.add_argument("--test-thread-id", required=True)
    init_parser.add_argument("--source-report", required=True)
    init_parser.add_argument("--source-report-sha256", required=True)
    init_parser.set_defaults(handler=command_init)

    begin_parser = subparsers.add_parser("begin-run", help="중복 실행 방지 lease를 얻는다")
    begin_parser.add_argument("--automation-id", required=True)
    begin_parser.add_argument("--run-id")
    begin_parser.add_argument("--lease-seconds", type=int, default=900)
    begin_parser.set_defaults(handler=command_begin_run)

    show_parser = subparsers.add_parser("show", help="현재 projection과 최근 이력을 읽는다")
    show_parser.add_argument("--run-limit", type=int, default=5)
    show_parser.add_argument("--dispatch-limit", type=int, default=5)
    show_parser.set_defaults(handler=command_show)

    reserve_parser = subparsers.add_parser(
        "reserve-dispatch", help="고유 목적 키로 새 작업 생성을 예약한다"
    )
    reserve_parser.add_argument("--run-id", required=True)
    reserve_parser.add_argument("--purpose-key", required=True)
    reserve_parser.add_argument("--lane", choices=("development", "recovery", "test"), required=True)
    reserve_parser.add_argument("--phase", required=True)
    reserve_parser.add_argument("--project-id", required=True)
    reserve_parser.add_argument("--title", required=True)
    reserve_parser.add_argument("--assignment-digest")
    reserve_parser.add_argument("--model", required=True)
    reserve_parser.add_argument("--reasoning-effort", required=True)
    reserve_parser.add_argument("--model-selection-reason", required=True)
    reserve_parser.add_argument("--parent-thread-id")
    reserve_parser.set_defaults(handler=command_reserve_dispatch)

    confirm_parser = subparsers.add_parser(
        "confirm-dispatch", help="create_thread 결과를 예약에 결속한다"
    )
    confirm_parser.add_argument("--run-id", required=True)
    confirm_parser.add_argument("--dispatch-id", required=True)
    confirm_parser.add_argument("--thread-id")
    confirm_parser.add_argument("--client-thread-id")
    confirm_parser.add_argument("--status", choices=("queued", "active"), required=True)
    confirm_parser.set_defaults(handler=command_confirm_dispatch)

    observe_parser = subparsers.add_parser(
        "observe-dispatch", help="작업의 최신 상태와 cursor를 기록한다"
    )
    observe_parser.add_argument("--run-id", required=True)
    observe_parser.add_argument("--dispatch-id", required=True)
    observe_parser.add_argument("--thread-id")
    observe_parser.add_argument("--client-thread-id")
    observe_parser.add_argument("--status")
    observe_parser.add_argument("--turn-id")
    observe_parser.add_argument("--cursor")
    observe_parser.add_argument("--terminal-outcome")
    observe_parser.add_argument("--summary")
    observe_parser.add_argument("--summary-sha256")
    observe_parser.set_defaults(handler=command_observe_dispatch)

    close_parser = subparsers.add_parser(
        "close-dispatch", help="종료된 작업을 닫고 활성 슬롯을 해제한다"
    )
    close_parser.add_argument("--run-id", required=True)
    close_parser.add_argument("--dispatch-id", required=True)
    close_parser.add_argument("--status", required=True)
    close_parser.add_argument("--terminal-outcome")
    close_parser.set_defaults(handler=command_close_dispatch)

    state_parser = subparsers.add_parser("set-state", help="현재 projection을 갱신한다")
    state_parser.add_argument("--run-id", required=True)
    state_parser.add_argument("--lifecycle-status")
    state_parser.add_argument("--current-phase")
    state_parser.add_argument("--current-lane")
    state_parser.add_argument("--blocker-code")
    state_parser.add_argument("--blocker-fingerprint")
    state_parser.add_argument("--no-progress-count", type=int)
    state_parser.add_argument("--completed-at")
    state_parser.set_defaults(handler=command_set_state)

    metadata_parser = subparsers.add_parser(
        "set-metadata", help="예약 작업의 고정·준고정 메타데이터를 갱신한다"
    )
    metadata_parser.add_argument("--run-id", required=True)
    metadata_parser.add_argument("--key", required=True)
    metadata_parser.add_argument("--value-json", required=True)
    metadata_parser.set_defaults(handler=command_set_metadata)

    event_parser = subparsers.add_parser("event", help="구조화 이벤트를 추가한다")
    event_parser.add_argument("--run-id", required=True)
    event_parser.add_argument("--event-type", required=True)
    event_parser.add_argument("--entity-type")
    event_parser.add_argument("--entity-id")
    event_parser.add_argument("--payload-json", default="{}")
    event_parser.set_defaults(handler=command_event)

    finish_parser = subparsers.add_parser("finish-run", help="실행 결과를 기록하고 lease를 해제한다")
    finish_parser.add_argument("--run-id", required=True)
    finish_parser.add_argument("--outcome", required=True)
    finish_parser.add_argument("--decision-code")
    finish_parser.add_argument("--decision-lane")
    finish_parser.add_argument("--decision-reason")
    finish_parser.add_argument("--error-text")
    finish_parser.add_argument("--snapshot-json", default="{}")
    finish_parser.set_defaults(handler=command_finish_run)

    verify_parser = subparsers.add_parser("verify", help="SQLite 무결성과 개수를 확인한다")
    verify_parser.set_defaults(handler=command_verify)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (ValueError, RuntimeError, sqlite3.Error, json.JSONDecodeError) as error:
        json_output({"ok": False, "error": type(error).__name__, "message": str(error)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
