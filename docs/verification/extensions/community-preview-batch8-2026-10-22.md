# 社区插件施工记录：第八批完整本地商店闭环

日期：2026-10-22（Pacific/Auckland）。实验分支 codex/extension-community-preview。
本轮按“完整功能原型”验收，不要求实际公开发布。未推送、未合并主线、未改变仓库保护或公开目录。

## 完成范围与实现归属

原缺口属于验收集成：维护端生成/校验工具与客户端真实安装旅程分别通过，但客户端之前消费的是手工构造的 fixture index。新增本地发布演练把它们接成同一条链，没有修改生产下载、安装、安全状态、权限或运行契约。

- tools/probes/extension_store_demo.py：独立 Git 克隆、真实参考 wheel 快照、仅 loopback 的 HTTPS 服务、正式目录 CLI 调用及命令/发布回执；--execute 明确开启执行。
- tools/probes/extension_store_roundtrip.py：实际 renderer/preload/IPC、手动更新、安装后原生 MCP 调用、进程重启与损坏恢复。
- electron/tests/fixtures/extensionPublication.cjs：仅在受控演示进程的私有 inspector 注入，将固定官方索引请求转向 loopback；不修改响应字节。wheel URL 本身就是本地 HTTPS，镜像按 /sha256/filename 提供相同字节。生产包没有新增测试 URL/信任开关。
- 复用原 TLS 证书生成与 model-less 桌面启动器；新模式与旧 fixture、失败矩阵、人工确认模式互斥。新增三项启动前参数边界测试。
- 新入口与文档加入既有源码归档必需清单/白名单。固定 checker、协议/SDK pin 和依赖锁不变。

维护端每阶段先提交合成收录/审核事实，使用前一发布版本的 check_catalog_ci.py 验证输入，再通过本地 demo-approved ref 模拟输入合入。正式 build_catalog.py 读取已提交输入，用 --check 重复核对同字节输出；check_catalog.py 验证转移，独立 publication commit 再通过 protected-base CI 重建检查。只有检查成功的原始 index.json 才被 HTTPS 服务提供。没有在客户端进程中拼接或修改目录 entries/security。

模拟身份明确叫 demo-author/demo-reviewer，证据 URL 为 loopback demo-only。它们验证流程和数据契约，不构成真实人工审核、GitHub 身份认证或分支保护证明。

## 六次发布与真实客户端结果

| 序号 | 操作 | 验收结果 |
| --- | --- | --- |
| 1 | Notes MCP、Theme 0.1.0 首次收录 | 实际浏览、下载、安装、启用；主题应用；安装后的 Notes 完成 save/read/list |
| 2 | 两包 0.2.0 | 已安装视图手动更新按钮，真实镜像 HTTPS，摘要一致；启用/Pi 绑定保留；新应用进程继续使用 |
| 3 | Notes 0.2.0 版权撤回 | 新安装被拒；不产生安全封禁；已安装 Notes 仍能读写 |
| 4 | 两份 0.2.0 exact artifact 安全封禁 | 安装/激活被拒，主题撤下；离线本地 wheel 入口也拒绝 |
| 5 | 显式高序号解封 | 两份安全事实保留并改为 allowed；主题重新可装；Notes 仍因版权撤回拒绝分发 |
| 6 | 单独恢复 Notes 分发 | Notes 重新安装/使用；重放真实序号 4 被拒；序号 6 与解封事实离线恢复后仍保留 |

封禁后删除目录展示缓存，停止应用，将 settings.json 填充 NUL，再离线启动，新进程从 .bak 恢复已知封禁。随后停止应用、同时损坏两份设置，新进程保持 safety unavailable；普通保存/刷新不能清除，必须显式 recover 才从同一正式生成目录恢复安全事实，并且不捏造丢失的安装清单。

解封和恢复分发后再次删除缓存、损坏主设置、离线重启，验证 allowed 决定和最高发布序号也能恢复，主题继续应用，Notes 仍能读写。总计四次完整应用重启，五个独立应用进程。故障是停机后注入 NUL，不是物理断电或所有文件系统崩溃时序证明。

另外，正式发布 CI 拒绝手改生成目录，正式转移校验拒绝删除已有安全事实。两份拒绝结果未替换服务端当前目录。

## 固定版本与结果

