from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from ..adapters.authority import FileHumanControlAuthority, default_control_path
from ..adapters.evidence import FileEvidenceStore
from ..adapters.runtime import CodexAppServerRuntime, FakeAgentRuntime
from ..adapters.sqlite import SQLiteLedger
from ..application import FlowMarshalService, RunOutcome
from ..domain import AccessMode, DomainError, PlanDraft, ResourceKind
from ..time import SystemClock
from ..windows_sandbox import (
    SANDBOX_NOT_READY,
    SandboxContractError,
    query_windows_sandbox_status,
    resolve_host_context,
)
from .verifier import Gate0BVerifier, write_report


def default_data_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "FlowMarshal"
    return Path.home() / ".local" / "share" / "FlowMarshal"


def build_parser() -> argparse.ArgumentParser:
    data_root = default_data_root()
    parser = argparse.ArgumentParser(
        prog="flowmarshal-gate0b",
        description="FlowMarshal Gate 0B 내부 기술 CLI (공개 호환성 계약 아님)",
    )
    parser.add_argument("--db", type=Path, default=data_root / "data" / "flowmarshal.db")
    parser.add_argument("--evidence", type=Path, default=data_root / "evidence")
    parser.add_argument("--control", type=Path, default=default_control_path())
    parser.add_argument(
        "--codex-bin",
        type=Path,
        help="실 Codex 실행에 사용할 호환 codex.exe 경로",
    )
    parser.add_argument(
        "--authoritative-codex-home",
        type=Path,
        help="실제 사용자·시스템 Codex가 함께 사용하는 권위 홈",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-ledger", help="SQLite 원장을 초기화합니다.")
    subparsers.add_parser("init-authority", help="사람 승인 capability를 최초 생성합니다.")

    register = subparsers.add_parser("register-project")
    register.add_argument("--name", required=True)
    register.add_argument("--workspace", type=Path, required=True)
    register.add_argument("--reference", type=Path, action="append", default=[])
    register.add_argument("--runtime-dependency", type=Path, action="append", default=[])
    register.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="PATH::REASON",
        help="수정·읽기 금지 경로와 이유",
    )

    import_plan = subparsers.add_parser("import-plan")
    import_plan.add_argument("--file", type=Path, required=True)

    for name in ("approve-plan", "activate-plan"):
        command = subparsers.add_parser(name)
        command.add_argument("revision_id")

    request = subparsers.add_parser("request-access")
    request.add_argument("attempt_id")
    request.add_argument("--path", type=Path, required=True)
    request.add_argument("--reason", required=True)
    request.add_argument(
        "--kind",
        choices=[ResourceKind.REFERENCE.value, ResourceKind.RUNTIME_DEPENDENCY.value],
        default=ResourceKind.REFERENCE.value,
    )

    grant = subparsers.add_parser("grant-access")
    grant.add_argument("request_id")

    run = subparsers.add_parser("run-once")
    run.add_argument("project_id")
    run.add_argument("--runtime", choices=["codex", "fake"], default="codex")

    subparsers.add_parser("startup-recover")
    observe = subparsers.add_parser("recover-observe")
    observe.add_argument("unknown_intent_id")
    bind = subparsers.add_parser("recover-bind")
    bind.add_argument("unknown_intent_id")
    bind.add_argument("external_id")
    interrupt = subparsers.add_parser("recover-interrupt")
    interrupt.add_argument("unknown_intent_id")
    interrupt.add_argument("--thread-id", required=True)
    interrupt.add_argument("--turn-id", required=True)
    abandon = subparsers.add_parser("recover-abandon")
    abandon.add_argument("unknown_intent_id")
    review = subparsers.add_parser("approve-human-review")
    review.add_argument("attempt_id")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--json", dest="json_output", type=Path)
    verify.add_argument("--markdown", type=Path)

    smoke_fake = subparsers.add_parser("smoke-fake")
    smoke_fake.add_argument("--root", type=Path, required=True)

    smoke_codex = subparsers.add_parser("smoke-codex")
    smoke_codex.add_argument("--root", type=Path, required=True)
    smoke_codex.add_argument("--timeout", type=int, default=300)
    smoke_codex.add_argument(
        "--authoritative-codex-home",
        type=Path,
        default=argparse.SUPPRESS,
    )
    return parser


