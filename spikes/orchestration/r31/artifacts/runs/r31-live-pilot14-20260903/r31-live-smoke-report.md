# FlowMarshal Planner R3.1 실제 모델 smoke 보고서

- 판정: **PASS**
- qualification 범위: **PARTIAL_SINGLE_SCENARIO**
- 역할 설정: `r31-forward-hypothesis-20260902-v1`
- 역할 설정 digest: `sha256:f93d5961415647b8f82340725f4a340408d9e6f01b22a8f8b9b79c5c49ce448a`
- model inventory digest: `sha256:5cbce375478c3583d6c4453d65d391fbfdc3a76747740a1b919eabbde1457cd3`
- 실제 model call: 14회
- schema recovery: 0회
- 합계 token: 0
- 합계 model latency: 1017153 ms
- 후보 version: 4개
- 추천 후보: `candidate-001`
- Core 계획 활성화: `false`

## 실제 역할 배정

- `candidate_generator`: `gpt-5.6-luna` / `medium`
- `critical_reviewer`: `gpt-5.6-sol` / `xhigh`
- `hard_gate_reviewer`: `gpt-5.6-terra` / `high`
- `intent_reviewer`: `gpt-5.6-terra` / `high`
- `purpose_resolver`: `gpt-5.6-luna` / `medium`
- `scorer_selector`: `gpt-5.6-terra` / `high`

## 판정 범위

이 실행은 실제 모델로 Mission 제안·독립 intent review·요구 추출·독립 review·후보 생성·Hard Gate·Top-K walkthrough·정제·단일 PlanDraft export를 관통한다. Core 활성화나 WorkItem 실행은 하지 않는다.

단일 시나리오 smoke이므로 전체 50개 fixture, 순서 변형과 독립 복원 평가를 대체하지 않는다. 이 결과만으로 R3.1을 GO로 선언하지 않는다.

## 결속 digest

- planning input: `sha256:3aada06b6f293917656ebcc15e33f6c1e5b323f04cb82969b1b1e3a9214ae0b2`
- selection: `sha256:c0041d7ba14fcce7df2cbbddb3ba5401f5118e1aa39d3942c5ccb95bb1f488a5`
- exported PlanDraft: `sha256:578ac6d99903142f59582bd90329b81c14316845d943b245693c7c3757322865`
- export 경로: `D:\codex\flowmarshal\spikes\orchestration\r31\artifacts\runs\r31-live-pilot14-20260903\runs\run-bb304beb379589ad723f070383a0c5625e1d75c6\exports\c0041d7ba14fcce7df2cbbddb3ba5401\plan-draft.json`
