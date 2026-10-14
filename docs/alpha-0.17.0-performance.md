# 0.17.0 Alpha performance evidence / 性能数据

Recorded measurements from the merged optimization work, collected on 2026-10-13/14.
This release-preflight session **did not rerun these GPU benchmarks**. Its new
regression and live-product results are recorded separately in
[acceptance](alpha-0.17.0-acceptance.md). The rows below use different workloads
and baselines; their percentages must not be added or treated as an end-to-end
conversation speedup.

## Main results

| Optimization | Recorded comparison | Scope and limit |
| --- | --- | --- |
| ASR CUDA Graph (#202) | Paired median backend latency **−75.17%**, 95% interval 74.81–75.57%; Chinese 5.55 s clip p50/p95 **526/569 → 140/149 ms** | Seven fixed clips, 140 pairs on one RTX 4070 Ti SUPER; warm ASR return time, excluding VAD, LLM and playback. |
| TTS semantic-history/EOS work (#205) | Fixed-work AR throughput **+1.2% to +6.0%**, depending on GPU and length | 160 measured pairs, all token/RNG matches; not an end-to-end first-sound claim. |
| TTS shared nested KV (#206) | Unique KV storage **561 → 48 MiB**, saving **513 MiB** | Measured 24-layer FP16 checkpoint and default multi-graph configuration; not total runtime/VRAM. |
| TTS graph reuse (#206) | Default graph count **17 → 6**; explicit common four-bucket capture **17 → 4** | Snapshot preparation timings and finite AR comparisons below, with implementation-revision boundaries. |
| Hybrid frame supply (#200) | Matched 600 s CPU **80.99% → 47.71%** of one core (**−41.1%**) | Same code, route, 384 MiB frame budget and GC settings; no claim of an order-of-magnitude RAM/VRAM reduction. |

## ASR: final single-384 graph comparison

Windows 11 x64, RTX 4070 Ti SUPER 16GB, Python 3.12.10, Torch 2.6.0+cu124,
Transformers 4.57.6, Qwen3-ASR-0.6B BF16, flash-attn 2.7.4.post1, driver 581.57.
Both routes use audio FA2 and text SDPA; the comparator is the previous selected
official generation loop. Seven clips, two warmup rounds per route, 20 measured
rounds per route: **280 measured calls + 28 warmups**. Order alternates by round.

| Audio | Official p50 / p95, ms | Graph p50 / p95, ms |
| --- | ---: | ---: |
| English 3.82 s | 465 / 505 | 130 / 144 |
| Japanese 4.98 s | 711 / 738 | 171 / 179 |
| Chinese 5.55 s | 526 / 569 | 140 / 149 |
| Japanese 7.56 s | 1128 / 1228 | 247 / 258 |
| Japanese 8.57 s | 947 / 1032 | 221 / 282 |
| Japanese 9.29 s | 1226 / 1400 | 269 / 323 |
| Sequential Chinese/English concatenation, 9.57 s | 522 / 570 | 141 / 167 |

The 140-pair median reduction is 75.17%; the 5–10 s subset is 76.44%
(100 pairs, interval 75.34–76.86%). Intervals describe repeated fixed clips,
not a population of natural speech. The concatenated sample is not natural
code-switching. GPU clocks were not locked. All 140 measured Graph calls replayed
with no fallback. The default graph adds about **50.4 MiB allocated / 100 MiB
reserved**; one preparation record was 293 ms warmup + 192 ms capture.

The separate quality set had **53/57 exact text/token matches**. For 24 synthetic
reference cases (1,086 characters), CER was 6.63% → 6.45%; there was also a local
punctuation regression. These are mostly synthetic Japanese references and
perturbations, not an independent human-transcribed Chinese/mixed-language corpus.

Source: [final ASR audit and measurement boundaries](https://github.com/Code-Amadeus/Amadeus/blob/7d61eeff725f0ccd8e6db8c8788db2a04cc7b0e0/docs/asr_cuda_graph_audit_2026-10-14.md).

## TTS: semantic history and masked EOS (#205)

Windows 11, Python 3.12.10, Torch 2.6.0+cu124, FA2 2.7.4.post1, FP16, batch 1,
32 text tokens, 304 prompt tokens, bucket 768. Each row used 15 paired warmups and
20 measured pairs with alternating order. Medians use synchronized complete-call
time; natural EOS was ignored for fixed workload size, while the relevant EOS
predicate/host read still ran. All 160 measured pairs matched tokens and CUDA RNG.

| GPU | AR iterations | Before, it/s | After, it/s | Change |
| --- | ---: | ---: | ---: | ---: |
| RTX 4070 Ti SUPER | 32 | 590.2 | 625.6 | +6.0% |
| RTX 4070 Ti SUPER | 64 | 646.8 | 681.4 | +5.3% |
| RTX 4070 Ti SUPER | 128 | 690.5 | 720.8 | +4.4% |
| RTX 4070 Ti SUPER | 256 | 716.2 | 740.8 | +3.4% |
| RTX 4070 Laptop | 32 | 504.0 | 530.0 | +5.2% |
| RTX 4070 Laptop | 64 | 551.3 | 562.2 | +2.0% |
| RTX 4070 Laptop | 128 | 565.5 | 579.7 | +2.5% |
| RTX 4070 Laptop | 256 | 586.7 | 593.5 | +1.2% |

Separate real-v3 speech validation recorded 72 before/after utterance pairs with
matching PCM, chunk boundaries, semantic tokens/indices and RNG states across
dynamic KV, static KV and CUDA Graph. This equivalence belongs to #205, not to
all later changes in the long-output attention path.

Source: [merged #205 benchmark and validation record](https://github.com/Code-Amadeus/Amadeus/commit/ef3a54b5c6ddacd994081b8129450b7e499d2679).

## TTS: nested storage, graph reuse and promotion (#206)

With the measured checkpoint, default independent-graph KV was 561 MiB; nested
uses a 48 MiB root. Default six-graph preparation recorded 214.39 MiB allocated /
258 MiB reserved for the measured decoder process. Those allocator counters
include more than KV and are not total GPU use. A sparse single-448 deployment
instead changes KV **21 → 48 MiB (+27 MiB)**; the `bucket` option remains available.

The graph-reuse/promotion snapshot was measured before the final exposed-tail
clearing repair. Against earlier nested `4ef8e7`, preparing four common buckets
changed from 17 graphs to four: FA2 preparation **3.922/3.689 → 0.815/1.183 s**
in two construction orders; Laptop SDPA **3.810 → 1.088 s**. These are single
preparation observations, not startup percentile guarantees.

A fixed 224-step AR workload (initial KV 335) promoted 512→768 and retained all
223 decode steps on Graph: FA2 median **798.59 → 350.67 ms**, Laptop SDPA
**925.98 → 579.13 ms**, ten pairs per backend with matching tokens. Clocks were
not locked; some samples overlapped CPU CI. These numbers are explicitly an
implementation-stage snapshot, not a new final-release benchmark.

Controlled full synthesis used a ready synthetic queue, four task types and five
pairs each. First voiced **generated PCM chunk** medians were:

| Task | Earlier nested, ms | Graph reuse/promotion, ms | Exact token/PCM pairs |
| --- | ---: | ---: | ---: |
| First sentence, 10 characters | 300.09 | 299.12 | 5/5 |
| Two fragments, 31 characters | 1123.04 | 1100.35 | 5/5 |
| Three fragments, 83 characters | 5469.72 | 4506.73 | 3/5 |
| Later sentence, 12 characters | 640.19 | 672.92 | 5/5 |

The 83-character row differs in output length in two pairs and cannot establish
a strict equal-work speedup. The later short sentence also showed about 33 ms
more latency. This experiment excludes live LLM scheduling, acoustic output and
ASR overlap. Avoiding the old FA2→dynamic SDPA transition changes some long-output
sampling/EOS trajectories; universal waveform identity is not claimed.

The final tail-clearing repair separately matched **60/60 token pairs** and kept
KV at 48 MiB. Its paired median latency changes ranged from −5.38% to +2.11%
over six backend/workload cells; this is a separate comparison to the preceding
implementation, with one construction order and no frequency locking. It is not
combined with the earlier speedup figures.

Sources: [evaluation, latest sections and preserved historical experiments](https://github.com/Code-Amadeus/Amadeus/blob/7815fdf36480367a1cf57c27e2121379e12967f4/docs/tts_nested_kv_evaluation_2026-10-14.md),
[final tail-clear evidence](https://github.com/Code-Amadeus/Amadeus/blob/7815fdf36480367a1cf57c27e2121379e12967f4/docs/evidence/tts_nested_kv_tail_clear_validation_2026-10-14.json).

## Rendering: matched static/video comparison (#200)

Same product code, 60 FPS nearest-neighbor sampling, 384 MiB FrameStore, GPU GC
60/30, identical full-material route, 600 s each. Both caches contained 7,451
files / 2,610,366,484 bytes with matching hashes and zero first transcodes.
CPU is cumulative CPU time expressed as a percentage of one core; memory means
exclude the first 20 seconds. Both runs contain the same offscreen readback load.

| Metric | Static | Video | Recorded change |
| --- | ---: | ---: | ---: |
| CPU, one-core equivalent | 80.99% | 47.71% | −41.1% |
| Private working set | 833.78 MiB | 792.89 MiB | −40.89 MiB / −4.9% |
| Private commit | 1228.12 MiB | 1209.58 MiB | −18.54 MiB |
| Process dedicated GPU memory | 247.52 MiB | 247.51 MiB | Approximately unchanged |
| Process shared GPU memory | 52.36 MiB | 92.17 MiB | +39.81 MiB |

The gain established here is primarily CPU. Do not sum working set and shared
GPU memory, or claim all application RAM/VRAM falls by an order of magnitude.
The earlier **2 GiB / 190 s / different-route** historical static record had
1861.67 MiB working set and 1560.51 MiB dedicated GPU memory; it is not a matched
baseline for the table above. Video activates only for a matching installed
package and supported decoder; old/missing packages keep static compatibility.
Real desktop motion/alpha/color acceptance and live LLM/microphone/TTS interruption
remain tracked in #201, with platform-specific resource questions in #103.

Sources: [matched 600 s report](https://github.com/Code-Amadeus/Amadeus/blob/2f390367f76985e93a2a5a128882c70d20353e37/docs/frame_video_full_acceptance_2026-10-13.md),
[integration contract and historical/comparable distinction](https://github.com/Code-Amadeus/Amadeus/blob/2f390367f76985e93a2a5a128882c70d20353e37/docs/frame_video_candidate.md).

## 非性能修复与本轮验收

Work 终态事实/权限同步、MCP 环境与审批响应、输出设备的一次有界重试、退出/取消修复，
主要属于正确性和可靠性变化，没有为它们编造百分比。上表不能代表完整对话首声或所有硬件。
本轮新增的真实 Chat → Work → Provider → AUIP 结果必须另记；单元测试、模拟应用、
无模型 GUI、真实模型任务和物理语音验收不可相互替代。
