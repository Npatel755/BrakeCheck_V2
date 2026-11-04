@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Start-Audi-Import.ps1" %*
pause
