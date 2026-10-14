"""External text sources for VN sessions.

Sources translate their own transport into the existing ``vn.line`` input.
They do not decide what a line means to the VN runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import psutil

from server.vn_profiles import validate_luna_ws_url

VNLine = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
StatusChanged = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]

logger = logging.getLogger(__name__)


class VNTextSourceAdapter(Protocol):
    async def start(self, profile: dict[str, Any], params: dict[str, Any], *, target_pid: int | None) -> None: ...
    async def stop(self) -> None: ...
    def status(self) -> tuple[dict[str, Any], dict[str, Any]]: ...


class AgentVNTextSource:
    """Own the installed 0xDC00 Agent process and its text transport."""

    def __init__(self, on_line: VNLine, on_status: StatusChanged) -> None:
        self._on_line = on_line
        self._on_status = on_status
        self._proc: subprocess.Popen[Any] | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_url = ""
        self._line_count = 0
        self._hook: dict[str, Any] = {"status": "not_started", "pid": None, "helper": ""}
        self._bridge: dict[str, Any] = {"status": "not_started", "lineCount": 0, "source": "agent_websocket"}

    def status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._proc is not None:
            self._hook["status"] = "running" if self._proc.poll() is None else "exited"
            self._hook["pid"] = self._proc.pid
        return dict(self._hook), dict(self._bridge)

    async def _publish(self) -> None:
        await self._on_status(*self.status())

    async def start(self, profile: dict[str, Any], params: dict[str, Any], *, target_pid: int | None) -> None:
        attach = _truthy(params.get("attachHook") or params.get("attach_hook"))
        open_ui = _truthy(params.get("openHookAgent") or params.get("open_hook_agent"))
        bridge = _truthy(params.get("bridgeText", True))
        if attach or open_ui:
            await self._launch_agent(profile, target_pid=target_pid, attach=attach)
        else:
            self._hook = {"status": "manual_required", "pid": None, "helper": profile.get("hookHelper") or ""}
            await self._publish()
        if bridge:
            self._start_bridge(profile, params)
            await self._publish()

    async def stop(self) -> None:
        await self._stop_task("_ws_task")
        if self._proc is not None and self._proc.poll() is None:
            await _terminate_proc(self._proc)
        self._proc = None
        self._hook = {"status": "not_started", "pid": None, "helper": ""}
        self._bridge = {"status": "not_started", "lineCount": self._line_count, "source": "agent_websocket"}
        await self._publish()

    async def _launch_agent(self, profile: dict[str, Any], *, target_pid: int | None, attach: bool) -> None:
        executable = Path(str(profile.get("agentExe") or ""))
        script = Path(str(profile.get("hookHelper") or ""))
        if not executable.is_file():
            raise FileNotFoundError(f"VN hook agent not found: {executable}")
        if not script.is_file():
            raise FileNotFoundError(f"VN hook script not found: {script}")
        if attach and target_pid is None:
            raise RuntimeError("Cannot attach VN hook: launch the game first or start it manually.")
        if attach and await _matching_agent_pids(executable, script):
            raise RuntimeError("A matching 0xDC00 Agent is already running. Close it before starting this VN source.")
        args = [str(executable)]
        if attach:
            args.append(_agent_switch("pname", str(target_pid)))
        args.append(_agent_switch("script", str(script)))
        self._proc = _spawn(args, cwd=executable.parent, hidden=attach)
        self._hook = {
            "status": "running" if attach else "manual_ui_open",
            "pid": self._proc.pid,
            "helper": str(script),
        }
        if not attach:
            self._hook["mode"] = "manual"
        await self._publish()

    def _start_bridge(self, profile: dict[str, Any], params: dict[str, Any]) -> None:
        mode = str(params.get("bridgeMode") or params.get("bridge_mode") or profile.get("lineBridgeMode") or "websocket").strip().lower()
        if mode != "websocket":
            raise ValueError("Agent text input supports WebSocket only. Select websocket; clipboard input is not supported.")
        self._line_count = 0
        host = str(params.get("agentWsHost") or params.get("agent_ws_host") or profile.get("agentWsHost") or "127.0.0.1")
        port = _coerce_int(params.get("agentWsPort") or params.get("agent_ws_port") or profile.get("agentWsPort"), 9001)
        self._ws_url = f"ws://{host}:{port}"
        self._bridge = {"status": "connecting", "lineCount": 0, "source": "agent_websocket", "url": self._ws_url}
        self._ws_task = asyncio.create_task(self._websocket_loop())

    async def _stop_task(self, name: str) -> None:
        task = getattr(self, name)
        setattr(self, name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _websocket_loop(self) -> None:
        try:
            import websockets
        except Exception as exc:
            self._bridge = {"status": "error", "lineCount": self._line_count, "source": "agent_websocket", "error": str(exc)}
            await self._publish()
            return
        while True:
            try:
                self._bridge = {"status": "connecting", "lineCount": self._line_count, "source": "agent_websocket", "url": self._ws_url}
                await self._publish()
                async with websockets.connect(self._ws_url, open_timeout=2, ping_interval=20, ping_timeout=10) as ws:
                    self._bridge = {"status": "running", "lineCount": self._line_count, "source": "agent_websocket", "url": self._ws_url}
                    await self._publish()
                    async for raw in ws:
                        await self._receive_agent_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[VNSource] Agent WebSocket waiting: %s", exc)
                self._bridge = {"status": "waiting", "lineCount": self._line_count, "source": "agent_websocket", "url": self._ws_url, "error": str(exc)}
                await self._publish()
                await asyncio.sleep(1.0)

    async def _receive_agent_message(self, raw: Any) -> None:
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = str(data.get("type") or "")
        if kind not in {"copyText", "translate"}:
            return
        if kind == "translate" and not _truthy(os.environ.get("VN_AGENT_ACCEPT_TRANSLATE")):
            return
        sentence = str(data.get("sentence") or data.get("text") or data.get("content") or "")
        message_id = str(data.get("id") or "")
        for item in _prepare_incoming_items(sentence):
            hook_source = str(item["metadata"].get("source") or "")
            payload = {
                **item,
                "metadata": {
                    **item["metadata"],
                    "source": "agent_websocket",
                    "hook_source": hook_source,
                    "agent_message_type": kind,
                    "process_path": str(data.get("process_path") or ""),
                    "agent_message_id": message_id,
                },
            }
            await self._forward(payload, source="agent_websocket")

    async def _forward(self, payload: dict[str, Any], *, source: str) -> None:
        result = await self._on_line(payload)
        if isinstance(result, dict) and result.get("status") == "ignored":
            return
        line = result.get("line") if isinstance(result, dict) and isinstance(result.get("line"), dict) else {}
        self._line_count += 1
        self._bridge = {
            "status": "running",
            "lineCount": self._line_count,
            "source": source,
            "lastTextPreview": str(line.get("text") or payload["text"])[:80],
            "lastScriptId": str(line.get("script_id") or payload.get("script_id") or ""),
        }
        if source == "agent_websocket":
            self._bridge["url"] = self._ws_url
        await self._publish()


class LunaVNTextSource:
    """Consume LunaTranslator's original-text WebSocket without controlling Luna."""

    def __init__(self, on_line: VNLine, on_status: StatusChanged) -> None:
        self._on_line = on_line
        self._on_status = on_status
        self._task: asyncio.Task[None] | None = None
        self._url = ""
        self._count = 0
        self._bridge: dict[str, Any] = {"status": "not_started", "lineCount": 0, "source": "luna_original_text"}

    def status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return {
            "status": self._bridge.get("status", "not_started"),
            "pid": None,
            "helper": "LunaTranslator",
            "owned": False,
        }, dict(self._bridge)

    async def _publish(self) -> None:
        await self._on_status(*self.status())

    async def start(self, profile: dict[str, Any], params: dict[str, Any], *, target_pid: int | None) -> None:
        del profile, target_pid
        if _truthy(params.get("attachHook") or params.get("attach_hook") or params.get("openHookAgent") or params.get("open_hook_agent")):
            raise ValueError("Luna text source connects to an already configured LunaTranslator; it does not attach Agent.")
        self._url = str(params.get("lunaWsUrl") or params.get("luna_ws_url") or "").strip()
        validate_luna_ws_url(self._url)
        self._count = 0
        self._bridge = {"status": "connecting", "lineCount": 0, "source": "luna_original_text", "url": self._url}
        self._task = asyncio.create_task(self._loop())
        await self._publish()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._bridge = {"status": "not_started", "lineCount": self._count, "source": "luna_original_text"}
        await self._publish()

    async def _loop(self) -> None:
        try:
            import websockets
        except Exception as exc:
            self._bridge = {"status": "error", "lineCount": self._count, "source": "luna_original_text", "error": str(exc)}
            await self._publish()
            return
        while True:
            try:
                self._bridge = {"status": "connecting", "lineCount": self._count, "source": "luna_original_text", "url": self._url}
                await self._publish()
                async with websockets.connect(self._url, open_timeout=3, ping_interval=20, ping_timeout=10) as ws:
                    self._bridge = {"status": "running", "lineCount": self._count, "source": "luna_original_text", "url": self._url}
                    await self._publish()
                    async for raw in ws:
                        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
                        if not text.strip():
                            continue
                        result = await self._on_line({"text": text, "metadata": {"source": "luna_original_text"}})
                        if isinstance(result, dict) and result.get("status") == "ignored":
                            continue
                        line = result.get("line") if isinstance(result, dict) and isinstance(result.get("line"), dict) else {}
                        self._count += 1
                        self._bridge = {
                            "status": "running", "lineCount": self._count, "source": "luna_original_text",
                            "url": self._url, "lastTextPreview": str(line.get("text") or text)[:80],
                        }
                        await self._publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[VNSource] Luna original-text stream waiting: %s", exc)
                self._bridge = {"status": "waiting", "lineCount": self._count, "source": "luna_original_text", "url": self._url, "error": str(exc)}
                await self._publish()
                await asyncio.sleep(1.0)


