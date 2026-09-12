"""설치 console과 동일 parser/host 경로의 승인·거부 쌍을 실제 Core DB로 검사한다."""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from flowmarshal.engine import cli, console_host
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.capabilities import (
    CoreActionAuthority,
    CoreCapabilityError,
    EffectCheckpointTarget,
    GoalAuthorizationCapability,
    role_execution_scope,
)
from flowmarshal.engine.console_host import TrustedConsoleHost
from flowmarshal.engine.domain import new_id, utc_now
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.service import EngineService
from tests.test_engine_ledger_service import EngineServiceFixture


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class ConsoleAuthorityIntegrationTests(EngineServiceFixture):
    def setUp(self):
        super().setUp()
        # 준비용 fixture만 재사용하며 승인 경로에는 production service를 사용한다.
        self.service = EngineService(self.ledger)
        self.argv = ["--db", str(self.ledger.path), "--artifacts", str(self.ledger.artifact_root),
                     "authorize", "--project-id", self.project_id, "--source", "forged-source"]

    def rows(self):
        with self.ledger.read() as connection:
            return {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("goal_authorizations", "plan_activations", "effect_checkpoints", "history_events")}

    def host_run(self, stream, argv=None):
        shown, output = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output):
            code = TrustedConsoleHost(input_stream=stream, approval_stream=shown).run(argv or self.argv)
        return code, json.loads(output.getvalue()), shown.getvalue()

    def test_confirmation_matrix_keeps_all_authority_rows_unchanged(self):
        target = self.service.goal_authorization_target(project_id=self.project_id)
        before = self.rows()
        cases = {
            "decline": TerminalInput("아니요\n"),
            "eof": TerminalInput(""),
            "partial": TerminalInput(target.target_digest[:12] + "\n"),
            "wrong_digest": TerminalInput("0" * 64 + "\n"),
            "leading_space": TerminalInput(" " + target.target_digest + "\n"),
            "trailing_space": TerminalInput(target.target_digest + " \n"),
            "pipe_exact_digest": io.StringIO(target.target_digest + "\n"),
        }
        for case, stream in cases.items():
            with self.subTest(case=case):
                code, payload, _ = self.host_run(stream)
                self.assertEqual(2 if case == "pipe_exact_digest" else 1, code)
                self.assertIn(payload["error_code"], ("AUTHORIZATION_DECLINED", "AUTHORIZATION_CONFIRMATION_REQUIRED"))
                self.assertEqual(before, self.rows())

    def test_exact_confirmation_activates_displayed_plan_without_injected_capability(self):
        target = self.service.goal_authorization_target(project_id=self.project_id)
        before = self.rows()
        code, payload, shown = self.host_run(TerminalInput(target.target_digest + "\r\n"))
        self.assertEqual(0, code)
        self.assertEqual(self.project_id, payload["project_id"])
        displayed = json.loads(shown[shown.index("{"):shown.rindex("}") + 1])
        for field in ("goal_revision_id", "goal_contract_digest", "profile_definition_digest",
                      "project_root", "plan_revision_id", "plan_definition_digest", "plan_activation_digest",
                      "effect_policy", "operating_policy"):
            self.assertEqual(target.model_dump(mode="json")[field], displayed[field])
        self.assertEqual(target.target_digest, displayed["target_digest"])
        self.assertEqual(before["goal_authorizations"] + 1, self.rows()["goal_authorizations"])
        self.assertEqual(before["plan_activations"] + 1, self.rows()["plan_activations"])
        with self.ledger.read() as connection:
            active = connection.execute("SELECT active_plan_revision_id FROM projects WHERE id=?",
                                        (self.project_id,)).fetchone()[0]
        self.assertEqual(target.plan_revision_id, active)
        self.assertEqual(["argv"], list(inspect.signature(console_host.main).parameters))
        self.assertEqual(["argv"], list(inspect.signature(cli.main).parameters))

    def test_internal_cli_source_is_not_authority(self):
        before = self.rows()
        for source in ("cli-user", "danger-full-access", "TrustedConsoleHost", "ApplicationAuthority"):
            with self.subTest(source=source), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(2, cli.main([*self.argv[:-1], source]))
                self.assertEqual("CORE_CAPABILITY_REQUIRED", json.loads(output.getvalue())["error_code"])
                self.assertEqual(before, self.rows())

    def test_application_wrong_target_type_project_and_policy_burn_attempt_without_writes(self):
        target = self.service.goal_authorization_target(project_id=self.project_id)
        wrong_type = EffectCheckpointTarget(project_id=self.project_id, task_id=self.task.task_id,
            effect_id="synthetic", execution_spec_digest=self.plan.definition_digest, effect_identity={})
        for changed in (wrong_type, target.model_copy(update={"project_id": "other"}),
                        target.model_copy(update={"project_root": str(self.base)}),
                        target.model_copy(update={"operating_policy": target.operating_policy.model_copy(
                            update={"max_provider_calls": 3})})):
            with self.subTest(target_type=type(changed).__name__):
                authority = ApplicationAuthority(EngineApplication(EngineService(self.ledger)))
                before = self.rows()
                with self.assertRaises(CoreCapabilityError):
                    authority.authorize(self.project_id, target=changed, source="user")
                self.assertEqual(before, self.rows())
                with self.assertRaises(CoreCapabilityError):
                    authority.authorize(self.project_id, target=target, source="user")
                self.assertEqual(before, self.rows())

    def test_core_wrong_ledger_and_constructed_handle_have_no_writes(self):
        authority = CoreActionAuthority()
        service = EngineService(self.ledger, action_authority=authority)
        target = service.goal_authorization_target(project_id=self.project_id)
        wrong_ledger = authority.issue_goal_authorization(ledger_path=Path(self.base) / "other.sqlite3", target=target)
        before = self.rows()
        for cap in (wrong_ledger, GoalAuthorizationCapability()):
            with self.assertRaises(CoreCapabilityError):
                service.authorize_goal(project_id=self.project_id, source="user", capability=cap,
                                       authorization_target=target)
            self.assertEqual(before, self.rows())

    def test_application_success_then_replay_keeps_authority_rows_unchanged(self):
        authority = ApplicationAuthority(EngineApplication(self.service))
        target = authority.authorization_target(self.project_id)
        authority.authorize(self.project_id, target=target, source="user")
        before = self.rows()
        with self.assertRaises(CoreCapabilityError):
            authority.authorize(self.project_id, target=target, source="user")
        self.assertEqual(before, self.rows())

    def test_host_and_authority_reentry_are_denied_for_worker_and_validator(self):
        application = EngineApplication(self.service)
        authority = ApplicationAuthority(application)
        target = authority.authorization_target(self.project_id)
        before = self.rows()
        for role in ("worker", "validator"):
            with self.subTest(role=role), role_execution_scope(role):
                actions = (
                    lambda: TrustedConsoleHost().run(self.argv),
                    lambda: console_host.main(self.argv),
                    lambda: ApplicationAuthority(EngineApplication(EngineService(self.ledger))),
                    lambda: authority.authorize(self.project_id, target=target, source="user"),
                    lambda: application.authorize(self.project_id, source="user"),
                    lambda: self.service.authorize_goal(project_id=self.project_id, source="user"),
                    lambda: self.ledger._connect(),
                    lambda: self.ledger._connect(readonly=True),
                )
                for action in actions:
                    with self.assertRaises(CoreCapabilityError):
                        action()
            self.assertEqual(before, self.rows())

    def assert_stale_observation_has_no_approval_writes(self, kind, *, active=False):
        if active:
            target = self.service.goal_authorization_target(project_id=self.project_id)
            self.assertEqual(0, self.host_run(TerminalInput(target.target_digest + "\n"))[0])
        target = self.service.goal_authorization_target(project_id=self.project_id)
        case, observations, statements = self, {}, []
        connect = SQLiteEngineLedger._connect

        def traced_connect(ledger, **kwargs):
            connection = connect(ledger, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        class NewObservationHost(TrustedConsoleHost):
            def _show_target(self, shown, *, audit_source):
                super()._show_target(shown, audit_source=audit_source)
                if kind == "state":
                    case.service.record_state_snapshot(case.state.model_copy(update={
                        "snapshot_id": new_id("state"), "version": case.state.version + 1,
                        "observed_at": utc_now(),
                    }))
                else:
                    case.service.record_project_map(case.map.model_copy(update={
                        "project_map_revision_id": new_id("map"), "revision_no": case.map.revision_no + 1,
                        "created_at": utc_now(),
                    }))
                observations["after_new_observation"] = case.rows()
                statements.clear()

        host = NewObservationHost(input_stream=TerminalInput(target.target_digest + "\n"),
                                  approval_stream=io.StringIO())
        with patch.object(SQLiteEngineLedger, "_connect", traced_connect), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, host.run(self.argv))
        self.assertEqual(observations["after_new_observation"], self.rows())
        self.assertFalse(any("INSERT INTO GOAL_AUTHORIZATIONS" in sql.upper() for sql in statements))
        self.assertFalse(any("INSERT INTO PLAN_ACTIVATIONS" in sql.upper() for sql in statements))

    def test_new_state_after_display_is_rejected_before_approval_insert(self):
        self.assert_stale_observation_has_no_approval_writes("state")

    def test_new_project_map_after_display_is_rejected_before_approval_insert(self):
        self.assert_stale_observation_has_no_approval_writes("map")

    def test_active_plan_reauthorization_rejects_stale_state_without_writes(self):
        self.assert_stale_observation_has_no_approval_writes("state", active=True)

    def test_active_plan_reauthorization_rejects_stale_map_without_writes(self):
        self.assert_stale_observation_has_no_approval_writes("map", active=True)

    def test_activation_write_failure_rolls_back_approval_and_burns_capability(self):
        authority = CoreActionAuthority()
        service = EngineService(self.ledger, action_authority=authority)
        target = service.goal_authorization_target(project_id=self.project_id)
        cap = authority.issue_goal_authorization(ledger_path=self.ledger.path, target=target)
        with self.ledger.transaction() as tx:
            tx.connection.execute("CREATE TRIGGER synthetic_activation_failure BEFORE INSERT ON plan_activations "
                                  "BEGIN SELECT RAISE(ABORT, 'synthetic activation failure'); END")
        before = self.rows()
        args = dict(project_id=self.project_id, source="user", authorization_target=target,
                    expected_plan_revision_id=target.plan_revision_id)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic activation failure"):
            service.authorize_goal_and_activate_plan(**args, capability=cap)
        self.assertEqual(before, self.rows())
        with self.ledger.read() as connection:
            self.assertIsNone(connection.execute("SELECT active_plan_revision_id FROM projects WHERE id=?",
                                               (self.project_id,)).fetchone()[0])
        with self.ledger.transaction() as tx:
            tx.connection.execute("DROP TRIGGER synthetic_activation_failure")
        with self.assertRaises(CoreCapabilityError):
            service.authorize_goal_and_activate_plan(**args, capability=cap)
        self.assertEqual(before, self.rows())
        fresh = authority.issue_goal_authorization(ledger_path=self.ledger.path, target=target)
        service.authorize_goal_and_activate_plan(**args, capability=fresh)
        self.assertEqual(before["goal_authorizations"] + 1, self.rows()["goal_authorizations"])
        self.assertEqual(before["plan_activations"] + 1, self.rows()["plan_activations"])
