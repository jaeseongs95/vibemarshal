"""소수 edge probe: 실제 v3 source copy, hot journal와 committed WAL. 합성 DB만 사용."""
import hashlib,io,json,os,sqlite3,subprocess,sys,tarfile,tempfile
from pathlib import Path
BASE="32bb0f9dd9f024045d24487312b50f5b703573a3"
EVIDENCE="8a159ed4b79058aba8e6302358733696b33d11ff"
REPO=Path(sys.argv[1]).resolve()
def git(*args):return subprocess.check_output(["git","-C",str(REPO),*args])
results={}
with tempfile.TemporaryDirectory(prefix="sqlite-policy-edge-") as td:
 root=Path(td);source=root/"candidate";source.mkdir()
 with tarfile.open(fileobj=io.BytesIO(git("archive",BASE,"src"))) as t:t.extractall(source,filter="data")
 proposal=git("show",EVIDENCE+":docs/independent-results/2026-10-02/vm-sqlite/revision3/atomic-initialize-v3.patch")
 assert hashlib.sha256(proposal).hexdigest()=="40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89"
 subprocess.run(["git","apply","--whitespace=error-all","-"],cwd=source,input=proposal,check=True)
 env=dict(os.environ,PYTHONPATH=str(source/"src"),PYTHONDONTWRITEBYTECODE="1")
 path=root/"spill.db"
 child="""import os,sys
from pathlib import Path
from flowmarshal.engine.ledger import SQLiteEngineLedger
l=SQLiteEngineLedger(Path(sys.argv[1]));original=l._connect
def connect(**kw):
 c=original(**kw);c.execute('PRAGMA cache_size=10')
 c.set_trace_callback(lambda q:os._exit(75) if q=='COMMIT' else None)
 return c
l._connect=connect
l.initialize()
"""
 proc=subprocess.run([sys.executable,"-c",child,str(path)],env=env);assert proc.returncode==75
 before={"main_bytes":path.stat().st_size,"journal_bytes":Path(str(path)+"-journal").stat().st_size}
 baseline="""import json,sys
from pathlib import Path
from flowmarshal.engine.ledger import SQLiteEngineLedger
try:SQLiteEngineLedger(Path(sys.argv[1])).initialize()
except Exception as e:print(json.dumps({'type':type(e).__name__,'message':str(e)}))
else:print(json.dumps({'status':'success'}))
"""
 base_env=dict(os.environ,PYTHONPATH=str(REPO/"src"),PYTHONDONTWRITEBYTECODE="1")
 base_result=json.loads(subprocess.check_output([sys.executable,"-c",baseline,str(path)],env=base_env))
 assert base_result["type"]=="OperationalError"
 with sqlite3.connect(path) as c:
  c.execute("BEGIN IMMEDIATE")
  after={"main_bytes":path.stat().st_size}
  for pragma in ("application_id","user_version","schema_version","freelist_count"):
   after[pragma]=c.execute("PRAGMA "+pragma).fetchone()[0]
  after["objects"]=c.execute("SELECT type,name FROM sqlite_master").fetchall()
  c.rollback()
 assert after["main_bytes"]==after["application_id"]==after["user_version"]==after["schema_version"]==after["freelist_count"]==0 and not after["objects"]
 results["fresh-delete-spill"]={"process_exit":75,"before_recovery":before,"baseline_readonly_precheck":base_result,"writer_locked_recovered_state":after}
 sys.path.insert(0,str(source/"src"))
 from flowmarshal.engine.ledger import SQLiteEngineLedger,EngineLedgerError
 p=root/"foreign-wal.db";c=sqlite3.connect(p)
 c.execute("PRAGMA journal_mode=WAL");c.execute("PRAGMA wal_autocheckpoint=0")
 c.execute("CREATE TABLE foreign_committed(x)")
 c.execute("INSERT INTO foreign_committed VALUES (7)");c.commit()
 before_bytes=[p.read_bytes(),Path(str(p)+"-wal").read_bytes()]
 rejected=False
 try:SQLiteEngineLedger(p,artifact_root=root/"foreign-artifacts").initialize()
 except EngineLedgerError:rejected=True
 assert rejected
 assert before_bytes==[p.read_bytes(),Path(str(p)+"-wal").read_bytes()]
 row=c.execute("SELECT x FROM foreign_committed").fetchone()[0];assert row==7
 results["foreign-committed-in-wal"]={"main_bytes":len(before_bytes[0]),"wal_bytes":len(before_bytes[1]),"rejected":True,"main_and_wal_bytes_preserved":True,"row_preserved":row}
 c.close()
print(json.dumps({"sqlite":sqlite3.sqlite_version,"python":sys.version.split()[0],"status":"PASS","cases":results,"limits":["Linux synthetic; no Windows or power loss proof","one spill process exit, no race benchmark","does not implement or qualify a narrower candidate"]},indent=2))
