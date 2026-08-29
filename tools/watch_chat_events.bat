@echo off
setlocal
cd /d "%~dp0\.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist ".venv_cu124\Scripts\python.exe" (
  ".venv_cu124\Scripts\python.exe" -X utf8 tools\watch_chat_events.py ws://127.0.0.1:17777/ws 120
) else (
  python -X utf8 tools\watch_chat_events.py ws://127.0.0.1:17777/ws 120
)
pause
