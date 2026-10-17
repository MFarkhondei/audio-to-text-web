@echo off
setlocal
cd /d "%~dp0"
set "APP_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "APP_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist ".runtime\python313\python.exe" set "APP_PYTHON=%~dp0.runtime\python313\python.exe"
if not defined WHISPER_DOWNLOAD_ROOT set "WHISPER_DOWNLOAD_ROOT=%~dp0models"

"%APP_PYTHON%" app.py
if errorlevel 1 pause
