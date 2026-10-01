"""VM E 합성 입력 shard. 실제 provider/command 실행 없이 선언 API와 기존 adapter를 검사한다."""
import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from flowmarshal.canonical import sha256_digest
from flowmarshal.engine.domain import (
    AttemptKind, DependencyType, EvidenceKind, EvidenceRecord, FailureClass, PlanContractDefinition,
    PlanContractRevision, PlanDependency, PlanSkeletonCandidate,
    RunOnceAction, SemanticValidationObservation, ValidationResult, ValidationStatus, new_id, utc_now,
)
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.service import EngineServiceError
from tests.test_engine_domain import _skeleton
from tests.test_engine_ledger_service import EngineServiceFixture
from tests.test_engine_qualification import qualification_inventory
from tests.fixtures.engine.governance import multitask


class InvalidGraphInputs(EngineServiceFixture):
    def test_skeleton_cycles_for_all_declared_edge_types(self):
        for kind in DependencyType:
            with self.subTest(kind=kind):
                payload = _skeleton().model_dump(mode='json')
                payload['tasks'][0]['consumes'] = ['data:b']
                payload['dependencies'][0]['dependency_type'] = kind.value
                payload['dependencies'].append(dict(producer_task_ref='task_b',
                    consumer_task_ref='task_a', dependency_type=kind.value,
                    produces=['data:b'], consumes=['data:b']))
                with self.assertRaisesRegex(ValidationError, 'cycle'):
                    PlanSkeletonCandidate.model_validate(payload)

    def test_plan_cycles_for_all_declared_edge_types(self):
        base = self.plan.definition.model_dump(mode='json')
        second = copy.deepcopy(base['tasks'][0])
        second.update(task_id=new_id('task'), task_ref='task_two',
                      produces=['result:two'], consumes=['result:one'])
        second['validations'][0]['validation_id'] = 'validation_two'
        base['tasks'][0]['consumes'] = ['result:two']
        base['tasks'].append(second)
        for kind in DependencyType:
            with self.subTest(kind=kind):
                payload = copy.deepcopy(base)
                a, b = (task['task_id'] for task in payload['tasks'])
                payload['dependencies'] = [dict(producer_task_id=a, consumer_task_id=b,
                    dependency_type=kind.value, products=['result:one']),
                    dict(producer_task_id=b, consumer_task_id=a,
                    dependency_type=kind.value, products=['result:two'])]
                with self.assertRaisesRegex(ValidationError, 'cycle'):
                    PlanContractDefinition.model_validate(payload)

    def test_unknown_task_and_self_dependency(self):
        for consumer in (new_id('task'), self.task.task_id):
            with self.subTest(consumer=consumer):
                payload = self.plan.definition.model_dump(mode='json')
                payload['dependencies'] = [dict(producer_task_id=self.task.task_id,
                    consumer_task_id=consumer, dependency_type='control', products=[])]
                with self.assertRaises(ValidationError):
                    PlanContractDefinition.model_validate(payload)

    def test_declared_api_rejects_conditional_edge_fields(self):
        skeleton_payload = _skeleton().model_dump(mode='json')
        skeleton_payload['dependencies'][0]['condition'] = 'producer failed OR advisor approved'
        with self.assertRaisesRegex(ValidationError, 'extra_forbidden'):
            PlanSkeletonCandidate.model_validate(skeleton_payload)
        with self.assertRaisesRegex(ValidationError, 'extra_forbidden'):
            PlanDependency.model_validate(dict(producer_task_id=self.task.task_id,
                consumer_task_id=new_id('task'), dependency_type='control', products=[],
                condition='advisor says GO'))

    def test_made_up_handoff_product_is_rejected(self):
        payload = _skeleton().model_dump(mode='json')
        payload['dependencies'][0].update(produces=['data:invented'], consumes=['data:invented'])
        with self.assertRaises(ValidationError):
            PlanSkeletonCandidate.model_validate(payload)

    def test_duplicate_task_and_duplicate_dependency_are_rejected(self):
        for duplicate in ('tasks', 'dependencies'):
            with self.subTest(duplicate=duplicate):
                payload = _skeleton().model_dump(mode='json')
                payload[duplicate].append(copy.deepcopy(payload[duplicate][0]))
                with self.assertRaises(ValidationError):
                    PlanSkeletonCandidate.model_validate(payload)

    def test_revision_digest_and_self_supersession_are_rejected(self):
        for updates in (dict(definition_digest='sha256:' + '0' * 64),
                        dict(supersedes_plan_revision_id=self.plan.plan_revision_id)):
            with self.subTest(updates=updates):
                payload = self.plan.model_dump(mode='json') | updates
                with self.assertRaises(ValidationError):
                    PlanContractRevision.model_validate(payload)


