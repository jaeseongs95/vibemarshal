# 별도 폐기 Windows VM 실행·증거 회수 절차

이 문서는 **현재 사용자 PC의 Windows Sandbox·Hyper-V 기능을 바꾸지 않고**, 별도로
마련한 Windows VM에서 FlowMarshal의 sandbox provisioning 검사를 수행하는 절차다.
VM 공급자 생성·삭제 작업은 관리 계층에서 수행하고, FlowMarshal은 VM 안의 검사와
허용된 JSON 증거의 무결성만 관리한다.

## 안전 경계

- 현재 PC에서는 `windowsSandbox/setupStart`, Windows 기능 활성화, VM 생성·삭제를 실행하지 않는다.
- VM에는 호스트의 `auth.json`, `.sandbox-secrets`, 세션 DB, 사용자 `.codex` 폴더를 복사하지 않는다.
- 전달 ZIP에는 실행에 필요한 소스·스크립트·문서만 허용 목록으로 담는다.
- VM에서 반출하는 파일은 `scenario.json`, `setup-result.json`, 두 번째 홈 차단 JSON과
  각 파일의 SHA-256 manifest뿐이다.
- 세 시나리오는 초기화된 **같은 base snapshot에서 각각 따로** 시작한다. 한 시나리오가
  끝나면 증거를 공유 위치로 반출하고 snapshot을 되돌린다.
- 마지막 판정은 VM을 실제로 초기화하거나 폐기한 관리 작업 ID가 있어야만 `GO`가 될 수 있다.

## VM 요구사항

- Windows 11 x64 Pro 또는 Enterprise VM
- VM 안에서 사용할 로컬 관리자 계정
- 현재 지원 버전의 시스템 Codex 실행 파일
- Python 3.10 이상과 인터넷 또는 사전에 준비한 Python 패키지 공급 경로
- 최소 3회의 base snapshot 복원이 가능한 VM 관리 기능
- snapshot 복원과 VM 폐기 뒤에도 유지되는 별도 증거 공유 폴더(SMB 공유, 별도 데이터 디스크 등)

Codex 인증이 필요하더라도 호스트의 인증 파일을 복사하지 않는다. VM 안에서 별도의
시험용 인증 절차를 수행하며, 인증 폴더는 증거 공유 위치에 두지 않는다.

## 1. 호스트에서 전달 번들 생성

```powershell
cd D:\codex\flowmarshal
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bundle = "spikes\gate0a\artifacts\vm-harness\handoff\flowmarshal-vm-$stamp.zip"
.\.venv\Scripts\python.exe -m flowmarshal.vm_handoff build-bundle `
  --project-root $PWD --output $bundle
.\.venv\Scripts\python.exe -m flowmarshal.vm_handoff verify-bundle `
  --archive $bundle
```

ZIP과 같은 이름의 `.sha256` 파일을 VM 전달 채널에 함께 복사한다. ZIP을 풀기 전에
전달 채널에서 SHA-256을 비교하고, 압축을 푼 뒤 초기화 스크립트가 개별 파일 manifest를
다시 검사한다.

## 2. VM base snapshot 준비

ZIP을 예를 들어 `C:\FlowMarshal-Bundle`에 푼다. 관리자 PowerShell에서 현재 시스템
Codex 경로를 명시해 초기화한다.

```powershell
cd C:\FlowMarshal-Bundle
.\scripts\windows-vm\Initialize-FlowMarshalVm.ps1 `
  -SystemCodexPath "C:\Program Files\OpenAI\Codex\codex.exe"
```

스크립트는 다음만 수행한다.

1. 번들 파일 집합과 SHA-256 검사
2. 관리자 권한과 Windows VM 제조사·모델 검사
3. 현재 Codex와 Python 실행 확인
4. 로컬 `.venv` 구성과 고정 구버전 런타임 설치

이 단계는 sandbox setup API를 호출하지 않는다. 결과가
`READY_FOR_CLEAN_SNAPSHOT`이면 VM을 종료하고 **base snapshot**을 만든다. 이
snapshot에는 `CodexSandboxOffline`, `CodexSandboxOnline` 계정과 scenario home의
marker·secrets가 없어야 한다.

## 3. 세 시나리오 실행

suite ID와 snapshot 밖 공유 증거 폴더를 정한다.

```powershell
$suiteId = "fm-sbx-vm-YYYYMMDD-01"
$codex = "C:\Program Files\OpenAI\Codex\codex.exe"
$evidence = "E:\FlowMarshal-Evidence"
```

매번 base snapshot으로 복원해 VM을 시작한 뒤 아래 명령 중 **하나만** 실행한다.

```powershell
.\scripts\windows-vm\Invoke-FlowMarshalVmScenario.ps1 `
  -Scenario current-first -SuiteId $suiteId `
  -SystemCodexPath $codex -EvidenceRoot $evidence
```

base snapshot으로 복원한 다음:

```powershell
.\scripts\windows-vm\Invoke-FlowMarshalVmScenario.ps1 `
  -Scenario cross-version -SuiteId $suiteId `
  -SystemCodexPath $codex -EvidenceRoot $evidence
```

다시 base snapshot으로 복원한 다음:

```powershell
.\scripts\windows-vm\Invoke-FlowMarshalVmScenario.ps1 `
  -Scenario failure-retry -SuiteId $suiteId `
  -SystemCodexPath $codex -EvidenceRoot $evidence
```

각 스크립트는 허용된 단계에서만 한 번 재시도하고, 성공 시 공유 폴더의
`<suite-id>\scenarios\<scenario>`에 JSON 증거와 manifest를 원자적으로 만든다.
기존 반출 폴더는 덮어쓰지 않는다.

## 4. VM 폐기 후 호스트 판정

세 시나리오 반출을 확인한 다음 VM을 관리 계층에서 초기화하거나 삭제한다. 공급자가
반환한 작업 ID·이벤트 ID를 보존한다. 그 후에만 호스트에서 다음을 실행한다.

```powershell
cd D:\codex\flowmarshal
.\scripts\windows-vm\Complete-FlowMarshalVmSuite.ps1 `
  -EvidenceRoot "E:\FlowMarshal-Evidence" `
  -SuiteId "fm-sbx-vm-YYYYMMDD-01" `
  -Action disposed `
  -Provider "<VM 공급자>" `
  -VmIdentifier "<VM 식별자>" `
  -EvidenceReference "<삭제 작업 또는 이벤트 ID>"
```

완료 스크립트는 반출 manifest를 다시 검사하고, 세 시나리오를 기존 증거와 섞지 않고
한 번만 가져온 뒤 폐기 사실을 기록하고 `verify-vm-suite`를 실행한다. 어떤 증거라도
누락·변조됐거나 폐기 작업 ID가 없으면 Gate 복구를 허용하지 않는다.

## 아직 외부 VM이 없을 때 필요한 입력

FlowMarshal이 실제 실행을 이어가려면 다음 중 하나가 필요하다.

- 이미 준비된 Windows VM의 접속 방법(SSH/WinRM 또는 연결된 Codex 호스트)
- 사용할 클라우드/사내 VM 공급자, 구독·프로젝트, 허용 예산과 폐기 정책
- VM 운영자가 위 번들을 실행한 뒤 반환한 증거 공유 폴더

비밀번호, API 키, `auth.json` 내용은 채팅이나 프로젝트 파일에 기록하지 않는다.
