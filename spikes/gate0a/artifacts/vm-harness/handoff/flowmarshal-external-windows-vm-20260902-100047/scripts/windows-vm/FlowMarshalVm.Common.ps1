Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-FlowMarshalBundleRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Assert-FlowMarshalAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "폐기 VM 안의 관리자 PowerShell에서 실행해야 합니다."
    }
}

function Get-FlowMarshalVmIdentity {
    $computer = Get-CimInstance Win32_ComputerSystem
    $signature = ("{0} {1}" -f $computer.Manufacturer, $computer.Model).ToLowerInvariant()
    $markers = @(
        "virtual machine", "vmware", "virtualbox", "kvm", "qemu", "xen",
        "parallels", "google compute engine", "amazon ec2"
    )
    $recognized = $false
    foreach ($marker in $markers) {
        if ($signature.Contains($marker)) {
            $recognized = $true
            break
        }
    }
    return [pscustomobject]@{
        recognized_virtual_machine = $recognized
        manufacturer = $computer.Manufacturer
        model = $computer.Model
        hypervisor_present = $computer.HypervisorPresent
    }
}

function Assert-FlowMarshalDisposableVm {
    if ($env:OS -ne "Windows_NT") {
        throw "Windows VM에서만 실행할 수 있습니다."
    }
    $identity = Get-FlowMarshalVmIdentity
    if (-not $identity.recognized_virtual_machine) {
        throw "Windows가 가상 머신으로 인식하지 않아 중단했습니다: $($identity.manufacturer) / $($identity.model)"
    }
    return $identity
}

function Assert-FlowMarshalBundleManifest {
    param([Parameter(Mandatory = $true)][string]$BundleRoot)

    $manifestPath = Join-Path $BundleRoot "bundle-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "bundle-manifest.json이 없습니다."
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema_version -ne "1.0") {
        throw "지원하지 않는 VM 번들 manifest 버전입니다."
    }

    $expected = @{}
    foreach ($row in $manifest.files) {
        $relative = [string]$row.path
        if ($relative.Contains("..") -or [IO.Path]::IsPathRooted($relative)) {
            throw "잘못된 번들 상대경로입니다: $relative"
        }
        $path = Join-Path $BundleRoot ($relative.Replace("/", "\"))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "번들 파일이 없습니다: $relative"
        }
        $file = Get-Item -LiteralPath $path -Force
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "번들에 reparse point를 사용할 수 없습니다: $relative"
        }
        if ($file.Length -ne [long]$row.bytes) {
            throw "번들 파일 크기가 다릅니다: $relative"
        }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($hash -ne ([string]$row.sha256).ToLowerInvariant()) {
            throw "번들 파일 해시가 다릅니다: $relative"
        }
        $expected[$relative] = $true
    }

    foreach ($file in Get-ChildItem -LiteralPath $BundleRoot -Recurse -File -Force) {
        $relative = $file.FullName.Substring($BundleRoot.TrimEnd("\").Length).TrimStart("\").Replace("\", "/")
        if ($relative -eq "bundle-manifest.json") {
            continue
        }
        if (-not $expected.ContainsKey($relative)) {
            throw "manifest에 없는 파일이 번들에 있습니다: $relative"
        }
    }
}

function Invoke-FlowMarshalCheckedCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Executable @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "명령 실행 실패(exit=$LASTEXITCODE): $Executable $($Arguments -join ' ')"
    }
}
