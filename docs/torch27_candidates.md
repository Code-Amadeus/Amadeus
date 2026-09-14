# Torch 2.7 installation candidates

The CPU build uses Torch/Torchaudio 2.7.0. `local-cu128` (Windows/Linux x86_64)
and `local-mps` (Apple Silicon) are explicit, experimental installation choices.
Windows cu124 remains on 2.6.0 and Windows ROCm remains on AMD's 2.9.1 build.
Core and remote voice still require no Torch. See [installation profiles](install_profiles.md)
for complete commands; every build selection uses the existing single `.venv`.

This is installation qualification, not promotion of a new GPU baseline.
The maintainer has used a cu128 environment but has not supplied a verified
Torch 2.7 model regression. Issue [#67](https://github.com/Code-Amadeus/Amadeus/issues/67)
reports independent M4 Max Qwen-ASR results with 2.7.0. Existing GPT-SoVITS MPS
evidence used 2.6.0 and does not transfer automatically to this version.

## Application integration boundary

GPT-SoVITS already supports MPS device selection. Application Qwen ASR currently
accepts `auto`, `cpu`, or `cuda`; both its in-process and sidecar loaders select
CPU/CUDA. Setting `QWEN3_ASR_DEVICE=mps` is not supported. The MPS dependency
profile enables repeatable standalone ASR experiments and existing TTS integration;
it does not implement application Qwen MPS routing.

Candidate CI checks clean locked installation, build/version/import contracts,
CPU tensor and fake-device tests, and switching back to CPU VAD and core.
It does not open microphones, load external voice/model assets or claim GPU
execution. The existing cu124 ladder also runs dependency-gated model tests.

## FlashAttention wheel inventory

Checked on 2026-09-11. FlashAttention is not a required dependency. The existing
NVIDIA Qwen path selects `flash_attention_2` when `flash_attn` imports, otherwise
Torch SDPA. MPS does not use these CUDA wheels. Installing a wheel is not proof
of support for every GPU, including RTX 50-series Blackwell.

| Target | Located artifact | Publisher |
|---|---|---|
| Windows x86_64, CPython 3.12, Torch 2.7.0, CUDA 12.8 | `flash_attn-2.8.3+cu128torch2.7.0cxx11abiFALSE-cp312-cp312-win_amd64.whl` | [kingbri1 community build](https://github.com/kingbri1/flash-attention/releases/tag/v2.8.3) |
| Linux x86_64, CPython 3.12, Torch 2.7, CUDA 12 family, C++11 ABI true | `flash_attn-2.8.3.post1+cu12torch2.7cxx11abiTRUE-cp312-cp312-linux_x86_64.whl` | [Dao-AILab upstream](https://github.com/Dao-AILab/flash-attention/releases/tag/v2.8.3.post1) |

GitHub release asset SHA-256 values:

```text
Windows: 916ae4d818d2b5a02b3b25e8431251b88a01ce98d08315495dd78242d81f7182
Linux:   f87164bb919f5597cb94f7485196be6ac7842b71d7bf8e9f8e9133b9045ab5ea
```

The Windows publisher's [build workflow at the release tag](https://github.com/kingbri1/flash-attention/blob/v2.8.3/.github/workflows/build-wheels.yml)
includes Python 3.12, Torch 2.7.0 and CUDA toolkit 12.8.1, with C++11 ABI false.
This is a community binary, not an official Windows FlashAttention wheel.
The upstream [installation documentation](https://github.com/Dao-AILab/flash-attention#installation-and-features)
primarily targets Linux and describes Windows compilation as needing further testing.
An alternative [mjun0812 release](https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/tag/v0.4.10)
also lists CPython 3.12 / Torch 2.7 / cu128 Windows wheels.

For a controlled wheel experiment, first install the full `local-cu128` profile.
Then install **only the wheel for the current platform** using its pinned hash:

```bash
# Windows community candidate
uv pip install --python .venv --no-deps "https://github.com/kingbri1/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3%2Bcu128torch2.7.0cxx11abiFALSE-cp312-cp312-win_amd64.whl#sha256=916ae4d818d2b5a02b3b25e8431251b88a01ce98d08315495dd78242d81f7182"

# Linux upstream candidate; first confirm torch._C._GLIBCXX_USE_CXX11_ABI is True
uv pip install --python .venv --no-deps "https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3.post1/flash_attn-2.8.3.post1%2Bcu12torch2.7cxx11abiTRUE-cp312-cp312-linux_x86_64.whl#sha256=f87164bb919f5597cb94f7485196be6ac7842b71d7bf8e9f8e9133b9045ab5ea"

uv pip check --python .venv
uv run --locked --no-sync python -c "import torch, flash_attn, flash_attn_2_cuda; print(torch.__version__, torch.version.cuda, torch._C._GLIBCXX_USE_CXX11_ABI, flash_attn.__version__)"

# Native import only, then an explicitly requested GPU comparison (no model assets)
uv run --locked --no-sync python tools/verify_flash_attention.py
uv run --locked --no-sync python tools/verify_flash_attention.py --compute --device cuda:0
```

These binaries are deliberately outside the default lock. A subsequent
`uv sync` removes the experimental wheel. This avoids automatic source compilation
or silently changing the tested Torch version. To qualify a GPU, compare forward
and KV-cache results against Torch's math SDPA before testing the real Qwen model;
native extension import alone does not test CUDA kernels.

### Windows kernel evidence, 2026-09-11

The Windows artifact above was downloaded and its SHA-256 verified. In a fresh
candidate environment, Python 3.12.10, Torch/Torchaudio 2.7.0+cu128 and driver
581.57 passed native import, `uv pip check`, and the checked-in probe on both
RTX 4070 Ti SUPER and RTX 4070 Laptop (compute capability 8.9).

Each GPU passed eight comparisons: FP16/BF16, head dimensions 64/128, causal
GQA forward (128 tokens, eight query heads/two KV heads), and a one-token
KV-cache append with 32 prefix tokens. Cache writes were also checked. Against
Torch math SDPA, the largest absolute error was 0.0009765625 for FP16 and
0.0078125 for BF16, within the probe's respective 0.003 and 0.03 tolerances.

These results qualify this small kernel probe on these two devices. No real
Qwen/GPT-SoVITS inference, latency benchmark, audio-device journey, Linux GPU,
MPS device, or RTX 50-series kernel test was performed in this check.

## Evidence needed before default promotion

Record the final source commit, OS, Python, Torch/Torchaudio versions, CUDA/HIP/MPS
facts, GPU, driver, model revision and attention implementation. Keep installation,
kernel, model and audio-device results separate.

1. Clean locked install and return to model-less core pass on the target platform.
2. Required GPU computations pass. For FlashAttention, compare representative
   FP16/BF16 forward and KV-cache calls with a math reference on the actual GPU.
3. Qwen-ASR and GPT-SoVITS pass with the same model/audio inputs; record cold start,
   first output latency and peak memory against the existing environment.
4. VAD, continuous playback, interruption, repeated start/stop and shutdown pass.
5. Promote cu128 only after those results; retain cu124 only where a demonstrated
   driver/device or extension compatibility requirement remains.

Source references: [PyTorch 2.7 installation pairs](https://pytorch.org/get-started/previous-versions/#v270),
[PyTorch 2.7 release](https://pytorch.org/blog/pytorch-2-7/).
