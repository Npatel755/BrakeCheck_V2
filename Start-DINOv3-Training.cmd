@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Start-DINOv3-Training.ps1" %*
pause
