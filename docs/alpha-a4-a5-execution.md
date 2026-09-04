# 기능 Alpha A4 실행과 A5 중단·재개 기록

- 기준일: 2026-09-04 KST
- 시작 commit: `22e2efee8eb739f212321d48f000c989b714b4ea`
- 이전 준비 기록: [A4 인계](alpha-a4-handoff.md)
- 실행 위치: `.flowmarshal-engine-eval/runs/alpha-a4-session-high-20260904`

## 승인과 실행 범위

사용자가 이전 답변의 정확한 Plan 후보를 확인한 뒤 “진행해줘”라고 요청하여 해당 후보를 활성화했다. 문서 안의 명령문을 승인으로 대체하지 않았다.

- Plan: `plan_revision_a795bacd689740179af23c0becf414bc`
- digest: `sha256:162e20930e5ab6430a911827b33ffe8fa427966531834ba8f18a103c92fe4b26`
- activation: `activation_9b65fadd25e54901937117a072c5ff73`
- project: `project_e33f3f7e8b1b4aac800ae2299ddada3f`

원본 fixture의 별도 복사본에서 `app.py`의 뺄셈 결함을 덧셈으로 수정했다. Task는 수정, 기존 unittest·테스트 무수정 검사, 독립 의미 검토의 세 개다. 원본 fixture와 legacy/prototype은 변경하지 않았다. Core의 활성화·실행·관측·검증 API를 사용하고 원장을 직접 수정하지 않았다.

## 최초 source에서 확인한 실행

source `sha256:05129553563f7b03965602f3b9d853b706be101528341b0e4e766906ae9a308f`에서 `step-001`부터 `step-020`까지 수행했다.

| 범위 | 관측 |
|---|---|
| Task 실행 | 세 Task 모두 completed |
| Task 검증 | 구현, unittest, 원본 테스트 보존, 독립 semantic 검토의 네 validation PASS |
| State | 각 Task 완료 뒤 재관측 |
| 독립 Goal Test | 별도 준비 후 실제 명령 실행, exit 9009로 FAIL |
| GoalVerdict | 최초 실패 직후에는 기록하지 않음 |

이 source의 결정적 Gate는 `deterministic-execution-session/qualification-report.json`에 보존했다. **447 tests, OK**, compileall·pip check·synthetic lifecycle·legacy freeze 40개 모두 PASS이며 report digest는 `sha256:04f3d7d2ef6ee1bf280baa929e8a5ae54202948fa1c25aa083a430402946be7e`다.

## A5 저장 관측·중단·재개

Task `task_validate_unittest`의 실행 중 저장된 turn이 실제 active임을 `read_stored`로 확인하고 정확한 turn을 interrupt했다. 저장 관측에서도 interrupted를 확인한 뒤 App Server와 프로세스를 종료했다. 새 프로세스의 정상 `run once`는 기존 binding을 먼저 읽고 같은 Attempt·thread에서 새 turn을 시작했다.

- Attempt: `attempt_e997e571ef6c4435b3e0c4eb4be2bf33`
- thread: `01a069f8-7c19-7673-8a8a-59d9141ea5f5`
- 중단 turn: `01a069f8-7d12-7ea3-9266-f7817dbc01e0`
- 재개 turn: `01a069f8-ad3d-7e70-b7a8-bf2fb8157f70`
- 근거: `a5-fault-v2/`, `step-008/`, `step-009/`, `step-010/`

독립 receipt 감사에서 같은 Attempt·thread, 새 turn, 재개 이후 추가 `create_thread` 0회, `read → resume → start_turn` 순서를 확인했다. 최초·재개 Prompt의 canonical digest도 저장 artifact와 Core intent에 일치하며 Attempt는 최종 `succeeded`다. 직접 runtime receipt의 usage는 null이므로 A5 Worker 실행 토큰을 추정해 기입하지 않는다.

첫 harness는 `step-007`을 dispatch로 예상했지만 Core가 다른 ready Task를 먼저 materialize했다. Attempt가 없는 결과에서 harness가 예외를 낸 기록은 `a5-fault/`에 보존했다. 이후 Attempt 존재 여부를 확인하는 새 harness로 주입했다. 원래 runner와 lock, 기존 phase를 덮어쓰지 않았다. 이는 평가 harness 오류이며 Worker 실행 실패로 집계하지 않는다.

이 실측은 저장된 turn 중단과 프로세스·App Server 재시작이다. 물리적 PC 전원 차단이나 실제 사용량 제한 소진을 검증한 결과로 확대하지 않는다.

## 독립 Goal Test 실패와 복구 계약

준비 역할은 Windows `python` 별칭과 평가 복사본에 맞지 않는 `git diff HEAD`를 사용했다. 실제 명령은 `python` 호출에서 exit 9009, stderr `Python`으로 실패했다. Git 부분은 실행되지 않았으며, 코드 결함으로 단정하지 않는다.

