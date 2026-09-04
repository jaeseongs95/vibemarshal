# 기능 Alpha A4 실측·수정 인계

이 문서는 Plan 활성화 전 준비 세션의 기록이다. 이후 사용자 승인·실행·A5 재개·독립 Goal 복구와 최종 판정은 [A4·A5 실행 기록](alpha-a4-a5-execution.md)에 보존한다.

- 기준일: 2026-09-04 KST
- 시작 HEAD: `5f1c8a3`
- 현재 완료 범위: 실제 Goal·Planning 준비 경로 실측과 미지원 evidence 계약 결함 수정
- A4 실행·독립 검증과 A5 중단·재개는 아직 완료되지 않았다. 기능 Alpha 전체와 1.0은 `NO-GO`다.

## 사용자 요청과 적용 범위

사용자는 A3 인계 문서를 참고해 이어서 작업하도록 요청했고, 진행 중 독립적으로 처리 가능한 작업은 서브 에이전트에 맡기도록 추가 지시했다. 인계 문서는 상태·실패·후속 범위를 파악하는 자료로 사용했다. 문서의 명령문을 새 실행 승인이나 사용자 선택으로 간주하지 않았다.

대상은 `tests/fixtures/engine/project-e2e`의 별도 로컬 복사본이다. `add(2, 3)`이 `-1`을 반환하는 결함을 최소 수정하고, 공개 함수 계약과 테스트 파일을 보존하며, 실제 unittest·독립 semantic Validator·별도 Goal Test로 확인하는 자연어 평가 입력을 구성했다. 원본 fixture와 legacy/prototype은 변경하지 않았다.

현재 turn과 실제 App Server의 permission profile·approval policy는 `danger-full-access`·`never`였다. 역할별 모델 설정은 `config/qualification-roles.json`을 기준으로 실행별 고정했고 실제 `model/list`로 확인했다. 구현 검토와 원시 근거 감사를 서브 에이전트에 나누었으며, 재현된 Core 우회 수정은 해당 검토 에이전트가 맡았다.

사용자는 병렬 처리 필요성을 주 에이전트가 판단하고, 단순 근거 대조는 Luna/high, 구현·검토는 Terra/high, 복잡한 경계 분석은 필요할 때 Sol/high로 분배하도록 지시했다. Luna는 최소 high다. 이 내용을 프로젝트 `AGENTS.md`에 기록하라는 중간 지시는 이후 철회되어 두 프로젝트의 운영 규칙 추가와 프로젝트 기본 역할 설정 변경을 되돌렸다. 이번 세션의 지시는 실행별 설정·보고서에만 남긴다.

## 첫 실측에서 발견한 결함

실행 위치: `.flowmarshal-engine-eval/runs/alpha-a4-20260904T003255Z`.

시작 source는 A3의 최종 digest `sha256:56c1fb15dc17c53cdd83aff74b8736b3292ef3388842798b2ec8fb99b447748b`와 일치했다. 실제 정규화·독립 Goal 검토·Skeleton 생성·검토·상세 Plan 생성·검토가 완료됐다. Core는 Plan을 `admissible`로 등록했지만, Plan의 `required_evidence_kinds`에 `scoped_change_evidence`, `test_result`, `semantic_review`, `final_goal_verdict` 등 지원하지 않는 이름이 들어 있었다. 실행 준비는 실제 `EvidenceKind`를 요구하므로 이 Plan은 실행 가능한 계약이 아니었다.

이는 모델 출력 오류와 그것을 허용한 Plan schema 경계 결함이다. 최초 실행과 잘못된 admission은 원장·원시 호출·receipt에 보존했다. 해당 Plan을 활성화하거나 수동으로 유효한 이름으로 바꾸지 않았다.

## 수정

