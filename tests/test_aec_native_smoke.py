"""Exercise the installed WebRTC binding without opening an audio device."""

import pytest


aec = pytest.importorskip("aec_audio_processing", reason="requires the voice tier")


def test_native_aec_processes_silent_reference_and_capture_frames():
    processor = aec.AudioProcessor(
        enable_aec=True, enable_ns=False, enable_agc=False, enable_vad=False,
    )
    processor.set_stream_format(48000, 1, 48000, 1)
    processor.set_reverse_stream_format(48000, 1)
    processor.set_stream_delay(0)
    frame = bytes(480 * 2)  # 10 ms, mono signed 16-bit PCM.
    for _ in range(10):
        processor.process_reverse_stream(frame)
        output = processor.process_stream(frame)
        assert len(output) == len(frame)
        assert output == frame
