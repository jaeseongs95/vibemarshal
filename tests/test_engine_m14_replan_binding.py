"""M-14 회귀: 재계획 역할 turn마다 RuntimeJob, bound target kind의 owner lease, durable 결과 snapshot.

설계 권위는 research-e1d89c37/m14/decision-m14-design-v5.1.md이고 수용 기준은 task-envelope-m14-v5.1.json의
AC1~AC14·AC17~AC22다. 테스트 이름의 ``ac<n>``이 그 AC다.

- 모든 검사는 층 A다(fake runtime, 같은 process, `_acquire_owner_lease` 테스트 double). 실제 provider(층 B)·
  subprocess CLI(층 C)·E2E(층 D)의 근거가 아니다.
- 제품 경로 검사는 ``structured_runner`` 없이 EngineApplication을 만들어 제품 기본 runner
  (`CodexStructuredRoleRunner`)를 쓴다. provider 대신 `_RoleScriptedRuntime`이 역할 thread의 turn만 대본
  JSON으로 끝낸다. 그래서 제품 runner가 실제 provider에서처럼 thread_created·turn_started(job binding)
  progress를 낸다. Worker turn은 fm08처럼 테스트가 직접 끝낸다.
- supervisor 수준 검사는 합성 Goal·Plan 프로젝트(`_prepare`)에서 RuntimeJob을 직접 세운다.
- 이 파일은 HEAD 2b30044 src와 함께 import·수집돼야 한다. 수정에서 새로 생기는 기호는 module 수준에서
  import하지 않는다. 보존 AC(AC4·AC11·AC12·AC14·AC22, AC18(i))는 새 기호에 기대지 않는다.
- gate·hook·event 대기에는 모두 상한이 있다. HEAD에서 정지 지점·hook에 닿지 못하면 그 시점의 실제 상태가
  실패 메시지에 남는다.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flowmarshal.engine.runtime as runtime_module
import tests.test_engine_fm08_recovery_integration as fm08
import tests.test_engine_g1_replan_candidate as g1
import tests.test_engine_g1b_owner_lease as owner_lease
import tests.test_engine_g1b_recovery_path as g1b
from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.application import ApplicationAuthority, EngineApplication
from flowmarshal.engine.domain import (
    FailureClass,
    FindingSeverity,
    GateName,
    RepairAction,
    ReviewFinding,
    RunOnceAction,
    RuntimeJobKind,
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    ThreadBinding,
    utc_now,
)
from flowmarshal.engine.ledger import EngineLedgerError, SQLiteEngineLedger
from flowmarshal.engine.operations import ExternalOperationUnknown
from flowmarshal.engine.recovery_planning import RECOVERY_PLAN_REVIEWER_ROLE, RecoveryPlanProvider
from flowmarshal.engine.roles import CodexStructuredRoleRunner
from flowmarshal.engine.runtime import (
    OWNER_PROOF_MISSING_DETAIL,
    REPLAN_PROVIDER_REQUIRED_DETAIL,
    FakeCodexRuntime,
    OwnerLockState,
    RuntimeJobSupervisor,
    RuntimeOperationReceipt,
    RuntimePolicyError,
    active_runtime_job_id,
    notify_active_runtime_job_progress,
    probe_owner_lock,
    runtime_owner_lock_path,
)
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.engine_helpers import inventory, profile
from tests.engine_inspection_helpers import InspectionScriptedRunner, inspection_fixture
from tests.fixtures.engine.governance.allow import ALLOW_ALL


#: worker 종료·정지 지점·hook 도달을 기다리는 상한. 넘으면 멈추지 않고 테스트 실패로 드러낸다.
_WAIT_SECONDS = 10.0
_TICK_LIMIT = 40
_ROLE_TITLE_PREFIX = "FlowMarshal role: "
_REVIEW_ROLES = frozenset({
    "compact_plan_reviewer", "critical_effect_reviewer", "high_risk_reviewer",
    "external_effect_reviewer", RECOVERY_PLAN_REVIEWER_ROLE,
})
_REPLAN_ROLES = ("plan_expander", "plan_refiner", RECOVERY_PLAN_REVIEWER_ROLE)
#: runtime job의 provider binding 열. 역할 turn 추적 검사에서는 이 열을 근거로 쓰지 않는다.
_BINDING_COLUMNS = frozenset({("runtime_jobs", "thread_id"), ("runtime_jobs", "turn_id")})
#: 제품 코드가 binding 불일치에 남기는 문구. 정상 흐름의 원장에는 없어야 한다.
_BINDING_MISMATCH_MARKERS = (
    "runtime job provider binding이 기존 exact thread/turn과 다릅니다",
    "RUNTIME_OBSERVATION_BINDING_MISMATCH",
    "RUNTIME_JOB_RESULT_CHECKPOINT_BINDING_MISMATCH",
    "ROLE_TERMINAL_OBSERVATION_BINDING_MISMATCH",
)
_REPLANNED_ACCEPTANCE = "재계획한 검사 statement가 실패 근거를 다시 관측한다."
_REPLANNED_STATEMENT = "app.py의 값이 정확히 2인지 실제 파일을 읽어 실행 검사한다."
#: review 입력 결속 실패의 detail 접두(v5.1 D1-6). code·enum이 아니다.
_REVIEW_INPUT_MISMATCH = "RECOVERY_REPLAN_REVIEW_INPUT_MISMATCH"
#: 변조에 쓰는 다른 digest 값.
_OTHER_DIGEST = "sha256:" + "0" * 64
_M14_MODEL = "gpt-5.6-sol"


def _replan_keys(assessment_id: str) -> tuple[str, str]:
    """(expand·refine phase key, review phase key). v5.1 D1의 key 체계다."""

    return f"replanning:{assessment_id}", f"replanning:{assessment_id}:review"


class _Crash(BaseException):
    """expand 소비 뒤 review 예약 전에 owner process가 죽은 것을 흉내 낸다(AC8)."""


class _Clock:
    """supervisor clock double. 테스트가 deadline 뒤로 옮긴다(AC19)."""

    def __init__(self) -> None:
        self.value = utc_now()

    def __call__(self) -> datetime:
        return self.value


# --- fake runtime ------------------------------------------------------------------------------


class _RoleScriptedRuntime(FakeCodexRuntime):
    """역할 thread의 turn만 대본 JSON으로 끝내는 결정적 runtime.

    thread/turn ID·receipt·read는 FakeCodexRuntime 그대로다. 그래서 제품 runner가 실제 provider에서처럼
    thread_created·turn_started(thread/turn ID 포함) progress를 낸다. 대본 envelope 변환 규칙은
    InspectionScriptedRunner와 같다.

    - ``hold_read_role`` 역할 thread의 첫 ``read``(turn_started binding 뒤)에서 호출 thread를 세운다.
    - ``defer_roles`` 역할의 turn은 start_turn에서 끝내지 않는다(active turn). ``release``가 끝낸다.
    """

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        super().__init__(inventory())
        self.responses = {role: list(values) for role, values in responses.items()}
        self.role_by_thread: dict[str, str] = {}
        self.turn_log: list[tuple[str, str, str]] = []
        self.hold_read_role: str | None = None
        self.hold_reached = threading.Event()
        self.hold_release = threading.Event()
        self._held = False
        self._hold_lock = threading.Lock()
        self.defer_roles: set[str] = set()
        self.deferred: dict[str, str] = {}
        self.reads: Counter = Counter()
        self.stored_reads: Counter = Counter()

    def create_thread(self, **kwargs):
        title = kwargs.get("title", "")
        role = title[len(_ROLE_TITLE_PREFIX):] if title.startswith(_ROLE_TITLE_PREFIX) else None
        receipt = super().create_thread(**kwargs)
        if role is not None:
            self.role_by_thread[receipt.operation_id] = role
        return receipt

    def start_turn(self, **kwargs):
        receipt = super().start_turn(**kwargs)
        thread_id = kwargs["thread_id"]
        role = self.role_by_thread.get(thread_id)
        if role is None:
            return receipt  # Worker turn은 테스트가 직접 끝낸다.
        queue = self.responses.get(role)
        if not queue:
            raise AssertionError(f"{role} 역할 대본이 없습니다.")
        raw = queue.pop(0)
        payload = json.loads(kwargs["prompt"])
        if role == "plan_expander" and "inspection" not in raw:
            raw = {"plan": raw, "inspection": inspection_fixture(raw, payload["goal"])}
        elif role in _REVIEW_ROLES and "inspection" not in raw:
            catalog = payload["evidence_catalog"]
            raw = {"review": raw, "inspection": inspection_fixture(
                catalog["artifact:plan_contract"], catalog["source:goal"], revision=True, review=raw)}
        response = json.dumps(raw, ensure_ascii=False)
        if role in self.defer_roles:
            self.deferred[thread_id] = response
        else:
            self.complete(thread_id, response=response)
        self.turn_log.append((role, thread_id, receipt.operation_id))
        return receipt

    def read(self, *, thread_id):
        self.reads[thread_id] += 1
        role = self.role_by_thread.get(thread_id)
        if role is not None and role == self.hold_read_role:
            with self._hold_lock:
                first, self._held = not self._held, True
            if first:
                self.hold_reached.set()
                if not self.hold_release.wait(_WAIT_SECONDS):
                    raise TimeoutError("hold_release가 제한 시간 안에 오지 않았습니다.")
        return super().read(thread_id=thread_id)

    def read_stored(self, *, thread_id, turn_id=None, timeout_seconds=5.0):
        self.stored_reads[thread_id] += 1
        return super().read_stored(thread_id=thread_id, turn_id=turn_id, timeout_seconds=timeout_seconds)

    def finish_deferred(self) -> None:
        for thread_id, response in list(self.deferred.items()):
            if self.threads[thread_id].terminal_status is None:
                self.complete(thread_id, response=response)
        self.deferred.clear()

    def release(self) -> None:
        """세운 worker가 끝날 수 있게 한다(정리·재개 공통)."""

        self.finish_deferred()
        self.hold_release.set()


class _CountingRuntime(FakeCodexRuntime):
    """read·read_stored·interrupt를 thread별로 센다. interrupt receipt는 turn을 끝내지 않는다.

    grace 안에 provider가 terminal을 내지 않는 경우(AC19 hard stop)를 결정적으로 만든다.
    """

    def __init__(self, models) -> None:
        super().__init__(models)
        self.reads: Counter = Counter()
        self.stored_reads: Counter = Counter()
        self.interrupts: Counter = Counter()

    def read(self, *, thread_id):
        self.reads[thread_id] += 1
        return super().read(thread_id=thread_id)

    def read_stored(self, *, thread_id, turn_id=None, timeout_seconds=5.0):
        self.stored_reads[thread_id] += 1
        return super().read_stored(thread_id=thread_id, turn_id=turn_id, timeout_seconds=timeout_seconds)

    def interrupt(self, *, thread_id, turn_id, timeout_seconds=5.0):
        del timeout_seconds
        self.interrupts[thread_id] += 1
        self.interrupt_calls += 1
        if self.threads[thread_id].turn_id != turn_id:
            raise KeyError(turn_id)
        return RuntimeOperationReceipt(
            operation_id=turn_id,
            payload={"thread_id": thread_id, "turn_id": turn_id, "interrupt_requested": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )


# --- 원장·service double -------------------------------------------------------------------------


def _inside(function_name: str) -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == function_name:
            return True
        frame = frame.f_back
    return False


class _HookedConnection:
    """read 연결의 execute 수준 hook(AC6). 나머지 속성은 원래 연결에 위임한다."""

    def __init__(self, connection: sqlite3.Connection, ledger: "_PausingLedger") -> None:
        self._connection = connection
        self._ledger = ledger

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, *args):
        cursor = self._connection.execute(sql, *args)
        hook = self._ledger.after_job_select
        if (
            hook is not None
            and threading.get_ident() == self._ledger.armed_thread
            and "FROM runtime_jobs " in " ".join(str(sql).split()) + " "
            and _inside("_durable_target_result")
        ):
            self._ledger.after_job_select = None
            hook()
        return cursor


class _PausingLedger(SQLiteEngineLedger):
    """`_durable_target_result`가 runtime_jobs 행을 읽은 직후·관측을 읽기 전에 한 번 멈추는 ledger(AC6)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.after_job_select = None
        self.armed_thread: int | None = None

    @contextmanager
    def read(self):
        with super().read() as connection:
            yield _HookedConnection(connection, self)


class _ServiceProxy:
    """EngineService 위임 proxy. 제품 코드는 바꾸지 않고 두 지점에만 hook을 건다.

    - ``durable_terminal_hook``: 성공 checkpoint 재부착의 PROVIDER_TERMINAL 기록 직전 한 번(AC20(c)(d1)).
    - ``after_tick_load_hook``: ``tick``이 job을 처음 읽은 직후 한 번(AC11 늦은 owner tick).
    """

    def __init__(self, real: EngineService) -> None:
        self._real = real
        self.durable_terminal_hook = None
        self.after_tick_load_hook = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def record_runtime_job_observation(self, job_id, *, kind, payload, **kwargs):
        hook = self.durable_terminal_hook
        if (
            hook is not None
            and kind is RuntimeJobObservationKind.PROVIDER_TERMINAL
            and isinstance(payload, dict)
            and payload.get("result_source") == "durable_target_checkpoint"
        ):
            self.durable_terminal_hook = None
            hook(job_id)
        return self._real.record_runtime_job_observation(job_id, kind=kind, payload=payload, **kwargs)

    def load_runtime_job(self, job_id):
        job = self._real.load_runtime_job(job_id)
        hook = self.after_tick_load_hook
        if hook is not None and sys._getframe(1).f_code.co_name == "tick":
            self.after_tick_load_hook = None
            hook(job_id)
        return job


class _PausingDurableReadSupervisor(RuntimeJobSupervisor):
    """tick의 첫 `_results` pop 뒤, `_durable_target_result` 읽기 직전에 한 번 hook을 부른다(D5 (2) 경합)."""

    before_durable_read = None

    def _durable_target_result(self, job_id):
        hook, self.before_durable_read = self.before_durable_read, None
        if hook is not None:
            hook()
        return super()._durable_target_result(job_id)


class _HelperBarrierSupervisor(RuntimeJobSupervisor):
    """AC20(e): checkpoint 전용 helper가 None을 돌려주기 직전에 멈추는 테스트 subclass.

    HEAD에는 helper가 없으므로 이 override는 불리지 않는다(그 사실이 HEAD red 원인으로 남는다).
    """

    barrier: SimpleNamespace | None = None

    def _reattach_checkpoint(self, job_id):  # noqa: D401 - 수정 뒤 private helper의 test hook
        result = super()._reattach_checkpoint(job_id)
        barrier = self.barrier
        if result is None and barrier is not None and barrier.armed:
            barrier.armed = False
            barrier.helper_done.set()
            barrier.transition_done.wait(_WAIT_SECONDS)
        return result


# --- 제품 runner harness -----------------------------------------------------------------------


