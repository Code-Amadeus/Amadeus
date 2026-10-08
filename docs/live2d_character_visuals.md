# 角色形象与 Live2D 实验支持

日期：2026-10-08。分支：`codex/live2d-character-renderer`。默认仍为 Sprite；Live2D 是显式选择的实验性美术后端。原生 Main Host render、真实 Host 壁纸页面、统一人物取景、Sprite 往返、连接恢复及 SDK 错误/加载竞态均已有实测。桌面宿主支持范围见下方限制。

## 使用

从侧栏打开 **Settings / 设置 → Character visuals / 角色形象**。形象管理是设置内的独立分区；全局侧栏只保留功能窗口、后端和设置入口。

1. 使用 **添加 Live2D 模型** 选择本地 `.model3.json`。也可以输入绝对路径，再点 **检查并添加**。检查读取模型注册的表情、LipSync 声明和资源引用，不复制模型包。
2. 使用 **选择 Core** 指定已有的 `live2dcubismcore.min.js`。留空时只尝试已有的本地默认文件，不下载 SDK。Core 不随 public 源码分发，参见 [Core exclusion notice](../LICENSES/Live2D-Cubism-Core-NOTICE.md)。
3. 给形象配置命名，在 **形象渲染方式** 中选择 Live2D。同一模型可以通过 **添加为新配置** 建立多份独立映射。更换模型会保留原 `profile_id` 和两个布局，重新生成模型能力对应的映射及嘴型设置。
4. 核对 **情绪映射**。下拉列表来自模型注册的表达名称；空映射是明确的默认姿态，不是假定模型有该表情。Work 等标签的近似映射会标注。没有 LipSync 声明时才填写手动参数 ID，运行时核对实际模型参数。
5. 点 **打开预览**。独立实例真正 ready 后才能测试注册表情、现有情绪标签和嘴型。预览使用当前草稿，测试不发送全局 speaking/mouth，也不改变正在运行的聊天。映射或布局修改后需刷新预览；关闭、换配置或离开设置分区会销毁实例。
6. 分别调整 **Render / 渲染** 与 **Wallpaper / 壁纸** 的缩放和水平、垂直位置。缩放范围为 0.1–5，位置为视口比例 −1–1。预览可以选择任一布局；真实 CRT 多边形裁剪、字幕和 Canvas 仍应在壁纸画面核对。
7. 点 **保存并应用**，再从侧栏打开 Render 或 Wallpaper。**已应用形象的状态** 显示展示面回执；已保存配置不等于模型 ready。“尚无展示面回执”也不等于加载成功。

**重新加载模型** 使用已保存配置，保留本页草稿。切换 Core 文件需要关闭并重新打开对应 Render/Wallpaper，让该页面重新建立 SDK 上下文；模型重载按钮不能替换已加载的全局 Core。预览重建使用独立页面，因此可以直接检查新 Core。

选择 Sprite 并保存即可回切既有行为图。损坏或缺失的模型/Core 会给出错误，不会默默改成别的形象，也不会把“已发加载命令”当作就绪。损坏的持久 JSON 会保留原文件并提示修复。

## 美术资源与人格

`profile_id` 是稳定的美术配置标识，`kind` 当前只能是 `live2d`。它不使用 `character_id`，不创建或切换会话、人格、Work 身份。未来的人格配置可以引用美术 `profile_id`；本分支没有加入自动人格关联。

Host 的 [VisualProfileStore](../render/visual_profile.py) 是持久配置 owner。默认文件位于 Electron 用户数据目录下的 `visual_profiles.json`；可用 `AMADEUS_VISUAL_PROFILES_PATH` 指定另一位置。模型入口、映射、嘴型增益/平滑/参数、双展示面布局与 Core 路径保存于该 store。模型能力在配置加载/保存、显式检查/重载和 Runtime 资源重建时从源文件检查；状态回执读取 Host 已检查的能力缓存，不重复解析模型文件。不持久化另一份能力清单，也不写回原始 expression 文件或 renderer localStorage。壁纸 AssetServer 按整个 `/visual-model/` 命名空间替换所选模型文件；切换模型、回到 Sprite 或加载失败时撤销旧映射，不影响其他挂载。

