# macOS 安装指南

本文覆盖 Amadeus 在 macOS 上的源码部署。措辞遵循 [分层与构建术语契约](TIER_TAXONOMY.md)：
**Tier** 是安装阶梯，**Build** 是轮子来源，**Implementation** 是远端还是本地，**Platform**
决定能不能装。本文所有层级名、构建名、命令都按该契约书写。

平台结论先说：**Apple Silicon 推荐 `T4 embedded`（build `mps`）；Intel Mac 推荐 `T2 voice`**。
梯级不按平台设上限；Intel Mac 装不了 `T3` 及以上，只是因为锁定的 torch 2.7.0 没有 macOS x86_64 轮子。

以下内容取自 `README.md`、`docs/install_profiles.md`、`docs/macos_wallpaper_startup.md`、
`.github/workflows/python-macos.yml`、`.github/workflows/python-local-models.yml`，以及 uv 官方环境变量文档与清华 TUNA 镜像站帮助页。
凡涉及版本钉死或 CI 取值的地方都标了出处。

## macOS 支持范围

| Tier | 能力 | Apple Silicon | Intel | 装机命令 | 验证 profile |
|---|---|---|---|---|---|
| `T1 core` | 文字对话、Work、Provider、角色渲染 | 受支持（CI） | 受支持 | `uv sync --locked` | `cpu` |
| `T2 voice` | 远端语音 I/O：播放、麦克风、AEC、远端 TTS / ASR | 受支持（CI） | 受支持 | `--extra voice` | `voice` |
| `T3 vad` | 实时打断 | 受支持（CI：仅安装/导入） | **装不上**（缺 x86_64 轮子） | `--extra vad --extra torch-cpu` | `vad-cpu` |
| `T4 embedded` | 本地 TTS、本地 ASR、唤醒词 | 实验性（CI：仅安装/导入；TTS 实机证据基于 torch 2.6.0） | **装不上** | `--extra local-mps`（或 `--extra local-cpu`） | `mps`（`local-cpu` 用 `t4-cpu`） |

可用 build：Apple Silicon 上是 `mps` 与 `cpu`（**两者在 macOS 上解析到同一个轮子**，差别只在
`local-mps`/`local-cpu` 额外带 `local-models` 依赖集，且 `local-mps` 与 `local-cpu` 在 macOS 上
完全等价）；Intel Mac 无可用 build。

`T4` 的 macOS 形态（来自术语契约的支持矩阵与能力矩阵）：

- 本地 **TTS（GPT-SoVITS）走 MPS**——见 `README.md` 的「Torch 2.7 profiles」；代码在 `local_tts_infer.py`
  校验 `torch.backends.mps.is_available()` 并调用 `torch.mps.synchronize()`
- 本地 **ASR（Qwen3）只能落 CPU**——应用层设备枚举只接受 `auto`/`cpu`/`cuda`，没有 `mps`
  （`README.md` 的「Torch 2.7 profiles」、`docs/torch27_candidates.md` 的「Application integration boundary」）
- **唤醒词恒 CPU**——与平台无关，`asr/wake_service.py` 固定 `backend.load("cpu")`

其余 macOS 边界：

- Electron 只能从源码启动；**不提供安装包，也没有签名与公证**。macOS 壁纸宿主仅有社区真机
  验证（追踪于 #46）。
- Docker 不是受支持的安装方式。
- VN Player 是 Windows 专属的实验功能。
- CI 覆盖安装、导入、契约测试与 Electron 构建，但**不覆盖** Electron 界面、真实音频设备、
  麦克风采集、连续播放、打断以及壁纸集成；这些仍需人工验收。

## 前置条件

