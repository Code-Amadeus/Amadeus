"""C8 bounded Session archive fallback for explicit historical questions.

Session JSON remains the transcript authority and is read only after the normal
SQLite memory fast path reports low confidence.  A Session turn is eligible only
when it is anchored by at least one currently-active memory row and is not covered
by explicit-forget provenance.  This keeps archive recall from becoming a second
durable truth source or bypassing tombstones.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from core.continuity.models import ArchiveRetrievalHit, MemoryRecord, MemoryRetrievalHit
from core.continuity.retrieval_policy import ContinuityRetrievalPolicy
from core.continuity.store import ContinuityStore


_HISTORICAL_MARKERS = (
    "还记得", "记得我们", "之前", "以前", "上次", "那次", "当时", "过去", "曾经",
    "昨天", "前天", "上周", "上个月", "去年", "前年", "几天前", "几周前", "几个月前", "几年前",
    "我们聊过", "我告诉过", "我说过", "你说过", "当时说",
    "remember when", "do you remember", "last time", "previously", "before", "earlier",
    "yesterday", "the day before yesterday", "last week", "last month", "last year",
    "ago", "back then", "we talked about", "i told you", "i said", "you said",
)
_ASSISTANT_HISTORY_MARKERS = ("你说过", "你当时说", "你上次说", "what did you say", "you said", "your reply")


@dataclass(frozen=True, slots=True)
class TemporalHint:
    kind: str = "none"
    start_ts: float | None = None
    end_ts: float | None = None


@dataclass(frozen=True, slots=True)
class ArchiveRecallResult:
    hits: tuple[ArchiveRetrievalHit, ...] = ()
    session_candidates: int = 0
    sessions_opened: int = 0
    turns_scored: int = 0
    guard_ready: bool = True
    reason: str = ""
    temporal_kind: str = "none"


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _ngrams(value: str) -> set[str]:
    text = re.sub(r"\s+", "", _normalize(value))
    if not text:
        return set()
    if len(text) <= 3:
        return {text}
    return {text[i : i + 3] for i in range(len(text) - 2)}


def _coverage(query: str, text: str) -> float:
    q = _ngrams(query)
    t = _ngrams(text)
    if not q or not t:
        return 0.0
    return len(q & t) / max(1, min(len(q), 24))


def _parse_dt(value: str) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _year_range(now: datetime, year: int) -> TemporalHint:
    tz = now.tzinfo
    start = datetime(year, 1, 1, tzinfo=tz)
    end = datetime(year + 1, 1, 1, tzinfo=tz)
    return TemporalHint("year", start.timestamp(), end.timestamp())


def parse_temporal_hint(query: str, now: datetime) -> TemporalHint:
    text = _normalize(query)
    # ISO and common Chinese absolute dates.
    match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if not match:
        match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    if match:
        year, month, day = map(int, match.groups())
        try:
            start = datetime(year, month, day, tzinfo=now.tzinfo)
            return TemporalHint("date", start.timestamp(), (start + timedelta(days=1)).timestamp())
        except ValueError:
            pass
    year_only = re.search(r"\b(20\d{2})\b", text)
    if year_only:
        return _year_range(now, int(year_only.group(1)))
    if "前年" in text:
        return _year_range(now, now.year - 2)
    if "去年" in text or "last year" in text:
        return _year_range(now, now.year - 1)
    if "昨天" in text or "yesterday" in text:
        start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo) - timedelta(days=1)
        return TemporalHint("relative_day", start.timestamp(), (start + timedelta(days=1)).timestamp())
    if "前天" in text or "day before yesterday" in text:
        start = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo) - timedelta(days=2)
        return TemporalHint("relative_day", start.timestamp(), (start + timedelta(days=1)).timestamp())
    if "上周" in text or "last week" in text:
        today = datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return TemporalHint("relative_week", start.timestamp(), this_monday.timestamp())
    if "上个月" in text or "last month" in text:
        first = datetime(now.year, now.month, 1, tzinfo=now.tzinfo)
        previous_end = first
        previous_last = first - timedelta(days=1)
        start = datetime(previous_last.year, previous_last.month, 1, tzinfo=now.tzinfo)
        return TemporalHint("relative_month", start.timestamp(), previous_end.timestamp())
    ago = re.search(r"(\d{1,3})\s*(天|日|周|星期|个月|月|年)前", text)
    if ago:
        amount = max(1, int(ago.group(1)))
        unit = ago.group(2)
        days = amount
        if unit in {"周", "星期"}:
            days = amount * 7
        elif unit in {"个月", "月"}:
            days = amount * 30
        elif unit == "年":
            days = amount * 365
        center = now - timedelta(days=days)
        width = 1 if unit in {"天", "日"} else 7 if unit in {"周", "星期"} else 30 if unit in {"个月", "月"} else 90
        return TemporalHint("ago", (center - timedelta(days=width)).timestamp(), (center + timedelta(days=width)).timestamp())
    return TemporalHint()


def has_historical_cue(query: str) -> bool:
    text = _normalize(query)
    return any(marker in text for marker in _HISTORICAL_MARKERS)


class SessionArchiveSearcher:
    """Read-only, bounded Session JSON search gated behind low fast confidence."""

    def __init__(
        self,
        store: ContinuityStore,
        session_dir: str | Path | None,
        *,
        policy: ContinuityRetrievalPolicy | None = None,
    ) -> None:
        self.store = store
        self.session_dir = Path(session_dir).expanduser() if session_dir is not None else None
        self.policy = policy or ContinuityRetrievalPolicy()

    def evaluate_gate(self, query: str, fast_hits: Sequence[MemoryRetrievalHit]) -> tuple[bool, str]:
        if not self.policy.archive_recall_enabled:
            return False, "disabled"
        if not has_historical_cue(query):
            return False, "no_historical_cue"
        top = max((float(hit.score) for hit in fast_hits), default=0.0)
        if top >= self.policy.archive_fast_score_threshold:
            return False, "fast_confident"
        return True, "low_confidence_historical_query"

    @staticmethod
    def _safe_session_name(session_id: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", str(session_id or "default"))

    @staticmethod
    def _temporal_score(ts: float | None, hint: TemporalHint) -> float:
        if hint.start_ts is None or hint.end_ts is None:
            return 0.0
        if ts is None:
            return 0.1
        if hint.start_ts <= ts < hint.end_ts:
            return 1.0
        distance = min(abs(ts - hint.start_ts), abs(ts - hint.end_ts))
        # A small soft shoulder helps fuzzy "N months ago" phrasing but does
        # not beat an in-range candidate.
        return max(0.0, 0.35 - distance / (90.0 * 86400.0))

    def search(
        self,
        query: str,
        *,
        scope: str = "global",
        now: datetime,
        exclude_turn_id: str = "",
    ) -> ArchiveRecallResult:
        if self.session_dir is None or not self.session_dir.is_dir():
            return ArchiveRecallResult(reason="session_dir_missing")
        if not self.store.archive_forget_guard_ready():
            return ArchiveRecallResult(guard_ready=False, reason="legacy_tombstone_guard")

        hint = parse_temporal_hint(query, now)
        anchors = self.store.list_archive_source_anchors(
            scope=scope,
            now=float(now.timestamp()),
            exclude_turn_id=exclude_turn_id,
            limit=max(5000, self.policy.archive_max_source_turns * 8),
        )
        grouped: dict[tuple[str, str], list[MemoryRecord]] = defaultdict(list)
        for record in anchors:
            key = (record.source_session_id, record.source_turn_id)
            if self.store.is_archive_source_blocked(*key):
                continue
            grouped[key].append(record)
        if not grouped:
            return ArchiveRecallResult(reason="no_authority_anchors", temporal_kind=hint.kind)

        # Rank source turns before filesystem access. This keeps archive scans
        # bounded even when the user has many historical Sessions.
        ranked_sources: list[tuple[float, tuple[str, str], list[MemoryRecord]]] = []
        for key, records in grouped.items():
            anchor_text = " ".join(
                f"{r.memory_key} {r.summary} {r.object_text}" for r in records
            )
            lexical = _coverage(query, anchor_text)
            representative_ts = min(float(r.created_at) for r in records)
            temporal = self._temporal_score(representative_ts, hint)
            if hint.kind != "none" and temporal <= 0.0 and lexical <= 0.0:
                continue
            importance = max((float(r.importance) for r in records), default=0.0)
            rank = 0.62 * lexical + 0.25 * temporal + 0.13 * importance
            ranked_sources.append((rank, key, records))
        ranked_sources.sort(key=lambda item: (-item[0], item[1][0], item[1][1]))
        ranked_sources = ranked_sources[: self.policy.archive_max_source_turns]

        by_session: dict[str, set[str]] = defaultdict(set)
        source_records: dict[tuple[str, str], list[MemoryRecord]] = {}
        for _rank, (sid, tid), records in ranked_sources:
            by_session[sid].add(tid)
            source_records[(sid, tid)] = records
        session_ids = list(by_session)
        # Prefer Sessions whose anchors ranked highest.
        session_rank = {
            sid: max((rank for rank, (candidate_sid, _), _records in ranked_sources if candidate_sid == sid), default=0.0)
            for sid in session_ids
        }
        session_ids.sort(key=lambda sid: (-session_rank[sid], sid))
        session_ids = session_ids[: self.policy.archive_max_sessions]

        prefer_assistant = any(marker in _normalize(query) for marker in _ASSISTANT_HISTORY_MARKERS)
        hits: list[ArchiveRetrievalHit] = []
        opened = 0
        turns_scored = 0
        for sid in session_ids:
            path = self.session_dir / f"{self._safe_session_name(sid)}.json"
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(payload.get("session_id") or sid) != sid:
                # A filename collision or manually-edited archive must not be
                # trusted as provenance for another Session id.
                continue
            opened += 1
            turns: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: {"user": [], "assistant": []})
            for message in payload.get("dialog") or ():
                if not isinstance(message, dict):
                    continue
                tid = str(message.get("turn_id") or "").strip()
                role = str(message.get("role") or "").strip().lower()
                if not tid or tid not in by_session[sid] or role not in {"user", "assistant"}:
                    continue
                turns[tid][role].append(message)
            for tid, role_map in turns.items():
                if exclude_turn_id and tid == exclude_turn_id:
                    continue
                if self.store.is_archive_source_blocked(sid, tid):
                    continue
                turns_scored += 1
                users = role_map["user"]
                assistants = role_map["assistant"]
                user_text = "\n".join(str(m.get("content") or "").strip() for m in users if str(m.get("content") or "").strip())
                assistant_text = "\n".join(str(m.get("content") or "").strip() for m in assistants if str(m.get("content") or "").strip())
                if not user_text and not assistant_text:
                    continue
                user_score = _coverage(query, user_text)
                assistant_score = _coverage(query, assistant_text)
                lexical = max(user_score, assistant_score)
                records = source_records.get((sid, tid), [])
                anchor_text = " ".join(f"{r.summary} {r.object_text}" for r in records)
                anchor_score = _coverage(query, anchor_text)
                created_at = ""
                ts: float | None = None
                candidates = users + assistants
                for message in candidates:
                    value = str(message.get("created_at") or "").strip()
                    parsed = _parse_dt(value)
                    if parsed is not None:
                        created_at = value
                        ts = parsed
                        break
                temporal = self._temporal_score(ts, hint)
                if hint.kind != "none" and ts is not None and temporal <= 0.0:
                    continue
                score = 0.64 * lexical + 0.21 * anchor_score + 0.15 * temporal
                if score < self.policy.archive_min_score:
                    continue
                max_chars = self.policy.archive_max_excerpt_chars
                chosen_user = user_text[:max_chars].strip()
                chosen_assistant = ""
                if prefer_assistant or assistant_score > user_score + 0.08:
                    chosen_assistant = assistant_text[: min(max_chars, 560)].strip()
                hits.append(
                    ArchiveRetrievalHit(
                        session_id=sid,
                        turn_id=tid,
                        user_text=chosen_user,
                        assistant_text=chosen_assistant,
                        created_at=created_at,
                        score=min(1.0, max(0.0, score)),
                        lexical_score=min(1.0, max(0.0, lexical)),
                        temporal_score=min(1.0, max(0.0, temporal)),
                        anchor_memory_ids=tuple(dict.fromkeys(r.id for r in records)),
                    )
                )

        hits.sort(key=lambda hit: (-hit.score, -hit.temporal_score, hit.session_id, hit.turn_id))
        selected = tuple(hits[: self.policy.archive_max_hits])
        return ArchiveRecallResult(
            hits=selected,
            session_candidates=len(session_ids),
            sessions_opened=opened,
            turns_scored=turns_scored,
            guard_ready=True,
            reason="selected" if selected else "no_matching_session_turn",
            temporal_kind=hint.kind,
        )


__all__ = [
    "ArchiveRecallResult",
    "SessionArchiveSearcher",
    "TemporalHint",
    "has_historical_cue",
    "parse_temporal_hint",
]
