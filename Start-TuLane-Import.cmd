@echo off
setlocal
cd /d "%~dp0tools\dataset_pipeline"
".venv\Scripts\python.exe" -m bcdata.import_tulane --data-root "%~dp0BrakeCheck_Dataset" --workers 8 %*
if errorlevel 1 pause
