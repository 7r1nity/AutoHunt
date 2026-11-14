@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0autohunt\scripts\launch-local.ps1" -Action start %*
set "AUTOHUNT_EXIT=%ERRORLEVEL%"
if not "%AUTOHUNT_EXIT%"=="0" pause
exit /b %AUTOHUNT_EXIT%
