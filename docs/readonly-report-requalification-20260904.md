# 읽기 전용 보고 계약과 실행 evidence 연결 검증

- 기준일: 2026-09-04 KST
- 시작 commit: `728e373`
- 이전 근거: [최종 source 재평가](final-source-requalification-20260904.md)
- 실행 root: `.flowmarshal-engine-eval/runs/readonly-report-20260904`

## 변경 범위

읽기 전용 분석에서 프로젝트 파일 무변경과 새 응답 보고 생성을 구분했다. 보고 product key는 파일 생성 권한이 아니다. Skeleton generator·refiner·Plan expander가 응답 보고의 내용 검사와 프로젝트 파일 무변경 검사를 구분하도록 지침을 보완했다. 명시적인 파일 요구나 더 강한 금지 조건은 유지한다. Reviewer 판정, 기존 회귀 oracle와 합격선은 바꾸지 않았다.

실행 경로에서는 현재 성공 Execution Attempt의 Worker 응답 관측을, `external_observation`을 요구하는 Task semantic 검사에 한해 전달한다. prompt·intent의 evidence ID·결과 검사에 같은 catalog를 사용한다. Worker의 완료 주장은 충족 증명이 아니며 응답 내용은 원본 파일 근거와 함께 검사한다. Goal Validator에도 같은 검토 원칙을 명시했다.

원장에 보존되는 응답 관측의 기존 10,000자 한도는 유지한다. 그보다 긴 응답은 `:truncated`로 표시하고 Task·Goal semantic catalog에서 제외한다. 잘린 본문을 완전한 보고로 검증하지 않는다. 전문 artifact 저장이나 대형 보고서 처리는 이 변경의 완료 범위가 아니다.

회귀는 응답 보고의 Plan 계약 보존, 직접 모순 finding의 차단, 필요한 경우에만 현재 Worker 응답을 전달하는 경계, 임의 외부 제출 제외, 원본 근거 누락 및 응답 잘림 시 Goal PASS 차단을 검사한다. 합성 역할 출력·모의 runtime 회귀를 실제 읽기 전용 Goal의 실행 완료로 표현하지 않는다.

## 합성 검사 입력 수정

첫 `deterministic/` 실행은 전체 463개 테스트를 통과했지만 synthetic lifecycle이 저장소 전체 지침을 선택해 Context 예산 12,000을 초과했다. 실패 artifact와 stderr를 보존한다. 이 실패는 읽기 전용 Planning의 모델 실패와 구분한다.

합성 상태 전이 검사의 입력을 전용 `tests/fixtures/engine/synthetic-lifecycle-project`로 고정했다. fixture 파일별 digest를 synthetic cell에, 상대 경로를 규칙 digest에 명시 결속한다. 전체 source 검증은 기존 전체 테스트·compileall·freeze가 담당한다. Context 예산과 합격선을 늘리지 않았다. 독립 회귀에서 큰 상위 문서의 영향을 받지 않는 lifecycle 완료, fixture 무변경, fixture 수정 시 계약 무효화를 확인했다.

## 첫 수정 source의 평가

source digest는 `sha256:b3e91bdcc97306ca84ebd7fa9fa448a783afa97349423eaa7524d7891c4f8c79`다. 실제 모델 scope는 직전 세션의 역할 설정을 유지했다.

- 역할 설정 digest: `sha256:be726ac5b76c4b6e12172a5e6e4060cfc8d1ed832d2e5e31167333ad2d52564e`
- model lock: `sha256:87d0ec7abb30e101d94b2d96aa67da38f81ae3601506fd94c9a5ebd19e2148f5`
- 역할 설정: Luna/high, Terra/high, critical Reviewer·Validator Sol/xhigh
- 실행 전 실제 `model/list`로 지원 여부를 확인했다.

`final-deterministic/`: 전체 **464 tests OK**, compileall·pip check·synthetic lifecycle·legacy freeze 40개 PASS.

`role-fixture/`: **48/48 cell, PASS**. 필수 finding recall 97.22%, precision 100%, critical false admission 0, clean false block 0, schema failure 0, critical seed 불일치 0이다.

