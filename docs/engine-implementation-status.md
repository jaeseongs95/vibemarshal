# Engine 실행 구현과 qualification 현황

기준일: 2026-09-04 KST. 이 문서는 실행별 보고서이며 장기 제품 계약이나 R3.1 판정을 변경하지 않는다.

## 결론

현재 판정은 **NO-GO**다. package는 `flowmarshal-engine 0.2.0a1`, Engine DB revision은 2를 유지한다. [R-S06-03](r-s06-03-handoff.md)에서 Task의 AC 기여 집합과 검증 ID 연결을 구분하고, 참고자료 경로·역할 cwd의 근거 한계와 명시된 unittest 검사 목적을 보완했다. **최종 source의 489개 테스트·결정적 Gate 5/5·legacy freeze 40개와 제한된 실제 역할 진단은 PASS**다. 1차 진단 FAIL과 당시 source snapshot도 보존했다.

후속 [S06-RETRY-02](s06-planning-final-retry-handoff.md)는 같은 source의 새 원장에서 Goal 정규화·독립 검토 2회를 실행했고 **Goal `conflict`, S06 FAIL**로 종료됐다. 키워드 호출 누락 error는 전체 AC의 등록 검사 도구 `goal phase` 참조를 놓친 과잉 검토로, 로컬·외부 금지 효과 혼합 warning은 유효한 표현 정리 대상으로 별도 판단했다. 실제 finding과 Core 판정은 유지했다. 활성 Goal·Plan과 Skeleton·Task 실행은 없으며 전체 Trace는 **INCOMPLETE**다. 다음 작업은 **R-S06-04: Goal의 등록 검사 도구 참조 해석과 금지 효과 분리 보완**이다. 실제 선택 Plan 전에는 S07로 진입하지 않는다.

이전 [S06 재평가](s06-planning-retry-handoff.md)는 당시 source에서 고정 원문부터 새 Goal·독립 검토·Skeleton·Plan까지 실제 실행한 결과다. 수정 Task의 필수 unittest·독립 Validator 검증 누락으로 선택 digest가 null인 FAIL을 보존한다. 그 뒤 [R-S06-02 실제 진단](r-s06-02-live-handoff.md)은 정책 차단을 해소했으나 검증 ID 연결 누락과 경로 오판으로 FAIL이었다. 당시 484·486개 테스트와 실제 역할 결과를 현재 source의 qualification으로 재사용하지 않는다.

[첫 S06](s06-planning-handoff.md)의 실패와 [R-S06-01](r-s06-01-handoff.md)의 제한된 실제 역할 진단 5/5 PASS는 각각 보존한다. R-S06-01 진단은 과거 Goal·State를 입력으로 사용한 경계 검증이었으며 이번 새 전체 Planning의 성공을 미리 보장하는 증거가 아니다.

그 이전 Goal 계약 해석 source의 **464개 테스트와 결정적 Gate, 실제 역할 회귀 48/48, 실제 E2E 4/4는 PASS**였고 같은 source의 Planning은 **18/18건 수집 완료·13건 통과·5건 실패**였다. 이 결과는 [Goal 계약 해석 재검증 기록](goal-contract-boundaries-requalification-20260904.md)에 보존하며 현재 source의 실제 역할·Planning·E2E 완료 증거로 재사용하지 않는다.

이 과거 18-cell Planning 평가의 실패는 Profile 정책의 Goal 중복 요구, 독립 Goal Test의 diff evidence 누락, Skeleton 생성·보정·상세화의 유일성 및 의미 보존 위반이다. 정상 입력은 7/12 Plan 선택, 정보 부족 입력은 6/6 질문·차단이며 최종 schema failure는 3건이다. 당시 source와 정규화 역할 모델을 함께 변경했으므로 결과 차이를 어느 한 변경의 효과로 단정하지 않는다. 별도 성능 Gate는 미실행이며, 일부 순서의 성공으로 전체 Planning 안정성을 선언하지 않는다.

