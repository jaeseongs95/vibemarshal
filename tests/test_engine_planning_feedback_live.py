from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import RevisionStatus, utc_now
from flowmarshal.engine.model_lock import ModelCapability, ModelInventory, RUNTIME_CAPABILITIES
from flowmarshal.engine.planning import plan_review_evidence_catalog, skeleton_review_evidence_catalog
from flowmarshal.engine.runtime import ExecutionPolicyEvidence, RuntimeObservation
from flowmarshal.engine.goal import (
    GoalNormalizationProposal, GoalNormalizerAdapter, GoalPreparationPipeline,
    GoalReviewerAdapter, ReviewDraft,
)
from flowmarshal.engine.goal_feedback import (
    GoalPreparationRefiner, GoalRefinementOutcome, GoalRefinementProposal,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.roles import (
    RoleCallReceipt, RoleCallResult, ScriptedStructuredRoleRunner, make_role_request,
)
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import clean_review, goal, plan, profile, skeleton

from scripts.diagnostics import planning_feedback_live as live


class BlockedPreparation(BaseModel):
    goal_contract: object


class ReadyPreparation(BaseModel):
    goal_contract: object


class FakeRuntime:
    def __init__(self, *, run_root: Path, **_):
        self.run_root = run_root

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def verify_execution_policy(self, cwd):
        return ExecutionPolicyEvidence(
            permission_profile=":danger-full-access", approval_policy="never",
            config_digest="sha256:" + "1" * 64, profile_catalog_digest="sha256:" + "2" * 64,
            cwd=str(Path(cwd).resolve()),
        )

    def capture_call(self, _capture):
        return nullcontext()

    def list_models(self):
        return ModelInventory(
            source="test", executable_digest="sha256:" + "3" * 64,
            runtime_capabilities=RUNTIME_CAPABILITIES,
            models=(
                ModelCapability(model="gpt-5.6-luna", supported_efforts=("high",)),
                ModelCapability(model="gpt-5.6-sol", supported_efforts=("xhigh",)),
                ModelCapability(model="gpt-5.6-terra", supported_efforts=("high",)),
            ),
        )


class ScriptedGenerator:
    def __init__(self, *_args, **_kwargs):
        pass

    def generate(self, *, goal, state, candidate_count, **_):
        base = skeleton(goal, state)
        criterion_id = goal.definition.hard_acceptance[0].criterion_id
        self.candidate = base.model_copy(update={
            "tasks": tuple(item.model_copy(update={"contributes_to": (criterion_id,)}) for item in base.tasks),
            "goal_coverage": tuple(item.model_copy(update={"criterion_id": criterion_id}) for item in base.goal_coverage),
        })
        return (self.candidate,)

    def refine(self, **_):
        raise AssertionError("clean scripted planning은 skeleton refinement를 호출하지 않습니다.")


class ScriptedSkeletonReviewer:
    def __init__(self, *_args, **_kwargs):
        pass

    def review(self, *, candidate, goal, state, project_map):
        return clean_review(
            sha256_digest(candidate), role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, goal, state, project_map),
        )


class ScriptedExpander:
    def __init__(self, *_args, inventory, **_kwargs):
        self.inventory = inventory

    def expand(self, *, candidate, goal, state, project_map, planning_budget, **_):
        revision = plan(
            goal.definition.project_id, goal, state, project_map.revision_digest, candidate, self.inventory,
        )[0]
        criterion_id = goal.definition.hard_acceptance[0].criterion_id
        tasks = tuple(item.model_copy(update={"goal_criterion_refs": (criterion_id,)}) for item in revision.definition.tasks)
        integration = tuple(item.model_copy(update={"criterion_refs": (criterion_id,)}) for item in revision.definition.integration_validations)
        coverage = tuple(item.model_copy(update={"criterion_id": criterion_id}) for item in revision.definition.goal_coverage)
        definition = revision.definition.model_copy(update={
            "planning_budget": planning_budget, "tasks": tasks,
            "integration_validations": integration, "goal_coverage": coverage,
        })
        return revision.model_copy(update={
            "definition": definition, "definition_digest": definition.definition_digest,
        })


class ScriptedPlanReviewer:
    def __init__(self, *_args, **_kwargs):
        pass

    def review(self, *, plan, goal, state, project_map, **_):
        return clean_review(
            plan.activation_digest, role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
        )


def _tree_digest(root: Path) -> str:
    values = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            values.append((item.relative_to(root).as_posix(), hashlib.sha256(item.read_bytes()).hexdigest()))
    return hashlib.sha256(repr(values).encode()).hexdigest()


def _ratings() -> dict[str, int]:
    return {
        "goal_fit": 4, "grounding": 4, "engineering": 4,
        "verification": 4, "execution_safety": 4,
    }


