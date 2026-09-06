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
    GoalNormalizationProposal, GoalNormalizerAdapter, GoalPreparationOutcome, GoalPreparationPipeline,
    GoalReviewerAdapter, ReviewDraft,
)
from flowmarshal.engine.goal_feedback import (
    GoalPreparationRefiner, GoalRefinementOutcome, GoalRefinementProposal,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.roles import (
    RoleCallReceipt, RoleCallResult, ScriptedStructuredRoleRunner, StructuredRoleError,
    make_role_request,
)
from flowmarshal.engine.role_execution import RoleTimeoutOverride, RoleTimeoutPolicy
from flowmarshal.engine.planner_roles import PlanReviewEnvelopeV2
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


class KnownUsageScriptedRoleRunner(ScriptedStructuredRoleRunner):
    """합성 legacy 계보에 귀속 가능한 단일 turn 사용량을 기록한다."""

    def run(self, request, *, validator=None):
        result = super().run(request, validator=validator)
        number = len(self.calls)
        return result.model_copy(update={
            "receipt": result.receipt.model_copy(update={
                "thread_id": f"thread-known-{number}",
                "turn_ids": (f"turn-known-{number}",),
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 5,
                "reasoning_tokens": 1,
                "usage_available": True,
            }),
        })


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
    def test_v3_timeout_continuation_imports_nine_calls_and_blocks_unknown_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            arguments, source, failed, policy = self._synthetic_v3_timeout_continuation(
                temporary_path
            )
            source_before = _tree_digest(source)

            class ContinuationRuntime(FakeRuntime):
                def read(self, *, thread_id):
                    return RuntimeObservation(
                        thread_id=thread_id, turn_id=failed.turn_ids[0], active=False,
                        terminal_status="interrupted", final_response=None, payload={},
                    )

            run_root = temporary_path / "continuation-preview"
            timeout_path = temporary_path / "role-timeouts.json"
            budget_path = temporary_path / "budget.json"
            timeout_path.write_text(policy.model_dump_json(), encoding="utf-8")
            budget_path.write_text(GoalBudgetPolicy(
                total_tokens=1_000_000, call_reservation_tokens=100_000,
                replan_reserve_percent=25,
            ).model_dump_json(), encoding="utf-8")
            arguments.run_root = str(run_root.resolve())
            arguments.role_timeout_policy = str(timeout_path.resolve())
            arguments.budget_policy = str(budget_path.resolve())
            arguments.timeout_usage_adjustment = None
            arguments.resume_goal_run = None
            arguments.resume_planning_run = None
            arguments.continue_planning_run = str(source.resolve())
            with patch.object(live, "CaptureRuntime", ContinuationRuntime), \
                 patch.object(live, "capture_workspace_binding", return_value={"test": "binding"}), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}):
                self.assertEqual(1, live.run(arguments))
            summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("BUDGET_USAGE_UNKNOWN", summary["status"])
            self.assertEqual(9, summary["prior_provider_calls"])
            self.assertEqual(5, summary["remaining_provider_calls"])
            self.assertFalse(summary["claim_created"])
            database = sqlite3.connect(run_root / "ledger" / "flowmarshal-engine.sqlite3")
            try:
                self.assertEqual(3, database.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(2, database.execute("SELECT COUNT(*) FROM goal_revisions").fetchone()[0])
                self.assertEqual(9, database.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0])
                self.assertEqual(
                    [("settled", 8), ("usage_unknown", 1)],
                    database.execute(
                        "SELECT status, COUNT(*) FROM provider_calls GROUP BY status ORDER BY status"
                    ).fetchall(),
                )
            finally:
                database.close()
            self.assertFalse((run_root / "calls").exists())
            self.assertFalse((run_root / "project").exists())
            self.assertEqual(source_before, _tree_digest(source))

    def test_v3_timeout_checkpoint_is_observed_and_preserves_five_remaining_calls(self):
        root = live.project_root()
        scenario = next(item for item in live.PlanningScenarioCatalog.load(
            root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        ).scenarios if item.scenario_id == "S01-single-bugfix")
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            arguments, source, failed, policy = self._synthetic_v3_timeout_continuation(
                temporary_path
            )
            source_before = _tree_digest(source)
            ancestor = Path(json.loads(
                (source / "preflight.json").read_text(encoding="utf-8")
            )["resume_source"]["run_root"])
            ancestor_before = _tree_digest(ancestor)

            class TerminalRuntime:
                def read(self, *, thread_id):
                    self.thread_id = thread_id
                    return RuntimeObservation(
                        thread_id=thread_id, turn_id=failed.turn_ids[0], active=False,
                        terminal_status="interrupted", final_response=None, payload={},
                    )

            run_root = temporary_path / "new-run"
            run_root.mkdir()
            result = live._load_planning_continuation(
                source.resolve(), run_root, scenario,
                Path(arguments.role_config).read_bytes(),
                Path(arguments.codex_bin), TerminalRuntime(), policy,
            )
            target, resume_source, checkpoint = result[2], result[-2], result[-1]
            self.assertEqual(9, resume_source["prior_provider_calls"])
            self.assertEqual(5, resume_source["remaining_provider_calls"])
            self.assertEqual(policy.policy_digest,
                             resume_source["timeout_policy_transition"]["new_policy_digest"])
            self.assertEqual("compact_plan_reviewer",
                             json.loads((source / "calls" / "04-compact_plan_reviewer" / "request.json").read_text(encoding="utf-8"))["role"])
            self.assertEqual(9, len(checkpoint.source_requests))
            self.assertEqual(9, len(checkpoint.source_receipts))
            self.assertEqual(9, len(checkpoint.import_entries))
            self.assertEqual("timed_out", checkpoint.timed_out_receipt.status)
            self.assertTrue((run_root / "source-terminal-observation.json").is_file())
            changed = target / "app.py"
            original_bytes = changed.read_bytes()
            changed.write_bytes(original_bytes + b"\n# changed\n")
            try:
                with self.assertRaisesRegex(ValueError, "대상 프로젝트가 변경"):
                    live._load_planning_continuation(
                        source.resolve(), temporary_path / "changed-run", scenario,
                        Path(arguments.role_config).read_bytes(),
                        Path(arguments.codex_bin), TerminalRuntime(), policy,
                    )
            finally:
                changed.write_bytes(original_bytes)
            self.assertEqual(source_before, _tree_digest(source))
            self.assertEqual(ancestor_before, _tree_digest(ancestor))

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

    def _synthetic_v3_timeout_continuation(self, temporary: Path):
        """host 운영 artifact 없이 v1→v2→v3의 검증된 9호출 계보를 만든다."""
        root = live.project_root()
        arguments = self._arguments(root, temporary / "configuration")
        v2, prepared = self._original_planning_schema_failure(
            root, temporary / "lineage", arguments, known_usage=True,
        )
        v1 = temporary / "lineage" / "ancestor-goal-conflict"

        # v1 성공 호출도 실제 checkpoint와 같은 단일 completed terminal을 갖는다.
        for call in sorted((v1 / "calls").iterdir()):
            result = RoleCallResult.model_validate_json(
                (call / "result.json").read_text(encoding="utf-8")
            )
            live._write_new(call / "terminal.json", RuntimeObservation(
                thread_id=result.receipt.thread_id,
                turn_id=result.receipt.turn_ids[0],
                active=False,
                terminal_status="completed",
                final_response=json.dumps(result.payload, ensure_ascii=False),
                payload={},
            ))

        # v2 failure는 앞선 두 성공 receipt와 schema failure receipt를 함께 보존한다.
        v2_calls = sorted((v2 / "calls").iterdir())
        successful_v2_receipts = [
            RoleCallResult.model_validate_json(
                (call / "result.json").read_text(encoding="utf-8")
            ).receipt
            for call in v2_calls[:2]
        ]
        failed_v2_path = v2_calls[2] / "failed.json"
        failed_v2 = RoleCallReceipt.model_validate(json.loads(
            failed_v2_path.read_text(encoding="utf-8")
        )["receipts"][-1])
        failed_v2_path.write_text(json.dumps({
            "receipts": [
                item.model_dump(mode="json")
                for item in (*successful_v2_receipts, failed_v2)
            ],
        }, ensure_ascii=False), encoding="utf-8")

        refinement = GoalRefinementOutcome.model_validate_json(
            (v2 / "goal-refinement.json").read_text(encoding="utf-8")
        )
        prepared = GoalPreparationOutcome.model_validate_json(
            (v2 / "goal-preparation.json").read_text(encoding="utf-8")
        )
        v2_preflight_path = v2 / "preflight.json"
        v2_preflight = json.loads(v2_preflight_path.read_text(encoding="utf-8"))
        v2_preflight.update({
            "maximum_provider_calls": 12,
            "resume_source": {
                "resume_mode": "goal",
                "run_root": str(v1.resolve()),
                "prior_provider_calls": 2,
                "files": live._artifact_files(v1),
                "outcome_digest": live.sha256_digest(refinement.original_outcome),
            },
        })
        v2_preflight_path.write_text(
            json.dumps(v2_preflight, ensure_ascii=False), encoding="utf-8"
        )

        target = Path(v2_preflight["project_root"])
        project_id = prepared.goal_contract.definition.project_id
        with SQLiteEngineLedger(
            v2 / "ledger" / "flowmarshal-engine.sqlite3",
            artifact_root=v2 / "ledger" / "artifacts",
        ).read() as connection:
            profile_revision = live.ProjectProfileRevision.model_validate_json(
                connection.execute(
                    "SELECT payload_json FROM profile_revisions WHERE project_id=?",
                    (project_id,),
                ).fetchone()["payload_json"]
            )

        v3 = temporary / "synthetic-v3-timeout"
        v3.mkdir()
        v3_ledger = SQLiteEngineLedger(
            v3 / "ledger" / "flowmarshal-engine.sqlite3",
            artifact_root=v3 / "ledger" / "artifacts",
        )
        service = EngineService(v3_ledger)
        service.initialize()
        service.create_project(name="합성 v3 planning timeout", root=target, project_id=project_id)
        service.register_profile(profile_revision)
        service.register_goal(refinement.original_outcome.goal_contract, activate=False)
        service.register_goal(prepared.goal_contract)
        project_map = live.ProjectMapper().build(
            project_id=project_id, root=target, revision_no=1,
        )
        project_map, state = service.reobserve_project(project_id)

        inventory = FakeRuntime(run_root=v3).list_models()
        roles = live.EngineRoleConfiguration.model_validate_json(
            Path(arguments.role_config).read_text(encoding="utf-8")
        )
        criterion_id = prepared.goal_contract.definition.hard_acceptance[0].criterion_id
        candidate = ScriptedGenerator().generate(
            goal=prepared.goal_contract, state=state, candidate_count=1,
        )[0]
        planning_budget = live.PlanningBudgetPolicy(max_logical_role_calls=9)
        detailed_plan = ScriptedExpander(inventory=inventory).expand(
            candidate=candidate, goal=prepared.goal_contract, state=state,
            project_map=project_map, planning_budget=planning_budget,
        )

        def request(role, role_config, payload, schema, *, timeout_seconds=900):
            return make_role_request(
                role=role, instructions="합성 checkpoint JSON만 반환한다.",
                payload=payload, output_schema=schema,
                model=role_config.model, effort=role_config.effort,
                inventory_digest=inventory.inventory_digest,
                cwd=str(target.resolve()), timeout_seconds=timeout_seconds,
            )

        skeleton_payload = {
            "candidates": [{
                "approach": candidate.approach.model_dump(mode="json"),
                "tasks": [{
                    "task_ref": item.task_ref, "kind": item.kind,
                    "objective": item.objective,
                    "contributes_to": list(item.contributes_to),
                    "produces": list(item.produces), "consumes": list(item.consumes),
                    "risk_tags": list(item.risk_tags),
                    "required_capabilities": list(item.required_capabilities),
                    "no_op_when": list(item.no_op_when),
                    "unknown_refs": list(item.unknown_refs),
                    "detail_requirements": list(item.detail_requirements),
                } for item in candidate.tasks],
                "dependencies": [],
                "goal_coverage": [item.model_dump(mode="json") for item in candidate.goal_coverage],
                "unknowns": list(candidate.unknowns),
                "estimated_change_cost": candidate.estimated_change_cost,
                "estimated_context_tokens": candidate.estimated_context_tokens,
            }],
        }
        generator_request = request(
            "skeleton_generator", roles.skeleton_generator, {"goal": "synthetic"},
            live.SkeletonBatchDraft.model_json_schema(),
        )
        skeleton_catalog = {
            "artifact:skeleton": candidate.model_dump(mode="json"),
            "source:goal": prepared.goal_contract.definition.model_dump(mode="json"),
        }
        skeleton_review_payload = {"findings": [], "ratings": _ratings()}
        skeleton_review_request = request(
            "skeleton_reviewer", roles.general_reviewer,
            {"evidence_catalog": skeleton_catalog}, ReviewDraft.model_json_schema(),
        )
        plan_draft = {
            "tasks": [{
                "task_ref": "task_one", "kind": "change",
                "objective": "요구를 구현한다.",
                "goal_criterion_refs": [criterion_id],
                "produces": ["result:one"], "consumes": ["input:request"],
                "acceptance_criteria": ["Task validation이 PASS다."],
                "validations": [{
                    "validation_id": "validation_task",
                    "statement": "Task 결과를 확인한다.",
                    "method": "deterministic", "required_evidence_kinds": ["test"],
                }],
                "risk_level": "low",
                "recovery": {"retryable_failure_classes": ["implementation"]},
            }],
            "dependencies": [],
            "goal_coverage": [{
                "criterion_id": criterion_id, "task_refs": ["task_one"],
                "validation_ids": ["validation_task", "validation_goal"],
            }],
            "integration_validations": [{
                "validation_id": "validation_goal", "statement": "Goal을 확인한다.",
                "criterion_refs": [criterion_id], "method": "deterministic",
                "required_evidence_kinds": ["test"],
            }],
        }
        expansion_payload = {
            "inspection": {
                "validation_rows": [{
                    "validation_id": "validation_task",
                    "mechanisms": [{
                        "mechanism_id": "mechanism_1", "tool": "unittest",
                        "phase": None, "direct_refs": ["citation_1"],
                    }],
                }],
                "validation_scope_rows": [{
                    "scope_id": "scope_1", "validation_id": "validation_task",
                    "mechanism_id": "mechanism_1", "claim": "합성 검사를 실행한다.",
                    "direct_extra_refs": [], "status": "supported",
                }],
                "ac_scope_requirements": [{
                    "criterion_id": criterion_id, "statement_scope_ids": [],
                    "validation_intent_scope_ids": ["scope_1"],
                }],
                "constraint_task_rows": [],
            },
            "plan": plan_draft,
        }
        live.PlanExpansionEnvelopeV2.model_validate(expansion_payload)
        expander_request = request(
            "plan_expander", roles.plan_expander,
            {"skeleton": candidate.model_dump(mode="json")},
            live.PlanExpansionEnvelopeV2.model_json_schema(),
        )
        review_catalog = plan_review_evidence_catalog(
            detailed_plan, prepared.goal_contract, state, project_map,
        )
        reviewer_request = request(
            "compact_plan_reviewer", roles.general_reviewer,
            {
                "case_ref": "case-" + detailed_plan.activation_digest.split(":", 1)[1][:16],
                "evidence_catalog": review_catalog,
            },
            PlanReviewEnvelopeV2.model_json_schema(), timeout_seconds=900,
        )

        def success_result(call_request, payload, number):
            receipt = RoleCallReceipt(
                call_id=f"model_call_{number:032d}", role=call_request.role,
                status="succeeded", model=call_request.model, effort=call_request.effort,
                inventory_digest=call_request.inventory_digest,
                permission_profile=":danger-full-access", approval_policy="never",
                thread_id=f"thread-planning-{number}", turn_ids=(f"turn-planning-{number}",),
                input_digest=call_request.request_digest,
                output_digest=live.sha256_digest(payload),
                output_schema_digest=live.sha256_digest(
                    live.strict_json_output_schema(call_request.output_schema)
                ),
                input_tokens=10, cached_input_tokens=0, output_tokens=5,
                reasoning_tokens=1, usage_available=True, latency_ms=1,
                recorded_at=utc_now(),
            )
            return RoleCallResult(payload=payload, receipt=receipt)

        successful = (
            (generator_request, success_result(generator_request, skeleton_payload, 101)),
            (skeleton_review_request, success_result(
                skeleton_review_request, skeleton_review_payload, 102,
            )),
            (expander_request, success_result(expander_request, expansion_payload, 103)),
        )
        for number, (call_request, result) in enumerate(successful, 1):
            call = v3 / "calls" / f"{number:02d}-{call_request.role}"
            live._write_new(call / "request.json", call_request)
            live._write_new(call / "result.json", result)
            live._write_new(call / "terminal.json", RuntimeObservation(
                thread_id=result.receipt.thread_id,
                turn_id=result.receipt.turn_ids[0], active=False,
                terminal_status="completed",
                final_response=json.dumps(result.payload, ensure_ascii=False), payload={},
            ))

        failed_receipt = RoleCallReceipt(
            call_id="model_call_" + "8" * 32, role=reviewer_request.role,
            status="timed_out", model=reviewer_request.model, effort=reviewer_request.effort,
            inventory_digest=reviewer_request.inventory_digest,
            permission_profile=":danger-full-access", approval_policy="never",
            thread_id="thread-timeout", turn_ids=("turn-timeout",),
            input_digest=reviewer_request.request_digest,
            output_schema_digest=live.sha256_digest(
                live.strict_json_output_schema(reviewer_request.output_schema)
            ),
            input_tokens=None, cached_input_tokens=None, output_tokens=None,
            reasoning_tokens=None, usage_available=False, latency_ms=900_000,
            recorded_at=utc_now(), error_summary="role turn timeout",
        )
        failed_call = v3 / "calls" / "04-compact_plan_reviewer"
        live._write_new(failed_call / "request.json", reviewer_request)
        live._write_new(failed_call / "failed.json", {
            "error": "role turn timeout",
            "receipts": [
                item.receipt.model_dump(mode="json") for _, item in successful
            ] + [failed_receipt.model_dump(mode="json")],
        })

        live._write_new(v3 / "goal-preparation.json", prepared)
        live._write_new(v3 / "preflight.json", {
            "scenario": next(item for item in live.PlanningScenarioCatalog.load(
                root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
            ).scenarios if item.scenario_id == arguments.scenario_id),
            "role_config": str(Path(arguments.role_config).resolve()),
            "role_config_digest": live.sha256_bytes(Path(arguments.role_config).read_bytes()),
            "codex_bin": str(Path(arguments.codex_bin).resolve()),
            "codex_bin_digest": live.sha256_bytes(Path(arguments.codex_bin).read_bytes()),
            "project_root": str(target.resolve()),
            "project_files": live._project_files(target),
            "prior_provider_calls": 5, "maximum_provider_calls": 9,
            "resume_source": {
                "resume_mode": "planning", "run_root": str(v2.resolve()),
                "prior_provider_calls": 5, "files": live._artifact_files(v2),
                "outcome_digest": live.sha256_digest(prepared),
            },
        })
        live._write_new(v3 / "summary.json", {
            "status": "FAIL", "error_type": "StructuredRoleError",
            "error": "role turn timeout", "plan_activated": False,
        })
        connection = sqlite3.connect(v3 / "ledger" / "flowmarshal-engine.sqlite3")
        try:
            connection.execute(
                "UPDATE schema_meta SET value='2' WHERE key='schema_revision'"
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
        finally:
            connection.close()
        policy = RoleTimeoutPolicy(overrides=(RoleTimeoutOverride(
            role="compact_plan_reviewer", timeout_seconds=1800,
            replaces_timeout_seconds=900,
            reason="합성 900초 timeout 사후 관측에 따른 운영 변경",
        ),))
        return arguments, v3, failed_receipt, policy

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

    def _original_planning_schema_failure(
        self, root: Path, temporary: Path, arguments, *, known_usage: bool = False,
    ):
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
        runner_class = KnownUsageScriptedRoleRunner if known_usage else ScriptedStructuredRoleRunner
        scripted = runner_class({
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
            input_tokens=10 if known_usage else 0, cached_input_tokens=0,
            output_tokens=5 if known_usage else 0, reasoning_tokens=1 if known_usage else 0,
            usage_available=known_usage, latency_ms=1, recorded_at=utc_now(), observed_binding=failed_request.operational_binding,
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
            original = temp / "original"
            original.mkdir()
            (original / "ledger.sqlite3").write_bytes(b"closed-ledger")
            (original / "ledger.sqlite3-wal").write_bytes(b"")
            (original / "ledger.sqlite3-shm").write_bytes(b"transport-state")
            executable = Path(arguments.codex_bin)
            lock = {
                "workspace_binding": {"test": "binding"}, "codex_bin": str(executable),
                "codex_bin_digest": live.sha256_bytes(executable.read_bytes()),
                "role_config": arguments.role_config,
                "role_config_digest": live.sha256_bytes(Path(arguments.role_config).read_bytes()),
                "source_manifest": {"source": "digest"},
                "project_files": live._project_files(project),
                "operational_binding": roles.operational_binding(runtime.list_models()),
                "resume_source": {
                    "resume_mode": "plan_review_timeout_continuation",
                    "run_root": str(original), "files": live._artifact_files(original),
                },
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
                capped = live.RecordedRunner(runtime, temp, lock | {"maximum_provider_calls": 0})
                with self.assertRaises(StructuredRoleError) as capped_error:
                    capped.run(request)
                self.assertFalse(capped_error.exception.effects_started)
                self.assertIn("MAXIMUM_PROVIDER_CALLS_EXCEEDED", str(capped_error.exception))
                self.assertEqual(result, recorded.run(request))
                provider.assert_called_once()
                source.write_text("value = 2\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "PLANNING_PROJECT_INPUT_CHANGED"):
                    recorded._verify_lock()
                with self.assertRaises(StructuredRoleError) as raised:
                    recorded.run(request)
                self.assertFalse(raised.exception.effects_started)
                self.assertEqual(1, provider.call_count)
                self.assertFalse((temp / "calls" / "02-plan_refiner").exists())

    def test_artifact_snapshot_only_ignores_zero_wal_and_shm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ledger.sqlite3").write_bytes(b"ledger")
            (root / "ledger.sqlite3-wal").write_bytes(b"")
            (root / "ledger.sqlite3-shm").write_bytes(b"shared-memory")
            self.assertEqual(
                {"ledger.sqlite3": live.sha256_bytes(b"ledger")},
                live._artifact_files(root),
            )
            (root / "ledger.sqlite3-wal").write_bytes(b"committed-or-pending")
            with self.assertRaisesRegex(RuntimeError, "ARTIFACT_SQLITE_WAL_NOT_EMPTY"):
                live._artifact_files(root)

    def test_explicit_preflight_retry_releases_only_no_effect_reservation_and_appends_claim(self):
        root = live.project_root()
        scenario = next(item for item in live.PlanningScenarioCatalog.load(
            root / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        ).scenarios if item.scenario_id == "S01-single-bugfix")
        roles_bytes = (
            root / "tests" / "fixtures" / "engine"
            / "plan-inspection-general-reviewer-sol-xhigh-roles.json"
        ).read_bytes()
        policy = RoleTimeoutPolicy(overrides=(RoleTimeoutOverride(
            role="compact_plan_reviewer", timeout_seconds=1800,
            replaces_timeout_seconds=900, reason="검증된 timeout 운영 변경",
        ),))
        policy_bytes = policy.model_dump_json().encode("utf-8")
        budget = GoalBudgetPolicy(
            total_tokens=1_000_000, call_reservation_tokens=100_000,
            replan_reserve_percent=25,
        )
        budget_bytes = budget.model_dump_json().encode("utf-8")
        adjustment = {
            "source_receipt_digest": "sha256:" + "a" * 64,
            "charge_tokens": 100_000,
            "reason": "사용자가 승인한 timeout 잠정 차감",
        }
        adjustment_bytes = json.dumps(
            adjustment, ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runs = base / "runs"
            source = runs / "source"
            failed = runs / "failed"
            new_run = runs / "retry"
            source.mkdir(parents=True)
            failed.mkdir()
            new_run.mkdir()
            (source / "source.json").write_text("{}\n", encoding="utf-8")
            source_outcome = "sha256:" + "b" * 64
            claim_digest = "sha256:" + "c" * 64
            resume_source = {
                "run_root": str(source.resolve()),
                "files": live._artifact_files(source),
                "outcome_digest": source_outcome,
                "claim_digest": claim_digest,
                "resume_mode": "plan_review_timeout_continuation",
                "prior_provider_calls": 9,
                "remaining_provider_calls": 5,
                "planner_logical_budget": 9,
            }
            inputs = failed / "inputs"
            inputs.mkdir()
            (inputs / "role-config.json").write_bytes(roles_bytes)
            (inputs / "role-timeout-policy.json").write_text(
                policy.model_dump_json(), encoding="utf-8",
            )
            (inputs / "budget-policy.json").write_bytes(budget_bytes)
            (inputs / "timeout-usage-adjustment.json").write_bytes(adjustment_bytes)
            for name in (
                "goal-preparation.json", "source-terminal-observation.json",
            ):
                (failed / name).write_text("{}\n", encoding="utf-8")
            executable = Path(sys.executable).resolve()
            preflight = {
                "scenario": {"scenario_id": scenario.scenario_id},
                "codex_bin_digest": live.sha256_bytes(executable.read_bytes()),
                "role_config_digest": live.sha256_bytes(roles_bytes),
                "role_timeout_policy_digest": policy.policy_digest,
                "role_timeout_policy_bytes_digest": live.sha256_bytes(policy_bytes),
                "budget_policy": budget.model_dump(mode="json"),
                "budget_policy_bytes_digest": live.sha256_bytes(budget_bytes),
                "timeout_usage_adjustment_bytes_digest": live.sha256_bytes(adjustment_bytes),
                "prior_provider_calls": 9,
                "maximum_provider_calls": 5,
                "planning_role_budget": 9,
                "resume_source": resume_source,
            }
            preflight["lock_digest"] = live.sha256_digest(preflight)
            (failed / "preflight.json").write_text(
                json.dumps(preflight, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summary = {
                "status": "FAIL", "error_type": "RuntimeError",
                "error": "GOAL_REPAIR_ORIGINAL_ARTIFACT_CHANGED",
                "plan_activated": False, "worker_executed": False,
            }
            (failed / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            claim = runs / "planning-feedback-claims" / (claim_digest[7:] + ".json")
            claim.parent.mkdir()
            claim_value = {
                "source_outcome_digest": source_outcome,
                "run_root": str(failed.resolve()),
                "preflight_digest": preflight["lock_digest"],
            }
            claim.write_text(
                json.dumps(claim_value, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            claim_before = claim.read_bytes()
            protected_before = {
                name: (failed / name).read_bytes()
                for name in ("summary.json", "preflight.json", "goal-preparation.json")
            }

            ledger = SQLiteEngineLedger(
                failed / "ledger" / "flowmarshal-engine.sqlite3",
                artifact_root=failed / "ledger" / "artifacts",
            )
            service = EngineService(ledger)
            service.initialize()
            project_id = service.create_project(name="retry fixture", root=source)
            imported_ids = tuple(f"provider_call_imported_{index}" for index in range(9))
            request = make_role_request(
                role="compact_plan_reviewer", instructions="JSON만 반환",
                payload={"case": "retry"}, output_schema={"type": "object"},
                model="gpt-5.6-sol", effort="xhigh",
                inventory_digest="sha256:" + "d" * 64, cwd=str(source),
                timeout_seconds=1800, timeout_policy_digest=policy.policy_digest,
            )
            reserved_id = "provider_call_retry_reserved"
            with ledger.transaction() as tx:
                for index, identifier in enumerate(imported_ids):
                    status = "usage_unknown" if index == 8 else "settled"
                    tx.connection.execute(
                        "INSERT INTO provider_calls "
                        "(id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                        "request_digest,request_json,estimated_tokens,policy_digest,status,"
                        "actual_tokens,receipt_json,usage_id,attempt_id,created_at,completed_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (identifier, project_id, "goal_retry", "sha256:" + "e" * 64,
                         f"imported-{index}", "fixture", "plan_review", "sha256:" + "f" * 64,
                         "{}", 0, None, status, 0 if status == "settled" else None,
                         "{}", None, None, tx.now, tx.now),
                    )
                    tx.history(
                        project_id, "budget.checkpoint_call_imported", "provider_call",
                        identifier, {"source_digest": source_outcome},
                    )
                tx.connection.execute(
                    "INSERT INTO budget_adjustments VALUES (?,?,?,?,?,?,?)",
                    ("adjustment_retry", project_id, "goal_retry", imported_ids[-1],
                     100_000, adjustment["reason"], tx.now),
                )
                tx.connection.execute(
                    "INSERT INTO provider_calls "
                    "(id,project_id,goal_id,goal_contract_digest,call_key,role,stage,"
                    "request_digest,request_json,estimated_tokens,policy_digest,status,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,'reserved',?)",
                    (reserved_id, project_id, "goal_retry", "sha256:" + "e" * 64,
                     "retry-reserved", request.role, "plan_review", live.sha256_digest(
                         request.model_dump(mode="json")
                     ), request.model_dump_json(), 100_000, live.sha256_digest(budget), tx.now),
                )
                tx.history(
                    project_id, "budget.call_reserved", "provider_call", reserved_id,
                    {"provider_effect": "not_started"},
                )
            (failed / "imported-role-checkpoint.json").write_text(
                json.dumps({
                    "source_digest": source_outcome,
                    "provider_call_ids": imported_ids,
                    "provider_calls": 9,
                    "remaining_provider_calls": 5,
                }, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "입력이 다릅니다"):
                live._load_preflight_retry(
                    failed.resolve(), new_run, scenario=scenario,
                    roles_bytes=roles_bytes + b" ", codex_bin=executable,
                    timeout_policy=policy, timeout_policy_bytes=policy_bytes,
                    budget_policy=budget, budget_policy_bytes=budget_bytes,
                    adjustment=adjustment, adjustment_bytes=adjustment_bytes,
                )
            retry = live._load_preflight_retry(
                failed.resolve(), new_run, scenario=scenario,
                roles_bytes=roles_bytes, codex_bin=executable,
                timeout_policy=policy, timeout_policy_bytes=policy_bytes,
                budget_policy=budget, budget_policy_bytes=budget_bytes,
                adjustment=adjustment, adjustment_bytes=adjustment_bytes,
            )
            with ledger.read() as connection:
                released = connection.execute(
                    "SELECT status,receipt_json FROM provider_calls WHERE id=?", (reserved_id,),
                ).fetchone()
                release_count = connection.execute(
                    "SELECT COUNT(*) FROM history_events "
                    "WHERE event_type='budget.released_before_effect' AND entity_id=?",
                    (reserved_id,),
                ).fetchone()[0]
            self.assertEqual(("released", None), tuple(released))
            self.assertEqual(1, release_count)
            self.assertEqual(claim_before, claim.read_bytes())
            for name, value in protected_before.items():
                self.assertEqual(value, (failed / name).read_bytes())

            retry_again = live._load_preflight_retry(
                failed.resolve(), new_run, scenario=scenario,
                roles_bytes=roles_bytes, codex_bin=executable,
                timeout_policy=policy, timeout_policy_bytes=policy_bytes,
                budget_policy=budget, budget_policy_bytes=budget_bytes,
                adjustment=adjustment, adjustment_bytes=adjustment_bytes,
            )
            self.assertEqual(retry, retry_again)
            runtime = FakeRuntime(run_root=new_run)
            roles = live.EngineRoleConfiguration.model_validate_json(roles_bytes)
            verify_lock = {
                "workspace_binding": {"test": "binding"},
                "codex_bin": str(executable),
                "codex_bin_digest": live.sha256_bytes(executable.read_bytes()),
                "role_config": str(inputs / "role-config.json"),
                "role_config_digest": live.sha256_bytes(roles_bytes),
                "source_manifest": {"source": "digest"},
                "project_root": str(source),
                "project_files": live._project_files(source),
                "operational_binding": roles.operational_binding(runtime.list_models()),
                "retry_preflight": retry,
            }
            followup_path, followup_digest = live._append_preflight_retry_claim(
                retry, resume_source, new_run, verify_lock,
            )
            self.assertTrue(followup_path.is_file())
            self.assertEqual(
                followup_digest,
                live.sha256_digest(json.loads(followup_path.read_text(encoding="utf-8"))),
            )
            self.assertEqual(claim_before, claim.read_bytes())
            started = new_run / "calls" / "01-compact_plan_reviewer"
            started.mkdir(parents=True)
            (started / "thread.receipt.json").write_text("{}\n", encoding="utf-8")
            (started / "turn.intent.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PRECALL_RETRY_ALREADY_CLAIMED"):
                live._load_preflight_retry(
                    failed.resolve(), new_run, scenario=scenario,
                    roles_bytes=roles_bytes, codex_bin=executable,
                    timeout_policy=policy, timeout_policy_bytes=policy_bytes,
                    budget_policy=budget, budget_policy_bytes=budget_bytes,
                    adjustment=adjustment, adjustment_bytes=adjustment_bytes,
                )

            recorded = live.RecordedRunner(runtime, new_run, verify_lock)
            with patch.object(live, "verify_workspace_binding"), \
                 patch.object(live, "source_manifest_files", return_value={"source": "digest"}):
                recorded._verify_lock()
                (failed / "goal-preparation.json").write_text(
                    '{"changed":true}\n', encoding="utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "RETRY_PREFLIGHT_PROVENANCE_CHANGED"):
                    recorded._verify_lock()


if __name__ == "__main__":
    unittest.main()
