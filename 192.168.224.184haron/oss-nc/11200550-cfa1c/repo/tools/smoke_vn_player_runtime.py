from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vn_player.runtime import VNPlayerRuntime


DEFAULT_SAMPLE_IDS = [
    "a0_010_0287",
    "a0_010_0288",
    "a0_010_0320",
    "a0_030_0854",
    "a0_030_0855",
]


def _default_script_path(repo_root: Path) -> Path:
    return (
        repo_root.parent
        / "visual novel player"
        / "ParanormasightChsLocalization"
        / "texts"
        / "zh_Hans"
        / "Hazy_Script.txt"
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.no_llm:
        os.environ["VN_LLM_ENABLED"] = "0"
    elif args.llm:
        os.environ["VN_LLM_ENABLED"] = "1"
    os.environ["VN_LOOKAHEAD_LLM_ENABLED"] = "1" if args.lookahead_llm else "0"
    os.environ["VN_REASONER_LLM_ENABLED"] = "1" if args.reasoner_llm else "0"
    if args.reasoner_llm:
        os.environ.setdefault("VN_REASONER_EVERY_LINES", "1")

    repo_root = Path(__file__).resolve().parents[1]
    project_root = Path(args.project_root).resolve() if args.project_root else repo_root
    script_path = Path(args.script_path).resolve() if args.script_path else _default_script_path(repo_root)

    events: list[tuple[str, dict[str, Any]]] = []
    speaks: list[dict[str, Any]] = []

    async def emit(method: str, params: dict[str, Any]) -> None:
        events.append((method, params))

    async def speak(payload: dict[str, Any]) -> None:
        speaks.append(payload)

    runtime = VNPlayerRuntime(project_root, event_emit=emit, speak_callback=speak)
    status = await runtime.start(
        {
            "session_id": args.session_id,
            "script_path": str(script_path),
            "output_language": args.output_language,
        }
    )

    rows = []
    for script_id in args.sample_id:
        line = runtime.script_index._by_id.get(script_id)
        if line is None:
            rows.append({"script_id": script_id, "error": "not_found"})
            continue
        result = await runtime.ingest_line({"script_id": script_id, "text": line.text, "speaker": line.speaker})
        reaction = result.get("reaction") or {}
        lookahead = result.get("lookahead") or {}
        rows.append(
            {
                "script_id": script_id,
                "decision": reaction.get("decision"),
                "reason": reaction.get("reason_label"),
                "speak": (reaction.get("speak") or {}).get("text", ""),
                "lookahead_source": lookahead.get("source"),
                "lookahead_target": ((lookahead.get("reaction_plan") or [{}])[0]).get("target_script_id", ""),
                "reasoner_applied": (result.get("reasoner") or {}).get("applied", []),
            }
        )

    return {
        "status": status["status"],
        "script_lines": status["script"]["line_count"],
        "session_id": args.session_id,
        "rows": rows,
        "events": len(events),
        "speaks": len(speaks),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test VN Player runtime.")
    parser.add_argument("--script-path", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--session-id", default="smoke_vn_player")
    parser.add_argument("--output-language", default="zh")
    parser.add_argument("--sample-id", action="append", default=None)
    parser.add_argument("--no-llm", action="store_true", help="Force rules-only mode.")
    parser.add_argument("--llm", action="store_true", help="Use configured LLM for immediate lane.")
    parser.add_argument("--lookahead-llm", action="store_true")
    parser.add_argument("--reasoner-llm", action="store_true")
    args = parser.parse_args()
    if args.sample_id is None:
        args.sample_id = list(DEFAULT_SAMPLE_IDS)
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
