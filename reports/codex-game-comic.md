# Codex — Game / Comic Companion 核心集成报告

日期：2026-10-01
分支：`integration/companion-reading-memory`

## 已完成

### Game Companion

- `server/handlers/vn_player_handler.py` 新增 `activity()`，把 VN/Galgame hook 最近活动暴露为只读上下文。
- `server/app.py` 新增 `_game_companion_context`，把当前游戏名、最近 hook 台词/反应注入主 Chat 的 `extra_context`。
- `/companion/scene/windows`、`/bind`、`/capture`、`/status`、`/close` 已接入通用视觉窗口。

### Comic Companion

- 浏览器扩展 `browser-extension/amadeus-reading-bridge`：
  - 新增“开始看漫画”按钮。
  - `content.js` 抓当前视口大图、canvas、章节标题和下一章链接。
  - 图片优先转 data URL，跨域失败保留图片 URL。
  - `background.js` POST 到 `127.0.0.1:17878/comic/chapter`。
- 后端：
  - `core/reading/server.py` 新增 `/comic/chapter`，body 上限 32MB。
  - `CompanionRuntime` 把当前章图片均匀抽样、拼图、调用 Qwen-VL 生成章摘要。
  - 章摘要写入 SceneStore 的 `last_capture`。
  - 下一章只写入 `future_ref`，`context_block` 输出 `future_content=marked_but_not_loaded`，不加载下一章内容。
  - 同章图片 hash 缓存，重复点击秒回。

## 测试结果

```text
tests/test_companion_scenes.py
tests/test_companion_comic_chapter.py
tests/test_companion_scene_robustness.py
21 passed
```

- Python 语法检查通过。
- 扩展 JS `node --check` 通过。
- DSH 的 robustness 测试已合并，修复了 `turns` 非列表的损坏 JSON 边界。

## 真实端到端

请求：`POST http://127.0.0.1:17878/comic/chapter`，1 张 1200×800 截图，默认 Qwen-VL。

- 首次真实摘要：**12.86 秒**，返回 868 字章摘要。
- 第二次同章：**0.024 秒**，`cached: true`。
- `runtime/companion/scene.json` 已写入 `kind=comic`、章节标题、摘要和 `future_ref`。
- 下一章 URL 未进入 `context_block`，只显示 future 标记。

## 速度说明

- 首次延迟主要是 Qwen-VL 远程推理；抓图、本地 POST、拼图、缓存都很快。
- 已加入优化开关：
  - `AMADEUS_COMIC_VL_MODEL` 可切到更快的 Qwen-VL 模型。
  - 章摘要 prompt 限制 400 字，`max_tokens=600`。
  - 同章缓存命中后 0.02 秒级。

## 失败点与处理

- 第一次 `/comic/chapter` 出现连接重置。
- 给接收端加最外层异常捕获和 traceback 后重试成功，未再复现。
- 仍需 WorkBuddy 在真实漫画站点做黑盒验收。

## 剩余

- WorkBuddy：真实漫画章节页“开始看漫画”黑盒验收。
- Game Companion：真实 Galgame hook 运行时的主 Chat 验收。
- 速度：如首次仍偏慢，设置更快的 `AMADEUS_COMIC_VL_MODEL`，或先返回本地抓图成功提示。

## Game Companion 卡片入口（138060e）

- 角色卡右上角新增 game 按钮（手柄图标）。
- 点击逻辑：
  - 查询 `vn.launch.status`；
  - active/starting 时调用 `vn.launch.stop`；
  - idle/error 时读取 `vn.launch.profiles` 并弹出 profile 列表。
- 选择 profile 后调用 `vn.launch.start`，传 `launchOverlay=false` 和 `overlayUrl=http://127.0.0.1:8788/reaction`，复用现有 Companion 卡片，不再抢 8788。
- `server/vn_launch_manager.py` 支持 external overlay URL，把 VN runtime 的 overlay_url 指向现有卡片。
- 控制通道 `VNOverlayControls` 新增通用 request/response callback。

实测：

```text
vn.launch.profiles -> 0 个（当前机器未保存 VN/Galgame profile）
vn.launch.status -> idle
相关测试 -> 38 passed
```
