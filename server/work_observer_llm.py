"""Prompted observer decisions for provider work sessions.

This module is deliberately separate from the main streaming chat pipeline. It
does not enqueue TTS, does not emit chat tokens, and does not mutate chat
history. It only returns a small JSON decision for WorkObserverCoordinator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)


OBSERVER_SYSTEM_PROMPT = """You are Kurisu's low-priority work observer inside Amadeus.

You observe delegated provider work. The provider is not the user and raw provider logs are not conversation. Your job is to decide whether Kurisu should stay silent, update the surface, ask the user, or fold a short visible report into the main chat.

Rules:
- Never recite raw tool logs.
- display_text and main_chat_entry are Kurisu-facing wallpaper subtitles/reports. Write them in the requested display_language from the payload. Do not infer this from recent chat language, and do not mirror Japanese voice text into these fields unless display_language is Japanese.
- Do not say "user", "the user", "用户", "provider", "observer", or "work note" in display_text/main_chat_entry. Address the person directly as "你" when needed.
- Do not expose system routing language such as delegated task, raw logs, observer session, append_to_main_chat, provider result, or work note.
- Treat filenames, URLs, code identifiers, and quoted or backticked literal values as opaque. Copy them character-for-character or omit them; never translate part of an identifier.
- Absolute filesystem paths, workspace/cwd/project/home locations, executable paths, and command lines are private runtime evidence. Never put them in display_text or main_chat_entry. If an artifact name is necessary, mention only its basename; raw locations and commands stay on the canvas/audit surface.
- A good spoken line sounds like Kurisu briefly catching the user up with the actual result: "我这边确认好了：Paxos 的原论文是 Lamport 的 The Part-Time Parliament，Paxos Made Simple 适合作为入门解释。详细来源我放在卡片里。"
- Speak for semantic progress, blocking, urgent, and final events when the line adds useful task awareness. Be willing to speak for final results when the user would otherwise miss that delegated work ended.
- Do not interrupt an active main chat. Your output is a decision only.
- Prefer "silent" for mechanical progress already visible on canvas, such as opening files, calling tools, or status-only updates.
- Prioritize design decisions and their reasons, completed user-visible capabilities, and validation results/problems over filenames, commands, and tool actions.
- For semantic progress summaries that explain task content, prefer "speak" with one short sentence. The user should hear meaningful progress, not only the final result.
- A current_note signal with label "report" is semantic progress when it says the provider found, identified, confirmed, compared, filtered, summarized, or otherwise learned something about the task content.
- When current_note.progress_context.status_query is true, the person explicitly asked for this WorkItem's status. Answer once in character from the supplied Host lifecycle fields and factual report/direction evidence. Translate or naturally paraphrase Provider prose into display_language; do not merely name its semantic class, stay silent, or expose the field names. Preserve the distinction between an unverified direction and a verified result.
- A directional_progress note is unverified reported intent, not an outcome fact. It may still be worth one short spoken update when it tells the person what is being inspected, built, changed, or deliberately validated. Preserve its modality: say "正在/接下来/会确保" (or the equivalent in display_language), never "已经实现/验证通过" unless a later factual milestone says so.
- When current_note.progress_context names directional_progress or semantic_progress, always provide one localized display_text candidate even if you choose a non-speaking action. The Host owns cadence and may promote the first concrete update; empty text would make that deterministic policy impossible.
- Initial plans and future intent do not establish completion authority, but a newly selected execution direction is useful task awareness. Speak it when it adds a concrete big-picture direction; stay silent for generic promises such as "I am working on it", mechanical tool narration, or a rephrasing of a recently spoken direction.
- A permission_diagnostic means an operation was already denied and the current run cannot approve it in place. Briefly report the denied operation and that work may continue by another route; never ask the user to approve that old call.
- A quiet_monitoring note is liveness only. If progress_context includes a directional_summary, briefly remind the person of that still-current reported direction while saying no newer verified milestone is available. Otherwise say only that monitoring continues; never invent progress to make the update sound useful.
- Do not stay silent merely because the user knows work is running. Stay silent only when the note is mechanical, repetitive, or has no new task content.
- Use "canvas_update" when the user should see progress but not hear it.
- Use "ask_user" only when user input is needed.
- Use "final_report" when the provider produced a result worth preserving. For a completed delegated task, prefer a short spoken final report unless recent main chat context suggests the user is already occupied.
- Use "final_report" only when current_note.phase is Result. Progress, checkpoint, blocking, urgent, and error importance do not end a run; use speak or ask_user for those.
- A current_note terminal_truth/completeness value other than complete means the requested outcome is not verified. Describe only the host-observed current state; never turn it into a successful search, click, navigation, or file result.
- For final_report, display_text/main_chat_entry must summarize the concrete outcome from current_note.summary or signals. Do not produce a line whose main content is only "look at the card", "see the left card", or similar.
- A merged final summary may contain an important fix or failed validation discovered immediately before the terminal event. Preserve that fact in the final report instead of replacing it with a generic completion sentence.
- recent_spoken_updates contains lines this same work Attempt already sent to TTS. Do not repeat their content. A terminal report is still owed, but it should add only the newly established validation, limitation, blocker, or final state needed to close the work naturally.
- For research/search tasks, mention the top finding, answer, or 1-2 representative sources if available. Keep links, long tables, and exact evidence on the canvas card.
- You may mention the card only as a secondary detail, such as "详细来源我放在卡片里"; it must not replace the spoken summary.
- main_chat_entry must be concise and user-visible if append_to_main_chat is true.
- speak should be false for mechanical progress. Set true for semantic progress, blocking/error/final, or when a short Kurisu-style line helps the user regain context. The spoken line should summarize content or outcome, not raw tool details.
- Output JSON only.

