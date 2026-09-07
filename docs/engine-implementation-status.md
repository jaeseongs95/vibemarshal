# Engine 구현과 qualification 현황

기준일: 2026-09-07 KST. 이 문서는 현재 상태의 단일 진입점이다. 장기 권위는 [재설계](orchestration-redesign.md)와 [cutover ADR](engine-cutover-adr.md), 작업 순서는 [승인 로드맵](pre-1.0-roadmap.md), 실행 절차는 [현재 인계](pre-1.0-handoff.md)를 따른다.

## 현재 판정

종료 관측·정산 보완은 v28 동결본 `6a17e2d`에서 분리했다. v28의 결정적 테스트 969개·static11 PASS와 qualification13의 첫 호출 timeout·나머지 12건 미실행은 과거 결과로 보존한다. 원래 실패 turn을 새 turn·resume 없이 조회해 `interrupted` 종료를 확인했으나 실제 사용량은 여전히 미확정이다. 기존 pre-Goal 원장은 원본 receipt를 보존한 채 `reserved`에서 `usage_unknown`으로 바뀌었고, History 관측 한 건만 추가됐다. Goal·Profile·사용량 레코드·DB schema를 새로 만들지 않았다.

이 보완의 원본 대조·결정적 Gate·소스 동결 결과는 [종료 관측 보완 실행 보고서](D:/codex/fm-inspection-runtime/planning-continuation-20260906/timeout-observation-recovery-20260907/구현-검증-결과.md)에 연결한다. 실제 사용량이 확보되기 전에는 새 static11·qualification13과 이후 전체 평가, 선택 Plan 실행, lifecycle, main 병합을 차단한다. 잠정 차감·재호출·새 원장으로 비용을 초기화하지 않는다. 최초 30분 지연의 내부 원인이 해결됐거나 qualification이 통과했다고 판정하지 않는다.

**NO-GO**, 개발 package `flowmarshal-engine 0.2.0a1`, 새 원장 schema revision **3**이다. 최종 source의 전체 qualification이 아직 완료되지 않았다. 최초 v9 역할 회귀는 **48/48건 완료·FAIL**로 보존한다. 당시 clean false block 1건과 schema failure 1건, recall 97.06%·precision 90.70%였고 critical false admission·seed 간 critical verdict 불일치는 0건이었다. v9 결정적 Gate는 **5/5·824개 테스트**, legacy 동결 **40개 / 변경·누락 없음**으로 통과했다. [최초 결과와 원인](role-fixture-48-qualification.md)에 원본·source·예산·실패 분류를 연결했다.

최초 v9 48회의 provider/receipt/usage와 History를 읽기 전용으로 감사했고 누락·중복·미정산·미확인 사용량은 없었다. 실측 총량은 **1,332,724 token**이었다. schema 실패 1건은 provider schema가 허용한 값을 사후 검사에서 금지한 평가 계약 불일치와 연결됐다. 정상 Goal 오차단과 별도 필수 finding 누락은 모델 의미 판단 문제로 분류해 후속 source에서 보완했다. 과거 결과는 그대로 보존한다.

v25의 실제 static11은 **10 PASS·1 NOT_RUN·전체 FAIL**이다. 이전 v24의 동일 10사례는 1 PASS였으며, 검사 기여 관계 규칙을 보완한 뒤 의미 오류가 개선됐다. 11번째는 실측 누계 **656,226 token**에 다음 호출 예약 100,000을 더하면 일반 사용 가능분 750,000을 넘어서 호출 전에 차단됐다. 추가 실제 모델 호출은 중단했다. 관측 범위를 과장한 검증 입력을 독립 검토 후 바로잡은 v26은 개발·동결 환경에서 **결정적 Gate 5/5·946개 테스트 PASS**이며 실제 모델 검증은 미실행이다. 최종 source의 S06·역할·Planning·E2E·성능 검증과 main 병합은 미완료다. 제품 기본 provider는 v1을 유지한다. [반복 검증 기록](pre-1.0-iterative-validation.md)에 결속과 중단 근거를 보존한다.

v19는 실제 역할 **48/48 PASS**, Planning **18/18 완료·15 PASS·3 FAIL**, 정상 Plan 선택 9건과 110회 호출 **4,059,253 token**의 정산·History 감사가 완료된 과거 기준선이다. 이 결과를 새 source의 선행 Gate로 재사용하지 않는다.

최신 실제 Goal 계보는 revision 1 conflict에서 독립 피드백 후 revision 2 READY로 진행했고, timeout 뒤 저장 후보를 이어받은 독립 Reviewer가 완료되어 S06의 exact Plan을 선택했다. 사용자가 승인한 Plan을 활성화한 뒤 환경 실패를 복구하고, 두 Task·최신 Task 검증 3개·새 독립 Goal Test를 통과해 Core가 `satisfied` GoalVerdict를 기록했다. 이전 Attempt·FAIL과 schema 실패는 그대로 남는다. 정확한 ID·원본·비용은 [현재 인계](pre-1.0-handoff.md)에 기록한다.

