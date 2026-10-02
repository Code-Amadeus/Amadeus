@echo off
rem Companion without Electron.
rem Starts the local backend directly; the backend owns the Tk Companion card.
setlocal
chcp 65001 >NUL
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
set LANG=zh_CN.UTF-8
set OPEN_JTALK_DICT_DIR=%~dp0open_jtalk_dic\open_jtalk_dic_utf_8-1.11
set RAG_ENABLED_FOR_LOCAL=0
set VTS_ENABLED=0
set VTS_HEARTBEAT_ENABLED=0
set VTS_RECONNECT_ENABLED=0
set WAKE_ENABLED=0
set AEC_REALTIME_ENABLED=0
set AMADEUS_VISION_ENABLED=1
set AMADEUS_VISION_MODE=on_demand
set AMADEUS_VISION_SCOPE=read_window
set AMADEUS_VISION_PROVIDER=qwen
set AMADEUS_COMPANION=1
set TTS_BACKEND=gpt_sovits
set TTS_DEVICE=cuda
set TTS_OUTPUT_LANGUAGE=ja
set TTS_REF_AUDIO_JA=./assets/audio/reference/kurisu_reference.wav
rem Remote DashScope recognizer. The default is qwen3_asr, which loads the local
rem Qwen3-ASR model; this launcher must not do that. Conversation backend only.
set ASR_BACKEND=qwen_remote
set QWEN3_ASR_DEVICE=cpu
set MICROPHONE_DEVICE_INDEX=1
set MICROPHONE_PREFERRED_NAME=

cd /d "%~dp0"

if defined AMADEUS_PYTHON if exist "%AMADEUS_PYTHON%" set "PYTHON=%AMADEUS_PYTHON%"
if not defined PYTHON if exist "D:\a\Amadeus\.venv_cu124\Scripts\python.exe" set "PYTHON=D:\a\Amadeus\.venv_cu124\Scripts\python.exe"
if not defined PYTHON if exist ".venv_cu124\Scripts\python.exe" set "PYTHON=.venv_cu124\Scripts\python.exe"
if not defined PYTHON set "PYTHON=python"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_amadeus_companion.ps1" >NUL 2>&1
echo [Companion] starting Python backend and Tk card...
echo [Companion] There will be no Electron window.
echo [Companion] Close the card (right-click) to end the session.
echo.

"%PYTHON%" -m server.app --port 17777 --companion
set EXITCODE=%ERRORLEVEL%
echo.
echo [Companion] exited with code %EXITCODE%
pause