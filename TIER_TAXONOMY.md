# 分层与构建术语契约（Tier & Build Taxonomy）

本文是 Amadeus 中"装哪一层""用哪套轮子""用什么实现""在哪台机器上能跑"这四件事的
**唯一措辞来源**。任何文档、注释、Issue、PR 描述涉及这些概念时，用词以本文为准。

本文的物理事实取自 `pyproject.toml`、`uv.lock`、`tools/verify_python_environment.py`、
`tests/test_core_install_tiers.py` 与 `README.md`；每条结论都标了出处，没有出处的不写。

## 为什么要统一

迁移前，仓库里同一批概念曾有三种说法，且没有一份定义（下表与引文均为迁移前快照，现已统一）：

| 说法 | 出现在 | 覆盖范围 |
|---|---|---|
| `L1–L4` | `README.md`、`README_ZH.md`、`docs/`（约 96 处） | core / voice / CPU VAD / local-cu124 |
| `T1/T2/T2a/T2b` | `pyproject.toml`、`asr/`、`tts/`、`server/`、`tests/`、CI 注释（约 20 处） | 只有 T1/T2a/T2b，没有 T3/T4 |
| profile 名 | `tools/verify_python_environment.py` 的 `--profile` | `cpu/voice/vad/vad-cpu/ci/cu124/cu128/mps/rocm` |

而且同一句话里混着用（迁移前原文）：

- `asr/manager.py`：torch is a local-model tier **(T2b)** dependency. Remote ASR installs **(L2)** have no torch
- `server/app.py`：It is a **T2b (local-cu124)** dependency: **L1** installs skip…
- `.github/workflows/python-macos.yml`：macOS product path is **T1 core + T2a remote voice**: install **L1+L2**, verify…

只有 T1 是**带测试的契约**（`tests/test_core_install_tiers.py` 屏蔽 torch/pyaudio/
onnxruntime 等后断言 `server.app` 仍可导入）；`L1–L4` 只是文档说法。统一的目标不是
"改好看"，而是让说法跟着契约走。

## 四个命名空间

| 命名空间 | 取值 | 定义 | 关系 |
|---|---|---|---|
| **Tier** | `T1 core` / `T2 voice` / `T3 vad` / `T4 embedded` | **推荐的安装套餐**；依赖集逐级严格包含，由 extra 声明保证 | 累积，一层比一层多 |
| **Build** | `cpu` / `cu124` / `cu128` / `mps` / `rocm` | 只决定 Torch / Torchaudio 的**锁定来源** | 互斥；只在 T3 及以上需要 |
| **Implementation** | `remote` / `embedded` | 同一能力由远端 API 还是本地模型提供 | 同一能力可并存，配置上可混搭 |
| **Platform** | 见「平台推荐」 | 只做推荐；能否安装只取决于锁文件里有没有对应轮子 | 不进 tier 名 |

`remote` / `embedded` 这两个词沿用产品既有说法（Settings 里就是 "Embedded GPT-SoVITS
model"，`.env.example` 里就是 `tts_remote`）。

### Tier 定义

| Tier | 能力 | 依赖增量 | 装机命令 | 验证 profile |
|---|---|---|---|---|
| `T1 core` | 文字对话、Work、Provider、角色渲染 | 基础依赖，**torch-free** | `uv sync --locked` | `cpu`、`ci` |
| `T2 voice` | 远端语音 I/O：播放、麦克风、回声消除、远端 TTS / ASR | `--extra voice`，**仍 torch-free** | `uv sync --locked --extra voice` | `voice` |
| `T3 vad` | 角色说话时的实时打断 | `--extra vad`（含 `voice`；**由此传递引入 torch**）+ build `cpu` | `uv sync --locked --extra vad --extra torch-cpu` | `vad`、`vad-cpu` |
| `T4 embedded` | 本地 TTS、本地 ASR、唤醒词 | `local-models` 依赖集（含 `vad`）+ build | `uv sync --locked --extra local-<build>` | `t4-cpu`、`mps`、`cu124`、`cu128`、`rocm` |

