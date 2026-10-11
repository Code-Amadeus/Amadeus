# 插件模块实施计划与实验验收

2026-10-11；实验分支 `codex/extension-pilot`，基线 `6147acb`。
本版替代原热切换提案，采纳[审计](extension_module_implementation_plan_review_2026-10-11.md)与用户后续决定。
实验代码尚未合并或发布，不宣称完整 v0.1 合规。验收结果见[施工记录](extension_experiment_execution_2026-10-11.md)。

## 1. 交付范围

P1 验证 TTS、MCP 和 Agent Skill 三种接入。五份独立包使用同一安装生命周期；Skill 补充验收见[专项记录](extension_skill_experiment_2026-10-11.md)。
新增 CSV 审核 Skill 与 DeepSeek Harness ACP 真模型验收见[实机记录](extension_dsh_live_2026-10-11.md)。
ACP 文件目录投递与各 Harness 的原生 Skill 注册分开验收；DSH 原生注册对照使用 Agent 自有配置，不将其冒充通用 ACP 方法。

完整桌面抽样已运行，见[桌面真机记录](extension_desktop_live_2026-10-11.md)：Chat → Work → DSH → Skill → 文件、重启续接和 UI 更新生效通过；普通 JSON/CSV 产物尚不能从当前 AUIP 产物列表打开。
下一优先级是复用已有 Work artifact 登记补齐普通文件交付入口及桌面验收，再完善原生 Skill 发现，随后进入记忆阶段。当前不标记桌面端到端全部通过。

| 包 | 原生接入点 | 作用与限制 |
|---|---|---|
| MiMo | BaseTTSBackend 代理 | 英语语音。用户确认不支持日语，取消日语试听待办。真实英语验收因未配置 key 留待后续。 |
| Fish Audio | 同一个 TTS 代理 | 日语/英语，MessagePack WebSocket、44.1 kHz PCM；已有一次真实日语合成，音质由用户试听。 |
| Notes MCP | MCP connection → 兼容 Work Provider | 列出、读取、保存包自己目录内的笔记；不是 Amadeus 语义记忆，Main Chat 没有工具权限。 |
| Handoff Skill | Codex / ACP / Pi | 安装一次、选择 Agent；原生发现/实际资源读取/产物/恢复/更新/卸载均有测试。 |

样例位于 `examples/extensions/`，不导入 Host 内部实现。共同语义为**保存后重启后端生效**。
Skill 已从 AUIP 专用路径扩展为独立声明式资源，绑定兼容 Agent；本次 ACP 实测目标是 Codex ACP。
拒绝 ASR、context_source、observer、proactive、第三方 UI 和任何 Host 权限。
不引入 Grant 存储，不做市场、源码安装、自动更新、广泛目录发现或 OS 沙箱。允许用户显式安装同 ID 的另一版本，确认停机后回收旧环境。

## 2. 当前代码归属

| owner | 路径 | 不变量 |
|---|---|---|
| 安装事务 | electron/src/main/extensions.ts | main 唯一负责准备、独立 venv、wheel 安装、提交和删除；Python helper 只检查/验证。 |
| 期望状态与密钥 | electron/src/main/desktopSettings.ts | 复用可靠写入、备份与 safeStorage；renderer 不回读密钥；单一可写期望状态。 |
| 启动快照 | server/extensions/snapshot.py、runtime.py、server/app.py | 原始快照尽早移出环境；独立验证记录，失败不启动包；启动时一次登记。 |
| Runner/SDK | server/extensions/runner.py、sdk/extension-python/ | JSON-lines 握手、贡献核对、截止时间、取消、流量上限、有界恢复；不重放合成。 |
| TTS | server/extensions/tts.py、tts/backend.py、tts/registry.py | 进程内代理身份不变；重启应用新配置，pipeline、缓存和调度保留原 owner。 |
| MCP | agent_host/extension_mcp.py、mcp_connections.py、mcp_stdio.py | 显式 Provider 绑定；Host probe 受控进程树；实际 Provider session 沿用原生生命周期。 |
| 子进程 | config/subprocess_environment.py、config/process_tree.py | 平台变量白名单加该包配置；Windows 子进程先暂停、加入 Job 再运行。 |
| UI/目录 | ExtensionSettings.tsx、extensionSettingsCatalog.ts、server/capability_composition.py | 安装/配置/启用/待重启/不可用/移除可见；目录仅投影，不取得执行权。 |

`TTS_BACKEND` 仍是唯一持久选择，仅该字段允许验证过的完整贡献 ID，其他 identifier 不放宽。
内置与扩展切换、disabled 启动再选扩展都在重启生效；不需改变 `tts/pipeline.py` 全局读取或 `configure(None)`。

## 3. 完整生命周期

1. **检查和安装。** 用户选择本地 wheel；复制后检查同一字节的清单、entry point、模块路径、API、kind/contract 和权限。
   静态检查不导入包代码。独立 venv、仅 wheel 依赖、可信 SDK 版本约束。成功后默认禁用。
   失败无半提交；不能确认准备进程停止时保留目录，不误删仍在运行的文件。
2. **配置和选择。** 普通字段与密钥分开校验保存；缺必需密钥为 needs_config，safeStorage 不可用拒绝保存密钥。
   配置、启用、Provider 绑定、TTS 选择均待重启。包没有 Host 权限，不保存 Grant。
