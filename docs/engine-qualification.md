# FlowMarshal Engine 1.0 qualification

이 문서는 개발용 `flowmarshal-engine-eval`의 실행 계약을 설명한다. 네 기능 scope와 token/latency Gate를 모두 실제로 통과하기 전까지 판정은 `NO-GO`이며 package와 기본 CLI는 `flowmarshal-engine` pre-1.0 상태를 유지한다.

## 고정 입력

- 역할 설정: `config/qualification-roles.json`
- 공통 finding taxonomy: `config/qualification-finding-taxonomy.json`
- legacy 동결: `config/legacy-freeze-manifest.json`
- reviewer 회귀: `tests/fixtures/engine/r31-reviewer-regressions.json`, `goal-reviewer-regressions.json`
- planning 시나리오: `tests/fixtures/engine/planning-scenarios.json`
- 실제 프로젝트 E2E 원본: `tests/fixtures/engine/project-e2e`
- order seed: `17`, `43`, `89`

각 run의 `EvaluationContract`는 scenario·fixture, seed, 역할 설정, source manifest, prompt, schema, taxonomy, threshold와 `model/list` inventory를 digest로 고정한다. 이 중 하나가 달라지면 기존 run root를 재사용할 수 없다.

## Scope

`deterministic`은 `compileall`, 전체 테스트, `pip check`, synthetic lifecycle과 legacy freeze를 검사한다. 하나라도 실패하면 실제 모델 scope는 시작하지 않는다.

synthetic lifecycle의 프로젝트 입력은 `tests/fixtures/engine/synthetic-lifecycle-project`에 고정한다. 이 fixture와 검사 규칙은 평가 계약에 결속하고 원장·artifact는 임시 경로에 둔다. 저장소 전체 문서량에 따라 합성 상태 전이 검사의 Context가 달라지지 않게 하며, 전체 저장소 코드 검증은 전체 테스트·compileall·freeze에서 수행한다. 합성 fixture는 실제 모델 E2E를 대신하지 않는다.

`role-fixture`는 plan 8건과 Goal 8건을 seed 3개로 실행한다. recall 90%, precision 85%, critical false admission 0, clean false block 0, schema failure 0, critical seed 불일치 0을 모두 요구한다.

개별 필수 finding 누락은 `diagnostics`와 raw cell에 항상 보존하고 recall에 반영한다. 누락 한 건을 독립 FAIL로 처리해 사실상 recall 100%를 요구하지 않는다. critical false admission·clean false block 등 0건 조건은 여전히 개별 한 건도 Gate 실패다. 이 구분을 잘못 적용한 과거 보고서는 변경하지 않으며 새 source 계약에서만 수정 판정기를 사용한다.

`full-planning-pipeline`은 bugfix, 읽기 전용 분석, migration 전략 비교, produces/consumes DAG, 필수 context 부족, 비가역 외부 효과를 seed 3개로 실행한다. clean 입력은 Plan 선택, adversarial 입력은 질문 또는 차단이어야 하고 전체 역할 호출 14회 및 candidate version 5개 한도를 지킨다.

`project-e2e`는 원본 fixture를 run root 아래에 복사하고 네 cell 모두 실제 Codex adapter를 사용한다. 정상 완료는 안전한 코드 수정·테스트·Task validation·Goal Test를 검사한다. stale materialization은 dispatch 전 차단을, receipt 불명확 복구는 실제 thread 생성 직후 receipt 저장 전 fault와 중복 생성 방지를 검사한다. 저장 turn 재개는 실제 turn 중단 후 App Server 연결과 Core 인스턴스를 새로 열어 `thread/read` → 필요할 때만 `thread/resume`하는 순서를 확인하고 최종 Goal까지 진행한다. 각 호출의 receipt는 별도 journal과 완료 cell에 결속한다. 단위 테스트에서는 동일 harness에 모의 runtime을 주입하지만 그 결과를 실제 qualification으로 집계하지 않는다. 원본 fixture는 수정하지 않는다.

`run once` CLI가 dispatch한 turn은 해당 CLI가 App Server 연결을 소유하므로 현재 turn 종료 또는 ExecutionSpec timeout까지 연결을 유지한다. 이 대기 중에는 추가 Core 상태 전이를 하지 않으며, 다음 `run once`/`attempt observe`가 저장된 결과를 읽는다. 호출 자체의 강제 종료는 recovery 대상이다.

`run once --role-config <설정 파일>`은 ready Task의 실제 실행 상세화를 설정된 역할에 요청하고 Core가 검사·결속한다. 필수 자료가 없으면 구조화 Context 요청으로 차단한다. 모든 Task가 완료되면 독립 Goal Test의 상세화를 준비하고 다음 호출에서 명령 또는 별도 Validator 검사를 수행한다. 수동 경로는 `--proposal-file`, `--goal-validation-file`로 유지한다. 실제 E2E는 Task unittest와 별도로 Goal unittest를 재실행한 task-less evidence까지 요구한다.

준비·검증 효과의 intent와 완료 관측은 Core History에 기록한다. 완료 관측 뒤 프로세스 중단은 재사용으로 복구하고, receipt 없는 호출은 재생성하지 않는다. 준비 역할의 세부 thread receipt를 잃은 경우 자동 추정·재개하지 않는 보수적 한계가 있으며, 사용자 확인을 포함한 별도 reconciliation이 필요하다.

## Checkpoint와 재개

