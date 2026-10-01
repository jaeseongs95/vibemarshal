# SQLite 초기화 제안 독립 최종 검토

## 최종 판정

**revision 3의 검토 대상 초기화 경계에 남은 차단 finding은 없다.** 원자적 bootstrap, lock 안에서의 identity 재검증, 외부 DB 보존, 연결 설정 실패 cleanup을 독립 재현으로 확인했다.

이 판정은 **정확히 아래 patch의 기술 검토 수용**이다. canonical source 적용, main merge, 운영 DB 변경, 제품 source GO, Windows qualification 또는 release GO를 승인하지 않는다. 결과 게시 시 patch는 미적용 제안으로 구분한다.

| 항목 | identity |
|---|---|
| 기준 commit | `32bb0f9dd9f024045d24487312b50f5b703573a3` |
| 기준 tree | `27e18c0c7853c1d35a98d5437cebf0ba703ed42d` |
| 최종 patch | `revision3/artifacts/atomic-initialize-v3.patch` |
| 최종 patch SHA-256 | `40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89` |
| 원본 ledger SHA-256 | `cecb5d6b50e736933005c6c849d6018992434ccdc8a9b13cb012f8e5fc5c0d41` |
| 최종 ledger SHA-256 | `7c1b089720f5b270bd192902aea7db7f793f7903859f11a20e5c55cccb216fc2` |
| 원본 ledger Git blob | `fe82d17527a0dfa6ce4a37f13f0a7e28a23fd6ab` |
| 실행 환경 | locked `vm-test-venv`, Python 3.12.14, SQLite 3.53.1, Linux/POSIX |

리뷰어는 구현자와 별도로 원본/패치/계약/테스트를 읽고 자체 synthetic regression을 작성했다. 원본과 구현자의 제안 소스는 수정하지 않았다. 모든 DB는 disposable synthetic fixture이며, 운영 DB/WAL·사용자 원장·provider 효과·원격 쓰기·migration은 없다.

## finding과 수정 이력

### R1: 빈 DB 확인과 쓰기 lock 사이의 외부 DB 채택

revision 1 SHA-256 `15c25624117c3d6737c919f7979fd623e709be54901087d50e5ea487f42aefa9`는 source 통합 부적합이었다. readonly probe 뒤 writer lock 전에 별도 process가 foreign table/행과 identity `12345/9`를 commit하면 initialize가 성공하며 foreign identity를 `1179469105/4`로 덮어썼다. 행은 남아도 Engine table 38개가 추가되어 외부 DB를 채택했다.

revision 3은 `BEGIN IMMEDIATE` 이후 같은 connection에서 emptiness/identity를 읽는다. foreign DB의 경우 `EngineLedgerError`로 거절하며 byte-for-byte main DB, foreign 행, application_id, user_version 및 `DELETE` journal mode가 그대로다. nonexistent 경로가 foreign DB로 바뀌는 경우와 이미 존재하는 빈 DB가 바뀌는 경우 모두 통과했다. WAL 설정은 수용된 DB의 transaction commit 뒤에만 한다.

### R2: rollback 오류가 초기화의 첫 오류를 가림

revision 1에서 metadata 오류 뒤 rollback fault를 주입하면 첫 오류 대신 rollback 오류가 최상위로 전달됐다. Python exception context에는 첫 오류가 남았으며, 실제 디스크 오류를 만든 시험은 아니다.

revision 3은 첫 오류를 보존하고 cleanup을 수행한다. 실제 연결이 닫혔고 재시도가 성공하는 것도 확인했다. cleanup 자체가 실패하면 정상 rollback을 보장하는 것은 아니며, 최상위 원인 보존을 확인한 것이다.

### R3: malformed 기존 파일의 초기 연결 설정 실패

revision 2 SHA-256 `47701107ba6f78135badc01da6353b62c2ee5429777ac4844a7c5b447e117a7c`는 R1/R2를 고쳤지만, malformed synthetic SQLite 파일의 `PRAGMA synchronous=FULL` 실패가 initialize의 try/finally 전에 발생해 acquired connection을 명시적으로 닫지 않았다. 원본은 같은 파일에서 close를 호출했다. 데이터 변경은 관측되지 않았다.

revision 3은 connection 획득 이후 PRAGMA/row factory 설정 전체를 cleanup 경계로 감싼다. malformed 파일 byte 보존, writable/readonly 설정 실패 시 close, cleanup 오류가 원 설정 오류를 가리지 않음을 확인했다.

## 독립 실행 결과

| suite | revision 1 | revision 2 | revision 3 |
|---|---:|---:|---:|
| 리뷰어 core regression 7개 | 5 PASS / 1 FAIL / 1 ERROR | 7 PASS | 7 PASS |
| 리뷰어 lifecycle 5개 | 미실행 | 4 PASS / 1 FAIL | 5 PASS |
| 리뷰어 동시 최초 초기화 | 미실행 | 20회 × 4 process 성공 | 20회 × 4 process 성공 |
| 구현자 atomic 8개 독립 재실행 | 8 PASS | 8 PASS | 8 PASS |
| 구현자 SQLite boundary 26개 독립 재실행 | 26 PASS | 26 PASS | 26 PASS |
| 구현자 identity 재검증 4개 독립 재실행 | 미실행 | 미실행 | 4 PASS |
| 구현자 setup cleanup 3개 독립 재실행 | 미실행 | 미실행 | 3 PASS |
| 저장소 25개 회귀 독립 재실행 | 22 PASS / 3 ERROR | 미실행 | 22 PASS / 3 ERROR |

