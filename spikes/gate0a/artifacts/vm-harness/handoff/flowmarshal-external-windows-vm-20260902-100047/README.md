# FlowMarshal

FlowMarshal은 사용자의 큰 기능 요청을 검증 가능한 task로 나누고, task마다 알맞은 Codex 모델과 추론 수준을 배정한 뒤 안전하게 위임·검증·재시도·복구하는 재사용 가능한 로컬 Workflow Control Plane을 목표로 한다. 현재 저장소에는 제품 전체가 아니라 **Gate 0A와 Gate 0B 기술 프로토타입**이 구현되어 있다.

## 현재 상태

- `Gate 0A-R = GO`: 기존 Codex SDK/App Server/Desktop 상호운용성 증거는 보존
- `Gate 0A-P = NO-GO`, 전체 `Gate 0A = NO-GO`: 임시 `CODEX_HOME` provisioning 사고로 기존 권한 증거 무효화
- `Gate 0B Core = GO`, 실제 sandbox command smoke = `GO`, 전체 `Gate 0B = BLOCKED/NO-GO`: 폐기 Windows VM 재검증이 남아 Gate 0A 선행조건을 아직 복구하지 못함

Gate 0B는 완성 제품이나 공개 CLI가 아니다. 자동 Planner, 기능의 task 분해, 모델 역할 자동 배정, scheduler, 자동 재시도 정책, 병렬 실행, Starter Template은 뒤 단계로 남겨 두었다.

## Gate 0B 구현 범위

- SQLite WAL 기반 영속 작업 장부와 append-only hash history
- `PlanRevision`, `WorkItem`, dependency, `Attempt`, 실행 slot과 쓰기 lease
- `RuntimeActionIntent`를 이용한 thread 생성·turn 시작·검사·복구 효과 분리
- 승인 capability와 HMAC proof를 이용한 계획 승인·활성화·접근 승인·사람 검토·복구
- 프로젝트 밖 읽기 자료의 명시적 등록, 읽기 전용 `AccessGrant`, 입력 snapshot과 drift 무효화
- 내용 주소 기반 evidence와 독립 verifier의 10개 불변식 검사
- 재시작 시 실행 중 효과를 `unknown`으로 격리하고 관측·명시적 binding·중단·보류로 복구
- 논리 모델 역할과 실제 모델 ID·추론 수준을 PlanRevision과 Attempt에 기록
- 실제 Codex 합성 실행과 deterministic fake runtime을 이용한 crash window·중복 효과 검사

설계와 내부 CLI는 [Gate 0B 설계](docs/gate0b-design.md), 판정은 [Gate 0B 보고서](spikes/gate0b/artifacts/gate0b-report.md)에 정리되어 있다.

## Gate 0A 검증 범위

- Python SDK가 고정된 Codex 런타임과 Desktop 동봉 런타임을 구동할 수 있는가
- `thread/start`와 `turn/start`를 분리하고, 저장된 task를 list/read/resume할 수 있는가
- App Server를 재시작한 뒤 동일 task를 복구할 수 있는가
- turn을 interrupt할 수 있는가
- 실제 사용자·시스템 Codex가 공유하는 authoritative home에서 구형 `sandbox_mode` 없이 permission profile만 유효하게 선택됐는가
- 시스템 Codex의 elevated Windows sandbox가 실제 command 실행 준비 상태이며 검사 전후 계정·marker·secret 메타데이터가 변하지 않는가
- Windows elevated sandbox가 작업 루트 쓰기, 승인된 외부 참조 읽기, 외부 쓰기 차단과 보호 경로 차단을 서로 구분하는가
- 미등록 외부 읽기가 엄격 차단되는지, 아니면 승인된 native Windows 임시 호환성 예외가 사용되는지 구분해 기록하는가
- 작업 계약 제어 파일을 읽을 수 있지만 수정할 수 없는가
- sandboxed command의 네트워크가 차단되는가
- Web Search, MCP, Plugin, Connector, Browser/Computer Use 표면을 비활성화할 수 있는가
- 모든 서버 승인 요청을 명시적으로 거절하는 fail-closed 처리기가 동작하는가
- SDK가 만든 task가 Codex Desktop에 표시되는가
- SDK resume 결과가 Desktop에 보이고, Desktop에서 만든 task와 turn을 SDK가 read/resume할 수 있는가
- 동일 task의 project grouping과 SDK/Desktop 연속·중첩 접근이 일관적인가

Gate 0A는 다음 두 하위 게이트로 판정한다.

- `0A-R Runtime`: SDK/App Server/Desktop 상호운용성
- `0A-P Permission`: 역할·승인 리소스 기반 파일 권한 primitive

