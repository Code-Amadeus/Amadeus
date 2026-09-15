# 30/60 FPS 与纹理加载对齐实验

日期：2026-09-16（本地时区 Pacific/Auckland；产物目录使用 UTC）。

后续发布开关：`RENDER_TEXTURE_SAMPLING=false`（默认）。下述 A/B 是开启采样时的实验结果；
默认关闭保留原版全帧加载、每 tick 最多推进 4 个源帧、按第 0 帧通知循环结束，以及原有 hold 行为。
启用后才应用本文的采样和时钟修正。省电档本身不会自动启用实验。

## 结论

**30 FPS 下可以把本机真实角色包的 CPU 纹理数据减少约一半，同时保持与原版接近的换帧频率。** 可行方案需要按动画时间选择采样帧；只将原始帧索引映射到最近保留帧，会产生额外重复画面，第一版实验因此被淘汰。

这是一份实验原型，不代表已经完成所有角色包、壁纸宿主或长时间低内存运行的验收。当前没有实施 LRU 淘汰，也没有改变资源画质或重新压缩 KTX2 文件。

## 基线与实验条件

- 本地 `main` 已快进到公开 `origin/main`：`c86177c`。实验位于 `codex/fps-texture-experiment`。
- 基线包含 #80 的 `RenderBudget`：通过原有配置设置 30 FPS；两组分辨率预算相同。
- A 组使用从 `c86177c` 导出的原版 `renderer.js`；B 组使用采样原型。基线文件由诊断脚本通过 `git show` 自动生成，服务端、素材与其他网页代码相同。
- Windows、Electron 44.0.0、AMD Radeon 780M / ANGLE D3D11。硬件加速开启。
- 使用持续绘制的 Electron offscreen 窗口（requested 960×600、offscreen 上限 60 FPS），实际 Pixi ticker 受 30 FPS 预算约束。它有 GPU→CPU 画面回读开销，不等同于实际显示器或 Wallpaper Engine/Lively 宿主。
- 复用真实 `WallpaperEngineBridgeHost`、`SpriteForgeAnimator`、背景与 Kurisu KTX2 包；不运行 LLM、ASR、TTS 模型或麦克风。说话事件由本地测试驱动发送。
- 固定根 idle 节点的自动回跳以稳定工作负载；关闭随机闭眼说话分支。**最终对照保留真实说话/转场图的其他路径**，没有强制所有节点返回 idle。
- 依次运行独立 profile。预加载结束或缓存帧数/字节量连续 30 秒不再变化后，执行说话 5 秒、停说 3 秒、首次 smile 5 秒、预热 idle 10 秒。
- 原始帧间隔和资源计数由页面内采集；进程私有内存由 `app.getAppMetrics()` 采集。私有提交量不等于物理工作集。CPU buffer 按 ArrayBuffer 身份去重，不与进程内存再次相加。
- 视频编码在性能采样阶段之后，避免把 MediaRecorder 的额外开销计入表中。

## 最终 30 FPS 对照

测量点：完成相同动作流程、开始视频编码之前。

| 指标 | 原版 A | 时间采样 B |
| --- | ---: | ---: |
| 已加载角色帧 | 6,048 | 3,007 |
| CPU 纹理 buffer | 4,671.1 MiB | 2,324.2 MiB |
| 壁纸页面私有提交 | 5,093.6 MiB | 2,531.1 MiB |
| Electron 全部进程私有提交 | 6,124.7 MiB | 3,619.0 MiB |

CPU 纹理 buffer 减少 **50.2%**，壁纸页面私有提交减少约 **50.3%**，Electron 合计减少约 **40.9%**。进程合计还包含 GPU 进程、浏览器进程等共同开销。

### 流畅性与行为

