"""게시용 재현 script. 원 SQL 절차는 과거 실행됐으며 이 파일은 게시 단계에서 실행하지 않았다."""
import hashlib, json, sqlite3, tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="sqlite-provenance-collision-") as td:
    paths = []
    for name, revert in [("external", False), ("rollback", True)]:
        p = Path(td) / (name + ".db")
        paths.append(p)
        with sqlite3.connect(p) as c:
            c.execute("PRAGMA journal_mode=WAL")
            if revert:
                c.execute("BEGIN IMMEDIATE")
                c.execute("CREATE TABLE boot(x)")
                c.execute("PRAGMA application_id=1179469105")
                c.execute("PRAGMA user_version=4")
                c.rollback()
    bodies = [p.read_bytes() for p in paths]
    assert bodies[0] == bodies[1]
    print(len(bodies[0]), hashlib.sha256(bodies[0]).hexdigest())

marker = b"synthetic-decision-review-dropped-history-534ea7"
with tempfile.TemporaryDirectory(prefix="sqlite-history-only-") as td:
    p = Path(td) / "history.db"
    c = sqlite3.connect(p)
    c.execute("PRAGMA secure_delete=OFF")
    c.execute("CREATE TABLE old_record(x)")
    c.execute("INSERT INTO old_record VALUES (?)", (marker.decode(),))
    c.commit()
    c.execute("DROP TABLE old_record")
    c.commit()
    state = {q: c.execute("PRAGMA " + q).fetchone()[0]
             for q in ("application_id", "user_version",
                       "schema_version", "freelist_count")}
    state["objects"] = c.execute("SELECT type,name FROM sqlite_master").fetchall()
    c.close()
    state["synthetic_marker_remains_in_bytes"] = marker in p.read_bytes()
    print(json.dumps(state))
