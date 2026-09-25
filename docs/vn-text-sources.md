# VN text sources (experimental)

VN Player accepts live displayed text through the existing `vn.line` method.
`AgentVNTextSource` and `LunaVNTextSource` translate their external transports into
that input; they do not choose reactions, alter VN memory, or interpret the story.
The shared runtime supports Base VN and Mystery VN independently of the source.
PARANORMASIGHT retains its established Mystery preset. Connecting another game's
text alone does not qualify its extraction quality or semantic behavior.

For your first game, start with the [Agent installation steps](#0xdc00-agent) or
the [LunaTranslator setup](#lunatranslator), then use **Save and test text**.
The [Chinese README](../README_ZH.md#vn-player-安装与首次取文) also has a quick start.

## Game profiles in VN Player

Use **Add game** to name a game, choose its type and select a text source. Agent
profiles select an installed Agent executable and the game's compatible Hook `.js`
script; Luna profiles use its original-text WebSocket endpoint. The native file picker
is available in the desktop app; absolute paths can also be entered directly.
Agent's installation path is shared by all profiles. Profiles are stored locally
in `.amadeus/vn-profiles.json` under the backend's project root, outside Git, with
atomic replacement on save. Amadeus does not download or modify third-party scripts.

**Save and test text** saves the settings and starts a capture session. Advance
the game and compare the latest 20 observations in the preview, including real
repetitions and option text. This preview uses the source adapter's normalized
input without calling the VN story runtime, loading a full script, or invoking
an LLM. Receiving text is not an automatic quality approval. **Test text capture**
can repeat this check for any saved profile, including PARANORMASIGHT.

**Start** uses the selected game's saved settings. Agent is automatically launched
with the game's current PID and Hook script. VN Player reuses a running game by
its exact executable path; if it is absent, the saved **Launch game if it is not
running** preference is represented by **Launch game with**: direct executable,
Steam, or **I will start the game**.
Multiple matching instances produce an explicit error. PID values are never
saved. Game startup is shared by both sources. For Steam, selecting the actual
game executable reads its matching local Steam manifest to fill the app ID;
ambiguous or absent manifests leave the ID as an explicit choice (demos have
their own IDs). VN Player opens the registered
`steam://rungameid/<id>` link, waits up to 60 seconds for the exact executable, then
connects the selected text source. It does not launch the exe with fabricated Steam environment values.
A running game is reused without reopening Steam. Missing Steam, a wrong ID/path,
or multiple matching processes fail visibly. Steam itself is never owned or closed.
Other launchers currently require starting the game externally.

**Edit game profile** updates the saved paths and launch preferences while the
session is stopped. **End session** disconnects capture and stops the Agent process
launched by VN Player. Closing a game is opt-in and applies only to the game
process owned by VN Player; externally started games remain open. Newly added
games default to Base companion mode without requiring a complete script. The
existing PARANORMASIGHT profile retains its Mystery preset. The developer's built-in
profile is offered only when that game is installed; a clean installation starts
with **Add your first game**. The editor groups game identity/type, text connection,
and optional play preferences. Agent download/script links use the official upstream
pages; script selection starts in the configured Agent's `data/scripts` directory.
Shared Agent setup is collapsed once configured. After previewing text, **Text looks
right — start companion** switches to play while keeping the game open. This button
is the user's quality check, not an automatic certification of extraction.

## Companion type and capabilities

`promptPack` selects `base` or `mystery` in a launch profile; it maps to the existing
runtime `prompt_pack`. Game type owns the following fixed capability presets;
the editor and expandable ability details display abilities without editable switches. The host
returns `capabilityPresets` so the renderer does not maintain a separate policy.

| Capability | General VN | Mystery VN | Prerequisites |
| --- | --- | --- | --- |
| `immediate` — commentary | On | On | A configured model for Base; existing rule path retained in Mystery |
| `interaction` — player questions | On | On | Active VN session; configured model for Base |
| `summary` — story summaries | On | On | Displayed history; neutral rule summary remains possible without a model |
| `retrospective` — reflection | On | On | Enough displayed history; Base needs its model lane |
| `lookahead` | Off | On | Complete script and verified alignment; Base also needs its model lane |
| `reasoning` — detective reasoning | Off | On | Mystery type |

Abilities outside a type do not run; input recording continues. Runtime status
reports requested, available and enabled states with a reason. Base does not infer
clues using PARANORMASIGHT keyword rules, and never implicitly loads its script.
An explicitly empty `scriptPath` also means text-only for a new Mystery game.
Unverified upstream IDs, ambiguous identical text and fuzzy matches do not establish
a usable lookahead position. Future script content remains planner-only.

Base enables the existing summary/reflection/lookahead model lanes by default
when those capabilities are requested; explicit environment/parameter model
disables still apply. Mystery retains its existing lane defaults. The existing
low-level runtime API retains capability overrides for isolated probes and existing
headless callers. Saved launch profiles no longer accept overrides; the launch
facade always derives them from the selected type. Saved profiles reject individual
capability maps. Type and voice preferences are retained; switching General →
Mystery enables the entire Mystery preset. The low-level `lookahead_enabled` input
alias remains supported for the existing replay/live runner, while serialized
profiles use only `capabilities.lookahead`. `status.llm.lookahead_llm_enabled`
identifies the model lane separately.

### Background summary and reflection

Immediate reactions still enter their model flow per line, in order, when enabled
(a context request can require a second model call). Model-backed
summary/reflection jobs run outside the text-ingress chain. Each lane has one
worker processing trigger-time snapshots in order; new triggers queue without
concurrent model calls in that lane. Base defaults trigger summaries every 12
lines and reflection from line 40, then every 40 lines. The trigger count is
reserved when queued so completion latency cannot shift those intervals.
Stop/restart cancels queued and in-flight jobs; late responses cannot update
another session. Unhandled job errors emit `vn.error` and do not stop the lane;
existing invalid-model-response handling is retained.

Model-backed summary/reflection no longer return in that line's synchronous
response: consume `vn.summary`/`vn.context.updated` or the context store. Offline
evaluators may call `wait_for_context_updates()` before reading a chunk snapshot.
Rule-based paths remain synchronous, preserving the existing Mystery replay.

### Session controls and activity

The VN page owns the current play controls. `commentaryFrequency` (`quiet`,
`balanced`, `frequent`) is a saved startup preference. Current
changes use `vn.mode.set` with `session_id`, `commentary_frequency`,
or `commentary_paused`. Normal frequency preserves the existing
runtime cadence; quiet/frequent adjust its line cooldown and per-minute ceiling.
Pausing spontaneous comments keeps story recording, summary work and direct
player questions active. A pending model comment is checked again before delivery.
The initial release has no independent VN mute preference. Replies use the configured
speech path; **Stop speech** interrupts current playback. Pausing spontaneous
comments still permits spoken answers to direct questions. ASR remains a separate
input control. Text replies remain visible in VN activity history; portrait
captions currently follow actual speech playback. Text-only portrait presentation
and in-window failure feedback are deferred to the follow-up D3 design.

The renderer reads production event shapes: `vn.line.line`,
`vn.reaction.reaction.speak`, `vn.summary.scene_summary` and `vn.player.event.event`.
Only spoken reactions appear as companion messages. Accepted player input is
published before its model answer. Runtime-owned event identities and sequence
numbers let clients merge current events with `vn.status {include_history: true}`
without dropping real repeated text or duplicating replayed presentation events.
The current runtime retains the most recent 200 visible activities; diagnostic
events have a separate UI buffer. This restores a page reconnect, not a promise
of semantic continuity across loading an earlier game save or restarting the host.

The page distinguishes startup, waiting for first text, following, interrupted
source, exited game and unavailable model. A source receiving no new text during
a menu or reading pause is not by itself an error. Lifecycle monitoring publishes
process exits even without new text. **Cancel connection** preempts startup on the
same WebSocket and cleans acquired resources. Steam may still finish opening a
game after its launch URI has been submitted; cancellation does not claim authority
over an as-yet-unidentified future game process.

### Voice, companion window and game view

The profile's `voiceInput` and `visionMode` (`off` / `on_question`) are startup
defaults. The live **This play session** controls change voice and vision
independently without editing the profile. Their state belongs to the backend VN
session, so reloading or navigating away from VN does not restart the microphone.
Stopping VN ends its own listener and resets the session input settings.

ASR requires the active session's player-interaction capability, including when
spoken text is routed as a note or pin. Each listener carries both a session ID
and an input generation. Disabling/restarting it rejects late recognition from
the previous generation. Session-specific stop cannot shut down another scene's
or a newer VN listener. **Stop speech** still uses the existing speech interrupt.

With **When I ask** enabled, both typed and spoken questions (ask/choice) capture
a fresh frame from the already bound game window and pass it to the existing VN
model completion. Notes/pins remain text-only. **Preview game view** displays a
preview; the next question captures again, so a stale preview is not sent as a
current game view. A capture failure is reported instead of silently answering a
visual question without an image. Late capture results cannot cross sessions or
revive disabled voice input.

VN controls do not consult or mutate the general visual capture policy, enable
general watching, or fall back to the desktop. The shared multimodal model/capture
implementation is reused, while the scene's input policy and capture target remain
separate. General Settings now states this scope explicitly. Images remain
transient question context, not persistent story facts. Luna may specify its game
executable for the same bounded capture.

**General vision (excluding VN)** is not a master switch for VN: General off / VN
on still captures for VN questions; General on / VN off does not enable VN
question capture. VN also owns its current image encoding defaults (960px long
edge, JPEG quality 68); General image sizing and quality apply only to its callers.

The present control surface is the VN page's always-visible session toolbar. The
existing companion presentation windows are not given an independent input state;
a later small-window control surface must use this same session API.

On Windows, VN vision uses Windows Graphics Capture for the exact verified game
HWND. It does not use a desktop crop: the earlier Pillow HWND implementation
included occluding windows. Capture runs in a short-lived hidden worker because
the native capture library faulted during a two-game acceptance run. A crash or
timeout becomes a capture error while the VN host stays alive; no desktop fallback
is allowed. `windows-capture` and its OpenCV dependency are pinned in `uv.lock` for
Windows; this adds no Agent/Luna dependency. A minimized, closed or unavailable
game must be restored/reconnected before capturing.

New games enable the portrait overlay by default when its helper is installed.
An explicitly saved off preference remains off. Text-capture-only tests still do
not launch the overlay. The window implementation is now entirely in this repository
(`tools/vn_portrait_overlay_lite.py` and `render/vn_overlay_window.py`); it does not
import the sibling workspace's Tk helper or portrait cropper. The optional
`companion-kurisu` art pack uses the existing asset-bundle installer described in
`docs/companion-panel.md`. Without that pack the window displays a simple avatar
and captions. An invalid installed pack reports an error rather than silently
substituting unrelated assets. **Show/Hide portrait** changes this session only;
incoming reactions do not reopen a hidden window.

Luna profiles save the original-text WebSocket URL. Start Luna, configure its
extraction; VN Player can launch the game through its shared exe/Steam launcher or
connect to a game started manually. Luna's service remains externally owned.
Luna extraction compatibility still needs validation for each game.

### API and verification

- `vn.launch.profiles` returns profiles, file availability, the shared `agentExe`, `overlayAvailable`, and `capabilityPresets`.
- `vn.launch.inspect` accepts `{gameExe}` and returns an unambiguous installed
  Steam app ID/name when its local manifest identifies that executable's directory.
- `vn.launch.profile.save` accepts `{profile: {name, gameExe, hookHelper, ...}, agentExe}`;
  omit `profile.id` to create, or supply an existing ID to edit. `promptPack`,
  `voiceInput`, `visionMode`, `commentaryFrequency` and overlay preferences configure the companion. `launchMethod`
  (`exe` or `steam`), `steamAppId` and `launchGame` configure startup. Live process IDs and
  arbitrary runtime implementation fields are not editable profile fields.
- `vn.launch.start` accepts `{profileId}` to use saved settings, or
  `{profileId, captureOnly: true}` to test extraction without companion responses.
- `vn.launch.status` includes `captureOnly`, `capturedLines` and source connection
  status. `vn.launch.stop` uses the saved game-close preference unless overridden.
- `vn.input.set` accepts the active `session_id` plus `voice`, `vision_mode` or
  input `kind`; it returns enriched `vn.status` including `inputs`. Changes are
  broadcast through the existing `vn.status` event.
- `vn.mode.set` changes current output preferences; `vn.launch.overlay` accepts
  the active `session_id` plus boolean `enabled` to show/hide the window.
- `vn.launch.capture` returns a transient `visual_context` for explicit previews
  or low-level question attachment. Normal typed/ASR requests acquire their fresh
  game frame at the shared VN player handler according to `vision_mode`.

Run `python -m pytest tests/test_vn_profiles.py tests/test_vn_text_sources.py` for
storage, source, process ownership and runtime isolation checks. For a browser
acceptance run, start Vite in `electron`, then run
`python tools/probes/verify_vn_profiles_ui.py --url http://127.0.0.1:5173`.
The UI probe uses the real form, handlers, store, runtime and emitted events in an
isolated temporary directory. Process injection, file picking, microphone, images
and model output are substituted. It does not replace real game, microphone or
model acceptance. `tests/test_vn_product_contracts.py` separately exercises real
loopback Agent and Luna streams plus direct recorded input against the same runtime.

## 0xDC00 Agent

These steps cover native Windows PC games. This branch's real-game checks used
Agent v0.1.4; a script's existence does not guarantee compatibility with every
game update, language patch, demo, or storefront edition.

### Install the extractor and scripts

1. Download the Windows archive from [Agent's official releases](https://github.com/0xDC00/agent/releases)
   and extract it completely to a stable folder. Select the extracted `agent.exe`
   later; keep its supporting files and `data` directory with it.
2. Open Agent. Its script selector's **update scripts** action synchronizes the
   [official script repository](https://github.com/0xDC00/scripts) into `data/scripts`.
   For manual installation, use the repository's **Code → Download ZIP**, extract
   it, and place its contents directly under `data/scripts`, preserving subfolders.
   Avoid an extra `data/scripts/scripts-main` nesting level. Keep any custom scripts
   separately backed up when updating.
3. Search the collection for the game and read the script's header and instructions.
   Match the PC version you actually run; a script for an emulated console edition
   is not interchangeable. Select the game's `.js`, not a shared `lib*.js` file.
   Preserve its dependencies: for example, the upstream
   [Kemono Teatime script](https://github.com/0xDC00/scripts/blob/main/PC_Steam_Unity_Kemono_Teatime.js)
   imports `libUI.js` and `libMono.js` from the same directory.

Illustrative layout; the game script's filename depends on the game:

```text
Agent/
  agent.exe
  ...other files from the Agent archive...
  data/
    scripts/
      PC_Your_Game.js
      libMono.js
      libUI.js
      ...other repository files and subfolders...
```

The [upstream Agent guide](https://github.com/0xDC00/agent#quickstart) describes its
folder layout. No separate hook DLL needs to be selected in Amadeus's Agent profile;
Agent loads the selected extraction script. If no compatible script exists, this
route cannot extract that game's text merely by selecting its executable.

### Check extraction and WebSocket output

Start the game, select its running process and the matching script in Agent, and
use **Attach**. Advance dialogue and confirm Agent displays the expected text.
In Agent's settings, enable **WebSocket** and use host `127.0.0.1`, port `9001`.
The ordinary desktop profile connects to `ws://127.0.0.1:9001`; if you previously
customized Agent's endpoint, restore these values for this walkthrough. The profile
editor currently has no Agent host/port field. Clipboard output is not an input to
Amadeus and does not substitute for WebSocket output.

Close the manually opened Agent before starting a VN Player session; Amadeus launches
its own Agent instance. The game can stay open. In **VN Player → Add game**, fill in:

| Field | What to select |
| --- | --- |
| **Game executable** | The actual game process's `.exe`, not `steam.exe` or a launcher shortcut |
| **Game hook script (.js)** | The compatible extraction script in `Agent/data/scripts` |
| **Agent installation (shared by all games)** | The extracted `agent.exe`; configure once for your game profiles |
| **Launch game with** | Direct executable, **Steam**, or **I will start the game**; Steam demos need their own app ID |
| **Full script for alignment** | Optional story text for alignment/lookahead; leave empty unless you have a supported script file. Do not put the hook `.js` here |

Choose **General VN** or **Mystery VN** for the companion's behavior; this does not
choose or install an extraction script. Click **Save and test text**, advance several
lines, and compare dialogue, choices, and repeated text with the game. The preview
does not invoke a model. Then use **Text looks right — start companion**. For model
replies, configure the model connection selected by **VN companion** in Settings
(DeepSeek or OpenAI-compatible), applying backend restarts when prompted. Later,
**Start** reuses the saved profile and attaches Agent automatically.

Amadeus does not install, update, or redistribute Agent, game scripts, or game files.
Use **Add game** with your own paths; you do not need the developer's directory
layout. The optional built-in PARANORMASIGHT profile is offered only when its
separately installed game is found under the sibling `visual novel player` directory.

### Adapter contract

The Agent adapter optionally launches the installed executable with its script
and consumes `copyText` messages from its WebSocket. The initial release supports
WebSocket only: startup failure or disconnection reports the connection state and
retries the same endpoint, without reading the system clipboard. Clipboard and
hybrid modes are rejected explicitly. A future clipboard source would require an
explicit user selection and its own input boundary. Plain text is valid. A modified
game script may additionally supply `speaker` and `script_id` in JSON. The adapter
passes them through `vn.line` without interpreting their story meaning.

The adapter owns only the Agent process it starts. If a matching Agent process
is already running, close it before asking Amadeus to launch another; Amadeus
does not terminate externally started Agent processes.

## LunaTranslator

Amadeus only needs the extracted original text from Luna's hook workflow; it
does not consume translated text or control Luna's other features. The current adapter
connects to the network service hosted by the running LunaTranslator app.
LunaHook can be used separately, but a standalone LunaHook setup would need a
host/output bridge for Amadeus; it does not use this WebSocket adapter as-is.

1. Install LunaTranslator using its [official download guide](https://docs.lunatranslator.org/en/README.html).
   Start the game and follow Luna's [HOOK setup](https://docs.lunatranslator.org/en/basicuse.html)
   to select the process and a text stream that matches the displayed dialogue.
   Confirm extraction works in Luna first. Agent's `.js` scripts are not needed here.
2. Enable [Luna's network service](https://docs.lunatranslator.org/en/apiservice.html)
   and note its configured port. Keep Luna running during the VN session.
3. In **VN Player → Add game**, choose **Luna original text (experimental)** and enter
   `ws://127.0.0.1:<port>/api/ws/text/origin`, replacing `<port>` with Luna's actual
   service port. Use the original-text endpoint, not `/api/ws/text/trans` or a web-page URL.
4. Choose **I will start the game** if it is already managed externally, or configure
   the shared exe/Steam launcher. Use **Save and test text** and compare several
   lines before starting the companion.

Amadeus connects to Luna's already-running original-text stream. It does not launch
Luna, select hooks, change Luna settings, or consume translations.
The original-text stream supplies text; speaker, script ID, choices, and scene
metadata are not assumed. Repeated text is forwarded as repeated observations.

When using the API, first save a Luna profile with `textSource: "luna"`,
`lunaWsUrl: "ws://127.0.0.1:<configured-port>/api/ws/text/origin"`, and the intended
game launch preference. Pass its `profileId` to `vn.launch.start`. The port comes
from the user's Luna network-service configuration. Stop with `vn.launch.stop`;
stopping disconnects Amadeus without stopping Luna.

## Setup troubleshooting

Open **Connection details and diagnostics** in VN Player to see game, text-source,
and bridge status. A connected bridge alone does not establish that extraction works.

| Symptom | Check |
| --- | --- |
| Agent reports a missing `lib*.js` module | Update/install the complete script collection and preserve its relative layout, rather than copying only the game `.js` |
| Agent already running / port already used | Close the Agent opened for manual testing and retry. Amadeus does not terminate an externally started Agent |
| Agent captures text, but VN Player stays connecting or waiting | Enable Agent's WebSocket output at `127.0.0.1:9001`; clipboard copying is not enough. Check the bridge error shown in diagnostics |
| Bridge connects, but advancing dialogue produces no text, or text is wrong | Test the same process/script in Agent or the selected text stream in Luna. Check game version, language patch, and script compatibility; Amadeus cannot repair extraction by changing the companion model |
| Direct exe launch fails for a Steam game | Select **Steam**, confirm the actual game executable and app ID, or start it in Steam first and choose **I will start the game** |
| Luna fails to connect | Keep Luna and its network service running; check the port and exact `/api/ws/text/origin` path |
| Text preview works, but model replies are unavailable | Configure the connection used by **VN companion** in Settings. Capture-only testing intentionally does not produce companion replies |

## Current boundary

`vn.line` remains the only live VN text input. Nonempty `text` is required;
`speaker` and `script_id` are optional. The source adapters report connection
state and the most recent text preview through `vn.launch.status`.

The adapters never deduplicate by text or by an unverified Agent message ID. A
text-only stream cannot reliably distinguish a replay from a real repeated line;
both are forwarded, including after a WebSocket reconnect. Agent uses one transport
and does not switch to another input source when disconnected. Source-specific
replay suppression can be added when the external transport supplies a verified
event identity.

The VN runtime preserves repeated observations even when they share a script ID.
Branch revisits are legitimate input. Transport replay suppression must be owned
by a source adapter with verified event identity; script IDs are not that identity.
