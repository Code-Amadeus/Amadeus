# Barge-in, AEC, and TTS Interrupt Notes

Date: 2026-05-29

This document records the debugging notes and technical decisions around Amadeus' realtime barge-in path:

```text
LLM stream -> TTS synthesis -> Playback/PyAudio -> expression/lip-sync
                                 ^
                                 |
                         mic/VAD/AEC barge-in
```

The goal is simple: when the user speaks while Amadeus is talking, the current assistant turn should be aborted immediately, old audio should never keep playing, and ASR should take over cleanly for the user's new input.

## Current Direction

The chosen architecture is:

- Use a lightweight barge-in detector during assistant playback.
- The detector uses mic energy / VAD plus realtime AEC residual, not Qwen ASR.
- When barge-in is detected, abort the current chat turn and interrupt TTS/playback.
- Keep Qwen ASR hot for a short idle window after wake/interaction, but do not let it continuously listen during TTS playback.
- Guard all playback work with epoch ownership so stale audio tasks cannot play after interrupt.

This keeps the expensive ASR model out of the hottest playback path while preserving a route to proper interruption.

## Main Bug: Stale Playback After Interrupt

Observed log shape:

```text
[BargeIn] speech start
barge-in speech detected; aborting current chat/TTS turn
[TTS-INTERRUPT] playback queue cleared
annotated interrupted assistant turn

...but then...
PlaybackManager starts sentence_5_...
StreamPlayer starts physical playback sentence_5_...
```

The important detail was that `sentence_5` had already finished TTS and had been added to the playback playlist before the interrupt.

Root cause:

- `PlaybackManager.run()` could take an item out of `pending_audio`.
- Then it waited on `player_is_ready`.
- Interrupt cleared queues and called `player_is_ready.set()`.
- The old `run()` iteration woke up and started the already-owned stale sentence.

So the interrupt itself was firing correctly. The missing piece was ownership validation after every async boundary.

Fix:

- Added `PlaybackManager._playback_epoch`.
- `interrupt()` increments the epoch before clearing queues.
- Playlist items, stream playback tasks, segment-start tasks, and physical playback receive/check the epoch.
- After `player_is_ready.wait()`, playback rechecks the item's epoch before starting.
- PyAudio write paths receive an `is_current()` callback and skip stale writes.

Expected invariant:

```python
if item_epoch != current_playback_epoch:
    discard_or_return()
```

This check is intentionally cheap: it is only an integer comparison or a tiny callback before important actions. It does not block on IO, GPU, locks, or model inference.

## Second Bug: Stale Inference After Interrupt

After playback ownership was fixed, old audio stopped playing, but old TTS jobs could still enter the CUDA Graph/static KV inference path after a barge-in.

Observed log shape:

```text
[TTS-INTERRUPT] epoch=3 drained_pending=3
[TTS-INTERRUPT] drop queued synthesis result: sentence_5_...

...but then stale jobs still entered...
[Graph Serial] acquired lock: sentence_7_...
[TTS-FUNC-ENTER] sentence_7_...
[Graph Serial] force CUDA Graph params: sentence_7_...

...and tts_inference still ran...
[T2S] using static KV cache mode
[T2S] using static KV cache mode
```

The dangerous symptom was CUDA Graph stats such as `graph_replay_steps > total_steps`, which strongly suggests shared runtime state was being touched concurrently or after its logical owner had been invalidated.

Root causes:

- Stale jobs were only dropped after synthesis results arrived.
- Jobs already waiting for the Graph Serial lock were not invalidated before acquiring the lock.
- The Graph Serial lock could be released by the async consumer while the background producer thread was still inside `infer_stream`.
- Queue draining did not cover jobs that had already left the sentence queue and were waiting on semaphore/graph lock.

Fix:

- Check interrupt epoch before Graph Serial lock acquisition.
- Check interrupt epoch again immediately after acquiring the Graph Serial lock.
- Check epoch before creating the TTS task after semaphore acquisition.
- Check epoch before submitting the background producer.
- The producer checks epoch before entering `infer_stream` and between yielded chunks.
- If an active graph producer is interrupted, the consumer waits for that producer to exit before releasing the graph lock.

Current invariant:

```text
Old epoch jobs may finish cooperatively, but they must not start new graph inference after interrupt.
If a graph producer has already entered inference, it keeps graph ownership until it exits.
```

This means barge-in may have to wait for one already-running TTS step to unwind, but it prevents static KV / CUDA Graph corruption and avoids multiple stale jobs entering the graph path.

## Boundaries That Need Epoch Checks

Every boundary below can outlive the turn that created it:

