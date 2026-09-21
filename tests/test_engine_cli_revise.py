"""CLI facade `revise`가 EngineApplication.revise를 호출하고 결과·오류 code를 내보내는지 고정한다."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from flowmarshal.engine import cli
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.qualification import default_role_configuration
from tests.test_engine_application_revise import ledger_rows
from tests.test_engine_ledger_service import EngineServiceFixture


ROOT = Path(__file__).resolve().parents[1]


class EngineCliReviseTests(EngineServiceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.role_config = self.base / "roles.json"
        self.role_config.write_text(
            default_role_configuration(ROOT).model_dump_json(), encoding="utf-8"
        )
        self.argv = [
            "--db", str(self.ledger.path), "--artifacts", str(self.ledger.artifact_root),
            "revise", "--project-id", self.project_id, "--request", "수정 요청",
            "--role-config", str(self.role_config),
        ]

    def run_cli(self, argv: list[str]) -> tuple[int, dict, MagicMock]:
        runtime = MagicMock()
        runtime.__enter__.return_value = runtime
        output = io.StringIO()
        with patch("flowmarshal.engine.cli._runtime", return_value=runtime), redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue()), runtime

    def test_parser_routes_revise_to_its_handler(self) -> None:
        parsed = cli.build_parser().parse_args(self.argv + ["--candidate-count", "2"])
        self.assertEqual("revise", parsed.command)
        self.assertIs(cli._cmd_revise, parsed.handler)
        self.assertEqual(2, parsed.candidate_count)
        self.assertEqual("plan-inspection-v1", parsed.inspection_contract)
        self.assertFalse(hasattr(parsed, "plan_revision_id"))

    def test_handler_calls_application_revise_and_emits_result(self) -> None:
        emitted = {"status": "ready_for_authorization", "project_id": self.project_id}
        with patch.object(EngineApplication, "revise", autospec=True, return_value=emitted) as revise:
            code, payload, runtime = self.run_cli(self.argv + ["--candidate-count", "2"])
        self.assertEqual(0, code)
        self.assertEqual(emitted, payload)
        revise.assert_called_once()
        application, project_id = revise.call_args.args
        self.assertIs(runtime, application.runtime)
        self.assertEqual(self.project_id, project_id)
        self.assertEqual(
            {"source_request": "수정 요청", "candidate_count": 2}, revise.call_args.kwargs
        )

    def test_active_plan_is_reported_as_error_code_without_runtime_calls_or_writes(self) -> None:
        self.activate()
        before = ledger_rows(self.ledger)
        code, payload, runtime = self.run_cli(self.argv)
        self.assertEqual(2, code)
        self.assertEqual("EngineApplicationError", payload["error"])
        self.assertEqual("GOAL_REVISION_ACTIVE_PLAN", payload["error_code"])
        # CLI는 prepare처럼 runtime을 연 뒤 revise를 부르지만, 거절 전 runtime method 호출은 없다.
        self.assertEqual([], runtime.method_calls)
        self.assertEqual(before, ledger_rows(self.ledger))
