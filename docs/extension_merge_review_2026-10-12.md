# 插件实验分支审查入口

后续合并前复查、修复与草稿 PR 状态见
[最新复查记录](extension_premerge_review_2026-10-12.md)。以下为前轮整理时的快照。

2026-10-12，`codex/extension-pilot`。本轮起点 `7663549`；整个插件实验起点
`6147acbc36e2b16faa5d8192a873d40a7feb5965`。本地实现、配套文档与证据已经整理，
未推送、开 PR、合并或发布 SDK。记忆模块未修改。

## 审查结论的范围

可审查合并的是明确标为 experimental 的本地 wheel TTS/MCP/Skill 实现，
以及 Host 文件/AUIP 交付展示。默认不自动安装或启用包；保存后重启生效。
不把此状态解释为完整协议合规、全平台认证或 DSH 当前依赖组合无故障。

公开行为统一以 [Host profile 0.1](extension_host_profile_v0.1.md) 为实现对照，
[SDK README](../sdk/extension-python/README.md) 与 [五个样例](../examples/extensions/README.md)
链接到同一范围。协议草案的未来能力仍可保留，但不能作为当前 SDK 的可调用承诺。

本轮实现提交：`2e33684`（文件/AUIP 分离），`0be5054`（诊断与上游草稿）；
契约、示例和审查材料在后续对齐提交中集中保存。

## 按归属审查

| 范围 | 主要入口 | 审查要点 |
| --- | --- | --- |
| 文件/AUIP 归属 | `server/auip_app_source.py`、`work_read_model.py` | 应用只收纳已验证入口/manifest、Host runtime 或精确 outcome receipt 的成员；同目录报告不被吞掉 |
| 用户入口 | `ProjectAppsPanel.tsx`、`WorkFilesPanel.tsx`、`ChatPage.tsx` | Files / Interactive apps 分视图、分计数、分最近范围；组成文件收在应用详情；普通读取与 AUIP Attach 不混用 |
| 安装与状态 | `electron/src/main/extensions.ts`、`desktopSettings.ts` | main 单一事务 owner；失败不半提交，重启后才回收旧代码，默认保留独立数据 |
| Runner/公共 SDK | `server/extensions/`、`sdk/extension-python/` | 清单与注册一致；stdout 独占；环境白名单；取消与无合成重试；无 Host Grants |
| MCP/Skill | `agent_host/extension_mcp.py`、`skills.py`、`acp_skills.py` | 只投影所选 Agent；原生 owner 保留；更新后旧会话在提交前拒绝；DSH 配置显式选择 |

没有新建另一套 artifact 数据库，也没有按文件扩展名或目录猜测 AUIP 成员。
没有被 Host 确认的文件保持独立可见。失效 AUIP 没有可运行应用卡，原文件仍可在
文件视图辨认；查看时继续核对 Work、工作区、当前版本与摘要。

混合交付测试覆盖一份应用、Host 确认资源、同目录 Markdown 报告与独立 HTML；
覆盖重开台账、manifest 失效以及 Draft/Project 双向互不挤占的最近列表。

## 异常的处理决定

详见[定位记录](extension_diagnostics_2026-10-12.md)和[结构化证据](evidence/extension_review_2026-10-12.json)。

1. **ACP stdout：上游 SDK 缺陷，尚未在生产依赖中修复。** 无外部模型最小复现确认
   MCP Client 2.0.0 的 debug 日志污染外层 ACP stdout。临时模块副本将日志送 stderr
   后业务结果不变、非协议行归零。已有上游 issue/PR，准备了脱敏补充草稿；未投稿。
   `probe_dsh_stdio.py --expect-clean` 对当前原始依赖按预期返回 1，作为明确的稳定支持门槛。
   不在 Amadeus 中吞坏行、不静默修改用户安装的 Agent 包。
2. **Windows ACL：已定位的部署前提。** 本检出下的目录可写、归当前用户所有，但
   缺 WRITE_OWNER；原生 sandbox 无法设置 Low integrity label。权限齐全的临时目录
   初始化和内写/外写拒绝均通过。提供只读诊断；不自动改 ACL、迁移目录或降级执行。
3. **模型“不用 shell”遵循度：未被上述失败证明。** 原模型确实尝试过 shell，
   失败原因是环境初始化；不新增 Host 文本关键词授权，也不把环境失败当语义验收。

## 验证

- 相关 Python 回归：最终去重 **271 passed / 2 skipped**。组合运行 270 通过后，
  新增 Project/Draft 参数化补充一项；最终文件套件 16 通过/1 跳过。两项跳过均为
  Windows 无符号链接创建权限，其他越界、摘要、版本和混合交付检查实际执行。
- Electron **428 passed**；production build 通过；产物组件 smoke 通过，包括
  两类视图、成员详情、AUIP 操作、过期响应、缺失提示、纯文本转义和窄窗口。
- 五个源码样例通过 Host profile、安装资源、版本/entry point、SDK 依赖与无私有
  Host import 检查；MiMo 示例文字已明确“不支持日语”。
- 独立协议仓 **61 passed / 1 skipped**；本轮未修改其 schema/未来记忆契约。
- stdio 本地模型复现和纯 SDK 对照均完成，无残留诊断 Agent 子进程；ACL 目录内/外
  写入使用真实受限子进程。之前的 11 个真实模型与桌面测试是前序证据，本轮未重复付费。

## 配套协议仓

实际稿位于同级 `Amadeus-Extensions/spec/extension-protocol-v0.1.md`，其 Git HEAD
为 `2fab8bdc6bd497a22c6ee22592d992931d7bf8b7`，已有草案内容尚未提交。
本轮保留这些现有改动，仅补 §1.3.1 与 README 的 DSH/交付/限制说明。
本轮精确差异另存为 [配套补丁](extension_protocol_alignment_2026-10-12.patch)，
已对当前协议工作副本通过 reverse apply check。它不是从 Git HEAD 重建整份草案的补丁。
协议仓保留未提交状态，方便和主分支实现一起审阅；没有替用户提交其原有未提交文件。
忽略目录里的旧 `build/extension-protocol-reference` 副本不是本轮对齐依据。

## 合并与发布

审查通过后，可按现有项目流程合并实验实现。稳定发布前仍需：消费并验证上游日志
修复、明确支持平台与 SDK 分发/许可证、补语音三种界面人工体验。MiMo 英语实机按
此前用户决定保留待办，日语没有待办。内置 MiMo 迁移、市场/自动更新、Host 历史授权
及记忆能力均未混入此变更。记忆顺序继续为 M0 → M2 → M1，待另行开工。
