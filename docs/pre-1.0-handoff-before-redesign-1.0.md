# 1.0 선행 보완 작업 인계

현재 상태는 [구현 현황](engine-implementation-status.md), 승인된 범위와 이후 기능은 [로드맵](pre-1.0-roadmap.md)을 따른다. 이전 R-S06 인계와 원본 실행은 역사 기록으로 보존한다.

## 현재 반복 검증

ReleasePerformanceFloor 구현 기준선은 v29 `df06a7c`·991 tests다. 분리한 `D:/codex/fm-performance-floor`의 `codex/performance-release-floor`는 정책·v4 assessment·RPC/원장 안전성·lifecycle·cutover 재계산 연결과 최종 **Gate 5/5·1,056 tests·diff check PASS**를 완료했다. 새 동결 commit·source manifest·정책 원문·검증 artifact는 [구현 결과](D:/codex/fm-inspection-runtime/performance-release-floor-20260907/구현-검증-결과.md)를 기준으로 이어받는다. 991개 기준선의 결과와 새 1,056개 결과를 구분한다.

현재 전체 상태는 **BLOCKED_USAGE_UNKNOWN**이다. `provider_call_4f3dc65762fd492e97d68087b2ec3ea3`의 원래 thread `01a079f1-f41d-7a93-adaa-22c30a746a52`, turn `01a079f1-f876-7ca2-9f0c-3510ecfcb634`만 다시 읽었고 `interrupted`·실제 usage 미제공을 확인했다. 원본 원장에는 Goal이 없고 actual tokens는 null이다. 추가 평가 모델 호출·임의 정산·새 원장 비용 초기화 없이 멈춘다. 확정된 static11의 724,352 token과 미확정 qualification 호출을 따로 보존하며 hypothesis round 3·연속 무진전 0/5를 승계한다. 진단 Goal 1,500,000·예약 100,000·reserve 25%, 다른 Goal 기본 1,000,000을 유지한다.

사용량 확보와 Core 정산·무결성 확인 후에만 새 동결본 `static11 → qualification13(독립 생성 검토 포함) → 실제 Goal Plan 선택 → 역할48 → Planning18 → E2E4 → performance36 중간 판정 → exact Plan 12개 활성화·실행 → 동일 Goal 예산의 lifecycle12 최종 판정 → cutover 감사 → main 병합 → 1.0 승격 판정`으로 진행한다. 변경된 계약에 과거 checkpoint를 PASS 근거로 재사용하지 않는다. 실제 성능은 계속 `NOT_OBSERVED / NO-GO`다.

v25의 실제 static11은 **10 PASS·1 NOT_RUN·전체 FAIL**이다. 이전 v24의 동일 10사례는 1 PASS였으며, 검사 기여 관계 규칙을 보완한 뒤 의미 오류가 개선됐다. 11번째는 실측 누계 **656,226 token**에 다음 호출 예약 100,000을 더하면 일반 사용 가능분 750,000을 넘어서 호출 전에 차단됐다. 추가 실제 모델 호출은 중단했다. 관측 범위를 과장한 검증 입력을 독립 검토 후 바로잡은 v26은 개발·동결 환경에서 **결정적 Gate 5/5·946개 테스트 PASS**이며 실제 모델 검증은 미실행이다. 최종 source의 S06·역할·Planning·E2E·성능 검증과 main 병합은 미완료다. 제품 기본 provider는 v1을 유지한다. 기존 1m/100k/25 안의 새 source 재검증과 완료 후 main 병합은 승인됐으며, 예산 증액은 사용자의 별도 승인이 필요하다. 진전 없는 약 5회 반복 시 중단 지시도 유지한다. [반복 검증 기록](pre-1.0-iterative-validation.md)에 원본·현재 source·재개 조건이 있다.

v19는 실제 역할 **48/48 PASS**, Planning **18/18 완료·15 PASS·3 FAIL**, 정상 Plan 선택 9건과 110회 호출 **4,059,253 token**의 정산·History 감사가 완료된 과거 기준선이다. 이 결과를 새 source의 선행 Gate로 재사용하지 않는다. 아래 v9/v10은 최초 실행과 후속 보완의 역사 기록이다.