class MacVisionOCRTextSource:
    """Capture game dialogue via native macOS ScreenCaptureKit and Apple Vision OCR."""

    def __init__(self, on_line: VNLine, on_status: StatusChanged) -> None:
        self._on_line = on_line
        self._on_status = on_status
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._line_count = 0
        self._bridge: dict[str, Any] = {"status": "not_started", "lineCount": 0, "source": "mac_vision_ocr"}

    def status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        return {
            "status": self._bridge.get("status", "not_started"),
            "pid": self._proc.pid if self._proc is not None else None,
            "helper": "mac_screen_ocr",
            "owned": True,
        }, dict(self._bridge)

    async def _publish(self) -> None:
        await self._on_status(*self.status())

    async def start(self, profile: dict[str, Any], params: dict[str, Any], *, target_pid: int | None) -> None:
        del target_pid
        if sys.platform != "darwin":
            raise RuntimeError("mac_vision_ocr text source is supported only on macOS.")

        project_root = Path(__file__).resolve().parents[1]
        binary = project_root / "build" / "bin" / "mac_screen_ocr"
        build_script = project_root / "tools" / "build_mac_screen_ocr.sh"
        if not binary.is_file():
            if build_script.is_file():
                build_proc = await asyncio.create_subprocess_exec(str(build_script))
                await build_proc.wait()
            if not binary.is_file():
                raise FileNotFoundError(f"Native OCR helper not found: {binary}")

        crop = profile.get("ocrCropRect") or params.get("ocrCropRect") or {"x": 0.0, "y": 0.65, "w": 1.0, "h": 0.35}
        crop_str = f"{crop.get('x', 0.0)},{crop.get('y', 0.65)},{crop.get('w', 1.0)},{crop.get('h', 0.35)}"
        args = [str(binary), "--stream", "--crop", crop_str]

        win_id = params.get("ocrWindowId") or profile.get("ocrWindowId")
        win_name = str(params.get("ocrWindowName") or profile.get("ocrWindowName") or "").strip()
        app_name = str(params.get("ocrAppName") or profile.get("ocrAppName") or "").strip()
        if not win_name and not app_name and profile.get("gameExe"):
            app_name = Path(profile["gameExe"]).stem

        if win_id:
            args.extend(["--window-id", str(win_id)])
        elif target_pid:
            args.extend(["--pid", str(target_pid)])
        elif win_name:
            args.extend(["--window-name", win_name])
        elif app_name:
            args.extend(["--app-name", app_name])

        languages = str(profile.get("ocrLanguage") or "ja,zh-Hans,zh-Hant,en-US").strip()
        if languages:
            args.extend(["--languages", languages])

        interval = profile.get("ocrIntervalMs") or 700
        args.extend(["--interval-ms", str(interval)])

        self._line_count = 0
        self._bridge = {"status": "connecting", "lineCount": 0, "source": "mac_vision_ocr"}
        await self._publish()

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._bridge = {"status": "running", "lineCount": 0, "source": "mac_vision_ocr"}
        await self._publish()
        self._read_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

        self._bridge = {"status": "not_started", "lineCount": self._line_count, "source": "mac_vision_ocr"}
        await self._publish()

    async def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            return
        while True:
            line_bytes = await self._proc.stdout.readline()
            if not line_bytes:
                break
            line_str = line_bytes.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
            except Exception:
                continue
            msg_type = data.get("type")
            if msg_type == "status":
                status = data.get("status")
                if status:
                    self._bridge["status"] = status
                    await self._publish()
            elif msg_type == "line":
                text = str(data.get("text") or "").strip()
                if text:
                    for item in _prepare_incoming_items(text):
                        payload = {
                            **item,
                            "metadata": {
                                **item.get("metadata", {}),
                                "source": "mac_vision_ocr",
                            },
                        }
                        result = await self._on_line(payload)
                        if isinstance(result, dict) and result.get("status") == "ignored":
                            continue
                        self._line_count += 1
                        line_info = result.get("line") if isinstance(result, dict) and isinstance(result.get("line"), dict) else {}
                        self._bridge = {
                            "status": "running",
                            "lineCount": self._line_count,
                            "source": "mac_vision_ocr",
                            "lastTextPreview": str(line_info.get("text") or payload["text"])[:80],
                        }
                        await self._publish()