| 依赖 | 要求 | 出处 |
|---|---|---|
| Python | 3.12（`requires-python = ">=3.12,<3.13"`） | `pyproject.toml` |
| uv | **>= 0.12.8**（`required-version`） | `pyproject.toml`，CI 安装 `0.12.8` |
| Node.js | 22（参考值 `22.21.1`） | `README.md`、`python-macos.yml` |
| PortAudio | `T2` 必需（PyAudio 需源码编译） | `README.md`、`python-macos.yml` |
| Xcode 命令行工具 | 上述源码编译需要 | — |
| Homebrew | 可选，用于装 `uv` 与 `portaudio` | `README.md` |
| 显卡 | `T1`–`T3` 都不需要 | — |

```bash
xcode-select -p            # 未安装则执行：xcode-select --install
uv --version               # 必须 >= 0.12.8
node --version             # 22.x
```

Python 由 uv 管理，不需要系统 Python。新版 macOS 自带的 `python3` 对本项目来说太新，**不要**
用它启动应用。

## 安装 uv（环境里没有 uv 时）

项目通过 `pyproject.toml` 的 `required-version = ">=0.12.8"` 钉住 uv 版本。版本偏低时，本仓库
内**任何** `uv` 命令都会直接失败：

```text
error: Required uv version `>=0.12.8` does not match the running version `0.12.5`
```

下面三种方式任选一种，装完用 `uv --version` 确认。

### 方式一：pip 走清华 PyPI（不需要访问 GitHub）

uv 在 PyPI 上有官方发行包，因此可以直接从清华源安装：

```bash
# 若系统/Homebrew 的 python3 是 externally-managed，先建一个临时环境
python3 -m venv ~/.uv-bootstrap
~/.uv-bootstrap/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple uv

# 完成后再把它链到 PATH（示例路径，按实际 shell 配置调整）
ln -sf ~/.uv-bootstrap/bin/uv /usr/local/bin/uv
```

也可以直接对已有的 pip 使用：

```bash
python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple uv
```

需要指定版本时写 `uv==0.12.8`；不写则取最新版（同样满足 `>=0.12.8`）。

### 方式二：Homebrew（走清华 bottles 镜像）

```bash
brew install uv
```

若 Homebrew 本身也需要走国内镜像，按清华 Homebrew 帮助页设置 `HOMEBREW_API_DOMAIN`、
`HOMEBREW_BOTTLE_DOMAIN`（取值见 <https://mirrors.tuna.tsinghua.edu.cn/help/homebrew/> 的
「Homebrew bottles」一节），并为 Homebrew 内的 pip 设置清华 PyPI：

```bash
export HOMEBREW_PIP_INDEX_URL="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
```

### 方式三：官方独立安装脚本（需要能访问 GitHub）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

清华镜像站**没有**镜像 `astral-sh/uv` 的 release，所以这条路径必须能连上 GitHub。无法直连时，
可改用其他高校镜像（非清华，取值以各站帮助页为准），例如 USTC 文档给出的
`UV_DOWNLOAD_URL=https://mirrors.ustc.edu.cn/github-release/astral-sh/uv/LatestRelease/`，或设置
`UV_INSTALLER_GITHUB_BASE_URL` 指向任意 GitHub 代理后再执行安装脚本。

## 国内镜像配置（清华源）

镜像只影响下载来源，不改变任何功能行为。CI 与本文的参考值都不假定使用镜像。

### uv：把默认索引指向清华 PyPI

uv 的配置文件位于 `~/.config/uv/uv.toml`（macOS 与 Linux 相同）。按清华帮助页给出的写法：

```toml
[[index]]
url = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
default = true
```

等价的环境变量写法（`UV_DEFAULT_INDEX` 需要 uv >= 0.4.23；旧的 `UV_INDEX_URL` 已弃用）：

```bash
export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
```

`https://pypi.tuna.tsinghua.edu.cn/simple` 与 `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple`
是同一个服务的两个域名，任选其一。

几点必须注意：

