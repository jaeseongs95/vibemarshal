# Engine 구현과 qualification 현황

기준일: 2026-09-06 KST. 이 문서는 현재 상태의 단일 진입점이다. 장기 권위는 [재설계](orchestration-redesign.md)와 [cutover ADR](engine-cutover-adr.md), 작업 순서는 [승인 로드맵](pre-1.0-roadmap.md), 실행 절차는 [현재 인계](pre-1.0-handoff.md)를 따른다.

## 현재 판정

**NO-GO**, 개발 package `flowmarshal-engine 0.2.0a1`, 새 원장 schema revision **3**이다. 최종 source의 결정적 Gate **5/5**, 전체 테스트 **823개 / 122.253초**, legacy 동결 **40개 / 변경·누락 없음**으로 통과했다. 원본 경로와 source digest는 [현재 인계](pre-1.0-handoff.md)에 기록했다. 실제 역할 회귀 48-cell·Planning 18-cell·E2E 4-cell·성능 36-cell은 같은 최종 source와 고정 설정으로 새로 검증해야 한다.

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

1. 새 qualification Goal들의 예산 범위를 명시하고 최종 source·역할·inventory·fixture·seed·합격선을 고정한다. 현재 Goal의 증액을 다른 Goal로 자동 확대하지 않는다.
2. 실제 역할 48-cell·Planning 18-cell·E2E 4-cell을 전수 실행해 원시 결과·오분류·순서 안정성·사용량과 복구를 검증한다.
3. 같은 source의 기능 Gate 통과 뒤 성능 36-cell을 수집한다. 선택된 Engine Plan의 exact 활성화와 실제 실행 lifecycle을 별도로 관측하고 불변 assessment를 추가한다.

S06~S09의 동일 Goal 연결과 실제 예산 집행은 확인했다. 단위 테스트·합성 lifecycle·단일 Goal의 진단·복구 성공으로 위 전체 qualification이나 1.0 완료를 선언하지 않는다.

## 과거 결과

- `6beef88` 이전 최신 개발 검증: 710 tests·결정적 Gate 5/5. [원본 검증 기록](planning-feedback-loop.md)에 source manifest와 artifact 경로가 있다.
- 최신 검사 계약의 실제 static 11/11 결과와 S06 재진입 실패는 [검사 복구 진행 기록](inspection-recovery-progress.md), [S06 재진입](inspection-s06-reentry.md)에 보존한다.
- 이전 R19~R32 및 A1~A5 인계는 [문서 색인](README.md)의 역사 기록이다. 현재 source 결과로 승격하지 않는다.
- R3.1은 [동결 기준선](r31-frozen-baseline.md)에 보존하며 source·artifact를 수정하거나 재실행해 GO로 바꾸지 않는다.
