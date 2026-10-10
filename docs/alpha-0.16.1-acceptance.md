# 0.16.1 Alpha acceptance record

Release scope: changes after `v0.16.0-alpha.0`, through main commit `60c5d57`
(#177–#181), followed by version metadata and source-release preparation.
Final candidate checks are identified by the preparation PR's head SHA; final
archive identity is recorded in `source-manifest.json` and the GitHub Release.

## Automated evidence

- #179's final head passed all 16 CI checks, including the buffered-capture and
  abandoned-utterance snapshot regression tests.
- #180 and #181 each passed all 14 checks on their final heads. #181 was updated
  to include merged #179 and #180 before its final CI and merge.
- Combined local voice and entry checks passed 115 tests. On the clean release
  worktree, these suites plus `tests/test_release_tooling.py` passed **128**
  tests. The only warning was Python 3.12's existing `audioop` deprecation.
- `uv lock --check --offline` passed after the product-version update. Python
  and npm dependency selections remain unchanged.
- Source Release CI uses `v0.16.0-alpha.0` for the previous-release native
  encrypted-settings upgrade. It installs, builds, packages and launches the
  application from the extracted allowlisted ZIP, not the full Git checkout.
- Publication requires the final candidate checks, source/provenance gates,
  applicable mainline/tag workflows, and final asset hash verification. Earlier
  feature passes do not replace the final candidate or tagged-archive checks.

For #177's NVIDIA capability selection and #178's terminal-report semantics,
see their merged PR validation records. This patch release does not relabel
those earlier checks as new full-device or semantic-model acceptance.

## Real microphone observations

The maintainer exercised the real Windows microphone, Qwen3-ASR, remote
DeepSeek Chat and local GPT-SoVITS playback. The following sanitized comparison
uses seven complete older turns and the first ten complete new turns on
2026-10-11 (Pacific/Auckland). The older local baseline already included #177
and #178; this is not a direct comparison against the 0.16.0 release tag.
The new running process included #179 and #180, before loading #181.

| Median latency | Older local baseline | New sample |
|---|---:|---:|
| ASR text ready to first-sound marker | 1,975 ms | 1,617.5 ms |
| Capture finish to first-sound marker | 2,416 ms | 2,093 ms |
| ASR text ready to first-sentence enqueue | 1,378 ms | 1,129.5 ms |
| Capture finish to ASR text ready | 398 ms | 515 ms |

Application-context preparation took 17.66–46.04 ms and overlapped ASR; all ten
foreground waits were 0.02 ms. The first subtitle cold-start delay between TTS
start intent and function entry fell from 409 ms to 1 ms. The first-sentence
queue-to-function interval was 1–2 ms in the new sample.

The ten observed model request-to-first-text times ranged from 684.4 to
1,639.1 ms (median 935.4 ms). The first main-model client preparation took
429.1 ms; subsequent preparations logged 0.0 ms at the instrument's precision.

These are observational software timings. Different utterances, remote timing
and cached speech (two of seven old first sentences, four of ten new ones)
confound aggregate before/after attribution. A first-sound marker is recorded
before writing the first voiced playback window; it is not a microphone-loopback
measurement of sound reaching the listener. No ASR speedup is established.
No `handoff=True` capture occurred in this sample, so physical barge-in is not
claimed. #181 has regression/CI evidence, not a new physical VN acceptance run.
Raw conversations, session identifiers, logs and personal paths are not included.

## Remaining observations and boundaries

No ERROR-level records occurred in the ten-turn sample. Background event
callbacks still recorded 1,015–1,797 ms elapsed durations; elapsed callback time
alone does not prove synchronous event-loop blocking. Four 45-second ASR
turn-completion timeout warnings also occurred in the older run. These are not
claimed fixed or newly introduced by this patch. Existing background narration
and foreground speech queue ordering is intentional and remains unchanged.

The dependency-audit disposition and experimental platform/model limitations
from [0.16](alpha-0.16-acceptance.md) remain in force. This release does not
include dependency upgrades, long-duration stability qualification, a complete
new device matrix, or renewed speech-quality acceptance. Uncommitted local voice
configuration is excluded from the source package.
