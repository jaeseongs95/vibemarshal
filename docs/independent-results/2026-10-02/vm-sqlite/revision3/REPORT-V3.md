# SQLite 초기화 제안 revision 3

현재 제안 diff: **NOT_APPLIED_TO_CANONICAL**. 기준은 private vibemarshal commit `32bb0f9dd9f024045d24487312b50f5b703573a3`이며 원본 source/운영 DB/WAL은 수정하지 않았다. 과거 proposal과 raw evidence는 보존했다.

## 제안 변화와 감사에서 배운 점

- 원본 결함: schema 생성 중 metadata/identity 쓰기에 실패하면 부분 schema가 남아 initialize 재시도가 거절됐다
- revision 1: atomic schema transaction과 빈 DB 재시도를 추가했으나, writer lock 밖의 빈 DB 판정 뒤 다른 프로세스가 foreign DB를 만들면 identity를 덮어쓰는 race가 독립 감사에서 확인되어 통합 차단
- revision 2: authoritative acceptance/identity 검사를 writer lock 안으로 옮기고 foreign journal도 수용 전에 바꾸지 않도록 고쳤으나, malformed SQLite 파일의 연결 설정 오류가 acquired connection을 닫지 않는 cleanup 회귀를 독립 감사에서 확인하여 대체
- revision 3: revision 2에 `_connect`의 연결 획득 후 설정 실패 cleanup을 보완했다. close 오류가 원 설정 예외를 가리지 않도록 했다

## 현재 구현 범위

격리 copy의 `src/flowmarshal/engine/ledger.py` 한 파일만 다르다.

1. 초기화 연결은 journal mode를 바꾸지 않고 FULL synchronous를 설정한다
2. `BEGIN IMMEDIATE`의 동일 연결·동일 transaction에서 빈 DB(application_id=0, user_version=0, sqlite_master 없음) 또는 기존 Engine identity를 확인한다
3. Python 3.12 전용 autocommit을 요구하지 않는다. `sqlite3.complete_statement`로 trigger 본문을 보존하여 정적 schema 문장을 실행하고 implicit commit을 피한다
4. schema·metadata·application_id·user_version을 함께 commit한 뒤, 성공한 Engine DB에만 WAL을 설정한다
5. rollback 오류는 최초 초기화 오류를 가리지 않으며, connection 설정 실패도 acquired connection을 정리하고 원 예외를 재전파한다

Foreign DB를 자동 변환하거나 삭제하지 않는다. 기존 데이터·schema·identity·journal 보존 회귀가 포함되어 있다. 손상 History JSON의 API 의미, HistoryReader security 계약, Windows owner-lock 구현, 운영 index/VACUUM/migration은 바꾸지 않았다.

## 정확한 후보

- v3 patch SHA-256: `40339ffa0ef57f5d6152c289344c7fda0011c70c0ea40338861495c3fa170a89`
- v3 ledger SHA-256: `7c1b089720f5b270bd192902aea7db7f793f7903859f11a20e5c55cccb216fc2`
- 원본 ledger Git blob: `fe82d17527a0dfa6ce4a37f13f0a7e28a23fd6ab` (변경 없음)
- v1 patch: `15c25624117c3d6737c919f7979fd623e709be54901087d50e5ea487f42aefa9` (감사 반례로 통합 차단)
- v2 patch: `47701107ba6f78135badc01da6353b62c2ee5429777ac4844a7c5b447e117a7c` (cleanup 회귀로 대체)

## 작성자 검증 결과

|검사|전후 근거|v3 결과|
|---|---|---|
|atomic 실패/재시도 8개|원본 2 PASS / 5 FAIL / 1 ERROR|8 PASS|
|foreign race·lock·원 예외 보존 4개|v1 3 FAIL / 1 ERROR|4 PASS|
|connection 설정 cleanup 3개|v2 1 PASS / 2 FAIL|3 PASS|
|최초 SQLite 경계 26개|동일 회귀 재실행|26 PASS|
|History/CAS 2개 × 10회|재실행이며 신규 20개가 아님|20실행 PASS|
|기존 저장소 회귀 25개|원본/v1/v2와 동일|22 PASS / 3 ERROR|
|patch apply + whitespace 검사|원본 exact source 대상|exit 0|

기존 저장소 3개 ERROR는 Linux/POSIX에서 `RUNTIME_OWNER_LOCK_UNAVAILABLE`로 막힌 뒤 fixture가 없는 Attempt를 역참조한 결과다. 해당 Windows 전용 가드는 유지된다. 새 테스트·재실행·과거 운영 최적화 92 unique PASS를 하나의 신규 수량으로 합치지 않는다.

Test runner는 각 shell command의 실제 UTC 시작/종료 시각·wall duration·cwd·환경·exit를 `execution-v3.json`에 기록했다. 예상된 기존 repo exit 1은 그대로 보존했다. runner 전체 exit 0은 모든 예상 exit가 일치했다는 뜻이며 전체 제품 test PASS를 뜻하지 않는다.

환경은 locked Python 3.12.14 / SQLite 3.53.1 / pydantic 2.13.5, Linux/POSIX다. Windows/native qualification, 실제 provider 호출, 전체 제품 release GO는 미검증이다.

## 독립 감사 상태

별도 auditor가 v3 exact hash의 재검토를 수행 중이다. 확정 disposition은 별도 최상위 상태 파일을 따른다. 이 보고서의 작성자 PASS가 독립 audit나 canonical integration 승인을 대신하지 않는다.

## 재현

깨끗한 exact candidate의 disposable copy에 `atomic-initialize-v3.patch`를 적용한다. 해당 copy를 `VM_SOURCE`로 지정하고 locked Python으로 동봉 test를 실행한다. 실제 원본 저장소에 적용할지는 소유 writer가 결정한다.

```sh
VM_SOURCE=<PROPOSAL_V3> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_atomic_initialize_proposal.py
VM_SOURCE=<PROPOSAL_V3> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_initialize_revalidation.py
VM_SOURCE=<PROPOSAL_V3> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_connection_setup_cleanup.py
VM_SOURCE=<PROPOSAL_V3> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_sqlite_boundaries.py
VM_SOURCE=<PROPOSAL_V3> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python run_concurrency_stress.py
```

모든 fixture는 disposable directory에서 생성된다. 전달물에는 source 전체/AGENTS/운영 DB/WAL/runtime profile·key·pin/개인 자료를 포함하지 않는다. 게시 로그는 cloud 절대 경로만 명시된 alias로 치환했으며 내부 원시 로그는 보존했다.
