import ast
import hashlib
import json
import pathlib
import platform
import sqlite3
import subprocess
import sys

base = '32bb0f9dd9f024045d24487312b50f5b703573a3'
v3 = '211a5a0f5891a44f217aa7aae18bdb8825775878'
evidence = 'fdc5d03bf58c9521ff06ab475aea01d1650a4269'
source_path = 'src/flowmarshal/engine/ledger.py'
test_path = 'tests/test_engine_ledger_initialize.py'
def git(*args): return subprocess.check_output(['git', *args])
assert git('rev-parse', 'codex/sqlite-atomic-initialize-v3').decode().strip() == v3
assert git('rev-parse', 'codex/sqlite-atomic-initialize-v3-evidence').decode().strip() == evidence
assert git('rev-parse', v3 + '^{tree}').decode().strip() == 'e901b77ebf7551342ada996dd7ee4e8422b92f7a'
original = git('show', base + ':' + source_path)
previous = git('show', v3 + ':' + source_path)
source = pathlib.Path(source_path).read_bytes()
tests = pathlib.Path(test_path).read_bytes()
assert hashlib.sha256(previous).hexdigest() == '7c1b089720f5b270bd192902aea7db7f793f7903859f11a20e5c55cccb216fc2'
old_line = b'                and connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is None\n'
new_line = old_line + ('                # SQLite native recovery와 writer lock 뒤에도 0-byte인 대상만 bootstrap한다.\n'
                        '                and self.path.stat().st_size == 0\n').encode()
assert previous.count(old_line) == 1
assert source == previous.replace(old_line, new_line)
def schema(data):
    tree = ast.parse(data, feature_version=(3, 10))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'SCHEMA_SQL' for target in node.targets))
assert schema(original) == schema(previous) == schema(source)
ast.parse(tests, feature_version=(3, 10))
print(json.dumps(dict(platform=platform.platform(), python=sys.version, executable=sys.executable,
                     sqlite=sqlite3.sqlite_version, predicate_delta='one size-zero clause plus comment; exact bytes verified',
                     prior_v3_and_evidence_refs_preserved=True, schema_sql_unchanged=True,
                     schema_sql_sha256=hashlib.sha256(schema(source).encode()).hexdigest(),
                     source_sha256=hashlib.sha256(source).hexdigest(), tests_sha256=hashlib.sha256(tests).hexdigest(),
                     python_310_ast='PASS; runtime NOT_RUN'), ensure_ascii=False, indent=2))
