@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Amadeus Local llama.cpp Server

set "PYTHON_BIN=%AMADEUS_PYTHON%"
if not defined PYTHON_BIN if exist "%CD%\.venv_cu124\Scripts\python.exe" set "PYTHON_BIN=%CD%\.venv_cu124\Scripts\python.exe"
if not defined PYTHON_BIN if exist "%CD%\.venv\Scripts\python.exe" set "PYTHON_BIN=%CD%\.venv\Scripts\python.exe"

if not defined PYTHON_BIN (
  echo ERROR: Amadeus Python was not found.
  echo Set AMADEUS_PYTHON or create .venv_cu124 / .venv in this repository.
  pause
  exit /b 1
)

echo Starting the pure-local llama.cpp profile from .env / Settings...
"%PYTHON_BIN%" -m llm.llama_server --foreground --profile local
set "LLAMA_EXIT=%ERRORLEVEL%"

echo.
echo Local llama.cpp server stopped with exit code %LLAMA_EXIT%.
pause
exit /b %LLAMA_EXIT%
