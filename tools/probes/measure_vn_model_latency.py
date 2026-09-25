"""Opt-in matched-prompt VN model benchmark: full/streamed JSON × serial/parallel.

Reads a local session; no dialogue is bundled or written to the aggregate report.
Uses normal configured credentials. Measures model/ordered-delivery readiness, not
physical audio. Network calls require --live-model. Both arms reuse one client,
so the comparison does not count the old per-request connection setup overhead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vn_player.llm_client import VNLLMClient
from vn_player.prompt_layers import compose_messages
from vn_player.schemas import VNProfile
from vn_player.speech_stream import VNSpeechStream
from vn_player.runtime import _normalize_speak_text


async def arm(profile, prompts, *, stream, parallel, interval, repeat):
    client = VNLLMClient(profile)
    slots = asyncio.Semaphore(3 if parallel else 1)
    commits = [asyncio.Event() for _ in prompts]
    rows = []
    origin = time.perf_counter()

    async def one(index, messages):
        await asyncio.sleep(max(0, origin + interval * index - time.perf_counter()))
        received = time.perf_counter()
        metrics = {}
        row = {"sample": index + 1, "repeat": repeat, "stream": stream, "concurrency": 3 if parallel else 1}

        async def submit(_payload):
            row.setdefault("delivery_ready_ms", round((time.perf_counter() - received) * 1000))

        speech = VNSpeechStream(
            authorize=lambda header, closed: header, active=lambda: True, submit=submit,
            normalize=_normalize_speak_text, previous=commits[index - 1] if index else None,
            language="英文" if profile.output_language.lower().startswith("en") else "日文",
        )

        async with slots:
            row["queue_ms"] = round((time.perf_counter() - received) * 1000)
            parsed, _ = await client.complete_json(
                messages, lane="immediate" if stream else "benchmark_full_json",
                max_tokens=800, temperature=.55, on_ready=speech.update if stream else None, metrics=metrics,
            )
            row["response_ready_ms"] = round((time.perf_counter() - received) * 1000)
            row.update(valid=parsed is not None, decision=(parsed or {}).get("decision"),
                       finish_reason=metrics.get("finish_reason"), model=metrics.get("model"),
                       completion_tokens=metrics.get("completion_tokens"),
                       model_ms=metrics["completed_at_ms"] - metrics["started_at_ms"])
            if metrics.get("first_token_at_ms"):
                row["first_token_ms"] = metrics["first_token_at_ms"] - metrics["started_at_ms"]
            if not speech.observed and parsed is not None and parsed.get("decision") == "speak":
                await speech.update(parsed, True)
            if index:
                await commits[index - 1].wait()
            await speech.finish()
            commits[index].set()
        rows.append(row)
    try:
        await asyncio.gather(*(one(i, messages) for i, messages in enumerate(prompts)))
    finally:
        await client.aclose()
    return rows


async def main(args):
    if not args.live_model:
        raise SystemExit("Supply --live-model to authorize this network/model experiment.")
    session = Path(args.session)
    profile = VNProfile(**json.loads((session / "profile.json").read_text("utf-8")))
    calls = [json.loads(row) for row in (session / "model_calls.jsonl").read_text("utf-8").splitlines()]
    selected = [row for row in calls if row["lane"] == "immediate" and row["ok"]
                and isinstance(row["response"], dict) and row["response"].get("decision") == "speak"][:args.samples]
    if not selected:
        raise SystemExit("No recorded spoken immediate calls with reusable prompt context.")
    prompts = [compose_messages(profile, "immediate", row["request"]["clean_context"]) for row in selected]
    modes = [(False, False), (True, False), (False, True), (True, True)]
    rows = []
    for repeat in range(args.repeats):
        for stream, parallel in (modes if repeat % 2 == 0 else list(reversed(modes))):
            result = await arm(profile, prompts, stream=stream, parallel=parallel,
                               interval=args.interval, repeat=repeat + 1)
            rows.extend(result)
            print(json.dumps({"repeat": repeat + 1, "stream": stream,
                              "concurrency": 3 if parallel else 1, "valid": sum(row["valid"] for row in result),
                              "samples": len(result)}, ensure_ascii=False), flush=True)
    summary = []
    groups = {(row["repeat"], row["sample"]) for row in rows}
    matched = {key for key in groups if all("delivery_ready_ms" in row for row in rows
                                           if (row["repeat"], row["sample"]) == key)}
    for stream, parallel in modes:
        group = [row for row in rows if row["stream"] == stream and row["concurrency"] == (3 if parallel else 1)]
        result = {"stream": stream, "concurrency": 3 if parallel else 1, "samples": len(group),
                  "valid": sum(row["valid"] for row in group),
                  "speak_samples": sum("delivery_ready_ms" in row for row in group)}
        for metric in ("queue_ms", "model_ms", "first_token_ms", "response_ready_ms", "delivery_ready_ms"):
            values = [row[metric] for row in group if metric in row]
            result[metric + "_median"] = statistics.median(values) if values else None
        common = [row["delivery_ready_ms"] for row in group if (row["repeat"], row["sample"]) in matched]
        result["matched_spoken_samples"] = len(common)
        result["matched_delivery_ready_ms_median"] = statistics.median(common) if common else None
        summary.append(result)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"interval_seconds": args.interval, "summary": summary, "calls": rows,
                                  "scope": "Matched recorded prompts; no physical audio measurement"}, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", default="output/diagnostics/vn-model-latency.json")
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between simulated displayed lines")
    asyncio.run(main(parser.parse_args()))
