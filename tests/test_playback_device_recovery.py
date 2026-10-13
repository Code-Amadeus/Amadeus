"""Output-open recovery replaces stale native state and preserves real failures."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tts.playback import StreamPlayer


@pytest.fixture
def device_factory(monkeypatch):
    devices = []
    stream = SimpleNamespace(write=Mock(), close=Mock(), stop_stream=Mock(), is_active=lambda: True)

    def device(error=None):
        pa = SimpleNamespace(open=Mock(side_effect=error) if error else Mock(return_value=stream),
                             terminate=Mock())
        devices.append(pa)
        return pa

    factory = Mock()
    monkeypatch.setitem(sys.modules, "pyaudio", SimpleNamespace(PyAudio=factory, paFloat32=1))
    player = StreamPlayer(SimpleNamespace(publish_mouth_value=Mock()))
    yield player, factory, device, stream
    player.cleanup()


def test_stale_output_recovers_and_writes_same_audio(device_factory, caplog):
    player, factory, device, stream = device_factory
    stale, fresh = device(OSError(-9986, "Internal PortAudio error")), device()
    # Reproduce a long-lived backend which already owns the stale instance.
    player.pyaudio_instance = stale
    factory.return_value = fresh
    assert player.prewarm(44100, silence_ms=10)
    stale.terminate.assert_called_once_with()
    factory.assert_called_once_with()
    assert player.pyaudio_instance is fresh
    assert player.stream is stream and player.is_playing
    assert len(stream.write.call_args.args[0]) == 441 * 4
    assert "AUDIO-RECOVERY" in caplog.text
    player.initialize(44100)
    fresh.open.assert_called_once()


def test_persistent_failure_is_bounded_and_next_call_can_recover(device_factory):
    player, factory, device, stream = device_factory
    first = device(OSError(-9986, "Internal PortAudio error"))
    second = device(OSError(-9986, "Still unavailable"))
    healthy = device()
    factory.side_effect = [first, second, healthy]
    with pytest.raises(OSError, match="Still unavailable"):
        player.initialize(44100)
    assert factory.call_count == 2
    first.terminate.assert_called_once_with()
    second.terminate.assert_called_once_with()
    assert player.pyaudio_instance is None and player.stream is None
    assert not player.is_playing
    player.initialize(44100)
    assert player.stream is stream


def test_unsupported_format_is_reported_without_retry(device_factory):
    player, factory, device, _ = device_factory
    invalid = device(OSError(-9997, "Invalid sample rate"))
    factory.return_value = invalid
    with pytest.raises(OSError, match="Invalid sample rate"):
        player.initialize(123)
    factory.assert_called_once_with()
    invalid.terminate.assert_called_once_with()
    assert player.pyaudio_instance is None and not player.is_playing
