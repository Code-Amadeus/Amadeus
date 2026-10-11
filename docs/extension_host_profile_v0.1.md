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
The manifest and referenced package files are checked before importing code.
The wheel's `amadeus.extensions` entry point names the package containing
`amadeus-extension.json`; a TTS manifest additionally names its Runner object.
All five [examples](../examples/extensions/) are validated against these rules.

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

Stdout carries JSON-RPC only. The SDK redirects ordinary package prints to
stderr, including imports; package code must not write raw protocol descriptors.
Errors do not echo arbitrary exception bodies or secrets. Runner subprocesses
receive an explicit platform environment plus their own bindings, not the
Host's decrypted credential environment. This is not malicious-code isolation.

MiMo declares English only and does not support Japanese. Fish declares Japanese
and English. Backend-specific speed controls are optional adaptations, not a
requirement to reproduce GPT-SoVITS behavior. Paid synthesis is not retried.

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

Native session handles store the selected resource revision (package,
contribution, version, digest and actual installation path). Unchanged revisions
resume. Updating, disabling, removing, unbinding or relocating a selected Skill
rejects old handles before native resume/model submission; start a new Agent
session. Restarting the backend applies the selection but does not erase old
Agent history. Other Agents' unrelated binding changes do not invalidate it.

## Work artifacts and AUIP

Plugins are installed capabilities; files and AUIP applications are Work outputs.
They share ledger identity, provenance and user-acceptance ownership. The artifact
UI has separate file and interactive-app views, counts and recent windows.
Verified entry/manifest and Host-receipted members appear in application details;
independent reports in the same Work/directory remain ordinary files. Unclaimed
files are never hidden merely because their extension or directory looks like
application code. An invalid AUIP revision has no runnable application card.
Membership receipts name exact artifact revisions, not a path's future contents.
Entry and manifest records require a registered digest. Provider declarations
remain separately identified evidence and cannot replace Host file receipts.
An accepted recovery inherits only its predecessor's unchanged, hashed outputs;
unrelated or intervening edits remain ambiguous.

Text preview accepts registered Work/artifact IDs, validates current revision,
workspace containment and SHA-256, and returns at most 512 KiB UTF-8. AUIP open
continues through manifest/entry validation and explicit session Attach. File
preview does not execute HTML or accept a WorkItem.
These artifact APIs belong to the Host UI; they are not extension RPC methods
and do not create a package permission to read Host artifacts or history.

## Evidence and explicit limits

Windows local source builds are the tested target. MiMo English live acceptance
was deferred by the user; Fish has one live Japanese synthesis, not a listening
or performance certification. Codex/Pi native lifecycle tests use controlled
model endpoints; DSH additionally has real-model and desktop evidence.

DSH 0.2.0-rc.2 with MCP client 2.0.0 has a reproduced SDK diagnostic-to-stdout
defect when listing unsupported resources. Keep this combination experimental
until an upstream fix is consumed and the clean-wire probe passes. No Host line
filter or global console monkeypatch is installed. Existing upstream tracking
and a sanitized follow-up draft are in [the diagnosis](extension_diagnostics_2026-10-12.md).

DSH Windows workspace-write confinement requires both DACL and integrity-label
access. A directory can be writable and owned by the user while lacking
`WRITE_OWNER`, causing initialization to fail. The Host does not repair ACLs,
relocate work or downgrade to unrestricted execution. Use the read-only
diagnostic documented in [ACP setup](acp_provider.md).

Release qualification still needs SDK distribution/licensing decisions, other
platforms and manual Chat/Wallpaper/VN voice acceptance. Memory remains outside
this change; its recorded order stays M0 → M2 → M1.
