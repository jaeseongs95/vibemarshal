"""G1b R4b: 재계획 후보가 있을 때 TrustedConsoleHost 재승인 target이 실제 활성화할 후보를 결속한다.

제품 `EngineApplication.prepare`로 만든 프로젝트(fm08 facade harness, scripted runner)에서
설치 console과 같은 `TrustedConsoleHost` 경로로 authorize한다. 표시 target 5필드가 후보와 같은지,
승인과 후보 활성화가 한 transaction에서 함께 commit·rollback되는지, 좁은 정책·typed identity 없는
effect·STALE·cancelled·표시 뒤 head 변경이 원장 사본 그대로 거절되는지, paused·두 console 경쟁·다음
run_once·후보 없는 기존 경로를 본다. 결정적 테스트이며 live qualification이 아니다.
"""
from __future__ import annotations

import contextlib
import io
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import tests.test_engine_g1_replan_candidate as g1
import tests.test_engine_g1b_recovery_path as g1b
from flowmarshal.engine import cli
from flowmarshal.engine.application import EngineApplicationError
from flowmarshal.engine.capabilities import CoreActionAuthority, CoreCapabilityError
from flowmarshal.engine.console_host import TrustedConsoleHost
from flowmarshal.engine.domain import GoalOperatingPolicy, RunOnceAction
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.operations import CoreOperations
from flowmarshal.engine.recovery_planning import RECOVERY_PLAN_REVIEWER_ROLE
from flowmarshal.engine.service import EngineService, EngineServiceError, GoalAuthorizationRequired


WIDE = GoalOperatingPolicy(max_same_failure_replans=3)
PLAN_FIELDS = ("plan_id", "plan_revision_id", "plan_revision_no", "plan_definition_digest",
               "plan_activation_digest")
_STATEMENT = "app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다."


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class _ConsoleHarness(g1b._ReplanHarness):
    """I1 harness(fm08 facade + 실제 RecoveryPlanProvider)에 제품 console authorize를 더한다."""

    def _argv(self, policy: GoalOperatingPolicy | None) -> list[str]:
        argv = ["--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                "authorize", "--project-id", self.project_id, "--source", "r4b-console-user"]
        if policy is not None:
            path = Path(self.temp.name) / f"policy-{policy.max_same_failure_replans}.json"
            path.write_text(policy.model_dump_json(), encoding="utf-8")
            argv += ["--policy-file", str(path)]
        return argv

    def _host(self, policy, *, digest=None, host_cls=TrustedConsoleHost, statements=None):
        """TrustedConsoleHost로 표시·확인한다. digest가 없으면 표시 전 Core target의 digest를 입력한다."""

        if digest is None:
            digest = self.service.goal_authorization_target(
                project_id=self.project_id, operating_policy=policy,
            ).target_digest
        shown, output = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            if statements is not None:
                connect = SQLiteEngineLedger._connect

                def traced(ledger, **kwargs):
                    connection = connect(ledger, **kwargs)
                    connection.set_trace_callback(statements.append)
                    return connection

                stack.enter_context(patch.object(SQLiteEngineLedger, "_connect", traced))
            code = host_cls(input_stream=TerminalInput(digest + "\n"), approval_stream=shown).run(
                self._argv(policy),
            )
        text = shown.getvalue()
        displayed = json.loads(text[text.index("{"):text.rindex("}") + 1]) if "{" in text else None
        return code, json.loads(output.getvalue()), displayed

    def _plan_fields(self, plan_revision_id: str) -> dict:
        row = self._rows(
            "SELECT plan_id,id,revision_no,definition_digest,activation_digest FROM plan_revisions WHERE id=?",
            plan_revision_id,
        )[0]
        return dict(zip(PLAN_FIELDS, row))

    @staticmethod
    def _displayed_fields(displayed: dict) -> dict:
        return {field: displayed[field] for field in PLAN_FIELDS}

    def _activation(self, plan_revision_id: str) -> list[tuple]:
        return self._rows(
            "SELECT id,authorization_id,source FROM plan_activations WHERE plan_revision_id=?", plan_revision_id,
        )

    def _latest_authorization(self) -> str:
        return self._rows(
            "SELECT id FROM goal_authorizations WHERE project_id=? ORDER BY revision_no DESC LIMIT 1",
            self.project_id,
        )[0][0]

    def _gar_candidate(self, **recovery) -> tuple[str, str]:
        """정책 확장 재계획 후보가 GOAL_AUTHORIZATION_REQUIRED로 멈춘 상태를 만든다."""

        self._fail_contract()
        rev1 = self._active_plan_id()
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT,
                           **(recovery or {"max_same_failure_replans": 3}))
        blocked = self._until_blocked()
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        return rev1, self._plans()[-1][0]

    def _base_currency(self, plan_revision_id: str) -> tuple[int, int]:
        """후보가 결속한 (base StateSnapshot, ProjectMap)의 is_current."""

        plan = self._plan(plan_revision_id)
        state = self._rows("SELECT is_current FROM state_snapshots WHERE snapshot_digest=?",
                           plan.definition.base_state_snapshot_digest)[0][0]
        project_map = self._rows("SELECT is_current FROM project_map_revisions WHERE revision_digest=?",
                                 plan.definition.project_map_digest)[0][0]
        return state, project_map

    def _assert_capability_burned(self, policy, error, pattern) -> None:
        """Core capability는 거절에서도 첫 시도에 소모되고 원장 사본은 그대로다."""

        authority = CoreActionAuthority()
        service = EngineService(
            SQLiteEngineLedger(self.service.ledger.path, artifact_root=self.service.ledger.artifact_root),
            action_authority=authority,
        )
        target = service.goal_authorization_target(project_id=self.project_id, operating_policy=policy)
        capability = authority.issue_goal_authorization(ledger_path=service.ledger.path, target=target)
        arguments = dict(project_id=self.project_id, source="r4b-core", operating_policy=policy,
                         authorization_target=target, expected_plan_revision_id=target.plan_revision_id)
        before = self._ledger()
        with self.assertRaisesRegex(error, pattern):
            service.authorize_goal_and_activate_plan(**arguments, capability=capability)
        with self.assertRaises(CoreCapabilityError):
            service.authorize_goal_and_activate_plan(**arguments, capability=capability)
        self.assertEqual(before, self._ledger())

    def _assert_rejected_by_console(self, policy, code: str, *, candidate: str, before_insert: bool) -> str:
        before = self._ledger()
        statements: list[str] = []
        exit_code, payload, displayed = self._host(policy, statements=statements)
        self.assertEqual((2, code), (exit_code, payload.get("error_code")), payload)
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        self.assertEqual(before, self._ledger())
        if before_insert:
            upper = [sql.upper() for sql in statements]
            self.assertFalse(any("INSERT INTO GOAL_AUTHORIZATIONS" in sql for sql in upper))
            self.assertFalse(any("INSERT INTO PLAN_ACTIVATIONS" in sql for sql in upper))
        return payload["message"]


