@echo off
setlocal
cd /d "%~dp0"
set "SRT_PYTHON=%~dp0.runtime\python313\python.exe"

if not exist "%SRT_PYTHON%" (
    echo Preparing Python 3.13 inside the project...
    py install --target="%~dp0.runtime\python313" 3.13
    if errorlevel 1 goto failed
)

"%SRT_PYTHON%" -c "import sys, struct; print(sys.executable); print(sys.version); sys.exit(0 if sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto failed

"%SRT_PYTHON%" -m pip install --cache-dir "%~dp0.runtime\pip-cache" --no-warn-script-location -r "%~dp0requirements-srt.txt"
if errorlevel 1 goto failed

"%SRT_PYTHON%" -c "import ctranslate2, faster_whisper; print('CTranslate2:', ctranslate2.__version__)"
if errorlevel 1 goto failed
"%SRT_PYTHON%" -m pip check
if errorlevel 1 goto failed

echo Setup complete. Run run_srt.bat to start the app.
if not defined SRT_SETUP_NO_PAUSE pause
exit /b 0

:failed
echo Setup failed. See the error above.
echo This setup uses the Python install manager to prepare 64-bit Python 3.13.
echo If your py command does not support install, use the Python 3.13 venv instructions in README.md.
if not defined SRT_SETUP_NO_PAUSE pause
exit /b 1
