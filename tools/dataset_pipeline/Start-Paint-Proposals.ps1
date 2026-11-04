$ErrorActionPreference = 'Stop'
$env:HF_HUB_DISABLE_XET = '1'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$gpuPython = Join-Path $PSScriptRoot '.gpu-venv\Scripts\python.exe'
$logs = Join-Path $projectRoot 'BrakeCheck_Dataset\logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$job = Start-Process -FilePath $gpuPython -ArgumentList '-m bcdata.paint_proposals' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs ('paint-proposals-' + $stamp + '.stdout.log')) -RedirectStandardError (Join-Path $logs ('paint-proposals-' + $stamp + '.stderr.log'))
Write-Output ('Fresh CPU paint proposals started: ' + $job.Id)
