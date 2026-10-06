"""C1 non-product PRE fixture source. Execute only after new SOURCE/PRE acceptance."""
import argparse
import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

PACKET = Path(__file__).resolve().parent
FROZEN = PACKET / "frozen" / "runner.py"
DUMMY_IDS = ["c1_non_product.synthetic_shape." + str(i) for i in range(5)]
BUDGET = 30.0


def digest(path):
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def now():
    return datetime.now(timezone.utc).isoformat()


def create_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def source_inputs(args):
    if (sys.platform != "linux" or sys.version_info[:2] != (3, 12)
            or not sys.flags.isolated or not sys.flags.no_site
            or not sys.dont_write_bytecode or not sys.flags.utf8_mode):
        raise RuntimeError("Linux Python3.12 -I -S -B -X utf8 required")
    checks = {"fixture.py": args.fixture_sha256, "manifest.json": args.manifest_sha256,
              "SOURCE.SHA256.json": args.seal_sha256}
    for name, expected in checks.items():
        if digest(PACKET / name)["sha256"] != expected:
            raise RuntimeError("fixture external pin mismatch")
    manifest = json.loads((PACKET / "manifest.json").read_bytes())
    seal = json.loads((PACKET / "SOURCE.SHA256.json").read_bytes())
    if (manifest["schema"] != "vm-c1-non-product-preparation-r03-01"
            or manifest["dummy_test_ids"] != DUMMY_IDS
            or manifest["fixture"] != digest(PACKET / "fixture.py")
            or manifest["frozen_runner"] != digest(FROZEN)):
        raise RuntimeError("fixture contract mismatch")
    snapshot = {}
    for name, expected in seal.items():
        path = PACKET / name
        if path.is_symlink() or path.resolve() != path or digest(path) != expected:
            raise RuntimeError("sealed fixture source mismatch")
        snapshot[name] = digest(path)
    snapshot["SOURCE.SHA256.json"] = digest(PACKET / "SOURCE.SHA256.json")
    return manifest, snapshot


def selected_tree():
    tree = ast.parse(FROZEN.read_bytes(), filename=str(FROZEN))
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    child = functions["child"]
    parent = functions["supervise"]
    handler = next(n for n in functions["main"].body if isinstance(n, ast.Try)).handlers[0]
    module = next(n.value.value for n in tree.body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "MODULE")
    segments = {}
    body = []
    for name in ("utc", "save", "effect_denial_counts", "observe_effect_denials", "residual_files"):
        node = copy.deepcopy(functions[name])
        segments[name] = [functions[name]]
        body.append(node)

    def wrapper(name, parameters, nodes, tail=None):
        nodes = list(nodes)
        segments[name] = nodes
        copied = copy.deepcopy(nodes)
        if tail:
            extra = ast.parse(tail).body[0]
            ast.increment_lineno(extra, max(n.end_lineno for n in nodes))
            copied.append(extra)
        function = ast.parse("def " + name + "(" + parameters + "):\n    pass\n").body[0]
        function.body = copied
        function.lineno = min(n.lineno for n in nodes)
        function.end_lineno = max(n.end_lineno for n in copied)
        body.append(function)

    wrapper("guard_factory", "root, args, tmp", child.body[5:10],
            "return denial_state, denial_stream, audit")
    wrapper("finish_synthetic", "root, args, denial_state, denial_stream, ids, result, started, before",
            child.body[31:43])
    wrapper("parent_acceptance",
            "root, manifest, process, terminal, timed_out, code, before, after, started, kill_error",
            list(parent.body[12:25]) + [parent.body[26]])
    wrapper("capture_child_error", "args, text", handler.body[2:4])
    catalog = {name: {
        "lines": [[n.lineno, n.end_lineno] for n in nodes],
        "ast_sha256": hashlib.sha256(
            ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False).encode()).hexdigest()
    } for name, nodes in segments.items()}
    selected = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    catalog["_selected_module"] = {
        "ast_sha256": hashlib.sha256(ast.dump(selected, include_attributes=False).encode()).hexdigest()}
    return selected, catalog, module


def runtime_files():
    rows = []
    for name, module in sorted(sys.modules.copy().items()):
        value = getattr(module, "__file__", None)
        if value:
            path = Path(value)
            rows.append({"module": name, "path": str(path),
                         "pin": digest(path) if path.is_file() else None})
    return rows


