# 社区插件开发预览第三批：S1 下载、安装与耐久安全准入

日期：2026-10-21。状态：实验分支实现和本地验收完成；未推送、未合并、未发布。

Host 继续使用 `codex/extension-community-preview`，从 D2 的
`36ae48287779d05608b1bb505ece39391da8eb4a` 施工。主实现提交为
`f00623a811e001f37a76e4135c03dabe8fd84a17`，实际/期望版本反馈修正为
`a7aab2be21bbdf03744131ef8868e5bf09064306`；双副本损坏丢失安装清单后仍显示已运行
后端停止提示的收口为 `a6fd0c95d6706cdc16f6d7afd178a7d0b2f9fe48`。本记录随后独立提交。
协议实验仓保持 `c7f5487a0fb9962b718187eec20428697ce4c244`，本轮没有改动。

没有更改 SDK wheel、SDK/manifest 协议来源 pin、Host 权限或开放贡献类型。
新增目录 pin 独立记录在 `server/extensions/catalog-source.json`，副本直接取自协议
提交的 Git blob，避免 Windows 工作区换行影响文件摘要。原有协议来源检查及目录
副本与已提交 Git blob 的 SHA-256 核对通过。

## 交付范围

| 责任边界 | 本轮实现 |
| --- | --- |
| 目录解释 | `server/extensions/catalog.py` 复用固定 S0 schema/政策；从现有 schema 和 profile 得出当前 contract，再按实际 Python、平台和 SDK 判断逐包兼容性。未知类型/contract、混合未来包不影响其他条目和有效安全事实 |
| 目录 main owner | `electron/src/main/extensionCatalog.ts` 固定唯一官方索引，缓存只展示；商店安装先刷新，renderer 只能提交 action、ID、版本，不能提交 URL、摘要、等级或来源；安装时及提交前重核当前条目和安全状态 |
| 网络与镜像 | `extensionDownloads.ts` 使用 Electron net、独立内存 Session、omit credentials、正常证书与系统代理；逐跳 HTTPS、最多 5 跳、120 秒请求期限、4 MiB 索引和 256 MiB wheel 上限；按解码后的流量计数 |
| 暂存清理 | 成功、失败、取消后清理；下一次启动后的请求回收有完整归属标记、只有预期普通文件的中断下载，跳过当前活动目录、链接、未知或损坏归属。复用桌面的单实例边界 |
| 安装事务 | `extensions.ts` 继续私有复制、inspect、原生确认、准备、校验、提交；在复制后的 wheel 上验证三元组。SDK-only/无依赖 Python 包的本地和商店入口都离线安装；第三方依赖只从本地走已有 resolver；资源包直接解包 |
| 安全事实 | `desktopSettings.ts` 与 `extensionSecurity.ts` 在原 settings/.bak 中保存最高序号、完整决定、检查时间和内容校验和；不添加分发来源或另一份配置文件。校验和用于意外损坏检测，不是签名 |
| 启动与显示 | 封禁覆盖本地/商店安装、启用和下一次后端启动；主题即时恢复内置底色并显示安全原因。已运行的被封禁版本如实显示待停止，安全旧版与被封禁的期望新版分开处理 |
| 最小用户入口 | 已安装管理器增加检查时间、手动检查及独立恢复按钮；刷新不清掉编辑草稿。HTTPS 产物镜像字段复用配置目录及设置 UI；完整浏览、详情、搜索、更新推荐和进度界面留在 S2 |

镜像采用 `<HTTPS 根地址>/<sha256>/<原 wheel 文件名>` 布局，只换产物字节位置；没有
索引镜像和静默切换来源。说明见 [客户端与恢复文档](../../extension_catalog.md)。

## 断电与恢复的实际语义

- 主副本都参与完整安全快照选择；最高序号的完整备份不能被较旧、但可解析的主文件
  覆盖。同序号冲突进入 unavailable，不逐字段拼接。
- 旧版有效设置没有安全字段时为 unseen。字段损坏且没有完整副本、双 NUL、截断等
  情况保留 `.corrupt-*` 字节证据，并将 unavailable 写回原有耐久存储。普通保存、
  markApplied、进程重启都不能把它变成空封禁；已知的冲突序号下限仍被保留。