class DependencyReadiness(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.inventory = qualification_inventory()
        workspace = multitask.copy_fixture(base)
        self.prepared = multitask.prepare(workspace=workspace, state_root=base/'state',
            inventory=self.inventory, roles=default_role_configuration(Path.cwd()))
        self.service = self.prepared.service
        self.ids = list(self.prepared.task_ids.values())

    def compile(self, task_id):
        proposal = multitask.MultitaskProposals(self.prepared).prepare_task(
            project_id=self.prepared.project_id, task_id=task_id, inventory=self.inventory).proposal
        return self.service.compile_execution_spec(proposal, inventory=self.inventory)

    def test_pending_downstream_spec_is_rejected(self):
        self.assertEqual((self.ids[0],), self.service.list_ready_tasks(self.prepared.project_id))
        with self.assertRaisesRegex(EngineServiceError, 'ready'):
            self.compile(self.ids[1])
        with self.service.ledger.read() as conn:
            self.assertEqual(0, conn.execute('SELECT COUNT(*) FROM execution_spec_revisions').fetchone()[0])

    def test_failed_worker_does_not_unlock_downstream_task(self):
        self.compile(self.ids[0])
        attempt = self.service.reserve_attempt(task_id=self.ids[0])
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=False,
                                    failure_class=FailureClass.IMPLEMENTATION)
        self.assertNotIn(self.ids[1], self.service.list_ready_tasks(self.prepared.project_id))
        with self.assertRaises(EngineServiceError):
            self.service.complete_task(self.ids[0])

    def test_failed_validation_does_not_unlock_downstream_task(self):
        self.compile(self.ids[0])
        attempt = self.service.reserve_attempt(task_id=self.ids[0])
        self.service.finish_attempt(attempt_id=attempt.attempt_id, succeeded=True)
        evidence = EvidenceRecord(evidence_id=new_id('evidence'),
            project_id=self.prepared.project_id, task_id=self.ids[0], attempt_id=attempt.attempt_id,
            kind=EvidenceKind.TEST, source_ref='synthetic:failed-test', observation='failed',
            content_digest=sha256_digest('failed'), observed_at=utc_now())
        self.service.record_evidence(evidence)
        result = ValidationResult(validation_result_id=new_id('validation_result'),
            validation_id='validation_test_app', task_id=self.ids[0], status=ValidationStatus.FAIL,
            evidence_ids=(evidence.evidence_id,), rationale='합성 FAIL', evaluated_at=utc_now())
        with self.service.ledger.read() as conn:
            plan_id = conn.execute('SELECT active_plan_revision_id FROM projects').fetchone()[0]
        self.service.record_validation(project_id=self.prepared.project_id,
            plan_revision_id=plan_id, result=result)
        with self.assertRaisesRegex(EngineServiceError, 'PASS'):
            self.service.complete_task(self.ids[0])
        self.assertNotIn(self.ids[1], self.service.list_ready_tasks(self.prepared.project_id))


