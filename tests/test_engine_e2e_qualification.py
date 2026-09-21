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
    _DRAFT_ACTIVATION_ERROR,
    _E2E_SOURCE_REQUEST,
    _PROHIBITED_EFFECT_REQUEST,
    _copy_fixture,
    _e2e_failure_disposition,
    _forced_termination_no_duplicate,
    _guard_e2e_partial_resume,
    _in_flight_replan_protection,
    _ledger_delta,
    _observe_frozen_plugin_identity,
    _partial_write_input_changed,
    _partial_write_resume,
    _prepare,
    _prepare_from_raw_request,
    _preserve_inventory_observation,
    _prohibited_effect_blocked,
    _responsibility_outcome,
    _scope_expansion_blocked,
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
    QualificationCellStatus,
    QualificationFailureClass,
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


class _TurnCompletingFakeRuntime(_RequestEchoFakeRuntime):
    def __init__(self, inventory, workspace: Path, *, completion_turns: set[int]):
        super().__init__(inventory)
        self.workspace = workspace
        self.completion_turns = completion_turns

    def start_turn(self, **arguments):
        receipt = super().start_turn(**arguments)
        if self.turn_calls in self.completion_turns:
            (self.workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n    return left + right\n",
                encoding="utf-8",
            )
            self.complete(arguments["thread_id"], response="완료")
        return receipt


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

    def test_invalid_clean_install_link_stops_before_provider(self) -> None:
        from flowmarshal.canonical import sha256_bytes
        from flowmarshal.engine.qualification_manifest import CandidateWheelBinding

        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            wheel = base / "candidate.whl"
            wheel.write_bytes(b"candidate")
            import_root = base / "site-packages" / "flowmarshal"
            import_root.mkdir(parents=True)
            digests = {"flowmarshal/__init__.py": sha256_bytes(b"package")}
            binding = CandidateWheelBinding(
                wheel_path=str(wheel), wheel_digest=sha256_bytes(b"candidate"),
                distribution_name="flowmarshal-engine", distribution_version="0.2.0a1",
                distribution_root=str(import_root.parent), import_root=str(import_root),
                package_file_digests=digests, wheel_package_digest=sha256_digest(digests),
                installed_package_digest=sha256_digest(digests),
            )
            release = SimpleNamespace(
                valid=True, mismatches=(),
                manifest=SimpleNamespace(governance_plugin=None, freeze_digest="sha256:" + "1" * 64),
            )
            with patch(
                "flowmarshal.engine.e2e_qualification._bind_project_e2e_candidate_wheel",
                return_value=binding,
            ), patch(
                "flowmarshal.engine.release_freeze.verify_release_freeze", return_value=release,
            ), patch(
                "flowmarshal.engine.e2e_qualification._preflight"
            ) as preflight, patch(
                "flowmarshal.engine.e2e_qualification.CodexAppServerRuntime"
            ) as runtime:
                for broken in (Path("relative-clean-install"), base / "missing-clean-install"):
                    with self.subTest(broken=broken), self.assertRaisesRegex(
                        QualificationRunError, "E2E_CLEAN_INSTALL_LINK_INVALID"
                    ):
                        run_project_e2e(
                            root=ROOT,
                            evaluation_policies=policies,
                            candidate_wheel=wheel,
                            release_freeze=base / "freeze",
                            clean_install_run_root=broken,
                        )
            preflight.assert_not_called()
            runtime.assert_not_called()

    def _linked_clean_install(self):
        """검증기를 실제로 통과하는 clean install run root를 만든다(clean install 테스트 fixture 재사용)."""

        from tests.test_engine_clean_install_qualification import CleanInstallQualificationTests

        case = CleanInstallQualificationTests(
            "test_link_passes_for_verified_live_run_with_same_candidate"
        )
        case.setUp()
        self.addCleanup(case.tearDown)
        binding, report = case._linked_run()
        return case, binding, report

    def _enter_project_e2e_fakes(self, stack, *, binding, prepare, inventory=None) -> None:
        """provider·freeze·설치 결속·governance 설정만 가짜로 두고 run_project_e2e 본문을 돌린다."""

        from contextlib import nullcontext

        runtime_inventory = inventory or self.inventory
        release = SimpleNamespace(
            valid=True,
            mismatches=(),
            manifest=SimpleNamespace(governance_plugin=None, freeze_digest="sha256:" + "1" * 64),
        )
        for target, options in (
            ("flowmarshal.engine.e2e_qualification._bind_project_e2e_candidate_wheel",
             {"return_value": binding}),
            ("flowmarshal.engine.release_freeze.verify_release_freeze", {"return_value": release}),
            ("flowmarshal.engine.e2e_qualification._preflight", {"return_value": ()}),
            ("flowmarshal.engine.e2e_qualification.CodexAppServerRuntime",
             {"side_effect": lambda **_: nullcontext(FakeCodexRuntime(runtime_inventory))}),
            ("flowmarshal.engine.e2e_qualification._observe_frozen_plugin_identity",
             {"return_value": {"governance_plugin_identity_digest": "sha256:" + "e" * 64}}),
            ("flowmarshal.engine.governance_gate.GovernanceSettings.from_environment",
             {"return_value": ALLOW_ALL}),
            # _contract가 이 함수의 소스를 digest에 넣으므로 MagicMock이 아니라 함수로 바꾼다.
            ("flowmarshal.engine.e2e_qualification._prepare_from_raw_request", {"new": prepare}),
        ):
            stack.enter_context(patch(target, **options))

    def test_clean_install_anchor_survives_rate_limit_pause_and_resume(self) -> None:
        from contextlib import ExitStack

        from flowmarshal.engine.evaluation import EvaluationContract
        from flowmarshal.engine.ledger import SQLiteEngineLedger
        from flowmarshal.engine.qualification import resume_run
        from flowmarshal.engine.service import EngineService

        case, binding, report = self._linked_clean_install()
        anchor = {
            "run_root": str(case.run_root.resolve()),
            "report_digest": report.report_digest,
            "evaluation_contract_digest": case.contract_digest,
        }
        prepared_roots: list[Path] = []

        def rate_limited(**kwargs):
            # provider 호출 전 사용량 제한: 빈 cell 원장만 남기고 멈춘다.
            state_root = kwargs["state_root"]
            prepared_roots.append(state_root)
            EngineService(SQLiteEngineLedger(
                state_root / "flowmarshal-engine.sqlite3", artifact_root=state_root / "artifacts"
            )).initialize()
            raise RuntimeError("provider rate limit")

        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            destination = Path(raw) / "e2e-run"
            freeze = Path(raw) / "freeze"
            freeze.mkdir()
            self._enter_project_e2e_fakes(stack, binding=binding, prepare=rate_limited)
            arguments = {
                "root": ROOT,
                "run_root": destination,
                "role_configuration": self.roles,
                "evaluation_policies": policies,
                "candidate_wheel": case.wheel,
                "governance": ALLOW_ALL,
                "release_freeze": freeze,
            }
            with self.assertRaisesRegex(RuntimeError, "rate limit"):
                run_project_e2e(**arguments, clean_install_run_root=case.run_root)
            contract = EvaluationContract.model_validate_json(
                (destination / "evaluation-contract.json").read_text(encoding="utf-8")
            )
            store = ImmutableCheckpointStore(destination, contract)
            self.assertEqual(EvaluationRunStatus.PAUSED_RATE_LIMIT, store.state().status)
            metadata_path = destination / "run-metadata.json"
            self.assertEqual(
                anchor, json.loads(metadata_path.read_text(encoding="utf-8"))["clean_install_link"]
            )
            saved = metadata_path.read_bytes()

            # 연결을 빠뜨린 재개는 metadata 대조에서 멈추고 pause 상태를 RUNNING으로 바꾸지 않는다.
            with self.assertRaisesRegex(ValueError, "EVALUATION_RUN_METADATA_BINDING_MISMATCH"):
                run_project_e2e(**arguments)
            self.assertEqual(EvaluationRunStatus.PAUSED_RATE_LIMIT, store.state().status)

            # resume_run은 고정한 연결을 다시 넘겨 metadata 대조를 지나 미완료 cell 재개 단계에 닿는다.
            with patch(
                "flowmarshal.engine.qualification.verify_candidate_wheel_metadata",
                return_value=binding,
            ), self.assertRaisesRegex(QualificationRunError, "재개 상태가 없습니다"):
                resume_run(destination)
            self.assertEqual(saved, metadata_path.read_bytes())
            self.assertEqual(1, len(prepared_roots))

    def test_project_e2e_routes_authority_cells_and_keeps_typed_failures(self) -> None:
        from contextlib import ExitStack

        from flowmarshal.engine.evaluation import EvaluationContract
        from tests.engine_helpers import inventory
        from tests.engine_inspection_helpers import InspectionScriptedRunner
        from tests.test_engine_user_facade import _responses, _roles

        case, binding, _report = self._linked_clean_install()
        requested: dict[str, dict[str, object]] = {}

        def scripted(**kwargs):
            # 실제 _prepare_from_raw_request를 scripted 역할로 돌린다(제품 facade 경로 그대로).
            responses = _responses()
            responses["goal_normalizer"][0]["prohibited_effects"] = ["원격 저장소에 변경을 push한다."]
            requested[kwargs["state_root"].parent.name] = {
                "source_request": kwargs["source_request"],
                "authorize": kwargs.get("authorize", True),
            }
            return _prepare_from_raw_request(
                **kwargs, structured_runner=InspectionScriptedRunner(responses)
            )

        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            destination = Path(raw) / "e2e-run"
            freeze = Path(raw) / "freeze"
            freeze.mkdir()
            self._enter_project_e2e_fakes(
                stack, binding=binding, prepare=scripted, inventory=inventory()
            )
            stack.enter_context(patch(
                "flowmarshal.engine.e2e_qualification.E2E_SCENARIOS",
                ("prohibited-effect", "scope-expansion"),
            ))
            _run_root, report = run_project_e2e(
                root=ROOT,
                run_root=destination,
                role_configuration=_roles(),
                evaluation_policies=policies,
                candidate_wheel=case.wheel,
                governance=ALLOW_ALL,
                release_freeze=freeze,
                clean_install_run_root=case.run_root,
            )
            contract = EvaluationContract.model_validate_json(
                (destination / "evaluation-contract.json").read_text(encoding="utf-8")
            )
            store = ImmutableCheckpointStore(destination, contract)
            cells = {
                scenario: store.completed(digest, 0).raw_structured_assessment
                for scenario, digest in zip(
                    ("prohibited-effect", "scope-expansion"), contract.fixture_digests
                )
            }
            receipt = json.loads(
                (destination / "work" / "scope-expansion" / "plan-activation-receipt.json")
                .read_text(encoding="utf-8")
            )

        # E2E-14만 금지 효과 요청을 쓰고, E2E-15만 승인을 cell driver에 넘긴다.
        self.assertEqual(
            {
                "prohibited-effect": {"source_request": _PROHIBITED_EFFECT_REQUEST, "authorize": True},
                "scope-expansion": {"source_request": _E2E_SOURCE_REQUEST, "authorize": False},
            },
            requested,
        )
        prohibited = cells["prohibited-effect"]
        self.assertTrue(prohibited["passed"], prohibited)
        self.assertEqual(["live", "fake"], prohibited["qualification_outcome"]["provenance"])
        scope = cells["scope-expansion"]
        self.assertFalse(scope["passed"])
        self.assertEqual("product", scope["qualification_outcome"]["failure_class"])
        self.assertEqual(
            "E2E15_TARGET_EFFECT_NOT_COVERED_LIVE", scope["qualification_outcome"]["failure_code"]
        )
        # 두 cell 모두 활성화 receipt digest가 있고 E2E-15 receipt는 이 계약·fixture에 결속된다.
        for item in cells.values():
            self.assertIsNotNone(item["qualification_plan_activation"]["activation_receipt_digest"])
        self.assertEqual(contract.contract_digest, receipt["evaluation_contract_digest"])
        self.assertEqual(contract.fixture_digests[1], receipt["fixture_digest"])
        self.assertIn(
            "PRODUCT_FAILURE:E2E-15:E2E15_TARGET_EFFECT_NOT_COVERED_LIVE", report.failures
        )
        self.assertFalse(any(item.startswith(("PRODUCT_FAILURE:E2E-14", "PROVENANCE")) for item in report.failures))
        self.assertEqual("passed", report.metrics["clean_install_link_status"])
        # E2E-14와 연결된 E2E-18만 PASS다.
        self.assertEqual(2, report.metrics["passed_responsibility_count"])

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
            # scripted runner·fake runtime으로 돈 준비는 LIVE로 적지 않는다.
            self.assertEqual("fake", prepared.preparation_provenance.value)
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
            before = result["ledger"]["effect_counts_before"]
            after = result["ledger"]["effect_counts_after"]
            for key in (
                "provider_calls",
                "runtime_intents",
                "runtime_receipts",
                "distinct_provider_operation_ids",
            ):
                self.assertEqual(before[key], after[key])
            self.assertEqual(0, before["provider_new_turn_count"])
            self.assertEqual(1, after["provider_new_turn_count"])
            self.assertEqual(1, before["provider_reserved_calls"])
            self.assertEqual(0, after["provider_reserved_calls"])

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

    def test_partial_write_input_change_blocks_resume_and_records_residual_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
            )
            runtime = RecordedRuntime(
                _RequestEchoFakeRuntime(self.inventory),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _partial_write_input_changed(
                prepared,
                runtime,
                source_digest,
                cell_root=cell_root,
                governance=ALLOW_ALL,
                timeout_seconds=3,
            )

            self.assertTrue(result["passed"], result)
            self.assertEqual("STALE_EXECUTION_INPUT", result["error"]["code"])
            self.assertEqual(
                {"create_thread": 0, "start_turn": 0, "resume": 0},
                result["effect_count"]["provider_operations"],
            )
            before_effect = result["effect_count"]["attempt_before_preflight"]
            first_block = result["effect_count"]["attempt_after_first_block"]
            self.assertEqual(
                first_block,
                result["effect_count"]["attempt_after_repeated_block"],
            )
            self.assertEqual(
                before_effect["runtime_receipts"], first_block["runtime_receipts"]
            )
            self.assertTrue(any(
                item["kind"] == "mutable_target_observation"
                and item["changed"]
                for item in result["error"]["changes"]
            ))

            (cell_root / "qualification-observation.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            digest = sha256_digest({"binding": "partial-write-input-changed"})
            outcome = _responsibility_outcome(
                scenario="partial-write-input-changed",
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
            self.assertEqual(("E2E-13",), outcome.responsibility_ids)
            self.assertEqual(
                (EvidenceProvenance.FAULT_INJECTED,), outcome.provenance
            )

    def test_partial_write_resume_keeps_thread_and_reaches_independent_validation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
            )
            runtime = RecordedRuntime(
                _TurnCompletingFakeRuntime(
                    self.inventory, workspace, completion_turns={2}
                ),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _partial_write_resume(
                prepared,
                runtime,
                source_digest,
                cell_root=cell_root,
                governance=ALLOW_ALL,
                timeout_seconds=3,
            )

            self.assertTrue(result["passed"], result)
            self.assertEqual(
                result["binding"]["before"]["thread_id"],
                result["binding"]["after"]["thread_id"],
            )
            self.assertNotEqual(
                result["binding"]["before"]["turn_id"],
                result["binding"]["after"]["turn_id"],
            )
            self.assertEqual(1, runtime.resume_calls)
            self.assertEqual(1, result["validation"]["goal_verdict_count"])

    def _raw_request_cell(self, raw: str, *, prohibited: tuple[str, ...] = (), authorize: bool = True):
        from tests.engine_helpers import inventory
        from tests.engine_inspection_helpers import InspectionScriptedRunner
        from tests.test_engine_user_facade import _responses, _roles

        responses = _responses()
        responses["goal_normalizer"][0]["prohibited_effects"] = list(prohibited)
        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root)
        runtime = RecordedRuntime(
            FakeCodexRuntime(inventory()), journal=cell_root / "runtime-receipts.json"
        )
        prepared = _prepare_from_raw_request(
            workspace=workspace,
            state_root=cell_root / "state",
            runtime=runtime,
            roles=_roles(),
            evaluation_policies=load_evaluation_policies(
                budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
                role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
            ),
            evaluation_contract_digest=sha256_digest({"contract": "authority"}),
            fixture_digest=sha256_digest({"fixture": "authority"}),
            source_request=_PROHIBITED_EFFECT_REQUEST,
            structured_runner=InspectionScriptedRunner(responses),
            authorize=authorize,
        )
        return cell_root, prepared, runtime, source_digest, _roles()

    def test_prohibited_effect_candidate_is_blocked_before_execution(self) -> None:
        statement = "원격 저장소에 변경을 push한다."
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, roles = self._raw_request_cell(
                raw, prohibited=(statement,)
            )
            active_before = prepared.service.status(prepared.project_id)["project"][
                "active_plan_revision_id"
            ]

            result = _prohibited_effect_blocked(
                prepared, runtime, source_digest, cell_root=cell_root, roles=roles
            )

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertEqual(_DRAFT_ACTIVATION_ERROR, result["error"])
            self.assertIn("PLAN_EFFECT_POLICY_VIOLATION", result["candidate_finding_codes"])
            self.assertEqual({"create_thread": 0, "start_turn": 0, "resume": 0}, result["effect_count"])
            self.assertEqual(0, result["execution_spec"]["candidate_execution_spec_count"])
            self.assertEqual(
                active_before,
                prepared.service.status(prepared.project_id)["project"]["active_plan_revision_id"],
            )
            observation = json.loads(
                (cell_root / "authorization-observation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "scripted_plan_expander_stub_via_recovery_plan_provider",
                observation["candidate_source"],
            )
            self.assertEqual([statement], observation["goal_prohibited_effects"])
            self.assertEqual([statement], observation["authorization"]["effect_policy"]["prohibited_effects"])
            self.assertEqual(["plan_expander"], observation["stub_calls"])
            self.assertEqual(
                f"Goal 효과 정책 밖의 외부 효과입니다: unexpected=[], prohibited={[statement]}",
                observation["finding_summary"],
            )
            self.assertEqual(["PLAN_EFFECT_POLICY_VIOLATION"], result["candidate_finding_codes"])
            self.assertIn("history_events", observation["measured_tables"])
            # 후보 생성은 stub 호출 한 건과 그 예산 회계 history만 남긴다.
            candidate = observation["candidate_creation_ledger_delta"]
            self.assertEqual({"provider_calls", "history_events"}, set(candidate))
            self.assertTrue(all(
                row["event_type"].startswith("budget.")
                for row in candidate["history_events"]["added"]
            ))
            # 차단 단계에는 provider 호출·실행 행이 없고 draft 후보 행과 등록 history만 생긴다.
            block = observation["block_ledger_delta"]
            self.assertLessEqual(set(block), {"plans", "tasks", "history_events"})
            self.assertEqual(
                ["plan.registered"],
                [row["event_type"] for row in block["history_events"]["added"]],
            )
            with prepared.service.ledger.read() as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM runtime_intents").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])
                self.assertEqual(
                    0, connection.execute("SELECT COUNT(*) FROM execution_spec_revisions").fetchone()[0]
                )

    def test_prohibited_effect_cell_needs_live_prohibition(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, roles = self._raw_request_cell(raw)

            result = _prohibited_effect_blocked(
                prepared, runtime, source_digest, cell_root=cell_root, roles=roles
            )

            self.assertFalse(result["passed"])
            self.assertTrue(result["failure"].startswith("E2E14_PRECONDITION_NO_PROHIBITED_EFFECT"))
            with prepared.service.ledger.read() as connection:
                roles_called = [
                    row[0] for row in connection.execute("SELECT role FROM provider_calls ORDER BY rowid")
                ]
            # 전제 불충족이면 stub 후보를 만들지 않는다.
            self.assertEqual(1, roles_called.count("plan_expander"))

    def test_scope_expansion_policy_boundary_is_observed_but_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, roles = self._raw_request_cell(
                raw, authorize=False
            )
            self.assertEqual("plan_reviewer", prepared.pipeline_stages[-1])
            self.assertFalse((cell_root / "goal-authorization.json").exists())
            goal_id = prepared.service.load_active_goal(prepared.project_id).goal_id

            contract_digest = sha256_digest({"contract": "authority"})
            fixture_digest = sha256_digest({"fixture": "scope-expansion"})
            result = _scope_expansion_blocked(
                prepared,
                runtime,
                source_digest,
                cell_root=cell_root,
                roles=roles,
                evaluation_contract_digest=contract_digest,
                fixture_digest=fixture_digest,
                governance=ALLOW_ALL,
            )

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_TARGET_EFFECT_NOT_COVERED_LIVE", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertTrue(result["failure"].startswith("E2E15_TARGET_EFFECT_NOT_COVERED_LIVE: "))
            self.assertNotIn("정책 경계 단계 실패", result["failure"])
            self.assertTrue(result["coverage"]["policy_boundary_passed"], result)
            self.assertEqual(["target", "effect"], result["coverage"]["not_covered_live"])
            changes = {item["step"]: item["authorization_changes"] for item in result["steps"]}
            self.assertEqual(
                {
                    "A_narrow_authorization": [["policy", "planning_budget.max_logical_role_calls"]],
                    "B_internal_plan_id_activation": [["goal", "authorization"]],
                    "C_user_authorization": None,
                    "D_budget_expansion_run_once": [["policy", f"budget.effective.{goal_id}"]],
                    "E_selected_plan_reactivation": [["policy", f"budget.effective.{goal_id}"]],
                    "F_user_reauthorization": None,
                },
                changes,
            )
            self.assertEqual({"create_thread": 0, "start_turn": 0, "resume": 0}, result["effect_count"])
            scope = json.loads((cell_root / "scope-check.json").read_text(encoding="utf-8"))
            self.assertFalse(scope["cost_estimate"]["authorization_has_plan_or_cost_field"])
            self.assertIn("history_events", scope["measured_tables"])
            self.assertIn("budget_policy_revisions", scope["measured_tables"])
            steps = {item["step"]: item for item in scope["steps"]}
            # 원장 불변 주장은 history·예산 표까지 포함한 측정과 일치한다.
            for name in ("A_narrow_authorization", "B_internal_plan_id_activation",
                         "D_budget_expansion_run_once", "E_selected_plan_reactivation"):
                self.assertEqual({}, steps[name]["ledger_delta"], name)
            setup = steps["D_budget_expansion_run_once"]["budget_setup_ledger_delta"]
            self.assertEqual(
                [goal_id],
                [row["scope_key"] for row in setup["budget_policy_revisions"]["added"]],
            )
            receipt = json.loads(
                (cell_root / "plan-activation-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(contract_digest, receipt["evaluation_contract_digest"])
            self.assertEqual(fixture_digest, receipt["fixture_digest"])
            self.assertEqual(prepared.plan_revision_id, receipt["plan_revision_id"])
            with prepared.service.ledger.read() as connection:
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM goal_authorizations").fetchone()[0])
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM plan_activations").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM runtime_jobs").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def test_scope_expansion_policy_regression_is_primary_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, roles = self._raw_request_cell(
                raw, authorize=False
            )
            # Core가 승인 없이 내부 Plan ID 활성화를 받아 준 회귀를 흉내 낸다.
            with patch.object(
                prepared.service, "activate_authorized_plan", return_value="activation_forged"
            ):
                result = _scope_expansion_blocked(
                    prepared,
                    runtime,
                    source_digest,
                    cell_root=cell_root,
                    roles=roles,
                    evaluation_contract_digest=sha256_digest({"contract": "authority"}),
                    fixture_digest=sha256_digest({"fixture": "scope-expansion"}),
                    governance=ALLOW_ALL,
                )

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_POLICY_BOUNDARY_REGRESSION", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertIn("B_internal_plan_id_activation:failed", result["failure"])
            # 정책 회귀가 주 code여도 미커버 범위는 따로 남는다.
            self.assertFalse(result["coverage"]["policy_boundary_passed"])
            self.assertEqual(["target", "effect"], result["coverage"]["not_covered_live"])

    def test_authority_cells_claim_only_contract_provenance(self) -> None:
        from flowmarshal.engine.e2e_qualification import _cell_failure

        suite = QualificationSuiteManifest.load(ROOT / "config" / "qualification-suite.json")
        requirements = {item.responsibility_id: item for item in suite.e2e_responsibilities}
        digest = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as raw:
            for scenario, responsibility_id, code in (
                ("prohibited-effect", "E2E-14", "E2E14_BLOCK_EVIDENCE_MISSING"),
                ("scope-expansion", "E2E-15", "E2E15_TARGET_EFFECT_NOT_COVERED_LIVE"),
            ):
                cell_root = Path(raw) / scenario
                (cell_root / "state").mkdir(parents=True)
                for name in (
                    "authorization-observation.json", "qualification-observation.json",
                    "scope-check.json", "state/flowmarshal-engine.sqlite3",
                ):
                    (cell_root / name).write_text("{}", encoding="utf-8")
                outcome = _responsibility_outcome(
                    scenario=scenario,
                    prepared=SimpleNamespace(pipeline_stages=("prepare",)),
                    cell={"passed": False, **_cell_failure(code, "설명 문장")},
                    contract=SimpleNamespace(contract_digest=digest),
                    cell_root=cell_root,
                    run_root=Path(raw),
                    fixture_digest=digest,
                    freeze_bundle_digest=digest,
                    governance_plugin_identity_digest=digest,
                    candidate_wheel_digest=digest,
                    candidate_wheel_binding_digest=digest,
                    candidate_distribution_name="flowmarshal-engine",
                    candidate_distribution_version="1.0.0",
                )
                requirement = requirements[responsibility_id]
                # 동결 suite가 허용·요구하는 출처 안에서만 주장한다.
                self.assertLessEqual(set(outcome.provenance), set(requirement.allowed_provenance))
                self.assertLessEqual(set(requirement.required_provenance), set(outcome.provenance))
                self.assertEqual(QualificationCellStatus.FAILED, outcome.status)
                # 책임 판정에는 설명 문장이 아니라 안정된 code와 class가 들어간다.
                self.assertEqual(code, outcome.failure_code)
                self.assertEqual(QualificationFailureClass.PRODUCT, outcome.failure_class)

    def test_ledger_delta_reports_new_changed_and_removed_rows(self) -> None:
        before = {"plans": {"p1": {"id": "p1", "status": "ready"}, "p0": {"id": "p0", "status": "draft"}}}
        after = {"plans": {"p1": {"id": "p1", "status": "active"}, "p2": {"id": "p2", "status": "draft"}}}

        self.assertEqual({}, _ledger_delta(before, before))
        self.assertEqual(
            {
                "plans": {
                    "added": [{"id": "p2", "status": "draft"}],
                    "changed": [{"before": {"id": "p1", "status": "ready"}, "after": {"id": "p1", "status": "active"}}],
                    "removed": [{"id": "p0", "status": "draft"}],
                }
            },
            _ledger_delta(before, after),
        )

    def test_in_flight_replan_rejects_invalid_reuse_and_preserves_active_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace,
                state_root=cell_root / "state",
                inventory=self.inventory,
                roles=self.roles,
            )
            runtime = RecordedRuntime(
                _TurnCompletingFakeRuntime(
                    self.inventory, workspace, completion_turns={1, 2}
                ),
                journal=cell_root / "runtime-receipts.json",
            )

            result = _in_flight_replan_protection(
                prepared,
                runtime,
                source_digest,
                cell_root=cell_root,
                governance=ALLOW_ALL,
                timeout_seconds=3,
            )

            self.assertTrue(result["passed"], result)
            self.assertTrue(result["state_unchanged_during_rejection"])
            self.assertTrue(result["in_flight_error"].startswith(
                "PLAN_REPLACEMENT_IN_FLIGHT:"
            ))
            self.assertEqual(
                "EVIDENCE_INVALID_OR_INSUFFICIENT",
                result["reuse_decision"]["reason"],
            )
            self.assertEqual(
                "qualification_fixture_equivalent_revision",
                result["fault_injection"]["replan_source"],
            )
            self.assertFalse(
                result["fault_injection"]["proves_product_replan_pipeline"]
            )
            self.assertFalse(result["quiescence"]["automatic_resume_occurred"])
            self.assertEqual(
                result["quiescence"]["operation_counts_before"]["start_turn"],
                result["quiescence"]["operation_counts_after"]["start_turn"],
            )
            self.assertEqual(
                result["quiescence"]["attempt_effect_counts_before"]["provider_calls"],
                result["quiescence"]["attempt_effect_counts_after"]["provider_calls"],
            )
            activation_calls = result["fault_injection"]["activation_calls"]
            stale_call = next(
                item
                for item in activation_calls
                if item["purpose"] == "reject_stale_revision_after_attempt_quiescence"
            )
            self.assertTrue(stale_call["error"].startswith(
                "PLAN_STATE_SNAPSHOT_STALE:"
            ))
            self.assertEqual(
                "running", prepared.service.workflow_control_state(prepared.project_id)
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
