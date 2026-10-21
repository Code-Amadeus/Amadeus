# 社区插件施工记录：第七批 Pi 稳定性与依赖审计修复

日期：2026-10-21 至 2026-10-22（Pacific/Auckland）。分支 `codex/extension-community-preview`。接续第六批外审准备；未推送或合并主线，未发布商店/索引。旧外审包及其失败证据保留，新材料使用 r2 名称。

本轮用户要求修复 Pi 集成失败与 certifi/Pillow 漏洞，不放宽测试、不新增忽略项。另一个词典会话获用户授权协调环境升级：依赖修复保持独立提交，由该会话独立审查/承接单独依赖 PR，本会话没有修改其工作树。

## 独立提交和范围

- 依赖修复：`0c8150c973c5dbf45c7d993ded8407a9c388c385`。
- 依赖 lint 收尾：`59c379a6e9615b1cc17ba90cbfc9a8f55508ea39`，三处 package initializer 的七个显式重导出，编译字节码/名字/常量与上游相同。
- Pi 测试夹具修复：`51c8363e7c2bc2d9198b93ade047bd10d5af7d79`。
- 后续仅追加施工/交接记录。SDK、协议来源 pin、目录 source pin、插件类型、生产权限和安装事务未变。

## Pi：修复测试准备条件，保留生产边界

第六批失败涉及合成 MCP 服务的首轮发现和权限撤销。该服务原先通过 `mcp.server.MCPServer` 启动，导入整套 Python MCP SDK 后才创建 ready 标记。3 次本机测量为 1.219–1.687 秒；这还不含 native Pi 的进程启动和工具发现，负载下可能错过 Host 明确限定的 3 秒首轮窗口。受控启动延迟实验复现了首轮没有工具、权限测试没有实际调用的现象。

曾尝试去掉 Host 的 3 秒覆盖、恢复 Pi 的 10 秒默认值；原有挂起服务测试立即失败，因为它要求模型请求在 7 秒测试上限内到达。**这项生产改动已完全撤回**，`agent_host/pi_runtime/mcp.ts` 和其契约文档与第六批相同。没有改测试超时、移除用例、跳过失败或自动重试。既有慢服务可以在后续模型轮次才提供工具的行为保留，没有承诺任意 SDK 服务都必须首轮就绪。

最终归类为测试仪器的准备成本与被测时限互相干扰。`tests/fixtures/pi_mcp_server.py` 改为只依赖标准库的合成 MCP JSONL peer，保持原来的工具名/参数/注解、凭据回显、文件效果、资源读取与 EOF 退出行为。Pi CLI、原生 MCP client/stdio transport、工具发现和真实权限 pipeline 仍完整执行；不是替换 Pi 或直接伪造权限结果。ready 标记的同机测量为 0.140–0.156 秒。Python SDK/真实包覆盖仍在单独作者旅程，包括安装后的 Notes 经真实 Pi 授权和 native transport 调用。

现有非 ASCII 长工具名测试还捕获了新夹具使用 Windows 默认 stdin 编码的错误；改为按 MCP 约定明确解码 UTF-8，未改该测试。两个原失败用例只补了失败诊断，原断言全部保留。5 轮独立稳定性验证每轮运行“绑定发现、撤销、挂起服务”三项，**15/15 通过**；每轮任一失败即停止，不是失败重试。

旧权限失败只有 `status=error`，没有完整底层错误，不能声称已经证明那个单次结果的每个细节。可复现的准备条件竞争已消除；同一整组回归和稳定性结果见下表。

## 依赖：真实可解的 Pillow 13 环境

`Pillow 12.3.0 → 13.0.0`；`certifi 2026.7.22 → 2026.10.19`。其它保留包版本没有变化。最小作者工具依赖导出逐字节不变。

直接升级被 Gradio `<13` 上限阻断，Qwen-ASR 0.0.6 又把 Gradio 作为必需依赖；只移除 Host 的 Gradio 下限会导致解析器退到旧 Gradio。这两种方案均未保留。

