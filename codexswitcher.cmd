@echo off
setlocal
set "CODEXSWITCHER_CMD_LAUNCHER=1"
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0codexswitcher.ps1" %*
set "CODEXSWITCHER_EXIT=%ERRORLEVEL%"
endlocal & exit /b %CODEXSWITCHER_EXIT%
