# 插件模块实施计划评审

> 2026-10-11 · 评审对象：[插件模块实施计划](extension_module_implementation_plan_2026-10-11.md)
>
> 代码基线：Amadeus `6147acb`（main）。协议：Amadeus-Extensions `claude/spec-v0.1`
> 的未提交草案（HEAD `2fab8bd`）。本轮只读核对，没有修改代码或协议文件。

## 结论：修改后施工

方向和权限边界是对的。计划 §2 引用的代码事实全部核实无误；Amadeus-Extensions 的清单测试
复现为 61 passed / 1 skipped。但有三处与 Amadeus 现有产品语义冲突，并直接决定 P1 的规模和
M0 的行为，需要先改计划：

1. **生效语义（F1）**：P1 要求扩展 TTS 运行期切换和 generation 交接。而 `TTS_BACKEND`、
   MiMo 配置，以及 MCP/ACP 结构化记录，在 Amadeus 里都是"保存 → 待重启 → 重启后生效"。
2. **发声语言（F2）**：Amadeus 只说日语或英语，MiMo 示例却声明 zh/en。按契约，默认日语用户
   永远听不到 MiMo。
3. **记忆注入位置（F3）**：M0 把记忆注入 system prompt，违反协议自己的"上下文不是 system
   prompt"不变量。

另外有一处凭据边界缺口（F4），必须写进 P1-2 的验收。

## 1. 应当保留的部分

- Host 拥有身份、权限、持久状态和执行权。安装不授予权限，Main Chat 不执行扩展，不暗中选用
  远程 fallback，失败后文字继续，Runner 崩溃后可由 Host 单方面回收。
- 期望状态只有一个 owner，放在 Electron main，与 MCP 连接的做法一致。`TTS_BACKEND` 仍是唯一
  的持久选择，完整贡献 id 只对这个键放开。
- 内置 MiMo 保留到满足移除条件为止，移除单独收尾。
- M0 通过现有 Host 读取器召回，不建第二本 Work 事实账。旧消息缺失的时间标为未知；排除某条
  来源不等于删除原始记录。

## 2. P1 施工前必须修改

### F1 生效语义：沿用"待重启"，去掉运行期切换与 generation 交接

**现状**

- `TTS_BACKEND`（`config/catalog/tts/selection.json:25`）和内置 MiMo 的四个字段
  （`config/catalog/tts/mimo.json:65`）都是 `apply: backend_restart`。设置页会显示
  "restart required"，并提供 Restart backend 按钮
  （`electron/src/renderer/components/SettingsPage.tsx:347`、`:811`）。
- 结构化记录已有先例：MCP 连接和 ACP agent 存放在 `DesktopSettingsStore`。变更后调用
  `markPending`（`electron/src/main/desktopSettings.ts:619`）；后端启动时带上快照，启动后再
  `markApplied`（`electron/src/main/index.ts:464`、`:499`）。Python 侧只读，并明确提示
  "restart the backend to apply changes"（`server/handlers/mcp_connection_handler.py:50`）。
- 计划 §3.3 却要求：无语音启动后选用扩展即接通、再禁用即停止准入、`live`/`reactivate` 交接
  和 drain。这会把下面三处都拉进试点：`tts/pipeline.py` 中 18 处 `_tts_runtime` 读取、
  disabled 模式的丢弃 worker（`server/app.py:1279`），以及 `configure(None)`
  （`tts/pipeline.py:546`）。
- 这套机制没有真实调用方。MiMo 的四个字段都是 HTTP 客户端配置，不需要 `reactivate`；内置
  MiMo 修改任何字段同样要重启。

**与产品语义的冲突**

- 同一个 `TTS_BACKEND` 会出现"由取值决定"的生效边界：选内置值要重启，选扩展值立即生效。
  内置与扩展之间的切换没有定义，例如显存中已加载 GPT-SoVITS 时改选扩展，由谁卸载模型。
- 第三方扩展的生命周期会比所有内置后端都更强，优先级颠倒。
- TTS 是 Chat、Wallpaper 和 VN 共用的实现（`selection.json:26` 的 `ui_description`，
  以及 `server/vn_tts_bridge.py`）。热切换的验收需要覆盖这三个表面，计划只按 Chat 写。
- Windows 上运行中的 `python.exe` 会锁住环境目录，在线卸载必须走 drain → 退出 → 重试删除。
  改为重启生效后，删除发生在没有 Runner 运行的时刻。