class IndependentValidationInputs(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        workspace, _ = _copy_fixture(Path.cwd(), base)
        self.inventory = qualification_inventory()
        self.prepared = _prepare(workspace=workspace, state_root=base/'state',
            inventory=self.inventory, roles=default_role_configuration(Path.cwd()),
            semantic_task_validation=True)
        self.service = self.prepared.service
        self.service.compile_execution_spec(self.prepared.proposal, inventory=self.inventory)
        self.worker = self.service.reserve_attempt(task_id=self.prepared.task_id)
        self.service.finish_attempt(attempt_id=self.worker.attempt_id, succeeded=True)

    def assert_review_rejected(self, attempt_id, expected):
        observation = SemanticValidationObservation(validation_id='validation_public_contract',
            task_id=self.prepared.task_id, reviewer_role='synthetic-validator',
            model='different-model-does-not-prove-independence', effort='high', passed=True,
            rationale='adversarial completion claim', evidence_refs=('unobserved:source',),
            observed_at=utc_now())
        review = EvidenceRecord(evidence_id=new_id('evidence'),
            project_id=self.prepared.project_id, task_id=self.prepared.task_id, attempt_id=attempt_id,
            kind=EvidenceKind.MODEL_REVIEW, source_ref='codex-validator:synthetic-unbound-thread',
            observation=observation.model_dump_json(), content_digest=sha256_digest(observation),
            observed_at=utc_now())
        self.service.record_evidence(review)
        result = ValidationResult(validation_result_id=new_id('validation_result'),
            validation_id=observation.validation_id, task_id=observation.task_id,
            status=ValidationStatus.PASS, evidence_ids=(review.evidence_id,),
            rationale='separate model claims pass', evaluated_at=utc_now())
        with self.assertRaisesRegex(EngineServiceError, expected):
            self.service.record_validation(project_id=self.prepared.project_id,
                plan_revision_id=self.prepared.plan_revision_id, result=result)
        with self.service.ledger.read() as connection:
            self.assertEqual(0, connection.execute('SELECT COUNT(*) FROM validation_results').fetchone()[0])

    def test_worker_review_with_different_model_cannot_be_independent_authority(self):
        self.assert_review_rejected(self.worker.attempt_id, 'INDEPENDENT_VALIDATION_ATTEMPT_REQUIRED')

    def test_separate_validator_attempt_without_terminal_job_is_rejected(self):
        validator = self.service.reserve_attempt(task_id=self.prepared.task_id, kind=AttemptKind.VALIDATION)
        self.assert_review_rejected(validator.attempt_id, 'INDEPENDENT_VALIDATION_PROVENANCE_MISSING')

    def test_production_d1_posix_guard_precedes_job_and_attempt_mutation(self):
        with self.service.ledger.read() as connection:
            before = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in ('runtime_jobs', 'attempts', 'history_events')}
        result = EngineDispatcher(self.service, FakeCodexRuntime(self.inventory)).run_once(self.prepared.project_id)
        self.assertEqual(RunOnceAction.BLOCKED, result.action)
        self.assertIn('platform unsupported: posix', result.detail)
        with self.service.ledger.read() as connection:
            after = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in before}
        self.assertEqual(before, after)


SELECTED = {
    'tests.test_engine_planning.EnginePlanningTests': [
        'test_project_prefix_does_not_authorize_a_made_up_input',
        'test_hard_gate_failure_is_not_scored_or_reviewed',
        'test_stale_expanded_plan_is_rejected_before_plan_reviewer',
        'test_reviewer_finding_cannot_be_admitted',
        'test_serialized_evaluation_cannot_override_core_decision',
        'test_search_outcome_recomputes_selected_plan_and_call_count'],
    'tests.test_engine_ledger_service.EngineLedgerServiceTests': [
        'test_service_recomputes_skeleton_gate_instead_of_trusting_payload',
        'test_service_rejects_plan_semantic_drift_hidden_by_clean_review',
        'test_service_rejects_copied_unknown_evidence_before_plan_registration',
        'test_plan_revision_must_supersede_latest_in_same_lineage',
        'test_execution_spec_cannot_change_task_semantics',
        'test_file_change_after_materialization_blocks_attempt',
        'test_same_project_attempts_are_serialized'],
    'tests.test_engine_core_capabilities.EngineCoreCapabilityTests': [
        'test_source_permission_strings_and_constructed_handles_cannot_authorize',
        'test_capability_is_bound_to_authority_project_and_current_policy',
        'test_nested_worker_validator_context_cannot_mint_or_use_core_handles',
        'test_structured_validator_callback_cannot_use_even_leaked_capability'],
    'tests.test_engine_validation_operation_binding.ValidationOperationBindingTests': [
        'test_tampered_operation_binding_is_rejected_before_recording'],
    'tests.test_engine_domain.EngineDomainTests': [
        'test_models_are_frozen_and_extra_fields_are_forbidden',
        'test_new_schema_does_not_accept_unconsumed_commit_horizon'],
}

if __name__ == '__main__':
    suite = unittest.TestSuite()
    for cls in (InvalidGraphInputs, DependencyReadiness, IndependentValidationInputs):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    for cls, methods in SELECTED.items():
        suite.addTests(unittest.defaultTestLoader.loadTestsFromNames([cls + '.' + m for m in methods]))
    if '--regressions' in sys.argv:
        from tests.test_engine_goal_verdict_authority import GoalVerdictAuthorityTests
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(GoalVerdictAuthorityTests))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(
            'tests.test_engine_ledger_service.EngineLedgerServiceTests.'
            'test_goal_satisfied_is_refused_before_task_and_goal_evidence'))
    def fake_command(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, b'synthetic command fixture\n', b'')
    with patch('flowmarshal.engine.validation_execution.subprocess.run', side_effect=fake_command):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
