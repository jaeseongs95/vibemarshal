"""G1b: 재계획 차단의 사용자 경로(replan)·C-1 계보 분리·run_once/status parity.

실제 `EngineApplication` + `RecoveryPlanProvider` + scripted structured runner(fm08 방식)로
NOT_ADMISSIBLE·STALE 차단에서 facade `replan`이 Core 기록만 하고 다음 run_once가
`replanning:{retry_id}` RuntimeJob으로 역할을 호출하는지, 거절·한도·멱등·재시작·동시 실행이
원장 표 사본으로 zero-mutation인지, status가 run_once와 같은 판정을 표시하는지 본다.
결정적 테스트이며 live qualification이 아니다.
"""
from __future__ import annotations

import copy
import inspect
import io
import json
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import tests.test_engine_fm08_recovery_integration as fm08
import tests.test_engine_g1_replan_candidate as g1
from flowmarshal.canonical import sha256_bytes
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.capabilities import CoreCapabilityError, role_execution_scope
from flowmarshal.engine.cli import build_parser, main
from flowmarshal.engine.domain import (
    GoalOperatingPolicy,
    RuntimeJobKind,
    RuntimeJobStatus,
    PlanContractRevision,
    RecoveryAssessment,
    RunOnceAction,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.recovery import (
    current_replan_assessment,
    replan_retry_assessment_id,
    stable_recovery_assessment_id,
)
from flowmarshal.engine.recovery_planning import RECOVERY_PLAN_REVIEWER_ROLE, RecoveryPlanProvider
from flowmarshal.engine.runtime import (
    REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE,
    REPLAN_PROVIDER_REQUIRED_DETAIL,
    FakeCodexRuntime,
    RuntimeJobSupervisor,
    notify_active_runtime_job_progress,
)
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.engine_helpers import inventory


_FINDING_CODE = "G1B_CHECK_DOES_NOT_REOBSERVE"
_FINDING_SUMMARY = "재계획 검사 statement가 실패 근거인 실제 파일 값을 다시 관측하지 않는다."


def _finding_review(*, severity: str = "error", remediable: bool = True) -> dict:
    return {
        "findings": [{
            "finding_code": _FINDING_CODE,
            "gate": "verification",
            "severity": severity,
            "summary": _FINDING_SUMMARY,
            "evidence_refs": ["artifact:plan_contract", "source:goal"],
            "affected_task_refs": ["single_task"],
            "remediable": remediable,
        }],
        "ratings": None,
    }


def _clean_review() -> dict:
    return {"findings": [], "ratings": fm08._ratings()}


def _expansion(statement: str, **recovery) -> dict:
    expansion = fm08._plan_expansion(acceptance=["Task 검사가 PASS다."], statement=statement)
    expansion["tasks"][0]["recovery"].update(recovery)
    return expansion


class _ReplanHarness:
    """fm08 facade harness(EngineApplication + 실제 RecoveryPlanProvider)를 빌려 쓴다."""

    _Base = fm08.EngineFm08RecoveryIntegrationTests
    setUp = _Base.setUp
    _close_supervisors = _Base._close_supervisors
    _prepare = _Base._prepare
    _authorize = _Base._authorize
    _queue_execution_preparation = _Base._queue_execution_preparation
    _queue_goal_validation = _Base._queue_goal_validation
    _run_until = _Base._run_until
    _settle = _Base._settle
    _worker_binding = _Base._worker_binding
    _dispatch_worker = _Base._dispatch_worker
    _attempt_status = _Base._attempt_status
    _fail_worker = _Base._fail_worker

    # --- 원장 관측 ---------------------------------------------------------

    def _ledger(self) -> dict:
        """AC3 표(project·plans·…·authorizations)와 assessment·evidence·Goal·provider 호출 사본."""

        snapshot = g1.ReplanCandidateTests._ledger(self.service, self.project_id)
        with self.service.ledger.read() as c:
            def rows(query):
                return [tuple(row) for row in c.execute(query, (self.project_id,))]

            snapshot |= {
                "assessments": rows("SELECT id,payload_json FROM recovery_assessments "
                                    "WHERE project_id=? ORDER BY rowid"),
                "evidence": rows("SELECT id FROM evidence_records WHERE project_id=? ORDER BY rowid"),
                "goal_revisions": rows("SELECT id,status FROM goal_revisions WHERE project_id=? ORDER BY rowid"),
                "provider_calls": rows("SELECT id,status FROM provider_calls WHERE project_id=? ORDER BY rowid"),
            }
        return snapshot

    def _rows(self, query: str, *parameters) -> list[tuple]:
        with self.service.ledger.read() as connection:
            return [tuple(row) for row in connection.execute(query, parameters)]

    def _plans(self) -> list[tuple]:
        return self._rows(
            "SELECT id,plan_id,revision_no,status,supersedes_id FROM plan_revisions "
            "WHERE project_id=? ORDER BY revision_no", self.project_id,
        )

    def _plan(self, plan_revision_id: str) -> PlanContractRevision:
        return PlanContractRevision.model_validate_json(self._rows(
            "SELECT payload_json FROM plan_revisions WHERE id=?", plan_revision_id,
        )[0][0])

    def _active_plan_id(self) -> str:
        return self._rows(
            "SELECT active_plan_revision_id FROM projects WHERE id=?", self.project_id,
        )[0][0]

    def _current_state_digest(self) -> str:
        return self._rows(
            "SELECT snapshot_digest FROM state_snapshots WHERE project_id=? AND is_current=1",
            self.project_id,
        )[0][0]

    def _retry_events(self) -> list[dict]:
        return [json.loads(row[0]) for row in self._rows(
            "SELECT payload_json FROM history_events WHERE project_id=? "
            "AND event_type='recovery.replan_retry_requested' ORDER BY sequence", self.project_id,
        )]

    def _jobs(self, kind: str = "replanning") -> list[tuple]:
        return self._rows(
            "SELECT checkpoint_key,status FROM runtime_jobs WHERE project_id=? AND kind=? ORDER BY rowid",
            self.project_id, kind,
        )

    # --- 시나리오 ----------------------------------------------------------

    def _fail_contract(self) -> tuple[str, str]:
        task_id = self._prepare()
        self._authorize()
        attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(
            attempt, binding,
            response="Task 계약이 현재 대상과 맞지 않습니다.",
            error_code="TASK_CONTRACT_INVALID",
        )
        return task_id, attempt

    def _queue_replan(self, *, review: dict, statement: str, **recovery) -> None:
        self.runner.responses.setdefault("plan_expander", []).append(_expansion(statement, **recovery))
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(review)

    def _queue_refinement(self, *, review: dict, statement: str) -> None:
        self.runner.responses.setdefault("plan_refiner", []).append({
            "action": "detail_revision",
            "rationale": "차단 후보의 finding이 지적한 검사 statement를 실제 파일 재관측으로 고친다.",
            "evidence_refs": ["artifact:plan_contract", "source:goal"],
            "plan": _expansion(statement),
            "skeleton": None,
        })
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(review)

    def _until_blocked(self, *, limit: int = 80):
        result = None
        for _ in range(limit):
            result = self.application.run_once(self.project_id)
            if result.action is RunOnceAction.BLOCKED:
                return result
            self.assertIn(
                result.action,
                {RunOnceAction.RECOVERED, RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED},
                result,
            )
            time.sleep(0.01)
        self.fail(f"BLOCKED에 도달하지 못했습니다: {result}")

    def _until_recovered_activation(self, *, limit: int = 80):
        """예약·관측을 넘어 재계획 후보 활성화(RECOVERED)까지 진행한다."""

        result = None
        for _ in range(limit):
            result = self.application.run_once(self.project_id)
            if result.action is RunOnceAction.RECOVERED:
                return result
            self.assertIn(result.action, {RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED}, result)
            time.sleep(0.01)
        self.fail(f"재계획 활성화에 도달하지 못했습니다: {result}")

    def _blocked_needs_revision(self) -> tuple[str, str, str]:
        task_id, attempt = self._fail_contract()
        self._queue_replan(review=_finding_review(), statement="app.py 값을 검사한다.")
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code, blocked)
        self.assertIn("needs_revision", blocked.detail)
        candidate = self._plans()[-1][0]
        return task_id, attempt, candidate

    def _blocked_stale(self) -> tuple[str, str, str]:
        """적격 후보를 등록한 직후 State가 다시 관측돼 활성화 전에 STALE로 멈춘다."""

        task_id, attempt = self._fail_contract()
        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다.",
        )
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
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", blocked.blocker_code, blocked)
        return task_id, attempt, self._plans()[-1][0]

    def _replan(self, rationale: str = "차단 후보의 finding을 근거로 한 번 다시 계획한다.") -> dict:
        return self.application.replan(self.project_id, rationale=rationale)

    def _assert_rejected(self, code: str, *, application: EngineApplication | None = None) -> str:
        before = self._ledger()
        calls = len(self.runner.calls)
        with self.assertRaises(EngineServiceError) as raised:
            (application or self.application).replan(self.project_id, rationale="거절 확인")
        self.assertTrue(str(raised.exception).startswith(f"{code}:"), str(raised.exception))
        self.assertEqual(before, self._ledger())
        self.assertEqual(calls, len(self.runner.calls))
        return str(raised.exception)

    def _complete_goal(self, task_id: str) -> None:
        self._queue_execution_preparation(task_id)
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        binding = self._worker_binding(dispatched.attempt_id)
        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(binding.thread_id, response="replanned task complete")
        self._run_until(RunOnceAction.VALIDATED)
        self._queue_goal_validation(task_id)
        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        report = self.application.final_report(self.project_id, goal_verdict_id=completed.goal_verdict_id)
        self.assertEqual("satisfied", report.verdict.status.value)


