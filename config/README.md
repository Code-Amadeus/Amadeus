# Configuration ownership

Amadeus uses environment variables as **startup input**, not as a general
runtime state store.

## Precedence and entry points

1. The parent process environment has highest priority. Electron, CI, or a
   launcher can use it to define one locked process profile.
2. Electron desktop settings supply user-managed connection values to the
   Python child process. Secrets are encrypted through Electron `safeStorage`
   and are never returned to the renderer.
3. The repository-root `.env` supplies local developer values only when the
   parent/desktop launch profile did not already define a key.
4. `config/settings.py` parses application startup values and remains the
   compatibility import surface for Python callers.
5. Function arguments or explicitly owned runtime objects carry values that
   can change after startup. They must not be written back into `os.environ`.

`.env.example` is the public, curated setup template. `.env` is local and must
not be committed. Defaults that ordinary users should not tune stay in
`config/settings.py` instead of making the template unreadable.

`config/environment.py` is the shared dotenv and type-parsing boundary. It
loads one reader per project root, preserves the existing process-over-dotenv
precedence, and records every setting declared through it. New startup
configuration should use this boundary rather than calling `load_dotenv`
again.

## Shared startup declarations

The shared catalog covers the TTS selector, all four built-in TTS connection/model
groups, graphics, voice input/reference controls, model connections, model roles
and Provider connections: 122 fields in the catalog domain directories. Edit the owning JSON for defaults, types, options,
ranges, desktop editability, restart policy, and English/Chinese labels. The
configuration keys retain their existing environment-variable names.
Run `npm run generate:config` from `electron` after editing a declaration.

Python reads the JSON through `config/catalog/__init__.py` and the existing
`EnvironmentReader`; `settings.FISH_TTS_*` callers keep working. Electron consumes
the generated `src/shared/configCatalog.generated.ts`, which is compiled into
both main and renderer bundles. Offline Settings never requires a running Python
process or access to the source checkout. The generator also maintains marked
sections of `.env.example`. Commit generated output with its declaration;
`npm test` and `npm run build` reject stale output. Removed or renamed groups
lose their old managed env sections; duplicate declared keys outside those sections
are rejected. `catalog_legacy.json` bounds the remaining handwritten declarations:
new keys and migrated keys cannot be added to legacy desktop lists/forms or Python
parsing/status definitions. Shrink that inventory when migrating an owner.

Desktop snapshots distinguish stored overrides from known non-secret startup
inputs. The form follows source precedence even when offline. Dotenv interpolation
remains Python-owned: an unresolved offline value is shown as unknown, and a
pending clear never reuses the running backend's superseded value. Secrets expose
only configured state.

Supported field types are string, path, URL, enum, boolean, integer and number.
`computed_default: true` omits the static default: its owner supplies a value
when resolving it. Texture sampling still defaults to whether the effective
frame rate is 60 FPS, with explicit choices taking precedence. Graphics presets,
TTS device selection and atomic checkpoint-pair resolution remain ordinary code.
An `example` may differ from the runtime default (for example the curated Kurisu
setup); `example_active` preserves whether that env example is enabled.

Each TTS declaration has `voice_backend` metadata and a `section` (`output` or
`remote`). These drive the existing TTS registry, offline selector, Settings
placement and backend status projection. To add a built-in TTS backend, add its
declaration plus implementation: `factory` names its constructor, `probe` reports
readiness, and `streaming` is either a boolean or an implementation function.
Entry points are resolved lazily; listing backends never constructs a model.
The implementation owns protocol validation and optional dependency checks.
Add backend-specific tests, then regenerate; no central backend switch or
Settings allowlist needs editing. New env sections are appended automatically.

This is an internal catalog of packaged application data, not an extension
manifest loader. Third-party code still needs the extension host's lifecycle
and authorization boundary. ASR fields, shared reference/emotion controls and
live runtime controls retain their existing owners.

The catalog describes fields; it does not store user values or prove that a
backend is available. `DesktopSettingsStore` still owns encryption, process
locks, clearing, durable saves and pending revisions. Runtime probes still own
availability; an incomplete unselected provider must not prevent startup.
Secret declarations have no default, and status responses expose only whether
a credential is configured. Protocol-specific validation stays in the backend.

## Handler composition

