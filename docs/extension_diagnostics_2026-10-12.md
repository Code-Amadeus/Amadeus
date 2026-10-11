# ACP stdout 与 Windows ACL 定位

本轮使用已安装、未修改的 DSH 0.2.0-rc.2。新增诊断都是显式运行，不进入应用启动路径。
模型请求由本地合成 HTTP 服务回答；没有调用外部模型或使用真实密钥。

## ACP：MCP SDK 的日志通道

`probe_dsh_stdio.py` 让真实 DSH 通过真实 ACP SDK 调用两次资源列表。
透传 stdout 的诊断启动器不删行、不改协议；它记录了正好两行非 JSON：
MCP Client 在服务未声明 resources 能力时输出的“返回空列表”提示。
调用本身成功、返回空列表，ACP SDK 记录解析异常后继续。

源码归属是 `@modelcontextprotocol/client@2.0.0` 的 `console.debug`，不是 Skill
安装、Notes 数据、DSH JSON 序列化或 Amadeus artifact 读取。`probe_mcp_stdout.mjs`
进一步移除 DSH 和 Amadeus，仅用原始 MCP Client + tools-only filesystem server
重现两行；临时模块副本把日志送到 stderr 后，两行消失，业务结果完全相同。
安装中的原文件未修改，也未往 Host 添加丢弃坏行、匹配告警字符串或全局 console 替换。

上游核对到 `modelcontextprotocol/typescript-sdk` 提交
`b022522089a0c8b632595c6e7b536453945ed5a9`，缺能力分支仍直接写 console。
已有 [#1262](https://github.com/modelcontextprotocol/typescript-sdk/issues/1262) 和
[#2370](https://github.com/modelcontextprotocol/typescript-sdk/pull/2370) 处理可注入 logger；
核对时未合并。准备了[脱敏补充草稿](upstream/mcp-sdk-stdout-followup_2026-10-12.md)，
没有发帖、开重复 issue 或提交私有实现。正式支持这个 DSH 组合前，需要消费修复并在
DSH 中将 logger 配到 stderr，然后让相同的完整 ACP 探针通过零非协议行检查。

## Windows：文件可写与沙箱配置权限不同

DSH 的 `grantWrite` 同时设置 DACL 和 Low integrity label（SecurityInfo = 0x14）。
Windows 对后者要求 `WRITE_OWNER`，对 DACL 要求 `WRITE_DAC`：
[Microsoft SECURITY_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/secauthz/security-information)。

只读 `CreateFileW` 权限句柄探针及真实 DSH 沙箱的结果如下：

| 位置（均为新建、当前用户所有的 NTFS 目录） | READ_CONTROL | WRITE_DAC | WRITE_OWNER | DSH 初始化 |
| --- | --- | --- | --- | --- |
| 本检出所在目录下 | 成功 | 成功 | Win32 5 | `SetNamedSecurityInfoW / grantWrite` 失败 |
| 用户临时目录下 | 成功 | 成功 | 成功 | 成功 |

临时目录的真实受限子进程进一步验证：目录内写入成功，目录外写入拒绝，退出码 0。
没有采用 `manageDacls=false`、提权、接管所有权或移除完整性标签。
这确认了失败环境缺少原生沙箱所需权限；不是所有 F 盘或所有 NTFS 目录都失败，
也不是 Amadeus 把“不用 shell”解析成了有效权限规则。之前模型尝试了 shell，
只是碰巧在沙箱初始化处失败，不能用它证明模型遵守约束。

`check_windows_acl.py <workspace>` 只读取有效访问权；`probe_dsh_acl.mjs [temp]`
只修改并清理它新建的诊断目录。现有用户目录 ACL、配置位置和执行策略均未修改。
运行 DSH 的 workspace-write shell 应由用户显式选择满足权限条件的目录；有意维持
受限 ACL 的目录继续失败关闭。该环境差异不作为上游缺陷投稿。

## 审查处理

产物分离和插件公共契约可以独立审查。DSH 0.2.0-rc.2 / MCP Client 2.0.0
继续标为实验支持，不升级为稳定合规。上游 stdout 缺陷和目录权限前提均有确定复现；
没有把未修的外部依赖问题包装成“全链路零故障”。记忆接口及行为不在本轮改动中。
