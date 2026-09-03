from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from flowmarshal.planning.r31_evaluation import (
    EvaluationFixture,
    EvaluationObservation,
    EvaluationObservationBatch,
    EvaluationScope,
    EvaluationSuite,
    FixtureOracle,
    FixtureVariant,
    MISSION_FIXTURE_FAMILIES,
    PLAN_QUALITY_FIXTURE_FAMILIES,
    build_builtin_fixtures,
    evaluate_fixture_observations,
    evaluate_repeated_runs,
    ordered_model_inputs,
)
from flowmarshal.planning.r31_eval_cli import (
    evaluate_observation_file,
    export_evaluation_inputs,
)


class PlannerR31EvaluationTests(unittest.TestCase):
    def test_fixture_catalog_has_required_family_count(self) -> None:
        self.assertGreaterEqual(len(MISSION_FIXTURE_FAMILIES), 12)
        self.assertEqual(12, len(PLAN_QUALITY_FIXTURE_FAMILIES))

    def test_builtin_catalog_has_clean_and_adversarial_variant_per_family(self) -> None:
        fixtures = build_builtin_fixtures()
        expected = 2 * (
            len(MISSION_FIXTURE_FAMILIES) + len(PLAN_QUALITY_FIXTURE_FAMILIES)
        )
        self.assertEqual(expected, len(fixtures))
        self.assertTrue(all("oracle" not in item.model_input() for item in fixtures))
        ambiguous = next(item for item in fixtures if item.case_id == "M02-clean")
        self.assertTrue(ambiguous.oracle.expected_question)
        self.assertFalse(ambiguous.oracle.should_admit)
        stale = next(item for item in fixtures if item.case_id == "M05-adversarial")
        self.assertEqual("stale", stale.prompt_input["project_profile"]["freshness"])
        self.assertTrue(
            stale.prompt_input["project_profile"]["source_digest_matches"]
        )
        existing_project_auth = next(
            item for item in fixtures if item.case_id == "M13-clean"
        )
        self.assertEqual(
            "feature_extension", existing_project_auth.oracle.expected_mission
        )

    def test_hidden_oracle_is_not_exposed_to_model(self) -> None:
        fixture = self._fixture()
        payload = fixture.model_input()
        self.assertNotIn("oracle", payload)
        self.assertNotIn("case_id", payload)
        self.assertTrue(payload["case_ref"].startswith("case-"))
        self.assertEqual("요청", payload["input"]["request"])

    def test_complete_observation_passes(self) -> None:
        fixture = self._fixture()
        observation = self._observation()
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertTrue(report.passed)
        self.assertEqual(1.0, report.metrics.requirement_recall)
        self.assertEqual(0, report.metrics.critical_false_admission_count)

    def test_hard_failure_and_missing_requirement_fail_report(self) -> None:
        fixture = self._fixture()
        observation = self._observation(
            matched_requirement_ids=(),
            admitted=True,
            hard_fail_candidate_scored=True,
        )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertFalse(report.passed)
        self.assertIn("필수 requirement recall 미달", report.failures)
        self.assertIn("critical 결함 후보가 admission됨", report.failures)
        self.assertIn("Hard Gate 실패 후보가 점수화됨", report.failures)

    def test_missing_observation_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_fixture_observations((self._fixture(),), ())

    def test_optional_correlated_defect_affects_precision_but_not_recall(self) -> None:
        fixture = self._fixture().model_copy(
            update={
                "oracle": self._fixture().oracle.model_copy(
                    update={"allowed_major_defects": ("VERIFICATION-2",)}
                )
            }
        )
        observation = self._observation(
            detected_major_defects=("SECURITY-1", "VERIFICATION-2")
        )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertTrue(report.passed)
        self.assertEqual(1.0, report.metrics.major_defect_recall)
        self.assertEqual(1.0, report.metrics.major_defect_precision)

    def test_expected_block_and_unnecessary_question_are_zero_tolerance(self) -> None:
        blocked_fixture = self._fixture().model_copy(
            update={
                "oracle": self._fixture().oracle.model_copy(
                    update={"expected_blocked": True}
                )
            }
        )
        observation = self._observation(blocked=False, question_asked=True)
        report = evaluate_fixture_observations((blocked_fixture,), (observation,))
        self.assertFalse(report.passed)
        self.assertIn("필수 blocked 판정을 놓침", report.failures)

    def test_clean_non_admission_counts_as_false_block(self) -> None:
        fixture = next(
            item for item in build_builtin_fixtures() if item.case_id == "P04-clean"
        )
        observation = EvaluationObservation(
            case_id=fixture.case_id,
            observed_mission="feature_extension",
            matched_requirement_ids=("req.plan",),
            admitted=False,
            blocked=False,
            first_pass_schema_valid=True,
            forward_reconstruction_complete=False,
        )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertFalse(report.passed)
        self.assertEqual(1, report.metrics.clean_false_block_count)

    def test_expected_integrity_block_does_not_count_as_mission_mismatch(self) -> None:
        fixture = next(
            item
            for item in build_builtin_fixtures()
            if item.case_id == "M10-adversarial"
        )
        observation = EvaluationObservation(
            case_id=fixture.case_id,
            observed_mission=None,
            matched_requirement_ids=("req.digest",),
            matched_exclusion_ids=("exc.implementation",),
            blocked=True,
            mission_resolved=False,
            admitted=False,
            stale_or_digest_mismatch_detected=True,
            detected_major_defects=("MISSION_DIGEST_MISMATCH",),
        )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertTrue(report.passed)
        self.assertEqual(0, report.metrics.mission_mismatch_count)

    def test_plan_scope_question_does_not_make_fixed_mission_ambiguous(self) -> None:
        fixture = next(
            item
            for item in build_builtin_fixtures()
            if item.case_id == "P08-adversarial"
        )
        observation = EvaluationObservation(
            case_id=fixture.case_id,
            observed_mission="feature_extension",
            matched_requirement_ids=("req.plan",),
            question_asked=True,
            blocked=True,
            mission_resolved=True,
            admitted=False,
            detected_structural_defects=("HUMAN_CHECKPOINT_MISSING",),
            detected_major_defects=("AUTHORITY_GAP",),
            distinct_candidate_count=0,
            forward_reconstruction_complete=False,
        )
        report = evaluate_fixture_observations((fixture,), (observation,))
        self.assertTrue(report.passed)
        self.assertEqual(0, report.metrics.ambiguous_mission_resolved_count)

    def test_order_variants_hide_oracle_and_repeated_critical_verdicts_must_match(self) -> None:
        fixtures = build_builtin_fixtures()
        first_order = ordered_model_inputs(fixtures, order_seed=1)
        second_order = ordered_model_inputs(fixtures, order_seed=2)
        self.assertNotEqual(
            tuple(item["case_ref"] for item in first_order),
            tuple(item["case_ref"] for item in second_order),
        )
        self.assertTrue(all("oracle" not in item for item in first_order))

        duplicate_fixture = next(
            item for item in fixtures if item.case_id == "P11-adversarial"
        )
        first_candidates = next(
            item["input"]["candidate_set"]
            for item in first_order
            if item["case_ref"] == duplicate_fixture.model_case_ref
        )
        second_candidates = next(
            item["input"]["candidate_set"]
            for item in second_order
            if item["case_ref"] == duplicate_fixture.model_case_ref
        )
        self.assertNotEqual(
            [item["candidate_ref"] for item in first_candidates],
            [item["candidate_ref"] for item in second_candidates],
        )

        fixture = self._fixture()
        clean = self._observation()
        repeated = evaluate_repeated_runs(
            (fixture,),
            (
                EvaluationObservationBatch(
                    run_id="run-1",
                    order_seed=1,
                    observations=(clean,),
                ),
                EvaluationObservationBatch(
                    run_id="run-2",
                    order_seed=2,
                    observations=(clean,),
                ),
            ),
        )
        self.assertTrue(repeated.passed)
        inconsistent = evaluate_repeated_runs(
            (fixture,),
            (
                EvaluationObservationBatch(
                    run_id="run-1",
                    order_seed=1,
                    observations=(clean,),
                ),
                EvaluationObservationBatch(
                    run_id="run-2",
                    order_seed=2,
                    observations=(self._observation(admitted=True),),
                ),
            ),
        )
        self.assertFalse(inconsistent.passed)
        self.assertEqual(1, inconsistent.critical_verdict_disagreement_count)

    def test_eval_cli_export_contains_no_hidden_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "model-inputs.json"
            export_evaluation_inputs(destination, (7, 11))
            exported_text = destination.read_text(encoding="utf-8")
            payload = json.loads(exported_text)
        self.assertFalse(payload["oracle_included"])
        self.assertEqual(2, len(payload["batches"]))
        self.assertNotIn('"oracle":', exported_text)
        self.assertNotIn("-clean", exported_text)
        self.assertNotIn("-adversarial", exported_text)

    def test_eval_cli_can_reassess_a_consistent_fixture_subset(self) -> None:
        observation = EvaluationObservation(
            case_id="M01-clean",
            observed_mission="feature_extension",
            matched_requirement_ids=("req.export", "req.compat"),
            matched_exclusion_ids=("exc.deploy",),
            admitted=True,
        )
        batch = EvaluationObservationBatch(
            run_id="subset-1",
            order_seed=1,
            observations=(observation,),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.json"
            path.write_text(
                json.dumps({"batches": [batch.model_dump(mode="json")]}),
                encoding="utf-8",
            )
            report = evaluate_observation_file(path)
        self.assertTrue(report.passed)
        self.assertEqual(1, report.run_reports[0].metrics.fixture_count)

    def test_role_probe_does_not_claim_mission_candidate_diversity(self) -> None:
        fixture = next(
            item for item in build_builtin_fixtures() if item.case_id == "M11-clean"
        )
        observation = EvaluationObservation(
            case_id=fixture.case_id,
            observed_mission="feature_extension",
            matched_requirement_ids=("req.compare",),
            matched_exclusion_ids=("exc.mixed-mission",),
            mission_resolved=True,
            admitted=True,
            distinct_candidate_count=1,
            stale_or_digest_mismatch_detected=False,
        )
        full = evaluate_fixture_observations((fixture,), (observation,))
        role_probe = evaluate_fixture_observations(
            (fixture,),
            (observation,),
            evaluation_scope=EvaluationScope.ROLE_FIXTURE_PROBE,
        )
        self.assertFalse(full.passed)
        self.assertIn("후보 다양성 성공률 미달", full.failures)
        self.assertTrue(role_probe.passed)
        self.assertEqual(EvaluationScope.ROLE_FIXTURE_PROBE, role_probe.evaluation_scope)

    def test_builtin_inputs_use_concrete_artifacts_without_answer_labels(self) -> None:
        fixtures = build_builtin_fixtures()
        mission_adversarial = next(
            item for item in fixtures if item.case_id == "M04-adversarial"
        )
        self.assertFalse(mission_adversarial.oracle.expected_blocked)
        self.assertTrue(mission_adversarial.oracle.request_must_override_profile)
        self.assertNotIn("profile_state", mission_adversarial.prompt_input)

        plan_adversarial = next(
            item for item in fixtures if item.case_id == "P05-adversarial"
        )
        self.assertNotIn("candidate_shape", plan_adversarial.prompt_input)
        self.assertTrue(plan_adversarial.prompt_input["candidate_set"])
        self.assertIn(
            "fixture literal",
            plan_adversarial.prompt_input["candidate_set"][0]["work_items"][0][
                "objective"
            ],
        )

        migration = next(
            item for item in fixtures if item.case_id == "P03-adversarial"
        )
        self.assertIn("RECOVERY_GAP", migration.oracle.allowed_major_defects)
        over_gated = next(
            item for item in fixtures if item.case_id == "P09-adversarial"
        )
        self.assertIn("VERIFICATION_GAP", over_gated.oracle.allowed_major_defects)
        self.assertNotIn("AUTHORITY_GAP", over_gated.oracle.allowed_major_defects)

        clean_dispatch = next(
            item for item in fixtures if item.case_id == "P04-clean"
        ).prompt_input["candidate_set"][0]
        integration_assertions = clean_dispatch["integration_validation"]["assertions"]
        self.assertIn(
            "같은 idempotency key는 하나의 binding만 만든다.",
            integration_assertions,
        )
        self.assertIn(
            "reconcile",
            clean_dispatch["work_items"][0]["failure_policy"],
        )
        clean_deletion = next(
            item for item in fixtures if item.case_id == "P08-clean"
        ).prompt_input["candidate_set"][0]
        self.assertIn(
            "각 Attempt는 고정 batch 크기와 최대 실행 시간을 넘지 않는다.",
            clean_deletion["integration_validation"]["assertions"],
        )

    @staticmethod
    def _fixture() -> EvaluationFixture:
        return EvaluationFixture(
            case_id="P10-adversarial",
            suite=EvaluationSuite.PLAN_QUALITY,
            variant=FixtureVariant.ADVERSARIAL,
            family="soft 고득점에 숨은 보안 결함",
            critical=True,
            prompt_input={
                "request": "요청",
                "source_clauses": [{"clause_id": "REQ-1", "text": "요청"}],
            },
            oracle=FixtureOracle(
                expected_mission="feature_extension",
                required_requirement_ids=("REQ-1",),
                expected_structural_defects=("STRUCT-1",),
                expected_major_defects=("SECURITY-1",),
                should_admit=False,
                minimum_distinct_candidates=1,
            ),
        )

    @staticmethod
    def _observation(**overrides) -> EvaluationObservation:
        values = {
            "case_id": "P10-adversarial",
            "observed_mission": "feature_extension",
            "matched_requirement_ids": ("REQ-1",),
            "detected_structural_defects": ("STRUCT-1",),
            "detected_major_defects": ("SECURITY-1",),
            "admitted": False,
            "scored_candidate_count": 0,
        }
        values.update(overrides)
        return EvaluationObservation(**values)


if __name__ == "__main__":
    unittest.main()
