from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vn_player.runtime import VNPlayerRuntime


async def main() -> None:
    os.environ["VN_LLM_ENABLED"] = "0"
    root = ROOT.parent / "visual novel player" / "agent" / "vn_mvp_eval"
    script = ROOT.parent / "visual novel player" / "ParanormasightChsLocalization" / "texts" / "zh_Hans" / "Hazy_Script.txt"
    runtime = VNPlayerRuntime(root)
    await runtime.start({"session_id": "smoke_verifier", "script_path": str(script), "output_language": "zh"})

    line = runtime.script_index._by_id["a0_010_0288"]
    result = await runtime.ingest_line({"script_id": line.script_id, "text": line.text})
    line_event = result["line"]
    patches = [
        {
            "layer": "candidate_fact",
            "target": "evidence_nodes",
            "item": {
                "id": "fact_good",
                "claim": "兴家君被询问是否没事。",
                "evidence_line_ids": [line_event["line_id"]],
            },
        },
        {
            "layer": "candidate_fact",
            "target": "evidence_nodes",
            "item": {
                "id": "fact_bad",
                "claim": "凶手已经承认自己是人鱼。",
                "evidence_line_ids": [line_event["line_id"]],
            },
        },
        {
            "layer": "hypothesis",
            "target": "hypotheses",
            "item": {
                "id": "hyp_weak",
                "claim": "可能和人鱼诅咒有关。",
                "evidence_line_ids": [line_event["line_id"]],
            },
        },
    ]
    verified, feedback = runtime._verify_context_patches(patches, line_event)
    applied = runtime.store.apply_context_patches(verified, source_line=line_event)
    print(
        json.dumps(
            {
                "feedback": feedback,
                "applied": applied,
                "evidence_nodes": runtime.store.evidence_nodes(),
                "hypotheses_tail": runtime.store.hypotheses()[-2:],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
