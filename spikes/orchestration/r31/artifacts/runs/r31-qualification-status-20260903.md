# FlowMarshal Planner R3.1 qualification 상태

## 결론

- 현재 판정: **프로토타입 구현 완료 / GO 보류**
- 결정적 pipeline smoke: **PASS**
- 실제 모델 단일 시나리오 smoke: **PASS**
- 대표 역할 fixture probe: **PASS**
- 전체 catalog 역할 probe: **IN_PROGRESS — 1/150 checkpoint cells**
- candidate generator 전체 pipeline 반복과 독립 forward 복원: **NOT_RUN**
- R4 Assigner 시작 조건: **미충족**

부분 smoke와 대표 probe는 실제 역할 연결이 동작하고 평가기가 주요 결함을 구분한다는 증거다. 전체 역할 campaign은 재개 가능한 형태로 시작했지만 아직 149개 cell이 남았다. 전체 catalog의 생성·검토·순서 변형·독립 복원 합격선을 대신하지 않으므로 R3.1을 `GO`로 올리지 않는다.

## 통과한 증거

### 1. 결정적 통합 smoke

- 보고서: [r31-smoke-report.md](r31-integration-final-20260902/r31-smoke-report.md)
- receipt: [r31-smoke-receipt.json](r31-integration-final-20260902/r31-smoke-receipt.json)
- 결과: 같은 Mission의 후보 검색, Hard-before-soft, diversity, 제한 정제, 선택과 단일 PlanDraft export 통과
- Core 계획 활성화: `false`

### 2. 실제 모델 단일 시나리오 smoke

- 보고서: [r31-live-smoke-report.md](r31-live-pilot14-20260903/r31-live-smoke-report.md)
- receipt: [r31-live-smoke-receipt.json](r31-live-pilot14-20260903/r31-live-smoke-receipt.json)
- telemetry 보정: [r31-live-smoke-telemetry-correction.json](r31-live-pilot14-20260903/r31-live-smoke-telemetry-correction.json)
- 실제 model call: 14회
- schema recovery: 0회
- 후보 version: 4개
- 선택 digest: `sha256:c0041d7ba14fcce7df2cbbddb3ba5401f5118e1aa39d3942c5ccb95bb1f488a5`
- exported PlanDraft digest: `sha256:578ac6d99903142f59582bd90329b81c14316845d943b245693c7c3757322865`
- Core 계획 활성화: `false`
- 실제 권한 증거: 모든 역할 thread가 `:danger-full-access`, `approval_policy=never`
- 원본 보고서의 token 합계 `0`은 중첩 usage 필드를 읽지 못한 telemetry 결함이다. 원본 receipt는 수정하지 않고 source SHA-256에 결속된 sidecar로 457,098 token을 보정했으며 semantic 판정에는 영향이 없다.

### 3. 대표 역할 fixture probe

- 재평가 보고서: [r31-role-fixture-reevaluation.json](r31-role-eval-pilot2-20260903/r31-role-fixture-reevaluation.json)
- 범위: 사용자 요청이 ProjectProfile을 덮는 목적 fixture와 literal secret을 포함한 계획 fixture, seed 1
- 실제 model call: 3회
- 결과: requirement·exclusion recall 100%, major defect recall·precision 100%, structural recall 100%, false admission 0, 첫 출력 schema 준수 100%
- 합계: 63,741 token, 58,420 ms model latency
- 이 결과는 선택된 2개 fixture의 부분 probe이며 전체 catalog 판정이 아니다.

### 4. 현재 평가 입력 계약

- 입력: [forward-eval-inputs.json](r31-eval-contract-v3-20260903/forward-eval-inputs.json)
- catalog: 목적 해석 26개 + 계획 품질 24개 = 50개 fixture
- 순서 seed: 1, 2, 3
- catalog digest: `sha256:68be20c5c11b66e0c0b4eea45a23e006f2e70dda5cb98f40856e6d85980dd717`
- 모델 노출에서 숨은 oracle, 내부 `case_id`, variant suffix와 `mutant` 표시가 제외됨
- 후보 배열도 seed별로 순서를 바꾸며 각 fixture는 opaque `case_ref`와 구체적인 profile 또는 계획 artifact를 제공함

### 5. 전체 역할 평가 campaign

- 상태: [campaign 2 상태](r31-role-eval-campaign2-20260903/campaign-status.md)
- 진행: 150개 cell 중 1개 완료, 149개 남음
- 완료 cell: `P11-clean`, seed 1
- 결과: requirement·후보 다양성·중복 방지·독립 복원·evidence 정직성 통과
- schema: 첫 출력 실패 후 1회 recovery로 성공
- 사용량: 53,341 token, 22,274 ms model latency
- 계정 한도: 2026-09-03 관측 시 주간 사용량 98%, 추가 크레딧 없음
- 현재 reset 관측값: 2026-09-07 11:27:31 KST

campaign은 cell별 원시 assessment·observation·model-call receipt·권한 증거를 저장한다. manifest는 fixture catalog와 prompt·output schema 계약을, model lock은 역할별 model·effort·inventory를 결속한다. 사용량 한도 중단은 완료 cell로 세지 않고 `--resume`에서 같은 계약의 완료 cell만 재사용한다.

## 남은 GO Gate

1. 전체 50개 fixture를 seed 1·2·3으로 실제 역할 모델에 실행한다. 현재 첫 cell을 완료했으며 149개 cell이 남았다.
   - Mission 26개 × 3 seeds × 제안·독립 검토 2회 = 156 calls
   - Plan 24개 × 3 seeds × 위험도별 reviewer 1회 = 72 calls
   - 합계 228 calls
2. 같은 catalog에서 실제 candidate generator를 포함한 전체 검색 pipeline의 다양성, false admission, 실패 격리와 선택 일관성을 검증한다.
3. 선택된 PlanDraft만 받은 독립 세션이 Mission·요구·제외·dependency·validation·failure policy를 100% 복원하는지 검증한다.
4. 전체 합격선과 기존 회귀 검사를 함께 판정한다.

전체 역할 probe는 비용이 큰 작업이므로 CLI가 `--full-catalog --order-seeds 1,2,3`을 명시적으로 요구한다. 중단된 동일 campaign은 `--resume`으로만 이어간다. 역할 probe 하나만 통과해도 2·3번을 생략할 수 없다.

## 구현 검증

- `python -m unittest discover -s tests -q`: 285 tests `PASS`
- `python -m compileall -q src tests`: `PASS`
- Planner 스킬 quick validation: `PASS`
