# Installation profiles and environment migration

The project uses `pyproject.toml` for direct dependencies and `uv.lock` for
resolved packages and builds. CI uses uv 0.12.8 and Python 3.12.10. The default
desktop interpreter is the checkout's `.venv`; launchers do not install packages.

## Tier and build selection

A **tier** is a recommended install preset whose ladder is encoded in the extras
(`vad` includes `voice`; each `local-<build>` includes `vad`); a **build** selects which Torch/Torchaudio
packages are locked. Terminology is fixed by the
[tier and build taxonomy](../TIER_TAXONOMY.md).

Run one complete command for the environment you want:

| Tier | Capability | Install | Verify |
|---|---|---|---|
| T1 core | Core Chat/Work | `uv sync --locked` | `--profile cpu` |
| T2 voice | Remote voice and audio I/O | `uv sync --locked --extra voice` | `--profile voice` |
| T3 vad | Real-time interruption | `uv sync --locked --extra vad --extra torch-cpu` | `--profile vad-cpu` |
| T4 embedded | Local GPT-SoVITS / Qwen3 ASR / wake word | `uv sync --locked --extra local-<build>` | see the build table |

Commands that also name `voice` and `vad` remain valid and select the same environment.

Builds. The `local-<build>` extras bundle the shared local-model dependency set;
`torch-cpu` is the bare CPU build. T1 and T2 take no build.

| Build | Platform | Extra | Tiers | Verify |
|---|---|---|---|---|
| `cu124` (reference) | Windows + NVIDIA | `local-cu124` | T4 | `--profile cu124`, add `--require-cuda-device` on a GPU machine |
| `cu128` (experimental) | Windows / Linux x86_64 | `local-cu128` | T4 | `--profile cu128`, add `--require-cuda-device` on a GPU machine |
| `mps` (experimental) | macOS Apple Silicon | `local-mps` | T4 | `--profile mps`, add `--require-mps-device` on a machine with MPS |
| `rocm` (experimental) | Windows + GPU in AMD's support matrix | `local-rocm` | T4 | `--profile rocm`, then `tools/rocm_sidecar/verify_gpu.py --compute` |
| `cpu` | Windows / Linux / macOS Apple Silicon (same wheel as `mps`) | `torch-cpu` | T3 | `--profile vad-cpu` |
| `cpu` | Windows / Linux / macOS Apple Silicon | `local-cpu` | T4 | `--profile t4-cpu` |

The platform column says where a build installs, not a cap: any installable build
may be chosen, and the README recommends one per platform. `local-cpu` exists so
that CPU-only features such as the wake word (SenseVoice + Silero) work without a
GPU; local TTS/ASR also install on it without a real-time promise. Outside macOS,
`TTS_DEVICE=auto` resolves to `cuda:0`, so set `TTS_DEVICE=cpu` with `local-cpu`.

Invoke the verifier with
`uv run --locked --no-sync python tools/verify_python_environment.py` and the
profile above. `--profile vad` detects which locked build is installed and fails
when Torch came from no build extra; `vad-cpu` requires the CPU build.
The cu124, cu128 and ROCm profiles can additionally use `--require-cuda-device` on a GPU machine.
An import or build check does not establish model inference or audio-device support.

The default T1 and T2 presets remain Torch-free (optional extras such as
`rag` with `torch-cpu` may still add Torch). T3 and T4 add Torch through the `vad` extra, which
transitively requires `silero-vad` → `torch`; this is why a build must be named
explicitly from T3 onward. `torch-cpu`, `local-cpu`, `local-cu124`, `local-cu128`,
`local-mps`, and `local-rocm` are mutually exclusive build selections. The CPU
build and optional RAG select Torch/Torchaudio 2.7.0, using the explicit CPU
index on Windows and Linux and the macOS registry wheel. Switching builds
replaces the previous build extra.

Optional [character RAG](character_rag.md) adds local embedding dependencies.
For core plus retrieval use `uv sync --locked --extra rag --extra torch-cpu`;
for an existing local-voice profile, append `--extra rag` to its complete command
without changing its Torch build selection. This does not add a new GPU baseline.

`uv sync` removes packages outside the selected configuration. To add development
tools, append `--extra dev` to the complete command for your intended capability.
Running only `uv sync --locked --extra dev` selects core plus development tools
and removes the optional voice/model stack. Ordinary application launch uses the
installed environment, without synchronizing or changing its selected extras.

Windows is the current reference platform. macOS core/voice has a separate CI
qualification path and needs PortAudio for PyAudio; the macos-14 candidate job
also installs `local-mps` and switches back through T3 vad. A successful install, import
or Electron build does not replace microphone, playback and desktop acceptance.
The reference cu124 profile is Windows-only. The cu128 candidate targets
Windows/Linux x86_64; the MPS candidate targets Apple Silicon macOS. Local model weights, reference audio
and dictionaries are external assets and are not downloaded by this installer.

Local ASR/TTS model loading enforces Hugging Face offline mode even when the
parent process sets `HF_HUB_OFFLINE=0` or Transformers/Hub have already been
imported. The shared boundary updates their cached flags and replaces cached Hub
HTTP sessions with the standard offline transport. Model loaders also request
local files explicitly. This boundary targets the pinned Hub 0.36.2 / Transformers
4.57.6 APIs; the cu124 ladder and ROCm CI exercise it with the real libraries,
including blocked remote requests and successful loading of a tiny local model.
Explicit asset acquisition runs separately; remote Chat/ASR/TTS API clients are
unchanged. The advisory exceptions remain temporary, not claims of patched packages.

