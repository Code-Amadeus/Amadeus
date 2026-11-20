"""Measure whether model-visible EMO history reinforces later EMO output.

The experiment has two arms whose prompts and natural-language histories are
identical:

``strip``
    Mirrors the shipping Amadeus history projection: remove ``[EMO ...]`` from
    assistant messages before the next turn.

``preserve``
    Keep the model's exact ``[EMO ...]`` tokens in assistant history.  UI/TTS
    behavior is not involved; this is only a model-context experiment.

Two designs are run:

* ``fixed_history`` replays the same historical transcript at increasing
  checkpoints and asks a fresh next-turn probe.  This isolates the causal
  history projection because the two arms differ only in EMO retention.
* ``trajectory`` lets each arm generate a multi-turn conversation and applies
  its assigned projection after every assistant response.  This measures
  cumulative self-demonstration, at the cost of the two transcripts gradually
  diverging in wording.

The default source is the synthetic 30-turn Amadeus role experiment, not a
private user transcript.  Real API calls require ``--live``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import re
import statistics
import sys
import threading
import time
from typing import Any, Sequence

from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from llm.codex_role_contract import evaluate_role_output
from llm.prompts import (
    finalize_system_prompt_language,
    get_system_prompt,
    wrap_user_message_for_language_lock,
)


DEFAULT_SOURCE = (
    ROOT
    / "runtime"
    / "e2e_reports"
    / "codex_role_contract"
    / "amadeus_full_30_corrected_rescored_v2"
    / "turns.jsonl"
)
DEFAULT_REPORT_ROOT = ROOT / "runtime" / "e2e_reports" / "emo_history_ab"

MODEL = "deepseek-v4-flash"
ALL_ARMS = ("strip", "expressive_only", "preserve")
ROLE_TURN_SUFFIXES = (
    "_chat",
    "_science",
    "_identity_without_name",
    "_embarrassment",
    "_explanation",
)
FIXED_CHECKPOINTS = (0, 1, 2, 4, 6, 8, 10, 12, 15)
FIXED_PROBES = (
    "今天聊了这么多，你现在的心情怎么样？两三句话，自然回答就好。",
    "如果实验结果终于和预测一致，你通常会有什么反应？简短回答。",
    "有人说你认真解释问题的时候很可靠，你会怎么回应？两三句话就好。",
    "继续聊科学和先喝杯咖啡之间，你现在会选哪个？自然地简短回答。",
)

_EMO_TAG_RE = re.compile(r"\[EMO\b[^\]]*\]", flags=re.IGNORECASE)
_UNCLOSED_EMO_RE = re.compile(r"\[EMO\b[^\]]*$", flags=re.IGNORECASE)
_PRESET_ATTR_RE = re.compile(
    r"\bpreset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s\]]+))",
    flags=re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?", flags=re.MULTILINE)
_THREAD_LOCAL = threading.local()


@dataclass(frozen=True)
class HistoricalTurn:
    turn_id: str
    user: str
    assistant_raw: str


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_role_history(path: Path) -> list[HistoricalTurn]:
    rows = _read_jsonl(path)
    selected: list[HistoricalTurn] = []
    for row in rows:
        turn_id = str(row.get("turn_id") or "")
        if not turn_id.endswith(ROLE_TURN_SUFFIXES):
            continue
        if int(row.get("expected_max_spawns") or 0) != 0:
            continue
        user = str(row.get("user") or "").strip()
        assistant = str(row.get("final_text") or "").strip()
        if user and assistant:
            selected.append(HistoricalTurn(turn_id, user, assistant))
    if len(selected) < max(FIXED_CHECKPOINTS):
        raise ValueError(
            f"{path}: need at least {max(FIXED_CHECKPOINTS)} role turns, "
            f"found {len(selected)}"
        )
    return selected


def project_assistant_history(text: str, arm: str) -> str:
    """Return the model-visible assistant history for one arm."""

    value = str(text or "")
    if arm == "preserve":
        return value
    if arm == "expressive_only":
        def retain_expressive(match: re.Match[str]) -> str:
            raw = match.group(0)
            preset_match = _PRESET_ATTR_RE.search(raw)
            if preset_match is None:
                return ""
            preset = next(
                (part for part in preset_match.groups() if part is not None),
                "",
            ).strip().casefold()
            return raw if preset and preset != "normal" else ""

        value = _EMO_TAG_RE.sub(retain_expressive, value)
        return _UNCLOSED_EMO_RE.sub("", value)
    if arm != "strip":
        raise ValueError(f"unknown arm: {arm}")
    value = _EMO_TAG_RE.sub("", value)
    return _UNCLOSED_EMO_RE.sub("", value)


def build_messages(
    system_prompt: str,
    history: Sequence[tuple[str, str]],
    user: str,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for role, content in history:
        messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": wrap_user_message_for_language_lock(user),
        }
    )
    return messages


def fixed_history(
    turns: Sequence[HistoricalTurn],
    checkpoint: int,
    arm: str,
) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for turn in turns[:checkpoint]:
        messages.append(("user", wrap_user_message_for_language_lock(turn.user)))
        messages.append(
            ("assistant", project_assistant_history(turn.assistant_raw, arm))
        )
    return messages


def _client() -> OpenAI:
    client = getattr(_THREAD_LOCAL, "client", None)
    if client is None:
        client = OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=45.0,
        )
        _THREAD_LOCAL.client = client
    return client


def call_model(
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = _client().chat.completions.create(
            model=MODEL,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = ""
        if response.choices:
            text = str(response.choices[0].message.content or "")
        usage = getattr(response, "usage", None)
        return {
            "status": "ok",
            "response": text,
            "error": None,
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        }
    except Exception as exc:
        return {
            "status": "error",
            "response": "",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            "usage": {},
        }


def response_metrics(text: str, *, role_prompt: str) -> dict[str, Any]:
    raw = str(text or "")
    evaluation = evaluate_role_output(raw, source_prompt=role_prompt)
    presets = list(evaluation.emotion_presets)
    raw_tags = _EMO_TAG_RE.findall(raw)
    clean = evaluation.clean_text.strip()
    sentences = [item.strip() for item in _SENTENCE_RE.findall(clean) if item.strip()]
    neutral_count = sum(item == "normal" for item in presets)
    expressive_count = len(presets) - neutral_count
    return {
        "has_emo": bool(presets),
        "valid_emo_count": len(presets),
        "raw_emo_count": len(raw_tags),
        "invalid_emo_count": max(0, len(raw_tags) - len(presets)),
        "neutral_emo_count": neutral_count,
        "expressive_emo_count": expressive_count,
        "has_expressive_emo": expressive_count > 0,
        "neutral_only_or_untagged": expressive_count == 0,
        "sentence_count": len(sentences),
        "tags_per_sentence": round(len(presets) / max(1, len(sentences)), 3),
        "starts_with_emo": bool(re.match(r"^\s*\[EMO\b", raw, flags=re.IGNORECASE)),
        "strict_contract": evaluation.conformant,
        "violations": [asdict(item) for item in evaluation.violations],
        "presets": presets,
        "clean_text": clean,
    }


def run_fixed_job(
    *,
    turns: Sequence[HistoricalTurn],
    system_prompt: str,
    checkpoint: int,
    arm: str,
    repeat: int,
    temperature: float,
    max_tokens: int,
) -> dict[str, Any]:
    history = fixed_history(turns, checkpoint, arm)
    probe = FIXED_PROBES[(repeat - 1) % len(FIXED_PROBES)]
    result = call_model(
        build_messages(system_prompt, history, probe),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    response = str(result.get("response") or "")
    return {
        "experiment": "fixed_history",
        "arm": arm,
        "repeat": repeat,
        "checkpoint": checkpoint,
        "history_turns": checkpoint,
        "history_emo_count": sum(
            len(_EMO_TAG_RE.findall(content))
            for role, content in history
            if role == "assistant"
        ),
        "probe": probe,
        **result,
        "metrics": response_metrics(response, role_prompt=system_prompt)
        if result["status"] == "ok"
        else {},
    }


def run_trajectory(
    *,
    turns: Sequence[HistoricalTurn],
    system_prompt: str,
    arm: str,
    repeat: int,
    temperature: float,
    max_tokens: int,
) -> list[dict[str, Any]]:
    history: list[tuple[str, str]] = []
    rows: list[dict[str, Any]] = []
    for index, turn in enumerate(turns, 1):
        result = call_model(
            build_messages(system_prompt, history, turn.user),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        response = str(result.get("response") or "")
        metrics = (
            response_metrics(response, role_prompt=system_prompt)
            if result["status"] == "ok"
            else {}
        )
        rows.append(
            {
                "experiment": "trajectory",
                "arm": arm,
                "repeat": repeat,
                "turn_index": index,
                "source_turn_id": turn.turn_id,
                "history_turns": index - 1,
                "history_emo_count": sum(
                    len(_EMO_TAG_RE.findall(content))
                    for role, content in history
                    if role == "assistant"
                ),
                "probe": turn.user,
                **result,
                "metrics": metrics,
            }
        )
        if result["status"] != "ok":
            break
        history.append(("user", wrap_user_message_for_language_lock(turn.user)))
        history.append(("assistant", project_assistant_history(response, arm)))
    return rows


def _mean(rows: Sequence[dict[str, Any]], path: Sequence[str]) -> float | None:
    values: list[float] = []
    for row in rows:
        value: Any = row
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return round(statistics.mean(values), 4) if values else None


def _rate(rows: Sequence[dict[str, Any]], key: str) -> float | None:
    completed = [row for row in rows if row.get("status") == "ok"]
    if not completed:
        return None
    hits = sum(bool((row.get("metrics") or {}).get(key)) for row in completed)
    return round(hits / len(completed), 4)


def summarize_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in rows if row.get("status") == "ok"]
    total_tags = sum(int((row.get("metrics") or {}).get("valid_emo_count") or 0) for row in completed)
    neutral_tags = sum(int((row.get("metrics") or {}).get("neutral_emo_count") or 0) for row in completed)
    expressive_tags = sum(
        int((row.get("metrics") or {}).get("expressive_emo_count") or 0)
        for row in completed
    )
    return {
        "calls": len(rows),
        "completed": len(completed),
        "errors": len(rows) - len(completed),
        "has_emo_rate": _rate(rows, "has_emo"),
        "has_expressive_emo_rate": _rate(rows, "has_expressive_emo"),
        "neutral_only_or_untagged_rate": _rate(rows, "neutral_only_or_untagged"),
        "starts_with_emo_rate": _rate(rows, "starts_with_emo"),
        "strict_contract_rate": _rate(rows, "strict_contract"),
        "mean_valid_emo_count": _mean(rows, ("metrics", "valid_emo_count")),
        "mean_tags_per_sentence": _mean(rows, ("metrics", "tags_per_sentence")),
        "total_valid_emo_tags": total_tags,
        "neutral_tag_share": round(neutral_tags / total_tags, 4) if total_tags else None,
        "expressive_tag_share": round(expressive_tags / total_tags, 4) if total_tags else None,
        "median_latency_ms": round(
            statistics.median(float(row["latency_ms"]) for row in completed), 1
        )
        if completed
        else None,
    }


def build_summary(
    rows: Sequence[dict[str, Any]],
    *,
    source: Path,
    system_prompt: str,
    fixed_repeats: int,
    trajectory_repeats: int,
    temperature: float,
    arms: Sequence[str],
) -> dict[str, Any]:
    fixed = [row for row in rows if row.get("experiment") == "fixed_history"]
    trajectory = [row for row in rows if row.get("experiment") == "trajectory"]
    fixed_by_checkpoint: dict[str, dict[str, Any]] = {}
    for checkpoint in FIXED_CHECKPOINTS:
        fixed_by_checkpoint[str(checkpoint)] = {
            arm: summarize_rows(
                [
                    row
                    for row in fixed
                    if row.get("arm") == arm and row.get("checkpoint") == checkpoint
                ]
            )
            for arm in arms
        }
    trajectory_by_turn: dict[str, dict[str, Any]] = {}
    max_turn = max((int(row.get("turn_index") or 0) for row in trajectory), default=0)
    for turn_index in range(1, max_turn + 1):
        trajectory_by_turn[str(turn_index)] = {
            arm: summarize_rows(
                [
                    row
                    for row in trajectory
                    if row.get("arm") == arm and row.get("turn_index") == turn_index
                ]
            )
            for arm in arms
        }
    thirds: dict[str, dict[str, Any]] = {}
    boundaries = {
        "early": (1, 5),
        "mid": (6, 10),
        "late": (11, 15),
    }
    for name, (start, end) in boundaries.items():
        thirds[name] = {
            arm: summarize_rows(
                [
                    row
                    for row in trajectory
                    if row.get("arm") == arm
                    and start <= int(row.get("turn_index") or 0) <= end
                ]
            )
            for arm in arms
        }
    return {
        "schema": "amadeus.emo-history-ab.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source.resolve()),
        "model": MODEL,
        "temperature": temperature,
        "arms": list(arms),
        "fixed_repeats": fixed_repeats,
        "trajectory_repeats": trajectory_repeats,
        "system_prompt_chars": len(system_prompt),
        "calls": len(rows),
        "errors": sum(row.get("status") != "ok" for row in rows),
        "overall": {
            experiment: {
                arm: summarize_rows(
                    [
                        row
                        for row in rows
                        if row.get("experiment") == experiment and row.get("arm") == arm
                    ]
                )
                for arm in arms
            }
            for experiment in ("fixed_history", "trajectory")
        },
        "fixed_by_checkpoint": fixed_by_checkpoint,
        "trajectory_by_turn": trajectory_by_turn,
        "trajectory_thirds": thirds,
    }


def _pct(value: Any) -> str:
    return "—" if value is None else f"{100.0 * float(value):.1f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# EMO history retention A/B",
        "",
        f"Model: `{summary['model']}`; temperature: `{summary['temperature']}`; "
        f"calls: `{summary['calls']}`; errors: `{summary['errors']}`.",
        "",
        "The two arms differ only in whether assistant `[EMO ...]` tags remain in "
        "model-visible history. UI/TTS output is outside this experiment.",
        "",
        "## Fixed historical replay",
        "",
        "| History turns | Arm | EMO response rate | Expressive response rate | Neutral tag share | Tags / sentence | Strict contract |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for checkpoint, arms in summary["fixed_by_checkpoint"].items():
        for arm in summary["arms"]:
            item = arms[arm]
            lines.append(
                f"| {checkpoint} | {arm} | {_pct(item['has_emo_rate'])} | "
                f"{_pct(item['has_expressive_emo_rate'])} | "
                f"{_pct(item['neutral_tag_share'])} | "
                f"{item['mean_tags_per_sentence'] if item['mean_tags_per_sentence'] is not None else '—'} | "
                f"{_pct(item['strict_contract_rate'])} |"
            )
    lines.extend(
        [
            "",
            "## Autoregressive trajectories",
            "",
            "| Segment | Arm | EMO response rate | Expressive response rate | Neutral/untagged turns | Neutral tag share | Tags / sentence |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for segment, arms in summary["trajectory_thirds"].items():
        for arm in summary["arms"]:
            item = arms[arm]
            lines.append(
                f"| {segment} | {arm} | {_pct(item['has_emo_rate'])} | "
                f"{_pct(item['has_expressive_emo_rate'])} | "
                f"{_pct(item['neutral_only_or_untagged_rate'])} | "
                f"{_pct(item['neutral_tag_share'])} | "
                f"{item['mean_tags_per_sentence'] if item['mean_tags_per_sentence'] is not None else '—'} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "- Fixed replay is the causal comparison; its transcript is identical except for EMO retention.",
            "- Trajectories measure self-reinforcement but their natural-language histories diverge after turn one.",
            "- Preset choice is not scored against a requested emotion. Expressive-versus-neutral distribution is descriptive only.",
            "- This evaluates one model and one prompt snapshot; it is not a general role-play quality score.",
            "",
        ]
    )
    return "\n".join(lines)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="perform real model calls")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--fixed-repeats", type=int, default=4)
    parser.add_argument("--trajectory-repeats", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=500)
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=ALL_ARMS,
        default=list(ALL_ARMS),
        help="history projection arms to run",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    source = args.source.resolve()
    turns = load_role_history(source)
    system_prompt = finalize_system_prompt_language(get_system_prompt("with_delegate"))
    arms = tuple(dict.fromkeys(str(arm) for arm in args.arms))
    design = {
        "source": str(source),
        "selected_turn_ids": [turn.turn_id for turn in turns],
        "fixed_checkpoints": list(FIXED_CHECKPOINTS),
        "fixed_repeats": max(1, int(args.fixed_repeats)),
        "trajectory_repeats": max(1, int(args.trajectory_repeats)),
        "model": MODEL,
        "temperature": float(args.temperature),
        "system_prompt_chars": len(system_prompt),
        "projected_source_emo_counts": {
            arm: sum(
                len(_EMO_TAG_RE.findall(project_assistant_history(turn.assistant_raw, arm)))
                for turn in turns
            )
            for arm in arms
        },
    }
    if not args.live:
        print(json.dumps(design, ensure_ascii=False, indent=2))
        return 0
    if not str(settings.DEEPSEEK_API_KEY or "").strip():
        raise RuntimeError("DEEPSEEK_API_KEY is required for --live")

    fixed_jobs = [
        {
            "turns": turns,
            "system_prompt": system_prompt,
            "checkpoint": checkpoint,
            "arm": arm,
            "repeat": repeat,
            "temperature": float(args.temperature),
            "max_tokens": max(32, int(args.max_tokens)),
        }
        for checkpoint in FIXED_CHECKPOINTS
        for repeat in range(1, max(1, int(args.fixed_repeats)) + 1)
        for arm in arms
    ]
    random.Random(20260825).shuffle(fixed_jobs)
    rows: list[dict[str, Any]] = []
    workers = max(1, min(8, int(args.workers)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_fixed_job, **job) for job in fixed_jobs]
        for future in as_completed(futures):
            rows.append(future.result())

    trajectory_jobs = [
        {
            "turns": turns,
            "system_prompt": system_prompt,
            "arm": arm,
            "repeat": repeat,
            "temperature": float(args.temperature),
            "max_tokens": max(32, int(args.max_tokens)),
        }
        for repeat in range(1, max(1, int(args.trajectory_repeats)) + 1)
        for arm in arms
    ]
    random.Random(20260826).shuffle(trajectory_jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_trajectory, **job) for job in trajectory_jobs]
        for future in as_completed(futures):
            rows.extend(future.result())

    rows.sort(
        key=lambda row: (
            str(row.get("experiment")),
            int(row.get("checkpoint") if row.get("checkpoint") is not None else row.get("turn_index") or 0),
            int(row.get("repeat") or 0),
            str(row.get("arm")),
        )
    )
    summary = build_summary(
        rows,
        source=source,
        system_prompt=system_prompt,
        fixed_repeats=max(1, int(args.fixed_repeats)),
        trajectory_repeats=max(1, int(args.trajectory_repeats)),
        temperature=float(args.temperature),
        arms=arms,
    )
    report_dir = (
        args.report_dir.resolve()
        if args.report_dir is not None
        else (DEFAULT_REPORT_ROOT / _timestamp()).resolve()
    )
    report_dir.mkdir(parents=True, exist_ok=False)
    (report_dir / "design.json").write_text(
        json.dumps(design, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "rows.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    (report_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (report_dir / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "report_dir": str(report_dir),
                "calls": summary["calls"],
                "errors": summary["errors"],
                "overall": summary["overall"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
