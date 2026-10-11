# Skill 插件实验：安装一次，选择 Agent

2026-10-11，基于实验分支 `codex/extension-pilot` 的 TTS/MCP 提交 `a24f584`。
本次补齐独立 Skill 包、Agent 绑定、版本更新和会话兼容边界。仍为实验协议，未发布稳定 SDK。

后续已补充 [DeepSeek Harness ACP 真实模型验收](extension_dsh_live_2026-10-11.md)：两种 Skill、两个 MCP 服务和原生网页工具。
下文的 Codex/Codex ACP/Pi 结果仍是本地可控模型；不能把后续 DeepSeek 的真实推理结果扩大到其他 Agent。

## 用户操作

1. 在 Extensions 选择 **Install or update local wheel…**，安装包含 `skill` contribution 的 wheel。
   首次安装默认禁用。样例源码在 `examples/extensions/handoff-skill/`。
2. 打开 Configure extension，勾选 Codex、具体 ACP Agent 或 Pi，启用并保存。
   只有同时支持包内所有 Work contribution 的 Agent 才出现。绑定针对 Amadeus 管理的 Agent 运行，
   不修改各工具的全局 Skill 目录；Main Chat 不加载 Skill 正文或获得工具权限。
3. 重启后端。保存不改变当前运行进程；所选 Agent 在后续执行时发现相应 Skill。
4. 更新时选择同一包 ID 的另一版本 wheel。先在新环境验证，成功才替换期望记录；旧环境在确认后端停止后回收。
   保留启用状态、Agent 绑定、兼容配置和独立数据。准备失败保留旧版本，不自动下载更新。
5. 卸载仍默认保留数据，可明确删除。重装默认禁用、不恢复绑定/密钥。

## 三种消费方式

| 路径 | 发现与使用 | 本次实际运行目标 |
|---|---|---|
| Codex App Server | 进程级 `skills/extraRoots/set` + `skills/list(forceReload)`，核对名称和绝对路径；原生渐进加载正文 | 本机 Codex 可执行文件 + official Python SDK |
| ACP | 通过标准 `session/prompt` 提供所选 Skill 的名称、描述和位置；Agent 用原生文件工具读取正文与相对资源 | 用户选择的临时 `@agentclientprotocol/codex-acp@2.2.2`，调用本机 Codex |
| Pi | 每个选中 Skill 传 `--skill`；`get_commands` 核对实际 `sourceInfo.path`；原生 read 按需取正文 | 锁定的 `@earendil-works/pi-coding-agent@0.86.1` |

ACP 规范没有统一的“安装 Skill”RPC；这里实现可移植的任务级 Skill 目录，不伪称修改了所有 ACP Agent 的原生 `/skills` 注册表。
本机默认配置没有 ACP Agent，随后按用户选择安装了临时 Codex ACP 适配器。其他 ACP 实现尚未逐个认证。
Direct Codex CLI 沿用任务级目录投递，接口回归覆盖；本轮完整原生过程验收使用 App Server。

公开接口证据：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、
[ACP prompt turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)。
实际代码优先于版本漂移的示例：本地 Codex JSON Schema 确认 `extraRoots`；Pi 0.86.1 的真实 RPC 返回
`sourceInfo.path`，部分随包文档仍写旧的顶层 `path`。实现按当前锁定版本核对身份。

## 会话恢复、更新与卸载

`ProviderSessionHandle.skill_revision` 由 Host 适配器生成并随原生会话句柄持久化。
它反映该 Agent 当前绑定的包/贡献、版本、wheel digest 和实际安装位置。

- 相同绑定、版本和安装位置：后端/原生进程重启后可以恢复原会话。
- 更新、解绑、禁用或卸载：旧句柄与当前 revision 不一致，在原生恢复/付费请求之前拒绝，并明确要求新建 Agent 会话。
- 同版本同字节重新安装但位置改变：也要求新会话，防止恢复旧上下文里的失效文件路径。
- 改动只绑定其他 Agent 的包，不破坏当前 Agent 的会话。

删除文件不能抹去模型已经读入的技能。这里不自动改写旧历史、不暗中丢弃上下文、不重放旧任务。
用户可以新建 Amadeus 对话，在同一工作目录重新发起 Agent 任务来使用更新后的技能；
继续原 Work 的恢复/修订不会绕过版本检查。自动迁移原生历史不在本次范围。

## 权限与所有权

- Electron main 保持唯一安装、更新和删除事务 owner。没有新增 Grant 存储或第二份插件数据库。
- Skill 是声明式资源，不启动 Runner，不导入 Host Python。沿用相对路径、无链接、16 MiB 资源上限，
  另验证 `SKILL.md` frontmatter、name/目录一致及有界 description。
