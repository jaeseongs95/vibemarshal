# VM SQLite 격리 교차검사 보고서

게시 상태: atomic-initialize.patch는 **NOT_APPLIED_TO_CANONICAL** 제안 diff다. 아래 원본 사실과 후속 proposal 결과를 구분한다. 경로는 execution-manifest의 문서화된 alias로 치환했다. 실제 원시 로그는 내부에 보존했고 게시 로그는 경로만 치환했다. shell command의 개별 시작·종료 시각/소요시간은 기록하지 않았으며, unittest가 출력한 suite 시간은 그대로 보존했다. Host clock은 KST(+0900), 이번 기록의 UTC 날짜는 2026-10-01이다.

## 결론

- 기준 commit: `32bb0f9dd9f024045d24487312b50f5b703573a3` (`jaeseongs95/vibemarshal`), tree `27e18c0c7853c1d35a98d5437cebf0ba703ed42d`
- 새 경계 검사 **26개 PASS**. 실제 `EngineService`를 import한 최종 실행이다. 초기 AST slice 진단은 이 결과로 대체하며 중복 집계하지 않는다.
- 그중 History/CAS 동시성 2개를 10회씩 반복한 **20실행 PASS**. 신규 test 수에 더하지 않는다.
- 결함/보강 후보 3개의 acceptance 제안은 현재 candidate에서 **1 FAIL / 2 ERROR**로 재현했다. 이는 일반 suite PASS로 숨기지 않는다.
- 기존 저장소 test 25개 재실행은 **22 PASS / 3 ERROR**. 3개 모두 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix` 뒤 fixture가 `attempt_id=None`을 역참조한 결과다. Windows 전용 owner-lock 가드가 의도대로 막은 것으로, SQLite 결함이나 Windows 실패로 판정하지 않는다.
- 기존 운영 최적화 92 unique PASS는 신규 결과에 합산하지 않는다. 실제 운영 DB와 WAL을 열거나 복사하지 않았다. 원본 source 수정, 운영 index/VACUUM/migration, provider 호출, native runtime 효과, commit/push는 없었다. writer4-only 운영 권위는 그대로 보존한다.

## 환경과 source 증명

- 최종 Python: `<VENV>/bin/python`, 3.12.14
- SQLite 3.53.1, pydantic 2.13.5, cryptography 50.0.1, openai-codex 0.147.0
- source: `<SOURCE>`
- 공유 전체 source 검증: `../vm-evidence/source-verification.json`, `../vm-materialization/source-archive-manifest.json`
- 최초 선택 취득 12개 Git blob 검증: `artifacts/source-manifest.json`. 최종 검사는 전체 source를 직접 사용한다.
- 초기 system pydantic 2.13.4 실행은 예비 관측일 뿐이다. locked 환경의 최종 실행이 이를 대체한다.
- OS는 Linux/POSIX다. multiprocessing은 fork를 사용한다. Windows owner lease·Win32 native lock은 검증하지 않았다.

## 확인된 결함과 보강 후보

### SQ-INIT-1: 초기화 실패가 부분 schema를 남긴다

`ledger.py:794-817`의 schema 생성 뒤 첫 metadata write에 오류를 주입했다. 38개 table이 이미 남았고 `schema_meta` 0행, application_id/user_version 0이었다. 다시 `initialize()`하면 `EngineLedgerError: prototype 또는 외부 DB를 Engine 원장으로 열 수 없습니다.`로 거절된다. SQL DDL과 metadata/identity가 단일 atomic transaction에 묶여 있지 않아 신규 DB 초기화 중단의 복구 가능성이 떨어진다.

제안: 신규 DB에 한정해 schema와 identity를 명시 transaction으로 묶고 오류 시 rollback한다. 기존/외부 DB 보호와 migration 금지는 유지한다. 이후 부모 요청으로 별도 proposal copy에만 최소 수정을 구현했다. 아래 후속 절을 따른다.

### SQ-INIT-2: 동시 최초 initialize 경합

두 프로세스가 동일한 존재하지 않는 fixture DB를 대상으로 `is_new` 검사 뒤 동기화되도록 만들었다. 하나는 초기화됐고 하나는 `table budget_policy_revisions already exists` 또는 `database is locked`로 끝났다. 이 현상은 동시 초기화의 멱등성 보강 후보이며 기존 제품 계약의 명시적 위반 또는 Windows 운영 장애로 단정하지 않는다.

제안: writer lock 획득 뒤 identity를 다시 검사하는 초기화 경로와 동시 최초 시작 acceptance를 소유 팀에서 결정한다.

### SQ-HIST-1: 손상 JSON이 audit snapshot을 중단

disposable fixture에서만 update 금지 trigger를 내려 `history_events.payload_json`을 잘못된 JSON으로 바꿨다. `_verify_history_rows`(`ledger.py:733-759`)가 JSON 파싱 예외를 처리하지 않아서 `verify_history()`와 `project_snapshot()` 모두 `JSONDecodeError`로 끝났다. hash mismatch의 false 반환과 달리 진단 snapshot 자체가 제공되지 않는다.

제안: 손상 JSON/비canonical 값은 명시적 invalid 또는 별도 integrity error로 보고하고 원본을 보존한다. 이는 손상 내구성 제안이며 악의적 같은-process/raw-SQL 격리 보장을 주장하지 않는다.

### SQ-HARDEN-1: 역사 reader의 confinement 차이

`SQLiteEngineHistoryReader._connect`는 `mode=ro`로 main DB 쓰기는 막지만 `query_only`와 `require_host_execution`을 설정하지 않는다. 따라서 합성 auxiliary DB ATTACH 뒤 쓰기 및 role scope 안의 read가 가능했다. 반대로 일반 ledger read는 두 경계가 적용되어 검사에 통과했다. AGENTS는 같은-process raw-code 격리를 보장하지 않으므로 **defense-in-depth 관측**으로만 분류한다.

## Query-plan 검사

61개 합성 project와 jobs/plans/tasks/attempts/context 각 1,820행, history 1,801행을 만들었다. 대상 project는 각 20행이다. 원본 조회와 fixture-only candidate index 조회의 결과 및 순서 digest가 모두 동일했다. 아래는 cold schema 준비를 제외한 SQLite VM instruction count다. wall-time/production speedup 수치가 아니다.

|조회|원본 VM steps|fixture index 후|원본 관측|
|---|---:|---:|---|
|History tail|17|17|기존 unique index 사용|
|History 전체|24|24|기존 unique index 사용|
|Snapshot jobs|372|170|정렬 temporary B-tree|
|Snapshot plans|372|190|정렬 temporary B-tree|
|Snapshot tasks|7,389|170|전체 task index scan|
|Snapshot attempts|5,690|130|전체 scan + temporary B-tree|
|Snapshot context|252|110|정렬 temporary B-tree|

제안 index는 `(project_id,created_at)` jobs/attempts, `(project_id,plan_id,revision_no)` plans, `(project_id,plan_revision_id,position)` tasks, `(project_id,registered_at)` context다. **disposable fixture에만** 추가했다. 생산 적용 승인이 아니고 과거 candidate 대비 regression이라는 주장도 아니다. 유지 비용·write overhead·Windows 실제 workload 평가는 미실행이다.

현재 exact candidate에서 새로 만든 schema는 **38 table / 명시 index 7 (auto 포함 94) / trigger 29**다. 기존 운영 기록의 26 table / 45 index / 79 trigger와 동일 schema라고 합칠 수 없다.

## 신규 26개 범위

WAL/FULL/foreign_keys/busy_timeout/query_only, Unicode·공백·#·? 경로, FK rollback, BaseException rollback, read/transaction close, role read/write 거절, snapshot 종료 뒤 hashing, 별도 process writer와 WAL snapshot 공존, busy wait 성공 및 실제 10초 timeout 뒤 재사용, 6 writer의 History 150행 chain, process 강제종료 rollback, 8 process CAS 단일 winner, terminal job 무효 claim, observation fault의 CAS rollback, partial unique active job, Goal별 current snapshot uniqueness, append-only/hash tamper, 외부 DB byte 보존, execution/effect column 없는 최소 synthetic schema3 읽기와 원래 usage 의미·byte 보존, identity mismatch, 명시 BEGIN 없는 read 특성, project별 History 분리, STRICT/CHECK, project_snapshot의 7개 SELECT 사이 writer commit 7회에도 동일 snapshot 유지.

## 재실행

기본 cwd는 `<WORKSPACE>`. source가 다른 곳이면 `VM_SOURCE`만 바꾼다. 모든 DB는 이 폴더의 `artifacts` 아래 TemporaryDirectory로 생성되어 종료 시 제거된다. 운영 DB 경로 입력을 받는 도구가 아니다.

```sh
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_sqlite_boundaries.py
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/run_concurrency_stress.py
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/probe_failures_and_plans.py
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_proposed_regressions.py ProposedRegressions
```

기대 exit는 순서대로 0, 0, 0, **1**이다. 마지막 명령은 미수정 문제를 고정하는 제안 acceptance이다. 상세 cwd/명령/exit와 raw 로그는 `artifacts/execution-manifest.json`, `logs/`에 둔다.

기존 repo 검사 cwd는 `vm-portable`이며 명령은 `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD/src ../vm-test-venv/bin/python -m unittest tests.test_engine_ledger_snapshot tests.test_engine_ledger_service tests.test_engine_current_snapshot_goal_scope -v`다. exit 1과 위 POSIX 제한을 그대로 보존했다.

## 후속: 초기화 atomicity 최소 구현 제안

부모의 추가 구현 요청에 따라 `proposal/src/flowmarshal/engine/ledger.py`에만 패치를 적용했다. canonical `vm-portable`은 수정하지 않았다. 실행·운영 DB migration도 없다.

- runnable diff: `artifacts/atomic-initialize.patch` (파일 1개, initialize 메서드만)
- schema SQL script 내부에서 `BEGIN IMMEDIATE`를 시작하고 metadata/application_id/user_version을 동일 transaction으로 묶는다
- BaseException 때 명시 rollback한다
- 실패한 신규 DB에 SQLite 빈 header가 남을 수 있어, **application_id=0, user_version=0, sqlite_master 완전 비어 있음**을 모두 만족하는 DB만 신규로 받아 재시도한다
- 검증된 빈 SQLite를 받아들이는 범위가 기존 zero-byte 조건보다 넓다는 점은 integration review 대상이다. 기존 비어 있지 않은 schema나 비0 identity를 채택/변환하지 않는 회귀를 포함했다. 파일 삭제는 하지 않는다

추가 atomic regression 8개 결과:
- canonical before: **2 PASS / 5 FAIL / 1 ERROR**, exit 1
- proposal after: **8 PASS**, exit 0
- metadata 뒤, application_id 뒤, commit 직전, 중간 DDL, KeyboardInterrupt fault의 rollback+재시도, foreign identity/schema byte 보존, empty/zero-byte DB 초기화를 확인했다
- 기존 신규 26 경계 전부 proposal에서도 **PASS**, exit 0
- patch apply/whitespace check exit 0. canonical ledger blob은 여전히 `fe82d17527a0dfa6ce4a37f13f0a7e28a23fd6ab`
- 동시 최초 initialize 멱등성과 malformed JSON API 의미는 고치지 않았다. proposal의 기존 문제 3개 검사에서 초기화 재시도만 PASS이며 나머지 1 FAIL / 1 ERROR는 유지한다
- 기존 repo 25개 검사에서 POSIX owner-lock 제한은 그대로 남는다. 제품 release GO 또는 Windows 검증 완료를 주장하지 않는다

추가 재실행:

```sh
VM_SOURCE="$PWD/vm-portable" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_atomic_initialize_proposal.py
VM_SOURCE="$PWD/vm-sqlite/proposal" PYTHONDONTWRITEBYTECODE=1 vm-test-venv/bin/python vm-sqlite/test_atomic_initialize_proposal.py
```

첫 명령 exit 1, 둘째 exit 0. full source copy는 게시하지 않으며 patch와 합성 test만 parent/publisher에게 전달한다.