async def list_macos_windows() -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        return []
    project_root = Path(__file__).resolve().parents[1]
    binary = project_root / "build" / "bin" / "mac_screen_ocr"
    build_script = project_root / "tools" / "build_mac_screen_ocr.sh"
    if not binary.is_file() and build_script.is_file():
        proc = await asyncio.create_subprocess_exec(str(build_script))
        await proc.wait()
    if not binary.is_file():
        return []
    proc = await asyncio.create_subprocess_exec(
        str(binary), "--list-windows",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("[VNSource] mac_screen_ocr --list-windows error (%d): %s", proc.returncode, stderr.decode("utf-8", errors="replace"))
    try:
        raw_list = json.loads(stdout.decode("utf-8"))
        results = []
        for item in raw_list:
            wid = item.get("id") or item.get("windowId")
            app = item.get("app") or item.get("appName") or ""
            title = item.get("title") or ""
            pid = item.get("pid", 0)
            results.append({
                "id": wid,
                "windowId": wid,
                "app": app,
                "appName": app,
                "title": title,
                "pid": pid,
                "width": item.get("width", 0),
                "height": item.get("height", 0),
            })
        return results
    except Exception:
        return []


async def capture_macos_window_preview(
    window_id: int | None = None,
    window_name: str = "",
    app_name: str = "",
    pid: int | None = None,
) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"error": "Supported only on macOS"}
    project_root = Path(__file__).resolve().parents[1]
    binary = project_root / "build" / "bin" / "mac_screen_ocr"
    build_script = project_root / "tools" / "build_mac_screen_ocr.sh"
    if not binary.is_file() and build_script.is_file():
        proc = await asyncio.create_subprocess_exec(str(build_script))
        await proc.wait()
    if not binary.is_file():
        return {"error": "mac_screen_ocr binary not available"}
    args = [str(binary), "--preview-frame"]
    if window_id:
        args.extend(["--window-id", str(window_id)])
    elif pid:
        args.extend(["--pid", str(pid)])
    elif window_name:
        args.extend(["--window-name", window_name])
    elif app_name:
        args.extend(["--app-name", app_name])
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    try:
        return json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        return {"error": f"Failed to capture preview: {stderr.decode('utf-8', errors='replace') or exc}"}


