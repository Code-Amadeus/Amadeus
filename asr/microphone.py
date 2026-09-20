"""Microphone discovery and selection helpers for ASR and wake word paths."""
from __future__ import annotations

import audioop
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import pyaudio

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_VIRTUAL_NAME_PARTS = (
    "sound mapper",
    "primary sound",
    "stereo mix",
    "what u hear",
    "loopback",
    "wasapi",
    "output",
    "speaker",
    "headphone",
    "digital audio",
)
_BLUETOOTH_MIC_PARTS = (
    "bluetooth",
    "freebuds",
    "headset",
    "hands-free",
    "handsfree",
    "earbuds",
    "耳机",
    "蓝牙",
)
_INTERNAL_MIC_PARTS = (
    "microphone array",
    "mic array",
    "built-in",
    "internal",
    "麦克风阵列",
    "内置",
)


@dataclass
class MicrophoneInfo:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    default_sample_rate: float
    rms: int
    is_virtual: bool
    preferred: bool
    selected: bool = False
    score: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MicDeviceDescriptor:
    index: int | None
    name: str
    host_api: str
    max_input_channels: int
    default_sample_rate: float
    device_class: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_device(name: str, host_api: str = "") -> str:
    text = f"{name or ''} {host_api or ''}".lower()
    if (
        "bluetooth" in text
        or "hands-free" in text
        or "handsfree" in text
        or re.search(r"\bbt\b", text)
    ):
        return "bluetooth"
    if any(part in text for part in ("realtek", "internal", "内置", "microphone array")):
        return "internal"
    if "usb" in text:
        return "usb"
    return "unknown"


def device_descriptor_from_info(info: dict[str, Any], host_api: str = "") -> MicDeviceDescriptor:
    name = str(info.get("name", "") or "")
    host_api = str(host_api or info.get("host_api", "") or info.get("hostApiName", "") or "")
    try:
        index = int(info.get("index", -1))
        if index < 0:
            index = None
    except Exception:
        index = None
    try:
        channels = int(info.get("maxInputChannels", info.get("max_input_channels", 0)) or 0)
    except Exception:
        channels = 0
    try:
        sample_rate = float(info.get("defaultSampleRate", info.get("default_sample_rate", 0.0)) or 0.0)
    except Exception:
        sample_rate = 0.0
    return MicDeviceDescriptor(
        index=index,
        name=name,
        host_api=host_api,
        max_input_channels=channels,
        default_sample_rate=sample_rate,
        device_class=classify_device(name, host_api),
    )


def device_descriptor_for_index(index: int | None, pa: pyaudio.PyAudio | None = None) -> MicDeviceDescriptor:
    if index is None:
        return MicDeviceDescriptor(
            index=None,
            name="",
            host_api="",
            max_input_channels=0,
            default_sample_rate=0.0,
            device_class="unknown",
        )
    owned_pa = pa is None
    if pa is None:
        pa = pyaudio.PyAudio()
    try:
        info = pa.get_device_info_by_index(int(index))
        host_api_index = int(info.get("hostApi", -1))
        host_api = ""
        if host_api_index >= 0:
            try:
                host_api = str(pa.get_host_api_info_by_index(host_api_index).get("name", ""))
            except Exception:
                host_api = ""
        return device_descriptor_from_info(info, host_api)
    except Exception:
        return MicDeviceDescriptor(
            index=index,
            name="",
            host_api="",
            max_input_channels=0,
            default_sample_rate=0.0,
            device_class="unknown",
        )
    finally:
        if owned_pa:
            pa.terminate()


def configured_device_index() -> int | None:
    raw = os.environ.get("MICROPHONE_DEVICE_INDEX", "").strip()
    if raw:
        try:
            index = int(raw)
            return index if index >= 0 else None
        except ValueError:
            pass
    try:
        from config import settings

        index = int(getattr(settings, "MICROPHONE_DEVICE_INDEX", -1))
        return index if index >= 0 else None
    except Exception:
        return None


def configured_preferred_name() -> str:
    raw = os.environ.get("MICROPHONE_PREFERRED_NAME", "").strip()
    if raw:
        return raw
    try:
        from config import settings

        return str(getattr(settings, "MICROPHONE_PREFERRED_NAME", "") or "").strip()
    except Exception:
        return ""


