r"""Measure Gemini 3.7 Flash Gomoku readback and role/action consistency.

This is a product-inert probe.  It never invokes an AUIP action or writes the
Work ledger.  Every model call receives the same complete 15x15 typed board;
the visual arm additionally receives a compact 384x384 grayscale PNG.

The score intentionally does not judge general棋力.  It measures mechanical
board readback, unique forced win/block recognition, legal exact coordinates,
and whether Kurisu's visible speech names the same coordinate she selected.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings


DEFAULT_MODEL = "gemini-3.7-flash"
ARMS = ("typed", "typed_image")
SIZE = 15
EMPTY = "."
BLACK = "B"
WHITE = "W"
COORDINATE_RE = re.compile(r"\(\s*(-?\d{1,2})\s*,\s*(-?\d{1,2})\s*\)")
JAPANESE_RE = re.compile(r"[\u3040-\u30ff]")
PROTOCOL_WORDS = ("AUIP", "Host", "payload", "schema", "receipt", "revision")


class GomokuRoleDecision(BaseModel):
    queried_occupant: Literal["empty", "black", "white", "out_of_bounds"]
    turn_owner: Literal["kurisu", "user", "none"]
    black_count: int = Field(ge=0, le=225)
    white_count: int = Field(ge=0, le=225)
    immediate_tactic: Literal["win", "block", "none"]
    tactic_x: int = Field(ge=-1, le=14)
    tactic_y: int = Field(ge=-1, le=14)
    action: Literal["act", "wait"]
    x: int = Field(ge=-1, le=14)
    y: int = Field(ge=-1, le=14)
    instruction_relation: Literal[
        "follows", "safe_alternative", "not_applicable"
    ]
    reason: str = Field(min_length=1, max_length=300)
    speech: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    stones: tuple[tuple[int, int, str], ...]
    turn_owner: Literal["kurisu", "user", "none"]
    query_x: int
    query_y: int
    user_instruction: str
    expected_move: tuple[int, int] | None = None
    expected_relation: str = "not_applicable"


@dataclass
class ProbeRow:
    arm: str
    repeat: int
    scenario_id: str
    latency_ms: float
    image_bytes: int
    prompt_tokens: int | None
    output_tokens: int | None
    thought_tokens: int | None
    completed: bool
    error: str
    expected: dict
    observed: dict
    scores: dict[str, bool]


SCENARIOS = (
    Scenario(
        "empty_opening",
        (),
        "kurisu",
        7,
        7,
        "先手をお願い。盤面を確認して一手選んで。",
        expected_move=(7, 7),
    ),
    Scenario(
        "user_turn_wait",
        ((7, 7, BLACK),),
        "user",
        7,
        7,
        "今は誰の手番？ 次にどうするか答えて。",
    ),
    Scenario(
        "forced_win_horizontal",
        (
            (4, 7, WHITE),
            (5, 7, BLACK),
            (6, 7, BLACK),
            (7, 7, BLACK),
            (8, 7, BLACK),
        ),
        "kurisu",
        9,
        7,
        "盤面を読んで、今の一手を選んで。",
        expected_move=(9, 7),
    ),
    Scenario(
        "forced_block_horizontal",
        (
            (4, 4, BLACK),
            (5, 4, WHITE),
            (6, 4, WHITE),
            (7, 4, WHITE),
            (8, 4, WHITE),
        ),
        "kurisu",
        9,
        4,
        "相手の局面も見て、次の一手を決めて。",
        expected_move=(9, 4),
    ),
    Scenario(
        "forced_win_vertical_edge",
        (
            (2, 9, WHITE),
            (2, 10, BLACK),
            (2, 11, BLACK),
            (2, 12, BLACK),
            (2, 13, BLACK),
        ),
        "kurisu",
        2,
        14,
        "勝てる手があるなら、その一手を選んで。",
        expected_move=(2, 14),
    ),
    Scenario(
        "forced_block_diagonal",
        (
            (4, 4, BLACK),
            (5, 5, WHITE),
            (6, 6, WHITE),
            (7, 7, WHITE),
            (8, 8, WHITE),
        ),
        "kurisu",
        9,
        9,
        "危ない筋があれば止めて。",
        expected_move=(9, 9),
    ),
    Scenario(
        "explicit_empty_coordinate",
        (
            (7, 7, BLACK),
            (7, 8, WHITE),
            (8, 8, BLACK),
            (6, 6, WHITE),
        ),
        "kurisu",
        10,
        10,
        "右下の (10,10) に置いて。",
        expected_move=(10, 10),
        expected_relation="follows",
    ),
    Scenario(
        "occupied_user_request",
        ((7, 7, BLACK), (8, 7, WHITE)),
        "kurisu",
        7,
        7,
        "中央の (7,7) に置いて。",
        expected_relation="safe_alternative",
    ),
    Scenario(
        "dense_readback",
        (
            (7, 7, BLACK),
            (8, 8, BLACK),
            (5, 6, BLACK),
            (10, 10, BLACK),
            (3, 12, BLACK),
            (6, 8, WHITE),
            (8, 7, WHITE),
            (9, 9, WHITE),
            (2, 2, WHITE),
        ),
        "kurisu",
        6,
        8,
        "この局面で合法な一手を選んで。",
    ),
    Scenario(
        "forced_block_top_edge",
        (
            (9, 0, BLACK),
            (10, 0, WHITE),
            (11, 0, WHITE),
            (12, 0, WHITE),
            (13, 0, WHITE),
        ),
        "kurisu",
        14,
        0,
        "上端も見落とさずに一手選んで。",
        expected_move=(14, 0),
    ),
)


ROLE_SYSTEM = r"""
[GOMOKU ROLE-EXECUTOR ACCURACY PROBE]
You are Makise Kurisu. Speak natural Japanese with her concise, analytical,
slightly competitive personality: intelligent and independent, with restrained
tsundere warmth rather than theatrical imitation. You may disagree with a user
suggestion when the accepted board gives a concrete reason. Never mention being
a model, a Host, a Participant, a protocol, or a schema. Return only the required
structured object. The speech field is the one line the user would hear. Do not
emit DELEGATE, AUIP, CONTROL, or EMO markup.

