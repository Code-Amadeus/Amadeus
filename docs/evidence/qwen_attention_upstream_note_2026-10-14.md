# Qwen attention configuration upstream evidence

Repository evidence note; no upstream report has been submitted. Existing destination: [QwenLM/Qwen3-ASR issue 201](https://github.com/QwenLM/Qwen3-ASR/issues/201). Do not open a duplicate issue.

## Problem

Requesting `flash_attention_2` can leave the live thinker, audio encoder and text decoder using SDPA while the outer model configuration reports FlashAttention 2. Logs based only on the requested implementation therefore misdescribe the actual execution path and invalidate attention comparisons.

## Upstream check

Checked repository `QwenLM/Qwen3-ASR`, revision `7c6daf77a2421100f5fb066495372c00129d39ff`, on 2026-10-14. The two composite configuration constructors still call the parent initializer before creating their nested configs; see [configuration constructors](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/core/transformers_backend/configuration_qwen3_asr.py#L329). The model initializes the thinker from its nested configuration; see [model construction](https://github.com/QwenLM/Qwen3-ASR/blob/7c6daf77a2421100f5fb066495372c00129d39ff/qwen_asr/core/transformers_backend/modeling_qwen3_asr.py#L1311).

Issue 201 already describes this defect. [PR 202](https://github.com/QwenLM/Qwen3-ASR/pull/202) proposes a construction-order fix; at the checked date its API state was closed, merged=false. The pinned installed package was not patched for this reproduction. No application wrapper or local inference hook is involved in the minimal configuration reproduction below.

## Environment and evidence sanitized

Windows x86-64, Python 3.12.10, qwen-asr 0.0.6, Transformers 4.57.6, PyTorch 2.6.0+cu124. For the separate GPU observation: RTX 4070 Ti SUPER, FlashAttention 2.7.4.post1, BF16.

Minimal weight-free, audio-free reproduction against the installed upstream package:

```python
from qwen_asr.core.transformers_backend.configuration_qwen3_asr import Qwen3ASRConfig
c = Qwen3ASRConfig(attn_implementation="eager")
print([
    c._attn_implementation,
    c.thinker_config._attn_implementation,
    c.thinker_config.audio_config._attn_implementation,
    c.thinker_config.text_config._attn_implementation,
])
```

Observed output: `['eager', None, None, None]`.

The separately loaded real 0.6B checkpoint reported outer `flash_attention_2`, but thinker/audio/text `sdpa`. Explicit recursive selection for a controlled attention experiment subsequently reported `flash_attention_2` on all four components. This establishes configuration propagation and live dispatch configuration; it is not a kernel trace or a claim of universal speedup.

Related but distinct: [issue 213](https://github.com/QwenLM/Qwen3-ASR/issues/213) and [PR 103](https://github.com/QwenLM/Qwen3-ASR/pull/103) concern the audio encoder's window mask on non-FA2 backends. They explain why long-audio transcripts must be checked separately when changing attention; this note does not report a new reproduction of that separate defect.
