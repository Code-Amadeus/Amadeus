# Experimental extension Host profile 0.1

Status: implemented experimental subset, extended locally on `codex/extension-theme-pilot`.
Updated 2026-10-13. This document describes what this checkout actually accepts;
it does not upgrade the broader Extension Protocol v0.1 draft or its provisional
TTS/MCP/Skill/theme contracts to a stable SDK. The schema is wider than this profile.

## Supported contribution owners

| Contribution | Native owner | Public authoring interface |
| --- | --- | --- |
| `voice_backend`, role `tts` | Host TTS proxy + separate Runner | `amadeus-extension-sdk==0.1.0`, `amadeus.voice.tts/v0` |
| `mcp_server` | Selected compatible Work Provider | Declarative MCP command/arguments/environment; native MCP protocol |
| `skill` | Selected compatible Work Agent | Packaged `SKILL.md`, description and relative resources |
| `theme` | Electron main + trusted desktop renderer | Data-only palettes and bounded images; `amadeus.ui.theme/v0` |

`permissions.host` must be empty. ASR, context sources, observers, proactive
execution and third-party executable UI contributions are rejected. No Grant records are
created. OS permission declarations disclose intent; they are not an OS sandbox.
Main Chat receives neither MCP execution authority nor installed Skill bodies.
Notes MCP stores its own notes and has no Amadeus memory/history authority.

## Installation, versions and activation

Electron main owns local-wheel installation, private Python environments,
desired records, encrypted secret storage, explicit version updates and removal.
The Host carries one SDK wheel in `vendor/extension-sdk`, selected by
`server/extensions/protocol-source.json` (source commit, version, filename and
SHA-256). Normal frontend builds verify its bytes with Node and need no Python
SDK build. Packaged Electron ships the same wheel in `resources/extension-sdk`.
Installation checks direct SDK requirements using Python dependency semantics,
including active environment markers, before pip. Compatible ranges and exact
pins are accepted; incompatible constraints and SDK direct URLs get explicit
errors without exposing URL credentials. Transitive dependency conflicts remain
pip resolver errors. Installation stages and verifies the local SDK bytes; it
never downloads or builds an SDK from a third party.
The manifest and referenced package files are checked before importing code.
The wheel's `amadeus.extensions` entry point names the package containing
`amadeus-extension.json`; a TTS manifest additionally names its Runner object.
All six [examples](../examples/extensions/) are validated against these rules.
New `runtime.kind=none` wheels contain resources only: their inspected package
directory is extracted without creating a venv, invoking pip or importing code.
They cannot declare `Requires-Dist`. Existing no-code installations retain their
previous private-environment layout until explicitly updated.

The Host's support matrix lives in `server/extensions/profile.json`; Electron
uses its checked generated copy. It controls contribution acceptance, selection
and Agent-binding requirements, not dynamic imports or execution authority.
Saved package revisions advance only after a real, committed change. Runtime
status reports the revision of each validated startup snapshot separately from
contribution health; Electron never treats a saved revision as an acknowledgement
from the backend. Reading older records supplies initial revision 1 without
rewriting them.

A fresh backend launch retires previous logical loaded revisions even when
child-process exit was not confirmed. This never authorizes deleting files;
cleanup still requires confirmed shutdown. A Host-generated `startup_error`
settles the attempted revision without marking it loaded or requiring endless
restarts; newer edits remain pending. Frontend-only validation or removal errors
do not create backend-restart work. POSIX resource extraction retains executable
bits for bundled MCP commands, but never archive setuid/setgid permissions.

The manager shows package identity, author-declared license (or Not declared),
full wheel SHA-256, local source filename, contributions, commands and OS disclosures
and asks for confirmation before preparing an installation/update or enabling
a package. Updating an enabled package requires fresh confirmation.
Use **Plugins** in the main navigation for the complete lifecycle. Settings →
Extensions renders the same manager and records. Voice and Appearance settings
link to it; selection remains in the relevant settings page. Update confirmation
highlights new or changed contributions, commands and OS access.

These baseline screenshots use synthetic packages and the production manager
components (main-navigation access was added in the theme pilot):

![Installed packages, disclosures and saved/runtime state](images/extensions/packages.png)

![One Skill installation with selected Work Agents](images/extensions/skill-agent-bindings.png)

