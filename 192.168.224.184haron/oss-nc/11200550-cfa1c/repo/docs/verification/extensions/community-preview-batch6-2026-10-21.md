# 社区插件施工记录：第六批预合并审查与本机隔离验收

日期：2026-10-21。实验分支 `codex/extension-community-preview`。本轮从 Host `febff4b6`、协议仓 `1531535` 继续；未合并、未推送、未发布，也未改仓库保护规则。

本轮完成送外审前的本地审查、独立依赖安装、源码 ZIP 和 Windows 打包版商店验收；整组 Host 回归与 Python 依赖审计仍有阻断项，不建议合并或发布。用户没有干净 Windows VM，明确接受先完成本机隔离验收，并要求使用 Docker 补充验证。这不把本机隔离或 Linux 容器等同于干净 Windows、独立人工验收或公开 S3 发布资格。

## 审查范围与修订

审查安装身份与确认前后重检、renderer/主进程权威边界、官方索引与镜像下载、离线安装依赖、持久化封禁、设置原子写入与备份、显式损坏恢复、启动回执、旧 reader、新类型隔离、打包资源闭包和协议目录 CI。范围内未确认新的生产权限绕过或封禁持久化缺陷；这不是独立安全审计或无漏洞保证。

- 测试工具缺陷：源码 ZIP 没有 Git，原 smoke 无法建立代码身份。现在从 `SOURCE_MANIFEST.json` 验证全部列明输入的相对路径、大小和摘要，保留归档提交与 manifest 摘要；缺失、篡改、越界路径拒绝。不是签名认证，也不担保归档之外新增文件。依赖与实际 dist/exe/asar 仍另记哈希。对应 7 项 smoke 测试通过。
- 依赖缺陷：Vite 8.2.2 落在已披露 launch-editor 漏洞范围，最小补丁升级至 8.2.4。SDK、协议 pin 和目录源 pin 没有改动。最终 `npm audit --omit=dev` 为零；全依赖仍有 8 项中危计数，来自同一 `sprintf-js` advisory 的构建工具传播链，不是 8 个独立根漏洞。未采用审计建议的 electron-builder 大版本回退。
- 验收工具：添加 shipping failure matrix 和不自动应答的原生确认模式。CI 在源码和 Windows 包两处启用失败矩阵。探针处理 idle progress 的 `null`；没有为探针修改产品规则。