현재 판정은 `0A-R = GO`, `0A-P = NO-GO`, 전체 `Gate 0A = NO-GO`다. 과거 0A-P는 임시 `CODEX_HOME`에서 elevated provisioning을 실행해 실제 PC의 컴퓨터 전역 샌드박스 사용자 상태를 바꿨으므로 증거가 무효화됐다. 원인과 preimage 해시는 [사고 감사 기록](spikes/gate0a/artifacts/audit/sandbox-home-cross-contamination-20260902.md)에 있다.

## Native Windows 외부 읽기 임시 예외

최종 목표는 등록된 외부 참조만 읽고 미등록 경로는 차단하는 것이다. 그러나 고정 Codex 0.147.0과 최신 교차검증 Codex 0.152.0 모두 permission profile의 `:root=deny`가 미등록 외부 파일의 상속 읽기 권한을 차단하지 못했다.

이 런타임 결함 때문에 현재는 미등록 외부 읽기만 조건부 임시 예외로 인정한다. 외부 로그·요구사항·참고 자료는 여전히 입력 리소스로 등록하고 출처와 digest를 기록한다. 프로젝트 밖 쓰기, 보호 경로 읽기·쓰기, 제어 파일 쓰기, 명령 네트워크와 외부 도구 표면은 예외 없이 차단해야 한다.

`:root=deny` 설정과 미등록 읽기 카나리는 제거하지 않았다. 지원 런타임이 바뀌거나 카나리가 `strict_read_isolation`으로 전환되면 재검토하고, 정책 개정을 거쳐 임시 예외를 제거한다. 상세 근거와 증거 해시는 [감사 기록](spikes/gate0a/artifacts/audit/native-windows-read-exception-20260902.md)에 있다.

## 실행

PowerShell에서:

```powershell
cd D:\codex\flowmarshal
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe sandbox-status
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe run --runtime system
.\.venv\Scripts\python.exe -m flowmarshal.gate0b.cli smoke-fake `
  --root spikes\gate0b\artifacts\smoke
```

Gate 0B 내부 CLI 도움말:

```powershell
.\.venv\Scripts\python.exe -m flowmarshal.gate0b.cli --help
```

실제 Codex smoke는 준비상태를 먼저 확인한 뒤 계정에 합성 task를 하나 만들고, sandbox command로 marker를 읽은 증거까지 검사한다. 호스트 재검증 smoke는 통과했으며 현재 Gate 0B 판정은 폐기 Windows VM 선행조건 때문에 `BLOCKED/NO-GO`다.

Gate 0A는 서로 다른 두 App Server 세션을 사용한다.

- `0A-P` 세션: 현재 시스템 Codex와 실제 authoritative home을 사용한다. App Server만 홈을 신뢰 계층으로 사용하고, permission profile의 command에는 홈 전체를 명시적으로 `deny`한다.
- `0A-R` 세션: 같은 authoritative home의 인증·Desktop 상태를 사용하되 permission profile을 섞지 않고 `read-only` thread sandbox로 실행한다. 현재 구성된 모든 MCP 서버는 이름을 읽은 다음 새 App Server 프로세스 시작 인자에서 명시적으로 비활성화한다.

준비상태 조회는 canary·task·command보다 먼저 실행한다. `READY`가 아니면 어떤 실행도 시작하지 않고 `SETUP_REQUIRED` 또는 `ERROR`를 구조화해 반환한다. 실제 PC에서는 현재 시스템 Codex만 사용하며 고정·구버전 교차검사는 VM으로 제한한다.

`setup-sandbox`는 실제 PC에서 설정을 실행하지 않고 종료 코드 `3`과 수동 복구 명령만 출력한다.

```powershell
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox
```

출력된 `codex sandbox setup --elevated --user ... --codex-home ...` 명령은 사용자가 실제 홈을 확인한 뒤 관리자 PowerShell에서 직접 실행한다. 폐기 가능한 VM에서는 `--environment disposable-vm`과 `FLOWMARSHAL_DISPOSABLE_WINDOWS_VM=1`을 함께 지정해야 하며, Windows가 실제 VM으로 인식되는지도 확인한 뒤에만 setup API가 활성화된다.

폐기 VM suite는 깨끗한 snapshot을 각각 사용하는 `current-first`, `cross-version`, `failure-retry` 세 시나리오로 구성한다. 다음은 구버전→현재 버전 시나리오의 예다.

```powershell
$env:FLOWMARSHAL_DISPOSABLE_WINDOWS_VM = "1"
$suiteId = "fm-sbx-vm-YYYYMMDD-01"
$vmHome = "C:\FlowMarshal-VM\codex-home"
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime pinned `
  --vm-suite-id $suiteId --vm-scenario cross-version `
  --authoritative-codex-home $vmHome --vm-attempt-id old-runtime-setup
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system `
  --vm-suite-id $suiteId --vm-scenario cross-version `
  --authoritative-codex-home $vmHome --vm-attempt-id current-runtime-upgrade
