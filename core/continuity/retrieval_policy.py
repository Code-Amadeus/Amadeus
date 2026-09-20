"""C3 retrieval policy loading.

Weights live outside environment variables so retrieval tuning stays a single,
reviewable policy rather than becoming dozens of process-level switches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ContinuityRetrievalPolicy:
    max_items: int = 5
    max_open_loops: int = 2
    max_context_chars: int = 2400
    candidate_limit: int = 24
    structured_weight: float = 0.34
    lexical_weight: float = 0.26
    semantic_weight: float = 0.30
    importance_weight: float = 0.06
    recency_weight: float = 0.04
    mmr_lambda: float = 0.82
    minimum_score: float = 0.18
    semantic_model: str = "intfloat/multilingual-e5-small"
    semantic_candidate_limit: int = 24
    retention_half_life_days: float = 30.0
    retention_cold_after_days: float = 45.0
    retention_archive_after_days: float = 180.0
    retention_maintenance_interval_hours: float = 24.0
    retention_max_hot_memories: int = 5000
    archive_recall_enabled: bool = True
    archive_fast_score_threshold: float = 0.38
    archive_max_sessions: int = 32
    archive_max_source_turns: int = 120
    archive_max_hits: int = 3
    archive_max_excerpt_chars: int = 720
    archive_min_score: float = 0.12

    @classmethod
    def load(cls, path: str | Path | None) -> "ContinuityRetrievalPolicy":
        if path is None:
            return cls()
        policy_path = Path(path)
        if not policy_path.is_file():
            return cls()
        try:
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid continuity policy JSON: {policy_path}") from exc
        retrieval = raw.get("retrieval") if isinstance(raw, dict) else {}
        semantic = raw.get("semantic") if isinstance(raw, dict) else {}
        retention = raw.get("retention") if isinstance(raw, dict) else {}
        archive = raw.get("archive_recall") if isinstance(raw, dict) else {}
        if not isinstance(retrieval, dict):
            retrieval = {}
        if not isinstance(semantic, dict):
            semantic = {}
        if not isinstance(retention, dict):
            retention = {}
        if not isinstance(archive, dict):
            archive = {}
        defaults = cls()
        result = cls(
            max_items=int(retrieval.get("max_items", defaults.max_items)),
            max_open_loops=int(retrieval.get("max_open_loops", defaults.max_open_loops)),
            max_context_chars=int(retrieval.get("max_context_chars", defaults.max_context_chars)),
            candidate_limit=int(retrieval.get("candidate_limit", defaults.candidate_limit)),
            structured_weight=float(retrieval.get("structured_weight", defaults.structured_weight)),
            lexical_weight=float(retrieval.get("lexical_weight", defaults.lexical_weight)),
            semantic_weight=float(retrieval.get("semantic_weight", defaults.semantic_weight)),
            importance_weight=float(retrieval.get("importance_weight", defaults.importance_weight)),
            recency_weight=float(retrieval.get("recency_weight", defaults.recency_weight)),
            mmr_lambda=float(retrieval.get("mmr_lambda", defaults.mmr_lambda)),
            minimum_score=float(retrieval.get("minimum_score", defaults.minimum_score)),
            semantic_model=str(semantic.get("model", defaults.semantic_model) or defaults.semantic_model),
            semantic_candidate_limit=int(
                semantic.get("candidate_limit", defaults.semantic_candidate_limit)
            ),
            retention_half_life_days=float(retention.get("half_life_days", defaults.retention_half_life_days)),
            retention_cold_after_days=float(retention.get("cold_after_days", defaults.retention_cold_after_days)),
            retention_archive_after_days=float(retention.get("archive_after_days", defaults.retention_archive_after_days)),
            retention_maintenance_interval_hours=float(retention.get("maintenance_interval_hours", defaults.retention_maintenance_interval_hours)),
            retention_max_hot_memories=int(retention.get("max_hot_memories", defaults.retention_max_hot_memories)),
            archive_recall_enabled=bool(archive.get("enabled", defaults.archive_recall_enabled)),
            archive_fast_score_threshold=float(archive.get("fast_score_threshold", defaults.archive_fast_score_threshold)),
            archive_max_sessions=int(archive.get("max_sessions", defaults.archive_max_sessions)),
            archive_max_source_turns=int(archive.get("max_source_turns", defaults.archive_max_source_turns)),
            archive_max_hits=int(archive.get("max_hits", defaults.archive_max_hits)),
            archive_max_excerpt_chars=int(archive.get("max_excerpt_chars", defaults.archive_max_excerpt_chars)),
            archive_min_score=float(archive.get("minimum_score", defaults.archive_min_score)),
        )
        if not 1 <= result.max_items <= 20:
            raise ValueError("continuity retrieval max_items must be 1..20")
        if not 0 <= result.max_open_loops <= result.max_items:
            raise ValueError("continuity max_open_loops must be 0..max_items")
        if not 256 <= result.max_context_chars <= 12000:
            raise ValueError("continuity max_context_chars must be 256..12000")
        if not 1 <= result.candidate_limit <= 200:
            raise ValueError("continuity candidate_limit must be 1..200")
        if not 1 <= result.semantic_candidate_limit <= 200:
            raise ValueError("continuity semantic candidate_limit must be 1..200")
        if not 0.0 <= result.mmr_lambda <= 1.0:
            raise ValueError("continuity mmr_lambda must be 0..1")
        if not 0.0 <= result.minimum_score <= 1.0:
            raise ValueError("continuity minimum_score must be 0..1")
        if result.retention_half_life_days <= 0 or result.retention_cold_after_days < 0 or result.retention_archive_after_days < result.retention_cold_after_days:
            raise ValueError("invalid continuity retention age policy")
        if result.retention_maintenance_interval_hours <= 0 or result.retention_max_hot_memories < 1:
            raise ValueError("invalid continuity retention maintenance policy")
        for name in (
            "structured_weight", "lexical_weight", "semantic_weight",
            "importance_weight", "recency_weight",
        ):
            if getattr(result, name) < 0:
                raise ValueError(f"continuity {name} must be non-negative")
        return result