- 备份先写、主文件后写之间的故障不报告刷新成功；已完整持久化的新备份仍是之后
  恢复必须尊重的事实。不可用哨兵自身的中断写入也不能悄悄恢复为空。
- 已知封禁不因目录缓存丢失、离线、卸载/重装或条目缺失消失。解除需更高序号明确
  allowed 决定；安全存储不可恢复时需要显式官方恢复，不可用普通刷新冒充恢复。
- 双副本整体损毁时，官方目录只能恢复安全事实，不能恢复其中已经丢失的安装记录、
  配置或凭据；受损证据保留。没有承诺抵抗主动删除全部本地状态，也没有物理断电实验。
- 普通撤回和版权下架只停止目录供应，不自动停用本地已装版本。封禁保留启用意图、
  凭据和包数据；不会把仍在运行的后端宣称为已停止。

## 验证结果

| 验证 | 结果与证据 |
| --- | --- |
| 完整 Electron 测试 | 最终 `npm --prefix electron test`：565 通过，`build/s1-electron-final.log` |
| Python 插件、作者、协议、SDK、发行及 Pi 权限作用域 | 按文件运行合计 187 通过、7 跳过，`build/s1-python-suites/*.log`。目录独立来源与发行复验另有 33 通过：`build/s1-source-final.log` |
| 跳过项 | POSIX 执行位 1 项、Windows symlink 权限 1 项；独立 pytest 未传安装旅程记录的作者契约 5 项。实际安装后的 Tone/Notes/Skill 运行检查见下，不将未运行的项目算作通过 |
| 实际下载、安装与损坏恢复 | 主实现提交 f00623a8 上的 `build/extension-catalog-journey/5f1362210254/report.json` 为 ok，30 个检查全为 true；之前完整复验 `89550fedcf27` 也通过 |
| 真实运行 | 同目录 `tone-runtime.json`：进程契约通过；`notes-runtime.json`：原生 MCP 传输发现、读写及关闭通过，native_agent 为 not_run；`handoff-runtime.json`：实际 Pi 读取/恢复通过，使用受控 loopback 模型 |
| 主题与用户入口 GUI | `output/diagnostics/extension-settings-ui/report.json` 为 passed，10 项检查。生产 React/CSS 在真实隐藏 Electron 窗口运行，包含中英文/窄窗口、刷新失败与草稿保留、显式恢复和镜像字段；截图包括 `security-recovery-and-mirror.png` |
| 封禁反馈收口 | a7aab2b 的插件单元组 71 通过，`build/s1-security-feedback.log`；覆盖安全旧版/被封禁新版与反向情况、活动主题立即回退和原因为安全封禁。双损坏导致清单丢失后，仍从内存中的实际加载事实显示停止提示；最终完整 Electron 集合 565 通过 |
| 构建及静态检查 | `npm run build` 完成配置/SDK/Pi 检查、TypeScript 和 Vite；最终反馈改动再经 tsc。保留既有大 chunk 提示。ruff 全仓、协议副本、Git whitespace 通过 |
| 文件系统隔离 | 两处 node_modules 都是独立目录，无 junction；没有改主仓、复制日常凭据、推送、合并、归档或启动日常后端 |

主要边界用例包括：摘要/ID/版本不符、下载后换字节、确认期间封禁、取消、超限、
失败清理、并发卸载串行化、缓存仅展示、过时目录重放、同序号冲突、缺失安全决定、
普通/版权撤回、SDK-only 与第三方依赖分支、未来类型/contract/mixed 目录，以及
主坏/副坏/主缺失/双坏/NUL/截断/安全字段校验和损坏、双文件写入中断、备份较新、
普通保存和显式恢复。未改变安装后默认禁用或失败更新保留旧版本的行为。

### 真实网络与测试信任的区分

真实 Electron 网络栈验证了公网 HTTPS 的系统证书检查、与默认 Session 一致的系统
代理解析、无效证书拒绝、HTTPS 重定向、HTTP 降级拒绝、CONNECT 代理隧道、认证与
Cookie 隔离、取消和体积限制。代理隧道只在测试 Session 中配置，没有修改系统代理。

