from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai_codex.types import TurnStatus

from flowmarshal.canonical import sha256_digest
from flowmarshal.planning.r31_models import (
    AvailablePlanningModel,
    CodexModelInventoryAdapter,
    CodexStructuredRoleRunner,
    ExecutionPolicyEvidence,
    ModelResolutionError,
    ModelRolePreference,
    PlanningModelRole,
    StructuredRoleRequest,
    StructuredRoleError,
    _token_count,
    resolve_model_role,
    strict_json_output_schema,
)
from flowmarshal.planning.r31_domain import ModelCallStatus


DEFAULT_MODELS = [
    {
        "id": "gpt-5.6-luna",
        "displayName": "Luna",
        "hidden": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
    },
    {
        "id": "gpt-5.6-terra",
        "displayName": "Terra",
        "hidden": False,
        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
    },
]


def _inventory_digest() -> str:
    return sha256_digest(
        (
            AvailablePlanningModel(
                model_id="gpt-5.6-luna",
                display_name="Luna",
                supported_efforts=("medium", "high"),
            ),
            AvailablePlanningModel(
                model_id="gpt-5.6-terra",
                display_name="Terra",
                supported_efforts=("high",),
            ),
        )
    )


class _FakeThread:
    id = "thread_test"

    def __init__(self) -> None:
        self.run_kwargs = None

    def run(self, payload, **kwargs):
        self.run_kwargs = kwargs
        return SimpleNamespace(
            id="turn_test",
            status=TurnStatus.completed,
            final_response='{"ok": true}',
            duration_ms=12,
            usage={"inputTokens": 10, "outputTokens": 2},
        )


class _FakeCodex:
    def __init__(self, models=None) -> None:
        self.closed = False
        self.thread = _FakeThread()
        self.thread_kwargs = None
        self.policy_checks = []
        self._models = DEFAULT_MODELS if models is None else models

    def models(self, *, include_hidden=False):
        return {"data": self._models}

    def verify_execution_policy(self, cwd):
        self.policy_checks.append(str(cwd))
        return ExecutionPolicyEvidence(
            config_digest=sha256_digest({"approval_policy": "never"}),
            profile_catalog_digest=sha256_digest([":danger-full-access"]),
        )

    def thread_start(self, **kwargs):
        self.thread_kwargs = kwargs
        return self.thread

    def close(self):
        self.closed = True


