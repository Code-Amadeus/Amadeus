# Local character management

Character management selects the conversational identity used by the next
backend start. It does not select an animation, a voice, a rendering backend,
a Provider, or execution permissions. All characters follow the application's
existing appearance and voice settings, including optional Live2D support when
that separate feature is installed.

## Create and edit

In Settings, create a character with a name and optional personality text.
The Host generates a stable ID; names may contain non-Latin text or be repeated.
Names use a single line without control characters, up to 128 Unicode code
points. Personality text allows 7900 code points after trimming its outer
whitespace; internal paragraphs are preserved. An all-whitespace personality is
empty. The GUI reads these limits from the Host and counts emoji in the same
units. These limits also apply to manually authored user records; shorten an
oversized name/personality without changing its stable ID.
The new character starts with no conversation history. An optional Kurisu
Japanese-persona prefill copies only the visible editable text; it may contain
Kurisu's name, which should be reviewed before saving. It does not copy history,
phrase tables, the Kurisu reference index, or execution authority.

User characters are stored under `.amadeus/characters/` in the application
project/data root. `AMADEUS_CHARACTER_DIR` can explicitly override this location,
including for isolated tests. This directory is local data, not source or an
asset download. Built-in Kurisu remains program-owned and cannot be shadowed by
a user file of the same ID. Missing or corrupt explicitly selected identities
are errors; they do not silently become Kurisu.

The simple editor manages name/personality records. Advanced manually authored
records with explicit name forms, text slots or voice lines are read-only in the
simple editor so saving a short form cannot discard or contradict those fields.
They still use the same loader validation and cannot override Host fact wording.

Updating a user character keeps its ID and conversation history. Saving changes
does not change the running character; restart the backend to apply them.
An edited active role retains a **Modified. Restart the backend to apply.**
label when revisiting Settings. The Host compares parsed saved content with the
fixed running definition; formatting-only changes do not set this label, and
reverting the content clears it. Missing or invalid files are shown as errors.
There is no copy, hide, delete, per-character media switch, or hot-switch API in
this iteration.

## Select the next startup character

The selection uses the existing `AMADEUS_CHARACTER_ID` desktop setting, default
`kurisu`. The Host validates the saved definition before the GUI selects it.
Settings distinguishes the actual running identity from the saved next-start
selection. A parent-process environment override remains locked; the GUI cannot
overwrite that source. Other existing configuration precedence remains unchanged.

The main chat, AUIP option labels, Companion speaker heading, VN overlay heading and avatar controls
use the running identity, not the pending selection. The actual avatar image,
animation, scene and voice remain application-level resources shared by all roles.
Changing those application settings therefore affects every character using them.
Chat avatars distinguish the user and assistant; they are not per-character
identifiers. Session titles remain unchanged. Foreign-character sessions show
their character's saved name, with the full stable ID in the tooltip so repeated
names remain distinguishable. An unavailable character is labeled explicitly.
The switch guidance points to **Settings → General → Character roles**; an
explicit parent-environment lock instead directs the user to that launch setting.
Renaming a role changes its display label, not historic session ownership or
accepted Work identity.

Before a running identity can be obtained, avatar settings use **Assistant
avatar** / **A**, and a Companion without an initial identity uses **Amadeus**.
This is an intentional visible fallback change from fixed Kurisu labels. Once
identity is known, the original Kurisu labels are retained. Amadeus remains the
application/system name.

## Recover an invalid startup selection

If a selected user file is removed or invalid when the backend starts, Electron
reports the character startup failure and offers an explicit **Use built-in
Kurisu and restart** action in Settings. This action works without a ready Python
backend. It writes the literal `kurisu` selection through the existing desktop
settings store and restarts; it does not delete or overwrite the invalid file.

When the parent process locks `AMADEUS_CHARACTER_ID`, the recovery control is
unavailable and the source must be corrected outside the GUI. An unrelated
backend failure is not classified as a character error and does not trigger a
silent reset. Save and restart failures remain visible.

## Personality scope and compatibility

A user personality is expanded once at load time into the existing Japanese and
English identity slots. Main Chat and its AUIP/Browser role branches consume
those slots through their existing assemblers. No extra model call is made.
With an empty personality, the existing name-only behavior is preserved.

VN reactions and Work commentary continue to follow the role's name without
receiving this new personality field. Hybrid opening examples retain neutral
user-role defaults. The inherited-branch fallback used when the main prompt
cannot load also retains its existing name-only text. These are current scope limits, not claims of a fully uniform
personality across every surface. A model trained for one character may also be
less reliable at portraying a different personality.

The existing **Kurisu Japanese persona override** remains a separate built-in
control: its applicable later requests use saved changes without changing the
startup identity. Its current language/surface scope, original prompts, fixed
Host phrases, media settings and timing are preserved.

The previous manual convention of placing custom TOML files in the program's
`characters/` directory is replaced by the user directory. Move a custom file
there with its existing stable ID and validate it before selecting it. The built-in ID remains reserved; a user file named `kurisu.toml` is never loaded
in place of the built-in character. Do not rename an ID
to change a display name or rewrite old session ownership.

## History and shared work

[Character startup and conversation ownership](character-runtime.md) remains the
identity contract. Conversations stay bound to one character. Retained Projects,
Work and artifacts remain shared under existing references and permissions;
unretained Draft lookup remains scoped to its conversation. Accepted Work keeps
its original source identity across Retry/Resume. Independent Work never gains
a fictitious conversational source because a startup character changed.

This change does not add VN-session ownership enforcement or a cross-role
history reader. The separately recorded VN explicit-session-ID reuse boundary
must not be interpreted as solved by this management UI.

## Development boundary

The M1 branch changes profile loading/storage, settings/recovery, and visible
identity projection. SpriteForge graph algorithms, rendering events, TTS,
scene policy, shared presentation claims and model prompt assemblers remain
owned by their existing modules. The parallel Live2D work owns application-level
visual profiles; M1 neither stores nor overrides their IDs. Per-character asset
references belong to a later reviewed change.