# Experimental extension packages

These source packages exercise TTS, MCP and Agent Skill contribution owners. They are not
published packages or a claim of a stable SDK. Build local wheels, install them
from Settings → Extensions, then configure, select/enable and restart the
backend. Removing or disabling a package also takes effect after restart.
The versioned [Host profile](../../docs/extension_host_profile_v0.1.md) defines the
accepted subset; broader fields in the protocol draft may be unsupported here.

- `mimo-tts`: one remote speech contribution using the standalone public SDK.
  It makes a single paid request for a synthesis, supports cancellation and
  rejects undeclared languages. The experiment declares **English only**;
  MiMo does not support Japanese (confirmed by the user). Live English service acceptance is deferred until a key is configured. The selected
  voice must exist in the user's MiMo account. No key is included. The SDK
  `speed` value is advisory and is not sent as a service parameter.
- `fish-audio`: Japanese/English speech over the official binary MessagePack
  WebSocket API. One real Japanese utterance has passed through an installed
  isolated environment; listening quality still needs user audition. Its pinned
  dependencies are websockets 16.0 and msgpack 1.2.2; HTTP proxies are supported,
  SOCKS additionally needs python-socks. No automatic synthesis retry.
- `notes-mcp`: a standalone stdlib MCP service with list/read/save tools for
  its own data directory. Select compatible Work Providers before enabling it.
  Main Chat gets no tools or execution authority. This is a note utility, not
  Amadeus semantic memory. Retain or explicitly delete its data when removing it.

- `handoff-skill`: declarative SKILL.md plus a report-layout reference. Install once,
  choose Codex/ACP/Pi, then restart. Updating or removing its installed revision
  requires a new Agent session; unchanged revisions resume normally. Resources
  grant no additional write/shell permissions. Native tests use real processes
  with a controlled local model; they do not certify LLM selection quality.
- `csv-audit-skill`: a second declarative Skill with packaged data quality rules.
  It checks required values, duplicate IDs and negative amounts in a small CSV,
  then writes a structured audit without changing the source. This and the
  handoff Skill have also been exercised with a real DeepSeek model over ACP.

The normal `npm run build` and `npm run electron:dev` commands in `electron/`
prepare the trusted SDK wheel automatically, offline from bundled sources.
Packaged Electron includes that wheel in its resources. Use the documented
Python development environment, or select it with `AMADEUS_PYTHON`; no separate
SDK build command is needed to install a local extension.

To build the example wheels from the repository root:

```powershell
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/mimo-tts -w build/extension-examples
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/notes-mcp -w build/extension-examples
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/fish-audio -w build/extension-examples
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/handoff-skill -w build/extension-examples
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/csv-audit-skill -w build/extension-examples
```

For SDK-only development, `python tools/build_extension_sdk.py` performs the
same build without downloading dependencies. A wheel manifest is inspected
without importing package code. The notes package
does not need the Runner protocol: its native runtime is MCP, managed by its
selected Provider. All share installation identity, configuration, restart
semantics, removal and the Host's lifecycle diagnostics.
For Skills, applying the changed selection and restoring an old Agent session
are separate steps: only unchanged selected resource revisions may resume.

To use DSH native discovery, choose **DeepSeek Harness native Skills** in the
ACP connection's advanced settings (`skill_discovery: "dsh"`), bind the installed
packages to that Agent, save and restart. Other ACP connections default to the
portable file catalog. See [ACP setup and known constraints](../../docs/acp_provider.md).

Work outputs are not extension registrations; artifact presentation and AUIP
Attach belong to the Host's separate Work output lifecycle.

No package imports `server`, `core`, `config.settings` or a private TTS helper.
Package code still runs with the user's OS privileges. TTS Runners and Host-owned
MCP probes use explicit environment bindings; native Agents retain their own
configuration and security model. These mechanisms do not prevent arbitrary OS
access by a malicious local package. Source/package licensing follows this repository;
separate SDK publication and licensing decisions remain pending.

Native discovery, resource use, restoration, addition, update and removal are
exercised in `tests/test_skill_codex_runtime.py` and `tests/test_skill_pi_runtime.py`.
