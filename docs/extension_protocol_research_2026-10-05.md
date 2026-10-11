# Amadeus 扩展协议调研与设计提案

初稿 2026-10-05；2026-10-11 根据审计和实验实现更新。实验分支 `codex/extension-pilot`，
产品基线 `6147acb`；扩展协议仓基线 `2fab8bd` 加未提交草案。没有发布 SDK 或完整 v0.1 合规声明。

## 0. 当前结论

- 首阶段验证 TTS、声明式 MCP 和 Agent Skill 三种接入，共享包身份、安装、配置、重启和卸载。
  MiMo 仅英语，用户确认不支持日语；Fish Audio 验证日语/英语；Notes MCP 验证自己的笔记数据生命周期。
- 统一保存后重启生效，和内置 TTS、MCP、ACP 一致。进程内稳定 TTS 代理管理 Runner 恢复，
  不修改 pipeline 做热切换，不实现配置 generation 交接。内置 MiMo 用户迁移移到真正删除内置后端时。
- Electron main 唯一拥有安装事务、加密密钥和期望记录；Python 只消费启动快照并拥有运行期。
  拒绝任何 Host 权限，不保存 Grant。复用现有设置恢复策略，验证器暂时不可用则保留记录。
- Runner 环境从白名单构造，仅注入自己的密钥；快照移出后端环境。此举防止自动继承，不是 OS 沙箱。
- MCP 和 Skill 只投影到选中的兼容 Work Provider，Main Chat 没有工具权或 Skill 正文。
  Skill 已验证 Codex 原生发现、ACP 任务目录和 Pi 原生加载；记忆、observer、ASR 和主动行为仍拒绝。
  具体消费差异、更新和旧会话边界见[Skill 专项记录](extension_skill_experiment_2026-10-11.md)。
- TTS/MCP/Skill 均保留 provisional，实验不自动将其升为 normative。公开稳定性需通过约定的完整验收。

详细 owner、边界、测试与后续门槛以[实施计划](extension_module_implementation_plan_2026-10-11.md)
和[施工记录](extension_experiment_execution_2026-10-11.md)为准。

## 1. 当前代码接缝

| 边界 | 当前实现路径 |
|---|---|
| 单一设置与安装事务 | electron/src/main/desktopSettings.ts、extensions.ts |
| 验证与只读启动记录 | server/extensions/package.py、manifest.py、snapshot.py、runtime.py |
| 公共 Runner 与 TTS | server/extensions/runner.py、tts.py；sdk/extension-python/ |
| 原生 MCP 投影与进程 | agent_host/extension_mcp.py、mcp_connections.py、mcp_stdio.py |
| 凭据与进程树 | config/subprocess_environment.py、process_tree.py |
| 设置与能力目录 | ExtensionSettings.tsx、extensionSettingsCatalog.ts、server/capability_composition.py |
| 独立样例 | examples/extensions/mimo-tts、fish-audio、notes-mcp |
| 未来记忆接缝 | server/cooperative_provider_loop.py 的 role_reference；core/character_rag.py；Session/Work/Artifact owner |

配置 catalog 仍只声明随应用发布的可信内置能力；树外记录不插入静态白名单。
TTS_BACKEND 单独接受经验证的 qualified ID。目录是读模型，不能变成另一份路由或执行真相源。

## 2. 同类项目调研（2026-10-05 历史资料）

本节保留原调研的外部项目比较和来源，未在 10 月 11 日重新检索；项目状态、数量与事件描述
仅代表原调研记录，不是本轮对外部产品的最新核验。施工范围由当前代码和下文试点决定。

### 2.1 对比

