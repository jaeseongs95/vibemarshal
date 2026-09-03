from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.canonical import canonical_json, sha256_digest
from flowmarshal.domain import (
    ATTEMPT_TRANSITIONS,
    INTENT_TRANSITIONS,
    REVISION_TRANSITIONS,
    AttemptStatus,
    CompletionCriterion,
    DomainError,
    IntentStatus,
    PlanDraft,
    RevisionStatus,
    VerificationSpec,
    VerificationType,
    WorkItemDefinition,
    dependency_order,
    require_attempt_transition,
    require_intent_transition,
    require_revision_transition,
)


def criterion() -> tuple[CompletionCriterion, ...]:
    return (
        CompletionCriterion(
            criterion_id="done",
            description="완료",
            verification=VerificationSpec(
                type=VerificationType.ARTIFACT,
                relative_path="result.txt",
                predicate="exists",
            ),
        ),
    )


class Gate0BDomainTests(unittest.TestCase):
    def test_canonical_digest_is_independent_of_dict_order(self) -> None:
        left = {"한글": [1, True], "a": {"z": 2, "b": None}}
        right = {"a": {"b": None, "z": 2}, "한글": [1, True]}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(sha256_digest(left), sha256_digest(right))

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json({"when": datetime(2026, 9, 2)})

    def test_plan_rejects_cycle_and_unknown_dependency(self) -> None:
        with self.assertRaises(ValidationError):
            PlanDraft(
                project_id="project_test",
                summary="순환",
                work_items=(
                    WorkItemDefinition(
                        client_ref="a",
                        goal="a",
                        dependencies=("b",),
                        write_resource_id="resource_test",
                        completion_criteria=criterion(),
                    ),
                    WorkItemDefinition(
                        client_ref="b",
                        goal="b",
                        dependencies=("a",),
                        write_resource_id="resource_test",
                        completion_criteria=criterion(),
                    ),
                ),
            )
        with self.assertRaises(ValidationError):
            PlanDraft(
                project_id="project_test",
                summary="누락",
                work_items=(
                    WorkItemDefinition(
                        client_ref="a",
                        goal="a",
                        dependencies=("missing",),
                        write_resource_id="resource_test",
                        completion_criteria=criterion(),
                    ),
                ),
            )

    def test_dependency_order_is_stable(self) -> None:
        items = (
            WorkItemDefinition(
                client_ref="c",
                goal="c",
                dependencies=("a", "b"),
                write_resource_id="resource_test",
                completion_criteria=criterion(),
            ),
            WorkItemDefinition(
                client_ref="b",
                goal="b",
                write_resource_id="resource_test",
                completion_criteria=criterion(),
            ),
            WorkItemDefinition(
                client_ref="a",
                goal="a",
                write_resource_id="resource_test",
                completion_criteria=criterion(),
            ),
        )
        self.assertEqual(("a", "b", "c"), dependency_order(items))

    def test_every_declared_transition_and_forbidden_transition(self) -> None:
        for current, targets in REVISION_TRANSITIONS.items():
            for target in RevisionStatus:
                if target in targets:
                    require_revision_transition(current, target)
                else:
                    with self.assertRaises(DomainError):
                        require_revision_transition(current, target)
        for current, targets in ATTEMPT_TRANSITIONS.items():
            for target in AttemptStatus:
                if target in targets:
                    require_attempt_transition(current, target)
                else:
                    with self.assertRaises(DomainError):
                        require_attempt_transition(current, target)
        for current, targets in INTENT_TRANSITIONS.items():
            for target in IntentStatus:
                if target in targets:
                    require_intent_transition(current, target)
                else:
                    with self.assertRaises(DomainError):
                        require_intent_transition(current, target)

    def test_definition_digest_excludes_lineage_and_client_reference(self) -> None:
        base = WorkItemDefinition(
            client_ref="old_name",
            goal="같은 목표",
            write_resource_id="resource_test",
            completion_criteria=criterion(),
        )
        successor = WorkItemDefinition(
            client_ref="new_name",
            previous_work_item_id="work_0123456789abcdef0123456789abcdef",
            goal="같은 목표",
            write_resource_id="resource_test",
            completion_criteria=criterion(),
        )
        self.assertEqual(base.definition_digest, successor.definition_digest)


if __name__ == "__main__":
    unittest.main()
