@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 codex_project_recovery.py diagnose --redact %*
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python codex_project_recovery.py diagnose --redact %*
  ) else (
    echo Python 3 was not found. Install Python 3 and rerun this file.
    set ERR=9009
    goto :done
  )
)
set ERR=%errorlevel%
:done
echo.
pause
exit /b %ERR%
