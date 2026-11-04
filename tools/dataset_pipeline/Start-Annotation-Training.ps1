param([string]$Cohort = '', [int]$Epochs = 8)
$ErrorActionPreference = 'Stop'
$env:HF_HUB_DISABLE_XET = '1'
$env:OMP_NUM_THREADS = '2'
$env:MKL_NUM_THREADS = '2'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$gpuPython = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) { throw 'The installed GPU model environment is missing.' }
if ($Epochs -lt 1) { throw 'Epochs must be positive.' }
$active = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m bcdata.annotation_training(?:\s|$)' }
if ($active) {
    Write-Output 'The annotation model worker is already running or waiting for the GPU.'
    exit 0
}
if (-not $Cohort) {
    $cohortReport = Join-Path $projectRoot 'BrakeCheck_Dataset\reports\annotation_update-latest.json'
    if (-not (Test-Path -LiteralPath $cohortReport)) { throw 'Provide -Cohort with a verified original-source-mask training manifest.' }
    $Cohort = (Get-Content -LiteralPath $cohortReport -Raw | ConvertFrom-Json).cohort_path
}
$cohortPath = (Resolve-Path -LiteralPath $Cohort).Path
if ($cohortPath.Contains('"')) { throw 'Invalid cohort path.' }
$logs = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$workerArgs = '-u -m bcdata.annotation_training --wait --epochs ' + $Epochs + ' --cohort "' + $cohortPath + '"'
$job = Start-Process -FilePath $gpuPython -ArgumentList $workerArgs -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs ('annotation-training-' + $stamp + '.stdout.log')) -RedirectStandardError (Join-Path $logs ('annotation-training-' + $stamp + '.stderr.log'))
Set-Content -LiteralPath (Join-Path $logs 'annotation-training.pid') -Value $job.Id -Encoding ascii
Write-Output ('Annotation model launcher started: ' + $job.Id)
Write-Output 'Progress: BrakeCheck_Dataset\reports\annotation_training.json'
Write-Output 'Trains an isolated road/paint candidate and compares it with the pinned original model. Saved reviews and active proposals stay preserved.'
