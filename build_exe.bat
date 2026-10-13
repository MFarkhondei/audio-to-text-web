@echo off
echo Installing PyInstaller...
python -m pip install pyinstaller

echo Building executable...
pyinstaller --clean audio_to_text.spec

echo.
echo Done! The executable is in the dist folder: dist\AudioToText.exe
echo Note: ffmpeg must still be installed on the system.
pause
