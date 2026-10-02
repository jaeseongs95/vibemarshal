"""Pinned Git 객체·공개 evidence·실제 import를 독립 대조한다."""
import ast
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import pydantic
import flowmarshal
from flowmarshal.engine import ledger
import test_engine_ledger_initialize as permanent_tests

ROOT = Path(__file__).resolve().parent
CANDIDATE = ROOT / 'candidate'
BASE = '32bb0f9dd9f024045d24487312b50f5b703573a3'
HEAD = '211a5a0f5891a44f217aa7aae18bdb8825775878'
EVIDENCE = 'fdc5d03bf58c9521ff06ab475aea01d1650a4269'
PREFIX = 'docs/independent-results/2026-10-02/vm-sqlite-candidate-codex/'

def git(*args):
    return subprocess.check_output(['git', '-C', str(CANDIDATE), *args])

def sha(data):
    return hashlib.sha256(data).hexdigest()

def emit(label, value):
    print(label, json.dumps(value, ensure_ascii=False, sort_keys=True))

assert git('rev-parse', 'HEAD').decode().strip() == HEAD
assert git('show', '-s', '--format=%T %P', HEAD).decode().strip() == (
    'e901b77ebf7551342ada996dd7ee4e8422b92f7a ' + BASE)
assert git('show', '-s', '--format=%P', EVIDENCE).decode().strip() == HEAD
assert git('diff', '--name-only', BASE, HEAD).decode().splitlines() == [
    'src/flowmarshal/engine/ledger.py', 'tests/test_engine_ledger_initialize.py']
assert all(p.startswith(PREFIX) for p in git('diff', '--name-only', HEAD, EVIDENCE).decode().splitlines())
assert git('diff', HEAD, EVIDENCE, '--', 'src', 'tests') == b''
emit('commit_tree_parent', git('show', '-s', '--format=%H %T %P', HEAD).decode().strip())
emit('evidence_commit_tree_parent', git('show', '-s', '--format=%H %T %P', EVIDENCE).decode().strip())

diff = git('diff', '--full-index', '--binary', BASE, HEAD)
published_diff = git('show', EVIDENCE + ':' + PREFIX + 'candidate.diff.patch')
assert diff == published_diff
(ROOT / 'candidate.diff.patch').write_bytes(diff)
emit('exact_diff_sha256', sha(diff))
manifest = json.loads(git('show', EVIDENCE + ':' + PREFIX + 'publication-manifest.json'))
assert manifest['source_commit'] == HEAD
assert manifest['source_tree'] == 'e901b77ebf7551342ada996dd7ee4e8422b92f7a'
assert manifest['source_parent'] == BASE
assert manifest['evidence_parent'] == HEAD
immutable = {}
for entry in manifest['immutable_original_files']:
    blob = git('show', EVIDENCE + ':' + PREFIX + entry['published'])
    assert sha(blob) == entry['sha256'], entry['published']
    immutable[entry['published']] = sha(blob)
emit('publication_immutable_hashes_match', immutable)
with tempfile.TemporaryDirectory() as temporary:
    temp = Path(temporary)
    target = temp / 'src/flowmarshal/engine/ledger.py'
    target.parent.mkdir(parents=True)
    target.write_bytes(git('show', BASE + ':src/flowmarshal/engine/ledger.py'))
    patch_path = temp / 'reviewed-v3.patch'
    patch_path.write_bytes(git('show', EVIDENCE + ':' + PREFIX + 'reviewed-v3.patch'))
    result = subprocess.run(['git', 'apply', str(patch_path)], cwd=temp, capture_output=True)
    emit('reviewed_patch_disposable_apply', {'argv': ['git', 'apply', str(patch_path)], 'exit_code': result.returncode, 'stdout': result.stdout.decode(), 'stderr': result.stderr.decode()})
    assert result.returncode == 0
    assert target.read_bytes() == git('show', HEAD + ':src/flowmarshal/engine/ledger.py')
emit('reviewed_patch_reconstructs_exact_candidate_ledger', True)
checksums = git('show', EVIDENCE + ':' + PREFIX + 'SHA256SUMS').decode().splitlines()
for line in checksums:
    if line.strip():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip('*')
        assert sha(git('show', EVIDENCE + ':' + PREFIX + name)) == digest, name
emit('evidence_SHA256SUMS_entries_verified', len(checksums))

for ref in (BASE, HEAD):
    data = git('show', ref + ':src/flowmarshal/engine/ledger.py')
    emit('ledger_blob', {'ref': ref, 'git_blob': git('rev-parse', ref + ':src/flowmarshal/engine/ledger.py').decode().strip(), 'sha256': sha(data)})
emit('permanent_test_blob', {'git_blob': git('rev-parse', HEAD + ':tests/test_engine_ledger_initialize.py').decode().strip(), 'sha256': sha((CANDIDATE / 'tests/test_engine_ledger_initialize.py').read_bytes())})
assert Path(permanent_tests.__file__).resolve() == CANDIDATE / 'tests/test_engine_ledger_initialize.py'
assert permanent_tests.SQLiteEngineLedger is ledger.SQLiteEngineLedger
assert Path(permanent_tests.__file__).read_bytes() == git('show', HEAD + ':tests/test_engine_ledger_initialize.py')
emit('permanent_test_import_and_class_binding', {'test_module_path': permanent_tests.__file__, 'candidate_ledger_class_binding': True})
imports = {}
for name, module in sorted(sys.modules.items()):
    if name == 'flowmarshal' or name.startswith('flowmarshal.'):
        source = Path(module.__file__).resolve()
        relative = source.relative_to(CANDIDATE).as_posix()
        assert source.read_bytes() == git('show', HEAD + ':' + relative), name
        imports[name] = {'path': str(source), 'sha256': sha(source.read_bytes())}
emit('all_loaded_flowmarshal_imports_match_candidate', imports)

def schema_from(ref):
    tree = ast.parse(git('show', ref + ':src/flowmarshal/engine/ledger.py'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_SQL' for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError('schema missing')

base_schema = schema_from(BASE)
assert base_schema == ledger.SCHEMA_SQL == schema_from(HEAD)
emit('unchanged_SCHEMA_SQL_sha256', sha(base_schema.encode()))
for path in ('src/flowmarshal/engine/ledger.py', 'tests/test_engine_ledger_initialize.py'):
    ast.parse((CANDIDATE / path).read_text(), feature_version=(3, 10))
emit('python_310_AST_only', 'PASS; runtime NOT_RUN')
emit('runtime', {'python_executable': sys.executable, 'python': sys.version, 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version, 'pydantic': pydantic.__version__, 'declared_python': '>=3.10', 'declared_pydantic': '2.13.5'})
print('SOURCE_VERIFICATION_PASS')
