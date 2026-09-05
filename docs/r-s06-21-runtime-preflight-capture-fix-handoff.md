# R-S06-21 runtime preflight 기록 충돌 보정 인계

기준일: 2026-09-05 KST. 대상: `D:\codex\flowmarshal`.

## 범위와 판정

R-S06-21의 fresh v2 prepare 뒤 첫 clean이 `runtime-preflight/inventory-01.json`의 exclusive create 충돌로 실패한 진단 harness 결함을 보정한다. 공통 runtime, model-lock v2, 역할 배정, fixture, oracle, threshold와 taxonomy는 변경하지 않았다. 과거 R21 root `.flowmarshal-engine-eval/runs/r-s06-19-post-lock-v2-r21-20260905-v1`와 그 안의 raw·artifact는 읽거나 수정·삭제·재개하지 않았다.

이 변경은 `scripts/diagnostics/r_s06_10.py`와 전용 결정적 회귀에 한정된다. Goal·Plan 의미, 권위 문서와 제품 계약은 바뀌지 않으므로 `orchestration-redesign.md`, ADR, model-lock 권위 문서의 갱신은 필요하지 않다.

## 구현

- `runtime-preflight/prepare`, `runtime-preflight/run`, `runtime-preflight/review-generated`를 명시 phase로 제한하고, 각 phase가 `phase-claim.json`을 exclusive create해 독점한다.
- inventory와 policy raw는 해당 phase에서 append-only로 기록한다. 번호는 `write_new` 성공 뒤에만 증가하므로 빈·절단·기존 `inventory-01.json`은 `-02`로 우회하지 않고 원본을 보존한 채 실패한다.
- call capture는 scoped context에서만 사용하고 성공·예외 모두 phase capture로 복구한다. 다음 사례의 preflight 관측이 이전 call 디렉터리에 기록되지 않는다.
- `execution-started.json`과 `generated-review-started.json` claim은 요약 예외 처리보다 앞에 둔다. 같은 실행 phase의 패자는 summary 또는 runtime/provider 효과 전에 실패한다. receipt가 없는 thread·turn intent도 같은 경로 진입 전에 차단한다.

## 결정적 검증

전용 `tests/test_engine_inspection_runtime_capture.py`는 mock raw 관측으로 다음 경계를 직접 검사한다.

1. 별도 runtime 인스턴스의 `prepare → run` 순차 기록이 서로 다른 phase 파일에 남고 prepare raw bytes가 불변이다.
2. call capture의 예외 뒤 다음 preflight 관측이 `runtime-preflight/run`으로 복구된다.
3. 같은 phase의 정상 연속 기록은 `01`, `02`이며, 별도 인스턴스·동시 claim은 정확히 하나만 성공한다.
4. 빈·절단 `inventory-01.json`은 번호를 건너뛰지 않고 실패하며 원본 bytes가 보존된다.
5. 기존 execution claim 또는 미완료 thread·turn intent가 있으면 runtime 생성 전 재실행이 차단된다.

새 source 기준 결정적 Gate artifact는 `.flowmarshal-engine-eval/runs/r-s06-21-runtime-preflight-capture-gate-20260905-v2`에 생성했고 5/5 PASS했다. 초기 `...-20260905` root는 외부 실행 시간 제한으로 완료 cell 0개인 상태에서 중단됐으며 보존한다. 이 Gate와 관련 model-lock 회귀가 통과해도 실제 provider 역할 검증을 대신하지 않는다.

## 제한 검증 재개 조건

새 제한 검증은 이 보정의 전용 테스트, model-lock v2 거부 회귀, 결정적 Gate 5/5, legacy freeze와 `git diff --check`를 새 source 기준으로 통과한 뒤에만 새 run root에서 수행할 수 있다. 재개 시에도 R21 root를 사용하거나 이어서 실행하지 않으며, prepare/run/review-generated의 새 phase raw와 모든 intent·receipt를 append-only로 남긴다. 실제 역할 호출, Plan activation, Worker 실행, 전체 qualification과 cutover는 이 보정 범위에 포함하지 않는다.