- **本仓库的 `uv.lock` 记录包来源为 `pypi.org/simple`。** 更换默认索引后，uv 可能提示锁文件与
  当前索引配置不一致。此时按报错提示处理：要么去掉 `--locked` 让 uv 依当前索引重新解析（会
  改写本地 `uv.lock`，**不要提交**），要么在该命令前临时取消镜像。
- `pyproject.toml` 里已经声明了若干 `[[tool.uv.index]]` 显式索引（PyTorch 的 `pytorch-cpu` /
  `pytorch-cu128` / `pytorch-cu124`）。这些是 `explicit = true` 的专用索引，只服务于钉住它们的
  包，不受默认索引替换影响。
- macOS 上的 torch 来自 PyPI（即 build `mps`/`cpu` 同源），因此走清华源即可；cu124/cu128/ROCm
  的专用索引只在对应 Windows/Linux 场景才会被使用。

### CPython 解释器下载

`uv venv .venv --python 3.12` 在本机没有 3.12 时，会去 Astral 的 `python-build-standalone` 下载
解释器，而该发布页**清华没有镜像**。可选做法：

1. 让 uv 用本机已有的 3.12：用 Homebrew 装（走上一节的 bottles 镜像）后 uv 会自动发现。

   ```bash
   brew install python@3.12
   uv venv .venv --python 3.12
   ```

2. 或把解释器下载指向其他高校镜像（非清华），`UV_PYTHON_INSTALL_MIRROR` 会替换
   `https://github.com/astral-sh/python-build-standalone/releases/download` 这一段：

   ```bash
   export UV_PYTHON_INSTALL_MIRROR="https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/"
   uv venv .venv --python 3.12
   ```

   （该变量需要 uv >= 0.2.35；取值以所选镜像站帮助页为准。）

### npm 与 Electron

清华镜像站**不提供 npm registry 镜像**，这部分请使用 npmmirror（`README.md` 中给出的 Electron
镜像方式）：

```bash
npm config set registry https://registry.npmmirror.com
export ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
```

`ELECTRON_MIRROR` 需要在 `npm ci` **之前**设置。

## 安装

环境目录必须叫 `.venv`，且放在仓库根目录。Electron 会自动发现其中的解释器（macOS 下是
`bin/python3`），无需设置 `AMADEUS_PYTHON` 覆盖。

要装 `T2` 及以上时，**先**安装 PortAudio（PyAudio 会从源码编译，缺少它时 `uv sync` 会在编译处失败）：

```bash
brew install portaudio
```

```bash
git clone https://github.com/Code-Amadeus/Amadeus.git
cd Amadeus

uv venv .venv --python 3.12
uv sync --locked                              # T1 core
uv sync --locked --extra voice                # T2 voice（需要上面的 PortAudio）
```

每次更新源码后构建一次 Electron 前端：

```bash
cd electron
npm ci
npm run build
cd ..
```

`npm ci` 会通过 postinstall 钩子安装 Electron 与钉死的 Pi 运行时。默认的 Pi 连接复用 Main Chat
的同一个 `DEEPSEEK_API_KEY`，因此桌面使用不需要另外安装或登录 agent。

### 选择 Tier 与 Build

每次都要给出**一条完整的命令**：`uv sync` 是精确同步，漏写的层会被删掉。Tier 是累积的，Build
是互斥的，两者正交——**同一个 Tier 可以配不同 Build，同一个 Build 也可以服务不同 Tier**。
需要保留开发工具时，在完整命令后追加 `--extra dev`。

```bash
# T1 core
uv sync --locked
uv run --locked --no-sync python tools/verify_python_environment.py --profile cpu

# T2 voice
uv sync --locked --extra voice
uv run --locked --no-sync python tools/verify_python_environment.py --profile voice

# T3 vad（必须显式带一个 build；vad 已包含 voice）
uv sync --locked --extra vad --extra torch-cpu
uv run --locked --no-sync python tools/verify_python_environment.py --profile vad-cpu

# T4 embedded（Apple Silicon；local-mps = local-models 依赖集（含 vad、voice）+ build mps）
uv sync --locked --extra local-mps
uv run --locked --no-sync python tools/verify_python_environment.py --profile mps --require-mps-device
```

