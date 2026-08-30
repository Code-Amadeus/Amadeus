# Desktop Continuous Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user minimize Electron and conduct a basic multi-turn voice conversation through the Lively Wallpaper, with a 60-second hot window, desktop status/user subtitles, playback-synchronized assistant subtitles, exact stop commands, and no idle work panel.

**Architecture:** Extend the official Wake/ASR/Chat/TTS path instead of adding another voice state machine. `AsrHandler` remains the microphone and deadline owner; it emits richer `asr.status` snapshots, while the existing Wallpaper bridge replays those snapshots and a small pure JavaScript reducer maps them to the Pixi desktop UI.

**Tech Stack:** Python 3.12, asyncio, pytest 8.4, Electron 44, TypeScript 5.6, browser JavaScript, PixiJS, Lively Wallpaper.

**Spec:** `docs/superpowers/specs/2026-08-30-desktop-continuous-voice-design.md`

## Global Constraints

- Preserve the pre-feature tag `archive/pre-desktop-voice-20260830` at commit `6d0bebf62a4d8c3c21eaac24d737a07581de3de1`.
- Reuse the existing SenseVoice Wake, Qwen3-ASR conversation, GPT-SoVITS TTS, AEC/barge-in, Chat Session, Wallpaper bridge, and Lively host.
- Do not merge unrelated `origin/main` changes.
- Do not add a second voice state machine, microphone owner, desktop Chat frontend, system tray, or dependency.
- `AsrHandler` owns the real hot-window deadline; the Wallpaper countdown is presentation only.
- Exact stop commands never reach `ASR_RECOGNIZED`, `chat_h.send_text`, or Chat history.
- Follow TDD: each production change begins with the listed failing test and the expected failure must be observed.
- Keep each implementation commit limited to the task named below.

## File Structure

- Create `server/desktop_voice.py`: normalize and classify the three exact desktop voice stop commands; no session state.
- Modify `server/handlers/asr_handler.py`: centralize final-recognition dispatch, attach hot-window presentation timestamps, and accept an explicit stop reason.
- Modify `server/app.py`: route Wake control payloads, publish the `thinking` presentation state, and leave normal Chat routing unchanged.
- Create `tests/test_desktop_voice_routing.py`: stop-command, dispatch-order, and status-payload tests.
- Create `render/web/wallpaper_voice_state.js`: pure browser-side reducer and countdown calculator.
- Modify `render/web/wallpaper_scene.js`: render the voice status marker and coordinate user/assistant subtitle ownership.
- Modify `render/web/wallpaper.html` and `render/web/wallpaper_engine.html`: load the reducer before `wallpaper_scene.js`.
- Modify `wallpaper/wallpaper_engine_bridge.py`: include the new reducer in the wallpaper asset revision.
- Create `tests/test_wallpaper_voice_state.py`: execute the reducer in Node and verify the loader/asset contract.
- Modify `server/handlers/wallpaper_handler.py`: collapse truly empty canvas projections before they reach the Electron Slice.
- Create `tests/test_wallpaper_idle_slice.py`: verify idle collapse and genuine work/attention preservation.
- Modify `docs/wake_word_setup.md`: document the final desktop conversation lifecycle and exit commands.

---

### Task 1: Exact Stop Commands and Wake Recognition Dispatch

**Files:**
- Create: `server/desktop_voice.py`
- Modify: `server/handlers/asr_handler.py:164-170, 343-398`
- Modify: `server/app.py:1219-1249, 1311-1319`
- Test: `tests/test_desktop_voice_routing.py`

**Interfaces:**
- Produces: `normalize_desktop_voice_command(text: object) -> str`
- Produces: `is_desktop_voice_exit_command(text: object) -> bool`
- Produces: `AsrHandler._dispatch_recognized(text: str) -> None`
- Changes: `AsrHandler.stop_listening(reason: str = "manual_stop") -> dict[str, Any]`
- Consumes: existing `AsrHandler._on_recognized` callback and `server.event_bus.bus.emit`.

