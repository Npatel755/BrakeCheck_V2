param([switch]$Resume)
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$dataRoot = Join-Path $projectRoot 'BrakeCheck_Dataset'
$pythonPath = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
$experimentRoot = Join-Path $dataRoot 'experiments\flagged_teacher'
New-Item -ItemType Directory -Path $experimentRoot -Force | Out-Null
$existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'bcdata\.flagged_model_trial' }
if ($existing) {
    Write-Output ('Experiment worker already running: ' + (($existing | Select-Object -ExpandProperty ProcessId) -join ', '))
    exit 0
}
if ($Resume) {
    $run = Get-ChildItem -LiteralPath $experimentRoot -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'experiment.json') } | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $run) { throw 'No experiment to resume.' }
    $runPath = $run.FullName
    $arguments = '-u -m bcdata.flagged_model_trial --resume --output "' + $runPath + '" --data-root "' + $dataRoot + '"'
} else {
    $runPath = Join-Path $experimentRoot ('run-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    # The orchestrator freezes the exact cleared dataset and these parent weights.
    $baseRun = Join-Path $dataRoot 'models\ufld_baseline\run-20261020-142147-post-n62-label-fixes'
    $teacher = Join-Path $dataRoot 'models\comma_segnet_adapted\run-20261018-233159\best.pt'
    $arguments = '-u -m bcdata.flagged_model_trial --output "' + $runPath + '" --data-root "' + $dataRoot + '" --base-run "' + $baseRun + '" --parent-checkpoint "' + $teacher + '"'
    if (Test-Path -LiteralPath $runPath) { throw 'Experiment run name already exists; use -Resume.' }
}
$logPrefix = $runPath + '.worker'
$worker = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput ($logPrefix + '.stdout.log') -RedirectStandardError ($logPrefix + '.stderr.log')
Set-Content -LiteralPath ($runPath + '.pid') -Value $worker.Id -Encoding ascii
Write-Output ('Experiment worker PID: ' + $worker.Id)
Write-Output ('Run: ' + $runPath)
Write-Output 'Stages: cleared-data teacher training -> isolated copies/proposals -> cleared+extra UFLDv2 training.'
Write-Output 'Optional comparison queue: http://127.0.0.1:8765/experiment'
