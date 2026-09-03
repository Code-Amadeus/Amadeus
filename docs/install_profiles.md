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

Invoke the verifier with
`uv run --locked --no-sync python tools/verify_python_environment.py` and the
profile above. `--profile vad` checks VAD capability imports without requiring
a particular Torch build; `vad-cpu` also verifies the qualified CPU build.
The cu124 profile can additionally use `--require-cuda-device` on a GPU machine.
An import or build check does not establish model inference or audio-device support.

Core and voice remain Torch-free. CPU VAD needs no NVIDIA GPU. `torch-cpu` and
`local-cu124` select different builds and cannot be enabled together. Switching
from CPU VAD to local models replaces `--extra torch-cpu` with
`--extra local-cu124`; preserve `voice` and `vad` in the command.

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

A single default environment does not prohibit explicitly selected model
interpreters. Qwen ASR retains `QWEN3_ASR_PYTHON` with
`QWEN3_ASR_MODE=sidecar`. An explicit interpreter entry is configuration, and its
model dependencies and load result still need validation. Merely finding the
default `.venv` does not establish that Qwen ASR is installed.

AMD ROCm integration materials are under review. They describe historical
RX 9070 XT sidecar success and a proposed ROCm 7.2.1 / Torch 2.9.1 reference
environment; the latter has not completed clean-machine end-to-end qualification.
The TTS sidecar adapter from those materials is not part of this dependency
migration. Do not install AMD wheels into `local-cu124` or treat these materials
as an officially qualified AMD profile.

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
