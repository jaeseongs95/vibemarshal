# plan-inspection-v2 2차 구조 보정 부분 실행 기록

## 결론

두 번째 구조 보정본은 완료된 7개 응답 모두에서 schema·compiler를 통과했다. 첫 완전 v2 실행에서 11건 중 6건을 막았던 citation 객체·영향 Task 중복 장부 실패는 관측 구간에서 재발하지 않았다. 따라서 adapter 참조 전개의 구조 효과는 확인됐다.

하지만 여덟 번째 `stored-expanded` 호출이 900초 timeout으로 종료돼 전체 실행은 완결되지 않았다. 결과는 PASS 1, semantic FAIL 6, `external_unknown` 1, NOT_RUN 3이다. 미완료 turn은 ephemeral thread였고 종료 뒤 provider에서 다시 읽을 수 없었으므로 완료 여부를 확인하거나 재개하지 않았다. 이 실행은 static 11 합격 evidence나 S06 진입 근거로 사용하지 않는다.

## 실행 결속

| 항목 | 값 |
|---|---|
| run root | `D:\codex\fm-inspection-v2r2\.flowmarshal-engine-eval\runs\inspection-v2r2-static11-20260906` |
| 고정 source commit | `19055d7116c75b069ef6129ebe3cda70f3877eab` |
| source manifest | `sha256:fe2d4b316d3a4f763980dd9616ada12d4d90931d228b0a4ee1c814807eaffe3c` |
| fixture package manifest | `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| workspace preflight binding | `sha256:9fb220e8b80cc2dd1b0eed0e454ab2403623ec9adacfd9b5bdfd3e82a1512c34` |
| prepare lock | `sha256:3b863d3e697329f6f40f3d9668ad1e11ff4435aa59e3e9c4f7dba165fa93e45b` |
| model lock | `sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005` |
| 역할 | `gpt-5.6-sol`, `xhigh`, fallback 없음 |
| 실제 권한 | `:danger-full-access`, `approval_policy=never` |
| logical/provider/recovery | 8 / 8 / 0 |
| summary SHA-256 | `fca4248d5dc3b38dc6f7bd46bf5bfc3cc0cedf8576f671aebaea563f3e3cc497` |

같은 고정 worktree에서 실행 전 전체 671개 테스트와 결정적 Gate 5/5가 통과했다. 실행 뒤 `source_unchanged`, `workspace_unchanged`, `instructions_unchanged`, `isolated_inputs_unchanged`, `preserved_originals`도 모두 참이었다.

## 사례별 결과

| # | 사례 | 결과 | 직접 관측 |
|---:|---|---|---|
| 1 | `clean` | semantic FAIL | `ac_004`가 별도 Task unittest·scope 검사까지 요구한다고 2개 관계를 과잉 판정했고 Validator의 test evidence 누락 finding을 추가했다. |
| 2 | `bad` | semantic FAIL | 기대한 task-phase 과장 결함은 탐지했다. AC 관계 3개가 달랐고 Validator test evidence 누락 finding을 추가했다. |
| 3 | `wrong-goal` | semantic FAIL | 기대한 Goal phase 결함은 탐지했다. `ac_004` 관계 2개를 과잉 판정했다. |
| 4 | `combined` | PASS | 고정 관계표와 finding 기대를 모두 만족했다. |
| 5 | `boundary-clean` | semantic FAIL | 20개 관계는 모두 맞았지만 `STATE_PROJECT_MAP_BINDING_MISMATCH`를 추가했다. |
| 6 | `missing-link` | semantic FAIL | 기대한 누락 연결을 탐지하고 20개 관계를 모두 맞췄지만 같은 State·ProjectMap finding을 추가했다. |
| 7 | `future-result` | semantic FAIL | 미래 Validator 결과 선행 요구를 올바른 typed target으로 탐지하고 관계를 모두 맞췄지만 같은 State·ProjectMap finding을 추가했다. |
| 8 | `stored-expanded` | `external_unknown` | provider turn이 900초에 timeout됐다. `result.json`, `terminal.json`, output binding이 없었다. |
| 9 | `semantic-explicit` | NOT_RUN | 앞선 `external_unknown`으로 중단했다. |
| 10 | `stored-multi-defect` | NOT_RUN | 앞선 `external_unknown`으로 중단했다. |
| 11 | `semantic-missing-link` | NOT_RUN | 앞선 `external_unknown`으로 중단했다. |

완료된 7건은 모두 모델 출력 형식과 adapter 입력 결속을 통과했다. 과거 응답을 보정하거나 누락 값을 채워 PASS로 바꾸지 않았다.

## 부분 사용량

여덟 번째 turn의 terminal usage를 유일하게 귀속할 수 없어 실행 전체 token 합계는 `unavailable`이다. 완료된 앞 7건만 합산하면 input 457,492, output 79,289, reasoning 63,515, total 536,781 token이며 provider duration은 1,486,571ms다. reasoning token은 output token에 포함된 provider 관측값이다. 전체 wall latency는 timeout을 포함해 2,394,843ms다.

| 사례 | input | output | reasoning | provider duration |
|---|---:|---:|---:|---:|
| `clean` | 66,208 | 14,536 | 12,171 | 277,738ms |
| `bad` | 66,227 | 11,959 | 9,580 | 229,871ms |
| `wrong-goal` | 66,200 | 12,207 | 9,840 | 224,048ms |
| `combined` | 66,378 | 10,346 | 7,852 | 190,894ms |
| `boundary-clean` | 64,193 | 9,141 | 7,064 | 169,240ms |
| `missing-link` | 64,130 | 7,623 | 5,555 | 140,633ms |
| `future-result` | 64,156 | 13,477 | 11,453 | 254,147ms |

## timeout 복구 판정

`stored-expanded`의 thread는 `01a072f1-fec5-73c1-850d-cf134fbf8866`, turn은 `01a072f2-027f-79c1-83e5-4f847c12914d`다. intent는 `ephemeral=true`였고 종료 뒤 App Server의 stored read는 `thread not loaded`를 반환했다. provider 완료를 관측할 receipt가 없으므로 새 호출이나 동일 run 재개를 하지 않고 `external_unknown`으로 보존했다.

## 새로 확인한 구조 원인과 후속 경계

보존된 `request.json`과 `thread.intent.json`을 대조한 결과, v2 역할 지침에는 v1의 `citations`·`claim_ref`·`basis_refs` 작성 규칙과 v2의 해당 장부 작성 금지가 함께 들어 있었다. 또한 citation catalog가 Goal source trace, State·ProjectMap의 ID·digest·path, Plan의 모든 문자열 leaf를 후보화했다. 첫 호출 기준 catalog는 334개·67,857자였고 전체 request는 129,360자였다. 모델 input은 사례별 64,130~66,378 token이었다.

후속 보정은 다음 경계를 따른다.

- v1 prompt와 계약은 그대로 보존한다.
- v2에는 별도 trace 지침을 사용해 모델이 의미 판단 필드와 직접 evidence ID만 제출하게 한다.
- State·ProjectMap revision/digest/root/freshness 결속은 기존 Core·preflight의 결정적 검사 책임으로 유지하고 semantic citation 후보에서 제외한다.
- Goal에서는 사용자 요청·outcome·AC·constraint·preference·assumption·effect 의미를, Plan과 Skeleton에서는 목적·입출력·완료 조건·검사 문장·효과 의미를 선택적으로 투영한다. source trace와 ID·digest 장부는 제외한다.
- AC 관계 bool, scope status, finding 유무·종류·target과 직접 근거 선택은 모델 판단으로 계속 보존한다.

동일 clean 입력에 후속 코드를 적용한 결정적 request 표본은 catalog 86개·21,035자, 전체 request 74,789자다. 보존 실행 대비 catalog 문자는 69.00%, request 문자는 42.19% 줄었고 v1 장부 필드 지침은 v2 요청에 남지 않았다. 이는 provider 실행 전의 구조 지표이며 실제 token·정확도 개선은 새 고정 worktree의 별도 static 11로 판정한다.
