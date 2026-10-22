# 插件基础设施外审交接：2026-10-21

当前 r3 可交付审查，新增完整本地维护端到客户端闭环；r2 的依赖/Pi 修复及历史失败作为明确历史保留。远端 CI、独立真实产物审核和公开 S3 门槛仍未关闭，**不是合并或发布批准**。没有推送、合并主线或发布索引；用户自行决定将本地材料交给谁。

## 2026-10-23 投稿流程补充

[第九批记录](community-preview-batch9-2026-10-23.md)补充一个预检命令、一个作者 PR、同投稿返修、维护者发布交接与候选/实际分发验证。协议 38 项、Host 30 项通过，维护端六阶段通过。本轮按用户选择不重跑桌面；第八批完整桌面结果保留为历史证据。原 r3 和 CI 补充包不改写，新证据另附。草稿 PR 材料保留本地，Host 推送前还需与已合入新依赖的 main 做隔离集成。

## r3 完整桌面入口

先读 [第八批施工记录](community-preview-batch8-2026-10-22.md) 与 [可重复演示入口](../../extension_store_demo.md)。新代码为 c44799eaa12c983245c1551cfd6f901007997b05，新增验收/演示工具，没有改变生产下载、安装、安全或权限实现。

正式目录工具在隔离 Git 历史中完成六次输入与发布检查，生成结果通过本地 HTTPS 原字节进入实际客户端。源码与打包版各 15 项桌面 + 25 项闭环检查通过；每轮 4 次应用重启、5 次安装后的 Notes 原生 MCP 合约检查、10 次客户端目录摘要比对。打包版从干净 c44799e 完整执行最终断言；源码探索轮的 dirty 身份与其后的断言补强在施工记录中单列，没有改写原报告。

优先核对 r3 的 evidence/publication/source、packaged：每阶段 index.json 与 publication.json、正式 CLI 命令/退出码、HTTP 请求摘要、原生合约与截图，以及自包含 demo-history.bundle 中的输入/发布提交。模拟身份、模拟 approved ref 和临时证书不能被误认成实际人工审核、GitHub 权限或公开信任根。r3 也保留本轮四个桌面探索失败和一次归档白名单失败。没有放宽原有测试、添加重试/skip/忽略项。


## 范围和入口

前批修复背景见 [第七批修复记录](community-preview-batch7-2026-10-21.md)、[第六批历史记录](community-preview-batch6-2026-10-21.md) 与 [发布门槛](store-v1.md)，再看 [获认可的计划](../../extension_community_preview_and_store_plan_2026-10-21.md)。第一至第六批记录是历史证据，不应替代最新的结果/限制。

- Host 审查范围：`fd17e707d8793480003f929bc8c78a8d134e41b5..codex/extension-community-preview`。
- Host 生产应用代码仍为第七批范围；新增闭环工具端点为 c44799eaa12c983245c1551cfd6f901007997b05。两轮实际受测身份见第八批记录和原始报告。bundle 的精确记录末端由 review-manifest.json 指明。
- 协议仓范围：`bf1a848448a19ed20eca029e8cc3991563e42e95..153153598a9f94e3b44a477e99c3017f978d13e5`。
- 固定目录 checker：Host `1840c8e4f646992ddaebea3139c8895f29743a8f`；Host 内目录副本 pin `58d3892d55c6a7ec6f8626153f503291bb10ab9a`。SDK/protocol pins 未变。

外审 ZIP 里的两个增量 Git bundle 需要上述公共基线提交。它们包含当前未推送提交以及固定历史 checker，供在已有仓库副本中 `git bundle verify`、`git fetch <bundle> refs/heads/codex/extension-community-preview:refs/heads/review-extension-store` 后审查。不要覆盖自己的工作分支。无 Git 阅读可用源码 ZIP；固定 checker CI 的 `verify_host` 必须用真实、无修改的历史 checkout，不能用解压目录冒充。

## 独立依赖 PR（2026-10-22 核查）