参考：[Vite/launch-editor 高危项](https://github.com/advisories/GHSA-587h-7wmj-h5pm)、[相关项一](https://github.com/advisories/GHSA-698v-4r3x-3vcv)、[相关项二](https://github.com/advisories/GHSA-r825-w8mm-5pxw)、[剩余 sprintf-js 项](https://github.com/advisories/GHSA-hp3w-g68c-fv3c)。后者当前无已修复版本；其实际使用在构建工具链，不能据生产依赖审计为零声称整条工具链无风险。

## 隔离边界与代码身份

新建独立资格目录，源码通过发布工具生成 ZIP 后解压，目录没有 `.git` 或 `.env`。新建 Python venv，用 `uv sync --locked --extra dev --no-cache --link-mode copy` 安装；新 npm 缓存与独立 `npm ci` 安装 Electron/Pi 依赖。没有目录联接、没有复制个人配置、凭据或已安装插件。主仓日常环境没有修改。

Windows 11 Home 10.0.26200 x64、CPython 3.12.10、Node 22.21.1、npm 10.9.4。Python、Node、.NET 基础可执行程序和操作系统仍来自本机，所以不是干净 OS。测试 profile 为新建的合成旧版设置，仅含英语 locale 和空安装清单；Pi 只配置关闭的 loopback 模型地址与合成 token，未调用外部模型/付费服务。

源码依次更新到干净提交生成的归档；只覆盖归档输入文件，独立依赖保留在资格目录。打包钩子添加的 wallpaper 空 `win-x64` lock 目标由自己的归档原字节还原。源码 smoke 使用 `f5ea6d45`；打包与原生窗口 smoke 使用 `457b1fe7` 的完整 manifest。两者之间只新增原生确认探针及入口，不改变打包应用代码。报告均记录 `identity_kind=source-release-manifest`、`dirty=false`、实际 frontend 摘要；后续留档提交不是重新构建的应用。

## 验收结果

证据根为本地资格目录 `qualification-20261021/`；对外压缩包内提供脱敏 JSON、测试结果和哈希索引。

| 检查 | 结果 | 证据相对路径 |
| --- | --- | --- |
| 新 Python 环境闭包 | 110 个安装包兼容 | `python-verify.log` |
| Electron | 576 通过 | `electron-tests.log` |
| Host 选定回归 | 194 通过、7 跳过、1 失败（Pi scoped approval） | `host-final-tests.log` |
| 独立串行 Pi/MCP + scoped approvals 复核 | 30 通过；不消除整组偶发失败 | `pi-final-serial.log` |
| 固定历史 Host 完整 Git checkout + 最小校验器 | catalog 29 通过，无跳过 | `catalog-tests.log` |
| 四类作者旅程 | 四类各 0.1.0/0.2.0、12 阶段全部通过 | `source/.../build/extension-author-journey/27c906e22fd4/report.json` |
| 源码 ZIP 实际 shell | 15 桌面 + 14 插件 + 18 故障检查通过 | `source-smoke/electron_model_less_20261021T094813Z_fd58d7/` |
| 实际 Windows exe | 同样 15 + 14 + 18 通过 | `packaged-smoke/electron_model_less_20261021T095401Z_2aafcc/` |
| 实际 Windows 原生确认 | 15 桌面 + 4 原生确认结果通过 | `native-confirmation/electron_model_less_20261021T095844Z_28c7a1/` |
| Linux Docker 断网协议回归 | 113 通过、11 跳过 | `docker-tests.log` |
| Linux SDK 构建 | 与 Windows/已有 pin 字节摘要相同 | `docker-sdk.log` |
| Ruff / Node fixture syntax / Git whitespace | 通过 | 本轮命令记录 |

所有三次实际 shell 报告 `console_errors=[]`、`page_errors=[]`，正常退出前验证 Electron/后端关闭。源与打包版都实际运行下载、镜像、安装、手动更新、Theme 外观、Tone Runner 和 Notes MCP 原生 client 的 save/read/list。D2 使用受控模型做原生 Pi Skill 旅程；不把 Notes 的直接 MCP client 测试说成模型驱动 Agent turn。

原生窗口使用 `--native-confirmations`，fixture 保留真正的 `dialog.showMessageBox`：Codex 通过 Windows computer-use 观察并点击一次 Cancel、一次安装 Confirm、一次 Enable Confirm，检查未安装、默认禁用、启用和主题生效四种结果。**操作员是代理，不是用户或独立人工审核者**。窗口截图在任务工具记录中，外审包提供独立操作回执与自动结果，不伪造截图文件。

## 故障矩阵与边界

源与打包版各 18 项：进程重启处理 pending removal；取消流式下载不安装；摘要错误的更新保留旧版与已应用主题；版权撤回阻止分发但不写安全封禁；默认卸载保留数据、明确要求才删除；真实后台重启激活插件；新版本启动失败可见、保留新版且不自动回滚；封禁立即撤下主题；删除目录展示缓存后损坏主 settings、离线重启从 `.bak` 恢复封禁；同一产物走本地入口仍被拒；低发布序号不能抹掉封禁；两份 settings 均损坏后新进程保持 unavailable；普通设置保存与普通刷新均不能清除故障；显式官方恢复恢复安全状态但不捏造丢失安装清单；恢复后仍拒绝被封禁产物；失败事务没有残留下载 staging。

每轮共三次新应用进程，另有真正后台 restart。NUL 在测试应用停止后注入，**没有做物理断电，也没有声称覆盖所有文件系统崩溃时序**。目录与 TLS 是一次性 fixture；没有公共审核目录。故意抛出异常的 Tone 0.3.0 只用于启动拒绝测试，不进入候选目录。

## 保留的失败与审计限制

1. 首次工作树回归 `194 passed, 7 skipped, 1 failed`：`test_only_host_bound_servers_load_despite_ambient_configuration` 没有发现 fixture MCP tool。原位置单项复核通过，独立源码目录完整 Pi/MCP 22 项通过；后续完整回归见上表。没有修改测试、增加重试或放宽超时。第二次工作树整组回归又在 `test_revocation_requires_fresh_approval_on_next_identical_call` 得到 `status=error`（仍为 194/7/1）；最后在无桌面探针并行的情况下单独执行两组 Pi 测试，30 项通过（171 秒），但没有一次完整工作树运行全绿，不能据此证明并行负载就是原因。根因未确定，交外审保留两次失败，不能写成已修复。
2. 在无 Git 的源码目录照搬包含 Git 发布工具的开发测试，得到 `194 passed, 7 skipped, 1 failed`。唯一失败是 `test_current_source_policy_has_no_selected_provenance_blockers` 调用 `git ls-files`；这是运行位置不满足该开发测试前置条件。Git 发行工具在工作树检查，真正源码 ZIP 的运行/篡改检查由 manifest 路径覆盖；不给源码伪造 `.git` 或放宽断言。
3. 七个跳过为五个要求显式 installer-backed journey 的合约用例、一个 POSIX executable 权限和一个 Windows symlink 权限。五项由另行完整 D2 旅程覆盖，但不把这次 pytest 的 skip 改写成 pass。
4. Docker 的 11 个 skip 要求固定 Host checkout；在 Linux 没有冒充 Windows 目标。相应 Windows catalog 29 项全部执行。Docker 使用 `python:3.12.10-slim@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db`，构建时以哈希清单安装依赖，测试/SDK 构建 `--network none --cap-drop ALL --security-opt no-new-privileges`。未挂载 Docker socket、个人目录或凭据。
5. Docker Desktop 本机启动因旧 AF_UNIX socket 不可访问而失败；停止后仅将正常 `run` 目录改名隔离，重建空目录后 Engine 29.8.2/Linux 正常启动。未恢复出厂、未删除镜像/容器/卷，隔离目录保留。此环境修复不是插件产品修订。
6. Python 审计没有通过：依照现有 CI 的 `--ignore-vuln PYSEC-2026-1845` 执行后，仍发现 certifi 2026.7.22 的 1 条、Pillow 12.3.0 的 8 条已知漏洞；既有 pytest 9 升级前豁免继续存在（审计提示 ignored 2）。本地 Amadeus 自身不是 PyPI 包，审计跳过。精简 advisory IDs 和修复版本在外审包 `python-audit-summary.json`。已对照 `fd17e707` 确认这两个版本就在原 Host 基线中，不是商店引入的升级。它们仍是发布阻断项；本轮不扩大成 Pillow 主版本迁移或重新安排整个图像链路验收，也没有加忽略项掩盖失败。参见 [certifi 官方公告](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) 与 [Pillow 13.0.0 发布说明](https://github.com/python-pillow/Pillow/releases/tag/13.0.0)。

## 固定产物

| 产物 | SHA-256 |
| --- | --- |
| `premerge-qualified.zip`（f5ea6d45，源码实际验收） | `c0da8d58994b294691597938ee2f18dfddae1b88bc7ba506edbcd223a9c9dd19` |
| `premerge-native.zip`（457b1fe7，含原生探针） | `2f499091e051ddb05fd40286a52f653b815a6b1ec4aaffbac5af1eaa0df9542f` |
| `amadeus-desktop.exe` | `e5528a9c00a5f1d8f592fd95b8f21c051ad9276d16ab7f42ca8fb2002081fafe` |
| `app.asar` | `cce87e3fbdd2d021737624d58f5e863ca5d6ddf59a2ec6b14ea944bbfb282109` |
| SDK 0.1.0 wheel（包括本轮 Docker 构建） | `274bd66b81932c8ea50f39f58e7bf91530a8015196acfcf9bcfd77fa2d8b8084` |

代码提交：`b3a18270`、`f5ea6d45`、`457b1fe7`。协议仓保持 `153153598a9f94e3b44a477e99c3017f978d13e5`；目录源 pin 为 `58d3892d55c6a7ec6f8626153f503291bb10ab9a`，固定 Host checker 为 `1840c8e4f646992ddaebea3139c8895f29743a8f`。完整八份新构建参考 wheel 身份、被使用的产物、checker/SDK/exe/asar 哈希在 shell JSON。参考 wheel 全部仍未经独立审核；新构建字节与旧候选不同也不覆盖旧版审核记录。

## 交外审与后续门槛

外审入口见 [审查说明](external-review-handoff-2026-10-21.md)。本地压缩包包含经过发布政策检查的源码归档、Host/协议增量 Git bundle、脱敏结果、参考 wheel 和 SHA256 清单，不含用户 profile、原始 backend 日志、TLS 私钥、凭据或运行环境。仅在本地生成，由用户决定交付对象。

[store-v1 检查表](store-v1.md) 继续保留：独立 exact-wheel 审核与素材权利、公开不可变下载地址、公开可取 pin、远端 Actions、main 保护/非作者评审/2FA/最小发布权限、最终已审核目录重跑、维护者发布决定。D0 协议首页必须等 Host 合入后指向不可变作者入口；D3 两位外部作者的资格仍未完成。没有因为本轮本地测试通过就开启普通用户安装或宣告 S3 完成。
