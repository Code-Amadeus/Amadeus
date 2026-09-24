# VN text sources (experimental)

VN Player accepts live displayed text through the existing `vn.line` method.
`AgentVNTextSource` and `LunaVNTextSource` translate their external transports into
that input; they do not choose reactions, alter VN memory, or interpret the story.
The shared runtime supports Base VN and Mystery VN independently of the source.
PARANORMASIGHT retains its established Mystery preset. Connecting another game's
text alone does not qualify its extraction quality or semantic behavior.

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
running** preference is represented by **Launch game with**: direct executable,
Steam, or **I will start the game**.
Multiple matching instances produce an explicit error. PID values are never
saved. For Steam, select the actual game executable for binding and enter the app ID
from its store URL (demos have their own IDs). VN Player opens the registered
`steam://rungameid/<id>` link, waits up to 60 seconds for the exact executable, then
injects Agent. It does not launch the exe with fabricated Steam environment values.
A running game is reused without reopening Steam. Missing Steam, a wrong ID/path,
or multiple matching processes fail visibly. Steam itself is never owned or closed.
Other launchers currently require starting the game externally.

**Edit game profile** updates the saved paths and launch preferences while the
session is stopped. **Stop** disconnects capture and stops the Agent process
launched by VN Player. Closing a game is opt-in and applies only to the game
process owned by VN Player; externally started games remain open. Newly added
games default to Base companion mode without requiring a complete script. The
existing PARANORMASIGHT profile retains its Mystery preset. The editor groups game identity/type, text connection, and optional play preferences.
Shared Agent setup is collapsed once configured. After previewing text, **Text looks
right — start companion** switches to play while keeping the game open. This button
is the user's quality check, not an automatic certification of extraction.

## Companion type and capabilities

`promptPack` selects `base` or `mystery` in a launch profile; it maps to the existing
runtime `prompt_pack`. Game type owns the following fixed capability presets;
the editor and play sidebar display abilities without editable switches. The host
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
facade always derives them from the selected type. Old persisted capability maps
are discarded during load and removed on the next save. Type and voice preferences
are retained; switching General → Mystery enables the entire Mystery preset.

### Voice, companion window and game view

The profile's `voiceInput` controls whether listening starts with play. The live
microphone button can change it for the current session. ASR input stays in the
owning VN session; late results and answers finishing after stop/restart are
discarded. **Stop speech** uses the existing speech interrupt path. The companion
overlay uses the existing helper and speech delivery integration; availability is
shown in settings.

**Attach game view** captures one frame from the bound game's window and displays
a thumbnail before sending it with a question. This requires an active interaction
lane and an image-capable configured model. It does not enable continuous watching,
alter global vision settings or fall back to capturing the desktop. The image is
attached to the existing completion, and is not stored in VN history or promoted
to script evidence. Luna can optionally specify the game executable for this use.

Luna profiles save the original-text WebSocket URL. Start Luna, configure its
extraction and start the game externally; VN Player then connects using the saved
URL. Luna extraction compatibility still needs validation for each game.

### API and verification

- `vn.launch.profiles` returns profiles, file availability, the shared `agentExe`, and `capabilityPresets`.
- `vn.launch.profile.save` accepts `{profile: {name, gameExe, hookHelper, ...}, agentExe}`;
  omit `profile.id` to create, or supply an existing ID to edit. `promptPack`,
  `voiceInput` and overlay preferences configure the companion. `launchMethod`
  (`exe` or `steam`), `steamAppId` and `launchGame` configure startup. Live process IDs and
  arbitrary runtime implementation fields are not editable profile fields.
- `vn.launch.start` accepts `{profileId}` to use saved settings, or
  `{profileId, captureOnly: true}` to test extraction without companion responses.
- `vn.launch.status` includes `captureOnly`, `capturedLines` and source connection
  status. `vn.launch.stop` uses the saved game-close preference unless overridden.
- `vn.launch.capture` returns a transient `visual_context` for a question. Submit
  it on `vn.player.ask` or `vn.choice.ask`; unsupported models fail visibly.

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

The VN runtime preserves repeated observations even when they share a script ID.
Branch revisits are legitimate input. Transport replay suppression must be owned
by a source adapter with verified event identity; script IDs are not that identity.
