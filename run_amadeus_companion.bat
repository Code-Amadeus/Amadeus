@echo off
rem Companion launch from an ordinary desktop session.
rem
rem Runs the built Electron main process directly (electron/dist) instead of the
rem Vite dev server, so it starts faster and has one less moving part.
rem Do NOT launch this from inside a sandboxed/agent terminal: Electron exits
rem with 0x80000003 there before its JavaScript starts.
rem
rem Companion owns no wallpaper host, no Slice, no Canvas and no visible
rem main window. The backend starts the repository-owned Tk Companion card and
rem keeps the existing session, context, ASR/TTS and subtitle contracts.

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
set AMADEUS_COMPANION=1
set AMADEUS_VISION_ENABLED=1
set AMADEUS_VISION_MODE=on_demand
set AMADEUS_VISION_SCOPE=read_window
set AMADEUS_VISION_PROVIDER=qwen

cd /d "%~dp0"

if not exist "electron\dist\main\index.js" (
  echo [Companion] electron\dist is missing; building once with npm...
  pushd electron
  call npm run build
  popd
  if not exist "electron\dist\main\index.js" (
    echo [Companion] build failed. Run "npm run build" inside electron\ and retry.
    pause
    exit /b 1
  )
)

echo [Companion] starting... There is no Amadeus window by design; the Tk
echo [Companion] Companion card is the visible surface.
echo [Companion] Close the card (right-click) to end the session.
echo.

"electron\node_modules\electron\dist\electron.exe" "%~dp0electron" --companion
set EXITCODE=%ERRORLEVEL%
echo.
echo [Companion] exited with code %EXITCODE%  (0x80000003 means a sandboxed terminal)
pause
