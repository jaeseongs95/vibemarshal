# R-S06-07 — 검사 연결·결과 제출 경계와 Reviewer 모델 비교

2026-09-04. [R-S06-06](r-s06-06-handoff.md)의 두 잔여 결함을 provenance로 생성·검토 지침과 합성 회귀를 보완했다. **497개 테스트·결정적 Gate 5/5·legacy freeze 40개와 실제 호출 결속 검증은 PASS지만, 제한 진단은 FAIL이다.** 전체 Trace는 `INCOMPLETE`, 1.0 cutover는 `NO-GO`를 유지한다.

새 상세화는 `ac_003` 검사 연결과 Worker·Validator 결과 순서를 보존했다. 그러나 `ac_004`가 명시한 task phase 검사 ID를 누락했다. 일반 Reviewer는 고정 비교의 검사 연결 결함을 놓쳤고, 전역 semantic Task 검증 의무를 AC별 필수 연결로 확대하여 정상 후보를 거부했다. 추천 분석 항목은 **R-S06-08: AC가 명시한 검사 책임과 전역 Task 검증 의무의 연결 경계 정밀화**다.

## 변경과 결정적 검증

- [planner_roles.py](../src/flowmarshal/engine/planner_roles.py)에 생성·검토가 공유하는 검사 연결 대조와 Worker·Validator 결과 순서 지침을 추가했다. 복합 검사 문장 전체와 Goal의 검사 목적·적용 범위를 대조하고, 실행 누락과 기존 검사 ID 연결 누락을 구분한다.
- Task 완료에 독립 Validator 통과를 요구하는 정상 계약을 유지한다. Worker 응답을 입력으로 이후 수행하는 Validator의 결과를 같은 Worker가 미리 제출하도록 요구하는 충돌을 구분한다. 작성·검토 schema에는 이 설명을 추가했으며 필드·값 제약·Core 판정 정책은 유지했다.
- [합성 회귀](../tests/test_engine_plan_result_boundaries.py)는 정상·복합 검사 ID 연결 누락·미래 Validator 결과 요구의 3사례를 한 parameterized 테스트로 검사한다. 다른 unittest·Goal ID가 연결되어 있어도 누락 입력을 Compiler가 자동 보정하지 않고 Reviewer에게 그대로 전달하며, scripted finding이 Core의 활성화 거부로 이어지는지 확인한다. 실제 모델의 의미 탐지 성능을 증명하는 테스트는 아니다.
- 프로젝트와 시작 프로젝트의 `AGENTS.md`, [권위 설계](orchestration-redesign.md)에 장기 경계를 맞췄다. 기존 phase 회귀·권위 validation schema·oracle·합격선은 유지했다.

최종 source에서 compileall·497개 테스트·pip check·synthetic lifecycle·legacy freeze가 모두 통과했다. 동결 파일 40개에 변경·누락·예상 밖 파일이 없다. 합성 fixture 주석을 한국어로 정리하기 전 Gate도 보존했으며, 최종 source의 Gate를 별도로 실행해 결속했다.

## 고정 입력과 실제 진단

실제 진단은 `.flowmarshal-engine-eval/runs/r-s06-07-20260904-v2`에 보존한다. 기존 phase 비교 4개와 Goal·Skeleton·State·Project Map·원장 복사본은 과거 파일과 byte 단위로 같다. 새 비교 3개는 직전 실제 `expanded-plan.json`에서 다음 두 부분만 수정한 정상 입력을 기준으로 만들었다.

1. `ac_003.validation_ids`에 이미 존재하는 `val_task_oracle`를 추가한다.
2. 마지막 완료 조건을 Worker의 직접 관측 보고 → 이후 독립 Validator 검토·별도 결과 → Task 완료로 바꾼다.

