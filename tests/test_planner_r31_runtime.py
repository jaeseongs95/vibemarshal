from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flowmarshal.planning.r31_domain import ModelCallStatus, PlanningRole
from flowmarshal.planning.r31_models import ModelRolePreference
from flowmarshal.planning.r31_runtime import (
    PlanningRoleInstructions,
    PlanningRuntimeResolutionError,
    build_planning_runtime,
)
from flowmarshal.planning.r31_store import PlanningArtifactRepository, PlanningRunService


def _raw_models(*, include_sol: bool = True):
    values = [
        {
            "id": "gpt-5.6-luna",
            "displayName": "Luna",
            "supportedReasoningEfforts": [
                {"reasoningEffort": value}
                for value in ("low", "medium", "high", "xhigh", "max")
            ],
        },
        {
            "id": "gpt-5.6-terra",
            "displayName": "Terra",
            "supportedReasoningEfforts": [
                {"reasoningEffort": value}
                for value in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
        },
    ]
    if include_sol:
        values.append(
            {
                "id": "gpt-5.6-sol",
                "displayName": "Sol",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": value}
                    for value in ("low", "medium", "high", "xhigh", "max", "ultra")
                ],
            }
        )
    return values


class _InventoryClient:
    def __init__(self, models):
        self._models = models
        self.closed = False

    def models(self, *, include_hidden=False):
        return {"data": self._models}

    def thread_start(self, **kwargs):  # pragma: no cover - build 단계에서는 호출 금지
        raise AssertionError("runtime 조립이 model turn을 시작하면 안 됩니다.")

    def close(self):
        self.closed = True


def _instructions() -> PlanningRoleInstructions:
    return PlanningRoleInstructions(
        purpose_resolver="목적과 요구를 typed artifact로 만든다.",
        intent_reviewer="요구와 Mission을 독립 검토한다.",
        candidate_generator="동일 Mission의 계획 후보를 생성한다.",
        hard_gate_reviewer="5개 Hard Gate를 독립 검토한다.",
        critical_reviewer="고위험 Gate를 강하게 독립 검토한다.",
        scorer_selector="admissible 후보를 점수화하고 walkthrough한다.",
    )


def _preferences() -> tuple[ModelRolePreference, ...]:
    return (
        ModelRolePreference(
            role=PlanningRole.PURPOSE_RESOLVER,
            preferred_model_ids=("gpt-5.6-luna",),
            preferred_effort="medium",
        ),
        ModelRolePreference(
            role=PlanningRole.INTENT_REVIEWER,
            preferred_model_ids=("gpt-5.6-terra",),
            preferred_effort="high",
        ),
        ModelRolePreference(
            role=PlanningRole.CANDIDATE_GENERATOR,
            preferred_model_ids=("gpt-5.6-luna",),
            preferred_effort="medium",
        ),
        ModelRolePreference(
            role=PlanningRole.HARD_GATE_REVIEWER,
            preferred_model_ids=("gpt-5.6-terra",),
            preferred_effort="high",
        ),
        ModelRolePreference(
            role=PlanningRole.CRITICAL_REVIEWER,
            preferred_model_ids=("gpt-5.6-sol",),
            preferred_effort="xhigh",
        ),
        ModelRolePreference(
            role=PlanningRole.SCORER_SELECTOR,
            preferred_model_ids=("gpt-5.6-terra",),
            preferred_effort="high",
        ),
    )


class PlannerR31RuntimeTests(unittest.TestCase):
    def test_runtime_composes_real_role_adapters_from_model_list(self) -> None:
        models = _raw_models()
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_planning_runtime(
                client_factory=lambda: _InventoryClient(models),
                run_service=PlanningRunService(directory),
                artifact_repository=PlanningArtifactRepository(directory),
                cwd=directory,
                instructions=_instructions(),
                preferences=_preferences(),
            )
        self.assertEqual(
            "gpt-5.6-luna",
            runtime.model_for(PlanningRole.CANDIDATE_GENERATOR).model_id,
        )
        self.assertEqual(
            "gpt-5.6-terra",
            runtime.model_for(PlanningRole.HARD_GATE_REVIEWER).model_id,
        )
        self.assertEqual(
            "gpt-5.6-sol",
            runtime.model_for(PlanningRole.CRITICAL_REVIEWER).model_id,
        )
        self.assertIsNotNone(runtime.mission_service)
        self.assertIsNotNone(runtime.requirement_service)
        self.assertIsNotNone(runtime.search_service)

    def test_missing_required_critical_model_fails_without_fallback(self) -> None:
        models = _raw_models(include_sol=False)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PlanningRuntimeResolutionError) as raised:
                build_planning_runtime(
                    client_factory=lambda: _InventoryClient(models),
                    run_service=PlanningRunService(directory),
                    artifact_repository=PlanningArtifactRepository(directory),
                    cwd=directory,
                    instructions=_instructions(),
                    preferences=_preferences(),
                )
            persisted = list(Path(directory).rglob("model-calls/*.json"))
        self.assertEqual(
            ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
            raised.exception.receipt.status,
        )
        self.assertEqual(PlanningRole.CRITICAL_REVIEWER, raised.exception.receipt.role)
        self.assertEqual(1, len(persisted))

    def test_runtime_rejects_duplicate_or_unused_role_preferences(self) -> None:
        models = _raw_models()
        duplicate = _preferences() + (_preferences()[0],)
        unused = _preferences() + (
            ModelRolePreference(
                role=PlanningRole.SESSION_ADVISOR,
                preferred_model_ids=("gpt-5.6-luna",),
                preferred_effort="medium",
            ),
        )
        for preferences, message in (
            (duplicate, "중복"),
            (unused, "소비하지 않는"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, message):
                    build_planning_runtime(
                        client_factory=lambda: _InventoryClient(models),
                        run_service=PlanningRunService(directory),
                        artifact_repository=PlanningArtifactRepository(directory),
                        cwd=directory,
                        instructions=_instructions(),
                        preferences=preferences,
                    )


if __name__ == "__main__":
    unittest.main()