- [ ] **Step 1: Write failing stop-command and dispatch tests**

Add these tests to `tests/test_desktop_voice_routing.py`:

```python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.desktop_voice import is_desktop_voice_exit_command
from server.handlers.asr_handler import AsrHandler
from server.protocol import Method


@pytest.mark.parametrize("text", ["停止对话", "结束对话。", " 退出对话！ "])
def test_exact_desktop_voice_exit_commands(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is True


@pytest.mark.parametrize("text", ["如何停止对话", "不要结束对话", "退出对话模式怎么用"])
def test_exit_words_inside_a_sentence_are_normal_chat(text: str) -> None:
    assert is_desktop_voice_exit_command(text) is False


@pytest.mark.asyncio
async def test_wake_exit_command_reaches_host_callback_but_not_public_transcript(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    callback = AsyncMock()
    handler = AsrHandler()
    handler._source = "wake"
    handler._on_recognized = callback
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._dispatch_recognized("停止对话")

    callback.assert_awaited_once()
    assert callback.await_args.args[0]["control"] == "stop"
    assert not any(method == Method.ASR_RECOGNIZED for method, _ in emitted)


@pytest.mark.asyncio
async def test_normal_wake_text_is_published_and_callback_runs_once(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    callback = AsyncMock()
    handler = AsrHandler()
    handler._source = "wake"
    handler._on_recognized = callback
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._dispatch_recognized("今天过得怎么样")

    callback.assert_awaited_once()
    recognized = [payload for method, payload in emitted if method == Method.ASR_RECOGNIZED]
    assert len(recognized) == 1
    assert recognized[0]["text"] == "今天过得怎么样"


@pytest.mark.asyncio
async def test_stop_listening_does_not_cancel_its_current_listener_task(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._active = True
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 160.0
    handler._listen_task = asyncio.current_task()
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler.stop_listening("voice_stop_command")

    assert handler._listen_task is None
    assert emitted[-1][1]["status"] == "idle"
    assert emitted[-1][1]["reason"] == "voice_stop_command"
```

- [ ] **Step 2: Run the tests and verify the RED state**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_voice_routing.py -q
```

Expected: collection fails because `server.desktop_voice` and
`AsrHandler._dispatch_recognized` do not exist.

- [ ] **Step 3: Add the pure exit-command classifier**

Create `server/desktop_voice.py` with this bounded implementation:

```python
from __future__ import annotations


_EXIT_COMMANDS = frozenset({"停止对话", "结束对话", "退出对话"})
_TERMINAL_PUNCTUATION = "。！？!?，,；;：:"


def normalize_desktop_voice_command(text: object) -> str:
    return str(text or "").strip().rstrip(_TERMINAL_PUNCTUATION).strip().casefold()


def is_desktop_voice_exit_command(text: object) -> bool:
    return normalize_desktop_voice_command(text) in _EXIT_COMMANDS
```

- [ ] **Step 4: Centralize final recognition dispatch in `AsrHandler`**

Add `_dispatch_recognized` and replace the inline `ASR_RECOGNIZED`/callback
block in `_listen_loop` with `await self._dispatch_recognized(text)`:

```python
async def _dispatch_recognized(self, text: str) -> None:
    from server.desktop_voice import is_desktop_voice_exit_command

    payload: dict[str, Any] = {"text": text, "is_final": True}
    if self._source:
        payload["source"] = self._source
    if self._wake_payload:
        payload["wake"] = self._wake_payload
    if self._source_payload:
        payload["source_payload"] = self._source_payload
    if self._source == "wake" and is_desktop_voice_exit_command(text):
        payload["control"] = "stop"
    else:
        await bus.emit(Method.ASR_RECOGNIZED, payload)
    if self._on_recognized is not None:
        result = self._on_recognized(payload)
        if hasattr(result, "__await__"):
            await result
