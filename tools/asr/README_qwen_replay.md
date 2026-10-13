Qwen offline replay compares `raw_official` (the unmodified wrapper), measured
`official`, last-position `prefill`, `greedy`, and prepared `cuda_graph` on one shared model. Production
defaults to `cuda_graph` with one prepared 384-token bucket; unsupported or
unprepared requests select `official` and record the reason. On NVIDIA installations with FA2 available, production uses
FA2 for the audio encoder and SDPA for the text decoder; CPU, ROCm and missing-FA2
loading keep their existing selection. No microphone, playback, chat, or network
input is used.

Create a local JSON manifest:

```json
{"samples":[{"id":"example","path":"audio.wav","language":"auto","context":"","max_tokens":64}]}
```

Paths are absolute or relative to the manifest. `language`, `context`, and
`max_tokens` are optional; defaults are CLI language, existing ASR_CONTEXT, and
the existing duration budget. Optional `reference` enables per-example CER/WER.
Without a reference, comparisons measure transcription consistency only.
CER uses exact Unicode characters; WER uses whitespace-separated words. Both
include punctuation/case and report reference denominators. CJK WER is generally
not meaningful, so use CER and review digits/names separately.

```powershell
.venv/Scripts/python.exe tools/asr/benchmark_qwen_replay.py --manifest output/diagnostics/asr-manifest.json --output output/diagnostics/asr-replay.json --repeats 5 --restore-repeats 1 --include-text
```

Coordinate GPU use before running. The tool keeps existing dtype rules,
does not change TF32, compile, quantization, dependencies or services. ASR CUDA
Graph preparation is an explicit option described below.
Attention × engine order rotates deterministically for every sample/repeat, while sample
order follows the manifest (put long/short alternations there). Warmup is excluded
from warm summaries. `--cold-repeats` explicitly reloads the model separately for
each cold call; it reports load time separately. This is a same-process weight
reload with OS/dependency caches, not a fresh-process cold start. Restore calls move the model to
RAM before each request and report exposed restore time. CPU cannot run restore.

Run `--profile` separately to collect CUDA events. Ordinary `wall_ms` is elapsed
host submission/wait time; `gpu_ms` is CUDA event elapsed time, which can include
stream gaps and is not kernel busy time. Frontend covers prompts/tokenization/
features; prefill excludes the separately timed encoder; generation preparation
is separate; decode includes token selection/stop overhead, with model
decode forwards reported separately. Official outer normalization/chunk/parse
overhead is included in total, not misclassified as a GPU stage. No per-token
timing synchronization is added; the existing stop check still synchronizes.

`--attention current` preserves/restores the existing loader behavior, including
mixed component configs. Pass `--attention current sdpa flash_attention_2` for a
paired matrix on one model. Setter work happens outside measured calls. The
offline-only overrides use public setters at each owning component and verify
the actual top/thinker/audio/text configs, also after each cold reload. This
avoids stale nested-config markers in this Transformers runtime. Each record and
summary carries attention; decoding comparisons use raw_official from the same
attention group. Compare attention separately from a fixed-attention engine ablation. The loader
request is recorded separately: it is not proof that nested modules use that
implementation. This experiment does not create a production attention setting.
The supported device arguments are cpu/cuda/cuda:0; this tool does not imply
support for a second CUDA device through the existing loader.

`audio_fa2_text_sdpa` explicitly selects the production FA2-capable policy: root, thinker and audio
encoder use `flash_attention_2`, while the text decoder uses `sdpa`. The owning
component setters and four live configs remain explicit. This separates audio
window semantics for longer inputs from single-token decoder performance;
it adds no custom attention kernel. For example, compare
`--attention sdpa flash_attention_2 audio_fa2_text_sdpa` in the same matrix.
Use explicit `sdpa` to reconstruct the old effective backend. After this change,
`current` resolves to the new mixed policy on this installation and must not be
used as the old baseline. The production loader and offline preset share one
component mapping and verification helper.

JSON includes runtime/model/config/device, PCM/context hashes, per-call stages,
generated token count and EOS/budget/criteria stops, nearest-rank p95 and n.
`raw_official` has no token/stage telemetry because it is deliberately untouched.
`--include-text` keeps complete text and token IDs in the output for local review;
otherwise only equality/edit counts are written. Store output under ignored
local diagnostics and do not publish user audio/text evidence.

