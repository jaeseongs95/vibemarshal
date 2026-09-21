from __future__ import annotations

import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import BudgetManager
from flowmarshal.engine.e2e_qualification import (
    RecordedRuntime,
    _absolute_timeout_no_duplicate,
    _assert_no_transient_plugin_identity_change,
    _cancel_active_job,
    _checkpoint_model_observation,
    _contract,
    _copy_fixture,
    _e2e_failure_disposition,
    _forced_termination_no_duplicate,
    _guard_e2e_partial_resume,
    _observe_frozen_plugin_identity,
    _prepare,
    _prepare_from_raw_request,
    _preserve_inventory_observation,
    _responsibility_outcome,
    _unknown_receipt,
    run_project_e2e,
)
from flowmarshal.engine.evaluation import EvaluationRunStatus, ImmutableCheckpointStore
from flowmarshal.engine.evaluation_budget import (
    evaluation_cell_provider_calls,
    load_evaluation_policies,
)
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.operations import CoreOperations
from flowmarshal.engine.qualification import (
    QualificationRunError,
    default_role_configuration,
)
from flowmarshal.engine.qualification_manifest import (
    EvidenceProvenance,
    QualificationSuiteManifest,
)
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeObservation
from tests.fixtures.engine.governance.allow import ALLOW_ALL


ROOT = Path(__file__).resolve().parents[1]


def _frozen_governance_identity(root: Path):
    from flowmarshal.engine.governance_conformance import CHECK_SET_DIGEST

    summary = {
        "manifest_sha256": sha256_digest({"manifest": "frozen"}),
        "closure_tree_digest": sha256_digest({"closure": "frozen"}),
        "entrypoint_table_digest": sha256_digest({"entrypoints": "frozen"}),
        "node_version": "v22.13.0",
    }
    fields = {
        "closure_tree_digest": summary["closure_tree_digest"],
        "entrypoint_table_digest": summary["entrypoint_table_digest"],
        "check_set_digest": CHECK_SET_DIGEST,
        "conformance_result_digest": sha256_digest({"conformance": "frozen"}),
    }
    frozen = SimpleNamespace(
        **fields,
        manifest_sha256=summary["manifest_sha256"],
        node_version=summary["node_version"],
        plugin_version_label="2.1.2",
        server_info={"name": "governance", "version": "2.1.2"},
        installation_root=str(root.resolve()),
        e2e_identity_digest=sha256_digest(fields),
    )
    return frozen, summary


class _IdentityOnlyGovernance:
    def __init__(self, root: Path, summary: dict, labels=()) -> None:
        self.plugin_root = root
        self.summary = summary
        self.labels = labels
        self.check_conformance_calls = 0

    def inspect_identity(self):
        return {"summary": self.summary, "labels": self.labels}

    def check_conformance(self):
        self.check_conformance_calls += 1
        raise AssertionError("E2E rebind는 conformance를 재실행하면 안 됩니다.")


def _inventory() -> ModelInventory:
    roles = default_role_configuration(ROOT)
    grouped: dict[str, set[str]] = defaultdict(set)
    for role in type(roles).model_fields:
        binding = roles.binding_for(role)
        grouped[binding.model].add(binding.effort)
    return ModelInventory(
        executable_digest="sha256:" + "0" * 64,
        runtime_capabilities=RUNTIME_CAPABILITIES,
        source="e2e-qualification-test-model-list",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
            for model, efforts in sorted(grouped.items())
        ),
    )


class _RequestEchoFakeRuntime(FakeCodexRuntime):
    def start_turn(self, **arguments):
        receipt = super().start_turn(**arguments)
        return receipt.model_copy(
            update={
                "payload": receipt.payload
                | {
                    "model": arguments["model"],
                    "effort": arguments["effort"],
                }
            }
        )


class _PartialCreateObservationFakeRuntime(FakeCodexRuntime):
    def create_thread(self, **arguments):
        receipt = super().create_thread(**arguments)
        return receipt.model_copy(
            update={"payload": receipt.payload | {"model": arguments["model"]}}
        )


class _CompletionCallbackFakeRuntime(_RequestEchoFakeRuntime):
    def __init__(self, inventory: ModelInventory) -> None:
        super().__init__(inventory)
        self.completion_observer = None
        self.completion_thread_id: str | None = None
        self.completion_turn_id: str | None = None
        self.last_turn_receipt = None

    def start_turn(self, **arguments):
        receipt = super().start_turn(**arguments)
        self.last_turn_receipt = receipt
        return receipt

    def register_completion_observer(self, *, thread_id, turn_id, observer):
        self.completion_thread_id = thread_id
        self.completion_turn_id = turn_id
        self.completion_observer = observer

    def emit_completion(self) -> RuntimeObservation:
        assert self.last_turn_receipt is not None
        assert self.completion_observer is not None
        assert self.completion_thread_id is not None
        assert self.completion_turn_id is not None
        self.complete(self.completion_thread_id, response="완료 callback")
        observation = RuntimeObservation(
            thread_id=self.completion_thread_id,
            turn_id=self.completion_turn_id,
            active=False,
            terminal_status="completed",
            final_response="완료 callback",
            payload={
                "thread_id": self.completion_thread_id,
                "turn_id": self.completion_turn_id,
                "prompt_digest": self.last_turn_receipt.payload["prompt_digest"],
                "usage": {
                    "inputTokens": 11,
                    "cachedInputTokens": 2,
                    "outputTokens": 7,
                    "reasoningOutputTokens": 3,
                    "totalTokens": 18,
                },
                "usage_scope": "turn",
                "usage_source": "sdk.turn_result",
                "duration_ms": 5,
            },
        )
        self.completion_observer(observation)
        return observation


