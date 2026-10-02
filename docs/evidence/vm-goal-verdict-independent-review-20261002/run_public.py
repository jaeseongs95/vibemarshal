from pathlib import Path
import os,subprocess,datetime,time,hashlib,json
out=Path(__file__).parent
for label in ('base','candidate'):
 args=['/workspace/cloud-setup/venv/bin/python',str(out/'public_boundary.py')];env=dict(os.environ);env['PYTHONPATH']=str(out/label/'src')+os.pathsep+str(out/label);env['PYTHONDONTWRITEBYTECODE']='1'
 start=datetime.datetime.now(datetime.timezone.utc).isoformat();t=time.monotonic();r=subprocess.run(args,cwd=out/label,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60)
 p=out/('public-'+label+'.raw.txt');p.write_bytes(r.stdout)
 record=dict(name='public-'+label,argv=args,cwd=str(out/label),env_overrides={k:env[k] for k in ['PYTHONPATH','PYTHONDONTWRITEBYTECODE']},started_at=start,ended_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),wall_seconds=time.monotonic()-t,exit_code=r.returncode,raw_file=p.name,raw_sha256=hashlib.sha256(r.stdout).hexdigest())
 with (out/'commands.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
 print(json.dumps(record)); print(r.stdout.decode())
