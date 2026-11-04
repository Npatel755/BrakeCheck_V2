@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Start-UFLD-Baseline.ps1" %*
pause
