"""독립 Core API 관측: 합성 임시 원장만 사용하며 Windows guard를 변경하지 않는다."""
from __future__ import annotations
import hashlib, json, sys, time
from datetime import timedelta
from pathlib import Path
from flowmarshal.canonical import canonical_json, sha256_digest, sha256_bytes
from flowmarshal.engine.domain import CriterionVerdict, EvidenceKind, EvidenceRecord, GoalVerdict, GoalVerdictStatus, ValidationResult, ValidationStatus, new_id, utc_now
from flowmarshal.engine.service import EngineServiceError
from flowmarshal.engine import service as source_service
from flowmarshal.engine.cli import build_parser
from flowmarshal.engine.runtime import owner_lock_platform_supported
from flowmarshal.engine.validation_execution import run_command_validation
from tests.test_engine_ledger_service import EngineServiceFixture
from tests.test_engine_goal_authorization import EngineGoalAuthorizationTests

class Fixture(EngineServiceFixture):
    internal_revision_evaluation = EngineGoalAuthorizationTests.internal_revision_evaluation
    register_internal_revision = EngineGoalAuthorizationTests.register_internal_revision


def snapshot(f):
    with f.ledger.read() as c:
        return {t: hashlib.sha256(canonical_json([dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]).encode()).hexdigest()
                for t in ('projects','plan_revisions','task_contracts','goal_verdicts','history_events','evidence_records','validation_results','attempts','runtime_intents','runtime_receipts','task_completion_reuse')}

def result(f, task=False, status=ValidationStatus.PASS, evaluated_at=None, plan=None, evidence=None):
    plan = plan or f.plan
    value=ValidationResult(validation_result_id=new_id('validation_result'),validation_id=plan.definition.tasks[0].validations[0].validation_id if task else plan.definition.integration_validations[0].validation_id, task_id=plan.definition.tasks[0].task_id if task else None, status=status,evidence_ids=() if status is ValidationStatus.NOT_RUN else ((evidence or f.evidence).evidence_id,),rationale='독립 합성 재검사',evaluated_at=evaluated_at or utc_now())
    f.service.record_validation(project_id=f.project_id,plan_revision_id=plan.plan_revision_id,result=value)
    return value

def verdict(f, selected, plan=None, status=GoalVerdictStatus.SATISFIED):
    plan=plan or f.plan
    return GoalVerdict(goal_verdict_id=new_id('goal_verdict'),goal_contract_digest=plan.definition.goal_contract_digest,plan_activation_digest=plan.activation_digest,status=status,criteria=(CriterionVerdict(criterion_id='ac_one',status=ValidationStatus.PASS if status is GoalVerdictStatus.SATISFIED else ValidationStatus.INCONCLUSIVE,evidence_ids=(f.evidence.evidence_id,),rationale='독립 합성 판정'),),integration_validation_result_ids=(selected.validation_result_id,),evaluated_at=utc_now())

def prepare(f, reusable=False):
    f.activate(); spec=f.spec(); f.service.materialize_execution_spec(spec,inventory=f.inventory)
    attempt=f.service.reserve_attempt(task_id=f.task.task_id);f.service.finish_attempt(attempt_id=attempt.attempt_id,succeeded=True)
    if reusable:
        observed={'path':'AGENTS.md','after_digest':sha256_bytes((f.root/'AGENTS.md').read_bytes())}
        file=EvidenceRecord(evidence_id=new_id('evidence'),project_id=f.project_id,task_id=f.task.task_id,attempt_id=attempt.attempt_id,kind=EvidenceKind.FILE,source_ref='AGENTS.md',observation=canonical_json(observed),content_digest=sha256_digest(observed),observed_at=utc_now())
        f.service.record_evidence(file)
        with f.ledger.read() as c: row=c.execute('SELECT * FROM task_contracts WHERE id=?',(f.task.task_id,)).fetchone()
        command=run_command_validation(f.service,row,spec.definition.validation_steps[0])
        assert command.action.value=='validated',command
        with f.ledger.read() as c: payload=c.execute('SELECT payload_json FROM evidence_records WHERE id=?',(command.evidence_ids[0],)).fetchone()[0]
        f.evidence=EvidenceRecord.model_validate_json(payload)
    else:
        f.evidence=EvidenceRecord(evidence_id=new_id('evidence'),project_id=f.project_id,task_id=f.task.task_id,attempt_id=attempt.attempt_id,kind=EvidenceKind.TEST,source_ref='synthetic:independent-review',observation='독립 합성 검사 관측',content_digest=sha256_digest('독립 합성 검사 관측'),observed_at=utc_now())
        f.service.record_evidence(f.evidence);result(f,task=True)
    f.service.complete_task(f.task.task_id)
    f.integration=result(f)

