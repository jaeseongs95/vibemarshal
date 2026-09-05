# Plan inspection v2 5차 독립 11사례 기준선

## 판정

커밋 `de16afb325f5ee5dfb007df502fbe93c972eb034`의 detached worktree `D:\codex\fm-inspection-v2r5`에서 11개 static 사례를 독립 호출했다. 결과는 **PASS 9, model output FAIL 1, semantic FAIL 1**이다. logical/provider turn은 11/11, schema recovery는 0, `external_unknown`과 provider terminal failure는 0이며 `collection_complete=true`다. 전체 판정과 cutover는 `FAIL`·`NO-GO`로 유지한다.

| 사례 | 판정 | scope / 양의 link | 직접 결과 |
|---|---|---:|---|
| clean | PASS | 9 / 14 | 관계 차이·finding 0 |
| bad | model output FAIL | 평가 전 | `validation_scope` target의 불필요한 secondary ref로 schema 후검증 실패 |
| wrong-goal | semantic FAIL | 10 / 12 | 의도 결함은 검출, AC 관계 false negative 2개 |
| combined | PASS | 11 / 14 | 관계 차이·finding 0 |
| boundary-clean | PASS | 8 / 10 | 관계 차이·finding 0 |
| missing-link | PASS | 7 / 10 | `ac003-task-oracle-link` 검출 |
| future-result | PASS | 7 / 10 | `worker-future-validator-result` 검출 |
| stored-expanded | PASS | 6 / 8 | `ac004-combined-task-link` 검출 |
| semantic-explicit | PASS | 9 / 15 | v2r3의 sibling 과잉 연결 3개 제거 |
| stored-multi-defect | PASS | 7 / 8 | 두 독립 결함 모두 검출 |
| semantic-missing-link | PASS | 7 / 15 | `ac001-explicit-semantic-link` 검출 |

artifact root는 `D:\codex\fm-inspection-v2r5\.flowmarshal-engine-eval\runs\inspection-v2r5-static11-20260906`이며 summary SHA-256은 `5bc29c6f99fb5cd45a971abd1028826c888fc1e5ba60401d079649904a217b17`다.

## 실행 결속

| 항목 | 값 |
|---|---|
| 고정 HEAD | `de16afb325f5ee5dfb007df502fbe93c972eb034` |
| 결정적 Gate | 5/5 PASS, contract `sha256:f8016844b24a240b5880589834379423ed00b98ef52690cdfddec2ebd3f63295` |
| preflight binding | `sha256:61ec031aabeb67695c53d9d5e8d0d0341ccd82a37696e8c3ec7031746a5bf67f` |
| prepare lock | `sha256:ce9e4d42571bfb1de05228a92f4f5b5e67883c30f4c48e8a8d972beb21b652f1` |
| source manifest | `sha256:7f44cf3a83e92c9cc56a767f8ee22f6f08f4407881b3372243b6ccbc27c46c8c` |
| output schema | `sha256:0068ca3c278cfd64a1b49fa509b3808b3a224bf1560849de558132ed24cd3d6a` |
| 모델·추론 | `gpt-5.6-sol/xhigh`, fallback 없음 |
| 정책 | `:danger-full-access`, `approval_policy=never` |

## 확인된 개선

v2r3은 4 PASS·7 semantic FAIL이었고 모든 실패가 AC 관계에 집중됐다. v2r5에서는 평가에 도달한 10건 중 wrong-goal을 제외한 9건의 고정 관계가 전부 일치했다. clean의 scope는 v2r4의 22개에서 9개로 줄었고, v2r3에서 세 sibling 검사를 과잉 연결했던 semantic-explicit도 통과했다. 전체 28행 false/true 장부를 직접 제출하지 않고 양의 link만 제출해도 missing-link와 semantic-missing-link의 실제 누락 finding을 놓치지 않았다.

총 input 550,373, cached input 22,016, output 104,651, reasoning 83,158, total 655,024 token이며 전체 latency는 2,025,940ms, provider duration은 2,013,906ms다. v2r3 대비 total token은 0.94% 감소했고 provider duration은 0.98% 증가했다. 정확도 개선은 확인됐지만 지연 개선 근거는 아니다.

## 남은 두 실패의 공통 경계

bad의 원시 응답은 `validation_scope` target에 `primary_ref=scope_task_oracle_unsupported_extended_calls`와 그 소유 validation ID를 `secondary_ref`로 함께 썼다. 의미 대상은 식별됐지만 모델이 target kind별 참조 개수를 다시 구성해야 하는 schema 때문에 전체 제출이 거부됐다. 이는 citation closure와 같은 기계 참조 작성이 target 표현에 남아 있다는 구조 문제다.

wrong-goal은 `goal-uses-task-phase` 결함을 정확히 검출하고 관련 scope도 supported/contradicted로 분리했다. 그러나 다음 두 필수 관계를 false로 제출했다.

- `ac_004 → val_goal_independent_behavior_contract`
- `ac_004 → val_goal_independent_unittest`

AC-004는 Task 검증과 별도의 독립 Goal Test에서 모든 동작·공개 계약·파일 보존 검사를 통과하도록 명시한다. 두 validation은 그 aggregate Goal Test의 실제 behavior/public-contract와 unittest 절차를 각각 수행한다. 모델은 같은 phase나 결과 주제만으로 sibling을 연결하지 말라는 경계를 과도하게 적용해 scope-preservation만 연결했다. 이는 특정 ID 암기 문제가 아니라, 명시된 aggregate validation phase의 구성 절차와 단순 동시 실행 sibling을 구분하는 일반 의미 규칙이 부족한 문제다.

다음 계약은 target의 kind·primary·secondary 조립을 없애고 모델이 고정 catalog ID 또는 직접 생성한 scope ID만 선택하도록 해야 한다. AC 관계 지침에는 Goal이 독립 Task/Goal validation phase와 그 안의 검사 책임을 명시한 경우, 그 phase에서 명시 책임을 실제 수행하는 각 validation을 연결하되 열거되지 않은 sibling은 확대하지 않는 규칙을 추가한다. v2r5 원본·기대표·summary는 수정하거나 재호출하지 않고 새 schema·request digest·고정 worktree에서 다시 전수 검증한다.