def configured_fallback_device_index() -> int | None:
    raw = os.environ.get("MICROPHONE_FALLBACK_DEVICE_INDEX", "").strip()
    if raw:
        try:
            index = int(raw)
            return index if index >= 0 else None
        except ValueError:
            pass
    try:
        from config import settings

        index = int(getattr(settings, "MICROPHONE_FALLBACK_DEVICE_INDEX", -1))
        return index if index >= 0 else None
    except Exception:
        return None


def configured_fallback_name() -> str:
    raw = os.environ.get("MICROPHONE_FALLBACK_NAME", "").strip()
    if raw:
        return raw
    try:
        from config import settings

        return str(getattr(settings, "MICROPHONE_FALLBACK_NAME", "") or "").strip()
    except Exception:
        return ""


def _is_virtual_input(name: str) -> bool:
    lower = name.lower()
    return any(part in lower for part in _VIRTUAL_NAME_PARTS)


def _device_name_for_index(index: int | None) -> tuple[str, str]:
    if index is None:
        return "", ""
    pa = pyaudio.PyAudio()
    try:
        info = pa.get_device_info_by_index(int(index))
        host_api_index = int(info.get("hostApi", -1))
        host_api = ""
        if host_api_index >= 0:
            try:
                host_api = str(pa.get_host_api_info_by_index(host_api_index).get("name", ""))
            except Exception:
                host_api = ""
        return str(info.get("name", "")), host_api
    except Exception:
        return "", ""
    finally:
        pa.terminate()


def _default_input_device_index(pa: pyaudio.PyAudio) -> int | None:
    try:
        info = pa.get_default_input_device_info()
        index = int(info.get("index", -1))
        return index if index >= 0 else None
    except Exception:
        return None


def _host_api_priority(host_api: str) -> int:
    lower = host_api.lower()
    if "directsound" in lower:
        return 4
    if "wasapi" in lower:
        return 3
    if "mme" in lower:
        return 2
    if "wdm-ks" in lower:
        return 1
    return 0


def classify_microphone_for_aec(name: str, host_api: str = "") -> str:
    device_class = classify_device(name, host_api)
    return "default" if device_class == "unknown" else device_class


def _measure_rms(pa: pyaudio.PyAudio, index: int, seconds: float = 0.35) -> int:
    frames_per_buffer = 1024
    chunks = max(1, int(seconds * _SAMPLE_RATE / frames_per_buffer))
    stream = None
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_SAMPLE_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=frames_per_buffer,
        )
        frames = []
        for _ in range(chunks):
            frames.append(stream.read(frames_per_buffer, exception_on_overflow=False))
        return audioop.rms(b"".join(frames), 2)
    except Exception:
        return -1
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass


def list_microphones(sample_seconds: float = 0.35) -> list[MicrophoneInfo]:
    preferred_name = configured_preferred_name().lower()
    selected_index = configured_device_index()
    pa = pyaudio.PyAudio()
    devices: list[MicrophoneInfo] = []
    try:
        host_apis = {
            i: pa.get_host_api_info_by_index(i).get("name", "")
            for i in range(pa.get_host_api_count())
        }
        for i in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(i)
                channels = int(info.get("maxInputChannels", 0))
                if channels <= 0:
                    continue
                name = str(info.get("name", f"Device {i}"))
                rms = _measure_rms(pa, i, sample_seconds)
                is_virtual = _is_virtual_input(name)
                preferred = bool(preferred_name and preferred_name in name.lower())
                score = float(rms)
                reason = "rms"
                if is_virtual:
                    score -= 100000.0
                    reason = "virtual_penalty"
                if preferred:
                    score += 1000000.0
                    reason = "preferred_name"
                if selected_index == i:
                    score += 10000000.0
                    reason = "manual_index"
                devices.append(
                    MicrophoneInfo(
                        index=i,
                        name=name,
                        host_api=str(host_apis.get(int(info.get("hostApi", -1)), "")),
                        max_input_channels=channels,
                        default_sample_rate=float(info.get("defaultSampleRate", 0.0) or 0.0),
                        rms=rms,
                        is_virtual=is_virtual,
                        preferred=preferred,
                        score=score,
                        reason=reason,
                    )
                )
            except Exception:
                continue
    finally:
        pa.terminate()

    chosen = choose_microphone(devices=devices, log=False)
    if chosen is not None:
        for device in devices:
            device.selected = device.index == chosen.index
    return devices


