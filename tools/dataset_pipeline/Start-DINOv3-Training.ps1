param([int]$Epochs = 20, [string]$Resume = '', [string]$WarmStart = '', [string]$Cohort = '')
$ErrorActionPreference = 'Stop'
$env:HF_HUB_DISABLE_XET = '1'
$env:OMP_NUM_THREADS = '2'
$env:MKL_NUM_THREADS = '2'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$gpuPython = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) { throw 'Run Setup-Large-Models.cmd first.' }
if ($Epochs -lt 1) { throw 'Epochs must be positive.' }
if ($Resume -and $WarmStart) { throw 'Choose Resume for the original experiment or WarmStart for a new cohort.' }
$active = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m bcdata.dinov3_training(?:\s|$)' }
if ($active) {
    Write-Output 'The DINOv3 decoder worker is already running or waiting for the GPU.'
    exit 0
}
$logRoot = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$workerArgs = '-u -m bcdata.dinov3_training --wait --epochs ' + $Epochs
if ($Resume) {
    $resumePath = (Resolve-Path -LiteralPath $Resume).Path
    if ($resumePath.Contains('"')) { throw 'Invalid checkpoint path.' }
    $workerArgs += ' --resume "' + $resumePath + '"'
}
if ($WarmStart) {
    $warmStartPath = (Resolve-Path -LiteralPath $WarmStart).Path
    if ($warmStartPath.Contains('"')) { throw 'Invalid checkpoint path.' }
    $workerArgs += ' --warm-start "' + $warmStartPath + '"'
}
if ($Cohort) {
    $cohortPath = (Resolve-Path -LiteralPath $Cohort).Path
    if ($cohortPath.Contains('"')) { throw 'Invalid cohort path.' }
    $workerArgs += ' --cohort "' + $cohortPath + '"'
}
$job = Start-Process -FilePath $gpuPython -ArgumentList $workerArgs -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot ('dinov3-training-' + $stamp + '.stdout.log')) -RedirectStandardError (Join-Path $logRoot ('dinov3-training-' + $stamp + '.stderr.log'))
Set-Content -LiteralPath (Join-Path $logRoot 'dinov3-training.pid') -Value $job.Id -Encoding ascii
Write-Output ('DINOv3 decoder worker started. Launcher PID: ' + $job.Id)
Write-Output 'It verifies frozen source masks and weights, waits for the GPU, checks real 7B fit, then trains the decoder candidate.'
Write-Output 'Results: BrakeCheck_Dataset\reports\dinov3_training.json. Candidate weights are never adopted automatically.'
