# 扩展实验施工约定与验收记录

实验分支：`codex/extension-pilot`，基线 `6147acb`。实施计划已同步采用重启生效。

用户确认当前重点为插件/接口模块；记忆先记录。2026-10-11 补充要求：验证常用、符合 Amadeus
语义的其他社区接口，以及安装、装载、运行和卸载的完整生命周期，不能只证明一个简单 TTS 样例。

## 已采纳的施工决定

- 安装、配置、启停、选用和卸载均保存后待后端重启生效，与 MCP/ACP 和内置 TTS 一致。
  后端进程内 TTSRuntimeAdapter 身份保持不变；不为了插件改造 pipeline 热切换。
- Electron main 唯一拥有安装事务、持久扩展记录、密钥和 pending revision。Python 只消费
  启动快照并管理 Runner/原生注册，不拥有另一份可写安装记录。
- 首阶段支持 `voice_backend` TTS 与声明式 `mcp_server`。后者仍由用户选择可消费的 Work
  Provider，绝不暴露给 Main Chat。Skill 已核对，缺少通用消费者，暂不支持。
- `permissions.host` 非空拒绝。暂不保存 Grant、实现观察者或历史读取服务。
- 显式本地 wheel 安装，独立环境；文件检查不执行包代码。运行环境由白名单从空构造，不继承
  Host 全部环境。启动快照读后从环境移除。包只收到自己的密钥；日志/错误不得返回原始密钥。
- MiMo 仅面向英语输出；用户确认不支持日语，日语试听待办取消。真实英语验收等待配置 key。
  速度是可忽略的提示，不能强加 GPT-SoVITS 内部启发值。内置 MiMo 保留，迁移不属于本试点。
- Fish Audio 为第二个独立 TTS 包，支持日语/英语。使用相同公共 Runner/TTS 契约，包内适配
  MessagePack WebSocket；Notes MCP 则验证声明式原生接口与数据生命周期。
- Runner 崩溃的有界恢复不重发失败的付费请求；迟到音频丢弃，取消可靠关闭资源。
- package 的记录缺失/恢复默认时没有启用项，不扫磁盘重建信任；沿用已有设置备份与损坏留证。
- 不公开发布、发送社区回复或新建公开安全 issue。既有 MCP 凭据问题可在相关 owner 做必要
  修复并留下脱敏本地报告；发布须另获授权。

## 并行实现边界

1. Python worker：`server/extensions/`、`tools/extension_package.py`、所需 schema、
   `sdk/extension-python/`、TTS 代理、app/composition/status 集成及其 Python 测试。
2. Desktop worker：`electron/src/main/`、`electron/src/preload/`、共享 extension 类型、
   `electron/src/renderer/env.d.ts`、设置持久化/安装/IPC 与对应测试。
3. UI worker：renderer 的扩展设置入口、表单、状态、语音选择、翻译及 UI 测试。
4. 父代理：文档/协议、真实样例包、MCP/Skill 原生消费接缝、整合、审计与完整验收。

所有 worker 共享此 worktree，互相保留修改。交界改动先沟通，父代理负责最终审计与取舍。

## 记忆部分记录（不在本次代码范围）

顺序改为 M0 → M2 → M1：先 Host 的有来源召回与显式记忆，再稳定外部接口。
system 保存规则，人设与语言设置仍归 Host；具体召回资料进入 frame 的独立数据字段，放在
稳定历史之后。当前 role_reference 是每轮主回答前的检索，不属于 Hybrid 独立本地首句，
也不自动接入 Provider 回执叙述。同一次生成开始后不会再注入新资料。
可选检索有截止时间、普通失败返回空并记录 dropped；取消、权限或作用域失效不能吞掉。
现有 RAG 已捕获常见加载/搜索异常，不能把它误写成完全无容错。缓存收益来自相同输入前缀，
不是 system/user 标签，仍受历史窗口和 Provider 实际缓存机制约束。来源优先级、纠正、
遗忘、跨角色/Project/Draft 范围与历史读取契约须在未来工作中验证。

## 2026-10-11 实验验收

本地独立源码包：MiMo、Fish Audio、Notes MCP；公共 SDK 在 sdk/extension-python。
三个包经真实 Desktop ExtensionInstaller 创建独立 venv、pip 安装、校验和初始化。
安装/配置使用合成凭据；只有单次 Fish 实机调用从现有本地配置读取凭据，仅在内存和 initialize 管道中传递，
不保存或输出真实密钥/音色 ID。实际 safeStorage 加密由 Electron 单测替身覆盖，真实操作系统密钥库实测不作此声明。