원본은 atomic suite에서 **2 PASS / 5 FAIL / 1 ERROR**, 리뷰어 core suite에서 **4 PASS / 3 FAIL**, 저장소 회귀에서 **22 PASS / 3 ERROR**였다. before/after 및 반복 실행을 신규 test 수로 합산하지 않았다. 동시 초기화 20회는 한 시나리오의 반복이며 80개의 unique test가 아니다. 서로 겹치는 suite의 PASS를 더해 제품 qualification 수치로 만들지 않는다.

저장소의 3 ERROR는 양쪽에서 동일한 `attempt_id=None` 후속 역참조다. `docs/redesign-1.0-contract.md` V03과 기존 진단 로그의 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`에 부합한다. Windows-only owner lock 제한을 우회하지 않았고, 25 PASS 또는 Windows 검증 완료로 기록하지 않는다.

추가 확인:

- schema DDL, metadata, 두 identity PRAGMA가 하나의 transaction 안에 있다. `executescript`의 implicit precommit을 피하며 trigger를 포함한 완전한 statement를 실행한다
- 모든 `sqlite_master(type,name,tbl_name,sql)` 행이 원본 SCHEMA_SQL을 원본 방식으로 실행한 결과와 정확히 동일하다. integrity/foreign key 검사도 통과했다
- DDL 직후 또는 application_id 직후 process 강제 종료 시 빈 schema/zero identity로 복구되고 재시도된다
- commit 직전 다른 connection에서는 partial schema/identity가 보이지 않는다
- 기존 정상 Engine DB의 schema dump, project/history 데이터, identity와 main bytes는 반복 initialize 뒤 동일하다
- borrowed identity connection을 검사해도 호출자의 transaction과 connection 소유권이 유지된다
- commit 후 WAL 전환 오류는 완성된 DB를 남길 수 있으며 재시도하면 기존 DB를 보존하면서 정상 WAL 상태가 된다. 이를 rollback 성공으로 오표기하지 않는다
- patch apply/whitespace check exit 0. Python 3.10 AST parsing은 통과했지만 Python 3.10 runtime 검증은 아니다

## 수용 범위와 잔여 한계

1. **빈 DB 허용 범위가 확대된다.** nonexistent/zero-byte뿐 아니라 application_id=0, user_version=0, sqlite_master가 완전히 빈 SQLite DB도 새 대상으로 허용한다. 실패 bootstrap과 다른 출처의 빈 SQLite를 출처상 구분하지 못한다. 이 정책을 명시적으로 수용한 초기화 대상에만 제안을 적용해야 한다. 비어 있지 않은 외부 DB/비zero identity DB는 거절된다.
2. DB schema commit과 WAL 설정·artifact directory 생성은 하나의 filesystem transaction이 아니다. 후속 오류가 발생해도 유효한 DB가 이미 commit됐을 수 있다. 원본 데이터 파괴 없이 재시도되는 해당 경로를 확인했다.
3. `complete_statement`는 현재 정적 SCHEMA_SQL에 대해 검증했다. 미래 schema 편집에도 이 동등성/rollback 회귀를 유지해야 한다.
4. 보호된 rollback 및 connection 설정 cleanup 오류는 첫 오류를 덮지 않지만 현재 구현은 그 cleanup 오류 자체를 별도로 기록하지 않는다. initialize의 기존 finally `close()` 자체가 실패하는 모든 조합까지 원 예외 보존을 보장한 것은 아니다. 이 관측을 성공한 복구 보장으로 해석하지 않는다.
5. Windows native owner lock, 모든 지원 Python/SQLite 조합, 실제 power-loss durability, 전체 설치/역할/실제 요청 E2E/release gate는 미실행이다. history malformed JSON 동작 등 별도 문제도 이 패치의 수정 범위가 아니다.

## 재현

작업 root에서 실행하며 아래 모든 DB 경로는 테스트 코드가 만든 synthetic temporary directory다. `VM_SOURCE`는 제품 소스 위치일 뿐 DB 입력 경로가 아니다.

```sh
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite-review/review_initialize_v2.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" VM_BASELINE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite-review/review_initialization_lifecycle.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite-review/review_concurrent_initialize.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_atomic_initialize_proposal.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_sqlite_boundaries.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/revision2/test_initialize_revalidation.py
VM_SOURCE="$PWD/vm-sqlite/proposal-v3" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/revision3/test_connection_setup_cleanup.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/vm-sqlite/proposal-v3/src:$PWD/vm-portable" vm-test-venv/bin/python -m unittest tests.test_engine_ledger_snapshot tests.test_engine_ledger_service tests.test_engine_current_snapshot_goal_scope -v
git -C vm-portable apply --check --whitespace=error-all ../vm-sqlite/revision3/artifacts/atomic-initialize-v3.patch
```

예상 exit는 순서대로 `0, 0, 0, 0, 0, 0, 0, 1, 0`이다. `review_initialize_v2.py`라는 파일명은 v2부터 바뀐 private hook 인자를 수용한다는 뜻이며 실행 대상은 VM_SOURCE로 고정한다. 테스트 oracle은 foreign DB 보존·원자성·연결 lifecycle이다. 원본/rev1/rev2의 실패 evidence는 별도 보존한다.
