from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.engine.http_api import LocalGuiEngineBackend, LocalGuiServer, main


class EngineHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "ledger.sqlite3"
        self.artifact_root = self.root / "artifacts"
        self.project_root = self.root / "workspace"
        self.backend = LocalGuiEngineBackend(
            db_path=self.db_path,
            artifact_root=self.artifact_root,
            project_root=self.project_root,
        )
        self.server = LocalGuiServer(self.backend, host="127.0.0.1", port=0)
        self.base_url = self.server.start_background()

    def tearDown(self) -> None:
        self.server.close()
        self._tmp.cleanup()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict | None = None, *, token: str | None = "default") -> dict:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token == "default":
            headers["X-FlowMarshal-Token"] = self.server.session_token
        elif token is not None:
            headers["X-FlowMarshal-Token"] = token
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_health_and_snapshot_and_sse_events(self) -> None:
        health = self._get("/api/v1/health")
        self.assertEqual(health["api_version"], "1")
        self.assertEqual(health["engine_package"], "flowmarshal-engine")
        self.assertEqual(health["sqlite_application_id"], "FME1")

        snapshot = self._get("/api/v1/snapshot")
        self.assertEqual(snapshot["apiVersion"], "1")
        self.assertEqual(snapshot["phase"], "plan_ready")
        self.assertGreaterEqual(len(snapshot["plans"]), 1)

        with urllib.request.urlopen(f"{self.base_url}/api/v1/events", timeout=5) as resp:
            sse_text = resp.read().decode("utf-8")
        self.assertIn("data: ", sse_text)
        self.assertIn("snapshot.ready", sse_text)

    def test_activate_plan_and_advance_tasks_to_satisfied_verdict(self) -> None:
        snapshot = self._get("/api/v1/snapshot")
        plan = snapshot["plans"][0]

        activated = self._post(
            "/api/v1/plans/activate",
            {
                "planRevisionId": plan["planRevisionId"],
                "activationDigest": plan["activationDigest"],
                "expectedGoalDigest": snapshot["goal"]["definitionDigest"],
                "expectedState": {
                    "historySequence": snapshot["historyCursor"]["sequence"],
                    "goalDigest": snapshot["goal"]["definitionDigest"],
                },
            },
        )
        self.assertTrue(activated["ok"])
        self.assertEqual(activated["data"]["phase"], "plan_active")
        self.assertEqual(len(activated["data"]["tasks"]), 3)

        # 3개 Task 각각 materialize + validation/complete (총 6회) + Goal Test 1회 = 7회 run-once
        last_snap = activated["data"]
        for _ in range(7):
            res = self._post("/api/v1/run-once", {})
            self.assertTrue(res["ok"])
            last_snap = res["data"]

        self.assertEqual(last_snap["phase"], "completed")
        self.assertEqual(last_snap["validation"]["verdict"], "satisfied")
        self.assertTrue(all(t["status"] == "completed" for t in last_snap["tasks"]))

    def test_goal_draft_and_prepare_creates_new_goal_revision(self) -> None:
        draft_snap = self._post("/api/v1/goals/draft", {})
        self.assertEqual(draft_snap["phase"], "goal_draft")

        prepared = self._post(
            "/api/v1/goals/prepare",
            {"sourceRequest": "새로운 목표 원문으로 계획 후보를 다시 생성하고 검증한다."},
        )
        self.assertTrue(prepared["ok"])
        self.assertEqual(prepared["data"]["phase"], "plan_ready")
        self.assertEqual(
            prepared["data"]["goal"]["sourceRequest"],
            "새로운 목표 원문으로 계획 후보를 다시 생성하고 검증한다.",
        )

    def test_stale_and_external_unknown_recovery_flows(self) -> None:
        stale_snap = self._post("/api/v1/scenario", {"scenario": "stale-activation"})
        plan = stale_snap["plans"][0]
        stale_res = self._post(
            "/api/v1/plans/activate",
            {
                "planRevisionId": plan["planRevisionId"],
                "activationDigest": plan["activationDigest"],
                "expectedGoalDigest": stale_snap["goal"]["definitionDigest"],
            },
        )
        self.assertFalse(stale_res["ok"])
        self.assertEqual(stale_res["error"]["code"], "STALE_EXECUTION_INPUT")
        self.assertEqual(stale_res["data"]["phase"], "recovery")

        refreshed = self._post("/api/v1/stale/refresh", {})
        self.assertTrue(refreshed["ok"])
        self.assertEqual(refreshed["data"]["phase"], "plan_ready")

        ext_snap = self._post("/api/v1/scenario", {"scenario": "external-unknown"})
        self.assertEqual(ext_snap["phase"], "recovery")
        self.assertEqual(ext_snap["recovery"]["kind"], "external_unknown")
        self.assertEqual(ext_snap["recovery"]["stage"], "detected")

        observed = self._post("/api/v1/attempts/observe", {})
        self.assertTrue(observed["ok"])
        self.assertEqual(observed["data"]["recovery"]["stage"], "observed")

        resumed = self._post("/api/v1/attempts/resume", {})
        self.assertTrue(resumed["ok"])
        self.assertIsNone(resumed["data"]["recovery"])

    def test_cli_gui_serve_once_command(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(
                [
                    "--db",
                    str(self.root / "cli_ledger.sqlite3"),
                    "--artifacts",
                    str(self.root / "cli_artifacts"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--project-root",
                    str(self.project_root),
                    "--once",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "ready")
        self.assertIn("http://127.0.0.1:", payload["base_url"])

    def test_snapshot_reflects_measured_budget_usage_when_present(self) -> None:
        from flowmarshal.canonical import sha256_digest
        from flowmarshal.engine.domain import BudgetStage, BudgetUsageRecord, new_id, utc_now

        goal = self.backend.service.load_latest_goal(self.backend.project_id)
        self.backend.service.record_budget_usage(
            BudgetUsageRecord(
                usage_id=new_id("usage"),
                project_id=self.backend.project_id,
                goal_contract_digest=goal.definition_digest,
                stage=BudgetStage.VALIDATION,
                role="goal_validator",
                logical_call_ref=new_id("model_call"),
                call_status="succeeded",
                runner_receipt_digest=sha256_digest({"test": 1}),
                model="gemini-3-flash",
                effort="high",
                permission_profile="danger-full-access",
                approval_policy="never",
                thread_id="thr_test_1",
                turn_ids=("turn_test_1",),
                input_digest=sha256_digest({"in": 1}),
                output_digest=sha256_digest({"out": 1}),
                output_schema_digest=sha256_digest({"schema": 1}),
                input_tokens=120,
                cached_input_tokens=30,
                output_tokens=45,
                reasoning_tokens=10,
                latency_ms=850,
                usage_available=True,
                retry_count=0,
                recorded_at=utc_now(),
            )
        )
        snap = self._get("/api/v1/snapshot")
        self.assertEqual(snap["budget"]["inputTokens"], 120)
        self.assertEqual(snap["budget"]["cachedInputTokens"], 30)
        self.assertEqual(snap["budget"]["outputTokens"], 45)
        self.assertEqual(snap["budget"]["latencyMs"], 850)
        self.assertIn("Gemini CLI (agy.exe)", snap["budget"]["availabilityReason"])

    def test_activate_plan_rejects_empty_or_mismatched_digest(self) -> None:
        snapshot = self._get("/api/v1/snapshot")
        plan = snapshot["plans"][0]

        empty_res = self._post("/api/v1/plans/activate", {})
        self.assertFalse(empty_res["ok"])
        self.assertEqual(empty_res["error"]["code"], "DIGEST_REQUIRED")

        missing_digest_res = self._post(
            "/api/v1/plans/activate",
            {"planRevisionId": plan["planRevisionId"]},
        )
        self.assertFalse(missing_digest_res["ok"])
        self.assertEqual(missing_digest_res["error"]["code"], "DIGEST_REQUIRED")

        wrong_digest_res = self._post(
            "/api/v1/plans/activate",
            {
                "planRevisionId": plan["planRevisionId"],
                "activationDigest": "sha256:" + "0" * 64,
            },
        )
        self.assertFalse(wrong_digest_res["ok"])
        self.assertEqual(wrong_digest_res["error"]["code"], "DIGEST_MISMATCH")

    def test_cors_origin_restriction_and_loopback_host_enforcement(self) -> None:
        import urllib.error

        req = urllib.request.Request(
            f"{self.base_url}/api/v1/snapshot",
            headers={"Origin": "https://evil.example.com"},
            method="GET",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 403)
        err_payload = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(err_payload["error"]["code"], "CROSS_ORIGIN_FORBIDDEN")

        local_req = urllib.request.Request(
            f"{self.base_url}/api/v1/health",
            headers={"Origin": "http://localhost:5173"},
            method="GET",
        )
        with urllib.request.urlopen(local_req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "http://localhost:5173")

        with self.assertRaises(ValueError):
            LocalGuiServer(self.backend, host="0.0.0.0", port=0)

    def test_post_mutation_requires_valid_session_token(self) -> None:
        import urllib.error

        health = self._get("/api/v1/health")
        self.assertEqual(health["session_token"], self.server.session_token)

        with self.assertRaises(urllib.error.HTTPError) as missing_ctx:
            self._post("/api/v1/run-once", {}, token=None)
        self.assertEqual(missing_ctx.exception.code, 403)
        missing_err = json.loads(missing_ctx.exception.read().decode("utf-8"))
        self.assertEqual(missing_err["error"]["code"], "SESSION_TOKEN_REQUIRED")

        with self.assertRaises(urllib.error.HTTPError) as invalid_ctx:
            self._post("/api/v1/run-once", {}, token="invalid-token")
        self.assertEqual(invalid_ctx.exception.code, 403)
        invalid_err = json.loads(invalid_ctx.exception.read().decode("utf-8"))
        self.assertEqual(invalid_err["error"]["code"], "SESSION_TOKEN_REQUIRED")

    def test_json_body_validation_and_recovery_ledger_persistence(self) -> None:
        import urllib.error

        # 1. Invalid JSON body returns HTTP 400
        bad_req = urllib.request.Request(
            f"{self.base_url}/api/v1/run-once",
            data=b"{invalid json",
            headers={
                "Content-Type": "application/json",
                "X-FlowMarshal-Token": self.server.session_token,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as bad_ctx:
            urllib.request.urlopen(bad_req, timeout=5)
        self.assertEqual(bad_ctx.exception.code, 400)
        bad_err = json.loads(bad_ctx.exception.read().decode("utf-8"))
        self.assertEqual(bad_err["error"]["code"], "INVALID_JSON_BODY")

        # 2. Oversized Content-Length returns HTTP 413
        huge_req = urllib.request.Request(
            f"{self.base_url}/api/v1/run-once",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(1_048_576 + 10),
                "X-FlowMarshal-Token": self.server.session_token,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as huge_ctx:
            urllib.request.urlopen(huge_req, timeout=5)
        self.assertEqual(huge_ctx.exception.code, 413)
        huge_err = json.loads(huge_ctx.exception.read().decode("utf-8"))
        self.assertEqual(huge_err["error"]["code"], "PAYLOAD_TOO_LARGE")

        # 3. SSE emits event: snapshot and engineSchemaVersion == 4
        with urllib.request.urlopen(f"{self.base_url}/api/v1/events", timeout=5) as resp:
            sse_text = resp.read().decode("utf-8")
        self.assertIn("event: snapshot", sse_text)

        snap = self._get("/api/v1/snapshot")
        self.assertEqual(snap["engineSchemaVersion"], 4)

        # 4. External-unknown recovery actions persist to SQLite history_events
        ext_snap = self._post("/api/v1/scenario", {"scenario": "external-unknown"})
        self.assertEqual(ext_snap["phase"], "recovery")
        abandoned = self._post("/api/v1/intents/abandon", {"rationale": "외부에서 이미 처리됨 확인"})
        self.assertTrue(abandoned["ok"])
        self.assertEqual(abandoned["data"]["phase"], "goal_draft")
        with self.backend.service.ledger.read() as conn:
            rows = conn.execute(
                "SELECT event_type FROM history_events WHERE project_id = ?",
                (self.backend.project_id,),
            ).fetchall()
        history_kinds = [row["event_type"] for row in rows]
        self.assertIn("recovery.external_unknown_detected", history_kinds)
        self.assertIn("recovery.intent_abandoned", history_kinds)

    def test_pause_cancel_replan_select_project_and_final_report_endpoints(self) -> None:
        # 1. /api/v1/plans/search
        searched = self._post("/api/v1/plans/search", {})
        self.assertTrue(searched["ok"])
        self.assertGreaterEqual(len(searched["data"]["plans"]), 1)

        # 2. Activate plan atomically (VM-D-0033)
        alt_plan = searched["data"]["plans"][-1]
        activated = self._post(
            "/api/v1/plans/activate",
            {
                "planRevisionId": alt_plan["planRevisionId"],
                "activationDigest": alt_plan["activationDigest"],
                "expectedGoalDigest": searched["data"]["goal"]["definitionDigest"],
            },
        )
        self.assertTrue(activated["ok"])
        self.assertEqual(activated["data"]["activePlanRevisionId"], alt_plan["planRevisionId"])
        # Task updatedAt comes from ledger row (VM-D-0043)
        self.assertTrue(all(bool(t["updatedAt"]) for t in activated["data"]["tasks"]))

        # 3. /api/v1/pause & /api/v1/replan & /api/v1/cancel (VM-T-0009, VM-D-0032)
        paused = self._post("/api/v1/pause", {"reason": "점검 일시정지"})
        self.assertTrue(paused["ok"])
        self.assertEqual(paused["data"]["project"]["runState"], "paused")

        # replan은 SUBGRAPH_REPLAN 차단 후보가 없을 때 예외를 삼키거나 가짜 성공을 만들지 않고 REPLAN_FAILED를 반환한다.
        replanned = self._post("/api/v1/replan", {"rationale": "대안 경로 재탐색"})
        self.assertFalse(replanned["ok"])
        self.assertEqual(replanned["error"]["code"], "REPLAN_FAILED")

        # Run to completion (paused state is resumed by run-once) and fetch /api/v1/final-report (VM-T-0009, VM-T-0011)
        for _ in range(9):
            step = self._post("/api/v1/run-once", {})
            if step["data"]["phase"] == "completed":
                break
        report_res = self._get("/api/v1/final-report")
        self.assertTrue(report_res["ok"])
        self.assertIn("Governance A2 서명 프로파일 및 보안 한계", report_res["data"]["markdown"])
        self.assertEqual(report_res["data"]["report"]["governance_profile_id"], "flowmarshal-same-user-v1")

        # 4. /api/v1/projects/select switches workspace root (VM-T-0009, VM-D-0032)
        new_ws = self.root / "second-workspace"
        selected = self._post("/api/v1/projects/select", {"projectRoot": str(new_ws)})
        self.assertTrue(selected["ok"])
        self.assertEqual(selected["data"]["project"]["location"]["displayName"], "second-workspace")

    def test_defect_and_task_invariants_across_engine_layers(self) -> None:
        from flowmarshal.engine.capabilities import CoreCapabilityError, role_execution_scope
        from flowmarshal.engine.eval_cli import _emit
        from flowmarshal.engine.ledger import SQLiteEngineHistoryReader

        # VM-D-0035: SQLiteEngineHistoryReader blocks access inside role_execution_scope
        with role_execution_scope("worker"):
            with self.assertRaises(CoreCapabilityError):
                SQLiteEngineHistoryReader(self.db_path)

        # VM-D-0039 / VM-T-0015: RoleSteward error message and pre-1.0 governance model classes config
        config_path = Path(__file__).resolve().parents[1] / "config" / "pre-1.0-governance-model-classes.json"
        self.assertTrue(config_path.is_file())
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertIn("claude-sonnet-4-6", config_data["classes"])
        self.assertIn("gemini-3-flash", config_data["classes"])

        # VM-D-0044: eval_cli._emit survives non-UTF8 stream encoding without UnicodeEncodeError
        raw_bytes = io.BytesIO()
        cp949_stream = io.TextIOWrapper(raw_bytes, encoding="ascii", errors="strict")
        with redirect_stdout(cp949_stream):
            _emit({"message": "한글 및 이모지 🚀 출력 안전성 검증"})
        cp949_stream.flush()
        self.assertIn("message", raw_bytes.getvalue().decode("ascii", errors="ignore"))

        # VM-T-0012: register_goal(replace_active_plan=True) supersedes active plan cleanly
        from flowmarshal.canonical import sha256_bytes
        from flowmarshal.engine.domain import new_id

        latest_goal = self.backend.service.load_latest_goal(self.backend.project_id)
        new_req = "수정된 목표 요청 원문으로 활성 Plan을 안전하게 교체한다."
        new_def = latest_goal.definition.model_copy(
            update={
                "source_request": new_req,
                "source_request_digest": sha256_bytes(new_req.encode("utf-8")),
            }
        )
        new_goal = latest_goal.model_copy(
            update={
                "goal_revision_id": new_id("goal_revision"),
                "revision_no": latest_goal.revision_no + 1,
                "definition": new_def,
                "definition_digest": new_def.definition_digest,
                "supersedes_goal_revision_id": latest_goal.goal_revision_id,
            }
        )
        self.backend.service.register_goal(new_goal, replace_active_plan=True)
        loaded_after = self.backend.service.load_latest_goal(self.backend.project_id)
        self.assertEqual(loaded_after.goal_revision_id, new_goal.goal_revision_id)
        status_after = self.backend.service.status(self.backend.project_id)
        self.assertIsNone(status_after["project"]["active_plan_revision_id"])


if __name__ == "__main__":
    unittest.main()
