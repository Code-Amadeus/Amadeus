# 0.15.2 Alpha: speech starts at the first word

Status: release candidate; not published. Intended tag: `v0.15.2-alpha.0`,
Python version `0.15.2a0`, Electron version `0.15.2-alpha.0`.
Publish as a GitHub **prerelease** after the release checklist below is complete.

This patch follows `v0.15.1-alpha.0` with one voice fix. It changes no
dependencies, settings, sentence splitting or synthesis scheduling.

## Changes

- **Speech starts at its first word.** Local GPT-SoVITS output opened every
  synthesized unit with a generated pause before the first word: 0.79–1.01 s
  across 58 production outputs from the bundled Kurisu v2Pro, v3 and retrained
  v3 checkpoints with the default reference, independent of CFM steps and
  chunking. The pause was played in full, so each reply's first word and every
  later unit started that much late. The GPT-SoVITS adapter now starts each
  unit 50 ms before its first voiced frame (10 ms RMS at or above −45 dBFS);
  the rest of the waveform is unchanged sample for sample. Remote voice
  backends are unaffected.
- **First sound means audible speech.** The `first_play` latency marker and the
  `first sound started` log now fire when the first voiced 10 ms window is
  written, and record `lead_ms`, the silence written before it. Earlier
  first-sound figures, including historical documents, were first device-write
  times that excluded the pause. Post-fix figures are therefore close to
  pre-fix ones for the same run (audio still arrives at the same time); use the
  voiced-sample positions below, not old logs, as evidence of the change.
- **Cached opening audio regenerates once.** The first-sentence audio cache
  revision changes, so entries that still hold the pause are not replayed.

## Measured effect

Same in-project Kurisu v3 checkpoint, seed and production parameters (CUDA
Graph; first unit 4 CFM steps at speed 1.1, later units 16 steps), with only
the trim toggled:

| Unit | Pause removed | First voiced sample | Synthesis time |
|---|---:|---:|---:|
| First sentence | 650 ms | 700 → 50 ms | 0.289 → 0.284 s |
| Second | 760 ms | 810 → 50 ms | 1.125 → 1.140 s |
| Third | 740 ms | 790 → 50 ms | 0.631 → 0.627 s |

For that three-unit reply synthesized serially from t = 0 (all text available,
LLM time excluded), the first word moved from 0.989 s to 0.334 s and the gaps
between units from 0.91/0.92 s to 0.15/0.18 s; the reply ends 2.2 s earlier.
The gap between units is now the unchanged `pause_second` (50 ms) plus the
50 ms pre-roll and the previous unit's tail, so sentence transitions are
tighter. The maintainer listened to the before/after pair and kept this pacing.

## Install, upgrade and rollback

Upgrade as for [0.15.1 Alpha](alpha-0.15.1.md#install-upgrade-and-rollback): this
is a source release; extract into a new directory, run `uv sync --locked` with
the complete optional-extra selection for your profile, then `npm ci` and
`npm run build` in `electron/`. There is no new dependency or setting. For
rollback, use a separate checkout of `v0.15.1-alpha.0`.

## Validation scope

- Tests cover the voiced-frame boundary, chunked-stream equivalence with
  whole-unit trimming, the adapter trim on arrays and tensors, the first-sound
  marker on both the streaming and complete-audio playback paths, and rejection
  of the previous cache revision.
- Real-model check on an RTX 4070 Ti SUPER (CUDA): the table above; the
  chunked-stream path emits its first chunk 50 ms before speech with the
  subtitle text attached. These are sample positions, not acoustic loopback.
  ROCm, MPS and CPU paths share the code but were not run live.
- Hardware microphone and listening acceptance remains tracked in
  [#112](https://github.com/Code-Amadeus/Amadeus/issues/112).

## Release checklist

- [ ] PR checks pass and review is complete.
- [ ] Merge the reviewed PR and confirm the resulting mainline checks.
- [ ] Build the source ZIP, source manifest and SHA256SUMS from that clean
  commit; verify file hashes and the tag's target before publishing the
  prerelease `v0.15.2-alpha.0`.

## 中文摘要

0.15.2 Alpha 只修一个问题：本地 GPT-SoVITS 每段合成音频开头都有约 0.8–1.0
秒由模型生成的静音，并且会被完整播放，导致每轮回复的第一个字和之后的每一段都
延迟相应时间。现在每段从第一个有声帧前 50 ms 开始，其余波形逐样本不变。首声
延迟标记改为记录第一个有声样本（附带 `lead_ms`），此前的首声数据只是首次写入
设备的时间，没有计入这段静音。首句音频缓存会重新生成一次。依赖、设置、切句和
合成调度均未改变；远端语音后端不受影响。
