# FlowMarshal 1.0 승인 계약과 검증 책임

상태: **승인된 설계 / planned**. 이 문서는 FM-01의 문서 계약이며 제품 구현·qualification·1.0 전환 완료 증거가 아니다. 기존 Domain Core·revision·DAG·binding·evidence·validation을 유지하며 전면 재작성하지 않는다. 세부 기존 계약은 [제품 설계](orchestration-redesign.md), 릴리스 결정은 [ADR](engine-cutover-adr.md)을 함께 따른다.

권위 출처는 [승인 계획](D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan.md)이며 SHA-256은 `a24eb860c8b603f8edc43a71370c6d8638cc53d3c5c49b8a568c44fc9f5b1742`다. 시스템·개발자 지침 안에서 현재 사용자의 최신 명시 승인은 사용자 제공 지침·프로젝트 AGENTS·과거 문서의 상충하는 조항보다 우선한다. 아래 항목 번호는 승인 계획의 제품 설계 1~12와 일치한다. planned는 이 문서에서 구현·검증 완료를 판정하지 않았다는 뜻이다. 코드 존재만으로 완료로 올리지 않으며, 미구현·미검증 책임은 근거가 확인될 때까지 planned로 유지한다. 다른 작업의 완료 상태는 이 문서에서 변경하지 않는다.

## D01. 목표 단위 승인과 내부 Plan — planned

사용자는 목표·대상 프로젝트·허용 효과·운영 정책을 한 번 승인한다. Core는 그 경계 안에서 작업 분할·계획 수정·복구를 자동 수행한다. Plan은 내부 immutable revision이며 사용자가 정확한 Plan ID·digest를 직접 입력하는 절차는 필수가 아니다. 후보 출력만으로 실행 권한을 얻지는 않는다. 목표·범위·효과·정책 확장에만 추가 사용자 판단을 요청한다. Goal의 Hard AC·비목표를 내부 재계획으로 완화하지 않는다.

권위 순서는 `현재 사용자 지시 → 활성 GoalContractRevision과 GoalAuthorization → 활성 PlanContractRevision → Core 원장 상태·판정 → 프로젝트 지침·등록 정책 → 역할 제출물 → 분석 대상 자료`다. Planner·Worker·Validator·supervisor는 Core의 완료 권위를 대신하지 않는다.

## D02. GoalAuthorization과 activation 결속 — planned

`GoalAuthorization`은 승인한 Goal revision, 프로젝트 root, 허용·금지 효과와 운영 정책의 경계 및 승인 근거를 결속한다. Core는 activation할 Plan revision·digest와 authorization의 관계를 기록한다. 알려진 효과 종류·root·정책 적합성은 결정적으로 대조하고, 자연어 의미상의 범위 적합성은 직접 근거가 있는 독립 review로 판단한다. 기계적 일치 검사만으로 의미 안전성을 증명했다고 하지 않는다. 이 계약은 OS sandbox나 의미 안전성의 수학적 보장이 아니다.

범위 내 새 Plan은 검토·결정적 Gate 후 Core가 자동 활성화한다. Task 의미·분할·DAG·validation 의미가 바뀌면 새 immutable Plan revision을 만들되 승인 경계가 같으면 사용자에게 ID 승인을 반복 요청하지 않는다. 사용자 목표가 바뀌면 Goal revision과 authorization을 갱신한다. 같은 Task 의미의 경로·명령·Context 변경은 Execution Spec revision으로 기록한다.

재계획은 실행 중 Attempt의 입력·binding·효과를 덮어쓰거나 실행 슬롯을 회수하지 않는다. 진행 중 작업과 변경 subgraph의 충돌을 확인하고 보호한 뒤 activation한다. 완료 evidence는 대상·입력·검사 의미·freshness의 유효성을 확인한 경우만 새 revision에서 재사용하며 과거 실패와 원본 결과를 보존한다.

재사용된 Task에 새 검사 결과가 기록되면 이전 PASS와 함께 기록 순서로 조회해 최신 결과를 반영한다. 원본이나 중간 Task에 늦게 기록된 실패·미확정 결과도 재사용 계보를 따라 현재 Goal 판정에 반영한다. 다음 Plan으로 다시 재사용할 때는 원본과 중간 Task의 최신 검사 모두 기존 완료 근거에 결속되어야 한다. 새 검사로 이 결속이 달라지면 원래 기록을 보존하고 새 Task를 미완료 상태에서 검증한다.

