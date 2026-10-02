# Wake word setup

Amadeus separates the lightweight always-on Wake recognizer from the full
Conversation recognizer. A typical local setup keeps Wake on SenseVoice while
Conversation uses Qwen3-ASR. They can run together and share one microphone;
changing one backend does not silently change the other.

## Configure it in Electron

1. Open **Settings > Voice > Voice backends > Wake recognition**.
2. Enable **Wake service**.
3. Enter comma-separated **Wake phrases**. Keep spelling variants only when
   the recognizer actually produces them.
4. Select `sense_voice` or `qwen3_asr` as the Wake backend. SenseVoice is the
   recommended lightweight default.
5. For SenseVoice, set the wake language passes and an explicit model path if
   the model is not available from the normal local cache.
6. Choose the microphone in the **Conversation recognition** card. That device
   is shared by Conversation and Wake recognition.
7. Choose whether a recognized command should be sent to Chat, then click
   **Restart backend to apply**.

Electron starts the wallpaper surface during normal desktop startup. With
`WAKE_ENABLED=true` and the default
`WAKE_AUTO_START_WITH_WALLPAPER=true`, that wallpaper start also begins wake
listening. A headless host does not have that lifecycle trigger; it must send
the backend method `wake.start` explicitly and may inspect `wake.status`.

All GUI Wake fields are startup settings. Electron persists them in its local
user-data settings store rather than rewriting the repository `.env`. A value
provided by the parent process or `.env` remains authoritative, appears locked
in Settings, and still requires a backend restart after it is changed.

## Basic `.env` profile

Use `.env` when preparing a reproducible developer machine or headless host:

```env
WAKE_ENABLED=true
WAKE_ASR_BACKEND=sense_voice
WAKE_PHRASES=Hey Amadeus,Hi Amadeus
WAKE_SENSEVOICE_LANGUAGES=en
WAKE_AUTO_SEND_TO_CHAT=true
SENSEVOICE_LANGUAGE=en
SENSEVOICE_MODEL_PATH=C:\path\to\SenseVoiceSmall
MICROPHONE_DEVICE_INDEX=-1
```

`MICROPHONE_DEVICE_INDEX=-1` selects automatically. A partial device-name
fallback can be set with `MICROPHONE_PREFERRED_NAME`; the Voice page lists the
indices detected on the current machine.

## Matching and capture tuning

Normal installations should keep the defaults from `.env.example`. Change one
value at a time and restart the backend before judging it.

| Setting | Default | Effect |
| --- | ---: | --- |
| `WAKE_MATCH_THRESHOLD` | `0.10` | Minimum normalized text similarity. Higher is stricter. Exact phrase containment still matches directly. |
| `WAKE_VAD_THRESHOLD` | `0.45` | Silero speech probability required to open a wake segment. Higher is stricter. |
| `WAKE_MIN_SEGMENT_RMS` | `0.003` | Rejects completed segments quieter than this RMS value. |
| `WAKE_AWAKE_SECONDS` | `60` | Minimum time the full Conversation recognizer stays hot after a wake; see [Returning to standby](#returning-to-standby). |
| `WAKE_BRIDGE_MAX_SECONDS` | `45` | Maximum post-wake SenseVoice bridge window. |
| `WAKE_BRIDGE_AUTO_SEND` | `false` | Allows bridge text to be submitted automatically; keep disabled unless that behavior is intended. |
| `WAKE_AUTO_START_WITH_WALLPAPER` | `true` | Starts/stops Wake with the Electron wallpaper lifecycle. |

The energy detector is a disabled fallback, not a second default VAD path.
Enable `WAKE_ENERGY_FALLBACK` only when the normal VAD path is unavailable or
has been deliberately ruled out. `WAKE_ENERGY_START_RMS` opens its segment and
`WAKE_ENERGY_END_RMS` closes it.

## Returning to standby

After a wake, the full Conversation recognizer stays hot for the larger of
`WAKE_AWAKE_SECONDS` and `ASR_IDLE_UNLOAD_SECONDS` (180 seconds by default),
counted from the wake or from the end of the last reply. When that window
passes, the conversation returns to Wake standby; on the Windows wallpaper this
includes continuous conversation. An in-process Qwen3-ASR model on CUDA then
waits in RAM instead of VRAM. The next wake, or the start of speech, moves it
back while the user is still speaking. Other Conversation recognizers unload at
that point.

The model stays in VRAM throughout the hot window, including a window renewed
after a long reply. Its last transcription time does not shorten that window.

Listening that does not return to standby (continuous conversation with Wake
disabled, or a manual microphone session) keeps running. An idle Qwen3-ASR
model still moves to RAM after `ASR_IDLE_UNLOAD_SECONDS` and comes back at
speech start. A continuous session also keeps any remaining hot window after
its last reply, even when Wake is disabled.

If Wake is configured to share Qwen3-ASR, stopping or unloading Conversation
does not release Wake's model. Its residency is shared until the last user
releases it; a Wake transcription restores a model waiting in RAM to its device.

| Setting | Default | Effect |
| --- | ---: | --- |
| `ASR_IDLE_UNLOAD_SECONDS` | `180` | Seconds the Conversation recognizer stays in VRAM after the last conversation activity. |
| `ASR_RAM_UNLOAD_SECONDS` | `0` | Once the model waits in RAM and listening has stopped, seconds before it unloads; `0` keeps it loaded. |

## Personalized template cache

After a high-confidence text wake, Amadeus can learn a small local acoustic
template for the current microphone. Templates live under
`runtime/wake_templates`, which is ignored by Git and never sent to a model or
remote service.

Relevant settings are:

```env
WAKE_TEMPLATE_CACHE_ENABLED=true
WAKE_TEMPLATE_CACHE_THRESHOLD=0.68
WAKE_TEMPLATE_CACHE_LEARN_THRESHOLD=0.80
WAKE_TEMPLATE_CACHE_MAX_PER_DEVICE=8
WAKE_TEMPLATE_CACHE_MIN_MS=500
WAKE_TEMPLATE_CACHE_MAX_MS=2600
```

Raise `WAKE_TEMPLATE_CACHE_THRESHOLD` if learned templates cause false wakes.
Delete `runtime/wake_templates` to reset learned templates; the next successful
text wake will begin learning again.

## Troubleshooting

If Wake never starts:

- confirm `WAKE_ENABLED=true`, restart the backend, and check that Electron
  successfully started the wallpaper;
- confirm the selected Wake backend reports available in Settings;
- verify the SenseVoice model path or local cache;
- verify the shared microphone selected under Conversation recognition.

If the phrase is heard but does not match:

- inspect the recognized text and language in `[Wake] candidate` logs;
- add only the spelling or language variant actually produced;
- lower `WAKE_MATCH_THRESHOLD` cautiously if fuzzy matching is too strict.

If unrelated speech wakes Amadeus:

- remove overly broad phrase variants;
- raise `WAKE_MATCH_THRESHOLD`, `WAKE_VAD_THRESHOLD`, or
  `WAKE_MIN_SEGMENT_RMS` one at a time;
- raise `WAKE_TEMPLATE_CACHE_THRESHOLD` or reset the template cache.

Set `WAKE_DEBUG_AUDIO=true` to log one input RMS sample per second while
diagnosing capture levels. This flag increases log volume but does not write
audio recordings.
