param([switch]$Open)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Push-Location $projectRoot
try {
    python (Join-Path $PSScriptRoot 'prepare_continuity.py')
    if ($LASTEXITCODE -ne 0) { throw 'Camera fixture extraction failed.' }
    $linuxRoot = (& wsl -d Ubuntu-24.04 -- wslpath -a $projectRoot).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve WSL project path.' }
    $testRoot = "$linuxRoot/reports/ar-mock-test-20261023"
    foreach ($variant in @('baseline', 'continuity')) {
        $resultName = if ($variant -eq 'baseline') { 'swift-gap-baseline-results.json' } else { 'swift-sustained-results.json' }
        $extraArgs = if ($variant -eq 'continuity') { @('--continuity') } else { @() }
        & wsl -d Ubuntu-24.04 -- /home/nick/.local/share/swiftly/bin/swift run `
            --package-path "$linuxRoot/tools/ar_mock_test/Runner" --scratch-path /tmp/brakecheck-ar-mock-build `
            -c release --jobs 2 ARMockRunner "$testRoot/mock-continuity-drive.json" "$testRoot/$resultName" @extraArgs
        if ($LASTEXITCODE -ne 0) { throw "Swift $variant run failed." }
    }
    python (Join-Path $PSScriptRoot 'render_sustained.py')
    if ($LASTEXITCODE -ne 0) { throw 'Comparison export failed.' }
    & (Join-Path $projectRoot 'tools/dataset_pipeline/.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'verify_sustained.py')
    if ($LASTEXITCODE -ne 0) { throw 'Comparison verification failed.' }
    $previewFile = Join-Path $projectRoot 'reports/ar-mock-test-20261023/sustained.html'
    Write-Output "Ready: $previewFile"
    if ($Open) {
        $edgePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft/Edge/Application/msedge.exe'
        Start-Process -FilePath $edgePath -ArgumentList ([System.Uri]$previewFile).AbsoluteUri
    }
} finally { Pop-Location }