工具代码提交：c44799eaa12c983245c1551cfd6f901007997b05。
协议工具：153153598a9f94e3b44a477e99c3017f978d13e5；固定 Windows/CPython 3.12.10 checker：1840c8e4f646992ddaebea3139c8895f29743a8f。
目录副本 pin 仍为 58d3892d55c6a7ec6f8626153f503291bb10ab9a。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 源码版实际桌面 | 15 桌面 + 25 闭环检查通过；4 重启、5 Notes 合约、6 发布、10 次目录摘要匹配 | build/pub-src/ee8238356951/electron_model_less_20261021T115449Z_c14da0/ |
| 实际 Windows 打包版，干净 c44799e | 同样全部通过；console_errors/page_errors 均为空，正常关闭桌面和后端 | build/pub-exe/e01ed5ec853e/electron_model_less_20261021T120247Z_24c931/ |
| 桌面隔离/打包/目录回归 | 30 通过，无跳过 | build/publication-tests.log |
| 源码发行回归 | 15 通过，无跳过 | build/publication-release-tests.log |
| 原 fixture 准备入口 | 复用 TLS 后仍通过 | build/publication-legacy-prepare.log |
| 新增/受影响文件 Ruff、Node syntax、Git whitespace | 通过 | build/publication-lint.log 及本轮命令记录 |

源码轮约 6 分钟，打包轮约 4 分 34 秒；这是本机观测值，不是性能承诺。

源码探索轮在 7a6389cd 加未提交实现上运行，不能称为干净 c44799e 验收。提交前另将三处安装清单断言补强为明确包含两个插件，避免空列表使 all() 空真，并完成文档白名单/行尾收尾；实际 Windows 包轮在干净 c44799e 上完整执行这些最终断言。后续文档提交不是重新运行的代码版本。

本轮没有修改 production Electron 源码或 Python Host 运行代码。实际 exe SHA-256 为 e5528a9c00a5f1d8f592fd95b8f21c051ad9276d16ab7f42ca8fb2002081fafe，asar 为 cce87e3fbdd2d021737624d58f5e863ca5d6ddf59a2ec6b14ea944bbfb282109，与前批相同；本轮实际重新启动并验收该打包程序，使用当前显式指定的 Python 后端环境。

每轮六份生成目录通过维护端与客户端贯通，十次成功读取逐字节摘要相符。每轮五次 Notes 合约检查是安装后真实 native MCP client 的 save/read/list，不是模型驱动 Agent turn。没有复制个人配置或调用付费模型。

## 初次失败与修正

保留了四次未完成的桌面探索报告：
1. 深层临时克隆超过 Git for Windows 默认路径限制：仅对演练克隆设置 core.longpaths=true，不改全局 Git 配置。
2. 首次更新定位仍使用首次安装按钮名称：修正演练对已有版本的操作入口。
3. 直接 IPC 刷新没有更新 renderer 的局部目录状态：补齐用户实际“检查更新与安全状态”操作，再点击已安装列表更新按钮。
4. 镜像断言误写成 /mirror/filename，实际契约为 /mirror/sha256/filename：按现有契约校正服务端精确路径和断言；真实更新已经成功，未修改产品镜像规则。

首次归档回归 14 通过、1 失败，因为新文档仅加 required_files、未加 include_files；已加入白名单，原断言不变，15 项随后全部通过。原失败日志保留。新文件的 EOF 空行也已清理。没有新增测试重试、skip、超时放宽、审计忽略或自动回滚。

## 复现与交付

完整命令及各阶段解释见 [演示入口](../../extension_store_demo.md)。既可使用当前源码构建，也可加 --packaged-executable 指向实际 Windows exe；每次建立全新演示目录并在结束后关闭应用与 HTTPS 服务。可从外审包提供的四类参考 wheel 中直接选用本轮相同的 Notes/Theme 四份字节，也可重新跑作者旅程。

r3 外审包以当前源码和两个实验仓 Git bundle 为入口；另包含两轮六份原始目录、模拟输入 Git 历史 bundle、生成/校验命令与退出码、wheel 内容清单和差异、HTTP 请求摘要、MCP 合约 JSON、截图、脱敏结果及本轮失败历史。r2 的修复与更早证据作为历史保留，r1/r2 原 ZIP 不改写。排除 TLS 私钥、用户/演示设置、provider 文件、原始后台日志、venv/node_modules、临时工作树和应用二进制。

按本轮定义，完整本地功能原型已经贯通。独立真实产物审核、素材权利、公开 URL/pin、远端 Actions、真实仓库治理、干净 OS/独立人工资格和公开发布决定仍是另外的发布门槛；D0 公开链接与 D3 外部作者资格未因此关闭。公开 bootstrap 仍为空，未发布任何插件。