最终采用有完整来源记录的最小打包修改：从已校验 SHA-256 的官方 Qwen-ASR 0.0.6 sdist 保留全部推理/模型/CLI 实现文件，逐字节不改；将 pyproject 中 `gradio` 从必需依赖移到 `demo` extra。`uv.lock` 使用这个目录 source，保留上游版本以支持漏洞匹配。全仓 lint 随后检出上游三个 `__init__.py` 的七处隐式公共重导出；仅增加 `as 原名`，不改公开名字或 `__all__`。测试反向还原这些机械别名后必须匹配上游哈希，同时比较编译字节码、名字和常量，三者完全相同。全仓 Ruff 通过，没有增加 exclude/noqa。不是 resolver metadata override、跳过依赖安装或审计豁免。许可证、上游文件摘要和移除条件见 [Qwen-ASR provenance](../../../vendor/qwen-asr.PROVENANCE.md)。

Host local-model profiles 不再安装 Gradio 演示 UI；`tools.my_utils` 的 Gradio/pandas 导入移进原有 UI helper，`load_audio` 解码路径不变。Qwen 的 Gradio demo 与旧 `subfix_webui.py` 需要与 Host 分离的兼容演示环境，不建议向 Host 填回旧 Pillow/Gradio。ASR/TTS 的 in-process/sidecar 选择、模型代码、权重与 GPU 资格没有改变。

完整 CPU 模型环境实际安装 223 个包并通过依赖检查及 GSV/Qwen 导入，证明不是仅让 resolver 表面通过。额外 61 项 CPU 推理/音频路径测试无跳过。未下载模型权重、未运行新的 GPU 实机资格测试。