| 预热 idle，约 10 秒 | 原版 A | 时间采样 B |
| --- | ---: | ---: |
| ticker 次数 / 实际更换纹理次数 | 295 / 295 | 294 / 294 |
| 平均帧间隔 | 33.956 ms | 33.955 ms |
| P95 帧间隔 | 34.9 ms | 34.7 ms |
| P99 帧间隔 | 50.7 ms | 50.3 ms |
| 超过 100 ms 的间隔 | 0 | 0 |
| 请求的显示纹理尚未加载 | 0 | 0 |

两组每次绘制都实际换图，未见采样导致的系统性重复帧。少量约 50 ms 的间隔两组都有；短样本不能证明长期绝无卡顿，亦不能把零点几毫秒的差别当成显著提速。

在完整说话路径中，两组都记录到 `speaking_long` 的闭嘴参考帧 **304**。已与本机 mouth profile 的最小 openness 索引核对；停留分别为约 **1,016.1 ms / 1,016.0 ms**，随后回到 idle。嘴型/锚点仍使用实际显示图片的原始源索引。

首次触发未预热的 smile 时，请求未加载纹理的次数从 **31** 降为 **1**，表明减少待解码帧在这个样例里降低了首次动作等待；这不是所有动作的时延保证。

## 为什么不是简单地“隔一张删一张”

原型保留原始 URL 数组长度、动作时长、嘴型标定索引；新增的采样计划只决定实际解码哪些图片。

1. 复用现有有效 FPS 上限，按各片段的原时长创建均匀时间采样表。开始加载时固定采样档位，纹理策略的档位变化在重载后生效。
2. 预加载循环跳过不在采样计划中的图片；保留首尾、明确的闭嘴参考帧及显式 hold 请求。
3. 播放按时间选择采样表中的源图片，逻辑时间不通过压缩后的数组长度推算。
4. 显示图片和嘴型/锚点共用该图片的原始索引；独立嘴型贴图没有按动画 FPS 抽样。

实验同时暴露了旧播放时钟的两个问题：

- 每 tick 最多推进 4 个源帧，会把 200 源 FPS 的素材在 30 FPS 绘制下放慢。原型移除此限制，仍保留原先的单 tick 100 ms 时间夹限。
- 只在恰好落到第 0 帧时发出循环结束事件，会漏掉跨过 0 的情况。原型按是否跨越循环边界通知。

所以 B 组除加载采样外，还包含这两个必要的时钟边界修正。高源 FPS 转场可能恢复为比旧 30 FPS 行为更快的原定速度；不能声称所有动作与旧实现逐像素、逐时刻完全相同。

## 补充实验与被淘汰的方案

- **仅索引映射版本**：虽然同样省约一半 CPU 数据，但实际换图少于绘制次数，产生额外重复帧；未作为最终方案。
- **60 FPS 补充样本**：时间采样后保留 5,599 帧、约 4,334.8 MiB CPU buffer。预热 idle 平均 ticker 间隔约 17.03 ms；约 10 秒 588 次绘制、472 次换图，符合该 idle 源素材约 47.6 FPS。当前包大部分素材本来就接近 60 FPS，因此 60 档节省空间明显小于 30 档。
- 60 FPS 样本来自较早的固定图测试驱动，不是完整 60 FPS A/B 或最终说话收尾验收；其结果只用于确认 60 档加载规模和绘制节奏。

本轮没有精确测量每项 GPU 显存分配，**不能把 CPU buffer 的 50.2% 降幅直接称为显存下降 50.2%**。这仍未替代未来按字节预算管理长期缓存的工作。

## 代码、验证与复现

- [采样计划与现有预算](../render/web/render_budget.js)
- [加载与播放原型](../render/web/renderer.js)
- [Node 语义测试](../tests/sprite_frame_sampling.test.cjs)
- [pytest 接入](../tests/test_sprite_frame_sampling.py)
- [离屏 A/B 驱动](../electron/tests/fpsTextures.probe.mjs)
- [隔离 Python bridge](../tools/probes/wallpaper_memory_host.py)

