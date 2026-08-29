"""Schema helpers for VN Player mode.

These are lightweight runtime contracts rather than strict Pydantic models so
the MVP can run inside the existing backend environment with minimal deps.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Decision = Literal["silence", "hold", "speak", "summary", "deep", "context_request", "context_patch"]
PatchLayer = Literal[
    "observed_fact",
    "candidate_fact",
    "evidence",
    "hypothesis",
    "interpretation",
    "summary",
    "policy",
]

VALID_DECISIONS: set[str] = {
    "silence",
    "hold",
    "speak",
    "summary",
    "deep",
    "context_request",
    "context_patch",
}
VALID_PATCH_LAYERS: set[str] = {
    "observed_fact",
    "candidate_fact",
    "evidence",
    "hypothesis",
    "interpretation",
    "summary",
    "policy",
}
VALID_EMOTIONS: set[str] = {
    "normal",
    "thinking",
    "smile",
    "happy",
    "shy",
    "blush",
    "angry",
    "sad",
    "disappointed",
    "surprised",
    "surprise",
    "serious_speaking",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class VNProfile:
    session_id: str
    game_id: str = "unknown_vn"
    game_title: str = "Unknown VN"
    game_genre: str = "mystery"
    prompt_pack: str = "mystery"
    output_language: str = "ja"
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str = ""
    overlay_url: str = ""
    short_memory_lines: int = 50
    lookahead_enabled: bool = True
    lookahead_min_lines: int = 20
    lookahead_max_lines: int = 50
    lookahead_spoiler_policy: str = "abstract_only"
    max_reactions_per_minute: int = 8
    schema_modules: list[str] = field(
        default_factory=lambda: ["characters", "timeline", "evidence_map", "reasoning_graph", "open_questions"]
    )

    @classmethod
    def from_params(cls, params: dict[str, Any], defaults: dict[str, Any] | None = None) -> "VNProfile":
        data = dict(defaults or {})
        data.update({k: v for k, v in (params or {}).items() if v is not None})
        session_id = str(data.get("session_id") or new_id("vn_session"))
        return cls(
            session_id=session_id,
            game_id=str(data.get("game_id") or "unknown_vn"),
            game_title=str(data.get("game_title") or data.get("game_id") or "Unknown VN"),
            game_genre=str(data.get("game_genre") or "mystery"),
            prompt_pack=str(data.get("prompt_pack") or data.get("game_genre") or "mystery"),
            output_language=str(data.get("output_language") or "ja"),
            provider=str(data.get("provider") or "deepseek"),
            model=str(data.get("model") or "deepseek-v4-flash"),
            base_url=str(data.get("base_url") or ""),
            overlay_url=str(data.get("overlay_url") or data.get("overlayUrl") or ""),
            short_memory_lines=int(data.get("short_memory_lines") or 50),
            lookahead_enabled=bool(data.get("lookahead_enabled", True)),
            lookahead_min_lines=int(data.get("lookahead_min_lines") or 20),
            lookahead_max_lines=int(data.get("lookahead_max_lines") or 50),
            lookahead_spoiler_policy=str(data.get("lookahead_spoiler_policy") or "abstract_only"),
            max_reactions_per_minute=int(data.get("max_reactions_per_minute") or 8),
            schema_modules=list(data.get("schema_modules") or ["characters", "timeline", "evidence_map", "reasoning_graph", "open_questions"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "game_id": self.game_id,
            "game_title": self.game_title,
            "game_genre": self.game_genre,
            "prompt_pack": self.prompt_pack,
            "output_language": self.output_language,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "overlay_url": self.overlay_url,
            "short_memory_lines": self.short_memory_lines,
            "lookahead_enabled": self.lookahead_enabled,
            "lookahead_min_lines": self.lookahead_min_lines,
            "lookahead_max_lines": self.lookahead_max_lines,
            "lookahead_spoiler_policy": self.lookahead_spoiler_policy,
            "max_reactions_per_minute": self.max_reactions_per_minute,
            "schema_modules": self.schema_modules,
        }


def default_response(decision: str = "silence", *, reason_label: str = "fallback") -> dict[str, Any]:
    return {
        "schema_version": "vn.response.v1",
        "lane": "immediate",
        "decision": decision if decision in VALID_DECISIONS else "silence",
        "importance": 0.0,
        "confidence": 0.0,
        "reason_label": reason_label,
        "line_refs": {"current_line_id": "", "script_id": "", "target_script_id": ""},
        "cadence": {"sample_every": 1, "duration_lines": 0, "until_script_id": "", "reason": ""},
        "speak": None,
        "context_requests": [],
        "context_patches": [],
        "ui_cards": [],
        "lane_payload": {},
    }


def coerce_patch_item(value: Any) -> dict[str, Any]:
    """Turn common malformed LLM patch item shapes into a safe weak claim."""
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    malformed = {
        "id": None,
        "status": "open",
        "confidence": 0.35,
        "_coerced_from_malformed_item": True,
    }
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        malformed["claim"] = text
        return malformed
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                coerced = dict(item)
                coerced.setdefault("_coerced_from_malformed_item", True)
                return coerced
        text = " ".join(str(item).strip() for item in value if str(item or "").strip())
        if not text:
            return {}
        malformed["claim"] = text
        return malformed
    text = str(value).strip()
    if not text:
        return {}
    malformed["claim"] = text
    return malformed


def sanitize_response(raw: dict[str, Any] | None) -> dict[str, Any]:
    res = default_response()
    if isinstance(raw, dict):
        res.update(raw)
    if res.get("decision") not in VALID_DECISIONS:
        res["decision"] = "silence"
    if res["decision"] in {"silence", "hold"}:
        res["speak"] = None
    speak = res.get("speak")
    if speak is not None and not isinstance(speak, dict):
        res["speak"] = None
    if isinstance(res.get("speak"), dict):
        res["speak"]["priority"] = str(res["speak"].get("priority") or "normal")
        try:
            res["speak"]["expires_after_lines"] = max(1, int(res["speak"].get("expires_after_lines") or 3))
        except Exception:
            res["speak"]["expires_after_lines"] = 3
    patches = []
    for patch in res.get("context_patches") or []:
        if not isinstance(patch, dict):
            continue
        patch = _normalize_context_patch(patch)
        layer = str(patch.get("layer") or "")
        if layer not in VALID_PATCH_LAYERS:
            # Keep old prompts usable, but never let an unclassified patch
            # masquerade as a fact.
            patch["layer"] = "hypothesis"
        patches.append(patch)
    res["context_patches"] = patches
    return res


def _normalize_context_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """Accept both native VN patches and common JSON-Patch-like LLM output."""
    out = dict(patch)
    if "item" not in out and isinstance(out.get("value"), dict):
        out["item"] = out.get("value")
    if "item" not in out and "value" in out:
        out["item"] = coerce_patch_item(out.get("value"))
    if "item" not in out and isinstance(out.get("patch"), dict):
        nested = dict(out.get("patch") or {})
        if isinstance(nested.get("value"), dict):
            out["item"] = nested.get("value")
        else:
            key = str(nested.get("key") or nested.get("id") or "").strip()
            value = str(nested.get("value") or nested.get("claim") or "").strip()
            out["item"] = {
                "id": key or None,
                "claim": value or key or "LLM proposed a context update.",
                "confidence": nested.get("confidence", out.get("confidence", 0.5)),
                "status": nested.get("status", "open"),
            }
        if "action" not in out and nested.get("operation"):
            out["action"] = str(nested.get("operation"))
    if "action" not in out and out.get("op"):
        out["action"] = str(out.get("op"))
    if "target" not in out and out.get("path"):
        path = str(out.get("path") or "").strip("/")
        first = path.split("/")[0] if path else ""
        target_map = {
            "characters": "characters",
            "entities": "characters",
            "hypotheses": "hypotheses",
            "reasoning_graph": "reasoning_graph",
            "timeline": "timeline",
            "evidence": "evidence",
            "evidence_map": "evidence_map",
            "scene_summary": "scene_summary",
            "story_summary": "story_summary_log",
            "story_summary_log": "story_summary_log",
            "open_questions": "open_questions",
        }
        if first in target_map:
            out["target"] = target_map[first]
    out["target"] = _canonical_patch_target(str(out.get("target") or ""), str(out.get("path") or ""))
    if "item" in out:
        out["item"] = coerce_patch_item(out.get("item"))
    if "layer" not in out and isinstance(out.get("item"), dict):
        kind = str(out["item"].get("kind") or "").strip()
        if kind in VALID_PATCH_LAYERS:
            out["layer"] = kind
    return out


def _canonical_patch_target(target: str, path: str = "") -> str:
    value = (target or path or "").strip().strip("/")
    first = value.split("/")[0].split(".")[0].split("[")[0]
    aliases = {
        "characters": "characters",
        "character": "characters",
        "entities": "characters",
        "entity": "characters",
        "hypotheses": "hypotheses",
        "hypothesis": "hypotheses",
        "reasoning_graph": "reasoning_graph",
        "timeline": "timeline",
        "evidence": "evidence",
        "evidence_map": "evidence_map",
        "scene_summary": "scene_summary",
        "summary": "scene_summary",
        "story_summary": "story_summary_log",
        "story_summary_log": "story_summary_log",
        "open_questions": "open_questions",
    }
    if first in aliases:
        return aliases[first]
    return first or value
