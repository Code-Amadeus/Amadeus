# Experimental Windows ROCm sidecars

This is an opt-in developer preview for local Qwen3-ASR and GPT-SoVITS on a
supported AMD GPU. It is not part of Amadeus's Windows + NVIDIA release
baseline and does not claim support for every Radeon GPU or Ryzen APU.

The Host, ASR sidecar, and TTS sidecar use the same project `.venv`. “Sidecar”
means that model inference runs in persistent child processes; it does not
mean that another virtual environment is required. The process boundary keeps
model initialization, GPU failure, and JSONL transport separate while uv owns
one exact package selection.

## Qualification boundary

The fixed reference selection is:

- Windows 11 x64 and CPython 3.12;
- AMD's Windows ROCm 7.2.1 packages;
- `torch==2.9.1+rocm7.2.1`, `torchaudio==2.9.1+rocm7.2.1`, and
  `torchvision==0.24.1+rocm7.2.1`;
- a GPU listed in AMD's Windows PyTorch compatibility matrix.

AMD's documentation requires the matching 26.2.2 graphics driver for this
release and notes that Windows provides PyTorch support rather than the entire
ROCm stack:

- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/install/installrad/windows/install-pytorch.html>
- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/compatibility/compatibilityrad/windows/windows_compatibility.html>
- <https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/compatibility/compatibilityryz/windows/windows_compatibility.html>

Current evidence is deliberately narrower than that matrix:

- community history records successful sidecar ASR/TTS on an RX 9070 XT, but
  with a different ROCm/PyTorch build;
- the fixed 7.2.1 dependency selection resolves, installs, imports, and passes
  `uv pip check` in a clean project `.venv`;
- a 2026-09-05 probe on a Radeon 780M (gfx1103), which is absent from AMD's
  support matrix, enumerated the GPU but crashed in `amdhip64_7.dll` during the
  first FP32 tensor operation. Radeon 780M is therefore not a usable validation
  target for this profile;
- the fixed combination still needs clean-machine ASR/TTS and long-running
  validation on a supported AMD GPU.

## Install in one project environment

Keep a working CUDA environment intact while evaluating this candidate. Use a
separate checkout or worktree if the same computer also runs the cu124 profile;
each checkout still owns exactly one `.venv`.

```powershell
uv venv .venv --python 3.12
uv sync --locked --extra voice --extra vad --extra local-rocm
uv run --locked --no-sync python tools\verify_python_environment.py --profile rocm
uv run --locked --no-sync python tools\rocm_sidecar\verify_gpu.py --compute
```

Do not continue when the compute probe fails, even if PyTorch reports
`torch.cuda.is_available() == True`. PyTorch intentionally exposes HIP devices
through `torch.cuda`; the device string remains `cuda:0`, while
`torch.version.hip` distinguishes ROCm from NVIDIA CUDA.

`local-rocm`, `local-cu124`, and `torch-cpu` are mutually exclusive. Always
provide the complete target selection to `uv sync`; exact sync removes packages
that do not belong to the selected profile.

## Enable the child processes

After the compute probe passes, merge the values from
`tools/rocm_sidecar/voice-rocm.env.example` into the checkout's `.env`. Both
sidecars default to the current `.venv` interpreter. `QWEN3_ASR_PYTHON` and
`TTS_PYTHON` remain available only when an advanced deployment intentionally
uses another interpreter.

The baseline disables CUDA Graphs, NVIDIA-only BigVGAN kernels, and flash
attention. ROCm still uses the PyTorch device string `cuda:0`.

Validate the real persistent protocol before enabling wake word or continuous
voice. Use legally obtained model files and a short, known recording:

```powershell
uv run --locked --no-sync python tools\rocm_sidecar\verify_sidecar.py --repo . asr `
  --model assets\models\asr\qwen3-asr-0.6b `
  --audio <known-short-recording.wav> --language Chinese --repeat 2

uv run --locked --no-sync python tools\rocm_sidecar\verify_sidecar.py --repo . tts `
  --gpt <gpt-v3.ckpt> --sovits <sovits-v3.pth> `
  --reference <reference.wav> --reference-text <matching-text> `
  --text <short-test-text> --language ja --output runtime\rocm-tts-smoke.wav
```

Passing these commands proves package integrity, one GPU operation, and the
ASR/TTS JSONL contracts. It does not establish microphone, playback, AEC,
barge-in, multi-turn stability, performance, or support for another GPU.

---

## Windows ROCm 实验 sidecar

这是面向受 AMD 官方矩阵支持 GPU 的显式开发者预览，不属于当前 Windows +
NVIDIA 正式基线，也不表示所有 Radeon 或 Ryzen 核显均受支持。

Host、ASR sidecar 与 TTS sidecar 共用项目的同一个 `.venv`。sidecar 只表示模型
在常驻子进程中运行，用于隔离模型初始化、GPU 故障与 JSONL 协议；它不要求额外
虚拟环境。

安装与验证顺序是：选择 `local-rocm`、验证固定 Torch/HIP 版本、执行真实 GPU
矩阵计算，最后才测试 ASR/TTS sidecar。即使 `torch.cuda.is_available()` 返回 True，
计算 probe 失败也必须停止。当前 Radeon 780M 实测可以被枚举，但首次 FP32 计算会
在 `amdhip64_7.dll` 中崩溃，因此不能作为可用 ROCm 目标。

`local-rocm`、`local-cu124` 与 `torch-cpu` 互斥。评估机器如果还要保留工作的
cu124 环境，应在另一个 checkout/worktree 中测试，避免改动正在使用的基线。