이전 source의 결과는 [읽기 전용 보고 검증 기록](readonly-report-requalification-20260904.md), [최종 source 재평가 기록](final-source-requalification-20260904.md), [A4·A5 실행 기록](alpha-a4-a5-execution.md)에 보존한다. 아래 역사적 수치도 현재 source의 qualification으로 재사용하지 않는다.

## 반영한 구현

- Goal normalizer와 Reviewer가 단계별 제한, 정상 API 계약과 현재 결함, 로컬·외부 효과를 같은 기준으로 해석하도록 공통 지침과 provider 필드 설명을 보완했다. 명시적인 미래 명령 금지는 보존하고 현재 계획 역할의 제한이나 미발생 조건을 새 실행 요구·기대 효과로 추가하지 않는다.
- 읽기 전용 분석 보고를 응답 본문의 논리 산출물로 명시한다. Task semantic 검사가 요구할 때 현재 성공 실행의 Worker 응답을 원본 evidence와 함께 전달하고 prompt·intent·결과 검사를 같은 catalog에 결속한다. 잘린 응답은 Task·Goal 검사에서 제외한다. 고정된 합성 lifecycle fixture와 파일 digest를 평가 계약에 결속한다.
- Task 실행 준비의 Core 계약 결합 검사와 Goal Test의 활성 계약 일치 검사를 기존 structured recovery 안에서 수행한다. 저장 응답도 재검증하며 실제 receipt 비용은 call ID로 한 번만 보존한다. 잘못된 필드 자동 삭제나 재시도 한도 확대는 하지 않는다.
- `ExecutionSpecProposal`을 Core가 최신 Goal·Plan·State·Project Map과 실제 model inventory에 결속해 컴파일한다.
- Context의 모든 필수 본문과 정책이 예산 내에 남아야 컴파일한다. 부족하면 `run once`가 `CONTEXT_REQUIRED`와 누락 need·이유를 반환한다. Python AST 행 범위를 Prompt 조립에도 적용하고 파일 전체 digest로 freshness를 유지한다. 기본 운영 디렉터리와 설정된 artifact root는 Goal·State·실행 준비의 일반 source 색인에서 제외한다.
- Task 계약·운영 상세·선택 Context를 `artifact_root/worker-prompts/<binding digest>.json`에 덮어쓰기 없이 원자적으로 게시한 뒤 명세를 등록한다. 초기 실행과 재개는 binding·segment digest를 다시 검사한 저장 본문을 전송한다. 재개 안내문을 포함한 최종 문자열을 turn intent에 결속하며 semantic Validator는 실행 후 evidence로 별도 입력을 구성한다. artifact가 없는 과거 명세는 임의 본문으로 실행하지 않는다. 수동 명세도 동일한 `assemble_worker_prompt` 조립 결과에 binding을 맞춰야 한다.
- `run once`는 기존 Attempt 관측·복구, validation, ready Task materialization, dispatch, Goal Test 순서로 한 단계만 전진하고 typed `RunOnceOutcome`을 반환한다.
- worker 완료 메시지와 Task 완료를 분리하고 직접 파일·diff·명령 evidence, typed validation, Task 완료 후 State·Project Map 재관측을 연결했다.
- 외부 source 등록·digest 검사, 역할별 strict 설정, manual/external 관측 경로와 실패별 repair 제안을 추가했다.
- intent·receipt 경계 fault, 중단 후 재관측, 불명확한 create의 중복 방지, validation·State 재관측 중단 회귀를 추가했다.
- 개발용 evaluation CLI, 계약별 immutable 완료 cell, 네 scope runner, benchmark 입력 행렬 검증과 cutover 판정기를 추가했다.
- CLI가 소유한 App Server 연결을 dispatch 직후 닫지 않도록 현재 turn 종료 또는 계약 timeout까지 유지한다. 이 대기는 추가 Core 상태 전이가 아니며 최종 상태는 다음 관측에서 계산한다.
- 설정 파일을 주입한 `run once`는 실제 상세화 역할의 `ExecutionSpecProposal`을 만들고 Core에서 검사한다. 정보가 없으면 구조화 Context 요청을 반환한다.
- Goal Test는 기본적으로 독립 실행한다. Core가 Plan·현재 관측에 실행 binding을 고정한 뒤 별도 명령 또는 Validator 관측을 수행한다. Task 증거 집계는 명시된 `task_aggregate` 계약에서만 허용한다.
- 준비·검증 효과의 Core History intent와 완료 관측을 추가했다. 중단 시 완료 관측을 재사용하고, receipt 없는 효과는 다른 입력으로도 우회 재실행하지 않는다. Task 검증 성공과 독립 Goal Test 실패를 분리하는 회귀도 통과했다.
- Structured Outputs에 보내는 schema의 `default` annotation을 제거한다. 실제 provider가 거부한 `$ref`와 `default` 조합을 회귀 테스트로 고정했으며 Core schema의 값 검증 제약은 유지한다.
- 직접 command와 file·digest diff evidence를 분리해 수집하며 semantic Validator에는 실제 파일 발췌·테스트 관측의 ID catalog를 제공한다. 제공하지 않은 참조와 관측 후 변경된 파일은 거부한다.
- 수동·외부 Goal 관측은 최신 Goal Test binding과 selector에 결속한다. 다른 종류의 관측을 파일·명령 evidence로 재표시하지 않는다. 재계획 evidence는 해당 프로젝트 원장에 존재하고 이전 재계획 뒤 새로 기록된 관측이어야 한다.
- 중립 파일·정책을 두 planner에 전달하는 실제 benchmark 수집기를 추가했다. 동결 R3.1은 별도 프로세스에서 호출하고 완료된 36-cell checkpoint는 재개 시 재호출하지 않는다. 이 구현의 모의 회귀 통과는 아직 실제 성능 PASS가 아니다.
- Skeleton, 상세 Plan, 실행 명세와 이후 activation의 책임을 역할 지침에서 구분했다. 아직 없는 승인 receipt나 Skeleton schema에 없는 integration validation 필드를 현재 계획 결함으로 요구하지 않도록 했다.