Programmatic use: `asr.qwen_inference.transcribe_qwen(wrapper, pcm, sr, context,
language, max_tokens, engine="greedy", profile=False)` returns official results
and metrics. Hold the backend's lifecycle lock throughout; hooks are local to
the model instance and removed in `finally`. Greedy uses Transformers' callable
`custom_generate` extension, preserving official preparation, logits processors,
stopping criteria and KV updates. It explicitly rejects batch/beam/sampling,
static-cache, score/attention exports and assistant modes outside this ASR scope.
Prefill calls the official multimodal thinker forward. Greedy continuation calls
the same text decoder directly with the established cache and official rope
formula, avoiding two redundant CUDA-scalar Python conditions per token. Each
request starts with fresh prefill state. The official/prefill/greedy paths retain
no additional device buffers across requests. The graph path owns static buffers
on the shared model and overwrites KV, validity and position for each request.
The upstream model still owns its usual mutable state, including `rope_deltas`.

For fixed-capacity CUDA Graph preparation and short-audio comparisons:

```powershell
.venv/Scripts/python.exe tools/asr/benchmark_qwen_replay.py --manifest output/diagnostics/short-manifest.json --output output/diagnostics/graph-replay.json --attention audio_fa2_text_sdpa --engines official greedy cuda_graph --prepare-cuda-graphs --graph-capacities 384 --warmup 2 --repeats 20 --include-text
```

The default 384-token-capacity graph captures one continuation token with exact
padding/unwritten-tail masks. Audio processing and multimodal prefill remain
dynamic. Preparation uses synthetic state and happens before timed replay;
JSON reports preparation time/memory separately. Use one attention preset while
comparing engines, because changing attention invalidates graph addresses. The
`--prepare-cuda-graphs` option cannot be combined with cold reloads. Park/restore
without another preparation intentionally reports an official route, not a
graph hit. Measure lifecycle preparation separately from warmed replay.

Capacity includes prompt tokens plus the request's maximum output budget; it is
not a duration in seconds. `--graph-capacities 256 384` permits an explicit bucket
comparison. The measured default keeps only 384: the additional 256 bucket saved
little warmed latency while adding capture time and persistent memory. Both
benchmark tools load an explicit official backend so production defaults cannot
silently prepare graphs in the baseline setup.

`Qwen3ASRBackend()` prepares on model load and
`prepare()`, including after RAM restore. The shared speech CUDA coordinator
defers capture while another owner is working; already prepared graphs require
no new capture or GPU synchronization. Unsupported devices/configurations,
missing graphs and requests exceeding prepared capacity select official before
generation and report `fallback_reason`. Capture/replay failures are visible;
there is no failed-inference retry. Metrics distinguish requested and actual
engines, per-chunk prompt/required capacity, cache hit and replay steps. Persistent
buffers and capture use `inference_mode(False)` plus `no_grad()` so a first ASR
capture cannot poison a later TTS capture on PyTorch 2.6; ordinary ASR inference
still uses inference mode. See the [implementation audit](../../docs/asr_cuda_graph_audit_2026-10-14.md).

For same-device GPT-SoVITS coexistence, use the existing production checkpoints
and reference voice without recording or playing sound:

```powershell
.venv/Scripts/python.exe tools/asr/benchmark_qwen_tts_coexistence.py --audio output/diagnostics/audio.wav --output output/diagnostics/asr-tts.json --engines raw_official,official,prefill,greedy --repeats 10
```

The probe warms both fixed Japanese TTS cases before overlapping work, rotates
ASR engines, pairs TTS seeds, and checks waveform hashes plus process-wide math
flags. It reports serial and overlapping ASR/TTS calls on `cuda:0`. The first
voiced chunk metric ends at waveform generation, not physical speaker playback.
TTS retains its existing CUDA Graph path; pass `--prepare-cuda-graphs` and
`--engines official,cuda_graph` to prepare ASR graphs after TTS warmup and before
overlapping work. Ordinary ASR/TTS calls may run concurrently; only new capture
reserves an exclusive window. Attention
overrides are the same explicit offline presets as the replay tool. Do not run
this probe concurrently with other GPU benchmarks or CI timing workloads.
Pass multiple presets, for example `--attention sdpa audio_fa2_text_sdpa`,
to rotate attention × engine combinations within the same process. Each row
records its verified actual configs; setter work stays outside measured calls.
