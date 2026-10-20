# 社区插件开发预览第二批：D2 / S0

日期：2026-10-21。状态：实验分支实现与本地验收完成，未推送、未合并、未发布。

继续使用两个仓库的 `codex/extension-community-preview` 分支。Host 从已修订的
`1840c8e4f646992ddaebea3139c8895f29743a8f` 开始，底层仍是包含 #234 的 `fd17e707`。
协议仓本批最终提交为 `c7f5487a0fb9962b718187eec20428697ce4c244`；目录生成器为
`85f4931`，精确来源记录在其 `catalog/index.json`。Host 本批提交可从本文件的 Git
历史定位。本批不改生产插件 owner、权限、SDK/协议来源 pin 或安装事务。

## D2 交付

- 新增 `tools/check_extension_contract.py`：显式 `--execute`，匹配实际 wheel 与独立
  安装记录的 ID/version/digest/manifest；不隐式安装依赖。每次运行建立临时 data/cache
  和 Agent 会话，使用已有 ProcessTree 限定子进程树与总超时。声明的远程服务另需显式开关。
- TTS 复用 RunnerClient 和 TTS proxy；MCP 复用原生投影与传输；Skill 复用真实 Pi
  发现、读取和会话恢复；主题复用 Electron 原生资源 owner、生产 themeApplication
  和实际 renderer CSS。没有新增通用 Runner 框架或生产模型调用。
- 报告区分 static/process_contract/native_agent/gui/remote_service，未运行保持
  not_run；失败非零退出并给出受控阶段、错误类、可用的 worker 退出码及 TTS 就绪状态。
  任意插件异常、工具返回和密钥值不进入报告。
- 保留六个现有示例，新增 MIT-0 `tone-tts`，使用随 Host 交付的固定 SDK wheel 输出
  合成 PCM。这是无付费依赖的 TTS 作者参考，不是 MiMo/Fish 的真实服务或听感资格。
- `tools/probes/extension_author_journey.py` 使用实际 Electron installer/store，构建
  Tone、Notes MCP、Handoff Skill、桌面主题的 0.1.0/0.2.0，完成安装、启用、更新、
  对应运行检查、停用、卸载、保留数据重装及显式数据删除。
- 作者文档补齐复制模板时的身份/元数据修改项、不同检查环境、场景参数和验证边界。
  Windows CI 在桌面构建后加入这条离线旅程，发行清单包含运行工具及其依赖源码。

## D2 验证

| 检查 | 结果与证据 |
| --- | --- |
| 最终四类真实安装旅程 | `build/extension-author-journey/4005cfb5b41a/report.json` 为 ok；12 个阶段全部完成，8 次按版本/类型划分的运行检查通过 |
| 真实 Provider 与失败路径 | 上述目录中的 `native-provider-regressions.log`：9 通过。Notes 经实际 Pi 完成 save/read，出现两次精确 MCP 调用审批；Skill 经实际 Pi 读取资源、恢复会话、更新/移除后拒绝旧会话，并覆盖无关新增与遮蔽 |
| 作者工具失败 | 未显式执行、错误 Provider 绑定、缺失资源、总超时均非零；输出不包含合成密钥。主题继承背景且保留有效对比度覆盖的用例通过 |
| Host 作用域回归 | 150 项通过、2 跳过；包括作者静态工具、Runner、MCP/Skill/资源/示例、Pi MCP/精确授权、协议来源、SDK 和发行工具 |
| 跳过项 | Windows 账户无法创建符号链接；POSIX 执行位契约 |
| 实际原生主题生命周期 | `build/extension-preview-batch2/theme-lifecycle/4f040befd2b3/report.json` 为 passed；资源主题与混合 TTS/主题的版本边界、取消更新、坏更新保留、停用/重装、数据删除、字体和进程退出通过 |
| Electron 回归与构建 | 89 项通过；独立依赖下 build 完成配置生成、SDK 摘要、Pi TypeScript、Electron TypeScript 和 Vite；保留既有大 chunk 提示 |
| 源码与发行检查 | 修改的 Python 经 ruff；Git whitespace 与相对文档链接检查通过；未改变 SDK wheel、协议副本及生产运行 owner |

Host 作用域命令：

```powershell
python -m pytest -q --tb=short -rs tests/test_extension_authoring.py tests/test_extensions.py tests/test_extension_mcp.py tests/test_extension_skills.py tests/test_extension_resources.py tests/test_extension_examples.py tests/test_pi_mcp.py tests/test_pi_scoped_approvals.py tests/test_extension_protocol_provenance.py tests/test_release_tooling.py tests/test_extension_sdk_build.py
python tools/probes/extension_author_journey.py
python tools/probes/extension_theme_lifecycle.py --output-dir build/extension-preview-batch2/theme-lifecycle
```

