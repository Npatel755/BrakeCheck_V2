$ErrorActionPreference = 'Stop'
$zodPipeline = $PSScriptRoot
$zodProject = (Resolve-Path -LiteralPath (Join-Path $zodPipeline '../..')).Path
$zodData = Join-Path $zodProject 'BrakeCheck_Dataset'
$zodBase = (Get-Content -LiteralPath (Join-Path $zodData 'reports/converted_data.json') -Raw | ConvertFrom-Json).run
$zodOutput = Join-Path $zodData ('models/ufld_baseline/run-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-zod-cleared')
Write-Output 'Preparing a new snapshot of cleared ZOD labels with the existing training base.'
Write-Output 'This verifies the frozen base and new labels; it does not start training.'
Push-Location -LiteralPath $zodPipeline
try {
    & (Join-Path $zodPipeline '.venv/Scripts/python.exe') -m bcdata.zod_training --data-root $zodData --base-run $zodBase --output-run $zodOutput
    if ($LASTEXITCODE -ne 0) { throw 'ZOD preparation failed. Existing source data and training snapshots are preserved.' }
    Write-Output ('Prepared run: ' + $zodOutput)
} finally {
    Pop-Location
}