Build 命名说明：`torch-cpu` 是**裸 build**，`local-<build>` 是 **build + `local-models` 打包**，
它们都是历史命名，按术语契约归类为「build 选择」。在 macOS 上 `torch-cpu` 拿到的轮子带 MPS
支持，并非 CPU-only。

需要可选的角色 RAG 时，在完整命令后追加 `--extra rag`；它只增加本地 embedding 依赖，不改变
Torch 构建选择。

校验器只检查包与导入，**不能**证明模型推理、音频设备或桌面验收可用。

## 配置

```bash
cp .env.example .env
```

编辑 `.env`：

- 为默认的远端 Main Chat 取消注释并填写 `DEEPSEEK_API_KEY`。用不到的其他厂商 key 保持注释
  状态。
- 只想先跑纯文字：设 `TTS_BACKEND=disabled` 与 `WAKE_ENABLED=false`。
- **TTS 的 `remote` 实现（`T2` 即可）：** 推荐 Fish Audio S2.1 Kurisu 组合（`FISH_TTS_WS_URL`、
  `FISH_TTS_API_KEY`、`FISH_TTS_MODEL`、`FISH_TTS_REFERENCE_ID`、`FISH_TTS_LATENCY`）。MiMo 与
  通用 OpenAI 兼容端点同样支持。
- **TTS 的 `embedded` 实现（需 `T4`）：** `TTS_DEVICE=auto` 在 Apple Silicon 上固定解析为 `mps`
  （MPS 不可用时直接报错，不回落；需要 CPU 时显式写 `TTS_DEVICE=cpu`），在 Intel 上解析为 `cpu`；
  CPU 合成会自动走边车。macOS 上 `TTS_DEVICE=cuda` 没有意义。
- **ASR 的 `embedded` 实现：** macOS 上只能落 CPU，建议显式写 `QWEN3_ASR_DEVICE=cpu`，而不是
  依赖 `auto`。

升级后**不要**整份覆盖已有的 `.env`，应按键合并，保住本地已填的 key。当前键集以 `README.md`
与 `.env.example` 为准。

### 外部运行时资产

模型权重、参考音频与角色包都**不会**由安装器下载。`T4`（本地 TTS / ASR）与可选角色包都需要
单独安装。请从官方资源中心获取，然后用：

```bash
uv run --locked --no-sync python tools/external_assets.py verify  <archive.zip>
uv run --locked --no-sync python tools/external_assets.py install <archive.zip>
uv run --locked --no-sync python tools/external_assets.py status
```

本地模型加载强制 Hugging Face 离线模式，即使父进程设置了 `HF_HUB_OFFLINE=0` 也一样。文件缺失
会直接失败，而不会触发隐式下载。角色包显示 **未安装** 是健康状态，不会禁用对话、Work 或纯
后端启动。

## 启动前检查清单

只需完成与用户所选功能对应的项（见「适用于」列）；不适用的项不影响启动。纯文字桌面启动只需要标
「所有启动」与「桌面」的项，headless 后端只需要「所有启动」的项。**自动**项由 Agent 执行并检查输出；
**手动**项由用户完成，Agent 只提示并等待确认：不代填密钥、不代替用户接受许可条款、不在用户同意前提权
安装软件。适用的项未通过时，先按「故障排查」处理再启动。密钥不是启动前提：可以先启动，再在 Settings
中填写。

**自动（Agent 执行）**

