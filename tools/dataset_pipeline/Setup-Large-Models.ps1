param([switch]$Run)
$ErrorActionPreference = 'Stop'
$pipelineRoot = $PSScriptRoot
$gpuRoot = Join-Path $pipelineRoot '.gpu-venv'
$gpuPython = Join-Path $gpuRoot 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $gpuPython)) {
    & python -m venv --system-site-packages $gpuRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the separate GPU environment.' }
}
& $gpuPython -c 'import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)'
if ($LASTEXITCODE -ne 0) {
    & $gpuPython -m pip install 'torch==2.11.0' 'torchvision==0.26.0' --index-url https://download.pytorch.org/whl/cu128
    if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed.' }
}
& $gpuPython -m pip install 'transformers>=5.0,<6' 'huggingface_hub>=1,<2' 'accelerate>=1.10,<2' 'segmentation-models-pytorch==0.5.0' safetensors pyyaml scipy opencv-python-headless pillow requests filelock addict tensorboard pathspec
if ($LASTEXITCODE -ne 0) { throw 'Model dependencies could not be installed.' }
Push-Location -LiteralPath $pipelineRoot
try {
    & $gpuPython -m bcdata.gpu_candidates inspect
    if ($LASTEXITCODE -ne 0) { throw 'The RTX 4090 preflight needs attention. See BrakeCheck_Dataset\reports\model_status.json.' }
    if ($Run) {
        & $gpuPython -m bcdata.gpu_candidates run --teachers sam3,sam2_large
        if ($LASTEXITCODE -ne 0) {
            Write-Output 'One or more large models need attention. Saved reviews are unaffected.'
            Write-Output 'If SAM 3 is waiting for access, request approval at https://huggingface.co/facebook/sam3, then run this again.'
        }
    }
} finally { Pop-Location }
Write-Output 'Model inference only. UFLDv2 training has not been started.'
