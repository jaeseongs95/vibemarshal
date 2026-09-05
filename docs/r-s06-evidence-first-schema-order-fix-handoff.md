# R-S06 근거 우선 strict schema 순서 보정 인계

기준일: 2026-09-05 KST. 대상 저장소: `D:\codex\flowmarshal`. 시작 기준은 `HEAD=main=origin/main=a72880070a2a6ea9992a2da6019538d8e9009939`, clean 작업 트리였다.

## 보정 결과

`strict_json_output_schema()`는 최초 schema의 `properties` 선언 순서를 보존하고 같은 순서로 전체 `required`를 만든다. strict 변환을 마친 schema는 `required` 배열이 완전한 순서 기록이므로, canonical JSON 저장이 object key를 정렬한 뒤 재로드해도 같은 `properties`·`required` 순서와 strict schema digest를 재구성한다. `make_role_request()`는 이 완성된 strict schema를 request에 넣어 저장 왕복 경계를 보존한다. `default` 제거, 전체 필수화, 모든 object의 `additionalProperties=false`, 중첩 object·array·union과 순서 의미 배열 계약은 유지한다.

Plan 검사 envelope의 실제 전송 순서는 `citations → validation_rows → validation_scope_rows → ac_validation_rows`다. 각 AC 행은 `criterion_id → validation_id → basis_refs → scope_ids → ac_link_required → finding_codes` 순서로 전송하여 대상과 근거·scope가 boolean 판정보다 먼저 온다. 공통 역할 지침도 복합 검사 scope, AC가 직접 요구한 절차 또는 특정 도구·phase, 전역 constraint의 Task 검사 의무를 이 순서로 분리했다. 사례 ID나 기대 정답은 provider prompt에 추가하지 않았다.

권위 문서와 `AGENTS.md`에는 선언 순서 보존과 근거 우선 출력 계약만 동기화했다. 고정 기대표·evaluator·oracle·threshold·taxonomy, Core 판정, adapter의 구조 검사 의미와 제품 Reviewer의 `gpt-5.6-terra/high` 설정은 변경하지 않았다.

## 결정적 회귀

- 최초 schema → strict 변환 → canonical key 정렬 저장 → 재로드 → strict 재구성에서 root·nested object의 property/required 순서와 digest가 같다.
- `default` 제거, 전체 required, `additionalProperties=false`, `prefixItems`·`enum`·`anyOf`·`oneOf` 순서와 union 안의 object·array item object 순서를 직접 검사한다.
- 실제 Plan Reviewer strict schema에서 근거 envelope 행이 AC 행보다 먼저이고, AC 행의 `basis_refs`·`scope_ids`가 `ac_link_required`보다 앞선다.
- request → strict artifact → turn intent → receipt schema digest와 terminal/result 결속의 단독·복합 변조 차단을 기존 회귀와 함께 통과한다.
- 합성 양성 회귀는 잘못된 task phase 능력의 별도 scope finding과 `ac_003` 두 관계·`ac_004` 한 관계의 `ac_link_required=true`를 함께 보존한다.
- AC-004의 별도 Task unittest·scope·semantic 검사는 명시 요구가 없으면 false이고, semantic 명시와 연결 제거 결함, integration `criterion_refs`↔`goal_coverage.validation_ids` 양방향 불일치 차단을 유지한다.

R25 보존 원본 `r-s06-19-post-diagnostics-fix-r25-20260905-v1`의 완료 call `model_call_14959e9166524ad19294666c6352e488`, thread `01a06ffa-d0df-72f2-9900-4540b211bb54`, turn `01a06ffa-d6f4-7d62-9c25-5d32da1a067a`는 수정·재개하지 않았다. 새 portable fixture는 그 응답·입력·기대표의 결정적 projection이며 adapter 구조 PASS를 재현한다. 고정 의미 대조는 다음 세 차이를 그대로 재현해 FAIL한다.

| AC | validation | Expected | Actual |
|---|---|---:|---:|
| `ac_003` | `val_goal_independent_behavior_contract` | true | false |
| `ac_003` | `val_task_add_behavior_contract` | true | false |
| `ac_004` | `val_task_add_behavior_contract` | true | false |

이 fixture와 schema 순서 회귀는 실제 모델 의미 성공 evidence가 아니다.

## 검증 기록

- 관련 schema·inspection·fixture·request/receipt 결속 회귀 89개를 실행했다. 선언 순서 변경으로 기존 변조 입력 하나가 정상값과 같아진 점을 확인해 실제 역순 변조로 수정했고 해당 결속 차단을 다시 통과했다.
- 전체 `python -m unittest discover -s tests -p 'test_*.py' -v`: 584 tests PASS.
- 인계 작성 전 fresh 결정론 Gate: `.flowmarshal-engine-eval/runs/r-s06-evidence-order-fix-prehandoff-20260905-v1`, 5/5 PASS, report digest `sha256:1bae9a44389d67fa095de65650daebd642ee4306e032efecc7a0e99ef2987c38`.
- 인계와 README를 포함한 최종 source의 fresh 결정론 Gate artifact는 `.flowmarshal-engine-eval/runs/r-s06-evidence-order-fix-final-20260905-v3`이며 `qualification-report.json`의 `passed=true`, `check_count=5`, `failure_count=0`을 최종 조건으로 사용한다.
- `git diff --check`와 push 직전 `HEAD/main/origin/main` 관계 및 작업 트리 범위를 별도로 확인한다.

이번 경계에서는 실제 provider 호출, Plan activation, 역할 변경, fallback, 과거 checkpoint 재사용을 수행하지 않았다.

## fresh 실제 검증 진입 조건

다음 실제 의미 검증은 별도 승인된 제한 경계에서 새 run root와 현재 source manifest를 사용한다. 변경된 prompt·strict schema·request·기대표·독립 review·v2 model lock을 호출 전에 다시 결속하고, 실제 turn intent·receipt·terminal/result를 새 thread/turn과 대조한다. R25의 raw·receipt·summary·thread·turn·checkpoint를 수정하거나 재개하지 않는다.

첫 clean Reviewer는 제품 설정 `gpt-5.6-terra/high`를 그대로 사용하며 silent fallback하지 않는다. 구조·결속 성공 뒤에도 고정 AC 관계표와 독립 defect 기대값으로 의미 평가를 따로 수행한다. 이번 순서 보정만으로 세 관계가 true가 되거나 실제 의미 검증이 PASS한다고 주장하지 않는다. 기존 S06 FAIL, Functional Alpha 미완료와 1.0 NO-GO는 유지한다.
