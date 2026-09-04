# S06 재실행 — 계획 선택 후 검사 phase 불일치 확인

2026-09-04, `S06-RETRY-03`. [R-S06-04](r-s06-04-handoff.md)의 최종 source에서 고정 원문부터 새 원장을 사용해 정규화·독립 Goal 검토·Skeleton·Plan을 실행한다. 실제 산출물은 `.flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904-v4`에 보존한다.

**Core는 단일 Task Plan을 `admissible`로 선택했으나, 별도 의미 대조에서 실제 검사 도구의 phase 능력과 Plan 문장 사이의 충돌을 확인해 S06은 FAIL이다.** 실제 선택·rating·원장 상태는 변경하지 않았다. Plan 활성화와 Worker 실행은 없으며 다음 작업은 **R-S06-05 — 상세 Plan의 검사 도구·phase 능력 보존**이다. 전체 Trace는 `INCOMPLETE`, 1.0은 `NO-GO`를 유지한다.

## 입력과 권위 경계

- source: `sha256:e72a69e3ec7a1b1446632604bd152c7dd910093da1b45e3cdb47946f09d37eff`.
- preflight: `sha256:cb4ab6e03e2764dfd8a04bef5e8154dfcee364576a2f3cb3c7da7148af812922`.
- 과거 파일 785개와 초기 workspace의 세 파일을 보존하며, 새 SQLite 원장과 별도 artifact root를 사용한다.
- 원문·Profile·등록 검증 자료·fixture·oracle·threshold·역할별 모델·effort를 S05 및 이전 S06 lock과 대조한다. `expected_goal_proposal`을 모델에 주입하거나 실제 Goal·Plan으로 대체하지 않는다.
- 같은 source의 494개 unittest·결정적 Gate 5/5·legacy freeze 40개 결과를 digest로 결속한다. source가 같아 이를 중복 실행하지 않는다.
- Plan 선택과 사용자에 의한 정확한 Plan revision·digest 활성화를 구분한다. 현재 단계의 산출물로 Worker나 Goal 완료를 선언하지 않는다.

## Goal 의미 대조

실제 Goal `goal_revision_1deefe6198134a989283d48463a19995`, definition digest `sha256:e43e73deb801e42b7762d474afcb62917d19d39dbbfbfb54e4f90424eba2bedd`는 독립 Goal Reviewer의 finding 없이 `ready`가 됐다. 메인과 보조 검토가 고정된 여섯 의미를 전체 AC·제약·검증 목적과 대조했다.

| 고정 의미 | 실제 Goal의 보존 위치 |
|---|---|
| 양수·음수·0 정수 합 | `ac_001`, 등록 oracle의 task·goal phase 및 고정 입력 검사 |
| 함수명·인자·annotation·위치/키워드 호출 | `ac_002`, 실제 함수 선언과 호출 검사 |
| add만 최소 수정, 테스트·지침·파일 집합 보존 | 수정 범위·보존 제약과 최소수정 preference |
| 기존 unittest의 실제 실행·통과 | `ac_003`의 새 프로세스 실행과 Task 검증 제약 |
| 실행 역할과 분리된 직접 evidence 검토 | Task 검증 제약의 분리 Validator |
| 모든 Task 후 동일 workspace의 독립 Goal Test | `ac_004`와 검증 제약, 새 command/test/file/diff, task_id null |

AC 개수·ID와 기대 proposal의 byte 일치를 요구하지 않는다. `mutation_policy=scoped_change`는 add 구현 하나로 제한하는 명시적 제약과 최소수정 preference 안에서 해석하며 변경 범위 확대를 뜻하지 않는다. 외부 변경·배포와 로컬 의존성 추가는 분리 금지됐고 허용 외부 효과·질문·가정은 없다.

## 최종 원장·Plan 대조

실제 선택 결과는 다음과 같다. 이 식별자는 실패 분석의 기준이며 활성화 승인 요청이 아니다.

- Plan revision: `plan_revision_dce63c176c8f4379b587b251aab3ded2`.
- activation digest: `sha256:874a46a2fa115610196f8962ddf10dce7a66a4b59fe5e2966501457ca8e44c10`.
- Task: `task_change_add` / `task_60f8eabb986e4083a301ea7959f8b153`, 상태 `pending`.
- Core 판정: `admissible`, finding 0개, fitness score 75. Skeleton·Plan 후보는 각각 1개다.
- Plan 활성화·Execution Spec·Attempt·Worker runtime intent/receipt·validation·evidence·GoalVerdict는 모두 0건이다.

