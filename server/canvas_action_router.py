"""Route user actions emitted by the CRT canvas surface.

The CRT canvas is an OS-level surface: it renders provider artifacts and emits
small user intents. It should not know how to run individual providers or
local shell actions directly. This router is the boundary between renderer
interaction and runtime execution.
"""

from __future__ import annotations

import inspect
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.event_bus import bus
from server.protocol import Method


_UNSAFE_CANVAS_OPEN_SUFFIXES = frozenset(
    {
        ".appref-ms", ".bat", ".bash", ".chm", ".cmd", ".com", ".cpl",
        ".exe", ".gadget", ".hta", ".htm", ".html", ".jar", ".js", ".jse",
        ".lnk", ".msi", ".msp", ".pl", ".ps1", ".psm1", ".py", ".pyw",
        ".rb", ".reg", ".scr", ".sh", ".svg", ".url", ".vbe", ".vbs",
        ".wsf", ".wsh", ".xhtml", ".zsh",
    }
)


class CanvasActionRouter:
    """Interpret canvas actions and dispatch them to local/runtime capabilities."""

    def __init__(
        self,
        provider_run: Callable[[dict[str, Any]], Any] | None = None,
        work_action: Callable[[dict[str, Any]], Any] | None = None,
        provider_inspect: Callable[[dict[str, Any]], Any] | None = None,
        context_action: Callable[[dict[str, Any]], Any] | None = None,
        attention_action: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self._provider_run = provider_run
        self._work_action = work_action
        self._provider_inspect = provider_inspect
        self._context_action = context_action
        self._attention_action = attention_action

    async def route(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = payload or {}
        target = str(data.get("target") or data.get("kind") or "").strip().lower().replace("-", "_")
        action = str(data.get("action") or "").strip().lower().replace("-", "_")
        if not target:
            return {"ok": False, "error": "missing_target"}
        if not action:
            return {"ok": False, "error": "missing_action"}

        if target == "file":
            return self._file_action(action, data)
        if target == "url":
            return self._url_action(action, data)
        if target == "command":
            return self._command_action(action, data)
        if target == "browser":
            return await self._browser_action(action, data)
        if target == "provider":
            return await self._provider_action(action, data)
        if target == "work_item":
            return await self._work_item_action(action, data)
        if target == "work_destination":
            return await self._work_destination_action(action, data)
        if target == "permission":
            return await self._permission_action(action, data)
        if target == "attention":
            return await self._attention_action_route(action, data)
        if target in {"conversation", "session_context"}:
            return await self._context_action_route(action, data)
        return {"ok": False, "error": "unsupported_target"}

    async def _attention_action_route(
        self,
        action: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in {"presented", "resolve"}:
            return {"ok": False, "error": "unsupported_action"}
        if self._attention_action is None:
            return {"ok": False, "error": "attention_control_plane_unavailable"}
        request_id = str(
            data.get("request_id") or data.get("requestId") or ""
        ).strip()
        if not request_id:
            return {"ok": False, "error": "missing_attention_request_id"}
        payload = {
            "target": "attention",
            "action": action,
            "request_id": request_id,
        }
        if action == "resolve":
            option_id = str(
                data.get("option_id") or data.get("optionId") or ""
            ).strip()
            if not option_id:
                return {"ok": False, "error": "missing_attention_option_id"}
            payload["option_id"] = option_id
        result = self._attention_action(payload)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    async def _context_action_route(
        self,
        action: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in {"open_project", "open_work_item"}:
            return {"ok": False, "error": "unsupported_action"}
        if self._context_action is None:
            return {"ok": False, "error": "session_context_unavailable"}
        payload = {
            "target": "session_context",
            "action": action,
            "project_id": str(data.get("project_id") or data.get("projectId") or ""),
            "work_item_id": str(data.get("work_item_id") or data.get("workItemId") or ""),
        }
        result = self._context_action(payload)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    async def _work_item_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        # The wallpaper may request only bounded WorkItem intents.  Execution
        # actions are revalidated by WorkLedgerHandler against the canonical
        # revision, selected item, latest attempt, and advertised capability.
        if action not in {
            "select",
            "set_focus",
            "retry",
            "resume",
            "open_preview",
            "accept",
            "archive",
            "reopen",
            "promote_to_project",
        }:
            return {"ok": False, "error": "unsupported_action"}
        if self._work_action is None:
            return {"ok": False, "error": "work_control_plane_unavailable"}
        if action == "open_preview":
            # Preview is resolved entirely by the trusted Host from durable
            # WorkItem identity.  In particular, never forward a Canvas-authored
            # path, URL, port, command, provider, or project alias here.
            work_item_id = str(
                data.get("work_item_id") or data.get("workItemId") or ""
            ).strip()
            if not work_item_id:
                return {"ok": False, "error": "missing_work_item_id"}
            attempt_id = str(
                data.get("attempt_id") or data.get("attemptId") or ""
            ).strip()
            if not attempt_id:
                return {"ok": False, "error": "missing_attempt_id"}
            revision = str(
                data.get("revision") or data.get("surface_revision") or ""
            ).strip()
            if not revision:
                return {"ok": False, "error": "missing_revision"}
            payload = {
                "target": "work_item",
                "action": action,
                "work_item_id": work_item_id,
                "attempt_id": attempt_id,
                "revision": revision,
            }
            result = self._work_action(payload)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, dict) else {"ok": True, "result": result}
        # Do not forward provider, task, mode, cwd, or arbitrary metadata from
        # the untrusted Canvas. Retry may carry only its explicitly bounded
        # amendment text; the host reconstructs the instruction and lineage.
        payload = {
            "target": "work_item",
            "action": action,
            "project_id": str(data.get("project_id") or data.get("projectId") or ""),
            "work_item_id": str(data.get("work_item_id") or data.get("workItemId") or ""),
            "run_id": str(data.get("run_id") or data.get("runId") or ""),
            "attempt_id": str(data.get("attempt_id") or data.get("attemptId") or ""),
            "revision": str(data.get("revision") or data.get("surface_revision") or ""),
            "focus_mode": str(data.get("focus_mode") or data.get("focusMode") or ""),
        }
        if action == "retry":
            amendment = data.get("amendment_text")
            if amendment is None and "amendmentText" in data:
                amendment = data.get("amendmentText")
            if amendment is not None:
                payload["amendment_text"] = amendment
            authorization_id = data.get("authorization_permission_request_id")
            if authorization_id is None and "authorizationPermissionRequestId" in data:
                authorization_id = data.get("authorizationPermissionRequestId")
            if authorization_id is not None:
                payload["authorization_permission_request_id"] = authorization_id
        result = self._work_action(payload)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    async def _work_destination_action(
        self,
        action: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Forward the single recovery action exposed beside destination state."""

        if action != "exit_project":
            return {"ok": False, "error": "unsupported_action"}
        if self._work_action is None:
            return {"ok": False, "error": "work_control_plane_unavailable"}
        payload = {
            "target": "work_destination",
            "action": action,
            "revision": str(data.get("revision") or data.get("surface_revision") or ""),
        }
        result = self._work_action(payload)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    async def _permission_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "allow": "allow_once",
            "approve_once": "allow_once",
            "reject": "deny",
        }.get(action, action)
        if normalized not in {"allow_once", "deny", "retry_export", "abandon_export"}:
            return {"ok": False, "error": "unsupported_action"}
        if self._work_action is None:
            return {"ok": False, "error": "work_control_plane_unavailable"}
        request_id = str(
            data.get("permission_request_id")
            or data.get("permissionRequestId")
            or data.get("request_id")
            or ""
        ).strip()
        if not request_id:
            return {"ok": False, "error": "missing_permission_request_id"}
        work_item_id = str(data.get("work_item_id") or data.get("workItemId") or "").strip()
        if not work_item_id:
            return {"ok": False, "error": "missing_work_item_id"}
        attempt_id = str(data.get("attempt_id") or data.get("attemptId") or "").strip()
        if not attempt_id:
            return {"ok": False, "error": "missing_attempt_id"}
        revision = str(data.get("revision") or data.get("surface_revision") or "").strip()
        if not revision:
            return {"ok": False, "error": "missing_revision"}
        payload = {
            "target": "permission",
            "action": normalized,
            "permission_request_id": request_id,
            "project_id": str(data.get("project_id") or data.get("projectId") or ""),
            "work_item_id": work_item_id,
            "run_id": str(data.get("run_id") or data.get("runId") or ""),
            "attempt_id": attempt_id,
            "revision": revision,
        }
        result = self._work_action(payload)
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, dict) else {"ok": True, "result": result}

    def _file_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action not in {"open", "folder", "open_with"}:
            return {"ok": False, "error": "unsupported_action"}
        raw_path = str(data.get("path") or "").strip().strip("\"'")
        if not raw_path or "://" in raw_path:
            return {"ok": False, "error": "invalid_path"}
        if raw_path.startswith(("\\\\", "//")):
            return {"ok": False, "error": "network_path_not_allowed"}

        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            return {"ok": False, "error": "path_must_be_absolute"}
        shell_suffix = Path(path.name.rstrip(" .")).suffix.lower()
        if action in {"open", "open_with"} and shell_suffix in _UNSAFE_CANVAS_OPEN_SUFFIXES:
            return {"ok": False, "error": "unsafe_file_type", "path": str(path)}
        if action in {"open", "open_with"} and not path.exists():
            return {"ok": False, "error": "path_not_found", "path": str(path)}
        if action == "folder" and not (path.exists() or path.parent.exists()):
            return {"ok": False, "error": "folder_not_found", "path": str(path)}

        try:
            if action == "folder":
                self._show_path_in_folder(path)
            elif action == "open_with":
                self._open_with_dialog(path)
            else:
                self._open_path_default(path)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": str(path)}
        return {"ok": True, "target": "file", "action": action, "path": str(path)}

    def _url_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action not in {"open", "source"}:
            return {"ok": False, "error": "unsupported_action"}
        try:
            url = self._sanitize_source_url(str(data.get("url") or ""))
            if action == "open":
                webbrowser.open(url, new=2, autoraise=True)
                return {"ok": True, "target": "url", "action": action, "url": url}
            source_path = self._make_source_chip(url)
            return {
                "ok": True,
                "target": "url",
                "action": action,
                "url": url,
                "sourcePath": str(source_path),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _command_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        # Provider-rendered Canvas content is not a trusted command authority.
        # It may prepare a chip for inspection, but never execute its text.
        if action != "make_bat":
            return {"ok": False, "error": "unsupported_action"}
        command = str(data.get("command") or "").strip()
        cwd = data.get("cwd")
        try:
            bat_path = self._make_command_bat(command, str(cwd) if cwd else None)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "target": "command", "action": action, "batPath": str(bat_path)}

    async def _browser_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action == "snapshot":
            action = "observe"
        if action not in {"open", "observe", "click_text"}:
            return {"ok": False, "error": "unsupported_action"}
        if self._provider_run is None:
            return {"ok": False, "error": "provider_runtime_unavailable"}

        url = str(data.get("url") or "").strip()
        text = str(data.get("text") or data.get("label") or "").strip()
        session_id = str(
            data.get("browserSessionId")
            or data.get("browser_session_id")
            or ""
        ).strip()
        metadata: dict[str, Any] = {
            "source": "canvas_action",
            "browser_action": action,
            "browser_mode": action,
            "browser_session_id": session_id,
        }
        if url:
            metadata["url"] = url
        if text:
            metadata["text"] = text
            metadata["label"] = text

        task = {
            "open": f"Open {url} in the current browser session" if url else "Open source in the current browser session",
            "observe": "Observe the current browser page",
            "click_text": f"Click {text} in the current browser page" if text else "Click text in the current browser page",
        }[action]
        result = self._provider_run(
            {
                "provider": "browser",
                "task": task,
                "mode": action,
                "metadata": metadata,
            }
        )
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "target": "browser", "action": action, "result": result}

    async def _provider_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        if action not in {"open_details", "view_diff"}:
            return {"ok": False, "error": "unsupported_action"}

        inspector = self._provider_inspect
        if inspector is not None:
            inspected = inspector({**data, "action": action})
            if inspect.isawaitable(inspected):
                inspected = await inspected
            if isinstance(inspected, dict) and inspected.get("handled"):
                return {
                    "target": "provider",
                    "action": action,
                    **inspected,
                }

        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        provider = str(data.get("provider") or metadata.get("provider") or "provider").strip() or "provider"
        run_id = str(data.get("run_id") or data.get("runId") or metadata.get("run_id") or metadata.get("runId") or "").strip()
        cwd = str(data.get("cwd") or metadata.get("cwd") or "").strip()
        ref = str(data.get("ref") or metadata.get("ref") or run_id).strip()
        label = str(data.get("label") or metadata.get("label") or "").strip()
        payload = {
            "action": action,
            "provider": provider,
            "run_id": run_id,
            "cwd": cwd,
            "ref": ref,
            "label": label,
        }
        await bus.emit(
            Method.PROVIDER_EVENT,
            {
                "provider": provider,
                "run_id": run_id,
                "type": "canvas.action",
                "payload": payload,
                "metadata": {"source": "wallpaper.canvas", "target": "provider", "action": action},
            },
        )
        return {"ok": True, "target": "provider", "action": action, "provider": provider, "run_id": run_id}

    @staticmethod
    def _open_path_default(path: Path) -> None:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    @staticmethod
    def _show_path_in_folder(path: Path) -> None:
        target = path if path.is_dir() else path.parent
        if sys.platform.startswith("win"):
            if path.exists() and path.is_file():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def _open_with_dialog(self, path: Path) -> None:
        if sys.platform.startswith("win"):
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(path)])
        else:
            self._open_path_default(path)

    @staticmethod
    def _source_chip_path() -> Path:
        root = Path(tempfile.gettempdir()) / "amadeus-source-chips"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"amadeus_source_{int(time.time())}_{secrets.token_hex(3)}.url"

    @staticmethod
    def _command_bat_path() -> Path:
        root = Path(tempfile.gettempdir()) / "amadeus-command-chips"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"amadeus_cmd_{int(time.time())}_{secrets.token_hex(3)}.bat"

    @classmethod
    def _make_source_chip(cls, url: str) -> Path:
        safe_url = cls._sanitize_source_url(url)
        path = cls._source_chip_path()
        path.write_text("[InternetShortcut]\r\nURL=" + safe_url + "\r\n", encoding="utf-8-sig")
        return path

    @classmethod
    def _make_command_bat(cls, command: str, cwd: str | None = None) -> Path:
        text = str(command or "").strip()
        if not text:
            raise ValueError("empty_command")
        if len(text) > 12000:
            raise ValueError("command_too_long")

        cwd_path: Path | None = None
        if cwd:
            candidate = Path(str(cwd)).expanduser()
            if candidate.is_absolute() and candidate.is_dir():
                cwd_path = candidate

        bat_path = cls._command_bat_path()
        lines = [
            "@echo off",
            "chcp 65001 >nul",
            "title Amadeus command chip",
            "echo [Amadeus] Command chip",
        ]
        if cwd_path is not None:
            lines.append(f'cd /d "{cwd_path}"')
        lines.extend(
            [
                "echo.",
                text,
                "echo.",
                "echo [Amadeus] Command finished with exit code %ERRORLEVEL%.",
            ]
        )
        bat_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig")
        return bat_path

    @staticmethod
    def _sanitize_source_url(raw_url: str) -> str:
        text = str(raw_url or "").strip()
        if text.lower().startswith("www."):
            text = "https://" + text
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("invalid_url")
        return urllib.parse.urlunparse(parsed)