오류 입력은 각각 연결 하나만 제거하거나 완료 조건 하나만 과거 문장으로 복원한다. 검사 실행·phase·Task 목적·기여 집합·모델 배정은 바꾸지 않았다. 호출 전 메인과 보조 검토자가 변화 범위를 대조했고, `fixture-provenance.json`과 preflight에 입력·기준·source·schema·model inventory를 고정했다.

| 고정 사례 | Terra/high 실제 결과 | 직접 의미 판정 |
|---|---|---|
| 기존 정상 phase | semantic ID의 `ac_001` 연결 요구 | 근거 부족으로 정상 후보 거부 |
| Task phase 과장 | semantic ID의 `ac_004` 연결만 요구 | 목표 phase 오류 검출 누락 |
| Goal 검사에 task phase 오지정 | phase 범위 모순 finding | 목표 결함 검출 |
| 같은 ID의 별도 실제 호출 검사 | semantic ID의 `ac_004` 연결 요구 | 정상 추가 검사 책임을 가진 후보 거부 |
| 새 경계 정상 입력 | finding 없음, rating 3 | 정상 허용 |
| 복합 검사 ID 연결만 누락 | finding 없음, rating 3 | `ac_003`의 `val_task_oracle` 누락을 놓침 |
| Worker가 미래 Validator 결과 제출 | Plan 내부 시점 충돌 finding | 목표 결함 검출 |

`constraint_003`의 독립 semantic 검토는 Task 자체의 필수 검사다. 해당 검사는 모든 비교 Plan에 실제 존재한다. 그러나 전역 의무나 검사 문장의 동작·공개 계약 언급만으로 각 AC가 요구하지 않은 추가 ID 연결을 강제하지 않는다. 특히 `ac_004`의 선후조건을 모든 Task 검사 ID의 연결 요구로 확대하지 않는다. 이전 [R-S06-06의 직접 근거 구분](r-s06-06-handoff.md)과 현재 Goal 원문을 유지한 판정이며, 정상 입력이나 oracle를 사후 수정하지 않았다.

새 상세 Plan의 `ac_004.validation_ids`에는 `validation_goal_test`만 있다. `ac_004.validation_intent`가 명시한 task phase를 실제 실행하는 **`validation_task_oracle_and_unittest`의 연결 누락은 직접 결함**이다. Reviewer도 이 누락을 지적했지만 같은 finding에서 semantic Validator ID까지 필수라고 확장한 부분은 채택하지 않았다. 실행 자체는 존재하며 추가 호출 누락이 아니다. `ac_003`의 복합 검사 연결, Worker 보고와 후속 Validator 순서, Task·Goal phase 구분은 보존됐다.

보조 검토의 중간 판단 중 semantic ID까지 `ac_004`에 요구한 주장은 선후조건을 확대했으므로 철회했다. 최종 의미 평가는 메인과 보조 검토자가 Goal·등록 자료·실제 구현에 대조한 최소 직접 근거만 남겼다. 원본 Reviewer 응답이나 Core 정책은 수정하지 않았다.

## 같은 입력의 Reviewer 모델 비교

기존 사용자 요청인 역할별 모델·추론 수준 검증을 반영하여, 반복된 검출 누락 3종과 정상 1종에서 **일반 Reviewer 모델만 Terra → Sol로 변경**했다. source·입력·지침·schema·inventory·cwd·`high` effort와 상대 호출 순서는 같다. 새 호출 이유·역할 설정·model lock·receipt를 `model-comparison/`에 별도로 남겼으며 기본 제품 설정을 변경하거나 자동 fallback으로 처리하지 않았다.

| 목표 | Terra/high | Sol/high |
|---|---|---|
| Task phase 과장 직접 검출 | 놓침 | 검출 |
| `ac_003` 복합 검사 ID 누락 검출 | 놓침 | 검출 |
| Worker 미래 Validator 결과 충돌 검출 | 검출 | 검출 |
| 경계 정상 입력 허용 | 허용 | 허용 |

