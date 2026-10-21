# 社区插件施工记录：第五批 S3 本地发行准备

日期：2026-10-21。实验分支 `codex/extension-community-preview`。
本批从 Host `e579f099` 与协议仓 `a5ca015` 继续；未合并、未推送、未发布。

发布资格以 [store-v1 检查表](store-v1.md) 为准；本记录不把自动化技术检查当作独立审核或完整 S3 结案。

## 发行缺口与修复

实际打包配置只复制了 SDK wheel，`ExtensionPackageHelper` 却在 packaged `resources/tools/extension_package.py` 查找校验器；该路径不存在。归类为发行资源集成缺口。现在将已有静态 owner、Skill 元数据 reader、schema/profile、目录固定副本与许可复制至 `resources/extension-host`，主进程的插件 helper/installer 使用这个独立根。SDK 仍用 `resources/extension-sdk`，运行时、能力类型和协议/SDK pin 不变。

不能把这些 `server` 文件直接放到 resources 根：正常后端仍由显式配置的 Python runtime 提供，会被同名 package 遮蔽。隔离单测从真正的打包过滤清单复制资源，在 `python -I` 下检查 profile、目录和 Skill，验证无需源码 checkout。生产代码没有加入测试 URL 或确认绕过入口。

## 实际桌面与 reader 检查

- 源码 shell：`build/s3-source-shell/electron_model_less_20261021T090109Z_dc3551/report.json`，15 项既有桌面检查 + 14 项插件检查通过。
- 实际 Windows 可执行文件：`build/s3-packaged-shell/electron_model_less_20261021T090301Z_f2628f/report.json`，同样 15 + 14 通过。两者使用正常认证后端、生产 renderer/preload/IPC，测试通过私有 inspector 提供合成目录 HTTPS 字节和自动原生确认。新建 settings、profile、安装目录，未复制个人配置或已安装环境。
- 主题 0.1.0 下载、启用、Appearance 选择，镜像更新 0.2.0 保持启用；离线刷新保留已安装；Tone 0.1.0 商店安装后以实际私有 Python 运行 Runner 合约，随后禁用/卸载。Notes MCP 0.1.0 通过实际界面保存 Pi 绑定，安装后的包经原生 MCP client 完成 save/read/list；这不包含模型驱动的原生 Agent turn。Tone 是合成 PCM，不冒称有用的语音服务；完整后台重启应用和最终发行异常矩阵仍未据此结案。
- `build/s3-reader-matrix/report.json`：从 Git blob 导出未修改 `f00623a811e001f37a76e4135c03dabe8fd84a17`（S1）和 `e579f099ca9a3583591323c386fec0a20bd850ff`（S2）reader。对同一新目录保留 8 个兼容版本与安全事实，未来类型/角色/contract/运行/交付逐包禁用；未知大版本和损坏安全结构整份拒绝。报告保存输入和 reader 文件摘要。
- 首次 shell 探针因只启用主题、未在 Appearance 选择而失败；这是验收脚本漏步骤，补齐真实选择后通过，没有修改产品语义。

## 首批候选与公开门槛

协议实验仓 `catalog/review-candidates/reference-wheels.json` 保存四类八份 wheel 的字节摘要、完整内容清单和候选版本差异。静态检查使用从固定 `1840c8e4f646992ddaebea3139c8895f29743a8f` 导出的原始 checker、最小依赖环境与 CPython 3.12.10；八份均通过 Host check-wheel 和固定 curated-v1 policy。不是完整 Git checkout 上的远端 CI/verify_host。审核状态全部 `not_reviewed`，不填写虚构 reviewer、归属或发布 URL，bootstrap/安全事实/包条目均未改动。

2026-10-21 只读核查实际 GitHub 配置：

```text
gh api repos/Code-Amadeus/Amadeus-Extensions/branches/main/protection
  HTTP 404: Branch not protected
gh api repos/Code-Amadeus/Amadeus-Extensions/rules/branches/main
  []
gh api repos/Code-Amadeus/Amadeus-Extensions/actions/permissions/workflow
  default_workflow_permissions=read; can_approve_pull_request_reviews=false
gh api orgs/Code-Amadeus
  two_factor_requirement_enabled=false
```

组织未强制 2FA 不等于个人未开启；个人状态未验证。未改仓库设置。公开发布仍需独立产物审核、真实 wheel 地址、公开可取的 pin、保护/评审/2FA/发布权限和远端 CI。D0 协议首页需在 Host 合入后更新；D3 外部作者仍未完成。清洁 Windows 机器、人工原生确认、最终发布构建上的完整异常矩阵仍是开放门槛。

## 自动化与复现

