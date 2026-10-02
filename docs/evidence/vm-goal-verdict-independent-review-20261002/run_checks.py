from pathlib import Path
import subprocess, datetime, hashlib, json, os, time
out=Path(__file__).parent; python='/workspace/cloud-setup/venv/bin/python'
jobs=[('independent-base','base',[python,str(out/'probe.py')]),('independent-candidate','candidate',[python,str(out/'probe.py')]),('permanent-candidate','candidate',[python,'-m','unittest','-v','tests.test_engine_goal_verdict_authority.GoalVerdictAuthorityTests'])]
for name,label,args in jobs:
 env=dict(os.environ);env['PYTHONPATH']=str(out/label/'src')+os.pathsep+str(out/label);env['PYTHONDONTWRITEBYTECODE']='1'
 start=datetime.datetime.now(datetime.timezone.utc).isoformat();t=time.monotonic()
 run=subprocess.run(args,cwd=out/label,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
 raw=out/(name+'.raw.txt');raw.write_bytes(run.stdout)
 record=dict(name=name,argv=args,cwd=str(out/label),env_overrides={k:env[k] for k in ['PYTHONPATH','PYTHONDONTWRITEBYTECODE']},started_at=start,ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),wall_seconds=time.monotonic()-t,exit_code=run.returncode,raw_file=raw.name,raw_sha256=hashlib.sha256(run.stdout).hexdigest())
 with (out/'commands.jsonl').open('a') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
 print(json.dumps(record));print(run.stdout.decode())