class _StoredNonActiveFakeRuntime(FakeCodexRuntime):
    """Claude transcript처럼 stored 조회는 active를 표현하지 않는 fake."""

    def read_stored(self, **arguments):
        observation = super().read_stored(**arguments)
        if observation.terminal_status is not None:
            return observation
        return observation.model_copy(
            update={
                "active": False,
                "payload": observation.payload | {"turn_status": "terminal_unobserved"},
            }
        )


class EngineE2EQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = default_role_configuration(ROOT)
        self.inventory = _inventory()

    def test_stale_responsibility_uses_allowed_live_fault_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            run_root = Path(raw)
            cell_root = run_root / "work" / "stale-after-materialization"
            cell_root.mkdir(parents=True)
            (cell_root / "qualification-observation.json").write_text(
                "{}", encoding="utf-8"
            )
            digest = sha256_digest({"binding": "test"})

            outcome = _responsibility_outcome(
                scenario="stale-after-materialization",
                prepared=SimpleNamespace(pipeline_stages=("prepare",)),
                cell={"passed": True},
                contract=SimpleNamespace(contract_digest=digest),
                cell_root=cell_root,
                run_root=run_root,
                fixture_digest=digest,
                freeze_bundle_digest=digest,
                governance_plugin_identity_digest=digest,
                candidate_wheel_digest=digest,
                candidate_wheel_binding_digest=digest,
                candidate_distribution_name="flowmarshal-engine",
                candidate_distribution_version="1.0.0",
            )

            self.assertEqual(
                (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
                outcome.provenance,
            )
            requirement = next(
                item
                for item in QualificationSuiteManifest.load(
                    ROOT / "config" / "qualification-suite.json"
                ).e2e_responsibilities
                if item.responsibility_id == "E2E-11"
            )
            self.assertLessEqual(
                set(outcome.provenance), set(requirement.allowed_provenance)
            )
            self.assertLessEqual(
                set(requirement.required_provenance), set(outcome.provenance)
            )

    def test_frozen_governance_identity_rebind_uses_only_four_exact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            frozen_root = Path(raw) / "frozen-plugin"
            current_root = Path(raw) / "current-plugin"
            frozen_root.mkdir()
            current_root.mkdir()
            frozen, summary = _frozen_governance_identity(frozen_root)
            governance = _IdentityOnlyGovernance(
                current_root,
                summary
                | {
                    "manifest_sha256": sha256_digest({"manifest": "audit-drift"}),
                    "node_version": "v22.13.9",
                },
                labels=(
                    {
                        "source": "plugin_manifest_file",
                        "plugin": {"id": "governance", "version": "9.9.9"},
                    },
                    {
                        "source": "mcp_server_info",
                        "serverInfo": {"name": "renamed", "version": "9.9.9"},
                    },
                ),
            )

            observed = _observe_frozen_plugin_identity(governance, frozen)

            self.assertEqual(
                frozen.e2e_identity_digest,
                observed["governance_plugin_identity_digest"],
            )
            self.assertTrue(all(observed["audit_only_changes"].values()))
            self.assertEqual(0, governance.check_conformance_calls)

            exact_drifts = {
                "closure_tree_digest": (
                    summary
                    | {"closure_tree_digest": sha256_digest({"drift": "closure"})},
                    frozen,
                ),
                "entrypoint_table_digest": (
                    summary
                    | {
                        "entrypoint_table_digest": sha256_digest(
                            {"drift": "entrypoints"}
                        )
                    },
                    frozen,
                ),
                "conformance_result_digest": (
                    summary,
                    SimpleNamespace(
                        **(
                            vars(frozen)
                            | {
                                "conformance_result_digest": sha256_digest(
                                    {"drift": "conformance"}
                                )
                            }
                        )
                    ),
                ),
            }
            for field, (drifted_summary, drifted_frozen) in exact_drifts.items():
                with self.subTest(field=field), self.assertRaisesRegex(
                    QualificationRunError,
                    "E2E_GOVERNANCE_PLUGIN_IDENTITY_MISMATCH",
                ):
                    _observe_frozen_plugin_identity(
                        _IdentityOnlyGovernance(frozen_root, drifted_summary),
                        drifted_frozen,
                    )
            with patch(
                "flowmarshal.engine.governance_conformance.CHECK_SET_DIGEST",
                sha256_digest({"drift": "checks"}),
            ), self.assertRaisesRegex(
                QualificationRunError,
                "E2E_GOVERNANCE_PLUGIN_IDENTITY_MISMATCH",
            ):
                _observe_frozen_plugin_identity(
                    _IdentityOnlyGovernance(frozen_root, summary),
                    frozen,
                )

    def test_frozen_governance_identity_wraps_contract_and_environment_errors(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frozen, _ = _frozen_governance_identity(root)

            for error in (ValueError("contract"), OSError("environment")):
                governance = _IdentityOnlyGovernance(root, {})
                governance.inspect_identity = lambda error=error: (_ for _ in ()).throw(
                    error
                )
                with self.subTest(error=type(error).__name__), self.assertRaisesRegex(
                    QualificationRunError,
                    "E2E_GOVERNANCE_PREFLIGHT_FAILED",
                ):
                    _observe_frozen_plugin_identity(governance, frozen)

    def test_transient_governance_identity_change_in_ledger_forbids_cell_pass(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            for phase in ("before_dispatch", "before_completion"):
                with self.subTest(phase=phase):
                    cell_root = Path(raw) / phase
                    cell_root.mkdir()
                    workspace, _ = _copy_fixture(ROOT, cell_root)
                    prepared = _prepare(
                        workspace=workspace,
                        state_root=cell_root / "state",
                        inventory=self.inventory,
                        roles=self.roles,
                        evaluation_policies=policies,
                        evaluation_contract_digest=sha256_digest(
                            {"contract": phase}
                        ),
                        fixture_digest=sha256_digest({"fixture": phase}),
                    )
                    _assert_no_transient_plugin_identity_change(prepared)
                    CoreOperations(prepared.service).invoke(
                        project_id=prepared.project_id,
                        kind="governance_gate",
                        request={"step": f"plugin_identity_changed:{phase}"},
                        execute=lambda: {"changed": True},
                    )
                    with self.assertRaisesRegex(
                        QualificationRunError,
                        "E2E_GOVERNANCE_PLUGIN_IDENTITY_CHANGED_DURING_CELL",
                    ):
                        _assert_no_transient_plugin_identity_change(prepared)

    def test_release_project_e2e_requires_absolute_candidate_wheel_before_provider(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with patch(
            "flowmarshal.engine.e2e_qualification.CodexAppServerRuntime"
        ) as runtime:
            with self.assertRaisesRegex(QualificationRunError, "CANDIDATE_WHEEL_REQUIRED"):
                run_project_e2e(root=ROOT, evaluation_policies=policies)
            with self.assertRaisesRegex(
                QualificationRunError, "CANDIDATE_WHEEL_ABSOLUTE_PATH_REQUIRED"
            ):
                run_project_e2e(
                    root=ROOT,
                    evaluation_policies=policies,
                    candidate_wheel=Path("candidate.whl"),
                )
        runtime.assert_not_called()

    def test_candidate_wheel_binding_changes_project_e2e_contract(self) -> None:
        fixture_root = ROOT / "tests" / "fixtures" / "engine" / "project-e2e"
        source_digest = sha256_digest(
            {
                path.relative_to(fixture_root).as_posix(): path.read_bytes().hex()
                for path in sorted(fixture_root.rglob("*"))
                if path.is_file() and "__pycache__" not in path.parts
            }
        )
        first = _contract(
            ROOT,
            self.inventory,
            self.roles,
            source_digest,
            candidate_wheel_binding_digest="sha256:" + "a" * 64,
        )
        second = _contract(
            ROOT,
            self.inventory,
            self.roles,
            source_digest,
            candidate_wheel_binding_digest="sha256:" + "b" * 64,
        )
        self.assertNotEqual(first.contract_digest, second.contract_digest)

    def test_runtime_journal_separates_request_binding_from_observed_response(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = RecordedRuntime(
                _RequestEchoFakeRuntime(self.inventory), journal=root / "runtime.json"
            )
            runtime.list_models()
            created = runtime.create_thread(
                cwd=root,
                title="E2E",
                model=self.roles.executor.model,
                developer_instructions="고정 지침",
                ephemeral=False,
            )
            runtime.start_turn(
                thread_id=created.binding.thread_id,
                cwd=root,
                prompt="고정 prompt",
                model=self.roles.executor.model,
                effort=self.roles.executor.effort,
                output_schema={"type": "object"},
            )

            event = runtime.events[-1]
            request = event["request_binding"]
            self.assertEqual(self.roles.executor.model, request["model"])
            self.assertEqual(self.roles.executor.effort, request["effort"])
            self.assertEqual(self.inventory.inventory_digest, request["inventory_digest"])
            self.assertEqual(sha256_digest("고정 prompt"), request["prompt_digest"])
            self.assertEqual(sha256_digest(request), event["request_binding_digest"])
            self.assertEqual(sha256_digest(event["receipt"]), event["receipt_digest"])
            event_without_digest = {
                key: value for key, value in event.items() if key != "event_digest"
            }
            self.assertEqual(sha256_digest(event_without_digest), event["event_digest"])
            self.assertEqual(
                self.roles.executor.model, event["receipt"]["payload"]["model"]
            )
            self.assertEqual(
                self.roles.executor.effort, event["receipt"]["payload"]["effort"]
            )
            self.assertIsNone(event["observed_model"])
            self.assertIsNone(event["observed_effort"])
            self.assertIsNone(event["model_observation_source"])
            self.assertEqual(
                "START_TURN_RECEIPT_MODEL_EFFORT_ARE_REQUEST_ECHO",
                event["model_observation_reason"],
            )

            observation = _checkpoint_model_observation(runtime.events)
            self.assertIsNone(observation["actual_model"])
            self.assertIsNone(observation["actual_effort"])
            self.assertEqual(
                "PROVIDER_RAW_MODEL_EFFORT_NOT_OBSERVED",
                observation["observation_reason"],
            )
            self.assertEqual(
                [{"model": self.roles.executor.model, "effort": self.roles.executor.effort}],
                observation["requested_pairs"],
            )
            self.assertEqual(
                ["START_TURN_RECEIPT_MODEL_EFFORT_ARE_REQUEST_ECHO"],
                observation["provider_observation_reasons"],
            )
            self.assertEqual([self.inventory.inventory_digest], observation["inventory_digests"])
            restored = RecordedRuntime(
                _RequestEchoFakeRuntime(self.inventory), journal=root / "runtime.json"
            )
            self.assertEqual(self.inventory.inventory_digest, restored.inventory.inventory_digest)

    def test_runtime_journal_discards_partial_raw_model_observation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = RecordedRuntime(
                _PartialCreateObservationFakeRuntime(self.inventory),
                journal=root / "runtime.json",
            )
            runtime.create_thread(
                cwd=root,
                title="E2E",
                model=self.roles.executor.model,
                developer_instructions="고정 지침",
                ephemeral=False,
            )
            event = runtime.events[-1]
            self.assertIsNone(event["observed_model"])
            self.assertIsNone(event["observed_effort"])
            self.assertIsNone(event["model_observation_source"])
            self.assertEqual(
                "PROVIDER_RAW_RESPONSE_MODEL_OR_EFFORT_NOT_REPORTED",
                event["model_observation_reason"],
            )

    def test_checkpoint_never_substitutes_config_for_missing_or_mixed_observation(self) -> None:
        self.assertEqual(
            {
                "actual_model": None,
                "actual_effort": None,
                "observation_reason": "NO_MODEL_TURN_OBSERVED",
            },
            {
                key: _checkpoint_model_observation([])[key]
                for key in ("actual_model", "actual_effort", "observation_reason")
            },
        )
        missing = _checkpoint_model_observation(
            [{"operation": "start_turn", "observed_model": None, "observed_effort": None}]
        )
        self.assertIsNone(missing["actual_model"])
        self.assertEqual(
            "PROVIDER_RAW_MODEL_EFFORT_NOT_OBSERVED",
            missing["observation_reason"],
        )
        mixed = _checkpoint_model_observation(
            [
                {
                    "operation": "start_turn",
                    "model_observation_source": "provider_raw_response",
                    "observed_model": "model-a",
                    "observed_effort": "high",
                },
                {
                    "operation": "start_turn",
                    "model_observation_source": "provider_raw_response",
                    "observed_model": "model-b",
                    "observed_effort": "xhigh",
                },
            ]
        )
        self.assertIsNone(mixed["actual_model"])
        self.assertEqual("MULTIPLE_OBSERVED_MODEL_EFFORT_PAIRS", mixed["observation_reason"])

    def test_completion_observer_is_forwarded_and_journals_sdk_usage_before_stored_read(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, _ = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "callback"}),
                fixture_digest=sha256_digest({"fixture": "callback"}),
            )
            underlying = _CompletionCallbackFakeRuntime(self.inventory)
            runtime = RecordedRuntime(underlying, journal=cell_root / "runtime.json")
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)
            self.assertIsNotNone(dispatched.attempt_id)
            self.assertTrue(callable(getattr(runtime, "register_completion_observer", None)))

            completion = underlying.emit_completion()
            stored = runtime.read_stored(thread_id=completion.thread_id)
            self.assertIsNone(stored.payload["usage"])
            with prepared.service.ledger.read() as connection:
                provider_call = connection.execute(
                    "SELECT status, actual_tokens, usage_id FROM provider_calls "
                    "WHERE attempt_id=?",
                    (dispatched.attempt_id,),
                ).fetchone()
                usage_count = connection.execute(
                    "SELECT COUNT(*) FROM budget_usage WHERE project_id=?",
                    (prepared.project_id,),
                ).fetchone()[0]
            self.assertEqual("settled", provider_call["status"])
            self.assertEqual(18, provider_call["actual_tokens"])
            self.assertIsNotNone(provider_call["usage_id"])
            self.assertEqual(1, usage_count)
            callbacks = [
                event
                for event in runtime.events
                if event["operation"] == "completion_observation"
            ]
            self.assertEqual(1, len(callbacks))
            self.assertEqual(completion.model_dump(mode="json"), callbacks[0]["receipt"])
            self.assertEqual(completion.turn_id, callbacks[0]["request_binding"]["turn_id"])
            self.assertIsNone(callbacks[0]["observed_model"])
            self.assertEqual(
                "PROVIDER_RAW_RESPONSE_MODEL_OR_EFFORT_NOT_REPORTED",
                callbacks[0]["model_observation_reason"],
            )

    def test_severed_collector_drops_late_completion_callback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, _ = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
            )
            underlying = _CompletionCallbackFakeRuntime(self.inventory)
            runtime = RecordedRuntime(underlying, journal=cell_root / "runtime.json")
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)

            runtime.sever_completion_forwarding()
            underlying.emit_completion()

            self.assertNotIn(
                "completion_observation",
                [event["operation"] for event in runtime.events],
            )
            with prepared.service.ledger.read() as connection:
                provider_call = connection.execute(
                    "SELECT status,new_turn_count FROM provider_calls WHERE attempt_id=?",
                    (dispatched.attempt_id,),
                ).fetchone()
            self.assertEqual(("reserved", 0), tuple(provider_call))

    def test_prepare_preserves_exact_plan_and_driver_activation_evidence(self) -> None:
        contract_digest = sha256_digest({"contract": "project-e2e"})
        fixture_digest = sha256_digest({"fixture": "normal-completion"})
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, _ = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=contract_digest,
                fixture_digest=fixture_digest,
            )
            generated = json.loads((cell_root / "generated-plan.json").read_text(encoding="utf-8"))
            activated = json.loads(
                (cell_root / "plan-activation-receipt.json").read_text(encoding="utf-8")
            )

            self.assertEqual(contract_digest, generated["evaluation_contract_digest"])
            self.assertEqual(fixture_digest, generated["fixture_digest"])
            self.assertEqual(prepared.plan_revision_id, generated["plan_revision_id"])
            self.assertEqual(prepared.activation_digest, generated["activation_digest"])
            self.assertEqual(prepared.plan_revision_id, activated["plan_revision_id"])
            self.assertEqual(prepared.activation_digest, activated["activation_digest"])
            self.assertEqual("qualification", activated["activation_source"])
            self.assertEqual("flowmarshal-engine-eval project-e2e", activated["driver"])
            self.assertEqual(str(workspace.resolve()), activated["workspace"])

    def test_raw_request_prepare_uses_user_facade_roles_and_one_authorization(self) -> None:
        from tests.engine_helpers import inventory
        from tests.engine_inspection_helpers import InspectionScriptedRunner
        from tests.test_engine_user_facade import _responses, _roles

        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "raw-request-cell"
            cell_root.mkdir()
            workspace, _ = _copy_fixture(ROOT, cell_root)
            runner = InspectionScriptedRunner(_responses())
            prepared = _prepare_from_raw_request(
                workspace=workspace,
                state_root=cell_root / "state",
                runtime=FakeCodexRuntime(inventory()),
                roles=_roles(),
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "raw"}),
                fixture_digest=sha256_digest({"fixture": "raw"}),
                source_request="두 단계 변경을 실제 사용자 흐름으로 준비해줘.",
                structured_runner=runner,
            )
            self.assertEqual("live", prepared.preparation_provenance.value)
            self.assertIsNone(prepared.proposal)
            self.assertEqual(
                (
                    "raw_request",
                    "goal_normalizer",
                    "goal_reviewer",
                    "skeleton_generator",
                    "skeleton_reviewer",
                    "plan_expander",
                    "plan_reviewer",
                    "goal_authorization",
                    "plan_activation",
                ),
                prepared.pipeline_stages,
            )
            generated = json.loads(
                (cell_root / "generated-plan.json").read_text(encoding="utf-8")
            )
            self.assertEqual(2, len(generated["plan"]["definition"]["tasks"]))
            with prepared.service.ledger.read() as connection:
                authorization_count = connection.execute(
                    "SELECT COUNT(*) FROM goal_authorizations WHERE project_id=?",
                    (prepared.project_id,),
                ).fetchone()[0]
            self.assertEqual(1, authorization_count)
            self.assertTrue((cell_root / "raw-request.json").is_file())
            self.assertTrue((cell_root / "goal-preparation.json").is_file())
            self.assertTrue((cell_root / "planning-outcome.json").is_file())

    def test_unknown_create_receipt_observes_empty_thread_and_safely_releases_reservation(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "unknown"}),
                fixture_digest=sha256_digest({"fixture": "unknown"}),
            )
            runtime = RecordedRuntime(
                FakeCodexRuntime(self.inventory), journal=cell_root / "runtime.json"
            )
            result = _unknown_receipt(prepared, runtime, source_digest, governance=ALLOW_ALL)

            self.assertTrue(result["passed"], result)
            evidence = result["empty_thread_reconciliation"]
            self.assertTrue(evidence["thread_read_without_resume"])
            self.assertEqual(0, evidence["observed_turn_count"])
            self.assertFalse(evidence["model_turn_effect"])
            self.assertIsNone(evidence["model_usage"])
            self.assertEqual("NO_MODEL_TURN_OBSERVED", evidence["model_usage_reason"])
            self.assertTrue(evidence["provider_reservation_released"])
            self.assertFalse(evidence["provider_reservation_unresolved"])
            self.assertEqual("released", evidence["provider_reservation_status"])
            self.assertIsNone(evidence["provider_actual_tokens"])
            self.assertIsNone(evidence["provider_receipt"])
            self.assertIsNone(evidence["provider_usage_id"])
            self.assertEqual(1, evidence["reservation_release_history_count"])
            self.assertEqual(0, evidence["budget_usage_count"])
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(0, runtime.turn_calls)
            self.assertEqual(0, runtime.resume_calls)
            self.assertNotIn("start_turn", [event["operation"] for event in runtime.events])
            self.assertNotIn("resume", [event["operation"] for event in runtime.events])

    def test_cancel_active_job_observes_exact_turn_without_new_provider_effect(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "cancel"}),
                fixture_digest=sha256_digest({"fixture": "cancel-active-job"}),
            )
            runtime = RecordedRuntime(
                _StoredNonActiveFakeRuntime(self.inventory),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _cancel_active_job(
                prepared,
                runtime,
                source_digest,
                governance=ALLOW_ALL,
                timeout_seconds=2,
            )

            self.assertTrue(result["passed"], result)
            self.assertEqual("cancelled", result["control_state"])
            self.assertEqual("WORKFLOW_CANCELLED", result["run_once_blocker"])
            self.assertEqual("consumed", result["runtime_job"]["status"])
            self.assertEqual("interrupted", result["ledger"]["attempt_status"])
            self.assertEqual(
                {"create_thread": 0, "start_turn": 0, "resume": 0},
                result["effect_count"],
            )
            self.assertTrue(result["exact_binding_preserved"])
            self.assertIn("read_stored", [event["operation"] for event in runtime.events])

            (cell_root / "qualification-observation.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            digest = sha256_digest({"binding": "cancel-active-job"})
            outcome = _responsibility_outcome(
                scenario="cancel-active-job",
                prepared=prepared,
                cell=result,
                contract=SimpleNamespace(contract_digest=digest),
                cell_root=cell_root,
                run_root=cell_root.parent,
                fixture_digest=digest,
                freeze_bundle_digest=digest,
                governance_plugin_identity_digest=digest,
                candidate_wheel_digest=digest,
                candidate_wheel_binding_digest=digest,
                candidate_distribution_name="flowmarshal-engine",
                candidate_distribution_version="1.0.0",
            )
            self.assertEqual(("E2E-16",), outcome.responsibility_ids)
            self.assertEqual((EvidenceProvenance.LIVE,), outcome.provenance)
            self.assertEqual(
                {"runtime_job", "control_state", "runtime_observation", "ledger"},
                set(outcome.evidence_kinds),
            )

    def test_forced_collector_termination_reobserves_exact_turn_without_new_effect(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "termination"}),
                fixture_digest=sha256_digest({"fixture": "forced-termination"}),
            )
            runtime = RecordedRuntime(
                _StoredNonActiveFakeRuntime(self.inventory),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _forced_termination_no_duplicate(
                prepared,
                runtime,
                source_digest,
                governance=ALLOW_ALL,
                timeout_seconds=2,
            )

            self.assertTrue(result["passed"], result)
            self.assertEqual("in_process_collector_termination", result["fault_injection"]["kind"])
            self.assertEqual("running", result["runtime_job"]["status"])
            self.assertTrue(result["exact_binding_preserved"])
            self.assertEqual(0, result["effect_count"]["create_thread"])
            self.assertEqual(0, result["effect_count"]["start_turn"])
            self.assertEqual(0, result["effect_count"]["resume"])
            self.assertEqual(0, result["effect_count"]["interrupt"])
            self.assertGreaterEqual(result["effect_count"]["read_stored"], 1)
            self.assertEqual(("observed", "observed", "observed"), result["repeated_tick_actions"])
            self.assertEqual(
                result["ledger"]["effect_counts_before"],
                result["ledger"]["effect_counts_after"],
            )
            self.assertTrue(result["cleanup"]["passed"], result["cleanup"])
            self.assertEqual("provider_terminal", result["cleanup"]["terminal_job"]["status"])
            self.assertEqual(
                "interrupted",
                result["cleanup"]["terminal_job"]["provider_terminal_status"],
            )
            self.assertEqual("consumed", result["cleanup"]["final_job"]["status"])
            self.assertEqual("cancelled", result["cleanup"]["control"]["control_state"])
            self.assertEqual("interrupted", result["cleanup"]["attempt_status"])
            self.assertEqual(
                "workflow_cancelled_and_attempt_terminal",
                result["cleanup"]["post_cell_reuse_policy"],
            )
            self.assertEqual(0, result["cleanup"]["effect_count"]["create_thread"])
            self.assertEqual(0, result["cleanup"]["effect_count"]["start_turn"])
            self.assertEqual(0, result["cleanup"]["effect_count"]["resume"])
            self.assertEqual(1, result["cleanup"]["effect_count"]["interrupt"])

            (cell_root / "qualification-observation.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            digest = sha256_digest({"binding": "forced-termination"})
            outcome = _responsibility_outcome(
                scenario="forced-termination-no-duplicate",
                prepared=prepared,
                cell=result,
                contract=SimpleNamespace(contract_digest=digest),
                cell_root=cell_root,
                run_root=cell_root.parent,
                fixture_digest=digest,
                freeze_bundle_digest=digest,
                governance_plugin_identity_digest=digest,
                candidate_wheel_digest=digest,
                candidate_wheel_binding_digest=digest,
                candidate_distribution_name="flowmarshal-engine",
                candidate_distribution_version="1.0.0",
            )
            self.assertEqual(("E2E-09",), outcome.responsibility_ids)
            self.assertEqual(
                (EvidenceProvenance.LIVE, EvidenceProvenance.FAULT_INJECTED),
                outcome.provenance,
            )

    def test_absolute_timeout_interrupts_exact_turn_and_blocks_duplicate_execution(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                evaluation_policies=policies,
                evaluation_contract_digest=sha256_digest({"contract": "timeout"}),
                fixture_digest=sha256_digest({"fixture": "absolute-timeout"}),
            )
            runtime = RecordedRuntime(
                _StoredNonActiveFakeRuntime(self.inventory),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _absolute_timeout_no_duplicate(
                prepared,
                runtime,
                source_digest,
                governance=ALLOW_ALL,
                timeout_seconds=3,
            )

            self.assertTrue(result["passed"], result)
            self.assertEqual(
                "runtime_job_clock_advanced_to_deadline_after_binding",
                result["deadline"]["kind"],
            )
            self.assertEqual(
                result["deadline"]["absolute_deadline_at"],
                result["deadline"]["injected_clock_at"],
            )
            self.assertEqual("failed", result["ledger"]["attempt_status"])
            self.assertEqual("environment", result["ledger"]["failure_class"])
            self.assertEqual("ABSOLUTE_DEADLINE_EXCEEDED", result["ledger"]["evidence"]["observation"]["failure_diagnosis"]["local_engine_code"])
            self.assertEqual("ENVIRONMENT_RECOVERY_REQUIRED", result["run_once_blocker"])
            self.assertEqual("consumed", result["runtime_job"]["status"])
            self.assertTrue(result["supervisor_reused_across_deadline"])
            self.assertEqual(0, result["effect_count"]["create_thread"])
            self.assertEqual(0, result["effect_count"]["start_turn"])
            self.assertEqual(0, result["effect_count"]["resume"])
            self.assertEqual(1, result["effect_count"]["interrupt"])
            self.assertEqual(
                result["ledger"]["effect_counts_before"],
                result["ledger"]["effect_counts_after"],
            )

            (cell_root / "qualification-observation.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            digest = sha256_digest({"binding": "absolute-timeout"})
            outcome = _responsibility_outcome(
                scenario="absolute-timeout-no-duplicate",
                prepared=prepared,
                cell=result,
                contract=SimpleNamespace(contract_digest=digest),
                cell_root=cell_root,
                run_root=cell_root.parent,
                fixture_digest=digest,
                freeze_bundle_digest=digest,
                governance_plugin_identity_digest=digest,
                candidate_wheel_digest=digest,
                candidate_wheel_binding_digest=digest,
                candidate_distribution_name="flowmarshal-engine",
                candidate_distribution_version="1.0.0",
            )
            self.assertEqual(("E2E-10",), outcome.responsibility_ids)
            self.assertEqual(
                {"runtime_job", "deadline", "runtime_observation", "effect_count"},
                set(outcome.evidence_kinds),
            )

    def test_journal_tamper_and_nonresumable_cell_are_explicitly_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            journal = Path(raw) / "runtime.json"
            journal.write_text(
                json.dumps(
                    [{"schema": "flowmarshal.project-e2e.runtime-event.v2"}]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(QualificationRunError, "schema"):
                RecordedRuntime(FakeCodexRuntime(self.inventory), journal=journal)
            journal.write_text(
                json.dumps(
                    [
                        {
                            "schema": "flowmarshal.project-e2e.runtime-event.v3",
                            "operation": "list_models",
                            "event_digest": "sha256:" + "0" * 64,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(QualificationRunError, "event digest"):
                RecordedRuntime(FakeCodexRuntime(self.inventory), journal=journal)

        paused = _e2e_failure_disposition(
            "normal-completion",
            RuntimeError("provider rate limit"),
            provider_calls=(),
        )
        self.assertEqual(EvaluationRunStatus.PAUSED_RATE_LIMIT, paused[0])
        self.assertTrue(paused[1])
        self.assertIn("BEFORE_PROVIDER_EFFECT", paused[2])
        released = _e2e_failure_disposition(
            "normal-completion",
            RuntimeError("provider rate limit"),
            provider_calls=(("call_released", "released"),),
        )
        self.assertEqual(EvaluationRunStatus.PAUSED_RATE_LIMIT, released[0])
        self.assertTrue(released[1])
        self.assertIn("call_released=released", released[2])
        partial = _e2e_failure_disposition(
            "normal-completion",
            RuntimeError("provider rate limit"),
            provider_calls=(("call_partial", "reserved"),),
        )
        self.assertEqual(EvaluationRunStatus.FAILED, partial[0])
        self.assertFalse(partial[1])
        self.assertIn("call_partial=reserved", partial[2])
        self.assertIn("새 Goal 예산으로 우회하지 않습니다", partial[2])
        mixed = _e2e_failure_disposition(
            "normal-completion",
            RuntimeError("provider rate limit"),
            provider_calls=(("call_complete", "settled"), ("call_released", "released")),
        )
        self.assertEqual(EvaluationRunStatus.FAILED, mixed[0])
        self.assertFalse(mixed[1])
        unreadable = _e2e_failure_disposition(
            "normal-completion",
            RuntimeError("provider rate limit"),
            provider_calls=None,
        )
        self.assertEqual(EvaluationRunStatus.FAILED, unreadable[0])
        self.assertFalse(unreadable[1])
        self.assertIn("provider_calls=[unavailable]", unreadable[2])
        failed = _e2e_failure_disposition(
            "stored-turn-restart-resume",
            RuntimeError("provider rate limit"),
            provider_calls=(),
        )
        self.assertEqual(EvaluationRunStatus.FAILED, failed[0])
        self.assertFalse(failed[1])
        self.assertIn("provider terminal", failed[2])
        self.assertIn("미확인 효과", failed[2])

    def test_run_level_restart_blocks_partial_provider_call_before_runtime_entry(self) -> None:
        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "e2e-run"
            cell_root = destination / "work" / "normal-completion"
            cell_root.mkdir(parents=True)
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            contract = _contract(
                ROOT, self.inventory, self.roles, source_digest, policies
            )
            store = ImmutableCheckpointStore(destination, contract)
            store.initialize()
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
                semantic_task_validation=True,
                evaluation_policies=policies,
                evaluation_contract_digest=contract.contract_digest,
                fixture_digest=contract.fixture_digests[0],
            )
            goal = prepared.service.load_active_goal(prepared.project_id)
            call_id = BudgetManager(prepared.service).reserve(
                project_id=prepared.project_id,
                goal_id=goal.goal_id,
                goal_digest=goal.definition_digest,
                call_key="hard-crash-partial-role",
                role="execution_preparation",
                request={"phase": "partial"},
            )
            before_calls = evaluation_cell_provider_calls(cell_root / "state")
            self.assertEqual(((call_id, "reserved"),), before_calls)

            with patch(
                "flowmarshal.engine.e2e_qualification.CodexAppServerRuntime"
            ) as runtime, patch(
                "flowmarshal.engine.e2e_qualification._preflight"
            ) as preflight, self.assertRaisesRegex(
                QualificationRunError, "E2E_PROVIDER_EFFECT_RECONCILIATION_REQUIRED"
            ):
                run_project_e2e(
                    root=ROOT,
                    run_root=destination,
                    role_configuration=self.roles,
                    evaluation_policies=policies,
                    governance=ALLOW_ALL,
                )

            runtime.assert_not_called()
            preflight.assert_not_called()
            self.assertEqual(EvaluationRunStatus.FAILED, store.state().status)
            self.assertEqual(before_calls, evaluation_cell_provider_calls(cell_root / "state"))

    def test_existing_inventory_observation_must_match_full_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "inventory.json"
            _preserve_inventory_observation(path, self.roles, self.inventory)
            expected = self.roles.operational_binding(self.inventory).model_dump(mode="json")
            self.assertEqual(expected, json.loads(path.read_text(encoding="utf-8")))
            changed = dict(expected)
            changed["inventory_digest"] = "sha256:" + "f" * 64
            path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(QualificationRunError, "inventory observation"):
                _preserve_inventory_observation(path, self.roles, self.inventory)


if __name__ == "__main__":
    unittest.main()
