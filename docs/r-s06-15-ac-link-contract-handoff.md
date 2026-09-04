# R-S06-15 AC 연결 필수성 계약 분리 및 제한 진단

## 결론

R-S06-14 분석에서 확인한 의미 혼합을 코드와 평가 계약에서 분리했다. Reviewer의 고정 의미 atom은 이제 `AC×validation`별 `ac_link_required: bool` 하나이며, 실제 연결 존재 여부는 Reviewer 응답이 아니라 입력 `Plan.definition.goal_coverage`에서 계산한다. 전역 Task 검사 의무는 `constraint_task_rows`, validation의 실제 실행 범위·phase는 `validation_scope_rows`에서 각각 평가한다.

결정론 Gate는 새 source manifest로 5/5 PASS했다. 제한 실모델 평가는 원래 순서와 모델 바인딩을 유지해 시작했으나 첫 `clean` 호출에서 28개 행 중 1개 의미 판정이 달라 FAIL했다. 계약대로 즉시 중단했으며 재시도·schema recovery·추가 호출은 없었다. 따라서 1.0 cutover 판정은 **NO-GO**다.

## 권한과 범위

- 작업 시작 시 파일시스템 권한은 unrestricted(`danger-full-access`와 동등), `approval_policy=never`였다.
- 실제 preflight와 첫 호출 receipt는 각각 `:danger-full-access`, `never`를 기록했다.
- 기존 R1–R3.1 동작, Core authority, Plan authority schema는 변경하지 않았다.
- Plan activation, Worker 실행, 새 orchestration ledger/SQLite 기록, 전체 qualification, 1.0 cutover는 수행하지 않았다.
- 다른 task/thread로 메시지를 보내거나 새 task·automation을 만들지 않았다.

## 구현

### Reviewer 출력 계약

`src/flowmarshal/engine/plan_inspection.py`의 `ACValidationInspection`에서 과거 3값 `relation`을 제거하고 strict boolean `ac_link_required`를 도입했다.

- 해당 validation이 AC의 일부를 직접 검사하면 `true`다.
- `false`는 연결 금지가 아니므로 선택적 연결이 있어도 오류가 아니다.
- 전역 Task 검사 의무를 AC별 필수 연결로 복제하지 않는다.
- adapter는 `ac_link_required=true`인데 입력 Plan의 `goal_coverage` 연결이 없을 때만 missing-link finding을 요구한다.

### 평가·입력 결속

`src/flowmarshal/engine/plan_inspection_eval.py`는 사전 고정된 전체 AC×validation boolean 표를 비교하고, 각 행의 `actual_link_exists`를 Plan `goal_coverage`에서 독립 계산한다. 평가 범위에는 다음을 명시했다.

- `ac_link_requirements: complete_matrix`
- `actual_link_source: plan_goal_coverage`
- `constraint_semantics: not_scored`
- `independent_defects: fixed_direct_evidence`
- `mechanism_semantics: fixed_defect_anchors_only`

모델 호출 전 `integration_validations[].criterion_refs`와 `goal_coverage`의 역방향 연결 집합이 정확히 일치하는지도 검사한다. 불일치는 `INTEGRATION_CRITERION_COVERAGE_MISMATCH`로 거부한다.

### v5 fixture

기존 원본을 수정하지 않고 재현 가능한 `scripts/diagnostics/r_s06_15_fixture_revision.py`로 다음 v5 파일을 새로 만들었다.

| 파일 | bytes SHA-256 |
|---|---|
| `tests/fixtures/engine/plan-inspection-v5-expectations.json` | `a826a9da5ef2faad40da1e8462206598f2a69bcc6dd52efa34a2b0791c871ba1` |
| `tests/fixtures/engine/plan-inspection-v5-independent-fixture-review.json` | `9e89d0a70cc178ee9032bd9e36659eea4baea619dcc4871a00cbd6279402ba27` |

