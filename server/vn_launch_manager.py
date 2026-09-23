"""Launch facade for VN Player mode.

This module keeps the main chat, Electron UI, and future hook helpers pointed at
one small control surface. The VN runtime remains the source of truth for story
state; this manager only owns profile selection and launch lifecycle state.
"""

from __future__ import annotations

import asyncio
import ctypes
import csv
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import request

from server.event_bus import bus
from server.protocol import Method
from server.vn_text_sources import AgentVNTextSource, LunaVNTextSource, VNTextSourceAdapter

RuntimeStart = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
RuntimeStop = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
RuntimeStatus = Callable[[], Awaitable[dict[str, Any] | None]]
RuntimeLine = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
BeforeExternalLaunch = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class VNLaunchManager:
    """Profile-aware launcher for VN Player runtime sessions."""

    def __init__(
        self,
        project_root: Path,
        *,
        runtime_start: RuntimeStart,
        runtime_stop: RuntimeStop,
        runtime_status: RuntimeStatus,
        runtime_line: RuntimeLine,
        before_external_launch: BeforeExternalLaunch | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.vn_root = self.project_root.parent / "visual novel player"
        self._runtime_start = runtime_start
        self._runtime_stop = runtime_stop
        self._runtime_status = runtime_status
        self._runtime_line = runtime_line
        self._before_external_launch = before_external_launch
        self._game_proc: subprocess.Popen[Any] | None = None
        self._overlay_proc: subprocess.Popen[Any] | None = None
        self._text_source: VNTextSourceAdapter | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "profileId": "",
            "sessionId": "",
            "startedAt": 0,
            "updatedAt": _now_ms(),
            "error": "",
            "game": {"status": "not_started", "pid": None, "path": ""},
            "hook": {"status": "not_started", "pid": None, "helper": ""},
            "overlay": {"status": "not_started", "pid": None, "url": "", "helper": ""},
            "bridge": {"status": "not_started", "lineCount": 0, "source": "line_bridge"},
        }

    def profiles(self) -> dict[str, Any]:
        profiles = [self._paranormasight_profile()]
        return {"profiles": profiles}

    async def status(self) -> dict[str, Any]:
        self._refresh_process_state()
        return {
            **self._state,
            "profiles": self.profiles()["profiles"],
            "runtime": await self._safe_runtime_status(),
        }

    async def start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if self._text_source is not None:
            raise RuntimeError("VN launch session is already active; stop it before starting another.")
        source_name = str(params.get("textSource") or params.get("text_source") or "agent").strip().lower()
        if source_name not in {"agent", "luna"}:
            raise ValueError(f"unknown VN text source: {source_name}")
        profile_id = str(params.get("profile_id") or params.get("profileId") or "paranormasight").strip()
        profile = self._profile_by_id(profile_id)
        session_id = str(params.get("session_id") or params.get("sessionId") or "").strip()
        if not session_id:
            session_id = f"live_{profile_id}_{time.strftime('%Y%m%d_%H%M%S')}"

        runtime_params = {
            **profile["runtime"],
            "session_id": session_id,
            "script_path": profile["scriptPath"],
        }
        if isinstance(params.get("runtime"), dict):
            runtime_params.update(params["runtime"])  # type: ignore[arg-type]
        launch_game = _truthy(params.get("launchGame") or params.get("launch_game"))
        attach_hook = _truthy(params.get("attachHook") or params.get("attach_hook"))
        stop_wallpaper = _truthy(params.get("stopWallpaper") if "stopWallpaper" in params else launch_game)
        overlay_param = params.get("launchOverlay") if "launchOverlay" in params else params.get("launch_overlay")
        launch_overlay = _truthy(overlay_param) if overlay_param is not None else (launch_game or attach_hook)
        if source_name == "luna" and (launch_game or attach_hook):
            raise ValueError("Luna text source connects to an external game and LunaTranslator; Agent launch options are unavailable.")

        self._state.update(
            {
                "status": "starting",
                "textSource": source_name,
                "profileId": profile_id,
                "sessionId": session_id,
                "startedAt": _now_ms(),
                "updatedAt": _now_ms(),
                "error": "",
                "game": {
                    "status": "not_started",
                    "pid": None,
                    "path": profile.get("gameExe") or "",
                },
                "hook": {
                    "status": "not_started",
                    "pid": None,
                    "helper": profile.get("hookHelper") or "",
                },
                "overlay": {
                    "status": "not_started",
                    "pid": None,
                    "url": profile.get("overlayUrl") or "",
                    "helper": profile.get("overlayHelper") or "",
                },
                "bridge": {"status": "not_started", "lineCount": 0, "source": "line_bridge"},
            }
        )
        await self._publish_status()

        runtime_started = False
        try:
            if launch_game and stop_wallpaper:
                await self._run_before_external_launch(
                    {
                        "reason": "vn_launch_game",
                        "profileId": profile_id,
                        "sessionId": session_id,
                    }
                )
            if launch_overlay:
                runtime_params["overlay_url"] = await self._launch_overlay(profile, params)
            runtime = await self._runtime_start(runtime_params)
            runtime_started = True
            if launch_game:
                await self._launch_game(profile)
            elif attach_hook:
                self._state["game"] = {
                    **dict(self._state.get("game") or {}),
                    "status": "manual_required",
                    "path": profile.get("gameExe") or "",
                }
            self._text_source = (
                AgentVNTextSource(self._runtime_line, self._source_status_changed)
                if source_name == "agent" else LunaVNTextSource(self._runtime_line, self._source_status_changed)
            )
            target_pid = self._game_proc.pid if self._process_alive(self._game_proc) else None
            if attach_hook and target_pid is None:
                target_pid = _find_windows_pid(str(profile.get("processName") or ""))
                if target_pid is not None:
                    self._state["game"] = {
                        "status": "external_running", "pid": target_pid,
                        "path": profile.get("gameExe") or "", "owned": False,
                    }
            await self._text_source.start(profile, params, target_pid=target_pid)
        except Exception as exc:
            await self._cleanup_failed_start(runtime_started=runtime_started, close_game=launch_game)
            self._state.update({"status": "error", "updatedAt": _now_ms(), "error": str(exc)})
            await self._publish_status()
            raise

        self._state.update({"status": "active", "updatedAt": _now_ms(), "error": ""})
        payload = await self.status()
        payload["runtime"] = runtime or payload.get("runtime")
        await bus.emit(Method.VN_LAUNCH_STATUS, payload)
        return payload

    async def stop(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self._state.update({"status": "stopping", "updatedAt": _now_ms(), "error": ""})
        await self._publish_status()
        if self._text_source is not None:
            await self._text_source.stop()
            self._text_source = None
        await self._terminate_proc("overlay", self._overlay_proc)
        self._overlay_proc = None
        if _truthy(params.get("closeGame") or params.get("close_game")):
            await self._terminate_proc("game", self._game_proc)
            self._game_proc = None
        runtime = await self._runtime_stop({"reason": str(params.get("reason") or "launch_stop")})
        self._state.update(
            {
                "status": "idle",
                "profileId": "",
                "sessionId": "",
                "updatedAt": _now_ms(),
                "game": {
                    "status": "not_started" if self._game_proc is None else "running",
                    "pid": self._game_proc.pid if self._process_alive(self._game_proc) else None,
                    "path": "",
                },
                "hook": {"status": "not_started", "pid": None, "helper": ""},
                "overlay": {"status": "not_started", "pid": None, "url": "", "helper": ""},
                "bridge": {"status": "not_started", "lineCount": self._state["bridge"].get("lineCount", 0), "source": "line_bridge"},
            }
        )
        payload = await self.status()
        payload["runtime"] = runtime or payload.get("runtime")
        await bus.emit(Method.VN_LAUNCH_STATUS, payload)
        return payload

    async def _publish_status(self) -> None:
        await bus.emit(Method.VN_LAUNCH_STATUS, await self.status())

    async def _source_status_changed(self, hook: dict[str, Any], bridge: dict[str, Any]) -> None:
        self._state["hook"] = hook
        self._state["bridge"] = bridge
        await self._publish_status()

    async def _cleanup_failed_start(self, *, runtime_started: bool, close_game: bool) -> None:
        if self._text_source is not None:
            await self._text_source.stop()
            self._text_source = None
        self._state["hook"] = {"status": "not_started", "pid": None, "helper": ""}
        await self._terminate_proc("overlay", self._overlay_proc)
        self._overlay_proc = None
        self._state["overlay"] = {"status": "not_started", "pid": None, "url": "", "helper": ""}
        if close_game:
            await self._terminate_proc("game", self._game_proc)
            self._game_proc = None
            self._state["game"] = {"status": "not_started", "pid": None, "path": ""}
        self._state["bridge"] = {"status": "not_started", "lineCount": self._state["bridge"].get("lineCount", 0), "source": "line_bridge"}
        if runtime_started:
            try:
                await self._runtime_stop({"reason": "launch_error"})
            except Exception:
                logger.exception("[VNLaunch] failed to stop runtime after launch error")

    async def _safe_runtime_status(self) -> dict[str, Any] | None:
        try:
            return await self._runtime_status()
        except Exception as exc:
            return {"status": "unknown", "error": str(exc)}

    async def _run_before_external_launch(self, payload: dict[str, Any]) -> None:
        callback = self._before_external_launch
        if callback is None:
            return
        try:
            await callback(payload)
        except Exception:
            logger.exception("[VNLaunch] before_external_launch callback failed")

    async def _launch_overlay(self, profile: dict[str, Any], params: dict[str, Any]) -> str:
        helper = Path(str(profile.get("overlayHelper") or ""))
        if not helper.is_file():
            raise FileNotFoundError(f"VN portrait overlay helper not found: {helper}")

        host = str(params.get("overlayHost") or params.get("overlay_host") or "127.0.0.1")
        port = _coerce_int(params.get("overlayPort") or params.get("overlay_port") or profile.get("overlayPort"), 8788)
        url = str(params.get("overlayUrl") or params.get("overlay_url") or f"http://{host}:{port}/reaction")
        health_url = str(
            params.get("overlayHealthUrl")
            or params.get("overlay_health_url")
            or profile.get("overlayHealthUrl")
            or f"http://{host}:{port}/health"
        )
        images_dir = Path(
            str(
                params.get("overlayImagesDir")
                or params.get("overlay_images_dir")
                or profile.get("overlayImagesDir")
                or self.project_root / "render" / "assets" / "images"
            )
        )

        if self._process_alive(self._overlay_proc):
            assert self._overlay_proc is not None
            self._state["overlay"] = {
                "status": "running",
                "pid": self._overlay_proc.pid,
                "url": url,
                "helper": str(helper),
                "owned": True,
            }
            await self._publish_status()
            return url

        if await asyncio.to_thread(_http_health, health_url, 0.35):
            self._state["overlay"] = {
                "status": "external_running",
                "pid": None,
                "url": url,
                "helper": str(helper),
                "owned": False,
            }
            await self._publish_status()
            return url

        args = [
            sys.executable,
            str(helper),
            "--host",
            host,
            "--port",
            str(port),
            "--images-dir",
            str(images_dir),
            "--x",
            str(_coerce_int(params.get("overlayX") or params.get("overlay_x"), 60)),
            "--y",
            str(_coerce_int(params.get("overlayY") or params.get("overlay_y"), 80)),
            "--crop-side-ratio",
            str(_coerce_float(params.get("overlayCropSideRatio") or params.get("overlay_crop_side_ratio"), 0.74)),
            "--crop-y-ratio",
            str(_coerce_float(params.get("overlayCropYRatio") or params.get("overlay_crop_y_ratio"), 0.035)),
        ]
        # Keep the original VN Tk shell. The adapter replaces only its portrait area.
        adapter = self.project_root / "tools" / "vn_portrait_overlay_lite.py"
        args[1:2] = [str(adapter), "--legacy-helper", str(helper),
                     "--lite-dir", str(self.project_root / "assets" / "companion" / "kurisu")]
        self._overlay_proc = self._spawn(args, cwd=helper.parent, hidden=True)
        self._state["overlay"] = {
            "status": "starting",
            "pid": self._overlay_proc.pid,
            "url": url,
            "helper": str(helper),
            "owned": True,
        }
        await self._publish_status()

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if not self._process_alive(self._overlay_proc):
                raise RuntimeError("VN portrait overlay exited before becoming ready.")
            if await asyncio.to_thread(_http_health, health_url, 0.35):
                self._state["overlay"] = {
                    "status": "running",
                    "pid": self._overlay_proc.pid if self._overlay_proc else None,
                    "url": url,
                    "helper": str(helper),
                    "owned": True,
                }
                await self._publish_status()
                return url
            await asyncio.sleep(0.12)
        raise RuntimeError(f"VN portrait overlay did not become ready: {health_url}")

    async def _launch_game(self, profile: dict[str, Any]) -> None:
        game_exe = Path(str(profile.get("gameExe") or ""))
        if not game_exe.is_file():
            raise FileNotFoundError(f"VN game executable not found: {game_exe}")
        if self._process_alive(self._game_proc):
            self._state["game"] = {"status": "running", "pid": self._game_proc.pid, "path": str(game_exe)}
            return
        self._game_proc = self._spawn([str(game_exe)], cwd=game_exe.parent, hidden=False)
        self._state["game"] = {"status": "running", "pid": self._game_proc.pid, "path": str(game_exe)}
        await self._publish_status()
        await asyncio.sleep(2.4)
        await asyncio.to_thread(_bring_process_window_to_front, self._game_proc.pid)

    def _spawn(self, args: list[str], *, cwd: Path, hidden: bool) -> subprocess.Popen[Any]:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            if hidden:
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0
                kwargs["startupinfo"] = startup
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        logger.info("[VNLaunch] spawn: %s", " ".join(args))
        return subprocess.Popen(args, **kwargs)

    async def _terminate_proc(self, label: str, proc: subprocess.Popen[Any] | None) -> None:
        if not self._process_alive(proc):
            return
        assert proc is not None
        logger.info("[VNLaunch] terminating %s pid=%s", label, proc.pid)
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                logger.exception("[VNLaunch] failed to kill %s pid=%s", label, proc.pid)

    def _refresh_process_state(self) -> None:
        if self._game_proc is not None:
            game = dict(self._state.get("game") or {})
            game["status"] = "running" if self._process_alive(self._game_proc) else "exited"
            game["pid"] = self._game_proc.pid
            self._state["game"] = game
        if self._text_source is not None:
            self._state["hook"], self._state["bridge"] = self._text_source.status()
        if self._overlay_proc is not None:
            overlay = dict(self._state.get("overlay") or {})
            overlay["status"] = "running" if self._process_alive(self._overlay_proc) else "exited"
            overlay["pid"] = self._overlay_proc.pid
            overlay["owned"] = True
            self._state["overlay"] = overlay

    @staticmethod
    def _process_alive(proc: subprocess.Popen[Any] | None) -> bool:
        return proc is not None and proc.poll() is None

    def _profile_by_id(self, profile_id: str) -> dict[str, Any]:
        for profile in self.profiles()["profiles"]:
            if profile["id"] == profile_id:
                return profile
        raise ValueError(f"unknown VN profile: {profile_id}")

    def _paranormasight_profile(self) -> dict[str, Any]:
        script_path = self.vn_root / "ParanormasightChsLocalization" / "texts" / "zh_Hans" / "Hazy_Script.txt"
        runner_path = self.vn_root / "vn_live_reaction_runner.py"
        game_exe = self.vn_root / "PARANORMASIGHT" / "PARANORMASIGHT.exe"
        agent_exe = self.vn_root / "agent" / "agent-v0.1.4-win32-x64" / "agent.exe"
        hook_script = self.vn_root / "PARANORMASIGHT" / "PC_Steam_Unity_Paranormasight.js"
        overlay_script = self.vn_root / "vn_portrait_overlay_tk.py"
        overlay_port = 8788
        overlay_host = "127.0.0.1"
        overlay_url = f"http://{overlay_host}:{overlay_port}/reaction"
        overlay_health_url = f"http://{overlay_host}:{overlay_port}/health"
        overlay_images_dir = self.project_root / "render" / "assets" / "images"
        agent_ws_host = "127.0.0.1"
        agent_ws_port = 9001
        return {
            "id": "paranormasight",
            "name": "PARANORMASIGHT",
            "description": "Mystery VN profile with bounded lookahead and evidence-aware Kurisu reactions.",
            "scriptPath": str(script_path),
            "scriptExists": script_path.is_file(),
            "gameExe": str(game_exe) if game_exe.is_file() else "",
            "gameExists": game_exe.is_file(),
            "agentExe": str(agent_exe) if agent_exe.is_file() else "",
            "agentExists": agent_exe.is_file(),
            "hookHelper": str(hook_script) if hook_script.is_file() else "",
            "hookExists": hook_script.is_file(),
            "overlayHelper": str(overlay_script) if overlay_script.is_file() else "",
            "overlayExists": overlay_script.is_file(),
            "overlayUrl": overlay_url,
            "overlayHealthUrl": overlay_health_url,
            "overlayPort": overlay_port,
            "overlayImagesDir": str(overlay_images_dir),
            "lineBridgeMode": "hybrid",
            "agentWsHost": agent_ws_host,
            "agentWsPort": agent_ws_port,
            "agentWsUrl": f"ws://{agent_ws_host}:{agent_ws_port}",
            "processName": "PARANORMASIGHT.exe",
            "runnerPath": str(runner_path) if runner_path.is_file() else "",
            "runtime": {
                "game_id": "paranormasight",
                "game_title": "PARANORMASIGHT",
                "game_genre": "mystery",
                "prompt_pack": "mystery",
                "output_language": "ja",
                "script_language": "zh_Hans",
                "lookahead_enabled": True,
                "lookahead_llm_enabled": True,
                "lookahead_max_calls": 20,
                "lookahead_min_lines": 20,
                "lookahead_max_lines": 50,
                "max_reactions_per_minute": 20,
            },
        }


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"", "0", "false", "no", "off", "none"}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _http_health(url: str, timeout: float) -> bool:
    if not url:
        return False
    try:
        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return False
            raw = response.read(256)
            if not raw:
                return True
            try:
                payload = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                return True
            return bool(payload.get("ok")) if isinstance(payload, dict) and "ok" in payload else True
    except Exception:
        return False


