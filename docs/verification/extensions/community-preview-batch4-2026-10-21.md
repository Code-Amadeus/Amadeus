# 社区插件开发预览第四批：S2 商店用户旅程

日期：2026-10-21。状态：实验分支实现与本地验收完成，未推送、未合并、未发布。

继续使用 Host `codex/extension-community-preview`，起点为 S1 留档提交 `39ed76f5`。
主实现：`24d410e47b920d69445a3ced5163df676180321f`；闭合语音角色展示修正：
`41e4836d45095c48a0bb96584a629e7e73e0c8c7`；明确恢复动作修正为
`a78399dba0bb37cab0befa265f43ce5b1a6d8e04`。本记录随后单独提交。
协议实验分支生成器补充：`58d3892d55c6a7ec6f8626153f503291bb10ab9a`，其验证留档为 `a5ca015f30f074474ec12633c9c8cc8fc45e9a02`。

Host API、开放贡献类型、运行 owner、权限、SDK wheel 和 manifest/SDK 来源 pin 均未改变。
仅独立目录副本 pin 更新到上述协议生成器提交；当前目录仍为 v1，没有增加分发来源
持久字段、自动更新、签名、回滚或插件沙箱承诺。

## 已交付

| 范围 | 实现与责任 |
| --- | --- |
| 同一管理器内导航 | `ExtensionSettings.tsx` 增加“已安装 / 浏览”；Tab/方向键/Home/End 可操作。切换视图保留未提交配置，下载状态在两个视图中可见，不新建独立插件设置页 |
| 发现与详情 | 新增 `ExtensionCatalog.tsx`：名称/说明/能力搜索、类型筛选、版本选择、维护者/许可、平台/Host API/Python/SDK、wheel 体积、产物地址/摘要、审核范围与检查时间、生效方式及外部访问说明 |
| 更新选择 | `extensionCatalogModel.ts` 按三个整数精确比较 X.Y.Z，跳过不兼容、撤回、封禁及待移除状态；更新最高版本不兼容时保留可安装的较新版本。非 X.Y.Z 本地版本不猜测升级顺序，允许明确选择版本走原生确认 |
| 真实阶段反馈 | main 维护短期检查/下载/校验/确认/准备进度，installer 在实际边界报告阶段；renderer 只轮询紧凑 status，不重复读取整个索引。下载完成不等于保存成功；取消等待操作回执，错误不冒充成功取消 |
| main 权限边界 | 受信 renderer 仍只能提交 action/ID/版本。`dispatch()` 被生产 IPC 和真实 UI 探针共同使用；URL/摘要/审核不是 renderer 的可信输入。主页及源码链接由 main 解析并验证 HTTPS、无凭据后打开 |
| 安装后旅程 | 新装保持禁用，成功提示引导到配置、启用、Agent/Voice/Appearance。已安装卡片继续区分已保存、待重启、实际加载和受阻。失败更新保留既有状态；安全阻止时仍可停用和卸载 |
| 扩展兼容 | 未来类型/contract 及混合未来包可浏览具体原因但不可安装；筛选未来能力时显示实际匹配版本。ASR 与未来语音角色不会被误标成 TTS，也没有因此开放执行能力 |
| 文本和样式 | 第三方说明、作者与访问数据只作文本，不渲染 HTML/Markdown、不请求远程图片；中文文案、窄窗口换行、详情焦点和原有主题 token 接入 |
| 来源与验证 | 协议 `artifact_display()` 从 wheel 生成可选 `display.access`；只含 OS 声明及 TTS/MCP 服务/命令模板，不复制配置默认值、环境值或 bearer token。旧目录没有该字段时明示缺失，并沿用安装确认中的 wheel 披露 |

协议 display 是原有 v1 的开放展示字段，本轮未改变格式主版本、安全决定结构或审核
规则。老条目可以继续复用历史生成事实而没有 access；客户端不会据缺字段宣称无外部
访问。协议仓的公开索引仍没有真实收录，测试审核记录不进入目录。