def _original_proposal() -> dict:
    return {
        "mission_class": "feature_extension",
        "observable_outcome": "수정 계획이 수립된다.",
        "hard_acceptance": [{
            "statement": "app.py의 add 결함이 수정된다.",
            "validation_intent": "수정 결과와 기존 검사를 확인한다.",
        }],
        "constraints": [],
        "non_goals": ["관측되지 않은 운영 시스템을 변경하지 않는다."],
        "mutation_policy": "scoped_change",
        "behavior_policy": "preserve_public_contracts",
    }


def _revised_proposal() -> dict:
    value = _original_proposal()
    value.update({
        "observable_outcome": "app.py의 add 결함이 수정되고 기존 검사가 통과한다.",
        "constraints": [{
            "category": "scope",
            "statement": "ProjectProfile의 공개 계약과 기존 검사를 보존한다.",
        }],
        "non_goals": [],
    })
    return value


def _conflict_review() -> dict:
    return {"findings": [{
        "finding_code": "OUTCOME_STAGE_CONFLICT", "gate": "goal", "severity": "error",
        "summary": "observable outcome이 계획 단계에 머뭅니다.",
        "evidence_refs": ["source:user_request", "artifact:goal_proposal"],
        "affected_task_refs": [], "remediable": True,
    }]}


def _schema_mismatch_skeleton() -> dict:
    return {"candidates": [{
        "approach": {
            "strategy_family": "direct", "change_shape": "single",
            "compatibility": "preserve", "rollout_recovery": "bounded retry",
        },
        "tasks": [{
            "task_ref": "task_one", "kind": "inspect",
            "objective": "현재 결함을 확인한다.", "contributes_to": [],
            "produces": ["result:inspection"], "consumes": ["input:app.py"],
            "risk_tags": [], "required_capabilities": [], "no_op_when": [],
            "unknown_refs": [], "detail_requirements": [],
        }],
        "dependencies": [],
        "goal_coverage": [{"criterion_id": "ac_001", "task_refs": ["task_one"]}],
        "unknowns": [], "estimated_change_cost": 1, "estimated_context_tokens": 100,
    }]}


class ResumeRecordedRunner:
    """실제 Goal feedback adapter에만 응답하는 모델 없는 runner."""

    def __init__(self, _runtime, _run_root, _lock):
        refined = GoalNormalizationProposal.model_validate(_revised_proposal()).model_dump(mode="json")
        review = ReviewDraft.model_validate({"findings": [], "ratings": _ratings()}).model_dump(mode="json")
        self.runner = ScriptedStructuredRoleRunner({
            "goal_refiner": [{
                "action": "revision",
                "rationale": "원문과 Profile에 맞춰 Goal 결과를 구체화했습니다.",
                "evidence_refs": [
                    "source:user_request", "source:project_profile",
                    "artifact:goal_proposal", "artifact:goal_review",
                ],
                "proposal": refined,
            }],
            "goal_reviewer": [review],
        })
        self._receipts = []

    def run(self, request, *, validator=None):
        result = self.runner.run(request, validator=validator)
        self._receipts.append(result.receipt)
        return result

    @property
    def receipts(self):
        return tuple(self._receipts)