```

Change `stop_listening` so the caller can preserve the semantic reason:

```python
async def stop_listening(self, reason: str = "manual_stop") -> dict[str, Any]:
    self._active = False
    self._one_shot = False
    listen_task = self._listen_task
    self._listen_task = None
    if listen_task is not None and listen_task is not asyncio.current_task():
        listen_task.cancel()
    await self._finish_listening(reason)
    return {"status": "stopped"}
```

The current-task check is required because an exact stop command is routed
from inside `_listen_loop`; cancelling that task before `_finish_listening`
would interrupt the transition back to passive Wake. Keep `_stop()` calling
`stop_listening()` with its default.

- [ ] **Step 5: Route stop control in `server/app.py` without starting Chat**

In `_handle_asr_recognized`, branch before `_send_wake_text`:

```python
if payload.get("control") == "stop":
    logger.info("desktop voice stop command received; returning to passive wake")
    await asr_h.stop_listening("voice_stop_command")
    return
```

In `_start_asr_from_wake`, check an inline `command_text` with
`is_desktop_voice_exit_command` before publishing `ASR_RECOGNIZED`. If it is an
exit command, do not call `_send_wake_text` and do not open a new Qwen hot
window. The Wake service is still active at this point, so return directly.

- [ ] **Step 6: Run focused and coordinator tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_voice_routing.py tests\test_turn_coordinator.py -q
```

Expected: all tests pass, normal Wake text emits once, and exact exit commands
remain host-only.

- [ ] **Step 7: Commit Task 1**

```powershell
git add server/desktop_voice.py server/handlers/asr_handler.py server/app.py tests/test_desktop_voice_routing.py
git commit -m "feat: route desktop voice stop commands"
```

---

### Task 2: Hot-window Presentation Snapshots

**Files:**
- Modify: `server/handlers/asr_handler.py:201-225, 264-318, 343-398`
- Modify: `server/app.py:1265-1310, 1361-1397`
- Modify: `tests/test_desktop_voice_routing.py`

**Interfaces:**
- Produces: `AsrHandler._status_payload(status: str, **extra: Any) -> dict[str, Any]`
- Extends: existing `asr.status` payload with `text`, `awake_remaining`, and `awake_deadline_ms`.
- Preserves: `AsrHandler._awake_until` as the only real deadline authority.

- [ ] **Step 1: Write failing hot-window payload tests**

Append:

```python
@pytest.mark.asyncio
async def test_awake_status_contains_wall_clock_deadline(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 112.0
    monkeypatch.setattr("server.handlers.asr_handler.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("server.handlers.asr_handler.time.time", lambda: 1_000.0)
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler._emit_listening_status()

    payload = emitted[-1][1]
    assert payload["status"] == "awake"
    assert payload["awake_remaining"] == pytest.approx(12.0)
    assert payload["awake_deadline_ms"] == 1_012_000


@pytest.mark.asyncio
async def test_turn_complete_resets_and_publishes_full_hot_window(monkeypatch) -> None:
    emitted: list[tuple[object, dict]] = []

    async def capture(method, payload):
        emitted.append((method, payload))

    handler = AsrHandler()
    handler._active = True
    handler._source = "wake"
    handler._awake_seconds = 60.0
    handler._awake_until = 120.0
    handler._waiting_turn_complete = True
    monkeypatch.setattr("server.handlers.asr_handler.time.monotonic", lambda: 200.0)
    monkeypatch.setattr("server.handlers.asr_handler.time.time", lambda: 1_000.0)
    monkeypatch.setattr("server.handlers.asr_handler.bus.emit", capture)

    await handler.notify_turn_complete("playback")

    payload = emitted[-1][1]
    assert payload["status"] == "turn_complete"
    assert payload["awake_remaining"] == pytest.approx(60.0)
    assert payload["awake_deadline_ms"] == 1_060_000
```

