# SQLite 초기화 제안 revision 2

상태: **NOT_APPLIED_TO_CANONICAL**. canonical 32bb0f9와 운영 DB/WAL은 그대로다. revision 1 보고서·diff·로그도 덮어쓰지 않았다. 이 문서는 revision 1의 기존 PASS를 유지한 채 새 감사 반례를 해결하는 별도 제안이다.

## revision 1 감사에서 확인된 차단점

- 기존 빈 SQLite의 분류가 readonly 연결에서 먼저 끝나고 writer lock 획득 전에 다른 프로세스가 foreign table/data와 application_id 12345 / user_version 9를 commit할 수 있었다
- revision 1은 그 DB에 Engine schema를 추가하고 identity를 FME1/4로 덮어썼다. 초기 atomic 8개 PASS만으로 이 race를 발견하지 못했다
- rollback 자체가 실패하면 최초 초기화 오류 대신 rollback 오류가 노출됐다
- revision 1 patch SHA-256: `15c25624117c3d6737c919f7979fd623e709be54901087d50e5ea487f42aefa9`. **통합 차단 상태**로 보존한다

## revision 2 구현

변경 파일은 별도 source copy의 `src/flowmarshal/engine/ledger.py` 한 개다.

1. 초기 연결에서는 journal mode를 바꾸지 않는다 (`_connect(configure_journal=False)`)
2. `BEGIN IMMEDIATE`로 writer lock을 획득한다
3. 같은 connection·같은 transaction에서 application_id=0, user_version=0, sqlite_master 비어 있음을 확인한다. 기존 DB라면 동일 연결로 전체 Engine identity를 검사한다
4. `executescript`는 기존 transaction을 implicit commit하므로 쓰지 않는다. `sqlite3.complete_statement`로 trigger 본문을 포함한 정적 schema 문장을 나누어 하나씩 실행한다. 이 API는 Python 3.12 전용 autocommit 기능을 요구하지 않는다
5. schema·metadata·application_id·user_version을 같은 transaction에서 commit한다. 성공한 Engine DB에만 WAL을 설정한다
6. 실패 시 rollback을 시도하되 rollback 오류가 최초 예외를 가리지 않게 보존한다. connection은 닫는다

- patch SHA-256: `47701107ba6f78135badc01da6353b62c2ee5429777ac4844a7c5b447e117a7c`
- proposal ledger SHA-256: `839d8ae5769bc66209f23144d778dcfea3c421edda532156fb6d6bc73b63363e`
- canonical ledger Git blob: `fe82d17527a0dfa6ce4a37f13f0a7e28a23fd6ab` (변경 없음)

## 검증

|구분|revision 1|revision 2|
|---|---|---|
|새 lock/revalidation 회귀 4개|3 FAIL / 1 ERROR|4 PASS|
|앞선 atomic 회귀 8개|8 PASS|8 PASS|
|앞선 SQLite 경계 26개|26 PASS|26 PASS|
|History/CAS stress 2개 × 10회|20 실행 PASS|20 실행 PASS|
|기존 저장소 회귀 25개|22 PASS / 3 ERROR|22 PASS / 3 ERROR|

기존 저장소 3개 ERROR는 `RUNTIME_OWNER_LOCK_UNAVAILABLE: platform unsupported: posix`가 반환된 후 fixture가 없는 Attempt를 역참조한 결과다. Windows 전용 owner-lock 가드는 그대로 유지했고 Windows 결과나 SQLite 실패로 바꾸어 해석하지 않는다. 반복·재실행은 신규 PASS 수에 더하지 않는다.

새 회귀는 (a) 기존 빈 DB가 다른 프로세스에서 foreign DB로 바뀜, (b) 존재하지 않던 경로에 다른 프로세스가 foreign DB를 생성함, (c) acceptance 조회가 writer transaction 안에 있음, (d) rollback 실패 중 최초 예외·connection 정리가 보존됨을 검사한다. 두 foreign race 모두 거절하면서 **DB bytes, 데이터 행, application_id, user_version, DELETE journal mode**를 보존한다.

Patch apply/whitespace check는 exit 0이다. 실제 provider/native effects, 운영 DB 접근·migration·index 추가·VACUUM은 없다. Windows/native qualification과 공식 source GO는 주장하지 않는다.

## 미변경 사항과 재실행

손상 History JSON의 API 의미와 HistoryReader security 경계는 수정하지 않았다. 동시 최초 initialize 전체 멱등성은 별도 계약이다. 이번 수정의 권위 판정과 atomicity는 writer lock 안에서 이뤄진다.

검증 source는 별도 copy `<PROPOSAL_V2>`다. 새 checkout에서 재현하려면 exact 32bb0f9에 `atomic-initialize-v2.patch`를 적용한 disposable copy를 `VM_SOURCE`로 지정한다. 게시 patch는 canonical에 적용되지 않은 제안 파일이다.

```sh
VM_SOURCE=<PROPOSAL_V2> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_initialize_revalidation.py
VM_SOURCE=<PROPOSAL_V2> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_atomic_initialize_proposal.py
VM_SOURCE=<PROPOSAL_V2> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python test_sqlite_boundaries.py
VM_SOURCE=<PROPOSAL_V2> PYTHONDONTWRITEBYTECODE=1 <VENV>/bin/python run_concurrency_stress.py
```

모든 DB는 test의 disposable temporary directory에만 생성한다. source bytes를 바꾸거나 운영 DB 경로를 받지 않는다. command별 cwd/exit, 미기록 시각 표시, unittest 소요시간은 execution-v2.json을 따른다.

독립 auditor 재검토: 요청 완료, 이 문서 생성 시 결과 대기 중. auditor의 별도 결과가 통합 판단의 전제다.