목표 결함 검출은 오류 3사례 중 **1개 → 3개**로 늘었다. 다만 Sol은 phase 오류 사례의 `ac_001`, 연결 오류 사례의 `ac_002`에 semantic ID를 추가로 요구했고, 연결 오류 사례에서는 Validator의 diff/test 입력이 빠졌다는 지적도 덧붙였다. 마지막 주장은 [runtime.py의 `_task_evidence_catalog`](../src/flowmarshal/engine/runtime.py)가 현재 성공 실행의 file/diff/command/test/build를 모두 제공하는 경로와 맞지 않는다. `required_evidence_kinds`는 이 직접 catalog의 허용 목록이 아니며 Worker 응답의 external_observation 포함 여부만 별도 제어한다. Plan의 file·응답 언급도 diff/test 제외 선언이 아니다.

따라서 **Sol의 목표 검출 개선은 관측했지만 정확한 최종 검토·기본 배정 qualification은 입증하지 못했다.** 이는 한 번의 고정 순서 비교이며 반복·순서 안정성, 최적 effort나 전체 Planning 성능의 증거가 아니다.

| 같은 4개 요청 | input tokens | output tokens | 합계 | 역할 latency 합 |
|---|---:|---:|---:|---:|
| Terra/high | 137,546 | 3,476 | 141,022 | 80,265ms |
| Sol/high | 137,546 | 6,805 | 144,351 | 141,657ms |

## 결속·비용과 평가 도구 한계

| 최종 결속 | digest |
|---|---|
| source | `sha256:444a818926ee9a3f7d7139fd9b4b2a31d4019b6b822250067b31f7b95043fbd0` |
| 결정적 계약 | `sha256:a742ae8490afbd091c9c220428c2610abcad1193f22e6603855164521a5f4f57` |
| 결정적 report | `sha256:150d20f8ec6d216bcb05037b83d48f1f0537b3fce87bad3f43209d3ea4cc08e5` |
| 기본 진단 preflight | `sha256:2a9728615fd46596890c6cb0dc35821732084d19e820cac0f53e151049b0ea53` |
| 모델 비교 preflight | `sha256:d59dbf77d6f0f45487ae927cd68b15dab82c17af13bf47d1b0fb4c6365794ea9` |

| 이번 실제 호출 | logical calls / provider turns | input tokens | output tokens | 합계 | 역할 latency 합 |
|---|---:|---:|---:|---:|---:|
| 기본 진단·새 상세화 | 9 / 9 | 310,328 | 14,190 | 324,518 | 295,938ms |
| Reviewer 모델 비교 | 4 / 4 | 137,546 | 6,805 | 144,351 | 141,657ms |
| 합계 | **13 / 13** | **447,874** | **20,995** | **468,869** | **437,595ms** |

schema recovery와 usage unavailable은 모두 0건이다. 위 비용은 메인·보조 에이전트를 제외한 실제 역할 receipt 합이며 latency 합은 전체 작업 wall time과 다르다. 이전 파일 1,357개와 원본 workspace를 보존했다. 기존 원장 쓰기·Plan 선택·활성화·Worker 실행은 모두 0건이다.

평가 도구 오류도 모델 결과와 분리했다.

