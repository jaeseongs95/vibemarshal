# S06 인계 — 실제 Goal 생성 통과, 선택 Plan 생성 실패

## 판정과 마지막 checkpoint

- 세션: `S06`, 2026-09-04. 승인된 로드맵의 [S05 입력 준비](s05-bugfix-trace-handoff.md) 다음 단위만 수행했다.
- **S06 판정: FAIL — 실제 Goal은 생성됐지만 선택 Plan이 없다.** `selected_activation_digest=null`이며 S07로 진입하지 않는다.
- 증거 도달 수준: 고정 원문 → 실제 Normalizer·독립 Goal reviewer → 실제 Skeleton 생성·검토·1회 보정 → 실제 Plan 상세화·검토 → Core의 `needs_revision` 판정까지다. 수동 Goal·Plan·ExecutionSpec으로 대체하지 않았다.
- 현재 승격 위치: M2/S06의 Planning 계약 경계에서 중단했다. 자연어 → Goal 완료 전체 Trace는 **INCOMPLETE**, 1.0은 **NO-GO**, package는 `flowmarshal-engine 0.2.0a1`을 유지한다.
- 마지막 checkpoint: 동일 원장의 활성 Goal 1개, Skeleton 2개, 미선택 `draft` Plan 1개, `pending` Task 계약 3개. Plan 활성화·Execution Spec·Attempt·Worker 실행·Task/Goal validation·GoalVerdict는 모두 0건이다.
- 다음 세션 하나는 **R-S06-01 — Skeleton 검토·보정의 독립 Goal Test 책임 경계 보완**이다. 이번 실패 원장·Plan을 고치거나 활성화하지 않는다.

## 고정 입력과 실제 호출

Engine source, S05 원문·역할 설정·oracle·초기 snapshot을 그대로 사용했다. CLI는 S05의 같은 workspace를 새 Engine DB에 등록했고, 기존 검증 도구의 경로·검사 의미만 별도 `reference`로 등록했다. `expected_goal_proposal`, 수동 Plan, `--outcome-file`은 모델 입력으로 주입하지 않았다.

| 항목 | 값 |
|---|---|
| 시작 HEAD | `36f54057e46036362c18d978c7761db5108dab53` |
| source digest | `sha256:d697a1cdc06faea9c5123ed696ef1cbd39b9f619a41a24348712173f5cec7937` |
| S05 입력 lock | `sha256:367e331f40f1ce38ffdbfaf813d11edb20859396677d34aaef2c2308b177ac42` |
| 원문 digest | `sha256:7a2e53cd7c7f0aebbda43ca6048972e122e9e08cab6c9679d26cab5d09e7e1a4` |
| 역할 설정 digest | `sha256:be726ac5b76c4b6e12172a5e6e4060cfc8d1ed832d2e5e31167333ad2d52564e` |
| 실제 inventory digest | `sha256:82e6bbcba85b38800c736c9f9809a493fbc9b3270cb53b514721a80839a49f14` |
| 등록 reference digest | `sha256:c0c58dc3ed0ff48b9f6161d1013d8fb8972549d20378322843f7279033b01ed8` |
| project ID | `project_c8b71450d4844abd982e1e5098b3f4d8` |
| Goal revision | `goal_revision_eed6d581dc0545a5893fd61242d2b9c6` |
| Goal digest | `sha256:3a0620ddac223eaa4dac741b9b0c583c678d339f9cc7938b037cc8b3eea8baa0` |
| 미선택 Plan revision | `plan_revision_8d07afdc2fe0441ea091374d830002c3` |
| 미선택 Plan activation digest | `sha256:94b19245343db779d63aa981f22b0748a47cf00ffc3f1cb41d3455b65364d91b` |
| State snapshot digest | `sha256:dea9a4085f84fcbf8ecbb6a1e151c6937ffea6cb7dbb4ce03d031896e19328eb` |
| Project Map digest | `sha256:81c35f584d385b265ca266b100da7f3352707c38b53b4b28520463a9d8bce485` |

