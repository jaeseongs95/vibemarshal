"""agent-governance-suite 플러그인 적합성 검사.

manifest와 preflight는 플러그인이 실행 가능한지만 확인한다. 이 모듈은 governance gate가 실제로 기대는 동작
(거절, 판정, 기록 형식)을 임시 git 저장소와 임시 플러그인 state에서 끝까지 돌려 확인한다. 테스트 프레임워크에
의존하지 않으며 채택 명령, E2E preflight, 제품 런타임의 처음 보는 plugin identity에서 같은 함수를 부른다.

- 격리: 검사마다 새 임시 디렉터리를 쓰고 끝나면 지운다. 대상 프로젝트·Engine 원장·제품 gate의 플러그인 state에는
  쓰지 않는다. 그래서 검사 안의 실패는 모두 효과 없는 실패다.
- 합성 attestation: probe의 서명은 임시 state의 키로만 한다(제품 run의 state에서는 검증되지 않는다). model 이름과
  class는 probe 전용 값을 호스트의 주장으로 직접 제출하며 실제 모델 이름을 쓰지 않는다. 결과는 local_derived이고
  usage·steward 관측·stage evidence·Task validation 근거가 아니다.
- 분류: 플러그인이 기대와 다르게 동작하면 그 검사를 FAIL로 돌려준다(결정적 결과). timeout·프로세스 시작 실패·git
  실패 같은 환경성 실패는 결과가 아니라 GovernanceUnavailable(effects_started=False)로 던져 호출자가 다시 시도한다.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..canonical import sha256_digest
from .governance_gate import (
    ACCEPTANCE_STATES, CONSUMED_SURFACE, MODEL_CLASSES_FORMAT, SCOPE_STATES, SNAPSHOT_IDENTITY, SUPPORTED_CAPABILITIES,
    GovernanceContractMismatch, GovernancePlugin, GovernanceUnavailable, _fields, _git, _sha256_file,
    conformance_result, convergence_frame, mcp_data, mcp_envelope, plan_stages, snapshot_worktree,
    stage_record_arguments,
)

CONFORMANCE_FORMAT = "flowmarshal-governance-conformance-v1"
# probe 전용 값이다. 실제 모델 이름이 아니며 class는 계획이 요구하는 하한을 넘도록 probe가 제출하는 주장이다.
PROBE_MODEL = "flowmarshal-conformance-probe"
PROBE_CLASS = "deep"
PROBE_OBSERVATION = {"model": PROBE_MODEL, "effort": "high", "actorId": "flowmarshal-engine:conformance-probe"}
DEADLINE_SECONDS = 600.0
# 검사 항목 ID. gate가 기대는 표면이 바뀔 때만 바뀐다. 이 목록의 digest가 검사 집합 digest다.
CHECKS = (
    "attestation_required", "plan", "root", "claim", "start", "baseline", "record_baseline", "scope_out_of_scope",
    "scope_in_scope", "record_implementation", "record_scope", "acceptance_refuted", "acceptance",
    "record_acceptance", "finalize",
)
CHECK_SET_DIGEST = sha256_digest({"format": CONFORMANCE_FORMAT, "checks": list(CHECKS)})
TARGET = "target.txt"


def _envelope() -> dict[str, Any]:
    """gate가 Task에서 투영하는 TaskEnvelope와 같은 모양의 probe 계약."""
    objective = "적합성 검사 probe: target.txt 한 줄을 바꾼다"
    return {
        "schemaVersion": "1.0.0", "taskId": "fm-conformance-probe", "objective": objective,
        "scope": {"included": [TARGET], "excluded": []},
        "acceptanceCriteria": ["target.txt가 바뀐다"], "riskLevel": "medium",
        "workUnits": [{"id": "WU-1", "objective": objective, "dependencies": [], "writeTargets": [TARGET]}],
        "requiredCapabilities": sorted(SUPPORTED_CAPABILITIES),
        "constraints": ["FlowMarshal Execution Spec의 쓰기 target만 바꾼다"],
        "authorization": {"allowedActions": ["Execution Spec의 쓰기 target 편집과 Task validation 실행"],
                          "prohibitedActions": ["쓰기 target 밖 변경"], "approvalRequired": []},
        "decision": {"complexity": "simple", "hasConflicts": False},
        "orchestration": {"requested": True, "mcpAvailable": True},
    }


def _write(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def _close(plugin: Any) -> None:
    """정리 실패가 진행 중인 예외를 덮지 않게 한다.

    검사 효과는 임시 디렉터리 안에만 있고 결과는 이미 관측한 검사 항목으로 정해지므로, 정리 실패는 판정을 바꾸지
    않는다. 플러그인 프로세스 정리 자체는 McpStdioClient.close가 stdin 실패와 무관하게 끝까지 한다.
    """
    try:
        plugin.close()
    except Exception:
        pass


class _Probe:
    def __init__(self, plugin: Any, work: Path, deadline: float) -> None:
        self.plugin, self.work, self.deadline = plugin, work, deadline
        self.repo = work / "repo"
        self.results: list[dict[str, Any]] = []

    def check(self, check_id: str, expected: str, action: Callable[[], Any]) -> Any:
        """검사 하나를 실행한다. 계약 불일치는 FAIL 결과로 남기고 None을 돌려준다."""
        if time.monotonic() > self.deadline:
            raise GovernanceUnavailable(f"GOVERNANCE_TIMEOUT: conformance: {DEADLINE_SECONDS}초 안에 끝나지 않았습니다.")
        try:
            value = action()
        except GovernanceContractMismatch as error:
            self.results.append({"id": check_id, "status": "FAIL", "expected": expected, "observed": str(error)})
            return None
        self.results.append({"id": check_id, "status": "PASS", "expected": expected, "observed": expected})
        return value

    def expect(self, check: str, expected: str, observed: Any, passed: bool) -> None:
        if not passed:
            raise GovernanceContractMismatch(f"conformance:{check}", expected, observed)

    def call(self, tool: str, arguments: dict[str, Any], *, signed: bool) -> dict[str, Any]:
        return mcp_data(tool, self.plugin.call(tool, arguments, PROBE_OBSERVATION if signed else None))

    def script(self, name: str, request: dict[str, Any], verdicts: dict[str, str] | None = None) -> tuple[dict[str, Any], Path]:
        request_path = _write(self.work / f"{name}-request.json", request)
        output = _fields(f"script_output:{name}", self.plugin.script(name, request_path), CONSUMED_SURFACE["scripts"][name])
        if verdicts is not None and output["verdict"] not in verdicts:
            raise GovernanceContractMismatch(f"script_output:{name}:verdict", sorted(verdicts), output["verdict"])
        return output, _write(self.work / f"{name}-{len(self.results)}.json", output)

    def snapshot(self, name: str) -> str:
        return snapshot_worktree(self.repo, ref=f"refs/flowmarshal/conformance/{name}",
                                 message=f"flowmarshal conformance {name}")["commit"]

    def run(self) -> None:
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / TARGET).write_text("before\n", encoding="utf-8", newline="\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--no-gpg-sign", "-q", "-m", "flowmarshal conformance probe",
             env={**os.environ, **SNAPSHOT_IDENTITY})
        envelope = _envelope()
        envelope_path = _write(self.work / "envelope.json", envelope)
        c0 = self.snapshot("c0")
        plan_arguments = {"schemaVersion": "1.0.0", "taskEnvelope": envelope}

        def attestation_required() -> None:
            reply = mcp_envelope(self.plugin.call("plan_workflow", plan_arguments, None))
            code = reply.get("error", {}).get("code") if isinstance(reply, dict) and isinstance(reply.get("error"), dict) else None
            self.expect("attestation_required", "ok:false, BINDING_REQUIRED", reply,
                        isinstance(reply, dict) and reply.get("ok") is False and code == "BINDING_REQUIRED")

        self.check("attestation_required", "attestation 없는 plan_workflow를 BINDING_REQUIRED로 거절", attestation_required)

        def plan() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
            reply = self.call("plan_workflow", plan_arguments, signed=True)
            return reply, plan_stages(reply)

        planned = self.check("plan", "서명된 plan_workflow가 orchestrated 계획과 네 stage를 돌려줌", plan)
        if planned is None:
            return
        workflow, stages = planned
        root = self.check("root", "open_convergence_root가 rootId와 revision을 돌려줌", lambda: self.call(
            "open_convergence_root", {"schemaVersion": "1.0.0", "parentRootId": None, "taskEnvelope": envelope,
                                      "frame": convergence_frame("flowmarshal-conformance-probe", self.repo, envelope_path, c0),
                                      "userApprovalRefs": [], "responseMode": "compact"}, signed=False))
        if root is None:
            return
        lease = self.check("claim", "claim_workflow_attempt가 leaseId와 rootRevision을 돌려줌", lambda: self.call(
            "claim_workflow_attempt", {"schemaVersion": "1.0.0", "rootId": root["rootId"], "expectedRevision": root["revision"],
                                       "plan": workflow, "actorId": PROBE_OBSERVATION["actorId"],
                                       "outputTargets": [str(self.repo)], "priorFailure": None}, signed=False))
        if lease is None:
            return
        started = self.check("start", "start_guarded_workflow가 runId와 revision을 돌려줌", lambda: self.call(
            "start_guarded_workflow", {"schemaVersion": "1.0.0", "leaseId": lease["leaseId"],
                                       "expectedRootRevision": lease["rootRevision"], "responseMode": "compact"}, signed=False))
        if started is None:
            return
        run_id, revision = started["runId"], started["revision"]

        def record(check_id: str, stage_key: str, state: str, output_file: Path, artifacts: tuple, target: str) -> bool:
            nonlocal revision
            recorded = self.check(check_id, f"record_stage_result({stage_key}, {state})가 revision을 돌려줌", lambda: self.call(
                "record_stage_result", stage_record_arguments(
                    run_id=run_id, revision=revision, stage=stages[stage_key], stage_key=stage_key, stage_state=state,
                    output_file=output_file, artifacts=artifacts, note=f"conformance probe {stage_key}",
                    target_digest=target), signed=True))
            if recorded is not None:
                revision = recorded["revision"]
            return recorded is not None

        def scope_request(commit: str) -> dict[str, Any]:
            return {"schemaVersion": "1.0.0", "repositoryRoot": str(self.repo), "mode": "verify",
                    "comparisonTarget": "commit", "commit": commit, "taskEnvelope": envelope,
                    "baseline": baseline[0], "baselineArtifactDigest": baseline[0]["manifestSha256"]}

        baseline = self.check("baseline", "어느 브랜치에도 없는 스냅샷 commit의 baseline", lambda: self.script("scope-baseline", {
            "schemaVersion": "1.0.0", "repositoryRoot": str(self.repo), "mode": "capture", "comparisonTarget": "commit",
            "commit": c0, "taskEnvelope": envelope}))
        if baseline is None or not record("record_baseline", "baseline", "passed", baseline[1],
                                          (("workspace-baseline", "WorkspaceBaseline.v1", ""),), baseline[0]["manifestSha256"]):
            return

        (self.repo / TARGET).write_text("after\n", encoding="utf-8", newline="\n")
        (self.repo / "undeclared.md").write_text("선언 밖 새 파일\n", encoding="utf-8", newline="\n")
        outside = self.snapshot("c1-out-of-scope")

        def out_of_scope() -> None:
            report, _ = self.script("scope-compare", scope_request(outside), SCOPE_STATES)
            self.expect("scope_out_of_scope", "PASS가 아닌 판정", report["verdict"], report["verdict"] != "PASS")

        self.check("scope_out_of_scope", "선언 밖 새 파일이 있는 스냅샷은 PASS가 아님", out_of_scope)
        (self.repo / "undeclared.md").unlink()
        c1 = self.snapshot("c1")

        def in_scope() -> tuple[dict[str, Any], Path]:
            report, path = self.script("scope-compare", scope_request(c1), SCOPE_STATES)
            self.expect("scope_in_scope", "PASS", f"{report['verdict']} {report['findings']}", report["verdict"] == "PASS")
            return report, path

        scope = self.check("scope_in_scope", "서로 다른 두 스냅샷 commit 비교에서 범위 안 변경은 PASS", in_scope)
        implementation = _write(self.work / "implementation.json", {"summary": "conformance probe가 target.txt를 바꿨다"})
        if scope is None or not record("record_implementation", "implementation", "passed", implementation, (),
                                       baseline[0]["manifestSha256"]):
            return
        if not record("record_scope", "scope", "passed", scope[1], (
                ("change-scope-report", "ChangeScopeReport.v1", ""),
                ("scoped-change-inventory", "ChangeScopeReport.v1#/changes", "#/changes"),
                ("ownership-collision-ledger", "ChangeScopeReport.v1#/summary", "#/summary")), scope[0]["currentDigest"]):
            return

        target = sha256_digest(c1)
        proof = _write(self.work / "evidence.json", {"observed": "target.txt가 after로 바뀌었다"})

        def acceptance_request(direction: str) -> dict[str, Any]:
            return {"schemaVersion": "1.0.0", "taskEnvelope": envelope,
                    "target": {"kind": "commit", "identifier": c1, "digest": target},
                    "evidence": [{"id": "fm-conformance-probe", "kind": "test", "locator": str(proof),
                                  "digest": f"sha256:{_sha256_file(proof)}", "targetDigest": target, "verified": True,
                                  "criterionIds": ["AC-001"], "direction": direction,
                                  "observedResult": f"conformance probe {direction}"}],
                    "verificationCommands": [], "knownLimitations": [], "criterionOverrides": []}

        def refuted() -> None:
            report, _ = self.script("acceptance-cli", acceptance_request("refutes"), ACCEPTANCE_STATES)
            self.expect("acceptance_refuted", "PASS가 아닌 판정", report["verdict"], report["verdict"] != "PASS")

        def accepted() -> tuple[dict[str, Any], Path]:
            report, path = self.script("acceptance-cli", acceptance_request("supports"), ACCEPTANCE_STATES)
            self.expect("acceptance", "PASS", report["verdict"], report["verdict"] == "PASS")
            return report, path

        self.check("acceptance_refuted", "refuting 근거만 있는 acceptance는 PASS가 아님", refuted)
        acceptance = self.check("acceptance", "supporting 근거의 acceptance는 PASS", accepted)
        if acceptance is None or not record("record_acceptance", "acceptance", "passed", acceptance[1], (
                ("acceptance-evidence-report", "AcceptanceEvidenceReport.v1", ""),
                ("verified-evidence-index", "AcceptanceEvidenceReport.v1#/verifiedEvidenceIndex", "#/verifiedEvidenceIndex")),
                target):
            return

        def finalize() -> None:
            final = self.call("finalize_workflow", {"runId": run_id, "expectedRevision": revision,
                                                    "responseMode": "compact"}, signed=False)
            self.expect("finalize", "passed", final["state"], final["state"] == "passed")

        self.check("finalize", "모든 stage를 기록한 workflow가 passed로 끝남", finalize)


def run_conformance(plugin_root: Path, *, plugin_factory: Callable[..., Any] = GovernancePlugin,
                    deadline_seconds: float = DEADLINE_SECONDS) -> dict[str, Any]:
    """plugin_root의 플러그인을 임시 자원에서 검사하고 결과(local_derived)를 돌려준다.

    preflight 불일치는 GovernanceContractMismatch, 환경성 실패는 GovernanceUnavailable로 던진다. 임시 디렉터리
    생성, 대응표 쓰기, 플러그인 생성과 정리까지 함수 본문 전체가 conformance_result의 분류 안에 있다.
    """
    return conformance_result(lambda: _observe(Path(plugin_root), plugin_factory, deadline_seconds))


def _observe(plugin_root: Path, plugin_factory: Callable[..., Any], deadline_seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="flowmarshal-conformance-", ignore_cleanup_errors=True) as temp:
        work = Path(temp)
        classes = _write(work / "probe-model-classes.json", {"format": MODEL_CLASSES_FORMAT, "classes": {PROBE_MODEL: PROBE_CLASS}})
        plugin = plugin_factory(plugin_root, work / "plugin-state", classes)
        try:
            identity = plugin.preflight()
            probe = _Probe(plugin, work, started + deadline_seconds)
            probe.run()
        finally:
            _close(plugin)
    ran = {item["id"]: item for item in probe.results}
    checks = [ran.get(check_id, {"id": check_id, "status": "NOT_RUN", "expected": None, "observed": None}) for check_id in CHECKS]
    return {
        "format": CONFORMANCE_FORMAT, "provenance": "local_derived",
        "verdict": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "check_set_digest": CHECK_SET_DIGEST, "identity": identity["summary"],
        "probe": {"model": PROBE_MODEL, "modelClass": PROBE_CLASS, "actorId": PROBE_OBSERVATION["actorId"]},
        "checks": checks, "duration_seconds": round(time.monotonic() - started, 1),
    }


def first_failure(result: dict[str, Any]) -> dict[str, Any] | None:
    return next((item for item in result["checks"] if item["status"] != "PASS"), None)