- [ ] **Step 2: Run and verify the tests fail on missing deadline fields**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_voice_routing.py -q
```

Expected: the new assertions fail because current `asr.status` payloads have
no `awake_deadline_ms`, and `turn_complete` lacks remaining time.

- [ ] **Step 3: Add one status-payload builder owned by `AsrHandler`**

Implement:

```python
def _status_payload(self, status: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "source": self._source or "",
        **extra,
    }
    if self._is_awake_session() and status in {"awake", "listening", "no_speech", "turn_complete"}:
        remaining = max(0.0, self._awake_until - time.monotonic())
        payload["awake_remaining"] = remaining
        payload["awake_deadline_ms"] = int((time.time() + remaining) * 1000)
    return payload
```

Use this builder for `_emit_listening_status`, `no_speech`, and
`notify_turn_complete`. Keep paused/thinking states without an active
countdown. Keep `_finish_listening`'s explicit idle payload based on the
`session_info` captured before `_clear_session_state`; calling the builder
after clearing would lose the Wake source.

- [ ] **Step 4: Publish recognized and thinking presentation states**

In `_dispatch_recognized`, emit the final user text before the public
`ASR_RECOGNIZED` event for normal Wake speech:

```python
if self._source == "wake" and "control" not in payload:
    await bus.emit(Method.ASR_STATUS, self._status_payload("recognized", text=text))
```

In `_send_wake_text`, after empty/prompt-leak checks and before speculative
resolution or `chat_h.send_text`, publish:

```python
await bus.emit(
    Method.ASR_STATUS,
    {"status": "thinking", "source": "wake", "text": text},
)
```

Do not change `chat_h.send_text` arguments or Session selection.

- [ ] **Step 5: Keep recoverable Chat errors in the official release path**

Retain `_handle_wake_chat_finished` and `_release_asr_after_playback_idle` as
the turn-release owners. Ensure their `notify_turn_complete` calls now publish
the full reset deadline through `_status_payload`; do not add a retry loop or a
second timer.

- [ ] **Step 6: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_voice_routing.py tests\test_turn_coordinator.py tests\test_interrupt_flow.py -q
```

Expected: all pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add server/handlers/asr_handler.py server/app.py tests/test_desktop_voice_routing.py
git commit -m "feat: publish desktop voice hot-window state"
```

---

### Task 3: Wallpaper Voice State, Status Marker, and Dual Subtitles

**Files:**
- Create: `render/web/wallpaper_voice_state.js`
- Create: `tests/test_wallpaper_voice_state.py`
- Modify: `render/web/wallpaper_scene.js:75-180, 1688-1768, 1950-1985, 2105-2160, 2388-2420`
- Modify: `render/web/wallpaper.html`
- Modify: `render/web/wallpaper_engine.html`
- Modify: `wallpaper/wallpaper_engine_bridge.py:57-69`

**Interfaces:**
- Produces: `window.AmadeusWallpaperVoiceState.initial()`
- Produces: `window.AmadeusWallpaperVoiceState.reduce(previous, payload) -> state`
- Produces: `window.AmadeusWallpaperVoiceState.remainingSeconds(state, nowMs) -> number | null`
- Consumes: existing `AmadeusWallpaperRuntime.setAsrStatus`, `setSpeaking`, and `setSubtitle` calls.

- [ ] **Step 1: Write a failing executable reducer contract**

Create `tests/test_wallpaper_voice_state.py`. Run the new JavaScript with Node's
`vm` module and assert this sequence:

```javascript
let state = api.initial();
state = api.reduce(state, {
  status: "awake",
  source: "wake",
  awake_deadline_ms: 61000
});
assert.equal(state.phase, "listening");
assert.equal(api.remainingSeconds(state, 1000), 60);

state = api.reduce(state, {
  status: "recognized",
  source: "wake",
  text: "你好"
});
assert.equal(state.phase, "recognized");
assert.equal(state.userText, "你好");
assert.equal(api.remainingSeconds(state, 2000), null);

state = api.reduce(state, { status: "thinking", source: "wake", text: "你好" });
assert.equal(state.phase, "thinking");

