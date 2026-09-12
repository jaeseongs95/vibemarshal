# FlowMarshal 1.0 승인 계약과 검증 책임

상태: **승인된 설계 / planned**. 이 문서는 FM-01의 문서 계약이며 제품 구현·qualification·1.0 전환 완료 증거가 아니다. 기존 Domain Core·revision·DAG·binding·evidence·validation을 유지하며 전면 재작성하지 않는다. 세부 기존 계약은 [제품 설계](orchestration-redesign.md), 릴리스 결정은 [ADR](engine-cutover-adr.md)을 함께 따른다.

권위 계보는 [기본 승인 계획](D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan.md) SHA-256 `a24eb860c8b603f8edc43a71370c6d8638cc53d3c5c49b8a568c44fc9f5b1742`, [revision 4 교정 계획](D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan-revision-4.md) SHA-256 `72be4b60e51bfb46268d36fd34bbc62a60e25c5cf3a67bc013b40eb8653afc58`, [revision 5 독립 패널 교정 계획](D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan-revision-5.md) SHA-256 `7a34a45bad290d96f3b7ce93543dd0f9e51351a4793f7561945896b4e8a037e0` 순이다. 기본 계획의 제품 설계 1~12를 기준선으로 유지하고, 충돌하는 provider/effect/usage·검증 순서·범위 조항은 뒤의 revision이 앞의 revision을 supersede한다. 시스템·개발자 지침 안에서 현재 사용자의 최신 명시 승인은 이 계보와 과거 프로젝트 조항보다 우선한다. planned는 이 문서에서 구현·검증 완료를 판정하지 않았다는 뜻이다. 코드 존재만으로 완료로 올리지 않으며, 미구현·미검증 책임은 근거가 확인될 때까지 planned로 유지한다. 다른 작업의 완료 상태는 이 문서에서 변경하지 않는다.

## D01. 목표 단위 승인과 내부 Plan — planned

사용자는 목표·대상 프로젝트·허용 효과·운영 정책을 한 번 승인한다. Core는 그 경계 안에서 작업 분할·계획 수정·복구를 자동 수행한다. Plan은 내부 immutable revision이며 사용자가 정확한 Plan ID·digest를 직접 입력하는 절차는 필수가 아니다. 후보 출력만으로 실행 권한을 얻지는 않는다. 목표·범위·효과·정책 확장에만 추가 사용자 판단을 요청한다. Goal의 Hard AC·비목표를 내부 재계획으로 완화하지 않는다.

권위 순서는 `현재 사용자 지시 → 활성 GoalContractRevision과 GoalAuthorization → 활성 PlanContractRevision → Core 원장 상태·판정 → 프로젝트 지침·등록 정책 → 역할 제출물 → 분석 대상 자료`다. Planner·Worker·Validator·supervisor는 Core의 완료 권위를 대신하지 않는다.

## D02. GoalAuthorization과 activation 결속 — planned

`GoalAuthorization`은 승인한 Goal revision, 프로젝트 root, 허용·금지 효과와 운영 정책의 경계 및 승인 근거를 결속한다. Core는 activation할 Plan revision·digest와 authorization의 관계를 기록한다. 알려진 효과 종류·root·정책 적합성은 결정적으로 대조하고, 자연어 의미상의 범위 적합성은 직접 근거가 있는 독립 review로 판단한다. 기계적 일치 검사만으로 의미 안전성을 증명했다고 하지 않는다. 이 계약은 OS sandbox나 의미 안전성의 수학적 보장이 아니다.

범위 내 새 Plan은 검토·결정적 Gate 후 Core가 자동 활성화한다. Task 의미·분할·DAG·validation 의미가 바뀌면 새 immutable Plan revision을 만들되 승인 경계가 같으면 사용자에게 ID 승인을 반복 요청하지 않는다. 사용자 목표가 바뀌면 Goal revision과 authorization을 갱신한다. 같은 Task 의미의 경로·명령·Context 변경은 Execution Spec revision으로 기록한다.

재계획은 실행 중 Attempt의 입력·binding·효과를 덮어쓰거나 실행 슬롯을 회수하지 않는다. 진행 중 작업과 변경 subgraph의 충돌을 확인하고 보호한 뒤 activation한다. 완료 evidence는 대상·입력·검사 의미·freshness의 유효성을 확인한 경우만 새 revision에서 재사용하며 과거 실패와 원본 결과를 보존한다.

