"""R-S06 검사 근거 대조표의 제한 실제 진단. 기존 실행·원장·oracle은 변경하지 않는다."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from flowmarshal.canonical import canonical_json, json_value, sha256_bytes, sha256_digest
from flowmarshal.engine.domain import GoalContractRevision, PlanContractRevision, PlanSkeletonCandidate, ProjectMapRevision, StateSnapshot, utc_now
from flowmarshal.engine.models import EngineRoleConfiguration
from flowmarshal.engine.model_lock import LOCK_FORMAT, ModelInventory, OperationalBinding, verify_binding
from flowmarshal.engine.plan_inspection_eval import (
    assess_case_inspection_review, bind_case_expectation, verify_case_expectation,
)
from flowmarshal.engine.planner_roles import PlanExpanderAdapter, PlanReviewerAdapter, PlanReviewEnvelope, RuleBasedTaskAssigner
from flowmarshal.engine.planning import plan_gate, risk_route
from flowmarshal.engine.qualification import PlanningScenarioCatalog, ScopeQualificationReport, _model_lock, _planning_contract, source_manifest_digest, source_manifest_files
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner, RoleCallReceipt, RoleCallRequest, RoleCallResult, StructuredRoleError,
    strict_json_output_schema, verify_role_receipt,
)
from flowmarshal.engine.runtime import CodexAppServerRuntime


ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / ".flowmarshal-engine-eval/runs/r-s06-09-20260904-v1"
S05 = OLD.parent / "s05-bugfix-trace-20260904"
STATIC_CASES = ("clean", "bad", "wrong-goal", "combined", "boundary-clean", "missing-link", "future-result",
                "stored-expanded", "semantic-explicit", "stored-multi-defect", "semantic-missing-link")
CALL_ORDER = (*STATIC_CASES, "expansion", "expanded-review")
MAXIMUM_CALLS = 13
EXECUTION_MODES = ("qualification", "development-diagnostic")
INSPECTION_PROVIDER_CONTRACT = "plan-inspection-v1"
CAPTURE_PHASES = ("prepare", "run", "review-generated")
ROLE_CONFIGURATION_IDS = tuple(EngineRoleConfiguration.model_fields)
ROLE_CONFIGURATION_INPUT_FORMAT = "flowmarshal-role-configuration-input-v1"
REQUEST_ROLE_CONFIGURATION = {
    "plan_expander": "plan_expander",
    "compact_plan_reviewer": "general_reviewer",
    "critical_effect_reviewer": "critical_reviewer",
    "high_risk_reviewer": "critical_reviewer",
    "external_effect_reviewer": "critical_reviewer",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_new_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)
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
    write_new_bytes(destination, source.read_bytes())


def execution_order(lock: dict[str, Any]) -> tuple[str, ...]:
    """독립 진단은 static 사례만 실행하며 기존 qualification 순서는 보존한다."""
    mode = lock.get("execution_mode", "qualification")
    if mode not in EXECUTION_MODES:
        raise RuntimeError("UNKNOWN_EXECUTION_MODE")
    return STATIC_CASES if mode == "development-diagnostic" else CALL_ORDER


def record_case_result(run: Path, name: str, *, status: str, failure_kind: str | None,
                       semantic_evaluated: bool, error: str | None = None) -> None:
    """후속 사례와 무관하게 현재 사례의 판정·평가 도달 여부를 한 번 보존한다."""
    write_new(run / "case-results" / f"{name}.json", {
        "case_id": name, "status": status, "failure_kind": failure_kind,
        "semantic_evaluated": semantic_evaluated, "error": error, "observed_at": utc_now(),
    })


def completed_output_failure(run: Path, name: str, lock: dict[str, Any]) -> str | None:
    """완결 schema 거부와 결속된 provider terminal 실패만 구분한다."""
    collected = collect_call_artifacts(run)
    number = execution_order(lock).index(name) + 1
    calls = [call for call in collected["calls"] if call["capture"].startswith(f"{number:02d}-")]
    if collected["issues"] or len(calls) != 1:
        return None
    call = calls[0]
    if (call["outcome"] != "failure" or not call["common_binding"] or
            call["common_binding"]["passed"] is not True):
        return None
    failure_kind = call.get("failure_kind")
    if failure_kind not in {"model_output", "provider_terminal_failed"}:
        return None
    failed = read(run / "calls" / call["capture"] / "failed.json")
    receipts = failed.get("receipts", [])
    receipt_status = "schema_failed" if failure_kind == "model_output" else "failed"
    return failure_kind if len(receipts) == 1 and receipts[0].get("status") == receipt_status else None


def _strict_json_document(raw_bytes: bytes) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON object key가 중복됐습니다: {key}")
            result[key] = value
        return result

    return json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=unique_object)


def _decode_role_configuration(raw_bytes: bytes) -> tuple[EngineRoleConfiguration, str]:
    try:
        document = _strict_json_document(raw_bytes)
        if not isinstance(document, dict) or set(document) != set(ROLE_CONFIGURATION_IDS):
            raise ValueError(
                "역할 ID 집합이 정확하지 않습니다. expected=" + ",".join(ROLE_CONFIGURATION_IDS)
            )
        roles = EngineRoleConfiguration.model_validate(document)
    except Exception as error:
        raise RuntimeError(f"ROLE_CONFIGURATION_INVALID: {error}") from error
    return roles, sha256_digest(document)


@dataclass(frozen=True)
class RoleConfigurationInput:
    input_path: Path
    raw_bytes: bytes
    roles: EngineRoleConfiguration
    source_canonical_digest: str
    selection_reason: str

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "format": ROLE_CONFIGURATION_INPUT_FORMAT,
            "input_path": str(self.input_path),
            "selection_reason": self.selection_reason,
            "source_bytes_digest": sha256_bytes(self.raw_bytes),
            "source_canonical_digest": self.source_canonical_digest,
            "configuration_digest": self.roles.configuration_digest,
            "role_ids": list(ROLE_CONFIGURATION_IDS),
            "copied_artifact": "roles.json",
        }


def load_role_configuration_input(path: Path | None = None) -> RoleConfigurationInput:
    """prepare mutation 전에 명시적 역할 설정 경로와 원문을 검증한다."""
    explicit = path is not None
    selected = S05 / "roles.json" if path is None else Path(path)
    if explicit and not selected.is_absolute():
        raise RuntimeError("ROLE_CONFIGURATION_PATH_MUST_BE_ABSOLUTE")
    try:
        resolved = selected.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise RuntimeError(f"ROLE_CONFIGURATION_NOT_FOUND: {selected}") from error
    if not resolved.is_file():
        raise RuntimeError(f"ROLE_CONFIGURATION_NOT_FILE: {resolved}")
    raw_bytes = resolved.read_bytes()
    roles, canonical_digest = _decode_role_configuration(raw_bytes)
    return RoleConfigurationInput(
        input_path=resolved,
        raw_bytes=raw_bytes,
        roles=roles,
        source_canonical_digest=canonical_digest,
        selection_reason=("caller_provided_explicit_role_configuration" if explicit
                          else "historical_s05_default_role_configuration"),
    )


def validate_role_configuration_inventory(roles: EngineRoleConfiguration, inventory: ModelInventory) -> None:
    """선택과 fallback의 모든 model/effort 조합을 fresh inventory에 대조한다."""
    unsupported = []
    for role_id in ROLE_CONFIGURATION_IDS:
        binding = roles.binding_for(role_id)
        choices = (("selected", binding),) + tuple(
            (f"fallback[{index}]", fallback)
            for index, fallback in enumerate(binding.allowed_fallbacks)
        )
        for envelope_name, choice in choices:
            if not inventory.supports(choice.model, choice.effort):
                unsupported.append(f"{role_id}:{envelope_name}:{choice.model}/{choice.effort}")
    if unsupported:
        raise RuntimeError("ROLE_CONFIGURATION_MODEL_EFFORT_UNSUPPORTED: " + ", ".join(unsupported))


def verify_role_configuration_artifacts(run: Path, binding: dict[str, Any]) -> EngineRoleConfiguration:
    """preflight에 결속한 외부 원문과 run 복사본이 모두 같은지 확인한다."""
    if binding.get("format") != ROLE_CONFIGURATION_INPUT_FORMAT:
        raise RuntimeError("ROLE_CONFIGURATION_INPUT_FORMAT_UNSUPPORTED")
    if binding.get("role_ids") != list(ROLE_CONFIGURATION_IDS) or binding.get("copied_artifact") != "roles.json":
        raise RuntimeError("ROLE_CONFIGURATION_INPUT_SCHEMA_MISMATCH")
    source = Path(binding.get("input_path", ""))
    if not source.is_absolute() or not source.is_file():
        raise RuntimeError("ROLE_CONFIGURATION_SOURCE_UNAVAILABLE")
    source_bytes = source.read_bytes()
    source_roles, source_canonical_digest = _decode_role_configuration(source_bytes)
    if (sha256_bytes(source_bytes) != binding.get("source_bytes_digest") or
            source_canonical_digest != binding.get("source_canonical_digest") or
            source_roles.configuration_digest != binding.get("configuration_digest")):
        raise RuntimeError("ROLE_CONFIGURATION_SOURCE_CHANGED")
    copied = run / binding["copied_artifact"]
    if not copied.is_file() or copied.read_bytes() != source_bytes:
        raise RuntimeError("ROLE_CONFIGURATION_COPY_CHANGED")
    copied_roles, copied_canonical_digest = _decode_role_configuration(copied.read_bytes())
    if (copied_canonical_digest != binding["source_canonical_digest"] or
            copied_roles.configuration_digest != binding["configuration_digest"]):
        raise RuntimeError("ROLE_CONFIGURATION_COPY_BINDING_MISMATCH")
    return copied_roles


def verify_role_request_binding(
    request: RoleCallRequest,
    roles: EngineRoleConfiguration,
    inventory: ModelInventory,
) -> None:
    """실제 호출 request를 역할 설정과 그 request의 v2 lock에 동시에 대조한다."""
    role_id = REQUEST_ROLE_CONFIGURATION.get(request.role)
    if role_id is None:
        raise RuntimeError(f"ROLE_CONFIGURATION_REQUEST_ROLE_UNKNOWN: {request.role}")
    configured = roles.binding_for(role_id)
    if (request.model, request.effort) != (configured.model, configured.effort):
        raise RuntimeError("ROLE_CONFIGURATION_REQUEST_MISMATCH")
    try:
        request = RoleCallRequest.model_validate(request.model_dump(mode="python"))
        verify_binding(
            request.operational_binding,
            inventory,
            role=request.role,
            model=request.model,
            effort=request.effort,
        )
    except ValueError as error:
        raise RuntimeError(f"ROLE_CONFIGURATION_REQUEST_LOCK_MISMATCH: {error}") from error
    locked_role = next(item for item in request.operational_binding.lock.roles if item.role == request.role)
    if tuple((item.model, item.effort) for item in locked_role.allowed_fallbacks) != tuple(
        (item.model, item.effort) for item in configured.allowed_fallbacks
    ):
        raise RuntimeError("ROLE_CONFIGURATION_REQUEST_FALLBACK_MISMATCH")


def instruction_binding(run: Path, *, observed_sources: list[str] | None = None,
                        probe_receipt: Path | None = None) -> dict[str, Any]:
    """실제 주입 경로와 본문을 잠근다. 기존 run의 진입 방식은 그대로 보존한다."""
    receipt = probe_receipt or OLD / "calls/01-compact_plan_reviewer/thread.receipt.json"
    paths = (read(receipt)["payload"]["instructionSources"] if observed_sources is None
             else observed_sources)
    if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
        raise RuntimeError("ACTUAL_INSTRUCTION_SOURCES_UNAVAILABLE")
    sources = []
    for index, raw in enumerate(paths):
        path = Path(raw)
        if observed_sources is None and path.resolve().is_relative_to((OLD / "workspace").resolve()):
            path = run / "workspace" / path.relative_to(OLD / "workspace")
        body = path.read_bytes()
        snapshot = f"instruction-sources/{index:02d}-AGENTS.md"
        copy_new(path, run / snapshot)
        sources.append({"path": str(path.resolve()), "content_digest": sha256_bytes(body), "snapshot": snapshot})
    return {"sources": sources, "prior_receipt_digest": sha256_bytes(receipt.read_bytes()),
            "source_observation": "current_ephemeral_thread_probe" if observed_sources is not None else "historical_thread",
            "verification": "현재 thread/start receipt의 instructionSources와 turn 전 정확한 경로·본문 digest를 대조한다."}


def portable_preflight(run: Path, *, fixture_package: Path, codex_bin: Path,
                       role_configuration: RoleConfigurationInput, execution_mode: str) -> dict[str, Any]:
    """모델 호출·fixture 생성 전에 고정 checkout과 외부 입력 경로를 검사한다."""
    from scripts.diagnostics.inspection_inputs import verify_fixture_package
    from scripts.diagnostics.inspection_workspace import capture_workspace_binding
    if execution_mode not in EXECUTION_MODES:
        raise RuntimeError("UNKNOWN_EXECUTION_MODE")
    if not fixture_package.is_absolute() or not codex_bin.is_absolute():
        raise RuntimeError("PORTABLE_INPUT_PATH_MUST_BE_ABSOLUTE")
    if role_configuration.selection_reason != "caller_provided_explicit_role_configuration":
        raise RuntimeError("PORTABLE_ROLE_CONFIGURATION_MUST_BE_EXPLICIT")
    executable = codex_bin.resolve(strict=True)
    body = {
        "format": "flowmarshal-inspection-preflight-v1", "execution_mode": execution_mode,
        "inspection_provider_contract": INSPECTION_PROVIDER_CONTRACT,
        "workspace_binding": capture_workspace_binding(ROOT),
        "fixture_package_binding": verify_fixture_package(fixture_package),
        "codex_bin": str(executable), "codex_bin_digest": sha256_bytes(executable.read_bytes()),
        "role_configuration_input": role_configuration.binding,
    }
    write_new(run / "workspace-preflight.json", body | {"binding_digest": sha256_digest(body)})
    return body


def verify_portable_inputs(lock: dict[str, Any]) -> None:
    """다른 checkout의 이동은 무시하고 이 실행에 결속한 입력만 재검사한다."""
    from scripts.diagnostics.inspection_inputs import verify_fixture_package
    from scripts.diagnostics.inspection_workspace import verify_workspace_binding
    verify_workspace_binding(ROOT, lock["workspace_binding"])
    package = lock["fixture_package_binding"]
    if verify_fixture_package(Path(package["package"])) != package:
        raise RuntimeError("FIXTURE_PACKAGE_BINDING_CHANGED")
    if sha256_bytes(Path(lock["codex_bin"]).read_bytes()) != lock["codex_bin_digest"]:
        raise RuntimeError("CODEX_EXECUTABLE_CHANGED")


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
    limit = (read(run / "preflight.json").get("maximum_provider_turns", MAXIMUM_CALLS)
             if (run / "preflight.json").exists() else MAXIMUM_CALLS)
    if len(list((run / "calls").glob("*/turn.intent.json"))) >= limit:
        raise RuntimeError("MAXIMUM_PROVIDER_CALLS_EXCEEDED")
    write_new(capture / "turn.intent.json", intent)


def claim_runtime_phase(run: Path, phase: str) -> Path:
    """단계별 preflight 기록을 독점해 기존 raw 관측을 덮어쓰지 않는다."""
    if phase not in CAPTURE_PHASES:
        raise RuntimeError(f"지원하지 않는 runtime preflight phase입니다: {phase}")
    capture = run / "runtime-preflight" / phase
    write_new(capture / "phase-claim.json", {"phase": phase})
    return capture


class CapturingRuntime(CodexAppServerRuntime):
    def __init__(self, *, run: Path, phase: str, **kwargs):
        self.run = run
        self.phase = phase
        self.capture = claim_runtime_phase(run, phase)
        self.indices = {}
        super().__init__(**kwargs)

    def record(self, kind, value):
        key = (str(self.capture), kind)
        number = self.indices.get(key, 0) + 1
        write_new(self.capture / f"{kind}-{number:02d}.json", value)
        self.indices[key] = number

    @contextmanager
    def call_capture(self, capture: Path):
        if not capture.resolve().is_relative_to((self.run / "calls").resolve()):
            raise RuntimeError("진단 call capture가 호출 경로 밖입니다.")
        previous = self.capture
        self.capture = capture
        try:
            yield
        finally:
            self.capture = previous

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
    inventory = inventory_digest if isinstance(inventory_digest, ModelInventory) else None
    return {"model": role.model, "effort": role.effort,
            "inventory_digest": inventory.inventory_digest if inventory is not None else inventory_digest,
            "inventory": inventory, "allowed_fallbacks": role.allowed_fallbacks, "cwd": workspace}


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
                                   critical_model=roles.critical_reviewer.model, critical_effort=roles.critical_reviewer.effort,
                                   critical_allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks)
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
    if lock.get("model_lock_format") != LOCK_FORMAT:
        raise RuntimeError("MODEL_LOCK_VERSION_UNSUPPORTED: v1 preflight를 v2로 재사용할 수 없습니다.")
    operational = OperationalBinding.model_validate(lock["operational_binding"])
    if operational.lock_digest != lock["model_lock_digest"] or operational.inventory_digest != lock["inventory_digest"]:
        raise RuntimeError("MODEL_LOCK_EVIDENCE_DIGEST_MISMATCH")
    body = dict(lock)
    digest = body.pop("lock_digest")
    if sha256_digest(body) != digest:
        raise RuntimeError("PREFLIGHT_LOCK_CHANGED")
    if source_manifest_digest(ROOT) != lock["source_manifest_digest"]:
        raise RuntimeError("SOURCE_LOCK_CHANGED")
    if lock.get("inspection_provider_contract", "plan-inspection-v1") != INSPECTION_PROVIDER_CONTRACT:
        raise RuntimeError("INSPECTION_PROVIDER_CONTRACT_CHANGED")
    if "workspace_binding" in lock:
        verify_portable_inputs(lock)
    roles = verify_role_configuration_artifacts(run, lock.get("role_configuration_input", {}))
    planning_binding = read(run / "planning-binding.json")
    if (planning_binding.get("role_configuration_input") != lock["role_configuration_input"] or
            planning_binding.get("role_configuration_digest") != roles.configuration_digest or
            lock.get("role_configuration_digest") != roles.configuration_digest):
        raise RuntimeError("ROLE_CONFIGURATION_PLANNING_BINDING_MISMATCH")
    if "workspace_binding" not in lock and preserved_files(run) != lock["original_files"]:
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
    expected_order = execution_order(lock)
    if lock["call_order"] != list(expected_order) or any(lock[key] != len(expected_order) for key in
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


def prepare(run, role_configuration: RoleConfigurationInput | None = None, *, execution_mode="qualification",
            fixture_package: Path | None = None, codex_bin: Path | None = None):
    if execution_mode not in EXECUTION_MODES:
        raise RuntimeError("UNKNOWN_EXECUTION_MODE")
    call_order = execution_order({"execution_mode": execution_mode})
    role_configuration = role_configuration or load_role_configuration_input()
    if (run / "preflight.json").exists() or (run / "calls").exists():
        raise RuntimeError("이미 잠그거나 실행한 진단은 반복하지 않습니다.")
    incomplete = incomplete_provider_intents(run)
    if incomplete:
        raise RuntimeError("INCOMPLETE_PROVIDER_INTENT_EXISTS: " + ", ".join(map(str, incomplete)))
    portable = fixture_package is not None
    if portable != (codex_bin is not None):
        raise RuntimeError("PORTABLE_PACKAGE_AND_EXECUTABLE_REQUIRED_TOGETHER")
    isolation = None
    if portable:
        isolation = read(run / "workspace-preflight.json")
        isolation_body = dict(isolation)
        if isolation_body.pop("binding_digest") != sha256_digest(isolation_body):
            raise RuntimeError("WORKSPACE_PREFLIGHT_CHANGED")
        verify_portable_inputs(isolation)
        if (isolation["execution_mode"] != execution_mode or
                isolation["inspection_provider_contract"] != INSPECTION_PROVIDER_CONTRACT or
                isolation["role_configuration_input"] != role_configuration.binding or
                isolation["codex_bin"] != str(codex_bin.resolve(strict=True)) or
                isolation["fixture_package_binding"]["package"] != str(fixture_package.resolve(strict=True))):
            raise RuntimeError("WORKSPACE_PREFLIGHT_INPUT_CHANGED")
    report = ScopeQualificationReport.model_validate(read(run / "deterministic/qualification-report.json"))
    source = source_manifest_digest(ROOT)
    if not report.passed or read(run / "deterministic/evaluation-contract.json")["source_manifest_digest"] != source:
        raise RuntimeError("DETERMINISTIC_GATE_FAILED_OR_STALE")
    from scripts.diagnostics.r_s06_10_fixtures import build_revision, verify_reviewed_case
    relocation = None
    if portable:
        from scripts.diagnostics.inspection_materialization import prepare_relocated_inputs
        relocation = prepare_relocated_inputs(fixture_package, run)
    else:
        build_revision(OLD, run)
    review = read(run / "independent-fixture-review.json")
    if (review.get("review_complete") is not True or review.get("reviewed_cases") != list(STATIC_CASES) or
            review.get("expectations_digest") != sha256_bytes((run / "expectations.json").read_bytes())):
        raise RuntimeError("INDEPENDENT_FIXTURE_REVIEW_MISSING_OR_STALE")
    if not portable:
        for name in files(OLD / "workspace"):
            copy_new(OLD / "workspace" / name, run / "workspace" / name)
    write_new_bytes(run / "roles.json", role_configuration.raw_bytes)
    roles = role_configuration.roles
    old_lock = isolation if portable else read(S05 / "input-lock.json")
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
    with CapturingRuntime(run=run, phase="prepare", codex_bin=Path(old_lock["codex_bin"])) as runtime:
        policy = runtime.verify_execution_policy(run / "workspace")
        inventory = runtime.list_models()
        validate_role_configuration_inventory(roles, inventory)
        if policy.permission_profile != ":danger-full-access" or policy.approval_policy != "never":
            raise RuntimeError("PERMISSION_POLICY_MISMATCH")
        # v1은 historical provenance다. 새 run에서 명시적 v2 운영 계약을 만든다.
        if runtime.executable_digest != old_lock["codex_bin_digest"]:
            raise RuntimeError("MODEL_OR_EXECUTABLE_LOCK_CHANGED")
        operational = roles.operational_binding(inventory)
        if portable:
            # 모델 turn을 시작하지 않는 새 ephemeral thread에서 실제 주입 경로만 관측한다.
            probe_arguments = {"cwd": run / "workspace", "title": "검사 입력 지침 관측",
                               "model": roles.general_reviewer.model, "developer_instructions": "",
                               "ephemeral": True}
            write_new(runtime.capture / "instructions-probe.intent.json", probe_arguments)
            probe = CodexAppServerRuntime.create_thread(runtime, **probe_arguments)
            probe_path = runtime.capture / "instructions-probe.receipt.json"
            write_new(probe_path, probe)
            if probe.payload.get("thread", {}).get("turns") != []:
                raise RuntimeError("INSTRUCTION_PROBE_REQUIRES_EMPTY_THREAD")
            write_new(run / "instruction-binding.json", instruction_binding(
                run, observed_sources=probe.payload.get("instructionSources", []), probe_receipt=probe_path))
        for name in (call_order if execution_mode == "development-diagnostic" else call_order[:-1]):
            request = capture_request(name, run, roles, inventory)
            verify_role_request_binding(request, roles, inventory)
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
        write_new(run / "planning-binding.json", json_value(contract) | {
            "role_configuration_input": role_configuration.binding,
        })
        templates = {}
        base = capture_request("clean", run, roles, inventory)
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
        if not portable:
            write_new(run / "instruction-binding.json", instruction_binding(run))
        source_files = source_manifest_files(ROOT)
        for name in source_files:
            copy_new(ROOT / name, run / "executed-source" / name)
        write_new(run / "executed-source-manifest.json", {"files": source_files, "source_manifest_digest": source})
        verify_role_configuration_artifacts(run, role_configuration.binding)
        locked = locked_input_files(run)
        body = {"session": "R-S06-19", "source_manifest_digest": source, "locked_files": locked,
                "harness_digest": sha256_bytes(Path(__file__).read_bytes()),
                "original_files": {} if portable else preserved_files(run),
                "base_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "policy": policy, "model_lock_format": LOCK_FORMAT, "operational_binding": operational,
                "historical_model_lock_digest": None if portable else old_lock["model_lock_digest"],
                "inventory_digest": inventory.inventory_digest, "model_lock_digest": _model_lock(inventory, roles),
                "codex_bin": old_lock["codex_bin"], "codex_bin_digest": runtime.executable_digest,
                "role_configuration_digest": roles.configuration_digest,
                "role_configuration_input": role_configuration.binding, "execution_mode": execution_mode,
                "inspection_provider_contract": INSPECTION_PROVIDER_CONTRACT,
                "call_order": call_order,
                "maximum_logical_calls": len(call_order), "maximum_provider_turns": len(call_order), "schema_recovery_attempts": 0,
                "prompt_digest": contract.prompt_digest, "output_schema_digest": contract.output_schema_digest,
                "deterministic_report_digest": report.report_digest, "observed_at": utc_now(),
                "scope": "제한 진단. 전체 S06·qualification·Plan 활성화·Worker 실행·1.0 cutover는 수행하지 않는다."}
        if portable:
            body.update({"workspace_binding": isolation["workspace_binding"],
                         "fixture_package_binding": isolation["fixture_package_binding"],
                         "workspace_files": relocation["workspace_files"],
                         "relocation_binding": relocation,
                         "workspace_preflight_digest": isolation["binding_digest"]})
        body = json_value(body)
        write_new(run / "preflight.json", body | {"lock_digest": sha256_digest(body)})
    print(json.dumps({"prepared": True, "maximum_calls": len(call_order), "lock_digest": sha256_digest(body)}), flush=True)


class RecordedRunner:
    def __init__(self, runtime, run, lock):
        self.runtime, self.run_root, self.lock = runtime, run, lock
        self.runner = CodexStructuredRoleRunner(runtime, max_schema_recovery_attempts=0,
                    operational_binding=(OperationalBinding.model_validate(lock["operational_binding"])
                                         if "operational_binding" in lock else None))
        self.name = None
        self.last_result = None

    def run(self, request, *, validator=None):
        verify_lock(self.run_root)
        expected = RoleCallRequest.model_validate(read(self.run_root / "requests" / f"{self.name}.json"))
        if request.request_digest != expected.request_digest:
            raise RuntimeError("ACTUAL_REQUEST_BINDING_MISMATCH")
        if self.name != "expansion":
            case_expectation(self.run_root, self.name, request)
        baseline = OperationalBinding.model_validate(self.lock["operational_binding"])
        current_inventory = self.runtime.list_models()
        verify_binding(baseline, current_inventory)
        roles = EngineRoleConfiguration.model_validate(read(self.run_root / "roles.json"))
        validate_role_configuration_inventory(roles, current_inventory)
        if roles.operational_binding(current_inventory).lock_digest != baseline.lock_digest:
            raise RuntimeError("MODEL_OR_EXECUTABLE_LOCK_CHANGED")
        verify_role_request_binding(request, roles, current_inventory)
        actual_schema = strict_json_output_schema(request.output_schema)
        expected_schema = (read(self.run_root / "schemas" / f"{self.name}.json") if self.name != "expanded-review" else
                           read(self.run_root / "generated-review-template.json")["templates"][request.role]["output_schema"])
        if actual_schema != expected_schema:
            raise RuntimeError("ACTUAL_SCHEMA_BINDING_MISMATCH")
        number = execution_order(self.lock).index(self.name) + 1
        capture = self.run_root / "calls" / f"{number:02d}-{request.role}"
        capture.mkdir(parents=True, exist_ok=False)
        with self.runtime.call_capture(capture):
            write_new(capture / "request.json", request)
            write_new(capture / "strict-schema.json", actual_schema)
            try:
                result = self.runner.run(request, validator=validator)
            except Exception as error:
                write_new(capture / "failed.json", {"error_type": type(error).__name__, "error": str(error),
                                                  "receipts": getattr(error, "receipts", ()), "observed_at": utc_now()})
                raise
            verify_role_receipt(request, result)
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


def common_call_verification(
        capture: Path, receipt: dict[str, Any] | None, *,
        terminal_statuses: frozenset[str] | None = None,
        receipt_statuses: frozenset[str] | None = None,
) -> dict[str, Any]:
    """성공 payload와 무관한 request·provider 효과·receipt 결속을 확인한다."""
    request = RoleCallRequest.model_validate(read(capture / "request.json"))
    terminal_record = read(capture / "terminal.json")
    terminal = terminal_record["payload"]
    thread = read(capture / "thread.receipt.json")["payload"]["thread"]
    intent = read(capture / "turn.intent.json")
    turn_receipt = read(capture / "turn.receipt.json")
    strict_artifact = read(capture / "strict-schema.json")
    receipt_value = (json_value(RoleCallReceipt.model_validate(receipt))
                     if isinstance(receipt, dict) else {})
    observation_valid = False
    if receipt_value and request.operational_binding is not None:
        try:
            observed = OperationalBinding.model_validate(receipt_value["observed_binding"])
            observation_valid = verify_binding(
                request.operational_binding, observed.inventory,
                role=request.role, model=request.model, effort=request.effort,
            ) == observed
        except (ValueError, RuntimeError, KeyError, TypeError):
            pass
    usage_total = _terminal_usage(terminal_record)
    if receipt_value.get("usage_available") is True:
        receipt_usage_valid = isinstance(usage_total, dict) and all(
            usage_total.get(raw) == receipt_value.get(field) for field, raw in (
                ("input_tokens", "inputTokens"), ("cached_input_tokens", "cachedInputTokens"),
                ("output_tokens", "outputTokens"), ("reasoning_tokens", "reasoningOutputTokens"),
            )
        )
    else:
        receipt_usage_valid = receipt_value.get("usage_available") is False and usage_total is None
    accepted_terminal_statuses = (frozenset({"completed", "success", "succeeded"})
                                  if terminal_statuses is None else terminal_statuses)
    checks = {
        "request_strict_artifact": strict_artifact == strict_json_output_schema(request.output_schema),
        "strict_artifact_turn_intent": strict_artifact == intent["output_schema"],
        "strict_artifact_receipt_digest": receipt_value.get("output_schema_digest") == sha256_digest(strict_artifact),
        "prompt_instruction": json.loads(intent["prompt"]) == request.payload and
                              terminal.get("prompt_digest") == sha256_digest(intent["prompt"]) and
                              read(capture / "thread.intent.json")["developer_instructions"] == request.instructions,
        "receipt_request_identity": receipt_value.get("input_digest") == request.request_digest and
                                    receipt_value.get("role") == request.role and
                                    receipt_value.get("inventory_digest") == request.inventory_digest and
                                    receipt_value.get("permission_profile") == ":danger-full-access" and
                                    receipt_value.get("approval_policy") == "never" and
                                    receipt_value.get("schema_recovery_attempts") == 0,
        "model_effort": intent["model"] == request.model and intent["effort"] == request.effort and
                        receipt_value.get("model") == request.model and receipt_value.get("effort") == request.effort,
        "thread_turn": intent["thread_id"] == thread["id"] == terminal.get("thread_id") and
                       terminal.get("turn_id") == turn_receipt["operation_id"] and
                       receipt_value.get("thread_id") == intent["thread_id"] and
                       receipt_value.get("turn_ids") == [terminal.get("turn_id")],
        "terminal_completed": terminal_record.get("active") is False and
                              terminal_record.get("terminal_status") in accepted_terminal_statuses,
        "receipt_terminal_usage": receipt_usage_valid,
        "model_observation": observation_valid,
    }
    if receipt_statuses is not None:
        checks["receipt_status"] = receipt_value.get("status") in receipt_statuses
    return {"passed": all(checks.values()), "checks": checks, "request_digest": request.request_digest,
            "receipt_digest": sha256_digest(receipt_value) if receipt_value else None}


def completed_call_verification(capture: Path, receipt: dict[str, Any] | None) -> dict[str, Any]:
    """정상 성공에만 output 결속을 추가한다. 실패 요약에서는 호출하지 않는다."""
    common = common_call_verification(capture, receipt)
    result = RoleCallResult.model_validate(read(capture / "result.json"))
    receipt_value = receipt if isinstance(receipt, dict) else {}
    terminal_record = read(capture / "terminal.json")
    try:
        terminal_payload = json.loads(terminal_record["final_response"])
    except (KeyError, TypeError, json.JSONDecodeError):
        terminal_payload = None
    result_receipt = json_value(result.receipt)
    canonical_receipt = json_value(RoleCallReceipt.model_validate(receipt_value)) if receipt_value else None
    checks = dict(common["checks"])
    checks["terminal_result_output_digest"] = (
        receipt_value.get("status") == "succeeded" and
        terminal_payload == result.payload and
        receipt_value.get("output_digest") == sha256_digest(result.payload) and
        result_receipt == canonical_receipt
    )
    return {"passed": all(checks.values()), "checks": checks, "request_digest": common["request_digest"],
            "receipt_digest": common["receipt_digest"]}


CALL_ARTIFACT_NAMES = (
    "request.json", "strict-schema.json", "thread.intent.json", "thread.receipt.json",
    "turn.intent.json", "turn.receipt.json", "terminal.json", "result.json", "failed.json",
    "binding-verification.json",
)


def _artifact(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """존재와 JSON object 파싱을 분리해 손상 artifact도 요약 가능한 관측으로 만든다."""
    if not path.exists():
        return {"exists": False, "parse_status": "missing", "digest": None, "error": None}, None
    digest = sha256_bytes(path.read_bytes())
    try:
        value = read(path)
        if not isinstance(value, dict):
            raise TypeError("JSON object가 아닙니다.")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        return {"exists": True, "parse_status": "malformed", "digest": digest,
                "error": f"{type(error).__name__}: {error}"}, None
    return {"exists": True, "parse_status": "parsed", "digest": digest, "error": None}, value


def _issue(issues: list[dict[str, Any]], code: str, capture: str, artifact: str, detail: str) -> None:
    issues.append({"code": code, "capture": capture, "artifact": artifact, "detail": detail})


def _receipt_document(raw: Any, *, capture: str, artifact: str,
                      issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _issue(issues, "PARTIAL_RECEIPT", capture, artifact, "receipt가 JSON object가 아닙니다.")
        return None
    # canonical JSON은 값이 None인 nullable 필드를 생략한다. 수치·bool default는
    # 누락을 0/false로 보정하지 않도록 반드시 원문에 있어야 한다.
    missing = sorted((set(RoleCallReceipt.model_fields) - {"output_digest", "error_summary"}) - set(raw))
    if missing:
        _issue(issues, "PARTIAL_RECEIPT", capture, artifact, "누락 필드: " + ", ".join(missing))
        return None
    if raw.get("status") == "succeeded" and not isinstance(raw.get("output_digest"), str):
        _issue(issues, "PARTIAL_RECEIPT", capture, artifact, "성공 receipt의 output_digest가 없습니다.")
        return None
    if raw.get("status") != "succeeded" and not isinstance(raw.get("error_summary"), str):
        _issue(issues, "PARTIAL_RECEIPT", capture, artifact, "실패 receipt의 error_summary가 없습니다.")
        return None
    try:
        return json_value(RoleCallReceipt.model_validate(raw))
    except (TypeError, ValueError) as error:
        _issue(issues, "MALFORMED_RECEIPT", capture, artifact, f"{type(error).__name__}: {error}")
        return None


def _terminal_usage(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        return None
    usage = record["payload"].get("usage")
    return usage.get("total") if isinstance(usage, dict) and isinstance(usage.get("total"), dict) else None


def collect_call_artifacts(run: Path) -> dict[str, Any]:
    """저장 artifact만 읽어 호출별 결과·효과·receipt 귀속을 결정적으로 계산한다."""
    issues: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    occurrences: list[tuple[str, str, dict[str, Any]]] = []
    call_root = run / "calls"
    capture_paths = sorted(path for path in call_root.glob("*") if path.is_dir()) if call_root.exists() else []
    for capture in capture_paths:
        states: dict[str, Any] = {}
        documents: dict[str, Any] = {}
        for name in CALL_ARTIFACT_NAMES:
            state, document = _artifact(capture / name)
            states[name] = state
            documents[name] = document
            if state["parse_status"] == "malformed":
                _issue(issues, "MALFORMED_ARTIFACT", capture.name, name, state["error"])
        result = documents["result.json"]
        if result is not None:
            receipt = _receipt_document(result.get("receipt"), capture=capture.name,
                                        artifact="result.json/receipt", issues=issues)
            if receipt is not None:
                occurrences.append((capture.name, "result.json", receipt))
        failed = documents["failed.json"]
        if failed is not None:
            missing_failure = [key for key in ("error_type", "error", "receipts") if key not in failed]
            if missing_failure:
                _issue(issues, "PARTIAL_FAILED_ARTIFACT", capture.name, "failed.json",
                       "누락 필드: " + ", ".join(missing_failure))
            raw_receipts = failed.get("receipts")
            if not isinstance(raw_receipts, list):
                _issue(issues, "PARTIAL_FAILED_ARTIFACT", capture.name, "failed.json/receipts",
                       "receipts 목록이 없습니다.")
            else:
                for index, raw in enumerate(raw_receipts):
                    receipt = _receipt_document(raw, capture=capture.name,
                                                artifact=f"failed.json/receipts/{index}", issues=issues)
                    if receipt is not None:
                        occurrences.append((capture.name, f"failed.json/receipts/{index}", receipt))
        terminal = documents["terminal.json"]
        if terminal is not None:
            missing_terminal = [key for key in ("active", "terminal_status", "payload") if key not in terminal]
            payload = terminal.get("payload")
            if isinstance(payload, dict):
                missing_terminal.extend(f"payload.{key}" for key in ("thread_id", "turn_id", "prompt_digest")
                                        if key not in payload)
            if missing_terminal:
                _issue(issues, "PARTIAL_TERMINAL", capture.name, "terminal.json",
                       "누락 필드: " + ", ".join(missing_terminal))
        captures.append({"name": capture.name, "path": capture, "artifacts": states, "documents": documents})

    receipts_by_id: dict[str, dict[str, Any]] = {}
    receipt_sources: dict[str, list[dict[str, str]]] = {}
    for capture_name, source, receipt in occurrences:
        call_id = receipt["call_id"]
        existing = receipts_by_id.get(call_id)
        if existing is not None and existing != receipt:
            _issue(issues, "DUPLICATE_CALL_ID_CONFLICT", capture_name, source,
                   f"동일 call_id {call_id}의 내용이 기존 receipt와 다릅니다.")
            continue
        receipts_by_id.setdefault(call_id, receipt)
        receipt_sources.setdefault(call_id, []).append({"capture": capture_name, "artifact": source})

    records: list[dict[str, Any]] = []
    owners: dict[str, list[str]] = {}
    turns: list[dict[str, Any]] = []
    for entry in captures:
        name, documents, states = entry["name"], entry["documents"], entry["artifacts"]
        request = documents["request.json"]
        request_value = None
        if request is not None:
            try:
                request_value = RoleCallRequest.model_validate(request)
            except (TypeError, ValueError) as error:
                _issue(issues, "MALFORMED_REQUEST", name, "request.json", f"{type(error).__name__}: {error}")
        thread_receipt = documents["thread.receipt.json"]
        thread = thread_receipt.get("payload", {}).get("thread") if isinstance(thread_receipt, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        turn_receipt = documents["turn.receipt.json"]
        turn_id = turn_receipt.get("operation_id") if isinstance(turn_receipt, dict) else None
        matching = []
        for call_id, receipt in receipts_by_id.items():
            if request_value is not None and receipt.get("input_digest") != request_value.request_digest:
                continue
            if thread_id is not None and receipt.get("thread_id") != thread_id:
                continue
            if turn_id is not None and turn_id not in receipt.get("turn_ids", []):
                continue
            if request_value is not None and (receipt.get("role"), receipt.get("model"), receipt.get("effort")) != (
                    request_value.role, request_value.model, request_value.effort):
                continue
            matching.append(receipt)
        attributed = matching[0] if len(matching) == 1 else None
        if len(matching) > 1:
            _issue(issues, "CALL_RECEIPT_ATTRIBUTION_AMBIGUOUS", name, "receipt",
                   "여러 receipt가 동일 capture에 결속됩니다.")
        if attributed is not None:
            owners.setdefault(attributed["call_id"], []).append(name)

        terminal = documents["terminal.json"]
        terminal_complete = bool(
            isinstance(terminal, dict) and terminal.get("active") is False and
            terminal.get("terminal_status") in {"completed", "success", "succeeded"}
        )
        terminal_provider_failed = bool(
            isinstance(terminal, dict) and terminal.get("active") is False and
            terminal.get("terminal_status") in {"failed", "error", "systemError", "system_error", "interrupted"}
        )
        failed = documents["failed.json"]
        result = documents["result.json"]
        output_binding: dict[str, Any]
        common_binding: dict[str, Any] | None = None
        failure_kind: str | None = None
        has_malformed = any(state["parse_status"] == "malformed" for state in states.values())
        if (states["terminal.json"]["parse_status"] == "missing" and
                (states["thread.intent.json"]["exists"] or states["turn.intent.json"]["exists"])):
            outcome = "external_unknown"
            output_binding = {"status": "NOT_EVALUATED", "passed": None,
                              "reason": "terminal 관측이 없어 provider 효과의 완료 여부를 알 수 없습니다."}
        elif has_malformed or request_value is None or attributed is None:
            outcome = "incomplete"
            output_binding = {"status": "NOT_EVALUATED", "passed": None,
                              "reason": "artifact가 malformed/partial이거나 receipt 귀속이 불완전합니다."}
        elif (failed is not None and result is None and attributed.get("status") != "succeeded" and
              (terminal_complete or terminal_provider_failed)):
            outcome = "failure"
            output_binding = {"status": "NOT_APPLICABLE", "passed": None,
                              "reason": "역할 실패에는 성공 전용 output payload 결속을 적용하지 않습니다."}
            try:
                if terminal_provider_failed:
                    common_binding = common_call_verification(
                        entry["path"], attributed,
                        terminal_statuses=frozenset({"failed", "error", "systemError", "system_error", "interrupted"}),
                        receipt_statuses=frozenset({"failed"}),
                    )
                else:
                    common_binding = common_call_verification(entry["path"], attributed)
                if not common_binding["passed"]:
                    _issue(issues, "COMMON_BINDING_FAILED", name, "binding",
                           "실패 artifact의 공통 request/provider/receipt 결속이 일치하지 않습니다.")
                    if terminal_provider_failed:
                        outcome = "external_unknown"
                elif terminal_provider_failed:
                    failure_kind = "provider_terminal_failed"
                elif attributed.get("status") == "schema_failed":
                    failure_kind = "model_output"
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                _issue(issues, "COMMON_BINDING_ERROR", name, "binding", f"{type(error).__name__}: {error}")
                if terminal_provider_failed:
                    outcome = "external_unknown"
        elif failed is not None and result is None and attributed.get("status") != "succeeded":
            outcome = "external_unknown"
            output_binding = {"status": "NOT_EVALUATED", "passed": None,
                              "reason": "실패 receipt에 대응하는 terminal이 active이거나 알려진 종료 상태가 아닙니다."}
        elif result is not None and attributed.get("status") == "succeeded" and terminal_complete:
            outcome = "success"
            try:
                verification = completed_call_verification(entry["path"], attributed)
                common_binding = {"passed": all(value for key, value in verification["checks"].items()
                                                if key != "terminal_result_output_digest"),
                                  "checks": {key: value for key, value in verification["checks"].items()
                                             if key != "terminal_result_output_digest"},
                                  "request_digest": verification["request_digest"],
                                  "receipt_digest": verification["receipt_digest"]}
                output_binding = {"status": "APPLICABLE",
                                  "passed": verification["checks"]["terminal_result_output_digest"],
                                  "reason": None}
                if not verification["passed"]:
                    _issue(issues, "SUCCESS_BINDING_FAILED", name, "binding",
                           "성공 artifact의 공통 또는 output 결속이 일치하지 않습니다.")
                    outcome = "incomplete"
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                outcome = "incomplete"
                output_binding = {"status": "NOT_EVALUATED", "passed": None,
                                  "reason": "성공 artifact의 output 결속을 파싱하지 못했습니다."}
                _issue(issues, "SUCCESS_BINDING_ERROR", name, "binding", f"{type(error).__name__}: {error}")
        else:
            outcome = "incomplete"
            output_binding = {"status": "NOT_EVALUATED", "passed": None,
                              "reason": "receipt·terminal·result 상태 조합이 완결된 성공이나 실패가 아닙니다."}

        prior = documents["binding-verification.json"]
        if prior is not None and outcome == "success":
            try:
                current = completed_call_verification(entry["path"], attributed)
                if prior != current:
                    _issue(issues, "PRIOR_BINDING_MISMATCH", name, "binding-verification.json",
                           "저장된 성공 결속 검증과 현재 artifact가 일치하지 않습니다.")
                    outcome = "incomplete"
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                _issue(issues, "PRIOR_BINDING_ERROR", name, "binding-verification.json",
                       f"{type(error).__name__}: {error}")
                outcome = "incomplete"

        usage_total = _terminal_usage(terminal)
        if isinstance(terminal, dict) and isinstance(terminal.get("payload"), dict):
            payload = terminal["payload"]
            empty_thread = thread.get("turns") == [] if isinstance(thread, dict) and "turns" in thread else None
            turns.append({"capture": name, "thread_id": payload.get("thread_id"), "turn_id": payload.get("turn_id"),
                          "usage_source": payload.get("usage_source"), "usage_scope": payload.get("usage_scope"),
                          "empty_new_thread": empty_thread, "usage_total": usage_total,
                          "provider_duration_ms": payload.get("duration_ms"),
                          "terminal_digest": states["terminal.json"]["digest"]})
        role_failure = None if failed is None else {
            "error_type": failed.get("error_type"), "error": failed.get("error"),
            "receipt_status": None if attributed is None else attributed.get("status"),
            "receipt_error_summary": None if attributed is None else attributed.get("error_summary"),
        }
        records.append({"capture": name, "artifacts": states, "outcome": outcome,
                         "receipt_call_id": None if attributed is None else attributed["call_id"],
                         "common_binding": common_binding, "output_binding": output_binding,
                         "role_failure": role_failure, "failure_kind": failure_kind,
                         "artifact_issue_count": 0})

    for call_id, capture_names in owners.items():
        if len(set(capture_names)) > 1:
            _issue(issues, "CALL_RECEIPT_ATTRIBUTION_CONFLICT", ",".join(capture_names), "receipt",
                   f"call_id {call_id}가 여러 capture에 귀속됩니다.")
    for call_id in receipts_by_id.keys() - owners.keys():
        _issue(issues, "UNATTRIBUTED_RECEIPT", "run", "receipt",
               f"call_id {call_id}가 현재 run의 어떤 capture에도 결속되지 않습니다.")
    turn_keys = [(turn["thread_id"], turn["turn_id"]) for turn in turns
                 if isinstance(turn["thread_id"], str) and isinstance(turn["turn_id"], str)]
    if len(set(turn_keys)) != len(turn_keys):
        _issue(issues, "DUPLICATE_TERMINAL_TURN", "run", "terminal.json",
               "동일 provider thread/turn의 terminal이 여러 capture에 귀속됩니다.")
    for record in records:
        record["artifact_issue_count"] = sum(issue["capture"] == record["capture"] for issue in issues)
    return {"calls": records, "receipts": [receipt for call_id, receipt in receipts_by_id.items()
                                             if call_id in owners],
            "receipt_sources": receipt_sources, "issues": issues, "provider_turn_usage": turns}


def _summary_input_digest(run: Path) -> str:
    excluded = {"summary.json", "generation-pending.json"}
    observed = {str(path.relative_to(run)): sha256_bytes(path.read_bytes()) for path in sorted(run.rglob("*"))
                if path.is_file() and path.name not in excluded and "runtime-preflight" not in path.parts}
    return sha256_digest(observed)


def summarize(run, status, error=None):
    """provider 호출 없이 저장 artifact를 요약하고 결과 파일을 한 번만 배타적으로 게시한다."""
    summary_path = run / ("generation-pending.json" if status == "GENERATION_REVIEW_REQUIRED" else "summary.json")
    input_digest = _summary_input_digest(run)
    if summary_path.exists():
        existing = read(summary_path)
        if existing.get("summary_input_digest") != input_digest:
            raise RuntimeError("SUMMARY_INPUT_CHANGED_AFTER_PUBLICATION")
        return existing
    lock = read(run / "preflight.json")
    binding = read(run / "instruction-binding.json")
    collected = collect_call_artifacts(run)
    portable = "workspace_binding" in lock
    isolation_error = None
    if portable:
        try:
            verify_portable_inputs(lock)
        except Exception as exc:
            isolation_error = f"{type(exc).__name__}: {exc}"
    checks = {"source_unchanged": source_manifest_digest(ROOT) == lock["source_manifest_digest"],
              "preserved_originals": isolation_error is None if portable else preserved_files(run) == lock["original_files"],
              "instructions_unchanged": all(Path(item["path"]).is_file() and
                  sha256_bytes(Path(item["path"]).read_bytes()) == item["content_digest"] for item in binding["sources"]),
              "workspace_unchanged": ({Path(name).as_posix(): digest for name, digest in files(run / "workspace").items()}
                                      == lock["workspace_files"] if portable
                                      else files(run / "workspace") == files(OLD / "workspace"))}
    if portable:
        checks["isolated_inputs_unchanged"] = isolation_error is None
    for call in collected["calls"]:
        if call["common_binding"] is not None:
            checks[f"{call['capture']}_common_binding"] = call["common_binding"]["passed"]
        if call["output_binding"]["status"] == "APPLICABLE":
            checks[f"{call['capture']}_output_binding"] = call["output_binding"]["passed"]
    effect_counts = {
        "logical_calls": len(collected["calls"]),
        "requests": sum(call["artifacts"]["request.json"]["exists"] for call in collected["calls"]),
        "thread_intents": sum(call["artifacts"]["thread.intent.json"]["exists"] for call in collected["calls"]),
        "thread_start_receipts": sum(call["artifacts"]["thread.receipt.json"]["parse_status"] == "parsed" for call in collected["calls"]),
        "turn_intents": sum(call["artifacts"]["turn.intent.json"]["exists"] for call in collected["calls"]),
        "turn_start_receipts": sum(call["artifacts"]["turn.receipt.json"]["parse_status"] == "parsed" for call in collected["calls"]),
        "terminal_observations": sum(call["artifacts"]["terminal.json"]["parse_status"] == "parsed" for call in collected["calls"]),
        "accepted_results": sum(call["outcome"] == "success" for call in collected["calls"]),
    }
    usage_keys = {"input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
                  "output_tokens": "outputTokens", "reasoning_tokens": "reasoningOutputTokens", "total_tokens": "totalTokens"}
    turns = collected["provider_turn_usage"]
    unique_turns = {(turn["thread_id"], turn["turn_id"]) for turn in turns
                    if isinstance(turn["thread_id"], str) and isinstance(turn["turn_id"], str)}
    usage_available = (effect_counts["turn_start_receipts"] == len(turns) == len(unique_turns) and bool(turns) and all(
        turn["empty_new_thread"] is True and isinstance(turn["usage_total"], dict) and
        all(type(turn["usage_total"].get(raw)) is int and turn["usage_total"][raw] >= 0 for raw in usage_keys.values())
        for turn in turns))
    usage = {key: sum(turn["usage_total"][raw] for turn in turns) if usage_available else None
             for key, raw in usage_keys.items()}
    receipts = collected["receipts"]
    all_calls_have_receipt = len(receipts) == effect_counts["logical_calls"] and not any(
        issue["code"].startswith(("PARTIAL_RECEIPT", "MALFORMED_RECEIPT", "DUPLICATE_CALL_ID", "CALL_RECEIPT"))
        for issue in collected["issues"])
    usage["latency_ms"] = sum(item["latency_ms"] for item in receipts) if all_calls_have_receipt and receipts else None
    usage["provider_duration_ms"] = sum(turn["provider_duration_ms"] for turn in turns) if usage_available and all(
        type(turn["provider_duration_ms"]) is int and turn["provider_duration_ms"] >= 0 for turn in turns) else None
    usage["reasoning_included_in_output"] = True if usage_available else None
    usage["unavailable_reason"] = None if usage_available else "모든 시작 turn의 완전한 terminal usage를 유일하게 귀속하지 못했습니다."
    recovery_count = sum(item["schema_recovery_attempts"] for item in receipts) if all_calls_have_receipt else None
    outcomes = {name: sum(call["outcome"] == name for call in collected["calls"])
                for name in ("success", "failure", "provider_terminal_failed", "external_unknown", "incomplete")}
    budget = {"maximum_logical_calls": lock.get("maximum_logical_calls"),
              "maximum_provider_turns": lock.get("maximum_provider_turns"),
              "remaining_logical_calls": (lock["maximum_logical_calls"] - effect_counts["logical_calls"]
                                            if type(lock.get("maximum_logical_calls")) is int else None),
              "remaining_provider_turns": (lock["maximum_provider_turns"] - effect_counts["turn_intents"]
                                             if type(lock.get("maximum_provider_turns")) is int else None)}
    clean_artifacts = not collected["issues"] and outcomes["external_unknown"] == 0 and outcomes["incomplete"] == 0
    final_status = status if all(checks.values()) and clean_artifacts else "FAIL"
    case_results = []
    for name in execution_order(lock):
        path = run / "case-results" / f"{name}.json"
        case_results.append(read(path) if path.is_file() else {
            "case_id": name, "status": "NOT_RUN", "failure_kind": None,
            "semantic_evaluated": False, "error": error or "해당 단계의 완료 관측이 없습니다.",
        })
    if any(case["status"] == "FAIL" for case in case_results):
        final_status = "FAIL"
    summary_path = run / ("generation-pending.json" if final_status == "GENERATION_REVIEW_REQUIRED" else "summary.json")
    summary = {"status": final_status, "error": error, "checks": checks,
               "isolation_error": isolation_error,
               "execution_mode": lock.get("execution_mode", "qualification"),
               "case_results": case_results,
               "collection_complete": all(case["status"] != "NOT_RUN" for case in case_results),
               "diagnostic_errors": collected["issues"], "call_artifacts": collected["calls"],
               "outcomes": outcomes, "effect_counts": effect_counts,
               "preflight_digest": lock["lock_digest"], "summary_input_digest": input_digest,
               "receipts": receipts, "receipt_sources": collected["receipt_sources"],
               "logical_calls": effect_counts["logical_calls"],
               "provider_turns": effect_counts["turn_start_receipts"],
               "schema_recovery_attempts": recovery_count,
               "budget": budget, "usage": usage, "provider_turn_usage": turns,
               "all_usage_available": usage_available,
               "billed_cost": None, "billed_cost_reason": "provider receipt가 청구 금액을 제공하지 않는다.",
               "generation_assessment": read(run / "generation-assessment.json") if (run / "generation-assessment.json").exists() else None,
               "plan_activated": False, "worker_executed": False, "new_ledger_writes": 0,
               "full_qualification": "NOT_RUN", "cutover": "NO-GO", "observed_at": utc_now()}
    write_new(summary_path, summary)
    published = read(summary_path)
    print(json.dumps({key: published[key] for key in ("status", "logical_calls", "provider_turns", "usage", "error")}, ensure_ascii=False), flush=True)
    return published


def verify_generation_pending(run: Path) -> None:
    pending = read(run / "generation-pending.json")
    if (pending.get("status") != "GENERATION_REVIEW_REQUIRED" or not pending.get("checks") or
            not all(pending["checks"].values()) or pending.get("logical_calls") != 12 or
            pending.get("provider_turns") != 12 or
            len(list((run / "calls").glob("*/result.json"))) != 12 or
            len(list((run / "calls").glob("*/turn.intent.json"))) != 12):
        raise RuntimeError("GENERATION_PENDING_FAILED_OR_INCOMPLETE")


def incomplete_provider_intents(run: Path) -> list[Path]:
    incomplete = []
    for intent in sorted((run / "runtime-preflight").glob("*/instructions-probe.intent.json")):
        if not intent.with_name("instructions-probe.receipt.json").exists():
            incomplete.append(intent)
    for capture in sorted((run / "calls").glob("*")):
        if not capture.is_dir():
            continue
        if (capture / "thread.intent.json").exists() and not (capture / "thread.receipt.json").exists():
            incomplete.append(capture / "thread.intent.json")
        if (capture / "turn.intent.json").exists() and not (capture / "turn.receipt.json").exists():
            incomplete.append(capture / "turn.intent.json")
    return incomplete


def claim_execution_phase(run: Path, marker: str, preflight_digest: str) -> None:
    incomplete = incomplete_provider_intents(run)
    if incomplete:
        raise RuntimeError("INCOMPLETE_PROVIDER_INTENT_EXISTS: " + ", ".join(str(path) for path in incomplete))
    write_new(run / marker, {"preflight_digest": preflight_digest, "observed_at": utc_now()})


def execute(run, generated=False):
    marker = "generated-review-started.json" if generated else "execution-started.json"
    if (run / "summary.json").exists():
        raise RuntimeError("완료·실패한 진단을 반복하지 않습니다.")
    lock = verify_lock(run)
    diagnostic = lock.get("execution_mode", "qualification") == "development-diagnostic"
    if generated and diagnostic:
        raise RuntimeError("GENERATED_REVIEW_NOT_IN_STATIC_DIAGNOSTIC")
    # claim은 요약 예외 처리보다 앞에 둔다. 패자는 summary나 provider 효과를 만들 수 없다.
    claim_execution_phase(run, marker, lock["lock_digest"])
    status = "FAIL"
    error = None
    try:
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
            request = capture_request("expanded-review", run, roles, OperationalBinding.model_validate(lock["operational_binding"]).inventory)
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
            names = execution_order(lock) if diagnostic else execution_order(lock)[:-1]
        phase = "review-generated" if generated else "run"
        with CapturingRuntime(run=run, phase=phase, codex_bin=Path(lock["codex_bin"])) as runtime:
            runner = RecordedRunner(runtime, run, lock)
            failed_cases = []
            for name in names:
                runner.name = name
                print(json.dumps({"starting_case": name, "maximum_calls": len(execution_order(lock))}), flush=True)
                try:
                    result = invoke(name, runner, run, roles, OperationalBinding.model_validate(lock["operational_binding"]).inventory)
                except StructuredRoleError as exc:
                    failure_kind = completed_output_failure(run, name, lock)
                    record_case_result(run, name, status="FAIL",
                                       failure_kind=failure_kind or "external_unknown",
                                       semantic_evaluated=False, error=f"{type(exc).__name__}: {exc}")
                    if not diagnostic or failure_kind != "model_output":
                        raise
                    failed_cases.append(name)
                    continue
                if name == "expansion":
                    write_new(run / "expanded-plan.json", result)
                    record_case_result(run, name, status="PASS", failure_kind=None, semantic_evaluated=False)
                    status = "GENERATION_REVIEW_REQUIRED"
                else:
                    write_new(run / f"{name}-review.json", result)
                    envelope = PlanReviewEnvelope.model_validate(runner.last_result.payload)
                    request = RoleCallRequest.model_validate(read(run / "requests" / f"{name}.json"))
                    assessment = assess_case_inspection_review(
                        envelope, case_expectation(run, name, request), case_id=name, payload=request.payload,
                    )
                    write_new(run / f"{name}-assessment.json", assessment)
                    record_case_result(run, name, status="PASS" if assessment["passed"] else "FAIL",
                                       failure_kind=None if assessment["passed"] else "semantic", semantic_evaluated=True)
                    if not assessment["passed"]:
                        if diagnostic:
                            failed_cases.append(name)
                            continue
                        raise RuntimeError(f"SEMANTIC_ASSESSMENT_FAILED: {name}: {json.dumps(assessment, ensure_ascii=False)}")
            if diagnostic:
                status = "FAIL" if failed_cases else "PASS"
                error = "CASE_FAILURES: " + ", ".join(failed_cases) if failed_cases else None
            elif generated:
                status = "PASS"
    except Exception as exc:
        status, error = "FAIL", f"{type(exc).__name__}: {exc}"
    summarize(run, status, error)


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "prepare", "run", "review-generated"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--role-config", type=Path,
                        help="preflight/prepare의 절대 역할 설정 경로. 기존 진단의 prepare에서만 S05 기본 설정 생략을 허용합니다.")
    parser.add_argument("--execution-mode", choices=EXECUTION_MODES)
    parser.add_argument("--fixture-package", type=Path)
    parser.add_argument("--codex-bin", type=Path)
    arguments = parser.parse_args(argv)
    preparation_options = (arguments.role_config, arguments.execution_mode, arguments.fixture_package, arguments.codex_bin)
    if any(option is not None for option in preparation_options) and arguments.mode not in ("preflight", "prepare"):
        parser.error("입력 설정은 preflight/prepare에서만 사용할 수 있습니다.")
    if (arguments.fixture_package is None) != (arguments.codex_bin is None):
        parser.error("--fixture-package와 --codex-bin을 함께 지정해야 합니다.")
    if arguments.mode == "preflight" and (arguments.fixture_package is None or arguments.role_config is None):
        parser.error("preflight에는 --fixture-package, --codex-bin, --role-config가 필요합니다.")
    if arguments.fixture_package is not None and arguments.role_config is None:
        parser.error("독립 package 실행에는 명시적 --role-config가 필요합니다.")
    return arguments


if __name__ == "__main__":
    arguments = parse_arguments()
    destination = arguments.run_root.resolve()
    if not destination.name.startswith(("inspection-", "r-s06-10-", "r-s06-12-", "r-s06-13-", "r-s06-14-", "r-s06-15-", "r-s06-17-", "r-s06-19-")) or destination.parent != OLD.parent:
        raise RuntimeError("새 R-S06 검사 진단 디렉터리만 허용합니다.")
    if destination.name.startswith("inspection-") and arguments.mode in ("preflight", "prepare") and arguments.fixture_package is None:
        raise RuntimeError("새 inspection 실행에는 고정 worktree와 독립 fixture package가 필요합니다.")
    if arguments.mode == "preflight":
        binding = portable_preflight(destination, fixture_package=arguments.fixture_package,
            codex_bin=arguments.codex_bin, role_configuration=load_role_configuration_input(arguments.role_config),
            execution_mode=arguments.execution_mode or "qualification")
        print(json.dumps({"preflight_passed": True, "binding_digest": sha256_digest(binding)}), flush=True)
    elif arguments.mode == "prepare":
        try:
            prepare(destination, load_role_configuration_input(arguments.role_config),
                    execution_mode=arguments.execution_mode or "qualification",
                    fixture_package=arguments.fixture_package, codex_bin=arguments.codex_bin)
        except Exception as error:
            write_new(destination / "preparation-failed.json", {
                "status": "FAIL", "error": f"{type(error).__name__}: {error}",
                "provider_turns": len(list((destination / "calls").glob("*/turn.intent.json"))),
                "full_qualification": "NOT_RUN", "cutover": "NO-GO", "observed_at": utc_now(),
            })
            raise
    else:
        execute(destination, generated=arguments.mode == "review-generated")
