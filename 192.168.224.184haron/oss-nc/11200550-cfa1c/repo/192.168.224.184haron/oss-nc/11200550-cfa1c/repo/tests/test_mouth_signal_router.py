from __future__ import annotations

from tts.mouth_signal import MouthSignalRouter


def test_primary_mouth_sink_does_not_require_vts_connection() -> None:
    rendered: list[float] = []
    vts_attempts: list[float] = []
    router = MouthSignalRouter(
        primary_sink=rendered.append,
        compatibility_sink=lambda value: vts_attempts.append(value) or False,
    )

    router.publish_mouth_value(0.42)

    assert rendered == [0.42]
    assert vts_attempts == [0.42]


def test_disconnected_vts_side_path_does_not_block_primary_sink(tmp_path) -> None:
    from vts.connection_manager import VTSConnectionManager

    rendered: list[float] = []
    manager = VTSConnectionManager(
        "ws://127.0.0.1:1",
        token_file=str(tmp_path / "unused-vts-token.json"),
    )
    router = MouthSignalRouter(
        primary_sink=rendered.append,
        compatibility_sink=manager.send_mouth_data,
    )

    router.publish_mouth_value(0.42)

    assert manager.connected is False
    assert rendered == [0.42]


def test_optional_mouth_sink_failure_does_not_block_local_rendering() -> None:
    rendered: list[float] = []

    def reject(_value: float) -> None:
        raise RuntimeError("external character host is unavailable")

    router = MouthSignalRouter(
        primary_sink=rendered.append,
        compatibility_sink=reject,
    )

    router.publish_mouth_value(0.75)

    assert rendered == [0.75]
