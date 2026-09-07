# Experimental Companion prototype

This branch is a source snapshot for [Issue #61](https://github.com/Code-Amadeus/Amadeus/issues/61), intended for inspection and further design discussion. It is not a request to merge the entire prototype into the supported product. The branch starts from upstream `afe0e74552be5faf9041d6f488d9e87b329cf8e3`; it does not include the contributor's earlier local development commits.

## Windows quick start: connect local Codex tasks

The experimental connection is **read-only observation of local Codex records**. Use the same Windows user that runs Codex, with at least one local task already created. No Companion plugin, Codex API key or Codex SDK service needs to be configured for observation. Amadeus model/voice credentials are a separate requirement for optional narration.

1. After the experimental branch has been published, clone it into a new directory. Install Git, uv, Python and Node.js as described in the [README](../README.md). These commands prepare core plus development tools, without optional voice/model packages:

   ```powershell
   git clone --single-branch --branch codex/companion-prototype https://github.com/Code-Amadeus/Amadeus.git Amadeus-companion
   cd Amadeus-companion
   uv sync --locked --extra dev --python 3.12.10
   Push-Location electron
   npm ci
   npm run build
   Pop-Location
   ```

   For an existing installation with optional packages, preserve its full [installation profile](install_profiles.md) when running `uv sync`; selecting only `dev` removes unselected voice/model packages.

2. Close existing Amadeus windows and its backend before changing the source settings. In the same PowerShell session, at the clone root, choose the Codex data directory. An existing `CODEX_HOME` is respected; otherwise the adapter uses `.codex` in the current user's home. If Codex uses a custom directory, set `CODEX_HOME` to that existing directory in this session before continuing. Do not create an empty replacement directory.

   To find a task UUID, the following optional command reads the latest 20 unarchived task IDs and names from the local database. Inspect its output locally; task names may contain private information.

   ```powershell
   @'
   import os, sqlite3
   from pathlib import Path
   codex_data = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
   with sqlite3.connect((codex_data / "state_5.sqlite").as_uri() + "?mode=ro", uri=True) as db:
       for task_id, name in db.execute("SELECT id, name FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT 20"):
           print(task_id, name or "(unnamed task)")
   '@ | & .\.venv\Scripts\python.exe -X utf8 -
   ```

3. Select the observation scope using **one** of the following commands. Paste UUIDs from the local list for a narrow scope:

   ```powershell
   $env:AMADEUS_COMPANION_CODEX_THREADS = Read-Host 'Paste one task UUID, or comma-separated task UUIDs'
   ```

   Alternatively, explicitly allow all supported local tasks across projects:

   ```powershell
   $env:AMADEUS_COMPANION_CODEX_THREADS = '*'
   ```

4. Launch from this same PowerShell session so both Electron and the backend inherit the scope. Changing only a backend `.env` does not configure Electron's source-navigation allowlist.

   ```powershell
   $env:NODE_ENV = 'production'
   $env:AMADEUS_PYTHON = (Resolve-Path '.\.venv\Scripts\python.exe').Path
   Push-Location electron
   npx --no-install electron . --floating-companion
   Pop-Location
   ```

5. Run or continue a selected task in Codex. Recent task cards should appear in Companion; open a card to read its current progress/result. `打开 Codex` uses the installed application's `codex://threads/<UUID>` handler. `知道了` acknowledges the Amadeus reminder; questions and approvals are still answered in Codex. Local observation does not start model requests on its own.

6. To reproduce the character and speech from the demo, install the optional visual/character assets and a suitable voice profile using the [README](../README.md) and [installation profiles](install_profiles.md). Configure the model service in **Settings → Models** and voice in **Settings → Voice**, verify ordinary Amadeus speech, then enable the Companion character toolbar's voice control. A fresh profile keeps reminders muted. The model and TTS configuration is needed in addition to enabling the observer; the source clone alone does not contain the demo's assets or weights.

PowerShell `$env:` changes above apply to processes launched from that session. A later launch from a new terminal needs the same settings, or a private local launcher that sets them. After changing the data directory/scope, fully restart Amadeus and its backend so an existing Electron instance does not retain the previous environment.

### If no task card or speech appears

| Symptom | Check |
| --- | --- |
| Companion window does not open | Use the sidebar Companion button or `--floating-companion`; startup is intentionally off by default. With no secondary display it uses a compact primary-screen window |
| Window is visible but has no tasks | Confirm a nonempty scope in the launch session, a valid UUID, the same Windows user/data directory, and a full application/backend restart. Continue a selected task; old inactive cards expire after eight hours |
| Database missing, task absent or schema error | The current adapter expects `state_5.sqlite`, `threads` and readable rollout JSONL paths. Check the chosen existing Codex data directory. The adapter uses internal formats, so another Codex version may require adaptation; do not rename databases to force compatibility |
| `打开 Codex` does nothing | Confirm the local Codex application is installed and its `codex://` URL handler works; the selected task must also be within Electron's launch-time scope |
| Cards work but reminders are silent | Check the toolbar voice toggle, Models/Voice configuration, installed voice packages/assets and playback device. Ordinary progress does not speak, and old pre-connection results are not replayed as fresh announcements. Active main-chat or protected audio can defer a reminder |
| Character is absent | Install the existing visual runtime and character bundle; core task observation is separate from character/model installation |

## Purpose and interaction

The Companion keeps background tasks visible beside the character while the user works elsewhere. Projects group their tasks; selecting a project spreads its cards, and selecting a task opens a reading surface with Markdown results, public progress, and explicitly identified child tasks. Other projects remain visible in miniature.

Cards distinguish running, attention needed, and a result available for the current turn. “知道了” acknowledges only the local reminder. It does not answer a question, grant approval, change the source's read state, or dismiss the result. Closing a card hides it until new activity. Cards expire after eight hours without source activity; reading does not manufacture activity. The queue supports at most one reminder after ten minutes, but the experimental Codex source currently sets `repeatable=false` because it lacks reliable read and approval lifecycle information.

The renderer already consumes existing Amadeus Provider events as well as optional external task snapshots. Keeping the experience independent of any one source or Provider is a design objective; the current prototype still has Codex-specific fields and source navigation, so this separation is not presented as finished.

## Run the source

Use the upstream setup documented in the README. The current Windows CI installs CPython 3.12.10, Node.js 22.21.1 and uv 0.12.8. A CPU/model-less inspection environment is:

```powershell
uv sync --locked --extra dev
cd electron
npm ci
npm run build
```

Install optional character/model/voice packages through the project's existing setup if you want to see the character or hear speech. No character textures, model weights, reference recordings, `.env`, user settings, or runtime logs are included in this contribution. Tests do not require those packages.

From `electron`, start the desktop in production mode:

```powershell
$env:NODE_ENV = 'production'
$env:AMADEUS_PYTHON = (Resolve-Path '..\.venv\Scripts\python.exe').Path
npx electron .
```

The Companion is **off at startup by default**. Use the sidebar's **Companion** button to open it explicitly, or launch with `npx electron . --floating-companion`. `AMADEUS_FLOATING_COMPANION=1` opts into automatic creation; `--no-floating-companion` suppresses it for one launch. Opening or closing the window is separate from enabling an external data source.

The main chat uses the primary screen as an ordinary window. The Companion uses the first secondary display's work area, falling back to a compact window on the primary screen if unavailable. Optional placement variables are `AMADEUS_MAIN_DISPLAY` (`primary`, `secondary`, or a numeric display ID), `AMADEUS_MAIN_FULLSCREEN=1`, and `AMADEUS_COMPANION_DISPLAY` (`primary`, `secondary`, or a numeric display ID). Wallpaper and Companion are mutually exclusive character surfaces.

### Single-screen and display changes

- A second monitor is optional. On one screen, Companion opens near the bottom-right of the current work area, normally at most 480 × 820 logical pixels, shrinking for smaller areas. The taskbar is excluded from that area.
- Display removal, addition and metrics changes recompute placement from the current display list. A missing preferred secondary falls back to an available display; with only the primary left, it becomes the compact overlay. Reconnecting a preferred display allows it to be selected again.
- Coordinates are not fixed to the contributor's monitor. Displays to the left/above the primary can have negative coordinates. Electron supplies logical work-area dimensions, so high scaling is handled as a smaller available area.
- Both initial window size limits and limits during display changes are constrained to the selected area. The usual minimum size must not prevent the window from fitting after a move to a smaller screen.
- To keep Companion on the primary even when a second screen is connected, set `$env:AMADEUS_COMPANION_DISPLAY = 'primary'` in the launch session. Otherwise its default preference is `secondary`.

Placement tests cover single-screen sizes, taskbar offsets, negative coordinates and simulated disconnect/reconnect. Physical monitor unplugging and visual readability at every scale remain separate acceptance checks; a window fitting on screen does not by itself prove ideal card density on a small display.

## External Codex data is opt-in

With `AMADEUS_COMPANION_CODEX_THREADS` unset or empty, the backend does not construct the Codex observer and does not read its database or rollout files. Internal Provider task display remains available.

To inspect selected local tasks, set a comma-separated list of task UUIDs before launching. To explicitly allow discovery of all supported local tasks:

```powershell
$env:AMADEUS_COMPANION_CODEX_THREADS = '*'
npx electron . --floating-companion
```

The adapter reads the user's `CODEX_HOME`, or the default `.codex` directory in their home, using SQLite and JSONL. It uses local task/project identity and public events; it does not scrape the screen or answer for the user. `*` covers supported local records across projects and should only be used when that scope is intended. Changing this environment variable requires restarting the application/backend. Clear it to disable access:

```powershell
Remove-Item Env:AMADEUS_COMPANION_CODEX_THREADS -ErrorAction SilentlyContinue
```

Reminder speech is **off for a fresh Companion profile**. Enable it using the character toolbar's voice control. Its choice persists in the Electron profile. When enabled, notification text and project/provider labels are passed to the configured model service for Japanese character expression and then to the existing TTS pipeline. Choose the data scope with that disclosure in mind. Ordinary progress updates do not trigger speech. The main chat takes priority over reminder speech; muting reminders stops the current reminder and does not mute normal chat.

Windows audio-session observation begins only after a Companion client requests speech/audio status. It examines session/process metadata, not audio recordings. Real Codex/Zoom/Tencent Meeting avoidance still needs separate end-to-end qualification.

## Implementation map

| Owning layer | Main files and responsibility |
| --- | --- |
| Electron main/preload | `electron/src/main/index.ts`, `windowPlacement.ts`, `preload/companion.cts`: separate window, display changes, restricted source navigation and hit regions |
| Task projection | `floatingCompanionState.ts`: existing Provider events and external snapshots projected into presentation state |
| Cards and reading | `CompanionTaskConstellation.tsx`, `companionConstellationLayout.ts`, `companion.css`: grouping, focus, Markdown and identified child tasks |
| Reminder scheduling | `companionNotificationQueue.ts`, `useCompanionSpeech.ts`: card retention, local acknowledgement, FIFO speech, mute and retry boundaries |
| Backend reminder delivery | `server/handlers/companion_handler.py`: one low-priority utterance at a time, cancellation and real playback receipts |
| External source adapter | `server/codex_desktop_observer.py`, `handlers/codex_observer_handler.py`: opt-in read-only Codex record observation |
| Existing voice integration | `server/vn_tts_bridge.py`, `tts/`, `llm/prompts.py`, `SpokenCaption.tsx`: character expression, bounded streaming generation, playback text and captions |

The source still owns task lifecycle and execution authority. Translated narration is presentation rather than completion or permission evidence. The prototype reuses the existing chat, Provider runtime, authenticated backend, render bridge, and TTS; it does not introduce a second execution host.

## Inspect without personal tasks

`npm run companion:preview` builds the production renderer and opens a development fixture on a secondary display. By default it uses fictional projects and does not make model/TTS calls. It requires an installed SpriteForge character runtime; set `AMADEUS_PREVIEW_CHARACTER_ROOT` to the chosen runtime directory if needed. Number keys 0–3 change basic scenes, P toggles preview click-through for automation, S saves a local screenshot, and Ctrl+Escape exits. Preview output stays under ignored `runtime/companion-preview`.

The explicit `--codex-thread <UUID>` preview option reads that real task; it is not a privacy-safe fixture. The preview server is loopback-only development tooling and does not use the authenticated production transport. Do not expose it on a network interface.

`electron/tools/companion-translation-check.cjs` is optional model-backed diagnostics. It uses existing settings and may incur the configured provider's cost; it is not run by deterministic validation. Its enqueue mode checks submission rather than proving audible playback.

The public demo attached to Issue #61 used the production UI, fictional source events and actual Amadeus speech. It demonstrates interaction and playback, not full external observer integration, normal-window click-through, every approval lifecycle, or cross-platform compatibility. Its recording harness and raw recordings are not included here.

## Current limits and review priorities

- External observation relies on Codex's internal local formats, not a public Desktop event subscription API. Cloud/remote tasks, ephemeral conversations, complete system approvals, and reliable source read state are not covered.
- Question extraction currently keeps the first question's text; separately structured options and further questions are incomplete. Source navigation remains necessary for decisions.
- The reading surface is a bounded recent view, not a complete conversation archive. “Result available” describes a turn, not necessarily a completed project.
- Windows dual-display behavior is the locally exercised path. Other screen arrangements, DPI values and platforms need qualification. macOS upstream changes are retained but not locally verified.
- The UI hierarchy, card density, attention states and transition into detailed reading remain design questions. Provider-neutral source navigation and presentation contracts need further discussion.
- The snapshot includes supporting Python discovery, render/caption and streaming TTS changes because the prototype uses them. These should be assessed for separate, bounded PRs if contribution proceeds.

Start review with the opt-in boundaries and task projection, then UI behavior, then voice delivery. Agreement on this experimental branch does not imply approval of its current defaults, contracts, or UI for mainline.

## Validation

Current preparation results are recorded in `docs/companion_validation.md`. Run the repository's current CI commands rather than treating the earlier demonstration's tests as proof for this branch. Full model-less validation does not send a chat request or load optional voice/character packages.
