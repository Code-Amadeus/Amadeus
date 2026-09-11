<div align="center">

<h1>Amadeus: Real-Time Multimodal AI Agent for Desktop Interaction</h1>

<p>An interaction layer for a local AI OS</p>

<p>
  <a href="./README.md">中文</a> | English
</p>

<img src="./assets/header-strip.en.svg" width="880" alt="TALK interruptible voice · EMBODY speech-synced performance · ACT delegated providers · CONTROL resumable and steerable"/>

<p>
  <a href="https://www.bilibili.com/video/BV1783G6hEYY/"><img src="https://img.shields.io/badge/demo-Bilibili-2f624a?labelColor=061710&logo=bilibili&logoColor=61eeb6" alt="Bilibili demo"/></a>
  <a href="./assets/architecture-overview-crt.svg"><img src="https://img.shields.io/badge/architecture-current-184b36?labelColor=061710" alt="Current architecture"/></a>
  <img src="https://img.shields.io/badge/version-0.1_%CE%B1-2f624a?labelColor=061710" alt="Amadeus 0.1 alpha"/>
  <img src="https://img.shields.io/badge/profiles-core%20%2F%20voice%20%2F%20CPU%20VAD%20%2F%20cu124-2f624a?labelColor=061710" alt="Installation profiles: core, voice, CPU VAD, cu124"/>
  <img src="https://img.shields.io/badge/license-AGPL--3.0-272018?labelColor=061710" alt="License"/>
</p>