재사용된 Task에 새 검사 결과가 기록되면 이전 PASS와 함께 기록 순서로 조회해 최신 결과를 반영한다. 원본이나 중간 Task에 늦게 기록된 실패·미확정 결과도 재사용 계보를 따라 현재 Goal 판정에 반영한다. 다음 Plan으로 다시 재사용할 때는 원본과 중간 Task의 최신 검사 모두 기존 완료 근거에 결속되어야 한다. 새 검사로 이 결속이 달라지면 원래 기록을 보존하고 새 Task를 미완료 상태에서 검증한다.

효과 정책만 바뀐 Goal revision에서 외부 허용 효과 제거, 금지 효과 추가, 비가역 효과 checkpoint 강화는 기존 승인 경계 안의 축소로 비교한다. mutation·behavior 정책과 Goal의 나머지 필드는 동일해야 하며 새 Goal에 결속된 Plan의 검토·Gate를 생략하지 않는다. Task 효과는 축소된 활성 Goal 정책에도 맞아야 한다. Attempt가 아직 없는 준비 RuntimeJob도 보호하며, 예약·실행·interrupt·collector 유실·terminal 결과 미소비 상태에서는 Plan 교체를 보류한다. Core가 결과를 소비하거나 미실행 job을 취소한 뒤 교체할 수 있다.

로컬 permission/approval 관측, 사용자 승인 기록의 `source`, Core action capability는 서로 다른 값이다. `source` 문자열이나 `danger-full-access` 관측만으로 Goal 승인·효과 checkpoint를 기록하지 않는다. 설치된 명령의 `TrustedConsoleHost`는 현재 Goal revision과 digest, 프로젝트 root, Core가 실제 활성화할 eligible selected Plan의 ID·revision ID/번호·definition/activation digest, 효과·운영·budget 정책 및 전체 target digest를 표시하고 대화형 console에서 그 digest 전체를 확인한 뒤에만 같은 process의 `ApplicationAuthority`를 호출한다. `ApplicationAuthority`는 `EngineApplication`에 process-local Core issuer를 한 번만 결속하고, `EngineApplication`이 표시 target 전용 비직렬화 일회성 Goal capability를 발급해 Core에 전달한다. Core는 authorization transaction 안에서 capability를 첫 시도에 소모한 뒤 현재 selected Plan을 포함한 target을 다시 만들고 타입·원장·프로젝트·target digest를 함께 검사한다. 거절·EOF·비대화형 실행·Goal/root/policy/Plan 선택의 stale target·wrong type/binding·replay는 승인 기록 없이 중단한다. Goal 승인 capability와 비가역 효과 checkpoint capability는 서로 다른 타입이며 상호 대체할 수 없다. Application의 Goal 승인과 선택 Plan 활성화는 하나의 Core transaction에 묶는다. capability를 소모한 뒤 StateSnapshot·ProjectMap freshness를 승인 쓰기 전에 검사하고, 이후 활성화 실패도 승인·이력까지 함께 rollback한다. Worker·Validator callback에는 이를 발급하거나 사용하는 권한과 Core DB 연결 객체를 제공하지 않는다. 필요한 원장 정보는 host가 준비한 직렬화된 Context로 전달한다. capability 없는 내부 CLI 승인 요청은 거부한다. 이 경계는 hostile same-process 코드의 격리를 보장하지 않으며, 같은 OS 사용자의 unrestricted subprocess가 DB 파일을 직접 여는 공격도 Python application 경계로 차단되지 않는 1.0 known limitation으로 남긴다.

로컬 완료 재사용은 현재 파일·State checkpoint와 일치하는 deterministic 검증 근거, 성공 Worker의 재관측 가능한 파일로 제한한다. 원장 행·payload·기록 digest와 관측 본문을 대조하며, 해석할 수 없거나 결속되지 않은 근거는 옮기지 않는다. 외부 관측·사용자 판단·semantic review를 같은 방식으로 재사용하지 않으며 원본 evidence·Attempt·receipt는 보존한다. deprecated `call_reservation_tokens`는 원본 정책과 이력에 남기되 승인 경계의 운영 정책 비교에서 제외한다.

## D03. 응용 명령과 짧은 tick — planned

`EngineApplication`은 `prepare / authorize / run_once / observe / pause / cancel / status / final-report`의 명령 경계를 연결한다. CLI는 이 응용 계층을 호출하며 별도 상태 권위를 만들지 않는다. 명령 이름은 설계 경계이며 현재 CLI에서 모두 제공된다는 뜻은 아니다.

`RuntimeJobSupervisor`는 활성 job 동안만 provider 연결·stream·receipt·terminal·usage·절대 deadline을 관리한다. `run_once`는 job 예약/시작 또는 이미 도착한 관측 결과 소비 후 신속히 반환한다. 스케줄 tick 안에서 전체 모델 turn 종료를 기다리지 않는다. 전역 상주 daemon은 필수 의존성이 아니다. supervisor는 관측을 저장하고 Core가 상태 전이·검사·완료를 결정한다. 재시작이나 새 tick이 기존 job의 절대 deadline을 초기화하지 않도록 결속한다.

