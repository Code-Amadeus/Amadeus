# Companion-only startup — 0.15.x

Companion-only is a startup mode, not a wallpaper modifier. One launch owns
exactly one visible surface: the repository-owned Tk Companion card in
`render/vn_overlay_window.py`, launched through
`tools/vn_portrait_overlay_lite.py`. It reuses the shared session, context,
Qwen3-ASR, GPT-SoVITS, subtitles and interruption behavior; it never starts the
wallpaper host, the Electron Slice, the Canvas surface, or a visible main UI.

## Entry points

| Surface | Command |
|---|---|
| Desktop launcher | `run_amadeus_companion.bat` (runs the built `electron/dist`, no Vite) |
| Electron dev | `electron . --companion` (or `AMADEUS_COMPANION=1`) |
| Backend only | `python -m server.app --port 17777 --companion` |
| Environment | `AMADEUS_COMPANION=1` |

`run_amadeus_companion.bat` uses the built main process, so it starts faster and
does not need a Vite dev server. It builds once if `electron/dist` is missing.
The dev path (`npm run electron:dev`) still works and additionally serves the
renderer from Vite.

Launch it from an ordinary desktop session — a shortcut, Explorer, or a normal
terminal. **Do not launch it from inside a sandboxed or agent terminal**:
Electron exits there with `0x80000003` before its JavaScript starts, and a
three-line minimal Electron app fails the same way, so it is the terminal, not
this application. Verify the environment with
`electron\node_modules\electron\dist\electron.exe --version`, which prints
`v44.x.x` on a working session.

`--companion` / `AMADEUS_COMPANION=1` resolve to `StartupMode = 'companion'` in
`electron/src/main/startupMode.ts`. Companion mode is checked before the Windows
wallpaper preference, so an enabled wallpaper setting can never pull the
wallpaper host into a companion launch. `--wallpaper` and
`AMADEUS_WINDOWS_STARTUP_MODE` keep their existing meaning for every other mode.

## Prerequisites for a fresh worktree

A git worktree only checks out tracked files, so a new companion worktree needs
two things that are deliberately not tracked:

- **`.env`** — `config/settings.py` reads the project `.env`, and the backend
  needs it for model and voice settings. It is gitignored, so copy it in (for
  example from the main checkout) before the first launch. Without it the
  backend fails on incomplete `AMADEUS_BACKEND_AUTH_MODE` /
  `AMADEUS_BACKEND_TOKEN` / `AMADEUS_BACKEND_INSTANCE_NONCE` configuration.
- **`assets/companion/`** — the optional `companion-kurisu` Lite pack that the
  card renders (or at least `assets/companion/kurisu/manifest.json`). The card
  still opens and shows captions without it, but the portrait stays on the
  text/avatar fallback. The installed pack ships through
  `python tools/external_assets.py install <bundle.zip>`; it is never in git.

## What companion mode skips

- `windowsWallpaper.start()` — the wallpaper host is never created
  (`STARTUP_MODE === 'wallpaper'` gates the session and the tray).
- `createElectronSliceWindow()` and `createElectronCanvasWindow()` — both return
  early, and the `electron-slice.open` IPC refuses before it can reach them.
- `createWorkOverlayWindow()` — returns early, so an explicit `--work-overlay`
  cannot add a Work panel to a companion launch.
- The visible main window — it is still created as a hidden bridge client so the
  Companion bridge descriptor and back-end session stay intact. On a successful
  backend start it stays hidden; if the backend fails to start, the launch shows
  it anyway, because a companion launch with no backend has no surface at all
  and the failure must be visible instead of silently hidden.
- Work, tools, Provider, permission and AUIP context — the backend runs with
  `--companion`, which turns off the cooperative/Work-planner lane and AUIP
  narration. Ordinary chat therefore creates no WorkItem. The session store,
  conversation history and context assembly are untouched.

## Card ownership and lifecycle

The backend owns the card process (`server/companion_runtime.py`):

- `CompanionCardHost.ensure_running()` probes port 8788 first. A card that is
  already serving is adopted as the presentation surface, so a repeated start
  never creates a second card.
- Otherwise the host spawns `tools/vn_portrait_overlay_lite.py` with
  `--on-close card-close`, waits for `/health`, and publishes its `/reaction`
  endpoint as the speech surface.
- `set_visible(false/true)` drives the card's own `/visibility` endpoint and
  `focus()` drives `/focus`; the tray in `electron/src/main/companionTray.ts` is
  the only visible control for a hidden main window.
- The card's audio boundary events still arrive through the shared TTS bridge:
  `server/vn_tts_bridge.py` falls back to the published card endpoint when a
  speech payload names no session overlay. An explicit `overlay_url` from a VN
  or reading session always wins.

Closing the card (right-click, or the Tk window close) posts
`/companion/card-close`. The owning backend stops the card and exits, which
stops Electron and releases the TTS/ASR resources it owns. If the backend was
already running before this Electron launch, Electron adopts it and exits
without stopping it, so a DSH/desktop backend is never killed by a companion
card close.

## Single instance

`app.requestSingleInstanceLock()` still owns process identity. A second
companion launch does not create a second card: the first instance receives
`second-instance` and calls `/companion/card/focus`, which shows and raises the
card it already owns.

## Verification commands

```powershell
# Electron launch-mode and startup-surface contracts
cd electron
node --experimental-strip-types --test tests/startupMode.test.mjs tests/startupWindowAccess.test.mjs

# Full desktop suite
node --experimental-strip-types --test tests/*.test.mjs

# Type check
node .\node_modules\typescript\bin\tsc --noEmit

# Companion routing, credential hand-off and close ownership
python -m pytest tests/test_companion_only_runtime.py tests/test_companion_card_close.py -q
```

Manual acceptance:

1. Start `run_amadeus_companion.bat`. Task Manager shows no `wallpaper64.exe`
   and no `webwallpaper64.exe`; no Slice, Canvas or main window appears.
2. The Tk card is visible, topmost, draggable, and its caption follows speech.
3. Speak into the microphone: ASR input, the LLM reply, TTS playback, subtitles
   and barge-in interruption behave as in the desktop app.
4. Ordinary chat creates no WorkItem in `runtime/work_ledger.sqlite3`.
5. Close the card. No orphan `python.exe` or `Electron` process remains.
6. Launch a second instance while the first runs. Only one card exists; the
   second launch surfaces the existing one.
