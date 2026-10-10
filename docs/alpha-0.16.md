# 0.16.0 Alpha: unified Chat and local character management

Tag: `v0.16.0-alpha.0`; Python `0.16.0a0`; Electron `0.16.0-alpha.0`.
This is a source Alpha release. Model weights, character media, reference voices,
Live2D Cubism Core, credentials and desktop installers are not bundled.
The comparison baseline is `v0.15.2-alpha.0`.

## Changes

- **One Chat runtime.** Cooperative owns production Chat in both professional
  and basic mode. Shared presentation retains streaming, expressions and speech;
  the old Original orchestration path is retired. Provider handoffs preserve
  shared constraints without expanding the assigned task's scope.
- **Local character management.** Settings can create and edit local roles,
  select the next startup identity, and recover explicitly from an invalid role.
  Conversations remain owned by their role; accepted Work retains its identity.
  Settings groups identity, appearance and the existing Kurisu knowledge controls
  under Characters, with voice configuration in its single Voice editor.
- **Experimental Live2D.** Optional local Live2D models use the existing render
  and wallpaper path, including expression and mouth signals. Sprite remains
  available. Users supply the model and Cubism Core under their respective terms.
- **Desktop and rendering.** Wallpaper and Render transitions share serialized
  lifecycle ownership. Texture residency is bounded and BC7 frames can be cached.
  SpriteForge framing uses the pack canvas; wallpaper preserves aspect ratio.
  Connection editors have consistent save state and dark Render backgrounds.
- **Voice and history.** Speech aggregation accounts for ready audio and the
  selected synthesis profile; idle output streams persist between turns.
  Continuous voice can return to wake standby, Qwen3-ASR can remain in system
  memory, and BERT loads only for Chinese GPT-SoVITS text. V3 emotion references
  remain opt-in. Sessions retain complete history while Main Chat receives a
  bounded recent window.
- **Configuration and recovery.** Setting declarations and built-in handler
  registration share one catalog. Durable state writes, runtime diagnostics and
  translation checks are strengthened. Restoring Watching mode preserves an
  explicitly disabled vision setting.

## Install and upgrade

Use the source ZIP and the installation profile in [README](../README.md).
Extract into a new directory and run `uv sync --locked` with the complete set of
extras for your profile, then `npm ci` and `npm run build` in `electron/`.
Running only the core install command against an existing optional-model
environment removes extras; retain your full profile selection.

Before upgrading, stop incoming turns and drain or stop owned Work. Back up your
settings and persistent data. Keep external assets separate and configure their
locations in Settings. Do not copy caches or an entire old virtual environment.

The retired `COOPERATIVE_CHAT_ENABLED=false` no longer selects Original Chat.
Settings explains and acknowledges migration; an obsolete `.env` assignment
must be removed from that file. `COOPERATIVE_WORK_PLANNER_ENABLED=false` still
selects basic Cooperative. There is no hidden Original fallback.

Pure-local Chat uses `llama_server`; persistent `cli` sessions require migration.
The managed pure-local context defaults to 16384, while Hybrid keeps 4096.
Explicit saved values are preserved. Hybrid now uses its configured local head
alongside the remote role model. See [runtime migration](chat-runtime-convergence.md).

Role selection applies after restart. Appearance and voice remain application-wide;
creating a role does not create a separate voice, artwork or memory library.
See [character management](character-management.md) and
[Live2D setup](live2d_character_visuals.md).

For rollback, use a separate checkout or source directory of `v0.15.2-alpha.0`.
Do not overwrite newer history or Work receipts with an old database snapshot.
Acknowledging removal of the retired route setting does not restore that setting
when older software is started. Unknown external Provider outcomes remain unknown.

## Validation and known limits

The [0.16 acceptance record](alpha-0.16-acceptance.md) distinguishes this release's
checks from earlier feature evidence. Alpha does not imply that every model,
device, operating system or experimental surface has been qualified.

- Model-less CI does not establish microphone recognition, audible quality,
  acoustic latency, GPU inference or long-duration resource stability.
- Browser cross-domain journeys and pure-local model task interpretation retain
  recorded limitations; LM Studio/Ollama capacity behavior remains unqualified.
  See the [runtime acceptance boundaries](chat-runtime-convergence.md#evidence-and-remaining-acceptance).
- Live2D and VN remain experimental. Live2D model physics/motions and external
  wallpaper hosts depend on the supplied assets and environment.
- Per-role voice/appearance bindings, automatic asset installation and general
  cross-session semantic memory are not part of this release.
- Platform and optional GPU-profile support remains limited to the evidence
  documented in [installation profiles](install_profiles.md).
- Dependency audits are not clean: the pinned Pi runtime retains a known
  brace-expansion denial-of-service risk, and Electron build tooling retains
  moderate findings. See the [exact audit disposition](alpha-0.16-acceptance.md#dependency-audit-disposition).

## 中文摘要

0.16.0 Alpha 汇总自 0.15.2 以来的运行时与桌面更新：统一 Cooperative Chat，
加入本地角色管理、启动恢复和实验性 Live2D，整理角色与连接设置，改进纹理驻留、
语音调度、完整历史保存、配置声明及持久化可靠性。

这是源码预发布，不包含桌面安装器、模型权重、角色素材、参考语音、Cubism Core
或凭据。升级请使用新目录并保留完整的可选依赖配置；先停止输入和进行中的 Work。
旧 Original Chat 开关已退役，角色切换需重启，声音与外观仍为应用级设置。
已知限制和本轮实测范围见验收记录；既有单项测试不代表所有硬件与实验功能均已验收。