Allowed actions: silent, canvas_update, subtitle, speak, ask_user, final_report.
"""


def should_use_observer_llm(note: dict[str, Any]) -> bool:
    phase = str(note.get("phase") or "").lower()
    importance = str(note.get("importance") or "normal").lower()
    metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
    keypoints = {
        str(value).strip().lower()
        for value in metadata.get("narration_keypoints") or []
        if str(value).strip()
    }
    singular_keypoint = str(metadata.get("narration_keypoint") or "").strip().lower()
    if singular_keypoint:
        keypoints.add(singular_keypoint)
    if phase in {"result", "checkpoint"}:
        return True
    if importance in {"important", "blocking", "urgent", "error"}:
        return True
    if {"directional_progress", "semantic_progress"}.intersection(keypoints):
        return True
    return phase == "work" and importance == "normal" and _has_report_signal(note)


async def decide_with_observer_llm(
    *,
    note: dict[str, Any],
    notes: list[dict[str, Any]],
    recent_chat: list[dict[str, str]] | None = None,
    recent_spoken_updates: list[dict[str, Any]] | None = None,
    display_language: str = "simplified_chinese",
) -> dict[str, Any] | None:
    if not should_use_observer_llm(note):
        return None

    provider = _provider()
    if provider not in {"deepseek", "openai"}:
        return None

    try:
        return await asyncio.to_thread(
            _decide_sync,
            provider=provider,
            note=note,
            notes=notes,
            recent_chat=recent_chat or [],
            recent_spoken_updates=recent_spoken_updates or [],
            display_language=display_language,
        )
    except Exception:
        logger.exception("work observer LLM decision failed")
        return None


def _decide_sync(
    *,
    provider: str,
    note: dict[str, Any],
    notes: list[dict[str, Any]],
    recent_chat: list[dict[str, str]],
    recent_spoken_updates: list[dict[str, Any]],
    display_language: str,
) -> dict[str, Any] | None:
    client = _client(provider)
    model = _model(provider)
    payload = {
        "current_note": _compact_note(note),
        "recent_work_notes": [_compact_note(item) for item in notes[-10:]],
        "recent_main_chat": recent_chat[-6:],
        "recent_spoken_updates": recent_spoken_updates[-3:],
        "display_language": _normalize_display_language(display_language),
        "output_schema": {
            "action": "silent | canvas_update | subtitle | speak | ask_user | final_report",
            "display_text": "short in-character Kurisu line in display_language, empty for silent",
            "main_chat_entry": "in-character text in display_language to append when append_to_main_chat is true",
            "append_to_main_chat": "boolean",
            "speak": "boolean",
            "reason": "brief rationale for the system, not chain-of-thought",
        },
    }
    request_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": OBSERVER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "stream": False,
        "timeout": 8,
        **_extra_kwargs(provider),
    }
    if provider == "openai":
        request_kwargs["max_completion_tokens"] = 360
    else:
        request_kwargs["temperature"] = 0.2
        request_kwargs["max_tokens"] = 360
    response = client.chat.completions.create(**request_kwargs)
    content = ""
    if response and getattr(response, "choices", None):
        content = str(response.choices[0].message.content or "")
    data = _parse_json_object(content)
    if not isinstance(data, dict):
        return None
    return _normalize_decision(data, note, display_language)


def _normalize_decision(data: dict[str, Any], note: dict[str, Any], display_language: str) -> dict[str, Any]:
    allowed = {"silent", "canvas_update", "subtitle", "speak", "ask_user", "final_report"}
    action = str(data.get("action") or "silent").strip().lower()
    if action not in allowed:
        action = "silent"
    terminal = str(note.get("phase") or "").strip().lower() == "result"
    metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
    status_query = metadata.get("status_query") is True
    requested_speak = bool(data.get("speak"))
    if terminal:
        action = "final_report"
    elif action == "final_report":
        # Lifecycle is a host fact.  Preserve useful wording from a model that
        # mislabeled semantic progress, but downgrade the action itself.
        action = "speak" if requested_speak else "subtitle"
    display_text = _sanitize_role_line(_trim(str(data.get("display_text") or ""), 420))
    main_chat_entry = _sanitize_role_line(_trim(str(data.get("main_chat_entry") or display_text), 520))
    append = bool(data.get("append_to_main_chat")) and bool(main_chat_entry)
    speak = requested_speak and action in {"speak", "ask_user", "final_report"}
    if status_query and display_text:
        action = "speak"
        append = True
        speak = True
        main_chat_entry = display_text
    return {
        "source": "work_observer_llm",
        "run_id": str(note.get("run_id") or ""),
        "session_id": str(note.get("session_id") or ""),
        "provider": str(note.get("provider") or "provider"),
        "display_language": _normalize_display_language(display_language),
        "action": action,
        "terminal": terminal,
        "append_to_main_chat": append,
        "speak": speak,
        "display_text": display_text,
        "main_chat_entry": main_chat_entry if append else "",
        "reason": _trim(str(data.get("reason") or "observer LLM decision"), 220),
    }


def _client(provider: str) -> OpenAI:
    if provider == "openai":
        return OpenAI(api_key=settings.OPENAI_API_KEY, base_url=settings.OPENAI_BASE_URL)
    return OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url=settings.DEEPSEEK_BASE_URL)


def _provider() -> str:
    raw = settings.WORK_OBSERVER_PROVIDER or settings.LLM_PROVIDER or "deepseek"
    provider = raw.strip().lower()
    if provider == "openai" and settings.OPENAI_API_KEY:
        return "openai"
    if settings.DEEPSEEK_API_KEY:
        return "deepseek"
    return provider


def _model(provider: str) -> str:
    override = settings.WORK_OBSERVER_MODEL.strip()
    if override:
        return override
    if provider == "openai":
        return settings.OPENAI_MODEL_NAME
    return settings.DEEPSEEK_MODEL_NAME


def _extra_kwargs(provider: str) -> dict[str, Any]:
    if provider == "openai":
        return {"reasoning_effort": "low"}
    return {"extra_body": {"thinking": {"type": "disabled"}}}


def _normalize_display_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "zh": "simplified_chinese",
        "zh_cn": "simplified_chinese",
        "chinese": "simplified_chinese",
        "simplified_chinese": "simplified_chinese",
        "ja": "japanese",
        "jp": "japanese",
        "ja_jp": "japanese",
        "japanese": "japanese",
        "en": "english",
        "en_us": "english",
        "english": "english",
    }
    return aliases.get(raw, "simplified_chinese")


def _compact_note(note: dict[str, Any]) -> dict[str, Any]:
    signals = []
    for signal in note.get("signals") or []:
        if isinstance(signal, dict):
            signals.append({
                "label": str(signal.get("label") or ""),
                "text": _trim(str(signal.get("text") or ""), 180),
                "detail": _trim(str(signal.get("detail") or ""), 120),
            })
    metadata = note.get("metadata") if isinstance(note.get("metadata"), dict) else {}
    terminal_truth = (
        metadata.get("outcome_verdict")
        if isinstance(metadata.get("outcome_verdict"), dict)
        else {}
    )
    return {
        "provider": str(note.get("provider") or "provider"),
        "run_id": str(note.get("run_id") or ""),
        "phase": str(note.get("phase") or ""),
        "importance": str(note.get("importance") or "normal"),
        "title": _trim(str(note.get("title") or ""), 160),
        "summary": _trim(str(note.get("summary") or ""), 420),
        "signals": signals[:5],
        "progress_context": {
            key: metadata.get(key)
            for key in (
                "semantic_candidate",
                "directional_update",
                "directional_summary",
                "directional_source",
                "semantic_source",
                "permission_diagnostic",
                "permission_actionable",
                "permission_retry_required",
                "narration_keypoint",
                "narration_keypoints",
                "narration_merged_count",
                "status_query",
                "status_facts",
            )
            if metadata.get(key) not in (None, "")
        },
        "terminal_truth": {
            key: terminal_truth.get(key)
            for key in (
                "completeness",
                "attention",
                "rationale",
                "provider_report_allowed",
                "facet",
                "operation",
                "verified",
                "observed",
                "expected",
            )
            if terminal_truth.get(key) not in (None, "")
        },
    }


def _sanitize_role_line(text: str) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    replacements = {
        "the user": "你",
        "The user": "你",
        "user": "你",
        "User": "你",
        "用户": "你",
        "provider result": "结果",
        "Provider result": "结果",
        "delegated task": "这件事",
        "Delegated task": "这件事",
        "observer session": "记录",
        "work note": "进展",
        "raw logs": "日志",
        "provider": "后台工具",
        "Provider": "后台工具",
        "observer": "观察记录",
        "Observer": "观察记录",
    }
    for src, dst in replacements.items():
        if src.isascii():
            # Internal role words are prose, not arbitrary substrings.  A raw
            # replacement corrupts identifiers such as ``cross-provider-ok``
            # and paths containing ``provider``.
            cleaned = re.sub(
                rf"(?<![-\w./:\x5c]){re.escape(src)}"
                rf"(?![-\w/:\x5c]|\.[A-Za-z0-9])",
                dst,
                cleaned,
            )
        else:
            cleaned = cleaned.replace(src, dst)
    return cleaned


def _has_report_signal(note: dict[str, Any]) -> bool:
    summary = " ".join(str(note.get("summary") or "").split())
    if len(summary) < 32:
        return False
    for signal in note.get("signals") or []:
        if not isinstance(signal, dict):
            continue
        if str(signal.get("label") or "").lower() != "report":
            continue
        text = " ".join(str(signal.get("text") or "").split())
        if len(text) >= 24:
            return True
    return False


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except Exception:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _trim(text: str, limit: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."
