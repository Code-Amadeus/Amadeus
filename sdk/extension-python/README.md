# Standalone Python extension SDK

Implemented subset: [experimental Host profile 0.1](../../docs/extension_host_profile_v0.1.md).
This SDK provides the TTS Runner API. MCP and Skill packages share installation
and lifecycle, but use their own native interfaces and do not call this API.

Install the `amadeus-extension-sdk` wheel in the package's private environment.
It uses only the Python standard library and imports no Amadeus Host modules.
The package manifest's `runtime.entry` references an `Extension` object:

```python
from amadeus_extension import AudioChunk, Extension

extension = Extension()

@extension.tts("tone")
def synthesize(request, context):
    context.check_cancelled()
    yield AudioChunk(sample_rate=24000, pcm_s16le=b"\x00\x00" * 2400)
```

`request` provides `utterance_id`, `text`, `language`, `speed`, and optional
`deadline_ms`. A synchronous generator emits mono signed PCM16 little-endian
chunks (8–48 kHz, at most 1 MiB each). The sample rate stays constant within a
request. The Runner admits at most two requests concurrently.

`context.config`, `context.secrets`, and `context.paths` are read-only snapshots.
Use `context.on_cancel(response.close)` to release network streams promptly,
call `context.check_cancelled()` between blocking operations, and put resource
cleanup in `finally`. Bound all network timeouts. The Host never repeats failed
synthesis requests. Backend-specific voice and credentials belong in manifest
config; v0 has no reference-audio or emotion input.

Optional `@extension.initialize` receives the context before ready; optional
`@extension.shutdown` releases package resources. Run with
`python -I -X utf8 -m amadeus_extension.runner --entry your_package:extension`.
Stdout is reserved for JSON-RPC and package prints go to stderr. Health and
errors returned by the Runner omit arbitrary exception contents.

The experimental Host applies installation, configuration, enablement,
selection, and removal after backend restart. TTS, declarative MCP and Agent Skill
contributions are accepted; nonempty `permissions.host` is rejected. OS
permission declarations are disclosures, not an OS security sandbox.
Skills use packaged SKILL.md resources and Agent bindings; they do not run this
SDK or a Runner. Same-ID version updates are explicit and take effect on restart.
After a selected Skill revision changes, saved Agent sessions also require a
new session; a backend restart does not clear instructions from old histories.
General draft live configuration, Host Grants, ASR and context interfaces are
not implemented by this SDK. Successful schema validation is not full protocol
conformance.
