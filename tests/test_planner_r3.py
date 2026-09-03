from __future__ import annotations

import copy
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.core import (
    ActivationSource,
    Assignment,
    ClarificationRequest,
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
from flowmarshal.planning import (
    CandidateReadiness,
    ContextSourceKind,
    ContextSourceSpec,
    ContextFileRegistration,
    PlanIssueCode,
    PlanningLimits,
    PlanValidationError,
    PlannerContractError,
    PlannerGenerationRequest,
    PlannerGenerationResult,
    PlannerService,
    RequestSpec,
    RequestSpecAssembler,
    RequestSpecAssemblyInput,
    RequirementSource,
    RequirementSpec,
    ValidationCapability,
)
import flowmarshal.planning.planner as planner_module
from flowmarshal.planning.smoke import run_r3_smoke


PROJECT_ID = "project_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class CapturingGenerator:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requests: list[PlannerGenerationRequest] = []

    def generate(self, request: PlannerGenerationRequest) -> PlannerGenerationResult:
        self.requests.append(request)
        return PlannerGenerationResult(
            payload=self.payload,
            receipt={"adapter": "unit-static", "candidate": "captured"},
        )


def request_spec(
    *,
    project_id: str = PROJECT_ID,
    project_root: str = "D:/synthetic/project",
    limits: PlanningLimits | None = None,
) -> RequestSpec:
    agents = ContextSourceSpec.from_text(
        source_id="project-agents",
        kind=ContextSourceKind.PROJECT_INSTRUCTIONS,
        path=f"{project_root}/AGENTS.md",
        purpose="모든 작업에 적용할 프로젝트 지침",
        content="모든 변경은 검사하고 문서는 한국어로 작성한다.",
        required_for_all_work_items=True,
    )
    requirements = ContextSourceSpec.from_text(
        source_id="approved-requirements",
        kind=ContextSourceKind.EXTERNAL_REFERENCE,
        path="D:/references/requirements.md",
        purpose="사용자가 승인한 기능 요구사항",
        content="Core를 만들고 연결 검사와 문서화를 수행한다.",
    )
    return RequestSpec(
        project_id=project_id,
        project_name="합성 Planner 프로젝트",
        project_root=project_root,
        project_description="작업 목록 생성과 추적을 검증하는 합성 프로젝트",
        user_request=(
            "Core 기능을 구현하고 연결 검사를 수행하되 프로젝트 지침을 모든 작업에 적용해줘."
        ),
        request_summary="Core 구현과 연결 검사를 작업으로 분해한다.",
        requirements=(
            RequirementSpec(
                requirement_id="req.core",
                statement="Core 기능을 구현한다.",
                source=RequirementSource.USER,
            ),
            RequirementSpec(
                requirement_id="req.integration",
                statement="Core 연결 검사를 수행한다.",
                source=RequirementSource.REFERENCE,
                source_refs=("approved-requirements",),
            ),
            RequirementSpec(
                requirement_id="req.rules",
                statement="프로젝트 지침을 모든 작업에 적용한다.",
                source=RequirementSource.PROJECT_INSTRUCTION,
                source_refs=("project-agents",),
            ),
        ),
        context_sources=(agents, requirements),
        available_validations=(
            ValidationCapability(
                capability_id="python-tests",
                check_type="command",
                description="등록된 Python 단위·통합 검사",
                configuration={"command_id": "python-unittest"},
            ),
        ),
        product_capabilities=("PlanDraft 후보 생성", "deterministic validation"),
        out_of_scope=("모델 배정", "계획 활성화", "Worker 실행"),
        planning_limits=limits or PlanningLimits(),
    )


def work_item(
    client_ref: str,
    *,
    dependencies: tuple[str, ...] = (),
    expected_changes: tuple[str, ...],
    context_sources: tuple[str, ...],
    objective: str | None = None,
    validations: tuple[ValidationDefinition, ...] | None = None,
    assignment: Assignment | None = None,
) -> WorkItemDefinition:
    return WorkItemDefinition(
        client_ref=client_ref,
        title=f"{client_ref} 작업",
        objective=objective or f"{client_ref}의 구체적인 결과를 독립적으로 구현하고 검사한다.",
        dependencies=dependencies,
        context_sources=context_sources,
        expected_changes=expected_changes,
        out_of_scope=("배포",),
        deliverables=(expected_changes[0] if expected_changes else f"{client_ref}.md",),
        acceptance_criteria=(f"{client_ref} 관련 검사가 통과한다.",),
        validations=validations
        or (
            ValidationDefinition(
                criterion_id=f"{client_ref}.tests",
                check_type="command",
                capability_id="python-tests",
                specification={"command_id": "python-unittest"},
            ),
        ),
        execution_requirements={"network": "normal"},
        assignment=assignment,
    )


def valid_draft(request: RequestSpec) -> PlanDraft:
    return PlanDraft(
        project_id=request.project_id,
        parent_revision_id=request.parent_revision_id,
        request_summary=request.request_summary,
        request_spec_digest=request.canonical_digest,
        work_items=(
            work_item(
                "foundation",
                expected_changes=("src/core.py",),
                context_sources=("project-agents", "approved-requirements"),
            ),
            work_item(
                "integration",
                dependencies=("foundation",),
                expected_changes=("tests/test_core.py",),
                context_sources=("project-agents", "approved-requirements"),
            ),
        ),
        requirement_coverage=(
            RequirementCoverage(
                requirement_id="req.core",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("foundation",),
                rationale="Core 구현 작업이 요구사항을 직접 충족한다.",
            ),
            RequirementCoverage(
                requirement_id="req.integration",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("integration",),
                rationale="연결 검사 작업이 요구사항을 직접 충족한다.",
            ),
            RequirementCoverage(
                requirement_id="req.rules",
                disposition=RequirementDisposition.WORK_ITEMS,
                work_item_refs=("foundation", "integration"),
                rationale="두 작업 모두 필수 프로젝트 지침을 입력으로 사용한다.",
            ),
        ),
        planning_notes=("모델과 추론 수준은 R4에서 배정한다.",),
    )


def payload(draft: PlanDraft) -> dict[str, object]:
    return draft.model_dump(mode="json")


def issue_codes(error: PlanValidationError) -> set[PlanIssueCode]:
    return {issue.code for issue in error.report.issues}


class PlannerR3Tests(unittest.TestCase):
    def _assembly_input(
        self,
        workspace: Path,
        *,
        references: tuple[ContextFileRegistration, ...] = (),
    ) -> RequestSpecAssemblyInput:
        return RequestSpecAssemblyInput(
            project_id=PROJECT_ID,
            project_name="입력 조립 테스트",
            project_root=str(workspace),
            project_description="AGENTS.md와 등록 참고자료만 읽는 테스트",
            user_request="프로젝트 지침에 맞게 기능을 구현해줘.",
            request_summary="기능 구현 계획 작성",
            requirements=(
                RequirementSpec(
                    requirement_id="req.feature",
                    statement="기능을 구현한다.",
                    source=RequirementSource.USER,
                ),
                RequirementSpec(
                    requirement_id="req.instructions",
                    statement="프로젝트 지침을 적용한다.",
                    source=RequirementSource.PROJECT_INSTRUCTION,
                    source_refs=("project-agents",),
                ),
            ),
            registered_context_sources=references,
            available_validations=(
                ValidationCapability(
                    capability_id="python-tests",
                    check_type="command",
                    description="Python 검사",
                ),
            ),
        )

    def test_input_assembler_always_loads_agents_and_only_registered_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            agents = workspace / "AGENTS.md"
            secret = workspace / "not-registered.txt"
            reference = root / "approved.md"
            agents.write_text("프로젝트 지침\n", encoding="utf-8")
            secret.write_text("읽으면 안 되는 비등록 자료\n", encoding="utf-8")
            reference.write_text("승인 참고자료\n", encoding="utf-8")
            expected_agents_content = agents.read_bytes().decode("utf-8")
            source = self._assembly_input(
                workspace,
                references=(
                    ContextFileRegistration(
                        source_id="approved-reference",
                        kind=ContextSourceKind.EXTERNAL_REFERENCE,
                        path=str(reference),
                        purpose="사용자 승인 자료",
                    ),
                ),
            )
            request = RequestSpecAssembler().build(source)

        self.assertEqual(
            ["project-agents", "approved-reference"],
            [item.source_id for item in request.context_sources],
        )
        self.assertEqual(expected_agents_content, request.context_sources[0].content)
        self.assertTrue(request.context_sources[0].required_for_all_work_items)
        self.assertNotIn(
            str(secret), [item.path for item in request.context_sources]
        )

    def test_input_assembler_reports_missing_agents_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "project"
            workspace.mkdir()
            with self.assertRaises(PlannerContractError) as caught:
                RequestSpecAssembler().build(self._assembly_input(workspace))
        self.assertEqual("PROJECT_INSTRUCTIONS_MISSING", caught.exception.code)

    def test_input_assembler_rejects_non_utf8_registered_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("지침", encoding="utf-8")
            binary = root / "binary.dat"
            binary.write_bytes(b"\xff\xfe\x00")
            source = self._assembly_input(
                workspace,
                references=(
                    ContextFileRegistration(
                        source_id="binary-reference",
                        kind=ContextSourceKind.EXTERNAL_REFERENCE,
                        path=str(binary),
                        purpose="잘못된 참고자료",
                    ),
                ),
            )
            with self.assertRaises(PlannerContractError) as caught:
                RequestSpecAssembler().build(source)
        self.assertEqual("CONTEXT_SOURCE_NOT_UTF8", caught.exception.code)

    def test_valid_candidate_is_ready_for_assignment_and_schema_is_explicit(self) -> None:
        request = request_spec()
        generator = CapturingGenerator(payload(valid_draft(request)))
        outcome = PlannerService(generator).propose(request)

        self.assertTrue(outcome.validation.valid)
        self.assertEqual(
            CandidateReadiness.READY_FOR_ASSIGNMENT,
            outcome.validation.readiness,
        )
        self.assertEqual(3, outcome.validation.coverage.covered_count)
        self.assertEqual(0, outcome.validation.coverage.missing_count)
        self.assertEqual("unit-static", outcome.generation_receipt["adapter"])
        self.assertEqual(1, len(generator.requests))
        generation = generator.requests[0]
        self.assertEqual(request.canonical_digest, generation.request_spec.canonical_digest)
        self.assertIn("$defs", generation.output_schema)
        self.assertEqual(
            "null",
            generation.output_schema["$defs"]["WorkItemDefinition"]["properties"][
                "assignment"
            ]["type"],
        )
        self.assertTrue(any("AGENTS.md" in item for item in generation.instructions))
        self.assertTrue(any("활성화" in item for item in generation.instructions))
        self.assertTrue(all(item.assignment is None for item in outcome.draft.work_items))
        self.assertEqual(
            outcome.validation.report_digest,
            PlannerService(generator).validate_candidate(
                request, outcome.draft
            ).report_digest,
        )

    def test_strict_schema_rejects_extra_field_empty_validation_and_cycle(self) -> None:
        request = request_spec()
        base = payload(valid_draft(request))

        extra = copy.deepcopy(base)
        extra["planner_can_activate"] = True
        with self.assertRaises(PlannerContractError) as caught:
            PlannerService(CapturingGenerator(extra)).propose(request)
        self.assertEqual("PLANNER_OUTPUT_SCHEMA_INVALID", caught.exception.code)

        unvalidated = copy.deepcopy(base)
        unvalidated["work_items"][0]["validations"] = []  # type: ignore[index]
        with self.assertRaises(PlannerContractError) as caught:
            PlannerService(CapturingGenerator(unvalidated)).propose(request)
        self.assertEqual("PLANNER_OUTPUT_SCHEMA_INVALID", caught.exception.code)

        cyclic = copy.deepcopy(base)
        cyclic["work_items"][0]["dependencies"] = ["integration"]  # type: ignore[index]
        with self.assertRaises(PlannerContractError) as caught:
            PlannerService(CapturingGenerator(cyclic)).propose(request)
        self.assertEqual("PLANNER_OUTPUT_SCHEMA_INVALID", caught.exception.code)

    def test_missing_and_unknown_requirement_coverage_are_rejected(self) -> None:
        request = request_spec()
        candidate = payload(valid_draft(request))
        candidate["requirement_coverage"] = [
            item
            for item in candidate["requirement_coverage"]  # type: ignore[index]
            if item["requirement_id"] != "req.integration"
        ] + [
            {
                "requirement_id": "req.unknown",
                "disposition": "work_items",
                "work_item_refs": ["integration"],
                "rationale": "알 수 없는 요구사항",
                "clarification_id": None,
                "requires_user_confirmation": False,
            }
        ]

        with self.assertRaises(PlanValidationError) as caught:
            PlannerService(CapturingGenerator(candidate)).propose(request)
        self.assertEqual(
            {
                PlanIssueCode.REQUIREMENT_COVERAGE_MISSING,
                PlanIssueCode.REQUIREMENT_COVERAGE_UNKNOWN,
            },
            issue_codes(caught.exception),
        )

    def test_unordered_shared_change_target_is_rejected_but_dependency_orders_it(self) -> None:
        request = request_spec()
        candidate = payload(valid_draft(request))
        candidate["work_items"][1]["expected_changes"] = ["src\\core.py"]  # type: ignore[index]
        candidate["work_items"][1]["dependencies"] = []  # type: ignore[index]
        with self.assertRaises(PlanValidationError) as caught:
            PlannerService(CapturingGenerator(candidate)).propose(request)
        self.assertIn(PlanIssueCode.CHANGE_CONFLICT_UNORDERED, issue_codes(caught.exception))

        candidate["work_items"][1]["dependencies"] = ["foundation"]  # type: ignore[index]
        outcome = PlannerService(CapturingGenerator(candidate)).propose(request)
        self.assertTrue(outcome.validation.valid)

    def test_context_validation_and_assignment_role_boundaries_are_enforced(self) -> None:
        request = request_spec()
        candidate = payload(valid_draft(request))
        first = candidate["work_items"][0]  # type: ignore[index]
        first["context_sources"] = ["approved-requirements", "unknown-source"]
        first["validations"][0]["capability_id"] = "unknown-check"
        first["assignment"] = {
            "execution_model_id": "planner-picked-model",
            "execution_reasoning_effort": "high",
            "validation_model_id": None,
            "validation_reasoning_effort": None,
            "selection_reason": "Planner가 권한을 넘었다.",
            "fallback_policy": None,
        }

        with self.assertRaises(PlanValidationError) as caught:
            PlannerService(CapturingGenerator(candidate)).propose(request)
        self.assertEqual(
            {
                PlanIssueCode.CONTEXT_SOURCE_UNKNOWN,
                PlanIssueCode.PREMATURE_ASSIGNMENT,
                PlanIssueCode.REQUIRED_CONTEXT_MISSING,
                PlanIssueCode.VALIDATION_CAPABILITY_UNKNOWN,
            },
            issue_codes(caught.exception),
        )

    def test_oversized_vague_and_orphan_work_items_are_rejected(self) -> None:
        request = request_spec(
            limits=PlanningLimits(max_requirements_per_work_item=1)
        )
        candidate = payload(valid_draft(request))
        candidate["work_items"][0]["objective"] = "기능 구현"  # type: ignore[index]
        candidate["work_items"].append(  # type: ignore[union-attr]
            work_item(
                "orphan",
                expected_changes=("src/orphan.py",),
                context_sources=("project-agents",),
            ).model_dump(mode="json")
        )

        with self.assertRaises(PlanValidationError) as caught:
            PlannerService(CapturingGenerator(candidate)).propose(request)
        codes = issue_codes(caught.exception)
        self.assertIn(PlanIssueCode.WORK_ITEM_TOO_LARGE, codes)
        self.assertIn(PlanIssueCode.WORK_ITEM_OBJECTIVE_TOO_VAGUE, codes)
        self.assertIn(PlanIssueCode.WORK_ITEM_WITHOUT_REQUIREMENT, codes)

    def test_explicit_exclusion_or_clarification_requires_user_input(self) -> None:
        request = request_spec()
        candidate = payload(valid_draft(request))
        candidate["requirement_coverage"][1] = {  # type: ignore[index]
            "requirement_id": "req.integration",
            "disposition": "clarification",
            "work_item_refs": [],
            "rationale": "지원할 통합 범위를 먼저 정해야 한다.",
            "clarification_id": "clarify.integration-scope",
            "requires_user_confirmation": True,
        }
        candidate["clarifications"] = [
            {
                "clarification_id": "clarify.integration-scope",
                "question": "통합 검사의 대상 환경은 무엇인가요?",
                "impact": "선택에 따라 integration WorkItem의 완료 조건이 달라집니다.",
                "requirement_ids": ["req.integration"],
                "blocking": True,
            }
        ]
        candidate["work_items"] = [candidate["work_items"][0]]  # type: ignore[index]
        candidate["requirement_coverage"][2]["work_item_refs"] = ["foundation"]  # type: ignore[index]

        outcome = PlannerService(CapturingGenerator(candidate)).propose(request)
        self.assertTrue(outcome.validation.valid)
        self.assertEqual(
            CandidateReadiness.NEEDS_USER_INPUT, outcome.validation.readiness
        )
        self.assertEqual(1, outcome.validation.coverage.clarification_count)

    def test_request_spec_rejects_tampered_context_and_unknown_source_reference(self) -> None:
        request = request_spec()
        document = request.model_dump(mode="json")
        document["context_sources"][0]["content"] = "변조된 지침"
        with self.assertRaises(ValidationError):
            RequestSpec.model_validate(document)

        document = request.model_dump(mode="json")
        document["requirements"][1]["source_refs"] = ["missing-source"]
        with self.assertRaises(ValidationError):
            RequestSpec.model_validate(document)

    def test_candidate_is_bound_to_exact_original_user_request(self) -> None:
        original = request_spec()
        candidate = valid_draft(original)
        changed_document = original.model_dump(mode="json")
        changed_document["user_request"] = "서로 다른 사용자 요청"
        changed = RequestSpec.model_validate(changed_document)

        with self.assertRaises(PlanValidationError) as caught:
            PlannerService(CapturingGenerator(payload(candidate))).propose(changed)
        self.assertIn(
            PlanIssueCode.REQUEST_DIGEST_MISMATCH, issue_codes(caught.exception)
        )

    def test_planner_has_no_core_activation_capability_and_import_stays_draft(self) -> None:
        source = inspect.getsource(planner_module)
        self.assertNotIn("activate_plan", source)
        self.assertNotIn("FlowMarshalCore", source)
        self.assertNotIn("SQLiteCoreLedger", source)
        signature = inspect.signature(PlannerService.__init__)
        self.assertEqual(("self", "generator", "validator"), tuple(signature.parameters))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "project"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
            ledger = SQLiteCoreLedger(root / "state" / "core.sqlite3")
            ledger.initialize()
            core = FlowMarshalCore(ledger)
            project_id = core.register_project(
                ProjectDefinition(
                    name="Planner 경계",
                    root=str(workspace),
                    context_sources=(str(workspace / "AGENTS.md"),),
                )
            )
            request = request_spec(
                project_id=project_id, project_root=str(workspace)
            )
            outcome = PlannerService(
                CapturingGenerator(payload(valid_draft(request)))
            ).propose(request)
            with ledger.raw_connection() as connection:
                self.assertEqual(
                    0,
                    connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0],
                )

            revision_id = core.create_plan_draft(outcome.draft)
            with ledger.raw_connection() as connection:
                revision = connection.execute(
                    "SELECT status FROM plan_revisions WHERE id = ?", (revision_id,)
                ).fetchone()
                project = connection.execute(
                    "SELECT active_revision_id FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
            self.assertEqual("draft", revision["status"])
            self.assertIsNone(project["active_revision_id"])
            with self.assertRaises(CoreDomainError):
                core.activate_plan(
                    revision_id,
                    expected_digest=outcome.draft.canonical_digest,
                    source=ActivationSource.CLI,
                )

    def test_r3_smoke_is_go_and_keeps_candidate_in_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = run_r3_smoke(
                project_root=root,
                output_dir=root / "r3-artifact",
                run_id="r3-unit-smoke",
            )

        self.assertEqual("GO", result["decision"])
        self.assertIsNone(result["error"])
        self.assertEqual(
            "ready_for_assignment",
            result["receipts"]["validation_report"]["readiness"],
        )
        self.assertEqual("draft", result["receipts"]["core_draft"]["status"])
        self.assertIsNone(
            result["receipts"]["core_draft"]["active_revision_id"]
        )
        self.assertIn(
            "REQUIREMENT_COVERAGE_MISSING",
            result["receipts"]["invalid_matrix"]["missing_requirement"][
                "issue_codes"
            ],
        )


if __name__ == "__main__":
    unittest.main()
