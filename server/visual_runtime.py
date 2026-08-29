"""Optional visual context runtime for chat turns.

This module is deliberately small and independent from ASR/TTS/wallpaper
ownership. It captures a frame only when vision is enabled and a turn asks for
visual context, then returns a normalized dictionary that LLM adapters can
attach to the current request.
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any

logger = logging.getLogger(__name__)


def _bool_env(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _str_env(key: str, default: str) -> str:
    return os.getenv(key, default).strip() or default


_VISION_TRIGGERS = (
    "看一下",
    "看下",
    "看看",
    "看这个",
    "屏幕",
    "画面",
    "当前页面",
    "当前窗口",
    "这个页面",
    "这个窗口",
    "看到",
    "看得到",
    "能看到",
    "what do you see",
    "look at",
    "look this",
    "screen",
    "current page",
    "current window",
)


@dataclass
class VisionConfig:
    enabled: bool = _bool_env("AMADEUS_VISION_ENABLED", False)
    mode: str = _str_env("AMADEUS_VISION_MODE", "off")
    scope: str = _str_env("AMADEUS_VISION_SCOPE", "full_screen")
    provider: str = _str_env("AMADEUS_VISION_PROVIDER", "auto")
    max_long_side: int = _int_env("AMADEUS_VISION_MAX_LONG_SIDE", 960)
    jpeg_quality: int = _int_env("AMADEUS_VISION_JPEG_QUALITY", 68)
    region: str = _str_env("AMADEUS_VISION_REGION", "")
    window_handle: str = _str_env("AMADEUS_VISION_WINDOW_HANDLE", "")


_config = VisionConfig()


def get_config() -> dict[str, Any]:
    return asdict(_config)


def set_config(values: dict[str, Any]) -> list[str]:
    """Update visual runtime config from system.set_config values."""

    updated: list[str] = []
    aliases = {
        "vision_enabled": "enabled",
        "vision_mode": "mode",
        "vision_scope": "scope",
        "vision_provider": "provider",
        "vision_max_long_side": "max_long_side",
        "vision_jpeg_quality": "jpeg_quality",
        "vision_region": "region",
        "vision_window_handle": "window_handle",
    }
    for raw_key, value in (values or {}).items():
        key = aliases.get(str(raw_key))
        if not key or not hasattr(_config, key):
            continue
        if key == "enabled":
            setattr(_config, key, bool(value))
        elif key in {"max_long_side", "jpeg_quality"}:
            try:
                setattr(_config, key, int(value))
            except (TypeError, ValueError):
                continue
        else:
            setattr(_config, key, str(value or "").strip())
        updated.append(str(raw_key))
    if "vision_enabled" in updated:
        if _config.enabled and _config.mode == "off":
            _config.mode = "on_demand"
        elif not _config.enabled:
            _config.mode = "off"
    if updated:
        logger.info("[VisionRuntime] updated config: %s", {k: getattr(_config, aliases[k]) for k in updated if k in aliases})
    return updated


def list_capture_windows(limit: int = 40) -> list[dict[str, Any]]:
    """Return visible top-level windows that are reasonable capture targets."""

    if os.name != "nt":
        return []

    windows: list[dict[str, Any]] = []
    selected_hwnd = _parse_hwnd(_config.window_handle)
    try:
        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd: int, _lparam: int) -> bool:
            if len(windows) >= limit:
                return False
            hwnd_int = int(hwnd)
            if not _is_capture_candidate_window(hwnd_int):
                return True
            title = _window_text(hwnd_int)
            rect = _window_rect(hwnd_int)
            if not title or not rect:
                return True
            pid = _window_pid(hwnd_int)
            process_name = _process_name(pid) if pid else ""
            windows.append(
                {
                    "hwnd": _format_hwnd(hwnd_int),
                    "title": title,
                    "pid": pid,
                    "processName": process_name,
                    "rect": rect,
                    "selected": bool(selected_hwnd and selected_hwnd == hwnd_int),
                }
            )
            return True

        user32.EnumWindows(enum_proc(callback), 0)
    except Exception:
        logger.exception("[VisionRuntime] failed to list capture windows")
    return windows


def is_enabled() -> bool:
    return bool(_config.enabled and _config.mode != "off")


def is_visual_intent(text: str) -> bool:
    lower = str(text or "").strip().lower()
    if not lower:
        return False
    return any(token in lower for token in _VISION_TRIGGERS)


async def prepare_for_chat_turn(text: str, request: Any = None) -> dict[str, Any] | None:
    """Return VisualContext for this chat turn, or None when no capture is needed."""

    if isinstance(request, dict):
        explicit = bool(request.get("request") or request.get("enabled"))
        mode = str(request.get("mode") or _config.mode).strip() or _config.mode
        scope = str(request.get("scope") or _config.scope).strip() or _config.scope
        provider = str(request.get("provider") or _config.provider).strip() or _config.provider
        attachment = _context_from_user_attachment(request, mode=mode, scope=scope, provider=provider)
        if attachment is not None:
            return attachment
    elif isinstance(request, bool):
        explicit = request
        mode = _config.mode
        scope = _config.scope
        provider = _config.provider
    else:
        explicit = False
        mode = _config.mode
        scope = _config.scope
        provider = _config.provider

    enabled = _config.enabled and mode != "off"
    if not enabled and not explicit:
        return None

    should_capture = explicit
    reason = "manual" if explicit else "user_asked"
    if not should_capture and enabled:
        if mode == "watching":
            should_capture = True
            reason = "turn_start"
        elif mode in {"on_demand", "self_aware"} and is_visual_intent(text):
            should_capture = True
            reason = "user_asked"
    if not should_capture:
        return None

    try:
        return capture_visual_context(
            scope=scope,
            provider=provider,
            mode=mode,
            reason=reason,
        )
    except Exception as exc:
        logger.exception("[VisionRuntime] capture failed")
        return {
            "enabled": True,
            "mode": mode,
            "scope": scope,
            "provider": provider,
            "reason": "capture_failed",
            "capturedAt": _iso_now(),
            "error": str(exc),
        }


def _context_from_user_attachment(
    request: dict[str, Any],
    *,
    mode: str,
    scope: str,
    provider: str,
) -> dict[str, Any] | None:
    frame = request.get("frame")
    if not isinstance(frame, dict):
        return None

    mime = str(frame.get("mime") or "image/jpeg").strip() or "image/jpeg"
    data_url = str(frame.get("dataUrl") or "").strip()
    data_base64 = str(frame.get("dataBase64") or "").strip()
    if data_url and not data_base64:
        marker = ";base64,"
        if marker in data_url:
            data_base64 = data_url.split(marker, 1)[1].strip()
            prefix = data_url.split(marker, 1)[0]
            if prefix.startswith("data:"):
                mime = prefix[5:] or mime
    if data_base64 and not data_url:
        data_url = f"data:{mime};base64,{data_base64}"
    if not data_base64:
        return None

    attachment = request.get("attachment") if isinstance(request.get("attachment"), dict) else {}
    try:
        width = int(frame.get("width") or 0)
    except (TypeError, ValueError):
        width = 0
    try:
        height = int(frame.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    try:
        byte_length = int(frame.get("byteLength") or len(base64.b64decode(data_base64, validate=False)))
    except Exception:
        byte_length = 0

    return {
        "enabled": True,
        "mode": mode or "attachment",
        "scope": scope or "user_image",
        "actualScope": "user_image",
        "provider": provider,
        "reason": "attachment",
        "capturedAt": _iso_now(),
        "attachment": {
            "name": str(attachment.get("name") or request.get("name") or "image"),
            "byteLength": byte_length,
        },
        "frame": {
            "mime": mime,
            "dataBase64": data_base64,
            "dataUrl": data_url,
            "width": width,
            "height": height,
            "byteLength": byte_length,
        },
    }


def capture_visual_context(
    *,
    scope: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    """Capture and compress one screenshot frame."""

    requested_scope = (scope or _config.scope or "full_screen").strip()
    provider = (provider or _config.provider or "auto").strip()
    mode = (mode or _config.mode or "on_demand").strip()

    image, region, actual_scope = _capture_image(requested_scope)

    resized = _resize_for_provider(image, max_long_side=max(320, int(_config.max_long_side or 960)))
    buffer = io.BytesIO()
    quality = max(35, min(92, int(_config.jpeg_quality or 68)))
    resized.save(buffer, format="JPEG", quality=quality, optimize=True)
    jpg = buffer.getvalue()
    b64 = base64.b64encode(jpg).decode("ascii")
    width, height = resized.size

    return {
        "enabled": True,
        "mode": mode,
        "scope": requested_scope,
        "actualScope": actual_scope,
        "provider": provider,
        "reason": reason,
        "capturedAt": _iso_now(),
        "frame": {
            "mime": "image/jpeg",
            "dataBase64": b64,
            "dataUrl": f"data:image/jpeg;base64,{b64}",
            "width": width,
            "height": height,
            "byteLength": len(jpg),
        },
        "capture": {
            "left": int(region["left"]),
            "top": int(region["top"]),
            "width": int(region["width"]),
            "height": int(region["height"]),
        },
    }


def _resolve_capture_region(scope: str, monitor_all: dict[str, int]) -> dict[str, int]:
    scope = (scope or "current_window").strip().lower()
    if scope == "region":
        parsed = _parse_region(_config.region)
        if parsed:
            parsed["_actual_scope"] = "region"
            return parsed
        logger.warning("[VisionRuntime] vision_region is invalid; falling back to full_screen")

    if scope in {"selected_window", "window"}:
        rect = _configured_window_rect()
        if rect:
            clamped = _clamp_region(rect, monitor_all)
            if clamped:
                clamped["_actual_scope"] = "selected_window"
                return clamped
        logger.warning("[VisionRuntime] selected window capture unavailable; falling back to full_screen")

    if scope in {"current_window", "browser_view"}:
        rect = _foreground_window_rect()
        if rect:
            clamped = _clamp_region(rect, monitor_all)
            if clamped:
                clamped["_actual_scope"] = "current_window"
                return clamped
        logger.warning("[VisionRuntime] active window capture unavailable; falling back to full_screen")

    if scope == "wallpaper_surface":
        parsed = _parse_region(_config.region)
        if parsed:
            parsed["_actual_scope"] = "wallpaper_surface"
            return parsed

    region = {
        "left": int(monitor_all.get("left", 0)),
        "top": int(monitor_all.get("top", 0)),
        "width": int(monitor_all.get("width", 1)),
        "height": int(monitor_all.get("height", 1)),
        "_actual_scope": "full_screen",
    }
    return region


def _capture_image(requested_scope: str):
    from PIL import ImageGrab, Image

    normalized_scope = (requested_scope or "full_screen").strip().lower()

    try:
        import mss

        with mss.mss() as sct:
            monitor_all = dict(sct.monitors[0])
            region = _resolve_capture_region(requested_scope, monitor_all)
            actual_scope = str(region.get("_actual_scope") or requested_scope)
            grab_region = {
                "left": int(region["left"]),
                "top": int(region["top"]),
                "width": int(region["width"]),
                "height": int(region["height"]),
            }
            raw = sct.grab(grab_region)
            image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            return image, region, actual_scope
    except ModuleNotFoundError:
        logger.warning("[VisionRuntime] mss is unavailable; using PIL.ImageGrab fallback")

    if normalized_scope in {"selected_window", "window"}:
        rect = _configured_window_rect()
        if rect:
            bbox = (
                int(rect["left"]),
                int(rect["top"]),
                int(rect["left"] + rect["width"]),
                int(rect["top"] + rect["height"]),
            )
            image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
            region = {**rect, "_actual_scope": "selected_window"}
            return image, region, "selected_window"

    if normalized_scope in {"current_window", "browser_view"}:
        rect = _foreground_window_rect()
        if rect:
            bbox = (
                int(rect["left"]),
                int(rect["top"]),
                int(rect["left"] + rect["width"]),
                int(rect["top"] + rect["height"]),
            )
            image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
            region = {**rect, "_actual_scope": "current_window"}
            return image, region, "current_window"

    parsed = _parse_region(_config.region) if normalized_scope in {"region", "wallpaper_surface"} else None
    if parsed:
        bbox = (
            int(parsed["left"]),
            int(parsed["top"]),
            int(parsed["left"] + parsed["width"]),
            int(parsed["top"] + parsed["height"]),
        )
        image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
        parsed["_actual_scope"] = normalized_scope
        return image, parsed, normalized_scope

    image = ImageGrab.grab(all_screens=True).convert("RGB")
    region = {"left": 0, "top": 0, "width": image.width, "height": image.height, "_actual_scope": "full_screen"}
    return image, region, "full_screen"


def _parse_region(value: str) -> dict[str, int] | None:
    if not value:
        return None
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    if len(parts) != 4:
        return None
    try:
        left, top, width, height = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _parse_hwnd(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text, 0)
    except ValueError:
        return 0


def _format_hwnd(hwnd: int) -> str:
    return f"0x{int(hwnd):X}"


def _window_text(hwnd: int) -> str:
    try:
        user32 = ctypes.windll.user32
        length = int(user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd)))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, length + 1)
        return str(buffer.value or "").strip()
    except Exception:
        return ""


def _window_rect(hwnd: int) -> dict[str, int] | None:
    try:
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 40 or height <= 40:
            return None
        return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}
    except Exception:
        return None


def _window_pid(hwnd: int) -> int:
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    handle = None
    try:
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ""
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value)
    except Exception:
        return ""
    finally:
        try:
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _is_window_cloaked(hwnd: int) -> bool:
    try:
        cloaked = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd),
            14,  # DWMWA_CLOAKED
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return result == 0 and bool(cloaked.value)
    except Exception:
        return False


def _is_capture_candidate_window(hwnd: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        hwnd_ptr = ctypes.c_void_p(hwnd)
        if not user32.IsWindow(hwnd_ptr):
            return False
        if not user32.IsWindowVisible(hwnd_ptr):
            return False
        if user32.IsIconic(hwnd_ptr):
            return False
        if _is_window_cloaked(hwnd):
            return False
        title = _window_text(hwnd)
        if not title or title == "Program Manager":
            return False
        return _window_rect(hwnd) is not None
    except Exception:
        return False


def _configured_window_rect() -> dict[str, int] | None:
    if os.name != "nt":
        return None
    hwnd = _parse_hwnd(_config.window_handle)
    if not hwnd:
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd_ptr = ctypes.c_void_p(hwnd)
        if not user32.IsWindow(hwnd_ptr) or user32.IsIconic(hwnd_ptr):
            return None
        return _window_rect(hwnd)
    except Exception:
        return None


def _foreground_window_rect() -> dict[str, int] | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 40 or height <= 40:
            return None
        return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}
    except Exception:
        return None


def _clamp_region(region: dict[str, int], bounds: dict[str, int]) -> dict[str, int] | None:
    left_bound = int(bounds.get("left", 0))
    top_bound = int(bounds.get("top", 0))
    right_bound = left_bound + int(bounds.get("width", 0))
    bottom_bound = top_bound + int(bounds.get("height", 0))
    left = max(left_bound, int(region["left"]))
    top = max(top_bound, int(region["top"]))
    right = min(right_bound, int(region["left"]) + int(region["width"]))
    bottom = min(bottom_bound, int(region["top"]) + int(region["height"]))
    width = right - left
    height = bottom - top
    if width <= 40 or height <= 40:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def _resize_for_provider(image, *, max_long_side: int):
    width, height = image.size
    long_side = max(width, height)
    if long_side <= max_long_side:
        return image
    scale = max_long_side / float(long_side)
    next_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    try:
        from PIL import Image

        resample = Image.Resampling.LANCZOS
    except Exception:
        resample = 1
    return image.resize(next_size, resample)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int((time.time() % 1) * 1000):03d}"