| 项目 | 扩展单元 / 清单 | 运行与隔离 | 生命周期 / 热插拔 | 权限 | 分发 |
|---|---|---|---|---|---|
| **AIRI**（moeru-ai，约 5 万星，同类 AI 角色项目） | `extension.airi.json` v2：`engines.airi` semver、按运行时区分的 `entrypoints`、`permissions`、`kits` | Plugin Host 可以嵌在 Electron 里，也可以是独立 Node 进程或远程服务；远程插件可以用任何语言经 WebSocket 接入；控制面和数据面分离 | 能力注册表加 readiness gate，缺依赖时进入 `waiting-deps` | 申请 ∩ 授予取交集（apis / resources / capabilities / processors / pipelines） | 仍处于设计阶段（文档标注 “Active design”） |
| **MaiBot**（约 6 千星，中文 AI 陪伴） | `_manifest.json` + `plugin.py` + `config.toml`；`pip install maibot-plugin-sdk` 是唯一依赖 | Host 加两个子进程（内置插件 Runner、第三方插件 Runner）；`ctx.*` 全部转为 RPC；规定插件“不得直接 import `src.*`” | 热重载时先拉起新 Runner，ready/health 通过后才切到新 generation，失败就回滚旧 generation | manifest 声明 `capabilities`，Host 为每个插件签发 capability 令牌 | WebUI + 市场 |
| **Neuro SDK**（VedalAI） | 游戏用 `actions/register`（带 JSON Schema）、`context`、`actions/force`、`action/result` | 外部进程 | `startup` 清空已注册动作；动作可以动态注册和注销 | 游戏自己校验动作；约 20 秒超时 | 社区游戏集成 |
| **SillyTavern** | UI 扩展 `manifest.json`（`loading_order`、`minimum_client_version`、`generate_interceptor` 等）；服务端插件 | UI 扩展和前端同页运行，没有隔离；服务端插件默认关闭，官方说明它“不沙箱，可以访问整个文件系统” | 通过 git URL 安装 | `getContext()` 是稳定 API；`extensionSettings` 对所有扩展可见，并以明文存储 | git |
| **Koishi / Cordis**（数千个社区插件） | npm 包 `koishi-plugin-*`；`package.json` 的 `koishi.service.required / optional / implements` | 进程内；Context / Service / `inject` 依赖注入 | 所有注册都是可逆 effect，卸载时自动逆序清理；HMR 失败时事务回滚 | 依赖的是“服务”，不是具体实现 | 市场自动索引 npm（约 15 分钟），没有人工审核 |
| **DeepSeek Harness**（基于 Cordis，“Everything is a Plugin”） | npm bundle；分 host 半和浏览器半的双半包 | Host 代码在进程内运行，文档明确说它“在 workspace sandbox 之外执行” | HMR；安装失败会恢复 lockfile；下载前先检查 peer 兼容性 | 依赖的构建脚本需要用户批准；版本豁免需要显式 `acceptRisk` | npm，带镜像回退 |
| **ComfyUI** | `custom_nodes`、Registry（`[tool.comfy]`）、Nodes v3 与版本化的 `comfy_api` | 进程内。官方 2025-06 的文章把进程隔离列为方向；Comfy-Org 维护的 pyisolate 提供每个扩展一个 uv venv、JSON-RPC、零拷贝 torch 张量和 Linux bubblewrap | — | — | Registry + Manager |
| **Dify** | `manifest.yaml` 声明反向调用权限（tool / model / node / app / endpoint / storage） | 独立的 plugin daemon | — | 声明式权限；自托管版默认强制校验签名 | Marketplace |
| **MCP 生态** | MCPB（`.mcpb` 包：`server.type` 取 node / python / binary / uv；`user_config` 中的 `sensitive` 值存进系统密钥库）；MCP Registry；MCP Apps | stdio / HTTP 外部进程；MCP Apps 用沙箱 iframe | — | 由 Host 侧审批 | 元索引：reverse-DNS 命名空间 + GitHub OIDC，条目指向 npm / PyPI / Docker / GitHub Release |
| **Claude Code 插件 / Agent Skills** | `plugin.json` 把 skills、agents、hooks、MCP、LSP 打成一个包；Agent Skills 于 2025-12 成为开放标准 | 每个组件按自己的原生方式运行 | — | — | marketplace 就是一个带 `marketplace.json` 的 git 仓库 |

其他参照：

- **VS Code**：`contributes` 和 `activationEvents` 实现声明式贡献与懒激活；扩展运行在独立的
  Extension Host 进程；用 `engines.vscode` 声明兼容版本。
- **Home Assistant**：config entry 的 `async_setup_entry` / `async_unload_entry`、config flow，以及
  社区商店 HACS。
- **Obsidian**：`register*` 注册的资源会自动清理；社区插件通过向 `community-plugins.json` 提 PR 来
  上架和审核。
- **JupyterLab 3 起的 prebuilt 扩展**：pip 包里直接带前端 bundle，用户不需要重新构建。
- **Zed**：WASM 扩展，配合 `granted_extension_capabilities`（例如 `process:exec`、
  `download_file`）。
- **AstrBot**：`metadata.yaml`、市场 JSON 规范，可在 WebUI 里重载插件。

