from __future__ import annotations

import unittest

from flowmarshal.canonical import sha256_bytes
from flowmarshal.planning.r31_domain import (
    PlanningRole,
    SessionStrategy,
)
from flowmarshal.planning.r31_session import SessionAdvisor


class PlannerR31SessionTests(unittest.TestCase):
    def test_reviewers_are_always_isolated(self) -> None:
        hint = SessionAdvisor().advise(
            role=PlanningRole.HARD_GATE_REVIEWER,
            candidate_id="candidate.review",
            prior_context_reuse_high=True,
        )
        self.assertEqual(SessionStrategy.ISOLATE, hint.strategy)
        self.assertTrue(hint.independent_review_session)

    def test_context_reuse_and_compaction_handoff_are_explicit(self) -> None:
        digest = sha256_bytes(b"stable-prefix")
        reused = SessionAdvisor().advise(
            role=PlanningRole.CANDIDATE_GENERATOR,
            reusable_prefix_digest=digest,
            prior_context_reuse_high=True,
        )
        handed_off = SessionAdvisor().advise(
            role=PlanningRole.CANDIDATE_GENERATOR,
            reusable_prefix_digest=digest,
            prior_context_reuse_high=True,
            compaction_risk=True,
        )
        self.assertEqual(SessionStrategy.REUSE, reused.strategy)
        self.assertEqual(digest, reused.reusable_prefix_digest)
        self.assertEqual(SessionStrategy.HANDOFF, handed_off.strategy)

    def test_independent_or_failure_contaminated_work_is_isolated(self) -> None:
        for flags in (
            {"independent_subsystem": True},
            {"parallelizable": True},
            {"failure_contamination_risk": True},
        ):
            with self.subTest(flags=flags):
                hint = SessionAdvisor().advise(
                    role=PlanningRole.CANDIDATE_GENERATOR,
                    **flags,
                )
                self.assertEqual(SessionStrategy.ISOLATE, hint.strategy)


if __name__ == "__main__":
    unittest.main()
