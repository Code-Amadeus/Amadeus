"""Opt-in, bounded real-model acceptance of Base VN roles on synthetic dialogue.

Runs existing summary/reflection/interaction lanes; does not launch games or TTS.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    for name in ("VN_LLM_ENABLED", "VN_IMMEDIATE_LLM_ENABLED", "VN_SUMMARY_LLM_ENABLED", "VN_RETROSPECTIVE_LLM_ENABLED"):
        os.environ[name] = "1"
    os.environ["VN_LOOKAHEAD_LLM_ENABLED"] = "0"
    os.environ["VN_REASONER_LLM_ENABLED"] = "0"
    from vn_player.runtime import VNPlayerRuntime

    output = ROOT / "output" / "diagnostics" / f"vn-base-model-{time.strftime('%Y%m%d-%H%M%S')}"
    runtime = VNPlayerRuntime(output)
    await runtime.start({"session_id": "synthetic_base", "game_id": "synthetic_seaside", "game_title": "海边的周末（合成验收）",
                         "prompt_pack": "base", "output_language": "zh", "script_path": "",
                         "capabilities": {"immediate": False, "interaction": True, "summary": True, "retrospective": True}})
    dialogue = [
        ("美咲", "今天的课上完了，一起去图书馆吗？"), ("遥", "好，我先把借来的小说还掉。"),
        ("美咲", "周末如果天气晴朗，我们去海边散步吧。"), ("遥", "我愿意去，不过还要确认天气预报。"),
        ("美咲", "那就星期五再确认出发时间。"), ("遥", "如果下雨，我们就改去车站旁的咖啡馆。"),
        ("美咲", "我会带上相机，想拍一下海边的晚霞。"), ("遥", "我带热茶，你记得穿外套。"),
        ("美咲", "谢谢，你总是很细心。"), ("遥", "那我们先去还书。"),
    ]
    roles = []
    try:
        for index in range(40):
            speaker, text = dialogue[index % len(dialogue)]
            result = await runtime.ingest_line({"text": text, "speaker": speaker})
            if result.get("status") != "ok":
                raise RuntimeError(result)
            for role in ("summary", "retrospective"):
                if role in result:
                    roles.append({"at_line": index + 1, "role": role, "result": result[role]})
                    print(f"{role} at {index + 1}: {result[role].get('lane')}", flush=True)
        answer = await runtime.player_intervention("ask", {"text": "我们周末打算做什么？哪些事情还没确定？"})
        assert answer["status"] == "ok"
        assert answer["reaction"]["decision"] == "speak"
        assert answer["reaction"]["speak"]["text"]
        report = {"status": runtime.status(), "roles": roles, "answer": answer,
                  "summary": runtime.store.story_summary_log(), "reflection": runtime.store.retrospective_bias(),
                  "evidence": runtime.store.evidence_nodes(), "hypotheses": runtime.store.hypotheses()}
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        assert any(entry["result"].get("lane") == "summary" for entry in roles)
        assert any(entry["result"].get("lane") == "retrospective" and entry["result"].get("bias") for entry in roles)
        assert not runtime.store.evidence_nodes()
        print(f"Base model role report: {output / 'report.json'}", flush=True)
    finally:
        await runtime.stop({"reason": "base_model_acceptance"})


if __name__ == "__main__":
    asyncio.run(main())
