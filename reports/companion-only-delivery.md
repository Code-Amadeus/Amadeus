# Companion-only 交付报告（DSH）

- 分支：`feature/companion-only`
- worktree：`D:\deepseek\111\a\_wt\companion-only`
- 提交：`764d910`（功能）、`f8c7780`（真机卡进程冒烟测试）
- 主仓库 `main` 工作区未改动（其未提交改动原样保留）

## 1. 交付物对照任务书

| 任务书要求 | 落点 |
|---|---|
| 1. Companion-only 启动入口，不新建整体启动架构 | `--companion` / `AMADEUS_COMPANION=1` → `resolveStartupMode()`；`run_amadeus_companion.bat`；`python -m server.app --companion` |
| 2. 跳过 `windowsWallpaper.start()` | `STARTUP_MODE === 'wallpaper'` 才创建 `WindowsWallpaperSession` 与托盘，companion 下 `windowsWallpaper` 为 `null` |
| 3. 跳过 Slice / Canvas | `createElectronSliceWindow()`、`createElectronCanvasWindow()` 在 companion 下直接返回；`electron-slice.open` IPC 在创建前拒绝 |
| 4. 保留 Companion bridge 数据通道 | 主窗口仍创建为隐藏 bridge 客户端；`companionBridge` / `wallpaper.start` 协议与 `CompanionPanel` 未改 |
| 5. 保留原 session/context、字幕、语音状态、口型 | 后端未改 session/context 组装；字幕与语音边界仍走 `vn_tts_bridge` 的 `/reaction` 通道（`source=vn_playback` 驱动 speaking/口型姿态，`vn_pretranslation` 驱动字幕） |
| 6. 关闭 Work/工具/Provider/权限/AUIP 上下文 | `--companion` 下 `cooperative_chat_enabled=False`（同时关掉 Work planner 与 `CONTROL_DECISION_AUTHORITY` 路径）、`AUIP_NARRATION_ENABLED` 分支跳过 |
| 7. 关闭角色卡清理本次拥有的后端与音频资源 | 卡的右键/关闭 → `render/vn_overlay_close.py` → `/companion/card-close` → 持卡后端 `stop()` 卡进程后退出；TTS/ASR 随 `bootstrap` teardown 释放；复用他人后端时 Electron 只退出自己 |
| 8. 启动、关闭、重连、单实例测试命令 | 见 `docs/companion-only-startup.md` 与本文第 4 节 |

## 2. 主要改动

**Electron（`electron/src/main/`）**

- `startupMode.ts`：新增 `StartupMode = 'window' | 'wallpaper' | 'companion'`、`resolveStartupMode()`、`isCompanionOnlyStartup()`；`isWallpaperStartup()` 签名不变，内部改为模式解析。companion 判定优先于 Windows 壁纸偏好。非 Windows 上 `--no-wallpaper` / `AMADEUS_WALLPAPER=0` 的原有语义保持不变。
- `index.ts`：启动表面一次解析为模块常量；隐藏主窗口（`show: !(mode !== 'window')`）；companion 下不建壁纸托盘、不建 Slice/Canvas；新增 `/companion/card/*` 客户端与 `CompanionTray`（显示/隐藏/退出）；`second-instance` 在 companion 下改为聚焦已有卡；companion 启动会复用已存在的 Amadeus 后端且不接管其生命周期；后端子进程加 `--companion` 与 `AMADEUS_COMPANION=1`。
- `companionTray.ts`（新）：由于没有可见主窗口，托盘是唯一的显示/隐藏/退出入口。

**Python 后端**

- `server/companion_runtime.py`（新）：唯一的卡进程所有者。含 Tk+Pillow 解释器探测（`VN_OVERLAY_PYTHON` → 当前解释器 → `pythonw`/`python`）、桌面凭证转发（卡上的语音/视觉按钮要回调本后端）、`ensure_running` 端口探测与复用、`set_visible` / `focus` / `stop`、以及发布给 TTS 的 `default_overlay_url()`。
- `server/app.py`：`bootstrap(port, companion_only=False)`；`--companion` CLI；`/companion/card/status|visibility|focus|card-close`；teardown 中停止卡；companion 下关闭 Work 协作通道与 AUIP 叙述。
- `server/vn_tts_bridge.py`：`submit_vn_tts` 的 `overlay_url` 缺省回落 Companion 卡端点；payload 显式给出时仍以 payload 为准（VN 会话不受影响）。
- `render/vn_overlay_close.py`（新）+ `render/vn_overlay_window.py`：卡在 companion 模式下把自己的关闭上报给持卡后端；`/focus` 端点用于重复启动时置顶已有卡。
- `tools/vn_portrait_overlay_lite.py`：新增 `--on-close {exit,card-close}`，默认 `exit`（VN 行为不变）。