故障服务器为本机 HTTPS：只在探针进程中钉住那一次生成的证书，系统证书库没有变化，
产品代码没有 TLS 例外。无效证书用独立主机名检查，避免 Chromium 缓存先前证书判定
干扰夹具。测试 wrapper 把固定官方索引请求映射到本机 fixture；产品没有索引覆盖参数。
发布者和独立审核记录都是明确的 fixture，不是四个包已正式审核、收录或发布的证明。
原生安装确认由驱动代答，`nativeDialogs=false`；没有调用付费 Fish/MiMo 服务。

四个复用产物均来自 D2 的 `build/extension-author-journey/4005cfb5b41a/wheels`，版本 0.1.0：

| 包 ID | wheel SHA-256 |
| --- | --- |
| com.example.tone-tts | cdbea2a6b70654952d9aeda97fec8085134ce6ecccfac2395f0b8467a7b2f0c1 |
| com.example.notes-mcp | a249c1233ab259ead7d401619e5f898ce95dfff993fd0447b0a47d20c2869b5f |
| com.example.handoff-skill | 20571002ac5be0f90720d845af0c32c3168cda508ad83acf61ac0a71861c7fab |
| com.example.desktop-theme | d70397a19c8698f0352cfe57df385ab648a9d7e8f0b8f7facf514ee429cf852e |

环境：Windows、CPython 3.12.10、Host API 0.1、SDK 0.1.0；SDK 来源仍为 49449a0f，
manifest 来源仍为 b30df736。完整来源和每次运行层级由上述 JSON 报告记录。
探针结束后安装目录按既有清理边界处理；双损坏证据和测试数据保留在 build 内。
报告中的临时安装快照不再作为之后执行插件的可用记录。

### 中间失败与修正

- 早期公网请求发现 Electron 响应头是 string 或 string[]，且内容可能由 Chromium
  解压。错误的 Content-Length 判断在下载 owner 修复并加入字符串/压缩响应回归。
- 本机证书夹具最初受证书判定缓存影响；调整测试主机名和精确临时证书校验，没有
  放宽产品证书验证。此前阻塞的探针进程树已按确切测试路径清理，驱动也增加超时树清理。
- 真实主题的目录 platforms 为空，表示未声明平台限制。reader 起初误判不兼容，已按
  manifest 默认语义修复，并加无平台限制的正例。
- 配置默认空 URL 暴露了设置 owner 未尊重 allow_empty 的情况；修复通用语义并保持
  非空 URL 的 scheme 校验。中文目录排序按原规则整理，没有放宽测试。
- 第一次 Python 合并运行结果是 186 通过、7 跳过、1 失败：发行检查要求新增源文件
  已纳入 Git。纳入后按与 CI 相同的分文件方式全部通过，发行及目录额外复验 33 通过。
- 中断下载归属回收测试曾发现 marker 长度常量写错，改为从唯一 marker 内容推导，
  不再保留第二份长度事实。最终整个 Electron 集合通过。

## 复验入口和后续

```powershell
npm --prefix electron test
npm --prefix electron run build
python -m pytest -q tests/test_extension_catalog.py tests/test_release_tooling.py
python tools/probes/extension_catalog_journey.py --execute --wheels build/extension-author-journey/4005cfb5b41a/wheels
node -e "require('node:child_process').execFileSync(require('./electron/node_modules/electron'), ['electron/tests/extensionSettings.smoke.mjs'], {stdio:'inherit',windowsHide:true,timeout:120000})"
```

Python 较大作用域按文件运行：extension_catalog、extensions、extension_resources、
extension_mcp、extension_skills、extension_examples、extension_fish_audio、
extension_protocol_provenance、extension_sdk_build、extension_authoring、extension_contract、
release_tooling、pi_mcp、pi_scoped_approvals（文件名均为 tests/test_*.py）。
本轮没有运行远端 GitHub Actions、跨平台资格或安装版完整发行检查。

S1 可以进入评审，下一阶段为 S2 的完整商店用户旅程。S0 仍只有本地实验索引；实际
官方索引发布、真实独立审核、仓库治理和安装版验收留在 S3；D3 外部作者资格仍未完成。
D0 协议首页链接仍需先合 Host 后更新。本轮遵守不合并主线，签名、自动更新、自动回滚
和新的插件能力均没有提前实现。

后续留档：S2 已在同一实验分支完成浏览、详情、进度和手动更新，见[第四批施工记录](community-preview-batch4-2026-10-21.md)。本文件保留 S1 结案时点及其验证边界。