def _components(
    args: argparse.Namespace,
    runtime: Any,
    *,
    validation_checks: dict[str, Any] | None = None,
) -> tuple[
    FlowMarshalService,
    SQLiteLedger,
    FileHumanControlAuthority,
]:
    clock = SystemClock()
    ledger = SQLiteLedger(args.db, clock=clock)
    ledger.initialize()
    authority = FileHumanControlAuthority(
        clock=clock, capability_path=args.control
    )
    service = FlowMarshalService(
        ledger=ledger,
        evidence_store=FileEvidenceStore(args.evidence),
        authority=authority,
        runtime=runtime,
        validation_checks=validation_checks,
    )
    return service, ledger, authority


def _revision_digest(ledger: SQLiteLedger, revision_id: str) -> str:
    with ledger.raw_connection() as connection:
        row = connection.execute(
            "SELECT content_digest FROM plan_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
    if row is None:
        raise DomainError("PlanRevision을 찾을 수 없습니다.")
    return row["content_digest"]


def _print(value: Any) -> None:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init-ledger":
            ledger = SQLiteLedger(args.db, clock=SystemClock())
            ledger.initialize()
            _print({"status": "initialized", "database": str(args.db)})
            return 0
        if args.command == "init-authority":
            authority = FileHumanControlAuthority(
                clock=SystemClock(), capability_path=args.control
            )
            path = authority.initialize()
            _print({"status": "ready", "capability_path": str(path)})
            return 0
        if args.command == "verify":
            report = Gate0BVerifier(args.db).verify()
            if args.json_output and args.markdown:
                write_report(
                    report,
                    json_path=args.json_output,
                    markdown_path=args.markdown,
                )
            elif args.json_output or args.markdown:
                parser.error("--json과 --markdown은 함께 지정해야 합니다.")
            _print(report.as_dict())
            return 0 if report.overall == "GO" else 2
        if args.command in {"smoke-fake", "smoke-codex"}:
            return _smoke(args, real=args.command == "smoke-codex")

        runtime: Any
        if args.command in {"run-once", "recover-observe", "recover-interrupt"}:
            if getattr(args, "runtime", "codex") == "fake":
                runtime = FakeAgentRuntime()
            else:
                context = resolve_host_context(
                    project_root=Path(__file__).resolve().parents[3],
                    authoritative_home=args.authoritative_codex_home,
                    requested_codex=args.codex_bin,
                )
                preflight = query_windows_sandbox_status(
                    context,
                    cwd=context.project_root,
                )
                if preflight.get("status") != "READY":
                    _print(preflight)
                    return 3 if preflight.get("status") == "SETUP_REQUIRED" else 2
                runtime = CodexAppServerRuntime(
                    codex_bin=context.system_codex,
                    codex_home=context.authoritative_home,
                )
        else:
            runtime = FakeAgentRuntime()
        try:
            service, ledger, authority = _components(args, runtime)
            if args.command == "register-project":
                protected = tuple(_parse_protected(item) for item in args.protect)
                _print(
                    service.register_project(
                        name=args.name,
                        workspace=args.workspace,
                        references=args.reference,
                        runtime_dependencies=args.runtime_dependency,
                        protected_paths=protected,
                    )
                )
            elif args.command == "import-plan":
                document = json.loads(args.file.read_text(encoding="utf-8"))
                draft = PlanDraft.model_validate(document)
                _print({"revision_id": service.import_candidate_plan(draft)})
            elif args.command == "approve-plan":
                digest = _revision_digest(ledger, args.revision_id)
                proof = authority.issue("approve_plan", digest)
                service.approve_plan(args.revision_id, proof)
                _print({"status": "approved", "revision_id": args.revision_id})
            elif args.command == "activate-plan":
                digest = _revision_digest(ledger, args.revision_id)
                proof = authority.issue("activate_plan", digest)
                service.activate_plan(args.revision_id, proof)
                _print({"status": "active", "revision_id": args.revision_id})
            elif args.command == "request-access":
                request_id = service.submit_access_request(
                    attempt_id=args.attempt_id,
                    exact_path=args.path,
                    reason=args.reason,
                    resource_kind=ResourceKind(args.kind),
                    access_mode=AccessMode.READ,
                )
                _print({"access_request_id": request_id, "status": "pending"})
            elif args.command == "grant-access":
                target = service.access_request_target_digest(args.request_id)
                proof = authority.issue("grant_access", target)
                grant_id = service.grant_access(args.request_id, proof)
                _print({"access_grant_id": grant_id, "status": "granted"})
            elif args.command == "run-once":
                recovered = service.startup_recover()
                if recovered:
                    _print(
                        {
                            "status": "recovery_required",
                            "reason_code": "EXECUTING_INTENT_FOUND_AT_STARTUP",
                            "unknown_intent_ids": recovered,
                        }
                    )
                    return 3
                _print(service.run_once(args.project_id))
            elif args.command == "startup-recover":
                _print({"unknown_intent_ids": service.startup_recover()})
            elif args.command == "recover-observe":
                _print(service.recover_observe(args.unknown_intent_id))
            elif args.command == "recover-bind":
                target = service.recovery_target_digest(args.unknown_intent_id)
                proof = authority.issue("recover_bind", target)
                _print(
                    service.recover_bind(
                        args.unknown_intent_id, args.external_id, proof
                    )
                )
            elif args.command == "recover-interrupt":
                target = service.recovery_target_digest(args.unknown_intent_id)
                proof = authority.issue("recover_interrupt", target)
                _print(
                    service.recover_interrupt(
                        args.unknown_intent_id,
                        thread_id=args.thread_id,
                        turn_id=args.turn_id,
                        proof=proof,
                    )
                )
            elif args.command == "recover-abandon":
                target = service.recovery_target_digest(args.unknown_intent_id)
                proof = authority.issue("recover_abandon", target)
                _print(service.recover_abandon(args.unknown_intent_id, proof))
            elif args.command == "approve-human-review":
                target = service.human_review_target_digest(args.attempt_id)
                proof = authority.issue("approve_human_review", target)
                _print(service.approve_human_review(args.attempt_id, proof))
            else:
                parser.error(f"지원하지 않는 명령: {args.command}")
            return 0
        finally:
            close = getattr(runtime, "close", None)
            if close is not None:
                close()
    except (DomainError, ValidationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error_type": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


def _parse_protected(value: str) -> tuple[Path, str]:
    try:
        path, reason = value.split("::", 1)
    except ValueError as error:
        raise ValueError("--protect는 PATH::REASON 형식이어야 합니다.") from error
    return Path(path), reason


def _smoke(args: argparse.Namespace, *, real: bool) -> int:
    from ..domain import (
        CompletionCriterion,
        VerificationSpec,
        VerificationType,
        WorkItemDefinition,
    )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = args.root / ("real" if real else "fake") / run_id
    workspace = root / "workspace"
    host_context = None
    sandbox_preflight = None
    if real:
        try:
            host_context = resolve_host_context(
                project_root=Path(__file__).resolve().parents[3],
                authoritative_home=getattr(args, "authoritative_codex_home", None),
                requested_codex=args.codex_bin,
            )
            sandbox_preflight = query_windows_sandbox_status(
                host_context,
                cwd=host_context.project_root,
            )
        except SandboxContractError as error:
            sandbox_preflight = {
                "status": "ERROR",
                "reason_code": error.reason_code,
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        if sandbox_preflight.get("status") != "READY":
            root.mkdir(parents=True, exist_ok=False)
            result = {
                "run_id": run_id,
                "runtime": "codex",
                "project_id": None,
                "revision_id": None,
                "execution_profile": None,
                "sandbox_preflight": sandbox_preflight,
                "outcome": {
                    "status": "failed",
                    "reason_code": sandbox_preflight.get("reason_code")
                    or SANDBOX_NOT_READY,
                },
                "workspace": None,
            }
            (root / "smoke-result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _print(result)
            return 3 if sandbox_preflight.get("status") == "SETUP_REQUIRED" else 2

    workspace.mkdir(parents=True, exist_ok=False)
    smoke_args = argparse.Namespace(
        db=root / "flowmarshal.db",
        evidence=root / "evidence",
        control=args.control if real else root / "control" / "approval-capability",
    )
    runtime: Any | None = None
    registered = None
    revision: str | None = None
    execution_profile: dict[str, str] | None = None
    try:
        runtime = (
            CodexAppServerRuntime(
                codex_bin=host_context.system_codex,
                codex_home=host_context.authoritative_home,
            )
            if real
            else FakeAgentRuntime()
        )
        validation_checks = None
        if real:
            validation_checks = {
                "runtime_output_ok": lambda _workspace: (
                    runtime.latest_agent_message() == "GATE0B_RUNTIME_OK"
                    and runtime.command_execution_succeeded(),
                    {
                        "message_digest": None
                        if runtime.latest_agent_message() is None
                        else "sha256:"
                        + hashlib.sha256(
                            runtime.latest_agent_message().encode("utf-8")
                        ).hexdigest(),
                        "sandbox_command_succeeded": runtime.command_execution_succeeded(),
                    },
                )
            }
        service, ledger, authority = _components(
            smoke_args, runtime, validation_checks=validation_checks
        )
        authority.initialize()
        registered = service.register_project(
            name="FlowMarshal Gate 0B 실제 합성 검사" if real else "FlowMarshal Gate 0B 합성 검사",
            workspace=workspace,
        )
        marker = workspace / "gate0b-smoke.txt"
        marker.write_text("GATE0B_SMOKE_OK\n", encoding="utf-8")
        execution_profile = (
            runtime.resolve_execution_profile(
                model_role="fast", reasoning_effort="low"
            )
            if real
            else {"model_role": "fast", "reasoning_effort": "low"}
        )
        draft = PlanDraft(
            project_id=registered.project_id,
            summary="RuntimeActionIntent와 evidence 전체 흐름을 검증한다.",
            work_items=(
                WorkItemDefinition(
                    client_ref="smoke",
                    goal=(
                        "PowerShell command 도구로 작업 폴더의 gate0b-smoke.txt를 읽어 "
                        "GATE0B_SMOKE_OK를 확인한 뒤 GATE0B_RUNTIME_OK 한 줄만 답한다."
                        if real
                        else "작업 폴더에 gate0b-smoke.txt가 준비돼 있다. 합성 실행을 완료한다."
                    ),
                    write_resource_id=registered.workspace_resource_id,
                    completion_criteria=(
                        CompletionCriterion(
                            criterion_id="runtime_output" if real else "marker",
                            description=(
                                "실 Codex가 sandbox command를 성공시키고 고정 응답을 완료한다."
                                if real
                                else "합성 marker가 올바른 내용을 가진다."
                            ),
                            verification=(
                                VerificationSpec(
                                    type=VerificationType.CHECK,
                                    check_id="runtime_output_ok",
                                )
                                if real
                                else VerificationSpec(
                                    type=VerificationType.ARTIFACT,
                                    relative_path="gate0b-smoke.txt",
                                    predicate="contains:GATE0B_SMOKE_OK",
                                )
                            ),
                        ),
                    ),
                    execution_profile=execution_profile,
                    validation_profile={
                        "model_role": "balanced",
                        "reasoning_effort": "medium",
                    },
                ),
            ),
        )
        revision = service.import_candidate_plan(draft)
        service.approve_plan(
            revision, authority.issue("approve_plan", draft.content_digest)
        )
        service.activate_plan(
            revision, authority.issue("activate_plan", draft.content_digest)
        )
        deadline = time.monotonic() + (args.timeout if real else 30)
        outcome: RunOutcome | None = None
        while time.monotonic() < deadline:
            outcome = service.run_once(registered.project_id)
            if outcome.status in {
                "completed",
                "failed",
                "recovery_required",
                "quarantined",
            }:
                break
            time.sleep(2 if real else 0.05)
        if outcome is None or outcome.status not in {"completed", "failed", "recovery_required", "quarantined"}:
            outcome = RunOutcome(
                registered.project_id,
                "failed",
                "SMOKE_TIMEOUT",
            )
        report = Gate0BVerifier(smoke_args.db).verify()
        write_report(
            report,
            json_path=root / "gate0b-ledger-verification.json",
            markdown_path=root / "gate0b-ledger-verification.md",
        )
        result = {
            "run_id": run_id,
            "runtime": "codex" if real else "fake",
            "project_id": registered.project_id,
            "revision_id": revision,
            "execution_profile": execution_profile,
            "sandbox_preflight": sandbox_preflight,
            "outcome": asdict(outcome),
            "ledger_verification": report.as_dict(),
            "workspace": str(workspace),
        }
        (root / "smoke-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _print(result)
        return 0 if outcome.status == "completed" and report.overall == "GO" else 2
    except Exception as error:
        # 초기 config 파싱 실패처럼 원장이 만들어지기 전의 오류도 실행별
        # smoke-result.json으로 남겨, 실패 시도 자체가 사라지지 않게 한다.
        result = {
            "run_id": run_id,
            "runtime": "codex" if real else "fake",
            "project_id": None if registered is None else registered.project_id,
            "revision_id": revision,
            "execution_profile": execution_profile,
            "outcome": {
                "status": "failed",
                "reason_code": "SMOKE_SETUP_OR_EXECUTION_ERROR",
            },
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            "workspace": str(workspace),
        }
        (root / "smoke-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _print(result)
        return 2
    finally:
        close = getattr(runtime, "close", None) if runtime is not None else None
        if close is not None:
            close()


if __name__ == "__main__":
    raise SystemExit(main())
