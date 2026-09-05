# Installation profiles and environment migration

The project uses `pyproject.toml` for direct dependencies and `uv.lock` for
resolved packages and builds. CI uses uv 0.12.8 and Python 3.12.10. The default
desktop interpreter is the checkout's `.venv`; launchers do not install packages.

## Capability and build selection

Run one complete command for the environment you want:

| Capability | Install | Verify |
|---|---|---|
| Core Chat/Work | `uv sync --locked` | `--profile cpu` |
| Remote voice and audio I/O | `uv sync --locked --extra voice` | `--profile voice` |
| CPU VAD | `uv sync --locked --extra voice --extra vad --extra torch-cpu` | `--profile vad-cpu` |
| Windows cu124 local models | `uv sync --locked --extra voice --extra vad --extra local-cu124` | `--profile cu124` |
| Experimental Windows ROCm local models | `uv sync --locked --extra voice --extra vad --extra local-rocm` | `--profile rocm`, then `tools/rocm_sidecar/verify_gpu.py --compute` |

Invoke the verifier with
`uv run --locked --no-sync python tools/verify_python_environment.py` and the
profile above. `--profile vad` checks VAD capability imports without requiring
a particular Torch build; `vad-cpu` also verifies the qualified CPU build.
The cu124 and ROCm profiles can additionally use `--require-cuda-device` on a GPU machine.
An import or build check does not establish model inference or audio-device support.

Core and voice remain Torch-free. CPU VAD needs no NVIDIA GPU. `torch-cpu`,
`local-cu124`, and `local-rocm` are pairwise incompatible build selections.
Switching builds replaces the previous build extra while preserving `voice` and
`vad` in the complete command.

`uv sync` removes packages outside the selected configuration. To add development
tools, append `--extra dev` to the complete command for your intended capability.
Running only `uv sync --locked --extra dev` selects core plus development tools
and removes the optional voice/model stack. Ordinary application launch uses the
installed environment, without synchronizing or changing its selected extras.

Windows is the current reference platform. macOS core/voice has a separate CI
qualification path and needs PortAudio for PyAudio. A successful install, import
or Electron build does not replace microphone, playback and desktop acceptance.
The CUDA local-model profile is Windows-only. Local model weights, reference audio
and dictionaries are external assets and are not downloaded by this installer.

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

The README records a community RTX 50-series configuration using Torch 2.7.0,
Torchaudio 2.7.0 and torchvision 0.22.0 from the cu128 index. It has no accompanying
full project regression report. It is a candidate for a separate, mutually
exclusive NVIDIA build selection, not a replacement for the cu124 lock. Framework
wheel installation alone does not install the complete Amadeus model stack.

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
