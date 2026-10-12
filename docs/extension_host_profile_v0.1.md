# Experimental extension Host profile 0.1

Status: implemented experimental subset, prepared for review on `codex/extension-pilot`.
Updated 2026-10-12. This document describes what this checkout actually accepts;
it does not upgrade the broader Extension Protocol v0.1 draft or its provisional
TTS/MCP/Skill contracts to a stable SDK. The schema is wider than this profile.

## Supported contribution owners

| Contribution | Native owner | Public authoring interface |
| --- | --- | --- |
| `voice_backend`, role `tts` | Host TTS proxy + separate Runner | `amadeus-extension-sdk==0.1.0`, `amadeus.voice.tts/v0` |
| `mcp_server` | Selected compatible Work Provider | Declarative MCP command/arguments/environment; native MCP protocol |
| `skill` | Selected compatible Work Agent | Packaged `SKILL.md`, description and relative resources |

`permissions.host` must be empty. ASR, context sources, observers, proactive
execution and third-party UI contributions are rejected. No Grant records are
created. OS permission declarations disclose intent; they are not an OS sandbox.
Main Chat receives neither MCP execution authority nor installed Skill bodies.
Notes MCP stores its own notes and has no Amadeus memory/history authority.

## Installation, versions and activation

Electron main owns local-wheel installation, private Python environments,
desired records, encrypted secret storage, explicit version updates and removal.
Normal Electron builds and `electron:dev` prepare the trusted SDK wheel offline
using the configured Python development environment. Packaged Electron ships it
in `resources/extension-sdk`; source installations use the generated build
artifact. Installation never downloads or builds the SDK from a third party.
The manifest and referenced package files are checked before importing code.
The wheel's `amadeus.extensions` entry point names the package containing
`amadeus-extension.json`; a TTS manifest additionally names its Runner object.
All five [examples](../examples/extensions/) are validated against these rules.
The manager shows package identity, contributions, commands and OS disclosures
and asks for confirmation before preparing an installation/update or enabling
a package. Updating an enabled package requires fresh confirmation.
Use **Settings → Extensions** for the complete lifecycle; Voice settings link
back to that manager rather than maintaining a second package record.

The baseline has no extension manager. These screenshots use synthetic packages
and the production settings components:

![Installed packages, disclosures and saved/runtime state](images/extensions/packages.png)

![One Skill installation with selected Work Agents](images/extensions/skill-agent-bindings.png)

The backend reads one startup snapshot. Install/configure/enable/disable,
Provider bindings, voice selection, same-ID version update and uninstall all
take effect after backend restart. Manifest `live`/`reactivate` labels cannot
override this profile. There is no `config.changed` notification or rolling
configuration generation handover in this implementation.

An update prepares a new environment, validates it, then commits the desired
record; compatible configuration/secrets and bindings survive. The old
environment is collected only after backend shutdown is confirmed. Removal
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

## TTS wire and SDK contract

The public [SDK README](../sdk/extension-python/README.md) is the authoring entry.
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

SDK preparation is covered from an extracted source fixture with no prebuilt
wheel: the normal desktop build entry creates the SDK offline, which installs
and imports in a fresh environment. Windows packaging also places the wheel in
`resources/extension-sdk`, and the installer uses that packaged location.
The normal `uv sync --locked` core environment includes its pinned setuptools
and wheel build backend; `--extra dev` is not required to build the desktop.
Windows Electron CI installs this exact base profile before the normal build.
Public SDK publication/licensing decisions, other-platform runtime qualification
and natural-voice Chat/Wallpaper/VN acceptance remain release work. Memory
remains outside this implementation.