state = api.reduce(state, { status: "turn_complete", source: "wake", awake_deadline_ms: 65000 });
assert.equal(state.phase, "listening");
assert.equal(api.remainingSeconds(state, 5000), 60);

state = api.reduce(state, { status: "idle", source: "wake", reason: "awake_timeout" });
assert.equal(state.phase, "idle");
assert.equal(state.userText, "");
```

The Python test must also assert that both wallpaper HTML loaders include
`/render/web/wallpaper_voice_state.js` immediately before
`/render/web/wallpaper_scene.js`, and `_WALLPAPER_CLIENT_ASSETS` includes the
new file.

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_wallpaper_voice_state.py -q
```

Expected: fail because the reducer file and loader entries do not exist.

- [ ] **Step 3: Implement the pure reducer**

Create a dependency-free IIFE that exports only the state contract:

```javascript
(function (global) {
  "use strict";

  function initial() {
    return { phase: "idle", active: false, userText: "", deadlineMs: 0, error: "" };
  }

  function reduce(previous, payload) {
    const current = previous || initial();
    const value = payload && typeof payload === "object" ? payload : {};
    if (value.source && value.source !== "wake") return current;
    const status = String(value.status || "").toLowerCase();
    if (["idle", "unloaded"].includes(status)) return initial();
    if (status === "error") {
      return { ...current, active: true, phase: "error", deadlineMs: 0, error: String(value.error || "语音不可用") };
    }
    if (["awake", "listening", "no_speech", "turn_complete"].includes(status)) {
      return { ...current, active: true, phase: "listening", deadlineMs: Number(value.awake_deadline_ms) || 0, error: "" };
    }
    if (status === "recognized") {
      return { ...current, active: true, phase: "recognized", userText: String(value.text || ""), deadlineMs: 0, error: "" };
    }
    if (["thinking", "waiting_turn_complete"].includes(status)) {
      return { ...current, active: true, phase: "thinking", userText: String(value.text || current.userText), deadlineMs: 0, error: "" };
    }
    return current;
  }

  function remainingSeconds(state, nowMs) {
    if (!state || state.phase !== "listening" || !(state.deadlineMs > 0)) return null;
    return Math.max(0, Math.ceil((state.deadlineMs - nowMs) / 1000));
  }

  global.AmadeusWallpaperVoiceState = { initial, reduce, remainingSeconds };
})(window);
```

- [ ] **Step 4: Add the Pixi status marker and subtitle ownership**

In `wallpaper_scene.js`, add one `wallpaperVoicePresentation` object that:

- Creates one Pixi container and one small text label in `init`.
- Places it at the top-right of `desktopScene._crtBounds` in `layout`.
- Calls the pure reducer in `setAsrStatus`.
- Displays `READY`, `已唤醒`, `正在听 · Ns`, `思考中`, `正在说话`, or a
  concise error.
- Calls `wallpaperSubtitle.setText("你：" + state.userText)` for recognized and
  thinking phases.
- Lets existing TTS `setSubtitle` replace that with `助手：<sentence>` only
  while the voice presentation is active.
- Uses the existing ticker to refresh the visible seconds; it never emits an
  event when the displayed countdown reaches zero.
- Clears the current-turn subtitle on `idle` after the existing brief display
  delay, without changing Chat history.

Wire `desktopScene.setAsrStatus`, `desktopScene.setSpeaking`, resize/layout, and
the existing `AmadeusWallpaperRuntime.setSubtitle` entry point to this object.
Do not change mouth or expression timing.

- [ ] **Step 5: Load and revision the new browser asset**

Insert `/render/web/wallpaper_voice_state.js` before `wallpaper_scene.js` in
both wallpaper loaders. Add the same path to `_WALLPAPER_CLIENT_ASSETS` so a
running Lively wrapper detects the new client revision.

