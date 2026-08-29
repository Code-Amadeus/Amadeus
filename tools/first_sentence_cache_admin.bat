@echo off
setlocal
cd /d "%~dp0\.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if exist ".venv_cu124\Scripts\python.exe" (
  ".venv_cu124\Scripts\python.exe" -X utf8 tools\first_sentence_cache_admin.py %*
) else (
  python -X utf8 tools\first_sentence_cache_admin.py %*
)
