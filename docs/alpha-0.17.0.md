# 0.17.0 Alpha: faster local voice and Work reliability

Release tag: `v0.17.0-alpha.0`; Python `0.17.0a0`; Electron `0.17.0-alpha.0`.
Comparison baseline: `v0.16.1-alpha.0`. This is a source Alpha release. The maintainer approved publication after
final CI with the known limitations below; this is not a full live-suite pass.
Desktop installers, model weights, character media, reference voices,
Cubism Core and credentials remain separately supplied.

Release tracking: [#215](https://github.com/Code-Amadeus/Amadeus/issues/215).
Detailed [performance comparisons](alpha-0.17.0-performance.md) include measurement
conditions, source revisions and counterexamples. Some live paths remain unqualified;
see [live results and failures](alpha-0.17.0-acceptance.md#live-product-acceptance).

## Changes since 0.16.1

- Local speech reuses semantic history, shares nested KV storage and reuses
  bucket CUDA graphs (#205, #206). ASR can prepare CUDA Graph decoding for
  conversational audio (#202). Supported-device checks and existing explicit
  configuration remain authoritative; these are not universal speed guarantees.
- Rendering selects hybrid video/static frames only with a matching installed
  package and a supported decoder, with static fallback and visible Graphics
  status (#200). The separately distributed, color-corrected
  `2026.10.14-keypoint-return-v3` character pack includes 40 video clips
  (7,672 frames), available from the [official resource page](https://code-amadeus.github.io/#resources).
  Matching video packs can use video in auto mode; older static-only packs retain
  static behavior. These media files are not included in the source ZIP.
  Ambient actions are paced from speech completion (#204).
  See [the frame-source contract](frame_video_candidate.md) for budgets and limits.
- Work preserves narration evidence and terminal facts, publishes permission
  changes received during snapshots, and separates registered files from verified
  AUIP application delivery (#189, #209, #210).
- Manual MCP tests isolate inherited credentials and require explicit bindings;
  Codex forwards the configured stdio environment names (#197, #211). Codex MCP
  empty-form confirmations use native action/content responses (#208), while
  unsupported form/URL input cannot be accepted by the existing permission UI.
- Audio output retries PortAudio `-9986` once with a new output owner, and
  propagates persistent or unsupported-format failures (#213, incorporating #199).
  Other active capture owners retain their references; this is not a global
  audio reset or a qualified AirPods reconnect repair.
- Owned backend shutdown/restart reports unresolved child lifetime explicitly
  (#198); development startup/shutdown and cancelled VN translation streams
  have focused fixes (#186, #183).
- Experimental extension lifecycle, Agent bindings and data-only themes remain
  in the source (#188, #191, #192). The main sidebar no longer advertises Plugins.
  Settings retains extension management and the direct plugin route remains
  available for integration work. This does not disable installed extensions or
  certify a marketplace; the extension protocol and wider integrations remain
  provisional.

## Upgrade

Extract into a new directory. Preserve settings, persistent data and external
assets; stop incoming turns and settle active Work before replacing the runtime.
Follow the installation profile in [README](../README.md), retaining the full
optional-extra selection when running `uv sync --locked`. Run `npm ci` and
`npm run build` in `electron/`, then restart the backend.

Native settings and encrypted-credential migration from `v0.16.1-alpha.0` is
covered by the source-release workflow. Existing explicit rendering/voice
settings remain in effect. Old character packs can continue using static frames.
For a manual stdio MCP probe that previously inherited ambient credentials,
save the required values as explicit encrypted bindings; see
[MCP migration](mcp_environment.md).

## Known limits and release decision

- AUIP automatic Gomoku play produced `b2_candidate_not_available` during a
  full-round test. The invalid/unavailable choice was not executed. A later run
  completed gameplay, but consistent full-round success remains unqualified;
  no silent retry or weaker candidate validation was added.
- On Windows, deeply nested project/application paths can prevent a generated
  AUIP app from loading SDK files. The observed 263–266-character paths reported
  `ERR_FILE_NOT_FOUND` / `managed_sdk_unavailable` without identifying path length.
  A byte-identical short-path copy passed, and a fresh short-path full control
  journey completed. Use a shallow workspace path until this limitation is fixed.
- [#185](https://github.com/Code-Amadeus/Amadeus/issues/185): intermittent Windows
  wallpaper placement can hide the input surface; abnormal-exit recovery still
  needs reporter-specific evidence. This is not fixed by hiding Plugins.
- [#207](https://github.com/Code-Amadeus/Amadeus/issues/207): sequential Codex Work
  can retain exclusive MCP resources; forceful unload is not proof of graceful
  application cleanup. #208 repairs approval responses, not connection lifetime.
- [#201](https://github.com/Code-Amadeus/Amadeus/issues/201): real microphone/LLM
  interruption, final-display visual acceptance and wider GPU/webview coverage
  remain open. Synthetic rendering tests do not close those checks.
- [#213](https://github.com/Code-Amadeus/Amadeus/pull/213) includes the narrowed
  output-device retry from #199. Actual same-process macOS/AirPods reconnect
  remains untested; capture-owned shared host state is not forcibly reset.
- Existing Pi and optional-model dependency advisories remain disclosed; see
  [acceptance](alpha-0.17.0-acceptance.md). This is not a vulnerability-free release.

The maintainer accepted these limitations for the source Alpha, conditional on
all final-head CI checks passing. This decision does not turn retained failed
live runs into passing tests. If Windows
wallpaper reliability or continuous exclusive-MCP desktop work is a release
promise, resolve #185/#207 before making that promise. Other new memory,
continuity and broad provider changes are outside this release preparation.

## 中文摘要

0.17.0 Alpha 集中发布 0.16.1 以来的本地 ASR/TTS、混合视频供帧、Work 事实与权限同步、
MCP 环境迁移以及退出/取消修复。已校色的 `2026.10.14-keypoint-return-v3` 角色包已独立发布，包含 40 组视频、7,672 帧；匹配的视频包可在 auto 模式启用视频，旧静态包继续使用静态帧。源码 ZIP 不包含这些媒体。插件代码保留，主侧栏默认不显示 Plugins；设置中的管理
能力仍可用于后续集成，不代表插件商店已成熟，也不会禁用已安装扩展。

这仍是源码 Alpha。壁纸输入框间歇失效（#185）、跨 Work 的独占 MCP 生命周期（#207）、
更多设备与真实打断验收（#201）尚未闭合。#213 纳入一次有界的音频输出重试，但不宣称
完成 AirPods 实机重连验证或全局音频重置。五子棋自动对局仍有候选不可用导致停止的已知问题；Windows 过深路径可能导致生成应用的 SDK 加载失败，建议使用浅层工作目录。维护者已同意在最终 CI 全绿后带这些明确限制发布，原始失败结果保留。