同时写上 `voice`、`vad` 的长命令仍然有效，选出的环境相同。

`T4` 的装机命令用打包形式 `local-<build>`（= build + `local-models`）；`README.md` 明确要求
「选文档化的 build profile，不要单独选共享的 `local-models` 能力 extra」，所以不要把这一行拆开写成
`--extra local-models --extra <build>`。

tier 名与 extra 名对齐，方便机械对照：`T2 ↔ voice`、`T3 ↔ vad`、`T4 ↔ local-models`、
`T1 ↔`（无 extra）。

### 四条不变式

1. `deps(T1) ⊂ deps(T2) ⊂ deps(T3) ⊂ deps(T4)`，**由 extra 自身声明**：`vad` 包含 `voice`，
   `local-models` 包含 `vad`，每个 `local-<build>` 包含 `local-models`。
   `tests/test_profile_contract.py::test_tier_extras_are_declared_as_a_strict_ladder` 校验声明，
   `test_t4_build_alone_installs_the_whole_ladder` 校验锁文件；`T1` 另由
   `tests/test_core_install_tiers.py` 兜底。
2. **`T1`/`T2` 必须 torch-free**。`pyproject.toml` 的 voice extra 注释明确写了
   "Torch-free on purpose: T2 stays a few hundred MB"。torch 只允许从 `T3` 起出现。
3. **装了 torch 就恰好是一个已锁定的 build**。build extra 两两互斥由 uv `conflicts` 保证；
   验证器的 `vad` profile 会识别实际 build，torch 不来自任何 build extra 时直接失败。
4. **平台不设上限**。平台表只做推荐；某组合装不上只能是因为锁文件里没有对应轮子（事实），
   不能是政策。

### 两条容易搞错的事实

**torch 是被 `vad` 拖进来的，不是"CPU VAD"需要它。**
`uv.lock` 中 `silero-vad 6.2.1` 的依赖里直接有 `torch` 与 `torchaudio`；而 `voice`
extra 里没有 torch。所以"`T3` 起必须选 build"的原因与 VAD 是否用 CPU 无关，只与
`silero-vad` 的依赖有关。（`vad` extra 自身只声明 `amadeus[voice]` 与 `silero-vad==6.2.1`，torch 不是它直接声明的。）

**在 macOS 上，两个 build 名字拿到的是同一个轮子。**
`uv.lock` 中 torch `2.7.0` 的 macOS 轮子只有 `torch-2.7.0-cp312-none-macosx_11_0_arm64.whl`；
在 darwin 上 `torch-cpu` 与 `local-mps` 都从 `pypi.org/simple` 解析，**差别只在
`local-mps` 额外带上 `local-models` 依赖集**。因此：

- `--extra torch-cpu` 在 macOS 上**不是** CPU-only 构建，它拿到的轮子带 MPS 支持；
- 该轮子只有 arm64，**Intel Mac 拿不到 torch 2.7.0，`T3` 及以上装不上**（缺轮子的事实，不是政策上限）。

## 平台推荐（平台只在这里出现）

| 平台 | 推荐 | 也可安装 | 备注 |
|---|---|---|---|
| Windows + NVIDIA | `T4` + `cu124`（参考） | `cu128`（实验）、`local-cpu` | 唯一有完整本地栈验收记录的平台 |
| Windows + AMD（官方支持矩阵内的卡） | `T4` + `rocm`（实验） | `local-cpu` | |
| Linux x86_64 + NVIDIA | `T4` + `cu128`（实验） | `local-cpu` | 源码部署已真机验证，GPU 模型无验收记录 |
| macOS Apple Silicon | `T4` + `mps`（实验） | `local-cpu`（同轮子） | 本地 TTS 走 MPS（实机证据基于 torch 2.6.0）；本地 ASR 落 CPU；唤醒词 CPU |
| 无 GPU 的 Windows / Linux | `T2` 或 `T3` | `T4` + `local-cpu` | 唤醒词可用；本地 TTS/ASR 无实时性承诺；需设 `TTS_DEVICE=cpu` |
| macOS Intel | `T2` | — | `T3` 起缺 torch 2.7.0 x86_64 轮子，装不上 |

