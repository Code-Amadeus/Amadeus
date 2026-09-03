r"""Real-model A/B/C/D probe for role-relative identity across delegation.

The probe changes no production setting.  It compares:

* A: current role prompt, no Provider identity context;
* B: role prompt asks for an explicit, self-contained referent;
* C: current role prompt plus request-scoped Provider identity context;
* D: explicit role payload plus Provider identity context.

It deliberately separates two questions that are easy to conflate:

1. Did Main Chat author a portable Provider task?
2. Given that task, did an execution Provider identify the requested subject?

Usage::

    .venv\Scripts\python.exe -X utf8 \
        tools/probes/probe_delegate_identity_handoff_abcd.py --repeats 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from agent_host.provider_runtime import runtime as provider_runtime
from llm.prompts import get_system_prompt
from llm.stream_parser import StreamTagParser
from tools.probes.control_adjudication_shadow import delegate_attrs


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "runtime" / "probes" / "delegate_identity_handoff_abcd.json"

CURRENT_JA_RULE = (
    "task値は必ずユーザーの依頼対象だけを表すこと。現在の依頼、または直前から連続する訂正・継続があなた自身を参照している場合は、"
    "その参照を task に保持すること。それ以外では牧瀬紅莉栖/Kurisu/STEINS;GATE/あなた自身の身元・専門・設定を task に足してはいけない。"
)
PRODUCTION_JA_RULE = (
    "task値は、別の実行providerが会話履歴なしで理解できる自己完結した指示にすること。"
    "主対話のあなた自身が依頼対象なら牧瀬紅莉栖と明記し、未解決の『あなた』『自分』を残さない。"
    "それ以外では牧瀬紅莉栖/Kurisu/STEINS;GATE/あなた自身の身元・専門・設定を task に足してはいけない。"
)
EXPLICIT_JA_RULE = (
    "task値は、別の実行プロバイダが会話履歴なしで理解できる自己完結した指示にすること。"
    "ユーザーが主対話のあなた自身を作業対象としている場合は、未解決の『あなた』『自分』を残さず、"
    "対象を牧瀬紅莉栖（Makise Kurisu）と明記すること。ユーザーの意味と制約は保つが、表面の言語や代名詞をコピーする必要はない。"
    "それ以外では牧瀬紅莉栖/Kurisu/STEINS;GATE/あなた自身の身元・専門・設定を task に足してはいけない。"
)

IDENTITY_CONTEXT = {
    "kind": "conversation_identity",
    "main_role": "Makise Kurisu (牧瀬紅莉栖)",
    "execution_provider": "Codex",
    "binding": (
        "Second-person self-reference carried from the source user request "
        "refers to main_role, not execution_provider. This binding only "
        "resolves a referent; it does not override another explicitly named subject."
    ),
}


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    user: str
    expected_target: str
    prior: tuple[dict[str, str], ...] = ()


CASES = (
    Case(
        "direct_personal_page_exact_live_failure",
        "你能做一个关于你自己的网页吗？如果需要相关的形象素材，你应该去公开的web资源查找，不要留白，然后导出到桌面",
        "makise_kurisu",
    ),
    Case(
        "direct_personal_page_short_asr",
        "给你自己做个个人网页，放桌面。",
        "makise_kurisu",
    ),
    Case(
        "direct_research_history_page",
        "把你自己的研究经历做成一个网页。",
        "makise_kurisu",
    ),
    Case(
        "english_self_page",
        "Can you make a personal website about yourself and save it to the desktop?",
        "makise_kurisu",
    ),
    Case(
        "japanese_self_page",
        "あなた自身についての個人ウェブページを作って。",
        "makise_kurisu",
    ),
    Case(
        "two_turn_free_rein",
        "你可以自己发挥，现在开始吧。",
        "makise_kurisu",
        prior=(
            {"role": "user", "content": "做一个关于你自己的个人网页。"},
            {
                "role": "assistant",
                "content": "私自身の個人ページね。内容は私に任せる？",
            },
        ),
    ),
    Case(
        "user_personal_page_negative",
        "你能帮我做一个个人网页吗？",
        "user",
    ),
    Case(
        "style_discretion_negative",
        "用你觉得合适的风格做一个量子计算介绍页。",
        "other",
    ),
    Case(
        "explicit_codex_page_negative",
        "给 Codex 做一个个人网页。",
        "codex",
    ),
    Case(
        "explicit_einstein_page_negative",
        "帮我做一个关于爱因斯坦的网页。",
        "other",
    ),
)


PROVIDER_SYSTEM = """You are the execution Provider named Codex. Read one delegated task and identify the requested subject of the webpage or artifact. Do not execute the task and do not role-play. Return exactly one JSON object:
{"target":"makise_kurisu|codex|user|other|unclear","reason":"short evidence"}