def _prepare_incoming_items(raw: str) -> list[dict[str, Any]]:
    value = str(raw or "").strip()
    if not value or _looks_like_noise(value):
        return []
    if value.startswith("{"):
        decoder = json.JSONDecoder()
        index = 0
        parsed: list[dict[str, Any]] = []
        while index < len(value):
            while index < len(value) and value[index].isspace():
                index += 1
            if index >= len(value):
                break
            try:
                item, index = decoder.raw_decode(value, index)
            except ValueError:
                parsed = []
                break
            if not isinstance(item, dict):
                parsed = []
                break
            parsed.append(item)
        if parsed:
            return [line for item in parsed if (line := _normalize_item(item))]
    return [_normalize_item({"text": value})]


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(item.get("text") or item.get("line") or "")).strip()
    if not text:
        return {}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    payload = {
        "text": text,
        "speaker": re.sub(r"\s+", " ", str(item.get("speaker") or "")).strip(),
        "script_id": str(item.get("script_id") or item.get("scriptId") or metadata.get("script_id") or ""),
        "metadata": metadata,
    }
    if item.get("scene_id") or item.get("sceneId"):
        payload["scene_id"] = str(item.get("scene_id") or item.get("sceneId"))
    if item.get("text_language"):
        payload["text_language"] = str(item["text_language"])
    return payload


