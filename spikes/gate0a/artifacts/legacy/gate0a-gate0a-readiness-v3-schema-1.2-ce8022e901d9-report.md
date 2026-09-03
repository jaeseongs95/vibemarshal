# FlowMarshal Gate 0A 기술 스파이크 보고서

- 실행 ID: `gate0a-readiness-v3`
- 실행 시각: `2026-09-01T11:55:56.185374Z`
- 판정: **NO-GO**
- 사유: Gate 0A-R 또는 Gate 0A-P 필수 조건을 통과하지 못함
- Gate 0A-R Runtime: **GO** — 런타임 내구성과 Desktop 양방향 상호운용성 조건을 모두 통과함
- Gate 0A-P Permission: **NO-GO** — 승인 리소스와 보호 경계를 구분하는 권한 primitive를 입증하지 못함

## 결론

Gate 0A를 통과하지 못했다. 아래 실패를 해소하기 전에는 Gate 0B 또는 제품 기능 구현으로 넘어가지 않는다.

## 런타임: sdk-pinned

- 역할: openai-codex 패키지 동봉 런타임
- 실행 파일: `D:\codex\flowmarshal\.venv\Lib\site-packages\codex_cli_bin\bin\codex.exe`
- 버전: `codex-cli 0.147.0`
- 생성 task ID: `01a05cd3-7552-7690-8c9d-de7d19838222`
- 생성 task 이름: FlowMarshal Gate 0A sdk-pinned gate0a-r

| 검사 | 상태 | 요약 |
|---|---:|---|
| `approvals_fail_closed` | pass | 모든 알려진 서버 승인/입력 요청이 명시적 거절 응답으로 매핑됨 |
| `permission_configuration` | pass | permission profile 전용 설정이 확인됨 |
| `windows_elevated_sandbox` | pass | Windows elevated sandbox가 유효 설정으로 확인됨 |
| `windows_sandbox_operational` | pass | elevated Windows sandbox에서 command/exec 프로세스를 시작함 |
| `permission_profile_supported` | pass | permission profile이 런타임에 등록되고 허용됨 |
| `workspace_read_allowed` | pass | 작업 루트: 승인된 읽기가 허용됨 |
| `workspace_write_allowed` | pass | 워크스페이스 내부 쓰기 성공 |
| `declared_reference_read_allowed` | pass | 승인된 외부 참조: 승인된 읽기가 허용됨 |
| `declared_reference_write_blocked` | pass | 승인된 외부 참조: 쓰기가 차단됨 |
| `undeclared_path_read_blocked` | fail | 미승인 외부 경로: 읽기 경계가 깨짐 |
| `undeclared_path_write_blocked` | pass | 미승인 외부 경로: 쓰기가 차단됨 |
| `protected_path_read_blocked` | pass | FlowMarshal 보호 경로: 미승인 읽기가 차단됨 |
| `protected_path_write_blocked` | pass | FlowMarshal 보호 경로: 쓰기가 차단됨 |
| `control_file_read_allowed` | pass | 작업 계약 제어 파일: 승인된 읽기가 허용됨 |
| `control_file_write_blocked` | pass | 작업 계약 제어 파일: 읽기 전용 파일 수정이 차단됨 |
| `command_network_blocked` | pass | 명령 네트워크가 차단됨 |
| `initialize` | pass | initialize 요청과 initialized 알림 완료 |
| `thread_turn_separation` | pass | thread/start 직후 turn이 생성되지 않음 |
| `turn_execution` | pass | turn/start로 시작한 최소 turn이 완료됨 |
| `external_surfaces_disabled` | pass | Web Search/MCP/Plugin/Connector 및 Browser·Computer Use 진입 표면이 비활성화됨 |
| `persisted_thread_listed` | pass | 재시작 후 thread/list에서 저장 task를 찾음 |
| `resume_after_restart` | pass | App Server 재시작 후 thread/read와 thread/resume 성공 |
| `interrupt` | pass | turn/interrupt가 in-flight turn을 interrupted 상태로 종료함 |

## Codex Desktop 표시 확인

- 상태: `pass`
- 확인 방법: Codex Desktop list_threads
- 확인된 task ID: `01a05cd3-7552-7690-8c9d-de7d19838222`
- 누락 task ID: 없음

## Codex Desktop 양방향 상호운용성

- 상태: `pass`
- 확인 방법: Codex Desktop read_thread + list_threads
- Desktop 버전: `미기록`
- Desktop 생성 task ID: `01a05bdf-2894-7b20-8498-3ae44f299b79`