class ReplanFacadePathTests(_ReplanHarness, unittest.TestCase):
    # --- AC3·AC4·AC6·AC7·AC8·AC10: NOT_ADMISSIBLE → replan → run_once → 활성화 → Goal 완료 --

    def test_needs_revision_replan_binds_findings_runs_as_a_job_and_completes_the_goal(self) -> None:
        _task_id, attempt, blocked_id = self._blocked_needs_revision()
        original_id = self._active_plan_id()
        original = self._plan(original_id)
        blocked_row_before = self._rows(
            "SELECT id,plan_id,revision_no,status,supersedes_id,payload_json FROM plan_revisions WHERE id=?",
            blocked_id,
        )
        decision_before = self._rows(
            "SELECT id,status,payload_json FROM candidate_decisions WHERE artifact_digest=?",
            self._plan(blocked_id).activation_digest,
        )
        blocked_status = self.application.status(self.project_id)["recovery"]
        self.assertEqual("user_decision_required", blocked_status["state"])
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked_status["next_action"]["blocker_code"])

        # 명령은 Core 기록만 한다. 역할·provider·supervisor 호출과 runtime job 예약은 없다.
        before = self._ledger()
        calls = len(self.runner.calls)
        supervisor_calls = g1.ReplanCandidateTests._count(
            self.application.supervisor, "schedule", "tick", "reattach",
        )
        result = self._replan()
        after = self._ledger()
        self.assertEqual({"schedule": 0, "tick": 0, "reattach": 0}, supervisor_calls)
        self.assertEqual(calls, len(self.runner.calls))
        self.assertEqual(before["jobs"], after["jobs"])
        self.assertEqual(before["journal"], after["journal"])
        self.assertEqual(before["provider_calls"], after["provider_calls"])
        self.assertEqual(before["evidence"], after["evidence"])
        self.assertEqual(
            ["recovery.assessed", "recovery.replan_retry_requested"],
            [event for _sequence, event in after["history"][len(before["history"]):]],
        )
        for name in ("project", "plans", "decisions", "reviews", "activations", "tasks",
                     "attempts", "state", "authorizations", "goal_revisions"):
            self.assertEqual(before[name], after[name], name)
        self.assertEqual(len(before["assessments"]) + 1, len(after["assessments"]))

        # 결정적 retry ID·typed basis·provenance. EvidenceRecord·evidence_ids에 넣지 않는다.
        fingerprint = json.loads(before["assessments"][0][1])["failure_fingerprint"]
        retry_id = replan_retry_assessment_id(attempt, fingerprint, blocked_id)
        self.assertEqual(
            {"project_id": self.project_id, "assessment_id": retry_id, "recorded": True,
             "blocked_plan_revision_id": blocked_id, "head_blocker_code": "REPLAN_CANDIDATE_NOT_ADMISSIBLE",
             "basis_kind": "candidate_needs_revision",
             "next": f"다음 run-once가 replanning:{retry_id} job을 예약합니다."},
            result,
        )
        retry = RecoveryAssessment.model_validate_json(after["assessments"][-1][1])
        self.assertEqual(retry_id, retry.assessment_id)
        self.assertEqual((), retry.new_evidence_ids)
        self.assertEqual(fingerprint, retry.failure_fingerprint)
        self.assertEqual((2, 2), (retry.same_failure_replan_count, retry.goal_replan_count))
        event = self._retry_events()[-1]
        self.assertEqual(stable_recovery_assessment_id(attempt, fingerprint), event["prior_assessment_id"])
        self.assertEqual("candidate_needs_revision", event["basis"]["kind"])
        self.assertEqual("local_derived", event["basis"]["provenance"])
        self.assertEqual("needs_revision", event["basis"]["decision_status"])
        self.assertEqual(decision_before[0][0], event["basis"]["decision_id"])
        self.assertEqual(
            [(_FINDING_CODE, _FINDING_SUMMARY, ["artifact:plan_contract", "source:goal"], True, "reviewer",
              "model_reported")],
            [(item["finding_code"], item["summary"], item["evidence_refs"], item["remediable"],
              item["source"], item["provenance"]) for item in event["basis"]["findings"]],
        )
        self.assertEqual("user_supplied_audit", event["rationale_provenance"])

        # head parity: 대체된 차단 blocker 대신 다음 run_once가 이어 갈 자동 단계를 표시한다.
        pending = self.application.status(self.project_id)["recovery"]
        self.assertEqual("automatic_pending", pending["state"], pending)
        self.assertEqual(retry_id, pending["limits"]["assessment_id"])

        # 다음 run_once 한 번이 replanning:{retry_id} job 하나를 예약하고 역할 종료를 기다리지 않는다.
        self._queue_refinement(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
        )
        gate = threading.Event()
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_refiner":
                gate.wait(10)
            return real_run(request, validator=validator)

        self.runner.run = gated
        scheduled = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        self.assertEqual("replanning", scheduled.runtime_job_kind)
        stable_id = stable_recovery_assessment_id(attempt, fingerprint)
        # M-14 D1: 첫 재계획은 expand·review 두 phase job이다(rowid 순).
        self.assertEqual(
            [(f"replanning:{stable_id}", "consumed"), (f"replanning:{stable_id}:review", "consumed"),
             (f"replanning:{retry_id}", "running")],
            self._jobs(),
        )
        gate.set()
        recovered = self._until_recovered_activation()
        self.assertEqual(attempt, recovered.attempt_id)

        # 차단 후보의 finding 원문·evidence_refs가 재계획 역할 입력에 결속됐다.
        refiner = next(call for call in self.runner.calls if call.role == "plan_refiner")
        self.assertIn(_FINDING_SUMMARY, [item["summary"] for item in refiner.payload["findings"]])
        self.assertIn(_FINDING_CODE, [item["finding_code"] for item in refiner.payload["findings"]])
        self.assertLessEqual({"artifact:plan_contract", "source:goal"}, set(refiner.payload["evidence_catalog"]))
        self.assertEqual(blocked_id, refiner.payload["evidence_catalog"]["artifact:plan_contract"]["plan_revision_id"])

        # AC7 계보: 같은 plan_id, MAX+1, 최신(차단 후보) supersede. 차단 후보와 decision은 그대로다.
        active_id = self._active_plan_id()
        active = self._plan(active_id)
        self.assertEqual(
            [(original_id, original.plan_id, 1, "superseded", None),
             (blocked_id, original.plan_id, 2, "draft", original_id),
             (active_id, original.plan_id, 3, "active", blocked_id)],
            self._plans(),
        )
        self.assertEqual(blocked_row_before, self._rows(
            "SELECT id,plan_id,revision_no,status,supersedes_id,payload_json FROM plan_revisions WHERE id=?",
            blocked_id,
        ))
        self.assertEqual(decision_before, self._rows(
            "SELECT id,status,payload_json FROM candidate_decisions WHERE artifact_digest=?",
            self._plan(blocked_id).activation_digest,
        ))
        # AC8 의미 보존: Task 집합·integration validation은 active Plan과 같고 입력은 current다.
        self.assertEqual(
            [task.task_ref for task in original.definition.tasks],
            [task.task_ref for task in active.definition.tasks],
        )
        self.assertEqual(original.definition.integration_validations, active.definition.integration_validations)
        self.assertEqual(self._current_state_digest(), active.definition.base_state_snapshot_digest)
        self.assertEqual(
            self.service.load_current_project_map(self.project_id).revision_digest,
            active.definition.project_map_digest,
        )
        self.assertEqual(
            "app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
            active.definition.tasks[0].validations[0].statement,
        )
        # activation digest는 결속된 계보 필드로 다시 계산되고 recovery reviewer가 그 digest를 검토했다.
        reviews = [json.loads(row[0]) for row in self._rows(
            "SELECT payload_json FROM candidate_reviews WHERE project_id=? AND artifact_digest=?",
            self.project_id, active.activation_digest,
        )]
        self.assertEqual([RECOVERY_PLAN_REVIEWER_ROLE], [item["reviewer_role"] for item in reviews])
        self.assertEqual("recovered", self.application.status(self.project_id)["recovery"]["state"])

        self._complete_goal(active.definition.tasks[0].task_id)

    # --- AC6 STALE·AC9(b) 단일 Task: 재시도는 current State로 상세화한다 --------------

    def test_stale_replan_expands_on_the_current_state_and_activates(self) -> None:
        _task_id, attempt, stale_id = self._blocked_stale()
        original_id = self._active_plan_id()
        stale = self._plan(stale_id)
        self.assertNotEqual(self._current_state_digest(), stale.definition.base_state_snapshot_digest)
        self.assertEqual(
            "PLAN_STATE_SNAPSHOT_STALE",
            self.application.status(self.project_id)["recovery"]["next_action"]["blocker_code"],
        )

        result = self._replan("State가 다시 관측돼 current 입력으로 다시 계획한다.")
        self.assertEqual("candidate_state_stale", result["basis_kind"])
        event = self._retry_events()[-1]
        current = self._rows(
            "SELECT id,snapshot_digest FROM state_snapshots WHERE project_id=? AND is_current=1", self.project_id,
        )[0]
        self.assertEqual(
            {"kind": "candidate_state_stale", "provenance": "local_derived",
             "candidate_base_state_snapshot_digest": stale.definition.base_state_snapshot_digest,
             "current_state_snapshot_id": current[0], "current_state_snapshot_digest": current[1],
             "observed_event": "state.observed"},
            {key: value for key, value in event["basis"].items() if key != "observed_sequence"},
        )
        # STALE 재시도도 두 한도에 포함된다(Q2).
        self.assertEqual((2, 2), (event["same_failure_replan_count"], event["goal_replan_count"]))

        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 다시 검사한다.",
        )
        self._until_recovered_activation()
        active_id = self._active_plan_id()
        active = self._plan(active_id)
        self.assertEqual(
            [(original_id, stale.plan_id, 1, "superseded", None),
             (stale_id, stale.plan_id, 2, "ready", original_id),
             (active_id, stale.plan_id, 3, "active", stale_id)],
            self._plans(),
        )
        self.assertEqual(self._current_state_digest(), active.definition.base_state_snapshot_digest)
        # 재계획 수단은 RecoveryPlanProvider의 subgraph 상세화이며 Skeleton 전체 검색이 없다.
        replan_roles = [call.role for call in self.runner.calls][-2:]
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], replan_roles)
        self.assertNotIn("skeleton_generator", [call.role for call in self.runner.calls][6:])
        self.assertEqual(attempt, json.loads(self._ledger()["assessments"][-1][1])["attempt_id"])

    # --- AC6: 후보당 멱등 1회, rationale은 감사값 ------------------------------------

    def test_replan_is_idempotent_per_blocked_candidate_and_rationale_is_only_audit(self) -> None:
        self._blocked_needs_revision()
        first = self._replan("첫 요청")
        before = self._ledger()
        again = self._replan("다른 문장의 두 번째 요청")
        self.assertEqual(first["assessment_id"], again["assessment_id"])
        self.assertFalse(again["recorded"])
        self.assertEqual(before, self._ledger())
        self.assertEqual(["첫 요청"], [item["rationale"] for item in self._retry_events()])

        # 동시에 두 요청이 와도 쓰기 잠금으로 직렬화돼 행은 하나다.
        results: list[object] = []

        def call():
            try:
                results.append(self._replan("동시 요청"))
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                results.append(error)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([first["assessment_id"]] * 2, [item["assessment_id"] for item in results])
        self.assertEqual(before, self._ledger())

    def test_blank_rationale_is_rejected_without_mutation(self) -> None:
        self._blocked_needs_revision()
        before = self._ledger()
        with self.assertRaisesRegex(EngineServiceError, "^REPLAN_RETRY_RATIONALE_REQUIRED"):
            self.application.replan(self.project_id, rationale="  ")
        self.assertEqual(before, self._ledger())

    # --- AC3: 조건별 typed 거절과 zero-mutation ---------------------------------------

    def test_replan_without_a_replan_blocker_is_rejected(self) -> None:
        task_id = self._prepare()
        self._authorize()
        self.assertIn("미해결 실패가 없습니다", self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED"))
        attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(attempt, binding, response="Task 계약이 현재 대상과 맞지 않습니다.",
                          error_code="TASK_CONTRACT_INVALID")
        # 자동 assessment 전, 자동 재계획 진행 중에는 run-once가 먼저 이어 간다.
        self.assertIn("자동 assessment가 아직 없습니다", self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED"))
        self._run_until(RunOnceAction.RECOVERED)
        self.assertIn("아직 예약 전", self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED"))

    def test_replan_rejects_non_remediable_candidates_with_the_e2e15_gap(self) -> None:
        self._fail_contract()
        effect = _expansion("app.py 값을 검사한다.")
        effect["tasks"][0]["expected_effects"] = [
            {"effect_id": "g1b-external", "statement": "G1b 외부 서비스 배포", "external": True}
        ]
        self.runner.responses.setdefault("plan_expander", []).append(effect)
        blocked = self._until_blocked()
        self.assertIn("PLAN_EFFECT_POLICY_VIOLATION", blocked.detail)
        self.assertIn("blocked", blocked.detail)
        message = self._assert_rejected("REPLAN_RETRY_NOT_REMEDIABLE")
        self.assertIn("E2E-15", message)
        self.assertIn("decision=blocked", message)
        self.assertNotIn("cancel", message.lower())
        self.assertNotIn("취소", message)

    def test_replan_rejects_a_rejected_candidate(self) -> None:
        self._fail_contract()
        self._queue_replan(review=_finding_review(severity="warning"), statement="app.py 값을 검사한다.")
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code)
        self.assertIn("rejected", blocked.detail)
        message = self._assert_rejected("REPLAN_RETRY_NOT_REMEDIABLE")
        self.assertIn("decision=rejected", message)
        self.assertNotIn("cancel", message.lower())

    def test_replan_rejects_a_candidate_that_needs_reauthorization(self) -> None:
        self._fail_contract()
        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다.",
            max_same_failure_replans=3,
        )
        blocked = self._until_blocked()
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        self.assertIn("authorize", self._assert_rejected("REPLAN_RETRY_REAUTHORIZATION_REQUIRED"))
        with self.service.ledger.read() as connection:
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM goal_authorizations WHERE project_id=?", (self.project_id,),
            ).fetchone()[0])

    def test_stale_candidate_first_reported_as_reauthorization_required_is_replanned(self) -> None:
        """GAR와 STALE이 함께 참이면 STALE 적격은 blocker 첫 code가 아니라 후보 base State로 판정한다."""

        self._fail_contract()
        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다.",
            max_same_failure_replans=3,
        )
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
        # run_once·status의 보고 순서(승인 먼저)는 그대로다.
        self.assertEqual("GOAL_AUTHORIZATION_REQUIRED", blocked.blocker_code, blocked)
        self.assertEqual(
            "GOAL_AUTHORIZATION_REQUIRED",
            self.application.status(self.project_id)["recovery"]["next_action"]["blocker_code"],
        )
        candidate_id = self._plans()[-1][0]
        self.assertNotEqual(
            self._current_state_digest(), self._plan(candidate_id).definition.base_state_snapshot_digest,
        )
        authorizations = self._rows("SELECT id FROM goal_authorizations WHERE project_id=?", self.project_id)

        result = self._replan("승인 차단과 함께 State도 바뀌어 current 입력으로 다시 계획한다.")
        self.assertEqual(
            (True, "GOAL_AUTHORIZATION_REQUIRED", "candidate_state_stale", candidate_id),
            (result["recorded"], result["head_blocker_code"], result["basis_kind"],
             result["blocked_plan_revision_id"]),
        )
        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 다시 검사한다.",
        )
        self._until_recovered_activation()
        active = self._plan(self._active_plan_id())
        self.assertEqual((3, candidate_id), (active.revision_no, active.supersedes_plan_revision_id))
        self.assertEqual(self._current_state_digest(), active.definition.base_state_snapshot_digest)
        self.assertEqual(authorizations, self._rows(
            "SELECT id FROM goal_authorizations WHERE project_id=?", self.project_id,
        ))

    def test_replan_rejects_a_cancelled_workflow(self) -> None:
        self._blocked_needs_revision()
        self.application.cancel(self.project_id, reason="사용자 취소")
        self._assert_rejected("WORKFLOW_CANCELLED")

    def test_replan_inside_a_role_scope_is_denied_without_mutation(self) -> None:
        self._blocked_needs_revision()
        before = self._ledger()
        with role_execution_scope("plan_expander"):
            with self.assertRaisesRegex(CoreCapabilityError, "^CORE_CAPABILITY_DENIED"):
                self.application.replan(self.project_id, rationale="역할 scope 재진입")
        self.assertEqual(before, self._ledger())

    # --- AC5: 원장 파생 한도 --------------------------------------------------------

    def test_one_manual_retry_after_the_automatic_replan_then_the_same_failure_limit(self) -> None:
        _task_id, attempt, first_blocked = self._blocked_needs_revision()
        retry = self._replan()
        # 재시도 후보도 수정 가능한 finding으로 다시 차단된다. 새 후보의 blocker만 보고한다.
        self.runner.responses.setdefault("plan_refiner", []).append({
            "action": "detail_revision",
            "rationale": "finding에 맞춰 검사 statement를 고친다.",
            "evidence_refs": ["artifact:plan_contract"],
            "plan": _expansion("app.py 값을 다시 검사한다."),
            "skeleton": None,
        })
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(_finding_review())
        second = self._until_blocked()
        second_blocked = self._plans()[-1][0]
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", second.blocker_code)
        self.assertIn(second_blocked, second.detail)
        self.assertNotIn(first_blocked, second.detail)
        status = self.application.status(self.project_id)["recovery"]
        self.assertEqual(second.detail[:2000], status["next_action"]["detail"])
        self.assertEqual(retry["assessment_id"], status["limits"]["assessment_id"])

        message = self._assert_rejected("SAME_FAILURE_REPLAN_LIMIT")
        self.assertIn("2회", message)
        # 원장에서 다시 센 값과 같다. 사용자는 fingerprint·count를 넣지 않았다.
        rows = [json.loads(payload) for _id, payload in self._ledger()["assessments"]]
        fingerprint = rows[0]["failure_fingerprint"]
        self.assertEqual(
            [(1, 1), (2, 2)],
            [(item["same_failure_replan_count"], item["goal_replan_count"]) for item in rows],
        )
        self.assertEqual({fingerprint}, {item["failure_fingerprint"] for item in rows})
        self.assertEqual(
            replan_retry_assessment_id(attempt, fingerprint, first_blocked), rows[1]["assessment_id"],
        )

    def test_goal_replan_limit_is_ledger_derived(self) -> None:
        self.runner.responses["plan_expander"][0]["tasks"][0]["recovery"]["max_goal_replans"] = 1
        task_id = self._prepare()
        policy = GoalOperatingPolicy(max_goal_replans=1)
        target = self.authority.authorization_target(self.project_id, operating_policy=policy)
        self.authority.authorize(self.project_id, target=target, source="g1b-test", operating_policy=policy)
        attempt, binding = self._dispatch_worker(task_id)
        self._fail_worker(attempt, binding, response="Task 계약이 현재 대상과 맞지 않습니다.",
                          error_code="TASK_CONTRACT_INVALID")
        self._queue_replan(review=_finding_review(), statement="app.py 값을 검사한다.", max_goal_replans=1)
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", self._until_blocked().blocker_code)
        self.assertIn("1회", self._assert_rejected("GOAL_REPLAN_LIMIT"))

    # --- AC4: 재시작과 동시 run_once --------------------------------------------------

    def _restarted_application(self) -> EngineApplication:
        service = EngineService(SQLiteEngineLedger(
            self.service.ledger.path, artifact_root=self.service.ledger.artifact_root,
        ))
        application = EngineApplication(
            service, runtime=self.runtime, role_configuration=fm08._roles(),
            structured_runner=self.runner, governance=ALLOW_ALL,
        )
        self.supervisors.append(application.supervisor)
        return application

    def test_restart_between_replan_and_run_once_reserves_the_retry_job_once(self) -> None:
        self._blocked_needs_revision()
        retry = self._replan()
        self._queue_refinement(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
        )
        restarted = self._restarted_application()
        outcome = None
        for _ in range(80):
            outcome = restarted.run_once(self.project_id)
            if outcome.action is RunOnceAction.RECOVERED:
                break
            self.assertIn(outcome.action, {RunOnceAction.DISPATCHED, RunOnceAction.OBSERVED}, outcome)
            time.sleep(0.01)
        self.assertEqual(RunOnceAction.RECOVERED, outcome.action, outcome)
        self.assertEqual(
            [f"replanning:{retry['assessment_id']}"],
            [key for key, _status in self._jobs() if key.endswith(retry["assessment_id"])],
        )
        self.assertEqual(3, self._plan(self._active_plan_id()).revision_no)

    def test_concurrent_run_once_after_replan_reserves_one_job_and_activates_once(self) -> None:
        self._blocked_needs_revision()
        retry = self._replan()
        self._queue_refinement(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
        )
        other = self._restarted_application()
        errors: list[BaseException] = []

        def drive(application):
            try:
                for _ in range(60):
                    outcome = application.run_once(self.project_id)
                    if outcome.action is RunOnceAction.BLOCKED:
                        errors.append(AssertionError(outcome))
                        return
                    if self._plan(self._active_plan_id()).revision_no == 3:
                        return
                    time.sleep(0.01)
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                errors.append(error)

        threads = [threading.Thread(target=drive, args=(item,)) for item in (self.application, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual([], errors)
        self.assertEqual(
            [(f"replanning:{retry['assessment_id']}", "consumed")],
            [row for row in self._jobs() if row[0].endswith(retry["assessment_id"])],
        )
        active = self._active_plan_id()
        self.assertEqual(3, self._plan(active).revision_no)
        self.assertEqual(1, len(self._rows(
            "SELECT id FROM plan_activations WHERE plan_revision_id=?", active,
        )))
        self.assertEqual(1, [call.role for call in self.runner.calls].count("plan_refiner"))

    # --- AC1: recovery-provider availability predicate 공유 ---------------------------

    def test_provider_availability_is_one_predicate_for_status_and_run_once(self) -> None:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        bare = EngineApplication(self.service, runtime=self.runtime, governance=ALLOW_ALL)
        self.supervisors.append(bare.supervisor)
        self.assertFalse(bare.recovery_provider_available())
        before = self._ledger()
        recovery = bare.status(self.project_id)["recovery"]
        self.assertEqual(before, self._ledger())
        self.assertEqual("user_decision_required", recovery["state"])
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", recovery["next_action"]["blocker_code"])
        self.assertIn("--role-config", recovery["next_action"]["detail"])
        blocked = bare.run_once(self.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, "REPLAN_PROVIDER_REQUIRED"), (blocked.action, blocked.blocker_code))
        self.assertEqual(blocked.detail, recovery["next_action"]["detail"])
        self.assertEqual(before, self._ledger())

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, main([
                "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                "status", "--project-id", self.project_id,
            ]))
        cli_recovery = json.loads(output.getvalue())["recovery"]
        self.assertEqual(recovery["next_action"], cli_recovery["next_action"])

        self.assertTrue(self.application.recovery_provider_available())
        pending = self.application.status(self.project_id)["recovery"]
        self.assertEqual("automatic_pending", pending["state"])
        self._queue_replan(review=_clean_review(), statement="app.py 값을 검사한다.")
        self.assertEqual(RunOnceAction.DISPATCHED, self.application.run_once(self.project_id).action)

    # --- AC2: 재계획 job_error parity ------------------------------------------------

    def test_replanning_job_error_status_matches_run_once_and_replan_is_rejected(self) -> None:
        self._fail_contract()
        real_run = self.runner.run

        def fail_after_terminal(request, *, validator=None):
            if request.role == "plan_expander":
                notify_active_runtime_job_progress({
                    "event": "role_terminal_observed",
                    "terminal_observation": {"terminal_status": "completed"},
                })
                raise RuntimeError("역할 terminal 뒤 local 결과 처리 실패")
            return real_run(request, validator=validator)

        self.runner.run = fail_after_terminal
        blocked = self._until_blocked()
        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", blocked.blocker_code, blocked)
        job_id = self._rows(
            "SELECT id FROM runtime_jobs WHERE project_id=? AND kind='replanning'", self.project_id,
        )[0][0]
        for text in (job_id, "error_type=RuntimeError", "terminal_observed=true",
                     REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE):
            self.assertIn(text, blocked.detail)
        before = self._ledger()
        for _ in range(2):
            again = self.application.run_once(self.project_id)
            self.assertEqual((blocked.blocker_code, blocked.detail), (again.blocker_code, again.detail))
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual(before, self._ledger())
        self.assertEqual("observe_first_required", recovery["state"])
        self.assertEqual(
            {"mode": "observe_first", "blocker_code": "EXTERNAL_EFFECT_UNKNOWN",
             "suggested_repair_action": "wait_external", "checkpoint_required": True,
             "detail": blocked.detail[:2000]},
            recovery["next_action"],
        )
        self.assertIn(REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE, self._assert_rejected("REPLAN_RETRY_OBSERVE_FIRST"))

    # --- CLI facade와 공개 표면 --------------------------------------------------------

    def _cli(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([
                "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                *arguments,
            ])
        return code, json.loads(output.getvalue())

    def test_cli_replan_records_once_and_rejects_with_a_typed_code(self) -> None:
        self._blocked_needs_revision()
        code, recorded = self._cli("replan", "--project-id", self.project_id, "--rationale", "CLI 재시도")
        self.assertEqual(0, code)
        self.assertTrue(recorded["recorded"])
        before = self._ledger()
        code, again = self._cli("replan", "--project-id", self.project_id, "--rationale", "CLI 재시도")
        self.assertEqual((0, False, recorded["assessment_id"]), (code, again["recorded"], again["assessment_id"]))
        self.assertEqual(before, self._ledger())

        self.application.cancel(self.project_id, reason="사용자 취소")
        before = self._ledger()
        code, rejected = self._cli("replan", "--project-id", self.project_id, "--rationale", "취소 뒤")
        self.assertEqual((2, "WORKFLOW_CANCELLED"), (code, rejected["error_code"]))
        self.assertEqual(before, self._ledger())

    def test_public_replan_surface_accepts_no_internal_identifiers(self) -> None:
        self.assertEqual(
            ["self", "project_id", "rationale"],
            list(inspect.signature(EngineApplication.replan).parameters),
        )
        parser = build_parser()
        parsed = parser.parse_args(["replan", "--project-id", "project_" + "a" * 32, "--rationale", "r"])
        self.assertEqual(("replan", "r"), (parsed.command, parsed.rationale))
        for extra in (("--assessment-id", "x"), ("--live",), ("--outcome-file", "x"), ("--plan-revision-id", "x")):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(["replan", "--project-id", "p", "--rationale", "r", *extra])


class ReplanRejectionIntegrityTests(unittest.TestCase):
    """G1 harness(EngineDispatcher + 주입 후보)로 무결성 차단 두 가지의 replan 거절을 본다."""

    setUp = g1.automatic.AutomaticRecoveryIntegrationTests.setUp
    prepared = g1.automatic.AutomaticRecoveryIntegrationTests.prepared
    _failed_attempt = g1.automatic.AutomaticRecoveryIntegrationTests._failed_attempt
    _finish_runtime_job_tick = g1.automatic.AutomaticRecoveryIntegrationTests._finish_runtime_job_tick
    _evaluation = g1.ReplanCandidateTests._evaluation
    _to_replan = g1.ReplanCandidateTests._to_replan
    _provider = staticmethod(g1.ReplanCandidateTests._provider)
    _ledger = staticmethod(g1.ReplanCandidateTests._ledger)

    def _assert_rejected(self, prepared, code: str) -> None:
        before = self._ledger(prepared.service, prepared.project_id)
        with self.assertRaises(EngineServiceError) as raised:
            prepared.service.request_subgraph_replan_retry(prepared.project_id, rationale="거절 확인")
        self.assertTrue(str(raised.exception).startswith(f"{code}:"), str(raised.exception))
        self.assertEqual(before, self._ledger(prepared.service, prepared.project_id))

    def test_binding_mismatch_is_rejected_for_diagnosis(self) -> None:
        prepared, runtime = self.prepared(name="g1b-binding")
        evaluation = self._evaluation(prepared)
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        prepared.service.register_plan_evaluation(self._evaluation(prepared, reviewer_finding=True))
        blocked = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual("REPLAN_CANDIDATE_BINDING_MISMATCH", blocked.blocker_code)
        self._assert_rejected(prepared, "REPLAN_RETRY_BINDING_MISMATCH")

    def test_activation_blocked_is_rejected_with_the_real_next_step(self) -> None:
        prepared, runtime = self.prepared(name="g1b-activation")
        from flowmarshal.engine.domain import derive_candidate_decision, new_id
        from flowmarshal.engine.planning import ExpandedPlanEvaluation, plan_review_evidence_catalog
        from tests.engine_helpers import clean_review

        base = self._evaluation(prepared)
        # 같은 plan_id 계보가 아닌 후보는 등록은 되지만 active Plan을 교체할 수 없다.
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
        evaluation = ExpandedPlanEvaluation(
            plan=plan, deterministic_findings=(), semantic_submissions=(review,),
            decision=derive_candidate_decision(
                candidate_digest=plan.activation_digest, findings=(), ratings=review.ratings,
            ),
        )
        dispatcher, _calls = self._to_replan(prepared, runtime, evaluation)
        blocked = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual("REPLAN_CANDIDATE_ACTIVATION_BLOCKED", blocked.blocker_code, blocked)
        self._assert_rejected(prepared, "REPLAN_RETRY_ACTIVATION_BLOCKED")


def _two_task_responses() -> dict[str, list[dict]]:
    """앞 Task(app.py) 완료 뒤 뒤 Task(other.py)가 실패하는 직렬 두 Task Plan."""

    responses = copy.deepcopy(fm08._responses())
    skeleton = responses["skeleton_generator"][0]["candidates"][0]
    skeleton["approach"]["change_shape"] = "serial-two-task"
    skeleton["tasks"] = [
        {"task_ref": "task_one", "kind": "change", "objective": "app.py 값을 2로 바꾼다.",
         "contributes_to": ["ac_001"], "produces": ["result:app_value"], "consumes": ["input:request"]},
        {"task_ref": "task_two", "kind": "change", "objective": "other.py 값을 2로 바꾼다.",
         "contributes_to": ["ac_001"], "produces": ["result:other_value"], "consumes": ["result:app_value"]},
    ]
    skeleton["dependencies"] = [{
        "producer_task_ref": "task_one", "consumer_task_ref": "task_two", "dependency_type": "data",
        "produces": ["result:app_value"], "consumes": ["result:app_value"],
    }]
    skeleton["goal_coverage"] = [{"criterion_id": "ac_001", "task_refs": ["task_one", "task_two"]}]
    responses["plan_expander"] = [_two_task_expansion("other.py 값을 검사한다.")]
    return responses


def _two_task_expansion(second_statement: str) -> dict:
    def task(ref: str, objective: str, produces: str, consumes: str, validation_id: str, statement: str):
        return {
            "task_ref": ref, "kind": "change", "objective": objective, "goal_criterion_refs": ["ac_001"],
            "produces": [produces], "consumes": [consumes], "acceptance_criteria": ["Task 검사가 PASS다."],
            "validations": [{"validation_id": validation_id, "statement": statement,
                             "method": "deterministic", "required_evidence_kinds": ["test"]}],
            "risk_level": "low",
            "recovery": {"retryable_failure_classes": ["implementation", "context", "task_contract", "dependency"]},
        }

    return {
        "tasks": [
            task("task_one", "app.py 값을 2로 바꾼다.", "result:app_value", "input:request",
                 "validation_one", "app.py의 값이 2인지 실행 검사한다."),
            task("task_two", "other.py 값을 2로 바꾼다.", "result:other_value", "result:app_value",
                 "validation_two", second_statement),
        ],
        "dependencies": [{"producer_task_ref": "task_one", "consumer_task_ref": "task_two",
                          "dependency_type": "data", "products": ["result:app_value"]}],
        "goal_coverage": [{"criterion_id": "ac_001", "task_refs": ["task_one", "task_two"],
                           "validation_ids": ["validation_one", "validation_two", "validation_goal"]}],
        "integration_validations": [{
            "validation_id": "validation_goal",
            "statement": "Task와 분리된 Validator가 최종 파일과 evidence를 검토한다.",
            "criterion_refs": ["ac_001"], "method": "semantic", "required_evidence_kinds": ["model_review"],
        }],
    }


class CompletedTaskLineageRegressionTests(_ReplanHarness, unittest.TestCase):
    """C-1 회귀: 앞 Task 완료의 강제 재관측 뒤에도 재계획이 current State로 후보를 만든다."""

    def setUp(self) -> None:
        _ReplanHarness.setUp(self)
        self.other_file = self.root / "other.py"
        self.other_file.write_text("value = 1\n", encoding="utf-8")
        self.runner.responses.clear()
        self.runner.responses.update(_two_task_responses())

    def _queue_task(self, task_id: str, path: str, validation_id: str) -> None:
        target = self.root / path
        self.runner.responses.setdefault("execution_preparation", []).append({
            "proposal": {
                "task_id": task_id,
                "context_needs": [{"need_id": "source", "description": f"변경 대상 {path} 본문이 필요하다.",
                                   "path_hints": [path]}],
                "resolved_targets": [{"target_ref": "target", "path": path,
                                      "expected_content_digest": sha256_bytes(target.read_bytes()),
                                      "access": "write"}],
                "actions": [{"action_ref": "edit", "kind": "edit", "description": f"{path} 값을 2로 바꾼다."}],
                "validation_steps": [{
                    "validation_id": validation_id,
                    "argv": [fm08.sys.executable, "-c",
                             f"from pathlib import Path; assert Path('{path}').read_text(encoding='utf-8') "
                             "== 'value = 2\\n'"],
                    "working_directory": str(self.root), "timeout_seconds": 30,
                    "expected_exit_codes": [0], "artifact_paths": [path],
                }],
                "timeout_seconds": 60, "context_token_budget": 12000,
                "idempotency_hint": f"g1b-{validation_id}",
            },
            "context_request": None,
        })

    def _task_ids(self) -> dict[str, str]:
        plan = self._plan(self._active_plan_id())
        return {task.task_ref: task.task_id for task in plan.definition.tasks}

    def _complete_first_and_fail_second(self) -> tuple[str, PlanContractRevision]:
        prepared = self.application.prepare(
            self.project_id, source_request="app.py와 other.py의 value를 차례로 2로 바꾸고 검증해 주세요.",
        )
        self.assertEqual("ready_for_authorization", prepared.status)
        self._authorize()
        tasks = self._task_ids()
        self._queue_task(tasks["task_one"], "app.py", "validation_one")
        self._run_until(RunOnceAction.MATERIALIZED)
        first = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, first.action, first)
        binding = self._worker_binding(first.attempt_id)
        self.app_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(binding.thread_id, response="first task complete")
        self._run_until(RunOnceAction.VALIDATED)

        self._queue_task(tasks["task_two"], "other.py", "validation_two")
        self._run_until(RunOnceAction.MATERIALIZED)
        second = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, second.action, second)
        self._fail_worker(second.attempt_id, self._worker_binding(second.attempt_id),
                          response="Task 계약이 현재 대상과 맞지 않습니다.", error_code="TASK_CONTRACT_INVALID")
        active = self._plan(self._active_plan_id())
        self.assertEqual(
            "completed",
            self._rows("SELECT status FROM task_contracts WHERE id=?", tasks["task_one"])[0][0],
        )
        # 앞 Task 완료가 State를 강제로 재관측해 active Plan의 base State는 더는 current가 아니다.
        self.assertNotEqual(self._current_state_digest(), active.definition.base_state_snapshot_digest)
        return second.attempt_id, active

    def _assert_registrable_candidate(self, *, revision_no: int, supersedes: str, original) -> None:
        active_id = self._active_plan_id()
        active = self._plan(active_id)
        self.assertEqual((original.plan_id, revision_no, supersedes),
                         (active.plan_id, active.revision_no, active.supersedes_plan_revision_id))
        self.assertEqual(self._current_state_digest(), active.definition.base_state_snapshot_digest)
        self.assertEqual(["task_one", "task_two"], [task.task_ref for task in active.definition.tasks])
        # 실패 subgraph 밖 앞 Task의 계약은 active Plan 원문이고 완료가 재사용된다.
        self.assertEqual(
            original.definition.tasks[0].model_dump(exclude={"task_id"}),
            active.definition.tasks[0].model_dump(exclude={"task_id"}),
        )
        self.assertEqual(
            "completed",
            self._rows("SELECT status FROM task_contracts WHERE id=?", active.definition.tasks[0].task_id)[0][0],
        )
        self.assertEqual("다시 계획한 other.py 값 검사를 실제 파일로 실행한다.",
                         active.definition.tasks[1].validations[0].statement)

    def test_automatic_subgraph_replan_after_a_completed_task_builds_a_registrable_candidate(self) -> None:
        _attempt, original = self._complete_first_and_fail_second()
        self.runner.responses.setdefault("plan_expander", []).append(
            _two_task_expansion("다시 계획한 other.py 값 검사를 실제 파일로 실행한다."),
        )
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(_clean_review())
        self._run_until(RunOnceAction.RECOVERED)
        self._run_until(RunOnceAction.RECOVERED, limit=60)
        self._assert_registrable_candidate(revision_no=2, supersedes=original.plan_revision_id, original=original)

        second = self._task_ids()["task_two"]
        self._queue_task(second, "other.py", "validation_two")
        self._run_until(RunOnceAction.MATERIALIZED)
        dispatched = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        binding = self._worker_binding(dispatched.attempt_id)
        self.other_file.write_text("value = 2\n", encoding="utf-8")
        self.runtime.complete(binding.thread_id, response="replanned second task complete")
        self._run_until(RunOnceAction.VALIDATED)
        self._queue_goal_validation(second)
        completed = self._run_until(RunOnceAction.COMPLETED)
        if completed.goal_verdict_id is None:
            completed = self._run_until(RunOnceAction.COMPLETED)
        report = self.application.final_report(self.project_id, goal_verdict_id=completed.goal_verdict_id)
        self.assertEqual("satisfied", report.verdict.status.value)

    def test_stale_retry_after_a_completed_task_builds_a_registrable_candidate(self) -> None:
        _attempt, original = self._complete_first_and_fail_second()
        self.runner.responses.setdefault("plan_expander", []).append(
            _two_task_expansion("처음 다시 계획한 other.py 값 검사."),
        )
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(_clean_review())
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
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", blocked.blocker_code, blocked)
        stale_id = self._plans()[-1][0]

        self.assertEqual("candidate_state_stale", self._replan("State 재관측 뒤 다시 계획")["basis_kind"])
        self.runner.responses.setdefault("plan_expander", []).append(
            _two_task_expansion("다시 계획한 other.py 값 검사를 실제 파일로 실행한다."),
        )
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(_clean_review())
        self._until_recovered_activation()
        self._assert_registrable_candidate(revision_no=3, supersedes=stale_id, original=original)


