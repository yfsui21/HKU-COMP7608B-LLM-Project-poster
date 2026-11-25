param([string]$Repository = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$taskRepo = (Resolve-Path -LiteralPath $Repository).Path
Set-Location -LiteralPath $taskRepo
$gitSafe = "safe.directory=$($taskRepo.Replace('\','/'))"
$targetBranch = 'work/proposal-ai-study'
$studyRoot = Join-Path $taskRepo 'runs/proposal-live-study'
$statusPath = Join-Path $studyRoot 'finalization-status.json'
function Save-Status([string]$state, [string]$detail) {
    @{state=$state;detail=$detail;updated_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
function Run-Git([string[]]$Arguments) {
    & git -c $gitSafe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git failed: $($Arguments -join ' ')" }
}
try {
    Save-Status 'waiting_for_batch' 'Progress commit is separate; waiting for all 90 case records.'
    $resultPath = Join-Path $studyRoot 'results.json'
    while (-not (Test-Path -LiteralPath $resultPath)) { Start-Sleep -Seconds 15 }
    $result = $null
    for ($readAttempt=0; $readAttempt -lt 5 -and $null -eq $result; $readAttempt++) {
        try { $result = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Start-Sleep -Seconds 2 }
    }
    if ($null -eq $result) { throw 'Could not read the completed result file.' }
    if ($result.expected_cases -ne 90 -or $result.attempted -ne 90 -or $result.cases.Count -ne 90) { throw 'Batch is incomplete; refusing to label or publish it as a completed 90-case run.' }
    $identifiers = @($result.cases | ForEach-Object { "$($_.paper)|$($_.area)|$($_.condition)" } | Sort-Object -Unique)
    if ($identifiers.Count -ne 90) { throw 'Duplicate or missing case identifiers.' }
    Save-Status 'exporting' '90 attempts recorded. Failures and missing scores remain explicit.'
    & ./.venv/Scripts/python.exe export_ai_study.py --study runs/proposal-live-study
    if ($LASTEXITCODE -ne 0) { throw 'Result export failed.' }
    $nodeExe = 'C:/Users/20673/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
    $sheetBuilder = 'runs/human-review/ai-simulated-review/build_multi_workbook.mjs'
    if ((Test-Path -LiteralPath $nodeExe) -and (Test-Path -LiteralPath $sheetBuilder)) {
        & $nodeExe $sheetBuilder runs/proposal-live-study
        if ($LASTEXITCODE -ne 0) { throw 'Workbook export failed.' }
    }
    & ./.venv/Scripts/python.exe package_ai_study.py --study runs/proposal-live-study
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
    @{status='BATCH_FINISHED';expected_cases=90;processed_cases=90;accepted_outputs=$result.accepted_outputs;accepted_scored=$result.ai_scored;rejected_candidate_diagnostic_scored=$result.failed_candidate_ai_scored;evaluation_type='AI-assisted role reviews, not two independent human ratings'} | ConvertTo-Json | Set-Content -LiteralPath 'evaluation/proposal-ai-study/progress-snapshot.json' -Encoding UTF8
    $branch = & git -c $gitSafe branch --show-current
    if ($branch.Trim() -ne $targetBranch) { throw 'Current branch changed; stopped to preserve user work.' }
    & git -c $gitSafe diff --cached --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Index contains other staged work; stopped without committing it.' }
    Save-Status 'fetching' 'Checking the latest remote commits before the results commit and push.'
    if (Test-Path -LiteralPath (Join-Path $taskRepo 'report/REVIEW_REQUIRED.txt')) { throw 'A report/results review hold is active; no automatic commit or push is permitted.' }
    Run-Git -Arguments @('fetch','origin')
    Run-Git -Arguments @('add','evaluation/proposal-ai-study')
    Run-Git -Arguments @('diff','--cached','--check')
    & git -c $gitSafe diff --cached --quiet
    if ($LASTEXITCODE -ne 0) { Run-Git -Arguments @('commit','-m','Record completed 90-case AI-assisted study attempts and outcomes') }
    Run-Git -Arguments @('merge','--no-edit','origin/main')
    foreach ($check in @('verify_crop_recovery.py','verify_demo.py','verify_native_tree_smoke.py')) {
        & ./.venv/Scripts/python.exe $check
        if ($LASTEXITCODE -ne 0) { throw "Validation failed: $check" }
    }
    Save-Status 'pushing' 'Pushing the completed attempt records, including failures, to the working branch.'
    Run-Git -Arguments @('push','origin',"HEAD:refs/heads/$targetBranch")
    $commit = & git -c $gitSafe rev-parse HEAD
    Save-Status 'pushed' "Results commit: $commit"
} catch {
    Save-Status 'stopped_needs_review' $_.Exception.Message
    Write-Error $_.Exception.Message
    exit 1
}