class PlanningFeedbackLiveTests(unittest.TestCase):
    def _arguments(self, root: Path, temporary: Path):
        temporary.mkdir(parents=True, exist_ok=True)
        roles = temporary / "roles.json"
        shutil.copyfile(
            root / "tests" / "fixtures" / "engine" / "plan-inspection-general-reviewer-sol-xhigh-roles.json",
            roles,
        )
        return type("Arguments", (), {
            "run_root": str(temporary / "new-live-run"),
            "codex_bin": str(Path(sys.executable).resolve()),
            "role_config": str(roles.resolve()), "scenario_id": "S01-single-bugfix",
        })()

    def _original_goal_conflict_run(self, root: Path, temporary: Path, arguments):
        """resume loader가 요구하는, 실제 adapter가 만든 최초 Goal CONFLICT artifact."""
        original = temporary / "original-goal-conflict"
        target = original / "project"
        original.mkdir(parents=True)
        shutil.copytree(root / "tests" / "fixtures" / "engine" / "live-smoke-project", target)
        roles = live.EngineRoleConfiguration.model_validate_json(Path(arguments.role_config).read_text(encoding="utf-8"))
        runtime = FakeRuntime(run_root=original)
        inventory = runtime.list_models()
        catalog = live.PlanningScenarioCatalog.load(root / "tests" / "fixtures" / "engine" / "planning-scenarios.json")
        scenario = next(item for item in catalog.scenarios if item.scenario_id == arguments.scenario_id)
        ledger = SQLiteEngineLedger(original / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=original / "ledger" / "artifacts")
        service = EngineService(ledger)
        service.initialize()
        project_id = service.create_project(name="원본 Goal CONFLICT", root=target)
        profile_revision = live._profile(project_id)
        service.register_profile(profile_revision)
        observed_facts = ({"path": "app.py", "content": "def add(a, b): return a - b"},)
        original_payload = GoalNormalizationProposal.model_validate(_original_proposal()).model_dump(mode="json")
        review_payload = ReviewDraft.model_validate(_conflict_review()).model_dump(mode="json")
        scripted = ScriptedStructuredRoleRunner({
            "goal_normalizer": [original_payload], "goal_reviewer": [review_payload],
        })
        normalizer = GoalNormalizerAdapter(
            scripted, model=roles.normalizer.model, effort=roles.normalizer.effort,
            allowed_fallbacks=roles.normalizer.allowed_fallbacks, inventory=inventory,
            inventory_digest=inventory.inventory_digest, cwd=target,
        )
        reviewer = GoalReviewerAdapter(
            scripted, model=roles.critical_reviewer.model, effort=roles.critical_reviewer.effort,
            allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks, inventory=inventory,
            inventory_digest=inventory.inventory_digest, cwd=target,
        )
        prepared = GoalPreparationPipeline(normalizer, reviewer).prepare(
            project_id=project_id, profile=profile_revision, source_request=scenario.source_request,
            observed_facts=observed_facts,
        )
        self.assertEqual(RevisionStatus.CONFLICT, prepared.goal_contract.status)
        live._write_new(original / "goal-preparation.json", prepared)
        for index, (request, payload, receipt) in enumerate((
            (scripted.calls[0], prepared.proposal.model_dump(mode="json"), prepared.normalizer_receipt),
            (scripted.calls[1], review_payload, prepared.reviewer_receipt),
        ), 1):
            call = original / "calls" / f"{index:02d}-{request.role}"
            live._write_new(call / "request.json", request)
            live._write_new(call / "result.json", RoleCallResult(payload=payload, receipt=receipt))
        role_bytes = Path(arguments.role_config).read_bytes()
        executable = Path(arguments.codex_bin)
        live._write_new(original / "preflight.json", {
            "scenario": scenario, "role_config_digest": live.sha256_bytes(role_bytes),
            "codex_bin_digest": live.sha256_bytes(executable.read_bytes()),
            "project_files": live._project_files(target),
        })
        live._write_new(original / "summary.json", {
            "status": "GOAL_BLOCKED", "role_calls": 2, "plan_activated": False,
        })
        return original, prepared

    def _original_planning_schema_failure(self, root: Path, temporary: Path, arguments):
        """실제 Goal feedback 뒤 구 schema의 첫 Skeleton 실패만 가진 planning 원본."""
        original = temporary / "original-planning-schema-failure"
        target = original / "project"
        original.mkdir(parents=True)
        shutil.copytree(root / "tests" / "fixtures" / "engine" / "live-smoke-project", target)
        roles = live.EngineRoleConfiguration.model_validate_json(Path(arguments.role_config).read_text(encoding="utf-8"))
        inventory = FakeRuntime(run_root=original).list_models()
        scenario = next(item for item in live.PlanningScenarioCatalog.load(
            root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        ).scenarios if item.scenario_id == arguments.scenario_id)
        ledger = SQLiteEngineLedger(original / "ledger" / "flowmarshal-engine.sqlite3", artifact_root=original / "ledger" / "artifacts")
        service = EngineService(ledger)
        service.initialize()
        project_id = service.create_project(name="원본 planning schema 실패", root=target)
        profile_revision = live._profile(project_id)
        service.register_profile(profile_revision)
        facts = ({"path": "app.py", "content": "def add(a, b): return a - b"},)
        refiner_payload = GoalRefinementProposal.model_validate({
            "action": "revision", "rationale": "수정 가능한 Goal 충돌을 원문에 맞춰 보완합니다.",
            "evidence_refs": ["source:user_request", "source:project_profile", "artifact:goal_proposal", "artifact:goal_review"],
            "proposal": GoalNormalizationProposal.model_validate(_revised_proposal()).model_dump(mode="json"),
        }).model_dump(mode="json")
        review_payload = ReviewDraft.model_validate({"findings": [], "ratings": _ratings()}).model_dump(mode="json")
        scripted = ScriptedStructuredRoleRunner({
            "goal_normalizer": [GoalNormalizationProposal.model_validate(_original_proposal()).model_dump(mode="json")],
            "goal_reviewer": [ReviewDraft.model_validate(_conflict_review()).model_dump(mode="json"), review_payload],
            "goal_refiner": [refiner_payload],
        })
        normalizer = GoalNormalizerAdapter(scripted, model=roles.normalizer.model, effort=roles.normalizer.effort,
            allowed_fallbacks=roles.normalizer.allowed_fallbacks, inventory=inventory, inventory_digest=inventory.inventory_digest, cwd=target)
        reviewer = GoalReviewerAdapter(scripted, model=roles.critical_reviewer.model, effort=roles.critical_reviewer.effort,
            allowed_fallbacks=roles.critical_reviewer.allowed_fallbacks, inventory=inventory, inventory_digest=inventory.inventory_digest, cwd=target)
        conflict = GoalPreparationPipeline(normalizer, reviewer).prepare(
            project_id=project_id, profile=profile_revision, source_request=scenario.source_request, observed_facts=facts,
        )
        ancestor = temporary / "ancestor-goal-conflict"
        live._write_new(ancestor / "goal-preparation.json", conflict)
        for number, request, payload, receipt in (
            (1, scripted.calls[0], conflict.proposal.model_dump(mode="json"), conflict.normalizer_receipt),
            (2, scripted.calls[1], ReviewDraft.model_validate(_conflict_review()).model_dump(mode="json"), conflict.reviewer_receipt),
        ):
            call = ancestor / "calls" / f"{number:02d}-{request.role}"
            live._write_new(call / "request.json", request)
            live._write_new(call / "result.json", RoleCallResult(payload=payload, receipt=receipt))
        raw_refinement = GoalPreparationRefiner(normalizer, reviewer).refine(
            previous=conflict, profile=profile_revision, observed_facts=facts,
        )
        raw_prepared = raw_refinement.revised_outcome
        self.assertIsNotNone(raw_prepared)
        refiner_receipt = raw_refinement.refiner_receipt.model_copy(update={
            "thread_id": "thread-goal-refiner", "turn_ids": ("turn-goal-refiner",),
        })
        reviewer_receipt = raw_prepared.reviewer_receipt.model_copy(update={
            "thread_id": "thread-goal-reviewer", "turn_ids": ("turn-goal-reviewer",),
        })
        prepared = raw_prepared.model_copy(update={
            "normalizer_receipt": refiner_receipt,
            "reviewer_receipt": reviewer_receipt,
        })
        refinement = GoalRefinementOutcome.model_validate({
            **raw_refinement.model_dump(mode="python"),
            "refiner_receipt": refiner_receipt,
            "revised_outcome": prepared,
        })
        service.register_goal(conflict.goal_contract, activate=False)
        service.register_goal(prepared.goal_contract)
        live._write_new(original / "goal-preparation.json", prepared)
        live._write_new(original / "goal-refinement.json", refinement)
        for number, request, payload, receipt in (
            (1, scripted.calls[2], refiner_payload, refiner_receipt),
            (2, scripted.calls[3], review_payload, reviewer_receipt),
        ):
            call = original / "calls" / f"{number:02d}-{request.role}"
            live._write_new(call / "request.json", request)
            live._write_new(call / "result.json", RoleCallResult(payload=payload, receipt=receipt))
            live._write_new(call / "terminal.json", RuntimeObservation(
                thread_id=receipt.thread_id, turn_id=receipt.turn_ids[0], active=False,
                terminal_status="completed", final_response=json.dumps(payload, ensure_ascii=False), payload={},
            ))
        old_schema = deepcopy(live.strict_json_output_schema(live.SkeletonBatchDraft.model_json_schema()))
        task_properties = old_schema["$defs"]["SkeletonTaskDraft"]["properties"]
        task_properties["task_ref"].pop("pattern")
        task_properties["objective"].pop("minLength")
        task_properties["objective"].pop("maxLength")
        task_properties["contributes_to"].pop("minItems")
        task_properties["produces"].pop("minItems")
        self.assertNotEqual(
            live.sha256_digest(live.strict_json_output_schema(old_schema)),
            live.sha256_digest(live.strict_json_output_schema(live.SkeletonBatchDraft.model_json_schema())),
        )
        failed_request = make_role_request(role="skeleton_generator", instructions="JSON만 반환", payload={"goal": prepared.goal_contract.definition.model_dump(mode="json")},
            output_schema=old_schema, model=roles.skeleton_generator.model, effort=roles.skeleton_generator.effort,
            inventory=inventory, allowed_fallbacks=roles.skeleton_generator.allowed_fallbacks, inventory_digest=inventory.inventory_digest, cwd=str(target.resolve()))
        failed_receipt = RoleCallReceipt(call_id="model_call_" + "9" * 32, role="skeleton_generator", status="schema_failed",
            model=failed_request.model, effort=failed_request.effort, inventory_digest=failed_request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never", thread_id="thread-schema", turn_ids=("turn-schema",),
            input_digest=failed_request.request_digest, output_schema_digest=live.sha256_digest(live.strict_json_output_schema(old_schema)),
            latency_ms=1, recorded_at=utc_now(), observed_binding=failed_request.operational_binding,
            schema_recovery_attempts=0, error_summary="old schema accepted empty contributes_to")
        failed = original / "calls" / "03-skeleton_generator"
        live._write_new(failed / "request.json", failed_request)
        live._write_new(failed / "failed.json", {"receipts": [failed_receipt]})
        live._write_new(failed / "terminal.json", RuntimeObservation(thread_id="thread-schema", turn_id="turn-schema", active=False,
            terminal_status="completed", final_response=json.dumps(_schema_mismatch_skeleton(), ensure_ascii=False), payload={}))
        live._write_new(failed / "thread.intent.json", {"ephemeral": False})
        live._write_new(failed / "turn.intent.json", {"thread_id": "thread-schema"})
        role_bytes = Path(arguments.role_config).read_bytes()
        executable = Path(arguments.codex_bin)
        live._write_new(original / "preflight.json", {"scenario": scenario, "role_config_digest": live.sha256_bytes(role_bytes),
            "codex_bin_digest": live.sha256_bytes(executable.read_bytes()), "project_root": str(target.resolve()),
            "project_files": live._project_files(target), "prior_provider_calls": 2, "resume_source": {
                "resume_mode": "goal", "run_root": str(ancestor.resolve()),
                "prior_provider_calls": 2, "files": live._project_files(ancestor),
                "outcome_digest": live.sha256_digest(conflict),
            }})
        live._write_new(original / "summary.json", {"status": "FAIL", "error_type": "StructuredRoleError", "plan_activated": False})
        return original, prepared

    def test_blocked_goal_keeps_fixture_and_never_activates(self):
        root = live.project_root()
        fixture = root / "tests" / "fixtures" / "engine" / "live-smoke-project"
        before = _tree_digest(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            run_root = temp / "new-live-run"
            project_id = "project_" + "7" * 32
            profile_revision = profile(project_id)
            blocked_goal = goal(project_id, profile_revision.definition_digest).model_copy(
                update={"status": RevisionStatus.NEEDS_INPUT}
            )
            prepared = BlockedPreparation(goal_contract=blocked_goal)
            arguments = self._arguments(root, temp)
            with patch.object(live, "CaptureRuntime", FakeRuntime), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(live.GoalPreparationPipeline, "prepare", return_value=prepared):
                self.assertEqual(1, live.run(arguments))
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("GOAL_BLOCKED", summary["status"])
            self.assertFalse(summary["plan_activated"])
            self.assertFalse(summary["worker_executed"])
            self.assertTrue((run_root / "ledger" / "flowmarshal-engine.sqlite3").is_file())
            self.assertTrue((run_root / "goal-preparation.json").is_file())
            self.assertTrue((run_root / "role-receipts.json").is_file())
            connection = sqlite3.connect(run_root / "ledger" / "flowmarshal-engine.sqlite3")
            try:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0])
            finally:
                connection.close()
        self.assertEqual(before, _tree_digest(fixture))

    def test_ready_goal_records_search_and_writes_review_without_activation(self):
        root = live.project_root()
        fixture = root / "tests" / "fixtures" / "engine" / "live-smoke-project"
        before = _tree_digest(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)

            def prepare(*, project_id, profile, **_):
                return ReadyPreparation(goal_contract=goal(project_id, profile.definition_digest))

            with patch.object(live, "CaptureRuntime", FakeRuntime), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(live.GoalPreparationPipeline, "prepare", side_effect=prepare), \
                 patch.object(live, "SkeletonGeneratorAdapter", ScriptedGenerator), \
                 patch.object(live, "SkeletonReviewerAdapter", ScriptedSkeletonReviewer), \
                 patch.object(live, "PlanExpanderAdapter", ScriptedExpander), \
                 patch.object(live, "PlanReviewerAdapter", ScriptedPlanReviewer):
                self.assertEqual(0, live.run(arguments))

            run_root = Path(arguments.run_root)
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("PLAN_READY_FOR_USER_REVIEW", summary["status"])
            self.assertFalse(summary["plan_activated"])
            self.assertFalse(summary["worker_executed"])
            self.assertTrue((run_root / "selected-plan.json").is_file())
            review = (run_root / "selected-plan-review.md").read_text(encoding="utf-8")
            self.assertIn(summary["plan_revision_id"], review)
            self.assertIn(summary["activation_digest"], review)
            connection = sqlite3.connect(run_root / "ledger" / "flowmarshal-engine.sqlite3")
            try:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type = 'planning.search_recorded'"
                ).fetchone()[0])
            finally:
                connection.close()
        self.assertEqual(before, _tree_digest(fixture))

    def test_resume_conflict_creates_revision_two_and_user_review_plan_without_activation(self):
        root = live.project_root()
        fixture = root / "tests" / "fixtures" / "engine" / "live-smoke-project"
        fixture_before = _tree_digest(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)
            original, previous = self._original_goal_conflict_run(root, temp, arguments)
            original_before = _tree_digest(original)
            arguments.resume_goal_run = str(original.resolve())
            with patch.object(live, "CaptureRuntime", FakeRuntime), \
                 patch.object(live, "RecordedRunner", ResumeRecordedRunner), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(live, "SkeletonGeneratorAdapter", ScriptedGenerator), \
                 patch.object(live, "SkeletonReviewerAdapter", ScriptedSkeletonReviewer), \
                 patch.object(live, "PlanExpanderAdapter", ScriptedExpander), \
                 patch.object(live, "PlanReviewerAdapter", ScriptedPlanReviewer):
                self.assertEqual(0, live.run(arguments))

            run_root = Path(arguments.run_root)
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            revised = live.GoalPreparationOutcome.model_validate_json(
                (run_root / "goal-preparation.json").read_text(encoding="utf-8")
            )
            self.assertEqual("PLAN_READY_FOR_USER_REVIEW", summary["status"])
            self.assertEqual(RevisionStatus.READY, revised.goal_contract.status)
            self.assertEqual(previous.goal_contract.goal_id, revised.goal_contract.goal_id)
            self.assertEqual(2, revised.goal_contract.revision_no)
            self.assertEqual(previous.goal_contract.goal_revision_id, revised.goal_contract.supersedes_goal_revision_id)
            self.assertEqual(
                previous.goal_contract.definition.source_request,
                revised.goal_contract.definition.source_request,
            )
            self.assertEqual(previous.goal_contract.definition.project_id, revised.goal_contract.definition.project_id)
            self.assertTrue((run_root / "goal-refinement.json").is_file())
            self.assertTrue((run_root / "selected-plan.json").is_file())
            self.assertIn(summary["activation_digest"], (run_root / "selected-plan-review.md").read_text(encoding="utf-8"))
            connection = sqlite3.connect(run_root / "ledger" / "flowmarshal-engine.sqlite3")
            try:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0])
                self.assertEqual(1, connection.execute(
                    "SELECT COUNT(*) FROM history_events WHERE event_type = 'planning.search_recorded'"
                ).fetchone()[0])
            finally:
                connection.close()
            self.assertEqual(original_before, _tree_digest(original))
        self.assertEqual(fixture_before, _tree_digest(fixture))

    def test_resume_rejects_changed_role_input_and_repeated_original_claim(self):
        root = live.project_root()
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)
            original, _ = self._original_goal_conflict_run(root, temp, arguments)
            original_before = _tree_digest(original)
            arguments.resume_goal_run = str(original.resolve())
            Path(arguments.role_config).write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                live.run(arguments)
            self.assertEqual(original_before, _tree_digest(original))

            arguments = self._arguments(root, temp / "second-roles")
            original, _ = self._original_goal_conflict_run(root, temp / "second", arguments)
            arguments.resume_goal_run = str(original.resolve())
            with patch.object(live, "CaptureRuntime", FakeRuntime), \
                 patch.object(live, "RecordedRunner", ResumeRecordedRunner), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(live, "SkeletonGeneratorAdapter", ScriptedGenerator), \
                 patch.object(live, "SkeletonReviewerAdapter", ScriptedSkeletonReviewer), \
                 patch.object(live, "PlanExpanderAdapter", ScriptedExpander), \
                 patch.object(live, "PlanReviewerAdapter", ScriptedPlanReviewer):
                self.assertEqual(0, live.run(arguments))
                repeated = self._arguments(root, temp / "third-roles")
                repeated.resume_goal_run = str(original.resolve())
                with self.assertRaises(FileExistsError):
                    live.run(repeated)
            self.assertEqual("FAIL", json.loads((Path(repeated.run_root) / "summary.json").read_text(encoding="utf-8"))["status"])

    def test_resume_planning_keeps_ready_goal_and_runs_only_new_planning(self):
        root = live.project_root()
        fixture = root / "tests" / "fixtures" / "engine" / "live-smoke-project"
        fixture_before = _tree_digest(fixture)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)
            original, previous = self._original_planning_schema_failure(root, temp, arguments)
            original_before = _tree_digest(original)
            arguments.resume_planning_run = str(original.resolve())
            with patch.object(live, "CaptureRuntime", FakeRuntime), \
                 patch.object(live, "RecordedRunner", ResumeRecordedRunner), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(live, "SkeletonGeneratorAdapter", ScriptedGenerator), \
                 patch.object(live, "SkeletonReviewerAdapter", ScriptedSkeletonReviewer), \
                 patch.object(live, "PlanExpanderAdapter", ScriptedExpander), \
                 patch.object(live, "PlanReviewerAdapter", ScriptedPlanReviewer):
                self.assertEqual(0, live.run(arguments))
            run_root = Path(arguments.run_root)
            resumed = live.GoalPreparationOutcome.model_validate_json((run_root / "goal-preparation.json").read_text(encoding="utf-8"))
            preflight = json.loads((run_root / "preflight.json").read_text(encoding="utf-8"))
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(previous.goal_contract.goal_id, resumed.goal_contract.goal_id)
            self.assertEqual(2, resumed.goal_contract.revision_no)
            self.assertEqual(previous.goal_contract.definition.source_request, resumed.goal_contract.definition.source_request)
            self.assertEqual(9, preflight["planning_role_budget"])
            self.assertEqual(5, preflight["prior_provider_calls"])
            self.assertFalse((run_root / "goal-refinement.json").exists())
            self.assertEqual([], json.loads((run_root / "role-receipts.json").read_text(encoding="utf-8")))
            self.assertEqual("PLAN_READY_FOR_USER_REVIEW", summary["status"])
            connection = sqlite3.connect(run_root / "ledger" / "flowmarshal-engine.sqlite3")
            try:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM plan_revisions").fetchone()[0])
            finally:
                connection.close()
            self.assertEqual(original_before, _tree_digest(original))
        self.assertEqual(fixture_before, _tree_digest(fixture))

    def test_resume_planning_rejects_active_terminal_and_current_schema(self):
        root = live.project_root()
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)
            original, _ = self._original_planning_schema_failure(root, temp, arguments)
            terminal = original / "calls" / "03-skeleton_generator" / "terminal.json"
            value = json.loads(terminal.read_text(encoding="utf-8"))
            terminal.write_text(json.dumps(value | {"active": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                live._load_planning_resume(original.resolve(), temp / "new-terminal", next(
                    item for item in live.PlanningScenarioCatalog.load(root / "tests" / "fixtures" / "engine" / "planning-scenarios.json").scenarios
                    if item.scenario_id == arguments.scenario_id), Path(arguments.role_config).read_bytes(), Path(arguments.codex_bin))

            arguments = self._arguments(root, temp / "schema")
            original, _ = self._original_planning_schema_failure(root, temp / "schema", arguments)
            failed = original / "calls" / "03-skeleton_generator"
            request = live.RoleCallRequest.model_validate_json((failed / "request.json").read_text(encoding="utf-8"))
            current = live.SkeletonBatchDraft.model_json_schema()
            request = request.model_copy(update={"output_schema": current})
            receipt = RoleCallReceipt.model_validate(json.loads((failed / "failed.json").read_text(encoding="utf-8"))["receipts"][-1]).model_copy(update={
                "input_digest": request.request_digest,
                "output_schema_digest": live.sha256_digest(live.strict_json_output_schema(current)),
            })
            failed.joinpath("request.json").write_text(request.model_dump_json(), encoding="utf-8")
            failed.joinpath("failed.json").write_text(json.dumps({"receipts": [receipt.model_dump(mode="json")]}), encoding="utf-8")
            scenario = next(item for item in live.PlanningScenarioCatalog.load(root / "tests" / "fixtures" / "engine" / "planning-scenarios.json").scenarios if item.scenario_id == arguments.scenario_id)
            with self.assertRaisesRegex(ValueError, "수정 직전 provider schema"):
                live._load_planning_resume(original.resolve(), temp / "new-schema", scenario,
                    Path(arguments.role_config).read_bytes(), Path(arguments.codex_bin))

            arguments = self._arguments(root, temp / "malformed")
            original, _ = self._original_planning_schema_failure(root, temp / "malformed", arguments)
            terminal = original / "calls" / "03-skeleton_generator" / "terminal.json"
            value = json.loads(terminal.read_text(encoding="utf-8"))
            terminal.write_text(json.dumps(value | {"final_response": "{"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "terminal 출력이 JSON object"):
                live._load_planning_resume(original.resolve(), temp / "new-malformed", scenario,
                    Path(arguments.role_config).read_bytes(), Path(arguments.codex_bin))

            arguments = self._arguments(root, temp / "unrelated")
            original, _ = self._original_planning_schema_failure(root, temp / "unrelated", arguments)
            failed = original / "calls" / "03-skeleton_generator"
            request = live.RoleCallRequest.model_validate_json((failed / "request.json").read_text(encoding="utf-8"))
            unrelated = {"type": "object", "properties": {"candidates": {"type": "array"}},
                         "required": ["candidates"], "additionalProperties": False}
            request = request.model_copy(update={"output_schema": unrelated})
            receipt = RoleCallReceipt.model_validate(
                json.loads((failed / "failed.json").read_text(encoding="utf-8"))["receipts"][-1]
            ).model_copy(update={
                "input_digest": request.request_digest,
                "output_schema_digest": live.sha256_digest(live.strict_json_output_schema(unrelated)),
            })
            failed.joinpath("request.json").write_text(request.model_dump_json(), encoding="utf-8")
            failed.joinpath("failed.json").write_text(
                json.dumps({"receipts": [receipt.model_dump(mode="json")]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "수정 직전 provider schema"):
                live._load_planning_resume(original.resolve(), temp / "new-unrelated", scenario,
                    Path(arguments.role_config).read_bytes(), Path(arguments.codex_bin))

    def test_resume_planning_rejects_unbound_goal_success_calls(self):
        root = live.project_root()
        for defect in ("recovery", "terminal", "review_request"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                temp = Path(temporary)
                arguments = self._arguments(root, temp)
                original, _ = self._original_planning_schema_failure(root, temp, arguments)
                call = original / "calls" / ("01-goal_refiner" if defect == "recovery" else "02-goal_reviewer")
                if defect == "recovery":
                    result = live.RoleCallResult.model_validate_json(
                        (call / "result.json").read_text(encoding="utf-8")
                    )
                    result = result.model_copy(update={
                        "receipt": result.receipt.model_copy(update={"schema_recovery_attempts": 1})
                    })
                    (call / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
                    expected = "단일 역할 완료"
                elif defect == "terminal":
                    terminal = json.loads((call / "terminal.json").read_text(encoding="utf-8"))
                    terminal["final_response"] = json.dumps({"findings": [], "ratings": None})
                    (call / "terminal.json").write_text(json.dumps(terminal), encoding="utf-8")
                    expected = "단일 역할 완료"
                else:
                    request = live.RoleCallRequest.model_validate_json(
                        (call / "request.json").read_text(encoding="utf-8")
                    )
                    payload = deepcopy(request.payload)
                    payload["evidence_catalog"]["artifact:goal_proposal"]["observable_outcome"] = "다른 입력"
                    request = request.model_copy(update={"payload": payload})
                    result = live.RoleCallResult.model_validate_json(
                        (call / "result.json").read_text(encoding="utf-8")
                    )
                    reviewer_receipt = result.receipt.model_copy(update={
                        "input_digest": request.request_digest,
                    })
                    result = result.model_copy(update={"receipt": reviewer_receipt})
                    (call / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
                    (call / "result.json").write_text(result.model_dump_json(), encoding="utf-8")
                    prepared_path = original / "goal-preparation.json"
                    prepared = live.GoalPreparationOutcome.model_validate_json(
                        prepared_path.read_text(encoding="utf-8")
                    ).model_copy(update={"reviewer_receipt": reviewer_receipt})
                    prepared_path.write_text(prepared.model_dump_json(), encoding="utf-8")
                    refinement_path = original / "goal-refinement.json"
                    refinement = GoalRefinementOutcome.model_validate_json(
                        refinement_path.read_text(encoding="utf-8")
                    )
                    refinement = GoalRefinementOutcome.model_validate({
                        **refinement.model_dump(mode="python"), "revised_outcome": prepared,
                    })
                    refinement_path.write_text(refinement.model_dump_json(), encoding="utf-8")
                    expected = "request가 원본 outcome"
                scenario = next(item for item in live.PlanningScenarioCatalog.load(
                    root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
                ).scenarios if item.scenario_id == arguments.scenario_id)
                with self.assertRaisesRegex(ValueError, expected):
                    live._load_planning_resume(original.resolve(), temp / "new-run", scenario,
                        Path(arguments.role_config).read_bytes(), Path(arguments.codex_bin))

    def test_recorded_runner_uses_request_binding_fallback_and_detects_project_change(self):
        root = live.project_root()
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            arguments = self._arguments(root, temp)
            roles = live.EngineRoleConfiguration.model_validate_json(
                Path(arguments.role_config).read_text(encoding="utf-8")
            )
            runtime = FakeRuntime(run_root=temp)
            project = temp / "project"
            project.mkdir()
            source = project / "app.py"
            source.write_text("value = 1\n", encoding="utf-8")
            executable = Path(arguments.codex_bin)
            lock = {
                "workspace_binding": {"test": "binding"}, "codex_bin": str(executable),
                "codex_bin_digest": live.sha256_bytes(executable.read_bytes()),
                "role_config": arguments.role_config,
                "role_config_digest": live.sha256_bytes(Path(arguments.role_config).read_bytes()),
                "source_manifest": {"source": "digest"},
                "project_files": live._project_files(project),
                "operational_binding": roles.operational_binding(runtime.list_models()),
            }
            request = make_role_request(
                role="plan_refiner", instructions="JSON만 반환", payload={"input": "x"},
                output_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                model=roles.plan_expander.model, effort=roles.plan_expander.effort,
                inventory=runtime.list_models(), allowed_fallbacks=roles.plan_expander.allowed_fallbacks,
                inventory_digest=runtime.list_models().inventory_digest,
                cwd=str(project),
            )
            result = RoleCallResult(
                payload={}, receipt=RoleCallReceipt(
                    call_id="model_call_" + "1" * 32, role=request.role, status="succeeded",
                    model=request.model, effort=request.effort, inventory_digest=request.inventory_digest,
                    permission_profile=":danger-full-access", approval_policy="never", thread_id="thread-test",
                    turn_ids=("turn-test",), input_digest=request.request_digest,
                    output_schema_digest=live.sha256_digest(live.strict_json_output_schema(request.output_schema)),
                    latency_ms=1, recorded_at=utc_now(), observed_binding=request.operational_binding,
                ),
            )
            recorded = live.RecordedRunner(runtime, temp, lock)
            with patch.object(live, "verify_workspace_binding"), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}), \
                 patch.object(recorded.runner, "run", return_value=result) as provider:
                self.assertEqual(result, recorded.run(request))
                provider.assert_called_once()
                source.write_text("value = 2\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "PLANNING_PROJECT_INPUT_CHANGED"):
                    recorded._verify_lock()


if __name__ == "__main__":
    unittest.main()