최초 FAIL은 `validation_result_59196fc3c659499e8a7825469136ae6f`, 직접 command evidence는 `evidence_8a97cf86a43a451090bdbba319bf40c8`이다. 정상 dispatcher는 이 FAIL을 발견하면 수정된 명령을 받기 전에 실패 GoalVerdict를 기록하므로, 실패 뒤 운영 상세를 수정할 정식 경로가 없었다. 내부 helper를 직접 호출해 성공 결과를 추가하는 우회는 사용하지 않았다.

수정된 명령은 절대 Python 경로로 원본 unittest, 공개 `add(left: int, right: int) -> int` 계약, 양수·음수·0·큰 정수 합산과 원본 파일 digest를 확인한다. `validation_id`, deterministic method, `command/test/diff` 종류와 여섯 Goal 완료 조건은 유지한다. 테스트·정책 파일의 원본 SHA-256은 최초 lock에 대조한다. 실행 명령의 운영 상세 수정이며 Plan 의미를 변경하지 않는다.

기존 실행의 `session.py`와 `lock.json`은 보존한다. 복구는 별도 `goal_recovery_session.py`, `goal-recovery/lock.json`, 복구 전 원장 snapshot으로 source 전환을 기록한다. 이전 source의 Task·중단 재개 증거와 수정 source의 Goal 복구 결과를 단일 source의 qualification으로 합치지 않는다.

새 CLI 입력 `run once --goal-validation-retry-file`은 `step`, `failed_validation_result_id`, `failure_class`, `failure_evidence_id`, `rationale`을 받는다. 원인 `environment`는 제출자의 근거 있는 분류이며 Core가 exit code로 자동 추정한 사실은 아니다. Core는 실패·evidence·binding 연결, 현재 입력, 검사 종류와 종료 코드 기준, 재시도 횟수를 확인한다. 명령의 모든 의미가 같음을 자동 증명하는 기능은 아니므로 이 실행에서는 원래 Goal의 각 조건과 수정 명령을 직접 대조했다. 이번 요청은 `goal-test-retry-request.json`에 보존한다.

## 최종 검증과 남은 범위

수정 source는 `sha256:e2787c9c438d2273a4a717023b2385b8fa48a6524591869ec3e46bbe52b91e64`다. `deterministic-goal-recovery/qualification-report.json`에서 **455 tests, OK**와 compileall·pip check·synthetic lifecycle·legacy freeze 40개 PASS를 확인했다. report digest는 `sha256:657c9f2b993422460f14b1bc9c5400ed145634813cb1735b13a20d12d9c04a25`다. 추가 회귀는 현재·이전 형식 FAIL 복구, binding digest 보존, 검사 기준·artifact 범위 불변, terminal 판정 불변, 잘못된 증거·변경 없는 명세·stale 입력의 무변경 차단, 최대 두 번의 재시도를 다룬다.

표준 CLI 후속 결과는 다음과 같다.

| 복구 phase | Core 관측 |
|---|---|
| `step-021` | 실패 result·evidence와 새 재시도 binding 기록 |
| `step-022` | 실제 독립 명령 exit 0, validation PASS, 별도 command/test/diff evidence 4개 |
| `step-023` | GoalVerdict `satisfied`, Plan·project completed |

- 새 PASS: `validation_result_f2023e4c18e7495b9dccf5700a18271a`
- 최종 GoalVerdict: `goal_verdict_af094b4efa794bad8b9eeaa89550a97b`
- 최종 상태: 세 Task completed, 네 Attempt succeeded, History 111건·hash chain 유효
- 원장 대조: 복구 전 History 102건, validation 5건과 evidence 24건의 모든 열 값 보존
- 기능 검증 산출물: `execution-verification.json`, digest `sha256:4891b996634daf84a0625d025d3a16939d2210c9d9c18e4e0b51d7e7c8e5bb48`
- 최종 Core 보고서: `goal-recovery/status-final/outcome.json`

이 fixture에서 A4의 실행·독립 검증과 A5 저장 binding 재개를 확인했고, 명시적 환경 복구를 거쳐 승인된 Goal을 완료했다. A4의 최초 source에는 독립 Goal FAIL이 남으며, 수정 source에서 자연어 Goal부터 전체 경로를 다시 돌린 결과는 아니다. 전체 역할 회귀, 전체 planning, 실제 프로젝트 E2E 네 시나리오와 token/latency campaign도 같은 최종 source에서 모두 통과한 상태는 아니다. **1.0 cutover는 NO-GO**를 유지한다.

실제 DB·prompt·원시 receipt·사용자별 설정은 Git에서 제외하고, 이 보고서와 구현·회귀 테스트만 세션 커밋에 포함한다.
