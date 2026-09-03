param(
    [Parameter(Mandatory = $true)][string]$EvidenceRoot,
    [Parameter(Mandatory = $true)][ValidatePattern("^[A-Za-z0-9._-]+$")][string]$SuiteId,
    [Parameter(Mandatory = $true)][ValidateSet("reset", "disposed")][string]$Action,
    [Parameter(Mandatory = $true)][string]$Provider,
    [Parameter(Mandatory = $true)][string]$VmIdentifier,
    [Parameter(Mandatory = $true)][string]$EvidenceReference,
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "FlowMarshal 프로젝트 가상환경을 찾을 수 없습니다: $python"
}

& $python -m flowmarshal.vm_handoff import-suite `
    --project-root $root --evidence-root $EvidenceRoot --vm-suite-id $SuiteId | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "외부 VM 증거 가져오기에 실패했습니다."
}

& $python -m flowmarshal_gate0a.probe record-vm-disposal `
    --vm-suite-id $SuiteId --action $Action --provider $Provider `
    --vm-identifier $VmIdentifier --evidence-reference $EvidenceReference `
    --project-root $root | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "VM 초기화·폐기 증거 기록에 실패했습니다."
}

& $python -m flowmarshal_gate0a.probe verify-vm-suite `
    --vm-suite-id $SuiteId --project-root $root | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "VM suite가 GO 조건을 충족하지 못했습니다."
}
