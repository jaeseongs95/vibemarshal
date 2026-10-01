from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from flowmarshal.engine.application import ApplicationAuthority, EngineApplication, EngineApplicationError
from flowmarshal.engine.capabilities import CoreCapabilityError
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.runtime import FakeCodexRuntime, owner_lock_platform_supported
from flowmarshal.engine.service import EngineService
from tests.engine_helpers import inventory, profile
from tests.engine_inspection_helpers import InspectionScriptedRunner
from tests.fixtures.engine.governance.allow import AllowAllGate
from tests.test_engine_fm08_core_integration import _responses, _roles

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'evidence'
RUN = ROOT / 'disposable-workflow'
RUN.mkdir()
PROJECT = RUN / 'project'
PROJECT.mkdir()
(PROJECT / 'AGENTS.md').write_text('Disposable scripted adapter probe. No real providers.\n')
(PROJECT / 'app.py').write_text('value = 1\n')
subprocess.run(['git', 'init', '--quiet', str(PROJECT)], check=True)


class CountingRunner(InspectionScriptedRunner):
    def __init__(self):
        super().__init__(copy.deepcopy(_responses()))
        self.receipts = []

    def run(self, request, *, validator=None):
        result = super().run(request, validator=validator)
        self.receipts.append({
            'role': request.role, 'call_id': result.receipt.call_id,
            'status': result.receipt.status,
            'requested_model': request.model, 'requested_effort': request.effort,
            'observed_model': result.receipt.observed_model,
            'observed_effort': result.receipt.observed_effort,
            'provenance': 'existing scripted adapter; no live provider',
        })
        return result


class CountingGate(AllowAllGate):
    def __init__(self):
        self.before_execution_calls = 0
        self.before_completion_calls = 0

    def before_execution(self, task):
        self.before_execution_calls += 1
        return super().before_execution(task)

    def before_completion(self, task):
        self.before_completion_calls += 1
        return super().before_completion(task)


class CountingGovernance:
    def __init__(self):
        self.gate = CountingGate()
        self.open_calls = 0

    def open_gate(self, service, *, runtime, roles, runner):
        self.open_calls += 1
        return self.gate


ledger = SQLiteEngineLedger(RUN / 'state' / 'engine.sqlite3', artifact_root=RUN / 'artifacts')
service = EngineService(ledger)
service.initialize()
project_id = service.create_project(name='bounded-public-core-vertical', root=PROJECT)
service.register_profile(profile(project_id))
runtime = FakeCodexRuntime(inventory())
runner = CountingRunner()
governance = CountingGovernance()
application = EngineApplication(service, runtime=runtime, role_configuration=_roles(),
                                structured_runner=runner, governance=governance)


def snapshot():
    tables = ['goal_revisions', 'plan_revisions', 'goal_authorizations', 'plan_activations',
              'execution_spec_revisions', 'task_contracts', 'attempts', 'runtime_intents',
              'runtime_receipts', 'runtime_jobs', 'provider_calls', 'validation_results',
              'goal_verdicts', 'history_events']
    with ledger.read() as connection:
        counts = {table: connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
                  for table in tables}
        kinds = dict(connection.execute('SELECT event_type,COUNT(*) FROM history_events GROUP BY event_type'))
        history = [tuple(row) for row in connection.execute('SELECT * FROM history_events ORDER BY sequence')]
        plans = [dict(row) for row in connection.execute(
            'SELECT id,revision_no,definition_digest,activation_digest,payload_json,status FROM plan_revisions ORDER BY rowid')]
        tasks = [dict(row) for row in connection.execute('SELECT id,status FROM task_contracts ORDER BY rowid')]
        calls = [dict(row) for row in connection.execute(
            'SELECT role,status,execution_status,result_status,effect_status FROM provider_calls ORDER BY rowid')]
    history_hash = hashlib.sha256(json.dumps(history, ensure_ascii=False, default=str).encode()).hexdigest()
    return {'tables': counts, 'history_event_counts': kinds, 'history_rows_sha256': history_hash,
            'plans': plans, 'tasks': tasks, 'provider_calls': calls,
            'adapter_effect_calls': {'structured_role': len(runner.calls), 'scripted_receipts': len(runner.receipts),
                'runtime_create': runtime.create_calls, 'runtime_turn': runtime.turn_calls,
                'runtime_resume': runtime.resume_calls, 'runtime_interrupt': runtime.interrupt_calls,
                'governance_open': governance.open_calls,
                'gate_before_execution': governance.gate.before_execution_calls,
                'gate_before_completion': governance.gate.before_completion_calls},
            'live_provider_calls': 0}


def digest_preserved(before, after):
    fields = ('id', 'revision_no', 'definition_digest', 'activation_digest', 'payload_json')
    return [{k: p[k] for k in fields} for p in before['plans']] == [
        {k: p[k] for k in fields} for p in after['plans']]


events = []