## 3. 已执行的验证（本次会话实测）

| 项目 | 结果 |
|---|---|
| `node .\node_modules\typescript\bin\tsc --noEmit`（worktree） | 通过（exit 0） |
| `node --experimental-strip-types --test tests/*.test.mjs` | 160/160 通过（含新增 3 个 companion 启动模式用例，及改写后的启动偏好用例） |
| `python -m pytest tests/test_companion_only_runtime.py tests/test_companion_card_close.py -q` | 15/15 通过 |
| `python -m pytest tests/test_companion_card_live.py -q -s`（真实开窗） | 通过：卡进程真实启动、`/health` 返回 `visible`、`/reaction` 被接受、`/visibility` 与 `/focus` 生效、第二次 `ensure_running` 复用而非新建、`stop()` 后端口关闭 |
| 冒烟后进程/端口残留检查 | 8799/8788 无监听、无遗留 `python`/`pythonw` 进程 |

## 4. 验收命令

```powershell
# 1. 启动（Companion-only）
run_amadeus_companion.bat
#   或：cd electron; npx electron . --companion
#   或仅后端：AMADEUS_COMPANION=1 python -m server.app --port 17777

# 2. 卡状态 / 显示隐藏 / 关闭（需要 AMADEUS_BACKEND_TOKEN 时带 X-Amadeus-Token 头）
curl http://127.0.0.1:17777/companion/card/status
curl -X POST http://127.0.0.1:17777/companion/card/visibility -H "Content-Type: application/json" -d "{\"visible\":false}"
curl -X POST http://127.0.0.1:17777/companion/card/focus
curl -X POST http://127.0.0.1:17777/companion/card-close   # 关闭卡并结束持卡后端

# 3. 单实例：再启动一次 run_amadeus_companion.bat，只应聚焦已有卡，不出现第二张卡

# 4. 清理检查
Get-Process wallpaper64,webwallpaper64 -ErrorAction SilentlyContinue

# 5. 回归
cd electron; node --experimental-strip-types --test tests/*.test.mjs
python -m pytest tests/test_companion_only_runtime.py tests/test_companion_card_close.py tests/test_companion_card_live.py -q
```

## 5. 环境限制与仍需人工确认的项

本次会话的环境缺少两样东西，以下项目**未**实测，需在真实桌面环境复核：

1. **Electron 主进程真机启动**：worktree 的 `electron/` 没有可用的 `node_modules`（npm 需要写 `%LOCALAPPDATA%\npm-cache`，被沙箱拒绝；junction/`mklink` 也被拒绝），只在会话内临时复制了一份用于 `tsc` 与单元测试。请在自己的环境 `cd electron; npm ci` 后运行真机验收。
2. **`.env` 未复制**：worktree 没有 `.env`（主仓库那份被 gitignore）。真机验收时请保证工作目录有 `.env`，否则 `AMADEUS_BACKEND_TOKEN`/`AMADEUS_BACKEND_INSTANCE_NONCE/AUTH_MODE` 会因配置不完整而启动失败。
3. **真实模型链路**：本次未加载 GPT-SoVITS / Qwen3-ASR 权重，因此"说话/打断/字幕/麦克风"只验证到通道与契约层，没有实机听感。
4. **任务管理器无 `wallpaper64` / `webwallpaper64`**：代码路径上 companion 下不会创建壁纸会话或托盘，但没有实机跑过 Electron 启动，需按第 4 节第 1、4 步确认。
5. **卡置顶/拖动/口型观感**：Tk 卡本身未改动窗口属性（`-topmost`、`-alpha .88`、拖动），本次只验证了它能真实开窗并响应协议。

## 6. 未做（按任务书禁止项）

- 未实现阅读器解析、漫画 OCR。
- 未设计长期记忆 schema，未读写 `memory.jsonl` / `memory.sqlite3`。
- 未改 `memory/`、ReaderAdapter、SpoilerGuard。
- 未复制任何来源不明代码；沿用仓库自有 Tk 卡与既有 bridge/协议。
