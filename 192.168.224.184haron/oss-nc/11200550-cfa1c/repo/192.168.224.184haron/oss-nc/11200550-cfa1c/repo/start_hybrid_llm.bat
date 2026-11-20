@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Amadeus Hybrid Local Head

set "PYTHON_BIN=%AMADEUS_PYTHON%"
if not defined PYTHON_BIN if exist "%CD%\.venv\Scripts\python.exe" set "PYTHON_BIN=%CD%\.venv\Scripts\python.exe"

if not defined PYTHON_BIN (
  echo ERROR: Amadeus Python was not found.
  echo Set AMADEUS_PYTHON or create .venv in this repository.
  pause
  exit /b 1
)

echo Starting the Hybrid local-head profile from .env / Settings...
"%PYTHON_BIN%" -m llm.llama_server --foreground --profile hybrid
set "LLAMA_EXIT=%ERRORLEVEL%"

echo.
echo Hybrid local-head server stopped with exit code %LLAMA_EXIT%.
pause
exit /b %LLAMA_EXIT%
