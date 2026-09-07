from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetedRoleRunner, GoalBudgetPolicy
from flowmarshal.engine.domain import (
    CandidateDecision,
    CandidateStatus,
    FindingSeverity,
    GateName,
    ReviewFinding,
    ReviewerSubmission,
    derive_candidate_decision,
)
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    initialize_cell_budget,
    register_and_attach_goal,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    PlanningSearchOutcome,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.planning_recovery import (
    PlanningRecoveryPolicy,
    settled_candidate_schema_failure,
)
from flowmarshal.engine.planning_feedback import skeleton_semantic_digest
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import (
    RoleCallReceipt,
    RoleCallResult,
    StructuredRoleError,
    make_role_request,
    strict_json_output_schema,
)
from flowmarshal.engine.service import EngineServiceError
from tests.engine_helpers import clean_review, goal, profile, project_map, state, skeleton


class _SchemaFailingProvider:
    max_schema_recovery_attempts = 0

    def __init__(self, receipt: RoleCallReceipt) -> None:
        self.receipt = receipt
        self.receipts: list[RoleCallReceipt] = []

    def run(self, _request, *, validator=None):
        self.receipts.append(self.receipt)
        raise StructuredRoleError("schema failure", receipt=self.receipt)


class _SuccessfulProvider:
    max_schema_recovery_attempts = 0

    def __init__(self, receipt: RoleCallReceipt) -> None:
        self.receipt = receipt
        self.calls = 0

    def run(self, _request, *, validator=None):
        self.calls += 1
        payload = {}
        if validator is not None:
            validator(payload)
        return RoleCallResult(payload=payload, receipt=self.receipt)


class InitialSkeletonFailureLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("지침\n", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "7" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(
            self.project_id, self.goal.definition_digest, self.map.revision_digest
        )
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=100),
            role_timeouts=RoleTimeoutPolicy(),
        )
        self.service, _manager = initialize_cell_budget(
            state_root=Path(self.temporary.name) / "state",
            workspace=self.root,
            project_id=self.project_id,
            profile=self.profile,
            policies=policies,
        )
        register_and_attach_goal(self.service, _manager, self.goal)
        self.service.record_project_map(self.map)
        self.service.record_state_snapshot(self.state)
        self.candidate = skeleton(self.goal, self.state)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _settled_failure(self, *, operation: str, payload: dict, role: str, candidate=None):
        candidate = self.candidate if candidate is None else candidate
        request = make_role_request(
            role=role,
            instructions="initial skeleton schema failure ledger test",
            payload=payload,
            output_schema={"type": "object", "properties": {}},
            model="test-model",
            effort="high",
            inventory_digest="sha256:" + "1" * 64,
            cwd=str(self.root),
        )
        receipt = RoleCallReceipt(
            call_id="model_call_" + operation,
            role=role,
            status="schema_failed",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id="thread_" + operation,
            turn_ids=("turn_" + operation,),
            input_digest=request.request_digest,
            output_digest=None,
            output_schema_digest=sha256_digest(
                strict_json_output_schema(request.output_schema)
            ),
            input_tokens=11,
            cached_input_tokens=1,
            output_tokens=7,
            reasoning_tokens=2,
            usage_available=True,
            latency_ms=1,
            schema_recovery_attempts=0,
            recorded_at=datetime.now(timezone.utc),
        )
        runner = BudgetedRoleRunner(
            _SchemaFailingProvider(receipt),
            self.service,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
        )
        with self.assertRaisesRegex(StructuredRoleError, "schema failure") as captured:
            runner.run(request)
        failure = settled_candidate_schema_failure(
            captured.exception,
            operation=operation,
            source_skeleton_digest=sha256_digest(candidate),
        )
        self.assertIsNotNone(failure)
        return failure

    def _rejected_evaluation(self) -> CandidateEvaluation:
        return CandidateEvaluation(
            candidate=self.candidate,
            decision=CandidateDecision(
                candidate_digest=sha256_digest(self.candidate),
                status=CandidateStatus.REJECTED,
                finding_codes=("MISSING_SEMANTIC_REVIEW",),
            ),
        )

    def _successful_child_reviewer_call(self) -> _SuccessfulProvider:
        child = self.candidate.model_copy(update={
            "candidate_id": "candidate_" + "4" * 32,
            "parent_candidate_id": self.candidate.candidate_id,
            "version": 2,
            "refinement_round": 1,
        })
        request = make_role_request(
            role="skeleton_reviewer",
            instructions="normal refined skeleton review",
            payload={"evidence_catalog": {"artifact:skeleton": child.model_dump(mode="json")}},
            output_schema={"type": "object", "properties": {}},
            model="test-model",
            effort="high",
            inventory_digest="sha256:" + "1" * 64,
            cwd=str(self.root),
        )
        receipt = RoleCallReceipt(
            call_id="model_call_normal_child_review",
            role="skeleton_reviewer",
            status="succeeded",
            model=request.model,
            effort=request.effort,
            inventory_digest=request.inventory_digest,
            permission_profile=":danger-full-access",
            approval_policy="never",
            thread_id="thread_normal_child_review",
            turn_ids=("turn_normal_child_review",),
            input_digest=request.request_digest,
            output_digest=sha256_digest({}),
            output_schema_digest=sha256_digest(strict_json_output_schema(request.output_schema)),
            input_tokens=11,
            cached_input_tokens=1,
            output_tokens=7,
            reasoning_tokens=2,
            usage_available=True,
            latency_ms=1,
            schema_recovery_attempts=0,
            recorded_at=datetime.now(timezone.utc),
        )
        provider = _SuccessfulProvider(receipt)
        runner = BudgetedRoleRunner(
            provider,
            self.service,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
        )
        runner.run(request)
        return provider

    def _needs_revision_evaluation(self) -> CandidateEvaluation:
        catalog = skeleton_review_evidence_catalog(
            self.candidate, self.goal, self.state, self.map
        )
        finding = ReviewFinding(
            finding_code="INITIAL_SKELETON_REPAIR_REQUIRED",
            gate=GateName.PLAN,
            severity=FindingSeverity.ERROR,
            summary="초기 Skeleton의 수정 가능한 결함입니다.",
            evidence_refs=("artifact:skeleton",),
            affected_task_refs=(self.candidate.tasks[0].task_ref,),
            remediable=True,
        )
        submission = ReviewerSubmission(
            reviewer_role="skeleton_reviewer",
            candidate_digest=sha256_digest(self.candidate),
            findings=(finding,),
            evidence_catalog_digest=sha256_digest(catalog),
        )
        return CandidateEvaluation(
            candidate=self.candidate,
            semantic_submission=submission,
            decision=derive_candidate_decision(
                candidate_digest=sha256_digest(self.candidate),
                findings=(finding,),
                ratings=None,
            ),
        )

    def _successful_initial_refinement_outcome(self) -> PlanningSearchOutcome:
        root = self._needs_revision_evaluation()
        child = self.candidate.model_copy(update={
            "candidate_id": "candidate_" + "8" * 32,
            "parent_candidate_id": self.candidate.candidate_id,
            "version": 2,
            "refinement_round": 1,
        })
        catalog = skeleton_review_evidence_catalog(child, self.goal, self.state, self.map)
        submission = clean_review(
            sha256_digest(child),
            role="skeleton_reviewer",
            evidence_catalog=catalog,
        )
        child_evaluation = CandidateEvaluation(
            candidate=child,
            semantic_submission=submission,
            decision=derive_candidate_decision(
                candidate_digest=sha256_digest(child),
                findings=(),
                ratings=submission.ratings,
            ),
        )
        self.service.record_skeleton_evaluation(root)
        self.service.record_skeleton_evaluation(child_evaluation)
        return PlanningSearchOutcome(
            goal_contract_digest=self.goal.definition_digest,
            state_snapshot_digest=self.state.snapshot_digest,
            skeleton_evaluations=(root, child_evaluation),
            shortlist_digests=(),
            plan_evaluations=(),
            logical_role_calls=4,
            candidate_versions=2,
            recovery_policy=PlanningRecoveryPolicy(),
        )

    def _planning_outcome(self, evaluation: CandidateEvaluation, failure) -> PlanningSearchOutcome:
        return PlanningSearchOutcome(
            goal_contract_digest=self.goal.definition_digest,
            state_snapshot_digest=self.state.snapshot_digest,
            skeleton_evaluations=(evaluation,),
            shortlist_digests=(),
            plan_evaluations=(),
            logical_role_calls=1 + int(evaluation.semantic_submission is not None) + 1,
            candidate_versions=1,
            recovery_policy=PlanningRecoveryPolicy(),
            candidate_schema_failures=(failure,),
        )

    def test_initial_review_failure_binds_catalog_selector_and_is_history_idempotent(self) -> None:
        failure = self._settled_failure(
            operation="initial_skeleton_review",
            role="skeleton_reviewer",
            payload={"evidence_catalog": {"artifact:skeleton": self.candidate.model_dump(mode="json")}},
        )
        evaluation = self._rejected_evaluation()
        outcome = self._planning_outcome(evaluation, failure)
        self.service.record_skeleton_evaluation(evaluation)

        search_id = self.service.record_planning_search(outcome)
        self.assertEqual(search_id, self.service.record_planning_search(outcome))
        with self.service.ledger.read() as connection:
            events = connection.execute(
                "SELECT entity_id,payload_json FROM history_events WHERE project_id=? "
                "AND event_type='planning.candidate_schema_failed'",
                (self.project_id,),
            ).fetchall()
        self.assertEqual(1, len(events))
        self.assertEqual(failure.provider_call_id, events[0]["entity_id"])
        self.assertEqual(
            {
                "operation": "initial_skeleton_review",
                "source_skeleton_digest": sha256_digest(self.candidate),
                "source_skeleton_semantic_digest": skeleton_semantic_digest(self.candidate),
                "source_plan_digest": None,
                "receipt_digest": sha256_digest(failure.receipt),
                "search_id": search_id,
            },
            json.loads(events[0]["payload_json"]),
        )

    def test_initial_review_rejects_request_without_artifact_skeleton_catalog_selector(self) -> None:
        with self.assertRaisesRegex(EngineServiceError, "Skeleton"):
            self._settled_failure(
                operation="initial_skeleton_review",
                role="skeleton_reviewer",
                payload={"candidate": self.candidate.model_dump(mode="json")},
            )
        with self.service.ledger.read() as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            )

    def test_normal_initial_refinement_child_reviewer_runs_through_budgeted_runner(self) -> None:
        provider = self._successful_child_reviewer_call()
        self.assertEqual(1, provider.calls)
        with self.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT status FROM provider_calls WHERE role='skeleton_reviewer'"
            ).fetchone()
        self.assertEqual("settled", row["status"])

    def test_recorded_initial_review_failure_blocks_semantically_same_candidate_before_reservation(self) -> None:
        failure = self._settled_failure(
            operation="initial_skeleton_review",
            role="skeleton_reviewer",
            payload={"evidence_catalog": {"artifact:skeleton": self.candidate.model_dump(mode="json")}},
        )
        evaluation = self._rejected_evaluation()
        self.service.record_skeleton_evaluation(evaluation)
        self.service.record_planning_search(self._planning_outcome(evaluation, failure))
        replay = self.candidate.model_copy(update={"candidate_id": "candidate_" + "6" * 32})

        with self.assertRaisesRegex(EngineServiceError, "schema 실패"):
            self._settled_failure(
                operation="initial_skeleton_review",
                role="skeleton_reviewer",
                candidate=replay,
                payload={"evidence_catalog": {"artifact:skeleton": replay.model_dump(mode="json")}},
            )
        with self.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            )

    def test_new_search_cannot_omit_recorded_initial_review_failure(self) -> None:
        failure = self._settled_failure(
            operation="initial_skeleton_review",
            role="skeleton_reviewer",
            payload={"evidence_catalog": {"artifact:skeleton": self.candidate.model_dump(mode="json")}},
        )
        evaluation = self._rejected_evaluation()
        self.service.record_skeleton_evaluation(evaluation)
        self.service.record_planning_search(self._planning_outcome(evaluation, failure))
        omitted_failure = PlanningSearchOutcome(
            goal_contract_digest=self.goal.definition_digest,
            state_snapshot_digest=self.state.snapshot_digest,
            skeleton_evaluations=(evaluation,),
            shortlist_digests=(),
            plan_evaluations=(),
            logical_role_calls=1,
            candidate_versions=1,
            recovery_policy=PlanningRecoveryPolicy(),
        )

        with self.assertRaisesRegex(EngineServiceError, "누락"):
            self.service.record_planning_search(omitted_failure)

    def test_initial_refine_failure_binds_candidate_selector(self) -> None:
        failure = self._settled_failure(
            operation="skeleton_refine",
            role="skeleton_refiner",
            payload={"candidate": self.candidate.model_dump(mode="json")},
        )
        evaluation = self._needs_revision_evaluation()
        self.service.record_skeleton_evaluation(evaluation)
        self.service.record_planning_search(self._planning_outcome(evaluation, failure))

    def test_recorded_initial_refine_failure_consumes_semantic_root_slot_before_reservation(self) -> None:
        failure = self._settled_failure(
            operation="skeleton_refine",
            role="skeleton_refiner",
            payload={"candidate": self.candidate.model_dump(mode="json")},
        )
        evaluation = self._needs_revision_evaluation()
        self.service.record_skeleton_evaluation(evaluation)
        self.service.record_planning_search(self._planning_outcome(evaluation, failure))
        replay = self.candidate.model_copy(update={"candidate_id": "candidate_" + "5" * 32})

        with self.assertRaisesRegex(EngineServiceError, "수정 호출"):
            self._settled_failure(
                operation="skeleton_refine",
                role="skeleton_refiner",
                candidate=replay,
                payload={"candidate": replay.model_dump(mode="json")},
            )
        with self.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            )

    def test_refiner_schema_failure_blocks_same_root_reviewer_before_reservation(self) -> None:
        failure = self._settled_failure(
            operation="skeleton_refine",
            role="skeleton_refiner",
            payload={"candidate": self.candidate.model_dump(mode="json")},
        )
        evaluation = self._needs_revision_evaluation()
        self.service.record_skeleton_evaluation(evaluation)
        self.service.record_planning_search(self._planning_outcome(evaluation, failure))
        replay = self.candidate.model_copy(update={"candidate_id": "candidate_" + "9" * 32})
        request = make_role_request(
            role="skeleton_reviewer",
            instructions="forbidden review after failed initial refinement",
            payload={"evidence_catalog": {"artifact:skeleton": replay.model_dump(mode="json")}},
            output_schema={"type": "object", "properties": {}},
            model="test-model",
            effort="high",
            inventory_digest="sha256:" + "1" * 64,
            cwd=str(self.root),
        )
        provider = _SchemaFailingProvider(None)  # type: ignore[arg-type]
        runner = BudgetedRoleRunner(
            provider,
            self.service,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
        )

        with self.assertRaisesRegex(EngineServiceError, "schema 실패 역할 호출"):
            runner.run(request)
        self.assertEqual([], provider.receipts)
        with self.service.ledger.read() as connection:
            self.assertEqual(
                1,
                connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            )

    def test_prior_successful_refinement_and_current_schema_failure_cannot_restore_slot(self) -> None:
        self.service.record_planning_search(self._successful_initial_refinement_outcome())
        failure = self._settled_failure(
            operation="skeleton_refine",
            role="skeleton_refiner",
            payload={"candidate": self.candidate.model_dump(mode="json")},
        )
        evaluation = self._needs_revision_evaluation()
        current = self._planning_outcome(evaluation, failure)

        with self.assertRaisesRegex(EngineServiceError, "수정 슬롯"):
            self.service.record_planning_search(current)

    def test_initial_refine_rejects_catalog_selector_instead_of_candidate_selector(self) -> None:
        with self.assertRaisesRegex(EngineServiceError, "Skeleton"):
            self._settled_failure(
                operation="skeleton_refine",
                role="skeleton_refiner",
                payload={"evidence_catalog": {"artifact:skeleton": self.candidate.model_dump(mode="json")}},
            )
        with self.service.ledger.read() as connection:
            self.assertEqual(
                0,
                connection.execute("SELECT COUNT(*) FROM provider_calls").fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
