@echo off
cd /d "%~dp0"
"tools\dataset_pipeline\.venv\Scripts\python.exe" "tools\dataset_pipeline\tools\start_zod_mirror_import.py"
pause