## 验证与证据

| 检查 | 结果 |
| --- | --- |
| 最终完整 Electron 集合 | 576 通过；`build/s2-electron-final.log`。覆盖现有设置/安装/主题/生命周期，新增目录模型、取消语义、紧凑 status、草稿保留与未来语音角色 |
| Host Python 作用域 | 105 通过、1 跳过；`build/s2-python-final.log`。目录/作者工具/安装静态 owner/资源/协议来源/SDK/发行检查。跳过项为 Windows 上的 POSIX 执行位契约 |
| 协议目录单元检查 | 18 通过、11 跳过；其中新增公开披露提取与敏感默认值/环境值排除。11 项要求显式 1840c8e 固定 Host 检查环境，本轮没有重跑该固定历史 CI；不把跳过当通过 |
| 真实商店 UI 旅程 | 在主实现提交 24d410e 上，`build/extension-catalog-journey/add316eb2079/report.json` 为 ok，27 项全部通过。此前完整轮次 `79a32b43af93` 亦通过 |
| 真实运行 | 同目录 `tone-runtime.json` 为 ok：UI 安装并启用的 Tone TTS 经实际 Runner 完成运行契约。真实主题由安装器原生校验，且通过生产 Appearance / ThemeProvider / themeApplication 实际绘制和更新 |
| 既有 GUI 回归 | `output/diagnostics/extension-settings-ui/report.json` 为 passed，10 项检查；真实隐藏 Electron 中保留旧配置、MCP/Skill 绑定展示、主题、导航、刷新/恢复与窄窗口行为。该套 Host 响应为合成，不混作真实网络安装证据 |
| 构建与静态检查 | 完整 npm build、Pi/SDK/profile/config 检查及 TypeScript 通过；ruff 全仓通过；协议副本来源、发行检查与 whitespace 检查通过。保留既有 Vite 大 chunk 提示 |

真实 UI 使用生产 renderer、preload 桥接、main `dispatch`、Electron net、installer 和
settings owner。它使用独立 profile、loopback HTTPS 与临时证书精确 pin；测试代码
才将固定官方索引请求映射到 fixture，产品没有索引覆盖开关。没有修改系统信任库或
系统代理，没有复制日常配置/凭据，也没有调用付费 Fish/MiMo 服务。

原生安装确认由驱动代答（`nativeDialogs=false`），完整 Chat/backend 未启动
（`fullBackend=false`）。TTS 运行由明确执行的契约工具验证；主题使用实际已安装资源。
因此这些证据证明商店界面到安装/资源应用/Runner 的连接，不等同于手工确认、完整
聊天体验、全部 Provider 资格或正式发布审查。

### 27 项真实 UI 检查覆盖

- 只打开“已安装”不联网；进入“浏览”取得目录。真实 wheel 详情和第三方 HTML 字符串
  以纯文本显示；危险主页被 main 拒绝，安全源码链接只将验证后的 HTTPS 交给外链 owner。
- 下载显示实际字节进度，切换到已安装仍可取消。取消后没有新记录；双击只确认并提交
  一次；新装处于禁用状态。
- 主题安装、启用、进入 Appearance 选用后实际绘制；0.1.0 → 0.2.0 手动更新；合成
  0.10.0 混合未来版本没有覆盖兼容更新推荐，详情给出原因且强行 IPC 安装也被拒绝。
- 错字节更新失败保留已保存/已应用的旧主题；成功更新保留启用意图并应用资源包；离线
  刷新保留配置草稿并清掉旧成功提示。
- 停用恢复内置外观；卸载默认保留数据；重装后明确选择删除数据才删除。
- Tone TTS 经界面安装/启用，实际 Runner 输出契约通过，然后停用/卸载走原有停止清理边界。
- 中文封禁、禁用安装按钮与版权下架原因分别可见；560px 页面不横向溢出；没有远程图片
  请求、renderer 未处理错误或遗留的活动下载目录。

