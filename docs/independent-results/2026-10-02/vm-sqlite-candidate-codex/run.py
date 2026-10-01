import datetime
import json
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(__file__).parent
command = sys.argv[1:]
start = datetime.datetime.now(datetime.timezone.utc).isoformat()
tick = time.monotonic()
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
end = datetime.datetime.now(datetime.timezone.utc).isoformat()
record = dict(argv=command, cwd=str(pathlib.Path.cwd()), started_at=start, ended_at=end,
              elapsed_seconds=time.monotonic() - tick, exit_code=result.returncode,
              stdout=result.stdout.decode('utf-8', 'replace'), stderr=result.stderr.decode('utf-8', 'replace'))
with (root / 'commands.jsonl').open('a') as stream:
    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
sys.stdout.write(record['stdout'])
sys.stderr.write(record['stderr'])
print(json.dumps({key: record[key] for key in ('started_at', 'ended_at', 'elapsed_seconds', 'exit_code')}))
sys.exit(result.returncode)
