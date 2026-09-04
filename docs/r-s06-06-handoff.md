# R-S06-06 — 검사 연결 입력 보정과 검사별 범위 대조

2026-09-04. [R-S06-05](r-s06-05-handoff.md)에서 남긴 정상 입력 분류 오류, 검사 phase 과장과 Reviewer의 검출 누락을 이어서 보완했다. 원문·Goal·Skeleton·등록 검사 자료·oracle·합격선·역할별 모델과 effort를 유지하고 과거 artifact를 수정하지 않았다.

**최종 source의 496개 테스트·결정적 Gate 5/5·legacy freeze 40개와 실제 호출 결속 검증은 PASS다. 최종 제한 진단은 FAIL이다.** 네 고정 비교 사례와 새 상세화의 phase 범위 보존은 통과했지만, 새 Plan의 검사 ID 연결 누락과 Worker·Validator 결과 시점 충돌을 Reviewer가 놓쳤다. 다음 작업은 **R-S06-07: 검사 ID 연결·Worker와 Validator의 결과 제출 경계 보완**이다. 새 S06·Plan 활성화·Worker 실행은 수행하지 않았다. 전체 Trace는 `INCOMPLETE`, 1.0 cutover는 `NO-GO`다.

## 최종 변경

- 정상 비교 입력의 `ac_003.validation_ids`에 이미 존재하는 `val_task_add_behavior_contract`를 추가했다. 이 Task oracle는 기존 unittest를 실제 실행한다. 새 검사·명령·Task를 추가한 것이 아니라 필수 검사 ID의 추적 연결을 보완한 것이다. 원본은 그대로 보존하고 새 입력의 변화와 digest를 `fixture-provenance.json`에 기록했다.
- 오류 비교 입력은 보정된 정상 Plan에서 첫 Task 검사 문장 또는 첫 Goal 검사 문장만 과거 오류 문장으로 교체했다. 같은 validation ID에서 별도 입력 호출·기대값 비교 책임을 명시한 정상 사례도 추가했다. 호출 전에 메인과 독립 보조 검토가 네 입력을 대조했다.
- [검사 범위 지침과 작성 draft](../src/flowmarshal/engine/planner_roles.py)는 수단이 실제 관측하는 범위와 별도 실행할 검사 책임을 구분한다. 같은 문장에 추가 검사 책임을 둘 수 있으며, 목적만 덧붙여 도구 능력을 과장하지 않는다. 직접 모순을 낮은 rating이나 다른 finding으로 대신하지 않도록 안내한다.
- [검사별 검토 색인](../src/flowmarshal/engine/planning.py)의 `plan_validation_scope_rows`는 모든 Task·integration validation의 ID·소유 Task·원문·method·mode·evidence 종류·현재 AC 연결을 그대로 투영한다. 능력이나 필수 연결의 판정을 미리 채우지 않으며, finding은 기존 evidence catalog에 결속한다. AC에 연결되지 않은 검사도 색인에 포함한다.
- `PlanReviewDraft`, `PlanTaskValidationDraft`, `PlanIntegrationValidationDraft`는 provider 필드 설명을 구체화한다. 권위 schema의 필드·값 제약, finding/rating 상호배타성, Core 판정·점수·재시도 정책은 유지한다. 모델 fallback이나 점수 하한을 추가하지 않았다.
- 프로젝트와 시작 프로젝트의 `AGENTS.md`, [권위 설계](orchestration-redesign.md)에 장기 경계를 맞췄다. 원장 상태·기존 선택 Plan을 사후 편집하지 않았다.

## 결정적 검증

[회귀 테스트](../tests/test_engine_plan_validation_scope.py)는 실제 oracle에서 `return 5`가 task phase를 통과하고 goal phase에서 거부됨을 확인한다. Scripted 역할 결과로 네 사례의 finding 전달·Compiler ID 변환·Core 등록 경계를 검사하며, 이를 실제 모델의 의미 탐지 성공으로 주장하지 않는다.

별도 색인 회귀는 두 Task와 AC 미연결 검사를 포함한 고정 기대 행으로 순서·소유자·원문·mode·연결 보존을 확인한다. statement 변경에 따른 요청 digest 변화와 provider draft의 값 제약 보존도 확인한다.