- [ ] **Step 6: Run focused browser/bridge tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_wallpaper_voice_state.py tests\test_wallpaper_asset_revision.py tests\test_wallpaper_bridge_observability.py -q
```

Expected: all pass, including Node execution of the reducer.

- [ ] **Step 7: Commit Task 3**

```powershell
git add render/web/wallpaper_voice_state.js render/web/wallpaper_scene.js render/web/wallpaper.html render/web/wallpaper_engine.html wallpaper/wallpaper_engine_bridge.py tests/test_wallpaper_voice_state.py
git commit -m "feat: show continuous voice state on wallpaper"
```

---

### Task 4: Keep the Electron Work Slice Collapsed When Idle

**Files:**
- Modify: `server/handlers/wallpaper_handler.py:243-276`
- Create: `tests/test_wallpaper_idle_slice.py`

**Interfaces:**
- Produces: `WallpaperHandler._collapse_empty_canvas(payload: dict[str, Any]) -> dict[str, Any]`
- Preserves: genuine canvas content, selected WorkItem context, permission requests, and the independent `setAttention` transport.

- [ ] **Step 1: Write failing idle/active projection tests**

Use a fake host with `set_canvas` capture and a projector callback. Assert:

```python
from __future__ import annotations

from server.handlers.wallpaper_handler import WallpaperHandler


def configured_handler(projected: dict) -> tuple[WallpaperHandler, list[dict]]:
    calls: list[dict] = []

    class Host:
        @staticmethod
        def set_canvas(payload: dict) -> None:
            calls.append(payload)

    handler = WallpaperHandler()
    handler._wallpaper_host = Host()
    handler._canvas_projector = lambda _payload: dict(projected)
    return handler, calls


def test_empty_canvas_projection_collapses_slice() -> None:
    handler, calls = configured_handler(projected={})
    assert handler._apply_canvas({}) is True
    assert calls[-1] == {"clear": True, "visible": False, "expanded": False}


def test_selected_work_projection_remains_visible() -> None:
    payload = {
        "phase": "Work",
        "title": "Running task",
        "workContext": {"workItemId": "work-1"},
    }
    handler, calls = configured_handler(projected=payload)
    assert handler._apply_canvas({}) is True
    assert calls[-1]["workContext"]["workItemId"] == "work-1"
    assert calls[-1].get("visible") is not False


def test_permission_projection_remains_visible() -> None:
    payload = {"permissionRequest": {"id": "permission-1"}}
    handler, calls = configured_handler(projected=payload)
    assert handler._apply_canvas({}) is True
    assert calls[-1]["permissionRequest"]["id"] == "permission-1"
```

Also retain the existing attention test in `tests/test_attention_slice.py` as
the proof that a pending attention request expands through `setAttention`
independently of canvas collapse.

- [ ] **Step 2: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_wallpaper_idle_slice.py tests\test_attention_slice.py -q
```

Expected: the empty projection test fails because the current handler sends
`{}`, which `crt_canvas_surface.js` interprets as an expanded manual canvas.

- [ ] **Step 3: Add a narrow empty-projection rule**

Implement a helper that returns the explicit clear payload only when there is
no selected work context, permission request, artifact/report/diff/browser
content, title/lead, phase, signals, or explicit visible/open request. Apply it
after `self._canvas_projector` and before assigning `_last_canvas_payload`:

```python
_CANVAS_CONTENT_KEYS = frozenset({
    "artifact", "reportView", "reportMarkdown", "diff", "diffView", "html",
    "url", "screenshot", "permissionRequest", "workContext", "title", "lead",
    "phase", "signals",
})


@classmethod
def _collapse_empty_canvas(cls, payload: dict[str, Any]) -> dict[str, Any]:
    output = dict(payload or {})
    if any(output.get(key) for key in _CANVAS_CONTENT_KEYS):
        return output
    return {"clear": True, "visible": False, "expanded": False}
```

Do not treat a task rail with no selected WorkItem as active content. Do not
change `_apply_attention_snapshot`; its pending request remains independently
visible and actionable.

