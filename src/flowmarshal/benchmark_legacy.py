"""동결 R3.1을 별도 프로세스에서 호출하는 개발 전용 중립 입력 harness.

기존 source·campaign·판정을 수정하지 않는다. Engine 도메인은 이 모듈을 import하지 않는다.
"""
from __future__ import annotations

import argparse
import inspect
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .canonical import sha256_digest
from .planning import r31_role_adapters as adapters
from .planning.domain import ContextSourceSpec, RequestSpec, RequirementSpec, ValidationCapability
from .planning.input import ContextFileRegistration
from .planning.r31_domain import (
    PlanningRole, PlanningRunInput, ProfileSection, ProjectProfileDefinition, SearchOutcomeStatus,
)
from .planning.r31_intent import (
    EffectivePlanningPolicyBuilder, ProjectStateSnapshotBuilder, RequestSpecAssemblyContext, RequirementAnalyzer,
)
from .planning.r31_live_smoke import load_role_instructions
from .planning.r31_models import ModelRolePreference, PolicyVerifiedCodex
from .planning.r31_runtime import build_planning_runtime
from .planning.r31_store import PlanningArtifactRepository, PlanningRunService, ProjectProfileStore


ROLE_MAP = {
    "purpose_resolver": "normalizer", "intent_reviewer": "critical_reviewer",
    "candidate_generator": "skeleton_generator", "hard_gate_reviewer": "general_reviewer",
    "critical_reviewer": "critical_reviewer", "scorer_selector": "general_reviewer",
}


def describe(skill_root: Path) -> dict[str, Any]:
    instructions = load_role_instructions(skill_root)
    schemas = {name: value.model_json_schema() for name, value in vars(adapters).items()
               if inspect.isclass(value) and issubclass(value, BaseModel)
               and value is not BaseModel and not getattr(value, "__parameters__", ())}
    return {
        "prompt_digest": sha256_digest({
            "instructions": vars(instructions), "adapter_builders": inspect.getsource(adapters),
        }),
        "schema_digest": sha256_digest(schemas), "role_map": ROLE_MAP,
    }


class CapturingRepository(PlanningArtifactRepository):
    def __init__(self, root: Path):
        super().__init__(root)
        self.receipts: dict[str, Any] = {}

    def save_model_call_journal_receipt(self, receipt):
        self.receipts[receipt.call_id] = receipt
        return super().save_model_call_journal_receipt(receipt)


class CandidateCapture:
    def __init__(self, wrapped, records, method):
        self.wrapped, self.records, self.method = wrapped, records, method

    def __getattr__(self, name):
        original = getattr(self.wrapped, name)
        if name != self.method:
            return original

        def call(*args, **kwargs):
            output = original(*args, **kwargs)
            self.records.append({"candidate_id": output.value.candidate_id,
                                 "task_count": len(output.value.plan.work_items),
                                 "receipt_ids": [item.call_id for item in output.receipts]})
            return output
        return call


