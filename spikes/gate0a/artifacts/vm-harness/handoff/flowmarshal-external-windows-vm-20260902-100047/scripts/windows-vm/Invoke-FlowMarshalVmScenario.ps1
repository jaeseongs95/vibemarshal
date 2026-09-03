param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("current-first", "cross-version", "failure-retry")]
    [string]$Scenario,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9._-]+$")]
    [string]$SuiteId,

    [Parameter(Mandatory = $true)]
    [string]$SystemCodexPath,

    [Parameter(Mandatory = $true)]
    [string]$EvidenceRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "FlowMarshalVm.Common.ps1")

$bundleRoot = Get-FlowMarshalBundleRoot
Assert-FlowMarshalAdministrator
$null = Assert-FlowMarshalDisposableVm

$python = Join-Path $bundleRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "먼저 Initialize-FlowMarshalVm.ps1을 실행하고 base snapshot을 만드세요."
}
$codex = (Resolve-Path -LiteralPath $SystemCodexPath).Path
$evidence = [IO.Path]::GetFullPath($EvidenceRoot)
New-Item -ItemType Directory -Path $evidence -Force | Out-Null
if ($evidence.StartsWith($bundleRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "증거 공유 폴더는 snapshot으로 복원되는 번들 밖이어야 합니다."
}

$env:FLOWMARSHAL_DISPOSABLE_WINDOWS_VM = "1"
$env:FLOWMARSHAL_SYSTEM_CODEX = $codex
$vmHome = Join-Path "C:\FlowMarshal-VM" ("{0}-home" -f $Scenario)

function Invoke-SetupAttempt {
    param(
        [Parameter(Mandatory = $true)][string]$Runtime,
        [Parameter(Mandatory = $true)][string]$AttemptId,
        [switch]$InjectFailure
    )
    $arguments = @(
        "-m", "flowmarshal_gate0a.probe", "setup-sandbox",
        "--environment", "disposable-vm",
        "--runtime", $Runtime,
        "--vm-suite-id", $SuiteId,
        "--vm-scenario", $Scenario,
        "--vm-attempt-id", $AttemptId,
        "--authoritative-codex-home", $vmHome,
        "--project-root", $bundleRoot
    )
    if ($InjectFailure) {
        $arguments += "--simulate-setup-failure"
    }
    & $python @arguments | Out-Host
    return $LASTEXITCODE
}

function Invoke-SetupWithOneRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Runtime,
        [Parameter(Mandatory = $true)][string]$AttemptPrefix
    )
    $exitCode = Invoke-SetupAttempt -Runtime $Runtime -AttemptId ("{0}-01" -f $AttemptPrefix)
    if ($exitCode -eq 1) {
        $exitCode = Invoke-SetupAttempt -Runtime $Runtime -AttemptId ("{0}-02" -f $AttemptPrefix)
    }
    if ($exitCode -ne 0) {
        throw "setup 단계가 제한된 재시도 안에 성공하지 못했습니다: $AttemptPrefix (exit=$exitCode)"
    }
}

switch ($Scenario) {
    "current-first" {
        Invoke-SetupWithOneRetry -Runtime "system" -AttemptPrefix "current-first"
    }
    "cross-version" {
        Invoke-SetupWithOneRetry -Runtime "pinned" -AttemptPrefix "old-runtime"
        Invoke-SetupWithOneRetry -Runtime "system" -AttemptPrefix "current-runtime"

        $otherHome = "C:\FlowMarshal-VM\must-be-rejected"
        & $python -m flowmarshal_gate0a.probe setup-sandbox `
            --environment disposable-vm --runtime system `
            --vm-suite-id $SuiteId --vm-scenario $Scenario `
            --vm-attempt-id "second-home-guard" `
            --authoritative-codex-home $otherHome --project-root $bundleRoot | Out-Host
        if ($LASTEXITCODE -ne 2) {
            throw "두 번째 authoritative home 요청이 예상 종료 코드 2로 차단되지 않았습니다."
        }
    }
    "failure-retry" {
        $injected = Invoke-SetupAttempt -Runtime "system" -AttemptId "injected-failure-01" -InjectFailure
        if ($injected -ne 1) {
            throw "결정적 실패 주입의 예상 종료 코드는 1입니다: exit=$injected"
        }
        $retry = Invoke-SetupAttempt -Runtime "system" -AttemptId "retry-01"
        if ($retry -ne 0) {
            throw "실제 setup 재시도가 성공하지 못했습니다: exit=$retry"
        }
    }
}

& $python -m flowmarshal.vm_handoff export-scenario `
    --project-root $bundleRoot --evidence-root $evidence `
    --vm-suite-id $SuiteId --vm-scenario $Scenario | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "시나리오 증거 반출에 실패했습니다."
}

[pscustomobject]@{
    status = "SCENARIO_EXPORTED"
    suite_id = $SuiteId
    scenario = $Scenario
    evidence_root = $evidence
    next_action = "VM을 종료하고 base snapshot으로 되돌린 뒤 다음 시나리오를 실행하세요."
} | ConvertTo-Json -Depth 4
