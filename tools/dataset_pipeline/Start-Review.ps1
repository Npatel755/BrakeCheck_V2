param([switch]$Stop, [switch]$NoBrowser, [int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$dataRoot = Join-Path $projectRoot 'BrakeCheck_Dataset'
$pythonPath = Join-Path $pipelineRoot '.venv\Scripts\python.exe'
$logRoot = Join-Path $dataRoot 'logs'
$pidPath = Join-Path $logRoot ('review-server-' + $Port + '.pid')
$desktopUrl = 'http://127.0.0.1:' + $Port

if ($Stop) {
    & $pythonPath (Join-Path $pipelineRoot 'tools\stop_review.py') --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'The matching review server could not be stopped.' }
    if (Test-Path -LiteralPath $pidPath) { Remove-Item -LiteralPath $pidPath }
    exit 0
}

New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $pythonPath)) {
    & python -m venv --system-site-packages (Join-Path $pipelineRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the review Python environment.' }
    & $pythonPath -m pip install -r (Join-Path $pipelineRoot 'requirements-review.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install review dependencies.' }
}

$alreadyRunning = $false
try {
    $health = Invoke-RestMethod -Uri ($desktopUrl + '/api/health') -TimeoutSec 2
    if ($health.app -eq 'brakecheck-human-review') { $alreadyRunning = $true }
    else { throw 'This port is already used by another service.' }
} catch {
    if ($_.Exception.Message -eq 'This port is already used by another service.') { throw }
}

if (-not $alreadyRunning) {
    $preparationPath = Join-Path $dataRoot 'reports\preparation.json'
    $prepared = $false
    if (Test-Path -LiteralPath $preparationPath) {
        $prepared = (Get-Content -LiteralPath $preparationPath -Raw | ConvertFrom-Json).state -eq 'ready'
    }
    if (-not $prepared) {
        Write-Output 'Preparing the dataset. Source images and masks may need downloading once.'
        & $pythonPath (Join-Path $pipelineRoot 'review.py') prepare --data-root $dataRoot
        if ($LASTEXITCODE -ne 0) { throw 'Dataset preparation failed. See BrakeCheck_Dataset\reports\preparation.json.' }
    }
    $args = '"' + (Join-Path $pipelineRoot 'review.py') + '" serve --data-root "' + $dataRoot + '" --port ' + $Port
    $reviewServer = Start-Process -FilePath $pythonPath -ArgumentList $args -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'review-server.stdout.log') -RedirectStandardError (Join-Path $logRoot 'review-server.stderr.log')
    Set-Content -LiteralPath $pidPath -Value $reviewServer.Id -Encoding ascii
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 300
        try {
            $health = Invoke-RestMethod -Uri ($desktopUrl + '/api/health') -TimeoutSec 1
            if ($health.app -eq 'brakecheck-human-review') { $ready = $true; break }
        } catch {}
        if ($reviewServer.HasExited) { break }
    }
    if (-not $ready) { throw 'The review server did not start. See BrakeCheck_Dataset\logs\review-server.stderr.log.' }
}

Write-Output ('PC reviewer: ' + $desktopUrl)
$pairing = Invoke-RestMethod -Uri ($desktopUrl + '/api/host') -TimeoutSec 5
foreach ($phoneUrl in $pairing.urls) { Write-Output ('Phone on the same Wi-Fi: ' + $phoneUrl) }
Write-Output 'Click Pair phone on the PC page to show the QR and pairing code.'
if (-not $NoBrowser) { Start-Process $desktopUrl }