효과 정책만 바뀐 Goal revision에서 외부 허용 효과 제거, 금지 효과 추가, 비가역 효과 checkpoint 강화는 기존 승인 경계 안의 축소로 비교한다. mutation·behavior 정책과 Goal의 나머지 필드는 동일해야 하며 새 Goal에 결속된 Plan의 검토·Gate를 생략하지 않는다. Task 효과는 축소된 활성 Goal 정책에도 맞아야 한다. Attempt가 아직 없는 준비 RuntimeJob도 보호하며, 예약·실행·interrupt·collector 유실·terminal 결과 미소비 상태에서는 Plan 교체를 보류한다. Core가 결과를 소비하거나 미실행 job을 취소한 뒤 교체할 수 있다.

## D03. 응용 명령과 짧은 tick — planned

`EngineApplication`은 `prepare / authorize / run_once / observe / pause / cancel / status / final-report`의 명령 경계를 연결한다. CLI는 이 응용 계층을 호출하며 별도 상태 권위를 만들지 않는다. 명령 이름은 설계 경계이며 현재 CLI에서 모두 제공된다는 뜻은 아니다.

`RuntimeJobSupervisor`는 활성 job 동안만 provider 연결·stream·receipt·terminal·usage·절대 deadline을 관리한다. `run_once`는 job 예약/시작 또는 이미 도착한 관측 결과 소비 후 신속히 반환한다. 스케줄 tick 안에서 전체 모델 turn 종료를 기다리지 않는다. 전역 상주 daemon은 필수 의존성이 아니다. supervisor는 관측을 저장하고 Core가 상태 전이·검사·완료를 결정한다. 재시작이나 새 tick이 기존 job의 절대 deadline을 초기화하지 않도록 결속한다.

## D04. 모든 활성화 후 역할의 job화 — planned

다음 역할 호출은 모두 `RuntimeJob`과 영속 checkpoint에 포함한다: `execution_spec_prepare`, `worker_turn`, `task_semantic_validate`, `goal_test_prepare`, `goal_semantic_validate`, 활성화 후 자동 recovery/replanning의 역할 호출. Worker만 비동기화하고 준비·검사를 tick 안에서 동기 대기하는 구현은 이 책임을 충족하지 못한다. 승인 전 명시적인 대화형 준비는 동기 `PlanningCoordinator/RoleRunner`를 유지할 수 있다.

## D05. 실행·효과와 usage 관측 분리 — planned

실행 admission·예약·슬롯과 사용량 회계 관측을 별도 축으로 유지한다. provider terminal 및 유효 결과가 확인되면 usage 누락만으로 후속 실행을 막지 않는다. 필수 evidence·validation·효과 안전 검사는 그대로 적용한다. 누락 token은 `null/unknown`과 사유이며 0·예약량·추정치로 채우지 않는다. 종료 미확인과 종료 후 usage 미확인은 서로 다른 상태다.

외부 생성·실행 효과가 미확정이면 기존 intent·thread/turn binding·receipt를 먼저 관측하며 무조건 재실행하지 않는다. 늦은 usage는 원본 receipt를 보존한 회계 관측만 멱등 추가한다. 늦은 사용량으로 실행 슬롯 환불·재차감·재실행을 유발하지 않는다. Goal 등록 전 호출도 원래 프로젝트·Goal 계보·요청·receipt에 결속하고 가짜 Goal/Profile을 만들지 않는다.

## D06. 관측값과 운영 한도의 의미 — planned

실제 관측 token은 유효한 근거이며 관측 소계·미확인 호출·불완전 총량을 구분한다. token·API 가격·계정 사용률 %를 구독 한도 차감량이나 특정 작업의 정확한 요금으로 환산하지 않는다. 총 필요 호출 수·시간·token을 계산할 수 있다고 약속하지 않는다.

선택적 총 호출/시간 제한은 사용자가 정한 중단 정책으로만 사용한다. 소요량 예측·요금 상한·완료 보장이 아니다. 미제공 실시간 지원이나 exact usage backfill은 필수 의존성이 아니다. 과거 token budget·잠정 차감은 역사 reader에서 원래 의미로 읽으며 새 실행 한도로 재해석하지 않는다.

## D07. 유효 운영 제한 보존 — planned