## D04. 모든 활성화 후 역할의 job화 — planned

다음 역할 호출은 모두 `RuntimeJob`과 영속 checkpoint에 포함한다: `execution_spec_prepare`, `worker_turn`, `task_semantic_validate`, `goal_test_prepare`, `goal_semantic_validate`, 활성화 후 자동 recovery/replanning의 역할 호출. Worker만 비동기화하고 준비·검사를 tick 안에서 동기 대기하는 구현은 이 책임을 충족하지 못한다. 승인 전 명시적인 대화형 준비는 동기 `PlanningCoordinator/RoleRunner`를 유지할 수 있다.

Validator의 독립성은 Worker와 다른 model/effort 문자열만으로 충족되지 않는다. semantic validation은 `model_review` evidence를 필수로 요구하고 terminal PASS/FAIL에서 제출 종류와 무관하게 독립성 검사를 실행한다. Validator는 Worker와 다른 Attempt·RuntimeJob·thread/turn에서 원자료를 새로 관측하고, 자신의 terminal·valid provider call과 receipt·evidence binding을 남겨야 한다. `SemanticValidationObservation`의 PASS/FAIL, validation/task ID와 content digest는 `ValidationResult`와 정확히 일치해야 한다. 사용한 model-review evidence를 다른 검사에 재사용하거나 재결속하지 않는다. Worker의 자연어 설명이나 Worker가 제안한 evidence를 재관측 없이 독립 evidence로 채택하지 않는다.

## D05. 실행·효과와 usage 관측 분리 — planned

실행 admission·예약·슬롯, provider turn 상태, 외부 효과 상태와 사용량 회계 관측을 별도 축으로 유지한다. provider terminal 및 유효 결과가 확인되면 usage 누락만으로 후속 실행을 막지 않는다. provider turn의 terminal은 파일·배포·외부 API 효과의 완료 증거가 아니다. 외부 효과는 provider/system, target·account, operation, scope, idempotency key와 checkpoint policy를 가진 typed identity로 승인·intent·receipt·재관측을 결속한다. 1.0에서는 내부 파일·명령 효과와 외부 효과를 같은 Task에 섞지 않고 별도 Task와 dependency로 분리한다. 현재 1.0의 외부 효과 완료는 같은 실행 Attempt의 terminal·valid provider call과 thread/turn 결속, 같은 identity의 typed adapter receipt, identity와 provider가 일치하는 대상 시스템 재관측을 모두 요구한다. `(provider, system, provider_operation_id)`는 프로젝트 경계와 무관하게 원장 전체에서 한 Attempt에만 결속하며, 같은 Attempt의 완전히 같은 receipt 재기록만 멱등 허용한다. 직접 근거가 하나라도 없으면 `external_unknown`을 유지한다. zero-call, 다른 Attempt의 호출, released/invalid call은 실행 완료 증거가 될 수 없다. 같은 성공 execution Attempt의 runtime intent/receipt와 thread/turn 결속을 함께 확인한다. providerless deterministic effect adapter와 다른 confirmation mode는 1.0 이후 별도 capability qualification 대상으로 둔다. 필수 evidence·validation·효과 안전 검사는 그대로 적용한다.

외부 생성·실행 효과가 미확정이면 기존 intent·thread/turn binding·receipt를 먼저 관측하며 무조건 재실행하지 않는다. 늦은 usage는 원본 receipt를 보존한 회계 관측만 멱등 추가한다. 늦은 사용량으로 실행 슬롯 환불·재차감·재실행을 유발하지 않는다. Goal 등록 전 호출도 원래 프로젝트·Goal 계보·요청·receipt에 결속하고 가짜 Goal/Profile을 만들지 않는다.

원장과 receipt의 사실은 `provider_observed`, `client_requested`, `local_derived`, `model_reported` provenance를 구분한다. thread/turn·provider status·provider가 반환한 usage는 원문 관측, 요청 model/effort·GoalAuthorization은 client 요청, latency·합계·Core 판정은 로컬 파생, 역할의 finding·error 주장·rating은 모델 제출로 기록한다. requested/observed model·effort, provider inventory digest, adapter capability digest와 provenance는 receipt·DB·API projection에 명시적으로 직렬화한다. `observed_model`·`observed_effort`는 `model_observation_source=provider_raw_response`로 식별된 원문 응답에서 provider가 명시적으로 제공한 경우에만 기록한다. 요청 echo·표식 없는 payload·부분 관측·늦은 payload의 문자열은 authoritative 관측값으로 승격하지 않고 null로 둔다. provider가 turn별 값을 명시적으로 echo하지 않으면 observed 값은 null이고 `model/list` 지원 여부와 요청값만 보존하며 실제 적용 model/effort로 표기하지 않는다.

