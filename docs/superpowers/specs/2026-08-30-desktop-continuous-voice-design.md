# Desktop Continuous Voice Design

Date: 2026-08-30

Status: approved for implementation planning

Baseline archive: `archive/pre-desktop-voice-20260830` at
`6d0bebf62a4d8c3c21eaac24d737a07581de3de1`

## Goal

Allow the user to minimize the Electron Chat window and conduct a basic,
continuous voice conversation through the Lively Wallpaper surface. A wake
phrase opens a 60-second hot window, recognized speech is submitted to the
current Chat session, the desktop shows user and assistant subtitles, and each
completed assistant playback opens the next 60-second listening window.

## Scope

This change provides one focused conversation loop:

```text
wake -> listen -> recognize -> send to current Chat session
     -> think -> speak with subtitles -> listen again
     -> timeout or explicit stop -> passive wake
```

It reuses the repository's existing Wake, Qwen3-ASR, Chat, TTS, AEC/barge-in,
Wallpaper bridge, and Lively host. It does not introduce a second voice state
machine, a second microphone owner, or a second Chat frontend.

## Non-goals

- No complete Chat transcript embedded in the desktop wallpaper.
- No system tray lifecycle or automatic Electron window minimization.
- No permanent always-listening conversation mode.
- No new ASR, Wake, TTS, or realtime transport dependency.
- No merge of unrelated upstream model or provider changes.
- No change to the authority boundary: Wallpaper remains a presentation
  surface and cannot submit arbitrary model input.

## Existing Official Implementation to Reuse

The official repository already implements the difficult audio lifecycle:

- Wake recognition is separate from conversation recognition; SenseVoice is
  the lightweight Wake baseline and Qwen3-ASR is the conversation baseline.
  They share the configured microphone.
- `WAKE_AWAKE_SECONDS` defaults to 60 seconds.
- A Wake ASR session uses `finish_after_turn_complete=False`, so it remains
  available across multiple turns.
- The hot-window deadline is reset after assistant turn completion rather than
  when user speech begins.
- TTS playback pauses normal ASR capture, while the existing AEC/barge-in path
  can interrupt playback and return control to ASR.
- `WallpaperHandler` already forwards `ASR_STATUS` to
  `WallpaperEngineBridgeHost.set_asr_status`, and the bridge replays the latest
  `asrStatus` event after a page reconnect.

Primary references:

- [Wake word setup](https://github.com/Code-Amadeus/Amadeus/blob/main/docs/wake_word_setup.md)
- [ASR handler](https://github.com/Code-Amadeus/Amadeus/blob/main/server/handlers/asr_handler.py)
- [Barge-in and AEC notes](https://github.com/Code-Amadeus/Amadeus/blob/main/docs/barge_in_aec_interrupt_notes.md)
- [Wallpaper handler](https://github.com/Code-Amadeus/Amadeus/blob/main/server/handlers/wallpaper_handler.py)

The implementation must extend these paths rather than replace them.

## User-visible Behavior

### Entry and window behavior

1. Wallpaper mode starts the existing Wake service.
2. The user may minimize the Electron main window manually.
3. A configured Wake phrase opens the existing Qwen ASR hot window.
4. Wake and subsequent voice turns must not restore, focus, or otherwise bring
   the Electron window to the foreground.

### Desktop presentation

The Wallpaper keeps the current laboratory background and character. It adds
two lightweight presentation regions:

- A top-right status marker for `ready`, `awake`, `listening`, `thinking`,
  `speaking`, and transient errors.
- A bottom subtitle frame for the current turn only.

The visible states are:

| Phase | Status marker | Subtitle |
| --- | --- | --- |
| Passive wake | `READY` | Hidden |
| Wake accepted | `已唤醒` | Hidden |
| Listening | `正在听 · Ns` | Optional listening hint |
| Recognized | `已识别` | `你：<recognized text>` |
| Chat pending | `思考中` | Preserve the user subtitle |
| TTS playback | `正在说话` | `助手：<playback-synchronized sentence>` |
| Interrupted | `正在听 · 60s` | Mark the old assistant line interrupted, then accept the new turn |
| Timeout/stop | `READY` | Fade out and hide |
| Recoverable error | Concise error text | Preserve useful final text when available |

Only the listening phase displays a countdown. The countdown pauses while Chat
or TTS owns the turn. It restarts from 60 seconds when physical assistant
playback completes.

Assistant subtitles remain driven by the existing TTS playback timeline.
Recognized user text is carried in the ASR presentation payload. The Wallpaper
must not display speculative or non-final ASR text as a submitted user turn.

### Work panel coexistence

The green Electron work slice must not remain visible merely to show
`Waiting for work signal`. It is visible only when there is an active WorkItem,
an attention/permission request, or a work result that the existing product
contract requires to remain visible. Ordinary voice conversation uses only the
Wallpaper status marker and subtitle frame.

## Host Data Flow

No new desktop voice protocol is required. The implementation extends the
existing `asr.status` presentation payload with the minimum fields needed by
the Wallpaper:

- `status`: the existing lifecycle status.
- `source`: `wake` for the continuous desktop conversation path.
- `text`: final recognized user text only when relevant.
- `awake_remaining`: remaining listening seconds when available.
- `awake_deadline_ms`: a wall-clock deadline used by the page for a smooth
  local countdown between Host events.
- `reason`: timeout, stop phrase, playback completion, or error reason when
  relevant.

`AsrHandler` remains the owner of `_awake_until` and the actual microphone
lifecycle. The Wallpaper countdown is presentation only; reaching zero in the
browser does not stop ASR or submit a request.

The normal turn path remains:

1. `AsrHandler` emits final `ASR_RECOGNIZED` once.
2. The Host checks whether the normalized text is an exact stop command.
3. A normal utterance follows the existing wake-to-Chat path and current
   Session selection.
4. Chat and TTS continue publishing their existing events.
5. TTS physical playback completion notifies `AsrHandler`, which resets the
   hot-window deadline and emits the next listening snapshot.

This preserves the invariant that each final recognized utterance is submitted
at most once and recorded in the same current Chat Session that the Electron UI
later displays.

## Exit Commands

The normalized final ASR text is an exit command only when it exactly equals
one of:

- `停止对话`
- `结束对话`
- `退出对话`

Normalization removes surrounding whitespace and terminal punctuation and
uses case-insensitive comparison for any future Latin aliases. It does not use
substring matching. For example, `如何停止对话` is a normal Chat message.

When an exit command is recognized, the Host:

1. Stops the active Wake-sourced ASR hot window.
2. Clears the desktop conversation subtitle through the existing presentation
   path.
3. Returns to passive Wake listening.
4. Does not call `chat_h.send_text`.
5. Does not append the command to Chat history.

## Barge-in

The existing AEC/barge-in implementation remains authoritative. When user
speech is detected during assistant playback:

1. Abort the current Chat/TTS turn using the existing interrupt path.
2. Stop stale playback through existing epoch checks.
3. Mark the visible assistant line as interrupted.
4. Return the existing Wake-sourced ASR session to listening.
5. Preserve the repository's existing interrupted-turn history contract.

No continuous Qwen transcription is added during TTS playback.

## Failure Handling

- ASR initialization or microphone failure emits a concise unavailable state,
  submits no empty message, and returns to passive Wake when the existing
  lifecycle allows it.
- A Chat error emits a connection-failed presentation and releases the current
  turn so the hot window can listen again.
- If Chat produced final text but TTS fails, the final text may remain briefly
  visible, but the turn must leave `speaking` and return to listening.
- A Wallpaper/Lively disconnect does not stop backend voice processing. The
  bridge replays the latest ASR presentation state after reconnect.
- Stopping Wallpaper continues to use the existing
  `WAKE_AUTO_START_WITH_WALLPAPER` lifecycle and stops Wake when configured.

## Testing Strategy

Implementation follows test-driven development. Tests must demonstrate:

1. A normal final Wake utterance is submitted exactly once.
2. Each exact exit command stops the Wake-sourced ASR session and is not
   submitted or persisted.
3. A longer sentence containing exit words remains a normal Chat turn.
4. Physical TTS turn completion resets the hot-window deadline to 60 seconds.
5. Hot-window timeout returns to passive Wake.
6. Barge-in interrupts old playback and returns the conversation to listening.
7. The Wallpaper bridge replays the latest ASR state, final user subtitle, and
   deadline after reconnect.
8. The Wallpaper renderer maps Host states to the expected marker and subtitle
   content without acquiring authority over the timer.
9. The Electron work slice is hidden with no active WorkItem/attention/result
   and remains visible for genuine work or permission state.

The real-device acceptance pass on the configured RTX 5060 machine is:

```text
start Amadeus and Wallpaper
-> minimize Electron
-> speak wake phrase
-> ask question 1 and hear/see answer
-> ask question 2 within 60 seconds
-> interrupt answer 2 and ask question 3
-> hear/see answer 3
-> say "停止对话"
-> observe READY/passive wake
-> restore Electron and verify Chat history
```

The history must contain the real user/assistant turns and must not contain the
exit command.

## Rollback

The exact pre-feature tracked baseline is preserved by the annotated Git tag
`archive/pre-desktop-voice-20260830`. A rollback must restore that commit using
a non-destructive branch or checkout workflow; it must not use
`git reset --hard` against the user's working tree.