class _ProductRunnerHarness(g1b._ReplanHarness):
    """fm08·G1b facade harness를 빌리되 역할 runner만 제품 기본 runner로 바꾼다."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("테스트 지침\n", encoding="utf-8")
        self.app_file = self.root / "app.py"
        self.app_file.write_text("value = 1\n", encoding="utf-8")
        self.service = EngineService(
            SQLiteEngineLedger(base / "state" / "engine.sqlite3", artifact_root=base / "artifacts")
        )
        self.service.initialize()
        self.project_id = self.service.create_project(name="m14-replan-binding", root=self.root)
        self.service.register_profile(profile(self.project_id))
        self.runtime = _RoleScriptedRuntime(fm08._responses())
        # fm08·G1b 대본 helper는 `self.runner.responses`에 넣는다. 제품 runner 경로에서는 runtime 대본이다.
        self.runner = self.runtime
        self.supervisors = []
        self.addCleanup(self._close_supervisors)
        # 정리는 역순이다. 세워 둔 worker를 supervisor 정리보다 먼저 풀어 준다.
        self.addCleanup(self.runtime.release)
        self.application = self._application()
        # structured_runner를 주지 않으면 EngineApplication이 제품 기본 runner를 만든다.
        runner = self.application._execution_components()[2]
        self.assertIs(CodexStructuredRoleRunner, type(runner))
        self.authority = ApplicationAuthority(self.application)

    def _application(self, *, service: EngineService | None = None, **options) -> EngineApplication:
        application = EngineApplication(
            service or self.service,
            runtime=self.runtime,
            role_configuration=fm08._roles(),
            governance=ALLOW_ALL,
            **options,
        )
        self.supervisors.append(application.supervisor)
        return application

    def _separate_service(self) -> EngineService:
        """같은 원장을 따로 연 EngineService. 다른 process·재시작과 같은 처지다."""

        return EngineService(SQLiteEngineLedger(
            self.service.ledger.path, artifact_root=self.service.ledger.artifact_root,
        ))

    def _non_owner(self) -> EngineApplication:
        """provider가 있는 두 번째 EngineApplication(따로 연 service·새 supervisor)."""

        return self._application(service=self._separate_service())

    def _providerless(self, *, shared_supervisor: bool) -> EngineApplication:
        """역할 설정 없는 인스턴스(tests/test_engine_g1b_recovery_path.py:1267-1275와 같은 모양)."""

        application = EngineApplication(
            self.service, runtime=self.runtime, governance=ALLOW_ALL,
            supervisor=self.application.supervisor if shared_supervisor else None,
        )
        if not shared_supervisor:
            self.supervisors.append(application.supervisor)
        self.assertFalse(application.recovery_provider_available())
        return application

    # --- tick ----------------------------------------------------------------

    def _run_once(self, application: EngineApplication | None = None):
        try:
            return (application or self.application).run_once(self.project_id)
        except Exception as error:  # noqa: BLE001 - run_once 밖으로 나온 예외 자체가 실패다.
            first = (str(error).splitlines() or [""])[0]
            self.fail(f"run_once가 예외를 냈습니다: {type(error).__name__}: {first[:600]}")

    def _quiesce(self, application: EngineApplication | None = None) -> None:
        """application supervisor의 worker가 모두 끝날 때까지 기다린다. 상한을 넘으면 실패한다."""

        supervisor = (application or self.application).supervisor
        deadline = time.monotonic() + _WAIT_SECONDS
        for worker in list(supervisor._workers.values()):
            if worker.ident is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
        alive = sorted(worker.name for worker in supervisor._workers.values() if worker.is_alive())
        if alive:
            self.fail(f"{_WAIT_SECONDS}초 안에 끝나지 않은 owner worker: {alive}")

    def _assert_not_blocked(self, result) -> None:
        self.assertNotEqual(
            RunOnceAction.BLOCKED, result.action,
            f"run_once가 BLOCKED로 멈췄습니다: blocker={result.blocker_code} "
            f"detail={(result.detail or '')[:600]} replanning jobs={self._replan_job_states()}",
        )

    def _tick(self, application: EngineApplication | None = None):
        """run_once 한 번. BLOCKED·예외는 실패이고, owner worker가 끝난 뒤 돌아온다."""

        result = self._run_once(application)
        self._assert_not_blocked(result)
        self._quiesce(application)
        return result

    def _drive_until_plan_activated(self, previous_plan_id: str, *, application=None, after_tick=None) -> str:
        for _ in range(_TICK_LIMIT):
            self._tick(application)
            if after_tick is not None:
                after_tick()
            active = self._active_plan_id()
            if active != previous_plan_id:
                return active
        self.fail(f"{_TICK_LIMIT} tick 안에 재계획 Plan이 활성화되지 않았습니다: {self._replan_job_states()}")

    def _drive_until_blocked(self, application: EngineApplication | None = None):
        for _ in range(_TICK_LIMIT):
            result = self._run_once(application)
            self._quiesce(application)
            if result.action is RunOnceAction.BLOCKED:
                return result
        self.fail(f"{_TICK_LIMIT} tick 안에 BLOCKED에 도달하지 못했습니다: {self._replan_job_states()}")

    def _wait_event_or_idle(self, event: threading.Event, application=None) -> bool:
        """event가 오면 True, 오지 않고 owner worker가 모두 끝나면 False. 상한을 넘으면 실패한다."""

        supervisor = (application or self.application).supervisor
        deadline = time.monotonic() + _WAIT_SECONDS
        while time.monotonic() < deadline:
            if event.wait(0.01):
                return True
            if not any(worker.is_alive() for worker in list(supervisor._workers.values())):
                return event.is_set()
        self.fail(f"{_WAIT_SECONDS}초 안에 owner worker가 정지 지점에 닿지도, 끝나지도 않았습니다.")

    def _drive_until_event(self, event: threading.Event, what: str, application=None) -> None:
        for _ in range(_TICK_LIMIT):
            self._assert_not_blocked(self._run_once(application))
            if self._wait_event_or_idle(event, application):
                return
        self.fail(f"{_TICK_LIMIT} tick 안에 {what}에 닿지 못했습니다: {self._replan_job_states()}")

    # --- 원장 관측 -------------------------------------------------------------

    def _ledger_mentions(self, needle: str) -> list[str]:
        """binding 열 밖에서 needle을 그대로 담은 `table.column` 목록."""

        hits = []
        with self.service.ledger.read() as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )]
            for table in tables:
                for column in [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]:
                    if (table, column) in _BINDING_COLUMNS:
                        continue
                    try:
                        found = connection.execute(
                            f'SELECT 1 FROM "{table}" WHERE instr(CAST("{column}" AS TEXT), ?) > 0 LIMIT 1',
                            (needle,),
                        ).fetchone()
                    except sqlite3.Error:
                        continue
                    if found is not None:
                        hits.append(f"{table}.{column}")
        return hits

    def _assert_role_turns_traceable(self, turns: list[tuple[str, str, str]], *, roles: tuple[str, ...]) -> None:
        self.assertLessEqual(set(roles), {turn[0] for turn in turns}, f"재계획 구간의 역할 turn: {turns}")
        orphans = [(role, turn_id) for role, _thread, turn_id in turns if not self._ledger_mentions(turn_id)]
        self.assertEqual([], orphans, "binding 열 밖의 원장에서 turn ID로 찾을 수 없는 역할 turn")

    def _assert_no_binding_mismatch_recorded(self) -> None:
        found = {marker: hits for marker in _BINDING_MISMATCH_MARKERS
                 if (hits := self._ledger_mentions(marker))}
        self.assertEqual({}, found, "원장에 binding 불일치가 기록됐습니다.")

    def _reviewer_roles(self, activation_digest: str) -> list[str]:
        return [row[0] for row in self._rows(
            "SELECT reviewer_role FROM candidate_reviews WHERE project_id=? AND artifact_digest=?",
            self.project_id, activation_digest,
        )]

    def _replan_jobs(self) -> list[tuple]:
        """(checkpoint_key, status, thread_id, turn_id, id) — rowid 순."""

        return self._rows(
            "SELECT checkpoint_key,status,thread_id,turn_id,id FROM runtime_jobs "
            "WHERE project_id=? AND kind='replanning' ORDER BY rowid", self.project_id,
        )

    def _replan_job_states(self) -> list[tuple[str, str]]:
        return [(row[0], row[1]) for row in self._replan_jobs()]

    def _job(self, checkpoint_key: str) -> dict | None:
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE project_id=? AND checkpoint_key=?",
                (self.project_id, checkpoint_key),
            ).fetchone()
        return None if row is None else dict(row)

    def _job_status(self, job_id: str) -> str:
        return self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0]

    def _observations(self, job_id: str) -> list[tuple[str, dict]]:
        return [(kind, json.loads(payload)) for kind, payload in self._rows(
            "SELECT kind,payload_json FROM runtime_job_observations WHERE job_id=? ORDER BY rowid", job_id,
        )]

    def _kinds(self, job_id: str) -> list[str]:
        return [kind for kind, _payload in self._observations(job_id)]

    def _role_progress(self, job_id: str) -> list[str]:
        return [payload["role_progress"].get("event") for kind, payload in self._observations(job_id)
                if kind == "provider_progress" and isinstance(payload.get("role_progress"), dict)]

    def _candidate_state(self) -> dict[str, list[tuple]]:
        """교체 후보가 원장에 들어갔는지 보는 세 표(plan_revisions·candidate_decisions·plan_activations)."""

        return {
            table: self._rows(f"SELECT id FROM {table} WHERE project_id=? ORDER BY rowid", self.project_id)
            for table in ("plan_revisions", "candidate_decisions", "plan_activations")
        }

    def _max_provider_call_rowid(self) -> int:
        return self._rows(
            "SELECT COALESCE(MAX(rowid),0) FROM provider_calls WHERE project_id=?", self.project_id,
        )[0][0]

    def _role_calls_since(self, rowid: int) -> list[tuple]:
        """재계획 역할 provider_call의 (role, execution_status, receipt 있음) — rowid 순."""

        return self._rows(
            "SELECT role,execution_status,receipt_json IS NOT NULL FROM provider_calls "
            "WHERE project_id=? AND rowid>? AND role IN (?,?,?) ORDER BY rowid",
            self.project_id, rowid, *_REPLAN_ROLES,
        )

    def _turn_roles(self, since: int) -> list[str]:
        return [turn[0] for turn in self.runtime.turn_log[since:]]

    # --- 시나리오 ---------------------------------------------------------------

    def _to_assessment(self) -> tuple[str, str, str, str]:
        """task_contract 실패를 주입하고 SUBGRAPH_REPLAN assessment가 기록될 때까지 진행한다."""

        task_id, attempt = self._fail_contract()
        original = self._active_plan_id()
        self._run_until(RunOnceAction.RECOVERED)
        rows = self._rows("SELECT id FROM recovery_assessments WHERE project_id=? ORDER BY rowid", self.project_id)
        self.assertEqual(1, len(rows), rows)
        return task_id, attempt, original, rows[0][0]

    def _queue_c1b_replan(self) -> int:
        """fm08 C1-b와 같은 재계획 대본을 넣고 그 전까지 시작된 역할 turn 수를 돌려준다."""

        self.runner.responses.setdefault("plan_expander", []).append(fm08._plan_expansion(
            acceptance=["Task 검사가 PASS다.", _REPLANNED_ACCEPTANCE], statement=_REPLANNED_STATEMENT,
        ))
        self.runner.responses.setdefault(RECOVERY_PLAN_REVIEWER_ROLE, []).append(g1b._clean_review())
        return len(self.runtime.turn_log)

    def _lose_owner_lease_for(self, checkpoint_key: str) -> Path:
        """`_acquire_owner_lease` 테스트 double: 이 checkpoint의 lock은 누구에게나 FREE다(owner 소실).

        owner worker는 살아 있지만 lock은 잃은 상태를 만든다. lease는 process registry에 등록되지 않으므로
        읽기 전용 probe(status)도 lock 파일을 직접 잠가 보고 FREE를 본다. 다른 lock은 실제 함수를 쓴다.
        """

        path = str(runtime_owner_lock_path(self.service, self.project_id, checkpoint_key))
        real = runtime_module._acquire_owner_lease

        def free_for_everyone(lock_path, holder, *, create):
            if str(lock_path) != path:
                return real(lock_path, holder, create=create)
            if create:
                Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
                Path(lock_path).touch()
            return OwnerLockState.FREE, runtime_module._OwnerLease(str(lock_path), holder, None)

        patcher = mock.patch.object(runtime_module, "_acquire_owner_lease", free_for_everyone)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(path)

    def _set_job_columns(self, job_id: str, **columns) -> None:
        """테스트 임시 원장의 runtime_jobs 열만 바꾼다(변조·손상 주입)."""

        assignments = ",".join(f"{name}=?" for name in columns)
        with self.service.ledger.transaction() as tx:
            tx.connection.execute(
                f"UPDATE runtime_jobs SET {assignments} WHERE id=?", (*columns.values(), job_id),
            )

    @staticmethod
    def _shift_plan_created_at(value: dict) -> dict:
        """plan.created_at만 1초 옮긴다. definition·activation digest와 strict 파싱은 그대로다."""

        changed = copy.deepcopy(value)
        created = datetime.fromisoformat(changed["plan"]["created_at"].replace("Z", "+00:00"))
        changed["plan"]["created_at"] = (created + timedelta(seconds=1)).isoformat()
        return changed

    def _assert_replan_finishes_the_goal(
        self, *, original: str, task_id: str, attempt: str, active_id: str, turns: list[tuple[str, str, str]],
    ) -> None:
        """C1-b의 끝 상태: 재계획 revision 활성화, 복구 검토, 실패 근거 보존, Goal 완료."""

        self.assertEqual(
            [(original, 1, "superseded", None), (active_id, 2, "active", original)],
            [(row[0], row[2], row[3], row[4]) for row in self._plans()],
        )
        active = self._plan(active_id)
        self.assertEqual(1, len(active.definition.tasks))
        new_task = active.definition.tasks[0]
        self.assertNotEqual(task_id, new_task.task_id)
        self.assertIn(_REPLANNED_ACCEPTANCE, new_task.acceptance_criteria)
        self.assertEqual(_REPLANNED_STATEMENT, new_task.validations[0].statement)
        self.assertIn(RECOVERY_PLAN_REVIEWER_ROLE, self._reviewer_roles(active.activation_digest))
        self.assertEqual(
            [("failed", "task_contract")],
            self._rows("SELECT status,failure_class FROM attempts WHERE id=?", attempt),
        )
        self.assertEqual(
            [(task_id,)],
            self._rows("SELECT id FROM task_contracts WHERE project_id=? AND status='superseded'", self.project_id),
        )
        self.assertEqual("recovered", self.application.status(self.project_id)["recovery"]["state"])
        self._assert_role_turns_traceable(turns, roles=("plan_expander", RECOVERY_PLAN_REVIEWER_ROLE))
        self._complete_goal(new_task.task_id)
        self._assert_no_binding_mismatch_recorded()

    def _assert_retry_refines_reviews_and_finishes(self, *, original: str, candidate: str) -> tuple[str, list]:
        """needs_revision 차단 뒤 replan → 재시도(plan_refiner → 복구 검토) → 활성화 → Goal 완료."""

        retry = self._replan()["assessment_id"]
        self._queue_refinement(review=g1b._clean_review(), statement=_REPLANNED_STATEMENT)
        turns_before = len(self.runtime.turn_log)
        active_id = self._drive_until_plan_activated(original)
        turns = self.runtime.turn_log[turns_before:]
        self.assertEqual(["plan_refiner", RECOVERY_PLAN_REVIEWER_ROLE], [turn[0] for turn in turns])
        self.assertEqual(
            [(original, 1, "superseded"), (candidate, 2, "draft"), (active_id, 3, "active")],
            [(row[0], row[2], row[3]) for row in self._plans()],
        )
        self.assertEqual(
            [(original, None), (candidate, original), (active_id, candidate)],
            [(row[0], row[4]) for row in self._plans()],
        )
        active = self._plan(active_id)
        self.assertEqual(_REPLANNED_STATEMENT, active.definition.tasks[0].validations[0].statement)
        self.assertIn(RECOVERY_PLAN_REVIEWER_ROLE, self._reviewer_roles(active.activation_digest))
        self.assertEqual("recovered", self.application.status(self.project_id)["recovery"]["state"])
        self._assert_role_turns_traceable(turns, roles=("plan_refiner", RECOVERY_PLAN_REVIEWER_ROLE))
        self._complete_goal(active.definition.tasks[0].task_id)
        self._assert_no_binding_mismatch_recorded()
        return retry, turns

    def _use_scripted_first_replan(self):
        """첫 재계획은 scripted runner application이 만든다(같은 원장을 따로 연 service).

        제품 runner application과 원장·runtime을 공유한다. 돌려준 함수를 부르면 제품 runner로 되돌린다.
        """

        product, product_authority = self.application, self.authority
        scripted = InspectionScriptedRunner(fm08._responses())
        self.application = self._application(service=self._separate_service(), structured_runner=scripted)
        self.authority = ApplicationAuthority(self.application)
        self.runner = scripted

        def restore():
            self.application, self.authority, self.runner = product, product_authority, self.runtime

        return restore


# --- AC1·AC2·AC3·AC4·AC8·AC14·AC21: 두 phase job의 제품 경로 ---------------------------------------


class ReplanWithProductRunnerTests(_ProductRunnerHarness, unittest.TestCase):
    def test_ac1_subgraph_replan_runs_expand_and_review_as_two_jobs_and_finishes(self) -> None:
        """AC1: 역할 turn마다 job 하나, 각 job binding = 그 turn, review 소비 전 후보 없음, Goal 완료."""

        task_id, attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        baseline = self._candidate_state()
        calls_before = self._max_provider_call_rowid()
        turns_before = self._queue_c1b_replan()
        states: list[tuple] = []

        def no_candidate_before_review_is_consumed() -> None:
            jobs = dict(self._replan_job_states())
            if jobs.get(review_key) == "consumed":
                return
            states.append((jobs.get(expand_key), jobs.get(review_key)))
            self.assertEqual(
                baseline, self._candidate_state(),
                f"review job 소비 전에 교체 후보가 원장에 생겼습니다: (expand, review) 상태 {states}",
            )

        active_id = self._drive_until_plan_activated(original, after_tick=no_candidate_before_review_is_consumed)
        self.assertIn(("consumed", "running"), states, states)
        self.assertIn(("consumed", "provider_terminal"), states, states)
        turns = self.runtime.turn_log[turns_before:]
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], [turn[0] for turn in turns])
        jobs = self._replan_jobs()
        self.assertEqual([(expand_key, "consumed"), (review_key, "consumed")], [(row[0], row[1]) for row in jobs])
        self.assertEqual(
            [(turns[0][1], turns[0][2]), (turns[1][1], turns[1][2])],
            [(row[2], row[3]) for row in jobs],
            "각 job binding은 그 역할 turn이어야 합니다.",
        )
        self.assertEqual(
            [("plan_expander", "terminal", 1), (RECOVERY_PLAN_REVIEWER_ROLE, "terminal", 1)],
            self._role_calls_since(calls_before),
        )
        self._assert_replan_finishes_the_goal(
            original=original, task_id=task_id, attempt=attempt, active_id=active_id, turns=turns,
        )

    def _pause_reviewer_at_first_read(self) -> tuple[str, str, str, str, int, dict]:
        """C1-b 실패를 주입하고 owner worker를 review job의 reviewer turn 첫 read(binding 뒤)에서 세운다."""

        task_id, attempt, original, stable = self._to_assessment()
        turns_before = self._queue_c1b_replan()
        self.runtime.hold_read_role = RECOVERY_PLAN_REVIEWER_ROLE
        self._drive_until_event(self.runtime.hold_reached, "복구 검토 역할 turn의 첫 read")
        review = self._job(_replan_keys(stable)[1])
        self.assertIsNotNone(review, f"review job 행이 없습니다: {self._replan_job_states()}")
        self.assertEqual(
            (review["thread_id"], review["turn_id"]), self.runtime.turn_log[-1][1:],
            "정지 지점은 reviewer turn_started binding 뒤여야 합니다.",
        )
        self.assertTrue(self.application.supervisor.runtime_job_worker_alive(review["id"]))
        return task_id, attempt, original, stable, turns_before, review

    def test_ac2a_non_owner_tick_during_the_paused_reviewer_turn_only_observes(self) -> None:
        """AC2: owner가 살아 있는 bound review job에 비소유 run_once는 OBSERVED(2행)이고 관측을 쓰지 않는다."""

        *_rest, review = self._pause_reviewer_at_first_read()
        job_id = review["id"]
        other = self._non_owner()
        kinds_before = self._kinds(job_id)
        outcome = self._run_once(other)
        new_kinds = self._kinds(job_id)[len(kinds_before):]
        self.assertEqual(RunOnceAction.OBSERVED, outcome.action, outcome)
        self.assertEqual([], [kind for kind in new_kinds if kind in {"provider_terminal", "collector_lost"}], new_kinds)
        self.assertEqual("running", self._job_status(job_id))
        self.assertNotIn(job_id, other.supervisor._owned_job_ids)
        verdict = other._unbound_job_verdict(self.service.load_runtime_job(job_id))
        self.assertEqual("2", getattr(verdict, "row", None), verdict)
        recovery = other.status(self.project_id)["recovery"]
        self.assertEqual(
            ("none", "none", None),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"]),
            recovery,
        )
        self.assertIn("owner 실행 중", recovery["next_action"]["detail"])
        self.assertTrue(
            self.application.supervisor.runtime_job_worker_alive(job_id),
            "검사 전제: owner worker가 아직 정지 지점에 서 있어야 합니다.",
        )

    def test_ac2b_owner_finishes_the_replan_after_a_non_owner_tick(self) -> None:
        """AC2: 비소유 tick 뒤 owner를 재개하면 AC1과 같이 끝난다."""

        task_id, attempt, original, stable, turns_before, review = self._pause_reviewer_at_first_read()
        self._run_once(self._non_owner())
        self.runtime.release()
        active_id = self._drive_until_plan_activated(original)
        turns = self.runtime.turn_log[turns_before:]
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], [turn[0] for turn in turns])
        self.assertEqual([(key, "consumed") for key in _replan_keys(stable)], self._replan_job_states())
        self._assert_replan_finishes_the_goal(
            original=original, task_id=task_id, attempt=attempt, active_id=active_id, turns=turns,
        )

    def test_ac3_needs_revision_retry_refines_and_reviews_with_the_product_runner(self) -> None:
        """AC3 (3): 첫 재계획 차단 → replan → [plan_refiner, reviewer] 두 phase job → 활성화 → Goal 완료."""

        _task_id, _attempt, original, stable = self._to_assessment()
        self._queue_replan(review=g1b._finding_review(), statement="app.py 값을 검사한다.")
        blocked = self._drive_until_blocked()
        self.assertEqual(
            "REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code,
            f"첫 재계획이 needs_revision 차단이 아닌 곳에서 멈췄습니다: {(blocked.detail or '')[:600]}",
        )
        retry, _turns = self._assert_retry_refines_reviews_and_finishes(
            original=original, candidate=self._plans()[-1][0],
        )
        self.assertEqual(
            [(key, "consumed") for key in (*_replan_keys(stable), *_replan_keys(retry))],
            self._replan_job_states(),
        )

    def test_ac3_retry_job_alone_refines_and_reviews_with_the_product_runner(self) -> None:
        """AC3 (3'): 첫 재계획은 scripted, 재시도의 두 phase job만 제품 runner로 돈다."""

        restore = self._use_scripted_first_replan()
        self._fail_contract()
        original = self._active_plan_id()
        self._queue_replan(review=g1b._finding_review(), statement="app.py 값을 검사한다.")
        blocked = self._drive_until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code, blocked)
        candidate = self._plans()[-1][0]
        restore()
        retry, _turns = self._assert_retry_refines_reviews_and_finishes(original=original, candidate=candidate)
        self.assertEqual(
            [(key, "consumed") for key in _replan_keys(retry)], self._replan_job_states()[-2:],
        )

    def test_ac4_refiner_without_a_plan_replays_the_block_without_a_review_job(self) -> None:
        """AC4(보존): plan_refiner가 plan=None이면 review 행 없음·역할 1회·같은 NOT_ADMISSIBLE 재생·원장 무변화."""

        restore = self._use_scripted_first_replan()
        self._fail_contract()
        self._queue_replan(review=g1b._finding_review(), statement="app.py 값을 검사한다.")
        first = self._drive_until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", first.blocker_code, first)
        candidate = self._plans()[-1][0]
        restore()
        retry = self._replan()["assessment_id"]
        self.runtime.responses.setdefault("plan_refiner", []).append({
            "action": "unresolved",
            "rationale": "finding을 해소할 근거가 부족하다.",
            "evidence_refs": ["artifact:plan_contract"],
            "plan": None,
            "skeleton": None,
        })
        plans = self._plans()
        turns_before = len(self.runtime.turn_log)
        blocked = self._drive_until_blocked()
        self.assertEqual("REPLAN_CANDIDATE_NOT_ADMISSIBLE", blocked.blocker_code, blocked)
        self.assertIn(candidate, blocked.detail)
        self.assertEqual(["plan_refiner"], self._turn_roles(turns_before))
        retry_key, retry_review_key = _replan_keys(retry)
        self.assertEqual((retry_key, "consumed"), self._replan_job_states()[-1])
        self.assertIsNone(self._job(retry_review_key))
        self.assertEqual(plans, self._plans())
        before = self._ledger()
        for _ in range(2):
            again = self._run_once()
            self.assertEqual((blocked.blocker_code, blocked.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())
        self.assertEqual(["plan_refiner"], self._turn_roles(turns_before))
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual(blocked.detail[:2000], recovery["next_action"]["detail"])

    def test_ac8_owner_loss_between_expand_consume_and_review_reservation(self) -> None:
        """AC8: expand 소비 뒤 review 예약 전에 owner를 잃으면 다음 run_once가 review를 예약하고 expander는 다시 부르지 않는다."""

        _task_id, _attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        turns_before = self._queue_c1b_replan()
        supervisor = self.application.supervisor
        real_schedule = supervisor.schedule

        def crash_at_review_reservation(*args, **kwargs):
            if str(kwargs.get("checkpoint_key", "")).endswith(":review"):
                raise _Crash("review 예약 직전 owner process 종료")
            return real_schedule(*args, **kwargs)

        supervisor.schedule = crash_at_review_reservation
        crashed = False
        for _ in range(_TICK_LIMIT):
            try:
                result = self._run_once()
            except _Crash:
                crashed = True
                break
            self._assert_not_blocked(result)
            self._quiesce()
        self.assertTrue(crashed, f"review 예약에 닿지 못했습니다: {self._replan_job_states()}")
        self.assertEqual([(expand_key, "consumed")], self._replan_job_states())
        other = self._non_owner()
        active_id = self._drive_until_plan_activated(original, application=other)
        self.assertEqual([(expand_key, "consumed"), (review_key, "consumed")], self._replan_job_states())
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], self._turn_roles(turns_before))
        self.assertEqual(2, self._plan(active_id).revision_no)
        self.assertIn(RECOVERY_PLAN_REVIEWER_ROLE, self._reviewer_roles(self._plan(active_id).activation_digest))

    def test_ac8_owner_loss_with_an_expand_checkpoint_reattaches_then_reserves_review(self) -> None:
        """AC8(R3의 checkpoint 경우): owner가 tick 전에 사라져도 새 인스턴스가 checkpoint로 이어 가고 expander를 다시 부르지 않는다."""

        _task_id, _attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        turns_before = self._queue_c1b_replan()
        scheduled = self._run_once()
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        self._quiesce()
        other = self._non_owner()
        active_id = self._drive_until_plan_activated(original, application=other)
        self.assertEqual([(expand_key, "consumed"), (review_key, "consumed")], self._replan_job_states())
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], self._turn_roles(turns_before))
        expand_terminal = [payload for kind, payload in self._observations(self._job(expand_key)["id"])
                           if kind == "provider_terminal"]
        self.assertEqual(["durable_target_checkpoint"], [item.get("result_source") for item in expand_terminal])
        self.assertEqual(2, self._plan(active_id).revision_no)

    def test_ac14_default_product_runner_has_no_schema_recovery(self) -> None:
        """AC14(보존): EngineApplication 기본 runner의 max_schema_recovery_attempts는 0이다."""

        runner = self.application._execution_components()[2]
        self.assertIs(CodexStructuredRoleRunner, type(runner))
        self.assertEqual(0, runner.max_schema_recovery_attempts)
        self.assertFalse(runner.ephemeral_threads)
        fresh = EngineApplication(
            self.service, runtime=self.runtime, role_configuration=fm08._roles(), governance=ALLOW_ALL,
        )
        self.supervisors.append(fresh.supervisor)
        self.assertEqual(0, fresh._execution_components()[2].max_schema_recovery_attempts)

    def test_ac21_providerless_instance_stops_for_review_and_the_provider_instance_finishes(self) -> None:
        """AC21: provider 없는 인스턴스는 expand를 관측·소비하고 review 예약 시점에 REPLAN_PROVIDER_REQUIRED다."""

        _task_id, _attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        baseline = self._candidate_state()
        turns_before = self._queue_c1b_replan()
        scheduled = self._run_once()
        self.assertEqual((RunOnceAction.DISPATCHED, "replanning"), (scheduled.action, scheduled.runtime_job_kind))
        self._quiesce()
        bare = self._providerless(shared_supervisor=True)
        blocked = self._drive_until_blocked(bare)
        self.assertEqual(
            ("REPLAN_PROVIDER_REQUIRED", REPLAN_PROVIDER_REQUIRED_DETAIL),
            (blocked.blocker_code, blocked.detail), blocked,
        )
        self.assertEqual([(expand_key, "consumed")], self._replan_job_states())
        self.assertEqual(["plan_expander"], self._turn_roles(turns_before))
        self.assertEqual(baseline, self._candidate_state())
        recovery = bare.status(self.project_id)["recovery"]
        self.assertEqual(
            ("user_decision_required", "REPLAN_PROVIDER_REQUIRED", blocked.detail),
            (recovery["state"], recovery["next_action"]["blocker_code"], recovery["next_action"]["detail"]),
            recovery,
        )
        before = self._ledger()
        again = self._run_once(bare)
        self.assertEqual((blocked.blocker_code, blocked.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())

        active_id = self._drive_until_plan_activated(original)
        self.assertEqual([(expand_key, "consumed"), (review_key, "consumed")], self._replan_job_states())
        self.assertEqual(["plan_expander", RECOVERY_PLAN_REVIEWER_ROLE], self._turn_roles(turns_before))
        self.assertEqual(2, self._plan(active_id).revision_no)
        self.assertEqual("recovered", self.application.status(self.project_id)["recovery"]["state"])


# --- AC5·AC9·AC10: bound target kind의 owner lease(제품 경로) ----------------------------------------


class BoundTargetOwnerLeaseProductTests(_ProductRunnerHarness, unittest.TestCase):
    def test_ac5_non_owner_run_once_leaves_a_live_owners_spec_prepare_job_to_the_owner(self) -> None:
        """AC5: owner가 살아 있는(held_other) bound SPEC_PREPARE job에 비소유 run_once는 terminal을 쓰지 않는다."""

        task_id = self._prepare()
        self._authorize()
        self.runtime.hold_read_role = "execution_preparation"
        self._queue_execution_preparation(task_id)
        self._drive_until_event(self.runtime.hold_reached, "Execution Spec 준비 역할 turn의 첫 read")
        jobs = self._rows(
            "SELECT id,checkpoint_key,thread_id FROM runtime_jobs WHERE project_id=? "
            "AND kind='execution_spec_prepare'", self.project_id,
        )
        self.assertEqual(1, len(jobs), jobs)
        job_id, checkpoint_key, bound_thread = jobs[0]
        self.assertIsNotNone(bound_thread, "정지 지점은 turn_started binding 뒤여야 합니다.")
        lock = runtime_owner_lock_path(self.service, self.project_id, checkpoint_key)
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(lock, holder=object()))
        other = self._non_owner()
        kinds_before = self._kinds(job_id)
        outcome = self._run_once(other)
        new_kinds = self._kinds(job_id)[len(kinds_before):]
        self.assertEqual(RunOnceAction.OBSERVED, outcome.action, outcome)
        self.assertEqual(
            [], [kind for kind in new_kinds if kind in {"provider_terminal", "collector_lost"}],
            f"비소유 run_once가 살아 있는 owner의 job에 관측을 썼습니다: {new_kinds} "
            f"(job status={self._job_status(job_id)})",
        )
        self.assertEqual("running", self._job_status(job_id))
        self.assertNotIn(job_id, other.supervisor._owned_job_ids)

        self.runtime.release()
        self.application.supervisor._workers[job_id].join(_WAIT_SECONDS)
        self._run_until(RunOnceAction.MATERIALIZED)
        consumed = self._rows("SELECT status,result_json FROM runtime_jobs WHERE id=?", job_id)[0]
        self.assertEqual("consumed", consumed[0])
        self.assertLessEqual({"preparation", "model_inventory"}, set(json.loads(consumed[1])))
        terminals = [payload for kind, payload in self._observations(job_id) if kind == "provider_terminal"]
        self.assertEqual(1, len(terminals), terminals)
        self.assertNotIn("observation", terminals[0])

    def _job_with_lost_owner_paused_at_first_read(self, *, review: bool, active_turn: bool) -> tuple[dict, str]:
        """expand 또는 review job을 bound·lease FREE(owner 소실)·owner worker 정지·checkpoint 없음으로 세운다."""

        _task_id, _attempt, _original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        key = review_key if review else expand_key
        role = RECOVERY_PLAN_REVIEWER_ROLE if review else "plan_expander"
        lock = self._lose_owner_lease_for(key)
        self._queue_c1b_replan()
        self.runtime.hold_read_role = role
        if active_turn:
            self.runtime.defer_roles.add(role)
        self._drive_until_event(self.runtime.hold_reached, f"{role} 역할 turn의 첫 read")
        job = self._job(key)
        self.assertIsNotNone(job, f"{key} 행이 없습니다: {self._replan_job_states()}")
        self.assertIsNotNone(job["thread_id"], "정지 지점은 turn_started binding 뒤여야 합니다.")
        self.assertEqual("running", job["status"])
        self.assertTrue(self.application.supervisor.runtime_job_worker_alive(job["id"]))
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(lock, holder=object()))
        self.assertNotIn("target_result_checkpoint_version", json.dumps(self._observations(job["id"])))
        return job, role

    def _assert_free_active_turn_is_observed_as_b(self, job: dict) -> None:
        """AC9(i)·AC10(i): turn active면 OBSERVED·COLLECTOR_LOST 0·PROGRESS{observation} 1·RUNNING, status B."""

        job_id = job["id"]
        other = self._non_owner()
        rows = self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id)
        turns = len(self.runtime.turn_log)
        count = len(self._observations(job_id))
        outcome = self._run_once(other)
        new = self._observations(job_id)[count:]
        self.assertEqual(RunOnceAction.OBSERVED, outcome.action, outcome)
        self.assertEqual([], [kind for kind, _payload in new if kind == "collector_lost"], new)
        self.assertEqual(
            1, len([payload for kind, payload in new if kind == "provider_progress" and "observation" in payload]),
            [kind for kind, _payload in new],
        )
        self.assertEqual("running", self._job_status(job_id))
        before = owner_lease._ledger_copy(self.service)
        verdict = other._unbound_job_verdict(self.service.load_runtime_job(job_id))
        recovery = other.status(self.project_id)["recovery"]
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "status는 원장을 바꾸지 않아야 합니다.")
        self.assertEqual("B", getattr(verdict, "row", None), verdict)
        self.assertEqual(
            ("automatic_pending", "automatic", None),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"]),
            recovery,
        )
        self.assertEqual(turns, len(self.runtime.turn_log), "역할을 다시 부르면 안 됩니다.")
        self.assertEqual(
            rows, self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id),
        )

    def _assert_free_terminal_turn_is_unavailable_then_blocked(self, job: dict) -> None:
        """AC9(ii)·AC10(ii): turn terminal이면 PROVIDER_TERMINAL{observation, unavailable} 뒤 BLOCKED unknown, status 같음."""

        job_id = job["id"]
        other = self._non_owner()
        rows = self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id)
        turns = len(self.runtime.turn_log)
        count = len(self._observations(job_id))
        outcome = self._run_once(other)
        self.assertEqual(RunOnceAction.OBSERVED, outcome.action, outcome)
        terminals = [payload for kind, payload in self._observations(job_id)[count:] if kind == "provider_terminal"]
        self.assertEqual(1, len(terminals), terminals)
        self.assertIn("observation", terminals[0])
        self.assertIs(
            True, terminals[0].get("result", {}).get("runtime_job_result_unavailable"),
            f"target kind의 역할 원문이 결과로 기록됐습니다: result keys="
            f"{sorted(terminals[0].get('result', {}))}",
        )
        blocked = self._run_once(other)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN", FailureClass.EXTERNAL_UNKNOWN,
             RepairAction.WAIT_EXTERNAL),
            (blocked.action, blocked.blocker_code, blocked.failure_class, blocked.suggested_repair_action),
            blocked,
        )
        recovery = other.status(self.project_id)["recovery"]
        self.assertEqual(
            ("observe_first_required", "observe_first", "EXTERNAL_EFFECT_UNKNOWN", blocked.detail[:2000]),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"],
             recovery["next_action"]["detail"]),
        )
        self.assertEqual(turns, len(self.runtime.turn_log), "역할을 다시 부르면 안 됩니다.")
        self.assertEqual(
            rows, self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id),
        )

    def test_ac9i_free_review_job_with_an_active_turn_is_observed_as_b(self) -> None:
        """AC9(i) R5: review job bound·lease FREE·checkpoint 없음·turn active."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=True, active_turn=True)
        self._assert_free_active_turn_is_observed_as_b(job)

    def test_ac9ii_free_review_job_with_a_terminal_turn_is_unavailable_and_blocks(self) -> None:
        """AC9(ii) R6: review job bound·lease FREE·checkpoint 없음·turn terminal."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=True, active_turn=False)
        self._assert_free_terminal_turn_is_unavailable_then_blocked(job)

    def test_ac10i_free_expand_job_with_an_active_turn_is_observed_as_b(self) -> None:
        """AC10(i) R2: expand job에 AC9(i)."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=False, active_turn=True)
        self._assert_free_active_turn_is_observed_as_b(job)

    def test_ac10ii_free_expand_job_with_a_terminal_turn_is_unavailable_and_blocks(self) -> None:
        """AC10(ii) R2: expand job에 AC9(ii). HEAD에서는 역할 원문이 결과가 된다."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=False, active_turn=False)
        self._assert_free_terminal_turn_is_unavailable_then_blocked(job)

    def _job_ledger(self, job_id: str) -> tuple:
        """이 job의 원장 행·관측·history 전체(close 전후 비교용)."""

        return (
            self._rows("SELECT * FROM runtime_jobs WHERE id=?", job_id),
            self._rows("SELECT * FROM runtime_job_observations WHERE job_id=? ORDER BY rowid", job_id),
            self._rows("SELECT * FROM history_events WHERE entity_id=? ORDER BY rowid", job_id),
        )

    def _assert_owner_close_discards_the_leftover_result(self, job: dict) -> None:
        """WU8: 비소유 run_once가 unavailable terminal을 남긴 뒤 끝난 owner worker의 결과를 close()가 버린다.

        정리(cleanup) 순서에 기대지 않는다. hold 해제, owner worker join, owner supervisor close()를 본문에서
        차례로 한다. close() 직전에 worker가 끝났고 결과가 in-memory에 남아 있다는 전제를 먼저 단언해,
        close()가 끝난 worker의 결과를 처리하는 경로를 실제로 지나는지 보인다.
        """

        job_id = job["id"]
        owner = self.application.supervisor
        worker = owner._workers[job_id]
        self.runtime.release()
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive(), f"{_WAIT_SECONDS}초 안에 owner worker가 끝나지 않았습니다.")
        self.assertIn(job_id, owner._results, "close 전 owner worker의 결과가 in-memory에 남아 있어야 합니다.")
        self.assertIn(self._job_status(job_id), {"provider_terminal", "consumed"})
        before = self._job_ledger(job_id)
        digest = self._rows("SELECT result_digest FROM runtime_jobs WHERE id=?", job_id)[0][0]
        terminal_digests = self._rows(
            "SELECT payload_digest FROM runtime_job_observations WHERE job_id=? AND kind='provider_terminal'", job_id,
        )
        try:
            owner.close(timeout_seconds=0.5)
        except EngineServiceError as error:
            self.fail(f"owner supervisor close()가 예외를 냈습니다: {type(error).__name__}: {error}")
        terminals = [payload for kind, payload in self._observations(job_id) if kind == "provider_terminal"]
        self.assertEqual(1, len(terminals), terminals)
        self.assertIs(True, terminals[0].get("result", {}).get("runtime_job_result_unavailable"), terminals[0])
        self.assertEqual(digest, self._rows("SELECT result_digest FROM runtime_jobs WHERE id=?", job_id)[0][0])
        self.assertEqual(terminal_digests, self._rows(
            "SELECT payload_digest FROM runtime_job_observations WHERE job_id=? AND kind='provider_terminal'", job_id,
        ))
        self.assertEqual(before, self._job_ledger(job_id), "close()가 이 job의 원장에 새로 썼습니다.")
        self.assertNotIn(job_id, owner._results)
        self.assertNotIn(job_id, owner._leases)
        lock = runtime_owner_lock_path(self.service, self.project_id, job["checkpoint_key"])
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(lock, holder=object()))

    def test_wu8_ac9ii_owner_close_after_the_unavailable_terminal_discards_the_leftover_review_result(self) -> None:
        """WU8 AC9(ii): review job. close()는 이미 terminal인 job에 남은 결과를 기록하지 않고 예외 없이 끝난다."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=True, active_turn=False)
        self._assert_free_terminal_turn_is_unavailable_then_blocked(job)
        self._assert_owner_close_discards_the_leftover_result(job)

    def test_wu8_ac10ii_owner_close_after_the_unavailable_terminal_discards_the_leftover_expand_result(self) -> None:
        """WU8 AC10(ii): expand job에 같은 검사."""

        job, _role = self._job_with_lost_owner_paused_at_first_read(review=False, active_turn=False)
        self._assert_free_terminal_turn_is_unavailable_then_blocked(job)
        self._assert_owner_close_discards_the_leftover_result(job)


# --- AC17: review 입력 결속 변조 음성 --------------------------------------------------------------


class ReviewInputBindingTests(_ProductRunnerHarness, unittest.TestCase):
    def _stop_before_review(self) -> SimpleNamespace:
        """provider 없는 인스턴스로 expand를 소비해 review 예약 전 REPLAN_PROVIDER_REQUIRED에서 멈춘다."""

        _task_id, _attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        baseline = self._candidate_state()
        calls_before = self._max_provider_call_rowid()
        turns_before = self._queue_c1b_replan()
        scheduled = self._run_once()
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        self._quiesce()
        bare = self._providerless(shared_supervisor=True)
        blocked = self._drive_until_blocked(bare)
        self.assertEqual(
            ("REPLAN_PROVIDER_REQUIRED", REPLAN_PROVIDER_REQUIRED_DETAIL),
            (blocked.blocker_code, blocked.detail), blocked,
        )
        self.assertEqual([(expand_key, "consumed")], self._replan_job_states())
        return SimpleNamespace(
            original=original, stable=stable, expand_key=expand_key, review_key=review_key,
            baseline=baseline, calls_before=calls_before, turns_before=turns_before, bare=bare,
            expand=self._job(expand_key),
        )

    def _assert_no_review_role_and_no_replacement(self, stop: SimpleNamespace) -> None:
        """공통 기대: reviewer 호출 0(role_requested·provider_call 0)·후보 등록·활성화 0·expander 재호출 0."""

        self.assertEqual(["plan_expander"], self._turn_roles(stop.turns_before))
        self.assertEqual([("plan_expander", "terminal", 1)], self._role_calls_since(stop.calls_before))
        review = self._job(stop.review_key)
        if review is not None:
            self.assertEqual([], self._role_progress(review["id"]), "review job이 역할을 요청했습니다.")
        self.assertEqual(stop.baseline, self._candidate_state())

    def _assert_blocked_before_review(self, stop: SimpleNamespace) -> None:
        """(A): provider 있는 인스턴스의 run_once는 BLOCKED EXTERNAL_EFFECT_UNKNOWN(규칙 6)이고 status도 같다."""

        rows = self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id)
        outcome = self._run_once(self.application)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN", FailureClass.EXTERNAL_UNKNOWN,
             RepairAction.WAIT_EXTERNAL, True),
            (outcome.action, outcome.blocker_code, outcome.failure_class, outcome.suggested_repair_action,
             outcome.checkpoint_required),
            outcome,
        )
        self.assertTrue(outcome.detail.startswith(_REVIEW_INPUT_MISMATCH), outcome.detail)
        self.assertIsNone(self._job(stop.review_key))
        self.assertEqual(
            rows, self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id),
        )
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual(
            ("observe_first_required", "observe_first", "EXTERNAL_EFFECT_UNKNOWN", outcome.detail[:2000]),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"],
             recovery["next_action"]["detail"]),
        )
        before = self._ledger()
        again = self._run_once(self.application)
        self.assertEqual((outcome.blocker_code, outcome.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())
        self._assert_no_review_role_and_no_replacement(stop)
        # WU7 N7(b): 행동 요소는 실행 가능한 사실만 말한다. 이 상태의 replan은 REPLAN_RETRY_OBSERVE_FIRST로 거절된다.
        self.assertIn("replan으로는 이 상태가 풀리지 않습니다", outcome.detail)
        self.assertNotIn("revision routing", outcome.detail)
        # T2-N1: 행동 요소는 "replan으로 풀리지 않음" 바로 뒤에 "공개 recovery 명령 없음"이 이어지며 끝난다.
        self.assertTrue(outcome.detail.endswith(
            "replan으로는 이 상태가 풀리지 않습니다. " + runtime_module.REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE,
        ), outcome.detail)
        with self.assertRaises(EngineServiceError) as raised:
            self.application.replan(self.project_id, rationale="M-14 WU7 N7(b) 확인")
        self.assertTrue(str(raised.exception).startswith("REPLAN_RETRY_OBSERVE_FIRST: "), str(raised.exception))
        self.assertEqual(before, self._ledger())

    def test_ac17_a_i_tampered_expand_result_json_blocks_before_review(self) -> None:
        """AC17 (A)×(i): 예약 전 expand 행 result_json만 변조하면 canonical 재계산으로 잡힌다."""

        stop = self._stop_before_review()
        value = json.loads(stop.expand["result_json"])
        tampered = self._shift_plan_created_at(value)
        self.assertNotEqual(sha256_digest(value), sha256_digest(tampered))
        self._set_job_columns(stop.expand["id"], result_json=canonical_json(tampered))
        self._assert_blocked_before_review(stop)

    def test_ac17_a_iii_tampered_result_digest_column_blocks_before_review(self) -> None:
        """AC17 (A)×(iii): 예약 전 result_digest 열만 변조."""

        stop = self._stop_before_review()
        self.assertNotEqual(_OTHER_DIGEST, stop.expand["result_digest"])
        self._set_job_columns(stop.expand["id"], result_digest=_OTHER_DIGEST)
        self._assert_blocked_before_review(stop)

    def test_ac28_iv_replan_retry_while_the_review_is_unreserved_is_rejected_as_review_pending(self) -> None:
        """AC28 (iv): 규칙 6(expand 소비·review 미예약)에서 사용자 replan 재시도는 REPLAN_RETRY_NOT_BLOCKED다.

        원장은 바뀌지 않고, detail은 review 대기를 말하며 '활성화됐거나'를 말하지 않는다.
        """

        stop = self._stop_before_review()
        before = self._ledger()
        with self.assertRaises(EngineServiceError) as raised:
            self.application.replan(self.project_id, rationale="M-14 AC28 (iv): review 대기 중 재시도")
        message = str(raised.exception)
        self.assertTrue(message.startswith("REPLAN_RETRY_NOT_BLOCKED: "), message)
        self.assertIn("review 대기", message)
        self.assertNotIn("활성화됐거나", message)
        # WU7 N7(a): 모르는 것(후보 적격 여부)과 실행 가능한 다음 행동(--role-config가 있는 run-once)을 말한다.
        self.assertIn("후보가 적격인지는 아직 모릅니다", message)
        self.assertIn("--role-config", message)
        self.assertEqual(before, self._ledger())
        self.assertIsNone(self._job(stop.review_key))
        self.assertEqual([(stop.expand_key, "consumed")], self._replan_job_states())
        # T2-N1: 모르는 것 뒤에 행동이 오고, 안내한 행동이 실제로 맞다. 역할 설정이 있는 다음 run-once가
        # review job을 예약한다.
        self.assertLess(message.index("후보가 적격인지는 아직 모릅니다"), message.index("review job을 예약합니다"))
        reserved = self._run_once(self.application)
        self._assert_not_blocked(reserved)
        self.assertIsNotNone(self._job(stop.review_key), self._replan_job_states())
        self._quiesce()

    def _stop_at_review_entry(self, *, lose_owner: bool) -> SimpleNamespace:
        """review job을 예약시키고 그 worker를 review_replan 진입 gate로 세운다(역할 호출 전)."""

        _task_id, _attempt, original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        if lose_owner:
            self._lose_owner_lease_for(review_key)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = getattr(RecoveryPlanProvider, "review_replan", None)

        def gated(provider, *args, **kwargs):
            entered.set()
            if not release.wait(_WAIT_SECONDS):
                raise TimeoutError("review_replan 진입 gate가 제한 시간 안에 풀리지 않았습니다.")
            return real(provider, *args, **kwargs)

        # HEAD에는 review_replan이 없다(create=True). 그때는 gate에 닿지 못한 실제 상태가 실패로 남는다.
        patcher = mock.patch.object(RecoveryPlanProvider, "review_replan", gated, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        baseline = self._candidate_state()
        calls_before = self._max_provider_call_rowid()
        turns_before = self._queue_c1b_replan()
        self._drive_until_event(entered, "review job worker의 review_replan 진입")
        review = self._job(review_key)
        self.assertIsNotNone(review, f"review job 행이 없습니다: {self._replan_job_states()}")
        self.assertEqual("running", review["status"])
        self.assertIsNone(review["thread_id"], "reviewer 역할 호출 전이어야 합니다.")
        return SimpleNamespace(
            original=original, stable=stable, expand_key=expand_key, review_key=review_key,
            baseline=baseline, calls_before=calls_before, turns_before=turns_before, release=release,
            expand=self._job(expand_key), review=review,
        )

    def _assert_preflight_stop_after_reservation(self, stop: SimpleNamespace, *, lose_owner: bool) -> None:
        """(B): RecoveryPlanError → 실패 checkpoint → COLLECTOR_LOST → 5행 RUNTIME_EFFECT_PREFLIGHT_FAILED."""

        review_id = stop.review["id"]
        stop.release.set()
        worker = self.application.supervisor._workers[review_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        # B1: 살아 있는 owner의 tick. B2: owner process가 tick 전에 죽어 새 인스턴스가 이어 간다.
        application = self._non_owner() if lose_owner else self.application
        first = self._run_once(application)
        self.assertEqual(RunOnceAction.OBSERVED, first.action, first)
        self.assertEqual("collector_lost", self._job_status(review_id))
        evidence = runtime_module._unbound_job_evidence(self.service, self.service.load_runtime_job(review_id))
        # v5.1: 5행을 단언하기 전에 효과 근거가 비었는지 먼저 본다. 8행을 기대값으로 받지 않는다.
        self.assertEqual((), evidence.effects, f"5행 진입 선행조건(effects == ()) 위반: {evidence}")
        self.assertIsNotNone(evidence.failure, evidence)
        self.assertIs(False, evidence.failure_retryable, evidence)
        self.assertIn(_REVIEW_INPUT_MISMATCH, json.dumps(evidence.failure, ensure_ascii=False))
        blocked = self._run_once(application)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "RUNTIME_EFFECT_PREFLIGHT_FAILED"), (blocked.action, blocked.blocker_code),
            blocked,
        )
        self.assertIn(_REVIEW_INPUT_MISMATCH, blocked.detail)
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(review_id))
        self.assertEqual("5", getattr(verdict, "row", None), verdict)
        recovery = application.status(self.project_id)["recovery"]
        self.assertEqual(
            ("user_decision_required", "user_decision", "RUNTIME_EFFECT_PREFLIGHT_FAILED"),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"]),
            recovery,
        )
        self.assertIn(_REVIEW_INPUT_MISMATCH, recovery["next_action"]["detail"])
        self.assertTrue(recovery["next_action"]["detail"].startswith(blocked.detail.rstrip(".")))
        # 재시작 0(S·6행 아님).
        kinds = self._kinds(review_id)
        self.assertEqual((1, 0), (kinds.count("started"), kinds.count("collector_reattached")), kinds)
        self.assertEqual(1, self._rows(
            "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type='runtime_job.started'", review_id,
        )[0][0])
        before = self._ledger()
        again = self._run_once(application)
        self.assertEqual((blocked.blocker_code, blocked.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())
        self._assert_no_review_role_and_no_replacement(stop)
        # WU7 N7(b): worker 실패 문구에도 같은 행동 문장이 있다. 이 상태의 replan은 REPLAN_RETRY_NOT_BLOCKED로 거절된다.
        self.assertIn("replan으로는 이 상태가 풀리지 않습니다", json.dumps(evidence.failure, ensure_ascii=False))
        # T2-N1: 사용자에게 보이는 5행 detail에도 "replan으로 풀리지 않음" 뒤에 "공개 recovery 명령 없음"이 이어진다.
        action = "replan으로는 이 상태가 풀리지 않습니다. " + runtime_module.REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE
        self.assertIn(action, json.dumps(evidence.failure, ensure_ascii=False))
        self.assertIn(action, blocked.detail)
        with self.assertRaises(EngineServiceError) as raised:
            application.replan(self.project_id, rationale="M-14 WU7 N7(b) 확인")
        self.assertTrue(str(raised.exception).startswith("REPLAN_RETRY_NOT_BLOCKED: "), str(raised.exception))
        self.assertEqual(before, self._ledger())

    def _tamper_expand_result_json(self, stop: SimpleNamespace) -> None:
        value = json.loads(stop.expand["result_json"])
        self._set_job_columns(stop.expand["id"], result_json=canonical_json(self._shift_plan_created_at(value)))

    def _tamper_review_request_digest(self, stop: SimpleNamespace) -> None:
        request = json.loads(stop.review["request_json"])
        self.assertIn("expand_result_digest", request, sorted(request))
        self.assertNotEqual(_OTHER_DIGEST, request["expand_result_digest"])
        request["expand_result_digest"] = _OTHER_DIGEST
        self._set_job_columns(stop.review["id"], request_json=canonical_json(request))

    def _tamper_result_digest_column(self, stop: SimpleNamespace) -> None:
        self._set_job_columns(stop.expand["id"], result_digest=_OTHER_DIGEST)

    def test_ac17_b1_i_tampered_expand_result_json_after_reservation(self) -> None:
        """AC17 (B1)×(i): 예약 뒤 실행 전 expand result_json 변조. owner tick이 COLLECTOR_LOST를 기록한다."""

        stop = self._stop_at_review_entry(lose_owner=False)
        self._tamper_expand_result_json(stop)
        self._assert_preflight_stop_after_reservation(stop, lose_owner=False)

    def test_ac17_b1_ii_tampered_review_request_digest_after_reservation(self) -> None:
        """AC17 (B1)×(ii): 예약 뒤 실행 전 review request의 expand_result_digest만 변조."""

        stop = self._stop_at_review_entry(lose_owner=False)
        self._tamper_review_request_digest(stop)
        self._assert_preflight_stop_after_reservation(stop, lose_owner=False)

    def test_ac17_b1_iii_tampered_result_digest_column_after_reservation(self) -> None:
        """AC17 (B1)×(iii): 예약 뒤 실행 전 result_digest 열만 변조."""

        stop = self._stop_at_review_entry(lose_owner=False)
        self._tamper_result_digest_column(stop)
        self._assert_preflight_stop_after_reservation(stop, lose_owner=False)

    def test_ac17_b2_i_owner_absent_c_row_records_collector_lost(self) -> None:
        """AC17 (B2)×(i): owner tick 없이(owner lease double) 새 인스턴스의 C행이 COLLECTOR_LOST를 기록한다."""

        stop = self._stop_at_review_entry(lose_owner=True)
        self._tamper_expand_result_json(stop)
        self._assert_preflight_stop_after_reservation(stop, lose_owner=True)

    def test_wu4_q3_active_goal_digest_mismatch_stops_the_review_before_the_role(self) -> None:
        """WU4 Q3: review 역할 전에 active Goal digest가 교체 Plan의 goal digest와 다르면 fail-closed로 멈춘다.

        Goal이 바뀐 상황은 `review_replan` 안에서 읽는 active Goal의 digest만 다른 값으로 돌려주는 test double로
        만든다. 기대는 AC17 (B1)과 같다: 역할 호출 0, 실패 checkpoint → COLLECTOR_LOST → 5행.
        """

        stop = self._stop_at_review_entry(lose_owner=False)
        real = EngineService.load_active_goal

        def changed_goal(service, project_id):
            goal = real(service, project_id)
            if _inside("review_replan"):
                return goal.model_copy(update={"definition_digest": _OTHER_DIGEST})
            return goal

        patcher = mock.patch.object(EngineService, "load_active_goal", changed_goal)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._assert_preflight_stop_after_reservation(stop, lose_owner=False)
        failure = runtime_module._unbound_job_evidence(
            self.service, self.service.load_runtime_job(stop.review["id"]),
        ).failure
        text = json.dumps(failure, ensure_ascii=False)
        self.assertIn("goal_contract_digest", text)
        self.assertIn(_OTHER_DIGEST, text)


# --- AC13·AC18: reader 규칙과 run_once·status 정합 ----------------------------------------------


class ReplanPhaseReadTests(_ProductRunnerHarness, unittest.TestCase):
    def _assert_typed_unknown_in_run_once_and_status(self, *, phase: bool) -> None:
        rows = self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id)
        candidates = self._candidate_state()
        outcome = self._run_once(self.application)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (outcome.action, outcome.blocker_code), outcome,
        )
        if phase:
            self.assertTrue(outcome.detail.startswith(_REVIEW_INPUT_MISMATCH), outcome.detail)
        recovery = self.application.status(self.project_id)["recovery"]
        self.assertEqual(
            ("observe_first_required", "observe_first", "EXTERNAL_EFFECT_UNKNOWN", outcome.detail[:2000]),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"],
             recovery["next_action"]["detail"]),
        )
        self.assertEqual(
            rows, self._rows("SELECT id FROM runtime_jobs WHERE project_id=? ORDER BY rowid", self.project_id),
        )
        self.assertEqual(candidates, self._candidate_state())
        before = self._ledger()
        again = self._run_once(self.application)
        self.assertEqual((outcome.blocker_code, outcome.detail), (again.blocker_code, again.detail))
        self.assertEqual(before, self._ledger())

    def _consumed_expand_row(self, result: dict) -> str:
        """테스트 임시 원장에 손상된 result_json을 가진 소비된 expand 행을 넣는다(역할 호출 없음)."""

        task_id, attempt, _original, stable = self._to_assessment()
        assessment = json.loads(self._rows(
            "SELECT payload_json FROM recovery_assessments WHERE id=?", stable,
        )[0][0])
        job = self.service.schedule_runtime_job(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key=_replan_keys(stable)[0],
            request={"assessment": assessment, "evidence_documents": []},
            absolute_deadline_at=utc_now() + timedelta(minutes=5), attempt_id=attempt, task_id=task_id,
        )
        self.service.start_runtime_job(job.job_id)
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload={"result": result},
            provider_terminal=True, terminal_status="completed",
        )
        self.service.consume_runtime_job(job.job_id)
        return job.job_id

    def test_ac13_malformed_legacy_result_is_typed_unknown_in_run_once_and_status(self) -> None:
        """AC13: replan_phase 없는(legacy 모양) 손상 결과. HEAD는 status automatic_pending·run_once ValidationError."""

        self._consumed_expand_row({"plan": {"tasks": []}, "inspection": {"citations": []}})
        self._assert_typed_unknown_in_run_once_and_status(phase=False)
        # WU7 N7(c): 파싱 실패 detail은 실행 가능한 행동(status 확인)과 replan 거절을 말한다.
        outcome = self._run_once(self.application)
        self.assertIn("status로 같은 판정과 job ID를 확인", outcome.detail)
        self.assertIn("replan은 이 상태에서 거절됩니다", outcome.detail)
        # T2-N1: 안내한 행동이 실제로 맞다. status가 같은 판정과 이 job의 ID를 보인다. 행동 요소 뒤에는
        # "공개 recovery 명령 없음"이 이어지며 끝난다.
        (job_id,), = self._rows(
            "SELECT id FROM runtime_jobs WHERE project_id=? AND kind='replanning'", self.project_id,
        )
        status_detail = self.application.status(self.project_id)["recovery"]["next_action"]["detail"]
        self.assertEqual(outcome.detail[:2000], status_detail)
        self.assertIn(job_id, status_detail)
        self.assertTrue(outcome.detail.endswith(
            "status로 같은 판정과 job ID를 확인하는 것이며, replan은 이 상태에서 거절됩니다. "
            + runtime_module.REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE,
        ), outcome.detail)
        before = self._ledger()
        with self.assertRaises(EngineServiceError) as raised:
            self.application.replan(self.project_id, rationale="M-14 WU7 N7(c) 확인")
        self.assertTrue(str(raised.exception).startswith("REPLAN_RETRY_OBSERVE_FIRST: "), str(raised.exception))
        self.assertEqual(before, self._ledger())

    def test_ac13_malformed_phase_result_is_typed_unknown_in_run_once_and_status(self) -> None:
        """AC13: replan_phase 있는(phase 모양) 손상 결과."""

        self._consumed_expand_row({
            "replan_phase": "expand", "plan": {"tasks": []}, "deterministic_findings": [], "review_required": True,
        })
        self._assert_typed_unknown_in_run_once_and_status(phase=True)

    def test_ac18_ii_malformed_phase_checkpoints_are_typed_unknown_and_never_registered(self) -> None:
        """AC18 (ii)(iii): phase checkpoint 결함마다 typed unknown·run_once=status, 후보 등록·활성화 없음."""

        _task_id, _attempt, _original, stable = self._to_assessment()
        expand_key, review_key = _replan_keys(stable)
        baseline = self._candidate_state()
        self._queue_c1b_replan()
        self.assertEqual(RunOnceAction.DISPATCHED, self._run_once().action)
        self._quiesce()
        bare = self._providerless(shared_supervisor=True)
        stopped = self._drive_until_blocked(bare)
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", stopped.blocker_code, stopped)
        expand = self._job(expand_key)
        original_json, original_digest = expand["result_json"], expand["result_digest"]
        base = json.loads(original_json)
        self.assertEqual("expand", base.get("replan_phase"), sorted(base))
        finding = ReviewFinding(
            finding_code="M14_INJECTED_FINDING", gate=GateName.VERIFICATION, severity=FindingSeverity.ERROR,
            summary="M-14 AC18 주입 finding", evidence_refs=("artifact:plan_contract",), remediable=True,
        ).model_dump(mode="json")
        variants = {
            "extra_key": lambda value: value | {"m14_unexpected": True},
            "review_required_false": lambda value: value | {"review_required": False},
            "finding_present": lambda value: value | {"deterministic_findings": [finding]},
            "plan_parse_failure": lambda value: value | {
                "plan": value["plan"] | {"definition_digest": _OTHER_DIGEST},
            },
        }
        for name, change in variants.items():
            with self.subTest(name):
                changed = change(copy.deepcopy(base))
                self._set_job_columns(
                    expand["id"], result_json=canonical_json(changed), result_digest=sha256_digest(changed),
                )
                self._assert_typed_unknown_in_run_once_and_status(phase=True)
                self.assertIsNone(self._job(review_key))
        with self.subTest("canonical_digest_differs_from_result_digest"):
            self._set_job_columns(
                expand["id"], result_json=canonical_json(self._shift_plan_created_at(base)),
                result_digest=original_digest,
            )
            self._assert_typed_unknown_in_run_once_and_status(phase=True)
            self.assertIsNone(self._job(review_key))
        self._set_job_columns(expand["id"], result_json=original_json, result_digest=original_digest)
        # 대조: 원래 checkpoint는 규칙 6을 통과하므로 provider 없는 인스턴스에서는 review 예약 전 멈춘다.
        control = self._run_once(bare)
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", control.blocker_code, control)
        # (iii) replan_phase 결과는 등록·활성화되지 않는다.
        self.assertEqual(baseline, self._candidate_state())


# --- AC12·AC18(i)·AC22: 보존 ------------------------------------------------------------------


class ReplanLimitParityTests(g1b._ReplanHarness, unittest.TestCase):
    """fm08 scripted harness(역할 progress 없음). HEAD scripted 대조군 값과 같은 수를 본다."""

    def test_ac12_replan_limits_and_provider_calls_are_unchanged(self) -> None:
        """AC12(보존): 같은 실패·Goal 재계획 count, 재계획 provider_call 2, recovery_assessments 행 수."""

        _task_id, attempt = self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        with self.service.ledger.read() as connection:
            calls_before = connection.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM provider_calls WHERE project_id=?", (self.project_id,),
            ).fetchone()[0]
        self._queue_replan(review=g1b._clean_review(), statement=_REPLANNED_STATEMENT)
        self._until_recovered_activation()
        calls = self._rows(
            "SELECT role,execution_status,receipt_json IS NOT NULL FROM provider_calls "
            "WHERE project_id=? AND rowid>? ORDER BY rowid", self.project_id, calls_before,
        )
        self.assertEqual(
            [("plan_expander", "terminal", 1), (RECOVERY_PLAN_REVIEWER_ROLE, "terminal", 1)], calls,
        )
        assessments = [json.loads(row[0]) for row in self._rows(
            "SELECT payload_json FROM recovery_assessments WHERE project_id=? ORDER BY rowid", self.project_id,
        )]
        self.assertEqual(
            [(attempt, "subgraph_replan", 1, 1)],
            [(item["attempt_id"], item["action"], item["same_failure_replan_count"], item["goal_replan_count"])
             for item in assessments],
        )
        fingerprint = assessments[0]["failure_fingerprint"]
        self.assertEqual((1, 1), (
            self._rows(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? AND action='subgraph_replan' "
                "AND json_extract(payload_json,'$.failure_fingerprint')=?", self.project_id, fingerprint,
            )[0][0],
            self._rows(
                "SELECT COUNT(*) FROM recovery_assessments WHERE project_id=? "
                "AND action IN ('subgraph_replan','goal_revision')", self.project_id,
            )[0][0],
        ))
        policy = json.loads(self._rows(
            "SELECT payload_json FROM goal_authorizations WHERE project_id=? ORDER BY revision_no DESC LIMIT 1",
            self.project_id,
        )[0][0])["operating_policy"]
        self.assertEqual((2, 5), (policy["max_same_failure_replans"], policy["max_goal_replans"]))
        self.assertEqual(2, self._plan(self._active_plan_id()).revision_no)


class LegacyFinalReplanTests(unittest.TestCase):
    """AC22·AC18(i): replan()이 replan_phase 없는 최종 evaluation을 돌려주는 fake provider(legacy) 경로."""

    # G1 harness의 메서드만 빌린다(기반 TestCase를 이 모듈 이름으로 두면 그 테스트가 다시 수집된다).
    setUp = g1.ReplanCandidateTests.setUp
    prepared = g1.ReplanCandidateTests.prepared
    _failed_attempt = g1.ReplanCandidateTests._failed_attempt
    _finish_runtime_job_tick = g1.ReplanCandidateTests._finish_runtime_job_tick
    _evaluation = g1.ReplanCandidateTests._evaluation
    _to_replan = g1.ReplanCandidateTests._to_replan
    _assert_replan_blocked = g1.ReplanCandidateTests._assert_replan_blocked
    _assert_replays = g1.ReplanCandidateTests._assert_replays
    _provider = vars(g1.ReplanCandidateTests)["_provider"]
    _count = vars(g1.ReplanCandidateTests)["_count"]
    _ledger = vars(g1.ReplanCandidateTests)["_ledger"]
    _active_plan = vars(g1.ReplanCandidateTests)["_active_plan"]

    @staticmethod
    def _replan_jobs(prepared) -> list[tuple]:
        with prepared.service.ledger.read() as connection:
            return [tuple(row) for row in connection.execute(
                "SELECT checkpoint_key,status,result_json FROM runtime_jobs WHERE project_id=? "
                "AND kind='replanning' ORDER BY rowid", (prepared.project_id,),
            )]

    def _assert_one_legacy_job(self, prepared) -> None:
        jobs = self._replan_jobs(prepared)
        self.assertEqual(1, len(jobs), jobs)
        key, status, result_json = jobs[0]
        self.assertEqual("consumed", status)
        self.assertFalse(key.endswith(":review"), key)
        self.assertNotIn("replan_phase", json.loads(result_json))

    def test_ac22_legacy_final_evaluation_is_one_job_without_review(self) -> None:
        """AC22(보존): legacy final 적격 후보는 job 1개·review 없음으로 활성화된다."""

        prepared, runtime = self.prepared(name="m14-legacy-admissible")
        evaluation = self._evaluation(prepared)
        dispatcher, calls = self._to_replan(prepared, runtime, evaluation)
        activated = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, activated.action, activated)
        self.assertEqual(
            evaluation.plan.plan_revision_id, self._active_plan(prepared.service, prepared.project_id),
        )
        self.assertEqual(1, len(calls))
        self._assert_one_legacy_job(prepared)

    def test_ac18_i_legacy_final_not_admissible_replays_without_review(self) -> None:
        """AC18 (i)(보존): legacy final 비적격 결과는 review 없이 같은 차단을 무변경 재생한다."""

        prepared, runtime = self.prepared(name="m14-legacy-needs-revision")
        evaluation = self._evaluation(prepared, reviewer_finding=True)
        dispatcher, calls = self._to_replan(prepared, runtime, evaluation)
        first = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self._assert_replan_blocked(first, "REPLAN_CANDIDATE_NOT_ADMISSIBLE")
        self._assert_replays(prepared, dispatcher, first)
        self.assertEqual(1, len(calls))
        self._assert_one_legacy_job(prepared)


# --- supervisor 수준 harness(합성 Goal·Plan) ------------------------------------------------------


class _TargetJobHarness(owner_lease._PreparedProject):
    """합성 Goal·Plan 프로젝트에서 bound target kind RuntimeJob을 직접 세운다."""

    def setUp(self) -> None:
        super().setUp()
        self.runtime = _CountingRuntime(self.inventory)

    def _turn(self, *, terminal_response: str | None) -> tuple[str, str]:
        thread = self.runtime.create_thread(
            cwd=self.prepared.workspace, title="m14 target turn", model=_M14_MODEL, developer_instructions="m14",
        )
        turn = self.runtime.start_turn(
            thread_id=thread.binding.thread_id, cwd=self.prepared.workspace, prompt="m14",
            model=_M14_MODEL, effort="high",
        )
        if terminal_response is not None:
            self.runtime.complete(thread.binding.thread_id, response=terminal_response)
        return thread.binding.thread_id, turn.binding.turn_id

    def _bound_service_job(
        self, key: str, *, lock_file: bool, kind: RuntimeJobKind = RuntimeJobKind.REPLANNING,
        terminal_response: str | None = None, task_id: str | None = None,
    ):
        """supervisor 없이 예약·시작한 bound job(owner worker 없음). lock_file이면 lock은 FREE, 아니면 ABSENT다."""

        if lock_file:
            path = self._lock_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        job = self.service.schedule_runtime_job(
            project_id=self.project_id, kind=kind, checkpoint_key=key, request={"m14": key},
            absolute_deadline_at=utc_now() + timedelta(seconds=60), task_id=task_id,
        )
        thread_id, turn_id = self._turn(terminal_response=terminal_response)
        return self.service.start_runtime_job(job.job_id, thread_id=thread_id, turn_id=turn_id), thread_id

    def _checkpointed_job(self, key: str, *, result: dict):
        """owner supervisor가 예약한 bound target job. worker가 성공 checkpoint를 쓰고 lease를 놓았다.

        owner는 tick하지 않는다(결과는 owner의 in-memory `_results`에만 있다). lock 파일은 남아 있고 FREE다.
        """

        owner = self._supervisor(service=self._second_service())
        thread_id, turn_id = self._turn(terminal_response='{"m14_raw_role_output": true}')

        def target():
            owner.bind_provider_turn(active_runtime_job_id(), thread_id=thread_id, turn_id=turn_id)
            return result

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key=key,
            request={"m14": key}, timeout_seconds=60, target=target,
        )
        worker = owner._workers[job.job_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        job = self.service.load_runtime_job(job.job_id)
        self.assertEqual((RuntimeJobStatus.RUNNING, thread_id), (job.status, job.thread_id))
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))
        return owner, job, thread_id

    def _application_for(self, supervisor: RuntimeJobSupervisor, service=None) -> EngineApplication:
        return EngineApplication(
            service or self.service, runtime=supervisor.runtime, supervisor=supervisor, governance=ALLOW_ALL,
        )

    def _payloads(self, job_id: str, kind: str) -> list[dict]:
        return [json.loads(raw) for (raw,) in self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind=? ORDER BY rowid",
            job_id, kind,
        )]

    def _interrupt_trace(self, job_id: str) -> tuple:
        """(INTERRUPT_REQUESTED, INTERRUPT_RECEIPT, COLLECTOR_LOST 관측 수, history interrupt·collector_lost 수)."""

        kinds = self._kinds(job_id)
        return (
            kinds.count("interrupt_requested"), kinds.count("interrupt_receipt"), kinds.count("collector_lost"),
            self._history(job_id, "runtime_job.interrupt_requested"),
            self._history(job_id, "runtime_job.collector_lost"),
        )


# --- AC6·AC7·AC11: durable 결과와 terminal 기록자 -----------------------------------------------


class DurableTargetResultTests(_TargetJobHarness):
    def test_ac6_forced_snapshot_race_returns_no_result_then_the_checkpoint(self) -> None:
        """AC6: runtime_jobs SELECT 뒤·관측 SELECT 전에 worker가 bind+checkpoint를 써도 예외 없이 (False, None)."""

        ledger = _PausingLedger(self.service.ledger.path, artifact_root=self.service.ledger.artifact_root)
        supervisor = self._supervisor(service=EngineService(ledger))
        thread_id, turn_id = self._turn(terminal_response=None)
        go = threading.Event()
        self._gates.append(go)

        def target():
            if not go.wait(_WAIT_SECONDS):
                raise TimeoutError("AC6 hook이 worker를 풀지 않았습니다.")
            supervisor.bind_provider_turn(active_runtime_job_id(), thread_id=thread_id, turn_id=turn_id)
            return {"probe": "m14-ac6"}

        job = supervisor.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key="m14-ac6",
            request={"m14": "ac6"}, timeout_seconds=60, target=target,
        )
        seen: dict[str, bool] = {}

        def binding_and_checkpoint_between_the_two_reads() -> None:
            go.set()
            deadline = time.monotonic() + _WAIT_SECONDS
            while time.monotonic() < deadline:
                if any(item.get("target_result_checkpoint_version") == "1.0"
                       for item in self._payloads(job.job_id, "provider_progress")):
                    seen["checkpoint_durable"] = True
                    return
                time.sleep(0.005)
            seen["checkpoint_durable"] = False

        ledger.armed_thread = threading.get_ident()
        ledger.after_job_select = binding_and_checkpoint_between_the_two_reads
        try:
            first = supervisor._durable_target_result(job.job_id)
        except RuntimePolicyError as error:
            self.fail(f"_durable_target_result가 snapshot 경합에서 예외를 냈습니다: {error}")
        self.assertEqual({"checkpoint_durable": True}, seen, "hook이 두 읽기 사이에서 발동해야 합니다.")
        self.assertEqual((False, None), first)
        self.assertEqual((True, {"probe": "m14-ac6"}), supervisor._durable_target_result(job.job_id))

    def test_ac7_same_supervisor_tick_race_records_the_failure_checkpoint(self) -> None:
        """AC7: `_acquire_owner_lease` double 안에서 worker가 실패로 끝나도 같은 tick이 job_error를 기록한다."""

        owner = self._supervisor()
        thread_id, turn_id = self._turn(terminal_response='{"m14_raw_role_output": true}')
        bound, gate = threading.Event(), threading.Event()
        self._gates.append(gate)

        def target():
            notify_active_runtime_job_progress({
                "event": "turn_started", "role": "plan_expander", "thread_id": thread_id, "turn_id": turn_id,
            })
            notify_active_runtime_job_progress({
                "event": "role_terminal_observed", "role": "plan_expander",
                "terminal_observation": {"thread_id": thread_id, "turn_id": turn_id, "terminal_status": "completed"},
            })
            bound.set()
            if not gate.wait(_WAIT_SECONDS):
                raise TimeoutError("AC7 gate가 풀리지 않았습니다.")
            raise RuntimeError("M14 AC7 역할 terminal 뒤 target 실패")

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key="m14-ac7",
            request={"m14": "ac7"}, timeout_seconds=60, target=target,
        )
        self.assertTrue(bound.wait(_WAIT_SECONDS))
        self.assertEqual(thread_id, self.service.load_runtime_job(job.job_id).thread_id)
        lock = str(self._lock_path("m14-ac7"))
        real = runtime_module._acquire_owner_lease
        fired: list[bool] = []

        def release_worker_then_acquire(path, holder, *, create):
            if holder is owner and not create and str(path) == lock and not fired:
                fired.append(True)
                gate.set()
                owner._workers[job.job_id].join(_WAIT_SECONDS)
            return real(path, holder, create=create)

        with mock.patch.object(runtime_module, "_acquire_owner_lease", release_worker_then_acquire):
            observed = owner.tick(job.job_id)
        self.assertTrue(
            fired,
            f"bound job tick이 `_job_lease`(→ `_acquire_owner_lease`)를 부르지 않아 hook이 발동하지 않았습니다: "
            f"job status={observed.status.value}, observations={self._kinds(job.job_id)}",
        )
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, observed.status)
        terminals = self._payloads(job.job_id, "provider_terminal")
        self.assertEqual(1, len(terminals), terminals)
        self.assertNotIn("observation", terminals[0])
        error = terminals[0]["result"]["job_error"]
        self.assertEqual(("RuntimeError", "M14 AC7 역할 terminal 뒤 target 실패"), (error["error_type"], error["error"]))
        self.assertEqual(0, self.runtime.stored_reads[thread_id])

    def test_ac7_dead_worker_branch_repops_a_result_left_after_the_first_pop(self) -> None:
        """AC7·D5 (2): tick의 첫 pop 뒤 worker가 실패 결과를 남기고 끝나면 dead-worker 분기가 다시 꺼내 기록한다.

        재pop이 없으면 event가 set돼 COLLECTOR_LOST를 건너뛰고 bound 재관측 원문이 결과가 된다(evidence §4.4).
        """

        owner = _PausingDurableReadSupervisor(self.service, self.runtime)
        self._supervisors.append(owner)
        thread_id, turn_id = self._turn(terminal_response='{"m14_raw_role_output": true}')
        bound, gate = threading.Event(), threading.Event()
        self._gates.append(gate)

        def target():
            notify_active_runtime_job_progress({
                "event": "turn_started", "role": "plan_expander", "thread_id": thread_id, "turn_id": turn_id,
            })
            notify_active_runtime_job_progress({
                "event": "role_terminal_observed", "role": "plan_expander",
                "terminal_observation": {"thread_id": thread_id, "turn_id": turn_id, "terminal_status": "completed"},
            })
            bound.set()
            if not gate.wait(_WAIT_SECONDS):
                raise TimeoutError("D5 (2) gate가 풀리지 않았습니다.")
            raise RuntimeError("M14 D5 첫 pop 뒤 target 실패")

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key="m14-d5-dead-worker",
            request={"m14": "d5"}, timeout_seconds=60, target=target,
        )
        self.assertTrue(bound.wait(_WAIT_SECONDS))
        fired: list[bool] = []

        def finish_the_worker_after_the_first_pop() -> None:
            fired.append(True)
            gate.set()
            owner._workers[job.job_id].join(_WAIT_SECONDS)

        owner.before_durable_read = finish_the_worker_after_the_first_pop
        observed = owner.tick(job.job_id)
        self.assertEqual([True], fired, "hook이 tick 안에서 발동하지 않았습니다.")
        self.assertFalse(owner._workers[job.job_id].is_alive())
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, observed.status)
        terminals = self._payloads(job.job_id, "provider_terminal")
        self.assertEqual(1, len(terminals), terminals)
        self.assertNotIn(
            "observation", terminals[0],
            f"재pop 대신 bound 재관측 원문이 결과가 됐습니다: {terminals[0].get('result')}",
        )
        error = terminals[0]["result"]["job_error"]
        self.assertEqual(("RuntimeError", "M14 D5 첫 pop 뒤 target 실패"), (error["error_type"], error["error"]))
        self.assertEqual(0, self.runtime.stored_reads[thread_id])
        self.assertNotIn(job.job_id, owner._results)

    def test_ac11_late_owner_tick_after_a_free_k_reattach_is_deduplicated(self) -> None:
        """AC11(보존): FREE 보유자의 K 재부착 뒤 늦은 owner tick이 예외 없이 같은 terminal, 관측 1건."""

        proxy = _ServiceProxy(self.service)
        owner = self._supervisor(service=proxy)
        result = {"m14": "ac11"}
        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key="m14-ac11",
            request={"m14": "ac11"}, timeout_seconds=60, target=lambda: result,
        )
        owner._workers[job.job_id].join(_WAIT_SECONDS)
        self.assertIn(job.job_id, owner._results, "늦은 owner tick이 기록할 local 결과가 있어야 합니다.")
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path("m14-ac11"), holder=object()))
        holder = self._supervisor()
        reattached: list = []
        # owner tick이 job을 RUNNING으로 읽은 직후, FREE lease 보유자가 K 재부착을 끝낸다.
        proxy.after_tick_load_hook = lambda job_id: reattached.append(holder.tick(job_id))
        late = owner.tick(job.job_id)
        self.assertEqual([RuntimeJobStatus.PROVIDER_TERMINAL], [item.status for item in reattached])
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, late.status)
        self.assertEqual(reattached[0].result_digest, late.result_digest)
        self.assertNotIn(job.job_id, owner._results, "늦은 owner tick은 자기 결과를 dedupe 경로로 기록해야 합니다.")
        terminals = self._payloads(job.job_id, "provider_terminal")
        self.assertEqual(
            [("durable_target_checkpoint", result)],
            [(item.get("result_source"), item.get("result")) for item in terminals],
        )


# --- AC19: bound target kind deadline hard stop ------------------------------------------------


class DeadlineHardStopTests(_TargetJobHarness):
    def test_wu7_n4_replan_progress_text_in_the_7b_state_does_not_promise_reobservation(self) -> None:
        """WU7 N4: 효과 불명 정지 상태 job의 replan 거절 안내는 run-once 재관측을 약속하지 않는다.

        저장된 binding이 있다는 사실과, 조건부로 명시적 observe만 원래 turn을 관측한다는 사실만 말한다.
        """

        clock, supervisor, job, _thread = self._deadline_job("m14-wu7-n4")
        application = self._application_for(supervisor)
        self._to_collector_lost_after_grace(clock, supervisor, job)
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("7b", getattr(verdict, "row", None), verdict)
        with self.service.ledger.transaction() as tx:
            text = EngineService._replan_job_progress_text(
                tx, SimpleNamespace(job_status=RuntimeJobStatus.COLLECTOR_LOST.value, job_id=job.job_id),
            )
        self.assertNotIn("재관측으로 이어", text)
        self.assertIn("저장된 provider binding이나 결과 checkpoint가 있습니다", text)
        self.assertIn("run-once는 다시 관측하지 않고 멈추며 명시적 observe가 원래 turn을 관측합니다", text)
        # T2-N1: "멈춘다"는 약속은 네 조건(owner lock 풀림, 절대 deadline 경과, 관측 유예 경과, interrupt 뒤 turn
        # 종료 미관측)을 모두 단 한 조건문 안에만 있다. 바로 뒤 문장은 그 밖의 상태를 다음 run-once 판정으로 넘기며
        # 문구가 끝난다. 조건을 지우거나(B·K 상태에서 거짓) "그 밖에는" 문장을 지우면 실패한다.
        conditional = re.search(
            r"\. ([^.]*)으면, run-once는 다시 관측하지 않고 멈추며 명시적 observe가 원래 turn을 관측합니다\. "
            r"그 밖에는 다음 run-once가 이 job을 판정합니다\.$",
            text,
        )
        self.assertIsNotNone(conditional, text)
        for factor in (
            "owner lock이 풀려 owner가 없고", "절대 deadline과 관측 유예 시간이 지났으며",
            "interrupt 요청 뒤에도 turn 종료가 관측되지 않았",
        ):
            self.assertIn(factor, conditional.group(1))
        self.assertEqual(1, text.count("멈추"), text)
        # 조건이 모두 성립한 이 상태에서 run-once는 실제로 다시 관측하지 않고 멈춘다.
        kinds = self._kinds(job.job_id)
        self._assert_7b(application, job)
        self.assertEqual(kinds, self._kinds(job.job_id), "run-once가 원래 turn을 다시 관측했습니다.")

    def _deadline_job(self, key: str):
        clock = _Clock()
        supervisor = self._supervisor(clock=clock, interrupt_timeout_seconds=0.5)
        job, thread_id = self._bound_service_job(key, lock_file=True)
        return clock, supervisor, job, thread_id

    def _to_collector_lost_after_grace(self, clock, supervisor, job) -> None:
        clock.value = job.absolute_deadline_at + timedelta(seconds=1)
        self.assertIs(RuntimeJobStatus.INTERRUPTING, supervisor.tick(job.job_id).status)
        clock.value = job.absolute_deadline_at + timedelta(seconds=supervisor.terminal_observation_grace_seconds + 1)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, supervisor.tick(job.job_id).status)
        self.assertEqual((1, 1, 1), self._interrupt_trace(job.job_id)[:3])

    def _assert_7b(self, application, job) -> None:
        blocked = application.run_once(self.project_id)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN", FailureClass.EXTERNAL_UNKNOWN,
             RepairAction.WAIT_EXTERNAL, True),
            (blocked.action, blocked.blocker_code, blocked.failure_class, blocked.suggested_repair_action,
             blocked.checkpoint_required),
            f"{blocked} (job status={self.service.load_runtime_job(job.job_id).status.value}, "
            f"interrupt·collector trace={self._interrupt_trace(job.job_id)})",
        )
        self.assertIsNone(blocked.runtime_job_id)
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("7b", getattr(verdict, "row", None), verdict)
        recovery = application.status(self.project_id)["recovery"]
        self.assertEqual(
            ("observe_first_required", "observe_first", "EXTERNAL_EFFECT_UNKNOWN", "wait_external", True),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"],
             recovery["next_action"]["suggested_repair_action"], recovery["next_action"]["checkpoint_required"]),
            recovery,
        )

    def test_ac19_ab_deadline_interrupt_is_requested_once_like_the_dispatch_kind(self) -> None:
        """AC19 (a)(b)(e)(f): deadline 뒤 INTERRUPT_RECEIPT 1건·INTERRUPTING, 재호출에도 1건, dispatch kind와 같은 관측."""

        clock = _Clock()
        supervisor = self._supervisor(clock=clock, interrupt_timeout_seconds=0.5)
        control, _thread = self._bound_service_job(
            "m14-ac19-dispatch", lock_file=True, kind=RuntimeJobKind.WORKER_TURN, task_id=self.prepared.task_id,
        )
        clock.value = control.absolute_deadline_at + timedelta(seconds=1)
        self.assertIs(RuntimeJobStatus.INTERRUPTING, supervisor.tick(control.job_id).status)
        supervisor.tick(control.job_id)
        control_kinds = self._kinds(control.job_id)
        self.service.cancel_runtime_job(control.job_id, reason="M-14 AC19(f) 대조군 정리")

        job, thread_id = self._bound_service_job("m14-ac19-ab", lock_file=True)
        effects = (self.runtime.create_calls, self.runtime.turn_calls)
        clock.value = job.absolute_deadline_at + timedelta(seconds=1)
        first = supervisor.tick(job.job_id)
        self.assertIs(RuntimeJobStatus.INTERRUPTING, first.status)
        self.assertEqual((1, 1, 0), self._interrupt_trace(job.job_id)[:3])
        second = supervisor.tick(job.job_id)
        self.assertIs(RuntimeJobStatus.INTERRUPTING, second.status)
        self.assertEqual((1, 1, 0), self._interrupt_trace(job.job_id)[:3])
        self.assertEqual(1, self.runtime.interrupts[thread_id])
        self.assertEqual(control_kinds, self._kinds(job.job_id))
        self.assertEqual(effects, (self.runtime.create_calls, self.runtime.turn_calls))

    def test_ac19_c_after_deadline_and_grace_run_once_is_a_typed_7b_stop_without_repeats(self) -> None:
        """AC19 (c)(e): grace 경과 뒤 COLLECTOR_LOST 1건, run_once·status 7b, 추가 tick에 flip·history 증가 없음."""

        clock, supervisor, job, _thread = self._deadline_job("m14-ac19-c")
        effects = (self.runtime.create_calls, self.runtime.turn_calls)
        application = self._application_for(supervisor)
        self._to_collector_lost_after_grace(clock, supervisor, job)
        before = owner_lease._ledger_copy(self.service)
        self._assert_7b(application, job)
        for _ in range(2):
            supervisor.tick(job.job_id)
            again = application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (again.action, again.blocker_code))
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "7b 정지는 원장을 바꾸지 않아야 합니다.")
        self.assertEqual((1, 1, 1, 1, 1), self._interrupt_trace(job.job_id))
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(effects, (self.runtime.create_calls, self.runtime.turn_calls))

    def test_ac19_d_observe_of_an_active_turn_rearms_one_cycle_back_to_7b(self) -> None:
        """AC19 (d)(e): observe 1회 → active면 RUNNING 복귀, 다음 run_once가 한 바퀴만 돌아 다시 7b."""

        clock, supervisor, job, _thread = self._deadline_job("m14-ac19-d-active")
        effects = (self.runtime.create_calls, self.runtime.turn_calls)
        application = self._application_for(supervisor)
        self._to_collector_lost_after_grace(clock, supervisor, job)
        interrupts = self._history(job.job_id, "runtime_job.interrupt_requested")
        observed = application.observe(self.project_id)
        self.assertEqual("running", observed["runtime_job"]["status"], observed["runtime_job"])
        outcomes = []
        for _ in range(3):
            outcomes.append(application.run_once(self.project_id))
            if outcomes[-1].action is RunOnceAction.BLOCKED:
                break
        self.assertIs(
            RunOnceAction.BLOCKED, outcomes[-1].action,
            f"observe 뒤 run_once가 7b로 돌아오지 않았습니다: {[item.action.value for item in outcomes]}, "
            f"history interrupt_requested {interrupts}→{self._history(job.job_id, 'runtime_job.interrupt_requested')}",
        )
        self.assertLessEqual(len(outcomes), 2, [item.action.value for item in outcomes])
        self._assert_7b(application, job)
        self.assertEqual(interrupts + 1, self._history(job.job_id, "runtime_job.interrupt_requested"))
        before = owner_lease._ledger_copy(self.service)
        for _ in range(2):
            again = application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (again.action, again.blocker_code))
        self.assertEqual(before, owner_lease._ledger_copy(self.service))
        self.assertEqual(effects, (self.runtime.create_calls, self.runtime.turn_calls))

    def test_ac19_d_observe_of_a_terminal_turn_is_typed_unavailable(self) -> None:
        """AC19 (d)(e): 7b에서 turn이 terminal이면 observe 1회가 CC-2 unavailable PROVIDER_TERMINAL을 기록한다."""

        clock, supervisor, job, thread_id = self._deadline_job("m14-ac19-d-terminal")
        effects = (self.runtime.create_calls, self.runtime.turn_calls)
        application = self._application_for(supervisor)
        self._to_collector_lost_after_grace(clock, supervisor, job)
        self.runtime.complete(thread_id, response='{"m14_raw_role_output": true}')
        observed = application.observe(self.project_id)
        self.assertEqual("provider_terminal", observed["runtime_job"]["status"], observed["runtime_job"])
        terminals = self._payloads(job.job_id, "provider_terminal")
        self.assertEqual(1, len(terminals), terminals)
        self.assertIn("observation", terminals[0])
        self.assertIs(
            True, terminals[0]["result"].get("runtime_job_result_unavailable"),
            f"target kind의 역할 원문이 결과가 됐습니다: {terminals[0]['result']}",
        )
        with self.assertRaises(ExternalOperationUnknown):
            self.service.consume_runtime_job_required_result(job.job_id)
        self.assertEqual(effects, (self.runtime.create_calls, self.runtime.turn_calls))


# --- AC20: 비소유 관측 --------------------------------------------------------------------------


class NonOwnerObservationTests(_TargetJobHarness):
    # (a) ABSENT 주 사례와 대조군 ----------------------------------------------------------------

    def test_ac20a_absent_bound_job_is_left_untouched_by_run_once_status_observe_tick_and_close(self) -> None:
        """AC20 (a): ABSENT·checkpoint 없음. run_once 0p·status 0p, observe·tick 무등록·무관측, close 0건."""

        job, thread_id = self._bound_service_job("m14-ac20a", lock_file=False)
        self.assertFalse(self._lock_path("m14-ac20a").exists())
        supervisor = self._supervisor()
        application = self._application_for(supervisor)
        before = owner_lease._ledger_copy(self.service)
        outcome = application.run_once(self.project_id)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE", OWNER_PROOF_MISSING_DETAIL, None),
            (outcome.action, outcome.blocker_code, outcome.detail, outcome.runtime_job_id),
            f"{outcome} (observations={self._kinds(job.job_id)})",
        )
        self.assertEqual(before, owner_lease._ledger_copy(self.service))
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("0p", getattr(verdict, "row", None), verdict)
        recovery = application.status(self.project_id)["recovery"]
        self.assertEqual(
            ("observe_first_required", "observe_first", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
            (recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"]),
        )
        application.observe(self.project_id)
        self.assertIs(RuntimeJobStatus.RUNNING, supervisor.tick(job.job_id).status)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "observe·tick이 원장을 바꿨습니다.")
        self.assertNotIn(job.job_id, supervisor._owned_job_ids)
        self.assertEqual(0, self.runtime.stored_reads[thread_id])
        supervisor.close(timeout_seconds=0.1)
        self.assertEqual((0, 0, 0, 0, 0), self._interrupt_trace(job.job_id))
        self.assertEqual(0, self.runtime.interrupts[thread_id])
        self.assertFalse(self._lock_path("m14-ac20a").exists())

    def test_ac20a_absent_collector_lost_observe_is_gated(self) -> None:
        """AC20 (a): ABSENT·COLLECTOR_LOST(bound)면 observe는 gate로 원장을 바꾸지 않는다."""

        job, thread_id = self._bound_service_job("m14-ac20a-lost", lock_file=False)
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "M-14 AC20(a) 소실 흉내"},
        )
        supervisor = self._supervisor()
        application = self._application_for(supervisor)
        before = owner_lease._ledger_copy(self.service)
        observed = application.observe(self.project_id)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), f"observations={self._kinds(job.job_id)}")
        self.assertEqual("collector_lost", observed["runtime_job"]["status"])
        self.assertNotIn(job.job_id, supervisor._owned_job_ids)
        self.assertEqual(0, self.runtime.stored_reads[thread_id])
        supervisor.close(timeout_seconds=0.1)
        self.assertEqual(before, owner_lease._ledger_copy(self.service))

    def test_ac20a_control_free_b_live_registration_is_closed_as_collector_lost(self) -> None:
        """AC20 (a)의 대조군: FREE B(checkpoint 없음)에서 live 경로로 등록된 job은 close() 뒤 COLLECTOR_LOST 1건."""

        job, thread_id = self._bound_service_job("m14-ac20a-control", lock_file=True)
        supervisor = self._supervisor()
        observed = supervisor.tick(job.job_id)
        self.assertIs(RuntimeJobStatus.RUNNING, observed.status)
        self.assertEqual(1, len([item for item in self._payloads(job.job_id, "provider_progress")
                                 if "observation" in item]))
        self.assertIn(job.job_id, supervisor._owned_job_ids)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path("m14-ac20a-control"), holder=object()))
        supervisor.close(timeout_seconds=0.5)
        self.assertEqual(1, self._kinds(job.job_id).count("collector_lost"))
        self.assertEqual(1, self.runtime.stored_reads[thread_id])

    # (a′) K 부 사례 ------------------------------------------------------------------------------

    def _assert_k_reattach_takes_no_ownership(self, key: str, *, absent: bool) -> None:
        result = {"m14": key}
        _owner, job, thread_id = self._checkpointed_job(key, result=result)
        path = self._lock_path(key)
        if absent:
            path.unlink()
        holder = self._supervisor()
        returned = holder.tick(job.job_id)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, returned.status)
        terminals = self._payloads(job.job_id, "provider_terminal")
        self.assertEqual(
            [("durable_target_checkpoint", result)],
            [(item.get("result_source"), item.get("result")) for item in terminals],
        )
        self.assertNotIn(job.job_id, holder._owned_job_ids)
        if absent:
            self.assertFalse(path.exists())
        else:
            self.assertIs(OwnerLockState.FREE, probe_owner_lock(path, holder=object()))
        holder.close(timeout_seconds=0.1)
        self.assertEqual((0, 0, 0, 0, 0), self._interrupt_trace(job.job_id))
        self.assertEqual(0, self.runtime.stored_reads[thread_id])

    def test_ac20a_prime_absent_k_reattach_takes_no_ownership(self) -> None:
        """AC20 (a′) ABSENT: tick(:2609)의 K 재부착은 등록·lock 파일 없이 PROVIDER_TERMINAL 1건."""

        self._assert_k_reattach_takes_no_ownership("m14-ac20a1-absent", absent=True)

    def test_ac20a_prime_free_k_reattach_takes_no_ownership(self) -> None:
        """AC20 (a′) FREE: `_settle_released_owner` K 재부착은 등록 없이 PROVIDER_TERMINAL 1건, tick 뒤 FREE."""

        self._assert_k_reattach_takes_no_ownership("m14-ac20a1-free", absent=False)

    # (b) HELD_OTHER -----------------------------------------------------------------------------

    def _held_other_job(self, key: str):
        owner = self._supervisor(service=self._second_service())
        thread_id, turn_id = self._turn(terminal_response='{"m14_raw_role_output": true}')
        bound, gate = threading.Event(), threading.Event()
        self._gates.append(gate)

        def target():
            owner.bind_provider_turn(active_runtime_job_id(), thread_id=thread_id, turn_id=turn_id)
            bound.set()
            gate.wait(_WAIT_SECONDS)
            return {"m14": "late"}

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key=key,
            request={"m14": key}, timeout_seconds=60, target=target,
        )
        self.assertTrue(bound.wait(_WAIT_SECONDS))
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(self._lock_path(key), holder=object()))
        return job, thread_id

    def test_ac20b_held_other_observe_of_a_running_job_is_read_only(self) -> None:
        """AC20 (b): HELD_OTHER(owner 생존)에서 observe는 원장을 바꾸지 않는다(RUNNING)."""

        job, thread_id = self._held_other_job("m14-ac20b-running")
        supervisor = self._supervisor()
        application = self._application_for(supervisor)
        before = owner_lease._ledger_copy(self.service)
        application.observe(self.project_id)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), f"observations={self._kinds(job.job_id)}")
        self.assertNotIn(job.job_id, supervisor._owned_job_ids)
        self.assertEqual(0, self.runtime.stored_reads[thread_id])

    def test_ac20b_held_other_observe_of_a_collector_lost_job_is_read_only(self) -> None:
        """AC20 (b): HELD_OTHER에서 COLLECTOR_LOST(bound) job의 observe도 원장을 바꾸지 않는다."""

        job, thread_id = self._held_other_job("m14-ac20b-lost")
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "M-14 AC20(b) 소실 흉내"},
        )
        supervisor = self._supervisor()
        application = self._application_for(supervisor)
        before = owner_lease._ledger_copy(self.service)
        observed = application.observe(self.project_id)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), f"observations={self._kinds(job.job_id)}")
        self.assertEqual("collector_lost", observed["runtime_job"]["status"])
        self.assertNotIn(job.job_id, supervisor._owned_job_ids)
        self.assertEqual(0, self.runtime.stored_reads[thread_id])

    # (c) 동시 close() 음성 -----------------------------------------------------------------------

    def _close_inside_the_k_window(self, key: str, *, absent: bool) -> None:
        result = {"m14": key}
        _owner, job, thread_id = self._checkpointed_job(key, result=result)
        path = self._lock_path(key)
        if absent:
            path.unlink()
        proxy = _ServiceProxy(self.service)
        holder = self._supervisor(service=proxy)
        in_window, close_done, tick_done = threading.Event(), threading.Event(), threading.Event()
        seen: dict = {}

        def hold_the_terminal_record(_job_id) -> None:
            in_window.set()
            seen["close_finished_in_window"] = close_done.wait(_WAIT_SECONDS)

        def close_from_another_thread() -> None:
            deadline = time.monotonic() + _WAIT_SECONDS
            while not in_window.wait(0.01):
                if tick_done.is_set() or time.monotonic() >= deadline:
                    seen["window"] = False
                    return
            seen["window"] = True
            seen["owned_before_close"] = job.job_id in holder._owned_job_ids
            seen["probe_in_window"] = probe_owner_lock(path, holder=object())
            try:
                holder.close(timeout_seconds=0.2)
            except BaseException as error:  # noqa: BLE001 - 관측
                seen["close_error"] = repr(error)
            seen["owned_after_close"] = job.job_id in holder._owned_job_ids
            close_done.set()

        proxy.durable_terminal_hook = hold_the_terminal_record
        closer = threading.Thread(target=close_from_another_thread, daemon=True)
        closer.start()
        try:
            returned = holder.tick(job.job_id)
        finally:
            tick_done.set()
            closer.join(_WAIT_SECONDS)
        self.assertEqual(True, seen.get("window"), f"PROVIDER_TERMINAL 기록 창에 닿지 못했습니다: {seen}")
        self.assertEqual(True, seen.get("close_finished_in_window"), seen)
        self.assertNotIn("close_error", seen, seen)
        # (1) interrupt·collector_lost 0, fake runtime interrupt 0. (5) history 0.
        self.assertEqual((0, 0, 0, 0, 0), self._interrupt_trace(job.job_id), self._kinds(job.job_id))
        self.assertEqual(0, self.runtime.interrupts[thread_id])
        # (2) close 전후 모두 미등록.
        self.assertEqual((False, False), (seen["owned_before_close"], seen["owned_after_close"]), seen)
        self.assertNotIn(job.job_id, holder._owned_job_ids)
        # (3) tick 반환은 PROVIDER_TERMINAL{durable_target_checkpoint} 1건.
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, returned.status)
        self.assertEqual(
            [("durable_target_checkpoint", result)],
            [(item.get("result_source"), item.get("result")) for item in self._payloads(job.job_id, "provider_terminal")],
        )
        # (4) lease 보존.
        if absent:
            self.assertIs(OwnerLockState.ABSENT, seen["probe_in_window"])
            self.assertFalse(path.exists())
        else:
            self.assertIs(OwnerLockState.HELD_OTHER, seen["probe_in_window"])
            self.assertIs(OwnerLockState.FREE, probe_owner_lock(path, holder=object()))

    def test_ac20c_close_inside_the_absent_k_window_does_not_interrupt(self) -> None:
        """AC20 (c) ABSENT 주 사례: K 기록 창 안의 동시 close()는 그 job을 건드리지 않는다."""

        self._close_inside_the_k_window("m14-ac20c-absent", absent=True)

    def test_ac20c_close_inside_the_free_k_window_does_not_interrupt(self) -> None:
        """AC20 (c) FREE 부 사례: 창 안에서 다른 holder의 probe는 HELD_OTHER, tick 뒤 FREE."""

        self._close_inside_the_k_window("m14-ac20c-free", absent=False)

    # (d) checkpoint 재조회·기록 실패 음성 -------------------------------------------------------------

    @staticmethod
    def _fail_the_terminal_record(_job_id) -> None:
        raise EngineLedgerError("M-14 AC20(d1) 주입: PROVIDER_TERMINAL 기록 실패")

    def _assert_failed_k_left_no_trace(self, job, thread_id: str, holder, path: Path, *, absent: bool) -> None:
        self.assertIs(RuntimeJobStatus.RUNNING, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual((0, 0, 0, 0, 0), self._interrupt_trace(job.job_id), self._kinds(job.job_id))
        self.assertNotIn(job.job_id, holder._owned_job_ids)
        if absent:
            self.assertFalse(path.exists())
        else:
            self.assertIs(OwnerLockState.FREE, probe_owner_lock(path, holder=object()))
            self.assertNotIn(job.job_id, holder._leases)
        holder.close(timeout_seconds=0.1)
        self.assertEqual((0, 0, 0, 0, 0), self._interrupt_trace(job.job_id), self._kinds(job.job_id))
        self.assertEqual(0, self.runtime.interrupts[thread_id])
        self.assertEqual([], self._payloads(job.job_id, "provider_terminal"))

    def _assert_recovered_after_the_fault(self, job, recover) -> None:
        recover()
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(
            ["durable_target_checkpoint"],
            [item.get("result_source") for item in self._payloads(job.job_id, "provider_terminal")],
        )

    def test_ac20d1_absent_k_record_failure_propagates_without_ownership(self) -> None:
        """AC20 (d1) ABSENT: 기록 실패는 그대로 전파되고 COLLECTOR_LOST·등록 없이 lock 파일도 없다. 뒤 tick이 복구."""

        _owner, job, thread_id = self._checkpointed_job("m14-ac20d1-absent", result={"m14": "d1"})
        path = self._lock_path("m14-ac20d1-absent")
        path.unlink()
        proxy = _ServiceProxy(self.service)
        holder = self._supervisor(service=proxy)
        proxy.durable_terminal_hook = self._fail_the_terminal_record
        with self.assertRaises(EngineLedgerError):
            holder.tick(job.job_id)
        self._assert_failed_k_left_no_trace(job, thread_id, holder, path, absent=True)
        self._assert_recovered_after_the_fault(job, lambda: holder.tick(job.job_id))

    def test_ac20d1_free_k_record_failure_in_run_once_propagates_without_ownership(self) -> None:
        """AC20 (d1) FREE: run_once(`_route_active_unbound` → `_settle_released_owner`)의 기록 실패."""

        _owner, job, thread_id = self._checkpointed_job("m14-ac20d1-free", result={"m14": "d1"})
        proxy = _ServiceProxy(self.service)
        holder = self._supervisor(service=proxy)
        dispatcher = self._dispatcher(holder, service=proxy)
        proxy.durable_terminal_hook = self._fail_the_terminal_record
        with self.assertRaises(EngineLedgerError):
            dispatcher.run_once(self.project_id)
        self._assert_failed_k_left_no_trace(
            job, thread_id, holder, self._lock_path("m14-ac20d1-free"), absent=False,
        )
        self._assert_recovered_after_the_fault(job, lambda: dispatcher.run_once(self.project_id))

    def test_ac20d1_free_k_record_failure_in_observe_propagates_without_ownership(self) -> None:
        """AC20 (d1) FREE: observe 경로의 기록 실패."""

        _owner, job, thread_id = self._checkpointed_job("m14-ac20d1-observe", result={"m14": "d1"})
        proxy = _ServiceProxy(self.service)
        holder = self._supervisor(service=proxy)
        application = self._application_for(holder, service=proxy)
        proxy.durable_terminal_hook = self._fail_the_terminal_record
        with self.assertRaises(EngineLedgerError):
            application.observe(self.project_id)
        self._assert_failed_k_left_no_trace(
            job, thread_id, holder, self._lock_path("m14-ac20d1-observe"), absent=False,
        )
        self._assert_recovered_after_the_fault(job, lambda: application.observe(self.project_id))

    def _corrupt_checkpoint_digest(self, job_id: str) -> None:
        """원장 손상 흉내: 테스트 임시 원장에서만, 한 transaction 안에서 append-only trigger를 빼고
        checkpoint 행을 바꾼 뒤 원래 정의로 다시 만든다. 운영 원장에는 쓰지 않는다."""

        trigger = "tr_engine_job_observation_no_update"
        triggers = "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        with self.service.ledger.transaction() as tx:
            before = [tuple(row) for row in tx.connection.execute(triggers)]
            definition = dict(before)[trigger]
            tx.connection.execute(f"DROP TRIGGER {trigger}")
            changed = tx.connection.execute(
                "UPDATE runtime_job_observations SET payload_json="
                "json_set(payload_json,'$.target_result_digest',?) WHERE job_id=? AND kind='provider_progress' "
                "AND json_extract(payload_json,'$.target_result_checkpoint_version')='1.0'",
                (_OTHER_DIGEST, job_id),
            ).rowcount
            tx.connection.execute(definition)
        self.assertEqual(1, changed)
        # 재생성 뒤 trigger 정의 문자열·개수가 원래와 같다.
        self.assertEqual(before, self._rows(triggers))

    def test_ac20d2_free_k_checkpoint_reread_failure_repeats_without_ownership(self) -> None:
        """AC20 (d2) FREE 주 사례: K(존재만 봄) → 재조회 MISMATCH가 전파되고 복구 없이 반복된다."""

        _owner, job, thread_id = self._checkpointed_job("m14-ac20d2-free", result={"m14": "d2"})
        self._corrupt_checkpoint_digest(job.job_id)
        holder = self._supervisor()
        dispatcher = self._dispatcher(holder)
        path = self._lock_path("m14-ac20d2-free")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_JOB_RESULT_CHECKPOINT_MISMATCH"):
                dispatcher.run_once(self.project_id)
            self.assertIs(RuntimeJobStatus.RUNNING, self.service.load_runtime_job(job.job_id).status)
            self.assertNotIn(job.job_id, holder._owned_job_ids)
            self.assertIs(OwnerLockState.FREE, probe_owner_lock(path, holder=object()))
        self._assert_failed_k_left_no_trace(job, thread_id, holder, path, absent=False)

    def test_ac20d2_absent_k_checkpoint_reread_failure_is_raised_before_reattach(self) -> None:
        """AC20 (d2) ABSENT 보조: tick(:2609)이 reattach 전에 같은 typed 예외를 낸다."""

        _owner, job, thread_id = self._checkpointed_job("m14-ac20d2-absent", result={"m14": "d2"})
        self._corrupt_checkpoint_digest(job.job_id)
        path = self._lock_path("m14-ac20d2-absent")
        path.unlink()
        holder = self._supervisor()
        for _ in range(2):
            with self.assertRaisesRegex(RuntimePolicyError, "RUNTIME_JOB_RESULT_CHECKPOINT_MISMATCH"):
                holder.tick(job.job_id)
        self._assert_failed_k_left_no_trace(job, thread_id, holder, path, absent=True)

    # (e) helper None 직후 terminal 전이 음성 -------------------------------------------------------

    def _terminal_transition_after_helper_none(self, key: str, *, consume: bool) -> None:
        job, thread_id = self._bound_service_job(
            key, lock_file=True, terminal_response='{"m14_raw_role_output": "다른 결과"}',
        )
        barrier = SimpleNamespace(armed=True, helper_done=threading.Event(), transition_done=threading.Event())
        holder = _HelperBarrierSupervisor(self.service, self.runtime)
        holder.barrier = barrier
        self._supervisors.append(holder)
        injected = {"m14": "주입된 terminal 결과"}
        main_returned = threading.Event()
        seen: dict = {}

        def transition_from_another_thread() -> None:
            deadline = time.monotonic() + _WAIT_SECONDS
            while not barrier.helper_done.wait(0.01):
                if main_returned.is_set() or time.monotonic() >= deadline:
                    seen["helper_reached"] = False
                    return
            seen["helper_reached"] = True
            try:
                self.service.record_runtime_job_observation(
                    job.job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload={"result": injected},
                    provider_terminal=True, terminal_status="completed",
                )
                if consume:
                    self.service.consume_runtime_job(job.job_id)
            finally:
                barrier.transition_done.set()

        other = threading.Thread(target=transition_from_another_thread, daemon=True)
        other.start()
        progress = len(self._payloads(job.job_id, "provider_progress"))
        try:
            returned = holder.tick(job.job_id)
        finally:
            main_returned.set()
            other.join(_WAIT_SECONDS)
        self.assertEqual(
            True, seen.get("helper_reached"),
            f"checkpoint 전용 helper가 None을 돌려주는 지점에 닿지 못했습니다: {seen}, "
            f"owned={job.job_id in holder._owned_job_ids}, read_stored={self.runtime.stored_reads[thread_id]}, "
            f"observations={self._kinds(job.job_id)}",
        )
        expected = RuntimeJobStatus.CONSUMED if consume else RuntimeJobStatus.PROVIDER_TERMINAL
        self.assertNotIn(job.job_id, holder._owned_job_ids)
        self.assertEqual((0, 0), (self.runtime.stored_reads[thread_id], self.runtime.reads[thread_id]))
        self.assertEqual(
            [injected], [item.get("result") for item in self._payloads(job.job_id, "provider_terminal")],
        )
        self.assertEqual(0, self._kinds(job.job_id).count("collector_lost"))
        self.assertEqual(progress, len(self._payloads(job.job_id, "provider_progress")))
        self.assertIs(expected, returned.status)
        self.assertEqual(sha256_digest(injected), returned.result_digest)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))
        self.assertNotIn(job.job_id, holder._leases)

    def test_ac20e_provider_terminal_after_helper_none_is_not_reobserved(self) -> None:
        """AC20 (e) 주 사례: helper None 직후 PROVIDER_TERMINAL 전이면 등록·read_stored 없이 그 job을 반환한다."""

        self._terminal_transition_after_helper_none("m14-ac20e-terminal", consume=False)

    def test_ac20e_consumed_after_helper_none_is_not_reobserved(self) -> None:
        """AC20 (e) 부 사례: helper None 직후 CONSUMED 전이."""

        self._terminal_transition_after_helper_none("m14-ac20e-consumed", consume=True)


# --- WU3 결정: bound target C행은 RUNNING·INTERRUPTING에서 한 번만(codex-fm-m14-wu3-boundc-reply-20260924-01) ----


class BoundTargetFailureRowTests(_TargetJobHarness):
    """역할 terminal progress 없이 실패한 bound target job: C는 한 번만 기록하고 다음 tick은 7b 또는 B다."""

    _ERROR = "M14 WU3 역할 terminal 없는 bound 실패"

    def _bound_roleless_failure(self, key: str):
        """owner worker가 binding 뒤 역할 terminal progress 없이 실패했고 owner는 tick하지 않은(소실) job."""

        owner = self._supervisor(service=self._second_service())
        thread_id, turn_id = self._turn(terminal_response=None)
        bound, gate = threading.Event(), threading.Event()
        self._gates.append(gate)

        def target():
            notify_active_runtime_job_progress({
                "event": "turn_started", "role": "plan_expander", "thread_id": thread_id, "turn_id": turn_id,
            })
            bound.set()
            if not gate.wait(_WAIT_SECONDS):
                raise TimeoutError("WU3 gate가 풀리지 않았습니다.")
            raise RuntimeError(self._ERROR)

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key=key,
            request={"m14": key}, timeout_seconds=60, target=target,
        )
        self.assertTrue(bound.wait(_WAIT_SECONDS))
        gate.set()
        owner._workers[job.job_id].join(_WAIT_SECONDS)
        self.assertFalse(owner._workers[job.job_id].is_alive())
        job = self.service.load_runtime_job(job.job_id)
        self.assertEqual((RuntimeJobStatus.RUNNING, thread_id), (job.status, job.thread_id))
        self.assertTrue(any(
            item.get("target_failure_checkpoint_version") == "1.0"
            for item in self._payloads(job.job_id, "provider_progress")
        ))
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))
        return job, thread_id

    def _effects(self) -> tuple[int, int, int]:
        """fake runtime의 provider 효과 호출 수(새 thread·새 turn·resume). observe-first면 변하지 않는다."""

        return self.runtime.create_calls, self.runtime.turn_calls, self.runtime.resume_calls

    def _failure_records(self, job_id: str) -> int:
        return len([item for item in self._payloads(job_id, "collector_lost") if item.get("error") == self._ERROR])

    def _first_tick_records_the_failure_once(self, application, job) -> None:
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("C", getattr(verdict, "row", None), verdict)
        first = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, first.action, first)
        self.assertEqual(
            (RuntimeJobStatus.COLLECTOR_LOST, 1),
            (self.service.load_runtime_job(job.job_id).status, self._failure_records(job.job_id)),
            self._kinds(job.job_id),
        )

    def test_bound_c_records_a_roleless_failure_once_then_b_observes_the_turn(self) -> None:
        """deadline 전: C 한 번 → B(lease 아래 원래 turn 관측) → B 반복은 원장을 바꾸지 않는다."""

        job, thread_id = self._bound_roleless_failure("m14-wu3-boundc-b")
        holder = self._supervisor()
        application = self._application_for(holder)
        effects = self._effects()
        self._first_tick_records_the_failure_once(application, job)
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("B", getattr(verdict, "row", None), verdict)
        second = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, second.action, second)
        self.assertIs(RuntimeJobStatus.RUNNING, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(1, self._failure_records(job.job_id), "C를 반복했습니다.")
        self.assertEqual(1, len([item for item in self._payloads(job.job_id, "provider_progress")
                                 if "observation" in item]))
        before = owner_lease._ledger_copy(self.service)
        third = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, third.action, third)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "같은 상태에서 원장·updated_at이 다시 바뀌었습니다.")
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("B", getattr(verdict, "row", None), verdict)
        # AC28 (i): B 관측 뒤 다음 tick(supervisor tick 직접)도 원장·updated_at을 바꾸지 않는다.
        holder.tick(job.job_id)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "B 뒤 다음 tick이 원장을 바꿨습니다.")
        self.assertEqual(1, self._failure_records(job.job_id))
        self.assertEqual(effects, self._effects())

    def test_bound_c_records_a_roleless_failure_once_then_stops_at_7b(self) -> None:
        """deadline+grace 뒤: C 한 번 → B의 interrupt·grace → 7b 정지, 반복 없음."""

        job, thread_id = self._bound_roleless_failure("m14-wu3-boundc-7b")
        clock = _Clock()
        holder = self._supervisor(clock=clock, interrupt_timeout_seconds=0.5)
        application = self._application_for(holder)
        effects = self._effects()
        self._first_tick_records_the_failure_once(application, job)
        clock.value = job.absolute_deadline_at + timedelta(seconds=holder.terminal_observation_grace_seconds + 1)
        second = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, second.action, second)
        self.assertEqual((1, 1), self._interrupt_trace(job.job_id)[:2])
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        third = application.run_once(self.project_id)
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (third.action, third.blocker_code), third,
        )
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("7b", getattr(verdict, "row", None), verdict)
        before = owner_lease._ledger_copy(self.service)
        fourth = application.run_once(self.project_id)
        self.assertEqual((third.blocker_code, third.detail), (fourth.blocker_code, fourth.detail))
        self.assertEqual(before, owner_lease._ledger_copy(self.service))
        # AC28 (i): 7b 정지 뒤 다음 tick(supervisor tick 직접)도 원장·updated_at을 바꾸지 않는다.
        holder.tick(job.job_id)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "7b 뒤 다음 tick이 원장을 바꿨습니다.")
        self.assertEqual(1, self._failure_records(job.job_id), "C를 반복했습니다.")
        self.assertEqual(effects, self._effects())

    def test_bound_c_after_7b_observe_first_reads_the_original_turn_and_never_repeats_c(self) -> None:
        """WU3b Q2(3): 7b 뒤 명시 observe는 원래 turn을 관측(observe-first)하고, RUNNING 복귀 뒤에도 C는 없다."""

        job, thread_id = self._bound_roleless_failure("m14-wu3b-boundc-observe")
        turn_id = self.service.load_runtime_job(job.job_id).turn_id
        clock = _Clock()
        holder = self._supervisor(clock=clock, interrupt_timeout_seconds=0.5)
        application = self._application_for(holder)
        effects = self._effects()
        self._first_tick_records_the_failure_once(application, job)
        clock.value = job.absolute_deadline_at + timedelta(seconds=holder.terminal_observation_grace_seconds + 1)
        self.assertEqual(RunOnceAction.OBSERVED, application.run_once(self.project_id).action)
        blocked = application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (blocked.action, blocked.blocker_code))
        reads = self.runtime.stored_reads[thread_id]

        observed = application.observe(self.project_id)
        self.assertEqual("running", observed["runtime_job"]["status"], observed["runtime_job"])
        self.assertEqual(reads + 1, self.runtime.stored_reads[thread_id], "원래 thread를 다시 읽지 않았습니다.")
        latest = [item for item in self._payloads(job.job_id, "provider_progress") if "observation" in item][-1]
        self.assertEqual((thread_id, turn_id), (latest["observation"]["thread_id"], latest["observation"]["turn_id"]))
        self.assertEqual(effects, self._effects(), "새 thread·turn을 만들었습니다.")
        self.assertEqual(1, self._failure_records(job.job_id), "observe가 실패를 다시 기록했습니다.")
        verdict = application._unbound_job_verdict(self.service.load_runtime_job(job.job_id))
        self.assertEqual("B", getattr(verdict, "row", None), verdict)

        outcomes = []
        for _ in range(3):
            outcomes.append(application.run_once(self.project_id))
            if outcomes[-1].action is RunOnceAction.BLOCKED:
                break
        self.assertEqual(
            (RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (outcomes[-1].action, outcomes[-1].blocker_code),
            [item.action.value for item in outcomes],
        )
        self.assertEqual("7b", getattr(application._unbound_job_verdict(
            self.service.load_runtime_job(job.job_id)), "row", None))
        self.assertEqual(1, self._failure_records(job.job_id), "RUNNING 복귀 뒤 C를 다시 판정했습니다.")
        self.assertEqual(effects, self._effects())


# --- WU3b Q1: close가 끝난 worker의 결과를 소실로 덮지 않음 ------------------------------------------


class CloseDrainTests(_TargetJobHarness):
    """close() 직전에 worker가 결과를 남기고 끝났으면(tick 전) 그 결과를 tick과 같은 경로로 기록한다.

    CLI owner loop가 tick 반환과 다음 생존 확인 사이에 끝난 worker를 보고 close하는 경합(owner_lease C2)의
    결정적 재현이다. worker 결과는 `_results`(in-memory)에만 있고 원장에는 checkpoint만 있다.
    """

    _FAILURE = "M14 WU3b close drain 실패"

    def _dead_worker_job(self, supervisor: RuntimeJobSupervisor, key: str, target):
        job = self._schedule(supervisor, key, target)
        worker = supervisor._workers[job.job_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertIs(RuntimeJobStatus.RUNNING, self.service.load_runtime_job(job.job_id).status)
        self.assertIn(job.job_id, supervisor._results, "worker 결과가 in-memory에 남지 않았습니다.")
        return job

    def test_close_records_a_dead_workers_pending_success_like_tick(self) -> None:
        """Q1(a): 성공 결과는 tick과 같은 PROVIDER_TERMINAL `{"result"}`이 되고 collector_lost는 남지 않는다."""

        result = {"m14": "close-drain-success"}
        ticked = self._supervisor()
        control = self._dead_worker_job(ticked, "m14-wu3b-close-tick-control", lambda: dict(result))
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, ticked.tick(control.job_id).status)
        tick_payloads = self._payloads(control.job_id, "provider_terminal")
        self.assertEqual(result, self.service.consume_runtime_job_required_result(control.job_id))

        supervisor = self._supervisor()
        job = self._dead_worker_job(supervisor, "m14-wu3b-close-success", lambda: dict(result))
        supervisor.close(timeout_seconds=0.5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, self.service.load_runtime_job(job.job_id).status,
                      self._kinds(job.job_id))
        self.assertEqual(tick_payloads, self._payloads(job.job_id, "provider_terminal"))
        self.assertEqual([{"result": result}], tick_payloads)
        self.assertNotIn("collector_lost", self._kinds(job.job_id))
        self.assertNotIn(job.job_id, supervisor._results)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path("m14-wu3b-close-success"), holder=object()))
        self.assertEqual(result, self.service.consume_runtime_job_required_result(job.job_id))

    def test_close_records_a_dead_workers_pending_failure_once_without_a_second_collector_lost(self) -> None:
        """Q1(b): 실패 결과는 기존 실패 관측으로 한 번 기록되고 close 사유 collector_lost는 덧붙지 않는다."""

        def target():
            raise RuntimeError(self._FAILURE)

        supervisor = self._supervisor()
        job = self._dead_worker_job(supervisor, "m14-wu3b-close-failure", target)
        checkpoints = [item for item in self._payloads(job.job_id, "provider_progress")
                       if item.get("target_failure_checkpoint_version") == "1.0"]
        self.assertEqual(1, len(checkpoints), self._kinds(job.job_id))
        supervisor.close(timeout_seconds=0.5)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual([checkpoints[0]["error"]], self._payloads(job.job_id, "collector_lost"))
        self.assertEqual(self._FAILURE, checkpoints[0]["error"]["error"])
        self.assertNotIn(job.job_id, supervisor._results)

    def test_close_of_a_bound_dead_worker_failure_records_it_then_keeps_interrupt_and_mark(self) -> None:
        """Q1(b) bound: 실패를 먼저 기록한 뒤 기존 close 분기(interrupt 1회, close 사유 표시)를 그대로 한다.

        HEAD의 "tick이 실패를 기록한 뒤 close"와 같은 순서·최종 상태(COLLECTOR_LOST)다.
        """

        supervisor = self._supervisor(interrupt_timeout_seconds=0.5)
        thread_id, turn_id = self._turn(terminal_response=None)

        def target():
            notify_active_runtime_job_progress({
                "event": "turn_started", "role": "plan_expander", "thread_id": thread_id, "turn_id": turn_id,
            })
            raise RuntimeError(self._FAILURE)

        job = supervisor.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.REPLANNING, checkpoint_key="m14-wu3b-close-bound",
            request={"m14": "m14-wu3b-close-bound"}, timeout_seconds=60, target=target,
        )
        worker = supervisor._workers[job.job_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertEqual(thread_id, self.service.load_runtime_job(job.job_id).thread_id)
        self.assertIn(job.job_id, supervisor._results)
        supervisor.close(timeout_seconds=0.5)
        kinds = self._kinds(job.job_id)
        tail = kinds[kinds.index("collector_lost"):] if "collector_lost" in kinds else kinds
        self.assertEqual(["collector_lost", "interrupt_requested", "interrupt_receipt", "collector_lost"], tail, kinds)
        lost = self._payloads(job.job_id, "collector_lost")
        self.assertEqual(self._FAILURE, lost[0].get("error"), lost)
        self.assertEqual(
            {"reason": "supervisor SDK owner closed before provider terminal; worker_alive=False"}, lost[1],
        )
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(1, self.runtime.interrupts[thread_id])

    def test_close_with_a_live_worker_keeps_the_existing_bounded_close(self) -> None:
        """Q1(c) 보존: 살아 있는 worker는 기존처럼 소실 표시 1건만 남고 결과를 기록하지 않으며 lease는 worker가 놓는다."""

        supervisor = self._supervisor()
        target, gate, entered, _calls = self._blocking_target()
        job = self._schedule(supervisor, "m14-wu3b-close-live", target)
        self.assertTrue(entered.wait(_WAIT_SECONDS))
        supervisor.close(timeout_seconds=0.5)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(
            [{"reason": "supervisor SDK owner closed before provider terminal; worker_alive=True"}],
            self._payloads(job.job_id, "collector_lost"),
        )
        self.assertNotIn("provider_terminal", self._kinds(job.job_id))
        self.assertIn(job.job_id, supervisor._leases, "살아 있는 worker의 lease를 close가 놓았습니다.")
        gate.set()
        worker = supervisor._workers[job.job_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertNotIn(job.job_id, supervisor._leases)
        self.assertIn(job.job_id, supervisor._results, "close 뒤 결과는 기존처럼 다음 tick 몫입니다.")
        self.assertNotIn("provider_terminal", self._kinds(job.job_id))

    def _assert_close_discards_the_leftover_result(self, supervisor, job, key: str, status: RuntimeJobStatus):
        """WU8: 이미 끝난 상태(status)의 job에 남은 worker 결과를 close()가 기록하지 않고 버린다."""

        self.assertIs(status, self.service.load_runtime_job(job.job_id).status)
        self.assertIn(job.job_id, supervisor._results, "close 전 worker 결과가 in-memory에 남아 있어야 합니다.")
        before = owner_lease._ledger_copy(self.service)
        try:
            supervisor.close(timeout_seconds=0.5)
        except EngineServiceError as error:
            self.fail(f"close()가 예외를 냈습니다: {type(error).__name__}: {error}")
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "close()가 원장에 새로 썼습니다.")
        self.assertIs(status, self.service.load_runtime_job(job.job_id).status)
        self.assertNotIn(job.job_id, supervisor._results)
        self.assertNotIn(job.job_id, supervisor._leases)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))

    def test_wu8_close_discards_a_leftover_result_of_a_job_another_path_consumed(self) -> None:
        """WU8 단위: 다른 경로가 다른 결과로 terminal을 기록하고 소비한 job(CONSUMED)에 남은 결과.

        제품 경로 대신 검사하는 것: worker와 그 결과는 실제 supervisor worker가 만든다. "다른 경로"의 terminal
        기록과 소비는 같은 원장을 연 두 번째 service로 직접 한다(비소유 run_once의 unavailable 기록과 소비를
        흉내 냄). close()가 terminal 상태를 보고 남은 결과를 기록하지 않고 버리는지만 본다.
        """

        key = "m14-wu8-close-consumed"
        supervisor = self._supervisor()
        job = self._dead_worker_job(supervisor, key, lambda: {"m14": "owner-leftover"})
        other = self._second_service()
        other.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload={"result": {"m14": "other-path"}},
            provider_terminal=True, terminal_status="completed",
        )
        self.assertEqual({"m14": "other-path"}, other.consume_runtime_job(job.job_id))
        self._assert_close_discards_the_leftover_result(supervisor, job, key, RuntimeJobStatus.CONSUMED)

    def test_wu8_close_discards_a_leftover_result_of_a_job_another_path_made_provider_terminal(self) -> None:
        """WU8 단위: 다른 경로가 다른 결과로 terminal만 기록하고 아직 소비하지 않은 job(PROVIDER_TERMINAL).

        제품 경로 대신 검사하는 것: 위 CONSUMED 사례와 같고 소비만 하지 않는다.
        """

        key = "m14-wu8-close-provider-terminal"
        supervisor = self._supervisor()
        job = self._dead_worker_job(supervisor, key, lambda: {"m14": "owner-leftover"})
        self._second_service().record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload={"result": {"m14": "other-path"}},
            provider_terminal=True, terminal_status="completed",
        )
        self._assert_close_discards_the_leftover_result(supervisor, job, key, RuntimeJobStatus.PROVIDER_TERMINAL)

    def test_wu8_close_discards_a_leftover_result_of_a_job_another_path_cancelled(self) -> None:
        """WU8 단위: worker가 결과를 남긴 뒤 다른 경로가 취소한 job(CANCELLED)에 남은 결과.

        제품 경로 대신 검사하는 것: 취소는 같은 원장을 연 두 번째 service의 `cancel_runtime_job`으로 직접 한다.
        close()가 취소된 job을 결과로 덮지 않는지만 본다.
        """

        key = "m14-wu8-close-cancelled"
        supervisor = self._supervisor()
        job = self._dead_worker_job(supervisor, key, lambda: {"m14": "owner-leftover"})
        self._second_service().cancel_runtime_job(job.job_id, reason="M-14 WU8 다른 경로의 취소")
        self._assert_close_discards_the_leftover_result(supervisor, job, key, RuntimeJobStatus.CANCELLED)


# --- WU9: 다른 경로가 끝낸 job에 늦게 남은 worker 결과 --------------------------------------------------


class _InjectedLedgerFault(RuntimeError):
    """WU9 테스트가 주입한 원장 기록 실패(제품 오류와 구별하는 이름)."""


class _CheckpointFaultService:
    """EngineService 위임 proxy(WU9). worker checkpoint 기록만 정해진 표지마다 한 번 실패시킨다.

    ``fail_next``에 넣은 payload 표지(``target_result_checkpoint_version``·``target_failure_checkpoint_version``)를
    가진 PROVIDER_PROGRESS 기록이 오면 그 표지를 빼고 `_InjectedLedgerFault`를 낸다. 나머지는 원래 service에 위임한다.
    """

    def __init__(self, real: EngineService) -> None:
        self._real = real
        self.fail_next: set[str] = set()
        self.fired: list[str] = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def record_runtime_job_observation(self, job_id, *, kind, payload, **kwargs):
        if kind is RuntimeJobObservationKind.PROVIDER_PROGRESS and isinstance(payload, dict):
            for marker in sorted(self.fail_next):
                if payload.get(marker) == "1.0":
                    self.fail_next.discard(marker)
                    self.fired.append(marker)
                    raise _InjectedLedgerFault(f"M-14 WU9 주입: {marker} 기록 실패")
        return self._real.record_runtime_job_observation(job_id, kind=kind, payload=payload, **kwargs)


class CloseLateResultAfterOtherPathTests(_TargetJobHarness):
    """close()가 다른 경로가 끝낸 job(취소·terminal)에 늦게 남은 worker 결과를 다루는 방식(M-14 WU9).

    순서는 F2e-cancelled 계열과 같다. owner worker가 gate에서 기다리는 동안 다른 경로(같은 원장을 연 두 번째
    service)가 job을 취소하거나 terminal을 기록한다. 그 뒤 worker가 끝나고, owner tick은 결과를 꺼내지 않고
    반환하며, 마지막에 owner supervisor의 close()를 부른다. checkpoint 기록 fault는 `_CheckpointFaultService`가
    주입한다. 모든 대기에 상한이 있고 cleanup 순서에 기대지 않는다(close는 본문에서 부른다).
    """

    _LATE_FAILURE = "M14 WU9 다른 경로 뒤 늦은 worker 실패"
    _LATE_SUCCESS = {"m14": "late owner success"}

    def _late_worker_after_other_path(self, key: str, *, fail: bool, other_path, faults=(), role_terminal=False):
        proxy = _CheckpointFaultService(self.service)
        supervisor = self._supervisor(service=proxy)
        gate, entered = threading.Event(), threading.Event()
        self._gates.append(gate)

        def target():
            if role_terminal:
                # 역할 turn의 terminal progress(PROVIDER_PROGRESS)를 먼저 남긴다. 이 표지가 있어도 close가
                # 취소된 job을 PROVIDER_TERMINAL로 덮지 않는지 본다(WU9 조건 3).
                notify_active_runtime_job_progress({
                    "event": "role_terminal_observed", "role": "plan_expander",
                    "terminal_observation": {"terminal_status": "completed"},
                })
            entered.set()
            if not gate.wait(_WAIT_SECONDS):
                raise TimeoutError("WU9 gate가 제한 시간 안에 풀리지 않았습니다.")
            if fail:
                raise RuntimeError(self._LATE_FAILURE)
            return dict(self._LATE_SUCCESS)

        job = self._schedule(supervisor, key, target)
        self.assertTrue(entered.wait(_WAIT_SECONDS), "worker가 gate에 닿지 않았습니다.")
        other_path(self._second_service(), job)
        proxy.fail_next.update(faults)
        gate.set()
        worker = supervisor._workers[job.job_id]
        worker.join(_WAIT_SECONDS)
        self.assertFalse(worker.is_alive(), f"{_WAIT_SECONDS}초 안에 owner worker가 끝나지 않았습니다.")
        self.assertEqual(sorted(faults), sorted(proxy.fired), "주입한 checkpoint fault가 모두 일어나지 않았습니다.")
        status = self.service.load_runtime_job(job.job_id).status
        self.assertIn(status, {
            RuntimeJobStatus.PROVIDER_TERMINAL, RuntimeJobStatus.CONSUMED, RuntimeJobStatus.CANCELLED,
        })
        # tick은 끝난 상태의 job에서 결과를 꺼내지 않고 반환한다(close 전 결과는 in-memory에 남는다).
        self.assertIs(status, supervisor.tick(job.job_id).status)
        self.assertIn(job.job_id, supervisor._results, "close 전 worker 결과가 in-memory에 남아 있어야 합니다.")
        return supervisor, job

    @staticmethod
    def _cancel(other: EngineService, job) -> None:
        other.cancel_runtime_job(job.job_id, reason="M-14 WU9 다른 경로의 취소")

    @staticmethod
    def _record_terminal(other: EngineService, job) -> None:
        other.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.PROVIDER_TERMINAL, payload={"result": {"m14": "other-path"}},
            provider_terminal=True, terminal_status="completed",
        )

    def _close(self, supervisor) -> None:
        try:
            supervisor.close(timeout_seconds=0.5)
        except EngineServiceError as error:
            self.fail(f"close()가 예외를 냈습니다: {type(error).__name__}: {error}")

    def _assert_lease_held_before_close(self, supervisor, job, key: str) -> None:
        """T3-N2: close 직전 lease를 worker가 아직 쥐고 있다. 그래서 close 뒤 FREE는 close가 놓은 것이다."""

        self.assertIn(job.job_id, supervisor._leases, "close 전 lease는 worker가 아직 쥐고 있어야 합니다.")
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(self._lock_path(key), holder=object()))

    def _assert_released_by_close(self, supervisor, job, key: str) -> None:
        self.assertNotIn(job.job_id, supervisor._results)
        self.assertNotIn(job.job_id, supervisor._leases)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))

    def _assert_close_keeps_the_cancelled_failure(self, supervisor, job, key: str, error: dict) -> None:
        """취소된 job에 남은 현재 epoch 실패: COLLECTOR_LOST(error) 1건, 상태 cancelled 유지, 두 번째 close는 무변경."""

        self.assertEqual((False, error), supervisor._results[job.job_id][1])
        progress = self._payloads(job.job_id, "provider_progress")
        self._close(supervisor)
        self.assertEqual([error], self._payloads(job.job_id, "collector_lost"))
        self.assertIs(RuntimeJobStatus.CANCELLED, self.service.load_runtime_job(job.job_id).status)
        self.assertNotIn("provider_terminal", self._kinds(job.job_id))
        self.assertEqual(1, self._history(job.job_id, "runtime_job.collector_lost"))
        self.assertEqual(progress, self._payloads(job.job_id, "provider_progress"), "PROVIDER_PROGRESS 행이 바뀌었습니다.")
        self._assert_released_by_close(supervisor, job, key)
        before = owner_lease._ledger_copy(self.service)
        self._close(supervisor)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "두 번째 close가 원장에 새로 썼습니다.")

    def test_wu9_a_late_failure_of_a_cancelled_job_whose_failure_checkpoint_write_failed_is_kept(self) -> None:
        """WU9 (a) F2e-cancelled-fail-fault·T3-B1: 취소 → worker 실패 → 실패 checkpoint 기록 1회 실패 → tick → close.

        role terminal progress가 있어도 close는 PROVIDER_TERMINAL이 아니라 COLLECTOR_LOST로 적고 그 행을 보존한다.
        """

        key = "m14-wu9-cancelled-fail-fault"
        supervisor, job = self._late_worker_after_other_path(
            key, fail=True, other_path=self._cancel, faults={"target_failure_checkpoint_version"}, role_terminal=True,
        )
        progress = self._payloads(job.job_id, "provider_progress")
        self.assertEqual(
            ["role_terminal_observed"], [item["role_progress"]["event"] for item in progress if "role_progress" in item],
        )
        self.assertEqual([], [item for item in progress if "target_failure_checkpoint_version" in item])
        self._assert_lease_held_before_close(supervisor, job, key)
        self._assert_close_keeps_the_cancelled_failure(
            supervisor, job, key, {"error_type": "RuntimeError", "error": self._LATE_FAILURE},
        )

    def test_wu9_a2_a_cancelled_job_whose_success_and_failure_checkpoint_writes_failed_keeps_the_failure(self) -> None:
        """WU9 (a2) F2e-cancelled-success-fault2(F3b): 성공 결과 checkpoint와 이어진 실패 checkpoint가 모두 1회 실패.

        worker 결과는 성공이 아니라 `stage=target_result_checkpoint` 실패로 남고(runtime.py:2534-2542), close는 그
        실패를 COLLECTOR_LOST로 남긴다.
        """

        key = "m14-wu9-cancelled-success-fault2"
        supervisor, job = self._late_worker_after_other_path(
            key, fail=False, other_path=self._cancel,
            faults={"target_result_checkpoint_version", "target_failure_checkpoint_version"},
        )
        error = supervisor._results[job.job_id][1][1]
        self.assertEqual({
            "error_type": "_InjectedLedgerFault",
            "error": "M-14 WU9 주입: target_result_checkpoint_version 기록 실패",
            "stage": "target_result_checkpoint",
        }, error)
        self.assertEqual([], [
            item for item in self._payloads(job.job_id, "provider_progress")
            if "target_result_checkpoint_version" in item or "target_failure_checkpoint_version" in item
        ])
        self._assert_lease_held_before_close(supervisor, job, key)
        self._assert_close_keeps_the_cancelled_failure(supervisor, job, key, error)

    def test_wu9_b_a_late_failure_of_a_cancelled_job_with_its_failure_checkpoint_is_recorded_once_more(self) -> None:
        """WU9 (b): 실패 checkpoint가 남은 경우. close는 활성 job의 tick·r2 close와 같이 COLLECTOR_LOST를 한 번 더 남긴다.

        checkpoint(PROVIDER_PROGRESS)는 worker의 진행 기록이고 COLLECTOR_LOST는 실패 관측이다. 두 기록의 error는 같다.
        """

        key = "m14-wu9-cancelled-fail"
        supervisor, job = self._late_worker_after_other_path(key, fail=True, other_path=self._cancel)
        checkpoints = [item for item in self._payloads(job.job_id, "provider_progress")
                       if item.get("target_failure_checkpoint_version") == "1.0"]
        self.assertEqual(1, len(checkpoints), self._kinds(job.job_id))
        # 실패 checkpoint가 durable하므로 worker가 이미 lease를 놓았다(runtime.py:2560, 2568-2569).
        self.assertNotIn(job.job_id, supervisor._leases)
        self._assert_close_keeps_the_cancelled_failure(supervisor, job, key, checkpoints[0]["error"])

    def test_wu9_c_a_previous_epoch_failure_of_a_cancelled_job_is_not_recorded(self) -> None:
        """WU9 (c): 다른 경로가 job을 다시 시작(새 epoch)한 뒤 취소했으면 이전 epoch worker의 실패는 기록하지 않는다.

        제품 경로 대신 검사하는 것: 다른 process의 소실 표시·재시작 claim·취소를 두 번째 service로 직접 한다.
        """

        def restarted_then_cancelled(other: EngineService, job) -> None:
            other.record_runtime_job_observation(
                job.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "M-14 WU9 owner 소실 흉내"},
            )
            self.assertTrue(other._claim_runtime_job_start_epoch(job.job_id)[1])
            self._cancel(other, job)

        key = "m14-wu9-cancelled-old-epoch"
        supervisor, job = self._late_worker_after_other_path(key, fail=True, other_path=restarted_then_cancelled)
        epoch, outcome = supervisor._results[job.job_id]
        self.assertIs(False, outcome[0])
        self.assertNotEqual(epoch, supervisor._current_epoch(job.job_id)[1])
        before = owner_lease._ledger_copy(self.service)
        self._close(supervisor)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "이전 epoch 실패를 기록했습니다.")
        self.assertIs(RuntimeJobStatus.CANCELLED, self.service.load_runtime_job(job.job_id).status)
        self._assert_released_by_close(supervisor, job, key)

    def test_wu9_d_a_late_success_of_a_cancelled_job_is_discarded_only_with_its_durable_checkpoint(self) -> None:
        """WU9 (d) 조건 4: `_results`에 남은 성공에는 worker가 기록한 결과 checkpoint가 원장에 있다. close는 그것만 버린다.

        성공이 `_results`에 남는 것은 결과 checkpoint 기록이 예외 없이 끝난 경우뿐이다(runtime.py:2518-2545, 2565).
        그 checkpoint가 durable하면 worker가 lease를 먼저 놓으므로(:2568-2569) close 직전 lease는 이미 FREE다.
        """

        key = "m14-wu9-cancelled-success"
        supervisor, job = self._late_worker_after_other_path(key, fail=False, other_path=self._cancel)
        succeeded, value = supervisor._results[job.job_id][1]
        self.assertIs(True, succeeded)
        self.assertEqual(self._LATE_SUCCESS, value)
        checkpoints = [item for item in self._payloads(job.job_id, "provider_progress")
                       if item.get("target_result_checkpoint_version") == "1.0"]
        self.assertEqual(
            [(value, sha256_digest(value))], [(item["target_result"], item["target_result_digest"]) for item in checkpoints],
        )
        self.assertNotIn(job.job_id, supervisor._leases)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path(key), holder=object()))
        before = owner_lease._ledger_copy(self.service)
        self._close(supervisor)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "취소된 job의 성공 잔여를 기록했습니다.")
        self.assertIs(RuntimeJobStatus.CANCELLED, self.service.load_runtime_job(job.job_id).status)
        self._assert_released_by_close(supervisor, job, key)

    def test_wu9_t3n1_a_late_failure_of_a_provider_terminal_job_is_discarded(self) -> None:
        """WU9 T3-N1·T3-N2: 다른 경로가 terminal을 기록한 job에 남은 실패는 기록하지 않고 버린다.

        실패 checkpoint 기록 fault로 close 직전 lease를 worker가 아직 쥐고 있고, close가 그 lease를 놓는다.
        """

        key = "m14-wu9-provider-terminal-fail-fault"
        supervisor, job = self._late_worker_after_other_path(
            key, fail=True, other_path=self._record_terminal, faults={"target_failure_checkpoint_version"},
        )
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, self.service.load_runtime_job(job.job_id).status)
        self.assertIs(False, supervisor._results[job.job_id][1][0])
        self._assert_lease_held_before_close(supervisor, job, key)
        before = owner_lease._ledger_copy(self.service)
        self._close(supervisor)
        self.assertEqual(before, owner_lease._ledger_copy(self.service), "terminal job에 남은 실패를 기록했습니다.")
        self._assert_released_by_close(supervisor, job, key)


if __name__ == "__main__":
    unittest.main()
