# VN streaming and bounded inference

This report records the whole-speech-object implementation at `0a2353c` and its
measurements. The subsequent [first-segment delivery change](vn-first-segment-streaming-2026-09-25.md)
starts speech before the text string closes; it changes JSON output ordering
while preserving the story rules. The measurements below remain historical.

## Scope and invariants

This change belongs to `vn_player`. Main Chat and wallpaper inference, the shared
TTS scheduler, English prompt templates, saved profiles and the public `vn.line`
request/response contract remain unchanged.

- Agent and Luna use the same internal source handoff. The runtime admits up to
  three line turns, snapshots their contexts, and starts inference concurrently.
  At the limit it applies backpressure; it does not drop lines or grow an
  unlimited queue. Bridge line counts reflect admission.
- Direct `vn.line` callers still receive a completed reaction. Internally both
  APIs use one admission/processing path; there is no second public text protocol.
- Model requests may finish out of order. Speech delivery, reaction records and
  memory updates wait for their predecessor. All genuine repeated text remains
  eligible input. No stale-comment expiration policy was added.
- Initial model context contains script through that line and memory already
  committed when it was admitted. It can lack a preceding in-flight model's
  memory patch; it is not a promise of sequential-model semantic equivalence.
  Explicit context retrieval retains its existing access to displayed session
  memory. Background summary/reflection use their trigger-local history windows.
- Cadence, cooldown, summaries and reflection use the ordinal of the line being
  processed, not the newest admitted line. Cheap rule-only processing remains
  ordered and reads committed state.
- Stopping/replacing a session cancels its turns and revokes late results. Slot
  cleanup also handles cancellation before a task has started.

## Streaming delivery

VN immediate calls, including explicit player questions and context-request
answers, consume streaming completions. A JSON parser waits for complete
`decision`, `importance`, `confidence` and `speak` values. Partial strings do not
authorize speech. The existing runtime gates authorize one complete utterance,
then submit it to the existing TTS bridge while the JSON tail continues arriving.
Waiting for ordered speech does not stop consumption of the network stream.

The final tail may supply memory patches. It cannot rewrite an already delivered
speech header. A broken stream retains its delivered decision, discards the
unverified memory tail and records `vn.immediate.stream_incomplete`; it never
replays the speech. Duplicate JSON keys are rejected. Streamed requests have no
SDK retry; nonstreaming background lanes keep their existing retry behavior.

Connections are reused within the VN session. Changing credentials/base URL
creates a separate client without closing in-flight requests; session shutdown
closes its clients. There are no new frontend switches or profile settings.

## Timing and measurements

Runtime diagnostics now correlate line admission, request start, first token,
complete speech header, speech submission, model completion and ordered commit.
Context retries retain separate request timings. These clocks do not establish
the original game-display time: a source needs its own capture clock for that.

### Matched model prompts

Reused three recorded PARANORMASIGHT prompts, unchanged, with simulated arrivals
one second apart. Ran all four combinations twice, reversing their order on the
second pass: 24 real calls, all valid JSON. Both arms reused clients, so this
comparison does not include savings from replacing the old per-request client.

Five sample/repetition pairs spoke in all four modes; one other pair changed its
decision between calls. The table uses only the five matched speaking pairs.
Values include simulated input waiting and ordered delivery, but exclude TTS.

| Completion mode | In-flight limit | Median input → speech eligible |
| --- | ---: | ---: |
| Full JSON | 1 | 5.232 s |
| Streamed speech header | 1 | 3.920 s |
| Full JSON | 3 | 3.280 s |
| Streamed speech header | 3 | 2.298 s |

This controlled model/ordering experiment reduces the median by about 56%.
It is not a measurement of physical sound or a new live-game acceptance.

### Actual audio-device playback

Ran the production VN runtime, VN TTS bridge, configured GPT-SoVITS synthesizer
and shared physical player in an isolated process. No game, microphone or desktop
host was launched. Warmup was excluded and sentence-audio caching disabled. Every
trial verified nonzero samples sent to the device. Timing uses the existing
first-audio-write playback marker, not an acoustic microphone loopback.

The longer-comment experiment restored preceding displayed lines, supplied the
same recorded model prompt, and retained the runtime's ordinary commentary gates.
Four trials used full/stream/stream/full order. Model wording can still vary.

| Input | Full JSON median → first sound | Streaming median → first sound |
| --- | ---: | ---: |
| Very short fixed VN answer, 2 trials per mode | 2.106 s | 2.068 s |
| Recorded game-comment prompt, 2 trials per mode | 4.061 s | 2.381 s |

The recorded comment gained approximately 1.680 seconds (41%). In one streamed
trial, first sound occurred at 2.517 seconds and the full reaction completed at
3.781 seconds. Short fixed answers showed little benefit; their JSON tail is
small. These are small samples with provider and synthesis variation, not a
general performance guarantee.

Sequential playback remains necessary: when an earlier comment is still playing,
a later comment waits even if its model result is ready. Long speech and sustained
input faster than model capacity can still produce backlog.

## Verification and reproduction

- 225 relevant Python tests passed; lint, generated architecture checks and
  Electron production build passed (existing large-chunk warning).
- The complete model-less Electron smoke passed backend startup, authenticated
  control, renderer navigation and clean desktop/backend shutdown.
- Python regression coverage includes fragmented JSON, escaped strings,
  duplicate fields, broken tails, connection reuse/settings changes, overlapping
  requests, ordered delivery, pause, stop/restart, pre-start cancellation, explicit
  questions, context retries, both adapters and background cadence/window length.
- Model-off replay against `54d700e`: 61 observations on the accepted original
  trajectory, no reaction/summary/reflection differences. Both sequential and
  concurrent submission passed. Genuine repeated-line retention is tested
  separately; the comparison tool retains its original trajectory filter.
- Prompt compatibility fixtures continue to compare full messages byte for byte.

Network/model calls and physical audio are opt-in. Supply your own local session;
no script text, character media, raw game dialogue or credentials are bundled.

```powershell
.venv/Scripts/python.exe -X utf8 tools/probes/measure_vn_model_latency.py --session <session-directory> --live-model
.venv/Scripts/python.exe -X utf8 tools/probes/measure_vn_audio_latency.py --session <session-directory> --live-model-and-audio
.venv/Scripts/python.exe -X utf8 tools/probes/compare_vn_mystery_replay.py --baseline-ref 54d700e --trace <raw-lines.jsonl> --script <script-file> --concurrent-current
```

Local measurement artifacts are under
`output/diagnostics/vn-streaming-20260925/`; they are intentionally not published.
