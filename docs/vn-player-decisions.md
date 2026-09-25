# VN Player implementation decisions

## 2026-09-25 — WebSocket-only Agent input in the initial PR

The user approved resolving D5 before opening the draft PR. Remove automatic and
explicit clipboard reads from the Agent adapter in this release. WebSocket is the
default and sole transport; disconnection keeps the same source identity and
reconnects with visible status. Former clipboard/hybrid modes report unsupported
instead of silently selecting another source. The launch-only probe switch is
now named `bridgeText`, with its existing callers updated.

Regression coverage uses a real loopback WebSocket: initial connection refusal,
first text, disconnect, reconnect and a repeated line. The OS clipboard entry point
must remain untouched throughout. Any future clipboard adapter requires explicit
user selection; it must not be an implicit transport fallback.

## 2026-09-25 — Defer independent muting from the initial PR

At the user's request, remove the session mute checkbox, saved speech preference,
and corresponding runtime gate from the initial release. Retain current-playback
interruption, ASR input, and pausing spontaneous commentary. Paused commentary
continues to allow spoken answers to player questions.

D3's remaining portrait presentation work is deferred: directly pushing new text
into the existing caption would overwrite the line whose audio is still playing.
A later design must coordinate readable reply history, playback-aligned captions,
queue failures, and input/model errors without discarding generated replies.
The local saved profiles did not contain the removed preference, so no migration
or compatibility path is introduced.

## 2026-09-25 — Session controls and source-independent product flow

Game launch, text extraction, VN semantics and presentation retain separate owners.
Both Agent and Luna profiles can use the common exe/Steam launcher. The sole live
ingress remains `vn.line`; game type selects abilities while session preferences
control commentary, speech, microphone, scoped vision and portrait visibility.
The runtime owns visible activity/history and event identity. The renderer reads
the existing nested event structures. The portrait window is repository-owned,
with the existing optional Companion Lite art pack.

Window vision uses exact-HWND Windows Graphics Capture in an isolated worker.
Desktop-composited screenshots violated the game-only boundary; a later observed
native access violation justified isolating capture from the host. Neither error
path falls back to a desktop screenshot or another text provider.

Implementation and qualified evidence: [product acceptance](vn-product-acceptance-2026-09-25.md).

## 2026-09-24 — Saved game setup

**Decision:** VN Player owns editable launch profiles and orchestrates the separately
installed Agent executable. Profiles hold executable/script paths and launch
preferences, never a PID. Agent installation is shared. The backend persists
settings atomically in `.amadeus/vn-profiles.json`; Electron provides file selection.
Existing adapters and `vn.line` remain the text boundary.

**Why:** The backend already supported automatic injection, but the ordinary Start
button defaulted to manual game/Agent startup and choices were not saved. A saved
profile fixes that user journey without embedding or forking Agent.

**Acceptance:** 2026-09-24, an isolated workspace with no saved profiles added
PARANORMASIGHT through the actual React form. Save/test launched game PID 14376
and Agent PID 3376 and received three real observations. After stopping and
recreating the manager, ordinary Start used the saved profile, launched game
PID 13048 and Agent PID 26188, and received the same three observations. They
contained one interaction label without a script ID and two aligned dialogue IDs.
No manual Agent process/script selection occurred. Both runs stopped their owned
processes. The OS path picker was substituted in the browser harness; the form,
handler, storage, launcher, Agent and game were real.

Local evidence: `output/diagnostics/vn-profile-live-20260924-232336/`.
Repeatable harness: `tools/probes/verify_vn_profile_live.py`.
The transport test makes no statement about model response quality.

## 2026-09-24 — Semantic design and compatibility baseline

**Decision:** Keep one VN runtime and one input protocol. Separate independent
capability selection from the semantic type using the existing `prompt_pack`
contract. Initially implement `base` and `mystery`. Source selection (Agent/Luna)
does not select a semantic type or imply script alignment.

Capabilities are immediate commentary, player interaction, summaries,
retrospective reflection, lookahead and mystery reasoning. Shared scheduling,
displayed-text memory, verification and output remain in the runtime; type policies
own interpretation instructions and type-specific rules. Existing mystery rules
remain in the mystery policy. Base VN must accept text without a complete script,
speaker, scene ID or script ID, and must not load PARANORMASIGHT assets implicitly.