완료 cell만 `cells/seed-*/`에 배타적으로 생성되고 다른 결과로 덮어쓸 수 없다. 사용량 제한은 `run-state.json`의 `PAUSED_RATE_LIMIT`으로 기록한다.

```powershell
flowmarshal-engine-eval resume --run-root D:\path\to\existing-run
```

prompt, schema, oracle, threshold, source manifest, 역할 설정 또는 model inventory가 달라지면 새 run root를 사용한다. `project-e2e`의 실제 Codex cell이 사용량 제한으로 멈추면 digest-bound `cell-state.json`, 기존 Engine 원장과 workspace를 같은 run root에서 다시 열어 이어간다. 완료되지 않은 cell을 결과 checkpoint로 간주하거나 새 프로젝트·thread로 재생성하지 않는다.

## Benchmark와 cutover

benchmark 입력은 6개 중립 planning scenario × seed 3개 × `r31_baseline`/`skeleton_engine`의 36개 `BenchmarkCell`이다. 각 pair는 같은 scenario digest, 중립 파일·정책 digest, model lock과 functional result digest를 가져야 하며 실제 Runner receipt digest를 포함한다. `benchmark`는 두 구현을 별도 작업 복사본에서 순차 호출한다. R3.1은 Engine 밖의 `flowmarshal.benchmark_legacy` 프로세스에서 호출하며 frozen source·Planner 스킬·campaign artifact와 기존 판정을 변경하지 않는다. `benchmark --cells-file`은 외부 결과의 수입·판정 경로로 유지한다.

수집 범위는 계획 시작부터 최종 선택 또는 질문·차단까지다. token은 실제 uncached input과 output의 합이며 usage가 없으면 0으로 추정하지 않고 수집을 중단한다. 최초 feasible 시각은 Core의 결정적 admission 직후 관측한다. 상세 Task는 파일·명령을 확정한 운영 실행 명세를 뜻하며 목적·DAG만 있는 semantic TaskContract를 포함하지 않는다. 활성화 전 ExecutionSpec을 만들지 않는 Engine 경로는 운영 상세 생성 수가 0이다. 폐기 후보 출력은 후보별 expander/refiner의 실제 receipt로 계산하고, 여러 Skeleton이 한 응답에 담긴 출력 token을 임의로 후보별 배분하지 않는다.

```powershell
flowmarshal-engine-eval benchmark --codex-bin C:\path\to\codex.exe --scope-report <deterministic-report> --scope-report <role-report> --scope-report <planning-report> --scope-report <e2e-report>
flowmarshal-engine-eval resume --run-root D:\path\to\benchmark-run
```

완료된 cell은 재개 시 호출하지 않는다. 부분 호출 진단과 실패 원인은 별도 보존하며 미완료 cell을 immutable 완료 checkpoint로 표시하지 않는다. 실제 모델 Gate를 다른 모델 작업과 동시에 실행하면 latency 비교에 영향을 줄 수 있으므로 성능 측정은 다른 qualification 호출을 마친 뒤 수행한다.

실제 runtime의 실행 파일 SHA-256도 `model/list` inventory의 source identity에 결속한다. `--codex-bin`을 바꾸면 새 model lock·새 run root가 필요하며 모델 ID나 effort는 자동으로 바꾸지 않는다.

`BenchmarkCell` v2는 기대 판정과 실제 판정, 최종 판정 시간을 명시한다. 정상 4개 × seed 3개의 pair만 최초 feasible plan 시간을 비교하고, 정보 부족 2개 × seed 3개는 해당 값을 `null`로 두어 질문·차단 지연을 별도로 집계한다. token은 6개 모두 비교한다. 정상 입력의 Plan 생성 실패는 Gate 실패이며 차단 시간을 계획 생성 시간으로 대입하거나 0으로 채우면 schema에서 거부한다. 기존 합격선은 유지한다. 이전 형식의 보고서나 checkpoint를 v2로 자동 변환하지 않는다.

입력·taxonomy를 명확히 한 역할 fixture v2는 기존 기대 판정·필수 코드·허용 코드·합격선을 그대로 둔다. 원본 Engine 축약 fixture는 `tests/fixtures/engine/archive/`에 보존한다. 정규화와 Reviewer는 같은 실제 파일 관측을 받고, 전체 planning cell에는 Goal preparation·질문·Plan 후보와 finding 원문을 함께 저장한다. 정보 부족 입력이 단순한 Plan 생성 오류로 끝난 것은 올바른 질문·차단의 PASS로 집계하지 않는다.

`cutover`는 네 개의 `qualification-report.json`과 수집한 token/latency report를 명시적으로 받는다. 원본 계약의 source·역할·model lock을 확인하고 36-cell 성능 결과를 다시 계산한다. 누락·실패·다른 계약의 report가 하나라도 있으면 승격하지 않는다. 보고서가 모두 PASS인 경우에만 package를 `flowmarshal` 1.0.0으로 승격하며 DB migration이나 배포는 수행하지 않는다.

정상 프로젝트 E2E는 실제 상세화 역할이 준비한 ExecutionSpec, Worker, 직접 unittest, 독립 semantic Task validator, 별도 Goal Test 명령을 통과해야 한다. 직접 파일 evidence는 digest와 제한된 실제 내용 발췌를 포함하고 검토 전후 freshness를 확인한다. 수동·외부 Goal 관측도 현재 실행 binding과 selector에 결속하며 다른 evidence 종류로 재표시하지 않는다.
