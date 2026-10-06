
"""Stdlib dummy regression only. Does not import or execute product/runner modules."""
import argparse
import ast
import contextlib
import functools
import io
import hashlib
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import FunctionType, ModuleType

PACKET = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-sha256", required=True)
    parser.add_argument("--self-sha256", required=True)
    parser.add_argument("--dummy-sha256", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    runner, dummy = PACKET / "runner.py", PACKET / "regression/dummy_helper.py"
    if (digest(runner) != args.runner_sha256
            or digest(Path(__file__)) != args.self_sha256 or digest(dummy) != args.dummy_sha256):
        raise RuntimeError("dummy regression source pin mismatch")
    source = ast.parse(runner.read_text(encoding="utf-8"))
    names = {"utc", "checked_helper_origin", "helper_origin_observation", "record_helper_origin"}
    node = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if len(node) != 4:
        raise RuntimeError("exact validator AST missing")
    namespace = {"Path": Path, "contextmanager": contextlib.contextmanager,
                 "inspect": inspect, "FunctionType": FunctionType, "json": json,
                 "datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=node, type_ignores=[]), str(runner), "exec"), namespace)
    check = namespace["checked_helper_origin"]
    text = dummy.read_text(encoding="utf-8")
    helper = ModuleType("stdlib_origin_dummy")
    exec(compile(text, str(dummy), "exec"), helper.__dict__)
    alien = ModuleType("stdlib_origin_alien_dummy")
    alien_name = str(PACKET / "regression/alien_dummy_filename.py")
    exec(compile(text, alien_name, "exec"), alien.__dict__)
    plain, decorated = helper.command_migrate, helper.open_readonly
    original = decorated.__wrapped__
    rows = []
    origin_log = io.StringIO()
    observe = namespace["helper_origin_observation"]
    record = namespace["record_helper_origin"]

    def trial(label, function_name, function, accepted, error_prefix=None):
        setattr(helper, function_name, function)
        before = observe(helper, function_name, dummy, function)
        record(origin_log, "before", {"case": label, **before})
        try:
            observation = check(helper, function_name, dummy, function)
        except RuntimeError as error:
            observation = {"error": str(error)}
            actual = False
            record(origin_log, "rejected", {"case": label, **before, **observation})
        else:
            actual = True
            record(origin_log, "accepted", {"case": label, **before, **observation})
        if actual != accepted or (error_prefix and not observation.get("error", "").startswith(error_prefix)):
            raise AssertionError((label, actual, accepted, observation))
        rows.append({"case": label, "expected_accept": accepted,
                     "observed_accept": actual, "observation": observation})

    trial("valid_plain", "command_migrate", plain, True)
    old_would_accept = Path(decorated.__code__.co_filename).resolve() == dummy
    if old_would_accept:
        raise AssertionError("dummy did not distinguish r01 wrapper origin")
    trial("valid_stdlib_contextmanager_old_guard_rejects", "open_readonly", decorated, True)
    bad_plain = FunctionType(alien.command_migrate.__code__, helper.__dict__)
    trial("wrong_plain_origin", "command_migrate", bad_plain, False, "helper code origin mismatch")
    bad_original = FunctionType(alien.open_readonly.__wrapped__.__code__, helper.__dict__)
    bad_decorated = contextlib.contextmanager(bad_original)
    trial("genuine_wrapper_wrong_wrapped_origin", "open_readonly", bad_decorated, False, "helper code origin mismatch")
    fake = FunctionType(helper.fake_wrapper.__code__, contextlib.__dict__, name="open_readonly")
    fake.__wrapped__ = original
    trial("fake_wrapper_code_even_with_expected_filename", "open_readonly", fake, False,
          "helper contextmanager wrapper mismatch")
    spoof = contextlib.contextmanager(bad_original)
    spoof.__wrapped__ = original
    trial("spoof_wrapped_metadata_wrong_closure", "open_readonly", spoof, False,
          "helper contextmanager wrapper mismatch")
    wrong_globals = FunctionType(original.__code__, dict(helper.__dict__))
    trial("correct_filename_wrong_original_globals", "open_readonly",
          contextlib.contextmanager(wrong_globals), False, "helper code origin mismatch")
    helper.contextmanager = lambda function: function
    trial("wrong_imported_decorator_binding", "open_readonly", decorated, False,
          "helper contextmanager binding mismatch")
    helper.contextmanager = contextlib.contextmanager
    cycle = contextlib.contextmanager(original)
    cycle.__wrapped__ = cycle
    try:
        inspect.unwrap(cycle)
    except ValueError as error:
        cycle_error = type(error).__name__
    else:
        raise AssertionError("general unwrap did not reject cycle")
    trial("cycle_wrapped_metadata", "open_readonly", cycle, False,
          "helper contextmanager binding mismatch")
    trial("code_less_object", "open_readonly", object(), False, "helper origin requires function")

    @functools.wraps(decorated)
    def nested(*args, **kwargs):
        helper.body_calls.append("nested")
        return decorated(*args, **kwargs)

    nested_unwraps_to_original = inspect.unwrap(nested) is original
    if not nested_unwraps_to_original:
        raise AssertionError("normal nested stdlib wraps chain did not unwrap")
    trial("normal_nested_chain_not_in_pinned_single_decorator_contract", "open_readonly",
          nested, False, "helper contextmanager binding mismatch")
    journal = [json.loads(line) for line in origin_log.getvalue().splitlines()]
    if (len(journal) != 22 or any(journal[i * 2]["phase"] != "before"
            or journal[i * 2]["case"] != rows[i]["case"]
            or journal[i * 2 + 1]["phase"] != ("accepted" if rows[i]["expected_accept"] else "rejected")
            for i in range(11))):
        raise AssertionError("before/accepted/rejected provenance journal mismatch")
    if helper.body_calls or alien.body_calls:
        raise AssertionError("dummy function body was executed")
    data = {
        "scope": "stdlib dummy validator regression; no product/whole runner import or execution",
        "utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
        "platform": sys.platform, "executable": sys.executable,
        "argv": sys.argv, "cwd": str(Path.cwd()), "case_count": len(rows), "cases": rows, "provenance_journal": journal,
        "general_unwrap_cycle_error": cycle_error,
        "normal_nested_chain_unwraps_to_original": nested_unwraps_to_original,
        "nested_binding_policy": "rejected: pinned original declares one contextmanager only",
        "old_guard_accepts_valid_decorated": old_would_accept,
        "old_exposed_co_filename": decorated.__code__.co_filename,
        "wrapped_original_co_filename": original.__code__.co_filename,
        "dummy_body_calls": 0, "product_imports": 0, "product_loader_calls": 0,
        "product_test_runs": 0, "whole_candidate_runner_starts": 0,
        "validator_ast_functions_executed": sorted(names),
        "stdlib_source_pins": {
            "contextlib": {"path": contextlib.__file__, "sha256": digest(Path(contextlib.__file__))},
            "inspect": {"path": inspect.__file__, "sha256": digest(Path(inspect.__file__))},
        },
        "input_pins": {"runner.py": args.runner_sha256, "regression/check_origin.py": args.self_sha256,
                       "regression/dummy_helper.py": args.dummy_sha256},
        "status": "DUMMY_REGRESSION_PASS",
    }
    if len(rows) != 11 or not all(r["expected_accept"] == r["observed_accept"] for r in rows):
        raise AssertionError("exact regression case count/result mismatch")
    with Path(args.result).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps(data, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
