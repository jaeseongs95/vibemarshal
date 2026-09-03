# FlowMarshal 문서 지도

## 현재 권위 문서

- [전면 재설계 권위 문서](orchestration-redesign.md): `flowmarshal.engine`의 제품 목적, 권위 모델, 승인·lazy expansion 경계, 실행·복구와 cutover Gate
- [Engine cutover ADR](engine-cutover-adr.md): legacy 동결, 별도 namespace·DB, migration과 1.0 승격 결정
- [입력 자료와 실행 범위 정책](file-access-policy.md): 프로젝트·`AGENTS.md`·등록 참고자료·localhost를 정상 입력으로 다루는 정책
- [저장소 작업 지침](../AGENTS.md): 구현 시 지켜야 할 안정된 불변조건
- [Engine 1.0 qualification](engine-qualification.md): 네 실행 scope, immutable checkpoint, benchmark와 cutover 절차
- [Engine 실행 구현 현황](engine-implementation-status.md): 실제 실행 결과, NO-GO 근거와 남은 qualification 범위

## 동결 기준선과 회귀 입력

- [R3.1 최종 동결 기준선](r31-frozen-baseline.md): campaign 10의 150/150 최종 `FAIL`, telemetry와 새 엔진으로 이관한 실패 유형
- [R3.1 회귀 fixture](../tests/fixtures/engine/r31-reviewer-regressions.json): 모순 admission, clean 오차단, schema 실패와 correlated-defect 사례
- [R3.1 상세 계약](planner-r31.md): 당시 prototype 계약. 현재 설계가 아닌 감사 기준선
- [R3.1 원본 campaign 결과](../spikes/orchestration/r31/artifacts/runs/r31-role-eval-campaign10-20260903/r31-role-fixture-evaluation.json): 수정하지 않는 원시 결과

## 현재 Engine 구현

- `src/flowmarshal/engine/domain.py`: frozen schema와 Core-derived 판정
- `src/flowmarshal/engine/ledger.py`: 별도 SQLite 원장, immutable revision과 History
- `src/flowmarshal/engine/context.py`: Project Map, Context Selector와 prompt binding
- `src/flowmarshal/engine/goal.py`: Goal 정규화·독립 검토·Core compilation
- `src/flowmarshal/engine/planning.py`: bounded Skeleton-first search와 Hard Gate
- `src/flowmarshal/engine/planner_roles.py`: 실제 구조화 역할 adapter
- `src/flowmarshal/engine/models.py`: `model/list` inventory와 명시적 fallback 배정
- `src/flowmarshal/engine/runtime.py`: Codex Runtime Port, dispatcher와 복구 표면
- `src/flowmarshal/engine/service.py`: activation, lazy materialization, 상태 전이와 validation
- `src/flowmarshal/engine/evaluation.py`: 회귀 지표, immutable checkpoint, token/latency와 cutover Gate
- `src/flowmarshal/engine/cli.py`: 개발용 `flowmarshal-engine` 인터페이스
- `src/flowmarshal/engine/eval_cli.py`: 개발용 `flowmarshal-engine-eval` qualification 인터페이스

## 역사적 설계와 증거

다음 자료는 당시 계약과 실험 결과를 재현하기 위한 감사 이력이다. 현재 Engine의 권위 API로 import하거나 현재 상태로 재해석하지 않는다.

- [R1 Runtime 보고서](../spikes/orchestration/r1/artifacts/runs/r1-20260902T081113Z-51e2d5b8/runtime-report.md)
- [R2 Core 설계·검증](core-r2.md)
- [R3 Planner 계약·검증](planner-r3.md)
- [Gate 0B 설계](gate0b-design.md)
- [Gate 0C 상세 구현 계획](gate0c-implementation-plan.md)
- [Gate 0C 권한 경계 재시험](gate0c-retest-plan.md)
- [Windows sandbox VM 재검증](windows-sandbox-vm-revalidation.md)
- [Windows VM 외부 전달 절차](windows-vm-external-handoff.md)

역사적 결과의 `GO`·`NO-GO`는 당시 계약에 대한 판정으로 유지한다. 새 제품의 1.0 여부는 권위 문서의 cutover Gate만으로 판단한다.
