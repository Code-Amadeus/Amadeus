# Experimental GPT-SoVITS v3 semantic decoder on MLX

This opt-in path replaces only GPT-SoVITS v3 T2S semantic autoregression. The
Japanese/English frontend, reference extraction, SoVITS v3/CFM, BigVGAN, audio
stream, onset gate, subtitles, and playback stay on the existing path. Torch is
the default. The MLX implementation uses POST-LayerNorm, ReLU,
the source sampler, and a per-generation cache growing in 256-position chunks.
The application keeps FP32; FP16 is a decoder/probe candidate pending Mac
numerical and listening results, with no additional application setting.
It supports one
referenced input at a time. It has no automatic Torch fallback.

The fixed behavior baseline is Amadeus
`77161583eb32b4ae6893289e1e020731b6ae0b5c`. OminiX-MLX
`4988a3fcfa48b8cb5d0780a501b92c6a41401523` was consulted for T2S layout
and trace strategy; Amadeus's actual `infer_panel_naive` remains the semantic
authority. Converted weights are derived from the selected `TTS_GPT_MODEL_PATH`
and cached by its SHA-256 under `.cache/gsv-mlx-t2s`; they are checked before
reuse. The source checkpoint remains authoritative.

The September 30 audit correctly separated weight conversion from runtime
precision: conversion preserves the source FP16 tensors, while the original
runtime upcast them to FP32. The revised manifest describes only the converted
artifact. Actual inference dtype belongs in the decoder/probe report.