3. **启动和装载。** main 生成快照，Python 只读消费；TTS Runner 注册到稳定代理，声明式 MCP 进原生 registry。
   不扫磁盘恢复信任。暂时无法运行验证器时保留记录/密钥、显示 unavailable、不激活、不误报 revision 已应用。
4. **运行。** Host 管语言、播放、字幕、口型和取消；TTS 仅返回音频。speed 为可忽略提示，MiMo 不被要求实现
   GPT-SoVITS 节奏参数；Fish 映射自己的 prosody。序号、采样率、流量、截止时间有界校验；迟到音频丢弃。
5. **故障。** 暂态 Runner 崩溃有界恢复，协议/注册错误停止恢复，付费请求不重发。
   合成失败继续文字，无静默远程 fallback。保存配置不在旧进程内创建第二个 TTS 实例。
6. **禁用和卸载。** 允许移除当前选中的包，保存待重启；确认后端停止才删代码和密钥。
   保留原选择并显示不可用，继续文字。默认保留独立数据目录，显式勾选才删数据；重装不恢复旧密钥或启用状态。
7. **重启恢复。** 按当前期望状态重新登记，移除包不再注册。损坏沿用现有设置备份/恢复策略；
   默认设置没有启用包。验证器暂不可用不能导致无关设置保存永久抹掉记录。

Windows Job 的异常 Host 退出回收已有实测；POSIX 异常退出同等保证尚未认证。
Provider 自行启动的 MCP 进程仍由 Provider 管理，不能把 Host probe 的证据扩充为所有 Provider 保证。
扩展 MCP 在目录中保留包归属，不重复显示为手动连接。

## 4. 验收门槛

| 用户结果 | 证据 |
|---|---|
| 安装不执行清单、不混用核心依赖、失败无半提交 | 校验器/安装器故障测试，真实 venv + pip 安装三包 |
| 保存/重启一致，密钥不回读，失效选择可见 | desktopSettings、IPC、renderer、production Settings smoke |
| 不串 Host 凭据 | 环境白名单、合成秘密哨兵、错误输出及独立子进程测试 |
| 流式发声、取消、异常无重复请求 | Runner/代理、本地 Fish WS 故障服务、一次真实 Fish 日语 WAV |
| MCP 符合产品权限语义 | 实际 list/read/save、路径拒绝、Codex 配置投影、Main Chat 无权限 |
| 卸载和重装可靠 | 延迟删除、默认保留、重装不恢复凭据、显式删数据 |
| 已有功能回归 | 隔离全仓 Python suites、Electron 测试与 production build |

单次 Fish 时间仅是一个样本，不称 p50/p95 或性能门槛已达成。
不声称已完成人工 Chat/Wallpaper/VN 听感、真实 Codex/ACP Work 回合、macOS/Linux 或签名分发验收。
这些列入发布前门槛；实验通过不等于稳定 SDK。

Skill 会话保存所绑定资源的 revision；相同版本可恢复，更新/卸载/重装后旧会话在模型调用前拒绝恢复并要求新会话，其他 Agent 不受无关绑定变化影响。

## 5. 后续顺序

先审阅 P1 接口和生命周期证据再决定合并。发布前确定 SDK 分发/许可证和平台，补真实 Provider 启停及三种发声界面人工体验。
MiMo 英语实机验收独立安排，日语不再测试。内置 MiMo 保留；迁移移出 P1。
真正移除时才显式迁移有效 `MIMO_TTS_BASE_URL`、`MIMO_TTS_API_KEY`、`MIMO_TTS_MODEL`、`MIMO_TTS_VOICE`，
覆盖 CLI/env 使用者，不编辑用户 .env。

## 6. 记忆衔接（只记录）

顺序 **M0 → M2 → M1**：

- **M0：Host 有来源召回。** 当前 CooperativeProviderLoop.role_reference 是每轮主回答前检索，
  不是仅首句、Hybrid 本地首句或 Provider 回执续接。改为 frame 独立资料字段，排在稳定历史之后；
  有截止时间，普通失败/超时为空并记录 dropped。现有 RAG 已捕获常见异常；取消、权限和作用域失效不能吞掉。
  避免重复召回窗口已带入的对话；Draft 价值用真实长会话验证，不把检索本身当收益。
- **M2：Host 显式记忆。** 先定义记住/查看/纠正/遗忘/导出及个人共享、角色、Project 范围；
  重建索引不能复活已遗忘项，Work Ledger 仍拥有任务事实。
- **M1：外部记忆贡献。** 等上述语义稳定再补 provisional 历史访问和管理契约。
  外部包不能直读 Host 数据库，Notes MCP 不自动获得这些权限。

“上下文不进 system prompt”保护指令/资料边界：首轮避免检索资料获得 system 权威，后续轮还可
避免变化召回破坏稳定历史前的公共前缀。缓存收益取决于实际 token 前缀而非角色名，改成 user 但仍
放历史前并未解决。结构分离也不等于免疫注入，仍需来源、作用域、长度预算与截止时间。
生成开始后不追注新资料；Provider 回执不能借记忆另改账本事实。

社区 #97、#146 列为需求核对和回复待办，本次未发布回复；应说明先定义 Host 范围/纠正/遗忘，再开放接口，
不承诺日期或把 Notes 工具称为长期记忆。既有 Provider/MCP 环境凭据继承问题独立记录；
Runner 白名单不等于整个应用的恶意代码沙箱。