class PlannerR31ModelTests(unittest.TestCase):
    def test_token_count_reads_nested_codex_total_usage(self) -> None:
        self.assertEqual(
            42,
            _token_count(
                {
                    "last": {"inputTokens": 30, "outputTokens": 12},
                    "total": {"totalTokens": 42},
                }
            ),
        )

    def test_strict_output_schema_requires_every_object_property(self) -> None:
        schema = strict_json_output_schema(
            {
                "type": "object",
                "properties": {
                    "schema_version": {"type": "string", "default": "3.1"},
                    "enum_ref": {
                        "$ref": "#/$defs/Status",
                        "description": "Pydantic field 설명",
                        "title": "Status field",
                    },
                    "nested": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": [],
                    },
                    "freeform": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "specification": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["nested"],
            }
        )
        self.assertEqual(
            [
                "schema_version",
                "enum_ref",
                "nested",
                "freeform",
                "specification",
            ],
            schema["required"],
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("default", schema["properties"]["schema_version"])
        self.assertEqual(
            {"$ref": "#/$defs/Status"},
            schema["properties"]["enum_ref"],
        )
        nested = schema["properties"]["nested"]
        self.assertEqual(["value"], nested["required"])
        self.assertFalse(nested["additionalProperties"])
        freeform = schema["properties"]["freeform"]
        self.assertFalse(freeform["additionalProperties"])
        self.assertEqual({}, freeform["properties"])
        self.assertEqual([], freeform["required"])
        specification = schema["properties"]["specification"]
        self.assertFalse(specification["additionalProperties"])
        self.assertEqual(
            set(specification["properties"]),
            set(specification["required"]),
        )
        self.assertIn("command", specification["properties"])
        self.assertIn("assertions", specification["properties"])

    def test_inventory_and_role_resolution_do_not_silently_fallback(self) -> None:
        fake = _FakeCodex(
            [
                {
                    "id": "gpt-5.6-luna",
                    "displayName": "Luna",
                    "hidden": False,
                    "isDefault": False,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium"},
                        {"reasoningEffort": "high"},
                    ],
                },
                {
                    "id": "hidden-model",
                    "displayName": "Hidden",
                    "hidden": True,
                    "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                },
            ]
        )
        inventory = CodexModelInventoryAdapter(lambda: fake).list_models()
        self.assertEqual(("gpt-5.6-luna",), tuple(item.model_id for item in inventory))
        resolved = resolve_model_role(
            inventory,
            ModelRolePreference(
                role=PlanningModelRole.CANDIDATE_GENERATOR,
                preferred_model_ids=("gpt-5.6-luna",),
                preferred_effort="medium",
            ),
        )
        self.assertEqual("gpt-5.6-luna", resolved.model_id)
        with self.assertRaises(ModelResolutionError):
            resolve_model_role(
                inventory,
                ModelRolePreference(
                    role=PlanningModelRole.CRITICAL_REVIEWER,
                    preferred_model_ids=("gpt-5.6-sol",),
                    preferred_effort="xhigh",
                ),
            )
        self.assertTrue(fake.closed)

    def test_inventory_normalization_is_semantically_stable(self) -> None:
        reordered_models = [
            {
                "id": "gpt-5.6-terra",
                "displayName": "Terra",
                "hidden": False,
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            },
            {
                "id": "gpt-5.6-luna",
                "displayName": "Luna",
                "hidden": False,
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "high"},
                    {"reasoningEffort": "medium"},
                ],
            },
        ]
        baseline = CodexModelInventoryAdapter(lambda: _FakeCodex()).list_models()
        reordered = CodexModelInventoryAdapter(
            lambda: _FakeCodex(reordered_models)
        ).list_models()
        self.assertEqual(baseline, reordered)
        self.assertEqual(_inventory_digest(), sha256_digest(reordered))
        self.assertEqual(
            ("medium", "high"),
            next(
                item.supported_efforts
                for item in reordered
                if item.model_id == "gpt-5.6-luna"
            ),
        )

    def test_inventory_rejects_duplicate_model_ids_and_pagination(self) -> None:
        duplicate = [DEFAULT_MODELS[0], dict(DEFAULT_MODELS[0])]
        with self.assertRaisesRegex(ModelResolutionError, "중복"):
            CodexModelInventoryAdapter(lambda: _FakeCodex(duplicate)).list_models()

        class ResponseCodex(_FakeCodex):
            def __init__(self, response) -> None:
                super().__init__()
                self._response = response

            def models(self, *, include_hidden=False):
                return self._response

        responses = (
            {"data": DEFAULT_MODELS, "next_cursor": "page-2"},
            {"data": DEFAULT_MODELS, "nextCursor": "page-2"},
            SimpleNamespace(data=DEFAULT_MODELS, next_cursor="page-2"),
            SimpleNamespace(data=DEFAULT_MODELS, nextCursor="page-2"),
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaisesRegex(ModelResolutionError, "pagination"):
                    CodexModelInventoryAdapter(
                        lambda response=response: ResponseCodex(response)
                    ).list_models()

    def test_structured_runner_uses_full_access_never_ephemeral_thread_and_receipt(self) -> None:
        fake = _FakeCodex()
        with tempfile.TemporaryDirectory() as temp:
            result = CodexStructuredRoleRunner(lambda: fake).run(
                StructuredRoleRequest(
                    role=PlanningModelRole.CANDIDATE_GENERATOR,
                    instructions="계획 후보만 생성한다.",
                    payload={"request": "테스트"},
                    output_schema={"type": "object"},
                    model_id="gpt-5.6-luna",
                    reasoning_effort="medium",
                    inventory_digest=_inventory_digest(),
                    cwd=str(Path(temp)),
                )
            )
        self.assertEqual({"ok": True}, result.payload)
        self.assertTrue(fake.thread_kwargs["ephemeral"])
        self.assertEqual("full-access", fake.thread_kwargs["sandbox"].value)
        self.assertEqual("deny_all", fake.thread_kwargs["approval_mode"].value)
        self.assertEqual("full-access", fake.thread.run_kwargs["sandbox"].value)
        self.assertEqual(1, len(fake.policy_checks))
        self.assertEqual(ModelCallStatus.SUCCEEDED, result.receipt.status)
        self.assertEqual(_inventory_digest(), result.receipt.inventory_digest)
        self.assertEqual("thread_test", result.receipt.thread_id)
        self.assertEqual(("turn_test",), result.receipt.turn_ids)
        self.assertEqual(
            {"inputTokens": 10, "outputTokens": 2},
            {item.name: item.value for item in result.receipt.usage},
        )
        with self.assertRaises(TypeError):
            result.receipt.usage[0] = result.receipt.usage[0]  # type: ignore[index]
        self.assertTrue(fake.closed)

    def test_structured_runner_recovers_schema_once(self) -> None:
        class RecoveringThread(_FakeThread):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def run(self, payload, **kwargs):
                self.calls += 1
                result = super().run(payload, **kwargs)
                result.id = f"turn_{self.calls}"
                result.final_response = '{}' if self.calls == 1 else '{"ok": true}'
                return result

        fake = _FakeCodex()
        fake.thread = RecoveringThread()
        with tempfile.TemporaryDirectory() as temp:
            result = CodexStructuredRoleRunner(
                lambda: fake,
                call_id_factory=lambda: "model_call_schema_recovery",
            ).run(
                StructuredRoleRequest(
                    role=PlanningModelRole.CANDIDATE_GENERATOR,
                    instructions="계획 후보만 생성한다.",
                    payload={"request": "테스트"},
                    output_schema={
                        "type": "object",
                        "required": ["ok"],
                        "properties": {"ok": {"type": "boolean"}},
                    },
                    model_id="gpt-5.6-luna",
                    reasoning_effort="medium",
                    inventory_digest=_inventory_digest(),
                    cwd=str(Path(temp)),
                )
            )
        self.assertEqual(ModelCallStatus.SCHEMA_RECOVERED, result.receipt.status)
        self.assertEqual(1, result.receipt.schema_recovery_attempts)
        self.assertEqual(("turn_1", "turn_2"), result.receipt.turn_ids)

    def test_schema_recovery_rechecks_inventory_and_fails_closed_on_drift(self) -> None:
        class RecoveringThread(_FakeThread):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def run(self, payload, **kwargs):
                self.calls += 1
                result = super().run(payload, **kwargs)
                result.id = f"turn_{self.calls}"
                result.final_response = '{}'
                return result

        class DriftingCodex(_FakeCodex):
            def __init__(self) -> None:
                super().__init__()
                self.model_calls = 0

            def models(self, *, include_hidden=False):
                self.model_calls += 1
                models = DEFAULT_MODELS if self.model_calls == 1 else DEFAULT_MODELS[:1]
                return {"data": models}

        fake = DriftingCodex()
        fake.thread = RecoveringThread()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(lambda: fake).run(
                    StructuredRoleRequest(
                        role=PlanningModelRole.CANDIDATE_GENERATOR,
                        instructions="계획 후보만 생성한다.",
                        payload={"request": "테스트"},
                        output_schema={
                            "type": "object",
                            "required": ["ok"],
                            "properties": {"ok": {"type": "boolean"}},
                        },
                        model_id="gpt-5.6-luna",
                        reasoning_effort="medium",
                        inventory_digest=_inventory_digest(),
                        cwd=str(Path(temp)),
                    )
                )
        self.assertEqual(2, fake.model_calls)
        self.assertEqual(1, fake.thread.calls)
        self.assertEqual(
            ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
            raised.exception.receipt.status,
        )
        self.assertEqual(("turn_1",), raised.exception.receipt.turn_ids)

    def test_client_close_error_does_not_mask_result_or_original_error(self) -> None:
        class CloseFailingCodex(_FakeCodex):
            def close(self):
                self.closed = True
                raise RuntimeError("close failed")

        inventory_client = CloseFailingCodex()
        inventory = CodexModelInventoryAdapter(lambda: inventory_client).list_models()
        self.assertEqual(_inventory_digest(), sha256_digest(inventory))
        self.assertTrue(inventory_client.closed)

        success_client = CloseFailingCodex()
        with tempfile.TemporaryDirectory() as temp:
            result = CodexStructuredRoleRunner(lambda: success_client).run(
                StructuredRoleRequest(
                    role=PlanningModelRole.CANDIDATE_GENERATOR,
                    instructions="후보를 생성한다.",
                    payload={"request": "테스트"},
                    output_schema={"type": "object"},
                    model_id="gpt-5.6-luna",
                    reasoning_effort="medium",
                    inventory_digest=_inventory_digest(),
                    cwd=str(Path(temp)),
                )
            )
        self.assertEqual(ModelCallStatus.SUCCEEDED, result.receipt.status)
        self.assertTrue(success_client.closed)

        class TimeoutThread(_FakeThread):
            def run(self, payload, **kwargs):
                raise TimeoutError("fixture timeout")

        failing_client = CloseFailingCodex()
        failing_client.thread = TimeoutThread()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(lambda: failing_client).run(
                    StructuredRoleRequest(
                        role=PlanningModelRole.HARD_GATE_REVIEWER,
                        instructions="Hard Gate를 독립 검토한다.",
                        payload={"candidate": "fixture"},
                        output_schema={"type": "object"},
                        model_id="gpt-5.6-terra",
                        reasoning_effort="high",
                        inventory_digest=_inventory_digest(),
                        cwd=str(Path(temp)),
                    )
                )
        self.assertEqual(ModelCallStatus.TIMED_OUT, raised.exception.receipt.status)
        self.assertTrue(failing_client.closed)

    def test_resolution_rejects_unsupported_effort(self) -> None:
        inventory = (
            AvailablePlanningModel(
                model_id="gpt-5.6-luna",
                display_name="Luna",
                supported_efforts=("medium",),
            ),
        )
        with self.assertRaises(ModelResolutionError):
            resolve_model_role(
                inventory,
                ModelRolePreference(
                    role=PlanningModelRole.CANDIDATE_GENERATOR,
                    preferred_model_ids=("gpt-5.6-luna",),
                    preferred_effort="high",
                ),
            )

    def test_runtime_timeout_fails_closed_with_receipt(self) -> None:
        class TimeoutThread(_FakeThread):
            def run(self, payload, **kwargs):
                raise TimeoutError("fixture timeout")

        fake = _FakeCodex()
        fake.thread = TimeoutThread()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(lambda: fake).run(
                    StructuredRoleRequest(
                        role=PlanningModelRole.HARD_GATE_REVIEWER,
                        instructions="Hard Gate를 독립 검토한다.",
                        payload={"candidate": "fixture"},
                        output_schema={"type": "object"},
                        model_id="gpt-5.6-terra",
                        reasoning_effort="high",
                        inventory_digest=_inventory_digest(),
                        cwd=str(Path(temp)),
                    )
                )
        self.assertIsNotNone(raised.exception.receipt)
        self.assertEqual(ModelCallStatus.TIMED_OUT, raised.exception.receipt.status)
        self.assertTrue(fake.closed)

    def test_live_inventory_drift_is_required_model_unavailable(self) -> None:
        fake = _FakeCodex(models=DEFAULT_MODELS[:1])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(lambda: fake).run(
                    StructuredRoleRequest(
                        role=PlanningModelRole.CANDIDATE_GENERATOR,
                        instructions="후보를 생성한다.",
                        payload={"request": "테스트"},
                        output_schema={"type": "object"},
                        model_id="gpt-5.6-luna",
                        reasoning_effort="medium",
                        inventory_digest=_inventory_digest(),
                        cwd=str(Path(temp)),
                    )
                )
        self.assertEqual(
            ModelCallStatus.REQUIRED_MODEL_UNAVAILABLE,
            raised.exception.receipt.status,
        )
        self.assertIsNone(fake.thread_kwargs)

    def test_predispatch_failure_has_receipt_and_schema_is_snapshotted(self) -> None:
        fake = _FakeCodex()
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(StructuredRoleError) as raised:
                CodexStructuredRoleRunner(lambda: fake).run(
                    StructuredRoleRequest(
                        role=PlanningModelRole.CANDIDATE_GENERATOR,
                        instructions="후보를 생성한다.",
                        payload={"request": "테스트"},
                        output_schema={"type": "object"},
                        model_id="gpt-5.6-luna",
                        reasoning_effort="medium",
                        inventory_digest=_inventory_digest(),
                        cwd=str(Path(temp) / "missing-directory"),
                    )
                )
        self.assertEqual(ModelCallStatus.FAILED, raised.exception.receipt.status)
        self.assertIsNone(fake.thread_kwargs)

        class MutatingThread(_FakeThread):
            def run(self, payload, **kwargs):
                kwargs["output_schema"]["mutated_by_sdk"] = True
                return super().run(payload, **kwargs)

        fake = _FakeCodex()
        fake.thread = MutatingThread()
        schema = {"type": "object"}
        expected_digest = sha256_digest(schema)
        with tempfile.TemporaryDirectory() as temp:
            result = CodexStructuredRoleRunner(lambda: fake).run(
                StructuredRoleRequest(
                    role=PlanningModelRole.CANDIDATE_GENERATOR,
                    instructions="후보를 생성한다.",
                    payload={"request": "테스트"},
                    output_schema=schema,
                    model_id="gpt-5.6-luna",
                    reasoning_effort="medium",
                    inventory_digest=_inventory_digest(),
                    cwd=str(Path(temp)),
                )
            )
        self.assertEqual(expected_digest, result.receipt.output_schema_digest)
        self.assertEqual({"type": "object"}, schema)


if __name__ == "__main__":
    unittest.main()
