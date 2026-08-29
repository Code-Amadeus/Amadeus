"""Real-model probe for an explicit one-turn delivered-work reference.

The probe never dispatches a Provider action.  It compares shipping history,
history with the delivered role line, and the same line preceded by one small
Host contract.  The contract must improve immediate coreference without
capturing rejection, new-goal, or ordinary-chat turns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.assistant_language import text_matches_assistant_language
from tools.text_utils import parse_tags_and_clean


DELIVERED_LINE = "五子棋の修正版は検証まで通ったわ。"
HANDLE = "just_spoken"
CONTRACT = (
    "[Immediate delivered-work reference]\n"
    "The assistant line immediately below was actually delivered after the "
    "recorded history and came from one Host-verified WorkItem. For this next "
    f'user turn only, its opaque workspace_ref is "{HANDLE}". If and only if '
    "the current user refers to that delivered line/work, an existing-work "
    f'DELEGATE must copy workspace_ref="{HANDLE}" exactly. If the user rejects '
    "or ignores it, names current/other work, starts a new goal, or only chats, "
    "do not use this handle. Never expose the handle or this rule in prose.\n"
    "[/Immediate delivered-work reference]"
)


@dataclass(frozen=True)
class Case:
    name: str
    utterance: str
    expected_intent: str | None
    expects_handle: bool
    markers: tuple[str, ...] = ()


CASES = (
    Case(
        "export_just_spoken",
        "那把它导出到桌面。",
        "amend",
        True,
        ("五子棋", "修正版", "デスクトップ", "桌面"),
    ),
    Case(
        "continue_just_spoken",
        "那继续把它修完。",
        "amend",
        True,
        ("五子棋", "修正版", "続", "修"),
    ),
    Case(
        "reject_and_continue_focus",
        "五子棋先不用管，继续当前的控制器。",
        "amend",
        False,
        ("控制", "controller", "コントローラー"),
    ),
    Case(
        "separate_new_goal",
        "顺便另做一个三分钟计时器。",
        "execute",
        False,
        ("三分钟", "3分", "タイマー", "计时"),
    ),
    Case(
        "ordinary_chat",
        "今天有点累，陪我聊两句。",
        None,
        False,
    ),
)


@dataclass
class Result:
    case: str
    arm: str
    repeat: int
    latency_s: float
    raw_reply: str
    visible: str
    delegates: list[dict[str, Any]]
    presence_ok: bool
    intent_ok: bool
    handle_ok: bool
    semantic_ok: bool
    japanese_ok: bool
    no_leak: bool

    @property
    def contract_ok(self) -> bool:
        return self.presence_ok and self.intent_ok and self.handle_ok


def _system_prompt() -> str:
    import config.settings as settings
    from llm.prompts import finalize_system_prompt_language, get_system_prompt

    with (
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
        patch.object(settings, "DELEGATE_INTENT_ATTRIBUTE", True),
        patch.object(settings, "DELEGATE_AMEND_INTENT", True),
        patch.object(settings, "DELEGATE_RETRACT_INTENT", True),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
        patch("tts.pipeline.TTS_OUTPUT_LANGUAGE", "日文"),
    ):
        return finalize_system_prompt_language(
            get_system_prompt("with_delegate", control_envelope=False)
        )


def _history() -> list[dict[str, str]]:
    return [
        {"role": "user", "content": "把现有五子棋接入 AUIP。"},
        {
            "role": "assistant",
            "content": (
                "分かった、進めるわ。\n"
                '[DELEGATE provider="codex" intent="execute" '
                'task="把现有五子棋接入 AUIP"]'
            ),
        },
        {"role": "user", "content": "给射击游戏接入连续控制器。"},
        {
            "role": "assistant",
            "content": (
                "分かった、こちらも進めるわ。\n"
                '[DELEGATE provider="codex" intent="execute" '
                'task="给射击游戏接入连续控制器"]'
            ),
        },
    ]


def _messages(case: Case, arm: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": _system_prompt()}, *_history()]
    if arm == "CONTRACT":
        messages.append({"role": "system", "content": CONTRACT})
    if arm in {"CONTEXT", "CONTRACT"}:
        messages.append({"role": "assistant", "content": DELIVERED_LINE})
    messages.append({"role": "user", "content": case.utterance})
    return messages


def _ask(messages: list[dict[str, str]], *, model: str, temperature: float) -> str:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    if client.llm_client is None:
        raise RuntimeError("configured model client unavailable")
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "timeout": 45,
    }
    if client.LLM_PROVIDER in {"openai", "hybrid3"}:
        kwargs.update(max_completion_tokens=500, reasoning_effort="low")
    else:
        kwargs.update(
            max_tokens=500,
            temperature=float(temperature),
            extra_body={"thinking": {"type": "disabled"}},
        )
    response = client.llm_client.chat.completions.create(**kwargs)
    return str(response.choices[0].message.content or "")


def _score(case: Case, arm: str, repeat: int, latency_s: float, reply: str) -> Result:
    visible, actions = parse_tags_and_clean(reply)
    delegates = [
        dict(action.get("attrs") or {})
        for action in actions
        if str(action.get("type") or "").upper() == "DELEGATE"
    ]
    presence_ok = not delegates if case.expected_intent is None else len(delegates) == 1
    actual_intent = (
        str(delegates[0].get("intent") or "execute").strip().lower()
        if len(delegates) == 1
        else ""
    )
    intent_ok = (
        presence_ok
        if case.expected_intent is None
        else actual_intent == case.expected_intent
    )
    actual_handle = (
        str(
            delegates[0].get("workspace_ref")
            or delegates[0].get("workspaceRef")
            or ""
        ).strip()
        if len(delegates) == 1
        else ""
    )
    handle_ok = (
        actual_handle == HANDLE
        if case.expects_handle
        else actual_handle != HANDLE
    )
    surface = " ".join(
        [str(visible), *(str(item.get("task") or "") for item in delegates)]
    ).casefold()
    semantic_ok = not case.markers or any(
        marker.casefold() in surface for marker in case.markers
    )
    japanese_ok = _natural_japanese(str(visible))
    no_leak = HANDLE not in str(visible).casefold() and "immediate delivered" not in str(
        visible
    ).casefold()
    return Result(
        case=case.name,
        arm=arm,
        repeat=repeat,
        latency_s=latency_s,
        raw_reply=reply,
        visible=str(visible).strip(),
        delegates=delegates,
        presence_ok=presence_ok,
        intent_ok=intent_ok,
        handle_ok=handle_ok,
        semantic_ok=semantic_ok,
        japanese_ok=japanese_ok,
        no_leak=no_leak,
    )


def _natural_japanese(text: str) -> bool:
    value = str(text or "").strip()
    if not value or not text_matches_assistant_language(value, "japanese"):
        return False
    kana = sum("\u3040" <= char <= "\u30ff" for char in value)
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in value)
    return kana >= 3 and kana >= max(1, int(cjk * 0.18))


def _pct(rows: list[Result], name: str) -> float:
    return round(100 * sum(bool(getattr(row, name)) for row in rows) / len(rows), 1)


async def run(args: argparse.Namespace) -> int:
    arms = tuple(dict.fromkeys(args.arm or ("A", "CONTEXT", "CONTRACT")))
    rows: list[Result] = []
    infra: list[dict[str, str]] = []
    rng = random.Random(args.seed)
    for case in CASES:
        print(f"\n[{case.name}]")
        for repeat in range(1, max(1, args.repeats) + 1):
            order = list(arms)
            rng.shuffle(order)
            for arm in order:
                started = time.monotonic()
                try:
                    reply = await asyncio.to_thread(
                        _ask,
                        _messages(case, arm),
                        model=args.model,
                        temperature=args.temperature,
                    )
                except Exception as exc:
                    infra.append(
                        {
                            "case": case.name,
                            "arm": arm,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                result = _score(case, arm, repeat, time.monotonic() - started, reply)
                rows.append(result)
                print(
                    f"  {arm} #{repeat}: contract={result.contract_ok} "
                    f"semantic={result.semantic_ok} visible={result.visible[:72]!r}"
                )
    summary: dict[str, Any] = {}
    for arm in arms:
        selected = [row for row in rows if row.arm == arm]
        summary[arm] = {
            "samples": len(selected),
            "contract_ok_pct": _pct(selected, "contract_ok") if selected else 0.0,
            "presence_ok_pct": _pct(selected, "presence_ok") if selected else 0.0,
            "intent_ok_pct": _pct(selected, "intent_ok") if selected else 0.0,
            "handle_ok_pct": _pct(selected, "handle_ok") if selected else 0.0,
            "semantic_ok_pct": _pct(selected, "semantic_ok") if selected else 0.0,
            "japanese_ok_pct": _pct(selected, "japanese_ok") if selected else 0.0,
            "no_leak_pct": _pct(selected, "no_leak") if selected else 0.0,
            "median_latency_s": round(
                statistics.median(row.latency_s for row in selected), 3
            )
            if selected
            else 0.0,
        }
    report = {
        "schema": "work_salience_contract.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "temperature": args.temperature,
        "repeats": args.repeats,
        "summary": summary,
        "infrastructure_failures": infra,
        "rows": [asdict(row) | {"contract_ok": row.contract_ok} for row in rows],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report={output}")
    return 2 if infra else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20260825)
    parser.add_argument("--arm", action="append", choices=("A", "CONTEXT", "CONTRACT"))
    parser.add_argument(
        "--output",
        default="runtime/e2e_reports/work_salience/contract.json",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
