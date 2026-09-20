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
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib import request

from server.event_bus import bus
from server.protocol import Method

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
        self._agent_proc: subprocess.Popen[Any] | None = None
        self._overlay_proc: subprocess.Popen[Any] | None = None
        self._clipboard_task: asyncio.Task[Any] | None = None
        self._agent_ws_task: asyncio.Task[Any] | None = None
        self._clipboard_last_raw = ""
        self._agent_ws_url = ""
        self._agent_ws_clipboard_fallback = False
        self._last_bridge_line_key = ""
        self._last_bridge_line_at = 0
        self._recent_bridge_lines: dict[str, int] = {}
        self._line_count = 0
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
        bridge_clipboard = _truthy(params.get("bridgeClipboard") if "bridgeClipboard" in params else True)
        stop_wallpaper = _truthy(params.get("stopWallpaper") if "stopWallpaper" in params else launch_game)
        open_hook_agent = _truthy(params.get("openHookAgent") or params.get("open_hook_agent"))
        overlay_param = params.get("launchOverlay") if "launchOverlay" in params else params.get("launch_overlay")
        launch_overlay = _truthy(overlay_param) if overlay_param is not None else (launch_game or attach_hook)

        self._state.update(
            {
                "status": "starting",
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
            if attach_hook:
                await self._launch_agent(profile, params)
            elif open_hook_agent:
                await self._open_hook_agent_ui(profile)
            else:
                self._state["hook"] = {
                    **dict(self._state.get("hook") or {}),
                    "status": "manual_required",
                    "helper": profile.get("hookHelper") or "",
                }
            if bridge_clipboard:
                self._start_line_bridge(profile, params)
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
        await self._stop_line_bridge()
        await self._terminate_proc("agent", self._agent_proc)
        self._agent_proc = None
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
                "bridge": {"status": "not_started", "lineCount": self._line_count, "source": "line_bridge"},
            }
        )
        payload = await self.status()
        payload["runtime"] = runtime or payload.get("runtime")
        await bus.emit(Method.VN_LAUNCH_STATUS, payload)
        return payload

    async def _publish_status(self) -> None:
        await bus.emit(Method.VN_LAUNCH_STATUS, await self.status())

    async def _cleanup_failed_start(self, *, runtime_started: bool, close_game: bool) -> None:
        await self._stop_line_bridge()
        await self._terminate_proc("agent", self._agent_proc)
        self._agent_proc = None
        self._state["hook"] = {"status": "not_started", "pid": None, "helper": ""}
        await self._terminate_proc("overlay", self._overlay_proc)
        self._overlay_proc = None
        self._state["overlay"] = {"status": "not_started", "pid": None, "url": "", "helper": ""}
        if close_game:
            await self._terminate_proc("game", self._game_proc)
            self._game_proc = None
            self._state["game"] = {"status": "not_started", "pid": None, "path": ""}
        self._state["bridge"] = {"status": "not_started", "lineCount": self._line_count, "source": "line_bridge"}
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

    async def _launch_agent(self, profile: dict[str, Any], params: dict[str, Any] | None = None) -> None:
        params = params or {}
        agent_exe = Path(str(profile.get("agentExe") or ""))
        hook_script = Path(str(profile.get("hookHelper") or ""))
        if not agent_exe.is_file():
            raise FileNotFoundError(f"VN hook agent not found: {agent_exe}")
        if not hook_script.is_file():
            raise FileNotFoundError(f"VN hook script not found: {hook_script}")
        if self._process_alive(self._agent_proc):
            self._state["hook"] = {"status": "running", "pid": self._agent_proc.pid, "helper": str(hook_script)}
            return

        target_pid = self._game_proc.pid if self._process_alive(self._game_proc) else None
        if target_pid is None:
            target_pid = _find_windows_pid(str(profile.get("processName") or ""))
            if target_pid is not None:
                game_exe = Path(str(profile.get("gameExe") or ""))
                self._state["game"] = {
                    "status": "external_running",
                    "pid": target_pid,
                    "path": str(game_exe) if game_exe else "",
                    "owned": False,
                }
        if target_pid:
            args = [
                str(agent_exe),
                _agent_switch("pname", str(target_pid)),
                _agent_switch("script", str(hook_script)),
            ]
        else:
            raise RuntimeError("Cannot attach VN hook: launch the game first or start it manually.")
        cleanup_param = (
            params.get("cleanupHookAgents")
            if "cleanupHookAgents" in params
            else params.get("cleanup_hook_agents")
        )
        cleanup_hook_agents = _truthy(cleanup_param) if cleanup_param is not None else True
        if cleanup_hook_agents:
            await self._cleanup_stale_hook_agents(agent_exe, hook_script)
        self._agent_proc = self._spawn(args, cwd=agent_exe.parent, hidden=True)
        self._state["hook"] = {"status": "running", "pid": self._agent_proc.pid, "helper": str(hook_script)}
        await self._publish_status()

    async def _open_hook_agent_ui(self, profile: dict[str, Any]) -> None:
        agent_exe = Path(str(profile.get("agentExe") or ""))
        hook_script = Path(str(profile.get("hookHelper") or ""))
        if not agent_exe.is_file():
            raise FileNotFoundError(f"VN hook agent not found: {agent_exe}")
        if not hook_script.is_file():
            raise FileNotFoundError(f"VN hook script not found: {hook_script}")
        if self._process_alive(self._agent_proc):
            self._state["hook"] = {
                "status": "manual_ui_open",
                "pid": self._agent_proc.pid,
                "helper": str(hook_script),
                "mode": "manual",
            }
            await self._publish_status()
            return
        self._agent_proc = self._spawn(
            [str(agent_exe), _agent_switch("script", str(hook_script))],
            cwd=agent_exe.parent,
            hidden=False,
        )
        self._state["hook"] = {
            "status": "manual_ui_open",
            "pid": self._agent_proc.pid,
            "helper": str(hook_script),
            "mode": "manual",
        }
        await self._publish_status()

    async def _cleanup_stale_hook_agents(self, agent_exe: Path, hook_script: Path) -> None:
        if os.name != "nt":
            return
        current_pid = self._agent_proc.pid if self._process_alive(self._agent_proc) and self._agent_proc else None
        processes = await asyncio.to_thread(_windows_process_infos, agent_exe.name)
        for item in processes:
            pid = _coerce_int(item.get("pid"), 0)
            if pid <= 0 or pid == current_pid:
                continue
            exe = str(item.get("path") or "")
            cmd = str(item.get("commandLine") or "")
            if not _same_windows_path(exe, agent_exe) and str(hook_script).lower() not in cmd.lower():
                continue
            if not _is_hook_agent_command(cmd, hook_script):
                continue
            logger.info("[VNLaunch] terminating stale hook agent pid=%s", pid)
            await asyncio.to_thread(_terminate_windows_pid, pid)

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

    def _start_line_bridge(self, profile: dict[str, Any], params: dict[str, Any]) -> None:
        self._recent_bridge_lines.clear()
        self._last_bridge_line_key = ""
        self._last_bridge_line_at = 0
        mode = str(params.get("bridgeMode") or params.get("bridge_mode") or profile.get("lineBridgeMode") or "hybrid").strip().lower()
        if mode in {"clipboard", "clip"}:
            self._agent_ws_clipboard_fallback = False
            self._start_clipboard_bridge()
            return
        host = str(params.get("agentWsHost") or params.get("agent_ws_host") or profile.get("agentWsHost") or "127.0.0.1")
        port = _coerce_int(params.get("agentWsPort") or params.get("agent_ws_port") or profile.get("agentWsPort"), 9001)
        if mode in {"both", "debug-both"}:
            self._start_clipboard_bridge(reset_count=True)
            self._start_agent_websocket_bridge(host=host, port=port, reset_count=False)
            self._state["bridge"] = {
                "status": "connecting",
                "lineCount": self._line_count,
                "source": "both",
                "url": self._agent_ws_url,
            }
            return
        if mode in {"hybrid", "auto"}:
            self._start_agent_websocket_bridge(host=host, port=port, fallback_clipboard=True)
            self._state["bridge"] = {
                "status": "connecting",
                "lineCount": self._line_count,
                "source": "agent_websocket",
                "mode": "auto",
                "fallback": "clipboard",
                "url": self._agent_ws_url,
            }
            return
        self._start_agent_websocket_bridge(host=host, port=port)

    def _start_clipboard_bridge(self, *, reset_count: bool = True) -> None:
        if self._clipboard_task and not self._clipboard_task.done():
            return
        self._clipboard_last_raw = _clipboard_text()
        if reset_count:
            self._line_count = 0
        self._state["bridge"] = {"status": "running", "lineCount": self._line_count, "source": "clipboard"}
        self._clipboard_task = asyncio.create_task(self._clipboard_loop())

    def _start_agent_websocket_bridge(
        self,
        *,
        host: str,
        port: int,
        reset_count: bool = True,
        fallback_clipboard: bool = False,
    ) -> None:
        self._agent_ws_clipboard_fallback = fallback_clipboard
        if self._agent_ws_task and not self._agent_ws_task.done():
            return
        if reset_count:
            self._line_count = 0
        self._agent_ws_url = f"ws://{host}:{port}"
        self._state["bridge"] = {
            "status": "connecting",
            "lineCount": 0,
            "source": "agent_websocket",
            "url": self._agent_ws_url,
        }
        self._agent_ws_task = asyncio.create_task(self._agent_websocket_loop(self._agent_ws_url))

    async def _stop_line_bridge(self) -> None:
        await self._stop_clipboard_bridge()
        await self._stop_agent_websocket_bridge()

    async def _stop_clipboard_bridge(self) -> None:
        task = self._clipboard_task
        self._clipboard_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _stop_agent_websocket_bridge(self) -> None:
        task = self._agent_ws_task
        self._agent_ws_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._agent_ws_url = ""
        self._agent_ws_clipboard_fallback = False

    async def _clipboard_loop(self) -> None:
        while True:
            try:
                raw = _clipboard_text()
                if raw and raw != self._clipboard_last_raw:
                    items = _prepare_incoming_items(raw)
                    if items:
                        self._clipboard_last_raw = raw
                    for item in items:
                        item_metadata = dict(item.get("metadata") or {})
                        hook_source = str(item_metadata.get("source") or "")
                        payload = {
                            "text": item["text"],
                            "speaker": item.get("speaker") or "",
                            "script_id": item.get("script_id") or "",
                            "metadata": {
                                **item_metadata,
                                "source": "vn_launch_clipboard_bridge",
                                "hook_source": hook_source,
                            },
                        }
                        if self._is_duplicate_bridge_line(payload):
                            logger.debug(
                                "[VNLaunch] clipboard bridge skipped duplicate chars=%s incoming_script_id=%s",
                                len(payload["text"]),
                                payload.get("script_id") or "",
                            )
                            continue
                        result = await self._runtime_line(payload)
                        result = result if isinstance(result, dict) else {}
                        matched_script_id = _runtime_result_script_id(result)
                        self._mark_bridge_line_seen(payload, matched_script_id)
                        if str(result.get("status") or "") == "ignored":
                            logger.debug(
                                "[VNLaunch] clipboard bridge runtime ignored chars=%s incoming_script_id=%s matched_script_id=%s reason=%s",
                                len(payload["text"]),
                                payload.get("script_id") or "",
                                matched_script_id,
                                result.get("reason") or "",
                            )
                            continue
                        self._line_count += 1
                        self._state["bridge"] = {
                            "status": "running",
                            "lineCount": self._line_count,
                            "source": "clipboard",
                            "lastTextPreview": payload["text"][:80],
                            "lastScriptId": matched_script_id or payload.get("script_id") or "",
                        }
                        logger.info(
                            "[VNLaunch] clipboard bridge forwarded line #%s chars=%s incoming_script_id=%s matched_script_id=%s",
                            self._line_count,
                            len(payload["text"]),
                            payload.get("script_id") or "",
                            matched_script_id,
                        )
                        await self._publish_status()
                await asyncio.sleep(0.35)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[VNLaunch] clipboard bridge error")
                self._state["bridge"] = {
                    "status": "error",
                    "lineCount": self._line_count,
                    "source": "clipboard",
                }
                await asyncio.sleep(1.0)

    async def _agent_websocket_loop(self, url: str) -> None:
        try:
            import websockets
        except Exception as exc:
            logger.exception("[VNLaunch] websockets package unavailable")
            self._state["bridge"] = {
                "status": "error",
                "lineCount": self._line_count,
                "source": "agent_websocket",
                "url": url,
                "error": str(exc),
            }
            await self._publish_status()
            return

        while True:
            try:
                logger.info("[VNLaunch] connecting Agent WebSocket line bridge: %s", url)
                self._state["bridge"] = {
                    "status": "connecting",
                    "lineCount": self._line_count,
                    "source": "agent_websocket",
                    "url": url,
                }
                await self._publish_status()
                async with websockets.connect(url, open_timeout=3, ping_interval=20, ping_timeout=10) as ws:
                    if self._agent_ws_clipboard_fallback and self._clipboard_task and not self._clipboard_task.done():
                        logger.info("[VNLaunch] Agent WebSocket connected; stopping clipboard fallback")
                        await self._stop_clipboard_bridge()
                    self._state["bridge"] = {
                        "status": "running",
                        "lineCount": self._line_count,
                        "source": "agent_websocket",
                        "url": url,
                    }
                    await self._publish_status()
                    async for raw in ws:
                        await self._handle_agent_websocket_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[VNLaunch] Agent WebSocket bridge waiting: %s", exc)
                self._state["bridge"] = {
                    "status": "waiting",
                    "lineCount": self._line_count,
                    "source": "agent_websocket",
                    "url": url,
                    "error": str(exc),
                }
                await self._publish_status()
                if self._agent_ws_clipboard_fallback and not (self._clipboard_task and not self._clipboard_task.done()):
                    logger.info("[VNLaunch] Agent WebSocket unavailable; starting clipboard fallback")
                    self._start_clipboard_bridge(reset_count=False)
                    bridge = dict(self._state.get("bridge") or {})
                    bridge["source"] = "clipboard_fallback"
                    bridge["primary"] = "agent_websocket"
                    bridge["url"] = url
                    bridge["error"] = str(exc)
                    self._state["bridge"] = bridge
                    await self._publish_status()
                await asyncio.sleep(1.0)

    async def _handle_agent_websocket_message(self, raw: Any) -> None:
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        try:
            data = json.loads(text)
        except Exception:
            logger.debug("[VNLaunch] ignored non-json Agent WS message: %r", text[:160])
            return
        if not isinstance(data, dict):
            return
        message_type = str(data.get("type") or "")
        if message_type not in {"copyText", "translate"}:
            return
        if message_type == "translate" and not _truthy(os.environ.get("VN_AGENT_ACCEPT_TRANSLATE")):
            return
        sentence = str(data.get("sentence") or data.get("text") or data.get("content") or "")
        items = _prepare_incoming_items(sentence)
        if not items:
            logger.debug("[VNLaunch] ignored Agent WS line payload: %r", sentence[:160])
            return
        for item in items:
            item_metadata = dict(item.get("metadata") or {})
            hook_source = str(item_metadata.get("source") or "")
            metadata = {
                **item_metadata,
                "source": "agent_websocket",
                "hook_source": hook_source,
                "agent_message_type": message_type,
                "process_path": str(data.get("process_path") or ""),
                "agent_message_id": str(data.get("id") or ""),
            }
            payload = {
                "text": item["text"],
                "speaker": item.get("speaker") or "",
                "script_id": item.get("script_id") or "",
                "metadata": metadata,
            }
            if self._is_duplicate_bridge_line(payload):
                continue
            result = await self._runtime_line(payload)
            result = result if isinstance(result, dict) else {}
            matched_script_id = _runtime_result_script_id(result)
            self._mark_bridge_line_seen(payload, matched_script_id)
            if str(result.get("status") or "") == "ignored":
                logger.debug(
                    "[VNLaunch] Agent WebSocket bridge runtime ignored chars=%s incoming_script_id=%s matched_script_id=%s reason=%s",
                    len(payload["text"]),
                    payload.get("script_id") or "",
                    matched_script_id,
                    result.get("reason") or "",
                )
                continue
            self._line_count += 1
            self._state["bridge"] = {
                "status": "running",
                "lineCount": self._line_count,
                "source": "agent_websocket",
                "url": self._agent_ws_url,
                "lastTextPreview": payload["text"][:80],
                "lastScriptId": matched_script_id or payload.get("script_id") or "",
            }
            logger.info(
                "[VNLaunch] Agent WebSocket bridge forwarded line #%s chars=%s incoming_script_id=%s matched_script_id=%s",
                self._line_count,
                len(payload["text"]),
                payload.get("script_id") or "",
                matched_script_id,
            )
            await self._publish_status()

    def _bridge_line_keys(self, payload: dict[str, Any], matched_script_id: str = "") -> list[str]:
        text = _bridge_dedupe_text(str(payload.get("text") or ""))
        speaker = _bridge_dedupe_text(str(payload.get("speaker") or ""))
        keys: list[str] = []
        for script_id in (str(payload.get("script_id") or ""), str(matched_script_id or "")):
            if script_id:
                key = f"id:{script_id}"
                if key not in keys:
                    keys.append(key)
        if text:
            keys.append(f"text:{speaker}\n{text}")
        return keys

    def _prune_recent_bridge_lines(self, now: int) -> None:
        self._recent_bridge_lines = {
            item_key: item_at
            for item_key, item_at in self._recent_bridge_lines.items()
            if now - item_at < 20000
        }

    def _remember_bridge_line_keys(self, keys: list[str], now: int) -> None:
        for key in keys:
            self._recent_bridge_lines[key] = now
        if keys:
            self._last_bridge_line_key = keys[0]
            self._last_bridge_line_at = now

    def _mark_bridge_line_seen(self, payload: dict[str, Any], matched_script_id: str = "") -> None:
        keys = self._bridge_line_keys(payload, matched_script_id)
        if not keys:
            return
        now = _now_ms()
        self._prune_recent_bridge_lines(now)
        self._remember_bridge_line_keys(keys, now)

    def _is_duplicate_bridge_line(self, payload: dict[str, Any]) -> bool:
        keys = self._bridge_line_keys(payload)
        if not keys:
            return True
        now = _now_ms()
        self._prune_recent_bridge_lines(now)
        if any(key in self._recent_bridge_lines for key in keys):
            return True
        if any(key == self._last_bridge_line_key for key in keys) and now - self._last_bridge_line_at < 20000:
            return True
        self._remember_bridge_line_keys(keys, now)
        return False

    def _refresh_process_state(self) -> None:
        if self._game_proc is not None:
            game = dict(self._state.get("game") or {})
            game["status"] = "running" if self._process_alive(self._game_proc) else "exited"
            game["pid"] = self._game_proc.pid
            self._state["game"] = game
        if self._agent_proc is not None:
            hook = dict(self._state.get("hook") or {})
            hook["status"] = "running" if self._process_alive(self._agent_proc) else "exited"
            hook["pid"] = self._agent_proc.pid
            self._state["hook"] = hook
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