def _find_windows_pid(image_name: str) -> int | None:
    if os.name != "nt" or not image_name:
        return None
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_no_window:
        kwargs["creationflags"] = creation_no_window
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            **kwargs,
            timeout=3,
            check=False,
        )
    except Exception:
        logger.exception("[VNLaunch] failed to inspect process list for %s", image_name)
        return None
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() == image_name.lower():
            try:
                return int(row[1])
            except ValueError:
                return None
    return None


def _bring_process_window_to_front(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    try:
        user32 = ctypes.windll.user32
        enum_windows_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows.argtypes = [enum_windows_proc, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_bool
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_bool
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
    except Exception:
        logger.exception("[VNLaunch] failed to prepare window focus helpers")
        return False

    SW_RESTORE = 9
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        handles: list[int] = []

        def callback(hwnd: int, _lparam: int) -> bool:
            process_id = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == pid:
                handles.append(int(hwnd))
            return True

        try:
            user32.EnumWindows(enum_windows_proc(callback), None)
            visible = [hwnd for hwnd in handles if user32.IsWindowVisible(hwnd)]
            target = visible[0] if visible else (handles[0] if handles else 0)
            if target:
                hwnd = ctypes.c_void_p(target)
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetForegroundWindow(hwnd)
                logger.info("[VNLaunch] restored game window pid=%s hwnd=%s", pid, target)
                return True
        except Exception:
            logger.exception("[VNLaunch] failed to restore game window pid=%s", pid)
            return False
        time.sleep(0.1)
    logger.warning("[VNLaunch] no game window found to restore pid=%s", pid)
    return False