호출 직전 실제 정책은 `:danger-full-access / never`였으며 S05와 같은 inventory·model lock·Codex 실행 파일을 확인했다. Normalizer·Skeleton generator/refiner·Plan expander는 Luna/high, Goal reviewer는 Sol/xhigh, Skeleton·해당 Plan reviewer는 Terra/high였다. Task 계약의 Executor Terra/high·Validator Sol/xhigh는 배정만 됐고 실행하지 않았다. 제품 기본 모델 설정을 변경하지 않았다.

실제 사용한 제품 경로는 `goal create --live --request <S05 원문>`과 `plan search --live --candidate-count 1`이다. 두 CLI는 종료 코드 0으로 결과를 반환했다. 이는 **평가 처리 완료**이지 Goal 달성이나 Plan 선택 성공을 뜻하지 않는다.

## 실제 Goal의 여섯 의미 대조

Goal reviewer는 finding 없이 rating을 제출했고 Core는 Goal을 등록·활성화했다. 활성 Goal의 준비 binding과 Normalizer/reviewer receipt가 일치한다. 별도 읽기 전용 검토에서도 Goal 의미를 막을 문제는 발견하지 못했다.

| S05 의미 | 실제 Goal 근거 | 이번 확인 |
|---|---|---|
| 양수·음수·0 덧셈 | `ac_001`, `ac_002` | 보존 |
| 공개 이름·annotation·위치/키워드 호출 | `ac_003`, `constraint_002` | 보존 |
| add 구현만 최소 변경·파일 보존 | `ac_004`, `constraint_001~003`, `minimal_change`, ProjectProfile | 보존 |
| 기존 unittest 실제 실행 | `ac_005.validation_intent` | 보존 |
| 분리 Validator의 직접 evidence 검토 | `constraint_004` | 보존; 같은 의미를 별도 Hard AC로 중복 요구하지 않음 |
| 모든 Task 뒤 같은 workspace의 독립 Goal Test | `ac_006` | Goal에서는 보존, 이후 Planning의 책임 투영에서 실패 |

기대 proposal의 예시 비목표 목록을 그대로 복제하도록 요구하지 않았다. 실제 원문의 외부 서비스 변경·배포·의존성 추가 금지는 보존됐다. 기대 Goal proposal과의 byte 동일성은 판정 기준이 아니다.

## 실패 흐름과 해석

| 단계 | 실제 관측 |
|---|---|
| Skeleton v1 | `task_change_add → task_validate_change`, 결정적 finding 0개 |
| v1 semantic review | `SKEL_GOAL_001`: 독립 Goal Test가 Skeleton DAG에 없다는 이유로 후속 작업 요구 |
| 1회 refinement | `task_goal_test`를 추가한 3-Task DAG로 변경 |
| v2 review | finding 0개, Core `admissible` |
| Plan 확장 | 위 3개 Task를 보존하고 별도 `integration_validations.goal_independent_validation`도 생성 |
| 최종 Plan review | `VERIFICATION_GOAL_TEST_SELF_DEPENDENCY` 제출 |
| Core 최종 처리 | `needs_revision`, score 없음, 원장 Plan `draft`, 선택 digest null |

직접 문제는 `task_goal_test.preconditions[all_task_validation_available]`가 “모든 Task 검증 완료”를 요구하면서, 자기 자신도 그 Task 집합에 포함된다는 **자연어 계약의 자기의존**이다. 별도 integration validation이 있는데 일반 Task에서도 Goal Test를 중복 수행하도록 모델링했다.

원시 finding은 “ready가 될 수 없다”고도 표현했지만, 이를 실제 runtime 교착의 관측으로 채택하지 않는다. 명시적 DAG는 순환이 없고 결정적 Gate를 통과했다. 현재 `EngineService`의 ready 전이는 dependency producer의 완료를 조회하며 자연어 precondition을 해석하지 않는다. **검증된 사실은 semantic 계약 충돌과 미선택이지 실제 DAG cycle·실행 교착이 아니다.** 원시 finding은 원문 그대로 보존했다.

