# 普通文件交付与 DSH 原生 Skill 适配

后续（2026-10-12）：两类产物已分视图与最近范围；ACP/ACL 异常已定位，见[审查入口](extension_merge_review_2026-10-12.md)。以下保留本轮原始观察。

2026-10-11，实验分支 `codex/extension-pilot`，本轮基于 `40d62d3`。
补齐上一轮桌面验收发现的两个缺口：普通文件已经生成但产物列表不可见；DSH 只能从通用任务目录读 Skill，原生按名称查找会失败。
结构化结果见 [验收 JSON](evidence/extension_delivery_native_2026-10-11.json)。未合并或发布。

## 交付行为

Chat → Artifacts → Draft artifacts / Project 现在同时显示文件与已验证 AUIP 应用。
先支持 JSON、CSV、TSV、TXT、Markdown 的 UTF-8 原文查看，最大 512 KiB；其他文件仍列出，明确标记不可预览。
Markdown/HTML 片段作为文本显示，不执行，不自动启动外部程序。
每行区分执行状态、Work 状态和登记状态；`succeeded` 与 `Awaiting review` 可以同时成立，查看不代表验收。

- `work.artifacts.list` 从已有 WorkLedgerStore 投影，Draft 取最近五个有产物的 Work，保留每个交付的文件；最多 200 文件，截断有提示。
- `work.artifact.read` 接受 `work_item_id` + `artifact_id`，不接受任意路径授权。必须属于该 Work，当前记录为 `business.file/registered`，路径仍在工作区内，字节仍匹配登记 SHA-256。
- 后续 attempt 的同路径记录覆盖先前记录；新版 pending 不会被旧 registered 记录遮住。
- 缺失、移出工作区、内容变化、缺少摘要、过大或非 UTF-8 时明确拒绝查看，不返回旧内容。列表保留台账事实供用户识别。
- 复用 AUIP 的路径归一化/工作区校验和当前版本排序，提取到 `server/work_artifact_source.py`；既有 AUIP 的 app-only 查询及运行验证保持原语义。

## DSH 原生 Skill 发现

ACP 配置新增明确的 `skill_discovery: "catalog" | "dsh"`，缺省为 `catalog`。Providers 高级设置可选择；新建 DeepSeek Harness 预设选 `dsh`，不通过 provider ID 或命令名称猜测。
普通 ACP 继续投递名称、描述和安装位置；DSH 模式不再重复注入这份任务目录，而使用真实原生 Skill registry。

DSH 0.2.0-rc.2 支持重复 `--patch`，依次叠加配置。适配器为每个原生进程生成临时启动覆盖，追加在用户参数之后，将 `skill-filesystem.customSkillDirs` 指向本 Agent 绑定的每个贡献目录（不是包含其他 Skill 的父目录），`includeDefaultRoots=false`。
DSH 在目录根直接发现 `SKILL.md`，正文与相对资源仍来自同一个安装目录。没有第二次安装、资源复制或全局目录修改。
已有 DSH profile 中单独配置的内置插件/技能不因此被宣称全部禁用；卸载保证的是 Amadeus 管理的贡献不再投影。

临时覆盖在 SDK 管理的进程结束之后清理，启动失败也清理。扩展安装器继续是代码/版本/数据事务的唯一 owner；保存后重启生效。
`skill_revision` 仍校验包、贡献、版本、digest、实际安装路径。相同资源可以恢复；更新或移除之后，旧会话在原生进程/付费请求前拒绝，新会话使用当前所选资源。
此适配不是 ACP 标准安装 API，也不是操作系统沙箱。

## 验证结果

- Python 相关回归：**199 passed / 2 skipped**。两项跳过均为当前 Windows 无符号链接创建权限；非链接越界、缺失、修改、旧版本拒绝均实际通过。
- Electron：**428 passed**；production build 通过。离屏真实 React 组件测试覆盖四种文本、转义、缺失反馈、状态区分、AUIP 按钮及窄布局。
- 本机真实 DSH Skill filesystem 组件发现、读取引用资源、排除未选同级 Skill、移除后的空投影通过；真实 ACP SDK 子进程收到覆盖并在退出后清理。
- 真实 `deepseek-v4-flash`：**11 / 11 场景达到各自验收条件**。两种 Skill、Notes MCP、filesystem MCP、网页工具、原生会话续接、更新、跨进程持久化和卸载；每阶段进程检查均无遗留子进程。交接 v1、CSV、交接 v2 的原生 `skill` 调用均成功，无原生 Skill 查找失败。
- 真实 Electron 主界面：在之前保留的隔离 profile 打开 `audit.json`、`orders.csv`、`issues.txt`；临时移走合成 JSON 后出现缺失提示，恢复后摘要完全一致。通过 Settings 重启，再从产物列表打开同一个旧 JSON，身份和归属未变。
- 重启后从 Chat 新建任务，真实 Host Work → DSH 生成 `facts.json` 和 handoff v2；两份文件均登记、摘要匹配，Markdown 通过产物页面打开，显示三项给定事实和 `handoff-layout-v2`。Work 保持 `review_ready`。

可复用的实机命令：先用 `electron/tests/extensionAcpLive.smoke.mjs` 在独立 `AMADEUS_TEST_DSH_STATE` 执行 prepare/update/remove；对应运行 `tools/probes/probe_dsh_extension_live.py <phase> --native-skills --state <隔离目录> --env-file <本地凭据文件>`。
探针不是 CI，不自动运行付费调用；凭据只在内存使用，报告不含认证值、原始会话或个人路径。

## 仍需记录的异常与边界

1. 裸 `example.com` 的两次抓取被 DSH 的公网地址校验拒绝，模型随后用 `www.example.com` 成功。未绕过网络边界。
2. 卸载后调用 MCP 资源列表时，ACP SDK 出现两次 JSON-RPC 解析错误，随后运行完成。已检查本地 MCP 请求代码，未定位输出来源；不把未确认的原因报成上游缺陷，保留为后续最小复现待办。
3. 新桌面任务的模型曾尝试探索性 `pwsh` 调用。已核对 DSH 原始工具记录：调用在 Windows 沙箱 ACL 初始化时报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite`，随后用文件工具完成。这是沙箱环境错误，不能当作“不许 shell”的语义权限验收；交付成功也不等于模型全程遵守该要求。原生 `skill` 与资源读取的成功亦由原始工具记录确认。本轮没有加入 Host 关键词分类或削弱沙箱。
4. 之前重启成功与 `reconnecting` 并存的问题，本次未复现；不宣称已修复。

隔离桌面窗口保留供检查。用户生产设置未修改；未扩大到语音听感、macOS/Linux、真实付费 Codex/Pi、签名分发或长期可靠性认证。
先审阅本轮及已记录的传输异常，再按 M0 → M2 → M1 进入记忆；本轮没有实施记忆模块，也未发布社区回复。