Lookahead is optional and only becomes usable with script data and a verified
alignment; an arbitrary upstream ID is not an aligned position. Model reasoning
and summaries cannot promote unseen text or model commentary to observed facts.
Disabled capabilities must actually stop their work, not merely hide the UI.

**Alternatives considered:** A single runtime with scattered game-name conditions
would keep the current coupling. Separate per-type runtimes would duplicate
memory, scheduling and speech. A shared runtime with bounded type policies and
explicit capability gates provides the needed separation without a plugin framework.

**Compatibility:** The existing PARANORMASIGHT preset, model prompts, cadence,
summary/reflection behavior, script alignment and companion output form the
Mystery baseline. Synthetic characterization tests were captured before semantic
changes in `tests/test_vn_semantic_compatibility.py`. Live local traces support
additional before/after replay. Real model outputs may vary; assess decisions,
evidence use, spoilers, cadence and interaction rather than identical wording.

**Intentional correction:** Session-wide suppression of an already seen script ID
is not valid transport deduplication. Earlier design discussion explicitly required
branch revisits and genuinely repeated lines to be retained. Correct that behavior
with regression coverage and identify it separately from compatibility regressions.

**Next gates:** Base/Mystery offline contract tests, deterministic Mystery replay,
then bounded model comparisons where credentials/configuration permit. Select an
additional non-mystery game from verified available Agent scripts and legal game
sources before adding further semantic types. Do not predesign romance/horror
modules solely from genre names.

## 2026-09-24 — Semantic acceptance and product interaction

**Implemented:** The profile editor separates game connection and companion
settings. The primary play feed shows game text and companion replies; raw protocol
events remain in collapsed diagnostics. Capability states explain missing scripts,
alignment or model availability. Voice input is explicit and session-bound. The
existing companion helper remains the presentation surface. Game-view attachment
is one user-requested snapshot, with a thumbnail and remove action before sending.

**Vision decision:** Capture the process already bound by the launcher, verify
executable path and window ownership again, and never expand to the foreground
window or desktop. Use the existing multimodal attachment helper in the same
completion. No image-description intermediary and no permanent screenshot memory.
For an external Luna stream, an optional game executable supplies the capture target.

**Acceptance evidence:**

- Archived `d3b42f4` versus the new Mystery runtime on 80 unique-script observations
  from a local real-game trace: no differences in per-line decisions, attention,
  lookahead, summary or reflection outputs after timestamp normalization. Genuine
  repeated IDs are covered separately as the intentional correction.
- A bounded real-model A/B on six of those observations: both arms spoke only on
  the same fourth observation; wording, confidence and model patches varied.
  Five of six decision labels matched; `silence` versus `hold` on the first line
  produced no speech in either arm. This is a limited compatibility check, not a
  claim of deterministic model wording or a complete story playthrough.
- Base real-model run on 40 synthetic ordinary dialogue observations without a
  script: three model summaries, one model reflection, and a grounded player answer
  about weekend plans. No detective evidence or hypothesis nodes were created.
- Deferred-answer regression: an answer started in session A cannot write or speak
  in session B. ASR uses the same active-session identity rule.
- Actual PARANORMASIGHT window capture returned the correct 960×540 title screen
  from the bound game process. An early attempt captured the game's temporary
  startup window; the settled window was then verified visually. Snapshot previews
  remain explicit so the user can inspect what will be attached. Evidence:
  `output/diagnostics/vn-game-visual-live/`. No desktop capture fallback was used.

The current workspace's existing PARANORMASIGHT paths and launch preferences are
saved in its ignored `.amadeus/vn-profiles.json`. Existing operator capability
defaults remain inherited until the user explicitly changes the switches.

Local reports are under `output/diagnostics/vn-mystery-replay/`,
`vn-mystery-model-ab/`, and `vn-base-model-20260924-234656/`.
The final focused suite passed 55 tests covering profiles, semantic contracts,
ASR/vision boundaries, text adapters, overlay continuity and speech delivery.
The Electron production build and headless UI acceptance probe passed.
Real microphone recognition accuracy was not requalified; the existing ASR
pipeline is reused and the new routing/lifecycle boundaries are tested.

## Further game coverage

