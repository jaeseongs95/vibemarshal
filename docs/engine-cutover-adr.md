# ADR: FlowMarshal Engine 분리와 1.0 cutover

- 상태: **Accepted（설계）/ 재설계 구현·검증 planned**
- 대상: 기존 FlowMarshal Domain Core를 유지하는 1.0 Engine
- 권위: [승인 12항목과 필수 검증](redesign-1.0-contract.md), [제품 설계](orchestration-redesign.md)

## Context

R1~R3.1 prototype은 유용한 transport·원장·Planner 실험 자산이자 동결된 감사 기준선이다. 기존 Engine Core·revision·DAG·binding·evidence·validation을 전면 재작성하지 않고 승인·운영 계약을 바꾼다. 현재 사용자의 최신 명시 승인은 과거 사용자 제공 지침과 프로젝트 문서의 usage 누락 전역 차단·exact Plan 수동 승인·비교 성능 필수 릴리스 조건보다 우선한다.

## Decision

1. 새 제품 코드는 `flowmarshal.engine`에 유지한다. 기존 `flowmarshal.core`·`flowmarshal.planning` 도메인은 import하지 않고 검증된 Codex transport만 `CodexRuntimePort` 뒤에서 재사용한다.
2. SQLite application ID `0x464D4531` (`FME1`), 기본 파일명 `flowmarshal-engine.sqlite3`, 기본 artifact root `.flowmarshal-engine/artifacts`의 분리 원칙을 유지한다. application ID와 schema revision을 각각 검사한다.
3. schema 4는 별도 새 DB로 만든다 (**planned**). schema 3/raw receipt/history는 read-only adapter로 읽고 운영·prototype DB의 자동 제자리 변환, 가짜 Goal/Profile 생성, 옛 token budget 재해석을 금지한다. 조율 메타데이터 DB migration과 혼동하지 않는다.
4. 사용자 목표·범위·효과·운영 정책을 GoalAuthorization으로 승인받는다. Core는 적합한 내부 immutable Plan revision을 자동 활성화한다 (**planned**). root·알려진 효과·정책의 결정적 대조와 의미 범위 review를 구분하며 OS sandbox나 의미 안전성 보장을 주장하지 않는다.
5. 실행·효과·슬롯과 usage 관측을 분리한다 (**planned**). 유효 terminal 결과는 usage 누락만으로 진행을 막지 않고 외부 효과 미확정은 기존 binding을 먼저 관측한다. 늦은 usage는 회계 관측만 추가하며 원본 receipt·실측 null을 보존한다.
6. RuntimeJobSupervisor와 EngineApplication의 짧은 run_once tick을 연결한다 (**planned**). 활성화 후 준비·worker·검사·recovery/replanning을 모두 checkpoint에 포함하고 supervisor는 활성 job 동안만 연결·deadline을 관리한다. 완료 판정은 Core가 한다.
7. 기본 provider는 qualification된 v1이다. v2 static 11/qualification 13은 v2 자체 채택 조건이며 모든 제품 실행의 선행조건이 아니다. inventory 전체 원문은 감사용, 선택·허용 조합/executable/필수 capability projection은 운영 binding으로 유지한다. 임의 fallback·version 간 checkpoint 재사용을 금지한다.
8. package는 Engine-only 사용자 CLI와 필요한 shared canonical 자산을 포함한다 (**planned**). legacy/eval/developer 도구를 분리하고 source-tree 평가에는 명시 source root와 재현 입력 bundle을 요구할 수 있다. wheel에 없는 fixture/config를 가정하지 않는다.
9. 개발 CLI/package 이름은 `flowmarshal-engine`이다. 아래 필수 검증과 독립 감사 뒤에만 로컬 main 통합·`flowmarshal` 1.0 전환을 수행한다. 원격 push·공개 릴리스·PyPI 업로드는 이 승인의 범위가 아니다. main의 무관한 변경은 보존한다.

## Cutover 조건 — planned / 미실행

다음 책임을 모두 충족해야 한다. 정확한 임계값·책임별 evidence와 구현 task 연결은 [V01·V02](redesign-1.0-contract.md)를 따른다.

- 결정적 schema/DAG/원장/정책/검사 Gate·변경 영향 회귀 및 schema 호환 검증
- 실제 역할 48회 전 cell 완료와 recall·precision·critical/clean/schema/seed 기준
- 실제 Planning 18회 전 cell 완료와 정상 선택·진짜 정보 부족 질문·호출/후보 한도
- 실제 요청부터 Goal 정규화·독립 review·Plan 선택·한 번의 승인·다중 Task/복구·독립 검사·최종 결과까지의 E2E와 안전 책임 전부
- 깨끗한 non-editable 설치, 독립 최종 감사

하나라도 필수 책임이 누락·실패·미실행이면 1.0 전환은 NO-GO다. 합성 smoke·과거 51개/1,056개 검사·미리 작성한 Goal/Plan/rating으로 실제 신규 qualification을 대신하지 않는다. 공통 계약이 바뀐 최초 실제 역할·Planning 평가는 새로 수행하며 판정 후 oracle·threshold를 낮추지 않는다.

## Consequences와 비차단 범위

GoalAuthorization과 자동 activation, schema 4, supervisor, 패키징, 새 qualification은 문서 승인만 완료됐으며 구현·검증은 후속 task의 책임이다. 과거 원시 결과·fixture·receipt·History·R1~R3.1 동결 근거는 수정하지 않는다.

R3.1 token/speed·performance36·비교 lifecycle 최적화는 별도 비차단 보고다. 과거 `TokenLatencyGateReport` v3.0, `PerformanceQualificationReport` v4.0과 `ReleasePerformanceFloor` 수치·판정은 [이전 비교 계약](performance-release-floor-before-redesign-1.0.md)으로 보존한다. 과거 보고서의 cutover 필드를 현재 1.0 권위로 사용하지 않는다. GUI·Localizer/번역·MCTS/광범위 graph·동일 프로젝트 병렬·remote/multiOS hardening은 1.0 이후다.
