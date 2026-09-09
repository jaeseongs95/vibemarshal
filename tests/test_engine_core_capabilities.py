from __future__ import annotations

import contextlib
import io
import json
import pickle
import sqlite3

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.capabilities import (
    CoreActionAuthority,
    CoreCapabilityError,
    EffectCheckpointCapability,
    GoalAuthorizationCapability,
    role_execution_scope,
)
from flowmarshal.engine.cli import main
from flowmarshal.engine.console_host import TrustedConsoleHost
from flowmarshal.engine.domain import GoalOperatingPolicy, new_id, utc_now
from flowmarshal.engine.planning import ExpandedPlanEvaluation, plan_review_evidence_catalog
from flowmarshal.engine.roles import CodexStructuredRoleRunner, make_role_request
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService
from tests.test_engine_ledger_service import EngineServiceFixture, clean_review
from tests.test_engine_roles import ImmediateRoleRuntime


class InteractiveInput(io.StringIO):
    def isatty(self):
        return True


class EngineCoreCapabilityTests(EngineServiceFixture):
    def setUp(self):
        super().setUp()
        self.authority = CoreActionAuthority()
        # 권한 부정 검증은 자동 승인하는 합성 fixture subclass를 사용하지 않는다.
        self.service = EngineService(self.ledger, action_authority=self.authority)

    def goal_capability(self, *, authority=None, policy=None, project_id=None):
        target = self.service.goal_authorization_target(
            project_id=self.project_id,
            operating_policy=policy,
        )
        if project_id is not None:
            target = target.model_copy(update={"project_id": project_id})
        return (authority or self.authority).issue_goal_authorization(
            ledger_path=self.ledger.path,
            target=target,
        )

    def authorization_count(self):
        with self.ledger.read() as connection:
            return connection.execute("SELECT count(*) FROM goal_authorizations").fetchone()[0]

    def activate(self):
        self.service.authorize_goal(project_id=self.project_id, source="user", capability=self.goal_capability())
        self.service.activate_authorized_plan(plan_revision_id=self.plan.plan_revision_id)

    def test_source_permission_strings_and_constructed_handles_cannot_authorize(self):
        for forged in (
            None,
            "cli-user",
            "danger-full-access",
            {"action": "goal.authorize"},
            GoalAuthorizationCapability(),
            EffectCheckpointCapability(),
        ):
            with self.subTest(forged=repr(forged)), self.assertRaises(CoreCapabilityError):
                self.service.authorize_goal(project_id=self.project_id, source="cli-user", capability=forged)
        self.assertEqual(0, self.authorization_count())
        self.service.authorize_goal(project_id=self.project_id, source="user", capability=self.goal_capability())
        self.assertEqual(1, self.authorization_count())

    def test_capability_is_bound_to_authority_project_and_current_policy(self):
        cap = self.goal_capability()

        class ForgedCapability(GoalAuthorizationCapability):
            def __hash__(self):
                return hash(cap)

            def __eq__(self, other):
                return True

        with self.assertRaises(CoreCapabilityError):
            self.service.authorize_goal(project_id=self.project_id, source="user", capability=ForgedCapability())
        for invalid in (self.goal_capability(authority=CoreActionAuthority()),
                        self.goal_capability(project_id="project_elsewhere")):
            with self.assertRaises(CoreCapabilityError):
                self.service.authorize_goal(project_id=self.project_id, source="user", capability=invalid)
        with self.assertRaises(CoreCapabilityError):
            self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap,
                                        operating_policy=GoalOperatingPolicy(max_provider_calls=3))
        with self.assertRaises(TypeError):
            pickle.dumps(cap)
        self.assertEqual(0, self.authorization_count())

    def test_changed_project_root_invalidates_issued_capability(self):
        cap = self.goal_capability()
        with self.ledger.transaction() as tx:
            tx.connection.execute("UPDATE projects SET root=? WHERE id=?", (str(self.base), self.project_id))
        with self.assertRaises(CoreCapabilityError):
            self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)
        self.assertEqual(0, self.authorization_count())

    def test_successful_approval_consumes_handle_and_cannot_reset_operating_limits(self):
        cap = self.goal_capability()
        original = self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)
        with self.assertRaises(CoreCapabilityError):
            self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)
        self.assertEqual(1, self.authorization_count())
        with self.ledger.read() as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM goal_authorizations").fetchone()[0])
        self.assertEqual(original.absolute_deadline_at.isoformat().replace("+00:00", "Z"), payload["absolute_deadline_at"])

    def test_changed_goal_invalidates_issued_capability(self):
        from flowmarshal.engine.domain import new_id
        cap = self.goal_capability()
        definition = self.goal.definition.model_copy(update={"observable_outcome": "새 승인 대상"})
        revised = self.goal.model_copy(update={
            "goal_revision_id": new_id("goal_revision"), "revision_no": 2,
            "supersedes_goal_revision_id": self.goal.goal_revision_id,
            "definition": definition, "definition_digest": definition.definition_digest,
        })
        self.service.register_goal(revised)
        with self.assertRaises(CoreCapabilityError):
            self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)

    def test_same_os_raw_sqlite_remains_a_documented_unisolated_boundary(self):
        # C4 전체 PASS 근거가 아니다. OS/broker 격리 없이는 API guard를 우회한다는 실제 재현이다.
        with role_execution_scope("worker"), contextlib.closing(sqlite3.connect(self.ledger.path)) as raw_connection:
            raw_connection.execute("UPDATE projects SET run_state='recovery_required' WHERE id=?", (self.project_id,))
            raw_connection.commit()
        with self.ledger.read() as connection:
            self.assertEqual("recovery_required", connection.execute(
                "SELECT run_state FROM projects WHERE id=?", (self.project_id,)).fetchone()[0])

    def test_nested_worker_validator_context_cannot_mint_or_use_core_handles(self):
        cap = self.goal_capability()
        for role in ("worker", "validator"):
            with self.subTest(role=role), role_execution_scope(role):
                with role_execution_scope("host"):
                    with self.assertRaises(CoreCapabilityError):
                        CoreActionAuthority()
                    with self.assertRaises(CoreCapabilityError):
                        self.goal_capability()
                    with self.assertRaises(CoreCapabilityError):
                        self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)
                    with self.assertRaises(CoreCapabilityError):
                        with self.ledger.transaction():
                            self.fail("role acquired writable transaction")
                    with self.assertRaises(CoreCapabilityError):
                        self.ledger._connect()
                    with self.assertRaises(CoreCapabilityError):
                        with self.ledger.read():
                            self.fail("role acquired a Core DB read handle")
                    with self.assertRaises(CoreCapabilityError):
                        self.ledger._connect(readonly=True)
        self.service.authorize_goal(project_id=self.project_id, source="user", capability=cap)

    def test_role_read_attach_escalation_is_denied_before_connection_is_exposed(self):
        for role in ("worker", "validator"):
            with self.subTest(role=role), role_execution_scope(role):
                reached_connection = False
                with self.assertRaises(CoreCapabilityError):
                    with self.ledger.read() as connection:
                        reached_connection = True
                        connection.execute("PRAGMA query_only=OFF")
                        connection.execute("ATTACH DATABASE ? AS writable_core", (str(self.ledger.path),))
                        connection.execute("UPDATE writable_core.projects SET run_state='recovery_required'")
                        connection.commit()
                self.assertFalse(reached_connection)
        with self.ledger.read() as connection:
            self.assertEqual("idle", connection.execute(
                "SELECT run_state FROM projects WHERE id=?", (self.project_id,)).fetchone()[0])
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE projects SET run_state='recovery_required'")

    def test_cli_source_cannot_create_authority_and_trusted_console_host_can_confirm(self):
        argv = ["--db", str(self.ledger.path), "--artifacts", str(self.ledger.artifact_root),
                "authorize", "--project-id", self.project_id, "--source", "cli-user"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, main(argv))
        self.assertIn("CORE_CAPABILITY_REQUIRED", output.getvalue())
        self.assertEqual(0, self.authorization_count())
        target = self.service.goal_authorization_target(project_id=self.project_id)

        host = TrustedConsoleHost(
            input_stream=InteractiveInput(target.target_digest + "\n"),
            approval_stream=io.StringIO(),
        )
        with role_execution_scope("worker"), self.assertRaises(CoreCapabilityError):
            host.run(argv)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, host.run(argv))
        self.assertEqual(1, self.authorization_count())

    def test_trusted_console_decline_and_noninteractive_input_do_not_authorize(self):
        argv = ["--db", str(self.ledger.path), "--artifacts", str(self.ledger.artifact_root),
                "goal", "authorize", "--project-id", self.project_id, "--source", "shown-source"]
        target = self.service.goal_authorization_target(project_id=self.project_id)
        approval_output = io.StringIO()
        noninteractive = TrustedConsoleHost(
            input_stream=io.StringIO(target.target_digest + "\n"),
            approval_stream=approval_output,
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, noninteractive.run(argv))
        self.assertEqual("AUTHORIZATION_CONFIRMATION_REQUIRED", json.loads(output.getvalue())["error_code"])
        shown = approval_output.getvalue()
        for expected in (
            self.project_id,
            json.dumps(str(self.root.resolve()), ensure_ascii=False)[1:-1],
            self.goal.goal_id,
            self.goal.goal_revision_id,
            self.goal.definition_digest,
            self.plan.plan_id,
            self.plan.plan_revision_id,
            self.plan.definition_digest,
            self.plan.activation_digest,
            target.target_digest,
            "shown-source",
            '"effect_policy"',
            '"operating_policy"',
        ):
            self.assertIn(expected, shown)

        declined = TrustedConsoleHost(
            input_stream=InteractiveInput("아니요\n"),
            approval_stream=io.StringIO(),
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(1, declined.run(argv))
        self.assertEqual("AUTHORIZATION_DECLINED", json.loads(output.getvalue())["error_code"])
        self.assertEqual(0, self.authorization_count())

    def test_application_authority_binds_selected_plan_and_burns_stale_confirmation(self):
        application = EngineApplication(EngineService(self.ledger))
        with role_execution_scope("worker"), self.assertRaises(CoreCapabilityError):
            ApplicationAuthority(application)
        self.assertIsNone(application.service._action_authority)

        authority = ApplicationAuthority(application)
        target = authority.authorization_target(self.project_id)
        self.assertEqual(self.plan.plan_id, target.plan_id)
        self.assertEqual(self.plan.plan_revision_id, target.plan_revision_id)
        self.assertEqual(self.plan.revision_no, target.plan_revision_no)
        self.assertEqual(self.plan.definition_digest, target.plan_definition_digest)
        self.assertEqual(self.plan.activation_digest, target.plan_activation_digest)

        task_ids = {task.task_id: new_id("task") for task in self.plan.definition.tasks}
        replacement_definition = self.plan.definition.model_copy(update={
            "tasks": tuple(
                task.model_copy(update={"task_id": task_ids[task.task_id]})
                for task in self.plan.definition.tasks
            ),
            "dependencies": tuple(
                dependency.model_copy(update={
                    "producer_task_id": task_ids[dependency.producer_task_id],
                    "consumer_task_id": task_ids[dependency.consumer_task_id],
                })
                for dependency in self.plan.definition.dependencies
            ),
            "goal_coverage": tuple(
                coverage.model_copy(update={
                    "task_ids": tuple(task_ids[item] for item in coverage.task_ids),
                })
                for coverage in self.plan.definition.goal_coverage
            ),
        })
        replacement = self.plan.model_copy(update={
            "plan_id": new_id("plan"),
            "plan_revision_id": new_id("plan_revision"),
            "revision_no": 1,
            "supersedes_plan_revision_id": None,
            "created_at": utc_now(),
            "definition": replacement_definition,
            "definition_digest": replacement_definition.definition_digest,
        })
        review = clean_review(
            replacement.activation_digest,
            role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(
                replacement, self.goal, self.state, self.map
            ),
        )
        self.service.register_plan_evaluation(ExpandedPlanEvaluation(
            plan=replacement,
            semantic_submissions=(review,),
            decision=self.decision.model_copy(update={
                "candidate_digest": replacement.activation_digest,
            }),
        ))
        with self.ledger.transaction() as tx:
            tx.history(
                self.project_id,
                "planning.search_recorded",
                "planning_search",
                new_id("planning_search"),
                {"outcome": {"selected_activation_digest": replacement.activation_digest}},
            )

        with self.assertRaises(CoreCapabilityError):
            authority.authorize(self.project_id, target=target, source="user")
        with self.assertRaises(CoreCapabilityError):
            authority.authorize(self.project_id, target=target, source="user")
        self.assertEqual(0, self.authorization_count())

    def test_trusted_console_stale_target_is_rejected_without_authorization(self):
        argv = ["--db", str(self.ledger.path), "--artifacts", str(self.ledger.artifact_root),
                "authorize", "--project-id", self.project_id]
        target = self.service.goal_authorization_target(project_id=self.project_id)
        case = self

        class StaleTargetHost(TrustedConsoleHost):
            def _show_target(self, shown_target, *, audit_source):
                super()._show_target(shown_target, audit_source=audit_source)
                with case.ledger.transaction() as tx:
                    tx.connection.execute(
                        "UPDATE projects SET root=? WHERE id=?",
                        (str(case.base.resolve()), case.project_id),
                    )

        host = StaleTargetHost(
            input_stream=InteractiveInput(target.target_digest + "\n"),
            approval_stream=io.StringIO(),
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(2, host.run(argv))
        self.assertIn("CORE_CAPABILITY_DENIED", output.getvalue())
        self.assertEqual(0, self.authorization_count())

    def test_worker_dispatch_callback_cannot_write_core_or_forge_authorization(self):
        self.activate()
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        case = self
        attempted = []

        class AttackingRuntime(FakeCodexRuntime):
            def create_thread(self, **kwargs):
                for callback in (
                    lambda: case.service.authorize_goal(project_id=case.project_id, source="cli-user"),
                    lambda: case.ledger._connect(),
                    lambda: CoreActionAuthority(),
                ):
                    with case.assertRaises(CoreCapabilityError):
                        callback()
                    attempted.append(True)
                return super().create_thread(**kwargs)

        EngineDispatcher(self.service, AttackingRuntime(self.inventory)).run_once(project_id=self.project_id)
        self.assertEqual([True, True, True], attempted)
        self.assertEqual(1, self.authorization_count())

    def test_structured_validator_callback_cannot_use_even_leaked_capability(self):
        cap = self.goal_capability()
        case = self
        attempted = []

        class AttackingRuntime(ImmediateRoleRuntime):
            def create_thread(self, **kwargs):
                with case.assertRaises(CoreCapabilityError):
                    case.service.authorize_goal(project_id=case.project_id, source="user", capability=cap)
                attempted.append(True)
                return super().create_thread(**kwargs)

        runtime = AttackingRuntime(['{}'])
        request = make_role_request(
            inventory=runtime.inventory, role="semantic_validator", instructions="검사 결과를 제출한다.",
            payload={"request": "검토"}, output_schema={"type": "object", "properties": {}},
            model="available", effort="low", inventory_digest=runtime.inventory.inventory_digest,
            cwd=str(self.root),
        )
        CodexStructuredRoleRunner(runtime).run(request)
        self.assertEqual([True], attempted)
        self.assertEqual(0, self.authorization_count())

    def test_effect_checkpoint_requires_exact_effect_and_execution_binding(self):
        self.activate()
        spec = self.spec()
        self.service.materialize_execution_spec(spec, inventory=self.inventory)
        identity = {"provider": "synthetic", "system": "release", "target": "test",
                    "account": "fixture", "operation": "publish", "scope": "one",
                    "idempotency_key": "test", "checkpoint_policy": "before_irreversible"}
        # 이 임시 DB 행은 checkpoint 입력만 격리하며 실행/완료 판정을 만들지 않는다.
        checkpoint_task = "task_checkpoint_fixture"
        with self.ledger.transaction() as tx:
            row = tx.one("SELECT * FROM task_contracts WHERE id=?", (self.task.task_id,))
            contract = json.loads(row["payload_json"])
            contract.update(approval_class="execution_checkpoint", expected_effects=[{
                "effect_id": "publish", "external": True, "reversible": False,
                "identity_version": "2.0", "identity": identity,
            }])
            tx.connection.execute(
                "INSERT INTO task_contracts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (checkpoint_task, self.project_id, self.plan.plan_revision_id, "checkpoint_fixture", 42,
                 sha256_digest(contract), json.dumps(contract), "materialized", tx.now, tx.now))
            original = dict(tx.one("SELECT * FROM execution_spec_revisions WHERE task_id=?", (self.task.task_id,)))
            original.update(id="spec_checkpoint_fixture", task_id=checkpoint_task,
                            definition_digest=sha256_digest({"fixture": "checkpoint"}))
            tx.connection.execute(
                "INSERT INTO execution_spec_revisions (" + ",".join(original) + ") VALUES (" +
                ",".join("?" for _ in original) + ")", tuple(original.values()))
        spec_digest = original["definition_digest"]
        args = {"task_id": checkpoint_task, "effect_id": "publish",
                "execution_spec_digest": spec_digest, "approved_by": "cli-user"}
        target = self.service.effect_checkpoint_target(
            task_id=checkpoint_task,
            effect_id="publish",
            execution_spec_digest=spec_digest,
        )
        bad_cap = self.authority.issue_effect_checkpoint(
            ledger_path=self.ledger.path,
            target=target.model_copy(update={"execution_spec_digest": "old"}),
        )
        for invalid in (None, "user", GoalAuthorizationCapability(), bad_cap):
            with self.assertRaises(CoreCapabilityError):
                self.service.record_effect_checkpoint(**args, capability=invalid)
        cap = self.authority.issue_effect_checkpoint(
            ledger_path=self.ledger.path,
            target=target,
        )
        with role_execution_scope("validator"), self.assertRaises(CoreCapabilityError):
            self.service.record_effect_checkpoint(**args, capability=cap)
        with self.ledger.read() as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM effect_checkpoints").fetchone()[0])
        self.service.record_effect_checkpoint(**args, capability=cap)
        with self.assertRaises(CoreCapabilityError):
            self.service.record_effect_checkpoint(**args, capability=cap)
        with self.ledger.read() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM effect_checkpoints").fetchone()[0])
