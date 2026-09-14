"""Blocking microphone callers never consume DirectSound's unimplemented read."""

from unittest.mock import Mock

import pytest

pytest.importorskip("pyaudio", reason="voice tier (pyaudio) is not installed")

from asr import microphone as mic


class Audio:
    def __init__(self, *, default=5):
        self.default = default
        self.opened = []
        self.devices = {
            1:dict(index=1, name="Realtek Microphone Array", hostApi=0,
                maxInputChannels=2, defaultSampleRate=48000),
            5:dict(index=5, name="Realtek Microphone Array", hostApi=1,
                maxInputChannels=2, defaultSampleRate=48000),
            8:dict(index=8, name="USB microphone", hostApi=0,
                maxInputChannels=1, defaultSampleRate=16000),
        }

    def get_device_info_by_index(self, index):
        return self.devices[index]

    def get_default_input_device_info(self):
        return self.devices[self.default]

    def get_host_api_info_by_index(self, index):
        return {"name":"MME" if index == 0 else "Windows DirectSound",
            "type":mic.pyaudio.paMME if index == 0 else mic.pyaudio.paDirectSound}

    def get_device_count(self):
        return 9

    def get_host_api_count(self):
        return 2

    def open(self, **kwargs):
        self.opened.append(kwargs)
        # Even success and plausible loud bytes cannot establish read support.
        return Mock(read=Mock(return_value=b"\xff\x7f" * kwargs["frames_per_buffer"]))

    def terminate(self):
        pass


@pytest.fixture
def audio(monkeypatch):
    pa = Audio()
    devices = [mic.MicrophoneInfo(index, info["name"],
        pa.get_host_api_info_by_index(info["hostApi"])["name"],
        info["maxInputChannels"], info["defaultSampleRate"],
        30000 if index == 5 else 1, False, False, score=30000 if index == 5 else 1)
        for index, info in pa.devices.items()]
    monkeypatch.setattr(mic, "list_microphones", lambda _seconds:devices)
    monkeypatch.setattr(mic, "configured_device_index", lambda:None)
    monkeypatch.setattr(mic, "configured_preferred_name", lambda:"")
    monkeypatch.setattr(mic, "configured_fallback_device_index", lambda:None)
    monkeypatch.setattr(mic, "configured_fallback_name", lambda:"")
    return pa, devices


def test_directsound_rms_probe_never_opens_or_scores_successful_stub(audio):
    pa, devices = audio
    assert mic._measure_rms(pa, 5) == -1
    assert pa.opened == []
    assert mic.choose_microphone(devices=devices, log=False).index != 5


@pytest.mark.parametrize("route", ["preferred", "default", "system_default", "fallback"])
def test_all_blocking_open_routes_exclude_directsound(audio, monkeypatch, route):
    pa, _ = audio
    if route == "system_default":
        monkeypatch.setattr(mic, "_default_input_device_index", lambda _pa:None)
    if route == "fallback":
        monkeypatch.setattr(mic, "configured_fallback_device_index", lambda:5)
    stream, index = mic.open_input_stream_with_fallback(pa,
        preferred_index=5 if route == "preferred" else None, frames_per_buffer=512)
    assert stream is not None and index == 1
    assert len(pa.opened) == 1 and pa.opened[0]["input_device_index"] == 1
    assert pa.opened[0]["rate"] == 16000
    assert pa.opened[0]["channels"] == 1
    assert pa.opened[0]["frames_per_buffer"] == 512
    assert pa.opened[0]["format"] == mic.pyaudio.paInt16


def test_explicit_unsupported_device_fails_without_switching(audio, monkeypatch):
    pa, _ = audio
    monkeypatch.setattr(mic, "configured_device_index", lambda:5)
    with pytest.raises(RuntimeError, match="DirectSound.*blocking"):
        mic.open_input_stream_with_fallback(pa, preferred_index=5)
    assert pa.opened == []


def test_explicit_supported_fallback_remains_authorized(audio, monkeypatch):
    pa, _ = audio
    monkeypatch.setattr(mic, "configured_device_index", lambda:5)
    monkeypatch.setattr(mic, "configured_fallback_device_index", lambda:8)
    _, index = mic.open_input_stream_with_fallback(pa, preferred_index=5)
    assert index == 8 and [call["input_device_index"] for call in pa.opened] == [8]


def test_supported_rms_probe_preserves_audio_sampling(audio):
    pa, _ = audio
    assert mic._measure_rms(pa, 1) == 32767
    assert [call["input_device_index"] for call in pa.opened] == [1]


def test_device_listing_keeps_unsupported_description_without_sampling(monkeypatch):
    pa = Audio()
    monkeypatch.setattr(mic.pyaudio, "PyAudio", lambda:pa)
    monkeypatch.setattr(mic, "configured_device_index", lambda:5)
    monkeypatch.setattr(mic, "configured_preferred_name", lambda:"Realtek")
    devices = mic.list_microphones()
    unsupported = next(device for device in devices if device.index == 5)
    assert unsupported.rms == -1 and not unsupported.selected
    assert "DirectSound" in unsupported.reason and "blocking" in unsupported.reason
    assert unsupported.max_input_channels == 2 and unsupported.default_sample_rate == 48000
    assert {call["input_device_index"] for call in pa.opened} == {1, 8}
    pa.opened.clear()
    # Descriptor enumeration is still general; callback consumers lose no devices.
    assert {device.index for device in mic.list_microphone_devices()} == {1, 5, 8}
    assert pa.opened == []


def test_automatic_preferred_endpoint_tries_same_named_supported_device_first(audio):
    pa, devices = audio
    next(device for device in devices if device.index == 8).score = 1000000
    next(device for device in devices if device.index == 8).rms = 30000
    _, index = mic.open_input_stream_with_fallback(pa, preferred_index=5)
    assert index == 1


@pytest.mark.parametrize("host_api", ["MME", "Windows WASAPI", "Windows WDM-KS", "ALSA"])
def test_other_host_apis_are_not_speculatively_rejected(audio, host_api):
    pa, _ = audio
    pa.get_host_api_info_by_index = lambda _index:{"name":host_api}
    assert mic._measure_rms(pa, 1) == 32767
