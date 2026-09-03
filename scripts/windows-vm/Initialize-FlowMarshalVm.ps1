param(
    [Parameter(Mandatory = $true)]
    [string]$SystemCodexPath,

    [string]$PythonExecutable = "python.exe",

    [string]$Wheelhouse
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "FlowMarshalVm.Common.ps1")

$bundleRoot = Get-FlowMarshalBundleRoot
Assert-FlowMarshalBundleManifest -BundleRoot $bundleRoot
Assert-FlowMarshalAdministrator
$vmIdentity = Assert-FlowMarshalDisposableVm

$codex = (Resolve-Path -LiteralPath $SystemCodexPath).Path
if (-not (Test-Path -LiteralPath $codex -PathType Leaf)) {
    throw "현재 시스템 Codex 실행 파일이 없습니다: $SystemCodexPath"
}
$python = (Get-Command $PythonExecutable -ErrorAction Stop).Source

& $codex --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "현재 시스템 Codex 버전을 확인할 수 없습니다."
}
& $python --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Python을 실행할 수 없습니다."
}

$venv = Join-Path $bundleRoot ".venv"
if (Test-Path -LiteralPath $venv) {
    throw "초기화 전용 번들에 이미 .venv가 있습니다. 깨끗하게 다시 압축을 푸세요."
}
Invoke-FlowMarshalCheckedCommand -Executable $python -Arguments @("-m", "venv", $venv)
$venvPython = Join-Path $venv "Scripts\python.exe"
$pipArguments = @("-m", "pip", "install", "--disable-pip-version-check")
if ($Wheelhouse) {
    $resolvedWheelhouse = (Resolve-Path -LiteralPath $Wheelhouse).Path
    $pipArguments += @("--no-index", "--find-links", $resolvedWheelhouse)
}
$pipArguments += @("-r", (Join-Path $bundleRoot "requirements.lock"))
Invoke-FlowMarshalCheckedCommand -Executable $venvPython -Arguments $pipArguments
$editableArguments = @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps")
if ($Wheelhouse) {
    $editableArguments += @("--no-index", "--find-links", $resolvedWheelhouse)
}
$editableArguments += @("-e", $bundleRoot)
Invoke-FlowMarshalCheckedCommand -Executable $venvPython -Arguments $editableArguments

[pscustomobject]@{
    status = "READY_FOR_CLEAN_SNAPSHOT"
    bundle_root = $bundleRoot
    system_codex = $codex
    vm_identity = $vmIdentity
    next_action = "이 상태에서 깨끗한 base snapshot을 만든 뒤, 시나리오마다 base snapshot으로 되돌려 실행하세요."
} | ConvertTo-Json -Depth 5