`project-e2e/`: **4/4 cell, PASS**, 중복 효과 0건이다. 정상 완료, 입력 stale 차단, 저장 turn 중단·재개, 생성 receipt 유실 차단을 실제 adapter로 검사했다. 이 네 시나리오는 읽기 전용 보고 Goal 전체의 실제 실행이나 성능 Gate를 대신하지 않는다.

기능 scope는 별도 원장·작업 복사본에서 병행했다. 수집된 지연은 성능 benchmark의 단독 실행 수치로 사용하지 않는다.

`full-planning-pipeline/`: **18/18 cell 수집 완료, 12건 통과·6건 실패, Gate FAIL**이다. 정상 입력 Plan 선택은 6건, 필수 정보가 없는 입력의 정상 차단은 6건이다. 최대 역할 호출 9회, 최대 후보 버전 5개, 최종 schema failure 0건이다.

| 시나리오 | seed 17 | seed 43 | seed 89 |
|---|---|---|---|
| S01 단순 수정 | PASS | PASS | Goal의 로컬 효과 오분류 |
| S02 읽기 전용 분석 | 변경 Task 오분류 | 변경 Task 오분류 | 금지 효과 범위 혼합 |
| S03 migration 비교 | PASS | 반환 동작 계약 누락 | 반환 동작 계약 누락 |
| S04 produces/consumes DAG | PASS | PASS | PASS |
| S05 필수 입력 부족 | 정상 차단 | 정상 차단 | 정상 차단 |
| S06 비가역 효과 대상 부족 | 정상 차단 | 정상 차단 | 정상 차단 |

S02 seed 89의 `INTENT_EFFECT_SCOPE_CONFLATION`은 하나의 `external: true` 금지 효과에 로컬 파일 변경과 외부 시스템·계정·제3자 효과를 함께 넣은 문제다. S03의 `GOAL_PUBLIC_BEHAVIOR_OMITTED`·`GOAL_OMITS_ADD_RESULT_CONTRACT`는 함수 이름·호출 형태만 보존하고 관찰된 문서·테스트의 합산 반환 계약을 Hard AC에서 누락한 문제다. S01 seed 89의 `INTENT_EXTERNAL_EFFECT_MISCLASSIFIED`는 Goal이 로컬 코드 수정·로컬 검증 실행을 외부 효과로 허용해 상세 Plan의 분류와 충돌한 문제다. Reviewer의 원시 finding을 보존하며 oracle 결함으로 바꾸거나 합격선을 낮추지 않았다.

| 보고서 | digest |
|---|---|
| final-deterministic | `sha256:72ccb0facc92249df5dec76fab06e4e0977179d89f1196312974a8251e53a9c0` |
| role-fixture | `sha256:311cf72b6e4efc762af38f37263d8de28b17831e69d11326533359a08bf71e13` |
| full-planning-pipeline | `sha256:4b6e15d6cfc9e1f46d4f036fccd039f044c4432bbb513f9061e8e01091b296b3` |
| project-e2e | `sha256:919aec94afe7e1f12c9f707ee483226ea48b4d9d856edb818354819391034bb4` |

역할 회귀 receipt 48건은 input 1,205,284, cached input 553,856, output 12,142 tokens, latency 합 462,592ms, schema recovery 3회를 기록했다. Planning receipt 82건은 input 2,713,570, cached input 1,200,384, output 127,002 tokens, latency 합 2,888,656ms, schema recovery 22회다. 두 범위 모두 모든 receipt에 usage가 있다. cached input은 input에 포함되고 reasoning은 output에 포함된다. recovery 횟수와 최종 schema 실패를 구분하며 이 수치를 독립 성능 Gate로 사용하지 않는다.

## Task 종류 진단

첫 S02 seed 17의 Skeleton은 응답 보고를 명시하면서 `write_response_report`의 종류를 `change`로 지정했다. Core는 기존 `READ_ONLY_MUTATION`으로 차단했다. 이는 올바른 차단이며 기존 보고 산출물 모순을 검증하는 상세 Plan 단계까지 도달하지 못한 결과다.

`task-kind-probe/`에서는 소스 파일을 바꾸지 않고 공통 지침에 “read_only 응답 보고 Task는 inspect 또는 decide이며 작성 동사만으로 change Task를 만들지 않는다”를 추가해 동일 S02 seed 17을 진단했다. override 본문, 평가 계약, fixture digest, seed와 receipt를 별도 보존했다. **6회 역할 호출 후 Plan 선택에 성공**했다.

