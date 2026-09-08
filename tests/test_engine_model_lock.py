from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.evaluation import (
    CheckpointContractError, EvaluationCellCheckpoint, ImmutableCheckpointStore,
)
from flowmarshal.engine.model_lock import (
    ALL_CAPABILITIES, LOCK_FORMAT, ModelCapability, ModelChoice, ModelInventory,
    OperationalBinding, RUNTIME_CAPABILITIES, RuntimeCapability, bind_models,
    parse_inventory_models, role_lock, verify_binding,
)
from flowmarshal.engine.models import EngineRoleConfiguration, RoleModelBinding
from flowmarshal.engine.qualification import _deterministic_contract, _model_lock
from flowmarshal.engine.roles import (
    CodexStructuredRoleRunner, RoleCallRequest, StructuredRoleError,
    make_role_request, verify_role_receipt,
)
from flowmarshal.engine.runtime import CodexAppServerRuntime, RuntimePolicyError
from tests.test_engine_roles import ImmediateRoleRuntime


ROOT = Path(__file__).resolve().parents[1]


def inventory():
    return ModelInventory(
        source="synthetic:model/list", executable_digest="sha256:" + "0" * 64,
        runtime_capabilities=RUNTIME_CAPABILITIES,
        models=tuple(ModelCapability(model=name, supported_efforts=efforts) for name, efforts in (
            ("worker", ("low", "high")), ("reviewer", ("high",)),
            ("backup", ("low", "high")), ("backup-two", ("high",)), ("unrelated", ("low",)),
        )),
    )


def unrelated_change(value):
    return value.model_copy(update={"models": tuple(reversed(value.models)) + (
        ModelCapability(model="new-unrelated", supported_efforts=("low",)),
    )})


def binding(value):
    return bind_models(value, (role_lock("executor", "worker", "high", (
        ModelChoice(model="backup", effort="high"), ModelChoice(model="backup-two", effort="high"),
    )),))


