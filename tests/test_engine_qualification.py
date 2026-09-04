from __future__ import annotations

import tempfile
import unittest
import sqlite3
import shutil
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.engine.domain import (
    ContextSourceRegistration,
    ContextSourceRegistrationKind,
    FailureClass,
    ProjectProfileRevision,
    RepairAction,
    RevisionStatus,
    RunOnceAction,
    ThreadBinding,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import (
    _copy_fixture,
    _contract,
    _live_restart_resume,
    _normal_completion,
    _prepare,
    _restart_resume,
    _restore_prepared_state,
    _stale_after_materialization,
    _unknown_receipt,
    _write_prepared_state,
)
from flowmarshal.engine.eval_cli import build_parser, validate_benchmark_matrix
from flowmarshal.engine.evaluation import BenchmarkCell
from flowmarshal.engine.freeze import LegacyFreezeManifest, verify_legacy_freeze
from flowmarshal.engine.ledger import ENGINE_SCHEMA_REVISION, EngineLedgerError, SQLiteEngineLedger
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.qualification import (
    PlanningScenarioCatalog,
    QualificationRunError,
    ORDER_SEEDS,
    _deterministic_contract,
    _planning_cell,
    default_role_configuration,
)
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineService, EngineServiceError

from tests.engine_helpers import goal, profile


ROOT = Path(__file__).resolve().parents[1]


def qualification_inventory() -> ModelInventory:
    roles = default_role_configuration(ROOT)
    grouped: dict[str, set[str]] = defaultdict(set)
    for role in (
        "normalizer",
        "skeleton_generator",
        "plan_expander",
        "general_reviewer",
        "critical_reviewer",
        "executor",
        "validator",
    ):
        binding = roles.binding_for(role)
        grouped[binding.model].add(binding.effort)
    return ModelInventory(
        source="qualification-test-model-list",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
            for model, efforts in sorted(grouped.items())
        ),
    )


class EngineQualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roles = default_role_configuration(ROOT)
        self.inventory = qualification_inventory()

    def prepared(self, base: Path):
        base.mkdir(parents=True)
        workspace, digest = _copy_fixture(ROOT, base)
        prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=self.roles,
        )
        return prepared, digest

    def test_eval_cli_exposes_all_qualification_commands_and_scopes(self) -> None:
        parser = build_parser()
        self.assertIn("run", parser.format_help())
        self.assertIn("resume", parser.format_help())
        self.assertIn("benchmark", parser.format_help())
        self.assertIn("cutover", parser.format_help())
        for scope in (
            "deterministic",
            "role-fixture",
            "full-planning-pipeline",
            "project-e2e",
        ):
            arguments = parser.parse_args(["run", "--scope", scope])
            self.assertEqual(scope, arguments.scope)

    def test_qualification_contracts_fix_expected_cells_and_model_ids_stay_in_config(self) -> None:
        scenarios = PlanningScenarioCatalog.load(
            ROOT / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        )
        self.assertEqual(6, len(scenarios.scenarios))
        contract = _deterministic_contract(ROOT)
        self.assertEqual(5, contract.expected_cell_count)
        python_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src" / "flowmarshal" / "engine").glob("*.py")
        )
        self.assertNotIn("gpt-5.6-luna", python_source)
        self.assertNotIn("gpt-5.6-terra", python_source)
        self.assertNotIn("gpt-5.6-sol", python_source)

    def test_legacy_freeze_manifest_is_current(self) -> None:
        manifest = LegacyFreezeManifest.load(ROOT / "config" / "legacy-freeze-manifest.json")
        report = verify_legacy_freeze(manifest, project_root=ROOT)
        self.assertTrue(report.passed, report)
        self.assertEqual(40, report.checked_file_count)

    def test_synthetic_fixture_is_independently_bound_and_runs_without_source_mutation(self) -> None:
        from flowmarshal.engine.smoke import run_synthetic_lifecycle
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            selector = Path("tests/fixtures/engine/synthetic-lifecycle-project")
            fixture = base / selector
            shutil.copytree(ROOT / selector, fixture)
            before = {path.name: path.read_bytes() for path in fixture.iterdir()}
            (base / "AGENTS.md").write_text("검사 프로젝트 밖의 큰 지침" * 20000, encoding="utf-8")
            with patch("flowmarshal.engine.qualification.source_manifest_digest", return_value="sha256:" + "1" * 64), \
                 patch("flowmarshal.engine.qualification.default_role_configuration", return_value=self.roles):
                original = _deterministic_contract(base)
                status = run_synthetic_lifecycle(project_root=fixture, database_path=base / "state.sqlite3",
                                                artifact_root=base / "artifacts")
                self.assertEqual("completed", status["project"]["run_state"])
                self.assertTrue(status["history_valid"])
                self.assertEqual(before, {path.name: path.read_bytes() for path in fixture.iterdir()})
                (fixture / "app.py").write_text("value = 2\n", encoding="utf-8")
                changed = _deterministic_contract(base)
            self.assertEqual(original.fixture_digests[:3], changed.fixture_digests[:3])
            self.assertNotEqual(original.fixture_digests[3], changed.fixture_digests[3])
            self.assertEqual(original.fixture_digests[4], changed.fixture_digests[4])
            self.assertNotEqual(original.contract_digest, changed.contract_digest)

    def test_benchmark_requires_exact_scenarios_seeds_and_one_model_lock(self) -> None:
        catalog = PlanningScenarioCatalog.load(
            ROOT / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        )
        cells = tuple(
            BenchmarkCell(
                scenario_id=scenario.scenario_id, scenario_digest=scenario.scenario_digest,
                neutral_input_digest=sha256_digest({"neutral_fixture": scenario.scenario_id}),
                order_seed=seed, path_kind=scenario.path_kind, implementation=implementation,
                model_lock_digest="sha256:" + "a" * 64,
                functional_result_digest="sha256:" + "b" * 64,
                runner_receipt_digest="sha256:" + "c" * 64,
                expected_disposition=scenario.expected_disposition,
                disposition=scenario.expected_disposition,
                uncached_input_tokens=100, output_tokens=50,
                latency_ms_to_first_feasible=(1000 if scenario.expected_disposition == "selected" else None),
                latency_ms_to_disposition=1000, detailed_task_count=1,
                unexecuted_detailed_task_count=0, candidate_output_tokens=50,
                discarded_candidate_output_tokens=0,
            )
            for scenario in catalog.scenarios for seed in ORDER_SEEDS
            for implementation in ("r31_baseline", "skeleton_engine")
        )
        validate_benchmark_matrix(cells, catalog)
        for changed in (
            cells[0].model_copy(update={"order_seed": 999}),
            cells[0].model_copy(update={"scenario_digest": "sha256:" + "d" * 64}),
            cells[0].model_copy(update={"model_lock_digest": "sha256:" + "e" * 64}),
            cells[0].model_copy(update={"expected_disposition": "blocked"}),
        ):
            with self.assertRaises(QualificationRunError):
                validate_benchmark_matrix((changed, *cells[1:]), catalog)

    def test_e2e_recovery_scenarios_do_not_duplicate_external_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, helper in enumerate(
                (_stale_after_materialization, _restart_resume, _unknown_receipt)
            ):
                prepared, digest = self.prepared(root / f"case-{index}")
                cell = helper(prepared, FakeCodexRuntime(self.inventory), digest)
                self.assertTrue(cell["passed"], cell)

    def test_live_restart_harness_reopens_core_and_records_read_before_resume(self) -> None:
        class RuntimeConnection(FakeCodexRuntime):
            stored_read_calls = 0

            def read_stored(self, *, thread_id):
                self.stored_read_calls += 1
                if self.stored_read_calls == 1:
                    raise RuntimeError("rollout is empty")
                return super().read_stored(thread_id=thread_id)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

            def start_turn(self, **arguments):
                result = super().start_turn(**arguments)
                if self.resume_calls:
                    (arguments["cwd"] / "app.py").write_text(
                        "def add(left, right):\n    return left + right\n", encoding="utf-8"
                    )
                    self.complete(arguments["thread_id"])
                return result

        with tempfile.TemporaryDirectory() as temp:
            cell_root = Path(temp) / "restart"
            prepared, digest = self.prepared(cell_root)
            contract = _contract(ROOT, self.inventory, self.roles, digest)
            fixture_digest = contract.fixture_digests[2]
            _write_prepared_state(
                cell_root, prepared,
                evaluation_contract_digest=contract.contract_digest,
                fixture_digest=fixture_digest,
            )
            first = RuntimeConnection(self.inventory)
            second = RuntimeConnection(self.inventory)
            second.threads = first.threads
            with patch(
                "flowmarshal.engine.e2e_qualification.CodexAppServerRuntime",
                side_effect=[first, second],
            ) as factory:
                cell, receipts = _live_restart_resume(
                    prepared, digest, cell_root=cell_root, contract=contract,
                    fixture_digest=fixture_digest, codex_bin=None,
                )
            self.assertEqual(2, factory.call_count)
            self.assertTrue(cell["passed"], cell)
            self.assertTrue(cell["read_before_resume"])
            self.assertTrue(receipts)
            self.assertGreaterEqual(first.stored_read_calls, 2)

    def test_planning_workspace_is_preserved_and_mutation_is_not_overwritten(self) -> None:
        catalog = PlanningScenarioCatalog.load(
            ROOT / "tests" / "fixtures" / "engine" / "planning-scenarios.json"
        )
        scenario = next(item for item in catalog.scenarios if item.expected_disposition == "blocked")
        prepared = SimpleNamespace(
            goal_contract=SimpleNamespace(
                status=RevisionStatus.NEEDS_INPUT,
                preparation_binding=SimpleNamespace(finding_codes=("CONTEXT_REQUIRED",)),
            ),
            proposal=SimpleNamespace(unresolved_questions=(SimpleNamespace(
                blocking=True, model_dump=lambda **_: {"question": "필수 계약 source를 제공하세요", "blocking": True},
            ),)),
            model_dump=lambda **_: {"test_double": True},
        )
        with tempfile.TemporaryDirectory() as temp:
            work_root = Path(temp) / "cell"
            arguments = dict(
                scenario=scenario, seed=17,
                fixture_root=ROOT / "tests" / "fixtures" / "engine" / "live-smoke-project",
                runtime=FakeCodexRuntime(self.inventory), inventory=self.inventory,
                roles=self.roles, work_root=work_root,
            )
            with patch("flowmarshal.engine.qualification.GoalPreparationPipeline.prepare", return_value=prepared):
                cell, _ = _planning_cell(**arguments)
                self.assertTrue(cell["passed"])
                self.assertTrue((work_root / "project" / "app.py").is_file())
                _planning_cell(**arguments)
                (work_root / "project" / "app.py").write_text("changed\n", encoding="utf-8")
                with self.assertRaises(QualificationRunError):
                    _planning_cell(**arguments)

    def test_project_e2e_incomplete_cell_restores_same_ledger_and_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cell_root = Path(temp) / "normal-completion"
            prepared, source_digest = self.prepared(cell_root)
            contract_digest = sha256_digest({"contract": "resume"})
            fixture_digest = sha256_digest({"fixture": "normal-completion"})
            _write_prepared_state(
                cell_root,
                prepared,
                evaluation_contract_digest=contract_digest,
                fixture_digest=fixture_digest,
            )
            runtime = FakeCodexRuntime(self.inventory)
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            (prepared.workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n"
                "    \"\"\"두 정수의 합을 반환한다.\"\"\"\n\n"
                "    return left + right\n",
                encoding="utf-8",
            )
            runtime.complete(binding.thread_id)

            restored = _restore_prepared_state(
                cell_root,
                evaluation_contract_digest=contract_digest,
                fixture_digest=fixture_digest,
            )
            result = _normal_completion(restored, runtime, source_digest)
            self.assertTrue(result["passed"], result)
            self.assertEqual(prepared.task_id, restored.proposal.task_id)
            self.assertEqual(1, runtime.create_calls)

    def test_faults_around_thread_and_turn_intents_never_recreate_provider_effect(self) -> None:
        points = (
            "before_thread_intent",
            "after_thread_intent",
            "after_thread_effect",
            "after_thread_receipt",
            "before_turn_intent",
            "after_turn_intent",
            "after_turn_effect",
            "after_turn_receipt",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index, point in enumerate(points):
                with self.subTest(point=point):
                    prepared, _ = self.prepared(root / f"fault-{index}")
                    runtime = FakeCodexRuntime(self.inventory)
                    EngineDispatcher(prepared.service, runtime).run_once(
                        prepared.project_id, proposal=prepared.proposal
                    )

                    def fault(actual: str) -> None:
                        if actual == point:
                            raise RuntimeError(point)

                    with self.assertRaisesRegex(RuntimeError, point):
                        EngineDispatcher(
                            prepared.service, runtime, fault_hook=fault
                        ).run_once(prepared.project_id)
                    create_before = runtime.create_calls
                    turn_before = runtime.turn_calls
                    recovered = EngineDispatcher(prepared.service, runtime).run_once(
                        prepared.project_id
                    )
                    self.assertLessEqual(runtime.create_calls, 1)
                    self.assertLessEqual(runtime.turn_calls, 1)
                    if point in {
                        "after_thread_intent",
                        "after_thread_effect",
                        "after_turn_intent",
                        "after_turn_effect",
                    }:
                        self.assertEqual(RunOnceAction.BLOCKED, recovered.action)
                        self.assertEqual("EXTERNAL_EFFECT_UNKNOWN", recovered.blocker_code)
                        self.assertEqual(create_before, runtime.create_calls)
                        self.assertEqual(turn_before, runtime.turn_calls)

    def test_run_once_returns_typed_repair_proposal_after_attempt_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prepared, _ = self.prepared(Path(temp) / "repair")
            runtime = FakeCodexRuntime(self.inventory)
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            runtime.fail(binding.thread_id, response="구현 실패")
            observed = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.OBSERVED, observed.action)

            blocked = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.BLOCKED, blocked.action)
            self.assertEqual("TASK_RECOVERY_REQUIRED", blocked.blocker_code)
            self.assertEqual(FailureClass.IMPLEMENTATION, blocked.failure_class)
            self.assertEqual(RepairAction.TASK_REPAIR, blocked.suggested_repair_action)
            self.assertFalse(blocked.checkpoint_required)

    def test_validation_and_state_reobservation_faults_resume_without_duplicate_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prepared, _ = self.prepared(Path(temp) / "validation")
            runtime = FakeCodexRuntime(self.inventory)
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            (prepared.workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n"
                "    \"\"\"두 정수의 합을 반환한다.\"\"\"\n\n"
                "    return left + right\n",
                encoding="utf-8",
            )
            runtime.complete(binding.thread_id)
            dispatcher.run_once(prepared.project_id)

            def before_validation(point: str) -> None:
                if point == "before_validation":
                    raise RuntimeError(point)

            with self.assertRaisesRegex(RuntimeError, "before_validation"):
                EngineDispatcher(
                    prepared.service, runtime, fault_hook=before_validation
                ).run_once(prepared.project_id)
            validated = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.VALIDATED, validated.action)

            def before_state(point: str) -> None:
                if point == "before_state_reobservation":
                    raise RuntimeError(point)

            with self.assertRaisesRegex(RuntimeError, "before_state_reobservation"):
                EngineDispatcher(prepared.service, runtime, fault_hook=before_state).run_once(
                    prepared.project_id
                )
            recovered = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.OBSERVED, recovered.action)
            with prepared.service.ledger.read() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM validation_results WHERE task_id = ?",
                    (prepared.task_id,),
                ).fetchone()[0]
            self.assertEqual(1, count)

    def test_faults_after_execution_validation_and_state_do_not_repeat_runtime_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            prepared, _ = self.prepared(Path(temp) / "post-effect-faults")
            runtime = FakeCodexRuntime(self.inventory)
            dispatcher = EngineDispatcher(prepared.service, runtime)
            dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                row = connection.execute(
                    "SELECT binding_json FROM attempts WHERE id = ?", (dispatched.attempt_id,)
                ).fetchone()
            binding = ThreadBinding.model_validate_json(row["binding_json"])
            (prepared.workspace / "app.py").write_text(
                "def add(left: int, right: int) -> int:\n"
                "    \"\"\"두 정수의 합을 반환한다.\"\"\"\n\n"
                "    return left + right\n",
                encoding="utf-8",
            )
            runtime.complete(binding.thread_id)

            def after_execution(point: str) -> None:
                if point == "after_execution_observed":
                    raise RuntimeError(point)

            with self.assertRaisesRegex(RuntimeError, "after_execution_observed"):
                EngineDispatcher(
                    prepared.service, runtime, fault_hook=after_execution
                ).run_once(prepared.project_id)
            recovered_execution = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.OBSERVED, recovered_execution.action)

            def after_validation(point: str) -> None:
                if point == "after_validation_observed":
                    raise RuntimeError(point)

            with self.assertRaisesRegex(RuntimeError, "after_validation_observed"):
                EngineDispatcher(
                    prepared.service, runtime, fault_hook=after_validation
                ).run_once(prepared.project_id)

            def after_state(point: str) -> None:
                if point == "after_state_reobservation":
                    raise RuntimeError(point)

            with self.assertRaisesRegex(RuntimeError, "after_state_reobservation"):
                EngineDispatcher(
                    prepared.service, runtime, fault_hook=after_state
                ).run_once(prepared.project_id)
            goal_bound = dispatcher.run_once(
                prepared.project_id, goal_validation_step=prepared.proposal.validation_steps[0].model_copy(
                    update={"validation_id": "validation_goal"}),
            )
            self.assertEqual(RunOnceAction.MATERIALIZED, goal_bound.action)
            continued = dispatcher.run_once(prepared.project_id)
            self.assertEqual(RunOnceAction.VALIDATED, continued.action)
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(1, runtime.turn_calls)
            with prepared.service.ledger.read() as connection:
                validation_count = connection.execute(
                    "SELECT COUNT(*) FROM validation_results WHERE task_id = ?",
                    (prepared.task_id,),
                ).fetchone()[0]
                state_count = connection.execute(
                    "SELECT COUNT(*) FROM state_snapshots WHERE project_id = ?",
                    (prepared.project_id,),
                ).fetchone()[0]
            self.assertEqual(1, validation_count)
            self.assertEqual(2, state_count)

    def test_registered_context_source_is_mapped_and_changed_digest_blocks_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            reference = base / "contract.md"
            reference.write_text("external contract v1\n", encoding="utf-8")
            service = EngineService(
                SQLiteEngineLedger(base / "state.sqlite3", artifact_root=base / "artifacts")
            )
            service.initialize()
            project_id = service.create_project(name="context source", root=project)
            source_id = new_id("context_source")
            service.register_context_source(
                ContextSourceRegistration(
                    context_source_id=source_id,
                    project_id=project_id,
                    kind=ContextSourceRegistrationKind.REFERENCE,
                    path=str(reference.resolve()),
                    content_digest=sha256_bytes(reference.read_bytes()),
                    registered_at=utc_now(),
                )
            )
            baseline = profile(project_id)
            definition = baseline.definition.model_copy(
                update={"context_source_refs": (source_id,)}
            )
            configured = ProjectProfileRevision(
                profile_revision_id=baseline.profile_revision_id,
                project_id=project_id,
                revision_no=1,
                definition=definition,
                definition_digest=definition.definition_digest,
                status=RevisionStatus.READY,
                created_at=baseline.created_at,
            )
            service.register_profile(configured)
            service.register_goal(goal(project_id, configured.definition_digest))
            project_map, _ = service.reobserve_project(project_id)
            self.assertIn(str(reference.resolve()), {item.path for item in project_map.entries})
            reference.write_text("external contract v2\n", encoding="utf-8")
            with self.assertRaisesRegex(EngineServiceError, "CONTEXT_SOURCE_CHANGED"):
                service.reobserve_project(project_id)

    def test_profile_cannot_reference_unregistered_context_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            project.mkdir()
            service = EngineService(
                SQLiteEngineLedger(base / "state.sqlite3", artifact_root=base / "artifacts")
            )
            service.initialize()
            project_id = service.create_project(name="unknown source", root=project)
            baseline = profile(project_id)
            definition = baseline.definition.model_copy(
                update={"context_source_refs": (new_id("context_source"),)}
            )
            invalid = baseline.model_copy(
                update={"definition": definition, "definition_digest": definition.definition_digest}
            )
            with self.assertRaisesRegex(EngineServiceError, "등록되지 않은"):
                service.register_profile(invalid)

    def test_new_engine_database_uses_revision_two(self) -> None:
        self.assertEqual(2, ENGINE_SCHEMA_REVISION)
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "old-engine.sqlite3"
            ledger = SQLiteEngineLedger(database)
            ledger.initialize()
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE schema_meta SET value = '1' WHERE key = 'schema_revision'"
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(EngineLedgerError, "revision"):
                SQLiteEngineLedger(database).initialize()


if __name__ == "__main__":
    unittest.main()
