# Release Performance Floor 권위 계약

이 문서는 FlowMarshal 1.0 cutover에 사용하는 `ReleasePerformanceFloor`의 권위 설명이다. 과거 `TokenLatencyGateReport` v3.0과 여섯 최적화 목표의 원문·수치를 변경하지 않는다. v3.0은 역사 결과를 읽고 다시 계산하는 호환 형식이며, 새 cutover 판정에는 final `PerformanceQualificationReport` v4.0만 사용할 수 있다.

## 1. 1.0 판정에서의 위치

1.0 승격에는 다음 다섯 조건이 모두 필요하다.

1. deterministic schema·DAG·ledger qualification
2. 실제 Goal/Reviewer 역할 fixture qualification
3. Skeleton-to-selection 전체 실제 모델 planning qualification
4. activation-to-recovery 실제 프로젝트 E2E qualification
5. 같은 source·고정 계약의 final `ReleasePerformanceFloor`

앞의 네 qualification은 서로 다른 계약·artifact·receipt를 갖는다. 성능 최소선도 별도 불변 평가이며 네 기능 Gate를 대신하지 않는다. 하나라도 실패·미실행·미관측이면 cutover는 `NO-GO`다.

## 2. 고정 비교 행렬과 측정값

성능 행렬은 기대 manifest에서 직접 고정하며 수집된 cell로 기대 집합을 유추하지 않는다.

- 6개 scenario × seed `(17, 43, 89)` × `r31_baseline`/`skeleton_engine` = 36 cell
- 같은 scenario·seed의 baseline/Engine = 18 whole pair
- 정상 Plan 선택 pair 12개, 질문·차단 pair 6개
- scenario digest, path kind, 중립 입력 digest, 기대 disposition, model lock을 pair 양쪽에서 대조

Token 측정값은 planning 단계의 `uncached_input_tokens + output_tokens`다. 전체 input과 cached input도 원본 usage로 별도 공개하고 `uncached = input - cached`를 검증한다. 각 pair의 상대 비율 `(baseline - engine) / baseline`을 먼저 정확한 유리수로 계산한 뒤 평균·중앙값을 구한다. 보고서에 내보내는 최종 비율만 float다. baseline token이 0이면 상대 비율을 0으로 만들지 않고 `null / NOT_OBSERVED`로 남겨 최소선을 통과시키지 않는다.

최초 feasible plan 시간은 정상 Plan 선택 12 pair에서만 비교한다. 모든 18 pair의 최종 disposition 시간은 별도로 비교하므로 질문·차단도 final disposition 지연 최소선에 포함된다. 기대 cell이나 whole pair가 하나라도 없으면 집계 지표를 부분 표본으로 계산해 합격시키지 않고 `null / NOT_OBSERVED`로 반환한다.

## 3. 1.0 필수 최소선

다음 여섯 수치를 모두 만족해야 수치 최소선이 통과한다.

| 지표 | 필수 최소선 |
|---|---:|
| 전체 pair token 상대 감소율 평균 | `>= -0.20` |
| multi-path pair token 상대 감소율 중앙값 | `>= -0.25` |
| single-path pair token 최악 회귀 | `<= 0.50` |
| 전체 cell pair token 최악 회귀 | `<= 1.00` |
| 정상 Plan의 Time to First Feasible 상대 개선 중앙값 | `>= -0.25` |
| 전체 pair의 final disposition 상대 개선 중앙값 | `>= -0.25` |

이 값들은 회귀를 일부 허용하는 1.0 출시 하한이다. 정확히 경계값이면 통과한다. 예산·timeout·모델·prompt·fixture를 바꾸어 하한을 맞추지 않으며 정책 본문과 digest를 실제 성능 수집 전에 고정한다.

## 4. 비차단 최적화 scorecard

기존 여섯 목표는 1.0 필수 최소선과 분리된 scorecard로 유지한다.

| 지표 | 최적화 목표 |
|---|---:|
| multi-path token 감소 중앙값 | `>= 0.30` |
| 전체 token 감소 평균 | `>= 0.20` |
| single-path token 최악 회귀 | `<= 0.05` |
| 상세화 후 미실행 Task 비율 | `<= 0.10` |
| 폐기 후보 상세 출력 비율 | `<= 0.25` |
| Time to First Feasible 개선 중앙값 | `>= 0.20` |

