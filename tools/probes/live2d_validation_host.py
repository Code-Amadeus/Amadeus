"""Run the actual Host with only deterministic narration inputs for local validation."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def control_inputs(directory: Path) -> None:
    from server.ws_handler import manager
    from server.protocol import Method
    from server.character_presentation import coordinator
    from vts.expression_controller import get_controller
    from llm.stream_parser import StreamTagParser
    import numpy as np

    source = directory / "input.json"
    result_path = directory / "input-result.json"
    while True:
        await asyncio.sleep(.05)
        if not source.is_file():
            continue
        try:
            command = json.loads(source.read_text(encoding="utf-8"))
            source.unlink()
            action = command.get("action")
            controller = get_controller()
            if action == "emotion":
                label = command["label"]
                parser = StreamTagParser()
                parts = parser.process_chunk_parts("[EMO " + label + "] 固定表示テスト。")
                controller.register_sentence_actions("local-validation", parts[1])
                controller.on_sentence_start("local-validation")
            elif action == "mouth":
                handler = manager._request_handlers[Method.TTS_INTERRUPT]
                samples = np.sin(np.arange(2400, dtype=np.float32) * (2*np.pi*180/24000)) * float(command.get("amplitude", .25))
                handler._player._emit_mouth_value_for_audio(samples)
            elif action == "end":
                controller.on_turn_end()
            elif action == "work":
                await coordinator.claim(source_kind="work", source_id="local-validation",
                                        label="work", tier="ambient", scenario="computer-use")
            elif action == "release-work":
                await coordinator.release(source_kind="work", source_id="local-validation",
                                          tier="ambient", scenario="computer-use")
            else:
                raise ValueError("unknown local validation input")
            result = {"id": command["id"], "ok": True}
        except Exception as error:
            result = {"id": command.get("id"), "ok": False, "error": str(error)}
        result_path.write_text(json.dumps(result), encoding="utf-8")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=17777)
    parser.add_argument("--control-directory", type=Path, required=True)
    args = parser.parse_args()
    from server.app import bootstrap
    input_task = asyncio.create_task(control_inputs(args.control_directory))
    try:
        await bootstrap(port=args.port)
    finally:
        input_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
