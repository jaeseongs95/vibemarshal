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