初次资产定位发现导出的 1920×1980 canvas 不等于可见人物范围：所测 mesh 偏右且越过 canvas 底部。旧版胸腹截断资产与包含腰部、衣袋的较新版资产也不同。当前 SDK adapter 初次加载时测量实际可见 art bounds，包含模型原始偏移与变换，再统一按视口等比 fit、居中并底部对齐。profile 的 scale/x/y 在此基准上微调；取景基准不每帧随动作变化。其他 Live2D 模型沿用相同规则，因此整体占用比例与留白策略一致；不同身段、长宽比不能保证人脸绝对大小相同。

本次试用 profile 使用 render scale 0.96、wallpaper scale 0.92、x/y 均为 0。这些是可编辑的本次配置，不是某模型专用的运行时默认；新配置仍从统一 fit 基准开始。旧的偏移补偿值也不作为正式默认。

## 架构边界

| 层 | 责任 |
| --- | --- |
| 既有 parser、ChatRuntime、TTS/StreamPlayer | 解释现有标签、安排句子播放与幅度信号。Live2D 不增加 LLM 调用、分类器或第二份语音分析。 |
| [CharacterPresentationCoordinator](../server/character_presentation.py) | 继续拥有 utterance/ambient 来源优先级、释放和 handoff。Sprite 路由在广播前解析一次，两个展示面不独立抽签。 |
| [VisualHandler](../server/handlers/visual_handler.py) 与 store | 选择美术后端、校验/持久化配置、下发当前选择及收取真实状态。保存和重连恢复当前 claim/playback 状态，不重放已结束语句。 |
| [model_character.js](../render/web/model_character.js) | 一个显式模型 adapter 构造边界；当前只构造 Live2D，没有注册表或插件平台。 |
| [Live2DCharacter](../render/web/live2d_character.js) | SDK 加载、表达、模型参数、嘴型更新顺序、原始人物取景、RenderTexture 和资源生命周期。SDK 细节留在此层。 |
| render / wallpaper 场景 | 提供视口、mask、图层、颜色效果、时钟和可见性。Cubism 先画到视口大小的 RenderTexture，再由普通 Pixi 层接受裁剪。 |
| [SettingsPage](../electron/src/renderer/components/SettingsPage.tsx) 与 [形象管理组件](../electron/src/renderer/components/CharacterVisualsPage.tsx) | 草稿与 applied 分开；独立预览等待 ready 握手并检查消息来源。离开分区按条件卸载，不保留隐藏预览。 |

语义事件使用 `render.character_config`、`render.character_intent`、`render.character_release`；真正的 Sprite 资产图事件 `render.spriteforge_graph` 保留专用命名。表达名称和 Cubism 参数 ID 不与 VTS 的 EXPR/PARAM/HOTKEY 命名空间混用。

