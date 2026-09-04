# R-S06-01 인계 — Planning의 독립 Goal Test 책임 경계 보완

## 판정과 다음 작업

- 세션: `R-S06-01`, 2026-09-04. [S06 실패](s06-planning-handoff.md)의 다음 Repair 단위를 수행했다.
- **Repair 검증 PASS**: 최종 source의 484개 테스트·결정적 Gate 5/5와 제한된 실제 역할 진단 5/5를 통과했다.
- 수정 범위는 Skeleton 생성·검토·보정과 Plan 상세화·검토의 책임 안내 및 provider 필드 설명이다. Core 상태 전이·권위 schema·DB·공식 fixture·oracle·합격선·모델 배정·재시도 한도는 바꾸지 않았다.
- 이번 실제 진단은 과거 Goal·State·Skeleton·finding을 입력으로 역할 경계를 비교한 것이다. **새 Normalizer·generator부터 시작한 전체 Planning 실행이나 Plan 선택이 아니다.** 생성된 진단용 Plan은 원장에 등록하거나 활성화하지 않았다.
- 기존 S06은 선택 Plan 없는 FAIL로 보존한다. 자연어 → Goal 완료 Trace는 `INCOMPLETE`, 제품은 `flowmarshal-engine 0.2.0a1`, 1.0은 `NO-GO`다.
- **다음 세션은 새 source 계약의 S06 재평가 하나다.** 실제 선택 Plan을 확보하기 전에는 S07로 진행하지 않는다.

## 변경 내용과 근거

S06에서는 Task의 AC coverage를 그 Task가 독립 Goal Test까지 직접 실행해야 한다는 의미로 해석했다. 초기 Skeleton reviewer의 추가 Task 요구가 refiner와 expander를 거쳐 일반 Task의 “모든 Task 검증 완료” 선행조건으로 전파됐다. 실제 DAG cycle이나 runtime 교착이 관측된 것은 아니다.

[planner_roles.py](../src/flowmarshal/engine/planner_roles.py)에 공통 지침을 두고 generator, Skeleton reviewer, refiner, expander와 Plan reviewer에 적용했다.

- `contributes_to`와 Task coverage는 AC에 기여하는 산출물·근거의 연결이다. 모든 연결 AC의 검사 절차를 해당 Task가 직접 실행한다는 뜻은 아니다.
- 정상적인 테스트 작성·실행과 선행 산출물 검토 Task를 허용한다. 같은 대상을 검사한다는 이유만으로 Task validation과 Goal Test를 중복으로 판정하지 않는다.
- 모든 Task 완료 후 Core의 독립 Goal Test는 `integration_validations`에 두고 Plan의 validation ID로 연결한다. 일반 Task가 자신을 포함한 전체 Task 검증이나 이후 Goal Test 결과를 기다리게 하지 않는다.
- Goal과 AC 연결로 이미 전달된 요구를 선택 `detail_requirements`에 반복하지 않았다는 이유로 Skeleton을 차단하지 않는다. 미래 단계의 가상 누락은 현재 결함의 직접 근거가 아니다.
- 실제 AC 기여 누락, 상충하는 Task 요구, 상세 Plan의 독립 검사·evidence mode·validation 연결 결함은 계속 검토한다. expander의 Task 집합·목적·dependency 보존 검사도 유지한다.

provider 전용 `contributes_to`, `detail_requirements`, Skeleton coverage와 Plan coverage의 필드 설명을 같은 의미로 보완했다. 장기 원칙은 [AGENTS.md](../AGENTS.md)와 [권위 설계](orchestration-redesign.md)에 반영했다. finding 코드나 Task 이름을 기준으로 분기·삭제하는 처리는 없다.

## 회귀와 실제 진단

[역할 adapter 회귀](../tests/test_engine_role_adapters.py)에 두 테스트를 추가했다. 첫 테스트는 과거 finding을 refiner에 그대로 전달하고, 기존 Task의 기여·상세화 책임으로 보완한 뒤 재검토·Plan 연결을 유지하는지 검사한다. 다른 테스트는 정상 검사 Task의 이름이 `task_goal_test`여도 허용하고, 자기의존 또는 독립 검사 대신 `task_aggregate`를 배정한 실제 finding은 Core가 `needs_revision`으로 유지하는지 검사한다. scripted 결과의 전달·판정 검사이며 실제 모델 품질의 대체 증거로 쓰지 않는다.

