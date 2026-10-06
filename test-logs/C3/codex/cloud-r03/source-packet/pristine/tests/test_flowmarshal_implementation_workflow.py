from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "flowmarshal_implementation_workflow.py"
LEDGER_PATH = ROOT / "scripts" / "flowmarshal_orchestrator_ledger.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


impl = load_module("flowmarshal_implementation_workflow", MODULE_PATH)
legacy = load_module("flowmarshal_orchestrator_ledger", LEDGER_PATH)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def connect(path: Path):
    connection = sqlite3.connect(path, factory=ClosingConnection)
    # 현재 schema의 fail-closed writer fence를 통과하는 writer를 모사한다.
    # 등록되지 않은 구 writer 검사는 sqlite3.connect를 직접 사용한다.
    connection.create_function(
        "flowmarshal_writer_contract_version", 0,
        lambda: getattr(impl, "WRITER_CONTRACT_VERSION", 2),
    )
    return connection


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ns(**values):
    return argparse.Namespace(**values)


class ImplementationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "ledger.sqlite3"
        self.codex_state_db = self.root / "codex-state.sqlite3"
        self.backups = self.root / "backups"
        self.plan = self.root / "approved-plan.md"
        self.plan.write_text("approved test plan\n", encoding="utf-8")
        self.proof = self.root / "proof.txt"
        self.proof.write_text("deterministic proof\n", encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        self._make_v1()
        self._make_codex_state()
        self.manifest = self._manifest()
        self._write_manifest(self.manifest)

    def _make_codex_state(self) -> None:
        with connect(self.codex_state_db) as connection:
            connection.executescript(
                """
                CREATE TABLE threads(
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL
                );
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
                CREATE TABLE project_roots(
                    project_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    PRIMARY KEY(project_id,position)
                );
                CREATE TABLE project_idempotency_keys(
                    key TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL
                );
                """
            )

    def _record_created_thread_policy(
        self, *, thread_id: str, project_id: str,
        sandbox_policy: dict | None = None, approval_policy: str = "never",
    ) -> dict:
        internal_id = f"internal-{project_id}"
        primary_root = self.root / internal_id
        primary_root.mkdir(exist_ok=True)
        with connect(self.codex_state_db) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO projects(id,name) VALUES(?,?)",
                (internal_id, f"Project {project_id}"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO project_roots(project_id,position,path) VALUES(?,?,?)",
                (internal_id, 0, str(primary_root)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO project_idempotency_keys(key,project_id) VALUES(?,?)",
                (project_id, internal_id),
            )
            connection.execute(
                """INSERT OR REPLACE INTO threads(id,cwd,sandbox_policy,approval_mode)
                   VALUES(?,?,?,?)""",
                (
                    thread_id, str(primary_root),
                    json.dumps(sandbox_policy or {"type": "disabled"}),
                    approval_policy,
                ),
            )
        return impl._observe_created_thread_policy(
            self.codex_state_db, thread_id=thread_id, external_project_id=project_id,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_v1(self) -> None:
        now = impl.isoformat()
        with connect(self.db) as connection:
            connection.executescript(legacy.SCHEMA_SQL)
            connection.execute(
                """INSERT INTO orchestration_state(singleton,lifecycle_status,current_phase,current_lane,
                   blocker_code,blocker_fingerprint,active_dispatch_id,last_run_id,no_progress_count,
                   completed_at,updated_at,version)
                   VALUES(1,'WAIT_EXTERNAL','WAIT_EXTERNAL_USAGE','recovery','BLOCKED_USAGE_UNKNOWN',
                   'receipt:usage_unknown',NULL,NULL,1,NULL,?,1)""",
                (now,),
            )
            metadata = {
                "schema_version": 1,
                "external_action_required": {"usage": None, "receipt": "preserve-me"},
                "scheduler_health": {"status": "HEALTHY", "verified_at": "stale"},
            }
            for key, value in metadata.items():
                connection.execute(
                    "INSERT INTO metadata(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, impl.canonical_json(value), now),
                )
            connection.execute(
                """INSERT INTO orchestration_runs(run_id,automation_id,started_at,finished_at,
                   outcome,snapshot_json) VALUES('legacy-run','legacy',?,?,?,'{}')""",
                (now, now, "DONE"),
            )
            connection.execute(
                """INSERT INTO dispatches(dispatch_id,purpose_key,lane,phase,project_id,title,
                   assignment_digest,model,reasoning_effort,model_selection_reason,parent_thread_id,
                   created_by_run_id,created_at,status)
                   VALUES('legacy-dispatch','legacy-purpose','recovery','old','old-project','old',NULL,
                   'old-model','high','history','old-parent','legacy-run',?,'needs_attention')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO orchestration_events(run_id,occurred_at,event_type,entity_type,
                   entity_id,payload_json) VALUES('legacy-run',?,'legacy.event','dispatch',
                   'legacy-dispatch','{}')""",
                (now,),
            )

    def _task(self, task_id: str, order: int, lane: str, depends: list[str]):
        non_bootstrap = lane != "bootstrap"
        return {
            "task_id": task_id,
            "order_index": order,
            "title": f"Task {task_id}",
            "objective": f"Complete {task_id}",
            "instructions": ["Do only this task"],
            "inputs": [str(self.plan)],
            "outputs": ["report"],
            "prohibited_effects": ["external publication"],
            "lane": lane,
            "project_id": "project-development" if non_bootstrap else None,
            "model": "gpt-test-terra" if non_bootstrap else None,
            "reasoning_effort": "high" if non_bootstrap else None,
            "model_selection_reason": "test binding",
            "depends_on": depends,
            "checks": [{
                "check_id": f"{task_id}-C1",
                "criterion": "deterministic evidence exists",
                "method": "inspect exact file hash",
                "required": True,
            }],
        }

    def _manifest(self):
        return {
            "workflow_id": impl.WORKFLOW_ID,
            "revision": 1,
            "title": "Test implementation workflow",
            "approval": {
                "user_request": "Implement approved plan",
                "plan_document": str(self.plan),
                "plan_sha256": digest(self.plan),
            },
            "baseline_commit": "a" * 40,
            "source_root": str(self.root),
            "artifact_root": str(self.root / "artifacts"),
            "automation_id": "automation-test",
            "parent_thread_id": "parent-test",
            "policies": {
                "fast_mode": False,
                "usage_missing_blocks_execution": False,
                "recovery_model": "gpt-test-sol",
                "recovery_effort": "high",
                "architecture_recovery_model": "gpt-test-astra",
                "architecture_recovery_effort": "high",
                "recovery_project_id": "project-recovery",
                "max_same_failure_recoveries": 2,
                "max_total_recoveries": 5,
            },
            "tasks": [
                self._task("FM-00", 0, "bootstrap", []),
                self._task("FM-01", 1, "development", ["FM-00"]),
            ],
        }

    def _write_manifest(self, manifest) -> None:
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _revision2_manifest(self) -> dict:
        manifest = copy.deepcopy(self.manifest)
        manifest["revision"] = 2
        manifest["parent_revision"] = 1
        manifest["parent_manifest"] = {
            "revision": 1,
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": digest(self.manifest_path),
        }
        manifest["registration_status"] = "READY_FOR_REGISTRATION"
        manifest["registration_guard"] = {
            "database_registration_allowed": True,
            "activation_allowed": True,
            "automation_change_allowed": False,
            "reason": "unit-test revision registration and activation are authorized",
        }
        manifest["lineage"] = [
            {
                "from_task_id": "FM-00", "to_task_id": "FM-00",
                "relation": "unchanged", "decision": "task meaning is unchanged",
            },
            {
                "from_task_id": "FM-01", "to_task_id": "FM-01-A",
                "relation": "split", "decision": "separate preparation",
            },
            {
                "from_task_id": "FM-01", "to_task_id": "FM-01-B",
                "relation": "split", "decision": "separate execution",
            },
        ]
        manifest["carry_forward"] = [
            {
                "from_task_id": "FM-00", "to_task_id": "FM-00",
                "decision": "reuse only the immutable PASS verified PASS evidence",
            }
        ]
        manifest["tasks"] = [
            copy.deepcopy(self.manifest["tasks"][0]),
            self._task("FM-01-A", 1, "development", ["FM-00"]),
            self._task("FM-01-B", 2, "development", ["FM-01-A"]),
        ]
        return manifest

    def _write_revision2(self, manifest: dict | None = None) -> Path:
        path = self.root / "manifest-revision-2.json"
        path.write_text(
            json.dumps(manifest or self._revision2_manifest(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _activation_decision(
        self, *, generation: int = 1, from_revision: int = 1, to_revision: int = 2,
    ) -> Path:
        path = self.root / f"revision-{from_revision}-to-{to_revision}-activation.json"
        with connect(self.db) as connection:
            target = connection.execute(
                """SELECT spec_sha256,source_root,baseline_commit
                   FROM implementation_workflows WHERE workflow_id=? AND workflow_revision=?""",
                (impl.WORKFLOW_ID, to_revision),
            ).fetchone()
        self.assertIsNotNone(target)
        path.write_text(json.dumps({
            "schema_version": 1,
            "workflow_id": impl.WORKFLOW_ID,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "expected_generation": generation,
            "target_spec_sha256": target[0],
            "source_snapshot": impl._capture_source_snapshot(target[1], target[2]),
            "outcome": "PASS",
            "reviewer": {"kind": "independent", "id": "unit-test-auditor"},
            "observed_at": impl.isoformat(),
            "findings": [],
            "files": [{"path": str(self.proof), "sha256": digest(self.proof)}],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def migrate(self):
        return impl.command_migrate(ns(
            db=str(self.db), manifest=str(self.manifest_path), backup_dir=str(self.backups)
        ))

    def begin(self):
        return impl.command_begin(ns(
            db=str(self.db), automation_id="automation-test", lease_seconds=900
        ))

    def register_revision(self, manifest: dict | None = None):
        return impl.command_register_revision(ns(
            db=str(self.db), manifest=str(self._write_revision2(manifest))
        ))

    def activate_revision(
        self, *, revision: int = 2, generation: int = 1, from_revision: int = 1,
    ):
        return impl.command_activate_revision(ns(
            db=str(self.db), revision=revision, expected_generation=generation,
            decision_file=str(self._activation_decision(
                generation=generation, from_revision=from_revision, to_revision=revision,
            )),
        ))

    def activate_revision2_fixture(self, *, fm00_bound_freshness: bool = False):
        if fm00_bound_freshness:
            self.manifest["tasks"][0]["evidence_contract"] = "bound"
            self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(
            run["run_id"], freshness_contract="bound" if fm00_bound_freshness else None,
        )
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="settle revision 1 before registering revision 2",
        ))
        self.register_revision()
        return self.activate_revision()

    def evidence(
        self, task_id: str, *, dispatch=None, finding=None, name="evidence.json",
        freshness_contract: str | None = None,
    ) -> Path:
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            head_revision = (
                connection.execute(
                    """SELECT active_workflow_revision FROM implementation_workflow_heads
                       WHERE workflow_id=?""",
                    (impl.WORKFLOW_ID,),
                ).fetchone()[0]
                if impl.table_exists(connection, "implementation_workflow_heads") else 1
            )
            workflow_revision = (
                dispatch.get("workflow_revision") if dispatch else None
            ) or head_revision
            task = connection.execute(
                """SELECT * FROM implementation_tasks
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=?""",
                (impl.WORKFLOW_ID, workflow_revision, task_id),
            ).fetchone()
            checks = [row[0] for row in connection.execute(
                """SELECT check_id FROM implementation_task_checks
                   WHERE workflow_id=? AND workflow_revision=? AND task_id=? ORDER BY check_id""",
                (impl.WORKFLOW_ID, workflow_revision, task_id),
            )]
        item = {"path": str(self.proof), "sha256": digest(self.proof)}
        data = {
            "schema_version": 1,
            "workflow_id": impl.WORKFLOW_ID,
            "workflow_revision": workflow_revision,
            "task_id": task_id,
            "task_revision": task["task_revision"],
            "task_spec_sha256": task["spec_sha256"],
            "dispatch_id": dispatch["dispatch_id"] if dispatch else None,
            "assignment_sha256": dispatch["assignment_sha256"] if dispatch else None,
            "reviewer": {"kind": "deterministic", "id": "unit-test"},
            "observed_at": impl.isoformat(),
            "files": [item],
            "checks": [{
                "check_id": check_id,
                "status": "FAIL" if finding else "PASS",
                "method": "exact test command and file digest inspection",
                "detail": "command exit=0; compared exact proof SHA-256" if not finding else "command exit=1; exact failure reproduced",
                "evidence_refs": [str(self.proof)],
            } for check_id in checks],
            "finding": finding,
        }
        if freshness_contract is not None:
            data["freshness"] = {
                "contract": freshness_contract,
                "scopes": ["dependency", "completion"],
                "bindings": [{"kind": "proof", **item}],
            }
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def pass_fm00(self, run_id: str, *, freshness_contract: str | None = None):
        path = self.evidence(
            "FM-00", name="fm00-evidence.json", freshness_contract=freshness_contract,
        )
        return impl.command_review(ns(
            db=str(self.db), run_id=run_id, task_id="FM-00", evidence_file=str(path)
        ))

    def reserve_fm01(self, run_id: str):
        return impl.command_reserve_next(ns(db=str(self.db), run_id=run_id))

    def claim_dispatch(self, run_id: str, dispatch: dict) -> dict:
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                          j.attempt_no,j.assignment_sha256,j.confirmed_at
                   FROM dispatches d JOIN implementation_task_dispatches j
                     ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
                (dispatch["dispatch_id"],),
            ).fetchone())
        claim, _ = impl._claim_launch_intent(
            str(self.db), run_id=run_id, row=row, intent_origin="fresh",
        )
        return claim

    def create_receipt(self, dispatch, *, thread_id=None, client_thread_id=None, name=None) -> Path:
        if bool(thread_id) == bool(client_thread_id):
            raise AssertionError("test receipt에는 정확히 하나의 identity가 필요합니다")
        data = {"hostId": "local"}
        if thread_id:
            data["threadId"] = thread_id
        else:
            data["clientThreadId"] = client_thread_id
        if client_thread_id:
            envelope = {
                "schema_version": 2,
                "dispatch_id": dispatch["dispatch_id"],
                "assignment_sha256": dispatch["assignment_sha256"],
                "request": dispatch["create_thread"],
                "request_sha256": dispatch["create_thread_request_sha256"],
                "captured_at": impl.isoformat(),
                "raw_response": data,
            }
        else:
            with connect(self.db) as connection:
                connection.row_factory = sqlite3.Row
                row = dict(connection.execute(
                    """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                              j.attempt_no,j.assignment_sha256,j.confirmed_at
                       FROM dispatches d JOIN implementation_task_dispatches j
                         ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
                    (dispatch["dispatch_id"],),
                ).fetchone())
                existing_claim = connection.execute(
                    "SELECT * FROM implementation_launch_claims WHERE dispatch_id=?",
                    (dispatch["dispatch_id"],),
                ).fetchone()
            if existing_claim is None:
                claim, _ = impl._claim_launch_intent(
                    str(self.db), run_id=row["created_by_run_id"], row=row,
                    intent_origin="fresh",
                )
            else:
                claim = dict(existing_claim)
            envelope = {
            "schema_version": 4,
            "dispatch_id": dispatch["dispatch_id"],
            "assignment_sha256": dispatch["assignment_sha256"],
            "request": dispatch["create_thread"],
            "request_sha256": dispatch["create_thread_request_sha256"],
            "captured_at": impl.isoformat(),
            "raw_response": data,
            "execution_policy": self._record_created_thread_policy(
                thread_id=thread_id,
                project_id=dispatch["create_thread"]["target"]["projectId"],
            ),
        }
        path = self.root / (name or f"{dispatch['dispatch_id']}-create-receipt.json")
        path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        if thread_id and claim["resolved_at"] is None:
            impl._resolve_launch_claim(
                str(self.db), run_id=row["created_by_run_id"],
                dispatch_id=dispatch["dispatch_id"],
                assignment_sha256=dispatch["assignment_sha256"], thread_id=thread_id,
                receipt_path=path.resolve(), receipt_sha256=digest(path),
            )
        return path

    def policy_retry_decision(self, task_id: str, dispatch: dict) -> Path:
        with connect(self.db) as connection:
            task = connection.execute(
                "SELECT workflow_revision,task_revision,active_attempt_no "
                "FROM implementation_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT evidence_sha256 FROM implementation_task_attempts
                   WHERE task_id=? AND attempt_no=?""",
                (task_id, task[2]),
            ).fetchone()
        files = [
            {"path": str(MODULE_PATH), "sha256": digest(MODULE_PATH)},
            {"path": str(Path(__file__).resolve()), "sha256": digest(Path(__file__).resolve())},
        ]
        path = self.root / "policy-retry-decision.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "workflow_id": impl.WORKFLOW_ID,
            "workflow_revision": task[0],
            "task_id": task_id,
            "task_revision": task[1],
            "attempt_no": task[2],
            "dispatch_id": dispatch["dispatch_id"],
            "assignment_sha256": dispatch["assignment_sha256"],
            "failure_evidence_sha256": attempt[0],
            "action": "RETRY_SAME_TASK",
            "remediation": {
                "kind": "dispatch_policy_receipt_v4",
                "required_permission_profile": ":danger-full-access",
                "required_approval_policy": "never",
                "files": files,
                "validation_command": "python -m unittest tests.test_flowmarshal_implementation_workflow",
                "validation_exit_code": 0,
                "validated_at": impl.isoformat(),
            },
            "approval": {
                "kind": "user",
                "source": "codex_thread",
                "source_thread_id": "parent-test",
                "statement": "fix the root cause and reactivate after validation",
            },
            "approved_at": impl.isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_json_output_is_ascii_safe_and_round_trips_unicode(self):
        output = io.StringIO()
        value = {"title": "Planning·ProjectMap 계약 정직성"}
        with redirect_stdout(output):
            impl.json_output(value)
        rendered = output.getvalue()
        self.assertTrue(rendered.isascii())
        self.assertEqual(value, json.loads(rendered))

    def resolution_receipt(self, dispatch, *, client_thread_id, thread_id, name="resolution.json") -> Path:
        data = {
            "schema_version": 1,
            "status": "resolved",
            "dispatch_id": dispatch["dispatch_id"],
            "assignment_sha256": dispatch["assignment_sha256"],
            "client_thread_id": client_thread_id,
            "thread_id": thread_id,
            "observed_at": impl.isoformat(),
            "raw_observation": {
                "clientThreadId": client_thread_id,
                "threadId": thread_id,
                "status": "completed",
            },
        }
        path = self.root / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def interrupt_receipt(
        self, dispatch, *, thread_id: str, turn_id: str, origin: str = "unknown",
        request_id: str | None = None, name: str = "interrupt.json",
    ) -> Path:
        raw_observation = {
            "threadId": thread_id,
            "turnId": turn_id,
            "status": "interrupted",
        }
        if origin != "unknown":
            raw_observation["interruptOrigin"] = origin
        if request_id is not None:
            raw_observation["requestId"] = request_id
        data = {
            "schema_version": 1,
            "status": "interrupted",
            "dispatch_id": dispatch["dispatch_id"],
            "assignment_sha256": dispatch["assignment_sha256"],
            "automation_id": "automation-test",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "origin": origin,
            "request_id": request_id,
            "reason": "provider reported an interrupted turn without a final handoff",
            "observed_at": impl.isoformat(),
            "raw_observation": raw_observation,
        }
        path = self.root / name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path

    def interruption_reopen_decision(
        self, dispatch, interrupt_receipt: Path, *, task_id: str,
        name: str = "interruption-reopen-decision.json",
    ) -> Path:
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """SELECT t.workflow_id,t.workflow_revision,t.task_id,t.task_revision,
                          a.attempt_no,a.evidence_sha256,j.assignment_sha256,j.receipt_sha256
                   FROM implementation_tasks t
                   JOIN implementation_task_attempts a
                     ON a.workflow_id=t.workflow_id AND a.workflow_revision=t.workflow_revision
                    AND a.task_id=t.task_id AND a.task_revision=t.task_revision
                    AND a.attempt_no=t.active_attempt_no
                   JOIN implementation_task_dispatches j
                     ON j.workflow_id=a.workflow_id AND j.workflow_revision=a.workflow_revision
                    AND j.task_id=a.task_id AND j.task_revision=a.task_revision
                    AND j.attempt_no=a.attempt_no
                   WHERE t.task_id=? AND j.dispatch_id=?""",
                (task_id, dispatch["dispatch_id"]),
            ).fetchone()
        data = {
            "schema_version": 1,
            "workflow_id": row["workflow_id"],
            "workflow_revision": row["workflow_revision"],
            "task_id": row["task_id"],
            "task_revision": row["task_revision"],
            "attempt_no": row["attempt_no"],
            "dispatch_id": dispatch["dispatch_id"],
            "assignment_sha256": row["assignment_sha256"],
            "failure_evidence_sha256": row["evidence_sha256"],
            "creation_receipt_sha256": row["receipt_sha256"],
            "interrupt_receipt_sha256": digest(interrupt_receipt),
            "action": "RETRY_SAME_TASK",
            "approval": {
                "kind": "user",
                "source": "codex_thread",
                "source_thread_id": "unit-test-user-thread",
                "statement": "같은 recovery Task를 append-only로 다시 실행한다.",
            },
            "approved_at": impl.isoformat(),
        }
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def historical_interrupted_recovery(self):
        self.migrate()
        first_run = self.begin()
        self.pass_fm00(first_run["run_id"])
        original = self.reserve_fm01(first_run["run_id"])
        original_thread = "thread-original-failure"
        impl.command_confirm(ns(
            db=str(self.db), run_id=first_run["run_id"], dispatch_id=original["dispatch_id"],
            thread_id=original_thread, client_thread_id=None,
            receipt_file=str(self.create_receipt(original, thread_id=original_thread)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=first_run["run_id"], dispatch_id=original["dispatch_id"],
            status="failed", cursor="original-failure", turn_id="turn-original-failure",
            summary_sha256="4" * 64, thread_id=original_thread, client_thread_id=None,
            interrupt_receipt_file=None,
        ))
        original_finding = {
            "failure_class": "implementation", "fingerprint": "5" * 64,
            "summary": "original failure requires one recovery task", "remediable": True,
            "scope_expansion_required": False,
        }
        original_evidence = self.evidence(
            "FM-01", dispatch=original, finding=original_finding,
            name="original-failure-evidence.json",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=first_run["run_id"], task_id="FM-01",
            evidence_file=str(original_evidence),
        ))
        registered = impl.command_register_recovery(ns(
            db=str(self.db), run_id=first_run["run_id"], task_id="FM-01",
            evidence_file=str(original_evidence),
        ))
        impl.command_finish(ns(
            db=str(self.db), run_id=first_run["run_id"], outcome="CONTINUE",
            reason="start recovery in a distinct one-dispatch run",
        ))

        second_run = self.begin()
        recovery = impl.command_reserve_next(ns(db=str(self.db), run_id=second_run["run_id"]))
        self.assertEqual(recovery["task_id"], registered["recovery_task_id"])
        recovery_thread = "thread-historical-interruption"
        recovery_turn = "turn-historical-interruption"
        impl.command_confirm(ns(
            db=str(self.db), run_id=second_run["run_id"],
            dispatch_id=recovery["dispatch_id"], thread_id=recovery_thread,
            client_thread_id=None,
            receipt_file=str(self.create_receipt(recovery, thread_id=recovery_thread)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=second_run["run_id"],
            dispatch_id=recovery["dispatch_id"], status="failed",
            cursor="historical-interruption", turn_id=recovery_turn,
            summary_sha256="6" * 64, thread_id=recovery_thread, client_thread_id=None,
            interrupt_receipt_file=None,
        ))
        interruption_finding = {
            "failure_class": "environment", "fingerprint": "7" * 64,
            "summary": "worker turn was interrupted before handoff", "remediable": True,
            "scope_expansion_required": False,
        }
        recovery_evidence = self.evidence(
            recovery["task_id"], dispatch=recovery, finding=interruption_finding,
            name="historical-interruption-failure-evidence.json",
        )
        recovery_evidence_data = json.loads(recovery_evidence.read_text(encoding="utf-8"))
        recovery_evidence_data["checks"][0]["status"] = "NOT_RUN"
        recovery_evidence_data["checks"][0]["detail"] = (
            "worker was interrupted before the required recovery check ran"
        )
        recovery_evidence.write_text(
            json.dumps(recovery_evidence_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=second_run["run_id"], task_id=recovery["task_id"],
            evidence_file=str(recovery_evidence),
        ))
        interrupt = self.interrupt_receipt(
            recovery, thread_id=recovery_thread, turn_id=recovery_turn,
            name="historical-interrupt-receipt.json",
        )
        decision = self.interruption_reopen_decision(
            recovery, interrupt, task_id=recovery["task_id"],
        )
        return {
            "run": second_run, "original": original, "recovery": recovery,
            "recovery_task_id": recovery["task_id"], "interrupt": interrupt,
            "decision": decision,
        }

    def test_migration_backup_history_idempotence_tamper_cycle_and_readonly(self):
        before = {
            "runs": 1, "dispatches": 1, "events": 1,
            "legacy_digest": None, "usage": {"usage": None, "receipt": "preserve-me"},
        }
        result = self.migrate()
        self.assertTrue(result["migrated"])
        backup = Path(result["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(digest(backup), result["backup_sha256"])
        with connect(self.db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orchestration_runs").fetchone()[0], before["runs"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], before["dispatches"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orchestration_events WHERE event_type='legacy.event'").fetchone()[0], before["events"])
            self.assertIsNone(connection.execute("SELECT assignment_digest FROM dispatches WHERE dispatch_id='legacy-dispatch'").fetchone()[0])
            usage = json.loads(connection.execute("SELECT value_json FROM metadata WHERE key='external_action_required'").fetchone()[0])
            self.assertEqual(usage, before["usage"])
            state = connection.execute("SELECT lifecycle_status,current_phase FROM orchestration_state").fetchone()
            self.assertEqual(state, ("ACTIVE", "IMPLEMENTATION"))
            health = json.loads(connection.execute("SELECT value_json FROM metadata WHERE key='scheduler_health'").fetchone()[0])
            self.assertEqual(health["status"], "CONFIGURED_PENDING_SCHEDULED_OBSERVATION")
        self.assertTrue(impl.command_migrate(ns(
            db=str(self.db), manifest=str(self.manifest_path), backup_dir=str(self.backups)
        ))["idempotent"])

        before_read = digest(self.db)
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["task_id"], "FM-00")
        impl.command_task(ns(db=str(self.db), task_id="FM-00"))
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])
        self.assertEqual(digest(self.db), before_read)
        missing_db = self.root / "missing" / "db.sqlite3"
        with self.assertRaises(ValueError):
            impl.command_status(ns(db=str(missing_db)))
        self.assertFalse(missing_db.parent.exists())

        altered = copy.deepcopy(self.manifest)
        altered["title"] = "tampered same revision"
        self._write_manifest(altered)
        with self.assertRaises(RuntimeError):
            impl.command_migrate(ns(db=str(self.db), manifest=str(self.manifest_path), backup_dir=str(self.backups)))

        performance_blocked = copy.deepcopy(self.manifest)
        performance_blocked["policies"]["performance_comparison_blocks_release"] = True
        self._write_manifest(performance_blocked)
        with self.assertRaises(ValueError):
            impl.load_manifest(str(self.manifest_path))

        cycle = copy.deepcopy(self.manifest)
        cycle["tasks"][0]["depends_on"] = ["FM-01"]
        self._write_manifest(cycle)
        with self.assertRaises(ValueError):
            impl.load_manifest(str(self.manifest_path))

    def test_revision5_requires_typed_ancestor_inputs_and_evidence_contract(self):
        parent_path = self.root / "manifest-revision-4.json"
        parent_path.write_text("{}\n", encoding="utf-8")
        manifest = self._revision2_manifest()
        manifest["revision"] = 5
        manifest["parent_revision"] = 4
        manifest["parent_manifest"] = {
            "revision": 4,
            "manifest_path": str(parent_path),
            "manifest_sha256": digest(parent_path),
        }
        for task in manifest["tasks"]:
            task["input_task_refs"] = []
            if task["task_id"] != "FM-00":
                task["evidence_contract"] = "bound"

        validated = impl.validate_manifest(copy.deepcopy(manifest))
        self.assertEqual(validated["revision"], 5)

        off_ancestry = copy.deepcopy(manifest)
        target = next(task for task in off_ancestry["tasks"] if task["task_id"] == "FM-01-B")
        target["depends_on"] = ["FM-00"]
        target["inputs"].append("FM-01-A result")
        target["input_task_refs"] = ["FM-01-A"]
        with self.assertRaisesRegex(ValueError, "dependency ancestry"):
            impl.validate_manifest(off_ancestry)

        undeclared = copy.deepcopy(manifest)
        target = next(task for task in undeclared["tasks"] if task["task_id"] == "FM-01-B")
        target["inputs"].append("FM-01-A result")
        with self.assertRaisesRegex(ValueError, "inputs의 Task 참조"):
            impl.validate_manifest(undeclared)

        unknown = copy.deepcopy(manifest)
        target = next(task for task in unknown["tasks"] if task["task_id"] == "FM-01-B")
        target["inputs"].append("FM-99-UNKNOWN result")
        with self.assertRaisesRegex(ValueError, "알 수 없는 Task 참조"):
            impl.validate_manifest(unknown)

        missing_contract = copy.deepcopy(manifest)
        target = next(task for task in missing_contract["tasks"] if task["task_id"] == "FM-01-A")
        target.pop("evidence_contract")
        with self.assertRaisesRegex(ValueError, "evidence_contract"):
            impl.validate_manifest(missing_contract)

    def test_fm00_gate_one_active_binding_and_completed_is_not_done(self):
        self.migrate()
        run = self.begin()
        self.assertEqual(run["decision"], "READY")
        direct = impl.command_reserve_next(ns(db=str(self.db), run_id=run["run_id"]))
        self.assertEqual(direct["action"], "DIRECT_REVIEW")
        missing = self.root / "missing-evidence.json"
        with self.assertRaises(ValueError):
            impl.command_review(ns(db=str(self.db), run_id=run["run_id"], task_id="FM-00", evidence_file=str(missing)))
        self.assertEqual(self.pass_fm00(run["run_id"])["outcome"], "PASS")
        reserved = self.reserve_fm01(run["run_id"])
        self.assertEqual(reserved["task_id"], "FM-01")
        self.assertNotIn("project_id", reserved["create_thread"])
        self.assertEqual(reserved["create_thread"]["target"]["environment"], {"type": "local"})
        with self.assertRaises(RuntimeError):
            self.reserve_fm01(run["run_id"])
        receipt = self.create_receipt(reserved, thread_id="thread-1")
        confirmed = impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-1", client_thread_id=None,
            receipt_file=str(receipt)
        ))
        self.assertTrue(confirmed["confirmed"])
        idem = impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-1", client_thread_id=None,
            receipt_file=str(receipt)
        ))
        self.assertTrue(idem["idempotent"])
        with self.assertRaises(RuntimeError):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-other", client_thread_id=None,
                receipt_file=str(self.create_receipt(reserved, thread_id="thread-other", name="other.json"))
            ))
        observed = impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="cursor-1", turn_id="turn-1", summary_sha256="b" * 64,
            thread_id="thread-1", client_thread_id=None
        ))
        self.assertEqual(observed["task_status"], "AWAITING_REVIEW")
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(status["decision"], "OBSERVE")
        self.assertEqual(status["active_dispatch"]["dispatch_id"], reserved["dispatch_id"])
        task = impl.command_task(ns(db=str(self.db), task_id="FM-01"))
        self.assertEqual(task["current_dispatch"]["assignment_sha256"], reserved["assignment_sha256"])

    def test_confirm_rejects_create_request_that_differs_from_assignment(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        receipt = self.create_receipt(reserved, thread_id="thread-corrupt")
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["request"]["prompt"] += "\ncorrupted"
        data["request_sha256"] = impl.sha256_bytes(
            impl.canonical_json(data["request"]).encode("utf-8")
        )
        receipt.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "request.prompt"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-corrupt", client_thread_id=None, receipt_file=str(receipt),
            ))

    def test_capture_create_receipt_binds_actual_full_access_policy(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        thread_id = "thread-policy-ok"
        self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        raw = self.root / "raw-create.json"
        raw.write_text(json.dumps({"hostId": "local", "threadId": thread_id}), encoding="utf-8")
        receipt = self.root / "captured-create-receipt.json"
        captured = impl.command_capture_create_receipt(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            raw_response_file=str(raw), codex_state_db=str(self.codex_state_db),
            output=str(receipt),
        ))
        self.assertTrue(captured["captured"])
        self.assertEqual(captured["permission_profile"], ":danger-full-access")
        confirmed = impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None, receipt_file=str(receipt),
        ))
        self.assertTrue(confirmed["confirmed"])

    def test_capture_create_receipt_accepts_windows_extended_length_cwd(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        thread_id = "thread-policy-extended-cwd"
        policy = self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        extended_cwd = "\\\\?\\" + policy["cwd"]
        with connect(self.codex_state_db) as connection:
            connection.execute(
                "UPDATE threads SET cwd=? WHERE id=?", (extended_cwd, thread_id)
            )
        raw = self.root / "raw-create-extended-cwd.json"
        raw.write_text(
            json.dumps({"hostId": "local", "threadId": thread_id}), encoding="utf-8"
        )
        receipt = self.root / "captured-create-extended-cwd-receipt.json"

        captured = impl.command_capture_create_receipt(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            raw_response_file=str(raw), codex_state_db=str(self.codex_state_db),
            output=str(receipt),
        ))

        self.assertTrue(captured["captured"])
        envelope = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(extended_cwd, envelope["execution_policy"]["cwd"])

    def test_same_path_accepts_only_drive_and_unc_extended_namespaces(self):
        self.assertTrue(impl._same_path(r"C:\project", r"\\?\C:\project"))
        self.assertTrue(
            impl._same_path(
                r"\\server\share\project", r"\\?\UNC\server\share\project",
            )
        )
        self.assertFalse(
            impl._same_path(
                r"\\?\GLOBALROOT\Device\HarddiskVolume1\project",
                Path.cwd() / r"GLOBALROOT\Device\HarddiskVolume1\project",
            )
        )
        self.assertFalse(
            impl._same_path(
                r"\\?\Volume{00000000-0000-0000-0000-000000000000}\project",
                Path.cwd() / r"Volume{00000000-0000-0000-0000-000000000000}\project",
            )
        )
        self.assertFalse(impl._same_path(r"C:\project", r"D:\project"))

    def test_capture_create_receipt_rejects_workspace_policy_before_confirm(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        thread_id = "thread-policy-mismatch"
        project_id = reserved["create_thread"]["target"]["projectId"]
        self._record_created_thread_policy(thread_id=thread_id, project_id=project_id)
        with connect(self.codex_state_db) as connection:
            connection.execute(
                "UPDATE threads SET sandbox_policy=?,approval_mode=? WHERE id=?",
                (json.dumps({"type": "managed"}), "on-request", thread_id),
            )
        raw = self.root / "raw-create-mismatch.json"
        raw.write_text(json.dumps({"hostId": "local", "threadId": thread_id}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "PERMISSION_POLICY_MISMATCH"):
            impl.command_capture_create_receipt(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                raw_response_file=str(raw), codex_state_db=str(self.codex_state_db),
                output=str(self.root / "must-not-exist.json"),
            ))
        with connect(self.db) as connection:
            state = connection.execute(
                "SELECT status,thread_id,client_thread_id FROM dispatches WHERE dispatch_id=?",
                (reserved["dispatch_id"],),
            ).fetchone()
        self.assertEqual(state, (impl.UNCONFIRMED_DISPATCH_STATUS, None, None))

    def test_launch_codex_exec_uses_explicit_full_access_and_captures_v4_receipt(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-cli-launch"
        self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        output_dir = self.root / "launch"
        seen = {}

        class FakeProcess:
            pid = 4321

            @staticmethod
            def poll():
                return None

        def fake_popen(command, **kwargs):
            seen["command"] = command
            seen["cwd"] = kwargs["cwd"]
            kwargs["stdout"].write(
                (json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n").encode()
            )
            kwargs["stdout"].flush()
            return FakeProcess()

        with mock.patch.object(impl.subprocess, "Popen", side_effect=fake_popen) as popen:
            launched = impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))
            repeated = impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))
        self.assertTrue(launched["launched"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(popen.call_count, 1)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", seen["command"])
        self.assertNotIn("--approve-for-me", seen["command"])
        self.assertEqual(seen["command"][-1], "-")
        receipt = json.loads(Path(launched["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 4)
        self.assertEqual(receipt["execution_policy"]["approval_policy"], "never")
        self.assertEqual(receipt["execution_policy"]["sandbox_policy"], {"type": "disabled"})
        with connect(self.db) as connection:
            claim = connection.execute(
                """SELECT intent_origin,process_id,thread_id,receipt_sha256,resolved_by_run_id
                   FROM implementation_launch_claims WHERE dispatch_id=?""",
                (reserved["dispatch_id"],),
            ).fetchone()
        self.assertEqual(claim[0], "fresh")
        self.assertEqual(claim[1], 4321)
        self.assertEqual(claim[2], thread_id)
        self.assertEqual(claim[3], launched["receipt_sha256"])
        self.assertEqual(claim[4], run["run_id"])
        confirmed = impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=launched["receipt_path"],
        ))
        self.assertTrue(confirmed["confirmed"])
        self.assertTrue(impl.command_verify(ns(db=str(self.db), revision=None))["ok"])

    def test_launch_codex_exec_retries_transient_policy_observation(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-cli-transient-policy"
        self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        output_dir = self.root / "launch-transient"

        class FakeProcess:
            pid = 4322

            @staticmethod
            def poll():
                return None

        def fake_popen(command, **kwargs):
            kwargs["stdout"].write(
                (json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n").encode()
            )
            kwargs["stdout"].flush()
            return FakeProcess()

        original_observe = impl._observe_created_thread_policy
        observation_count = 0

        def transient_observe(*args, **kwargs):
            nonlocal observation_count
            observation_count += 1
            if observation_count == 1:
                raise ValueError(
                    "PERMISSION_POLICY_MISMATCH: created thread approval policy가 never가 아닙니다"
                )
            return original_observe(*args, **kwargs)

        with mock.patch.object(impl.subprocess, "Popen", side_effect=fake_popen) as popen, \
                mock.patch.object(
                    impl, "_observe_created_thread_policy", side_effect=transient_observe,
                ):
            launched = impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))

        self.assertTrue(launched["launched"])
        self.assertEqual(popen.call_count, 1)
        self.assertGreaterEqual(observation_count, 3)
        self.assertTrue(Path(launched["receipt_path"]).is_file())

    def test_launch_codex_exec_rechecks_lease_after_streams_open_before_popen(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        project_id = reserved["create_thread"]["target"]["projectId"]
        self._record_created_thread_policy(
            thread_id="unrelated-stream-fence", project_id=project_id,
        )
        output_dir = self.root / "launch-stream-fence"
        events_path = Path(f"{output_dir / reserved['dispatch_id']}-events.jsonl")
        original_open = Path.open
        expired = False

        def expire_during_stream_open(path, *args, **kwargs):
            nonlocal expired
            stream = original_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if Path(path) == events_path and mode == "wb" and not expired:
                with connect(self.db) as connection:
                    connection.execute(
                        "UPDATE orchestration_locks SET expires_at=? WHERE lock_name=?",
                        (impl.isoformat(impl.utc_now() - timedelta(seconds=1)), impl.LOCK_NAME),
                    )
                expired = True
            return stream

        with mock.patch.object(Path, "open", autospec=True, side_effect=expire_during_stream_open), \
                mock.patch.object(impl.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "lease가 만료|LAUNCH_CLAIM_FENCE_LOST"):
            impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))
        self.assertTrue(expired)
        popen.assert_not_called()

    def test_launch_codex_exec_rejects_claimless_receipt_and_direct_confirm(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-claimless-receipt"
        policy = self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        output_dir = self.root / "launch-claimless-receipt"
        output_dir.mkdir()
        receipt_path = Path(
            f"{output_dir / reserved['dispatch_id']}-create-receipt-v4.json"
        )
        receipt_path.write_text(json.dumps({
            "schema_version": 4,
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "captured_at": impl.isoformat(),
            "request": reserved["create_thread"],
            "request_sha256": reserved["create_thread_request_sha256"],
            "raw_response": {"hostId": "local", "threadId": thread_id},
            "execution_policy": policy,
        }, indent=2), encoding="utf-8")
        arguments = ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
            codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
            start_timeout_seconds=1.0,
        )
        with mock.patch.object(impl.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "LAUNCH_CLAIM_REQUIRED"):
            impl.command_launch_codex_exec(arguments)
        popen.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "LAUNCH_CLAIM_REQUIRED"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"], thread_id=thread_id,
                client_thread_id=None, receipt_file=str(receipt_path),
            ))

    def test_launch_codex_exec_requires_one_thread_started_for_existing_receipt(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        thread_id = "thread-receipt-without-events"
        policy = self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        output_dir = self.root / "launch-receipt-without-events"
        output_dir.mkdir()
        receipt_path = Path(
            f"{output_dir / reserved['dispatch_id']}-create-receipt-v4.json"
        )
        receipt_path.write_text(json.dumps({
            "schema_version": 4,
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "captured_at": impl.isoformat(),
            "request": reserved["create_thread"],
            "request_sha256": reserved["create_thread_request_sha256"],
            "raw_response": {"hostId": "local", "threadId": thread_id},
            "execution_policy": policy,
        }, indent=2), encoding="utf-8")
        with mock.patch.object(impl.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "identity가 유일하지"):
            impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))
        popen.assert_not_called()

    def test_launch_codex_exec_recovers_existing_launch_without_relaunch(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        thread_id = "thread-cli-existing-launch"
        self._record_created_thread_policy(
            thread_id=thread_id,
            project_id=reserved["create_thread"]["target"]["projectId"],
        )
        output_dir = self.root / "launch-existing"
        output_dir.mkdir()
        prefix = output_dir / reserved["dispatch_id"]
        prompt_path = Path(f"{prefix}-prompt.txt")
        events_path = Path(f"{prefix}-events.jsonl")
        prompt_path.write_text(
            reserved["create_thread"]["prompt"], encoding="utf-8", newline="\n",
        )
        events_path.write_text(
            json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
            encoding="utf-8", newline="\n",
        )

        with mock.patch.object(impl.subprocess, "Popen") as popen:
            recovered = impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))

        popen.assert_not_called()
        self.assertFalse(recovered["launched"])
        self.assertTrue(recovered["recovered"])
        receipt = json.loads(Path(recovered["receipt_path"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 4)
        self.assertEqual(receipt["raw_response"]["threadId"], thread_id)
        with connect(self.db) as connection:
            claim = connection.execute(
                """SELECT intent_origin,process_id,thread_id,receipt_sha256
                   FROM implementation_launch_claims WHERE dispatch_id=?""",
                (reserved["dispatch_id"],),
            ).fetchone()
        self.assertEqual(claim, ("fresh", None, thread_id, recovered["receipt_sha256"]))
        confirmed = impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"],
            dispatch_id=reserved["dispatch_id"], thread_id=thread_id,
            client_thread_id=None, receipt_file=recovered["receipt_path"],
        ))
        self.assertTrue(confirmed["confirmed"])
        self.assertTrue(impl.command_verify(ns(db=str(self.db), revision=None))["ok"])

    def test_launch_codex_exec_rejects_multiple_thread_started_identities(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.claim_dispatch(run["run_id"], reserved)
        project_id = reserved["create_thread"]["target"]["projectId"]
        self._record_created_thread_policy(thread_id="thread-one", project_id=project_id)
        output_dir = self.root / "launch-ambiguous-identities"
        output_dir.mkdir()
        prefix = output_dir / reserved["dispatch_id"]
        Path(f"{prefix}-prompt.txt").write_text(
            reserved["create_thread"]["prompt"], encoding="utf-8", newline="\n",
        )
        Path(f"{prefix}-events.jsonl").write_text(
            "\n".join((
                json.dumps({"type": "thread.started", "thread_id": "thread-one"}),
                json.dumps({"type": "thread.started", "thread_id": "thread-two"}),
                "",
            )),
            encoding="utf-8", newline="\n",
        )
        with mock.patch.object(impl.subprocess, "Popen") as popen, \
                self.assertRaisesRegex(RuntimeError, "identity가 유일하지"):
            impl.command_launch_codex_exec(ns(
                db=str(self.db), run_id=run["run_id"],
                dispatch_id=reserved["dispatch_id"],
                codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
                codex_state_db=str(self.codex_state_db), output_dir=str(output_dir),
                start_timeout_seconds=1.0,
            ))
        popen.assert_not_called()
        self.assertFalse(Path(f"{prefix}-create-receipt-v4.json").exists())

    def test_launch_claim_compare_and_set_has_one_winner_under_barrier(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                          j.attempt_no,j.assignment_sha256,j.confirmed_at
                   FROM dispatches d JOIN implementation_task_dispatches j
                     ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
                (reserved["dispatch_id"],),
            ).fetchone())
        barrier = threading.Barrier(3)
        outcomes: list[bool] = []
        failures: list[Exception] = []

        def claim_once():
            try:
                barrier.wait()
                _, acquired = impl._claim_launch_intent(
                    str(self.db), run_id=run["run_id"], row=row, intent_origin="fresh",
                )
                outcomes.append(acquired)
            except Exception as error:
                failures.append(error)

        workers = [threading.Thread(target=claim_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse(failures)
        self.assertEqual(sorted(outcomes), [False, True])
        with connect(self.db) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM implementation_launch_claims WHERE dispatch_id=?",
                (reserved["dispatch_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_launch_claim_survives_lease_takeover_and_blocks_all_relaunches(self):
        self.migrate()
        first_run = self.begin()
        self.pass_fm00(first_run["run_id"])
        reserved = self.reserve_fm01(first_run["run_id"])
        project_id = reserved["create_thread"]["target"]["projectId"]
        self._record_created_thread_policy(
            thread_id="unrelated-project-bootstrap", project_id=project_id,
        )
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            row = dict(connection.execute(
                """SELECT d.*,j.workflow_id,j.workflow_revision,j.task_id,j.task_revision,
                          j.attempt_no,j.assignment_sha256,j.confirmed_at
                   FROM dispatches d JOIN implementation_task_dispatches j
                     ON j.dispatch_id=d.dispatch_id WHERE d.dispatch_id=?""",
                (reserved["dispatch_id"],),
            ).fetchone())
        _, acquired = impl._claim_launch_intent(
            str(self.db), run_id=first_run["run_id"], row=row, intent_origin="fresh",
        )
        self.assertTrue(acquired)
        with connect(self.db) as connection:
            connection.execute(
                "UPDATE orchestration_locks SET expires_at=? WHERE lock_name=?",
                (impl.isoformat(impl.utc_now() - timedelta(seconds=1)), impl.LOCK_NAME),
            )
        second_run = self.begin()
        self.assertTrue(second_run["acquired"])
        arguments = dict(
            db=str(self.db), dispatch_id=reserved["dispatch_id"],
            codex_executable=str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"),
            codex_state_db=str(self.codex_state_db),
            output_dir=str(self.root / "claim-takeover"), start_timeout_seconds=1.0,
        )
        with mock.patch.object(impl.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "LAUNCH_INTENT_UNCERTAIN"):
                impl.command_launch_codex_exec(ns(run_id=second_run["run_id"], **arguments))
            with self.assertRaisesRegex(RuntimeError, "lease owner"):
                impl.command_launch_codex_exec(ns(run_id=first_run["run_id"], **arguments))
        popen.assert_not_called()

    def test_terminal_observation_is_idempotent_and_never_regresses_to_running(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-terminal-monotonic"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id)),
        ))
        terminal = dict(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="terminal-cursor", turn_id="terminal-turn",
            summary_sha256="9" * 64, thread_id=thread_id, client_thread_id=None,
            binding_receipt_file=None, interrupt_receipt_file=None,
        )
        first = impl.command_observe(ns(**terminal))
        repeated = impl.command_observe(ns(**terminal))
        self.assertEqual(first["task_status"], "AWAITING_REVIEW")
        self.assertTrue(repeated["idempotent"])
        missing_cursor = dict(terminal)
        missing_cursor["cursor"] = None
        with self.assertRaisesRegex(RuntimeError, "STALE_DISPATCH_OBSERVATION"):
            impl.command_observe(ns(**missing_cursor))
        with self.assertRaisesRegex(RuntimeError, "STALE_DISPATCH_OBSERVATION"):
            impl.command_observe(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                status="running", cursor="late-running", turn_id=None,
                summary_sha256=None, thread_id=thread_id, client_thread_id=None,
                binding_receipt_file=None, interrupt_receipt_file=None,
            ))
        conflicting = dict(terminal)
        conflicting.update(status="failed", summary_sha256="a" * 64)
        with self.assertRaisesRegex(RuntimeError, "STALE_DISPATCH_OBSERVATION"):
            impl.command_observe(ns(**conflicting))
        with connect(self.db) as connection:
            dispatch = connection.execute(
                "SELECT status,last_turn_id,wait_cursor,summary_sha256 FROM dispatches WHERE dispatch_id=?",
                (reserved["dispatch_id"],),
            ).fetchone()
            task = connection.execute(
                "SELECT status FROM implementation_tasks WHERE task_id='FM-01'"
            ).fetchone()[0]
            attempt = connection.execute(
                "SELECT status FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()[0]
        self.assertEqual(dispatch, ("completed", "terminal-turn", "terminal-cursor", "9" * 64))
        self.assertEqual((task, attempt), ("AWAITING_REVIEW", "AWAITING_REVIEW"))

    def test_policy_mismatch_reopens_same_task_without_duplicate_recovery(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-policy-failed"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="failed", cursor="policy-failed", turn_id="turn-policy-failed",
            summary_sha256="8" * 64, thread_id=thread_id, client_thread_id=None,
            binding_receipt_file=None, interrupt_receipt_file=None,
        ))
        fingerprint = hashlib.sha256(b"PERMISSION_POLICY_MISMATCH:workspace-on-request").hexdigest()
        finding = {
            "failure_class": "environment",
            "fingerprint": fingerprint,
            "summary": "PERMISSION_POLICY_MISMATCH: child used workspace-write/on-request",
            "remediable": True,
            "scope_expansion_required": False,
        }
        failure = self.evidence(
            "FM-01", dispatch=reserved, finding=finding, name="policy-failure.json",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(failure),
        ))
        decision = self.policy_retry_decision("FM-01", reserved)
        mutable_remediation = self.root / "mutable-remediation.txt"
        mutable_remediation.write_text("validated remediation\n", encoding="utf-8")
        decision_data = json.loads(decision.read_text(encoding="utf-8"))
        decision_data["remediation"]["files"].append({
            "path": str(mutable_remediation),
            "sha256": digest(mutable_remediation),
        })
        decision.write_text(
            json.dumps(decision_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        reopened = impl.command_reopen_policy_mismatch(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            dispatch_id=reserved["dispatch_id"], decision_file=str(decision),
        ))
        self.assertTrue(reopened["reopened"])
        self.assertEqual(reopened["next_attempt_no"], 2)
        immutable_copy = (
            Path(self.manifest["artifact_root"]) / impl.POLICY_REMEDIATION_ARCHIVE_DIR
            / decision_data["remediation"]["files"][-1]["sha256"]
            / mutable_remediation.name
        )
        self.assertEqual(
            digest(immutable_copy), decision_data["remediation"]["files"][-1]["sha256"],
        )
        mutable_remediation.write_text("changed after reopen\n", encoding="utf-8")
        repeated = impl.command_reopen_policy_mismatch(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            dispatch_id=reserved["dispatch_id"], decision_file=str(decision),
        ))
        self.assertTrue(repeated["idempotent"])
        with connect(self.db) as connection:
            task = connection.execute(
                "SELECT status,active_attempt_no,attempt_count FROM implementation_tasks "
                "WHERE task_id='FM-01'"
            ).fetchone()
            history = connection.execute(
                "SELECT status,evidence_sha256 FROM implementation_task_attempts "
                "WHERE task_id='FM-01' AND attempt_no=1"
            ).fetchone()
            recovery_count = connection.execute(
                "SELECT COUNT(*) FROM implementation_recoveries"
            ).fetchone()[0]
        self.assertEqual(task, ("PENDING", None, 1))
        self.assertEqual(history[0], "FAILED")
        self.assertIsNotNone(history[1])
        self.assertEqual(recovery_count, 0)
        self.assertTrue(impl.command_verify(ns(db=str(self.db), revision=None))["ok"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="same task reopened for a policy-gated next attempt",
        ))
        next_run = self.begin()
        next_dispatch = impl.command_reserve_next(ns(
            db=str(self.db), run_id=next_run["run_id"],
        ))
        self.assertEqual((next_dispatch["task_id"], next_dispatch["attempt_no"]), ("FM-01", 2))

    def test_confirm_rejects_replacement_character_and_legacy_envelope(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])

        corrupted = self.create_receipt(reserved, thread_id="thread-corrupt-title")
        corrupted_data = json.loads(corrupted.read_text(encoding="utf-8"))
        corrupted_data["request"]["title"] = "Planning\ufffdProjectMap"
        corrupted.write_text(json.dumps(corrupted_data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "U\\+FFFD"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-corrupt-title", client_thread_id=None,
                receipt_file=str(corrupted),
            ))

        legacy = self.root / "legacy-create-receipt.json"
        legacy.write_text(json.dumps({
            "schema_version": 1,
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "captured_at": impl.isoformat(),
            "raw_response": {"hostId": "local", "threadId": "thread-legacy"},
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "request.*결속"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-legacy", client_thread_id=None, receipt_file=str(legacy),
            ))

    def test_confirm_rejects_legacy_utf8_correction_receipt_v3_without_policy(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        initial_request = copy.deepcopy(reserved["create_thread"])
        initial_request["prompt"] = initial_request["prompt"].replace("FlowMarshal", "FlowMarshal\ufffd", 1)
        receipt = self.root / "corrected-create-receipt.json"
        receipt.write_text(json.dumps({
            "schema_version": 3,
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "captured_at": impl.isoformat(),
            "request": initial_request,
            "request_sha256": impl.sha256_bytes(
                impl.canonical_json(initial_request).encode("utf-8")
            ),
            "raw_response": {"hostId": "local", "threadId": "thread-corrected"},
            "correction": {
                "prompt": reserved["create_thread"]["prompt"],
                "prompt_sha256": reserved["assignment_sha256"],
                "sent_at": impl.isoformat(),
                "raw_response": {"hostId": "local", "threadId": "thread-corrected"},
            },
        }, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "receipt v4"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-corrected", client_thread_id=None, receipt_file=str(receipt),
            ))

    def test_confirm_rejects_utf8_correction_that_is_not_exact_assignment(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        initial_request = copy.deepcopy(reserved["create_thread"])
        initial_request["prompt"] = initial_request["prompt"].replace("FlowMarshal", "FlowMarshal\ufffd", 1)
        wrong_prompt = reserved["create_thread"]["prompt"] + "\nnot exact"
        receipt = self.root / "wrong-correction-receipt.json"
        receipt.write_text(json.dumps({
            "schema_version": 3,
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "captured_at": impl.isoformat(),
            "request": initial_request,
            "request_sha256": impl.sha256_bytes(
                impl.canonical_json(initial_request).encode("utf-8")
            ),
            "raw_response": {"hostId": "local", "threadId": "thread-wrong-correction"},
            "correction": {
                "prompt": wrong_prompt,
                "prompt_sha256": impl.sha256_bytes(wrong_prompt.encode("utf-8")),
                "sent_at": impl.isoformat(),
                "raw_response": {"hostId": "local", "threadId": "thread-wrong-correction"},
            },
        }, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "receipt v4"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-wrong-correction", client_thread_id=None,
                receipt_file=str(receipt),
            ))

    def test_expired_lease_and_old_client_binding_preserve_unknown_intent(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-persisted"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id))
        ))
        skipped = self.begin()
        self.assertFalse(skipped["acquired"])
        self.assertEqual(skipped["outcome"], "SKIPPED_LOCKED")
        with connect(self.db) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM orchestration_events WHERE run_id=? AND event_type='implementation_run.skipped_locked'",
                (skipped["run_id"],),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT owner_run_id FROM orchestration_locks WHERE lock_name='scheduler'"
            ).fetchone()[0], run["run_id"])
        with connect(self.db) as connection:
            old = impl.isoformat(impl.utc_now() - timedelta(hours=2))
            connection.execute("UPDATE orchestration_locks SET expires_at=?", (old,))
            connection.execute("UPDATE dispatches SET created_at=? WHERE dispatch_id=?", (old, reserved["dispatch_id"]))
        with self.assertRaises(RuntimeError):
            impl.command_finish(ns(
                db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
                reason="만료된 owner는 새 owner 전에도 fence되어야 한다",
            ))
        next_run = self.begin()
        self.assertTrue(next_run["acquired"])
        self.assertEqual(next_run["decision"], "OBSERVE")
        with connect(self.db) as connection:
            row = connection.execute(
                "SELECT status,thread_id FROM dispatches WHERE dispatch_id=?", (reserved["dispatch_id"],)
            ).fetchone()
            active = connection.execute("SELECT active_dispatch_id FROM orchestration_state").fetchone()[0]
            old_outcome = connection.execute("SELECT outcome FROM orchestration_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0]
        self.assertEqual(row, ("active", thread_id))
        self.assertEqual(active, reserved["dispatch_id"])
        self.assertEqual(old_outcome, "ABANDONED")

    def test_confirm_requires_exact_raw_receipt_and_rejects_unverified_client_identity(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        with self.assertRaises(ValueError):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-lost", client_thread_id=None, receipt_file=None,
            ))
        wrong = self.create_receipt(reserved, thread_id="thread-other", name="wrong-receipt.json")
        with self.assertRaises(ValueError):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-lost", client_thread_id=None, receipt_file=str(wrong),
            ))
        wrong_binding = self.create_receipt(
            reserved, thread_id="thread-lost", name="wrong-binding.json"
        )
        wrong_binding_data = json.loads(wrong_binding.read_text(encoding="utf-8"))
        wrong_binding_data["dispatch_id"] = "dispatch_other"
        wrong_binding.write_text(json.dumps(wrong_binding_data), encoding="utf-8")
        with self.assertRaises(ValueError):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id="thread-lost", client_thread_id=None,
                receipt_file=str(wrong_binding),
            ))
        receipt = self.create_receipt(
            reserved, client_thread_id="client-pending", name="client-receipt.json"
        )
        with self.assertRaisesRegex(ValueError, "receipt v4"):
            impl.command_confirm(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                thread_id=None, client_thread_id="client-pending", receipt_file=str(receipt),
            ))
        with connect(self.db) as connection:
            state = connection.execute(
                "SELECT status,thread_id,client_thread_id FROM dispatches WHERE dispatch_id=?",
                (reserved["dispatch_id"],),
            ).fetchone()
        self.assertEqual(state, (impl.UNCONFIRMED_DISPATCH_STATUS, None, None))

    def test_interrupted_turn_requires_bound_provenance_and_never_becomes_failure(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-interrupted"
        turn_id = "turn-interrupted"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id)),
        ))
        with self.assertRaisesRegex(ValueError, "interrupt-receipt-file"):
            impl.command_observe(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                status="interrupted", cursor="interrupt-cursor", turn_id=turn_id,
                summary_sha256=None, thread_id=thread_id, client_thread_id=None,
                interrupt_receipt_file=None,
            ))

        forged = self.interrupt_receipt(
            reserved, thread_id=thread_id, turn_id=turn_id,
            origin="app_lifecycle", name="forged-interrupt.json",
        )
        forged_data = json.loads(forged.read_text(encoding="utf-8"))
        del forged_data["raw_observation"]["interruptOrigin"]
        forged.write_text(json.dumps(forged_data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "직접 근거"):
            impl.command_observe(ns(
                db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
                status="interrupted", cursor="interrupt-cursor", turn_id=turn_id,
                summary_sha256=None, thread_id=thread_id, client_thread_id=None,
                interrupt_receipt_file=str(forged),
            ))

        receipt = self.interrupt_receipt(
            reserved, thread_id=thread_id, turn_id=turn_id,
        )
        observed = impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="interrupted", cursor="interrupt-cursor", turn_id=turn_id,
            summary_sha256=None, thread_id=thread_id, client_thread_id=None,
            interrupt_receipt_file=str(receipt),
        ))
        self.assertEqual(observed["decision"], "USER_DECISION_REQUIRED")
        self.assertEqual(observed["action"], "INTERRUPTION_REQUIRES_DECISION")
        self.assertEqual(observed["interrupt"]["origin"], "unknown")
        self.assertIsNone(observed["failure_fingerprint"])
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(status["decision"], "USER_DECISION_REQUIRED")
        self.assertEqual(status["action"], "INTERRUPTION_REQUIRES_DECISION")
        with connect(self.db) as connection:
            task = connection.execute(
                "SELECT status,failure_fingerprint FROM implementation_tasks WHERE task_id='FM-01'"
            ).fetchone()
            attempt = connection.execute(
                "SELECT status,failure_fingerprint FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()
            active = connection.execute(
                "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
            ).fetchone()[0]
        self.assertEqual(task, ("DISPATCHED", None))
        self.assertEqual(attempt, ("INTERRUPTED_OBSERVED", None))
        self.assertEqual(active, reserved["dispatch_id"])
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])

        receipt_data = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_data["reason"] = "tampered after observation"
        receipt.write_text(json.dumps(receipt_data), encoding="utf-8")
        verification = impl.command_verify(ns(db=str(self.db)))
        self.assertFalse(verification["ok"])
        self.assertIn(
            f"interrupt_receipt_binding:{reserved['dispatch_id']}", verification["errors"],
        )

    def test_reopen_historical_interruption_is_append_only_idempotent_and_reserves_attempt_two(self):
        fixture = self.historical_interrupted_recovery()
        run_id = fixture["run"]["run_id"]
        task_id = fixture["recovery_task_id"]
        command = dict(
            db=str(self.db), run_id=run_id, task_id=task_id,
            dispatch_id=fixture["recovery"]["dispatch_id"],
            interrupt_receipt_file=str(fixture["interrupt"]),
            decision_file=str(fixture["decision"]),
        )
        conflicting = self.root / "conflicting-interruption-decision.json"
        conflicting_data = json.loads(fixture["decision"].read_text(encoding="utf-8"))
        conflicting_data["approval"]["statement"] = "different decision for the same attempt"
        conflicting.write_text(
            json.dumps(conflicting_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        with connect(self.db) as connection:
            before_history = {
                "attempt": connection.execute(
                    "SELECT * FROM implementation_task_attempts WHERE task_id=? AND attempt_no=1",
                    (task_id,),
                ).fetchone(),
                "dispatch": connection.execute(
                    "SELECT * FROM implementation_task_dispatches WHERE dispatch_id=?",
                    (fixture["recovery"]["dispatch_id"],),
                ).fetchone(),
                "evidence": connection.execute(
                    "SELECT evidence_sha256,evidence_json,outcome FROM implementation_evidence "
                    "WHERE task_id=? AND attempt_no=1", (task_id,),
                ).fetchone(),
                "recoveries": connection.execute(
                    "SELECT COUNT(*) FROM implementation_recoveries"
                ).fetchone()[0],
            }

        before_dry_run = digest(self.db)
        dry_run = impl.command_reopen_interrupted(ns(**command, dry_run=True))
        self.assertTrue(dry_run["dry_run"])
        self.assertFalse(dry_run["reopened"])
        self.assertEqual(dry_run["decision"], "READY")
        self.assertEqual(digest(self.db), before_dry_run)

        reopened = impl.command_reopen_interrupted(ns(**command, dry_run=False))
        self.assertTrue(reopened["reopened"])
        self.assertEqual(reopened["decision"], "READY")
        self.assertEqual(reopened["previous_attempt_no"], 1)
        self.assertEqual(reopened["next_attempt_no"], 2)
        self.assertEqual(reopened["recovery_total_count"], before_history["recoveries"])
        repeated = impl.command_reopen_interrupted(ns(**command, dry_run=False))
        self.assertTrue(repeated["idempotent"])
        with self.assertRaisesRegex(RuntimeError, "다른 사용자 결정"):
            impl.command_reopen_interrupted(ns(
                **{**command, "decision_file": str(conflicting)}, dry_run=False,
            ))

        with connect(self.db) as connection:
            task = connection.execute(
                "SELECT status,attempt_count,active_attempt_no,failure_fingerprint "
                "FROM implementation_tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            after_history = {
                "attempt": connection.execute(
                    "SELECT * FROM implementation_task_attempts WHERE task_id=? AND attempt_no=1",
                    (task_id,),
                ).fetchone(),
                "dispatch": connection.execute(
                    "SELECT * FROM implementation_task_dispatches WHERE dispatch_id=?",
                    (fixture["recovery"]["dispatch_id"],),
                ).fetchone(),
                "evidence": connection.execute(
                    "SELECT evidence_sha256,evidence_json,outcome FROM implementation_evidence "
                    "WHERE task_id=? AND attempt_no=1", (task_id,),
                ).fetchone(),
                "recoveries": connection.execute(
                    "SELECT COUNT(*) FROM implementation_recoveries"
                ).fetchone()[0],
                "events": connection.execute(
                    "SELECT COUNT(*) FROM orchestration_events "
                    "WHERE event_type='implementation_task.interruption_reopened' AND entity_id=?",
                    (task_id,),
                ).fetchone()[0],
            }
        self.assertEqual(task, ("PENDING", 1, None, None))
        self.assertEqual(after_history["attempt"], before_history["attempt"])
        self.assertEqual(after_history["dispatch"], before_history["dispatch"])
        self.assertEqual(after_history["evidence"], before_history["evidence"])
        self.assertEqual(after_history["recoveries"], before_history["recoveries"])
        self.assertEqual(after_history["events"], 1)
        self.assertTrue(impl.command_verify(ns(db=str(self.db), revision=None))["ok"])

        impl.command_finish(ns(
            db=str(self.db), run_id=run_id, outcome="CONTINUE",
            reason="reopen transaction complete",
        ))
        next_run = self.begin()
        reserved = impl.command_reserve_next(ns(db=str(self.db), run_id=next_run["run_id"]))
        self.assertEqual(reserved["task_id"], task_id)
        self.assertEqual(reserved["attempt_no"], 2)
        self.assertEqual(
            reserved["create_thread"]["target"],
            {"type": "project", "projectId": "project-recovery", "environment": {"type": "local"}},
        )

    def test_reopen_historical_interruption_rejects_receipt_and_decision_binding_mismatches(self):
        fixture = self.historical_interrupted_recovery()
        base = dict(
            db=str(self.db), run_id=fixture["run"]["run_id"],
            task_id=fixture["recovery_task_id"],
            dispatch_id=fixture["recovery"]["dispatch_id"], dry_run=False,
        )
        bad_receipt = self.root / "bad-thread-interrupt.json"
        bad_receipt_data = json.loads(fixture["interrupt"].read_text(encoding="utf-8"))
        bad_receipt_data["thread_id"] = "wrong-thread"
        bad_receipt_data["raw_observation"]["threadId"] = "wrong-thread"
        bad_receipt.write_text(json.dumps(bad_receipt_data, indent=2), encoding="utf-8")
        bad_receipt_decision = self.interruption_reopen_decision(
            fixture["recovery"], bad_receipt, task_id=fixture["recovery_task_id"],
            name="bad-thread-decision.json",
        )
        with self.assertRaisesRegex(ValueError, "thread_id binding"):
            impl.command_reopen_interrupted(ns(
                **base, interrupt_receipt_file=str(bad_receipt),
                decision_file=str(bad_receipt_decision),
            ))
        bad_turn_receipt = self.root / "bad-turn-interrupt.json"
        bad_turn_data = json.loads(fixture["interrupt"].read_text(encoding="utf-8"))
        bad_turn_data["turn_id"] = "wrong-turn"
        bad_turn_data["raw_observation"]["turnId"] = "wrong-turn"
        bad_turn_receipt.write_text(json.dumps(bad_turn_data, indent=2), encoding="utf-8")
        bad_turn_decision = self.interruption_reopen_decision(
            fixture["recovery"], bad_turn_receipt, task_id=fixture["recovery_task_id"],
            name="bad-turn-decision.json",
        )
        with self.assertRaisesRegex(ValueError, "turn_id binding"):
            impl.command_reopen_interrupted(ns(
                **base, interrupt_receipt_file=str(bad_turn_receipt),
                decision_file=str(bad_turn_decision),
            ))

        for field in ("assignment_sha256", "failure_evidence_sha256", "creation_receipt_sha256"):
            with self.subTest(field=field):
                decision_path = self.root / f"bad-{field}.json"
                decision_data = json.loads(fixture["decision"].read_text(encoding="utf-8"))
                decision_data[field] = "f" * 64
                decision_path.write_text(json.dumps(decision_data, indent=2), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, f"{field} binding"):
                    impl.command_reopen_interrupted(ns(
                        **base, interrupt_receipt_file=str(fixture["interrupt"]),
                        decision_file=str(decision_path),
                    ))

    def test_reopen_historical_interruption_rejects_forged_known_origin(self):
        fixture = self.historical_interrupted_recovery()
        forged = self.root / "forged-known-origin.json"
        forged_data = json.loads(fixture["interrupt"].read_text(encoding="utf-8"))
        forged_data["origin"] = "app_lifecycle"
        forged.write_text(json.dumps(forged_data, indent=2), encoding="utf-8")
        decision = self.interruption_reopen_decision(
            fixture["recovery"], forged, task_id=fixture["recovery_task_id"],
            name="forged-known-origin-decision.json",
        )
        with self.assertRaisesRegex(ValueError, "직접 근거"):
            impl.command_reopen_interrupted(ns(
                db=str(self.db), run_id=fixture["run"]["run_id"],
                task_id=fixture["recovery_task_id"],
                dispatch_id=fixture["recovery"]["dispatch_id"],
                interrupt_receipt_file=str(forged), decision_file=str(decision),
                dry_run=False,
            ))
        guessed_request = self.root / "unknown-origin-guessed-request.json"
        guessed_data = json.loads(fixture["interrupt"].read_text(encoding="utf-8"))
        guessed_data["request_id"] = "guessed-request-id"
        guessed_data["raw_observation"]["requestId"] = "guessed-request-id"
        guessed_request.write_text(json.dumps(guessed_data, indent=2), encoding="utf-8")
        guessed_decision = self.interruption_reopen_decision(
            fixture["recovery"], guessed_request, task_id=fixture["recovery_task_id"],
            name="unknown-origin-guessed-request-decision.json",
        )
        with self.assertRaisesRegex(ValueError, "request_id=null"):
            impl.command_reopen_interrupted(ns(
                db=str(self.db), run_id=fixture["run"]["run_id"],
                task_id=fixture["recovery_task_id"],
                dispatch_id=fixture["recovery"]["dispatch_id"],
                interrupt_receipt_file=str(guessed_request),
                decision_file=str(guessed_decision), dry_run=False,
            ))
        hidden_origin = self.root / "unknown-origin-hides-known-raw-origin.json"
        hidden_data = json.loads(fixture["interrupt"].read_text(encoding="utf-8"))
        hidden_data["raw_observation"]["interruptOrigin"] = "app_lifecycle"
        hidden_origin.write_text(json.dumps(hidden_data, indent=2), encoding="utf-8")
        hidden_decision = self.interruption_reopen_decision(
            fixture["recovery"], hidden_origin, task_id=fixture["recovery_task_id"],
            name="unknown-origin-hides-known-raw-origin-decision.json",
        )
        with self.assertRaisesRegex(ValueError, "unknown으로 숨길 수 없습니다"):
            impl.command_reopen_interrupted(ns(
                db=str(self.db), run_id=fixture["run"]["run_id"],
                task_id=fixture["recovery_task_id"],
                dispatch_id=fixture["recovery"]["dispatch_id"],
                interrupt_receipt_file=str(hidden_origin),
                decision_file=str(hidden_decision), dry_run=False,
            ))

    def test_reopen_historical_interruption_rejects_non_recovery_and_active_dispatch(self):
        fixture = self.historical_interrupted_recovery()
        original_interrupt = self.interrupt_receipt(
            fixture["original"], thread_id="thread-original-failure",
            turn_id="turn-original-failure", name="original-interrupt.json",
        )
        original_decision = self.interruption_reopen_decision(
            fixture["original"], original_interrupt, task_id="FM-01",
            name="original-interrupt-decision.json",
        )
        with self.assertRaisesRegex(RuntimeError, "기존 recovery Task"):
            impl.command_reopen_interrupted(ns(
                db=str(self.db), run_id=fixture["run"]["run_id"], task_id="FM-01",
                dispatch_id=fixture["original"]["dispatch_id"],
                interrupt_receipt_file=str(original_interrupt),
                decision_file=str(original_decision), dry_run=False,
            ))

        with connect(self.db) as connection:
            connection.execute(
                "UPDATE orchestration_state SET active_dispatch_id=? WHERE singleton=1",
                (fixture["recovery"]["dispatch_id"],),
            )
        with self.assertRaisesRegex(RuntimeError, "active_dispatch=null"):
            impl.command_reopen_interrupted(ns(
                db=str(self.db), run_id=fixture["run"]["run_id"],
                task_id=fixture["recovery_task_id"],
                dispatch_id=fixture["recovery"]["dispatch_id"],
                interrupt_receipt_file=str(fixture["interrupt"]),
                decision_file=str(fixture["decision"]), dry_run=False,
            ))

    def test_task_reasoning_policy_starts_medium_and_escalates_after_reviewed_failure(self):
        self.migrate()
        configured = impl.command_configure_reasoning_policy(ns(
            db=str(self.db), scope="task", key="FM-01", model="gpt-test-terra",
            effort_ladder=["medium", "high", "xhigh"],
            escalation_trigger="independent_review_failure",
            reason="unit-test task escalation policy",
        ))
        self.assertEqual(configured["initial_effort"], "medium")
        self.assertEqual(configured["effort_ladder"], ["medium", "high", "xhigh"])
        before = impl.command_task(ns(db=str(self.db), task_id="FM-01"))
        self.assertEqual(before["task"]["reasoning_effort"], "high")
        self.assertEqual(before["effective_model_binding"]["reasoning_effort"], "medium")

        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        self.assertEqual(reserved["create_thread"]["thinking"], "medium")
        self.assertEqual(reserved["model_binding"]["escalation_step"], 0)
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-policy-fail", client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id="thread-policy-fail")),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="policy-cursor", turn_id="policy-turn",
            summary_sha256="9" * 64, thread_id="thread-policy-fail", client_thread_id=None,
        ))
        finding = {
            "failure_class": "implementation", "fingerprint": "8" * 64,
            "summary": "reviewed unit-test failure", "remediable": True,
            "scope_expansion_required": False,
        }
        failure = self.evidence(
            "FM-01", dispatch=reserved, finding=finding, name="policy-failure.json"
        )
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(failure),
        ))
        after = impl.command_task(ns(db=str(self.db), task_id="FM-01"))
        self.assertEqual(after["effective_model_binding"]["reasoning_effort"], "high")
        self.assertEqual(after["effective_model_binding"]["escalation_step"], 1)
        with connect(self.db) as connection:
            binding = connection.execute(
                "SELECT model,reasoning_effort,escalation_step,policy_revision "
                "FROM implementation_dispatch_model_bindings WHERE dispatch_id=?",
                (reserved["dispatch_id"],),
            ).fetchone()
        self.assertEqual(binding, ("gpt-test-terra", "medium", 0, 1))
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])

    def test_evaluation_freshness_contract_blocks_stale_completion(self):
        self.manifest["tasks"][1]["evidence_contract"] = "evaluation"
        self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-evaluation"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="evaluation", turn_id="turn-evaluation",
            summary_sha256="c" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        evidence_path = self.evidence("FM-01", dispatch=reserved, name="evaluation-evidence.json")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            impl.command_review(ns(
                db=str(self.db), run_id=run["run_id"], task_id="FM-01",
                evidence_file=str(evidence_path),
            ))
        bindings = []
        for kind in sorted(impl.EVALUATION_FRESHNESS_KINDS):
            artifact = self.root / f"{kind}.artifact"
            artifact.write_text(f"immutable {kind}\n", encoding="utf-8")
            item = {"path": str(artifact), "sha256": digest(artifact)}
            evidence["files"].append(item)
            bindings.append({"kind": kind, **item})
        evidence["freshness"] = {
            "contract": "evaluation",
            "scopes": ["completion"],
            "bindings": bindings,
        }
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence_path),
        ))
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["decision"], "COMPLETE")
        (self.root / "evaluator.artifact").write_text("changed evaluator\n", encoding="utf-8")
        stale = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(stale["decision"], "REVALIDATION_REQUIRED")
        self.assertEqual(stale["task_id"], "FM-01")
        self.assertEqual(stale["action"], "REVALIDATE_SUCCESS")
        verified = impl.command_verify(ns(db=str(self.db)))
        self.assertTrue(verified["ok"])
        self.assertIn("stale_evidence:FM-01", verified["warnings"])
        self.assertEqual(verified["revalidation_required"], ["FM-01"])

        revalidation = json.loads(evidence_path.read_text(encoding="utf-8"))
        revalidation["dispatch_id"] = None
        revalidation["assignment_sha256"] = None
        revalidation["observed_at"] = impl.isoformat()
        revalidation["reviewer"] = {"kind": "deterministic", "id": "revalidation-test"}
        for item in revalidation["files"]:
            item["sha256"] = digest(Path(item["path"]))
        for binding in revalidation["freshness"]["bindings"]:
            binding["sha256"] = digest(Path(binding["path"]))
        revalidation_path = self.root / "evaluation-revalidation-evidence.json"
        revalidation_path.write_text(
            json.dumps(revalidation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        reviewed = impl.command_revalidate_success(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(revalidation_path),
        ))
        self.assertTrue(reviewed["revalidated"])
        self.assertFalse(reviewed["worker_launched"])
        self.assertFalse(reviewed["dispatch_created"])
        self.assertFalse(reviewed["legacy_reopened_repaired"])
        self.assertEqual(reviewed["attempt_no"], 2)
        self.assertEqual(reviewed["decision"], "COMPLETE")
        with connect(self.db) as connection:
            history = connection.execute(
                "SELECT attempt_no,status,evidence_sha256,purpose_key FROM implementation_task_attempts "
                "WHERE task_id='FM-01' ORDER BY attempt_no"
            ).fetchall()
            dispatch_count = connection.execute(
                "SELECT COUNT(*) FROM implementation_task_dispatches WHERE task_id='FM-01'"
            ).fetchone()[0]
        self.assertEqual(len(history), 2)
        self.assertEqual([row[0:2] for row in history], [(1, "SUCCEEDED"), (2, "SUCCEEDED")])
        self.assertTrue(history[1][3].endswith(":direct-revalidation"))
        self.assertEqual(dispatch_count, 1)
        again = impl.command_revalidate_success(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(revalidation_path),
        ))
        self.assertTrue(again["idempotent"])

    def test_revalidate_success_repairs_legacy_reopen_without_dispatch(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        dispatch = self.reserve_fm01(run["run_id"])
        thread_id = "thread-legacy-revalidation"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="legacy-revalidation", turn_id="legacy-turn",
            summary_sha256="d" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        legacy_proof = self.root / "legacy-revalidation-proof.txt"
        legacy_proof.write_text("initial legacy proof\n", encoding="utf-8")
        first = self.evidence("FM-01", dispatch=dispatch, name="legacy-first.json")
        first_data = json.loads(first.read_text(encoding="utf-8"))
        first_data["files"] = [{"path": str(legacy_proof), "sha256": digest(legacy_proof)}]
        first_data["checks"][0]["evidence_refs"] = [str(legacy_proof)]
        first.write_text(json.dumps(first_data, ensure_ascii=False, indent=2), encoding="utf-8")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(first),
        ))
        legacy_proof.write_text("fresh replacement proof\n", encoding="utf-8")
        self.assertEqual(
            impl.command_status(ns(db=str(self.db)))["decision"], "REVALIDATION_REQUIRED"
        )
        reopened = impl.command_reopen_stale(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
        ))
        self.assertEqual((reopened["decision"], reopened["task_id"]), ("READY", "FM-01"))
        fresh = self.evidence("FM-01", name="legacy-direct-revalidation.json")
        fresh_data = json.loads(fresh.read_text(encoding="utf-8"))
        fresh_data["files"] = [{"path": str(legacy_proof), "sha256": digest(legacy_proof)}]
        fresh_data["checks"][0]["evidence_refs"] = [str(legacy_proof)]
        fresh.write_text(json.dumps(fresh_data, ensure_ascii=False, indent=2), encoding="utf-8")
        repaired = impl.command_revalidate_success(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(fresh),
            legacy_reopen_event_id=reopened["revalidation_event_id"],
        ))
        self.assertTrue(repaired["legacy_reopened_repaired"])
        self.assertEqual(repaired["decision"], "COMPLETE")
        with connect(self.db) as connection:
            attempts = connection.execute(
                "SELECT attempt_no,status FROM implementation_task_attempts "
                "WHERE task_id='FM-01' ORDER BY attempt_no"
            ).fetchall()
            dispatch_count = connection.execute(
                "SELECT COUNT(*) FROM implementation_task_dispatches WHERE task_id='FM-01'"
            ).fetchone()[0]
        self.assertEqual(attempts, [(1, "SUCCEEDED"), (2, "SUCCEEDED")])
        self.assertEqual(dispatch_count, 1)

    def test_revalidate_success_rejects_forged_legacy_reopen_event(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        dispatch = self.reserve_fm01(run["run_id"])
        thread_id = "thread-forged-legacy-revalidation"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="forged-legacy", turn_id="forged-legacy-turn",
            summary_sha256="e" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        proof = self.root / "forged-legacy-proof.txt"
        proof.write_text("initial proof\n", encoding="utf-8")
        first = self.evidence("FM-01", dispatch=dispatch, name="forged-legacy-first.json")
        first_data = json.loads(first.read_text(encoding="utf-8"))
        first_data["files"] = [{"path": str(proof), "sha256": digest(proof)}]
        first_data["checks"][0]["evidence_refs"] = [str(proof)]
        first.write_text(json.dumps(first_data, ensure_ascii=False, indent=2), encoding="utf-8")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(first),
        ))
        proof.write_text("replacement proof\n", encoding="utf-8")
        with connect(self.db) as connection:
            connection.execute(
                "UPDATE implementation_tasks SET status='PENDING',active_attempt_no=NULL "
                "WHERE task_id='FM-01'"
            )
            payload = {
                "previous_status": "SUCCEEDED",
                "new_status": "PENDING",
                "previous_attempt_no": 999,
                "reason": "completion-scope evidence became stale after bound input changes",
                "history_preserved": True,
            }
            connection.execute(
                """INSERT INTO orchestration_events(
                   run_id,occurred_at,event_type,entity_type,entity_id,payload_json
                   ) VALUES(?,?,?,?,?,?)""",
                (run["run_id"], "2000-01-01T00:00:00.000000Z",
                 "implementation_task.revalidation_requested", "implementation_task",
                 "FM-01", impl.canonical_json(payload)),
            )
            forged_event_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        fresh = self.evidence("FM-01", name="forged-legacy-direct.json")
        fresh_data = json.loads(fresh.read_text(encoding="utf-8"))
        fresh_data["files"] = [{"path": str(proof), "sha256": digest(proof)}]
        fresh_data["checks"][0]["evidence_refs"] = [str(proof)]
        fresh.write_text(json.dumps(fresh_data, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "최신 성공 Attempt"):
            impl.command_revalidate_success(ns(
                db=str(self.db), run_id=run["run_id"], task_id="FM-01",
                evidence_file=str(fresh), legacy_reopen_event_id=forged_event_id,
            ))
        with connect(self.db) as connection:
            payload["previous_attempt_no"] = 1
            connection.execute(
                """INSERT INTO orchestration_events(
                   run_id,occurred_at,event_type,entity_type,entity_id,payload_json
                   ) VALUES(?,?,?,?,?,?)""",
                (run["run_id"], "Z", "implementation_task.revalidation_requested",
                 "implementation_task", "FM-01", impl.canonical_json(payload)),
            )
            malformed_event_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "유효한 timestamp"):
            impl.command_revalidate_success(ns(
                db=str(self.db), run_id=run["run_id"], task_id="FM-01",
                evidence_file=str(fresh), legacy_reopen_event_id=malformed_event_id,
            ))
        with connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM implementation_task_attempts WHERE task_id='FM-01'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM implementation_task_dispatches WHERE task_id='FM-01'"
                ).fetchone()[0],
                1,
            )

    def test_revalidate_success_rejects_ordinary_pending_task(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        evidence = self.evidence("FM-01", name="ordinary-pending-revalidation.json")
        with self.assertRaisesRegex(RuntimeError, "정확한 --legacy-reopen-event-id"):
            impl.command_revalidate_success(ns(
                db=str(self.db), run_id=run["run_id"], task_id="FM-01",
                evidence_file=str(evidence),
            ))

    def _record_direct(self, run_id: str, task_id: str, evidence: Path, **overrides):
        values = dict(
            db=str(self.db), run_id=run_id, task_id=task_id, evidence_file=str(evidence),
            executor_model="claude-test-opus", executor_kind="claude-subagent",
        )
        values.update(overrides)
        return impl.command_record_direct_attempt(ns(**values))

    def test_record_direct_attempt_completes_ready_task_without_dispatch(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        evidence = self.evidence("FM-01", name="direct-attempt.json")
        with connect(self.db) as connection:
            dispatches_before = connection.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
        result = self._record_direct(run["run_id"], "FM-01", evidence)
        self.assertEqual((result["recorded"], result["outcome"], result["attempt_no"]),
                         (True, "PASS", 1))
        self.assertEqual(result["executor"], {"kind": "claude-subagent", "model": "claude-test-opus"})
        self.assertFalse(result["worker_launched"])
        self.assertFalse(result["dispatch_created"])
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            task = connection.execute(
                "SELECT * FROM implementation_tasks WHERE task_id='FM-01'"
            ).fetchone()
            self.assertEqual((task["status"], task["attempt_count"], task["active_attempt_no"]),
                             ("SUCCEEDED", 1, 1))
            # 원장의 task model/effort는 실행자와 무관하게 보존된다.
            self.assertEqual((task["model"], task["reasoning_effort"]), ("gpt-test-terra", "high"))
            attempt = connection.execute(
                "SELECT * FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()
            self.assertEqual(attempt["status"], "SUCCEEDED")
            self.assertTrue(attempt["purpose_key"].endswith(":1:direct-attempt"))
            self.assertEqual(attempt["evidence_sha256"], result["evidence_sha256"])
            # direct attempt는 dispatch 행을 만들지 않는다(fixture의 legacy 행은 그대로).
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
                dispatches_before,
            )
            self.assertIsNone(connection.execute(
                "SELECT active_dispatch_id FROM orchestration_state WHERE singleton=1"
            ).fetchone()[0])
            event = connection.execute(
                """SELECT payload_json FROM orchestration_events
                   WHERE event_type='implementation_task.direct_attempt_recorded'"""
            ).fetchone()
            payload = json.loads(event["payload_json"])
            self.assertEqual(payload["executor"]["model"], "claude-test-opus")
            self.assertEqual(payload["ledger_model_binding"]["model"], "gpt-test-terra")
            self.assertFalse(payload["worker_launched"])
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["decision"], "COMPLETE")
        # 같은 evidence 재기록은 멱등이다.
        again = self._record_direct(run["run_id"], "FM-01", evidence)
        self.assertEqual((again["recorded"], again["idempotent"]), (False, True))

    def test_record_direct_attempt_rejects_task_that_is_not_ready(self):
        self.migrate()
        run = self.begin()
        evidence = self.evidence("FM-01", name="not-ready.json")
        with self.assertRaisesRegex(RuntimeError, "READY 결정 대상이 아닙니다"):
            self._record_direct(run["run_id"], "FM-01", evidence)
        with connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM implementation_tasks WHERE task_id='FM-01'"
                ).fetchone()[0], "PENDING",
            )

    def test_record_direct_attempt_rejects_bootstrap_and_active_dispatch(self):
        self.migrate()
        run = self.begin()
        fm00 = self.evidence("FM-00", name="direct-fm00.json")
        with self.assertRaisesRegex(RuntimeError, "READY 결정 대상이 아닙니다|bootstrap"):
            self._record_direct(run["run_id"], "FM-00", fm00)
        self.pass_fm00(run["run_id"])
        self.reserve_fm01(run["run_id"])
        evidence = self.evidence("FM-01", name="active-dispatch.json")
        with self.assertRaisesRegex(RuntimeError, "활성 dispatch"):
            self._record_direct(run["run_id"], "FM-01", evidence)

    def test_record_direct_attempt_with_finding_marks_task_failed(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        finding = {
            "failure_class": "implementation", "fingerprint": "b" * 64,
            "summary": "canary boundary violated", "remediable": True,
            "scope_expansion_required": False,
        }
        evidence = self.evidence("FM-01", finding=finding, name="direct-fail.json")
        result = self._record_direct(run["run_id"], "FM-01", evidence)
        self.assertEqual((result["outcome"], result["task_status"]), ("FAIL", "FAILED"))
        with connect(self.db) as connection:
            connection.row_factory = sqlite3.Row
            task = connection.execute(
                "SELECT status,failure_fingerprint FROM implementation_tasks WHERE task_id='FM-01'"
            ).fetchone()
            self.assertEqual((task["status"], task["failure_fingerprint"]), ("FAILED", "b" * 64))
            attempt = connection.execute(
                "SELECT status,failure_fingerprint FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()
            self.assertEqual((attempt["status"], attempt["failure_fingerprint"]), ("FAILED", "b" * 64))
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["decision"], "RECOVERY_REQUIRED")

    def test_dependency_freshness_blocks_downstream_ready_after_tamper(self):
        self.manifest["tasks"][1]["evidence_contract"] = "evaluation"
        self.manifest["tasks"].append(self._task("FM-02", 2, "development", ["FM-01"]))
        self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        dispatch = self.reserve_fm01(run["run_id"])
        thread_id = "thread-dependency-freshness"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="dependency", turn_id="turn-dependency",
            summary_sha256="1" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        evidence_path = self.evidence("FM-01", dispatch=dispatch, name="dependency-evidence.json")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        bindings = []
        for kind in sorted(impl.EVALUATION_FRESHNESS_KINDS):
            artifact = self.root / f"dependency-{kind}.artifact"
            artifact.write_text(f"dependency {kind}\n", encoding="utf-8")
            item = {"path": str(artifact), "sha256": digest(artifact)}
            evidence["files"].append(item)
            bindings.append({"kind": kind, **item})
        evidence["freshness"] = {
            "contract": "evaluation", "scopes": ["dependency"], "bindings": bindings,
        }
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence_path),
        ))
        ready = impl.command_status(ns(db=str(self.db)))
        self.assertEqual((ready["decision"], ready["task_id"]), ("READY", "FM-02"))
        (self.root / "dependency-evaluator.artifact").write_text("tampered\n", encoding="utf-8")
        self.assertEqual(
            impl.command_status(ns(db=str(self.db)))["decision"], "REVALIDATION_REQUIRED"
        )

    def test_legacy_digest_contract_revalidates_already_stored_success_evidence(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        dispatch = self.reserve_fm01(run["run_id"])
        thread_id = "thread-before-contract"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="legacy", turn_id="turn-legacy",
            summary_sha256="7" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        evidence_path = self.evidence("FM-01", dispatch=dispatch, name="pre-contract-evidence.json")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence_path),
        ))
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["decision"], "COMPLETE")
        with connect(self.db) as connection:
            spec_sha = connection.execute(
                "SELECT spec_sha256 FROM implementation_tasks WHERE task_id='FM-01'"
            ).fetchone()[0]
        previous = impl.LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256.get(spec_sha)
        impl.LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256[spec_sha] = "evaluation"
        try:
            self.assertEqual(
                impl.command_status(ns(db=str(self.db)))["decision"],
                "REVALIDATION_REQUIRED",
            )
            verified = impl.command_verify(ns(db=str(self.db)))
            self.assertTrue(verified["ok"])
            self.assertIn("stale_evidence:FM-01", verified["warnings"])
        finally:
            if previous is None:
                del impl.LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256[spec_sha]
            else:
                impl.LEGACY_EVIDENCE_CONTRACTS_BY_TASK_SPEC_SHA256[spec_sha] = previous

    def test_registered_multistage_dag_and_specs_are_the_only_next_stage_authority(self):
        self.manifest["tasks"] = [
            self._task("FM-00", 0, "bootstrap", []),
            self._task("FM-08", 8, "development", ["FM-00"]),
            self._task("FM-09", 9, "development", ["FM-00", "FM-08"]),
            self._task("FM-10", 10, "development", ["FM-08", "FM-09"]),
        ]
        self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        self.manifest_path.write_text("{not the registered workflow}", encoding="utf-8")
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["task_id"], "FM-08")

        def pass_next(current_run, expected_task, thread_id):
            dispatch = impl.command_reserve_next(ns(db=str(self.db), run_id=current_run))
            self.assertEqual(dispatch["task_id"], expected_task)
            impl.command_confirm(ns(
                db=str(self.db), run_id=current_run, dispatch_id=dispatch["dispatch_id"],
                thread_id=thread_id, client_thread_id=None,
                receipt_file=str(self.create_receipt(dispatch, thread_id=thread_id)),
            ))
            impl.command_observe(ns(
                db=str(self.db), run_id=current_run, dispatch_id=dispatch["dispatch_id"],
                status="completed", cursor=f"cursor-{thread_id}", turn_id=f"turn-{thread_id}",
                summary_sha256="d" * 64, thread_id=thread_id, client_thread_id=None,
            ))
            before_review = impl.command_status(ns(db=str(self.db)))
            self.assertEqual(before_review["decision"], "OBSERVE")
            evidence = self.evidence(expected_task, dispatch=dispatch, name=f"{expected_task}.json")
            impl.command_review(ns(
                db=str(self.db), run_id=current_run, task_id=expected_task,
                evidence_file=str(evidence),
            ))

        pass_next(run["run_id"], "FM-08", "thread-08")
        self.assertEqual(impl.command_status(ns(db=str(self.db)))["task_id"], "FM-09")
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE", reason="next tick",
        ))
        run = self.begin()
        pass_next(run["run_id"], "FM-09", "thread-09")
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual((status["decision"], status["task_id"]), ("READY", "FM-10"))
        registered = impl.command_task(ns(db=str(self.db), task_id="FM-10"))
        self.assertEqual(
            [row["depends_on_task_id"] for row in registered["dependencies"]],
            ["FM-08", "FM-09"],
        )
        expected_spec = next(task for task in self.manifest["tasks"] if task["task_id"] == "FM-10")
        self.assertEqual(registered["spec"], expected_spec)
        self.assertEqual(
            registered["task"]["spec_sha256"],
            hashlib.sha256(impl.canonical_json(expected_spec).encode("utf-8")).hexdigest(),
        )
        self.assertEqual([row["check_id"] for row in registered["checks"]], ["FM-10-C1"])

    def test_fail_review_recovery_uses_direct_evidence_and_has_no_deadlock(self):
        self.migrate()
        impl.command_configure_reasoning_policy(ns(
            db=str(self.db), scope="architecture_recovery", key="default",
            model="gpt-test-astra", effort_ladder=["medium", "high", "xhigh"],
            escalation_trigger="same_failure_with_new_evidence",
            reason="unit-test architecture recovery escalation policy",
        ))
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-fail", client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id="thread-fail"))
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="c", turn_id="t", summary_sha256="c" * 64,
            thread_id="thread-fail", client_thread_id=None
        ))
        fingerprint = "d" * 64
        finding = {
            "failure_class": "task_contract", "fingerprint": fingerprint,
            "summary": "deterministic implementation failure", "remediable": True,
            "scope_expansion_required": False,
        }
        failure_evidence = self.evidence(
            "FM-01", dispatch=reserved, finding=finding, name="fm01-fail.json"
        )
        reviewed = impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01", evidence_file=str(failure_evidence)
        ))
        self.assertEqual(reviewed["outcome"], "FAIL")
        recovery = impl.command_register_recovery(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01", evidence_file=str(failure_evidence)
        ))
        self.assertTrue(recovery["registered"])
        self.assertEqual(recovery["model"], "gpt-test-astra")
        self.assertEqual(recovery["reasoning_effort"], "medium")
        with connect(self.db) as connection:
            row = connection.execute(
                "SELECT project_id FROM implementation_tasks WHERE task_id=?",
                (recovery["recovery_task_id"],),
            ).fetchone()
            wrong_edge = connection.execute(
                """SELECT 1 FROM implementation_task_dependencies
                   WHERE task_id='FM-01' AND depends_on_task_id=?""",
                (recovery["recovery_task_id"],),
            ).fetchone()
        self.assertEqual(row[0], "project-recovery")
        self.assertIsNone(wrong_edge)
        decision = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(decision["decision"], "READY")
        self.assertEqual(decision["task_id"], recovery["recovery_task_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="recovery registered for next bounded run"
        ))
        recovery_run = self.begin()
        recovery_dispatch = impl.command_reserve_next(ns(db=str(self.db), run_id=recovery_run["run_id"]))
        self.assertEqual(recovery_dispatch["task_id"], recovery["recovery_task_id"])
        self.assertEqual(
            recovery_dispatch["create_thread"]["target"]["environment"], {"type": "local"},
        )
        impl.command_confirm(ns(
            db=str(self.db), run_id=recovery_run["run_id"], dispatch_id=recovery_dispatch["dispatch_id"],
            thread_id="thread-recovery-fail", client_thread_id=None,
            receipt_file=str(self.create_receipt(recovery_dispatch, thread_id="thread-recovery-fail"))
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=recovery_run["run_id"], dispatch_id=recovery_dispatch["dispatch_id"],
            status="completed", cursor="rc", turn_id="rt", summary_sha256="e" * 64,
            thread_id="thread-recovery-fail", client_thread_id=None
        ))
        self.proof.write_text("new direct evidence for failed recovery\n", encoding="utf-8")
        leaf_failure = self.evidence(
            recovery["recovery_task_id"], dispatch=recovery_dispatch, finding=finding,
            name="recovery-leaf-fail.json"
        )
        impl.command_review(ns(
            db=str(self.db), run_id=recovery_run["run_id"], task_id=recovery["recovery_task_id"],
            evidence_file=str(leaf_failure)
        ))
        leaf_status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(leaf_status["decision"], "RECOVERY_REQUIRED")
        self.assertEqual(leaf_status["task_id"], recovery["recovery_task_id"])
        second = impl.command_register_recovery(ns(
            db=str(self.db), run_id=recovery_run["run_id"], task_id=recovery["recovery_task_id"],
            evidence_file=str(leaf_failure)
        ))
        self.assertTrue(second["registered"])
        self.assertEqual(second["model"], "gpt-test-astra")
        self.assertEqual(second["reasoning_effort"], "high")
        with connect(self.db) as connection:
            lineage = connection.execute(
                "SELECT lineage_root_task_id,registration_no FROM implementation_recoveries ORDER BY registration_no"
            ).fetchall()
        self.assertEqual(lineage, [("FM-01", 1), ("FM-01", 2)])

    def test_revision_two_recovery_survives_verify_first_and_is_reserved_next_tick(self):
        self.activate_revision2_fixture()
        run = self.begin()
        reserved = impl.command_reserve_next(ns(db=str(self.db), run_id=run["run_id"]))
        self.assertEqual(reserved["task_id"], "FM-01-A")
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-revision-two-fail", client_thread_id=None,
            receipt_file=str(self.create_receipt(
                reserved, thread_id="thread-revision-two-fail",
            )),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="revision-two-cursor", turn_id="revision-two-turn",
            summary_sha256="a" * 64, thread_id="thread-revision-two-fail",
            client_thread_id=None,
        ))
        fingerprint = "9" * 64
        finding = {
            "failure_class": "task_contract", "fingerprint": fingerprint,
            "summary": "revision two deterministic failure", "remediable": True,
            "scope_expansion_required": False,
        }
        evidence = self.evidence(
            "FM-01-A", dispatch=reserved, finding=finding,
            name="revision-two-failure.json",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01-A",
            evidence_file=str(evidence),
        ))
        recovery = impl.command_register_recovery(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01-A",
            evidence_file=str(evidence),
        ))
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="recovery registered for next verify-first tick",
        ))

        verification = impl.command_verify(ns(db=str(self.db)))
        self.assertTrue(verification["ok"], verification)
        self.assertNotIn("task_lineage_coverage", verification["errors"])
        next_run = self.begin()
        repair = impl.command_reserve_next(ns(
            db=str(self.db), run_id=next_run["run_id"],
        ))
        self.assertEqual(repair["task_id"], recovery["recovery_task_id"])
        self.assertEqual(repair["create_thread"]["target"]["projectId"], "project-recovery")

    def test_exhausted_recovery_budget_requires_user_decision_in_status_and_registration(self):
        self.manifest["policies"]["max_total_recoveries"] = 0
        self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        thread_id = "thread-budget-exhausted"
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id=thread_id, client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id=thread_id)),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="budget-cursor", turn_id="budget-turn",
            summary_sha256="7" * 64, thread_id=thread_id, client_thread_id=None,
        ))
        finding = {
            "failure_class": "implementation",
            "fingerprint": "6" * 64,
            "summary": "reproduced failure after the approved recovery budget was exhausted",
            "remediable": True,
            "scope_expansion_required": False,
        }
        evidence = self.evidence(
            "FM-01", dispatch=reserved, finding=finding, name="budget-exhausted.json",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence),
        ))

        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(status["decision"], "USER_DECISION_REQUIRED")
        self.assertEqual(status["action"], "RECOVERY_LIMIT_REACHED")
        self.assertEqual(status["recovery_budget"]["scope"], "workflow_lifetime")
        self.assertEqual(status["recovery_budget"]["total_count"], 0)
        self.assertEqual(status["recovery_budget"]["total_limit"], 0)
        self.assertEqual(status["recovery_budget"]["exhausted_by"], ["workflow_total"])

        registration = impl.command_register_recovery(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence),
        ))
        self.assertFalse(registration["registered"])
        self.assertEqual(registration["decision"], "USER_DECISION_REQUIRED")
        self.assertEqual(registration["action"], "RECOVERY_LIMIT_REACHED")
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="USER_DECISION_REQUIRED",
            reason="approved workflow-lifetime recovery budget is exhausted",
        ))
        next_run = self.begin()
        reserved_again = impl.command_reserve_next(ns(
            db=str(self.db), run_id=next_run["run_id"],
        ))
        self.assertFalse(reserved_again["reserved"])
        self.assertEqual(reserved_again["decision"], "USER_DECISION_REQUIRED")
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])

    def test_verify_rejects_same_count_wrong_lineage_tuple(self):
        self.activate_revision2_fixture()
        with connect(self.db) as connection:
            decision = "tampered while keeping the lineage row count unchanged"
            connection.execute(
                """UPDATE implementation_task_lineage SET decision=?,decision_sha256=?
                   WHERE workflow_id=? AND to_workflow_revision=2 AND to_task_id='FM-01-A'""",
                (decision, hashlib.sha256(decision.encode("utf-8")).hexdigest(), impl.WORKFLOW_ID),
            )
        verification = impl.command_verify(ns(db=str(self.db)))
        self.assertFalse(verification["ok"])
        self.assertIn("task_lineage_coverage", verification["errors"])

    def test_verify_rejects_orphaned_recovery_marker(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            thread_id="thread-orphan-recovery", client_thread_id=None,
            receipt_file=str(self.create_receipt(reserved, thread_id="thread-orphan-recovery")),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            status="completed", cursor="orphan-cursor", turn_id="orphan-turn",
            summary_sha256="b" * 64, thread_id="thread-orphan-recovery", client_thread_id=None,
        ))
        finding = {
            "failure_class": "implementation", "fingerprint": "8" * 64,
            "summary": "orphan recovery test", "remediable": True,
            "scope_expansion_required": False,
        }
        evidence = self.evidence(
            "FM-01", dispatch=reserved, finding=finding, name="orphan-failure.json",
        )
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence),
        ))
        recovery = impl.command_register_recovery(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence),
        ))
        with connect(self.db) as connection:
            connection.execute(
                "DELETE FROM implementation_recoveries WHERE workflow_id=? AND recovery_task_id=?",
                (impl.WORKFLOW_ID, recovery["recovery_task_id"]),
            )
        verification = impl.command_verify(ns(db=str(self.db)))
        self.assertFalse(verification["ok"])
        self.assertIn(
            f"recovery_integrity:{recovery['recovery_task_id']}", verification["errors"],
        )

    def test_reserve_enforces_main_checkout_before_creating_attempt(self):
        source = self.root / "source"
        source.mkdir()
        (source / "tracked.txt").write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "wrong-branch", str(source)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run([
            "git", "-C", str(source), "-c", "user.name=FlowMarshal Test",
            "-c", "user.email=flowmarshal-test@example.invalid", "commit", "-m", "fixture",
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.manifest["source_root"] = str(source)
        self.manifest["baseline_commit"] = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ).stdout.strip()
        self.manifest["policies"]["required_git_branch"] = "main"
        self.manifest["policies"]["main_checkout_only"] = True
        self._write_manifest(self.manifest)
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        with connect(self.db) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "EXECUTION_SOURCE_POLICY_MISMATCH"):
            self.reserve_fm01(run["run_id"])
        with connect(self.db) as connection:
            after = connection.execute(
                "SELECT COUNT(*) FROM implementation_task_attempts WHERE task_id='FM-01'"
            ).fetchone()[0]
        self.assertEqual(after, before)

        subprocess.run(["git", "-C", str(source), "branch", "-m", "main"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        reserved = self.reserve_fm01(run["run_id"])
        self.assertTrue(reserved["reserved"])
        self.assertIn('"branch": "main"', reserved["create_thread"]["prompt"])
        self.assertIn('"main_checkout": true', reserved["create_thread"]["prompt"])

    def test_explicit_no_effect_rejection_requires_review_before_recovery(self):
        self.migrate()
        impl.command_configure_reasoning_policy(ns(
            db=str(self.db), scope="task", key="FM-01", model="gpt-test-terra",
            effort_ladder=["medium", "high", "xhigh"],
            escalation_trigger="independent_review_failure",
            reason="unit-test non-reasoning failure policy",
        ))
        run = self.begin()
        self.pass_fm00(run["run_id"])
        reserved = self.reserve_fm01(run["run_id"])
        receipt_path = self.root / "creation-rejection.json"
        receipt_path.write_text(json.dumps({
            "schema_version": 1,
            "status": "rejected",
            "effect": "none",
            "dispatch_id": reserved["dispatch_id"],
            "assignment_sha256": reserved["assignment_sha256"],
            "error_code": "UNSUPPORTED_MODEL_EFFORT",
            "message": "validation rejected before creating a task",
            "observed_at": impl.isoformat(),
        }, indent=2), encoding="utf-8")
        rejected = impl.command_reject_creation(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=reserved["dispatch_id"],
            receipt_file=str(receipt_path)
        ))
        self.assertTrue(rejected["active_slot_preserved"])
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual((status["decision"], status["action"]), ("OBSERVE", "REVIEW_REQUIRED"))
        finding = {
            "failure_class": "environment", "fingerprint": "f" * 64,
            "summary": "requested model/effort unsupported", "remediable": True,
            "scope_expansion_required": False,
        }
        failure = self.evidence("FM-01", dispatch=reserved, finding=finding, name="reject-review.json")
        reviewed = impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01", evidence_file=str(failure)
        ))
        self.assertEqual(reviewed["outcome"], "FAIL")
        effective = impl.command_task(ns(db=str(self.db), task_id="FM-01"))[
            "effective_model_binding"
        ]
        self.assertEqual(effective["reasoning_effort"], "medium")
        self.assertEqual(effective["escalation_step"], 0)
        self.assertIsNone(impl.command_status(ns(db=str(self.db)))["active_dispatch"])
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])
        original_receipt = receipt_path.read_text(encoding="utf-8")
        tampered_receipt = json.loads(original_receipt)
        tampered_receipt["message"] = "tampered after review"
        receipt_path.write_text(json.dumps(tampered_receipt), encoding="utf-8")
        self.assertFalse(impl.command_verify(ns(db=str(self.db)))["ok"])
        receipt_path.write_text(original_receipt, encoding="utf-8")
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])
        registered = impl.command_register_recovery(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01", evidence_file=str(failure)
        ))
        self.assertTrue(registered["registered"])
        self.assertEqual(registered["model"], "gpt-test-sol")
        self.assertEqual(registered["reasoning_effort"], "high")


    def test_revision_draft_is_inert_then_activation_is_atomic_and_cas_guarded(self):
        self.migrate()
        head = impl.command_workflow_head(ns(db=str(self.db)))
        self.assertEqual((head["active_workflow_revision"], head["generation"]), (1, 1))

        run = self.begin()
        self.pass_fm00(run["run_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="register a reviewed draft on the next bounded operation",
        ))
        registered = self.register_revision()
        self.assertTrue(registered["registered"])
        self.assertEqual(registered["state"], "DRAFT")

        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(status["workflow"]["workflow_revision"], 1)
        self.assertEqual(status["task_id"], "FM-01")
        unchanged_head = impl.command_workflow_head(ns(db=str(self.db)))
        self.assertEqual(
            (unchanged_head["active_workflow_revision"], unchanged_head["generation"]),
            (1, 1),
        )

        activated = self.activate_revision(generation=1)
        self.assertEqual(
            (activated["active_workflow_revision"], activated["generation"]), (2, 2)
        )
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual(status["workflow"]["workflow_revision"], 2)
        self.assertEqual(status["task_id"], "FM-01-A")
        with connect(self.db) as connection:
            states = connection.execute(
                """SELECT workflow_revision,state FROM implementation_workflows
                   WHERE workflow_id=? ORDER BY workflow_revision""",
                (impl.WORKFLOW_ID,),
            ).fetchall()
            carried = connection.execute(
                """SELECT invalidated_at,invalidated_by_attempt_no
                   FROM implementation_task_carry_forwards
                   WHERE workflow_id=? AND to_workflow_revision=2 AND to_task_id='FM-00'""",
                (impl.WORKFLOW_ID,),
            ).fetchone()
            revision_two_tasks = connection.execute(
                """SELECT task_id,task_revision FROM implementation_tasks
                   WHERE workflow_id=? AND workflow_revision=2 ORDER BY order_index""",
                (impl.WORKFLOW_ID,),
            ).fetchall()
        self.assertEqual(states, [(1, "RETIRED"), (2, "ACTIVE")])
        self.assertEqual(carried, (None, None))
        self.assertEqual(
            revision_two_tasks, [("FM-00", 1), ("FM-01-A", 1), ("FM-01-B", 1)]
        )
        with self.assertRaisesRegex(RuntimeError, "generation|CAS"):
            self.activate_revision(generation=1)

    def test_draft_does_not_steal_dispatch_binding_and_activation_waits_for_review(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="register draft before the next v1 dispatch",
        ))
        self.register_revision()
        run = self.begin()
        dispatch = self.reserve_fm01(run["run_id"])
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id="thread-v1", client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id="thread-v1")),
        ))
        with self.assertRaisesRegex(RuntimeError, "dispatch|Attempt|review|lease|effect"):
            self.activate_revision(generation=1)

        observed = impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="cursor-v1", turn_id="turn-v1",
            summary_sha256="9" * 64, thread_id="thread-v1", client_thread_id=None,
            binding_receipt_file=None,
        ))
        self.assertEqual(observed["task_status"], "AWAITING_REVIEW")
        with connect(self.db) as connection:
            v1_status = connection.execute(
                """SELECT status FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=1 AND task_id='FM-01' AND task_revision=1""",
                (impl.WORKFLOW_ID,),
            ).fetchone()[0]
            v2_status = connection.execute(
                """SELECT status FROM implementation_tasks WHERE workflow_id=?
                   AND workflow_revision=2 AND task_id='FM-01-A' AND task_revision=1""",
                (impl.WORKFLOW_ID,),
            ).fetchone()[0]
        self.assertEqual(v1_status, "AWAITING_REVIEW")
        self.assertEqual(v2_status, "PENDING")

        evidence = self.evidence("FM-01", dispatch=dispatch, name="v1-late-review.json")
        reviewed = impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01",
            evidence_file=str(evidence),
        ))
        self.assertEqual(reviewed["outcome"], "PASS")
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="v1 dispatch terminal and review are fully settled",
        ))
        self.assertEqual(self.activate_revision(generation=1)["active_workflow_revision"], 2)

    def test_revision_manifest_rejects_false_carry_and_incomplete_lineage(self):
        self.migrate()
        changed = self._revision2_manifest()
        changed["tasks"][0]["title"] = "changed task meaning"
        with self.assertRaisesRegex((ValueError, RuntimeError), "carry|unchanged|digest|동일"):
            self.register_revision(changed)

        incomplete = self._revision2_manifest()
        incomplete["lineage"] = [
            row for row in incomplete["lineage"] if row["to_task_id"] != "FM-01-B"
        ]
        with self.assertRaisesRegex((ValueError, RuntimeError), "lineage|계보|FM-01-B"):
            self.register_revision(incomplete)

        duplicate = self._revision2_manifest()
        duplicate["tasks"].append(copy.deepcopy(duplicate["tasks"][-1]))
        with self.assertRaisesRegex(ValueError, "중복 task_id"):
            self.register_revision(duplicate)

    def test_revision_registration_and_activation_honor_manifest_guard(self):
        self.migrate()
        denied = self._revision2_manifest()
        denied["registration_guard"]["database_registration_allowed"] = False
        with self.assertRaisesRegex(RuntimeError, "registration_guard"):
            self.register_revision(denied)

        run = self.begin()
        self.pass_fm00(run["run_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="settle parent before registering an activation-denied revision",
        ))
        activation_denied = self._revision2_manifest()
        activation_denied["registration_guard"]["activation_allowed"] = False
        self.register_revision(activation_denied)
        with self.assertRaisesRegex(RuntimeError, "registration_guard"):
            self.activate_revision(generation=1)

    def test_carry_rehashes_all_evidence_files_without_freshness_contract(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="settle parent evidence before mutating its registered file",
        ))
        self.proof.write_text("mutated after review\n", encoding="utf-8")
        registered = self.register_revision()
        self.assertEqual(registered["carried_task_ids"], [])
        self.assertEqual(
            registered["skipped_carry"],
            [{"task_id": "FM-00", "reason": "source_succeeded_stale"}],
        )

    def test_activation_decision_binds_target_spec_and_source_snapshot(self):
        self.migrate()
        run = self.begin()
        self.pass_fm00(run["run_id"])
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="settle parent before activation binding checks",
        ))
        self.register_revision()
        decision_path = self._activation_decision()
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["target_spec_sha256"] = "0" * 64
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "target spec"):
            impl.command_activate_revision(ns(
                db=str(self.db), revision=2, expected_generation=1,
                decision_file=str(decision_path),
            ))

        decision_path = self._activation_decision()
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["source_snapshot"] = {"kind": "wrong"}
        decision_path.write_text(json.dumps(decision), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source snapshot"):
            impl.command_activate_revision(ns(
                db=str(self.db), revision=2, expected_generation=1,
                decision_file=str(decision_path),
            ))

    def test_schema_v3_migrates_additively_from_v2_and_preserves_history(self):
        self.migrate()
        with connect(self.db) as connection:
            before = connection.execute(
                "SELECT COUNT(*) FROM implementation_tasks"
            ).fetchone()[0]
            connection.execute("DROP TABLE implementation_launch_claims")
            connection.execute(
                "DELETE FROM implementation_schema_migrations WHERE version=3"
            )
            connection.execute(
                "UPDATE implementation_writer_contract SET minimum_writer_version=2 WHERE singleton=1"
            )
            connection.execute(
                "UPDATE implementation_workflow_heads SET writer_contract_version=2 WHERE workflow_id=?",
                (impl.WORKFLOW_ID,),
            )
        with self.assertRaisesRegex(RuntimeError, "writer contract version 불일치"):
            self.begin()
        upgraded = self.migrate()
        self.assertTrue(upgraded["migrated"])
        self.assertEqual(upgraded["schema_version"], 3)
        self.assertTrue(Path(upgraded["backup_path"]).is_file())
        self.assertEqual(digest(Path(upgraded["backup_path"])), upgraded["backup_sha256"])
        with connect(self.db) as connection:
            versions = {
                row[0] for row in connection.execute(
                    "SELECT version FROM implementation_schema_migrations"
                )
            }
            writer = connection.execute(
                "SELECT minimum_writer_version FROM implementation_writer_contract WHERE singleton=1"
            ).fetchone()[0]
            after = connection.execute(
                "SELECT COUNT(*) FROM implementation_tasks"
            ).fetchone()[0]
        self.assertEqual(versions, {1, 2, 3})
        self.assertEqual(writer, 3)
        self.assertEqual(after, before)
        self.assertTrue(impl.command_verify(ns(db=str(self.db), revision=None))["ok"])

    def test_schema_v3_writer_fence_rejects_missing_and_old_contract_versions(self):
        self.migrate()
        for register_udf, writer_version in (
            (False, None), (True, None), (True, 1), (True, 2),
        ):
            connection = sqlite3.connect(self.db)
            try:
                if register_udf:
                    connection.create_function(
                        "flowmarshal_writer_contract_version", 0, lambda: writer_version
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "CONTRACT_MISMATCH|function"):
                    connection.execute(
                        "UPDATE metadata SET updated_at=? WHERE key='schema_version'",
                        (impl.isoformat(),),
                    )
            finally:
                connection.close()

        with connect(self.db) as connection:
            connection.execute(
                "UPDATE metadata SET updated_at=? WHERE key='schema_version'", (impl.isoformat(),)
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO implementation_tasks(
                           workflow_id,workflow_revision,task_id,task_revision,order_index,title,lane,
                           project_id,model,reasoning_effort,model_selection_reason,spec_json,
                           spec_sha256,status,attempt_count,active_attempt_no,failure_fingerprint,
                           recovery_for_task_id,recovery_for_fingerprint,created_at,updated_at
                       )
                       SELECT workflow_id,workflow_revision,task_id,2,99,title,lane,project_id,model,
                              reasoning_effort,model_selection_reason,spec_json,spec_sha256,status,
                              attempt_count,active_attempt_no,failure_fingerprint,
                              recovery_for_task_id,recovery_for_fingerprint,created_at,updated_at
                       FROM implementation_tasks WHERE workflow_id=? AND workflow_revision=1
                         AND task_id='FM-01' AND task_revision=1""",
                    (impl.WORKFLOW_ID,),
                )
        self.assertTrue(impl.command_verify(ns(db=str(self.db)))["ok"])

    def test_revision_two_can_rollback_before_its_first_attempt_or_dispatch(self):
        activated = self.activate_revision2_fixture()
        self.assertEqual(
            (activated["active_workflow_revision"], activated["generation"]), (2, 2)
        )
        with connect(self.db) as connection:
            attempts = connection.execute(
                """SELECT COUNT(*) FROM implementation_task_attempts
                   WHERE workflow_id=? AND workflow_revision=2""",
                (impl.WORKFLOW_ID,),
            ).fetchone()[0]
            dispatches = connection.execute(
                """SELECT COUNT(*) FROM implementation_task_dispatches
                   WHERE workflow_id=? AND workflow_revision=2""",
                (impl.WORKFLOW_ID,),
            ).fetchone()[0]
        self.assertEqual((attempts, dispatches), (0, 0))

        rolled_back = self.activate_revision(
            revision=1, generation=2, from_revision=2,
        )
        self.assertEqual(
            (rolled_back["active_workflow_revision"], rolled_back["generation"]), (1, 3)
        )
        self.assertFalse(rolled_back["scheduled_automation_status_changed"])
        with connect(self.db) as connection:
            states = connection.execute(
                """SELECT workflow_revision,state FROM implementation_workflows
                   WHERE workflow_id=? ORDER BY workflow_revision""",
                (impl.WORKFLOW_ID,),
            ).fetchall()
        self.assertEqual(states, [(1, "ACTIVE"), (2, "DRAFT")])
        status = impl.command_status(ns(db=str(self.db)))
        self.assertEqual((status["workflow"]["workflow_revision"], status["task_id"]), (1, "FM-01"))

    def test_revision_two_history_permanently_blocks_rollback_to_revision_one(self):
        self.activate_revision2_fixture()
        run = self.begin()
        dispatch = impl.command_reserve_next(ns(db=str(self.db), run_id=run["run_id"]))
        self.assertEqual(dispatch["task_id"], "FM-01-A")
        impl.command_confirm(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            thread_id="thread-revision-2", client_thread_id=None,
            receipt_file=str(self.create_receipt(dispatch, thread_id="thread-revision-2")),
        ))
        impl.command_observe(ns(
            db=str(self.db), run_id=run["run_id"], dispatch_id=dispatch["dispatch_id"],
            status="completed", cursor="cursor-revision-2", turn_id="turn-revision-2",
            summary_sha256="8" * 64, thread_id="thread-revision-2", client_thread_id=None,
            binding_receipt_file=None,
        ))
        evidence = self.evidence("FM-01-A", dispatch=dispatch, name="revision-2-pass.json")
        impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-01-A",
            evidence_file=str(evidence),
        ))
        impl.command_finish(ns(
            db=str(self.db), run_id=run["run_id"], outcome="CONTINUE",
            reason="revision 2 attempt is terminal and retained as immutable history",
        ))

        with self.assertRaisesRegex(RuntimeError, "dispatch/attempt|forward revision"):
            self.activate_revision(revision=1, generation=2, from_revision=2)
        head = impl.command_workflow_head(ns(db=str(self.db)))
        self.assertEqual((head["active_workflow_revision"], head["generation"]), (2, 2))

    def test_carried_task_attempt_invalidates_carry_in_the_reservation_transaction(self):
        self.activate_revision2_fixture(fm00_bound_freshness=True)
        self.proof.write_text("changed proof makes carried FM-00 stale\n", encoding="utf-8")
        run = self.begin()
        self.assertEqual((run["decision"], run["task_id"]), ("REVALIDATION_REQUIRED", "FM-00"))
        reopened = impl.command_reopen_stale(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-00",
        ))
        self.assertEqual((reopened["decision"], reopened["task_id"]), ("READY", "FM-00"))
        evidence = self.evidence(
            "FM-00", name="revision-2-fm00-revalidation.json", freshness_contract="bound",
        )

        # 인위적으로 attempt INSERT를 실패시켜 선행 carry UPDATE도 함께 rollback되는지 확인한다.
        with connect(self.db) as connection:
            connection.execute(
                """CREATE TRIGGER test_abort_revision_two_attempt
                   BEFORE INSERT ON implementation_task_attempts
                   WHEN NEW.workflow_revision=2 AND NEW.task_id='FM-00'
                   BEGIN SELECT RAISE(ABORT, 'TEST_ABORT_ATTEMPT'); END"""
            )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "TEST_ABORT_ATTEMPT"):
            impl.command_review(ns(
                db=str(self.db), run_id=run["run_id"], task_id="FM-00",
                evidence_file=str(evidence),
            ))
        with connect(self.db) as connection:
            carry_after_abort = connection.execute(
                """SELECT invalidated_at,invalidated_by_attempt_no
                   FROM implementation_task_carry_forwards WHERE workflow_id=?
                     AND to_workflow_revision=2 AND to_task_id='FM-00'""",
                (impl.WORKFLOW_ID,),
            ).fetchone()
            attempt_count_after_abort = connection.execute(
                """SELECT COUNT(*) FROM implementation_task_attempts WHERE workflow_id=?
                     AND workflow_revision=2 AND task_id='FM-00'""",
                (impl.WORKFLOW_ID,),
            ).fetchone()[0]
            connection.execute("DROP TRIGGER test_abort_revision_two_attempt")
        self.assertEqual(carry_after_abort, (None, None))
        self.assertEqual(attempt_count_after_abort, 0)

        reviewed = impl.command_review(ns(
            db=str(self.db), run_id=run["run_id"], task_id="FM-00",
            evidence_file=str(evidence),
        ))
        self.assertEqual(reviewed["outcome"], "PASS")
        with connect(self.db) as connection:
            carry_after_success = connection.execute(
                """SELECT invalidated_at,invalidated_by_attempt_no
                   FROM implementation_task_carry_forwards WHERE workflow_id=?
                     AND to_workflow_revision=2 AND to_task_id='FM-00'""",
                (impl.WORKFLOW_ID,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT attempt_no,status FROM implementation_task_attempts WHERE workflow_id=?
                     AND workflow_revision=2 AND task_id='FM-00'""",
                (impl.WORKFLOW_ID,),
            ).fetchone()
        self.assertIsNotNone(carry_after_success[0])
        self.assertEqual(carry_after_success[1], 1)
        self.assertEqual(attempt, (1, "SUCCEEDED"))


if __name__ == "__main__":
    unittest.main()