초기 reviewer 요구는 기존 역할 책임과 충돌한다. [planner_roles.py](../src/flowmarshal/engine/planner_roles.py)의 generator는 Goal Test용 중복 Task가 불필요하다고 명시하고, Skeleton reviewer도 상세 `integration_validations`의 부재를 현재 결함으로 삼지 않도록 지시한다. 그럼에도 reviewer가 별도 노드를 요구했고 refiner가 이를 수용했다. refiner에는 generator와 같은 명시적 Goal Test 책임 안내가 없다. Plan expander는 Task 집합·목적·dependency를 보존해야 하므로 추가된 Task를 임의 제거할 수 없었다. 원인은 이 **검토 → 보정 → 상세화 경계**에서 조사하며, source 하나의 문구 누락만으로 모델 실패의 전부를 설명했다고 단정하지 않는다.

Plan의 `integration_validations` 자체는 모든 AC, `evidence_mode=independent`, 새 `command/test/file/diff`를 포함한다. 따라서 문제를 “독립 Goal Test 계약이 전혀 없음”으로 분류하지 않는다. `task_goal_test`를 일반 Task로 중복·재귀 배치한 것이 핵심이다. Task 실제 검증, task-less Goal evidence, 최종 GoalVerdict와 재개 검증은 아직 수행하지 않았다.

Plan payload의 schema 상태 `ready`와 원장의 실행 가능 상태를 혼동하지 않는다. Core는 검토 결과에 따라 DB 상태를 `draft`로 저장했고, `activate_plan`은 원장 `ready` 및 Core `admissible`을 모두 요구한다. 활성화 명령을 시험 호출하지 않고 읽기 전용 대조로 확인했다.

## 원장·사용량·결정적 검증

실제 logical 역할 호출은 총 **8회**, provider turn은 **10회**다. 이 중 Planning은 6회이며 Skeleton version 2개·Plan 1개다. Goal review와 Skeleton review에서 schema recovery가 각각 1회 있었고 최종 structured 출력은 모두 성공했다. 이는 semantic 합격을 뜻하지 않는다. 복구 전 raw 응답·오류 전문은 기존 CLI 산출물에서 확보하지 못했으므로 그 두 복구의 구체 원인을 추정하지 않는다.

| 단계 | logical calls | provider turns | input tokens | output tokens |
|---|---:|---:|---:|---:|
| Goal 정규화 | 1 | 1 | 24,125 | 1,659 |
| Goal 검토 | 1 | 2 | 55,513 | 1,120 |
| Skeleton 생성·보정 | 2 | 2 | 54,752 | 3,333 |
| Skeleton 검토 | 2 | 3 | 90,058 | 1,080 |
| Plan 상세화 | 1 | 1 | 28,760 | 3,685 |
| Plan 검토 | 1 | 1 | 30,883 | 1,215 |
| 합계 | 8 | 10 | 284,091 | 12,092 |

총 296,183 tokens는 **기존 역할 receipt에 기록된 이번 준비·Planning 부분 합계**다. 원장의 8개 고유 logical call과 대응하며 별도 보조 검토 에이전트·메인 세션 사용량은 포함하지 않는다. 역할 receipt의 `usage_available`은 모두 true이지만 원시 turn별 정산 완결성이나 M3 Goal 전체 비용 검증까지 주장하지 않는다. 특히 복구 turn은 누적 역할 receipt이며 개별 turn 비용으로 임의 분할하지 않았다. Worker·Validator·Goal Test 비용과 미관측 항목을 0 실측으로 채우지 않는다.

`verify_trace.py`는 원본·초기 snapshot·workspace·등록 reference·source lock, Goal 원문, 실제 adapter로 재구성한 Normalizer/reviewer 요청 digest, receipt와 usage binding, DB의 Goal·Plan payload와 Core decision, SQLite integrity/foreign key/application ID, History hash chain을 별도 프로세스에서 대조했다. 추가 provider 호출과 Core mutation은 없다. 3개 Task는 모두 `pending`이며 활성화·명세·Attempt·runtime 효과·validation·GoalVerdict 0건을 확인했다.

전체 **482개 테스트와 결정적 Gate 5/5는 PASS**다. compileall, 전체 unittest, pip check, synthetic lifecycle, legacy freeze 40개 파일 검사가 모두 통과했다. 이는 S06 실패를 상쇄하거나 전체 Planning Gate를 대신하지 않는다.

