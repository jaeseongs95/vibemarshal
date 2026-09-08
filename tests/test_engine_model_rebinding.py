from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.engine.benchmark_lifecycle import _successful_worker_for_current_spec
from flowmarshal.engine.context import ProjectMapper
from flowmarshal.engine.domain import (
    AttemptKind,
    CandidateDecision,
    CandidateStatus,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    ModelFallback,
    PlanContractRevision,
    RevisionStatus,
    RoleAssignmentPolicy,
    RuntimeIntentKind,
    ValidationResult,
    ValidationStatus,
    new_id,
    utc_now,
)
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.model_lock import ModelCapability, ModelChoice, ModelInventory, RUNTIME_CAPABILITIES, verify_binding
from flowmarshal.engine.model_rebinding import (
    ModelRebindRequest,
    ModelRebindingError,
    ModelRebindingService,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    ExpandedPlanEvaluation,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService
from flowmarshal.engine.worker_prompt import PromptArtifactStore, assemble_worker_prompt

from tests.engine_helpers import clean_review, goal, plan, profile, skeleton, state
from tests.test_engine_ledger_service import EngineServiceFixture


def _inventory(*models: str) -> ModelInventory:
    efforts = {
        "worker": ("medium",),
        "backup": ("high",),
        "optional": ("low",),
        "validator": ("high",),
        "validator_backup": ("medium",),
        "outside": ("high",),
    }
    return ModelInventory(
        source="strict-model-list-v2",
        executable_digest="sha256:" + "4" * 64,
        runtime_capabilities=RUNTIME_CAPABILITIES,
        models=tuple(ModelCapability(model=model, supported_efforts=efforts[model]) for model in models),
    )


class ModelRebindingFixture(EngineServiceFixture):
    """Plan 등록 전에 fallback envelope를 넣는 실제 SQLite fixture."""

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
        self.service = EngineService(self.ledger)
        self.service.initialize()
        self.project_id = self.service.create_project(name="재결속 합성", root=self.root)
        self.profile = profile(self.project_id)
        self.service.register_profile(self.profile)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.service.register_goal(self.goal)
        self.map = ProjectMapper().build(project_id=self.project_id, root=self.root, revision_no=1)
        self.service.record_project_map(self.map)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.service.record_state_snapshot(self.state)
        self.inventory = _inventory("worker", "backup", "optional", "validator")
        self.skeleton = skeleton(self.goal, self.state)
        original_plan, original_task, _ = plan(
            self.project_id,
            self.goal,
            self.state,
            self.map.revision_digest,
            self.skeleton,
            self.inventory,
        )
        executor = RoleAssignmentPolicy(
            role="executor",
            preferred_model="worker",
            preferred_effort="medium",
            allowed_fallbacks=(
                ModelFallback(model="backup", effort="high"),
                ModelFallback(model="optional", effort="low"),
            ),
        )
        validator = RoleAssignmentPolicy(
            role="validator",
            preferred_model="validator",
            preferred_effort="high",
            allowed_fallbacks=(ModelFallback(model="validator_backup", effort="medium"),),
        )
        semantic_validation = original_task.validations[0].model_copy(
            update={
                "method": "semantic",
                "statement": "Worker 직접 evidence를 독립적으로 검사한다.",
            }
        )
        self.task = original_task.model_copy(
            update={
                "assignment": original_task.assignment.model_copy(
                    update={"executor": executor, "validator": validator}
                ),
                "validations": (semantic_validation,),
            }
        )
        definition = original_plan.definition.model_copy(update={"tasks": (self.task,)})
        self.plan = PlanContractRevision(
            plan_revision_id=original_plan.plan_revision_id,
            plan_id=original_plan.plan_id,
            revision_no=original_plan.revision_no,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            created_at=utc_now(),
        )
        self.decision = CandidateDecision(
            candidate_digest=self.plan.activation_digest,
            status=CandidateStatus.ADMISSIBLE,
            fitness_score=100,
            weakest_dimension="engineering",
        )
        self.service.record_skeleton_evaluation(
            CandidateEvaluation(
                candidate=self.skeleton,
                semantic_submission=clean_review(
                    self.plan.definition.source_skeleton_digest,
                    role="skeleton_reviewer",
                    evidence_catalog=skeleton_review_evidence_catalog(
                        self.skeleton, self.goal, self.state, self.map
                    ),
                ),
                decision=CandidateDecision(
                    candidate_digest=self.plan.definition.source_skeleton_digest,
                    status=CandidateStatus.ADMISSIBLE,
                    fitness_score=100,
                    weakest_dimension="engineering",
                ),
            )
        )
        self.service.register_plan_evaluation(
            ExpandedPlanEvaluation(
                plan=self.plan,
                semantic_submissions=(
                    clean_review(
                        self.plan.activation_digest,
                        role="compact_plan_reviewer",
                        evidence_catalog=plan_review_evidence_catalog(
                            self.plan, self.goal, self.state, self.map
                        ),
                    ),
                ),
                decision=self.decision,
            )
        )
        self.activate()
        self.original_spec = self.spec()
        self.service.materialize_execution_spec(self.original_spec, inventory=self.inventory)
        self.rebinding = ModelRebindingService(self.ledger)

    def spec(self, **kwargs):
        spec = super().spec(**kwargs)
        step = spec.definition.validation_steps[0].model_copy(
            update={
                "method": "semantic",
                "argv": (),
                "working_directory": None,
                "semantic_instruction": "Worker 직접 evidence와 Task 계약을 독립 대조한다.",
            }
        )
        definition = spec.definition.model_copy(update={"validation_steps": (step,)})
        bundle = assemble_worker_prompt(
            task=kwargs.get("task") or self.task,
            definition=definition,
            profile=self.profile.definition,
            root=self.root,
        )
        definition = definition.model_copy(
            update={
                "context_manifest": definition.context_manifest.model_copy(
                    update={"prompt_binding": bundle.binding}
                )
            }
        )
        return spec.model_copy(
            update={"definition": definition, "definition_digest": definition.definition_digest}
        )

    def request(self, selection: ModelChoice, **changes) -> ModelRebindRequest:
        values = {
            "project_id": self.project_id,
            "plan_revision_id": self.plan.plan_revision_id,
            "plan_activation_digest": self.plan.activation_digest,
            "task_id": self.task.task_id,
            "current_execution_spec_revision_id": self.original_spec.execution_spec_revision_id,
            "current_execution_spec_digest": self.original_spec.definition_digest,
            "role": "executor",
            "selection": selection,
            "reason": "현재 model/list에서 사용 가능한 명시적 선택",
        }
        values.update(changes)
        return ModelRebindRequest(**values)

    def complete_worker_with_evidence(self) -> tuple[object, EvidenceRecord]:
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=self.project_id,
            task_id=self.task.task_id,
            attempt_id=attempt.attempt_id,
            kind=EvidenceKind.TEST,
            source_ref="synthetic-worker-command",
            observation=json.dumps({"exit_code": 0}),
            content_digest="sha256:" + "9" * 64,
            observed_at=utc_now(),
        )
        self.service.record_evidence(evidence)
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        return attempt, evidence


