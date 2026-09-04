# R-S06-05 — 상세 Plan의 검사 도구·phase 능력 보존

2026-09-04. [S06-RETRY-03](s06-planning-goal-reference-handoff.md)에서 실제 선택된 Plan이 등록 도구의 task phase에 없는 동작·호출 검사를 선언한 문제를 보완한다. 원문·Goal·등록 검사 자료·oracle·합격선·역할별 모델과 effort를 유지한다. 실제 진단 원장 복사본과 호출 원문은 `.flowmarshal-engine-eval/runs/r-s06-05-20260904*`에 보존하고 Git에서 제외한다.

**최종 source의 495개 테스트·결정적 Gate 5/5·legacy freeze 40개는 PASS지만 실제 진단은 FAIL이다.** 지침·입력 결속 회귀는 반영했으나 phase 의미 보존을 안정적으로 달성하지 못했다. 다음 작업은 **R-S06-06: 검사 ID 추적 입력 보완·phase 주장 대조·Reviewer의 finding 반환 보완**이다. 새 S06, Plan 활성화와 Worker 실행은 수행하지 않았다.

## 변경 내용

- [상세화·Reviewer 공통 지침](../src/flowmarshal/engine/planner_roles.py)은 등록 자료의 경로에서 관련 본문과 필요한 구현 분기를 읽어, 선언한 검사 목적을 해당 도구·phase가 실제 수행하는지 대조한다. 도구 이름·경로·evidence 종류만으로 능력을 추정하거나 다른 phase의 검사를 합쳐 설명하지 않는다.
- Task validation과 independent Goal Test의 범위 차이는 허용한다. 참조한 도구로 부족한 필수 검사는 별도 실제 검사 책임으로 보존하며, Task 검증을 약화하거나 Goal Test에만 넘기지 않는다. Goal이 Task에 요구하지 않은 검사를 일괄 추가하지 않는다.
- 도구·phase 참조는 검사 의미를 식별하는 계약이다. 구체적인 argv는 ready-time에 확정하지만, 이미 명시한 phase와 검사 목적의 충돌은 새 Plan Contract로 수정해야 한다.
- Skeleton·Plan 작성 draft의 `task_refs`와 Compiler 후 권위 Plan의 `task_ids`를 구분한다. Reviewer는 `Task.task_id` 결속과 대응하는 기여 집합을 확인하고, finding의 `affected_task_refs`에는 `Task.task_ref`를 사용한다.
- 장기 경계는 프로젝트 `AGENTS.md`, [권위 설계](orchestration-redesign.md), 시작 프로젝트의 `D:\codex\자동화템플릿\AGENTS.md`에 반영했다. Core schema의 값 제약, 상태 전이, finding 처리와 재시도 한도는 변경하지 않았다.

## 회귀 검사의 증거 범위

[새 회귀](../tests/test_engine_plan_validation_scope.py)는 실제 oracle의 `return 5` 반례로 task PASS·goal FAIL을 확인한다. 과장된 task phase, 정상 task·독립 goal phase, Goal 검사에서 잘못 선택한 task phase를 비교한다.

Scripted 역할 결과는 의미 탐지 품질을 증명하지 않는다. 이 회귀는 등록 reference의 selector·digest 전달, Draft→Compiler의 ID 변환, Reviewer의 참조와 Core 등록·활성화 판정의 결속을 검사한다. 실제 의미 탐지는 아래의 별도 역할 진단으로 확인한다. oracle, 공식 fixture, 합격선과 과거 원장은 수정하지 않았다.

## 1차 실제 진단

1차 source는 `sha256:1a22b94b70994aa7490da627c3afb1396b2a58ff575f1f95a39ef60803e2c85d`다. **495개 unittest·결정적 Gate 5/5·legacy freeze 40개는 PASS지만, 제한된 실제 진단은 FAIL**이다. 입력·현재 adapter 재생·정책·strict schema·receipt·원시 사용량의 결속 검증은 별도로 통과했다.

