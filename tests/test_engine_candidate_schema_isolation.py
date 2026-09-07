from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetedRoleRunner, GoalBudgetPolicy
from flowmarshal.engine.domain import CandidateStatus, derive_candidate_decision
from flowmarshal.engine.evaluation_budget import (
    EvaluationPolicies,
    initialize_cell_budget,
    register_and_attach_goal,
)
from flowmarshal.engine.planning import (
    CandidateEvaluation,
    SkeletonFirstPlanner,
    plan_review_evidence_catalog,
    skeleton_review_evidence_catalog,
)
from flowmarshal.engine.planning_recovery import (
    CandidateSchemaFailure,
    PlanningRecoveryPolicy,
    settled_candidate_schema_failure,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import (
    RoleCallReceipt,
    RoleCallResult,
    StructuredRoleError,
    make_role_request,
    strict_json_output_schema,
)
from tests.engine_helpers import clean_review, goal, inventory, plan, profile, project_map, state, skeleton


def _receipt(
    *,
    call_id: str,
    role: str = "plan_expander",
    status: str = "schema_failed",
    usage_available: bool = True,
    permission_profile: str = ":danger-full-access",
    approval_policy: str = "never",
    turn_ids: tuple[str, ...] = ("turn-one",),
    schema_recovery_attempts: int = 0,
) -> RoleCallReceipt:
    return RoleCallReceipt(
        call_id=call_id,
        role=role,
        status=status,
        model="test-model",
        effort="high",
        inventory_digest="sha256:" + "1" * 64,
        permission_profile=permission_profile,
        approval_policy=approval_policy,
        thread_id="thread-one",
        turn_ids=turn_ids,
        input_digest="sha256:" + "2" * 64,
        output_digest=None,
        output_schema_digest="sha256:" + "3" * 64,
        input_tokens=11 if usage_available else None,
        cached_input_tokens=1 if usage_available else None,
        output_tokens=7 if usage_available else None,
        reasoning_tokens=2 if usage_available else None,
        usage_available=usage_available,
        latency_ms=19,
        schema_recovery_attempts=schema_recovery_attempts,
        recorded_at=datetime.now(timezone.utc),
    )


class _Generator:
    def __init__(self, candidates):
        self.candidates = tuple(candidates)

    def generate(self, *, candidate_count, **_kwargs):
        return self.candidates[:candidate_count]

    def refine(self, **_kwargs):
        raise AssertionError("이 테스트에서는 정제를 호출하지 않습니다.")


class _CleanSkeletonReviewer:
    def review(self, *, candidate, goal, state, project_map):
        return clean_review(
            sha256_digest(candidate),
            role="compact-skeleton-reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, goal, state, project_map),
        )


class _Expander:
    def __init__(self, *, project_id, goal_revision, snapshot, map_digest, model_inventory, fail_indexes=(), receipt=None, settled=True):
        self.project_id = project_id
        self.goal_revision = goal_revision
        self.snapshot = snapshot
        self.map_digest = map_digest
        self.model_inventory = model_inventory
        self.fail_indexes = set(fail_indexes)
        self.receipt = receipt
        self.settled = settled
        self.calls = 0

    def expand(self, *, candidate, **_kwargs):
        self.calls += 1
        if self.calls in self.fail_indexes:
            raise StructuredRoleError(
                "schema failure",
                receipt=self.receipt,
                settled_provider_call_id=("provider_call_" + str(self.calls)) if self.settled else None,
            )
        return plan(
            self.project_id,
            self.goal_revision,
            self.snapshot,
            self.map_digest,
            candidate,
            self.model_inventory,
        )[0]


class _Reviewer:
    def __init__(self, *, receipt=None, fail_indexes=(), settled=True):
        self.receipt = receipt
        self.fail_indexes = set(fail_indexes)
        self.settled = settled
        self.calls = 0

    def review(self, *, plan, goal, state, project_map, **_kwargs):
        self.calls += 1
        if self.calls in self.fail_indexes:
            raise StructuredRoleError(
                "schema failure",
                receipt=self.receipt,
                settled_provider_call_id=("provider_review_" + str(self.calls)) if self.settled else None,
            )
        return clean_review(
            plan.activation_digest,
            role="compact_plan_reviewer",
            evidence_catalog=plan_review_evidence_catalog(plan, goal, state, project_map),
        )


class _FailingProvider:
    max_schema_recovery_attempts = 0

    def __init__(self, receipt):
        self.receipt = receipt
        self.receipts = []

    def run(self, _request, *, validator=None):
        self.receipts.append(self.receipt)
        raise StructuredRoleError("schema failure", receipt=self.receipt)


class _InitialGenerator(_Generator):
    """초기 Skeleton 단계의 schema 실패 경계를 재현하는 결정적 test double."""

    def __init__(self, candidates, *, refine_actions=(), generate_error=None):
        super().__init__(candidates)
        self.refine_actions = list(refine_actions)
        self.generate_error = generate_error
        self.refine_calls = 0

    def generate(self, *, candidate_count, **kwargs):
        if self.generate_error is not None:
            raise self.generate_error
        return super().generate(candidate_count=candidate_count, **kwargs)

    def refine(self, *, candidate, **_kwargs):
        self.refine_calls += 1
        action = self.refine_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action(candidate)


class _InitialSkeletonReviewer:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def review(self, *, candidate, goal, state, project_map):
        self.calls += 1
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        if action == "finding":
            from flowmarshal.engine.domain import FindingSeverity, GateName, ReviewFinding, ReviewerSubmission

            return ReviewerSubmission(
                reviewer_role="compact-skeleton-reviewer",
                candidate_digest=sha256_digest(candidate),
                findings=(ReviewFinding(
                    finding_code="INITIAL_SKELETON_REPAIR_REQUIRED",
                    gate=GateName.PLAN,
                    severity=FindingSeverity.ERROR,
                    summary="초기 Skeleton의 보완이 필요합니다.",
                    evidence_refs=("artifact:skeleton",),
                    remediable=True,
                ),),
                evidence_catalog_digest=sha256_digest(
                    skeleton_review_evidence_catalog(candidate, goal, state, project_map)
                ),
            )
        return clean_review(
            sha256_digest(candidate),
            role="compact-skeleton-reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(candidate, goal, state, project_map),
        )


class CandidateSchemaIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("지침\n", encoding="utf-8")
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.project_id = "project_" + "9" * 32
        self.profile = profile(self.project_id)
        self.goal = goal(self.project_id, self.profile.definition_digest)
        self.map = project_map(self.project_id, self.root)
        self.state = state(self.project_id, self.goal.definition_digest, self.map.revision_digest)
        self.inventory = inventory()
        self.first = skeleton(self.goal, self.state)
        self.second = self.first.model_copy(update={
            "candidate_id": "candidate_" + "8" * 32,
            "approach": self.first.approach.model_copy(update={"strategy_family": "alternate"}),
        })
        self.shortlist = tuple(sorted(
            (self.first, self.second),
            key=lambda candidate: (
                candidate.estimated_change_cost,
                candidate.estimated_context_tokens,
                sha256_digest(candidate),
            ),
        ))
        self.policy = PlanningRecoveryPolicy()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _planner(self, *, fail_expand=(), expand_receipt=None, expand_settled=True, fail_review=(), review_receipt=None):
        expander = _Expander(
            project_id=self.project_id, goal_revision=self.goal, snapshot=self.state,
            map_digest=self.map.revision_digest, model_inventory=self.inventory,
            fail_indexes=fail_expand, receipt=expand_receipt, settled=expand_settled,
        )
        reviewer = _Reviewer(receipt=review_receipt, fail_indexes=fail_review)
        return SkeletonFirstPlanner(
            generator=_Generator((self.first, self.second)),
            skeleton_reviewer=_CleanSkeletonReviewer(),
            expander=expander,
            plan_reviewer=reviewer,
        ), expander, reviewer

    def _search(self, planner):
        return planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
            recovery_policy=self.policy,
        )

    def test_second_expander_schema_failure_preserves_first_admissible_candidate_and_cost(self) -> None:
        receipt = _receipt(call_id="provider-schema-second")
        planner, expander, reviewer = self._planner(fail_expand=(2,), expand_receipt=receipt)
        outcome = self._search(planner)

        self.assertEqual(2, expander.calls)
        self.assertEqual(1, reviewer.calls)
        self.assertEqual(6, outcome.logical_role_calls)
        self.assertEqual(outcome.plan_evaluations[0].plan.activation_digest, outcome.selected_activation_digest)
        self.assertEqual(1, len(outcome.candidate_schema_failures))
        failure = outcome.candidate_schema_failures[0]
        self.assertEqual("expand", failure.operation)
        self.assertEqual(receipt, failure.receipt)
        self.assertEqual(18, failure.receipt.input_tokens + failure.receipt.output_tokens)
        self.assertEqual(sha256_digest(self.shortlist[1]), failure.source_skeleton_digest)
        self.assertNotEqual(
            failure.source_skeleton_digest,
            outcome.plan_evaluations[0].plan.definition.source_skeleton_digest,
        )

        duplicate = outcome.model_dump(mode="json")
        duplicate["candidate_schema_failures"].append(duplicate["candidate_schema_failures"][0])
        with self.assertRaisesRegex(ValueError, "중복"):
            type(outcome).model_validate(duplicate)
        wrong_count = outcome.model_dump(mode="json")
        wrong_count["logical_role_calls"] += 1
        with self.assertRaisesRegex(ValueError, "logical role call"):
            type(outcome).model_validate(wrong_count)

    def test_first_expander_failure_does_not_prevent_second_candidate_selection(self) -> None:
        planner, expander, _reviewer = self._planner(
            fail_expand=(1,), expand_receipt=_receipt(call_id="provider-schema-first")
        )
        outcome = self._search(planner)
        self.assertEqual(2, expander.calls)
        self.assertEqual(outcome.plan_evaluations[0].plan.activation_digest, outcome.selected_activation_digest)
        self.assertEqual(sha256_digest(self.shortlist[0]), outcome.candidate_schema_failures[0].source_skeleton_digest)

    def test_review_schema_failure_is_recorded_without_admission(self) -> None:
        receipt = _receipt(call_id="provider-schema-review", role="compact_plan_reviewer")
        planner, _expander, reviewer = self._planner(fail_review=(1,), review_receipt=receipt)
        outcome = self._search(planner)
        self.assertEqual(2, reviewer.calls)
        self.assertIsNotNone(outcome.selected_activation_digest)
        self.assertNotEqual(CandidateStatus.ADMISSIBLE, outcome.plan_evaluations[0].decision.status)
        self.assertEqual((), outcome.plan_evaluations[0].semantic_submissions)
        self.assertEqual("review", outcome.candidate_schema_failures[0].operation)

    def test_unverified_schema_failures_propagate_instead_of_being_isolated(self) -> None:
        invalid = (
            _receipt(call_id="unknown", usage_available=False),
            _receipt(call_id="failed", status="failed"),
            _receipt(call_id="timeout", status="timeout"),
            _receipt(call_id="running", status="running"),
            _receipt(call_id="input", status="input_contract_failed"),
            _receipt(call_id="permission", permission_profile="read-only"),
            _receipt(call_id="turns", turn_ids=()),
            _receipt(call_id="recovery", schema_recovery_attempts=1),
        )
        for receipt in invalid:
            with self.subTest(receipt=receipt.call_id):
                planner, _expander, _reviewer = self._planner(
                    fail_expand=(1,), expand_receipt=receipt
                )
                with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
                    self._search(planner)
        planner, _expander, _reviewer = self._planner(
            fail_expand=(1,), expand_receipt=_receipt(call_id="unsettled"), expand_settled=False
        )
        with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
            self._search(planner)

    def _refined_skeleton(self, candidate):
        task = candidate.tasks[0].model_copy(update={
            "detail_requirements": candidate.tasks[0].detail_requirements
            + ("초기 검토 finding을 반영한다.",),
        })
        return candidate.model_copy(update={
            "candidate_id": "candidate_" + "7" * 32,
            "parent_candidate_id": candidate.candidate_id,
            "version": candidate.version + 1,
            "refinement_round": 1,
            "tasks": (task,),
        })

    def _initial_planner(self, *, review_actions, refine_actions=(), generate_error=None):
        generator = _InitialGenerator(
            (self.first, self.second),
            refine_actions=refine_actions,
            generate_error=generate_error,
        )
        reviewer = _InitialSkeletonReviewer(review_actions)
        expander = _Expander(
            project_id=self.project_id,
            goal_revision=self.goal,
            snapshot=self.state,
            map_digest=self.map.revision_digest,
            model_inventory=self.inventory,
        )
        return (
            SkeletonFirstPlanner(
                generator=generator,
                skeleton_reviewer=reviewer,
                expander=expander,
                plan_reviewer=_Reviewer(),
            ),
            generator,
            reviewer,
            expander,
        )

    def _initial_search(self, planner):
        return planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
            recovery_policy=self.policy,
        )

    def test_first_initial_skeleton_review_schema_failure_is_rejected_and_other_candidate_survives(self) -> None:
        receipt = _receipt(
            call_id="initial-review-first",
            role="skeleton_reviewer",
        )
        planner, generator, reviewer, expander = self._initial_planner(
            review_actions=(
                StructuredRoleError("schema failure", receipt=receipt, settled_provider_call_id="provider_initial_review_first"),
                "clean",
            ),
        )

        outcome = self._initial_search(planner)

        self.assertEqual(0, generator.refine_calls)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(1, expander.calls)
        self.assertEqual(1, len(outcome.candidate_schema_failures))
        failure = outcome.candidate_schema_failures[0]
        self.assertEqual("initial_skeleton_review", failure.operation)
        self.assertEqual(sha256_digest(self.first), failure.source_skeleton_digest)
        self.assertIsNone(failure.source_plan_digest)
        failed = next(
            item for item in outcome.skeleton_evaluations
            if sha256_digest(item.candidate) == failure.source_skeleton_digest
        )
        self.assertIsNone(failed.semantic_submission)
        self.assertEqual(CandidateStatus.REJECTED, failed.decision.status)
        self.assertNotIn(failure.source_skeleton_digest, outcome.shortlist_digests)
        self.assertEqual(sha256_digest(self.second), outcome.plan_evaluations[0].plan.definition.source_skeleton_digest)
        self.assertIsNotNone(outcome.selected_activation_digest)

    def test_second_initial_skeleton_review_schema_failure_does_not_reject_first_candidate(self) -> None:
        receipt = _receipt(
            call_id="initial-review-second",
            role="skeleton_reviewer",
        )
        planner, generator, reviewer, expander = self._initial_planner(
            review_actions=(
                "clean",
                StructuredRoleError("schema failure", receipt=receipt, settled_provider_call_id="provider_initial_review_second"),
            ),
        )

        outcome = self._initial_search(planner)

        self.assertEqual(0, generator.refine_calls)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(1, expander.calls)
        self.assertEqual("initial_skeleton_review", outcome.candidate_schema_failures[0].operation)
        self.assertEqual(sha256_digest(self.second), outcome.candidate_schema_failures[0].source_skeleton_digest)
        self.assertEqual(sha256_digest(self.first), outcome.plan_evaluations[0].plan.definition.source_skeleton_digest)
        self.assertIsNotNone(outcome.selected_activation_digest)

    def test_initial_skeleton_refine_schema_failure_consumes_root_slot_without_creating_child(self) -> None:
        receipt = _receipt(call_id="initial-refine", role="skeleton_refiner")
        planner, generator, reviewer, expander = self._initial_planner(
            review_actions=("finding", "clean"),
            refine_actions=(StructuredRoleError(
                "schema failure", receipt=receipt, settled_provider_call_id="provider_initial_refine"
            ),),
        )

        outcome = self._initial_search(planner)

        self.assertEqual(1, generator.refine_calls)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(1, expander.calls)
        self.assertEqual(2, outcome.candidate_versions)
        self.assertEqual(2, len(outcome.skeleton_evaluations))
        failure = outcome.candidate_schema_failures[0]
        self.assertEqual("skeleton_refine", failure.operation)
        self.assertEqual(sha256_digest(self.first), failure.source_skeleton_digest)
        self.assertIsNone(failure.source_plan_digest)
        original = next(item for item in outcome.skeleton_evaluations if item.candidate == self.first)
        self.assertEqual(CandidateStatus.NEEDS_REVISION, original.decision.status)
        self.assertNotIn(sha256_digest(self.first), outcome.shortlist_digests)
        self.assertEqual(sha256_digest(self.second), outcome.plan_evaluations[0].plan.definition.source_skeleton_digest)

    def test_refined_initial_skeleton_review_schema_failure_cannot_reach_shortlist_or_plan(self) -> None:
        receipt = _receipt(call_id="refined-initial-review", role="skeleton_reviewer")
        planner, generator, reviewer, expander = self._initial_planner(
            review_actions=(
                "finding",
                "clean",
                StructuredRoleError(
                    "schema failure", receipt=receipt, settled_provider_call_id="provider_refined_initial_review"
                ),
            ),
            refine_actions=(self._refined_skeleton,),
        )

        outcome = self._initial_search(planner)

        refined = self._refined_skeleton(self.first)
        self.assertEqual(1, generator.refine_calls)
        self.assertEqual(3, reviewer.calls)
        self.assertEqual(1, expander.calls)
        self.assertEqual(3, outcome.candidate_versions)
        failure = outcome.candidate_schema_failures[0]
        self.assertEqual("initial_skeleton_review", failure.operation)
        self.assertEqual(sha256_digest(refined), failure.source_skeleton_digest)
        self.assertIsNone(failure.source_plan_digest)
        failed = next(item for item in outcome.skeleton_evaluations if item.candidate == refined)
        self.assertIsNone(failed.semantic_submission)
        self.assertEqual(CandidateStatus.REJECTED, failed.decision.status)
        self.assertNotIn(sha256_digest(refined), outcome.shortlist_digests)
        self.assertTrue(all(
            item.plan.definition.source_skeleton_digest != sha256_digest(refined)
            for item in outcome.plan_evaluations
        ))

    def test_all_initial_skeleton_review_schema_failures_leave_no_shortlist_or_plan(self) -> None:
        planner, _generator, reviewer, expander = self._initial_planner(
            review_actions=(
                StructuredRoleError(
                    "schema failure", receipt=_receipt(call_id="all-initial-first", role="skeleton_reviewer"),
                    settled_provider_call_id="provider_all_initial_first",
                ),
                StructuredRoleError(
                    "schema failure", receipt=_receipt(call_id="all-initial-second", role="skeleton_reviewer"),
                    settled_provider_call_id="provider_all_initial_second",
                ),
            ),
        )

        outcome = self._initial_search(planner)

        self.assertEqual(2, reviewer.calls)
        self.assertEqual(0, expander.calls)
        self.assertEqual((), outcome.shortlist_digests)
        self.assertEqual((), outcome.plan_evaluations)
        self.assertIsNone(outcome.selected_activation_digest)
        self.assertEqual(
            {"initial_skeleton_review"},
            {failure.operation for failure in outcome.candidate_schema_failures},
        )

    def test_initial_schema_failure_rejects_forged_success_retry_and_phase_mixing(self) -> None:
        receipt = _receipt(call_id="forged-initial-review", role="skeleton_reviewer")
        planner, _generator, _reviewer, _expander = self._initial_planner(
            review_actions=(
                StructuredRoleError(
                    "schema failure", receipt=receipt, settled_provider_call_id="provider_forged_initial_review"
                ),
                "clean",
            ),
        )
        outcome = self._initial_search(planner)
        raw = outcome.model_dump(mode="json")
        failure = raw["candidate_schema_failures"][0]

        # 같은 실패 후보에 성공 검토를 덧붙여 admission을 복구할 수 없다.
        success = clean_review(
            sha256_digest(self.first),
            role="compact-skeleton-reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(self.first, self.goal, self.state, self.map),
        )
        forged_success = outcome.model_dump(mode="json")
        forged_success["skeleton_evaluations"][0]["semantic_submission"] = success.model_dump(mode="json")
        with self.assertRaises(ValueError):
            type(outcome).model_validate(forged_success)

        # 실패 뒤 다른 call ID의 같은 초기 검토를 재시도한 원장은 거부한다.
        forged_retry = outcome.model_dump(mode="json")
        duplicate = dict(failure)
        duplicate["provider_call_id"] = "provider_forged_initial_review_retry"
        duplicate["receipt"] = dict(duplicate["receipt"])
        duplicate["receipt"]["call_id"] = "forged-initial-review-retry"
        forged_retry["candidate_schema_failures"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "schema 실패"):
            type(outcome).model_validate(forged_retry)

        # 초기 검토 실패에 상세 Plan을 섞어 단계 경계를 우회할 수 없다.
        forged_phase = outcome.model_dump(mode="json")
        forged_phase["candidate_schema_failures"][0]["source_plan_digest"] = (
            outcome.plan_evaluations[0].plan.activation_digest
        )
        with self.assertRaises(ValueError):
            type(outcome).model_validate(forged_phase)

    def test_failed_initial_refine_cannot_be_forged_into_successful_child_or_slot_recovery(self) -> None:
        receipt = _receipt(call_id="forged-initial-refine", role="skeleton_refiner")
        planner, _generator, _reviewer, _expander = self._initial_planner(
            review_actions=("finding", "clean"),
            refine_actions=(StructuredRoleError(
                "schema failure", receipt=receipt, settled_provider_call_id="provider_forged_initial_refine"
            ),),
        )
        outcome = self._initial_search(planner)
        child = self._refined_skeleton(self.first)
        submission = clean_review(
            sha256_digest(child),
            role="compact-skeleton-reviewer",
            evidence_catalog=skeleton_review_evidence_catalog(child, self.goal, self.state, self.map),
        )
        child_evaluation = CandidateEvaluation(
            candidate=child,
            semantic_submission=submission,
            decision=derive_candidate_decision(
                candidate_digest=sha256_digest(child), findings=(), ratings=submission.ratings,
            ),
        )
        forged = outcome.model_dump(mode="json")
        forged["skeleton_evaluations"].append(child_evaluation.model_dump(mode="json"))
        forged["candidate_versions"] += 1
        with self.assertRaises(ValueError):
            type(outcome).model_validate(forged)

    def test_initial_batch_and_unverified_initial_stage_failures_propagate(self) -> None:
        generation_error = StructuredRoleError(
            "schema failure",
            receipt=_receipt(call_id="initial-batch", role="skeleton_generator"),
            settled_provider_call_id="provider_initial_batch",
        )
        planner, _generator, _reviewer, _expander = self._initial_planner(
            review_actions=(), generate_error=generation_error,
        )
        with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
            self._initial_search(planner)

        invalid_receipts = (
            _receipt(call_id="initial-usage", role="skeleton_reviewer", usage_available=False),
            _receipt(call_id="initial-status", role="skeleton_reviewer", status="failed"),
            _receipt(call_id="initial-input", role="skeleton_reviewer", status="input_contract_failed"),
            _receipt(call_id="initial-policy", role="skeleton_reviewer", permission_profile="read-only"),
            _receipt(call_id="initial-turn", role="skeleton_reviewer", turn_ids=()),
            _receipt(call_id="initial-recovery", role="skeleton_reviewer", schema_recovery_attempts=1),
        )
        for invalid in invalid_receipts:
            with self.subTest(receipt=invalid.call_id):
                planner, _generator, _reviewer, _expander = self._initial_planner(
                    review_actions=(StructuredRoleError(
                        "schema failure", receipt=invalid, settled_provider_call_id="provider_" + invalid.call_id
                    ), "clean"),
                )
                with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
                    self._initial_search(planner)
        planner, _generator, _reviewer, _expander = self._initial_planner(
            review_actions=(StructuredRoleError(
                "schema failure",
                receipt=_receipt(call_id="initial-unsettled", role="skeleton_reviewer"),
            ), "clean"),
        )
        with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
            self._initial_search(planner)

    def test_schema_isolation_requires_effect_started_unique_current_receipt_and_repropagates_other_errors(self) -> None:
        primary = _receipt(call_id="single-settled", role="skeleton_reviewer")
        normal = StructuredRoleError(
            "schema failure",
            receipt=primary,
            settled_provider_call_id="provider_single_settled",
        )
        isolated = settled_candidate_schema_failure(
            normal,
            operation="initial_skeleton_review",
            source_skeleton_digest=sha256_digest(self.first),
        )
        self.assertIsNotNone(isolated)
        self.assertEqual(primary, isolated.receipt)

        invalid_errors = (
            StructuredRoleError(
                "schema failure", receipt=primary, receipts=(primary, primary),
                settled_provider_call_id="provider_duplicate_current_receipt",
            ),
            StructuredRoleError(
                "schema failure",
                receipt=_receipt(call_id="effect-not-started", role="skeleton_reviewer"),
                settled_provider_call_id="provider_effect_not_started",
                effects_started=False,
            ),
            StructuredRoleError(
                "schema failure",
                receipt=_receipt(call_id="multiple-primary", role="skeleton_reviewer"),
                receipts=(
                    _receipt(call_id="multiple-primary", role="skeleton_reviewer"),
                    _receipt(call_id="multiple-second", role="skeleton_reviewer"),
                ),
                settled_provider_call_id="provider_multiple_receipts",
            ),
        )
        for error in invalid_errors:
            with self.subTest(error=error.receipt.call_id):
                self.assertIsNone(settled_candidate_schema_failure(
                    error,
                    operation="initial_skeleton_review",
                    source_skeleton_digest=sha256_digest(self.first),
                ))
                planner, _generator, _reviewer, _expander = self._initial_planner(
                    review_actions=(error, "clean"),
                )
                with self.assertRaisesRegex(StructuredRoleError, "schema failure"):
                    self._initial_search(planner)

    def test_second_initial_failure_allows_prior_runner_receipts_without_counting_them_twice(self) -> None:
        previous = _receipt(call_id="prior-success", role="skeleton_reviewer", status="succeeded")
        current = _receipt(call_id="current-failure", role="skeleton_reviewer")
        error = StructuredRoleError(
            "schema failure", receipt=current, receipts=(previous, current),
            settled_provider_call_id="provider_current_failure",
        )
        planner, generator, reviewer, expander = self._initial_planner(
            review_actions=("clean", error),
        )
        outcome = self._initial_search(planner)
        self.assertEqual(2, reviewer.calls)
        self.assertEqual(0, generator.refine_calls)
        self.assertEqual(1, expander.calls)
        self.assertIsNotNone(outcome.selected_activation_digest)
        self.assertEqual(5, outcome.logical_role_calls)
        self.assertEqual((current,), tuple(item.receipt for item in outcome.candidate_schema_failures))

    def test_initial_review_failure_cannot_be_forged_as_expand(self) -> None:
        receipt = _receipt(call_id="initial-to-expand", role="skeleton_reviewer")
        planner, _generator, _reviewer, _expander = self._initial_planner(
            review_actions=(
                StructuredRoleError(
                    "schema failure", receipt=receipt, settled_provider_call_id="provider_initial_to_expand"
                ),
                "clean",
            ),
        )
        outcome = self._initial_search(planner)

        for source_plan_digest in (None, outcome.plan_evaluations[0].plan.activation_digest):
            with self.subTest(source_plan_digest=source_plan_digest):
                forged = outcome.model_dump(mode="json")
                failure = forged["candidate_schema_failures"][0]
                failure["operation"] = "expand"
                failure["source_plan_digest"] = source_plan_digest
                failure["receipt"] = dict(failure["receipt"])
                failure["receipt"]["role"] = "plan_expander"
                with self.assertRaises(ValueError):
                    type(outcome).model_validate(forged)

    def test_v1_success_serialization_omits_optional_recovery_fields_and_keeps_expand_validation(self) -> None:
        planner, _expander, _reviewer = self._planner()
        outcome = planner.search(
            goal=self.goal,
            state=self.state,
            project_map=self.map,
            candidate_count=2,
        )
        serialized = outcome.model_dump(mode="json")
        self.assertNotIn("recovery_policy", serialized)
        self.assertNotIn("candidate_schema_failures", serialized)
        self.assertEqual(outcome, type(outcome).model_validate(serialized))

        valid_expand = CandidateSchemaFailure(
            operation="expand",
            source_skeleton_digest=sha256_digest(self.first),
            provider_call_id="provider-v1-expand",
            receipt=_receipt(call_id="v1-expand", role="plan_expander"),
        )
        self.assertEqual("expand", valid_expand.operation)
        with self.assertRaises(ValueError):
            CandidateSchemaFailure(
                operation="expand",
                source_skeleton_digest=sha256_digest(self.first),
                provider_call_id="provider-v1-expand-wrong-role",
                receipt=_receipt(call_id="v1-expand-wrong-role", role="skeleton_reviewer"),
            )

    def test_budgeted_runner_sets_settled_provider_call_id_only_after_sqlite_settlement(self) -> None:
        policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=100),
            role_timeouts=RoleTimeoutPolicy(),
        )
        service, _manager = initialize_cell_budget(
            state_root=Path(self.temporary.name) / "state",
            workspace=self.root,
            project_id=self.project_id,
            profile=self.profile,
            policies=policies,
        )
        register_and_attach_goal(service, _manager, self.goal)
        request = make_role_request(
            role="plan_expander",
            instructions="schema isolation",
            payload={},
            output_schema={"type": "object", "properties": {}},
            model="test-model",
            effort="high",
            inventory_digest="sha256:" + "4" * 64,
            cwd=str(self.root),
        )
        receipt = _receipt(
            call_id="runtime-schema-call",
            role="plan_expander",
        ).model_copy(update={
            "model": request.model,
            "effort": request.effort,
            "inventory_digest": request.inventory_digest,
            "input_digest": request.request_digest,
            "output_schema_digest": sha256_digest(strict_json_output_schema(request.output_schema)),
        })
        runner = BudgetedRoleRunner(
            _FailingProvider(receipt),
            service,
            project_id=self.project_id,
            goal_id=self.goal.goal_id,
            goal_digest=self.goal.definition_digest,
        )
        with self.assertRaisesRegex(StructuredRoleError, "schema failure") as captured:
            runner.run(request)
        error = captured.exception
        self.assertIsNotNone(error.settled_provider_call_id)
        with service.ledger.read() as connection:
            row = connection.execute(
                "SELECT status,usage_id FROM provider_calls WHERE id=?", (error.settled_provider_call_id,)
            ).fetchone()
            usage = connection.execute(
                "SELECT payload_json FROM budget_usage WHERE id=?", (row["usage_id"],)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("settled", row["status"])
        self.assertIsNotNone(usage)
        recorded = json.loads(usage["payload_json"])
        self.assertTrue(recorded["usage_available"])
        self.assertEqual(11, recorded["input_tokens"])
        self.assertEqual(7, recorded["output_tokens"])


if __name__ == "__main__":
    unittest.main()
