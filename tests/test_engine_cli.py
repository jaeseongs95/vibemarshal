from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from flowmarshal.engine.cli import build_parser, main


class EngineCliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
