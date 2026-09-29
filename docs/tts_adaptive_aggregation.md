# Sentence-bounded adaptive TTS grouping

Configuration discussion: [#132](https://github.com/Code-Amadeus/Amadeus/issues/132).
Draft policy: [#131](https://github.com/Code-Amadeus/Amadeus/pull/131), based on the
separate readiness/accounting fix [#133](https://github.com/Code-Amadeus/Amadeus/pull/133).

The goal is to complete the current sentence when playback cover permits it.
It is not to maximize batch size across sentences. First-unit streaming remains
unchanged; subsequent groups stop at a full stop, question mark, exclamation mark
or newline, including trailing closing quotes/brackets. Under time/length limits,
the scheduler submits a prefix at an existing upstream fragment boundary.

## Configuration and compatibility

The scheduler remains disabled by default (`ENABLE_TTS_UTTERANCE_SCHEDULER=0`).
For an explicit opt-in starting with the second unit:

```dotenv
ENABLE_TTS_UTTERANCE_SCHEDULER=1
TTS_UTTERANCE_MIN_START_SEQ=2
TTS_UTTERANCE_MAX_CHARS=120
TTS_UTTERANCE_FLUSH_TIMEOUT_MS=350
# Leave TTS_UTTERANCE_MAX_SENTENCES absent for adaptive grouping.
```

An explicitly set `TTS_UTTERANCE_MAX_SENTENCES` is always a hard fragment-count
limit, regardless of available cover. Fragments may be comma clauses. Invalid
values retain the historical fallback of three; values below one clamp to one.
If the setting is absent or blank, usable timing enables adaptive grouping; without
timing (including disabled deadline aggregation), the fallback is three.

`TTS_UTTERANCE_FLUSH_TIMEOUT_MS=0` retains its previous meaning: no grouping,
including ready fragments. Positive values bound empty-queue lookahead; already
ready text can be grouped without waiting. `TTS_UTTERANCE_MAX_CHARS` caps merging;
it does not truncate or split an oversized incoming request.

Migration from original #131: explicit limits now work again. Remove the count
setting to opt into adaptive grouping. Complete sentences are never joined just
to fill the character limit. Default-disabled installations need no migration.

## Ownership and cost model

After turn authorization and inference-slot acquisition, sample playback cover
once and set an absolute monotonic finish deadline. A candidate is affordable if:

`now + predicted_producer_seconds(candidate) <= start + cover - safety_margin`

The first queued fragment is always eligible for immediate submission. If it
already exhausts the budget, there is no lookahead wait. Otherwise waiting is
bounded by both remaining flush time and the current prefix's synthesis reserve.
New audio arriving during lookahead can only make this snapshot conservative.
Cancellation returns every scheduler-owned item to the buffer in order.

The synthesis layer shares text preparation and effective parameter selection
between predictions and actual requests. Local profiles include language, actual
sample steps (including the ROCm cap), splitter and graph mode. Remote backends
do not inherit fictional local 16/32-step tiers. Runtime replacement clears
observations; ordinary turn changes retain performance knowledge.

Each profile holds five recent successful later-unit observations. An affine fit
is only identifiable when there are five samples and the second-longest length is
at least twice the second-shortest: two samples at each end must support the
length difference. Median pairwise slopes and median residuals then estimate
variable and fixed cost; nonpositive slopes are rejected. Fitting happens when an
observation arrives, not once per grouping candidate.

Otherwise, after three samples, use the median of `t * max(1, m/n)` for a request
of length `m` and observed time/length `(t,n)`. For a stable nonnegative affine
cost `a+b*n`, this bounds both shorter and longer requests without guessing which
part of the measured time was fixed overhead. An accepted affine fit is used for
interpolation; extrapolation beyond the observed maximum must also satisfy this
envelope. Below the measured minimum, do not predict less than the fitted minimum
request cost. Robust medians address isolated noise, but are not a statistical
guarantee under arbitrary timing noise, nonlinear cost or changing performance.

The old repeated-short-unit test encoded an unsupported assumption about fixed
overhead. Both fixed .4 seconds and .1 seconds/character explain .4 seconds at
four characters; neither supports predicting 40 characters in 1.12 seconds.
The regression now covers the slower explanation too. The reviewed .5 + .2*n
example changes from 6.1 seconds predicted for 40 characters to 9 seconds, against
8.5 seconds actual in that synthetic model.

Unobserved profiles start from `TTS_RTF_INITIAL / TTS_CHARS_PER_SEC`, scaled by the
effective local sample-step ratio relative to 16. This is a bootstrap assumption,
not calibrated hardware evidence. No fixed-overhead estimate exists before data.
The clock covers work inside the producer thread; queue/player waits, openings,
cache hits, errors and cancellations do not train the model. Appended silence
does not reduce measured cost. The global previous-length growth cap is removed.

The scheduler emits an INFO decision record with reason, count, characters,
predicted time and budget, without conversation text. Cover is scanned once per
job, rather than repeatedly per candidate.
Identity/character-limit rejects do not run candidate text preparation. A malformed
queue item is rejected once with queue accounting completed; cancellation still
restores valid items. Chat splitting and grouping share the strong boundary set.

## Engine boundaries

Merged jobs do not override `how_to_cut`. The canonical >=45-character profile
retains its adapter splitter, and existing semantic-generation guards remain.
A host character cap is not proof of a safe autoregressive token length. The
previous semantic-only length sweep was not an audio/intelligibility validation;
its raw probe is superseded here by the full guarded synthesis probe.

Speed stays at the existing 0.97 multiplier (first profile 1.067), with 400 ms
appended pause. Static KV/graph reuse within a decode is unchanged. This feature
does not implement cross-request semantic/KV continuation.

The initial revision inadvertently changed profile selection when pronunciation
expanded the text. This is now restored: buffered/graph synthesis selects using
the pre-expansion text; the legacy enhanced mode retains its post-expansion
selection and terminal-mark insertion. The existing ROCm 16-step cap remains on
the buffered/graph path. Prediction uses the selected mode's same preparation
helper; these behavior differences are pre-existing, not new policy choices.

Prebuilding and runtime opening lookup share `_prepare_synthesis_request`. The
13-character input containing `AI` expands to 15 characters but retains the
buffered mode's 3.5-second duration override in both paths. The regression test
stores prebuilt audio and proves runtime uses it without invoking inference.
No cache schema or identity has changed, so bumping the version would invalidate
valid entries unnecessarily; entries generated with different parameters already
have different keys.

## Deterministic and synthetic evidence

Windows/Python 3.12 model-less TTS tests: **466 passed, 3 skipped** at this revision.
Coverage includes explicit/absent limits, zero flush, sentence/quote boundaries,
short openings, lookahead reserves, cancellation, retained playback cover,
prepared-text consistency, effective 44/45-character profiles, outliers and
runtime replacement. Existing full-sentence role delivery tests now assert
separate sentence jobs while preserving turn identity.

Run the synthetic comparison:

```powershell
python -X utf8 tools/probes/simulate_tts_aggregation.py --output simulation.json
```

It loads main `00e3864` and original PR `e27f58f` schedulers and their deadline
helpers, using each revision's own observation rule. Inputs are 96 eight-character
fragments, 0.8 seconds of opening audio, a 1.5-second safety margin, fixed request
overhead, and double variable cost at >=45 characters. Most sentences contain six
fragments; the audit case has 96. Output/audio duration is synthetic and does not
model the vocoder's internal splitting or physical speaker.

| Scenario | Main gaps / jobs | Original PR gaps / jobs | Revised gaps / jobs |
| --- | ---: | ---: | ---: |
| Fast (rate .15) | 0 s / 49 | 0 s / 11 | 0 s / 20 |
| Medium (.6) | 0 s / 50 | 1.9467 s / 20 | 0 s / 25 |
| Slow (1.2) | .56 s / 71 | .56 s / 73 | .56 s / 73 |
| Audit long sentence (.6) | 0 s / 50 | 1.9467 s / 20 | 0 s / 19 |
| Fixed overhead .5 s | 0 s / 50 | 0 s / 12 | 0 s / 21 |
| Text arriving every .45 s | .22 s / 50 | 4.1834 s / 19 | 0 s / 25 |
| Abrupt .15 -> 1.2 slowdown | 0 s / 49 | 22.8533 s / 35 | 6 s / 44 |

The abrupt-slowdown regression relative to main is real in this model. The
revised scheduler limits the effect compared with the old PR but cannot predict
an unseen slowdown, and profile-local histories can lag changes already observed
in another profile. The new trace records 5.68 seconds at unit 10 (48 characters,
after 24 input fragments) and .32 seconds at unit 13 (40 characters, after 36
fragments). It is therefore not valid to dismiss all six seconds as one
unavoidable first surprise. The nine-sample fit previously produced 6.64 seconds;
both the previous five-sample fit and the corrected envelope produce 6. Fewer groups
also insert less tail silence, so fewer pauses must not be confused with higher
synthesis throughput. This is not a universal-optimality or no-underrun claim.

## Full guarded synthesis and listening gate

```powershell
python -X utf8 tools/probes/render_tts_sentence_planning.py `
  --asset-root <existing-model-installation> --output-dir <local-output> --rounds 2
```

This separate process uses the installation's configured model and matching
reference audio/transcript, the same dictionary and pacing for both strategies,
and regular guarded `infer_stream` including the vocoder. It replays a ready LLM
burst with simulated audio cover and saves WAVs, timing, semantic attempts and
graph-key counts. It does not play audio or measure the physical speaker.
The first round is fixed/revised; the second is revised/fixed to expose warm-order
effects. GPU/runtime conditions still limit generalization.

Prior revision `c143a38` local v3hc run on 2026-09-29, 16 fragments and identical model/reference/dictionary
for both strategies, using the installation's existing Python environment and
BigVGAN CUDA binary cache:

| Order | Jobs | Largest group | Producer total | Second/later ledger gaps |
| --- | ---: | ---: | ---: | ---: |
| Fixed, round 1 | 10 | 28 chars | 8.33 s | 0 s |
| Revised, round 1 | 8 | 66 chars | 7.57 s | 0 s |
| Revised, round 2 | 8 | 66 chars | 7.12 s | 0 s |
| Fixed, round 2 | 10 | 28 chars | 6.26 s | 0 s |

All 36 requests produced non-silent audio; no observed semantic attempt hit its
configured generation ceiling. The 66-character group used 32 steps and the
canonical four-sentence splitter; this input still produced one decode, ending
naturally at 280 tokens. Its decoder log reports bucket 768 and 280 graph replay
steps out of 281. Preserving the splitter does not mean every long request will
be split, and this sample does not establish a generally safe token length.

The reversed order demonstrates why producer totals must not be called a speed
improvement: the warmed fixed strategy was faster. An earlier 10-fragment trial
produced seven jobs for both strategies, also with zero measured ledger gaps.
The useful evidence is changed sentence grouping without an observed underrun in
these cases; it is not proof of better prosody or performance on another device.

Human listening and actual application playback remain required before promoting
this policy from Draft. Earlier local listening feedback mixed policy changes
with independent pronunciation work and is not isolated A/B evidence.
The full GPU A/B above has not been rerun for the extrapolation/cache revision.
The remaining slowdown tradeoff needs explicit maintainer acceptance or a further
policy change; fixing the three new correctness bugs alone is not a merge gate.

## Audit disposition

All 14 findings from the accepted 2026-09-28 Claude review were retrieved.
The forced splitter overrides, global growth field, max-biased EMA, unused old
deadline wrapper and duplicate boundary predicates are removed. Explicit settings
are honored, canonical preprocessing/profile selection is shared, and one budget
snapshot replaces repeated ledger scans. Stop reasons are logged. The separate
facts PR handles waiting-item cleanup and stream-claim tests. Tests no longer
depend on an ambient deadline flag for the deleted growth rule. Experimental
limitations above remain open acceptance concerns rather than resolved claims.
