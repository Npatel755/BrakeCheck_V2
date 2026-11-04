param([string]$Teachers = 'sam3,sam2_large')
$ErrorActionPreference = 'Stop'
$env:HF_HUB_DISABLE_XET = '1'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$gpuPython = Join-Path $pipelineRoot '.gpu-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) { throw 'Run Setup-Large-Models.cmd first.' }
if ($Teachers -notmatch '^(sam3|sam2_large)(,(sam3|sam2_large))*$') { throw 'Unknown model selection.' }
$logRoot = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$job = Start-Process -FilePath $gpuPython -ArgumentList ('-m bcdata.gpu_candidates run --teachers ' + $Teachers) -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot ('large-models-' + $runStamp + '.stdout.log')) -RedirectStandardError (Join-Path $logRoot ('large-models-' + $runStamp + '.stderr.log'))
Set-Content -LiteralPath (Join-Path $logRoot 'large-models.pid') -Value $job.Id -Encoding ascii
Write-Output ('Large-model background pass started. Launcher PID: ' + $job.Id)
Write-Output 'Progress is visible in the review app. No training is started.'
