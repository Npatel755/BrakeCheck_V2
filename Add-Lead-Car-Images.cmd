@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Expand-Lead-Car-Images.ps1" -Add 250
pause
