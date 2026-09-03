<div align="center">

<h1>Amadeus: Real-Time Multimodal AI Agent for Desktop Interaction</h1>

<p>一层面向本地 AI OS 的交互界面</p>

<p>
  中文 | <a href="./README_EN.md">English</a>
</p>

<img src="./assets/header-strip.zh.svg" width="880" alt="TALK 可打断的实时语音 · EMBODY 表演与语音同帧 · ACT Provider 委派执行 · CONTROL 可恢复、可接管"/>

<p>
  <a href="https://www.bilibili.com/video/BV1783G6hEYY/"><img src="https://img.shields.io/badge/demo-Bilibili-2f624a?labelColor=061710&logo=bilibili&logoColor=61eeb6" alt="B 站演示"/></a>
  <a href="./assets/architecture-overview-crt.svg"><img src="https://img.shields.io/badge/architecture-current-184b36?labelColor=061710" alt="当前架构图"/></a>
  <img src="https://img.shields.io/badge/version-0.1_%CE%B1-2f624a?labelColor=061710" alt="Amadeus 0.1 alpha"/>
  <img src="https://img.shields.io/badge/部署阶梯-L1%2FL2%20跨平台%20·%20L3%2FL4%20Windows-2f624a?labelColor=061710" alt="部署阶梯：L1/L2 跨平台，L3/L4 仅 Windows"/>
  <img src="https://img.shields.io/badge/license-PolyForm%20Noncommercial-272018?labelColor=061710" alt="许可证"/>
</p>

