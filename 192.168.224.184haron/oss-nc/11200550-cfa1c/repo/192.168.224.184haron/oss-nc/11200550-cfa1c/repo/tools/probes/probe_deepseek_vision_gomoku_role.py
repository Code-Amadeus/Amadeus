r"""Compare DeepSeek's experimental vision model on the Gomoku role probe.

This is a product-inert transport arm for the fixtures and scorer in
``probe_gemini37_gomoku_role.py``.  It does not change the product's provider
capabilities, invoke an AUIP action, or write the Work ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from tools.probes import probe_gemini37_gomoku_role as common


DEFAULT_MODEL = "deepseek-v4-flash-vision-exp"


def _usage(response) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None, None
    details = getattr(usage, "completion_tokens_details", None)
    return (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        (
            getattr(details, "reasoning_tokens", None)
            if details is not None
            else None
        ),
    )


async def _run_one(
    client: AsyncOpenAI,
    *,
    model: str,
    scenario: common.Scenario,
    arm: str,
    repeat: int,
    timeout_s: float,
    semaphore: asyncio.Semaphore,
) -> common.ProbeRow:
    rows = common._rows(scenario)
    expected = common._expected(scenario)
    image_bytes = common._render_board(rows) if arm == "typed_image" else b""
    user_content: list[dict] = [
        {
            "type": "text",
            "text": common._prompt(
                scenario,
                rows,
                has_image=bool(image_bytes),
            ),
        }
    ]
    if image_bytes:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            }
        )
    schema = json.dumps(
        common.GomokuRoleDecision.model_json_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = [
        {
            "role": "system",
            "content": (
                common.ROLE_SYSTEM
                + "\nThe required JSON object must satisfy this JSON Schema:\n"
                + schema
            ),
        },
        {"role": "user", "content": user_content},
    ]
    started = time.perf_counter()
    try:
        async with semaphore:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0,
                    max_tokens=700,
                    stream=False,
                    response_format={"type": "json_object"},
                    extra_body={"thinking": {"type": "disabled"}},
                ),
                timeout=max(1.0, float(timeout_s)),
            )
        latency_ms = (time.perf_counter() - started) * 1000
        content = str(response.choices[0].message.content or "")
        decision = common.GomokuRoleDecision.model_validate_json(content)
        observed, scores = common._score(scenario, decision)
        prompt_tokens, output_tokens, thought_tokens = _usage(response)
        return common.ProbeRow(
            arm=arm,
            repeat=repeat,
            scenario_id=scenario.scenario_id,
            latency_ms=latency_ms,
            image_bytes=len(image_bytes),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thought_tokens=thought_tokens,
            completed=True,
            error="",
            expected=expected,
            observed=observed,
            scores=scores,
        )
    except Exception as exc:
        return common.ProbeRow(
            arm=arm,
            repeat=repeat,
            scenario_id=scenario.scenario_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            image_bytes=len(image_bytes),
            prompt_tokens=None,
            output_tokens=None,
            thought_tokens=None,
            completed=False,
            error=f"{type(exc).__name__}: {exc}",
            expected=expected,
            observed={},
            scores={},
        )


def _summary(rows: list[common.ProbeRow]) -> dict:
    result = common._summary(rows)
    for arm, values in result.items():
        completed = [row for row in rows if row.arm == arm and row.completed]
        output_tokens = [
            int(row.output_tokens)
            for row in completed
            if row.output_tokens is not None
        ]
        thought_tokens = [
            int(row.thought_tokens)
            for row in completed
            if row.thought_tokens is not None
        ]
        values["median_output_tokens"] = (
            round(statistics.median(output_tokens), 2) if output_tokens else None
        )
        values["reported_thought_tokens"] = sum(thought_tokens)
    return result


async def run_probe(args: argparse.Namespace) -> dict:
    if not str(getattr(settings, "DEEPSEEK_API_KEY", "") or "").strip():
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=settings.DEEPSEEK_BASE_URL,
        max_retries=0,
        timeout=max(1.0, float(args.timeout)),
    )
    semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))
    selected_arms = tuple(
        arm for arm in common.ARMS if arm in set(args.arm or common.ARMS)
    )
    selected_ids = set(args.scenario or ())
    selected_scenarios = tuple(
        scenario
        for scenario in common.SCENARIOS
        if not selected_ids or scenario.scenario_id in selected_ids
    )
    job_specs = [
        (scenario, arm, repeat)
        for repeat in range(1, args.repeats + 1)
        for scenario in selected_scenarios
        for arm in selected_arms
    ]
    if args.case:
        selected_cases = set(args.case)
        job_specs = [
            spec
            for spec in job_specs
            if f"{spec[1]}:{spec[0].scenario_id}" in selected_cases
        ]
    rows: list[common.ProbeRow] = []
    for index, (scenario, arm, repeat) in enumerate(job_specs, 1):
        row = await _run_one(
            client,
            model=args.model,
            scenario=scenario,
            arm=arm,
            repeat=repeat,
            timeout_s=args.timeout,
            semaphore=semaphore,
        )
        rows.append(row)
        print(
            f"[{index}/{len(job_specs)}] arm={arm} "
            f"scenario={scenario.scenario_id} completed={row.completed} "
            f"latency_ms={row.latency_ms:.0f}",
            flush=True,
        )
        if args.request_interval > 0 and index < len(job_specs):
            await asyncio.sleep(float(args.request_interval))
    await client.close()
    return {
        "schema": "amadeus.deepseek-vision-gomoku-role-probe.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "thinking_mode": "disabled",
        "arms": list(selected_arms),
        "repeats": args.repeats,
        "timeout_s": args.timeout,
        "image_contract": {
            "format": "PNG",
            "mode": "grayscale",
            "width": 384,
            "height": 384,
            "authoritative": False,
        },
        "scenarios": [asdict(scenario) for scenario in selected_scenarios],
        "summary": _summary(rows),
        "rows": [asdict(row) for row in rows],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--arm",
        nargs="+",
        choices=common.ARMS,
        default=list(common.ARMS),
    )
    parser.add_argument("--request-interval", type=float, default=0.0)
    parser.add_argument(
        "--scenario",
        nargs="+",
        choices=tuple(s.scenario_id for s in common.SCENARIOS),
        default=[],
    )
    parser.add_argument("--case", nargs="+", default=[])
    parser.add_argument(
        "--output-dir",
        default=str(
            ROOT / "runtime" / "e2e_reports" / "deepseek_vision_gomoku_role"
        ),
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = asyncio.run(run_probe(args))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"deepseek_vision_gomoku_role_{stamp}.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report={output_path}")


if __name__ == "__main__":
    main()
