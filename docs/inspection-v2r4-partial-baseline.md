# Plan inspection v2 4차 부분 실행 기준선

## 판정

커밋 `12e496ad89bde703d4ff5898470dbbd0e9dd4398`의 고정 worktree `D:\codex\fm-inspection-v2r4`에서 static 11을 실행했다. 실행은 clean·bad·wrong-goal의 provider 응답과 의미 평가를 완료한 뒤 네 번째 combined turn이 900,125ms에 timeout되어 중단됐다. 완료된 세 사례는 schema·compiler·request binding을 모두 통과했고 schema recovery는 0회였다.

| 사례 | 결과 | 직접 관측 |
|---|---|---|
| clean | semantic FAIL | 기대 false인 `ac_002→val_task_validator_review`, `ac_004→val_task_scope_preservation`, `ac_004→val_task_unittest`를 true로 제출 |
| bad | PASS | 고정 결함 `TASK_ORACLE_PHASE_SCOPE_OVERCLAIM`과 AC 관계 기대값 일치 |
| wrong-goal | PASS | 고정 결함 `GOAL_ORACLE_PHASE_SCOPE_MISMATCH`와 AC 관계 기대값 일치 |
| combined | external_unknown | turn 시작 receipt 뒤 terminal·result·usage 없음 |
| 나머지 7사례 | NOT_RUN | 외부 효과 완료 여부가 불명확해 전체 실행 중단 |

전체 summary는 `FAIL`, `collection_complete=false`, logical/provider turn 4/4, 성공 응답 3, `external_unknown` 1이다. summary SHA-256은 `7764370a8f99e42a4006485669bb26919a0edebfcfed42d592ffe83134ff9f54`다. artifact root는 `D:\codex\fm-inspection-v2r4\.flowmarshal-engine-eval\runs\inspection-v2r4-static11-20260906`이다.

## 구조 관측

4차 계약은 AC의 양의 연결을 각 scope의 `criterion_refs`에 넣어 28행 전체 장부를 제거했다. 전체 행을 직접 쓰는 실패는 사라졌지만 scope가 검사 능력과 AC 요구 관계를 함께 담게 됐다.

| 사례 | scope 수 | 양의 criterion ref 수 | result artifact bytes | input/output/reasoning token |
|---|---:|---:|---:|---:|
| clean | 22 | 32 | 17,950 | 58,468 / 2,258 / 0 |
| bad | 17 | 23 | 17,404 | 50,199 / 10,715 / 8,569 |
| wrong-goal | 23 | 21 | 18,626 | 50,176 / 9,552 / 7,223 |

clean은 validation의 실제 능력 자체는 supported로 판정하면서 관련 Task·phase의 sibling 검사까지 같은 AC에 연결했다. 모델이 AC마다 scope를 세분하고 `criterion_refs`를 반복하면서, scope의 질문인 “이 검사가 무엇을 실제로 수행하는가”와 관계의 질문인 “Goal이 이 validation ID의 절차를 요구하는가”가 다시 결합됐다. clean의 22개 scope와 32개 관계 ref는 이 결합이 구조 축소로 이어지지 않았음을 보여 준다.

## timeout과 재시도 경계

combined의 미완료 thread는 `01a07356-daef-7942-9725-2040d7e3fac1`, turn은 `01a07356-de31-7821-b978-34701c4667e1`이다. 실행 종료 뒤 resume 없이 저장 결과를 관측했으나 JSON-RPC `-32600 thread not loaded`가 반환됐고 terminal·result·usage를 확인하지 못했다. 완료 여부를 추정하거나 같은 intent를 재실행하지 않고 `external_unknown`으로 보존했다.

이 실행만으로 4차 계약의 전체 정확도나 token/latency를 판정할 수 없다. 완료된 세 사례의 결과는 scope와 AC 의미 축을 분리하는 5차 계약의 직접 근거이며, 후속 실행은 새 request/schema digest와 새 고정 worktree에서 수행한다.
