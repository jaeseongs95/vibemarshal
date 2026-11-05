from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from flowmarshal.engine.agy_gemini_bridge import (
    AgyLocalGovernance,
    create_agy_gemini_runtime,
    default_agy_gemini_role_configuration,
    resolve_agy_exe,
)
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.domain import (
    Criticality,
    GoalVerdictStatus,
    LifecycleStage,
    ProjectProfileDefinition,
    ProjectProfileRevision,
    RevisionStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.http_api import LocalGuiEngineBackend
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.service import EngineService


class EngineAgyGeminiE2ETests(unittest.TestCase):
    def test_live_agy_gemini_full_workflow_and_gui_snapshot(self) -> None:
        agy_exe = resolve_agy_exe()
        self.assertTrue(agy_exe.exists(), f"Antigravity CLI not found at {agy_exe}")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            root = Path(tmp_dir).resolve()
            db_path = root / "ledger.sqlite3"
            artifact_root = root / "artifacts"
            workspace_root = root / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            (workspace_root / "README.md").write_text(
                "# Calculator E2E Workspace\n", encoding="utf-8"
            )
            (workspace_root / "AGENTS.md").write_text(
                "# Project Instructions\nFollow standard Python unittest conventions.\n",
                encoding="utf-8",
            )

            ledger = SQLiteEngineLedger(db_path, artifact_root=artifact_root)
            service = EngineService(ledger)
            service.initialize()

            project_id = service.create_project(
                name="Agy Gemini Calculator E2E",
                root=str(workspace_root),
            )
            profile_def = ProjectProfileDefinition(
                product_goal="Build and verify calculator module using real Antigravity Gemini CLI.",
                lifecycle_stage=LifecycleStage.DEVELOPMENT,
                criticality=Criticality.MEDIUM,
                compatibility_policy="Preserve public function signatures and exit codes.",
                validation_policy=("Pass all deterministic unit tests via unittest.",),
                runtime_requirements=("local Windows", "Python 3.10+"),
            )
            profile_rev = ProjectProfileRevision(
                profile_revision_id=new_id("profile_revision"),
                project_id=project_id,
                revision_no=1,
                definition=profile_def,
                definition_digest=profile_def.definition_digest,
                status=RevisionStatus.READY,
                created_at=utc_now(),
            )
            service.register_profile(profile_rev)

            runtime = create_agy_gemini_runtime(state_root=root / "gemini-state")
            roles = default_agy_gemini_role_configuration()
            governance = AgyLocalGovernance()

            app = EngineApplication(
                service,
                runtime=runtime,
                role_configuration=roles,
                governance=governance,
            )

            # 1. Prepare Goal & Plan via real Antigravity CLI (agy.exe)
            prep = app.prepare(
                project_id,
                source_request=(
                    "calc_app.py에 사칙연산(add, subtract, multiply, divide) 함수를 구현하고 "
                    "test_calc_app.py로 단위 테스트를 작성해 검증해줘."
                ),
                candidate_count=1,
            )
            self.assertEqual(
                prep.status,
                "ready_for_authorization",
                f"planning={prep.planning.model_dump(mode='json') if prep.planning else None}",
            )
            self.assertIsNotNone(prep.planning)
            self.assertIsNotNone(prep.planning.selected_activation_digest)

            # 2. Authorize Goal & auto-activate Plan
            authority = ApplicationAuthority(app)
            target = authority.authorization_target(project_id)
            auth_result = authority.authorize(
                project_id,
                target=target,
                source="e2e-test:agy-gemini",
            )
            self.assertIsNotNone(auth_result.activation_id)

            # 3. Advance run_once / observe loop until GoalVerdict is SATISFIED
            for _ in range(25):
                tick = app.run_once(project_id)
                for _ in range(60):
                    if service.active_runtime_job(project_id) is None:
                        break
                    app.observe(project_id)
                    time.sleep(0.1)
                status_now = service.status(project_id)
                if status_now.get("goal_verdict") is not None:
                    break

            self.assertTrue((workspace_root / "calc_app.py").exists())
            self.assertTrue((workspace_root / "test_calc_app.py").exists())

            report = app.final_report(project_id)
            self.assertIsNotNone(report.verdict)
            self.assertEqual(report.verdict.status, GoalVerdictStatus.SATISFIED)
            self.assertGreater(report.usage.input_tokens.known_subtotal, 0)
            self.assertGreater(report.usage.output_tokens.known_subtotal, 0)
            self.assertGreater(report.usage.latency_ms.known_subtotal, 0)
            self.assertEqual(report.usage.incomplete_reasons, ())
            self.assertTrue(
                any(
                    item.observed_model == "gemini-3-flash"
                    and item.observed_source == "gemini_session_transcript"
                    for item in report.execution_summary.model_observations
                )
            )

            # 4. Verify GUI Backend Snapshot over the same ledger
            gui_backend = LocalGuiEngineBackend(
                db_path=db_path,
                artifact_root=artifact_root,
                project_root=workspace_root,
                project_id=project_id,
                runtime=runtime,
                role_configuration=roles,
                live_gemini=True,
            )
            snap = gui_backend.snapshot()
            self.assertEqual(snap["phase"], "completed")
            self.assertEqual(snap["validation"]["verdict"], "satisfied")
            self.assertIsNotNone(snap["budget"]["inputTokens"])
            self.assertGreater(snap["budget"]["inputTokens"], 0)
            self.assertGreater(snap["budget"]["outputTokens"], 0)


if __name__ == "__main__":
    unittest.main()