`python-windows.yml` 现在用 D2 构建的真实 wheel 生成独立 fixture，在源码与打包 smoke 两处加入 `--extension-fixture`。fixture 仅含一次性证书、测试索引和受检产物位置；不上传私钥。默认 smoke 不运行插件。固定 reader 使用 `tools/probes/extension_reader_matrix.py`；命令见 [客户端指南](../../extension_catalog.md#source-and-packaged-desktop-qualification)。

本地打包钩子会给 wallpaper 的 packages.lock.json 加一个空 win-x64 目标；本轮已还原这个无关生成差异，不把它作为插件修改提交。


## 最终代码、产物与回归

实现提交：`c1aa9f0`（打包资源与发行探针），`3ea67e4`（Notes 旅程），`ebcfd00`、`ee45e33`（真实界面选择和隔离 Pi 注册）。最终代码 commit 为 `ee45e337428de4ccd21d9f0252497a5c49a32270`；协议候选材料 commit 为 `e09f431`，目录 source pin 仍为 `58d3892`，manifest/SDK pin 均未改变。

两次最终 shell 运行时是 `ebcfd00` 加当时未提交的隔离 Pi fixture 改动，该改动随后原样提交为 `ee45e33`。不能把运行报告的 dirty 标记说成干净提交验收。报告同时记录可执行文件/asar/SDK 的实际摘要、24 个 checker 资源摘要、全部 8 个 fixture 产物与实际使用的 4 个产物。另核对打包资源与当前源码逐字节相同，asar 内 48 个编译文件与本轮 dist 相同。后续仅追加记录，不把记录提交冒称已重新构建的发布包。

| 产物 | SHA-256 |
| --- | --- |
| `build/s3-source-ee45e33.zip` | `e73a442d58e5f4e955bba744a7bb64f9184ce44f744b90875504d1ff609fe98f` |
| `electron/build/win-unpacked/amadeus-desktop.exe` | `e5528a9c00a5f1d8f592fd95b8f21c051ad9276d16ab7f42ca8fb2002081fafe` |
| `resources/app.asar` | `cce87e3fbdd2d021737624d58f5e863ca5d6ddf59a2ec6b14ea944bbfb282109` |
| SDK 0.1.0 wheel | `274bd66b81932c8ea50f39f58e7bf91530a8015196acfcf9bcfd77fa2d8b8084` |

源码归档来自干净 `ee45e33`：4216 文件入选、138 排除、0 错误、0 警告；解压至新目录后用最小依赖解释器 `-I` 校验八份 wheel 全通过，报告 `build/s3-source-final-static.json`。未从这个 ZIP 重新安装完整后端/Node 依赖，也未声称是干净 Windows 机器验收。source-release 的 `release_ready` 只表示该工具覆盖的源文件/许可/归档门槛，不是 S3 公开发布批准。

- Electron：`npm --prefix electron test`，**576 通过**，`build/s3-electron.log`。
- Python：插件、Pi/MCP/scoped approvals、来源、SDK、发行和 model-less scope 首轮 **193 通过、7 跳过、1 失败**。唯一失败是新 required 文档尚未进入 Git 的 `required-file-not-tracked`；纳入提交后重跑发行与 smoke 文件 **21 通过**，随后 smoke 配置修订再次 **6 通过**。没有修改或放宽断言；合计覆盖的通过用例为 194，跳过 7。日志 `build/s3-python.log`、`build/s3-release-tests.log`。
- 七个跳过：五个要求显式 installer-backed author journey 的合约用例（本轮 shell 仅补 Tone/Notes，四类完整 D2 证据仍引用第二批），一个 POSIX executable 权限、一个 Windows symlink 权限。未冒称这些 pytest 用例通过。
- 协议 catalog 测试 **18 通过、11 跳过**；11 项仍要求固定历史 Host 的完整 Git checkout。八份候选的固定 blob 静态校验补充证据不替代这些测试。
- 构建通过（仅原有 Vite 大 chunk 提示），Ruff、fixture Node syntax、Git whitespace 检查通过。没有远端 Actions 运行。

Notes 扩充探针的失败也保留在 `build/s3-source-shell`：先绕 UI 写绑定造成配置草稿仍为空；改成界面选择后发现无凭据隔离环境中 Pi 被 `pi_model_credentials_unavailable` 拒绝注册。最终按原生 Pi 的自定义模型格式写入仅指向 `127.0.0.1:1` 的合成条目，使用非真实 token，让本地 `auth check --no-refresh` 注册通过。没有模型请求、外部服务调用或付费验证；没有更改生产启动/权限规则。相关等待显式限时。

本轮完成 S3 的本地工程准备和可审查材料。S3 公共发布仍受 [store-v1](store-v1.md) 所列门槛约束；不创建或合并 PR，不发布目录，不调整仓库治理设置，不归档 worktree。
