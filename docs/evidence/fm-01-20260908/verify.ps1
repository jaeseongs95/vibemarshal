$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))
if ($taskRoot.TrimEnd('\', '/') -ne 'D:\codex\flowmarshal') { throw 'Unexpected checkout' }
if ((Get-Location).Path.TrimEnd('\','/') -ne $taskRoot.TrimEnd('\','/')) { throw 'Run from main checkout root' }
$taskBranch = (git -C $taskRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $taskBranch -ne 'main') { throw 'main checkout required' }
$taskHead = (git -C $taskRoot rev-parse HEAD).Trim()
$taskBase = 'c484243cfa871b04b9858a71f74c4736ac505a00'
$taskUtf8 = [Text.UTF8Encoding]::new($false)
function Save-TaskText($name, $value) {
    $taskText = (($value -join "`n") + "`n").Replace("`r`n", "`n")
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot $name), $taskText, $taskUtf8)
}
function Get-TaskHash($path) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() }

$taskNames = @('AGENTS.md','docs/orchestration-redesign.md','docs/engine-cutover-adr.md','docs/pre-1.0-roadmap.md','docs/pre-1.0-handoff.md','docs/r31-frozen-baseline.md')
$taskInputs = @('D:/codex/fm-inspection-runtime/performance-release-floor-20260907/redesign-1.0/approved-plan.md','D:/codex/자동화템플릿/AGENTS.md')
$taskInputs += $taskNames | ForEach-Object { 'D:/codex/fm-performance-floor/' + $_ }
$taskInputHashes = @($taskInputs | ForEach-Object { [ordered]@{path=$_;sha256=(Get-TaskHash $_)} })
Save-TaskText 'inputs.json' (ConvertTo-Json -Depth 8 -InputObject $taskInputHashes)

$taskDocs = @('AGENTS.md','docs/README.md','docs/orchestration-redesign.md','docs/engine-cutover-adr.md','docs/pre-1.0-roadmap.md','docs/pre-1.0-handoff.md','docs/redesign-1.0-contract.md')
$taskDocHashes = @($taskDocs | ForEach-Object {
    $taskBlob = (git -C $taskRoot rev-parse "${taskBase}:$_").Trim()
    [ordered]@{path=$_;base_git_blob=$taskBlob;sha256=(Get-TaskHash (Join-Path $taskRoot $_))}
})
Save-TaskText 'documents.json' (ConvertTo-Json -Depth 8 -InputObject $taskDocHashes)

$taskPatterns = @('usage.*차단|사용량.*차단|BLOCKED_USAGE_UNKNOWN|usage_unknown','exact.*승인|정확한.*승인|수동.*승인','비교.*필수|성능.*필수|performance36|performance 36|cutover_eligible','detached|fm-performance-floor|main 통합|push')
$taskRgArgs = @('-n','-C','1','--no-heading','--color','never')
foreach ($taskPattern in $taskPatterns) { $taskRgArgs += @('-e',$taskPattern) }
$taskScan = & rg @taskRgArgs 'README.md' 'AGENTS.md' 'docs' '-g' '*.md' '-g' '!docs/evidence/**'
$taskScanExit = $LASTEXITCODE
if ($taskScanExit -gt 1) { throw 'Repository rg scan failed' }
Save-TaskText 'contract-search.txt' $taskScan
$taskExternalScan = & rg @taskRgArgs 'D:/codex/fm-performance-floor/AGENTS.md' 'D:/codex/자동화템플릿/AGENTS.md'
if ($LASTEXITCODE -gt 1) { throw 'Instruction rg scan failed' }
Save-TaskText 'instruction-search.txt' $taskExternalScan
$taskAnchors = & rg '-n' '^#{1,3} (D[0-9]{2}|V[0-9]{2}|M01)|^\| [0-9]+ \|' 'docs/redesign-1.0-contract.md'
if ($LASTEXITCODE -ne 0) { throw 'Contract anchor scan failed' }
Save-TaskText 'requirement-locations.txt' $taskAnchors

$taskFreeze = Get-Content -LiteralPath (Join-Path $taskRoot 'config/legacy-freeze-manifest.json') -Raw | ConvertFrom-Json
$taskFreezeRows = @($taskFreeze.entries | ForEach-Object {
    $taskPath = [IO.Path]::GetFullPath((Join-Path $taskRoot $_.path))
    $taskActual = if (Test-Path -LiteralPath $taskPath -PathType Leaf) { 'sha256:' + (Get-TaskHash $taskPath) } else { $null }
    [ordered]@{path=$_.path;expected=$_.sha256;actual=$taskActual;match=($taskActual -eq $_.sha256)}
})
$taskDiscovered = @()
$taskMissingRoots = @()
foreach ($taskFreezeRoot in $taskFreeze.roots) {
    $taskDirectory = [IO.Path]::GetFullPath((Join-Path $taskRoot $taskFreezeRoot.path))
    if (-not (Test-Path -LiteralPath $taskDirectory -PathType Container)) { $taskMissingRoots += $taskFreezeRoot.path; continue }
    $taskFiles = if ($taskFreezeRoot.recursive -eq $false) { Get-ChildItem -LiteralPath $taskDirectory -File -Force } else { Get-ChildItem -LiteralPath $taskDirectory -File -Force -Recurse }
    foreach ($taskFile in $taskFiles) {
        if ($taskFile.FullName -match '[\\/]__pycache__[\\/]' -or $taskFile.Extension -in @('.pyc','.pyo')) { continue }
        $taskRelative = $taskFile.FullName.Substring($taskDirectory.TrimEnd('\','/').Length + 1).Replace('\','/')
        $taskDiscovered += $taskFreezeRoot.path.TrimEnd('/') + '/' + $taskRelative
    }
}
$taskUnexpected = @($taskDiscovered | Where-Object { $_ -notin $taskFreeze.entries.path })
$taskFreezeFailures = @($taskFreezeRows | Where-Object { -not $_.match })
Save-TaskText 'freeze-verification.json' (ConvertTo-Json -Depth 8 -InputObject ([ordered]@{manifest_sha256=(Get-TaskHash (Join-Path $taskRoot 'config/legacy-freeze-manifest.json'));entries=$taskFreezeRows;missing_roots=$taskMissingRoots;unexpected_paths=$taskUnexpected;passed=($taskFreezeFailures.Count -eq 0 -and $taskMissingRoots.Count -eq 0 -and $taskUnexpected.Count -eq 0)}))

$taskProtectedPaths = @('config/legacy-freeze-manifest.json','docs/r31-frozen-baseline.md','tests/fixtures/engine/r31-reviewer-regressions.json')
$taskProtectedPaths += @(Get-ChildItem -LiteralPath (Join-Path $taskRoot 'docs') -File | Where-Object { $_.Name -like '*-before-redesign-1.0.md' } | ForEach-Object { 'docs/' + $_.Name })
$taskProtected = @($taskProtectedPaths | ForEach-Object {
    $taskExpectedBlob = (git -C $taskRoot rev-parse "${taskBase}:$_").Trim()
    $taskCurrentBlob = (git -C $taskRoot hash-object "--path=$_" (Join-Path $taskRoot $_)).Trim()
    [ordered]@{path=$_;base_git_blob=$taskExpectedBlob;current_git_blob=$taskCurrentBlob;unchanged=($taskExpectedBlob -eq $taskCurrentBlob)}
})
Save-TaskText 'protected-documents.json' (ConvertTo-Json -Depth 8 -InputObject $taskProtected)

$taskDiff = @(git -C $taskRoot -c core.quotepath=false diff --unified=0 $taskBase -- @taskDocs)
if ($LASTEXITCODE -ne 0) { throw 'Document diff failed' }
Save-TaskText 'authority-documents.patch' ($taskDiff | ForEach-Object { if ($_.StartsWith('@@')) { $_.TrimEnd() } else { $_ } })
$taskDiffCheck = @(git -C $taskRoot diff --check $taskBase)
$taskDiffExit = $LASTEXITCODE
Save-TaskText 'diff-check.txt' (@("git diff --check $taskBase", "exit_code=$taskDiffExit") + $taskDiffCheck)
$taskChanged = @(git -C $taskRoot diff --name-only $taskBase)
$taskChanged += @(git -C $taskRoot ls-files --others --exclude-standard)
$taskUnexpectedChanges = @($taskChanged | Where-Object { $_ -notin $taskDocs -and -not $_.StartsWith('docs/evidence/fm-01-20260908/') })
$taskResult = [ordered]@{base_commit=$taskBase;observed_head=$taskHead;branch=$taskBranch;checkout=$taskRoot;observed_at_utc=[DateTime]::UtcNow.ToString('o');input_count=$taskInputHashes.Count;authority_document_count=$taskDocHashes.Count;freeze_entries=$taskFreezeRows.Count;freeze_failures=$taskFreezeFailures.Count;unexpected_frozen_paths=$taskUnexpected;missing_freeze_roots=$taskMissingRoots;protected_document_count=$taskProtected.Count;protected_document_failures=@($taskProtected | Where-Object { -not $_.unchanged });diff_check_exit=$taskDiffExit;unexpected_changed_paths=$taskUnexpectedChanges;semantic_review='Separate human/agent review required; rg matches and hashes are not semantic PASS';product_qualification='NOT_RUN'}
Save-TaskText 'verification.json' (ConvertTo-Json -Depth 8 -InputObject $taskResult)
$taskResult | ConvertTo-Json -Depth 8
if ($taskDiffExit -ne 0 -or $taskFreezeFailures.Count -gt 0 -or $taskMissingRoots.Count -gt 0 -or $taskUnexpected.Count -gt 0 -or @($taskProtected | Where-Object { -not $_.unchanged }).Count -gt 0 -or $taskUnexpectedChanges.Count -gt 0) { exit 1 }
