"""D3 source candidate. Execute only after this packet's SOURCE/PRE."""
import argparse
import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from types import FunctionType
from pathlib import Path

PACKET = Path(__file__).resolve().parent
MODULE = "test_flowmarshal_direct_selection"
FIXTURE_MODULE = "test_flowmarshal_implementation_workflow"
SOURCE_PATHS = (
    "pristine/tests/test_flowmarshal_direct_selection.py",
    "pristine/tests/test_flowmarshal_implementation_workflow.py",
    "pristine/scripts/flowmarshal_implementation_workflow.py",
    "pristine/scripts/flowmarshal_orchestrator_ledger.py",
)


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def inputs(args):
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Linux Python 3.12 required; no fallback/install")
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("Invoke with -I -S -B")
    if sha(PACKET / "runner.py") != args.runner_sha256:
        raise RuntimeError("runner pin mismatch")
    manifest_path = PACKET / "manifest.json"
    if sha(manifest_path) != args.manifest_sha256:
        raise RuntimeError("manifest pin mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest["schema"] != "vm-after-c5-direct-neg3-source-r01"
            or manifest["test_ids"] != args.expected_ids):
        raise RuntimeError("manifest contract mismatch")
    if (manifest["runner"]["sha256"] != args.runner_sha256
            or manifest["runner"]["bytes"] != (PACKET / "runner.py").stat().st_size):
        raise RuntimeError("manifest runner binding mismatch")
    if set(manifest["sources"]) != set(SOURCE_PATHS):
        raise RuntimeError("source inventory mismatch")
    observed_files = set()
    for path in (PACKET / "pristine").rglob("*"):
        if path.is_symlink():
            raise RuntimeError("pristine symlink")
        if path.is_file():
            observed_files.add(path.relative_to(PACKET).as_posix())
    if observed_files != set(SOURCE_PATHS):
        raise RuntimeError("extra/missing pristine files")
    snapshot = {"runner.py": sha(PACKET / "runner.py"),
                "manifest.json": sha(manifest_path)}
    for name in SOURCE_PATHS:
        path = PACKET / name
        expected = manifest["sources"][name]
        if path.resolve() != path or path.stat().st_size != expected["bytes"]:
            raise RuntimeError("source path/size mismatch: " + name)
        snapshot[name] = sha(path)
        if snapshot[name] != expected["sha256"]:
            raise RuntimeError("source hash mismatch: " + name)
    return manifest, snapshot


