from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.context import PromptAssembler
from flowmarshal.engine.domain import (
    CandidateDecision,
    CandidateStatus,
    ContextFragmentRef,
    ContextManifest,
    ContextSourceKind,
    ExecutionAction,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    GoalCoverage,
    ResolvedTarget,
    RuntimeIntentKind,
    StateFact,
    CriterionVerdict,
    GoalContractRevision,
    GoalVerdict,
    GoalVerdictStatus,
    GoalPreparationBinding,
    GoalReviewFindingBinding,
    PlanSkeletonCandidate,
    PlanContractDefinition,
    PlanContractRevision,
    PlanGoalCoverage,
    RecoveryAssessment,
    RepairAction,
    RevisionStatus,
    ValidationContract,
    ValidationExecutionStep,
    ValidationStatus,
    ManualValidationObservation,
    TaskExecutionSpecDefinition,
    TaskExecutionSpecRevision,
    ThreadBinding,
    new_id,
    TaskSkeleton,
    utc_now,
)
from flowmarshal.engine.ledger import (
    SQLITE_APPLICATION_ID,
    EngineLedgerError,
    SQLiteEngineLedger,
)
from flowmarshal.engine.models import AssignmentResolver
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.service import EngineService, EngineServiceError
from flowmarshal.engine.capabilities import CoreActionAuthority

from tests.engine_helpers import (
    clean_review,
    goal,
    inventory,
    plan,
    profile,
    project_map,
    state,
    skeleton,
)


class TrustedTestEngineService(EngineService):
    """합성 fixture의 사용자 승인을 명시 host capability로 표현한다."""

    def __init__(self, ledger, **kwargs):
        self.test_authority = CoreActionAuthority()
        super().__init__(ledger, action_authority=self.test_authority, **kwargs)

    def authorize_goal(self, **kwargs):
        if kwargs.get("capability") is None:
            try:
                target = self.goal_authorization_target(
                    project_id=kwargs["project_id"],
                    operating_policy=kwargs.get("operating_policy"),
                )
            except EngineServiceError as error:
                if not str(error).startswith("PLAN_SELECTION_REQUIRED:"):
                    raise
                # 일부 하위 계층 회귀는 Plan을 만들기 전의 GoalAuthorization만
                # fixture로 준비한다. 실제 Application/console 경계는 이 우회를
                # 사용하지 않으며 언제나 원장의 eligible selected Plan을 요구한다.
                synthetic = SimpleNamespace(
                    plan_id="plan_test_authorization_boundary",
                    plan_revision_id="plan_revision_test_authorization_boundary",
                    revision_no=1,
                    definition_digest=sha256_digest(
                        f"test-authorization-plan:{kwargs['project_id']}:definition"
                    ),
                    activation_digest=sha256_digest(
                        f"test-authorization-plan:{kwargs['project_id']}:activation"
                    ),
                )
                with patch.object(
                    EngineService,
                    "_selected_plan_for_activation",
                    return_value=synthetic,
                ):
                    target = self.goal_authorization_target(
                        project_id=kwargs["project_id"],
                        operating_policy=kwargs.get("operating_policy"),
                    )
                    kwargs["capability"] = self.test_authority.issue_goal_authorization(
                        ledger_path=self.ledger.path,
                        target=target,
                    )
                    kwargs["authorization_target"] = target
                    return super().authorize_goal(**kwargs)
            kwargs["capability"] = self.test_authority.issue_goal_authorization(
                ledger_path=self.ledger.path,
                target=target,
            )
        return super().authorize_goal(**kwargs)

    def record_effect_checkpoint(self, **kwargs):
        if kwargs.get("capability") is None:
            target = self.effect_checkpoint_target(
                task_id=kwargs["task_id"],
                effect_id=kwargs["effect_id"],
                execution_spec_digest=kwargs["execution_spec_digest"],
            )
            kwargs["capability"] = self.test_authority.issue_effect_checkpoint(
                ledger_path=self.ledger.path,
                target=target,
            )
        return super().record_effect_checkpoint(**kwargs)


class EngineServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.ledger = SQLiteEngineLedger(
            self.base / "state" / "flowmarshal-engine.sqlite3",
            artifact_root=self.base / "engine-artifacts",
        )
        self.service = TrustedTestEngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="합성", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.map = project_map(self.project_id, self.root)
        self.service.record_project_map(self.map)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.service.record_state_snapshot(self.state)
        self.inventory = inventory()
        self.skeleton = skeleton(self.goal, self.state)
        self.plan, self.task, self.decision = plan(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.skeleton,
            self.inventory,
        )
        skeleton_review = clean_review(
            self.plan.definition.source_skeleton_digest,
            role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(
                self.skeleton, self.goal, self.state, self.map
            ),
        )
        self.service.record_skeleton_evaluation(
            CandidateEvaluation(
                candidate=self.skeleton,
                semantic_submission=skeleton_review,
                decision=CandidateDecision(
                    candidate_digest=self.plan.definition.source_skeleton_digest,
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )
        )
        plan_review = clean_review(
            self.plan.activation_digest,
            role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(
                self.plan, self.goal, self.state, self.map
            ),
        )
        self.service.register_plan_evaluation(
            ExpandedPlanEvaluation(
                plan=self.plan,
                semantic_submissions=(plan_review,),
                decision=self.decision,
            )
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def activate(self) -> None:
        self.service.authorize_goal(project_id=self.project_id, source="합성 사용자 승인")
        self.service.activate_plan(
            plan_revision_id=self.plan.plan_revision_id,
            activation_digest=self.plan.activation_digest,
            source="test",
        )

    def spec(self, *, task=None, contract_digest=None, revision_no=1, supersedes=None):
        task = task or self.task
        prompt = PromptAssembler().assemble(
            static_policy="Core 계약 준수",
            project_policy="AGENTS 적용",
            stage_schema="result",
            task_instruction=task.objective,
            reference_blocks=(),
        )
        entry = next(item for item in self.map.entries if item.path == "AGENTS.md")
        fragment = ContextFragmentRef(
            fragment_id=f"fragment_{task.task_ref}",
            source_kind=ContextSourceKind.POLICY,
            source_ref=entry.path,
            content_digest=entry.content_digest,
            selector="whole-file",
            token_estimate=10,
            immutable=True,
        )
        manifest = ContextManifest(
            context_pack_id=new_id("context_pack"),
            fragments=(fragment,),
            prompt_binding=prompt.binding,
            total_token_estimate=10,
            selection_rationale=("project instruction",),
        )
        executor, validator = AssignmentResolver().resolve_contract(task.assignment, self.inventory)
        definition = TaskExecutionSpecDefinition(
            plan_activation_digest=self.plan.activation_digest,
            task_contract_digest=contract_digest or task.contract_digest,
            task_id=task.task_id,
            snapshot_digest=self.state.snapshot_digest,
            project_map_digest=self.map.revision_digest,
            context_manifest=manifest,
            resolved_targets=(
                ResolvedTarget(
                    target_ref="target_agents",
                    path="AGENTS.md",
                    expected_content_digest=entry.content_digest,
                    access="read",
                ),
            ),
            actions=(
                ExecutionAction(
                    action_ref="action_inspect",
                    kind="inspect",
                    description="파일을 확인한다.",
                ),
            ),
            validation_steps=(
                ValidationExecutionStep(
                    validation_id=task.validations[0].validation_id,
                    method="deterministic",
                    argv=(sys.executable, "-c", "print('ok')"),
                    working_directory=str(self.root),
                    timeout_seconds=30,
                    expected_exit_codes=(0,),
                    required_evidence_kinds=task.validations[0].required_evidence_kinds,
                ),
            ),
            executor=executor,
            validator=validator,
            resource_locks=(f"project:{self.project_id}",),
            idempotency_key=f"test-engine-{task.task_id}",
        )
        from flowmarshal.engine.worker_prompt import assemble_worker_prompt
        bundle = assemble_worker_prompt(task=task, definition=definition,
                                        profile=self.profile.definition, root=self.root)
        definition = definition.model_copy(update={
            "context_manifest": definition.context_manifest.model_copy(update={"prompt_binding": bundle.binding}),
        })
        return TaskExecutionSpecRevision(
            execution_spec_revision_id=new_id("execution_spec"),
            task_id=task.task_id,
            revision_no=revision_no,
            definition=definition,
            definition_digest=definition.definition_digest,
            supersedes_execution_spec_revision_id=supersedes,
            created_at=utc_now(),
        )


class EngineLedgerServiceTests(EngineServiceFixture):
    def test_engine_ledger_has_distinct_identity_and_no_access_grants(self) -> None:
        with self.ledger.read() as connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        self.assertEqual(SQLITE_APPLICATION_ID, application_id)
        self.assertNotIn("access_grants", tables)
        self.assertNotIn("authority_proofs", tables)
        self.assertIn("plan_activations", tables)
        self.assertIn("runtime_intents", tables)

    def test_service_recomputes_skeleton_gate_instead_of_trusting_payload(self) -> None:
        candidate = self.skeleton.model_copy(
            update={
                "candidate_id": new_id("candidate"),
                "state_signature": "sha256:" + "0" * 64,
            }
        )
        digest = sha256_digest(candidate)
        evaluation = CandidateEvaluation(
            candidate=candidate,
            semantic_submission=clean_review(
                digest,
                role="skeleton_reviewer",
                evidence_catalog=skeleton_review_evidence_catalog(
                    candidate, self.goal, self.state, self.map
                ),
            ),
            decision=CandidateDecision(
                candidate_digest=digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
        with self.assertRaisesRegex(EngineServiceError, "Core 재계산"):
            self.service.record_skeleton_evaluation(evaluation)

    def test_service_rejects_plan_semantic_drift_hidden_by_clean_review(self) -> None:
        drifted_task = self.task.model_copy(update={"objective": "Skeleton과 다른 목적"})
        definition = self.plan.definition.model_copy(update={"tasks": (drifted_task,)})
        drifted_plan = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=new_id("plan"),
            revision_no=1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            created_at=utc_now(),
        )
        evaluation = ExpandedPlanEvaluation(
            plan=drifted_plan,
            semantic_submissions=(
                clean_review(
                    drifted_plan.activation_digest,
                    role="compact_plan_reviewer",
                    evidence_catalog=plan_review_evidence_catalog(
                        drifted_plan, self.goal, self.state, self.map
                    ),
                ),
            ),
            decision=CandidateDecision(
                candidate_digest=drifted_plan.activation_digest,
                status=CandidateStatus.ADMISSIBLE,
                fitness_score=100,
                weakest_dimension="engineering",
            ),
        )
        with self.assertRaisesRegex(EngineServiceError, "Core 재계산"):
            self.service.register_plan_evaluation(evaluation)

    def test_service_rejects_copied_unknown_evidence_before_plan_registration(self) -> None:
        for location in ("task", "goal"):
            with self.subTest(location=location):
                task = self.task.model_copy(update={"task_id": new_id("task")})
                integrations = self.plan.definition.integration_validations
                if location == "task":
                    validation = task.validations[0].model_copy(
                        update={"required_evidence_kinds": ("unknown_kind",)}
                    )
                    task = task.model_copy(update={"validations": (validation,)})
                else:
                    integrations = (
                        integrations[0].model_copy(
                            update={"required_evidence_kinds": ("unknown_kind",)}
                        ),
                    )
                coverage = tuple(
                    item.model_copy(update={"task_ids": (task.task_id,)})
                    for item in self.plan.definition.goal_coverage
                )
                definition = self.plan.definition.model_copy(update={
                    "tasks": (task,),
                    "goal_coverage": coverage,
                    "integration_validations": integrations,
                })
                invalid_plan = PlanContractRevision(
                    plan_revision_id=new_id("plan_revision"),
                    plan_id=new_id("plan"),
                    revision_no=1,
                    definition=definition,
                    definition_digest=definition.definition_digest,
                    created_at=utc_now(),
                )
                evaluation = ExpandedPlanEvaluation(
                    plan=invalid_plan,
                    semantic_submissions=(clean_review(
                        invalid_plan.activation_digest,
                        role="compact_plan_reviewer",
                        evidence_catalog=plan_review_evidence_catalog(
                            invalid_plan, self.goal, self.state, self.map
                        ),
                    ),),
                    decision=CandidateDecision(
                        candidate_digest=invalid_plan.activation_digest,
                        status=CandidateStatus.ADMISSIBLE,
                        fitness_score=100,
                        weakest_dimension="engineering",
                    ),
                )
                with self.assertRaisesRegex(EngineServiceError, "알 수 없는 evidence kind"):
                    self.service.register_plan_evaluation(evaluation)
                with self.ledger.read() as connection:
                    self.assertIsNone(connection.execute(
                        "SELECT id FROM plan_revisions WHERE id = ?",
                        (invalid_plan.plan_revision_id,),
                    ).fetchone())
                    self.assertIsNone(connection.execute(
                        "SELECT id FROM task_contracts WHERE id = ?", (task.task_id,)
                    ).fetchone())

    def test_prototype_database_is_rejected_without_migration(self) -> None:
        other = self.base / "legacy.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO schema_meta VALUES ('schema_id', 'flowmarshal.core')")
        connection.execute("INSERT INTO schema_meta VALUES ('schema_revision', '2')")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(EngineLedgerError, "prototype"):
            SQLiteEngineLedger(other).initialize()

    def test_activation_requires_exact_digest(self) -> None:
        with self.assertRaisesRegex(EngineServiceError, "activation digest"):
            self.service.activate_plan(
                plan_revision_id=self.plan.plan_revision_id,
                activation_digest="sha256:" + "0" * 64,
                source="test",
            )
        self.activate()
        self.assertEqual((self.task.task_id,), self.service.list_ready_tasks(self.project_id))

    def test_manual_typed_observation_is_bound_to_current_spec_and_evidence(self) -> None:
        manual_validation = self.task.validations[0].model_copy(
            update={"method": "manual", "required_evidence_kinds": ("user_decision",)}
        )
        manual_task = self.task.model_copy(
            update={"task_id": new_id("task"), "validations": (manual_validation,)}
        )
        coverage = self.plan.definition.goal_coverage[0].model_copy(
            update={"task_ids": (manual_task.task_id,)}
        )
        definition = self.plan.definition.model_copy(
            update={"tasks": (manual_task,), "goal_coverage": (coverage,)}
        )
        revised = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=self.plan.plan_id,
            revision_no=2,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=self.plan.plan_revision_id,
            created_at=utc_now(),
        )
        self.service.register_plan_evaluation(
            ExpandedPlanEvaluation(
                plan=revised,
                semantic_submissions=(
                    clean_review(
                        revised.activation_digest,
                        role="compact_plan_reviewer",
                        evidence_catalog=plan_review_evidence_catalog(
                            revised, self.goal, self.state, self.map
                        ),
                    ),
                ),
                decision=CandidateDecision(
                    candidate_digest=revised.activation_digest,
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )
        )
        self.plan = revised
        self.task = manual_task
        self.activate()
        spec = self.spec(task=manual_task, contract_digest=manual_task.contract_digest)
        manual_step = ValidationExecutionStep(
            validation_id=manual_validation.validation_id,
            method="manual",
            required_evidence_kinds=("user_decision",),
            manual_instruction="사용자가 결과를 직접 확인한다.",
        )
        spec_definition = spec.definition.model_copy(
            update={"validation_steps": (manual_step,)}
        )
        from flowmarshal.engine.worker_prompt import assemble_worker_prompt
        bundle = assemble_worker_prompt(task=manual_task, definition=spec_definition,
                                        profile=self.profile.definition, root=self.root)
        spec_definition = spec_definition.model_copy(update={
            "context_manifest": spec_definition.context_manifest.model_copy(update={"prompt_binding": bundle.binding}),
        })
        spec = spec.model_copy(
            update={
                "definition": spec_definition,
                "definition_digest": spec_definition.definition_digest,
            }
        )
        self.service.materialize_execution_spec(spec, inventory=self.inventory)
        runtime = FakeCodexRuntime(self.inventory)
        dispatcher = EngineDispatcher(self.service, runtime)
        dispatched = dispatcher.run_once(self.project_id)
        with self.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(
                connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()["binding_json"]
            )
        runtime.complete(binding.thread_id)
        dispatcher.run_once(self.project_id)
        result = self.service.record_typed_validation_observation(
            project_id=self.project_id,
            plan_revision_id=revised.plan_revision_id,
            observation=ManualValidationObservation(
                validation_id=manual_validation.validation_id,
                task_id=manual_task.task_id,
                observer="qualification-user",
                passed=True,
                observation="요구 동작을 직접 확인했다.",
                source_ref="manual://qualification",
                observed_at=utc_now(),
            ),
        )
        self.assertEqual(ValidationStatus.PASS, result.status)
        with self.ledger.read() as connection:
            kinds = {
                row["kind"]
                for row in connection.execute(
                    "SELECT kind FROM evidence_records WHERE id IN (?)", result.evidence_ids
                )
            }
        self.assertEqual({"user_decision"}, kinds)

    def test_plan_revision_must_supersede_latest_in_same_lineage(self) -> None:
        invalid = self.plan.model_copy(
            update={
                "plan_revision_id": new_id("plan_revision"),
                "revision_no": 2,
                "supersedes_plan_revision_id": new_id("plan_revision"),
                "created_at": utc_now(),
            }
        )
        decision = CandidateDecision(
            candidate_digest=invalid.activation_digest,
            status=CandidateStatus.ADMISSIBLE,
            fitness_score=100,
            weakest_dimension="engineering",
        )
        with self.assertRaisesRegex(EngineServiceError, "최신 revision"):
            self.service.register_plan_evaluation(
                ExpandedPlanEvaluation(
                    plan=invalid,
                    semantic_submissions=(
                        clean_review(
                            invalid.activation_digest,
                            role="compact_plan_reviewer",
                            evidence_catalog=plan_review_evidence_catalog(
                                invalid, self.goal, self.state, self.map
                            ),
                        ),
                    ),
                    decision=decision,
                )
            )

    def test_stale_state_blocks_activation(self) -> None:
        newer = self.state.model_copy(
            update={
                "snapshot_id": new_id("snapshot"),
                "version": 2,
                "facts": (
                    StateFact(
                        fact_id="fact_one",
                        predicate="project map current",
                        value="changed",
                        source_ref="project-map",
                        evidence_digest=self.map.revision_digest,
                    ),
                ),
                "observed_at": utc_now(),
            }
        )
        self.service.record_state_snapshot(newer)
        with self.assertRaisesRegex(EngineServiceError, "stale"):
            self.activate()

    def test_conflict_goal_can_be_inspected_and_revised_in_one_chain(self) -> None:
        conflict_definition = self.goal.definition.model_copy(
            update={"observable_outcome": "review conflict를 해결한다."}
        )
        conflict = GoalContractRevision(
            goal_revision_id=new_id("goal_revision"),
            goal_id=self.goal.goal_id,
            revision_no=2,
            definition=conflict_definition,
            definition_digest=conflict_definition.definition_digest,
            status=RevisionStatus.CONFLICT,
            preparation_binding=GoalPreparationBinding(
                normalization_proposal_digest="sha256:" + "1" * 64,
                reviewer_submission_digest="sha256:" + "2" * 64,
                reviewer_role="goal-reviewer",
                finding_codes=("GOAL_AMBIGUITY",),
                finding_evidence_refs=("sha256:" + "3" * 64,),
                findings=(
                    GoalReviewFindingBinding(
                        finding_code="GOAL_AMBIGUITY",
                        evidence_refs=("sha256:" + "3" * 64,),
                        remediable=True,
                    ),
                ),
            ),
            supersedes_goal_revision_id=self.goal.goal_revision_id,
            created_at=utc_now(),
        )
        self.service.register_goal(conflict, activate=False)
        self.assertEqual(conflict.goal_revision_id, self.service.load_latest_goal(self.project_id).goal_revision_id)
        self.assertEqual(self.goal.goal_revision_id, self.service.load_active_goal(self.project_id).goal_revision_id)

        ready_definition = conflict_definition.model_copy(
            update={"observable_outcome": "모호성 없이 검증 가능한 결과를 만든다."}
        )
        ready = GoalContractRevision(
            goal_revision_id=new_id("goal_revision"),
            goal_id=self.goal.goal_id,
            revision_no=3,
            definition=ready_definition,
            definition_digest=ready_definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_goal_revision_id=conflict.goal_revision_id,
            created_at=utc_now(),
        )
        self.service.register_goal(ready)
        self.assertEqual(ready.goal_revision_id, self.service.load_latest_goal(self.project_id).goal_revision_id)
        self.assertEqual(ready.goal_revision_id, self.service.load_active_goal(self.project_id).goal_revision_id)

    def test_execution_spec_cannot_change_task_semantics(self) -> None:
        self.activate()
        invalid = self.spec(contract_digest="sha256:" + "8" * 64)
        with self.assertRaisesRegex(EngineServiceError, "TaskContract 의미"):
            self.service.materialize_execution_spec(invalid, inventory=self.inventory)
        valid = self.spec()
        self.service.materialize_execution_spec(valid, inventory=self.inventory)
        with self.ledger.read() as connection:
            status = connection.execute(
                "SELECT status FROM task_contracts WHERE id = ?", (self.task.task_id,)
            ).fetchone()["status"]
        self.assertEqual("materialized", status)

    def test_file_change_after_materialization_blocks_attempt(self) -> None:
        self.activate()
        valid = self.spec()
        self.service.materialize_execution_spec(valid, inventory=self.inventory)
        (self.root / "AGENTS.md").write_text("materialization 뒤 바뀐 지침", encoding="utf-8")
        with self.assertRaisesRegex(EngineServiceError, "STALE_EXECUTION_INPUT"):
            self.service.reserve_attempt(task_id=self.task.task_id)

    def test_unreceipted_intent_enters_recovery_and_is_not_retried(self) -> None:
        self.activate()
        spec = self.spec()
        self.service.materialize_execution_spec(spec, inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        first = self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key="same-idempotency-key-001",
            request={"task": self.task.task_id},
        )
        again = self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.CREATE_THREAD,
            idempotency_key="same-idempotency-key-001",
            request={"task": self.task.task_id},
        )
        self.assertEqual(first.intent_id, again.intent_id)
        unknown = self.service.recover_inspect(self.project_id)
        self.assertEqual((first.intent_id,), unknown)
        snapshot = self.service.status(self.project_id)
        self.assertEqual("recovery_required", snapshot["project"]["run_state"])
        with self.assertRaisesRegex(EngineServiceError, "external_unknown"):
            self.service.retry_task(task_id=self.task.task_id)

    def test_contract_and_history_are_immutable(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.ledger.transaction() as tx:
                tx.connection.execute(
                    "UPDATE task_contracts SET payload_json = '{}' WHERE id = ?",
                    (self.task.task_id,),
                )
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_same_project_attempts_are_serialized(self) -> None:
        task_a = self.task.model_copy(
            update={
                "task_id": new_id("task"),
                "task_ref": "task_a",
                "produces": ("result:a",),
                "validations": (
                    ValidationContract(
                        validation_id="validation_a",
                        statement="A 검증",
                        method="deterministic",
                        required_evidence_kinds=("test",),
                    ),
                ),
            }
        )
        task_b = self.task.model_copy(
            update={
                "task_id": new_id("task"),
                "task_ref": "task_b",
                "produces": ("result:b",),
                "validations": (
                    ValidationContract(
                        validation_id="validation_b",
                        statement="B 검증",
                        method="deterministic",
                        required_evidence_kinds=("test",),
                    ),
                ),
            }
        )
        second_skeleton = PlanSkeletonCandidate(
            candidate_id=new_id("candidate"),
            goal_contract_digest=self.goal.definition_digest,
            state_signature=self.state.semantic_digest,
            approach=self.skeleton.approach,
            tasks=(
                TaskSkeleton(
                    task_ref="task_a",
                    kind=task_a.kind,
                    objective=task_a.objective,
                    contributes_to=task_a.goal_criterion_refs,
                    produces=task_a.produces,
                    consumes=task_a.consumes,
                ),
                TaskSkeleton(
                    task_ref="task_b",
                    kind=task_b.kind,
                    objective=task_b.objective,
                    contributes_to=task_b.goal_criterion_refs,
                    produces=task_b.produces,
                    consumes=task_b.consumes,
                ),
            ),
            goal_coverage=(
                GoalCoverage(criterion_id="ac_one", task_refs=("task_a", "task_b")),
            ),
            estimated_change_cost=2,
            estimated_context_tokens=200,
        )
        second_skeleton_digest = sha256_digest(second_skeleton)
        self.service.record_skeleton_evaluation(
            CandidateEvaluation(
                candidate=second_skeleton,
                semantic_submission=clean_review(
                    second_skeleton_digest,
                    role="skeleton_reviewer",
                    evidence_catalog=skeleton_review_evidence_catalog(
                        second_skeleton, self.goal, self.state, self.map
                    ),
                ),
                decision=CandidateDecision(
                    candidate_digest=second_skeleton_digest,
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )
        )
        definition = PlanContractDefinition(
            project_id=self.project_id,
            goal_contract_digest=self.goal.definition_digest,
            base_state_snapshot_digest=self.state.snapshot_digest,
            project_map_digest=self.map.revision_digest,
            source_skeleton_digest=second_skeleton_digest,
            tasks=(task_a, task_b),
            goal_coverage=(
                PlanGoalCoverage(
                    criterion_id="ac_one",
                    task_ids=(task_a.task_id, task_b.task_id),
                    validation_ids=("validation_a", "validation_b", "validation_goal"),
                ),
            ),
            integration_validations=self.plan.definition.integration_validations,
            model_inventory_digest=self.inventory.inventory_digest,
        )
        second_plan = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=self.plan.plan_id,
            revision_no=2,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=self.plan.plan_revision_id,
            created_at=utc_now(),
        )
        decision = CandidateDecision(
            candidate_digest=second_plan.activation_digest,
            status=CandidateStatus.ADMISSIBLE,
            fitness_score=100,
            weakest_dimension="engineering",
        )
        self.service.register_plan_evaluation(
            ExpandedPlanEvaluation(
                plan=second_plan,
                semantic_submissions=(
                    clean_review(
                        second_plan.activation_digest,
                        role="compact_plan_reviewer",
                        evidence_catalog=plan_review_evidence_catalog(
                            second_plan, self.goal, self.state, self.map
                        ),
                    ),
                ),
                decision=decision,
            )
        )
        self.plan = second_plan
        self.service.authorize_goal(project_id=self.project_id, source="합성 사용자 승인")
        self.service.activate_plan(
            plan_revision_id=second_plan.plan_revision_id,
            activation_digest=second_plan.activation_digest,
            source="test",
        )
        self.service.materialize_execution_spec(self.spec(task=task_a), inventory=self.inventory)
        self.service.materialize_execution_spec(self.spec(task=task_b), inventory=self.inventory)
        self.service.reserve_attempt(task_id=task_a.task_id)
        with self.assertRaisesRegex(EngineServiceError, "직렬"):
            self.service.reserve_attempt(task_id=task_b.task_id)

    def test_goal_satisfied_is_refused_before_task_and_goal_evidence(self) -> None:
        self.activate()
        verdict = GoalVerdict(
            goal_verdict_id=new_id("goal_verdict"),
            goal_contract_digest=self.goal.definition_digest,
            plan_activation_digest=self.plan.activation_digest,
            status=GoalVerdictStatus.SATISFIED,
            criteria=(
                CriterionVerdict(
                    criterion_id="ac_one",
                    status=ValidationStatus.PASS,
                    evidence_ids=(new_id("evidence"),),
                    rationale="주장만 존재",
                ),
            ),
            integration_validation_result_ids=(new_id("validation_result"),),
            evaluated_at=utc_now(),
        )
        with self.assertRaisesRegex(EngineServiceError, "모든 Task"):
            self.service.record_goal_verdict(
                project_id=self.project_id,
                plan_revision_id=self.plan.plan_revision_id,
                verdict=verdict,
            )

    def test_replan_limits_and_counts_are_ledger_derived(self) -> None:
        self.activate()
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)

        def fresh_evidence():
            evidence = EvidenceRecord(
                evidence_id=new_id("evidence"), project_id=self.project_id,
                task_id=self.task.task_id, attempt_id=attempt.attempt_id,
                kind=EvidenceKind.FILE, source_ref="app.py",
                observation="새 재계획 관측", content_digest=sha256_digest(new_id("observation")),
                observed_at=utc_now(),
            )
            self.service.record_evidence(evidence)
            return evidence.evidence_id

        with self.assertRaisesRegex(EngineServiceError, "원장에서 계산한 1"):
            self.service.record_recovery_assessment(
                self.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="잘못된 외부 카운터를 거부한다.",
                    new_evidence_ids=(new_id("evidence"),),
                    same_failure_replan_count=0,
                    goal_replan_count=0,
                ),
            )

        for count in (1, 2):
            self.service.record_recovery_assessment(
                self.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale=f"새 evidence를 사용한 {count}차 재계획",
                    new_evidence_ids=(fresh_evidence(),),
                    same_failure_replan_count=count,
                    goal_replan_count=0,
                ),
            )

        with self.assertRaisesRegex(EngineServiceError, "한도 2회"):
            self.service.record_recovery_assessment(
                self.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="세 번째 동일 실패 재계획은 금지한다.",
                    new_evidence_ids=(new_id("evidence"),),
                    same_failure_replan_count=3,
                    goal_replan_count=0,
                ),
            )

        for count in range(1, 6):
            self.service.record_recovery_assessment(
                self.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.REQUIREMENT_CHANGE,
                    action=RepairAction.GOAL_REVISION,
                    rationale=f"새 요구 evidence를 사용한 {count}차 Goal 재계획",
                    new_evidence_ids=(fresh_evidence(),),
                    same_failure_replan_count=0,
                    goal_replan_count=count,
                ),
            )

        with self.assertRaisesRegex(EngineServiceError, "한도 5회"):
            self.service.record_recovery_assessment(
                self.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.REQUIREMENT_CHANGE,
                    action=RepairAction.GOAL_REVISION,
                    rationale="여섯 번째 Goal 재계획은 금지한다.",
                    new_evidence_ids=(new_id("evidence"),),
                    same_failure_replan_count=0,
                    goal_replan_count=6,
                ),
            )


if __name__ == "__main__":
    unittest.main()
