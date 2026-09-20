@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating venv...
  py -3 -m venv .venv || python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -U pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
echo Starting PVE Client...
".venv\Scripts\python.exe" main.py