최종 실제 진단은 고정된 과거 입력과 같은 역할 설정을 사용했다. 호출 직전 실제 `:danger-full-access / never`, App Server inventory와 model lock을 확인했다. refiner·expander는 Luna/high, 해당 reviewer는 Terra/high였고 fallback은 없었다. generator의 새 생성 호출과 Normalizer는 이 진단에 포함하지 않았다.

| 진단 조건 | 최종 관측 |
|---|---|
| 기존 정상 Skeleton 검토 | `detail_requirements`가 비어 있어도 finding 없이 rating 반환 |
| 과거 raw finding을 별도 주입한 refiner | 기존 2개 Task·목적 문장·입출력·AC 기여·dependency 유지, 독립 검사 책임만 명확화 |
| 보정 후보의 상세화 | 기존 Task 유지, 독립 `iv_independent_goal_test`와 실제 validation ID 연결 보존 |
| 새 상세 Plan 검토 | 결정적 Gate와 독립 의미 검토에 finding 없음 |
| 기존 불량 Plan의 재검토 | 직접 Plan evidence로 일반 Task의 재귀 배치와 독립 검사 ID 미연결을 계속 거부 |

최종 Plan의 독립 검사에는 실제로 6개 AC가 모두 연결됐으며, 새 command/test/file/diff evidence와 기존 unittest 재실행·동작·공개 계약·파일 보존 검사를 명시했다. 이 관측은 해당 검사 계약의 생성·검토 증거이고 Worker·검사 명령의 실제 실행 증거가 아니다. 일반적인 정답을 2개 Task나 특정 ID로 고정하지 않는다.

## 첫 진단의 실패와 진단 코드 보정

첫 source의 실제 진단은 원래 기준에서 **FAIL**이며 그대로 보존했다. 초기 reviewer가 `GOAL_INDEPENDENT_TEST_UNSPECIFIED`로 선택 detail requirement의 반복 부재를 차단했다. Goal·AC 연결에 이미 있는 요구인데도 “상세화에서 누락될 수 있다”는 가정을 근거로 삼았다. 이를 직접 evidence 원칙에 맞게 보완한 뒤 새 source·별도 디렉터리에서 한 번 더 진단했다.

첫 진단 코드에도 두 과잉 조건이 있었다. 이 오류를 모델 실패와 구분하고 `contract-correction.json`에 원인과 근거를 고정했다.

1. refiner의 목적 **문장 동일성**을 의미 보존으로 취급했다. 첫 결과는 목적을 재서술하면서 상세 요구에 기존 검사 범위를 보존했다. 두 번째 계약에서는 구조·기여·입출력·dependency와 변경 문장·Goal·Plan review를 대조한다. 최종 결과의 목적 문장은 실제로 원본과 동일했다.
2. **모든 AC의 독립 재검사**를 강제했다. 고정 Goal의 `ac_005`는 기존 unittest 실행이고, `ac_001~004`와 `ac_006`에는 독립 Goal 검사 책임이 있다. 두 번째 계약은 전체 AC의 유효 validation 연결과 명시된 독립 범위를 검사하며, `ac_005`의 추가 독립 검사도 허용한다. 등록된 S05 oracle의 실제 검사 항목은 수정하거나 생략하지 않았다. 최종 결과는 추가로 `ac_005`까지 독립 검사에 연결했다.

공식 qualification oracle·합격선을 바꾼 것이 아니며, 첫 진단의 코드·기준·raw 결과·FAIL을 덮어쓰지 않았다. 별도 읽기 전용 검토에서 source 책임 경계와 진단 기준의 수정 근거를 대조했다.

## 검증 lock과 사용량

