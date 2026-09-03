from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from flowmarshal.planning.r31_domain import PlanningRole
from flowmarshal.planning.r31_live_smoke import (
    LiveRoleConfiguration,
    _live_request_spec,
    load_role_configuration,
    load_role_instructions,
)
from flowmarshal.planning.r31_models import ModelRolePreference


def _preferences() -> tuple[ModelRolePreference, ...]:
    roles = (
        PlanningRole.PURPOSE_RESOLVER,
        PlanningRole.INTENT_REVIEWER,
        PlanningRole.CANDIDATE_GENERATOR,
        PlanningRole.HARD_GATE_REVIEWER,
        PlanningRole.CRITICAL_REVIEWER,
        PlanningRole.SCORER_SELECTOR,
    )
    return tuple(
        ModelRolePreference(
            role=role,
            preferred_model_ids=(f"model-{index}",),
            preferred_effort="medium",
        )
        for index, role in enumerate(roles, start=1)
    )


class PlannerR31LiveSmokeTests(unittest.TestCase):
    def test_live_request_registers_real_project_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = _live_request_spec(Path(directory))
        self.assertEqual(
            {"project-agents", "r31-project-inventory"},
            {item.source_id for item in request.context_sources},
        )
        self.assertTrue(
            all(item.required_for_all_work_items for item in request.context_sources)
        )
        self.assertTrue(request.project_root.endswith("flowmarshal"))

    def test_role_configuration_requires_exact_runtime_roles(self) -> None:
        configuration = LiveRoleConfiguration(
            configuration_id="test-config",
            preferences=_preferences(),
        )
        self.assertEqual(6, len(configuration.preferences))
        with self.assertRaises(ValidationError):
            LiveRoleConfiguration(
                configuration_id="missing-role",
                preferences=_preferences()[:-1],
            )

    def test_role_configuration_loads_without_product_model_defaults(self) -> None:
        configuration = LiveRoleConfiguration(
            configuration_id="test-config",
            preferences=_preferences(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roles.json"
            path.write_text(
                json.dumps(configuration.model_dump(mode="json")),
                encoding="utf-8",
            )
            loaded = load_role_configuration(path)
        self.assertEqual(configuration, loaded)

    def test_skill_references_are_loaded_by_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "references").mkdir()
            (root / "SKILL.md").write_text("공통", encoding="utf-8")
            for name in (
                "intent-contract.md",
                "plan-contract.md",
                "quality-gates.md",
                "candidate-selection.md",
                "session-strategy.md",
            ):
                (root / "references" / name).write_text(name, encoding="utf-8")
            instructions = load_role_instructions(root)
        self.assertIn("intent-contract.md", instructions.purpose_resolver)
        self.assertNotIn("plan-contract.md", instructions.purpose_resolver)
        self.assertIn("quality-gates.md", instructions.candidate_generator)
        self.assertIn("session-strategy.md", instructions.candidate_generator)
        self.assertIn("quality-gates.md", instructions.critical_reviewer)
        self.assertIn("candidate-selection.md", instructions.scorer_selector)


if __name__ == "__main__":
    unittest.main()
