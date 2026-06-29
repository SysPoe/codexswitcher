@echo off
setlocal
set "SCRIPT=%~dp0codexswitcher.py"
if not exist "%SCRIPT%" (
  >&2 echo codexswitcher.py was not found beside this launcher.
  exit /b 2
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

where python3 >nul 2>nul
if not errorlevel 1 (
  python3 "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if not errorlevel 1 (
  python "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

>&2 echo Python 3 was not found on PATH. Install Python or run codexswitcher.py with a Python 3 interpreter.
exit /b 2
