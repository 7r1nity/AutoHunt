@echo off
setlocal
call "%~dp0..\stop-local.cmd" %*
exit /b %ERRORLEVEL%
