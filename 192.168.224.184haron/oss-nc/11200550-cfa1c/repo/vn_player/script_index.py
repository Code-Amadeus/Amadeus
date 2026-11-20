"""Complete-script indexing and live-line matching."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .text import normalize_for_match, rough_similarity, text_hash


@dataclass
class ScriptLine:
    script_id: str
    text: str
    order: int
    chapter_id: str = ""
    scene_id: str = ""
    speaker: str = ""
    language: str = ""
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "script_id": self.script_id,
            "chapter_id": self.chapter_id,
            "scene_id": self.scene_id,
            "speaker": self.speaker,
            "text": self.text,
            "normalized_text": normalize_for_match(self.text),
            "language": self.language,
            "order": self.order,
            "source_file": self.source_file,
        }


class ScriptIndex:
    def __init__(self, lines: list[ScriptLine] | None = None) -> None:
        self.lines = list(lines or [])
        self._by_id = {line.script_id: line for line in self.lines if line.script_id}
        self._by_hash: dict[str, list[ScriptLine]] = {}
        for line in self.lines:
            self._by_hash.setdefault(text_hash(line.text), []).append(line)

    @classmethod
    def empty(cls) -> "ScriptIndex":
        return cls([])

    @classmethod
    def from_file(cls, path: str | Path, *, language: str = "") -> "ScriptIndex":
        p = Path(path)
        if not p.is_file():
            return cls.empty()
        if p.suffix.lower() == ".json":
            return cls._from_json(p, language=language)
        return cls._from_csv_like(p, language=language)

    @classmethod
    def _from_json(cls, path: Path, *, language: str = "") -> "ScriptIndex":
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_lines = data.get("lines") if isinstance(data, dict) else data
        lines: list[ScriptLine] = []
        for i, item in enumerate(raw_lines or []):
            if not isinstance(item, dict):
                continue
            script_id = str(item.get("script_id") or item.get("id") or f"line_{i:06d}")
            text = str(item.get("text") or "")
            if not text:
                continue
            lines.append(
                ScriptLine(
                    script_id=script_id,
                    text=text,
                    order=int(item.get("order") or i),
                    chapter_id=str(item.get("chapter_id") or ""),
                    scene_id=str(item.get("scene_id") or ""),
                    speaker=str(item.get("speaker") or ""),
                    language=str(item.get("language") or language),
                    source_file=str(item.get("source_file") or path.name),
                )
            )
        return cls(lines)

    @classmethod
    def _from_csv_like(cls, path: Path, *, language: str = "") -> "ScriptIndex":
        # Paranormasight text files are simple "id,text" rows. csv handles
        # commas inside quoted strings if future extracts contain them.
        lines: list[ScriptLine] = []
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if not row:
                    continue
                if len(row) == 1:
                    script_id = f"line_{i:06d}"
                    text = row[0]
                else:
                    script_id = str(row[0]).strip() or f"line_{i:06d}"
                    text = ",".join(row[1:]).strip()
                if not text:
                    continue
                chapter_id = "_".join(script_id.split("_")[:2]) if "_" in script_id else ""
                lines.append(
                    ScriptLine(
                        script_id=script_id,
                        text=text,
                        order=i,
                        chapter_id=chapter_id,
                        language=language,
                        source_file=path.name,
                    )
                )
        return cls(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_count": len(self.lines),
            "lines": [line.to_dict() for line in self.lines],
        }

    def match(self, text: str, *, after_order: int | None = None, window: int = 200) -> dict[str, Any] | None:
        if not self.lines:
            return None
        normalized_hash = text_hash(text)
        candidates = self._by_hash.get(normalized_hash) or []
        if after_order is not None and candidates:
            after_candidates = [line for line in candidates if line.order >= after_order]
            if after_candidates:
                candidates = after_candidates
        if candidates:
            line = min(candidates, key=lambda item: abs(item.order - (after_order or item.order)))
            return {"match_type": "hash", "score": 1.0, "line": line.to_dict()}

        search_lines = self.lines
        if after_order is not None:
            low = max(0, after_order - 5)
            high = min(len(self.lines), after_order + max(window, 20))
            search_lines = self.lines[low:high]
        best_line = None
        best_score = 0.0
        for line in search_lines:
            score = rough_similarity(text, line.text)
            if score > best_score:
                best_score = score
                best_line = line
        if best_line and best_score >= 0.72:
            return {"match_type": "fuzzy", "score": round(best_score, 3), "line": best_line.to_dict()}
        if after_order is not None and best_line and best_score >= 0.3:
            # Once live playback has a reliable anchor, nearby lines can be
            # semantically equivalent across localization revisions while using
            # different wording. Keep this fallback local to the forward window
            # so it gives lookahead an id without pretending to be exact text.
            normalized = normalize_for_match(text)
            if len(normalized) >= 6 or best_score >= 0.45:
                return {"match_type": "anchored_fuzzy", "score": round(best_score, 3), "line": best_line.to_dict()}
        return None

    def window_after(self, script_id: str, *, count: int = 50) -> list[dict[str, Any]]:
        line = self._by_id.get(script_id)
        if line is None:
            return []
        start = line.order + 1
        end = min(len(self.lines), start + max(0, count))
        return [item.to_dict() for item in self.lines[start:end]]