| 已完成实验 | 观察 |
|---|---|
| Fish 日本语真实合成 | 1 次；44100 Hz、5 块、380928 字节 PCM、4.319 秒音频；启动 0.735 秒、首块 2.375 秒、完成 4.203 秒 |
| Fish 合成后关闭 | Runner 已退出；没有重试、静默备用请求或 Host 私有导入 |
| Fish 本地 WS 协议 | 17 项通过：双向流、奇数字节续接、超限/超时/错误、取消和无重试 |
| Notes 原生消费 | list/read/save 成功，路径越界被拒绝；绑定 codex，Main Chat 无权限；关闭进程无残留 |
| 三包移除/重装 | 先确认停止再删环境；默认保留数据；重装恢复原数据目录但不恢复密钥/启用；显式数据删除成功 |
| Settings production smoke | 英文桌面/中文深色 560px；表单有标签、密钥遮蔽不回读、失败保留编辑、待重启、移除失效选择，无浏览器错误 |
| 协议仓 | 61 passed / 1 skipped；MiMo 英语、provisional 状态、重启 profile 与 README 对齐 |

报告：output/diagnostics/extensions/fish-extension-live-report.json；
音频：output/diagnostics/extensions/fish-extension-ja.wav；
本地安装/消费报告：build/extension-desktop-smoke/case-dAvnKE/；
Settings 报告及截图：output/diagnostics/extension-settings-ui/。
这些是本地诊断产物，不发布或提交用户配置。

实际 Codex 配置投影与 Host MCP 调用并不等于真实 Codex/ACP Work 会话验收。
Fish 时间为单一样本，不代表 p50/p95；日语音质仍待用户试听。
MiMo 英语真实服务尚未验证；MiMo 日语明确不支持，无此待办。
Windows Host/子孙进程异常退出已验证；POSIX 同等保证没有认证。

## 父代理审计与修正

- 安装目录、entry point、Runner 注册需核对同一身份，静态验证不能执行包代码。
- SDK 和验证器 JSON Schema 必须进各自 wheel；增加实际 wheel 加载验证。
- 取消接回现有 TTS interrupt owner；取消失败不能阻止停止播放。第一音频块之后不重复整句字幕。
- 突发合法音频使用有界背压；协议/注册错误停止恢复；崩溃恢复不重放请求。
- Windows Runner 与 扩展的 Host MCP probe 在执行前加入 Job，退出关闭整个进程树。MCP framing 使用官方 SDK 消息类型。
- 验证器短暂不可用保留记录/密钥，不在无关保存时抹掉；跳过激活仍保留待应用状态。
- 卸载保留数据为默认，重新安装不恢复信任。备份仍引用的环境不能作为孤儿误删。
- 原生 MCP 投影保留扩展来源，避免目录重复为手动连接；Windows PATH 查找遵守大小写不敏感。
  手动连接保留官方 SDK 的 Windows npx.cmd 支持，仍过滤继承环境；扩展连接使用先暂停再入 Job 的 transport。
- 修复新增导入越过 OpenMP 初始化的集成错误；关闭流程提取测试补齐 extension_runtime owner。
- UI 中文字典去除重复 key。首轮 full suite 的长临时路径触发 Git filename-too-long，
  换短路径后相关 51 项通过；没有为测试环境添加产品绕过。

## 发布前尚未覆盖

真实 Work Provider 会话的启停/退出、Chat/Wallpaper/VN 人工听感、其他平台、SDK 分发和许可证安排。
既有 Provider 启动环境的跨服务密钥继承需独立审查；本次白名单不构成全应用或 OS 沙箱保证。
这些边界不影响记录当前 Windows 实验结果，但不应被略去而宣称 SDK 稳定或全 v0.1 合规。


## 最终回归结果

- Python：四个隔离分片全量执行；修正 VTS 关闭测试的缺失依赖后，受影响的关闭/启动套件复测 45 passed。
  去重汇总为 **5666 passed / 17 skipped / 5 xfailed**。原始分片报告保留两项修正前的夹具失败，未涂改日志。
  MCP 兼容性测试在分片 collection 后增加，实际执行 7 项而旧 collection 记录 6 项；按执行结果计数。
- Electron：**425 passed / 0 failed**；production build 成功；最终 production Settings smoke 成功。
- 协议仓：**61 passed / 1 skipped**。
- 修改的 Python 文件 Ruff 全通过，Git diff whitespace 检查通过。
- 日语音频与敏感配置分开保存；所有真实安装的实验环境及按明确选项删除的数据已经回收。

实验完成，不代表发布认证；发布前限制仍如上所列。记忆代码、内置 MiMo 迁移与社区发布没有进入本轮施工。
