# 插件商店外审修复（2026-10-23）

本轮核对主仓本地 pr_audit_254-256_extensions_4-5_2026-10-23.md，负责 Host #256 与 Extensions #5。用户决定：空商店保留浏览并标注预览；图标用新 schema（由原 UI 会话更新 #255/#4）；词典 #254 保留 rc2，发布时再定。两个商店 PR 保持 Draft，不合并、不发布目录，不操作正在运行的 Amadeus。

## 审计结论与处理

- C1 属实，构建层修复。源码身份校验正确指出 wallpaper packages.lock.json 被 publish 隐式 restore 改写。仅加 RestoreLockedMode 首次得到 NU1004，进一步定位为锁文件缺少发布使用的 win-x64 目标。csproj 声明 RuntimeIdentifiers=win-x64、RestoreLockedMode=true；受控 force-evaluate 只增加空的 net8.0-windows7.0/win-x64 目标，未改任何依赖版本/哈希。此后实际 BuildOnly/Rebuild 成功、锁字节不变；保留打包后的完整源码身份校验。Source Release 的触发路径补入 wallpaper 与 build hook。
- D1 属实，执行权威边界修复。原 proposal 测试可污染同 job 解释器；python -I 不禁用 site .pth。受信检查移到独立 catalog-admission.yml，从 pull_request_target 的受保护基线加载 workflow/代码，在全新环境安装基线及固定 Host 的带哈希依赖；不运行 proposal requirements、测试或工具，不下载 proposal job 的 cache/artifact，checkout 不保留凭据。普通 pull_request 的 catalog-contracts 只做开发回归，不再冒充受信 admission。
- 首次 bootstrap 的边界：新受信 workflow 尚未存在于 main，不能为自身引入出具可信证明。本 PR 仍只包含空目录，需要人工审核工作流。bootstrap 合入后、首个包/发布 PR 之前，必须把 catalog-admission 设为必需检查，并用故意无效投稿验证受保护路径拒绝。当前未改仓库规则，也未把本地隔离演练称为该远端门禁验收。
- C3/D2 属实。唯一官方地址改为 catalog/v1/index.json，工具、客户端、测试 fixture 和文档同步，移除未发布的无版本入口。v1 路径永久绑定严格 v1 格式；新格式另开路径。支持期内旧路径仍需发布可理解的安全事实并递增序号，不能以 v2 替换 v1 导致旧客户端丢失封禁信息。客户端本地缓存文件名不变。
- C4 属实。保留 Electron net、隔离 session、HTTPS/重定向和大小边界，逐块同步写盘改为有背压的异步 stream pipeline，完成后等待 flush/close。30 秒停滞超时，每次新数据重置；另设按准入大小、32 KiB/s 加连接时间计算的总预算（最少两分钟）。仍可取消，失败只清理自有 staging 文件，不能覆盖/删除已有目标。
- Electron 44 的官方源码 IncomingMessage extends Readable，生成 d.ts 却只声明 EventEmitter。本地首次 TypeScript 构建因此拒绝 pipeline 参数；在该单一 API 边界使用明确的 ReadableStream 类型断言，并通过实际 Electron HTTPS 流程验证。没有替换网络栈或放宽类型检查全局配置。
- C5 采纳：恢复 main 的 package.json build 格式与 Unicode 描述，只保留新增 checker extraResources 和已有版本选择。
- C8/D3：按用户明确批准创建注释标签 extension-catalog-checker-d1，剥离后为 1840c8e4f646992ddaebea3139c8895f29743a8f。工具仍钉 SHA，不信任可移动标签；未创建 Release。之后将检查器重新认证至 main 并验证真实 v1 图标 wheel，仍是后续资格；当前固定 checker 不接受图标包。
- C2 是跨未合并分支的真实集成门槛。本轮先复用已有 capability-page-tabs，去除商店独立 tab CSS。#255 的全部 token/容器规则及卡片启用开关尚未进入 #256；剩余 CSS 两处 token、容器布局和 activation 探针必须在整合 #255 后一起更新并跑组合认证，当前不加探测多套 UI 的兼容分支，也不声称组合已通过。
- C6 是 PR 组织建议，不是代码缺陷。本轮保留已验证的 Pi/端口夹具及历史施工记录，避免重写已被目录 pin 引用的历史。最新摘要为本文件；旧记录不当作当前结果。

A1/A2 已由 #255 的 0e761b9d 修复，不再重复改它。B1 规范冲突成立，但外审“只提升 Host API 即可让旧宿主显示 incompatible”的建议不成立：已发布旧 Host 先检查 schema，再检查 engines。用户选定新 schema，已授权交给原 UI 会话落实。A3 及新图标 pin 的主线来源资格仍需在协议合入后验证。E2 保持 rc2；不代替词典发布决定。

C7 的旧状态已过期：检查时 #256 Python Windows 的全部 job 与 cpu-model-less 已成功，无需重跑。source-archive 的旧失败真实存在；本轮新 head 会重新触发构建资格，不能将旧绿灯当作新 head 结果。

## 本地验证

- Host 相关 Python 回归：67 通过。build/audit-host-tests.log。
- Electron 全量：579 通过、0 跳过；完整 build 通过。新增慢速持续传输跨越旧 120 秒上限、停滞清理、既有文件保留和空目录预览验证。
- 全量首次 578/579，失败是新增翻译键未排序；修正字典顺序后全量通过，没有跳过/忽略或放宽断言。
- 实际 Electron 商店 UI + HTTPS + 真实参考 wheel：27 项通过，build/extension-catalog-journey/21b5eaab6443/report.json；覆盖取消、更新失败保持旧版本、实际 TTS Runner、封禁/撤回展示、窄屏和无远程图片。独立临时窗口/配置，不启动日常后端。
- 实际 Electron 传输/安装/新进程恢复：30 项通过，build/extension-catalog-journey/750626bea988/report.json。含系统代理/证书、HTTPS 重定向、镜像错摘要、真实 Notes/Tone/Handoff、封禁缓存删除、主设置 NUL 恢复、双 NUL 拒绝、新 Electron 进程仍受限及显式恢复；临时 profile、fixture authority，不是完整 shipping-shell 或物理断电。
- 协议完整套件：132 通过、1 个原有 Windows symlink 权限跳过。build/audit-protocol-tests.log。
- 新版本化路径的正式本地 Git/HTTPS 演练：六阶段 input/publication CI、可重复生成及 12 份候选/实际分发验收通过，build/audit-publisher/c8ecec3fcfe4/result.json。仍为模拟审批、desktop=not_run。
- 隔离污染探针 build/audit-admission/result.json：复现 -I 执行污染环境中的 .pth；fresh admission 环境不执行 proposal 的恶意 requirements、sitecustomize 或测试文件；合法空输入通过，无效安全记录拒绝。可信工具为 bd4b233471fca608ab339011fb9371ec675c47b6。该探针不是 GitHub pull_request_target 的线上验收。
- Host 全仓 Ruff、协议修改的 Python 文件 Ruff 通过。额外全协议 Ruff 暴露 19 项原有 SDK/manifest 等问题（未改部分）；未修改固定协议来源或加入忽略项来消除它们。

完整 shipping-shell 重启/NUL 恢复的第八批证据保持为历史；本轮不关闭用户应用来重跑占用 17777 的脚本。跨 #255 的组合桌面、首次受信 workflow 主线运行、分支必需检查配置、独立作者和真实包审核均未被本轮结果替代。

参考：Electron v44.0.0 lib/common/api/net-client-request.ts 与官方 IncomingMessage 文档；GitHub pull_request_target 事件文档。网络类型断言只针对已核实的实际 Readable 实现。
