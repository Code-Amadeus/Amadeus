<div align="center">

# Amadeus

**一个会说话、会出现，也会把工作做完的本地桌面 AI Agent**

把可打断语音、角色具身、桌面场景与 Coding / Browser Agent<br>
放进同一个可见、可恢复、可由用户接管的工作界面。

[简体中文](./README.md) · [English](./README_EN.md) · [B 站演示](https://www.bilibili.com/video/BV1783G6hEYY/) · [架构大图](./assets/architecture-overview-crt.svg)

[![Amadeus 中的 Provider 工作界面：任务状态、工具事件、流式结果与场景角色同时可见](./assets/demo/provider-runtime.jpg)](https://www.bilibili.com/video/BV1783G6hEYY/)

<sub>点击画面观看 10 分钟完整演示</sub>

</div>

> [!IMPORTANT]
> **源码正在整理与发布审计中。** 当前仓库用于公开项目方向、演示和发布进度，尚未包含可构建运行的源代码，因此目前还不是正式开源发行版。

## Amadeus 想解决什么

今天的语音助手、桌面角色与执行型 Agent 往往分散在不同窗口里：一个负责聊天，一个负责表演，另一个在终端或浏览器中工作。长任务开始后，用户又很难知道它进行到了哪里、需要什么权限，以及失败后能否继续。

Amadeus 试图把这些体验连成一个闭环：

1. **Talk — 自然交流**：用户通过语音或文字交谈，并能像真人对话一样随时插话。
2. **Embody — 角色具身**：语音、字幕、口型、表情和场景行为沿同一条播放时间线发生。
3. **Act — 委派执行**：OpenClaw、Browser，以及经 Locus 接入的 Claude Code / Codex 在后台完成工作。
4. **Control — 保持掌控**：任务进度、权限、终端、文件、Diff 和结果都可见；用户可以暂停、继续、重试或接管。

Amadeus 并不是给聊天窗口加一张立绘，也不是把 IDE 缩小后塞进桌面。它更接近一层面向本地 AI OS 的交互界面：角色负责交流和叙述，专业 Agent 负责执行，系统负责状态、边界和恢复。

## 演示切片

| 实时对话与角色表现 | 场景化工作状态 |
|---|---|
| ![角色正在进行带字幕的实时语音对话](./assets/demo/conversation.jpg) | ![角色进入工作场景并播报 Provider 的检索结果](./assets/demo/scene-runtime.jpg) |
| 语音、字幕、口型与表情绑定到真实播放进度。 | 后台任务会驱动角色行为、场景状态和结果叙述。 |

演示视频展示了实时语音、角色表现、桌面场景、Browser / OpenClaw 任务以及论文检索流程。Coding Agent 与游戏陪玩方向仍在继续开发，将在后续演示和源码版本中逐步公开。

**[前往哔哩哔哩观看完整演示 →](https://www.bilibili.com/video/BV1783G6hEYY/)**

> [!NOTE]
> 演示画面中的角色、场景、声音及其他第三方素材仅用于原型展示，不属于 Amadeus 的开源范围。正式发行不会包含未获得再分发许可的素材。

## 核心能力

| 能力 | 当前内部版本 |
|---|---|
| **可打断实时语音** | 共享麦克风生命周期、唤醒与连续对话、Qwen3-ASR / SenseVoice 路径、两段式端点、AEC 回声保护，以及贯穿 LLM、TTS 和物理播放的全链路中断。 |
| **流式表达时间线** | 多 LLM 后端、Hybrid 首句、分句流式 TTS、有序并发播放、双轨字幕；表情和动作在对应语句真正开始播放时触发。 |
| **角色与桌面场景** | SpriteForge 图状态机、PixiJS 场景运行时、口型和情绪同步，并通过 Wallpaper Engine / Lively 桥接到桌面壁纸。 |
| **Provider Runtime** | 在统一事件和权限边界后接入 OpenClaw、Browser Provider 与 Locus；主对话无需亲自完成所有工具工作。 |
| **持久任务控制面** | SQLite Work Ledger、独立 RunAttempt、Continue / Retry / Resume、重启恢复、Task Dock、Artifact Registry 与结构化 Diff。 |
| **用户控制与审计** | 作用域权限对象、一次性本地导出、过期操作保护、受控本地动作入口，以及叙述层与原始工具日志分离。 |

<details>
<summary><strong>展开技术细节</strong></summary>

### 实时对话

- `TurnCoordinator` 统一管理轮次身份、确认与作废、聊天、TTS / 播放 epoch，以及真正的播放完成时刻。
- 用户插话时会同时中止模型输出、待合成语句和当前音频；epoch 机制阻止旧文本或音频在中断后重新出现。
- LLM 流式输出经标签解析与分句后立即进入 TTS；有界队列、首句优先和播放余量估计负责延迟与连贯度之间的取舍。
- 当前可连接 DeepSeek、Gemini、AWS Bedrock、OpenAI 兼容接口和本地模型服务；低延迟语音能力与 [Aqua-TTS](https://github.com/Lucas1479/Aqua-TTS) 协同开发。

### 具身与渲染

- Python 后端只发送结构化渲染事件，前端运行时负责高频动画，避免后台调度抖动直接进入角色表现。
- 播放振幅驱动口型，语句生命周期驱动字幕、表情和说话状态。
- 壁纸或渲染页面重新连接后，可恢复当前表情、字幕、画布、说话状态与行为图节点。

### 工作与浏览

- 同一个 `WorkItem` 可以包含多次 `RunAttempt`；进程退出成功不等于用户目标已经完成。
- Completion Evaluator 会结合任务状态、文件、Diff 和外部产物，判断完成、部分完成或仍被阻塞。
- 浏览器技术会话可跨页面导航保持连接；每个语义任务使用独立 Interaction Branch，避免旧页面目标污染新请求。

</details>

## 系统架构

点击下图查看 1800 × 1120 原始尺寸：

[![Amadeus 系统架构：用户界面、核心运行时、执行 Provider，以及 Locus 对 Claude Code 和 Codex CLI 的接入关系](./assets/architecture-overview-crt.svg)](./assets/architecture-overview-crt.svg)

### Locus 与 Claude Code / Codex

Locus 不是第三个 Coding Agent，也不是另一个模型。它是 Amadeus 面向 Coding CLI 的稳定网关：

```text
Amadeus
  → Locus Local Job API
      → runtime.id = claude-code  → Claude Code CLI
      → runtime.id = codex        → Codex CLI
```

它的运行时切换角色与 CC Switch 有相似之处，但职责更靠近执行内核：除选择 CLI 外，Locus 还吸收不同 CLI 的协议和流式输出差异，并提供稳定 Job API、持久事件序列与断线恢复。Amadeus 因此只消费统一的状态、工具事件、终端输出、文件产物、Diff 和最终结果。

## 当前边界

| 范围 | 状态 |
|---|---|
| 实时对话、角色具身、桌面场景 | 已在内部版本与演示中运行 |
| OpenClaw / Browser Provider | 已有端到端原型与任务控制面 |
| Locus → Claude Code / Codex | 接入层和运行时选择已实现，仍在整理公开配置与验收 |
| 自动 Worktree 隔离 | 代码与离线测试已完成，默认开关仍关闭，等待完整真机验收 |
| 通用屏幕 / 窗口 / 摄像头视觉 | 浏览器观察链已工作；通用视觉上下文仍在收敛 |
| VN Player 与游戏陪玩 | 后端 MVP 已存在，前端控制与通用 Hooker 桥仍在开发 |
| 可安装公开发行版 | 尚未完成依赖、素材、模型、配置与许可证边界整理 |

“内部已经实现”不等于“已经整理成可复现的公开发行版”。首次源码发布会优先保证边界清晰、配置诚实和最小可运行。

## 开源进度

- [x] 建立公开仓库
- [x] 发布中英双语项目介绍、演示画面与系统架构
- [ ] 冻结首次公开版本的功能与依赖边界
- [ ] 完成敏感信息和第三方许可证审计
- [ ] 移除或替换个人角色、语音、模型及本地运行资产
- [ ] 整理安装流程、配置示例和最小运行文档
- [ ] 发布第一个可运行源码版本
- [ ] 确定并发布开源许可证

首次源码版本预计包含核心运行时代码、必要配置示例和基础使用文档。API Key、Token、个人配置、会话数据、模型权重、日志、缓存及未获得再分发许可的素材不会随源码发布。

## 仓库历史与许可证

本公开仓库的提交历史从开源准备阶段开始。此前的内部研发历史保留在非公开仓库中；正式源码会作为经过整理和审计的公开版本提交到这里。

本仓库目前尚未发布源码，也尚未选择开源许可证。在许可证随首个源码版本正式公布之前，请勿将当前仓库视为已经授予代码或媒体素材的使用、修改或再分发许可。

## 相关项目

- [Aqua-TTS](https://github.com/Lucas1479/Aqua-TTS)：为 Amadeus 优化的 GPT-SoVITS V3 实时推理运行时
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)：语音合成推理基础

欢迎 Star 或 Watch 本仓库，关注源码发布进度。

---

<div align="center"><em>El Psy Kongroo.</em></div>
