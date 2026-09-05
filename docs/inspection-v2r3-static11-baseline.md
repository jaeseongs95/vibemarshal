# plan-inspection-v2 3차 구조 보정 독립 11사례 기준선

## 결론

의미 citation projection과 v2 전용 역할 지침을 적용한 고정 실행은 11/11 provider 호출을 모두 완료했고, 모든 응답이 strict schema·request/result binding·compiler를 통과했다. 결과는 PASS 4, semantic FAIL 7, model output FAIL 0, provider/recovery 실패 0이다. 이전 부분 실행에서 발생한 State·ProjectMap 기계 메타데이터 finding과 `stored-expanded` timeout은 재발하지 않았다.

남은 실패 7건은 AC×validation 관계에 집중됐다. 모델은 같은 validation의 원자 scope를 supported로 판정한 뒤에도 별도 28행 `ac_validation_rows`에서 해당 AC 연결을 빠뜨리거나, 반대로 sibling scope를 과잉 연결했다. 이는 모델이 의미 판단과 그 판단의 cross-product 장부 전개를 연속해서 다시 수행하는 구조에서 생긴 불일치다. 후속 계약은 원자 scope의 claim·status와 양의 AC 관계만 모델이 제출하고, 전체 true/false 행렬과 scope ID 목록은 adapter가 결정적으로 확장한다.

이 실행은 v2 승격이나 S06 재개 근거가 아니다. qualification은 NOT_RUN이고 cutover는 NO-GO다.

## 실행 결속

| 항목 | 값 |
|---|---|
| run root | `D:\codex\fm-inspection-v2r3\.flowmarshal-engine-eval\runs\inspection-v2r3-static11-20260906` |
| 고정 source commit | `2fd29ad8079eb582ed968fd89bba6cb031f34f97` |
| source manifest | `sha256:0fa613bf07ee264c26ad08cfa4f8ed4777de98475553ce89478269f1411d655d` |
| fixture package manifest | `sha256:091bde16cb39ac26ee66df7e4fd30a54388443ef48088fa661fd0138fb6f0866` |
| 결정적 Gate 계약 | `sha256:d1f880d05e0fb34989a111fab07ef295dd6e3e3ccb5756903f3c8db43b50a4ad` |
| workspace preflight binding | `sha256:69b766e0ad0d5ef5fc2b954dd3c322e560dc823d53da8384277b875c031e1b2d` |
| prepare lock | `sha256:29fae98241b1e6ed9125cdfa38c427ed2aba81d5564f4117e5d6c5aaaee3b9eb` |
| model lock | `sha256:9cb6e554b5ffbab137cbfe643f8e3d67c0b81f3963dd26fcf33f08d1cde5a005` |
| provider manifest | `sha256:8bdb9abbf5ddb904605e83ed0f1f9650007dc5d7d178ce1172685cee5e601892` |
| 역할 | `gpt-5.6-sol`, `xhigh`, fallback 없음 |
| 실제 권한 | `:danger-full-access`, `approval_policy=never` |
| logical/provider/recovery | 11 / 11 / 0 |
| summary SHA-256 | `2986ef6b9c3ae1841036b2e494e8f70af0e31020e4baff4afb6e01f557e000c9` |

같은 고정 worktree에서 실행 전 전체 671개 테스트와 결정적 Gate 5/5가 통과했다. 실행 뒤 모든 11건의 common/output binding과 `source_unchanged`, `workspace_unchanged`, `instructions_unchanged`, `isolated_inputs_unchanged`, `preserved_originals`가 참이었다. 새 원장 쓰기, Plan 활성화, Worker 실행은 없었다.

## 사례별 결과

| # | 사례 | 결과 | 직접 관측 |
|---:|---|---|---|
| 1 | `clean` | semantic FAIL | finding 오류는 없었다. AC 관계 4개를 누락했다: AC-001 Goal unittest, AC-003 Task oracle·Goal oracle·Goal unittest. |
| 2 | `bad` | semantic FAIL | 의도한 Task phase 과장 결함은 정확히 탐지했다. clean의 4개 누락에 AC-004 Goal unittest 누락이 더해졌다. |
| 3 | `wrong-goal` | semantic FAIL | 의도한 Goal phase 결함은 정확히 탐지했다. AC-004 Goal unittest 연결 1개를 누락했다. |
| 4 | `combined` | PASS | 고정 관계표와 finding 기대를 모두 만족했다. |
| 5 | `boundary-clean` | PASS | 20개 관계와 무결함 branch를 만족했다. 이전의 State·ProjectMap semantic finding은 사라졌다. |
| 6 | `missing-link` | semantic FAIL | AC-003 Task oracle의 필수 관계를 false로 판정해 의도한 missing-link finding도 제출하지 않았다. |
| 7 | `future-result` | PASS | 미래 Validator 결과의 선행 요구를 올바른 typed target으로 탐지했다. |
| 8 | `stored-expanded` | semantic FAIL | 정상 완료했고 의도한 AC-004 결함을 탐지했다. AC-003 Goal Test 관계 1개를 누락했다. |
| 9 | `semantic-explicit` | semantic FAIL | AC-002 Task Validator, AC-004 Task scope·Task unittest의 세 sibling 관계를 과잉 연결했다. |
| 10 | `stored-multi-defect` | PASS | 두 독립 결함과 전체 관계표를 모두 만족했다. |
| 11 | `semantic-missing-link` | semantic FAIL | 의도한 semantic 연결 누락 finding은 탐지했다. AC-003 Task oracle·Goal oracle 관계 2개를 누락했다. |

