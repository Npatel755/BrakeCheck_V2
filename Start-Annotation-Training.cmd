@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Start-Annotation-Training.ps1" %*
pause
