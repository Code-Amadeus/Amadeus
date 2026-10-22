# 复审整合与主线合并记录（2026-10-23）

用户授权：修复阻塞后合并五个 PR，不发布插件包或创建 Release。词典继续 rc2；N1 四色方案按用户最新决定保留，主题适配作为后续项。主工作区的本地文件不切换、不覆盖。

## 已完成的合并与门禁

- Extensions #4：merge commit 6f3fee2d386125d6c31a4c8157f53b254e35dd79；b444dd8 图标协议 pin 在 main 上可达。以该 main 的独立检出运行 #255 的 check_extension_protocol.py --source，通过。
- Host #255：全部检查已成功（滞留 macOS 状态自行完成，无需重跑），按 squash 合入 49c67f5c0cbbca9744bfcfad7ebfad370da94f27。
- Extensions #5：先合入协议 main 并跑完整组合测试（164 通过、2 个条件跳过），新 head 的四项 CI 成功，再 merge 至 e2fc0e93fa7cfd9bf98e378cac6e158f14181b9a；历史政策 pin 58d3892d 仍可达。
- main 的目录门禁使用增量规则集 25654501，唯一新增要求为 GitHub Actions（integration 15368）的 catalog-admission，strict=true、无 bypass。没有覆盖或移除评审规则。一次整体 PUT 保护请求被自动审批拒绝，理由是空评审配置可能弱化保护；未执行该请求，改为只追加检查的规则集。
- 真实远端资格 PR Extensions #6：42b12d5 的无效安全记录被受信 job 拒绝、PR 为 BLOCKED；4b99af9 恢复与 main 相同数据后全部检查通过、PR 为 CLEAN。测试 PR 关闭且未合并，没有无效记录进入 main。
- Host #254：同步 #255 后所有必需检查成功，按保留 rc2 的决定 squash 至 48f23acffcbbb036f620fbbf14d1a976b0c22539。没有发布词典 ZIP；代码版验收使用它明确提供的 code-only 构建配置。

## #256 的组合修复

实验分支以 merge 保留历史，先整合 UI main（016b0e0），再整合词典 main（25276b0）。八处 UI/IPC/类型/翻译/fixture 冲突及两处打包/源码政策冲突按语义合并。

保留 UI 的 Installed plugins 标题操作、卡片开关、图标、卡内配置、详情与移除；保留商店页签、进度、手动更新、精确产物审核/撤回/封禁状态。封禁/安全存储不可用时不能开启包，但仍可关闭；草稿跨页签保留。镜像设置保留在 Plugins 页面。

商店样式使用现有角色 token 和 settings-main 容器查询。N1 的四色状态样式未改变。预览、SDK 和摘要标签通过当前 i18n 检查。

N2：extension-host 资源过滤改为 server/extensions/*.schema.json。资源选择回归在独立暂存目录运行实际 helper，v0 与 v1 图标 wheel 均通过；删除暂存 v1 schema 后按预期失败，不能借源码目录补足。shipping-shell packaged 验收也直接运行 resources/extension-host 中的 helper，重打真实参考 wheel 的 v1 图标变体并更新 RECORD；未导入插件代码。

探针按明确包 ID 定位卡片开关，详情内执行移除；不再借 Configure/Save 改 enabled。已适配 extension_release_shell、extension_store_roundtrip、extensionStore.smoke，保留原有错误、权限、重启及恢复断言。旧包待删除时仍可能留在列表，因此不能用“第一个/唯一开关”推测当前包。

Windows CI 增加正式生成器、固定 Host checker 和真实打包客户端的六阶段发布/恢复闭环。工具来源固定为已合入的协议 e2fc0e9，checker 保持 1840c8e；不收录 v1 图标包。上传范围排除这项演练的私钥和 profile。

## 本地证据与正在完成的检查

- Electron 全量 603 项通过，build 通过；真实商店 UI 流程通过（build/extension-catalog-journey/c927bea66582/report.json）。
- 插件相关 Python 158 通过、7 个原有条件跳过；v0/v1 打包与作者/图标测试 31 通过、1 条件跳过；词典并入后的发布/打包回归 42 通过、2 条件跳过。没有放宽超时/断言或增加忽略。
- N2 实际 code-only exe 已打包；从其 resources/extension-host 校验真实 v0 与 v1 wheel 均成功，build/merge-actual-bundle-schemas.json。
- 独立 extensionSettings UI 验收通过。首次用 PowerShell 直接启动 GUI exe 导致父进程提前返回、Vite 向关闭的日志管道写入 EPIPE，用户看到了测试窗口弹窗。确认 PID、可执行路径和命令后仅清理该测试进程；改用现有 ProcessTree 和持有文件日志句柄的 Python 启动器，等待真实结束后通过。没有在产品中忽略 EPIPE。
- 随后用户明确授权关闭当前 Amadeus；通过正常窗口关闭请求退出 UI 及所属后台，释放 17777，开始本机完整验收。未删除或改写用户 profile，不自动重启日常应用。
- 完整验收探索轮保留失败：聊天侧栏 Artifacts 悬停定位两次超时；探针改用既有焦点维持展开契约，额外断言页签 aria-pressed=true，并保留内容可见性和原超时；整合探针的多卡片选择歧义及一次参数名错误已定位修正。最终完整源码/打包/发布闭环及新 head 的 CI 结果以各自回执为准，未完成前不将 #256 记为已合并。

后续独立资格：把目录 checker 重新认证到含 v1 的 Host main、将 manifest 版本纳入目录兼容信息并覆盖旧客户端、测试真实图标 wheel。当前历史 checker 仍拒绝它，空目录未发布任何包。历史计划和 Pi 测试保留，不重写已被 pin 引用的提交。
