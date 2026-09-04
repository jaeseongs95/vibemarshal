"""단일 bugfix fixture의 결정적 관측. Core의 Task·Goal 완료 판정을 대신하지 않는다."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import types


CONTRACT_PATH = Path(__file__).with_name("contract.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_json(value: object) -> str:
    return digest_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False).encode("utf-8"))


def load_contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def snapshot(workspace: Path) -> dict[str, str]:
    # 저장소 메타데이터와 Python bytecode만 제외한다. 운영 원장은 workspace 밖에 둔다.
    return {
        path.relative_to(workspace).as_posix(): digest_bytes(path.read_bytes())
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
        and not {".git", "__pycache__"}.intersection(path.relative_to(workspace).parts)
        and path.suffix not in {".pyc", ".pyo"}
    }


def outside_add(source: str) -> str:
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "add"]
    if len(functions) != 1:
        raise ValueError("최상위 add 함수가 정확히 하나 있어야 합니다.")
    # 구현 본문은 정답 문자열과 비교하지 않는다. 공개 선언과 나머지 소스만 보존 검사한다.
    functions[0].body = [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def observe(workspace: Path, phase: str) -> dict:
    if phase not in {"task", "goal"}:
        raise ValueError("지원하지 않는 검사 단계입니다.")
    workspace = workspace.resolve(strict=True)
    contract = load_contract()
    expected = contract["initial_snapshot"]["files"]
    baseline = REPOSITORY_ROOT / contract["project_fixture"]
    if snapshot(baseline) != expected or digest_json(expected) != contract["initial_snapshot"]["snapshot_digest"]:
        raise ValueError("원본 fixture 또는 초기 snapshot 계약이 변경됐습니다.")
    before = snapshot(workspace)
    checks: list[dict] = []

    def record(name: str, passed: bool, **detail: object) -> None:
        checks.append({"name": name, "passed": passed, **detail})

    record("project_file_set", set(before) == set(expected),
           unexpected=sorted(set(before) - set(expected)), missing=sorted(set(expected) - set(before)))
    for filename in ("test_app.py", "AGENTS.md"):
        record(f"preserved:{filename}", before.get(filename) == expected[filename])

    try:
        source = (workspace / "app.py").read_text(encoding="utf-8")
        original = (baseline / "app.py").read_text(encoding="utf-8")
        record("outside_add_preserved", outside_add(source) == outside_add(original))
        module = types.ModuleType("bugfix_trace_app")
        module.__file__ = str(workspace / "app.py")
        exec(compile(source, module.__file__, "exec", dont_inherit=True), module.__dict__)
        signature = str(inspect.signature(module.add))
        record("public_signature", signature == contract["oracles"]["public_signature"], actual=signature)
        if phase == "goal":
            for left, right, expected_sum in contract["oracles"]["goal"]["behavior_cases"]:
                for mode in ("positional", "keyword"):
                    actual = (module.add(left, right) if mode == "positional"
                              else module.add(left=left, right=right))
                    record(f"behavior:{left}:{right}:{mode}", type(actual) is int and actual == expected_sum,
                           expected=expected_sum, actual=repr(actual))
    except Exception as error:
        record("app_observation", False, error=f"{type(error).__name__}: {error}")

    argv = [str(Path(sys.executable).resolve()) if value == "{python}" else value
            for value in contract["oracles"]["unittest_argv"]]
    try:
        result = subprocess.run(argv, cwd=workspace, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False,
                                timeout=contract["oracles"]["timeout_seconds"])
        record("fresh_unittest", result.returncode == 0, argv=argv, cwd=str(workspace),
               exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
    except (OSError, subprocess.TimeoutExpired) as error:
        record("fresh_unittest", False, argv=argv, error=str(error))
    after = snapshot(workspace)
    record("inspection_did_not_mutate_sources", before == after)
    return {
        "fixture_id": contract["fixture_id"],
        "contract_digest": digest_json(contract),
        "phase": phase,
        "observation_scope": "deterministic_fixture_checks_only",
        "workspace": str(workspace),
        "before": before,
        "after": after,
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--phase", choices=("task", "goal"), required=True)
    args = parser.parse_args()
    try:
        report = observe(args.workspace, args.phase)
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
