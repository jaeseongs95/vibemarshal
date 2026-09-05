# plan-inspection-v2 독립 11사례 기준선

## 결론

`plan-inspection-v2`의 첫 완전 static 11 실행은 11개 provider turn을 모두 완료했지만 PASS 1, FAIL 10이었다. v1보다 총 token과 실행 시간은 줄었으나 실제 합격 안정성은 낮아졌으므로 S06 qualification으로 진입할 수 없다.

실패는 세 원인군으로 나뉜다.

1. 모델이 원문 citation 객체를 다시 작성하는 중복 장부: 5건
2. typed target과 `affected_task_refs`를 함께 작성하는 중복 장부: 1건
3. AC 연결 bool 또는 finding 자체의 의미 판단: 4건

앞의 두 원인군은 이미 선택한 의미 관계에서 프로그램이 계산할 수 있다. 다음 provider revision에서는 adapter가 고정 citation catalog를 제공하고 모델은 그 ID만 선택하며, 영향 Task는 typed target에서 파생한다. bool·scope status·finding 종류·typed target·직접 evidence 선택은 계속 모델의 직접 판단으로 남긴다.

## 실행 결속

| 항목 | 값 |
|---|---|
| run root | `D:\codex\fm-inspection-v2r1\.flowmarshal-engine-eval\runs\inspection-v2r1-static11-20260906` |
| 고정 source commit | `7d3b20135c7d0070e4e39b56a845520015b38096` |
| source manifest | `sha256:6a72989d5784fd921de7028ed02fbb4b2cfa211c703ed0f475f1c1a8501cabd5` |
| fixture package manifest | `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| preflight binding | `sha256:143e9936c5c3c0ac7908bcc16c43625f0f3412e7d5b2985cdd9f946d7e0aa818` |
| preflight artifact SHA-256 | `3ec58cbf2e5c82cfd31ab1f2e3239816569132a5d046a4ef5764a2f5487cb80f` |
| model lock | `sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005` |
| 역할 | `gpt-5.6-sol`, `xhigh`, fallback 없음 |
| 실제 권한 | `:danger-full-access`, `approval_policy=never` |
| logical/provider/recovery | 11 / 11 / 0 |
| summary SHA-256 | `c162b5be22ace578a15fdd3ca091a9159d34fe78d75de624fe44602da158161a` |

같은 고정 worktree에서 실행 직전 결정적 Gate 5/5와 670개 테스트가 통과했다. 별도 첫 v2 고정본 `880874d5`는 provider가 배열 item의 `oneOf`를 허용하지 않아 모델 추론 전에 400으로 중단됐고, 그 원본은 `D:\codex\fm-inspection-v2\.flowmarshal-engine-eval\runs\inspection-v2-static11-20260906`에 보존했다. 본 기준선은 단일 `{kind, primary_ref, secondary_ref}` target schema로 그 API 경계를 수정한 새 실행이다.

## 사례별 결과

| # | 사례 | 결과 | 직접 원인 |
|---:|---|---|---|
| 1 | `clean` | model output FAIL | `ac_001` statement citation 누락 또는 중복 |
| 2 | `bad` | semantic FAIL | 기대 결함은 탐지했으나 AC bool 2개 오류와 과잉 finding 1개 |
| 3 | `wrong-goal` | semantic FAIL | 기대 결함은 탐지했으나 AC bool 2개 오류 |
| 4 | `combined` | semantic FAIL | `ac_004 × val_goal_independent_unittest`를 false로 오판 |
| 5 | `boundary-clean` | model output FAIL | `ac_001` statement citation 누락 또는 중복 |
| 6 | `missing-link` | model output FAIL | `ac_001` statement citation 누락 또는 중복 |
| 7 | `future-result` | model output FAIL | finding target에서 파생되는 영향 Task와 직접 `affected_task_refs` 불일치 |
| 8 | `stored-expanded` | model output FAIL | `ac_001` statement citation 누락 또는 중복 |
| 9 | `semantic-explicit` | semantic FAIL | 28개 bool은 일치했으나 과잉 finding 1개 |
| 10 | `stored-multi-defect` | model output FAIL | `ac_001` statement citation 누락 또는 중복 |
| 11 | `semantic-missing-link` | PASS | 기대 finding·bool·target·파생 evidence 일치 |

직접 citation 실패 5건은 모두 같은 compiler 경계에서 발생했다. 과거 응답에 citation을 채워 PASS로 바꾸지 않았고 각 사례를 `model_output`으로 보존했다. `future-result`도 adapter가 영향 Task를 고쳐 주지 않아 실패로 남겼다. 의미 FAIL 4건은 compiler 통과 후 고정 기대표와 비교한 결과다.

## v1 대비 사용량

| 지표 | v1 | v2 첫 완전 기준선 | 변화 |
|---|---:|---:|---:|
| input tokens | 594,343 | 475,338 | -119,005 (-20.02%) |
| cached input tokens | 153,472 | 64,768 | -88,704 (-57.80%) |
| output tokens | 175,746 | 159,132 | -16,614 (-9.45%) |
| reasoning tokens | 92,928 | 114,348 | +21,420 (+23.05%) |
| total tokens | 770,089 | 634,470 | -135,619 (-17.61%) |
| latency | 3,230,096 ms | 2,920,154 ms | -309,942 ms (-9.60%) |
| provider duration | 3,218,655 ms | 2,907,916 ms | -310,739 ms (-9.65%) |
| PASS | 7/11 | 1/11 | -6 cases |

reasoning token은 output token에 포함된 provider 관측값이다. provider는 청구 금액을 제공하지 않았으므로 비용을 통화로 환산하지 않는다.

## 다음 계약 경계

- citation의 source·selector·quote 후보는 요청 전에 고정 catalog로 만들고 request binding에 결속한다.
- 모델은 mechanism·scope·AC·finding에서 직접 근거로 판단한 catalog ID만 선택한다.
- Plan·Goal의 statement/validation intent처럼 ID로 유일하게 정해지는 citation과 row closure는 adapter가 계산한다.
- `affected_task_refs`는 모델이 선택한 validation·scope·constraint·Task target에서 계산한다. `other` finding이 특정 Task에 영향을 준다고 판단하면 모델이 Task target을 명시한다.
- AC×validation bool, scope status, finding 유무·종류·typed target, `other`의 gate·severity와 복구 가능성은 모델의 의미 판단으로 유지한다.
- 수정 뒤에는 새 고정 worktree와 새 static 11 run을 사용한다. 이 기준선의 checkpoint·응답·기대표는 합격 evidence로 재사용하지 않는다.

Functional Alpha와 1.0 cutover는 계속 NO-GO다.