## D06. 관측값과 운영 한도의 의미 — planned

실제 관측 token은 유효한 근거이며 관측 소계·미확인 호출·불완전 총량을 구분한다. input·cached·output·reasoning·total 중 provider가 준 구성요소만 기록하고, 누락된 각 항목은 서로 독립적인 `null/unknown`과 사유로 남긴다. 일부 구성요소가 없다는 이유로 제공된 다른 구성요소를 폐기하거나 0·예약량·추정치로 채우지 않는다. token·API 가격·계정 사용률 %를 구독 한도 차감량이나 특정 작업의 정확한 요금으로 환산하지 않는다. 총 필요 호출 수·시간·token을 계산할 수 있다고 약속하지 않는다.

모든 활성 실행은 immutable `max_provider_calls`와 `absolute_deadline`을 결정적 hard stop으로 결속한다. token stop은 사용자가 명시적으로 선택한 경우에만 provider가 관측해 준 token 소계에 적용하는 best-effort 중단 정책이다. 신규 정책의 `call_reservation_tokens`는 선택적인 deprecated 호환 필드이며 admission·요금·구독 한도 계산에 쓰지 않는다. usage 결측이 있으면 정확한 잔여 token이나 strict cap을 주장하지 않고, 결측 자체만으로 terminal 결과·후속 실행을 차단하지 않는다. 호출·시간·token stop은 소요량 예측·요금 상한·완료 보장이 아니다. 미제공 실시간 지원이나 exact usage backfill은 필수 의존성이 아니다. 과거 token budget·잠정 차감과 숫자 필드는 역사 reader에서 원래 의미로 읽으며 새 실행 한도로 재해석하지 않는다.

## D07. 유효 운영 제한 보존 — planned

| 제한 | 보존할 값과 책임 |
|---|---|
| Goal provider 호출 | 활성 실행에 결속한 `max_provider_calls`; 예약 전 Core가 계산하는 hard stop |
| 절대 실행 기한 | 활성 실행에 결속한 `absolute_deadline`; 재시작·새 tick으로 연장하지 않는 hard stop |
| 관측 token stop | 사용자 opt-in인 경우만 적용; 제공된 구성요소의 소계에 대한 best-effort stop이며 결측을 추정하지 않음 |
| planning 역할 호출 | 준비·피드백·필수 재검토를 포함한 전체 14회 |
| 후보 | version 5개, 후보 refinement 1회; 별도 결속된 기존 단계별 복구 정책의 범위를 확인하며 임의로 기회를 늘리지 않음 |
| replan | 동일 실패 최대 2회, Goal 전체 최대 5회; 첫 재계획 뒤 새 evidence 없는 반복 차단 |
| resume | 1회; 기존 저장 turn 관측과 실제 새 resume 호출을 구분 |
| schema 재시도 | 기본 0회; 원본 실패를 성공으로 덮지 않음 |
| 역할 timeout | 기본 900초, 명시된 compact reviewer 1800초 |
| timeout 후 관측 / RPC | 관측 30초 / 개별 RPC 5초; RPC 대기는 전체 관측 창에 포함 |

각 제한의 실제 적용 범위·기산점·소비/해제 조건은 구현 시 기존 호출 계보와 대조해 고정하고 검증한다. 이 문서의 표만으로 현 코드가 집행함을 주장하지 않는다. timeout의 시작·deadline·관측 창을 요청·receipt·checkpoint에 결속한다. 값은 예상 필요량이 아니다. 계정 rate-limit은 관측된 오류·reset 조건에 따라 대기할 수 있으나 조회 불가 자체로 정지하지 않는다. 새 ID·원장으로 소진된 호출·복구 한도를 초기화하지 않는다.

## D08. 실제 효과 직전 검증과 재시작 — planned

예약 때뿐 아니라 실제 효과 직전에 input freshness와 target/context/prompt/requested model/policy 결속을 재검사한다. 변경된 입력을 묵시적으로 실행하지 않는다. reserve/start 사이 입력 변화, 생성 응답 유실, abrupt process death, timeout을 별도로 검증한다. lease 만료·collector 종료·interrupt ACK는 실제 provider terminal이 아니다.

