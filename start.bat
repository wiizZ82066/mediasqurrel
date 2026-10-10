@echo off
setlocal
pushd "%~dp0"
if errorlevel 1 (
    echo Cannot open the Media Squirrel application directory.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run.py %*
) else (
    python run.py %*
)
set "MS_LAUNCH_EXIT=%errorlevel%"
popd

if not "%MS_LAUNCH_EXIT%"=="0" (
    echo.
    echo Media Squirrel could not start. Review the message above, then try again.
    pause
)
exit /b %MS_LAUNCH_EXIT%
