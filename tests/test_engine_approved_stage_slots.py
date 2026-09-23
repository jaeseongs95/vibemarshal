from __future__ import annotations

import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.application import EngineApplication
from flowmarshal.engine.domain import (
    PlanContractDefinition, PlanContractRevision, RevisionStatus, RoleSlotAssignment,
    RoleSlotRequirements, RoleStage, derive_candidate_decision, new_id, utc_now,
)
from flowmarshal.engine.planning import ExpandedPlanEvaluation, plan_review_evidence_catalog
from flowmarshal.engine.service import EngineService, EngineServiceError
from tests.engine_helpers import clean_review, plan as base_plan
from tests.test_engine_ledger_service import EngineServiceFixture


def requirements(*, filesystem: str = "read", context_mode: str = "limited") -> RoleSlotRequirements:
    return RoleSlotRequirements(
        input_modalities=("text",), tools=(), filesystem=filesystem,
        allowed_surfaces=("local-subagent",), allowed_runtime_modes=(),
        allow_nested_delegation=False, require_observable=("model",),
        excluded_actors=(), excluded_sessions=(), context_mode=context_mode,
    )


def stages(task_id: str, *, assignment_id: str = "assign_one") -> tuple[RoleStage, ...]:
    return (RoleStage(task_id=task_id, stage_id="stage_one", assignments=(
        RoleSlotAssignment(
            assignment_id=assignment_id, purpose="결과를 검토한다.",
            routing_role="independent-audit", risk_level="high", high_risk=True,
            independence_required=True, requirements=requirements(),
        ),
    )),)