五张截图位于该轮报告目录：`store-details-en.png`、`store-download-en.png`、
`store-future-version-en.png`、`store-updated-en.png`、`store-withdrawal-zh-narrow.png`。
人工读取了英文详情与中文窄窗口截图；真实滚动位置可能只显示详情的一部分。

使用 D2 原有 `build/extension-author-journey/4005cfb5b41a/wheels`，四类包的
0.1.0 / 0.2.0 共八份 wheel 均由同一静态 owner 检查并生成 fixture。完整 ID、版本、
SHA-256 在 `report.json.artifacts` 中；安装和使用旅程选择主题、Tone TTS。0.1.0 的
摘要与第三批记录一致，0.2.0 在该报告中独立记录。

## 测试中发现和处理的问题

- 原有浅层 React harness 没有调用 effect cleanup，加入进度轮询后会留下定时器。
  在测试 owner 修复卸载/重绘清理；原“离线页面完全没有 input”的断言改为检查未打开
  配置表单，允许离线浏览搜索框。没有删除草稿/错误/运行状态的行为断言。
- GUI fixture 漏了后端离线时会调用的 startup-failure API，又只改变 localStorage
  而没有同步真实设置中的语言偏好；补齐显式“无启动故障”响应与独立测试设置，使用
  原子 DOM 查找/点击，未改变产品逻辑迎合夹具。驱动脚本启动/引号错误也在夹具层修复，
  增加预先语法检查和有界 UI 查询；精确清理了失败测试进程树。
- 一轮 fixture 在刷新时移除了正查看的版本，界面正确关闭安装并显示“版本已不在目录”。
  正向旅程随后遵守版本保留规则，显式选择 Tone 版本；没有增加自动替换用户所选版本的 fallback。
- 类型筛选起初可能命中未来版本却展示同包较旧版本；修正为从实际匹配版本中选卡片，
  全部版本仍在详情选择器可见。已补同包未来版本正反测试。
- 取消信号不能覆盖 installer 自身的准备/持久化错误。仅将下载阶段的中止转为取消；
  installer 的明确取消例外继续沿用，其他错误保留真实失败，避免误报“没有保存”。
- 最后收口语音角色文案，ASR/未来 role 保持身份且仍不兼容，没有映射到 TTS。该改动的
  模型/文本/翻译十项检查和完整 Electron 集合通过。
- 进入 Browse 只能触发普通刷新，损坏安全状态必须通过明确的恢复按钮请求 recover；
  不将导航到商店当作恢复授权。对应组件组 16 项与 GUI 恢复回归通过。

## 复验与下一步

```powershell
npm --prefix electron test
npm --prefix electron run build
python -m pytest -q --tb=short -rs tests/test_extension_catalog.py tests/test_extension_authoring.py tests/test_extensions.py tests/test_extension_resources.py tests/test_extension_protocol_provenance.py tests/test_extension_sdk_build.py tests/test_release_tooling.py
python tools/probes/extension_catalog_journey.py --execute --store-ui --wheels build/extension-author-journey/4005cfb5b41a/wheels
node -e "require('node:child_process').execFileSync(require('./electron/node_modules/electron'), ['electron/tests/extensionSettings.smoke.mjs'], {stdio:'inherit',windowsHide:true,timeout:120000})"
```

Windows CI 在 D2 生成真实参考 wheel 后增加同一 `--store-ui` 旅程，并收集报告、运行
结果、截图和日志；不会上传临时私钥。此次没有执行远端 Actions、跨平台或完整安装版
发行资格，不把本地成功写成远端 CI 通过。

S2 可进入评审。S3 仍需正式目录发布、每个实际产物的独立审核、仓库治理、打包/升级
和干净环境验收；D3 两名外部作者资格未完成。D0 协议首页链接仍按先合 Host 后改入口
的顺序。本轮不推送、不合并主线、不归档 worktree；两处 Node 依赖继续是独立目录。