모든 사례가 semantic evaluator까지 도달했다. 기대 finding을 탐지한 사례의 추가 finding은 없었고, clean에도 예상 밖 finding이 없었다. 따라서 남은 주된 오차축은 finding 참조 장부가 아니라 필수 AC 관계의 누락·과잉이다.

## 사용량과 지연

전체 실측은 input 554,281, cached input 21,632, output 106,973, reasoning 82,320, total 661,254 token이다. reasoning token은 output token에 포함된 provider 관측값이다. 전체 latency는 2,006,438ms, provider duration 합계는 1,994,442ms다. 청구 금액은 receipt가 제공하지 않아 계산하지 않았다.

| 사례 | input | cached | output | reasoning | provider duration |
|---|---:|---:|---:|---:|---:|
| `clean` | 50,925 | 0 | 9,475 | 7,067 | 174,581ms |
| `bad` | 50,944 | 0 | 10,479 | 7,816 | 193,333ms |
| `wrong-goal` | 50,916 | 0 | 11,884 | 9,547 | 218,691ms |
| `combined` | 51,081 | 0 | 11,385 | 8,847 | 218,190ms |
| `boundary-clean` | 49,912 | 0 | 9,707 | 7,566 | 179,626ms |
| `missing-link` | 49,908 | 0 | 9,738 | 7,648 | 183,707ms |
| `future-result` | 49,870 | 0 | 6,885 | 5,142 | 128,827ms |
| `stored-expanded` | 49,450 | 0 | 6,713 | 5,107 | 124,834ms |
| `semantic-explicit` | 51,023 | 0 | 11,406 | 8,777 | 213,312ms |
| `stored-multi-defect` | 49,236 | 21,632 | 7,453 | 5,819 | 142,466ms |
| `semantic-missing-link` | 51,016 | 0 | 11,848 | 8,984 | 216,875ms |

v2r2의 완료된 7건은 사례별 input이 64,130~66,378 token이었고 `stored-expanded`가 900초 timeout됐다. v2r3은 49,236~51,081 input token으로 11건을 끝냈으며 `stored-expanded`도 124,834ms provider duration으로 완료했다. 이는 semantic projection과 v2 전용 지침 분리의 입력·완결성 개선을 보여 준다. 관계 정확도는 별도 구조 보정이 필요하다.

## 후속 계약의 수용 기준

다음 보정은 개별 오답 문구를 사례별로 추가하는 방식이 아니라 제출 구조를 바꾼다.

- 모델은 각 validation의 복합 책임을 원자 scope로 나누고, 각 scope의 실제 검사 절차·주장을 `claim`으로 명시한다.
- supported scope에는 그 절차를 직접 요구하는 AC의 양의 `criterion_refs`만 둔다. contradicted·unresolved scope에는 AC ref를 둘 수 없다.
- adapter는 이 양의 관계를 고정 순서의 모든 AC×validation 조합으로 확장하고, `ac_link_required`, `scope_ids`, AC·validation·scope citation closure와 coverage membership witness를 생성한다.
- evaluator는 모델 원문에 더 이상 존재하지 않는 전체 행렬 대신 compiled decision 28개를 기존 동결 기대표와 대조한다.
- v1 raw·schema·validator·evaluator와 본 v2r3 artifact는 수정하거나 새 결과로 덮어쓰지 않는다.

이 변경 뒤 새 고정 worktree에서 static 11을 다시 전수 실행한다. 그 결과가 합격해야 qualification 13으로 진행하고, qualification 합격 뒤에만 실제 Goal의 Plan 후보·정확한 revision/digest 활성화·Task 실행·독립 validation·GoalVerdict 경로를 수행한다.
