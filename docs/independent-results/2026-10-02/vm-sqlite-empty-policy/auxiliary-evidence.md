# Auxiliary counterexample reproduction

모든 DB는 TemporaryDirectory의 synthetic 파일이다. 제품 marker나 실제 원장 데이터가 아니다. 다음은 Coordinator가 직접 실행한 SQL 절차를 재현하는 standalone 코드다. 출력값만 보존하며 DB 파일을 게시하지 않는다.

```python
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
```

관측 environment: Python3.12.14/SQLite3.53.1. collision: 4096-byte 두 전체 main 파일 SHA256 `0ab48b25cba617ed3a4acca0161813b314c095ef63544bb4af60769eb1012977`. DROP: application_id0/user_version0/schema_version2/freelist1/objects[]/synthetic_marker_remains=true.

C7의 추가header 관측은 source-derived v3 DDL-denial experiment 뒤 두 synthetic pristine-WAL/rollback DB의 readonly 직접 조회로 확인했다. 다음 SQL 값이 양쪽 모두0이었다: `PRAGMA application_id`, `PRAGMA user_version`, `PRAGMA schema_version`, `PRAGMA freelist_count`; 전체 `sqlite_master` 행은 없었다. 이 관측은 narrowed predicate 후보의 coherence 근거이며 후보 구현/전체 qualification을 뜻하지 않는다. 해당 reviewer 실험에서는 source의 initialize/identity 클래스 subset을 추출해 host capability 검사를 synthetic harness로 대체했으므로 full product authority integration 시험으로 해석하지 않는다. Coordinator의 다른 policy/edge scripts는 실제 package import를 사용했다.
