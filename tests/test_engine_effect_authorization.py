from __future__ import annotations

import sqlite3
import unittest

from pydantic import ValidationError

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.engine.authorization import authorization_changes
from flowmarshal.engine.budget import BudgetManager
from flowmarshal.engine.domain import (
    ApprovalClass, BudgetStage, CriterionVerdict, EffectContract, EffectIdentity, EvidenceKind,
    EvidenceRecord, ExternalValidationObservation, FailureClass, GoalContractRevision, GoalVerdict,
    GoalVerdictStatus, MutationPolicy, RevisionStatus, TaskKind, ValidationResult,
    ValidationStatus, RuntimeIntentKind, ThreadBinding, ExecutionAction,
    derive_candidate_decision, new_id, utc_now,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation, ExpandedPlanEvaluation, plan_review_evidence_catalog,
    plan_gate, skeleton_gate, skeleton_review_evidence_catalog,
)
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.engine.worker_prompt import assemble_worker_prompt
from tests import engine_helpers as fixtures
from tests.test_engine_ledger_service import EngineServiceFixture


class EngineEffectAuthorizationTests(EngineServiceFixture):
    @staticmethod
    def effect_identity() -> EffectIdentity:
        return EffectIdentity(
            provider="github", system="github-api", target="repo:owner/name",
            account="account:owner", operation="publish-release", scope="release:v1",
            idempotency_key="release-v1-idempotent", checkpoint_policy="none",
        )
    def revised_goal(self, effects, **updates):
        definition = self.goal.definition.model_copy(update={"effect_policy": effects, **updates})
        return GoalContractRevision(
            goal_revision_id=new_id("goal_revision"), goal_id=self.goal.goal_id,
            revision_no=self.goal.revision_no + 1, definition=definition,
            definition_digest=definition.definition_digest, status=RevisionStatus.READY,
            supersedes_goal_revision_id=self.goal.goal_revision_id, created_at=utc_now(),
        )

    def changes(self, approved_effects, requested_effects, *, plan=None, **updates):
        authorization = self.service.authorize_goal(project_id=self.project_id, source="test")
        approved_definition = self.goal.definition.model_copy(update={"effect_policy": approved_effects})
        authorization = authorization.model_copy(update={
            "effect_policy": approved_effects, "goal_contract_digest": approved_definition.definition_digest,
        })
        with self.ledger.read() as connection:
            project = connection.execute("SELECT * FROM projects WHERE id=?", (self.project_id,)).fetchone()
        return authorization_changes(
            authorization, project=project, goal=self.revised_goal(requested_effects, **updates),
            profile_digest=self.profile.definition_digest, plan=plan or self.plan, budget_policies=(),
        )

    def test_effect_restrictions_and_relaxations_are_paired(self):
        base = self.goal.definition.effect_policy
        pairs = (
            (base.model_copy(update={"allowed_external_effects": ("publish",)}), base),
            (base, base.model_copy(update={"prohibited_effects": (*base.prohibited_effects, "delete-archive")})),
            (base.model_copy(update={"irreversible_effects_require_checkpoint": False}),
             base.model_copy(update={"irreversible_effects_require_checkpoint": True})),
        )
        for broad, narrow in pairs:
            with self.subTest(broad=broad, narrow=narrow):
                self.assertEqual((), self.changes(broad, narrow))
                denied = self.changes(narrow, broad)
                self.assertTrue(any(item["boundary"] == "effect" for item in denied))
                self.assertTrue(all(item["evidence_ref"] == self.plan.activation_digest for item in denied))

    def test_read_only_restriction_and_mutation_expansion_are_paired(self):
        base = self.goal.definition.effect_policy
        read_only = base.model_copy(update={"mutation_policy": MutationPolicy.READ_ONLY})
        for mutation in MutationPolicy:
            if mutation is MutationPolicy.READ_ONLY:
                continue
            with self.subTest(mutation=mutation):
                broad = base.model_copy(update={"mutation_policy": mutation})
                self.assertEqual((), self.changes(broad, read_only))
                denied = self.changes(read_only, broad)
                self.assertTrue(any(item["boundary"] == "effect" for item in denied))
                self.assertTrue(all(item["evidence_ref"] == self.plan.activation_digest for item in denied))
        changed_goal = self.changes(base, read_only, observable_outcome="새로운 사용자 목표")
        self.assertTrue(any(item["boundary"] == "goal" for item in changed_goal))

    def test_effect_restriction_does_not_hide_goal_or_acceptance_change(self):
        base = self.goal.definition.effect_policy
        narrow = base.model_copy(update={"prohibited_effects": (*base.prohibited_effects, "delete-archive")})
        for updates in (
            {"observable_outcome": "다른 사용자 목표"},
            {"non_goals": (*self.goal.definition.non_goals, "새로운 제외 범위")},
            {"hard_acceptance": (self.goal.definition.hard_acceptance[0].model_copy(
                update={"statement": "완화한 합격 조건"}), *self.goal.definition.hard_acceptance[1:])},
        ):
            with self.subTest(updates=updates):
                self.assertTrue(any(item["boundary"] == "goal" for item in self.changes(base, narrow, **updates)))

    def test_plan_effect_must_respect_current_restricted_goal(self):
        base = self.goal.definition.effect_policy
        effect = EffectContract(effect_id="publish", external=True, statement="publish")
        task = self.task.model_copy(update={"expected_effects": (effect,)})
        plan = self.plan.model_copy(update={"definition": self.plan.definition.model_copy(update={"tasks": (task,)})})
        broad = base.model_copy(update={"allowed_external_effects": ("publish",)})
        denied = self.changes(broad, base, plan=plan)
        self.assertTrue(any(item["boundary"] == "effect" and item["field"].startswith(task.task_ref) for item in denied))

    def test_typed_effect_identity_is_authorized_by_exact_digest(self):
        identity = self.effect_identity()
        effect = EffectContract(
            effect_id="publish", external=True, reversible=True,
            statement="publish", identity_version="2.0", identity=identity,
        )
        task = self.task.model_copy(update={"expected_effects": (effect,)})
        definition = self.plan.definition.model_copy(update={"tasks": (task,)})
        plan = self.plan.model_copy(update={
            "definition": definition, "definition_digest": definition.definition_digest,
        })
        approved = self.goal.definition.effect_policy.model_copy(update={
            "allowed_external_effects": ("publish",),
            "allowed_external_effect_contracts": (identity,),
        })
        self.assertEqual((), self.changes(approved, approved, plan=plan))
        changed = identity.model_copy(update={"target": "repo:other/name"})
        denied_policy = approved.model_copy(update={
            "allowed_external_effect_contracts": (changed,),
        })
        denied = self.changes(approved, denied_policy, plan=plan)
        self.assertTrue(any(item["boundary"] == "effect" for item in denied))

    def test_mixed_internal_and_external_effects_require_task_split(self):
        identity = self.effect_identity()
        policy = self.goal.definition.effect_policy.model_copy(update={
            "allowed_external_effects": ("publish",),
            "allowed_external_effect_contracts": (identity,),
        })
        goal = self.revised_goal(policy)
        state = fixtures.state(
            self.project_id, goal.definition_digest, self.map.revision_digest,
        )
        source = fixtures.skeleton(goal, state)
        plan, task, _decision = fixtures.plan(
            self.project_id, goal, state, self.map.revision_digest, source, self.inventory,
        )
        task = task.model_copy(update={"expected_effects": (
            EffectContract(
                effect_id="write_file", external=False, reversible=True,
                statement="write project file",
            ),
            EffectContract(
                effect_id="publish", external=True, reversible=True, statement="publish",
                identity_version="2.0", identity=identity,
            ),
        )})
        definition = plan.definition.model_copy(update={"tasks": (task,)})
        plan = plan.model_copy(update={
            "definition": definition, "definition_digest": definition.definition_digest,
        })

        findings = plan_gate(
            plan, source=source, goal=goal, state=state, project_map=self.map,
        )

        self.assertIn(
            "PLAN_MIXED_EFFECT_CONTRACT_REQUIRES_SPLIT",
            {finding.finding_code for finding in findings},
        )

    def test_semantic_task_and_validation_ids_are_independently_bound_at_admission(self):
        semantic = self.task.validations[0].model_copy(update={
            "method": "semantic",
            "required_evidence_kinds": ("model_review",),
        })
        task_payload = self.task.model_dump(mode="python")
        task_payload["validations"] = (semantic,)
        task_payload["assignment"] = self.task.assignment.model_copy(update={
            "validator": None,
            "independence_required": False,
        })
        with self.assertRaisesRegex(ValidationError, "분리된 validator"):
            type(self.task).model_validate(task_payload)

        duplicate_identity = self.effect_identity()
        duplicate_payload = self.task.model_dump(mode="python")
        duplicate_payload["expected_effects"] = (
            EffectContract(
                effect_id="publish_one", external=True, reversible=True,
                statement="publish one", identity_version="2.0",
                identity=duplicate_identity,
            ),
            EffectContract(
                effect_id="publish_two", external=True, reversible=True,
                statement="publish two", identity_version="2.0",
                identity=duplicate_identity,
            ),
        )
        with self.assertRaisesRegex(ValidationError, "external effect identity"):
            type(self.task).model_validate(duplicate_payload)

        duplicate_integration = self.plan.definition.integration_validations[0].model_copy(
            update={"validation_id": self.task.validations[0].validation_id}
        )
        definition_payload = self.plan.definition.model_dump(mode="python")
        definition_payload["integration_validations"] = (duplicate_integration,)
        with self.assertRaisesRegex(ValidationError, "재사용"):
            type(self.plan.definition).model_validate(definition_payload)

    def test_external_target_observation_binds_identity_and_receipt(self):
        identity = self.effect_identity()
        target_document = {
            "provider": "github",
            "selector": "release:v1",
            "observation": "release가 존재한다.",
            "effect_identity_digest": identity.identity_digest,
        }
        observation = ExternalValidationObservation(
            validation_id="observe_release", task_id=self.task.task_id,
            provider="github", selector="release:v1", passed=True,
            observation="release가 존재한다.", receipt_digest=sha256_digest("adapter receipt"),
            effect_identity=identity, effect_identity_digest=identity.identity_digest,
            target_observation_digest=sha256_digest(target_document), observed_at=utc_now(),
        )
        self.assertEqual(identity.identity_digest, observation.effect_identity_digest)
        with self.assertRaisesRegex(ValueError, "target observation digest"):
            ExternalValidationObservation.model_validate(
                observation.model_dump(mode="python") | {
                    "target_observation_digest": sha256_digest("다른 관측")
                }
            )
        with self.assertRaisesRegex(ValueError, "effect identity provider"):
            ExternalValidationObservation.model_validate(
                observation.model_dump(mode="python") | {"provider": "other-provider"}
            )

    def test_external_effect_requires_every_typed_receipt_and_target_observation(self):
        first = self.effect_identity().model_copy(update={
            "checkpoint_policy": "before_irreversible",
        })
        second = first.model_copy(update={
            "operation": "publish-assets",
            "scope": "release-assets:v1",
            "idempotency_key": "release-assets-v1-idempotent",
        })
        effects = tuple(
            EffectContract(
                effect_id=f"publish_{index}", external=True, reversible=False,
                statement=f"publish_{index}",
                identity_version="2.0", identity=identity,
            )
            for index, identity in enumerate((first, second), start=1)
        )
        policy = self.goal.definition.effect_policy.model_copy(update={
            "allowed_external_effects": tuple(item.effect_id for item in effects),
            "allowed_external_effect_contracts": (first, second),
        })
        self.goal = self.revised_goal(policy)
        self.service.register_goal(self.goal)
        self.state = fixtures.state(
            self.project_id, self.goal.definition_digest, self.map.revision_digest,
        )
        self.service.record_state_snapshot(self.state)
        self.skeleton = fixtures.skeleton(self.goal, self.state)
        skeleton_review = fixtures.clean_review(
            sha256_digest(self.skeleton), role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(
                self.skeleton, self.goal, self.state, self.map,
            ),
        )
        self.service.record_skeleton_evaluation(CandidateEvaluation(
            candidate=self.skeleton,
            semantic_submission=skeleton_review,
            decision=derive_candidate_decision(
                candidate_digest=sha256_digest(self.skeleton),
                findings=(), ratings=skeleton_review.ratings,
            ),
        ))
        self.plan, task, _decision = fixtures.plan(
            self.project_id, self.goal, self.state, self.map.revision_digest,
            self.skeleton, self.inventory,
        )
        task = task.model_copy(update={
            "expected_effects": effects,
            "approval_class": ApprovalClass.EXECUTION_CHECKPOINT,
        })
        definition = self.plan.definition.model_copy(update={"tasks": (task,)})
        self.plan = self.plan.model_copy(update={
            "definition": definition,
            "definition_digest": definition.definition_digest,
        })
        self.task = task
        plan_review = fixtures.clean_review(
            self.plan.activation_digest, role="external_effect_reviewer",
            evidence_catalog=plan_review_evidence_catalog(
                self.plan, self.goal, self.state, self.map,
            ),
        )
        self.service.authorize_goal(project_id=self.project_id, source="typed effect test")
        self.service.register_authorized_plan_revision(ExpandedPlanEvaluation(
            plan=self.plan,
            semantic_submissions=(plan_review,),
            decision=derive_candidate_decision(
                candidate_digest=self.plan.activation_digest,
                findings=(), ratings=plan_review.ratings,
            ),
        ))
        spec = self.spec()
        local_action = ExecutionAction(
            action_ref="local_command",
            kind="command",
            description="local command must be split",
            command=("echo", "local"),
        )
        spec_definition = spec.definition.model_copy(update={
            "actions": tuple(
                ExecutionAction(
                    action_ref=f"external_{index}",
                    kind="external_effect",
                    description=effect.statement,
                    effect_id=effect.effect_id,
                )
                for index, effect in enumerate(effects, start=1)
            ),
        })
        bundle = assemble_worker_prompt(
            task=self.task,
            definition=spec_definition,
            profile=self.profile.definition,
            root=self.root,
        )
        spec_definition = spec_definition.model_copy(update={
            "context_manifest": spec_definition.context_manifest.model_copy(update={
                "prompt_binding": bundle.binding,
            }),
        })
        spec = spec.model_copy(update={
            "definition": spec_definition,
            "definition_digest": spec_definition.definition_digest,
        })
        missing_definition = spec_definition.model_copy(update={
            "actions": spec_definition.actions[:1],
        })
        missing = spec.model_copy(update={
            "definition": missing_definition,
            "definition_digest": missing_definition.definition_digest,
        })
        with self.assertRaisesRegex(EngineServiceError, "정확히 대응"):
            self.service.materialize_execution_spec(missing, inventory=self.inventory)
        mixed_definition = spec_definition.model_copy(update={
            "actions": (*spec_definition.actions, local_action),
        })
        mixed = spec.model_copy(update={
            "definition": mixed_definition,
            "definition_digest": mixed_definition.definition_digest,
        })
        with self.assertRaisesRegex(
            EngineServiceError, "MIXED_EFFECT_EXECUTION_SPEC_REQUIRES_TASK_SPLIT",
        ):
            self.service.materialize_execution_spec(mixed, inventory=self.inventory)
        self.service.materialize_execution_spec(spec, inventory=self.inventory)
        with self.assertRaisesRegex(EngineServiceError, "checkpoint"):
            self.service.reserve_attempt(task_id=self.task.task_id)
        checkpoint_ids = tuple(
            self.service.record_effect_checkpoint(
                task_id=self.task.task_id,
                effect_id=effect.effect_id,
                execution_spec_digest=spec.definition_digest,
                approved_by="FM-02 test",
            )
            for effect in effects
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "APPEND_ONLY"):
            with self.ledger.transaction() as tx:
                tx.connection.execute(
                    "UPDATE effect_checkpoints SET effect_id='mutated' WHERE id=?",
                    (checkpoint_ids[0],),
                )
        attempt = self.service.reserve_attempt(task_id=self.task.task_id)
        call_id = BudgetManager(self.service).reserve(
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
            call_key="two-external-effects",
            role="worker",
            stage=BudgetStage.EXECUTION,
            request={"kind": "worker"},
            attempt_id=attempt.attempt_id,
        )
        thread_id = "typed-effect-thread"
        turn_id = "typed-effect-turn"
        effect_identities = [
            identity.model_dump(mode="json") for identity in (first, second)
        ]
        for field, value in (
            ("provider", "other-provider"),
            ("system", "other-system"),
            ("target", "repo:other/name"),
            ("account", "account:other"),
            ("operation", "other-operation"),
            ("scope", "release:other"),
            ("idempotency_key", "other-idempotency-key"),
            ("checkpoint_policy", "always"),
        ):
            changed = first.model_copy(update={field: value}).model_dump(mode="json")
            with self.subTest(runtime_intent_identity_field=field), self.assertRaisesRegex(
                EngineServiceError, "EFFECT_RUNTIME_INTENT_IDENTITY_MISMATCH",
            ):
                self.service.prepare_runtime_intent(
                    attempt_id=attempt.attempt_id,
                    kind=RuntimeIntentKind.START_TURN,
                    idempotency_key=f"typed-effect-mutated-{field}",
                    request={
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "effect_identities": [changed, effect_identities[1]],
                    },
                )
        runtime_intent = self.service.prepare_runtime_intent(
            attempt_id=attempt.attempt_id,
            kind=RuntimeIntentKind.START_TURN,
            idempotency_key="typed-effect-runtime-turn",
            request={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "effect_identities": effect_identities,
            },
        )
        runtime_receipt = self.service.record_runtime_receipt(
            intent_id=runtime_intent.intent_id,
            provider_operation_id=turn_id,
            response={"accepted": True},
            binding=ThreadBinding(thread_id=thread_id, turn_id=turn_id, bound_at=utc_now()),
        )
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET execution_status='terminal',effect_status='unknown',"
                "result_status='valid',status='settled',completed_at=? WHERE id=?",
                (tx.now, call_id),
            )

        def observe(identity: EffectIdentity, label: str) -> str:
            target_document = {
                "provider": "github",
                "selector": label,
                "observation": f"{label} exists",
                "effect_identity_digest": identity.identity_digest,
            }
            target_digest = sha256_digest(target_document)
            receipt_digest = self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=attempt.attempt_id,
                provider_call_id=call_id,
                runtime_intent_id=runtime_intent.intent_id,
                runtime_receipt_id=runtime_receipt.receipt_id,
                thread_id=thread_id,
                turn_id=turn_id,
                effect_identity=identity,
                provider_operation_id=f"operation-{label}",
                response_digest=sha256_digest({"operation": label}),
                target_observation_digest=target_digest,
            )
            self.service._confirm_external_effect_observation(ExternalValidationObservation(
                validation_id="observe_release", task_id=self.task.task_id,
                provider="github", selector=label, passed=True,
                observation=f"{label} exists", receipt_digest=receipt_digest,
                effect_identity=identity, effect_identity_digest=identity.identity_digest,
                target_observation_digest=target_digest, observed_at=utc_now(),
            ))
            return receipt_digest

        with self.assertRaisesRegex(
            EngineServiceError, "EXTERNAL_EFFECT_EXECUTION_BINDING_REQUIRED",
        ):
            self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)

        first_receipt_digest = observe(first, "release:v1")
        with self.ledger.read() as connection:
            status = connection.execute(
                "SELECT effect_status FROM provider_calls WHERE id=?", (call_id,),
            ).fetchone()[0]
        self.assertEqual("unknown", status)

        with self.assertRaisesRegex(
            EngineServiceError, "EXTERNAL_EFFECT_EXECUTION_BINDING_REQUIRED",
        ):
            self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)

        observe(second, "release-assets:v1")
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        task_evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=self.project_id,
            task_id=self.task.task_id, attempt_id=attempt.attempt_id,
            kind=EvidenceKind.TEST, source_ref="typed-effect-task-test",
            observation="task validation passed", content_digest=sha256_digest("task-pass"),
            observed_at=utc_now(),
        )
        self.service.record_evidence(task_evidence)
        self.service.record_validation(
            project_id=self.project_id, plan_revision_id=self.plan.plan_revision_id,
            result=ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id="validation_task", task_id=self.task.task_id,
                status=ValidationStatus.PASS, evidence_ids=(task_evidence.evidence_id,),
                rationale="deterministic task validation passed", evaluated_at=utc_now(),
            ),
        )
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET effect_status='unknown' WHERE id=?",
                (call_id,),
            )
        with self.assertRaisesRegex(
            EngineServiceError, "TASK_PROVIDER_EXECUTION_OR_EFFECT_UNRESOLVED",
        ):
            self.service.complete_task(self.task.task_id)

        # Goal 판정은 Task status만 신뢰하지 않고 provider ledger를 다시 검사해야 한다.
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE task_contracts SET status='completed',updated_at=? WHERE id=?",
                (tx.now, self.task.task_id),
            )
        goal_evidence = EvidenceRecord(
            evidence_id=new_id("evidence"), project_id=self.project_id,
            task_id=None, attempt_id=None, kind=EvidenceKind.TEST,
            source_ref="typed-effect-goal-test", observation="goal test passed",
            content_digest=sha256_digest("goal-pass"), observed_at=utc_now(),
        )
        self.service.record_evidence(goal_evidence)
        integration_result = ValidationResult(
            validation_result_id=new_id("validation_result"),
            validation_id="validation_goal", task_id=None,
            status=ValidationStatus.PASS, evidence_ids=(goal_evidence.evidence_id,),
            rationale="deterministic goal validation passed", evaluated_at=utc_now(),
        )
        self.service.record_validation(
            project_id=self.project_id, plan_revision_id=self.plan.plan_revision_id,
            result=integration_result,
        )

        def verdict() -> GoalVerdict:
            return GoalVerdict(
                goal_verdict_id=new_id("goal_verdict"),
                goal_contract_digest=self.goal.definition_digest,
                plan_activation_digest=self.plan.activation_digest,
                status=GoalVerdictStatus.SATISFIED,
                criteria=(CriterionVerdict(
                    criterion_id="ac_one", status=ValidationStatus.PASS,
                    evidence_ids=(task_evidence.evidence_id,), rationale="criterion passed",
                ),),
                integration_validation_result_ids=(integration_result.validation_result_id,),
                evaluated_at=utc_now(),
            )

        with self.assertRaisesRegex(
            EngineServiceError, "GOAL_PROVIDER_EXECUTION_OR_EFFECT_UNRESOLVED",
        ):
            self.service.record_goal_verdict(
                project_id=self.project_id, plan_revision_id=self.plan.plan_revision_id,
                verdict=verdict(),
            )

        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE provider_calls SET effect_status='terminal' WHERE id=?",
                (call_id,),
            )
        with self.ledger.read() as connection:
            status = connection.execute(
                "SELECT effect_status FROM provider_calls WHERE id=?", (call_id,),
            ).fetchone()[0]
            confirmations = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                "AND event_type='provider_effect.confirmed'", (call_id,),
            ).fetchone()[0]
        self.assertEqual("terminal", status)
        self.assertEqual(2, confirmations)

        replayed = self.service.record_effect_receipt(
            task_id=self.task.task_id,
            attempt_id=attempt.attempt_id,
            provider_call_id=call_id,
            runtime_intent_id=runtime_intent.intent_id,
            runtime_receipt_id=runtime_receipt.receipt_id,
            thread_id=thread_id,
            turn_id=turn_id,
            effect_identity=first,
            provider_operation_id="operation-release:v1",
            response_digest=sha256_digest({"operation": "release:v1"}),
            target_observation_digest=sha256_digest({
                "provider": "github",
                "selector": "release:v1",
                "observation": "release:v1 exists",
                "effect_identity_digest": first.identity_digest,
            }),
        )
        self.assertEqual(first_receipt_digest, replayed)
        with self.assertRaisesRegex(EngineServiceError, "EFFECT_ADAPTER_RECEIPT_CONFLICT"):
            self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=attempt.attempt_id,
                provider_call_id=call_id,
                runtime_intent_id=runtime_intent.intent_id,
                runtime_receipt_id=runtime_receipt.receipt_id,
                thread_id=thread_id,
                turn_id=turn_id,
                effect_identity=first,
                provider_operation_id="operation-release:v1",
                response_digest=sha256_digest({"operation": "conflicting-release"}),
                target_observation_digest=sha256_digest({
                    "provider": "github",
                    "selector": "release:v1",
                    "observation": "release:v1 exists",
                    "effect_identity_digest": first.identity_digest,
                }),
            )

        self.service.record_goal_verdict(
            project_id=self.project_id, plan_revision_id=self.plan.plan_revision_id,
            verdict=verdict(),
        )

        # 이전 attempt의 늦은 receipt는 같은 Task의 최신 retry call에 붙을 수 없다.
        retry_attempt_id = new_id("attempt")
        retry_call_id = new_id("provider_call")
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO attempts (id,project_id,plan_revision_id,task_id,"
                "execution_spec_digest,attempt_no,kind,status,created_at,started_at,updated_at) "
                "VALUES (?,?,?,?,?,2,'execution','running',?,?,?)",
                (retry_attempt_id, self.project_id, self.plan.plan_revision_id,
                 self.task.task_id, spec.definition_digest, tx.now, tx.now, tx.now),
            )
            tx.connection.execute(
                "INSERT INTO provider_calls (id,project_id,goal_id,goal_contract_digest,"
                "call_key,role,stage,request_digest,request_json,estimated_tokens,"
                "execution_status,effect_status,result_status,status,attempt_id,created_at,completed_at) "
                "VALUES (?,?,?,?,?,'worker','execution',?,?,0,'terminal','unknown','valid',"
                "'settled',?,?,?)",
                (retry_call_id, self.project_id, self.goal.goal_id,
                 self.goal.definition_digest, "typed-effect-retry",
                 sha256_digest({"retry": True}), "{}", retry_attempt_id, tx.now, tx.now),
            )
        with self.assertRaisesRegex(
            EngineServiceError, "EFFECT_ADAPTER_RUNTIME_BINDING_MISMATCH",
        ):
            self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=retry_attempt_id,
                provider_call_id=retry_call_id,
                runtime_intent_id=runtime_intent.intent_id,
                runtime_receipt_id=runtime_receipt.receipt_id,
                thread_id=thread_id,
                turn_id=turn_id,
                effect_identity=first,
                provider_operation_id="delayed-old-operation",
                response_digest=sha256_digest({"delayed": True}),
                target_observation_digest=sha256_digest("delayed-target"),
            )
        with self.ledger.read() as connection:
            retry_status = connection.execute(
                "SELECT effect_status FROM provider_calls WHERE id=?", (retry_call_id,),
            ).fetchone()[0]
            retry_receipts = connection.execute(
                "SELECT COUNT(*) FROM history_events WHERE entity_id=? "
                "AND event_type='effect.receipt_recorded'", (retry_call_id,),
            ).fetchone()[0]
        self.assertEqual(("unknown", 0), (retry_status, retry_receipts))

        retry_thread_id = "typed-effect-retry-thread"
        retry_turn_id = "typed-effect-retry-turn"
        retry_intent_id = new_id("runtime_intent")
        retry_runtime_receipt_id = new_id("runtime_receipt")
        retry_binding = ThreadBinding(
            thread_id=retry_thread_id, turn_id=retry_turn_id, bound_at=utc_now(),
        )
        retry_request = {
            "thread_id": retry_thread_id,
            "turn_id": retry_turn_id,
            "effect_identities": effect_identities,
        }
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "UPDATE attempts SET binding_json=?,updated_at=? WHERE id=?",
                (canonical_json(retry_binding), tx.now, retry_attempt_id),
            )
            tx.connection.execute(
                "INSERT INTO runtime_intents (id,attempt_id,kind,idempotency_key,"
                "request_digest,request_json,status,prepared_at,updated_at) "
                "VALUES (?,?,'start_turn',?,?,?,'received',?,?)",
                (retry_intent_id, retry_attempt_id, "typed-effect-retry-runtime-turn",
                 sha256_digest(retry_request), canonical_json(retry_request), tx.now, tx.now),
            )
            tx.connection.execute(
                "INSERT INTO runtime_receipts (id,intent_id,provider_operation_id,"
                "response_digest,payload_json,binding_json,received_at) VALUES (?,?,?,?,?,?,?)",
                (retry_runtime_receipt_id, retry_intent_id, retry_turn_id,
                 sha256_digest({"accepted": True}), "{}", canonical_json(retry_binding), tx.now),
            )
        with self.assertRaisesRegex(
            EngineServiceError, "EXTERNAL_EFFECT_EXECUTION_BINDING_REQUIRED",
        ):
            self.service.finish_attempt(attempt_id=retry_attempt_id, succeeded=True)
        with self.assertRaisesRegex(
            EngineServiceError, "EFFECT_ADAPTER_OPERATION_REPLAY_CONFLICT",
        ):
            self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=retry_attempt_id,
                provider_call_id=retry_call_id,
                runtime_intent_id=retry_intent_id,
                runtime_receipt_id=retry_runtime_receipt_id,
                thread_id=retry_thread_id,
                turn_id=retry_turn_id,
                effect_identity=first,
                provider_operation_id="operation-release:v1",
                response_digest=sha256_digest({"operation": "release:v1"}),
                target_observation_digest=sha256_digest({
                    "provider": "github",
                    "selector": "release:v1",
                    "observation": "release:v1 exists",
                    "effect_identity_digest": first.identity_digest,
                }),
            )

        other_root = self.base / "other-project"
        other_root.mkdir()
        other_project_id = self.service.create_project(
            name="교차 프로젝트", root=other_root,
        )
        with self.ledger.transaction() as tx:
            tx.history(
                other_project_id,
                "effect.receipt_recorded",
                "provider_call",
                new_id("provider_call"),
                {
                    "attempt_id": new_id("attempt"),
                    "effect_identity": first.model_dump(mode="json"),
                    "provider_operation_id": "cross-project-operation",
                    "receipt_digest": sha256_digest("other-project-receipt"),
                },
            )
        with self.assertRaisesRegex(
            EngineServiceError, "EFFECT_ADAPTER_OPERATION_REPLAY_CONFLICT",
        ):
            self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=retry_attempt_id,
                provider_call_id=retry_call_id,
                runtime_intent_id=retry_intent_id,
                runtime_receipt_id=retry_runtime_receipt_id,
                thread_id=retry_thread_id,
                turn_id=retry_turn_id,
                effect_identity=first,
                provider_operation_id="cross-project-operation",
                response_digest=sha256_digest({"operation": "cross-project"}),
                target_observation_digest=sha256_digest("cross-project-target"),
            )

        self.service.finish_attempt(
            attempt_id=retry_attempt_id,
            succeeded=False,
            failure_class=FailureClass.EXTERNAL_UNKNOWN,
            detail="typed effect confirmation이 없어 retry를 종료한다.",
        )

        zero_call_attempt_id = new_id("attempt")
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO attempts (id,project_id,plan_revision_id,task_id,"
                "execution_spec_digest,attempt_no,kind,status,created_at,started_at,updated_at) "
                "VALUES (?,?,?,?,?,3,'execution','running',?,?,?)",
                (zero_call_attempt_id, self.project_id, self.plan.plan_revision_id,
                 self.task.task_id, spec.definition_digest, tx.now, tx.now, tx.now),
            )
        with self.assertRaisesRegex(
            EngineServiceError, "EXTERNAL_EFFECT_EXECUTION_BINDING_REQUIRED",
        ):
            self.service.finish_attempt(
                attempt_id=zero_call_attempt_id, succeeded=True,
            )

        with self.assertRaisesRegex(EngineServiceError, "EFFECT_ADAPTER_RESPONSE_DIGEST_INVALID"):
            self.service.record_effect_receipt(
                task_id=self.task.task_id,
                attempt_id=attempt.attempt_id,
                provider_call_id=call_id,
                runtime_intent_id=runtime_intent.intent_id,
                runtime_receipt_id=runtime_receipt.receipt_id,
                thread_id=thread_id,
                turn_id=turn_id,
                effect_identity=first,
                provider_operation_id="operation-invalid",
                response_digest="not-a-digest",
                target_observation_digest=sha256_digest("target"),
            )

    def test_reviewed_restricted_goal_plan_activates_with_original_authorization(self):
        self.activate_restricted_goal({
            "prohibited_effects": (*self.goal.definition.effect_policy.prohibited_effects, "delete-archive"),
        })

    def test_reviewed_read_only_goal_plan_activates_with_original_authorization(self):
        self.activate_restricted_goal({"mutation_policy": MutationPolicy.READ_ONLY}, task_kind=TaskKind.VALIDATE)

    def test_read_only_restriction_keeps_mutating_task_gate(self):
        effects = self.goal.definition.effect_policy.model_copy(update={"mutation_policy": MutationPolicy.READ_ONLY})
        goal = self.revised_goal(effects)
        state = fixtures.state(self.project_id, goal.definition_digest, self.map.revision_digest)
        candidate = fixtures.skeleton(goal, state)
        findings = skeleton_gate(candidate, goal=goal, state=state, project_map=self.map)
        self.assertTrue(any(item.finding_code == "READ_ONLY_MUTATION" for item in findings))

    def activate_restricted_goal(self, effect_updates, *, task_kind=TaskKind.CHANGE):
        authorization = self.service.authorize_goal(project_id=self.project_id, source="test")
        effects = self.goal.definition.effect_policy.model_copy(update=effect_updates)
        self.goal = self.revised_goal(effects)
        self.service.register_goal(self.goal)
        self.state = fixtures.state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.service.record_state_snapshot(self.state)
        candidate = fixtures.skeleton(self.goal, self.state)
        candidate = candidate.model_copy(update={"tasks": tuple(
            task.model_copy(update={"kind": task_kind}) for task in candidate.tasks
        )})
        review = fixtures.clean_review(sha256_digest(candidate), role="skeleton_reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, self.goal, self.state, self.map))
        self.service.record_skeleton_evaluation(CandidateEvaluation(candidate=candidate,
            semantic_submission=review, decision=derive_candidate_decision(
                candidate_digest=sha256_digest(candidate), findings=(), ratings=review.ratings)))
        plan, task, decision = fixtures.plan(self.project_id, self.goal, self.state, self.map.revision_digest,
                                             candidate, self.inventory)
        task = task.model_copy(update={"kind": task_kind})
        definition = plan.definition.model_copy(update={"tasks": (task,)})
        plan = plan.model_copy(update={"definition": definition, "definition_digest": definition.definition_digest})
        review = fixtures.clean_review(plan.activation_digest, role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(plan, self.goal, self.state, self.map))
        decision = derive_candidate_decision(candidate_digest=plan.activation_digest, findings=(), ratings=review.ratings)
        self.service.register_authorized_plan_revision(ExpandedPlanEvaluation(
            plan=plan, semantic_submissions=(review,), decision=decision))
        with self.ledger.read() as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM goal_authorizations").fetchone()[0])
            row = connection.execute("SELECT authorization_id FROM plan_activations").fetchone()
        self.assertEqual(authorization.authorization_id, row[0])
        self.assertEqual((task.task_id,), self.service.list_ready_tasks(self.project_id))


if __name__ == "__main__":
    unittest.main()
