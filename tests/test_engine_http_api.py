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
        self._tmp = tempfile.TemporaryDirectory()
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
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
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


if __name__ == "__main__":
    unittest.main()