本表只做推荐。装成功不等于验收通过（模型推理、音频设备、连续播放、打断仍需人工验证）。

## 能力矩阵（与 tier 解耦）

| 能力 | 最低 Tier | 可用实现 |
|---|---|---|
| 文字对话 / Work / 渲染 | `T1` | `remote` |
| 说话（TTS） | `T2` | `remote`（Fish Audio / MiMo / OpenAI 兼容）、`embedded`（GPT-SoVITS，需 `T4`） |
| 听（ASR） | `T2` | `remote`、`embedded`（Qwen3，需 `T4`；macOS 上只能 CPU） |
| 打断 | `T3` | `embedded`（Silero VAD，恒 CPU） |
| 唤醒词 | `T4` | `embedded`（恒 CPU，`asr/wake_service.py` 固定 `backend.load("cpu")`） |

能力可以混搭实现，例如"远端 TTS + 本地 ASR"= `T4` 的安装 × 两种实现的配置。层级是
**安装阶梯**，回答"装哪些包"；它不回答"用哪个实现"。

## 旧词到新词的映射

| 旧写法 | 新写法 | 说明 |
|---|---|---|
| `L1 core` | `T1 core` | 概念相同，只统一字母 |
| `L2 voice` | `T2 voice` | |
| `L3 CPU VAD` | `T3 vad` | 去掉"CPU"——build 是独立轴，VAD 本身恒 CPU |
| `L4 local-cu124` | `T4 embedded`（+ build `cu124`） | 层级名里不再出现平台与轮子名 |
| `T2a remote voice` | `T2` + 实现 `remote` | `T2a/T2b` 不是层级，是实现 |
| `T2b local model` | `T4` + 实现 `embedded` | |
| `torch-cpu` / `local-cu124` / `local-cu128` / `local-mps` / `local-rocm` | 都是 **build 选择**；后四者 = build + `local-models` 打包 | **不改名**（见下） |
| verify 的 `--profile <name>` | **(tier, build) 组合的具名别名** | 保留现值，见下表 |

验证 profile 对照：

| profile | 含义 |
|---|---|
| `cpu` / `ci` | `T1`（两者检查内容相同，`ci` 是 CI 使用的别名） |
| `voice` | `T2` |
| `vad` | `T3`（自动识别已装的 build；torch 不来自任何 build extra 时失败） |
| `vad-cpu` | `T3` + build `cpu` |
| `mps` | `T4` + build `mps`（可加 `--require-mps-device`） |
| `cu124` / `cu128` / `rocm` | `T4` + 对应 build（`cu124`、`rocm` 为 Windows-only profile） |
| `t4-cpu` | `T4` + build `cpu`（extra `local-cpu`） |

新增 profile 按 `<tier>-<build>` 命名，例如 `t3-mps`。

## 刻意不做的事

- **不改既有 extra 名**（`torch-cpu`、`local-*`）；新增的 `local-cpu` 沿用同一命名。它们是公共配置，改名会影响所有下游安装
  命令与 `uv.lock`，需要独立 Issue 与迁移路径。本契约只在定义层把它们归类为「build
  选择」，并登记历史命名。
- **不改 verifier 的现有 profile 名**，因为它们是 CI 与文档的既有调用面。
- **不动 `server/extensions/manifest.py` 主题校验里的 `T1.` / `T2.` 错误码前缀**。那是扩展
  清单校验的命名空间，与 tier 无关（全仓库无第二处引用）。此处登记，防后人误读。

## 统一状态

术语替换**已完成**（纯措辞改动，零行为变化）：

