from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.context import (
    AdditionalContextRequest,
    ContextNeed,
    ProjectMapper,
    resolve_additional_context_request,
)
from flowmarshal.engine.domain import (
    CandidateStatus,
    EvidenceKind,
    EvidenceRecord,
    FailureClass,
    PlanContractRevision,
    PlanSkeletonCandidate,
    RepairAction,
    RecoveryAssessment,
    RevisionStatus,
    RunOnceAction,
    ThreadBinding,
    derive_candidate_decision,
    new_id,
    utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.execution import ExecutionProposalAdapter
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.planning import (
    ExpandedPlanEvaluation,
    plan_gate,
    plan_review_evidence_catalog,
)
from flowmarshal.engine.recovery import (
    EvidenceFirstFailureClassifier,
    FailureSignal,
)
from flowmarshal.engine.roles import ScriptedStructuredRoleRunner
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.service import EngineServiceError
from tests.test_engine_qualification import qualification_inventory
from tests.engine_helpers import clean_review


ROOT = Path(__file__).resolve().parents[1]


class EvidenceFirstClassifierTests(unittest.TestCase):
    def test_explicit_codes_and_transport_precede_terminal_semantics(self) -> None:
        classifier = EvidenceFirstFailureClassifier()
        cases = (
            ({"error_code": "CONTEXT_REQUIRED"}, FailureClass.CONTEXT),
            ({"code": "PERMISSION_POLICY_MISMATCH"}, FailureClass.ENVIRONMENT),
            ({"transport_error": {"message": "socket closed"}}, FailureClass.EXTERNAL_UNKNOWN),
            ({"effect_status": "unknown"}, FailureClass.EXTERNAL_UNKNOWN),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                diagnosis = classifier.classify(FailureSignal(
                    terminal_status="failed",
                    final_response="worker failed",
                    provider_payload=payload,
                ))
                self.assertEqual(expected, diagnosis.failure_class)
                self.assertNotEqual(FailureClass.IMPLEMENTATION, diagnosis.failure_class)

    def test_failed_terminal_without_direct_evidence_stays_unclassified(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="작업을 마치지 못했습니다.",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)

    def test_model_reported_code_is_diagnostic_only(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="RATE_LIMITED: 모델이 추측한 제한",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)
        self.assertEqual(("RATE_LIMITED",), diagnosis.model_reported_codes)

    def test_model_reported_korean_failure_is_not_direct_evidence(self) -> None:
        diagnosis = EvidenceFirstFailureClassifier().classify(FailureSignal(
            terminal_status="failed",
            final_response="구현 실패",
            provider_payload={"thread_id": "thread"},
        ))
        self.assertIsNone(diagnosis.failure_class)
        self.assertEqual("unclassified", diagnosis.source)


class AutomaticRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.inventory = qualification_inventory()
        self.roles = default_role_configuration(ROOT)

    def prepared(self, name="work"):
        base = self.base / name
        base.mkdir()
        workspace, _ = _copy_fixture(ROOT, base)
        prepared = _prepare(
            workspace=workspace,
            state_root=base / "state",
            inventory=self.inventory,
            roles=self.roles,
        )
        return prepared, FakeCodexRuntime(self.inventory)

    def _failed_attempt(self, prepared, runtime, dispatcher, response):
        dispatcher.run_once(prepared.project_id, proposal=prepared.proposal)
        dispatched = dispatcher.run_once(prepared.project_id)
        with prepared.service.ledger.read() as connection:
            row = connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
            ).fetchone()
        binding = ThreadBinding.model_validate_json(row["binding_json"])
        error_code = response.partition(":")[0] if ":" in response else None
        runtime.fail(binding.thread_id, response=response, error_code=error_code)
        observed = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.OBSERVED, observed.action)
        return dispatched

    def _finish_runtime_job_tick(self, dispatcher, project_id):
        """job 예약/관측/소비를 서로 다른 scheduler tick으로 진행한다."""

        outcome = dispatcher.run_once(project_id)
        for _ in range(4):
            if outcome.action not in {
                RunOnceAction.DISPATCHED,
                RunOnceAction.OBSERVED,
            }:
                return outcome
            outcome = dispatcher.run_once(project_id)
        self.fail("runtime job이 bounded tick 안에서 terminal 소비로 수렴하지 않았습니다.")

    def test_fault_injection_repairs_without_manual_assessment_and_revalidates(self) -> None:
        prepared, runtime = self.prepared(name="automatic-repair")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        first = self._failed_attempt(
            prepared, runtime, dispatcher, "IMPLEMENTATION_ERROR: injected fault"
        )

        recovered = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, recovered.action)
        with prepared.service.ledger.read() as connection:
            assessment = connection.execute(
                "SELECT payload_json FROM recovery_assessments"
            ).fetchone()
            job = connection.execute(
                "SELECT kind,status FROM runtime_jobs WHERE kind='recovery'"
            ).fetchone()
        self.assertEqual(first.attempt_id, json.loads(assessment["payload_json"])["attempt_id"])
        self.assertEqual(("recovery", "consumed"), (job["kind"], job["status"]))

        second = dispatcher.run_once(prepared.project_id)
        self.assertEqual(RunOnceAction.DISPATCHED, second.action)
        with prepared.service.ledger.read() as connection:
            binding = ThreadBinding.model_validate_json(connection.execute(
                "SELECT binding_json FROM attempts WHERE id=?", (second.attempt_id,)
            ).fetchone()["binding_json"])
        (prepared.workspace / "app.py").write_text(
            "def add(left: int, right: int) -> int:\n    return left + right\n",
            encoding="utf-8",
        )
        runtime.complete(binding.thread_id, response="repair complete")
        self.assertEqual(RunOnceAction.OBSERVED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.VALIDATED, dispatcher.run_once(prepared.project_id).action)
        self.assertEqual(RunOnceAction.COMPLETED, dispatcher.run_once(prepared.project_id).action)

    def test_repeated_failure_stops_at_task_limit_with_fresh_evidence_each_time(self) -> None:
        prepared, runtime = self.prepared(name="recovery-limit")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self._failed_attempt(prepared, runtime, dispatcher, "IMPLEMENTATION_ERROR: same fault")
        self.assertEqual(
            RunOnceAction.RECOVERED,
            self._finish_runtime_job_tick(dispatcher, prepared.project_id).action,
        )

        for ordinal in (2, 3):
            dispatched = dispatcher.run_once(prepared.project_id)
            with prepared.service.ledger.read() as connection:
                binding = ThreadBinding.model_validate_json(connection.execute(
                    "SELECT binding_json FROM attempts WHERE id=?", (dispatched.attempt_id,)
                ).fetchone()["binding_json"])
            runtime.fail(
                binding.thread_id,
                response="IMPLEMENTATION_ERROR: same fault",
                error_code="IMPLEMENTATION_ERROR",
            )
            dispatcher.run_once(prepared.project_id)
            outcome = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
            if ordinal == 2:
                self.assertEqual(RunOnceAction.RECOVERED, outcome.action)
            else:
                self.assertEqual(RunOnceAction.BLOCKED, outcome.action)
                self.assertEqual("SAME_FAILURE_RECOVERY_LIMIT", outcome.blocker_code)

    def test_scope_expansion_is_asked_instead_of_automatically_replanned(self) -> None:
        prepared, runtime = self.prepared(name="scope-expansion")
        dispatcher = EngineDispatcher(prepared.service, runtime)
        self._failed_attempt(
            prepared, runtime, dispatcher,
            "SCOPE_EXPANSION_REQUIRED: another project is required",
        )
        blocked = dispatcher.run_once(prepared.project_id)
        self.assertEqual("AUTHORIZATION_EXPANSION_REQUIRED", blocked.blocker_code)
        self.assertEqual(FailureClass.REQUIREMENT_CHANGE, blocked.failure_class)
        with prepared.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute(
                "SELECT COUNT(*) FROM recovery_assessments"
            ).fetchone()[0])

    def test_subgraph_replan_is_reviewed_activated_and_keeps_checkpoints(self) -> None:
        prepared, runtime = self.prepared(name="subgraph-replan")
        service = prepared.service
        with service.ledger.read() as connection:
            old_plan = PlanContractRevision.model_validate_json(connection.execute(
                "SELECT payload_json FROM plan_revisions WHERE id=?",
                (prepared.plan_revision_id,),
            ).fetchone()["payload_json"])
            skeleton = PlanSkeletonCandidate.model_validate_json(connection.execute(
                "SELECT payload_json FROM skeleton_candidates WHERE candidate_digest=?",
                (old_plan.definition.source_skeleton_digest,),
            ).fetchone()["payload_json"])
            project_map = service.load_current_project_map(prepared.project_id)
            goal = service.load_active_goal(prepared.project_id)
            state = service.load_current_state(
                prepared.project_id, goal.definition_digest
            )
        replacement_task = old_plan.definition.tasks[0].model_copy(
            update={"task_id": new_id("task")}
        )
        coverage = tuple(
            item.model_copy(update={
                "task_ids": tuple(
                    replacement_task.task_id if task_id == prepared.task_id else task_id
                    for task_id in item.task_ids
                )
            })
            for item in old_plan.definition.goal_coverage
        )
        definition = old_plan.definition.model_copy(update={
            "tasks": (replacement_task,),
            "goal_coverage": coverage,
        })
        replacement = PlanContractRevision(
            plan_revision_id=new_id("plan_revision"),
            plan_id=old_plan.plan_id,
            revision_no=old_plan.revision_no + 1,
            definition=definition,
            definition_digest=definition.definition_digest,
            status=RevisionStatus.READY,
            supersedes_plan_revision_id=old_plan.plan_revision_id,
            created_at=utc_now(),
        )
        deterministic = plan_gate(
            replacement,
            source=skeleton,
            goal=goal,
            state=state,
            project_map=project_map,
        )
        catalog = plan_review_evidence_catalog(
            replacement, goal, state, project_map
        )
        review = clean_review(
            replacement.activation_digest,
            role="independent_recovery_reviewer",
            evidence_catalog=catalog,
        )
        decision = derive_candidate_decision(
            candidate_digest=replacement.activation_digest,
            findings=deterministic + review.findings,
            ratings=review.ratings if not deterministic else None,
        )
        self.assertEqual(CandidateStatus.ADMISSIBLE, decision.status)
        evaluation = ExpandedPlanEvaluation(
            plan=replacement,
            deterministic_findings=deterministic,
            semantic_submissions=(review,),
            decision=decision,
        )

        class Provider:
            def replan(self, **_kwargs):
                return evaluation

        dispatcher = EngineDispatcher(
            service, runtime, recovery_provider=Provider()
        )
        self._failed_attempt(
            prepared,
            runtime,
            dispatcher,
            "TASK_CONTRACT_INVALID: injected contract defect",
        )
        assessed = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, assessed.action)
        activated = self._finish_runtime_job_tick(dispatcher, prepared.project_id)
        self.assertEqual(RunOnceAction.RECOVERED, activated.action)
        with service.ledger.read() as connection:
            project = connection.execute(
                "SELECT active_plan_revision_id FROM projects WHERE id=?",
                (prepared.project_id,),
            ).fetchone()
            jobs = connection.execute(
                "SELECT kind,status FROM runtime_jobs WHERE kind IN ('recovery','replanning') "
                "ORDER BY created_at,rowid"
            ).fetchall()
            reviews = connection.execute(
                "SELECT reviewer_role FROM candidate_reviews WHERE artifact_kind='plan' "
                "AND artifact_digest=?",
                (replacement.activation_digest,),
            ).fetchall()
        self.assertEqual(replacement.plan_revision_id, project["active_plan_revision_id"])
        self.assertEqual(
            [("recovery", "consumed"), ("replanning", "consumed")],
            [(row["kind"], row["status"]) for row in jobs],
        )
        self.assertEqual(
            ["independent_recovery_reviewer"],
            [row["reviewer_role"] for row in reviews],
        )

    def test_goal_wide_replan_limit_and_new_evidence_are_ledger_derived(self) -> None:
        prepared, _runtime = self.prepared(name="goal-replan-limit")
        service = prepared.service
        service.compile_execution_spec(prepared.proposal, inventory=self.inventory)
        attempt = service.reserve_attempt(task_id=prepared.task_id)

        def evidence(label: str) -> str:
            item = EvidenceRecord(
                evidence_id=new_id("evidence"),
                project_id=prepared.project_id,
                task_id=prepared.task_id,
                attempt_id=attempt.attempt_id,
                kind=EvidenceKind.TEST,
                source_ref=f"fixture:{label}",
                observation=json.dumps({"passed": False, "label": label}),
                content_digest=sha256_digest({"label": label}),
                observed_at=utc_now(),
            )
            service.record_evidence(item)
            return item.evidence_id

        first_evidence = evidence("first")
        service.record_recovery_assessment(
            prepared.project_id,
            RecoveryAssessment(
                assessment_id=new_id("recovery_assessment"),
                attempt_id=attempt.attempt_id,
                failure_class=FailureClass.TASK_CONTRACT,
                action=RepairAction.SUBGRAPH_REPLAN,
                rationale="첫 재계획",
                failure_fingerprint=sha256_digest("same-failure"),
                new_evidence_ids=(first_evidence,),
                same_failure_replan_count=1,
                goal_replan_count=1,
            ),
        )
        with self.assertRaisesRegex(EngineServiceError, "새 evidence"):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="같은 evidence로 반복",
                    failure_fingerprint=sha256_digest("same-failure"),
                    new_evidence_ids=(),
                    same_failure_replan_count=2,
                    goal_replan_count=2,
                ),
            )

        for ordinal in range(2, 6):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale=f"독립 실패 {ordinal}",
                    failure_fingerprint=sha256_digest(f"failure-{ordinal}"),
                    new_evidence_ids=(evidence(f"fresh-{ordinal}"),),
                    same_failure_replan_count=1,
                    goal_replan_count=ordinal,
                ),
            )
        with self.assertRaisesRegex(EngineServiceError, "Goal 전체 재계획 한도 5회"):
            service.record_recovery_assessment(
                prepared.project_id,
                RecoveryAssessment(
                    assessment_id=new_id("recovery_assessment"),
                    attempt_id=attempt.attempt_id,
                    failure_class=FailureClass.TASK_CONTRACT,
                    action=RepairAction.SUBGRAPH_REPLAN,
                    rationale="여섯 번째 독립 실패",
                    failure_fingerprint=sha256_digest("failure-6"),
                    new_evidence_ids=(evidence("fresh-6"),),
                    same_failure_replan_count=1,
                    goal_replan_count=6,
                ),
            )


