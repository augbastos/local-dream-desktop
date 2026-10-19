@echo off
setlocal
cd /d "%~dp0"
set "LDPC_PY="
set "LDPC_ARGS="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "LDPC_PY=%~dp0.venv\Scripts\python.exe"
    goto check
)
rem Prefer python on PATH to another app's bundled interpreter.
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "LDPC_PY=python"
    goto check
)
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    set "LDPC_PY=py"
    set "LDPC_ARGS=-3"
    goto check
)
echo Python 3.10 or newer was not found. Install Python, then run:
echo py -m pip install -r requirements.txt
goto failed

:check
rem Check once, then launch the same entry used by packaged releases.
"%LDPC_PY%" %LDPC_ARGS% "%~dp0tools\check_env.py" --launch
if errorlevel 1 goto failed
exit /b 0

:failed
echo Local Dream Desktop could not start. Resolve the problem above and try again.
pause
exit /b 1
