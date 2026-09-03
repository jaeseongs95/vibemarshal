# Windows 샌드박스 폐기 VM 재검증 절차

이 절차는 실제 사용자 PC가 아니라 초기화하거나 폐기할 수 있는 **Windows VM**에서만
실행한다. `windowsSandbox/setupStart`는 Windows 로컬 사용자와 방화벽을 바꿀 수 있으므로
호스트에서 환경 변수나 인자만 흉내 내 실행하면 안 된다.

현재 사용자 PC의 Windows 기능을 변경하지 않는 **별도 VM** 경로를 선택한 경우에는
[별도 폐기 Windows VM 실행·증거 회수 절차](windows-vm-external-handoff.md)를 함께
따른다. 전달 번들, snapshot별 실행, 증거 반출·가져오기와 폐기 작업 ID 연결은 해당
문서와 `scripts/windows-vm`의 스크립트가 담당한다.

## 완료 조건

하나의 suite는 서로 깨끗하게 초기화된 세 VM 시나리오의 증거를 모은다.

1. `current-first`: 현재 시스템 Codex로 최초 설정
2. `cross-version`: 고정 구버전으로 설정한 같은 홈을 현재 시스템 Codex로 확인·갱신하고, 두 번째 홈 요청 차단
3. `failure-retry`: setup API 전 결정적 실패 1회 뒤 같은 홈에서 실제 setup을 1회만 재시도

각 시나리오 시작 때 두 샌드박스 계정, setup marker와 secrets가 없어야 한다. 비밀
내용은 읽지 않고 존재 여부와 메타데이터만 기록한다. 마지막에는 VM을 초기화하거나
폐기한 관리 계층 증거가 필요하다.

## 사전 준비

- 깨끗한 Windows VM snapshot
- VM 안에 현재 시스템 Codex와 FlowMarshal 개발 환경 설치
- `FLOWMARSHAL_SYSTEM_CODEX`에 현재 Codex 실행 파일의 절대경로 지정
- snapshot 초기화에도 유지되는 공유 증거 폴더 또는 시나리오별 증거 반출 절차
- VM의 authoritative home은 FlowMarshal 프로젝트·artifacts·임시 폴더 밖에 배치

VM 안에서 다음 공통 값을 설정한다.

```powershell
cd C:\FlowMarshal\flowmarshal
$env:FLOWMARSHAL_DISPOSABLE_WINDOWS_VM = "1"
$env:FLOWMARSHAL_SYSTEM_CODEX = "C:\Program Files\OpenAI\Codex\codex.exe"
$suiteId = "fm-sbx-vm-YYYYMMDD-01"
```

CLI는 두 명시적 확인값뿐 아니라 `Win32_ComputerSystem`의 제조사·모델도 검사한다.
Windows가 VM으로 인식하지 않으면 `HOST_PROVISIONING_FORBIDDEN`으로 중단한다.

## 1. 현재 버전 최초 설정

깨끗한 snapshot에서 실행한다.

```powershell
$vmHome = "C:\FlowMarshal-VM\current-first-home"
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system `
  --vm-suite-id $suiteId --vm-scenario current-first `
  --vm-attempt-id current-first-01 `
  --authoritative-codex-home $vmHome
```

실패하면 같은 시나리오·홈에서 새 Attempt ID로 한 번만 재시도할 수 있다. 성공 증거를
공유 위치로 반출한 뒤 VM을 깨끗한 snapshot으로 되돌린다.

## 2. 구버전에서 현재 버전으로 전환

새로 초기화된 snapshot에서 같은 시나리오 홈을 두 Attempt가 공유한다.

```powershell
$vmHome = "C:\FlowMarshal-VM\cross-version-home"
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime pinned `
  --vm-suite-id $suiteId --vm-scenario cross-version `
  --vm-attempt-id old-runtime-01 `
  --authoritative-codex-home $vmHome

.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system `
  --vm-suite-id $suiteId --vm-scenario cross-version `
  --vm-attempt-id current-runtime-01 `
  --authoritative-codex-home $vmHome
```

두 번째 홈 차단은 같은 시나리오에 다른 경로를 요청해 확인한다. 종료 코드 `2`가
예상 결과이며 런타임과 setup API가 시작되지 않았다는 guard 증거가 생성된다.

```powershell
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system `
  --vm-suite-id $suiteId --vm-scenario cross-version `
  --vm-attempt-id second-home-guard `
  --authoritative-codex-home "C:\FlowMarshal-VM\must-be-rejected"
```

증거를 반출한 뒤 VM을 다시 깨끗한 snapshot으로 되돌린다.

## 3. 제한된 실패 재시도

첫 명령은 setup API를 호출하기 전에 결정적 실패를 기록하며 종료 코드 `1`이 정상이다.
두 번째 명령만 실제 setup을 호출한다. 세 번째 Attempt는 시퀀스 검사에서 거절된다.

```powershell
$vmHome = "C:\FlowMarshal-VM\failure-retry-home"
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system --simulate-setup-failure `
  --vm-suite-id $suiteId --vm-scenario failure-retry `
  --vm-attempt-id injected-failure-01 `
  --authoritative-codex-home $vmHome

.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe setup-sandbox `
  --environment disposable-vm --runtime system `
  --vm-suite-id $suiteId --vm-scenario failure-retry `
  --vm-attempt-id retry-01 `
  --authoritative-codex-home $vmHome
```

## 4. 폐기 기록과 최종 판정

세 시나리오 증거를 호스트의
`spikes/gate0a/artifacts/vm-harness/suites/<SUITE_ID>`에 모은 뒤 검사 VM을 초기화하거나
폐기한다. VM 관리 계층의 이벤트·작업 ID를 `evidence-reference`에 기록한다.

```powershell
.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe record-vm-disposal `
  --vm-suite-id $suiteId --action disposed --provider Hyper-V `
  --vm-identifier flowmarshal-sandbox-test `
  --evidence-reference "Hyper-V Remove-VM operation/event id"

.\.venv\Scripts\python.exe -m flowmarshal_gate0a.probe verify-vm-suite `
  --vm-suite-id $suiteId
```

`verify-vm-suite`는 모든 증거가 유효할 때만 종료 코드 `0`, 상태 `GO`를 반환한다.
증거가 빠지면 종료 코드 `3`과 `PENDING`, 모순되거나 안전 위반이 있으면 종료 코드
`1`과 `NO-GO`다. 이 명령은 기존 Gate 판정 파일을 자동으로 바꾸지 않는다. suite가
`GO`인 뒤 호스트 권한 재검사와 Gate0B 실제 smoke 증거를 함께 검토해 Gate를 별도로
재판정한다.

주요 fail-closed 코드는 다음과 같다.

- `VM_SCENARIO_NOT_CLEAN`: 시나리오 시작 때 샌드박스 사용자·marker·secrets가 이미 존재함
- `VM_SUITE_SEQUENCE_INVALID`: 런타임 순서 또는 단계별 최대 1회 재시도 규칙 위반
- `VM_SUITE_INCOMPLETE`: 시나리오나 VM 폐기 증거가 빠짐
- `VM_SUITE_INVALID`: 증거 모순, 잘못된 구조 또는 안전 위반
- `VM_TEST_INJECTED_SETUP_FAILURE`: 제한된 재시도를 시험하기 위해 setup API 전에 만든 결정적 실패
