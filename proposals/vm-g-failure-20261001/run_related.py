import os,subprocess,time,json,hashlib,sys
from pathlib import Path
from datetime import datetime,timezone
root=Path('/workspace/vm-g-artifacts'); repo='/workspace/vm-g-failure-shard'
names=['test_collector_loss_and_restart_are_not_provider_terminal','test_restart_reattach_of_every_typed_job_kind_is_result_unavailable','test_restart_restores_durable_target_result_for_every_job_kind','test_deadline_interrupt_is_bounded_and_not_terminal','test_deadline_terminal_observation_grace_ends_as_collector_lost','test_sdk_close_is_bounded_and_does_not_decide_completion','test_restarted_supervisor_close_marks_and_interrupts_bound_job','test_restarted_supervisor_observes_exact_running_turn_before_terminal','test_runtime_job_lifecycle_and_close_do_not_decide_core_completion','test_turn_started_progress_binds_once_and_rejects_a_different_turn']
command=[sys.executable,'-m','unittest','-v']+['tests.test_engine_runtime_job_supervisor.RuntimeJobSupervisorTests.'+n for n in names]
env=os.environ.copy();env.update(PYTHONPATH=str(root/'candidate/src')+':'+repo,PYTHONDONTWRITEBYTECODE='1')
start=datetime.now(timezone.utc);clock=time.monotonic()
with (root/'related-raw.log').open('w') as f:r=subprocess.run(command,cwd=repo,env=env,stdout=f,stderr=subprocess.STDOUT)
meta={'command':command,'cwd':repo,'PYTHONPATH':env['PYTHONPATH'],'start_utc':start.isoformat(),'end_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-clock,'exit':r.returncode,'raw_sha256':hashlib.sha256((root/'related-raw.log').read_bytes()).hexdigest()}
(root/'related-run.json').write_text(json.dumps(meta,indent=2)+'\n');print(json.dumps(meta));sys.exit(r.returncode)