[草稿 PR #252](https://github.com/Code-Amadeus/Amadeus/pull/252) 已从 `main` 独立建立，head `80be1ebfc1dc03cdd466f7650baedbe6774de43a`，承接依赖补丁 `0c8150c9` + `59c379a6`。已核对 vendor/导入边界/来源测试相同及锁中的修复版本；它不包含商店或词典功能。较早快照中 CI 尚在运行；2026-10-22 再次只读核查同一 head，18 个检查均已 COMPLETED / SUCCESS。这是依赖 PR 的远端结果，不能据此关闭商店实验分支的远端 CI 门槛。

r2 包不改写，随附 `external-review-2026-10-22-pr252-addendum.zip` 提供本次 PR 快照与更新文档。独立 PR 的测试区分父审自行执行与协作提供的 CPU 模型证据，均没有被描述成 GPU 实机验收。

r3 ZIP 保持原字节与 SHA-256；新增 external-review-2026-10-22-r3-ci-addendum.zip 保存上述 GitHub 快照和更新记录，与 r3 一起交外审。本次只更新文档，不创建重复依赖 PR，也未合并或发布。

## 建议优先核查

1. **修复是否充分**：独立依赖提交 `0c8150c9`、显式导出收尾 `59c379a6` 与 Pi 夹具提交 `51c8363e`。复核标准库 MCP peer 是否保持原工具/权限/UTF-8/资源/关闭语义，以及旧时限和断言确实未放宽；检查真实 Notes/Pi 旅程仍执行。复核 Qwen-ASR 原始 sdist 哈希、推理/模型/CLI 实现字节一致、三个 initializer 可逆且字节码等价的重导出、demo 依赖 metadata patch、Pillow/certifi 锁与真实 223 包 local-cpu 导入资格。无 resolver override、Gradio 降级或新增审计忽略项。r2 的 `evidence/history/batch6/` 保留原失败，当前结果在 `evidence/remediation/`。
2. **安装权威**：`electron/src/main/extensions.ts`、`extensionCatalog.ts`、`extensionDownloads.ts`。renderer 只给 ID/版本，主进程从唯一官方目录选 URL/摘要；镜像只换产物地址；产物私有快照、确认前后/提交前复查；本地和商店入口按 wheel 本身依赖选择安装方式。
3. **安全状态**：`extensionSecurity.ts`、`desktopSettings.ts` 和相应测试。封禁与最大序号不依赖展示缓存；备份优先原子写入、NUL/双损坏后 fail closed、普通保存/刷新不解封、显式恢复不捏造安装记录；运行中进程包警告与真实启动回执。
4. **公开目录与未来能力**：协议 `catalog/REVIEW_CHECKLIST.md`、`policy.json`、`tools/catalog.py` / `check_catalog_ci.py`。审核针对 exact-wheel；v1 收录限制与运行契约分离；未知未来能力逐包禁用、不可丢弃已理解的安全信息；发布序号只增不减；Windows + 固定 Python/checker/最小锁；受保护 base 工具验证提案。
5. **发行/验收工具**：Host `electron/package.json` 的 checker 资源闭包、`tools/smoke_electron_model_less.py`、`tools/probes/extension_release_shell.py` / `extension_release_failures.py` / `extension_native_confirmation.py`、test-only fixture 和 CI。生产包没有测试 URL 或确认绕过开关；manifest 身份检查不应被误称签名或完整机器证明。

## 前批可复核证据（r2）

- 当前修复：Host 197 pass / 7 原有 skip；Pi 三个重点用例五轮独立批次 15/15；Pillow/相关路径 204 pass，另在真实 CPU 模型环境补验 61 pass；core 与 local-cpu 包一致性及 10 组 profile/platform 预演通过。
- 四类真实参考 wheel 作者旅程本轮 12 阶段通过。未改 Electron/协议实现，前批 Electron 576 项、Windows 固定 catalog 29 项、Docker 113 项（11 skip）作为明确历史证据保留。
- 源码和打包 shell 各 15 桌面 + 14 插件 + 18 故障检查通过；新建 profile、独立 Python/Node 依赖、实际后台/应用重启；双 settings NUL 故障由显式操作恢复。
- 第六批历史 native dialog：代理观察后点 Cancel / Install Confirm / Enable Confirm，4 项结果通过。不是自动 response stub，也不是独立人工验收。操作回执单列。
- 全部测试首次失败、未保留的 10 秒等待方案与夹具 UTF-8 修正过程均保留。`host-isolated-tests.log` 唯一失败来自在无 Git 源码 ZIP 中运行必须 `git ls-files` 的开发测试，不能被归类为产品启动失败；其余 194 通过。原工作树的两次 Pi 失败属于第六批历史；第七批同组回归已通过，且没有更改生产 3 秒等待。
- 第六批历史 Linux 容器断网构建 SDK 摘要与 Windows/现有 SDK pin 一致。Dockerfile 使用已记录镜像 digest 和协议哈希依赖清单；无特权、无个人目录/凭据、无 Docker socket 挂载。

## 复现

在独立目录按 README 安装 Python/Node 依赖，执行 `npm --prefix electron test`、插件相关 Python 测试，以及 `python -X utf8 tools/probes/extension_author_journey.py`。这会生成四类两版本 wheel。随后按 [客户端资格指南](../../extension_catalog.md#source-and-packaged-desktop-qualification) 生成带 `--failure-fixture` 的目录，分别跑源码/实际打包 exe 的 `--extension-failures`；原生确认使用另外一轮 `--native-confirmations`，记录真正操作员。

`SOURCE_MANIFEST.json` 的源码完整性检查会拒绝被改写的输入。若打包钩子改写 wallpaper lock，请记录差异并只从对应源码 ZIP 恢复该输入。不要在源码 ZIP 里运行依赖 Git 索引的源码发布工具测试；在 bundle 的真实 Git checkout 上运行。

Docker 只补协议/SDK 隔离：把 `protocol-source.zip` 解压后将包内 `docker/Dockerfile` 放到该源码根，`docker build -t amadeus-extension-review .`；`docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges amadeus-extension-review`。SDK 构建另挂载一个专用空输出目录，运行 `python tools/build_sdk.py --output-dir /evidence`。Windows catalog 资格按协议 workflow 使用固定历史 Host checkout，Linux 不替代它。

## 包内容与禁止误读

当前包名带 -r3，旧 r1/r2 ZIP 原样保留。r3 的 evidence/history/r2 保留前批结果，evidence/history/batch8 保留本轮探索失败。三份 patch（两份依赖、一份 Pi）可独立提取；依赖补丁交另一个会话独立审查并保持与词典 PR 分离。

`review-manifest.json` 和 `SHA256SUMS.txt` 标识精确提交、源归档及每份交付文件。应用二进制不放入轻量审查 ZIP，实际 exe/asar 摘要保留在结果和施工记录中。参考 wheel 是本轮测试字节，均未被独立审核；故意启动失败的 0.3.0 不混入参考目录。

目录公开发布仍要求 independent wheel review、素材授权、可达不可变 URL/pins、远程 Actions、main 保护、评审/2FA/发布权限及维护者决定。D0 的跨仓首页链接、D3 外部作者资格也未关闭。这里没有新建签名、自动更新、回滚承诺或新 Host 权限。