**反面案例：ClawHub。** Koi Security 于 2026-02-01 披露了 ClawHavoc 投毒事件；Antiy 统计
ClawHub 历史上至少出现过 1,184 个恶意 skill。攻击没有利用漏洞，而是在 SKILL.md 的 “Prerequisites”
或 “Setup” 段落里诱导用户执行命令或下载加密压缩包，投递 AMOS 窃密木马。OpenClaw 随后接入了
VirusTotal 扫描并每日重扫，同时承认这“不是银弹”。

### 2.2 借鉴什么，避开什么

借鉴：

1. **声明先于代码**（VS Code、MCPB、Koishi、MaiBot）：不运行扩展代码，就能列出、配置、审查权限、
   显示健康状态。
2. **能力代理，而不是 import 核心**（MaiBot 的 `ctx.*`、SillyTavern 的 `getContext`、Cordis 的
   `ctx.<service>`）：SDK 是唯一依赖，核心重构不会打碎扩展。
3. **可逆注册**（Cordis effect、Obsidian `register*`、HA `async_on_unload`）：卸载和热重载由结构
   保证。
4. **generation 切换**（MaiBot）：新实例 ready 之后才切流量，失败时保留旧实例。
5. **申请 ∩ 授予**（AIRI、Zed、Dify）：生效的权限永远不会比任何一方更宽。
6. **配置 schema 自动生成界面，secret 进系统密钥库**（MCPB `sensitive`、MaiBot `config_model`、
   Koishi Schema）。
7. **元索引，而不是包托管**（MCP Registry、Koishi、Claude Code marketplace）：直接复用 PyPI、npm、
   GitHub 的托管和来源证明。
8. **版本化公共 API 加一致性测试**（ComfyUI `comfy_api`、AIRI `engines`、DeepSeek Harness 由源码
   生成并校验的 API 目录）。
9. **控制面和数据面分离**（AIRI）：音频流不要和生命周期消息挤在同一条通道里。

避开：

1. **默认在进程内运行任意代码**（ComfyUI、SillyTavern 服务端插件）。这样生态起量快，但依赖冲突和
   “一改核心就打碎插件”的债，要花很多年去还。
2. **能任意改写 prompt 的拦截器**。SillyTavern 的 `generate_interceptor` 在生成前拿到即将用于构建
   prompt 的整段聊天历史，并且可以修改它。放在 Amadeus 里，这会直接破坏 Main Chat 的人格和权限边界。
3. **扩展之间共享明文设置**（SillyTavern 的 `extensionSettings`）。
4. **把扫描当作信任**（ClawHub）。扫描只是其中一层，还需要来源证明、信任分级，以及“声明不等于
   强制”的诚实提示。

## 3. 与 ROADMAP 和记忆的衔接

顺序 M0 → M2 → M1。先 Host 有来源的有限召回，再定义显式记忆、范围、纠正和遗忘，最后开放外部记忆贡献。
当前 role_reference 在每轮主回答生成前检索，不是仅第一次讲话，也不负责 Hybrid 本地首句或 Provider 回执。
具体资料进入 frame 独立字段，放在稳定历史之后；system 保留规则和身份。
首轮收益是指令/资料边界，后续轮还减少动态资料对缓存前缀的破坏；收益来自相同 token 前缀而非消息角色名。
普通检索超时/失败为空并留下诊断，取消/权限失效不能吞掉。RAG 已处理常见加载/搜索异常，不误报完全无容错。
Draft 当前会话召回先证明超过对话窗口的有效命中，避免把已有历史再检索一遍。Notes MCP 不等同于语义记忆。
社区 #97、#146 需求核对与回复另列待办，本次不发消息。

## 4. 实验边界与后续

本地 wheel、安装记录、Runner/TTS、MCP 原生投影已有实验代码；不是发布功能或全平台认证。
真实 Fish 日语已合成一次，MiMo 英语实机仍待 key，MiMo 日语不再测试。时间样本不构成 p50/p95 或性能承诺。
发布前还需真实 Codex/ACP Work 回合、Chat/Wallpaper/VN 人工体验、支持平台和 SDK 分发/许可证决策。
Host probe 的进程回收不能代替外部 Provider 的进程生命周期验收，既有跨 Provider 凭据继承另行处理。
保留旧内置调用方，迁移与删除各自有退出条件。不发布包、不联系作者、不自动回复社区。

## 2026-10-11 后续实现对照

普通文件与 AUIP 应用共用现有 Work artifact 登记；主聊天产物页面通过 `work.artifacts.list` 显示两类结果，
`work.artifact.read` 只接受 Work/artifact ID，校验当前登记版本、工作区及 SHA-256，再提供有界 UTF-8 文本。
不把模型报出的路径或完成说明提升成读取权限、登记事实或用户验收。

