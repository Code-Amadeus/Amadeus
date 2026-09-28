# Bounded adaptive TTS aggregation

Status: locally trialled candidate with deterministic experiments and a limited
CUDA decoder probe. Not a cross-device acoustic or naturalness qualification.
The existing `ENABLE_TTS_UTTERANCE_SCHEDULER` switch remains **off by default**.
This work is separate from the speed/pause defaults in PR #130.

## Problem and owning layer

The old scheduler waits up to its lookahead timeout even when a short opening
leaves no synthesis reserve. With ample cover it still stops at a fixed number
of punctuation fragments. It also finalizes text before waiting for an inference
permit, missing text that arrives during that wait. These are TTS scheduling and
measurement integration problems, not a need for another semantic model.

The scheduler now selects a contiguous prefix of already available fragments
subject to identity boundaries, synthesis budget and length limits. Speech and
subtitle segment identities remain intact. The first/explicitly streamed unit
still bypasses aggregation. Questions and exclamations remain hard boundaries.
The adapter is told not to re-split a host-planned merged utterance. Otherwise
the long-text four-fragment splitter would undo part of the planning. Completed
audio is registered immediately; playback alone owns waiting for its turn, and
ready-but-waiting audio remains visible to the cover estimator.

## KV and prosody scope

The existing KV cache accelerates autoregressive generation within an inference
request; its K/V state is initialized again for the next request. Graph storage
and reference-conditioning caches are reused, but do not carry the previous
utterance's generated prosody forward. `ENABLE_TTS_KV_WINDOW` is only a reserved
switch with no production consumer. The separate
`tools/test_sentence_prefix/ab_test.py` experiments with previous speech-token
prefixes and is not part of the production playback path.

This change gives the synthesizer more contiguous text in each planned unit.
It does not claim cross-unit KV continuation, an emotional-state model, or
guaranteed perceptual improvement. Those require separate evidence.

## Policy

- The host confirms turn permission and obtains the existing synthesis permit
  before final grouping. It snapshots interruption identity before either wait.
- Remaining lookahead time is `cover - safety margin - estimated synthesis`.
  A nonpositive value emits the current fragment immediately. The first
  fragment is always deliverable even when no candidate can meet the deadline.
- Ready text can be consumed without spending lookahead time, including when
  the configured wait is zero. An already complete sentence does not wait for
  another sentence, though already queued following text may join it.
- When a usable deadline estimate exists, it replaces the fixed fragment-count
  limit. The selected prefix is maximal under the modeled constraints; this
  is not a claim of global optimality over unknown future text or hardware load.
- Expansion is bounded to twice the most recently completed non-opening job's
  character count, and never exceeds `TTS_UTTERANCE_MAX_CHARS`. This limits
  extrapolation from small jobs into different step profiles or Graph shapes.
  A single input fragment is not split or truncated to meet the merge limit.
- Cost comes from the producer thread's actual iteration time and original
  synthesis-input character count. It excludes waiting for permits, playback,
  cache hits and fast-opening observations. Added silence cannot reduce the
  observed cost. Slow observations apply immediately; faster observations use
  the existing 0.3 smoothing weight.
- `TTS_CHARS_PER_SEC` normalizes both prediction and observation, so after
  learning, the cost is effectively seconds per character. The initial estimate
  remains conservative configuration, not a benchmark from one GPU.

No GPU name, measured laptop throughput or new model invocation enters the
policy. CPU, CUDA, ROCm and remote paths share the cost observations. Models,
languages and input content can still have different nonlinear costs; the
estimator is intentionally approximate.

## Configuration compatibility

`TTS_UTTERANCE_MAX_SENTENCES` continues to bound jobs when deadline aggregation
is disabled or its estimates are unavailable. With valid adaptive estimates it
no longer caps the number of comma fragments. Existing character, start-sequence
and maximum-wait settings retain their roles. The character cap remains 120 by
default; no machine-specific cap was introduced. Larger local overrides remain
explicit operator choices.

The existing GPT-SoVITS adapter selects and reuses CUDA Graph keys. It can fall
back to dynamic KV when a bucket fills. The scheduler's character cap prevents
unbounded aggregation but does **not** guarantee Graph replay for every token:
reference context, phonemes and generated semantic length also determine it.

## Deterministic experiments

Command (the baseline is the pacing-only commit, before scheduler changes):