중간 전체 Gate에서 [evidence enum 검사](../tests/test_engine_evidence_contract.py)가 이전 provider schema 이름을 참조해 `KeyError: ValidationContract`로 한 건 실패했다. 새 draft 이름으로 참조만 갱신했으며 enum과 합격선은 유지했다. 실패 report와 당시 source를 최종 실행의 `deterministic/`, `gate-failure-source/`에 보존했고 snapshot을 통한 source digest 복원도 확인했다. 이 실패는 실제 모델 호출 전에 발견된 테스트 연동 결함이다.

최종 `deterministic-v2/`의 496개 테스트·compileall·pip check·synthetic lifecycle·legacy freeze는 모두 통과했다. 동결 파일 40개에 변경·누락·예상 밖 파일이 없다.

## 실제 진단 결과

실제 실행은 다음 두 디렉터리에 별도로 보존하며 Git에서 제외한다.

- 1차: `.flowmarshal-engine-eval/runs/r-s06-06-20260904`
- 최종: `.flowmarshal-engine-eval/runs/r-s06-06-20260904-v2`

각 진단은 정상·Task phase 오류·Goal phase 오류·별도 실제 검사 책임 사례를 검토한 뒤, 같은 Goal·Skeleton을 새로 상세화하고 독립 Reviewer로 검토한다. 최종 진단의 네 Plan·Goal·State·Project Map·원장 복사본은 1차와 byte 단위로 같다. 같은 criteria·호출 순서·모델·effort를 유지하고 새 source와 schema를 각각 결속했다. 역할 cwd는 별도 동일 내용 복사본이며 Goal 대상 root를 바꾸지 않는다.

| 범위 | 1차 | 최종 |
|---|---|---|
| 보정된 정상 Plan | 허용 | 허용 |
| Task phase 능력 과장 | 직접 finding으로 거부 | 직접 finding으로 거부 |
| Goal 검사에 task phase 오지정 | finding 없음, 검출 실패 | 직접 finding으로 거부 |
| 같은 ID의 별도 실제 검사 책임 | 허용 | 허용 |
| 새 상세 Plan의 phase 범위 | 과장 재발, Reviewer 검출 | 범위 보존 |
| 새 상세 Plan의 전체 의미 | 검사 연결 결함도 남음 | 검사 연결·역할 시점 결함을 Reviewer가 놓침 |
| 제한 진단 전체 | FAIL | FAIL |

1차 source는 `sha256:de1af1f4bc4d89443a6a1527ebd084b93be612025a328faa57fabd1cd7b55cdb`다. 495개 테스트와 호출 결속 검증은 통과했지만 Goal phase 오류를 놓쳤다. 당시 source·결과를 `source-snapshot/`과 원본 응답에 보존한 뒤 검사 색인과 작성 draft 설명을 보완했다.

1차 새 Plan의 AC 연결 finding은 `ac_004.validation_intent`가 명시한 task phase 검사 ID의 누락을 최소 직접 근거로 채택했다. ‘모든 Task 완료 후’라는 선후조건만으로 모든 Task 검사 ID를 모든 AC에 요구하지 않는다. 또한 Task phase 과장의 복구는 실제 범위로 문장을 좁히거나 별도 검사 책임을 명시하는 두 방법이 가능하다. 해당 Task에 포괄 호출 검사를 반드시 추가하라는 부가 설명까지 채택하지 않았다.

최종 상세화의 첫 출력은 Skeleton의 objective를 재서술해 Compiler의 보존 검사에 실패했다. 기존에 허용된 한 번의 structured recovery 후 원문을 보존했다. 복구 전 응답·오류·새 turn receipt를 모두 보존했으며 이 성공을 의미 검증 통과로 계산하지 않는다.

## 최종 상세 Plan의 직접 결함

최종 `expanded-review.json`은 finding 없이 모든 rating을 4로 반환했다. 저장 응답을 읽기 전용으로 `derive_candidate_decision`에 대입하면 **score 100의 `admissible`**이 된다. 실제 원장 등록·선택·활성화를 수행한 것은 아니다.

메인과 독립 보조 검토가 다음 두 결함을 확인했다.