def bindings(args):
    selected, catalog, module = selected_tree()
    expected = json.loads((PACKET / "EXTRACTIONS.json").read_bytes())
    if catalog != expected:
        raise RuntimeError("frozen AST extraction mismatch")
    namespace = {"__builtins__": __builtins__, "os": os, "sys": sys, "json": json,
                 "hashlib": hashlib, "Path": Path, "datetime": datetime, "timezone": timezone,
                 "traceback": traceback, "MODULE": module,
                 "inputs": lambda ignored: source_inputs(args), "closure": runtime_files}
    # Only selected definitions/wrappers, never the frozen module or its main/child/supervise.
    exec(compile(selected, str(FROZEN), "exec"), namespace)
    return namespace


class SyntheticResult:
    testsRun = 5
    failures = errors = skipped = expectedFailures = unexpectedSuccesses = ()
    def wasSuccessful(self):
        return True  # Shape fixture only: no unittest, product test, or loader ran.


class FaultStream:
    def __init__(self, stream, args, fault):
        self.stream, self.args, self.fault, self.fired = stream, args, fault, False
    def inject(self, operation):
        state = self.args.effect_denial_state
        if (self.fault == operation and not self.fired
                and (operation == "close" or state["count"] > 0)):
            self.fired = True
            self.args.fault_observations.append(
                {"operation": operation, "count_before_fault": state["count"]})
            raise OSError("owned synthetic journal I/O fault")
    def write(self, value):
        self.inject("write")
        return self.stream.write(value)
    def flush(self):
        self.inject("flush")
        return self.stream.flush()
    def close(self):
        self.inject("close")
        return self.stream.close()
    @property
    def closed(self):
        return self.stream.closed


class OpenFault:
    def __init__(self, path, args, fault):
        self.path, self.args, self.fault = path, args, fault
    def open(self, *positional, **named):
        if self.fault == "init":
            self.args.fault_observations.append(
                {"operation": "init", "count_before_fault": self.args.effect_denial_state["count"]})
            raise OSError("owned synthetic journal initialization fault")
        return FaultStream(self.path.open(*positional, **named), self.args, self.fault)


class InitRoot:
    def __init__(self, root, args, fault):
        self.root, self.args, self.fault = root, args, fault
    def __truediv__(self, name):
        if name != "effect-denials.jsonl":
            raise RuntimeError("dummy facade exceeded journal initialization seam")
        return OpenFault(self.root / name, self.args, self.fault)


def preserve_and_inject(path, payload):
    # Only a newly owned dummy file. Preserve original bytes; never delete or repair evidence.
    path.rename(path.with_name(path.name + ".before-fault"))
    with path.open("xb") as stream:
        stream.write(payload)


def mutate_evidence(name, root, bound):
    journal = root / "effect-denials.jsonl"
    raw = json.loads((root / "result.json").read_bytes())
    post = json.loads((root / "postflight.json").read_bytes())
    if name == "journal_missing":
        journal.rename(root / "effect-denials.jsonl.before-fault")
    elif name == "journal_partial":
        preserve_and_inject(journal, b"{")
    elif name == "journal_corrupt":
        preserve_and_inject(journal, b"not-json\n")
    elif name == "pid_mismatch":
        row = json.loads(journal.read_bytes())
        row["pid"] += 1
        preserve_and_inject(journal, (json.dumps(row) + "\n").encode())
    elif name in ("count_mismatch", "bool_counter"):
        raw["effect_denials"]["count"] = 1 if name == "count_mismatch" else False
        raw["suite_exit"] = 1 if name == "count_mismatch" else 0
        preserve_and_inject(root / "result.json", (json.dumps(raw) + "\n").encode())
    elif name == "post_counter_mismatch":
        post["effect_denials"]["count"] = 1
        preserve_and_inject(root / "postflight.json", (json.dumps(post) + "\n").encode())
        return
    else:
        return
    # Match the observer to the injected data so UNKNOWN need not rely on an unrelated mismatch.
    post["effect_denials"] = raw["effect_denials"]
    post["effect_denial_observation"] = bound["observe_effect_denials"](
        root, os.getpid(), raw["effect_denials"])
    preserve_and_inject(root / "postflight.json", (json.dumps(post) + "\n").encode())