- [ ] **Step 4: Run Slice and canvas regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_wallpaper_idle_slice.py tests\test_attention_slice.py tests\test_canvas_presentation.py tests\test_wallpaper_asset_revision.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit Task 4**

```powershell
git add server/handlers/wallpaper_handler.py tests/test_wallpaper_idle_slice.py
git commit -m "fix: collapse idle wallpaper work slice"
```

---

### Task 5: Documentation, Full Verification, and Real-device Acceptance

**Files:**
- Modify: `docs/wake_word_setup.md`
- Verify: Python tests, Electron build, local GPU runtime, Lively desktop output.

**Interfaces:**
- Documents: the existing `WAKE_ENABLED`, `WAKE_AUTO_SEND_TO_CHAT`,
  `WAKE_AWAKE_SECONDS`, and `WAKE_AUTO_START_WITH_WALLPAPER` settings.
- Documents: exact exit commands and manual Electron minimization.

- [ ] **Step 1: Update the Wake guide**

Add a `Desktop continuous conversation` section that states:

```text
With Wallpaper running, a Wake phrase opens the Qwen conversation recognizer.
Final speech is sent to the current Chat Session automatically. After physical
assistant playback completes, the listener remains hot for 60 seconds; each
completed turn resets that window. The Electron window may be minimized and is
not restored by Wake. Say exactly “停止对话”, “结束对话”, or “退出对话” to leave
the hot window without adding that command to Chat history.
```

Explain that `WAKE_AUTO_SEND_TO_CHAT=true` is required and that Wallpaper owns
the Wake start/stop lifecycle when `WAKE_AUTO_START_WITH_WALLPAPER=true`.

- [ ] **Step 2: Run the focused voice and Wallpaper suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_desktop_voice_routing.py tests\test_wallpaper_voice_state.py tests\test_wallpaper_idle_slice.py tests\test_turn_coordinator.py tests\test_interrupt_flow.py tests\test_attention_slice.py tests\test_wallpaper_asset_revision.py tests\test_wallpaper_bridge_observability.py -q
```

Expected: all pass with no warnings introduced by this feature.

- [ ] **Step 3: Run the complete Python suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass. If a pre-existing environment-dependent test fails,
record its exact command/output and prove the focused feature suite remains
green before deciding whether the failure is in scope.

- [ ] **Step 4: Build Electron**

```powershell
Set-Location D:\Amadeus\electron
npm run build
Set-Location D:\Amadeus
```

Expected: TypeScript and Vite build complete successfully.

- [ ] **Step 5: Commit the documentation**

```powershell
git add docs/wake_word_setup.md
git commit -m "docs: explain desktop continuous voice"
```

- [ ] **Step 6: Start the configured runtime and execute the real-device path**

Start with the machine-specific launcher:

```powershell
Set-Location D:\Amadeus
.\runtime\run_electron_sm120.bat
```

Then verify in order:

1. Start Wallpaper and manually minimize Electron.
2. Speak the configured Wake phrase; observe `已唤醒` then `正在听 · 60s`.
3. Ask a first question; observe `你：...`, `思考中`, then playback-synchronized
   `助手：...` with mouth movement.
4. Ask a second question without another Wake phrase.
5. Interrupt the second answer; confirm old audio stops and listening resumes.
6. Ask a third question and receive the answer.
7. Say `停止对话`; confirm the surface returns to `READY` and does not send a
   new Chat turn.
8. Restore Electron and confirm the three real questions/answers exist in the
   current Session while the exit command does not.
9. Confirm the green work panel stays collapsed during ordinary conversation
   and still appears for a real WorkItem or attention request.

- [ ] **Step 7: Verify repository state and rollback marker**

```powershell
git status --short
git log --oneline --decorate -8
git show --no-patch archive/pre-desktop-voice-20260830
```

Expected: the worktree is clean, the feature commits are visible, and the
pre-feature archive tag still resolves to `6d0bebf`.