| 제한 | 보존할 값과 책임 |
|---|---|
| planning 역할 호출 | 준비·피드백·필수 재검토를 포함한 전체 14회 |
| 후보 | version 5개, 후보 refinement 1회; 별도 결속된 기존 단계별 복구 정책의 범위를 확인하며 임의로 기회를 늘리지 않음 |
| replan | 동일 실패 최대 2회, Goal 전체 최대 5회; 첫 재계획 뒤 새 evidence 없는 반복 차단 |
| resume | 1회; 기존 저장 turn 관측과 실제 새 resume 호출을 구분 |
| schema 재시도 | 기본 0회; 원본 실패를 성공으로 덮지 않음 |
| 역할 timeout | 기본 900초, 명시된 compact reviewer 1800초 |
| timeout 후 관측 / RPC | 관측 30초 / 개별 RPC 5초; RPC 대기는 전체 관측 창에 포함 |

각 제한의 실제 적용 범위·기산점·소비/해제 조건은 구현 시 기존 호출 계보와 대조해 고정하고 검증한다. 이 문서의 표만으로 현 코드가 집행함을 주장하지 않는다. timeout의 시작·deadline·관측 창을 요청·receipt·checkpoint에 결속한다. 값은 예상 필요량이 아니다. 계정 rate-limit은 관측된 오류·reset 조건에 따라 대기할 수 있으나 조회 불가 자체로 정지하지 않는다. 새 ID·원장으로 소진된 호출·복구 한도를 초기화하지 않는다.

## D08. 실제 효과 직전 검증과 재시작 — planned

예약 때뿐 아니라 실제 효과 직전에 input freshness와 target/context/prompt/model/policy 결속을 재검사한다. 변경된 입력을 묵시적으로 실행하지 않는다. reserve/start 사이 입력 변화, 생성 응답 유실, abrupt process death, timeout을 별도로 검증한다. lease 만료·collector 종료·interrupt ACK는 실제 provider terminal이 아니다.

재시작은 저장된 intent·binding·receipt와 provider 상태를 재개 없이 먼저 관측한 뒤, 실제 후속 turn이 필요한 경우에만 새 turn을 만든다. 허용된 부분 쓰기 후 resume와 immutable 입력 변경을 구분한다. cancel과 실행 중 재계획도 기존 효과를 지우거나 중복 생성하는 우회로가 될 수 없다. scope 밖 자동 rollback·완전한 exactly-once를 보장하지 않는다.

## D09. 근거 기반 복구와 ContextRequest — planned

실패 분류는 명시적 transport/error code와 직접 evidence가 우선이다. `implementation`, `context`, `task_contract`, `dependency`, `environment`, `requirement_change`, `external_unknown`을 원인에 맞게 사용하며 모든 failed terminal을 implementation으로 분류하지 않는다. 의미가 불명확할 때 진단 모델을 쓰고 근거가 부족하면 `unclassified`를 유지한다.

허용된 로컬 `ContextRequest`는 필요한 source·selector·이유를 기록하고 초기 sample 밖까지 자동 탐색하여 해소한다. 접근 불가 사실·사용자 취향·승인 경계 확장에만 질문한다. 원인·새 근거 없는 반복 복구를 중단한다. 실패 관측에 결속한 복구는 실패 원태스크의 성공을 선행조건으로 삼지 않는다.

## D10. Planning과 ProjectMap의 정확한 범위 — planned

같은 의미/canonical Plan만 dedupe한다. 서로 다른 전략은 목표 적합성·품질·위험을 비교하며 비용 추정만으로 우월성을 선언해 가지치기하지 않는다. 후보 수는 Hard AC 개수만으로 늘리지 않고 Goal에 실제 전략 trade-off가 있을 때만 늘린다. 실행 경로가 소비하지 않는 `CommitHorizon` 설정은 새 schema에 저장하지 않는다. schema 3의 과거 reader는 원래 필드와 의미를 보존한다.

`ProjectMap`은 실제 관측한 파일·symbol·`observed_link_refs`로 검증된 연결만 표현한다. `test`·`build` 표지는 관측 파일의 분류일 뿐 module/test/build 관계를 추론하지 않는다. 전체 의존성을 완전히 복원한 그래프라고 주장하지 않는다. 필요한 사실만 State/Context에 투영하며 영향 매트릭스는 보수적 평가 재사용 근거로 제한한다. 범용 그래프 제품을 새로 구현하는 승인이 아니다.

## D11. schema 4와 과거 reader — planned