- revision: `plan-inspection-v5-r-s06-15`
- parent: `plan-inspection-v4-r-s06-13`
- runtime expectations bytes: `sha256:3f098168547ee7eaadb00292ca839d82773f7746d62533e1c0fe62068f41a90e`
- 정규화·파생 case는 integration criterion ref와 goal coverage를 함께 변경한다.
- 독립 검토는 portable `PlanContractRevision` canonical digest로 모든 case 입력을 다시 결속한다.

## 기존 원본 보존

`git diff --exit-code`와 bytes hash로 v4·raw fixture가 변경되지 않았음을 확인했다.

| 원본 | bytes SHA-256 |
|---|---|
| `plan-inspection-v4-expectations.json` | `f132337b8b237e2379c6315a3b12ff18bfd5d24f8d62be6e29608b8bf963b011` |
| `plan-inspection-v4-independent-fixture-review.json` | `7b664981550bd50d53f19345052da0f92410e44f44458a9eb55fb3b291a3bd2d` |
| `plan-inspection-raw-v1.json` | `4b4c7ef5083c2488f65212b235cfb33b6fd1037ba2ab51ea75c70c8f7cb8bc69` |
| `plan-inspection-raw-v3-rejected.json` | `64ee38c7ccc7fd36edcd6ded162061337d3321ce8f445902552ce4bcd21042df` |
| `plan-inspection-raw-v6-rejected.json` | `b928cee62ec26a73c0a226a91ed59e2180c605a2f0773f3344c6217cdd47f03e` |
| `r-s06-12-raw-rejected/manifest.json` | `ff9254a6a025020831bec75ae747f0627748046c0bbcda9c1ca070cfa6e2ea18` |

R-S06-14 실행 원본도 byte-for-byte 동일하다.

| R-S06-14 증거 | bytes SHA-256 |
|---|---|
| `summary.json` | `8b15196685080a00c03ab7a33299597bac695bda52b2435220a421639bbb986d` |
| `result.json` | `66a964cf1f0425ec6833c9c0aa9c5bd21af67d52ad65acd836fab3bc4582875d` |
| `thread.receipt.json` | `6e3345c9d6e6e4e09c6d7b31c1a09cde713b10a36443203b16af32f5c5e989aa` |
| `turn.receipt.json` | `7f6d438838f5e06418a8347f8d9d92473b4111b8c86a8cc8968054073336f6fe` |
| `terminal.json` | `3cb766c0fc221104b5dbb7da1826377a591907365833b008a5d09c0abe3d7976` |
| `binding-verification.json` | `daed304e230484c96ba099ba7a80e844f3ed4fcedd60ab4bc8bcbc3499914758` |

## 검증

- inspection 집중 테스트: 45 PASS
- adapter·case binding 관련 테스트: 27 PASS
- 전체 테스트: 553 PASS, 60.006초
- `compileall src scripts tests`: PASS
- `pip check`: `No broken requirements found.`
- legacy freeze: 40개 경로 PASS, manifest `sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a`
- `git diff --check`: PASS

### 결정론 Gate

- run root: `.flowmarshal-engine-eval/runs/r-s06-15-20260905-v1/deterministic`
- status: `COMPLETED`, 5/5 PASS, failure 0
- scope: `deterministic_schema_dag_ledger`
- contract digest: `sha256:558ee3ca61f5a7642e03cdec98dd88153c611edc928895aca1247e43e345286a`
- source manifest: `sha256:719b871765146c2f405fa0ead086da53b3f44bf9a83faed4282ba101dbb8565c`
- qualification report bytes: `sha256:87e172ef60978eb6616fa4575b8a25cb147611998ba879cddca798c69cd6c3eb`

## 제한 실모델 평가

### 고정 조건

