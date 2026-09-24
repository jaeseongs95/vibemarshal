"""G1b r4: RuntimeJob owner lease(Windows msvcrt)와 binding 전 job의 소유 불변조건.

owner lease의 획득·해제·probe, lease → durable CAS → worker 순서, 비소유 호출의 무전이,
lock 파일(owner proof)이 없는 활성 행의 fail-closed, POSIX fail-closed, CLI owner loop 조건을 본다.
실제 process 경계는 이 모듈의 child 진입점을 ``sys.executable``로 띄워 확인한다.
결정적 테스트이며 live qualification이 아니다. 동시 run_once·scheduler Gate의 근거로 쓰지 않는다.
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flowmarshal.engine.runtime as runtime_module
import tests.test_engine_fm08_recovery_integration as fm08
import tests.test_engine_g1b_recovery_path as g1b
from flowmarshal.canonical import sha256_digest
from flowmarshal.engine import cli
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import (
    FailureClass,
    RecoveryAssessment,
    RunOnceAction,
    RunOnceResult,
    RuntimeJobKind,
    RuntimeJobObservationKind,
    RuntimeJobStatus,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.ledger import EngineTransaction, SQLiteEngineLedger
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.recovery_planning import RECOVERY_PLAN_REVIEWER_ROLE, RecoveryPlanProvider
from flowmarshal.engine.roles import CodexStructuredRoleRunner
from flowmarshal.engine.runtime import (
    OWNER_PROOF_MISSING_DETAIL,
    REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE,
    REPLAN_PROVIDER_REQUIRED_DETAIL,
    RUNTIME_OWNER_PLATFORM_UNSUPPORTED,
    EngineDispatcher,
    FakeCodexRuntime,
    OwnerLockState,
    RuntimeJobSupervisor,
    RuntimeOwnerLockUnavailable,
    active_runtime_job_id,
    classify_unbound_runtime_job,
    probe_owner_lock,
    runtime_owner_lock_path,
)
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import inventory
from tests.engine_inspection_helpers import InspectionScriptedRunner
from tests.fixtures.engine.governance.allow import ALLOW_ALL
from tests.test_engine_qualification import qualification_inventory
from tests.test_engine_runtime_job_supervisor import SHORT_TICK_LIMIT_SECONDS


ROOT = Path(__file__).resolve().parents[1]
# 원장 무변경은 이 일곱 표의 사본이 같다는 뜻이다.
_LEDGER_TABLES = (
    "runtime_jobs", "runtime_job_observations", "history_events", "attempts",
    "runtime_intents", "provider_calls", "projects",
)
_REPLAN_STATEMENT = "app.py의 값이 정확히 2인지 실제 파일을 읽어 검사한다."
_FIXED_APP = "def add(left: int, right: int) -> int:\n    return left + right\n"


def _ledger_copy(service: EngineService) -> dict[str, list[tuple]]:
    with service.ledger.read() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _LEDGER_TABLES
        }


def _owner_files(service: EngineService, project_id: str) -> list[str] | None:
    owners = runtime_owner_lock_path(service, project_id, "status").parent
    return sorted(path.name for path in owners.iterdir()) if owners.exists() else None


def _read_only_status(test: unittest.TestCase, application: EngineApplication, project_id: str) -> dict:
    """status 한 번(S3): 원장 7표 사본·runtime-owners 파일 목록·process owner registry가 호출 전후 같다."""

    service = application.service

    def observed() -> tuple:
        return _ledger_copy(service), _owner_files(service, project_id), dict(runtime_module._OWNER_LEASES)

    before = observed()
    status = application.status(project_id)
    test.assertEqual(before, observed())
    return status


def _status_row(status: dict) -> tuple:
    """(recovery.state, next_action.mode, next_action.blocker_code)."""

    recovery = status["recovery"]
    return recovery["state"], recovery["next_action"]["mode"], recovery["next_action"]["blocker_code"]


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _journal(work: Path, entry: str) -> None:
    with (work / "journal.txt").open("a", encoding="utf-8") as handle:
        handle.write(entry + "\n")


def _journal_entries(work: Path) -> list[str]:
    path = work / "journal.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _process_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


# --- child process 진입점 ---------------------------------------------------------------


class _JournalRuntime(FakeCodexRuntime):
    """child owner의 fake runtime. RuntimeJob worker 안의 inventory 조회를 기록하고 release 파일까지 멈춘다."""

    def __init__(self, work: Path, models) -> None:
        super().__init__(models)
        self.work = work

    def list_models(self):
        if active_runtime_job_id() is not None:
            _journal(self.work, "inventory")
            _wait_until((self.work / "release").exists, timeout=60)
        return super().list_models()


class _JournalRunner(InspectionScriptedRunner):
    def __init__(self, work: Path, responses: dict) -> None:
        super().__init__(responses)
        self.work = work

    def run(self, request, *, validator=None):
        _journal(self.work, f"role:{request.role}")
        return super().run(request, validator=validator)


def _run_replanning_owner(db: str, artifacts: str, project_id: str, work: Path) -> int:
    """실제 ``cli.main run-once``로 CLI owner를 돌린다. runtime·application factory만 fake로 바꾼다."""

    os.environ["FLOWMARSHAL_RUNTIME_OWNER_RESULT"] = str(work / "owner-result.json")
    runner = _JournalRunner(work, {
        "plan_expander": [g1b._expansion(_REPLAN_STATEMENT)],
        RECOVERY_PLAN_REVIEWER_ROLE: [g1b._clean_review()],
    })

    def application(arguments, *, runtime=None):
        return EngineApplication(
            cli._service(arguments), runtime=runtime, role_configuration=fm08._roles(),
            structured_runner=runner, governance=ALLOW_ALL,
        )

    with (
        mock.patch.object(cli, "_runtime", lambda _arguments: _JournalRuntime(work, inventory())),
        mock.patch.object(cli, "_application", application),
    ):
        return cli.main(["--db", db, "--artifacts", artifacts, "run-once", "--project-id", project_id])


def _hold_until_release(work: Path, name: str) -> None:
    _journal(work, f"hold:{name}")
    _wait_until((work / "release").exists, timeout=120)


class _SharedRuntime(FakeCodexRuntime):
    """fake thread 상태를 process 사이에서 파일로 나누고 provider 효과(create·start·resume)를 journal에 남긴다.

    ``block``이 ``inventory``(job worker 안의 inventory 조회)나 ``create``(create RPC 안)이면 release 파일까지 멈춘다.
    """

    def __init__(self, work: Path, models, block: str | None = None) -> None:
        super().__init__(models)
        self.work, self.block = work, block
        self._shared_lock = threading.RLock()

    def _sync(self, operation):
        with self._shared_lock:
            path = self.work / "threads.json"
            if path.exists():
                self.threads = {
                    key: runtime_module._FakeThread(
                        thread_id=key, cwd=Path(value["cwd"]), turn_id=value["turn_id"],
                        terminal_status=value["terminal_status"], final_response=value["final_response"],
                        provider_payload=value["provider_payload"],
                    )
                    for key, value in json.loads(path.read_text(encoding="utf-8")).items()
                }
            result = operation()
            _write_json(path, {
                key: {"cwd": str(item.cwd), "turn_id": item.turn_id, "terminal_status": item.terminal_status,
                      "final_response": item.final_response, "provider_payload": item.provider_payload}
                for key, item in self.threads.items()
            })
            return result

    def list_models(self):
        if self.block == "inventory" and active_runtime_job_id() is not None:
            _hold_until_release(self.work, "inventory")
        return super().list_models()

    def create_thread(self, **kwargs):
        _journal(self.work, "create")  # RPC가 시작됐다(효과 수 대조의 기준).
        if self.block == "create":
            _hold_until_release(self.work, "create")
        return self._sync(lambda: FakeCodexRuntime.create_thread(self, **kwargs))

    def start_turn(self, **kwargs):
        _journal(self.work, "start")
        return self._sync(lambda: FakeCodexRuntime.start_turn(self, **kwargs))

    def resume(self, **kwargs):
        _journal(self.work, "resume")
        return self._sync(lambda: FakeCodexRuntime.resume(self, **kwargs))

    def read(self, **kwargs):
        return self._sync(lambda: FakeCodexRuntime.read(self, **kwargs))

    def read_stored(self, **kwargs):
        return self._sync(lambda: FakeCodexRuntime.read_stored(self, **kwargs))

    def interrupt(self, **kwargs):
        return self._sync(lambda: FakeCodexRuntime.interrupt(self, **kwargs))

    def complete(self, thread_id: str, **kwargs) -> None:
        self._sync(lambda: FakeCodexRuntime.complete(self, thread_id, **kwargs))


class _HoldingRunner(InspectionScriptedRunner):
    """``role:<역할>`` block이면 BudgetedRoleRunner 예약 뒤·역할 효과 전에 멈춘다(scripted runner는 progress를 내지 않는다)."""

    def __init__(self, work: Path, responses: dict, block: str | None) -> None:
        super().__init__(responses)
        self.work, self.block = work, block

    def run(self, request, *, validator=None):
        if self.block == f"role:{request.role}":
            _hold_until_release(self.work, self.block)
        _journal(self.work, f"role:{request.role}")
        return super().run(request, validator=validator)


def _run_owner(db: str, artifacts: str, project_id: str, work: Path) -> int:
    """``work/child.json`` 설정대로 실제 ``cli.main run-once`` owner를 돌리고 지정 지점에서 멈춘다(부모가 kill한다).

    block: ``inventory``·``create``(runtime), ``role:<역할>``(scripted runner), ``hit:<fault 지점>``
    (EngineDispatcher._hit), ``claim``(job 행 insert 뒤 시작 claim 전).
    """

    config = json.loads((work / "child.json").read_text(encoding="utf-8"))
    os.environ["FLOWMARSHAL_RUNTIME_OWNER_RESULT"] = str(work / "owner-result.json")
    block = config.get("block")
    models = qualification_inventory() if config.get("qualification") else inventory()
    shared = _SharedRuntime(work, models, block)
    runner = (
        _HoldingRunner(work, config.get("responses", {}), block)
        if config.get("runner") == "scripted" else None  # None이면 제품 CodexStructuredRoleRunner다.
    )
    roles = fm08._roles() if config.get("roles") else None

    def application(arguments, *, runtime=None):
        return EngineApplication(
            cli._service(arguments), runtime=runtime, role_configuration=roles,
            structured_runner=runner, governance=ALLOW_ALL,
        )

    real_hit = EngineDispatcher._hit

    def hit(dispatcher, point: str) -> None:
        if block == f"hit:{point}":
            _hold_until_release(work, block)
        real_hit(dispatcher, point)

    def claim(_service, _job_id):
        _hold_until_release(work, "claim")
        raise RuntimeError("claim hold released")

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(cli, "_runtime", lambda _arguments: shared))
        stack.enter_context(mock.patch.object(cli, "_application", application))
        stack.enter_context(mock.patch.object(EngineDispatcher, "_hit", hit))
        if block == "claim":
            stack.enter_context(mock.patch.object(EngineService, "_claim_runtime_job_start_epoch", claim))
        return cli.main(["--db", db, "--artifacts", artifacts, "run-once", "--project-id", project_id])


def _child_main(argv: list[str]) -> int:
    import flowmarshal.engine

    origin = Path(flowmarshal.engine.__file__).resolve()
    if ROOT not in origin.parents:
        print(f"import origin mismatch: {origin}", file=sys.stderr)
        return 3
    scenario, *rest = argv
    if scenario == "origin":
        print(origin)
        return 0
    if scenario in {"hold", "hold-grandchild"}:
        path, ready = Path(rest[0]), Path(rest[1])
        state, _lease = runtime_module._acquire_owner_lease(path, object(), create=True)
        if state is not OwnerLockState.FREE:
            return 4
        info: dict = {"origin": str(origin)}
        if scenario == "hold-grandchild":
            # close_fds=False: 상속 가능한 handle은 손자에게 넘어간다. lock fd는 상속되지 않아야 한다.
            grandchild = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"], close_fds=False,
            )
            info["grandchild_pid"] = grandchild.pid
        _write_json(ready, info)
        while True:
            time.sleep(0.05)
    if scenario == "replan-owner":
        return _run_replanning_owner(rest[0], rest[1], rest[2], Path(rest[3]))
    if scenario == "owner":
        return _run_owner(rest[0], rest[1], rest[2], Path(rest[3]))
    return 5


def _child_env() -> dict[str, str]:
    environment = os.environ.copy()
    # child가 공유 venv의 editable 설치(main checkout)를 import하지 않게 이 worktree를 앞에 둔다.
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    environment["FLOWMARSHAL_ENGINE_SOURCE_ROOT"] = str(ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.pop("FLOWMARSHAL_RUNTIME_OWNER_RESULT", None)
    return environment


class _ChildProcessMixin:
    def _spawn(self, work: Path, scenario: str, *arguments) -> subprocess.Popen:
        log = (work / f"{scenario}.log").open("wb")
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), scenario, *map(str, arguments)],
            env=_child_env(), cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )

        def stop() -> None:
            if process.poll() is None:
                process.kill()
            process.wait(10)
            log.close()

        self.addCleanup(stop)
        return process

    def _child_log(self, work: Path, scenario: str) -> str:
        path = work / f"{scenario}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


# --- lease 단위(10.1) -------------------------------------------------------------------


class OwnerLeaseUnitTests(_ChildProcessMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.path = self.work / "runtime-owners" / "job.lock"

    def test_harness_child_imports_this_worktree(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "origin"],
            env=_child_env(), cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        origin = Path(completed.stdout.strip())
        self.assertIn(ROOT, origin.parents)

    def test_l1_registry_conflict_is_held_other_without_opening_the_file(self) -> None:
        first, second = object(), object()
        state, lease = runtime_module._acquire_owner_lease(self.path, first, create=True)
        self.assertIs(OwnerLockState.FREE, state)
        with mock.patch.object(runtime_module, "_os_owner_lock", wraps=runtime_module._os_owner_lock) as opened:
            other, holder = runtime_module._acquire_owner_lease(self.path, second, create=True)
            again, _same = runtime_module._acquire_owner_lease(self.path, first, create=True)
        self.assertEqual((OwnerLockState.HELD_OTHER, OwnerLockState.HELD_SELF), (other, again))
        self.assertIs(lease, holder)
        self.assertEqual(0, opened.call_count)
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(self.path))
        runtime_module._release_owner_lease(lease)
        runtime_module._release_owner_lease(lease)  # 멱등
        self.assertTrue(lease.settled.is_set())
        state, lease = runtime_module._acquire_owner_lease(self.path, second, create=False)
        self.assertIs(OwnerLockState.FREE, state)
        runtime_module._release_owner_lease(lease)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self.path))

    def test_l2_raw_backend_refuses_a_second_handle_in_the_same_process(self) -> None:
        state, fd = runtime_module._os_owner_lock(self.path, create=True)
        self.assertIs(OwnerLockState.FREE, state)
        self.assertEqual((OwnerLockState.HELD_OTHER, None), runtime_module._os_owner_lock(self.path, create=False))
        runtime_module._os_owner_unlock(fd)
        state, fd = runtime_module._os_owner_lock(self.path, create=False)
        self.assertIs(OwnerLockState.FREE, state)
        runtime_module._os_owner_unlock(fd)
        missing = self.work / "runtime-owners" / "missing.lock"
        self.assertEqual((OwnerLockState.ABSENT, None), runtime_module._os_owner_lock(missing, create=False))
        self.assertFalse(missing.exists())

    def _hold_in_child(self, scenario: str) -> tuple[subprocess.Popen, dict]:
        ready = self.work / f"{scenario}.ready.json"
        process = self._spawn(self.work, scenario, self.path, ready)
        self.assertTrue(
            _wait_until(lambda: ready.exists() or process.poll() is not None, 60),
            self._child_log(self.work, scenario),
        )
        self.assertTrue(ready.exists(), self._child_log(self.work, scenario))
        info = json.loads(ready.read_text(encoding="utf-8"))
        self.assertIn(ROOT, Path(info["origin"]).parents)
        return process, info

    def _acquired_within(self, seconds: float) -> bool:
        acquired: list = []

        def attempt() -> bool:
            state, lease = runtime_module._acquire_owner_lease(self.path, self, create=False)
            if state is OwnerLockState.FREE:
                acquired.append(lease)
                return True
            return False

        if not _wait_until(attempt, seconds, 0.01):
            return False
        runtime_module._release_owner_lease(acquired[0])
        return True

    def test_l3_killed_child_holder_is_released_by_the_os(self) -> None:
        process, _info = self._hold_in_child("hold")
        state, lease = runtime_module._acquire_owner_lease(self.path, self, create=False)
        self.assertEqual((OwnerLockState.HELD_OTHER, None), (state, lease))
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(self.path))
        process.kill()
        process.wait(10)
        self.assertTrue(self._acquired_within(5.0))

    def test_l4_grandchild_does_not_inherit_the_lock_handle(self) -> None:
        process, info = self._hold_in_child("hold-grandchild")
        pid = info["grandchild_pid"]
        self.addCleanup(subprocess.run, ["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(self.path))
        process.kill()
        process.wait(10)
        self.assertTrue(self._acquired_within(5.0))
        self.assertTrue(_process_alive(pid))


# --- 활성 Plan이 있는 합성 프로젝트 ----------------------------------------------------


class _PreparedProject(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        workspace, _ = _copy_fixture(ROOT, self.base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(
            workspace=workspace, state_root=self.base / "state",
            inventory=self.inventory, roles=default_role_configuration(ROOT),
        )
        self.service = self.prepared.service
        self.project_id = self.prepared.project_id
        self.runtime = FakeCodexRuntime(self.inventory)
        self._gates: list[threading.Event] = []
        self._supervisors: list[RuntimeJobSupervisor] = []
        self.addCleanup(self._release_all)

    def _release_all(self) -> None:
        for gate in self._gates:
            gate.set()
        workers = [worker for item in self._supervisors for worker in item._workers.values()]
        deadline = time.monotonic() + 10
        for worker in workers:
            if worker.ident is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
        for item in self._supervisors:
            item.close(timeout_seconds=0.1)

    def _supervisor(self, service: EngineService | None = None, runtime=None, **kwargs) -> RuntimeJobSupervisor:
        supervisor = RuntimeJobSupervisor(service or self.service, runtime or self.runtime, **kwargs)
        self._supervisors.append(supervisor)
        return supervisor

    def _second_service(self) -> EngineService:
        """같은 원장을 여는 다른 service(다른 process 흉내가 아니라 같은 process의 두 번째 연결)."""

        service = EngineService(SQLiteEngineLedger(
            self.service.ledger.path, artifact_root=self.service.ledger.artifact_root,
        ))
        service.initialize()
        return service

    def _blocking_target(self):
        gate, entered = threading.Event(), threading.Event()
        self._gates.append(gate)
        calls: list[int] = []

        def target():
            entered.set()
            calls.append(1)
            gate.wait(10)
            return {"probe": "done"}

        return target, gate, entered, calls

    def _rows(self, query: str, *parameters) -> list[tuple]:
        with self.service.ledger.read() as connection:
            return [tuple(row) for row in connection.execute(query, parameters)]

    def _kinds(self, job_id: str) -> list[str]:
        return [kind for (kind,) in self._rows(
            "SELECT kind FROM runtime_job_observations WHERE job_id=? ORDER BY rowid", job_id,
        )]

    def _history(self, job_id: str, event_type: str) -> int:
        return self._rows(
            "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type=?", job_id, event_type,
        )[0][0]

    def _lock_path(self, checkpoint: str) -> Path:
        return runtime_owner_lock_path(self.service, self.project_id, checkpoint)

    def _service_job(self, checkpoint: str, *, lock_file: bool, deadline_seconds: float = 60.0,
                     start: bool = True):
        """service 수준 예약(supervisor 없는 예약·구버전 owner 흉내). lock_file이면 r4 owner가 만든 파일을 둔다."""

        if lock_file:
            path = self._lock_path(checkpoint)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        job = self.service.schedule_runtime_job(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key=checkpoint,
            request={"probe": checkpoint}, absolute_deadline_at=utc_now() + timedelta(seconds=deadline_seconds),
        )
        return self.service.start_runtime_job(job.job_id) if start else job

    def _dispatcher(self, supervisor: RuntimeJobSupervisor, service: EngineService | None = None, **kwargs):
        return EngineDispatcher(service or self.service, supervisor.runtime, supervisor=supervisor, **kwargs)

    def _schedule(self, supervisor: RuntimeJobSupervisor, checkpoint: str, target, *, timeout: float = 60):
        return supervisor.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key=checkpoint,
            request={"probe": checkpoint}, timeout_seconds=timeout, target=target,
        )

    def _hold_both_at_scheduled(self, services, checkpoint: str) -> list[float]:
        """두 호출자가 모두 SCHEDULED 행을 받은 뒤에야 시작 단계로 넘어가게 한다. 통과 시각을 돌려준다."""

        barrier = threading.Barrier(len(services), timeout=10)
        passed: list[float] = []
        for service in services:
            real = service.schedule_runtime_job

            def both_scheduled(_real=real, **kwargs):
                job = _real(**kwargs)
                if kwargs["checkpoint_key"] == checkpoint:
                    barrier.wait()
                    passed.append(time.monotonic())
                return job

            service.schedule_runtime_job = both_scheduled
            self.addCleanup(setattr, service, "schedule_runtime_job", real)
        return passed


class OwnerLeaseProjectTests(_PreparedProject):
    # --- 10.1 ---------------------------------------------------------------------------

    def test_l5_owner_lock_directory_error_fails_closed_before_any_job_row(self) -> None:
        owners = self._lock_path("l5").parent
        owners.parent.mkdir(parents=True, exist_ok=True)
        owners.write_text("일반 파일", encoding="utf-8")
        before = _ledger_copy(self.service)
        target_calls: list[int] = []
        supervisor = self._supervisor()
        with self.assertRaises(RuntimeOwnerLockUnavailable):
            self._schedule(supervisor, "l5", lambda: target_calls.append(1))
        self.assertEqual([], self._rows("SELECT id FROM runtime_jobs WHERE checkpoint_key='l5'"))
        self.assertEqual([], target_calls)
        self.assertEqual(before, _ledger_copy(self.service))

        blocked = self._dispatcher(supervisor).run_once(self.project_id, proposal=self.prepared.proposal)
        self.assertEqual((RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         (blocked.action, blocked.blocker_code), blocked)
        self.assertEqual([], self._rows("SELECT id FROM runtime_jobs"))

        proposal_file = self.base / "proposal.json"
        proposal_file.write_text(self.prepared.proposal.model_dump_json(), encoding="utf-8")
        result_file = self.base / "l5-owner.json"
        with (
            mock.patch.dict(os.environ, {"FLOWMARSHAL_RUNTIME_OWNER_RESULT": str(result_file)}),
            mock.patch.object(cli, "_runtime", lambda _arguments: FakeCodexRuntime(self.inventory)),
            mock.patch.object(cli, "_application", lambda arguments, *, runtime=None: EngineApplication(
                cli._service(arguments), runtime=runtime, governance=ALLOW_ALL)),
        ):
            code = cli.main([
                "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                "run-once", "--project-id", self.project_id, "--proposal-file", str(proposal_file),
            ])
        self.assertEqual(0, code)
        published = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(("blocked", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         (published["action"], published["blocker_code"]))
        self.assertEqual([], self._rows("SELECT id FROM runtime_jobs"))

        # 비소유 tick·run_once: lock 계층 오류에서는 원장을 바꾸지 않는다.
        job = self._service_job("l5-other", lock_file=False)
        before = _ledger_copy(self.service)
        self.assertIs(RuntimeJobStatus.RUNNING, self._supervisor().tick(job.job_id).status)
        again = self._dispatcher(self._supervisor()).run_once(self.project_id)
        self.assertEqual("RUNTIME_OWNER_LOCK_UNAVAILABLE", again.blocker_code, again)
        self.assertEqual(before, _ledger_copy(self.service))

    def test_l6_lock_path_comes_from_the_ledger_not_the_process_spelling(self) -> None:
        checkpoint = "l6:path"
        database = self.service.ledger.path.resolve()
        swapped = str(database)[0].swapcase() + str(database)[1:]
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.base)
        relative = Path(os.path.relpath(database, self.base))
        services = [self.service]
        for path, artifacts in ((swapped, self.base / "other-artifacts"), (relative, Path("rel-artifacts"))):
            service = EngineService(SQLiteEngineLedger(path, artifact_root=artifacts))
            service.initialize()
            services.append(service)
        paths = {str(runtime_owner_lock_path(item, self.project_id, checkpoint)) for item in services}
        self.assertEqual(1, len(paths))
        artifact_root = self._rows("SELECT artifact_root FROM projects WHERE id=?", self.project_id)[0][0]
        self.assertTrue(paths.pop().startswith(str(Path(artifact_root) / "runtime-owners")))
        holder = object()
        state, lease = runtime_module._acquire_owner_lease(
            runtime_owner_lock_path(services[1], self.project_id, checkpoint), holder, create=True,
        )
        self.assertIs(OwnerLockState.FREE, state)
        try:
            # registry를 우회한 OS 수준 확인: 다른 표기의 service가 계산한 경로도 같은 파일이다.
            self.assertEqual(
                (OwnerLockState.HELD_OTHER, None),
                runtime_module._os_owner_lock(runtime_owner_lock_path(services[2], self.project_id, checkpoint),
                                              create=False),
            )
        finally:
            runtime_module._release_owner_lease(lease)

    # --- 10.2 ---------------------------------------------------------------------------

    def test_o1_the_start_cas_runs_while_the_caller_holds_the_lease(self) -> None:
        supervisor = self._supervisor()
        path = str(self._lock_path("o1"))
        holders: list = []
        real = self.service._claim_runtime_job_start_in_transaction

        def spy(tx, job_id, **kwargs):
            lease = runtime_module._OWNER_LEASES.get(path)
            holders.append(None if lease is None else lease.holder)
            return real(tx, job_id, **kwargs)

        self.service._claim_runtime_job_start_in_transaction = spy
        self.addCleanup(setattr, self.service, "_claim_runtime_job_start_in_transaction", real)
        job = self._schedule(supervisor, "o1", lambda: {"ok": True})
        self.assertEqual([supervisor], holders)
        supervisor._workers[job.job_id].join(5)
        self.assertNotIn(path, runtime_module._OWNER_LEASES)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, supervisor.tick(job.job_id).status)

    def test_o2_b1c_two_scheduled_callers_with_a_non_exclusive_lock_start_one_worker(self) -> None:
        other = self._second_service()
        supervisors = [self._supervisor(), self._supervisor(other)]
        target, gate, _entered, calls = self._blocking_target()
        self._hold_both_at_scheduled((self.service, other), "o2")

        def non_exclusive(path, holder, *, create):
            return OwnerLockState.FREE, runtime_module._OwnerLease(str(path), holder, None)

        jobs: dict[int, object] = {}

        def schedule(index: int) -> None:
            try:
                jobs[index] = self._schedule(supervisors[index], "o2", target)
            except BaseException as error:  # noqa: BLE001 - 동시 호출 결과 관측
                jobs[index] = error

        with mock.patch.object(runtime_module, "_acquire_owner_lease", non_exclusive):
            threads = [threading.Thread(target=schedule, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        job_id = jobs[0].job_id
        owners = [item for item in supervisors if job_id in item._workers]
        self.assertEqual(1, len(owners), jobs)
        loser = next(item for item in supervisors if item is not owners[0])
        for state in (loser._owned_job_ids, loser._result_events, loser._complete_on_return,
                      loser._results, loser._leases):
            self.assertNotIn(job_id, state)
        gate.set()
        owners[0]._workers[job_id].join(10)
        self.assertEqual([1], calls)
        self.assertEqual(1, self._kinds(job_id).count("started"))
        self.assertEqual(1, self._history(job_id, "runtime_job.started"))
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owners[0].tick(job_id).status)

    def test_o6_n6_schedule_of_a_running_row_is_not_ownership(self) -> None:
        owner = self._supervisor()
        other = self._supervisor(self._second_service())
        target, gate, entered, calls = self._blocking_target()
        job = self._schedule(owner, "o6", target)
        self.assertTrue(entered.wait(5))
        seen = self._schedule(other, "o6", lambda: calls.append("second"))
        self.assertEqual((job.job_id, RuntimeJobStatus.RUNNING), (seen.job_id, seen.status))
        for state in (other._owned_job_ids, other._workers, other._leases, other._result_events):
            self.assertNotIn(job.job_id, state)
        other.close(timeout_seconds=0.1)
        self.assertEqual("running", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job.job_id)[0][0])
        gate.set()
        owner._workers[job.job_id].join(10)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owner.tick(job.job_id).status)
        self.assertEqual([1], calls)
        self.assertNotIn("collector_lost", self._kinds(job.job_id))

    def test_o4_claim_exception_keeps_the_lease_and_ownership_until_close(self) -> None:
        """B1i의 lease 부분: claim 예외 뒤에도 lease·소유를 유지하고 close가 소실을 기록한 뒤 놓는다."""

        supervisor = self._supervisor()
        path = self._lock_path("o4")
        with mock.patch.object(self.service, "_claim_runtime_job_start_epoch",
                               side_effect=RuntimeError("claim 실패 주입")):
            with self.assertRaisesRegex(RuntimeError, "claim 실패 주입"):
                self._schedule(supervisor, "o4", lambda: {"ok": True})
        job_id = self._rows("SELECT id FROM runtime_jobs WHERE checkpoint_key='o4'")[0][0]
        self.assertIn(job_id, supervisor._owned_job_ids)
        self.assertIn(job_id, supervisor._leases)
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(path))
        supervisor.close(timeout_seconds=0.1)
        self.assertEqual("collector_lost", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0])
        self.assertNotIn(job_id, supervisor._leases)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(path))

    def test_o7_public_start_does_not_claim_an_unbound_collector_lost_job(self) -> None:
        job = self._service_job("o7", lock_file=True)
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "o7"},
        )
        before = _ledger_copy(self.service)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.start_runtime_job(job.job_id).status)
        self.assertEqual(before, _ledger_copy(self.service))

    def test_o8_same_process_loser_returns_running_within_the_wait_bound(self) -> None:
        supervisors = [self._supervisor(), self._supervisor(self._second_service())]
        target, gate, _entered, calls = self._blocking_target()
        passed = self._hold_both_at_scheduled((self.service, supervisors[1].service), "o8")
        results: dict[int, tuple] = {}

        def schedule(index: int) -> None:
            job = self._schedule(supervisors[index], "o8", target)
            results[index] = (job, time.monotonic())

        threads = [threading.Thread(target=schedule, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual({RuntimeJobStatus.RUNNING}, {job.status for job, _ in results.values()})
        job_id = results[0][0].job_id
        winner = next(index for index, item in enumerate(supervisors) if job_id in item._workers)
        loser_elapsed = results[1 - winner][1] - min(passed)
        self.assertLess(loser_elapsed, SHORT_TICK_LIMIT_SECONDS)
        gate.set()
        supervisors[winner]._workers[job_id].join(10)
        self.assertEqual([1], calls)
        self.assertEqual(1, self._kinds(job_id).count("started"))
        self.assertEqual(1, self._history(job_id, "runtime_job.started"))

    # --- 10.3 ---------------------------------------------------------------------------

    def test_n1_c1_non_owner_observe_and_run_once_do_not_touch_a_live_owner_job(self) -> None:
        owner = self._supervisor()
        target, gate, entered, calls = self._blocking_target()
        job = self._schedule(owner, "n1", target)
        self.assertTrue(entered.wait(5))
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory))
        application = EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL)
        before = _ledger_copy(self.service)
        for _ in range(2):
            self.assertEqual("running", application.observe(self.project_id)["runtime_job"]["status"])
        observed = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, observed.action, observed)
        self.assertEqual(before, _ledger_copy(self.service))
        gate.set()
        owner._workers[job.job_id].join(10)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owner.tick(job.job_id).status)
        self.assertEqual({"probe": "done"}, self.service.consume_runtime_job(job.job_id))
        self.assertEqual([1], calls)
        self.assertNotIn("collector_lost", self._kinds(job.job_id))

    def _dispatch_held_worker(self, point: str):
        gate, entered = threading.Event(), threading.Event()
        self._gates.append(gate)
        held = {"done": False}

        def hold() -> None:
            if not held["done"]:
                held["done"] = True
                entered.set()
                gate.wait(10)

        class HeldRuntime(FakeCodexRuntime):
            def list_models(inner):
                if point == "inventory" and active_runtime_job_id() is not None:
                    hold()
                return super().list_models()

        runtime = HeldRuntime(self.inventory)
        supervisor = self._supervisor(runtime=runtime)
        dispatcher = self._dispatcher(supervisor, fault_hook=lambda name: hold() if name == point else None)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        dispatched = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        self.assertTrue(entered.wait(5))
        return dispatcher, runtime, supervisor, gate, dispatched

    def _complete_dispatched_worker(self, dispatcher, runtime, dispatched) -> None:
        job_id = dispatched.runtime_job_id
        self.assertTrue(_wait_until(lambda: self.service.load_runtime_job(job_id).turn_id is not None, 10))
        (self.prepared.workspace / "app.py").write_text(_FIXED_APP, encoding="utf-8")
        runtime.complete(self.service.load_runtime_job(job_id).thread_id, response="작업 완료")
        for _ in range(200):
            outcome = dispatcher.run_once(self.project_id)
            if outcome.action is RunOnceAction.VALIDATED:
                break
            self.assertNotEqual(RunOnceAction.BLOCKED, outcome.action, outcome)
            time.sleep(0.01)
        self.assertEqual(RunOnceAction.VALIDATED, outcome.action, outcome)
        self.assertEqual("succeeded", self._rows(
            "SELECT status FROM attempts WHERE id=?", dispatched.attempt_id)[0][0])
        self.assertEqual((1, 1), (runtime.create_calls, runtime.turn_calls))
        self.assertEqual(1, self._rows(
            "SELECT COUNT(*) FROM provider_calls WHERE attempt_id=?", dispatched.attempt_id)[0][0])

    def _assert_non_owner_worker_calls_are_inert(self, point: str) -> None:
        dispatcher, runtime, _supervisor, gate, dispatched = self._dispatch_held_worker(point)
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory))
        other_dispatcher = self._dispatcher(other, other_service)
        application = EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL)
        before = _ledger_copy(self.service)
        for _ in range(2):
            outcome = other_dispatcher.run_once(self.project_id)
            self.assertEqual(RunOnceAction.OBSERVED, outcome.action, outcome)
        self.assertEqual("running", application.observe(self.project_id)["runtime_job"]["status"])
        self.assertEqual(before, _ledger_copy(self.service))
        gate.set()
        self._complete_dispatched_worker(dispatcher, runtime, dispatched)
        job_statuses = {status for (status,) in self._rows("SELECT status FROM runtime_jobs")}
        self.assertNotIn("cancelled", job_statuses)
        self.assertEqual([], self._rows("SELECT id FROM runtime_intents WHERE status='unknown'"))
        self.assertNotEqual("recovery_required", self._rows(
            "SELECT run_state FROM projects WHERE id=?", self.project_id)[0][0])
        self.assertNotIn("collector_lost", self._kinds(dispatched.runtime_job_id))

    def test_n4_non_owner_calls_while_the_worker_waits_at_inventory(self) -> None:
        self._assert_non_owner_worker_calls_are_inert("inventory")

    def test_n4_non_owner_calls_while_the_worker_waits_after_thread_intent(self) -> None:
        self._assert_non_owner_worker_calls_are_inert("after_thread_intent")

    def test_n4_non_owner_calls_while_the_worker_waits_after_turn_intent(self) -> None:
        self._assert_non_owner_worker_calls_are_inert("after_turn_intent")

    def test_n7_one_process_host_reentry_is_observed_not_lost(self) -> None:
        gate, entered = threading.Event(), threading.Event()
        self._gates.append(gate)
        application = EngineApplication(self.service, runtime=self.runtime, governance=ALLOW_ALL)
        self._supervisors.append(application.supervisor)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        dispatcher = application._dispatcher()

        def hold(point: str) -> None:
            if point == "after_thread_intent" and not entered.is_set():
                entered.set()
                gate.wait(10)

        dispatcher.fault_hook = hold
        dispatched = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        self.assertTrue(entered.wait(5))
        before = _ledger_copy(self.service)
        for _ in range(3):
            observed = application.run_once(self.project_id)
            self.assertEqual(RunOnceAction.OBSERVED, observed.action, observed)
            self.assertEqual("running", observed.runtime_job_status)
        self.assertEqual(before, _ledger_copy(self.service))
        gate.set()
        self._complete_dispatched_worker(application, self.runtime, dispatched)
        self.assertNotIn("collector_lost", self._kinds(dispatched.runtime_job_id))

    def test_n7_fault_after_the_thread_effect_keeps_the_harness_expectation(self) -> None:
        """e2e_qualification.py:1832-1851 fault 경로: create intent가 unknown 또는 received로 수렴한다."""

        application = EngineApplication(self.service, runtime=self.runtime, governance=ALLOW_ALL)
        self._supervisors.append(application.supervisor)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        triggered = threading.Event()

        def fault(point: str) -> None:
            if point == "after_thread_effect":
                triggered.set()
                raise RuntimeError("fault after provider effect before receipt")

        dispatcher = application._dispatcher()
        dispatcher.fault_hook = fault
        dispatcher.run_once(self.project_id)
        deadline = time.monotonic() + 2
        statuses: set[str] = set()
        application.run_once(self.project_id)
        while time.monotonic() < deadline:
            statuses = {status for (status,) in self._rows(
                "SELECT i.status FROM runtime_intents i JOIN attempts a ON a.id=i.attempt_id "
                "WHERE a.project_id=? AND i.kind='create_thread'", self.project_id)}
            if triggered.is_set() and statuses & {"unknown", "received"}:
                break
            time.sleep(0.01)
            application.run_once(self.project_id)
        self.assertTrue(statuses & {"unknown", "received"}, statuses)
        self.assertEqual(1, self.runtime.create_calls)

    # --- 10.4b owner proof ------------------------------------------------------------------

    def _assert_run_once_is_owner_proof_blocked(self, dispatcher) -> None:
        before = _ledger_copy(self.service)
        for _ in range(3):
            blocked = dispatcher.run_once(self.project_id)
            self.assertEqual((RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE", OWNER_PROOF_MISSING_DETAIL),
                             (blocked.action, blocked.blocker_code, blocked.detail), blocked)
        self.assertEqual(before, _ledger_copy(self.service))

    def test_p1_row_without_a_lock_file_is_not_a_crash_and_prober_creates_no_file(self) -> None:
        job = self._service_job("p1", lock_file=False)
        other = self._supervisor()
        application = EngineApplication(self.service, runtime=self.runtime, supervisor=other, governance=ALLOW_ALL)
        before = _ledger_copy(self.service)
        self.assertIs(RuntimeJobStatus.RUNNING, other.tick(job.job_id).status)
        self.assertEqual("running", application.observe(self.project_id)["runtime_job"]["status"])
        self.assertEqual(before, _ledger_copy(self.service))
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(other))
        self.assertFalse(self._lock_path("p1").exists())
        self.assertIs(OwnerLockState.ABSENT, probe_owner_lock(self._lock_path("p1")))

    def test_p2_deadline_hard_stop_is_the_only_tick_effect_then_run_once_stays_blocked(self) -> None:
        job = self._service_job("p2", lock_file=False, deadline_seconds=-0.001)
        supervisor = self._supervisor(terminal_observation_grace_seconds=0)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, supervisor.tick(job.job_id).status)
        self.assertIn("terminal observation grace elapsed", self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'",
            job.job_id)[0][0])
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(supervisor))
        self.assertEqual("collector_lost", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job.job_id)[0][0])
        self.assertNotIn("collector_reattached", self._kinds(job.job_id))

    def test_p3_scheduled_row_without_a_lock_file_is_not_restarted(self) -> None:
        job = self._service_job("p3", lock_file=False, start=False)
        supervisor = self._supervisor()
        before = _ledger_copy(self.service)
        self.assertIs(RuntimeJobStatus.SCHEDULED, supervisor.tick(job.job_id).status)
        self.assertEqual(before, _ledger_copy(self.service))
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(supervisor))
        self.assertEqual("scheduled", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job.job_id)[0][0])

    def test_p4_schedule_does_not_create_a_lock_file_for_an_existing_row(self) -> None:
        job = self._service_job("p4", lock_file=False, start=False)
        supervisor = self._supervisor()
        target_calls: list[int] = []
        seen = self._schedule(supervisor, "p4", lambda: target_calls.append(1))
        self.assertEqual((job.job_id, RuntimeJobStatus.SCHEDULED), (seen.job_id, seen.status))
        self.assertFalse(self._lock_path("p4").exists())
        for state in (supervisor._owned_job_ids, supervisor._workers, supervisor._leases):
            self.assertNotIn(job.job_id, state)
        self.assertEqual([], target_calls)
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(supervisor))

    def test_p5_the_restart_test_needs_the_owner_proof_file(self) -> None:
        """test_crash_after_winning_the_claim_is_observed_as_a_restart의 보정 전·후 setup."""

        job = self._service_job("g1b-b1:crash", lock_file=False)
        restarted = self._supervisor()
        self.assertIs(RuntimeJobStatus.RUNNING, restarted.tick(job.job_id).status)
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(restarted))
        path = self._lock_path("g1b-b1:crash")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, restarted.tick(job.job_id).status)
        payload = json.loads(self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'",
            job.job_id)[0][0])
        self.assertEqual({"reason": "supervisor restarted before provider binding was durable"}, payload)
        self.assertEqual(1, self._kinds(job.job_id).count("started"))

    def test_p6_success_checkpoint_on_a_row_without_a_lock_file_is_reattached_only_by_observe(self) -> None:
        """T r4 공백 G1(AC22·J4): lock 파일 없는 행(0p)에 저장된 성공 결과가 있어도 run_once는 tick 없이 멈춘다.

        성공 checkpoint 재부착은 lease 없는 tick(observe)의 기존 예외로만 일어난다. P1~P5에는 성공
        checkpoint가 없어 run_once가 tick을 부르는지 가리지 못한다.
        """

        job = self._service_job("p6", lock_file=False)
        # 구버전 owner의 worker가 남긴 것과 같은 성공 checkpoint다(supervisor worker와 같은 service 기록 경로·payload).
        result = {"probe": "p6"}
        self.service.record_runtime_job_observation(
            job.job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
            payload={
                "target_result_checkpoint_version": "1.0", "target_result": result,
                "target_result_digest": sha256_digest(result), "job_request_digest": job.request_digest,
                "attempt_id": job.attempt_id, "thread_id": job.thread_id, "turn_id": job.turn_id,
            },
        )
        supervisor = self._supervisor()
        application = EngineApplication(self.service, runtime=self.runtime, supervisor=supervisor, governance=ALLOW_ALL)
        # dispatcher와 scheduler facade 모두 run_once×3이 원장 무변경 BLOCKED다(재부착 없음).
        self._assert_run_once_is_owner_proof_blocked(self._dispatcher(supervisor))
        self._assert_run_once_is_owner_proof_blocked(application)
        self.assertIs(RuntimeJobStatus.RUNNING, self.service.load_runtime_job(job.job_id).status)
        status = _read_only_status(self, application, self.project_id)
        self.assertEqual(("observe_first_required", "observe_first", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         _status_row(status))
        self.assertTrue(status["recovery"]["next_action"]["detail"].startswith(OWNER_PROOF_MISSING_DETAIL))
        self.assertFalse(self._lock_path("p6").exists())
        # observe(tick)만 lease·lock 파일 없이 저장된 결과를 그대로 재부착한다.
        self.assertEqual("provider_terminal", application.observe(self.project_id)["runtime_job"]["status"])
        self.assertEqual(result, self.service.consume_runtime_job(job.job_id))
        self.assertFalse(self._lock_path("p6").exists())

    # --- 10.5 ------------------------------------------------------------------------------

    def _owner_arguments(self) -> SimpleNamespace:
        return SimpleNamespace(project_id=self.project_id, proposal_file=None, goal_validation_file=None,
                               goal_validation_retry_file=None, resume=False)

    def _run_owned_in_thread(self, application, result_path: Path) -> threading.Thread:
        with (
            mock.patch.object(cli, "_runtime", lambda _arguments: FakeCodexRuntime(self.inventory)),
            mock.patch.object(cli, "_application", lambda _arguments, *, runtime=None: application),
        ):
            # daemon: 가드가 깨져 loop가 끝나지 않아도 테스트 process 종료를 막지 않는다.
            thread = threading.Thread(
                target=cli._run_once_owned, args=(self._owner_arguments(), result_path), daemon=True,
            )
            thread.start()
            # patch는 _run_once_owned가 application을 만든 뒤에만 풀어도 된다.
            self.assertTrue(_wait_until(result_path.exists, 10))
        self.addCleanup(thread.join, 10)
        return thread

    def test_c1_non_owner_cli_run_once_returns_right_after_publishing(self) -> None:
        owner = self._supervisor()
        target, gate, entered, _calls = self._blocking_target()
        job = self._schedule(owner, "c1", target)
        self.assertTrue(entered.wait(5))
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory))
        application = EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL)
        result = self.base / "c1-owner.json"
        thread = self._run_owned_in_thread(application, result)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        published = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(("observed", job.job_id), (published["action"], published["runtime_job_id"]))
        self.assertEqual("running", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job.job_id)[0][0])
        gate.set()

    class _OwnerApplication:
        """loop 조건만 보는 CLI owner application. job 예약은 실제 supervisor가 한다."""

        def __init__(self, service, supervisor, schedule) -> None:
            self.service, self.supervisor, self._schedule = service, supervisor, schedule

        def run_once(self, project_id, **_kwargs):
            job = self._schedule()
            return RunOnceResult(
                action=RunOnceAction.DISPATCHED, project_id=project_id, detail="owner loop 조건 확인",
                runtime_job_id=job.job_id, runtime_job_kind=job.kind, runtime_job_status=job.status,
            )

        def close_task_gate(self) -> None:
            return None

    def test_c2_owner_loop_keeps_following_its_live_worker_until_the_result(self) -> None:
        supervisor = self._supervisor()
        target, gate, entered, _calls = self._blocking_target()
        application = self._OwnerApplication(self.service, supervisor, lambda: self._schedule(supervisor, "c2", target))
        thread = self._run_owned_in_thread(application, self.base / "c2-owner.json")
        self.assertTrue(entered.wait(5))
        job_id = self._rows("SELECT id FROM runtime_jobs WHERE checkpoint_key='c2'")[0][0]
        # 명시적 소실 표시가 있어도 자기 worker가 살아 있고 deadline+grace 전이면 loop는 끝나지 않는다.
        self.service.record_runtime_job_observation(
            job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "c2 explicit mark"},
        )
        thread.join(0.3)
        self.assertTrue(thread.is_alive())
        gate.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual("provider_terminal", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0])

    def test_c2_owner_loop_ends_after_deadline_plus_grace_even_with_a_live_worker(self) -> None:
        supervisor = self._supervisor(terminal_observation_grace_seconds=0)
        target, gate, entered, _calls = self._blocking_target()
        application = self._OwnerApplication(
            self.service, supervisor, lambda: self._schedule(supervisor, "c2-grace", target, timeout=0.05),
        )
        thread = self._run_owned_in_thread(application, self.base / "c2-grace-owner.json")
        self.assertTrue(entered.wait(5))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        job_id = self._rows("SELECT id FROM runtime_jobs WHERE checkpoint_key='c2-grace'")[0][0]
        self.assertTrue(supervisor.runtime_job_worker_alive(job_id))
        self.assertEqual("collector_lost", self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0])
        gate.set()

    def test_r1_01_bound_job_keeps_the_existing_cli_loop_and_close(self) -> None:
        """R1-01: binding된 job에는 owner loop 규칙을 적용하지 않는다(기존 동작 보존).

        owner(P1)의 dispatch worker가 binding 뒤 끝나 lease를 놓은 상태에서 비소유 CLI run-once(P2)를 한 번 부른다.
        P2는 결과 게시 뒤 기존처럼 같은 turn을 따라가며 조기 close·interrupt를 하지 않고, P1도 binding 뒤 loop를 떠나지 않는다.
        """

        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        owner = self._supervisor()
        other_service = self._second_service()
        other = self._supervisor(other_service)
        events = {name: threading.Event() for name in ("owner_bound_tick", "owner_closed", "other_in_loop", "other_closed")}
        other_ticks: list[str] = []

        def instrument(supervisor, tick_hook, closed) -> None:
            real_tick, real_close = supervisor.tick, supervisor.close

            def tick(job_id, **kwargs):
                tick_hook(job_id)
                return real_tick(job_id, **kwargs)

            def close(**kwargs):
                events[closed].set()
                return real_close(**kwargs)

            supervisor.tick, supervisor.close = tick, close

        def owner_tick(job_id: str) -> None:
            if self.service.load_runtime_job(job_id).thread_id is not None:
                events["owner_bound_tick"].set()

        def other_tick(job_id: str) -> None:
            other_ticks.append(job_id)
            if len(other_ticks) >= 2:  # 첫 tick은 run_once 안, 그다음은 결과 게시 뒤 loop다.
                events["other_in_loop"].set()

        instrument(owner, owner_tick, "owner_closed")
        instrument(other, other_tick, "other_closed")
        owner_thread = self._run_owned_in_thread(
            EngineApplication(self.service, runtime=self.runtime, supervisor=owner, governance=ALLOW_ALL),
            self.base / "r1-01-owner.json",
        )
        job_id = json.loads((self.base / "r1-01-owner.json").read_text(encoding="utf-8"))["runtime_job_id"]
        self.assertTrue(_wait_until(lambda: self.service.load_runtime_job(job_id).turn_id is not None, 10))
        owner._workers[job_id].join(10)
        self.assertFalse(owner.runtime_job_worker_alive(job_id))
        self.assertNotIn(job_id, owner._leases)
        self.assertTrue(events["owner_bound_tick"].wait(10))
        self.assertTrue(owner_thread.is_alive())  # P1은 binding 뒤에도 loop에 남는다.

        def counts() -> tuple:
            return (
                self._rows("SELECT status FROM runtime_jobs WHERE id=?", job_id)[0][0],
                self._kinds(job_id).count("interrupt_requested"),
                self._kinds(job_id).count("collector_lost"),
            )

        before = counts()
        self.assertEqual(("running", 0, 0), before)
        other_thread = self._run_owned_in_thread(
            EngineApplication(other_service, runtime=self.runtime, supervisor=other, governance=ALLOW_ALL),
            self.base / "r1-01-other.json",
        )
        published = json.loads((self.base / "r1-01-other.json").read_text(encoding="utf-8"))
        self.assertEqual(("observed", job_id), (published["action"], published["runtime_job_id"]))
        # 결과 게시 뒤 P2가 loop에 들어갔는지(또는 곧바로 끝났는지)를 시간 대기 대신 사건으로 가른다.
        self.assertTrue(_wait_until(lambda: events["other_in_loop"].is_set() or not other_thread.is_alive(), 10))
        self.assertTrue(events["other_in_loop"].is_set())
        self.assertFalse(events["other_closed"].is_set())
        self.assertFalse(events["owner_closed"].is_set())
        self.assertEqual(before, counts())
        self.assertTrue(owner_thread.is_alive())
        self.runtime.complete(self.service.load_runtime_job(job_id).thread_id, response="작업 완료")
        owner_thread.join(10)
        other_thread.join(10)
        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(other_thread.is_alive())
        self.assertEqual(("provider_terminal", 0, 0), counts())

    def test_lp1_unbound_collector_lost_after_deadline_is_not_re_interrupted(self) -> None:
        for checkpoint, lock_file in (("lp1-no-lock", False), ("lp1-lock", True)):
            with self.subTest(lock_file=lock_file):
                job = self._service_job(checkpoint, lock_file=lock_file, deadline_seconds=-0.001)
                self.service.record_runtime_job_observation(
                    job.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": checkpoint},
                )
                supervisor = self._supervisor(terminal_observation_grace_seconds=0)
                before = _ledger_copy(self.service)
                for _ in range(10):
                    self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, supervisor.tick(job.job_id).status)
                self.assertEqual(before, _ledger_copy(self.service))
                self.service.cancel_runtime_job(job.job_id, reason="다음 subTest 격리")

    def test_row2_live_owner_after_deadline_gets_one_interrupt_request_and_no_loss(self) -> None:
        owner = self._supervisor()
        target, gate, entered, _calls = self._blocking_target()
        job = self._schedule(owner, "row2", target, timeout=0.05)
        self.assertTrue(entered.wait(5))
        self.assertTrue(_wait_until(lambda: utc_now() >= job.absolute_deadline_at, 5))
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory), terminal_observation_grace_seconds=0)
        dispatcher = self._dispatcher(other, other_service)
        first = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, first.action, first)
        self.assertIn("owner가 deadline 뒤에도 lock을 쥐고 있음(응답 없음)", first.detail)
        self.assertEqual("interrupting", first.runtime_job_status)
        before = _ledger_copy(self.service)
        for _ in range(3):
            again = dispatcher.run_once(self.project_id)
            self.assertEqual((RunOnceAction.OBSERVED, first.detail), (again.action, again.detail))
        self.assertEqual(before, _ledger_copy(self.service))
        self.assertEqual(1, self._history(job.job_id, "runtime_job.interrupt_requested"))
        self.assertNotIn("collector_lost", self._kinds(job.job_id))
        gate.set()

    # --- 2.5 epoch 결속 실패 checkpoint와 I8 ---------------------------------------------------

    def _failing_job(self, checkpoint: str):
        owner = self._supervisor()

        def target():
            raise RuntimeError("target 실패")

        job = self._schedule(owner, checkpoint, target)
        owner._workers[job.job_id].join(5)
        return owner, job

    def _observations_and_history(self) -> tuple[list, list]:
        copy = _ledger_copy(self.service)
        return copy["runtime_job_observations"], copy["history_events"]

    def test_failure_checkpoint_gives_a_non_owner_the_owner_payload(self) -> None:
        owner, job = self._failing_job("fcp")
        checkpoints = [
            payload for payload in (json.loads(raw) for (raw,) in self._rows(
                "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='provider_progress'",
                job.job_id))
            if payload.get("target_failure_checkpoint_version") == "1.0"
        ]
        started = self._rows(
            "SELECT id FROM runtime_job_observations WHERE job_id=? AND kind='started'", job.job_id)[0][0]
        self.assertEqual(1, len(checkpoints))
        self.assertEqual(started, checkpoints[0]["epoch_observation_id"])
        self.assertEqual({"error_type": "RuntimeError", "error": "target 실패"}, checkpoints[0]["error"])
        # 실패 checkpoint가 durable해졌으므로 worker가 lease를 놓았다.
        self.assertNotIn(str(self._lock_path("fcp")), runtime_module._OWNER_LEASES)
        other = self._supervisor(self._second_service())
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, other.tick(job.job_id).status)
        self.assertEqual([checkpoints[0]["error"]], [json.loads(raw) for (raw,) in self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'",
            job.job_id)])
        # owner의 in-memory 결과는 같은 payload라 새 관측·History를 만들지 않는다.
        before = self._observations_and_history()
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, owner.tick(job.job_id).status)
        self.assertEqual(before, self._observations_and_history())

    def test_i8_result_of_a_previous_epoch_is_discarded(self) -> None:
        owner, job = self._failing_job("i8")
        other = self._supervisor(self._second_service())
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, other.tick(job.job_id).status)
        # 재시작 claim 흉내: 새 epoch(thread 없는 COLLECTOR_REATTACHED)를 만들고 새 owner가 lease를 쥔다.
        _claimed_job, claimed, epoch = self.service._claim_runtime_job_start_epoch(job.job_id)
        self.assertTrue(claimed)
        state, lease = runtime_module._acquire_owner_lease(self._lock_path("i8"), object(), create=False)
        self.assertIs(OwnerLockState.FREE, state)
        self.addCleanup(runtime_module._release_owner_lease, lease)
        self.assertEqual(epoch, owner._current_epoch(job.job_id)[1])
        before = self._observations_and_history()
        self.assertIs(RuntimeJobStatus.RUNNING, owner.tick(job.job_id).status)
        self.assertEqual(before, self._observations_and_history())
        self.assertNotIn(job.job_id, owner._results)


# --- 실제 subprocess CLI owner의 binding 전 REPLANNING job ------------------------------


class ReplanningOwnerProcessTests(_ChildProcessMixin, g1b._ReplanHarness, unittest.TestCase):
    """fm08 자동 SUBGRAPH_REPLAN을 child process의 CLI owner가 inventory 단계에서 붙잡는다."""

    def _start_replanning_owner(self) -> tuple[subprocess.Popen, Path]:
        self._fail_contract()
        # 부모 인스턴스는 재계획 provider가 없어 새 job을 예약하지 않는다. 예약은 child owner만 한다.
        self.application.recovery_provider_available = lambda: False
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", blocked.blocker_code, blocked)
        self.expander_calls_before = self._expander_calls()
        work = Path(self.temp.name) / "owner-work"
        work.mkdir()
        process = self._spawn(
            work, "replan-owner", self.service.ledger.path, self.service.ledger.artifact_root,
            self.project_id, work,
        )
        ready = _wait_until(
            lambda: (work / "owner-result.json").exists() and "inventory" in _journal_entries(work)
            or process.poll() is not None,
            90,
        )
        self.assertTrue(ready and process.poll() is None, self._child_log(work, "replan-owner"))
        published = json.loads((work / "owner-result.json").read_text(encoding="utf-8"))
        self.assertEqual(("dispatched", "replanning"),
                         (published["action"], published["runtime_job_kind"]), published)
        self.job_id = published["runtime_job_id"]
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(
            runtime_owner_lock_path(self.service, self.project_id, self._job_checkpoint())))
        return process, work

    def _expander_calls(self) -> int:
        return [role for (role,) in self._rows(
            "SELECT role FROM provider_calls WHERE project_id=?", self.project_id)].count("plan_expander")

    def _job_checkpoint(self) -> str:
        return self._rows("SELECT checkpoint_key FROM runtime_jobs WHERE id=?", self.job_id)[0][0]

    def _release_and_converge(self, process: subprocess.Popen, work: Path) -> None:
        self.assertEqual("running", self._rows("SELECT status FROM runtime_jobs WHERE id=?", self.job_id)[0][0])
        self.assertIsNone(process.poll())  # owner loop가 끝나지 않았다.
        (work / "release").touch()
        self.assertEqual(0, process.wait(90), self._child_log(work, "replan-owner"))
        self._until_recovered_activation()
        self.assertEqual(2, self._plan(self._active_plan_id()).revision_no)
        self.assertEqual("consumed", self._rows("SELECT status FROM runtime_jobs WHERE id=?", self.job_id)[0][0])
        entries = _journal_entries(work)
        self.assertEqual(1, entries.count("inventory"))
        self.assertEqual(1, entries.count("role:plan_expander"))
        # Goal 준비의 plan_expander 호출 뒤로 원장 provider_call도 journal과 같은 1회만 늘었다.
        self.assertEqual(self.expander_calls_before + 1, self._expander_calls())
        kinds = [kind for (kind,) in self._rows(
            "SELECT kind FROM runtime_job_observations WHERE job_id=? ORDER BY rowid", self.job_id)]
        self.assertEqual(1, kinds.count("started"))
        self.assertNotIn("collector_lost", kinds)
        self.assertNotIn("collector_reattached", kinds)

    def test_n2_sequential_second_cli_run_once_is_a_non_owner_that_exits(self) -> None:
        process, work = self._start_replanning_owner()
        before = _ledger_copy(self.service)
        second = work / "second.json"
        codes: list[int] = []

        def run() -> None:
            with (
                mock.patch.dict(os.environ, {"FLOWMARSHAL_RUNTIME_OWNER_RESULT": str(second)}),
                mock.patch.object(cli, "_runtime", lambda _arguments: FakeCodexRuntime(inventory())),
                mock.patch.object(cli, "_application", lambda arguments, *, runtime=None: EngineApplication(
                    cli._service(arguments), runtime=runtime, governance=ALLOW_ALL)),
                redirect_stdout(io.StringIO()),
            ):
                codes.append(cli.main([
                    "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                    "run-once", "--project-id", self.project_id,
                ]))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([0], codes)
        published = json.loads(second.read_text(encoding="utf-8"))
        self.assertEqual(("observed", self.job_id), (published["action"], published["runtime_job_id"]))
        self.assertEqual(before, _ledger_copy(self.service))
        self._release_and_converge(process, work)

    def test_n3_n5_non_owner_observe_does_not_lose_the_live_replanning_job(self) -> None:
        process, work = self._start_replanning_owner()
        before = _ledger_copy(self.service)
        for _ in range(2):
            self.assertEqual("running", self.application.observe(self.project_id)["runtime_job"]["status"])
        self.assertEqual(before, _ledger_copy(self.service))
        self._release_and_converge(process, work)

    def test_sr3_scheduler_run_once_only_observes_while_the_owner_lives(self) -> None:
        process, work = self._start_replanning_owner()
        before = _ledger_copy(self.service)
        for _ in range(3):
            observed = self.application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.OBSERVED, self.job_id), (observed.action, observed.runtime_job_id))
            self.assertIn("replanning/running", observed.detail)
        self.assertEqual(before, _ledger_copy(self.service))
        self._release_and_converge(process, work)

    def test_n5s_status_never_shows_automatic_pending_while_the_cli_owner_lives(self) -> None:
        """10.3 N5의 status 몫(I4). I3a의 N5는 그대로 두고 같은 시나리오를 따로 본다."""

        process, work = self._start_replanning_owner()
        before = _ledger_copy(self.service)
        rows = []
        for _ in range(2):
            rows.append(_status_row(_read_only_status(self, self.application, self.project_id)))
            observed = self.application.observe(self.project_id)
            self.assertEqual("running", observed["runtime_job"]["status"])
            rows.append(_status_row(observed["status"]))  # observe가 돌려준 status도 같은 판정이다
        rows.append(_status_row(_read_only_status(self, self.application, self.project_id)))
        # 살아 있는 CLI owner(판정 2) 동안에는 사실과 다른 automatic_pending이 한 번도 없다.
        self.assertEqual([("none", "none", None)] * len(rows), rows)
        self.assertEqual(before, _ledger_copy(self.service))
        self._release_and_converge(process, work)
        self.assertEqual("recovered", self.application.status(self.project_id)["recovery"]["state"])

    def test_killed_cli_owner_is_lost_at_the_next_observe_without_waiting_for_the_deadline(self) -> None:
        """B-1 조건 6의 판정 부분: 실제 owner process가 강제 종료되면 lock이 풀려 다음 observe가 곧바로 판정한다."""

        process, work = self._start_replanning_owner()
        lock = runtime_owner_lock_path(self.service, self.project_id, self._job_checkpoint())
        deadline = self.service.load_runtime_job(self.job_id).absolute_deadline_at
        process.kill()
        process.wait(10)
        # 해제 대기는 원장을 읽지 않고 OS lock만 10ms 간격으로 최대 5초 확인한다.
        self.assertTrue(_wait_until(lambda: probe_owner_lock(lock) is OwnerLockState.FREE, 5))
        self.assertLess(utc_now(), deadline)
        observed = self.application.observe(self.project_id)
        self.assertEqual("collector_lost", observed["runtime_job"]["status"])
        payload = json.loads(self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='collector_lost'",
            self.job_id)[0][0])
        self.assertEqual({"reason": "supervisor restarted before provider binding was durable"}, payload)
        self.assertEqual(["inventory"], _journal_entries(work))
        self.assertEqual(self.expander_calls_before, self._expander_calls())

    def test_l7_posix_fails_closed_before_any_ledger_write_but_other_facades_work(self) -> None:
        with mock.patch.object(runtime_module, "owner_lock_platform_supported", lambda: False):
            self._prepare()
            self._authorize()
            before = _ledger_copy(self.service)
            blocked = self.application.run_once(self.project_id)
            self.assertEqual(
                (RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE", RUNTIME_OWNER_PLATFORM_UNSUPPORTED),
                (blocked.action, blocked.blocker_code, blocked.detail), blocked,
            )
            self.assertIn("platform unsupported: posix", blocked.detail)
            self.assertEqual(before, _ledger_copy(self.service))
            self.assertEqual([], self._rows("SELECT id FROM runtime_jobs"))
            lock = runtime_owner_lock_path(self.service, self.project_id, "l7")
            with self.assertRaisesRegex(RuntimeOwnerLockUnavailable, "platform unsupported: posix"):
                runtime_module._acquire_owner_lease(lock, self, create=True)
            with self.assertRaisesRegex(RuntimeOwnerLockUnavailable, "platform unsupported: posix"):
                probe_owner_lock(lock)
            with self.assertRaisesRegex(RuntimeOwnerLockUnavailable, "platform unsupported: posix"):
                self.application.supervisor.schedule(
                    project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="l7",
                    request={"probe": 7}, timeout_seconds=60, target=lambda: {"ok": True},
                )
            self.assertEqual([], self._rows("SELECT id FROM runtime_jobs"))
            self.assertFalse(lock.parent.exists())
            result = Path(self.temp.name) / "l7-owner.json"
            with (
                mock.patch.dict(os.environ, {"FLOWMARSHAL_RUNTIME_OWNER_RESULT": str(result)}),
                mock.patch.object(cli, "_runtime", lambda _arguments: FakeCodexRuntime(inventory())),
                mock.patch.object(cli, "_application", lambda arguments, *, runtime=None: EngineApplication(
                    cli._service(arguments), runtime=runtime, governance=ALLOW_ALL)),
            ):
                code = cli.main([
                    "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                    "run-once", "--project-id", self.project_id,
                ])
            self.assertEqual(0, code)
            published = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(("blocked", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                             (published["action"], published["blocker_code"]))
            self.assertEqual("paused", self.application.pause(self.project_id)["control_state"])
            self.assertEqual("cancelled", self.application.cancel(self.project_id)["control_state"])


# --- I3b: 종료 라우팅·재시작·효과 fence(설계 4장·5장, 10.1 L8, 10.2 O3~O5, 10.3 N8, 10.4, 10.4c, 10.5) --------


_BEFORE_EFFECT = "before effect"


class _OwnerKillMixin(_ChildProcessMixin):
    """설정 파일로 child owner를 지정 지점에 세우고 실제 ``Popen.kill``로 끝낸다."""

    def _start_owner(self, work: Path, config: dict) -> subprocess.Popen:
        work.mkdir(exist_ok=True)
        (work / "child.json").write_text(json.dumps(config), encoding="utf-8")
        process = self._spawn(
            work, "owner", self.service.ledger.path, self.service.ledger.artifact_root, self.project_id, work,
        )
        marker = f"hold:{config['block']}"
        ready = _wait_until(lambda: marker in _journal_entries(work) or process.poll() is not None, 90)
        self.assertTrue(ready and process.poll() is None, self._child_log(work, "owner"))
        return process

    def _kill_owner(self, process: subprocess.Popen, job) -> None:
        lock = runtime_owner_lock_path(self.service, self.project_id, job.checkpoint_key)
        self.assertIs(OwnerLockState.HELD_OTHER, probe_owner_lock(lock))
        process.kill()
        process.wait(10)
        # 해제 대기는 원장을 읽지 않고 OS lock만 10ms 간격으로 최대 5초 본다.
        self.assertTrue(_wait_until(lambda: probe_owner_lock(lock) is OwnerLockState.FREE, 5))

    def _history_count(self, job_id: str, event_type: str) -> int:
        with self.service.ledger.read() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type=?", (job_id, event_type),
            ).fetchone()[0]

    def _observations(self, job_id: str, kind: str) -> list[dict]:
        with self.service.ledger.read() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind=? ORDER BY rowid",
                (job_id, kind),
            )]

    def _assert_restart_recorded(self, job, kind: str) -> None:
        """재시작 claim 관측 1건과 History 1건, 저장된 request digest·deadline 불변(결정 D2)."""

        restarts = [item for item in self._observations(job.job_id, kind) if "restart" in item]
        self.assertEqual(1, len(restarts), restarts)
        self.assertEqual("owner_lost_no_effect_proven", restarts[0]["restart"]["reason"])
        self.assertEqual(1, restarts[0]["restart"]["restart_no"])
        self.assertNotIn("owner_proof", json.dumps(restarts[0]))
        self.assertEqual(1, self._history_count(job.job_id, f"runtime_job.{kind}"))
        current = self.service.load_runtime_job(job.job_id)
        self.assertEqual((job.request_digest, job.absolute_deadline_at),
                         (current.request_digest, current.absolute_deadline_at))

    def _history_payloads(self, event_type: str) -> list[dict]:
        return [json.loads(raw) for (raw,) in self._rows(
            "SELECT payload_json FROM history_events WHERE project_id=? AND event_type=? ORDER BY sequence",
            self.project_id, event_type,
        )]

    def _converge(self, dispatcher, runtime, attempt_id: str, *, application=None) -> None:
        binding = self._rows("SELECT binding_json FROM attempts WHERE id=?", attempt_id)
        self.assertTrue(_wait_until(lambda: json.loads(self._rows(
            "SELECT binding_json FROM attempts WHERE id=?", attempt_id)[0][0] or "{}").get("turn_id"), 10), binding)
        thread_id = json.loads(self._rows("SELECT binding_json FROM attempts WHERE id=?", attempt_id)[0][0])["thread_id"]
        (self.prepared.workspace / "app.py").write_text(_FIXED_APP, encoding="utf-8")
        runtime.complete(thread_id, response="작업 완료")
        runner = application or dispatcher
        outcome = None
        for _ in range(200):
            outcome = runner.run_once(self.project_id)
            if outcome.action is RunOnceAction.VALIDATED:
                break
            self.assertNotEqual(RunOnceAction.BLOCKED, outcome.action, outcome)
            time.sleep(0.01)
        self.assertEqual(RunOnceAction.VALIDATED, outcome.action, outcome)
        self.assertEqual("succeeded", self._rows("SELECT status FROM attempts WHERE id=?", attempt_id)[0][0])

    def _assert_blocked_without_writes(self, run_once, code: str, *, times: int = 3):
        before = _ledger_copy(self.service)
        outcomes = [run_once() for _ in range(times)]
        for outcome in outcomes:
            self.assertEqual((RunOnceAction.BLOCKED, code), (outcome.action, outcome.blocker_code), outcome)
            self.assertNotIn(_BEFORE_EFFECT, outcome.detail)
        self.assertEqual({outcomes[0].detail}, {outcome.detail for outcome in outcomes})
        self.assertEqual(before, _ledger_copy(self.service))
        return outcomes[0]


class OwnerRoutingProjectTests(_OwnerKillMixin, _PreparedProject):
    """lease를 얻은 binding 없는 job의 라우팅 행(4.3)과 효과 fence F1(5장)를 한 process 안에서 본다.

    "owner 소실"은 service 수준 claim 뒤 worker 없이 남은 행(owner process가 claim 직후 죽은 원장 상태)으로 만든다.
    실제 subprocess kill은 아래 process 테스트가 맡는다.
    """

    def _lost_job(self, checkpoint: str, *, kind: RuntimeJobKind = RuntimeJobKind.RECOVERY,
                  request: dict | None = None, deadline_seconds: float = 60.0, attempt_id: str | None = None):
        path = self._lock_path(checkpoint)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        job = self.service.schedule_runtime_job(
            project_id=self.project_id, kind=kind, checkpoint_key=checkpoint,
            request=request if request is not None else {"assessment": {"probe": checkpoint}},
            absolute_deadline_at=utc_now() + timedelta(seconds=deadline_seconds), attempt_id=attempt_id,
        )
        claimed, won = self.service._claim_runtime_job_start(job.job_id)
        self.assertTrue(won)
        return claimed

    def _record_loss(self, dispatcher, job) -> None:
        """C행: lease를 얻은 run_once가 먼저 COLLECTOR_LOST 하나만 기록하고 반환한다."""

        observed = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, observed.action, observed)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)

    def _approval_deadline(self):
        with self.service.ledger.read() as connection:
            value = connection.execute(
                "SELECT json_extract(payload_json,'$.absolute_deadline_at') FROM goal_authorizations "
                "WHERE project_id=? ORDER BY revision_no DESC LIMIT 1", (self.project_id,),
            ).fetchone()[0]
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    # --- 10.1 L8 --------------------------------------------------------------------------

    def test_l8_probe_race_inside_the_retry_is_a_normal_claim(self) -> None:
        supervisor = self._supervisor()
        real = runtime_module._acquire_owner_lease
        misses = [2]

        def busy_twice(path, holder, *, create):
            if misses[0]:
                misses[0] -= 1
                return OwnerLockState.HELD_OTHER, None
            return real(path, holder, create=create)

        calls: list[int] = []
        with mock.patch.object(runtime_module, "_acquire_owner_lease", busy_twice):
            job = self._schedule(supervisor, "l8-retry", lambda: calls.append(1) or {"ok": True})
        self.assertIs(RuntimeJobStatus.RUNNING, job.status)
        supervisor._workers[job.job_id].join(5)
        self.assertEqual([1], calls)
        self.assertEqual(1, self._kinds(job.job_id).count("started"))
        self.assertEqual([], [item for item in self._observations(job.job_id, "started") if "restart" in item])

    def test_l8_probe_race_past_the_retry_leaves_an_orphan_that_s_restarts_once(self) -> None:
        path = self._lock_path("l8")
        ready = self.base / "l8.ready.json"
        holder = self._spawn(self.base, "hold", path, ready)
        self.assertTrue(_wait_until(ready.exists, 60), self._child_log(self.base, "hold"))
        supervisor = self._supervisor()
        calls: list[int] = []
        job = supervisor.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="l8",
            request={"assessment": {"probe": "l8"}}, timeout_seconds=60, target=lambda: calls.append(1),
        )
        self.assertIs(RuntimeJobStatus.SCHEDULED, job.status)
        self.assertNotIn(job.job_id, supervisor._owned_job_ids)
        holder.kill()
        holder.wait(10)
        self.assertTrue(_wait_until(lambda: probe_owner_lock(path) is OwnerLockState.FREE, 5))
        restarted = self._dispatcher(supervisor).run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "started")
        supervisor._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, supervisor.tick(job.job_id).status)
        self.assertEqual({"assessment": {"probe": "l8"}}, self.service.consume_runtime_job(job.job_id))
        self.assertEqual([], calls)  # 원래 target은 한 번도 불리지 않고 request로 재구성한 target만 1회 돌았다.

    # --- 10.2 O3(재부착)·O4·O5 ----------------------------------------------------------------

    def test_o4_after_close_the_next_run_once_restarts_the_job_once(self) -> None:
        owner = self._supervisor()
        with mock.patch.object(self.service, "_claim_runtime_job_start_epoch",
                               side_effect=RuntimeError("claim 실패 주입")):
            with self.assertRaisesRegex(RuntimeError, "claim 실패 주입"):
                owner.schedule(
                    project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="o4r",
                    request={"assessment": {"probe": "o4r"}}, timeout_seconds=60, target=lambda: {"ok": 1},
                )
        job = self.service.active_runtime_job(self.project_id)
        owner.close(timeout_seconds=0.1)
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        other = self._supervisor(self._second_service())
        restarted = self._dispatcher(other, other.service).run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        self.assertEqual(0, self._kinds(job.job_id).count("started"))
        other._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, other.tick(job.job_id).status)

    def test_o5_b1g_restart_is_a_collector_lost_to_running_cas_that_runs_the_target_once(self) -> None:
        job = self._lost_job("o5")
        supervisor = self._supervisor()
        dispatcher = self._dispatcher(supervisor)
        self._record_loss(dispatcher, job)
        transitions: list[tuple] = []
        real = self.service._claim_runtime_job_start_in_transaction

        def spy(tx, job_id, **kwargs):
            before = tx.one("SELECT status FROM runtime_jobs WHERE id=?", (job_id,))["status"]
            result = real(tx, job_id, **kwargs)
            transitions.append((before, result[0].status.value, result[1], "restart" in kwargs))
            return result

        self.service._claim_runtime_job_start_in_transaction = spy
        self.addCleanup(setattr, self.service, "_claim_runtime_job_start_in_transaction", real)
        restarted = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, restarted.action, restarted)
        self.assertEqual([("collector_lost", "running", True, True)], transitions)
        self._assert_restart_recorded(job, "collector_reattached")
        supervisor._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, supervisor.tick(job.job_id).status)
        self.assertEqual({"assessment": {"probe": "o5"}}, self.service.consume_runtime_job(job.job_id))

    # --- 10.4 K7·K8·K9·K10·K14 -----------------------------------------------------------------

    def test_k7_worker_error_before_any_intent_stops_with_the_same_code(self) -> None:
        class PolicyMismatchRuntime(FakeCodexRuntime):
            def verify_execution_policy(inner, cwd):
                evidence = super().verify_execution_policy(cwd)
                if active_runtime_job_id() is not None:
                    return evidence.model_copy(update={"permission_profile": ":read-only"})
                return evidence

        runtime = PolicyMismatchRuntime(self.inventory)
        supervisor = self._supervisor(runtime=runtime)
        dispatcher = self._dispatcher(supervisor)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        dispatched = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        supervisor._workers[dispatched.runtime_job_id].join(5)
        # owner 자신의 tick이 epoch 결속 실패를 COLLECTOR_LOST로 기록한다(C와 같은 payload).
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(self.project_id).action)
        blocked = self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "PERMISSION_POLICY_MISMATCH",
        )
        self.assertIn("PERMISSION_POLICY_MISMATCH", blocked.detail)
        self.assertEqual(0, self._kinds(dispatched.runtime_job_id).count("collector_reattached"))
        self.assertEqual(0, runtime.create_calls)

    def test_k8_approval_deadline_elapsed_is_goal_absolute_deadline_exceeded_without_a_claim(self) -> None:
        job = self._lost_job("k8")
        late = self._approval_deadline() + timedelta(seconds=1)
        supervisor = self._supervisor(clock=lambda: late)
        dispatcher = self._dispatcher(supervisor)
        self._record_loss(dispatcher, job)
        self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "GOAL_ABSOLUTE_DEADLINE_EXCEEDED",
        )
        self.assertEqual(0, self._kinds(job.job_id).count("collector_reattached"))

    def test_k8b_the_restart_claim_rechecks_the_approval_deadline_inside_its_transaction(self) -> None:
        """T r4 공백 G5(결정 D2): 바깥 판정 뒤·재시작 claim 전에 승인 deadline이 지나면 claim이 막는다.

        예약 때 job deadline은 그때 승인 deadline 이하로 잘리므로(schedule_runtime_job), claim의 승인 검사가
        job deadline 검사와 따로 의미를 갖는 것은 뒤의 재승인이 더 이른 deadline을 둔 경우다. 두 시각을 주입해
        경합을 결정적으로 만든다. 라우터는 supervisor clock(새 승인 deadline 전)으로 6행을 고르고, claim
        transaction은 원장 clock(새 승인 deadline 뒤, job deadline 전)으로 다시 본다.
        """

        from flowmarshal.engine.application import ApplicationAuthority
        from flowmarshal.engine.domain import GoalOperatingPolicy

        job = self._lost_job("k8b", deadline_seconds=120)
        self._record_loss(self._dispatcher(self._supervisor()), job)
        # 제품 ApplicationAuthority 경로로 더 짧은 절대 deadline을 재승인한다.
        policy = GoalOperatingPolicy(absolute_deadline_seconds=60)
        authority = ApplicationAuthority(EngineApplication(self._second_service()))
        authority.authorize(
            self.project_id, target=authority.authorization_target(self.project_id, operating_policy=policy),
            source="g1b-k8b", operating_policy=policy,
        )
        approval = self._approval_deadline()
        late = approval + timedelta(seconds=1)
        self.assertLess(utc_now(), approval)
        self.assertLess(late, job.absolute_deadline_at)

        class LateLedgerClock:
            def now(self_inner) -> str:
                return late.isoformat()

        late_service = EngineService(SQLiteEngineLedger(
            self.service.ledger.path, artifact_root=self.service.ledger.artifact_root, clock=LateLedgerClock(),
        ))
        late_service.initialize()
        supervisor = self._supervisor(late_service)  # supervisor clock은 기본 utc_now(승인 deadline 전)다.
        verdict = classify_unbound_runtime_job(
            self.service, self.service.load_runtime_job(job.job_id), lock_state=OwnerLockState.FREE,
            provider_available=True, now=utc_now(),
        )
        self.assertEqual("6", verdict.row)  # 바깥 판정만 보면 재시작이다.
        before = _ledger_copy(self.service)
        outcome = self._dispatcher(supervisor, late_service).run_once(self.project_id)
        self.assertEqual((RunOnceAction.OBSERVED, job.job_id), (outcome.action, outcome.runtime_job_id), outcome)
        self.assertEqual(before, _ledger_copy(self.service))
        self.assertNotIn(job.job_id, supervisor._workers)
        self.assertEqual(0, self._kinds(job.job_id).count("collector_reattached"))
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path("k8b")))  # claim 실패 뒤 lease를 놓았다
        # 다음 run_once는 바깥 판정에서 승인 deadline 경과를 보고 claim 없이 멈춘다.
        late_dispatcher = self._dispatcher(self._supervisor(clock=lambda: late))
        self._assert_blocked_without_writes(
            lambda: late_dispatcher.run_once(self.project_id), "GOAL_ABSOLUTE_DEADLINE_EXCEEDED",
        )

    def test_k9_goal_kinds_are_owner_lost_with_effect_state_unknown(self) -> None:
        cases = (
            ("k9-test-before", RuntimeJobKind.GOAL_TEST_PREPARE, False),
            ("k9-test-during", RuntimeJobKind.GOAL_TEST_PREPARE, True),
            ("k9-semantic-before", RuntimeJobKind.GOAL_SEMANTIC_VALIDATE, False),
            ("k9-semantic-during", RuntimeJobKind.GOAL_SEMANTIC_VALIDATE, True),
        )
        for checkpoint, kind, during in cases:
            with self.subTest(checkpoint=checkpoint):
                job = self._lost_job(checkpoint, kind=kind, request={"probe": checkpoint})
                if during:  # 역할 호출 중(role_requested 뒤)에 owner를 잃은 원장 상태
                    self.service.record_runtime_job_observation(
                        job.job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                        payload={"role_progress": {"event": "role_requested", "call_id": checkpoint}},
                    )
                calls = self.service.load_runtime_job(job.job_id)
                dispatcher = self._dispatcher(self._supervisor())
                self._record_loss(dispatcher, job)
                blocked = self._assert_blocked_without_writes(
                    lambda: dispatcher.run_once(self.project_id), "RUNTIME_JOB_OWNER_LOST",
                )
                self.assertEqual(f"effect_state=unknown; kind={kind.value}", blocked.detail)
                self.assertEqual(FailureClass.EXTERNAL_UNKNOWN, blocked.failure_class)
                self.assertTrue(blocked.checkpoint_required)
                verdict = classify_unbound_runtime_job(
                    self.service, self.service.load_runtime_job(job.job_id), lock_state=OwnerLockState.FREE,
                    provider_available=True, now=utc_now(),
                )
                self.assertEqual(("7g", "observe_first_required"), (verdict.row, verdict.status_state))
                self.assertNotIn(_BEFORE_EFFECT, verdict.status_detail)
                self.assertEqual("collector_lost", self.service.load_runtime_job(job.job_id).status.value)
                self.assertEqual(0, self._kinds(job.job_id).count("cancelled"))
                self.assertEqual(calls.request_digest, self.service.load_runtime_job(job.job_id).request_digest)
                self.service.cancel_runtime_job(job.job_id, reason="다음 subTest 격리")

    def test_row5_stale_failure_restarts_only_after_a_new_state_observation(self) -> None:
        """5행 예외: worker 자체 오류가 STALE(또는 GAR)이면 그 뒤 새 state 관측(또는 승인)이 있을 때만 재시작 조건으로 간다."""

        owner = self._supervisor()

        def target():
            raise RuntimeError("STALE_EXECUTION_INPUT: 준비 중 관찰이 바뀌었습니다(흉내)")

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="row5-stale",
            request={"assessment": {"probe": "row5"}}, timeout_seconds=60, target=target,
        )
        owner._workers[job.job_id].join(5)
        dispatcher = self._dispatcher(owner)
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(self.project_id).action)  # owner tick
        self._assert_blocked_without_writes(lambda: dispatcher.run_once(self.project_id), "STALE_EXECUTION_INPUT")
        self.service.reobserve_project(self.project_id, force_state_revision=True)
        restarted = dispatcher.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        owner._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owner.tick(job.job_id).status)
        self.assertEqual({"assessment": {"probe": "row5"}}, self.service.consume_runtime_job(job.job_id))

    def test_row5_gar_failure_restarts_only_after_a_new_goal_authorization(self) -> None:
        """T r4 공백 G4(AC16 5행 예외): worker 자체 오류가 GAR이면 그 뒤 새 승인(goal.authorized)이 있을 때만 재시작한다."""

        from flowmarshal.engine.application import ApplicationAuthority

        owner = self._supervisor()

        def target():
            raise RuntimeError("GOAL_AUTHORIZATION_REQUIRED: 준비 중 승인 경계가 바뀌었습니다(흉내)")

        job = owner.schedule(
            project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="row5-gar",
            request={"assessment": {"probe": "row5-gar"}}, timeout_seconds=60, target=target,
        )
        owner._workers[job.job_id].join(5)
        dispatcher = self._dispatcher(owner)
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(self.project_id).action)  # owner tick
        self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "GOAL_AUTHORIZATION_REQUIRED",
        )
        # 새 승인: 같은 원장을 여는 새 host 세션이 제품 ApplicationAuthority 경로로 기록한다(state 관측은 하지 않는다).
        authority = ApplicationAuthority(EngineApplication(self._second_service()))
        authority.authorize(
            self.project_id, target=authority.authorization_target(self.project_id), source="g1b-row5-gar",
        )
        restarted = dispatcher.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        owner._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owner.tick(job.job_id).status)
        self.assertEqual({"assessment": {"probe": "row5-gar"}}, self.service.consume_runtime_job(job.job_id))

    def test_row8_an_unfinished_core_operation_after_the_epoch_is_unknown_not_a_restart(self) -> None:
        """T r4 공백 G2(AC16 8행): 현재 epoch 뒤 완료 관측 없는 CoreOperation(O)만 있어도 재시작하지 않는다."""

        from flowmarshal.engine.operations import CoreOperations

        job = self._lost_job("row8-o")

        # owner가 이 epoch에서 제품 CoreOperations 효과를 시작한 뒤 완료·무효과·실패 관측을 남기지 못한 원장이다.
        def effect():
            raise RuntimeError("row8-o: 완료 관측 전에 끊긴 효과(흉내)")

        with self.assertRaisesRegex(RuntimeError, "row8-o"):
            CoreOperations(self.service).invoke(
                project_id=self.project_id, kind="g1b_row8_probe", request={"probe": "row8-o"}, execute=effect,
            )
        dispatcher = self._dispatcher(self._supervisor())
        self._record_loss(dispatcher, job)
        blocked = self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "EXTERNAL_EFFECT_UNKNOWN",
        )
        # O가 유일한 근거다(role_requested·provider_call·intent·marker 없음).
        self.assertTrue(blocked.detail.endswith("evidence=core_operation; last_role_event=none"), blocked.detail)
        self.assertEqual(FailureClass.EXTERNAL_UNKNOWN, blocked.failure_class)
        self.assertTrue(blocked.checkpoint_required)
        verdict = classify_unbound_runtime_job(
            self.service, self.service.load_runtime_job(job.job_id), lock_state=OwnerLockState.FREE,
            provider_available=True, now=utc_now(),
        )
        self.assertEqual(("8", "observe_first_required"), (verdict.row, verdict.status_state))
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        self.assertEqual(0, self._kinds(job.job_id).count("collector_reattached"))

    def test_row3_cancel_is_only_for_kinds_that_run_their_own_attempt(self) -> None:
        """3.3: REPLANNING·RECOVERY는 실패한 앞 Attempt ID를 가지므로 그 Attempt의 prepared intent로 cancel하지 않는다."""

        from flowmarshal.engine.domain import RuntimeIntentKind

        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        task_id = self._rows(
            "SELECT id FROM task_contracts WHERE project_id=? AND status='materialized'", self.project_id,
        )[0][0]
        attempt = self.service.reserve_attempt(task_id=task_id)
        self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id, kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key="row3:previous-attempt:thread", request={"probe": "row3"},
        )
        job = self._lost_job("row3", kind=RuntimeJobKind.REPLANNING,
                             request={"assessment": {}, "evidence_documents": []}, attempt_id=attempt.attempt_id)
        dispatcher = self._dispatcher(self._supervisor())
        self._record_loss(dispatcher, job)
        self._assert_blocked_without_writes(lambda: dispatcher.run_once(self.project_id), "REPLAN_PROVIDER_REQUIRED")
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)

    def test_k5b_a_reservation_outside_the_attribution_rule_is_external_effect_unknown(self) -> None:
        """역할 job의 귀속 규칙(s0 뒤·attempt 없음·reserved·receipt 없음) 밖 미해결 예약은 효과 근거다(8번)."""

        from flowmarshal.engine.budget import BudgetManager

        goal_id, goal_digest = self._rows(
            "SELECT goal_id,definition_digest FROM goal_revisions WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
            self.project_id,
        )[0]
        older = BudgetManager(self.service).reserve(
            project_id=self.project_id, goal_id=goal_id, goal_digest=goal_digest, call_key="k5b-older",
            role="plan_expander", request={"probe": "k5b"},
        )
        job = self._lost_job("k5b", kind=RuntimeJobKind.REPLANNING, request={"assessment": {}, "evidence_documents": []})
        dispatcher = self._dispatcher(self._supervisor())
        self._record_loss(dispatcher, job)
        blocked = self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "EXTERNAL_EFFECT_UNKNOWN",
        )
        self.assertIn("evidence=provider_call", blocked.detail)
        self.assertEqual("reserved", self._rows("SELECT status FROM provider_calls WHERE id=?", older)[0][0])

    def test_k10_worker_start_failure_is_recorded_then_restarted_once(self) -> None:
        owner = self._supervisor()
        calls: list[int] = []
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread start failed")):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                owner.schedule(
                    project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key="k10",
                    request={"assessment": {"probe": "k10"}}, timeout_seconds=60, target=lambda: calls.append(1),
                )
        job = self.service.active_runtime_job(self.project_id)
        self.assertIs(RuntimeJobStatus.COLLECTOR_LOST, owner.tick(job.job_id).status)
        self.assertIs(OwnerLockState.FREE, probe_owner_lock(self._lock_path("k10")))
        restarted = self._dispatcher(owner).run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        owner._workers[job.job_id].join(5)
        self.assertIs(RuntimeJobStatus.PROVIDER_TERMINAL, owner.tick(job.job_id).status)
        self.assertEqual({"assessment": {"probe": "k10"}}, self.service.consume_runtime_job(job.job_id))
        self.assertEqual([], calls)

    def test_k14_job_deadline_is_never_extended_and_elapsed_loss_is_owner_lost(self) -> None:
        # (a) 재시작 claim은 transaction 안에서 저장 deadline과 이전 재시작을 다시 본다(재계산·연장 없음).
        past = self._lost_job("k14-past", deadline_seconds=0.2)
        self.assertTrue(_wait_until(lambda: utc_now() >= past.absolute_deadline_at, 5))
        self.service.record_runtime_job_observation(
            past.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "k14"},
        )
        before = _ledger_copy(self.service)
        claimed, won, _epoch = self.service._claim_runtime_job_restart(
            past.job_id, restart={"reason": "owner_lost_no_effect_proven", "epoch_sequence": 0,
                                  "evidence_digest": "sha256:" + "0" * 64, "restart_no": 1},
        )
        self.assertEqual((False, RuntimeJobStatus.COLLECTOR_LOST), (won, claimed.status))
        self.assertEqual(before, _ledger_copy(self.service))
        self.service.cancel_runtime_job(past.job_id, reason="다음 경우 격리")
        # (a2) 같은 job의 두 번째 재시작 claim도 transaction 안에서 거절된다(재시작 0회 재검사).
        twice = self._lost_job("k14-twice")
        restart = {"reason": "owner_lost_no_effect_proven", "epoch_sequence": 0,
                   "evidence_digest": "sha256:" + "3" * 64, "restart_no": 1}
        self.service.record_runtime_job_observation(
            twice.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "k14-1"},
        )
        self.assertTrue(self.service._claim_runtime_job_restart(twice.job_id, restart=restart)[1])
        self.service.record_runtime_job_observation(
            twice.job_id, kind=RuntimeJobObservationKind.COLLECTOR_LOST, payload={"reason": "k14-2"},
        )
        before = _ledger_copy(self.service)
        self.assertFalse(self.service._claim_runtime_job_restart(
            twice.job_id, restart=restart | {"restart_no": 2})[1])
        self.assertEqual(before, _ledger_copy(self.service))
        self.service.cancel_runtime_job(twice.job_id, reason="다음 경우 격리")
        # (b) job deadline이 지난 뒤(승인 deadline 전) 발견한 효과 없는 소실은 7j다.
        job = self._lost_job("k14")
        late = job.absolute_deadline_at + timedelta(seconds=1)
        self.assertLess(late, self._approval_deadline())
        supervisor = self._supervisor(clock=lambda: late)
        dispatcher = self._dispatcher(supervisor)
        self._record_loss(dispatcher, job)
        blocked = self._assert_blocked_without_writes(
            lambda: dispatcher.run_once(self.project_id), "RUNTIME_JOB_OWNER_LOST",
        )
        self.assertEqual("effect_state=none_proven; reason=job_deadline_elapsed", blocked.detail)
        self.assertNotIn(job.job_id, supervisor._workers)
        self.assertEqual(0, self._kinds(job.job_id).count("collector_reattached"))
        self.assertEqual(job.absolute_deadline_at, self.service.load_runtime_job(job.job_id).absolute_deadline_at)

    # --- 10.5 LP2 --------------------------------------------------------------------------------

    def test_lp2_every_typed_stop_leaves_the_ledger_unchanged(self) -> None:
        def worker_failure(checkpoint: str):
            owner = self._supervisor()

            def target():
                raise RuntimeError("target 실패")

            job = owner.schedule(
                project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key=checkpoint,
                request={"assessment": {"probe": checkpoint}}, timeout_seconds=60, target=target,
            )
            owner._workers[job.job_id].join(5)
            self.assertEqual(RunOnceAction.OBSERVED, self._dispatcher(owner).run_once(self.project_id).action)
            return job

        def restarted_once(checkpoint: str):
            job = self._lost_job(checkpoint)
            self._record_loss(self._dispatcher(self._supervisor()), job)
            _job, won, _epoch = self.service._claim_runtime_job_restart(
                job.job_id, restart={"reason": "owner_lost_no_effect_proven", "epoch_sequence": 1,
                                     "evidence_digest": "sha256:" + "1" * 64, "restart_no": 1},
            )
            self.assertTrue(won)
            return job

        def role_requested(checkpoint: str):
            job = self._lost_job(checkpoint, kind=RuntimeJobKind.REPLANNING,
                                 request={"assessment": {}, "evidence_documents": []})
            self.service.record_runtime_job_observation(
                job.job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                payload={"role_progress": {"event": "role_requested", "call_id": checkpoint}},
            )
            return job

        spec_request = {"task_id": "task_lp2", "supplied_proposal_digest": None,
                        "inventory_observation": "runtime_job_owned"}
        cases = (
            ("5", "RUNTIME_EFFECT_PREFLIGHT_FAILED", lambda: worker_failure("lp2-5"), {}),
            ("6p", "REPLAN_PROVIDER_REQUIRED", lambda: self._lost_job(
                "lp2-6p", kind=RuntimeJobKind.REPLANNING, request={"assessment": {}, "evidence_documents": []}), {}),
            ("6e", "EXECUTION_SPEC_PROPOSAL_REQUIRED", lambda: self._lost_job(
                "lp2-6e", kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE, request=spec_request), {}),
            ("7", "RUNTIME_JOB_OWNER_LOST", lambda: restarted_once("lp2-7"), {}),
            ("7j", "RUNTIME_JOB_OWNER_LOST", lambda: self._lost_job("lp2-7j"), {"late": True}),
            ("7k", "RUNTIME_JOB_OWNER_LOST", lambda: self._lost_job(
                "lp2-7k", kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
                request=spec_request | {"supplied_proposal_digest": "sha256:" + "2" * 64}), {}),
            ("7g", "RUNTIME_JOB_OWNER_LOST", lambda: self._lost_job(
                "lp2-7g", kind=RuntimeJobKind.GOAL_TEST_PREPARE, request={"probe": "7g"}), {}),
            ("8", "EXTERNAL_EFFECT_UNKNOWN", lambda: role_requested("lp2-8"), {}),
        )
        for row, code, make, options in cases:
            with self.subTest(row=row):
                job = make()
                clock = (lambda: job.absolute_deadline_at + timedelta(seconds=1)) if options.get("late") else utc_now
                supervisor = self._supervisor(clock=clock)
                dispatcher = self._dispatcher(supervisor)
                if self.service.load_runtime_job(job.job_id).status is RuntimeJobStatus.RUNNING:
                    self._record_loss(dispatcher, job)
                blocked = self._assert_blocked_without_writes(lambda: dispatcher.run_once(self.project_id), code)
                verdict = classify_unbound_runtime_job(
                    self.service, self.service.load_runtime_job(job.job_id), lock_state=OwnerLockState.FREE,
                    provider_available=False, now=clock(),
                )
                self.assertEqual((row, code), (verdict.row, verdict.blocker_code))
                self.assertFalse(verdict.writes_ledger)
                if row == "6p":
                    self.assertEqual(REPLAN_PROVIDER_REQUIRED_DETAIL, blocked.detail)
                # typed 정지 뒤 tick(observe)도 원장을 바꾸지 않는다.
                before = _ledger_copy(self.service)
                supervisor.tick(job.job_id)
                self.assertEqual(before, _ledger_copy(self.service))
                self.service.cancel_runtime_job(job.job_id, reason="다음 subTest 격리")
        # 0p는 I3a P1~P5가 같은 단언(run_once×3 원장 무변경)으로 본다.

    # --- 10.4c SR2 (단일 run-once 규칙에서 typed 정지) ------------------------------------------------

    def test_sr2_scheduler_run_once_keeps_every_typed_stop_without_writes(self) -> None:
        application = EngineApplication(self.service, runtime=self.runtime, supervisor=self._supervisor(),
                                        governance=ALLOW_ALL)
        goal = self._lost_job("sr2-7g", kind=RuntimeJobKind.GOAL_SEMANTIC_VALIDATE, request={"probe": "sr2"})
        self.assertEqual(RunOnceAction.OBSERVED, application.run_once(self.project_id).action)
        self._assert_blocked_without_writes(lambda: application.run_once(self.project_id), "RUNTIME_JOB_OWNER_LOST")
        self.service.cancel_runtime_job(goal.job_id, reason="다음 경우 격리")
        role = self._lost_job("sr2-8", kind=RuntimeJobKind.REPLANNING,
                              request={"assessment": {}, "evidence_documents": []})
        self.service.record_runtime_job_observation(
            role.job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
            payload={"role_progress": {"event": "role_requested", "call_id": "sr2"}},
        )
        self.assertEqual(RunOnceAction.OBSERVED, application.run_once(self.project_id).action)
        self._assert_blocked_without_writes(lambda: application.run_once(self.project_id), "EXTERNAL_EFFECT_UNKNOWN")
        self.service.cancel_runtime_job(role.job_id, reason="다음 경우 격리")
        self._service_job("sr2-0p", lock_file=False)
        blocked = self._assert_blocked_without_writes(
            lambda: application.run_once(self.project_id), "RUNTIME_OWNER_LOCK_UNAVAILABLE",
        )
        self.assertEqual(OWNER_PROOF_MISSING_DETAIL, blocked.detail)

    # --- 10.5 F0·F1·F3·F4·F5, 10.3 N8 -----------------------------------------------------------

    def _held_dispatch(self, point: str, runtime=None):
        """dispatch worker를 ``point``(fault hook 또는 runtime create 안)에서 붙잡는다."""

        gate, entered = threading.Event(), threading.Event()
        self._gates.append(gate)

        def hold(name: str) -> None:
            if name == point and not entered.is_set():
                entered.set()
                gate.wait(10)

        class HeldRuntime(FakeCodexRuntime):
            def create_thread(inner, **kwargs):
                hold("inside_create")
                return super().create_thread(**kwargs)

        runtime = runtime or HeldRuntime(self.inventory)
        supervisor = self._supervisor(runtime=runtime)
        dispatcher = self._dispatcher(supervisor, fault_hook=hold)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        dispatched = dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        self.assertTrue(entered.wait(5))
        return dispatcher, runtime, supervisor, gate, dispatched

    def test_f0_inline_dispatch_without_a_job_skips_the_job_fence(self) -> None:
        seen: list = []
        real = self.service.prepare_authorized_runtime_effect

        def spy(*args, **kwargs):
            seen.append(kwargs.get("runtime_job_id"))
            return real(*args, **kwargs)

        self.service.prepare_authorized_runtime_effect = spy
        self.addCleanup(setattr, self.service, "prepare_authorized_runtime_effect", real)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        dispatched = EngineDispatcher(self.service, self.runtime).run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, dispatched.action, dispatched)
        self.assertEqual([None, None], seen)
        self.assertEqual((1, 1), (self.runtime.create_calls, self.runtime.turn_calls))
        self.assertEqual([], self._history_payloads("runtime.effect_not_started"))

    def test_f1_job_cancelled_before_the_marker_starts_no_create(self) -> None:
        _dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("after_thread_intent")
        self.service.cancel_runtime_job(dispatched.runtime_job_id, reason="F1 cancel 직전")
        gate.set()
        supervisor._workers[dispatched.runtime_job_id].join(10)
        self.assertEqual(0, runtime.create_calls)
        refused = self._history_payloads("runtime.effect_not_started")
        self.assertEqual(["RUNTIME_EFFECT_PREFLIGHT_FAILED"], [item["code"] for item in refused])
        self.assertTrue(refused[0]["detail"].startswith("RUNTIME_JOB_NOT_RUNNING"), refused)
        self.assertEqual([], self._history_payloads("runtime.effect_dispatching"))

    def test_f1_workflow_cancel_keeps_the_existing_workflow_cancelled_code(self) -> None:
        _dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("after_thread_intent")
        application = EngineApplication(self.service, runtime=runtime, supervisor=supervisor, governance=ALLOW_ALL)
        self.assertEqual("cancelled", application.cancel(self.project_id)["control_state"])
        gate.set()
        supervisor._workers[dispatched.runtime_job_id].join(10)
        self.assertEqual(0, runtime.create_calls)
        self.assertEqual(["WORKFLOW_CANCELLED"],
                         [item["code"] for item in self._history_payloads("runtime.effect_not_started")])

    def test_f3_the_fence_and_the_marker_share_one_transaction_and_a_later_cancel_is_honest(self) -> None:
        tagged: list = []
        real_assert = EngineService._assert_runtime_job_runnable
        real_history = EngineTransaction.history

        def fence(tx, row):
            tx._i3b_fence_checked = True
            return real_assert(tx, row)

        def history(tx, project_id, event_type, *args, **kwargs):
            if event_type == "runtime.effect_dispatching":
                tagged.append(getattr(tx, "_i3b_fence_checked", False))
            return real_history(tx, project_id, event_type, *args, **kwargs)

        with (
            mock.patch.object(EngineService, "_assert_runtime_job_runnable", staticmethod(fence)),
            mock.patch.object(EngineTransaction, "history", history),
        ):
            _dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("inside_create")
            # marker commit 뒤에 job이 취소됐다. 이미 시작된 create는 진행하고(정직한 효과) start는 fence가 막는다.
            self.service.cancel_runtime_job(dispatched.runtime_job_id, reason="F3 marker 뒤 cancel")
            gate.set()
            supervisor._workers[dispatched.runtime_job_id].join(10)
        self.assertEqual([True], tagged)  # create marker는 fence를 검사한 transaction에서만 쓰였다.
        self.assertEqual((1, 0), (runtime.create_calls, runtime.turn_calls))
        self.assertEqual(["RUNTIME_EFFECT_PREFLIGHT_FAILED"],
                         [item["code"] for item in self._history_payloads("runtime.effect_not_started")])
        marker = self._rows("SELECT MIN(sequence) FROM history_events WHERE project_id=? "
                            "AND event_type='runtime.effect_dispatching'", self.project_id)[0][0]
        cancelled = self._rows("SELECT MIN(sequence) FROM history_events WHERE entity_id=? "
                               "AND event_type='runtime_job.cancelled'", dispatched.runtime_job_id)[0][0]
        self.assertLess(marker, cancelled)

    def test_f4_the_marker_after_the_job_deadline_starts_no_effect(self) -> None:
        _dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("after_thread_intent")
        job = self.service.load_runtime_job(dispatched.runtime_job_id)

        class LateClock:
            def now(self_inner) -> str:
                return (job.absolute_deadline_at + timedelta(seconds=1)).isoformat()

        with mock.patch.object(self.service.ledger, "clock", LateClock()):
            gate.set()
            supervisor._workers[job.job_id].join(10)
        self.assertEqual(0, runtime.create_calls)
        refused = self._history_payloads("runtime.effect_not_started")
        self.assertEqual(["RUNTIME_EFFECT_PREFLIGHT_FAILED"], [item["code"] for item in refused])
        self.assertIn("RUNTIME_JOB_NOT_RUNNING", refused[0]["detail"])

    def test_f5_an_interrupting_job_is_not_refused(self) -> None:
        _dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("after_thread_intent")
        supervisor.request_interrupt(dispatched.runtime_job_id, reason="workflow_paused")
        self.assertIs(RuntimeJobStatus.INTERRUPTING, self.service.load_runtime_job(dispatched.runtime_job_id).status)
        gate.set()
        supervisor._workers[dispatched.runtime_job_id].join(10)
        self.assertEqual((1, 1), (runtime.create_calls, runtime.turn_calls))
        self.assertEqual([], self._history_payloads("runtime.effect_not_started"))
        self.assertEqual(1, self._kinds(dispatched.runtime_job_id).count("interrupt_receipt"))

    def test_n8_pause_before_binding_then_resume_continues_the_same_attempt(self) -> None:
        dispatcher, runtime, supervisor, gate, dispatched = self._held_dispatch("after_thread_intent")
        application = EngineApplication(self.service, runtime=runtime, supervisor=supervisor, governance=ALLOW_ALL)
        paused = application.pause(self.project_id)
        self.assertEqual(("paused", "interrupting"), (paused["control_state"], paused["runtime_job"]["status"]))
        gate.set()
        supervisor._workers[dispatched.runtime_job_id].join(10)
        # binding 직후 interrupt를 전달하는 기존 의미. 효과 직전 검사가 INTERRUPTING을 막지 않는다.
        self.assertEqual((1, 1), (runtime.create_calls, runtime.turn_calls))
        self.assertEqual(1, self._kinds(dispatched.runtime_job_id).count("interrupt_receipt"))
        self.assertEqual([], self._history_payloads("runtime.effect_not_started"))
        blocked = application.run_once(self.project_id)
        self.assertEqual("WORKFLOW_PAUSED", blocked.blocker_code)
        resumed = application.run_once(self.project_id, resume=True)
        self.assertNotEqual(RunOnceAction.BLOCKED, resumed.action, resumed)
        outcome = None
        for _ in range(50):
            outcome = application.run_once(self.project_id)
            self.assertNotEqual(RunOnceAction.BLOCKED, outcome.action, outcome)
            if runtime.turn_calls == 2:
                break
            time.sleep(0.01)
        self.assertEqual((1, 2, 1), (runtime.create_calls, runtime.turn_calls, runtime.resume_calls))
        self._converge(dispatcher, runtime, dispatched.attempt_id, application=application)
        self.assertEqual([], self._history_payloads("runtime.effect_not_started"))

    # --- 10.6 S1~S4 status 정합(I4) -----------------------------------------------------------

    def _status_application(self, supervisor: RuntimeJobSupervisor) -> EngineApplication:
        """역할 설정 없는 인스턴스. AC1 predicate와 run_once의 kind별 provider가 함께 없다(risks G8)."""

        application = EngineApplication(
            self.service, runtime=supervisor.runtime, supervisor=supervisor, governance=ALLOW_ALL,
        )
        self.assertFalse(application.recovery_provider_available())
        return application

    def _settle_case(self, job) -> None:
        """다음 경우가 같은 활성 job 자리를 쓰도록 이 경우의 job을 활성 상태에서 내보낸다."""

        for gate in self._gates:
            gate.set()
        for supervisor in self._supervisors:
            worker = supervisor._workers.get(job.job_id)
            if worker is not None:
                worker.join(10)
                supervisor.tick(job.job_id)
        if self.service.load_runtime_job(job.job_id).status in {
            RuntimeJobStatus.SCHEDULED, RuntimeJobStatus.RUNNING,
            RuntimeJobStatus.INTERRUPTING, RuntimeJobStatus.COLLECTOR_LOST,
        }:
            self.service.cancel_runtime_job(job.job_id, reason="다음 subTest 격리")

    def test_s1_status_shows_the_run_once_verdict_for_every_routing_row(self) -> None:
        def schedule(checkpoint: str):
            return self.service.schedule_runtime_job(
                project_id=self.project_id, kind=RuntimeJobKind.RECOVERY, checkpoint_key=checkpoint,
                request={"assessment": {"probe": checkpoint}},
                absolute_deadline_at=utc_now() + timedelta(seconds=60),
            )

        def unopenable(checkpoint: str):
            # 0행: lock 경로가 디렉터리라 open이 PermissionError다(충돌 외 lock 오류, 비durable 판정).
            self._lock_path(checkpoint).mkdir(parents=True)
            return self.service._claim_runtime_job_start(schedule(checkpoint).job_id)[0]

        def live_owner(checkpoint: str):
            target, _gate, entered, _calls = self._blocking_target()
            job = self._schedule(self._supervisor(), checkpoint, target)
            self.assertTrue(entered.wait(5))
            return job

        def orphan(checkpoint: str):
            path = self._lock_path(checkpoint)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            return schedule(checkpoint)

        def worker_failure(checkpoint: str):
            owner = self._supervisor()

            def target():
                raise RuntimeError("target 실패")

            job = self._schedule(owner, checkpoint, target)
            owner._workers[job.job_id].join(5)
            self.assertEqual(RunOnceAction.OBSERVED, self._dispatcher(owner).run_once(self.project_id).action)
            return job

        def lost(checkpoint: str, **kwargs):
            job = self._lost_job(checkpoint, **kwargs)
            self._record_loss(self._dispatcher(self._supervisor()), job)
            return job

        def restarted_once(checkpoint: str):
            job = lost(checkpoint)
            self.assertTrue(self.service._claim_runtime_job_restart(
                job.job_id, restart={"reason": "owner_lost_no_effect_proven", "epoch_sequence": 1,
                                     "evidence_digest": "sha256:" + "4" * 64, "restart_no": 1},
            )[1])
            self._record_loss(self._dispatcher(self._supervisor()), job)
            return job

        def role_requested(checkpoint: str):
            job = self._lost_job(checkpoint, kind=RuntimeJobKind.REPLANNING, request=role)
            self.service.record_runtime_job_observation(
                job.job_id, kind=RuntimeJobObservationKind.PROVIDER_PROGRESS,
                payload={"role_progress": {"event": "role_requested", "call_id": checkpoint}},
            )
            self._record_loss(self._dispatcher(self._supervisor()), job)
            return job

        def worker_turn(checkpoint: str):
            # failure 없는 dispatch kind(J2). 효과 근거 없는 소실이 job deadline 뒤에 발견된다(7j).
            self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
            task_id = self._rows(
                "SELECT id FROM task_contracts WHERE project_id=? AND status='materialized'", self.project_id,
            )[0][0]
            attempt = self.service.reserve_attempt(task_id=task_id)
            return lost(checkpoint, kind=RuntimeJobKind.WORKER_TURN, request={"validation_id": None},
                        attempt_id=attempt.attempt_id)

        role = {"assessment": {}, "evidence_documents": []}
        spec = {"task_id": "task_s1", "supplied_proposal_digest": None, "inventory_observation": "runtime_job_owned"}
        decision = ("user_decision_required", "user_decision")
        observe_first = ("observe_first_required", "observe_first")
        automatic = ("automatic_pending", "automatic", None)
        lost_code = "RUNTIME_JOB_OWNER_LOST"
        cases = (
            ("0", unopenable, None, (*decision, "RUNTIME_OWNER_LOCK_UNAVAILABLE"), RunOnceAction.BLOCKED),
            ("0p", lambda c: self._service_job(c, lock_file=False), None,
             (*observe_first, "RUNTIME_OWNER_LOCK_UNAVAILABLE"), RunOnceAction.BLOCKED),
            ("2", live_owner, None, ("none", "none", None), RunOnceAction.OBSERVED),
            ("C", self._lost_job, None, automatic, RunOnceAction.OBSERVED),
            ("S", orphan, None, automatic, RunOnceAction.DISPATCHED),
            ("5", worker_failure, None, (*decision, "RUNTIME_EFFECT_PREFLIGHT_FAILED"), RunOnceAction.BLOCKED),
            ("6", lost, None, automatic, RunOnceAction.DISPATCHED),
            ("6p", lambda c: lost(c, kind=RuntimeJobKind.REPLANNING, request=role), None,
             (*decision, "REPLAN_PROVIDER_REQUIRED"), RunOnceAction.BLOCKED),
            ("6e", lambda c: lost(c, kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE, request=spec), None,
             (*decision, "EXECUTION_SPEC_PROPOSAL_REQUIRED"), RunOnceAction.BLOCKED),
            ("7", restarted_once, None, (*decision, lost_code), RunOnceAction.BLOCKED),
            ("7d", lost, "approval", (*decision, "GOAL_ABSOLUTE_DEADLINE_EXCEEDED"), RunOnceAction.BLOCKED),
            ("7j", lost, "job", (*decision, lost_code), RunOnceAction.BLOCKED),
            ("7k", lambda c: lost(c, kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE,
                                  request=spec | {"supplied_proposal_digest": "sha256:" + "2" * 64}), None,
             (*decision, lost_code), RunOnceAction.BLOCKED),
            ("7g", lambda c: lost(c, kind=RuntimeJobKind.GOAL_TEST_PREPARE, request={"probe": c}), None,
             (*observe_first, lost_code), RunOnceAction.BLOCKED),
            ("7g", lambda c: lost(c, kind=RuntimeJobKind.GOAL_SEMANTIC_VALIDATE, request={"probe": c}), None,
             (*observe_first, lost_code), RunOnceAction.BLOCKED),
            ("8", role_requested, None, (*observe_first, "EXTERNAL_EFFECT_UNKNOWN"), RunOnceAction.BLOCKED),
            ("7j", worker_turn, "job", (*decision, lost_code), RunOnceAction.BLOCKED),
        )
        for index, (row, make, late, expected, action) in enumerate(cases):
            checkpoint = f"s1-{index}-{row}"
            with self.subTest(row=row, checkpoint=checkpoint):
                job = make(checkpoint)
                try:
                    self._assert_s1_case(job, late, expected, action)
                finally:
                    # 단언이 실패해도 다음 경우가 이 job의 활성 자리에 걸리지 않게 한다.
                    self._settle_case(job)

    def _assert_s1_case(self, job, late: str | None, expected: tuple, action: RunOnceAction) -> None:
        now = {
            None: None,
            "approval": self._approval_deadline() + timedelta(seconds=1),
            "job": job.absolute_deadline_at + timedelta(seconds=1),
        }[late]
        application = self._status_application(
            self._supervisor(clock=utc_now if now is None else (lambda: now)),
        )
        status = _read_only_status(self, application, self.project_id)
        recovery = status["recovery"]
        self.assertEqual(expected, _status_row(status), recovery)
        self.assertEqual(job.job_id, status["active_runtime_job"]["job_id"])
        # 활성 job 안내도 같은 판정의 문구다(기존 "observe로 … 먼저 관측" 문구가 아니다).
        self.assertEqual(recovery["next_action"]["detail"], status["next_action"])
        self.assertNotIn(_BEFORE_EFFECT, recovery["next_action"]["detail"])
        outcome = application.run_once(self.project_id)
        self.assertEqual((action, expected[2]), (outcome.action, outcome.blocker_code), outcome)
        if outcome.blocker_code is not None:
            self.assertTrue(
                recovery["next_action"]["detail"].startswith(outcome.detail.rstrip(".")),
                (recovery["next_action"], outcome),
            )
            self.assertEqual(
                (outcome.checkpoint_required,
                 None if outcome.suggested_repair_action is None else outcome.suggested_repair_action.value),
                (recovery["next_action"]["checkpoint_required"],
                 recovery["next_action"]["suggested_repair_action"]),
            )

    def test_s2_live_owner_before_binding_is_not_automatic_pending_for_a_worker_turn(self) -> None:
        _dispatcher, runtime, owner, gate, dispatched = self._held_dispatch("after_thread_intent")
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory))
        # 같은 supervisor(판정 1, 재진입)와 다른 supervisor(판정 2) 모두 owner 실행 중이다(J1 (A)).
        for application in (
            EngineApplication(self.service, runtime=runtime, supervisor=owner, governance=ALLOW_ALL),
            EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL),
        ):
            status = _read_only_status(self, application, self.project_id)
            self.assertEqual(("none", "none", None), _status_row(status))
            self.assertEqual("owner 실행 중: 다음 run-once·observe는 관측만 합니다.",
                             status["recovery"]["next_action"]["detail"])
            self.assertEqual(status["recovery"]["next_action"]["detail"], status["next_action"])
            self.assertIsNone(status["recovery"]["classification"])  # failure 없는 job(J2)
            self.assertEqual("executing_task", status["current_stage"])  # current_stage 값은 그대로다
            observed = application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.OBSERVED, None), (observed.action, observed.blocker_code), observed)
        gate.set()
        owner._workers[dispatched.runtime_job_id].join(10)

    def test_s2_unresponsive_owner_detail_is_the_same_as_run_once(self) -> None:
        owner = self._supervisor()
        target, gate, entered, _calls = self._blocking_target()
        job = self._schedule(owner, "s2-late", target, timeout=0.05)
        self.assertTrue(entered.wait(5))
        self.assertTrue(_wait_until(lambda: utc_now() >= job.absolute_deadline_at, 5))
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory), terminal_observation_grace_seconds=0)
        application = EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL)
        status = _read_only_status(self, application, self.project_id)
        self.assertEqual(("none", "none", None), _status_row(status))
        detail = status["recovery"]["next_action"]["detail"]
        self.assertEqual("owner가 deadline 뒤에도 lock을 쥐고 있음(응답 없음)", detail)
        observed = application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, observed.action, observed)
        self.assertIn(detail, observed.detail)
        gate.set()

    def test_s2_same_process_owner_past_deadline_plus_grace_is_running_not_unresponsive(self) -> None:
        """T r4 공백 G6(AC18): status probe는 자기 supervisor가 쥔 lease를 HELD_SELF(판정 1)로 본다.

        차이는 deadline+grace 뒤 문구에만 드러난다. 자기 owner는 "owner 실행 중", 다른 holder는 "응답 없음"이다.
        """

        running = "owner 실행 중: 다음 run-once·observe는 관측만 합니다."
        unresponsive = "owner가 deadline 뒤에도 lock을 쥐고 있음(응답 없음)"
        owner = self._supervisor(terminal_observation_grace_seconds=0)
        target, gate, entered, _calls = self._blocking_target()
        job = self._schedule(owner, "s2-self-late", target, timeout=0.05)
        self.assertTrue(entered.wait(5))
        self.assertTrue(_wait_until(lambda: utc_now() >= job.absolute_deadline_at, 5))
        same = EngineApplication(self.service, runtime=owner.runtime, supervisor=owner, governance=ALLOW_ALL)
        status = _read_only_status(self, same, self.project_id)
        self.assertEqual(("none", "none", None), _status_row(status))
        self.assertEqual(running, status["recovery"]["next_action"]["detail"])
        self.assertEqual(running, status["next_action"])
        other_service = self._second_service()
        other = self._supervisor(other_service, FakeCodexRuntime(self.inventory), terminal_observation_grace_seconds=0)
        other_status = _read_only_status(
            self, EngineApplication(other_service, runtime=other.runtime, supervisor=other, governance=ALLOW_ALL),
            self.project_id,
        )
        self.assertEqual(("none", "none", None), _status_row(other_status))
        self.assertEqual(unresponsive, other_status["recovery"]["next_action"]["detail"])
        # 같은 supervisor의 run_once 라우터도 응답 없음 문구 없이 owner tick만 한다(status와 같은 판정 1).
        observed = same.run_once(self.project_id)
        self.assertEqual((RunOnceAction.OBSERVED, None), (observed.action, observed.blocker_code), observed)
        self.assertNotIn(unresponsive, observed.detail)
        gate.set()

    def test_s3_status_probe_creates_no_lock_file_and_keeps_no_lease(self) -> None:
        application = self._status_application(self._supervisor())
        absent = self._service_job("s3-absent", lock_file=False)
        status = _read_only_status(self, application, self.project_id)  # 원장·파일 목록·registry 불변
        self.assertEqual(("observe_first_required", "observe_first", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         _status_row(status))
        self.assertFalse(self._lock_path("s3-absent").exists())
        self.service.cancel_runtime_job(absent.job_id, reason="다음 경우 격리")
        self._lost_job("s3-free")
        status = _read_only_status(self, application, self.project_id)
        self.assertEqual(("automatic_pending", "automatic", None), _status_row(status))  # C
        # probe가 잡은 lock을 곧바로 놓았으므로 다른 holder가 바로 얻는다.
        state, lease = runtime_module._acquire_owner_lease(self._lock_path("s3-free"), object(), create=False)
        self.assertIs(OwnerLockState.FREE, state)
        runtime_module._release_owner_lease(lease)

    def test_s4_posix_status_shows_the_run_once_blocker_with_the_windows_only_guidance(self) -> None:
        self._lost_job("s4")
        application = self._status_application(self._supervisor())
        with mock.patch.object(runtime_module, "owner_lock_platform_supported", lambda: False):
            status = _read_only_status(self, application, self.project_id)
            before = _ledger_copy(self.service)
            blocked = application.run_once(self.project_id)
            self.assertEqual(before, _ledger_copy(self.service))
        self.assertEqual(("user_decision_required", "user_decision", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         _status_row(status))
        self.assertEqual(
            (RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE", RUNTIME_OWNER_PLATFORM_UNSUPPORTED),
            (blocked.action, blocked.blocker_code, blocked.detail), blocked,
        )
        detail = status["recovery"]["next_action"]["detail"]
        self.assertTrue(detail.startswith(RUNTIME_OWNER_PLATFORM_UNSUPPORTED), detail)
        self.assertIn("Windows 전용", detail)
        self.assertEqual(detail, status["next_action"])

    def test_s4_posix_status_without_a_job_shows_the_run_once_blocker(self) -> None:
        """AC19 L7 조건: 활성 Plan이 있고 활성 job이 0이면 run_once는 job 생성 전에 멈춘다. status도 같다."""

        application = self._status_application(self._supervisor())
        windows = _read_only_status(self, application, self.project_id)
        with mock.patch.object(runtime_module, "owner_lock_platform_supported", lambda: False):
            status = _read_only_status(self, application, self.project_id)
            before = _ledger_copy(self.service)
            blocked = application.run_once(self.project_id)
            self.assertEqual(before, _ledger_copy(self.service))
        self.assertIsNone(status["active_runtime_job"])
        self.assertIsNone(_owner_files(self.service, self.project_id))  # lock 파일·폴더를 만들지 않았다
        self.assertEqual(("user_decision_required", "user_decision", "RUNTIME_OWNER_LOCK_UNAVAILABLE"),
                         _status_row(status))
        self.assertEqual(
            (RunOnceAction.BLOCKED, "RUNTIME_OWNER_LOCK_UNAVAILABLE", RUNTIME_OWNER_PLATFORM_UNSUPPORTED),
            (blocked.action, blocked.blocker_code, blocked.detail), blocked,
        )
        detail = status["recovery"]["next_action"]["detail"]
        # 판정 0행과 같은 상수 두 개를 그대로 잇는다(문구를 새로 만들지 않는다).
        self.assertEqual(
            f"{RUNTIME_OWNER_PLATFORM_UNSUPPORTED} {runtime_module.RUNTIME_OWNER_PLATFORM_GUIDANCE}", detail,
        )
        self.assertIn("Windows 전용", detail)
        self.assertEqual(detail, status["next_action"])
        # 지원 플랫폼의 같은 원장 status는 기존 안내 그대로다.
        self.assertEqual(("none", "none", None), _status_row(windows))
        self.assertEqual("run-once로 Task 실행 명세 준비를 시작하십시오.", windows["next_action"])
        self.assertEqual(windows["current_stage"], status["current_stage"])


class DispatchOwnerKillProcessTests(_OwnerKillMixin, _PreparedProject):
    """실제 subprocess CLI owner의 WORKER_TURN dispatch를 create RPC 안·receipt 뒤·예약 뒤에 kill한다(K2·K3·K12).

    부모와 child는 같은 원장과 파일 공유 fake runtime을 쓴다. 효과 수는 journal과 원장을 대조한다.
    """

    def setUp(self) -> None:
        super().setUp()
        self.work = self.base / "owner"
        self.work.mkdir()
        self.runtime = _SharedRuntime(self.work, self.inventory)
        self.supervisor = self._supervisor()
        self.dispatcher = self._dispatcher(self.supervisor)
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)

    def _lose_dispatch_owner(self, block: str):
        process = self._start_owner(self.work, {"block": block, "qualification": True})
        job = self.service.active_runtime_job(self.project_id)
        self.assertEqual(RuntimeJobKind.WORKER_TURN, job.kind)
        self._kill_owner(process, job)
        self.assertEqual(RunOnceAction.OBSERVED, self.dispatcher.run_once(self.project_id).action)  # C
        self.assertEqual(RuntimeJobStatus.COLLECTOR_LOST, self.service.load_runtime_job(job.job_id).status)
        return job

    def _effects(self) -> list[str]:
        return [entry for entry in _journal_entries(self.work) if entry in {"create", "start", "resume"}]

    def test_k2_killed_inside_the_create_rpc_keeps_the_prepared_path_and_no_second_create(self) -> None:
        job = self._lose_dispatch_owner("create")
        routed = self.dispatcher.run_once(self.project_id)
        # 3번: job을 닫고 기존 prepared 경로가 marker 뒤 효과를 unknown으로 보존한다(결정 D6).
        self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"),
                         (routed.action, routed.blocker_code), routed)
        self.assertEqual("cancelled", self.service.load_runtime_job(job.job_id).status.value)
        self.assertEqual([("create_thread", "unknown")], self._rows(
            "SELECT kind,status FROM runtime_intents WHERE attempt_id=?", job.attempt_id))
        for _ in range(2):
            self.assertEqual(RunOnceAction.BLOCKED, self.dispatcher.run_once(self.project_id).action)
        self.assertEqual(["create"], self._effects())
        self.assertEqual(0, self._kinds(job.job_id).count("collector_reattached"))

    def test_k3_killed_after_the_create_receipt_resumes_the_same_thread(self) -> None:
        job = self._lose_dispatch_owner("hit:after_thread_receipt")
        self.assertEqual(["create"], self._effects())
        routed = self.dispatcher.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, routed.action, routed)
        cancelled = self._observations(job.job_id, "cancelled")
        self.assertEqual(["owner_lost_after_thread_receipt"], [item["reason"] for item in cancelled])
        self.assertEqual(["create", "resume", "start"], self._effects())
        self._converge(self.dispatcher, self.runtime, job.attempt_id)
        self.assertEqual(["create", "resume", "start"], self._effects())
        self.assertEqual(1, self._rows(
            "SELECT COUNT(*) FROM runtime_intents WHERE attempt_id=? AND kind='create_thread'", job.attempt_id)[0][0])

    def test_k12_killed_after_the_reservation_restarts_with_the_same_provider_call(self) -> None:
        job = self._lose_dispatch_owner("hit:before_thread_intent")
        calls = self._rows("SELECT id,execution_status FROM provider_calls WHERE attempt_id=?", job.attempt_id)
        self.assertEqual(1, len(calls))
        self.assertEqual("reserved", calls[0][1])
        self.assertEqual([], self._effects())
        restarted = self.dispatcher.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        self._converge(self.dispatcher, self.runtime, job.attempt_id)
        self.assertEqual(["create", "start"], self._effects())
        self.assertEqual([calls[0][0]], [row[0] for row in self._rows(
            "SELECT id FROM provider_calls WHERE attempt_id=?", job.attempt_id)])

    # --- 10.6 S5: 라우팅 3·4의 과도기 한 단계(설계 8.3, 한계 고정) ------------------------------------

    def _status(self) -> dict:
        application = EngineApplication(self.service, runtime=self.runtime, supervisor=self.supervisor,
                                        governance=ALLOW_ALL)
        return _read_only_status(self, application, self.project_id)

    def test_s5_row3_status_is_automatic_pending_for_one_step_then_matches_run_once(self) -> None:
        self._lose_dispatch_owner("create")
        pending = self._status()
        self.assertEqual(("automatic_pending", "automatic", None), _status_row(pending))
        self.assertIn("기존 prepared 복구 경로로 넘김", pending["recovery"]["next_action"]["detail"])
        routed = self.dispatcher.run_once(self.project_id)
        self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"), (routed.action, routed.blocker_code))
        after = self._status()
        self.assertIsNone(after["active_runtime_job"])
        self.assertEqual(("observe_first_required", "observe_first", routed.blocker_code), _status_row(after))

    def test_s5_row4_status_is_automatic_pending_for_one_step_then_matches_run_once(self) -> None:
        self._lose_dispatch_owner("hit:after_thread_receipt")
        pending = self._status()
        self.assertEqual(("automatic_pending", "automatic", None), _status_row(pending))
        self.assertIn("기존 thread 재개 경로로 넘김", pending["recovery"]["next_action"]["detail"])
        routed = self.dispatcher.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, None), (routed.action, routed.blocker_code), routed)
        after = self._status()
        self.assertEqual(("none", "none", None), _status_row(after))
        self.assertNotEqual("cancelled", (after["active_runtime_job"] or {}).get("status"))
        self._converge(self.dispatcher, self.runtime, routed.attempt_id)


class ReplanningOwnerKillProcessTests(_OwnerKillMixin, g1b._ReplanHarness, unittest.TestCase):
    """실제 subprocess CLI owner의 REPLANNING·EXECUTION_SPEC_PREPARE job을 kill한 뒤의 라우팅(K1·K4·K5·K6·K11·SR1·AC1·6e)."""

    def _blocked_head(self) -> None:
        self._fail_contract()
        # 부모 인스턴스는 재계획 provider가 없어 새 job을 예약하지 않는다. 예약은 child owner만 한다.
        self.application.recovery_provider_available = lambda: False
        blocked = self._until_blocked()
        self.assertEqual("REPLAN_PROVIDER_REQUIRED", blocked.blocker_code, blocked)
        self.expander_calls_before = self._expander_calls()

    def _expander_calls(self) -> int:
        return [role for (role,) in self._rows(
            "SELECT role FROM provider_calls WHERE project_id=?", self.project_id)].count("plan_expander")

    def _lose_owner(self, block: str, *, runner: str | None = "scripted", work_name: str = "owner",
                    kind: RuntimeJobKind = RuntimeJobKind.REPLANNING):
        work = Path(self.temp.name) / work_name
        process = self._start_owner(work, {
            "block": block, "runner": runner, "roles": True,
            "responses": {"plan_expander": [g1b._expansion(_REPLAN_STATEMENT)],
                          RECOVERY_PLAN_REVIEWER_ROLE: [g1b._clean_review()]},
        })
        job = self.service.active_runtime_job(self.project_id)
        self.assertEqual(kind, job.kind)
        self._kill_owner(process, job)
        return job, work

    def _enable_provider(self) -> None:
        del self.application.recovery_provider_available
        self.assertTrue(self.application.recovery_provider_available())
        self._queue_replan(review=g1b._clean_review(), statement=_REPLAN_STATEMENT)

    def _converged(self, job) -> None:
        self._until_recovered_activation()
        self.assertEqual(2, self._plan(self._active_plan_id()).revision_no)
        self.assertEqual("consumed", self.service.load_runtime_job(job.job_id).status.value)

    def _backup(self, destination: Path) -> None:
        source = sqlite3.connect(self.service.ledger.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def test_k1_killed_at_inventory_restarts_once_with_the_same_inputs_as_an_undisturbed_run(self) -> None:
        self._blocked_head()
        control_db = Path(self.temp.name) / "control.sqlite3"
        self._backup(control_db)
        job, work = self._lose_owner("inventory")
        self.assertLess(utc_now(), job.absolute_deadline_at)
        self.assertEqual("collector_lost", self.application.observe(self.project_id)["runtime_job"]["status"])
        self._enable_provider()
        inputs: list[tuple[str, str]] = []
        real = RecoveryPlanProvider.replan

        def spy(provider, **kwargs):
            inputs.append((sha256_digest(kwargs["assessment"].model_dump(mode="json")),
                           sha256_digest(list(kwargs["evidence_documents"]))))
            return real(provider, **kwargs)

        with mock.patch.object(RecoveryPlanProvider, "replan", spy):
            restarted = self.application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
            self._assert_restart_recorded(job, "collector_reattached")
            self._converged(job)
            # 무간섭 대조 run: kill 전 원장 사본에서 같은 재계획을 끊김 없이 수행한다.
            control = EngineService(SQLiteEngineLedger(control_db, artifact_root=Path(self.temp.name) / "control"))
            control.initialize()
            application = EngineApplication(
                control, runtime=FakeCodexRuntime(inventory()), role_configuration=fm08._roles(),
                structured_runner=InspectionScriptedRunner({
                    "plan_expander": [g1b._expansion(_REPLAN_STATEMENT)],
                    RECOVERY_PLAN_REVIEWER_ROLE: [g1b._clean_review()],
                }),
                governance=ALLOW_ALL,
            )
            self.supervisors.append(application.supervisor)
            for _ in range(80):
                if application.run_once(self.project_id).action is RunOnceAction.RECOVERED:
                    break
                time.sleep(0.01)
        # 재시작 입력(assessment·evidence_documents)이 원래 target 입력(원장 assessment·실패 Attempt evidence)과 같다.
        assessment = RecoveryAssessment.model_validate_json(self._rows(
            "SELECT payload_json FROM recovery_assessments WHERE id=?", job.request["assessment"]["assessment_id"],
        )[0][0])
        _ids, documents = EngineDispatcher(self.service, self.runtime)._failure_evidence(job.attempt_id)
        expected = (sha256_digest(assessment.model_dump(mode="json")), sha256_digest(list(documents)))
        self.assertEqual([expected, expected], inputs)

        def digests(service: EngineService) -> tuple:
            with service.ledger.read() as connection:
                return (
                    connection.execute("SELECT request_digest FROM runtime_jobs WHERE kind='replanning'").fetchall()[0][0],
                    connection.execute("SELECT request_digest FROM provider_calls WHERE role='plan_expander' "
                                       "ORDER BY rowid DESC LIMIT 1").fetchone()[0],
                )

        self.assertEqual(digests(control), digests(self.service))
        self.assertEqual(job.request_digest, digests(self.service)[0])
        self.assertEqual(["hold:inventory"], _journal_entries(work))
        self.assertEqual(self.expander_calls_before + 1, self._expander_calls())

    def test_sr1_scheduler_run_once_alone_records_the_loss_then_restarts_then_converges(self) -> None:
        self._blocked_head()
        job, _work = self._lose_owner("inventory")
        self._enable_provider()
        first = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.OBSERVED, job.job_id), (first.action, first.runtime_job_id), first)
        self.assertIn("replanning/collector_lost", first.detail)
        second = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (second.action, second.runtime_job_id), second)
        self._converged(job)
        self._assert_restart_recorded(job, "collector_reattached")
        self.assertEqual(1, len(self._observations(job.job_id, "started")))

    def test_ac1_ext_collector_lost_head_job_without_a_provider_blocks_without_writes(self) -> None:
        self._blocked_head()
        job, _work = self._lose_owner("inventory")
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)
        blocked = self._assert_blocked_without_writes(
            lambda: self.application.run_once(self.project_id), "REPLAN_PROVIDER_REQUIRED",
        )
        self.assertEqual(REPLAN_PROVIDER_REQUIRED_DETAIL, blocked.detail)
        self._enable_provider()
        restarted = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._converged(job)

    def test_k11_scheduled_orphan_restarts_once_and_blocks_without_a_provider(self) -> None:
        self._blocked_head()
        job, work = self._lose_owner("claim")
        self.assertIs(RuntimeJobStatus.SCHEDULED, job.status)
        self.assertEqual(["hold:claim"], _journal_entries(work))
        # AC1 v4 예외: provider 확보만 빼고 재시작 조건을 갖춘 SCHEDULED 고아도 원장 무변경 BLOCKED다.
        self._assert_blocked_without_writes(
            lambda: self.application.run_once(self.project_id), "REPLAN_PROVIDER_REQUIRED",
        )
        self._enable_provider()
        restarted = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "started")
        self._converged(job)
        self.assertEqual(0, len(self._observations(job.job_id, "collector_reattached")))
        self.assertEqual(self.expander_calls_before + 1, self._expander_calls())

    # --- AC1-ext-s: provider 미구성 인스턴스의 status가 run_once와 같은 REPLAN_PROVIDER_REQUIRED(I4) ----

    def _assert_status_is_replan_provider_required(self, job) -> None:
        status = _read_only_status(self, self.application, self.project_id)
        blocked = self._assert_blocked_without_writes(
            lambda: self.application.run_once(self.project_id), "REPLAN_PROVIDER_REQUIRED",
        )
        self.assertEqual(job.job_id, status["active_runtime_job"]["job_id"])
        self.assertEqual(("user_decision_required", "user_decision", "REPLAN_PROVIDER_REQUIRED"), _status_row(status))
        detail = status["recovery"]["next_action"]["detail"]
        self.assertTrue(detail.startswith(blocked.detail.rstrip(".")), (detail, blocked.detail))
        self.assertIn("--role-config", detail)
        self.assertEqual(detail, status["next_action"])
        # CLI status에는 역할 설정이 없으므로 같은 분류로 같은 code를 보인다(원장 무변경).
        before = _ledger_copy(self.service)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, cli.main([
                "--db", str(self.service.ledger.path), "--artifacts", str(self.service.ledger.artifact_root),
                "status", "--project-id", self.project_id,
            ]))
        self.assertEqual(status["recovery"]["next_action"], json.loads(output.getvalue())["recovery"]["next_action"])
        self.assertEqual(before, _ledger_copy(self.service))

    def test_ac1_ext_s_status_of_a_collector_lost_head_job_without_a_provider(self) -> None:
        self._blocked_head()
        job, _work = self._lose_owner("inventory")
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)  # C
        self._assert_status_is_replan_provider_required(job)

    def test_ac1_ext_s_status_of_a_scheduled_orphan_head_job_without_a_provider(self) -> None:
        self._blocked_head()
        job, _work = self._lose_owner("claim")
        self.assertIs(RuntimeJobStatus.SCHEDULED, self.service.load_runtime_job(job.job_id).status)
        self._assert_status_is_replan_provider_required(job)

    def test_k5_role_job_killed_before_role_requested_releases_its_reservation_and_restarts(self) -> None:
        self._blocked_head()
        job, work = self._lose_owner("role:plan_expander")
        self.assertEqual(["hold:role:plan_expander"], _journal_entries(work))
        call_id, status = self._rows(
            "SELECT id,status FROM provider_calls WHERE project_id=? AND role='plan_expander' "
            "ORDER BY rowid DESC LIMIT 1", self.project_id)[0]
        self.assertEqual("reserved", status)
        self._enable_provider()
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)  # C
        restarted = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self.assertEqual("released", self._rows("SELECT status FROM provider_calls WHERE id=?", call_id)[0][0])
        self.assertEqual(1, self._history_count(call_id, "budget.released_before_effect"))
        self._converged(job)
        self._assert_restart_recorded(job, "collector_reattached")
        statuses = [status for (status,) in self._rows(
            "SELECT status FROM provider_calls WHERE project_id=? AND role='plan_expander' ORDER BY rowid",
            self.project_id)][-2:]
        self.assertEqual("released", statuses[0])
        self.assertNotIn(statuses[1], {"reserved", "released"})

    def test_k4_role_job_killed_after_role_requested_is_external_effect_unknown(self) -> None:
        self._blocked_head()
        job, work = self._lose_owner("create", runner=None)  # 제품 CodexStructuredRoleRunner
        self.assertEqual(["create", "hold:create"], _journal_entries(work))
        events = [item["role_progress"]["event"] for item in self._observations(job.job_id, "provider_progress")
                  if "role_progress" in item]
        self.assertEqual(["role_requested"], events)
        self._enable_provider()
        calls, creates = len(self.runner.calls), self.runtime.create_calls
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)  # C
        blocked = self._assert_blocked_without_writes(
            lambda: self.application.run_once(self.project_id), "EXTERNAL_EFFECT_UNKNOWN",
        )
        self.assertEqual(FailureClass.EXTERNAL_UNKNOWN, blocked.failure_class)
        self.assertIn("role_requested", blocked.detail)
        self.assertIn(job.job_id, blocked.detail)
        self.assertEqual((calls, creates), (len(self.runner.calls), self.runtime.create_calls))
        self.assertEqual(["create", "hold:create"], _journal_entries(work))

    def test_k6_a_restarted_job_lost_again_stops_with_the_restart_limit(self) -> None:
        self._blocked_head()
        job, first = self._lose_owner("inventory", work_name="owner-1")
        self.assertEqual("collector_lost", self.application.observe(self.project_id)["runtime_job"]["status"])
        # 두 번째 CLI owner가 run-once 라우터로 같은 job을 한 번 재시작한 뒤 다시 kill된다.
        second = Path(self.temp.name) / "owner-2"
        process = self._start_owner(second, {"block": "inventory", "runner": "scripted", "roles": True})
        self.assertTrue(_wait_until((second / "owner-result.json").exists, 30))
        published = json.loads((second / "owner-result.json").read_text(encoding="utf-8"))
        self.assertEqual(("dispatched", job.job_id), (published["action"], published["runtime_job_id"]))
        self._kill_owner(process, job)
        self._enable_provider()
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)  # C
        blocked = self._assert_blocked_without_writes(
            lambda: self.application.run_once(self.project_id), "RUNTIME_JOB_OWNER_LOST",
        )
        self.assertEqual("effect_state=none_proven; reason=restart_limit", blocked.detail)
        self.assertEqual(1, len([item for item in self._observations(job.job_id, "collector_reattached")
                                 if "restart" in item]))
        self.assertEqual((["hold:inventory"], ["hold:inventory"]), (_journal_entries(first), _journal_entries(second)))
        self.assertEqual(self.expander_calls_before, self._expander_calls())

    def test_6e_spec_prepare_without_a_proposal_provider_blocks_then_restarts_with_role_config(self) -> None:
        task_id = self._prepare()
        self._authorize()
        job, work = self._lose_owner("inventory", kind=RuntimeJobKind.EXECUTION_SPEC_PREPARE)
        self.assertIsNone(job.request["supplied_proposal_digest"])
        providerless = EngineApplication(self.service, runtime=self.runtime, governance=ALLOW_ALL)
        self.supervisors.append(providerless.supervisor)
        self.assertEqual(RunOnceAction.OBSERVED, providerless.run_once(self.project_id).action)  # C
        blocked = self._assert_blocked_without_writes(
            lambda: providerless.run_once(self.project_id), "EXECUTION_SPEC_PROPOSAL_REQUIRED",
        )
        self.assertIn("--role-config", blocked.detail)
        self._queue_execution_preparation(task_id)
        restarted = self.application.run_once(self.project_id)
        self.assertEqual((RunOnceAction.DISPATCHED, job.job_id), (restarted.action, restarted.runtime_job_id))
        self._assert_restart_recorded(job, "collector_reattached")
        self._run_until(RunOnceAction.MATERIALIZED)
        self.assertEqual(["hold:inventory"], _journal_entries(work))


class RoleEffectFenceTests(g1b._ReplanHarness, unittest.TestCase):
    """역할 효과 직전 fence F2와 role_requested 불변식(RR). 제품 CodexStructuredRoleRunner를 활성 job 안에서 부른다."""

    def _replanning_job_with_the_product_runner(self, *, hook: str | None = None, action=None,
                                                before=None) -> str:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        self.application._structured_runner = CodexStructuredRoleRunner(
            self.runtime, max_schema_recovery_attempts=0, ephemeral_threads=False,
        )
        if before is not None:
            before()
        # 앞 단계(실패한 Worker)의 효과를 빼고 이 job의 역할 효과만 센다.
        self.effects_before = (self.runtime.create_calls, self.runtime.turn_calls)
        supervisor = self.application.supervisor
        if hook is not None:
            real = supervisor.record_role_progress
            entered, gate = threading.Event(), threading.Event()
            self.addCleanup(gate.set)

            def gated(job_id, event):
                if event.get("event") == hook:
                    entered.set()
                    gate.wait(10)
                return real(job_id, event)

            supervisor.record_role_progress = gated
        scheduled = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        job_id = scheduled.runtime_job_id
        if hook is not None:
            self.assertTrue(entered.wait(10))
            action(job_id)
            gate.set()
        supervisor._workers[job_id].join(10)
        return job_id

    def _progress_events(self, job_id: str) -> list[str]:
        return [payload["role_progress"]["event"] for payload in (json.loads(raw) for (raw,) in self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='provider_progress' "
            "ORDER BY rowid", job_id)) if "role_progress" in payload]

    def _failure_error(self, job_id: str) -> str:
        payloads = [json.loads(raw) for (raw,) in self._rows(
            "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='provider_progress'", job_id)]
        return next(item["error"]["error"] for item in payloads if "target_failure_checkpoint_version" in item)

    def test_rr_role_requested_is_durable_before_create_thread(self) -> None:
        seen: list = []

        def create_thread(**_kwargs):
            job_id = active_runtime_job_id()
            # 역할 scope 안에서는 원장 handle을 쓸 수 없으므로 DB 파일을 읽기 전용으로 직접 본다.
            connection = sqlite3.connect(Path(self.service.ledger.path).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT payload_json FROM runtime_job_observations WHERE job_id=? AND kind='provider_progress' "
                    "ORDER BY rowid", (job_id,)).fetchall()
            finally:
                connection.close()
            seen.append((job_id, [json.loads(raw)["role_progress"]["event"] for (raw,) in rows
                                  if "role_progress" in json.loads(raw)]))
            raise RuntimeError("RR probe: create_thread 호출 시점만 관측한다")

        job_id = self._replanning_job_with_the_product_runner(
            before=lambda: setattr(self.runtime, "create_thread", create_thread),
        )
        self.assertEqual([(job_id, ["role_requested"])], seen)

    def test_f2_cancel_just_before_role_requested_starts_no_create_and_releases_the_reservation(self) -> None:
        job_id = self._replanning_job_with_the_product_runner(
            hook="role_requested",
            action=lambda job_id: self.service.cancel_runtime_job(job_id, reason="F2 role_requested 직전 cancel"),
        )
        self.assertEqual(self.effects_before, (self.runtime.create_calls, self.runtime.turn_calls))
        self.assertEqual([], self._progress_events(job_id))
        self.assertIn("RUNTIME_JOB_NOT_RUNNING", self._failure_error(job_id))
        call_id, status = self._rows(
            "SELECT id,status FROM provider_calls WHERE project_id=? AND role='plan_expander' "
            "ORDER BY rowid DESC LIMIT 1", self.project_id)[0]
        # effects_started=False였으므로 BudgetedRoleRunner가 예약을 효과 전 해제로 닫았다.
        self.assertEqual("released", status)
        self.assertEqual(1, self._rows(
            "SELECT COUNT(*) FROM history_events WHERE entity_id=? AND event_type='budget.released_before_effect'",
            call_id)[0][0])

    def test_g4_refused_thread_created_leaves_the_thread_and_an_unsettled_reservation_as_unknown(self) -> None:
        job_id = self._replanning_job_with_the_product_runner(
            hook="thread_created",
            action=lambda job_id: self.application.supervisor.mark_collector_lost(job_id, reason="G4 소실 흉내"),
        )
        # thread_created는 roles.py try 밖이라 일반 예외다. thread는 생겼고 start는 시작되지 않았다.
        creates, turns = self.effects_before
        self.assertEqual((creates + 1, turns), (self.runtime.create_calls, self.runtime.turn_calls))
        self.assertEqual(["role_requested"], self._progress_events(job_id))
        self.assertIn("RUNTIME_JOB_NOT_RUNNING", self._failure_error(job_id))
        self.assertEqual([("reserved", "reserved", None)], [row for row in self._rows(
            "SELECT status,execution_status,receipt_json FROM provider_calls WHERE project_id=? "
            "AND role='plan_expander' ORDER BY rowid DESC LIMIT 1", self.project_id)])
        self.assertEqual(RunOnceAction.OBSERVED, self.application.run_once(self.project_id).action)  # owner tick
        before = _ledger_copy(self.service)
        for _ in range(3):
            blocked = self.application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.BLOCKED, "EXTERNAL_EFFECT_UNKNOWN"),
                             (blocked.action, blocked.blocker_code), blocked)
        self.assertEqual(before, _ledger_copy(self.service))


class OwnerStatusReplanTests(g1b._ReplanHarness, unittest.TestCase):
    """status 정합(10.6 S2·S6, I4): 실패가 기록된 REPLANNING head job을 fm08 facade harness로 본다."""

    def _until_collector_lost(self):
        outcome = None
        for _ in range(20):
            outcome = self.application.run_once(self.project_id)
            if "replanning/collector_lost" in (outcome.detail or ""):
                break
            time.sleep(0.01)
        self.assertIn("replanning/collector_lost", outcome.detail)
        return outcome

    def _assert_fail_stop_status(self, job_id: str) -> dict:
        """fail-stop head job: status는 automatic_pending이 아니고 run_once와 같은 typed blocker를 보인다."""

        status = _read_only_status(self, self.application, self.project_id)
        blocked = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, blocked.action, blocked)
        self.assertEqual((job_id, "collector_lost", None), tuple(
            status["active_runtime_job"][key] for key in ("job_id", "status", "thread_id")))
        state, mode, code = _status_row(status)
        self.assertNotEqual("automatic_pending", state)
        self.assertEqual(blocked.blocker_code, code)
        self.assertTrue(status["recovery"]["next_action"]["detail"].startswith(blocked.detail.rstrip(".")))
        self.assertEqual(status["recovery"]["next_action"]["detail"], status["next_action"])
        return status

    def test_s2_live_owner_of_a_replanning_job_with_a_failure_is_not_automatic_pending(self) -> None:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        self._queue_replan(review=g1b._clean_review(), statement=_REPLAN_STATEMENT)
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        real_run = self.runner.run

        def gated(request, *, validator=None):
            if request.role == "plan_expander":
                entered.set()
                gate.wait(10)
            return real_run(request, validator=validator)

        self.runner.run = gated
        scheduled = self.application.run_once(self.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, scheduled.action, scheduled)
        self.assertTrue(entered.wait(10))
        other = EngineApplication(self.service, runtime=self.runtime, governance=ALLOW_ALL)
        self.supervisors.append(other.supervisor)
        # 같은 supervisor(판정 1)와 다른 supervisor(판정 2) 모두 owner 실행 중이다(J1 (A)).
        for application in (self.application, other):
            status = _read_only_status(self, application, self.project_id)
            self.assertEqual(("none", "none", None), _status_row(status))
            self.assertEqual("owner 실행 중: 다음 run-once·observe는 관측만 합니다.",
                             status["recovery"]["next_action"]["detail"])
            self.assertEqual(status["recovery"]["next_action"]["detail"], status["next_action"])
            # failure가 함께 있으므로 분류·한도는 원장 사실대로 채운다.
            self.assertEqual("task_contract", status["recovery"]["classification"]["failure_class"])
            self.assertIsNotNone(status["recovery"]["limits"])
            self.assertEqual("replanning", status["current_stage"])
            observed = application.run_once(self.project_id)
            self.assertEqual((RunOnceAction.OBSERVED, None), (observed.action, observed.blocker_code), observed)
            self.assertIn("replanning/running", observed.detail)
        gate.set()
        self.application.supervisor._workers[scheduled.runtime_job_id].join(10)
        self._until_recovered_activation()

    def test_s4_posix_status_is_unchanged_before_activation_and_blocks_after_it(self) -> None:
        """AC19: 활성 Plan이 없으면 POSIX status도 그대로다. 활성화 뒤에는 run_once와 같은 blocker다."""

        def view(status: dict) -> tuple:
            return status["current_stage"], status["reason"], status["next_action"], status["recovery"]

        self._prepare()
        windows = _read_only_status(self, self.application, self.project_id)
        with mock.patch.object(runtime_module, "owner_lock_platform_supported", lambda: False):
            posix = _read_only_status(self, self.application, self.project_id)
        self.assertEqual(view(windows), view(posix))
        self._authorize()
        with mock.patch.object(runtime_module, "owner_lock_platform_supported", lambda: False):
            status = _read_only_status(self, self.application, self.project_id)
            blocked = self.application.run_once(self.project_id)
        self.assertEqual(("user_decision_required", "user_decision", blocked.blocker_code), _status_row(status))
        self.assertEqual("RUNTIME_OWNER_LOCK_UNAVAILABLE", blocked.blocker_code)
        self.assertTrue(status["next_action"].startswith(blocked.detail))
        self.assertIn("Windows 전용", status["next_action"])

    def test_s6_collector_lost_stable_head_job_status_and_replan_say_the_same_durable_state(self) -> None:
        self._fail_contract()
        self._run_until(RunOnceAction.RECOVERED)
        # scripted 응답이 없어 역할이 효과 근거 없이 실패하고, 결과·binding 없는 collector_lost가 된다.
        job_id = self._until_collector_lost().runtime_job_id
        message = self._assert_rejected("REPLAN_RETRY_NOT_BLOCKED")
        self.assertIn(f"replanning job {job_id}의 collector가 결과·provider binding 없이 끊겼습니다", message)
        self.assertIn("다음 run-once가 owner lock으로 생존을 확인해 한 번 재시작하거나 typed blocker로 멈춥니다", message)
        self.assertNotIn("관측만 반복", message)
        self.assertNotIn(REPLAN_JOB_ERROR_NO_PUBLIC_ESCAPE, message)
        status = self._assert_fail_stop_status(job_id)
        self.assertEqual(("user_decision_required", "user_decision", "RUNTIME_EFFECT_PREFLIGHT_FAILED"),
                         _status_row(status))

    def test_s6_g2_collector_lost_retry_head_job_is_not_automatic_pending_on_fail_stop(self) -> None:
        """T r3 G2: 재시도 head job이 collector_lost로 멈추면 replan은 기록 없이 status를 가리킨다."""

        self._blocked_needs_revision()
        recorded = self._replan()
        self.assertTrue(recorded["recorded"], recorded)
        # 재시도 job의 역할 응답이 없어 효과 근거 없이 실패하고 collector_lost가 된다.
        job_id = self._until_collector_lost().runtime_job_id
        before = self._ledger()
        again = self._replan()
        self.assertEqual((False, recorded["assessment_id"]), (again["recorded"], again["assessment_id"]))
        self.assertIn("status로 진행을 확인", again["next"])
        self.assertEqual(before, self._ledger())
        status = self._assert_fail_stop_status(job_id)
        self.assertEqual(f"replanning:{recorded['assessment_id']}", status["active_runtime_job"]["checkpoint_key"])


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