실제 연결은 v2·v4·v6·v7 개발 source를 거쳤고, Task 상세화의 digest 전사 오류와 독립 Goal Test 운영 명세를 명시적으로 수동 보정했다. 따라서 같은 Goal의 실행·검증·복구 완료 근거로 사용하며, 최종 source의 무인 전체 pipeline이나 출시 E2E qualification 통과로 집계하지 않는다.

사용자는 과거 timeout 1회의 100,000 token 잠정 차감 후 같은 Goal을 재개하도록 승인했다. 실제 Worker 재시도 뒤 같은 Goal의 상한 2,000,000·호출 예약 200,000·재계획 reserve 25%를 추가 승인했다. 원장 provider 호출 16회 중 15회의 실측 소계는 **788,868 token**, 별도 잠정 차감은 **100,000 token**이다. 일반 잔여 **611,132**, reserve **500,000**, 미정산 예약 **0**으로 종료했다. 미확인 호출 1회가 있으므로 최종 실측 총량은 계속 `null`이며 잠정 차감을 실측에 넣지 않는다.

## 이번에 연결한 구현

| 영역 | 현재 코드 동작 | 수용 근거 및 남은 경계 |
|---|---|---|
| 최종 보고·공통 조회 | 최종 Verdict가 참조한 Plan·Goal revision을 조회하고 같은 Goal 계보만 집계한다. 실측 0, nullable usage/latency, 중복·충돌·누락을 구분한다. token 미확인과 관측된 latency를 독립 처리한다. | typed 조회 회귀와 실제 최종 JSON·Markdown의 동일 데이터 대조. provider 전체 호출 수와 실측 소계 호출 수를 구분한다. |
| 역할 usage·예산 | Goal 준비·planning·상세화·Worker·Task/Goal Validator·수정 호출을 예약·정산 경로로 연결한다. 정책은 project 기본값/Goal override로 주입하고 재계획 reserve를 보존한다. | 사용량 미확인 시 차단, 외부 효과 전 거부의 예약 해제, 실측 초과·재시작·명시 잠정 정산 회귀. 잠정 정산은 실측 총량에 넣지 않는다. |
| 완료 관측·continuation | 역할 timeout 정책을 request·receipt에 결속하고 interrupt 요청과 terminal 관측을 구분한다. 저장 후보 continuation, Worker별 validation operation 결속, 확인된 역할 schema 실패의 종료 기록을 제공한다. | 동일 Goal의 실제 완료·복구, 새 Worker의 과거 receipt 재사용 차단, validator-only rebind 회귀. 이전 원장·Attempt·FAIL은 보존하며 미확정 효과는 재실행하지 않는다. |
| 모델 변경 | 최신 inventory와 exact Plan/Spec digest를 확인하고 허용 envelope 안의 명시 선택·이유로 새 Spec·Attempt를 기록한다. | 미지원·stale binding·범위 밖 선택·불명확한 효과는 차단한다. 자동 fallback은 없다. |
| 성능 측정 | planning-only의 미관측 상세화 폐기를 0%로 통과시키지 않는다. 실제 lifecycle evidence가 없는 값은 null/NOT_OBSERVED다. | 실제 Spec·Attempt·receipt·validation·State·Verdict 근거를 수집해야 하며 36-cell과 합격선은 유지한다. |

## 출시 전에 남은 검증

1. 보완한 최종 source에서 역할 48건을 전수 검증하고 호출·receipt·usage·History를 감사한다. 이전 source의 PASS·FAIL·원시 결과·합격선은 보존한다.
2. 새 source·계약의 역할 48-cell 재검증 뒤 Planning 18-cell·E2E 4-cell을 전수 검증한다. 2026-09-06의 추가 사용자 지시에 따라 기존 1m/100k/25 정책 안에서 실패 분석·수정·재검증을 계속하며, token 상한이나 호출 예약량을 늘릴 때만 추가 승인받는다. 과거 진단 Goal의 2m/200k override는 승계하지 않는다.
3. 같은 source의 기능 Gate 통과 뒤 성능 36-cell을 수집한다. 선택된 Engine Plan의 exact 활성화와 실제 실행 lifecycle을 별도로 관측하고 불변 assessment를 추가한다.

S06~S09의 동일 Goal 연결과 실제 예산 집행은 확인했다. 단위 테스트·합성 lifecycle·단일 Goal의 진단·복구 성공으로 위 전체 qualification이나 1.0 완료를 선언하지 않는다.

## 과거 결과

- `6beef88` 이전 최신 개발 검증: 710 tests·결정적 Gate 5/5. [원본 검증 기록](planning-feedback-loop.md)에 source manifest와 artifact 경로가 있다.
- 최신 검사 계약의 실제 static 11/11 결과와 S06 재진입 실패는 [검사 복구 진행 기록](inspection-recovery-progress.md), [S06 재진입](inspection-s06-reentry.md)에 보존한다.
- 이전 R19~R32 및 A1~A5 인계는 [문서 색인](README.md)의 역사 기록이다. 현재 source 결과로 승격하지 않는다.
- R3.1은 [동결 기준선](r31-frozen-baseline.md)에 보존하며 source·artifact를 수정하거나 재실행해 GO로 바꾸지 않는다.
