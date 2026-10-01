from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,os,subprocess,sys,time
root=Path('/workspace/vm-g-artifacts')
mode=sys.argv[1]
repo='/workspace/vm-g-failure-shard'
source=repo+'/src' if mode=='base' else str(root/'candidate/src')
env=os.environ.copy(); env.update(VM_G_REPO=repo,VM_G_EVIDENCE=str(root/(mode+'-evidence.json')),PYTHONPATH=source+':'+repo,PYTHONDONTWRITEBYTECODE='1')
command=[sys.executable,str(root/'vm_g_faults.py')]
start=datetime.now(timezone.utc); clock=time.monotonic()
with (root/(mode+'-raw.log')).open('w') as log:
    result=subprocess.run(command,env=env,cwd=repo,stdout=log,stderr=subprocess.STDOUT)
metadata={'command':command,'cwd':repo,'environment_overrides':{k:env[k] for k in ('VM_G_REPO','VM_G_EVIDENCE','PYTHONPATH','PYTHONDONTWRITEBYTECODE')},'start_utc':start.isoformat(),'end_utc':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':time.monotonic()-clock,'exit':result.returncode,'raw_sha256':hashlib.sha256((root/(mode+'-raw.log')).read_bytes()).hexdigest(),'evidence_sha256':hashlib.sha256((root/(mode+'-evidence.json')).read_bytes()).hexdigest()}
(root/(mode+'-run.json')).write_text(json.dumps(metadata,indent=2)+'\n')
print(json.dumps(metadata)); sys.exit(result.returncode)