150 项的首轮结果是 149 通过、1 失败、2 跳过：发行扫描命中了新测试里名为 secret 的
合成长字串赋值。改为清楚的 fixture_value 后，该发行检查复验通过；未放宽扫描规则。
早期旅程探针也分别暴露了驱动的 MCP 返回结构/模型工具名选择、UTF-8 读取和新进程
校验缓存刷新遗漏，均在工具/测试驱动所属层修正。最终完整旅程重新运行后才计为通过。

确认由测试驱动代答，`nativeDialogs=false`。Pi 使用本地受控模型驱动真实原生工具，
不证明真实模型选择质量。主题检查包含实际 Electron/Chromium 隐藏窗口，但不冒称完整
手工文件选择、弹框或 Chat 窗口交互。第一批完整 GUI 的 15 项结果没有在本轮重新计数。
本轮没有调用付费 Fish/MiMo 服务，没有复制日常配置、凭据、Agent 会话或用户数据。

## S0 交付与验证

协议仓新增目录 schema、集中政策、公开审核清单、手工事实目录、独立安全事实文件、
共享未来兼容性 fixture、生成/校验/差异工具、固定 Windows CI 和空的序号 0 索引。
实际信息和逐项结果另见该仓 `catalog/README.md`、`REVIEW_CHECKLIST.md`、`VERIFICATION.md`。

- 唯一官方索引位置、GitHub 仓库信任根、审核治理与后续镜像边界已写明。首版纯 Python、
  X.Y.Z、SDK-only 依赖和 MCP 启动限制集中在目录工具；未加入通用 Host 运行校验。
- 审核对象是摘要对应的实际 wheel。源仓只提供辅助证据。工具输出实际版本内容差异，
  每个版本仍需独立人工审核；维护者自己的参考包不例外。
- 同一 ID/version 不能换字节或消失；撤回保留条目，版权原因与安全封禁分离。序号严格
  递增，明确解除必须使用新序号。未知贡献只使相应整包不兼容；坏格式/安全结构拒绝新快照。
- 输入 commit 和生成器 commit 都可追溯；时间取输入提交时间。测试中实际生成两次字节
  一致，错误缓存摘要被拒绝。已审核旧事实只能从受保护 main 的旧索引复用，不能从展示
  缓存取得权威，避免旧产物下载失败阻塞安全/版权撤回发布。
- 固定校验器仍为 Host `1840c8e`、Windows、Python 3.12.10 及 D1 最小依赖哈希。
  当前开发分支后续会前进；复跑目录检查时必须另外提供该精确 checkout，不把浮动 main
  或本批新 HEAD 冒充这个 pin。
- 独立、按哈希安装的协议仓环境全套测试：121 通过、1 跳过（符号链接权限）。最后补充
  GitHub 审核人大小写身份处理后，目录专项 28 通过。固定 Host 环境验证了五个现有
  参考包可生成目录，Fish 因第三方依赖只被目录拒绝，本地 Host 静态检查仍通过。
- 受保护基线 CI 检查入口在本地通过；最终空索引重建与 --check 一致。实际 GitHub Actions
  未运行，Host pin 需先公开可获取。没有绕过这一前置，也没有把本地结果冒称远端 CI 通过。

真实包的归属和独立审核尚未发生，所以 `catalog/packages/` 为空，索引没有真实收录。
测试中的 example.test 审核事实只是 fixture，不进入目录。未来客户端兼容矩阵在本批
确立并验证参考语义；Electron 真正的目录读取、耐久安全事实和安装入口复核属于 S1。

## 阶段状态与下一步

- D2：四类作者入口、独立环境参考旅程和 Windows 本地运行验收已交付。其他 Provider、
  平台、手工 GUI、真实模型和付费服务保持各自原有限制，不统称全部认证。
- S0：目录格式、收录政策、审核清单、发布工具与本地 fixture 验收已交付；远端 CI 和
  公开发布仍待相应分支/版本可获取及后续发布决定。
- D0 仍需先合入 Host，再把协议仓首页改到合入后的作者入口；本轮遵守不合并主线。
- D3 外部作者验证未开始。维护者模板不能代替两名外部作者的资格。
- 下一阶段 S1：Electron net 下载与镜像、可信身份传入现有安装事务、按 wheel 依赖选择
  准备方式，以及设置存储中的封禁/最高序号、备份恢复与启用边界。S2 才提供商店 UI。
- S3 的真实收录、独立审核和仓库治理没有提前宣称完成。签名、自动更新、自动回滚及新
  插件类型均未实现。本批没有推送、建 PR、合并或归档；Node 依赖继续独立安装，无主仓联接。