def run_cell(document: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(document["workspace"]).resolve(strict=True)
    state_root = Path(document["state_root"])
    state_root.mkdir(parents=True, exist_ok=False)
    neutral = document["neutral_input"]
    project_id = "project_" + document["neutral_input_digest"][7:39]
    contexts = tuple(ContextSourceSpec.from_text(
        source_id="project-agents" if item["path"] == "AGENTS.md" else f"source-{index}",
        kind="project_instructions" if item["path"] == "AGENTS.md" else "reference",
        path=str(workspace / item["path"]), purpose=f"관찰한 프로젝트 파일 {item['path']}",
        content=item["content"], required_for_all_work_items=item["path"] == "AGENTS.md",
    ) for index, item in enumerate(neutral["files"]))
    request = RequestSpec(
        project_id=project_id, project_name="중립 qualification fixture", project_root=str(workspace),
        project_description=neutral["profile"]["product_goal"], user_request=neutral["source_request"],
        request_summary=neutral["source_request"],
        requirements=(RequirementSpec(requirement_id="request", statement=neutral["source_request"], source="user"),),
        context_sources=contexts,
        available_validations=(ValidationCapability(
            capability_id="python-test", check_type="command",
            description="README에 있는 순수 Python 회귀 함수 실행; 계획 단계에서는 실행하지 않는다.",
            configuration={"argv": [document["python_executable"], "-c",
                                    "from test_app import test_add_returns_sum; test_add_returns_sum()"]},
        ),),
        product_capabilities=("Python 함수와 파일 관측", "프로젝트 내부 테스트", "읽기 전용 분석"),
    )
    section = ProfileSection(source_refs=("neutral-profile",), source_digest=sha256_digest(neutral["profile"]),
                             freshness="current")
    definition = ProjectProfileDefinition(
        product_goal=neutral["profile"]["product_goal"], lifecycle_stage="prototype", criticality="high",
        compatibility_policy="preserve", default_risk_tolerance="balanced",
        architecture=section, validation=section, runtime=section, risk=section, compatibility=section,
    )
    profiles = ProjectProfileStore(state_root)
    profile = profiles.create_revision(project_id, None, definition)
    profile = profiles.activate_revision(profile.profile_revision_id, profile.definition_digest)
    roles = document["roles"]
    preferences = tuple(ModelRolePreference(role=PlanningRole(role),
                                            preferred_model_ids=(roles[binding]["model"],),
                                            preferred_effort=roles[binding]["effort"])
                        for role, binding in ROLE_MAP.items())
    policy_evidence = []
    repository = CapturingRepository(state_root)
    run_service = PlanningRunService(state_root)
    started = time.monotonic()
    runtime = build_planning_runtime(
        client_factory=lambda: PolicyVerifiedCodex(codex_bin=document["codex_bin"], evidence_sink=policy_evidence.append),
        run_service=run_service, artifact_repository=repository, cwd=workspace,
        instructions=load_role_instructions(Path(document["skill_root"])), preferences=preferences,
    )
    records, feasible_times = [], []
    pipeline = runtime.search_service
    pipeline._expander = CandidateCapture(pipeline._expander, records, "expand")
    if pipeline._refiner is not None:
        pipeline._refiner = CandidateCapture(pipeline._refiner, records, "refine")
    search = pipeline._search.search

    def observed_search(*args, **kwargs):
        outcome = search(*args, **kwargs)
        if outcome.status is SearchOutcomeStatus.READY_FOR_REVIEW:
            feasible_times.append(max(1, int((time.monotonic() - started) * 1000)))
        return outcome

    pipeline._search.search = observed_search
    result: dict[str, Any] = {"disposition": "failed", "selected_candidate_id": None}
    try:
        reviewed = runtime.mission_service.resolve(request, profile)
        result["mission"] = reviewed.model_dump(mode="json")
        mission = reviewed.mission_selection
        if mission.mission is None:
            result["disposition"] = "blocked"
        else:
            snapshot = ProjectStateSnapshotBuilder().capture(request, mission)
            # enum section 이름은 모델 입력 생성 전에 typed 계약으로 결속한다.
            from .planning.r31_domain import ProfileSectionName
            context = RequirementAnalyzer().context(
                request.user_request, mission, profile,
                (ProfileSectionName.ARCHITECTURE, ProfileSectionName.COMPATIBILITY, ProfileSectionName.VALIDATION), snapshot,
            )
            reviewed_requirements = runtime.requirement_service.analyze_and_assemble(
                context, RequestSpecAssemblyContext(
                    project_id=project_id, project_name=request.project_name, project_root=str(workspace),
                    project_description=request.project_description, project_instruction_path=str(workspace / "AGENTS.md"),
                    registered_context_sources=tuple(ContextFileRegistration(
                        source_id=item.source_id, kind=item.kind, path=item.path, purpose=item.purpose,
                        required_for_all_work_items=item.required_for_all_work_items,
                    ) for item in contexts if item.source_id != "project-agents"),
                    available_validations=request.available_validations, product_capabilities=request.product_capabilities,
                    planning_limits=request.planning_limits,
                ),
            )
            assembly = reviewed_requirements.assembly
            policy = EffectivePlanningPolicyBuilder().build(profile, assembly.mission_selection,
                                                            requirement_extraction=assembly.extraction_receipt)
            run_input = PlanningRunInput(
                request_spec=assembly.request_spec, profile_revision=profile,
                mission_selection=assembly.mission_selection, mission_review_evidence=reviewed.review_evidence,
                effective_policy=policy, requirement_extraction=assembly.extraction_receipt,
                requirement_review_evidence=reviewed_requirements.review_evidence, project_snapshot=assembly.project_snapshot,
            )
            frozen = run_service.freeze(run_input, "neutral-benchmark:" + run_input.planning_input_digest)
            outcome = pipeline.search(frozen)
            result["planning_outcome"] = outcome.model_dump(mode="json")
            selected = None if outcome.selection_receipt is None else outcome.selection_receipt.selected_candidate_id
            result.update(disposition="selected" if selected else "failed", selected_candidate_id=selected)
    except adapters.RequirementIntentReviewRejected as error:
        result.update(disposition="blocked", blocking_review=error.review.model_dump(mode="json"))
    except Exception as error:
        result.update(disposition="failed", error=type(error).__name__, message=str(error))
    receipts = list(repository.receipts.values())
    for receipt in receipts:
        binding = roles[ROLE_MAP[receipt.role.value]]
        if (receipt.model_id, receipt.reasoning_effort) != (binding["model"], binding["effort"]):
            raise ValueError("R3.1 benchmark의 model lock과 실제 receipt가 다릅니다.")
    result.update(
        receipts=[item.model_dump(mode="json") for item in receipts], candidate_records=records,
        permission_evidence=[item.model_dump(mode="json") for item in policy_evidence],
        latency_ms_to_first_feasible=feasible_times[0] if feasible_times and result["disposition"] == "selected" else None,
        latency_ms_to_disposition=max(1, int((time.monotonic() - started) * 1000)),
        neutral_input_digest=document["neutral_input_digest"],
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="동결 R3.1의 독립 benchmark harness")
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--skill-root")
    parser.add_argument("--request-file")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    result = (describe(Path(arguments.skill_root)) if arguments.describe else
              run_cell(json.loads(Path(arguments.request_file).read_text(encoding="utf-8"))))
    Path(arguments.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