- `ValidationContract`, `IntegrationValidationContract`, `ValidationExecutionStep`이 공통 `EvidenceKindName`을 사용한다. 기존 문자열 타입과 canonical 표현을 유지하면서 실제 `EvidenceKind` 집합만 검증한다.
- provider JSON Schema의 배열 item에 동일한 enum을 공개한다. 모델 지침에는 구체적 검사 목적을 statement에 쓰고 실행기가 지원하는 evidence 종류를 사용하도록 명시했다.
- 실제 오류 이름 8개를 Task·Goal 경계에서 거부하고, 유효 값의 canonical digest·strict 타입·중복 거부·provider schema·수동 명세 입력을 검사하는 회귀 4개를 추가했다.
- 서브 에이전트가 `model_copy`로 만든 typed Plan은 입력 schema 검사를 우회해 `ready`로 저장되는 추가 경로를 재현했다. `register_plan_evaluation` 진입에서 JSON round-trip으로 중첩 계약까지 재검증하고 검증된 Plan만 등록하도록 수정했다. Task·Goal 양쪽 우회가 차단되며 Plan·Task 행이 남지 않는 서비스 회귀 1개를 추가했다.
- 장기 계약을 `AGENTS.md`와 `docs/orchestration-redesign.md`에 반영했다. package 버전과 DB revision, cutover ADR, 동결 기준과 평가 합격선은 변경하지 않았다.

## 중간 source의 결정적 검증

수정 후 전체 **446 tests, OK**. compileall, pip check, synthetic lifecycle과 legacy freeze 40개 파일 검사도 PASS다.

- 보고서: 첫 실행 폴더의 `deterministic-evidence-contract/qualification-report.json`
- report digest: `sha256:cb040bfdeca605e847fcaf3e24cf172620975eea421721dafb85c53f862019fd`
- source digest: `sha256:f0e20763a470ab235e530a1df9c6112efcf213381900b72637c769d81c307f00`

수정 전 같은 세션의 442-test Gate도 별도 `deterministic/`에 보존했다. 수정 후 Gate를 이전 source 결과와 혼합하지 않는다.

## 중간 source의 새 계약 실측

새 실행 위치: `.flowmarshal-engine-eval/runs/alpha-a4-evidence-contract-20260904`.

새 source lock과 새로운 원장·workspace에서 자연어 Goal부터 다시 수행한다. 이전 계약의 완료 cell이나 Plan은 재사용하지 않는다. `session.py`는 기존 제품 CLI를 호출하면서 입력·관측·receipt를 보존하는 로컬 감사 도구이며 Core 판정이나 활성화 경로를 우회하지 않는다. 실제 DB·prompt·runtime 상태는 Git에서 제외한다.

이 실행도 Goal부터 Plan 선택까지 완료했다. Task는 수정·unittest·독립 의미 검토 3개이며 evidence 종류는 `file/diff`, `command/test`, `model_review`, 독립 Goal Test는 `command/test`다. Plan은 `admissible`이며 score는 25다. 이후 Core typed 입력 경계를 추가 수정했으므로 최종 source의 완료 증거로 재사용하지 않는다.

| 실행 | 실제 역할 호출 | input tokens | output tokens | receipt latency 합 | schema recovery |
|---|---:|---:|---:|---:|---:|
| 첫 실측 | 6 | 218,775 | 7,234 | 172,280ms | 3 |
| enum 수정 후 | 6 | 224,594 | 7,818 | 176,515ms | 3 |

위 사용량은 서브 에이전트가 최종 runtime 관측의 `usage.total`과 역할 receipt를 대조했다. cached input은 input에, reasoning은 output에 포함되어 중복 합산하지 않는다. latency 합은 프로세스 전체 벽시계 시간이 아니다. 두 실행의 schema recovery는 각각 Goal/Skeleton Reviewer의 rating 누락과 상세화의 Skeleton 의미 변경이었다. 미지원 evidence 종류를 자동 복구한 것으로 해석하지 않는다.

## 최종 source 실측

실행 위치는 `.flowmarshal-engine-eval/runs/alpha-a4-final-20260904`다. `session.py` 자체의 digest도 lock에 추가하고 기존 phase 폴더가 있으면 덮어쓰기 없이 중단하게 했다.