def child(args):
    manifest, before = source_inputs(args)
    root = Path(args.run_root)
    tmp = root / "tmp"
    if (not root.is_absolute() or root.resolve() != root or root.is_relative_to(PACKET)
            or Path.cwd() != root / "cwd" or list((root / "cwd").iterdir())
            or any((root / name).is_symlink() for name in ("home", "tmp", "cwd", "cases"))
            or os.environ.get("HOME") != str(root / "home")
            or any(os.environ.get(name) != str(tmp) for name in ("TMPDIR", "TMP", "TEMP"))
            or Path(tempfile.gettempdir()).resolve() != tmp):
        raise RuntimeError("owned PRE child roots/environment mismatch")
    bound = bindings(args)
    route = {"hook": None}
    def dispatch(event, values):
        hook = route["hook"]
        if hook is not None:
            hook(event, values)
    sys.addaudithook(dispatch)  # Only this single owned child; no hook in parent.
    records = []
    for requirement in manifest["required_cases"]:
        if time.monotonic() >= args.deadline:
            raise RuntimeError("PRE deadline before case")
        name = requirement["name"]
        case_root = root / "cases" / name
        case_root.mkdir()
        local = SimpleNamespace(child=True, run_root=str(case_root), fault_observations=[])
        caught, error_capture, stage_return = None, False, None
        try:
            state, stream, hook = bound["guard_factory"](
                InitRoot(case_root, local, requirement.get("io_fault")), local, tmp)
            route["hook"] = hook
            if requirement["synthetic_event"]:
                if name == "uncaught_denial":
                    sys.audit("os.system", "C1 synthetic event only; no OS command")
                else:
                    try:
                        sys.audit("os.system", "C1 synthetic event only; no OS command")
                    except RuntimeError as error:
                        caught = {"type": type(error).__name__, "message": str(error)}
                    if caught != {"type": "RuntimeError", "message": "C5 prohibited effect: os.system"}:
                        raise RuntimeError("original refusal exception changed or missing")
            stage_return = bound["finish_synthetic"](
                case_root, local, state, stream, DUMMY_IDS, SyntheticResult(), now(), before)
        except Exception:
            text = traceback.format_exc()
            error_capture = True
            stage_return = bound["capture_child_error"](local, text)
        finally:
            route["hook"] = None
            stream = getattr(local, "effect_denial_stream", None)
            if isinstance(stream, FaultStream) and not stream.stream.closed:
                stream.stream.close()  # Owned fixture finalizer; preserve recorded error and raw bytes.
        if not error_capture:
            mutate_evidence(name, case_root, bound)
        state = getattr(local, "effect_denial_state", None)
        counts = bound["effect_denial_counts"](state)
        previous_count = state["count"] if state is not None else None
        sys.audit("os.system", "disabled-router synthetic sentinel; no command")
        if state is not None and state["count"] != previous_count:
            raise RuntimeError("audit leaked across case boundary")
        observation = {"scope": "non_product_dummy", "name": name, "pid": os.getpid(),
                       "caught": caught, "outer_error_capture": error_capture,
                       "stage_return": stage_return, "stage_return_is_not_native_exit": True,
                       "final_counters": counts, "fault_observations": local.fault_observations,
                       "routing_disabled_between_cases": True,
                       "synthetic_unittest_shape": not error_capture,
                       "actual_product_tests_or_loader": 0}
        create_json(case_root / "fixture-observation.json", observation)
        records.append(observation)
    _, after = source_inputs(args)
    create_json(root / "fixture-child.json", {
        "scope": "non_product_dummy", "pid": os.getpid(), "argv": sys.argv,
        "cwd": str(Path.cwd()), "utc": now(), "python": sys.version, "executable": sys.executable,
        "source_before": before, "source_after": after, "cases": records,
        "source_unchanged": before == after, "module_files_after": runtime_files()})
    return 0 if before == after else 2


def check_case(requirement, execution, observation):
    errors = []
    def check(condition, label):
        if not condition:
            errors.append(label)
    check(execution["status"] == requirement["acceptance"], "acceptance")
    evidence = execution["effect_denial_observation"]
    check(evidence["state"] == requirement["denial_state"], "denial_state")
    check(execution["unittest_observation_unknown"] == requirement["native_shape_unobserved"],
          "synthetic_shape_presence")
    check(observation["final_counters"] == requirement["final_counters"], "final_counters")
    check(observation["outer_error_capture"] == requirement["outer_error_capture"], "error_capture")
    if requirement["synthetic_event"] and not requirement["outer_error_capture"]:
        check(observation["caught"] == {"type": "RuntimeError", "message": "C5 prohibited effect: os.system"},
              "RuntimeError_preserved")
    if requirement.get("io_fault"):
        expected_count = 0 if requirement["io_fault"] == "init" else 1
        check(observation["fault_observations"] == [
            {"operation": requirement["io_fault"], "count_before_fault": expected_count}],
            "counter_before_IO_fault")
    for key, value in requirement.get("observer_fields", {}).items():
        check(evidence.get(key) == value, "observer_" + key)
    return errors


