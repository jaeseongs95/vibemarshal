import datetime,hashlib,json,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parent
argv=[sys.executable,str(root/'verify_inputs.py')];start=datetime.datetime.now(datetime.timezone.utc).isoformat();clock=time.monotonic()
r=subprocess.run(argv,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
out={'argv':argv,'cwd':str(root),'start_utc':start,'end_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'elapsed_seconds':time.monotonic()-clock,'exit_code':r.returncode,'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(),'stderr_sha256':hashlib.sha256(r.stderr).hexdigest()}
for name,body in [('stdout',r.stdout),('stderr',r.stderr)]:(root/('verification-'+name+'.log')).write_bytes(body)
(root/'verification-command.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out));print(r.stdout.decode(),end='');print(r.stderr.decode(),end='');sys.exit(r.returncode)
