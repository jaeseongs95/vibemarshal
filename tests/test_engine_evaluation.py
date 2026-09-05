from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    CandidateDecision,
    CandidateStatus,
    FindingSeverity,
    GateName,
    ReviewFinding,
    ReviewRatings,
    ReviewerSubmission,
    derive_candidate_decision,
)
from flowmarshal.engine.evaluation import (
    BenchmarkCell,
    CheckpointContractError,
    EvaluationCellCheckpoint,
    EvaluationContract,
    EvaluationScope,
    FixtureResult,
    ImmutableCheckpointStore,
    RegressionCatalog,
    evaluate_role_fixtures,
    evaluate_token_latency_gate,
    evaluate_cutover_gate,
    ScopeGateResult,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "engine" / "r31-reviewer-regressions.json"


def _cell(**values):
    values.setdefault("expected_disposition", "selected")
    values.setdefault("disposition", "selected")
    values.setdefault("latency_ms_to_disposition", values["latency_ms_to_first_feasible"])
    return BenchmarkCell(
        scenario_digest="sha256:" + "a" * 64,
        order_seed=1,
        model_lock_digest="sha256:" + "b" * 64,
        functional_result_digest="sha256:" + "c" * 64,
        runner_receipt_digest="sha256:" + "d" * 64,
        **values,
    )


def _result(fixture):
    digest = sha256_digest(fixture.artifact)
    findings = tuple(
        ReviewFinding(
            finding_code=code,
            gate=GateName.PLAN,
            severity=FindingSeverity.ERROR,
            summary=f"{code} 직접 증거",
            evidence_refs=(digest,),
            remediable=False,
        )
        for code in fixture.required_finding_codes
    )
    ratings = None
    if not findings:
        ratings = ReviewRatings(
            goal_fit=4,
            grounding=4,
            engineering=4,
            verification=4,
            execution_safety=4,
        )
    submission = ReviewerSubmission(
        reviewer_role="fixture-reviewer",
        candidate_digest=digest,
        findings=findings,
        ratings=ratings,
        evidence_catalog_digest="sha256:" + "a" * 64,
    )
    decision = derive_candidate_decision(
        candidate_digest=digest,
        findings=findings,
        ratings=ratings,
    )
    return FixtureResult(
        opaque_case_ref=fixture.opaque_case_ref,
        submission=submission,
        decision=decision,
    )


class EngineEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = RegressionCatalog.load(FIXTURE_PATH)

    def test_model_batch_hides_oracle_and_variant_labels(self) -> None:
        batch = self.catalog.model_batch()
        self.assertEqual(len(self.catalog.fixtures), len(batch))
        for item in batch:
            rendered = str(item)
            self.assertNotIn("fixture_id", rendered)
            self.assertNotIn("expected_admissible", rendered)
            self.assertNotIn("required_finding_codes", rendered)
            self.assertNotIn("known_r31_failure", rendered)
            self.assertRegex(item["case_ref"], r"^case-[0-9a-f]{16}$")

    def test_regression_catalog_passes_with_minimal_direct_findings(self) -> None:
        results = tuple(_result(item) for item in self.catalog.fixtures)
        report = evaluate_role_fixtures(
            catalog=self.catalog,
            results=results,
            prompt_digest="sha256:" + "1" * 64,
            output_schema_digest="sha256:" + "2" * 64,
            model_lock_digest="sha256:" + "3" * 64,
        )
        self.assertTrue(report.passed, report.failures)
        self.assertEqual(1.0, report.metrics.required_finding_recall)
        self.assertEqual(1.0, report.metrics.finding_precision)

    def test_recall_threshold_is_not_replaced_by_per_cell_perfection(self) -> None:
        template = next(item for item in self.catalog.fixtures if item.required_finding_codes)
        codes = tuple(f"DIRECT_FINDING_{index}" for index in range(100))
        fixture = template.model_copy(update={"required_finding_codes": codes})
        catalog = self.catalog.model_copy(update={"fixtures": (fixture,)})
        for detected_count, expected_pass in ((90, True), (89, False)):
            with self.subTest(detected_count=detected_count):
                observed = fixture.model_copy(update={"required_finding_codes": codes[:detected_count]})
                report = evaluate_role_fixtures(
                    catalog=catalog, results=(_result(observed),),
                    prompt_digest="sha256:" + "1" * 64,
                    output_schema_digest="sha256:" + "2" * 64,
                    model_lock_digest="sha256:" + "3" * 64,
                )
                self.assertEqual(expected_pass, report.passed)
                self.assertEqual(detected_count / 100, report.metrics.required_finding_recall)
                self.assertEqual(1, len(report.diagnostics))
                self.assertIn("필수 finding 누락", report.diagnostics[0])

    def test_p01_false_positive_is_counted_as_clean_block_and_precision_loss(self) -> None:
        results = list(_result(item) for item in self.catalog.fixtures)
        fixture = next(item for item in self.catalog.fixtures if item.fixture_id == "P01-clean")
        digest = sha256_digest(fixture.artifact)
        extra = ReviewFinding(
            finding_code="NON_IDEMPOTENT",
            gate=GateName.EXECUTION,
            severity=FindingSeverity.ERROR,
            summary="직접 증거 없는 과잉 진단",
            evidence_refs=(digest,),
            remediable=False,
        )
        submission = ReviewerSubmission(
            reviewer_role="fixture-reviewer",
            candidate_digest=digest,
            findings=(extra,),
            evidence_catalog_digest="sha256:" + "a" * 64,
        )
        replacement = FixtureResult(
            opaque_case_ref=fixture.opaque_case_ref,
            submission=submission,
            decision=derive_candidate_decision(
                candidate_digest=digest,
                findings=(extra,),
                ratings=None,
            ),
        )
        results = [replacement if item.opaque_case_ref == fixture.opaque_case_ref else item for item in results]
        report = evaluate_role_fixtures(
            catalog=self.catalog,
            results=tuple(results),
            prompt_digest="sha256:" + "1" * 64,
            output_schema_digest="sha256:" + "2" * 64,
            model_lock_digest="sha256:" + "3" * 64,
        )
        self.assertFalse(report.passed)
        self.assertEqual(1, report.metrics.clean_false_block_count)
        self.assertLess(report.metrics.finding_precision, 1.0)

    def test_token_latency_gate_uses_matched_full_pipeline_pairs(self) -> None:
        cells = (
            _cell(
                scenario_id="multi-1",
                path_kind="multi_path",
                implementation="r31_baseline",
                uncached_input_tokens=800,
                output_tokens=200,
                latency_ms_to_first_feasible=1000,
                detailed_task_count=10,
                unexecuted_detailed_task_count=4,
                candidate_output_tokens=200,
                discarded_candidate_output_tokens=100,
            ),
            _cell(
                scenario_id="multi-1",
                path_kind="multi_path",
                implementation="skeleton_engine",
                uncached_input_tokens=450,
                output_tokens=150,
                latency_ms_to_first_feasible=700,
                detailed_task_count=10,
                unexecuted_detailed_task_count=1,
                candidate_output_tokens=100,
                discarded_candidate_output_tokens=20,
            ),
            _cell(
                scenario_id="single-1",
                path_kind="single_path",
                implementation="r31_baseline",
                uncached_input_tokens=400,
                output_tokens=100,
                latency_ms_to_first_feasible=500,
                detailed_task_count=2,
                unexecuted_detailed_task_count=0,
                candidate_output_tokens=50,
                discarded_candidate_output_tokens=0,
            ),
            _cell(
                scenario_id="single-1",
                path_kind="single_path",
                implementation="skeleton_engine",
                uncached_input_tokens=380,
                output_tokens=100,
                latency_ms_to_first_feasible=350,
                detailed_task_count=2,
                unexecuted_detailed_task_count=0,
                candidate_output_tokens=40,
                discarded_candidate_output_tokens=0,
            ),
        )
        report = evaluate_token_latency_gate(cells, functional_gate_passed=True)
        self.assertTrue(report.passed, report.failures)
        self.assertGreaterEqual(report.multi_path_median_reduction, 0.30)
        self.assertLessEqual(report.worst_single_path_regression, 0.05)

    def test_checkpoint_reuse_requires_identical_immutable_contract(self) -> None:
        contract = EvaluationContract(
            model_lock_format="flowmarshal-model-lock-v2",
            scope=EvaluationScope.ROLE_FIXTURE,
            fixture_digests=tuple(item.fixture_digest for item in self.catalog.fixtures),
            scenario_set_digest=self.catalog.catalog_digest,
            order_seeds=(1,),
            expected_cell_count=len(self.catalog.fixtures),
            role_configuration_digest="sha256:" + "0" * 64,
            source_manifest_digest="sha256:" + "f" * 64,
            rules_digest="sha256:" + "1" * 64,
            threshold_digest="sha256:" + "2" * 64,
            taxonomy_digest="sha256:" + "3" * 64,
            prompt_digest="sha256:" + "4" * 64,
            output_schema_digest="sha256:" + "5" * 64,
            model_lock_digest="sha256:" + "6" * 64,
        )
        fixture_digest = self.catalog.fixtures[0].fixture_digest
        checkpoint = EvaluationCellCheckpoint(
            model_lock_format="flowmarshal-model-lock-v2",
            contract_digest=contract.contract_digest,
            fixture_digest=fixture_digest,
            order_seed=1,
            raw_structured_assessment={"findings": []},
            runner_receipts=({"call_id": "call-1", "status": "succeeded"},),
        )
        with tempfile.TemporaryDirectory() as temp:
            store = ImmutableCheckpointStore(temp, contract)
            store.initialize()
            store.put(checkpoint)
            self.assertEqual(
                checkpoint.checkpoint_digest,
                store.completed(fixture_digest, 1).checkpoint_digest,  # type: ignore[union-attr]
            )
            changed = checkpoint.model_copy(
                update={"raw_structured_assessment": {"findings": ["changed"]}}
            )
            with self.assertRaisesRegex(CheckpointContractError, "덮어쓸"):
                store.put(changed)
            incompatible = contract.model_copy(
                update={"prompt_digest": "sha256:" + "9" * 64}
            )
            with self.assertRaisesRegex(CheckpointContractError, "계약"):
                ImmutableCheckpointStore(temp, incompatible).initialize()

    def test_cutover_requires_four_independent_scopes(self) -> None:
        cells = (
            _cell(
                scenario_id="multi",
                path_kind="multi_path",
                implementation="r31_baseline",
                uncached_input_tokens=800,
                output_tokens=200,
                latency_ms_to_first_feasible=1000,
                detailed_task_count=10,
                unexecuted_detailed_task_count=5,
                candidate_output_tokens=200,
                discarded_candidate_output_tokens=100,
            ),
            _cell(
                scenario_id="multi",
                path_kind="multi_path",
                implementation="skeleton_engine",
                uncached_input_tokens=400,
                output_tokens=100,
                latency_ms_to_first_feasible=700,
                detailed_task_count=10,
                unexecuted_detailed_task_count=1,
                candidate_output_tokens=100,
                discarded_candidate_output_tokens=20,
            ),
        )
        token_gate = evaluate_token_latency_gate(cells, functional_gate_passed=True)
        result = ScopeGateResult(
            scope=EvaluationScope.DETERMINISTIC,
            contract_digest="sha256:" + "1" * 64,
            artifact_digest="sha256:" + "2" * 64,
            passed=True,
        )
        cutover = evaluate_cutover_gate(scope_results=(result,), token_latency_gate=token_gate)
        self.assertFalse(cutover.passed)
        self.assertEqual(3, len(cutover.missing_scopes))

    def test_token_gate_rejects_unmatched_or_mislabeled_pairs(self) -> None:
        baseline = _cell(
            scenario_id="scenario",
            path_kind="multi_path",
            implementation="r31_baseline",
            uncached_input_tokens=10,
            output_tokens=1,
            latency_ms_to_first_feasible=10,
            detailed_task_count=1,
            unexecuted_detailed_task_count=0,
            candidate_output_tokens=1,
            discarded_candidate_output_tokens=0,
        )
        with self.assertRaisesRegex(ValueError, "불완전"):
            evaluate_token_latency_gate((baseline,), functional_gate_passed=True)
        mismatched = baseline.model_copy(
            update={"implementation": "skeleton_engine", "path_kind": "single_path"}
        )
        with self.assertRaisesRegex(ValueError, "path_kind"):
            evaluate_token_latency_gate((baseline, mismatched), functional_gate_passed=True)


if __name__ == "__main__":
    unittest.main()
