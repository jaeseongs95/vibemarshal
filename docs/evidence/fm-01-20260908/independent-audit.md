# FM-01 최종 독립 감사

판정: **FM-01-C1·C2·C3 통과**. 승인 계약의 문서 정합성에 대한 판정이며 제품 구현, 실제 qualification, FM-15 제품 최종 감사 또는 1.0 릴리스 통과를 뜻하지 않는다. 구현에 참여하지 않은 별도 감사자가 승인 원문, 현재 문서와 실제 Git diff를 직접 대조했다. 이전 세션의 완료 주장이나 메인 보고서의 PASS만으로 판정하지 않았다.

감사 기준은 `D:/codex/flowmarshal`의 `main`, HEAD `c484243cfa871b04b9858a71f74c4736ac505a00`과 그 이후의 미커밋 변경이다. 첫 파일 조회·명령 전에 이 turn의 실제 `danger-full-access`, `approval_policy=never`를 확인했고 main/source/template 지침의 권한 요구와 일치했다. main 상위 경로와 docs 하위에서 추가 적용 지침이 발견되지 않았다. 사용자 제공 전역 지침을 함께 적용했다. 감사자는 이 파일만 작성하며 제품 문서·코드·원장·외부 상태는 변경하지 않았다.

## 승인 원문과 문서 대조

승인 원문은 `D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan.md`이며 SHA-256 `a24eb860c8b603f8edc43a71370c6d8638cc53d3c5c49b8a568c44fc9f5b1742`를 직접 확인했다. 필수 source 문서 6개는 감사 기준 commit의 main 대응 Git blob과 모두 일치했다. 승인 설계가 이미 있는 부분도 원문을 읽어 재검토했다.

| 승인 항목 | 직접 확인한 권위 위치 | 구현 책임 | 감사 결과 |
|---|---|---|---|
| 1 | `redesign-1.0-contract.md` D01, 7행 | FM-03·08 | 기존 Core 유지, 목표 단위 승인, 내부 immutable Plan과 확장 시 추가 판단 보존 |
| 2 | D02, 13행 | FM-03·05 | root·효과·정책 결정적 대조와 의미 review 구분, Attempt 보호·유효 evidence 재사용 명시 |
| 3 | D03, 21행 | FM-04·08 | 응용 명령 경계, 신속한 run_once 반환, 활성 job supervisor와 Core 완료 권위 명시 |
| 4 | D04, 27행 | FM-04·06·08 | 활성화 후 준비·worker·검사·recovery/replanning 전 역할의 job/checkpoint 책임 보존 |
| 5 | D05, 31행 | FM-02·04 | terminal·유효 결과와 usage 분리, null 보존, 효과 미확정 관측 우선, 늦은 usage의 회계 한정 |
| 6 | D06, 37행 | FM-02·08 | token·가격·사용률의 요금 환산 금지, 운영 한도 의미와 backfill 비의존성 명시 |
| 7 | D07, 43행 | FM-02·04·06·07 | 호출14·version5·refinement1·replan2/5·resume1·schema0·900/1800초·30/5초와 적용 범위 검증 책임 보존 |
| 8 | D08, 57행 | FM-05 | 효과 직전 freshness/binding, reserve/start 변화, 응답 유실·강제 종료·timeout·부분 resume 구분 |
| 9 | D09, 63행 | FM-06 | error/evidence 우선 분류, unclassified 보존, 허용 로컬 ContextRequest 자동 탐색과 반복 제한 |
| 10 | D10, 69행 | FM-07 | 의미/canonical dedupe, 비용만의 가지치기 금지, 미사용 CommitHorizon·관측 ProjectMap 경계 |
| 11 | D11, 75행 및 cutover ADR | FM-02·10 | schema 4 별도 DB, schema 3 read-only, 제자리 변환·가짜 객체·옛 budget 재해석 금지 |
| 12 | D12, 79행 및 cutover ADR | FM-07·10 | 기본 v1, v2 자체 채택 조건, Engine-only 패키지와 source-tree bundle 경계 |

V01 85행의 결정적·설치·실제 역할48·Planning18·실제 요청 E2E·독립 감사 조건과 V02 100행의 책임을 원문과 대조했다. 역할48의 recall ≥90%, precision ≥85%, critical false admission·clean false block·schema failure·critical admission seed instability 각 0을 보존했다. Planning18의 정상 4종 선택·정보 부족 2종 질문, 호출 ≤14·version ≤5·선택 후보 deterministic finding 0도 일치한다. 모든 finding 100%를 추가 기준으로 만들지 않았다.

다중 DAG·승인 내 복구, read_only 완전 응답과 무변경, sample 밖 Context, missing/late usage, 저장 후 재시작, 생성 응답 유실·강제 종료·timeout, stale, partial resume, 금지 효과·범위 확장·cancel·실행 중 Attempt 보호, non-editable 설치가 V02에 연결된다. 실제 provider·stub·fault injection·과거 evidence 구분, 실행 전 digest 고정, 사후 oracle/threshold 완화 금지와 최초 실제 역할·Planning 재실행 책임도 유지한다. V03 118행은 비교 성능·GUI·Localizer·광범위 graph 등 후속 범위를 비차단으로 구분한다. M01 124행은 12항 모두를 구현·검증 태스크에 연결하며 원장 상태를 변경하지 않는다.