def record(name, before, result):
    after = snapshot()
    events.append({'name': name, 'before': before, 'after': after, 'result': result,
                   'table_deltas': {k: after['tables'][k]-before['tables'][k] for k in before['tables']},
                   'effect_deltas': {k: after['adapter_effect_calls'][k]-before['adapter_effect_calls'][k]
                                     for k in before['adapter_effect_calls']}})
    return after


start = time.monotonic()
try:
    before = snapshot()
    prepared = application.prepare(project_id, source_request='app.py의 value를 2로 바꾸고 독립적으로 검증해 주세요.')
    after_prepare = record('public_application_prepare', before,
                           {'status': prepared.status, 'role_sequence': [r.role for r in runner.calls]})
    assert prepared.status == 'ready_for_authorization'
    assert len(runner.calls) == 6
    assert after_prepare['tables']['goal_authorizations'] == 0
    assert after_prepare['tables']['plan_activations'] == 0
    assert runtime.create_calls == runtime.turn_calls == 0

    before = snapshot()
    try:
        application.authorize(project_id, source='fixture-business-user')
        raise AssertionError('source-only authorization unexpectedly succeeded')
    except CoreCapabilityError as exc:
        denial = {'error': type(exc).__name__, 'message': str(exc)}
    after = record('public_authorize_without_host_capability_denied', before, denial)
    assert after == before

    authority = ApplicationAuthority(application)
    target = authority.authorization_target(project_id)
    before = snapshot()
    authorized = authority.authorize(project_id, target=target, source='disposable-test-host-confirmation')
    after_authorize = record('public_application_authority_authorize_activate', before,
        {'authorization_id': authorized.authorization_id, 'activation_id': authorized.activation_id,
         'target_digest': target.target_digest, 'scope': 'public trusted-host API; synthetic business confirmation'})
    assert after_authorize['tables']['goal_authorizations'] == 1
    assert after_authorize['tables']['plan_activations'] == 1
    assert after_authorize['plans'][0]['status'] == 'active'
    assert after_authorize['tasks'][0]['status'] == 'ready'
    assert digest_preserved(after_prepare, after_authorize)

    before = snapshot()
    try:
        application.revise(project_id, source_request='same narrow disposable request')
        raise AssertionError('active-plan revision unexpectedly accepted')
    except EngineApplicationError as exc:
        denied_revision = {'error': type(exc).__name__, 'message': str(exc)}
    after = record('public_active_plan_revision_supported_boundary', before, denied_revision)
    assert after == before
    assert digest_preserved(before, after)

    assert os.name == 'posix' and owner_lock_platform_supported() is False
    for tick in range(2):
        before = snapshot()
        outcome = application.run_once(project_id)
        after = record(f'public_run_once_posix_failclosed_{tick+1}', before, outcome.model_dump(mode='json'))
        assert outcome.blocker_code == 'RUNTIME_OWNER_LOCK_UNAVAILABLE'
        assert 'platform unsupported: posix' in outcome.detail
        assert after['tables'] == before['tables']
        assert after['history_rows_sha256'] == before['history_rows_sha256']
        for key in ('structured_role','scripted_receipts','runtime_create','runtime_turn','runtime_resume',
                    'runtime_interrupt','gate_before_execution','gate_before_completion'):
            assert after['adapter_effect_calls'][key] == before['adapter_effect_calls'][key]

    final = snapshot()
    assert final['tables']['execution_spec_revisions'] == 0
    assert final['tables']['attempts'] == final['tables']['runtime_intents'] == final['tables']['runtime_receipts'] == 0
    assert final['tables']['validation_results'] == final['tables']['goal_verdicts'] == 0
    assert ledger.verify_history(project_id)
    result = {
        'verification': 'PASS bounded supported public preparation/authorization/D1 checks; full vertical NOT_RUN',
        'scope': 'EngineApplication + public ApplicationAuthority; existing scripted role and FakeCodexRuntime adapters',
        'not_full_public_e2e': 'No installed CLI/user terminal, real AGS, native execution or live provider qualification',
        'runtime_platform_supported': False, 'platform': platform.platform(),
        'requested_orchestration_model': 'gpt-6.1-sol', 'requested_orchestration_effort': 'high',
        'observed_orchestration_model': None, 'observed_orchestration_effort': None,
        'fallback': None, 'live_provider_calls': 0, 'scripted_role_receipts': runner.receipts,
        'events': events, 'final': final, 'ledger_history_valid': True,
        'not_run': ['ready ExecutionSpec materialization', 'Task admission with AGS AND gate',
                    'Task execution', 'independent Task validation', 'independent GoalTest', 'Core GoalVerdict'],
        'skip_count': 0, 'not_run_leg_count': 6,
        'remaining_input': 'A Windows executor supporting D1 with the same exact candidate and existing scripted adapters. Real AGS AND acceptance additionally needs its declared plugin closure and authorized explicit profile supplied by its owner.',
        'seconds': round(time.monotonic()-start, 6),
    }
    (EVIDENCE/'vertical-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('verification','live_provider_calls','not_run','seconds')}, ensure_ascii=False))
finally:
    application.close_task_gate()
    if application.supervisor is not None:
        application.supervisor.close(timeout_seconds=0.1)
