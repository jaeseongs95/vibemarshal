"""독립 interrupt 검토: disposable 합성 원장, file barrier, 실제 adapter guard만 사용."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import flowmarshal.engine.runtime as runtime_module
from flowmarshal.engine.claude_runtime import ClaudeCodeRuntime
from flowmarshal.engine.domain import RuntimeJobKind, RuntimeJobObservationKind, utc_now
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import EngineDispatcher, FakeCodexRuntime, RuntimeJobSupervisor, RuntimeObservation, RuntimeOperationReceipt
from flowmarshal.engine.service import EngineService
from tests.test_engine_qualification import qualification_inventory

HERE = Path(__file__).resolve().parent
THREAD, TURN = 'review-thread', 'review-turn'

def wait(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(path.name)
        time.sleep(.005)

def append(path, value):
    with path.open('a') as stream:
        stream.write(json.dumps(value, sort_keys=True) + '\n')
        stream.flush()
        os.fsync(stream.fileno())

class JournalRuntime:
    def __init__(self, root, label='parent', behavior=None, read_turn=TURN, terminal=None):
        self.root, self.label, self.behavior = root, label, behavior
        self.read_turn, self.terminal = read_turn, terminal
    def interrupt(self, *, thread_id, turn_id, timeout_seconds):
        assert (thread_id, turn_id) == (THREAD, TURN)
        append(self.root / 'effects.jsonl', {'label':self.label, 'thread_id':thread_id, 'turn_id':turn_id})
        (self.root / (self.label + '.effect')).touch()
        if self.behavior == 'hold':
            wait(self.root / 'release')
        if self.behavior == 'crash_after_effect':
            os._exit(73)
        if self.behavior == 'lost_response':
            raise ConnectionError('SYNTHETIC_RESPONSE_LOST_AFTER_EFFECT')
        return RuntimeOperationReceipt(operation_id=turn_id, payload={'synthetic':True})
    def read_stored(self, *, thread_id, turn_id, timeout_seconds):
        append(self.root / 'reads.jsonl', {'thread_id':thread_id, 'turn_id':turn_id})
        return RuntimeObservation(thread_id=thread_id, turn_id=self.read_turn,
            active=self.terminal is None, terminal_status=self.terminal,
            final_response=None if self.terminal is None else 'direct synthetic terminal', payload={'synthetic':True})

def snapshot(service, project_id, job_id, root):
    with service.ledger.read() as c:
        observations=[{'kind':r['kind'], 'provider_terminal':bool(r['provider_terminal']),
            'terminal_status':r['terminal_status'], 'payload':json.loads(r['payload_json'])}
            for r in c.execute('SELECT * FROM runtime_job_observations WHERE job_id=? ORDER BY rowid',(job_id,))]
        markers=[json.loads(r[0]) for r in c.execute("SELECT payload_json FROM history_events WHERE entity_id=? AND event_type='runtime_job.interrupt_dispatching'",(job_id,))]
        counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['attempts','runtime_intents','runtime_receipts','provider_calls','goal_verdicts']}
        task_states=[r[0] for r in c.execute('SELECT status FROM task_contracts')]
    read=lambda name:[json.loads(line) for line in (root/name).read_text().splitlines()] if (root/name).exists() else []
    job=service.load_runtime_job(job_id)
    return {'job_status':job.status.value, 'thread_id':job.thread_id, 'turn_id':job.turn_id,
        'provider_terminal_status':job.provider_terminal_status, 'markers':markers,
        'observations':observations, 'table_counts':counts, 'task_states':task_states,
        'effects':read('effects.jsonl'), 'reads':read('reads.jsonl'), 'history_valid':service.ledger.verify_history(project_id)}

@contextlib.contextmanager
def fixture(source, label, case):
    root=HERE/'scratch'/label/case
    root.mkdir(parents=True, exist_ok=False)
    workspace,_=_copy_fixture(source,root)
    p=_prepare(workspace=workspace,state_root=root/'state',inventory=qualification_inventory(),roles=default_role_configuration(source))
    s=p.service
    job=s.schedule_runtime_job(project_id=p.project_id, kind=RuntimeJobKind.WORKER_TURN,
        checkpoint_key='independent-'+case, request={'synthetic':True},
        absolute_deadline_at=utc_now()+timedelta(minutes=2),task_id=p.task_id)
    s.start_runtime_job(job.job_id)
    s.bind_runtime_job_provider(job.job_id,thread_id=THREAD,turn_id=TURN)
    yield root,p,job.job_id

def child(config):
    root=Path(config['root'])
    s=EngineService(SQLiteEngineLedger(config['db'], artifact_root=config['artifacts']))
    label=config['label']
    (root/(label+'.ready')).touch()
    wait(root/(label+'.go'))
    if config['behavior']=='crash_before_claim':
        s.begin_runtime_job_interrupt(config['job'],payload={'reason':'workflow_paused','thread_id':THREAD,'turn_id':TURN})
        os._exit(71)
    if config['behavior']=='crash_at_rpc_boundary':
        original=runtime_module.bounded_observation_call
        def crash(operation, *, timeout_seconds, operation_name):
            if operation_name=='runtime_job_interrupt':
                os._exit(72)
            return original(operation,timeout_seconds=timeout_seconds,operation_name=operation_name)
        runtime_module.bounded_observation_call=crash
    rt=JournalRuntime(root,label,config['behavior'])
    RuntimeJobSupervisor(s,rt,interrupt_timeout_seconds=3).request_interrupt(config['job'],reason='workflow_paused')
    return 0

def spawn(source,root,p,job,label,behavior):
    cfg={'root':str(root),'db':str(p.service.ledger.path),'artifacts':str(p.service.ledger.artifact_root),
        'job':job,'label':label,'behavior':behavior}
    env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(source/'src')+':'+str(source)}
    process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',json.dumps(cfg)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    wait(root/(label+'.ready'))
    (root/(label+'.go')).touch()
    return process

def finish(process):
    stdout,stderr=process.communicate(timeout=10)
    return {'exit':process.returncode,'stdout':stdout,'stderr':stderr}

def probe(source,label):
    cases=[]
    with fixture(source,label,'concurrent_inflight') as (root,p,j):
        a=spawn(source,root,p,j,'a','hold')
        try:
            wait(root/'a.effect')
            b=spawn(source,root,p,j,'b',None)
            children=[finish(b)]
        finally:
            (root/'release').touch()
        children.append(finish(a))
        snap=snapshot(p.service,p.project_id,j,root)
        assert len(snap['effects'])==(1 if label=='candidate' else 2),snap
        cases.append({'case':'concurrent_inflight','children':children,'after':snap})
    for behavior,exit_code in [('crash_before_claim',71),('crash_at_rpc_boundary',72),('crash_after_effect',73)]:
        with fixture(source,label,behavior) as (root,p,j):
            proc=spawn(source,root,p,j,'crasher',behavior)
            receipt=finish(proc); assert receipt['exit']==exit_code,receipt
            before=snapshot(p.service,p.project_id,j,root)
            rt=JournalRuntime(root,'recover')
            supervisor=RuntimeJobSupervisor(p.service,rt)
            if behavior=='crash_after_effect':
                supervisor.reattach(j)
            else:
                supervisor.request_interrupt(j,reason='workflow_paused')
                supervisor.reattach(j)
            supervisor.reattach(j)
            supervisor.request_interrupt(j,reason='workflow_cancelled')
            after=snapshot(p.service,p.project_id,j,root)
            expected=1 if behavior=='crash_before_claim' else (0 if label=='candidate' else 1) if behavior=='crash_at_rpc_boundary' else (1 if label=='candidate' else 2)
            assert len(after['effects'])==expected,after
            assert after['provider_terminal_status'] is None
            assert all((r['thread_id'],r['turn_id'])==(THREAD,TURN) for r in after['reads'])
            cases.append({'case':behavior,'child':receipt,'before':before,'after':after})
    with fixture(source,label,'response_loss_then_active_then_terminal') as (root,p,j):
        rt=JournalRuntime(root,behavior='lost_response')
        supervisor=RuntimeJobSupervisor(p.service,rt)
        supervisor.request_interrupt(j,reason='workflow_paused')
        supervisor.reattach(j)
        supervisor.request_interrupt(j)
        active=snapshot(p.service,p.project_id,j,root)
        assert len(active['effects'])==1 and active['provider_terminal_status'] is None
        rt.terminal='completed'
        supervisor.reattach(j)
        terminal=snapshot(p.service,p.project_id,j,root)
        assert terminal['provider_terminal_status']=='completed' and terminal['table_counts']['goal_verdicts']==0
        cases.append({'case':'response_loss_then_active_then_terminal','active':active,'terminal':terminal})
    with fixture(source,label,'nonowner_claude_no_effect_strands_owner') as (root,p,j):
        disconnected=object.__new__(ClaudeCodeRuntime)
        disconnected._lock=threading.RLock();disconnected._threads={};disconnected._interrupted_turn_ids=set()
        caller=RuntimeJobSupervisor(p.service,disconnected)
        owns_before=caller.owns_runtime_job(j)
        caller.request_interrupt(j,reason='workflow_paused')
        rejected=snapshot(p.service,p.project_id,j,root)
        rightful=RuntimeJobSupervisor(p.service,JournalRuntime(root,'rightful-owner'))
        rightful.request_interrupt(j,reason='workflow_paused')
        rightful.reattach(j)
        after=snapshot(p.service,p.project_id,j,root)
        assert len(after['effects'])==0 and after['provider_terminal_status'] is None
        errors=[o['payload'].get('detail') for o in rejected['observations'] if o['kind']=='interrupt_receipt']
        assert any('CLAUDE_INTERRUPT_TARGET_NOT_LIVE' in str(e) for e in errors)
        cases.append({'case':'nonowner_claude_no_effect_strands_owner','owns_before':owns_before,'rejected':rejected,'after':after})
    with fixture(source,label,'replacement_turn_rejected') as (root,p,j):
        p.service.begin_runtime_job_interrupt(j,payload={'reason':'workflow_paused','thread_id':THREAD,'turn_id':TURN})
        wrong_claim=None
        if label=='candidate':
            wrong_claim=p.service._claim_runtime_job_interrupt_delivery(j,thread_id=THREAD,turn_id='replacement-turn')
            assert wrong_claim is None
        binding_error=None
        try:
            p.service.bind_runtime_job_provider(j,thread_id=THREAD,turn_id='replacement-turn')
        except Exception as e:
            binding_error=type(e).__name__+': '+str(e)
        assert binding_error is not None
        adapter=object.__new__(ClaudeCodeRuntime)
        adapter._lock=threading.RLock();adapter._interrupted_turn_ids=set()
        adapter._threads={THREAD:SimpleNamespace(current=SimpleNamespace(turn_id='replacement-turn'))}
        writes=[]
        adapter._write=lambda *args:writes.append(args)
        supervisor=RuntimeJobSupervisor(p.service,adapter)
        supervisor.request_interrupt(j,reason='workflow_paused')
        replacement=RuntimeJobSupervisor(p.service,JournalRuntime(root,read_turn='replacement-turn'))
        read_error=None
        try:
            replacement.reattach(j)
        except Exception as e:
            read_error=type(e).__name__+': '+str(e)
        assert read_error and 'RUNTIME_OBSERVATION_BINDING_MISMATCH' in read_error
        snap=snapshot(p.service,p.project_id,j,root)
        assert not writes and not snap['effects'] and snap['provider_terminal_status'] is None
        cases.append({'case':'replacement_turn_rejected','wrong_claim':wrong_claim,'binding_error':binding_error,
            'adapter_control_writes':len(writes),'read_error':read_error,'after':snap})
    with fixture(source,label,'cancel_between_claim_and_rpc') as (root,p,j):
        original=runtime_module.bounded_observation_call
        def cancel_then_call(operation, *, timeout_seconds, operation_name):
            if operation_name=='runtime_job_interrupt':
                p.service.cancel_runtime_job(j,reason='synthetic revocation after dispatch boundary')
            return original(operation,timeout_seconds=timeout_seconds,operation_name=operation_name)
        runtime_module.bounded_observation_call=cancel_then_call
        try:
            RuntimeJobSupervisor(p.service,JournalRuntime(root)).request_interrupt(j,reason='workflow_paused')
        finally:
            runtime_module.bounded_observation_call=original
        snap=snapshot(p.service,p.project_id,j,root)
        assert snap['job_status']=='cancelled' and len(snap['effects'])==1 and snap['provider_terminal_status'] is None
        cases.append({'case':'cancel_between_claim_and_rpc','after':snap})
    with fixture(source,label,'d1_platform_boundary') as (root,p,j):
        rt=FakeCodexRuntime(qualification_inventory())
        outcome=EngineDispatcher(p.service,rt).run_once(p.project_id,proposal=p.proposal)
        assert outcome.action.value=='blocked' and 'platform unsupported: posix' in outcome.detail
        assert rt.create_calls==rt.turn_calls==0
        cases.append({'case':'d1_platform_boundary','action':outcome.action.value,'detail':outcome.detail,
            'create_calls':rt.create_calls,'turn_calls':rt.turn_calls,'after':snapshot(p.service,p.project_id,j,root)})
    for case in cases:
        print(json.dumps({'source':label,'case':case['case'],'observed_effects':len(case.get('after',case.get('terminal',{})).get('effects',[])),'oracle':'observation matched; not product acceptance'},sort_keys=True))
    output={'task_id':'vm-interrupt-independent-review-20261002','source':label,'source_head':'32bb0f9dd9f024045d24487312b50f5b703573a3',
        'requested_model':'gpt-6.1-sol','requested_effort':'high','actual_observed_model':None,'actual_observed_effort':None,
        'live_provider_calls':0,'cases':cases,'history_valid_all':all(c.get('after',c.get('terminal',{})).get('history_valid',True) for c in cases)}
    (HERE/(label+'-results.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':
    if sys.argv[1]=='--child':
        sys.exit(child(json.loads(sys.argv[2])))
    probe(Path(sys.argv[1]),sys.argv[2])
