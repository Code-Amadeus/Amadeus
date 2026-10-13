# PyTorch 2.6 CUDA Graph 捕获顺序：上游核验记录

## Problem

首次 CUDA Graph 在 `torch.inference_mode()` 中捕获后，同进程另一个模型在 `torch.no_grad()` 中首次捕获失败，错误为 `Inplace update to inference tensor outside InferenceMode is not allowed`。实际影响是 ASR 先加载并预捕获后，TTS 首次捕获失败。预期是两个模型可以依次捕获各自的图。

## Upstream check

规范上游是 [pytorch/pytorch](https://github.com/pytorch/pytorch)。2026-10-14 核验了 v2.6.0 的 `aten/src/ATen/cuda/CUDAGeneratorImpl.cpp`：`CUDAGeneratorState::register_graph`（原始源码 123–133 行）初始化共享 seed/offset tensor 时没有关闭 inference mode；`capture_prologue`（179–183 行）随后对这些 tensor 执行 `fill_`。

已有 [issue #166792](https://github.com/pytorch/pytorch/issues/166792) 与合入的 [PR #172706](https://github.com/pytorch/pytorch/pull/172706)。核验的修复提交为 [58a1b7229545c3bd21966dfa933d1ae20d24fc7b](https://github.com/pytorch/pytorch/commit/58a1b7229545c3bd21966dfa933d1ae20d24fc7b)。该修复在共享 graph generator state 初始化处关闭 inference mode。没有重复创建 issue，也没有发布本地代码或日志。

## Environment and evidence (sanitized)

Windows 11 x86-64、Python 3.12.10、PyTorch 2.6.0+cu124、RTX 4070 Ti SUPER、驱动 581.57。使用已安装的原版 PyTorch；复现不导入 ASR/TTS、不使用模型、录音或随机采样。

```python
import torch

x = torch.ones(4, device="cuda:0")
for _ in range(3):
    y = x + 1
torch.cuda.synchronize()

with torch.inference_mode():
    first = torch.cuda.CUDAGraph()
    with torch.cuda.graph(first):
        y = x + 1
with torch.no_grad():
    second = torch.cuda.CUDAGraph()
    with torch.cuda.graph(second):
        z = x * 2
```

原代码实测失败；在新进程将第一个上下文改为 `torch.no_grad()` 后实测通过。两个实验都没有补丁修改 PyTorch。错误发生在第二次 capture begin，后续 RNG 还可能报告 offset 状态错误；该受影响进程的延迟结果不用于性能比较。没有在新 PyTorch wheel 上运行修复验证，修复状态依据上游提交源码。

本地 ASR 初始化与捕获已限定在 `inference_mode(False) + no_grad()`，外层调用者是否启用 inference mode 都不会改变该边界。真实模型的 ASR 先捕获、TTS 后捕获，以及捕获争用和 park/restore 已另行验证，详见 [实施审计](../asr_cuda_graph_audit_2026-10-14.md)。此为本仓库核验记录，未向上游提交报告。