The backend reads one startup snapshot. TTS/MCP/Skill installation, configuration,
bindings, voice selection, enable/disable and removal apply after backend restart.
Manifest labels cannot override that boundary. There is no live configuration
notification or rolling TTS handover.

Pure frontend theme packages apply immediately and never mark the backend for
restart. If either the old or new package includes a backend contribution, the
whole package switches at the backend boundary. Until then, the theme reads
the old accepted manifest, path and configuration. The manager distinguishes
saved and loaded versions/revisions from health and cleanup errors.
Disabling/removing a mixed package immediately closes frontend admission and
falls back to the theme base; its old backend may still be running. Re-enabling
does not reopen the old frontend before a new startup acknowledgement.

An update prepares a new environment, validates it, then commits the desired
record; compatible configuration/secrets and bindings survive. Backend installation
references, including an in-flight startup snapshot, survive until confirmed
shutdown. Superseded pure frontend resources are collected after application. Removal
defaults to keeping package data; explicit data deletion is a separate choice.
Reinstall does not restore activation, bindings or removed credentials.
Unavailable saved TTS selections remain visible and use text-only behavior.
Host/Python incompatibility or an unavailable validator never deletes installed
records, encrypted credentials or installation references. Validation runs
asynchronously; incompatibility blocks activation while retaining update/removal.
Transient deletion failures retain ownership markers for the next cleanup.

Runner crash recovery is separate from configuration application: it stays
inside the process-lifetime proxy, is bounded, and never repeats failed TTS
requests. Installation trust is not reconstructed by scanning directories.

## Data-only desktop themes

Build the [desktop theme example](../examples/extensions/desktop-theme/) and
install its wheel from Plugins. Enable it, then choose Quiet day or Quiet night
in **Settings → General → Appearance**. The example has no dependencies and
runs no package code. Themes do not enter the Work capability catalog or a Runner.

The builtin base remains `data-theme=classic|wallpaper-slice`; the qualified
package selection uses `data-theme-extension`. Component selectors, native
title bar and color scheme continue to follow the builtin base.
`electron/src/shared/themeTokens.generated.ts` publishes allowed tokens,
generated from the pinned protocol `theme-tokens.v0.json` allowlist; only their
default values come from the actual base CSS. Danger, warning and success tokens are
reserved. Only literal colors and installed font families are accepted; CSS
URLs, variable references and declarations are rejected.

The Host validates the same normalized palette it sends to the renderer.
Normal text and accent links must retain contrast of 4.5; muted/faint text at
least 3 across canvas, surface, alternate surface and their interaction overlays.
Reserved status foregrounds must retain 4.5 both directly on those surfaces and
on their own status fills. Surface and alternate-surface colors must be opaque;
the VN editor uses a surface inside dialogs instead of repeating a translucent
canvas. Relative luminance must stay at least 0.5 for classic and at most 0.1
for wallpaper-slice across all three base surfaces. An image background is
proved over its complete possible RGB range after dimming/compositing, including
interior minima. The Host derives a private `--on-accent` black/white foreground
that passes 4.5 on both accent fills; packages cannot set this extra token.
Borderline cases whose contrast cannot be proven are rejected before committing
a desired record. These checks qualify extension palettes, not the unchanged
built-in themes; the example explicitly overrides faint text and interaction
fills that otherwise inherit the older built-in defaults.

Press Ctrl+Shift+F12 (Cmd+Shift+F12 on macOS) in the main window to restore and
persist classic through Electron's native key handler. The Windows wallpaper
tray also offers Restore built-in theme. Neither uses package fonts or rendered
controls. An explicit launch-environment theme override remains authoritative;
a native error explains how to remove it if it prevents saving the reset.

Backgrounds are package-relative PNG/JPEG, at most 8 MiB and 4096 pixels
per dimension. Main validates paths, headers and native decoding; only bounded
bytes and MIME enter the renderer. No package filesystem URL or custom URL
scheme is admitted to the main window. Switching releases the previous style
and Blob URL. Invalid updates preserve the previous installation and selection.

An unavailable selected theme retains its saved identity and falls back to the
last verified builtin base, with a reason shown in Appearance. Reinstalling
never silently enables it. Uninstall defaults to keeping package data, with
explicit deletion available; cleanup failure remains owned and retryable.

## TTS wire and SDK contract

