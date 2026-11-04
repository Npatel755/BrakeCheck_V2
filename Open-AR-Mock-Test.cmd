@echo off
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
    start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" "%~dp0reports\ar-mock-test-20261023\sustained.html"
) else (
    start "" "%~dp0reports\ar-mock-test-20261023\sustained.html"
)
