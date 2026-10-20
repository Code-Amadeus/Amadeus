# 0.17.1 Alpha: checked Japanese dictionary and desktop fixes

Release tag: `v0.17.1-alpha.0`; Python `0.17.1a0`; Electron `0.17.1-alpha.0`.
Baseline: `v0.17.0-alpha.0`. This remains a **source Alpha release**.
Desktop installers, voice/model weights, character media, reference recordings
and credentials are not included in the source ZIP.

## Post-release Linux Voice upgrade note (#249)

The AEC build repair in #249 is a follow-up and is **not included in the
`v0.17.1-alpha.0` tag**. Older Linux builds may have left locally regenerated
SWIG files and `vendor/aec-audio-processing/webrtc-audio-processing/install/`.
After updating to the fixed revision, back up intentional vendor source edits,
restore the committed `vendor/aec-audio-processing/src` bindings, and remove
that obsolete install directory after checking its contents. Mixed wrapper/proxy
versions now fail before native build work; no files are restored or deleted
automatically. See the [Linux upgrade steps](install_profiles.md#upgrading-an-existing-linux-voice-checkout).
Arch with system Abseil 20260817 still requires the reporter's cold-build
acceptance in #247.

## Application and dictionary in one Release

The application Release contains its source ZIP, source manifest, checksums and
the separately installable `japanese-pronunciation` dictionary **2026.10.20.3**
(129 rows / 127 distinct words, #237; public contract #244). The resource is
optional and no download occurs automatically. Earlier application releases
do not contain its loader. Existing voice/character packs need no rebuild.

The application and dictionary versions identify the tested pair for this
release. They are not a permanent obligation for future applications to load
old resource formats. Future releases ship the appropriate checked dictionary
as another attachment; unchanged data may reuse identical bytes after validation.
See the [publication procedure](../release/README.md#5-publish-the-application-and-optional-resources-together).

After setting up a supported local GPT-SoVITS profile, run from the project root:

```powershell
python tools/external_assets.py verify C:\Downloads\amadeus-japanese-pronunciation-2026.10.20.3.zip
python tools/external_assets.py install C:\Downloads\amadeus-japanese-pronunciation-2026.10.20.3.zip
python tools/external_assets.py status
```

Restart the backend. **Use installed Japanese dictionaries** controls activation;
`ENABLE_JAPANESE_USER_DICTIONARY=false` disables optional, personal and legacy
dictionaries and uses only the base dictionary. For an update conflict, inspect
customizations before explicitly using `install --overwrite`. Personal files
and legacy sources remain outside the official bundle.

The ZIP is 94,135 bytes; SHA-256:

```text
eb347fa68545056eac8ab9ff7473bfd11c4d1e02e673e64dc5663e574e53ed8c
```

[Dictionary behavior and quality limits](japanese_pronunciation_dictionary.md)
and [source/license basis](../LICENSES/JAPANESE-DICTIONARY.md) document the checked
ledger, immediate upstream MIT basis, recorded CMUdict/BSD-2-Clause source chain
and Amadeus AGPL-3.0 modifications. All notices travel in the dictionary ZIP.

## Other changes since 0.17.0

- Work gains opt-in bounded automatic approvals and exact-call Pi MCP grants
  (#234). Host-owned committed decisions, deny/read-only policies, connection
  identity and revocation remain authoritative. This does not enable unrestricted
  or default automatic approval.
- Pi 1.2.0 uses Host-managed MCP (#221), finishes transport disposal after
  producer cancellation (#226), and preserves image metadata for legacy DeepSeek
  selections (#225).
- Windows wallpaper preserves window geometry during reconciliation (#220),
  preserves rollback and recovers after host restarts (#228). These targeted
  repairs do not establish universal device or desktop-layer reliability.
- Chat/Wallpaper subtitles can select Chinese or English (#239). Chat role
  temperature defaults to 0.7 with reduced repeated openings (#229); existing
  explicit settings remain effective.
- Local TTS loads v3 references without requiring TorchCodec while preserving
  real failures (#238), and serializes shared playback stop/initialization (#242).
- Dependencies include the patched MCP SDK 2.2.0 (#232), aiohttp 3.14.4 and yarl
  1.25.1 (#243). Installation/setup documentation is updated (#216, #231).

## Recommended performance settings (carried forward from 0.17.0)

These recommendations retain the 0.17.0 setup and its workload/hardware limits;
they are not new 0.17.1 performance measurements. Existing explicit settings are
preserved during upgrades, so updating alone may retain a previous static/off choice.

1. **Install the matching character video pack.** Download
   `amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip` from the
   [official resource center](https://code-amadeus.github.io/#resources).
   The color-corrected pack contains 40 video clips / 7,672 frames and the
   key-point return transition. Google Drive uses 28 parts plus helper files:
   download all parts and run `Assemble-Kurisu.ps1` to reconstruct and verify
   the ZIP; never install an individual part. From the project root:

   ```powershell
   uv run --locked --no-sync python tools/external_assets.py verify "C:\Downloads\amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip"
   uv run --locked --no-sync python tools/external_assets.py install "C:\Downloads\amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip"
   ```

   Back up customized assets before upgrading an older pack, then add
   `--overwrite` to `install` when replacement is intended. This large pack is
   unchanged and is not re-uploaded with the dictionary.
2. **Graphics & performance:** select **Standard (60 FPS)**,
   **Frame source: Automatic · video when available**, and
   **Frame cache budget: Automatic · video 384 MiB / static 2 GiB**.
   Enable **Sample animation textures** and **Reuse converted textures on disk**.
   Standard/automatic and disk reuse are defaults; texture sampling defaults on
   at 60 FPS. Saved explicit settings take precedence over those defaults.
3. **Restart and check the actual path.** Restart the backend, reopen the
   character/wallpaper, and check **Actual animation frame source** for
   **Video with static heads · 384 MiB**. Selecting Automatic does not prove
   video use. Missing/incompatible video or decoder support keeps static frames
   with a 2 GiB automatic budget. These limits cover retained frame payloads,
   not total application RAM/VRAM or decoder memory.
4. **Local NVIDIA voice:** use the **T4 `local-cu124`** installation profile and
   compatible Qwen/GPT-SoVITS assets. Keep Qwen device on `auto`, Qwen CUDA Graph
   enabled (`QWEN3_ASR_CUDA_GRAPH=true`), and local TTS inference on Automatic
   (`ENABLE_CUDA_GRAPH=auto`, `TTS_T2S_FLASH_ATTN=auto`). Qwen FlashAttention
   defaults on (`QWEN3_ASR_FLASH_ATTN=true`), but FlashAttention paths require
   a separately installed compatible extension and supported hardware. Restart
   the backend and inspect startup diagnostics to confirm the selected path.
   Remote voice and CPU profiles do not receive these CUDA benefits.

The [current English setup guide](../README.md#recommended-017-graphics-setup)
and [NVIDIA voice guide](../README.md#recommended-nvidia-speech-acceleration)
provide profile-specific details. Historical timing/KV measurements and their
conditions remain in the [0.17.0 performance record](alpha-0.17.0-performance.md).

### 推荐性能设置与生效检查（延续 0.17.0）

以下沿用 0.17.0 的推荐配置，不代表 0.17.1 新测了一轮性能。升级会保留旧用户的手动
设置，不能只升级后就认定视频供帧或 CUDA 加速已经生效。

1. 从[官网资源中心](https://code-amadeus.github.io/#resources)下载已校色的
   `amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip`，包含 40 组视频、
   7,672 帧及 key point 返回过渡动画。Google Drive 需取齐 28 个分卷和辅助文件，
   用 `Assemble-Kurisu.ps1` 合并、校验后再安装，不能安装单个分卷。在项目根目录执行：

   ```powershell
   uv run --locked --no-sync python tools/external_assets.py verify "C:\Downloads\amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip"
   uv run --locked --no-sync python tools/external_assets.py install "C:\Downloads\amadeus-character-kurisu-2026.10.14-keypoint-return-v3.zip"
   ```

   覆盖旧角色包前备份自定义素材，再按需给 `install` 加 `--overwrite`。
   本版不重打或重传这个既有大包。
2. **设置 → 图形与性能**：选择**标准（60 FPS）**、
   **帧源选择：自动 · 可用时优先视频**、
   **帧缓存预算：自动 · 视频 384 MiB / 静态 2 GiB**，开启**启用动画纹理采样**和
   **复用磁盘中的已转码纹理**。标准、自动帧源/预算和磁盘复用本就是默认配置；
   纹理采样在 60 FPS 下默认开启。旧用户保存过的静态帧或关闭选项会保留，需自行核对。
3. **重启后端并重新打开角色／壁纸**，查看**当前实际动画帧源**是否为
   **视频与静态入口帧 · 384 MiB**。选择“自动”不代表已经用视频；缺少匹配素材或
   解码能力时仍走静态帧，自动预算为 2 GiB。预算限制的是保留的帧数据，不等于
   整个应用或解码器的总内存／显存。
4. **本地 NVIDIA 语音**：安装 **T4 `local-cu124`** 环境及 Qwen/GPT-SoVITS 资源。
   Qwen 设备保持 `auto`，开启 Qwen CUDA Graph（`QWEN3_ASR_CUDA_GRAPH=true`），
   本地 TTS 推理保持**自动**（`ENABLE_CUDA_GRAPH=auto`、`TTS_T2S_FLASH_ATTN=auto`）。
   Qwen FlashAttention 默认开启（`QWEN3_ASR_FLASH_ATTN=true`），但相关路径仍需另装
   兼容扩展且硬件支持。修改后重启后端，通过启动诊断核对实际路径；远程语音和 CPU
   配置不享有这些 CUDA 优化。

另见[中文图形设置](../README_ZH.md#017-推荐图形设置与生效检查)与
[NVIDIA 语音设置](../README_ZH.md#推荐的-nvidia-语音加速配置)。原有性能数字保留在
[0.17.0 性能对照](alpha-0.17.0-performance.md)，不改写为本版新结果。

## Verification and limits

The underlying merged mainline `8746938e` passed all its hosted workflows.
Release preparation must additionally pass final-head CI and the tag's extracted
source workflow before publication. Those workflow results and the final release
commit are recorded in the GitHub Release; a planned check is not a passing result.

Dictionary acceptance exercises every released word in three production
`clean_text` contexts (381 checks), all 129 ledger memberships, baseline units,
base-owned words and common English fallback controls. Candidate integrity,
clean installation and idempotent reinstall have passed. Lexical checking is
model-assisted, including independent model review, **not human/native-speaker
sign-off**. Human listening and pitch-accent acceptance remain pending, including
the provisional flat accent of `proposer`. Unlisted English words can still be
spelled as letters. There is no exhaustive pronunciation, emotional delivery or
voice-quality claim.

The separate experimental context-reading switch remains **off by default**.
Its tsqyomi v5 model is **not distributed in this Release**: the recorded gate is
89/92, including two regressions of previously correct contextual readings.
Installing the dictionary does not install or enable that model.

Previously unqualified full-round AUIP gameplay, deep Windows SDK paths, wider
GPU/webview coverage, real acoustic interruption and same-process AirPods
reconnection remain outside this release's acceptance. Targeted fixes and CI do
not certify all devices or close every historical live failure. Optional-model
dependency advisories and upstream Pi audit disposition remain separate from the
patched core advisories; this is not a vulnerability-free claim.

## Upgrade

Stop incoming turns and settle active Work, then extract into a fresh directory.
Preserve settings, durable data and external assets. Synchronize dependencies
using the **complete** installation profile, then run `npm ci` and `npm run build`
in `electron/` and restart the backend. Do not run a core-only sync against a
voice/model installation unless intentionally removing its optional tiers.
The source-release workflow checks native settings migration from
`v0.17.0-alpha.0`. Optional dictionary installation is a separate explicit step.

## 中文摘要

0.17.1 Alpha 同时提供新版应用源码和可选日语词典附件；词典不塞进源码包，也不自动
安装。每次 Release 记录实际验收的应用与词典组合，未来格式升级时随新版应用提供
对应资源，不要求新代码永久迁就旧包。词典 2026.10.20.3 只收录逐条检查清单中的
129 行／127 词；人工试听与重音验收仍待完成。安装后重启后端，设置中可关闭。

本版还包含 Work 有界自动批准、Pi Host 管理 MCP、壁纸回滚与重启恢复、字幕语言选择、
TTS 参考音频与播放并发修复，以及核心依赖安全更新。语境模型默认关闭且本次不分发。
升级保留设置与外置资产，按完整安装 profile 同步依赖并重建 Electron。