- Adding synthesized audio to `pending_audio`.
- Pulling audio from `pending_audio`.
- Waiting for `player_is_ready`.
- Starting physical playback.
- Writing PCM to PyAudio.
- Sending mouth/lip-sync values.
- Pushing AEC reference audio.
- Dispatching expression segment-start events.
- Marking sentence complete.
- Firing turn playback complete callbacks.

The rule is that stale work may finish quietly, but it must not mutate current playback state or produce audible/visible output.

## AEC Debugging Notes

Realtime AEC was introduced to reduce false barge-in from Amadeus hearing her own speaker output.

Important lessons:

- AEC needs a reference stream from the exact TTS PCM that is being played.
- The reference must be aligned closely enough with the mic signal. Delay differs by output device.
- Bluetooth headphones, wired headphones, laptop speakers, and external speakers can all have different delay.
- The first goal is not perfect echo cancellation. The practical goal is to make the residual reliable enough for VAD-based barge-in.
- Capturing `reference.f32` and `mic.f32` is useful for offline alignment checks, but final validation must happen in the realtime path.

Known environment knobs:

```text
AEC_REALTIME_ENABLED=1
AEC_REALTIME_BARGE_IN=1
AEC_REALTIME_DELAY_MS=280
ASR_ECHO_TAIL_GUARD_MS=650
```

The delay value is not universal. It may need per-device tuning later.

## Barge-in Detector Decision

Rejected approach: let Qwen ASR listen continuously during assistant playback.

Reasons:

- Qwen ASR is expensive compared with VAD/AEC.
- It adds scheduling and GPU contention near TTS playback.
- It makes echo/self-speech handling harder because the ASR model may transcribe Amadeus' own voice.
- It complicates interruption timing.

Chosen approach:

- During TTS playback, run a lightweight barge-in detector.
- Detector decides only "user speech likely started".
- Once barge-in fires, stop TTS/playback and then hand control to ASR.

This separates the realtime interruption problem from the recognition problem.

## SenseVoice Wake/Bridge Lessons

SenseVoice was useful as a lightweight wake recognizer, but the temporary "bridge" mode was not a good fit.

Problems observed:

- Wake phrases such as `hi amadeus` were often transcribed as variants like `Hi, I'm As`.
- Lowering the match threshold helped wake, but increased false positives.
- Bridge mode could send low-confidence or malformed text to LLM before Qwen ASR was ready.
- This made the first post-wake interaction feel brittle.

Decision:

- SenseVoice can be used for wake/lightweight detection.
- Do not rely on SenseVoice bridge text as a normal user command unless confidence/intent is strong.
- Prefer loading/keeping Qwen ASR hot after wake, then use VAD-gated Qwen for actual conversation.

## ASR Lifecycle Decision

The desired lifecycle is:

```text
idle/wallpaper:
  lightweight wake detector

wake detected:
  load or reuse Qwen ASR
  enter awake window

after assistant finishes speaking:
  keep Qwen hot for a short window
  VAD gates user speech

if no user speech before idle timeout:
  return to wake detector
```

Important detail:

- The hot-window timer should start after assistant playback finishes.
- It should reset each turn.
- SenseVoice wake should resume only after Qwen's hot window expires, not immediately after one assistant response.

## Self-echo and Interruption Policy

Several options were considered:

- Voiceprint filtering.
- Text matching against Amadeus' own spoken output.
- Frequency filtering / ducking.
- Always-on ASR with post-filtering.
- WebRTC AEC with TTS PCM reference.

Decision:

- Do not implement a half-finished interruption path based on voiceprint/text matching.
- Prefer a standard AEC path when barge-in is needed.
- Until AEC is good enough, avoid making Qwen ASR listen through assistant playback.

Reasoning:

- Voiceprint may help filter Amadeus' own generated voice, but it is not enough for arbitrary speakers, room echo, or playback artifacts.
- Text matching still spends ASR compute on echo and can fail when ASR paraphrases/mistranscribes.
- Ducking reduces risk but does not solve interruption.
- WebRTC AEC is the cleaner long-term direction because it attacks the audio problem before ASR.

## History Annotation Decision

When user barge-in interrupts an assistant response, the chat transcript and
conversation history should stay aligned.

Current policy:

- The chatbox displays the assistant text known at interrupt time.
- The persisted/provider conversation history stores the same assistant text.
- Append an interruption marker.

Conceptual shape:

```text
assistant: <visible/generated assistant part> [interrupted by user]
```

This preserves the user-visible transcript and prevents a split-brain state
where the UI shows one conversation while the next provider call sees another.

