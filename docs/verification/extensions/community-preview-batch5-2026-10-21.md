# 社区插件施工记录：第五批 S3 本地发行准备

日期：2026-10-21。实验分支 `codex/extension-community-preview`。
本批从 Host `e579f099` 与协议仓 `a5ca015` 继续；未合并、未推送、未发布。

发布资格以 [store-v1 检查表](store-v1.md) 为准；本记录不把自动化技术检查当作独立审核或完整 S3 结案。

## 发行缺口与修复

实际打包配置只复制了 SDK wheel，`ExtensionPackageHelper` 却在 packaged `resources/tools/extension_package.py` 查找校验器；该路径不存在。归类为发行资源集成缺口。现在将已有静态 owner、Skill 元数据 reader、schema/profile、目录固定副本与许可复制至 `resources/extension-host`，主进程的插件 helper/installer 使用这个独立根。SDK 仍用 `resources/extension-sdk`，运行时、能力类型和协议/SDK pin 不变。

不能把这些 `server` 文件直接放到 resources 根：正常后端仍由显式配置的 Python runtime 提供，会被同名 package 遮蔽。隔离单测从真正的打包过滤清单复制资源，在 `python -I` 下检查 profile、目录和 Skill，验证无需源码 checkout。生产代码没有加入测试 URL 或确认绕过入口。

## 实际桌面与 reader 检查

- 源码 shell：`build/s3-source-shell/electron_model_less_20261021T083534Z_45d04b/report.json`，15 项既有桌面检查 + 12 项插件检查通过。
- 实际 Windows 可执行文件：`build/s3-packaged-shell/electron_model_less_20261021T083642Z_0d97a8/report.json`，同样 15 + 12 通过。两者使用正常认证后端、生产 renderer/preload/IPC，测试通过私有 inspector 提供合成目录 HTTPS 字节和自动原生确认。新建 settings、profile、安装目录，未复制个人配置或已安装环境。
- 主题 0.1.0 下载、启用、Appearance 选择，镜像更新 0.2.0 保持启用；离线刷新保留已安装；Tone 0.1.0 商店安装后以实际私有 Python 运行 Runner 合约，随后禁用/卸载。Tone 是合成 PCM，不冒称有用的语音服务；完整后台 TTS 路由和有用进程包发行验收仍未据此结案。
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

