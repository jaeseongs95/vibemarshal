# 1.0 전 승인 로드맵

> 상태: **NO-GO**
> 실제 역할 48건 실행 source(v9)의 결정적 Gate: **5/5·824개 통과**
> 과거 고정 source(v13)의 결정적 Gate: **5/5·842개 통과**
> 최근 실제 v25: **10 PASS·1 NOT_RUN**, 11번째 호출 전 예산 차단으로 전체 FAIL. 동일 10사례의 v24 PASS는 1건.
> 최신 검증본 v26: **결정적 Gate 5/5·946개 PASS**, 새 입력의 실제 모델 호출 0회. 예산 증액 승인 전 추가 실제 호출 중단.
> 과거 v19: 역할 **48/48 PASS**, Planning **15 PASS·3 FAIL**, 실제 110회 정산·History 감사 PASS. 새 source의 Gate로 재사용하지 않음.
> 실제 역할 회귀: v9 **FAIL**·v12 **PASS** 보존, v13 **48/48 PASS**·Planning **18/18 완료·FAIL**
> 실제 동일 Goal 진단·복구: **두 Task·독립 Goal Test·satisfied GoalVerdict 완료**

이 문서는 승인된 1.0 선행 순서와 각 단계의 재개 조건을 한곳에 표시한다. 제품 계약이나 합격선을 새로 정하지 않는다. Core와 SQLite 원장, 활성 Goal·Plan 계약 및 실제 실행 receipt가 권위이며, 이 문서는 그 결과를 대체하지 않는다.

## 현재 판정과 해석 원칙

- 현재 1.0 판정은 NO-GO다. v26 결정적 검사와 v25의 실제 의미 개선을 구분하고, 승인 예산에서 막힌 추가 실제 호출을 중단했다. 최종 source의 S06·역할·Planning·E2E·성능 qualification과 이후 main 병합은 미완료다. [source별 결과](pre-1.0-iterative-validation.md)를 구분해 보존한다. timeout·usage 미확인은 성공이나 0 사용량으로 환산하지 않으며 기존 thread를 먼저 관측하고, 종료 후 usage가 없으면 `BUDGET_USAGE_UNKNOWN`으로 추가 호출을 중단한다.
- 과거 개발 Gate의 **710 tests** 및 **Gate 5/5** 결과는 고정된 과거 source·계약의 provenance다. 현재 source 또는 이번 변경의 PASS가 아니며, 변경된 계약에 재사용하지 않는다.
- schema 3은 새 Engine 원장 기준이다. 이전 v2 원장·artifact는 변경하거나 제자리 migration하지 않고 보존한다. v2 결과를 schema 3 qualification의 근거로 섞지 않는다.
- 동일 Goal은 저장 후보 continuation·사용자 exact Plan 활성화·환경 복구·두 Task·독립 Goal Test를 거쳐 완료했다. 여러 개발 source와 명시적 운영 보정을 거친 진단·복구 결과이며, 최종 source의 전체 qualification PASS가 아니다. 실제 ID·실측·실패 보존 근거는 [현재 인계](pre-1.0-handoff.md)에 있다.
- 현 구현의 `1m / 100k / 25`은 사용자 승인 검증 설정이며 최적값·제품 상한·새 합격선이 아니다. 최초 48회의 실측 총 1,332,724 token을 모두 정산했다. 사용자는 2026-09-06에 이 정책 안에서 실패 분석·수정·전수 재검증과 후속 qualification을 계속하도록 추가 승인했다. token 상한 또는 호출 예약량 증가는 다시 승인받고, 과거 진단 Goal의 `2m / 200k / 25` override는 다른 Goal에 승계하지 않는다.

## 근거와 사용 범위

