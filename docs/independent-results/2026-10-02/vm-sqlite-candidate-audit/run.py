"""독립 감사 명령의 원문 stdout/stderr와 명시 argv만 보존한다."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
cwd = ROOT / 'candidate'
env = os.environ.copy()
env['PYTHONPATH'] = str(cwd / 'src') + os.pathsep + str(cwd / 'tests')
env['PYTHONDONTWRITEBYTECODE'] = '1'
env['TMPDIR'] = str(ROOT / 'synthetic-tmp')
Path(env['TMPDIR']).mkdir(exist_ok=True)
args = sys.argv[1:]
started = datetime.datetime.now(datetime.timezone.utc).isoformat()
tick = time.monotonic()
result = subprocess.run(args, cwd=cwd, env=env, capture_output=True)
record = dict(argv=args, cwd=str(cwd), explicit_environment={k: env[k] for k in
              ('PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'TMPDIR')},
              started_utc=started, ended_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
              elapsed_seconds=time.monotonic() - tick, exit_code=result.returncode,
              stdout=result.stdout.decode('utf-8', errors='replace'),
              stderr=result.stderr.decode('utf-8', errors='replace'))
with (ROOT / 'commands.jsonl').open('a') as handle:
    handle.write(json.dumps(record, ensure_ascii=False) + '\n')
sys.stdout.write(record['stdout'])
sys.stderr.write(record['stderr'])
print('AUDIT_RECEIPT', json.dumps({k: record[k] for k in ('exit_code', 'elapsed_seconds')}))
sys.exit(result.returncode)
