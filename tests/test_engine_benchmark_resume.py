"""미완료 benchmark가 새 attempt·예산으로 중복 실행되는 회귀를 검사한다."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.benchmark import _reject_benchmark_partial_resume, run_benchmark
from flowmarshal.engine.evaluation import (
    EvaluationCellCheckpoint, EvaluationContract, EvaluationRunStatus,
    EvaluationScope, ImmutableCheckpointStore,
)
from flowmarshal.engine.domain import utc_now
from flowmarshal.engine.qualification import ORDER_SEEDS, PlanningScenarioCatalog, QualificationRunError
from tests.test_engine_benchmark_runner import POLICIES

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkResumeTests(unittest.TestCase):
    def prepare(self, destination: Path):
        catalog = PlanningScenarioCatalog.load(ROOT / "tests/fixtures/engine/planning-scenarios.json")
        neutral = {item.scenario_id: {"request": item.source_request} for item in catalog.scenarios}
        identities = {(item.scenario_id, implementation): sha256_digest({
            "scenario": item.scenario_digest, "implementation": implementation,
            "neutral": sha256_digest(neutral[item.scenario_id]),
        }) for item in catalog.scenarios for implementation in ("r31_baseline", "skeleton_engine")}
        digest = "sha256:" + "a" * 64
        contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2", scope=EvaluationScope.FULL_PLANNING_PIPELINE,
            fixture_digests=tuple(identities.values()), order_seeds=ORDER_SEEDS, expected_cell_count=36,
            scenario_set_digest=sha256_digest(neutral), role_configuration_digest=digest,
            source_manifest_digest=digest, rules_digest=digest, threshold_digest=digest,
            taxonomy_digest=digest, prompt_digest=digest, output_schema_digest=digest, model_lock_digest=digest,
        )
        store = ImmutableCheckpointStore(destination, contract)
        store.initialize()
        store.set_state(EvaluationRunStatus.FAILED, updated_at=utc_now(), reason="provider 결과 미확인")
        (destination / "neutral-inputs.json").write_text(json.dumps(neutral), encoding="utf-8")
        scenario = catalog.scenarios[0].scenario_id
        return store, contract, scenario, identities

    def test_incomplete_attempt_blocks_before_runtime_or_new_budget_and_preserves_state(self):
        for implementation in ("r31_baseline", "skeleton_engine"):
            with self.subTest(implementation=implementation), tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "run"
                store, _, scenario, _ = self.prepare(destination)
                attempt = destination / "work" / "seed-17" / scenario / implementation / ("attempt_" + "b" * 32)
                attempt.mkdir(parents=True)
                marker = attempt / "unresolved-provider-effect.json"
                marker.write_text('{"status":"usage_unknown"}', encoding="utf-8")
                before = {str(path.relative_to(destination)): path.read_bytes()
                          for path in destination.rglob("*") if path.is_file()}
                with patch("flowmarshal.engine.benchmark._preflight") as preflight, patch(
                    "flowmarshal.engine.benchmark.CodexAppServerRuntime"
                ) as runtime, self.assertRaisesRegex(
                    QualificationRunError, "BENCHMARK_PARTIAL_CELL_RECONCILIATION_REQUIRED"
                ):
                    run_benchmark(root=ROOT, run_root=destination, evaluation_policies=POLICIES)
                preflight.assert_not_called()
                runtime.assert_not_called()
                self.assertEqual(EvaluationRunStatus.FAILED, store.state().status)
                self.assertEqual(before, {str(path.relative_to(destination)): path.read_bytes()
                                          for path in destination.rglob("*") if path.is_file()})
                self.assertEqual([attempt], list(attempt.parent.iterdir()))

    def test_completed_cell_attempt_is_not_mistaken_for_partial_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "run"
            store, contract, scenario, identities = self.prepare(destination)
            (destination / "work" / "seed-17" / scenario / "skeleton_engine" / ("attempt_" + "c" * 32)).mkdir(parents=True)
            store.put(EvaluationCellCheckpoint(
                model_lock_format="flowmarshal-model-lock-v2", contract_digest=contract.contract_digest,
                fixture_digest=identities[scenario, "skeleton_engine"], order_seed=17,
                raw_structured_assessment={"synthetic_completed_checkpoint": True},
                runner_receipts=({"synthetic_receipt": True},),
            ))
            _reject_benchmark_partial_resume(ROOT, destination)

    def test_tampered_neutral_input_does_not_hide_partial_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "run"
            self.prepare(destination)
            (destination / "neutral-inputs.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(QualificationRunError, "BENCHMARK_RECOVERY_BINDING_INVALID"):
                _reject_benchmark_partial_resume(ROOT, destination)


if __name__ == "__main__":
    unittest.main()
