"""Compose reading state and long-term memory for the existing chat context."""

from __future__ import annotations

from collections.abc import Sequence
import html
import re

from core.memory import MemoryContextProvider
from core.reading import ReadingChunk, ReadingSessionStore, SpoilerGuard


class CompanionContextBuilder:
    """Produces bounded context blocks without replacing the Host context builder."""

    def __init__(
        self,
        memory: MemoryContextProvider,
        reading: ReadingSessionStore,
        *,
        spoiler_guard: SpoilerGuard | None = None,
        recent_turn_limit: int = 6,
    ):
        self.memory = memory
        self.reading = reading
        self.spoiler_guard = spoiler_guard or SpoilerGuard()
        self.recent_turn_limit = max(0, min(int(recent_turn_limit), 20))

    def build(
        self,
        query: str,
        *,
        book_id: str | None = None,
        chunks: Sequence[ReadingChunk] = (),
        include_activity_memory: bool = True,
        prior_user_messages: Sequence[str] = (),
    ) -> str:
        blocks: list[str] = []
        reading_context = self.reading.get_context(book_id) if book_id else None
        if reading_context is not None:
            allowed = self.spoiler_guard.prepare_chunks(reading_context, chunks) if chunks else []
            blocks.append(self._render_reading(reading_context, allowed))
        scopes = ("user", "activity") if include_activity_memory else ("user",)
        namespace = reading_context.namespace if reading_context else None
        records = self.memory.recall(query, namespace=namespace, scopes=scopes)
        memory_block = self.memory.render(records)
        if memory_block:
            blocks.append(memory_block)
        repetition_block = self._repeat_context(query, prior_user_messages)
        if repetition_block:
            blocks.append(repetition_block)
        return "\n\n".join(blocks)

    @staticmethod
    def _normalize_question(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "")).strip().casefold()

    def _repeat_context(self, query: str, prior_user_messages: Sequence[str]) -> str:
        normalized = self._normalize_question(query)
        if len(normalized) < 2:
            return ""
        repeats = sum(
            1
            for message in prior_user_messages
            if self._normalize_question(message) == normalized
        )
        if repeats < 1:
            return ""
        return (
            "<repetition_context>\n"
            f"The user has already asked this exact question {repeats + 1} times in this conversation.\n"
            "Do not repeat the previous answer verbatim. Briefly acknowledge that it was asked before, "
            "then add new information, clarify what has changed, or ask what part is still unanswered. "
            "Do not claim to have forgotten the earlier exchange.\n"
            "</repetition_context>"
        )

    def remember_turn(
        self,
        book_id: str,
        *,
        user_message: str,
        assistant_message: str,
        selected_excerpt: str = "",
        referenced_chunks: Sequence[str] = (),
    ) -> None:
        self.reading.append_turn(
            book_id,
            user_message=user_message,
            assistant_message=assistant_message,
            selected_excerpt=selected_excerpt,
            referenced_chunks=referenced_chunks,
        )

    def _render_reading(self, context, chunks: Sequence[ReadingChunk]) -> str:
        lines = [
            "<reading_context>",
            f"book_id={html.escape(context.book_id)}",
            f"kind={html.escape(context.kind)}",
            f"chapter={html.escape(context.current_chapter)}",
            f"cursor={context.cursor}",
            f"spoiler_cursor={context.spoiler_cursor}",
        ]
        if context.current_page is not None:
            lines.append(f"page={context.current_page}")
        if chunks:
            lines.append("selected_chunks:")
            for chunk in chunks:
                lines.append(
                    f"- id={html.escape(chunk.id)} chapter={html.escape(chunk.chapter)} "
                    f"offset={chunk.start_offset}:{chunk.end_offset}\n"
                    f"{html.escape(chunk.text)}"
                )
        turns = self.reading.recent_turns(context.book_id, limit=self.recent_turn_limit)
        if turns:
            lines.append("recent_turns:")
            for turn in turns:
                lines.append(
                    f"- user: {html.escape(str(turn['user_message']))}\n"
                    f"  assistant: {html.escape(str(turn['assistant_message']))}"
                )
        lines.append("</reading_context>")
        return "\n".join(lines)