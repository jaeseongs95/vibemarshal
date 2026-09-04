# R-S06-09 검사 근거 대조표 구현·제한 진단 결과

구현과 최종 결정적 Gate는 **PASS**, 제한 실제 진단은 **FAIL**이다. 첫 정상 대조 사례의 인용 검증 실패에서 중단했다. 전체 qualification은 미실행이며 1.0 cutover는 **NO-GO**를 유지한다.

## 권한·시작 상태와 적용 지침

첫 파일 조회 전에 이 turn에 실제 제공된 `sandbox_mode=danger-full-access`, `approval_policy=never`를 확인했다. 개발자 지침의 파일 시스템 제한 없음·네트워크 허용을 근거로 승인 질문 없이 진행했다. 역할 cwd는 `C:\Users\sjs95\Documents\ChatGPT\자동화 개발`, 제품은 `D:\codex\flowmarshal`로 구분했다.

시작 HEAD는 `76b2bd9fc3f231874979119fa2df2d1707381acf`, branch는 `main`, 작업 트리는 clean이었다. `C:\Users\sjs95\.codex\AGENTS.md`, 제품 `AGENTS.md`, `docs/orchestration-redesign.md`, `docs/engine-cutover-adr.md`, `docs/r31-frozen-baseline.md`를 적용했다. 역할·제품의 확인 대상 상위 경로에는 추가 AGENTS.md가 없었다. 지정된 원인분석 작업의 완료 결과를 읽고 기존 결함·검증 기준을 재사용했다. 다른 작업에 메시지·callback·예약을 보내지 않았다.

## 구현

- `planning.validation_comparison_targets`가 모든 AC×Task·integration validation과 constraint×Task의 원문 ID·순서를 열거한다. 필요한 연결이나 도구 능력을 추정하지 않는다. 작성 시에는 완성된 draft를 같은 함수로 대조한다.
- `planner_roles`의 `PlanExpansionEnvelope`, `PlanReviewEnvelope`가 기존 draft/review와 `PlanInspection`을 감싼다. 대조표는 AC 명시 절차·전역 의무·선택적 관계, 검사 주장·도구·phase·별도 실제 검사와 독립 finding 연결을 제출한다. 짧은 인용은 한 번 등록하고 각 행에서 참조한다.
- `plan_inspection`이 완전한 행 집합, 중복·ID, JSON pointer, 연속 원문 인용, 등록 파일 digest, 해당 AC·검사·constraint의 직접 인용, finding의 ID·Task·evidence와 내부 일관성을 검증한다. 잘못된 의미 분류 자체를 코드로 정답화하지 않으며 coverage를 수정하지 않는다.
- `qualification`의 digest에 실제 adapter 출력 envelope schema와 모든 공유 지침을 결속했다. 새 진단 실행기·테스트 helper도 source manifest에 포함한다.
- `plan_inspection_eval`은 호출 전 고정한 독립 결함별 AC·validation·Task·원문 selector·evidence와 실제 finding을 대조한다. 하나의 finding을 두 독립 결함에 재사용하거나 근거 없는 추가 finding으로 통과시키지 않는다. 탐지 결속은 Core의 gate·severity·remediable 분류와 분리한다.
- `scripts/diagnostics/r_s06_09.py`는 source·지침·schema·입력·기대값·13회 상한을 잠그고, 실제 효과 전에 호출을 소비한다. 기본 제품 역할 배정은 유지하며 진단에서만 schema recovery를 0회로 설정한다. 생성 정상성은 별도 독립 대조를 요구하고, 통과한 생성 결과만 고정 template에 결속해 Reviewer에 전달하도록 구성했다.

GoalContractRevision, PlanContractRevision, ReviewerSubmission와 DB schema는 변경하지 않았다. Core admission·score, finding/rating 상호배타성, Skeleton 기여 집합, 독립 Goal Test와 ready-time 명령 경계도 유지했다. 대조표는 provider 제출물로만 보존하며 Core에는 기존 계약만 전달한다.

## 결정적 검증

호출 전 전체 테스트 **508개**와 결정적 Gate 5개가 통과했다. 최종 보완 후에는 **510개**와 동일 Gate 5개가 통과했다. Gate는 compileall, 전체 unittest, pip check, 합성 lifecycle, legacy freeze다. freeze의 40개 파일은 모두 보존됐다.