class AutomaticContextResolutionTests(unittest.TestCase):
    def test_project_map_searches_beyond_initial_sample_and_reinvokes_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            (workspace / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
            (workspace / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
            (workspace / "test_app.py").write_text("def test_add(): pass\n", encoding="utf-8")
            for index in range(15):
                (workspace / f"sample_{index:02}.txt").write_text("sample", encoding="utf-8")
            hidden = workspace / "z_hidden.py"
            hidden.write_text("def hidden_selector():\n    return 42\n", encoding="utf-8")
            inventory = qualification_inventory()
            roles = default_role_configuration(ROOT)
            prepared = _prepare(
                workspace=workspace,
                state_root=Path(temp) / "state",
                inventory=inventory,
                roles=roles,
            )
            proposal = prepared.proposal.model_dump(mode="json")
            for step in proposal["validation_steps"]:
                step.pop("method")
                step.pop("required_evidence_kinds")
            response = {"proposal": proposal, "context_request": None}
            request = {
                "proposal": None,
                "context_request": {
                    "task_id": prepared.task_id,
                    "missing_needs": [{
                        "need_id": "hidden",
                        "description": "hidden_selector 구현 본문",
                        "path_hints": [],
                        "symbol_hints": ["hidden_selector"],
                        "tag_hints": [],
                        "required": True,
                    }],
                    "reason": "초기 관측 sample에 본문이 없습니다.",
                },
            }
            runner = ScriptedStructuredRoleRunner({
                "execution_preparation": [request, response]
            })
            adapter = ExecutionProposalAdapter(prepared.service, runner, roles)

            result = adapter.prepare_task(
                project_id=prepared.project_id,
                task_id=prepared.task_id,
                inventory=inventory,
            )

            self.assertIsNotNone(result.proposal)
            self.assertEqual(2, len(runner.calls))
            additional = runner.calls[1].payload["additional_context"]
            self.assertEqual("z_hidden.py", additional[0]["source_ref"])
            self.assertIn("hidden_selector", additional[0]["content"])

    def test_preference_request_is_not_resolved_from_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "choice.txt").write_text("blue", encoding="utf-8")
            project_map = ProjectMapper().build(
                project_id="project_" + "1" * 32,
                root=root,
                revision_no=1,
            )
            request = AdditionalContextRequest(
                task_id="task_" + "2" * 32,
                missing_needs=(ContextNeed(
                    need_id="preference",
                    description="색상 선택",
                    path_hints=("choice.txt",),
                ),),
                reason="사용자 선호가 필요합니다.",
            )
            resolution = resolve_additional_context_request(
                project_map=project_map, request=request
            )
            self.assertEqual((), resolution.resolved)
            self.assertEqual(request, resolution.unresolved_request)


if __name__ == "__main__":
    unittest.main()