## 충돌과 변경 범위 검토

`rg -n -C 1`로 usage 차단·`BLOCKED_USAGE_UNKNOWN`·exact/수동 승인·비교 성능 필수·`cutover_eligible`를 검색하고 main AGENTS, 제품 설계, ADR, 계약, 로드맵·인계, README와 관련 현재 권위 문맥을 읽었다. 활성 계약에는 usage만의 전역 차단, 사용자 exact Plan ID 입력 의무, 비교 성능 필수 릴리스 조건이 없다. 외부 효과 미확정의 안전 차단과 내부 digest 결속은 유지한다.

검색에 남은 `*-before-redesign-1.0.md`는 현재 문서가 원문 보존·역사 범위를 명시한다. `planner-r3.md`·`planner-r31.md`와 R1~R3.1 자료는 동결 근거다. 반복 검증·inspection·R-S06·Alpha 기록은 문서 지도에서 당시 source·시점의 provenance로 분류하며 현재 실행 지시로 사용하지 않는다. GUI의 당시 exact-digest UX도 후속 비권위 제안으로 분리됐다. 역사적 문구를 검색에서 삭제한 것으로 정합성을 주장하지 않았다.

source AGENTS의 detached preflight·자동 push와 최신 main-only·push 금지 사이 충돌은 최신 명시 지시 우선으로 해소했다. template의 source_root 문구는 최신 명시 대상을 대체하는 근거가 될 수 없으며 template의 한시 main 규칙은 현 지시와 일치한다. main 밖 source/template은 입력으로 보존했고 물리적 동기화를 주장하지 않는다. main AGENTS에는 장기 우선순위·승인된 checkout·명시 승인 push 원칙을, 인계·로드맵에는 한시 경로와 만료 조건을 뒀다. main 개발·커밋과 감사 후 1.0 전환을 구분한다. 최종 preflight 문구가 origin/main·다른 checkout HEAD의 시작 provenance 원칙을 보존한 것도 직접 확인했다.

## 직접 수행한 검증과 발견사항

| 검사 | 직접 관측 결과 |
|---|---|
| 브랜치·HEAD·상태 | main checkout과 위 기준 HEAD 확인. 변경은 권위 문서 7개 및 `docs/evidence/fm-01-20260908/`에 한정 |
| 증거와 현재 파일 | `documents.json`의 7개 문서 SHA-256, `inputs.json`의 8개 입력 SHA-256 모두 현재 파일과 일치 |
| 동결 보존 | freeze manifest의 40개 SHA-256 모두 일치. manifest root 기준 재열거 40개, 누락 root·미등록 경로 0 |
| 보호 문서 | R3.1 기준선·회귀 fixture·freeze manifest·역사본 5개를 포함한 8개 파일이 기준 commit의 Git blob과 일치 |
| 변경 검토 | 권위 7개 파일의 기준 commit 대비 diff를 직접 읽고 승인 범위·planned 상태·보존 계약 확인 |
| 최종 공백 검사 | `git diff --cached --check`와 `git diff --check c484243cfa871b04b9858a71f74c4736ac505a00` 모두 exit 0, 출력 0행 |
| 제품 실행 검증 | NOT_RUN. 역할48·Planning18·E2E·설치·제품 테스트를 이 감사의 실행 결과로 보고하지 않음 |

감사 중 evidence가 staging된 첫 전체 diff 검사에서 JSON의 CRLF와 patch hunk 제목 후행 공백이 검출됐다. 이 상태를 FM-01-C3 차단사항으로 메인에 전달했다. 메인은 `Save-TaskText`의 LF 정규화와 `@@` hunk 제목만의 후행 공백 제거를 적용하고 patch 생성 뒤 diff 검사하도록 바꿨다. 수정된 생성기를 직접 읽었고 2026-09-08 09:57:55 UTC 관측 묶음의 문서·입력 해시를 다시 대조했다. 이후 전체·staged diff 검사가 모두 exit 0이었다. 최초 실패를 숨기지 않고 `report.md`에도 기록했음을 확인했다. 현재 미해결 감사 발견사항은 없다.

`verify.ps1`은 같은 evidence 파일을 갱신하므로 감사자는 실행하지 않았다. 대신 위 해시·Git blob·동결 root·diff 검사를 별도 읽기 명령으로 수행했다. 증거 생성기는 명시 main 경로와 cwd·branch를 확인하고 tracked/untracked 범위를 검사한다. 검색·해시 통과 자체가 의미 또는 제품 qualification PASS를 만들지 않는다는 한계도 JSON과 보고서에 명시되어 있다.

이 감사가 확인한 완료 범위는 FM-01 문서 계약과 근거의 정합성이다. 기존 harness의 checkout 조건 정합화, 후속 구현과 실제 평가, 제품 최종 감사·1.0 전환은 별도 책임으로 남는다. 운영 원장과 외부 provider 상태는 이 감사에서 조회·판정하지 않았으며 원격 push나 공개 배포도 수행하지 않았다.