선택된 Plan은 `inspect` Task 하나의 논리 응답 보고를 요구한다. Task 및 독립 Goal 의미 검사는 `model_review`, `external_observation`, `file`을 모두 요구하고 파일 집합·내용의 무변경 검사는 별도로 둔다. 이 단일 진단은 전체 qualification PASS가 아니다.

## 최종 지침 보완

전체 평가가 종료된 후 진단에서 확인한 Task 종류 지침을 소스에 반영했다. 상세화 역할에는 기대·금지 효과 모두 로컬 파일 mutation과 외부 시스템 효과를 별도 항목으로 구분하도록 명시했다. 새 source digest는 `sha256:d7d109bbdfbedf0cdcc97e6dc0e9d05ddc1deea8816bfa5083bcd7563959fd75`다.

`revised-deterministic/`에서 최종 source의 전체 **464개 테스트**, compileall·pip check·synthetic lifecycle·legacy freeze 40개가 모두 통과했다. 보고서 digest는 `sha256:3a6ae5151101186287062321115833c83a6e0a4ae22303b8af4c3141c9465d19`다.

최종 source에 override 없이 S02 seed 17·89를 다시 실행했다. 이는 특정 실패의 개발 재검증이며 전체 qualification은 아니다. **두 실행 모두 Plan 선택에 실패**했다. 이전 단일 override 진단의 성공으로 최종 수정 성공을 대신하지 않는다.

| 재검증 | 결과 | 직접 finding |
|---|---|---|
| `final-probe-seed-17/` | 역할 호출 5회, 선택 실패 | `PLAN_EFFECT_POLICY_VIOLATION`: 외부 효과가 발생하지 않는다는 부정형 문장을 `external: true` 기대 효과로 넣어 Goal 허용 효과와 불일치 |
| `final-probe-seed-89/` | 역할 호출 6회, 선택 실패 | `VER_COMMAND_EXECUTION_EVIDENCE_MISSING`: Goal의 명령 미실행 조건을 file·diff만으로 입증하려 했으며 명령 실행 기록 등 필요한 직접 근거가 없음 |

두 결과에는 앞선 `READ_ONLY_MUTATION`이 없지만 그것만으로 읽기 전용 Planning 전체가 해결됐다고 판단하지 않는다. file·diff는 파일 무변경의 근거이며 부작용 없는 명령까지 실행되지 않았음을 증명하지 못한다. 새로운 finding에 맞춰 oracle나 합격선을 바꾸거나 실패를 지우지 않았다. 모든 실제 실행은 종료됐으며 진행 중인 평가를 남기지 않았다.

## 판정과 다음 작업

성능 benchmark와 1.0 cutover는 수행하지 않았으며 **NO-GO**를 유지한다. 전체 Planning은 수집 완료 상태의 FAIL이며 미완료 campaign이 아니다. 위 네 범위의 결과는 첫 수정 source에 결속되므로 후속 지침을 반영한 최종 source의 전체 qualification으로 재사용하지 않는다.

다음 작업은 Goal 정규화의 관찰된 반환 동작 계약 보존, 로컬·외부 효과 분류, 계획 역할의 현재 명령 금지를 미래 Task의 명령 금지로 옮겼는지의 출처를 검토하는 것이다. 명시적인 사용자 금지를 완화하지 않으며, 실제 명령 미실행이 Hard AC라면 그 조건을 입증할 evidence 계약도 함께 설계한다. Plan의 부정형 효과 문장을 기대 효과로 분류하는 오류도 보완해야 한다. 정규화 후보의 직접 누락을 독립 Reviewer가 차단한 결과를 오차단으로 바꾸지 않는다.

이 계약 문제들을 보완한 후 확정된 하나의 source에서 네 기능 scope를 다시 대조한다. 네 기능 scope가 통과한 경우에 별도 36-cell 성능 Gate를 수행한다. 현재 세션은 검증 경로 구현과 실패 근거 수집을 마쳤으며, 읽기 전용 Planning의 실제 안정성은 미완료 상태다.