| 范围 | 内容 |
|---|---|
| `README.md`、`README_ZH.md` | `L1–L4` → `T1–T4`；梯级表删除 Platform 列，平台信息拆到新的支持矩阵表；`mps` 与 `cu128`/`rocm` 同标实验性 |
| `MACOS_INSTALL.md`、`WINDOWS_INSTALL.md`、`LINUX_INSTALL.md` | 新增，按本契约书写；由两份 README 的基础环境一节链接 |
| `docs/install_profiles.md` | 原「能力与构建」混轴表拆成「梯级表 + 构建表」 |
| `docs/torch27_candidates.md`、`CONTRIBUTING.md`、`docs/character_rag.md` | `CPU VAD` → `T3 vad`；MPS 定位与 `README.md` 对齐 |
| `docs/alpha-0.17.0.md`、`docs/alpha-0.17.0-acceptance.md` | 只改指向当前配置的梯级名，不改历史事实 |
| `pyproject.toml`、`asr/*`、`server/*`、`tools/verify_python_environment.py`、`tools/verify_clean_python_install.ps1`、`tools/README.md`、`tests/*`、`.github/workflows/*` | `T2a→T2`、`T2b→T4`、`L*→T*`；验证器注释补上「profile = (tier, build)」 |

### 刻意未改（改了会造成语义错误）

| 位置 | 原因 |
|---|---|
| `tools/semantic_journey_evidence.py` 的 `TEST_LEVELS = {"L0"…"L4"}`、`tools/semantic_release_gate.py` 的 `test_level` 判断、`docs/evidence/*.json` 已落盘的 `"level"` | 这是**语义测试等级**命名空间，与安装梯级同字母不同义；改动会静默修改发布闸门并让历史证据失效 |
| `docs/character_rag*.md`、`config/catalog/models/character_rag.json`、`electron/src/shared/configCatalog.generated.ts` 里的 "squared L2 distance" | 数学量（距离），与梯级无关 |
| `local_tts_infer.py` 的 "L2 cache miss"、`vendor/` 下的 `L1/L2 cache` | 无关命名空间 |
| 带日期的证据与发布文档里的历史结论 | 历史记录，不做事实改写 |

### Tier 结构调整（第二轮，有行为变化）

| 范围 | 内容 |
|---|---|
| `pyproject.toml`、`uv.lock` | `vad` 包含 `voice`，`local-models` 包含 `vad`；新增 `local-cpu`（CPU torch 2.7.0 + `local-models`）并加入互斥组。锁文件包与版本集合不变，只有依赖图变化 |
| `tools/verify_python_environment.py` | 新增 `t4-cpu` profile；`vad` profile 识别实际 build，拒绝来源不明的 torch |
| `tests/test_profile_contract.py`、`tests/test_python_environment_verifier.py` | 阶梯声明、单独装 T4 即得全阶梯、`local-cpu` 取 CPU 轮子、互斥组、build 识别 |
| `.github/workflows/python-local-models.yml` | 新增 ubuntu `local-cpu` / `t4-cpu` 候选任务 |
| `README.md`、`README_ZH.md`、`docs/install_profiles.md`、`MACOS_INSTALL.md` | 短命令；平台表改为推荐而非上限 |

### 遗留待决

- 非 macOS 平台的 `TTS_DEVICE=auto` 不探测 GPU，固定解析为 `cuda:0`；`local-cpu` 目前靠文档要求显式设 `TTS_DEVICE=cpu`。是否让 `auto` 探测可用设备需单独决定。
- `local-cpu` 上唤醒词的真机运行尚未验证（需要 SenseVoice 模型资产）；当前只有安装与导入验证。

- 把语义测试等级改名（如 `S0–S4`）才能真正消除同字母冲突；需要同时改代码、CI 与已落盘的证据 JSON，属于独立变更。
- 上游同步代价：本轮改到的 `README.md`、`README_ZH.md`、`pyproject.toml` 与 CI 工作流都是上游高频维护的文件，此后 merge 会在这些行产生冲突。如需回推上游，按 `CONTRIBUTING.md` 先开 Issue，并声明这是 public configuration / product semantics 变更。