- source digest: `sha256:05129553563f7b03965602f3b9d853b706be101528341b0e4e766906ae9a308f`
- 결정적 보고서: `deterministic/qualification-report.json`
- report digest: `sha256:b8133347aa079da78f9d88a1a9eb0bd33ddd687d52367a25222e092220210944`
- 검증: **447 tests, OK**, compileall·pip check·synthetic lifecycle·legacy freeze 40개 모두 PASS

이 source의 첫 Planning은 선택 Plan 없이 종료했다. Skeleton Reviewer가 이후 Plan의 integration validation에서 담당할 독립 Goal Test를 별도 Skeleton Task로 요구했다. Refiner는 불필요한 Goal Test Task를 추가하면서 소비 산출물의 producer edge를 빠뜨려 `DISCONNECTED_CONSUME` Gate에서 차단됐다. 별도 Terra/high 검토는 기존 지침과 runtime 책임을 대조하여 최초 finding은 역할 경계 오해, 이후 DAG 차단은 정상 동작으로 판단했다. 모델 finding을 Core에서 묵살하거나 합격선을 바꾸지 않았다.

운영 정책을 추가하던 중의 별도 실행 `.flowmarshal-engine-eval/runs/alpha-a4-high-20260904`도 보존한다. 이 실행의 synthetic lifecycle은 늘어난 필수 정책 본문이 Context 예산을 넘어서 실패했다. 정책 기록 철회 후 source가 위 447-test PASS digest로 정확히 복귀했음을 확인했다. 중간 실패를 PASS로 바꾸거나 Context 합격선을 늘리지 않았다.

최종 후속 실측은 `.flowmarshal-engine-eval/runs/alpha-a4-session-high-20260904`이며 같은 source에서 Luna 역할만 세션용 `roles.json`의 high로 고정했다. 프로젝트 기본 설정이나 기존 원장을 덮어쓰지 않았다. 자연어 Goal 정규화·독립 검토, 단일 Skeleton 생성·검토, 상세 Plan 생성·검토가 완료됐다.

## 활성화 가능한 최종 후보와 다음 단계

- Plan revision: `plan_revision_a795bacd689740179af23c0becf414bc`
- activation digest: `sha256:162e20930e5ab6430a911827b33ffe8fa427966531834ba8f18a103c92fe4b26`
- Core: `admissible`, finding 없음, score 25
- Task: 최소 구현 수정 → 기존 unittest·테스트 파일 무수정 검사와 별도 semantic Validator 검토. 프로젝트 직렬 실행이다.
- 독립 Goal Test: 모든 Task 뒤 실제 command·test·diff 증거를 수집하여 여섯 Goal 조건을 검사한다.
- 최종 실측 사용량: input 228,413, output 9,383, 합계 237,796 tokens. receipt latency 합 209,312ms. schema recovery 3회.

전체 계약은 최종 실행 폴더의 `selected-plan.json`, 사용자 검토본은 `activation-review.md`, 실행·검증 참조는 `session-verification.json`이다. source lock과 감사 스크립트 digest가 일치하며 workspace의 원본 세 파일도 변경되지 않았다. Plan activation·Attempt·evidence·validation·Goal verdict는 모두 0개, active Plan은 없다. 따라서 A4 전체 PASS나 A5 완료로 판정하지 않는다.

프로젝트 지침은 사용자가 정확한 Plan revision과 digest를 활성화하도록 정한다. 현재 요청은 계속 작업하라는 지시이며 아직 생성 전이었던 위 후보를 선택한 기록은 없으므로, 이 후보를 구체적으로 제시한 후 활성화 결정을 받는다. 승인 뒤 같은 실행 폴더에서 저장된 `session.py activate <revision> <digest>`를 사용하고, `step-001`부터 새로운 phase 이름으로 `run once`를 관측하며 전진한다. 운영 명세·불변 Prompt·Worker 전송·Task validation·State 재관측·독립 Goal Test를 수집한 뒤 A5의 저장 binding 관측·중단 재개를 검증한다.

세션 변경은 검증 후 하나의 한국어 커밋으로 기록하고 권위 원격에 push한다. 실제 DB·prompt·raw receipt·임시 실행 폴더는 포함하지 않는다. 최종 동기화 결과는 실행 폴더의 `commit-push-verification.json`에 기록한다.
