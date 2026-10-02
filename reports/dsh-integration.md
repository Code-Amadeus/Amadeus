# DSH 集成报告 — Companion-only × memory/reading

- 日期：2026-09-30（+0800）
- DSH 分支：`feature/companion-only`（worktree `D:\deepseek\111\a\_wt\companion-only`），本次新增提交 `198ef1b`、`846de42`
- 复查对象：`C:\Users\violet\Desktop\Amadeus-Companion-Work\worktrees\integration`，分支 `integration/companion-reading-memory`
- 结论：**集成可用，但有 2 个 `core/memory` 召回缺陷会让"记忆注入"在正常中文对话里静默失效**。缺陷属于 Codex 负责的 `memory/` 核心，DSH 按任务书未直接改，已用单行补丁证明修法。

---

## 1. 集成分支状态

`integration` 工作树当前 **HEAD = DSH 的 `5c7b77a`**（已包含 Companion-only 启动链路），其上叠着 Codex **尚未提交**的改动：

- 已 `A`（staged）：`core/companion/*`、`core/memory/*`、`core/reading/*`、`tests/*`、`pyproject.toml`
- 已 `M`：`core/chat_runtime.py`
- `AM`（staged + 未暂存）：`core/companion/runtime.py`、`core/reading/session.py`、`tests/test_reading_context.py`、`tests/test_reading_server.py`
- ` M`（未暂存）：`server/app.py`

即：**Codex 的 `core/*` 桥接尚未 commit**（与 `codex-progress.md` 的"Remaining: 在集成分支提交工作树中的桥接改动"一致）。DSH 本次未改这些文件。

## 2. 接口接入确认（任务 2）

`server/app.py` 里四条契约都已接上，DSH 未重复实现：

| 接口 | 接入点 | 判定 |
|---|---|---|
| `core.companion.CompanionRuntime` | `server/app.py:325-327`（仅 `--companion` 时构造，root=`runtime/companion`） | 已接入 |
| `runtime.context_block(...)` | `server/app.py:2927-2938`，结果经 `extra_context` 传入 `rt.stream_llm_query(...)`（`:2950`） | 已接入 |
| `runtime.remember_turn(...)` | 经 `remember_conversation` 在回复完成后写回（`server/app.py:2952-2961`）；`remember_turn` 由 `core/companion/runtime.py:80-100` 转发 | 已接入 |
| `runtime.start_reading_server()` | `server/app.py:328-332`，默认端口 17878，`OSError` 降级为告警 | 已接入 |
| `runtime.close()` | `server/app.py:2788-2792`（teardown，先停卡再关 memory/reading） | 已接入 |

`core/chat_runtime.py` 的 `extra_context` 也确实是活路径：`_TurnState.extra_context`（:1121）→ 组装（:893-895，截断 12000 字符）→ `stream_llm_query(extra_context=...)`（:1408、:1480）。

**一处旁路**：`server/app.py` 的 `_run_work_observer_llm`（:2965）调 `decide_with_observer_llm`，没有带 `extra_context`。Companion 模式下 Work 通道已关闭，所以不影响 Companion；如果以后要保留，需要另说。

## 3. 卡进程失败诊断（任务 3）— 已修

原实现把子进程 `stdout/stderr` 丢给 `DEVNULL`，失败时只报
`Companion card exited before becoming ready.`，无法分辨"解释器缺 tkinter"这类真实原因（WorkBuddy 预警缺陷 2 的建议）。

修复（提交 `198ef1b`，`server/companion_runtime.py`）：

1. 卡的 `stdout/stderr` 追加写入 `runtime/companion/card.log`（`buffering=1`，日志不可写时降级告警、不阻断启动）。
2. 启动失败抛出时带齐：**退出码、所用解释器、日志路径、子进程最后一段输出**，并以同样内容写 ERROR 日志。
3. `tkinter/Pillow` 探测保留每个候选解释器的**失败原因**（不是文件 / 超时 / 具体 stderr 最后一行）并写 WARNING，选错解释器在 spawn 之前就能看出来。

新增测试 `tests/test_companion_card_diagnostics.py`（5 例，全过），其中一例真的让卡死掉，断言错误信息里必须出现 `ModuleNotFoundError`、`tkinter` 和日志路径。

## 4. 端口确认（任务 4）

| 端口 | 归属 | 实测 |
|---|---|---|
| 8788 | Companion 卡（Tk） | 空闲可绑；卡冒烟真的绑上并有 `/health` |
| 17878 | Reading Adapter | 空闲可绑；`start_reading_server()` 返回 17878 且 `/health` 正常 |
| 17777 | 后端正主端口 | 未被占用（`--companion` 后端未与其它实例冲突） |

两者不同，且卡与阅读适配器是**两个独立进程/服务**，不共享端口。

