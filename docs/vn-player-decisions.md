# VN Player implementation decisions

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

## Remaining product work

After semantic integration, organize the UI around game setup, companion settings,
current play, and diagnostics. Connect the existing companion window, speech input,
speech output and visual context with explicit status and user controls. These
are separate from the game's text source. Preserve the established PARANORMASIGHT
experience while making optional capabilities and unavailable dependencies clear.
