import ast
import hashlib
import json
import pathlib
import platform
import sqlite3
import subprocess
import sys

base = '32bb0f9dd9f024045d24487312b50f5b703573a3'
root = pathlib.Path.cwd()
source_path = 'src/flowmarshal/engine/ledger.py'
test_path = 'tests/test_engine_ledger_initialize.py'
original = subprocess.check_output(['git', 'show', base + ':' + source_path])
patched = (root / source_path).read_bytes()
tests = (root / test_path).read_bytes()
assert hashlib.sha256(original).hexdigest() == 'cecb5d6b50e736933005c6c849d6018992434ccdc8a9b13cb012f8e5fc5c0d41'
assert hashlib.sha256(patched).hexdigest() == '7c1b089720f5b270bd192902aea7db7f793f7903859f11a20e5c55cccb216fc2'
patch = pathlib.Path('/workspace/sqlite-candidate-evidence/atomic-initialize-v3.patch').read_bytes()
assert hashlib.sha256(patch).hexdigest() == '40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89'
def schema(data):
    tree = ast.parse(data, feature_version=(3, 10))
    return next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'SCHEMA_SQL' for target in node.targets))
assert schema(original) == schema(patched)
ast.parse(tests, feature_version=(3, 10))
product_paths = subprocess.check_output(['git', 'diff', '--name-only', base, '--', 'src']).decode().splitlines()
assert product_paths == [source_path], product_paths
print(json.dumps(dict(platform=platform.platform(), python=sys.version, executable=sys.executable,
                     sqlite=sqlite3.sqlite_version, schema_sql_sha256=hashlib.sha256(schema(patched).encode()).hexdigest(),
                     source_sha256=hashlib.sha256(patched).hexdigest(), test_sha256=hashlib.sha256(tests).hexdigest(),
                     python_310_ast='PASS; runtime NOT_RUN', product_paths=product_paths), indent=2))
