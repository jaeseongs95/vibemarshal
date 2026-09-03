# FlowMarshal Planner R3.1 결정적 smoke 보고서

- 판정: **PASS**
- 실행 모드: `deterministic_fixture`
- 실제 모델 forward qualification: **NOT_RUN**
- PlanningRun: `planning_run_1fb1993cd5c24f3788fd7a110ea7b694`
- Mission: `feature_extension`
- 후보 version 수: 5
- Diversity Top-K: `candidate.adapter.boundary`, `candidate.staged.rollout`
- 추천 후보: `candidate.adapter.boundary`
- Core 계획 활성화: `false`

## 결론

R3의 `RequestSpec`과 `PlanDraft`를 유지한 채 R3.1 sidecar가 같은 Mission의 후보를 생성하고, Hard Gate 통과 후보만 비교·정제한 뒤 완전한 계획 하나를 export했다. 이 smoke는 Core를 활성화하지 않는다.

이 결과는 결정적 fixture 통합 검사의 증거이며 실제 모델 품질 검증을 대체하지 않는다. R3.1 `GO`에는 별도의 live forward qualification이 필요하다.

## 결속 digest

- planning input: `sha256:c5e83a741f3f1f3247ce45ba2d988a7b329d1c3e02d92851fe235d33372a2988`
- selection: `sha256:e9e78b02026e6caa2f15287f27ada9f2a6dbdd2d644b47964ab4fb51e3480241`
- exported PlanDraft: `sha256:7621ad7cf7d1add0aad9eb2b0d9b2c30331d1f4c29bf962687ad8db4efe4ac35`
- export 경로: `D:\codex\flowmarshal\spikes\orchestration\r31\artifacts\runs\r31-integration-final-20260902\runs\run-679f00285495734dc7d5cc2461b5acb34ab63a17\exports\e9e78b02026e6caa2f15287f27ada9f2\plan-draft.json`
