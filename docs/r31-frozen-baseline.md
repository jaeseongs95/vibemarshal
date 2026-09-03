# R3.1 최종 동결 기준선

## 판정

R3.1 목적 기반 다중 후보 Planner는 campaign 10 완료 결과 **FAIL**로 동결한다. 추가 보정이나 동일 campaign 재실행으로 `GO`를 만들지 않는다.

원본 권위 artifact는 다음 파일이다.

- `spikes/orchestration/r31/artifacts/runs/r31-role-eval-campaign10-20260903/r31-role-fixture-evaluation.json`
- `spikes/orchestration/r31/artifacts/runs/r31-role-eval-campaign10-20260903/campaign-manifest.json`
- `spikes/orchestration/r31/artifacts/runs/r31-role-eval-campaign10-20260903/model-lock.json`

위 파일과 기존 R1~R3.1 source는 수정·삭제하지 않는다.

## 완료 수치

| 항목 | 최종 값 |
|---|---:|
| evaluation cell | 150 / 150 |
| 남은 cell | 0 |
| 실제 모델 호출 | 228회 |
| 성공 | 222회 |
| schema recovery 성공 | 5회 |
| 최종 실패 | 1회 |
| input tokens | 5,096,120 |
| cached input tokens | 3,729,408 |
| output tokens | 98,646 |
| reasoning output tokens | 52,687 |
| total tokens | 5,194,766 |
| cached input / input | 73.18% |
| 누적 latency | 2,699,032ms |
| 최종 상태 | FAIL |

이 수치는 원본 JSON의 top-level cell count·status, `model_call_receipts`와 `evaluation_report`에서 계산했다. 과거 README와 qualification 문서에 남은 `1/150 진행 중` 표기는 campaign 중간 시점 기록이며 현재 상태로 사용하지 않는다.

## 실패의 의미

실패는 sandbox나 승인 자료 접근 문제가 아니라 Planner reviewer 계약의 품질 문제에 집중됐다.

1. Reviewer가 직접 증거보다 많은 상관 결함을 붙여 precision을 낮췄다.
2. 핵심 결함을 finding으로 제출하고도 일부 후보를 `admissible`로 판정하는 모순이 있었다.
3. clean 후보를 과잉 차단했다.
4. 최초 출력과 한 번의 복구 뒤에도 schema 검증에 실패한 사례가 있었다.

R3.1 역할 fixture probe 자체도 candidate generator 전체 pipeline, 순서 변형과 독립 forward 복원을 단독으로 대체하지 않는다. 따라서 150/150을 실행했다는 사실은 `GO`의 근거가 아니다.

## 새 Engine으로 이관한 회귀 기준

`tests/fixtures/engine/r31-reviewer-regressions.json`에 다음을 provenance와 함께 복사했다.

- `P11`: 결함 탐지와 admission이 모순되는 사례
- `P01`: clean plan 과잉 진단·오차단
- `P05`: schema 복구 실패
- `P03`, `P07`, `P08`, `P09`, `P12`: 한 증상에서 상관 결함을 과도하게 파생한 사례

새 Engine은 Reviewer가 권위 상태를 출력하지 못하게 하고 Core가 status와 score를 결정적으로 계산한다. 회귀 fixture의 oracle과 variant label은 모델 입력에서 제거하고 opaque case ref만 전달한다.