def _clipboard_text() -> str:
    if os.name != "nt":
        return ""
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    try:
        opened = user32.OpenClipboard(None)
    except Exception:
        logger.exception("[VNLaunch] failed to open clipboard")
        return ""
    if not opened:
        return ""
    try:
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            locked = kernel32.GlobalLock(handle)
            if not locked:
                return ""
            try:
                return ctypes.wstring_at(locked) or ""
            except (OSError, ValueError):
                logger.exception("[VNLaunch] failed to read clipboard unicode text")
                return ""
            finally:
                try:
                    kernel32.GlobalUnlock(handle)
                except Exception:
                    logger.exception("[VNLaunch] failed to unlock clipboard handle")
        except Exception:
            logger.exception("[VNLaunch] clipboard read failed")
            return ""
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            logger.exception("[VNLaunch] failed to close clipboard")


def _prepare_incoming_items(raw: str) -> list[dict[str, Any]]:
    value = str(raw or "").strip()
    if not value or _looks_like_noise(value):
        return []
    parsed = _parse_json_object_stream(value)
    if parsed:
        items: list[dict[str, Any]] = []
        for item in parsed:
            normalized = _normalize_line_item(item)
            if normalized:
                items.append(normalized)
        return items
    return [_normalize_line_item({"text": value})]