완료된 DB를 byte 복사한 뒤 `mode=ro&immutable=1`로 integrity·foreign key·Engine application ID, Profile revision·등록 source·Goal preparation binding·Plan·Task·결정적 Gate·usage·History chain 15건을 확인했다. Core가 기록한 `admissible`과 선택 digest가 CLI 결과와 일치한다. 실제 원장과 관측 결과는 수정하지 않았다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904-v4/verify_trace.py
```

`verification-summary.json.verification_passed=true`와 `selected_plan_available=true`는 수집·결속 검증이다. 별도 `semantic-assessment.json`의 `plan_semantic_passed=false`, `s06_passed=false`를 함께 보존한다. 실제 Planner의 무결함 판정을 독립 의미 검토의 합격으로 자동 전환하지 않는다.

## 직접 확인한 검사 계약 충돌

`val_task_add_behavior_contract.statement`는 등록 `oracle.py`의 **task phase**가 양수·음수·0의 합산 결과와 위치·키워드 호출을 실제 검사한다고 명시했다. 그러나 [oracle 구현](../tests/fixtures/engine/bugfix-trace/oracle.py)은 `phase == "goal"`에서만 고정된 7개 쌍의 두 호출 방식을 실행한다. task phase는 파일 집합·보존 해시·add 본문 밖 AST·공개 시그니처와 기존 unittest를 검사한다.

[기존 회귀](../tests/test_engine_bugfix_trace_fixture.py)의 `test_goal_rejects_constant_that_passes_existing_unittest`도 `return 5` 구현이 task phase를 통과하고 goal phase에서 거부됨을 확인한다. 이 회귀는 현재 source의 494개 테스트에 포함돼 통과했다. 새 테스트나 oracle 수정 없이 실제 검사 능력 차이를 직접 확인했다.

메인과 독립 보조 검토는 `PLAN_TASK_PHASE_CAPABILITY_MISMATCH` 한 건을 유효한 의미 결함으로 남겼다. 이는 호출 argv가 아직 없다는 문제가 아니라 Plan이 특정 도구·phase에 없는 검사 능력을 부여한 모순이다. 같은 의미의 Execution Spec에서 명령만 바꾸는 방식으로 숨기지 않으며 **새 Plan Contract revision**이 필요하다. Goal·원문·oracle·합격선은 변경할 이유가 없다.

독립 보조 검토가 처음 추가한 semantic Validator의 `test` 입력 누락 지적은 채택하지 않았다. 실제 Runtime은 현재 실행의 `file/diff/command/test/build`를 semantic catalog에 모두 전달하며, Plan에는 필수 unittest 검사가 존재한다. `file·diff`를 강조한 문장은 test evidence를 제외하지 않는다. 직접 실행 경로를 대조한 뒤 보조 검토도 이 지적을 철회했다. 불확실하거나 상관된 finding을 늘리지 않는다.

## 실제 사용량과 다음 작업

| 구간 | logical calls | provider turns | input tokens | output tokens |
|---|---:|---:|---:|---:|
| Goal 정규화·검토 | 2 | 3 | 83,552 | 2,490 |
| Skeleton·상세 Plan | 4 | 5 | 154,516 | 5,941 |
| 합계 | 6 | 8 | 238,068 | 8,431 |

총 **246,499 tokens**, schema recovery 2회, usage unavailable 0건이다. 역할 latency 합계는 184,064ms이며 전체 wall time과 구분한다. 메인·보조 에이전트나 앞선 R-S06-04 비용은 포함하지 않는다. 최종 schema failure는 0건이고 복구 전 응답·오류도 보존했다.

다음 R-S06-05에서는 상세화와 Reviewer가 등록 검사 도구·phase의 실제 범위를 대조하게 보완하고, 이번 실제 선택 Plan을 phase 범위 오판 회귀의 provenance로 사용한다. 정상 task phase·과장된 task phase·독립 goal phase의 paired 진단을 수행한다. Task 검사의 문장을 실제 지원 범위로 고치거나 별도 실제 Task 검사를 명시하는 새 Plan을 확보한 뒤에만 S07 활성화·실행으로 진행한다. 현재 원장·선택 결과를 사후 편집하거나 이번 Plan을 그대로 활성화하지 않는다.
