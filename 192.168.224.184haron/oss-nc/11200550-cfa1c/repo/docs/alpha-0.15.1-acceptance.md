# 0.15.1 Alpha acceptance record

Dates: 2026-09-26–27. This record distinguishes source-package checks, real-model
observations, and the deferred human microphone/listening check.
The release PR's head SHA and CI checks identify the final candidate.

## Findings and repairs

1. The original source archive omitted top-level desktop setup scripts and
   several user configuration guides. Repository CI could still succeed.
   The allowlist now includes these inputs; the release workflow installs,
   builds, packages and starts an application from its own extracted ZIP.
2. A real Chat request to create a Pi Draft exposed a Host intake failure:
   the accepted short Work title and the directory derived from the full task
   had different slugs. C1 correctly refused the inconsistent workspace.
   The coordinator now uses the accepted title for both allocation and the
   Work record. Authority checks remain unchanged. Regression tests cover
   Japanese, English and mixed-language titles, execution and replay across
   none/read/write workspace access. The contextual news request and its
   confirmation also cover a Chinese task with an AI-bearing display title:
   all six combinations reproduce the same workspace conflict on the old
   implementation and pass with the repair. Provider access requirements and
   write intent remain unchanged, and its cwd matches the Work workspace.
   The new English case also failed before the fix; all 105 related tests passed
   after the expanded coverage (`test_cooperative_planned_work.py`,
   `test_work_effect_executor.py`, `test_work_ledger_coordinator.py`).
3. Live timing isolated approximately 490ms of avoidable waiting: a cached Chat
   opening waited for an earlier Work utterance's CUDA Graph inference lock.
   Cache lookup now precedes inference-lock acquisition. A miss still acquires
   the same lock and rechecks the turn epoch; a hit cannot release another
   producer's lock. Four new cache/lock cases failed before the repair.
   Afterward 76 related TTS/cache/threading/VN tests passed, including stale
   cache hits, stale waiters, cancellation and both playback modes.

## Automated and archive checks

The earlier packaging preflight at `45fe60e` passed:

- 13 release-tool tests and the source/provenance gates.
- Two deterministic ZIP builds with identical SHA-256; all 3,662 extracted
  file hashes matched their manifest. Later release documents increase the
  archive contents; use the final CI artifact's manifest and checksum.
- Fresh locked Python core/development installation and environment verification.
- Fresh Electron/Pi installation; 155 Electron tests, 35 Pi tests, TypeScript/
  Vite build, and both npm audits (zero reported vulnerabilities).
- Windows packaging, packaged wallpaper/tray smoke, 13 .NET lifecycle contracts
  and all 11 packaged model-less startup/authentication/navigation/shutdown checks.

These are earlier preflight results, not substitutes for final candidate CI.
The full Python and platform checks run on the preparation PR.

## Upgrade compatibility

The opt-in `electron/scripts/smoke-settings-upgrade.cjs` loads the actual
`DesktopSettingsStore` from `v0.15.0-alpha.0`, creates an isolated profile and
encrypts a test credential with Electron's native safeStorage. The current build
preserved its model/provider/voice settings, decrypted the same test credential,
kept the legacy dotenv file unchanged, and retained newly saved independent
provider roles and startup preference after reopening the store. No personal
profile or real credential was used by this check.

The old release configured `COOPERATIVE_CHAT_PROVIDER` through dotenv, not the
desktop store. A separate current-settings import confirmed that this legacy
alias still selects OpenClaw when `WORK_EXECUTION_PROVIDER` is absent.

Reproduction from a Git checkout with the previous tag available:

```text
cd electron
npm ci
npm run build
node -e "require('node:child_process').execFileSync(require('electron'), ['scripts/smoke-settings-upgrade.cjs', 'v0.15.0-alpha.0'], {stdio: 'inherit', windowsHide: true, timeout: 45000})"
```

## Real Pi and Windows wallpaper

The configured DeepSeek model and pinned Pi CLI completed the existing
`scripts/pi_daily_acceptance.py` task in 22.24 seconds: fetch the original arXiv
abstract, write the requested UTF-8 note, read it back, request a browser open
and launch Notepad. The trace contains a rejected shell permission followed by
successful file-tool recovery. The output file was inspected. Browser page
rendering and Notepad window visibility were not independently verified in this
run; launch acknowledgements are not presented as visible-window evidence.