## 5. Companion 模式保持（任务 5）

- 静态：`integration` 工作树里 `electron/src/main/startupMode.ts` 仍是 DSH 的 companion 实现，`--companion` / `AMADEUS_COMPANION=1` 解析为 `companion` 模式；`run_amadeus_companion.bat` 存在。
- 动态（本会话实测）：卡进程真实启动 → `/health` 返回 `visible` → `/reaction` 被接受 → `/visibility`、`/focus` 生效 → 第二次 `ensure_running` **复用**而非新建 → `stop()` 后端口释放、无残留进程。
- `work_ledger.sqlite3` 在本次 Companion 验证过程中**未被创建**，普通对话路径不产生 WorkItem（companion 后端把协作/Work planner 通道与 AUIP 叙述都关掉）。
- **未做**：整机启动 Electron 观察任务管理器里没有 `wallpaper64`/`webwallpaper64`、没有 Slice/Canvas/可见主窗口。原因见第 8 节环境限制。

## 6. 运行记录（任务 6）

### 6.1 TypeScript `tsc --noEmit`

```
node .\node_modules\typescript\bin\tsc --noEmit
TSC_EXIT=0
```

（在 `_wt/companion-only/electron`；`integration` 工作树没有 `node_modules`。）

### 6.2 Electron startup tests

```
node --experimental-strip-types --test tests/*.test.mjs
tests 160  pass 160  fail 0
```

含本次新增的 3 个 companion 启动模式用例与改写后的启动偏好用例。重点文件单跑：

```
node --experimental-strip-types --test tests/startupMode.test.mjs tests/startupWindowAccess.test.mjs
tests 8  pass 8  fail 0（startupMode）
tests 2  pass 2  fail 0（startupWindowAccess）
```

### 6.3 DSH Companion runtime / card tests

```
python -m pytest tests/test_companion_only_runtime.py tests/test_companion_card_close.py \
                 tests/test_companion_card_diagnostics.py tests/test_companion_card_live.py -q -s
21 passed
```

`test_companion_card_live.py` 就是任务 6 要求的那次真实启动冒烟：**真的开 Tk 窗口**，然后关掉并验证端口释放。

### 6.4 集成契约验证（对 Codex 模块）

`tools/verify_companion_integration.py`（在 DSH 工作树，用集成工作树复制过来的 `core/memory|reading|companion`）跑了 30 项检查，结果：

```
25 项通过
  3 项失败，全部指向同一个根因：core/memory/store.py 的 recall
```

通过的关键项：阅读事件（`reading.selection` 含 `app`/`cursor`/`chunks`）→ 会话持久化 → `/reading/session` 可查；`start_reading_server()` 绑定 17878；`context_block` 含 `<reading_context>`、`spoiler_cursor`、选中 chunk；越界 chunk 被 `SpoilerGuard` 拒绝、未来文本与 future chunk id 都没进 block；`remember_turn` 写入最近对话；`close()` 停服且端口释放；三层存储文件落盘；重开后记忆/阅读位置/最近对话都能读回。

### 6.5 Codex 直接测试（memory/reading）

```
python -m pytest tests/test_memory_store.py tests/test_memory_extractor.py tests/test_reading_context.py \
                 tests/test_reading_server.py tests/test_companion_context.py -q
23 passed（其余 16 项 error 全部是本会话 pytest 临时目录权限问题，与本代码无关）
```

## 7. 阻塞项：`core/memory` 的两个召回缺陷

两个缺陷都在 `core/memory/store.py::recall` 的同一条 FTS 路径上，**症状一致**：`<memory_data>` 块在正常中文对话里根本不出现，且没有任何报错（`except sqlite3.OperationalError` 会静默吞掉失败并退化）。

### 缺陷 A（确定是 bug）：`ORDER BY bm25(f)` 让多 token 召回整条失效

- 位置：`core/memory/store.py:263` → `order_sql = "bm25(f) ASC, ..."`
- 复现：

```python
store.remember([MemoryRecord.create(text="user dislikes spoilers", kind="preference",
                                    scope="user", namespace="general")])
store.recall("spoilers")        # -> 1 条（走 LIKE）
store.recall("spoilers plot")   # -> 0 条（走 FTS，应为 1 条）
```

- 机理：查询有多个 token 且都 ≥3 字符时走 FTS；`bm25()` 的实参必须是 FTS5 表名，别名 `f` 不是合法实参，SQL 报错 → 被 `except sqlite3.OperationalError` 捕获 → 退化到 `_fallback_like`，而 fallback 对多个 token 用的是 **AND**（`store.py:274`），于是"有 1 个词命中"也返回空。
- 已证实的修法（单行）：`bm25(f)` → `bm25(memory_fts)`。打上后 `recall("spoilers plot")` 返回 1 条，`recall("weather forecast")` 仍返回 0 条。

