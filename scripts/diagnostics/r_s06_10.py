"""R-S06 검사 근거 대조표의 제한 실제 진단. 기존 실행·원장·oracle은 변경하지 않는다."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from flowmarshal.canonical import canonical_json, json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import GoalContractRevision, PlanContractRevision, PlanSkeletonCandidate, ProjectMapRevision, StateSnapshot, utc_now
from flowmarshal.engine.models import EngineRoleConfiguration
from flowmarshal.engine.plan_inspection_eval import (
    assess_case_inspection_review, bind_case_expectation, verify_case_expectation,
)
from flowmarshal.engine.planner_roles import PlanExpanderAdapter, PlanReviewerAdapter, PlanReviewEnvelope, RuleBasedTaskAssigner
from flowmarshal.engine.planning import plan_gate, risk_route
from flowmarshal.engine.qualification import PlanningScenarioCatalog, ScopeQualificationReport, _model_lock, _planning_contract, source_manifest_digest, source_manifest_files
from flowmarshal.engine.roles import CodexStructuredRoleRunner, RoleCallRequest, strict_json_output_schema
from flowmarshal.engine.runtime import CodexAppServerRuntime


ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / ".flowmarshal-engine-eval/runs/r-s06-09-20260904-v1"
S05 = OLD.parent / "s05-bugfix-trace-20260904"
STATIC_CASES = ("clean", "bad", "wrong-goal", "combined", "boundary-clean", "missing-link", "future-result",
                "stored-expanded", "semantic-explicit", "stored-multi-defect", "semantic-missing-link")
CALL_ORDER = (*STATIC_CASES, "expansion", "expanded-review")
MAXIMUM_CALLS = 13


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def files(root: Path):
    return {str(path.relative_to(root)): sha256_bytes(path.read_bytes()) for path in sorted(root.rglob("*"))
            if path.is_file() and not {".git", "__pycache__"}.intersection(path.parts)}


def locked_input_files(run: Path) -> dict[str, str]:
    """호출 입력과 완료된 Gate artifact를 잠그며 실행 중인 최상위 출력 로그는 분리한다."""
    return {str(path.relative_to(run)): sha256_bytes(path.read_bytes()) for path in sorted(run.rglob("*"))
            if path.is_file() and "runtime-preflight" not in path.parts and "__pycache__" not in path.parts
            and not (path.parent == run and path.suffix == ".log")}


def preserved_files(run: Path | None = None):
    return {str(path.relative_to(ROOT)): sha256_bytes(path.read_bytes())
            for base in sorted(OLD.parent.iterdir())
            if base.is_dir() and base.name.startswith(("r-s06-", "s06-bugfix-", "s05-bugfix-"))
            and (run is None or base.resolve() != run.resolve())
            for path in sorted(base.rglob("*")) if path.is_file() and "__pycache__" not in path.parts}


def copy_new(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(source.read_bytes())


def instruction_binding(run: Path) -> dict[str, Any]:
    """직전 실제 주입 경로를 출발점으로 잠그고 새 thread의 실제 경로와 호출 전에 대조한다."""
    receipt = OLD / "calls/01-compact_plan_reviewer/thread.receipt.json"
    paths = read(receipt)["payload"]["instructionSources"]
    sources = []
    for index, raw in enumerate(paths):
        path = Path(raw)
        if path.resolve().is_relative_to((OLD / "workspace").resolve()):
            path = run / "workspace" / path.relative_to(OLD / "workspace")
        body = path.read_bytes()
        snapshot = f"instruction-sources/{index:02d}-AGENTS.md"
        copy_new(path, run / snapshot)
        sources.append({"path": str(path.resolve()), "content_digest": sha256_bytes(body), "snapshot": snapshot})
    return {"sources": sources, "prior_receipt_digest": sha256_bytes(receipt.read_bytes()),
            "verification": "현재 thread/start receipt의 instructionSources와 turn 전 정확한 경로·본문 digest를 대조한다."}


def verify_instruction_sources(run: Path, observed: list[str]) -> None:
    binding = read(run / "instruction-binding.json")
    normalize = lambda path: os.path.normcase(str(Path(path).resolve()))
    if [normalize(path) for path in observed] != [normalize(item["path"]) for item in binding["sources"]]:
        raise RuntimeError("ACTUAL_INSTRUCTION_SOURCES_MISMATCH")
    for item in binding["sources"]:
        if sha256_bytes(Path(item["path"]).read_bytes()) != item["content_digest"]:
            raise RuntimeError("INSTRUCTION_CONTENT_CHANGED")


def claim_turn(run: Path, capture: Path, intent: Any):
    """공급자 효과 전에 파일로 상한을 소비한다. 실패한 호출도 반환하지 않으며 재개로 우회할 수 없다."""
    if not capture.resolve().is_relative_to((run / "calls").resolve()):
        raise RuntimeError("진단 capture가 호출 경로 밖입니다.")
    if len(list((run / "calls").glob("*/turn.intent.json"))) >= MAXIMUM_CALLS:
        raise RuntimeError("MAXIMUM_PROVIDER_CALLS_EXCEEDED")
    write_new(capture / "turn.intent.json", intent)


class CapturingRuntime(CodexAppServerRuntime):
    def __init__(self, *, run: Path, **kwargs):
        super().__init__(**kwargs)
        self.run = run
        self.capture = run / "runtime-preflight"
        self.indices = {}

    def record(self, kind, value):
        key = (str(self.capture), kind)
        number = self.indices.get(key, 0) + 1
        self.indices[key] = number
        write_new(self.capture / f"{kind}-{number:02d}.json", value)

    def verify_execution_policy(self, cwd):
        result = super().verify_execution_policy(cwd)
        self.record("policy", result)
        return result

    def list_models(self):
        result = super().list_models()
        self.record("inventory", result)
        return result

    def create_thread(self, **kwargs):
        write_new(self.capture / "thread.intent.json", kwargs)
        result = super().create_thread(**kwargs)
        write_new(self.capture / "thread.receipt.json", result)
        verify_instruction_sources(self.run, result.payload.get("instructionSources", []))
        if result.payload.get("thread", {}).get("turns") != []:
            raise RuntimeError("USAGE_REQUIRES_EMPTY_NEW_THREAD")
        return result

    def start_turn(self, **kwargs):
        verify_lock(self.run)
        request = RoleCallRequest.model_validate(read(self.capture / "request.json"))
        thread = read(self.capture / "thread.receipt.json")
        verify_instruction_sources(self.run, thread["payload"].get("instructionSources", []))
        expected = {"prompt": canonical_json(request.payload), "model": request.model, "effort": request.effort,
                    "output_schema": read(self.capture / "strict-schema.json"),
                    "thread_id": thread["binding"]["thread_id"]}
        if any(kwargs[key] != value for key, value in expected.items()) or Path(kwargs["cwd"]).resolve() != Path(request.cwd).resolve():
            raise RuntimeError("ACTUAL_TURN_BINDING_MISMATCH")
        claim_turn(self.run, self.capture, kwargs)
        result = super().start_turn(**kwargs)
        write_new(self.capture / "turn.receipt.json", result)
        return result

    def read(self, **kwargs):
        result = super().read(**kwargs)
        if not result.active and not (self.capture / "terminal.json").exists():
            write_new(self.capture / "terminal.json", result)
        return result


class CapturedRequest(Exception):
    pass


class RequestCapture:
    request = None

    def run(self, request, **kwargs):
        self.request = request
        raise CapturedRequest()


def options(role, inventory_digest, workspace):
    return {"model": role.model, "effort": role.effort, "inventory_digest": inventory_digest, "cwd": workspace}


def invoke(name, runner, run, roles, inventory_digest):
    goal = GoalContractRevision.model_validate(read(run / "input-goal.json"))
    state = StateSnapshot.model_validate(read(run / "input-state.json"))
    project_map = ProjectMapRevision.model_validate(read(run / "input-project-map.json"))
    if name in {"semantic-explicit", "semantic-missing-link"}:
        goal = GoalContractRevision.model_validate(read(run / "input-semantic-explicit-goal.json"))
        state = StateSnapshot.model_validate(read(run / "input-semantic-explicit-state.json"))
    if name == "expansion":
        source = PlanContractRevision.model_validate(read(run / "input-clean-plan.json"))
        assignment = source.definition.tasks[0].assignment
        expander = PlanExpanderAdapter(runner, RuleBasedTaskAssigner(assignment, assignment, assignment),
                                      **options(roles.plan_expander, inventory_digest, run / "workspace"))
        skeleton = PlanSkeletonCandidate.model_validate(read(run / "input-skeleton.json"))
        return expander.expand(candidate=skeleton, goal=goal, state=state, project_map=project_map)
    path = run / ("expanded-plan.json" if name == "expanded-review" else f"input-{name}-plan.json")
    plan = PlanContractRevision.model_validate(read(path))
    reviewer = PlanReviewerAdapter(runner, **options(roles.general_reviewer, inventory_digest, run / "workspace"),
                                   critical_model=roles.critical_reviewer.model, critical_effort=roles.critical_reviewer.effort)
    return reviewer.review(plan=plan, goal=goal, state=state, project_map=project_map, risk_route=risk_route(plan))


def capture_request(name, run, roles, inventory_digest):
    capture = RequestCapture()
    try:
        invoke(name, capture, run, roles, inventory_digest)
    except CapturedRequest:
        return capture.request
    raise AssertionError("요청이 생성되지 않았습니다.")


def case_expectation(run: Path, name: str, request: RoleCallRequest) -> dict[str, Any]:
    """반드시 해당 사례의 사전 검토표를 읽는다. clean 표나 미평가 PASS로 대체하지 않는다."""
    expectation = read(run / "case-expectations" / f"{name}.json")
    verify_case_expectation(expectation, case_id=name, payload=request.payload)
    return expectation


def verify_lock(run):
    lock = read(run / "preflight.json")
    body = dict(lock)
    digest = body.pop("lock_digest")
    if sha256_digest(body) != digest:
        raise RuntimeError("PREFLIGHT_LOCK_CHANGED")
    if source_manifest_digest(ROOT) != lock["source_manifest_digest"]:
        raise RuntimeError("SOURCE_LOCK_CHANGED")
    if preserved_files(run) != lock["original_files"]:
        raise RuntimeError("PRESERVED_ORIGINALS_CHANGED")
    for name, expected in lock["locked_files"].items():
        if sha256_bytes((run / name).read_bytes()) != expected:
            raise RuntimeError(f"INPUT_LOCK_CHANGED: {name}")
    binding = read(run / "instruction-binding.json")
    for item in binding["sources"]:
        if sha256_bytes(Path(item["path"]).read_bytes()) != item["content_digest"]:
            raise RuntimeError("INSTRUCTION_CONTENT_CHANGED: " + item["path"])
    if sha256_bytes(Path(__file__).read_bytes()) != lock["harness_digest"]:
        raise RuntimeError("HARNESS_LOCK_CHANGED")
    if lock["call_order"] != list(CALL_ORDER) or any(lock[key] != MAXIMUM_CALLS for key in
            ("maximum_logical_calls", "maximum_provider_turns")) or lock["schema_recovery_attempts"] != 0:
        raise RuntimeError("CALL_BUDGET_LOCK_CHANGED")
    generated_lock_path = run / "generated-input-lock.json"
    if generated_lock_path.exists():
        generated = read(generated_lock_path)
        request = RoleCallRequest.model_validate(read(run / "requests/expanded-review.json"))
        expectation = case_expectation(run, "expanded-review", request)
        if (generated["request_digest"] != request.request_digest or
                generated["expectation_digest"] != expectation["expectation_digest"] or
                generated["assessment_digest"] != sha256_bytes((run / "generation-assessment.json").read_bytes()) or
                generated["preflight_digest"] != lock["lock_digest"] or
                generated["plan_digest"] != PlanContractRevision.model_validate(read(run / "expanded-plan.json")).activation_digest):
            raise RuntimeError("GENERATED_INPUT_LOCK_CHANGED")
    return lock


def prepare(run):
    if (run / "preflight.json").exists() or (run / "calls").exists():
        raise RuntimeError("이미 잠그거나 실행한 진단은 반복하지 않습니다.")
    report = ScopeQualificationReport.model_validate(read(run / "deterministic/qualification-report.json"))
    source = source_manifest_digest(ROOT)
    if not report.passed or read(run / "deterministic/evaluation-contract.json")["source_manifest_digest"] != source:
        raise RuntimeError("DETERMINISTIC_GATE_FAILED_OR_STALE")
    from scripts.diagnostics.r_s06_10_fixtures import build_revision, verify_reviewed_case
    build_revision(OLD, run)
    review = read(run / "independent-fixture-review.json")
    if (review.get("review_complete") is not True or review.get("reviewed_cases") != list(STATIC_CASES) or
            review.get("expectations_digest") != sha256_bytes((run / "expectations.json").read_bytes())):
        raise RuntimeError("INDEPENDENT_FIXTURE_REVIEW_MISSING_OR_STALE")
    for name in files(OLD / "workspace"):
        copy_new(OLD / "workspace" / name, run / "workspace" / name)
    copy_new(S05 / "roles.json", run / "roles.json")
    roles = EngineRoleConfiguration.model_validate(read(run / "roles.json"))
    old_lock = read(S05 / "input-lock.json")
    goal = GoalContractRevision.model_validate(read(run / "input-goal.json"))
    state = StateSnapshot.model_validate(read(run / "input-state.json"))
    project_map = ProjectMapRevision.model_validate(read(run / "input-project-map.json"))
    skeleton = PlanSkeletonCandidate.model_validate(read(run / "input-skeleton.json"))
    for name in STATIC_CASES:
        case_goal, case_state = goal, state
        if name.startswith("semantic-"):
            case_goal = GoalContractRevision.model_validate(read(run / "input-semantic-explicit-goal.json"))
            case_state = StateSnapshot.model_validate(read(run / "input-semantic-explicit-state.json"))
        plan = PlanContractRevision.model_validate(read(run / f"input-{name}-plan.json"))
        if plan_gate(plan, source=skeleton, goal=case_goal, state=case_state, project_map=project_map):
            raise RuntimeError(f"FIXED_INPUT_GATE_FAILED: {name}")
    with CapturingRuntime(run=run, codex_bin=Path(old_lock["codex_bin"])) as runtime:
        policy = runtime.verify_execution_policy(run / "workspace")
        inventory = runtime.list_models()
        roles.validate_inventory(inventory)
        if policy.permission_profile != ":danger-full-access" or policy.approval_policy != "never":
            raise RuntimeError("PERMISSION_POLICY_MISMATCH")
        if (_model_lock(inventory, roles) != old_lock["model_lock_digest"] or
                inventory.inventory_digest != old_lock["inventory_digest"] or runtime.executable_digest != old_lock["codex_bin_digest"]):
            raise RuntimeError("MODEL_OR_EXECUTABLE_LOCK_CHANGED")
        for name in CALL_ORDER[:-1]:
            request = capture_request(name, run, roles, inventory.inventory_digest)
            write_new(run / "requests" / f"{name}.json", request)
            write_new(run / "schemas" / f"{name}.json", strict_json_output_schema(request.output_schema))
            if name in STATIC_CASES:
                expectations = read(run / "expectations.json")
                verify_reviewed_case(name, request.payload, expectations, review)
                expectation = bind_case_expectation(
                    case_id=name, payload=request.payload,
                    rows=expectations["case_ac_validation_rows"][name], defects=expectations[name],
                    review_digest=sha256_bytes((run / "independent-fixture-review.json").read_bytes()),
                )
                write_new(run / "case-expectations" / f"{name}.json", expectation)
        catalog = PlanningScenarioCatalog.model_validate(read(ROOT / "tests/fixtures/engine/planning-scenarios.json"))
        contract = _planning_contract(ROOT, catalog, inventory, roles)
        write_new(run / "planning-binding.json", contract)
        templates = {}
        base = capture_request("clean", run, roles, inventory.inventory_digest)
        for role, binding in (("compact_plan_reviewer", roles.general_reviewer),
                              ("critical_effect_reviewer", roles.critical_reviewer),
                              ("high_risk_reviewer", roles.critical_reviewer),
                              ("external_effect_reviewer", roles.critical_reviewer)):
            templates[role] = {"instructions": base.instructions, "output_schema": strict_json_output_schema(base.output_schema),
                               "model": binding.model, "effort": binding.effort}
        write_new(run / "generated-review-template.json", {
            "input_dependency": "expanded-plan.json", "templates": templates, "expected_generation": "결함 없는 Plan",
            "generation_gate": "호출 전 고정 기준으로 생성 결과를 독립 대조한다. 결함이면 FAIL로 종료하고 재생성하지 않는다.",
            "expected_review_defects": [], "generation_criteria": [
                "AC가 명시한 task/goal 절차와 복합 unittest ID 연결을 모두 보존한다.",
                "전역 semantic 의무를 Task에 보존하되 AC의 추가 연결로 확대하지 않는다.",
                "도구·phase의 실제 범위와 별도 실행·기대값 비교를 대조한다.",
                "Worker 응답 제출 뒤 Validator 검사 순서, Skeleton 기여 집합, 독립 Goal Test와 ready-time 명령 경계를 보존한다.",
                "생성된 AC×validation 전체 관계를 원문과 독립 대조한 새 표를 기록하고 생성 입력·등록 근거 digest에 결속한다.",
            ]})
        write_new(run / "instruction-binding.json", instruction_binding(run))
        source_files = source_manifest_files(ROOT)
        for name in source_files:
            copy_new(ROOT / name, run / "executed-source" / name)
        write_new(run / "executed-source-manifest.json", {"files": source_files, "source_manifest_digest": source})
        locked = locked_input_files(run)
        body = {"session": "R-S06-17", "source_manifest_digest": source, "locked_files": locked,
                "harness_digest": sha256_bytes(Path(__file__).read_bytes()), "original_files": preserved_files(run),
                "base_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "policy": policy, "inventory_digest": inventory.inventory_digest, "model_lock_digest": _model_lock(inventory, roles),
                "codex_bin": old_lock["codex_bin"], "codex_bin_digest": runtime.executable_digest,
                "role_configuration_digest": roles.configuration_digest, "call_order": CALL_ORDER,
                "maximum_logical_calls": MAXIMUM_CALLS, "maximum_provider_turns": MAXIMUM_CALLS, "schema_recovery_attempts": 0,
                "prompt_digest": contract.prompt_digest, "output_schema_digest": contract.output_schema_digest,
                "deterministic_report_digest": report.report_digest, "observed_at": utc_now(),
                "scope": "제한 진단. 전체 S06·qualification·Plan 활성화·Worker 실행·1.0 cutover는 수행하지 않는다."}
        body = json_value(body)
        write_new(run / "preflight.json", body | {"lock_digest": sha256_digest(body)})
    print(json.dumps({"prepared": True, "maximum_calls": MAXIMUM_CALLS, "lock_digest": sha256_digest(body)}), flush=True)


class RecordedRunner:
    def __init__(self, runtime, run, lock):
        self.runtime, self.run_root, self.lock = runtime, run, lock
        self.runner = CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0)
        self.name = None
        self.last_result = None

    def run(self, request, *, validator=None):
        verify_lock(self.run_root)
        expected = RoleCallRequest.model_validate(read(self.run_root / "requests" / f"{self.name}.json"))
        if request.request_digest != expected.request_digest:
            raise RuntimeError("ACTUAL_REQUEST_BINDING_MISMATCH")
        if self.name != "expansion":
            case_expectation(self.run_root, self.name, request)
        actual_schema = strict_json_output_schema(request.output_schema)
        expected_schema = (read(self.run_root / "schemas" / f"{self.name}.json") if self.name != "expanded-review" else
                           read(self.run_root / "generated-review-template.json")["templates"][request.role]["output_schema"])
        if actual_schema != expected_schema:
            raise RuntimeError("ACTUAL_SCHEMA_BINDING_MISMATCH")
        number = CALL_ORDER.index(self.name) + 1
        capture = self.run_root / "calls" / f"{number:02d}-{request.role}"
        capture.mkdir(parents=True, exist_ok=False)
        self.runtime.capture = capture
        write_new(capture / "request.json", request)
        write_new(capture / "strict-schema.json", actual_schema)
        try:
            result = self.runner.run(request, validator=validator)
        except Exception as error:
            write_new(capture / "failed.json", {"error_type": type(error).__name__, "error": str(error),
                                              "receipts": getattr(error, "receipts", ()), "observed_at": utc_now()})
            raise
        self.last_result = result
        write_new(capture / "result.json", result)
        receipt = result.receipt
        if (receipt.input_digest != request.request_digest or receipt.output_digest != sha256_digest(result.payload) or
                receipt.output_schema_digest != sha256_digest(strict_json_output_schema(request.output_schema)) or
                receipt.schema_recovery_attempts != 0 or len(receipt.turn_ids) != 1):
            raise RuntimeError("ACTUAL_RECEIPT_BINDING_MISMATCH")
        terminal = read(capture / "terminal.json")
        if json.loads(terminal["final_response"]) != result.payload:
            raise RuntimeError("RAW_RESULT_BINDING_MISMATCH")
        verification = completed_call_verification(capture, json_value(receipt))
        write_new(capture / "binding-verification.json", verification)
        if not verification["passed"]:
            raise RuntimeError("ACTUAL_COMPLETED_CALL_BINDING_MISMATCH")
        return result


def completed_call_verification(capture: Path, receipt: dict[str, Any] | None) -> dict[str, Any]:
    """완료 receipt 결속은 다음 사례를 호출하기 전에 확인한다."""
    request = RoleCallRequest.model_validate(read(capture / "request.json"))
    terminal = read(capture / "terminal.json")["payload"]
    thread = read(capture / "thread.receipt.json")["payload"]["thread"]
    intent = read(capture / "turn.intent.json")
    turn_receipt = read(capture / "turn.receipt.json")
    checks = {
        "payload": json.loads(intent["prompt"]) == request.payload,
        "prompt": terminal.get("prompt_digest") == sha256_digest(intent["prompt"]),
        "instructions": read(capture / "thread.intent.json")["developer_instructions"] == request.instructions,
        "schema": intent["output_schema"] == read(capture / "strict-schema.json"),
        "model_effort": intent["model"] == request.model and intent["effort"] == request.effort,
        "thread_turn": intent["thread_id"] == thread["id"] == terminal.get("thread_id") and
                       terminal.get("turn_id") == turn_receipt["operation_id"],
        "receipt": receipt is not None and receipt["input_digest"] == request.request_digest and
                   receipt["thread_id"] == terminal.get("thread_id") == intent["thread_id"] and
                   receipt["role"] == request.role and receipt["model"] == request.model and
                   receipt["effort"] == request.effort and receipt["inventory_digest"] == request.inventory_digest and
                   receipt["permission_profile"] == ":danger-full-access" and receipt["approval_policy"] == "never" and
                   receipt["output_schema_digest"] == sha256_digest(intent["output_schema"]) and
                   receipt["schema_recovery_attempts"] == 0 and receipt["turn_ids"] == [terminal.get("turn_id")],
    }
    return {"passed": all(checks.values()), "checks": checks, "request_digest": request.request_digest,
            "receipt_digest": sha256_digest(receipt) if receipt else None}


def summarize(run, status, error=None):
    lock = read(run / "preflight.json")
    receipts = {}
    for path in sorted((run / "calls").glob("*/result.json")):
        item = read(path)["receipt"]
        receipts[item["call_id"]] = item
    for path in sorted((run / "calls").glob("*/failed.json")):
        for item in read(path)["receipts"]:
            receipts[item["call_id"]] = item
    binding = read(run / "instruction-binding.json")
    checks = {"source_unchanged": source_manifest_digest(ROOT) == lock["source_manifest_digest"],
              "preserved_originals": preserved_files(run) == lock["original_files"],
              "instructions_unchanged": all(Path(item["path"]).is_file() and
                  sha256_bytes(Path(item["path"]).read_bytes()) == item["content_digest"] for item in binding["sources"]),
              "workspace_unchanged": files(run / "workspace") == files(OLD / "workspace")}
    values = list(receipts.values())
    turns = []
    for path in sorted((run / "calls").glob("*/terminal.json")):
        terminal = read(path)
        payload = terminal["payload"]
        raw_usage = payload.get("usage")
        usage = raw_usage.get("total") if isinstance(raw_usage, dict) else None
        thread = read(path.parent / "thread.receipt.json")["payload"]["thread"]
        turns.append({"thread_id": payload.get("thread_id"), "turn_id": payload.get("turn_id"),
                      "usage_source": payload.get("usage_source"), "usage_scope": payload.get("usage_scope"),
                      "empty_new_thread": thread.get("turns") == [], "usage_total": usage,
                      "provider_duration_ms": payload.get("duration_ms"),
                      "terminal_digest": sha256_bytes(path.read_bytes())})
        receipt = next((item for item in values if item["thread_id"] == payload.get("thread_id")), None)
        verification = completed_call_verification(path.parent, receipt)
        verification_path = path.parent / "binding-verification.json"
        if verification_path.exists():
            checks[f"{path.parent.name}_prior_binding"] = read(verification_path) == verification
        else:
            write_new(verification_path, verification)
        checks[f"{path.parent.name}_binding"] = verification["passed"]
    usage_keys = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                  "output_tokens": "outputTokens", "reasoning_tokens": "reasoningOutputTokens", "total_tokens": "totalTokens"}
    available = len(turns) == len(list((run / "calls").glob("*/turn.intent.json"))) and bool(turns) and all(
        turn["empty_new_thread"] and isinstance(turn["usage_total"], dict) and
        all(key in turn["usage_total"] for key in usage_keys.values()) for turn in turns)
    usage = {key: sum(turn["usage_total"][raw] for turn in turns) if available else None for key, raw in usage_keys.items()}
    usage["latency_ms"] = sum(item["latency_ms"] for item in values)
    usage["provider_duration_ms"] = sum(turn["provider_duration_ms"] for turn in turns) if all(
        turn["provider_duration_ms"] is not None for turn in turns) and turns else None
    usage["reasoning_included_in_output"] = True
    summary = {"status": status if all(checks.values()) else "FAIL", "error": error, "checks": checks,
               "preflight_digest": lock["lock_digest"], "receipts": values,
               "logical_calls": len(list((run / "calls").glob("*"))),
               "provider_turns": len(list((run / "calls").glob("*/turn.intent.json"))),
               "usage": usage, "provider_turn_usage": turns,
               "all_usage_available": available,
               "billed_cost": None, "billed_cost_reason": "provider receipt가 청구 금액을 제공하지 않는다.",
               "generation_assessment": read(run / "generation-assessment.json") if (run / "generation-assessment.json").exists() else None,
               "plan_activated": False, "worker_executed": False, "new_ledger_writes": 0,
               "full_qualification": "NOT_RUN", "cutover": "NO-GO", "observed_at": utc_now()}
    write_new(run / ("generation-pending.json" if summary["status"] == "GENERATION_REVIEW_REQUIRED" else "summary.json"), summary)
    print(json.dumps({key: summary[key] for key in ("status", "logical_calls", "provider_turns", "usage", "error")}, ensure_ascii=False), flush=True)


def verify_generation_pending(run: Path) -> None:
    pending = read(run / "generation-pending.json")
    if (pending.get("status") != "GENERATION_REVIEW_REQUIRED" or not pending.get("checks") or
            not all(pending["checks"].values()) or pending.get("logical_calls") != 12 or
            pending.get("provider_turns") != 12 or
            len(list((run / "calls").glob("*/result.json"))) != 12 or
            len(list((run / "calls").glob("*/turn.intent.json"))) != 12):
        raise RuntimeError("GENERATION_PENDING_FAILED_OR_INCOMPLETE")


def execute(run, generated=False):
    marker = "generated-review-started.json" if generated else "execution-started.json"
    if (run / "summary.json").exists():
        raise RuntimeError("완료·실패한 진단을 반복하지 않습니다.")
    status = "FAIL"
    error = None
    try:
        lock = verify_lock(run)
        write_new(run / marker, {"preflight_digest": lock["lock_digest"], "observed_at": utc_now()})
        roles = EngineRoleConfiguration.model_validate(read(run / "roles.json"))
        # 첫 turn 전 모든 고정 사례의 표 존재·입력 결속을 확인한다.
        for name in STATIC_CASES:
            request = RoleCallRequest.model_validate(read(run / "requests" / f"{name}.json"))
            case_expectation(run, name, request)
        if generated:
            verify_generation_pending(run)
            assessment = read(run / "generation-assessment.json")
            plan = PlanContractRevision.model_validate(read(run / "expanded-plan.json"))
            template_document = read(run / "generated-review-template.json")
            if (assessment["plan_digest"] != plan.activation_digest or not assessment["generation_acceptable"] or
                    assessment.get("preflight_digest") != lock["lock_digest"] or
                    assessment.get("criteria_digest") != sha256_digest(template_document["generation_criteria"])):
                raise RuntimeError("GENERATION_DEFECT_OR_BINDING_FAILURE")
            request = capture_request("expanded-review", run, roles, lock["inventory_digest"])
            template = read(run / "generated-review-template.json")["templates"][request.role]
            for key in ("instructions", "model", "effort"):
                if getattr(request, key) != template[key]:
                    raise RuntimeError("GENERATED_REVIEW_TEMPLATE_CHANGED")
            if strict_json_output_schema(request.output_schema) != template["output_schema"]:
                raise RuntimeError("GENERATED_REVIEW_SCHEMA_CHANGED")
            write_new(run / "requests/expanded-review.json", request)
            generated_expectation = bind_case_expectation(
                case_id="expanded-review", payload=request.payload,
                rows=assessment["ac_validation_rows"], defects=[],
                review_digest=sha256_bytes((run / "generation-assessment.json").read_bytes()),
            )
            # 사전 criteria에 따른 독립 검토자가 실제 생성 입력 digest까지 확인해야 한다.
            if assessment.get("input_binding") != generated_expectation["input_binding"]:
                raise RuntimeError("GENERATED_REVIEW_SEMANTIC_BINDING_MISMATCH")
            write_new(run / "case-expectations/expanded-review.json", generated_expectation)
            write_new(run / "generated-input-lock.json", {"request_digest": request.request_digest,
                      "plan_digest": plan.activation_digest, "assessment_digest": sha256_bytes((run / "generation-assessment.json").read_bytes()),
                      "expectation_digest": generated_expectation["expectation_digest"],
                      "expected_defects": [], "preflight_digest": lock["lock_digest"]})
            names = ("expanded-review",)
        else:
            names = CALL_ORDER[:-1]
        with CapturingRuntime(run=run, codex_bin=Path(lock["codex_bin"])) as runtime:
            runner = RecordedRunner(runtime, run, lock)
            for name in names:
                runner.name = name
                print(json.dumps({"starting_case": name, "maximum_calls": MAXIMUM_CALLS}), flush=True)
                result = invoke(name, runner, run, roles, lock["inventory_digest"])
                if name == "expansion":
                    write_new(run / "expanded-plan.json", result)
                    status = "GENERATION_REVIEW_REQUIRED"
                else:
                    write_new(run / f"{name}-review.json", result)
                    envelope = PlanReviewEnvelope.model_validate(runner.last_result.payload)
                    request = RoleCallRequest.model_validate(read(run / "requests" / f"{name}.json"))
                    assessment = assess_case_inspection_review(
                        envelope, case_expectation(run, name, request), case_id=name, payload=request.payload,
                    )
                    write_new(run / f"{name}-assessment.json", assessment)
                    if not assessment["passed"]:
                        raise RuntimeError(f"SEMANTIC_ASSESSMENT_FAILED: {name}: {json.dumps(assessment, ensure_ascii=False)}")
            if generated:
                status = "PASS"
    except Exception as exc:
        status, error = "FAIL", f"{type(exc).__name__}: {exc}"
    summarize(run, status, error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run", "review-generated"))
    parser.add_argument("--run-root", type=Path, required=True)
    arguments = parser.parse_args()
    destination = arguments.run_root.resolve()
    if not destination.name.startswith(("r-s06-10-", "r-s06-12-", "r-s06-13-", "r-s06-14-", "r-s06-15-", "r-s06-17-")) or destination.parent != OLD.parent:
        raise RuntimeError("새 R-S06 검사 진단 디렉터리만 허용합니다.")
    if arguments.mode == "prepare":
        try:
            prepare(destination)
        except Exception as error:
            write_new(destination / "preparation-failed.json", {
                "status": "FAIL", "error": f"{type(error).__name__}: {error}",
                "provider_turns": len(list((destination / "calls").glob("*/turn.intent.json"))),
                "full_qualification": "NOT_RUN", "cutover": "NO-GO", "observed_at": utc_now(),
            })
            raise
    else:
        execute(destination, generated=arguments.mode == "review-generated")
