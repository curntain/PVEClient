@echo off && setlocal && cd /d "%~dp0" && echo Building EXE with PyInstaller... && ".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean PVEClient.spec || (echo BUILD FAILED & exit /b 1)