Engine schema 4는 별도의 새 DB로 만든다. schema 3/raw receipt/history는 read-only adapter로 읽을 수 있게 하며 원본 DB·receipt·History를 수정하지 않는다. prototype/운영 DB의 자동 제자리 변환, 가짜 Goal/Profile 생성, 옛 token budget 재해석을 금지한다. 새 DB는 기록을 지워 비용·복구 한도를 초기화하는 수단이 아니다. 조율 메타데이터 DB의 migration은 제품 Engine migration과 별개다.

## D12. provider와 배포물 경계 — planned

제품 기본 provider는 qualification된 v1을 유지한다. v2 static 11/qualification 13과 관련 실제 Goal 경로 근거는 v2 자체 채택 조건이며 모든 제품 실행의 선행조건이 아니다. provider version 사이 임의 fallback이나 checkpoint 재사용을 금지한다.

패키지는 Engine-only 사용자 CLI와 필요한 shared canonical 자산을 포함한다. legacy/eval/developer 도구는 분리한다. source-tree 평가 도구에는 명시적 source root와 재현 입력 bundle을 요구할 수 있다. wheel에 없는 fixture/config가 있다고 가정하지 않으며 깨끗한 non-editable 설치에서 확인한다.

## V01. 기능·안전 기반 1.0 필수 검증 — planned / 미실행

모든 필수 검증과 독립 최종 감사가 통과해야만 1.0 패키지 전환을 한다. 최신 작업 지시에 따른 main checkout의 개발·커밋은 이 전환 이전에도 수행한다. 문서 정합성 통과는 제품 PASS가 아니다. 아래는 각각 필수 책임이며 과거 51개 재검증이나 1,056개 결과는 대상·시점이 다른 provenance다. 새 실행으로 세지 않는다. 도구 환경 실패와 제품 실패를 구분한다.

| 검증 | 필수 수용 기준 | 담당 |
|---|---|---|
| 결정적·호환·설치 | schema/DAG/원장/정책/검사 Gate, 변경 영향 회귀, schema 3 read-only와 schema 4 신규 DB, 깨끗한 non-editable 설치 | FM-11 harness, FM-12 검증 |
| 실제 역할 48회 | Plan 8 + Goal 8 fixture × 3 seed 전 cell 완료; required finding recall ≥90%, precision ≥85%, critical false admission 0, clean false block 0, schema failure 0, critical admission seed instability 0 | FM-11, FM-13 |
| 실제 Planning 18회 | 6 fixture × 3 seed 전 cell 완료; 정상 4종은 선택, 실제 정보 부족 2종은 의미 있는 blocking question; 일반 오류를 정상 blocked로 계산하지 않음; 준비 포함 역할 호출 ≤14, 후보 version ≤5, 선택 후보 deterministic finding 0 | FM-11, FM-13 |
| 실제 요청 E2E | 실제 요청 → Goal 정규화 → 독립 review → Plan 선택 → 한 번의 승인 → Task 실행 → 독립 검사 → 최종 결과를 실제 provider로 연결 | FM-11, FM-14 |
| 독립 최종 감사 | 필수 검증·허용 효과·비목표·패키지·증거/실행 설정의 일치와 남은 실패를 확인 | FM-15 |
| 로컬 전환 | 감사 통과 뒤 main의 검증된 변경 확인·1.0 전환·설치 확인. 전환 전 main 개발·커밋과 구분 | FM-16 |

모든 finding 100% 검출을 추가 합격선으로 만들지 않는다. E2E에서 harness가 Goal/Plan/rating을 미리 작성한 결과는 실제 요청부터 시작하는 책임을 대신하지 못한다.

### V02. E2E 책임별 evidence — planned / 미실행

| 필수 책임 | 구현 연결 | 검증 연결 |
|---|---|---|
| 다중 Task DAG 및 승인 내 replan/repair | FM-03, FM-06, FM-08 | FM-11, FM-14 |
| read_only의 근거 있는 완전한 응답 및 프로젝트 무변경 | FM-06, FM-08 | FM-11, FM-14 |
| 초기 sample 밖 로컬 Context 검색 | FM-06, FM-07 | FM-11, FM-14 |
| usage missing·늦은 관측에도 valid terminal 결과 진행 | FM-02, FM-04 | FM-11, FM-14 |
| 저장 후 재시작 | FM-04, FM-05 | FM-11, FM-14 |
| 생성 응답 유실·강제 종료·timeout 후 중복 없음 | FM-04, FM-05 | FM-11, FM-14 |
| reserve/start 사이 stale 입력 | FM-05 | FM-11, FM-14 |
| 부분 쓰기 resume와 immutable 입력 변경 구분 | FM-05 | FM-11, FM-14 |
| 금지 효과·범위 확장·cancel·실행 중 재계획 보호 | FM-03, FM-05, FM-08 | FM-11, FM-14 |
| 스케줄러 tick 신속 반환·supervisor 책임·배정 결속 | FM-04, FM-09 | FM-11, FM-14 |
| 깨끗한 non-editable install | FM-10 | FM-12, FM-14 |

