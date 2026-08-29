@echo off
chcp 65001 >NUL
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set LANG=zh_CN.UTF-8
set AMADEUS_PYTHON=%CD%\.venv_cu124\Scripts\python.exe
set RAG_ENABLED_FOR_LOCAL=0
set VTS_ENABLED=0
set VTS_HEARTBEAT_ENABLED=0
set VTS_RECONNECT_ENABLED=0

cd /d "%~dp0electron"
npm run electron:dev