def _normalize_line_item(data: dict[str, Any]) -> dict[str, Any]:
    text = _clean_text(data.get("text") or data.get("line") or "")
    if not text:
        return {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    return {
        "text": text,
        "speaker": _clean_text(data.get("speaker") or ""),
        "script_id": str(data.get("script_id") or data.get("scriptId") or metadata.get("script_id") or ""),
        "metadata": metadata,
    }


def _runtime_result_script_id(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return ""
    line = result.get("line")
    if isinstance(line, dict):
        script_id = str(line.get("script_id") or "")
        if script_id:
            return script_id
    reaction = result.get("reaction")
    if isinstance(reaction, dict):
        refs = reaction.get("line_refs")
        if isinstance(refs, dict):
            return str(refs.get("script_id") or refs.get("target_script_id") or "")
    return ""


def _bridge_dedupe_text(value: str) -> str:
    return re.sub(r"\s+", "", _clean_text(value)).lower()


def _parse_json_object_stream(value: str) -> list[dict[str, Any]]:
    text = str(value or "").strip()
    if not text.startswith("{"):
        return []
    decoder = json.JSONDecoder()
    index = 0
    items: list[dict[str, Any]] = []
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        if text[index] != "{":
            return []
        try:
            data, end = decoder.raw_decode(text, index)
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        items.append(data)
        index = end
    return items


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


def _windows_process_infos(image_name: str) -> list[dict[str, Any]]:
    if os.name != "nt" or not image_name:
        return []
    script = (
        "$items = Get-CimInstance Win32_Process -Filter \"Name='"
        + image_name.replace("'", "''")
        + "'\" | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine; "
        "$items | ConvertTo-Json -Compress"
    )
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_no_window:
        kwargs["creationflags"] = creation_no_window
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            **kwargs,
            timeout=4,
            check=False,
        )
    except Exception:
        logger.exception("[VNLaunch] failed to inspect process list for %s", image_name)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        logger.exception("[VNLaunch] failed to parse process list for %s", image_name)
        return []
    if isinstance(parsed, dict):
        parsed_items = [parsed]
    elif isinstance(parsed, list):
        parsed_items = parsed
    else:
        return []
    out: list[dict[str, Any]] = []
    for item in parsed_items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "pid": item.get("ProcessId"),
                "parentPid": item.get("ParentProcessId"),
                "path": item.get("ExecutablePath") or "",
                "commandLine": item.get("CommandLine") or "",
            }
        )
    return out


