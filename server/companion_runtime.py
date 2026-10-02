"""Companion runtime: the portrait card process and its speech surface.

Companion mode reuses the existing repository-owned Tk window
(``render/vn_overlay_window.py`` via ``tools/vn_portrait_overlay_lite.py``) as the
only visible surface. This module owns that one child process and publishes the
card's endpoints so the shared TTS bridge can drive its captions, emotion and
speaking state.

The card is not a VN player session: no game, no profile, no Work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from urllib import request

from server.local_auth import (
    AUTH_MODE_ENV,
    AUTH_TOKEN_ENV,
    INSTANCE_NONCE_ENV,
    LocalAuthPolicy,
    clear_inherited_auth_environment,
)

logger = logging.getLogger(__name__)

DEFAULT_CARD_HOST = "127.0.0.1"
DEFAULT_CARD_PORT = 8788
CARD_READY_TIMEOUT_S = 15.0
CARD_HEALTH_TIMEOUT_S = 0.35
CARD_LOG_RELATIVE_PATH = Path("runtime") / "companion" / "card.log"
CARD_LOG_TAIL_BYTES = 4096

# The card is the companion's speech surface. The shared TTS bridge reads this
# when a speech payload carries no session-specific overlay URL.
_DEFAULT_OVERLAY_URL = ""


def default_overlay_url() -> str:
    """Overlay endpoint every companion speech payload falls back to."""
    return _DEFAULT_OVERLAY_URL


def set_default_overlay_url(url: str) -> None:
    global _DEFAULT_OVERLAY_URL
    _DEFAULT_OVERLAY_URL = str(url or "").strip()


def desktop_credential_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Capture the desktop credential before Bootstrap clears our environment.

    The card's voice/vision controls call this same backend, so they need the
    credential this process serves. An empty result means loopback development
    mode with authentication disabled.
    """

    mode = str(environ.get(AUTH_MODE_ENV) or "").strip()
    token = str(environ.get(AUTH_TOKEN_ENV) or "").strip()
    nonce = str(environ.get(INSTANCE_NONCE_ENV) or "").strip()
    if not token or not nonce:
        return {}
    return {AUTH_MODE_ENV: mode or "required", AUTH_TOKEN_ENV: token, INSTANCE_NONCE_ENV: nonce}


def card_health_ok(
    health_url: str,
    timeout: float = CARD_HEALTH_TIMEOUT_S,
    *,
    require_companion_controls: bool = False,
    expected_backend_url: str = "",
) -> bool:
    if not health_url:
        return False
    try:
        with request.urlopen(request.Request(health_url, method="GET"), timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return False
            raw = response.read(4096)
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if require_companion_controls and not payload.get("companion_controls"):
            return False
        if expected_backend_url and str(payload.get("backend_url") or "") != expected_backend_url:
            return False
        return True
    except Exception:
        return False


def post_card_visibility(url: str, visible: bool, timeout: float = 2.0) -> bool:
    """The card's own visibility endpoint, derived from its reaction URL."""
    return _post_card_endpoint(url, "visibility", {"visible": bool(visible)}, timeout)


def post_card_focus(url: str, timeout: float = 2.0) -> bool:
    """Raise an already running card, so a repeated launch never adds a second."""
    return _post_card_endpoint(url, "focus", {}, timeout)


def _post_card_endpoint(url: str, name: str, payload: dict[str, Any], timeout: float) -> bool:
    if not url:
        return False
    endpoint = url.rsplit("/", 1)[0] + "/" + name
    body = json.dumps(payload).encode("utf-8")
    try:
        with request.urlopen(
            request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            ),
            timeout=timeout,
        ) as response:
            response.read(256)
        return True
    except Exception:
        logger.debug("[companion] card %s update failed: %s", name, endpoint, exc_info=True)
        return False