| 근거 | 이 문서에서 사용하는 내용 | 채택하지 않는 해석 |
|---|---|---|
| [참고자료 목록](../../자동화템플릿/참고자료/자료목록.md) 및 [보관 안내](../../자동화템플릿/참고자료/README.md) | 승인계획의 출처와 방향 자료의 위치 | 보관 자료·아이디어의 자동 구현 또는 자동 승인 |
| [다중 세션 실행 설계](../../자동화템플릿/참고자료/방향검토/FlowMarshal_향후_작업방향_다중세션_실행설계_v1.0.md) | M2~M5와 S05~S19의 순서, 실패 시 재개 원칙 | 새로운 단계명·수치·기능 범위 |
| [planning feedback loop](planning-feedback-loop.md) | S06 실제 경로의 timeout, usage 미확인, 현재 NO-GO | timeout을 성공·실패 외의 임의 PASS로 처리하는 일 |
| [inspection recovery progress](inspection-recovery-progress.md) | 과거 Gate·S06 결과의 source-bound provenance | 과거 결과의 현 source 승격 |
| [A1 handoff](alpha-a1-handoff.md), [A2 handoff](alpha-a2-handoff.md), [A3 handoff](alpha-a3-handoff.md), [A4/A5 execution](alpha-a4-a5-execution.md) | Alpha 단계별 연결 범위와 미충족 E2E | 부분 fixture·정적 통과의 1.0 판정 대체 |

## 승인된 pre-1.0 순서

이번 사용자 승인 순서는 **보고 오류 수정 → S06~S09 실제 완료 경로 → S10~S12 계측·예산 → 최소 모델 복구·조회 → qualification**이다. 기존 S단계는 추적용 대응표이며 과거 자료의 일정이 이번 지시보다 우선하지 않는다. 독립적인 코드·검증 작업은 진행할 수 있지만, 실제 실행에 필요한 예산·관측·계약 선행조건은 건너뛰지 않는다.

| 반영 항목 | 참고자료·현재 근거 | 완료 조건 |
|---|---|---|
| 최종 보고와 조회 | 기존 `engine/cli.py` 보고 경로, [공통 조회](../src/flowmarshal/engine/application.py) | 과거 Verdict의 정확한 Plan·Goal revision, 동일 Goal 계보의 stage/role별 실측 소계·미확인 호출·이유. 불완전 총량 null, JSON/Markdown 동일. |
| S06~S09 연결 | [feedback 기록](planning-feedback-loop.md), [다중 세션 실행 설계](../../자동화템플릿/참고자료/방향검토/FlowMarshal_향후_작업방향_다중세션_실행설계_v1.0.md) | 원본 요청·후보·receipt·사후 terminal 대조, 역할별 timeout 결속, 같은 Goal의 명시 continuation, exact Plan 활성화·Task/Goal 독립 검증. timeout은 의미 finding으로 바꾸지 않는다. |
| S10~S12 계측·예산 | 같은 실행 설계의 F04/F09 및 기존 25% reserve | 모든 역할 호출 전 예약/후 정산, 미확인 시 차단, 명시 잠정 차감과 실측 분리, 재계획과 필수 재검토 비용의 reserve 적용. |
| 최소 모델 복구 | [모델 서비스 종료 설계](../../자동화템플릿/참고자료/설계아이디어/FlowMarshal-모델-서비스종료-감지-및-설정갱신-프로세스-설계.md), 현 model/list preflight | 미지원 이유·영향 Task·다음 행동 표시. envelope 안의 명시 선택·사유·새 inventory binding을 새 Spec/Attempt에 기록하고, 범위 밖은 새 Plan 요구. 불명확한 호출은 재실행하지 않는다. |
| 공통 typed 조회 | [역할별 운영 계획](../../자동화템플릿/참고자료/설계아이디어/FlowMarshal-역할별-모델-추론수준-검증-및-다중모델-운영계획.md), 기존 Core/CLI | EngineApplication의 usage_summary, attempt_detail, recovery_status, model_binding_status. revision/digest·오류 코드·다음 행동·History cursor를 CLI가 표시. HTTP/GUI 제외. |
| 측정과 출시 Gate | 기존 qualification와 성능 측정 창 | 동일 source·고정 설정의 결정적 Gate, 역할48, Planning18, 실제 E2E4, 성능36 전수 통과. 상세화 폐기는 실제 lifecycle에서 관측하며 미관측은 0%가 아니다. |