**公开协议迁移：** `render.spriteforge_intent` 与 `render.spriteforge_release` 分别改名为 `render.character_intent` 与 `render.character_release`。这项共享协议变更也影响只使用 Sprite 的外部订阅者；“Live2D 默认关闭”不代表旧订阅者自动兼容。Host 与随附前端应一同升级，外部订阅者需更新事件名称并按 payload 的 backend 处理人物表现。现有来源优先级、释放和播放语义不变，不发送永久并行的两套事件。旧 Expression 入口由 Settings → 角色形象替代，新 `visual.*` 控制及形象配置文件不迁移会话或人格。没有形象配置时仍默认 Sprite；回切 Sprite 不会恢复旧协议名称。迁移约定与合并前的产品决定跟踪于 [Issue #165](https://github.com/Code-Amadeus/Amadeus/issues/165)。

状态为 `loading/ready/error/unloaded`，含 `profile_id`、Host 实例 `runtime_id` 和配置 `revision`。状态是观察，不是新的执行或选择权限。Host 拒绝旧实例/旧选择回执；Electron 只转发自己 iframe 的 render 状态。掉线时关闭该 iframe 的 speaking/mouth，已经完成加载的 iframe 在连接恢复时请求 `render.ready`，由 Host 重放当前选择。

Live2D 模式禁用会隐藏前景、换成旧人物素材的活动场景及其专属动作音效；背景、环境、字幕与 Work Canvas 保留。停用场景时释放其自有纹理缓存，既有加载令牌阻止晚完成的加载重新驻留；共享背景纹理不随场景销毁。回切 Sprite 后重新加载所需场景资源并恢复原场景能力。

## 后续 E-mote

尚未实现 E-mote，也不展示占位选项。未来 adapter 可以复用工厂入口与已有 configure、intent/release、speaking/mouth、viewport、pause、status、unload/destroy 边界；必要的 SDK 加载、参数及绘制细节由新 adapter 自己拥有。只在实际 SDK 需求得到证明后扩展相应配置校验，不建设通用插件平台，不把 E-mote 伪装成 SpriteForge 节点。

## 已取得证据与限制

以下目录是本地忽略输出，原始模型与 Core 仅被引用，没有复制或提交。下表记录初次实现 `c071769` 的验收；后续审查修订的完整 CI 状态以 [PR #164](https://github.com/Code-Amadeus/Amadeus/pull/164) 当前提交为准。审查修订另已通过 9 个相关测试文件的 94 项回归、全库 Ruff、两项架构检查、标准 `npm run build` 和标准无模型桌面启动 smoke（11 项检查，含正常退出）。

| 验证层 | 观察到的结果 | 范围 |
| --- | --- | --- |
| 相关 Python 契约测试 | 最终 29 个相关文件：210 passed，1 skipped（Windows 上不适用的 non-Windows import 回归）。 | Host、来源/释放、模型配置与现有 Sprite/TTS 语义边界；不代替像素或原生窗口验收。 |
| Electron 契约与构建 | 完整 Electron suite 262/262，TypeScript 与生产 build 通过；覆盖草稿保留、预览隔离、来源校验及断线恢复。 | 用户导航、子页切换与预览卸载另由真实 GUI smoke 覆盖。 |
| `runtime/live2d-p0` | 本地 Core 5.1.0、Pixi 7.4.3、vendor adapter 0.4.0；四个表情像素/参数、口型 0→0.8→0、RenderTexture mask 与销毁。 | 独立真实模型关口，直接 Cubism mesh 不能证明普通 Pixi mask 正确。 |
| `runtime/live2d-surfaces` | 生产 render/wallpaper 页面与 WallpaperBridge 均 ready；Smile 参数 >0.9；固定 PCM 口型约 .65/.02/.65，尾部闭嘴及 Work 恢复；polygon 外抽样不透明点 97→0、内部保留 7450；resize/pause、5 次 model/ticker/RT 释放检查。 | render 通过直接 dispatch 接入既有 StreamPlayer 固定 PCM；不是完整 Main Host GUI/LLM/provider/音频设备端到端验收。 |
| `output/diagnostics/character-visuals` | 实际 Sidebar→Settings→角色形象；9 个检查通过，errors=[]；真实本地 Core/model/生产 preview；PNG 有效性与尺寸检查。 | 控件 RPC 是测试夹具。不能据此宣称完整 Host、真实聊天 TTS 或默认壁纸宿主已验收。 |
| `runtime/live2d-host/native-report.json` | 真实编译 Electron/Main Host 的 12 项检查通过：parser/controller/PCM 双面参数、Work 恢复、Sprite 真实帧驻留与回切释放、断线闭嘴、同 iframe 新 Host 实例恢复及双面 ready、stop 后 unloaded/旧回执拒绝；原生 render/CRT 左右 alpha 留白差分别为 3/1px。 | 壁纸像素与回执来自真实 Host SSE 的完整页面；不等同 Lively 桌面挂载。 |
| `runtime/live2d-loader/report.json` | 9 项检查通过：Sprite 不请求 SDK；无效 Core 返回 HTTP 200/空 namespace 后修正恢复；坏模型清理后恢复；更换 Core 明确要求重开；延迟加载→Sprite→最新模型只有最终 revision ready；另一个真实短身模型在 360×720 与 900×900 视口、同一 scale 0.9 下占用比例均为 0.9，居中并底部对齐。 | 真实 SDK 的错误恢复、竞态与跨模型取景；不代替完整 Host 或桌面宿主验收。 |
| `runtime/live2d-lively/report.json` | success=true；Lively 2.2.1.0/WebView2 经正常 production helper 实际 mounted 并回报所选 profile ready；取得 5 张 3842×2162 原生 JPEG（default、smile-closed、smile-open、thinking、work-activity）。真实 parser/controller 与固定 PCM 驱动 Smile、开闭嘴、结束恢复 Thinking，Work 活动保持 Live2D。 | 已验证 Windows 默认 Lively 的完整壁纸页面；截图播放条件见下文，不扩展为所有平台、模型动作或长期性能结论。 |
| `runtime/live2d-asset-comparison` | moc/texture 与较新内部备份相同；旧资产截断范围不同；canvas/可见 mesh 取景比较。 | 本地资产定位证据；旧补偿值不是正式布局默认。 |

该模型自带 physics 是旧格式，且有 4 个 motion 的格式/metadata 告警。已验证基本模型、四表情与幅度嘴型；不宣称所有 physics/motion 都能加载或正常运动。

P0/双页面探针及 GUI 夹具使用 SwiftShader。原生 Main Host 的 WebGL renderer 则实测为 D3D11/RTX 4070 Ti SUPER，GPU feature status 为 enabled；取得了原生硬件环境与实际 renderer 的证据。120 个 ticker 帧间隔样本的 median 约 14ms、p95 约 21ms；短窗口进程树 RSS 未见明显增长。ticker 间隔不是 GPU 绘制耗时，以上也不是 VRAM、长时负载或泄漏基准。有限次数释放检查不支持长期“无泄漏”结论。

测试 Host 已正常 shutdown，remaining_owned_host_processes=[]；helper 已退出，测试 journal 不存在，临时认证 environment 文件已删除。真实原生 Electron render 已接通 Main Host、parser/controller、固定 PCM 与状态回执；真实 Host SSE 的完整壁纸页面也已显示新 fit 基准下的模型。隔离 Electron Slice/WorkerW 验证只覆盖 Canvas、keyboard、Companion 叠层，该 Slice 不包含 Live2D，不能当作完整 Live2D 桌面挂载证据。默认 Lively 的正常生产流程已挂载完整页面并回报正确 profile ready，真实原生截图已验证模型、Smile、开闭嘴、Thinking 及 Work 活动保持 Live2D。验收截图前临时将 Lively 的 AppFullscreenPause 从 0 改为 Ignore=1，使宿主在测试窗口存在时继续播放；finally 已恢复 0。这是本次验收播放条件，没有修改产品时钟或布局。本轮已按用户授权清除旧壁纸缓存；旧画面恢复要求取消，未恢复旧画面。外部 Wallpaper Engine、其他平台及 Companion 专用 Live2D 不自动纳入支持声明。

## 本地复核

在已安装项目 Python/Playwright 依赖的环境中，把占位参数替换为自己的本地资源：

```powershell
python tools/probes/live2d_model_probe.py --model "<absolute model3.json path>" --core "<absolute live2dcubismcore.min.js path>" --output runtime/live2d-p0
python tools/probes/live2d_surface_probe.py --model "<absolute model3.json path>" --core "<absolute live2dcubismcore.min.js path>" --asset-root "<installed assets directory>" --output runtime/live2d-surfaces
python tools/probes/live2d_loader_probe.py --model "<absolute model3.json path>" --core "<absolute live2dcubismcore.min.js path>" --comparison-model "<another existing local model3.json path>" --output runtime/live2d-loader
```

`--comparison-model` 使用另一个现有的本地模型；省略时只执行错误恢复与竞态检查。

原生 Host 验证由 [live2d_validation_host.py](../tools/probes/live2d_validation_host.py) 调用真实 `server.app.bootstrap`，仅提供固定的现有 parser/controller/PCM 输入；[live2d_native_probe.py](../tools/probes/live2d_native_probe.py) 驱动隔离的编译 Electron/CDP 与真实 Host RPC/事件。它需要先建立独立的测试 Host、用户数据与 CDP 环境，不作为普通应用启动命令，也不接管已有 Lively 会话。

GUI smoke 从 `electron` 目录运行，只创建自己的测试用户数据目录与控件夹具；使用同一个生产 preview。PowerShell 的 GUI 程序启动应等待进程完成后读取报告：

```powershell
Start-Process -FilePath ".\node_modules\electron\dist\electron.exe" -ArgumentList @("tests/characterVisuals.smoke.mjs", "--model", '"<absolute model3.json path>"', "--core", '"<absolute live2dcubismcore.min.js path>"') -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -Wait
Get-Content "..\output\diagnostics\character-visuals\report.json"
```

GUI source/test fixture 位于 [characterVisuals.test.mjs](../electron/tests/characterVisuals.test.mjs)、[characterVisuals.smoke.mjs](../electron/tests/characterVisuals.smoke.mjs)。原设计与验收目标保留在 [支持分支提案](live2d_support_branch_proposal_2026-10-08.md)。
