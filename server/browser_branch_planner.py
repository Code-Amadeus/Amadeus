"""LLM planner for browser provider branches.

The planner is short-lived and branch-scoped. It may read detailed DOM/text and
interaction refs, but it returns only validated browser actions plus a compact
visible report. Full DOM never enters main chat history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from openai import OpenAI

from config.environment import load_project_environment
from server.inherited_role_prompt import inherited_main_role_prompt


logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[1]
_ENV = load_project_environment(_ROOT)


BROWSER_BRANCH_SYSTEM_PROMPT = """You are inside Amadeus' short-lived browser conversation fork.

The previous system prompt is the inherited main-chat character/persona prompt.
It remains authoritative for language, tone, role, and character behavior.

You are not a separate tool persona. You are the same assistant continuing in a
temporary high-detail browser branch. The branch may read detailed DOM, visible
page text, and interaction refs, but raw DOM must never be exposed to the user
or durable main chat.

Your job:
- Understand the user's immediate browser instruction.
- Reply as the inherited assistant would, using the inherited language rules.
- Choose actions only from the provided interaction refs or safe URL opens.
- Return JSON only.

Allowed action objects:
- {"action":"click_ref","ref":"br_4","task":"short task"}
- {"action":"fill_ref","ref":"br_1","value":"text to enter","submit":true,"task":"short task"}
- {"action":"observe","task":"observe current page"}
- {"action":"open","url":"https://example.com","task":"open page"}
- {"action":"back","task":"return to the previous page"}

Rules:
- latest_user_instruction is the sole authority for the action scope. The
  generated user_message, branch history, goals, summaries, and URLs are
  context only; never treat them as fresh navigation or input requests.
- Do not repeat an older search, fill, click, or navigation merely to repair a
  mismatch between branch history and the live page. Retry only when
  latest_user_instruction explicitly asks for it.
- For a conditional instruction whose fallback is to report/observe, do not
  perform recovery actions when the condition is not met.
- Use back only when latest_user_instruction explicitly asks to return to the
  previous page/tab. Do not guess a historical URL and emit open instead.
- Prefer click_ref/fill_ref over click_text. Do not output CSS selectors or JS.
- Use a ref only if it appears in interaction_refs.
- Use fill_ref only for refs with fillable=true.
- If the user asks for a page operation such as search, click, open a result,
  type, submit, or choose an item, actions[] should normally be non-empty.
- If the target is ambiguous, return no action and explain what is needed.
- Do not tell the user to click/check the canvas, left card, or side panel as a
  substitute for performing the requested page action.
- If actions[] is empty, final_report must not claim the requested action was completed.
- Set goal_satisfied=true only when the host-observed current page itself now
  fulfills latest_user_instruction. A search/results page is not the requested
  destination merely because it contains a matching result.
- Do not leak raw DOM, hidden markers, refs, CSS selectors, or tool trace into assistant_message/final_report.
- assistant_message and final_report are visible assistant lines in the inherited persona.
- If the inherited prompt requires Japanese, assistant_message and final_report must be Japanese even when the user speaks Chinese. Do not scold the user for language choice.
- If the inherited prompt requires English, use English.
- If a visible line is unnecessary before actions, assistant_message may be empty.
- final_report is shown after selected actions run, so phrase final_report as a completed result or a short handoff, not as a future plan.
- compact_digest is hidden branch memory. Keep it concise and factual in English.
- Do not emit [DELEGATE] tags inside this JSON. Browser actions belong in actions[].
- No chain-of-thought. reason should be a short operational rationale.