재시작은 저장된 intent·binding·receipt와 provider 상태를 재개 없이 먼저 관측한 뒤, 실제 후속 turn이 필요한 경우에만 새 turn을 만든다. 허용된 부분 쓰기 후 resume와 immutable 입력 변경을 구분한다. cancel과 실행 중 재계획도 기존 효과를 지우거나 중복 생성하는 우회로가 될 수 없다. scope 밖 자동 rollback·완전한 exactly-once를 보장하지 않는다.

## D09. 근거 기반 복구와 ContextRequest — planned

실패 분류는 provider가 구조화해 반환한 code, 로컬 Engine이 직접 관측해 만든 code와 직접 evidence가 우선이다. 모델 응답에 적힌 error code·원인·효과 상태는 `model_reported` 진단 가설이며 provider/local code로 승격하거나 자동 복구의 단독 근거로 쓰지 않는다. `implementation`, `context`, `task_contract`, `dependency`, `environment`, `requirement_change`, `external_unknown`을 원인에 맞게 사용하며 모든 failed terminal을 implementation으로 분류하지 않는다. 의미가 불명확할 때 진단 모델을 쓰고 근거가 부족하면 `unclassified`를 유지한다.

허용된 로컬 `ContextRequest`는 필요한 source·selector·이유를 기록하고 초기 sample 밖까지 자동 탐색하여 해소한다. 접근 불가 사실·사용자 취향·승인 경계 확장에만 질문한다. 1.0은 이 로컬 Context 해소, `external_unknown`의 observe-first 처리와 직접 evidence에 결속한 실제 Task repair 또는 subgraph replan 한 경로를 끝까지 검증한다. 나머지 분류에는 typed vocabulary와 명시적 정지·Execution Spec/Plan/Goal revision routing을 보존하되 범용 자율 복구기를 필수 범위로 확대하지 않는다. 원인·새 근거 없는 반복 복구를 중단한다. 실패 관측에 결속한 복구는 실패 원태스크의 성공을 선행조건으로 삼지 않는다.

## D10. Planning과 ProjectMap의 정확한 범위 — planned

같은 의미/canonical Plan만 dedupe한다. 서로 다른 전략은 목표 적합성·품질·위험을 비교하며 비용 추정만으로 우월성을 선언해 가지치기하지 않는다. 후보 수는 Hard AC 개수만으로 늘리지 않고 Goal에 실제 전략 trade-off가 있을 때만 늘린다. 실행 경로가 소비하지 않는 `CommitHorizon` 설정은 새 schema에 저장하지 않는다. schema 3의 과거 reader는 원래 필드와 의미를 보존한다.

`ProjectMap`은 Goal에 필요한 범위에서 실제 관측한 파일과 `AGENTS.md`·등록 자료를 먼저 색인하고, Task 준비나 ContextRequest에 필요한 symbol과 `observed_link_refs`만 lazy 수집한다. `test`·`build` 표지는 관측 파일의 분류일 뿐 module/test/build 관계를 추론하지 않는다. 전체 저장소의 symbol/module 의존성을 미리 만들거나 완전한 그래프라고 주장하지 않는다. 필요한 사실만 State/Context에 투영하며 영향 매트릭스는 보수적 평가 재사용 근거로 제한한다. 범용 그래프 제품을 새로 구현하는 승인이 아니다.

## D11. schema 4와 과거 reader — planned

Engine schema 4는 별도의 새 DB로 만든다. schema 3/raw receipt/history 호환은 제품 runtime과 분리한 최소 read-only inspector로 제공한다. inspector는 기존 상태·receipt·usage/history를 원래 의미로 조회·보고하는 데 필요한 표면만 가지며 새 Engine 실행·import·상태 전이는 담당하지 않는다. 원본 DB·receipt·History 수정, prototype/운영 DB의 자동 제자리 변환, 가짜 Goal/Profile 생성, 옛 token budget 재해석을 금지한다. 새 DB는 기록을 지워 비용·복구 한도를 초기화하는 수단이 아니다. 조율 메타데이터 DB의 migration은 제품 Engine migration과 별개다.

## D12. provider와 배포물 경계 — planned

제품 기본 provider는 qualification된 v1을 유지한다. v2 static 11/qualification 13과 관련 실제 Goal 경로 근거는 v2 자체 채택 조건이며 모든 제품 실행의 선행조건이 아니다. provider version 사이 임의 fallback이나 checkpoint 재사용을 금지한다.

패키지는 Engine-only 사용자 CLI와 필요한 shared canonical 자산을 포함한다. legacy/eval/developer 도구는 분리한다. source-tree 평가 도구에는 명시적 source root와 재현 입력 bundle을 요구할 수 있다. wheel에 없는 fixture/config가 있다고 가정하지 않으며 깨끗한 non-editable 설치에서 확인한다.