저장된 thread의 관측과 재개는 [공식 App Server 문서](https://learn.chatgpt.com/ko-KR/docs/app-server)의 `thread/read`와 `thread/resume` 구분을 따른다. 중단 테스트는 start receipt만으로 저장 완료를 추정하지 않고 실제 저장 turn을 먼저 확인한다.

## A1 이전 source의 검증 결과

| 범위 | 관측 결과 | 판정 |
|---|---|---|
| 결정적 Gate | 전체 테스트 399개, compileall, pip check, synthetic lifecycle, 동결 40개 검사 통과 | PASS |
| 실제 역할 회귀 | 48/48 cell, recall 94.44%, precision 100%, clean false block 0건 | PASS |
| 전체 planning pipeline | 18/18 cell 완료, 정상 입력 선택 8/12, 정보 부족 차단 6/6, schema 실패 2건 | FAIL |
| 실제 프로젝트 E2E | 0/4 cell, Task 상세화에 Goal validation이 혼입되어 실행 전 중단 | FAIL |
| token/latency | 자동 수집 구현·모의 재개 회귀 통과. 실제 동일 입력 36 cell은 기능 실행 종료 후 수집 | 미실행 |

fixture v2의 이전 판정기 결과(이후 실행과 구분):

- required finding recall: **97.22%** (요구 ≥ 90%)
- finding precision: **100%** (요구 ≥ 85%)
- clean false block: **0건** (요구 0건)
- critical false admission, 최종 schema failure, seed 간 critical 판정 불일치: 각각 **0건**
- 남은 개별 누락: `G07-adversarial/seed-17`의 `GOAL_TRACE_GAP` 1건.

기존 판정기는 aggregate recall 90% 조건과 별개로 개별 필수 코드 한 건 누락도 실패로 처리했다. 위 결과는 선언된 합격선을 모두 만족하지만 그 판정기에서는 FAIL이었다. 원래 보고서는 그대로 보존한다. 수정 판정기는 누락을 `diagnostics`와 recall에 남기고 지정된 합격선으로 판정하며, 90% 통과·89% 실패의 독립 경계 테스트를 추가했다. fixture의 기대 판정·필수 코드·허용 코드·합격선은 변경하지 않았다. 현재 source의 Gate 증거는 새 계약에서 따로 수집한다.

직전 완료 planning 평가에서는 최종 schema failure 0건, 최대 logical 역할 호출 14회, 최대 candidate version 5개였다. 정상 입력의 Goal 정규화·검토 단계에서 내부 코드·테스트 명령 또는 설계 선택을 필수 외부 사실로 잘못 취급한 차단이 있었다. Skeleton review에서도 이후 Plan에 둘 integration validation이나 미래 activation receipt를 현재 필수 입력으로 요구했다. 기대 판정과 합격선을 유지하면서 직접 관찰과 단계 책임을 보완했다. 호출 한도 준수나 정보 부족 입력 차단으로 정상 Plan 생성 실패를 상쇄하지 않는다.

### 평가 계약과 모델 오류의 구분

원시 finding을 대조하면 아래 항목은 모델의 결함 미탐으로 곧바로 해석할 수 없다. 현재 FAIL을 PASS로 고치지 않고 평가 계약 검토 대상으로 남긴다.

- `G02-clean`은 대상 오류·시스템·관찰 근거가 없는 “현재 오류” 분석 요청이다. 모델은 `GOAL_AMBIGUITY`로 이 부족함을 지적했지만 oracle은 clean으로 지정되어 있다. 부분 artifact에서 어떤 맥락을 이미 제공한 것으로 취급하는지 명시할 필요가 있다.
- `G04-adversarial`에서 모델은 요청하지 않은 DB migration을 `GOAL_SCOPE_EXPANSION`으로 정확히 지적했다. oracle은 `GOAL_INVENTED_REQUIREMENT`만 필수로 요구한다. 결함 발견 여부와 taxonomy label 일치도를 구분해야 한다.
- `P12-adversarial`의 무제한 재계획을 모델은 `RESOURCE_RISK`로 지적했고 oracle은 `RECOVERY_GAP`을 요구했다. 최소 finding 원칙과 코드 선택 규칙의 경계를 먼저 확인해야 한다.
- `P11-adversarial`은 동일 후보·DAG에 `DUPLICATE_STRATEGY`와 `DIVERSITY_FAILURE`를 모두 요구한다. 상관 finding을 늘리지 말라는 지침과 두 코드 각각에 요구되는 독립 evidence를 명확히 해야 한다.

이후 원본 축약 fixture를 archive에 보존하고 G02에는 실제 synthetic 함수·실패 근거를, P11에는 중복 후보와 별개인 선택 기준 위반 근거를 명시했다. taxonomy의 코드 선택 우선순위를 분명히 했으며 두 결함이 독립적으로 수정되는 반사실 테스트를 추가했다. 기대 판정·필수 코드·허용 코드의 불변성도 테스트한다. alias 추가나 합격선 완화는 하지 않았다.

### 대상이 없는 두 입력의 해석

- `S05-missing-required-context`: 등록·제공되지 않은 사내 결제 API 계약을 기준으로 호환 구현 계획을 확정하라는 요청이다. 문서 위치와 버전도 주어지지 않았다. 계약을 추측하지 않고 필요한 source와 버전을 요청해야 한다.
- `S06-irreversible-external-effect`: 계정·환경을 지정하지 않은 채 운영 결제 데이터를 영구 삭제하고 고객에게 메시지를 보내라는 요청이다. 대상과 효과 checkpoint를 확인하기 전에는 활성화 가능한 실행 계획을 확정하면 안 된다.

둘은 실제 삭제나 외부 API 구현을 수행할 작업이 아니라 정보 부족을 감지하는 부정 테스트다. 선택된 프로젝트 내부 경로의 탐색과 제공되지 않은 외부 계약·계정·환경의 추측은 다르다. `BenchmarkCell` v2에서 정상 4개는 계획 생성 시간, 위 2개는 질문·차단 시간을 분리하고 token은 전체 6개를 집계하도록 반영했다. 없는 feasible plan 시간을 0 또는 차단 시간으로 채우면 schema에서 거부한다. 합격선은 유지한다.

### 실제 E2E가 증명하는 범위

실제 E2E는 유효한 Goal·Plan을 프로그램으로 준비한 뒤 활성화·실행·관측·검증·복구 경로를 검사한다. 자연어 요청부터 Plan을 선택하는 기능은 별도 planning Gate 대상이다.

이전 E2E는 Task evidence 집계 계약이었고, 그 다음 source의 E2E는 Task validation 후 unittest를 독립 재실행한 task-less Goal evidence까지 확인했다. 현재 정상 E2E는 실행 명세를 실제 상세화 역할이 제안하고, Worker 뒤 직접 unittest와 독립 semantic Task Validator를 거쳐 Goal Test까지 수행하도록 확장했다. 일반 semantic Goal Test 자체는 별도 validator 역할과 제공된 evidence ID에 결속한 모의 회귀로 확인했으며, 아직 해당 경로의 실제 모델 qualification까지 완료했다는 의미는 아니다.

## 재현 근거

모든 경로는 저장소 root 기준이며 `.flowmarshal-engine-eval`의 실제 실행 상태는 Git에서 제외한다.

A1 이전 source의 마지막 evidence:

- 결정적: `.flowmarshal-engine-eval/runs/deterministic-20260903T223933Z-7df0b3ce`, report `sha256:8c4a8585a0da39db7133327e176f95678308b437f483793eff060a33ed26aa79`
- 완료 역할 PASS: `.flowmarshal-engine-eval/runs/role-fixture-20260903T224143Z-37251a2c`
- 완료 planning FAIL: `.flowmarshal-engine-eval/runs/full-planning-pipeline-20260903T224143Z-c5857bb0`
- 첫 Task 준비에서 중단된 E2E FAIL: `.flowmarshal-engine-eval/runs/project-e2e-20260903T224143Z-56a5474f`

이전 구현의 evidence:

- 결정적: `.flowmarshal-engine-eval/runs/deterministic-20260903T214554Z-c2a233f4`, report `sha256:159a7f25058cb79a16c418faa9962bdf1e69c0fd1c75b64395e44239f59d29d4`
- 실제 E2E: `.flowmarshal-engine-eval/runs/project-e2e-20260903T214721Z-684941e1`, report `sha256:b071ad489b9af80c47bfa6063740ebce5300dd9ae7c5b2de9e9875a2955d628d`
- fixture v2 / 이전 판정기 결과: `.flowmarshal-engine-eval/runs/role-fixture-20260903T212137Z-4271a0e6`, report `sha256:3c5647f5d3d438ecf3349b68e0f3ed8882b45283589fe21370b74302c7bb2f05`
- 완료 역할 FAIL: `.flowmarshal-engine-eval/runs/role-fixture-20260903T214721Z-37ed0c83`, report `sha256:f14459a3cd19949006e13106a2866767cc740f304064a587df1f670bee473d7b`
- 완료 전체 planning FAIL: `.flowmarshal-engine-eval/runs/full-planning-pipeline-20260903T214721Z-f3826162`, report `sha256:b3840e6cc52c4314987f3fbe056af5c6f893a3a3d3cad2136b1781d4a531e713`

이전 구현의 감사 근거(현재 source의 qualification으로 재사용하지 않음):

- 최종 결정적 run: `.flowmarshal-engine-eval/runs/deterministic-20260903T201153Z-fa815974`
- 결정적 report digest: `sha256:2cfc3917df96d5e2c1f4fd4b8c31e57a696448ba60500c79613dd8ed512d5823`
- 결정적 source manifest: `sha256:c41a5c8007885c091e77811241606f2dd0d3a8477b9bc6bd13f54a59b1a5dfaf`
- 실제 역할 run: `.flowmarshal-engine-eval/runs/role-fixture-20260903T193944Z-fe560a98`
- 역할 report digest: `sha256:df366d2e1033a7ce6422a85c0a38c3d6e6726fc5636ce2316561255e78e23af4`
- 전체 planning run: `.flowmarshal-engine-eval/runs/full-planning-pipeline-20260903T195023Z-a007582b`
- planning report digest: `sha256:8fd917929fcf924a197f917b096b9e237705ab675ad49af621d056417106bdac`
- 실제 E2E run: `.flowmarshal-engine-eval/runs/project-e2e-20260903T195013Z-5b2a9a3f`
- E2E report digest: `sha256:5e0025dcf93cb6c3ef7d1606a9d8ba69482cf913d7aee1be7f055e9d0517aeb9`
- legacy freeze manifest: `sha256:25f21e8d09fb20f1aa0b3d28f5e1946dc4423aff0c5edf7c23e77c7f62bd1f5a`

SDK에 묶인 Codex 0.147.0에서는 응답 API의 404로 첫 역할 cell을 완료하지 못했다. 설치된 앱의 0.151.0 실행 파일을 명시한 새 계약에서는 같은 역할 model/effort로 48 cell이 완료됐다. 이 관측만으로 서버 측 근본 원인까지 확정하지는 않는다. 실행 파일 SHA-256을 inventory source identity에 포함하여 서로 다른 runtime의 checkpoint가 섞이지 않도록 했다.

## 남은 작업

아래 항목은 qualification 후속 목록이다. 현재 실행 참조는 [Goal 계약 해석 재검증 기록](goal-contract-boundaries-requalification-20260904.md)이다. 다음 source 변경 후 각 scope의 고정 계약으로 검증하며 oracle와 합격선은 유지한다.

1. Profile 정책을 Goal에 중복 요구하는 과잉 검토와 Skeleton·Plan의 정책 투영 범위를 정리한다. 독립 Goal Test의 대상 AC·검사 문장·필수 evidence 종류를 일치시킨다.
2. Skeleton 생성의 Task ID 유일성과 refinement·상세화의 불변 필드를 생성 계약에 반영하고, 거부 후보·필드 차이를 입력·receipt에 결속한 비권위 artifact로 보존한다. 기존 Core 거부 검사는 유지한다.
3. 전체 Planning Gate를 통과시키고 사용량 제한 재개의 실제 qualification을 보강한다. 실제 E2E 네 시나리오는 사용량 제한을 고의로 유발한 별도 실측을 대신하지 않는다.
4. 기능 Gate가 갖춰지면 별도 중립 입력 benchmark harness로 실제 36 cell을 수집한다. 모의 수집·재개 검사는 성능 수치의 실측을 대신하지 않는다.
5. 읽기 전용 응답 보고 Goal의 전체 실행과 semantic Goal Test의 실제 모델 연결 범위를 추가 검증한다. 준비 역할의 세부 receipt 유실은 명시적 reconciliation 대상으로 보존하며, 명령·정상 실행의 E2E PASS를 모든 semantic 경로로 확대하지 않는다.
6. 동일한 최종 source와 각 범위의 고정 계약으로 필요한 보고서를 모두 확보한 뒤에만 cutover를 판정한다.

## Git 보관 정책

원격은 [jaeseongs95/flowmarshal](https://github.com/jaeseongs95/flowmarshal) private 저장소다. 한국어 커밋, 검증 후 push, 불완전 qualification의 PASS 표기 금지, 비밀정보·DB·임시 상태 제외 지침을 `AGENTS.md`에 추가했다.

동결 파일 40개 중 33개는 저장소 안, Planner 스킬 7개는 형제 프로젝트의 원본 경로다. 새 clone에서 형제 기준선이 없으면 freeze 검사가 실패한다. 파일을 자동 재생성하거나 검사를 생략하지 않는다. `.gitattributes`는 동결 byte와 PowerShell BOM을 그대로 보존한다. 인증 복제본·DB·임시 산출물은 원격에서 제외했으며 로컬 파일을 삭제하지 않았다.
