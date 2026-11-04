@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Start-Remote-Review.ps1" %*
pause