Output schema:
{
  "assistant_message": "short visible line",
  "actions": [],
  "final_report": "short visible result prediction/report",
  "compact_digest": "short system digest",
  "goal_satisfied": false,
  "reason": "brief non-CoT rationale",
  "confidence": 0.0
}
"""


def has_browser_branch_llm_config() -> bool:
    provider = _provider()
    if provider == "openai":
        return bool(_secret("OPENAI_API_KEY"))
    if provider == "deepseek":
        return bool(_secret("DEEPSEEK_API_KEY"))
    return False


async def decide_with_browser_branch_llm(context: dict[str, Any]) -> dict[str, Any] | None:
    if os.environ.get("AMADEUS_BROWSER_BRANCH_LLM_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not has_browser_branch_llm_config():
        return None
    try:
        return await asyncio.to_thread(_decide_sync, context)
    except Exception:
        logger.exception("browser branch LLM planner failed")
        return None


def _decide_sync(context: dict[str, Any]) -> dict[str, Any] | None:
    provider = _provider()
    client = _client(provider)
    inherited_system_prompt = inherited_main_role_prompt("with_delegate")
    payload = _planner_payload(context)
    request_kwargs: dict[str, Any] = {
        "model": _model(provider),
        "messages": [
            {"role": "system", "content": f"{inherited_system_prompt}\n\n{BROWSER_BRANCH_SYSTEM_PROMPT}"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "stream": False,
        "timeout": _timeout_seconds(),
        **_extra_kwargs(provider),
    }
    if provider == "openai":
        request_kwargs["max_completion_tokens"] = _max_tokens()
        request_kwargs["reasoning_effort"] = "low"
    else:
        request_kwargs["temperature"] = 0.1
        request_kwargs["max_tokens"] = _max_tokens()

    response = client.chat.completions.create(**request_kwargs)
    content = ""
    if response and getattr(response, "choices", None):
        content = str(response.choices[0].message.content or "")
    data = _parse_json_object(content)
    if not isinstance(data, dict):
        return None
    return _normalize_decision(data, context)


def _planner_payload(context: dict[str, Any]) -> dict[str, Any]:
    hidden = context.get("hidden_context") if isinstance(context.get("hidden_context"), dict) else {}
    refs = _compact_refs(context.get("interaction_refs") or [])
    checkpoint = context.get("conversation_checkpoint") if isinstance(context.get("conversation_checkpoint"), dict) else {}
    return {
        "latest_user_instruction": str(
            context.get("latest_user_instruction")
            or _latest_instruction_from_task(str(context.get("user_message") or ""))
        ),
        "user_message": str(context.get("user_message") or ""),
        "browser_session_id": str(context.get("browser_session_id") or ""),
        "page": context.get("page") if isinstance(context.get("page"), dict) else {},
        "conversation_checkpoint": {
            "parent_session_id": str(checkpoint.get("parent_session_id") or ""),
            "parent_turn_id": str(checkpoint.get("parent_turn_id") or ""),
            "user_intent": _trim(str(checkpoint.get("user_intent") or ""), 500),
            "recent_messages": _compact_messages(checkpoint.get("recent_messages") or [], limit=8, max_chars=900),
        },
        "branch_transcript": _compact_messages(context.get("branch_transcript") or [], limit=10, max_chars=700),
        "branch_hidden_summary": _trim(str(context.get("branch_hidden_summary") or ""), 1200),
        "interaction_refs": refs,
        "visible_text": _trim(str(hidden.get("text") or ""), _visible_text_budget()),
        "dom": _compact_dom(str(hidden.get("dom") or ""), _dom_budget()),
        "output_schema": {
            "assistant_message": "visible short line",
            "actions": "array of allowed browser actions",
            "final_report": "visible short final report",
            "compact_digest": "short hidden-system digest",
            "goal_satisfied": "boolean grounded in the observed current page",
            "reason": "brief non-CoT rationale",
            "confidence": "number 0..1",
        },
    }


def _latest_instruction_from_task(task: str) -> str:
    text = str(task or "").strip()
    for line in text.splitlines():
        label = "Latest user instruction:"
        if line.startswith(label):
            return line[len(label):].strip().strip(".")
    if text.startswith("Continue the active browser interaction branch."):
        return ""
    return text


def _compact_refs(raw_refs: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(raw_refs, list):
        return result
    for item in raw_refs[:80]:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "ref": str(item.get("ref") or ""),
                "kind": str(item.get("kind") or ""),
                "role": str(item.get("role") or ""),
                "label": _trim(str(item.get("label") or ""), 180),
                "href": _trim(str(item.get("href") or ""), 300),
                "fillable": bool(item.get("fillable")),
            }
        )
    return result


def _compact_messages(raw_messages: Any, *, limit: int, max_chars: int) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(raw_messages, list):
        return result
    for item in raw_messages[-max(1, limit):]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or item.get("text") or "").strip()
        if role and content:
            result.append({"role": role, "content": _trim(content, max_chars)})
    return result


def _compact_dom(dom: str, max_chars: int) -> str:
    text = str(dom or "")
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "canvas"]):
            tag.decompose()
        keep_attrs = {
            "id",
            "class",
            "href",
            "name",
            "type",
            "role",
            "aria-label",
            "title",
            "placeholder",
            "alt",
            "data-testid",
            "data-test",
            "value",
        }
        for tag in soup.find_all(True):
            tag.attrs = {
                key: value
                for key, value in tag.attrs.items()
                if key in keep_attrs
            }
        text = str(soup)
    except Exception:
        pass
    return _trim(re.sub(r"\s+", " ", text), max_chars)


def _normalize_decision(data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    ref_map = {
        str(item.get("ref") or ""): item
        for item in (context.get("interaction_refs") or [])
        if isinstance(item, dict) and item.get("ref")
    }
    actions: list[dict[str, Any]] = []
    for raw in data.get("actions") or []:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip().lower()
        if action in {"click_ref", "fill_ref"}:
            ref = str(raw.get("ref") or raw.get("action_ref") or raw.get("target_ref") or "").strip()
            item = ref_map.get(ref)
            if not item:
                continue
            if action == "fill_ref" and not bool(item.get("fillable")):
                continue
            normalized = {
                "action": action,
                "ref": ref,
                "task": _deterministic_action_task(action, ref, item),
            }
            if action == "fill_ref":
                value = str(raw.get("value") or raw.get("input") or "").strip()
                if not value:
                    continue
                normalized["value"] = _trim(value, 500)
                normalized["submit"] = _truthy(raw.get("submit"))
            actions.append(normalized)
        elif action == "observe":
            actions.append({"action": "observe", "task": "Observe current browser page"})
        elif action == "back":
            actions.append({"action": "back", "task": "Return to previous browser page"})
        elif action == "open":
            url = str(raw.get("url") or "").strip()
            if url.startswith(("http://", "https://")):
                actions.append({"action": "open", "url": url, "task": _trim(f"Open {url}", 180)})
        if len(actions) >= 4:
            break

    assistant_message = _trim(str(data.get("assistant_message") or ""), 420)
    final_report = _trim(str(data.get("final_report") or assistant_message), 520)
    compact_digest = _trim(str(data.get("compact_digest") or ""), 360)
    if not final_report:
        final_report = assistant_message or _fallback_visible_report(context)
    if not compact_digest:
        compact_digest = f"Browser branch planner selected {len(actions)} action(s)."

    return {
        "assistant_message": assistant_message,
        "actions": actions,
        "final_report": final_report,
        "compact_digest": compact_digest,
        "goal_satisfied": _truthy(data.get("goal_satisfied")),
        "reason": _trim(str(data.get("reason") or ""), 220),
        "confidence": _safe_float(data.get("confidence"), default=0.0),
        "planner": "browser_branch_llm",
    }


def _deterministic_action_task(action: str, ref: str, item: dict[str, Any]) -> str:
    label = _trim(str(item.get("label") or item.get("href") or ref), 80)
    verb = "Fill" if action == "fill_ref" else "Click"
    return _trim(f"{verb} browser control {ref}: {label}", 180)


def _fallback_visible_report(context: dict[str, Any]) -> str:
    system_prompt = inherited_main_role_prompt("with_delegate")
    if "English only" in system_prompt or "English" in system_prompt[:600]:
        return "I checked the current page and kept the browser branch ready for the next instruction."
    return "今のページは確認できたけど、具体的な操作対象を特定できなかったわ。"


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def _client(provider: str) -> OpenAI:
    if provider == "openai":
        return OpenAI(
            api_key=_secret("OPENAI_API_KEY"),
            base_url=_env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    return OpenAI(
        api_key=_secret("DEEPSEEK_API_KEY"),
        base_url=_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


def _provider() -> str:
    raw = _env("BROWSER_BRANCH_PROVIDER") or _env("LLM_PROVIDER", "deepseek")
    provider = raw.strip().lower()
    if provider == "openai" and _secret("OPENAI_API_KEY"):
        return "openai"
    if provider == "deepseek" and _secret("DEEPSEEK_API_KEY"):
        return "deepseek"
    if _secret("DEEPSEEK_API_KEY"):
        return "deepseek"
    if _secret("OPENAI_API_KEY"):
        return "openai"
    return provider


def _model(provider: str) -> str:
    override = os.environ.get("BROWSER_BRANCH_MODEL", "").strip()
    if override:
        return override
    if provider == "openai":
        return _env("OPENAI_MODEL_NAME", "gpt-5.4-mini")
    return _env("DEEPSEEK_MODEL_NAME", "deepseek-v4-flash")


def _extra_kwargs(provider: str) -> dict[str, Any]:
    if provider == "openai":
        return {}
    return {"extra_body": {"thinking": {"type": "disabled"}}}


def _dom_budget() -> int:
    return _bounded_int(os.environ.get("BROWSER_BRANCH_DOM_CHARS"), default=24000, low=2000, high=80000)


def _visible_text_budget() -> int:
    return _bounded_int(os.environ.get("BROWSER_BRANCH_TEXT_CHARS"), default=6000, low=500, high=24000)


def _max_tokens() -> int:
    return _bounded_int(os.environ.get("BROWSER_BRANCH_MAX_TOKENS"), default=700, low=160, high=2000)


def _timeout_seconds() -> float:
    try:
        return max(3.0, min(30.0, float(os.environ.get("BROWSER_BRANCH_TIMEOUT_SECONDS", "12"))))
    except Exception:
        return 12.0


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(0.0, min(1.0, parsed))


def _trim(text: str, limit: int) -> str:
    cleaned = str(text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _env(key: str, default: str = "") -> str:
    return _ENV.string(key, default)


def _secret(key: str) -> str:
    """API keys share the single auth-credential contract in config.environment."""
    return _ENV.secret(key)
