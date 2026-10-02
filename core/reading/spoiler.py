"""Deterministic spoiler boundary enforcement."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .models import ReadingChunk, ReadingContext


Verifier = Callable[[str, Sequence[ReadingChunk]], bool]


@dataclass(frozen=True, slots=True)
class SpoilerDecision:
    allowed: bool
    reason: str = ""
    offending_chunk_ids: tuple[str, ...] = ()


class SpoilerGuard:
    """Keeps future chunks out of prompts and checks obvious response leakage."""

    def __init__(self, *, minimum_overlap: int = 12):
        self.minimum_overlap = max(4, int(minimum_overlap))

    def allowed_chunks(self, context: ReadingContext, chunks: Sequence[ReadingChunk]) -> list[ReadingChunk]:
        cursor = context.spoiler_cursor or context.cursor
        return [chunk for chunk in chunks if chunk.end_offset <= cursor]

    def forbidden_chunks(self, context: ReadingContext, chunks: Sequence[ReadingChunk]) -> list[ReadingChunk]:
        cursor = context.spoiler_cursor or context.cursor
        return [chunk for chunk in chunks if chunk.end_offset > cursor]

    def prepare_chunks(self, context: ReadingContext, chunks: Sequence[ReadingChunk]) -> list[ReadingChunk]:
        allowed = self.allowed_chunks(context, chunks)
        if not allowed:
            raise PermissionError("no reading chunks are inside the spoiler cursor")
        return allowed

    def check_response(
        self,
        response_text: str,
        context: ReadingContext,
        chunks: Sequence[ReadingChunk],
        *,
        verifier: Verifier | None = None,
    ) -> SpoilerDecision:
        forbidden = self.forbidden_chunks(context, chunks)
        if not forbidden:
            return SpoilerDecision(True)
        if verifier is not None:
            return SpoilerDecision(bool(verifier(response_text, forbidden)))
        normalized = self._normalize(response_text)
        offending: list[str] = []
        for chunk in forbidden:
            excerpt = self._normalize(chunk.text)
            if len(excerpt) < self.minimum_overlap:
                continue
            for start in range(0, len(excerpt) - self.minimum_overlap + 1, self.minimum_overlap):
                fragment = excerpt[start : start + self.minimum_overlap]
                if fragment and fragment in normalized:
                    offending.append(chunk.id)
                    break
        if offending:
            return SpoilerDecision(False, "response contains future chapter text", tuple(offending))
        return SpoilerDecision(True)

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(str(value or "").split())