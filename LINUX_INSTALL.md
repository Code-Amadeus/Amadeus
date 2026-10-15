# Linux 安装指南

本文覆盖 Amadeus 在 Linux 上的源码部署，以及每项功能需要额外准备什么。措辞遵循
[分层与构建术语契约](TIER_TAXONOMY.md)：**Tier** 是推荐的安装套餐，**Build** 决定 Torch 来源，
平台只做推荐、不设上限。

Linux 源码部署已在实机上跑通；CI 在 Ubuntu 24.04 上覆盖 T1 + dev 锁定安装、T2 Voice 源码构建、
Electron 构建，以及 `cu128` 候选安装（`local-cpu` 候选任务已加入工作流）。CI **不覆盖** Electron 界面、真实音频设备、
VAD 与本地模型推理、Wayland 会话。社区报告过 Arch Linux / Wayland 下的桌面与角色渲染结果，
不代表所有发行版与桌面环境。环境记录与已知问题见 [Linux 跟踪 issue #64](https://github.com/Code-Amadeus/Amadeus/issues/64)。

## 给 Agent 的提示清单

协助用户安装时，先问清用户想用哪些功能，再按下表**主动提示**需要用户自己完成的外部准备。
标「不支持」的功能请直接告知用户 Linux 上没有，不要尝试变通安装。启动前再按
[启动前检查清单](#启动前检查清单)逐项确认。

| 用户想要 | 需要提示用户准备 | 说明 |
|---|---|---|
| 任何功能 | Git、[uv](https://docs.astral.sh/uv/)（>= 0.12.8）、Node.js 22（参考 `22.21.1`）、图形桌面会话（运行 Electron 时） | Python 3.12 由 uv 管理 |
| 文字对话（默认） | DeepSeek API key | 填入 `.env` 的 `DEEPSEEK_API_KEY`，或 Settings → Models |
| 语音（T2 及以上） | 系统包：`build-essential pkg-config portaudio19-dev`（Ubuntu 名称；其他发行版用对应包名） | PyAudio 与回声消除模块需要从源码编译 |
| 本地模型（T4） | 再加 `swig meson ninja-build`（CI 使用的名称）；NVIDIA 显卡与支持 CUDA 12.8 的驱动，或改用 `local-cpu` | `cu128` 为实验配置，GPU 模型推理尚无验收记录 |
| 远程语音（推荐） | Fish Audio API key | 推荐 S2.1 + Kurisu 音色 |
| 本地 ASR / TTS | Qwen ASR 与 GPT-SoVITS 资产包 | 从[资源中心](https://code-amadeus.github.io/#resources)下载，用 `tools/external_assets.py` 安装 |
| 唤醒词 | SenseVoice 模型；T4（任一 build，含 `local-cpu`） | 只用 CPU；自动启动依赖壁纸启动，见下文 |
| 角色动画 | 角色包（`character-kurisu`）与 `visual-runtime` 包 | 缺包仍可启动 |
| Live2D 角色 | 用户自备的 `live2dcubismcore.min.js` 与模型 | Core 不随源码分发 |
| Browser Work | Playwright Chromium 及其系统依赖 | 浏览器由 Agent 下载；系统依赖 `playwright install-deps chromium` 需要 sudo，由用户执行或授权 |
| Codex 复杂开发 | 模型凭据（默认复用 `DEEPSEEK_API_KEY`） | App Server 运行时由 `openai-codex` 依赖固定；旧的 Direct Codex 需要 `codex` 可执行文件 |
| ACP Agent | 外部 Agent 程序 | 安装时追加 `--extra acp` |
| 本地 LLM | `llama-server` 与 GGUF 模型 | 可选 |
| **桌面壁纸** | **不支持** | Linux 没有 Lively / Wallpaper Engine 这类桌面宿主集成，壁纸与 compositor 集成未验收 |
| **VN Player** | **不支持** | Windows 专属 |
| **AMD ROCm 本地语音** | **不支持** | `local-rocm` 只锁定了 Windows 轮子 |
| 参考 `cu124` 配置 | **不支持** | `local-cu124` 只路由 Windows 的 CUDA 12.4 轮子；Linux 用 `local-cu128` |

## 平台推荐

| 机器 | 推荐 | 也可安装 |
|---|---|---|
| x86_64 + NVIDIA | `T4` + `local-cu128`（实验） | `local-cpu` |
| 无合适显卡 | `T2` 或 `T3` | `T4` + `local-cpu`（唤醒词可用；本地 TTS/ASR 无实时性承诺） |

## 安装

### 前置条件（Ubuntu 24.04 示例）

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y git build-essential pkg-config portaudio19-dev
# 只有 T4 需要：
sudo apt-get install --no-install-recommends -y swig meson ninja-build

uv --version     # >= 0.12.8
node --version   # 22.x
```

### Python 环境

环境目录必须叫 `.venv`，放在仓库根目录。启动器会自动发现 `.venv/bin/python3`。

```bash
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus
uv venv .venv --python 3.12.10
```

每次给出**一条完整命令**：`uv sync` 是精确同步，命令里没写的 extra 会被删除。

```bash
# T1 core：文字对话、Work、Provider、角色渲染（不需要 GPU，也不需要语音系统包）
uv sync --locked
uv run --locked --no-sync python tools/verify_python_environment.py --profile cpu

# T2 voice：远程语音、播放、麦克风、回声消除
uv sync --locked --extra voice
uv run --locked --no-sync python tools/verify_python_environment.py --profile voice

# T3 vad：实时打断
uv sync --locked --extra vad --extra torch-cpu
uv run --locked --no-sync python tools/verify_python_environment.py --profile vad-cpu

# T4 embedded：NVIDIA（实验）
uv sync --locked --extra local-cu128
uv run --locked --no-sync python tools/verify_python_environment.py --profile cu128 --require-cuda-device

# T4 embedded：纯 CPU
uv sync --locked --extra local-cpu
uv run --locked --no-sync python tools/verify_python_environment.py --profile t4-cpu
```

可选 extra 追加在完整命令后：`rag`（角色知识）、`acp`（ACP Agent）、`dev`（开发工具）。

Linux 的回声消除模块使用仓库内基于官方 `aec-audio-processing==1.0.1` sdist 的源码，并强制使用
bundled Abseil，避免选中新版系统 Abseil 导致构建失败；不会修改系统 Abseil。见
[AEC provenance](vendor/aec-audio-processing.PROVENANCE.md)。

### Electron 前端

在图形桌面会话中构建（每次更新源码后都要重新 `npm run build`）：

```bash
cd electron
npm ci
npm run build
cd ..
```

`npm ci` 会安装 Electron 与固定版本的 Pi 运行时。

## 配置与启动

```bash
cp .env.example .env
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
| 前端已构建 | 桌面 | `cd electron && npm ci && npm run build && cd ..`（每次拉取新源码后都要重跑） |
| `.env` 存在 | 所有启动 | 不存在时 `cp .env.example .env`；已存在则**不要覆盖** |
| 非密钥配置与已装梯级一致 | 所有启动 | 只跑文字：设 `TTS_BACKEND=disabled`、`WAKE_ENABLED=false`；用 `local-cpu`：设 `TTS_DEVICE=cpu` |
| 本地语音资产（T4 本地语音） | 本地 TTS / ASR | `uv run --locked --no-sync python tools/external_assets.py status`；用户下载好的 ZIP 由 Agent 执行 `verify` 与 `install` |
| 日文字典预热（本地 TTS） | 本地 TTS | `uv run --locked --no-sync python -c "import pyopenjtalk; print(pyopenjtalk.g2p('準備完了'))"` |
| Browser Work 的浏览器 | Browser Work | `uv run --locked --no-sync python -m playwright install chromium`（只下载浏览器，不装系统依赖） |
| 端口空闲，没有其他 Amadeus 在运行 | 所有启动（`5173` 只影响桌面） | `ss -ltn '( sport = :5173 or sport = :17777 )'` 只有表头即空闲。`5173` 被占用时在 `.env` 设 `AMADEUS_DEV_PORT`；`17777` 被占用时先问用户，不要结束未知进程 |
| 有图形会话（启动桌面端时） | 桌面 | `echo "$WAYLAND_DISPLAY$DISPLAY"` 非空；为空时只能运行 headless 后端 |

**手动（用户完成，Agent 提示并等待确认）**

| 事项 | 适用于 | 必须手动的原因 |
|---|---|---|
| 填写 `DEEPSEEK_API_KEY`（用到时还有 Fish Audio 等其他密钥） | 用到对应服务时；可启动后再填 | 密钥属于用户。可以启动后在 Settings 中填写，或由用户自己编辑 `.env` |
| 安装系统包（`apt-get install …`）与 Playwright 系统依赖（`sudo .venv/bin/python -m playwright install-deps chromium`） | 语音（T2 及以上）、T4、Browser Work | 需要 sudo。用户自己执行，或明确授权 Agent 执行 |
| 安装或升级 NVIDIA 驱动（用 `local-cu128` 时） | `local-cu128` | 系统级安装，需要 root，可能需要重启 |
| 从[资源中心](https://code-amadeus.github.io/#resources)下载资产包 | 本地语音、角色动画 | 需要用户访问下载页 |
| 获取 Live2D Cubism Core（用 Live2D 时） | Live2D | 用户须按 Live2D 条款自行获取，Amadeus 不分发 |

完成清单后启动：

```bash
# 桌面
cd electron && npm run electron:dev

# 仅后端（headless）
uv run --locked --no-sync python -m server.app --port 17777
```

- 开发端口默认 `5173`。报 `EACCES` / `EADDRINUSE` 时在**仓库根 `.env`** 设 `AMADEUS_DEV_PORT`，再完整重启。
- 启动型设置修改后点 **Restart backend to apply**。
- 配置优先级：父进程环境变量 > 桌面设置 > `.env` > 代码默认值。详见 [config/README.md](config/README.md)。

## 功能逐项

### 文字对话与 Work

- **Main Chat**：默认远程 DeepSeek（`deepseek-v4-flash`）。
- **Pi**（日常任务，默认）：随 `npm ci` 安装，默认复用 `DEEPSEEK_API_KEY`。见 [Pi 配置](docs/pi-rpc-provider.md)。
- **Codex App Server**（复杂开发）：运行时由 Python 依赖固定。旧的 Direct Codex 需设
  `DIRECT_CODEX_PROVIDER_ENABLED=true` 并指向 `codex` 可执行文件；两者不要同时开启。
- **Browser**：需要先安装 Playwright Chromium（见提示清单）。
- **ACP Agent**（实验）：见 [ACP 说明](docs/acp_provider.md)。
- **MCP**：Settings → Providers 中添加，只授予 Codex、Pi、ACP 这类兼容 Work Provider。见
  [MCP 环境与审批](docs/mcp_environment.md)。

### 语音

- **远程 TTS**（`T2`）：推荐 Fish Audio：

  ```dotenv
  TTS_BACKEND=fish_audio
  FISH_TTS_API_KEY=<your-fish-api-key>
  FISH_TTS_MODEL=s2.1-pro-free
  FISH_TTS_REFERENCE_ID=b450b19370434173b121446057622e9b
  FISH_TTS_LATENCY=balanced
  ```

  MiMo 与 OpenAI 兼容端点同样支持。见 [Fish Audio 说明](docs/fish_audio_websocket.md)。
- **远程 ASR**（`T2`）：OpenAI 兼容端点，显式选择，不会静默回退。
- **实时打断**（`T3`）：Silero VAD + 回声消除。构建或导入通过不代表真实设备上的回声消除已验收。
- **本地 ASR / TTS**（`T4`）：

  ```bash
  uv run --locked --no-sync python tools/external_assets.py install ~/Downloads/amadeus-asr-qwen3-0.6b.zip
  uv run --locked --no-sync python tools/external_assets.py install ~/Downloads/amadeus-voice-kurisu-gpt-sovits-v3.zip
  uv run --locked --no-sync python tools/external_assets.py status
  ```

  用 `local-cpu` 时需设 `TTS_DEVICE=cpu`，因为 `auto` 在 Linux 上固定选 `cuda:0`。
  FlashAttention 可选，Linux 上游 wheel 的来源与验证范围见
  [Torch 2.7 候选](docs/torch27_candidates.md#flashattention-wheel-inventory)。
- **唤醒词**（`T4`）：Settings → Voice → Wake recognition，默认 SenseVoice。唤醒监听默认随壁纸启动
  （`--wallpaper` / `AMADEUS_WALLPAPER=1`），而 Linux 壁纸未验收；需要唤醒词时，可让后端客户端显式发送
  `wake.start`。见 [唤醒词设置](docs/wake_word_setup.md)。

### 角色

- **角色包**：用 `tools/external_assets.py install` 安装，见 [外部资产包](docs/external_asset_bundles.md)。
- **Live2D**（实验）：Settings → Characters → Appearance，见 [角色形象与 Live2D](docs/live2d_character_visuals.md)。
- **角色管理**：见 [本地角色管理](docs/character-management.md)。
- **角色知识 RAG**（可选）：追加 `--extra rag`，见 [角色知识](docs/character_rag.md)。

### 其他

- **本地 LLM**（可选）：设 `LLM_PROVIDER=local` 并配置 `llama-server` 与 GGUF，再运行
  `uv run --locked --no-sync python -m llm.llama_server --foreground --profile local`。
- **插件**（实验）：Settings → General → Manage plugins，见 [扩展 Host profile](docs/extension_host_profile_v0.1.md)。
- **AUIP 应用会话**：见 [AUIP 应用会话](docs/auip_application_sessions.md)。
- **图形性能**：Settings → 图形与性能。
- **VTube Studio**：默认关闭的兼容旁路，`VTS_ENABLED=true` 开启。

## 不支持或不提供

- 桌面壁纸宿主（Lively / Wallpaper Engine 为 Windows 专属；macOS 有原生 Electron 宿主，Linux 没有）。
- VN Player（Windows 专属）。
- `local-cu124`、`local-rocm`（只锁定了 Windows 轮子）。
- 安装包、登录自启动、Docker 部署。

## 故障排查

| 现象 | 成因与处理 |
|---|---|
| `--extra voice` 时 PyAudio 或回声消除模块编译失败 | 安装 `build-essential pkg-config portaudio19-dev`，在全新 `.venv` 重新同步。 |
| T4 安装时本地模型依赖编译失败 | 再安装 `swig meson ninja-build`。 |
| `uv sync` 后需要的包被删了 | `uv sync` 是精确同步，始终给完整命令。 |
| `vad` 校验报 `not a locked build` | torch 不来自任何 build extra。重跑带 `--extra torch-cpu` 或 `--extra local-<build>` 的命令。 |
| `--require-cuda-device` 失败 | 检查 NVIDIA 驱动是否支持 CUDA 12.8。 |
| Browser Work 报找不到浏览器或缺系统库 | `uv run --locked --no-sync python -m playwright install chromium`；缺系统库时由用户运行 `sudo .venv/bin/python -m playwright install-deps chromium`。 |
| `local-cpu` 下本地 TTS 报 CUDA 错误 | 设 `TTS_DEVICE=cpu`。 |
| Electron 无法启动 | 确认在图形桌面会话中运行；无界面时改用 headless 后端。 |
| 改了源码但桌面端没变化 | `cd electron && npm run build`。 |
| 不要复制或重命名 `.venv` | 入口脚本内嵌原解释器绝对路径，请在最终路径重建。 |
