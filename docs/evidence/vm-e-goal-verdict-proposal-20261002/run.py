import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path('/workspace/vm-e-evidence')
label, cwd, *command = sys.argv[1:]
environment = os.environ.copy()
environment['PYTHONPATH'] = str(Path(cwd) / 'src') + ':' + cwd
environment['TMPDIR'] = str(root / 'fixtures')
environment['PYTHONDONTWRITEBYTECODE'] = '1'
start = datetime.datetime.now(datetime.timezone.utc).isoformat()
clock = time.monotonic()
result = subprocess.run(command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
elapsed = time.monotonic() - clock
raw = root / (label + '.raw.txt')
raw.write_bytes(result.stdout)
record = {'label':label, 'cwd':cwd, 'argv':command, 'started_at':start, 'duration_seconds':elapsed, 'exit_code':result.returncode, 'raw_path':str(raw), 'raw_sha256':hashlib.sha256(result.stdout).hexdigest()}
with (root / 'commands.jsonl').open('a') as f:
    f.write(json.dumps(record) + '\n')
print(result.stdout.decode(errors='replace'))
print(json.dumps(record))
sys.exit(result.returncode)