## 최초 역할 48건 실행

사용자가 승인한 역할 48-cell을 v9 고정 source에서 전수 완료했다. **48/48 완료·Gate FAIL·1.0 NO-GO**다. 정상 Goal 오차단 1건과 schema failure 1건이 Gate를 막았다. recall 97.06%·precision 90.70%, critical false admission·seed 간 critical verdict 불일치는 0이다. 상세 근거는 [48건 결과](role-fixture-48-qualification.md)에 기록했다.

provider/checkpoint/usage 48/48/48, History chain 48/48, 중복·unknown·미정산 예약 0을 읽기 전용으로 감사했다. **실측 총 1,332,724 token**은 입력 1,320,603 + 출력 12,121이며 cached input 466,048은 입력에 포함된다. 승인한 Goal별 1m/100k/25를 적용했고 과거 Goal의 잠정 차감·2m override와 합산하지 않았다.

v9 실제 run은 `D:\codex\fm-inspection-runtime\qualification-2952c04-role48-20260906\10-role-fixture`다. 호출 전 JSON serialization 오류(v7)와 불안정한 테스트 시각 순서(v8)는 각각 실제 호출 0회였고 별도 실패 원본으로 보존했다. 이 두 문제를 수정한 v9에서 Gate 5/5·824개 테스트를 통과한 뒤 48회를 실행했다.

P11의 schema failure는 모델에 전달한 schema가 허용한 candidate ref를 generic 사후 validator가 금지한 계약 불일치와 연결됐다. 후속 개발 source에서 generic 전용 draft·`maxItems: 0`·명시 역할 지침으로 수정했다. 실제 Goal/Plan Task ref 계약, fixture·oracle·합격선은 유지한다. G01 정상 요청의 문맥 누락과 G07의 필수 trace finding 누락은 당시 의미 판단 보완 대상으로 분류했다. v9 결과를 수정해 PASS로 바꾸지 않았고, v10 수정 직후 실제 모델 호출은 0회였다.

당시 후속 보완 source의 동결 작업본은 **v10**(`D:\codex\fm-pre10-validation-v10`), HEAD `00f341af30f34f23362fbede65f42081ac0324e0`, manifest `sha256:c543d2d1fa9cdad379cd9d8fd4e58c58faeff62a6beed41bb6d45db8b289cbad`다. **당시 Gate 5/5·826개 테스트 / 113.639초·legacy 40개 변경·누락 0**을 통과했다. 원본 `.flowmarshal-engine-eval\runs\pre10-role48-followup-devgate-20260906`의 contract는 `sha256:d7ad0188a6ac032999723ceb59b39e2ce9c8ad797c5d55d425110548fa7420e4`, report는 `sha256:d091be087c840eb08037d2013029bc07dcd9ef6f98cb826104c587d069fc69fe`다. 실제 48 FAIL은 v9 계약으로 유지하고 v10 후속 실제 실행은 새 run root가 필요하다.

## 이번 구현

