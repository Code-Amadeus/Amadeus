# 0.17.0 Alpha: faster local voice, hybrid rendering and Work reliability

Candidate tag: `v0.17.0-alpha.0`; Python `0.17.0a0`; Electron `0.17.0-alpha.0`.
Comparison baseline: `v0.16.1-alpha.0`. This is a source Alpha candidate;
preparation and passing checks do not themselves publish a release.
Desktop installers, model weights, character media, reference voices,
Cubism Core and credentials remain separately supplied.

## Changes since 0.16.1

- Local speech reuses semantic history, shares nested KV storage and reuses
  bucket CUDA graphs (#205, #206). ASR can prepare CUDA Graph decoding for
  conversational audio (#202). Supported-device checks and existing explicit
  configuration remain authoritative; these are not universal speed guarantees.
- Rendering selects hybrid video/static frames only with a matching installed
  package and a supported decoder, with static fallback and visible Graphics
  status (#200). Ambient actions are paced from speech completion (#204).
  See [the frame-source contract](frame_video_candidate.md) for budgets and limits.
- Work preserves narration evidence and terminal facts, publishes permission
  changes received during snapshots, and separates registered files from verified
  AUIP application delivery (#189, #209, #210).
- Manual MCP tests isolate inherited credentials and require explicit bindings;
  Codex forwards the configured stdio environment names (#197, #211). Codex MCP
  empty-form confirmations use native action/content responses (#208), while
  unsupported form/URL input cannot be accepted by the existing permission UI.
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

- [#185](https://github.com/Code-Amadeus/Amadeus/issues/185): intermittent Windows
  wallpaper placement can hide the input surface; abnormal-exit recovery still
  needs reporter-specific evidence. This is not fixed by hiding Plugins.
- [#207](https://github.com/Code-Amadeus/Amadeus/issues/207): sequential Codex Work
  can retain exclusive MCP resources; forceful unload is not proof of graceful
  application cleanup. #208 repairs approval responses, not connection lifetime.
- [#201](https://github.com/Code-Amadeus/Amadeus/issues/201): real microphone/LLM
  interruption, final-display visual acceptance and wider GPU/webview coverage
  remain open. Synthetic rendering tests do not close those checks.
- [#199](https://github.com/Code-Amadeus/Amadeus/pull/199) and its bounded follow-up
  [#213](https://github.com/Code-Amadeus/Amadeus/pull/213) require separate review.
  Their output-device retry is not included in this candidate and is not a
  demonstrated AirPods/shared-host-state repair.
- Existing Pi and optional-model dependency advisories remain disclosed; see
  [acceptance](alpha-0.17.0-acceptance.md). This is not a vulnerability-free release.

Publish only after required final-head checks and maintainer review. If Windows
wallpaper reliability or continuous exclusive-MCP desktop work is a release
promise, resolve #185/#207 before making that promise. Other new memory,
continuity and broad provider changes are outside this release preparation.

## 中文摘要

0.17.0 Alpha 集中发布 0.16.1 以来的本地 ASR/TTS、混合视频供帧、Work 事实与权限同步、
MCP 环境迁移以及退出/取消修复。插件代码保留，主侧栏默认不显示 Plugins；设置中的管理
能力仍可用于后续集成，不代表插件商店已成熟，也不会禁用已安装扩展。

这仍是源码 Alpha 候选。壁纸输入框间歇失效（#185）、跨 Work 的独占 MCP 生命周期（#207）、
更多设备与真实打断验收（#201）尚未闭合。音频设备重试另见待复审的 #213，本候选不宣称
已包含该修复。版本更新不代替最终 CI、复审和明确的发布决定。