- run root: `.flowmarshal-engine-eval/runs/r-s06-15-20260905-v1`
- session: `R-S06-15`
- 원래 13개 호출 순서 유지
- 최대 logical call 13, 최대 provider turn 13
- schema recovery 0
- 일반 Reviewer: `gpt-5.6-terra/high`
- 중요 Reviewer: `gpt-5.6-sol/xhigh`
- Plan expander: `gpt-5.6-luna/high`
- preflight/lock digest: `sha256:ed41bd2b4e3212048756a2239c1d517278613fb7ad621aa27f7ad7d4d0519b3b`
- model lock digest: `sha256:5cca3c82ad7d7eb4e61aea0dba9e2101a465860b8abe1ec771538bc7d13762ec`

### 결과

첫 `clean` 호출에서 FAIL하여 나머지 12개 호출은 실행하지 않았다.

- logical calls: 1
- provider turns: 1
- schema recovery: 0
- 모델/effort: `gpt-5.6-terra/high`
- pair set: 일치
- 28개 AC×validation 행 중 27개 일치
- missing/unexpected finding: 없음
- 불일치: `ac_004 × val_task_add_behavior_contract`
- 기대: `ac_link_required=true`
- 모델 응답: `ac_link_required=false`
- 해당 실제 Plan link: 존재(`actual_link_exists=true`)

`val_task_add_behavior_contract`는 함수 구현·공개 호출 계약·annotation과 함께 반환값/예외 없는 정상 동작을 직접 검사하므로 AC-004의 일부를 직접 검증한다. 따라서 v5 사전 고정 기대값 `true`를 유지하며 oracle을 사후 변경하지 않았다.

| usage | 값 |
|---|---:|
| input tokens | 44,080 |
| cached input tokens | 0 |
| output tokens | 5,915 |
| reasoning tokens | 2,588 |
| total tokens | 49,995 |
| 역할 latency | 111,203ms |
| provider duration | 109,981ms |
| 청구 금액 | 미제공(null) |

reasoning token은 provider total에 포함되어 있으므로 다시 더하지 않았다.

### R-S06-15 실행 증거

| 파일 | bytes SHA-256 |
|---|---|
| `summary.json` | `0f814c985c4042a1109eb28c34060047b559879e3b72df5b0625e0b696db2132` |
| `clean-assessment.json` | `60e7ab8b6f7f5e362421e03cc245101e94411b0791a62699bfaa701fc780c091` |
| `calls/01-compact_plan_reviewer/result.json` | `bbcbfed08d5683833c8e7b614482eded6424af3c86b1d20239784405a72a4712` |
| `calls/01-compact_plan_reviewer/thread.receipt.json` | `7b5b26bd8c4aa9d0ef1e5395f6fa66e4f27f46c95bc358c46fa929fee408015e` |
| `calls/01-compact_plan_reviewer/turn.receipt.json` | `be4f7455e0d83280b2835185c87654deb6b55586ccb530d8aa6de8f1af0408b2` |

실행 종료 검사는 source·workspace·기존 원본 보존, 현재/이전 instruction 및 모델 binding을 모두 true로 기록했다. `full_qualification=NOT_RUN`, `plan_activated=false`, `worker_executed=false`, `new_ledger_writes=0`, `cutover=NO-GO`다.

## 잔여 조건과 1.0 판정

R-S06-15는 계약 혼합과 fixture 내부 양방향 연결 불일치를 코드 수준에서 닫았고 결정론 Gate를 통과했다. 그러나 제한 실모델 평가가 첫 clean case에서 1개 boolean 의미 오분류로 실패했으며 나머지 case·생성/재검토 호출은 stop-on-first-failure 규칙 때문에 관측되지 않았다. 전체 qualification, production-like workflow, token/latency budget 검증도 수행 범위 밖이다.

따라서 현재 1.0 Gate는 **NO-GO**다. 다음 시도는 이 결과를 덮어쓰거나 기대표를 사후 수정하지 말고, AC-004처럼 하나의 validation이 AC 일부만 직접 검사해도 `ac_link_required=true`라는 지시/모델 동작을 별도 source revision에서 개선한 뒤 새 run root와 새 source manifest로 평가해야 한다.
