from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import test_flowmarshal_implementation_workflow as fixture

impl, ns, connect, digest = fixture.impl, fixture.ns, fixture.connect, fixture.digest


class DirectSelectionTest(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.ImplementationWorkflowTest()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.fx.manifest['tasks'].extend([
            self.fx._task('FM-02', 2, 'development', ['FM-00']),
            self.fx._task('FM-03', 3, 'development', ['FM-01']),
        ])
        self.fx._write_manifest(self.fx.manifest)
        self.fx.migrate()
        self.run = self.fx.begin()['run_id']
        self.fx.pass_fm00(self.run)
        self.assignment = self.fx.root / 'assignment.json'
        self.assignment.write_text('coordinator writer/source/scope assignment', encoding='utf-8')
        self.policy = self.fx.root / 'policy.md'
        self.policy.write_text('explicit user authorized direct selection policy', encoding='utf-8')
        self.impact = self.fx.root / 'impact.md'
        self.impact.write_text('coordinator reviewed shared inputs and effects; no impact on FM-02', encoding='utf-8')

    def ready(self):
        return impl.command_ready_tasks(ns(db=str(self.fx.db)))

    def selection(self, task_id='FM-02'):
        state = self.ready()
        task = next(t for t in state['ready_tasks'] if t['task_id'] == task_id)
        return {
            'schema_version': 1, 'mode': 'dependency-ready',
            'workflow_id': impl.WORKFLOW_ID,
            'workflow_revision': state['workflow_revision'],
            'generation': state['generation'],
            'task_id': task_id, 'task_revision': task['task_revision'],
            'task_spec_sha256': task['task_spec_sha256'],
            'task_attempt_count': task['task_attempt_count'],
            'coordinator_id': 'test-coordinator',
            'assignment': self.ref(self.assignment), 'policy': self.ref(self.policy),
            'dependencies': task['dependencies'],
            'excluded_failures': [dict(f, shared_inputs_verdict='independent', impact_review=self.ref(self.impact))
                                  for f in state['failed_tasks']],
        }

    @staticmethod
    def ref(path):
        return {'path': str(path), 'sha256': digest(path)}

    def prepare(self, selection, task_id='FM-02'):
        path = self.fx.root / 'selection.json'
        path.write_text(json.dumps(selection, ensure_ascii=False), encoding='utf-8')
        return impl.command_prepare_direct_selection(ns(db=str(self.fx.db), run_id=self.run,
                                                       task_id=task_id, selection_file=str(path)))

    def record(self, selection=None, task_id='FM-02', evidence=None):
        evidence = evidence or self.fx.evidence(task_id, name=f'{task_id}-selected-evidence.json')
        kwargs = {}
        if selection is not None:
            path = self.fx.root / 'selection.json'
            path.write_text(json.dumps(selection, ensure_ascii=False), encoding='utf-8')
            data = json.loads(evidence.read_text('utf-8'))
            refs = [self.ref(path), selection['assignment'], selection['policy']]
            refs += [x['impact_review'] for x in selection['excluded_failures']]
            for ref in refs:
                if ref['path'] not in {f['path'] for f in data['files']}:
                    data['files'].append(ref)
            evidence.write_text(json.dumps(data), encoding='utf-8')
            kwargs['selection_file'] = str(path)
        return self.fx._record_direct(self.run, task_id, evidence, **kwargs)

    def counts(self):
        with connect(self.fx.db) as c:
            return tuple(c.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0] for t in [
                'implementation_task_attempts', 'implementation_evidence',
                'implementation_check_results', 'orchestration_events'])

    def fail_first(self, failure_class='implementation'):
        evidence = self.fx.evidence('FM-01', name='failed-first.json', finding={
            'failure_class': failure_class, 'fingerprint': 'b' * 64,
            'summary': 'isolated defect', 'remediable': True, 'scope_expansion_required': False,
        })
        return self.record(task_id='FM-01', evidence=evidence)

    def test_default_preserved_opt_in_records_second_and_event(self):
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'READY'):
            self.record()
        self.assertEqual(before, self.counts())
        selected = self.selection()
        prepared = self.prepare(selected)
        self.assertTrue(self.prepare(selected)['idempotent'])
        result = self.record(selected)
        self.assertTrue(result['recorded'])
        with connect(self.fx.db) as c:
            self.assertEqual(c.execute("SELECT status FROM implementation_tasks WHERE task_id='FM-01'").fetchone()[0], 'PENDING')
            payload = json.loads(c.execute("SELECT payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_attempt_recorded' ORDER BY event_id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(payload['direct_selection']['decision'], selected)
        self.assertEqual(payload['direct_selection']['prepared_event_id'], prepared['prepared_event_id'])
        self.assertEqual(payload['direct_selection']['sha256'], digest(self.fx.root / 'selection.json'))
        repeated = self.record(selected, evidence=self.fx.root / 'FM-02-selected-evidence.json')
        self.assertTrue(repeated['idempotent'])

    def test_failed_independent_selection_cannot_complete_workflow(self):
        self.fail_first()
        with self.assertRaisesRegex(RuntimeError, 'READY'):
            self.record()
        selected = self.selection()
        self.prepare(selected)
        self.assertTrue(self.record(selected)['recorded'])
        self.assertEqual(impl.command_status(ns(db=str(self.fx.db)))['decision'], 'RECOVERY_REQUIRED')
        with self.assertRaisesRegex(RuntimeError, 'COMPLETE'):
            impl.command_finish(ns(db=str(self.fx.db), run_id=self.run, outcome='COMPLETE', reason='must not pass'))

    def test_failed_dependency_and_unknown_effect_are_not_bypassed(self):
        self.fail_first('external_unknown')
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'external_unknown'):
            self.record(self.selection())
        self.assertEqual(before, self.counts())
        selected = self.selection()
        with connect(self.fx.db) as c:
            spec = c.execute("SELECT spec_sha256 FROM implementation_tasks WHERE task_id='FM-03'").fetchone()[0]
        selected.update(task_id='FM-03', task_spec_sha256=spec)
        with self.assertRaisesRegex(RuntimeError, 'dependency-ready'):
            self.record(selected, task_id='FM-03')
        self.assertEqual(before, self.counts())

    def test_unprepared_and_superseded_preparations_rejected(self):
        selected = self.selection()
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'prepare-direct-selection'):
            self.record(selected)
        self.assertEqual(before, self.counts())
        first = self.prepare(selected)
        newer = copy.deepcopy(selected)
        newer['coordinator_id'] = 'replacement-coordinator'
        second = self.prepare(newer)
        self.assertGreater(second['prepared_event_id'], first['prepared_event_id'])
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'prepare-direct-selection'):
            self.record(selected)
        self.assertEqual(before, self.counts())
        self.assertTrue(self.record(newer)['recorded'])

    def test_changed_identity_or_schema_rejects_without_rows(self):
        selected = self.selection()
        changes = [('workflow_revision', 77), ('generation', 77), ('task_spec_sha256', 'f' * 64),
                   ('dependencies', []), ('approved', True)]
        before = self.counts()
        for key, value in changes:
            with self.subTest(key=key):
                altered = copy.deepcopy(selected)
                altered[key] = value
                with self.assertRaises((RuntimeError, ValueError)):
                    self.record(altered)
                self.assertEqual(before, self.counts())

    def test_new_failure_after_assignment_requires_new_review(self):
        selected = self.selection()
        self.fail_first()
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'FAILED'):
            self.record(selected)
        self.assertEqual(before, self.counts())
        selected = self.selection()
        selected['excluded_failures'][0]['failure_fingerprint'] = 'c' * 64
        with self.assertRaisesRegex(RuntimeError, 'FAILED'):
            self.record(selected)
        self.assertEqual(before, self.counts())

    def test_stale_success_and_evidence_tamper_reject(self):
        selected = self.selection()
        self.fx.proof.write_text('changed prior success evidence', encoding='utf-8')
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'stale|dependency-ready'):
            self.record(selected)
        self.assertEqual(before, self.counts())

    def test_active_dispatch_and_expired_lease_preserved(self):
        selected = self.selection()
        self.fx.reserve_fm01(self.run)
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, '활성 dispatch'):
            self.record(selected)
        self.assertEqual(before, self.counts())
        with connect(self.fx.db) as c:
            c.execute("UPDATE orchestration_locks SET expires_at='2000-01-01T00:00:00Z'")
        with self.assertRaisesRegex(RuntimeError, 'lease'):
            self.record(selected)
        self.assertEqual(before, self.counts())

    def test_selection_refs_and_required_check_cannot_be_forged(self):
        selected = self.selection()
        selected['assignment']['sha256'] = 'e' * 64
        before = self.counts()
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.record(selected)
        self.assertEqual(before, self.counts())
        evidence = self.fx.evidence('FM-02', name='not-run.json')
        data = json.loads(evidence.read_text('utf-8'))
        data['checks'][0]['status'] = 'NOT_RUN'
        evidence.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'PASS'):
            self.record(self.selection(), evidence=evidence)
        self.assertEqual(before, self.counts())

    def test_ready_tasks_is_read_only_and_cli_option_parses(self):
        before = self.counts()
        state = self.ready()
        self.assertFalse(state['execution_authorized'])
        self.assertEqual([t['task_id'] for t in state['ready_tasks']], ['FM-01', 'FM-02'])
        self.assertEqual(before, self.counts())
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(fixture.MODULE_PATH), '--db', str(self.fx.db), 'ready-tasks'], capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['execution_authorized'])
        help_result = subprocess.run([sys.executable, str(fixture.MODULE_PATH), '--db', str(self.fx.db), 'record-direct-attempt', '--help'], capture_output=True, text=True, encoding='utf-8', env=env)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('--selection-file', help_result.stdout)

    def test_recovery_priority_and_stale_bootstrap_diagnostic(self):
        self.fail_first()
        selected = self.selection()
        recovery = impl.command_register_recovery(ns(db=str(self.fx.db), run_id=self.run,
                                                     task_id='FM-01', evidence_file=str(self.fx.root / 'failed-first.json')))
        self.assertTrue(recovery['registered'])
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, 'READY recovery'):
            self.prepare(selected)
        self.assertEqual(before, self.counts())
        ready = self.ready()
        self.assertEqual(ready['global_decision']['lane'], 'recovery')
        self.fx.proof.write_text('bootstrap evidence changed', encoding='utf-8')
        state = self.ready()
        self.assertEqual(state['global_decision']['decision'], 'RECOVERY_REQUIRED')
        self.assertIn('FM-00', state['stale_succeeded_tasks'])
        self.assertEqual(len(state['blocked_recoveries']), 1)
        self.assertEqual(state['blocked_recoveries'][0]['dependency_blockers'], [
            {'task_id': 'FM-00', 'status': 'SUCCEEDED', 'required_check_state': 'stale'}])
        self.assertEqual(before, self.counts())

    def test_preparation_survives_lease_release_and_records_in_new_run_via_cli(self):
        selected = self.selection()
        prepared = self.prepare(selected)
        original_run = self.run
        impl.command_finish(ns(db=str(self.fx.db), run_id=self.run, outcome='WAIT',
                               reason='release writer lease while external worker executes'))
        self.run = self.fx.begin()['run_id']
        self.assertNotEqual(original_run, self.run)
        evidence = self.fx.evidence('FM-02', name='cross-run-evidence.json')
        data = json.loads(evidence.read_text('utf-8'))
        selection_path = self.fx.root / 'selection.json'
        data['files'].extend([self.ref(selection_path), selected['assignment'], selected['policy']])
        evidence.write_text(json.dumps(data), encoding='utf-8')
        result = subprocess.run([
            sys.executable, str(fixture.MODULE_PATH), '--db', str(self.fx.db),
            'record-direct-attempt', '--run-id', self.run, '--task-id', 'FM-02',
            '--evidence-file', str(evidence), '--executor-model', 'claude-test-opus',
            '--selection-file', str(selection_path),
        ], capture_output=True, text=True, encoding='utf-8',
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8'))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)['recorded'])
        with connect(self.fx.db) as c:
            row = c.execute("SELECT run_id,payload_json FROM orchestration_events WHERE event_type='implementation_task.direct_attempt_recorded' ORDER BY event_id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], self.run)
        self.assertEqual(json.loads(row[1])['direct_selection']['prepared_event_id'], prepared['prepared_event_id'])


if __name__ == '__main__':
    unittest.main()
