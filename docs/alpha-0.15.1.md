# 0.15.1 Alpha: desktop setup, Pi and VN speech

Status: release candidate; not published. Intended tag: `v0.15.1-alpha.0`,
Python version `0.15.1a0`, Electron version `0.15.1-alpha.0`.
Publish as a GitHub **prerelease** after the release checklist below is complete.

This source snapshot follows `v0.15.0-alpha.0`. It brings together 28 mainline
commits through `4bf0e85`, plus source-distribution repairs and release preparation.
The [0.15 routing document](alpha-0.15.md) retains its historical measurements;
it is not current-candidate acceptance evidence.

## Changes

- **Pi and provider roles (#104, #108, #109):** normal desktop installation now
  includes the pinned Pi runtime. Coding and daily execution can use independent
  providers. Existing Work retains its execution owner. Pi reuses the configured
  model connection and still requires valid credentials.
- **Desktop settings (#95, #96, #105, #113):** model connections, role assignments
  and capability controls are grouped explicitly, with revised locale, theme,
  graphics and startup settings.
- **Windows wallpaper (#111, #115):** managed first-run setup, startup, tray,
  lifecycle and restoration; unified composer controls; preserved keyboard
  chat turn identity.
- **Companion and VN (#90, #91, #93, #116–#118):** optional shared Lite portraits,
  reusable game profiles, text-source adapters, shared character prompts and
  first-segment speech delivery with bounded inference.
- **Voice and model compatibility (#85, #88, #102, #106, #114):** Fish Audio
  WebSocket support, GPT-SoVITS v2Pro inference, CPU synthesis isolation and
  ROCm streaming-path repairs.
- **Interaction fixes (#87, #107):** DeepSeek Flash image input and concrete
  Japanese Work terminal reports.
- **Source distribution:** include desktop setup scripts, configuration guides
  and their supporting resources. Source Release CI now installs, packages and
  launches the application from the extracted ZIP, so a successful repository
  build cannot hide missing archive inputs.
- **Draft execution:** use the accepted Work title for both its directory and
  ledger record. A shortened planner title no longer causes Host intake to
  reject an otherwise valid new Draft.
- **Cached opening latency:** cached first-sentence audio no longer waits for
  the CUDA Graph inference lock. Actual inference remains serialized, with
  unchanged stale-turn and playback-order checks.

## Install, upgrade and rollback

This is a **source release**. It does not bundle a desktop installer, Python
runtime, model weights, character/voice assets, credentials or user state.
Optional assets remain separately installed.

1. Preserve personal configuration and user data. Extract into a new directory
   rather than overwriting the previous checkout or its virtual environment.
2. Follow the [installation profiles](install_profiles.md). Run `uv sync --locked`
   with the complete optional-extra selection for your intended profile.
   Omitted extras are removed; do not use the core-only command to upgrade a
   voice/model environment unless that removal is intended.
3. Run `npm ci` and `npm run build` in `electron/`. The postinstall installs
   both Electron and Pi. Check [Pi configuration](pi-rpc-provider.md) before
   relying on the provider; installing its CLI does not validate model access.
4. Preserve existing explicit provider and desktop settings. Provider-role and
   backend configuration changes take effect after backend restart.
5. If a macOS launcher embeds the old checkout path, rebuild it using the
   [macOS startup guide](macos_wallpaper_startup.md). Windows source wallpaper
   setup uses the included [setup script and .NET helper](windows_wallpaper_lifecycle.md).

For source rollback, use a separate checkout of `v0.15.0-alpha.0` and recreate
its environment from its lockfiles. Source rollback does not reverse subsequent
user-data changes. Preserve personal files before changing versions.

## Validation scope and Alpha limits

See the [candidate acceptance record](alpha-0.15.1-acceptance.md) for the
release-blocking defects found, their repairs, upgrade checks and live samples.
The mainline base passed Windows Python suites and packaged desktop smoke,
Linux/macOS platform suites, Electron tests/build, and the local-model
installation matrix. Release-candidate results belong to the release PR's
checks and acceptance record; those are distinct from base-commit evidence.

The archive preflight found and repaired missing desktop setup files. A fresh
extraction passed Python and Electron installation, Pi CLI startup, 155 Electron
tests, 35 Pi provider/runtime/web tests, 13 release-tool tests, 13 .NET wallpaper
contracts, Windows packaging and the packaged model-less desktop smoke.
The Pi runtime tests use the real CLI with a deterministic local model endpoint;
they are not real-model task evidence. Repeat-archive hashes and extracted-file
hashes matched. Candidate CI rechecks the final source package.

These boundaries remain explicit:

- **Hardware microphone and listening acceptance is pending.** Native composer,
  lifecycle and synthetic ASR tests do not replace speaking into a microphone
  and listening to playback. Track this in
  [#112](https://github.com/Code-Amadeus/Amadeus/issues/112).
- The reported [macOS wallpaper memory/GPU issue #103](https://github.com/Code-Amadeus/Amadeus/issues/103)
  remains open. This release does not claim to solve or requalify it.
- CUDA/MPS/ROCm installation and import checks do not qualify every physical GPU,
  driver or local model. Linux voice source-build checks do not prove live audio.
- Historical warm Chat and VN first-audio samples are conditional observations,
  not latency guarantees for this candidate. Device-write measurements, where
  recorded, are not acoustic-loopback or subjective listening measurements.
- ACP remains experimental. Basic cooperative routing with explicit provider
  constraints, arbitrary cross-provider batches and restart persistence for
  deferred application entry retain the documented 0.15 Alpha limits.

## Release checklist

- [ ] Candidate PR checks, including the extracted-source application smoke, pass.
- [ ] Record candidate acceptance results and remaining hardware limitations.
- [ ] Complete the deferred human microphone/listening check before publication.
- [ ] Merge the reviewed preparation PR and confirm the resulting mainline checks.
- [ ] Build the source ZIP, source manifest and SHA256SUMS from that clean commit;
  verify file hashes and the tag's target before publishing the prerelease.

The preparation PR does not create a tag or publish a GitHub Release. Artifacts
from earlier preflight commits are not substitutes for final tagged artifacts.

## 中文摘要

0.15.1 Alpha 汇集 Pi 默认接入与角色分配、能力设置界面、Windows 壁纸管理、
Companion Lite、VN 配置与首段流式语音，以及 Fish Audio、v2Pro 和语音修复。
本次仍提供源码包，补齐了 ZIP 缺少的平台脚本和配置文档，并增加解压源码包
后的构建、打包和启动检查。

升级时保留个人配置与数据，为目标能力给出完整的 uv extras，并在 electron
目录重新运行 npm ci 和 npm run build。真实麦克风与听感验收仍待完成，
macOS 壁纸资源占用问题继续公开保留。草稿准备 PR 不等于已发布版本。