- 최종 Verdict가 참조한 Plan·Goal revision을 조회하고 동일 Goal 계보만 집계한다. 실측 0과 usage/latency 미확인, 동일·충돌 receipt와 호출 누락을 구분한다.
- token 사용량이 미확인이어도 관측된 latency는 보존한다. 중복 usage ID가 같은 논리 호출을 가리키는 경우에도 token 완결 여부와 latency를 일관되게 계산한다.
- schema revision 3에 budget policy·provider call 예약·잠정 정산·model rebind 이력을 둔다. 기존 revision 2 원장은 자동 변환하지 않는다.
- Goal 준비·계획·상세화·Worker·Task/Goal Validator·수정 호출의 예약·정산을 연결하고, CoreOperations의 효과 전 차단은 외부 효과 불명으로 오분류하지 않는다.
- 역할별 timeout 정책, interrupt 요청/receipt/terminal 구분, 기존 9개 호출과 후보를 검증하는 명시 continuation을 추가했다.
- 허용 envelope 안의 명시 model/effort 선택은 새 Spec·Attempt에 결속한다. Worker 완료 뒤 Validator 변경으로 Worker를 자동 재실행하지 않는다.
- 결정적 Task validation의 operation은 실제 Worker Attempt·성공 History sequence·검증 epoch와 실행/검사 Spec에 결속한다. 동일 Worker의 완료 receipt는 재사용하되, 새 Worker에 과거 검증을 재사용하지 않는다.
- 역할의 `schema_failed` receipt와 원래 요청·정산·기존 thread terminal이 일치하면 `operation.failed`로 닫는다. 같은 실패 요청을 자동 재호출하거나 실패를 성공·무효과로 바꾸지 않는다.
- planning-only 상세화 폐기 비율은 null/NOT_OBSERVED로 차단하며 실제 lifecycle evidence와 비교 계약에 결속한다.

## 운영 설정

검증 시작 설정은 [예산 JSON](../config/pre-1.0-validation-budget.json)의 Goal당 1,000,000 token, 호출당 100,000 예약, reserve 25%다. 제품의 고정 상한이나 입증된 최적값이 아니다. 실제 후속 Worker의 도구 사용 포함 실측이 한 호출 166,858 token에 도달해, 사용자는 같은 Goal만 상한 2,000,000·호출 예약 200,000·reserve 25%로 조정하도록 추가 승인했다. 이 Goal override는 `budget_policy_440a2165efdf4d6682b47cbc6a9f3a0d`에 기록했고 다른 Goal의 설정은 바꾸지 않았다. [timeout JSON](../config/pre-1.0-role-timeouts.json)은 기존 900초 Reviewer timeout의 후속 관측 창을 명시적으로 1,800초로 바꾼다.

```powershell
flowmarshal-engine project budget set --project-id <project-id> --policy-file config/pre-1.0-validation-budget.json
flowmarshal-engine project budget show --project-id <project-id>
flowmarshal-engine project budget observe-role --call-id <provider-call-id> --codex-bin <절대 실행파일>
flowmarshal-engine project budget adjust-unknown --call-id <provider-call-id> --charge-tokens <명시 차감량> --reason <이유>
flowmarshal-engine model status --project-id <project-id> --live --codex-bin <절대 실행파일>
flowmarshal-engine model rebind --request-file <선택 요청 JSON> --codex-bin <절대 실행파일>
flowmarshal-engine report final --project-id <project-id> --goal-verdict-id <과거 판정 ID> --format markdown
```

일반 역할 요청은 전역 `--role-timeout-policy`로 정책을 주입한다. `model rebind` 요청에는 project/task/Plan revision·activation digest/current Spec revision·digest, executor 또는 validator 역할, 선택 model/effort와 이유가 필요하다. 현재 query 결과가 대상과 digest를 제공한다. 미지원 fallback 밖 변경은 새 Plan이 필요하며, 결과가 불명확한 호출을 모델 변경으로 재실행하지 않는다.

## 현재 실제 실행 경계

원본 `D:\codex\fm-planning-feedback-v3\.flowmarshal-engine-eval\runs\planning-feedback-s01-planning-20260906`과 선행 v1·v2의 9회 호출을 보존한다. 완료 8회의 실측 소계 269,854 token, 이후 interrupted·final null로 관측된 timeout 1회의 사용량 미확인이라는 과거 사실은 변경하지 않았다. 사용자는 2026-09-06에 그 호출의 100,000 token 잠정 차감과 Goal 상한 1,000,000 안의 재개를 승인했다.

첫 continuation은 모델 호출 전에 SQLite의 새 0-byte WAL·SHM을 원본 변경으로 판단해 차단됐다. 원본·실패·claim을 보존한 `--retry-preflight-run` 경로를 추가해, 효과가 시작되지 않은 정확한 예약만 해제했다. 비어 있지 않은 WAL, 원본 digest 변경, 이미 소비된 retry claim은 차단한다. 원본 timeout과 9개 호출의 계보·잠정 차감·남은 탐색 예산은 새 실행에 이어진다.