def supervise(args):
    manifest, before = source_inputs(args)
    root = Path(args.run_root)
    if (not root.is_absolute() or root.resolve() != root or root.exists()
            or root.is_symlink() or root.is_relative_to(PACKET) or not root.parent.is_dir()):
        raise RuntimeError("fresh absolute PRE root outside fixture required")
    root.mkdir()
    for name in ("home", "tmp", "cwd", "cases"):
        (root / name).mkdir()
    deadline = time.monotonic() + BUDGET
    env = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "TMP": str(root / "tmp"),
           "TEMP": str(root / "tmp"), "PATH": os.defpath}
    argv = [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(PACKET / "fixture.py"),
            "--child", "--run-root", str(root), "--fixture-sha256", args.fixture_sha256,
            "--manifest-sha256", args.manifest_sha256, "--seal-sha256", args.seal_sha256,
            "--deadline", str(deadline)]
    create_json(root / "invocation.json", {
        "scope": "non_product_dummy", "utc": now(), "parent_argv": sys.argv,
        "parent_cwd": str(Path.cwd()), "child_argv": argv, "child_cwd": str(root / "cwd"),
        "child_env": env, "deadline_monotonic": deadline, "budget_seconds": BUDGET,
        "source_before": before, "python": sys.version, "executable": sys.executable,
        "parent_module_files": runtime_files(), "child_count": 1, "retry": 0})
    if time.monotonic() >= deadline:
        raise RuntimeError("PRE deadline before only child")
    with (root / "stdout.txt").open("xb") as out, (root / "stderr.txt").open("xb") as err:
        process = subprocess.Popen(argv, cwd=root / "cwd", env=env,
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        started = now()
        timed_out, kill_error = False, None
        try:
            code = process.wait(timeout=max(0.0, deadline - time.monotonic() - 3.0))
            terminal = True
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                process.kill()
            except OSError as error:
                kill_error = repr(error)
            try:
                code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                terminal = True
            except subprocess.TimeoutExpired:
                code, terminal = None, False
    try:
        _, after = source_inputs(args)
    except Exception:
        after = {"input_error": traceback.format_exc()}
    bound = bindings(args)
    rows = []
    for requirement in manifest["required_cases"]:
        case_root = root / "cases" / requirement["name"]
        try:
            stage = bound["parent_acceptance"](
                case_root, {"test_ids": DUMMY_IDS}, process, terminal, timed_out, code,
                before, after, started, kill_error)
            execution = json.loads((case_root / "execution.json").read_bytes())
            observation = json.loads((case_root / "fixture-observation.json").read_bytes())
            errors = check_case(requirement, execution, observation)
            rows.append({"name": requirement["name"], "dummy_acceptance": execution["status"],
                         "denial_observation": execution["effect_denial_observation"],
                         "checks_match": not errors, "check_errors": errors,
                         "frozen_parent_return": stage, "product_PASS": False})
        except Exception as error:
            rows.append({"name": requirement["name"], "checks_match": False,
                         "observation_state": "UNKNOWN", "error_type": type(error).__name__,
                         "product_PASS": False})
    report = {"scope": "non_product_dummy", "utc": now(), "pid": process.pid,
              "actual_native_exit": code, "terminal_observed": terminal, "timed_out": timed_out,
              "owned_kill_error": kill_error, "source_before": before, "source_after": after,
              "cases": rows, "product_tests_or_loader": 0, "retry": 0,
              "module_files_after": runtime_files()}
    observed = terminal and not timed_out and code == 0 and before == after
    report["fixture_status"] = "MATCH" if observed and all(r["checks_match"] for r in rows) else "NOT_ACCEPTED"
    create_json(root / "fixture-execution.json", report)
    print(json.dumps({"scope": "non_product_dummy", "fixture_status": report["fixture_status"],
                      "actual_native_exit": code, "run_root": str(root)}))
    return 0 if report["fixture_status"] == "MATCH" else 2


def main():
    parser = argparse.ArgumentParser()
    for name in ("run-root", "fixture-sha256", "manifest-sha256", "seal-sha256"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args()
    if args.child and (args.deadline is None or not 0 < args.deadline < float("inf")):
        parser.error("child requires finite positive deadline")
    try:
        return child(args) if args.child else supervise(args)
    except Exception:
        print(traceback.format_exc(), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

