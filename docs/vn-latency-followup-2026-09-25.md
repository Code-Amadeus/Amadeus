# VN latency investigation and targeted fixes

## Recorded session

The latest inspected PARANORMASIGHT run contained 69 accepted lines and 68
completed immediate reactions: 53 silence, 6 hold and 9 speak. Immediate JSON
parsed on 67 of 68 calls; all 20 lookahead calls failed to parse.

For the nine spoken reactions, median runtime admission to reaction completion
was 3.646 seconds; median admission to first sound was 4.534 seconds. TTS enqueue
followed the reaction within 1–4 milliseconds. First audio generation took a
median 633 milliseconds. One utterance additionally waited 3.413 seconds for
previous speech to finish. Subtitle translation runs asynchronously.

These are admission-to-sound measurements. The source did not supply a capture
clock, and the recorded capture time defaults to admission time. They do not
measure upstream buffering or game-display-to-sound latency. No same-condition
wallpaper conversation benchmark was available.

## Streaming history and concurrency

The current immediate client waits for a complete JSON response (`stream=False`).
Both Agent and Luna receive loops await the entire `vn.line` handler before
forwarding another line. This serializes model processing at the adapter boundary.
In the recorded run, 63 next-line admissions followed the preceding reaction
within 100 milliseconds, consistent with buffered input being drained serially.

Experimental commit `5b4e501` contains genuine immediate-model streaming:
`AsyncOpenAI`, incremental complete speech-header parsing, and early delivery
through the runtime's speech guards. It is not an ancestor of the mainline
release. Mainline's nonstreaming client traces to the public source snapshot
`b1bb3cc`; the profile/prompt changes did not remove that experimental streaming
implementation. Current streamed Chinese-to-Japanese TTS translation is a
separate stage. Snapshot `43c9f6e` also retains `stream=False` for decisions.

Concurrency is possible, but directly scheduling concurrent `ingest_line` tasks
would allow out-of-order state changes and speech. Its short lock only protects
line admission; model completion is followed by shared cadence, evidence,
summary and playback updates. A future implementation needs ordered admission,
per-line context snapshots, bounded concurrent inference, and ordered commit.
Snapshots must state which earlier model results have already been committed.
Early speech must retain session/pause/frequency guards. The experimental branch
also has stale-response dropping and visual behavior, so it is not a wholesale
compatibility patch. This change does not introduce those policies or restore
streaming/concurrent immediate inference.

## Lookahead truncation: controlled reproduction

Reconstructed the first recorded planner context from the local script, checking
all 50 future script IDs and text hashes against the recorded redacted window.
Kept the existing English prompt and temperature, using the configured provider.
Only the output budget changed between these calls:

| Output ceiling | Returned model | Completion tokens | Finish reason | JSON |
| --- | --- | ---: | --- | --- |
| 650 | deepseek-flash | 650 | length | Invalid, truncated |
| 2200 | deepseek-flash | 757 | stop | Valid, 6 plan entries |

The original 20 calls did not record finish reasons, so this does not establish
the individual cause of every historical failure. It directly reproduces the
truncation defect in that call shape. The background planner now has a 2200-token
ceiling; this is a maximum, not a required response length. All VN calls log the
lane, resolved model, finish reason and completion-token count without logging
credentials or message contents. Truncation is a warning. Prompts, parser behavior
and retry policy remain unchanged. This fixes planner reliability; it is not a
measured improvement to immediate speech latency.

## Native overlay authentication

Bootstrap intentionally reads and removes desktop authentication variables from
the backend environment. The later-launched native overlay previously expected
to inherit them, causing repeated unauthenticated connections.

Bootstrap now passes its retained policy through the launch handler to the
manager. Only the repository-owned overlay receives it in a child-specific
environment. Ordinary game children receive an environment with authentication
variables removed. Credentials do not enter arguments, saved profiles, status or
global environment. The backend continues to reject unauthenticated clients.

Verification includes an actual child process running the production native
control client after parent credentials have been cleared. It authenticates,
reads the session and submits a voice-toggle request to a loopback test server.
Separate tests reject credential forwarding to an external helper and verify
game-process credential isolation. ASR hardware is not part of this test.
Existing backend/overlay processes need to restart to use the fix.

## Validation

- 205 relevant Python tests passed: all `test_vn_*.py`, narration/control/vision
  boundaries, and local-instance authentication contracts.
- `ruff check .`, both architecture checks and `git diff --check` passed.
- Electron production build passed (existing large-chunk warning).
- `tools/smoke_electron_model_less.py` passed: desktop-owned backend startup,
  authenticated control, renderer navigation and clean backend shutdown.
- Controlled model replay results above; private script text and raw session
  logs remain local and are not included in this change.