현재 실행 원장은 `D:\codex\fm-pre10-validation-v2\.flowmarshal-engine-eval\runs\planning-feedback-s01-preflight-retry-20260906\ledger`다. 같은 Goal `goal_c0872d8cdf3f4f7f994b44b9572f7d14`의 revision 2를 유지하며, 후속 Plan Reviewer는 input 45,638·output 5,223, 총 50,861 token으로 완료됐다. exact selected Plan은 `plan_revision_001468c3b4ca4d56acf62843affe9b96`, activation digest는 `sha256:4c5ada93e732d1f08f2bb521a24850b8113fde3702d7f8d83ed1b5c1303cf873`다. 사용자가 이 Plan을 명시 승인했고 activation `activation_ed503e23c74e416787acd1bda43d136e`를 기록했다. 원래 planning `summary.json`은 선택 단계의 불변 결과로 유지한다.

첫 Worker는 실행 호스트 `codex-code-mode-host.exe` 누락으로 파일 조회부터 실패했다고 응답했고 프로젝트를 바꾸지 않았다. provider terminal 성공은 Attempt 성공으로 기록됐지만, 실제 `test_add_returns_sum()` 실행은 FAIL이어서 Core가 Task 완료를 차단했다. Attempt `attempt_6e1a8dd411a04aad915cc9897f971c82`와 validation `validation_result_b14f1d6f0b3b492ca16ff40da4402375`를 보존한다. 같은 버전·동일 Codex executable hash의 설치 패키지에서 누락된 helper를 복구했으며, 증명은 아래 운영 경로의 `runtime-helper-repair.json`에 있다.

복구는 실패 validation의 직접 파일 근거와 같은 Worker Attempt의 환경 오류 응답을 함께 참조하는 명시적 RecoveryAssessment를 사용했다. 기존 성공 Attempt를 실패로 바꾸거나 이전 FAIL을 삭제하지 않았다. 재시도 Worker `attempt_b86dbfd9b2b641d985d7c3278e938228`는 실제 add를 수정했지만, 후속 검증에서 같은 Spec의 과거 command receipt가 재사용되는 공백을 발견했다. 현재 Worker·검증 epoch에 operation을 결속하도록 수정한 v6 source에서 새 검증을 실행해 PASS를 기록했다. 잘못 재사용된 FAIL `validation_result_97cf0aa2e768424e9559b76848b9ed11`도 원본으로 보존하고 현재 완료 판정에서는 제외한다. `task.retry_enabled`와 새 Worker 성공의 History sequence, Worker Spec/현재 validation Spec을 함께 대조한다.

운영 설정·사용자 승인·복구 입력은 `D:\codex\fm-inspection-runtime\planning-continuation-20260906`에 보관한다. 두 번째 Task의 실제 상세화 응답은 제공된 `AGENTS.md` digest의 전사 오류 때문에 거부됐다. 원래 관측과 현재 파일 bytes가 동일함을 대조하고, 잘못 복사한 digest와 기존 timeout 정책의 운영값만 별도 proposal에서 보정했다. `task2-preparation-correction-proof.json`·새 `execution_preparation_correction` operation·Spec `execution_spec_8e118dc21a1a4d7b8fb924af2ea1d1b9`로 결속했고 원본 응답은 변경하지 않았다.

독립 Goal Test 준비 역할은 실제로 완료됐지만 deterministic 명세에 `semantic_instruction`을 혼합해 schema에서 거부됐다. 원래 call `provider_call_f1bc1458d8434147b79873b7ba1e14b2`의 30,519 token도 비용에 남는다. 기존 thread/turn을 재개 없이 읽어 terminal을 확인한 뒤 v7의 `observe_terminal_role_failure`로 원래 operation `operation_8321279ec233707c55d9ccccec96c100`을 `ROLE_SCHEMA_FAILED`로 닫았다. 별도 `goal_test_preparation_correction`에 원본·terminal·수정 명세 digest를 남기고 기존 수동 `--goal-validation-file` 경로를 사용했다. Goal·Plan·검사 의미를 변경하거나 새 모델 호출을 하지 않았다.

