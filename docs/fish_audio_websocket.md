# Fish Audio WebSocket TTS

Select `fish_audio` in Voice settings, enter the API key in the secret field,
and restart the backend. Alternatively, configure the ignored local `.env`:

```dotenv
TTS_BACKEND=fish_audio
FISH_TTS_API_KEY=<your-api-key>
FISH_TTS_WS_URL=wss://api.fish.audio/v1/tts/live
FISH_TTS_MODEL=s2.1-pro-free
FISH_TTS_REFERENCE_ID=b450b19370434173b121446057622e9b
FISH_TTS_LATENCY=balanced
```

The voice reference is the public Japanese **牧濑红莉栖 / Makise kurisu**
[voice](https://fish.audio/zh-CN/app/text-to-speech/?modelId=b450b19370434173b121446057622e9b).
`FISH_TTS_MODEL` selects the inference engine sent in the `model` header;
it does not take a voice ID or an API key's display name. The tested and
recommended code default is `s2.1-pro-free`; `s2.1-pro` can be selected explicitly.
`FISH_TTS_LATENCY` accepts `normal`, `balanced`, or `low`.
The shared `TTS_API_TIMEOUT_SECONDS` limits connection, send, and receive waits.

Install the existing voice profile (`uv sync --locked --extra voice`) to include
MessagePack. Fish uses no local model weights or Torch. Voice settings save the
key through the existing encrypted desktop store; it is not returned to the renderer.

## Streaming boundary

The [official protocol](https://docs.fish.audio/api-reference/endpoint/websocket/tts-live)
uses binary MessagePack messages: `start`, successive `text`, optional `flush`,
then `stop`. Audio messages arrive concurrently. A `finish` with reason `stop`
is required for success. Partial audio followed by an error or disconnect is
reported as failure, with no automatic retry or HTTP fallback.

The adapter requests mono PCM16 at 44.1 kHz and converts it to the existing
float32 audio contract. Audio is yielded as it arrives; an incomplete PCM sample
is carried into the next message. Closing or cancelling a stream closes its
socket and cancels its input sender. Output is limited to 64 MiB per request,
consistent with the existing remote backends.

The first-sentence disk cache fingerprints local GPT-SoVITS weights/reference
files, so remote backends bypass both reads and writes. Its revision is bumped
once because older entries cannot prove whether local or remote speech populated
them. This avoids replaying the wrong voice and keeps remote timing measurements
from silently becoming cache-hit measurements.

**Chat and VN still schedule complete sentences/utterances.** Their existing
worker uses `synthesize_stream(request)` and sends that locally committed text
as one `text` chunk followed by `flush` and `stop` on its own WebSocket session.
It receives streaming audio; this PR
does not bypass translation, sentence scheduling, subtitles, or epoch cancellation
to send raw LLM token deltas. Native first-packet playback follows the existing
first-sentence streaming policy; other utterances may be buffered by the scheduler.

For actual incremental input, `FishAudioTTSBackend.synthesize_text_stream(request,
text_chunks)` accepts an **async iterable** and sends each chunk without collecting
the input first. Only that iterable supplies text; `request.text` is subtitle
metadata and is not sent a second time. Use `contextlib.aclosing` if consumption
may stop early. `flush_each_chunk=True` forces Fish to synthesize each nonempty
chunk immediately. It is opt-in: flushing every token can fragment speech;
provide meaningful phrase chunks when using it. The existing sentence worker
always flushes its chunk because the local scheduler already decided its boundary.

## Reproduce the duplex experiment

These commands each make one real API request using your configured account.
They use two short Japanese phrases, with a three-second pause between inputs:

```console
python -m tools.probes.probe_fish_audio --model s2.1-pro-free --gap 3 --flush-each-chunk --output tmp/fish-incremental.wav
python -m tools.probes.probe_fish_audio --model s2.1-pro-free --gap 3 --output tmp/fish-no-flush.wav
```

Use repeated `--chunk "text"` arguments to supply your own chunks. The probe saves
a WAV and prints JSON containing input availability times, first-audio time,
audio duration, chunk count, and whether audio arrived before input completion.
The timing starts before connecting. Input timestamps mark when the source
offers a chunk to the sender, not network delivery acknowledgements. A failed
probe exits nonzero and its WAV may be empty or partial.

`--sentence-sessions` exercises the real synchronous runtime adapter, one
WebSocket session per supplied local sentence/phrase, with flushing enabled.
In this mode `--gap` is a delay after the preceding synthesis finishes; it is a
serial adapter probe, not the concurrent Chat scheduler. In the default duplex
mode it is the delay between source chunks in a single session.
`estimated_immediate_playback_first_sound_seconds` adds the leading waveform
offset to immediate playback of arriving chunks. The signal threshold is
10 ms RMS at -40 dBFS. This estimate excludes device latency; it is not a
microphone measurement. Estimated underrun assumes immediate unbuffered playback.

Observed on 2026-09-17 from one Windows host, `s2.1-pro-free`, `balanced`, using
the reference above (one request per mode; these are smoke results, not a latency
guarantee or a speech-quality assessment):

| Mode | First text available | Second text available | First audio | Audio before input finished | Audio duration |
| --- | ---: | ---: | ---: | --- | ---: |
| Flush each phrase | 0.793 s | 3.799 s | 1.246 s | Yes | 4.923 s |
| No explicit flush | 0.546 s | 3.560 s | 4.010 s | No | 6.316 s |

The flush run returned eight audio chunks, with the first about 454 ms after
the first text became available. In the no-flush run these short phrases remained
buffered until input ended. The same duplex transport supports both modes.

Deterministic tests in `tests/test_tts_fish_audio_backend.py` use a local WebSocket
peer and no account credentials. They verify audio before future input is
available, wire events/headers, PCM split boundaries, optional flushing,
synchronous worker compatibility, errors, timeout, and cleanup.

## Longer audio and user-request-to-sound measurements

Three trials of the same Japanese passage in each mode produced these medians:

| Mode | Estimated first sound from TTS invocation | First-trial audio duration |
| --- | ---: | ---: |
| Whole passage, one session | 1.204 s | 10.495 s |
| Three phrase chunks, one session, 200 ms input gaps and flush | 1.093 s | 9.799 s |
| Three local phrase requests via the synchronous adapter | 1.180 s | 10.124 s |

Qwen3-ASR-0.6B locally transcribed the first trial of each mode. All retained the
three clauses with no additional missing/repeated clause in the chunked versions.
All three transcriptions rendered the technical term `量子もつれ` inaccurately;
this check cannot distinguish an ASR spelling error from a TTS pronunciation
issue. No clipped samples were detected (peaks 0.575, 0.567, 0.664 respectively).
Pauses changed across modes; there is no subjective naturalness/timbre score.
Keep the generated WAVs for direct listening rather than treating ASR as a MOS test.

A separate isolated real backend used `chat.send` over its normal local WebSocket,
DeepSeek `deepseek-v4-flash`, cooperative Chat, Japanese speech, Chinese subtitle
translation, `FIRST_SENTENCE_EARLY_CUT_CHARS=11`, and the existing
`ENABLE_CUDA_GRAPH=1` serial scheduling setting. Input:
`红莉栖，晚上好。请用一两句日语说说你今天在实验室做了什么。`
Each trial started in a new Session, waited for `tts.turn_complete`, and invoked
Fish for its first sentence (no local audio-cache hit):

| Trial | First local sentence | Fish request start | Fish first audio | First device write | First non-silent device write |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 1.910 s | 2.437 s | 3.526 s | 3.580 s | 3.697 s |
| 2 | 1.170 s | 1.174 s | 3.244 s | 3.288 s | 3.508 s |
| 3 | 1.509 s | 1.513 s | 3.528 s | 3.572 s | 3.743 s |

The end-to-end first non-silent write median is **3.697 s**, measured with
`time.perf_counter` immediately before sending `chat.send` through the actual
pipeline to the PortAudio writer. It includes chat generation, scheduling,
Fish synthesis and playback preparation, with normal subtitle translation
active. The device reports another 91 ms output latency, giving an approximate
3.79 s audible onset. Electron click dispatch, microphone transcription, app
startup, and acoustic loopback are not measured. These are three host/network
observations, not latency guarantees. An earlier cold exploratory request took
5.70 s; the controlled three-trial table is not a claim about cold-start latency.

Raw sanitized timing and ASR evidence:
[fish_audio_benchmark_2026-09-17.json](fish_audio_benchmark_2026-09-17.json).