## V01. 기능·안전 기반 1.0 필수 검증 — planned / 미실행

모든 필수 검증과 독립 최종 감사가 통과해야만 1.0 패키지 전환을 한다. 최신 작업 지시에 따른 main checkout의 개발·커밋은 이 전환 이전에도 수행한다. 문서 정합성 통과는 제품 PASS가 아니다. 아래는 각각 필수 책임이며 과거 51개 재검증이나 1,056개 결과는 대상·시점이 다른 provenance다. 새 실행으로 세지 않는다. 도구 환경 실패와 제품 실패를 구분한다.

전체 campaign 전에 실제 요청 수직 canary를 실행한다. canary는 `요청 → Goal 정규화·독립 review → Plan 선택 → 승인 → Task 실행 → 독립 Validator → Verdict`를 하나의 얇은 경로로 연결하고, 별도 fault canary는 효과 미확정 상태에서 observe-first와 중복 효과 0을 입증한다. canary가 실패하면 FM-06·FM-07의 범용 기능이나 평가 harness를 확장하지 않고 원인 계약을 먼저 줄이거나 고친다. 동결한 fixture·prompt·schema·model inventory·evaluator digest가 본 campaign과 같으면 통과한 역할·Planning canary cell을 아래 48회·18회에 포함하며 별도 추가 수량으로 만들지 않는다.

| 검증 | 필수 수용 기준 | 담당 |
|---|---|---|
| 결정적·호환·설치 | schema/DAG/원장/정책/검사 Gate, 변경 영향 회귀, schema 3 최소 read-only inspector와 schema 4 신규 DB, candidate wheel의 깨끗한 non-editable 설치. 모든 qualification evidence는 허용 root·파일 SHA-256·cell/fixture/seed/freeze에 결속한다. release project E2E는 절대 경로 candidate wheel과 그 SHA-256·배포판 이름/버전·non-editable 설치·import 경로·wheel 내부 package bytes까지 harness와 최종 scope verifier에서 재확인한다. source 기반 진단은 release PASS로 승격하지 않는다 | FM-11 harness, FM-12 검증 |
| 실제 역할 48회 | Plan 8 + Goal 8 fixture × 3 seed 전 cell 완료; required finding recall ≥90%, precision ≥85%, critical false admission 0, clean false block 0, schema failure 0, critical admission seed instability 0 | FM-11, FM-13 |
| 실제 Planning 18회 | 6 fixture × 3 seed 전 cell 완료; 정상 4종은 선택, 실제 정보 부족 2종은 의미 있는 blocking question; 일반 오류를 정상 blocked로 계산하지 않음; 준비 포함 역할 호출 ≤14, 후보 version ≤5, 선택 후보 deterministic finding 0 | FM-11, FM-13 |
| 실제 요청 E2E | 실제 요청 → Goal 정규화 → 독립 review → Plan 선택 → 한 번의 승인 → Task 실행 → 독립 검사 → 최종 결과를 실제 provider로 연결 | FM-11, FM-14 |
| 독립 최종 감사 | 서로 입력을 공유하되 결론을 공유하지 않는 두 감사에서 필수 검증·허용 효과·비목표·패키지·증거/실행 설정과 남은 실패를 각각 확인하고 Core가 finding을 결정적으로 join | FM-15 |
| 로컬 전환 | 감사 통과 뒤 main의 검증된 변경 확인·1.0 전환·동일 wheel digest의 delta 확인. 전환 전 main 개발·커밋과 구분 | FM-16 |

`QualificationCellOutcome`의 evidence는 허용 run root 안의 상대경로, SHA-256, kind, cell ID, evaluation contract와 fixture/seed/freeze binding을 가진 typed record다. harness 생성 시와 최종 scope verifier 집계 시 각각 검증하며 path escape, 파일 삭제·변조, wrong cell/fixture/contract/wheel과 검증 없는 legacy 경로 문자열은 release PASS로 만들지 않는다. developer harness는 명시 source root에서 외부 launcher로 읽고 제품 module은 같은 격리 Python에 non-editable로 설치한 candidate wheel에서 import한다. 평가 전 candidate probe가 distribution name·version·import root·wheel/설치 package bytes와 개발 harness 의존성 로드를 확인한다. 같은 환경과 wheel binding을 project-e2e run metadata·evidence·최종 verifier에 보존한다.

모든 finding 100% 검출을 추가 합격선으로 만들지 않는다. E2E에서 harness가 Goal/Plan/rating을 미리 작성한 결과는 실제 요청부터 시작하는 책임을 대신하지 못한다.