모든 분모와 측정값은 완전히 관측되어야 한다. 미실행 Task 비율은 final에서 선택된 Engine 12 cell의 모든 materialized Execution Spec을 분모로 하고, 폐기 후보 출력 비율은 같은 대상의 전체 후보 출력 token을 분모로 한다. 분모가 없거나 효과가 불명확하면 `null / NOT_OBSERVED`이며 final release floor를 차단한다. 측정은 완전하지만 목표치에 못 미친 경우에는 `optimization_targets_passed=false`, `optimization_followups_required=true`와 원시 cell·pair 근거를 공개하되 1.0 cutover 자체는 차단하지 않는다.

## 5. 단계별 평가와 CLI

`flowmarshal-engine-eval benchmark`는 36개 planning cell이 완성된 뒤 planning `PerformanceQualificationReport` v4.0을 만든다. 이 단계는 36개 planning 관측의 기능 결과, 안전 counter, 실제 usage와 수치 최소선을 확인한다. lifecycle 미관측만으로 `planning_assessment_passed`를 막지는 않지만 `release_floor_passed`와 `cutover_eligible`는 항상 false다.

정상 Plan을 선택한 Engine 12 cell은 정확한 Plan 활성화 뒤 Execution Spec → runtime 완료 → validation → State 재관측 → GoalVerdict를 완료해야 한다. `flowmarshal-engine-eval observe-benchmark-lifecycle --run-root <run> --scope-report <report> ...`는 원래 checkpoint와 현재 SQLite 원장을 읽기 전용으로 다시 대조해 final 보고서를 추가한다. baseline cell과 질문·차단 cell은 lifecycle 호출·Attempt가 없음을 실제 원장에서 확인한 경우에만 lifecycle counter와 usage를 0으로 기록한다.

각 planning·lifecycle provider operation에는 시작·종료·deadline·interrupt·thread/turn 결속을 담은 sealed operation trace가 필요하다. trace가 없거나 불완전하거나 digest가 맞지 않으면 관련 counter는 `null / NOT_OBSERVED`이며 0이나 PASS로 바꾸지 않는다. timeout, 중복 provider call·interrupt, unknown effect, usage 미확정, 승인되지 않은 retry/resume, 예산 정책 위반, deadline 위반은 각 cell의 안전 판정에 남긴다.

Trace는 `category=logical|rpc|wait`로 Core의 논리 호출, 실제 RPC, SDK 완료 대기를 구분한다. App Server 하위 조회와 pagination의 각 RPC는 고유 ID·부모 operation ID·method·남은 deadline을 기록한다. 논리 wrapper와 그 RPC를 중복 호출로 세지 않으며, 하위 RPC 근거가 없는 실제 논리 호출은 미관측이다. receipt에 고정한 snapshot뿐 아니라 같은 호출의 현재 JSONL 파일도 재생해 digest를 대조한다. 종료 후 추가 호출이나 미완결 행을 원래 sealed snapshot으로 숨길 수 없다.

`flowmarshal-engine-eval cutover --benchmark-report <final-v4-report> --scope-report <report> ...`는 저장 보고서만 신뢰하지 않는다. 원래 contract·metadata·checkpoint·원장·scope report를 현재 source에서 다시 계산해 같은 final v4.0 보고서인지 확인한 뒤 cutover를 판정한다. v3.0 token/latency 보고서, planning 단계 v4.0 보고서, 일부 cell 또는 합성 lifecycle 결과는 새 cutover 근거가 아니다.

## 6. 미확인 사용량과 현재 실행 상태

Provider 종료나 귀속 가능한 usage가 확인되지 않으면 실제 사용량은 `null / usage_unknown`으로 남고 추가 모델 호출 차단을 유지한다. 잠정 차감·예약량·과거 usage를 실측값으로 바꾸지 않는다. 이 규칙은 성능 평가에서도 동일하다.

현재 코드에 수집기·strict 정책·v4 evaluator·단계별 assessment·cutover 재계산 경로가 구현돼 있다는 사실과 실제 36-cell 성능 결과는 구분한다. 실제 모델/App Server 성능 36-cell은 아직 조회·수집하지 않았으며, 현재 상태 문서가 별도 완료 근거를 기록하기 전까지 ReleasePerformanceFloor와 1.0 판정은 `NOT_OBSERVED / NO-GO`다.