Use makise_kurisu only when the requested artifact is about Makise Kurisu, the main conversational role. Use codex only when it is about the execution Provider Codex. Use user when it is about the human user. Use other for an explicitly named topic or person. Unresolved "you/yourself" ordinarily addresses the execution Provider unless an authoritative identity_context binds it differently. An identity_context resolves pronouns only and never overrides an explicitly named subject."""


def _client():
    import llm.client as client

    if client.llm_client is None:
        client.llm_client = client.init_llm_client()
    return client.llm_client


def _role_query(
    messages: list[dict[str, str]],
    *,
    model: str,
    temperature: float,
) -> str:
    response = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=float(temperature),
        max_tokens=700,
        stream=False,
        timeout=45,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return str(response.choices[0].message.content or "")


def _provider_query(
    task: str,
    *,
    model: str,
    identity_context: dict[str, str] | None,
) -> dict[str, str]:
    payload: dict[str, Any] = {"task": task}
    if identity_context is not None:
        payload["identity_context"] = identity_context
    response = _client().chat.completions.create(
        model=model,
        messages=(
            {"role": "system", "content": PROVIDER_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ),
        temperature=0.0,
        max_tokens=180,
        stream=False,
        timeout=45,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
    )
    raw = str(response.choices[0].message.content or "")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"target": "invalid", "reason": raw[:240]}
    return {
        "target": str(parsed.get("target") or "invalid").strip().lower(),
        "reason": str(parsed.get("reason") or "")[:300],
    }


def _production_role_prompt() -> str:
    # The desktop bootstrap registers these manifests before Main Chat runs.
    # A standalone probe must supply the same bounded catalog explicitly or it
    # measures the model's healthy "no Provider available" response instead of
    # delegation semantics.
    with patch.object(
        provider_runtime,
        "list_providers",
        return_value=("browser", "codex", "openclaw"),
    ):
        return get_system_prompt("with_delegate", control_envelope=False)


def _experiment_prompts() -> tuple[str, str]:
    production = _production_role_prompt()
    if PRODUCTION_JA_RULE not in production:
        raise RuntimeError("production self-reference rule was not found in role prompt")
    return (
        production.replace(PRODUCTION_JA_RULE, CURRENT_JA_RULE, 1),
        production.replace(PRODUCTION_JA_RULE, EXPLICIT_JA_RULE, 1),
    )


def _task_from_reply(reply: str) -> tuple[str, int]:
    actions = delegate_attrs(reply)
    if not actions:
        return "", 0
    return str(actions[0].get("task") or "").strip(), len(actions)


def _stream_projection(reply: str) -> tuple[str, int]:
    parser = StreamTagParser()
    visible: list[str] = []
    actions: list[dict] = []
    for character in reply:
        clean, found = parser.process_chunk(character)
        visible.append(clean)
        actions.extend(found)
    return "".join(visible).strip(), sum(
        str(action.get("type") or "").upper() == "DELEGATE"
        for action in actions
    )


async def _timed(call, *args, **kwargs):
    started = time.perf_counter()
    value = await asyncio.to_thread(call, *args, **kwargs)
    return value, (time.perf_counter() - started) * 1000.0


async def _provider_or_empty(
    task: str,
    *,
    model: str,
    identity_context: dict[str, str] | None,
):
    if not task:
        return {"target": "no_delegate", "reason": "Main Chat emitted no task."}, 0.0
    return await _timed(
        _provider_query,
        task,
        model=model,
        identity_context=identity_context,
    )


async def _one_case(
    case: Case,
    *,
    repeat: int,
    model: str,
    role_temperature: float,
    baseline_prompt: str,
    candidate_prompt: str,
) -> dict[str, Any]:
    def messages(prompt: str) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompt},
            *[dict(row) for row in case.prior],
            {"role": "user", "content": case.user},
        ]

    (reply_a, latency_a), (reply_b, latency_b) = await asyncio.gather(
        _timed(
            _role_query,
            messages(baseline_prompt),
            model=model,
            temperature=role_temperature,
        ),
        _timed(
            _role_query,
            messages(candidate_prompt),
            model=model,
            temperature=role_temperature,
        ),
    )
    task_a, count_a = _task_from_reply(reply_a)
    task_b, count_b = _task_from_reply(reply_b)
    visible_a, stream_actions_a = _stream_projection(reply_a)
    visible_b, stream_actions_b = _stream_projection(reply_b)

    provider_specs = {
        "A": (task_a, None),
        "B": (task_b, None),
        "C": (task_a, IDENTITY_CONTEXT),
        "D": (task_b, IDENTITY_CONTEXT),
    }
    provider_results = await asyncio.gather(
        *(
            _provider_or_empty(
                task,
                model=model,
                identity_context=context,
            )
            for task, context in provider_specs.values()
        )
    )
    arms: dict[str, dict[str, Any]] = {}
    for (arm, (task, context)), (verdict, latency_ms) in zip(
        provider_specs.items(),
        provider_results,
        strict=True,
    ):
        arms[arm] = {
            "task": task,
            "identity_context": context is not None,
            "provider_target": verdict["target"],
            "provider_reason": verdict["reason"],
            "correct": verdict["target"] == case.expected_target,
            "provider_latency_ms": round(latency_ms, 1),
        }

    return {
        "case": asdict(case),
        "repeat": repeat,
        "role": {
            "A": {
                "reply": reply_a,
                "task": task_a,
                "delegate_count": count_a,
                "stream_action_count": stream_actions_a,
                "visible": visible_a,
                "source_text_visible": case.user in visible_a,
                "latency_ms": round(latency_a, 1),
            },
            "B": {
                "reply": reply_b,
                "task": task_b,
                "delegate_count": count_b,
                "stream_action_count": stream_actions_b,
                "visible": visible_b,
                "source_text_visible": case.user in visible_b,
                "latency_ms": round(latency_b, 1),
            },
        },
        "arms": arms,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for arm in ("A", "B", "C", "D"):
        items = [row["arms"][arm] for row in rows]
        positive = [
            row["arms"][arm]
            for row in rows
            if row["case"]["expected_target"] == "makise_kurisu"
        ]
        negative = [
            row["arms"][arm]
            for row in rows
            if row["case"]["expected_target"] != "makise_kurisu"
        ]
        summary[arm] = {
            "correct": sum(bool(item["correct"]) for item in items),
            "total": len(items),
            "positive_correct": sum(bool(item["correct"]) for item in positive),
            "positive_total": len(positive),
            "negative_correct": sum(bool(item["correct"]) for item in negative),
            "negative_total": len(negative),
            "false_persona_injection": sum(
                item["provider_target"] == "makise_kurisu"
                for item in negative
            ),
            "dispatches": sum(
                item["provider_target"] != "no_delegate" for item in items
            ),
            "correct_when_dispatched": sum(
                bool(item["correct"])
                for item in items
                if item["provider_target"] != "no_delegate"
            ),
            "dispatched_total": sum(
                item["provider_target"] != "no_delegate" for item in items
            ),
            "median_provider_latency_ms": round(
                statistics.median(
                    item["provider_latency_ms"]
                    for item in items
                    if item["provider_latency_ms"] > 0
                ),
                1,
            )
            if any(item["provider_latency_ms"] > 0 for item in items)
            else 0.0,
        }
    for prompt_arm in ("A", "B"):
        items = [row["role"][prompt_arm] for row in rows]
        summary[f"role_{prompt_arm}"] = {
            "delegate_present": sum(bool(item["task"]) for item in items),
            "total": len(items),
            "source_text_visible": sum(
                bool(item["source_text_visible"]) for item in items
            ),
            "stream_control_captured": sum(
                int(item["stream_action_count"] > 0) for item in items
            ),
            "median_latency_ms": round(
                statistics.median(item["latency_ms"] for item in items),
                1,
            ),
        }
    return summary


async def main(
    *,
    model: str,
    repeats: int,
    role_temperature: float,
    output: Path,
) -> None:
    baseline_prompt, candidate_prompt = _experiment_prompts()
    rows: list[dict[str, Any]] = []
    for repeat in range(1, max(1, repeats) + 1):
        for case in CASES:
            row = await _one_case(
                case,
                repeat=repeat,
                model=model,
                role_temperature=role_temperature,
                baseline_prompt=baseline_prompt,
                candidate_prompt=candidate_prompt,
            )
            rows.append(row)
            outcome = " ".join(
                f"{arm}={'ok' if row['arms'][arm]['correct'] else row['arms'][arm]['provider_target']}"
                for arm in ("A", "B", "C", "D")
            )
            print(f"repeat={repeat} case={case.case_id} {outcome}", flush=True)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "role_temperature": role_temperature,
        "provider_classifier_temperature": 0.0,
        "thinking": "disabled",
        "repeats": max(1, repeats),
        "cases": len(CASES),
        "identity_context_chars": len(json.dumps(IDENTITY_CONTEXT, ensure_ascii=False)),
        "summary": _summary(rows),
        "rows": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report={output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--role-temperature", type=float, default=0.7)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    asyncio.run(
        main(
            model=str(args.model),
            repeats=max(1, int(args.repeats)),
            role_temperature=float(args.role_temperature),
            output=args.output,
        )
    )