[![The Provider workspace in Amadeus, with task state, streaming results, and the embodied scene visible together](./assets/demo/provider-runtime.jpg)](https://www.bilibili.com/video/BV1783G6hEYY/)

<sub>Click the image to watch the full 10-minute demo</sub>

</div>

> [!IMPORTANT]
> This repository contains buildable, runnable source. The current version is
> **0.1 α**, not a packaged desktop release.
> Amadeus first-party code is open-source under the
> [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE).
> Third-party code and external assets retain their own terms.
>
> **Want to run it first?** See [Quick start](#quick-start). For an introduction,
> start with [What Amadeus is trying to solve](#what-amadeus-is-trying-to-solve).

## What Amadeus is trying to solve

Voice assistants, desktop characters, and execution agents usually live in
separate windows: one chats, one performs, and another works in a terminal or
browser. Once a long task starts, it is difficult to see what is happening,
which permission is needed, or whether the work can recover after a failure.

Amadeus connects those experiences into one loop:

1. **Talk — communicate naturally:** speak or type, with interruption across generation, synthesis, and physical playback.
2. **Embody — make the agent present:** voice, subtitles, lip sync, expression, and scene behavior share one playback timeline.
3. **Act — delegate real work:** the main role delegates to registered Work Providers instead of receiving every tool directly.
4. **Control — stay in charge:** Projects, Drafts, Artifacts, progress, permissions, diffs, and results remain visible and recoverable.

The character communicates and narrates, specialized Providers execute, and
the Host owns identity, state, permissions, persistence, and recovery.

## Demo highlights

| Real-time conversation and performance | Scene-aware working state |
|---|---|
| ![The character speaking in a real-time voice conversation with synchronized subtitles](./assets/demo/conversation.jpg) | ![The character moves into a working scene and reports the Provider's research result](./assets/demo/scene-runtime.jpg) |
| Voice, subtitles, lip sync, and expression follow actual playback. | Background work drives character behavior, scene state, and result narration. |

The video demonstrates real-time voice, character performance, desktop scenes,
Browser / OpenClaw tasks, and a paper-research flow. The desktop UI, Provider
integration, and asset boundaries have continued to evolve, so the video is a
product slice rather than a pixel-exact installation preview.

> [!NOTE]
> Characters, scenes, voices, and other third-party material visible in the
> demo are used only to present the prototype. They are not granted rights by
> the Amadeus code license. Public source does not include character packs,
> model weights, reference audio, or authoring intermediates without confirmed
> redistribution rights.

## Current capabilities

| Area | Current public source |
|---|---|
| **Interruptible real-time conversation** | Shared microphone lifecycle, independent Wake / Conversation ASR, two-stage endpointing, AEC / barge-in, and interruption across LLM, TTS, and physical playback. |
| **Remote Main Chat and local voice** | DeepSeek V4 Flash Main Chat; Qwen3-ASR / SenseVoice; embedded GPT-SoVITS v3 streaming synthesis, continuous playback, and mouth values published before matching PCM windows. |
| **Character and desktop presentation** | SpriteForge graph state, a KTX2/PixiJS runtime, subtitle, lip-sync, and emotion timing; Chat, Work, and headless startup remain available without a character pack. |
| **Provider Runtime** | Browser, Codex App Server / Direct Codex, and optional OpenClaw are current Providers. Claude CLI is a committed future direct Provider. |
| **Durable Work control plane** | Projects, default Drafts, WorkItems / Attempts, Continue / Retry, restart recovery, permissions, the Artifact Registry, and structured diffs. |
| **Artifacts and AUIP** | A Work Artifact can be previewed, opened, or attached as a bounded AUIP AppSession so Amadeus can interact with it without turning narration into execution authority. |
| **Unified settings** | Models, Voice, Providers/MCP, vision, character-pack status, and chat appearance are managed in Electron Settings. |

MCP and Skills remain compatible-Provider capabilities even when they share a
Host registry; **Main Chat cannot invoke MCP tools directly**. Remote DeepSeek
is the Main Chat baseline; remote ASR/TTS remain explicit compatibility routes.
A local voice failure never silently uploads data or creates a second billable request.

## Repository map

```text
electron/       Electron main, preload, React renderer, and Settings
server/         authenticated local backend, Host control plane, and AUIP
core/           Main Chat runtime and session integration
agent_host/     Provider contracts, adapters, Work identity, and capabilities
asr/            Conversation / Wake recognition backends
tts/            synthesis backends, sentence pipeline, playback, and mouth signal
render/         SpriteForge runtime adapter and PixiJS renderer
wallpaper/      Electron/Lively hosts and Win32 desktop placement
vn_player/      experimental VN Player integration
assets/         Git-owned UI assets and external runtime-asset destinations
release/        public-source selection, provenance, and deterministic archive policy
```

`main.py` is not an application entry; it prints a retirement notice. The
Python entry is `uv run --locked --no-sync python -m server.app --port 17777`,
and the desktop entry is `run_electron_utf8.bat` on Windows or
`npm run electron:dev` from `electron/` on macOS. Both discover `.venv`
automatically; choose an installation profile supported on your platform.

## Architecture

[![Current Amadeus architecture: Host authority, Work Providers, Provider-scoped MCP/Skills, AUIP AppSessions, voice, and SpriteForge presentation](./assets/architecture-overview-crt.svg)](./assets/architecture-overview-crt.svg)

Three separations are deliberate:

- Main Chat, Work Providers, and AUIP applications are distinct authority domains.
- A shared MCP/Skill registry does not expose those capabilities directly to Main Chat.
- Artifacts, identity, permission, and receipts are Host-verified facts; model narration cannot replace them.

Codex currently connects through App Server or Direct transport without the
retired Locus gateway. Claude CLI will join the same boundary later as an
independent direct Provider, not by restoring Locus.

## AUIP application sessions

AUIP is Amadeus's cooperative application protocol. It is not a Provider, MCP,
or Main Chat tool system. It addresses a different problem: once Work has
created a runnable Artifact, how can Amadeus continue collaborating with that
application while preserving Host authority?

```text
verified Work Artifact
  -> Host prepares a short-lived attach ticket
  -> application registers declared state/events/actions
  -> bounded AppSession
  -> character receives scoped projection and action receipts
```

- The ticket binds the current Session, an immutable Artifact reference, and a TTL. The application submits an Artifact id, not an arbitrary path.
- The Host validates workspace ownership, type, digest, and launch entry, and owns AppSession identity, revision, and action authority.
- The application may publish only declared state and semantic events and receive only declared, authorized typed actions.
- AUIP grants no `work.*`, `provider.*`, `tts.*`, arbitrary filesystem, or other-Session authority.
- Disconnects become visible state and invalidate pending actions instead of continuing against stale application state.

The current schema is `amadeus.auip/v0` and is implemented here. See
[AUIP application sessions](docs/auip_application_sessions.md). The separate
[Code-Amadeus/auip](https://github.com/Code-Amadeus/auip) repository is still a
public namespace placeholder; this release does not claim a standalone SDK or
conformance suite.

## Quick start

Dependencies are grouped into four capability tiers. Start with the minimal L1
installation, then add the tiers you need. Torch enters at L3/L4 in the default
ladder; optional RAG also adds local embedding/Torch dependencies. Windows is
the reference platform, and macOS L1/L2 installation and CI are validated
separately. Desktop, microphone, and playback behavior still need real-device
acceptance. L3 offers CPU VAD with **no NVIDIA GPU requirement**. The current L4
cu124 profile targets Windows + NVIDIA. Windows ROCm 7.2.1 has a mutually exclusive
`local-rocm` experimental lock and validation tools, but end-to-end acceptance on
supported AMD hardware remains incomplete. RTX 50-series cu128 remains a community
configuration record.

All profiles use [uv](https://docs.astral.sh/uv/) and Python 3.12; CI pins uv 0.12.8.

Linux users should start with [Linux (experimental)](#linux-experimental) below.

| Tier | Capability | Platform | Installation |
|---|---|---|---|
| L1 core | Text Chat, Work, Providers, and character rendering | Windows / macOS | `uv sync --locked` |
| L2 voice | Remote TTS, playback, lip-sync, microphone, and remote ASR | Windows / macOS | `uv sync --locked --extra voice` |
| L3 CPU VAD | Real-time interruption while the character is speaking | CPU; no NVIDIA GPU required | `uv sync --locked --extra voice --extra vad --extra torch-cpu` |
| L4 local-cu124 | Local GPT-SoVITS, Qwen3 ASR, and wake word | Windows + NVIDIA GPU | `uv sync --locked --extra voice --extra vad --extra local-cu124` |
| Experimental local-rocm | Local GPT-SoVITS / Qwen3 ASR sidecars | Windows + GPU in AMD's official support matrix | `uv sync --locked --extra voice --extra vad --extra local-rocm` |

The four default tiers and the ROCm experiment use **the same `.venv`**. Give the
complete target configuration each time: `uv sync` is exact and removes packages
from omitted tiers. `torch-cpu`, `local-cu124`, and `local-rocm` are pairwise
incompatible. To switch builds, replace the build extra while keeping `voice`
and `vad`. See [installation profiles and migration](docs/install_profiles.md).

- Main Chat defaults to remote DeepSeek. llama.cpp is an optional local LLM
  profile under [Compatibility routes](#compatibility-routes), not an installation prerequisite.
- L2 without VAD uses energy-based endpoint detection. Adding VAD enables
  Silero endpointing and interruption.
- On Windows, check each tier with
  `uv run --locked --no-sync python tools/verify_python_environment.py --profile <cpu|voice|vad-cpu>`
  (`ci` shares the core import checks). For L4, use `--profile cu124 --require-cuda-device`.
  For ROCm, use `--profile rocm`, then run the GPU compute probe. Import/build
  checks do not replace real model and audio-device tests.
- L1 is sufficient for text-only/headless use. Start the backend with
  `uv run --locked --no-sync python -m server.app --port 17777`.
  Set `TTS_BACKEND=disabled` and disable Wake for a strict text-only profile.

### Reference hardware

**L1/L2 (Windows / macOS)**

- CPython **3.12**, managed by uv; no system Python installation required
- Node.js **22** (`22.21.1` is the current reference)
- No GPU required

**Additional requirements for L4 cu124 (Windows local models)**

- CUDA 12.4-compatible NVIDIA GPU, targeting **8 GiB VRAM**
- **16 GiB system RAM minimum; 32 GiB recommended**

Peak memory depends on local ASR/TTS models and concurrency. The target describes
the remote-Chat/local-voice profile. An optional local LLM needs additional
memory according to its model, quantization, context, and GPU offload.

### Base environment (L1/L2, Windows / macOS)

Install uv with `winget install astral-sh.uv` on Windows or `brew install uv` on
macOS. For L2 on macOS, install PortAudio first with `brew install portaudio`;
PyAudio builds from source there. Then clone and choose a tier with the same
commands on both platforms:

```bash
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus

uv venv .venv --python 3.12
uv sync --locked                              # L1 core
uv sync --locked --extra voice                # L2 voice (optional)
```

Keep the environment named `.venv`. Electron discovers its interpreter
automatically (`Scripts/python.exe` on Windows, `bin/python3` on macOS), without
requiring an `AMADEUS_PYTHON` override.

Build the Electron frontend on either platform:

```bash
cd electron
npm ci
npm run build
cd ..
```

`npm ci` uses the project postinstall hook to fetch the pinned Electron runtime.
Where network access requires it, configure npm/Electron mirrors, such as
`ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`.

### Linux (experimental)

**Linux is currently an experimental source deployment path, with full platform
support still pending.** [Phase 1 Linux CI (#63)](https://github.com/Code-Amadeus/Amadeus/pull/63)
has passed locked L1 + dev installation, environment imports and model-less
dependency checks, basic contract tests, Ruff, architecture-view checks, and the
Electron build on Ubuntu 24.04. A separate Voice source-build job checks locked
installation, AEC import, bundled Abseil selection and related contracts. CI does
not cover the Electron GUI, real audio devices, VAD/local model inference,
Wayland sessions, or wallpaper integration.

The community has reported desktop and character-rendering results on Arch Linux /
Wayland. These reports do not establish compatibility across all distributions or
desktop environments. See [Linux tracking issue #64](https://github.com/Code-Amadeus/Amadeus/issues/64)
for environment records, known issues, and follow-up work.

Install Git, [uv](https://docs.astral.sh/uv/) (`0.12.8` in CI), and Node.js 22
(`22.21.1` in CI), then start with L1, which needs no GPU or voice packages:

```bash
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus
uv venv .venv --python 3.12.10
uv sync --locked
cp .env.example .env
```

Edit `.env`, provide `DEEPSEEK_API_KEY`, set `TTS_BACKEND=disabled`, and keep
`WAKE_ENABLED=false` to try the text-only path first. Verify the environment from
the project root:

```bash
uv run --locked --no-sync python tools/verify_python_environment.py --profile cpu
```

Build and launch Electron from a Linux graphical desktop session. The launcher
automatically discovers `.venv/bin/python3` and starts the backend:

```bash
cd electron
npm ci
npm run build
npm run electron:dev
```

For a headless backend instead, run this from the project root:

```bash
uv run --locked --no-sync python -m server.app --port 17777
```

For remote voice, recording and playback, install L2 in the same `.venv`.
On Ubuntu 24.04, install the native prerequisites used by CI first; other Linux
distributions need their corresponding package names:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y build-essential pkg-config portaudio19-dev
uv sync --locked --extra voice
uv run --locked --no-sync python tools/verify_python_environment.py --profile voice
```

Before adding voice or local models, consider these experimental boundaries:

- **Voice / AEC:** Linux uses vendored source based on the official
  `aec-audio-processing==1.0.1` sdist and forces bundled Abseil 20240722.0 to avoid
  selecting an incompatible modern system Abseil. The system installation is
  unchanged; Windows/macOS retain their registry artifacts. Source identity,
  the isolated patch and removal conditions are in [AEC provenance](vendor/aec-audio-processing.PROVENANCE.md).
  Build/import success does not qualify real-device echo cancellation or full voice interaction.
- **VAD / NVIDIA:** community inference results exist, but Linux CI does not cover
  them. The current CPU/cu124 PyTorch index selection only applies on Windows;
  reproducible Linux build profiles still need work.
- **Desktop / wallpaper:** GUI and Wayland compositor integration need separate
  acceptance. Community GNOME results do not establish support for niri, KDE, or
  other desktops.

### VAD and local models

Use the same `.venv` as L1/L2 and select the complete capability/build combination.

**L3 CPU VAD — real-time interruption:** Torch enters as a CPU build.

```powershell
uv sync --locked --extra voice --extra vad --extra torch-cpu
uv run --locked --no-sync python tools\verify_python_environment.py --profile vad-cpu
```

**L4 local-cu124 — local voice models:** select the CUDA profile in that same
environment. On Windows, `[tool.uv.sources]` routes this extra's Torch/Torchaudio
packages to the PyTorch cu124 index.

```powershell
uv sync --locked --extra voice --extra vad --extra local-cu124
uv run --locked --no-sync python tools\verify_python_environment.py --profile cu124 --require-cuda-device
```

The L4 profile pins `torch==2.6.0+cu124`, `torchaudio==2.6.0+cu124`, and the local
model dependencies. This is the current qualified local-model profile.

**Experimental local-rocm (Windows):** the same `.venv` can select AMD's official
ROCm 7.2.1, Torch/Torchaudio 2.9.1, and the local-model dependencies. Persistent
Qwen ASR and GPT-SoVITS sidecars still use that environment's interpreter by
default. This option is disabled by default and conflicts with cu124/CPU Torch
builds. After installation, run the environment check and an actual FP32 GPU
compute probe before model tests. Do not proceed after a compute failure, even
if `torch.cuda.is_available()` returns True. Commands, hardware-matrix references,
and acceptance limits are in the [Windows ROCm sidecar guide](tools/rocm_sidecar/README.md).

The maintainer's Radeon 780M (gfx1103) was detected by ROCm but crashed in an AMD
HIP DLL on the first FP32 operation. It is absent from AMD's official ROCm 7.2.1
Windows PyTorch support matrix and is not treated as a usable target.

> **GeForce RTX 50 series (Blackwell, community-validated configuration):**
> the current `torch==2.6.0+cu124` profile is incompatible with RTX 50-series
> GPUs and cannot run the local CUDA voice models. Update the NVIDIA driver and
> use the community-validated PyTorch 2.7.0 CUDA 12.8 combination instead.
>
> Run these commands only in a separate experimental project environment, such
> as `.venv_cu128`; keep the qualified `.venv` and its cu124 lock intact.
>
> ```powershell
> uv venv .venv_cu128 --python 3.12
> uv pip install --python .venv_cu128 --reinstall `
>   torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 `
>   --index-url https://download.pytorch.org/whl/cu128
> ```
>
> This installs only the reported PyTorch combination, not the complete Amadeus
> environment. It has not passed the project's full clean-install, ASR/TTS/VAD,
> and Electron regression gates. `uv.lock` and the `--profile cu124` verifier
> still require `torch==2.6.0+cu124`; this is not a replacement for that baseline.

### Install external runtime assets

[Optional character RAG](docs/character_rag.md) is off by default and works with
remote and local Main Chat. It includes a buildable Chinese/Japanese starter
corpus and supports personal knowledge directories. Settings shows applied
thresholds and loading state. RAG adds local embedding/Torch dependencies;
the guide covers setup, diagnostics and evaluation limits.

The full local-voice profile needs the Qwen ASR and GPT-SoVITS v3 voice packs.
The visual and character packs are optional:

```powershell
uv run --locked --no-sync python tools\external_assets.py verify C:\Downloads\amadeus-asr-qwen3-0.6b.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-asr-qwen3-0.6b.zip
uv run --locked --no-sync python tools\external_assets.py verify C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip

# Optional scene and KTX2 character animation
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-visual-runtime.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-character-kurisu.zip
uv run --locked --no-sync python tools\external_assets.py status
```

If a prepared Qwen pack is unavailable, download the upstream snapshot into
the same canonical location. Runtime inference remains offline and will not
start an implicit download when the microphone is opened:

```powershell
uv run --locked --no-sync python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-0.6B', local_dir='assets/models/asr/qwen3-asr-0.6b')"
```

The Japanese GPT-SoVITS frontend prepares an OpenJTalk dictionary on first
use. Prewarm it once if the normal application launch must remain offline:

```powershell
uv run --locked --no-sync python -c "import pyopenjtalk; print(pyopenjtalk.g2p('準備完了'))"
```

### Configure and launch

Copy `.env.example` to `.env` (`Copy-Item .env.example .env` on Windows;
`cp .env.example .env` on macOS), provide the DeepSeek API key, then review Settings:

- **Models:** `deepseek`, the official endpoint, `deepseek-v4-flash`, and an API key;
- **Voice:** remote TTS/ASR endpoints, such as MiMo; the L4 local stack also needs a Qwen model directory, GPT-SoVITS **v3** checkpoints, reference audio/text, microphone, AEC, and barge-in;
- **General:** optional character-pack status and presentation settings.

Launch Amadeus:

- Windows: `run_electron_utf8.bat`, the shared launcher for L1–L4; it discovers `.venv` automatically.
- macOS: `cd electron && npm run electron:dev`.

Use **Restart backend to apply** after changing startup settings. A
**Not installed** character pack is healthy and does not disable Chat, Work,
or headless startup.

The default B2 AppSession action path does not block first-time setup. Chat and
Settings still start without supported AUIP action-model credentials; application
actions remain blocked, and Settings displays the missing capability.

## Compatibility routes

### Optional local LLM

To use llama.cpp instead of the remote Main Chat baseline, set
`LLM_PROVIDER=local`, configure its executable/GGUF or an existing
OpenAI-compatible endpoint, and start it when needed:

```powershell
.\start_llm_server.bat
```

LM Studio, Ollama, llama-cli, and hybrid profiles remain available, but none is
an automatic fallback after a DeepSeek failure.

### Optional remote model recommendations

These are recommended profiles for the current APIs. They do not change the
role split above, and Amadeus never switches Providers silently after an endpoint
failure:

| Responsibility | Recommended profile | Current boundary |
|---|---|---|
| Main Chat API | DeepSeek-V4-Flash-0731: `DEEPSEEK_BASE_URL=https://api.deepseek.com` and `DEEPSEEK_MODEL_NAME=deepseek-v4-flash` | `deepseek-v4-flash` is the stable API alias currently pointing to the 0731 release; the dated version is not used as the runtime model id. |
| Multimodal / Vision | Prefer `gemini-3.7-flash`; use `gemini-3.5-flash` as a more conservative compatibility profile | Host-owned visual context performs capture in-process, while image delivery still follows the Main Chat Provider. Independent Gemini Vision API routing is not implemented and does not imply restoring the retired Gemini Live sidecar. |
| Work execution Provider | Prefer Codex App Server; use the optional OpenClaw Gateway second | This is a recommendation order, not an automatic failure fallback. Browser remains the specialized Provider for web tasks. |
| Work execution model | Codex App Server may explicitly select a GPT-5.6-family model or `deepseek-v4-flash` | The execution model belongs to the Work Provider and does not share Main Chat routing or credentials. |
| AUIP runtime action decisions | `AUIP_ACTION_PROVIDER=openai`, `AUIP_ACTION_MODEL=gpt-5.6-terra`, `AUIP_ACTION_REASONING_EFFORT=low`, and `AUIP_ACTION_SERVICE_TIER=fast` | This model decides AppSession actions and participation; it is not the execution Provider that authors an AUIP Artifact. `fast` requires availability for the API project. |

## External models and runtime assets

Model weights, reference audio, character packs, and large or copyright-
sensitive media are distributed separately. The source repository keeps the
required icons, default wallpaper, schemas, validators, and installation tool.

The current directory contracts are `asr-qwen3-0.6b`,
`voice-kurisu-gpt-sovits-v3`, `visual-runtime`, and `character-kurisu`. The
first two form the full local-voice profile; the latter two affect only scene
and character presentation.

```powershell
uv run --locked --no-sync python tools\external_assets.py verify C:\path\to\asset-bundle.zip
uv run --locked --no-sync python tools\external_assets.py install C:\path\to\asset-bundle.zip
uv run --locked --no-sync python tools\external_assets.py status
```

A bundle can be installed from any tier: `external_assets.py` uses only the
Python standard library. Running local voice models additionally requires the
matching model dependencies and hardware; installing a bundle alone does not
add them. See [installation profiles](docs/install_profiles.md) for the qualified
cu124 profile and the ROCm experimental boundary.

A SpriteForge character package ultimately lands at:

```text
assets/spriteforge/runtime/kurisu/
  runtime_manifest.json
  graph_config.json
  spriteforge_mouth_config.json
  textures/
```

The installer preserves the canonical `assets/...` layout, verifies SHA-256,
skips identical files, and rejects unexpected overwrites. See
[external asset bundles](docs/external_asset_bundles.md) and the
[character-pack contract](docs/character_pack_authoring.md).

### Wallpaper mode (Lively Wallpaper recommended)

On Windows, the open-source
[Lively Wallpaper](https://github.com/rocksdanister/lively) is the recommended
host for Amadeus's web wallpaper; Wallpaper Engine remains compatible. Start
Amadeus, add the local webpage URL below to Lively (WebView2 is recommended),
then click **Wallpaper** in the Amadeus sidebar:

```text
http://127.0.0.1:17777/wallpaper/lively/index.html
```

This stable entry discovers the actual asset and bridge ports automatically
and waits in place while wallpaper mode is off. Do not hard-code `17778` or
`17797`. For diagnostics, run
`uv run --locked --no-sync python tools\run_wallpaper_engine_bridge.py` and use the printed `Lively URL`.
See the [Lively entry guide](wallpaper/lively/README.md).

macOS has no corresponding Lively/Wallpaper Engine desktop host. When
**Wallpaper** is activated, Electron hosts the full scene at the desktop level
and uses a separate transparent window for the interactive Canvas. The scene
remains click-through so it does not block Finder desktop icons. This is a
community real-device candidate, not an official macOS support claim;
dependency and CI work is tracked by
[#46](https://github.com/Code-Amadeus/Amadeus/pull/46), and signing,
notarization, and an installer are not included yet.

## Configuration ownership

Startup values use one precedence order:

1. Parent-process environment variables (highest authority; shown as locked in the GUI)
2. Electron desktop settings
3. Repository-root `.env`
4. Defaults in `config/settings.py`

Settings never rewrites `.env`. Ordinary models, voice, microphones,
Providers/MCP, vision, avatars, and character-pack status belong in the GUI;
advanced diagnostics, experimental thresholds, and test-only flags remain in
`.env`. Secrets use the operating system's `safeStorage` encryption. See
[configuration ownership](config/README.md) and
[local instance authentication](docs/local_instance_authentication.md).

## Current release boundaries

| Scope | Status |
|---|---|
| L1/L2 (text + remote voice) | Source deployment on Windows and macOS; Windows is the reference platform, macOS L1/L2 has separate CI, and desktop/audio behavior still needs real-device acceptance |
| Linux (experimental) | Ubuntu 24.04 CI covers L1, L2 Voice source builds and the Electron build; GUI, real audio devices, GPU and wallpaper still need acceptance. See [Linux setup](#linux-experimental) |
| L3 CPU VAD | No NVIDIA GPU required; uses an explicit CPU build selection |
| L4 cu124 (local CUDA 12.4 voice) | Windows + NVIDIA; follows the qualified local-model configuration |
| AMD ROCm 7.2.1 | Single-`.venv` experimental lock, sidecar adapters and failure reporting; acceptance on supported AMD hardware remains incomplete |
| RTX 50-series cu128 | Community configuration record without a formal lock or full regression qualification |
| 8 GiB VRAM / 16–32 GiB RAM | Target configuration; actual use depends on model selection |
| Remote DeepSeek Main Chat | First-release default profile |
| Remote ASR / TTS | Explicit compatibility path, never a silent fallback |
| Electron installer | Not provided yet; launch from source |
| macOS Electron wallpaper host | Community real-device candidate; dependency/CI tracked by #46, with no signing, notarization, or installer yet |
| Docker | Not a supported desktop installation path |
| SpriteForge character pack | Externally distributed; source starts without it |
| VTS | Disabled-by-default compatibility route |
| VN Player | Experimental |
| Wallpaper mode | Windows hosts only (Lively / Wallpaper Engine); unavailable on other platforms |
| PyQt / old wallpaper hosts | Retired from public mainline |
| Claude CLI Provider | Committed future mainline Provider; no live caller yet |

## Development and contribution

```powershell
uv sync --locked --extra dev      # Core + dev tools; removes unselected voice/model tiers
# To retain voice/models, append --extra dev to the complete installation command
uv run --locked --no-sync python tools\verify_python_environment.py --profile ci
uv run --locked --no-sync python -X utf8 tools\run_tests.py

cd electron
npm ci
npm run build
npm audit --audit-level=high
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md) before
submitting a change. Product semantics, authority, protocols,
Providers/MCP/Skills, Projects/Drafts/Artifacts, or AUIP changes should start
with an Issue. Small fixes, documentation, tests, and presentation-only UI
changes may open a PR directly. Report security issues privately under
[SECURITY.md](SECURITY.md).

## Public history and license

The public repository begins with one prepared root commit. Internal development
commits, experimental branches, deleted character media, models, credentials,
sessions, personal paths, and original co-author metadata were not migrated.
The source itself remains included according to the reviewed release boundary.

Amadeus first-party source and modifications are open-source under the
[GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE). Third-party
components retain their own licenses, recorded under [LICENSES](LICENSES/README.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The code license grants no
automatic rights to character, model, reference-audio, or external asset packs.

## Related projects

- [Aqua-TTS](https://github.com/Lucas1479/Aqua-TTS): an MIT-licensed low-latency GPT-SoVITS v3 inference runtime. Amadeus does not require Aqua to start today.
- [Amadeus SpriteForge](https://github.com/Code-Amadeus/amadeus-spriteforge): the public namespace for the character authoring and graph/KTX2 toolchain; currently a release placeholder.
- [AUIP](https://github.com/Code-Amadeus/auip): the public namespace for the application-session / typed-action protocol; currently a release placeholder.
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS): the embedded speech-synthesis inference foundation.
- [OpenClaw](https://github.com/openclaw/openclaw): an optional external Work gateway.

<details>
<summary>Star History</summary>
<br />
<p align="center">
  <a href="https://github.com/Code-Amadeus/Amadeus/stargazers">
    <img src="./assets/star-history.svg" alt="Amadeus Star History" width="620" />
  </a>
</p>
</details>

---

<div align="center"><em>El Psy Kongroo.</em></div>