실제 역할 48회와 Planning 18회 수량·합격선은 유지한다. 각 campaign은 입력을 먼저 freeze한 뒤 서로 독립인 fixture/seed shard를 병렬 실행하고 aggregate에서 누락·중복·digest 불일치를 결정적으로 검사한다. shard 하나의 실패가 이미 완료된 독립 shard의 원시 결과를 지우지는 않지만, 필수 cell이 실패하거나 실행되지 않으면 전체 Gate는 PASS가 아니다.

### V02. E2E 책임별 evidence — planned / 미실행

| 필수 책임 | 구현 연결 | 검증 연결 |
|---|---|---|
| 다중 Task DAG 및 승인 내 replan/repair | FM-03, FM-06, FM-08 | FM-11, FM-14 |
| 직접 evidence에 결속한 실제 Task repair 또는 subgraph replan 한 경로 | FM-06, FM-08 | FM-11, FM-14 |
| read_only의 근거 있는 완전한 응답 및 프로젝트 무변경 | FM-06, FM-08 | FM-11, FM-14 |
| 초기 sample 밖 로컬 Context 검색 | FM-06, FM-07 | FM-11, FM-14 |
| usage missing·늦은 관측에도 valid terminal 결과 진행 | FM-02, FM-04 | FM-11, FM-14 |
| usage 구성요소별 nullable·관측 token stop의 best-effort 의미·호출/절대기한 hard stop | FM-02, FM-04 | FM-11, FM-14 |
| requested/provider-observed/local-derived/model-reported provenance와 실제 model/effort 미확인 표현 | FM-02, FM-04, FM-07 | FM-11, FM-14 |
| 저장 후 재시작 | FM-04, FM-05 | FM-11, FM-14 |
| 생성 응답 유실·강제 종료·timeout 후 중복 없음 | FM-04, FM-05 | FM-11, FM-14 |
| provider terminal과 외부 effect 확인 분리, model-reported code의 비권위성 | FM-02, FM-04, FM-05, FM-06 | FM-11, FM-14 |
| reserve/start 사이 stale 입력 | FM-05 | FM-11, FM-14 |
| 부분 쓰기 resume와 immutable 입력 변경 구분 | FM-05 | FM-11, FM-14 |
| 금지 효과·범위 확장·cancel·실행 중 재계획 보호 | FM-03, FM-05, FM-08 | FM-11, FM-14 |
| Worker와 분리된 Validator 실행·원자료 재관측·evidence binding | FM-04, FM-08 | FM-11, FM-14 |
| 스케줄러 tick 신속 반환·supervisor 책임·배정 결속 | FM-04, FM-08 | FM-11, FM-14 |
| 깨끗한 non-editable install | FM-10 | FM-12, FM-14 |

각 책임마다 실제 provider, synthetic stub, fault injection, 과거 evidence 재사용을 구분해 기록한다. 소수의 고정 case 수만으로 책임 충족을 주장하지 않는다. source/fixture/prompt/schema/lock/evaluator digest를 실행 전에 고정하며 판정 후 oracle·threshold를 낮추지 않는다. 공통 계약이 바뀐 이번 최초 qualification의 실제 역할·Planning 평가는 새로 수행한다. 이후 재사용은 보수적 영향 매트릭스로 유효성이 확인된 evidence에만 허용한다.

## V03. 비차단 후속 범위와 효과 제한

R3.1 대비 token/speed, performance 36, 비교 lifecycle 최적화와 v2 static 11/qualification 13은 별도 비차단 보고 또는 해당 provider 채택 Gate다. 원래 결과·fixture·보고서를 수정하거나 당시 판정을 PASS로 바꾸지 않는다. GUI, Localizer/번역 최적화, MCTS/광범위 그래프, 동일 프로젝트 병렬, remote/multiOS hardening은 1.0 이후다. 활성 job 동안의 supervisor는 필수 구현이며 후속 GUI나 전역 daemon과 혼동하지 않는다.

FM-09의 개발 조율·dispatch·원장 자동화는 제품 runtime 기능이나 1.0 품질 Gate가 아니다. 재현 가능한 release evidence를 수집하는 delivery 보조 도구로 유지하되, 그 자체의 확장·완성도와 사용자의 실제 Codex 예약 `ACTIVE`/`PAUSED` 상태를 제품 critical path에 넣지 않는다. 제품 scheduler의 `run_once` 의미와 supervisor 결속은 동결 wheel·격리 Engine DB에서 연속·동시·재시작 subprocess tick으로 FM-04·FM-08·FM-14에서 직접 검증한다. 실제 Codex 예약 연동은 사용자 opt-in 비차단 운영 검사다.

