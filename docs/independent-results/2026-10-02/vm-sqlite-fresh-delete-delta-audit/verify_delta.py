"""동결 v3를 바꾸지 않고 fresh DELETE candidate의 source/import/diff를 검증한다."""
import ast
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import pydantic
from flowmarshal.engine import ledger
import test_engine_ledger_initialize as tests

ROOT = Path(__file__).resolve().parent
CWD = ROOT / 'candidate'
BASE = '32bb0f9dd9f024045d24487312b50f5b703573a3'
V3 = '211a5a0f5891a44f217aa7aae18bdb8825775878'
HEAD = '4dae937be993693b78d1dbcd873d499633a08e97'
EVIDENCE = 'c94c7552b4622056cf31c674c9f83058a674cc05'
PREFIX = 'docs/independent-results/2026-10-02/vm-sqlite-fresh-delete-candidate/'

def git(*args):
    return subprocess.check_output(['git', '-C', str(CWD), *args])

def digest(data):
    return hashlib.sha256(data).hexdigest()

def emit(label, value):
    print(label, json.dumps(value, ensure_ascii=False, sort_keys=True))

assert git('rev-parse', 'HEAD').decode().strip() == HEAD
assert git('show', '-s', '--format=%T %P', HEAD).decode().strip() == '8c03f489783175f579c86b699a7b98d634217d19 ' + BASE
assert git('show', '-s', '--format=%P', EVIDENCE).decode().strip() == HEAD
assert git('diff', '--name-only', BASE, HEAD).decode().splitlines() == ['src/flowmarshal/engine/ledger.py', 'tests/test_engine_ledger_initialize.py']
assert git('diff', HEAD, EVIDENCE, '--', 'src', 'tests') == b''
assert git('show', HEAD + ':AGENTS.md') == git('show', V3 + ':AGENTS.md')
emit('candidate_shape', git('show', '-s', '--format=%H %T %P', HEAD).decode().strip())
emit('evidence_shape', git('show', '-s', '--format=%H %T %P', EVIDENCE).decode().strip())
for base, name in [(BASE, 'cumulative-from-32bb.patch'), (V3, 'predicate-and-tests-from-v3.patch')]:
    actual = git('diff', '--full-index', '--binary', base, HEAD)
    expected = git('show', EVIDENCE + ':' + PREFIX + name)
    assert actual == expected
    (ROOT / name).write_bytes(actual)
    emit('exact_patch', {'file': name, 'bytes': len(actual), 'sha256': digest(actual)})
publication = json.loads(git('show', EVIDENCE + ':' + PREFIX + 'publication-manifest.json'))
for entry in publication['original_files']:
    assert digest(git('show', EVIDENCE + ':' + PREFIX + entry['published'])) == entry['sha256']
lines = git('show', EVIDENCE + ':' + PREFIX + 'SHA256SUMS').decode().splitlines()
for line in lines:
    hash_value, name = line.split(maxsplit=1)
    assert digest(git('show', EVIDENCE + ':' + PREFIX + name.lstrip('*'))) == hash_value
emit('author_evidence_hashes_verified', {'original_files': len(publication['original_files']), 'checksum_entries': len(lines)})

old = git('show', V3 + ':src/flowmarshal/engine/ledger.py')
new = git('show', HEAD + ':src/flowmarshal/engine/ledger.py')
needle = b'                and connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None\n'
assert old.count(needle) == 1
assert old.replace(needle, needle + '                # SQLite native recovery와 writer lock 뒤에도 0-byte인 대상만 bootstrap한다.\n'.encode() + b'                and self.path.stat().st_size == 0\n') == new
emit('only_implementation_delta', 'one size predicate clause and one comment; all other implementation bytes match v3')
for name in ['src/flowmarshal/engine/ledger.py', 'tests/test_engine_ledger_initialize.py']:
    content = git('show', HEAD + ':' + name)
    assert (CWD / name).read_bytes() == content
    ast.parse(content, feature_version=(3, 10))
    emit('candidate_file', {'path': name, 'sha256': digest(content), 'git_blob': git('rev-parse', HEAD + ':' + name).decode().strip(), 'python310_AST': 'PASS_SYNTAX_ONLY'})

def schema(content):
    return next(ast.literal_eval(node.value) for node in ast.parse(content).body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SCHEMA_SQL' for t in node.targets))
assert schema(old) == schema(new) == schema(git('show', BASE + ':src/flowmarshal/engine/ledger.py')) == ledger.SCHEMA_SQL
emit('static_SCHEMA_SQL_unchanged', {'sha256': digest(ledger.SCHEMA_SQL.encode()), 'full_schema_execution_not_repeated': True})

def methods(ref):
    tree = ast.parse(git('show', ref + ':tests/test_engine_ledger_initialize.py'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'EngineLedgerInitializeTests')
    return {n.name: ast.dump(n) for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')}
prior = methods(V3)
current = methods(HEAD)
assert len(prior) == 11 and len(current) == 16
assert all(prior[name] == current[name] for name in prior)
added = sorted(set(current) - set(prior))
assert len(added) == 5
emit('permanent_method_inventory', {'existing': 11, 'new': 5, 'total': 16, 'prior_method_AST_unchanged': True, 'new_names': added, 'independent_executed_methods': 8})
imports = {}
assert Path(tests.__file__).resolve() == CWD / 'tests/test_engine_ledger_initialize.py'
assert tests.SQLiteEngineLedger is ledger.SQLiteEngineLedger
for name, module in sorted(sys.modules.items()):
    if name == 'flowmarshal' or name.startswith('flowmarshal.'):
        source = Path(module.__file__).resolve()
        relative = source.relative_to(CWD).as_posix()
        assert source.read_bytes() == git('show', HEAD + ':' + relative)
        imports[name] = {'path': str(source), 'sha256': digest(source.read_bytes())}
emit('candidate_imports_exact', imports)
emit('permanent_module_binding', {'path': tests.__file__, 'same_candidate_ledger_class': True})
emit('runtime', {'python': sys.version, 'python_executable': sys.executable, 'sqlite': sqlite3.sqlite_version, 'platform': platform.platform(), 'pydantic': pydantic.__version__, 'Python310_runtime': 'NOT_RUN', 'Windows_runtime': 'NOT_RUN'})
print('DELTA_SOURCE_VERIFICATION_PASS')
