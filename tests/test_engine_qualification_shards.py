"""shard 부분 실행과 교차 run-root aggregate를 실제 실행 함수로 검사한다."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.budget import GoalBudgetPolicy
from flowmarshal.engine.domain import utc_now
from flowmarshal.engine.evaluation import EvaluationContract, EvaluationRunStatus
from flowmarshal.engine.evaluation_budget import EvaluationPolicies
from flowmarshal.engine.model_lock import RUNTIME_CAPABILITIES
from flowmarshal.engine.models import ModelCapability, ModelInventory
from flowmarshal.engine.qualification import (
    QualificationRunError,
    _contract_cells,
    _shard_cells,
    aggregate_shard_runs,
    default_role_configuration,
    run_full_planning_pipeline,
    run_role_fixture,
)
from flowmarshal.engine.role_execution import RoleTimeoutPolicy
from flowmarshal.engine.roles import RoleCallReceipt, StructuredRoleError
from flowmarshal.engine.runtime import FakeCodexRuntime
from flowmarshal.engine.scope_report_verification import verify_scope_report


ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def _inventory(roles) -> ModelInventory:
    grouped: dict[str, set[str]] = {}
    for key in (
        "normalizer", "skeleton_generator", "plan_expander", "general_reviewer",
        "critical_reviewer", "executor", "validator",
    ):
        binding = roles.binding_for(key)
        grouped.setdefault(binding.model, set()).add(binding.effort)
    return ModelInventory(
        executable_digest="sha256:" + "0" * 64,
        runtime_capabilities=RUNTIME_CAPABILITIES,
        source="shard-aggregate-test",
        models=tuple(
            ModelCapability(model=model, supported_efforts=tuple(sorted(efforts)))
            for model, efforts in sorted(grouped.items())
        ),
    )


class _FakeRuntimeContext:
    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.runtime = FakeCodexRuntime(_inventory(default_role_configuration(ROOT)))

    def __enter__(self):
        return self.runtime

    def __exit__(self, *args) -> None:
        self.runtime.close()


def _receipt(status: str) -> RoleCallReceipt:
    return RoleCallReceipt(
        call_id="call-1", role="reviewer", status=status, model="fixture-model", effort="low",
        inventory_digest=DIGEST, permission_profile="danger-full-access", approval_policy="never",
        input_digest=DIGEST, output_schema_digest=DIGEST, latency_ms=0, recorded_at=utc_now(),
    )


class _SchemaFailingRunner:
    receipts: list[RoleCallReceipt] = []

    def run(self, request, *, validator):
        del request, validator
        raise StructuredRoleError("schema failed", receipt=_receipt("schema_failed"))


class ShardAggregateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temporary.name)
        cls.roles = default_role_configuration(ROOT)
        cls.policies = EvaluationPolicies(
            budget=GoalBudgetPolicy(total_tokens=1_000, call_reservation_tokens=10),
            role_timeouts=RoleTimeoutPolicy(default_timeout_seconds=30),
        )
        cls.planning_calls: dict[str, list[tuple[int, str]]] = {}
        cls.planning_full, cls.planning_full_report = cls._planning("full")
        cls.planning_shards = []
        cls.planning_reports = []
        for index in range(3):
            path, report = cls._planning(f"p{index}", shard_index=index, shard_count=3)
            cls.planning_shards.append(path)
            cls.planning_reports.append(report)
        cls.role_shards = [
            cls._role(f"r{index}", shard_index=index, shard_count=8)[0] for index in range(8)
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @classmethod
    def _planning(cls, name: str, **shard):
        calls = cls.planning_calls.setdefault(name, [])

        def cell(*, scenario, seed, **_kwargs):
            calls.append((seed, scenario.scenario_digest))
            selected = scenario.expected_disposition == "selected"
            return {
                "scenario_id": scenario.scenario_id, "scenario_digest": scenario.scenario_digest,
                "order_seed": seed, "expected_disposition": scenario.expected_disposition,
                "passed": True, "selected": selected, "schema_valid": True,
                "logical_role_calls": 1, "candidate_versions": 1,
            }, [_receipt("completed")]

        with (
            patch("flowmarshal.engine.qualification._preflight", return_value=()),
            patch("flowmarshal.engine.qualification.CodexAppServerRuntime", _FakeRuntimeContext),
            patch("flowmarshal.engine.qualification._planning_cell", side_effect=cell),
        ):
            return run_full_planning_pipeline(
                root=ROOT, run_root=cls.work / name, role_configuration=cls.roles,
                evaluation_policies=cls.policies, **shard,
            )

    @classmethod
    def _role(cls, name: str, **shard):
        with (
            patch("flowmarshal.engine.qualification._preflight", return_value=()),
            patch("flowmarshal.engine.qualification.CodexAppServerRuntime", _FakeRuntimeContext),
            patch("flowmarshal.engine.qualification.initialize_cell_budget", return_value=(None, None)),
            patch("flowmarshal.engine.qualification.register_and_attach_goal"),
            patch(
                "flowmarshal.engine.qualification.budgeted_role_runner",
                return_value=_SchemaFailingRunner(),
            ),
        ):
            return run_role_fixture(
                root=ROOT, run_root=cls.work / name, role_configuration=cls.roles,
                evaluation_policies=cls.policies, **shard,
            )

    def _contract(self, run_root: Path) -> EvaluationContract:
        return EvaluationContract.model_validate_json(
            (run_root / "evaluation-contract.json").read_text(encoding="utf-8")
        )

    def _copies(self) -> tuple[Path, list[Path]]:
        scratch = Path(tempfile.mkdtemp(dir=self.work))
        copies = []
        for index, shard in enumerate(self.planning_shards):
            copies.append(Path(shutil.copytree(shard, scratch / f"s{index}")))
        return scratch, copies

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def test_shards_run_disjoint_contiguous_blocks_of_the_unchanged_contract(self) -> None:
        cells = _contract_cells(self._contract(self.planning_full))
        self.assertEqual(18, len(cells))
        self.assertEqual(set(cells), set(self.planning_calls["full"]))
        executed = [self.planning_calls[f"p{index}"] for index in range(3)]
        for index, calls in enumerate(executed):
            self.assertEqual(set(cells[index * 6:(index + 1) * 6]), set(calls))
            self.assertEqual(6, len(calls))
        for name in ("evaluation-contract.json", "run-metadata.json"):
            expected = (self.planning_full / name).read_bytes()
            for shard in self.planning_shards:
                self.assertEqual(expected, (shard / name).read_bytes(), name)
        for shard, report in zip(self.planning_shards, self.planning_reports, strict=True):
            self.assertIs(EvaluationRunStatus.RUNNING, report.status)
            self.assertFalse(report.passed)
            self.assertEqual({"completed_cell_count": 6}, report.metrics)
            self.assertFalse((shard / "qualification-report.json").exists())
        self.assertTrue(self.planning_full_report.passed)

    def test_resume_keeps_the_stored_assignment_and_rejects_another(self) -> None:
        before = len(self.planning_calls["p1"])
        _path, report = self._planning("p1")
        self.assertEqual(before, len(self.planning_calls["p1"]))
        self.assertIs(EvaluationRunStatus.RUNNING, report.status)
        with self.assertRaisesRegex(QualificationRunError, "SHARD_ASSIGNMENT_MISMATCH"):
            self._planning("p1", shard_index=2, shard_count=3)

    def test_completed_full_run_root_cannot_be_reopened_as_a_shard(self) -> None:
        with self.assertRaisesRegex(QualificationRunError, "SHARD_ASSIGNMENT_MISMATCH"):
            self._planning("full", shard_index=0, shard_count=3)
        self.assertFalse((self.planning_full / "shard-assignment.json").exists())
        state = json.loads((self.planning_full / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual("COMPLETED", state["status"])

    def test_invalid_assignment_is_rejected_before_the_provider_opens(self) -> None:
        def opened(*_args, **_kwargs):
            raise AssertionError("provider를 열었다")

        for run, name in ((run_full_planning_pipeline, "planning"), (run_role_fixture, "role")):
            with (
                self.subTest(name),
                patch("flowmarshal.engine.qualification._preflight", return_value=()),
                patch("flowmarshal.engine.qualification.CodexAppServerRuntime", side_effect=opened),
                self.assertRaisesRegex(QualificationRunError, "SHARD_ASSIGNMENT_INVALID"),
            ):
                run(
                    root=ROOT, run_root=self.work / f"never-{name}", role_configuration=self.roles,
                    evaluation_policies=self.policies, shard_index=3, shard_count=3,
                )
            self.assertFalse((self.work / f"never-{name}").exists())

    def test_invalid_assignment_is_rejected_before_any_cell(self) -> None:
        contract = self._contract(self.planning_full)
        for index, count in ((3, 3), (-1, 3), (0, 19), (0, None), (None, 3), (True, 3)):
            with self.subTest(index=index, count=count):
                with self.assertRaisesRegex(QualificationRunError, "SHARD_ASSIGNMENT_INVALID"):
                    _shard_cells(contract, self.work / "unused", index, count)
        self.assertFalse((self.work / "unused").exists())

    def test_planning_aggregate_equals_the_full_run_and_verifies(self) -> None:
        destination, report = aggregate_shard_runs(
            root=ROOT, shard_run_roots=tuple(reversed(self.planning_shards)),
            destination=self.work / "planning-aggregate",
        )
        result = verify_scope_report(root=ROOT, run_root=destination, report=report)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(18, result.recalculated_metrics["cell_count"])
        self.assertEqual(self.planning_full_report.metrics, report.metrics)
        self.assertEqual(self.planning_full_report.passed, report.passed)
        self.assertEqual(self.planning_full_report.failures, report.failures)
        state = json.loads((destination / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual(("COMPLETED", 18), (state["status"], state["completed_cell_count"]))
        for name in ("shard-assignment.json", "role-progress", "last-error.json", "p", "w"):
            self.assertFalse((destination / name).exists(), name)
        self.assertTrue((destination / "reproduction-bundle").is_dir())
        self.assertEqual(
            (self.planning_full / "run-metadata.json").read_bytes(),
            (destination / "run-metadata.json").read_bytes(),
        )
        again, second = aggregate_shard_runs(
            root=ROOT, shard_run_roots=tuple(self.planning_shards), destination=destination,
        )
        self.assertEqual((destination, report.metrics), (again, second.metrics))

    def test_role_aggregate_of_eight_shards_verifies_48_cells(self) -> None:
        for shard in self.role_shards:
            self.assertEqual(6, sum(1 for _ in (shard / "cells").rglob("*.json")))
        destination, report = aggregate_shard_runs(
            root=ROOT, shard_run_roots=tuple(self.role_shards),
            destination=self.work / "role-aggregate",
        )
        result = verify_scope_report(root=ROOT, run_root=destination, report=report)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(48, result.recalculated_metrics["cell_count"])
        self.assertFalse(report.passed)
        self.assertTrue((destination / "role-qualification-report.json").is_file())

    def test_aggregate_leaves_shard_private_state_behind(self) -> None:
        scratch, copies = self._copies()
        private = ("p/s0/c0/budget-state/ledger.sqlite3", "w/s0/c0/state.json",
                   "role-progress/progress.json", "last-error.json")
        for shard in copies:
            for name in private:
                (shard / name).parent.mkdir(parents=True, exist_ok=True)
                (shard / name).write_text("{}", encoding="utf-8")
        destination, report = aggregate_shard_runs(
            root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "out",
        )
        self.assertTrue(verify_scope_report(root=ROOT, run_root=destination, report=report).valid)
        for name in ("p", "w", "role-progress", "last-error.json", "shard-assignment.json"):
            self.assertTrue((copies[0] / name).exists(), name)
            self.assertFalse((destination / name).exists(), name)
        shard_state = json.loads((copies[0] / "run-state.json").read_text(encoding="utf-8"))
        state = json.loads((destination / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual(("RUNNING", 6), (shard_state["status"], shard_state["completed_cell_count"]))
        self.assertEqual(("COMPLETED", 18), (state["status"], state["completed_cell_count"]))

    def test_aggregate_rejects_a_shard_bound_to_another_contract(self) -> None:
        # 같은 계약의 bytes만 다른 경우: metadata 결속은 통과하고 shard 사이 bytes 대조가 거부한다.
        scratch, copies = self._copies()
        contract = copies[2] / "evaluation-contract.json"
        contract.write_bytes(contract.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            QualificationRunError,
            "AGGREGATE_SHARD_BINDING_MISMATCH: evaluation-contract.json roots=.*s0,.*s2",
        ):
            aggregate_shard_runs(root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "a")
        self.assertFalse((scratch / "a").exists())
        # 다른 계약: shard 자신의 run metadata 결속부터 깨진다.
        scratch, copies = self._copies()
        contract = copies[2] / "evaluation-contract.json"
        document = json.loads(contract.read_text(encoding="utf-8"))
        document["prompt_digest"] = "sha256:" + "c" * 64
        contract.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(QualificationRunError, "evaluation contract digest가 다릅니다"):
            aggregate_shard_runs(root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "b")
        self.assertFalse((scratch / "b").exists())

    def test_aggregate_names_the_missing_cells(self) -> None:
        scratch, copies = self._copies()
        contract = self._contract(self.planning_full)
        seed, digest = _contract_cells(contract)[6]
        fixture_key = digest.split(":", 1)[-1][:24]
        (copies[1] / "cells" / f"seed-{seed}" / f"{fixture_key}.json").unlink()
        with self.assertRaisesRegex(
            QualificationRunError,
            f"AGGREGATE_SHARD_CELL_SET_MISMATCH:.*missing=.*{seed}.*{digest}",
        ):
            aggregate_shard_runs(
                root=ROOT,
                shard_run_roots=tuple(copies),
                destination=scratch / "missing-aggregate",
            )
        self.assertFalse((scratch / "missing-aggregate").exists())

    def test_aggregate_rejects_duplicate_checkpoint_before_writing(self) -> None:
        scratch, copies = self._copies()
        cell = next((copies[0] / "cells").rglob("*.json"))
        duplicate = copies[1] / cell.relative_to(copies[0])
        duplicate.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cell, duplicate)
        document = json.loads(cell.read_text(encoding="utf-8"))
        with self.assertRaises(QualificationRunError) as raised:
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "out",
            )
        message = str(raised.exception)
        self.assertIn("AGGREGATE_CELL_DUPLICATE", message)
        self.assertIn(f"seed={document['order_seed']} fixture={document['fixture_digest']}", message)
        self.assertIn(f"roots={copies[0]},{copies[1]}", message)
        self.assertFalse((scratch / "out").exists())

    def test_aggregate_requires_complete_unique_strict_assignments(self) -> None:
        cases = (
            ("missing", lambda roots: (roots[0] / "shard-assignment.json").unlink()),
            (
                "duplicate-index",
                lambda roots: (roots[2] / "shard-assignment.json").write_text(
                    json.dumps({"shard_index": 1, "shard_count": 3}), encoding="utf-8"
                ),
            ),
            (
                "different-count",
                lambda roots: (roots[2] / "shard-assignment.json").write_text(
                    json.dumps({"shard_index": 2, "shard_count": 4}), encoding="utf-8"
                ),
            ),
            (
                "extra-field",
                lambda roots: (roots[2] / "shard-assignment.json").write_text(
                    json.dumps({"shard_index": 2, "shard_count": 3, "extra": True}),
                    encoding="utf-8",
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name):
                scratch, copies = self._copies()
                mutate(copies)
                with self.assertRaisesRegex(
                    QualificationRunError, "AGGREGATE_SHARD_ASSIGNMENT_"
                ):
                    aggregate_shard_runs(
                        root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "out"
                    )
                self.assertFalse((scratch / "out").exists())
        scratch, copies = self._copies()
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_ASSIGNMENT_MISMATCH"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies[:2]), destination=scratch / "out"
            )
        self.assertFalse((scratch / "out").exists())

    def test_aggregate_revalidates_each_bundle_and_requires_equal_digests(self) -> None:
        scratch, copies = self._copies()
        payload = next((copies[1] / "reproduction-bundle" / "payload").rglob("*.py"))
        payload.write_bytes(payload.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_REPRODUCTION_BUNDLE_INVALID"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "invalid"
            )
        self.assertFalse((scratch / "invalid").exists())

        scratch, copies = self._copies()
        manifest_path = copies[2] / "reproduction-bundle" / "qualification-freeze.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["suite_manifest_digest"] = "sha256:" + "f" * 64
        manifest["bundle_digest"] = sha256_digest(
            {key: value for key, value in manifest.items() if key != "bundle_digest"}
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_REPRODUCTION_BUNDLE_MISMATCH"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "mismatch"
            )
        self.assertFalse((scratch / "mismatch").exists())

    def test_aggregate_revalidates_existing_destination_bundle(self) -> None:
        scratch, copies = self._copies()
        target = scratch / "out"
        shutil.copytree(copies[0] / "reproduction-bundle", target / "reproduction-bundle")
        payload = next((target / "reproduction-bundle" / "payload").rglob("*.py"))
        payload.write_bytes(payload.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_DESTINATION_REPRODUCTION_BUNDLE_INVALID"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=target
            )
        self.assertFalse((target / "evaluation-contract.json").exists())

    def test_aggregate_rejects_incomplete_checkpoint_before_writing(self) -> None:
        scratch, copies = self._copies()
        cell = sorted((copies[-1] / "cells").rglob("*.json"))[-1]
        document = json.loads(cell.read_text(encoding="utf-8"))
        document["completed"] = False
        cell.write_text(json.dumps(document), encoding="utf-8")
        target = scratch / "out"
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_CHECKPOINT_INVALID"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=target
            )
        self.assertFalse(target.exists())
        self.assertFalse(json.loads(cell.read_text(encoding="utf-8"))["completed"])

    def test_aggregate_rejects_destination_metadata_conflict_without_changes(self) -> None:
        scratch, copies = self._copies()
        target = scratch / "out"
        target.mkdir()
        (target / "run-metadata.json").write_text('{"different":true}', encoding="utf-8")
        before = self._snapshot(target)
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_BINDING_MISMATCH: run-metadata.json"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=target
            )
        self.assertEqual(before, self._snapshot(target))

    def test_aggregate_rejects_destination_checkpoint_conflict_without_changes(self) -> None:
        scratch, copies = self._copies()
        target = scratch / "out"
        target.mkdir()
        shutil.copyfile(copies[0] / "evaluation-contract.json", target / "evaluation-contract.json")
        shutil.copyfile(copies[0] / "run-state.json", target / "run-state.json")
        source_cell = sorted((copies[-1] / "cells").rglob("*.json"))[-1]
        destination_cell = target / "cells" / source_cell.relative_to(copies[-1] / "cells")
        destination_cell.parent.mkdir(parents=True)
        document = json.loads(source_cell.read_text(encoding="utf-8"))
        document["raw_structured_assessment"]["selected"] = not document[
            "raw_structured_assessment"
        ]["selected"]
        destination_cell.write_text(json.dumps(document), encoding="utf-8")
        state = json.loads((target / "run-state.json").read_text(encoding="utf-8"))
        state["completed_cell_count"] = 1
        (target / "run-state.json").write_text(json.dumps(state), encoding="utf-8")
        before = self._snapshot(target)
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_DESTINATION_CHECKPOINT_INVALID"
        ):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=target
            )
        self.assertEqual(before, self._snapshot(target))

    def test_aggregate_rejects_root_overlap_before_writing(self) -> None:
        scratch, copies = self._copies()
        child_target = copies[0] / "aggregate"
        with self.assertRaisesRegex(QualificationRunError, "AGGREGATE_SHARD_ROOTS_OVERLAP"):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=child_target
            )
        self.assertFalse(child_target.exists())

        with self.assertRaisesRegex(QualificationRunError, "AGGREGATE_SHARD_ROOTS_OVERLAP"):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=tuple(copies), destination=scratch
            )
        self.assertFalse((scratch / "evaluation-contract.json").exists())

        nested = copies[0] / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(QualificationRunError, "AGGREGATE_SHARD_ROOTS_OVERLAP"):
            aggregate_shard_runs(
                root=ROOT, shard_run_roots=(copies[0], nested), destination=scratch / "out"
            )
        self.assertFalse((scratch / "out").exists())

    def test_aggregate_rejects_shards_bound_to_different_metadata_or_inventory(self) -> None:
        scratch, copies = self._copies()
        metadata = copies[1] / "run-metadata.json"
        metadata.write_bytes(metadata.read_bytes() + b"\n")
        with self.assertRaisesRegex(
            QualificationRunError, "AGGREGATE_SHARD_BINDING_MISMATCH: run-metadata.json"
        ):
            aggregate_shard_runs(root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "a")
        scratch, copies = self._copies()
        metadata = copies[1] / "run-metadata.json"
        document = json.loads(metadata.read_text(encoding="utf-8"))
        document["codex_bin"] = "tampered"
        metadata.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(QualificationRunError, "AGGREGATE_SHARD_METADATA_INVALID"):
            aggregate_shard_runs(root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "c")
        scratch, copies = self._copies()
        (copies[2] / ("inventory-observation-" + "b" * 64 + ".json")).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(QualificationRunError, "AGGREGATE_INVENTORY_OBSERVATION_MISMATCH"):
            aggregate_shard_runs(root=ROOT, shard_run_roots=tuple(copies), destination=scratch / "b")
        self.assertFalse((scratch / "b").exists())


if __name__ == "__main__":
    unittest.main()