최종 1.0 package name·version·entrypoint·사용자 설정 표면을 release freeze 전에 확정한다. 깨끗한 non-editable 설치는 그 최종 candidate wheel digest에 대해 한 번 완전 검증하고 FM-14는 같은 설치 환경에서 실제 요청 E2E를 수행한다. FM-16은 검증된 동일 wheel·lock·환경 provenance를 확인해 로컬 활성화와 표면 smoke만 수행하며 package metadata를 다시 바꾸지 않는다. wheel digest나 필수 환경이 바뀌면 이전 설치·E2E 근거를 재사용하지 않는다.

## V04. 1.0 실행 순서와 병렬 경계 — planned / 미실행

필수 기능 구현 중에는 결정적/fake canary로 계약 우회를 조기에 차단한다. 최종 package identity를 포함한 candidate wheel·최소 harness·결정적/설치 검증을 끝내고 release input을 freeze한 뒤, 같은 freeze의 얇은 실제 요청 수직 canary와 대표 effect-unknown fault를 먼저 통과시킨다. 그 canary cell을 최종 수량에 포함하고 나서 Role 48, Planning 18과 서로 독립인 FM-14 lane을 가능한 범위에서 병렬 실행한다. 같은 프로젝트를 변경하는 제품 Task는 기존 직렬 원칙을 따르지만, immutable fixture를 읽고 별도 artifact를 쓰는 qualification shard에는 그 원칙을 적용하지 않는다.

두 FM-15 감사는 서로의 중간 결론을 보지 않고 병렬 수행한다. Core는 두 제출물의 evidence ref·finding code·affected Task를 원장 catalog에 대조해 결정적으로 join하며, 충돌이나 누락을 다수결로 숨기지 않는다. 모든 필수 finding을 처리한 뒤에만 FM-16 preflight와 로컬 활성화를 진행하고, postverify는 이미 검증한 동일 candidate wheel과 최종 identity가 바뀌지 않았는지 확인한다.

현재 변경 대상은 최신 명시 지시에 따른 `D:/codex/flowmarshal`의 `main` 브랜치 main checkout이다. FlowMarshal 1.0 릴리스 완료까지 파일 변경과 커밋은 이 위치에서만 수행한다. 승인 계획에 남은 `D:/codex/fm-performance-floor`와 해당 브랜치는 필수 입력의 출처로 보존하며 현재 쓰기 대상으로 사용하지 않는다. FM-16의 필수 감사 후 1.0 전환 조건은 유지한다. 원격 push·공개 배포·PyPI 업로드·외부 메시지·삭제·인증/권한 변경·크레딧 구매/사용은 포함하지 않는다. main checkout의 무관한 상태와 `D:/codex/fm-recovery`의 기존 변경은 보존한다. 적용 지침의 경로·효과 충돌 처리와 한시 규칙 만료 조건은 [현재 인계](pre-1.0-handoff.md)에 기록한다.

## M01. 설계별 구현 연결표

| 승인 항목 | 권위 반영 위치 | 후속 구현 책임 | 필수 검증 |
|---|---|---|---|
| 1 | D01, 제품 설계 §2·4 | FM-03, FM-08 | FM-12, FM-14 |
| 2 | D02, 제품 설계 §4·9 | FM-03, FM-05 | FM-12, FM-14 |
| 3 | D03, 제품 설계 §10 | FM-04, FM-08 | FM-12, FM-14 |
| 4 | D04 | FM-04, FM-06, FM-08 | FM-12, FM-14 |
| 5 | D05, 제품 설계 §13 | FM-02, FM-04 | FM-12, FM-14 |
| 6 | D06 | FM-02, FM-04 | FM-11, FM-14 |
| 7 | D07 | FM-02, FM-04, FM-06, FM-07 | FM-12, FM-13, FM-14 |
| 8 | D08, 제품 설계 §9 | FM-05 | FM-12, FM-14 |
| 9 | D09 | FM-06 | FM-12, FM-14 |
| 10 | D10, 제품 설계 §5·6 | FM-07 | FM-12, FM-13 |
| 11 | D11, ADR | FM-02, FM-10 | FM-12, FM-15 |
| 12 | D12, ADR | FM-07, FM-10 | FM-12, FM-13, FM-15 |

이 표는 책임 추적표이며 새 task 등록이나 완료 상태가 아니다. 정확한 명세·의존성·dispatch·검사 상태의 권위는 구현 조율 SQLite 원장이다. Worker는 원장을 직접 변경하거나 다음 앱 작업을 생성하지 않는다.