| 검사 | 상태 | 요약 |
|---|---:|---|
| `project_grouping_correct` | pass | Desktop가 대상 task를 예상 project와 cwd에 표시함 |
| `sdk_resume_visible_in_desktop` | pass | SDK가 resume 후 추가한 turn을 Desktop에서 확인함 |
| `desktop_turn_visible_to_sdk` | pass | Desktop에서 완료된 turn 증거를 SDK thread/read에서 확인함 |
| `desktop_thread_sdk_read_resume` | pass | Desktop task ID를 SDK가 thread/read와 thread/resume로 동일하게 복구함 |
| `concurrent_access_consistent` | pass | SDK 연결이 열린 동안 Desktop이 같은 task와 SDK turn을 일관되게 읽음 |

## 차단 항목

- `0A-P/sdk-pinned/undeclared_path_read_blocked`: fail — 미승인 외부 경로: 읽기 경계가 깨짐

## 증거 감사 기록

- `schema-1.1-evidence-loss-20260901`: acknowledged-loss — Gate 0A schema 1.1 결과가 schema 1.2로 변환될 때 원문 JSON과 보고서가 별도 보관되지 않았다. (`spikes/gate0a/artifacts/audit/schema-1.1-evidence-loss-20260901.json`)

## Desktop 상호운용성 시도 기록

- `fm0ar-live-20260901-01`: error / sdk-pinned `codex-cli 0.147.0` — InternalRpcError (`spikes/gate0a/artifacts/interop/fm0ar-live-20260901-01/attempt-result.json`)
- `fm0ar-live-20260901-02-system`: error / desktop-system `codex-cli 0.151.0` — ValidationError (`spikes/gate0a/artifacts/interop/fm0ar-live-20260901-02-system/attempt-result.json`)
- `fm0ar-live-20260901-03-compatible`: error / sdk-pinned `codex-cli 0.147.0` — InternalRpcError (`spikes/gate0a/artifacts/interop/fm0ar-live-20260901-03-compatible/attempt-result.json`)
- `fm0ar-live-20260901-04-raw-v2`: error / desktop-system `codex-cli 0.151.0` — InvalidRequestError (`spikes/gate0a/artifacts/interop/fm0ar-live-20260901-04-raw-v2/attempt-result.json`)
- `fm0ar-live-20260901-05-turn-binding`: pass / desktop-system `codex-cli 0.151.0` — 필수 검사 평가 완료 (`spikes/gate0a/artifacts/interop/fm0ar-live-20260901-05-turn-binding/attempt-result.json`)

## 권한 카나리 재검사 기록

- `fm0ap-live-20260901-01`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-01/attempt-result.json`)
- `fm0ap-live-20260901-02-allowlist`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-02-allowlist/attempt-result.json`)
- `fm0ap-live-20260901-03-separated-root`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-03-separated-root/attempt-result.json`)
- `fm0ap-live-20260901-04-no-minimal`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-04-no-minimal/attempt-result.json`)
- `fm0ap-live-20260901-05-drive-deny`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-05-drive-deny/attempt-result.json`)
- `fm0ap-live-20260901-06-system-0151`: fail / desktop-system `codex-cli 0.151.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-06-system-0151/attempt-result.json`)
- `fm0ap-live-20260901-07-system-official-profile`: fail / desktop-system `codex-cli 0.151.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-07-system-official-profile/attempt-result.json`)
- `fm0ap-live-20260901-08-final-official-profile`: fail / sdk-pinned `codex-cli 0.147.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-live-20260901-08-final-official-profile/attempt-result.json`)
- `fm0ap-research-20260901-09-codex-0152`: fail / desktop-system `codex-cli 0.152.0` (`spikes/gate0a/artifacts/permission-rechecks/fm0ap-research-20260901-09-codex-0152/attempt-result.json`)

## 범위와 주의사항

- 이 결과는 합성 카나리와 현재 설치된 로컬 런타임 조합에만 적용된다.
- 실제 사용자 파일의 내용은 카나리로 사용하지 않았다. 결과 JSON에는 카나리 원문 대신 SHA-256과 치환된 출력만 저장한다.
- 외부 경로라는 이유만으로 차단하지 않는다. 승인된 외부 참조는 읽기 가능해야 하고, 미승인·보호 경로는 읽기와 쓰기가 차단돼야 한다.
- permission profile이 미지원이면 넓은 읽기 권한으로 대체하지 않고 Gate 0A-P를 실패 처리한다.
- 현재 구현은 Gate 0A뿐이며 SQLite 원장, Planner, 스케줄러, 복구 엔진, 전체 CLI, Skill/Starter를 포함하지 않는다.

## 공식 계약

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Permissions](https://learn.chatgpt.com/docs/permissions)
- [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
