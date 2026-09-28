"""Microphone descriptor and AEC delay selection tests.

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_mic_descriptor.py
"""

from __future__ import annotations

import os
import sys
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("pyaudio", reason="voice tier (pyaudio) is not installed")

from asr.mic_input_service import MicInputService
from asr.microphone import (
    MicrophoneInfo,
    classify_device,
    device_descriptor_from_info,
    open_input_stream_with_fallback,
)


def test_classify_device_keywords():
    assert classify_device("Bluetooth Hands-Free AG Audio", "MME") == "bluetooth"
    assert classify_device("BT Headset Microphone", "") == "bluetooth"
    assert classify_device("耳机 (HUAWEI FreeBuds 6i)", "MME") == "bluetooth"
    assert classify_device("Realtek Microphone Array", "Windows WASAPI") == "internal"
    assert classify_device("内置麦克风", "") == "internal"
    assert classify_device("USB Audio Device", "DirectSound") == "usb"
    assert classify_device("Studio Capture", "DirectSound") == "unknown"


def test_descriptor_from_fake_device_info():
    desc = device_descriptor_from_info(
        {
            "index": 7,
            "name": "USB Podcast Mic",
            "maxInputChannels": 2,
            "defaultSampleRate": 48000.0,
        },
        "Windows WASAPI",
    )
    assert desc.index == 7
    assert desc.name == "USB Podcast Mic"
    assert desc.host_api == "Windows WASAPI"
    assert desc.max_input_channels == 2
    assert desc.default_sample_rate == 48000.0
    assert desc.device_class == "usb"


def test_preferred_name_is_tried_before_configured_fallback_and_default(monkeypatch):
    devices = [
        MicrophoneInfo(8, "耳机 (HUAWEI FreeBuds 6i)", "Windows WASAPI",
            1, 44100.0, 120, False, True, score=1000120),
        MicrophoneInfo(2, "麦克风阵列 (Realtek(R) Audio)", "MME",
            4, 44100.0, 300, False, False, score=300),
    ]
    opened = []
    stream = object()

    class Audio:
        def get_device_info_by_index(self, index):
            return {"index":index, "hostApi":0}

        def get_host_api_info_by_index(self, index):
            return {"name":"Windows WASAPI"}

        def open(self, **kwargs):
            opened.append(kwargs["input_device_index"])
            if kwargs["input_device_index"] == 8:
                return stream
            raise OSError("unavailable")

    monkeypatch.setattr("asr.microphone.list_microphones", lambda _seconds:devices)
    monkeypatch.setattr("asr.microphone.configured_device_index", lambda:None)
    monkeypatch.setattr("asr.microphone.configured_preferred_name", lambda:"FreeBuds")
    monkeypatch.setattr("asr.microphone.configured_fallback_device_index", lambda:2)
    monkeypatch.setattr("asr.microphone.configured_fallback_name", lambda:"")
    monkeypatch.setattr("asr.microphone._default_input_device_index", lambda _pa:2)
    selected, index = open_input_stream_with_fallback(Audio(), preferred_index=17)
    assert selected is stream and index == 8
    assert opened == [17, 8]


def test_explicit_microphone_index_retries_and_never_silently_changes_device(
    monkeypatch,
):
    devices = [
        MicrophoneInfo(8, "耳机 (HUAWEI FreeBuds 6i)", "MME",
            1, 44100.0, 100, False, True, score=1000100),
        MicrophoneInfo(17, "耳机 (HUAWEI FreeBuds 6i)", "Windows WASAPI",
            1, 16000.0, 0, False, True, score=10000000),
    ]
    stream = object()
    opened = []

    class Audio:
        def get_device_info_by_index(self, index):
            return {"index":index, "hostApi":0}

        def get_host_api_info_by_index(self, index):
            return {"name":"Windows WASAPI"}

        def open(self, **kwargs):
            index = kwargs["input_device_index"]
            opened.append(index)
            if len(opened) < 3:
                raise OSError("device activation pending")
            return stream

    monkeypatch.setattr("asr.microphone.list_microphones", lambda _seconds:devices)
    monkeypatch.setattr("asr.microphone.configured_device_index", lambda:17)
    monkeypatch.setattr("asr.microphone.configured_preferred_name", lambda:"FreeBuds")
    monkeypatch.setattr("asr.microphone.configured_fallback_device_index", lambda:None)
    monkeypatch.setattr("asr.microphone.configured_fallback_name", lambda:"")
    monkeypatch.setattr("asr.microphone.time.sleep", lambda _seconds:None)

    selected, index = open_input_stream_with_fallback(
        Audio(), preferred_index=17
    )

    assert selected is stream and index == 17
    assert opened == [17, 17, 17]


def test_running_shared_mic_keeps_its_actual_opened_index() -> None:
    service = MicInputService()
    service._mic_index = 1
    service._stream = object()
    service._thread = Mock(is_alive=Mock(return_value=True))
    service.start(preferred_index=17)
    assert service.mic_index == 1


def test_aec_delay_by_device_class_and_explicit_override():
    from config import settings
    from tts.aec_realtime import select_aec_delay_ms

    old = os.environ.pop("AEC_REALTIME_DELAY_MS", None)
    try:
        assert select_aec_delay_ms("bluetooth")[0] == float(settings.AEC_DELAY_MS_BLUETOOTH)
        assert select_aec_delay_ms("internal")[0] == float(settings.AEC_DELAY_MS_INTERNAL)
        assert select_aec_delay_ms("usb")[0] == float(settings.AEC_DELAY_MS_USB)
        assert select_aec_delay_ms("unknown")[0] == float(settings.AEC_REALTIME_DELAY_MS)

        os.environ["AEC_REALTIME_DELAY_MS"] = "80"
        for device_class in ("bluetooth", "internal", "usb", "unknown"):
            delay_ms, reason = select_aec_delay_ms(device_class)
            assert delay_ms == 80.0
            assert "explicit" in reason
    finally:
        if old is None:
            os.environ.pop("AEC_REALTIME_DELAY_MS", None)
        else:
            os.environ["AEC_REALTIME_DELAY_MS"] = old


def _main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok: {name}")
    print("all mic descriptor tests passed")


if __name__ == "__main__":
    _main()