参考：[Pillow 13 官方发布说明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)、[certifi 修复版本](https://github.com/certifi/python-certifi/releases/tag/2026.10.19)。

## 结果与证据

仓库证据根：`build/blocker-fixes-20261021/`。独立环境/桌面证据根：同级 `qualification-20261021/`。对外副本已脱敏。

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 同一 Host 插件/Pi/发行组 + 2 项依赖边界新测试 | 197 通过、7 原有条件跳过 | `host-final.log` |
| Pi 原失败两项 + 原挂起服务约束，5 个独立新进程批次 | 15 通过，无失败重试 | `pi-stability-1.log` 至 `pi-stability-5.log` |
| 新核心环境依赖一致性 | 110 包兼容 | `environment-verify.log` |
| 10 组 locked profile/platform 安装预演 | 全部通过 | `profile-matrix.json` 与各 profile 日志 |
| 新 local-cpu + dev 完整环境 | 223 包兼容、`--profile t4-cpu` 通过 | `model-environment-verify.log` |
| Pillow 图像/相关路径 | 204 通过、2 项缺 Torch 跳过 | `image-inference-regression.log` |
| 在真实 CPU 模型依赖环境补验 | 61 通过，无跳过 | `model-inference-regression.log` |
| 最终显式导出补丁 | 2 项来源/构建测试、全仓 Ruff、刷新安装后真实 GSV/Qwen 导入通过 | `headless-final-tests.log`、`ruff-final.log`、`model-exports-final.log` |
| 最小作者依赖 export + lock freshness | 无差异，287 包锁解析通过 | `author-export.log`、`lock-check.log` |
| pip-audit（现有 CI 规则） | 无未豁免漏洞；certifi/Pillow 各 `vulns=[]` | `python-audit.json`、`python-audit.log` |
| 四类参考插件 0.1/0.2 作者旅程 | 12 阶段通过，内含 9 项 native provider/contract 回归 | `build/extension-author-journey/d151d209c26f/` |
| 更新后的源码商店/故障矩阵 | 15 桌面 + 14 插件 + 18 故障全部通过 | `source-fixed-smoke/electron_model_less_20261021T105222Z_62c9b8/` |
| 更新后的实际 Windows 包/故障矩阵 | 15 桌面 + 14 插件 + 18 故障全部通过 | `packaged-fixed-smoke/electron_model_less_20261021T105719Z_9c4e4a/` |
| 源码归档 | 4241 文件入选、140 排除、0 错误/警告 | `source-archive.log` |

七项 Host pytest skip 为五个显式 installer-backed 合约用例、POSIX executable 权限和 Windows symlink 权限；前五项另在完整作者旅程执行，不把 skip 文本改成 pass。图像首轮两个缺 Torch 的 skip 已通过真实 CPU 环境补跑。既有 `locale.getdefaultlocale`、上游音频 API 弃用等 warning 保留，未新增过滤器。

审计仍使用既有 `--ignore-vuln PYSEC-2026-1845`，工具显示原 pytest 的 2 条 ignored；本地 Amadeus 项目本身不在 PyPI 漏洞数据库审计范围；本轮没有新增或扩展豁免，不声称整个模型依赖集合无漏洞。GPU/模型栈的其它历史审计边界继续按安装文档处理。本轮用户指定的 certifi/Pillow 9 条记录全部消除。

MPS 预演第一次默认模拟 macOS 13，发现已锁 onnxruntime 只提供 macOS 14 wheel；用与现有 CI 一致的 `MACOSX_DEPLOYMENT_TARGET=14.0` 复核通过。首轮日志仍保留。其它预演包括 Windows core/voice/vad、CPU/CUDA/ROCm 与 Linux CPU/CUDA。预演不等于对应硬件验收。

全新安装最初使用 `--no-cache`，uv 在 freshness 检查时开始下载未选用的 ROCm URL 元数据/产物，因此停止该安装。最终先 `uv lock --check` 验证一致，再从同一锁使用 `uv sync --frozen` 安装独立环境；不修改锁或忽略约束，`uv pip check` 与 verifier 均通过。模型环境从锁安装，未复制主仓 venv 或使用 junction。

Qwen 上游原始文件保留既有行尾空白，完整 `git diff --check` 会报告这些原样文件；本地修改文件的 whitespace 检查和全仓 Ruff 通过。没有格式化上游文件使来源哈希失效，也没有加入 Git whitespace 忽略配置。第三方 provenance 检查零错误，8 条仓库原有 unmatched-component warning 保留。

## 固定产物与后续

源码归档 `source-fixed.zip`（代码 `51c8363e`）SHA-256：`451552d8b645bc6fa76b0204ac2152393b33f230dfa3489a02a759119c8807e1`。Windows exe 和 asar 的字节未因 Python 依赖修复改变，仍为 `e5528a9c00a5f1d8f592fd95b8f21c051ad9276d16ab7f42ca8fb2002081fafe`、`cce87e3fbdd2d021737624d58f5e863ca5d6ddf59a2ec6b14ea944bbfb282109`；重新打包的 Python checker 资源哈希与当前 source 在桌面报告单列。

桌面两轮使用干净 `51c8363e` 源归档和 T1 环境；最后的 `59c379a6` 仅修改不被这两轮 T1 加载的 Qwen 包显式导出与来源测试，已单独验证字节码等价、实际刷新安装后的 GSV/Qwen 导入和全仓 lint，没有冒称桌面报告的 commit 已自动变成最终提交。r2 同时保留实际桌面受测归档及最终源归档，哈希在清单中。

外审 r2 包更新为当前源归档、两个仓库 bundle、三份可单独提取的修复 patch（两份依赖、一份 Pi）、当前结果及原失败历史；原批次 native 代理操作回执和 Docker 证据作为历史保留，没有冒充本轮重新运行。全部参考 wheel 仍未独立审核。主分支保护、公开 pin/目录/产物、远端 CI、D0 公开链接、D3 作者资格等 [S3 门槛](store-v1.md) 没有因此关闭。


## 2026-10-22：独立依赖 PR 交接

协作会话已创建 [草稿 PR #252](https://github.com/Code-Amadeus/Amadeus/pull/252)，目标 `main`，分支 `codex/headless-inference-security`，核查时 head 为 `80be1ebfc1dc03cdd466f7650baedbe6774de43a`，状态 OPEN / draft。它承接本实验分支的 `0c8150c9` 与 `59c379a6`，不包含词典或商店功能，不重复创建依赖 PR。

本会话只读核查 GitHub PR 元数据及对应本地 Git 对象：Qwen vendor 树、来源说明、`tools/my_utils.py`、headless 依赖测试与实验分支补丁完全相同；Pillow 13.0.0、certifi 2026.10.19、Qwen-ASR 0.0.6 目录 source 和移除 Host Gradio 依赖的声明一致。协议 provenance 的实验目录上下文未带入该 main-based PR。

核查快照中 dependency-review 已通过，其余列出的 CI 作业仍在运行；不宣称该 PR 全绿，也不将依赖 PR 的 CI 当作商店实验分支的 CI。协作会话报告的 198 项独立回归等详见 PR；本会话没有把这些结果重复记作亲自重跑。主线未合并，资源未发布。

r2 外审 ZIP 保持原字节与 SHA-256；新增 `external-review-2026-10-22-pr252-addendum.zip` 保存本次交接快照和更新记录，应与 r2 一同提供给外审。这是文档补充，没有新的代码或测试结论。