Biased projections use `mx.addmm`, normalization uses `mx.fast.layer_norm`,
and attention calls the SDPA API with the original text/audio mask. The locked MLX 0.32.2 Metal
[routing implementation](https://github.com/ml-explore/mlx/blob/v0.32.2/mlx/backend/metal/scaled_dot_product_attention.cpp#L619)
does not provide a fused path for this model's head dimension of 32.
The candidate pads Q/K/V with zeros to 64 features, keeps the original
`1/sqrt(32)` scale, and crops the attention output back to 32 before merging
heads. Padded K/V stay in the cache, so decode pads only the new rows.
This makes the layout eligible for the existing Metal kernel; actual kernel
execution and benefit still require a Mac. Weights are unchanged, while KV
array capacity doubles (at 512 positions and 24 layers: FP16 24 to 48 MiB,
FP32 48 to 96 MiB).
Likewise, slice updates permit cache-buffer donation but do not prove that
every update avoids copying on the untested Metal backend.

## Install, select, and revert on Apple Silicon

Use Python 3.12.10 and the existing v3 GPT/SoVITS model, reference audio, and
frontend assets. The existing `local-mps` acoustic stack is still required.

```bash
uv sync --locked --extra voice --extra vad --extra local-mps --extra mlx-t2s --extra dev
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py doctor --device metal --checkpoint /path/to/gpt-v3.ckpt
```

For the application, set `TTS_T2S_BACKEND=mlx` in its startup environment or
`.env`, keep `TTS_DEVICE=mps`, and restart the TTS process. `TTS_T2S_BACKEND`
accepts only `torch` and `mlx`. MLX refuses non-v3 models, absent reference
conditioning, missing native dependencies, or unusable Metal/MPS; it does not
switch models or voices. To revert, set `TTS_T2S_BACKEND=torch` and fully
restart. Existing Torch users do not need the MLX extra.

## Local probes

The probe writes JSON below `output/diagnostics/gsv-mlx-t2s/` unless given
`--output`. It never starts the full app, records a microphone, plays audio,
connects to a cloud speech service, or uploads results. Failure also writes a
small sanitized JSON and exits nonzero. Run each backend benchmark in its own
process; the JSON contains base/candidate SHA, dirty state, environment,
checkpoint and fixture hashes, parameters, token counts, and timings. A dirty
checkout's `candidate_sha` names only HEAD, so record the reviewed commit when
sharing final results.

```bash
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py export --checkpoint /path/to/gpt-v3.ckpt
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py validate --device metal --checkpoint /path/to/gpt-v3.ckpt
```

`validate` without `--inputs` uses synthetic phones, reference semantic IDs,
BERT features, and a fixed token history. It compares the unchanged Torch
source blocks with real MLX prefill and decode, cached versus full recompute,
and reports every tensor's absolute/RMS error and logit ranking. It does not
establish intelligibility or voice quality.

To make a private fixture with actual reference extraction and the existing
frontend, first use the full Torch model environment. The example text and
reference arguments are local; do not publish the resulting NPZ or audio.

```bash
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py inputs \
  --checkpoint /path/to/gpt-v3.ckpt --sovits /path/to/sovits-v3.pth \
  --reference-audio /path/to/reference.wav --reference-text '参考音声の書き起こし。' \
  --text 'これは短いテストです。' --history-steps 12 \
  --fixture-output output/diagnostics/private-gsv-inputs.npz
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py validate \
  --device metal --checkpoint /path/to/gpt-v3.ckpt \
  --sovits /path/to/sovits-v3.pth --reference-audio /path/to/reference.wav \
  --inputs output/diagnostics/private-gsv-inputs.npz
```

The fixture stores the GPT, SoVITS, and reference hashes. Validation rejects
a mismatched source, malformed token arrays, or nonfinite BERT values. The
private NPZ still contains reference-derived features; keep it local.

Run fixed-workload and free generation separately in the model-only benchmark.
The fixed part uses the same saved token history, performs sampling and the
EOS decision at each position, and observes completion at every step.
Prefill is reported separately. Fixed history deliberately ignores the stop
decision to preserve the same work count; free generation exercises stopping.
The free part runs the source Torch
sampler or MLX sampler normally and reports its output length, so a shorter
random output is visible. Warmup runs are excluded from measured summaries.
Measured trials use the same reported seed schedule in each process; equal
seed values do not imply Torch and MLX draw the same tokens. The report has no
playback timing. These `inputs` and `audio` probe examples exercise the
existing Japanese frontend. The explicit `--budget 400` below is a diagnostic
token cap; a reached budget boundary is not a completed sentence or speedup.

```bash
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py bench \
  --backend torch --device metal --checkpoint /path/to/gpt-v3.ckpt \
  --inputs output/diagnostics/private-gsv-inputs.npz --budget 400 --warmup 3 --runs 20 \
  --output output/diagnostics/gsv-torch-bench.json
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py bench \
  --backend mlx --device metal --checkpoint /path/to/gpt-v3.ckpt \
  --inputs output/diagnostics/private-gsv-inputs.npz --budget 400 --warmup 3 --runs 20 \
  --output output/diagnostics/gsv-mlx-bench.json
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py soak \
  --device metal --checkpoint /path/to/gpt-v3.ckpt \
  --inputs output/diagnostics/private-gsv-inputs.npz --requests 100
```

The commands above default to FP32. For the FP16 candidate, add
`--inference-dtype float16` to `bench` or `audio` and use a distinct output
path. Test both Torch and MLX at each precision. For FP16 numerical
characterization, add `--inference-dtype float16 --characterize` to `validate`;
this records deviations without treating them as passing FP32 tolerance.
The application itself still selects FP32 when MLX is enabled.

`soak` alternates short and long input shapes, reloads the decoder every 20
requests by default, and records per-request latency, output count, RSS, MLX
active/cache/peak memory, and Torch MPS allocation where available. It tests
the semantic model only, not the full acoustic chain or player. Its CPU mode
is numerical only; it does not test application cancellation or playback.

For real audio, run each backend as a separate local process with the same
model/reference, parameters, and text. `audio` repeats the same request after
warmup, disables semantic freeze, collects guard attempt and semantic token
counts, and can save numbered WAVs for blind listening. The first chunk is
measured after the existing onset gate; WAV disk writing is outside synthesis
time. There is no audio-device write or physical acoustic-onset measurement.

```bash
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py audio \
  --backend torch --checkpoint /path/to/gpt-v3.ckpt --sovits /path/to/sovits-v3.pth \
  --reference-audio /path/to/reference.wav --reference-text '参考音声の書き起こし。' \
  --text 'これは短いテストです。' --warmup 3 --runs 20 \
  --output output/diagnostics/gsv-torch-audio.json --wav output/diagnostics/torch.wav
uv run --locked --no-sync python tools/probes/gsv_backend_probe.py audio \
  --backend mlx --checkpoint /path/to/gpt-v3.ckpt --sovits /path/to/sovits-v3.pth \
  --reference-audio /path/to/reference.wav --reference-text '参考音声の書き起こし。' \
  --text 'これは短いテストです。' --warmup 3 --runs 20 \
  --output output/diagnostics/gsv-mlx-audio.json --wav output/diagnostics/mlx.wav
```

The app's first voiced device write, interruption drain, and audible quality
still need a real Mac playback session. Compare warm first emitted chunk p50
and p95, RTF, output duration, semantic tokens, guard retries, and memory;
listen to randomly labeled WAV pairs for omissions, repeated syllables,
noise, timbre, and endings. A faster AR stage alone does not establish faster
audible onset. The application shares the first-sentence audio cache across
Torch and MLX. Set `FIRST_SENTENCE_AUDIO_CACHE_ENABLED=0` for both A/B runs;
selecting MLX must not disable ordinary cache hits for users.

## Repeatable A/B speed and voice acceptance

Run the suite as a single command after installing the full local voice stack
and the optional MLX extra. It uses the actual
`tts.pipeline.get_sovits_params` values from this branch for first and later
sentences, including the 4/16/32 CFM step choices and length-dependent
stop budget. The five fixed Japanese cases cover short, medium, long,
weak-filler, and continuation text. Each case gets two sequential blocks:
Torch then MLX, followed by MLX then Torch. Each backend exits before the
next starts. The default one warmup plus two measured requests per process
gives 20 measured production-like requests per backend across the five cases.
The suite also runs one controlled `top_k=1` pair per case and a separate
actual-frontend fixed-history model benchmark. It disables completed-audio
cache use and semantic freeze. Reports retain failed runs and show paired
same-seed timings separately from unpaired successful observations.

```bash
uv run --locked --no-sync python tools/probes/gsv_ab_acceptance.py \
  --device metal --checkpoint /path/to/gpt-v3.ckpt \
  --sovits /path/to/sovits-v3.pth \
  --reference-audio /path/to/reference.wav \
  --reference-text '参考音声の書き起こし。'
```

For an FP16 MLX audio candidate, add `--mlx-dtype float16`; the Torch side
defaults to FP32 and can separately be selected with `--torch-dtype float16`.
These options affect only the semantic model; the acoustic precision stays
the same. Keep reports from different precision pairs in separate directories.

All outputs stay in a new `output/diagnostics/gsv-ab/` run directory. The
suite report records resolved parameters, backend order, checkpoint/reference
and code hashes, actual device/dtype, warmup and measured counts, per-case
p50/p95 first emitted chunk and synthesis time, RTF, token/guard counts,
bridge-inclusive semantic time, RSS, and audio quality flags. Fixed-history
prefill/decode timings are separate from free-generation audio. CPU timings
can expose cost but cannot establish a Metal speedup. The first emitted
chunk is after the existing onset gate; it is not an audio-device write or a
physical acoustic-onset measurement.

The private blind kit contains anonymously assigned `pair_###_A/B.wav`
files, `review-sheet.csv`, and a separate `answer-key.private.json`. Compare
intelligibility, missed or repeated words, timbre, prosody, artifacts, and
preference without opening the answer key first. The WAVs are saved as
PCM16. A controlled waveform comparison is made only when semantic IDs
match and the probe has restored Torch acoustic RNG around semantic
generation. Bit-identical saved PCM16 WAVs do not prove equality before WAV
encoding. Production-like `top_k=5` samples are independently random in
Torch and MLX; judge them by the blind listening results, guard behavior,
and objective flags, not waveform equality. ASR may help find omissions but
cannot approve timbre or prosody.

On Windows/Linux, `--device cpu` selects a **probe-only** MLX CPU decoder at
the TTS model-load seam and routes its real semantic IDs through the normal
Torch v3 acoustic and onset chain. Install the locked `torch-cpu`,
`local-models`, `voice`, `mlx-t2s-cpu` group, and `dev` components in an
isolated environment. Keep real voice assets private. This path does not
relax the production Apple Silicon Metal requirement and its CPU latency is
not the Mac speed gate.

The two final gates are **a measured Mac speed result** and **acceptable
voice quality from human listening**. The suite leaves both pending until
those observations are entered and reviewed. The application playback's
first voiced device write, interruption drain, and long Mac memory behavior
still need separate product-level qualification.

The Linux CI numerical lane uses the locked `mlx-t2s-cpu` dependency group.
For an isolated Docker rerun, build the image from the small `tools/probes`
context and mount a **sanitized, clean source-release archive** read-only as
`/workspace`. The image copies no checkout contents at build time. The
source-release tool rejects dirty/untracked files and private model assets;
run it after the candidate has been committed. Docker execution has not been
verified on the development Windows host.

```bash
python tools/build_source_release.py --output output/diagnostics/gsv-source.zip
mkdir -p output/diagnostics/gsv-source-tree
unzip -q output/diagnostics/gsv-source.zip -d output/diagnostics/gsv-source-tree
docker build -f tools/probes/Dockerfile.gsv-mlx-cpu -t amadeus-gsv-mlx-cpu tools/probes
docker run --rm --mount type=bind,src="$(pwd)/output/diagnostics/gsv-source-tree/amadeus-0.15.2a0",dst=/workspace,readonly amadeus-gsv-mlx-cpu
```

## Measured CPU evidence and remaining gates

The original FP32 path through `a1873b7` was a correctness reference, with no
demonstrated acceleration. The controlled audio pairs below are historical
evidence for that implementation, not qualification of subsequent changes.
They are the clearest existing
CPU semantic-stage comparison: they use identical generated semantic IDs,
and MLX was slower in every case. Metal performance remains unmeasured.

On clean candidate `c3450ea`, the actual v3 frontend/reference fixture passed
350 Torch-to-MLX prefill, decode, and cache comparisons (120 phones, 191
reference semantic IDs, 12 fixed history steps; maximum absolute difference
0.00091552734375). The required real MLX CPU test suite also passed. This is
numerical parity evidence, not a voice or Metal speed result.

The separate-process Windows 11 CPU audio suite on candidate `2ffe02a` used
Python 3.12.10, Torch/Torchaudio 2.7.0+cpu, MLX/MLX-CPU 0.32.2, FP32, and
four configured CPU threads. Its five Japanese cases each had two AB/BA
blocks and four paired normal samples per backend (20/backend total), with
one warmup per process excluded. All 30 audio processes succeeded and
produced 50 private PCM16 WAVs, including five controlled pairs. In all five
controlled pairs the semantic IDs and saved PCM16 WAVs matched exactly;
equality of the pre-encoding float waveform was not measured. A separate
50-WAV audit found no silence, clipping, or generation-budget flags. Two
continuous-token flags belonged to the **same** controlled short output on
both backends, so they are not evidence of an MLX-specific repetition.

| Controlled case | Generated IDs | Torch semantic s | MLX semantic s | MLX time increase |
|---|---:|---:|---:|---:|
| Short first | 40 | 1.380 | 2.229 | 61.5% |
| Weak filler first | 30 | 0.993 | 1.797 | 81.0% |
| Medium follow-up | 88 | 2.964 | 4.043 | 36.4% |
| Continuation follow-up | 156 | 5.478 | 7.126 | 30.1% |
| Long follow-up | 239 | 7.705 | 10.068 | 30.7% |

Each controlled pair has one measured request after warmup, not a latency
distribution. Its identical tokens make the observed semantic costs easier
to interpret than normal random generation, but repeated measurements are
still needed. The exported trained artifact has 77,606,402 FP16 parameters:
148.02 MiB of parameter arrays, versus 296.04 MiB after the reference
runtime's FP32 cast. These are parameter bytes, not process memory or
measured memory traffic. Upcasting preserves the source values exactly;
FP32 arithmetic can still change accumulation error. Runtime precision
must therefore be an explicit, tested choice rather than a converter claim.

Normal-sampling end-to-end synthesis p50, in seconds, was:

| Case | Torch | MLX |
|---|---:|---:|
| Short first | 7.041 | 7.563 |
| Medium follow-up | 32.986 | 31.305 |
| Long follow-up | 131.433 | 137.247 |
| Weak filler first | 7.193 | 7.717 |
| Continuation follow-up | 61.101 | 50.885 |

These are four paired observations per case; p95 is descriptive only. Normal
sampling can produce different token counts and audio lengths, so these
end-to-end values are not a fixed-workload engine speed comparison. The CPU
results show no stable overall gain and cannot predict Metal benefit.

Offline Qwen3-ASR-0.6B assessed all 50 WAVs after synthesis (zero failures;
45 unique recordings) without the target text as ASR context. Among the 20
paired normal samples per backend, micro CER was 4.7619% Torch versus
5.4762% MLX; pyopenjtalk micro PER was 1.2972% versus 1.65094%. PER for
the three content cases matched across backends; differences were mainly in
short fillers. ASR is an intelligibility proxy and cannot approve timbre,
prosody, or preference. Human blind listening remains pending.

A corrected fixed-history benchmark on separate clean candidate `271027f`
evaluated every MLX step's output projection. It used the same 104 phones,
191 reference IDs, and 32 fixed decode steps, with five measured runs after
one warmup. The earlier fixed-decode numbers embedded in the frozen audio
suite are **not comparison evidence**.

| Fixed CPU stage | Torch p50/p95 ms | MLX p50/p95 ms |
|---|---:|---:|
| Prefill | 239.361 / 247.494 | 688.391 / 789.524 |
| Decode, same 32-token history | 873.780 / 877.121 | 750.757 / 1133.951 |

The apparent 14% decode reduction from these five historical observations
is withdrawn as acceleration evidence: it has not been established as a
reproducible gain, and the independent audit supplied by the user did not
reproduce it. This benchmark evaluated all fixed-history outputs only at
the end; it did not include the production sampler and host synchronization
at each token. Its numbers are retained as historical observations only.
Use repeated per-token sampling/synchronization measurements with fixed
history, raw samples, and alternating backend order for the next comparison.
The controlled CPU results above show a slower MLX semantic stage.
Metal numerical qualification,
Mac mixed MPS/MLX audio and speed, human voice-quality approval, first voiced
device write, interruption behavior, and long Mac memory stability are
**not run**. Both final gates remain: measured Mac speed and acceptable voice
quality from blind human listening. The Linux CI numerical lane is defined
in `.github/workflows/gsv-mlx-t2s.yml`; a hosted result is not claimed here.