class ReplanBasisAndRoleInputTests(_ReplanHarness, unittest.TestCase):
    """typed basis의 시간 조건, 수정 역할의 비수정 응답, 상세화 역할 입력 보존."""

    def test_basis_older_than_the_last_recovery_checkpoint_is_not_new_evidence(self) -> None:
        _task_id, attempt, _blocked = self._blocked_needs_revision()
        # 차단 후보 decision 뒤에 다른 recovery checkpoint가 기록되면 그 decision은 더는 새 근거가 아니다.
        from flowmarshal.canonical import sha256_digest
        from flowmarshal.engine.domain import FailureClass, RepairAction, new_id

        self.service.record_recovery_assessment(self.project_id, RecoveryAssessment(
            assessment_id=new_id("recovery_assessment"),
            attempt_id=attempt,
            failure_class=FailureClass.IMPLEMENTATION,
            action=RepairAction.TASK_REPAIR,
            rationale="뒤에 기록된 다른 recovery checkpoint",
            failure_fingerprint=sha256_digest("g1b-other-failure"),
            same_failure_replan_count=0,
            goal_replan_count=1,
        ))
        self.assertIn("새 evidence", self._assert_rejected("NEW_RECOVERY_EVIDENCE_REQUIRED"))

    def _roles_called(self) -> list[str]:
        return [call.role for call in self.runner.calls]

    def _assert_refiner_decline_replays(self, action: str) -> None:
        """refine이 plan 없이 응답하면 expand fallback·재호출 없이 같은 NOT_ADMISSIBLE을 재생한다."""

        _task_id, _attempt, blocked_id = self._blocked_needs_revision()
        retry = self._replan()
        # fallback이 생기면 소비될 plan_expander·reviewer 응답을 미리 둔다. 남아 있어야 한다.
        self._queue_replan(review=_clean_review(), statement="fallback이 쓰면 안 되는 상세화")
        called = len(self.runner.calls)
        self.runner.responses.setdefault("plan_refiner", []).append({
            "action": action,
            "rationale": "finding을 해소할 근거가 부족하다.",
            "evidence_refs": ["artifact:plan_contract"],
            "plan": None,
            "skeleton": None,
        })
        # 재시도 job 예약·관측 뒤 첫 결정적 전이가 같은 후보의 차단이어야 한다(활성화 RECOVERED가 아니다).
        blocked = self._settle()
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action, blocked)
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code)
        self.assertIn(blocked_id, blocked.detail)
        self.assertEqual(["plan_refiner"], self._roles_called()[called:])
        self.assertEqual(1, len(self.runner.responses["plan_expander"]))
        self.assertEqual(1, len(self.runner.responses[RECOVERY_PLAN_REVIEWER_ROLE]))
        self.assertEqual(2, len(self._plans()))
        self.assertEqual((f"replanning:{retry['assessment_id']}", "consumed"), self._jobs()[-1])

        # 이후 tick도 역할을 다시 부르지 않고 plan 원장 변경 없이 같은 차단을 재생한다.
        before = self._ledger()
        for _ in range(3):
            again = self.application.run_once(self.project_id)
            self.assertEqual((blocked.blocker_code, blocked.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())
        self.assertEqual(["plan_refiner"], self._roles_called()[called:])
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual(blocked.detail[:2000], recovery["next_action"]["detail"])
        # 그 재시도는 소비된 상태로 남는다. 같은 후보로 다시 부르면 멱등이다.
        again = self._replan("다시 요청")
        self.assertEqual((retry["assessment_id"], False), (again["assessment_id"], again["recorded"]))
        self.assertEqual(before, self._ledger())
        self.assertEqual(["plan_refiner"], self._roles_called()[called:])

    def test_refiner_without_a_revision_keeps_the_same_typed_block(self) -> None:
        self._assert_refiner_decline_replays("unresolved")

    def test_disputed_refinement_keeps_the_same_typed_block_without_fallback(self) -> None:
        self._assert_refiner_decline_replays("disputed")

    def test_state_change_after_the_command_blocks_the_refined_candidate_as_stale(self) -> None:
        """명령 뒤 State가 바뀌면 refine 후보는 옛 State에 결속되고 활성화 전에 STALE로 멈춘다."""

        _task_id, _attempt, blocked_id = self._blocked_needs_revision()
        original_id = self._active_plan_id()
        blocked = self._plan(blocked_id)
        self.assertEqual("candidate_needs_revision", self._replan()["basis_kind"])
        self.service.reobserve_project(self.project_id, force_state_revision=True)
        self.assertNotEqual(self._current_state_digest(), blocked.definition.base_state_snapshot_digest)
        activations = self._rows("SELECT id FROM plan_activations WHERE project_id=?", self.project_id)
        self._queue_refinement(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
        )
        called = len(self.runner.calls)

        stale = self._settle()
        self.assertEqual(RunOnceAction.BLOCKED, stale.action, stale)
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", stale.blocker_code, stale)
        refined_id = self._plans()[-1][0]
        refined = self._plan(refined_id)
        self.assertEqual(
            [(original_id, blocked.plan_id, 1, "active", None),
             (blocked_id, blocked.plan_id, 2, "draft", original_id),
             (refined_id, blocked.plan_id, 3, "ready", blocked_id)],
            self._plans(),
        )
        # refine 후보는 차단 후보가 결속한 옛 State에 결속된다. 차단 후보도 이전 Plan도 다시 활성화하지 않는다.
        self.assertEqual(blocked.definition.base_state_snapshot_digest, refined.definition.base_state_snapshot_digest)
        self.assertNotEqual(self._current_state_digest(), refined.definition.base_state_snapshot_digest)
        self.assertEqual(original_id, self._active_plan_id())
        self.assertEqual(activations, self._rows(
            "SELECT id FROM plan_activations WHERE project_id=?", self.project_id,
        ))
        self.assertEqual(["plan_refiner", RECOVERY_PLAN_REVIEWER_ROLE], self._roles_called()[called:])
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual("PLAN_STATE_SNAPSHOT_STALE", recovery["next_action"]["blocker_code"])
        self.assertEqual(stale.detail[:2000], recovery["next_action"]["detail"])
        before = self._ledger()
        again = self.application.run_once(self.project_id)
        self.assertEqual((stale.blocker_code, stale.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())

    def test_recovery_expansion_role_input_is_the_same_without_previous_plan(self) -> None:
        """previous_plan은 역할 payload에 들어가지 않으므로 C-1 수정은 v1 상세화 입력을 바꾸지 않는다."""

        from flowmarshal.engine.planner_roles import PlanExpanderAdapter, RuleBasedTaskAssigner
        from tests.engine_helpers import assignment
        from tests.engine_inspection_helpers import InspectionScriptedRunner

        self._prepare()
        self._authorize()
        active = self._plan(self._active_plan_id())
        from flowmarshal.engine.domain import PlanSkeletonCandidate

        with self.service.ledger.read() as connection:
            skeleton = PlanSkeletonCandidate.model_validate_json(connection.execute(
                "SELECT payload_json FROM skeleton_candidates WHERE candidate_digest=?",
                (active.definition.source_skeleton_digest,),
            ).fetchone()[0])
        goal = self.service.load_active_goal(self.project_id)
        state = self.service.load_current_state(self.project_id, goal.definition_digest)
        project_map = self.service.load_current_project_map(self.project_id)
        models = self.runtime.list_models()
        runner = InspectionScriptedRunner({"plan_expander": [_expansion("app.py 값을 검사한다.")] * 2})
        adapter = PlanExpanderAdapter(
            runner, RuleBasedTaskAssigner(assignment(), assignment(), assignment()),
            model="worker", effort="medium", inventory_digest=models.inventory_digest,
            inventory=models, cwd=self.root,
        )
        with_previous = adapter.expand(
            candidate=skeleton, goal=goal, state=state, project_map=project_map, previous_plan=active,
        )
        without_previous = adapter.expand(candidate=skeleton, goal=goal, state=state, project_map=project_map)
        self.assertEqual(runner.calls[0].request_digest, runner.calls[1].request_digest)
        self.assertEqual(with_previous.definition.model_dump(exclude={"tasks", "goal_coverage", "dependencies"}),
                         without_previous.definition.model_dump(exclude={"tasks", "goal_coverage", "dependencies"}))
        self.assertEqual((active.plan_id, 2), (with_previous.plan_id, with_previous.revision_no))
        self.assertEqual((1, None), (without_previous.revision_no, without_previous.supersedes_plan_revision_id))


class ProviderlessHeadJobTests(_ReplanHarness, unittest.TestCase):
    """provider 없는 인스턴스는 기존 head replanning job을 새 job·역할 호출 없이 관측·재생한다."""

    def _providerless(self, *, shared_supervisor: bool) -> EngineApplication:
        application = EngineApplication(
            self.service, runtime=self.runtime, governance=ALLOW_ALL,
            supervisor=self.application.supervisor if shared_supervisor else None,
        )
        if not shared_supervisor:
            self.supervisors.append(application.supervisor)
        self.assertFalse(application.recovery_provider_available())
        return application

    def _job_ids(self) -> list[tuple]:
        return self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id)

    def _provider_call_roles(self) -> list[str]:
        return [role for (role,) in self._rows(
            "SELECT role FROM provider_calls WHERE project_id=? ORDER BY rowid", self.project_id,
        )]

    def test_status_follows_the_provider_predicate_not_the_role_configuration(self) -> None:
        """M10: status는 role_configuration 필드가 아니라 run_once와 같은 predicate를 따른다."""

        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        # 역할 설정이 있어도 predicate가 거짓이면 run_once처럼 provider 부재를 표시한다.
        self.assertIsNotNone(self.application.role_configuration)
        self.application.recovery_provider_available = lambda: False
        before = self._ledger()
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", recovery["next_action"]["blocker_code"])
        blocked = self.application.run_once(self.project_id)
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", blocked.blocker_code)
        self.assertEqual(blocked.detail, recovery["next_action"]["detail"])
        self.assertEqual(before, self._ledger())
        # 역할 설정이 없어도 predicate가 참이면 status는 provider 부재로 판정하지 않는다.
        bare = self._providerless(shared_supervisor=False)
        bare.recovery_provider_available = lambda: True
        self.assertIsNone(bare.role_configuration)
        self.assertEqual("automatic_pending", bare.status(self.project_id)["recovery"]["state"])
        self.assertEqual(before, self._ledger())

    def test_running_then_terminal_head_job_is_observed_and_consumed_without_a_provider(self) -> None:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        self._queue_replan(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다.",
        )
        gate = threading.Event()
        # 단언이 먼저 실패해도 gate에서 기다리는 worker가 정리 전에 끝나게 한다.
        self.addCleanup(gate.set)
        entered = threading.Event()
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_expander":
                entered.set()
                gate.wait(10)
            return real_run(request, validator=validator)

        self.runner.run = gated
        before_schedule = self._provider_call_roles()
        scheduled = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        job_id = scheduled.runtime_job_id
        bare = self._providerless(shared_supervisor=True)
        jobs = self._job_ids()
        # BudgetedRoleRunner는 provider_call을 예약한 뒤 runner를 부른다. runner에 들어온 뒤에 사본을 떠서
        # job thread의 예약과 경쟁하지 않게 한다. 그 시점까지의 새 예약은 기존 job의 plan_expander 하나다.
        self.assertTrue(entered.wait(10))
        provider_calls = self._provider_call_roles()
        self.assertEqual(["plan_expander"], provider_calls[len(before_schedule):])
        called = len(self.runner.calls)

        running = bare.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, running.action, running)
        self.assertIn("replanning/running", running.detail)
        recovery = bare.status(self.project_id)["recovery"]
        # AC14 (c) 교체(Codex 동의, J1 (A)): 같은 supervisor의 owner가 살아 있는 pre-binding은 판정 1이다.
        self.assertEqual(("none", "none", None), (recovery["state"], recovery["next_action"]["mode"],
                         recovery["next_action"]["blocker_code"]), recovery)
        self.assertIn("owner 실행 중", recovery["next_action"]["detail"])
        self.assertEqual(called, len(self.runner.calls))

        gate.set()
        self.application.supervisor._workers[job_id].join(10)
        terminal = bare.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, terminal.action, terminal)
        self.assertIn("replanning/provider_terminal", terminal.detail)
        self.assertEqual("automatic_pending", bare.status(self.project_id)["recovery"]["state"])
        # M-14 D1(AC21) 재작성: bare는 기존 expand job의 결과를 provider 없이 소비하고, 새 review job을
        # 예약해야 하는 시점에 provider 부재로 멈춘다.
        stopped = bare.run_once(self.project_id)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "REPLAN_PROVIDER_REQUIRED", REPLAN_PROVIDER_REQUIRED_DETAIL),
            (stopped.action, stopped.blocker_code, stopped.detail), stopped,
        )
        recovery = bare.status(self.project_id)["recovery"]
        self.assertEqual(("user_decision_required", "REPLAN_PROVIDER_REQUIRED"),
                         (recovery["state"], recovery["next_action"]["blocker_code"]), recovery)

        # bare 구간: 새 job 행 없음, 역할 호출은 기존 expand job의 expander 하나, 새 provider_call 없음.
        self.assertEqual(jobs, self._job_ids())
        self.assertEqual(["plan_expander"], [call.role for call in self.runner.calls][called:])
        self.assertEqual([], self._provider_call_roles()[len(provider_calls):])

        # provider 있는 인스턴스가 review job을 예약해 끝낸다. expander는 다시 부르지 않는다.
        recovered = self._until_recovered_activation()
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action, recovered)
        self.assertEqual(jobs, self._job_ids()[:len(jobs)])
        expand_key = self._jobs()[-2][0]
        self.assertEqual([(expand_key, "consumed"), (f"{expand_key}:review", "consumed")], self._jobs()[-2:])
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE],
                         [call.role for call in self.runner.calls][called:])
        self.assertEqual([RECOVERY_PLAN_REVIEWER_ROLE], self._provider_call_roles()[len(provider_calls):])
        self.assertEqual(2, self._plan(self._active_plan_id()).revision_no)
        self.assertEqual("recovered", bare.status(self.project_id)["recovery"]["state"])

    def test_consumed_head_job_is_replayed_instead_of_requiring_a_provider(self) -> None:
        _task_id, _attempt, blocked_id = self._blocked_needs_revision()
        replay = self.application.run_once(self.project_id)
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", replay.blocker_code)
        bare = self._providerless(shared_supervisor=False)
        before = self._ledger()
        called = len(self.runner.calls)
        for _ in range(2):
            outcome = bare.run_once(self.project_id)
            self.assertEqual(
                (RunOnceAction.BLOCKED, replay.blocker_code, replay.detail),
                (outcome.action, outcome.blocker_code, outcome.detail),
            )
        recovery = bare.status(self.project_id)["recovery"]
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", recovery["next_action"]["blocker_code"])
        self.assertEqual(replay.detail[:2000], recovery["next_action"]["detail"])
        self.assertIn(blocked_id, recovery["next_action"]["detail"])
        self.assertEqual(before, self._ledger())
        self.assertEqual(called, len(self.runner.calls))


