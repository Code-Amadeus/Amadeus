"""Canonical role prompt inherited by short-lived experience branches.

Branch-local observers and planners may have different fact contracts, but any
user-visible prose must inherit the same character and language authority as
the main chat.  Keeping this loader here prevents Browser, AUIP, and future
experience sources from copying or slowly diverging from the main prompt.
"""

from __future__ import annotations

import logging

from server.assistant_language import current_assistant_language


logger = logging.getLogger(__name__)


MAIN_CONVERSATION_ROLE_NAME = "Makise Kurisu (牧瀬紅莉栖)"


def inherited_main_role_prompt(variant: str = "base") -> str:
    """Return the current main-chat role prompt with its final language lock.

    ``base`` is appropriate for narrators that never own execution.  A branch
    planner that still needs the main chat's delegation vocabulary can request
    ``with_delegate`` explicitly.
    """

    try:
        from llm.prompts import finalize_system_prompt_language, get_system_prompt

        prompt = str(get_system_prompt(variant) or "").strip()
        if not prompt:
            raise RuntimeError("empty main role prompt")
        return finalize_system_prompt_language(prompt)
    except Exception:
        logger.exception("failed to load inherited main-chat role prompt")
        if current_assistant_language() == "japanese":
            return (
                "あなたは牧瀬紅莉栖。メインチャットと同じ人格と言語を保ち、"
                "自然かつ簡潔に日本語で答えてください。"
            )
        return (
            "You are Kurisu Makise. Keep the same language and personality as "
            "the main chat. Answer naturally and concisely."
        )
