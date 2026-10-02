"""source-tree TrustedConsoleHost의 수동 validation 경로; provider E2E가 아니다."""
from public_fixture import Fixture, prepare, result, verdict, snapshot
import os, subprocess, sys, json, hashlib, time
from pathlib import Path
from flowmarshal.engine.domain import ValidationResult, ValidationStatus, new_id, utc_now
for case in ('completed_task_late_fail','successive_pass_old_id','superseded_satisfied'):
 f=Fixture();f.setUp()
 try:
  prepare(f);replacement=None
  def command(kind,path):
   argv=[sys.executable,'-m','flowmarshal.engine.console_host','--db',str(f.ledger.path),'--artifacts',str(f.ledger.artifact_root),'validate',kind,'--project-id',f.project_id,'--plan-revision-id',f.plan.plan_revision_id,'--'+('result-file' if kind=='task' else 'verdict-file'),str(path)]
   t=time.monotonic();r=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=15)
   print(json.dumps({'case':case,'argv':argv,'cwd':str(Path.cwd()),'wall_seconds':time.monotonic()-t,'exit_code':r.returncode,'stdout':r.stdout.decode(),'stdout_sha256':hashlib.sha256(r.stdout).hexdigest()},ensure_ascii=False));return r
  if case=='completed_task_late_fail':
   late=ValidationResult(validation_result_id=new_id('validation_result'),validation_id=f.task.validations[0].validation_id,task_id=f.task.task_id,status=ValidationStatus.FAIL,evidence_ids=(f.evidence.evidence_id,),rationale='합성 CLI 재검사 실패',evaluated_at=utc_now())
   path=f.base/'result.json';path.write_text(late.model_dump_json());assert command('task',path).returncode==0
   with f.ledger.read() as c:assert c.execute('SELECT status FROM task_contracts WHERE id=?',(f.task.task_id,)).fetchone()[0]=='completed'
  elif case=='successive_pass_old_id':result(f)
  else:replacement,_=f.register_internal_revision();f.service.activate_authorized_plan(plan_revision_id=replacement.plan_revision_id)
  path=f.base/'verdict.json';path.write_text(verdict(f,f.integration).model_dump_json());before=snapshot(f);run=command('goal',path);after=snapshot(f)
  if run.returncode:assert before==after
  with f.ledger.read() as c: project=c.execute('SELECT active_plan_revision_id,run_state FROM projects WHERE id=?',(f.project_id,)).fetchone()
  print(json.dumps({'case':case,'accepted':run.returncode==0,'rejection_atomic':before==after if run.returncode else None,'run_state':project['run_state'],'replacement_preserved':project['active_plan_revision_id']==replacement.plan_revision_id if replacement else None},ensure_ascii=False))
 finally:f.tearDown()
