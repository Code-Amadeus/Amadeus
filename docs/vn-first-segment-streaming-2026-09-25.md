# VN first-segment speech delivery

## Owning layer and contract

The previous VN implementation streamed the network response but waited for the
complete `speak` object. This was an integration delay between model output and
speech submission. The VN client now decodes an append-only speech string once
the decision, importance, confidence and playback metadata are complete. The
runtime submits a safe first segment while the same model call produces the rest.
There is no additional model pass, public protocol, profile option or dependency.

- Both Base and Mystery use the same delivery controller and retain their
  existing gates. Agent and Luna still share one runtime input path.
- The first segment uses the ordinary voice path's sentence-boundary helper.
  JSON escapes, Unicode surrogate pairs and incomplete control tags are buffered.
  Control tags do not become spoken text or executable actions; confirmed
  `emotion_intent` still reaches the existing bridge.
- An utterance is authorized and charged against commentary cadence once.
  If a partial reply is indistinguishable from a recent reply, authorization
  waits for enough text to preserve the existing repeat check.
- One worker coalesces cumulative text updates. A per-session utterance lock
  prevents another VN answer from appearing between its segments. Model streams
  keep being consumed while ordered delivery waits.
- Every segment rechecks session ownership and pause state. Rejected segments
  are not retried. Reaction records retain only text accepted by the sink;
  legacy callbacks without an explicit receipt retain their existing behavior.
- The Host supplies its existing TTS playback epoch. The runtime captures it at
  utterance authorization, so a user interruption revokes the remaining segments
  without preventing a new answer. The VN core owns no second interrupt counter.
- Broken streams discard unsent fragments and unverified memory patches, and
  never replay accepted speech. Cancellation releases the delivery lock.
- Host-assigned segment numbers give narration requests distinct identities and
  preserve first-sentence TTS settings only for the first segment. Ambient VN
  does not enter the direct-conversation turn gate. Main Chat and wallpaper
  inference and the shared audio scheduler are unchanged.

## Prompt compatibility

Immediate output examples now place `decision`, `importance`, `confidence` and
`speak` first, with `text` last inside `speak`. One English format instruction
requests this order. Models emitting another order can still use complete-object
delivery; no provider-specific parser or game-specific bypass is introduced.

Story/character/type rules, schema keys and values, optional terminology, and
user-context construction are preserved. Tests retain the original fixtures and
permit only the declared format instruction and JSON example ordering. Other
lane prompts remain byte-for-byte identical. No game knowledge was added.

## Real audio A/B

The opt-in probe restored the same recorded PARANORMASIGHT context and used the
production VN runtime, bridge, configured GPT-SoVITS and physical audio player.
Warmup was excluded, audio caching was disabled, and all four trials produced
valid model speech with nonzero device-written audio. No game, microphone or
desktop host was launched. First sound means the existing first-device-write
marker, not an acoustic loopback measurement.

Both arms used identical new prompts and streaming network requests. The
baseline withheld text until its JSON string closed; the new arm submitted safe
segments incrementally. This isolates waiting for the speech body. It does not
separately measure the field-order change against the previous prompt format.

| Trial | Delivery | Input → first token | First token → TTS submission | Input → first sound |
| --- | --- | ---: | ---: | ---: |
| 1 | Whole speech body | 1.083 s | 0.948 s | 2.674 s |
| 2 | First segment | 1.535 s | 0.430 s | 2.579 s |
| 3 | First segment | 0.982 s | 0.277 s | 1.799 s |
| 4 | Whole speech body | 1.303 s | 0.713 s | 2.433 s |
| Median | Whole speech body | 1.193 s | 0.831 s | 2.554 s |
| Median | First segment | 1.259 s | 0.354 s | 2.189 s |

The median wait from first token to TTS submission fell about 57%. Median first
sound was about 0.365 s / 14% earlier. Provider first-token time varied enough
to obscure much of the gain in trial 2. These are two trials per arm, not a
general performance guarantee or a new live-game playthrough.

This removes one avoidable wait. Provider latency, TTS startup, and existing
queued audio remain. Longer comments still occupy the player and may delay the
next one. No stale-comment dropping or new interruption policy was introduced.
Earlier full-JSON/concurrency measurements are recorded separately in the
[preceding report](vn-streaming-and-concurrency-2026-09-25.md).

## Verification and reproduction

- 252 relevant Python tests passed: VN suites, narration/control/vision/auth
  boundaries, shared sentence splitting and chat protocol roles.
- Regression coverage includes speech before body and memory-tail completion,
  one-time cadence charging, both sides of repeated-prefix disambiguation,
  broken bodies, split escapes/tags, rejected later segments, pause/restart
  after the first segment, Host interruption followed by a fresh answer, and
  utterance ordering.
- Model-off replay against `0a2353c`: 61 observations on the accepted original
  trajectory, no reaction, summary or retrospective differences under concurrent
  submission. Genuine repeated input has separate tests; the replay tool retains
  its original trajectory filter.
- Ruff, both generated architecture checks, Electron build and the full
  model-less Electron startup/authentication/navigation/shutdown smoke passed.
  The build retains its existing large-chunk warning.

```powershell
$vnTests = @(rg --files tests -g 'test_vn_*.py')
.venv/Scripts/python.exe -X utf8 -m pytest @vnTests tests/test_cross_scene_narration_baseline.py tests/test_ws_control_preemption.py tests/test_visual_runtime_settings.py tests/test_local_instance_auth.py tests/test_backend_instance_auth_contract.py tests/test_sentence_splitter.py tests/test_chat_protocol_roles.py -q
.venv/Scripts/python.exe -X utf8 tools/probes/measure_vn_audio_latency.py --session <session-directory> --baseline-delivery whole-speak --live-model-and-audio
.venv/Scripts/python.exe -X utf8 tools/probes/compare_vn_mystery_replay.py --baseline-ref 0a2353c --trace <raw-lines.jsonl> --script <script-file> --concurrent-current
```

Network/model calls and physical audio are opt-in. Raw experiment artifacts are
local under `output/diagnostics/vn-first-segment-20260925/`; game dialogue,
scripts, model outputs, voice material and credentials are not published.
