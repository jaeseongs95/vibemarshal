from __future__ import annotations

import unittest

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    ApproachSignature,
    CandidateStatus,
    DependencyType,
    FindingSeverity,
    GateName,
    GoalCoverage,
    PlanSkeletonCandidate,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    SkeletonDependency,
    TaskKind,
    TaskSkeleton,
    ValidationResult,
    ValidationStatus,
    derive_candidate_decision,
    new_id,
    utc_now,
)


def _skeleton(*, cycle: bool = False) -> PlanSkeletonCandidate:
    tasks = (
        TaskSkeleton(
            task_ref="task_a",
            kind=TaskKind.INSPECT,
            objective="A를 조사한다.",
            contributes_to=("ac_one",),
            produces=("data:a",),
            consumes=("data:b",) if cycle else ("input:request",),
        ),
        TaskSkeleton(
            task_ref="task_b",
            kind=TaskKind.VALIDATE,
            objective="A를 검증한다.",
            contributes_to=("ac_one",),
            produces=("data:b",),
            consumes=("data:a",),
        ),
    )
    dependencies = [
        SkeletonDependency(
            producer_task_ref="task_a",
            consumer_task_ref="task_b",
            dependency_type=DependencyType.DATA,
            produces=("data:a",),
            consumes=("data:a",),
        )
    ]
    if cycle:
        dependencies.append(
            SkeletonDependency(
                producer_task_ref="task_b",
                consumer_task_ref="task_a",
                dependency_type=DependencyType.DATA,
                produces=("data:b",),
                consumes=("data:b",),
            )
        )
    return PlanSkeletonCandidate(
        candidate_id=new_id("candidate"),
        goal_contract_digest="sha256:" + "1" * 64,
        state_signature="sha256:" + "2" * 64,
        approach=ApproachSignature(
            strategy_family="direct",
            change_shape="small",
            compatibility="preserve",
            rollout_recovery="retry",
        ),
        tasks=tasks,
        dependencies=tuple(dependencies),
        goal_coverage=(GoalCoverage(criterion_id="ac_one", task_refs=("task_a", "task_b")),),
        estimated_change_cost=1,
        estimated_context_tokens=100,
    )


class EngineDomainTests(unittest.TestCase):
    def test_cycle_is_rejected_by_frozen_schema(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cycle"):
            _skeleton(cycle=True)

    def test_models_are_frozen_and_extra_fields_are_forbidden(self) -> None:
        candidate = _skeleton()
        with self.assertRaises(ValidationError):
            candidate.version = 2  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            ReviewerSubmission.model_validate(
                {
                    "reviewer_role": "reviewer",
                    "candidate_digest": sha256_digest(candidate),
                    "findings": [],
                    "ratings": {
                        "goal_fit": 4,
                        "grounding": 4,
                        "engineering": 4,
                        "verification": 4,
                        "execution_safety": 4,
                    },
                    "evidence_catalog_digest": "sha256:" + "3" * 64,
                    "status": "admissible"
                }
            )

    def test_p11_detected_defect_cannot_be_admissible(self) -> None:
        candidate = _skeleton()
        duplicate = ReviewFinding(
            finding_code="DUPLICATE_STRATEGY",
            gate=GateName.PLAN,
            severity=FindingSeverity.ERROR,
            summary="두 후보의 의미 signature가 같습니다.",
            evidence_refs=(sha256_digest(candidate),),
            remediable=True,
        )
        diversity = duplicate.model_copy(
            update={
                "finding_code": "DIVERSITY_FAILURE",
                "summary": "실질적 선택지가 없습니다.",
            }
        )
        with self.assertRaisesRegex(ValidationError, "finding"):
            ReviewerSubmission(
                reviewer_role="plan-reviewer",
                candidate_digest=sha256_digest(candidate),
                findings=(duplicate, diversity),
                ratings=ReviewRatings(
                    goal_fit=4,
                    grounding=4,
                    engineering=4,
                    verification=4,
                    execution_safety=4,
                ),
                evidence_catalog_digest="sha256:" + "3" * 64,
            )
        decision = derive_candidate_decision(
            candidate_digest=sha256_digest(candidate),
            findings=(duplicate, diversity),
            ratings=None,
        )
        self.assertEqual(CandidateStatus.NEEDS_REVISION, decision.status)
        self.assertIsNone(decision.fitness_score)

    def test_clean_ratings_are_scored_by_core(self) -> None:
        candidate = _skeleton()
        decision = derive_candidate_decision(
            candidate_digest=sha256_digest(candidate),
            findings=(),
            ratings=ReviewRatings(
                goal_fit=4,
                grounding=4,
                engineering=3,
                verification=4,
                execution_safety=3,
            ),
        )
        self.assertEqual(CandidateStatus.ADMISSIBLE, decision.status)
        self.assertEqual(90, decision.fitness_score)

    def test_non_blocking_finding_cannot_be_hidden_by_ratings(self) -> None:
        candidate = _skeleton()
        warning = ReviewFinding(
            finding_code="DIRECT_WARNING",
            gate=GateName.ENGINEERING,
            severity=FindingSeverity.WARNING,
            summary="직접 증거가 있는 경고입니다.",
            evidence_refs=(sha256_digest(candidate),),
            remediable=True,
        )
        with self.assertRaisesRegex(ValidationError, "finding"):
            ReviewerSubmission(
                reviewer_role="plan-reviewer",
                candidate_digest=sha256_digest(candidate),
                findings=(warning,),
                ratings=ReviewRatings(
                    goal_fit=4,
                    grounding=4,
                    engineering=4,
                    verification=4,
                    execution_safety=4,
                ),
                evidence_catalog_digest="sha256:" + "3" * 64,
            )

    def test_validation_pass_without_evidence_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "evidence"):
            ValidationResult(
                validation_result_id=new_id("validation_result"),
                validation_id="validation_one",
                status=ValidationStatus.PASS,
                rationale="근거 없이 통과",
                evaluated_at=utc_now(),
            )


if __name__ == "__main__":
    unittest.main()
