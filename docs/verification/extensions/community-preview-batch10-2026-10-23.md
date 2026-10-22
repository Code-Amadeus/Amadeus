# 插件商店主线集成与草稿 PR 验证（2026-10-23）

用户授权在当前实验分支解决最新主线冲突，采用主线已有依赖修复，验证后向两个仓库推草稿 PR；不合并主线、不公开发布、不影响正在运行的 Amadeus。本记录补充第九批，历史 ZIP、失败记录和桌面结果不改写。

## 主线集成

Host 从实验提交 9e3938c1 合入 main 5382b32901024787ada8d70ac10fda23b9b4f2fa，集成提交 af56e72c70cbe6aac874e3604b8ba5b6b65baf1f。这里是 main 合入实验分支，不是实验合入 main。协议仓以 bf1a848448a19ed20eca029e8cc3991563e42e95 为基线，工具提交 8fb39ba 已包含该基线，无需冲突合并。

依赖冲突归属集成层：完整采用 main 的 Qwen vendor、上游 pyproject 快照、来源说明和依赖测试；不保留实验分支旧的 initializer 别名。pyproject.toml 与 uv.lock 相对 main 只新增 extension-authoring 依赖组；已有包版本、Pillow/certifi 修复、包级构建后端约束均复用 main。provenance 保留 main 全部组件，只增加目录政策来源。model-less smoke 测试保留两边各自新增的契约。

固定目录 Host checker 1840c8e4f646992ddaebea3139c8895f29743a8f、Host 内目录政策 pin 58d3892d55c6a7ec6f8626153f503291bb10ab9a、SDK/运行协议 pin 均未改。先推 Host 实验分支，使协议 Actions 能获取历史 checker；再推协议分支和建立草稿 PR。空 bootstrap 不加入审核记录、公开包或安全决定。

## 本轮验证

- Host 插件、作者工具、目录、运行契约、打包、发布工具与主线依赖来源回归：184 通过、7 个已有条件跳过；原有 locale 弃用警告。包含实际隔离构建 Qwen sdist/wheel 和源码一致性校验。日志 build/integration-python.log。
- Pi MCP 与 scoped approvals：30 通过，未放宽原超时或断言。日志 build/integration-pi-tests.log。
- Electron 全量单测：576 通过、0 跳过；npm --prefix electron run build 通过（配置生成检查、SDK 摘要、Pi typecheck、TypeScript、Vite）。日志 build/integration-electron-tests.log 和 integration-electron-build.log。Vite 仅有现有大 chunk 提示。
- 协议仓全量测试，明确指定历史 checker 及其独立最小环境：132 通过、1 个 Windows symlink 权限条件跳过。日志 build/integration-protocol-tests.log。
- uv lock --check 通过；按 CI 参数 uv export --locked --only-group extension-authoring --no-emit-project --no-header 导出后，与已提交的最小带哈希清单逐字节相同。
- 全仓 Ruff 通过；未增加忽略或排除项；依赖实现选定路径与 main 无差异。

测试工具问题单独处理：首次 Electron 全量测试有 4 项因固定使用 15173 与用户运行中的 Vite 冲突。仅将需要真实监听器的自定义端口夹具改为临时可用端口；默认 5173、端口解析、端口占用必须拒绝、子进程清理与首屏可见性断言保持。没有关闭占用者、增加重试/skip、放宽超时或修改产品端口行为。

调整后第一次全量跑有一个 uiThemeSettings.test.mjs worker 退出码 1、无内部失败堆栈；原样单跑该文件 2 项通过，随后原样全量 576 项通过。根因尚未确认，不把这次退出归因于产品或端口。首次端口失败与 worker 退出分别保留于 integration-electron-tests-initial.log、integration-electron-tests-worker-exit.log。

## 六阶段维护端复验

按 docs/extension_store_demo.md 执行 extension_store_demo.py --execute --publisher-only，复用真实参考 wheel 与固定 checker；协议生成器为 8fb39bac8450562296975492d007dd8b852c407b。结果 build/integration-publisher/8e174b9f84b6/result.json：ok=true、desktop=not_run。

首次上架、更新、版权撤回、安全封禁、解除封禁、恢复分发六个阶段均完成 input CI、publication CI、原字节重建及候选/已分发索引验证，共 12 次分发验证通过。错误摘要投稿先被拒绝，同一份投稿修订后才获得模拟批准；生成结果篡改和安全事实丢失继续被正式工具拒绝。这是本地 Git + loopback HTTPS 演练，simulated_review_only=true，不是 GitHub 人类审批或公开目录发布。

## 与 UI/UX 和图标 PR 的衔接

核对时 Host #255 为 draft，head 44403f355971797ccea9fd6abf6bb91fba53fc2c；协议 #4 为 draft，head c3d1d897a3a97dc23f9880d2de042cc6cc9372ef。本轮不合入或修改它们。

Host 有 13 个共享修改文件，涉及插件设置、main/preload IPC、类型、样式/中文文本、Host profile 与 smoke。后续同时合入时必须保留商店浏览/详情/进度/手动更新/安全操作和包内图标读取两类行为，重新跑对应 UI、IPC、图片边界和目录测试；不能用整文件 ours/theirs 覆盖。

协议仓文件层面没有重叠，但有语义依赖：当前及固定历史 checker 的 manifest schema 都没有 icon，且 additionalProperties=false。因此图标协议合入不意味着目录已能收录带 icon 的包。届时需明确升级固定 Host checker/其哈希依赖、验证 Windows 真实图标 wheel 预检和生成/消费兼容，再协调 Host 协议与目录来源 pin。首版目录不加载远程图片的边界保持。本轮未新增图标字段，也不把这项跨 PR 资格记成通过。

## 验证边界与交接

本机日常 Amadeus 的 17777 和 Vite 15173 监听保持原进程。本轮不运行完整后台/打包版桌面验收；第八批 15+25 桌面闭环结果是历史证据。第九批新增的完整桌面断言仍需将来端口空闲时执行。本轮仅运行单测自有的临时 Electron 窗口和维护端工具。

正式发布仍需真实非作者审核、素材授权、公开地址、main 保护/2FA/最小发布权限、远端 Actions 与独立作者资格。草稿 PR 是代码审查入口，不构成发布或合并批准。旧外审包保持原摘要；本轮以集成提交、此记录和本地原始日志补充。
