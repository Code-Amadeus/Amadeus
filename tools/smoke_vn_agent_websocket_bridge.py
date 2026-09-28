from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def main() -> int:
    import websockets

    from server.vn_text_sources import AgentVNTextSource

    captured: list[dict[str, Any]] = []
    port = _free_port()

    line_one = {
        "text": "这里应该读取汉化后的中文台词。",
        "speaker": " narrator ",
        "script_id": "first_0215",
        "metadata": {
            "source": "Misty.MainMenu.ReserveLogData",
            "has_txtid": True,
        },
    }
    line_two = {
        "text": "第二行也是游戏画面上的中文文本。",
        "speaker": " narrator ",
        "script_id": "first_0216",
        "metadata": {
            "source": "Misty.MainMenu.ReserveLogData",
            "has_txtid": True,
        },
    }

    async def ws_handler(ws: Any, *_args: Any) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "copyText",
                    "process_path": "mock_game.exe",
                    "id": "copy-1",
                    "sentence": json.dumps(line_one, ensure_ascii=False),
                },
                ensure_ascii=False,
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "translate",
                    "process_path": "mock_game.exe",
                    "id": "translate-duplicate",
                    "sentence": json.dumps(line_one, ensure_ascii=False),
                },
                ensure_ascii=False,
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "copyText",
                    "process_path": "mock_game.exe",
                    "id": "copy-2",
                    "sentence": json.dumps(line_two, ensure_ascii=False),
                },
                ensure_ascii=False,
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "translate",
                    "process_path": "mock_game.exe",
                    "id": "translate-noise",
                    "sentence": "machine translated line that should not enter VN runtime",
                },
                ensure_ascii=False,
            )
        )
        await asyncio.sleep(0.2)

    async def line(params: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(params))
        return {"status": "accepted"}

    async def on_status(_hook: dict[str, Any], _bridge: dict[str, Any]) -> None:
        return None

    source = AgentVNTextSource(line, on_status)

    server = await websockets.serve(ws_handler, "127.0.0.1", port)
    try:
        source._start_bridge({"agentWsHost": "127.0.0.1", "agentWsPort": port}, {"bridgeMode": "websocket"})
        deadline = asyncio.get_running_loop().time() + 5
        while len(captured) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert len(captured) == 2, captured
        assert captured[0]["text"] == line_one["text"]
        assert captured[0]["speaker"] == "narrator"
        assert captured[0]["script_id"] == "first_0215"
        assert captured[0]["metadata"]["source"] == "agent_websocket"
        assert captured[0]["metadata"]["agent_message_type"] == "copyText"
        assert captured[0]["metadata"]["process_path"] == "mock_game.exe"
        assert captured[1]["text"] == line_two["text"]
        assert captured[1]["metadata"]["agent_message_type"] == "copyText"
        _hook, bridge = source.status()
        assert bridge["source"] == "agent_websocket"
        assert bridge["lineCount"] == 2
    finally:
        await source.stop()
        server.close()
        await server.wait_closed()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