**建议的 P1 形态**

- 扩展记录放进 `DesktopSettingsStore`，与 `mcpConnections` 同级，按包加密密钥。安装、启停、
  配置、选用和卸载都只标记 pending，用户点 Restart backend 后生效。
- Python 启动时读取记录并校验，然后以完整贡献 id 调用 `register_tts_backend`，注册一个进程内
  代理 `BaseTTSBackend`。`create_tts_runtime(TTS_BACKEND)`（`server/app.py:735`）保持不变。
  - 注册不等待 Runner ready，`load()` 不能把后端启动卡在 30 秒的握手窗口上。
  - Runner 未 ready 时请求失败，原因显示"语音启动中"，文字继续。
- Runner 崩溃、有界重启和迟到响应的丢弃，都在代理和管理器内部处理。一个后端进程的生命周期内，
  管线里的 runtime 对象不变，"每个 utterance 只用一个实现"自然成立。因此 P1-3 不再修改
  pipeline、cache 和 scheduler。
- "语音不可用并说明原因"应在 TTS owner 层为所有后端统一实现。现在合成失败只写日志
  （`tts/pipeline.py:872`、`:1100`），不能只给扩展加可见原因。
- `live`/`reactivate`、generation 交接和运行期选择挪到后续阶段。等出现真实调用方时，作为
  TTS owner 面向所有后端的能力来做，而不是只为扩展开一条路。
- 代价如实写明：重启后端会中断进行中的语音和 Work，与今天修改任何 `backend_restart` 设置
  相同。
- 协议同步修改：
  - 允许 v0.1 Host 在重启时应用全部变更；`live`/`reactivate` 表示允许的最早生效边界。
  - §1.3 试点验收中的 drain 条目改为重启语义。
  - README 删除 "without restarting Amadeus"。

如果你确实要在 P1 做扩展热切换，那就不再是扩展细节，而是 TTS owner 的产品变更：需要先定义
内置与扩展之间的切换，以及 VN 和 Wallpaper 的验收。我不建议放进试点。

### F2 发声语言：Amadeus 只说日语和英语

**现状**

- 助手的主语言就是 TTS 语言，只有 `日文` 和 `英文`
  （`config/catalog/tts/runtime.json:41`、`llm/prompts.py:9`）。
  `server/assistant_language.py:57` 写明中文只是展示语言，
  "not a third primary role-language axis"。
- MiMo 示例清单声明 `languages: ["zh","en"]`；voice-v0 §2.5 的示例是一句中文，
  `language: "zh"`。契约规定，未声明的语言走 fallback。结果有两个：默认日语（Kurisu）用户
  永远听不到 MiMo；`zh` 这个值 Amadeus 根本不会发出。
- 内置 MiMo 既不发送 language，也不发送 speed（`tts/backends/mimo.py:85-94`）。迁移到扩展会
  改变现有用户听到的行为。
