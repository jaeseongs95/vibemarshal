param(
    [string]$PayloadRoot = "P:\",
    [string]$InstallRoot = "C:\FlowMarshal"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "폐기 VM의 관리자 PowerShell에서 bootstrap을 실행해야 합니다."
}
$payload = (Resolve-Path -LiteralPath $PayloadRoot).Path
if (Test-Path -LiteralPath $InstallRoot) {
    throw "기존 설치 루트를 덮어쓰지 않습니다: $InstallRoot"
}
New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null

$pythonInstaller = Get-ChildItem -LiteralPath $payload -Filter "python-*-amd64.exe" -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$bundle = Get-ChildItem -LiteralPath $payload -Filter "flowmarshal-external-windows-vm-*.zip" -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$codexRuntime = Get-ChildItem -LiteralPath $payload -Filter "codex-runtime-*" -Directory |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$wheelhouse = Join-Path $payload "wheelhouse"
if (-not $pythonInstaller -or -not $bundle -or -not $codexRuntime -or -not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
    throw "payload에 Python, FlowMarshal 번들, 전체 Codex runtime 또는 wheelhouse가 없습니다."
}

$requiredCodexFiles = @(
    "bin\codex.exe",
    "bin\codex-code-mode-host.exe",
    "codex-resources\codex-windows-sandbox-setup.exe",
    "codex-resources\codex-command-runner.exe",
    "codex-path\rg.exe",
    "codex-package.json"
)
foreach ($relative in $requiredCodexFiles) {
    $required = Join-Path $codexRuntime.FullName $relative
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Codex runtime 필수 파일이 없습니다: $relative"
    }
    if ($required.EndsWith(".exe", [StringComparison]::OrdinalIgnoreCase) -and $relative -ne "codex-path\rg.exe") {
        $signature = Get-AuthenticodeSignature -LiteralPath $required
        if ($signature.Status -ne "Valid") {
            throw "Codex runtime 실행 파일 서명이 유효하지 않습니다: $relative ($($signature.Status))"
        }
    }
}

$checksumPath = "$($bundle.FullName).sha256"
if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    throw "FlowMarshal 번들 SHA-256 파일이 없습니다."
}
$expectedBundleHash = ((Get-Content -LiteralPath $checksumPath -Raw -Encoding ascii).Trim() -split "\s+")[0]
$actualBundleHash = (Get-FileHash -LiteralPath $bundle.FullName -Algorithm SHA256).Hash
if ($actualBundleHash -ne $expectedBundleHash) {
    throw "FlowMarshal 전달 ZIP의 SHA-256이 다릅니다."
}

$pythonRoot = Join-Path $InstallRoot "Python313"
$pythonInstall = Start-Process -FilePath $pythonInstaller.FullName -ArgumentList @(
    "/quiet", "InstallAllUsers=0", "TargetDir=$pythonRoot", "Include_launcher=0",
    "Include_test=0", "Include_doc=0", "Include_tcltk=0", "PrependPath=0"
) -Wait -PassThru
if ($pythonInstall.ExitCode -ne 0) {
    throw "Python 설치 실패(exit=$($pythonInstall.ExitCode))"
}
$python = Join-Path $pythonRoot "python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "설치된 Python 실행 파일을 찾을 수 없습니다."
}

$codexRoot = Join-Path $InstallRoot "CodexRuntime"
Copy-Item -LiteralPath $codexRuntime.FullName -Destination $codexRoot -Recurse
$systemCodex = Join-Path $codexRoot "bin\codex.exe"
$systemCodexHelper = Join-Path $codexRoot "bin\codex-code-mode-host.exe"
$sandboxSetupHelper = Join-Path $codexRoot "codex-resources\codex-windows-sandbox-setup.exe"
$commandRunner = Join-Path $codexRoot "codex-resources\codex-command-runner.exe"
$runtimeRipgrep = Join-Path $codexRoot "codex-path\rg.exe"
& $systemCodex --version | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "VM에 복사한 현재 Codex를 실행할 수 없습니다."
}
& $runtimeRipgrep --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "VM에 복사한 Codex ripgrep을 실행할 수 없습니다."
}

$bundleRoot = Join-Path $InstallRoot "Bundle"
Expand-Archive -LiteralPath $bundle.FullName -DestinationPath $bundleRoot
$initialize = Join-Path $bundleRoot "scripts\windows-vm\Initialize-FlowMarshalVm.ps1"
& $initialize -SystemCodexPath $systemCodex -PythonExecutable $python -Wheelhouse $wheelhouse | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "FlowMarshal VM 초기화에 실패했습니다."
}

$result = [ordered]@{
    status = "READY_FOR_CLEAN_SNAPSHOT"
    completed_at = (Get-Date).ToUniversalTime().ToString("o")
    install_root = $InstallRoot
    bundle_sha256 = $actualBundleHash.ToLowerInvariant()
    system_codex = (& $systemCodex --version | Out-String).Trim()
    python = (& $python --version | Out-String).Trim()
    codex_helper_copied = (Test-Path -LiteralPath $systemCodexHelper -PathType Leaf)
    sandbox_setup_helper_copied = (Test-Path -LiteralPath $sandboxSetupHelper -PathType Leaf)
    command_runner_copied = (Test-Path -LiteralPath $commandRunner -PathType Leaf)
    runtime_ripgrep_sha256 = (Get-FileHash -LiteralPath $runtimeRipgrep -Algorithm SHA256).Hash.ToLowerInvariant()
    authentication_copied = $false
    codex_home_copied = $false
    next_action = "VM을 종료하고 clean-base snapshot을 만든 뒤 세 시나리오를 각각 복원 실행하세요."
}
$resultPath = Join-Path $InstallRoot "bootstrap-result.json"
$result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultPath -Encoding utf8
$result | ConvertTo-Json -Depth 4
