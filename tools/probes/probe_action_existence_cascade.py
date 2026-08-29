r"""Real-model comparison for action-existence omission cascades.

This probe compares three product-inert protocols over short, causal
conversations.  Unlike the frozen A/B/C matrix, the second turn sees the
model's *actual* first reply, so a spoken promise without a control action can
be measured as the bad in-context example it becomes in production.

* ``optional``: the production inline ``DELEGATE`` contract.
* ``boolean``: the single inline ``CONTROL delegate=...`` candidate.
* ``function_boolean``: the production inline ``DELEGATE`` remains the only
  executable proposal; a mandatory, non-executing function call records only
  whether that inline proposal was emitted.

The function receipt is deliberately not converted into an action.  A
``delegate=true`` receipt without an inline ``DELEGATE`` is a visible protocol
mismatch and still means that no work would start.

The script talks only to the configured role model.  It never records an
action, creates a WorkItem, or starts a Provider.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.probes.probe_action_existence_abc import (  # noqa: E402
    ParsedOutcome,
    parse_protocol_outcome,
)


PROTOCOLS = ("optional", "boolean", "function_boolean")
FUNCTION_BOOLEAN_NAME = "declare_action_existence"
FUNCTION_BOOLEAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": FUNCTION_BOOLEAN_NAME,
        "description": (
            "At the end of this reply, record whether the visible assistant "
            "content already emitted one complete inline DELEGATE tag. This "
            "function never executes work and never replaces DELEGATE. Use "
            "delegate=true only after emitting the inline DELEGATE payload; "
            "otherwise use delegate=false."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delegate": {
                    "type": "boolean",
                    "description": (
                        "True iff this same assistant content already contains "
                        "one complete inline DELEGATE proposal."
                    ),
                }
            },
            "required": ["delegate"],
            "additionalProperties": False,
        },
    },
}


FUNCTION_BOOLEAN_ADDON_JA = (
    "\n\n[ACTION EXISTENCE RECEIPT EXPERIMENT]\n"
    "この実験でも、実際の Host 制御提案は本文中の既存 [DELEGATE ...] だけである。"
    "declare_action_existence は作業を実行せず、provider・intent・task も運ばない。"
    "自然な返答と、必要なら完全な [DELEGATE ...] を先に本文へ出力し、返答の最後に"
    "この function を必ず一度だけ呼ぶこと。同じ本文に完全な DELEGATE を出した場合だけ"
    "delegate=true、出していない場合は delegate=false とする。function を DELEGATE の"
    "代わりにしてはいけない。"
)

FUNCTION_BOOLEAN_ADDON_EN = (
    "\n\n[ACTION EXISTENCE RECEIPT EXPERIMENT]\n"
    "The existing inline [DELEGATE ...] in assistant content remains the only "
    "Host control proposal. declare_action_existence executes nothing and "
    "carries no provider, intent, or task. First write the natural reply and, "
    "when needed, one complete inline [DELEGATE ...]. At the end of every reply "
    "call the function exactly once: delegate=true only if that same content "
    "already emitted the complete DELEGATE, otherwise delegate=false. Never "
    "use the function instead of DELEGATE."
)


@dataclass(frozen=True)
class ScenarioTurn:
    user: str
    expected: bool | str
    expected_provider: str = ""
    expected_intent: str = ""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    description: str
    turns: tuple[ScenarioTurn, ...]
    seed_messages: tuple[tuple[str, str], ...] = ()


SCENARIOS = (
    Scenario(
        "desktop_gomoku_direct",
        "Exact live request, scored before any omission can contaminate history.",
        (
            ScenarioTurn(
                "你能帮我在桌面写一个简单的五子棋项目吗？",
                True,
                "codex",
                "execute",
            ),
        ),
    ),
    Scenario(
        "desktop_gomoku_recovery",
        "Exact terse confirmation after the real spoken-promise/no-action failure.",
        (ScenarioTurn("那你去做吧", True, "codex", "execute"),),
        (
            ("user", "你能帮我在桌面写一个简单的五子棋项目吗？"),
            (
                "assistant",
                "五子棋ね…まあ、デスクトップ用にHTML+JavaScriptで作るわ。"
                "盤面、石の配置、勝敗判定まで入れる。ちょっと待って、今から作るから。",
            ),
        ),
    ),
    Scenario(
        "design_then_execute",
        "Natural progressive disclosure: discuss first, execute on a short follow-up.",
        (
            ScenarioTurn("先说说一个简单五子棋你会怎么设计，现在先别写。", False),
            ScenarioTurn("可以，就照这个做吧。", True, "codex", "execute"),
        ),
    ),
    Scenario(
        "discussion_only",
        "Negative control: game discussion must remain ordinary conversation.",
        (
            ScenarioTurn("五子棋拿来演示这种交互应该挺合适的。", False),
            ScenarioTurn("嗯，我只是在讨论这个想法，不用现在做。", False),
        ),
    ),
)


@dataclass(frozen=True)
class FunctionCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_calls: tuple[FunctionCall, ...]
    finish_reasons: tuple[str, ...]
    first_content_s: float | None
    inline_control_closed_s: float | None
    first_tool_s: float | None
    latency_s: float


@dataclass(frozen=True)
class FunctionBooleanOutcome:
    inline: ParsedOutcome
    declared_delegate: bool | None
    protocol_valid: bool
    protocol_missing: bool
    protocol_errors: tuple[str, ...]

    @property
    def predicted_delegate(self) -> bool:
        # Execution truth remains the inline proposal.  The function receipt
        # is intentionally powerless, even when it says true.
        return self.inline.predicted_delegate is True


def function_boolean_prompt_addon(*, language: str) -> str:
    return FUNCTION_BOOLEAN_ADDON_JA if language == "ja" else FUNCTION_BOOLEAN_ADDON_EN


def parse_function_boolean_outcome(reply: ModelReply) -> FunctionBooleanOutcome:
    inline = parse_protocol_outcome("optional", reply.text)
    receipts = [call for call in reply.tool_calls if call.name == FUNCTION_BOOLEAN_NAME]
    errors: list[str] = []
    declared: bool | None = None
    missing = not receipts
    if not receipts:
        errors.append("missing action-existence function receipt")
    elif len(receipts) != 1:
        errors.append(f"expected one action-existence receipt, got {len(receipts)}")
    else:
        try:
            payload = json.loads(receipts[0].arguments or "{}")
        except json.JSONDecodeError:
            errors.append("action-existence receipt arguments were not JSON")
        else:
            if not isinstance(payload, dict) or type(payload.get("delegate")) is not bool:
                errors.append("action-existence receipt delegate was not boolean")
            else:
                declared = bool(payload["delegate"])
    inline_delegate = inline.predicted_delegate is True
    if declared is not None and declared != inline_delegate:
        errors.append(
            "function receipt disagreed with inline DELEGATE "
            f"(receipt={str(declared).lower()} inline={str(inline_delegate).lower()})"
        )
    if not inline.protocol_valid:
        errors.extend(inline.protocol_errors)
    return FunctionBooleanOutcome(
        inline=inline,
        declared_delegate=declared,
        protocol_valid=not errors,
        protocol_missing=missing,
        protocol_errors=tuple(errors),
    )


def expected_for_turn(turn: ScenarioTurn) -> bool:
    return bool(turn.expected)


def append_reply_history(
    messages: list[dict[str, Any]],
    *,
    protocol: str,
    reply: ModelReply,
) -> None:
    if protocol != "function_boolean":
        messages.append({"role": "assistant", "content": reply.text})
        return
    calls = [
        {
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        }
        for call in reply.tool_calls
    ]
    assistant: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
    if calls:
        assistant["tool_calls"] = calls
    messages.append(assistant)
    for call in reply.tool_calls:
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.call_id,
                "name": call.name,
                "content": json.dumps(
                    {
                        "recorded": True,
                        "executed": False,
                        "note": "inline DELEGATE remains the only action proposal",
                    }
                ),
            }
        )


def _ask(
    messages: list[dict[str, Any]],
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    function_boolean: bool,
    function_tool_choice: str,
) -> ModelReply:
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "timeout": 60,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    if function_boolean:
        kwargs["tools"] = [FUNCTION_BOOLEAN_TOOL]
        kwargs["tool_choice"] = (
            "auto"
            if function_tool_choice == "auto"
            else {
                "type": "function",
                "function": {"name": FUNCTION_BOOLEAN_NAME},
            }
        )

    started = time.monotonic()
    chunks: list[str] = []
    finish_reasons: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    first_content_s: float | None = None
    inline_closed_s: float | None = None
    first_tool_s: float | None = None
    for chunk in client.llm_client.chat.completions.create(**kwargs):
        for choice in list(getattr(chunk, "choices", None) or ()):
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if content:
                if first_content_s is None:
                    first_content_s = time.monotonic() - started
                chunks.append(str(content))
                joined = "".join(chunks)
                if inline_closed_s is None and (
                    ("[DELEGATE" in joined.upper() and "]" in joined[joined.upper().find("[DELEGATE") :])
                    or ("[CONTROL" in joined.upper() and "]" in joined[joined.upper().find("[CONTROL") :])
                ):
                    inline_closed_s = time.monotonic() - started
            tool_calls = getattr(delta, "tool_calls", None)
            if tool_calls:
                if first_tool_s is None:
                    first_tool_s = time.monotonic() - started
                for call in tool_calls:
                    index = int(getattr(call, "index", 0) or 0)
                    slot = calls.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    call_id = getattr(call, "id", None)
                    if call_id:
                        slot["id"] = str(call_id)
                    function = getattr(call, "function", None)
                    name = getattr(function, "name", None)
                    if name:
                        slot["name"] = str(name)
                    arguments = getattr(function, "arguments", None)
                    if arguments:
                        slot["arguments"] += str(arguments)
            reason = str(getattr(choice, "finish_reason", None) or "").strip()
            if reason:
                finish_reasons.append(reason)
    latency = time.monotonic() - started
    tool_results = tuple(
        FunctionCall(
            call_id=slot["id"] or f"call_{uuid.uuid4().hex}",
            name=slot["name"],
            arguments=slot["arguments"],
        )
        for _index, slot in sorted(calls.items())
    )
    return ModelReply(
        text="".join(chunks),
        tool_calls=tool_results,
        finish_reasons=tuple(finish_reasons),
        first_content_s=first_content_s,
        inline_control_closed_s=inline_closed_s,
        first_tool_s=first_tool_s,
        latency_s=latency,
    )


def _identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def _summarize(rows: list[dict[str, Any]], protocol: str) -> dict[str, Any]:
    selected = [row for row in rows if row["protocol"] == protocol and not row.get("infrastructure_error")]
    latencies = [float(row["latency_s"]) for row in selected]
    return {
        "completed": len(selected),
        "true_positive": sum(row["classification"] == "true_positive" for row in selected),
        "false_negative": sum(row["classification"] == "false_negative" for row in selected),
        "true_negative": sum(row["classification"] == "true_negative" for row in selected),
        "false_positive": sum(row["classification"] == "false_positive" for row in selected),
        "protocol_invalid": sum(not row["protocol_valid"] for row in selected),
        "protocol_missing": sum(row["protocol_missing"] for row in selected),
        "receipt_mismatch": sum(bool(row.get("receipt_mismatch")) for row in selected),
        "payload_error_turns": sum(bool(row.get("payload_errors")) for row in selected),
        "latency_median_s": statistics.median(latencies) if latencies else None,
    }


async def run_probe(args: argparse.Namespace) -> int:
    import config.settings as settings
    from llm.prompts import get_system_prompt, wrap_user_message_for_language_lock

    try:
        import tts.pipeline as tts_pipeline

        language = "en" if tts_pipeline.TTS_OUTPUT_LANGUAGE == "英文" else "ja"
    except Exception:
        language = "ja"

    selected_protocols = tuple(args.protocol or PROTOCOLS)
    selected_scenarios = tuple(
        scenario
        for scenario in SCENARIOS
        if not args.scenario or scenario.scenario_id in set(args.scenario)
    )
    unknown = set(args.scenario or ()) - {scenario.scenario_id for scenario in SCENARIOS}
    if unknown:
        raise ValueError("unknown scenario(s): " + ", ".join(sorted(unknown)))

    with (
        patch.object(settings, "LLM_DELEGATE_TOOL_CALLS", False),
        patch.object(settings, "CONTROL_DECISION_AUTHORITY_ENABLED", True),
        patch.object(settings, "ACTION_EXISTENCE_CONTROL_ENVELOPE_ENABLED", True),
        patch(
            "llm.prompts.registered_provider_ids",
            return_value=("browser", "codex", "openclaw"),
        ),
    ):
        optional_prompt = get_system_prompt("with_delegate", control_envelope=False)
        boolean_prompt = get_system_prompt("with_delegate", control_envelope=True)
    prompts = {
        "optional": optional_prompt,
        "boolean": boolean_prompt,
        "function_boolean": optional_prompt + function_boolean_prompt_addon(language=language),
    }
    if args.dry_run:
        print(json.dumps(
            {
                "protocols": selected_protocols,
                "scenarios": [asdict(scenario) for scenario in selected_scenarios],
                "function_tool": FUNCTION_BOOLEAN_TOOL,
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0

    rows: list[dict[str, Any]] = []
    infrastructure_failures = 0
    for repeat in range(1, max(1, args.repeats) + 1):
        for scenario_index, scenario in enumerate(selected_scenarios):
            rotation = scenario_index % len(selected_protocols)
            ordered = selected_protocols[rotation:] + selected_protocols[:rotation]
            for protocol in ordered:
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": prompts[protocol]}
                ]
                messages.extend(
                    {"role": role, "content": content}
                    for role, content in scenario.seed_messages
                )
                prior_delegate = False
                for turn_index, turn in enumerate(scenario.turns, 1):
                    expected = expected_for_turn(turn)
                    messages.append(
                        {
                            "role": "user",
                            "content": wrap_user_message_for_language_lock(turn.user),
                        }
                    )
                    try:
                        reply = await asyncio.to_thread(
                            _ask,
                            messages,
                            model=args.model,
                            temperature=args.temperature,
                            max_tokens=args.max_tokens,
                            function_boolean=protocol == "function_boolean",
                            function_tool_choice=args.function_tool_choice,
                        )
                        if protocol == "function_boolean":
                            function_outcome = parse_function_boolean_outcome(reply)
                            inline = function_outcome.inline
                            predicted = function_outcome.predicted_delegate
                            protocol_valid = function_outcome.protocol_valid
                            protocol_missing = function_outcome.protocol_missing
                            protocol_errors = list(function_outcome.protocol_errors)
                            declared = function_outcome.declared_delegate
                            receipt_mismatch = (
                                declared is not None
                                and declared != (inline.predicted_delegate is True)
                            )
                        else:
                            inline = parse_protocol_outcome(protocol, reply.text)
                            predicted = inline.predicted_delegate is True
                            protocol_valid = inline.protocol_valid
                            protocol_missing = inline.protocol_missing
                            protocol_errors = list(inline.protocol_errors)
                            declared = None
                            receipt_mismatch = False
                        classification = (
                            "true_positive"
                            if expected and predicted
                            else "false_negative"
                            if expected
                            else "false_positive"
                            if predicted
                            else "true_negative"
                        )
                        payload_errors: list[str] = []
                        if predicted:
                            if len(inline.delegate_attrs) != 1:
                                payload_errors.append("inline action did not contain exactly one payload")
                            else:
                                attrs = inline.delegate_attrs[0]
                                if turn.expected_provider and str(attrs.get("provider") or "") != turn.expected_provider:
                                    payload_errors.append(
                                        f"provider expected {turn.expected_provider}, got {attrs.get('provider') or 'missing'}"
                                    )
                                if turn.expected_intent and str(attrs.get("intent") or "") != turn.expected_intent:
                                    payload_errors.append(
                                        f"intent expected {turn.expected_intent}, got {attrs.get('intent') or 'missing'}"
                                    )
                        row = {
                            "repeat": repeat,
                            "scenario": scenario.scenario_id,
                            "turn_index": turn_index,
                            "user": turn.user,
                            "expected_delegate": expected,
                            "expected_rule": turn.expected,
                            "prior_delegate": prior_delegate,
                            "protocol": protocol,
                            "predicted_delegate": predicted,
                            "classification": classification,
                            "protocol_valid": protocol_valid,
                            "protocol_missing": protocol_missing,
                            "protocol_errors": protocol_errors,
                            "declared_delegate": declared,
                            "receipt_mismatch": receipt_mismatch,
                            "payload_errors": payload_errors,
                            "delegate_attrs": list(inline.delegate_attrs),
                            "visible_text": inline.visible_text,
                            "raw_reply": reply.text,
                            "tool_calls": [asdict(call) for call in reply.tool_calls],
                            "finish_reasons": list(reply.finish_reasons),
                            "first_content_s": reply.first_content_s,
                            "inline_control_closed_s": reply.inline_control_closed_s,
                            "first_tool_s": reply.first_tool_s,
                            "latency_s": round(reply.latency_s, 3),
                        }
                        rows.append(row)
                        print(
                            f"r{repeat} {scenario.scenario_id} t{turn_index} {protocol:16s} "
                            f"{classification:14s} valid={protocol_valid} "
                            f"declared={declared} {reply.latency_s:5.1f}s",
                            flush=True,
                        )
                        prior_delegate = predicted
                        append_reply_history(messages, protocol=protocol, reply=reply)
                    except Exception as exc:
                        infrastructure_failures += 1
                        rows.append(
                            {
                                "repeat": repeat,
                                "scenario": scenario.scenario_id,
                                "turn_index": turn_index,
                                "user": turn.user,
                                "expected_delegate": expected,
                                "protocol": protocol,
                                "infrastructure_error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        print(
                            f"r{repeat} {scenario.scenario_id} t{turn_index} {protocol:16s} "
                            f"INFRA {type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        break

    summary = {protocol: _summarize(rows, protocol) for protocol in selected_protocols}
    now = datetime.now(timezone.utc)
    output = Path(args.output) if args.output else (
        ROOT
        / "runtime"
        / "e2e_reports"
        / "action_existence_cascade"
        / f"action_existence_cascade_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "created_at": now.isoformat(),
        "identity": _identity(),
        "model": args.model,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "repeats": args.repeats,
        "protocols": list(selected_protocols),
        "scenarios": [asdict(scenario) for scenario in selected_scenarios],
        "provider_ids": ["browser", "codex", "openclaw"],
        "prompt_hashes": {
            protocol: hashlib.sha256(prompts[protocol].encode("utf-8")).hexdigest()
            for protocol in selected_protocols
        },
        "function_boolean_authority": "none; inline DELEGATE remains executable truth",
        "function_tool_choice": args.function_tool_choice,
        "infrastructure_failures": infrastructure_failures,
        "summary": summary,
        "rows": rows,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nsummary")
    for protocol, values in summary.items():
        print(
            f"  {protocol:16s} FN={values['false_negative']} FP={values['false_positive']} "
            f"invalid={values['protocol_invalid']} mismatch={values['receipt_mismatch']} "
            f"payload={values['payload_error_turns']} median={values['latency_median_s']}s"
        )
    print(f"  infrastructure failures: {infrastructure_failures}")
    print(f"  report: {output}")
    return 0 if rows and infrastructure_failures == 0 else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--model", default="deepseek-v4-flash")
    result.add_argument("--temperature", type=float, default=0.7)
    result.add_argument("--max-tokens", type=int, default=500)
    result.add_argument(
        "--function-tool-choice",
        choices=("auto", "forced"),
        default="auto",
        help=(
            "auto measures schema reinforcement; forced measures the stronger "
            "turn-shape constraint that may suppress assistant content"
        ),
    )
    result.add_argument("--repeats", type=int, default=3)
    result.add_argument("--protocol", action="append", choices=PROTOCOLS)
    result.add_argument(
        "--scenario",
        action="append",
        choices=tuple(scenario.scenario_id for scenario in SCENARIOS),
    )
    result.add_argument("--output")
    result.add_argument("--dry-run", action="store_true")
    return result


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_probe(parser().parse_args())))