| 항목 | 값 |
|---|---|
| 시작 HEAD | `708a29bce2ca8b0cec09e3a62297a981ac20ed77` |
| 첫 진단 source | `sha256:fc1b6c0d4a51071f9a73474e9a5305779a1b8e379d88700ec376e01fc36a9151` |
| 최종 source | `sha256:0e5fbef54b55c153729daa025e41064c59a363ccde280123785aa44d9cbb70b0` |
| 최종 진단 preflight | `sha256:91371f62d88a0464eca58748d5020bcded7f8ef793bb53c05f2c50797b58654e` |
| 최종 결정적 계약 | `sha256:243671336ce5095fee2573bf1e39af0e6587b2a730b65a162b2e1a0c8ae46594` |
| 최종 결정적 report | `sha256:6d4022b595d61a5b50c590532c052fc2f195387add627b3b0022650a934da6fd` |

최종 source에서 compileall, 전체 484개 unittest, pip check, synthetic lifecycle과 legacy freeze 40개 검사가 모두 통과했다. 첫 source의 결정적 PASS는 최종 source의 증거로 재사용하지 않았다.

| 실행 | logical calls | provider turns | input tokens | output tokens | schema recovery |
|---|---:|---:|---:|---:|---:|
| 첫 진단 | 5 | 5 | 147,582 | 9,158 | 0 |
| 최종 진단 | 5 | 7 | 223,626 | 11,502 | 2 |
| 합계 | 10 | 12 | 371,208 | 20,660 | 2 |

총 391,868 tokens는 이번 제한 진단의 역할 receipt 합계다. 메인·보조 에이전트 비용과 미실행 Worker·Validator·Goal Test 비용은 포함하지 않는다. 각 receipt는 usage available이지만, 복구 turn 비용을 개별 turn에 임의 분배하지 않았으며 M3 Goal 전체 정산 완료를 주장하지 않는다. 최종 schema recovery 두 건의 복구 전 응답 전문은 별도 확보하지 않았으므로 원인을 추정하지 않는다.

별도 프로세스의 `verify.py`가 두 진단의 source·preflight·요청·출력·receipt·schema·inventory 결속과 원본 62개 파일의 byte hash를 대조했다. 기존 S05·S06 원장·입력·workspace·결과는 그대로이고 활성화·Execution Spec·Attempt·runtime 효과·validation·GoalVerdict는 0건이다.

검증 도구에서 canonical 저장의 key 정렬이 strict schema의 `required` 배열 재구성 순서에 영향을 주는 것을 확인했다. 저장 schema와 provider 모델 schema의 내용 digest가 같음을 먼저 확인하고, 원래 선언 순서로 생성한 strict schema digest가 receipt와 일치함을 검증했다. 요청·결과를 수정하거나 모델을 재호출하지 않았다. 이후 수집기는 호출 전에 strict schema 자체도 별도 보존하는 것이 적절하다.

## 로컬 evidence와 다음 S06

- 첫 진단: `.flowmarshal-engine-eval/runs/r-s06-01-20260904`
- 최종 진단: `.flowmarshal-engine-eval/runs/r-s06-01-20260904-v2`
- 각 디렉터리의 `preflight.json`, `call-*.request/result/started.json`, `progress.jsonl`, `summary.json`은 입력 lock과 실제 호출 근거다. 진단 코드·중간 후보·raw review도 보존했다.
- 최종 디렉터리의 `contract-correction.json`, `verification.json`, `deterministic/`는 기준 보정 근거·별도 프로세스 대조·최종 Gate다. 실제 실행 이력은 Git에서 제외한다.

읽기 전용 재검증:

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-01-20260904-v2/verify.py
```

다음 S06에서는 S05의 고정 원문·fixture·oracle·역할 설정을 보존한 채 **현재 source의 새 입력 lock과 별도 Engine 원장**을 준비한다. 기존 S05 lock의 source digest를 덮어쓰거나, 이번 과거 Goal·refined Skeleton·진단 Plan을 실제 생성 출력으로 주입하지 않는다. `goal create --live`부터 새 Goal과 독립 review를 확보하고 `plan search --live --candidate-count 1`로 실제 선택 Plan까지 확인한다. 수집기는 UTF-8과 동일 canonical 직렬화를 사용하고 시작 intent가 있는 호출을 자동 반복하지 않는다. 이후 S07 활성화·실행은 그 실제 선택 revision과 digest를 기준으로 한다.