def _looks_like_noise(text: str) -> bool:
    value = re.sub(r"\s+", " ", text).strip().lower()
    markers = (
        "traceback (most recent call last):", "vn_live_reaction_runner.py", "agent-v0.1.4-win32-x64",
        "targetpid:", "device: [local]", "module ./libmono.js not found", "enoent: no such file or directory",
        "=> silence reason=", "=> hold reason=", "=> speak reason=", "powershell ", "get-content ",
    )
    return any(marker in value for marker in markers)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _agent_switch(name: str, value: str) -> str:
    return f"--{name}={value}"


def _spawn(args: list[str], *, cwd: Path, hidden: bool) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {"cwd": str(cwd), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL, "shell": False}
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        if hidden:
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            kwargs["startupinfo"] = startup
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    logger.info("[VNSource] spawn Agent: %s", " ".join(args))
    return subprocess.Popen(args, **kwargs)


async def _terminate_proc(proc: subprocess.Popen[Any]) -> None:
    try:
        proc.terminate()
        await asyncio.to_thread(proc.wait, 3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            logger.exception("[VNSource] failed to stop owned Agent process pid=%s", proc.pid)


async def _matching_agent_pids(executable: Path, script: Path) -> list[int]:
    if os.name != "nt":
        return []
    try:
        return await asyncio.wait_for(asyncio.to_thread(_matching_agent_pids_sync, executable, script), timeout=4)
    except Exception as exc:
        raise RuntimeError("Cannot inspect existing Agent processes before attaching.") from exc


def _matching_agent_pids_sync(executable: Path, script: Path) -> list[int]:
    executable = executable.resolve()
    script_text = str(script).lower()
    pids: list[int] = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if str(process.info.get("name") or "").lower() != executable.name.lower():
                continue
            path = process.exe()
            command = " ".join(process.cmdline()).lower()
            if (path and Path(path).resolve() == executable and script_text in command
                    and ("--pname" in command or "--script" in command)):
                pids.append(process.pid)
        except psutil.NoSuchProcess:
            continue
    return pids