## Experimental Linux Voice source build

On Ubuntu 24.04, install `build-essential`, `pkg-config` and `portaudio19-dev`,
then run `uv sync --locked --extra voice` and the `--profile voice` verifier.
The Linux-only AEC source override uses the official 1.0.1 sdist, forces and
verifies bundled Abseil 20240722.0, and stages setuptools/Meson writes in a
disposable source copy. It compiles the committed SWIG wrapper/proxies after
checking their version consistency; installation no longer invokes SWIG.
It does not modify system Abseil or change the Windows/macOS registry source.
See [AEC provenance](../vendor/aec-audio-processing.PROVENANCE.md)
for the artifact hash, patch, notices and removal conditions.

The Linux Voice CI uses a fresh uv cache, builds the path dependency and checks
the actual Meson options/subproject version. It qualifies this source-build
path on Ubuntu 24.04, not every Linux toolchain or real audio-device behavior.

### Upgrading an existing Linux Voice checkout

An older build may have regenerated only `audio_processing_wrap.cpp` and
`webrtc_audio_processing.py`, leaving the public `audio_processing.py` at a
different SWIG version. Updating Git preserves those local edits. The new
build rejects this mismatch before native library lookup or compilation,
rather than overwriting the user's files.

Inspect and back up any intentional changes under the vendor `src/` directory.
From the repository root **after updating to the fixed revision**, restore the
committed bindings:

```bash
git restore --source=HEAD -- vendor/aec-audio-processing/src
```

Remove the old generated
`vendor/aec-audio-processing/webrtc-audio-processing/install/` directory after
confirming it contains only the previous build's output. The new backend
excludes old build/install directories from staging and does not delete them
from the checkout. Then rerun `uv sync --locked --extra voice` and the Voice
verifier. For a source archive, unpack a fresh fixed archive instead of using
`git restore`.

For source-build qualification, run
`UV_CACHE_DIR="$(mktemp -d)" python3.12 tools/verify_aec_source_build.py`.
The tool requires Python 3.12 and pins the locked install, both distribution
builds, and the isolated wheel environment to that interpreter.

## Optional model interpreters and community configurations

A single default environment does not prohibit isolated model processes. Qwen ASR
and GPT-SoVITS can run as persistent sidecars while using the same `.venv` Python;
explicit `QWEN3_ASR_PYTHON` and `TTS_PYTHON` overrides remain advanced deployment
configuration rather than another default environment.

The experimental `local-rocm` selection uses AMD's fixed Windows ROCm 7.2.1 and
Torch 2.9.1 package URLs. The lock, clean install, imports, dependency check, and
failure reporting have been exercised. Historical community evidence records
RX 9070 XT sidecar ASR/TTS on another ROCm/PyTorch build. The fixed combination
still needs real ASR/TTS and lifecycle validation on a GPU in AMD's support matrix.
A Radeon 780M probe enumerated gfx1103 but crashed during its first FP32 operation;
device visibility alone is not acceptance. See `tools/rocm_sidecar/README.md`.

The `local-cu128` and `local-mps` candidates pin Torch/Torchaudio 2.7.0 and
reuse the shared `local-models` capability dependencies. Select a documented
build profile, not the shared capability extra on its own. All local profiles
continue using the existing offline model-loading boundary.

`--profile cu128` verifies the CUDA 12.8 package build without requiring a GPU;
add `--require-cuda-device` on NVIDIA hardware. `--profile mps` checks an Apple
Silicon environment and a Torch build with MPS support; add
`--require-mps-device` on a machine with accessible MPS. Availability checks do
not execute models. Hosted CI runs package/import and CPU/fake-device contracts,
then switches back through T3 vad to model-less T1 core in the same environment.

The application Qwen ASR configuration currently accepts `auto`, `cpu`, and
`cuda`, not `mps`. Issue #67's standalone MPS inference evidence must not be
presented as application-level ASR qualification. See
[Torch 2.7 candidates](torch27_candidates.md) for the device acceptance checklist
and optional FlashAttention wheel evidence. The cu124 reference and AMD's fixed
ROCm 2.9.1 build are retained until their respective replacements have evidence.

## Migrating an existing installation

1. Record the active interpreter, selected backends, package versions and external
   model locations. Keep the existing working `.venv_cu124` intact during testing.
2. Create a new environment from the lock and verify it. The clean-install helper
   `tools/verify_clean_python_install.ps1` targets a fresh path below `runtime/`.
   GPU model acceptance requires the matching voice/model extras and real models.
3. Compare the new environment with the working installation: Chat/Work, ASR,
   TTS, VAD, continuous playback, interruption and shutdown.
4. Once accepted, stop the backend and recreate the environment at the final
   `.venv` path. Do not copy or rename a venv; installed entry points may contain
   its original absolute interpreter path.
5. Switch the launcher and confirm actual interpreter selection. Until migration
   is accepted, an explicit `AMADEUS_PYTHON` override can select the preserved old
   interpreter. Restore any ASR interpreter/mode overrides together on rollback.

Reverting source commits does not undo package changes made by a prior sync.
Keep the old working environment as the explicit rollback target until the new
installation is accepted. Historical automatic environment-name discovery is
retired independently of how long a user keeps a local backup.