`server/handlers/voice.py` owns construction and binding of TTS, ASR and Wake
handlers. `app.py` registers the domain's `handlers` before starting the server
and supplies runtime dependencies later through `configure()`. Add a handler
using existing voice dependencies inside this domain; `app.py` does not need a
new import, constructor, registration entry or configure call. New cross-domain
dependencies still belong at the application composition root.

The domain closes the current Wake and ASR instances before the shared microphone;
shutdown never invokes lazy factories. Shared service creation and replacement
remain with the existing scene/runtime owners. WebSocket registration rejects
duplicate method ownership before inserting any methods from the new handler.

## Chat image input

DeepSeek image input is available with `DEEPSEEK_MODEL_NAME=deepseek-flash`.
The documented `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` aliases
also accept images. DeepSeek Pro and legacy text models remain text-only.
See the [DeepSeek vision guide](https://api-docs.deepseek.com/guides/vision/).

Both direct DeepSeek Chat and the DeepSeek tail of `hybrid2` send the image
using the existing OpenAI-compatible `image_url` format. The hybrid local
head receives only a text acknowledgement hint. Images are attached to the
current user turn, not stored in conversation history. The desktop image
button follows the backend's selected-model capability; changing the model
in startup settings requires restarting the backend.

## Graphics profiles

`GRAPHICS_PROFILE` is the startup owner for the shared PixiJS render budget:

- `standard` is the default and uses 60 FPS with the native device-pixel ratio.
- `power_saving` uses 30 FPS and caps resolution at 1.5.
- `custom` uses `RENDER_MAX_FPS` (10–240) and
  `RENDER_MAX_RESOLUTION` (0.25–4.0).

The parsed effective values are propagated to chat, Electron, Lively, and
Wallpaper Engine render surfaces. A missing effective resolution limit means
native DPR; it must not be serialized as zero, `NaN`, or the string `None`.
Wallpaper Engine's valid general `fps` property is a runtime host constraint,
so the renderer uses the lower of it and the project profile. Unsupported host
values restore the project limit. This runtime constraint does not mutate the
startup environment.

`RENDER_TEXTURE_SAMPLING` defaults on when the effective startup limit is
60 FPS, and off at other rates. An explicit true/false choice overrides the
default. The 30 FPS fast-transition timing tradeoff remains opt-in. Changing
sampling requires a backend restart and reopening render surfaces.

Migration from an older `.env.example`: an existing
`RENDER_TEXTURE_SAMPLING=false` remains an explicit override, even if it was
copied from that template. Remove/comment that line to use the new startup
default, or set it to `true` to enable sampling explicitly. Updates do not
rewrite the user's `.env` or guess whether an old value was intentional.

Once sampling is enabled, a Wallpaper Engine host-cap change rebuilds the
sampling plans at the new effective FPS on their next use. It preserves
authored playback time, the logical position and exact held/required frames.
For example, a 120-frame, 5 ms transition keeps its 600 ms duration when the
host changes from 60 to 30 FPS; the unsampled legacy 30 FPS path can take 1 s.
The host cap does not toggle the user's sampling choice. Sampling-disabled
sessions stay disabled, and a 30 FPS startup default is still off.

`RENDER_BC7_CACHE` defaults true for the shared HTTP wallpaper asset server.
On GPUs exposing WebGL BPTC, displayed UASTC frames are saved as derived BC7
in the background, then reused on later loads and application launches. The
2 GiB texture-store CPU + GPU budget is unchanged. Derived files target
4 GiB under `%LOCALAPPDATA%/Amadeus/texture-cache/bc7-v1` on Windows,
`~/Library/Caches/Amadeus/texture-cache/bc7-v1` on macOS, and
`$XDG_CACHE_HOME/Amadeus/texture-cache/bc7-v1` (or `~/.cache/...`) on Linux.
Only owned flat cache files are pruned; character packs are never modified.
Concurrent app instances reconcile the shared directory on writes every 30
seconds, so the disk target may be temporarily exceeded between scans.
New writes also preserve a 1 GiB free-space reserve, checked before allocating
the temporary file (including its size) and before publication. This is a
best-effort guard against concurrent external disk use, not a volume reservation.
Low space pauses derivation; existing cache reads and playback continue.

Cache identity includes source content and the bundled transcoder revision.
Writes publish atomically. Corrupt entries fall back to the UASTC source and
are rebuilt as their frames are displayed. Unsupported GPUs/layouts, file://
legacy rendering, and unavailable cache storage retain the source path.
Startup derivation is bounded to one compression worker and two pending
frames, using zstd level 3 to limit interactive CPU work. It does not prebuild
an entire pack. Cache disk space, worker/transient buffers, and reclaimable
OS file cache are separate from the texture-store residency budget.

Identical publications succeed without replacing an in-use file. Failed
evictions remain indexed and charged. Temporary write failures (including
503 sharing/space errors) cool down for 30 seconds, then a later playback load
can try again; no payloads or autonomous retries are queued during cooldown.
403 disables writes for that page's capability lifetime, without disabling
reads. A stalled/crashed worker is retired with only its own pending jobs;
later requests create a replacement. Three consecutive worker incidents in
one channel pause that channel for 30 seconds. Decode and encode health are
independent, and a worker interruption does not mark valid disk data corrupt.
Temporary cache-directory initialization errors likewise retry on a later
asset request after a 30-second cooldown, without changing the configured flag.

## What belongs where

- Credentials, model paths, ports, startup feature flags: `.env` ->
  `config/settings.py` -> imported constant or injected constructor argument.
- Electron launch-profile defaults: `electron/src/main/index.ts`. These may be
  intentionally different from headless Python defaults. The current desktop
  profile enables realtime AEC/barge-in while headless Python does not.
- Runtime choices changed by UI or request handling: a named runtime owner.
  `tts/pre_translation_runtime.py` is one example.
- Test isolation and subprocess metadata: the test/launcher that creates the
  child process. These are not application settings.
- Script-local variables such as executable paths or encoding flags: the
  script, unless Python application code also consumes the same value.

## Electron settings contract

`system.get_config` returns current values from their real owners and
`system.set_config` accepts an explicit runtime allowlist only. Unsupported
keys fail instead of being reported as updated. Model and Work Provider
connection cards use a separate Electron-owned desktop profile. Non-secret
values are persisted in the app user-data directory; secrets are stored only
as operating-system-encrypted ciphertext. Saving a startup value never mutates
the repository `.env` and requires a backend restart. An explicit parent
environment value remains authoritative and is shown as locked in the GUI.

Skills and MCP connections are Host-installed Work Provider capabilities. They
are not a Main Chat tool surface: a Provider manifest must explicitly accept
the capability projection before the Settings catalog shows it as a consumer.

TTS mode and language changes are accepted only while Chat and playback are
idle. `TTS_BACKEND` defaults to the embedded Amadeus GPT-SoVITS v3 rewrite,
which accepts v3 GPT/SoVITS checkpoint pairs only. Selecting `openai_compatible`
avoids importing the local model stack and sends synthesis text to the
explicitly configured speech endpoint. Remote TTS defaults to broadly compatible
buffered WAV responses; `TTS_API_STREAM_PROTOCOL=openai_sse` explicitly enables
PCM first-packet playback for endpoints that implement OpenAI speech SSE events.
There is no automatic retry from a partial stream to a second billable request.
`TTS_BACKEND=fish_audio` selects Fish Audio's MessagePack WebSocket transport.
Set `FISH_TTS_API_KEY`, `FISH_TTS_MODEL` (for example `s2.1-pro-free`), and
`FISH_TTS_REFERENCE_ID` (the hosted voice ID, not the inference model).
The default reference is the public Japanese Makise Kurisu voice
`b450b19370434173b121446057622e9b`. `FISH_TTS_LATENCY=balanced` is the default;
`FISH_TTS_WS_URL` defaults to `wss://api.fish.audio/v1/tts/live`.
Install the `voice` extra for MessagePack support. Chat/VN keep their existing
sentence scheduling; audio streams into the existing playback pipeline.
The adapter also accepts asynchronously arriving text chunks for duplex
experiments. See [Fish Audio setup and probe](../docs/fish_audio_websocket.md).
`ASR_BACKEND` selects only
the full Conversation recognizer and defaults to Qwen3-ASR, preserving context
prompting and speculative endpoint optimization. `WAKE_ASR_BACKEND` is an
independent always-on role and may keep SenseVoice loaded alongside Qwen. A
remote Conversation ASR intentionally disables partial speculative API calls
to avoid hidden duplicate network requests and metered usage.

The Electron Voice settings use the same precedence and encrypted-secret store
as model connections. `ASR_API_KEY`, `TTS_API_KEY`, and `FISH_TTS_API_KEY` are never returned to the
renderer. Remote voice backends are selected explicitly; local failures never
silently upload microphone audio or synthesis text.
The first-release Main Chat default is remote DeepSeek; local model settings
apply only when the user explicitly selects the pure-local profile.
`LOCAL_LLM_TYPE` is editable for the pure `local` provider and synchronizes
ChatRuntime with the synchronous fallback. `LLM_PROVIDER` is the only chat
router; the deprecated `USE_LOCAL_LLM` value no longer overrides it.
`LOCAL_LLM_LAUNCH_MODE=external|managed` controls only ownership of the
default llama.cpp server process. LM Studio, Ollama, and llama-cli remain
pure-local compatibility profiles with type-specific fields. The `hybrid*`
providers use the dedicated OpenAI-compatible `HYBRID_LOCAL_LLM_URL` and
`HYBRID_LOCAL_LLM_MODEL`; they never branch on `LOCAL_LLM_TYPE`.

Direct `os.environ` reads are still valid at genuine process boundaries (for
example an isolated legacy provider helper), but they should not duplicate an
already parsed startup setting. A direct write is reserved for child-process
construction or a documented third-party compatibility contract.

## Intentional late reads

These are not pending mechanical migrations:

| Owner | Values | Why they remain late-bound |
| --- | --- | --- |
| `tts/aec_realtime.py` | explicit AEC delay | Only presence is checked; the value comes from parsed settings. An explicit value disables device-class calibration. |
| `tts/pipeline.py` | `ENABLE_CUDA_GRAPH` | The compatibility mode function and bundled inference code read the value at synthesis time. There is no active mainline UI caller today, so no replacement runtime contract is invented yet. |
| provider/session storage helpers | `AMADEUS_*_PATH` values | Helpers accept explicit path injection and subprocess tests supply isolated stores at their process boundary. |
| wallpaper scenario helpers | wallpaper/scenario overrides | Both wallpaper hosts resolve component-local media overrides without importing the heavyweight application settings facade. |
| `vn_player/runtime.py` | `VN_*` values | These describe one VN session, not the application startup snapshot. |
| `server/runtime_status.py` | build/workspace metadata | The launcher supplies diagnostic facts for the current process. |

## Compatibility notes

- `TTS_DEVICE=auto` resolves to `mps` on Apple Silicon, `cpu` on Intel macOS,
  and the existing `cuda:0` default elsewhere. The resolved device is copied
  to `os.environ` because the bundled BigVGAN loader directly consumes that
  variable. An explicit indexed CUDA device or explicit `mps`/`cpu` value is
  preserved.
- Local GPT-SoVITS CPU synthesis automatically uses the existing persistent
  TTS sidecar (the current Python interpreter unless `TTS_PYTHON` is set).
  This isolates its PyTorch thread settings from ASR/VAD dependencies. CUDA
  and MPS keep their embedded default; explicit sidecar/interpreter
  settings still select a subprocess. The sidecar serializes synthesis and
  drains interrupted requests before accepting the next one; CPU compute is
  still shared with the host. All synthesis modes consume blocking model/IPC
  streams on the TTS executor, keeping the event loop available to ASR.
- `AMADUES_PRE_TRANSLATION_ENABLED` remains accepted as a deprecated spelling
  of `AMADEUS_PRE_TRANSLATION_ENABLED` at the pre-translation boundary.
- The legacy root GPT-SoVITS WebUI/API entry points and their conflicting
  `config.py` were removed. A future HTTP API should be designed around the
  current `server.app` contracts instead of reviving that compatibility layer.

Model connection inheritance stays in the model owner. The catalog records legacy
names (`aliases`), facade attribute bindings (`setting`), and local engine field
visibility (`local_engines`). CLI numeric controls remain parsed strings;
`control: "number"` supplies UI hints without changing command argument types.
Bedrock bearer credentials now use the same surrounding-quote/whitespace
normalization as other API secrets. AWS credential discovery remains unchanged.

Role/session fields use `scope: "session"`: their owner calls `read_catalog_value`
against its current environment, preserving per-session overrides without loading
all application settings. `scope: "virtual"` marks composite controls such as
Codex transport; the desktop owner translates these into the real launch keys.
They are not emitted as synthetic environment variables or Python settings.
`visible_when` permits only equality against declared selectors in the same group;
it contains no executable expressions. Provider availability and authorization
remain runtime facts. String selectors offer choices without turning a previously
open provider identifier into a new hard-coded enum. Structured ACP/MCP profiles
retain their existing dedicated storage, validation and authorization owners.
