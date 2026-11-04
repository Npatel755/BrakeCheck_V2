param([string]$PlanFile = '')
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$gpuPython = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
$active = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m bcdata.import_audi_remaining' }
if ($active) {
    Write-Output 'The remaining Audi import worker is already running.'
    exit 0
}
if (-not $PlanFile) {
    $PlanFile = Join-Path $projectRoot 'BrakeCheck_Dataset\reports\a2d2_remaining-20261018\import_plan_remaining.json'
}
$planPath = (Resolve-Path -LiteralPath $PlanFile).Path
if ($planPath.Contains('"')) { throw 'Invalid plan path.' }
if (-not (Test-Path -LiteralPath $gpuPython)) { throw 'The installed model environment is missing.' }
$logs = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$workerArgs = '-u -m bcdata.import_audi_remaining --plan-file "' + $planPath + '"'
$job = Start-Process -FilePath $gpuPython -ArgumentList $workerArgs -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs ('a2d2-remaining-' + $stamp + '.stdout.log')) -RedirectStandardError (Join-Path $logs ('a2d2-remaining-' + $stamp + '.stderr.log'))
Write-Output ('Audi import launcher started: ' + $job.Id)
Write-Output 'Progress: BrakeCheck_Dataset\reports\a2d2_remaining-20261018\progress.json'
Write-Output 'Current batch: BrakeCheck_Dataset\reports\a2d2_import.json'
