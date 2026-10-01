# SQLite 초기화 제안 독립 검토: revision 1

## 판정

**소스 통합 보류.** 신규 초기화의 DDL/metadata/identity 원자성은 실제로 개선됐지만, 확대된 빈 SQLite 수용 경로에 외부 DB identity를 덮어쓰는 check/write 경합이 있다. 이 보고서와 패치는 진단·미채택 제안으로 보존할 수 있다. 소스 GO, main merge, 제품 release GO 또는 Windows qualification 승인이 아니다.

- 기준 candidate: `32bb0f9dd9f024045d24487312b50f5b703573a3`
- 기준 tree: `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`
- 검토 patch: `artifacts/atomic-initialize.patch`
- patch SHA-256: `15c25624117c3d6737c919f7979fd623e709be54901087d50e5ea487f42aefa9`
- 원본 ledger SHA-256: `cecb5d6b50e736933005c6c849d6018992434ccdc8a9b13cb012f8e5fc5c0d41`
- 제안 ledger SHA-256: `77faf9da412e62a550c537c5061474a74067bcbc977d32e0f73988b0543296fc`
- Python 3.12.14, SQLite 3.53.1, locked `vm-test-venv`, Linux/POSIX
- 리뷰어는 원본/제안 소스를 수정하지 않았다. 모든 DB는 독립 테스트가 만든 disposable synthetic fixture다. 운영 DB/WAL, 실제 사용자 데이터, migration, 원격 쓰기, provider 효과는 없다.

## 차단 finding R1: 외부 DB의 초기화 경합

대상: 제안 `src/flowmarshal/engine/ledger.py:797-811`의 빈 DB probe와 이후 `BEGIN IMMEDIATE` 사이.

새 probe는 별도 read-only connection으로 `application_id=0`, `user_version=0`, `sqlite_master`가 비었는지를 확인한다. 그 연결을 닫은 뒤 writer connection을 열고 schema script에서 쓰기 lock을 얻는다. 이 사이에 다른 정상 SQLite writer가 commit한 상태를 다시 검증하지 않는다.

독립 재현은 같은 합성 DB를 쓰는 **별도 process**를 정확히 이 구간에 배치했다. 공격성 같은-process 코드 격리를 요구하는 시험이 아니라 일반 SQLite writer 간 interleaving이다. process의 입력은 고정된 합성 DB 경로이며 source나 운영 DB는 열지 않는다.

관측 (`race-observation.json`):

1. 기존의 비zero-size 빈 SQLite DB를 만든다.
2. 제안의 빈 DB probe가 성공한다.
3. 다른 process가 `independent_app_data` table과 `preserve-me` 행을 만들고 `application_id=12345`, `user_version=9`를 commit한다.
4. Engine 초기화가 오류 없이 반환한다.
5. 외부 행은 남지만 Engine table 38개가 추가되고 `application_id=1179469105` (`FME1`), `user_version=4`로 바뀐다.

원본은 같은 이미 존재하는 빈 SQLite DB를 writer 연결 전에 거절한다. 따라서 이번 빈 DB 수용 확장에 대한 새 회귀다. 기존의 nonexistent/zero-byte 경로에도 비슷한 check/write 한계가 있지만 이 사실은 확장 경로의 identity 덮어쓰기를 안전하게 만들지 않는다. 외부 row 삭제는 관측되지 않았으며, 문제는 foreign DB의 identity/schema 무단 채택이다.

관련 계약: README의 schema 4 신규 DB/제자리 migration 금지, `docs/redesign-1.0-contract.md` D11, 제안 자체의 외부 DB 보호 약속.

최소 수정 방향:

- 같은 writer transaction에서 lock을 얻은 뒤 DB emptiness/identity를 다시 읽고, 그 관측을 기준으로 초기화/기존 원장 허용/외부 DB 거절을 결정한다
- DDL, schema metadata, application_id, user_version은 그 transaction 안에 둔다
- lock 전에 foreign DB의 journal_mode 같은 영구 설정을 바꾸지 않는다
- Python 기본/legacy transaction 모드에서 이미 열린 transaction 뒤 `executescript`를 호출하면 먼저 COMMIT하므로 단순히 BEGIN을 옮겨서는 안 된다
- package는 Python >=3.10이다. `sqlite3.complete_statement`로 정적 schema의 완전한 statement를 분리해 `execute`하거나 동등한 호환 방식으로 implicit precommit을 피한다
- 빈 DB 경합뿐 아니라 이미 성공한 Engine 초기화를 다른 initializer가 보는 경우도 같은 transaction에서 재검증한다

## 보강 finding R2: rollback 오류가 첫 오류를 가림

대상: 제안 `ledger.py:822-824`.