Base is the common interpretation mode; no romance/horror pack is introduced
without a demonstrated semantic need. A concrete additional test candidate is
[Kemono Teatime's official demo](https://store.steampowered.com/app/3345060/),
with an existing [upstream Agent script](https://github.com/0xDC00/scripts/blob/main/PC_Steam_Unity_Kemono_Teatime.js).
The script identifies the full Steam game, so demo/version compatibility remains
unverified. This is a candidate, not a claim of supported extraction. No game
purchase or new demo installation was performed during that implementation pass.

## 2026-09-25 — Non-mystery game: Kemono Teatime Demo

Downloaded the official free Steam demo (app `3345060`, build `21219052`,
in-game version `1.0.1`) and tested its Simplified Chinese opening with the
unmodified upstream `PC_Steam_Unity_Kemono_Teatime.js` (`1.0.0`). The bundled
script matched the current upstream file after newline normalization.

**Text-source acceptance passed:** The real profile form's Save and test text
captured eight observations. A new launch manager reloaded the saved profile;
ordinary Start automatically reattached Agent and delivered eight more observations
to the Base VN runtime. No manual Agent process/script selection was needed.
The running game was started through Steam and was correctly treated as external.
Both phases used the Agent WebSocket. Evidence, UI screenshots and settings are in
`output/diagnostics/vn-profile-live-20260925-001310/`.

The sixteen observations include opening narration, unnamed speech, and named
dialogue between the sisters. No observation supplied structured `speaker`,
`script_id` or `scene_id`; speaker labels such as `【玛卡珑】` remained in the text.
Base accepted the stream with an empty script index and reasoning/lookahead off.
Model calls were disabled for this transport experiment, so this result does not
qualify companion response quality, summaries or long-term interpretation. Choices,
branch revisits, management screens and the full game remain outside this sample.

**Automatic game launch did not pass:** Direct execution of the installed exe
showed the game's own error dialog. The same failure reproduced with no Agent
running, while Steam `-applaunch 3345060` reached normal gameplay. This isolates a
storefront launch integration gap from extraction. The present local demo profile
therefore has Launch game disabled: start it through Steam, then Start in VN Player
automatically binds Agent. Supporting a Steam launch entry in the profile is the
next launch-layer change; do not disguise this requirement with a game-specific
hook change or injected Steam environment variables.

The live probe now supports `--attach-running` explicitly and reports progress
counts. Its status subscription was updated to the shared UI fixture's listener
contract, fixing a test-instrument mismatch introduced when that fixture changed.

## 2026-09-25 — Game-type policy and VN interaction redesign

User correction: semantic capabilities belong to the game type, and should be
displayed rather than configured as independent UI switches. General VN includes
commentary, player interaction, summaries and reflection. Mystery VN includes all
six capabilities, adding lookahead and detective reasoning. Availability still
depends on actual model/script/alignment prerequisites.

The launch facade now resolves this policy from the existing semantic presets;
the renderer receives that same map for previews. Old saved per-ability overrides
are dropped during load and removed on the next save. New profile saves reject
these overrides. Low-level runtime switches remain for existing headless callers
and isolated experiments, but do not override the selected launch profile's type.

The main page prioritizes the chosen game, a single Start/Stop action, story feed
and a read-only capability panel. Process details and raw protocol events are
collapsed. The editor groups game identity/type, connection, and optional play
preferences; shared Agent setup is collapsed after configuration. Its scrollable
body keeps Save/Test actions visible. A successful text preview offers an explicit
user action to begin companion play while keeping the game open. Messages display
in chronological order, with automatic following only while near the feed's end.
Chinese copy and narrow layouts are included.

Steam profiles now save `launchMethod: steam` and a numeric `steamAppId`. Start
opens the registered Steam game URI and waits for the configured exact executable;
it never attaches to or closes Steam itself. Existing game processes remain
external. Newly launched game processes use psutil identity checks for lifecycle
handling. Missing Steam, invalid IDs, ambiguous processes and a launch timeout are
explicit failures. The local Kemono demo profile now selects Steam app `3345060`.

Validation: 58 focused backend tests passed; the production build passed with the
existing bundle-size warning. The actual React form/handler/store acceptance
passed type switching, Steam setting persistence, preview-to-play transition,
model availability, microphone lifecycle, game-view attachment, and Chinese/narrow
layout checks. Screenshots: `output/diagnostics/vn-profiles-ui/`.

Steam live run `vn-profile-live-20260925-003820` reached game PID `30612` and Agent
PID `28636` automatically, then timed out with no text observations. The desktop
could not activate the game and capture showed a lock-screen-like background;
UI control was stopped and the user was asked to unlock. Both owned processes
were cleaned up. This proves dispatch/binding, not the full Steam-to-dialogue
acceptance. The two-run real text gate remains pending an unlocked desktop.

### Settings design alignment

Following the user's reference to the existing Settings page, VN now shares its
`GroupTitle`, `SettingsGroup`, `CardShell`, `CardIcon` and `StatusPill` primitives.
Their Settings behavior and styling are retained. VN uses the same page/panel
typography, Fluent icons, semantic theme tokens and card grouping.

The profile editor uses three navigable sections: Game and companion, Text
connection, and Play preferences. Tabs support arrow/Home/End keyboard movement.
Saving validates the first invalid field, opens its section, and focuses it;
required fields on another panel cannot become an invisible submission failure.
The action footer stays visible while individual panels scroll. Capabilities
remain read-only and type-bound. At narrow widths the navigation becomes a row.

Production build and the UI acceptance probe passed. Inspected Chinese game,
connection, preferences, dark-theme preferences and 480px editor screenshots in
`output/diagnostics/vn-profiles-ui/`. Theme screenshots wait for CSS transitions
to finish so an intermediate color frame is not mistaken for the final design.

## 2026-09-25 — Session voice and scoped visual input

User distinction: game type owns semantic abilities; ASR and vision are separately
controlled input choices during play. ASR requires player interaction. Visual
behavior is a VN setting, with current options Off and When I ask, and must not
compete with General Settings' visual controls.

`VNPlayerHandler` now owns current input state. `vn.input.set` checks the active
session identity and publishes the result on existing `vn.status`. The VN page
has a persistent session toolbar; reloading it reads the actual state instead of
restarting ASR. `voiceInput` and `visionMode` remain saved startup defaults, while
live changes leave the profile unchanged. Stop resets inputs and closes only the
matching VN listener. Each listener has a fresh input generation so recognition
from a disabled or restarted listener is rejected even within the same session.
Losing player interaction also stops VN listening, including note/pin modes.

Typed and spoken questions share the same visual path: When I ask captures one
fresh bound game-window image immediately before the existing model completion.
Notes and pins do not capture. Preview is explicitly a preview; its image is not
reused for later questions. Failed captures are visible. Late captures cannot
cross VN sessions, revive disabled ASR, or attach images after vision is turned off.
No general visual configuration is read or changed, no general watching starts,
and no desktop fallback is added. Settings and VN both explain the scope.

The existing companion presentation windows have not acquired a second controller.
A later small-window entry must use this same API/state. The current entry is the
VN page's session toolbar. Tests cover global-policy isolation, ASR ownership,
late results, capability loss, pending microphone startup, typed/voice fresh
frames, profile defaults, and stale capture results. 81 focused tests, Python
compile checks, production build and the real React/handler/store UI probe passed.
Screenshot: `output/diagnostics/vn-profiles-ui/session-inputs-zh.png`. Hardware
microphone recognition and real-model visual answers were not requalified in
this control-layer change; the earlier Steam dialogue gate remains pending.

### Portrait overlay default

At the user's request, new launch profiles now default to enabling the portrait
overlay. The editor selects it when the helper is available and retains explicitly
saved off preferences. A selected but unavailable overlay can still be unchecked.
The local Kemono demo profile, previously created with the old off default, is now
enabled; PARANORMASIGHT was already enabled. Capture-only tests remain overlay-free.

### General vision excludes VN

The user explicitly confirmed General vision should govern non-VN scenes only.
The Settings toggle now says "General vision (excluding VN)". Inspection confirmed
the enable/mode boundary, and found shared image encoding still read General size
and JPEG settings. Encoding now takes explicit parameters: General passes its own
settings, while VN retains 960px / quality 68. Regression checks cover General
on/off against VN question capture on/off, verified game-window capture with
General disabled, and preservation of General's independent image sizing.
