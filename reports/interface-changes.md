# 接口变更记录 — DSH / feature/companion-only

本分支（Companion-only 启动、壁纸旁路、本地语音接线）对共享接口的改动。冻结接口未变，
只新增了一个启动模式入口和两个本地 HTTP 端点。

## 新增

### 1. 启动模式 `companion`

- `electron/src/main/startupMode.ts`
  - `resolveStartupMode(args, env, platform, savedMode): 'window' | 'wallpaper' | 'companion'`
  - `isCompanionOnlyStartup(...): boolean`
  - `isWallpaperStartup(...)` 保持原有签名与判定，内部改用 `resolveStartupMode`。
- 入口：`--companion`、`AMADEUS_COMPANION=1`。优先级高于 Windows 壁纸偏好。
- 后端入口：`python -m server.app --port <port> --companion`。

### 2. Companion 卡控制端点（仅 `--companion` 启动存在，其余启动返回 404）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/companion/card/status` | — | `{ok, card:{status,running,owned,pid,visible,url,healthUrl}}` |
| POST | `/companion/card/visibility` | `{"visible": bool}` | `{ok, card:{...}}` |
| POST | `/companion/card/focus` | `{}` | `{ok, card:{...}}`，显示并置顶已有卡（重复启动用） |
| POST | `/companion/card-close` | `{}` | `{ok}`，随后关闭持卡后端 |

鉴权与现有一致：`X-Amadeus-Token`（`AMADEUS_BACKEND_AUTH_MODE=required` 时）+
本地 Origin 校验。

### 3. Tk 卡关闭上报

- `render/vn_overlay_close.py`：`post_close(on_close, backend_url, *, post=None)`。
- `PortraitOverlayTk(on_close=...)` 新参数，默认 `exit`（VN 行为不变）。
- `tools/vn_portrait_overlay_lite.py --on-close {exit,card-close}`。

### 4. 语音落卡

- `server/companion_runtime.py` 的 `set_default_overlay_url` / `default_overlay_url`。
- `server/vn_tts_bridge.py`：`submit_vn_tts` 构造 metadata 时，`overlay_url` 为空则回落到
  Companion 卡端点；payload 显式给出 `overlay_url` 时仍以 payload 为准。

## 未变（冻结项）

- 阅读事件格式 `reading.selection`：本分支不读写。
- Companion 事件通道仍是既有 `/reaction`：`{source, sentence_id, speaking, display_text, text,
  emotion, duration_ms}`；`/visibility`：`{visible: bool}`。
- 记忆操作与记忆记录字段：本分支不涉及，不读写 `memory.jsonl` / `memory.sqlite3`。
- `wallpaper.*` / `vn.*` WS 方法与 `wallpaper/canvas-action` 协议不变。
- `createElectronSliceWindow` / `createElectronCanvasWindow` 的既有调用方与行为不变
  （仅在 companion 模式下短路）。

## 给 Codex 侧的契约

- 阅读会话若要在 Companion 卡上出声/出字幕：沿用现有 `overlay_url` 注入方式，
  或在 companion 模式下不传 `overlay_url`，由卡端点兜底。
- 若需在阅读会话里显式控制 Companion 卡：POST `/companion/card/visibility`。
- 不要在 companion 模式里依赖 WorkItem / AUIP 上下文；该模式已关闭这两条通道。