1. **검사 ID 연결 누락:** `val_task_oracle.statement`는 기존 unittest 실행을 포함하지만 `ac_003.validation_ids`에는 `val_task_unittest`와 `val_goal_test`만 있다. 이번 정상 비교 입력에서 고쳤던 것과 같은 결함이다. 실제 unittest 실행은 존재하며, 추가 실행이 빠졌다는 뜻은 아니다.
2. **Worker·Validator 결과의 시점 충돌:** 마지막 Task 완료 조건은 분리 Validator의 검토 결과를 Worker 응답 본문으로 제출하라고 요구한다. 반면 `val_task_validator_review`는 Worker 응답을 입력으로 뒤에 수행되는 별도 Validator 검사다. Worker의 실행 보고와 이후 Validator의 결과 제출 책임이 충돌한다. 실행하지 않았으므로 실제 교착이나 runtime 실패를 관측한 것으로 확대하지 않는다.

Task phase의 파일·보존·AST·annotation·시그니처·기존 unittest 범위와 독립 Goal phase의 고정 입력·위치/키워드 호출 범위는 이번 최종 Plan에서 구분됐다. 이 개선과 위 두 검출 누락을 별도로 기록한다. 독립 판단은 `semantic-assessment.json`에 저장했으며 실제 Reviewer 응답이나 Core 정책을 수정하지 않았다.

## 결속과 사용량

| 최종 결속 | digest |
|---|---|
| source | `sha256:eb903ed6e7d38ca1536c3c554c3a28541916c4e26150ded94e8bf9bc957ff00a` |
| 결정적 계약 | `sha256:f08ce417bd6837ef51aedbea96908748dac647850e21305e7edde23697e8c575` |
| 결정적 report | `sha256:d6e0dc6b4bbb0319a87623e048ca7df0c3d31ca047593e19fe521eef992891ad` |
| 진단 preflight | `sha256:3290e1a0f683e2cb0740a6da4f741263c8af18b166e32fee80ae31c7659b2840` |

| 실행 | logical calls | provider turns | input tokens | output tokens | 합계 | 역할 latency 합 |
|---|---:|---:|---:|---:|---:|---:|
| 1차 | 6 | 6 | 230,827 | 10,587 | 241,414 | 221,438ms |
| 최종 | 6 | 7 | 243,744 | 9,290 | 253,034 | 203,530ms |
| 합계 | 12 | 13 | 474,571 | 19,877 | **494,448** | 424,968ms |

schema recovery는 최종 상세화의 1회이고 usage unavailable은 0건이다. 메인·보조 에이전트 비용은 포함하지 않으며 latency 합은 전체 작업 wall time과 다르다. 1차의 과거 파일 1,066개, 최종의 1,189개와 원본 workspace를 보존했다. 두 진단 모두 원장 쓰기·Plan 선택·활성화·Worker 실행은 0건이다.

```powershell
.venv\Scripts\python.exe -X utf8 -B .flowmarshal-engine-eval/runs/r-s06-06-20260904-v2/verify.py
```

최종 `verification.json`의 `binding_verification_passed=true`는 현재 adapter 재생·입력·strict schema·실제 정책·thread/turn receipt·사용량·원본 보존 검증이다. `diagnostic_passed=false`와 함께 읽어야 한다. 고정 네 사례의 통과를 전체 역할·Planning·E2E·token/latency qualification으로 확대하지 않는다.

## 다음 작업

R-S06-07은 최종 실제 Plan을 provenance로 다음을 보완한다.

1. 상세화와 Reviewer의 Task 검사 목적·AC 연결을 한 기준으로 대조하고, 기존 검사 실행과 추적 ID 연결을 구분한다. 정상 입력을 사후 고쳐 이번 진단을 PASS로 바꾸지 않는다.
2. Worker 실행 보고와 이후 Validator의 독립 결과 제출 경계를 생성·검토에 보존한다. 미래 Validator 결과를 Worker 응답의 완료 조건으로 요구하지 않는다.
3. 두 실제 결함의 정상·오류 비교와 이번 phase 경계를 회귀로 유지한다. oracle·원문·합격선을 완화하거나 finding을 임의 생성하지 않는다.
4. 제한 진단과 새 상세화의 의미 검증을 통과한 뒤 고정 원문부터 새 S06을 실행한다. 과거 선택 Plan이나 이번 진단 Plan을 사후 편집해 활성화하지 않는다.