class CompanionCardHost:
    """Owns the companion card process for this backend lifetime."""

    def __init__(
        self,
        project_root: Path,
        *,
        backend_url: str,
        auth_policy: LocalAuthPolicy,
        credential_environment: Mapping[str, str] | None = None,
        python: str = "",
        host: str = DEFAULT_CARD_HOST,
        port: int = DEFAULT_CARD_PORT,
        x: int = 60,
        y: int = 80,
    ) -> None:
        self.project_root = Path(project_root)
        self.backend_url = str(backend_url or "")
        self.auth_policy = auth_policy
        self.credential_environment = dict(credential_environment or {})
        self.host = host
        self.port = int(port)
        self.x = int(x)
        self.y = int(y)
        self._proc: subprocess.Popen[Any] | None = None
        self._adopted = False
        self._visible = True
        self._python = str(python or "")
        self._log_file: Any = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/reaction"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    def _process_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict[str, Any]:
        running = self._process_alive() or self._adopted
        return {
            "status": "running" if running else "not_started",
            "running": running,
            "owned": self._process_alive(),
            "pid": self._proc.pid if self._process_alive() and self._proc is not None else None,
            "visible": self._visible,
            "url": self.url,
            "healthUrl": self.health_url,
        }

    def _helper_path(self) -> Path:
        return self.project_root / "tools" / "vn_portrait_overlay_lite.py"

    def _interpreter(self) -> str:
        """Choose an interpreter with both Tk and Pillow for the card process."""
        if self._python:
            return self._python
        candidates = [
            os.environ.get("VN_OVERLAY_PYTHON", "").strip(),
            sys.executable,
            shutil.which("pythonw") or "",
            shutil.which("python") or "",
        ]
        seen: set[str] = set()
        rejected: list[str] = []
        for raw in candidates:
            if not raw:
                continue
            path = str(Path(raw).resolve())
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            if not Path(path).is_file():
                rejected.append(f"{path} (not a file)")
                continue
            try:
                probe = subprocess.run(
                    [path, "-c", "import tkinter, PIL"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                rejected.append(f"{path} (probe timed out)")
                continue
            except OSError as error:
                rejected.append(f"{path} ({error})")
                continue
            if probe.returncode == 0:
                logger.info("[companion] card interpreter: %s", path)
                self._python = path
                return path
            reason = (probe.stderr or probe.stdout or b"").decode("utf-8", "replace").strip()
            last_line = reason.splitlines()[-1] if reason else f"exit {probe.returncode}"
            rejected.append(f"{path} ({last_line})")
        logger.warning(
            "[companion] no Python with tkinter+Pillow found; the card will fail to start. "
            "Set VN_OVERLAY_PYTHON to an interpreter that has both. Rejected: %s",
            "; ".join(rejected) or "no candidates",
        )
        self._python = str(Path(sys.executable).resolve())
        return self._python

    def _child_environment(self) -> dict[str, str]:
        child_env = os.environ.copy()
        clear_inherited_auth_environment(child_env)
        self._interpreter()
        if self.backend_url:
            # Card controls (voice/vision) call this backend, so the card needs
            # the same desktop credential this process serves. Prefer the
            # credential captured before Bootstrap cleared our environment; an
            # injected policy (VN-style callers) covers launches without one.
            archive = self.credential_environment
            token = str(archive.get(AUTH_TOKEN_ENV) or "").strip() or self.auth_policy.token
            nonce = str(archive.get(INSTANCE_NONCE_ENV) or "").strip() or self.auth_policy.instance_nonce
            if token and nonce:
                child_env.update({
                    AUTH_MODE_ENV: "required",
                    AUTH_TOKEN_ENV: token,
                    INSTANCE_NONCE_ENV: nonce,
                })
            else:
                child_env[AUTH_MODE_ENV] = "disabled"
        return child_env

    def _spawn(self, args: list[str]) -> subprocess.Popen[Any]:
        log_path = self.card_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w", encoding="utf-8", buffering=1)
        except OSError as error:
            # A card that cannot be diagnosed is worse than a card without a
            # log, but it must never block startup; say so and keep going.
            logger.warning("[companion] card log unavailable (%s): %s", log_path, error)
            log_file = None
        self._log_file = log_file
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_root),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file or subprocess.DEVNULL,
            "stderr": log_file or subprocess.DEVNULL,
            "shell": False,
            "env": self._child_environment(),
        }
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            kwargs["startupinfo"] = startup
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(args, **kwargs)

    def card_log_path(self) -> Path:
        return self.project_root / CARD_LOG_RELATIVE_PATH

    def card_log_tail(self) -> str:
        """Last lines of the card's own stdout/stderr, for a failed start."""
        path = self.card_log_path()
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        try:
            with path.open("rb") as handle:
                if size > CARD_LOG_TAIL_BYTES:
                    handle.seek(size - CARD_LOG_TAIL_BYTES)
                raw = handle.read(CARD_LOG_TAIL_BYTES)
        except OSError:
            return ""
        return raw.decode("utf-8", "replace").strip()

    async def ensure_running(self) -> dict[str, Any]:
        helper = self._helper_path()
        if not helper.is_file():
            raise FileNotFoundError(f"Companion card helper not found: {helper}")
        if self._process_alive():
            return self.status()
        if await asyncio.to_thread(
            card_health_ok,
            self.health_url,
            require_companion_controls=True,
            expected_backend_url=self.backend_url,
        ):
            # A compatible card from a previous launch already owns the port.
            # Adopt the presentation surface instead of creating a second card.
            self._adopted = True
            self._publish_surface_url()
            return self.status()

        try:
            from core.character_profile import active_art_dir

            configured_art_dir = Path(active_art_dir())
        except Exception:
            configured_art_dir = Path()
        lite_dir = configured_art_dir if (configured_art_dir / "manifest.json").is_file() else (
            self.project_root / "assets" / "companion" / "kurisu"
        )
        args = [
            self._interpreter(),
            str(helper),
            "--host", self.host,
            "--port", str(self.port),
            "--lite-dir", str(lite_dir),
            "--x", str(self.x),
            "--y", str(self.y),
            "--on-close", "card-close",
            "--companion-controls",
        ]
        if self.backend_url:
            args.extend(["--backend-url", self.backend_url])
        self._proc = self._spawn(args)
        self._adopted = False
        deadline = time.monotonic() + CARD_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if not self._process_alive():
                exit_code = self._proc.poll() if self._proc else None
                message = self._failure_message(
                    f"Companion card exited before becoming ready (code {exit_code})"
                )
                self._close_log()
                raise RuntimeError(message)
            if await asyncio.to_thread(
                card_health_ok,
                self.health_url,
                require_companion_controls=True,
                expected_backend_url=self.backend_url,
            ):
                self._visible = True
                self._publish_surface_url()
                logger.info(
                    "[companion] card ready pid=%s url=%s log=%s",
                    self._proc.pid if self._proc else None,
                    self.url,
                    self.card_log_path(),
                )
                return self.status()
            await asyncio.sleep(0.12)
        message = self._failure_message(
            f"Companion card did not become ready: {self.health_url}"
        )
        self._close_log()
        raise RuntimeError(message)

    def _failure_message(self, headline: str) -> str:
        tail = self.card_log_tail()
        log_path = self.card_log_path()
        logger.error("[companion] %s (interpreter=%s, log=%s)", headline, self._python, log_path)
        if tail:
            logger.error("[companion] card output:\n%s", tail)
            return f"{headline}\ninterpreter: {self._python}\nlog: {log_path}\n{tail}"
        return f"{headline}\ninterpreter: {self._python}\nlog: {log_path} (empty)"

    def _close_log(self) -> None:
        log_file = self._log_file
        self._log_file = None
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass

    def _publish_surface_url(self) -> None:
        set_default_overlay_url(self.url)

    async def set_visible(self, visible: bool) -> dict[str, Any]:
        if visible and not (self._process_alive() or self._adopted):
            await self.ensure_running()
        if self._process_alive() or self._adopted:
            await asyncio.to_thread(post_card_visibility, self.url, bool(visible))
        self._visible = bool(visible)
        return self.status()

    async def focus(self) -> dict[str, Any]:
        """Show and raise the one card this launch owns."""
        if not (self._process_alive() or self._adopted):
            await self.ensure_running()
        self._visible = True
        if self._process_alive() or self._adopted:
            await asyncio.to_thread(post_card_focus, self.url)
        return self.status()

    async def stop(self) -> None:
        set_default_overlay_url("")
        self._adopted = False
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            self._close_log()
            return
        logger.info("[companion] terminating card pid=%s", proc.pid)
        try:
            proc.terminate()
            await asyncio.to_thread(proc.wait, 3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                logger.exception("[companion] failed to kill card pid=%s", proc.pid)
        self._close_log()