class ModelRebindingTests(ModelRebindingFixture):
    def test_explicit_fallback_creates_new_spec_attempt_record_and_history(self) -> None:
        with self.ledger.read() as connection:
            plan_before = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ?", (self.plan.plan_revision_id,)
            ).fetchone()[0]
            old_spec_before = connection.execute(
                "SELECT payload_json FROM execution_spec_revisions WHERE id = ?",
                (self.original_spec.execution_spec_revision_id,),
            ).fetchone()[0]
        live = _inventory("backup", "validator")
        result = self.rebinding.rebind_and_reserve(
            self.request(ModelChoice(model="backup", effort="high")), live
        )

        self.assertEqual(2, result.execution_spec.revision_no)
        self.assertEqual(self.original_spec.execution_spec_revision_id,
                         result.execution_spec.supersedes_execution_spec_revision_id)
        self.assertEqual("backup", result.execution_spec.definition.executor.model)
        self.assertTrue(result.execution_spec.definition.executor.fallback_used)
        self.assertEqual(result.execution_spec.definition_digest, result.attempt.execution_spec_digest)
        verify_binding(
            result.execution_spec.definition.executor.operational_binding,
            live,
            role="executor",
            model="backup",
            effort="high",
        )
        fallback_support = {
            item.model: item.supported
            for item in result.execution_spec.definition.executor.operational_binding.lock.roles[0].allowed_fallbacks
        }
        self.assertEqual({"worker": False, "optional": False}, fallback_support)

        expected_bundle = assemble_worker_prompt(
            task=self.task,
            definition=result.execution_spec.definition,
            profile=self.profile.definition,
            root=self.root,
        )
        stored = PromptArtifactStore(self.ledger.artifact_root).load(
            result.execution_spec.definition.context_manifest.prompt_binding
        )
        self.assertEqual(expected_bundle, stored)

        with self.ledger.read() as connection:
            specs = connection.execute(
                "SELECT id, payload_json, is_current FROM execution_spec_revisions "
                "WHERE task_id = ? ORDER BY revision_no", (self.task.task_id,)
            ).fetchall()
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ?", (result.attempt.attempt_id,)
            ).fetchone()
            selection = connection.execute(
                "SELECT * FROM model_rebinding_selections WHERE id = ?",
                (result.record.selection_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT event_type, entity_id, payload_json FROM history_events "
                "WHERE project_id = ? ORDER BY sequence", (self.project_id,)
            ).fetchall()
            plan_after = connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id = ?", (self.plan.plan_revision_id,)
            ).fetchone()[0]
        self.assertEqual([0, 1], [row["is_current"] for row in specs])
        self.assertEqual(old_spec_before, specs[0]["payload_json"])
        self.assertEqual(plan_before, plan_after)
        self.assertEqual(result.execution_spec.definition_digest, attempt["execution_spec_digest"])
        self.assertEqual(result.record.request_digest, selection["request_digest"])
        self.assertEqual("reserved", attempt["status"])
        rebound = next(row for row in events if row["event_type"] == "model.rebound")
        self.assertEqual(result.record.selection_id, rebound["entity_id"])
        self.assertEqual(result.execution_spec.definition_digest,
                         json.loads(rebound["payload_json"])["new_execution_spec_digest"])
        self.assertTrue(self.ledger.verify_history(self.project_id))

    def test_explicit_preferred_rebuild_survives_optional_fallback_inventory_loss(self) -> None:
        live = _inventory("worker", "validator")
        result = self.rebinding.rebind_and_reserve(
            self.request(ModelChoice(model="worker", effort="medium")), live
        )
        assignment = result.execution_spec.definition.executor
        self.assertFalse(assignment.fallback_used)
        self.assertEqual("worker", assignment.model)
        verify_binding(
            assignment.operational_binding,
            live,
            role="executor",
            model="worker",
            effort="medium",
        )
        self.assertEqual(
            {"backup": False, "optional": False},
            {item.model: item.supported
             for item in assignment.operational_binding.lock.roles[0].allowed_fallbacks},
        )

    def test_validator_rebind_reserves_only_validation_and_preserves_worker_lineage(self) -> None:
        worker_attempt, evidence = self.complete_worker_with_evidence()
        old_prompt_binding = self.original_spec.definition.context_manifest.prompt_binding
        result = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator_backup", effort="medium"),
                role="validator",
            ),
            _inventory("worker", "validator_backup"),
        )

        self.assertEqual(AttemptKind.VALIDATION, result.attempt.kind)
        self.assertEqual(AttemptKind.VALIDATION, result.record.attempt_kind)
        self.assertEqual("validator_backup", result.execution_spec.definition.validator.model)
        self.assertEqual(old_prompt_binding, result.execution_spec.definition.context_manifest.prompt_binding)
        with self.ledger.read() as connection:
            attempts = connection.execute(
                "SELECT id, kind, execution_spec_digest FROM attempts WHERE task_id = ? "
                "ORDER BY created_at, rowid",
                (self.task.task_id,),
            ).fetchall()
            lifecycle_worker_spec, lifecycle_worker_attempt = (
                _successful_worker_for_current_spec(
                    connection,
                    self.task.task_id,
                    result.execution_spec.definition_digest,
                )
            )
        self.assertEqual(["execution", "validation"], [row["kind"] for row in attempts])
        self.assertEqual(worker_attempt.execution_spec_digest, attempts[0]["execution_spec_digest"])
        self.assertEqual(result.execution_spec.definition_digest, attempts[1]["execution_spec_digest"])
        self.assertEqual(self.original_spec.execution_spec_revision_id, lifecycle_worker_spec["id"])
        self.assertEqual(worker_attempt.attempt_id, lifecycle_worker_attempt["id"])

        runtime = FakeCodexRuntime(_inventory("worker", "validator_backup"))
        dispatcher = EngineDispatcher(self.service, runtime)
        row, current = dispatcher._attempt_context(result.attempt.attempt_id)
        self.assertEqual(result.execution_spec.definition_digest, current.definition_digest)
        role, _prompt, _schema = dispatcher._role_for_attempt(
            row,
            current,
            validation_id=self.task.validations[0].validation_id,
        )
        self.assertEqual("validator_backup", role.model)
        catalog = dispatcher._semantic_evidence_catalog(
            row,
            current,
            self.task.validations[0].validation_id,
        )
        self.assertEqual({evidence.evidence_id}, set(catalog))
        dispatcher._dispatch_reserved(
            result.attempt.attempt_id,
            validation_id=self.task.validations[0].validation_id,
        )
        self.assertEqual(1, runtime.create_calls)
        self.assertEqual(1, runtime.turn_calls)
        with self.ledger.read() as connection:
            execution_count = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ? AND kind = 'execution'",
                (self.task.task_id,),
            ).fetchone()[0]
        self.assertEqual(1, execution_count)

        foreign_definition = current.definition.model_copy(
            update={"idempotency_key": "foreign-spec-without-authoritative-lineage"}
        )
        foreign_spec = current.model_copy(
            update={
                "definition": foreign_definition,
                "definition_digest": foreign_definition.definition_digest,
            }
        )
        self.assertEqual(
            {},
            dispatcher._semantic_evidence_catalog(
                row,
                foreign_spec,
                self.task.validations[0].validation_id,
            ),
        )

    def test_failed_validator_can_rebind_but_failed_worker_cannot(self) -> None:
        self.complete_worker_with_evidence()
        failed_validator = self.service.reserve_attempt(
            task_id=self.task.task_id,
            kind=AttemptKind.VALIDATION,
        )
        self.service.finish_attempt(
            attempt_id=failed_validator.attempt_id,
            succeeded=False,
            failure_class=FailureClass.IMPLEMENTATION,
            detail="validator provider failed",
        )
        result = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator_backup", effort="medium"),
                role="validator",
            ),
            _inventory("worker", "validator_backup"),
        )
        self.assertEqual(AttemptKind.VALIDATION, result.attempt.kind)

        self.tearDown()
        self.setUp()
        failed_worker = self.service.reserve_attempt(task_id=self.task.task_id)
        self.service.finish_attempt(
            attempt_id=failed_worker.attempt_id,
            succeeded=False,
            failure_class=FailureClass.IMPLEMENTATION,
            detail="worker failed",
        )
        before = self._counts()
        with self.assertRaisesRegex(ModelRebindingError, "성공한 Worker 원본"):
            self.rebinding.rebind_and_reserve(
                self.request(
                    ModelChoice(model="validator_backup", effort="medium"),
                    role="validator",
                ),
                _inventory("worker", "validator_backup"),
            )
        self.assertEqual(before, self._counts())

    def test_repeated_validator_rebind_walks_only_authoritative_selection_chain(self) -> None:
        _worker_attempt, evidence = self.complete_worker_with_evidence()
        first = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator_backup", effort="medium"),
                role="validator",
            ),
            _inventory("worker", "validator_backup"),
        )
        self.service.finish_attempt(
            attempt_id=first.attempt.attempt_id,
            succeeded=False,
            failure_class=FailureClass.IMPLEMENTATION,
            detail="first validator unavailable",
        )
        second = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator", effort="high"),
                role="validator",
                current_execution_spec_revision_id=(
                    first.execution_spec.execution_spec_revision_id
                ),
                current_execution_spec_digest=first.execution_spec.definition_digest,
            ),
            _inventory("worker", "validator"),
        )
        dispatcher = EngineDispatcher(
            self.service,
            FakeCodexRuntime(_inventory("worker", "validator")),
        )
        row, current = dispatcher._attempt_context(second.attempt.attempt_id)
        catalog = dispatcher._semantic_evidence_catalog(
            row,
            current,
            self.task.validations[0].validation_id,
        )
        self.assertEqual({evidence.evidence_id}, set(catalog))
        with self.ledger.read() as connection:
            execution_attempts = connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE task_id = ? AND kind = 'execution'",
                (self.task.task_id,),
            ).fetchone()[0]
        self.assertEqual(1, execution_attempts)

    def test_blocked_semantic_validator_can_rebind(self) -> None:
        _worker_attempt, evidence = self.complete_worker_with_evidence()
        validator_attempt = self.service.reserve_attempt(
            task_id=self.task.task_id,
            kind=AttemptKind.VALIDATION,
        )
        review_evidence = EvidenceRecord(
            evidence_id=new_id("evidence"),
            project_id=self.project_id,
            task_id=self.task.task_id,
            attempt_id=validator_attempt.attempt_id,
            kind=EvidenceKind.MODEL_REVIEW,
            source_ref="synthetic-validator-review",
            observation=json.dumps({"passed": False}),
            content_digest="sha256:" + "7" * 64,
            observed_at=utc_now(),
        )
        self.service.record_evidence(review_evidence)
        self.service.finish_attempt(attempt_id=validator_attempt.attempt_id, succeeded=True)
        self.service.record_validation(
            project_id=self.project_id,
            plan_revision_id=self.plan.plan_revision_id,
            result=ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id=self.task.validations[0].validation_id,
                task_id=self.task.task_id,
                status=ValidationStatus.FAIL,
                evidence_ids=(review_evidence.evidence_id, evidence.evidence_id),
                rationale="독립 validator가 실패를 판정함",
                evaluated_at=utc_now(),
            ),
        )
        self.service.block_task_from_validation(
            task_id=self.task.task_id,
            detail="semantic validation failed",
        )
        result = self.rebinding.rebind_and_reserve(
            self.request(
                ModelChoice(model="validator_backup", effort="medium"),
                role="validator",
            ),
            _inventory("worker", "validator_backup"),
        )
        self.assertEqual(AttemptKind.VALIDATION, result.attempt.kind)

    def test_reserved_provider_call_blocks_rebind_project_wide(self) -> None:
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO provider_calls "
                "(id, project_id, goal_id, call_key, role, stage, request_digest, request_json, "
                "estimated_tokens, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("provider_call"),
                    self.project_id,
                    self.goal.goal_id,
                    "unsettled-model-rebind-provider-call",
                    "executor",
                    "execution",
                    "sha256:" + "8" * 64,
                    "{}",
                    1,
                    "reserved",
                    tx.now,
                ),
            )
        before = self._counts()
        with self.assertRaisesRegex(ModelRebindingError, "reserved provider call"):
            self.rebinding.rebind_and_reserve(
                self.request(ModelChoice(model="backup", effort="high")),
                _inventory("backup"),
            )
        self.assertEqual(before, self._counts())

    def test_outside_envelope_and_stale_digest_leave_sqlite_unchanged(self) -> None:
        before = self._counts()
        with self.assertRaisesRegex(ModelRebindingError, "PLAN_MODEL_REVISION_REQUIRED"):
            self.rebinding.rebind_and_reserve(
                self.request(ModelChoice(model="outside", effort="high")),
                _inventory("worker", "validator", "outside"),
            )
        self.assertEqual(before, self._counts())

        with self.assertRaisesRegex(ModelRebindingError, "MODEL_REBIND_BINDING_MISMATCH"):
            self.rebinding.rebind_and_reserve(
                self.request(
                    ModelChoice(model="worker", effort="medium"),
                    current_execution_spec_digest="sha256:" + "f" * 64,
                ),
                _inventory("worker", "validator"),
            )
        self.assertEqual(before, self._counts())

    def test_active_attempt_blocks_rebinding_without_partial_revision(self) -> None:
        active = self.service.reserve_attempt(task_id=self.task.task_id)
        before = self._counts()
        with self.assertRaisesRegex(ModelRebindingError, "MODEL_REBIND_EFFECT_UNRESOLVED"):
            self.rebinding.rebind_and_reserve(
                self.request(ModelChoice(model="backup", effort="high")),
                _inventory("backup", "validator"),
            )
        self.assertEqual(before, self._counts())
        with self.ledger.read() as connection:
            status = connection.execute("SELECT status FROM attempts WHERE id = ?", (active.attempt_id,)).fetchone()[0]
        self.assertEqual("reserved", status)

    def test_unknown_external_effect_blocks_rebinding_without_partial_revision(self) -> None:
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.EXTERNAL_EFFECT,
            idempotency_key="unknown-external-effect-for-rebind",
            request={"effect": "synthetic"},
        )
        self.service.recover_inspect(self.project_id)
        before = self._counts()
        with self.assertRaisesRegex(ModelRebindingError, "MODEL_REBIND_EFFECT_UNRESOLVED"):
            self.rebinding.rebind_and_reserve(
                self.request(ModelChoice(model="backup", effort="high")),
                _inventory("backup", "validator"),
            )
        self.assertEqual(before, self._counts())

    def test_forged_inventory_is_revalidated_before_sqlite_mutation(self) -> None:
        valid = _inventory("worker", "validator")
        forged = valid.model_copy(update={"models": valid.models + (valid.models[0],)})
        before = self._counts()
        with self.assertRaises(ValueError):
            self.rebinding.rebind_and_reserve(
                self.request(ModelChoice(model="worker", effort="medium")), forged
            )
        self.assertEqual(before, self._counts())

    def _counts(self) -> tuple[int, int, int, int]:
        with self.ledger.read() as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "execution_spec_revisions",
                    "attempts",
                    "model_rebinding_selections",
                    "history_events",
                )
            )


if __name__ == "__main__":
    unittest.main()
