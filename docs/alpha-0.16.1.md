# 0.16.1 Alpha: faster voice entry and complete Work context

Tag: `v0.16.1-alpha.0`; Python `0.16.1a0`; Electron `0.16.1-alpha.0`.
Comparison baseline: `v0.16.0-alpha.0`. This source Alpha includes #177–#181.
The source ZIP, file manifest and SHA-256 checksums do not bundle desktop
installers, model weights, character media, reference voices, Cubism Core or
credentials.

## Changes

- **Automatic NVIDIA speech acceleration (#177).** Fresh GPT-SoVITS settings
  select CUDA Graph automatically on a supported NVIDIA speech runtime. The
  optional FlashAttention KV-cache extension is used only when the device,
  precision and installed extension support it. Existing explicit `0`/`1`
  overrides remain authoritative; unsupported paths retain their existing
  behavior. No new model, extension or dependency installation is required.
- **Full terminal reports in Chat (#178).** Main Chat can use the latest
  terminal report from Work accepted in its session/context, including after
  restart. Provider attribution and Host completion state remain authoritative.
  Progress and narration stay concise. The built-in Japanese Kurisu persona
  normally uses at most six sentences, with exceptions for requested detail or
  a sufficient, accurate answer; explicit persona overrides still take priority.
- **Shorter voice-entry preparation (#179).** The application catalog uses SQL
  to preselect manifest-bearing Work instead of scanning every historical Work.
  Voice captures prepare one application snapshot while ASR runs; buffered
  barge-in captures also start their own preparation. Text entry can wait for
  the short query to preserve context. Focused-app discovery runs off the event
  loop. Model-request and first-text timing identify the remaining delay.
- **Non-blocking cold translation setup (#180, #181).** Normal subtitles and
  the VN translation bridge construct SDK/HTTP clients inside their existing
  request workers. Concurrent initialization reuses each module's client cache;
  provider requests and streaming remain outside the initialization lock.
  Speech content, action authority and playback ordering are unchanged.

## Install and upgrade

Extract the source ZIP into a new directory and follow the installation profile
in [README](../README.md). Retain the full optional-extra selection when running
`uv sync --locked`, then run `npm ci` and `npm run build` in `electron/`.
Stop incoming turns and drain or stop Work before upgrading. Back up settings
and persistent data, retain external assets separately, and restart the backend.

Existing saved acceleration settings are preserved. To use automatic selection,
choose Automatic in the desktop inference settings or set the documented
`ENABLE_CUDA_GRAPH=auto` and `TTS_T2S_FLASH_ATTN=auto` overrides.
Manual `0`/`1` values remain supported. No database migration is added by this
patch. The [0.16 runtime migration guidance](alpha-0.16.md#install-and-upgrade)
still applies to upgrades from older versions.

## Evidence and limits

The [0.16.1 acceptance record](alpha-0.16.1-acceptance.md) separates deterministic
checks from the maintainer's real microphone observations. Local voice samples
show less delay, but different utterances, model/network timing and TTS-cache
hits prevent treating the aggregate difference as a controlled benchmark.
ASR speed itself is not improved by these changes. The measurements use software
playback markers, not an external acoustic measurement. Physical barge-in and
the #181 translation change were not covered by that microphone sample.

Experimental platform/model boundaries and the unresolved dependency audit
findings from [0.16](alpha-0.16-acceptance.md#dependency-audit-disposition) remain.
This patch does not change dependency versions or claim a clean audit.

## 中文摘要

0.16.1 Alpha 包含 #177–#181：自动选择受支持的 NVIDIA 语音加速，让后续对话
读取完整终态 Work 报告，并缩短语音入口应用查询和字幕翻译冷启动带来的等待。
应用上下文查询与 ASR 重叠，打断采集也会建立本轮快照；纯文字入口保留必要上下文。
任务权限、播报排队和已有手动加速配置保持原有语义。

本次仍发布源码 ZIP、逐文件清单和 SHA-256 校验文件。实机日志观察到首声更快，
但并非严格同条件 A/B，也不代表 ASR 本身提速或所有实验功能已完成实机验收。
升级使用新目录、保留完整安装配置和外部资源，并重启后端。
