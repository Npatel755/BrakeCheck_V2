param([switch]$Open)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$outputDir = Join-Path $projectRoot 'reports/ar-mock-test-20261023'
$runner = Join-Path $PSScriptRoot 'Runner'
Push-Location $projectRoot
try {
    python (Join-Path $PSScriptRoot 'generate.py') --out $outputDir
    if ($LASTEXITCODE -ne 0) { throw 'Fixture generation failed.' }
    $linuxProjectRoot = (& wsl -d Ubuntu-24.04 -- wslpath -a $projectRoot).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the project inside WSL.' }
    & wsl -d Ubuntu-24.04 -- /home/nick/.local/share/swiftly/bin/swift run `
        --package-path "$linuxProjectRoot/tools/ar_mock_test/Runner" `
        --scratch-path /tmp/brakecheck-ar-mock-build -c release --jobs 2 ARMockRunner `
        "$linuxProjectRoot/reports/ar-mock-test-20261023/mock-drive.json" `
        "$linuxProjectRoot/reports/ar-mock-test-20261023/swift-results.json"
    if ($LASTEXITCODE -ne 0) { throw 'Swift AR scenario failed.' }
    python (Join-Path $PSScriptRoot 'render.py') --out $outputDir
    if ($LASTEXITCODE -ne 0) { throw 'Video export failed.' }
    python (Join-Path $PSScriptRoot 'verify.py') --out $outputDir
    if ($LASTEXITCODE -ne 0) { throw 'Artifact verification failed.' }
    $previewFile = Join-Path $outputDir 'preview.html'
    Write-Output "Ready: $previewFile"
    if ($Open) {
        $edgePath = Join-Path ${env:ProgramFiles(x86)} 'Microsoft/Edge/Application/msedge.exe'
        if (Test-Path -LiteralPath $edgePath) {
            Start-Process -FilePath $edgePath -ArgumentList ([System.Uri]$previewFile).AbsoluteUri
        } else { Start-Process -FilePath $previewFile }
    }
} finally { Pop-Location }
