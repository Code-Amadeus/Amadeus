# 社区插件开发预览第一批施工记录

日期：2026-10-21。状态：本地实验，未推送、未发布。

Host 实验分支为 `codex/extension-community-preview`，基线为主线
`fd17e707d8793480003f929bc8c78a8d134e41b5`，包含 #234。独立协议仓同名分支从
`bf1a848448a19ed20eca029e8cc3991563e42e95` 开始。日常工作区保持原分支和已有修改。

## 本批交付

- D0：新增统一作者入口；主仓中英文 README、贡献说明和 ROADMAP 链接到当前路径。
  Host profile 保留当前契约，历史验收移到独立文件并保留旧标题锚点。协议仓首页修正了
  “未合并实验”的过期表述；没有改变规范、SDK 或其来源 pin。
- D1：已有静态 CLI 新增 `profile`、`check-package`、`check-wheel` 和显式文本输出。
  默认 Electron JSON helper 结果保持兼容。wheel 检查在临时快照中复用生产
  `validate_installed` 检查资源和 Skill 元数据，不导入插件、不执行 pip。
- 最小作者依赖清单锁定 jsonschema、packaging、PyYAML 及其传递依赖；独立环境只安装
  这组依赖即可检查包。增加 Windows CI 最小环境步骤和源码发行包含规则。
- 六个已有示例均重新构建并通过最小环境检查。没有增加新的生产插件类型、Host 权限
  或商店收录限制。

## 验证结果

执行环境为 Windows、CPython 3.12.10。Node 依赖复用现有锁定安装目录，构建输出属于
实验工作区；这些结果不是一次全新安装环境的完整发布资格。

| 检查 | 结果与范围 |
| --- | --- |
| 最小作者环境 | 由新 venv 和哈希依赖清单安装；profile、源码 Skill 和六个 wheel 静态检查通过 |
| Python 作用域回归 | 162 通过，2 跳过；含作者工具、八组插件回归、Pi MCP、精确调用授权与 Provider 自动授权回归 |
| 跳过项 | Windows 上的 POSIX 执行位契约，以及不可用的符号链接权限 |
| Electron 作用域回归 | extensions、extensionSettings、extensionTheme 共 89 项通过 |
| 生产桌面构建 | 配置生成、SDK 摘要、Pi TypeScript、Electron TypeScript 与 Vite 均通过；保留现有大 chunk 提示 |
| 静态代码检查 | 修改的 Python 工具与回归通过 ruff |
| 真实主题生命周期探针 | 资源主题和混合主题/TTS 的安装、更新、取消、启停、数据保留/删除通过；使用真实安装器和 SDK Runner，合成 PCM |
| 实际 Electron 无模型 GUI | 15 项通过，包含后端鉴权、设置中的插件入口、原生主题恢复及完整退出 |
| 实际安装的 Notes MCP | 原生 MCP 传输发现 3 个工具，save_note/read_note/list_notes 的合成笔记往返通过，连接关闭 |
| 源码发行规则 | 新作者工具、依赖锁与文档进入选择范围；发行/SDK 回归其余 27 项通过，required-file 检查在新增文件纳入 Git 后复验通过 |

本批 Python 命令在该实验主线上运行：

```powershell
python -m pytest -q --tb=short -rs tests/test_extension_authoring.py tests/test_extensions.py tests/test_extension_resources.py tests/test_extension_mcp.py tests/test_extension_skills.py tests/test_extension_examples.py tests/test_extension_fish_audio.py tests/test_extension_protocol_provenance.py tests/test_extension_sdk_build.py tests/test_pi_mcp.py tests/test_pi_scoped_approvals.py tests/test_provider_automatic_permissions.py
```

GUI 与生命周期复用现有入口：

```powershell
python tools/probes/extension_theme_lifecycle.py --output-dir build/extension-preview-verification/theme-lifecycle
python tools/smoke_electron_model_less.py --report-dir build/extension-preview-verification/model-less --startup-timeout 120
```

本地原始报告保存在 `build/extension-preview-verification/`，未作为公共日志上传。
主题探针中的 `nativeDialogs` 为 false，安装确认由驱动代答。独立窗口的包准备也使用
生产安装器加驱动确认，并非手工原生文件选择/确认全过程。Notes MCP 的验证是直接原生
传输调用，不是由真实 LLM 选择工具的验收。

## 独立实验窗口

实验配置位于 `build/extension-preview-verification/interactive-lab/`，安装并启用了：

- `com.example.desktop-theme`，选择 Quiet day；
- `com.example.notes-mcp`；
- `com.example.handoff-skill`；
- `com.example.csv-audit-skill`。

MCP 和 Skill 绑定 Pi。窗口没有复制个人凭据，没有配置远程模型，Pi 在该无凭据配置中
显示不可用属于预期，不代表已经取得真实模型使用 Skill 的资格。Fish Audio 与 MiMo
本批完成静态构建检查，没有调用付费服务。TTS 合成由独立生命周期探针中的本地测试包验证。

启动前确认 17777 空闲，并拒绝覆盖已有后端。实验窗口仍使用生产固定端口 17777；
它的设置、会话、账本、缓存与数据独立，但不能与另一实例的后端同时占用该端口。
关闭实验窗口应走正常退出；先关闭它再启动日常实例。

Computer Use 已观察到实际 Chat 窗口；原生控件几何捕获不稳定，因此没有把后续点击尝试
计作通过，也没有用其他应用的画面作为证据。完整 GUI 结果来自上面的现有生产应用 smoke。
实验准备脚本曾因顶层等待和错误的启动枚举值失败，修复的是一次性驱动；正式记录仅采用
修复后成功的准备和调用结果，没有修改生产启动逻辑来适应探针。

## 尚未交付

- D2 统一作者合约检查 CLI 尚未实现；本批只复用现有进程与 GUI 探针。
- D3 外部作者验证未开始，不把维护者示例当作社区作者证据。
- S0 目录规范、公开审核清单和发布流水线尚未实现。
- S1/S2 的远程下载、镜像、持久安全事实与商店界面尚未实现，当前窗口仍是本地插件管理器。
- 没有发布公开索引、SDK、软件版本或社区消息；没有合并到主线。

下一批从已可用的 D1 入口出发，按批准计划并行推进 D2 和 S0，继续保留首版收录政策与
Host 运行契约的边界。阶段退出证据以新的精确提交和验收记录更新。
