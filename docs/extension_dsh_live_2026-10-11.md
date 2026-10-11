# DeepSeek Harness ACP：真实模型与多插件验收

后续状态：普通文件交付与 DSH 自动原生 Skill 适配已完成，见[补齐与验收记录](extension_delivery_native_2026-10-11.md)。下文保留当时结果与限制。

日期：2026-10-11。基线：`codex/extension-pilot` 的 Skill 提交 `5dbcee4`。
本次直接使用已配置的 DeepSeek 凭据和官方 `deepseek-v4-flash`，不是本地模型替身。
经由 Amadeus `AcpProviderAdapter`、官方 ACP Python SDK 和真实 DSH/MCP 进程执行。
仍是隔离环境的适配器验收，不等于完整 Electron → Chat → Work 用户旅程或长期可靠性认证。

后续已执行[桌面用户流程抽样](extension_desktop_live_2026-10-11.md)，确认了真实 Chat/Work、重启续接与 UI 更新，同时发现普通文件的产物展示缺口；尚未判定桌面端到端全部通过。

## 版本与材料

- `@deepseek-ai/dsh@0.2.0-rc.2`，ACP 返回协议版本 1；模型从其真实 `configOptions` 读取，推理强度选 `low`。
- `@modelcontextprotocol/server-filesystem@1.0.0`。
- Windows、本地 Python 3.12；每次运行独立 DSH 进程，共享本轮独立 `DSH_HOME` 以验证恢复。
- 两种 Skill（handoff、csv-audit）和 Notes MCP 都先构建 wheel，再由真实 Electron main 安装器安装、绑定 `deepseek_live`、生成启动快照。
  安装器测试使用合成设置存储，未打开用户的生产设置或存储业务密钥。
- 文件服务仅暴露合成 archive 目录。输入为 5 行 CSV、三项交接事实、合成笔记和小型 JSON；网页请求使用公共示例页面。
- 密钥仅从本地环境/显式 env 文件读入内存，ACP 子进程只获得自身凭据引用和隔离 home；未写入 YAML、参数或报告。

## 结果

| 入口/插件 | 实际任务与证据 | 结果与限制 |
|---|---|---|
| Amadeus handoff Skill，经通用 ACP 文件目录 | 模型选择技能、读取正文和 layout、写入报告；v1/v2 标识与输入事实核对 | 最终正确；默认 DSH 原生 `skill` 查找先失败一次，随后按路径读取 |
| Amadeus csv-audit Skill，经通用 ACP 文件目录 | 读取 rules 与 CSV；写出 JSON；逐项核对缺值、重复 ID、负金额 | 最终 4 项问题全部正确；初稿漏报负金额，模型自行复查并修改产物 |
| 同两种 Skill，经 DSH 原生目录配置 | 同一份安装资源，使用官方 `skill-filesystem.customSkillDirs`；两个任务均出现成功 `skill` 调用 | 两项正确；无原生查找失败，CSV 源文件保持不变 |
| Notes MCP wheel | `save_note`、`read_note`；校验插件数据目录；另开进程和新会话读回 | 写入、读回和跨进程持久化均通过 |
| 官方 filesystem MCP | 发现允许目录、查询、读取 archive/catalog.json；核对未在提示中提供的内容 | 成功；首次猜测工作目录被服务拒绝，查询 allowed directories 后纠正 |
| DSH 原生 `dsh-tool-web` | 实际 `web_fetch`，核对工具返回和标题 | 裸域名被非公网 IP 检查拒绝；模型改取 `www.example.com` 后 HTTP 200 |
| 会话恢复 | 进程退出后恢复原句柄，不读取文件，回答之前模板标识 | 正确恢复 v1；该轮没有工具调用 |
| 更新与卸载 | 安装 v2；旧句柄在提交模型前拒绝，新会话使用 v2；卸载后旧句柄拒绝、新会话无两个 Skill 或 Notes MCP | 通过；卸载回收包环境、默认保留笔记数据 |
| 原生网页插件禁用 | 临时 profile 中禁用 `tool-web`，启动新进程和新会话 | 返回 `web_fetch_available: false`，没有替代执行 |

共 **14 个有断言的验收场景通过**，其中 2 项是模型调用前的旧会话拒绝；连通性探针另计。
各阶段结束时，记录过的 Agent/MCP 子进程均已退出。独立日志中保留工具失败和模型纠错，未把“最终产物正确”写成“过程无误”。
禁用检查初次仅匹配中文表述，误判了正确的英文回答；已修正探针为结构化布尔值并定向重跑。
这是测试仪器问题，没有为通过测试修改产品权限或 Agent 行为。
相关 Skill/ACP 回归 **36 passed / 1 skipped**（Windows symlink 权限不可用）；Ruff 和 Node 脚本语法检查通过。

