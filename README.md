# VibeMarshal

VibeMarshal은 사용자의 큰 요청을 검증 가능한 Task DAG로 만들고, Task마다 적절한 Codex 실행·검사 모델과 추론 수준을 배정한 뒤, 작업 생성·재개·진행·결과·실패·재시도를 추적하는 로컬 Workflow Orchestrator다. 코드와 CLI에서는 기존 식별자 `FlowMarshal`을 사용한다.

현재 권위 구현은 `flowmarshal.engine`이다. R1~R3.1 source와 artifact는 감사 가능한 `legacy/prototype` 기준선으로 동결하며 새 Engine에서 도메인 코드로 import하지 않는다.

자세한 계약은 [전면 재설계 권위 문서](docs/orchestration-redesign.md), 분리·전환 결정은 [Engine cutover ADR](docs/engine-cutover-adr.md)에 있다.

## 현재 판정

최신 상태의 단일 진입점은 [Engine 구현 현황](docs/engine-implementation-status.md)이다. [1.0 선행 로드맵](docs/pre-1.0-roadmap.md)에 승인된 구현 순서와 후속 기능을 구분했고, [현재 작업 인계](docs/pre-1.0-handoff.md)에 이번 변경과 다음 실행 조건을 기록한다.

현재 제품 1.0은 **미완료 / NO-GO**다. [승인된 12개 설계 항목과 필수 검증](docs/redesign-1.0-contract.md)을 문서에 반영했으며 새 GoalAuthorization·schema 4·RuntimeJobSupervisor·응용 CLI·패키징은 **planned**다. 실제 구현과 결정적·역할48·Planning18·실제 요청 E2E·설치·독립 감사는 후속 태스크의 책임이다. 과거 테스트와 실제 평가 결과는 [승인 이전 구현 현황](docs/engine-implementation-status-before-redesign-1.0.md)에 보존하며 새 PASS로 집계하지 않는다.

종료·유효 결과가 확인되면 usage 누락만으로 후속 실행을 막지 않고 미측정량은 null/unknown으로 남긴다. 외부 효과 미확정은 기존 intent/binding을 먼저 관측한다. 기본 provider는 qualification된 v1이며 v2 채택 평가와 R3.1 비교 성능은 모든 제품 실행의 선행조건이 아니다. 비교 성능 보고는 비차단 후속이다.

R3.1은 동결된 prototype 감사 기준선이다. [최종 동결 판정](docs/r31-frozen-baseline.md)을 현재 Engine의 qualification으로 재사용하지 않는다.

## 새 권위 구조

```text
사용자 요청
→ Goal Contract 정규화·독립 검토
→ Project Map과 State Projection
→ Skeleton 1~3개 생성
→ 결정적 Gate·compact review·pruning
→ 최대 2개만 Plan Contract 후보로 상세화
→ 사용자 목표·범위·효과·운영 정책 승인(GoalAuthorization)
→ Core의 내부 Plan revision 자동 활성화
→ ready Task의 Execution Spec·Context Pack materialize
→ precondition·snapshot·effect checkpoint
→ Codex 실행·receipt·binding
→ Task validation과 State 재관측
→ plan-level Goal Test
→ Continue | Repair | Subgraph Replan | Goal Revision
```

사용자는 목표·대상·허용 효과·운영 정책을 승인한다. Core는 이 경계 안에서 immutable Plan revision을 자동 활성화하고 작업 분할·재계획·복구를 수행한다 (planned). 목표·범위·효과·정책 확장에만 추가 판단을 요청하며 사용자의 exact Plan ID·digest 입력은 필수가 아니다. 실제 파일·symbol·명령·Context는 ready 시점에 결정하고 효과 직전에 다시 검증한다.

## 핵심 안전성과 사용성 원칙

- 프로젝트 파일, 전역·프로젝트 `AGENTS.md`, 등록 참고자료, 빌드·테스트와 필요한 localhost는 정상 입력이다.
- 파일마다 AccessGrant를 만들거나 네트워크를 일괄 차단하지 않는다.
- 참고자료 안의 명령문은 분석할 데이터이며 사용자 지시나 활성 계약보다 높은 권위를 갖지 않는다.
- Reviewer는 finding과 rating만 제출한다. Core가 admission·score·상태를 결정한다.
- 실제 모델은 코드에 하드코딩하지 않고 호출 직전 `model/list`와 대조한다.
- 로컬 Codex task는 실제 `:danger-full-access`, `approval_policy=never`일 때만 시작한다.
- 외부 효과는 intent를 먼저 기록하고 receipt·thread·turn binding으로 결속한다.
- 모델의 “완료” 선언이 아니라 evidence와 validation으로 Task와 Goal을 완료한다.
- 같은 프로젝트는 먼저 직렬 실행한다. 병렬화와 VM·WSL·permission hardening은 별도 qualification 뒤에 연다.

## 기존 개발 CLI（schema 3）

아래는 재설계 전 명령 표면이다. 새 EngineApplication 명령과 패키지 전환은 planned이며 [현재 인계](docs/pre-1.0-handoff.md)의 후속 구현·검증을 거친다. 역사적 adjust-unknown은 새 usage 누락 해소 절차로 사용하지 않는다.

```text
flowmarshal-engine project init|show
flowmarshal-engine project source add|list
flowmarshal-engine project budget set|show|observe-role|adjust-unknown
flowmarshal-engine model status|rebind
flowmarshal-engine goal create|revise|show
flowmarshal-engine plan search|compare|activate|status
flowmarshal-engine task show|materialize
flowmarshal-engine run once|status
flowmarshal-engine attempt show|retry|observe|resume|interrupt
flowmarshal-engine validate task|goal|observe
flowmarshal-engine recover inspect|resume|abandon
flowmarshal-engine report progress|final
```