def _same_windows_path(left: str, right: Path) -> bool:
    if not left:
        return False
    try:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
    except Exception:
        return str(left).strip().lower() == str(right).strip().lower()


def _is_hook_agent_command(command_line: str, hook_script: Path) -> bool:
    cmd = str(command_line or "").lower()
    if "--pname" in cmd or "--script" in cmd:
        return True
    hook = str(hook_script).lower()
    return bool(hook and hook in cmd)


def _agent_switch(name: str, value: str) -> str:
    # Electron's app.commandLine.getSwitchValue() expects Chromium-style
    # `--name=value`; passing `--name value` leaves the value empty.
    return f"--{name}={value}"


def _terminate_windows_pid(pid: int) -> None:
    if os.name != "nt" or pid <= 0:
        return
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    creation_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creation_no_window:
        kwargs["creationflags"] = creation_no_window
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], **kwargs, timeout=5, check=False)


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


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r\n", "\n")).strip()


def _looks_like_noise(text: str) -> bool:
    value = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not value:
        return True
    markers = [
        "traceback (most recent call last):",
        "vn_live_reaction_runner.py",
        "agent-v0.1.4-win32-x64",
        "targetpid:",
        "device: [local]",
        "module ./libmono.js not found",
        "enoent: no such file or directory",
        "=> silence reason=",
        "=> hold reason=",
        "=> speak reason=",
        "powershell ",
        "get-content ",
    ]
    return any(marker in value for marker in markers)