class ReplanJobStateGuidanceTests(_ReplanHarness, unittest.TestCase):
    """N-5: 소비 전 head job의 replan 거절 안내가 job의 실제 다음 행동과 같다."""

    def _stable_head_job(self) -> str:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        return self._rows(
            "SELECT id FROM recovery_assessments WHERE project_id=?", self.project_id,
        )[0][0]

    def test_collector_lost_head_job_without_a_result_is_not_called_in_progress(self) -> None:
        stable = self._stable_head_job()
        # scripted 응답이 없어 역할이 provider terminal 없이 실패한다. 결과·binding 없는 collector_lost다.
        outcome = None
        for _ in range(20):
            outcome = self.application.run_once(self.project_id)
            if "replanning/collector_lost" in (outcome.detail or ""):
                break
            time.sleep(0.01)
        self.assertIn("replanning/collector_lost", outcome.detail)
        self.assertEqual([(f"replanning:{stable}", "collector_lost")], self._jobs())
        message = self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED")
        self.assertIn("binding 없이 끊겼습니다", message)
        self.assertNotIn(REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE, message)
        self.assertIn("다음 run-once가 owner lock으로 생존을 확인해 한 번 재시작하거나 typed blocker로 멈춥니다", message)
        self.assertNotIn("진행 중", message)
        # AC14 (c) 교체(Codex 동의): 효과 근거 없이 worker 자체 오류로 끝난 job은 표 5행이다.
        # run-once는 관측을 반복하지 않고 매번 원장을 바꾸지 않는 typed 정지로 멈춘다.
        before = self._ledger()
        for _ in range(2):
            again = self.application.run_once(self.project_id)
            self.assertEqual(RunOnceAction.BLOCKED, again.action, again)
            self.assertNotEqual(RunOnceAction.OBSERVED, again.action, again)
            self.assertEqual("RUNTIME_EFFECT_PREFLIGHT_FAILED", again.blocker_code, again)
            self.assertEqual(before, self._ledger())
        self.assertEqual(before, self._ledger())

    def test_cancelled_head_job_is_not_called_in_progress(self) -> None:
        stable = self._stable_head_job()
        self._queue_replan(review=_clean_review(), statement="app.py 값을 검사한다.")
        gate = threading.Event()
        # 단언이 먼저 실패해도 gate에서 기다리는 worker가 정리 전에 끝나게 한다.
        self.addCleanup(gate.set)
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_expander":
                gate.wait(10)
            return real_run(request, validator=validator)

        self.runner.run = gated
        scheduled = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        self.service.cancel_runtime_job(scheduled.runtime_job_id, reason="g1b 취소 안내 확인")
        gate.set()
        self.application.supervisor._workers[scheduled.runtime_job_id].join(10)
        self.assertEqual([(f"replanning:{stable}", "cancelled")], self._jobs())
        message = self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED")
        self.assertIn("취소됐습니다", message)
        self.assertIn(REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE, message)
        self.assertNotIn("진행 중", message)
        # 안내대로 run-once는 취소된 job을 이어 가지 않는다(새 job·후보·활성화 없음).
        before = self._ledger()
        again = self.application.run_once(self.project_id)
        self.assertNotEqual(RunOnceAction.RECOVERED, again.action, again)
        after = self._ledger()
        for name in ("plans", "activations", "jobs", "assessments"):
            self.assertEqual(before[name], after[name], name)


