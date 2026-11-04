param([int]$Add = 250)
$ErrorActionPreference = 'Stop'
if ($Add -lt 1) { throw 'Choose a positive number of additional photos.' }
$leadPipeline = $PSScriptRoot
$leadProject = (Resolve-Path -LiteralPath (Join-Path $leadPipeline '../..')).Path
$leadData = Join-Path $leadProject 'BrakeCheck_Dataset'
$leadPython = Join-Path $leadPipeline '.venv/Scripts/python.exe'
Push-Location -LiteralPath $leadPipeline
try {
    $leadCurrent = (& $leadPython -m bcdata.lead_curation --data-root $leadData --stage count)
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the current lead collection.' }
    $leadTotal = [int]$leadCurrent + $Add
    Write-Output ('Adding ' + $Add + ' vehicle-candidate photos; new total ' + $leadTotal + '.')
    & $leadPython -m bcdata.lead_curation --data-root $leadData --stage pool --target $leadTotal
    if ($LASTEXITCODE -ne 0) { throw 'Candidate inventory failed.' }
    $leadLinuxPipeline = (& wsl.exe -e wslpath -a $leadPipeline).Trim()
    $leadLinuxData = (& wsl.exe -e wslpath -a $leadData).Trim()
    $leadLinuxWeights = (& wsl.exe -e wslpath -a (Join-Path $leadProject 'weights/LibreRTMDett.pt')).Trim()
    & wsl.exe --cd $leadLinuxPipeline -e /home/nick/brakecheck-detector-env/bin/python -m bcdata.lead_curation --stage screen --target $leadTotal --data-root $leadLinuxData --weights $leadLinuxWeights
    if ($LASTEXITCODE -ne 0) { throw 'Detector screening failed; previous labels and batches are preserved.' }
    & $leadPython -m bcdata.lead_curation --data-root $leadData --stage publish --target $leadTotal
    if ($LASTEXITCODE -ne 0) { throw 'Publication stopped; inspect the curation report before retrying.' }
    Write-Output 'Ready in BrakeCheck Studio > Lead cars.'
} finally { Pop-Location }