class ReauthorizationTargetTests(_ConsoleHarness, unittest.TestCase):
    # --- AC11(a)(b): target 5필드와 같은 transaction의 후보 활성화 ----------------------

    def test_console_target_binds_the_automatic_candidate_and_activates_it_with_the_approval(self) -> None:
        rev1, candidate = self._gar_candidate()
        # run_once가 GAR를 보고한 바로 그 consumed replanning 결과의 후보다.
        consumed = [json.loads(row[0])["plan"]["plan_revision_id"] for row in self._rows(
            "SELECT result_json FROM runtime_jobs WHERE project_id=? AND kind='replanning' AND status='consumed'",
            self.project_id,
        )]
        self.assertEqual([candidate], consumed)
        self.assertEqual(
            "GOAL_AUTHORIZATION_REQUIRED",
            self.application.status(self.project_id)["recovery"]["next_action"]["blocker_code"],
        )

        selections: list[tuple[str, bool]] = []
        real = EngineService.__dict__.get("_authorization_plan")

        def recording(service, connection, project_id):
            result = real(service, connection, project_id)
            selections.append((result[0].plan_revision_id, result[1]))
            return result

        digest = self.service.goal_authorization_target(
            project_id=self.project_id, operating_policy=WIDE,
        ).target_digest
        before = self._ledger()
        with patch.object(EngineService, "_authorization_plan", recording, create=True):
            code, payload, displayed = self._host(WIDE, digest=digest)
        self.assertEqual(0, code, payload)
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        # 표시 preview와 authorize transaction 재계산이 같은 후보를 고르고, insert 뒤에는 다시 고르지 않는다.
        self.assertEqual([(candidate, True), (candidate, True)], selections)

        after = self._ledger()
        self.assertEqual(len(before["authorizations"]) + 1, len(after["authorizations"]))
        self.assertEqual(len(before["activations"]) + 1, len(after["activations"]))
        activation = self._activation(candidate)
        self.assertEqual([(payload["activation_id"], self._latest_authorization(), "goal_authorization")],
                         activation)
        self.assertEqual(candidate, self._active_plan_id())
        self.assertEqual({rev1: "superseded", candidate: "active"},
                         {row[0]: row[3] for row in self._plans()})
        self.assertEqual(1, g1.ReplanCandidateTests._events(
            self.service, self.project_id, "plan.activated", candidate,
        ))

    def test_console_target_binds_the_replan_retry_candidate(self) -> None:
        _task_id, _attempt, blocked_id = self._blocked_needs_revision()
        rev1 = self._active_plan_id()
        blocked_before = self._rows(
            "SELECT id,plan_id,revision_no,status,supersedes_id,payload_json FROM plan_revisions WHERE id=?",
            blocked_id,
        )
        decision_before = self._rows(
            "SELECT id,status,payload_json FROM candidate_decisions WHERE artifact_digest=?",
            self._plan(blocked_id).activation_digest,
        )
        self._replan()
        self.runner.responses.setdefault("plan_refiner", []).append({
            "action": "detail_revision",
            "rationale": "finding이 지적한 검사 statement를 실제 파일 재관측으로 고친다.",
            "evidence_refs": ["artifact:plan_contract", "source:goal"],
            "plan": g1b._expansion(_STATEMENT, max_same_failure_replans=3),
            "skeleton": None,
        })
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(g1b._clean_review())
        blocked = self._until_blocked()
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        candidate = self._plans()[-1][0]
        self.assertEqual(
            [(rev1, 1, "active", None), (blocked_id, 2, "draft", rev1), (candidate, 3, "ready", blocked_id)],
            [(row[0], row[2], row[3], row[4]) for row in self._plans()],
        )

        code, payload, displayed = self._host(WIDE)
        self.assertEqual(0, code, payload)
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        self.assertEqual(candidate, self._active_plan_id())
        self.assertEqual([(payload["activation_id"], self._latest_authorization(), "goal_authorization")],
                         self._activation(candidate))
        # 대체된 차단 후보의 행과 decision은 그대로다.
        self.assertEqual(blocked_before, self._rows(
            "SELECT id,plan_id,revision_no,status,supersedes_id,payload_json FROM plan_revisions WHERE id=?",
            blocked_id,
        ))
        self.assertEqual(decision_before, self._rows(
            "SELECT id,status,payload_json FROM candidate_decisions WHERE artifact_digest=?",
            self._plan(blocked_id).activation_digest,
        ))

    def test_second_candidate_after_the_first_replan_activation_is_the_target(self) -> None:
        _rev1, first = self._gar_candidate()
        self.assertEqual(0, self._host(WIDE)[0])
        self.assertEqual(first, self._active_plan_id())

        task_id = self._plan(first).definition.tasks[0].task_id
        attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(attempt, binding, response="Task 계약이 현재 대상과 맞지 않습니다.",
                          error_code="TASK_CONTRACT_INVALID")
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT, max_same_failure_replans=4)
        blocked = self._until_blocked()
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        second = self._plans()[-1][0]
        self.assertEqual((3, first), (self._plan(second).revision_no, self._plan(second).supersedes_plan_revision_id))

        code, payload, displayed = self._host(GoalOperatingPolicy(max_same_failure_replans=4))
        self.assertEqual(0, code, payload)
        self.assertEqual(self._plan_fields(second), self._displayed_fields(displayed))
        self.assertEqual(second, self._active_plan_id())
        self.assertEqual("superseded", {row[0]: row[3] for row in self._plans()}[first])

    def test_a_draft_head_has_no_candidate_and_an_older_ready_revision_is_not_selected(self) -> None:
        _task_id, _attempt, stale_id = self._blocked_stale()
        rev1 = self._active_plan_id()
        self._replan("State가 다시 관측돼 current 입력으로 다시 계획한다.")
        self._queue_replan(review=g1b._finding_review(), statement="app.py 값을 검사한다.")
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code, blocked)
        head = self._plans()[-1]
        self.assertEqual((3, "draft"), (head[2], head[3]))
        self.assertEqual("ready", {row[0]: row[3] for row in self._plans()}[stale_id])
        target = self.service.goal_authorization_target(project_id=self.project_id)
        self.assertEqual(rev1, target.plan_revision_id)

    # --- AC11(c): 표시 뒤 head 후보 변경 ---------------------------------------------------

    def test_target_shown_before_the_candidate_is_registered_is_denied_without_writes(self) -> None:
        self._fail_contract()
        rev1 = self._active_plan_id()
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT, max_same_failure_replans=3)
        for _ in range(80):
            self.application.run_once(self.project_id)
            if self._jobs():
                break
        self.assertTrue(self._jobs())
        shown = self.service.goal_authorization_target(project_id=self.project_id, operating_policy=WIDE)
        self.assertEqual(rev1, shown.plan_revision_id)
        case, observed = self, {}

        class HeadChangesAfterDisplay(TrustedConsoleHost):
            def _show_target(self, target, *, audit_source):
                super()._show_target(target, audit_source=audit_source)
                observed["blocker"] = case._until_blocked().blocker_code
                observed["ledger"] = case._ledger()

        code, payload, displayed = self._host(WIDE, digest=shown.target_digest, host_cls=HeadChangesAfterDisplay)
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", observed["blocker"])
        self.assertEqual(rev1, displayed["plan_revision_id"])
        self.assertEqual((2, "CORE_CAPABILITY_DENIED"), (code, payload.get("error_code")), payload)
        self.assertIn("표시 후 승인 target이 변경됐습니다", payload["message"])
        self.assertEqual(observed["ledger"], self._ledger())

    # --- AC11(c): 원자 거절 --------------------------------------------------------------

    def test_narrow_policy_is_rejected_atomically_with_the_changes(self) -> None:
        _rev1, candidate = self._gar_candidate()
        message = self._assert_rejected_by_console(
            None, "GOAL_AUTHORIZATION_REQUIRED", candidate=candidate, before_insert=False,
        )
        self.assertIn('"boundary":"policy"', message)
        self.assertIn("single_task.recovery.max_same_failure_replans", message)
        self._assert_capability_burned(None, GoalAuthorizationRequired, "^GOAL_AUTHORIZATION_REQUIRED")
        # base가 current인 GAR의 replan 거절 안내는 console의 실제 동작(같은 transaction 활성화)을 말한다.
        guidance = self._assert_rejected("REPLAN_RETRY_REAUTHORIZATION_REQUIRED")
        self.assertIn("같은 transaction에서 승인과 후보 활성화를 함께 기록합니다", guidance)
        self.assertNotIn("다음 run-once", guidance)
        self.assertNotIn("E2E-15", guidance)

    def test_effect_gar_candidate_with_a_current_base_is_rejected_with_the_e2e15_gap(self) -> None:
        effect = "G1b 외부 서비스 배포"
        self.runner.responses["goal_normalizer"][0]["allowed_external_effects"] = [effect]
        self._fail_contract()
        expansion = g1b._expansion(_STATEMENT)
        expansion["tasks"][0]["expected_effects"] = [
            {"effect_id": "g1b-external", "statement": effect, "external": True},
        ]
        self.runner.responses.setdefault("plan_expander", []).append(expansion)
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(g1b._clean_review())
        blocked = self._until_blocked()
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        self.assertIn('"boundary":"effect"', blocked.detail)
        candidate = self._plans()[-1][0]
        # 이 후보의 base State·ProjectMap은 current다. 거절은 STALE 경로가 아니라 effect GAR 판정이다.
        self.assertEqual((1, 1), self._base_currency(candidate))

        for policy in (None, WIDE):
            with self.subTest(policy=policy):
                message = self._assert_rejected_by_console(
                    policy, "GOAL_AUTHORIZATION_REQUIRED", candidate=candidate, before_insert=False,
                )
                self.assertIn('"boundary":"effect"', message)
                for text in ("같은 Goal을 다시 승인해도 풀리지 않습니다", "새 Goal revision",
                             "GOAL_REVISION_ACTIVE_PLAN", "E2E-15"):
                    self.assertIn(text, message)
                self.assertNotIn("cancel", message.lower())
                self.assertNotIn("취소", message)
        self._assert_capability_burned(None, GoalAuthorizationRequired, "E2E-15")
        guidance = self._assert_rejected("REPLAN_RETRY_REAUTHORIZATION_REQUIRED")
        for text in ("같은 Goal을 다시 승인해도 풀리지 않습니다", "새 Goal revision", "E2E-15"):
            self.assertIn(text, guidance)
        self.assertNotIn("cancel", guidance.lower())
        self.assertNotIn("취소", guidance)

    def test_stale_candidate_is_rejected_before_the_approval_insert_and_replan_applies(self) -> None:
        _task_id, _attempt, stale_id = self._blocked_stale()
        self._assert_rejected_by_console(
            None, "PLAN_STATE_SNAPSHOT_STALE", candidate=stale_id, before_insert=True,
        )
        self._assert_capability_burned(None, EngineServiceError, "^PLAN_STATE_SNAPSHOT_STALE")
        self.assertEqual("candidate_state_stale", self._replan("State 재관측 뒤 다시 계획")["basis_kind"])
        self._queue_replan(review=g1b._clean_review(), statement="app.py 값을 current 입력으로 다시 검사한다.")
        self._until_recovered_activation()
        self.assertEqual((3, stale_id), (self._plan(self._active_plan_id()).revision_no,
                                         self._plan(self._active_plan_id()).supersedes_plan_revision_id))

    def test_candidate_that_needs_approval_and_is_stale_is_rejected_as_stale(self) -> None:
        self._fail_contract()
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT, max_same_failure_replans=3)
        real = self.service._ensure_replan_candidate_registered

        def registered_then_reobserved(evaluation):
            created = real(evaluation)
            if created:
                self.service.reobserve_project(self.project_id, force_state_revision=True)
            return created

        self.service._ensure_replan_candidate_registered = registered_then_reobserved
        try:
            blocked = self._until_blocked()
        finally:
            self.service._ensure_replan_candidate_registered = real
        # run_once는 승인 판정을 먼저 보고한다(순서 불변). authorize는 승인 쓰기 전에 STALE로 거절한다.
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        candidate = self._plans()[-1][0]
        for policy in (WIDE, None):
            with self.subTest(policy=policy):
                self._assert_rejected_by_console(
                    policy, "PLAN_STATE_SNAPSHOT_STALE", candidate=candidate, before_insert=True,
                )
        self._assert_capability_burned(WIDE, EngineServiceError, "^PLAN_STATE_SNAPSHOT_STALE")
        result = self._replan("승인 차단과 함께 State도 바뀌어 current 입력으로 다시 계획한다.")
        self.assertEqual((True, "GOAL_AUTHORIZATION_REQUIRED", "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))
        self._queue_replan(review=g1b._clean_review(), statement="app.py 값을 current 입력으로 다시 검사한다.")
        self._until_recovered_activation()
        self.assertEqual(3, self._plan(self._active_plan_id()).revision_no)

    # --- AC11(c)(d): cancelled·paused -------------------------------------------------

    def test_cancelled_workflow_rejects_the_candidate_before_the_approval_insert(self) -> None:
        rev1, candidate = self._gar_candidate()
        self.application.cancel(self.project_id, reason="사용자 취소")
        self._assert_rejected_by_console(WIDE, "WORKFLOW_CANCELLED", candidate=candidate, before_insert=True)
        self._assert_capability_burned(WIDE, EngineServiceError, "^WORKFLOW_CANCELLED")
        self.assertEqual(rev1, self._active_plan_id())

    def test_paused_workflow_activates_the_candidate_and_keeps_the_pause(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self.application.pause(self.project_id, reason="사용자 일시정지")

        def controls():
            return self._rows(
                "SELECT sequence,event_type FROM history_events WHERE project_id=? "
                "AND event_type LIKE 'workflow.%' ORDER BY sequence", self.project_id,
            )

        before = controls()
        code, payload, displayed = self._host(WIDE)
        self.assertEqual(0, code, payload)
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        self.assertEqual(candidate, self._active_plan_id())
        self.assertEqual(before, controls())
        self.assertEqual("paused", self.service.workflow_control_state(self.project_id))
        snapshot = self._ledger()
        paused = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, "WORKFLOW_PAUSED"), (paused.action, paused.blocker_code))
        self.assertEqual(snapshot, self._ledger())

    # --- AC11(e): 두 console 경쟁과 다음 run_once ---------------------------------------

    def test_two_consoles_race_and_exactly_one_approval_activates_the_candidate(self) -> None:
        _rev1, candidate = self._gar_candidate()
        before = self._ledger()
        barrier = threading.Barrier(2, timeout=30)
        emitted: dict[str, object] = {}
        codes: dict[str, object] = {}

        class RacingHost(TrustedConsoleHost):
            def _show_target(self, target, *, audit_source):
                super()._show_target(target, audit_source=audit_source)
                barrier.wait()  # 두 console 모두 같은 후보를 표시한 뒤에 확인한다.

        def emit(value):
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            emitted[threading.current_thread().name] = json.loads(json.dumps(value, default=str))

        digest = self.service.goal_authorization_target(
            project_id=self.project_id, operating_policy=WIDE,
        ).target_digest
        shown: dict[str, io.StringIO] = {}

        def console(name):
            shown[name] = io.StringIO()
            try:
                codes[name] = RacingHost(
                    input_stream=TerminalInput(digest + "\n"), approval_stream=shown[name],
                ).run(self._argv(WIDE))
            except BaseException as error:  # noqa: BLE001 - 경쟁 결과 관측
                codes[name] = error

        with patch.object(cli, "_emit", emit):
            threads = [threading.Thread(target=console, args=(name,), name=name) for name in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual([0, 2], sorted(codes.values()), codes)
        loser = next(name for name, code in codes.items() if code == 2)
        self.assertEqual("PLAN_SELECTION_STALE", emitted[loser]["error_code"], emitted[loser])
        for name in ("a", "b"):
            text = shown[name].getvalue()
            self.assertEqual(candidate, json.loads(text[text.index("{"):text.rindex("}") + 1])["plan_revision_id"])
        after = self._ledger()
        self.assertEqual(len(before["authorizations"]) + 1, len(after["authorizations"]))
        self.assertEqual(len(before["activations"]) + 1, len(after["activations"]))
        self.assertEqual(1, len(self._activation(candidate)))

    def test_next_run_once_does_not_reactivate_and_proceeds_to_the_goal(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self.assertEqual(0, self._host(WIDE)[0])
        calls = g1.ReplanCandidateTests._count(
            self.service, "_register_plan", "register_plan_evaluation", "activate_plan",
            "activate_authorized_plan",
        )
        self._complete_goal(self._plan(candidate).definition.tasks[0].task_id)
        self.assertEqual({name: 0 for name in calls}, calls)
        self.assertEqual(1, len(self._activation(candidate)))
        self.assertEqual(1, g1.ReplanCandidateTests._events(
            self.service, self.project_id, "plan.activated", candidate,
        ))

    # --- AC11(f): 재계획 후보가 없는 기존 경로 -----------------------------------------------

    def _legacy_target(self, policy):
        """재계획 후보 판정만 뺀 선택(기존 search 기록·단일 ready 선택)의 target."""

        with patch.object(EngineService, "_replan_candidate_for_activation", return_value=None):
            return self.service.goal_authorization_target(project_id=self.project_id, operating_policy=policy)

    def _assert_target_is_legacy(self, policy) -> str:
        target = self.service.goal_authorization_target(project_id=self.project_id, operating_policy=policy)
        legacy = self._legacy_target(policy)
        self.assertEqual((legacy.model_dump(mode="json"), legacy.target_digest),
                         (target.model_dump(mode="json"), target.target_digest))
        return target.target_digest

    def test_targets_without_a_replan_candidate_are_unchanged(self) -> None:
        task_id = self._prepare()
        for policy in (None, WIDE):
            self._assert_target_is_legacy(policy)
        self._authorize()
        rev1 = self._active_plan_id()
        digests = {self._assert_target_is_legacy(WIDE)}
        attempt, binding = self._dispatch_worker(task_id)
        digests.add(self._assert_target_is_legacy(WIDE))
        self._fail_worker(attempt, binding, response="Task 계약이 현재 대상과 맞지 않습니다.",
                          error_code="TASK_CONTRACT_INVALID")
        digests.add(self._assert_target_is_legacy(WIDE))
        self._queue_replan(review=g1b._finding_review(), statement="app.py 값을 검사한다.")
        for _ in range(80):
            self.application.run_once(self.project_id)
            if self._jobs():
                break
        digests.add(self._assert_target_is_legacy(WIDE))
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", self._until_blocked().blocker_code)
        digests.add(self._assert_target_is_legacy(WIDE))
        self.assertEqual(1, len(digests))
        self.assertEqual(rev1, self.service.goal_authorization_target(project_id=self.project_id).plan_revision_id)

        # 후보 없는 active Plan 재승인: 승인만 기록하고 기존 activation을 돌려준다.
        original_activation = self._activation(rev1)[0][0]
        before = self._ledger()
        code, payload, displayed = self._host(None)
        self.assertEqual(0, code, payload)
        self.assertEqual(self._plan_fields(rev1), self._displayed_fields(displayed))
        self.assertEqual(original_activation, payload["activation_id"])
        after = self._ledger()
        self.assertEqual(len(before["authorizations"]) + 1, len(after["authorizations"]))
        self.assertEqual(before["activations"], after["activations"])
        self.assertEqual(rev1, self._active_plan_id())

    def test_cli_plan_activate_keeps_its_selection_while_a_candidate_waits(self) -> None:
        rev1, candidate = self._gar_candidate()
        before = self._ledger()
        code, result = self._cli("plan", "activate", "--project-id", self.project_id)
        self.assertEqual((0, self._activation(rev1)[0][0]), (code, result["activation_id"]))
        self.assertEqual(before, self._ledger())
        self.assertEqual("ready", {row[0]: row[3] for row in self._plans()}[candidate])

    _cli = g1b.ReplanFacadePathTests._cli

    # --- AC11(g): 효과 정책 밖 statement ---------------------------------------------------

    def test_effect_outside_the_policy_blocks_and_replan_and_revise_do_not_touch_the_goal(self) -> None:
        self._fail_contract()
        rev1 = self._active_plan_id()
        effect = g1b._expansion("app.py 값을 검사한다.")
        effect["tasks"][0]["expected_effects"] = [
            {"effect_id": "g1b-external", "statement": "G1b 외부 서비스 배포", "external": True},
        ]
        self.runner.responses.setdefault("plan_expander", []).append(effect)
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code, blocked)
        self.assertIn("PLAN_EFFECT_POLICY_VIOLATION", blocked.detail)
        self.assertIn("blocked", blocked.detail)
        self.assertEqual(rev1, self.service.goal_authorization_target(project_id=self.project_id).plan_revision_id)
        self._assert_rejected("REPLAN_RETRY_NOT_REMEDIABLE")
        before = self._ledger()
        calls = len(self.runner.calls)
        with self.assertRaisesRegex(EngineApplicationError, "^GOAL_REVISION_ACTIVE_PLAN"):
            self.application.revise(self.project_id, source_request="외부 서비스 배포까지 허용해 주세요.")
        self.assertEqual(before, self._ledger())
        self.assertEqual(calls, len(self.runner.calls))


class StaleReplanRejectionTests(_ConsoleHarness, unittest.TestCase):
    """STALE 적격은 첫 code가 STALE 또는 GAR이고 다른 활성화 차단이 없을 때뿐이다(v3.1 예정분)."""

    def test_blocked_candidate_is_not_remediable_even_when_its_state_is_stale(self) -> None:
        self._fail_contract()
        effect = g1b._expansion("app.py 값을 검사한다.")
        effect["tasks"][0]["expected_effects"] = [
            {"effect_id": "g1b-external", "statement": "G1b 외부 서비스 배포", "external": True},
        ]
        self.runner.responses.setdefault("plan_expander", []).append(effect)
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", self._until_blocked().blocker_code)
        candidate = self._plans()[-1][0]
        # 재현용 강제 재관측(I1 STALE 회귀와 같은 수단)으로 후보 base State를 stale로 만든다.
        self.service.reobserve_project(self.project_id, force_state_revision=True)
        self.assertNotEqual(self._current_state_digest(), self._plan(candidate).definition.base_state_snapshot_digest)
        message = self._assert_rejected("REPLAN_RETRY_NOT_REMEDIABLE")
        self.assertIn("decision=blocked", message)
        self.assertNotIn("cancel", message.lower())


class _ObservationCrash(RuntimeError):
    """재관측이 새 Map을 기록한 뒤 State를 기록하기 전에 프로세스가 멈춘 것을 흉내 낸다."""


class CoStaleProductPathTests(_ConsoleHarness, unittest.TestCase):
    """v3.2 AC14: 대상 파일 변경 뒤 제품 재관측이 새 ProjectMap과 StateSnapshot을 함께 기록한 경우."""

    def _change_and_reobserve(self) -> None:
        """사용자가 대상 파일을 바꾸고 제품 재관측(기본 인자, 강제 아님)이 새 Map·State를 기록한다."""

        self.app_file.write_text("value = 1\n# 사용자가 대상 파일을 고쳤다.\n", encoding="utf-8")
        self.service.reobserve_project(self.project_id)

    def _reobserve_crashing_before_state(self) -> None:
        """제품 재관측이 새 ProjectMap을 기록한 뒤 StateSnapshot 기록 전에 멈춘 상태를 만든다."""

        real = self.service.record_state_snapshot

        def crash(_snapshot):
            raise _ObservationCrash("새 Map 기록 뒤 State 기록 전 중단")

        self.service.record_state_snapshot = crash
        try:
            with self.assertRaises(_ObservationCrash):
                self._change_and_reobserve()
        finally:
            self.service.record_state_snapshot = real

    def _assert_map_only_rejection(self, candidate: str, head_code: str) -> None:
        """Map만 비current인 적격 code 후보: 재시도 없이 거절하고, 다음 재관측 뒤에만 STALE 재시도가 된다."""

        self.assertEqual((1, 0), self._base_currency(candidate))
        message = self._assert_rejected("REPLAN_RETRY_ACTIVATION_BLOCKED")
        self.assertIn(f"head blocker={head_code}", message)
        self.assertIn("ProjectMap이 최신 revision이 아닙니다", message)
        self.assertIn("재관측", message)
        self.assertNotIn("cancel", message.lower())
        self.assertNotIn("취소", message)
        # 안내대로 run-once는 이 차단에서 재관측하지 않고 같은 차단을 원장 변화 없이 재생한다.
        before, calls = self._ledger(), len(self.runner.calls)
        replay = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, head_code), (replay.action, replay.blocker_code))
        self.assertEqual(before, self._ledger())
        self.assertEqual(calls, len(self.runner.calls))
        self.assertNotIn("plan_refiner", [call.role for call in self.runner.calls])
        self.assertEqual((1, 0), self._base_currency(candidate))
        # 다음 재관측이 새 StateSnapshot을 기록하면 base State도 stale해져 STALE 재시도로 기록된다.
        self.service.reobserve_project(self.project_id)
        self.assertEqual((0, 0), self._base_currency(candidate))
        result = self._replan("재관측 뒤 current State·Map으로 다시 계획한다.")
        self.assertEqual((True, head_code, "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))

    def _assert_co_stale(self, candidate: str) -> None:
        # 후보의 base State와 그 State가 결속한 Map이 함께 비current이고, current State는 current Map을 결속한다.
        self.assertEqual((0, 0), self._base_currency(candidate))
        plan = self._plan(candidate)
        goal = self.service.load_active_goal(self.project_id)
        state = self.service.load_current_state(self.project_id, goal.definition_digest)
        project_map = self.service.load_current_project_map(self.project_id)
        bound = next(item.value for item in state.facts if item.fact_id == "fact_project_map")
        self.assertEqual(project_map.revision_digest, bound)
        base_state = json.loads(self._rows(
            "SELECT payload_json FROM state_snapshots WHERE snapshot_digest=?",
            plan.definition.base_state_snapshot_digest,
        )[0][0])
        self.assertEqual(
            plan.definition.project_map_digest,
            next(item["value"] for item in base_state["facts"] if item["fact_id"] == "fact_project_map"),
        )
        self.assertNotEqual(project_map.revision_digest, plan.definition.project_map_digest)

    def _assert_bound_to_current_inputs(self, plan_revision_id: str) -> None:
        plan = self._plan(plan_revision_id)
        self.assertEqual(self._current_state_digest(), plan.definition.base_state_snapshot_digest)
        self.assertEqual(self.service.load_current_project_map(self.project_id).revision_digest,
                         plan.definition.project_map_digest)
        self.assertEqual((1, 1), self._base_currency(plan_revision_id))

    @contextlib.contextmanager
    def _expander_paused(self):
        """재계획 job이 current State·Map을 읽은 뒤 plan_expander 역할 호출 앞에서 기다리게 한다."""

        entered, release = threading.Event(), threading.Event()
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_expander" and not release.is_set():
                entered.set()
                release.wait(20)
            return real_run(request, validator=validator)

        self.runner.run = gated
        try:
            for _ in range(80):
                self.application.run_once(self.project_id)
                if entered.wait(0.05):
                    break
            self.assertTrue(entered.is_set(), "재계획 job이 plan_expander에 도달하지 않았습니다.")
            yield
        finally:
            release.set()
            self.runner.run = real_run

    def test_gar_candidate_with_state_and_map_stale_is_replanned_on_current_inputs(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self._change_and_reobserve()
        self._assert_co_stale(candidate)
        # run_once의 보고 순서(승인 먼저)는 그대로다.
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", self.application.run_once(self.project_id).blocker_code)
        authorizations = self._rows("SELECT id FROM goal_authorizations WHERE project_id=?", self.project_id)

        result = self._replan("대상 파일이 바뀌어 current State·Map으로 다시 계획한다.")
        self.assertEqual((True, "GOAL_AUTHORIZATION_REQUIRED", "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))
        self._queue_replan(review=g1b._clean_review(), statement="app.py 값을 current 입력으로 다시 검사한다.")
        self._until_recovered_activation()
        active = self._active_plan_id()
        self.assertEqual((3, candidate), (self._plan(active).revision_no,
                                          self._plan(active).supersedes_plan_revision_id))
        self._assert_bound_to_current_inputs(active)
        self.assertEqual(authorizations, self._rows(
            "SELECT id FROM goal_authorizations WHERE project_id=?", self.project_id,
        ))

    def test_candidate_made_stale_by_a_file_change_during_replanning_is_retried_on_current_inputs(self) -> None:
        self._fail_contract()
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT)
        with self._expander_paused():
            self._change_and_reobserve()
        blocked = self._until_blocked()
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", blocked.blocker_code, blocked)
        candidate = self._plans()[-1][0]
        self._assert_co_stale(candidate)

        result = self._replan("재계획 중 대상 파일이 바뀌어 current State·Map으로 다시 계획한다.")
        self.assertEqual((True, "PLAN_STATE_SNAPSHOT_STALE", "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))
        self._queue_replan(review=g1b._clean_review(), statement="app.py 값을 current 입력으로 다시 검사한다.")
        self._until_recovered_activation()
        active = self._active_plan_id()
        self.assertEqual((3, candidate), (self._plan(active).revision_no,
                                          self._plan(active).supersedes_plan_revision_id))
        self._assert_bound_to_current_inputs(active)

    def test_needs_revision_candidate_with_state_and_map_stale_uses_a_stale_basis(self) -> None:
        _task_id, _attempt, candidate = self._blocked_needs_revision()
        self._change_and_reobserve()
        self._assert_co_stale(candidate)

        result = self._replan("대상 파일이 바뀌어 current State·Map으로 다시 계획한다.")
        self.assertEqual((True, "REPLAN_CANDIDATE_NOT_ADMISSIBLE", "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))
        self.assertEqual("candidate_state_stale", self._retry_events()[-1]["basis"]["kind"])
        self.assertNotIn("findings", self._retry_events()[-1]["basis"])
        calls = len(self.runner.calls)
        self._queue_replan(review=g1b._clean_review(), statement="app.py 값을 current 입력으로 다시 검사한다.")
        self._until_recovered_activation()
        retry_calls = self.runner.calls[calls:]
        # STALE basis는 current 입력으로 다시 상세화한다. finding을 역할 입력에 넣는 plan_refiner는 부르지 않는다.
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], [call.role for call in retry_calls])
        self.assertNotIn("plan_refiner", [call.role for call in self.runner.calls])
        expander_input = json.dumps(retry_calls[0].payload, ensure_ascii=False, default=str)
        self.assertNotIn(g1b._FINDING_CODE, expander_input)
        self.assertNotIn(g1b._FINDING_SUMMARY, expander_input)
        self._assert_bound_to_current_inputs(self._active_plan_id())

    def test_authorize_rejects_a_candidate_with_state_and_map_stale_before_the_approval_insert(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self._change_and_reobserve()
        self._assert_co_stale(candidate)
        for policy in (WIDE, None):
            with self.subTest(policy=policy):
                self._assert_rejected_by_console(
                    policy, "PLAN_STATE_SNAPSHOT_STALE", candidate=candidate, before_insert=True,
                )
        self._assert_capability_burned(WIDE, EngineServiceError, "^PLAN_STATE_SNAPSHOT_STALE")

    def test_map_only_noncurrent_candidate_is_rejected_by_replan_and_authorize(self) -> None:
        """제품 재관측은 새 Map을 기록하면 새 State도 기록한다. 두 기록은 별도 transaction이라 그 사이
        중단만 Map만 비current인 상태를 남긴다(다음 재관측이 새 State로 메운다). 그 중단을 흉내 낸다."""

        self._fail_contract()
        self._queue_replan(review=g1b._clean_review(), statement=_STATEMENT)
        with self._expander_paused():
            self._reobserve_crashing_before_state()
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_ACTIVATION_BLOCKED", blocked.blocker_code, blocked)
        self.assertIn("Project Map이 최신 revision이 아닙니다", blocked.detail)
        candidate = self._plans()[-1][0]
        self.assertEqual((1, 0), self._base_currency(candidate))

        # replan: base State가 current이므로 STALE 적격이 아니다.
        self.assertIn("Project Map이 최신 revision이 아닙니다",
                      self._assert_rejected("REPLAN_RETRY_ACTIVATION_BLOCKED"))
        # authorize: 기존 Project Map 비current 오류로 승인 insert 전에 거절한다.
        before = self._ledger()
        statements: list[str] = []
        code, payload, displayed = self._host(WIDE, statements=statements)
        self.assertEqual(2, code, payload)
        self.assertIn("Project Map이 최신 revision이 아닙니다", payload["message"])
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        self.assertEqual(before, self._ledger())
        upper = [sql.upper() for sql in statements]
        self.assertFalse(any("INSERT INTO GOAL_AUTHORIZATIONS" in sql for sql in upper))
        self.assertFalse(any("INSERT INTO PLAN_ACTIVATIONS" in sql for sql in upper))
        self._assert_capability_burned(WIDE, EngineServiceError, "Project Map이 최신 revision이 아닙니다")

    # --- N-17: base State는 current이고 ProjectMap만 비current인 적격 code 후보 ---------

    def test_needs_revision_candidate_with_only_its_map_noncurrent_is_rejected_without_refine(self) -> None:
        _task_id, _attempt, candidate = self._blocked_needs_revision()
        self._reobserve_crashing_before_state()
        self._assert_map_only_rejection(candidate, "REPLAN_CANDIDATE_NOT_ADMISSIBLE")

    def test_gar_candidate_with_only_its_map_noncurrent_is_rejected_as_activation_blocked(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self._reobserve_crashing_before_state()
        # 재승인은 실제 다음 행동이 아니다: authorize도 기존 Project Map 오류로 insert 전에 거절한다.
        before = self._ledger()
        code, payload, displayed = self._host(WIDE)
        self.assertEqual(2, code, payload)
        self.assertIn("Project Map이 최신 revision이 아닙니다", payload["message"])
        self.assertEqual(self._plan_fields(candidate), self._displayed_fields(displayed))
        self.assertEqual(before, self._ledger())
        self._assert_map_only_rejection(candidate, "GOAL_AUTHORIZATION_REQUIRED")

    # --- M12a: GAR+stale 후보에서 GAR 밖 전제 가운데 quiescence만 깨진 경우 -----------------

    def test_gar_stale_candidate_with_an_inflight_operation_is_rejected_until_it_completes(self) -> None:
        _rev1, candidate = self._gar_candidate()
        self._change_and_reobserve()
        self._assert_co_stale(candidate)
        # 진행 중 효과: 제품 CoreOperations가 operation.prepared를 남기고 효과 실행 중에 머문다(완료 관측 전).
        kind, request = "g1b_inflight_probe", {"probe": "m12a"}
        operation_id = CoreOperations._operation_id(self.project_id, kind, request)[1]
        entered, release, errors = threading.Event(), threading.Event(), []

        def effect():
            entered.set()
            release.wait(20)
            return {"observed": True}

        def invoke():
            try:
                CoreOperations(self.service).invoke(
                    project_id=self.project_id, kind=kind, request=request, execute=effect,
                )
            except BaseException as error:  # noqa: BLE001 - worker 결과 관측
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(10), "진행 중 효과가 시작되지 않았습니다.")
            # 기록하지 않고 거절한다. _assert_rejected가 assessment·History·runtime_jobs 포함 원장 사본과
            # 역할 호출 수가 그대로인지 확인한다.
            message = self._assert_rejected("REPLAN_RETRY_ACTIVATION_BLOCKED")
        finally:
            release.set()
            worker.join(20)
        self.assertEqual([], errors)
        self.assertFalse(worker.is_alive())
        guidance, detail = message.split(" detail=", 1)
        self.assertEqual(
            "REPLAN_RETRY_ACTIVATION_BLOCKED: head blocker=GOAL_AUTHORIZATION_REQUIRED. 후보의 base StateSnapshot은 "
            "stale이지만 승인 밖 활성화 사전 조건이 맞지 않아 STALE 재시도를 기록하지 않습니다. "
            "run-once는 이 후보를 활성화하지 않습니다. 진행 중인 Plan 교체처럼 끝나면 풀리는 조건은 "
            "해소된 뒤 replan을 다시 실행하고, 그 밖의 조건은 detail로 원인을 확인합니다.",
            guidance,
        )
        prefix = "PLAN_REPLACEMENT_IN_FLIGHT: 기존 실행·검사·미확정 효과를 먼저 관측해야 합니다: "
        self.assertTrue(detail.startswith(prefix), detail)
        # 깨진 전제는 진행 중 operation 하나뿐이다(Attempt·intent·검사·provider call·job은 없다).
        self.assertEqual(
            {"attempts": [], "intents": [], "validating_tasks": [], "provider_calls": [],
             "runtime_jobs": [], "operations": [operation_id]},
            json.loads(detail[len(prefix):]),
        )

        # 효과가 완료 관측되면 같은 명령이 STALE basis로 기록된다.
        self.assertEqual({"observed": True}, CoreOperations(self.service).completed_result(
            project_id=self.project_id, kind=kind, request=request,
        ))
        result = self._replan("진행 중 효과가 끝나 current State·Map으로 다시 계획한다.")
        self.assertEqual((True, "GOAL_AUTHORIZATION_REQUIRED", "candidate_state_stale"),
                         (result["recorded"], result["head_blocker_code"], result["basis_kind"]))


class StaleActivationBlockedRejectionTests(unittest.TestCase):
    """G1 harness(EngineDispatcher + 주입 후보)로 활성화 차단과 stale이 겹친 후보의 replan 거절을 본다."""

    setUp = g1.automatic.AutomaticRecoveryIntegrationTests.setUp
    prepared = g1.automatic.AutomaticRecoveryIntegrationTests.prepared
    _failed_attempt = g1.automatic.AutomaticRecoveryIntegrationTests._failed_attempt
    _finish_runtime_job_tick = g1.automatic.AutomaticRecoveryIntegrationTests._finish_runtime_job_tick
    _evaluation = g1.ReplanCandidateTests._evaluation
    _authorization_expanding = g1.ReplanCandidateTests._authorization_expanding
    _to_replan = g1.ReplanCandidateTests._to_replan
    _provider = staticmethod(g1.ReplanCandidateTests._provider)
    _ledger = staticmethod(g1.ReplanCandidateTests._ledger)

    def _other_lineage(self, prepared, base):
        """같은 plan_id 계보가 아닌 후보. 등록은 되지만 active Plan을 교체할 수 없다."""

        from flowmarshal.engine.domain import PlanContractRevision, derive_candidate_decision, new_id
        from flowmarshal.engine.planning import ExpandedPlanEvaluation, plan_review_evidence_catalog
        from tests.engine_helpers import clean_review

        plan = PlanContractRevision.model_validate(base.plan.model_dump(mode="json") | {
            "plan_id": new_id("plan"), "revision_no": 1, "supersedes_plan_revision_id": None,
        })
        service = prepared.service
        goal = service.load_active_goal(prepared.project_id)
        catalog = plan_review_evidence_catalog(
            plan, goal, service.load_current_state(prepared.project_id, goal.definition_digest),
            service.load_current_project_map(prepared.project_id),
        )
        review = clean_review(plan.activation_digest, role="independent_recovery_reviewer", evidence_catalog=catalog)
        return ExpandedPlanEvaluation(
            plan=plan, deterministic_findings=(), semantic_submissions=(review,),
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest, findings=(), ratings=review.ratings,
            ),
        )

    def _stale_rejection(self, prepared, runtime, evaluation, head_code: str) -> str:
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        blocked = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(head_code, blocked.blocker_code, blocked)
        service = prepared.service
        # 재현용 강제 재관측으로 후보 base State를 stale로 만든다. 첫 code는 그대로다.
        service.reobserve_project(prepared.project_id, force_state_revision=True)
        goal = service.load_active_goal(prepared.project_id)
        self.assertNotEqual(
            service.load_current_state(prepared.project_id, goal.definition_digest).snapshot_digest,
            evaluation.plan.definition.base_state_snapshot_digest,
        )
        self.assertEqual(head_code, dispatcher.run_once(prepared.project_id).blocker_code)
        before = self._ledger(service, prepared.project_id)
        with self.assertRaises(EngineServiceError) as raised:
            service.request_subgraph_replan_retry(prepared.project_id, rationale="stale 거절 확인")
        self.assertTrue(str(raised.exception).startswith("REPLAN_RETRY_ACTIVATION_BLOCKED:"), str(raised.exception))
        self.assertEqual(before, self._ledger(service, prepared.project_id))
        return str(raised.exception)

    def test_activation_blocked_candidate_is_rejected_even_when_its_state_is_stale(self) -> None:
        prepared, runtime = self.prepared(name="r4b-stale-activation")
        evaluation = self._other_lineage(prepared, self._evaluation(prepared))
        message = self._stale_rejection(prepared, runtime, evaluation, "REPLAN_CANDIDATE_ACTIVATION_BLOCKED")
        self.assertIn("head blocker=REPLAN_CANDIDATE_ACTIVATION_BLOCKED", message)

    def test_stale_candidate_reported_as_reauthorization_with_a_hidden_lineage_block_is_rejected(self) -> None:
        prepared, runtime = self.prepared(name="r4b-stale-gar-lineage")
        evaluation = self._other_lineage(prepared, self._authorization_expanding(prepared))
        message = self._stale_rejection(prepared, runtime, evaluation, "GOAL_AUTHORIZATION_REQUIRED")
        self.assertIn("head blocker=GOAL_AUTHORIZATION_REQUIRED", message)
        self.assertIn("supersedes 계보", message)


class AmbiguousCandidateSelectionTests(unittest.TestCase):
    """job 결과와 원장 후보의 결속이 어긋나면 선택을 추측하지 않는다(G1 harness 무결성 주입)."""

    setUp = g1.automatic.AutomaticRecoveryIntegrationTests.setUp
    prepared = g1.automatic.AutomaticRecoveryIntegrationTests.prepared
    _failed_attempt = g1.automatic.AutomaticRecoveryIntegrationTests._failed_attempt
    _finish_runtime_job_tick = g1.automatic.AutomaticRecoveryIntegrationTests._finish_runtime_job_tick
    _evaluation = g1.ReplanCandidateTests._evaluation
    _to_replan = g1.ReplanCandidateTests._to_replan
    _provider = staticmethod(g1.ReplanCandidateTests._provider)
    _ledger = staticmethod(g1.ReplanCandidateTests._ledger)

    def test_binding_mismatch_requires_a_single_core_selection(self) -> None:
        prepared, runtime = self.prepared(name="r4b-ambiguous")
        dispatcher, _calls = self._to_replan(prepared, runtime, self._evaluation(prepared))
        prepared.service.register_plan_evaluation(self._evaluation(prepared, reviewer_finding=True))
        blocked = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual("REPLAN_CANDIDATE_BINDING_MISMATCH", blocked.blocker_code)
        before = self._ledger(prepared.service, prepared.project_id)
        with self.assertRaisesRegex(EngineServiceError, "^PLAN_SELECTION_REQUIRED"):
            prepared.service.goal_authorization_target(project_id=prepared.project_id)
        self.assertEqual(before, self._ledger(prepared.service, prepared.project_id))


if __name__ == "__main__":
    unittest.main()