`goal create --live`, `plan search --live`, `run once` 전에 `project budget set --policy-file config/pre-1.0-validation-budget.json`으로 검증 예산을 등록한다. 이 파일은 Goal당 1,000,000 token, 호출당 100,000 token 예약, 재계획 reserve 25%의 시작값이다. 최적값이나 구독 한도 환산값이 아니다. 역할별 timeout은 전역 `--role-timeout-policy config/pre-1.0-role-timeouts.json`으로 결속한다.

`goal create --live`와 `plan search --live`는 역할별 model/effort를 호출자가 명시해야 한다. Engine은 이를 최신 App Server model inventory와 대조하고, 지원되지 않는 값을 임의 fallback으로 숨기지 않는다.

아래는 schema 3 개발 qualification CLI의 역사적 명령이다. 새 harness와 Engine-only 패키지 분리는 planned다. 기존 thread·원장을 먼저 관측하고 미확정 효과를 새 attempt로 우회하지 않는다. usage 누락만으로 새 실행을 차단하거나 아래 benchmark-report 옵션을 현재 1.0 필수 조건으로 사용하지 않는다.

```powershell
flowmarshal-engine-eval run --scope deterministic
flowmarshal-engine-eval run --scope role-fixture
flowmarshal-engine-eval run --scope full-planning-pipeline
flowmarshal-engine-eval run --scope project-e2e
flowmarshal-engine-eval resume --run-root <run-root>
flowmarshal-engine-eval benchmark --cells-file <36-cell.json> --scope-report <report> ...
flowmarshal-engine-eval cutover --scope-report <report> ... --benchmark-report <report>
```

고정 역할 설정과 상세 합격 기준은 [Engine qualification 실행 지침](docs/engine-qualification.md)을 따른다.

실제 모델 평가에는 검증할 Codex executable과 명시 source root·재현 입력을 결속한다. 과거 SDK/runtime 실패·성공 관측은 [승인 이전 실행 현황](docs/engine-implementation-status-before-redesign-1.0.md)의 해당 source 근거로만 읽는다. 실행 파일을 바꾸면 새 evaluation 계약과 run root를 사용한다.

기존 benchmark 수집기·S10~S12 계측·정산 감사는 과거 구현 근거다. 측정되지 않은 lifecycle 비율은 null/NOT_OBSERVED로 남긴다. R3.1 36-cell 비교는 별도 비차단 보고이며 새 1.0 필수 검증을 대신하지 않는다.

동결 검사는 이 저장소 외에 형제 디렉터리 `../자동화템플릿/prototypes/skills/flowmarshal-work-planner`의 원본 Planner 스킬 7개 파일도 요구한다. 새 clone에서 해당 감사 기준선이 없으면 freeze Gate는 실패하며 자동으로 생략하거나 재생성하지 않는다. 인증정보·로컬 실행 DB·Codex home 복제본·평가 작업 디렉터리는 Git에서 제외하고 기존 로컬 파일은 보존한다.

## source-tree 개발 설치와 검증

다음 editable 설치는 개발용이다. 승인된 source_root에서 실행하며 로컬 main을 작업 대상으로 묵시 선택하지 않는다. 1.0의 깨끗한 non-editable 설치 검증은 FM-10/12의 별도 필수 책임이다.

```powershell
cd <승인된-source_root>
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

새 Engine만 빠르게 검사하려면 다음을 사용한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_engine*.py' -v
.\.venv\Scripts\python.exe -m flowmarshal.engine.smoke --project-root <승인된-source_root>
.\.venv\Scripts\flowmarshal-engine.exe --help
```

기본 상태는 프로젝트 루트의 `.flowmarshal-engine/flowmarshal-engine.sqlite3`와 `.flowmarshal-engine/artifacts`에 저장한다. FM-01 시작 source의 schema revision은 3이며 새 schema 4와 schema 3 read-only reader는 planned다. SQLite application ID `0x464D4531`과 schema revision을 각각 검사하고 prototype/운영 DB의 자동 제자리 migration을 금지한다.

## 코드 지도

| 영역 | 파일 |
|---|---|
| 권위 schema와 불변조건 | `src/flowmarshal/engine/domain.py` |
| SQLite 원장·History | `src/flowmarshal/engine/ledger.py` |
| Goal 정규화·검토 | `src/flowmarshal/engine/goal.py` |
| Project Map·Context Pack | `src/flowmarshal/engine/context.py` |
| Skeleton-first search | `src/flowmarshal/engine/planning.py` |
| 실제 Planner 역할 adapter | `src/flowmarshal/engine/planner_roles.py` |
| 모델 inventory·배정 | `src/flowmarshal/engine/models.py` |
| 서비스·상태 전이 | `src/flowmarshal/engine/service.py` |
| Codex Runtime·dispatcher | `src/flowmarshal/engine/runtime.py` |
| 평가·cutover Gate | `src/flowmarshal/engine/evaluation.py` |
| qualification 실행기 | `src/flowmarshal/engine/qualification.py`, `e2e_qualification.py`, `eval_cli.py` |
| CLI와 보고 | `src/flowmarshal/engine/cli.py`, `reporting.py` |

전체 문서와 역사적 증거는 [문서 지도](docs/README.md)에서 찾을 수 있다.
