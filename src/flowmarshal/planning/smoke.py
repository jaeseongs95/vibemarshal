from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..canonical import sha256_digest
from ..core import (
    ActivationSource,
    CoreDomainError,
    FlowMarshalCore,
    PlanDraft,
    ProjectDefinition,
    RequirementCoverage,
    RequirementDisposition,
    SQLiteCoreLedger,
    ValidationDefinition,
    WorkItemDefinition,
)
from .domain import (
    CandidateReadiness,
    ContextSourceKind,
    PlanValidationError,
    PlannerContractError,
    PlannerGenerationRequest,
    PlannerGenerationResult,
    RequestSpec,
    RequirementSource,
    RequirementSpec,
    ValidationCapability,
)
from .input import (
    ContextFileRegistration,
    RequestSpecAssembler,
    RequestSpecAssemblyInput,
)
from .planner import PlannerService


SCHEMA_VERSION = "1.0"
GATE_NAME = "R3"
RESULT_KIND = "flowmarshal_r3_planner_receipt"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _StaticCandidateGenerator:
    def __init__(self, candidate: dict[str, Any], *, case: str) -> None:
        self.candidate = candidate
        self.case = case
        self.requests: list[PlannerGenerationRequest] = []

    def generate(self, request: PlannerGenerationRequest) -> PlannerGenerationResult:
        self.requests.append(request)
        return PlannerGenerationResult(
            payload=self.candidate,
            receipt={"adapter": "r3-static-candidate", "case": self.case},
        )


def _work_item(
    client_ref: str,
    *,
    dependencies: tuple[str, ...] = (),
    expected_changes: tuple[str, ...],
) -> WorkItemDefinition:
    return WorkItemDefinition(
        client_ref=client_ref,
        title=f"R3 {client_ref}",
        objective=f"{client_ref} 범위의 산출물을 독립적으로 구현하고 등록 검사를 통과시킨다.",
        dependencies=dependencies,
        context_sources=("project-agents", "approved-reference"),
        expected_changes=expected_changes,
        out_of_scope=("모델 배정", "계획 활성화", "Worker 실행"),
        deliverables=(expected_changes[0],),
        acceptance_criteria=(f"{client_ref} 관련 등록 검사가 통과한다.",),
        validations=(
            ValidationDefinition(
                criterion_id=f"{client_ref}.tests",
                check_type="command",
                capability_id="python-tests",
                specification={"command_id": "python-unittest"},
            ),
        ),
        execution_requirements={"network": "normal"},
    )


def _build_request(
    *, project_id: str, workspace: Path, agents: Path, reference: Path
) -> RequestSpec:
    return RequestSpecAssembler().build(RequestSpecAssemblyInput(
        project_id=project_id,
        project_name="R3 합성 Planner 프로젝트",
        project_root=str(workspace),
        project_description="요구사항 coverage와 WorkItem DAG 검증용 합성 프로젝트",
        user_request=(
            "기초 기능을 먼저 구현하고 통합 기능을 연결하되 프로젝트 지침을 모든 작업에 적용해줘."
        ),
        request_summary="두 기능을 순서 있는 WorkItem으로 분해하고 검사 계획을 만든다.",
        requirements=(
            RequirementSpec(
                requirement_id="req.foundation",
                statement="기초 기능을 독립적으로 구현한다.",
                source=RequirementSource.USER,
            ),
            RequirementSpec(
                requirement_id="req.integration",
                statement="기초 기능과 연결되는 통합 기능을 구현한다.",
                source=RequirementSource.REFERENCE,
                source_refs=("approved-reference",),
            ),
            RequirementSpec(
                requirement_id="req.instructions",
                statement="프로젝트 지침을 모든 작업에 적용한다.",
                source=RequirementSource.PROJECT_INSTRUCTION,
                source_refs=("project-agents",),
            ),
        ),
        project_instruction_path=str(agents),
        registered_context_sources=(
            ContextFileRegistration(
                source_id="approved-reference",
                kind=ContextSourceKind.EXTERNAL_REFERENCE,
                path=str(reference),
                purpose="사용자가 승인한 외부 요구사항",
            ),
        ),
        available_validations=(
            ValidationCapability(
                capability_id="python-tests",
                check_type="command",
                description="신뢰된 Python unittest 명령",
                configuration={"command_id": "python-unittest"},
            ),
        ),
        product_capabilities=(
            "RequestSpec 구조화",
            "PlanDraft 후보 생성",
            "deterministic coverage validation",
        ),
        out_of_scope=("모델 배정", "계획 활성화", "실제 Codex Runner 호출"),
    ))


