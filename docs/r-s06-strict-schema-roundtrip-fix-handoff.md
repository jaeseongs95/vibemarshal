# R-S06 strict schema 저장 왕복 보정 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 보정 결과

`strict_json_output_schema()`가 object의 `properties`와 `required`를 같은 locale 비의존 결정적 순서로 생성하도록 보정했다. 저장 요청은 계속 `sort_keys=True`로 직렬화하며, 재로드 뒤 재생성한 strict schema와 최초 전송 schema의 값·canonical digest가 같아진다. `canonical.py`의 digest 규칙, 저장 규칙, `legacy/prototype` R3.1 helper는 변경하지 않았다.

`scripts/diagnostics/r_s06_10.py`의 완료 call 검증도 다음 관계를 독립 항목으로 분리했다.

- request → strict artifact 재생성
- strict artifact → turn intent
- strict artifact → receipt schema digest
- terminal → 저장 result 및 output digest
- prompt/instruction, receipt request identity, model/effort, thread/turn
- 실제 `verify_binding()` 결과만 쓰는 `model_observation`

따라서 schema digest가 어긋나도 model 관측 실패로 오인하지 않는다. strict artifact와 receipt digest를 함께 바꿔도 저장 request에서 다시 만든 schema 관계가 실패한다.

## 보존과 범위

R22 원본 `.flowmarshal-engine-eval/runs/r-s06-19-post-capture-fix-r22-20260905-v1`과 완료 receipt, R20/R21 및 과거 artifact는 읽기·수정·삭제·재사용하지 않았다. R22의 FAIL을 PASS로 바꾸지 않았고 Goal/Plan 의미, fixture, oracle, threshold, taxonomy, 역할 및 model binding을 바꾸지 않았다. 권위 설계 문서와 프로젝트 지침은 이 구현 보정으로 수정할 대상이 아니다.

변경된 transport schema와 source는 기존 Gate·preflight·checkpoint를 stale로 만든다. 새 source로 만든 fresh 결정적 Gate만 이 보정의 검증 근거이며, 실제 provider 역할 검증은 이번 작업에서 실행하지 않았다.

## 회귀와 결정적 Gate

선언 순서와 알파벳 순서가 다른 root·nested object, `prefixItems` 내부 object, `anyOf`·`oneOf`·`enum`의 순서 보존을 회귀로 추가했다. `RoleCallRequest` 저장→재로드 뒤 strict schema와 digest의 동일성도 확인한다. 완료 call 검증은 request, strict artifact, turn intent, receipt, terminal의 단독 변조와 strict artifact+receipt 동시 위조를 각각 차단하며, schema 실패가 `model_observation`을 false로 바꾸지 않는지 확인한다.

현재 source의 결정적 Gate는 별도 fresh run root에서 실행한다. Gate artifact의 `qualification-report.json`은 compileall, 전체 `test_*.py` suite, pip check, synthetic lifecycle, legacy freeze manifest의 5개 cell과 contract·report digest를 함께 보존한다. 이 문서의 변경까지 포함한 source에서는 이전 Gate·preflight·checkpoint를 재사용하지 않는다.

## 제한 실제 검증 재개 조건

실제 provider 재개는 별도 승인된 fresh 제한 검증에서만 가능하다. 새 run root에서 변경된 source manifest·schema·request·preflight·checkpoint를 다시 결속하고, 과거 R22 thread/turn·receipt를 재사용하지 않아야 한다. 이 보정 작업은 Plan activation, Worker 실행, provider call 또는 R-S06 실제 run을 수행하지 않는다.
