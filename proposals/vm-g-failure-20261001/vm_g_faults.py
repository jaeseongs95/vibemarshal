"""VM G: 합성 원장·별도 프로세스·파일 barrier만 사용하는 신규 fault 진단."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta

from flowmarshal.engine.budget import BudgetManager
from flowmarshal.engine.domain import RuntimeIntentKind, RuntimeJobKind, RuntimeJobObservationKind, ThreadBinding, utc_now
from flowmarshal.engine.e2e_qualification import _copy_fixture, _prepare
from flowmarshal.engine.ledger import SQLiteEngineLedger
from flowmarshal.engine.qualification import default_role_configuration
from flowmarshal.engine.runtime import RuntimeJobSupervisor, RuntimeOperationReceipt, RuntimeObservation
from flowmarshal.engine.service import EngineService
from tests.test_engine_qualification import qualification_inventory

ROOT = Path(os.environ['VM_G_REPO'])
EVIDENCE = []

def wait(path):
    deadline = time.monotonic() + 15
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(path.name)
        time.sleep(.005)

def child(c):
    work = Path(c['work'])
    service = EngineService(SQLiteEngineLedger(c['db'], artifact_root=c['artifacts']))
    label = c['label']
    (work / (label + '.ready')).touch()
    wait(work / (label + '.go'))
    class Runtime:
        def interrupt(self, **kwargs):
            with (work / 'effects.jsonl').open('a') as f:
                f.write(json.dumps({'label': label, 'operation': 'interrupt', 'thread': kwargs['thread_id'], 'turn': kwargs['turn_id']}) + '\n')
            (work / (label + '.inside')).touch()
            if c.get('hold'):
                wait(work / (label + '.release'))
            if c.get('response_loss'):
                raise ConnectionError('synthetic response loss after interrupt effect')
            return RuntimeOperationReceipt(operation_id='synthetic-interrupt', payload={'synthetic': True})
        def read_stored(self, **kwargs):
            with (work / 'reads.jsonl').open('a') as f:
                f.write(json.dumps(kwargs) + '\n')
            return RuntimeObservation(thread_id='synthetic-thread', turn_id='synthetic-turn', active=False,
                terminal_status='completed', final_response='synthetic direct re-observation', payload={'synthetic': True})
    try:
        op = c['op']
        if op == 'reserve':
            value = service.reserve_attempt(task_id=c['task']).attempt_id
        elif op == 'budget':
            with service.ledger.read() as conn:
                goal = conn.execute('SELECT g.goal_id,g.definition_digest FROM goal_revisions g JOIN projects p ON p.active_goal_revision_id=g.id').fetchone()
            value = BudgetManager(service).reserve(project_id=c['project'], goal_id=goal['goal_id'],
                goal_digest=goal['definition_digest'], call_key=c.get('key', label), role='worker', request={'synthetic': True})
        elif op == 'bind':
            value = service.bind_runtime_job_provider(c['job'], thread_id='synthetic-thread', turn_id=c.get('turn','synthetic-turn')).model_dump(mode='json')
        elif op == 'interrupt':
            value = RuntimeJobSupervisor(service, Runtime(), interrupt_timeout_seconds=10).request_interrupt(c['job'], reason='workflow_paused').model_dump(mode='json')
        elif op == 'reattach':
            value = RuntimeJobSupervisor(service, Runtime(), interrupt_timeout_seconds=10).reattach(c['job']).model_dump(mode='json')
        elif op == 'begin_interrupt':
            value = service.begin_runtime_job_interrupt(c['job'], payload={'reason':'workflow_paused'})
        elif op == 'bind_supervisor':
            value = RuntimeJobSupervisor(service, Runtime(), interrupt_timeout_seconds=10).bind_provider_turn(c['job'], thread_id='synthetic-thread', turn_id='synthetic-turn').model_dump(mode='json')
        elif op == 'claim_interrupt':
            value = service._claim_runtime_job_interrupt_delivery(c['job'], thread_id='synthetic-thread', turn_id='synthetic-turn')
            if value is None:
                raise AssertionError('synthetic delivery claim missing')
            os._exit(24)
        elif op == 'effect':
            service.prepare_authorized_runtime_effect(c['attempt'], c['intent'], request={'synthetic': True}, runtime_job_id=c.get('job'))
            value = 'effect-authorized'
        elif op == 'intent':
            value = service.prepare_runtime_intent(attempt_id=c['attempt'], kind=RuntimeIntentKind(c.get('kind','create_thread')),
                idempotency_key='synthetic-intent', request={'synthetic': True}).intent_id
        elif op == 'receipt':
            trace = json.loads((work/'provider-trace.json').read_text()) if c.get('reconcile') else None
            value = service.record_runtime_receipt(intent_id=c['intent'], provider_operation_id='synthetic-thread',
                response=trace['response'] if trace else {'thread_id':'synthetic-thread'}, binding=ThreadBinding.model_validate(trace['binding']) if trace else ThreadBinding(thread_id=c.get('thread','synthetic-thread'), bound_at=c['bound_at']),
                allow_reconcile_unknown=c.get('reconcile',False)).receipt_id
        elif op == 'provider_create_response_loss':
            trace={'intent':c['intent'],'request':{'synthetic':True},'response':{'thread_id':'synthetic-thread'},
                'binding':ThreadBinding(thread_id='synthetic-thread',bound_at=utc_now()).model_dump(mode='json')}
            with (work/'effects.jsonl').open('a') as f:
                f.write(json.dumps({'label':label,'operation':'create_thread','thread':'synthetic-thread'})+'\n')
            (work/'provider-trace.json').write_text(json.dumps(trace))
            os._exit(23)
        else:
            raise ValueError(op)
        out = {'ok':True, 'value':value}
    except Exception as e:
        out = {'ok':False,'error_type':type(e).__name__,'error':str(e)}
    result = work / (label + '.result')
    result.with_suffix('.tmp').write_text(json.dumps(out))
    os.replace(result.with_suffix('.tmp'), result)
    return 0

class FaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vm-g-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        workspace,_ = _copy_fixture(ROOT,self.base)
        self.p = _prepare(workspace=workspace,state_root=self.base/'state',inventory=qualification_inventory(),roles=default_role_configuration(ROOT))
        self.s = self.p.service
        self.s.compile_execution_spec(self.p.proposal,inventory=qualification_inventory())
        self.children=[]
        self.started=time.monotonic()
        self.barriers=[]
        self.addCleanup(self.stop)
    def stop(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)
    def tearDown(self):
        child_exits=[]
        for child in self.children:
            stdout,stderr=child.communicate(timeout=5)
            child_exits.append({'label':child.vm_label,'exit':child.returncode,'stdout':stdout,'stderr':stderr})
        with self.s.ledger.read() as conn:
            counts={t:conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('attempts','runtime_intents','runtime_receipts','provider_calls')}
            counts['job_observations']=[dict(r) for r in conn.execute('SELECT kind,provider_terminal,terminal_status FROM runtime_job_observations')]
            counts['delivery_markers']=conn.execute("SELECT COUNT(*) FROM history_events WHERE event_type='runtime_job.interrupt_dispatching'").fetchone()[0]
        counts['effects']=self.effects()
        counts['case']=self.id().split('.')[-1]
        counts['history_valid']=self.s.ledger.verify_history(self.p.project_id)
        counts['children']=child_exits
        counts['barriers']=self.barriers
        counts['elapsed_seconds']=time.monotonic()-self.started
        EVIDENCE.append(counts)
    def effects(self):
        p=self.base/'effects.jsonl'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    def spawn(self, label, op, **kwargs):
        c={'label':label,'op':op,'work':str(self.base),'db':str(self.s.ledger.path),'artifacts':str(self.s.ledger.artifact_root),
           'project':self.p.project_id,'task':self.p.task_id,**kwargs}
        p=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',json.dumps(c)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=os.environ.copy())
        self.children.append(p)
        p.vm_label=label
        wait(self.base/(label+'.ready'))
        self.barriers.append({'label':label,'barrier':'ready','elapsed_seconds':time.monotonic()-self.started})
        return p
    def go(self,label):
        (self.base/(label+'.go')).touch()
        self.barriers.append({'label':label,'barrier':'released','elapsed_seconds':time.monotonic()-self.started})
    def result(self,label):
        wait(self.base/(label+'.result'))
        return json.loads((self.base/(label+'.result')).read_text())
    def race(self,op,**kwargs):
        self.spawn('a',op,**kwargs); self.spawn('b',op,**kwargs)
        self.go('a'); self.go('b')
        return [self.result('a'),self.result('b')]
    def job(self, bound=True, deadline=None):
        job=self.s.schedule_runtime_job(project_id=self.p.project_id,kind=RuntimeJobKind.WORKER_TURN,
            checkpoint_key='synthetic-job',request={'synthetic':True},absolute_deadline_at=deadline or utc_now()+timedelta(minutes=2),task_id=self.p.task_id)
        self.s.start_runtime_job(job.job_id)
        if bound:
            self.s.bind_runtime_job_provider(job.job_id,thread_id='synthetic-thread',turn_id='synthetic-turn')
        return job.job_id
    def intent(self):
        a=self.s.reserve_attempt(task_id=self.p.task_id)
        i=self.s.prepare_runtime_intent(attempt_id=a.attempt_id,kind=RuntimeIntentKind.CREATE_THREAD,idempotency_key='synthetic-intent',request={'synthetic':True})
        return a.attempt_id,i.intent_id
    def test_01_two_process_attempt_reservation_has_one_winner(self):
        r=self.race('reserve'); self.assertEqual(1,sum(x['ok'] for x in r),r)
        self.assertFalse(self.effects())
    def test_02_two_process_budget_admission_has_one_winner(self):
        r=self.race('budget'); self.assertEqual(1,sum(x['ok'] for x in r),r)
        self.assertIn('PROVIDER_EFFECT_UNKNOWN',str(r))
    def test_03_two_process_exact_binding_is_idempotent(self):
        j=self.job(False); r=self.race('bind',job=j)
        self.assertTrue(all(x['ok'] for x in r),r)
        with self.s.ledger.read() as c:
            self.assertEqual(1,c.execute("SELECT COUNT(*) FROM runtime_job_observations WHERE kind='provider_progress'").fetchone()[0])
    def test_04_two_process_conflicting_binding_has_one_winner(self):
        j=self.job(False); self.spawn('a','bind',job=j,turn='a'); self.spawn('b','bind',job=j,turn='b')
        self.go('a'); self.go('b'); r=[self.result('a'),self.result('b')]
        self.assertEqual(1,sum(x['ok'] for x in r),r)
    def test_05_interrupt_inflight_response_window_has_one_effect(self):
        j=self.job(); self.spawn('a','interrupt',job=j,hold=True); self.go('a'); wait(self.base/'a.inside')
        self.spawn('b','interrupt',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        (self.base/'a.release').touch(); self.assertTrue(self.result('a')['ok'])
        self.assertEqual(1,len(self.effects()))
    def test_06_killed_interrupt_sender_is_observed_without_resend(self):
        j=self.job(); p=self.spawn('a','interrupt',job=j,hold=True); self.go('a'); wait(self.base/'a.inside')
        p.kill(); p.communicate(timeout=5)
        self.spawn('b','reattach',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.assertEqual(1,len(self.effects()))
        job=self.s.load_runtime_job(j); self.assertEqual('completed',job.provider_terminal_status)
        self.assertEqual(1,len((self.base/'reads.jsonl').read_text().splitlines()))
    def test_07_interrupt_response_loss_is_unknown_not_terminal_or_retry(self):
        j=self.job(); self.spawn('a','interrupt',job=j,response_loss=True); self.go('a'); self.assertTrue(self.result('a')['ok'])
        self.spawn('b','interrupt',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.assertEqual(1,len(self.effects())); self.assertIsNone(self.s.load_runtime_job(j).provider_terminal_status)
    def test_08_reservation_without_delivery_recovers_once(self):
        j=self.job(); self.spawn('a','begin_interrupt',job=j); self.go('a'); self.assertTrue(self.result('a')['value'])
        self.spawn('b','interrupt',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.assertEqual(1,len(self.effects()))
    def test_09_same_intent_key_is_idempotent_across_processes(self):
        a=self.s.reserve_attempt(task_id=self.p.task_id).attempt_id
        r=self.race('intent',attempt=a); self.assertTrue(all(x['ok'] for x in r),r)
        self.assertEqual(r[0]['value'],r[1]['value'])
    def test_10_same_receipt_is_idempotent_across_processes(self):
        _,i=self.intent(); r=self.race('receipt',intent=i,bound_at=utc_now().isoformat())
        self.assertTrue(all(x['ok'] for x in r),r); self.assertEqual(r[0]['value'],r[1]['value'])
    def test_11_stale_file_after_intent_refuses_effect_marker(self):
        a,i=self.intent(); self.spawn('a','effect',attempt=a,intent=i)
        (self.p.workspace/'app.py').write_text('synthetic stale input\n')
        self.go('a'); r=self.result('a'); self.assertFalse(r['ok'],r); self.assertEqual('StaleExecutionInputError',r['error_type'])
        with self.s.ledger.read() as c:
            self.assertEqual(0,c.execute("SELECT COUNT(*) FROM history_events WHERE event_type='runtime.effect_dispatching'").fetchone()[0])
    def test_12_expired_job_after_intent_refuses_effect_marker(self):
        a,i=self.intent(); j=self.job(deadline=utc_now()+timedelta(milliseconds=100))
        self.spawn('a','effect',attempt=a,intent=i,job=j); time.sleep(.12); self.go('a')
        r=self.result('a'); self.assertFalse(r['ok'],r); self.assertIn('RUNTIME_JOB_NOT_RUNNING',r['error'])
    def test_13_collector_loss_late_binding_preserves_exact_turn_without_terminal(self):
        j=self.job(False); self.s.record_runtime_job_observation(j,kind=RuntimeJobObservationKind.COLLECTOR_LOST,payload={'reason':'synthetic collector loss'})
        r=self.race('bind',job=j); self.assertTrue(all(x['ok'] for x in r),r)
        job=self.s.load_runtime_job(j); self.assertEqual('collector_lost',job.status.value); self.assertIsNone(job.provider_terminal_status)
    def test_14_unknown_receipt_reconciliation_never_repeats_effect(self):
        _,i=self.intent()
        p=self.spawn('provider','provider_create_response_loss',intent=i); self.go('provider'); self.assertEqual(23,p.wait(timeout=15))
        self.assertEqual((i,),self.s.recover_inspect(self.p.project_id))
        self.spawn('a','receipt',intent=i,bound_at=utc_now().isoformat()); self.go('a'); self.assertFalse(self.result('a')['ok'])
        self.spawn('b','receipt',intent=i,bound_at=utc_now().isoformat(),reconcile=True); self.go('b'); self.assertTrue(self.result('b')['ok'])
        with self.s.ledger.read() as c:
            self.assertEqual('received',c.execute('SELECT status FROM runtime_intents WHERE id=?',(i,)).fetchone()[0])
        self.assertEqual(1,len(self.effects()))
    def test_15_unbound_interrupt_is_delivered_once_after_late_binding(self):
        j=self.job(False); self.spawn('a','interrupt',job=j); self.go('a'); self.assertTrue(self.result('a')['ok'])
        self.assertFalse(self.effects())
        self.spawn('b','bind_supervisor',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.spawn('c','interrupt',job=j); self.go('c'); self.assertTrue(self.result('c')['ok'])
        self.assertEqual(1,len(self.effects())); self.assertIsNone(self.s.load_runtime_job(j).provider_terminal_status)
    @unittest.skipUnless(hasattr(EngineService,'_claim_runtime_job_interrupt_delivery'),'candidate-only durable claim crash boundary')
    def test_16_crash_after_claim_before_send_does_not_invent_delivery_or_retry(self):
        j=self.job(); self.s.begin_runtime_job_interrupt(j,payload={'reason':'workflow_paused'})
        p=self.spawn('a','claim_interrupt',job=j); self.go('a'); self.assertEqual(24,p.wait(timeout=15))
        self.spawn('b','interrupt',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.assertFalse(self.effects()); self.assertIsNone(self.s.load_runtime_job(j).provider_terminal_status)
        self.spawn('c','reattach',job=j); self.go('c'); self.assertTrue(self.result('c')['ok'])
        self.assertFalse(self.effects()); self.assertEqual('completed',self.s.load_runtime_job(j).provider_terminal_status)
    def test_17_workflow_revocation_after_intent_refuses_effect_marker(self):
        a,i=self.intent(); self.spawn('a','effect',attempt=a,intent=i)
        self.s.set_workflow_control(self.p.project_id,state='cancelled',reason='synthetic workflow cancellation')
        self.go('a'); r=self.result('a'); self.assertFalse(r['ok'],r); self.assertIn('WORKFLOW_CANCELLED',r['error'])
        with self.s.ledger.read() as c:
            self.assertEqual(0,c.execute("SELECT COUNT(*) FROM history_events WHERE event_type='runtime.effect_dispatching'").fetchone()[0])
        self.assertFalse(self.effects())
    def test_18_cancelled_late_binding_still_delivers_one_interrupt(self):
        j=self.job(False); self.s.begin_runtime_job_interrupt(j,payload={'reason':'workflow_cancelled'})
        self.s.cancel_runtime_job(j,reason='synthetic cancellation')
        self.spawn('a','bind_supervisor',job=j); self.go('a'); self.assertTrue(self.result('a')['ok'])
        self.spawn('b','bind_supervisor',job=j); self.go('b'); self.assertTrue(self.result('b')['ok'])
        self.assertEqual(1,len(self.effects())); self.assertEqual('cancelled',self.s.load_runtime_job(j).status.value)
        self.assertIsNone(self.s.load_runtime_job(j).provider_terminal_status)
    def test_19_provider_terminal_before_interrupt_starts_no_effect(self):
        j=self.job(); self.s.record_runtime_job_observation(j,kind=RuntimeJobObservationKind.PROVIDER_TERMINAL,
            payload={'result':{'synthetic':'completed'}},provider_terminal=True,terminal_status='completed')
        r=self.race('interrupt',job=j); self.assertTrue(all(x['ok'] for x in r),r)
        self.assertFalse(self.effects()); self.assertEqual('completed',self.s.load_runtime_job(j).provider_terminal_status)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--child':
        sys.exit(child(json.loads(sys.argv[2])))
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FaultTests))
    Path(os.environ['VM_G_EVIDENCE']).write_text(json.dumps({'cases':EVIDENCE,'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)},ensure_ascii=False,indent=2)+'\n')
    sys.exit(not result.wasSuccessful())