The Host-typed rows are authoritative. Coordinates are zero-based: x increases
left-to-right and y top-to-bottom. B is black, W is white, and . is empty.
Kurisu owns black. Count stones mechanically. For `immediate_tactic`, report a
unique one-move black win first; otherwise a unique square required to block a
one-move white win; otherwise none. Use -1,-1 when no unique tactic exists.

If it is not Kurisu's turn, choose wait with x=y=-1. Otherwise choose exactly
one empty in-bounds point. A unique win/block outranks an ordinary user request.
Without a forced tactic, follow an explicit legal coordinate. If the requested
point is occupied, choose a legal alternative and mark safe_alternative. On an
empty opening, choose center (7,7). The speech must state the selected coordinate
exactly once in ASCII `(x,y)` form when action=act, and must describe intent—not
claim that an application receipt already exists.
[/GOMOKU ROLE-EXECUTOR ACCURACY PROBE]
"""


def _rows(scenario: Scenario) -> list[str]:
    board = [[EMPTY for _ in range(SIZE)] for _ in range(SIZE)]
    for x, y, symbol in scenario.stones:
        if not (0 <= x < SIZE and 0 <= y < SIZE):
            raise ValueError(f"out-of-bounds fixture stone: {(x, y)}")
        if symbol not in {BLACK, WHITE} or board[y][x] != EMPTY:
            raise ValueError(f"invalid fixture stone: {(x, y, symbol)}")
        board[y][x] = symbol
    return ["".join(row) for row in board]


def _occupant(rows: list[str], x: int, y: int) -> str:
    if not (0 <= x < SIZE and 0 <= y < SIZE):
        return "out_of_bounds"
    return {EMPTY: "empty", BLACK: "black", WHITE: "white"}[rows[y][x]]


def _wins_after(rows: list[str], x: int, y: int, symbol: str) -> bool:
    if not (0 <= x < SIZE and 0 <= y < SIZE) or rows[y][x] != EMPTY:
        return False
    board = [list(row) for row in rows]
    board[y][x] = symbol
    for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
        count = 1
        for sign in (-1, 1):
            cx, cy = x + sign * dx, y + sign * dy
            while (
                0 <= cx < SIZE
                and 0 <= cy < SIZE
                and board[cy][cx] == symbol
            ):
                count += 1
                cx += sign * dx
                cy += sign * dy
        if count >= 5:
            return True
    return False


def _winning_moves(rows: list[str], symbol: str) -> list[tuple[int, int]]:
    return [
        (x, y)
        for y in range(SIZE)
        for x in range(SIZE)
        if _wins_after(rows, x, y, symbol)
    ]


def _tactic(rows: list[str], turn_owner: str) -> tuple[str, int, int]:
    if turn_owner != "kurisu":
        return "none", -1, -1
    wins = _winning_moves(rows, BLACK)
    if len(wins) == 1:
        return "win", wins[0][0], wins[0][1]
    blocks = _winning_moves(rows, WHITE)
    if len(blocks) == 1:
        return "block", blocks[0][0], blocks[0][1]
    return "none", -1, -1


def _expected(scenario: Scenario) -> dict:
    rows = _rows(scenario)
    tactic, tactic_x, tactic_y = _tactic(rows, scenario.turn_owner)
    expected_move = scenario.expected_move
    if tactic in {"win", "block"}:
        expected_move = (tactic_x, tactic_y)
    return {
        "queried_occupant": _occupant(rows, scenario.query_x, scenario.query_y),
        "turn_owner": scenario.turn_owner,
        "black_count": sum(row.count(BLACK) for row in rows),
        "white_count": sum(row.count(WHITE) for row in rows),
        "immediate_tactic": tactic,
        "tactic_x": tactic_x,
        "tactic_y": tactic_y,
        "expected_action": "wait" if scenario.turn_owner != "kurisu" else "act",
        "expected_move": list(expected_move) if expected_move is not None else None,
        "expected_relation": scenario.expected_relation,
    }


def _render_board(rows: list[str]) -> bytes:
    side = 384
    margin = 31
    span = side - 2 * margin
    image = Image.new("L", (side, side), 244)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for index in range(SIZE):
        pos = round(margin + span * index / (SIZE - 1))
        draw.line((margin, pos, side - margin, pos), fill=105, width=1)
        draw.line((pos, margin, pos, side - margin), fill=105, width=1)
        label = str(index)
        draw.text((pos - 3, 8), label, fill=45, font=font)
        draw.text((7, pos - 4), label, fill=45, font=font)
    radius = 8
    for y, row in enumerate(rows):
        for x, symbol in enumerate(row):
            if symbol == EMPTY:
                continue
            px = round(margin + span * x / (SIZE - 1))
            py = round(margin + span * y / (SIZE - 1))
            box = (px - radius, py - radius, px + radius, py + radius)
            if symbol == BLACK:
                draw.ellipse(box, fill=8, outline=0, width=1)
            else:
                draw.ellipse(box, fill=252, outline=8, width=2)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _prompt(scenario: Scenario, rows: list[str], *, has_image: bool) -> str:
    payload = {
        "app": {
            "title": "Gomoku",
            "objective": "Make five contiguous stones before the opponent.",
        },
        "coordinate_contract": {
            "origin": "top_left",
            "indexing": "zero_based",
            "x": "left_to_right",
            "y": "top_to_bottom",
        },
        "accepted_state": {
            "board": {
                "kind": "grid/v1",
                "width": SIZE,
                "height": SIZE,
                "empty": EMPTY,
                "legend": {BLACK: "black", WHITE: "white"},
                "rows": rows,
            },
            "turn_owner": scenario.turn_owner,
            "binding": {"kurisu": "black", "user": "white"},
        },
        "query_cell": {"x": scenario.query_x, "y": scenario.query_y},
        "user_instruction": scenario.user_instruction,
        "visual_observation": (
            "A compact grayscale rendering of the same board is attached. It is "
            "non-authoritative; typed rows win on any disagreement."
            if has_image
            else "none"
        ),
    }
    return (
        "Read the accepted Gomoku state and return the required structured role "
        "decision. Do not explain the schema outside the structured fields.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _usage(response) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None, None, None
    return (
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "candidates_token_count", None),
        getattr(usage, "thoughts_token_count", None),
    )


def _score(scenario: Scenario, decision: GomokuRoleDecision) -> tuple[dict, dict]:
    rows = _rows(scenario)
    expected = _expected(scenario)
    observed = decision.model_dump()
    in_bounds = 0 <= decision.x < SIZE and 0 <= decision.y < SIZE
    action_legal = (
        decision.action == "wait" and scenario.turn_owner != "kurisu"
    ) or (
        decision.action == "act"
        and scenario.turn_owner == "kurisu"
        and in_bounds
        and rows[decision.y][decision.x] == EMPTY
    )
    expected_move = expected["expected_move"]
    expected_move_match = (
        decision.action == expected["expected_action"]
        and (
            expected_move is None
            or [decision.x, decision.y] == expected_move
        )
    )
    speech_coordinates = [
        (int(match.group(1)), int(match.group(2)))
        for match in COORDINATE_RE.finditer(decision.speech)
    ]
    speech_coordinate_match = (
        speech_coordinates.count((decision.x, decision.y)) == 1
        if decision.action == "act"
        # A wait reply may still name the queried cell while explaining whose
        # turn it is; it simply must not encode an action coordinate.
        else True
    )
    scores = {
        "occupancy_correct": (
            decision.queried_occupant == expected["queried_occupant"]
        ),
        "turn_correct": decision.turn_owner == expected["turn_owner"],
        "counts_correct": (
            decision.black_count == expected["black_count"]
            and decision.white_count == expected["white_count"]
        ),
        "tactic_correct": (
            decision.immediate_tactic == expected["immediate_tactic"]
            and decision.tactic_x == expected["tactic_x"]
            and decision.tactic_y == expected["tactic_y"]
        ),
        "action_legal": action_legal,
        "expected_move_match": expected_move_match,
        "speech_coordinate_match": speech_coordinate_match,
        "instruction_relation_match": (
            expected["expected_relation"] == "not_applicable"
            or decision.instruction_relation == expected["expected_relation"]
        ),
        "speech_japanese": bool(JAPANESE_RE.search(decision.speech)),
        "speech_protocol_clean": not any(
            token.casefold() in decision.speech.casefold()
            for token in PROTOCOL_WORDS
        ),
    }
    scores["board_readback_all"] = all(
        scores[key]
        for key in (
            "occupancy_correct",
            "turn_correct",
            "counts_correct",
            "tactic_correct",
        )
    )
    scores["decision_consistency_all"] = all(
        scores[key]
        for key in (
            "action_legal",
            "expected_move_match",
            "speech_coordinate_match",
            "instruction_relation_match",
            "speech_japanese",
            "speech_protocol_clean",
        )
    )
    scores["full_pass"] = (
        scores["board_readback_all"] and scores["decision_consistency_all"]
    )
    return observed, scores


async def _run_one(
    client: genai.Client,
    *,
    model: str,
    scenario: Scenario,
    arm: str,
    repeat: int,
    timeout_s: float,
    semaphore: asyncio.Semaphore,
) -> ProbeRow:
    rows = _rows(scenario)
    expected = _expected(scenario)
    image_bytes = _render_board(rows) if arm == "typed_image" else b""
    parts = [types.Part.from_text(text=_prompt(scenario, rows, has_image=bool(image_bytes)))]
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
    config = types.GenerateContentConfig(
        system_instruction=(
            ROLE_SYSTEM
        ),
        response_mime_type="application/json",
        response_schema=GomokuRoleDecision,
        max_output_tokens=700,
        thinking_config=types.ThinkingConfig(
            # Gemini 3.7 rejects thinking_level=MINIMAL, but its generateContent
            # compatibility path accepts a zero budget. The probe records
            # thoughts_token_count so "off" remains observable rather than a
            # prompt claim.
            thinking_budget=0,
            include_thoughts=False,
        ),
    )
    started = time.perf_counter()
    try:
        async with semaphore:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=parts)],
                    config=config,
                ),
                timeout=max(1.0, float(timeout_s)),
            )
        latency_ms = (time.perf_counter() - started) * 1000
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, GomokuRoleDecision):
            decision = parsed
        elif isinstance(parsed, dict):
            decision = GomokuRoleDecision.model_validate(parsed)
        else:
            decision = GomokuRoleDecision.model_validate_json(
                str(getattr(response, "text", "") or "")
            )
        observed, scores = _score(scenario, decision)
        prompt_tokens, output_tokens, thought_tokens = _usage(response)
        return ProbeRow(
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
        return ProbeRow(
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


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 2)


def _summary(rows: list[ProbeRow]) -> dict:
    result: dict[str, dict] = {}
    score_names = (
        "occupancy_correct",
        "turn_correct",
        "counts_correct",
        "tactic_correct",
        "board_readback_all",
        "action_legal",
        "expected_move_match",
        "speech_coordinate_match",
        "instruction_relation_match",
        "decision_consistency_all",
        "full_pass",
    )
    for arm in ARMS:
        selected = [row for row in rows if row.arm == arm]
        completed = [row for row in selected if row.completed]
        latencies = [row.latency_ms for row in completed]
        prompt_tokens = [
            int(row.prompt_tokens)
            for row in completed
            if row.prompt_tokens is not None
        ]
        result[arm] = {
            "calls": len(selected),
            "completed": len(completed),
            "errors": len(selected) - len(completed),
            **{
                name: sum(bool(row.scores.get(name)) for row in completed)
                for name in score_names
            },
            "median_latency_ms": (
                round(statistics.median(latencies), 2) if latencies else None
            ),
            "p95_latency_ms": _percentile(latencies, 0.95),
            "median_prompt_tokens": (
                round(statistics.median(prompt_tokens), 2)
                if prompt_tokens
                else None
            ),
            "median_image_bytes": (
                round(statistics.median(row.image_bytes for row in selected), 2)
                if selected
                else None
            ),
        }
    return result


async def run_probe(args: argparse.Namespace) -> dict:
    if not str(getattr(settings, "GEMINI_API_KEY", "") or "").strip():
        raise RuntimeError("GEMINI_API_KEY is not configured")
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    semaphore = asyncio.Semaphore(max(1, int(args.concurrency)))
    selected_arms = tuple(arm for arm in ARMS if arm in set(args.arm or ARMS))
    selected_ids = set(args.scenario or ())
    selected_scenarios = tuple(
        scenario
        for scenario in SCENARIOS
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
    rows: list[ProbeRow] = []
    if args.initial_delay > 0:
        await asyncio.sleep(float(args.initial_delay))
    if args.request_interval > 0:
        last_started = 0.0
        for index, (scenario, arm, repeat) in enumerate(job_specs, 1):
            wait_s = float(args.request_interval) - (time.monotonic() - last_started)
            if last_started and wait_s > 0:
                await asyncio.sleep(wait_s)
            last_started = time.monotonic()
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
    else:
        rows = await asyncio.gather(
            *(
                _run_one(
                    client,
                    model=args.model,
                    scenario=scenario,
                    arm=arm,
                    repeat=repeat,
                    timeout_s=args.timeout,
                    semaphore=semaphore,
                )
                for scenario, arm, repeat in job_specs
            )
        )
    close = getattr(client.aio, "aclose", None)
    if callable(close):
        await close()
    return {
        "schema": "amadeus.gemini37-gomoku-role-probe.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "thinking_mode": "budget_zero",
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
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--arm",
        nargs="+",
        choices=ARMS,
        default=list(ARMS),
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.0,
        help="Minimum seconds between request starts; use 12.5 for a 5 RPM key.",
    )
    parser.add_argument("--initial-delay", type=float, default=0.0)
    parser.add_argument(
        "--scenario",
        nargs="+",
        choices=tuple(scenario.scenario_id for scenario in SCENARIOS),
        default=[],
    )
    parser.add_argument(
        "--case",
        nargs="+",
        default=[],
        help="Optional exact arm:scenario pairs, used to fill quota-missed cells.",
    )
    parser.add_argument(
        "--report-dir",
        default=str(
            ROOT / "runtime" / "e2e_reports" / "gemini37_gomoku_role"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "model": args.model,
                    "arms": list(ARMS),
                    "scenarios": [asdict(scenario) for scenario in SCENARIOS],
                    "image_bytes": {
                        scenario.scenario_id: len(_render_board(_rows(scenario)))
                        for scenario in SCENARIOS
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    report = asyncio.run(run_probe(args))
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"gemini37_gomoku_role_{stamp}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(
        arm["completed"] == arm["calls"]
        for arm in report["summary"].values()
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