### Prompt Cache / KV Cache Tradeoff

We considered storing only a compact marker in provider history, such as:

```text
assistant: [assistant response interrupted by user]
```

That would reduce token count and can be better for provider-side prefix/prompt
cache in some cases. However, it creates a mismatch between the chatbox,
session replay, and the actual context sent to the LLM.

Final decision:

- Prefer transcript correctness and semantic clarity.
- Keep history aligned with chatbox.
- Accept that modifying the most recent assistant history item can invalidate
  provider prefix cache from that item onward.
- The cache impact is local, not total: earlier system prompts and older history
  remain the same prefix and can still be reused by providers that support prefix
  caching.

If cache behavior becomes a measured bottleneck later, revisit this with a
separate "UI transcript vs provider memory" split. Do not mix that architectural
change into the current barge-in implementation.

### Realtime Chatbox Update Pitfall

Observed symptom:

- After barge-in, the session file/history was correctly annotated with
  `[interrupted by user]`.
- Reopening or reloading the Electron window showed the correct interrupted
  assistant text.
- The live chatbox did not update at the moment of interruption.

This looked like a frontend rendering/cache problem, but the root cause was in
the backend realtime event path.

Actual root cause:

- The interrupt handler updated and saved conversation history first.
- Then it tried to emit `chat.interrupted`.
- The emit path referenced `Method`/`bus` from a scope where they were not
  imported.
- Therefore persistence succeeded, but the live WebSocket event failed before
  the Electron chatbox could patch its in-memory message list.

Important diagnostic lesson:

```text
history correct after reload
live chatbox stale
  -> first check whether chat.interrupted was emitted and forwarded
  -> do not assume the React message patcher is wrong
```

`tools/watch_chat_events.py` is the preferred first check. A real successful
barge-in should show:

```text
[evt] tts.sentence_start ...
[evt] tts.status {"status":"barge_in_listening",...}
[evt] tts.status {"status":"barge_in_detected",...}
[evt] chat.interrupted ...
[evt] tts.status {"status":"interrupted"}
```

If `chat.interrupted` is absent, the live UI cannot update. Investigate the
backend event path first. If `chat.interrupted` is present but the UI stays
stale, then investigate Electron message patching.

Additional watcher pitfall:

- The watcher was waiting for `tts.status`, but the WebSocket bridge was not
  forwarding `TTS_STATUS`.
- That made the diagnostic output too sparse and hid whether barge-in was
  listening, suppressed, or detected.
- Forward `TTS_STATUS` and include `tts.sentence_start/end` in the watcher so
  the logs show whether the user spoke during playback or after
  `tts.turn_complete`.

Fixes applied:

- Import `Method` and `bus` in the backend bootstrap scope used by the
  interrupt annotation handler.
- Forward `TTS_STATUS` through `server.ws_handler`.
- Add barge-in debug statuses:
  `barge_in_listening`, `barge_in_suppressed`, `barge_in_detected`,
  `barge_in_tts_idle`, and `barge_in_error`.
- Keep the frontend chatbox patcher as the second layer of diagnosis, not the
  first, when the watcher lacks `chat.interrupted`.

## PyAudio / Buffering Notes

`player.stop()` should stop/abort/close the PyAudio stream and clear queued writer jobs.

However, playback tasks can still exist after the stop call. That is why epoch checks are required even if the stream is stopped. `stop()` is a device-level interruption; epoch is the logical ownership boundary.

Practical rule:

- Use `stop()` to flush/abort audible output.
- Use epoch checks to prevent stale tasks from restarting output or mutating state.

## Current Known Limitations

- AEC delay is still device-dependent.
- Barge-in behavior should be tested separately for headphones, laptop speakers, and external speakers.
- The current realtime AEC path is meant to validate timing and insertion points first; it is not yet a final polished acoustic echo cancellation product.
- If the output device changes, delay calibration may need to be revisited.
- Stale work must remain harmless even if Python tasks continue running briefly after interrupt.
- A CUDA `unspecified launch failure` should be treated as fatal for the current backend process. Restart the backend before judging follow-up behavior.
- The barge-in detector should not exit during tiny sentence gaps. It keeps a short TTS idle grace window and only applies cooldown after a real barge-in trigger, not on every sentence start.

## Unified MicInputService Direction

The older implementation opened separate microphone streams for wake,
barge-in, and Qwen ASR. That made the architecture brittle:

- Barge-in could detect speech on one stream, then Qwen ASR reopened the mic
  and missed the beginning of the same utterance.
- Each consumer applied AEC separately, so delay/profile changes were not a
  single source of truth.