| 사례 | 관측과 독립 대조 |
|---|---|
| 정상으로 분류한 task·독립 goal phase | `PLAN_GOAL_COVERAGE_TASK_REF_MISMATCH`는 잘못된 지적. 권위 Plan의 정상 `task_ids`를 draft의 `task_refs`로 바꾸라고 요구했다. 이 입력의 별도 AC 추적 누락은 재진단에서 확인했다. |
| 과거 실제 task phase 과장 | `ENG_TASK_ORACLE_PHASE_SCOPE_MISMATCH`로 올바르게 거부. |
| Goal 검사에 task phase 지정 | `VAL_GOAL_PHASE_SCOPE_MISMATCH`로 올바르게 거부. |
| 같은 Goal·Skeleton의 새 상세화 | 실제 task 범위와 별도 independent goal phase, 필수 unittest·파일 범위·분리 Validator를 보존. 독립 Reviewer finding 없음. |

메인과 독립 보조 검토가 `PlanGoalCoverage.task_ids`, Compiler의 `task_refs → task_ids` 변환, plan gate의 역대응 검사를 대조해 정상 후보의 거부가 잘못됐음을 확인했다. 원래 판정·응답·당시 수정 source를 `source-snapshot`에 보존한 뒤 지침을 한 차례 보완했다. finding을 삭제하거나 정상 판정으로 바꾸지 않았다.

1차는 logical calls **5회**, provider turns **5회**, schema recovery **0회**다. input **225,739**, output **10,746**, 총 **236,485 tokens**이며 usage unavailable은 0건이다. 역할 latency 합은 **229,000ms**다. 메인·보조 에이전트 비용과 후속 S06은 포함하지 않는다. 과거 파일 **859개**와 원본 workspace를 보존했고, 진단의 새 원장 쓰기·Plan 선택·활성화·Worker 실행은 0건이다.

## 최종 재진단

재진단은 `.flowmarshal-engine-eval/runs/r-s06-05-20260904-v2`에 별도로 기록한다. 세 비교 Plan·Goal·State·Project Map·Skeleton과 원장 복사본의 byte를 1차 입력과 대조하고, 같은 criteria·호출 순서·모델 배정을 사용한다. 변경 source의 결정적 Gate를 먼저 실행하며 과거 source의 성공을 재사용하지 않는다.

재진단에서는 다음 두 문제를 서로 다른 범위로 확인했다. 이전 판단·입력을 소급 수정하지 않는다.

1. **진단 입력의 정상 분류 오류:** `VAL_COVERAGE_AC003_MISSING_TASK_ORACLE`는 현재 권위 계약에서 유효한 추적 누락이다. 정상으로 분류한 Plan의 첫 Task validation은 task phase의 기존 unittest 실행을 명시하지만, 그 ID는 `ac_003.validation_ids`에 없다. 전용 unittest 검사는 이미 연결돼 있으므로 실제 unittest 실행이 빠진 것은 아니다. 현재 계약은 적용되는 필수 검사 ID의 추적 연결을 별도로 요구한다. ID 추가는 새 검사를 실행하라는 요구가 아니며, 이 누락을 Goal 오완료 위험으로 과장하지 않는다. 메인과 두 독립 검토가 권위 문구·Core의 Task 완료와 criterion 집계를 대조해 최종 판단했다.
2. **Reviewer의 검출 실패:** 잘못된 Goal phase 후보에는 finding 없이 모든 rating을 1로 반환했다. 현재 `derive_candidate_decision`은 이를 **fitness score 25의 `admissible`**로 계산한다. 이는 저장 응답의 읽기 전용 Core 판정 재계산이며 실제 원장 등록·활성화는 아니다. 점수는 후보 간 순위일 뿐 직접 확인된 검사 범위 결함의 finding을 대신하지 않는다. 점수 하한을 새로 도입하거나 기존 판정을 사후 차단 상태로 바꾸지 않았다.