```

시나리오별 재시도는 단계마다 1회로 제한되며 다른 홈을 지정하면 런타임 시작 전에 `HOST_PROVISIONING_FORBIDDEN`으로 중단하고 guard 증거를 남긴다. 전체 명령, snapshot 초기화 순서, 실패 주입, VM 폐기 기록과 `verify-vm-suite` 판정 방법은 [폐기 VM 재검증 절차](docs/windows-sandbox-vm-revalidation.md)에 있다.

현재 호스트에는 실행 가능한 Windows VM 도구나 기존 VM이 없어 실제 suite를 실행하지 않았다. [가용성 점검](spikes/gate0a/artifacts/vm-harness/host-capability-20260902.md)에 따라 기능 활성화·hypervisor 설치·재부팅 없이 Gate를 `NO-GO`로 유지한다. 실행 경로는 이 PC의 Windows Sandbox/Hyper-V 활성화가 아닌 **별도 폐기 Windows VM**으로 확정했다. 허용 목록 기반 전달 ZIP, snapshot별 시나리오 실행, JSON 증거 반출·무결성 검사와 VM 폐기 작업 ID 연결 방법은 [별도 VM 전달 절차](docs/windows-vm-external-handoff.md)에 있다.

그 후 Gate 0A를 다시 실행한다. preflight를 통과한 실행은 합성 카나리만 사용하지만 다음 상태 변경이 발생한다.

- Codex 계정에 런타임별 `FlowMarshal Gate 0A ...` task가 생성된다.
- `D:\codex\flowmarshal\spikes\gate0a\artifacts`에 결과와 실행 증거가 생성된다.
- 승인된 참조는 `D:\codex\flowmarshal-gate0a-canary` 아래에, 미등록 외부 읽기 검사는 `D:\FlowMarshal-Gate0A-Canary` 아래에 합성 카나리를 만든다.
- 보호 경계를 검증하기 위해 `%LOCALAPPDATA%\FlowMarshal\Gate0A` 아래에 합성 카나리를 만든다. 차단이 깨진 경우 의도한 테스트 파일도 남을 수 있다.

결과 파일:

- `spikes/gate0a/artifacts/gate0a-results.json`: 기계 판독 결과
- `spikes/gate0a/artifacts/gate0a-report.md`: 한국어 판정 보고서
- `spikes/gate0a/artifacts/legacy/`: 이전 schema 결과를 변환 전에 보존하는 원본과 migration receipt
- `spikes/gate0a/artifacts/audit/`: 예외 결정과 증거 손실·보완 사항의 감사 기록
- `spikes/gate0a/artifacts/permission-codex-home/`: 재사용이 금지된 과거 사고 증거. 로그·marker만 보존하며 실행 홈으로 사용하지 않음
- `spikes/gate0b/artifacts/gate0b-results.json`: Gate 0B 기계 판독 결과
- `spikes/gate0b/artifacts/gate0b-report.md`: Gate 0B 한국어 판정 보고서
- `spikes/gate0b/artifacts/smoke/`: fake/실제 Codex 실행별 원장·evidence·독립 검증 결과

Desktop 목록 확인 후 결과를 확정하려면:

```powershell
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe record-desktop `
  --visible <THREAD_ID> `
  --checked-via "Codex Desktop list_threads"
```

누락된 ID는 `--missing <THREAD_ID>`를 반복해서 전달한다.

`record-desktop`은 sidebar 가시성만 기록하며 Gate 0A-R 전체를 GO로 만들지 않는다. 구현된 `verify-desktop-interop` verifier가 다음 필수 검사를 SDK challenge와 Desktop 관측 묶음으로 검증한다.

- `project_grouping_correct`
- `sdk_resume_visible_in_desktop`
- `desktop_turn_visible_to_sdk`
- `desktop_thread_sdk_read_resume`
- `concurrent_access_consistent`

현재 주 결과에는 이 다섯 검사의 통과 증거가 병합되어 `0A-R = GO`로 판정됐다. 새로운 환경에서 다시 검증할 때는 `verify-desktop-interop --help`로 필요한 Desktop task·project·cwd 인자를 확인한다.

## 공식 계약 기준

- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Permissions](https://learn.chatgpt.com/docs/permissions)
- [Windows sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