- 결정적 계약 digest: `sha256:16b0ba6089c378cdf6b687c8b7f76b9bdb96253edbf94f3a0200d1c461ca6db8`
- 이번 결정적 report digest: `sha256:772859527725e449408ea89050a3e19082acacc0f77df2c19d332074212d90d2`
- Engine 구현·권위 문서·AGENTS.md·fixture·oracle·합격선·과거 실행은 변경하지 않았다. 이번 커밋 대상은 인계·현황 문서뿐이다.

## 로컬 수집 도구의 한계와 보존

두 수집 표현 오류를 제품 실패와 분리했다.

1. Goal CLI의 raw stdout은 Windows CP949였는데 부모 wrapper가 UTF-8로 읽어 CLI 종료 0 뒤 wrapper 종료 1이 발생했다. raw stdout·completion을 보존하고 CP949로 해석해 strict `GoalPreparationOutcome` 및 원장과 대조했다. UTF-8 사본을 추가했으며 **Goal을 재호출하지 않았다**. 이후 Plan 자식 프로세스에는 `PYTHONIOENCODING=utf-8`을 명시했다.
2. preflight의 `observed_at`은 hash 계산 때 datetime/canonical UTC였지만 파일에는 `str(datetime)`으로 직렬화됐다. 원본을 덮어쓰지 않고 최초 도구 코드에 따른 datetime 타입을 복원해 기록된 digest를 재현했다. 저장 JSON 자체의 canonical digest와 원래 typed digest가 다름을 `verification-summary.json`에 명시했다. source·입력 파일별 byte hash는 모두 일치한다. 다음 harness에서는 hash와 저장에 같은 canonical 변환을 사용해야 한다.

이 보정은 결과·평가 기준을 바꾼 것이 아니며 원시 증거의 형식을 해석한 것이다. 두 오류와 수정하지 않은 최초 harness를 provenance로 남긴다.

## Evidence와 다음 세션 하나

로컬 root: `D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\s06-bugfix-trace-20260904`.

- `flowmarshal-engine.sqlite3`, `artifacts/`: 같은 프로젝트의 실제 Core 원장과 artifact.
- `preflight.json`, `profile.json`, `validation-reference.md`: 호출 전 source·역할·모델·등록 자료 결속.
- `goal.stdout.json`(CP949), `goal.utf8.json`, `plan.stdout.json`(UTF-8): 실제 결과 원문과 strict 해석 사본.
- `*.started.json`, `*.completed.json`, `*.stderr.log`: 실행 인자·시점·종료 코드·원문 digest. 시작 marker가 있으면 자동 재호출하지 않는다.
- `harness-encoding-observation.json`, `run_stage.py`, `continue_plan.py`: 수집 오류와 재호출 없는 연결 과정.
- `verification-summary.json`, `verify_trace.py`, `deterministic/`: 원장·입력 대조와 전체 결정적 검증.

읽기 전용 재검증 명령:

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/s06-bugfix-trace-20260904/verify_trace.py
```

**다음은 R-S06-01 하나다.** 초기 Skeleton reviewer의 과잉 요구와 refiner의 Goal Test 책임 누락을 이번 raw finding·두 Skeleton·Plan에 근거해 최소 수정한다. generator/reviewer/refiner/expander가 같은 책임 경계를 해석하도록 연결하고, Task coverage가 독립 Goal Test의 직접 실행 책임을 뜻하지 않음을 회귀로 검증한다. 정상 Task validation은 허용하되 모든 Task 뒤의 Core Goal Test를 일반 Task로 재귀 배치하는 사례와 대비해야 한다.

특정 finding 코드·문자열만 지우거나 semantic finding을 묵살하는 우회, Task 이름 기준의 전면 금지, 수동 Plan 교체, 재시도 한도 확대·모델 자동 대체·합격선 완화는 하지 않는다. 이번 사례의 두 Task 구성을 일반 고정 정답으로 강제하지 않는다. 필요한 source 변경은 별도 Repair와 새 source 계약에 남긴 뒤 새 S06 평가로 확인한다. 기존 source의 완료 checkpoint를 새 source의 전체 실행 증거로 재사용하지 않는다.

Repair의 작은 회귀가 완료돼도 S06의 실제 선택 Plan을 확보하기 전에는 S07을 시작하지 않는다. S14 이후 전체 고정 평가·M3 계측·비용 Gate·1.0 승격은 여전히 후속 범위다.
