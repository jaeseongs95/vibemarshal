"""C3 source candidate. Execute only after this packet's SOURCE/PRE."""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
from datetime import datetime, timezone
from pathlib import Path

PACKET = Path(__file__).resolve().parent
MODULE = "test_flowmarshal_implementation_workflow"
SOURCE_PATHS = (
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
    if (manifest["schema"] != "vm-common3-source-r03"
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
        if name not in ("__main__", MODULE) and name.split(".")[0] not in sys.stdlib_module_names:
            raise RuntimeError("non-stdlib module: " + name)
        path = getattr(module, "__file__", None)
        if path:
            if any(part in ("site-packages", "dist-packages") for part in Path(path).parts):
                raise RuntimeError("package origin: " + path)
            files[name] = str(Path(path).resolve())
    return files


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

    def audit(event, values):
        # This checks the selected Python seams; it is not an OS sandbox.
        if (event.startswith(("socket.", "subprocess.", "os.exec", "os.spawn"))
                or event in ("os.system", "os.posix_spawn", "ctypes.dlopen", "os.chdir")):
            raise RuntimeError("C3 prohibited effect: " + event)
        if event == "import":
            name = values[0]
            if name != MODULE and name.split(".")[0] not in sys.stdlib_module_names:
                raise RuntimeError("C3 non-stdlib import: " + name)
        if event == "sqlite3.connect":
            path = Path(os.fspath(values[0]))
            if not path.is_absolute() or not path.resolve().is_relative_to(tmp):
                raise RuntimeError("C3 SQLite outside private tmp")

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
        "helper": str(Path(module.impl.__file__).resolve()),
        "legacy_import_only": str(Path(module.legacy.__file__).resolve()),
    }
    if list(origins.values()) != [str(PACKET / name) for name in SOURCE_PATHS]:
        raise RuntimeError("source origins mismatch: " + repr(origins))
    for case in cases:
        method = getattr(case, case._testMethodName)
        if Path(method.__func__.__code__.co_filename).resolve() != Path(origins["test"]):
            raise RuntimeError("test code origin mismatch")
    for name in ("json_output", "validate_manifest", "_same_path"):
        if Path(getattr(module.impl, name).__code__.co_filename).resolve() != Path(origins["helper"]):
            raise RuntimeError("helper code origin mismatch")
    save(root / "preflight.json", {
        "utc": utc(), "argv": sys.argv, "cwd": str(Path.cwd()), "pid": os.getpid(),
        "python": sys.version, "executable": sys.executable, "sqlite": sqlite3.sqlite_version,
        "loader_ids": ids, "loader_count": len(cases), "loader_errors": loader.errors,
        "origins": origins, "module_files": closure(), "inputs_before": before,
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
    data["suite_exit"] = 0 if ok else 1
    # Preserve the original result before any additional source/closure observation.
    save(root / "result.json", data)
    _, after = inputs(args)
    save(root / "postflight.json", {
        "utc": utc(), "inputs_after": after, "matches_before": before == after,
        "module_files_after": closure(),
    })
    return data["suite_exit"] if before == after else 2


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
        "inputs_before": before, "scope": "C3 only; own child timeout kill/reap, no retry/global kill/cleanup",
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
    residual = residual_files(root)
    observation_unknown = (not terminal or raw_error is not None or before != after
                           or any("error" in row for row in residual))
    raw_ok = (isinstance(raw, dict) and raw.get("suite_exit") == 0
              and raw.get("test_ids") == manifest["test_ids"] and raw.get("testsRun") == 3
              and raw.get("wasSuccessful") is True
              and all(raw.get(name) == [] for name in
                      ("failures", "errors", "skipped", "expectedFailures", "unexpectedSuccesses")))
    if timed_out:
        status = "TIMEOUT" if terminal else "UNKNOWN"
    elif observation_unknown:
        status = "UNKNOWN"
    else:
        status = "PASS" if code == 0 and raw_ok else "FAIL"
    save(root / "execution.json", {
        "started_utc": started, "observed_utc": utc(), "pid": process.pid,
        "actual_exit": code, "terminal_observed": terminal, "status": status,
        "timed_out": timed_out, "own_child_kill_error": kill_error,
        "raw_observation_error": raw_error, "observation_unknown": observation_unknown,
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
        MODULE + ".ImplementationWorkflowTest.test_json_output_is_ascii_safe_and_round_trips_unicode",
        MODULE + ".ImplementationWorkflowTest.test_revision5_requires_typed_ancestor_inputs_and_evidence_contract",
        MODULE + ".ImplementationWorkflowTest.test_same_path_accepts_only_drive_and_unc_extended_namespaces",
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
                save(Path(args.run_root) / "child-error.json",
                     {"utc": utc(), "status": "ERROR", "traceback": text})
            except Exception:
                print(traceback.format_exc(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