class ConcurrentJobStartTests(_ReplanHarness, unittest.TestCase):
    """B-1: 같은 RuntimeJob을 동시에 잡은 두 supervisor 중 durable claim을 이긴 쪽만 worker를 만든다."""

    def _observation_kinds(self, job_id: str) -> list[str]:
        return [kind for (kind,) in self._rows(
            "SELECT kind FROM runtime_job_observations WHERE job_id=? ORDER BY rowid", job_id,
        )]

    def _hold_both_at_scheduled(self, checkpoint: str) -> None:
        """두 호출자가 모두 SCHEDULED 행을 받은 뒤에야 시작 단계로 넘어가게 한다."""

        barrier = threading.Barrier(2, timeout=10)
        real = self.service.schedule_runtime_job

        def both_scheduled(**kwargs):
            job = real(**kwargs)
            if kwargs["checkpoint_key"] == checkpoint:
                barrier.wait()
            return job

        self.service.schedule_runtime_job = both_scheduled
        self.addCleanup(setattr, self.service, "schedule_runtime_job", real)

    def test_concurrent_run_once_on_the_retry_job_starts_one_worker(self) -> None:
        self._blocked_needs_revision()
        retry = self._replan()["assessment_id"]
        checkpoint = f"replanning:{retry}"
        self._queue_refinement(
            review=_clean_review(), statement="app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다.",
        )
        other = EngineApplication(
            self.service, runtime=self.runtime, role_configuration=fm08._roles(),
            structured_runner=self.runner, governance=ALLOW_ALL,
        )
        self.supervisors.append(other.supervisor)
        applications = (self.application, other)
        # 두 run_once가 모두 job 없음을 보고 schedule에 들어온 뒤에만 예약하게 한다.
        entry = threading.Barrier(2, timeout=10)
        for application in applications:
            real_schedule = application.supervisor.schedule

            def at_entry(*args, _real=real_schedule, **kwargs):
                if kwargs.get("checkpoint_key") == checkpoint:
                    entry.wait()
                return _real(*args, **kwargs)

            application.supervisor.schedule = at_entry
        self._hold_both_at_scheduled(checkpoint)
        gate = threading.Event()
        # 단언이 먼저 실패해도 gate에서 기다리는 worker가 정리 전에 끝나게 한다.
        self.addCleanup(gate.set)
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_refiner":
                gate.wait(10)
            return real_run(request, validator=validator)

        self.runner.run = gated
        targets: list[str] = []
        lock = threading.Lock()
        real_replan = RecoveryPlanProvider.replan

        def counted(provider, **kwargs):
            with lock:
                targets.append(kwargs["assessment"].assessment_id)
            return real_replan(provider, **kwargs)

        outcomes: list[object] = []

        def tick(application):
            try:
                outcomes.append(application.run_once(self.project_id).action)
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                outcomes.append(error)

        with mock.patch.object(RecoveryPlanProvider, "replan", counted):
            threads = [threading.Thread(target=tick, args=(item,)) for item in applications]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual([RunOnceAction.DISPATCHED] * 2, outcomes)
            job_id = self._rows(
                "SELECT id FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                self.project_id, checkpoint,
            )[0][0]
            owners = [item.supervisor for item in applications if job_id in item.supervisor._workers]
            self.assertEqual(1, len(owners))
            self.assertEqual(
                [True, False],
                sorted((job_id in item.supervisor._owned_job_ids for item in applications), reverse=True),
            )
            self.assertEqual("running", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0])
            gate.set()
            owners[0]._workers[job_id].join(10)
            self.assertEqual([retry], targets)

        self._until_recovered_activation()
        kinds = self._observation_kinds(job_id)
        self.assertEqual(1, kinds.count("started"))
        self.assertNotIn("collector_reattached", kinds)
        self.assertNotIn("collector_lost", kinds)
        self.assertEqual(1, [call.role for call in self.runner.calls].count("plan_refiner"))
        self.assertEqual(1, [role for (role,) in self._rows(
            "SELECT role FROM provider_calls WHERE project_id=?", self.project_id,
        )].count("plan_refiner"))
        self.assertEqual(3, self._plan(self._active_plan_id()).revision_no)

    def test_two_supervisors_claiming_one_scheduled_job_start_one_worker(self) -> None:
        """공통 RuntimeJob 경로: 진 쪽은 local 상태·worker를 만들지 않고 close도 이긴 쪽 job을 건드리지 않는다."""

        self._prepare()
        self._authorize()
        supervisors = [RuntimeJobSupervisor(self.service, FakeCodexRuntime(inventory())) for _ in range(2)]
        self.supervisors.extend(supervisors)
        checkpoint = "g1b-b1:common"
        self._hold_both_at_scheduled(checkpoint)
        gate = threading.Event()
        # 단언이 먼저 실패해도 gate에서 기다리는 worker가 정리 전에 끝나게 한다.
        self.addCleanup(gate.set)
        calls: list[int] = []
        lock = threading.Lock()

        def target():
            with lock:
                calls.append(1)
            gate.wait(10)
            return {"probe": "done"}

        jobs: dict[int, object] = {}

        def schedule(index: int) -> None:
            try:
                jobs[index] = supervisors[index].schedule(
                    project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key=checkpoint,
                    request={"probe": 1}, timeout_seconds=60, target=target,
                )
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                jobs[index] = error

        threads = [threading.Thread(target=schedule, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual({RuntimeJobStatus.RUNNING}, {job.status for job in jobs.values()})
        job_id = jobs[0].job_id
        self.assertEqual(job_id, jobs[1].job_id)
        owners = [item for item in supervisors if job_id in item._workers]
        self.assertEqual(1, len(owners))
        loser = next(item for item in supervisors if item is not owners[0])
        for state in (loser._owned_job_ids, loser._result_events, loser._complete_on_return, loser._results):
            self.assertNotIn(job_id, state)
        # 진 쪽이 닫혀도 이긴 쪽이 실행 중인 job을 collector_lost로 만들지 않는다.
        loser.close(timeout_seconds=0.1)
        self.assertEqual("running", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0])

        gate.set()
        owners[0]._workers[job_id].join(10)
        self.assertEqual([1], calls)
        # 수렴: 진 쪽 tick도 durable 결과 checkpoint로 같은 terminal을 관측한다.
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, loser.tick(job_id).status)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owners[0].tick(job_id).status)
        self.assertEqual({"probe": "done"}, self.service.consume_runtime_job(job_id))
        kinds = self._observation_kinds(job_id)
        self.assertEqual((1, 1), (kinds.count("scheduled"), kinds.count("started")))
        self.assertNotIn("collector_reattached", kinds)
        self.assertNotIn("collector_lost", kinds)


    def test_worker_start_failure_after_winning_the_claim_is_observed_as_collector_lost(self) -> None:
        """조건 6: claim을 이긴 직후 worker 시작이 실패하면 기존처럼 소유 supervisor의 tick이 collector_lost로 드러낸다."""

        self._prepare()
        self._authorize()
        owner = RuntimeJobSupervisor(self.service, FakeCodexRuntime(inventory()))
        # 시작되지 않은 thread는 join할 수 없으므로 harness join 목록에 넣지 않고 닫기만 한다.
        self.addCleanup(owner.close, timeout_seconds=0.1)
        calls: list[int] = []
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread start failed")):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                owner.schedule(
                    project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="g1b-b1:start-fail",
                    request={"probe": 1}, timeout_seconds=60, target=lambda: calls.append(1),
                )
        job_id, status = self._rows(
            "SELECT id,status FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
            self.project_id, "g1b-b1:start-fail",
        )[0]
        self.assertEqual("running", status)
        self.assertIn(job_id, owner._owned_job_ids)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, owner.tick(job_id).status)
        self.assertEqual([], calls)
        self.assertIn("collector thread exited without a result", self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'", job_id,
        )[0][0])
        self.assertEqual(1, self._observation_kinds(job_id).count("started"))

    def test_crash_after_winning_the_claim_is_observed_as_a_restart(self) -> None:
        """조건 6: claim 뒤 worker 전에 process가 끝나면 새 supervisor는 durable binding이 없어 collector_lost로 본다."""

        self._prepare()
        self._authorize()
        # AC14 (b) setup 보정: r4 owner가 job 행 예약 전에 만들었을 lock 파일(owner proof)을 먼저 만든다.
        from flowmarshal.engine.runtime import runtime_owner_lock_path

        owner_lock = runtime_owner_lock_path(self.service, self.project_id, "g1b-b1:crash")
        owner_lock.parent.mkdir(parents=True, exist_ok=True)
        owner_lock.touch()
        job = self.service.schedule_runtime_job(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="g1b-b1:crash",
            request={"probe": 2}, absolute_deadline_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
        claimed, won = self.service._claim_runtime_job_start(job.job_id)
        self.assertEqual((RuntimeJobStatus.RUNNING, True), (claimed.status, won))
        again, won_again = self.service._claim_runtime_job_start(job.job_id)
        self.assertEqual((RuntimeJobStatus.RUNNING, False), (again.status, won_again))
        self.assertIs(RuntimeJobStatus.RUNNING, self.service.start_runtime_job(job.job_id).status)
        restarted = RuntimeJobSupervisor(self.service, FakeCodexRuntime(inventory()))
        self.supervisors.append(restarted)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, restarted.tick(job.job_id).status)
        self.assertIn("supervisor restarted before provider binding was durable", self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'", job.job_id,
        )[0][0])
        self.assertEqual(1, self._observation_kinds(job.job_id).count("started"))


