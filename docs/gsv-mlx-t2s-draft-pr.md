# [Draft][Experimental] Add opt-in MLX T2S for GPT-SoVITS v3

The v3 semantic autoregressive stage can run on Python MLX when
`TTS_T2S_BACKEND=mlx` is selected at startup on Apple Silicon. The default
Torch decoder and the reference frontend, v3 SoVITS/CFM, BigVGAN, onset gate,
and playback remain in place. Unsupported models, absent reference
conditioning, and missing Metal/MPS fail explicitly. The derived artifact
preserves source weight values and dtype, then uses FP32 inference. It is
checked against the selected GPT checkpoint before cache reuse.

The real MLX CPU suite compares the unchanged Torch source against MLX
embeddings, every T2S block, prefill/decode logits, and cached/full results.
An actual local v3 frontend/reference fixture passed 350 comparisons on a
dirty experiment checkout; repeat after the final commit. Existing Torch
onset, guard, cache, contract, and sidecar suites also need a final post-edit
run. The Linux MLX CPU job is added but has not run on hosted CI yet.

Mac Metal numerics, mixed MPS/MLX audio, blind listening, first voiced device
write, cancellation, and memory soak remain unverified. The local doctor,
validation, model-only benchmark, soak, and audio probes produce per-process
reports; private fixtures and WAVs stay outside the PR. See
`docs/gsv-mlx-t2s-experiment.md` for exact commands and rollback.