class ApprovedStageSlotTests(EngineServiceFixture):
    def setUp(self) -> None:
        def with_slots(*args):
            revision, task, _ = base_plan(*args)
            definition = PlanContractDefinition.model_validate({
                **revision.definition.model_dump(mode="json"),
                "role_stages": stages(task.task_id),
            })
            revision = revision.model_copy(update={
                "definition": definition, "definition_digest": definition.definition_digest,
            })
            decision = derive_candidate_decision(
                candidate_digest=revision.activation_digest, findings=(),
                ratings=clean_review(
                    revision.activation_digest, role="compact_plan_reviewer",
                    evidence_catalog={},
                ).ratings,
            )
            return revision, task, decision

        with patch("tests.test_engine_ledger_service.plan", side_effect=with_slots):
            super().setUp()
        self.app = EngineApplication(self.service)

    def authorize(self):
        target = self.app.authorization_target(self.project_id)
        capability = self.service.test_authority.issue_goal_authorization(
            ledger_path=self.ledger.path, target=target,
        )
        result = self.app.authorize(
            self.project_id, source="합성 사용자 승인", capability=capability,
            authorization_target=target, selected_plan_revision_id=target.plan_revision_id,
        )
        return result.authorization, result.activation_id

    def test_authorize_read_reauthorize_cancel_and_restart(self) -> None:
        authorization, activation_id = self.authorize()
        first = self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        self.assertEqual(activation_id, first["activation_id"])
        self.assertEqual(authorization.authorization_id, first["authorization_id"])
        self.assertEqual("independent-audit", first["stages"][0]["assignments"][0]["routingRole"])
        self.assertEqual("limited", first["stages"][0]["assignments"][0]["requirements"]["contextMode"])
        self.assertEqual((), first["participation"]["entries"])

        restarted = EngineService(self.ledger)
        self.assertEqual(first, restarted.read_current_approved_role_slot_source(self.project_id, self.task.task_id))
        second_authorization, same_activation = self.authorize()
        second = self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        self.assertEqual(activation_id, same_activation)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second_authorization.authorization_id, second["authorization_id"])
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_STALE"):
            restarted.read_current_approved_role_slot_source(
                self.project_id, self.task.task_id,
                {"authorization_id": first["authorization_id"]},
            )
        self.service.set_workflow_control(self.project_id, state="cancelled", reason="합성 취소")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_UNAVAILABLE"):
            restarted.read_current_approved_role_slot_source(self.project_id, self.task.task_id)

    def test_participation_schema_read_and_replacement(self) -> None:
        self.authorize()
        before = self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        # R16-d가 쓸 저장소의 읽기 계약만 fixture에서 검증한다. 실제 dispatch writer는 이 Task 범위 밖이다.
        with self.ledger.transaction() as tx:
            tx.connection.execute(
                "INSERT INTO role_slot_participation "
                "(id,project_id,plan_revision_id,task_id,actor_id,host,session_id,receipt_digest,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id("participation"), self.project_id, self.plan.plan_revision_id,
                 self.task.task_id, "actor_one", "host_one", "session_one",
                 sha256_digest("synthetic receipt"), tx.now),
            )
            tx.history(self.project_id, "role_slot.participated", "task", self.task.task_id,
                       {"actor_id": "actor_one"})
        after = self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        self.assertGreater(after["source_revision"], before["source_revision"])
        self.assertEqual(({"actorId": "actor_one", "host": "host_one", "sessionId": "session_one"},),
                         after["participation"]["entries"])

        next_task = self.task.model_copy(update={"task_id": new_id("task")})
        definition = PlanContractDefinition.model_validate({
            **self.plan.definition.model_dump(mode="json"),
            "tasks": (next_task,), "role_stages": stages(next_task.task_id, assignment_id="assign_two"),
            "goal_coverage": tuple(coverage.model_copy(update={"task_ids": (next_task.task_id,)})
                                   for coverage in self.plan.definition.goal_coverage),
        })
        revision = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"), plan_id=self.plan.plan_id, revision_no=2,
            definition=definition, definition_digest=definition.definition_digest,
            status=RevisionStatus.READY, supersedes_plan_revision_id=self.plan.plan_revision_id,
            created_at=utc_now(),
        )
        catalog = plan_review_evidence_catalog(revision, self.goal, self.state, self.map)
        review = clean_review(revision.activation_digest, role="compact_plan_reviewer",
                              evidence_catalog=catalog)
        decision = derive_candidate_decision(candidate_digest=revision.activation_digest,
                                             findings=(), ratings=review.ratings)
        self.service.register_plan_evaluation(ExpandedPlanEvaluation(
            plan=revision, semantic_submissions=(review,), decision=decision,
        ))
        self.authorize()
        replacement = self.service.read_current_approved_role_slot_source(self.project_id, next_task.task_id)
        self.assertEqual("assign_two", replacement["stages"][0]["assignments"][0]["assignmentId"])
        self.assertEqual(after["participation"]["entries"], replacement["participation"]["entries"])
        with self.assertRaises(EngineServiceError):
            self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_STALE"):
            self.service.read_current_approved_role_slot_source(
                self.project_id, next_task.task_id,
                {"plan_revision_id": self.plan.plan_revision_id},
            )

    def test_stage_validation_and_missing_store_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            RoleSlotAssignment(
                assignment_id="bad", purpose="검토", routing_role="independent-audit",
                risk_level="low", high_risk=False, independence_required=False,
                requirements=requirements(context_mode="full-history"),
            )
        self.authorize()
        with self.ledger.transaction() as tx:
            tx.connection.execute("UPDATE approved_role_slot_sources SET participation_complete=0")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_PARTICIPATION_INCOMPLETE"):
            self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)

    def test_unscoped_authorization_and_explicit_revocation(self) -> None:
        self.authorize()
        before = self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        self.service.authorize_goal(project_id=self.project_id, source="별도 Goal 승인")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_STALE"):
            self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)
        self.app.revoke_approved_role_slots(self.project_id, reason="슬롯 철회")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_UNAVAILABLE"):
            self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id,
                                                           {"run_id": before["run_id"]})

    def test_unrecorded_legacy_attempt_blocks_complete_participation_claim(self) -> None:
        self.authorize()
        self.service.materialize_execution_spec(self.spec(), inventory=self.inventory)
        self.service.reserve_attempt(task_id=self.task.task_id)
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_PARTICIPATION_INCOMPLETE"):
            self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)

    def test_modified_stored_requirements_are_rejected(self) -> None:
        self.authorize()
        with self.ledger.transaction() as tx:
            tx.connection.execute("UPDATE approved_role_slot_sources SET stages_json='[]'")
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_INVALID"):
            self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)

    def test_activation_digest_tamper_is_rejected(self) -> None:
        self.authorize()
        with self.ledger.transaction() as tx:
            tx.connection.execute("UPDATE plan_activations SET activation_digest=?",
                                  (sha256_digest("changed activation"),))
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_INVALID"):
            self.app.read_current_approved_role_slot_source(self.project_id, self.task.task_id)

    def test_task_permission_bounds_are_checked_by_activation_guard(self) -> None:
        self.authorize()
        original = self.plan.definition.role_stages[0].assignments[0]
        for changed, message in (
            (original.model_copy(update={"requirements": requirements(filesystem="write")}),
             "Task 내부 변경 효과"),
            (original.model_copy(update={"requirements": requirements().model_copy(update={"tools": ("external_tool",)})}),
             "RoleSlot tool"),
        ):
            stage = self.plan.definition.role_stages[0].model_copy(update={"assignments": (changed,)})
            definition = PlanContractDefinition.model_validate({
                **self.plan.definition.model_dump(mode="json"), "role_stages": (stage,),
            })
            candidate = self.plan.model_copy(update={
                "definition": definition, "definition_digest": definition.definition_digest,
            })
            with self.ledger.transaction() as tx:
                project = tx.one("SELECT * FROM projects WHERE id=?", (self.project_id,))
                with self.assertRaisesRegex(EngineServiceError, message):
                    self.service._plan_authorization(tx, project, candidate)


class LegacyStageSlotTests(EngineServiceFixture):
    def test_old_plan_digest_stays_compatible_but_has_no_approved_slots(self) -> None:
        original_digest = self.plan.definition_digest
        self.activate()
        with self.ledger.read() as connection:
            row = connection.execute("SELECT payload_json FROM plan_revisions WHERE id=?",
                                     (self.plan.plan_revision_id,)).fetchone()
        revision = PlanContractRevision.model_validate_json(row["payload_json"])
        self.assertEqual(original_digest, revision.definition_digest)
        self.assertIsNone(revision.definition.role_stages)
        with self.assertRaisesRegex(EngineServiceError, "ROLE_SLOT_SOURCE_UNAVAILABLE"):
            self.service.read_current_approved_role_slot_source(self.project_id, self.task.task_id)


if __name__ == "__main__":
    unittest.main()