과거 Task phase 과장의 직접 모순은 재진단에서도 발견했다. 다만 finding 뒤에 덧붙인 “Task 완료 전에 포괄 동작 검사가 필수”라는 설명은 원래 Goal에서 그 검사 전체를 해당 Task에 요구한 것으로 확대하지 않는다. 유효한 핵심 근거는 명시한 도구·phase의 실제 능력과 검사 문장의 충돌이다.

새 상세 Plan은 `val_task_oracle_task`에서 task phase가 지원하는 범위 뒤에 양수·음수·0 동작 확인을 같은 수단의 결과로 덧붙여 **원래 phase 능력 과장을 재발시켰다**. 별도 입력을 실제 호출하고 기대 합과 비교한다는 책임은 없다. 별도 검사 책임은 같은 validation 문장에도 명시할 수 있으며 새 validation ID나 argv를 반드시 추가해야 한다는 뜻은 아니다.

새 Plan Reviewer는 `VERIFICATION_AC_TASK_SCOPE_LINK_MISSING`을 반환했다. Task의 파일 범위 검사 `val_task_scope`가 어느 AC에도 연결되지 않았다는 지적은 유효하며 후보를 차단한다. 그러나 phase 충돌은 finding으로 제출하지 않았다. **다른 결함으로 후보를 차단한 결과와 이번 핵심 결함의 검출 성공은 다르다.** 이 판단은 `independent-phase-assessment.json`에 저장했다. Worker 보고 검사 추가 여부는 별도 finding으로 확대하지 않았다.

| 최종 결속 | digest |
|---|---|
| source | `sha256:cf3ed30d4b5d676ca15533210fa402666c5724d6ba0bfa4c05b3ae72da944169` |
| 결정적 계약 | `sha256:e9a771afcbecc943b71c2e3dccadac31649edb24647ca8a2644bc95d6cca0809` |
| 결정적 report | `sha256:9e9fd6ac1694d2a023a292dadec712a6f043f194c3b728c0978cef809dc544af` |
| 진단 preflight | `sha256:628c8ec6ff9645fe31260fb8863ba16c77ae2c6d04bb7c689c60b62a4a1d8741` |

최종 진단은 logical calls **5회**, provider turns **5회**, schema recovery **0회**, usage unavailable **0건**이다. input **159,614**, output **21,700**, 총 **181,314 tokens**이며 역할 latency 합은 **431,967ms**다. 두 진단 합계는 **417,799 tokens**다. 과거 파일 **964개**와 workspace를 보존했고 원장 쓰기·선택·활성화·Worker 실행은 0건이다. latency 합은 전체 작업 wall time과 구분한다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-05-20260904-v2/verify.py
```

`verification.json.binding_verification_passed=true`는 입력·현재 adapter 재생·정책·strict schema·receipt·원시 사용량·원본 보존의 검증이다. `diagnostic_passed=false`와 함께 읽어야 한다. 두 실행의 원본 판정과 정상 입력 분류를 사후 PASS로 바꾸지 않았다. 전체 Planning·E2E·token/latency qualification과 1.0 cutover는 이 제한된 진단으로 대체하지 않는다.

## 다음 작업

R-S06-06에서는 다음 경계를 보완한다.

1. 정상 비교 입력의 `ac_003`에 이미 존재하는 Task oracle 검사 ID를 연결하고, 원본·변경 사유·차이를 새 입력으로 보존한다. 검사 도구와 합격선은 바꾸지 않는다.
2. 상세화가 명시한 수단의 실제 능력과 추가 검사 책임을 구별하도록 보완한다. 원래 Task 검증을 약화하거나 Goal 단계 검사를 무조건 Task에도 추가하지 않는다.
3. Reviewer가 직접 확인한 phase 모순을 finding으로 제출하게 보완한다. 낮은 rating이나 다른 결함의 finding만으로 해당 검출을 대신하지 않는다. 임의 점수 하한, finding 삭제와 모델 fallback은 추가하지 않는다.
4. 고정한 정상·오류 사례의 실제 진단과 새 상세화 검토가 통과한 뒤 같은 원문부터 새 S06을 수행한다. 이전 선택 Plan을 사후 편집·활성화하지 않는다.