## 协议上得到的结论

**通用 ACP 文件目录投递与原生 Skill 注册是两个不同的能力。** 本轮 DSH 默认配置没有把 Amadeus 的安装路径登记进其原生 Skill registry。
模型虽然最终按路径完成任务，仍会产生无效 `skill` 调用。不能据此宣称“支持 ACP 就支持每个 Harness 的原生技能安装”。

对照组只在独立 DSH profile 中配置官方 `customSkillDirs`，指向既有安装资源，没有复制另一份 Skill、修改全局目录，或增加 Host 专用 DSH 分支。
这种方式消除了本轮两个任务的查找失败。它验证了一个可行的原生消费接口，**当前产品仍没有自动管理这份 DSH 原生配置**。
后续若纳入产品，应由 Agent 适配边界声明并管理原生发现能力，沿用安装身份、绑定、重启和 revision 规则；不能把 profile 写入伪装为标准 ACP RPC。

MCP 的可移植性在本轮更直接：wheel 的声明式服务和官方独立 MCP 服务都通过同一个 ACP 会话投递完成实际工具调用。
Harness 内部 Cordis 工具插件则由其 profile 管理。本轮验证网页工具的启用/禁用，不声称 Amadeus 已能安装任意 Cordis npm 插件或浏览器 UI 半包。

CSV 中间叙述与产物曾不一致，进一步说明 Host 必须依据最终文件/结构化事实验证结果，不能把 Agent 的一段说明当成完成凭据。
目前每场景一轮，不能给出成功率、延迟分布或模型间优劣排名。

## 复现

代码入口：`tools/probes/probe_dsh_extension_live.py`、`electron/tests/extensionAcpLive.smoke.mjs`。
这些是显式运行的真实推理探针，不接入默认单测或 CI。

1. 按 `examples/extensions/README.md` 构建 SDK、handoff、csv-audit、notes 的 wheel。
   更新实验还需 handoff 0.2.0：在 build 临时副本中同时修改 pyproject/manifest 版本和 layout 的 `handoff-layout-v2` 标识，再单独构建 wheel；不修改 0.1.0 源码。
2. 在 `build/deepseek-acp-live/runtime` 安装锁定的 DSH 与 filesystem npm 包，使用 `--ignore-scripts --save-exact`。
3. 为安装器设置 `AMADEUS_TEST_PYTHON` 为开发 Python 的绝对路径，运行下面阶段。
   Python 探针使用 `DEEPSEEK_API_KEY`，或以 `--env-file <本地路径>` 显式指定来源。不要把 key 写在命令行。

```powershell
node electron/tests/extensionAcpLive.smoke.mjs prepare
python tools/probes/probe_dsh_extension_live.py prepare
python tools/probes/probe_dsh_extension_live.py native
node electron/tests/extensionAcpLive.smoke.mjs update
python tools/probes/probe_dsh_extension_live.py update
python tools/probes/probe_dsh_extension_live.py persistence
node electron/tests/extensionAcpLive.smoke.mjs remove
python tools/probes/probe_dsh_extension_live.py remove
python tools/probes/probe_dsh_extension_live.py web-off
```

默认状态目录是 `build/deepseek-acp-live`；新实验可用安装器的 `AMADEUS_TEST_DSH_STATE` 与 Python 的 `--state` 指向同一 build 子目录，运行时 npm 包也放在该目录的 `runtime` 下。
原始记录在各 `<phase>-results.json`，产物在 `workspace`；当前包环境已卸载，Notes 数据按默认语义保留。
版本、检查与工具成功/失败计数的脱敏摘要见 [JSON 证据](evidence/dsh_acp_plugins_2026-10-11.json)。

## 上游核对

核对官方仓库 HEAD `d743267388641bc76f17c45ce8b4c231aed1d32c`。运行的是发布的 npm 0.2.0-rc.2；没有把它声称为该 HEAD 的同一构建。
本次没有证据支持提交上游缺陷：Skill 目录差异属于配置/集成边界，目录拒绝和网页非公网检查属于原生限制。

- [ACP 契约](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/acp/acp/README.md)
- [CLI/profile 层](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/apps/cli/reference/README.md)
- [原生 filesystem Skills](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/skill/skill-filesystem/README.md)
- [官方 MCP servers](https://github.com/modelcontextprotocol/servers)
