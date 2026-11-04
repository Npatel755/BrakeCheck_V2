param([switch]$Stop)
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $pipelineRoot '..\..')).Path
$datasetRoot = Join-Path $projectRoot 'BrakeCheck_Dataset'
$configPath = Join-Path $datasetRoot 'reports\remote_access.json'
$binary = Join-Path $pipelineRoot 'vendor\cloudflared\cloudflared.exe'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$previous = $null
if (Test-Path -LiteralPath $configPath) { $previous = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json }
if ($previous -and $previous.tunnel_pid) {
    $existing = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $previous.tunnel_pid)
    $matching = $existing -and $existing.ExecutablePath -eq $binary
    if ($matching -and -not $Stop) {
        Write-Output ('Remote reviewer is already running: ' + $previous.url)
        Write-Output 'On the PC, choose Pair phone > Use away from home for the private link and QR.'
        exit 0
    }
    if ($matching -and $Stop) { Stop-Process -Id $previous.tunnel_pid }
}
if ($Stop) {
    if ($previous) {
        $previous.enabled = $false
        [IO.File]::WriteAllText($configPath, ($previous | ConvertTo-Json), $utf8)
    }
    Write-Output 'Remote reviewer tunnel stopped. Local review and training continue.'
    exit 0
}
if (-not (Test-Path -LiteralPath $binary)) { throw 'The verified cloudflared binary is missing.' }
$expected = '4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6'
if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw 'Cloudflared checksum mismatch.' }
# Load the secure proxy-aware server before an external route can reach it.
& (Join-Path $pipelineRoot 'Start-Review.ps1') -Stop
& (Join-Path $pipelineRoot 'Start-Review.ps1') -NoBrowser
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$logRoot = Join-Path $datasetRoot 'logs'
$stderr = Join-Path $logRoot ('remote-review-' + $stamp + '.stderr.log')
$stdout = Join-Path $logRoot ('remote-review-' + $stamp + '.stdout.log')
$job = Start-Process -FilePath $binary -ArgumentList 'tunnel --no-autoupdate --url http://127.0.0.1:8765 --protocol http2' -WorkingDirectory $pipelineRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$publicUrl = $null
for ($attempt = 0; $attempt -lt 80; $attempt++) {
    if ($job.HasExited) { throw ('Tunnel exited. Inspect ' + $stderr) }
    if (Test-Path -LiteralPath $stderr) {
        $log = [string](Get-Content -LiteralPath $stderr -Raw)
        $match = [regex]::Match($log, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($match.Success) { $publicUrl = $match.Value; break }
    }
    Start-Sleep -Milliseconds 500
}
if (-not $publicUrl) { Stop-Process -Id $job.Id; throw ('Tunnel did not supply a URL. Inspect ' + $stderr) }
$remoteBytes = New-Object byte[] 32
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
$random.GetBytes($remoteBytes)
$random.Dispose()
$key = [Convert]::ToBase64String($remoteBytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$config = [ordered]@{ enabled = $true; url = $publicUrl; access_key = $key; tunnel_pid = $job.Id; stderr = $stderr; stdout = $stdout; binary_sha256 = $expected }
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json), $utf8)
# Register this exact external hostname; arbitrary public hosts remain blocked.
& (Join-Path $pipelineRoot 'Start-Review.ps1') -Stop
& (Join-Path $pipelineRoot 'Start-Review.ps1') -NoBrowser
Write-Output ('Remote reviewer online: ' + $publicUrl)
Write-Output 'On the PC, choose Pair phone > Use away from home. Scan its private QR or copy its private link.'
Write-Output 'Keep the PC and this tunnel running. The public address changes when a new tunnel is started.'
