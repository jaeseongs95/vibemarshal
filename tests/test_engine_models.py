from __future__ import annotations

import inspect
import unittest

import flowmarshal.engine.models as models_module
from flowmarshal.engine.domain import (
    ModelAssignmentContract,
    ModelFallback,
    RoleAssignmentPolicy,
)
from flowmarshal.engine.models import (
    AssignmentResolutionError,
    AssignmentResolver,
    ModelCapability,
    ModelInventory,
)
from flowmarshal.engine.roles import _usage


class EngineModelAssignmentTests(unittest.TestCase):
    def test_allowed_fallback_is_explicit_and_recorded(self) -> None:
        inventory = ModelInventory(
            source="model/list",
            models=(ModelCapability(model="available", supported_efforts=("high",)),),
        )
        policy = RoleAssignmentPolicy(
            role="executor",
            preferred_model="preferred",
            preferred_effort="medium",
            allowed_fallbacks=(ModelFallback(model="available", effort="high"),),
        )
        resolved = AssignmentResolver().resolve_policy(policy, inventory)
        self.assertEqual("available", resolved.model)
        self.assertTrue(resolved.fallback_used)
        self.assertEqual(inventory.inventory_digest, resolved.inventory_digest)

    def test_unsupported_model_is_not_silently_replaced(self) -> None:
        inventory = ModelInventory(
            source="model/list",
            models=(ModelCapability(model="available", supported_efforts=("low",)),),
        )
        policy = RoleAssignmentPolicy(
            role="executor",
            preferred_model="missing",
            preferred_effort="high",
        )
        with self.assertRaises(AssignmentResolutionError):
            AssignmentResolver().resolve_policy(policy, inventory)

    def test_independent_validator_cannot_resolve_to_same_binding(self) -> None:
        inventory = ModelInventory(
            source="model/list",
            models=(ModelCapability(model="only", supported_efforts=("high",)),),
        )
        policy = RoleAssignmentPolicy(
            role="shared",
            preferred_model="only",
            preferred_effort="high",
        )
        contract = ModelAssignmentContract(
            executor=policy,
            validator=policy,
            independence_required=True,
        )
        with self.assertRaisesRegex(AssignmentResolutionError, "독립"):
            AssignmentResolver().resolve_contract(contract, inventory)

    def test_product_assignment_code_does_not_hardcode_current_model_names(self) -> None:
        source = inspect.getsource(models_module)
        self.assertNotIn("gpt-", source)

    def test_nested_app_server_usage_is_measured_from_total(self) -> None:
        measured = _usage(
            {
                "usage": {
                    "last": {"inputTokens": 2, "outputTokens": 1},
                    "total": {
                        "inputTokens": 100,
                        "cachedInputTokens": 60,
                        "outputTokens": 20,
                        "reasoningOutputTokens": 7,
                    },
                }
            }
        )
        self.assertEqual((100, 60, 20, 7, True), measured)
        self.assertEqual((0, 0, 0, 0, False), _usage({"usage": {"total": {}}}))


if __name__ == "__main__":
    unittest.main()