```console
python -X utf8 tools/probes/simulate_tts_aggregation.py --baseline-ref 00e3864
```

This is a synthetic ready-text burst: 96 eight-character fragments, an 0.8 s
opening, 80 ms per-request overhead, 400 ms trailing silence, 1.5 s reserve and
a 120-character cap. It is not a wall-clock benchmark. Both policies use the
same synthetic speed feedback so the comparison isolates grouping. Asynchronous
tests separately cover late arrivals, bounded waiting, cancellation and permits.

| Synthetic synthesis/speech ratio | Calls: old → new | Second-unit gap: old → new | Total starvation gap: old → new |
|---|---:|---:|---:|
| 0.15 | 49 → 11 | 0 → 0 s | 0 → 0 s |
| 0.6 | 50 → 12 | 0 → 0 s | 0 → 0 s |
| 1.2 | 73 → 73 | 0.56 → 0.56 s | 0.56 → 0.56 s |
| 0.15 → 1.2 after 16 fragments | 49 → 17 | 0 → 0 s | 0 → 2.3733 s |

Fewer calls also mean fewer explicit 400 ms pauses. They do not establish a
measured GPU speedup. The abrupt eightfold slowdown is a remaining risk: no
historical-rate estimator can foresee it. After observing it, the candidate
shrinks subsequent groups (15, 11, 10, 8, 7, 6, ... fragments). Removing the
growth bound caused 3.76 s of starvation in that same scenario; the bound
reduces the excursion but does not eliminate it. Do not tune a universal
constant merely to make this one trace pass.

The contract grid also verifies maximal prefixes and monotonic behavior across
five synthesis rates and five playback-cover values. Slower synthesis never
allows a larger group under otherwise equal constraints, and greater cover
never reduces it. This is the precise, limited optimality claim being tested.

## CUDA evidence and limits

The optional probe can be reproduced against an existing local model install:

```console
python tools/probes/measure_tts_graph_lengths.py --asset-root <installation> --output graph-lengths.json
```

It uses that installation's matched model/reference configuration, performs no
audio playback, and forces offline model loading. Run separately from latency
acceptance; this probe itself consumes GPU resources.

A silent T2S-only probe used the installed V3 model and matched reference on a
Windows RTX 4070 Laptop GPU (8 GiB). Seven synthetic Japanese prefixes from
13 to 111 characters were decoded twice. Three of the 14 trials hit the
1,000-token limit and are not valid pacing measurements. A preliminary run
with the default reference transcript was discarded because it did not match
the installed reference. No voice material, raw logs or personal paths are
included here.

Observed examples: the 13-character input filled its 448 bucket and continued
with dynamic KV; warm 38/50/81/111-character inputs reused 768-bucket Graphs
without that fallback. First use of new keys incurred capture work. These
observations rule out treating raw character count as a reliable Graph-hit
threshold. They do not establish intelligibility, full TTS latency or an optimal
length, and no measured local value was used as a global scheduling constant.

## Validation and remaining acceptance

The focused suites cover opening bypass, short-opening continuation, rates and
cover, length/growth caps, deadline-bounded waits, turn/source/epoch separation,
late arrivals, real worker permit ordering, cancellation, all synthesis modes,
cache behavior and subtitle identity. The new behavioral tests first reproduced
five failures on the unchanged scheduler.

Validation with `AMADEUS_E2E_NO_TTS=1`: all 28 test files importing the shared TTS
package passed **410 tests**, with **3 existing optional-tier skips**. Ruff
0.16.3 passed for changed Python files; `git diff --check` passed. An existing
Python `audioop` deprecation warning remains. These are targeted tests, not the
repository-wide suite or new live microphone acceptance.

The maintainer subsequently tried the candidate through the normal Windows BAT
launcher and reported a noticeable improvement. That listening checkout also
retained separate Japanese-pronunciation changes and passed **435 tests**, with
**3 skips** after integration. Those pronunciation changes are excluded from
this contribution. The feedback supports the direction but is not a controlled
acoustic A/B measurement or a qualification of other hardware/providers.

Before broad activation, repeat live continuous-speech listening with the
intended model/provider, and measure actual first-to-second and later gaps.
Especially check rapid load changes and model step-profile transitions. This
candidate does not establish stable speech on an engine whose output cannot
keep up with consumption, nor arbitrary sudden-slowdown immunity.
