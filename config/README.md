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
`ASR_BACKEND` selects only
the full Conversation recognizer and defaults to Qwen3-ASR, preserving context
prompting and speculative endpoint optimization. `WAKE_ASR_BACKEND` is an
independent always-on role and may keep SenseVoice loaded alongside Qwen. A
remote Conversation ASR intentionally disables partial speculative API calls
to avoid hidden duplicate network requests and metered usage.

The Electron Voice settings use the same precedence and encrypted-secret store
as model connections. `ASR_API_KEY` and `TTS_API_KEY` are never returned to the
renderer. Remote voice backends are selected explicitly; local failures never
silently upload microphone audio or synthesis text.
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
| `asr/microphone.py` | microphone selector overrides | Standalone device-selection helpers read at call time and avoid importing the full settings facade when a valid explicit selector is present. |
| `tts/aec_realtime.py` | explicit AEC delay | Presence of an explicit value changes whether device-class delay calibration is used; tests exercise that distinction. |
| `tts/pipeline.py` | `ENABLE_CUDA_GRAPH` | The compatibility mode function and bundled inference code read the value at synthesis time. There is no active mainline UI caller today, so no replacement runtime contract is invented yet. |
| provider/session storage helpers | `AMADEUS_*_PATH` values | Helpers accept explicit path injection and subprocess tests supply isolated stores at their process boundary. |
| wallpaper scenario helpers | wallpaper/scenario overrides | Both wallpaper hosts resolve component-local media overrides without importing the heavyweight application settings facade. |
| `vn_player/runtime.py` | `VN_*` values | These describe one VN session, not the application startup snapshot. |
| `server/runtime_status.py` | build/workspace metadata | The launcher supplies diagnostic facts for the current process. |

## Compatibility notes

- `TTS_DEVICE=auto` (or `cuda`) still performs the existing local-LLM port
  probe. The resolved device is copied to `os.environ` because the bundled
  BigVGAN loader directly consumes that variable.
- `AMADUES_PRE_TRANSLATION_ENABLED` remains accepted as a deprecated spelling
  of `AMADEUS_PRE_TRANSLATION_ENABLED` at the pre-translation boundary.
- The legacy root GPT-SoVITS WebUI/API entry points and their conflicting
  `config.py` were removed. A future HTTP API should be designed around the
  current `server.app` contracts instead of reviving that compatibility layer.
