from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetManager, GoalBudgetPolicy
from flowmarshal.engine.cli import _runtime, build_parser, main
from flowmarshal.engine.domain import new_id, utc_now
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.roles import RoleCallReceipt, RoleCallRequest, strict_json_output_schema
from flowmarshal.engine.service import EngineService
from flowmarshal.engine.runtime import CodexProjectBinding
from tests.engine_helpers import profile


class EngineCliTests(unittest.TestCase):
    def test_run_once_passes_explicit_codex_project_binding_to_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binding = CodexProjectBinding(
                project_id="server-project-id", expected_root=str(root), expected_name="테스트 프로젝트"
            )
            path = root / "codex-project.json"
            path.write_text(binding.model_dump_json(), encoding="utf-8")
            args = build_parser().parse_args([
                "--codex-project-binding", str(path), "run", "once",
                "--project-id", "engine-project-id", "--codex-bin", "pinned-codex.exe",
            ])
            with patch("flowmarshal.engine.cli.CodexAppServerRuntime") as runtime:
                _runtime(args)
            runtime.assert_called_once_with(codex_bin="pinned-codex.exe", project_binding=binding)

    def test_public_command_surface_is_present(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        for command in ("project", "goal", "plan", "task", "run", "attempt", "validate", "recover", "report"):
            self.assertIn(command, help_text)

    def test_project_goal_and_planning_context_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            db = base / "state" / "flowmarshal-engine.sqlite3"
            artifacts = base / "artifacts"
            common = ["--db", str(db), "--artifacts", str(artifacts)]

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    common
                    + [
                        "project",
                        "init",
                        "--name",
                        "CLI project",
                        "--root",
                        str(project),
                    ]
                )
            self.assertEqual(0, code, output.getvalue())
            project_id = json.loads(output.getvalue())["project_id"]

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    common
                    + [
                        "goal",
                        "create",
                        "--project-id",
                        project_id,
                        "--request",
                        "검증 가능한 변경을 한다.",
                        "--outcome",
                        "테스트가 통과한다.",
                        "--acceptance",
                        "단위 테스트가 통과한다.",
                    ]
                )
            self.assertEqual(0, code, output.getvalue())
            self.assertIn("definition_digest", json.loads(output.getvalue()))

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(common + ["goal", "show", "--project-id", project_id])
            self.assertEqual(0, code, output.getvalue())
            shown = json.loads(output.getvalue())
            self.assertTrue(shown["is_active"])
            self.assertEqual("active", shown["ledger_status"])
            self.assertEqual("ready", shown["goal_contract"]["status"])

            output = io.StringIO()
            with redirect_stdout(output):
                code = main(common + ["plan", "search", "--project-id", project_id])
            self.assertEqual(0, code, output.getvalue())
            context = json.loads(output.getvalue())
            self.assertEqual("context_ready", context["status"])
            self.assertTrue(context["project_map_digest"].startswith("sha256:"))
            self.assertTrue(context["state_snapshot_digest"].startswith("sha256:"))

    def test_budget_show_projects_pre_goal_provider_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "project"
            root.mkdir()
            db = base / "state" / "flowmarshal-engine.sqlite3"
            artifacts = base / "artifacts"
            service = EngineService(SQLiteEngineLedger(db, artifact_root=artifacts))
            service.initialize()
            project_id = service.create_project(name="CLI pre-Goal", root=root)
            service.register_profile(profile(project_id))
            manager = BudgetManager(service)
            manager.configure(
                project_id,
                GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=100),
            )
            goal_id = new_id("goal")
            schema = strict_json_output_schema({
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            })
            request = RoleCallRequest(
                role="goal_reviewer", instructions="schema를 검사합니다.", payload={},
                output_schema=schema, model="test-model", effort="medium",
                inventory_digest="sha256:" + "a" * 64, cwd=str(root),
            )
            call_id = manager.reserve(
                project_id=project_id, goal_id=goal_id, goal_digest=None,
                call_key="pre-goal-cli", role=request.role,
                request=request.model_dump(mode="json"),
            )
            manager.settle(call_id, RoleCallReceipt(
                call_id="pre-goal-cli", role=request.role, status="schema_failed",
                model=request.model, effort=request.effort,
                inventory_digest=request.inventory_digest,
                permission_profile=":danger-full-access", approval_policy="never",
                input_digest=request.request_digest,
                output_schema_digest=sha256_digest(schema),
                input_tokens=7, cached_input_tokens=2, output_tokens=3,
                reasoning_tokens=1, usage_available=True, latency_ms=13,
                recorded_at=utc_now(),
            ))

            output = io.StringIO()
            with redirect_stdout(output):
                code = main([
                    "--db", str(db), "--artifacts", str(artifacts),
                    "project", "budget", "show", "--project-id", project_id,
                    "--goal-id", goal_id,
                ])

            self.assertEqual(0, code, output.getvalue())
            shown = json.loads(output.getvalue())
            self.assertEqual(10, shown["budget"]["measured_token_subtotal"])
            self.assertEqual("GOAL_REVISION_NOT_CREATED", shown["usage"]["goal_revision_unavailable_reason"])
            self.assertEqual(7, shown["usage"]["input_tokens"]["total"])
            self.assertEqual(3, shown["usage"]["output_tokens"]["total"])
            self.assertEqual(1, len(shown["usage"]["provider_receipt_usage"]))


if __name__ == "__main__":
    unittest.main()
