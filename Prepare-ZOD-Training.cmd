@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\dataset_pipeline\Prepare-ZOD-Training.ps1"
pause
