param([int]$Epochs = 20, [string]$ResumeRun = '', [string]$WarmStart = '')
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$gpuPython = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
$active = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m bcdata.ufld_baseline' }
if ($active) {
    Write-Output 'A UFLDv2 baseline worker is already running.'
    exit 0
}
if ($Epochs -lt 1) { throw 'Epochs must be positive.' }
if ($ResumeRun -and $WarmStart) { throw 'Choose ResumeRun for unchanged data or WarmStart for the latest data.' }
$workerArgs = '-m bcdata.ufld_baseline --epochs ' + $Epochs
if ($ResumeRun) {
    $resumePath = (Resolve-Path -LiteralPath $ResumeRun).Path
    if ($resumePath.Contains('"')) { throw 'Invalid run path.' }
    $workerArgs += ' --resume-run "' + $resumePath + '"'
}
if ($WarmStart) {
    $checkpointPath = (Resolve-Path -LiteralPath $WarmStart).Path
    if ($checkpointPath.Contains('"')) { throw 'Invalid checkpoint path.' }
    $workerArgs += ' --warm-start "' + $checkpointPath + '"'
}
$logs = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$job = Start-Process -FilePath $gpuPython -ArgumentList $workerArgs -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs ('ufld-training-' + $stamp + '.stdout.log')) -RedirectStandardError (Join-Path $logs ('ufld-training-' + $stamp + '.stderr.log'))
Write-Output ('UFLDv2 baseline launcher started: ' + $job.Id)
Write-Output 'Progress: BrakeCheck_Dataset\reports\ufld_training.json'