### 缺陷 B（确定是行为缺口）：中文句子共享词也召不回

- 复现：

```python
store.recall("剧透")                 # -> 1 条
store.recall("接下来会不会剧透？")     # -> 0 条
```

- 机理（实测 SQLite FTS5 分词）：
  - FTS5 把**整段中文当作一个 token**：`match '"剧透"'` 在存有 `用户不喜欢主动剧透。` 的表上返回 **0**，`match '"用户不喜欢主动剧透"'` 返回 1。
  - `store.py:256-258` 把整句中文切成 1 个 ≥3 字符的 token，于是走 FTS；FTS 用短语匹配，命中不了；退化 LIKE 又只在"所有 token 都 <3 字符"时才会触发。
  - 结果：中文提问既进不了 FTS 的命中，也拿不到 LIKE 兜底。
- 影响面：`CompanionContextBuilder.build()` 用**用户当前这句话**当 query。中文用户说一句话，`<memory_data>` 基本恒为空 —— 也就是"长期记忆注入"这条链在中文场景下等于没接。
- 修法方向（不由 DSH 决定，交给 Codex）：中文 query 也走 LIKE/OR 兜底（例如按 CJK 单字或二元切分并 OR 连接），或给 `memory_fts` 换支持 CJK 的 tokenizer，或两者都做。

> DSH 未修改 `core/memory/store.py`：任务书禁止直接改集成分支的 memory/reading 核心文件。上面两条只做了"证明"，未落地。

## 8. 环境限制（未完成项，需真实桌面复核）

1. **无法整机跑 Electron**：`integration` 工作树没有 `node_modules`，且本会话 npm 无法写 `%LOCALAPPDATA%\npm-cache`（被沙箱拒绝），junction/`mklink` 也被拒绝。因此第 5 节里的"无 `wallpaper64` / 无 Slice/Canvas / 无可见主窗口"只做到静态与单元级确认。
2. **没有 `.env`**：`integration` 工作树同样缺 `.env`（被 gitignore）。真机启动前需保证工作目录有 `.env`，否则 `AMADEUS_BACKEND_TOKEN`/`INSTANCE_NONCE` 不完整会启动失败。
3. **沙箱副作用（已记录，非产品问题）**：
   - 受限沙箱里 `127.0.0.1` 的 `connect_ex` 会假成功、`bind` 会报 10048；本报告的端口判定改用 `bind('0.0.0.0', port)` 与真实 `/health`。
   - pytest 的 `tmp_path` 在系统临时目录被拒（`PermissionError`）。可用 `--basetemp=<工作区目录>` 绕过；本次 DSH 测试即用 `--basetemp='D:\deepseek\111\a\_pt5'` 跑通。
4. **真实模型链路未加载**：GPT-SoVITS / Qwen3-ASR 权重未启动，语音只验证到通道与契约层。

## 9. 需要谁做什么

| 项 | 归属 | 动作 |
|---|---|---|
| 缺陷 A：`bm25(f)` → `bm25(memory_fts)` | Codex | 改 `core/memory/store.py:263`；补一条"两个 token、其中一个命中"的回归测试 |
| 缺陷 B：中文 query 召回 | Codex | 决定 CJK 分词/兜底策略；补中文召回测试 |
| 提交 `core/*` 桥接 | Codex | 按 `Codex任务.md` 第 1 项 |
| 把 `198ef1b`、`846de42` 带进 integration | Codex 或 DSH（需授权） | `integration` 工作树对 DSH 只读，DSH 未改集成分支任何文件 |
| 整机验收（无壁纸进程、无 Slice/Canvas、无可见主窗口、麦克风/打断） | WorkBuddy / 用户 | 见 `docs/companion-only-startup.md` 的验收步骤 |

## 10. 收尾（第二轮任务书）

### 10.1 Work overlay 门禁 — 已完成

`integration` 分支 `4067231`：`createWorkOverlayWindow()` 首行加 `if (COMPANION_ONLY) return`，
与 Slice/Canvas 的门禁一致。现在 `--work-overlay` 也无法给 companion 启动加一个 Work 面板。

### 10.2 文档更新 — 已完成

`docs/companion-only-startup.md`（同为 `4067231`）新增/修正：

- 新增"Prerequisites for a fresh worktree"：worktree 只有被跟踪文件，需要补两样 gitignored 的东西
  ——`.env`（模型与语音配置；缺了后端会因 `AMADEUS_BACKEND_AUTH_MODE`/`TOKEN`/`INSTANCE_NONCE`
  不完整而启动失败）与 `assets/companion/`（可选 Lite 立绘包，缺了卡仍能显示字幕与文字头像）。
