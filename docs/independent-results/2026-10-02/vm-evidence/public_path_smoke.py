"""설치 wheel의 provider 없는 공개 CLI 경로를 별도 프로세스로 검사한다."""
from pathlib import Path
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys

HERE = Path(os.environ['VM_EVIDENCE_ROOT']).resolve()
CLI = Path(sys.executable).parent / 'flowmarshal-engine'
ROOT = HERE / os.environ.get('VM_PUBLIC_RUN_NAME', 'public smoke 최종')
ROOT.mkdir(exist_ok=True)
project = ROOT / 'project with spaces'
project.mkdir(exist_ok=True)
(project / 'AGENTS.md').write_text('합성 임시 프로젝트\n', encoding='utf-8')
(project / 'app.py').write_text('value = 1\n', encoding='utf-8')
db = ROOT / 'state' / 'engine.sqlite3'
artifacts = ROOT / 'artifacts'
env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ('TOKEN', 'API_KEY', 'AUTHORIZATION', 'SECRET'))}
env.pop('PYTHONPATH', None)
env['HOME'] = str(HERE / 'home')
env['CODEX_HOME'] = str(HERE / 'home' / '.codex')
commands = []

def run(name, args, expected=0):
    command = [str(CLI), *args]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    (HERE / (name + '.stdout')).write_text(result.stdout)
    (HERE / (name + '.stderr')).write_text(result.stderr)
    commands.append({'name': name, 'argv': command, 'cwd': str(ROOT), 'exit': result.returncode, 'expected_exit': expected})
    assert result.returncode == expected, (name, result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout.startswith('{') else result.stdout

try:
    assert 'site-packages' in __import__('flowmarshal').__file__
    run('public-help', ['--help'])
    run('config-help', ['config', 'init', '--help'])
    common = ['--db', str(db), '--artifacts', str(artifacts)]
    init = run('project-init', [*common, 'project', 'init', '--name', '공백 portable smoke', '--root', str(project)])
    pid = init['project_id']
    run('project-show-restart', [*common, 'project', 'show', '--project-id', pid])
    run('goal-create', [*common, 'goal', 'create', '--project-id', pid, '--request', '합성 검증', '--outcome', 'CLI 동작', '--acceptance', '단위 검사'])
    goal = run('goal-show-restart', [*common, 'goal', 'show', '--project-id', pid])
    assert goal['is_active'] is True
    context = run('plan-search-context', [*common, 'plan', 'search', '--project-id', pid])
    assert context['status'] == 'context_ready'
    run('public-status', [*common, 'status', '--project-id', pid])
    run('missing-project-rejected', [*common, 'project', 'show', '--project-id', 'project_'+'0'*32], 2)
    assert not (ROOT / '.flowmarshal-engine').exists(), 'cwd fallback database was created'
    result = {'passed': True, 'commands': commands, 'provider_calls': 0, 'scope': 'synthetic local CLI only; no release qualification', 'import_origin': __import__('flowmarshal').__file__}
except BaseException as exc:
    result = {'passed': False, 'commands': commands, 'error': repr(exc)}
    raise
finally:
    (HERE / 'public-path-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