[![Amadeus 中的 Provider 工作界面：任务状态、流式结果与角色场景同时可见](./assets/demo/provider-runtime.jpg)](https://www.bilibili.com/video/BV1783G6hEYY/)

<sub>点击画面观看 10 分钟完整演示</sub>

</div>

> [!IMPORTANT]
> 本仓库包含可构建、可运行的公开源码，当前版本为 **0.1 α**，
> 不是带安装器的正式桌面发行版。首方代码采用
> [PolyForm Noncommercial 1.0.0](LICENSE)：允许非商业使用、修改和再分发；
> 商业使用需要另行取得书面许可。第三方代码与外部资产保留各自条款。
>
> **想先跑起来？** → [快速开始](#快速开始)。想先了解项目，从
> [Amadeus 想解决什么](#amadeus-想解决什么) 开始。

## Amadeus 想解决什么

语音助手、桌面角色与执行型 Agent 往往分散在不同窗口：一个负责聊天，一个
负责表演，另一个在终端或浏览器中工作。长任务开始后，用户又很难知道它进行
到了哪里、需要什么权限，以及失败后能否继续。

Amadeus 试图把这些体验连成一个闭环：

1. **Talk — 自然交流**：语音或文字对话，并能在生成、合成和真实播放过程中随时打断。
2. **Embody — 角色具身**：语音、字幕、口型、表情和场景行为沿同一条播放时间线发生。
3. **Act — 委派执行**：主角色把工作交给注册的 Work Provider，而不是直接获得所有工具。
4. **Control — 保持掌控**：Project、Draft、Artifact、进度、权限、Diff 和结果保持可见，并可继续、重试或接管。

角色负责交流和叙述，专业 Provider 负责执行，Host 负责身份、状态、权限、
持久化与恢复。

## 演示切片

| 实时对话与角色表现 | 场景化工作状态 |
|---|---|
| ![角色正在进行带字幕的实时语音对话](./assets/demo/conversation.jpg) | ![角色进入工作场景并播报 Provider 的检索结果](./assets/demo/scene-runtime.jpg) |
| 语音、字幕、口型与表情绑定到真实播放进度。 | 后台任务驱动角色行为、场景状态和结果叙述。 |

演示视频展示了实时语音、角色表现、桌面场景、Browser / OpenClaw 任务以及
论文检索流程。当前源码的桌面界面、Provider 接入和资产边界已经继续演进，
视频应被视为一次产品切片，而不是逐像素安装预览。

> [!NOTE]
> 演示中的角色、场景、声音及其他第三方素材只用于展示原型，不属于 Amadeus
> 代码许可证授权范围。公开源码不包含未获得再分发许可的角色包、模型权重、
> 参考音频或创作中间资产。

## 当前核心能力

| 能力 | 当前公开源码 |
|---|---|
| **可打断实时对话** | 共享麦克风生命周期、独立 Wake / Conversation ASR、两段式端点、AEC / barge-in，以及贯穿 LLM、TTS 与物理播放的中断。 |
| **远程主 Chat 与本地语音** | DeepSeek V4 Flash Main Chat；Qwen3-ASR / SenseVoice；内嵌 GPT-SoVITS v3 流式合成、连续播放与播放前口型发布。 |
| **角色与桌面呈现** | SpriteForge 图状态、KTX2/PixiJS 运行时、字幕、口型和情绪同步；没有角色包时 Chat、Work 与 headless 仍可启动。 |
| **Provider Runtime** | 当前包括 Browser、Codex App Server / Direct Codex 与可选 OpenClaw；Claude CLI 是已确定的后续 direct Provider。 |
| **持久 Work 控制面** | Project、默认 Draft、WorkItem / Attempt、Continue / Retry、重启恢复、权限、Artifact Registry 与结构化 Diff。 |
| **Artifact 与 AUIP** | Work 产物可预览、打开，或在校验后附加为有界 AUIP AppSession，让 Amadeus 与应用交互而不把叙述变成执行权限。 |
| **统一设置入口** | Models、Voice、Providers/MCP、视觉、角色包状态和聊天外观在 Electron Settings 中集中管理。 |

MCP 与 Skills 即使共用 Host registry，也只授予兼容 Provider；**Main Chat
不能直接调用 MCP 工具**。远程 DeepSeek 是主 Chat 基线；远程 ASR/TTS 是显式
兼容路径，不会在本地语音失败后静默上传或产生第二笔计费请求。

## 仓库地图

```text
electron/       Electron main、preload、React renderer 与 Settings
server/         认证后的本地后端、Host 控制面与 AUIP
core/           Main Chat runtime 与会话集成
agent_host/     Provider contracts、adapters、Work identity 与 capabilities
asr/            Conversation / Wake 识别后端
tts/            合成后端、分句 pipeline、播放与口型信号
render/         SpriteForge runtime adapter 与 PixiJS renderer
wallpaper/      Electron/Lively host 与 Win32 桌面放置
vn_player/      Experimental VN Player integration
assets/         Git-owned UI 资产与外部 runtime 资产落点
release/        公开源码选择、provenance 与 deterministic archive policy
```

`main.py` 不是应用入口，只输出退役提示。Python 主入口是
`uv run --locked --no-sync python -m server.app --port 17777`，桌面入口是
Windows `run_electron_utf8.bat` / macOS `npm run electron:dev`（自动发现 `.venv`，L1–L4 通用）。

## 系统架构

[![Amadeus 当前架构：Host 权威、Work Provider、Provider-scoped MCP/Skills、AUIP AppSession、语音与 SpriteForge 呈现边界](./assets/architecture-overview-crt.svg)](./assets/architecture-overview-crt.svg)

图中有三个刻意的“不合并”：

- Main Chat、Work Provider 与 AUIP application 是不同权限域；
- MCP/Skills 不会因为 registry 共用而直接暴露给 Main Chat；
- Artifact、identity、permission 与 receipt 是 Host 核验的事实，模型叙述不能替代。

当前 Codex 由 App Server 或 Direct transport 接入，不依赖旧 Locus 网关。
Claude CLI 将在后续作为独立 direct Provider 进入同一边界，而不是恢复 Locus。

## AUIP 应用会话（application sessions）

AUIP 是 Amadeus 的 cooperative application protocol，不是 Provider、MCP 或主
Chat 工具系统。它解决的是：当 Work 已生成一个可运行 Artifact，用户如何在
保留 Host 权限边界的前提下，继续让 Amadeus 与这个应用协作。

```text
verified Work Artifact
  -> Host prepares a short-lived attach ticket
  -> application registers declared state/events/actions
  -> bounded AppSession
  -> character receives scoped projection and action receipts
```

- ticket 绑定当前 Session、不可变 Artifact 引用与有效期；应用提交 Artifact id，而不是任意路径。
- Host 校验 workspace 归属、类型、digest 和启动入口，并拥有 AppSession identity、revision 与 action authority。
- 应用只能发布 manifest 中声明的状态和语义事件，只能接收已声明且经过授权的 typed action。
- AUIP 不授予 `work.*`、`provider.*`、`tts.*`、任意文件系统或其他 Session 权限。
- 断连成为可见状态并使待确认动作失效，不会在陈旧状态上静默继续。

当前 schema 是 `amadeus.auip/v0`，实现位于本仓库。详见
[AUIP 应用会话文档](docs/auip_application_sessions.md)。独立的
[Code-Amadeus/auip](https://github.com/Code-Amadeus/auip) 目前仍是公共 namespace
placeholder，本版本不声称已经发布独立 SDK 或 conformance suite。

## 快速开始

依赖按能力分四级：先装最小的 L1 跑通，再按需升梯（torch 只在 L3/L4 进入
安装）。L1/L2 在 Windows 与 macOS 上通用；**L3/L4 目前仅支持 Windows +
NVIDIA 平台**（L4 依赖 NVIDIA CUDA 12.4 构建）——官方验证、锁文件与
安装基线都在 Windows 11 + CUDA 12.4；**AMD ROCm 及其他 GPU 平台无官方
支持**。所有平台统一用 [uv](https://docs.astral.sh/uv/) 提供 Python 3.12
并安装依赖。

| 梯级 | 能力 | 平台 | 安装方式 |
|---|---|---|---|
| L1 core | 文字聊天、工作、Provider、角色渲染 | Windows / macOS | `uv sync --locked` |
| L2 voice | 说（远程 TTS、播放、口型）+ 听（麦克风、远程 ASR）| Windows / macOS | `uv sync --locked --extra voice` |
| L3 vad | 实时打断（角色说话时可以插话）| 仅 Windows | `uv sync --locked --extra voice --extra vad` |
| L4 local-cu124 | 本地 GPT-SoVITS / Qwen3 ASR / 唤醒词 | 仅 Windows + NVIDIA GPU | `uv sync --locked --extra voice --extra vad --extra local-cu124`（见 [L4](#windows-专属梯级l3l4)） |

四个梯级共用**同一个 `.venv`**，命令是前缀递增的：升梯时把下层 extra 一并带上
（`uv sync` 会精确同步，漏带会把已装层裁掉）。torch 在 L3 进入安装（CPU 版），
升到 L4 时自动换成 CUDA 12.4 构建。

- 主 Chat 默认远程 DeepSeek；llama.cpp 是可选本地 LLM profile（见
  [兼容路径](#兼容路径)），不是安装前提。
- L2 无 vad 层时，语音端点自动降级为能量检测；安装 vad 后恢复
  silero 精准端点与打断。
- Windows 上每装完一级可验证导入合同（`ci` 同 `cpu`）：
  `uv run --locked --no-sync python tools/verify_python_environment.py --profile <cpu|voice|vad>`，
  L4 用 `--profile cu124 --require-cuda-device`（L3/L4 验证器仅限 Windows，
  macOS 以实际启动验证 L1/L2）。
- 纯文字 / headless（CI）场景用 L1 即可：`uv run --locked --no-sync python -m server.app --port 17777`
  直接启动后端；严格文字模式设置 `TTS_BACKEND=disabled` 并关闭 Wake。

### 参考硬件

**L1/L2（Windows / macOS）**

- CPython **3.12**（由 uv 管理，无需系统安装）
- Node.js **22**（当前参考 `22.21.1`）
- 无 GPU 要求

**L3/L4（仅 Windows）追加**

- CUDA 12.4-compatible NVIDIA GPU，目标 **8 GiB VRAM**
- **16 GiB 内存起步，32 GiB 推荐**

具体峰值取决于本地 ASR/TTS 模型与并发配置；8 GiB / 16–32 GiB
描述的是远程 Chat + 本地语音配置。选用本地 LLM 时需要按模型、量化、
context 和 GPU offload 另行评估内存。

### 基础环境（L1/L2，Windows / macOS）

安装 uv（Windows：`winget install astral-sh.uv`；macOS：`brew install uv`），
然后克隆并按梯级安装——两个平台的命令完全一致：

```bash
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus

uv venv .venv --python 3.12
uv sync --locked                              # L1 core
uv sync --locked --extra voice                # + L2 voice（可选，前缀递增）
```

macOS 上 PyAudio（L2 语音采集）从源码编译，需要先 `brew install portaudio`。

venv 固定命名为 `.venv`：Electron 启动器会自动发现它（Windows
`Scripts\python.exe`，macOS `bin/python3`），无需手动设置 `AMADEUS_PYTHON`。

Electron 前端（全平台）：

```bash
cd electron
npm ci
npm run build
cd ..
```

`npm ci` 会通过项目 postinstall 安装锁定的 Electron 运行时。国内网络可为
npm/Electron 配置镜像（如 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`）。

### Windows 专属梯级（L3/L4）

与 L1/L2 共用同一个 `.venv`，命令按前缀递增追加：

**L3 vad — 实时打断**（torch 随之以 CPU 版进入安装）：

```powershell
uv sync --locked --extra voice --extra vad
uv run --locked --no-sync python tools\verify_python_environment.py --profile vad
```

**L4 local-cu124 — 本地语音模型栈**：在同一 `.venv` 上追加，torch 自动换为
CUDA 12.4 构建（经 `pyproject.toml` 的 `[tool.uv.sources]` 路由到
PyTorch cu124 index，仅 Windows + 本 extra 生效）：

```powershell
uv sync --locked --extra voice --extra vad --extra local-cu124
uv run --locked --no-sync python tools\verify_python_environment.py --profile cu124 --require-cuda-device
```

L4 profile 固定 `torch==2.5.1+cu124`、`torchaudio==2.5.1+cu124` 和本地模型
依赖集；它以当前实际运行环境为第一版基线。

L3/L4 仅支持 Windows + NVIDIA 平台；AMD ROCm 及其他 GPU 平台无官方支持。

> **GeForce RTX 50 系（Blackwell，社区验证配置）**：本项目当前使用的
> `torch==2.5.1+cu124` profile 不兼容 RTX 50 系，无法运行本地 CUDA
> 语音模型。50 系用户需要更新 NVIDIA 驱动，并改用社区已验证可运行的
> PyTorch 2.7.0 CUDA 12.8 组合。
>
> **GeForce RTX 50 series (Blackwell, community-validated configuration):**
> the current `torch==2.5.1+cu124` profile is incompatible with RTX 50-series
> GPUs and cannot run the local CUDA voice models. Update the NVIDIA driver and
> use the community-validated PyTorch 2.7.0 CUDA 12.8 combination instead:
>
> 请在单独的实验项目虚拟环境（例如 `.venv_cu128`）中运行以下命令，
> 不要改动正式 `.venv`（其 `uv.lock` 固定 cu124）。
>
> Run this only inside a separate experimental project venv (for
> example `.venv_cu128`); do not modify the formal `.venv` whose `uv.lock`
> pins cu124.
>
> ```powershell
> uv venv .venv_cu128 --python 3.12
> uv pip install --python .venv_cu128 --reinstall `
>   torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 `
>   --index-url https://download.pytorch.org/whl/cu128
> ```
>
> 该组合目前尚未经过项目的完整 clean-install、ASR/TTS/VAD 与 Electron 回归；
> 当前 `uv.lock` 与 `--profile cu124` 验证器仍以
> `torch==2.5.1+cu124` 为准，因此不应将其视为 cu124 正式基线的替代品。
>
> This combination has not yet passed the project's full clean-install,
> ASR/TTS/VAD, and Electron regression gates. The current
> `uv.lock` and `--profile cu124` verifier still require
> `torch==2.5.1+cu124`, so this is not a replacement for the official cu124
> baseline.

### 安装外部运行资产

完整本地语音需要 Qwen ASR 与 GPT-SoVITS v3 语音包；视觉和角色包可选：

```powershell
uv run --locked --no-sync python tools\external_assets.py verify C:\Downloads\amadeus-asr-qwen3-0.6b.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-asr-qwen3-0.6b.zip
uv run --locked --no-sync python tools\external_assets.py verify C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-voice-kurisu-gpt-sovits-v3.zip

# 可选：场景与 KTX2 角色动画
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-visual-runtime.zip
uv run --locked --no-sync python tools\external_assets.py install C:\Downloads\amadeus-character-kurisu.zip
uv run --locked --no-sync python tools\external_assets.py status
```

如果没有预制 Qwen 包，可直接把上游 snapshot 下载到同一个固定落点；运行时
保持离线，不会在第一次录音时临时联网：

```powershell
uv run --locked --no-sync python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-0.6B', local_dir='assets/models/asr/qwen3-asr-0.6b')"
```

GPT-SoVITS 日文前端第一次使用会准备 OpenJTalk 字典。希望正式启动时不再下载，
可预先运行一次：

```powershell
uv run --locked --no-sync python -c "import pyopenjtalk; print(pyopenjtalk.g2p('準備完了'))"
```

### 配置与启动

复制 `.env` 并填写 DeepSeek API key（Windows：`Copy-Item .env.example .env`；
macOS：`cp .env.example .env`），然后在 Settings 中核对：

- **Models**：`deepseek`、官方 endpoint、`deepseek-v4-flash` 与 API key；
- **Voice**：远程 TTS/ASR 端点（如 MiMo）；L4 本地栈另需 Qwen model 目录、GPT-SoVITS **v3** checkpoints、reference audio/text、麦克风、AEC 和 barge-in；
- **General**：可选角色包状态与呈现设置。

启动：

- Windows：`run_electron_utf8.bat`（单一启动器；自动发现 `.venv`，L1–L4 通用）
- macOS：`cd electron && npm run electron:dev`

启动型设置变更后按 **Restart backend to apply**。角色包显示
**Not installed** 是健康状态，不影响 Chat、Work 或 headless 启动。

默认 B2 AppSession 动作路径不会阻塞首次配置。尚未配置受支持的 AUIP
动作模型凭据时，Chat 和 Settings 仍可启动；应用动作保持 fail-closed，
Settings 会明确显示缺少的能力。

## 兼容路径

### 可选本地 LLM

需要 llama.cpp 时，显式设置 `LLM_PROVIDER=local`，配置 executable / GGUF
或已存在的 OpenAI-compatible endpoint，再按需启动：

```powershell
.\start_llm_server.bat
```

LM Studio、Ollama、llama-cli 和 hybrid profiles 仍保留，但不会在
DeepSeek 失败后自动切换。

### 可选远程模型建议

下表是面向当前 API 的推荐 profile，不改变上述角色分工，也不会在端点
失败后自动切换 provider：

| 职责 | 推荐 profile | 当前边界 |
|---|---|---|
| 主 Chat API | DeepSeek-V4-Flash-0731：`DEEPSEEK_BASE_URL=https://api.deepseek.com`，`DEEPSEEK_MODEL_NAME=deepseek-v4-flash` | `deepseek-v4-flash` 是稳定 API alias，当前指向 0731 版本；不把日期写进运行时 model id。 |
| 多模态 / Vision | 优先 `gemini-3.7-flash`；需要较保守的兼容 profile 时可用 `gemini-3.5-flash` | 当前由 Host 内部 visual-context 链负责图像采集，图像发送仍跟随主 Chat provider；独立 Gemini Vision API 路由尚未实现，也不代表恢复旧 Gemini Live sidecar。 |
| Work 执行 Provider | 首选 Codex App Server；其次是可选 OpenClaw Gateway | 这是推荐优先级，不是失败后自动 fallback。Browser 仍是网页任务的专用 Provider。 |
| Work 执行模型 | Codex App Server 可显式选择 GPT-5.6 family 或 `deepseek-v4-flash` | 执行模型属于 Work Provider，不与主 Chat 共用路由或密钥。 |
| AUIP 运行时动作判定 | `AUIP_ACTION_PROVIDER=openai`、`AUIP_ACTION_MODEL=gpt-5.6-terra`、`AUIP_ACTION_REASONING_EFFORT=low`、`AUIP_ACTION_SERVICE_TIER=fast` | 这是 AppSession 的动作 / 参与判定模型，不是 AUIP Artifact 的执行 Provider；`fast` 需要对应 API 项目可用。 |

## 外部模型与运行资产

模型权重、参考音频、角色包及大型/版权敏感素材独立分发；源码仓库只保留
必要图标、默认壁纸、schema、validator 和安装工具。

当前目录合同包括 `asr-qwen3-0.6b`、`voice-kurisu-gpt-sovits-v3`、
`visual-runtime` 与 `character-kurisu`。前两个组成完整本地语音 profile；
后两个只影响场景和角色呈现。

```powershell
uv run --locked --no-sync python tools\external_assets.py verify C:\path\to\asset-bundle.zip
uv run --locked --no-sync python tools\external_assets.py install C:\path\to\asset-bundle.zip
uv run --locked --no-sync python tools\external_assets.py status
```
`external_assets.py` 是纯标准库工具，在任一梯级的 `.venv` 下运行均可；本地语音包
（`asr-qwen3` / `voice-kurisu`）需要 L4 梯级（Windows CUDA）才会被运行时加载。

SpriteForge 角色包最终应落在：

```text
assets/spriteforge/runtime/kurisu/
  runtime_manifest.json
  graph_config.json
  spriteforge_mouth_config.json
  textures/
```

安装器保持标准 `assets/...` 路径、校验 SHA-256、跳过相同文件并拒绝意外覆盖。
详见[外部资产包](docs/external_asset_bundles.md)与
[角色包合同](docs/character_pack_authoring.md)。

### 壁纸模式（推荐 Lively Wallpaper）

Windows 下推荐用开源的
[Lively Wallpaper](https://github.com/rocksdanister/lively) 托管 Amadeus 网页壁纸；
Wallpaper Engine 仍保留兼容。启动 Amadeus 后，将下列本地网页 URL
添加到 Lively（推荐 WebView2），再在 Amadeus 左侧栏点击 **Wallpaper**：

```text
http://127.0.0.1:17777/wallpaper/lively/index.html
```

该稳定入口会自动发现实际 asset/bridge 端口；壁纸模式关闭时会原地等待，
不要手工写死 `17778` 或 `17797`。诊断时可运行
`uv run --locked --no-sync python tools\run_wallpaper_engine_bridge.py` 并使用它打印的 `Lively URL`。
详见 [Lively 入口说明](wallpaper/lively/README.md)。

## 配置所有权

启动值优先级固定为：

1. 父进程环境变量（最高权威，在 GUI 中显示为 locked）；
2. Electron desktop settings；
3. 仓库根目录 `.env`；
4. `config/settings.py` 默认值。

Settings 不会回写 `.env`。普通模型、语音、麦克风、Provider/MCP、视觉、头像和
角色包状态应从 GUI 配置；高级诊断、实验阈值和测试开关留在 `.env`。密钥通过
操作系统 `safeStorage` 加密。详见[配置所有权](config/README.md)与
[本地实例认证](docs/local_instance_authentication.md)。

## 当前发布边界

| 范围 | 状态 |
|---|---|
| L1/L2（文字 + 远程语音）| Windows 与 macOS 源码部署；macOS 为实测路径，官方 CI/锁文件仍以 Windows 为准 |
| L3/L4（本地 CUDA 12.4 语音）| 仅 Windows；第一版产品基线，以当前实际运行环境为准 |
| 8 GiB VRAM / 16–32 GiB RAM | 目标配置；实际占用由模型组合决定 |
| 远程 DeepSeek Main Chat | 第一版默认 profile |
| 远程 ASR / TTS | 显式兼容路径，不静默 fallback |
| Electron installer | 尚未提供；当前从源码启动 |
| Docker | 不是支持的桌面安装路径 |
| SpriteForge 角色包 | 外部分发；缺包仍可启动 |
| VTS | 默认关闭的兼容旁路 |
| VN Player | Experimental |
| 壁纸模式 | 仅 Windows 宿主（Lively / Wallpaper Engine）；其他平台不提供 |
| PyQt / 旧壁纸 host | 已退出公开主线 |
| Claude CLI Provider | 已确定的后续主线 Provider；当前没有 live caller |

## 开发与贡献

```powershell
uv sync --locked --extra dev      # 开发依赖：pytest / ruff 等（见 pyproject [dev] extra）
# Windows 之外没有官方 L3/L4 验证器；macOS/Linux 贡献者直接跑下方 run_tests.py 即可
uv run --locked --no-sync python tools\verify_python_environment.py --profile ci
uv run --locked --no-sync python -X utf8 tools\run_tests.py

cd electron
npm run build
npm audit --audit-level=high
```

提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [ROADMAP.md](ROADMAP.md)。
产品语义、权限、协议、Provider/MCP/Skill、Project/Draft/Artifact 或 AUIP 边界变化
应先开 Issue；小型修复、文档、测试和纯呈现 UI 变更可直接发 PR。安全问题请按
[SECURITY.md](SECURITY.md) 私下报告。

## 公开历史与许可证

公开仓库从一个整理后的初始提交开始。内部研发 commit、实验 branch、已删除角色
素材、模型、密钥、会话、本地路径及原始共作者元数据没有迁入公开 Git 历史。
代码本身按当前发行边界保留。

Amadeus 首方源码和修改采用
[PolyForm Noncommercial 1.0.0](LICENSE)。它是公开源码、非商业许可，而不是 OSI
定义的开源许可证。第三方组件见 [LICENSES](LICENSES/README.md) 与
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。代码许可证不会自动授予角色、
模型、参考音频或外部资产包的权利。

## 相关项目

- [Aqua-TTS](https://github.com/Lucas1479/Aqua-TTS)：MIT 的低延迟 GPT-SoVITS v3 推理运行时；Amadeus 当前不要求安装 Aqua 才能启动。
- [Amadeus SpriteForge](https://github.com/Code-Amadeus/amadeus-spriteforge)：角色 authoring 与 graph/KTX2 工具链的公共 namespace；当前仍是待发布占位仓库。
- [AUIP](https://github.com/Code-Amadeus/auip)：application-session / typed-action 协议的公共 namespace；当前仍是待发布占位仓库。
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)：内嵌语音合成推理基础。
- [OpenClaw](https://github.com/openclaw/openclaw)：可选外部 Work gateway。

<details>
<summary>Star History</summary>
<br />
<p align="center">
  <a href="https://github.com/Code-Amadeus/Amadeus/stargazers">
    <img src="./assets/star-history.svg" alt="Amadeus Star History" width="620" />
  </a>
</p>
</details>

---

<div align="center"><em>El Psy Kongroo.</em></div>
