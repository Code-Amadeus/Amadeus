# Experimental GPT-SoVITS v3 semantic decoder on MLX

This opt-in path replaces only GPT-SoVITS v3 T2S semantic autoregression. The
Japanese/English frontend, reference extraction, SoVITS v3/CFM, BigVGAN, audio
stream, onset gate, subtitles, and playback stay on the existing path. Torch is
the default. The MLX implementation uses FP32 weights, POST-LayerNorm, ReLU,
the source sampler, and a per-generation dynamic KV cache. It supports one
referenced input at a time. It has no automatic Torch fallback.

The fixed behavior baseline is Amadeus
`77161583eb32b4ae6893289e1e020731b6ae0b5c`. OminiX-MLX
`4988a3fcfa48b8cb5d0780a501b92c6a41401523` was consulted for T2S layout
and trace strategy; Amadeus's actual `infer_panel_naive` remains the semantic
authority. Converted weights are derived from the selected `TTS_GPT_MODEL_PATH`
and cached by its SHA-256 under `.cache/gsv-mlx-t2s`; they are checked before
reuse. The source checkpoint remains authoritative.

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
The fixed part uses the same saved token history and one synchronization after
prefill and after the full decode sequence. The free part runs the source Torch
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

`soak` alternates short and long input shapes, reloads the decoder every 20
requests by default, and records per-request latency, output count, RSS, MLX
active/cache/peak memory, and Torch MPS allocation where available. Its CPU
mode is numerical only. It does not test application cancellation or playback.

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
audible onset. Keep the first-sentence audio cache disabled in both A/B runs;
MLX ignores its read and write paths automatically. Torch can disable it with
`FIRST_SENTENCE_AUDIO_CACHE_ENABLED=0`.

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

## Evidence and unrun gates

Windows real MLX CPU numerical tests passed on a synthetic tiny checkpoint.
A one-request Torch CPU v3/LoRA audio smoke also completed through the local
probe with four CFM steps and no playback; it verifies the command and stream
metadata/onset measurement, not sound quality or MLX acoustic integration.
A local v3 checkpoint with the actual frontend and reference produced 350
passing prefill/decode/cache comparisons over 120 phones, 191 reference
semantic tokens, and 12 fixed history steps; the observed maximum absolute
error was 0.000916. Those observations were made on an uncommitted experiment
checkout and must be repeated against the reviewed candidate. Linux CI has
an enforced real MLX CPU lane in `.github/workflows/gsv-mlx-t2s.yml`; its
hosted result is not yet available. Metal numerical qualification, the
Torch-MPS/MLX mixed audio path, audible quality, first voiced device write,
and long Mac memory stability are **not run**. CPU timings are not a speedup
claim.