def list_microphone_devices() -> list[MicDeviceDescriptor]:
    """Enumerate input devices without opening them or sampling microphone audio."""

    pa = pyaudio.PyAudio()
    devices: list[MicDeviceDescriptor] = []
    try:
        host_apis = {
            i: pa.get_host_api_info_by_index(i).get("name", "")
            for i in range(pa.get_host_api_count())
        }
        for index in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(index)
                if int(info.get("maxInputChannels", 0) or 0) <= 0:
                    continue
                devices.append(
                    device_descriptor_from_info(
                        info,
                        str(host_apis.get(int(info.get("hostApi", -1)), "")),
                    )
                )
            except Exception:
                continue
    finally:
        pa.terminate()
    return devices


def choose_microphone(
    *,
    devices: list[MicrophoneInfo] | None = None,
    sample_seconds: float = 0.35,
    log: bool = True,
) -> MicrophoneInfo | None:
    devices = list_microphones(sample_seconds) if devices is None else devices
    if not devices:
        if log:
            logger.warning("[Mic] no input microphone devices found")
        return None
    chosen = max(devices, key=lambda item: item.score)
    if log:
        for device in devices:
            logger.info(
                "[Mic] [%s] %s host=%s rms=%s virtual=%s preferred=%s score=%.0f%s",
                device.index,
                device.name,
                device.host_api,
                device.rms,
                device.is_virtual,
                device.preferred,
                device.score,
                " <- selected" if device.index == chosen.index else "",
            )
        logger.info("[Mic] selected [%s] %s (%s)", chosen.index, chosen.name, chosen.reason)
    return chosen


def open_input_stream_with_fallback(
    pa: pyaudio.PyAudio,
    *,
    preferred_index: int | None = None,
    sample_rate: int = _SAMPLE_RATE,
    frames_per_buffer: int = 1024,
    log_label: str = "[Mic]",
):
    """Open a mono int16 input stream, retrying other devices if the preferred one fails."""

    attempts: list[tuple[int | None, str]] = []
    seen: set[int | None] = set()

    def add(index: int | None, reason: str) -> None:
        if index in seen:
            return
        seen.add(index)
        attempts.append((index, reason))

    add(preferred_index, "preferred")

    fallback_index = configured_fallback_device_index()
    if fallback_index is not None:
        add(fallback_index, "configured_fallback")

    fallback_name = configured_fallback_name().lower()
    if fallback_name:
        pa_devices = list_microphones(0.1)
        named_fallbacks = [
            d
            for d in pa_devices
            if (fallback_name in d.name.lower() or fallback_name in d.host_api.lower())
            and not d.is_virtual
            and d.index != preferred_index
        ]
        named_fallbacks.sort(
            key=lambda d: (
                classify_microphone_for_aec(d.name, d.host_api) == "internal",
                _host_api_priority(d.host_api),
                d.rms,
                d.score,
            ),
            reverse=True,
        )
        for device in named_fallbacks:
            add(device.index, f"configured_fallback_name:{device.name}")

    default_index = _default_input_device_index(pa)
    if default_index is not None:
        add(default_index, "windows_default")
    add(None, "system_default")
    devices = pa_devices if fallback_name else list_microphones(0.1)
    usable = [d for d in devices if d.rms >= 0 and not d.is_virtual and d.index != preferred_index]
    usable.sort(key=lambda d: (d.preferred, d.rms, d.score), reverse=True)
    for device in usable:
        add(device.index, f"fallback:{device.name}")

    last_error: Exception | None = None
    for index, reason in attempts:
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=sample_rate,
                input=True,
                input_device_index=index,
                frames_per_buffer=frames_per_buffer,
            )
            if index != preferred_index:
                logger.warning("%s opened fallback microphone index=%s (%s)", log_label, index, reason)
            return stream, index
        except Exception as exc:
            last_error = exc
            logger.warning("%s microphone open failed index=%s (%s): %s", log_label, index, reason, exc)

    if last_error is not None:
        raise last_error
    raise RuntimeError("no microphone input stream could be opened")


def monitor_microphone(index: int, seconds: float = 5.0, interval: float = 0.25) -> None:
    pa = pyaudio.PyAudio()
    stream = None
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=_SAMPLE_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=1024,
        )
        deadline = time.time() + seconds
        while time.time() < deadline:
            frames = stream.read(1024, exception_on_overflow=False)
            rms = audioop.rms(frames, 2)
            print(f"rms={rms}", flush=True)
            time.sleep(interval)
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        pa.terminate()