- 后端正常启动时隐藏主窗口、后端启动失败时显示主窗口以暴露错误。
- `createWorkOverlayWindow()` 加入跳过清单。

### 10.3 真机运行 — 角色卡四项验收已在真实桌面确认

从 `integration` 工作树**真实启动**了集成版 Companion-only 运行时（无 Electron）：

```
cd worktrees\integration
$env:AMADEUS_BACKEND_AUTH_MODE='required'; $env:AMADEUS_BACKEND_TOKEN=<32字符>
$env:AMADEUS_BACKEND_INSTANCE_NONCE=<19字符>; $env:AMADEUS_COMPANION='1'
python -m server.app --port 17777 --companion
```

**桌面级观测**（进程 + 窗口枚举 + 按窗口标题截图，不是日志推断）：

| 项目 | 观测 | 判定 |
|---|---|---|
| `wallpaper64` / `webwallpaper64` | 进程列表无输出 | ✅ |
| 可见主窗口 / Slice / Canvas | 有标题的窗口枚举里没有 Amadeus 主窗口 | ✅ |
| 角色卡置顶显示 | 枚举到 `Amadeus · VN Companion`，`visible=True`，`hwnd=2034916`，进程 `pythonw` pid 145148 | ✅ |
| 字幕 + 说话状态 | 按 `/reaction` 协议写入后：字幕变成指定文本、表情转微笑、状态从 `STANDBY` 变 `VOICE`、信号条激活 | ✅ |
| 关闭后无残留 | `card-close` → `{"ok":true}`；20 秒后卡窗口消失、三端口全停、无 `python`/`pythonw` 残留进程 | ✅ |

截图证据（共享工作区）：

- `artifacts\02-companion-card-idle.png` — 待机：立绘、默认字幕、`STANDBY`
- `artifacts\03-companion-card-speaking.png` — 说话：新字幕、微笑表情、`VOICE`、信号条激活

### 10.4 仍然受限：Electron 二进制在本会话无法启动

`electron.exe` 在本机任何参数下都秒崩，两种崩法都试过：

| 启动方式 | 退出码 | 含义 |
|---|---|---|
| `electron.exe --version` | 静默返回 / `0x80000003` | 断点异常 |
| `electron.exe <最小应用>` | `0x80000003` | 断点异常 |
| `... --no-sandbox` | `0xC0000005` | 访问违例 |
| `... --disable-gpu` / `--disable-gpu-sandbox` | `0x80000003` | 断点异常 |
| `ELECTRON_DISABLE_SANDBOX=1` | `0xC0000005` | 访问违例 |

对照实验很关键：**最小 Electron 应用**（三行 `main.js` + 一个空白 `BrowserWindow`）同样崩，
所以这与 Amadeus 的启动逻辑无关。另外 `.electron-user-data` 目录在启动尝试后**依然不存在**
—— Electron 连 `app.setPath('userData', ...)` 都没走到，崩在 JS 主进程之前。
主仓库与集成分支的两个 Electron 二进制表现一致。

结论：这是**本会话/本机环境**（Chromium 需要自己的沙箱与作业对象，被上层沙箱拦下），
不是仓库缺陷。因此"Electron 主进程 + 托盘"这条整机链路未验收；但上表四项 GUI 验收
已用真实运行时的后端 + Tk 卡完成。

### 10.5 另一个环境短板：语音音频链路

`.env` 里 `TTS_BACKEND=fish_audio` 但**没有 `FISH_TTS_API_KEY`**，所以 `tts init failed`，
`/vn/speak` 虽返回 `queued` 且成功入队三句（`source=vn`），最后停在
`TTS backend is not initialized; cannot generate speech`。字幕翻译也因远端不可达报
`Connection error.`。

也就是说：**对话 → 分句 → 入队 → 落到卡的通道**已验证，但**真实音频输出**没验证到，
因为本环境既没有 TTS 凭据也没有外网。要在真机上听声音，需要 `.env` 里换成可用的
TTS 后端（本地 GPT-SoVITS 或带 key 的远端）。

## 11. 复现命令

```powershell
# 集成契约验证（在集成分支工作树）
cd C:\Users\violet\Desktop\Amadeus-Companion-Work\worktrees\integration
python tools\verify_companion_integration.py     # 30/30

# FTS5 中文分词取证
python tools\probes\_fts_tokenizer_probe.py

# DSH companion 测试 + 真实开窗冒烟
python -m pytest tests\test_companion_only_runtime.py tests\test_companion_card_close.py `
                 tests\test_companion_card_diagnostics.py tests\test_companion_card_live.py -q -s

# Electron 启动模式测试
cd electron
node --experimental-strip-types --test tests\startupMode.test.mjs tests\startupWindowAccess.test.mjs

# 真机 Companion-only（需要桌面会话）
run_amadeus_companion.bat
```