새 회귀에는 `ac_003 → val_task_oracle`, `ac_004 → validation_task_oracle_and_unittest`, 저장된 expanded의 연결 누락·phase 과장 두 결함, 정상 combined, 전역 semantic 의무와 AC 명시 의무의 구분, selector·인용·행·finding의 모순, 부분적인 전역 검사 누락, 실제 adapter schema·공유 지침 결속, 첫 schema 실패의 무재시도 receipt, 물리 호출 13회 상한을 포함했다. 기존 scripted 회귀의 대조표 helper는 의미 탐지의 증명이 아니며, 구조적으로 일관된 잘못된 분류가 고정 평가에서 실패하는 경계도 검증했다.

결정적 테스트의 임시 합성 원장은 운영 원장과 별개이며, 이번 실제 진단은 SQLite를 생성하거나 운영 원장을 변경하지 않았다.

## 실제 진단과 실패

증거 root는 `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\r-s06-09-20260904-v1`이다. 기존 11개 구성에 `stored-multi-defect`, `semantic-missing-link`를 더한 최대 13회 순서를 잠갔다. 고정 입력 JSON 17개와 이전 실행 파일 1,780개를 결속했다.

첫 `clean` 사례를 기존 `gpt-5.6-terra / high`로 실행했다. 정상 기대값은 finding 0개였다. 원시 JSON은 envelope의 Pydantic 형태 검증을 통과하고 AC 쌍 28개·constraint 쌍 3개·검사 행 7개를 제출했으나, adapter의 **원문 인용 일치 검사에서 거부**됐다. receipt 상태는 `schema_failed`다.

`C_REF_TASK`, `C_REF_VALIDATOR`, `C_REF_GOAL`은 등록 검증 문서의 문장을 인용하면서 모두 `source:goal /source_traces/4/statement`를 가리켰다. 해당 원문은 `test_app.py`의 unittest 관측 JSON이므로 세 인용이 모두 일치하지 않는다. 원시 응답·selector를 수정하지 않았다.

원시 finding은 다음 4개이며 사전 고정한 정상 기대값에 비추어 모두 추가 finding이다. adapter가 거부했으므로 Core의 ReviewerSubmission으로 수용되지 않았다.

| 원시 code | 제출 내용 |
|---|---|
| F001 | ac_001에 val_goal_independent_unittest 연결을 추가 요구 |
| F002 | ac_003에 val_goal_independent_behavior_contract 연결을 추가 요구 |
| F003 | AC가 semantic 절차를 명시하지 않았다는 이유로 기존의 선택적 semantic 연결을 결함 처리 |
| F004 | 검증 문장이 file·diff를 언급한 사실을 입력이 그것으로만 제한됐다는 주장으로 확대 |

F003은 연결 의무가 없다는 사실과 선택적 연결의 금지를 혼동한다. F004의 “로만” 제한은 Plan 원문에 없으며, 이후 semantic Validator의 직접 evidence catalog를 검증 계약의 필요 evidence 종류와 혼동하는 경계와 관련된다. F001·F002는 명시 절차와 다른 검사의 부가·중복 책임을 어디까지 같은 AC에 강제 연결하는지에 관한 잔여 판정이다. 이번 실패 뒤 원본·기대값·threshold를 수정해 정상으로 바꾸지 않았다.

직접 전송된 developer instructions·payload·strict schema, request digest, receipt의 input/schema digest와 실제 turn binding은 일치했다. 실제 turn은 **1회**, schema recovery와 후속 재호출은 **0회**다. 생성 단계에 도달하지 않았다.

| 실측 항목 | 값 |
|---|---:|
| 논리 호출 / 상한 | 1 / 13 |
| 실제 provider turn | 1 |
| input tokens | 83,755 |
| cached input tokens | 37,632 |
| output tokens | 10,947 |
| reasoning output tokens | 6,972 |
| input + output | 94,702 |
| latency | 205,062ms |

reasoning tokens는 output의 부분집합으로 별도 합산하지 않는다. provider의 실측 usage는 제공됐지만 청구 금액은 제공되지 않아 금액으로 환산하지 않았다.

## 호출 소스와 최종 소스의 구분