class ModelLockV2Tests(unittest.TestCase):
    def test_provider_inventory_and_adapter_capability_have_separate_provenance(self):
        original = inventory()
        provider_changed = original.model_copy(update={
            "models": (*original.models, ModelCapability(
                model="provider-added", supported_efforts=("low",)
            )),
        })
        adapter_changed = original.model_copy(update={
            "executable_digest": "sha256:" + "1" * 64,
        })
        self.assertNotEqual(
            original.provider_inventory_digest,
            provider_changed.provider_inventory_digest,
        )
        self.assertEqual(
            original.adapter_capability_digest,
            provider_changed.adapter_capability_digest,
        )
        self.assertEqual(
            original.provider_inventory_digest,
            adapter_changed.provider_inventory_digest,
        )
        self.assertNotEqual(
            original.adapter_capability_digest,
            adapter_changed.adapter_capability_digest,
        )

    def test_projection_ignores_unrelated_models_order_and_unused_efforts(self):
        original = inventory()
        expected = binding(original)
        changes = [
            unrelated_change(original),
            original.model_copy(update={"models": original.models[:-1]}),
            original.model_copy(update={"models": tuple(reversed(original.models))}),
            original.model_copy(update={"models": tuple(x.model_copy(update={
                "supported_efforts": tuple(reversed(x.supported_efforts))}) for x in original.models)}),
            original.model_copy(update={"models": (original.models[0].model_copy(update={
                "supported_efforts": ("high", "max")}), *original.models[1:])}),
            original.model_copy(update={"runtime_capabilities": tuple(reversed(original.runtime_capabilities)) + (
                RuntimeCapability(name="unused_feature", contract="new"),)}),
        ]
        for changed in changes:
            with self.subTest(inventory=changed.inventory_digest):
                self.assertNotEqual(original.inventory_digest, changed.inventory_digest)
                self.assertEqual(expected.lock_digest, binding(changed).lock_digest)
                observed = verify_binding(expected, changed)
                self.assertEqual(changed.inventory_digest, observed.inventory_digest)
                self.assertEqual(original.inventory_digest, expected.inventory_digest)

    def test_selected_combination_and_runtime_changes_are_blocked(self):
        original = inventory()
        expected = binding(original)
        changes = [original.model_copy(update={"models": original.models[1:]}),
                   original.model_copy(update={"models": (original.models[0].model_copy(update={
                       "supported_efforts": ("low",)}), *original.models[1:])}),
                   original.model_copy(update={"executable_digest": "sha256:" + "1" * 64}),
                   original.model_copy(update={"runtime_capabilities": original.runtime_capabilities[1:]}),
                   original.model_copy(update={"runtime_capabilities": (
                       original.runtime_capabilities[0].model_copy(update={"contract": "changed"}),
                       *original.runtime_capabilities[1:])})]
        for changed in changes:
            with self.subTest(change=changed.inventory_digest), self.assertRaises(ValueError):
                verify_binding(expected, changed)
        with self.assertRaisesRegex(ValueError, "ROLE_BINDING"):
            verify_binding(expected, original, role="executor", model="backup", effort="high")

    def test_fallback_envelope_order_effort_and_availability_are_locked(self):
        original = inventory()
        expected = binding(original)
        role = expected.lock.roles[0]
        choices = role.allowed_fallbacks
        envelopes = [(), choices[:1], tuple(reversed(choices)),
                     (*choices, choices[0].model_copy(update={"model": "other"})),
                     (choices[0].model_copy(update={"effort": "low"}), choices[1])]
        for envelope in envelopes:
            with self.subTest(envelope=envelope):
                changed = bind_models(original, (role.model_copy(update={"allowed_fallbacks": envelope}),))
                self.assertNotEqual(expected.lock_digest, changed.lock_digest)
        for changed in (
            original.model_copy(update={"models": tuple(x for x in original.models if x.model != "backup")}),
            original.model_copy(update={"models": tuple(x.model_copy(update={"supported_efforts": ("low",)})
                                                       if x.model == "backup" else x for x in original.models)}),
        ):
            with self.assertRaisesRegex(ValueError, "LOCK_CHANGED"):
                verify_binding(expected, changed)
            missing = bind_models(changed, expected.lock.roles)
            self.assertFalse(missing.lock.roles[0].allowed_fallbacks[0].supported)
            with self.assertRaisesRegex(ValueError, "LOCK_CHANGED"):
                verify_binding(missing, original)

    def test_all_configured_roles_are_in_qualification_projection(self):
        original = inventory()
        roles = EngineRoleConfiguration(**{
            name: RoleModelBinding(model="reviewer" if name == "validator" else "worker", effort="high")
            for name in EngineRoleConfiguration.model_fields
        })
        self.assertEqual(_model_lock(original, roles), _model_lock(unrelated_change(original), roles))
        changed = roles.model_copy(update={"normalizer": RoleModelBinding(model="worker", effort="low")})
        self.assertNotEqual(_model_lock(original, roles), _model_lock(original, changed))
        changed = roles.model_copy(update={"normalizer": roles.normalizer.model_copy(update={
            "allowed_fallbacks": (ModelChoice(model="backup", effort="high"),)})})
        self.assertNotEqual(_model_lock(original, roles), _model_lock(original, changed))

    def test_full_raw_inventory_is_strict_before_projection(self):
        raw = {"data": [{"id": "worker", "supported_reasoning_efforts": [{"reasoning_effort": "high"}]},
                        {"id": "unused", "hidden": True,
                         "supported_reasoning_efforts": [{"reasoning_effort": "low"}]}], "next_cursor": None}
        malformed = [None, {}, {"data": []}, {"data": None}, {"data": [None]},
                     dict(raw, data=raw["data"] + [raw["data"][1]])]
        alias = deepcopy(raw)
        alias["data"][1]["supportedReasoningEfforts"] = None
        malformed.append(alias)
        for key, values in (("id", (None, "", " ", " worker", "bad id", 123)),
                            ("supported_reasoning_efforts", (None, [], [None], ["high"],
                             [{"reasoning_effort": value} for value in ("low", "low")]))):
            for value in values:
                changed = deepcopy(raw)
                changed["data"][1][key] = value
                malformed.append(changed)
        for value in (None, "", " ", " high", "invalid", 1):
            changed = deepcopy(raw)
            changed["data"][1]["supported_reasoning_efforts"] = [{"reasoning_effort": value}]
            malformed.append(changed)
        for changed in malformed:
            with self.subTest(raw=changed), self.assertRaises(ValueError):
                parse_inventory_models(changed)
        parsed = ModelInventory(source="raw", models=parse_inventory_models(raw), raw_response=raw,
                                executable_digest="sha256:" + "0" * 64, runtime_capabilities=RUNTIME_CAPABILITIES)
        self.assertEqual(raw, parsed.raw_response)
        self.assertEqual(2, len(parsed.models))
        forged = parsed.model_copy(update={"models": parsed.models + (parsed.models[-1],)})
        with self.assertRaises(ValueError):
            bind_models(forged, (role_lock("executor", "worker", "high"),))
        for changed in malformed[1:]:
            runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
            runtime.executable_digest = parsed.executable_digest
            runtime._raw = lambda method, params: changed
            with self.assertRaises(RuntimePolicyError):
                runtime.list_models()

    def test_raw_app_server_camel_fields_preserve_audit_order(self):
        raw = {"data": [{"id": "worker", "supportedReasoningEfforts": [
            {"reasoningEffort": "high"}, {"reasoningEffort": "low"}]}], "nextCursor": None}
        runtime = CodexAppServerRuntime.__new__(CodexAppServerRuntime)
        runtime.executable_digest = "sha256:" + "0" * 64
        runtime._raw = lambda method, params: raw
        original = runtime.list_models()
        expected = bind_models(original, (role_lock("executor", "worker", "high"),))
        self.assertEqual(raw, original.raw_response)
        self.assertEqual(("high", "low"), original.models[0].supported_efforts)
        raw = deepcopy(raw)
        raw["data"][0]["supportedReasoningEfforts"].reverse()
        changed = runtime.list_models()
        self.assertNotEqual(original.inventory_digest, changed.inventory_digest)
        self.assertEqual(expected.lock_digest, verify_binding(expected, changed).lock_digest)

    def test_forged_observation_digest_and_v1_checkpoint_are_rejected(self):
        expected = binding(inventory())
        for key in ("inventory_digest", "lock_digest"):
            value = expected.model_dump(mode="json")
            value[key] = "sha256:" + "f" * 64
            with self.assertRaises(ValidationError):
                OperationalBinding.model_validate(value)
        value = expected.model_dump(mode="json")
        value["lock"]["format"] = "flowmarshal-model-lock-v1"
        with self.assertRaises(ValidationError):
            OperationalBinding.model_validate(value)
        contract = _deterministic_contract(ROOT)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = contract.model_dump(mode="json")
            old.pop("model_lock_format")
            path = root / "evaluation-contract.json"
            path.write_bytes(json.dumps(old).encode())
            before = path.read_bytes()
            with self.assertRaisesRegex(CheckpointContractError, "VERSION_UNSUPPORTED"):
                ImmutableCheckpointStore(root, contract).initialize()
            self.assertEqual(before, path.read_bytes())
        with self.assertRaises(ValidationError):
            EvaluationCellCheckpoint.model_validate({"contract_digest": contract.contract_digest})
        for module in ("r_s06_09", "r_s06_10"):
            with patch(f"scripts.diagnostics.{module}.read", return_value={"lock_digest": "v1"}):
                imported = __import__(f"scripts.diagnostics.{module}", fromlist=["verify_lock"])
                with self.assertRaisesRegex(RuntimeError, "VERSION_UNSUPPORTED"):
                    imported.verify_lock(Path("unused"))

    def test_role_call_accepts_audit_drift_and_rejects_forged_request_receipt(self):
        runtime = ImmediateRoleRuntime(["{}"])
        original = runtime.inventory
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(inventory=original, role="reviewer", model="available", effort="low",
                                        inventory_digest=original.inventory_digest, instructions="합성 검사",
                                        payload={}, output_schema={"type": "object"}, cwd=temp)
            runtime.inventory = unrelated_change(original)
            result = CodexStructuredRoleRunner(runtime, poll_interval_seconds=0).run(request)
            verify_role_receipt(request, result)
            self.assertEqual(runtime.inventory.inventory_digest, result.receipt.observed_binding.inventory_digest)
            self.assertEqual(original.inventory_digest, result.receipt.inventory_digest)
            self.assertEqual(request.model, result.receipt.requested_model)
            self.assertEqual(request.effort, result.receipt.requested_effort)
            self.assertEqual(
                result.receipt.observed_binding.inventory.provider_inventory_digest,
                result.receipt.provider_inventory_digest,
            )
            self.assertEqual(
                result.receipt.observed_binding.inventory.adapter_capability_digest,
                result.receipt.adapter_capability_digest,
            )
            projection = result.receipt.model_dump(mode="json")
            self.assertEqual("2.0", projection["binding_provenance_version"])
            self.assertEqual(request.model, projection["requested_model"])
            self.assertEqual(request.effort, projection["requested_effort"])
            self.assertIsNone(projection["observed_model"])
            self.assertIsNone(projection["observed_effort"])
            self.assertEqual("role_request", projection["binding_provenance"]["requested"])
            forged = request.model_copy(update={"inventory_digest": "sha256:" + "f" * 64})
            with patch.object(runtime, "create_thread") as create, self.assertRaises(StructuredRoleError):
                CodexStructuredRoleRunner(runtime).run(forged)
            create.assert_not_called()
            for key in ("inventory_digest", "input_digest", "output_digest"):
                receipt = result.receipt.model_copy(update={key: "sha256:" + "f" * 64})
                with self.assertRaises(StructuredRoleError):
                    verify_role_receipt(request, result.model_copy(update={"receipt": receipt}))
            for key in ("provider_inventory_digest", "adapter_capability_digest"):
                receipt = result.receipt.model_copy(update={key: "sha256:" + "f" * 64})
                with self.assertRaises(StructuredRoleError):
                    verify_role_receipt(request, result.model_copy(update={"receipt": receipt}))
            forged_binding = result.receipt.observed_binding.model_copy(update={"lock_digest": "sha256:" + "f" * 64})
            with self.assertRaises((ValueError, StructuredRoleError)):
                verify_role_receipt(request, result.model_copy(update={"receipt": result.receipt.model_copy(
                    update={"observed_binding": forged_binding})}))

    def test_role_preflight_blocks_before_any_effect_without_recovery(self):
        runtime = ImmediateRoleRuntime(["{}", "{}"])
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(inventory=runtime.inventory, role="reviewer", model="available", effort="low",
                                        inventory_digest=runtime.inventory.inventory_digest, instructions="합성 검사",
                                        payload={}, output_schema={"type": "object"}, cwd=temp)
            original = runtime.inventory
            changes = [original.model_copy(update={"models": (ModelCapability(model="other", supported_efforts=("low",)),)}),
                       original.model_copy(update={"models": (ModelCapability(model="available", supported_efforts=("high",)),)}),
                       original.model_copy(update={"executable_digest": "sha256:" + "f" * 64})]
            for changed in changes:
                runtime.inventory = changed
                runner = CodexStructuredRoleRunner(runtime)
                with patch.object(runtime, "create_thread") as create, patch.object(runtime, "start_turn") as start:
                    with self.assertRaises(StructuredRoleError):
                        runner.run(request)
                    create.assert_not_called()
                    start.assert_not_called()
                    self.assertEqual([], runner.receipts)
            runtime.inventory = original
            with self.assertRaisesRegex(StructuredRoleError, "VERSION_UNSUPPORTED"):
                CodexStructuredRoleRunner(runtime).run(request.model_copy(update={"operational_binding": None}))

    def test_other_selected_role_and_fallback_loss_stop_before_first_effect(self):
        runtime = ImmediateRoleRuntime(["{}"])
        runtime.inventory = original = inventory()
        whole = bind_models(original, (*binding(original).lock.roles, role_lock("validator", "reviewer", "high")))
        with tempfile.TemporaryDirectory() as temp:
            request = make_role_request(inventory=original, role="executor", model="worker", effort="high",
                                        inventory_digest=original.inventory_digest, instructions="합성 검사",
                                        payload={}, output_schema={"type": "object"}, cwd=temp,
                                        allowed_fallbacks=(ModelChoice(model="backup", effort="high"),))
            for missing in ("reviewer", "backup"):
                runtime.inventory = original.model_copy(update={"models": tuple(
                    x for x in original.models if x.model != missing)})
                runner = CodexStructuredRoleRunner(runtime, operational_binding=whole)
                with patch.object(runtime, "create_thread") as create, patch.object(runtime, "start_turn") as start:
                    with self.assertRaises(StructuredRoleError):
                        runner.run(request)
                    create.assert_not_called()
                    start.assert_not_called()
                    self.assertEqual(["{}"], runtime.outputs)
            narrowed = bind_models(original, request.operational_binding.lock.roles,
                                   required_capabilities=("local_execution",))
            with self.assertRaisesRegex(ValueError, "REQUIRED_CAPABILITY"):
                RoleCallRequest.model_validate(request.model_dump(mode="python") | {"operational_binding": narrowed})


if __name__ == "__main__":
    unittest.main()
