# VN text sources (experimental)

VN Player accepts live displayed text through the existing `vn.line` method.
`AgentVNTextSource` and `LunaVNTextSource` translate their external transports into
that input; they do not choose reactions, alter VN memory, or interpret the story.
The current VN runtime and game profile remain PARANORMASIGHT-oriented. Connecting
another game's text does not yet qualify its semantic behavior.

## Game profiles in VN Player

Use **Add game** to name a game and select its executable, an installed Agent
executable, and the game's compatible Hook `.js` script. The native file picker
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
running** setting decides whether to launch it or ask the user to start it.
Multiple matching instances produce an explicit error. PID values are never
saved. If a launcher starts a different executable, select the actual game
executable and start the game externally before attaching.

**Edit game profile** updates the saved paths and launch preferences while the
session is stopped. **Stop** disconnects capture and stops the Agent process
launched by VN Player. Closing a game is opt-in and applies only to the game
process owned by VN Player; externally started games remain open. Newly added
games currently start in capture mode. The existing PARANORMASIGHT profile retains
its companion runtime; its full-script and overlay settings are under the editor's
**Existing companion settings**. These runtime semantics are not inherited by
new games.

Luna profiles save the original-text WebSocket URL. Start Luna, configure its
extraction and start the game externally; VN Player then connects using the saved
URL. Luna extraction compatibility still needs validation for each game.

### API and verification

- `vn.launch.profiles` returns profiles, file availability and the shared `agentExe`.
- `vn.launch.profile.save` accepts `{profile: {name, gameExe, hookHelper, ...}, agentExe}`;
  omit `profile.id` to create, or supply an existing ID to edit. Runtime presets
  and live process IDs are not editable profile fields.
- `vn.launch.start` accepts `{profileId}` to use saved settings, or
  `{profileId, captureOnly: true}` to test extraction without companion responses.
- `vn.launch.status` includes `captureOnly`, `capturedLines` and source connection
  status. `vn.launch.stop` uses the saved game-close preference unless overridden.

Run `python -m pytest tests/test_vn_profiles.py tests/test_vn_text_sources.py` for
storage, source, process ownership and runtime isolation checks. For a browser
acceptance run, start Vite in `electron`, then run
`python tools/probes/verify_vn_profiles_ui.py --url http://127.0.0.1:5173`.
The UI probe uses the real form, handler, store and adapter in an isolated temporary
directory, substituting process injection and the native file picker. It does
not replace a real game's extraction acceptance.

## 0xDC00 Agent

Install [0xDC00 Agent](https://github.com/0xDC00/agent/releases) and select a
compatible script from its [script collection](https://github.com/0xDC00/scripts)
before connecting Amadeus. First confirm that Agent captures the game's text on
its own. The PARANORMASIGHT launch profile still looks for the separately installed
`visual novel player` directory beside the Amadeus checkout. It uses the local
Agent executable and game script from that directory. Amadeus does not install,
update, or redistribute Agent or game files.

The Agent adapter optionally launches the installed executable with its script,
consumes `copyText` messages from its WebSocket, and uses the existing clipboard
fallback when that connection is unavailable. Plain text is valid. A modified
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

Configure text extraction and enable [LunaTranslator's network service](https://docs.lunatranslator.org/en/apiservice.html) in Luna.
In VN Player, add or edit a game profile, select **Luna original text (experimental)**,
enter the WebSocket URL shown by Luna with the path `/api/ws/text/origin`, save,
and start the session. Amadeus connects to Luna's already-running original-text stream. It does
not launch Luna, select hooks, change Luna settings, or consume translations.
The original-text stream supplies text; speaker, script ID, choices, and scene
metadata are not assumed. Repeated text is forwarded as repeated observations.

When using the `vn.launch.start` API directly, pass `textSource: "luna"`,
`lunaWsUrl: "ws://127.0.0.1:<configured-port>/api/ws/text/origin"`, and
`launchOverlay: false`. The port comes from the user's Luna network-service
configuration. Stop with `vn.launch.stop`; stopping disconnects Amadeus without
stopping Luna.

## Current boundary

`vn.line` remains the only live VN text input. Nonempty `text` is required;
`speaker` and `script_id` are optional. The source adapters report connection
state and the most recent text preview through `vn.launch.status`.

The adapters never deduplicate by text or by an unverified Agent message ID. A
text-only stream cannot reliably distinguish a replay from a real repeated line;
both are forwarded. Agent hybrid mode keeps only one live transport active at a
time, avoiding routine WebSocket/clipboard double delivery. Source-specific
replay suppression can be added when the external transport supplies a verified
event identity.

The current PARANORMASIGHT VN runtime still ignores a second observation with
the same `script_id` during one session. That is a VN semantic limitation, not
transport deduplication: revisiting a branch may legitimately show the same
script line again. Keep source acceptance and runtime behavior as separate
validation results until that rule is revised.