- 첫 준비 실행은 생략된 선택 필드와 역직렬화 후 null을 raw dict로 비교하여 실제 호출 전에 중단됐다. 원본 디렉터리와 실패 기록을 보존하고 typed 비교 및 원본 byte 검사를 함께 사용했다. 모델 호출은 0건이다.
- 기본 진단의 거부 helper는 모든 finding에 `source:*` ref를 추가로 요구해 정상 검출된 내부 시점 충돌을 false로 기록했다. Plan 내부 완료 조건·검사 입력만으로 충분한 직접 근거다. 원본 summary를 유지하고 별도 의미 평가에 이 한계를 명시했다. 전체 FAIL은 다른 실제 실패로도 유지된다.
- 모델 비교 verifier는 저장 JSON의 정렬된 object key에서 strict schema를 재생성하면서 실제 `required` 배열 순서와 달라졌다. 최종 `verify_v3.py`는 저장 요청과 현재 원래 schema의 값 일치를 확인한 뒤 원래 schema에서 strict 변환해 실제 schema·receipt와 대조한다. 이전 검사 코드·수정 provenance를 보존했으며 모델을 다시 호출하거나 schema 제약을 완화하지 않았다. 원본 보존도 preflight에 고정한 파일 집합의 hash로 검사하여 이후 별도 감사 보고서 추가와 구분한다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-07-20260904-v2/verify.py
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-07-20260904-v2/model-comparison/verify_v3.py
```

두 결속 검증의 PASS는 의미 진단 PASS가 아니다. 기본 `verification.json.diagnostic_passed=false`와 두 `semantic-assessment.json.passed=false`를 함께 읽는다.

## 다음 작업

인계용 직접 증거는 다음과 같다. 아래 파일은 모두 `.flowmarshal-engine-eval/runs/r-s06-07-20260904-v2/` 기준이며 읽기 전용으로 원본을 대조할 수 있다.

- 연결 누락 재현: `input-missing-link-plan.json`의 `ac_003.validation_ids`와 `missing-link-review.json`. `input-boundary-clean-plan.json`과 검사 ID 하나만 다르다.
- 새 생성 결함: `expanded-plan.json`, `expanded-review.json`, `input-goal.json`의 `ac_004.validation_intent`. 실제 task phase 검사 문장과 AC 연결을 대조한다.
- 과잉 거부·phase 누락: `input-clean-plan.json`·`clean-review.json`, `input-bad-plan.json`·`bad-review.json`, `input-combined-plan.json`·`combined-review.json`.
- 모델 차이·근거 부족 지적: `model-comparison/*-review.json`, 두 `semantic-assessment.json`, `model-comparison/preflight.json`·`verification.json`. 원래 요청은 `calls/*/request.json`에 보존돼 있다.

R-S06-08에서는 **AC가 명시한 검사 책임 → 실제 검사 ID**와 **전역 Task 검증 의무 → Task 자체 validation**의 연결을 구분하도록 생성·검토 계약을 정밀화한다. 명시 task phase 연결 누락을 잡으면서 semantic 검토의 문장상 연관이나 선후조건만으로 AC 연결을 강제하지 않아야 한다. 기존 phase 모순·복합 검사 연결·미래 결과 충돌과 정상 비교를 함께 유지한다.

직접 evidence catalog를 Plan 필수 evidence enum의 허용 목록으로 오해하는 검토도 기존 runtime 근거와 함께 회귀에 포함한다. 새 비교에서는 Plan 내부 충돌의 직접 ref와 외부 자료가 필요한 phase 모순을 구분하고, 목표 결함 검출·부가 오지적·정상 허용을 각각 평가한다. 모델이나 호출 분리 전략의 추가 비교는 명시적인 새 lock·예산으로 수행하며 이번 원본과 고정 기준을 바꾸지 않는다.

후속 S06 스크립트는 `.flowmarshal-engine-eval/preparations/s06-bugfix-trace-20260904-v5`에만 준비했다. 진단 PASS·source 일치를 preflight 조건으로 두며 이번 실행 조건은 충족되지 않았다. 새 S06·Plan 활성화·Worker 실행은 수행하지 않았다. 제한 진단과 새 상세화 의미 검증을 통과한 뒤 고정 원문부터 새 S06을 진행할 수 있다.

현재 단계는 위 결과와 직접 증거 인계로 마친다. 조율 작업이 구현·검증을 배정하고 별도 분석 작업이 원인분석·해결안 설계를 담당한다는 후속 역할 분담에 따라, 추가 실험·다음 개발 단계는 독자적으로 시작하지 않는다. 별도 예약작업이나 작업관리 DB를 만들지 않았다.
