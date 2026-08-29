"""A closed WebSocket is removed on the first failed event send."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.event_bus import bus
from server.protocol import Method
from server.ws_handler import ConnectionManager


class _ClosedDuringForward:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.send_count = 0

    async def accept(self) -> None:
        return None

    async def iter_text(self):
        await self.release.wait()
        if False:
            yield ""

    async def send_json(self, _payload) -> None:
        self.send_count += 1
        raise AssertionError("send after close")


class _ConcurrentWriteGuard:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.sending = False
        self.payloads: list[dict] = []

    async def accept(self) -> None:
        return None

    async def iter_text(self):
        await self.release.wait()
        if False:
            yield ""

    async def send_json(self, payload: dict) -> None:
        assert not self.sending, "concurrent WebSocket write"
        self.sending = True
        try:
            await asyncio.sleep(0.01)
            self.payloads.append(payload)
        finally:
            self.sending = False


def test_failed_forward_is_removed_once() -> None:
    async def run() -> None:
        manager = ConnectionManager()
        ws = _ClosedDuringForward()
        task = asyncio.create_task(manager.handle_connection(ws))
        for _ in range(20):
            if manager._connections:
                break
            await asyncio.sleep(0)
        assert len(manager._connections) == 1

        await bus.emit(Method.WORK_UPDATED, {"workItemId": "work_a"})
        assert manager._connections == {}
        await bus.emit(Method.WORK_UPDATED, {"workItemId": "work_b"})
        assert ws.send_count == 1

        ws.release.set()
        await task

    asyncio.run(run())


def test_outbound_events_are_serialized_per_connection() -> None:
    async def run() -> None:
        manager = ConnectionManager()
        ws = _ConcurrentWriteGuard()
        task = asyncio.create_task(manager.handle_connection(ws))
        for _ in range(20):
            if manager._connections:
                break
            await asyncio.sleep(0)

        await asyncio.gather(
            bus.emit(Method.WORK_UPDATED, {"workItemId": "work_a"}),
            bus.emit(Method.CHAT_WORK_NOTE, {"run_id": "run_a"}),
        )
        assert len(ws.payloads) == 2
        assert len(manager._connections) == 1

        ws.release.set()
        await task

    asyncio.run(run())


def test_auip_launch_request_reaches_the_trusted_desktop_client() -> None:
    async def run() -> None:
        manager = ConnectionManager()
        ws = _ConcurrentWriteGuard()
        task = asyncio.create_task(manager.handle_connection(ws))
        for _ in range(20):
            if manager._connections:
                break
            await asyncio.sleep(0)

        await bus.emit(
            Method.AUIP_LAUNCH_REQUESTED,
            {
                "request_id": "auip_launch_a",
                "artifact_id": "artifact_a",
                "mode": "collaborate",
            },
        )
        assert ws.payloads == [
            {
                "type": "evt",
                "id": ws.payloads[0]["id"],
                "method": Method.AUIP_LAUNCH_REQUESTED,
                "params": {
                    "request_id": "auip_launch_a",
                    "artifact_id": "artifact_a",
                    "mode": "collaborate",
                },
            }
        ]

        ws.release.set()
        await task

    asyncio.run(run())


def test_auip_surface_close_request_reaches_the_trusted_desktop_client() -> None:
    async def run() -> None:
        manager = ConnectionManager()
        ws = _ConcurrentWriteGuard()
        task = asyncio.create_task(manager.handle_connection(ws))
        for _ in range(20):
            if manager._connections:
                break
            await asyncio.sleep(0)

        await bus.emit(
            Method.AUIP_SURFACE_CLOSE_REQUESTED,
            {
                "app_session_id": "app_a",
                "host_surface_id": "surface_a",
            },
        )
        assert ws.payloads == [
            {
                "type": "evt",
                "id": ws.payloads[0]["id"],
                "method": Method.AUIP_SURFACE_CLOSE_REQUESTED,
                "params": {
                    "app_session_id": "app_a",
                    "host_surface_id": "surface_a",
                },
            }
        ]

        ws.release.set()
        await task

    asyncio.run(run())


if __name__ == "__main__":
    test_failed_forward_is_removed_once()
    test_outbound_events_are_serialized_per_connection()
    test_auip_launch_request_reaches_the_trusted_desktop_client()
    test_auip_surface_close_request_reaches_the_trusted_desktop_client()
    print("all websocket disconnect tests passed")
