@echo off
set "PYTHON=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYTHON%" (
  echo Python environment not found. Run the setup commands in README.md.
  pause
  exit /b 1
)
start "" "%PYTHON%" "%~dp0app.py"
