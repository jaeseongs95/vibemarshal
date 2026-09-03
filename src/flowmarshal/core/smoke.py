from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..canonical import sha256_digest
from .domain import (
    ActivationSource,
    Assignment,
    PlanDraft,
    ProjectDefinition,
    RuntimeBindingReceipt,
    ValidationDefinition,
    ValidationResultInput,
    ValidationStatus,
    WorkItemDefinition,
)
from .ledger import SQLiteCoreLedger
from .service import FlowMarshalCore


SCHEMA_VERSION = "1.0"
GATE_NAME = "R2"
RESULT_KIND = "flowmarshal_r2_core_receipt"
FORBIDDEN_LEGACY_TABLES = frozenset(
    {
        "access_grants",
        "access_request_decisions",
        "access_requests",
        "authority_uses",
        "protected_paths",
    }
)
REQUIRED_CORE_TABLES = frozenset(
    {
        "attempts",
        "evidence_records",
        "history_events",
        "plan_revisions",
        "projects",
        "runtime_action_intents",
        "runtime_bindings",
        "schema_meta",
        "validation_results",
        "work_item_dependencies",
        "work_items",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def fingerprint_trees(roots: Iterable[Path]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for root in sorted((path.resolve(strict=False) for path in roots), key=str):
        if not root.exists():
            continue
        for path in sorted((item for item in root.rglob("*") if item.is_file()), key=str):
            entries.append(
                {
                    "path": str(path.resolve(strict=True)),
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    return {
        "file_count": len(entries),
        "aggregate_digest": sha256_digest(entries),
        "entries": entries,
    }


def _assignment() -> Assignment:
    return Assignment(
        execution_model_id="r2-synthetic-execution-model",
        execution_reasoning_effort="medium",
        validation_model_id="r2-synthetic-validation-model",
        validation_reasoning_effort="high",
        selection_reason="R2는 선택 알고리즘이 아니라 assignment 보존을 검증한다.",
        fallback_policy={"mode": "stop_and_report"},
    )


def _work_item(
    client_ref: str,
    *,
    context_sources: tuple[str, ...],
    dependencies: tuple[str, ...] = (),
) -> WorkItemDefinition:
    return WorkItemDefinition(
        client_ref=client_ref,
        title=f"R2 {client_ref}",
        objective=f"R2 합성 {client_ref} 작업을 추적한다.",
        dependencies=dependencies,
        context_sources=context_sources,
        expected_changes=(f"src/{client_ref}.py",),
        out_of_scope=("실제 Codex Runner 호출",),
        deliverables=(f"src/{client_ref}.py",),
        acceptance_criteria=(f"{client_ref} 합성 검증 통과",),
        validations=(
            ValidationDefinition(
                criterion_id=f"{client_ref}.synthetic",
                check_type="synthetic",
                specification={"expected": "passed"},
            ),
        ),
        execution_requirements={
            "permissions": ":danger-full-access",
            "approval_policy": "never",
            "network": "normal",
        },
        assignment=_assignment(),
    )


def _pass(
    result: dict[str, Any], name: str, summary: str, **evidence: Any
) -> None:
    result["checks"][name] = {
        "status": "pass",
        "summary": summary,
        "evidence": evidence,
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_r2_smoke(
    *,
    project_root: Path,
    output_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    """새 Core만 사용하는 합성 lifecycle과 역사 artifact 불변성을 검사한다."""

    project_root = project_root.resolve(strict=True)
    historical_roots = (
        project_root / "spikes" / "gate0b" / "artifacts",
        project_root / "spikes" / "gate0c" / "artifacts",
    )
    historical_before = fingerprint_trees(historical_roots)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "gate": GATE_NAME,
        "run_id": run_id,
        "started_at": utc_now(),
        "completed_at": None,
        "decision": "NO-GO",
        "scope": {
            "actual_codex_runner": False,
            "runtime_basis": "R1에서 별도 검증 완료",
            "historical_artifact_roots": [str(path) for path in historical_roots],
        },
        "checks": {},
        "receipts": {"historical_before": historical_before},
        "error": None,
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        workspace = output_dir / "synthetic-project"
        workspace.mkdir()
        agents_path = workspace / "AGENTS.md"
        reference_path = output_dir / "approved-requirements.md"
        agents_path.write_text("R2 합성 프로젝트 지침\n", encoding="utf-8")
        reference_path.write_text("R2 승인 참고자료\n", encoding="utf-8")
        database = output_dir / "core.sqlite3"

        ledger = SQLiteCoreLedger(database)
        ledger.initialize()
        core = FlowMarshalCore(ledger)
        context_sources = (str(agents_path), str(reference_path))
        project_id = core.register_project(
            ProjectDefinition(
                name="R2 합성 프로젝트",
                root=str(workspace),
                context_sources=context_sources,
                default_validations=("synthetic",),
                runtime_requirements={
                    "permissions": ":danger-full-access",
                    "approval_policy": "never",
                },
            )
        )
        draft = PlanDraft(
            project_id=project_id,
            request_summary="R2 Core 원장 합성 lifecycle",
            work_items=(
                _work_item("foundation", context_sources=context_sources),
                _work_item(
                    "integration",
                    context_sources=context_sources,
                    dependencies=("foundation",),
                ),
            ),
        )
        revision_id = core.create_plan_draft(draft)
        activation = core.activate_plan(
            revision_id,
            expected_digest=draft.canonical_digest,
            source=ActivationSource.CLI,
        )
        _require(activation.canonical_digest == draft.canonical_digest, "digest 불일치")
        _pass(
            result,
            "direct_plan_activation",
            "HMAC proof 없이 정확한 revision digest를 CLI에서 활성화함",
            project_id=project_id,
            revision_id=revision_id,
            canonical_digest=draft.canonical_digest,
            activation_source=activation.activation_source,
        )

        initial_ready = core.ready_work_items(project_id)
        _require(
            [item["client_ref"] for item in initial_ready] == ["foundation"],
            "초기 ready 계산이 dependency 계약과 다릅니다.",
        )
        first = initial_ready[0]
        reservation = core.reserve_attempt(
            first["work_item_id"],
            thread_request={"title": "R2 foundation", "cwd": str(workspace)},
        )
        core.begin_intent(reservation.intent_id)
        binding_id = core.record_thread_binding(
            reservation.intent_id,
            RuntimeBindingReceipt(
                thread_id="synthetic-thread-r2-foundation",
                cwd=str(workspace),
                instruction_sources=context_sources,
                runtime_receipt={
                    "kind": "synthetic",
                    "session_id": "synthetic-session-r2-foundation",
                },
            ),
        )
        turn_intent_id = core.reserve_turn_intent(
            reservation.attempt_id,
            turn_request={"prompt_digest": "sha256:r2-synthetic"},
        )
        core.begin_intent(turn_intent_id)
        core.record_turn_binding(
            turn_intent_id,
            RuntimeBindingReceipt(
                thread_id="synthetic-thread-r2-foundation",
                turn_id="synthetic-turn-r2-foundation",
                cwd=str(workspace),
                instruction_sources=context_sources,
                runtime_receipt={"kind": "synthetic", "status": "completed"},
            ),
        )
        core.mark_worker_finished(
            reservation.attempt_id, summary="합성 Worker가 종료됨"
        )
        core.record_validation_result(
            reservation.attempt_id,
            ValidationResultInput(
                criterion_id="foundation.synthetic",
                check_type="synthetic",
                status=ValidationStatus.PASSED,
                summary="R2 합성 validation 통과",
                evidence={"expected": "passed", "observed": "passed"},
            ),
        )
        plan_completed = core.complete_attempt(reservation.attempt_id)
        _require(not plan_completed, "첫 WorkItem 뒤 plan이 조기 완료됐습니다.")
        next_ready = core.ready_work_items(project_id)
        _require(
            [item["client_ref"] for item in next_ready] == ["integration"],
            "선행 작업 완료 후 dependency 해제가 반영되지 않았습니다.",
        )
        _pass(
            result,
            "derived_ready_state",
            "ready를 저장값이 아니라 dependency 완료 상태에서 계산함",
            initial_ready=["foundation"],
            next_ready=["integration"],
        )

        second = next_ready[0]
        crash_reservation = core.reserve_attempt(
            second["work_item_id"],
            thread_request={"title": "R2 recovery window", "cwd": str(workspace)},
        )
        core.begin_intent(crash_reservation.intent_id)
        unknown = core.mark_executing_intents_unknown()
        _require(
            unknown == (crash_reservation.intent_id,),
            "receipt 없는 executing intent가 unknown으로 격리되지 않았습니다.",
        )
        _pass(
            result,
            "attempt_intent_binding_evidence",
            "Attempt·intent·thread/turn binding·validation evidence와 crash 격리를 보존함",
            completed_attempt_id=reservation.attempt_id,
            binding_id=binding_id,
            thread_id="synthetic-thread-r2-foundation",
            turn_id="synthetic-turn-r2-foundation",
            unknown_intent_id=crash_reservation.intent_id,
        )

        tables = set(ledger.table_names())
        missing = sorted(REQUIRED_CORE_TABLES - tables)
        forbidden = sorted(FORBIDDEN_LEGACY_TABLES & tables)
        _require(not missing, f"필수 Core table이 없습니다: {missing}")
        _require(not forbidden, f"호환 전용 table이 새 Core에 섞였습니다: {forbidden}")
        integrity = ledger.integrity_check()
        _require(integrity == ("ok",), f"SQLite integrity 실패: {integrity}")
        _require(ledger.verify_history(project_id), "History hash chain이 유효하지 않습니다.")
        _pass(
            result,
            "reduced_core_schema",
            "새 DB는 오케스트레이션 table만 가지며 authority/access table을 만들지 않음",
            tables=sorted(tables),
            forbidden_tables_present=forbidden,
            integrity=list(integrity),
        )

        with ledger.raw_connection() as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in sorted(REQUIRED_CORE_TABLES - {"schema_meta"})
            }
            assignment_row = connection.execute(
                "SELECT model_id, reasoning_effort, validation_model_id, "
                "validation_reasoning_effort FROM attempts WHERE id = ?",
                (reservation.attempt_id,),
            ).fetchone()
        result["receipts"]["core"] = {
            "database": str(database),
            "project_id": project_id,
            "revision_id": revision_id,
            "plan_digest": draft.canonical_digest,
            "table_counts": counts,
            "assignment": dict(assignment_row),
            "snapshot": core.project_snapshot(project_id),
        }

        historical_after = fingerprint_trees(historical_roots)
        result["receipts"]["historical_after"] = historical_after
        _require(
            historical_after == historical_before,
            "기존 Gate artifact tree의 파일 내용 또는 구성이 바뀌었습니다.",
        )
        _pass(
            result,
            "historical_artifacts_unchanged",
            "Gate 0B/0C artifact를 마이그레이션하거나 덮어쓰지 않음",
            file_count=historical_after["file_count"],
            aggregate_digest=historical_after["aggregate_digest"],
        )
        result["decision"] = "GO"
    except BaseException as exc:
        result["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
        }
        result["checks"].setdefault(
            "execution",
            {
                "status": "fail",
                "summary": str(exc)[:500],
                "evidence": {"type": type(exc).__name__},
            },
        )
        try:
            result["receipts"]["historical_after"] = fingerprint_trees(
                historical_roots
            )
        except BaseException as fingerprint_error:
            result["receipts"]["historical_after_error"] = str(fingerprint_error)[:500]
    finally:
        result["completed_at"] = utc_now()
    return result


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# FlowMarshal R2 Core 원장 축소·정리",
        "",
        f"- 판정: **{result.get('decision', 'UNKNOWN')}**",
        f"- 실행 ID: `{result.get('run_id', '-')}`",
        f"- 시작: `{result.get('started_at', '-')}`",
        f"- 완료: `{result.get('completed_at', '-')}`",
        "",
        "## 검사 결과",
        "",
        "| 검사 | 상태 | 설명 |",
        "|---|---|---|",
    ]
    for name, check in result.get("checks", {}).items():
        lines.append(
            f"| `{name}` | {check.get('status', 'unknown')} | "
            f"{check.get('summary', '-')} |"
        )
    core = result.get("receipts", {}).get("core", {})
    if core:
        lines.extend(
            [
                "",
                "## Core 원장",
                "",
                f"- DB: `{core.get('database', '-')}`",
                f"- Project: `{core.get('project_id', '-')}`",
                f"- PlanRevision: `{core.get('revision_id', '-')}`",
                f"- Plan digest: `{core.get('plan_digest', '-')}`",
                "",
                "Assignment의 모델 ID는 원장 복사·보존을 시험하기 위한 합성값이다. "
                "실제 모델 catalog 기반 선택은 R4 범위다.",
            ]
        )
    error = result.get("error")
    if error:
        lines.extend(
            [
                "",
                "## 실패",
                "",
                f"- 유형: `{error.get('type', '-')}`",
                f"- 원인: {error.get('message', '-')}",
            ]
        )
    lines.extend(
        [
            "",
            "## 범위",
            "",
            "R2는 새 SQLite Core의 도메인 전이와 보존 성질을 합성 receipt로 검사한다. "
            "실제 Codex Runner lifecycle은 R1에서 이미 검증했으며 이 실행은 새 task를 만들지 않는다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(output_dir: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    receipt_path = output_dir / "core-receipt.json"
    report_path = output_dir / "core-report.md"
    receipt_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.write_text(render_report(result), encoding="utf-8")
    return receipt_path, report_path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"r2-{stamp}-{uuid.uuid4().hex[:8]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowMarshal R2 축소 Core 원장 lifecycle을 합성 검증합니다."
    )
    parser.add_argument("--project-root", type=Path, default=_project_root())
    parser.add_argument("--run-id")
    parser.add_argument("--output-dir", type=Path)
    return parser


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve(strict=True)
    run_id = args.run_id or _default_run_id()
    output_dir = args.output_dir or (
        project_root
        / "spikes"
        / "orchestration"
        / "r2"
        / "artifacts"
        / "runs"
        / run_id
    )
    if output_dir.exists():
        print(f"artifact 디렉터리가 이미 존재합니다: {output_dir}", file=sys.stderr)
        return 2
    result = run_r2_smoke(
        project_root=project_root,
        output_dir=output_dir,
        run_id=run_id,
    )
    receipt_path, report_path = write_artifacts(output_dir, result)
    print(f"R2 판정: {result['decision']}")
    print(f"receipt: {receipt_path}")
    print(f"report: {report_path}")
    if result.get("error"):
        print(
            f"error: {result['error']['type']} - {result['error']['message']}",
            file=sys.stderr,
        )
    return 0 if result["decision"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
