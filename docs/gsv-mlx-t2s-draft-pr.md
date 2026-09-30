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
An actual v3 frontend/reference fixture passed 350 comparisons on clean
candidate `c3450ea` (maximum absolute difference 0.00091552734375).

The probe-only CPU mixed chain ran the unchanged Torch v3 acoustic and onset
path with MLX semantic output. On clean audio candidate `2ffe02a`, five
Japanese cases produced 20 paired normal samples per backend, five
controlled pairs, and 50 private WAVs across 30 successful separate
processes. All controlled pairs had the same semantic IDs and bit-identical
saved PCM16 WAVs. The 50-WAV audit found no silence, clipping, or budget
flags; a continuous-token flag appeared in the same controlled short output
on both backends. Offline ASR processed all 50 WAVs with zero failures; paired
normal micro CER was 4.7619% Torch / 5.4762% MLX, and micro PER was 1.2972%
/ 1.65094%. These are intelligibility proxies, not a timbre or prosody verdict.

The corrected same-history CPU benchmark on clean candidate `271027f`
materializes every MLX decode step's logits. Across five measured runs,
prefill p50 was 239.361 ms Torch / 688.391 ms MLX; 32-step decode p50 was
873.780 / 750.757 ms, while decode p95 was 877.121 / 1133.951 ms. The old
fixed-decode numbers embedded in the audio suite are excluded from comparison.
Different random output lengths also limit end-to-end CPU timing comparisons;
no stable overall gain is demonstrated.

Mac Metal numerics, mixed MPS/MLX speed and audio, blind human voice-quality
review, first voiced device write, interruption, and long Mac memory behavior
remain unverified. **Both measured Mac speed and acceptable human-rated voice
quality are required before promotion.** The local probes and A/B suite keep
private fixtures and WAVs outside the PR. See
`docs/gsv-mlx-t2s-experiment.md` for commands, interpretation, and rollback.
