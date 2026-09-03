param(
    [string]$VmName = "FlowMarshal-Disposable-Win11",
    [Parameter(Mandatory = $true)][string]$IsoPath,
    [Parameter(Mandatory = $true)][string]$PayloadPath,
    [Parameter(Mandatory = $true)][string]$EvidencePath,
    [string]$VmRoot = "D:\VirtualMachines\FlowMarshal\vm",
    [string]$CredentialPath = "D:\VirtualMachines\FlowMarshal\secrets\guest-admin.credential.xml",
    [string]$PasswordFilePath = "D:\VirtualMachines\FlowMarshal\secrets\guest-password.tmp",
    [int]$MemoryMB = 8192,
    [int]$CpuCount = 4,
    [int]$DiskSizeMB = 102400,
    [string]$ExpectedIsoSha256 = "2CEE70BD183DF42B92A2E0DA08CC2BB7A2A9CE3A3841955A012C0F77AEB3CB29"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$vbox = "C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
if (-not (Test-Path -LiteralPath $vbox -PathType Leaf)) {
    throw "Oracle VirtualBox가 설치되지 않았습니다: $vbox"
}
$iso = (Resolve-Path -LiteralPath $IsoPath).Path
$payload = (Resolve-Path -LiteralPath $PayloadPath).Path
$evidence = (Resolve-Path -LiteralPath $EvidencePath).Path
$hash = (Get-FileHash -LiteralPath $iso -Algorithm SHA256).Hash
if ($hash -ne $ExpectedIsoSha256) {
    throw "Microsoft 공개값과 ISO SHA-256이 다릅니다: $hash"
}

$registered = & $vbox list vms
if ($registered -match ('^"' + [regex]::Escape($VmName) + '" ')) {
    throw "같은 이름의 VM을 덮어쓰지 않습니다: $VmName"
}
foreach ($path in @($VmRoot, (Split-Path -Parent $CredentialPath), (Split-Path -Parent $PasswordFilePath))) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}
if ((Test-Path -LiteralPath $CredentialPath) -or (Test-Path -LiteralPath $PasswordFilePath)) {
    throw "기존 VM 자격정보를 덮어쓰지 않습니다: $CredentialPath / $PasswordFilePath"
}

function Invoke-VBox {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $vbox @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "VBoxManage 실패(exit=$LASTEXITCODE): $($Arguments -join ' ')"
    }
}

$alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%_-"
$passwordCharacters = [System.Collections.Generic.List[char]]::new()
foreach ($character in "Fm1!".ToCharArray()) {
    $passwordCharacters.Add($character)
}
while ($passwordCharacters.Count -lt 28) {
    $index = [Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)
    $passwordCharacters.Add($alphabet[$index])
}
$password = -join $passwordCharacters
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
[pscredential]::new("flowmarshal", $securePassword) | Export-Clixml -LiteralPath $CredentialPath
Set-Content -LiteralPath $PasswordFilePath -Value $password -Encoding ascii -NoNewline

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
foreach ($secretPath in @($CredentialPath, $PasswordFilePath)) {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $identity.Name,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $secretPath -AclObject $acl
}
$password = $null
$securePassword = $null

$vmBase = Join-Path $VmRoot $VmName
$disk = Join-Path $vmBase "$VmName.vdi"
$snapshots = Join-Path $vmBase "Snapshots"

Invoke-VBox @("createvm", "--name=$VmName", "--platform-architecture=x86", "--basefolder=$VmRoot", "--ostype=Windows11_64", "--register")
New-Item -ItemType Directory -Path $snapshots -Force | Out-Null
Invoke-VBox @(
    "modifyvm", $VmName,
    "--memory=$MemoryMB", "--cpus=$CpuCount", "--vram=128",
    "--graphicscontroller=vboxsvga", "--firmware=efi", "--tpm-type=2.0",
    "--ioapic=on", "--x86-long-mode=on", "--paravirt-provider=hyperv",
    "--nested-hw-virt=off", "--clipboard-mode=disabled", "--drag-and-drop=disabled",
    "--audio-enabled=off", "--usb-ohci=off", "--usb-ehci=off", "--usb-xhci=off",
    "--nic1=nat", "--cable-connected1=on", "--boot1=dvd", "--boot2=disk",
    "--boot3=none", "--boot4=none", "--snapshot-folder=$snapshots"
)
Invoke-VBox @("modifynvram", $VmName, "inituefivarstore")
Invoke-VBox @("modifynvram", $VmName, "enrollmssignatures")
Invoke-VBox @("modifynvram", $VmName, "enrollorclpk")
Invoke-VBox @("modifynvram", $VmName, "secureboot", "--enable")
Invoke-VBox @("createmedium", "disk", "--filename=$disk", "--size=$DiskSizeMB", "--format=VDI", "--variant=Standard")
Invoke-VBox @("storagectl", $VmName, "--name=SATA", "--add=sata", "--controller=IntelAhci", "--portcount=4", "--bootable=on")
Invoke-VBox @("storageattach", $VmName, "--storagectl=SATA", "--port=0", "--device=0", "--type=hdd", "--medium=$disk")
Invoke-VBox @("sharedfolder", "add", $VmName, "--name=FlowMarshalPayload", "--hostpath=$payload", "--readonly", "--automount", "--auto-mount-point=P:")
Invoke-VBox @("sharedfolder", "add", $VmName, "--name=FlowMarshalEvidence", "--hostpath=$evidence", "--automount", "--auto-mount-point=E:")

[pscustomobject]@{
    status = "VM_REGISTERED"
    vm_name = $VmName
    vm_root = $vmBase
    disk = $disk
    iso = $iso
    iso_sha256 = $hash
    credential = $CredentialPath
    password_file = $PasswordFilePath
    payload_share = $payload
    evidence_share = $evidence
    next_action = "VBoxManage unattended install로 평가판 Windows를 설치하세요."
} | ConvertTo-Json -Depth 4
