@echo off
setlocal
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 tools\first_sentence_cache_admin.py %*
) else (
  python -X utf8 tools\first_sentence_cache_admin.py %*
)
