"""Canonical role prompt inherited by short-lived experience branches.

Branch-local observers and planners may have different fact contracts, but any
user-visible prose inherits the built-in character and current language.
The optional Main Chat character edit stays scoped to Main Chat. Keeping this
loader here prevents Browser, AUIP, and future experience sources from copying
or slowly diverging from the built-in role prompt.
"""

from __future__ import annotations

from llm.character_prompts import text

import logging

from server.assistant_language import current_assistant_language


logger = logging.getLogger(__name__)


MAIN_CONVERSATION_ROLE_NAME = text("display_name")


def inherited_main_role_prompt(variant: str = "base") -> str:
    """Return the built-in role prompt with its current final language lock.

    ``base`` is appropriate for narrators that never own execution.  A branch
    planner that still needs the main chat's delegation vocabulary can request
    ``with_delegate`` explicitly.
    """

    try:
        from llm.prompts import finalize_system_prompt_language, get_system_prompt

        prompt = str(get_system_prompt(variant, use_character_override=False) or "").strip()
        if not prompt:
            raise RuntimeError("empty main role prompt")
        return finalize_system_prompt_language(prompt)
    except Exception:
        logger.exception("failed to load inherited main-chat role prompt")
        if current_assistant_language() == "japanese":
            return text("inherited_fallback_ja")
        return text("inherited_fallback_en")