독립 검사는 등록된 `test_add_returns_sum()`를 새 프로세스에서 실행한 뒤 `from app import add` 공개 호출 결과가 정수 `5`인지 별도로 비교했다. 두 Task의 evidence 합계로 검사를 대체하지 않았다. 2026-09-06 17:18:43 KST에 최종 GoalVerdict `goal_verdict_dd3634400e9b469f965965e5654b2f70`가 **satisfied**, Hard AC 2개가 모두 PASS로 기록됐다. 프로젝트는 `completed`이며 History cursor **151**, History chain 검증은 유효하다.

| 실제 완료 근거 | ID |
|---|---|
| 첫 Task 최신 검증 PASS | `validation_result_3658e43a5d2340c9a14523973ab91c78` |
| 둘째 Task Worker | `attempt_8749d4484c0841ec924bba7b93c133c1` |
| 둘째 Task 등록 검사 PASS | `validation_result_731f0ca68c0c4041a559bc737c9080c2` |
| 둘째 Task 공개 계약 검사 PASS | `validation_result_eda2322374844cdba2f1d3a356a54aec` |
| 독립 Goal Test PASS | `validation_result_a393b112f91043c991c3d1f0d73412ec` |

원장 provider 호출은 **16회**, 확인된 usage 호출은 **15회**다. 실측 소계 **788,868 token**은 입력 **757,688** + 출력 **31,180**이며 캐시 입력 **335,872**는 입력에 포함된다. 승인된 잠정 차감 **100,000**을 별도로 더한 예산 차감은 **888,868**이다. 상한 2,000,000에서 일반 잔여 **611,132**, reserve **500,000**, 전체 잔여 **1,111,132**이며 미정산 예약과 미조정 unknown call은 없다. 과거 timeout 1회의 실측이 미확인이므로 실측 총량은 계속 **null**이다. 이 수치는 해당 Engine Goal 범위이며 Codex 계정 구독 한도 차감량이 아니다.

최종 원본은 위 운영 폴더의 `goal-final-report-v7.json`, `goal-final-report-v7.md`, `goal-completion-audit-v7.json`이다. JSON과 Markdown을 같은 typed 결과로 대조했고 조회 전후 실제 DB bytes가 같음을 확인했다. 실제 호출과 명령 단계별 receipt는 `engine-recovery-v6-steps` 및 `engine-recovery-v7-steps`에 보존한다.

이 결과는 v2·v4·v6·v7 source를 거친 **같은 Goal의 진단·복구 완료 근거**다. 두 운영 명세의 명시적 수동 보정을 포함하므로 최종 source의 무인 전체 pipeline이나 1.0 E2E qualification PASS로 집계하지 않는다. 이미 완료된 Goal에 아래 복구 명령을 다시 실행하지 않는다.

```powershell
flowmarshal-engine --db <현재-ledger> --artifacts <현재-artifacts> attempt retry --task-id <실패-Task> --recovery-assessment-file <명시-복구-JSON> --failed-validation-result-id <최신-FAIL>
flowmarshal-engine --db <현재-ledger> --artifacts <현재-artifacts> --role-timeout-policy <timeout-JSON> run once --project-id <같은-project> --codex-bin <고정-executable> --role-config <원래-roles>
```

새 benchmark는 같은 Goal의 모든 materialized Spec revision과 실제 실행·검증·State·Verdict를 재관측한다. 상세 Plan 수정 출력도 receipt와 exact Plan digest에 결속해 비용에 포함한다. R3.1 비교는 동결 source 밖의 예산 proxy와 parent timeout·receipt 대조로 연결했다. 36개 planning cell 뒤 중간 v4 평가를 만들고, 선택된 Engine 12 cell의 exact activation·lifecycle 완료 뒤 `observe-benchmark-lifecycle`로 final 평가를 추가한다. `cutover`는 같은 원본에서 재계산한 final v4 보고서만 받는다. 상세 계약은 [Release Performance Floor](performance-release-floor.md), 실행 명령은 [qualification 실행 계약](engine-qualification.md)을 따른다.