The public [SDK README](https://github.com/Code-Amadeus/Amadeus-Extensions/blob/49449a0f3572e74ac7269129c02c5b9a9e685bef/sdk/python/README.md) is the authoring entry for the pinned version.
An `Extension` registers exactly the TTS IDs declared by the manifest. The Host
compares registration with the manifest before accepting `ready`.

| Wire operation | Implemented behavior |
| --- | --- |
| `initialize` | `amadeus.extension-rpc/v0`, Host/package/activation metadata, initial config/secrets/paths, empty grants |
| `ready`, `contribution.health` | Runner readiness and bounded per-contribution health |
| `tts.synthesize` | One utterance; mono PCM16 little-endian chunk stream, constant rate per request |
| `$/cancel`, `$/ping`, `shutdown` | Cooperative cancellation, liveness and cleanup |

SDK inputs are `utterance_id`, `text`, `language`, advisory `speed` and optional
`deadline_ms`. The synchronous generator yields `AudioChunk`; rate is 8–48 kHz,
each chunk is at most 1 MiB, and at most two requests run concurrently. Wire
lines are bounded to 8 MiB. Use `context.on_cancel` to release blocking I/O and
`context.check_cancelled`; bound network timeouts and close resources in `finally`.
Context config/secrets/paths are read-only startup snapshots. TTS does not own
playback, subtitles, role language, interruption policy or fallback selection.
Cancelled work immediately stops reaching playback; late audio is discarded.
Pending cancellation responses occupy the two-request limit until completion
or process exit. Cooperative cancellation has a 10-second cleanup grace period;
completion within it preserves the Runner and restart budget. An overdue
cancellation terminates the Runner and uses the existing bounded restart budget,
without replaying any admitted utterance. Exhaustion reports an error instead
of leaving a supposedly healthy voice permanently full. Shutdown still has a
bounded process cleanup path.

Stdout carries JSON-RPC only. The SDK redirects ordinary package prints to
stderr, including imports; package code must not write raw protocol descriptors.
Errors do not echo arbitrary exception bodies or secrets. Runner subprocesses
receive an explicit platform environment plus their own bindings, not the
Host's decrypted credential environment. This is not malicious-code isolation.

MiMo declares English only and does not support Japanese. Fish declares Japanese
and English. Backend-specific speed controls are optional adaptations, not a
requirement to reproduce GPT-SoVITS behavior. Paid synthesis is not retried.
The Host sends neutral `speed=1.0`, without GPT-SoVITS sentence pacing.
Language matching uses case-insensitive primary tags, so `ja-JP` accepts `ja`
and `en-US` accepts `en`.

## Skills and native sessions

One installation can bind multiple Agents without copying resources into their
global Skill directories. The metadata names the actual installed resource.

| Agent | Discovery |
| --- | --- |
| Codex App Server | Extra roots, native discovery, exact source-path verification |
| Pi | Explicit `--skill`, native commands, exact source-path verification |
| Generic ACP / Direct Codex | Name/description/path catalog; native file tools load resources |
| ACP configured with `skill_discovery=dsh` | Temporary DSH startup overlay supplies exact selected contribution roots; DSH owns its native registry |

`skill_discovery` is an ACP connection setting, not a manifest field or an ACP
installation RPC. Its default is `catalog`; the new DeepSeek preset selects
`dsh`. Existing profiles require explicit selection and backend restart.
The overlay is removed after the SDK-owned Agent process exits, including failed
startup. No package resources are copied. DSH's independently configured built-in
skills/plugins are outside the Amadeus extension registry.

Native session handles store the selected package/contribution revision set
(version, digest and actual installation path). Adding Skills preserves existing
sessions, including sessions created before any Skills were installed. Updating,
disabling, removing, unbinding or relocating a previously recorded Skill rejects
the handle before native submission. Other Agents' changes do not invalidate it.
Older experimental handles with only an opaque digest upgrade on unchanged
resume; a changed digest cannot reveal its original set and requires a new session.
Native discovery checks exact paths. A missing or shadowed package is reported
as unavailable for that Agent while ordinary Agent tasks can continue; the Host
does not silently claim that a same-name native Skill is the selected package.

## Work artifacts and AUIP

Plugins are installed capabilities; files and AUIP applications are Work outputs.
Output classification, preview and AUIP Attach belong to the Work artifact
owner and are reviewed separately from this extension implementation. They are
not extension RPC methods or package permissions to read Host history.

## Configuration migration and protocol source

The shared [manual MCP migration](mcp_environment.md) and
[owned backend shutdown policy](backend_shutdown.md) are independent desktop
boundaries. Extension cleanup consumes the confirmed child-lifetime outcome;
uncertainty retains installed resources and reports a warning.

Manual MCP connection tests and Pi stdio calls receive the platform allowlist
plus that connection's explicitly encrypted environment. Move required tokens
and toolchain variables into **Providers → MCP → Encrypted environment values**;
do not depend on arbitrary Host `.env` inheritance. Native Agent launchers retain
their own environment behavior; successful execution there does not prove that
the connection probe has the required explicit bindings.

This also changes pre-extension manual connections. Their existing record format,
command, arguments and working directory remain supported. A real stdio
regression loads a version-1 manual record: a token present only in the Host
environment fails, adding it to the connection makes both the probe and tool call
succeed, and an unrelated Host secret stays absent. Desktop persistence tests
cover adding the encrypted binding, reloading it and projecting the registry
without exposing plaintext credentials. The transport uses MCP's `stdio_client`
context manager; passing launch parameters directly to `Client` is not supported.

The schema and semantic validator are pinned to a canonical source commit in
`server/extensions/protocol-source.json`. `tests/test_extension_protocol_provenance.py`
checks the copies offline. To verify the source as well, run
`python tools/check_extension_protocol.py --source <Amadeus-Extensions checkout>`.
Updating the protocol requires updating the reviewed pin and matching copies;
an upstream draft change does not silently change the Host's accepted subset.

## Evidence and explicit limits

Windows local source builds are the tested runtime target. MiMo English live acceptance
is pending; Fish has one live Japanese synthesis, not a listening
or performance certification. Codex/Pi native lifecycle tests use controlled
model endpoints; DSH additionally has real-model and desktop evidence.

The native Windows Electron lifecycle was also exercised in an isolated profile,
with a loopback model endpoint and a local synthetic tone wheel. The production
file picker, install/update/enable disclosures, settings store, Python backend,
independent Runner and playback path were used; no installer or confirmation
handler was mocked. This verifies lifecycle and playback, not paid-model or
natural-speech quality.

| Native GUI journey | Observed outcome |
| --- | --- |
| Install 0.1.0, confirm, enable, select voice, restart | Installed disabled first; activation required confirmation and restart; Chat invoked the Runner with `ja`, neutral `speed=1.0` and 4,800 samples; physical playback started and finished |
| Update to 0.2.0 while active | Saved version became 0.2.0 while 0.1.0 and its installation stayed active until restart; afterwards the old process and directory were gone and only 0.2.0 synthesized |
| Resume Chat after restart | Previous messages reappeared and another reply completed through the new Runner |
| Remove with default data retention, restart | Package, process and installation were removed; data remained; saved voice was visibly unavailable and a new Chat reply remained text-only |
| Reinstall, then remove with explicit data deletion | Reinstalled disabled with the retained data; after confirmed deletion and restart, both installation and saved data were absent |

![Native installation disclosure](images/extensions/native-install-confirmation.jpg)

![Chat invoking the installed local test voice](images/extensions/native-chat-use.jpg)

![Unavailable saved voice after removal](images/extensions/native-uninstalled-voice.jpg)

DSH 0.2.0-rc.2 with MCP client 2.0.0 has a reproduced SDK diagnostic-to-stdout
defect when listing unsupported resources. Keep this combination experimental
until an upstream fix is consumed and the clean-wire probe passes. No Host line
filter or global console monkeypatch is installed. See the
[ACP diagnostics and upstream tracking](acp_provider.md#current-dsh-constraints-and-diagnostics-2026-10-12).

DSH Windows workspace-write confinement requires both DACL and integrity-label
access. A directory can be writable and owned by the user while lacking
`WRITE_OWNER`, causing initialization to fail. The Host does not repair ACLs,
relocate work or downgrade to unrestricted execution. Use the read-only
diagnostic documented in [ACP setup](acp_provider.md).

An extracted source fixture verifies the shipped SDK using Node with an empty
PATH and an unavailable Python override, then installs/imports the wheel offline
in a fresh environment. Damaged bytes fail verification. Runtime Runner and
example tests consume that same wheel, not a second source tree. Build tools
remain development dependencies; frontend builds do not invoke Python.
Public SDK publication, other-platform runtime qualification and natural-voice
Chat/Wallpaper/VN acceptance remain release work. Memory remains out of scope.

## Licensing and SDK delivery (2026-10-13)

Host code remains AGPL-3.0. The canonical protocol/schema/validator and standalone
SDK in Amadeus-Extensions are Apache-2.0. Authored examples carry MIT-0; external
assets, voices, models and dependencies retain their own rights. The current
SDK source pin is retained in protocol main history by the merge of protocol #1.
Theme schema/validator provenance tracks protocol #2 pending its acceptance;
the reviewed SDK wheel and digest are unchanged. There is no PyPI release,
development-directory loader or new diagnostic-log feature in this change.

The installed SDK has PEP 639 license metadata and an embedded LICENSE.
`tools/check_extension_protocol.py --source <protocol-checkout>` verifies the
pinned text copies, wheel digest, metadata/license and packaged SDK source
against the recorded upstream commit. Source releases contain the same wheel.

The canonical build uses CPython 3.12.10 and hash-locked development dependencies.
It normalizes text line endings and ZIP timestamps/permissions, stores entries
without compression and recalculates RECORD hashes and sizes. Protocol CI
compares wheels built independently on Windows and Linux. The Host carries that
artifact unchanged; runtime still requires Python >=3.11. Matching SDK build
bytes do not imply cross-platform qualification of the Host or third-party
extensions.


### Explicit backend startup receipts (2026-10-13 audit follow-up)

The paired Electron/Python Host reports a received revision and a separate
`startup: accepted|rejected` result. A unique valid identity/revision rejected
by Python validation settles that launch without creating a loaded receipt.
A missing required configuration also rejects startup. Dynamic Runner health
and per-contribution availability do not rewrite a successfully accepted
snapshot. These receipts describe Host snapshot admission, not voice readiness.

HTTP/JSON/status-section failures, an unreadable whole startup snapshot,
duplicate identities, missing/invalid results and mismatched revisions are
unconfirmed. They never count as rejection or success, never confer loaded
state, and retain pending work with a diagnostic. Host preflight failures remain
known rejections. Revision comparisons still prevent late replies from clearing
newer edits. No package filesystem or Runner authority is granted by a failure
receipt, and SDK bytes/wire contracts are unchanged.
## Local theme pilot acceptance (2026-10-12)

The local branch integrates extension lifecycle 55551e1 and Work files/AUIP
adf1632 without changing either parent PR. P1 support/profile and persistence
rules are committed separately from P2 theme/UI work. The companion schema
source is pinned to 9c3f3a5 on a local protocol branch; publish that source
before opening a downstream PR that references it.

Automated evidence includes 470 desktop unit tests; 130 Python extension,
SDK, files/AUIP regressions (2 skips); the official model-less Electron smoke;
and the production-component visual smoke in English/classic at 1100 px and
Chinese/wallpaper-slice at 620 px. These visual fixtures use synthetic Host
snapshots, actual React components and the actual theme validator/renderer.
They verify base identity, one installed style, Blob image visibility, absence
of covering builtin gradients and no horizontal overflow. They do not exercise
native installation confirmation.

The following component-fixture screenshots were refreshed after the 2026-10-13
audit repairs, including the synthetic loaded revision. They are not new native
dialog acceptance evidence.

![Plugins with the light extension theme](images/extensions/theme-day-plugins-en-US-1100.png)

![Appearance with the dark extension theme in a narrow Chinese window](images/extensions/theme-night-settings-zh-CN-620.png)

A separate local diagnostic used the real Electron installer/store, trusted Python
wheel helper, native image/font APIs and an independent SDK Runner. Confirmation
decisions were supplied by the driver; it did not operate native dialogs.
The pure-theme lifecycle retained backend PID 11912, including cancel, invalid
update rejection, disable/re-enable, retained-data reinstall and explicit deletion.
The mixed journey used backend PIDs 12788 then 2272 and Runner PIDs 4296 then
34160. Completed tone requests were v1, v1 (after v2 was saved), then v2 (after
restart), each 4,800 samples at 24 kHz. Old installation references survived until
shutdown; final removal deleted the requested data. This is process/contract
evidence, not Chat playback or native GUI acceptance.

The process journey is reproducible on Windows after the normal desktop build:

```powershell
python tools/probes/extension_theme_lifecycle.py
```

The probe builds all synthetic wheels offline, creates a fresh profile, and
records the report under `build/extension-theme-lifecycle/<run>/report.json`.
It verifies real image decoding and installed fonts, pure-theme preparation
without pip/venv, actual revision acknowledgements, two mixed-package versions,
SDK audio and removal. Its isolated Python process hosts `ExtensionRuntime`;
it is not the HTTP/WebSocket server or the Chat playback path. Confirmation
choices are supplied by the driver and reported explicitly.

The P2 native Windows GUI journey also passed in an isolated profile. The real
file picker and native install/update/enable dialogs were operated, followed by
production settings, the backend, independent Runner and Chat playback. A local
synthetic model and tone package keep this repeatable without paid services.

| Native theme journey | Observed outcome |
| --- | --- |
| Pure theme install, enable and select day/night | Installed disabled first; both palettes applied immediately without backend restart |
| Cancel v2 update, then accept it | Cancellation preserved v1, its revision and installation; acceptance applied v2 and reclaimed the old resources |
| Invalid v3 theme | Contrast validation rejected the update and preserved working v2 |
| Disable/re-enable, remove, reinstall and delete data | Immediate fallback preserved the saved theme identity; re-enable restored it; retained data survived a disabled reinstall; explicit deletion removed it |
| Mixed theme/TTS v1, then save v2 | Saved revision 3/version 0.2.0 while loaded revision 2/version 0.1.0 remained active; Chat still invoked the same v1 Runner |
| Disable, then re-enable before restart | Frontend access closed immediately and stayed closed; the UI disclosed that the old backend continued until restart |
| Restart mixed package and resume Chat | Revision 5/version 0.2.0 applied together; old process and installation disappeared; history resumed and the new Runner played a response |
| Remove mixed package with data deletion, restart | Record, installation, data and Runner disappeared; theme/voice selections retained their unavailable identities; another Chat turn completed without speech |

The pure-theme journey kept the same backend process throughout. The mixed
journey completed three Japanese-tagged requests in order: v1, v1 after v2 was
saved, and v2 after restart. Each produced 4,800 samples at 24 kHz; playback logs
recorded physical start and completion. The final text-only turn added no
playback. These are observed process and playback results, not a subjective
listening assessment of natural speech.

Acceptance found two stale-feedback cases: a rejected update left an earlier
success notice, and a successful restart left the old pending-start notice.
Operation feedback now clears at the next attempt and confirms only that saving
succeeded; package cards remain the source of current application state. Both
cases have component regressions. The final build and visual smoke passed after
this presentation fix; the full native journey above preceded that final fix.

![Native mixed-package update disclosure](images/extensions/theme-native-mixed-update.jpg)

![Saved v2 while the original theme and backend remain active](images/extensions/theme-native-mixed-pending.jpg)

![Chat history and playback after the whole mixed package switches to v2](images/extensions/theme-native-mixed-chat.jpg)

![Saved theme identity remains visible after removal](images/extensions/theme-native-removed-identity.jpg)

No remote paid synthesis, natural voice audition or cross-platform GUI
certification is claimed. Narrow-window evidence is from the production-component
Electron smoke, not the native-dialog journey. DSH/ACL constraints described
above are unchanged; this theme work does not qualify a new Agent path.

## Audit repair revalidation (2026-10-13)

The parent file-delivery branch now preserves amendment ownership through
recovery and keeps previews stable during unrelated renderer updates. The
extension branch builds with the documented core-only Python environment and
recovers overdue cancellation under the existing bounded budget. General MCP
environment migration and desktop process shutdown are separate reviewed
dependencies; this pilot consumes their contracts.

After synchronizing those changes, the P1-only head passed 464 Electron tests
and the normal build. The combined P1/P2 head passed 478 Electron tests and
211 scoped Python regressions (2 skips), plus the core-environment build. The
actual model-less Electron smoke passed all 13 checks, including Plugins,
file/app separation and clean shutdown. The reusable theme lifecycle probe also
passed with the real installer/store and SDK Runner; its confirmation choices
are supplied by the driver, not native dialog input.

The earlier native-dialog and physical-playback acceptance remains historical
evidence for its recorded head. No second full manual GUI run, natural-voice
audition or new Agent/cross-platform qualification is claimed for this repair.
The new cancellation and shutdown failure regressions address gaps that the
earlier normal lifecycle journey did not cover.

## Mainline reconstruction and focused revalidation (2026-10-13)

P1 is rebuilt from its own reviewed diff on main `c569a0f`, which contains
the accepted file/AUIP, MCP migration, backend shutdown and extension lifecycle
PRs. Its stable patch-id is unchanged. P2 is rebuilt on that P1, preserving
the accepted SDK source `49449a0` and wheel digest. Theme schema/validator
provenance now points at protocol #2 `223aac5`; that theme contract remains
provisional pending separate review and merge. Developer mode, logs and memory
are outside this reconstruction.

The prior Electron Windows failure was reproduced in the real offline settings
smoke: its standalone test entry did not register `desktop-theme.get`, so the
theme provider could not obtain the locked theme and the GUI assertion timed
out. The smoke now calls the production `ExtensionInstaller.themeState` owner
through that IPC handler. Its eight existing outcome checks pass without a
backend or Python process. No production fallback or timeout increase was added.

Fresh local evidence on the rebuilt implementation:

- P1: 99 Python tests passed, one symlink privilege skip; 467 Electron tests
  passed and the production build passed with an unavailable Python override.
- P2: 264 scoped Python tests passed, three skips (two Windows symlink privileges
  and the optional DSH runtime); all 481 Electron tests passed. The scoped suite
  includes ordinary-file/AUIP ownership, preview and launch boundaries.
- The actual Electron/backend model-less smoke passed all 13 checks, including
  Plugins navigation, saved startup mode, separate file/app views and clean
  process shutdown.
- The production-component smoke passed its seven journeys, including theme
  rendering at 1100px English and 620px Chinese and the 560px configuration form.
  Host responses in this visual fixture are synthetic; image/font validation
  and component rendering use the real implementation.
- The real installer/store, native image/font validation and independent SDK
  Runner lifecycle probe passed. Pure themes retained their backend process
  with no venv or pip; mixed synthesis used v1, v1 after saving v2, then v2 after
  restart. Cancellation/rejected updates retained the working version, frontend
  admission closed on disable/remove, and owned processes/resources/data were
  cleaned according to the selected retention policy.

The lifecycle probe reports `nativeDialogs: false`: confirmation decisions were
supplied by the driver. The 2026-10-12 manual native-dialog/Chat playback journey
above remains historical evidence; it was not repeated or relabeled as a new
manual acceptance run. Final-head CI and focused external review remain the
merge gates for P1/P2 and protocol #2.

## First audit repairs (historical, 2026-10-13)

**Evidence correction:** the historical 14-check model-less smoke claim below is
withdrawn. Its asynchronous predicate could return false without failing the
wait; see the second audit investigation and replacement acceptance below.

P1 now retires logical backend receipts at every new launch, independently of
whether prior process-tree cleanup was confirmed. File deletion still requires
the original confirmation/reference boundary. POSIX resource extraction keeps
executable bits only (never setuid/setgid); its regression actually runs the
bundled command on Linux/macOS. A Host-generated `startup_error` settles that
launch revision without pretending it loaded; later edits remain pending.
Frontend-only validation/removal errors do not create backend restart work.

That first repair pinned protocol #2 `d06f319`: its published 25-token allowlist and literal/image
signature checks are shared by the Host. Runtime font availability, full native
image decoding and palette contrast remain Host responsibilities. The stricter
readability rules and theme-independent recovery described above replace the
previous fixed-white accent labels and translucent surface allowance. Plugins
now opens Voice in the navigable Settings view and preserves its remembered
section. Theme notifications no longer expose raw Electron events; startup
chrome uses the last verified theme base. Screenshot fixtures carry real-shaped
loaded revisions and the current component screenshots were refreshed.

Other checks from that first repair: 492 Electron tests, production build, eight
offline startup GUI checks and eight component visual journeys passed. The
separate 14-check model-less report is withdrawn as recovery evidence; see
the investigation below. The real
installer/store/native image/font/SDK Runner lifecycle probe passed for both
resource-only and mixed packages; it reports `nativeDialogs: false`. It is not
a new manual confirmation or natural-voice playback run. Protocol tests passed
91 cases with one local Windows symlink-privilege skip. SDK bytes remain unchanged.

Known lifecycle limit: once the old backend has stopped and references are
released, a mixed-package update may collect v1 before v2 finishes starting.
Preparation failures preserve the old version, but a later startup failure does
not provide automatic rollback. That behavior is disclosed, not expanded in
this repair. Linux/macOS executable-bit execution is a required CI check rather
than a claim of local Windows execution. These PRs remain Draft pending final
head CI and focused external review; no merge, developer mode, logs or memory
work is included.

The final scoped Host Python run passed 264 tests with four skips (POSIX execute
bits on Windows, two symlink privilege checks, and optional DSH availability).
Canonical-source comparison passed. Clean release gates passed for P1 (3,991
selected / 101 excluded) and P2 (4,021 / 95), each with zero errors or warnings.
Protocol #2 final-head CI also passed Windows/Linux contracts and wheel byte
comparison; downstream Host final-head checks remain separate merge gates.


## Second audit: rendered surfaces and receipt evidence (2026-10-13)

Protocol #2 is now pinned to `d5c8a7d`. Theme v0 supports PNG/JPEG only, including
matching signatures and actual native decoding. WebP declarations fail before
installation, and renaming WebP bytes does not grant acceptance. The canonical
night example and shared valid/invalid CSS literal cases are pinned as fixtures;
Host tests validate that example and real Chromium accepts the supported syntax.
Comma and space/slash forms cannot be mixed. Host-specific validator help and
provenance match again; the SDK wheel is unchanged.

Hard-coded wallpaper-slice card/tab decorations now apply only to the builtin
base. Extension capability cards and nested visual controls paint the same
opaque token surfaces used by the Host proof. A native Electron regression
checks decoded PNG/JPEG files and actual Chromium computed styles/contrast for
an accepted near-threshold black palette and an image-backed translucent canvas.
It also verifies builtin decoration returns after leaving the extension theme.

Mixed-package selection resolves content from the applied manifest while the
current desired record retains enable/removal authority. A saved v2 that drops
or renames a selected v1 theme therefore leaves v1 visible until restart.
New themes wait for v2 admission; disable/remove still closes frontend access.

### Why the earlier 14/14 result was invalid

A clean rebuild of `d7988dc` produced the same renderer asset names as the old
build log. Running its real application in a new profile returned
`AMADEUS_UI_THEME is locked by the parent process environment` for the theme
setup. The probe stopped before invoking the native error dialog. An independent
Chromium reproduction with Playwright 1.60.0 showed that
`wait_for_function("async () => false")` immediately returned a handle to false,
while a synchronous false predicate timed out. This known Promise-truthiness
behavior is recorded in [Playwright #29132](https://github.com/microsoft/playwright/issues/29132).
The checked release tag is `87bb9ddbd78f329df18c2b24847bc9409240cd07`;
this was reproduced without modifying Playwright. No duplicate upstream issue
was opened.

The smoke made two errors: it ignored a failed setting-save reply and used an
asynchronous wait predicate as proof of persistence. The resulting true check
flag did not establish recovery, so the prior local and CI green results are
not accepted as recovery evidence. This was a test-instrument defect, not a
reason to relax the product's environment lock.

The corrected smoke removes the inherited theme variable, checks each IPC save
reply, verifies classic -> wallpaper-slice -> shortcut -> classic, and awaits
saved values in Python with explicit comparisons and a deadline. The existing
startup-mode persistence check uses the same corrected polling. DOM theme changes
use locator attribute assertions, avoiding wait-for-function eval under the
shipping CSP. The GUI launcher uses an explicit environment allowlist, a temporary
profile and a checkout without `.env`. A temporary loopback main inspector
checks the owned PID and sole main-window identity, then uses Electron
`sendInputEvent` to exercise the actual `before-input-event` hook. The probe
does not call recovery directly; its connection closes immediately and the
inspector exits with the owned test process. Production startup and CSP remain
unchanged. Regressions prove a missing/wrong saved value times out. Fresh reports bind source identity
and the actual dist/packaged-ASAR digest to the result. The accompanying PR lists
the final clean-build GUI runs; historical reports above remain distinguishable.

Installation dialog decisions in the separate lifecycle probe remain driver
supplied. Mixed-package startup failure still does not guarantee rollback after
old references are released. No developer mode, logs feature, memory or later
phase is added by this repair; all three PRs remain Draft.
