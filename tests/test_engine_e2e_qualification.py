from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.budget import BudgetManager
from flowmarshal.engine.domain import (
    ExecutionAction,
    ExecutionContextNeed,
    ExecutionSpecProposal,
    PlanContractRevision,
    RecoveryEnvelope,
    ResolvedTarget,
    ValidationExecutionStep,
)
from flowmarshal.engine.e2e_qualification import (
    PreparedE2E,
    RecordedRuntime,
    UsageWithholdingRuntime,
    _absolute_timeout_no_duplicate,
    _approved_repair,
    _assert_no_transient_plugin_identity_change,
    _cancel_active_job,
    _checkpoint_model_observation,
    _context_discovery,
    _contract,
    _DRAFT_ACTIVATION_ERROR,
    _PROHIBITED_EFFECT_REQUEST,
    _SCOPE_EXPANSION_REQUEST,
    _copy_fixture,
    _e2e_failure_disposition,
    _forced_termination_no_duplicate,
    _guard_e2e_partial_resume,
    _in_flight_replan_protection,
    _initialize_workspace_git,
    _ledger_delta,
    _multi_task_dag,
    _observe_frozen_plugin_identity,
    _partial_write_input_changed,
    _partial_write_resume,
    _prepare,
    _prepare_from_raw_request,
    _preserve_inventory_observation,
    _prohibited_effect_blocked,
    _project_e2e_fixture_source_digest,
    _read_only_report,
    _responsibility_outcome,
    _scope_expansion_blocked,
    _unknown_receipt,
    _usage_missing_late,
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
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeObservation
from tests.engine_inspection_helpers import InspectionScriptedRunner
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


class _CompletingFakeRuntime(_RequestEchoFakeRuntime):
    """turn마다 ``side_effect``를 한 번 부르고 ``provider_payload``를 붙여 바로 완료한다."""

    def __init__(self, inventory, *, side_effect=None, provider_payload=None) -> None:
        super().__init__(inventory)
        self.side_effect = side_effect
        self.provider_payload = provider_payload or {}

    def start_turn(self, **arguments):
        receipt = super().start_turn(**arguments)
        if self.side_effect is not None:
            self.side_effect()
        self.threads[arguments["thread_id"]].provider_payload = dict(self.provider_payload)
        self.complete(arguments["thread_id"], response="완료")
        return receipt


def _replace_text(path: Path, old: str, new: str) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")


# governance multitask fixture의 Task별 올바른 Worker 편집이다.
_MULTITASK_EDITS = {
    "task_fix_add": lambda root: _replace_text(root / "app.py", "left - right", "left + right"),
    "task_fix_shout": lambda root: _replace_text(root / "text.py", "text.lower()", "text.upper()"),
    "task_add_total": lambda root: (root / "app.py").write_text(
        (root / "app.py").read_text(encoding="utf-8")
        + "\n\ndef total(values: list[int]) -> int:\n    result = 0\n    for value in values:\n"
        "        result = add(result, value)\n    return result\n",
        encoding="utf-8",
    ),
}

# project-e2e-context fixture의 올바른 구현이다. 규칙 값은 규칙 모듈 상수를 쓴다.
_SHIPPING_FEE_IMPLEMENTATION = (
    "from shipping_rules import BASE_FEE_WON, FREE_SHIPPING_MIN_WON, PER_KG_WON\n\n\n"
    "def shipping_fee(weight_kg: int, order_total_won: int) -> int:\n"
    "    if weight_kg <= 0:\n"
    "        raise ValueError(\"무게는 0보다 커야 합니다.\")\n"
    "    if order_total_won >= FREE_SHIPPING_MIN_WON:\n"
    "        return 0\n"
    "    return BASE_FEE_WON + weight_kg * PER_KG_WON\n"
)


class _TaskEditingFakeRuntime(_RequestEchoFakeRuntime):
    """Worker prompt의 TaskContract task_ref로 Task별 편집을 골라 적용하고 바로 완료한다."""

    def __init__(self, inventory, edits) -> None:
        super().__init__(inventory)
        self.edits = edits

    def start_turn(self, **arguments):
        receipt = super().start_turn(**arguments)
        for ref, edit in self.edits.items():
            if f'"task_ref":"{ref}"' in arguments["prompt"]:
                edit()
        self.complete(arguments["thread_id"], response="완료")
        return receipt


_TURN_USAGE = {
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
}


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


def _single_task_role_responses(*, mutation_policy: str, kind: str) -> dict[str, list[dict]]:
    """E2E-15 결정적 준비용 단일 Task 역할 응답이다. 의미 판단이 아니라 형식 채움이다."""

    from tests.test_engine_user_facade import _responses

    base = _responses()
    skeleton = base["skeleton_generator"][0]["candidates"][0]
    expander = base["plan_expander"][0]
    return {
        "goal_normalizer": [dict(base["goal_normalizer"][0], mutation_policy=mutation_policy)],
        "goal_reviewer": base["goal_reviewer"],
        "skeleton_generator": [{"candidates": [dict(
            skeleton,
            tasks=[dict(skeleton["tasks"][0], kind=kind)],
            dependencies=[],
            goal_coverage=[{"criterion_id": "ac_001", "task_refs": ["task_one"]}],
            estimated_change_cost=1,
        )]}],
        "skeleton_reviewer": base["skeleton_reviewer"],
        "plan_expander": [dict(
            expander,
            tasks=[dict(expander["tasks"][0], kind=kind)],
            dependencies=[],
            goal_coverage=[{
                "criterion_id": "ac_001",
                "task_refs": ["task_one"],
                "validation_ids": ["validation_one", "validation_goal"],
            }],
        )],
        "compact_plan_reviewer": base["compact_plan_reviewer"],
    }


class _ReadyTaskScriptedRunner(InspectionScriptedRunner):
    """실행 준비 응답의 Task binding만 요청의 ready Task로 채우는 결정적 fixture."""

    def run(self, request, *, validator=None):
        pending = self.responses.get(request.role)
        if request.role == "execution_preparation" and pending:
            pending[0]["proposal"]["task_id"] = request.payload["ready_task"]["task_id"]
        return super().run(request, validator=validator)


def _scope_runner(workspace: Path, *, r2_mutation: str = "scoped_change") -> _ReadyTaskScriptedRunner:
    """A r1·B·A r2 순서의 준비 응답과 A r1 실행 준비 응답이다(E2E-15 결정적 driver용)."""

    groups = (
        _single_task_role_responses(mutation_policy="read_only", kind="inspect"),
        _single_task_role_responses(mutation_policy="read_only", kind="inspect"),
        _single_task_role_responses(
            mutation_policy=r2_mutation,
            kind="inspect" if r2_mutation == "read_only" else "change",
        ),
    )
    responses = {role: [item for group in groups for item in group[role]] for role in groups[0]}
    # fixture의 add는 일부러 틀려 있다. 결정적 검사는 파일을 바꾸지 않는 import만 확인한다.
    check = (sys.executable, "-c", "import app")
    proposal = ExecutionSpecProposal(
        task_id="task_" + "0" * 32,
        context_needs=(
            ExecutionContextNeed(need_id="source_app", description="분석 대상 구현", path_hints=("app.py",)),
            ExecutionContextNeed(need_id="test_app", description="기대 동작 테스트", path_hints=("test_app.py",)),
        ),
        resolved_targets=(
            ResolvedTarget(target_ref="target_app", path="app.py", access="read"),
            ResolvedTarget(target_ref="target_test", path="test_app.py", access="read"),
        ),
        actions=(ExecutionAction(
            action_ref="action_inspect", kind="inspect", description="app.py와 test_app.py를 읽어 비교한다.",
        ),),
        validation_steps=(ValidationExecutionStep(
            validation_id="validation_one", method="deterministic", argv=check,
            working_directory=str(workspace), timeout_seconds=60, expected_exit_codes=(0,),
            required_evidence_kinds=("test",),
        ),),
        timeout_seconds=900,
        idempotency_hint="scope-expansion-r1",
    ).model_dump(mode="json")
    for item in proposal["validation_steps"]:
        item.pop("method")
        item.pop("required_evidence_kinds")
    responses["execution_preparation"] = [{"proposal": proposal, "context_request": None}]
    responses["goal_test_preparation"] = [{"step": ValidationExecutionStep(
        validation_id="validation_goal", method="deterministic", argv=check,
        working_directory=str(workspace), timeout_seconds=60, expected_exit_codes=(0,),
        required_evidence_kinds=("test",),
    ).model_dump(mode="json")}]
    return _ReadyTaskScriptedRunner(responses)


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

    def _enter_project_e2e_fakes(
        self, stack, *, binding, prepare, inventory=None, runtime=None
    ) -> None:
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
             {"side_effect": lambda **_: nullcontext(
                 runtime or FakeCodexRuntime(runtime_inventory)
             )}),
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
            cell = kwargs["state_root"].parent.name
            responses = _responses()
            responses["goal_normalizer"][0]["prohibited_effects"] = ["원격 저장소에 변경을 push한다."]
            requested[cell] = {
                "source_request": kwargs["source_request"],
                "authorize": kwargs.get("authorize", True),
            }
            # E2E-15 driver는 같은 runner로 두 번째 project 준비·r1 실행·r2 revise까지 돈다.
            runner = (
                _scope_runner(kwargs["workspace"])
                if cell == "scope-expansion"
                else InspectionScriptedRunner(responses)
            )
            return _prepare_from_raw_request(**kwargs, structured_runner=runner)

        policies = load_evaluation_policies(
            budget_policy_path=ROOT / "config" / "pre-1.0-validation-budget.json",
            role_timeout_policy_path=ROOT / "config" / "pre-1.0-role-timeouts.json",
        )
        with tempfile.TemporaryDirectory() as raw, ExitStack() as stack:
            destination = Path(raw) / "e2e-run"
            freeze = Path(raw) / "freeze"
            freeze.mkdir()
            self._enter_project_e2e_fakes(
                stack, binding=binding, prepare=scripted, inventory=inventory(),
                runtime=_CompletingFakeRuntime(inventory()),
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
                "scope-expansion": {"source_request": _SCOPE_EXPANSION_REQUEST, "authorize": False},
            },
            requested,
        )
        prohibited = cells["prohibited-effect"]
        self.assertTrue(prohibited["passed"], prohibited)
        self.assertEqual(["live", "fake"], prohibited["qualification_outcome"]["provenance"])
        # E2E-15 결정적 실행은 세 축을 모두 관측해 cell은 통과하지만 출처는 fake다.
        scope = cells["scope-expansion"]
        self.assertTrue(scope["passed"], scope)
        self.assertEqual({"policy": True, "target": True, "effect": True}, scope["coverage"]["axis_passed"])
        self.assertEqual([], scope["coverage"]["covered_live"])
        self.assertEqual(
            QualificationCellStatus.PASSED.value, scope["qualification_outcome"]["status"]
        )
        self.assertEqual(["fake"], scope["qualification_outcome"]["provenance"])
        # 두 cell 모두 활성화 receipt digest가 있고 E2E-15 receipt는 이 계약·fixture에 결속된다.
        for item in cells.values():
            self.assertIsNotNone(item["qualification_plan_activation"]["activation_receipt_digest"])
        self.assertEqual(contract.contract_digest, receipt["evaluation_contract_digest"])
        self.assertEqual(contract.fixture_digests[1], receipt["fixture_digest"])
        # live만 허용하는 E2E-15 책임은 fake 출처로 PASS가 되지 않는다.
        for code in ("PROVENANCE_NOT_ALLOWED", "REQUIRED_PROVENANCE_MISSING"):
            self.assertIn(f"{code}:E2E-15:scope-expansion", report.failures)
        self.assertFalse(any(item.startswith("PRODUCT_FAILURE:E2E-15") for item in report.failures))
        self.assertFalse(any(
            item.startswith("PRODUCT_FAILURE:E2E-14") or item.endswith(":E2E-14:prohibited-effect")
            for item in report.failures
        ))
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

    def test_fixture_source_digest_covers_every_scenario_fixture(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            fixtures = base / "tests" / "fixtures" / "engine"
            for name in (
                "project-e2e", "project-e2e-read-only", "project-e2e-multi-task", "project-e2e-context",
            ):
                shutil.copytree(ROOT / "tests" / "fixtures" / "engine" / name, fixtures / name)
            original = _project_e2e_fixture_source_digest(base)
            self.assertEqual(original, _project_e2e_fixture_source_digest(ROOT))
            (fixtures / "project-e2e-read-only" / "app.py").write_text("changed\n", encoding="utf-8")
            self.assertNotEqual(original, _project_e2e_fixture_source_digest(base))

    def _read_only_cell(self, raw: str, *, side_effect=None, read_only: bool = True):
        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root, "project-e2e-read-only")
        # run loop와 같이 prepare 앞에서 기준 commit을 만든다. .git은 트리 digest 밖이다.
        head = _initialize_workspace_git(workspace)["workspace_git_head"]
        self.assertRegex(head, r"^[0-9a-f]{40}$")
        prepared = _prepare(
            workspace=workspace,
            state_root=cell_root / "state",
            inventory=self.inventory,
            roles=self.roles,
            read_only=read_only,
        )
        runtime = RecordedRuntime(
            _CompletingFakeRuntime(self.inventory, side_effect=side_effect),
            journal=cell_root / "runtime-receipts.json",
        )
        result = _read_only_report(
            prepared, runtime, source_digest, cell_root=cell_root, governance=ALLOW_ALL
        )
        report = json.loads((cell_root / "final-report.json").read_text(encoding="utf-8"))
        return prepared, result, report

    def test_read_only_report_completes_without_any_workspace_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, result, report = self._read_only_cell(raw)

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertEqual([], result["source_digest"]["changed_paths"])
            self.assertEqual(3, result["source_digest"]["file_count_before"])
            self.assertEqual(1, result["goal_verdict_count"])
            self.assertIsNone(report["reason"])
            verification = report["final_report"]["read_only_verification"]
            self.assertTrue(verification["criteria_complete"])
            self.assertTrue(verification["evidence_grounded"])
            self.assertTrue(verification["source_unchanged"])
            self.assertIn("후속 과제", result["known_limitation"])

    def test_read_only_report_fails_on_new_file_that_product_check_misses(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw) / "cell" / "workspace"

            def create_file() -> None:
                (workspace / "notes.txt").write_text("분석 메모\n", encoding="utf-8")

            _prepared, result, _report = self._read_only_cell(raw, side_effect=create_file)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E03_WORKSPACE_TREE_CHANGED", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertEqual(["notes.txt"], result["source_digest"]["changed_paths"])
            # 제품 source_unchanged는 baseline 밖 새 파일을 못 본다(known limitation 회귀 표식).
            self.assertTrue(result["checks"]["product_source_unchanged"])
            self.assertFalse(result["checks"]["workspace_tree_unchanged"])

    def test_read_only_report_does_not_run_when_goal_is_not_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, result, report = self._read_only_cell(raw, read_only=False)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E03_PRECONDITION_NOT_READ_ONLY", result["failure_code"])
            self.assertEqual("model", result["failure_class"])
            self.assertEqual(["task_fix_add"], result["precondition"]["change_task_refs"])
            # 실패 경로에서도 책임 판정용 final report 파일은 null과 이유로 남는다.
            self.assertIsNone(report["final_report"])
            self.assertEqual("E2E03_FINAL_REPORT_NOT_PRODUCED", report["reason"])
            with prepared.service.ledger.read() as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def _usage_cell(self, raw: str, *, provider_payload=_TURN_USAGE):
        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root)
        prepared = _prepare(
            workspace=workspace,
            state_root=cell_root / "state",
            inventory=self.inventory,
            roles=self.roles,
        )

        def fix_add() -> None:
            (workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n    return left + right\n",
                encoding="utf-8",
            )

        runtime = RecordedRuntime(
            _CompletingFakeRuntime(
                self.inventory, side_effect=fix_add, provider_payload=provider_payload
            ),
            journal=cell_root / "runtime-receipts.json",
        )
        result = _usage_missing_late(
            prepared, runtime, source_digest, cell_root=cell_root, governance=ALLOW_ALL
        )
        document = json.loads((cell_root / "usage-observation.json").read_text(encoding="utf-8"))
        return cell_root, prepared, result, document

    def test_usage_missing_completes_and_late_usage_only_appends_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, result, document = self._usage_cell(raw)

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["missing_checks"].values()), result["missing_checks"])
            self.assertTrue(all(result["late_checks"].values()), result["late_checks"])
            self.assertEqual(1, result["goal_verdict_count"])
            before = document["before_late"]
            after = document["after_late"]
            # 누락 동안에는 0이나 추정이 아니라 null/unknown이다.
            self.assertEqual(
                (None, None, None, None, None),
                tuple(
                    before["usage_record"][key]
                    for key in (
                        "input_tokens", "cached_input_tokens", "output_tokens",
                        "reasoning_tokens", "total_tokens",
                    )
                ),
            )
            self.assertEqual(
                UsageWithholdingRuntime.WITHHELD_USAGE_SOURCE, before["usage_record"]["usage_source"]
            )
            self.assertEqual(("usage_unknown", None), (
                before["provider_call"]["status"], before["provider_call"]["actual_tokens"]
            ))
            # 늦은 usage는 회계 관측만 더한다. budget_usage 기록과 실행 필드는 그대로다.
            self.assertEqual(("settled", 18), (
                after["provider_call"]["status"], after["provider_call"]["actual_tokens"]
            ))
            self.assertEqual(before["usage_record"], after["usage_record"])
            self.assertEqual(
                [("unavailable", 0), ("measured", 1)],
                [(item["measurement_status"], item["late"]) for item in after["usage_observations"]],
            )
            self.assertEqual(["history_events"], list(document["ledger_delta"]))
            fault = json.loads((cell_root / "fault-injection.json").read_text(encoding="utf-8"))
            self.assertEqual("worker_usage_withheld_then_late_delivery", fault["kind"])
            self.assertTrue(fault["withheld"])
            self.assertTrue(all("provider_payload" not in item for item in fault["withheld"]))
            self.assertEqual("completed", prepared.service.status(prepared.project_id)["project"]["run_state"])

    def test_usage_missing_fails_when_missing_usage_is_filled_with_zero(self) -> None:
        def zero_filled(self, payload):
            return payload | {
                "usage": {
                    "inputTokens": 0,
                    "cachedInputTokens": 0,
                    "outputTokens": 0,
                    "reasoningOutputTokens": 0,
                    "totalTokens": 0,
                },
                "usage_scope": "turn",
            }

        with tempfile.TemporaryDirectory() as raw, patch.object(
            UsageWithholdingRuntime, "_withheld_payload", zero_filled
        ):
            _cell_root, _prepared, result, document = self._usage_cell(raw)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E06_MISSING_USAGE_NOT_NULL", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertFalse(result["missing_checks"]["usage_components_null"])
            self.assertEqual("E2E06_MISSING_USAGE_NOT_NULL", document["reason"])
            # 전제가 깨지면 늦은 usage를 전달하지 않는다.
            self.assertIsNone(document["after_late"])
            self.assertEqual(1, len(document["before_late"]["usage_observations"]))

    def test_usage_missing_without_provider_usage_is_environment_not_product(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _cell_root, _prepared, result, document = self._usage_cell(raw, provider_payload={})

            self.assertFalse(result["passed"])
            self.assertEqual("E2E06_FAULT_NOT_TRIGGERED", result["failure_code"])
            self.assertEqual("environment", result["failure_class"])
            self.assertEqual("E2E06_USAGE_OBSERVATION_NOT_PRODUCED", document["reason"])

    def _multitask_cell(self, raw: str):
        from tests.fixtures.engine.governance import multitask

        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace = multitask.copy_fixture(cell_root)
        state = multitask.prepare(
            workspace=workspace, state_root=cell_root / "state",
            inventory=self.inventory, roles=self.roles,
        )
        with state.service.ledger.read() as connection:
            plan = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT p.payload_json FROM projects j JOIN plan_revisions p "
                "ON p.id=j.active_plan_revision_id WHERE j.id=?",
                (state.project_id,),
            ).fetchone()[0])
        prepared = PreparedE2E(
            service=state.service, project_id=state.project_id,
            task_id=state.task_ids["task_fix_add"], plan_revision_id=plan.plan_revision_id,
            activation_digest=plan.activation_digest, proposal=None, workspace=workspace,
        )
        # 준비 역할 응답은 provider 형식이다. 대상 digest는 준비 시점 Project Map에서 Core가 채운다.
        preparations = []
        for step in multitask.STEPS:
            test = f"{step.test}.py"
            proposal = ExecutionSpecProposal(
                task_id=state.task_ids[step.ref],
                context_needs=(
                    ExecutionContextNeed(need_id="source", description="수정 대상 구현", path_hints=(step.write,)),
                    ExecutionContextNeed(need_id="test", description="고정 회귀 테스트", path_hints=(test,)),
                ),
                resolved_targets=(
                    ResolvedTarget(target_ref="target_source", path=step.write, access="write"),
                    ResolvedTarget(target_ref="target_test", path=test, access="read"),
                ),
                actions=(ExecutionAction(action_ref="action_edit", kind="edit", description=step.objective),),
                validation_steps=(ValidationExecutionStep(
                    validation_id=f"validation_{step.test}", method="deterministic",
                    argv=(sys.executable, "-m", "unittest", step.test),
                    working_directory=str(workspace), timeout_seconds=60, expected_exit_codes=(0,),
                    required_evidence_kinds=("test",)),),
                resource_locks=(f"file:{workspace / step.write}",),
                timeout_seconds=900, idempotency_hint=f"multitask-{step.ref}",
            ).model_dump(mode="json")
            for item in proposal["validation_steps"]:
                item.pop("method")
                item.pop("required_evidence_kinds")
            preparations.append({"proposal": proposal, "context_request": None})
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": preparations,
            "goal_test_preparation": [{"step": multitask.goal_step(state).model_dump(mode="json")}],
        })
        runtime = RecordedRuntime(
            _TaskEditingFakeRuntime(self.inventory, {
                ref: (lambda edit=edit: edit(workspace)) for ref, edit in _MULTITASK_EDITS.items()
            }),
            journal=cell_root / "runtime-receipts.json",
        )
        return prepared, runtime, runner

    def test_multi_task_dag_runs_dependent_tasks_one_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, runtime, runner = self._multitask_cell(raw)

            result = _multi_task_dag(
                prepared, runtime, "sha256:" + "0" * 64, roles=self.roles,
                governance=ALLOW_ALL, structured_runner=runner, timeout_seconds=60,
            )

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertEqual(1, result["max_in_flight"])
            self.assertTrue(result["edges"])
            self.assertTrue(all(item["respected"] for item in result["edges"]))
            # 제품 supervisor 경로로 돈다. 준비 역할은 Task마다 한 번, Goal Test 준비는 한 번이다.
            self.assertEqual("EngineApplication.run_once+RuntimeJobSupervisor", result["runtime_path"])
            self.assertEqual(
                ["execution_preparation"] * 3 + ["goal_test_preparation"],
                [call.role for call in runner.calls],
            )
            completed = [
                item["task_ref"] for item in result["execution_order"]
                if item["event_type"] == "task.completed"
            ]
            self.assertEqual(["task_fix_add", "task_fix_shout", "task_add_total"], completed)

    def test_multi_task_dag_single_task_plan_fails_without_running(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root = Path(raw) / "cell"
            cell_root.mkdir()
            workspace, source_digest = _copy_fixture(ROOT, cell_root)
            prepared = _prepare(
                workspace=workspace, state_root=cell_root / "state",
                inventory=self.inventory, roles=self.roles,
            )
            runtime = RecordedRuntime(
                _CompletingFakeRuntime(self.inventory), journal=cell_root / "runtime-receipts.json"
            )

            result = _multi_task_dag(prepared, runtime, source_digest, governance=ALLOW_ALL)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E02_PLAN_NOT_MULTI_TASK_DAG", result["failure_code"])
            self.assertEqual("model", result["failure_class"])
            self.assertEqual(["task_fix_add"], result["precondition"]["task_refs"])
            self.assertEqual(0, runtime.create_calls)
            with prepared.service.ledger.read() as connection:
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0])

    def _repair_cell(self, raw: str):
        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root)
        prepared = _prepare(
            workspace=workspace, state_root=cell_root / "state",
            inventory=self.inventory, roles=self.roles,
        )

        def fix_add() -> None:
            (workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n    return left + right\n",
                encoding="utf-8",
            )

        runtime = RecordedRuntime(
            _CompletingFakeRuntime(self.inventory, side_effect=fix_add),
            journal=cell_root / "runtime-receipts.json",
        )
        result = _approved_repair(
            prepared, runtime, source_digest, cell_root=cell_root,
            governance=ALLOW_ALL, timeout_seconds=60,
        )
        fault = json.loads((cell_root / "fault-injection.json").read_text(encoding="utf-8"))
        return prepared, result, fault

    def _execution_attempts(self, prepared) -> list[tuple[int, str]]:
        with prepared.service.ledger.read() as connection:
            return [
                (row["attempt_no"], row["status"])
                for row in connection.execute(
                    "SELECT attempt_no,status FROM attempts WHERE kind='execution' ORDER BY attempt_no"
                )
            ]

    def test_approved_repair_classifies_injected_fault_and_repairs_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, result, fault = self._repair_cell(raw)

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertEqual([(1, "succeeded"), (2, "succeeded")], self._execution_attempts(prepared))
            chain = result["repair_chain"]
            self.assertEqual(1, len(chain["retries"]))
            self.assertEqual(["implementation"], [item["failure_class"] for item in chain["assessments"]])
            self.assertTrue(chain["direct_failure_evidence_ids"])
            # fault 기록은 provenance일 뿐 분류기 입력이 아니며 주입기가 되돌리지 않는다.
            injection = fault["injection"]
            self.assertTrue(injection["injected"] and injection["effective"])
            self.assertEqual(["app.py"], [item["path"] for item in injection["targets"]])
            target = injection["targets"][0]
            self.assertEqual(target["expected_content_digest"], target["injected_digest"])
            self.assertNotEqual(target["worker_output_digest"], target["injected_digest"])
            self.assertFalse(fault["classifier_input"])
            self.assertFalse(fault["injector_rollback"])
            self.assertEqual(chain["attempt_id"], injection["attempt_id"])
            # 최종 파일은 두 번째 Worker가 고친 구현이다.
            self.assertEqual(
                target["worker_output_digest"], fault["final_target_digests"]["app.py"]
            )

    def test_approved_repair_without_retryable_implementation_fails_before_running(self) -> None:
        def context_only(**_arguments):
            return RecoveryEnvelope(retryable_failure_classes=("context",))

        with tempfile.TemporaryDirectory() as raw, patch(
            "flowmarshal.engine.e2e_qualification.RecoveryEnvelope", context_only
        ):
            prepared, result, fault = self._repair_cell(raw)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E04_PRECONDITION_IMPLEMENTATION_NOT_RETRYABLE", result["failure_code"])
            self.assertEqual("model", result["failure_class"])
            self.assertEqual([], self._execution_attempts(prepared))
            self.assertIsNone(fault["injection"])
            self.assertEqual("E2E04_FAULT_NOT_INJECTED", fault["reason"])

    def test_approved_repair_unclassified_failure_stops_without_repair(self) -> None:
        from flowmarshal.engine.recovery import EvidenceFirstFailureClassifier, FailureDiagnosis

        def unclassified(cls, signal):
            return FailureDiagnosis(
                evidence_ids=signal.evidence_ids, rationale="직접 근거 없음", source="unclassified"
            )

        with tempfile.TemporaryDirectory() as raw, patch.object(
            EvidenceFirstFailureClassifier, "classify", classmethod(unclassified)
        ):
            prepared, result, fault = self._repair_cell(raw)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E04_RUN_BLOCKED", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertIn("TASK_VALIDATION_RECOVERY_REQUIRED", result["failure"])
            self.assertTrue(fault["injection"]["effective"])
            self.assertEqual([], result["repair_chain"]["assessments"])
            self.assertEqual([(1, "succeeded")], self._execution_attempts(prepared))

    def _context_cell(self, raw: str, *, request_context: bool, unmapped_need: bool = False):
        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root, "project-e2e-context")
        prepared = _prepare(
            workspace=workspace, state_root=cell_root / "state",
            inventory=self.inventory, roles=self.roles,
        )
        proposal = prepared.proposal.model_dump(mode="json")
        for item in proposal["validation_steps"]:
            item.pop("method")
            item.pop("required_evidence_kinds")
        need = {
            "need_id": "shipping_rules",
            "description": "app.py가 가져오는 배송비 규칙 모듈 본문",
            "path_hints": ["shipping_rules.py"],
            "symbol_hints": [],
            "tag_hints": [],
            "required": True,
        }
        context_request = {
            "proposal": None,
            "context_request": {
                "task_id": prepared.task_id,
                "missing_needs": [need],
                "reason": "규칙 모듈이 초기 Project Map에 없습니다.",
            },
        }
        # ContextRequest 대신 proposal의 context_needs에 Map 밖 경로를 바로 적은 경우.
        unmapped = {
            "proposal": {**proposal, "context_needs": [*proposal["context_needs"], need]},
            "context_request": None,
        }
        runner = ScriptedStructuredRoleRunner({
            "execution_preparation": (
                [context_request] if request_context else []
            ) + ([unmapped] if unmapped_need else []) + [
                {"proposal": proposal, "context_request": None}
            ],
            "goal_test_preparation": [{"step": prepared.proposal.validation_steps[0].model_copy(
                update={"validation_id": "validation_goal"}).model_dump(mode="json")}],
        })

        def implement() -> None:
            (workspace / "app.py").write_text(_SHIPPING_FEE_IMPLEMENTATION, encoding="utf-8")

        runtime = RecordedRuntime(
            _CompletingFakeRuntime(self.inventory, side_effect=implement),
            journal=cell_root / "runtime-receipts.json",
        )
        result = _context_discovery(
            prepared, runtime, source_digest, cell_root=cell_root, roles=self.roles,
            governance=ALLOW_ALL, structured_runner=runner, timeout_seconds=60,
        )
        document = json.loads((cell_root / "context-discovery.json").read_text(encoding="utf-8"))
        return prepared, result, document, runner

    def test_context_discovery_resolves_unmapped_rule_module_through_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prepared, result, document, runner = self._context_cell(raw, request_context=True)

            self.assertTrue(result["passed"], result)
            self.assertTrue(all(result["checks"].values()), result["checks"])
            self.assertNotIn("shipping_rules.py", document["initial_project_map_paths"])
            [job] = document["context_jobs"]
            self.assertTrue(all(job["checks"].values()), job["checks"])
            self.assertEqual("provider_context_request", job["request_origin"])
            self.assertEqual(["shipping_rules.py"], [item["source_ref"] for item in job["resolution"]])
            digest = sha256_bytes((prepared.workspace / "shipping_rules.py").read_bytes())
            self.assertEqual(digest, job["resolution"][0]["content_digest"])
            self.assertEqual(digest, job["prompt_bindings"][0]["fragment_content_digest"])
            self.assertTrue(job["prompt_bindings"][0]["body_bound"])
            self.assertIsNone(document["reason"])
            # 첫 준비 job과 후속 job이 각각 역할 turn 하나를 쓴다.
            self.assertEqual(
                ["execution_preparation", "execution_preparation", "goal_test_preparation"],
                [call.role for call in runner.calls],
            )

    def test_context_discovery_resolves_unmapped_context_need_in_proposal(self) -> None:
        """준비 역할이 ContextRequest 없이 Map 밖 경로를 context_needs에 적어도 같은 후속 job으로 해소한다."""

        with tempfile.TemporaryDirectory() as raw:
            _prepared, result, document, runner = self._context_cell(
                raw, request_context=False, unmapped_need=True
            )

            self.assertTrue(result["passed"], result)
            [job] = document["context_jobs"]
            self.assertTrue(all(job["checks"].values()), job["checks"])
            self.assertEqual("provider_proposal_context_needs", job["request_origin"])
            self.assertEqual(["shipping_rules.py"], [item["source_ref"] for item in job["resolution"]])
            self.assertEqual(
                ["execution_preparation", "execution_preparation", "goal_test_preparation"],
                [call.role for call in runner.calls],
            )

    def test_context_discovery_without_context_request_is_not_observed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _prepared, result, document, _runner = self._context_cell(raw, request_context=False)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E05_CONTEXT_REQUEST_NOT_OBSERVED", result["failure_code"])
            self.assertEqual("model", result["failure_class"])
            self.assertEqual([], document["context_jobs"])
            self.assertEqual("E2E05_CONTEXT_REQUEST_NOT_OBSERVED", document["reason"])

    def test_new_drivers_require_raw_request_pipeline_stages(self) -> None:
        from flowmarshal.engine.qualification_manifest import _ordered_contains

        suite = QualificationSuiteManifest.load(ROOT / "config" / "qualification-suite.json")
        requirements = {item.fixture_id: item for item in suite.e2e_responsibilities}
        digest = "sha256:" + "b" * 64
        with tempfile.TemporaryDirectory() as raw:
            for scenario in ("multi-task-dag", "approved-repair", "context-discovery"):
                requirement = requirements[scenario]
                required = tuple(requirement.required_pipeline_stages)
                raw_stages = required[: required.index("plan_activation") + 1]
                cell_root = Path(raw) / scenario
                (cell_root / "state").mkdir(parents=True)
                for name in (
                    "state/flowmarshal-engine.sqlite3", "runtime-receipts.json",
                    "context-discovery.json",
                ):
                    (cell_root / name).write_text("{}", encoding="utf-8")
                # 합성 준비(pipeline_stages=())로는 책임 단계를 채울 수 없다.
                for stages, expected in (((), False), (raw_stages, True)):
                    outcome = _responsibility_outcome(
                        scenario=scenario,
                        prepared=SimpleNamespace(pipeline_stages=stages),
                        cell={"passed": True},
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
                    self.assertEqual((requirement.responsibility_id,), outcome.responsibility_ids)
                    self.assertEqual(expected, _ordered_contains(outcome.pipeline_stages, required))

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

    def _scope_cell(self, raw: str, *, r2_mutation: str = "scoped_change"):
        from tests.engine_helpers import inventory
        from tests.test_engine_user_facade import _roles

        cell_root = Path(raw) / "cell"
        cell_root.mkdir()
        workspace, source_digest = _copy_fixture(ROOT, cell_root)
        runner = _scope_runner(workspace, r2_mutation=r2_mutation)
        runtime = RecordedRuntime(
            _CompletingFakeRuntime(inventory()), journal=cell_root / "runtime-receipts.json"
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
            source_request=_SCOPE_EXPANSION_REQUEST,
            structured_runner=runner,
            authorize=False,
        )
        return cell_root, prepared, runtime, source_digest, runner

    def _run_scope(self, cell_root, prepared, runtime, source_digest):
        from tests.test_engine_user_facade import _roles

        return _scope_expansion_blocked(
            prepared,
            runtime,
            source_digest,
            cell_root=cell_root,
            roles=_roles(),
            evaluation_contract_digest=sha256_digest({"contract": "authority"}),
            fixture_digest=sha256_digest({"fixture": "scope-expansion"}),
            source_root=ROOT,
            governance=ALLOW_ALL,
            timeout_seconds=60,
        )

    def test_scope_expansion_observes_policy_target_and_effect_axes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, runner = self._scope_cell(raw)
            self.assertEqual("plan_reviewer", prepared.pipeline_stages[-1])
            self.assertFalse((cell_root / "goal-authorization.json").exists())
            goal_id = prepared.service.load_active_goal(prepared.project_id).goal_id

            result = self._run_scope(cell_root, prepared, runtime, source_digest)

            self.assertTrue(result["passed"], result)
            self.assertIsNone(result["failure"])
            self.assertEqual([], result["axis_failures"])
            # scripted·fake 준비 위의 결정적 실행은 어떤 축도 live로 덮었다고 적지 않는다.
            self.assertEqual("fake", result["provenance"])
            coverage = result["coverage"]
            self.assertEqual({"policy": True, "target": True, "effect": True}, coverage["axis_passed"])
            self.assertEqual([], coverage["covered_live"])
            self.assertEqual(["effect", "policy", "target"], coverage["not_covered_live"])
            steps = {item["step"]: item for item in result["steps"]}
            budget = [["policy", f"budget.effective.{goal_id}"]]
            self.assertEqual(
                {
                    "A_narrow_authorization": [["policy", "planning_budget.max_logical_role_calls"]],
                    "B_internal_plan_id_activation": [["goal", "authorization"]],
                    "C_user_authorization": None,
                    "D_budget_expansion_run_once": budget,
                    "E_selected_plan_reactivation": budget,
                    "F_user_reauthorization": None,
                    "E0_revise_with_active_plan": None,
                    "T1_authorization_target_binding": None,
                    "T2_target_b_selected_plan_activation": [["goal", "authorization"]],
                    "T3a_consumed_authority_reuse": None,
                    "T3b_displayed_target_a_for_b": None,
                    "T4_target_b_run_once": None,
                    "T5_target_b_user_authorization": None,
                    "E3_existing_authorization_activation": [
                        ["effect", "effect_policy"], ["goal", "goal_contract_digest"],
                    ],
                    "E4_run_once_after_revision": None,
                    "E5_user_reauthorization_for_revision": None,
                },
                {name: item["authorization_changes"] for name, item in steps.items()},
            )
            # 거절은 typed 예외 class·code로 남고 CoreCapabilityError도 cell을 멈추지 않는다.
            self.assertEqual(
                {
                    "A_narrow_authorization": ("GoalAuthorizationRequired", "GOAL_AUTHORIZATION_REQUIRED"),
                    "B_internal_plan_id_activation": ("GoalAuthorizationRequired", "GOAL_AUTHORIZATION_REQUIRED"),
                    "D_budget_expansion_run_once": (None, "GOAL_AUTHORIZATION_REQUIRED"),
                    "E_selected_plan_reactivation": (None, "GOAL_AUTHORIZATION_REQUIRED"),
                    "E0_revise_with_active_plan": ("EngineApplicationError", "GOAL_REVISION_ACTIVE_PLAN"),
                    "T2_target_b_selected_plan_activation": ("GoalAuthorizationRequired", "GOAL_AUTHORIZATION_REQUIRED"),
                    "T3a_consumed_authority_reuse": ("CoreCapabilityError", "CORE_CAPABILITY_DENIED"),
                    "T3b_displayed_target_a_for_b": ("CoreCapabilityError", "CORE_CAPABILITY_DENIED"),
                    "E3_existing_authorization_activation": ("GoalAuthorizationRequired", "GOAL_AUTHORIZATION_REQUIRED"),
                },
                {
                    name: (item["error_class"], item["error_code"])
                    for name, item in steps.items()
                    if item["error_code"] is not None
                },
            )
            self.assertEqual({"create_thread": 0, "start_turn": 0, "resume": 0}, result["effect_count"])

            scope = json.loads((cell_root / "scope-check.json").read_text(encoding="utf-8"))
            self.assertEqual("flowmarshal.project-e2e.scope-check.v3", scope["schema"])
            self.assertEqual("fake", scope["provenance"])
            self.assertFalse(scope["cost_estimate"]["authorization_has_plan_or_cost_field"])
            self.assertIn("history_events", scope["measured_tables"])
            root_a = str((cell_root / "workspace").resolve())
            root_b = str((cell_root / "workspace-b").resolve())
            self.assertEqual(
                {"A": root_a, "B": root_b},
                {key: item["root"] for key, item in scope["projects"].items()},
            )
            project_b = scope["projects"]["B"]["project_id"]
            detailed = {item["step"]: item for item in scope["steps"]}
            # 모든 probe 단계는 provider 호출·실행 표·runtime create/start/resume이 0이다.
            for name, item in detailed.items():
                self.assertTrue(item["zero_effect"], name)
                self.assertEqual({"create_thread": 0, "start_turn": 0, "resume": 0}, item["effect_count"], name)
            approvals = {
                "C_user_authorization", "F_user_reauthorization",
                "T5_target_b_user_authorization", "E5_user_reauthorization_for_revision",
            }
            for name, item in detailed.items():
                if name not in approvals:
                    self.assertEqual(
                        {key: {} for key in item["ledger_delta"]}, item["ledger_delta"], name
                    )
            binding = detailed["T1_authorization_target_binding"]["binding"]
            self.assertEqual(root_a, binding["project_root"])
            self.assertEqual(root_a, str(Path(binding["authorization_project_root"]).resolve()))
            self.assertEqual(root_a, str(Path(binding["plan_project_map_root"]).resolve()))
            self.assertEqual(root_b, str(Path(binding["target_b_project_root"]).resolve()))
            self.assertLessEqual(
                {"project_id", "project_root"},
                set(detailed["T3b_displayed_target_a_for_b"]["target_field_differences"]),
            )
            self.assertEqual(
                prepared.project_id,
                detailed["T3b_displayed_target_a_for_b"]["displayed_target"]["project_id"],
            )
            effect = scope["axes"]["effect"]
            self.assertTrue(effect["baseline"]["passed"], effect["baseline"])
            self.assertEqual("read_only", effect["approved_effect_policy"]["mutation_policy"])
            self.assertEqual("scoped_change", effect["revision"]["requested_effect_policy"]["mutation_policy"])
            self.assertTrue(effect["revision"]["same_goal"])
            self.assertEqual(2, effect["revision"]["revision_no"])
            self.assertEqual("ready", detailed["E3_existing_authorization_activation"]["candidate_plan_status"])
            p2 = effect["revision"]["plan_revision_id"]

            observation = json.loads(
                (cell_root / "authorization-observation.json").read_text(encoding="utf-8")
            )
            self.assertEqual("flowmarshal.project-e2e.authorization-observation.v3", observation["schema"])
            self.assertEqual(
                [
                    ("C_user_authorization", "A", root_a),
                    ("F_user_reauthorization", "A", root_a),
                    ("T5_target_b_user_authorization", "B", root_b),
                    ("E5_user_reauthorization_for_revision", "A", root_a),
                ],
                [
                    (item["step"], item["project"], str(Path(item["project_root"]).resolve()))
                    for item in observation["authorizations"]
                ],
            )
            receipt = json.loads(
                (cell_root / "plan-activation-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(prepared.plan_revision_id, receipt["plan_revision_id"])
            # 준비 역할은 A r1·B·A r2 세 번만 돈다. E0 거절은 역할을 부르지 않았다.
            self.assertEqual(3, [call.role for call in runner.calls].count("goal_normalizer"))
            with prepared.service.ledger.read() as connection:
                active = dict(connection.execute(
                    "SELECT id, active_plan_revision_id FROM projects"
                ).fetchall())
                b_executions = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM attempts WHERE project_id=?)"
                    " + (SELECT COUNT(*) FROM runtime_jobs WHERE project_id=?)",
                    (project_b, project_b),
                ).fetchone()[0]
                a_attempt_plans = {
                    row[0] for row in connection.execute(
                        "SELECT t.plan_revision_id FROM attempts a JOIN task_contracts t "
                        "ON t.id=a.task_id WHERE a.project_id=?",
                        (prepared.project_id,),
                    )
                }
            # E5·T5는 승인·활성화만 한다. P2와 B Plan은 실행하지 않았다.
            self.assertEqual(p2, active[prepared.project_id])
            self.assertIsNotNone(active[project_b])
            self.assertEqual(0, b_executions)
            self.assertEqual({prepared.plan_revision_id}, a_attempt_plans)

            digest = "sha256:" + "a" * 64
            outcome = _responsibility_outcome(
                scenario="scope-expansion",
                prepared=prepared,
                cell=result,
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
            # 결정적 PASS cell은 live 출처를 주장하지 않아 E2E-15 책임 PASS가 될 수 없다.
            self.assertEqual((EvidenceProvenance.FAKE,), outcome.provenance)

    def test_scope_expansion_policy_regression_is_primary_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, _runner = self._scope_cell(raw)
            # Core가 승인 없이 내부 Plan ID 활성화를 받아 준 회귀를 흉내 낸다.
            with patch.object(
                prepared.service, "activate_authorized_plan", return_value="activation_forged"
            ):
                result = self._run_scope(cell_root, prepared, runtime, source_digest)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_POLICY_BOUNDARY_REGRESSION", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertIn("B_internal_plan_id_activation:failed", result["failure"])
            self.assertFalse(result["coverage"]["axis_passed"]["policy"])
            # 같은 Core 진입점을 쓰는 target·effect 활성화 거절도 각자 제품 회귀로 남는다.
            self.assertEqual(
                [
                    ("policy", "E2E15_POLICY_BOUNDARY_REGRESSION", "product"),
                    ("target", "E2E15_TARGET_BOUNDARY_REGRESSION", "product"),
                    ("effect", "E2E15_EFFECT_BOUNDARY_REGRESSION", "product"),
                ],
                [(item["axis"], item["code"], item["class"]) for item in result["axis_failures"]],
            )

    def test_scope_expansion_target_guard_regression_is_target_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, _runner = self._scope_cell(raw)
            original = prepared.service.activate_selected_plan

            def forged(*, project_id, **arguments):
                # 승인 없는 두 번째 project의 활성화를 Core가 받아 준 회귀만 흉내 낸다.
                if project_id != prepared.project_id:
                    return "activation_forged"
                return original(project_id=project_id, **arguments)

            with patch.object(prepared.service, "activate_selected_plan", side_effect=forged):
                result = self._run_scope(cell_root, prepared, runtime, source_digest)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_TARGET_BOUNDARY_REGRESSION", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertIn("T2_target_b_selected_plan_activation:failed", result["failure"])
            self.assertEqual(
                {"policy": True, "target": False, "effect": True}, result["coverage"]["axis_passed"]
            )

    def test_scope_expansion_effect_guard_regression_is_effect_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, _runner = self._scope_cell(raw)
            # Core가 넓어진 효과 정책을 승인 안이라고 본 회귀를 흉내 낸다.
            with patch(
                "flowmarshal.engine.authorization._effects_within", return_value=True
            ):
                result = self._run_scope(cell_root, prepared, runtime, source_digest)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_EFFECT_BOUNDARY_REGRESSION", result["failure_code"])
            self.assertEqual("product", result["failure_class"])
            self.assertIn("E3_existing_authorization_activation:failed", result["failure"])
            self.assertEqual(
                {"policy": True, "target": True, "effect": False}, result["coverage"]["axis_passed"]
            )
            e3 = next(
                item for item in result["steps"]
                if item["step"] == "E3_existing_authorization_activation"
            )
            self.assertEqual([["goal", "goal_contract_digest"]], e3["authorization_changes"])

    def test_scope_expansion_without_effect_widening_is_model_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cell_root, prepared, runtime, source_digest, _runner = self._scope_cell(
                raw, r2_mutation="read_only"
            )

            result = self._run_scope(cell_root, prepared, runtime, source_digest)

            self.assertFalse(result["passed"])
            self.assertEqual("E2E15_EFFECT_WIDENING_NOT_OBSERVED", result["failure_code"])
            self.assertEqual("model", result["failure_class"])
            self.assertEqual(
                {"policy": True, "target": True, "effect": False}, result["coverage"]["axis_passed"]
            )
            # 확장 후보가 없으면 기존 승인 활성화·재승인 probe를 만들지 않는다.
            self.assertNotIn(
                "E3_existing_authorization_activation", [item["step"] for item in result["steps"]]
            )
            self.assertEqual({"create_thread": 0, "start_turn": 0, "resume": 0}, result["effect_count"])

    def test_authority_cells_claim_only_contract_provenance(self) -> None:
        from flowmarshal.engine.e2e_qualification import _cell_failure

        suite = QualificationSuiteManifest.load(ROOT / "config" / "qualification-suite.json")
        requirements = {item.responsibility_id: item for item in suite.e2e_responsibilities}
        digest = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as raw:
            for scenario, responsibility_id, code in (
                ("prohibited-effect", "E2E-14", "E2E14_BLOCK_EVIDENCE_MISSING"),
                ("scope-expansion", "E2E-15", "E2E15_TARGET_BOUNDARY_REGRESSION"),
                ("read-only-report", "E2E-03", "E2E03_WORKSPACE_TREE_CHANGED"),
                ("usage-missing-late", "E2E-06", "E2E06_MISSING_USAGE_NOT_NULL"),
                ("multi-task-dag", "E2E-02", "E2E02_DAG_EXECUTION_NOT_VERIFIED"),
                ("approved-repair", "E2E-04", "E2E04_REPAIR_CHAIN_INCOMPLETE"),
                ("context-discovery", "E2E-05", "E2E05_CONTEXT_BINDING_NOT_VERIFIED"),
            ):
                cell_root = Path(raw) / scenario
                (cell_root / "state").mkdir(parents=True)
                for name in (
                    "authorization-observation.json", "qualification-observation.json",
                    "scope-check.json", "state/flowmarshal-engine.sqlite3",
                    "final-report.json", "usage-observation.json", "runtime-receipts.json",
                    "context-discovery.json",
                ):
                    (cell_root / name).write_text("{}", encoding="utf-8")
                outcome = _responsibility_outcome(
                    scenario=scenario,
                    prepared=SimpleNamespace(pipeline_stages=("prepare",)),
                    # E2E-15 cell은 live 준비 위에서 돈 경우에만 live를 주장한다.
                    cell={"passed": False, "provenance": "live", **_cell_failure(code, "설명 문장")},
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
                self.assertEqual(
                    set(requirement.required_evidence_kinds), set(outcome.evidence_kinds)
                )
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