推送前验证通过：11 项 Node 采样测试、33 项 pytest 测试（包括图形预算、说话收尾、嘴型、资产版本和采样入口）、TypeScript 编译和 `git diff --check`。新测试覆盖按时间均匀换图、原始时长、跨循环事件、端点/闭嘴保护、锚点索引、显式 hold、低源 FPS 和旧说话循环排除第 0 帧的语义。

推送前审查另修正了两个冷帧边界：等待 hold 目标纹理时继续保留已显示图片的源索引；首次显示采用已加载帧作为临时画面时，锚点索引随实际图片走。定向回归已通过；上表和样片来自这两项小修之前的完整 A/B，没有将定向回归声称为重新测量的 A/B。

在仓库根目录（已有 `.venv` 与 Electron 依赖）：

```powershell
node --test tests/sprite_frame_sampling.test.cjs
.venv/Scripts/python.exe -m pytest -q tests/test_sprite_frame_sampling.py tests/test_render_budget.py tests/test_spriteforge_post_speech_handoff.py tests/test_character_pack_mouth_source.py

$probeRoot = (Get-Location).Path
foreach ($probeMode in @('baseline', 'sampled')) {
  Start-Process -FilePath "$probeRoot/electron/node_modules/electron/dist/electron.exe" `
    -ArgumentList @('tests/fpsTextures.probe.mjs', $probeMode, '30') `
    -WorkingDirectory "$probeRoot/electron" -WindowStyle Hidden -Wait
}
```

输出在 `output/diagnostics/fps-textures/<UTC时间>-<组别>-<FPS>/`。该目录被 Git 忽略，不随源码提交；各轮使用独立 Electron profile。所有实验服务和窗口在结束后退出，不修改系统壁纸设置。

## 试用设置与发布范围

试用需显式设置 `RENDER_TEXTURE_SAMPLING=true`；该实验开关不重复定义 FPS。30 FPS 可选择 `GRAPHICS_PROFILE=power_saving`；标准档为 `GRAPHICS_PROFILE=standard`（60 FPS）。修改 `.env` 后需要重启应用/后端，再打开壁纸；仅重开壁纸不会重新读取 Python 已导入的启动常量。运行期间改变 Wallpaper Engine 上限时，绘制上限会更新，纹理采样档位仍需要重载。关闭开关并重启即可恢复原版路径。

采样不作为默认行为发布。GUI 图形分组不在本次变更内；后续应复用现有桌面配置存储和同一组配置键，避免 GUI 与 `.env` 各自维护一套图形状态。

默认关闭机制新增了开关两侧的加载/时钟回归，并验证 chat URL、web wallpaper URL、bridge-info、Lively iframe 与 macOS Electron 场景的传递；测试包含旧描述符缺少开关时保持关闭。诊断脚本会仅为 sampled 组设置 `RENDER_TEXTURE_SAMPLING=true`。

## 本机最终证据

- [随源码保存的汇总证据](evidence/fps-texture-sampling-2026-09-16.json)
- [A 原版原始数据](../output/diagnostics/fps-textures/2026-09-15T16-10-06.489Z-baseline-30/samples.json)
- [B 时间采样原始数据](../output/diagnostics/fps-textures/2026-09-15T16-13-14.403Z-sampled-30/samples.json)
- [汇总数据](../output/diagnostics/fps-textures/summary.json)
- [A 原版动画样片](../output/diagnostics/fps-textures/2026-09-15T16-10-06.489Z-baseline-30/animation.webm)
- [B 时间采样动画样片](../output/diagnostics/fps-textures/2026-09-15T16-13-14.403Z-sampled-30/animation.webm)

样片顺序为 idle 约 4 秒、说话 4 秒、停说 3 秒、smile 3 秒；无真实音频。需进一步做可见壁纸宿主、实际语音、更多快速动作与 16GB 机器长时间体验检查，才能决定是否进入正式发布。