cases=['current_pass','integration_fail','integration_inconclusive','task_fail','task_not_run','task_inconclusive','successive_pass_old_id','successive_pass_latest_id','integration_fail_then_pass','superseded_satisfied','superseded_inconclusive','backdated_integration_fail','backdated_task_fail','reuse_valid_pass','reuse_late_source_fail','reuse_late_middle_fail']
print(json.dumps({'import_origin':source_service.__file__,'source_sha256':hashlib.sha256(Path(source_service.__file__).read_bytes()).hexdigest(),'owner_lock_platform_supported':owner_lock_platform_supported(),'python':sys.version},sort_keys=True))
for name in cases:
    started=time.monotonic(); f=Fixture(); f.setUp()
    try:
        prepare(f,reusable=name.startswith('reuse_'))
        selected=f.integration; target=f.plan; replacement=None; status=GoalVerdictStatus.SATISFIED
        if name.startswith('integration_') and name!='integration_fail_then_pass': result(f,status=ValidationStatus.FAIL if name=='integration_fail' else ValidationStatus.INCONCLUSIVE)
        elif name.startswith('task_'): result(f,task=True,status={'task_fail':ValidationStatus.FAIL,'task_not_run':ValidationStatus.NOT_RUN,'task_inconclusive':ValidationStatus.INCONCLUSIVE}[name])
        elif name.startswith('successive_pass_'):
            newest=result(f)
            if name.endswith('latest_id'): selected=newest
        elif name=='integration_fail_then_pass': result(f,status=ValidationStatus.FAIL);selected=result(f)
        elif name.startswith('superseded_'):
            replacement,_=f.register_internal_revision();f.service.activate_authorized_plan(plan_revision_id=replacement.plan_revision_id)
            if name.endswith('inconclusive'): status=GoalVerdictStatus.INCONCLUSIVE
        elif name.startswith('backdated_'): result(f,task=name=='backdated_task_fail',status=ValidationStatus.FAIL,evaluated_at=f.integration.evaluated_at-timedelta(days=1))
        elif name.startswith('reuse_'):
            target,reused=f.register_internal_revision();f.service.activate_authorized_plan(plan_revision_id=target.plan_revision_id)
            failed_plan=f.plan
            if name=='reuse_late_middle_fail':
                failed_plan=target;target,reused=f.register_internal_revision();f.service.activate_authorized_plan(plan_revision_id=target.plan_revision_id)
            with f.ledger.read() as c:
                assert c.execute('SELECT status FROM task_contracts WHERE id=?',(reused.task_id,)).fetchone()[0]=='completed','reuse not established'
            selected=result(f,plan=target)
            if name!='reuse_valid_pass':
                failure_evidence=EvidenceRecord(evidence_id=new_id('evidence'),project_id=f.project_id,task_id=failed_plan.definition.tasks[0].task_id,kind=EvidenceKind.TEST,source_ref='synthetic:late-recheck',observation='독립 합성 늦은 실패',content_digest=sha256_digest('독립 합성 늦은 실패'),observed_at=utc_now())
                f.service.record_evidence(failure_evidence);result(f,task=True,status=ValidationStatus.FAIL,plan=failed_plan,evidence=failure_evidence)
        with f.ledger.read() as c:
            irows=c.execute("SELECT id,status FROM validation_results WHERE plan_revision_id=? AND task_id IS NULL ORDER BY evaluated_at,rowid",(target.plan_revision_id,)).fetchall()
            effective=f.service.effective_task_validation_results(c,target.definition.tasks[0].task_id)
            latest_task={r['validation_id']:r['status'] for r in effective}
        before=snapshot(f); error=None
        try: f.service.record_goal_verdict(project_id=f.project_id,plan_revision_id=target.plan_revision_id,verdict=verdict(f,selected,target,status))
        except EngineServiceError as e: error=str(e)
        after=snapshot(f)
        with f.ledger.read() as c:
            project=c.execute('SELECT active_plan_revision_id,run_state FROM projects WHERE id=?',(f.project_id,)).fetchone()
            plan_status=c.execute('SELECT status FROM plan_revisions WHERE id=?',(target.plan_revision_id,)).fetchone()[0]
        preserved=all(before[t]==after[t] for t in ('evidence_records','validation_results','attempts','runtime_intents','runtime_receipts','task_completion_reuse'))
        assert preserved,'provenance mutation'
        if error: assert before==after,'non-atomic rejection'
        assert f.ledger.verify_history(f.project_id),'history hash chain failed'
        print(json.dumps(dict(case=name,accepted=error is None,error=error,rejection_atomic=(before==after) if error else None,provenance_preserved=preserved,latest_task=latest_task,integration_statuses_by_evaluated_at=[r['status'] for r in irows],active_plan_is_target=project['active_plan_revision_id']==target.plan_revision_id,active_plan_is_replacement=bool(replacement and project['active_plan_revision_id']==replacement.plan_revision_id),project_run_state=project['run_state'],target_plan_status=plan_status,wall_seconds=time.monotonic()-started),ensure_ascii=False,sort_keys=True))
    finally:f.tearDown()
# parse-only public CLI check: no operational file or provider access.
for args in [['validate','goal','--project-id','project_synthetic','--plan-revision-id','plan_revision_synthetic','--verdict-file','synthetic.json'],['validate','task','--project-id','project_synthetic','--plan-revision-id','plan_revision_synthetic','--result-file','synthetic.json']]:
    parsed=build_parser().parse_args(args);print(json.dumps({'cli_parse':args,'handler':parsed.handler.__name__},sort_keys=True))
