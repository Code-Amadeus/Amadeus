"""Compare an archived Mystery runtime with the current one on a local trace.

No game/script text is bundled. Output stays in ignored diagnostics. Model calls
are off unless --llm is explicitly supplied (limited to 12 observations per arm).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_observations(path: Path, limit: int) -> list[dict]:
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    observations = []
    seen = set()
    for entry in entries:
        payload = entry.get("payload", entry)
        script_id = payload.get("script_id")
        # Revisit retention is a separately tested intentional correction. Keep
        # this replay's cadence comparable on the previously accepted trajectory.
        if script_id and script_id in seen:
            continue
        if script_id:
            seen.add(script_id)
        observations.append({key: payload[key] for key in ("text", "speaker", "script_id", "scene_id", "line_id") if key in payload})
        if len(observations) >= limit:
            break
    return observations


def semantic_result(result: dict) -> dict:
    line = result.get("line") or {}
    reaction = result.get("reaction") or {}
    attention = result.get("attention") or {}
    lookahead = result.get("lookahead") or {}
    summary = result.get("summary") or {}
    retrospective = result.get("retrospective") or {}
    return {
        "status": result.get("status"), "reason": result.get("reason"),
        "line": {key: line.get(key) for key in ("text", "speaker", "script_id", "scene_id")},
        "match": line.get("match"),
        "reaction": {key: reaction.get(key) for key in ("decision", "reason_label", "importance", "confidence", "speak")},
        "attention": {key: attention.get(key) for key in ("route", "density", "current_kind", "current_score")},
        "lookahead": {key: lookahead.get(key) for key in ("window", "density", "reaction_plan", "cadence")},
        "summary_lane": summary.get("lane"), "retrospective_lane": retrospective.get("lane"),
        "applied_layers": [entry.get("layer") for entry in result.get("context_applied") or []],
        "verification": [entry.get("status") for entry in result.get("verification") or []],
    }


async def child(args) -> None:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(args.package_root).resolve()))
    # Keep keys in the normal settings mechanism; never serialize them.
    for name in ("VN_LLM_ENABLED", "VN_IMMEDIATE_LLM_ENABLED"):
        os.environ[name] = "1" if args.llm else "0"
    for name in ("VN_LOOKAHEAD_LLM_ENABLED", "VN_REASONER_LLM_ENABLED", "VN_SUMMARY_LLM_ENABLED", "VN_RETROSPECTIVE_LLM_ENABLED"):
        os.environ[name] = "0"
    from vn_player.runtime import VNPlayerRuntime

    runtime = VNPlayerRuntime(Path(args.workspace))
    observations = read_observations(Path(args.trace), min(args.limit, 12) if args.llm else args.limit)
    await runtime.start({"session_id": "replay", "game_id": "paranormasight", "game_title": "PARANORMASIGHT",
                         "prompt_pack": "mystery", "game_genre": "mystery", "output_language": "ja",
                         "script_path": str(Path(args.script).resolve()), "lookahead_enabled": True,
                         "lookahead_llm_enabled": False, "max_reactions_per_minute": 1000})
    results = []
    try:
        if args.concurrent_current and args.arm == "current":
            completed = await asyncio.gather(*(runtime.ingest_line(observation) for observation in observations))
            results = [semantic_result(result) for result in completed]
        else:
            for index, observation in enumerate(observations):
                results.append(semantic_result(await runtime.ingest_line(observation)))
                if args.llm:
                    print(f"{args.arm}: {index + 1}/{len(observations)}", flush=True)
        store = runtime.store
        report = {"observations": results, "story_summary": store.story_summary_log(),
                  "retrospective": store.retrospective_bias()}
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await runtime.stop({"reason": "compatibility_replay"})


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()
                if not key.endswith("_at_ms") and key not in {"session_id", "created_at", "updated_at", "timestamp", "recorded_at"}}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def main(args) -> None:
    output = Path(args.output_dir) if args.output_dir else ROOT / "output" / "diagnostics" / ("vn-mystery-model-ab" if args.llm else "vn-mystery-replay")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vn-mystery-baseline-") as temporary:
        temp = Path(temporary)
        archive = temp / "baseline.zip"
        subprocess.run(["git", "archive", "--format=zip", f"--output={archive}", args.baseline_ref, "vn_player"], cwd=ROOT, check=True)
        with zipfile.ZipFile(archive) as source:
            source.extractall(temp / "baseline")
        for arm, package in (("baseline", temp / "baseline"), ("current", ROOT)):
            command = [sys.executable, __file__, "--arm", arm, "--package-root", str(package),
                       "--baseline-ref", args.baseline_ref,
                       "--workspace", str(temp / arm / "state"), "--output", str(output / f"{arm}.json"),
                       "--trace", args.trace, "--script", args.script, "--limit", str(args.limit)]
            if args.llm:
                command.append("--llm")
            if args.concurrent_current:
                command.append("--concurrent-current")
            subprocess.run(command, cwd=ROOT, check=True)
    before = canonical(json.loads((output / "baseline.json").read_text(encoding="utf-8")))
    after = canonical(json.loads((output / "current.json").read_text(encoding="utf-8")))
    differences = [{"index": index + 1, "fields": [key for key in old if old[key] != new.get(key)]}
                   for index, (old, new) in enumerate(zip(before["observations"], after["observations"])) if old != new]
    summary = {"baseline_ref": args.baseline_ref, "model_calls_enabled": args.llm,
               "current_concurrent": args.concurrent_current,
               "observations": len(before["observations"]), "differences": differences,
               "summary_equal": before["story_summary"] == after["story_summary"],
               "retrospective_equal": before["retrospective"] == after["retrospective"]}
    (output / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not args.llm and (differences or not summary["summary_equal"] or not summary["retrospective_equal"]):
        raise SystemExit("Replay differs; inspect the reports before accepting compatibility.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", required=True, help="Existing local Git ref for the pre-change baseline")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--concurrent-current", action="store_true", help="Submit current-runtime lines concurrently; baseline stays serial")
    parser.add_argument("--arm", default="")
    parser.add_argument("--package-root")
    parser.add_argument("--workspace")
    parser.add_argument("--output")
    parser.add_argument("--output-dir", help="Keep a separate parent comparison artifact")
    args = parser.parse_args()
    asyncio.run(child(args)) if args.arm else main(args)
