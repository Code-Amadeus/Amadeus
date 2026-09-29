# Local Windows V3 streaming emotion experiment

This branch is experimental, not a proposed public default. The archived listening
experiments remain on `codex/windows-v3-emotion-lab`. This live fork starts with a
separate snapshot of the current local TTS/pronunciation state, so the emotion
commit can be reviewed independently of that baseline.

`ENABLE_EXPERIMENTAL_V3_EMOTION_ROUTING=0` is the default. Set it to `1` in this
checkout's local `.env` and restart the desktop app to opt in. Set it back to `0`
and restart to return to the normal path. The experiment only activates for the
embedded GPT-SoVITS backend, Windows CUDA (not ROCm/CPU), the current hc3 V3
weights (`kurisu_v3hc_ja-e13.ckpt`, `kurisu_v3hc_ja_e3_s3861_l32.pth`), and Japanese
speech. Remote backends and other OS/model profiles keep their previous behavior.

## One conditioning decision per queued text span

- The existing LLM parser is the source of EMO semantics. No classifier, second
  model call, generated reference path, or prose keyword routing is introduced.
- When enabled, ordered parser text/actions are consumed in order. An EMO change
  flushes preceding unfinished text before changing the turn-local emotion. The
  tag applies forward until the next EMO; a new turn starts normal. `dur` remains
  expression metadata, not the duration of a TTS text span.
- The existing `TTSRequest.emotion` reaches the utterance job. Different emotions
  cannot merge. First-sentence behavior, playback cover, inference permits,
  interruption epochs, and the existing adaptive scheduler remain in charge.
- Only the local experimental inferencer composes references. The selected
  reference provides prompt/phones/BERT; the original configured reference
  provides spectrum, style embedding, reference features, and mel as one group.
  Shared cache dictionaries are never modified. The conditioning scope is owned
  by the producer thread and reset on completion, failure or generator close.
- Routed first sentences bypass the old default-audio cache; normal/default
  references retain cache reuse. The inference cache still caches each reference.
- Existing semantic-collapse checks remain unchanged. There is no new silent
  seed retry, reference fallback, or automatic audio editing.

## Local reference catalog

WAV/OGG and same-name TXT pairs live in ignored `assets/audio/reference/emotions`:
angry → angry.wav (B03); sad → sad.wav (first-round crs_2773);
shy/blush → shy_b.ogg (third-round B, crs_1002);
surprised → surprised.ogg (second-round 3, crs_2679);
disappointed → disappointed_a.ogg (third-round A, crs_2408).
`disappointed_b` and `exasperated_candidate` remain saved alternatives, not extra
model labels. normal/thinking/serious_speaking/smile/happy retain default reference.

Missing reference pairs make opted-in initialization fail visibly. Unsupported
profiles do not activate the experiment. `[EmotionLab]` startup, queued-span,
synthesis and conditioning logs show the effective route without logging another
copy of the dialogue or credentials.

## Validation

Model-less tests cover disabled/non-Windows behavior, model/device gating,
request-local cache composition, close/reset, first-audio-cache bypass, emotion
merge boundaries and complete/split/intra-sentence tags through real Main Chat
queueing. Existing TTS threading/interruption and chat delivery checks are run.

Live verification must use normal authenticated `chat.send`, a real LLM response,
actual playback sentence events and backend conditioning logs. An accepted send
or `chat.complete` alone is not proof of speech playback completion. Keep the
synthetic prompt and test evidence local; never persist or print desktop tokens.

### Observed local trial (2026-09-29)

- 317 relevant model-less TTS/chat/history tests passed, including the 15 new
  routing cases. The local switch was then enabled and the desktop/backend pair
  restarted with authentication still required.
- A normal Session-bound `chat.send` produced a real model response containing
  normal, sad, disappointed, thinking, surprised and shy tags. No fixed response
  or seed retake was substituted for the model output.
- All 45 queued span IDs matched actual sentence-start and sentence-end events;
  chat completion and whole-turn playback completion were observed. There were
  27 synthesis jobs and 15 multi-span jobs; every recorded merge contained one
  emotion only.
- Conditioning logs showed sad.wav, disappointed_a.ogg, surprised.ogg and
  shy_b.ogg as semantic references, with kurisu_reference.wav as the acoustic
  reference. Normal and thinking used the default path. No synthesis error was
  observed for this test turn.
- The initial test client's unsupported source/session shape was rejected by
  the existing ingress boundary. It was corrected to the frontend's normal
  Session-bound request; no authentication or authority checks were weakened.

The local synthetic prompt, filtered events and verified counters are in
`.cache/emotion-live-integration/`. This is one functional live trial, not proof
of stable subjective prosody across arbitrary text or hardware.