ACP 仍无通用 Skill 安装 RPC。显式 DSH 适配使用启动末尾的 `--patch` 投影本 Agent 已绑定贡献的目录，
原地加载资源，不复制到全局技能目录。默认 ACP 保持任务目录；DSH 新建预设选择原生适配，已有配置需显式选择。
安装、更新、解绑、禁用和卸载仍由原安装器及 `skill_revision` 边界处理，保存后重启生效。
本轮证据见[交付与原生适配验收](extension_delivery_native_2026-10-11.md)，不据此提升为完整 v0.1 normative 合规。

## 来源

主线与组织仓库：

- [Code-Amadeus/Amadeus-Extensions README](https://github.com/Code-Amadeus/Amadeus-Extensions)
- [Code-Amadeus/AUIP README](https://github.com/Code-Amadeus/AUIP)
- [Code-Amadeus/Amadeus PR #13（MiMo TTS）](https://github.com/Code-Amadeus/Amadeus/pull/13)、
  [Issue #97（C8）](https://github.com/Code-Amadeus/Amadeus/issues/97)、
  [Issue #61（悬浮 Companion）](https://github.com/Code-Amadeus/Amadeus/issues/61)

同类项目：

- AIRI：[plugin platform 架构](https://github.com/moeru-ai/airi/blob/main/packages/plugin-sdk/docs/design/architecture.md)、
  [capability orchestration](https://github.com/moeru-ai/airi/blob/main/packages/plugin-sdk/docs/design/capability-orchestration.md)、
  [manifest 类型](https://github.com/moeru-ai/airi/blob/main/packages/plugin-sdk/src/plugin-host/shared/types.ts)
- MaiBot：[maibot-plugin-sdk](https://github.com/Mai-with-u/maibot-plugin-sdk)（README 与 `docs/guide.md`）
- Neuro SDK：[API specification](https://github.com/VedalAI/neuro-sdk/blob/main/API/SPECIFICATION.md)
- SillyTavern：[UI extensions](https://docs.sillytavern.app/for-contributors/writing-extensions/)、
  [server plugins](https://docs.sillytavern.app/for-contributors/server-plugins/)
- Koishi / Cordis：[发布到插件市场](https://koishi.chat/en-US/guide/develop/publish.html)、
  [Cordis paper](https://github.com/cordiverse/paper)、
  [解读文章](https://www.besthub.dev/articles/the-new-cordis-paper-behind-deepseek-harness-explained-d8ddbffdbb6a)
- DeepSeek Harness：[仓库](https://github.com/deepseek-ai/deepseek-harness)（`docs/cordis-primer.md`、
  `packages/boot/plugin-manager/README.md`）
- ComfyUI：[Dependency Resolution and Custom Node Standards](https://blog.comfy.org/p/dependency-resolution-and-custom)、
  [V3 migration](https://docs.comfy.org/custom-nodes/v3_migration)、
  [pyisolate](https://github.com/Comfy-Org/pyisolate)
- Dify：[manifest](https://docs.dify.ai/en/develop-plugin/features-and-specs/plugin-types/plugin-info-by-manifest)、
  [签名校验](https://legacy-docs.dify.ai/plugins/publish-plugins/signing-plugins-for-third-party-signature-verification)
- MCP：[MCPB manifest](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md)、
  [SEP-1865 MCP Apps](https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp)、
  [Registry server.json 要求](https://glama.ai/blog/2026-01-24-official-mcp-registry-serverjson-requirements)
- [Claude Code plugins reference](https://code.claude.com/docs/en/plugins-reference)、
  [Agent Skills](https://agentskills.io/)
- [AstrBot 插件市场规范](https://docs.astrbot.app/en/dev/plugin-market/)、
  [Zed extension capabilities](https://zed.dev/docs/extensions/capabilities)
- ClawHub：[The Hacker News](https://thehackernews.com/2026/02/openclaw-integrates-virustotal-scanning.html)、
  [Antiy ClawHavoc 分析](https://www.antiy.net/p/clawhavoc-analysis-of-large-scale-poisoning-campaign-targeting-the-openclaw-skill-market-for-ai-agents/)

- [Amadeus 记忆提案 #146](https://github.com/Code-Amadeus/Amadeus/issues/146)
- [Fish Audio WebSocket TTS](https://docs.fish.audio/api-reference/endpoint/websocket/tts-live)
