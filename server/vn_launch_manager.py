"""Launch facade for VN Player mode.

This module keeps the main chat, Electron UI, and future hook helpers pointed at
one small control surface. The VN runtime remains the source of truth for story
state; this manager only owns profile selection and launch lifecycle state.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import request

import psutil
from pydantic import ValidationError

from server.event_bus import bus
from server.protocol import Method
from server.vn_text_sources import AgentVNTextSource, LunaVNTextSource, VNTextSourceAdapter
from server.vn_profiles import LaunchProfile, VNProfileStore
from vn_player.schemas import new_id, resolve_capability_defaults

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
        runtime_overlay: Callable[[str], None] | None = None,
        backend_url: str = "",
    ) -> None:
        self.project_root = Path(project_root)
        self.vn_root = self.project_root.parent / "visual novel player"
        self._profiles = VNProfileStore(self.project_root)
        self._status_profiles: list[dict[str, Any]] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._start_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._external_game_proc: psutil.Process | None = None
        self._runtime_owned = False
        self._game_path = ""
        self._runtime_start = runtime_start
        self._runtime_stop = runtime_stop
        self._runtime_status = runtime_status
        self._runtime_line = runtime_line
        self._before_external_launch = before_external_launch
        self._runtime_overlay = runtime_overlay
        self._backend_url = backend_url
        self._game_proc: subprocess.Popen[Any] | psutil.Process | None = None
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
        saved = self._profiles.load()
        builtin = self._paranormasight_profile()
        agent_exe = saved.agentExe or (builtin["agentExe"] if Path(builtin["agentExe"]).is_file() else "")
        profiles = {builtin["id"]: builtin} if Path(builtin["gameExe"]).is_file() else {}
        for item in saved.profiles:
            preset = builtin if item.id == builtin["id"] else {}
            profiles[item.id] = {**preset, **item.model_dump()}
        for profile in profiles.values():
            profile["agentExe"] = agent_exe
            preset = profile.get("runtime", {})
            profile["promptPack"] = profile.get("promptPack") or preset.get("prompt_pack", "base")
            profile["capabilities"] = resolve_capability_defaults(profile["promptPack"])
            profile["runtime"] = {**preset, "capabilities": profile["capabilities"]}
            if profile.get("voiceInput") is None:
                profile["voiceInput"] = profile["id"] == "paranormasight"
            profile["runtimeSupported"] = True
            # The existing companion window is a shared presentation surface.
            for key in ("overlayHelper", "overlayUrl", "overlayHealthUrl", "overlayPort", "overlayImagesDir"):
                profile.setdefault(key, builtin[key])
            for key, flag in (("gameExe", "gameExists"), ("agentExe", "agentExists"),
                              ("hookHelper", "hookExists"), ("scriptPath", "scriptExists"),
                              ("overlayHelper", "overlayExists")):
                profile[flag] = bool(profile.get(key)) and Path(profile[key]).is_file()
        self._status_profiles = list(profiles.values())
        return {"profiles": self._status_profiles, "agentExe": agent_exe,
                "overlayAvailable": Path(builtin["overlayHelper"]).is_file(),
                "capabilityPresets": {kind: resolve_capability_defaults(kind) for kind in ("base", "mystery")}}

    def save_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._state["status"] in {"starting", "active", "stopping"}:
            raise RuntimeError("Stop text capture before editing game settings.")
        try:
            profile = LaunchProfile.model_validate(params.get("profile"))
        except ValidationError as exc:
            raise ValueError("\n".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())) from exc
        existing = self.profiles()
        if isinstance(params.get("profile"), dict) and params["profile"].get("id"):
            if profile.id not in {p["id"] for p in existing["profiles"]}:
                raise ValueError(f"Unknown VN profile: {profile.id}")
        self._profiles.save(profile, agent_exe=str(params.get("agentExe", existing["agentExe"])).strip())
        return {**self.profiles(), "profileId": profile.id}

    async def status(self, *, refresh_profiles: bool = True) -> dict[str, Any]:
        self._refresh_process_state()
        return {
            **self._state,
            "profiles": (self.profiles()["profiles"] if refresh_profiles or self._status_profiles is None
                         else self._status_profiles),
            "runtime": await self._safe_runtime_status(),
        }

    async def capture(self) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._state["status"] != "active" or self._state.get("captureOnly"):
                raise RuntimeError("Start a companion session before attaching a game view.")
            runtime = await self._safe_runtime_status() or {}
            if not (runtime.get("visual") or {}).get("supported"):
                raise RuntimeError((runtime.get("visual") or {}).get("reason") or "The current VN model does not support images.")
            if not (runtime.get("capabilities", {}).get("interaction") or {}).get("enabled"):
                raise RuntimeError("Enable player interaction before attaching a game view.")
            profile = self._profile_by_id(self._state["profileId"])
            executable = str(profile.get("gameExe") or "")
            if not executable:
                raise RuntimeError("Select the game executable in its profile before capturing.")
            # The launcher bound this session to a specific process. Preserve
            # that identity even if another instance appears afterwards.
            pid = self._state.get("game", {}).get("pid")
            if pid is None:
                pid = await asyncio.to_thread(_find_game_pid, executable)
            if pid is None:
                raise RuntimeError("The configured game is not running.")
            from server.visual_runtime import capture_game_window
            visual = await asyncio.to_thread(capture_game_window, pid, executable)
            return {"status": "ok", "visual_context": visual}

    async def set_overlay(self, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lifecycle_lock:
            if self._state["status"] != "active" or self._state.get("captureOnly") or params.get("session_id") != self._state["sessionId"]:
                raise ValueError("The VN session has ended or changed.")
            if not isinstance(params.get("enabled"), bool):
                raise ValueError("Overlay visibility must be a boolean.")
            enabled = params["enabled"]
            if enabled:
                url = await self._launch_overlay(self._profile_by_id(self._state["profileId"]), {})
                if self._runtime_overlay:
                    self._runtime_overlay(url)
            else:
                url = str(self._state["overlay"].get("url") or "")
            if url and self._state["overlay"].get("status") in {"running", "external_running"}:
                await asyncio.to_thread(_set_overlay_visible, url, enabled)
            self._state["overlay"]["visible"] = enabled
            await self._publish_status()
            return await self.status()

    async def start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._start_task is not None:
            raise RuntimeError("A VN game is already starting.")
        async with self._lifecycle_lock:
            self._start_task = asyncio.create_task(self._start(params))
            try:
                return await self._start_task
            finally:
                self._start_task = None

    async def _start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if self._text_source is not None:
            raise RuntimeError("VN launch session is already active; stop it before starting another.")
        profile_id = str(params.get("profile_id") or params.get("profileId") or "paranormasight").strip()
        profile = self._profile_by_id(profile_id)
        # Explicit API options still work; ordinary Start uses the saved game settings.
        aliases = {"text_source": "textSource", "launch_game": "launchGame", "attach_hook": "attachHook",
                   "launch_overlay": "launchOverlay", "luna_ws_url": "lunaWsUrl"}
        params = {aliases.get(key, key): value for key, value in params.items()}
        source_name = str(params.get("textSource", profile.get("textSource", "agent"))).strip().lower()
        if source_name not in {"agent", "luna"}:
            raise ValueError(f"unknown VN text source: {source_name}")
        params = {
            "launchGame": profile.get("launchGame", True),
            "attachHook": source_name == "agent", "bridgeText": source_name == "agent",
            "launchOverlay": profile.get("launchOverlay", True),
            "stopWallpaper": profile.get("stopWallpaper", True), "lunaWsUrl": profile.get("lunaWsUrl", ""),
            **params,
        }
        capture_only = _truthy(params.get("captureOnly"))
        if capture_only:
            params["launchOverlay"] = False
        session_id = str(params.get("session_id") or params.get("sessionId") or "").strip()
        if not session_id:
            session_id = new_id(f"live_{profile_id}")

        runtime_params = {
            **profile.get("runtime", {}),
            "game_id": profile.get("runtime", {}).get("game_id", profile_id),
            "game_title": profile.get("name") or profile_id,
            "game_genre": "mystery" if profile["promptPack"] == "mystery" else "visual_novel",
            "prompt_pack": profile["promptPack"],
            "capabilities": profile["runtime"]["capabilities"],
            "session_id": session_id,
            "script_path": profile.get("scriptPath", ""),
            "voice_input": profile.get("voiceInput", False),
            "vision_mode": profile.get("visionMode", "off"),
            "commentary_frequency": profile.get("commentaryFrequency", "balanced"),
            "terminology": profile.get("terminology", ""),
        }
        if isinstance(params.get("runtime"), dict):
            runtime_params.update(params["runtime"])  # type: ignore[arg-type]
        # The product's game type is authoritative even if an older caller sends
        # a stale runtime capability map. Direct runtime probes remain separate.
        runtime_params.update(prompt_pack=profile["promptPack"], capabilities=profile["capabilities"])
        launch_game = _truthy(params.get("launchGame") or params.get("launch_game"))
        attach_hook = _truthy(params.get("attachHook") or params.get("attach_hook"))
        stop_wallpaper = _truthy(params.get("stopWallpaper") if "stopWallpaper" in params else launch_game)
        overlay_param = params.get("launchOverlay") if "launchOverlay" in params else params.get("launch_overlay")
        launch_overlay = _truthy(overlay_param) if overlay_param is not None else (launch_game or attach_hook)
        if source_name == "luna" and attach_hook:
            raise ValueError("Agent injection is unavailable for the Luna text source.")
        if self._game_path != str(profile.get("gameExe") or ""):
            self._game_proc = None

        self._state.update(
            {
                "status": "starting",
                "textSource": source_name,
                "captureOnly": capture_only,
                "capturedLines": [],
                "closeGameOnStop": profile.get("closeGameOnStop", False),
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
        self._external_game_proc = None
        await self._publish_status()

        runtime_started = False
        game_started = False
        try:
            # Resolve the exact executable each session. Never bind a saved PID or
            # choose the first of several identically named processes.
            target_pid = None
            if launch_game or (source_name == "agent" and attach_hook):
                required = [("gameExe", "game executable")]
                if source_name == "agent" and attach_hook:
                    required.extend([("agentExe", "Agent executable"), ("hookHelper", "hook script")])
                for key, label in required:
                    if not profile.get(key) or not Path(profile[key]).is_file():
                        raise FileNotFoundError(f"VN {label} not found: {profile.get(key) or '(not configured)'}")
                target_pid = await asyncio.to_thread(_find_game_pid, profile["gameExe"])
                if target_pid is None and not launch_game and attach_hook:
                    raise RuntimeError("The configured game is not running. Start it first or enable Launch game in its settings.")
            elif profile.get("gameExe") and Path(profile["gameExe"]).is_file():
                target_pid = await asyncio.to_thread(_find_game_pid, profile["gameExe"])
            if self._game_proc is not None and self._game_proc.pid != target_pid:
                # A game kept open from another profile is no longer this session's process.
                self._game_proc = None
            if launch_game and target_pid is None and stop_wallpaper:
                await self._run_before_external_launch(
                    {
                        "reason": "vn_launch_game",
                        "profileId": profile_id,
                        "sessionId": session_id,
                    }
                )
            if launch_overlay:
                runtime_params["overlay_url"] = await self._launch_overlay(profile, params)
            runtime = None
            if not capture_only:
                runtime_started = self._runtime_owned = True
                runtime = await self._runtime_start(runtime_params)
            if launch_game and target_pid is None:
                game_started = True
                await self._launch_game(profile)
                target_pid = self._game_proc.pid if self._process_alive(self._game_proc) else None
            elif target_pid is not None:
                owned = self._process_alive(self._game_proc) and self._game_proc.pid == target_pid
                self._state["game"] = {
                    "status": "running" if owned else "external_running", "pid": target_pid,
                    "path": profile.get("gameExe") or "", "owned": owned,
                }
                if not owned:
                    try:
                        self._external_game_proc = psutil.Process(target_pid)
                        self._external_game_proc.create_time()
                    except psutil.NoSuchProcess:
                        self._state["game"]["status"] = "exited"
            on_line = self._capture_line if capture_only else self._runtime_line
            self._text_source = (
                AgentVNTextSource(on_line, self._source_status_changed)
                if source_name == "agent" else LunaVNTextSource(on_line, self._source_status_changed)
            )
            await self._text_source.start(profile, params, target_pid=target_pid)
        except asyncio.CancelledError:
            await self._cleanup_failed_start(runtime_started=runtime_started, close_game=game_started)
            self._state.update(status="idle", updatedAt=_now_ms(), error="")
            await self._publish_status()
            return await self.status()
        except Exception as exc:
            await self._cleanup_failed_start(runtime_started=runtime_started, close_game=game_started)
            self._state.update({"status": "error", "updatedAt": _now_ms(), "error": str(exc)})
            await self._publish_status()
            raise

        self._state.update({"status": "active", "updatedAt": _now_ms(), "error": ""})
        self._monitor_task = asyncio.create_task(self._monitor_processes())
        payload = await self.status()
        payload["runtime"] = runtime or payload.get("runtime")
        await bus.emit(Method.VN_LAUNCH_STATUS, payload)
        return payload

    async def stop(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # Cancellation must reach the startup task before waiting for its lifecycle lock.
        task = self._start_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lifecycle_lock:
            return await self._stop(params)

    async def _stop(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            await asyncio.gather(self._monitor_task, return_exceptions=True)
            self._monitor_task = None
        self._external_game_proc = None
        self._state.update({"status": "stopping", "updatedAt": _now_ms(), "error": ""})
        await self._publish_status()
        if self._text_source is not None:
            await self._text_source.stop()
            self._text_source = None
        await self._terminate_proc("overlay", self._overlay_proc)
        self._overlay_proc = None
        if _truthy(params.get("closeGame", params.get("close_game", self._state.get("closeGameOnStop", False)))):
            await self._terminate_proc("game", self._game_proc)
            self._game_proc = None
        runtime = None
        if self._runtime_owned:
            runtime = await self._runtime_stop({"reason": str(params.get("reason") or "launch_stop")})
            self._runtime_owned = False
        game_alive = self._process_alive(self._game_proc)
        self._state.update(
            {
                "status": "idle",
                "profileId": "",
                "sessionId": "",
                "updatedAt": _now_ms(),
                "game": {
                    "status": "running" if game_alive else "not_started",
                    "pid": self._game_proc.pid if game_alive else None,
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
        await bus.emit(Method.VN_LAUNCH_STATUS, await self.status(refresh_profiles=False))

    async def _monitor_processes(self) -> None:
        """Publish process exits even when a silent game emits no transport events."""
        previous = None
        while True:
            self._refresh_process_state()
            current = tuple(self._state[name].get("status") for name in ("game", "hook", "overlay", "bridge"))
            if current != previous:
                await self._publish_status()
                previous = current
            await asyncio.sleep(1)

    async def _capture_line(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Diagnostic preview: preserve order and genuine repetitions, without
        # starting the story runtime, indexing a script, or invoking an LLM.
        self._state["capturedLines"] = [*self._state.get("capturedLines", []), {
            "text": str(payload.get("text") or ""), "speaker": str(payload.get("speaker") or ""),
            "script_id": str(payload.get("script_id") or ""), "receivedAt": _now_ms(),
        }][-20:]
        return {"status": "ok", "line": payload}

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
            self._runtime_owned = False
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
            "--lite-dir",
            str(self.project_root / "assets" / "companion" / "kurisu"),
            "--x",
            str(_coerce_int(params.get("overlayX") or params.get("overlay_x"), 60)),
            "--y",
            str(_coerce_int(params.get("overlayY") or params.get("overlay_y"), 80)),
        ]
        if self._backend_url:
            args.extend(["--backend-url", self._backend_url])
        self._overlay_proc = self._spawn(args, cwd=self.project_root, hidden=True)
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
                    "visible": True,
                }
                await self._publish_status()
                return url
            await asyncio.sleep(0.12)
        raise RuntimeError(f"VN portrait overlay did not become ready: {health_url}")

    async def _launch_game(self, profile: dict[str, Any]) -> None:
        game_exe = Path(str(profile.get("gameExe") or ""))
        if not game_exe.is_file():
            raise FileNotFoundError(f"VN game executable not found: {game_exe}")
        if self._game_path == str(game_exe) and self._process_alive(self._game_proc):
            self._state["game"] = {"status": "running", "pid": self._game_proc.pid, "path": str(game_exe)}
            return
        if profile.get("launchMethod") == "steam":
            self._state["game"] = {"status": "waiting_for_steam", "pid": None, "path": str(game_exe)}
            await self._publish_status()
            await asyncio.to_thread(_open_steam_game, str(profile.get("steamAppId") or ""))
            self._game_proc = await self._wait_for_steam_game(str(game_exe))
        else:
            self._game_proc = self._spawn([str(game_exe)], cwd=game_exe.parent, hidden=False)
        self._game_path = str(game_exe)
        self._state["game"] = {"status": "running", "pid": self._game_proc.pid, "path": str(game_exe), "owned": True}
        await self._publish_status()
        await asyncio.sleep(2.4)
        await asyncio.to_thread(_bring_process_window_to_front, self._game_proc.pid)

    async def _wait_for_steam_game(self, executable: str, *, timeout: float = 60) -> psutil.Process:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid = await asyncio.to_thread(_find_game_pid, executable)
            if pid is not None:
                try:
                    process = psutil.Process(pid)
                    # Cache process identity before adopting it; psutil's mutation
                    # methods protect against PID reuse when this game exits.
                    process.create_time()
                    if _same_executable(process.exe(), executable):
                        return process
                except psutil.NoSuchProcess:
                    pass
            await asyncio.sleep(.5)
        raise TimeoutError("Steam did not start the configured game. Check Steam's launch window, the app ID and the selected game executable, then retry.")

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

    async def _terminate_proc(self, label: str, proc: subprocess.Popen[Any] | psutil.Process | None) -> None:
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
        if self._external_game_proc is not None:
            self._state["game"]["status"] = "external_running" if self._external_game_proc.is_running() else "exited"
        if self._game_proc is not None and self._state["game"].get("pid") == self._game_proc.pid:
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
    def _process_alive(proc: subprocess.Popen[Any] | psutil.Process | None) -> bool:
        if isinstance(proc, psutil.Process):
            return proc.is_running()
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
        overlay_script = self.project_root / "tools" / "vn_portrait_overlay_lite.py"
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
            "textSource": "agent",
            "launchGame": True,
            "launchOverlay": True,
            "stopWallpaper": True,
            "closeGameOnStop": False,
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
            "lineBridgeMode": "websocket",
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


def _set_overlay_visible(url: str, visible: bool) -> None:
    endpoint = url.rsplit("/", 1)[0] + "/visibility"
    req = request.Request(endpoint, data=json.dumps({"visible": visible}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=2) as response:
        response.read(256)


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


def _open_steam_game(app_id: str) -> None:
    if not app_id.isascii() or not app_id.isdecimal() or not 0 < int(app_id) <= 9999999999:
        raise ValueError("Enter a valid numeric Steam app ID.")
    if os.name != "nt":
        raise RuntimeError("Steam game launch is currently available on Windows.")
    try:
        os.startfile(f"steam://rungameid/{app_id}")
    except OSError as exc:
        raise RuntimeError("Steam could not open. Install or repair the Steam desktop client, then retry.") from exc


def _same_executable(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.realpath(actual)) == os.path.normcase(os.path.realpath(expected))


def _find_game_pid(executable: str) -> int | None:
    matches = []
    for process in psutil.process_iter(["pid", "exe"]):
        actual = process.info.get("exe")
        if actual and _same_executable(actual, executable):
            matches.append(process.info["pid"])
    if len(matches) > 1:
        raise RuntimeError("Multiple instances of this game are running. Keep one open before attaching Agent.")
    return matches[0] if matches else None


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
