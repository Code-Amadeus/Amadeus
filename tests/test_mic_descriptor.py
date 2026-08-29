"""Microphone descriptor and AEC delay selection tests.

运行：.venv\\Scripts\\python.exe -X utf8 tests\\test_mic_descriptor.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asr.microphone import classify_device, device_descriptor_from_info


def test_classify_device_keywords():
    assert classify_device("Bluetooth Hands-Free AG Audio", "MME") == "bluetooth"
    assert classify_device("BT Headset Microphone", "") == "bluetooth"
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