각 책임마다 실제 provider, synthetic stub, fault injection, 과거 evidence 재사용을 구분해 기록한다. 소수의 고정 case 수만으로 책임 충족을 주장하지 않는다. source/fixture/prompt/schema/lock/evaluator digest를 실행 전에 고정하며 판정 후 oracle·threshold를 낮추지 않는다. 공통 계약이 바뀐 이번 최초 qualification의 실제 역할·Planning 평가는 새로 수행한다. 이후 재사용은 보수적 영향 매트릭스로 유효성이 확인된 evidence에만 허용한다.

## V03. 비차단 후속 범위와 효과 제한

R3.1 대비 token/speed, performance 36, 비교 lifecycle 최적화는 별도 비차단 보고다. 원래 결과·fixture·보고서를 수정하거나 당시 판정을 PASS로 바꾸지 않는다. GUI, Localizer/번역 최적화, MCTS/광범위 그래프, 동일 프로젝트 병렬, remote/multiOS hardening은 1.0 이후다. 활성 job 동안의 supervisor는 필수 구현이며 후속 GUI나 전역 daemon과 혼동하지 않는다.

현재 변경 대상은 최신 명시 지시에 따른 `D:/codex/flowmarshal`의 `main` 브랜치 main checkout이다. FlowMarshal 1.0 릴리스 완료까지 파일 변경과 커밋은 이 위치에서만 수행한다. 승인 계획에 남은 `D:/codex/fm-performance-floor`와 해당 브랜치는 필수 입력의 출처로 보존하며 현재 쓰기 대상으로 사용하지 않는다. FM-16의 필수 감사 후 1.0 전환 조건은 유지한다. 원격 push·공개 배포·PyPI 업로드·외부 메시지·삭제·인증/권한 변경·크레딧 구매/사용은 포함하지 않는다. main checkout의 무관한 상태와 `D:/codex/fm-recovery`의 기존 변경은 보존한다. 적용 지침의 경로·효과 충돌 처리와 한시 규칙 만료 조건은 [현재 인계](pre-1.0-handoff.md)에 기록한다.

## M01. 설계별 구현 연결표

| 승인 항목 | 권위 반영 위치 | 후속 구현 책임 | 필수 검증 |
|---|---|---|---|
| 1 | D01, 제품 설계 §2·4 | FM-03, FM-08 | FM-12, FM-14 |
| 2 | D02, 제품 설계 §4·9 | FM-03, FM-05 | FM-12, FM-14 |
| 3 | D03, 제품 설계 §10 | FM-04, FM-08 | FM-12, FM-14 |
| 4 | D04 | FM-04, FM-06, FM-08 | FM-12, FM-14 |
| 5 | D05, 제품 설계 §13 | FM-02, FM-04 | FM-12, FM-14 |
| 6 | D06 | FM-02, FM-08 | FM-12, FM-15 |
| 7 | D07 | FM-02, FM-04, FM-06, FM-07 | FM-12, FM-13, FM-14 |
| 8 | D08, 제품 설계 §9 | FM-05 | FM-12, FM-14 |
| 9 | D09 | FM-06 | FM-12, FM-14 |
| 10 | D10, 제품 설계 §5·6 | FM-07 | FM-12, FM-13 |
| 11 | D11, ADR | FM-02, FM-10 | FM-12, FM-15 |
| 12 | D12, ADR | FM-07, FM-10 | FM-12, FM-13, FM-15 |

이 표는 책임 추적표이며 새 task 등록이나 완료 상태가 아니다. 정확한 명세·의존성·dispatch·검사 상태의 권위는 구현 조율 SQLite 원장이다. Worker는 원장을 직접 변경하거나 다음 앱 작업을 생성하지 않는다.
