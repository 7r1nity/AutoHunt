@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0docker\compose.ps1" -Action Stop %*
set "AUTOHUNT_EXIT=%ERRORLEVEL%"
if not "%AUTOHUNT_EXIT%"=="0" pause
exit /b %AUTOHUNT_EXIT%
