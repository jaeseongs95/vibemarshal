from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_bytes, sha256_digest
from flowmarshal.planning.r31_domain import (
    CandidateStatus,
    ConfidenceLevel,
    MissionPrimary,
    MissionSelectedBy,
    ModelCallReceipt,
    ModelCallStatus,
    MutationPolicy,
    BehaviorPreservation,
    PlanningMissionDefinition,
    PlanningRole,
    R31Model,
    RiskTag,
)
from flowmarshal.planning.r31_intent import (
    MissionResolutionHint,
    MissionResolutionHintKind,
    PlanningMissionResolver,
)
from flowmarshal.planning.r31_models import (
    ResolvedPlanningModel,
    StructuredRoleResult,
)
from flowmarshal.planning.r31_pipeline import RequiredPlanningRoleUnavailable, RoleOutput
from flowmarshal.planning.r31_role_adapters import (
    CandidateReviewResult,
    MissionPlanningService,
    ModelCallReceiptValidationError,
    RiskRoutedHardGateReviewer,
    StructuredApproachGenerator,
    StructuredCandidateExpander,
    StructuredHardGateReviewer,
    StructuredMissionProposer,
    _StructuredAdapter,
    _bind_unique_trace_offsets,
)
from flowmarshal.planning.r31_prototype import (
    FixtureApproachGenerator,
    FixtureCandidateExpander,
)
from flowmarshal.planning.r31_store import PlanningArtifactRepository
from tests.test_planner_r31_domain import candidate, planning_input, profile_revision, request_spec


class _FakeStructuredRunner:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def run(self, request, **kwargs):
        self.requests.append(request)
        payload = copy.deepcopy(self.payload)
        validator = kwargs.get("validator")
        if validator is not None:
            validator(payload)
        return StructuredRoleResult(
            payload=payload,
            receipt=ModelCallReceipt(
                call_id=f"model_call_{len(self.requests)}",
                role=request.role,
                model_id=request.model_id,
                reasoning_effort=request.reasoning_effort,
                inventory_digest=request.inventory_digest,
                input_digest=request.request_digest,
                output_schema_digest=sha256_digest(request.output_schema),
                output_digest=sha256_digest(payload),
                status=ModelCallStatus.SUCCEEDED,
                thread_id=f"thread_{len(self.requests)}",
                turn_ids=(f"turn_{len(self.requests)}",),
            ),
        )


class _ReceiptMutatingRunner(_FakeStructuredRunner):
    def __init__(self, payload, mutation):
        super().__init__(payload)
        self.mutation = mutation

    def run(self, request, **kwargs):
        result = super().run(request, **kwargs)
        receipt_data = result.receipt.model_dump(mode="python")
        receipt_data.update(self.mutation)
        return StructuredRoleResult(
            payload=result.payload,
            receipt=ModelCallReceipt.model_validate(receipt_data),
        )


def _resolved(role: PlanningRole) -> ResolvedPlanningModel:
    return ResolvedPlanningModel(
        role=role,
        model_id="fixture-model",
        reasoning_effort="medium",
        inventory_digest=sha256_bytes(b"inventory"),
    )


def _successful_role_output(value, role: PlanningRole, thread_id: str) -> RoleOutput:
    return RoleOutput(
        value,
        (
            ModelCallReceipt(
                call_id=f"model_call_{role.value}_{thread_id}",
                role=role,
                model_id="fixture-model",
                reasoning_effort="medium",
                inventory_digest=sha256_bytes(b"inventory"),
                input_digest=sha256_digest({"thread_id": thread_id}),
                output_schema_digest=sha256_digest({"type": "object"}),
                output_digest=sha256_digest(value),
                status=ModelCallStatus.SUCCEEDED,
                thread_id=thread_id,
                turn_ids=(f"turn_{thread_id}",),
            ),
        ),
    )