- 公开资料只见中文和英文音色，没有找到 MiMo 支持日语合成的官方说明
  （[v2.5 发布页](https://mimo.mi.com/docs/en-US/news/latest/v2.5-tts-release)、
  [OpenClaw Xiaomi 文档](https://docs.openclaw.ai/ja-JP/providers/xiaomi)）。
- 管线发送的 `speed` 是 GPT-SoVITS 的节奏启发值：首句 1.067，其余 0.97
  （`tts/pipeline.py:597`）。它不是用户意图。

**修改**

- **G0**：在 P1-3 之前，用真实 key 以 `mimo-v2.5-tts` 合成一句日语并试听（需要你来做）。
  效果不可接受时二选一：试点明确只面向英语输出用户，或者改用其他后端做试点。
- 语言兼容作为静态状态处理：在选用后端时，以及助手语言在线切换时
  （`server/handlers/system_handler.py:819`）对照检查，并在设置里显示"当前语音后端不支持
  日语"。逐请求检查保留，作为 fail-closed。
- `speed` 在 v0 改为建议值，或者由清单声明是否支持；不要求 MiMo 去兑现 GPT-SoVITS 的启发值。
- 协议示例改用 `ja`/`en`。

### F3 M0 注入位置：记忆不能进 system prompt

**现状**

- `role_reference` 的结果被拼进 system（`server/cooperative_provider_loop.py:1135-1136`）。
  其他每轮变化的内容都放在 JSON frame 里：`history`（`:1127`）、`work_tasks`（`:1133`）、
  `app_context`（`:1144`）和 `auip_context`（`:1148`）。
- 语言锁分两处：system 末尾由 `finalize_system_prompt_language` 追加，frame 由
  `wrap_user_message_for_language_lock` 包裹（`server/app.py:1738`、`:1748`）。
- 计划 §8 的 M0 写的是"在 role_reference 接缝内……在最终语言锁之前组装"，也就是放进 system。
  这有三个问题：
  - 与协议不变量 #4 和 context-source §4 相悖：上下文不得进入 system prompt，并且要与指令、
    人设和历史分开。
  - 角色 RAG 的标题本身就写明它 "not … conversation memories"（`core/character_rag.py:132`）。
  - system 每轮变化，会让后面的 history 失去前缀缓存。
- `role_reference` 没有截止时间，任何异常都会变成 `RoleDecisionUnavailable`
  （`server/cooperative_provider_loop.py:1677`），整轮失败。

**修改**（M0 开工前写进计划）

- M0 收集器的输出放进 frame 的独立字段，例如 `reference_material`，带来源标签，位于 history
  之后，仍由 frame 的语言锁包裹。角色 RAG 是否留在 system 另行决定。
- 加截止时间并隔离异常：超时或出错时返回空并记为 dropped，绝不让整轮失败。
- 写明 M0 相对 frame 中已有 Host 事实（`context`、`work_tasks`、`retained_contexts`）的增量：
  按来源 id 去重；同一事物上，Host 事实优先于召回文本。

### F4 Runner 环境与扩展密钥：不能沿用 MCP 的 `{**os.environ}`

**现状**

- `backendEnvironment` 会解密所有桌面密钥和 MCP 连接的环境变量，放进 Python 后端的环境
  （`electron/src/main/desktopSettings.ts:669-707`）。
- MCP stdio 以 `env={**os.environ, **connection.environment}` 启动
  （`agent_host/mcp_connections.py:267`）。`AMADEUS_MCP_CONNECTIONS` 一直留在 `os.environ`
  里，只有本地认证三项会被清掉（`server/local_auth.py:166`、`server/app.py:354`）。
- 协议 §6.1 和 §5.3 要求 Runner 使用白名单环境，并禁止接触其他包的密钥。计划只写了"不串密钥"
  （§6）和"复用进程管理经验"（§5）。照抄 MCP 的写法，第三方代码就能拿到全部 Host 密钥。

**修改**（写进 P1-2 验收）

- Runner 环境由白名单构造：SystemRoot、TEMP/TMP、区域设置、代理变量，加上明确的 PATH 策略。
  任何时候都不以 `os.environ` 为起点。
- 如果扩展记录或密钥像 MCP 那样经环境变量传给 Python，启动时读一次就从 `os.environ` 中删除，
  做法与 `clear_inherited_auth_environment` 相同，以免 Provider、MCP 和 sidecar 继承。
- 测试：Runner 环境中不能出现任何 Host 密钥键、`AMADEUS_MCP_CONNECTIONS`、扩展记录变量或
  其他包的密钥；stderr 日志要先脱敏。

另外，现有的 MCP 连接之间、MCP 与 Provider 之间可以互相看到密钥。这是既有问题，应另开 issue，
不并入本计划。

## 3. 进入 M0 前必须修改

### F5 记忆阶段顺序与 Draft 范围

- **阶段顺序。** M1（外部记忆契约）需要纠正、遗忘和作用域规则，而这些规则由 M2（Host 自有的
  显式记忆）定义。Host 拥有身份和持久状态，应当先定义记忆模型，再开放给外部来源。建议顺序
  改为 M0 → M2 → M1。
- **Draft 范围。** M0 对 Draft 只召回"当前会话"，但 role frame 已经带上最近 30 轮，即 60 条
  消息（`server/cooperative_provider_loop.py:1127`）。本机统计的会话中位数是 6 条消息，最长
  41 条（2026-10-10 统计，含测试会话）。因此 Draft 分支实际上什么都不会召回。M0 应写明只服务
  绑定了 Project 的会话；Draft 的跨会话召回，等 M2 决定用户级还是角色级作用域（D1）之后再做。
- **社区合作者。** M1 的天然合作者是 #97/#98/#99（C8）和 #146（persona memory）。前者从
  09-20、后者从 10-01 起无人回复。计划应写明如何回应，以及是否邀请他们做 M1 试点。

## 4. 建议简化（P1 内）

- **S1 Grant。** P1 不支持任何 Host capability，Grant 持久化没有调用方。规则改为：
  `permissions.host` 非空即判为 `incompatible`，方法白名单拒绝所有 `host.*`。Grant 记录和
  界面等第一个 capability 出现时一起做。
- **S2 损坏策略。** 扩展记录与 `mcpConnections` 存在一起，沿用现有的 `.bak` 和 set-aside
  恢复。只需要一条不变量："记录缺失或损坏时，不启用任何扩展，也不从磁盘扫描重建。"没有被引用
  的环境目录列为孤儿，由用户确认后清理。删掉计划里单独的损坏策略，并同步修改协议 §4.1 的
  MUST。
- **S3 安装事务只设一个 owner。** 计划让 Electron 持有记录、Python 做安装准备，形成跨进程的
  两阶段提交。改为重启生效后，建议整个流程都由 Electron main 负责：
  1. 暂存安装；
  2. 校验；
  3. 移入正式目录；
  4. 提交记录（这是提交点）；
  5. 下次启动时回收残留。

  清单校验只保留一份实现：Electron 调用 Host 自带的 Python 辅助程序（`python -I`，只读取
  ZIP，复用参考验证器），不要再用 TS 写一遍。后端激活时再校验一次，失败即拒绝。下载依赖时
  要遵循用户的 pip 索引和代理配置（中文用户常用镜像）；离线导致失败时，原因要说清楚。
- **S4 迁移移出 P1。** 内置 MiMo 保留期间，§7 第 2–3 步（搬迁密钥、改写 `TTS_BACKEND`）
  没有用户需求。P1 的退出证据只要求试点本身；迁移和移除放到有分发渠道之后，即 ROADMAP 的
  "Later"。

## 5. 文档错误

- 协议第 1029 行写成了 `MIMO_TTS_https://github.com/…/6147acb…_URL`，应为
  `MIMO_TTS_BASE_URL`，是替换永久链接时误改的。
- 协议 §1.3 规定，provisional 部分要经过真实扩展验收才能变成 normative；但 §3.6 已经把 TTS、
  MCP 和 Skill 标为 normative。建议在 P1 通过之前把它们改回 provisional，或者修改 §1.3 的
  措辞。
- README（未提交）写着 "enabled, disabled, and updated without restarting Amadeus"，与 P1
  不符，而且版本更新本身也已推迟。

## 6. 修改后的 P1 切分（建议）

| PR | 内容 | 完成条件 |
|---|---|---|
| P1-1 记录与检查 | `DesktopSettingsStore` 中的扩展记录，含按包加密的密钥和 pending revision；Host 清单检查辅助程序；Catalog 只读投影，经 `server/handlers/composition.py:84` 的 `extra_packages` | 检查不执行包代码；未知 kind/contract、非空 `permissions.host` 和重复 id 都在激活前拒绝；记录缺失或损坏时没有扩展被启用 |
| P1-2 安装与 Runner | 暂存安装、提交和启动时回收；启动激活、握手、ping、有界重启和关闭；白名单环境；最小 SDK | 用合成包覆盖崩溃、超时、Host 退出和重启；没有孤儿进程；在 Windows 上卸载并重启后，文件删除干净 |
| P1-3 TTS 代理 | 代理 `BaseTTSBackend`；`TTS_BACKEND` 完整 id 的校验；语言静态兼容；统一的"语音不可用"状态 | 流式与非流式；中断时发送 `$/cancel`，迟到的音频块被丢弃，不重复 POST；pipeline 不改动 |
| P1-4 设置旅程 | 安装、配置、选用、启停、卸载，均为待重启生效；显示状态和原因 | 在真实桌面上验证中英文、窄窗、离线，以及没有加密能力的情况 |
| P1-5 MiMo 包 | G0、真实包、契约测试，以及首块和取消的延迟证据 | 不 import 核心私有模块；受控的真实服务验收；不含迁移 |

## 7. 需要你决定

- **D-a P1 的生效语义。** 重启生效（推荐），还是扩展热切换（意味着 TTS owner 层面的产品
  变更）。
- **D-b G0 的结果。** MiMo 是否支持日语，决定试点面向哪些用户。
- **D-c 记忆阶段顺序。** 是否采用 M0 → M2 → M1，以及如何回应 #97 和 #146。