def _build_candidate(request: RequestSpec) -> PlanDraft:
    return PlanDraft(
        project_id=request.project_id,
        parent_revision_id=request.parent_revision_id,
        request_summary=request.request_summary,
        request_spec_digest=request.canonical_digest,
        work_items=(
            _work_item("foundation", expected_changes=("src/foundation.py",)),
            _work_item(
                "integration",
                dependencies=("foundation",),
                expected_changes=("src/integration.py",),
            ),
        ),
        requirement_coverage=(
            RequirementCoverage(
                requirement_id="req.foundation",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("foundation",),
                rationale="기초 구현 작업이 직접 충족한다.",
            ),
            RequirementCoverage(
                requirement_id="req.integration",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("integration",),
                rationale="통합 구현과 검사가 직접 충족한다.",
            ),
            RequirementCoverage(
                requirement_id="req.instructions",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("foundation", "integration"),
                rationale="두 작업 모두 project-agents를 필수 입력으로 사용한다.",
            ),
        ),
        planning_notes=("Assignment는 R4에서 추가한다.",),
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


def _invalid_case_codes(
    request: RequestSpec, candidate: dict[str, Any]
) -> dict[str, Any]:
    generator = _StaticCandidateGenerator(candidate, case="invalid-matrix")
    try:
        PlannerService(generator).propose(request)
    except PlanValidationError as exc:
        return {
            "boundary": "deterministic_validation",
            "error_code": exc.code,
            "issue_codes": [issue.code.value for issue in exc.report.issues],
            "report_digest": exc.report.report_digest,
        }
    except PlannerContractError as exc:
        return {"boundary": "strict_schema", "error_code": exc.code}
    raise RuntimeError("실패해야 하는 Planner 후보가 통과했습니다.")


def run_r3_smoke(
    *, project_root: Path, output_dir: Path, run_id: str
) -> dict[str, Any]:
    project_root = project_root.resolve(strict=True)
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
            "generator": "static strict-schema candidate",
            "next_runtime_integration": "R5/R8",
        },
        "checks": {},
        "receipts": {},
        "error": None,
    }
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
        workspace = output_dir / "synthetic-project"
        workspace.mkdir()
        agents = workspace / "AGENTS.md"
        reference = output_dir / "approved-reference.md"
        agents.write_text(
            "모든 사용자 산출물은 한국어로 작성하고 검사를 생략하지 않는다.\n",
            encoding="utf-8",
        )
        reference.write_text(
            "기초 기능을 먼저 구현한 뒤 통합 기능을 연결하고 검사한다.\n",
            encoding="utf-8",
        )
        ledger = SQLiteCoreLedger(output_dir / "core.sqlite3")
        ledger.initialize()
        core = FlowMarshalCore(ledger)
        project_id = core.register_project(
            ProjectDefinition(
                name="R3 합성 프로젝트",
                root=str(workspace),
                context_sources=(str(agents), str(reference)),
                default_validations=("python-tests",),
            )
        )
        request = _build_request(
            project_id=project_id,
            workspace=workspace,
            agents=agents,
            reference=reference,
        )
        candidate = _build_candidate(request)
        generator = _StaticCandidateGenerator(
            candidate.model_dump(mode="json"), case="valid"
        )
        planner = PlannerService(generator)
        outcome = planner.propose(request)
        _require(outcome.validation.valid, "정상 후보가 valid가 아닙니다.")
        _require(
            outcome.validation.readiness is CandidateReadiness.READY_FOR_ASSIGNMENT,
            "정상 후보가 assignment 준비 상태가 아닙니다.",
        )
        _require(
            outcome.validation.coverage.covered_count == len(request.requirements),
            "정상 후보의 요구사항 coverage가 완전하지 않습니다.",
        )
        _require(
            all(item.assignment is None for item in outcome.draft.work_items),
            "Planner 후보에 Assignment가 섞였습니다.",
        )
        _pass(
            result,
            "request_and_context_contract",
            "AGENTS.md·승인 참고자료의 내용과 digest를 정상 Planner 입력으로 구성함",
            request_spec_digest=request.canonical_digest,
            context_sources=[
                {
                    "source_id": source.source_id,
                    "kind": source.kind.value,
                    "content_digest": source.content_digest,
                    "required_for_all_work_items": source.required_for_all_work_items,
                }
                for source in request.context_sources
            ],
        )
        _pass(
            result,
            "valid_plan_candidate",
            "모든 요구사항을 추적하는 strict PlanDraft DAG 후보를 생성·검증함",
            candidate_digest=outcome.draft.canonical_digest,
            validation_report_digest=outcome.validation.report_digest,
            readiness=outcome.validation.readiness.value,
            coverage=outcome.validation.coverage.model_dump(mode="json"),
        )

        base = outcome.draft.model_dump(mode="json")
        missing = copy.deepcopy(base)
        missing["requirement_coverage"] = [
            item
            for item in missing["requirement_coverage"]
            if item["requirement_id"] != "req.integration"
        ]
        cycle = copy.deepcopy(base)
        cycle["work_items"][0]["dependencies"] = ["integration"]
        no_validation = copy.deepcopy(base)
        no_validation["work_items"][0]["validations"] = []
        conflict = copy.deepcopy(base)
        conflict["work_items"][1]["dependencies"] = []
        conflict["work_items"][1]["expected_changes"] = ["src\\foundation.py"]
        invalid_matrix = {
            "missing_requirement": _invalid_case_codes(request, missing),
            "dependency_cycle": _invalid_case_codes(request, cycle),
            "missing_validation": _invalid_case_codes(request, no_validation),
            "unordered_change_conflict": _invalid_case_codes(request, conflict),
        }
        _require(
            "REQUIREMENT_COVERAGE_MISSING"
            in invalid_matrix["missing_requirement"].get("issue_codes", []),
            "누락 requirement가 deterministic validation에서 거부되지 않았습니다.",
        )
        _require(
            invalid_matrix["dependency_cycle"]["error_code"]
            == "PLANNER_OUTPUT_SCHEMA_INVALID",
            "dependency cycle이 strict schema에서 거부되지 않았습니다.",
        )
        _require(
            invalid_matrix["missing_validation"]["error_code"]
            == "PLANNER_OUTPUT_SCHEMA_INVALID",
            "validation 없는 WorkItem이 strict schema에서 거부되지 않았습니다.",
        )
        _require(
            "CHANGE_CONFLICT_UNORDERED"
            in invalid_matrix["unordered_change_conflict"].get("issue_codes", []),
            "동일 변경 대상의 순서 누락이 거부되지 않았습니다.",
        )
        _pass(
            result,
            "deterministic_rejection_matrix",
            "요구사항 누락·cycle·무검증 작업·무순서 변경 충돌을 재현 가능하게 거부함",
            cases=invalid_matrix,
        )

        with ledger.raw_connection() as connection:
            plan_count_before_import = connection.execute(
                "SELECT COUNT(*) FROM plan_revisions"
            ).fetchone()[0]
        _require(
            plan_count_before_import == 0,
            "Planner가 후보 생성 중 Core 원장에 PlanRevision을 만들었습니다.",
        )
        revision_id = core.create_plan_draft(outcome.draft)
        activation_rejected = False
        try:
            core.activate_plan(
                revision_id,
                expected_digest=outcome.draft.canonical_digest,
                source=ActivationSource.CLI,
            )
        except CoreDomainError:
            activation_rejected = True
        with ledger.raw_connection() as connection:
            revision = connection.execute(
                "SELECT status FROM plan_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            project = connection.execute(
                "SELECT active_revision_id FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        _require(activation_rejected, "Assignment 없는 R3 후보가 활성화됐습니다.")
        _require(revision["status"] == "draft", "R3 후보가 draft 상태를 벗어났습니다.")
        _require(
            project["active_revision_id"] is None,
            "R3 후보 생성 뒤 active revision이 생겼습니다.",
        )
        _pass(
            result,
            "candidate_only_boundary",
            "Planner는 원장을 받지 않고 후보만 반환하며 trusted import 뒤에도 draft로 남음",
            plan_count_after_planner=plan_count_before_import,
            imported_revision_id=revision_id,
            imported_revision_status=revision["status"],
            active_revision_id=project["active_revision_id"],
            activation_without_assignment_rejected=activation_rejected,
        )
        result["receipts"] = {
            "request_spec": request.model_dump(mode="json"),
            "plan_candidate": outcome.draft.model_dump(mode="json"),
            "validation_report": outcome.validation.model_dump(mode="json"),
            "generation": {
                "request_digest": outcome.generation_request_digest,
                "receipt": outcome.generation_receipt,
                "call_count": len(generator.requests),
                "output_schema_digest": (
                    sha256_digest(generator.requests[0].output_schema)
                    if generator.requests
                    else None
                ),
            },
            "invalid_matrix": invalid_matrix,
            "core_draft": {
                "database": str(ledger.path),
                "project_id": project_id,
                "revision_id": revision_id,
                "status": revision["status"],
                "active_revision_id": project["active_revision_id"],
            },
        }
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
    finally:
        result["completed_at"] = utc_now()
    return result


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# FlowMarshal R3 Planner",
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
    receipt = result.get("receipts", {})
    candidate = receipt.get("plan_candidate", {})
    validation = receipt.get("validation_report", {})
    if candidate:
        lines.extend(
            [
                "",
                "## 후보 요약",
                "",
                f"- WorkItem 수: `{len(candidate.get('work_items', []))}`",
                f"- requirement coverage 수: `{len(candidate.get('requirement_coverage', []))}`",
                f"- 상태: `{validation.get('readiness', '-')}`",
                f"- 검증 issue 수: `{len(validation.get('issues', []))}`",
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
            "R3는 Planner의 입출력 계약과 deterministic 검사를 합성 generator로 검증한다. "
            "실제 모델 선택은 R4, 사용자 활성화·dispatch는 R5, 전체 실제 Runner 연결은 R8 범위다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(output_dir: Path, result: dict[str, Any]) -> tuple[Path, Path]:
    receipt_path = output_dir / "planner-receipt.json"
    report_path = output_dir / "planner-report.md"
    receipt_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report_path.write_text(render_report(result), encoding="utf-8")
    receipts = result.get("receipts", {})
    for filename, key in (
        ("request-spec.json", "request_spec"),
        ("plan-candidate.json", "plan_candidate"),
        ("validation-report.json", "validation_report"),
    ):
        if key in receipts:
            (output_dir / filename).write_text(
                json.dumps(receipts[key], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return receipt_path, report_path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"r3-{stamp}-{uuid.uuid4().hex[:8]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="FlowMarshal R3 Planner 계약과 거부 matrix를 합성 검증합니다."
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
        / "r3"
        / "artifacts"
        / "runs"
        / run_id
    )
    if output_dir.exists():
        print(f"artifact 디렉터리가 이미 존재합니다: {output_dir}", file=sys.stderr)
        return 2
    result = run_r3_smoke(
        project_root=project_root, output_dir=output_dir, run_id=run_id
    )
    receipt_path, report_path = write_artifacts(output_dir, result)
    print(f"R3 판정: {result['decision']}")
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