| 检查项 | 适用于 | 命令或判定 |
|---|---|---|
| 依赖与目标梯级一致 | 所有启动 | 重跑目标梯级的完整 `uv sync --locked …`，再运行对应的 `verify_python_environment.py --profile …`，必须输出 `environment ok` |
| 前端已构建 | 桌面 | `cd electron && npm ci && npm run build && cd ..`（每次拉取新源码后都要重跑） |
| `.env` 存在 | 所有启动 | 不存在时 `cp .env.example .env`；已存在则**不要覆盖** |
| 非密钥配置与已装梯级一致 | 所有启动 | 只跑文字：设 `TTS_BACKEND=disabled`、`WAKE_ENABLED=false`；本地 ASR 显式设 `QWEN3_ASR_DEVICE=cpu` |
| PortAudio（T2 及以上） | 语音（T2 及以上） | `brew list portaudio` 成功；缺失时 `brew install portaudio`（Homebrew 不需要管理员权限） |
| 本地语音资产（T4 本地语音） | 本地 TTS / ASR | `uv run --locked --no-sync python tools/external_assets.py status`；用户下载好的 ZIP 由 Agent 执行 `verify` 与 `install` |
| 日文字典预热（本地 TTS） | 本地 TTS | `uv run --locked --no-sync python -c "import pyopenjtalk; print(pyopenjtalk.g2p('準備完了'))"` |
| Browser Work 的浏览器 | Browser Work | `uv run --locked --no-sync python -m playwright install chromium` |
| 端口空闲，没有其他 Amadeus 在运行 | 所有启动（`5173` 只影响桌面） | `lsof -nP -iTCP:5173 -iTCP:17777 -sTCP:LISTEN` 无输出即空闲。`5173` 被占用时在仓库根 `.env` 设 `AMADEUS_DEV_PORT`；`17777` 被占用时先问用户，不要结束未知进程 |
| 壁纸 app 与仓库位置一致（用登录自启动时） | 登录自启动壁纸 | 仓库移动过就重新运行 `./scripts/build_macos_wallpaper_app.sh --force` |

**手动（用户完成，Agent 提示并等待确认）**