## 검증 기록

이전 동일 Goal 완료 당시 v7 결정적 Gate는 **5/5**, 전체 테스트는 **823개 / 122.253초**, legacy 동결 검사는 **40개 / 변경·누락 없음**으로 통과했다. 고정 source 작업본은 `D:\codex\fm-pre10-validation-v7`의 `4dcd03dbe69a97bf2907065d95ebae0829948d97`, 원본 Gate는 `.flowmarshal-engine-eval\runs\pre10-terminal-role-failure-devgate-20260906`이다. source manifest `sha256:e91658eba7722651748271108e2231d2ff3b3661f8703119bd8c20bb674ef284`, contract `sha256:df4825ea9cada8ad9c3201794b244044798809a3057d509f1f9a511fa5fc58a7`, report `sha256:f7a5b7323287253f7272c856273b433a0747d599d71426a21d3d525c595981c7`다.

v3 Gate는 옛 lifecycle helper import를 참조한 테스트 때문에 실패했고 v4에서 5/5·811개를 통과했다. v5 Gate는 실제 실행 디렉터리에 결속된 continuation 테스트가 변경된 실행 산출물에 영향을 받아 4/5·817개 중 2건 오류로 실패했다. 독립적인 임시 9-call fixture로 바꾼 v6에서 5/5·817개를 통과했고, 실제 target 변경 차단은 유지했다. v7은 당시 확인된 역할 실패 종료·집계 수정을 포함한다. 모든 과거 source·실패 보고서·기존 774-test 결과는 각 원래 작업본에 보존한다.

v9 당시 실제 역할 48-cell은 FAIL이었다. v13의 역할 48-cell은 PASS, Planning 18-cell은 완료·FAIL이며 E2E 4·성능 36은 미실행으로 **NO-GO**다. R3.1 비교 subprocess의 사전 예산 검사와 receipt 대조 구현은 보완했지만 실제 36-cell 통과 근거는 아직 없다. v29 `df06a7c`·991 tests와 이번 구현 검증도 실제 성능 결과가 아니며, 부분 단위 테스트나 합성 성공으로 이를 대체하지 않는다.

## 다음 실행

운영 폴더의 `qualification-launch-proposal.md`, `qualification-launch-plan.json`, `qualification-budget-approval-proposal.json`은 원래 v7 검토안의 역사 기록이다. 현재 명령으로 그대로 실행하지 않는다. 사용자 48건 승인·0-call 실패 뒤 재동결·실제 v9 입력은 최종 campaign의 `role48-launch/precall-retry-binding.json`에 연결됐다. 실제 48건은 종료됐으며 재호출하지 않는다. 현 구현은 Goal별 예약·정산만 집행하고 campaign 전체 cap은 집행하지 않는다.

최종 보완 source의 역할 48 PASS와 감사 뒤 Planning 18 → E2E 4 → 성능 36 planning 평가 → 선택 Engine 12 lifecycle final 평가 순서로 진행한다. 사용자는 2026-09-06에 기존 1m/100k/25 정책 안에서 실패 분석·수정·재검증을 계속하도록 승인했고 token 상한·호출 예약량 증가에만 추가 승인을 요구했다. 결정 원문은 운영 폴더의 `iterative-validation-user-decision-20260906.json`에 보존했다. 원래 실패 cell이나 완료 run root를 새 source로 재사용하지 않는다. trace·usage·whole pair·baseline denominator·미실행/폐기 분모가 없으면 null/NOT_OBSERVED로 차단한다. 여섯 최적화 목표는 완전 관측 후 미달을 공개 후속으로 남기며, final 필수 최소선과 네 qualification이 모두 통과한 때만 cutover를 실행한다. usage unknown 차단은 그대로 유지한다.
