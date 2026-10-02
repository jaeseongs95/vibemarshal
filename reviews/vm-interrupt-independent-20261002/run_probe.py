"""각 discriminating 실행의 exact argv·선택 환경·UTC·exit·raw bytes를 보존한다."""
import datetime
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
root=Path(__file__).resolve().parent
label=sys.argv[1]
source=root/label
argv=[sys.executable,str(root/'independent_probe.py'),str(source),label]
env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(source/'src')+':'+str(source)}
start=datetime.datetime.now(datetime.timezone.utc).isoformat();t=time.monotonic()
r=subprocess.run(argv,cwd=source,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=55)
end=datetime.datetime.now(datetime.timezone.utc).isoformat()
for name,data in [('stdout',r.stdout),('stderr',r.stderr)]:
 (root/(label+'-'+name+'.log')).write_bytes(data)
receipt={'task_id':'vm-interrupt-independent-review-20261002','argv':argv,'cwd':str(source),'selected_environment':env,
 'start_utc':start,'end_utc':end,'elapsed_seconds':time.monotonic()-t,'exit_code':r.returncode,
 'stdout_sha256':hashlib.sha256(r.stdout).hexdigest(),'stderr_sha256':hashlib.sha256(r.stderr).hexdigest(),
 'harness_sha256':hashlib.sha256((root/'independent_probe.py').read_bytes()).hexdigest(),
 'requested_model':'gpt-6.1-sol','requested_effort':'high','observed_model':None,'observed_effort':None,'live_provider_calls':0}
(root/(label+'-command.json')).write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False))
print(r.stdout.decode(),end='');print(r.stderr.decode(),end='')
sys.exit(r.returncode)
