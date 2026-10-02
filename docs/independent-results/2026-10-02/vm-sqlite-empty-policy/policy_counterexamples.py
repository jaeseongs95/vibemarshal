"""정책 반례 검증. DB와 미적용 v3 source copy는 TemporaryDirectory에서만 생성한다."""
import hashlib, io, json, os, sqlite3, subprocess, sys, tarfile, tempfile
from pathlib import Path
from unittest.mock import patch

BASE = "32bb0f9dd9f024045d24487312b50f5b703573a3"
EVIDENCE = "8a159ed4b79058aba8e6302358733696b33d11ff"
PATCH_PATH = "docs/independent-results/2026-10-02/vm-sqlite/revision3/atomic-initialize-v3.patch"
PATCH_SHA = "40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89"
REPO = Path(sys.argv[1]).resolve()
def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args])
def state(path):
    with sqlite3.connect(path) as c:
        return {"size": path.stat().st_size,
                "application_id": c.execute("PRAGMA application_id").fetchone()[0],
                "user_version": c.execute("PRAGMA user_version").fetchone()[0],
                "objects": c.execute("SELECT type,name FROM sqlite_master ORDER BY type,name").fetchall(),
                "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0],
                "integrity": c.execute("PRAGMA integrity_check").fetchone()[0]}
def eligible(s):
    return s["application_id"] == s["user_version"] == 0 and not s["objects"]
results = []
with tempfile.TemporaryDirectory(prefix="sqlite-policy-probe-") as td:
    root = Path(td); source = root / "candidate"; source.mkdir()
    archive = git("archive", BASE, "src")
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(source, filter="data")
    proposal = git("show", EVIDENCE + ":" + PATCH_PATH)
    assert hashlib.sha256(proposal).hexdigest() == PATCH_SHA
    subprocess.run(["git", "apply", "--whitespace=error-all", "-"], cwd=source, input=proposal, check=True)
    sys.path.insert(0, str(source / "src"))
    from flowmarshal.engine.ledger import SQLiteEngineLedger, EngineLedgerError
    for name, setup, accept in [
        ("external-vacuum", ["VACUUM"], True),
        ("external-wal-header", ["PRAGMA journal_mode=WAL"], True),
        ("foreign-plain-table", ["CREATE TABLE foreign_data(x)", "INSERT INTO foreign_data VALUES (7)"], False),
        ("foreign-view-only", ["CREATE VIEW foreign_view AS SELECT 7 AS x"], False),
        ("foreign-sequence-after-drop", ["CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)", "DROP TABLE t"], False),
        ("foreign-application-id-only", ["PRAGMA application_id=12345"], False),
        ("historical-version-only", ["PRAGMA user_version=3"], False),
        ("dropped-data-with-freelist", ["CREATE TABLE old_data(x)", "INSERT INTO old_data VALUES ('historic')", "DROP TABLE old_data"], True),
    ]:
        path = root / (name + ".db")
        with sqlite3.connect(path) as c:
            for q in setup: c.execute(q)
        before = state(path); digest = hashlib.sha256(path.read_bytes()).hexdigest()
        ledger = SQLiteEngineLedger(path, artifact_root=root/(name+"-artifacts"))
        rejected = False
        try: ledger.initialize()
        except EngineLedgerError: rejected = True
        assert rejected != accept
        if rejected:
            assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
            assert before == state(path)
        else: ledger._assert_identity()
        results.append({"case": name, "before": before, "accepted": not rejected,
                        "rejected_bytes_preserved": rejected})
    for mode in ("delete", "wal"):
        name = mode + "-bootstrap-rollback"; path = root/(name+".db")
        if mode == "wal":
            with sqlite3.connect(path) as c: c.execute("PRAGMA journal_mode=WAL")
        ledger = SQLiteEngineLedger(path, artifact_root=root/(name+"-artifacts"))
        original = ledger._connect
        class FailAfterMetadata:
            def __init__(self, c): self.c = c
            def __getattr__(self, n): return getattr(self.c, n)
            def executemany(self, *args, **kw):
                self.c.executemany(*args, **kw)
                raise RuntimeError("disposable metadata fault")
        with patch.object(ledger, "_connect", lambda **kw: FailAfterMetadata(original(**kw))):
            try: ledger.initialize()
            except RuntimeError: pass
            else: raise AssertionError("fault not reached")
        before = state(path); assert eligible(before)
        ledger.initialize(); ledger._assert_identity()
        results.append({"case": name, "before": before, "retry_success": True,
                        "old_size_predicate_would_accept": before["size"] == 0})
    # 실제 subprocess 종료 한 번씩; power-loss/benchmark를 뜻하지 않는다.
    for mode in ("delete", "wal"):
        path = root/("crash-"+mode+".db")
        if mode == "wal":
            with sqlite3.connect(path) as c: c.execute("PRAGMA journal_mode=WAL")
        code = """import os,sys
from pathlib import Path
from flowmarshal.engine.ledger import SQLiteEngineLedger
ledger=SQLiteEngineLedger(Path(sys.argv[1]))
original=ledger._connect
class Crash:
 def __init__(self,c):self.c=c
 def __getattr__(self,n):return getattr(self.c,n)
 def executemany(self,*args,**kw):
  self.c.executemany(*args,**kw)
  os._exit(73)
ledger._connect=lambda **kw:Crash(original(**kw))
ledger.initialize()
"""
        env = dict(os.environ, PYTHONPATH=str(source/"src"), PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", code, str(path)], env=env)
        assert proc.returncode == 73
        before = state(path); assert eligible(before)
        ledger = SQLiteEngineLedger(path, artifact_root=root/("crash-"+mode+"-artifacts"))
        ledger.initialize(); ledger._assert_identity()
        results.append({"case":"metadata-crash-"+mode, "before":before, "retry_success":True,
                        "old_size_predicate_would_accept":before["size"]==0})
print(json.dumps({"python":sys.version.split()[0],"sqlite":sqlite3.sqlite_version,
                  "base":BASE,"patch_sha256":PATCH_SHA,"cases":results,
                  "case_count":len(results),"status":"PASS",
                  "limits":["synthetic only","2 process exits; no power-loss durability proof",
                            "no race benchmark","empty schema does not prove no historical bytes"]},
                 ensure_ascii=False,indent=2))