진단 중 source와 workspace는 유지됐다. 실패 후 실행 당시 source 83개를 `executed-source/`에 복사하고 `executed-source-manifest.json`으로 당시 digest와 정확히 대조했다. 그 뒤 다음 두 구현만 보완하고 결정적 검증을 다시 수행했다.

1. 전역 검사 의무의 일부가 누락된 제출에서 존재하는 validation ID와 누락 finding을 함께 표현하도록 했다.
2. 결함별 탐지 평가에서 Core의 gate·severity·remediable 분류를 강제하지 않도록 했다.

이 보완은 prompt·출력 schema·고정 기대값을 바꾸지 않았다. 최종 코드로 원시 응답을 오프라인 재검증해도 같은 인용 오류로 거부된다. 보완 후 **새 실제 호출은 하지 않았으므로 최종 source의 실제 모델 검증은 미완료**다. 기존 `summary.json`을 수정하지 않고 `final-verification.json`에 이 차이를 남겼다.

| 결속 | digest |
|---|---|
| 실제 호출 source | `sha256:60fef3997c9de709a0990a358fd5c2856cff0f66d08f1df913f513cf3a486bbf` |
| 최종 source | `sha256:fc777cc48e00c6a8af3b65f4d2d4a533c21cad42ea559262c9f8ddf8f0c95db7` |
| preflight | `sha256:0c2aee6608f53a7600bfb119d9f9be772db86516071873077c1556b2e4829291` |
| 공유 prompt 계약 | `sha256:ab4bba9886d072e4a766f451fc4fc8ab7f06976d30954ebf58a04a3c38c2d22e` |
| 전체 planning 출력 schema 계약 | `sha256:62c95157377d2d845e30d4fec80f415ef37bf201a672df5eedadc6854152b9d2` |
| 실제 단일 Reviewer strict schema | `sha256:4e62b424a7c0c803622aff023f3776ef4039a8983dfc0db98df20c6d18498630` |
| 실제 request | `sha256:f9f29f9b7ad335fa2290e059115954a99949aacb97ea93fe77b556bff0ccefc2` |
| 실제 receipt | `sha256:23cd2cb732f4ebada002e0a46c87eb20931280036a0370c32d6987b97bd6ea83` |
| 최종 결정적 report | `sha256:67c9f299c21c75a9ec7339e94a13440698082755f57bd413bc36f5ed9e0e0865` |

실제 call은 `model_call_41ac64b1dd4e42d7b3745139153395e4`, thread는 `01a06cc8-f78a-7740-ab9e-12a00328d0e3`, turn은 `01a06cc8-f864-7b31-b203-5f688f182066`이다.

핵심 증거는 다음 파일이다.

- [호출 전 잠금](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/preflight.json)
- [실제 실패 receipt](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/calls/01-compact_plan_reviewer/failed.json)
- [원시 terminal 응답](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/calls/01-compact_plan_reviewer/terminal.json)
- [원문·추가 finding 대조](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/raw-clean-assessment.json)
- [실제 요청·receipt 결속 검증](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/binding-verification.json)
- [원본 실행 결과](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/summary.json)
- [최종 검증과 미실행 목록](D:/codex/flowmarshal/.flowmarshal-engine-eval/runs/r-s06-09-20260904-v1/final-verification.json)

## 잔여 범위와 Git

실제 모델의 인용 주소 선택, 명시 절차와 선택적·부가 검사 관계, runtime evidence catalog 경계의 정확성은 확보되지 않았다. `missing-link`, `stored-expanded`, 저장된 복수 결함, semantic 연결 제거 반례 등의 **실제 탐지 개선은 미검증**이다. 결정적 회귀 통과를 그 개선의 증명으로 사용하지 않는다. 나머지 12개 호출, 전체 S06, Plan 활성화, Worker 실행, 전체 qualification과 1.0 cutover는 수행하지 않았다.

R1~R3.1, v2 원본, 이전 probe.py·assess.py·oracle·threshold·결과는 보존했다. 운영 SQLite나 별도 오케스트레이션 원장을 만들지 않았다. 제품 지침에 따라 이 세션의 코드·회귀·설명 문서만 한 커밋으로 기록하고 비공개 권위 origin의 main에 push한다. 로컬 진단 원문과 source 스냅샷은 Git 제외 경로에 유지한다. 최종 commit·push receipt는 진단 root의 `git-result.json`과 이 작업의 최종 응답에 기록한다.