| 事项 | 适用于 | 必须手动的原因 |
|---|---|---|
| 填写 `DEEPSEEK_API_KEY`（用到时还有 Fish Audio 等其他密钥） | 用到对应服务时；可启动后再填 | 密钥属于用户。可以启动后在 Settings 中填写，或由用户自己编辑 `.env` |
| 安装 Xcode 命令行工具（`xcode-select --install`） | 语音（T2 及以上） | 会弹出系统安装对话框，需要用户确认 |
| 从[资源中心](https://code-amadeus.github.io/#resources)下载资产包 | 本地语音、角色动画 | 需要用户访问下载页 |
| 获取 Live2D Cubism Core（用 Live2D 时） | Live2D | 用户须按 Live2D 条款自行获取，Amadeus 不分发 |

## 启动

```bash
# 桌面端（Electron）
cd electron && npm run electron:dev

# 仅后端
uv run --locked --no-sync python -m server.app --port 17777
```

`main.py` 不是应用入口，它只打印退役提示。

- 开发启动器与 `npm run dev` 默认用 **5173** 端口；后端用 **17777**。
- 若 5173 报 `EACCES` 或 `EADDRINUSE`，把 `AMADEUS_DEV_PORT` 写进**仓库根的 `.env`**
  （`electron/.env` 无效；已存在的进程环境变量优先级更高），然后重启整个启动器。端口绑定失败
  会给出可操作的报错并退出，不会干等或悄悄换端口。
- 更新源码后要先在 `electron/` 下跑 `npm run build` 再启动；只改端口则仅需重启启动器。
- 改完启动相关设置后点 **Restart backend to apply**，之后需重新打开已有的角色与壁纸窗口。

### 登录时启动壁纸

macOS 没有 Lively / Wallpaper Engine 宿主。Electron 自己承载场景，并用单独一个透明窗口承载
可交互 Canvas；场景保持点击穿透，因此不会挡住 Finder 桌面图标。

```bash
cd electron && npm install && cd ..
./scripts/build_macos_wallpaper_app.sh --force      # 在仓库根生成 ./Amadeus Wallpaper.app
open "./Amadeus Wallpaper.app"
./scripts/setup_autostart.sh enable
./scripts/setup_autostart.sh status
```

生成的 app 内嵌了仓库的绝对路径，所以**移动仓库后必须重建**；可用 `AMADEUS_PROJECT_ROOT` 覆盖
该路径。日志写在 `~/Library/Logs/Amadeus/`。测试前先退出其他正在运行的实例；停止壁纸请用
Quit / Command-Q，让它自有的后端干净退出。启动器不会按端口杀进程：若 17777 属于未知的监听者，
Electron 只报冲突并保持不动。

## 开发自检

```bash
uv sync --locked --extra dev
uv run --locked --no-sync python tools/verify_python_environment.py --profile ci
uv run --locked --no-sync python -m ruff check .
uv run --locked --no-sync python tools/architecture/generate_views.py --check
uv run --locked --no-sync python -X utf8 tools/run_tests.py

cd electron && npm ci && npm test && npm run build && npm audit --audit-level=high
```

`uv sync --locked --extra dev` 会选中核心加开发工具，并删掉可选的语音/模型层；要在保留某一 Tier
的同时加上开发工具，就把 `--extra dev` 追加到该 Tier 的完整命令后。

## 故障排查

| 现象 | 成因与处理 |
|---|---|
| `command not found: uv` | 尚未安装 uv，按「安装 uv」一节任选一种方式。 |
| `error: Required uv version '>=0.12.8' does not match the running version` | 本仓库内任何 `uv` 命令都会失败。升级 uv 到 >= 0.12.8。 |
| `uv sync` 报锁文件与索引不一致 | 默认索引被换成了镜像。去掉 `--locked` 重新解析（改动本地 `uv.lock`，不要提交），或临时取消镜像。 |
| `uv venv` 长时间卡住 | 在从 GitHub 下载 CPython。改用本机已有的 3.12（如 `brew install python@3.12`）或设置 `UV_PYTHON_INSTALL_MIRROR`。 |
| 执行 `uv sync` 后需要用到的包被删了 | `uv sync` 是精确同步。始终给完整的 Tier + Build 命令，含 `dev` 选项。 |
| 拉到新源码后校验器报包版本不符 | 按目标 Tier 与 Build 重跑完整的 `uv sync`，再重新校验。 |
| `--extra voice` 时 PyAudio 编译失败 | 装 `brew install portaudio` 与 Xcode 命令行工具，然后在全新的 `.venv` 里重新同步。 |
| Intel Mac 装不上 `T3`/`T4` | 锁定的 torch 2.7.0 没有 macOS x86_64 轮子；Intel Mac 推荐 `T2`。 |
| 本地 ASR 无法使用 `mps` | 应用层 ASR 只接受 `auto`/`cpu`/`cuda`。改用 `QWEN3_ASR_DEVICE=cpu`。 |
| 启动时报 `EACCES` / `EADDRINUSE` | 在仓库根 `.env` 设 `AMADEUS_DEV_PORT`，然后重启启动器。 |
| 改了源码但桌面端没变化 | 重新构建：`cd electron && npm run build`。 |
| Electron 下载失败 | 设置 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/` 后重新 `npm ci`（清华镜像站无 npm 镜像）。 |
| 不要复制或重命名 `.venv` | 已安装的入口脚本内嵌原始解释器绝对路径。请在最终路径上重建环境。 |
| 移动仓库后壁纸 app 失效 | 重建 `Amadeus Wallpaper.app`，它保存的是绝对路径。 |

## 本文不覆盖的内容

- 分发所需的签名、公证与打包。
- 麦克风输入、播放、连续语音、打断、壁纸投射的真机验收。CI 无法替代这些。
- `T4` 在 Windows / Linux 上的形态（见 [术语契约](TIER_TAXONOMY.md) 的支持矩阵）。