class ReplanHeadHelperTests(_ReplanHarness, unittest.TestCase):
    """head helper는 재시도 행이 없으면 stable ID이고 있으면 그 행으로 옮겨 간다."""

    def test_head_is_the_stable_assessment_until_a_retry_row_exists(self) -> None:
        _task_id, attempt, blocked_id = self._blocked_needs_revision()
        fingerprint = json.loads(self._ledger()["assessments"][0][1])["failure_fingerprint"]
        stable = stable_recovery_assessment_id(attempt, fingerprint)
        with self.service.ledger.read() as connection:
            self.assertEqual(stable, current_replan_assessment(
                connection, project_id=self.project_id, attempt_id=attempt, failure_fingerprint=fingerprint,
            ))
        retry = self._replan()["assessment_id"]
        self.assertEqual(replan_retry_assessment_id(attempt, fingerprint, blocked_id), retry)
        self.assertRegex(retry, r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$")
        with self.service.ledger.read() as connection:
            self.assertEqual(retry, current_replan_assessment(
                connection, project_id=self.project_id, attempt_id=attempt, failure_fingerprint=fingerprint,
            ))

    def test_current_replan_head_reads_the_consumed_candidate_and_its_binding(self) -> None:
        from flowmarshal.engine.runtime import current_replan_head

        self.assertIsNone(current_replan_head(self.service, project_id=self.project_id))
        _task_id, attempt, blocked_id = self._blocked_needs_revision()
        head = current_replan_head(self.service, project_id=self.project_id)
        self.assertEqual(
            (attempt, head.stable_assessment_id, "consumed", blocked_id, True, False,
             "REPLAN_CANDIDATE_NOT_ADMISSIBLE"),
            (head.attempt_id, head.assessment_id, head.job_status, head.evaluation.plan.plan_revision_id,
             head.candidate_registered, head.candidate_activated, head.blocker[0]),
        )
        retry = self._replan()["assessment_id"]
        moved = current_replan_head(self.service, project_id=self.project_id)
        self.assertEqual((retry, None, None, None), (moved.assessment_id, moved.job_status, moved.evaluation,
                                                     moved.blocker))


if __name__ == "__main__":
    unittest.main()
