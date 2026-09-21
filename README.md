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
  <img src="https://img.shields.io/badge/安装配置-core%20%2F%20voice%20%2F%20CPU%20VAD%20%2F%20cu124-2f624a?labelColor=061710" alt="安装配置：core、voice、CPU VAD、cu124"/>
  <img src="https://img.shields.io/badge/license-AGPL--3.0-272018?labelColor=061710" alt="许可证"/>
</p>

[![Amadeus 中的 Provider 工作界面：任务状态、流式结果与角色场景同时可见](./assets/demo/provider-runtime.jpg)](https://www.bilibili.com/video/BV1783G6hEYY/)

<sub>点击画面观看 10 分钟完整演示</sub>

</div>

> [!IMPORTANT]
> 本仓库包含可构建、可运行的公开源码，当前版本为 **0.1 α**，
> 不是带安装器的正式桌面发行版。Amadeus 第一方代码依据
> [GNU Affero General Public License v3.0（AGPL-3.0）](LICENSE) 开源。
> 第三方代码与外部资产保留各自条款。
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
| 持久记忆与长期陪伴 | Host-owned Continuity Runtime：**每个对话框是独立连续域**（记忆、关系/情感、主题静默按 Session 隔离，新对话从零开始）；角色生活为角色级全局；SQLite 单一事实源，显式忘记不可逆；会话 / 记忆 / 账本随启动自动备份到 `runtime/backup/`，无备份的对话框状态在重启时回收（见 [C0 → C8 升级](#连续性与长期陪伴c0--c8-升级)）。 |
| **内置联网研究** | 不依赖 OpenClaw 的 Native Research：quick lookup 与带引用的深度多源报告；Tavily / Bing / SearXNG / DuckDuckGo 后端自动降级。 |

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

## 连续性与长期陪伴（C0 → C8 升级）

升级前，Amadeus 的对话记忆止步于单个 Session：没有跨会话的用户事实、稳定的关系
状态，也无法可靠回溯旧经历。C0–C8 在 Host 层引入完整的 **Continuity Runtime**
（实现位于 `core/continuity/`，测试位于 `tests/continuity/`），C8（Archive Recall /
Advanced Retrieval）为该升级的收口版本。该升级线按阶段交付，每阶段附带 schema
迁移、实现报告与回归证据。**2026-09 语义调整**：连续性被明确为**以对话框为
单位**——不同对话框之间的对话内容、记忆、关系/情感状态与主题静默完全隔离，
新对话框从零开始；下述升级机制（归档回忆、保留分层、关系演化、显式遗忘、
启动备份）在单个对话框域内全部成立。

| 模块 | 阶段 | 升级内容 |
|---|---|---|
| 权威边界冻结 | C0 | 记忆 / 关系 / 生活状态的归属与来源分级、显式忘记语义、非权威规则固化为契约。 |
| RealityClock | C1 | Host 统一提供现实时间（wall + monotonic），持久化时钟回拨检测；跨重启以 wall clock 为准。 |
| Memory Runtime | C1–C2 | SQLite（schema 1→2）长期记忆：确定性抽取、create / reinforce / supersede 语义、显式 remember 快速路径、崩溃恢复与原子写入。 |
| 检索与 grounding | C3 | 结构化查找 + SQLite FTS5 词法检索基线，可选语义检索仅作可重建派生索引；排序、去重与有界 `[Continuity grounding]` 注入。 |
| Retention / Maintenance | C4 | `hot` / `cold` / `archive` 保留分层（schema 4）、确定性衰减、pinned / P0 保护、hot 工作集上限降级、Work Ledger 事件投影。 |
| Relationship Runtime | C5 | 长期关系（familiarity / trust / warmth / respect / closeness）与短期情绪（半衰期衰减）分离；事件溯源 + Host reducer（schema 5）。 |
| Character Life | C6 | 确定性每日日程（一个本地日一份计划）、跨日 ongoing threads、重启与跨日有界追补、聊天中断暂停与恢复（schema 6）。 |
| Continuity UI 与诊断 | C7 | Host-owned WebSocket 控制面：记忆列表、pin / unpin、显式 Forget、维护操作与关系 / 日程诊断；Electron 只渲染 Host 事实。 |
| Archive Recall | C8 | 低置信度历史 gate、有界 Session archive 检索（仅本对话框内引用）、时间感知过滤、双方引用的高保真回退（超限语意压缩并标注 `condensed`）与无内容检索 trace（schema 7）。 |

### 技术路线

- **SQLite 是唯一权威事实源**；FTS5、embedding 与向量索引均为可重建的派生缓存，不构成第二事实库。
- **事件 / 事实 + 生命周期模型**：不把全部聊天无差别向量化；写入、合并、覆盖与过期的决定权在 Host，模型只输出候选语义。
- **同步快速路径与异步整合分路**：显式 remember / forget 同步完成；普通记忆抽取在 `chat.complete` 之后异步整合，不阻塞首句延迟。
- **显式忘记是不可逆闭包**：硬删除 + 不含被忘内容的 tombstone，并在同一事务内失效派生状态与派生索引；旧 Session 历史无法复活已忘记的事实。
- **来源分级**：仅 user_asserted / host_verified / work_ledger / auip_verified / simulated_life 等来源可成为长期事实；助手自由生成文本默认不是用户事实。
- **关系慢变量与短期情绪分离；角色生活是 `SIMULATED_LIFE` 派生状态**，不反向写入 Canon、用户事实或 Work 权威。
- **Shadow → Live 两步交付**：新状态先在影子路径累积与验证，Main Chat 投影由独立 feature flag 控制、默认关闭。
- **不改变既有权威边界**：Persona、Character RAG、Work、Provider、AUIP 与权限的权属维持不变；C8 的 schema 7 仅新增“归档检索不得绕过忘记”的 metadata guard。
- **可观测与性能隔离**：诊断与检索 trace 内容无涉；普通聊天路径不触碰 Session archive（本地探针：500 次普通请求 0 次归档尝试）。

### 记忆保真、历史回忆与本地备份（2026-09 强化）

- **会话隔离（核心语义）**：每个对话框（Session）是一个独立连续域 —— 记忆、关系/情感状态、主题静默、遗忘墓碑全部以 `scope = 会话 ID` 隔离，增删改查互不影响；新建对话框的记忆为空、关系为中性基线，另起新篇；历史提问只引用**本对话框**自己的对话原文。角色生活（C6）与主机时间保持角色/主机级全局。
- **捕获**：实质性用户发言逐字保存至 800 字符；超出上限的发言与显式“记住…”长负载整段做**语意压缩**（复用主对话所配置的 LLM，失败时回退确定性整体采样），不再尾截断丢内容；原文始终保留在 Session 记录中，可再被历史引用找回。
- **历史回忆**：显式历史提问触发的归档引用**每轮固定含双方**——用户声明 + 明确标注非权威的角色当时措辞；单侧预算 1400 / 1000 字符，超限侧压缩后在标签中标注 `(condensed)`，因此摘要不会被当作逐字原话；每次提问最多引用 3 轮、总 grounding 预算 3600 字符；语意压缩调用每问最多 4 次（带进程内缓存，失败 / 超时自动回退）。
- **备份即恢复单元**：每次启动自动刷新固定备份槽 `runtime/backup/`（`continuity.sqlite3`、`work_ledger.sqlite3`、`sessions/` 镜像）。同名覆盖、不堆叠；**已从界面删除的对话框只要备份镜像还在，对话内容 / 记忆 / 关系状态均可完整找回**；数据库仅在通过 `PRAGMA quick_check` 时刷新，损坏或缺失会保留上一份好备份。路径全部相对项目根解析，项目文件夹改名不受影响；CI / 隔离环境设置 `AMADEUS_*` 状态路径时自动跳过。
- **无备份则清理**：若某对话框的转录与备份镜像都不存在（例如会话和备份都被删掉），**下次启动时**该会话关联的记忆、关系/情感状态、静默、遗忘墓碑与整合日志会在启动备份之后被事务性清除；`__unsessioned__`（无会话语音轮）豁免。
- **后台工具**：`uv run --locked --no-sync python -X utf8 tools/backfill_continuity_memories.py --rebuild --backup`（先 `--dry-run` 预演）按会话作用域重存历史聊天；Continuity 设置页默认只展示**当前对话框**的记忆与关系诊断。

## 其他更新（C8）

- **兼容远程 Qwen3-ASR-Flash API（`qwen3_asr_api`）**：在本地 embedded Qwen3-ASR（sidecar）、SenseVoice 与 OpenAI-compatible 后端之外，新增阿里云百炼（Model Studio）Qwen3-ASR-Flash 形态的远程转录后端；配置 base URL、API key 与模型名即可启用，Settings 中可查看就绪状态。作为计量型远程 API，它不对未完成语音发送投机转录请求。
- **手动 / 自动语音输入模式**：手动 Mic 为 click-to-talk——每次点击只识别一整段话，且不启用后台打断与投机转录；自动 Mic 开启连续会话（continuous session）持续监听多段对话，与 Wake 一并构成免手模式并启用实时 barge-in。
- **Native Research 联网搜索**：内置研究 Provider 不依赖、也不调用 OpenClaw，直接从 Main Chat 或 Work 发起 Web 检索。短查询走 quick lookup（内联 `[n]` 引用）；复杂任务进入 ProviderRuntime 后台深度研究：多查询检索、来源评分与交叉印证、确定性生成带引用的 Markdown 报告。搜索后端按 Tavily → Bing → SearXNG → DuckDuckGo（免 key）自动降级，规划与综合复用现有 DeepSeek 配置。详见 [Native Research (Phase 3)](#native-research-phase-3)。

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

依赖按能力分四级：先装最小的 L1 跑通，再按需升梯（默认阶梯中 torch 在 L3/L4 进入安装；可选 RAG 也会引入
本地 embedding/Torch 依赖）。Windows 是当前参考平台，macOS 的 L1/L2 安装与 CI 单独验证；实际
桌面、麦克风和播放体验仍需设备验收。L3 可选择 CPU VAD，**无需 NVIDIA GPU**；
L4 的当前 cu124 配置面向 Windows + NVIDIA。Windows ROCm 7.2.1 已有互斥的
`local-rocm` 实验锁与验证入口，但尚未完成受支持 AMD GPU 的端到端验收；RTX 50 系
cu128 仍是社区配置记录。
统一使用 [uv](https://docs.astral.sh/uv/) 与 Python 3.12，CI 固定 uv 0.12.8。

| 梯级 | 能力 | 平台 | 安装方式 |
|---|---|---|---|
| L1 core | 文字聊天、工作、Provider、角色渲染 | Windows / macOS | `uv sync --locked` |
| L2 voice | 说（远程 TTS、播放、口型）+ 听（麦克风、远程 ASR）| Windows / macOS | `uv sync --locked --extra voice` |
| L3 CPU VAD | 实时打断（角色说话时可以插话）| CPU，无 NVIDIA GPU 前提 | `uv sync --locked --extra voice --extra vad --extra torch-cpu` |
| L4 local-cu124 | 本地 GPT-SoVITS / Qwen3 ASR / 唤醒词 | Windows + NVIDIA GPU | `uv sync --locked --extra voice --extra vad --extra local-cu124` |
| 实验 local-rocm | 本地 GPT-SoVITS / Qwen3 ASR sidecar | Windows + AMD 官方矩阵内 GPU | `uv sync --locked --extra voice --extra vad --extra local-rocm` |

四个默认梯级与 ROCm 实验选项均使用**同一个 `.venv`**。每次给出完整目标配置：
`uv sync` 会精确同步，漏带会移除已装层。`torch-cpu`、`local-cu124` 与
`local-rocm` 两两互斥；切换构建时替换对应 extra，并保留 `voice`、`vad`。
详见[安装配置与迁移](docs/install_profiles.md)。

- 主 Chat 默认远程 DeepSeek；llama.cpp 是可选本地 LLM profile（见
  [兼容路径](#兼容路径)），不是安装前提。
- L2 无 vad 层时，语音端点自动降级为能量检测；安装 vad 后恢复
  silero 精准端点与打断。
- Windows 上每装完一级可验证导入合同（`ci` 同 `cpu`）：
  `uv run --locked --no-sync python tools/verify_python_environment.py --profile <cpu|voice|vad-cpu>`，
  L4 用 `--profile cu124 --require-cuda-device`；ROCm 实验入口用 `--profile rocm`
  并继续执行 GPU compute probe。导入/构建验证不替代真实模型与音频设备测试。
- 纯文字 / headless（CI）场景用 L1 即可：`uv run --locked --no-sync python -m server.app --port 17777`
  直接启动后端；严格文字模式设置 `TTS_BACKEND=disabled` 并关闭 Wake。

### 参考硬件

**L1/L2（Windows / macOS）**

- CPython **3.12**（由 uv 管理，无需系统安装）
- Node.js **22**（当前参考 `22.21.1`）
- 无 GPU 要求

**L4 cu124（Windows 本地模型）追加**

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
uv sync --locked --extra voice                # L2 voice（可选）
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

### VAD 与本地模型

与 L1/L2 共用同一个 `.venv`，选择完整的能力与构建组合：

**L3 vad — 实时打断**（torch 随之以 CPU 版进入安装）：

```powershell
uv sync --locked --extra voice --extra vad --extra torch-cpu
uv run --locked --no-sync python tools\verify_python_environment.py --profile vad-cpu
```

**L4 local-cu124 — 本地语音模型栈**：在同一 `.venv` 上选择 CUDA 配置，torch 换为
CUDA 12.4 构建（经 `pyproject.toml` 的 `[tool.uv.sources]` 路由到
PyTorch cu124 index，仅 Windows + 本 extra 生效）：

```powershell
uv sync --locked --extra voice --extra vad --extra local-cu124
uv run --locked --no-sync python tools\verify_python_environment.py --profile cu124 --require-cuda-device
```

L4 profile 固定 `torch==2.6.0+cu124`、`torchaudio==2.6.0+cu124` 和本地模型
依赖集；它以当前实际运行环境为第一版基线。

**实验 local-rocm（Windows）**：同一 `.venv` 可精确选择 AMD 官方 ROCm 7.2.1、
Torch/Torchaudio 2.9.1 与完整本地模型依赖；Qwen ASR 和 GPT-SoVITS 在常驻 sidecar
子进程中运行，但默认仍使用当前 `.venv` 的解释器。该入口默认关闭，且与 cu124/CPU
Torch 构建互斥。安装后必须先运行环境验证与真实 FP32 GPU compute probe；即使
`torch.cuda.is_available()` 返回 True，compute 失败也不得继续模型测试。完整命令、
设备矩阵和验收边界见 [Windows ROCm 实验 sidecar](tools/rocm_sidecar/README.md)。

本机 Radeon 780M（gfx1103）实测可被 ROCm 枚举，但首次 FP32 计算在 AMD HIP DLL
中崩溃；该核显不在 AMD 官方 7.2.1 Windows PyTorch 矩阵内，因此不能作为可用目标。

> **GeForce RTX 50 系（Blackwell，社区验证配置）**：本项目当前使用的
> `torch==2.6.0+cu124` profile 不兼容 RTX 50 系，无法运行本地 CUDA
> 语音模型。50 系用户需要更新 NVIDIA 驱动，并改用社区已验证可运行的
> PyTorch 2.7.0 CUDA 12.8 组合。
>
> **GeForce RTX 50 series (Blackwell, community-validated configuration):**
> the current `torch==2.6.0+cu124` profile is incompatible with RTX 50-series
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
> 上述仅安装社区记录的 PyTorch 组合，不是完整 Amadeus 安装步骤。
>
> 该组合目前尚未经过项目的完整 clean-install、ASR/TTS/VAD 与 Electron 回归；
> 当前 `uv.lock` 与 `--profile cu124` 验证器仍以
> `torch==2.6.0+cu124` 为准，因此不应将其视为 cu124 正式基线的替代品。
>
> This combination has not yet passed the project's full clean-install,
> ASR/TTS/VAD, and Electron regression gates. The current
> `uv.lock` and `--profile cu124` verifier still require
> `torch==2.6.0+cu124`, so this is not a replacement for the official cu124
> baseline.

### 安装外部运行资产

另提供默认关闭的[角色知识 RAG 选项](docs/character_rag.md)，支持远程和本地 Main Chat。
它包含可直接构建的中日文基础资料，也支持自己的知识目录；Settings 可查看实际阈值和加载状态。
RAG 会额外安装本地 embedding/Torch 依赖。资料、索引构建、诊断与验证范围见说明。

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
`external_assets.py` 是纯标准库工具，在任一梯级的 `.venv` 下运行均可。运行本地语音
模型还需匹配的模型依赖与硬件，安装资产包本身不会补齐这些依赖。cu124 正式配置和
ROCm 实验边界见[安装配置](docs/install_profiles.md)。

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
| L1/L2（文字 + 远程语音）| Windows 与 macOS 源码部署；Windows 为参考平台，macOS L1/L2 有独立 CI，桌面与音频体验仍需实机验收 |
| L3 CPU VAD | 不要求 NVIDIA GPU；使用明确的 CPU 构建配置 |
| L4 cu124（本地 CUDA 12.4 语音）| Windows + NVIDIA；以当前实际运行环境为参考 |
| AMD ROCm 7.2.1 | 单 `.venv` 实验锁、sidecar adapter 与失败闭环已提供；受支持 AMD GPU 实机验收待补齐 |
| RTX 50 系 cu128 | 社区配置记录，尚无正式锁与完整回归 |
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
uv sync --locked --extra dev      # core + 开发工具；会移除未选择的语音/模型层
# 保留语音/模型能力时，在完整安装命令末尾追加 --extra dev
uv run --locked --no-sync python tools\verify_python_environment.py --profile ci
uv run --locked --no-sync python -X utf8 tools\run_tests.py

cd electron
npm ci
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

Amadeus 第一方源码和修改依据
[GNU Affero General Public License v3.0（AGPL-3.0）](LICENSE) 开源。
第三方组件保留各自许可证，见 [LICENSES](LICENSES/README.md) 与
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

## Native Research (Phase 3)

Amadeus includes a native `research` Provider for Web lookup and cited multi-source research without OpenClaw. Quick lookups return compact chat/voice-first answers; complex research runs inside ProviderRuntime's background Work lifecycle and produces a cited Markdown report. Search can use Tavily, Bing Web Search, SearXNG, or a keyless DuckDuckGo HTML fallback, while the existing DeepSeek configuration is used for planning and evidence synthesis. See `PHASE3_RESEARCH.md` for configuration, scoring/citation rules, and tests.