def flattened(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flattened(item)
        else:
            yield item


def closure():
    files = {}
    for name, module in list(sys.modules.items()):
        if name not in ("__main__", MODULE, FIXTURE_MODULE) and name.split(".")[0] not in sys.stdlib_module_names:
            raise RuntimeError("non-stdlib module: " + name)
        path = getattr(module, "__file__", None)
        if path:
            if any(part in ("site-packages", "dist-packages") for part in Path(path).parts):
                raise RuntimeError("package origin: " + path)
            files[name] = str(Path(path).resolve())
    return files



def helper_origin_observation(helper, name, expected_path, function):
    wrapped = getattr(function, "__wrapped__", None)
    exposed_code = getattr(function, "__code__", None)
    wrapped_code = getattr(wrapped, "__code__", None)
    return {"name": name, "expected_source": str(expected_path),
            "exposed_type": type(function).__name__, "exposed_object_id": id(function),
            "exposed_co_filename": getattr(exposed_code, "co_filename", None),
            "exposed_co_name": getattr(exposed_code, "co_name", None),
            "wrapped_type": type(wrapped).__name__ if wrapped is not None else None,
            "wrapped_object_id": id(wrapped) if wrapped is not None else None,
            "wrapped_co_filename": getattr(wrapped_code, "co_filename", None),
            "wrapped_co_name": getattr(wrapped_code, "co_name", None),
            "exposed_globals_bound": getattr(function, "__globals__", None) is helper.__dict__,
            "wrapped_globals_bound": getattr(wrapped, "__globals__", None) is helper.__dict__,
            "imported_contextmanager_is_stdlib": getattr(helper, "contextmanager", None) is contextmanager}


def record_helper_origin(stream, phase, observation):
    stream.write(json.dumps({"utc": utc(), "phase": phase, **observation}, ensure_ascii=False) + "\n")
    stream.flush()


def checked_helper_origin(helper, name, expected_path, function):
    target = function
    decorator = None
    if not isinstance(function, FunctionType):
        raise RuntimeError("helper origin requires function: " + name)
    if name == "open_readonly":
        wrapped = getattr(function, "__wrapped__", None)
        if (helper.contextmanager is not contextmanager
                or not isinstance(wrapped, FunctionType)
                or hasattr(wrapped, "__wrapped__")
                or not inspect.isgeneratorfunction(wrapped)):
            raise RuntimeError("helper contextmanager binding mismatch: " + name)
        reference = contextmanager(wrapped)  # Creates a wrapper; never calls the generator.
        actual_cells, expected_cells = function.__closure__ or (), reference.__closure__ or ()
        if (function.__code__ is not reference.__code__
                or function.__globals__ is not reference.__globals__
                or function.__defaults__ != reference.__defaults__
                or function.__kwdefaults__ != reference.__kwdefaults__
                or len(actual_cells) != len(expected_cells)
                or any(actual.cell_contents is not expected.cell_contents
                       for actual, expected in zip(actual_cells, expected_cells))):
            raise RuntimeError(
                f"helper contextmanager wrapper mismatch: {name}; "
                f"exposed={function.__code__.co_filename!r}; wrapped={wrapped.__code__.co_filename!r}"
            )
        target = inspect.unwrap(function)
        if target is not wrapped:
            raise RuntimeError("helper contextmanager unwrap mismatch: " + name)
        decorator = "contextlib.contextmanager"
    if (target.__globals__ is not helper.__dict__ or target.__name__ != name
            or target.__code__.co_name != name
            or Path(target.__code__.co_filename).resolve() != Path(expected_path)):
        raise RuntimeError(
            f"helper code origin mismatch: {name}; "
            f"exposed={function.__code__.co_filename!r}; original={target.__code__.co_filename!r}; "
            f"expected={str(expected_path)!r}"
        )
    return {"name": name, "decorator": decorator,
            "exposed_co_filename": function.__code__.co_filename,
            "original_co_filename": target.__code__.co_filename,
            "original_globals_bound": True}



def effect_denial_counts(state):
    if state is None:
        return None
    return {"count": state["count"], "logging_errors": state["logging_errors"]}


def observe_effect_denials(root, expected_pid, claimed):
    """Missing or inconsistent evidence cannot establish zero denials."""
    observed = {"state": "UNKNOWN", "count": None, "journal_count": None,
                "logging_errors": None, "bytes": None, "sha256": None}
    try:
        path = root / "effect-denials.jsonl"
        if path.is_symlink() or not path.is_file():
            raise ValueError("required denial journal missing or nonregular")
        data = path.read_bytes()
        observed.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        if (not isinstance(claimed, dict) or set(claimed) != {"count", "logging_errors"}
                or any(type(claimed[name]) is not int or claimed[name] < 0
                       for name in ("count", "logging_errors"))):
            raise ValueError("invalid child denial counters")
        observed.update(count=claimed["count"], logging_errors=claimed["logging_errors"])
        if data and not data.endswith(b"\n"):
            raise ValueError("partial denial journal row")
        rows = [json.loads(line) for line in data.decode("utf-8").splitlines()]
        for sequence, row in enumerate(rows, 1):
            if (not isinstance(row, dict)
                    or set(row) != {"sequence", "pid", "utc", "event", "reason"}
                    or type(row["sequence"]) is not int or row["sequence"] != sequence
                    or type(row["pid"]) is not int or row["pid"] != expected_pid
                    or not isinstance(row["event"], str) or not row["event"]
                    or row["reason"] not in (
                        "prohibited_effect", "nonstdlib_import", "unsupported_sqlite_uri",
                        "noncanonical_sqlite_uri", "sqlite_outside_private_tmp")
                    or not isinstance(row["utc"], str)
                    or datetime.fromisoformat(row["utc"]).utcoffset() is None):
                raise ValueError("invalid denial journal row")
        observed["journal_count"] = len(rows)
        if len(rows) != claimed["count"]:
            raise ValueError("denial journal counter mismatch")
        if claimed["logging_errors"]:
            raise ValueError("child denial journal I/O error")
        observed["state"] = "DENIED" if rows else "CLEAR"
    except Exception as error:
        # Preserve uncertainty; no target arguments or error-string fallback.
        observed["error_type"] = type(error).__name__
    return observed


def checked_test_module_binding(module):
    fixture = module.fixture
    for value, name, path in (
            (module, MODULE, PACKET / SOURCE_PATHS[0]),
            (fixture, FIXTURE_MODULE, PACKET / SOURCE_PATHS[1])):
        if (sys.modules.get(name) is not value or value.__name__ != name
                or value.__spec__.name != name or value.__package__ != ""
                or Path(value.__file__).resolve() != path):
            raise RuntimeError("natural test/fixture module identity mismatch")
    aliases = ("impl", "ns", "connect", "digest")
    if any(getattr(module, name) is not getattr(fixture, name) for name in aliases):
        raise RuntimeError("D fixture aliases must bind the same original M objects")
    observed = []
    for owner, names, expected, globals_owner in (
            (module.DirectSelectionTest, ("setUp", "selection", "ref", "record", "counts"),
             PACKET / SOURCE_PATHS[0], module),
            (fixture, ("ns", "connect", "digest"), PACKET / SOURCE_PATHS[1], fixture),
            (fixture.ImplementationWorkflowTest,
             ("setUp", "tearDown", "_make_v1", "_make_codex_state", "_manifest", "_task",
              "_write_manifest", "migrate", "begin", "evidence", "pass_fm00", "_record_direct"),
             PACKET / SOURCE_PATHS[1], fixture)):
        for name in names:
            function = getattr(owner, name)
            if (not isinstance(function, FunctionType)
                    or Path(function.__code__.co_filename).resolve() != expected
                    or function.__globals__ is not globals_owner.__dict__):
                raise RuntimeError("test/fixture function file or globals mismatch")
            observed.append({"owner": owner.__name__, "name": name,
                             "code_file": function.__code__.co_filename,
                             "globals_module": globals_owner.__name__})
    return {"test_module": MODULE, "fixture_module": FIXTURE_MODULE,
            "fixture_aliases_same_objects": list(aliases), "functions": observed}


def child(args, root):
    manifest, before = inputs(args)
    if time.monotonic() >= args.deadline:
        raise RuntimeError("deadline before loader")
    tmp = root / "tmp"
    if (not root.is_absolute() or root.resolve() != root or root.is_relative_to(PACKET)
            or Path.cwd() != root / "cwd" or list((root / "cwd").iterdir())
            or any((root / name).is_symlink() for name in ("home", "tmp", "cwd"))
            or any(os.environ.get(name) != str(tmp) for name in ("TMPDIR", "TMP", "TEMP"))
            or os.environ.get("HOME") != str(root / "home")):
        raise RuntimeError("child cwd/HOME/TMP binding mismatch")
    if Path(tempfile.gettempdir()).resolve() != tmp:
        raise RuntimeError("stdlib tempfile fell outside private tmp")

    denial_state = {"count": 0, "logging_errors": 0, "logging_active": False}
    args.effect_denial_state = denial_state
    try:
        denial_stream = (root / "effect-denials.jsonl").open("x", encoding="utf-8", newline="\n")
        args.effect_denial_stream = denial_stream
        denial_stream.flush()  # The required empty file represents normal zero denials.
    except Exception as error:
        denial_state["logging_errors"] += 1
        raise RuntimeError("C5 denial journal initialization failed") from error

    def deny(event, reason, message):
        # A test catching the exception cannot erase this independent observation.
        denial_state["count"] += 1
        if denial_state["logging_active"]:
            denial_state["logging_errors"] += 1
            raise RuntimeError(message)  # Prevent recursive logging; evidence is UNKNOWN.
        denial_state["logging_active"] = True
        try:
            row = {"sequence": denial_state["count"], "pid": os.getpid(), "utc": utc(),
                   "event": event, "reason": reason}
            denial_stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            denial_stream.flush()
        except Exception:
            denial_state["logging_errors"] += 1
        finally:
            denial_state["logging_active"] = False
        raise RuntimeError(message)  # Reject before the operation even when logging failed.

    def audit(event, values):
        # This checks the selected Python seams; it is not an OS sandbox.
        if (event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
                or event in ("os.system", "os.posix_spawn", "ctypes.dlopen", "os.chdir")):
            deny(event, "prohibited_effect", "C5 prohibited effect: " + event)
        if event == "import":
            name = values[0]
            if name not in (MODULE, FIXTURE_MODULE) and name.split(".")[0] not in sys.stdlib_module_names:
                deny(event, "nonstdlib_import", "C5 non-stdlib import: " + name)
        if event == "sqlite3.connect":
            target = os.fspath(values[0])
            if isinstance(target, str) and target.startswith("file:"):
                # Original open_readonly uses exactly file:{absolute}?mode=ro.
                if (not target.startswith("file:/") or target.startswith("file://")
                        or not target.endswith("?mode=ro") or target.count("?") != 1
                        or any(char in target for char in ("%", "#", "\x00"))):
                    deny(event, "unsupported_sqlite_uri", "C5 unsupported SQLite URI")
                target = target[5:-8]
                if Path(target).resolve().as_posix() != target:
                    deny(event, "noncanonical_sqlite_uri", "C5 noncanonical SQLite URI path")
            path = Path(target)
            if not path.is_absolute() or not path.resolve().is_relative_to(tmp):
                deny(event, "sqlite_outside_private_tmp", "C5 SQLite outside private tmp")

    sys.addaudithook(audit)
    sys.path.insert(0, str(PACKET / "pristine" / "tests"))
    module = __import__(MODULE)
    full_ids = manifest["test_ids"]
    if (full_ids != args.expected_ids or len(full_ids) != 3 or len(set(full_ids)) != 3
            or any(not identity.startswith(MODULE + ".") for identity in full_ids)):
        raise RuntimeError("exact original IDs/module prefix mismatch")
    names = [identity[len(MODULE) + 1:] for identity in full_ids]
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names, module=module)
    cases = list(flattened(suite))
    ids = [case.id() for case in cases]
    save(root / "loader.json", {"utc": utc(), "ids": ids, "count": len(cases),
                                "errors": loader.errors})
    if loader.errors or len(cases) != 3 or ids != manifest["test_ids"]:
        raise RuntimeError("exact loader IDs/count/errors mismatch: " + repr(loader.errors))
    origins = {
        "test": str(Path(module.__file__).resolve()),
        "fixture": str(Path(module.fixture.__file__).resolve()),
        "helper": str(Path(module.impl.__file__).resolve()),
        "legacy_import_only": str(Path(module.fixture.legacy.__file__).resolve()),
    }
    if list(origins.values()) != [str(PACKET / name) for name in SOURCE_PATHS]:
        raise RuntimeError("source origins mismatch: " + repr(origins))
    test_module_binding = checked_test_module_binding(module)
    for case in cases:
        method = getattr(case, case._testMethodName)
        if (Path(method.__func__.__code__.co_filename).resolve() != Path(origins["test"])
                or method.__func__.__globals__ is not module.__dict__):
            raise RuntimeError("test code origin mismatch")
    helper_code_origins = []
    with (root / "helper-origin.jsonl").open("x", encoding="utf-8", newline="\n") as origin_stream:
        for name in ("command_migrate", "command_begin", "command_verify", "open_readonly",
                     "open_write", "_backup_database", "_install_writer_fence_triggers",
                     "command_status", "command_task", "command_register_revision", "command_review",
                     "command_finish", "command_revalidate_success", "command_record_direct_attempt",
                     "command_ready_tasks", "_load_evidence", "_load_direct_selection"):
            function = getattr(module.impl, name)
            observation = helper_origin_observation(module.impl, name, origins["helper"], function)
            record_helper_origin(origin_stream, "before", observation)
            try:
                verified = checked_helper_origin(module.impl, name, origins["helper"], function)
            except Exception as error:
                try:
                    record_helper_origin(origin_stream, "rejected", {
                        **observation, "error_type": type(error).__name__, "error": str(error),
                    })
                except Exception:
                    print(traceback.format_exc(), file=sys.stderr)
                raise
            record_helper_origin(origin_stream, "accepted", {**observation, **verified})
            helper_code_origins.append(verified)
    save(root / "preflight.json", {
        "utc": utc(), "argv": sys.argv, "cwd": str(Path.cwd()), "pid": os.getpid(),
        "python": sys.version, "executable": sys.executable, "sqlite": sqlite3.sqlite_version,
        "loader_ids": ids, "loader_count": len(cases), "loader_errors": loader.errors,
        "origins": origins, "helper_code_origins": helper_code_origins,
        "test_module_binding": test_module_binding,
        "module_files": closure(), "inputs_before": before,
    })
    if time.monotonic() >= args.deadline:
        raise RuntimeError("deadline before tests")
    started = utc()
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
    data = {
        "started_utc": started, "finished_utc": utc(), "test_ids": ids,
        "testsRun": result.testsRun, "wasSuccessful": result.wasSuccessful(),
        "failures": [(case.id(), text) for case, text in result.failures],
        "errors": [(case.id(), text) for case, text in result.errors],
        "skipped": [(case.id(), reason) for case, reason in result.skipped],
        "expectedFailures": [(case.id(), text) for case, text in result.expectedFailures],
        "unexpectedSuccesses": [case.id() for case in result.unexpectedSuccesses],
    }
    ok = (data["testsRun"] == 3 and data["wasSuccessful"]
          and not any(data[name] for name in
                      ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")))
    data["unittest_exit"] = 0 if ok else 1
    data["effect_denials"] = effect_denial_counts(denial_state)
    data["suite_exit"] = (2 if denial_state["logging_errors"] else
                          (1 if denial_state["count"] else data["unittest_exit"]))
    # Preserve original unittest fields and counters before additional observations.
    save(root / "result.json", data)
    try:
        denial_stream.flush()
        denial_stream.close()
    except Exception:
        denial_state["logging_errors"] += 1
    denial_observation = observe_effect_denials(
        root, os.getpid(), effect_denial_counts(denial_state))
    _, after = inputs(args)
    save(root / "postflight.json", {
        "utc": utc(), "inputs_after": after, "matches_before": before == after,
        "module_files_after": closure(),
        "effect_denials": effect_denial_counts(denial_state),
        "effect_denial_observation": denial_observation,
    })
    if before != after or denial_observation["state"] == "UNKNOWN":
        return 2
    return 1 if denial_observation["state"] == "DENIED" else data["unittest_exit"]



def residual_files(root):
    rows = []
    try:
        for path in root.rglob("*"):
            try:
                if path.is_file():
                    rows.append({"path": path.relative_to(root).as_posix(),
                                 "bytes": path.stat().st_size})
            except OSError as error:
                rows.append({"path": path.relative_to(root).as_posix(),
                             "state": "UNKNOWN", "error": repr(error)})
    except OSError as error:
        rows.append({"enumeration": "UNKNOWN", "error": repr(error)})
    return rows


def supervise(args):
    manifest, before = inputs(args)
    root = Path(args.run_root)
    if (not root.is_absolute() or root.resolve() != root or root.exists()
            or root.is_symlink() or root.is_relative_to(PACKET)
            or not root.parent.is_dir()):
        raise RuntimeError("run-root must be a fresh absolute directory outside packet")
    deadline = time.monotonic() + args.timeout
    root.mkdir()
    for name in ("home", "tmp", "cwd"):
        (root / name).mkdir()
    env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
           "TMP": str(root / "tmp"), "TEMP": str(root / "tmp"), "PATH": os.defpath}
    argv = [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(PACKET / "runner.py"),
            "--child", "--run-root", str(root), "--runner-sha256", args.runner_sha256,
            "--manifest-sha256", args.manifest_sha256, "--deadline", str(deadline)]
    save(root / "invocation.json", {
        "utc": utc(), "parent_argv": sys.argv, "parent_cwd": str(Path.cwd()),
        "child_argv": argv, "child_cwd": str(root / "cwd"), "child_env": env,
        "deadline_monotonic": deadline, "timeout_seconds": args.timeout,
        "inputs_before": before, "scope": "D3 only; own child timeout kill/reap, no retry/global kill/cleanup",
    })
    if time.monotonic() >= deadline:
        raise RuntimeError("deadline before only child")
    with (root / "stdout.txt").open("xb") as out, (root / "stderr.txt").open("xb") as err:
        process = subprocess.Popen(argv, cwd=root / "cwd", env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        started = utc()
        timed_out, kill_error = False, None
        # Reserve part of the same <=300s budget for this owned child's reap.
        reap_reserve = min(5.0, args.timeout / 10.0)
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic() - reap_reserve))
            terminal = True
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                process.kill()  # Only this Popen's child; no process search/group kill.
            except OSError as error:
                kill_error = repr(error)
            try:
                code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                terminal = True
            except subprocess.TimeoutExpired:
                code, terminal = None, False
    try:
        _, after = inputs(args)
    except Exception:
        after = {"input_validation_error": traceback.format_exc()}
    raw, raw_error = None, None
    raw_path = root / "result.json"
    if terminal:
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raw_error = repr(error)  # Keep missing/partial raw bytes; never repair them.
    denial_observation = {"state": "UNKNOWN", "count": None, "journal_count": None,
                          "logging_errors": None, "bytes": None, "sha256": None}
    denial_claim_source = None
    if terminal:
        try:
            if isinstance(raw, dict):
                denial_claim_source = "result.json"
                claimed = raw["effect_denials"]
                denial_observation = observe_effect_denials(root, process.pid, claimed)
                post = json.loads((root / "postflight.json").read_text(encoding="utf-8"))
                if (post["effect_denials"] != claimed
                        or post["effect_denial_observation"] != denial_observation):
                    raise ValueError("child and parent denial observations mismatch")
                original_ok = (raw.get("test_ids") == manifest["test_ids"]
                               and raw.get("testsRun") == 3 and raw.get("wasSuccessful") is True
                               and all(raw.get(name) == [] for name in
                                       ("failures", "errors", "skipped", "expectedFailures",
                                        "unexpectedSuccesses")))
                if type(raw.get("unittest_exit")) is not int or raw["unittest_exit"] != (0 if original_ok else 1):
                    raise ValueError("original unittest result mismatch")
                expected_exit = (2 if claimed["logging_errors"] else
                                 (1 if claimed["count"] else raw["unittest_exit"]))
                if type(raw.get("suite_exit")) is not int or raw["suite_exit"] != expected_exit:
                    raise ValueError("child denial outcome mismatch")
            elif not raw_path.exists():
                # Pre-unittest uncaught refusal has no fabricated unittest result.
                error = json.loads((root / "child-error.json").read_text(encoding="utf-8"))
                if error["pid"] != process.pid or error["status"] != "ERROR":
                    raise ValueError("child error binding mismatch")
                denial_claim_source = "child-error.json"
                denial_observation = observe_effect_denials(
                    root, process.pid, error["effect_denials"])
        except Exception as error:
            denial_observation.update(state="UNKNOWN", protocol_error_type=type(error).__name__)
    residual = residual_files(root)
    unittest_observation_unknown = raw_error is not None or not isinstance(raw, dict)
    uncaught_denial = (denial_claim_source == "child-error.json"
                      and denial_observation["state"] == "DENIED")
    observation_unknown = (
        not terminal or before != after or any("error" in row for row in residual)
        or denial_observation["state"] == "UNKNOWN"
        or (unittest_observation_unknown and not uncaught_denial))
    raw_ok = (isinstance(raw, dict) and raw.get("suite_exit") == 0
              and raw.get("test_ids") == manifest["test_ids"] and raw.get("testsRun") == 3
              and raw.get("wasSuccessful") is True
              and all(raw.get(name) == [] for name in
                      ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")))
    if observation_unknown:
        status = "UNKNOWN"
    elif timed_out:
        status = "TIMEOUT"
    elif denial_observation["state"] == "DENIED":
        status = "FAIL"
    else:
        status = "PASS" if code == 0 and raw_ok and denial_observation["state"] == "CLEAR" else "FAIL"
    save(root / "execution.json", {
        "started_utc": started, "observed_utc": utc(), "pid": process.pid,
        "actual_exit": code, "terminal_observed": terminal, "status": status,
        "timed_out": timed_out, "own_child_kill_error": kill_error,
        "raw_observation_error": raw_error, "observation_unknown": observation_unknown,
        "unittest_observation_unknown": unittest_observation_unknown,
        "effect_denial_claim_source": denial_claim_source,
        "effect_denial_observation": denial_observation,
        "inputs_before": before, "inputs_after_observation": after,
        "files_observed": residual, "residual_observation_mutable": not terminal,
        "timeout_note": "Own child only kill/reap in same budget; no retry/global kill/cleanup",
    })
    print(json.dumps({"status": status, "run_root": str(root), "actual_exit": code}))
    return 124 if timed_out else (0 if status == "PASS" else (2 if status == "UNKNOWN" else 1))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--runner-sha256", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args()
    args.expected_ids = [
        MODULE + '.DirectSelectionTest.test_changed_identity_or_schema_rejects_without_rows',
        MODULE + '.DirectSelectionTest.test_selection_refs_and_required_check_cannot_be_forged',
        MODULE + '.DirectSelectionTest.test_stale_success_and_evidence_tamper_reject',
    ]
    if not (0 < args.timeout <= 300):
        parser.error("timeout must be >0 and <=300")
    if args.child and (args.deadline is None or not 0 < args.deadline < float("inf")):
        parser.error("child requires finite positive deadline")
    try:
        return child(args, Path(args.run_root)) if args.child else supervise(args)
    except Exception:
        text = traceback.format_exc()
        print(text, file=sys.stderr)
        if args.child:
            try:
                state = getattr(args, "effect_denial_state", None)
                stream = getattr(args, "effect_denial_stream", None)
                if stream is not None and not stream.closed:
                    try:
                        stream.flush()
                        stream.close()
                    except Exception:
                        if state is not None:
                            state["logging_errors"] += 1
                save(Path(args.run_root) / "child-error.json",
                     {"utc": utc(), "status": "ERROR", "traceback": text, "pid": os.getpid(),
                      "effect_denials": effect_denial_counts(state)})
            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
