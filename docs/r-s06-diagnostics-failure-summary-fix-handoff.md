# R-S06 실패 안전 diagnostics summary 보정 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 결과와 범위

`scripts/diagnostics/r_s06_10.py`의 성공 결과 전용 결속과 공통 호출 결속을 분리했다. 이제 `failed.json`의 `schema_failed` receipt와 완료된 `terminal.json`이 있고 `result.json`이 없는 정상 역할 실패도 `summary.status=FAIL`로 보존되며, 요약 처리는 예외를 바깥으로 전파하지 않아 정상 평가 FAIL 관례의 종료 코드 0을 유지한다.

R-S06-23의 원래 Reviewer 제출물과 `StructuredRoleError`는 유효한 실패로 유지했다. `plan_inspection` validator, oracle, threshold, 사례별 기대표, raw 응답, receipt와 terminal은 수정하지 않았다. provider 호출, R23 thread 재개·추가 turn, 새 제한 qualification, Goal·Plan revision도 수행하지 않았다. 기존 S06 FAIL, Functional Alpha 미완료와 1.0 `NO-GO`는 그대로다.

## 구현 경계

- 호출 디렉터리마다 request, strict schema, thread/turn intent·receipt, terminal, result, failed, 기존 binding verification의 존재·JSON 파싱 상태와 bytes digest를 먼저 수집한다.
- `common_call_verification()`은 request·schema·Prompt·instruction·권한·model/effort·thread/turn·terminal 완료·receipt usage·model observation을 검사한다. `completed_call_verification()`의 terminal/result/output digest 검사는 정상 성공에만 추가된다.
- 완료 terminal과 실패 receipt가 있으면서 result가 없는 경우 `outcome=failure`, output binding은 `passed=null`, `status=NOT_APPLICABLE`과 이유로 기록한다. 원래 `StructuredRoleError`, receipt의 상세 오류, usage와 logical/provider/recovery 수는 유지한다.
- terminal이 없는 provider intent는 `external_unknown`으로 보존한다. 자동 재시작하거나 사용량·recovery를 0으로 보정하지 않는다.
- malformed terminal, partial receipt, request·결속 파싱 오류는 `diagnostic_errors`와 `outcome=incomplete`로 역할 실패와 분리한다. 사용량을 완전히 귀속할 수 없으면 token·duration·reasoning 포함 여부는 null과 이유로 남긴다.
- 누적 receipt의 같은 `call_id`는 canonical 내용이 같을 때만 한 번 집계한다. 내용 충돌은 `DUPLICATE_CALL_ID_CONFLICT`, 현재 capture에 귀속되지 않는 과거 receipt는 `UNATTRIBUTED_RECEIPT`로 기록하고 authoritative 합계에서 제외한다.
- logical request, thread intent/start, turn intent/start, terminal, accepted result 효과 수를 분리한다. provider `totalTokens`를 그대로 합산해 `reasoningOutputTokens`를 다시 더하지 않는다.
- summary는 저장 artifact만 읽는 순수 집계 뒤 `write_new()`로 한 번 배타적으로 게시한다. 같은 입력의 재호출은 기존 summary를 반환하고 파일을 바꾸지 않으며, 게시 이후 입력이 달라지면 `SUMMARY_INPUT_CHANGED_AFTER_PUBLICATION`으로 차단한다. 기존 execution claim 재실행도 파일 불변과 provider/turn 추가 효과 0을 유지한다.

## 원본 R23 읽기 전용 대조

원본 root는 `.flowmarshal-engine-eval/runs/r-s06-19-post-schema-fix-r23-20260905-v1`이다. 새 수집 함수를 읽기 전용으로 적용한 결과는 다음과 같다.

| 항목 | 관측 |
|---|---|
| 호출 outcome | `failure` 1, `external_unknown` 0, `incomplete` 0 |
| receipt | `schema_failed`, logical/provider/recovery `1/1/0` |
| 원래 오류 | `StructuredRoleError: structured output이 유효하지 않습니다. schema recovery 0회` |
| 상세 오류 | `대조표 검사 scope 절차·phase·근거 결속 오류` |
| output binding | `NOT_APPLICABLE`, `passed=null` |
| 공통 결속 | 10개 검사 모두 true |
| usage | input 44,707, cached input 0, output 8,778, reasoning 4,375, total 53,485 |

원본 `failed.json` bytes SHA-256은 `4d252ffd6460fc46c495c2893eb204fc8d74fc808989413ee0d54bdc6f42cabd`, `terminal.json`은 `63db4945392c113fa958277a561c84c81e194212c52d5e9a125103322050097e`다. 이 대조 과정에서 원본 summary나 result를 합성하지 않았다.

## 테스트와 Gate

새 결정적 회귀는 완료 실패, terminal 없는 external unknown과 예산 소비 유지, malformed terminal, partial receipt, summary 재실행 불변, execution claim 재실행 불변, 누적·충돌·과거 receipt 귀속, usage와 reasoning 중복 방지, 효과 수 분리, 정상 성공 및 변조 차단을 포함한다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m unittest tests.test_engine_inspection_source_contract tests.test_engine_inspection_runtime_capture tests.test_engine_inspection_fixture_revision tests.test_engine_inspection_case_binding tests.test_engine_inspection_diagnostic tests.test_engine_inspection_summary tests.test_engine_inspection_raw_regressions -q
```

관련 회귀는 53 tests, exit 0으로 통과했다. 마지막 source에서 프로젝트 결정론 Gate를 새 root로 실행했다.

```powershell
.\.venv\Scripts\python.exe -X utf8 -B -m flowmarshal.engine.eval_cli run --scope deterministic --project-root D:\codex\flowmarshal --run-root D:\codex\flowmarshal\.flowmarshal-engine-eval\runs\diagnostics-summary-regression-20260905-v2
```

Gate는 `COMPLETED`, **5/5 PASS**, failure 0, exit 0이다. compileall, 전체 583 tests, pip check, synthetic lifecycle, legacy freeze를 포함한다. contract digest는 `sha256:35f0cc64256e6904461c84f1415ae59d2e190a4f173f84ecaee622daa28b2186`, source manifest는 `sha256:fab8c58d5bd57dd10abb36aeb5a48e19ec97cd106dfb93e38a43874df00b820a`, report digest는 `sha256:b6ba38e3a49d62fab76689e1c1ea0617d61524eb98c6dd84358025a2fc59143d`다. Gate artifact는 Git에 포함하지 않는다.

## 남은 실제 qualification 조건

현재 source에서 새 immutable evaluation contract와 fresh model lock을 준비한 뒤 제한 실제 역할 검증을 처음부터 새 run으로 수행해야 한다. R23의 thread·turn·receipt나 과거 checkpoint는 재사용·재개하지 않는다. 고정 호출 순서·13회 상한·schema recovery 0·첫 실패 중단, 사례별 expectation binding과 원래 validator/oracle/threshold를 유지해야 한다. clean부터 실제 성공 result와 결속된 summary가 나온 뒤에만 다음 사례로 진행할 수 있다. 이 실제 검증 전에는 현재 source의 Reviewer 의미 회귀 PASS나 전체 S06 PASS를 주장할 수 없다.