| 단계 | 출처 | 기존 구현·근거 | 이번 변경 | 남은 수용기준 |
|---|---|---|---|---|
| S00 기준선 고정과 실행 범위 잠금 | 다중 세션 실행 설계, A1~A5 | 과거 source·artifact 및 handoff가 보존돼 있다. | 없음 | 이번 실행 source, fixture, 계약, model inventory, 원장 위치를 실행 전에 고정하고 digest로 결속한다. |
| S01 F01 최소 회귀 | 다중 세션 실행 설계, A1 | 최소 회귀와 결정적 경로의 선행 구현·기록이 있다. | 없음 | 현 source에서 F01 재현과 최소 회귀의 결과를 새 receipt로 남긴다. |
| S02 Task Preparation과 Goal Test 분리 | 다중 세션 실행 설계, A2~A5 | Task 검증과 독립 Goal Test 분리 구현·기록이 있다. | 없음 | 새 source에서 두 검증의 실행·evidence·판정이 분리됨을 확인한다. |
| S03 필수 Context 예산 fail-closed | 다중 세션 실행 설계, A1~A3 | Context 계약·검사의 선행 구현이 있다. | 없음 | 누락된 필수 Context가 실제 경로에서 fail-closed되고 원장 reason이 남는다. |
| S04 실제 Runtime Prompt 단일 권위 경로 | 다중 세션 실행 설계, A3~A5 | Prompt/binding 검사와 handoff 근거가 있다. | 없음 | 실제 role 호출 prompt와 계약 digest가 동일 경로로 결속됨을 새 실행에서 확인한다. |
| S05 최소 실제 bugfix와 Goal Contract 고정 | 다중 세션 실행 설계, A4/A5 | 제한된 Python bugfix fixture와 Goal 계약 자료가 있다. | 없음 | 새 source에서 자연어 요청, 관측, Goal Contract, 고정 검증 자료를 실행 전 확정한다. |
| S06 자연어 요청부터 실제 Selected Plan | 다중 세션 실행 설계, planning feedback loop, inspection recovery progress | 동일 Goal revision 2의 후속 Reviewer가 완료됐고 exact Plan 선택과 사용자 활성화를 기록했다. 과거 timeout/usage 미확인은 보존한다. | 역할별 timeout 결속·사후 terminal 관측·저장 후보 continuation을 추가했다. | 완결 receipt가 있는 실제 Planner·Reviewer 관측, exact Selected Plan digest와 source·Goal binding을 남긴다. timeout은 PASS가 아니다. |
| S07 Plan 활성화부터 Task Validation | 다중 세션 실행 설계, A4/A5 | 사용자 승인 Plan의 두 Task와 최신 Task validation 3개가 실제 PASS다. 이전 환경 실패와 잘못 재사용된 FAIL은 보존했다. | 환경 복구 입력·새 Attempt·History 검증 epoch·Worker별 operation/receipt 결속을 연결했다. | 동일 Goal의 진단 경로는 충족했다. 최종 source로 처음부터 수행하는 E2E qualification은 남는다. |
| S08 독립 Goal Test와 Goal Finish | 다중 세션 실행 설계, A4/A5 | 새 독립 명령·task-less evidence·Goal Test PASS를 참조한 satisfied GoalVerdict를 기록했다. | schema 실패 원본을 보존하고 같은 검사 의미의 명시 운영 명세로 복구했다. | 실제 연결은 충족했다. 수동 보정이 포함돼 최종 source의 무인 pipeline 통과 근거는 아니다. |
| S09 중단·재시작·중복 방지 | 다중 세션 실행 설계, A5, inspection recovery progress | timeout·효과 전 실패·환경 실패·종료가 확인된 schema 실패를 기존 thread와 receipt로 대조했다. | 실패·claim 보존 continuation, Task validation 복구, 엄격한 operation.failed 종료를 추가했다. | 현재 Goal의 미해결 operation은 0이다. 별도 실제 E2E 4-cell에서 중단·재시작·중복 방지를 전수 검증한다. |
| S10 모든 LLM 호출 Usage Receipt 연결 | 다중 세션 실행 설계, planning feedback loop | 실제 provider 16회 중 15회 실측, 과거 timeout 1회 미확인을 확인했다. 실패 역할 호출도 비용에 남는다. | schema 3 provider_calls와 예약·receipt·정산 귀속 경로를 추가했다. | 현재 Goal 계보 대조는 완료했다. 전체 역할 회귀와 E2E에서 역할별 호출 누락·중복을 추가 검증한다. |
| S11 Goal 전체 Usage 집계와 Reconciliation | 다중 세션 실행 설계 | 현재 Goal의 실측 소계 788,868과 미확인 1회, 별도 잠정 차감 100,000을 확인했다. 최종 JSON·Markdown이 일치한다. | Goal별 revision 집계, 중복·충돌·누락 표시, token과 nullable latency 독립 집계를 추가했다. | 불완전 실측 총량은 null이다. 여러 Goal·revision·과거 Verdict·중복 receipt 회귀가 최종 Gate에 포함된다. |
| S12 Goal Budget과 Replan Reserve 실제 집행 | 다중 세션 실행 설계, orchestration redesign | 예산 부족으로 중단한 뒤 사용자 승인으로 현재 Goal만 2m/200k/25로 조정하고 완료했다. 일반 잔여 611,132·reserve 500,000·미정산 예약 0이다. | 정책·예약·정산·명시 조정과 회귀 검증을 연결했다. | 현재 Goal에서 집행을 확인했다. 새 qualification Goal의 예산 범위를 별도로 고정하고 실측 초과·unknown은 계속 차단한다. |
| S13 제한된 Project Map·Context 정확성 보강 | 다중 세션 실행 설계 | Project Map·Context 구성 요소와 선행 검사 자료가 있다. | 없음 | 고정 source에서 필요한 사실만 Context Pack에 투영하고 digest·freshness 위반을 fail-closed로 기록한다. |
| S14 Qualification source freeze와 결정적 Gate | 다중 세션 실행 설계, [반복 검증](pre-1.0-iterative-validation.md) | v16 Gate 5/5·880개 테스트를 통과했다. legacy 40개는 변경·누락 없다. | 출력 계약·문맥 검토·재시작 차단·프로젝트 결속·직접 빈 세션 관측 보완을 새 source로 동결했다. | 최종 source의 나머지 모든 qualification을 같은 계약 계보의 새 run root에서 수행한다. |
| S15 역할 회귀와 Planning Qualification | 다중 세션 실행 설계, [반복 검증](pre-1.0-iterative-validation.md) | v19 역할 48/48 PASS·recall 100%·precision 97.67%. Planning 18/18 완료·15 PASS·3 FAIL이며 실제 110회 호출·usage·공개 합계와 History 감사는 통과했다. | 검사 의무 정의·v2 기계적 참조 전개·단계별 수정·독립 재심·후보별 오류 격리를 보완한 새 source를 검증한다. 원본 실패와 비용은 보존한다. | 새 source에서 결정적 Gate부터 역할·Planning을 전수 검증한다. 실패 cell 교체·사후 oracle 조정은 하지 않는다. |
| S16 실제 E2E Qualification과 상태 보고 정합화 | 다중 세션 실행 설계, A4/A5, [반복 검증](pre-1.0-iterative-validation.md) | 부분 fixture 실행과 상태 보고가 있다. | runtime 완료 observer 전달, 요청/실제 모델 관측 구분, exact Plan 활성화 근거와 빈 thread 예약 해제를 보완했다. | 실제 프로젝트에서 S05~S09 전체를 완료하고 보고 상태가 원장 verdict·evidence와 일치함을 확인한다. |
| S17 Benchmark 측정 창과 지표 의미 수정 | 다중 세션 실행 설계 | 측정·benchmark 구성 요소가 있다. | 같은 Goal의 모든 materialized Spec·수정 후보 출력 비용을 정확히 결속하고 불변 lifecycle 재관측 명령을 추가했다. 미관측은 null/NOT_OBSERVED다. | 기능 qualification과 분리된 측정 창, 입력·모델 lock·지표 정의를 고정한다. |
| S18 동일 입력 소규모 Pair 수집 경로 검증 | 다중 세션 실행 설계 | 비교 수집을 위한 기반이 있다. | 없음 | 동일 입력의 소규모 pair를 실행해 receipt·비용·품질 수집 경로가 일관되게 결속됨을 확인한다. |
| S19 36-cell 비용·품질 비교와 1.0 승격 결정 | 다중 세션 실행 설계 | 비교 구조와 과거 자료가 있다. | 없음 | S14~S18의 새 source evidence를 바탕으로 36-cell 결과와 모든 선행 Gate를 검토한다. 이때만 1.0 승격 여부를 판정한다. |

