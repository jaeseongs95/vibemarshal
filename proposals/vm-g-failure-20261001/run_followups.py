from pathlib import Path
from datetime import datetime,timezone
import json,os,subprocess,sys,time,hashlib
root=Path('/workspace/vm-g-artifacts');repo='/workspace/vm-g-failure-shard'
mode=sys.argv[1]
if mode=='baseline-deadline':
    source=repo+'/src'
    names=['test_deadline_terminal_observation_grace_ends_as_collector_lost']
else:
    source=str(root/'candidate/src')
    names=['test_collector_loss_and_restart_are_not_provider_terminal','test_restart_reattach_of_every_typed_job_kind_is_result_unavailable','test_deadline_interrupt_is_bounded_and_not_terminal','test_restarted_supervisor_close_marks_and_interrupts_bound_job','test_restarted_supervisor_observes_exact_running_turn_before_terminal','test_runtime_job_lifecycle_and_close_do_not_decide_core_completion','test_turn_started_progress_binds_once_and_rejects_a_different_turn']
cmd=[sys.executable,'-m','unittest','-v']+['tests.test_engine_runtime_job_supervisor.RuntimeJobSupervisorTests.'+n for n in names]
env=os.environ.copy();env.update(PYTHONPATH=source+':'+repo,PYTHONDONTWRITEBYTECODE='1')
start=datetime.now(timezone.utc);timer=time.monotonic()
with (root/(mode+'-raw.log')).open('w') as f:r=subprocess.run(cmd,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT)
meta={'command':cmd,'cwd':repo,'PYTHONPATH':env['PYTHONPATH'],'start_utc':start.isoformat(),'end_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-timer,'exit':r.returncode,'raw_sha256':hashlib.sha256((root/(mode+'-raw.log')).read_bytes()).hexdigest()}
(root/(mode+'-run.json')).write_text(json.dumps(meta,indent=2)+'\n');print(json.dumps(meta));sys.exit(r.returncode)
