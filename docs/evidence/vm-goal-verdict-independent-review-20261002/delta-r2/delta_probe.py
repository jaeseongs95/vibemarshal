"""Reviewer 작성 R2 호환성 probe: 실제 provider·운영 DB 없이 Core API만 검증."""
from historical_fixture import Fixture, snapshot, result, verdict
from flowmarshal.engine.domain import EvidenceRecord, EvidenceKind, RuntimeIntentKind, ValidationStatus, new_id, utc_now, GoalVerdict
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.canonical import sha256_digest
from flowmarshal.engine import service as source_service
import json,hashlib,os
from pathlib import Path

def prepare(f):
 f.activate();f.service.materialize_execution_spec(f.spec(),inventory=f.inventory)
 a=f.service.reserve_attempt(task_id=f.task.task_id)
 intent=f.service.prepare_runtime_intent(attempt_id=a.attempt_id,kind=RuntimeIntentKind.CREATE_THREAD,idempotency_key=new_id('review_probe'),request={'synthetic':True})
 f.service.record_runtime_receipt(intent_id=intent.intent_id,provider_operation_id=new_id('synthetic_operation'),response={'synthetic':True})
 f.service.finish_attempt(attempt_id=a.attempt_id,succeeded=True)
 f.evidence=EvidenceRecord(evidence_id=new_id('evidence'),project_id=f.project_id,task_id=f.task.task_id,attempt_id=a.attempt_id,kind=EvidenceKind.TEST,source_ref='synthetic:r2-independent-review',observation='합성 R2 compatibility 직접 관측',content_digest=sha256_digest('합성 R2 compatibility 직접 관측'),observed_at=utc_now())
 f.service.record_evidence(f.evidence);result(f,task=True);f.service.complete_task(f.task.task_id);f.integration=result(f)

print(json.dumps({'reviewer_thread_id':os.environ.get('CODEX_THREAD_ID'),'import_origin':source_service.__file__,'source_sha256':hashlib.sha256(Path(source_service.__file__).read_bytes()).hexdigest()}))
cases=('valid_equivalent_old_pass_retained','latest_integration_fail','latest_integration_inconclusive','latest_task_fail','latest_task_inconclusive','wrong_goal_digest','wrong_activation_digest','unknown_result_id','failed_result_id_after_new_pass','other_plan_pass_id','superseded_satisfied')
for name in cases:
 f=Fixture();f.setUp()
 try:
  prepare(f);old=f.integration;selected=old;replacement=None;submitted=None
  if name=='valid_equivalent_old_pass_retained':
   new=result(f);assert old.validation_result_id!=new.validation_result_id;assert old.evidence_ids==new.evidence_ids and old.goal_validation_binding_digest==new.goal_validation_binding_digest
  elif name.startswith('latest_integration_'):result(f,status=ValidationStatus.FAIL if name.endswith('fail') else ValidationStatus.INCONCLUSIVE)
  elif name.startswith('latest_task_'):result(f,task=True,status=ValidationStatus.FAIL if name.endswith('fail') else ValidationStatus.INCONCLUSIVE)
  elif name=='failed_result_id_after_new_pass':selected=result(f,status=ValidationStatus.FAIL);result(f)
  elif name=='other_plan_pass_id':
   other,_=f.register_internal_revision();selected=result(f,plan=other)
  elif name=='superseded_satisfied':replacement,_=f.register_internal_revision();f.service.activate_authorized_plan(plan_revision_id=replacement.plan_revision_id)
  submitted=verdict(f,selected)
  if name in ('wrong_goal_digest','wrong_activation_digest','unknown_result_id'):
   update={'wrong_goal_digest':{'goal_contract_digest':'sha256:'+'0'*64},'wrong_activation_digest':{'plan_activation_digest':'sha256:'+'0'*64},'unknown_result_id':{'integration_validation_result_ids':(new_id('validation_result'),)}}[name];submitted=submitted.model_copy(update=update)
  before=snapshot(f);error=None
  try:f.service.record_goal_verdict(project_id=f.project_id,plan_revision_id=f.plan.plan_revision_id,verdict=submitted)
  except EngineServiceError as e:error=str(e)
  after=snapshot(f)
  with f.ledger.read() as c:
   p=c.execute('SELECT active_plan_revision_id,run_state FROM projects WHERE id=?',(f.project_id,)).fetchone()
   status=c.execute('SELECT status FROM plan_revisions WHERE id=?',(f.plan.plan_revision_id,)).fetchone()[0]
   if name=='valid_equivalent_old_pass_retained':
    assert error is None,error;assert p['active_plan_revision_id'] is None and p['run_state']=='completed' and status=='completed'
    saved=GoalVerdict.model_validate_json(c.execute('SELECT payload_json FROM goal_verdicts WHERE id=?',(submitted.goal_verdict_id,)).fetchone()[0]);assert saved.integration_validation_result_ids==(old.validation_result_id,)
    assert all(before[t]==after[t] for t in ('evidence_records','validation_results','attempts','runtime_intents','runtime_receipts'))
    assert all(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]>0 for t in ('evidence_records','validation_results','attempts','runtime_intents','runtime_receipts'))
   else:
    assert error is not None,name;assert before==after,'non-atomic '+name
    assert p['active_plan_revision_id']==(replacement.plan_revision_id if replacement else f.plan.plan_revision_id)
    assert p['run_state']=='active';assert status==('superseded' if replacement else 'active')
  assert f.ledger.verify_history(f.project_id)
  print(json.dumps({'case':name,'assertions':'PASS','accepted':error is None,'error':error,'old_submitted_id_retained':name=='valid_equivalent_old_pass_retained','all_tables_unchanged_on_rejection':before==after if error else None,'provenance_preserved':all(before[t]==after[t] for t in ('evidence_records','validation_results','attempts','runtime_intents','runtime_receipts'))},ensure_ascii=False))
 finally:f.tearDown()
