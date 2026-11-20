# Windows 安装指南

本文覆盖 Amadeus 在 Windows 上的源码部署，以及每项功能需要额外准备什么。措辞遵循
[分层与构建术语契约](TIER_TAXONOMY.md)：**Tier** 是推荐的安装套餐，**Build** 决定 Torch 来源，
平台只做推荐、不设上限。

Windows 是参考平台，也是唯一具备完整本地语音栈验收记录的平台（NVIDIA + build `cu124`）。

## 给 Agent 的提示清单

协助用户安装时，先问清用户想用哪些功能，再按下表**主动提示**需要用户自己完成的外部准备。
Amadeus 不会替用户下载模型、游戏、第三方 Agent 或付费服务的凭据。启动前再按
[启动前检查清单](#启动前检查清单)逐项确认。

| 用户想要 | 需要提示用户准备 | 说明 |
|---|---|---|
| 任何功能 | Git、[uv](https://docs.astral.sh/uv/)（`winget install astral-sh.uv`）、Node.js 22（参考 `22.21.1`） | Python 3.12 由 uv 管理，不需要系统 Python |
| 文字对话（默认） | DeepSeek API key | 填入 `.env` 的 `DEEPSEEK_API_KEY`，或 Settings → Models |
| **桌面壁纸** | **[Lively Wallpaper](https://github.com/rocksdanister/lively)** 与 Windows App Installer（winget）；源码运行另需 **.NET 8 SDK** | 首次进入壁纸时 Amadeus 会用 winget 安装固定版本 Lively 2.2.1.0；没有 winget 就要用户先装 App Installer。已装的 Microsoft Store 版 Lively 若未运行，需要用户先手动启动一次 |
| 用 Wallpaper Engine 当宿主 | Wallpaper Engine（Steam），设 `AMADEUS_WALLPAPER_HOST=external` | 手工添加本地网页 URL，见下文 |
| 远程语音（推荐） | Fish Audio API key | 推荐 S2.1 + Kurisu 音色，见下文 |
| 本地语音（T4） | NVIDIA 显卡与支持 CUDA 12.4 的驱动；Qwen ASR 与 GPT-SoVITS 资产包 | 资产从[资源中心](https://code-amadeus.github.io/#resources)下载，用 `tools/external_assets.py` 安装 |
| 唤醒词 | SenseVoice 模型；T4（任一 build，含 `local-cpu`） | 唤醒词只用 CPU，不需要 GPU |
| 角色动画 | 角色包（`character-kurisu`）与 `visual-runtime` 包 | 缺包仍可启动，显示「未安装」是健康状态 |
| Live2D 角色 | 用户自备的 `live2dcubismcore.min.js` 与 Live2D 模型 | Core 不随源码分发，需用户按 Live2D 条款自行获取 |
| Browser Work | Playwright 的 Chromium | 运行一次 `uv run --locked --no-sync python -m playwright install chromium` |
| Codex 复杂开发 | 模型凭据（默认复用 `DEEPSEEK_API_KEY`） | App Server 运行时由 `openai-codex` 依赖固定，无需另装；旧的 Direct Codex 需要可直接执行的 `codex.exe` |
| ACP Agent | 外部 Agent 程序（如 DeepSeek Harness、Claude Agent ACP） | 安装时追加 `--extra acp` |
| OpenClaw | OpenClaw 项目目录 | 可选 Provider |
| VN Player | **0xDC00 Agent**（含游戏 Hook 脚本）或 **LunaTranslator** 二选一，以及游戏本体 | Windows 专属实验功能 |
| 本地 LLM | llama.cpp 的 `llama-server` 与 GGUF 模型 | 可选，不是安装前提 |
| AMD 显卡本地语音 | AMD 官方 ROCm 7.2.1 Windows 支持矩阵内的显卡 | 实验，见 [ROCm sidecar 指南](tools/rocm_sidecar/README.md) |

## 平台推荐

| 机器 | 推荐 | 也可安装 |
|---|---|---|
| NVIDIA 显卡 | `T4` + `local-cu124`（参考） | `local-cu128`（实验；RTX 50 系应评估它）、`local-cpu` |
| AMD 显卡（官方支持矩阵内） | `T4` + `local-rocm`（实验） | `local-cpu` |
| 无合适显卡 | `T2` 或 `T3` | `T4` + `local-cpu`（唤醒词可用；本地 TTS/ASR 无实时性承诺） |

`T4` + `cu124` 的参考硬件：CUDA 12.4 兼容的 NVIDIA 显卡，目标 8 GiB 显存；内存 16 GiB 起步，
32 GiB 推荐。

## 前置条件

```powershell
winget install astral-sh.uv
git --version
uv --version     # >= 0.12.8
node --version   # 22.x
```

## 安装

环境目录必须叫 `.venv`，放在仓库根目录。启动器会自动发现 `.venv\Scripts\python.exe`。

```powershell
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus
uv venv .venv --python 3.12
```

每次给出**一条完整命令**：`uv sync` 是精确同步，命令里没写的 extra 会被删除。

```powershell
# T1 core：文字对话、Work、Provider、角色渲染
uv sync --locked
uv run --locked --no-sync python tools\verify_python_environment.py --profile cpu

# T2 voice：远程语音、播放、麦克风、回声消除
uv sync --locked --extra voice
uv run --locked --no-sync python tools\verify_python_environment.py --profile voice

# T3 vad：实时打断
uv sync --locked --extra vad --extra torch-cpu
uv run --locked --no-sync python tools\verify_python_environment.py --profile vad-cpu

# T4 embedded：本地 TTS / ASR / 唤醒词（按显卡选一个 build）
uv sync --locked --extra local-cu124
uv run --locked --no-sync python tools\verify_python_environment.py --profile cu124 --require-cuda-device
```

其他 T4 build：`local-cu128`（`--profile cu128`）、`local-rocm`（`--profile rocm`，再运行 GPU
compute probe）、`local-cpu`（`--profile t4-cpu`）。可选 extra 追加在完整命令后：`rag`（角色知识）、
`acp`（ACP Agent）、`dev`（开发工具）。

构建 Electron 前端（每次更新源码后都要重新 `npm run build`）：

```powershell
cd electron
npm ci
npm run build
cd ..
```

`npm ci` 会安装 Electron 与固定版本的 Pi 运行时。网络受限时可设
`ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`。

## 配置与启动

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，去掉 `# DEEPSEEK_API_KEY=<your-api-key>` 行首的 `# ` 并填入 key。只想先跑文字时，
设 `TTS_BACKEND=disabled`、`WAKE_ENABLED=false`。

### 启动前检查清单

只需完成与用户所选功能对应的项（见「适用于」列）；不适用的项不影响启动。纯文字桌面启动只需要标
「所有启动」与「桌面」的项，headless 后端只需要「所有启动」的项。**自动**项由 Agent 执行并检查输出；
**手动**项由用户完成，Agent 只提示并等待确认：不代填密钥、不代替用户接受许可条款、不在用户同意前提权
安装软件。适用的项未通过时，先按「故障排查」处理再启动。密钥不是启动前提：可以先启动，再在 Settings
中填写。

**自动（Agent 执行）**

| 检查项 | 适用于 | 命令或判定 |
|---|---|---|
| 依赖与目标梯级一致 | 所有启动 | 重跑目标梯级的完整 `uv sync --locked …`，再运行对应的 `verify_python_environment.py --profile …`，必须输出 `environment ok` |
| 前端已构建 | 桌面 | `cd electron; npm ci; npm run build; cd ..`（每次拉取新源码后都要重跑） |
| `.env` 存在 | 所有启动 | 不存在时 `Copy-Item .env.example .env`；已存在则**不要覆盖** |
| 非密钥配置与已装梯级一致 | 所有启动 | 只跑文字：设 `TTS_BACKEND=disabled`、`WAKE_ENABLED=false`；用 `local-cpu`：设 `TTS_DEVICE=cpu` |
| 本地语音资产（T4 本地语音） | 本地 TTS / ASR | `uv run --locked --no-sync python tools\external_assets.py status`，Qwen ASR 与 GPT-SoVITS v3 包应显示已安装；用户下载好的 ZIP 由 Agent 执行 `verify` 与 `install` |
| 日文字典预热（本地 TTS） | 本地 TTS | `uv run --locked --no-sync python -c "import pyopenjtalk; print(pyopenjtalk.g2p('準備完了'))"`，避免首次合成时临时下载 |
| Browser Work 的浏览器 | Browser Work | `uv run --locked --no-sync python -m playwright install chromium` |
| 端口空闲，没有其他 Amadeus 在运行 | 所有启动（`5173` 只影响桌面） | `Get-NetTCPConnection -LocalPort 5173,17777 -State Listen -ErrorAction SilentlyContinue` 无输出即空闲。`5173` 被占用时在 `.env` 设 `AMADEUS_DEV_PORT`；`17777` 被占用时先问用户是否已有 Amadeus 在运行，不要结束未知进程 |
| 壁纸辅助程序（要用壁纸时） | 壁纸 | `dotnet --list-sdks` 含 8.x 后运行 `powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1 -BuildOnly` |

**手动（用户完成，Agent 提示并等待确认）**

| 事项 | 适用于 | 必须手动的原因 |
|---|---|---|
| 填写 `DEEPSEEK_API_KEY`（用到时还有 Fish Audio 等其他密钥） | 用到对应服务时；可启动后再填 | 密钥属于用户。可以启动后在 Settings 中填写（加密存储），或由用户自己编辑 `.env` |
| 从[资源中心](https://code-amadeus.github.io/#resources)下载资产包 | 本地语音、角色动画 | 需要用户访问下载页；Google Drive 分卷要先用 `Assemble-Kurisu.ps1` 合并成完整 ZIP |
| 安装 .NET 8 SDK、NVIDIA 驱动、Windows App Installer（winget） | 壁纸、T4 GPU 构建 | 系统级安装，需要管理员权限或 UAC 确认 |
| 已装但未运行的 Microsoft Store 版 Lively 先手动打开一次 | 壁纸 | 图形界面操作；否则壁纸 setup 会拒绝重复安装 |
| 首次进入壁纸时 Lively 安装过程中的 Windows 权限弹窗 | 壁纸 | 需要用户点击确认 |
| 获取 Live2D Cubism Core（用 Live2D 时） | Live2D | 用户须按 Live2D 条款自行获取，Amadeus 不分发 |
| 安装 0xDC00 Agent 或 LunaTranslator 并完成 hook（用 VN Player 时） | VN Player | 外部程序的图形界面操作 |

完成清单后启动：

```powershell
# 桌面（T1–T4 共用）
.\run_electron_utf8.bat

# 仅后端
uv run --locked --no-sync python -m server.app --port 17777
```

- 默认先打开控制台（`AMADEUS_WINDOWS_STARTUP_MODE=window`），不准备 Lively，也不启动唤醒词。
- 开发端口默认 `5173`。报 `EACCES` / `EADDRINUSE` 时在**仓库根 `.env`** 设
  `AMADEUS_DEV_PORT=15173` 等可用端口，再完整重启启动器。可用
  `netsh interface ipv4 show excludedportrange protocol=tcp` 检查系统保留端口。
- 启动型设置修改后点 **Restart backend to apply**，并重新打开已有的角色与壁纸窗口。
- 配置优先级：父进程环境变量 > 桌面设置 > `.env` > 代码默认值。详见 [config/README.md](config/README.md)。

## 功能逐项

### 文字对话与 Work

- **Main Chat**：默认远程 DeepSeek（`deepseek-v4-flash`）。
- **Pi**（日常任务，默认）：随 `npm ci` 安装，默认复用 `DEEPSEEK_API_KEY`。见 [Pi 配置](docs/pi-rpc-provider.md)。
- **Codex App Server**（复杂开发）：运行时由 Python 依赖固定，不读取 `~/.codex/config.toml`。
  旧的 Direct Codex 需设 `DIRECT_CODEX_PROVIDER_ENABLED=true` 并指向真实的 `codex.exe`，
  不要依赖 WindowsApps 别名；两者不要同时开启。
- **Browser**：受管页面操作，需要先安装 Playwright Chromium（见提示清单）。
- **ACP Agent**（实验）：见 [ACP 说明](docs/acp_provider.md)。
- **MCP**：Settings → Providers 中添加，只授予兼容的 Work Provider（Codex、Pi、ACP），Main Chat
  不直接调用 MCP 工具。见 [MCP 环境与审批](docs/mcp_environment.md)。

### 语音

- **远程 TTS**（`T2`）：推荐 Fish Audio，在 Settings → Voice 选择 Fish Audio，或写入 `.env`：

  ```dotenv
  TTS_BACKEND=fish_audio
  FISH_TTS_API_KEY=<your-fish-api-key>
  FISH_TTS_MODEL=s2.1-pro-free
  FISH_TTS_REFERENCE_ID=b450b19370434173b121446057622e9b
  FISH_TTS_LATENCY=balanced
  ```

  MiMo 与 OpenAI 兼容端点同样支持。见 [Fish Audio 说明](docs/fish_audio_websocket.md)。
- **远程 ASR**（`T2`）：OpenAI 兼容端点，显式选择，不会静默回退。
- **实时打断**（`T3`）：Silero VAD + 回声消除；没装 `vad` 时用能量检测判断说话结束。
- **本地 ASR / TTS**（`T4`）：先安装资产包，再在 Settings → Voice 选择内嵌后端：

  ```powershell
  uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-asr-qwen3-0.6b.zip
  uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip
  uv run --locked --no-sync python tools\external_assets.py status
  ```

  安装前可先用 `verify` 子命令校验。内嵌 GPT-SoVITS 支持 v1、v2、v2Pro、v2ProPlus、v3。
  NVIDIA 加速（CUDA Graph、FlashAttention、nested KV）的推荐设置见 README 的
  「推荐的 NVIDIA 语音加速配置」。用 `local-cpu` 时需设 `TTS_DEVICE=cpu`，因为 `auto`
  在 Windows 上固定选 `cuda:0`。
- **唤醒词**（`T4`）：Settings → Voice → Wake recognition，默认 SenseVoice。唤醒监听跟随壁纸启动，
  控制台模式下不会自动开始。见 [唤醒词设置](docs/wake_word_setup.md)。

### 角色

- **角色包**：从资源中心下载后用 `tools\external_assets.py install` 安装；升级时先停后端、关窗口，
  再加 `--overwrite`。见 [外部资产包](docs/external_asset_bundles.md)。
- **Live2D**（实验）：Settings → Characters → Appearance 选择 Live2D，指定用户自备的 Cubism Core
  与模型。见 [角色形象与 Live2D](docs/live2d_character_visuals.md)。
- **角色管理**：见 [本地角色管理](docs/character-management.md)。
- **角色知识 RAG**（可选，默认关闭）：追加 `--extra rag`，见 [角色知识](docs/character_rag.md)。

### 桌面壁纸（推荐 Lively Wallpaper）

两种进入方式：侧栏点 **Wallpaper**，或在 Settings → General → Startup 选 **Enter wallpaper directly**
（完全退出再打开后生效）。

首次进入壁纸时 Amadeus 会：

1. 检查 Lively；缺失时用 winget 安装固定版本 **Lively 2.2.1.0**（需要 Windows App Installer）。
2. 首次构建壁纸辅助程序（源码运行需要 **.NET 8 SDK**）。
3. 挂载 Amadeus 场景；从托盘选 **Quit Amadeus** 时恢复原壁纸。

也可以预先手动准备：

```powershell
powershell -NoProfile -File scripts\setup_windows_wallpaper.ps1
```

- 已安装且正在运行的 Microsoft Store 版 Lively 可直接复用；未运行的 Store 版需要用户先手动启动一次，
  setup 不会重复安装。
- 启动参数 `--no-wallpaper` / `AMADEUS_WALLPAPER=0` 强制普通窗口，`--wallpaper` /
  `AMADEUS_WALLPAPER=1` 强制进入壁纸，均优先于 GUI 设置。
- 异常退出后的手动恢复（先关闭 Amadeus）：
  `build\windows-wallpaper\host\Amadeus.Wallpaper.exe recover`
- 输入框与异常退出的已知问题见 [#185](https://github.com/Code-Amadeus/Amadeus/issues/185)。

**用 Wallpaper Engine 或手工管理 Lively**：设 `AMADEUS_WALLPAPER_HOST=external`，启动 Amadeus 后把下面的
URL 添加到宿主（Lively 推荐 WebView2），再点 **Wallpaper**：

```text
http://127.0.0.1:17777/wallpaper/lively/index.html
```

不要手工写死 `17778` 或 `17797` 端口。完整说明见 [Windows 壁纸生命周期](docs/windows_wallpaper_lifecycle.md)
与 [Lively 入口](wallpaper/lively/README.md)。

### VN Player（实验）

Windows 专属。取文方式二选一：

- **0xDC00 Agent**：从[官方发布页](https://github.com/0xDC00/agent/releases)下载并完整解压（保留
  `data` 目录），用 **update scripts** 同步[脚本库](https://github.com/0xDC00/scripts)，在 Agent 设置中
  开启 WebSocket `127.0.0.1:9001`。
- **LunaTranslator**：按[官方说明](https://docs.lunatranslator.org/zh/README.html)下载，HOOK 模式选中游戏
  文本流并开启网络服务；在 VN Player 中填写 `ws://127.0.0.1:<端口>/api/ws/text/origin`。

之后在 **VN Player → 添加游戏** 中配置并点 **保存并测试取文**。完整步骤见 README 的「VN Player 安装与
首次取文」与 [VN 文本来源](docs/vn-text-sources.md)。

### 其他

- **本地 LLM**（可选）：设 `LLM_PROVIDER=local` 并配置 `llama-server` 与 GGUF，再运行
  `.\start_llm_server.bat`。任何 profile 都不会在 DeepSeek 失败后自动接管。
- **插件**（实验）：Settings → General → Manage plugins，见 [扩展 Host profile](docs/extension_host_profile_v0.1.md)。
- **AUIP 应用会话**：动作判定模型通过 `AUIP_ACTION_PROVIDER` / `AUIP_ACTION_MODEL` 配置；未配置时 Chat 与
  Settings 仍可启动，应用动作保持关闭。见 [AUIP 应用会话](docs/auip_application_sessions.md)。
- **图形性能**：Settings → 图形与性能；0.17 推荐设置见 README 的「0.17 推荐图形设置与生效检查」。
- **VTube Studio**：默认关闭的兼容旁路，`VTS_ENABLED=true` 开启。

## 不提供的内容

- 安装包、自动更新、代码签名。当前只能从源码启动。
- Windows 登录后自动启动 Amadeus。
- Docker 部署。
- Claude CLI Provider（已确定为后续主线 Provider，目前不可用）。

## 故障排查

| 现象 | 成因与处理 |
|---|---|
| `Required uv version '>=0.12.8'` | 升级 uv。 |
| `uv sync` 后需要的包被删了 | `uv sync` 是精确同步，始终给完整命令（含可选 extra 与 `dev`）。 |
| 拉新源码后校验器报版本不符 | 按目标 Tier 与 Build 重跑完整的 `uv sync`。 |
| `vad` 校验报 `not a locked build` | torch 不来自任何 build extra。重跑带 `--extra torch-cpu` 或 `--extra local-<build>` 的命令。 |
| `--require-cuda-device` 失败 | 检查 NVIDIA 驱动是否支持 CUDA 12.4（`cu124`）或 12.8（`cu128`）。 |
| 首次进入壁纸失败，提示需要 winget | 安装 Windows App Installer 后重试；也可先运行 `scripts\setup_windows_wallpaper.ps1`。 |
| 壁纸辅助程序构建失败 | 安装 .NET 8 SDK，再运行 `scripts\setup_windows_wallpaper.ps1 -BuildOnly -Rebuild`。 |
| setup 拒绝安装 Lively | 已有未运行的 Store 版 Lively，先手动启动它一次。 |
| 退出后桌面壁纸没恢复 | 关闭 Amadeus 后运行 `build\windows-wallpaper\host\Amadeus.Wallpaper.exe recover`。 |
| Browser Work 报找不到浏览器 | 运行 `uv run --locked --no-sync python -m playwright install chromium`。 |
| `local-cpu` 下本地 TTS 报 CUDA 错误 | 设 `TTS_DEVICE=cpu`。 |
| 改了源码但桌面端没变化 | `cd electron && npm run build`。 |
| 不要复制或重命名 `.venv` | 入口脚本内嵌原解释器绝对路径，请在最终路径重建。 |
