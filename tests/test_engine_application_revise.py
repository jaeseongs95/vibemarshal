"""EngineApplication.revise의 active Plan 가드, 같은 Goal의 다음 revision, 새 승인 경계를 고정한다."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flowmarshal.engine.application import (
    ApplicationAuthority,
    EngineApplication,
    EngineApplicationError,
)
from flowmarshal.engine.domain import RunOnceAction, ValidationExecutionStep
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService, GoalAuthorizationRequired
from tests.engine_inspection_helpers import InspectionScriptedRunner
from tests.fixtures.engine.governance import multitask
from tests.test_engine_qualification import qualification_inventory
from tests.test_engine_user_facade import _responses


ROOT = Path(__file__).resolve().parents[1]
REQUEST = "두 단계 변경을 구현하고 각각 검증해 주세요."
PREPARE_ROLES = [
    "goal_normalizer",
    "goal_reviewer",
    "skeleton_generator",
    "skeleton_reviewer",
    "plan_expander",
    "compact_plan_reviewer",
]


def ledger_rows(ledger) -> dict:
    """모든 테이블 행 수와 권위 상태 행을 비교 가능한 값으로 읽는다."""
    with ledger.read() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        rows = {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        for table in ("projects", "goal_revisions", "plan_revisions"):
            rows[f"{table}:rows"] = [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
    return rows


class EngineApplicationReviseTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        # multitask fixture를 add 수정 Task 하나로 줄여 첫 Goal을 짧게 SATISFIED까지 실행한다.
        step = multitask.STEPS[0]
        for name, value in (
            ("STEPS", (step,)),
            ("EDGES", ()),
            ("STATEMENTS", {step.criterion: multitask.STATEMENTS[step.criterion]}),
        ):
            patcher = mock.patch.object(multitask, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)
        self.workspace = multitask.copy_fixture(base)
        self.prepared = multitask.prepare(
            workspace=self.workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=self.roles,
        )
        self.service = self.prepared.service
        self.project_id = self.prepared.project_id
        self.first_goal = self.service.load_active_goal(self.project_id)

    def application(self, runner) -> tuple[EngineApplication, FakeCodexRuntime]:
        runtime = FakeCodexRuntime(self.inventory)
        return (
            EngineApplication(
                self.service,
                runtime=runtime,
                role_configuration=self.roles,
                structured_runner=runner,
            ),
            runtime,
        )

    def active_plan_revision_id(self) -> str | None:
        return self.service.status(self.project_id)["project"]["active_plan_revision_id"]

    def satisfy_first_goal(self) -> None:
        """기존 결정적 실행 경로로 첫 Goal을 SATISFIED까지 진행해 active Plan을 비운다."""
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(
            self.service,
            runtime,
            proposal_provider=multitask.MultitaskProposals(self.prepared),
        )
        goal_step = ValidationExecutionStep(
            validation_id="validation_goal", method="deterministic",
            argv=(sys.executable, "-m", "unittest", "test_app"),
            working_directory=str(self.workspace), timeout_seconds=60,
            expected_exit_codes=(0,), required_evidence_kinds=("test",),
        )
        for _ in range(40):
            outcome = dispatcher.run_once(self.project_id, goal_validation_step=goal_step)
            if outcome.action is RunOnceAction.DISPATCHED and outcome.attempt_id is not None:
                app = self.workspace / "app.py"
                app.write_text(
                    app.read_text(encoding="utf-8").replace("left - right", "left + right"),
                    encoding="utf-8",
                )
                runtime.complete(list(runtime.threads)[-1], response="worker completed")
            if outcome.action is RunOnceAction.BLOCKED or outcome.goal_verdict_id is not None:
                break
        else:
            self.fail("run_once가 40회 안에 끝나지 않았다")
        self.assertEqual(RunOnceAction.COMPLETED, outcome.action, outcome.detail)
        self.assertIsNone(self.active_plan_revision_id())

    def revise(self, responses: dict | None = None):
        runner = InspectionScriptedRunner(responses or _responses())
        application, runtime = self.application(runner)
        with mock.patch.object(runtime, "list_models", wraps=runtime.list_models) as list_models:
            result = application.revise(self.project_id, source_request=REQUEST)
        self.assertEqual(1, list_models.call_count)
        self.assertEqual(PREPARE_ROLES, [call.role for call in runner.calls])
        return result

    def blocked_changes(self) -> set[tuple[str, str]]:
        """기존 승인으로 Core 자동 활성화를 시도하면 원장 변화 없이 typed 경계가 나와야 한다."""
        before = ledger_rows(self.service.ledger)
        with self.assertRaises(GoalAuthorizationRequired) as raised:
            self.service.activate_selected_plan(project_id=self.project_id)
        self.assertEqual(before, ledger_rows(self.service.ledger))
        return {(item["boundary"], item["field"]) for item in raised.exception.changes}

    def test_active_plan_rejects_before_role_runtime_and_ledger_writes(self) -> None:
        self.assertIsNotNone(self.active_plan_revision_id())
        runner = InspectionScriptedRunner(_responses())
        application, runtime = self.application(runner)
        before = ledger_rows(self.service.ledger)

        with mock.patch.object(runtime, "list_models", wraps=runtime.list_models) as list_models:
            with self.assertRaises(EngineApplicationError) as raised:
                application.revise(self.project_id, source_request=REQUEST)
            with self.assertRaisesRegex(EngineApplicationError, "^SOURCE_REQUEST_REQUIRED$"):
                application.revise(self.project_id, source_request="  ")

        self.assertTrue(str(raised.exception).startswith("GOAL_REVISION_ACTIVE_PLAN: "))
        list_models.assert_not_called()
        self.assertEqual([], runner.calls)
        self.assertEqual((0, 0, 0), (runtime.create_calls, runtime.turn_calls, runtime.read_calls))
        self.assertEqual(before, ledger_rows(self.service.ledger))

    def test_revision_after_satisfied_goal_plans_without_activation(self) -> None:
        self.satisfy_first_goal()
        before = ledger_rows(self.service.ledger)

        result = self.revise()

        self.assertEqual("ready_for_authorization", result.status)
        revised = self.service.load_active_goal(self.project_id)
        self.assertEqual(result.goal_preparation.goal_contract.goal_revision_id, revised.goal_revision_id)
        self.assertEqual(self.first_goal.goal_id, revised.goal_id)
        self.assertEqual(self.first_goal.revision_no + 1, revised.revision_no)
        self.assertEqual(self.first_goal.goal_revision_id, revised.supersedes_goal_revision_id)
        after = ledger_rows(self.service.ledger)
        with self.service.ledger.read() as connection:
            first_status = connection.execute(
                "SELECT status FROM goal_revisions WHERE id=?", (self.first_goal.goal_revision_id,)
            ).fetchone()[0]
            searches = connection.execute(
                "SELECT count(*) FROM history_events WHERE project_id=? "
                "AND event_type='planning.search_recorded'", (self.project_id,)
            ).fetchone()[0]
            selected_status = connection.execute(
                "SELECT status FROM plan_revisions WHERE activation_digest=?",
                (result.planning.selected_activation_digest,),
            ).fetchone()[0]
        self.assertEqual("superseded", first_status)
        self.assertIsNotNone(result.planning_search_id)
        self.assertEqual(1, searches)
        self.assertEqual("ready", selected_status)
        self.assertEqual(before["plan_activations"], after["plan_activations"])
        self.assertEqual(before["goal_authorizations"], after["goal_authorizations"])
        self.assertIsNone(self.active_plan_revision_id())

        # 효과 정책이 같아도 새 Goal digest는 기존 승인 경계 밖이다.
        changes = self.blocked_changes()
        self.assertIn(("goal", "goal_contract_digest"), changes)
        self.assertNotIn(("effect", "effect_policy"), changes)
        self.assertFalse({boundary for boundary, _field in changes} & {"project"})

    def test_widened_effect_revision_needs_new_target_authorization(self) -> None:
        self.satisfy_first_goal()
        responses = _responses()
        responses["goal_normalizer"][0]["mutation_policy"] = "structural_change"

        result = self.revise(responses)

        self.assertEqual("ready_for_authorization", result.status)
        changes = self.blocked_changes()
        self.assertIn(("goal", "goal_contract_digest"), changes)
        self.assertIn(("effect", "effect_policy"), changes)
        self.assertFalse({boundary for boundary, _field in changes} & {"project"})
        self.assertIsNone(self.active_plan_revision_id())

        # 새 ApplicationAuthority가 표시한 새 Goal target으로 승인해야만 새 Plan이 활성화된다.
        before = ledger_rows(self.service.ledger)
        authority = ApplicationAuthority(EngineApplication(EngineService(self.service.ledger)))
        target = authority.authorization_target(self.project_id)
        revised = self.service.load_active_goal(self.project_id)
        self.assertEqual(revised.goal_revision_id, target.goal_revision_id)
        self.assertEqual(revised.definition_digest, target.goal_contract_digest)
        self.assertEqual("structural_change", target.effect_policy.mutation_policy.value)
        authorized = authority.authorize(self.project_id, target=target, source="test-user")
        after = ledger_rows(self.service.ledger)
        self.assertEqual(before["goal_authorizations"] + 1, after["goal_authorizations"])
        self.assertEqual(before["plan_activations"] + 1, after["plan_activations"])
        self.assertEqual(revised.definition_digest, authorized.authorization.goal_contract_digest)
        self.assertEqual(target.plan_revision_id, self.active_plan_revision_id())


if __name__ == "__main__":
    unittest.main()
