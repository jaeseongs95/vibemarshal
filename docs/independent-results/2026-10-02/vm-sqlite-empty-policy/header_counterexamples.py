"""원 header 관측의 portable replay utility. 이 게시용 script는 NOT_RUN; qualification이 아니다."""
import hashlib,io,json,sqlite3,subprocess,sys,tarfile,tempfile
from pathlib import Path

BASE="32bb0f9dd9f024045d24487312b50f5b703573a3"
EVIDENCE="8a159ed4b79058aba8e6302358733696b33d11ff"
PATCH_SHA="40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89"
repo=Path(sys.argv[1]).resolve()
def git(*args):return subprocess.check_output(["git","-C",str(repo),*args])
def observe(path):
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as c:
        result={q:c.execute("PRAGMA "+q).fetchone()[0]
                for q in ("application_id","user_version","schema_version","freelist_count")}
        result["objects"]=c.execute("SELECT type,name FROM sqlite_master").fetchall()
    result["main_bytes"]=path.stat().st_size
    result["main_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result
with tempfile.TemporaryDirectory(prefix="sqlite-header-replay-") as td:
    root=Path(td);source=root/"candidate";source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(git("archive",BASE,"src"))) as t:
        t.extractall(source,filter="data")
    proposal=git("show",EVIDENCE+":docs/independent-results/2026-10-02/vm-sqlite/revision3/atomic-initialize-v3.patch")
    assert hashlib.sha256(proposal).hexdigest()==PATCH_SHA
    subprocess.run(["git","apply","--whitespace=error-all","-"],cwd=source,input=proposal,check=True)
    sys.path.insert(0,str(source/"src"))
    from flowmarshal.engine.ledger import SQLiteEngineLedger
    paths={}
    for label in ("external-pristine-wal","pristine-wal-rollback"):
        p=root/(label+".db");paths[label]=p
        with sqlite3.connect(p) as c:c.execute("PRAGMA journal_mode=WAL")
    ledger=SQLiteEngineLedger(paths["pristine-wal-rollback"],artifact_root=root/"artifacts")
    original=ledger._connect
    def deny_projects(**kw):
        c=original(**kw)
        c.set_authorizer(lambda action,name,*_:sqlite3.SQLITE_DENY
                         if action==sqlite3.SQLITE_CREATE_TABLE and name=="projects"
                         else sqlite3.SQLITE_OK)
        return c
    ledger._connect=deny_projects
    try:ledger.initialize()
    except sqlite3.DatabaseError:pass
    else:raise AssertionError("DDL denial not reached")
    observations=[{"case":label,"state":observe(p)} for label,p in paths.items()]
    assert paths["external-pristine-wal"].read_bytes()==paths["pristine-wal-rollback"].read_bytes()
    for row in observations:
        s=row["state"]
        assert s["application_id"]==s["user_version"]==s["schema_version"]==s["freelist_count"]==0
        assert s["objects"]==[]
    print(json.dumps({"observations":observations,"limits":"synthetic source replay; no narrower candidate qualification"},indent=2))