초기화 오류 뒤 `connection.rollback()`도 실패하면 최상위로 전달되는 예외가 첫 오류에서 rollback 오류로 바뀐다. 독립 fault injection에서는 `PrimaryFailure` 대신 `RollbackFailure`가 전달됐다. Python exception context에는 첫 오류가 남으므로 완전한 증거 소실이라고 표현하지 않는다. 실제 disk I/O 오류를 만든 시험은 아니며 진단 보강 finding이다.

최소 수정은 첫 예외를 보존한 채 rollback을 best-effort 수행하고 cleanup 오류를 note/chain 등으로 남기며 close를 보장하는 것이다. rollback 실패만으로 DB가 정상 복구됐다고 주장하면 안 된다.

## 독립 검증 결과

| 검증 | 원본 | revision 1 |
|---|---:|---:|
| 구현자 atomic 8개 재실행 | 2 PASS / 5 FAIL / 1 ERROR | 8 PASS |
| 독립 7개 regression | 4 PASS / 3 FAIL | 5 PASS / 1 FAIL / 1 ERROR |
| 구현자 boundary 26개 재실행 | 이번 리뷰에서는 재실행 안 함 | 26 PASS |
| 저장소 회귀 25개 재실행 | 22 PASS / 3 ERROR | 22 PASS / 3 ERROR |
| patch apply/whitespace check | exit 0 | 적용 안 함 |

독립 7개는 같은 suite의 before/after 비교이며 14개의 새 테스트로 세지 않는다. 구현자 8개와 26개 재실행도 신규 coverage 수에 다시 더하지 않는다.

저장소 3 ERROR는 두 버전에서 같은 테스트의 `attempt_id=None` 후속 역참조다. 기존 진단 로그와 `docs/redesign-1.0-contract.md` V03은 POSIX에서 `RUNTIME_OWNER_LOCK_UNAVAILABLE`로 차단하는 Windows-only 계약을 보여 준다. 따라서 25 PASS로 올리거나 Windows 검증 결과로 해석하지 않는다.

독립 PASS 근거:

- schema 실행 직후, metadata 이전, 두 identity PRAGMA 모두 같은 열린 transaction 안에 있다. trace의 transaction boundary는 `BEGIN IMMEDIATE;` 한 번과 마지막 `COMMIT` 한 번이다
- DDL 직후 process 강제 종료와 application_id 설정 직후 process 강제 종료 모두 빈 schema/zero identity로 복구되고 재시도된다
- 기존 정상 Engine DB의 schema dump, project/history 행, identity, main-file bytes가 반복 initialize 전후 동일하다
- 외부 view-only SQLite DB를 거절하고 main-file bytes를 보존한다
- 기존 구현자의 mid-DDL, metadata, identity, commit 전, KeyboardInterrupt fault는 제안에서 rollback/retry된다

한계:

- 성공한 commit 뒤 Python 예외가 생기는 경우까지 '예외가 나면 항상 미초기화'라고 보장하지 않는다
- 삭제된 과거 schema/빈 외부 SQLite와 실패한 bootstrap의 빈 DB를 현재 probe만으로 출처상 구별할 수 없다. 빈 SQLite를 신규 대상으로 허용할지는 명시적인 수용 범위 결정이다
- host `artifact_root.mkdir`은 DB commit 뒤 실행돼 파일시스템/DB 전체 원자성은 주장하지 않는다
- Windows native owner lock, 실제 파일시스템 power-loss, unsupported Python/SQLite 조합, 전체 release suite는 이 검토 범위가 아니다

## 재현 명령

작업 root에서 실행한다. 모든 DB fixture는 `vm-sqlite-review` 또는 구현자 테스트의 synthetic temporary directory에 생성 후 정리된다.

```sh
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_atomic_initialize_proposal.py
VM_SOURCE="$PWD/vm-sqlite/proposal" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_atomic_initialize_proposal.py
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite-review/review_initialize.py
VM_SOURCE="$PWD/vm-sqlite/proposal" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite-review/review_initialize.py
VM_SOURCE="$PWD/vm-sqlite/proposal" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_sqlite_boundaries.py
git -C vm-portable apply --check --whitespace=error-all ../vm-sqlite/artifacts/atomic-initialize.patch
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/vm-portable/src:$PWD/vm-portable" vm-test-venv/bin/python -m unittest tests.test_engine_ledger_snapshot tests.test_engine_ledger_service tests.test_engine_current_snapshot_goal_scope -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/vm-sqlite/proposal/src:$PWD/vm-portable" vm-test-venv/bin/python -m unittest tests.test_engine_ledger_snapshot tests.test_engine_ledger_service tests.test_engine_current_snapshot_goal_scope -v
```

예상 exit는 순서대로 `1, 0, 1, 1, 0, 0, 1, 1`이다. 독립 테스트의 실패는 finding을 고정한 결과이며 숨기거나 기대값을 낮춰 PASS로 바꾸지 않았다. 다음 제안 revision은 별도 patch identity와 before/after evidence로 다시 감사해야 한다.
