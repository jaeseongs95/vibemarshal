# 목표 승인과 Plan revision

`GoalAuthorization`은 활성 Goal의 원문·목표·Hard AC·비목표를 포함하는 digest, Goal 계보, 프로젝트의 실제 root, Profile, 효과 정책과 운영 상한을 결속한다. 승인 기록은 strict/frozen 모델이며 schema 4의 append-only 테이블에 저장한다. 기존 schema 3 및 운영 DB는 변환하지 않는다. 사용자 승인과 선택 Plan 활성화는 `EngineApplication`이 `EngineService.authorize_goal_and_activate_plan`을 호출해 하나의 Core transaction으로 기록한다. 단일 승인 API `EngineService.authorize_goal`도 같은 권위 검사를 요구한다. `source`는 감사 표식이며 승인 권한이나 암호학적 신원 증명이 아니다.

설치된 사용자 CLI의 `authorize --project-id <project>`는 `TrustedConsoleHost`에서 현재 Goal·프로젝트·효과·운영 정책과 Core가 선택한 Plan을 포함한 전체 승인 대상을 표시한다. 사용자가 표시된 전체 `target_digest`를 그대로 입력해야 `ApplicationAuthority → EngineApplication → CoreActionAuthority` 경로로 일회성 capability를 소비한다. 사용자는 내부 Plan ID나 Plan digest를 별도로 복사하지 않는다. 거절·EOF·비대화형 입력, 표시 후 대상 변경, 잘못된 capability와 replay는 승인·활성화 쓰기 없이 거부한다. capability를 제공하지 않는 내부 `goal authorize` 경로는 사용자 승인 절차를 대신하지 못한다. 승인된 범위 안의 후속 Plan revision은 Core가 검토·Gate를 확인해 활성화하며, 진단용 `plan activate --plan-revision-id ... --digest ...`도 기존 GoalAuthorization을 요구한다.

Worker·Validator에는 host·authority·service·DB handle·capability를 전달하지 않으며 역할 scope의 host 재진입도 거부한다. 이 Application 권위 경계는 같은 OS 사용자의 raw SQLite 직접 쓰기나 hostile same-process 코드 격리를 보장하지 않는다. 상세 보장 범위와 known limitation은 [승인 계약 D02](redesign-1.0-contract.md)를 따른다.

내부 재계획은 `register_authorized_plan_revision(evaluation)`으로 review·결정적 Gate를 검증하고 후보를 저장한 뒤 승인 경계 안에서 활성화한다. Task 분할·검사 의미·DAG 변경에는 새 Plan과 review가 필요하며, 같은 의미의 경로·명령·Context 수정에는 기존 ExecutionSpec revision 경계를 사용한다. Goal 변경, root·Profile 변경, 허용 외부 효과와 운영 상한 확장은 `GOAL_AUTHORIZATION_REQUIRED`와 승인값·요청값·근거 ref를 반환한다. 명시 token 중단 상한과 planning 호출 상한을 줄이는 변경은 재승인을 요구하지 않는다. 자연어 범위와 효과 설명의 적합성은 근거를 결속한 독립 review의 책임이며 OS sandbox나 의미 안전성의 수학적 보장이 아니다.

Plan 교체 시 reserved/starting/running/unknown Attempt, 미확정 intent/provider call, 검증 중 Task가 있으면 `PLAN_REPLACEMENT_IN_FLIGHT`로 교체 전체를 지연한다. 원래 Task·Attempt·thread/turn·receipt를 보존하고 완료/확정 관측 뒤 같은 후보를 활성화할 수 있다. 사용자 재승인을 요구하는 상태는 아니다. supervisor의 자동 재관측·재계획 tick 연결은 별도 응용/복구 작업의 책임이다.

예약 이후에도 새 provider 효과, 실행 준비, Task 검사 명령과 Goal Test 직전에 승인 경계를 재검사한다. Goal별 budget override는 project 기본값보다 우선하므로 승인 당시와 현재의 실제 적용 상한을 비교한다. 실제 runtime 호출 전 승인 거부는 `effect_not_started`로 기록하고 재승인 뒤 같은 intent·Attempt·provider 예약을 사용한다. 호출을 시도하기 전에 `effect_dispatching`을 기록하므로 이후 응답 유실을 미실행으로 오판해 재실행하지 않는다. Core operation의 같은 경계는 기존 `operation.no_effect` 기록을 사용한다. 관측과 취소는 이 승인을 다시 요구하지 않는다.

완료 Task의 재사용은 동일 task ref·의미 계약·입력 dependency와 재사용 가능한 선행 Task, 최신 Worker epoch의 PASS 검증·직접 evidence, 검증 시점과 완료 시점 및 교체 시점의 로컬 파일·State 관측 일치에 한정한다. ProjectMapper가 관측한 파일 집합에 ExecutionSpec의 target·context·artifact 경로 digest를 추가 검사한다. 이 파일 관측은 전체 OS 상태 검증이 아니다. 외부 효과/외부 검증·수동 결정 또는 읽을 수 없는 입력의 자동 재사용은 보수적으로 제외한다. 관측이 바뀌거나 검사 의미가 바뀌면 새 Task를 실행하며 과거 완료·실패 결과는 그대로 보존한다.

`task_completion_reuse`는 새 Task에서 원본 검증·evidence를 읽는 연결이다. 과거 Attempt/receipt를 이동하거나 성공 실행을 새로 만들지 않는다. 후속 Task 입력과 Goal Test도 이 연결을 읽는다. 새 Plan의 독립 Goal Test는 별도로 수행해야 한다. 재사용 관측은 활성화 시점의 유효성 근거이며 이후 프로젝트 전체가 영구히 변하지 않는다는 보장이 아니다.

결정적 회귀는 `tests/test_engine_goal_authorization.py`, `tests/test_engine_authorization_freshness.py`, `tests/test_engine_plan_completion_reuse.py`에서 확인한다. 합성 provider와 임시 원장을 사용하므로 실제 역할·Planning·전체 요청 E2E·1.0 qualification을 대신하지 않는다.