A separate real-model Pi run invoked a harmless echo command, reached the
native permission boundary, and was cancelled before approval. The adapter
returned a confirmed cancellation and a cancelled result. An initial test prompt
only elicited a prose approval question; that attempt was not counted as a
cancellation pass.

`tools/probes/windows_wallpaper_lifecycle.py` passed four actual Lively
experiments: normal exit, parent stdin loss, helper termination with recovery,
and actual parent-process death. Live renderers connected, and the original
wallpaper, layout options, host-running state and recovery state were restored.
This is lifecycle evidence, not microphone or listening evidence.

## VN device playback

The production VN runtime, shared bridge, configured DeepSeek model and local
GPT-SoVITS v3 voice ran on Windows, Python 3.12.10, Torch 2.6.0+cu124 and
an NVIDIA RTX 4070 Ti SUPER. One warmup preceded these four short-question
samples; each produced nonzero audio delivered to the configured output device:

| Delivery | First-device playback marker | PCM peak |
| --- | ---: | ---: |
| Whole speech body | 1.934 s | 0.408 |
| First segment | 1.834 s | 0.525 |
| First segment | 1.925 s | 0.418 |
| Whole speech body | 2.399 s | 0.426 |

Command: `python tools/probes/measure_vn_audio_latency.py --live-model-and-audio --baseline-delivery whole-speak --output build/release/vn-audio`.
This uses real inference and device playback, not a new game playthrough or
acoustic loopback. It does not establish a general latency percentile.
Pause/restart, stale-tail cancellation and text-source behavior remain covered
by their deterministic contract suites and existing scoped game records.

## Chat and Work device checks

The initial live run verified nonzero Chat playback and found the Draft
allocation defect above. Its warm sample was 1.547 s, which exceeds the
historical 1.5-second target; it must not be reported as meeting that target.
Another pre-repair warm sample was 1.953 s. The investigation did not hide
these observations or replace them with model-less measurements.

A post-repair production run completed the actual Pi Draft, verified
`release-check.txt` contained only the requested marker, and resumed Chat.
With the default short-opening audio cache enabled, the successful first
device-write samples were:

| Scenario | Time |
| --- | ---: |
| Cold Chat | 3.329 s |
| Warm ordinary Chat | 1.266 s |
| Work request opening | 1.140 s |
| Chat following completed Work | 1.046 s |
| Ordinary Chat after an idle interval | 0.985 s |

The warm samples meet the historical 1.5-second sample target. Cold startup is
separate. These are individual cached-opening observations, not a percentile
guarantee or proof that uncached responses always meet the same target.

Transport instrumentation before the cache-lock repair separated a warm
1.203-second sample into 281ms before the API call, 844ms from the API call to
first content token, and 78ms from that token to device write. Another warm
sample was 109 + 906 + 79 = 1094ms. API waiting remains substantial and includes
network, remote queuing and inference; no alternate-network A/B was performed.
The local player also spent about 203–219ms closing old audio in two samples.
During the same investigation, 50 resource samples showed CPU median 17.5%
with a transient 100% peak and GPU median 24% / peak 75%. This neither proves
other concurrent sessions caused the earlier slow samples nor excludes brief
resource contention between samples.

The local probe observes successful PortAudio writes and the production
coordinator's turn-correlated first-write timestamps. It uses the authenticated
WebSocket Chat entry, an isolated session/ledger/scratch directory, real
professional planning, Pi and TTS. It does not measure microphone ingress,
Electron click dispatch, acoustic delay or subjective listening.

## Deferred publication check

The maintainer requested that human microphone/listening acceptance be deferred
and the preparation PR remain a draft. Actual microphone speech recognition,
continuous-voice interaction and audible quality are therefore **pending**.
The draft does not claim that #112 or the macOS resource issue #103 is closed.
Merge/publication, final tag creation and release asset upload are separate
steps after review and the deferred acceptance.

Raw model outputs, logs, local paths, session handles, credentials and audio
materials remain local and are not included in the source release.
