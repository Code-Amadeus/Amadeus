from __future__ import annotations

from asr.mic_input_service import MicInputService


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def test_start_keeps_active_device_identity_when_preference_changes(caplog) -> None:
    service = MicInputService()
    service._thread = _AliveThread()
    service._stream = object()
    service._mic_index = 4

    service.start(preferred_index=5, wait_timeout=0)

    assert service.mic_index == 4
    assert "requested microphone index=5 differs from active index=4" in caplog.text
