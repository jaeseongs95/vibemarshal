from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from flowmarshal.engine.agy_gemini_bridge import (
    create_agy_gemini_runtime,
    default_agy_gemini_role_configuration,
    resolve_agy_exe,
)
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.roles import CodexStructuredRoleRunner
from flowmarshal.engine.domain import (
    Criticality,
    GoalOperatingPolicy,
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

            app = EngineApplication(
                service,
                runtime=runtime,
                role_configuration=roles,
                structured_runner=CodexStructuredRoleRunner(
                    runtime,
                    max_schema_recovery_attempts=1,
                    ephemeral_threads=False,
                ),
                governance=ALLOW_ALL,
            )

            # 1. Prepare Goal & Plan via real Antigravity CLI (agy.exe)
            prep = app.prepare(
                project_id,
                source_request=(
                    "단일 Task 하나로 calc_app.py에 사칙연산(add, subtract, multiply, divide) 함수를 구현하고 "
                    "test_calc_app.py로 단위 테스트를 함께 작성해 검증해줘. "
                    "(calc_app.py와 test_calc_app.py를 분리된 Task로 나누지 말고 하나의 Task에서 모두 생성·검증할 것)"
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
            selected_plan = next(
                ev.plan
                for ev in prep.planning.plan_evaluations
                if ev.plan.activation_digest == prep.planning.selected_activation_digest
            )
            tasks = selected_plan.definition.tasks
            authority = ApplicationAuthority(app)
            policy = GoalOperatingPolicy(
                max_same_failure_replans=max(
                    [2, *[t.recovery.max_same_failure_replans for t in tasks]]
                ),
                max_goal_replans=max(
                    [2, *[t.recovery.max_goal_replans for t in tasks]]
                ),
                requires_new_evidence=all(
                    t.recovery.requires_new_evidence for t in tasks
                ),
                resume_strategy=(
                    tasks[0].recovery.resume_strategy
                    if tasks
                    else GoalOperatingPolicy().resume_strategy
                ),
                planning_budget=selected_plan.definition.planning_budget,
            )
            target = authority.authorization_target(
                project_id,
                operating_policy=policy,
            )
            auth_result = authority.authorize(
                project_id,
                target=target,
                source="e2e-test:agy-gemini",
                operating_policy=policy,
            )
            self.assertIsNotNone(auth_result.activation_id)

            # 3. Advance run_once / observe loop until GoalVerdict is SATISFIED
            last_tick = None
            for _ in range(45):
                last_tick = app.run_once(project_id)
                for _ in range(900):
                    if service.active_runtime_job(project_id) is None:
                        break
                    app.observe(project_id)
                    time.sleep(0.25)
                app.observe(project_id)
                status_now = app.status(project_id)
                if status_now.get("project", {}).get("run_state") == "completed":
                    break
                if last_tick.action.value == "blocked" and service.active_runtime_job(project_id) is None:
                    break

            diag = f"last_tick={last_tick.model_dump(mode='json') if last_tick else None}, status={app.status(project_id)}"
            self.assertTrue((workspace_root / "calc_app.py").exists(), diag)
            self.assertTrue((workspace_root / "test_calc_app.py").exists(), diag)

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