- 同一 Agent 上包之间的技能重名会阻止冲突贡献；Codex/Pi 的原生发现也核对实际来源，不只比较名称。
- Pi 的 Host 工具门只为所选 Skill 目录增加读取能力；不允许因此写该目录、读取相邻包文件或跳过 shell 确认。
- Skill 不扩大原有任务权限，也不获得 Host 历史、密钥或身份管理接口。此机制不提供恶意本地代码的 OS 沙箱。

## 验证方法与证据

样例 `handoff-brief` 要求读取 Skill 正文、`references/layout.json` 和任务输入，按模板生成 `handoff.md`。
两个 wheel 分别包含不同的模板版本标识。报告标识由实际工具读回的数据生成，测试模型事先不持有该标识。

原生集成使用真实 Codex、Codex ACP、Pi 进程和本地可控模型 endpoint；没有真实账号密钥或外部推理调用。
验证内容包括：发现元数据、正文/资源读取、真实写入产物、保存句柄、重启原生进程、恢复会话、更新后的新产物、
旧会话阻断、卸载后新会话不再发现技能，以及子进程退出。这证明协议与生命周期，**不等同于真实 LLM 的自主选用质量评测**。

Codex 实测版本为 `0.162.0-alpha.17.2`。最终 Codex/ACP 测试使用 `workspace-write` 与本机可用的 Windows MXC
沙箱，实际验证安装目录可读、工作目录可写、安装目录不可写；没有将 Skill 目录加入可写根目录。
临时 `CODEX_HOME` 需显式配置沙箱实现：留空会使工具启动被策略拒绝，旧 `unelevated` 在本次临时目录读取中也返回拒绝。
早期使用全权限的测试不足以证明这个边界，最终已移除该测试覆盖配置并重新验收。
这里配置的 MXC 只作用于测试子进程；产品沿用用户的原生沙箱设置，不自动降级。
不同 Windows 沙箱实现的部署约束见[官方文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)。

| 验证 | 文件/结果 |
|---|---|
| 清单、冲突、Agent 范围、revision | `tests/test_extension_skills.py` |
| Host 会话绑定保留 revision | `tests/test_provider_session_binding.py` |
| 真实 Codex/ACP 使用与恢复 | `tests/test_skill_codex_runtime.py`，两种真实入口 |
| 真实 Pi 使用、恢复与读写边界 | `tests/test_skill_pi_runtime.py` |
| 实际 pip/wheel 更新与删除 | `electron/tests/skillInstall.smoke.mjs` |
| UI 绑定与原子更新故障 | `electron/tests/extensionSettings.test.mjs`、`extensions.test.mjs` |
| 实际 production Settings | `electron/tests/extensionSettings.smoke.mjs`；Codex/ACP/Pi 可共同勾选，MCP 不错误显示 Pi |

已将桌面安装器生成的两个真实安装快照交给三个原生入口复测：**3 passed**。
随后确认旧环境回收、卸载、保留数据重装及显式删除。最终沙箱复测状态在 `build/skill-desktop-smoke/case-87gf4a/`；
安装环境在验收后清理。原生结果日志在 `build/skill-installed-sandbox-tests.log`，UI 报告在
`output/diagnostics/extension-settings-ui/report.json`。

回归记录：Electron **428 passed**，production build 与 Settings UI smoke 通过；扩展协议仓 **61 passed / 1 skipped**。
Python 全量分片初次有一项未修改的 texture cache 测试遇到 Windows loopback `WinError 10053`；
该套件原样重跑 **22 passed**，去重后结果为 **5691 passed / 8 skipped / 5 xfailed**。
最终沙箱夹具及 Pi 来源路径检查另做原生定向复测。上述全量结果不隐去首次的临时网络失败。

## 本地复现

```powershell
python -m pip wheel --no-build-isolation --no-deps --no-index sdk/extension-python -w build/extension-sdk
python -m pip wheel --no-build-isolation --no-deps --no-index examples/extensions/handoff-skill -w build/extension-examples
npm ci --ignore-scripts --prefix agent_host/pi_runtime
npm install --prefix build/skill-acp-runtime --no-package-lock --ignore-scripts @agentclientprotocol/codex-acp@2.2.2
python -m pytest tests/test_extension_skills.py tests/test_provider_session_binding.py tests/test_skill_codex_runtime.py tests/test_skill_pi_runtime.py -q
```

原生测试需要 Windows、本机 Codex 0.162 系列及可用 MXC、pinned Pi 和 ACP 依赖；
缺少 Agent 依赖时明确 skip，不能称为验收成功。沙箱不可用会导致测试失败，不切换到全权限。
测试配置、模型 endpoint、输入和产物均在临时目录；生产的模型凭据、全局 Skill 和 Agent 设置不被修改。
更新实验需要另构建 0.2.0 wheel，修改清单/包版本与参考布局标识，不能仅改文件名。
