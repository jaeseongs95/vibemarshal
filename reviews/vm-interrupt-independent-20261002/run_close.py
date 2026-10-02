import datetime,hashlib,json,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent;label=sys.argv[1];source=root/label
argv=[sys.executable,str(root/'close_bypass_probe.py'),str(source),label];env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(source/'src')+':'+str(source)}
start=datetime.datetime.now(datetime.timezone.utc).isoformat();clock=time.monotonic()
r=subprocess.run(argv,cwd=source,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
out={'task_id':'vm-interrupt-independent-review-20261002','argv':argv,'cwd':str(source),'selected_environment':env,'start_utc':start,'end_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-clock,'exit_code':r.returncode,'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(),'stderr_sha256':hashlib.sha256(r.stderr).hexdigest(),'harness_sha256':hashlib.sha256((root/'close_bypass_probe.py').read_bytes()).hexdigest(),'live_provider_calls':0}
for name,body in [('stdout',r.stdout),('stderr',r.stderr)]:(root/(label+'-close-'+name+'.log')).write_bytes(body)
(root/(label+'-close-command.json')).write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out));print(r.stdout.decode(),end='');print(r.stderr.decode(),end='');sys.exit(r.returncode)