class PlannerR31RoleAdapterTests(unittest.TestCase):
    def test_trace_offsets_are_computed_from_unique_exact_excerpt(self) -> None:
        draft = {
            "traces": [
                {
                    "trace_id": "span.target",
                    "start_offset": 999,
                    "end_offset": 1000,
                    "excerpt": "두 번째 문장",
                }
            ]
        }
        raw_request = "첫 문장. 두 번째 문장."
        _bind_unique_trace_offsets(raw_request, draft)
        expected_start = raw_request.index("두 번째 문장")
        self.assertEqual(expected_start, draft["traces"][0]["start_offset"])
        self.assertEqual(
            expected_start + len("두 번째 문장"),
            draft["traces"][0]["end_offset"],
        )
        with self.assertRaisesRegex(ValueError, "matches=2"):
            _bind_unique_trace_offsets(
                "반복 반복",
                {"traces": [{"excerpt": "반복"}]},
            )

    def test_authoritative_output_binding_replaces_model_provenance(self) -> None:
        class BoundOutput(R31Model):
            source_digest: str
            statement: str

        expected_digest = sha256_bytes(b"authoritative-source")
        runner = _FakeStructuredRunner(
            {
                "source_digest": sha256_bytes(b"model-invented"),
                "statement": "모델이 판단한 의미 내용",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = _StructuredAdapter(
                runner=runner,
                model=_resolved(PlanningRole.PURPOSE_RESOLVER),
                cwd=directory,
                instructions="의미 내용만 판단한다.",
                expected_roles=frozenset({PlanningRole.PURPOSE_RESOLVER}),
            )
            result = adapter._call(
                {"source": "fixture"},
                BoundOutput,
                authoritative_bindings={"source_digest": expected_digest},
            )

        self.assertEqual(expected_digest, result.value.source_digest)
        self.assertEqual(
            expected_digest,
            runner.requests[0].payload["authoritative_output_bindings"]["source_digest"],
        )
        self.assertEqual(sha256_digest(result.value), result.receipts[0].output_digest)

    def test_structured_mission_proposal_is_typed_and_records_receipt(self) -> None:
        mission = PlanningMissionDefinition(
            primary=MissionPrimary.FEATURE_EXTENSION,
            observable_outcome="호환 기능 확장",
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=ConfidenceLevel.HIGH,
            mission_policy_id="mission-policy",
            mission_policy_version="v1",
        )
        payload = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=mission,
        ).model_dump(mode="json")
        runner = _FakeStructuredRunner(payload)
        with tempfile.TemporaryDirectory() as directory:
            adapter = StructuredMissionProposer(
                runner=runner,
                model=_resolved(PlanningRole.PURPOSE_RESOLVER),
                cwd=directory,
                instructions="Mission 후보를 typed JSON으로 제안한다.",
            )
            result = adapter.propose(request_spec(), profile_revision())
        self.assertEqual(MissionPrimary.FEATURE_EXTENSION, result.value.recommended.primary)
        self.assertEqual(1, len(result.receipts))
        self.assertEqual(PlanningRole.PURPOSE_RESOLVER, runner.requests[0].role)
        self.assertEqual(sha256_digest(result.value), result.receipts[0].output_digest)

    def test_preflight_receipt_sink_observes_typed_receipt_immediately(self) -> None:
        mission = PlanningMissionDefinition(
            primary=MissionPrimary.FEATURE_EXTENSION,
            observable_outcome="호환 기능 확장",
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=ConfidenceLevel.HIGH,
            mission_policy_id="mission-policy",
            mission_policy_version="v1",
        )
        hint = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=mission,
        )
        observed: list[ModelCallReceipt] = []
        runner = _FakeStructuredRunner(hint.model_dump(mode="json"))
        with tempfile.TemporaryDirectory() as directory:
            repository = PlanningArtifactRepository(directory)

            def persist(receipt: ModelCallReceipt) -> None:
                observed.append(receipt)
                repository.save_model_call_journal_receipt(receipt)

            adapter = StructuredMissionProposer(
                runner=runner,
                model=_resolved(PlanningRole.PURPOSE_RESOLVER),
                cwd=directory,
                instructions="Mission을 제안한다.",
                receipt_sink=persist,
            )
            result = adapter.propose(request_spec(), profile_revision())
            persisted = list(Path(directory).rglob("model-calls/*.json"))

        self.assertEqual(result.receipts, tuple(observed))
        self.assertEqual(sha256_digest(result.value), observed[0].output_digest)
        self.assertEqual(1, len(persisted))

    def test_structured_adapter_rejects_receipt_not_bound_to_actual_request(self) -> None:
        mission = PlanningMissionDefinition(
            primary=MissionPrimary.FEATURE_EXTENSION,
            observable_outcome="호환 기능 확장",
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=ConfidenceLevel.HIGH,
            mission_policy_id="mission-policy",
            mission_policy_version="v1",
        )
        payload = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=mission,
        ).model_dump(mode="json")
        mutations = {
            "role": {"role": PlanningRole.INTENT_REVIEWER},
            "model_id": {"model_id": "other-model"},
            "reasoning_effort": {"reasoning_effort": "high"},
            "inventory_digest": {
                "inventory_digest": sha256_bytes(b"other-inventory")
            },
            "input_digest": {"input_digest": sha256_bytes(b"other-input")},
            "output_schema_digest": {
                "output_schema_digest": sha256_bytes(b"other-schema")
            },
        }

        with tempfile.TemporaryDirectory() as directory:
            for name, mutation in mutations.items():
                with self.subTest(field=name):
                    observed: list[ModelCallReceipt] = []
                    adapter = StructuredMissionProposer(
                        runner=_ReceiptMutatingRunner(payload, mutation),
                        model=_resolved(PlanningRole.PURPOSE_RESOLVER),
                        cwd=directory,
                        instructions="Mission 후보를 typed JSON으로 제안한다.",
                        receipt_sink=observed.append,
                    )
                    with self.assertRaises(ModelCallReceiptValidationError) as raised:
                        adapter.propose(request_spec(), profile_revision())

                    self.assertEqual(1, len(observed))
                    self.assertEqual((observed[0],), raised.exception.receipts)

    def test_mission_service_enforces_reviewed_independent_resolution(self) -> None:
        request = request_spec()
        profile = profile_revision()
        proposed_mission = PlanningMissionDefinition(
            primary=MissionPrimary.FEATURE_EXTENSION,
            observable_outcome=request.request_summary,
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=ConfidenceLevel.HIGH,
            mission_policy_id="mission-policy",
            mission_policy_version="v1",
        )
        hint = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=proposed_mission,
        )

        class Proposer:
            def propose(self, request_spec, profile_revision):
                return _successful_role_output(
                    hint,
                    PlanningRole.PURPOSE_RESOLVER,
                    "mission_proposer",
                )

        class Reviewer:
            def __init__(self, thread_id: str):
                self.thread_id = thread_id

            def review(self, request_spec, profile_revision, proposal):
                self.asserted_proposal = proposal
                return _successful_role_output(
                    proposal,
                    PlanningRole.INTENT_REVIEWER,
                    self.thread_id,
                )

        reviewer = Reviewer("mission_reviewer")
        result = MissionPlanningService(
            resolver=PlanningMissionResolver(),
            proposer=Proposer(),
            reviewer=reviewer,
        ).resolve(request, profile)
        self.assertEqual(MissionPrimary.FEATURE_EXTENSION, result.mission_selection.mission.primary)
        self.assertIs(hint, reviewer.asserted_proposal)
        self.assertEqual(2, len(result.model_call_receipts))
        self.assertNotEqual(
            result.model_call_receipts[0].thread_id,
            result.model_call_receipts[1].thread_id,
        )

        same_thread_reviewer = Reviewer("mission_proposer")
        with self.assertRaises(ModelCallReceiptValidationError):
            MissionPlanningService(
                resolver=PlanningMissionResolver(),
                proposer=Proposer(),
                reviewer=same_thread_reviewer,
            ).resolve(request, profile)

    def test_mission_service_rejects_malformed_success_receipts(self) -> None:
        request = request_spec()
        profile = profile_revision()
        mission = PlanningMissionDefinition(
            primary=MissionPrimary.FEATURE_EXTENSION,
            observable_outcome=request.request_summary,
            mutation_policy=MutationPolicy.SCOPED_CHANGE,
            behavior_preservation=BehaviorPreservation.PRESERVE_PUBLIC_CONTRACTS,
            selected_by=MissionSelectedBy.INFERRED,
            confidence=ConfidenceLevel.HIGH,
            mission_policy_id="mission-policy",
            mission_policy_version="v1",
        )
        hint = MissionResolutionHint(
            kind=MissionResolutionHintKind.UNAMBIGUOUS,
            recommended=mission,
        )
        valid = _successful_role_output(
            hint,
            PlanningRole.PURPOSE_RESOLVER,
            "malformed_proposer",
        ).receipts[0]

        class Proposer:
            def __init__(self, receipt):
                self.receipt = receipt

            def propose(self, request_spec, profile_revision):
                return RoleOutput(hint, (self.receipt,))

        class Reviewer:
            def review(self, request_spec, profile_revision, proposal):
                return _successful_role_output(
                    proposal,
                    PlanningRole.INTENT_REVIEWER,
                    "malformed_reviewer",
                )

        malformed = {
            "wrong role": valid.model_copy(update={"role": PlanningRole.INTENT_REVIEWER}),
            "missing thread": valid.model_copy(update={"thread_id": None}),
            "missing turn": valid.model_copy(update={"turn_ids": ()}),
            "wrong output digest": valid.model_copy(
                update={"output_digest": sha256_bytes(b"different")}
            ),
            "failed status": ModelCallReceipt(
                **{
                    **valid.model_dump(mode="python", exclude={"output_digest"}),
                    "status": ModelCallStatus.FAILED,
                    "error_summary": "fixture failure",
                }
            ),
        }
        for label, receipt in malformed.items():
            with self.subTest(label=label):
                service = MissionPlanningService(
                    resolver=PlanningMissionResolver(),
                    proposer=Proposer(receipt),
                    reviewer=Reviewer(),
                )
                with self.assertRaises(ModelCallReceiptValidationError):
                    service.resolve(request, profile)

    def test_structured_approach_generator_enforces_runtime_limit(self) -> None:
        run_input = planning_input()
        run = FixtureApproachGenerator().generate
        # fixture generator에서 유효한 서로 다른 ApproachBrief 두 개를 얻는다.
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        receipt = PlanningRunReceipt(
            run_id="planning_run_adapter",
            planning_input=run_input,
            planning_input_digest=run_input.planning_input_digest,
            idempotency_key="adapter",
            status=PlanningRunStatus.FROZEN,
            created_at=NOW,
            updated_at=NOW,
        )
        approaches = run(receipt, limit=2).value
        runner = _FakeStructuredRunner(
            {"approaches": [item.model_dump(mode="json") for item in approaches]}
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = StructuredApproachGenerator(
                runner=runner,
                model=_resolved(PlanningRole.CANDIDATE_GENERATOR),
                cwd=directory,
                instructions="서로 다른 ApproachBrief를 만든다.",
            )
            result = adapter.generate(receipt, limit=2)
        self.assertEqual(2, len(result.value))
        self.assertEqual(2, runner.requests[0].payload["candidate_limit"])
        frozen_context = runner.requests[0].payload["planning_input"]
        self.assertNotIn("snapshot_id", frozen_context["project_snapshot"])
        self.assertNotIn("captured_at", frozen_context["project_snapshot"])
        self.assertNotIn(
            "source_project_snapshot_digest",
            frozen_context["requirement_extraction"],
        )
        self.assertNotIn("source_run_ids", frozen_context)

    def test_candidate_expander_rejects_unknown_context_inside_model_boundary(self) -> None:
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        run_input = planning_input()
        receipt = PlanningRunReceipt(
            run_id="planning_run_candidate_adapter",
            planning_input=run_input,
            planning_input_digest=run_input.planning_input_digest,
            idempotency_key="candidate-adapter",
            status=PlanningRunStatus.SEARCHING,
            created_at=NOW,
            updated_at=NOW,
        )
        base = candidate(
            candidate_id="candidate_structural_boundary",
            status=CandidateStatus.ADMISSIBLE,
            strategy_family="adapter",
            input_digest=run_input.planning_input_digest,
        )
        payload = base.model_dump(mode="json")
        payload["plan"]["work_items"][0]["context_sources"] = ["invented-source"]
        runner = _FakeStructuredRunner(payload)
        with tempfile.TemporaryDirectory() as directory:
            adapter = StructuredCandidateExpander(
                runner=runner,
                model=_resolved(PlanningRole.CANDIDATE_GENERATOR),
                cwd=directory,
                instructions="등록된 식별자만 사용한다.",
            )
            with self.assertRaisesRegex(ValueError, "deterministic PlanValidator"):
                adapter.expand(receipt, base.approach)

        identifier_contract = runner.requests[0].payload["plan_identifier_contract"]
        self.assertEqual(
            sorted(item.requirement_id for item in run_input.request_spec.requirements),
            identifier_contract["required_requirement_coverage_ids_exactly_once"],
        )

    def test_critical_risk_never_falls_back_to_general_reviewer(self) -> None:
        base = candidate(
            candidate_id="candidate_security",
            status="admissible",
            strategy_family="adapter",
            input_digest=planning_input().planning_input_digest,
        )
        risky = base.model_copy(
            update={
                "approach": base.approach.model_copy(
                    update={"risk_tags": (RiskTag.SECURITY_SENSITIVE,)}
                )
            }
        )

        class General:
            called = False

            def review(self, planning_run, value):
                self.called = True
                return RoleOutput(value)

        general = General()
        routed = RiskRoutedHardGateReviewer(general=general, critical=None)
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        input_value = planning_input()
        run_receipt = PlanningRunReceipt(
            run_id="planning_run_risk",
            planning_input=input_value,
            planning_input_digest=input_value.planning_input_digest,
            idempotency_key="risk",
            status=PlanningRunStatus.SEARCHING,
            created_at=NOW,
            updated_at=NOW,
        )
        with self.assertRaises(RequiredPlanningRoleUnavailable):
            routed.review(run_receipt, risky)
        self.assertFalse(general.called)

    def test_hard_gate_reviewer_receives_anonymous_candidate_contract(self) -> None:
        input_value = planning_input()
        value = candidate(
            candidate_id="candidate_blind_review",
            status="admissible",
            strategy_family="secret-generator-label",
            input_digest=input_value.planning_input_digest,
        )
        payload = CandidateReviewResult(
            status=value.status,
            quality_report=value.quality_report,
        ).model_dump(mode="json")
        runner = _FakeStructuredRunner(payload)
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        run_receipt = PlanningRunReceipt(
            run_id="planning_run_blind_review",
            planning_input=input_value,
            planning_input_digest=input_value.planning_input_digest,
            idempotency_key="blind-review",
            status=PlanningRunStatus.SEARCHING,
            created_at=NOW,
            updated_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = StructuredHardGateReviewer(
                runner=runner,
                model=_resolved(PlanningRole.HARD_GATE_REVIEWER),
                cwd=directory,
                instructions="후보 식별자 없이 Hard Gate를 검토한다.",
            )
            reviewed = adapter.review(run_receipt, value).value
        sent = runner.requests[0].payload
        serialized = json.dumps(sent, ensure_ascii=False)
        self.assertEqual(value.candidate_id, reviewed.candidate_id)
        self.assertNotIn(value.candidate_id, serialized)
        self.assertNotIn(value.approach.approach_id, serialized)
        self.assertNotIn(value.approach.rationale, serialized)
        self.assertIn("anonymous_candidate", sent)
        representation = sent["frozen_contract"]["representation_contract"]
        self.assertEqual([], representation["explicit_request_constraint_ids"])
        self.assertIn("kind=exclusion", representation["rule"])

    def test_hard_gate_reviewer_binds_derived_status_and_score_fields(self) -> None:
        input_value = planning_input()
        value = candidate(
            candidate_id="candidate_derived_review",
            status="admissible",
            strategy_family="derived-review",
            input_digest=input_value.planning_input_digest,
        )
        payload = CandidateReviewResult(
            status=value.status,
            quality_report=value.quality_report,
        ).model_dump(mode="json")
        payload["status"] = "blocked"
        payload["quality_report"]["plan_verdict"] = "blocked"
        payload["quality_report"]["fitness_score"] = 0
        payload["quality_report"]["weakest_work_item_ref"] = "invented-ref"
        payload["quality_report"]["weakest_work_item_rating"] = 0
        runner = _FakeStructuredRunner(payload)
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        run_receipt = PlanningRunReceipt(
            run_id="planning_run_derived_review",
            planning_input=input_value,
            planning_input_digest=input_value.planning_input_digest,
            idempotency_key="derived-review",
            status=PlanningRunStatus.SEARCHING,
            created_at=NOW,
            updated_at=NOW,
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = StructuredHardGateReviewer(
                runner=runner,
                model=_resolved(PlanningRole.HARD_GATE_REVIEWER),
                cwd=directory,
                instructions="Gate 근거만 의미 평가한다.",
            )
            reviewed = adapter.review(run_receipt, value).value

        self.assertEqual(CandidateStatus.ADMISSIBLE, reviewed.status)
        self.assertEqual(
            reviewed.quality_report.dimension_ratings.calculated_score,
            reviewed.quality_report.fitness_score,
        )
        self.assertEqual(
            min(
                item.work_item_ref
                for item in reviewed.quality_report.work_item_quality
                if item.weakest_rating
                == reviewed.quality_report.weakest_work_item_rating
            ),
            reviewed.quality_report.weakest_work_item_ref,
        )

    def test_migration_mission_routes_to_critical_even_without_risk_tag(self) -> None:
        from flowmarshal.planning.r31_domain import PlanningRunReceipt, PlanningRunStatus
        from tests.test_planner_r31_domain import NOW

        input_value = planning_input(primary=MissionPrimary.MIGRATION_MODERNIZATION)
        run_receipt = PlanningRunReceipt(
            run_id="planning_run_migration_routing",
            planning_input=input_value,
            planning_input_digest=input_value.planning_input_digest,
            idempotency_key="migration-routing",
            status=PlanningRunStatus.SEARCHING,
            created_at=NOW,
            updated_at=NOW,
        )
        approach = FixtureApproachGenerator().generate(run_receipt, limit=1).value[0]

        class RecordingReviewer:
            def __init__(self):
                self.called = False

            def review(self, planning_run, value):
                self.called = True
                return RoleOutput(value)

        general = RecordingReviewer()
        critical = RecordingReviewer()
        routed = RiskRoutedHardGateReviewer(general=general, critical=critical)
        value = FixtureCandidateExpander().expand(run_receipt, approach).value
        routed.review(run_receipt, value)
        self.assertFalse(general.called)
        self.assertTrue(critical.called)


if __name__ == "__main__":
    unittest.main()