- A temporary post-barge-in mic unblock window could let Qwen ASR capture
  Amadeus' own speaker tail on built-in/default microphones.

The preferred realtime architecture is now:

```text
single mic stream
  -> realtime AEC once
  -> processed PCM ring buffer
  -> lightweight VAD consumers
  -> Qwen ASR only after VAD extracts an utterance
```

Qwen ASR should remain hot/lazy-loaded but not continuously transcribe.
The always-on part is the cheap mic reader, AEC, ring buffer, and VAD.

First implementation pass:

- Added `asr.mic_input_service.MicInputService`.
- SenseVoice wake now consumes frames from this shared service instead of
  opening its own PyAudio stream. This avoids a wake -> Qwen device handoff
  where Windows can fail to reopen the same microphone and silently fall back
  to another input.
- Stopping wake for Qwen handoff keeps the shared mic stream alive; stopping
  wake manually or through wallpaper shutdown closes the shared mic stream.
- Microphone fallback preserves the two important device profiles: try the
  configured Bluetooth headset first, then Windows default input, then generic
  fallbacks. AEC delay is applied from the final opened device, so headset and
  internal-mic paths can use different delay settings.
- Barge-in detector now consumes frames from this shared service instead of
  opening a separate PyAudio stream.
- On barge-in speech start, the service marks a handoff sequence with short
  pre-roll.
- `ASRManager.listen_for_speech()` now captures an utterance from the same
  shared service and consumes the handoff pre-roll when present.
- Handoff captures have a shorter safety cap and energy-end fallback. This
  prevents a missed VAD end from delaying a barge-in command until the normal
  15s ASR listen timeout.
- The old post-barge-in forced mic unblock window was removed; Qwen ASR should
  still respect the TTS echo tail guard unless a handoff is actively being
  consumed.

Remaining work:

- Add richer diagnostics around handoff frame count, residual RMS, and ASR
  capture start/end.
- Decide when the shared mic service should be stopped in non-wallpaper
  desktop mode versus kept warm.

## Latest Stabilization Notes

The current successful behavior is:

- Multiple barge-ins can happen in one awake session.
- Each interrupted utterance is recognized by Qwen ASR and sent as the next
  user turn.
- Old assistant audio is stopped/invalidated instead of continuing playback.
- Wallpaper/render animation can continue independently while the chat pipeline
  changes turns.

The most important latency lesson was that "Qwen ASR is slow" can be a false
diagnosis. In the second barge-in regression, Qwen inference itself was only
hundreds of milliseconds. The delay came from capture length:

```text
BargeIn speech start
  -> handoff marked
  -> ASR capture consumed handoff frames
  -> 16s audio captured
  -> Qwen infer ~0.5s
```

Root cause:

- Handoff capture started with buffered speech frames and set
  `speech_started=True`.
- Silero VAD's internal iterator had not necessarily observed the matching
  start state.
- If VAD did not emit an `end`, capture waited until the normal ASR listen
  timeout.

Final rule:

- Handoff capture must not depend only on VAD end.
- Handoff capture needs an energy-end fallback and a short max duration cap.
- Log capture finish reason and duration, then judge latency from those logs
  before blaming the recognizer or provider.

Good diagnostic sequence:

```text
[BargeIn] speech start
[MicInput] handoff marked
[MicInput] ASR capture consumed handoff frames=...
[MicInput] ASR capture finish reason=energy_end|vad_end|handoff_max ...
[ASR:Qwen3ASR] in-process <audio_ms> audio -> <infer_ms>ms
[server] wake ASR text; auto-sending to chat
```

If `<audio_ms>` is large, the bug is capture/VAD/handoff. If `<infer_ms>` is
large, then investigate Qwen/model/GPU. If auto-send is late after inference,
then investigate chat/session/provider dispatch.

Microphone selection lesson:

- Keep the configured Bluetooth/headset index as first choice when it is
  available.
- If that index fails to open, try Windows default input before RMS-scored
  generic fallbacks.
- Apply AEC delay from the final opened device, not from the configured device.
- Do not hard-code the internal mic globally just because the headset is absent
  in one run; the headset and internal mic intentionally need different AEC
  profiles.

## Audio Echo Guard

Text-level echo filtering is not reliable for Amadeus:

- The assistant speaks Japanese.
- Qwen ASR may hear the same audio as Chinese, English, translation-like text,
  or malformed mixed text.
- String similarity can therefore miss true echo and can also reject real user
  speech for the wrong reason.

The guard should stay in the audio domain:

```text
recent TTS reference PCM
candidate raw mic PCM
candidate AEC residual PCM
  -> band-energy correlation search
  -> residual/reference sanity checks
  -> drop only high-confidence echo-like handoff candidates
```

Implementation notes:

- `RealtimeAECProcessor` keeps a short in-memory reference PCM history while it
  feeds WebRTC AEC.
- `MicInputService` stores both raw mic chunks and AEC residual chunks.
- Only barge-in handoff candidates are guarded; ordinary ASR capture is not
  filtered by this path.
- A candidate is dropped only when raw mic and residual are both highly
  correlated with the recent reference, and residual energy is still mostly
  explainable as echo.
- The guard runs before Qwen ASR, so echo-like fragments do not spend GPU time
  and do not reach provider history.

Debug logs to watch:

```text
[EchoGuard] drop=True|False reason=... raw_corr=... residual_corr=... residual_ratio=...
[MicInput] ASR handoff dropped by echo guard ...
```

If real user barge-in is dropped, loosen the thresholds or disable the guard
with `ASR_ECHO_GUARD_ENABLED=0`. If self-echo still leaks through, inspect
`raw_corr`, `residual_corr`, and `residual_ratio` before changing AEC delay.

## Personalized Wake Template Cache

SenseVoice is useful as a lightweight wake recognizer, but the English phrase
`hi amadeus` is not stable enough in practice. It may become `Hi, I'm As`,
`hi im...`, Chinese phonetic fragments, or short malformed text. Lowering the
text threshold too far increases false wakes, and using bare `hi` as a wake
phrase is too easy to trigger accidentally.

The current compromise is a local acoustic template cache:

```text
successful SenseVoice wake segment
  -> extract normalized log-mel feature sequence
  -> save under runtime/wake_templates/<mic_key>.npz

future VAD wake candidate
  -> compare against cached templates with lightweight DTW
  -> if high-confidence match, accept wake before SenseVoice
  -> otherwise fall back to SenseVoice text matching
```

Why this is useful:

- It is faster than ASR because it only runs small FFT/log-mel/DTW work on CPU.
- It is more personalized than a fixed wake phrase matcher because it learns the
  user's actual pronunciation and microphone path.
- It does not keep Qwen ASR hot, and it does not change the post-wake ASR/TTS
  conversation lifecycle.

Safety rules:

- Only high-confidence ASR wake successes are learned as positive templates.
- Template matches are not recursively learned by default, avoiding drift.
- The cache is per microphone key, because Bluetooth headset and internal mic
  have different acoustic paths and AEC delays.
- Short segments can fast-accept directly. Longer segments still allow
  SenseVoice to run first so inline commands like `hi amadeus, ...` are not
  swallowed by a template-only wake decision.
- Cache files live under `runtime/`, which is intentionally ignored by Git.

Useful knobs:

```text
WAKE_TEMPLATE_CACHE_ENABLED=1
WAKE_TEMPLATE_CACHE_THRESHOLD=0.68
WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD=0.80
WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE=8
WAKE_TEMPLATE_CACHE_MIN_MS=500
WAKE_TEMPLATE_CACHE_MAX_MS=2600
```

Debug logs to watch:

```text
[WakeTemplate] learned positive template device=mic_17 count=...
[WakeTemplate] duration=... templates=... matched=True|False score=... distance=...
```

If the first wake after startup still feels bad, that is expected until at least
one successful wake has been learned. If false wakes appear, raise
`WAKE_TEMPLATE_CACHE_THRESHOLD` or delete `runtime/wake_templates`.

## Test Checklist

Playback epoch regression:

- Start a long assistant response.
- Trigger barge-in while an already synthesized later sentence is queued.
- Confirm logs show interrupt and stale playback is dropped.
- Confirm no old sentence starts after `[TTS-INTERRUPT] playback queue cleared`.

AEC/barge-in:

- Test with headphones to verify user speech triggers interruption.
- Test with external speaker to check self-echo false positives.
- Capture `reference.f32` and `mic.f32` if realtime behavior is unclear.
- Adjust `AEC_REALTIME_DELAY_MS` only after checking device path.

ASR lifecycle:

- Wake from idle.
- Speak one command.
- Let assistant reply.
- Speak again immediately after playback.
- Confirm Qwen ASR is still hot and SenseVoice wake has not prematurely resumed.

## Implementation References

- `asr/barge_in_detector.py`
- `tts/aec_realtime.py`
- `tts/aec_debug_capture.py`
- `tts/playback.py`
- `tts/pipeline.py`
- `server/handlers/tts_handler.py`
- `server/app.py`
- `core/session_manager.py`