S00~S04는 선행 설계·구현의 범위를 보존하기 위한 표기이며, 현재 qualification의 PASS 선언이 아니다. 이번 변경은 보고·운영 복구·계측 경계를 보완하며 실제 완료·출시 검증을 건너뛰게 하지 않는다.

## 현재 검증 설정의 사용

| 항목 | 승인된 사용법 | 금지된 해석 |
|---|---|---|
| `1m / 100k / 25` | 최초 48건에 적용·정산했고, 추가 사용자 승인에 따라 같은 정책의 반복 검증에 사용한다. 실행별 source·계약·예산을 결속한다. | 최적 설정, 일반 제품 기본값, 새 품질·비용 합격선, 승인 없는 token 상한·예약량 증가 |
| timeout 또는 receipt 미확인 | 먼저 provider 상태·기존 intent를 관측하고, 외부 효과 불명과 종료 후 usage 미확인을 구분해 차단한다. | 자동 재시도, 자동 해제, 0 token·0 latency·PASS 처리 |
| `usage_unknown`, 누락, receipt 충돌 | total을 불완전으로 표시하고 reconciliation의 다음 행동을 제공한다. | adjustment나 추정 reservation을 실제 사용량으로 합산 |

## 1.0 이후 별도 검토

| 후속 항목 | 미루는 이유 |
|---|---|
| 내부 영어 생성, 사용자별 표시 언어, 로컬 번역·NMT·번역 cache | [내부언어 설계](../../자동화템플릿/참고자료/설계아이디어/FlowMarshal-내부언어-토큰최적화-및-사용자선택-표시번역-구현계획.md)의 의미 보존·품질·비용을 독립 평가해야 한다. |
| 모델 종료일 선제 감지·알림, 대체 모델 자동 평가·설정 갱신 | 현재 미지원 차단과 명시 복구로 최소 운영 경계를 마련한다. |
| GUI, SSE, 상주 RuntimeSessionManager, 설치·업데이트 프로그램 | 공통 typed 조회 인터페이스 뒤에서 독립 확장한다. |
| 전체 모델·effort 조합 탐색, 동적 모델 선택 최적화 | 우선 한 역할 구성의 실제 완료와 출시 검증을 확보한다. |
| Beam/MCTS, 학습 휴리스틱, 범용 코드 지식 그래프 | [토큰 최적화 설계](../../자동화템플릿/참고자료/설계아이디어/LLM_작업계획_토큰최적화_설계서_v1.2_작업지점중심.md), [통합 계획 설계](../../자동화템플릿/참고자료/설계아이디어/llm_task_planning_integrated_design_v2.md)의 추가 복잡도를 운영 근거와 비교한다. |
| 동일 프로젝트 병렬 실행, 다중 OS·원격 실행, VM·WSL 보안 강화 | 별도 충돌·복구·환경 qualification이 필요하다. |
| 범용 Plugin·템플릿 배포, 자동 예약 생성·Git 운영 | Core+CLI의 안정적 실행 이후 제품화 범위로 관리한다. |

Planner 권위화, 별도 승인 계층, 자동 fallback처럼 기존 권위 경계와 충돌하는 제안은 후속 기능으로도 그대로 채택하지 않는다. 참고자료 원문과 과거 인계는 보존한다.

## 충돌 시 처리

- 참고자료, 과거 handoff, 구현 코드, 역사적 숫자가 현재 사용자 지시·활성 Goal/Plan·새 source receipt와 충돌하면 자동 채택하지 않는다.
- source·fixture·prompt/schema·threshold·inventory·receipt의 binding이 다른 결과는 동일 Gate의 PASS로 합치지 않는다.
- 새 기능, 단계명, 합격선, benchmark 수치, 실행 범위를 이 문서에서 추가하지 않는다. Task 의미나 사용자 목표 변경은 기존 Goal·Plan revision 규칙에 따라 검토하고, 운영 상세는 Execution Spec revision으로 처리한다.
