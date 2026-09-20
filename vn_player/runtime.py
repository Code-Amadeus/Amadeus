"""Runtime orchestration for Amadeus VN Player mode."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .attention_router import build_attention_route
from .context_store import VNContextStore, _looks_mojibake
from .evidence_verifier import EvidenceVerifier
from .llm_client import VNLLMClient
from .prompts import immediate_context_view, immediate_prompt, lookahead_prompt, reasoner_prompt, retrospective_prompt, summary_prompt
from .schemas import VNProfile, default_response, new_id, now_ms, sanitize_response
from .script_index import ScriptIndex
from .text import looks_like_topic_label, strip_vn_tags, text_hash

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None] | None]
SpeakCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

VN_EVENT_LINE = "vn.line"
VN_EVENT_REACTION = "vn.reaction"
VN_EVENT_CONTEXT_UPDATED = "vn.context.updated"
VN_EVENT_STATUS = "vn.status"
VN_EVENT_ERROR = "vn.error"
VN_EVENT_SUMMARY = "vn.summary"

_PUNCT_RE = re.compile(r"[。！？!?…]")

_EVIDENCE_KEYWORDS = {
    "证据",
    "线索",
    "调查",
    "推理",
    "真相",
    "规则",
    "记录",
    "名字",
    "电话",
    "照片",
    "地图",
    "时间",
    "地点",
    "目击",
    "矛盾",
    "不对劲",
    "秘密",
    "理由",
    "动机",
    "真货",
    "实际存在",
    "能看到",
    "能够看到",
    "看得见",
    "看不见",
    "条件",
    "确凿",
    "诅咒珠",
    "咒主",
    "魂渣",
    "不留证据",
    "值得信赖",
}
_MYSTERY_KEYWORDS = {
    "死",
    "死亡",
    "杀",
    "尸体",
    "诅咒",
    "怨灵",
    "怪谈",
    "仪式",
    "复活",
    "人鱼",
    "失踪",
    "犯人",
    "嫌疑",
    "凶手",
    "命案",
    "危险",
    "救",
    "秘术",
    "本所七大不可思议",
    "置行堀",
    "通灵",
    "灵异",
    "杀人",
    "杀死",
    "咒主",
    "魂渣",
}
_AFFECT_KEYWORDS = {
    "没事",
    "不要",
    "等等",
    "为什么",
    "不可能",
    "对不起",
    "害怕",
    "可怕",
    "求你",
    "哭",
    "疯",
    "痛苦",
    "相信",
}
_CHOICE_KEYWORDS = {"选择", "怎么办", "怎么做", "该不该", "要不要", "决定", "选项"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _safe_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _param_bool(params: dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key in params:
            value = params.get(key)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
    return default


def _param_int(params: dict[str, Any], *keys: str, default: int, minimum: int = 0) -> int:
    for key in keys:
        if key in params:
            try:
                return max(minimum, int(params.get(key)))
            except Exception:
                return default
    return default


class VNPlayerRuntime:
    """Owns one active VN Player session and its local context store."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        event_emit: EventEmitter | None = None,
        speak_callback: SpeakCallback | None = None,
    ) -> None:
        self.project_root = Path(project_root)
        self.event_emit = event_emit
        self.speak_callback = speak_callback

        self.profile: VNProfile | None = None
        self.store: VNContextStore | None = None
        self.script_index = ScriptIndex.empty()
        self.script_path = ""
        self.llm: VNLLMClient | None = None
        self.verifier: EvidenceVerifier | None = None

        self.enabled = False
        self._lock = asyncio.Lock()
        self._last_script_order: int | None = None
        self._seen_exact_script_ids: set[str] = set()
        self._line_count = 0
        self._recent_speaks: list[float] = []
        self._last_speak_line_count: int | None = None
        self._silence_pressure_count = 0
        self._last_summary_line_count: int | None = None
        self._last_retrospective_line_count: int | None = None
        self._last_player_intervention: dict[str, Any] | None = None
        self._player_dialogue: list[dict[str, Any]] = []

        self._llm_enabled = _env_bool("VN_LLM_ENABLED", True)
        self._immediate_llm_enabled = _env_bool("VN_IMMEDIATE_LLM_ENABLED", self._llm_enabled)
        self._lookahead_llm_enabled = _env_bool("VN_LOOKAHEAD_LLM_ENABLED", False)
        self._reasoner_llm_enabled = _env_bool("VN_REASONER_LLM_ENABLED", False)
        self._summary_llm_enabled = _env_bool("VN_SUMMARY_LLM_ENABLED", False)
        self._retrospective_enabled = _env_bool("VN_RETROSPECTIVE_ENABLED", True)
        self._retrospective_llm_enabled = _env_bool("VN_RETROSPECTIVE_LLM_ENABLED", False)
        self._verifier_enabled = _env_bool("VN_VERIFIER_ENABLED", True)
        self._lookahead_max_calls = max(0, _safe_int("VN_LOOKAHEAD_MAX_CALLS", 20))
        self._lookahead_refresh_min_lines = max(5, _safe_int("VN_LOOKAHEAD_REFRESH_MIN_LINES", 30))
        self._lookahead_refresh_near_target_lines = max(1, _safe_int("VN_LOOKAHEAD_REFRESH_NEAR_TARGET_LINES", 3))
        self._reasoner_every_lines = max(1, _safe_int("VN_REASONER_EVERY_LINES", 4))
        self._summary_every_lines = max(1, _safe_int("VN_SUMMARY_EVERY_LINES", 12))
        self._retrospective_every_lines = max(5, _safe_int("VN_RETROSPECTIVE_EVERY_LINES", 40))
        self._retrospective_window_lines = max(20, _safe_int("VN_RETROSPECTIVE_WINDOW_LINES", 80))
        self._silence_pressure_enabled = _env_bool("VN_IMMEDIATE_SILENCE_PRESSURE_ENABLED", True)
        self._silence_pressure_start = max(1, _safe_int("VN_IMMEDIATE_SILENCE_PRESSURE_START", 8))
        self._silence_pressure_opening_start = max(1, _safe_int("VN_IMMEDIATE_SILENCE_PRESSURE_OPENING_START", 4))
        self._silence_pressure_opening_lines = max(1, _safe_int("VN_IMMEDIATE_SILENCE_PRESSURE_OPENING_LINES", 45))
        self._silence_pressure_force_after = max(2, _safe_int("VN_IMMEDIATE_SILENCE_PRESSURE_FORCE_AFTER", 12))
        self._lookahead_task: asyncio.Task[None] | None = None
        self._lookahead_cache: dict[str, Any] | None = None
        self._lookahead_cache_to_order: int | None = None
        self._lookahead_last_refresh_order: int | None = None
        self._lookahead_llm_call_count = 0

    async def start(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start or replace the active VN session."""
        params = params or {}
        async with self._lock:
            defaults = self._profile_defaults()
            profile = VNProfile.from_params(params, defaults)
            script_path = str(params.get("script_path") or os.environ.get("VN_SCRIPT_PATH") or self._default_script_path())
            index = ScriptIndex.from_file(script_path, language=str(params.get("script_language") or "zh_Hans"))
            self._lookahead_llm_enabled = _param_bool(
                params,
                "lookahead_llm_enabled",
                "lookaheadLlmEnabled",
                default=self._lookahead_llm_enabled,
            )
            self._lookahead_max_calls = _param_int(
                params,
                "lookahead_max_calls",
                "lookaheadMaxCalls",
                default=self._lookahead_max_calls,
                minimum=0,
            )

            self.profile = profile
            self.store = VNContextStore(self.project_root, profile)
            self.script_index = index
            self.script_path = script_path
            self.llm = VNLLMClient(profile)
            self.verifier = EvidenceVerifier(self.store, self.script_index, hidden_probe=True) if self._verifier_enabled else None
            self.enabled = True
            self._line_count = 0
            self._recent_speaks = []
            self._last_speak_line_count = None
            self._silence_pressure_count = 0
            self._last_summary_line_count = None
            self._last_retrospective_line_count = None
            self._last_script_order = None
            self._seen_exact_script_ids = set()
            self._reset_lookahead_planner()

            self.store.record_runtime_event(
                "vn.start",
                {
                    "profile": profile.to_dict(),
                    "script_path": script_path,
                    "script_line_count": len(index.lines),
                    "llm_enabled": self._llm_enabled,
                    "immediate_llm_enabled": self._immediate_llm_enabled,
                    "lookahead_llm_enabled": self._lookahead_llm_enabled,
                    "lookahead_max_calls": self._lookahead_max_calls,
                    "reasoner_llm_enabled": self._reasoner_llm_enabled,
                    "summary_llm_enabled": self._summary_llm_enabled,
                    "retrospective_enabled": self._retrospective_enabled,
                    "retrospective_llm_enabled": self._retrospective_llm_enabled,
                    "verifier_enabled": self._verifier_enabled,
                },
            )
            result = self.status()
        await self._emit(VN_EVENT_STATUS, result)
        return result

    async def stop(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            self.enabled = False
            self._cancel_lookahead_task()
            if self.store is not None:
                self.store.record_runtime_event("vn.stop", dict(params or {}))
            result = self.status()
        await self._emit(VN_EVENT_STATUS, result)
        return result

    def status(self) -> dict[str, Any]:
        profile = self.profile.to_dict() if self.profile else None
        return {
            "status": "active" if self.enabled else "stopped",
            "profile": profile,
            "script": {
                "path": self.script_path,
                "line_count": len(self.script_index.lines),
                "last_order": self._last_script_order,
            },
            "llm": {
                "enabled": self._llm_enabled,
                "immediate_enabled": self._immediate_llm_enabled,
                "lookahead_enabled": self._lookahead_llm_enabled,
                "lookahead_max_calls": self._lookahead_max_calls,
                "lookahead_calls_used": self._lookahead_llm_call_count,
                "reasoner_enabled": self._reasoner_llm_enabled,
                "summary_enabled": self._summary_llm_enabled,
                "retrospective_enabled": self._retrospective_enabled,
                "retrospective_llm_enabled": self._retrospective_llm_enabled,
                "verifier_enabled": self._verifier_enabled,
            },
        }

    async def ingest_line(self, params: dict[str, Any]) -> dict[str, Any]:
        """Ingest one live VN line and optionally emit a Kurisu reaction."""
        if self.profile is None or self.store is None:
            await self.start({})
        assert self.profile is not None
        assert self.store is not None

        text = strip_vn_tags(str(params.get("text") or "")).strip()
        if not text:
            return {**self.status(), "status": "ignored", "reason": "empty_text"}

        async with self._lock:
            supplied_script_id = str(params.get("script_id") or "").strip()
            supplied_line = self.script_index._by_id.get(supplied_script_id) if supplied_script_id else None
            if supplied_line is not None:
                match = {"match_type": "id", "score": 1.0, "line": supplied_line.to_dict()}
            else:
                if _looks_mojibake(text):
                    match = None
                    if self._last_script_order is not None:
                        next_order = int(self._last_script_order) + 1
                        if 0 <= next_order < len(self.script_index.lines):
                            match = {
                                "match_type": "sequence_after_anchor",
                                "score": 0.75,
                                "line": self.script_index.lines[next_order].to_dict(),
                            }
                else:
                    match = self.script_index.match(text, after_order=self._last_script_order)
            matched_order: int | None = None
            if match and isinstance(match.get("line"), dict):
                try:
                    matched_order = int(match["line"].get("order"))
                except Exception:
                    matched_order = None
            matched_script_id = str((match.get("line") or {}).get("script_id") or "") if match else ""
            match_type = str(match.get("match_type") or "") if match else ""
            exact_script_id = supplied_script_id or (matched_script_id if match_type == "id" else "")
            if exact_script_id and exact_script_id in self._seen_exact_script_ids:
                ignored = {
                    "reason": "duplicate_exact_script_id",
                    "incoming_text": text[:160],
                    "incoming_script_id": supplied_script_id,
                    "matched_script_id": matched_script_id,
                    "matched_order": matched_order,
                    "last_order": self._last_script_order,
                    "match": {
                        "type": match_type,
                        "score": float(match.get("score") or 0.0),
                    },
                    "metadata": dict(params.get("metadata") or {}),
                }
                self.store.record_runtime_event("vn.line.ignored", ignored)
                return {**self.status(), "status": "ignored", **ignored}
            if matched_order is not None and match_type != "hash":
                self._last_script_order = matched_order
            if exact_script_id:
                self._seen_exact_script_ids.add(exact_script_id)
            line_event = self.store.record_line(params, match)
            self._line_count += 1

        await self._emit(VN_EVENT_LINE, {"line": line_event, "match": match or {}})

        try:
            lookahead = self._lookahead_for_immediate(line_event)
            self._schedule_lookahead_refresh(line_event, lookahead)
            retrospective_bias = self.store.retrospective_bias()
            attention = self._build_attention_route(line_event, lookahead, retrospective_bias=retrospective_bias)
            attention = self._apply_silence_pressure_to_attention(attention, line_event)
            context_pack = self._build_context_pack(line_event, lookahead, attention=attention)
            response = await self._immediate_response(context_pack, line_event, lookahead)
            response = self._postprocess_response(response, line_event)
            response = self._apply_attention_guard(response, attention, line_event)
            response = self._apply_silence_pressure_guard(response, attention, line_event)
            response = self._apply_line_budget_guard(response, attention, line_event)
            self._update_silence_pressure(response)

            patches, verification = self._verify_context_patches(response.get("context_patches") or [], line_event)
            applied = self.store.apply_context_patches(patches, source_line=line_event)
            self.store.record_reaction(response, line_event)
            if applied:
                await self._emit(
                    VN_EVENT_CONTEXT_UPDATED,
                    {
                        "session_id": self.profile.session_id,
                        "source_line": _line_ref(line_event),
                        "applied": applied,
                    },
                )

            reaction_payload = {
                "session_id": self.profile.session_id,
                "line": line_event,
                "reaction": response,
                "lookahead": self._public_lookahead(lookahead),
                "attention": attention,
                "retrospective_bias": attention.get("retrospective_bias") or {},
                "context_applied": applied,
                "verification": verification,
            }
            await self._emit(VN_EVENT_REACTION, reaction_payload)

            if response.get("decision") == "speak" and response.get("speak"):
                await self._speak(response["speak"], line_event)

            fact_result = await self._maybe_run_fact_extractor(context_pack, line_event)
            if fact_result:
                reaction_payload["fact_extractor"] = fact_result
                context_pack = self._build_context_pack(line_event, lookahead, attention=attention, write_immediate_view=False)

            character_result = await self._maybe_run_character_modeler(context_pack, line_event)
            if character_result:
                reaction_payload["character_modeler"] = character_result
                context_pack = self._build_context_pack(line_event, lookahead, attention=attention, write_immediate_view=False)

            reasoner_result = await self._maybe_run_reasoner(context_pack, line_event, response)
            if reasoner_result:
                reaction_payload["reasoner"] = reasoner_result

            summary_result = await self._maybe_run_summary(context_pack, line_event)
            if summary_result:
                reaction_payload["summary"] = summary_result

            retrospective_result = await self._maybe_run_retrospective(line_event, response)
            if retrospective_result:
                reaction_payload["retrospective"] = retrospective_result

            return {"status": "ok", **reaction_payload}
        except Exception as exc:
            logger.exception("VN line handling failed")
            payload = {"error": str(exc), "line": line_event}
            self.store.record_runtime_event("vn.error", payload)
            await self._emit(VN_EVENT_ERROR, payload)
            fallback = default_response("silence", reason_label="runtime_error")
            self.store.record_reaction(fallback, line_event)
            return {"status": "error", "error": str(exc), "line": line_event, "reaction": fallback}

    async def player_intervention(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record player notes/questions and optionally ask Kurisu to answer."""
        if self.profile is None or self.store is None:
            await self.start({})
        assert self.profile is not None
        assert self.store is not None

        text = str(params.get("text") or params.get("question") or "").strip()
        event = {
            "id": new_id("player"),
            "kind": kind,
            "text": text,
            "params": dict(params or {}),
            "recorded_at_ms": now_ms(),
        }
        self._last_player_intervention = event
        self._record_player_dialogue(event)
        self.store.record_runtime_event(f"vn.player.{kind}", event)

        if kind not in {"ask", "choice"} or not text:
            return {"status": "ok", "event": event}

        last_line = (self.store.short_memory() or [{}])[-1]
        context_pack = self._build_context_pack(last_line, self._empty_lookahead(last_line), player_intervention=event)
        response = await self._immediate_response(context_pack, last_line, self._empty_lookahead(last_line), force_llm=True)
        response = self._postprocess_response(response, last_line, player_requested=True)
        if response.get("decision") != "speak":
            output_language = (self.profile.output_language or "ja").strip().lower()
            fallback_text = (
                "\u5c11\u3057\u5f85\u3063\u3066\u3002[EMO preset=thinking dur=8s] "
                "\u305d\u306e\u8cea\u554f\u306f\u4eca\u898b\u3048\u3066\u3044\u308b\u60c5\u5831\u3060\u3051\u3067"
                "\u6574\u7406\u3057\u305f\u65b9\u304c\u3088\u3055\u305d\u3046\u306d\u3002"
                "\u5206\u304b\u3063\u3066\u3044\u308b\u7bc4\u56f2\u3067\u7b54\u3048\u308b\u308f\u3002"
                if output_language.startswith("ja") or "japanese" in output_language
                else "\u7b49\u4e00\u4e0b\u3002[EMO preset=thinking dur=8s] "
                "\u8fd9\u4e2a\u95ee\u9898\u5e94\u8be5\u53ea\u6309\u76ee\u524d\u5df2\u7ecf\u663e\u793a\u7684"
                "\u4fe1\u606f\u6765\u5224\u65ad\u3002\u6211\u4f1a\u57fa\u4e8e\u73b0\u6709\u7ebf\u7d22\u56de\u7b54\u3002"
            )
            response["decision"] = "speak"
            response["speak"] = {
                "text": fallback_text,
                "priority": "high",
                "interrupt": False,
                "expires_after_lines": 3,
                "target_line_id": last_line.get("line_id", ""),
                "target_script_id": last_line.get("script_id", ""),
                "emotion_intent": "thinking",
            }
        self.store.record_reaction(response, last_line)
        await self._emit(
            VN_EVENT_REACTION,
            {"session_id": self.profile.session_id, "line": last_line, "reaction": response, "source": f"player.{kind}"},
        )
        await self._speak(response["speak"], last_line)
        event["answered_at_ms"] = now_ms()
        return {"status": "ok", "event": event, "reaction": response}

    def _record_player_dialogue(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        if kind in {"clear", "reset"}:
            self._player_dialogue = []
            return
        text = str(event.get("text") or "").strip()
        if not text:
            return
        self._player_dialogue.append(
            {
                "id": event.get("id"),
                "kind": kind,
                "text": text,
                "line_count": self._line_count,
                "recorded_at_ms": event.get("recorded_at_ms"),
            }
        )
        self._player_dialogue = self._player_dialogue[-12:]

    def _recent_player_dialogue(self, limit: int = 6) -> list[dict[str, Any]]:
        return list(self._player_dialogue[-limit:])

    def _profile_defaults(self) -> dict[str, Any]:
        return {
            "game_id": os.environ.get("VN_GAME_ID", "paranormasight_the_mermaids_curse"),
            "game_title": os.environ.get("VN_GAME_TITLE", "Paranormasight: The Mermaid's Curse"),
            "game_genre": os.environ.get("VN_GAME_GENRE", "mystery"),
            "prompt_pack": os.environ.get("VN_PROMPT_PACK", os.environ.get("VN_GAME_GENRE", "mystery")),
            "output_language": os.environ.get("VN_OUTPUT_LANGUAGE", "zh"),
            "provider": os.environ.get("VN_LLM_PROVIDER", os.environ.get("LLM_PROVIDER", "deepseek")),
            "model": os.environ.get("VN_LLM_MODEL", "deepseek-v4-flash"),
            "base_url": os.environ.get("VN_LLM_BASE_URL", ""),
            "short_memory_lines": _safe_int("VN_SHORT_MEMORY_LINES", 50),
            "lookahead_enabled": _env_bool("VN_LOOKAHEAD_ENABLED", True),
            "lookahead_min_lines": _safe_int("VN_LOOKAHEAD_MIN_LINES", 20),
            "lookahead_max_lines": _safe_int("VN_LOOKAHEAD_MAX_LINES", 50),
            "max_reactions_per_minute": _safe_int("VN_MAX_REACTIONS_PER_MINUTE", 8),
        }

    def _default_script_path(self) -> str:
        candidate = (
            self.project_root.parent
            / "visual novel player"
            / "ParanormasightChsLocalization"
            / "texts"
            / "zh_Hans"
            / "Hazy_Script.txt"
        )
        return str(candidate)

    def _reset_lookahead_planner(self) -> None:
        self._cancel_lookahead_task()
        self._lookahead_cache = None
        self._lookahead_cache_to_order = None
        self._lookahead_last_refresh_order = None
        self._lookahead_llm_call_count = 0

    def _cancel_lookahead_task(self) -> None:
        task = self._lookahead_task
        if task is not None and not task.done():
            task.cancel()
        self._lookahead_task = None

    def _script_order(self, script_id: str) -> int | None:
        line = self.script_index._by_id.get(str(script_id or ""))
        if line is None:
            return None
        return int(line.order)

    def _lookahead_for_immediate(self, line_event: dict[str, Any]) -> dict[str, Any]:
        assert self.profile is not None and self.store is not None
        script_id = str(line_event.get("script_id") or "")
        if not self.profile.lookahead_enabled or not script_id:
            plan = self._empty_lookahead(line_event)
            self.store.save_lookahead_plan(plan)
            return plan

        current_order = self._script_order(script_id)
        if self._lookahead_cache and self._lookahead_cache_covers(current_order):
            return self._lookahead_cache_for_line(self._lookahead_cache, line_event, current_order)

        future_window = self.script_index.window_after(script_id, count=self.profile.lookahead_max_lines)
        plan = self._rule_lookahead(line_event, future_window)
        self.store.save_lookahead_plan(plan)
        return plan

    def _lookahead_cache_covers(self, current_order: int | None) -> bool:
        if current_order is None or self._lookahead_cache_to_order is None:
            return False
        if self._lookahead_last_refresh_order is not None and current_order < self._lookahead_last_refresh_order:
            return False
        return current_order <= self._lookahead_cache_to_order

    def _lookahead_cache_for_line(
        self,
        cached: dict[str, Any],
        line_event: dict[str, Any],
        current_order: int | None,
    ) -> dict[str, Any]:
        plan = dict(cached)
        plan["current_script_id"] = str(line_event.get("script_id") or "")
        adjusted: list[dict[str, Any]] = []
        for item in list(cached.get("reaction_plan") or []):
            if not isinstance(item, dict):
                continue
            next_item = dict(item)
            target_order = self._script_order(str(next_item.get("target_script_id") or ""))
            if current_order is not None and target_order is not None:
                distance = target_order - current_order
                if distance < 0:
                    continue
                next_item["distance_lines"] = distance
            adjusted.append(next_item)
        plan["reaction_plan"] = adjusted
        plan["source"] = f"{cached.get('source') or 'lookahead'}_cache"
        return plan

    def _schedule_lookahead_refresh(self, line_event: dict[str, Any], active_plan: dict[str, Any]) -> None:
        if not self.profile or not self.store:
            return
        if not self.profile.lookahead_enabled or not self._llm_enabled or not self._lookahead_llm_enabled or self.llm is None:
            return
        if self._lookahead_max_calls and self._lookahead_llm_call_count >= self._lookahead_max_calls:
            return
        if self._lookahead_task is not None and not self._lookahead_task.done():
            return
        script_id = str(line_event.get("script_id") or "")
        if not script_id:
            return
        current_order = self._script_order(script_id)
        if current_order is None:
            return
        reason = self._lookahead_refresh_reason(current_order, active_plan)
        if not reason:
            return
        self._lookahead_task = asyncio.create_task(self._refresh_lookahead_plan(line_event, reason))

    def _lookahead_refresh_reason(self, current_order: int, active_plan: dict[str, Any]) -> str:
        if self._lookahead_last_refresh_order is None:
            return "initial_window"
        if not self._lookahead_cache_covers(current_order):
            return "stale_window"
        if current_order - self._lookahead_last_refresh_order >= self._lookahead_refresh_min_lines:
            return "periodic_window"
        for item in active_plan.get("reaction_plan") or []:
            if not isinstance(item, dict):
                continue
            try:
                distance = int(item.get("distance_lines"))
            except Exception:
                continue
            if 0 <= distance <= self._lookahead_refresh_near_target_lines:
                if current_order - self._lookahead_last_refresh_order >= self._lookahead_refresh_near_target_lines:
                    return "near_planned_beat"
        return ""

    async def _refresh_lookahead_plan(self, line_event: dict[str, Any], reason: str) -> None:
        try:
            plan = await self._build_lookahead(line_event)
            current_order = self._script_order(str(line_event.get("script_id") or ""))
            if plan.get("source") == "llm":
                self._lookahead_cache = plan
                self._lookahead_last_refresh_order = current_order
                to_script_id = str((plan.get("window") or {}).get("to_script_id") or "")
                self._lookahead_cache_to_order = self._script_order(to_script_id)
                if self.store is not None:
                    self.store.record_runtime_event(
                        "vn.lookahead.refresh",
                        {
                            "line": _line_ref(line_event),
                            "reason": reason,
                            "calls_used": self._lookahead_llm_call_count,
                            "window": plan.get("window") or {},
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("VN lookahead refresh failed")
            if self.store is not None:
                self.store.record_runtime_event(
                    "vn.lookahead.error",
                    {"line": _line_ref(line_event), "reason": reason, "error": str(exc)},
                )
        finally:
            current = asyncio.current_task()
            if self._lookahead_task is current:
                self._lookahead_task = None

    async def _build_lookahead(self, line_event: dict[str, Any]) -> dict[str, Any]:
        profile = self.profile
        store = self.store
        assert profile is not None and store is not None

        if not profile.lookahead_enabled or not line_event.get("script_id"):
            plan = self._empty_lookahead(line_event)
            store.save_lookahead_plan(plan)
            return plan

        future_window = self.script_index.window_after(
            str(line_event.get("script_id") or ""),
            count=profile.lookahead_max_lines,
        )
        planner_context = {
            "current_line": line_event,
            "future_window": future_window,
            "short_memory": store.short_memory()[-10:],
            "scene_summary": store.scene_summary(),
            "runtime_policy": {
                "future_window_visible_only_to_planner": True,
                "planner_output_must_be_spoiler_safe": True,
            },
        }

        can_call_llm = (
            self._llm_enabled
            and self._lookahead_llm_enabled
            and future_window
            and self.llm is not None
            and (not self._lookahead_max_calls or self._lookahead_llm_call_count < self._lookahead_max_calls)
        )
        if can_call_llm:
            self._lookahead_llm_call_count += 1
            messages = lookahead_prompt(profile, planner_context)
            try:
                parsed, raw = await self.llm.complete_json(messages, lane="lookahead", max_tokens=650, temperature=0.2)
            except Exception as exc:
                store.record_model_call(
                    "lookahead",
                    {"context_metrics": _lookahead_metrics(planner_context), "context": _redact_future_text(planner_context)},
                    {"error": str(exc)},
                    ok=False,
                )
            else:
                store.record_model_call(
                    "lookahead",
                    {"context_metrics": _lookahead_metrics(planner_context), "context": _redact_future_text(planner_context)},
                    parsed or raw,
                    ok=parsed is not None,
                )
                if parsed:
                    plan = self._sanitize_lookahead(parsed, line_event, future_window)
                    store.save_lookahead_plan(plan)
                    return plan

        plan = self._rule_lookahead(line_event, future_window)
        store.save_lookahead_plan(plan)
        return plan

    def _empty_lookahead(self, line_event: dict[str, Any]) -> dict[str, Any]:
        script_id = str(line_event.get("script_id") or "")
        return {
            "schema_version": "vn.lookahead.v1",
            "current_script_id": script_id,
            "window": {"from_script_id": script_id, "to_script_id": script_id, "line_count": 0},
            "spoiler_policy": "abstract_only",
            "density": {"current": 0.0, "next_5": 0.0, "next_20": 0.0},
            "reaction_plan": [],
            "cadence": {"sample_every": 1, "until_script_id": "", "reason": "no_script_window"},
            "source": "empty",
        }

    def _rule_lookahead(self, line_event: dict[str, Any], future_window: list[dict[str, Any]]) -> dict[str, Any]:
        current_script_id = str(line_event.get("script_id") or "")
        scored: list[dict[str, Any]] = []
        for idx, item in enumerate(future_window):
            score, kind = _score_line(item.get("text", ""))
            if score <= 0:
                continue
            scored.append(
                {
                    "script_id": item.get("script_id", ""),
                    "order": item.get("order", 0),
                    "distance": idx + 1,
                    "score": score,
                    "kind": kind,
                }
            )
        scored.sort(key=lambda item: (-float(item["score"]), int(item["distance"])))

        current_score, current_kind = _score_line(line_event.get("text", ""))
        next_5 = sum(_score_line(item.get("text", ""))[0] for item in future_window[:5]) / max(len(future_window[:5]), 1)
        next_20 = sum(_score_line(item.get("text", ""))[0] for item in future_window[:20]) / max(len(future_window[:20]), 1)

        reaction_plan = []
        if scored:
            target = scored[0]
            stronger_soon = target["distance"] <= 6 and target["score"] >= current_score + 1.0
            reaction_plan.append(
                {
                    "target_script_id": target["script_id"],
                    "kind": target["kind"],
                    "priority": "high" if target["score"] >= 4 else "normal",
                    "suggested_action": "hold_until_target" if stronger_soon else "react_on_target",
                    "spoiler_safe_hint": (
                        "A stronger emotional or evidence beat follows shortly."
                        if stronger_soon
                        else "A plot-relevant beat is in this window."
                    ),
                    "context_topics_to_prepare": ["characters", "hypotheses"] if target["kind"] != "low_density" else [],
                    "speak_before_target": False,
                    "distance_lines": target["distance"],
                    "abstract_score": target["score"],
                }
            )

        sample_every = 1
        reason = "dense_window"
        if next_20 < 0.9:
            sample_every = 10
            reason = "low_density_window"
        elif next_5 < 1.2 and current_score < 2:
            sample_every = 5
            reason = "connective_dialogue"

        return {
            "schema_version": "vn.lookahead.v1",
            "current_script_id": current_script_id,
            "window": {
                "from_script_id": future_window[0].get("script_id", "") if future_window else current_script_id,
                "to_script_id": future_window[-1].get("script_id", "") if future_window else current_script_id,
                "line_count": len(future_window),
            },
            "spoiler_policy": "abstract_only",
            "density": {
                "current": round(current_score / 5, 2),
                "next_5": round(min(next_5 / 5, 1.0), 2),
                "next_20": round(min(next_20 / 5, 1.0), 2),
                "current_kind": current_kind,
            },
            "reaction_plan": reaction_plan,
            "cadence": {
                "sample_every": sample_every,
                "until_script_id": reaction_plan[0]["target_script_id"] if reaction_plan else "",
                "reason": reason,
            },
            "source": "rules",
        }

    def _sanitize_lookahead(
        self,
        raw: dict[str, Any],
        line_event: dict[str, Any],
        future_window: list[dict[str, Any]],
    ) -> dict[str, Any]:
        plan = self._empty_lookahead(line_event)
        plan.update({k: v for k, v in raw.items() if k in plan or k in {"reaction_plan", "cadence", "density"}})
        plan["schema_version"] = "vn.lookahead.v1"
        plan["spoiler_policy"] = "abstract_only"
        plan["current_script_id"] = str(line_event.get("script_id") or "")
        if future_window:
            plan["window"] = {
                "from_script_id": future_window[0].get("script_id", ""),
                "to_script_id": future_window[-1].get("script_id", ""),
                "line_count": len(future_window),
            }
        safe_plan = []
        future_ids = {str(item.get("script_id") or "") for item in future_window}
        for item in list(plan.get("reaction_plan") or [])[:5]:
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("target_script_id") or "")
            if target_id and target_id not in future_ids and target_id != line_event.get("script_id"):
                continue
            kind = str(item.get("kind") or "low_density")
            suggested_action = str(item.get("suggested_action") or "react_on_target")
            safe_plan.append(
                {
                    "target_script_id": target_id,
                    "kind": kind,
                    "priority": str(item.get("priority") or "normal"),
                    "suggested_action": suggested_action,
                    "spoiler_safe_hint": _generic_lookahead_hint(kind, suggested_action),
                    "context_topics_to_prepare": _safe_context_topics(item.get("context_topics_to_prepare")),
                    "speak_before_target": bool(item.get("speak_before_target", False)),
                }
            )
        plan["reaction_plan"] = safe_plan
        plan["source"] = "llm"
        return plan

    def _build_attention_route(
        self,
        line_event: dict[str, Any],
        lookahead: dict[str, Any],
        *,
        retrospective_bias: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self.profile is not None
        current_score, current_kind = _score_line(line_event.get("text", ""))
        return build_attention_route(
            profile=self.profile,
            line_event=line_event,
            lookahead=lookahead,
            current_score=current_score,
            current_kind=current_kind,
            line_count=self._line_count,
            retrospective_bias=retrospective_bias,
        )

    def _build_context_pack(
        self,
        line_event: dict[str, Any],
        lookahead: dict[str, Any],
        *,
        attention: dict[str, Any] | None = None,
        player_intervention: dict[str, Any] | None = None,
        write_immediate_view: bool = True,
    ) -> dict[str, Any]:
        assert self.profile is not None and self.store is not None
        public_lookahead = self._public_lookahead(lookahead)
        retrospective_bias = self.store.retrospective_bias()
        attention = attention or self._build_attention_route(
            line_event,
            lookahead,
            retrospective_bias=retrospective_bias,
        )
        max_context_lines = int((attention.get("budget") or {}).get("max_context_lines") or self.profile.short_memory_lines)
        pack = {
            "schema_version": "vn.context_pack.v1",
            "session_id": self.profile.session_id,
            "game": {
                "id": self.profile.game_id,
                "title": self.profile.game_title,
                "genre": self.profile.game_genre,
                "prompt_pack": self.profile.prompt_pack,
            },
            "current_line": line_event,
            "short_memory": self.store.short_memory()[-max_context_lines:],
            "scene_summary": self.store.scene_summary(),
            "story_summary_log": self.store.story_summary_log()[-12:],
            "characters": self.store.characters(),
            "hypotheses": self.store.hypotheses()[-20:],
            "evidence_nodes": self.store.evidence_nodes()[-20:],
            "verifier_feedback": self.store.verifier_feedback()[-12:],
            "recent_kurisu_speech": _recent_kurisu_speech(self.store.recent_reactions(80), limit=8),
            "lookahead_hint": public_lookahead,
            "retrospective_bias": retrospective_bias,
            "attention": attention,
            "player_intervention": player_intervention,
            "recent_player_dialogue": self._recent_player_dialogue(),
            "runtime_policy": {
                "facts_runtime_owned": True,
                "candidate_facts_require_displayed_evidence": True,
                "evidence_nodes_require_displayed_evidence": True,
                "unseen_script_probe_may_warn_but_not_reveal_text": True,
                "llm_may_patch": ["candidate_fact", "evidence", "hypothesis", "interpretation", "summary"],
                "do_not_spoil_future": True,
                "speak_max_sentences": 2,
            },
        }
        self.store.write_context_pack("immediate", pack)
        if write_immediate_view:
            self.store.write_context_pack(
                "immediate.clean",
                {
                    "schema_version": "vn.immediate_clean_context.v1",
                    "session_id": self.profile.session_id,
                    "text": immediate_context_view(pack),
                },
            )
        return pack

    async def _immediate_response(
        self,
        context_pack: dict[str, Any],
        line_event: dict[str, Any],
        lookahead: dict[str, Any],
        *,
        force_llm: bool = False,
    ) -> dict[str, Any]:
        assert self.profile is not None and self.store is not None
        if (self._immediate_llm_enabled or force_llm) and self.llm is not None:
            messages = immediate_prompt(self.profile, context_pack)
            parsed, raw = await self.llm.complete_json(messages, lane="immediate", max_tokens=800, temperature=0.55)
            self.store.record_model_call(
                "immediate",
                {"context_metrics": _context_metrics(context_pack), "clean_context": immediate_context_view(context_pack)},
                parsed or raw,
                ok=parsed is not None,
            )
            if parsed is not None:
                response = sanitize_response(parsed)
                if response.get("decision") == "context_request":
                    expanded = self._resolve_context_requests(response.get("context_requests") or [])
                    context_pack = dict(context_pack)
                    context_pack["retrieved_context"] = expanded
                    self.store.write_context_pack("immediate.expanded", context_pack)
                    messages = immediate_prompt(self.profile, context_pack)
                    parsed2, raw2 = await self.llm.complete_json(messages, lane="immediate_context_retry", max_tokens=800, temperature=0.5)
                    self.store.record_model_call(
                        "immediate_context_retry",
                        {"context_metrics": _context_metrics(context_pack), "clean_context": immediate_context_view(context_pack)},
                        parsed2 or raw2,
                        ok=parsed2 is not None,
                    )
                    if parsed2 is not None:
                        response = sanitize_response(parsed2)
                return response
        return self._rule_immediate(line_event, lookahead, context_pack.get("attention") or {})

    async def _maybe_run_fact_extractor(
        self,
        context_pack: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert self.store is not None
        attention = context_pack.get("attention") or {}
        route = attention.get("route") or {}
        if route.get("fact_extractor") != "run":
            return None
        text = strip_vn_tags(str(line_event.get("text") or "")).strip()
        if not _claim_family(text, str(attention.get("current_kind") or "")):
            return None
        patch = _rule_evidence_node_patch(line_event, attention)
        patches, verification = self._verify_context_patches([patch], line_event)
        applied = self.store.apply_context_patches(patches, source_line=line_event)
        if applied:
            await self._emit(
                VN_EVENT_CONTEXT_UPDATED,
                {
                    "session_id": self.profile.session_id if self.profile else "",
                    "source_line": _line_ref(line_event),
                    "applied": applied,
                    "lane": "fact_extractor_rules",
                },
            )
        return {"lane": "fact_extractor_rules", "applied": applied, "verification": verification}

    async def _maybe_run_character_modeler(
        self,
        context_pack: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert self.store is not None
        attention = context_pack.get("attention") or {}
        route = attention.get("route") or {}
        if route.get("character_modeler") != "run":
            return None
        raw_patches = _rule_character_mention_patches(line_event, attention)
        if not raw_patches:
            return None
        patches, verification = self._verify_context_patches(raw_patches, line_event)
        applied = self.store.apply_context_patches(patches, source_line=line_event)
        if applied:
            await self._emit(
                VN_EVENT_CONTEXT_UPDATED,
                {
                    "session_id": self.profile.session_id if self.profile else "",
                    "source_line": _line_ref(line_event),
                    "applied": applied,
                    "lane": "character_modeler_rules",
                },
            )
        return {"lane": "character_modeler_rules", "applied": applied, "verification": verification}

    async def _maybe_run_summary(
        self,
        context_pack: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert self.profile is not None and self.store is not None
        attention = context_pack.get("attention") or {}
        route = attention.get("route") or {}
        budget = attention.get("budget") or {}
        summary_route = str(route.get("summary") or "append_later")
        summary_after = max(1, int(budget.get("next_summary_after_lines") or self._summary_every_lines))
        try:
            summary_cooldown = max(1, int(budget.get("summary_cooldown_lines") or 5))
        except Exception:
            summary_cooldown = 5
        lines_since_summary = (
            999999 if self._last_summary_line_count is None else self._line_count - self._last_summary_line_count
        )
        event_append = summary_route == "append_now" and lines_since_summary >= summary_cooldown
        periodic_append = (
            summary_route != "append_now"
            and self._line_count % summary_after == 0
            and lines_since_summary >= min(summary_cooldown, summary_after)
        )
        should_append = event_append or periodic_append
        if summary_route == "skip" or not should_append:
            return None

        if self._llm_enabled and self._summary_llm_enabled and self.llm is not None:
            messages = summary_prompt(self.profile, context_pack)
            parsed, raw = await self.llm.complete_json(messages, lane="summary", max_tokens=900, temperature=0.25)
            self.store.record_model_call(
                "summary",
                {"context_metrics": _context_metrics(context_pack), "context_pack": _compact_context_for_log(context_pack)},
                parsed or raw,
                ok=parsed is not None,
            )
            if parsed:
                response = sanitize_response(parsed)
                patches, verification = self._verify_context_patches(response.get("context_patches") or [], line_event)
                applied = self.store.apply_context_patches(patches, source_line=line_event)
                if applied:
                    await self._emit(
                        VN_EVENT_CONTEXT_UPDATED,
                        {
                            "session_id": self.profile.session_id,
                            "source_line": _line_ref(line_event),
                            "applied": applied,
                            "lane": "summary",
                        },
                    )
                await self._emit(
                    VN_EVENT_SUMMARY,
                    {
                        "session_id": self.profile.session_id,
                        "source_line": _line_ref(line_event),
                        "scene_summary": self.store.scene_summary(),
                    },
                )
                self._last_summary_line_count = self._line_count
                return {"lane": "summary", "response": response, "applied": applied, "verification": verification}

        patch = _rule_scene_summary_patch(self.store.short_memory(), line_event)
        patches, verification = self._verify_context_patches([patch], line_event)
        applied = self.store.apply_context_patches(patches, source_line=line_event)
        if applied:
            await self._emit(
                VN_EVENT_CONTEXT_UPDATED,
                {
                    "session_id": self.profile.session_id,
                    "source_line": _line_ref(line_event),
                    "applied": applied,
                    "lane": "summary_rules",
                },
            )
        await self._emit(
            VN_EVENT_SUMMARY,
            {
                "session_id": self.profile.session_id,
                "source_line": _line_ref(line_event),
                "scene_summary": self.store.scene_summary(),
            },
        )
        self._last_summary_line_count = self._line_count
        return {"lane": "summary_rules", "applied": applied, "verification": verification}

    async def _maybe_run_retrospective(
        self,
        line_event: dict[str, Any],
        immediate_response: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert self.profile is not None and self.store is not None
        if not self._retrospective_enabled or not self._should_run_retrospective(line_event):
            return None

        pack = self._build_retrospective_pack(line_event, immediate_response)
        self.store.write_context_pack("retrospective", pack)

        bias: dict[str, Any] | None = None
        lane = "retrospective_rules"
        raw: dict[str, Any] | str | None = None
        ok = True
        if self._llm_enabled and self._retrospective_llm_enabled and self.llm is not None:
            messages = retrospective_prompt(self.profile, pack)
            parsed, raw_text = await self.llm.complete_json(messages, lane="retrospective", max_tokens=1100, temperature=0.25)
            raw = parsed or raw_text
            ok = parsed is not None
            self.store.record_model_call(
                "retrospective",
                {"context_metrics": _retrospective_metrics(pack), "context_pack": _compact_retrospective_for_log(pack)},
                raw,
                ok=ok,
            )
            if parsed:
                bias = _normalize_retrospective_bias(parsed, self._line_count, source="llm")
                lane = "retrospective"

        if bias is None:
            bias = _rule_retrospective_bias(pack, self._line_count, self._retrospective_window_lines)

        self.store.save_retrospective_bias(bias)
        self._last_retrospective_line_count = self._line_count
        self.store.record_runtime_event(
            "vn.retrospective",
            {
                "line": _line_ref(line_event),
                "lane": lane,
                "ok": ok,
                "strength": bias.get("strength", 0.0),
                "ttl_lines": bias.get("ttl_lines", 0),
                "attention_bias": bias.get("attention_bias", {}),
                "route_bias": bias.get("route_bias", {}),
            },
        )
        return {"lane": lane, "bias": bias, "raw": raw if not ok else None}

    def _should_run_retrospective(self, line_event: dict[str, Any]) -> bool:
        if self.store is None:
            return False
        if len(self.store.short_memory()) < min(20, self._retrospective_window_lines):
            return False
        if self._last_retrospective_line_count is None:
            return self._line_count >= max(20, min(40, self._retrospective_window_lines // 2))
        lines_since = self._line_count - self._last_retrospective_line_count
        if lines_since >= self._retrospective_every_lines:
            return True
        score, kind = _score_line(line_event.get("text", ""))
        if kind in {"new_evidence", "contradiction", "choice"} and score >= 3.0:
            return lines_since >= max(10, self._retrospective_every_lines // 2)
        return False

    def _build_retrospective_pack(
        self,
        line_event: dict[str, Any],
        immediate_response: dict[str, Any],
    ) -> dict[str, Any]:
        assert self.profile is not None and self.store is not None
        recent_lines = self.store.short_memory()[-self._retrospective_window_lines :]
        return {
            "schema_version": "vn.retrospective_context.v1",
            "session_id": self.profile.session_id,
            "game": {
                "id": self.profile.game_id,
                "title": self.profile.game_title,
                "genre": self.profile.game_genre,
                "prompt_pack": self.profile.prompt_pack,
            },
            "window": {
                "past_lines": len(recent_lines),
                "from_script_id": str((recent_lines[0] if recent_lines else {}).get("script_id") or ""),
                "to_script_id": str((recent_lines[-1] if recent_lines else {}).get("script_id") or ""),
            },
            "current_line": line_event,
            "recent_lines": recent_lines,
            "recent_reactions": _compact_recent_reactions(self.store.recent_reactions(self._retrospective_window_lines)),
            "latest_immediate_response": immediate_response,
            "story_summary_log": self.store.story_summary_log()[-12:],
            "scene_summary": self.store.scene_summary(),
            "characters": self.store.characters(),
            "hypotheses": self.store.hypotheses()[-30:],
            "evidence_nodes": self.store.evidence_nodes()[-30:],
            "verifier_feedback": self.store.verifier_feedback()[-20:],
            "previous_retrospective_bias": self.store.retrospective_bias(),
            "runtime_policy": {
                "output_is_soft_bias": True,
                "do_not_write_facts": True,
                "kurisu_reactions_are_not_game_facts": True,
                "use_recent_lines_for_game_facts_only": True,
                "do_not_spoil_future": True,
            },
        }

    def _rule_immediate(self, line_event: dict[str, Any], lookahead: dict[str, Any], attention: dict[str, Any] | None = None) -> dict[str, Any]:
        response = default_response("silence", reason_label="low_density")
        current_score, kind = _score_line(line_event.get("text", ""))
        attention = attention or {}
        attention_route = attention.get("route") or {}
        attention_kind = str(attention.get("current_kind") or "")
        if attention_route.get("immediate") == "react" and current_score < 2.2 and attention_kind in {"emotional_beat", "contradiction", "choice"}:
            current_score = 2.2
            kind = attention_kind
        target = (lookahead.get("reaction_plan") or [{}])[0] if isinstance(lookahead, dict) else {}
        target_id = str(target.get("target_script_id") or "")
        target_near = bool(target_id and target_id != line_event.get("script_id") and int(target.get("distance_lines") or 99) <= 6)
        stronger_soon = str(target.get("suggested_action") or "") == "hold_until_target" and target_near

        response["importance"] = round(min(current_score / 5, 1.0), 2)
        response["confidence"] = 0.55
        response["line_refs"] = {
            "current_line_id": str(line_event.get("line_id") or ""),
            "script_id": str(line_event.get("script_id") or ""),
            "target_script_id": target_id,
        }
        response["cadence"] = lookahead.get("cadence") or response["cadence"]

        if attention_route.get("immediate") == "hold" or (not attention_route and stronger_soon and current_score < 3):
            response["decision"] = "hold"
            response["reason_label"] = "stronger_beat_soon"
            return response

        if attention_route.get("immediate") == "skip" or current_score < 2.2:
            return response

        response["decision"] = "speak"
        response["reason_label"] = kind
        emotion = "thinking"
        if kind == "emotional_beat":
            emotion = "serious_speaking"
        elif kind == "contradiction":
            emotion = "surprised"
        elif kind == "choice":
            emotion = "thinking"
        response["speak"] = {
            "text": _fallback_speech(kind, self.profile.output_language if self.profile else "zh", line_event.get("text", "")),
            "priority": "high" if current_score >= 4 else "normal",
            "interrupt": False,
            "expires_after_lines": 3,
            "target_line_id": str(line_event.get("line_id") or ""),
            "target_script_id": str(line_event.get("script_id") or ""),
            "emotion_intent": emotion,
        }
        if kind in {"contradiction", "choice", "rule_anomaly"} or current_score >= 3.4:
            response["context_patches"] = [_rule_hypothesis_patch(line_event, kind, current_score)]
        return sanitize_response(response)

    def _postprocess_response(
        self,
        response: dict[str, Any],
        line_event: dict[str, Any],
        *,
        player_requested: bool = False,
    ) -> dict[str, Any]:
        response = sanitize_response(response)
        refs = dict(response.get("line_refs") or {})
        refs["current_line_id"] = str(line_event.get("line_id") or "")
        refs["script_id"] = str(line_event.get("script_id") or "")
        refs.setdefault("target_script_id", refs.get("script_id", ""))
        response["line_refs"] = refs

        speak = response.get("speak")
        if isinstance(speak, dict):
            speak.setdefault("target_line_id", str(line_event.get("line_id") or ""))
            speak.setdefault("target_script_id", str(line_event.get("script_id") or ""))
            speak.setdefault("emotion_intent", "thinking")
            speak["text"] = _normalize_speak_text(str(speak.get("text") or ""))
            if not speak["text"].strip():
                response["decision"] = "silence"
                response["speak"] = None
        return response

    def _apply_silence_pressure_to_attention(
        self,
        attention: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any]:
        pressure = self._silence_pressure_state(attention, line_event)
        if not pressure.get("enabled"):
            return attention

        updated = dict(attention or {})
        route = dict(updated.get("route") or {})
        budget = dict(updated.get("budget") or {})
        lane_focus = dict(updated.get("lane_focus") or {})
        immediate_focus = dict(lane_focus.get("immediate") or {})
        reasons = list(updated.get("reasons") or [])

        updated["silence_pressure"] = pressure
        if pressure.get("action") == "boost_react":
            route["immediate"] = "react"
            budget["speak_cooldown_lines"] = min(int(budget.get("speak_cooldown_lines") or 4), 3)
            immediate_focus["mode"] = "react"
            immediate_focus["goal"] = (
                "continuous silence pressure is active; make one concise reaction if this line is a real clue, "
                "rule, choice, or emotional beat"
            )
            reasons.append("silence pressure boosted a reactable line")

        lane_focus["immediate"] = immediate_focus
        updated["route"] = route
        updated["budget"] = budget
        updated["lane_focus"] = lane_focus
        updated["reasons"] = reasons
        return updated

    def _silence_pressure_state(self, attention: dict[str, Any], line_event: dict[str, Any]) -> dict[str, Any]:
        if not self._silence_pressure_enabled:
            return {"enabled": False}

        route = dict((attention or {}).get("route") or {})
        immediate_route = str(route.get("immediate") or "skip")
        kind = str((attention or {}).get("current_kind") or "")
        density = str((attention or {}).get("density") or "")
        try:
            score = float((attention or {}).get("current_score") or 0.0)
        except Exception:
            score = 0.0

        threshold = self._silence_pressure_opening_start if self._line_count <= self._silence_pressure_opening_lines else self._silence_pressure_start
        force_after = max(threshold + 1, self._silence_pressure_force_after)
        over_threshold = max(0, self._silence_pressure_count - threshold + 1)
        strength = min(1.0, over_threshold / max(1, force_after - threshold))
        candidate_kinds = {"evidence", "rule", "contradiction", "choice", "rule_anomaly", "emotional_beat", "scene_shift"}
        line_text = strip_vn_tags(str(line_event.get("text") or "")).strip()
        candidate = (
            kind in candidate_kinds
            and immediate_route != "hold"
            and not looks_like_topic_label(line_text)
            and (density in {"medium", "high"} or score >= 1.8)
        )
        action = "observe"
        if candidate and strength > 0:
            if immediate_route == "react":
                action = "force_if_model_silent" if self._silence_pressure_count >= threshold else "observe"
            elif immediate_route == "skip" and density != "low" and score + strength * 1.4 >= 3.0:
                action = "boost_react"

        return {
            "enabled": True,
            "silent_lines": self._silence_pressure_count,
            "threshold": threshold,
            "force_after": force_after,
            "opening_window_lines": self._silence_pressure_opening_lines,
            "strength": round(strength, 2),
            "candidate": candidate,
            "action": action,
        }

    def _apply_silence_pressure_guard(
        self,
        response: dict[str, Any],
        attention: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any]:
        if response.get("decision") == "speak" and response.get("speak"):
            return response
        pressure = (attention or {}).get("silence_pressure") or {}
        if not pressure.get("candidate"):
            return response
        if str(((attention or {}).get("route") or {}).get("immediate") or "") != "react":
            return response
        if str(pressure.get("action") or "") not in {"force_if_model_silent", "boost_react"}:
            return response

        kind = str((attention or {}).get("current_kind") or "new_evidence")
        speech = _fallback_speech(kind, self.profile.output_language if self.profile else "zh", line_event.get("text", ""))
        if not speech.strip():
            return response
        try:
            strength = float(pressure.get("strength") or 0.0)
        except Exception:
            strength = 0.0
        forced = default_response("speak", reason_label="silence_pressure")
        forced["importance"] = max(float(response.get("importance") or 0.0), min(1.0, 0.55 + strength * 0.3))
        forced["confidence"] = max(float(response.get("confidence") or 0.0), 0.5)
        forced["line_refs"] = {
            "current_line_id": str(line_event.get("line_id") or ""),
            "script_id": str(line_event.get("script_id") or ""),
            "target_script_id": str(((attention or {}).get("target") or {}).get("script_id") or ""),
        }
        forced["cadence"] = response.get("cadence") or forced["cadence"]
        forced["context_requests"] = response.get("context_requests") or []
        forced["context_patches"] = response.get("context_patches") or []
        forced["ui_cards"] = response.get("ui_cards") or []
        forced["lane_payload"] = {
            "previous_decision": response.get("decision"),
            "previous_reason_label": response.get("reason_label"),
            "silence_pressure": pressure,
        }
        forced["speak"] = {
            "text": speech,
            "priority": "normal",
            "interrupt": False,
            "expires_after_lines": 3,
            "target_line_id": str(line_event.get("line_id") or ""),
            "target_script_id": str(line_event.get("script_id") or ""),
            "emotion_intent": "thinking" if kind != "emotional_beat" else "serious_speaking",
        }
        return sanitize_response(forced)

    def _update_silence_pressure(self, response: dict[str, Any]) -> None:
        if response.get("decision") == "speak" and response.get("speak"):
            self._silence_pressure_count = 0
        else:
            self._silence_pressure_count += 1

    def _apply_attention_guard(
        self,
        response: dict[str, Any],
        attention: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any]:
        route = attention.get("route") or {}
        immediate_route = str(route.get("immediate") or "")
        if response.get("decision") == "speak" and immediate_route == "hold":
            guarded = default_response("hold", reason_label="attention_hold")
            guarded["importance"] = response.get("importance", 0.0)
            guarded["confidence"] = response.get("confidence", 0.0)
            guarded["line_refs"] = {
                "current_line_id": str(line_event.get("line_id") or ""),
                "script_id": str(line_event.get("script_id") or ""),
                "target_script_id": str((attention.get("target") or {}).get("script_id") or ""),
            }
            guarded["context_patches"] = response.get("context_patches") or []
            guarded["lane_payload"] = {"suppressed_decision": "speak", "attention": attention}
            return guarded
        if response.get("decision") == "speak" and immediate_route == "skip":
            try:
                importance = float(response.get("importance") or 0.0)
            except Exception:
                importance = 0.0
            density = str(attention.get("density") or "")
            kind = str(attention.get("current_kind") or "")
            allow_demo_evidence = (
                _env_bool("VN_ALLOW_SKIPPED_EVIDENCE_SPEAK", False)
                and density == "high"
                and kind in {"evidence", "rule"}
                and importance >= 0.55
            )
            if allow_demo_evidence:
                return response
            if importance < 0.65:
                guarded = default_response("silence", reason_label="attention_skip")
                guarded["line_refs"] = {
                    "current_line_id": str(line_event.get("line_id") or ""),
                    "script_id": str(line_event.get("script_id") or ""),
                    "target_script_id": str((attention.get("target") or {}).get("script_id") or ""),
                }
                guarded["context_patches"] = response.get("context_patches") or []
                guarded["lane_payload"] = {"suppressed_decision": "speak", "attention": attention}
                return guarded
        return response

    def _apply_line_budget_guard(
        self,
        response: dict[str, Any],
        attention: dict[str, Any],
        line_event: dict[str, Any],
    ) -> dict[str, Any]:
        if response.get("decision") != "speak":
            return response
        budget = attention.get("budget") or {}
        route = attention.get("route") or {}
        density = str(attention.get("density") or "")
        try:
            cooldown = max(1, int(budget.get("speak_cooldown_lines") or 4))
        except Exception:
            cooldown = 4
        try:
            importance = float(response.get("importance") or 0.0)
        except Exception:
            importance = 0.0
        if (
            self._last_speak_line_count is not None
            and self._line_count - self._last_speak_line_count < cooldown
            and not (density == "high" and importance >= 0.8)
            and route.get("immediate") != "hold"
        ):
            muted = default_response("silence", reason_label="line_cooldown")
            muted["importance"] = importance
            muted["confidence"] = response.get("confidence", 0.0)
            muted["line_refs"] = {
                "current_line_id": str(line_event.get("line_id") or ""),
                "script_id": str(line_event.get("script_id") or ""),
                "target_script_id": str((attention.get("target") or {}).get("script_id") or ""),
            }
            muted["context_patches"] = response.get("context_patches") or []
            muted["lane_payload"] = {
                "suppressed_decision": "speak",
                "cooldown_lines": cooldown,
                "last_speak_line_count": self._last_speak_line_count,
                "attention": attention,
            }
            return muted
        if self.store is not None and _recent_same_speech(self.store.recent_reactions(24), (response.get("speak") or {}).get("text", "")):
            line_text = strip_vn_tags(str(line_event.get("text") or "")).strip()
            attention_kind = str(attention.get("current_kind") or "")
            muted_decision = "hold" if _claim_family(line_text, attention_kind) else "silence"
            muted = default_response(muted_decision, reason_label="repeated_speech")
            muted["importance"] = importance
            muted["confidence"] = response.get("confidence", 0.0)
            muted["line_refs"] = {
                "current_line_id": str(line_event.get("line_id") or ""),
                "script_id": str(line_event.get("script_id") or ""),
                "target_script_id": str((attention.get("target") or {}).get("script_id") or ""),
            }
            muted["context_patches"] = response.get("context_patches") or []
            muted["lane_payload"] = {"suppressed_decision": "speak", "reason": "same speech text in recent reactions"}
            return muted
        if not self._rate_allows_speak():
            muted = default_response("silence", reason_label="rate_limited")
            muted["importance"] = importance
            muted["confidence"] = response.get("confidence", 0.0)
            muted["line_refs"] = {
                "current_line_id": str(line_event.get("line_id") or ""),
                "script_id": str(line_event.get("script_id") or ""),
                "target_script_id": str((attention.get("target") or {}).get("script_id") or ""),
            }
            muted["context_patches"] = response.get("context_patches") or []
            muted["lane_payload"] = {"suppressed_decision": "speak", "reason": "rate_limited"}
            return muted
        self._last_speak_line_count = self._line_count
        return response

    def _rate_allows_speak(self) -> bool:
        assert self.profile is not None
        now = time.monotonic()
        self._recent_speaks = [t for t in self._recent_speaks if now - t < 60.0]
        if len(self._recent_speaks) >= max(1, self.profile.max_reactions_per_minute):
            return False
        self._recent_speaks.append(now)
        return True

    async def _speak(self, speak: dict[str, Any], line_event: dict[str, Any]) -> None:
        payload = dict(speak)
        payload["line"] = _line_ref(line_event)
        payload["session_id"] = self.profile.session_id if self.profile else ""
        if self.profile and self.profile.overlay_url and not payload.get("overlay_url"):
            payload["overlay_url"] = self.profile.overlay_url
        if self.speak_callback is None:
            return
        result = self.speak_callback(payload)
        if inspect.isawaitable(result):
            await result

    async def _maybe_run_reasoner(
        self,
        context_pack: dict[str, Any],
        line_event: dict[str, Any],
        immediate_response: dict[str, Any],
    ) -> dict[str, Any] | None:
        assert self.profile is not None and self.store is not None
        attention = context_pack.get("attention") or {}
        route = attention.get("route") or {}
        budget = attention.get("budget") or {}
        reasoner_route = str(route.get("reasoner") or "skip")
        reasoner_after = max(1, int(budget.get("next_reasoner_after_lines") or self._reasoner_every_lines))
        should_run = (
            reasoner_route in {"run_light", "run_deep"}
            or self._line_count % reasoner_after == 0
            or float(immediate_response.get("importance") or 0.0) >= 0.65
            or bool(immediate_response.get("context_patches"))
        )
        if reasoner_route == "skip" and not bool(immediate_response.get("context_patches")) and self._line_count % reasoner_after != 0:
            should_run = False
        if not should_run:
            return None

        if self._llm_enabled and self._reasoner_llm_enabled and self.llm is not None:
            messages = reasoner_prompt(self.profile, context_pack)
            parsed, raw = await self.llm.complete_json(messages, lane="reasoner", max_tokens=900, temperature=0.25)
            self.store.record_model_call(
                "reasoner",
                {"context_metrics": _context_metrics(context_pack), "context_pack": _compact_context_for_log(context_pack)},
                parsed or raw,
                ok=parsed is not None,
            )
            if parsed:
                response = sanitize_response(parsed)
                patches, verification = self._verify_context_patches(response.get("context_patches") or [], line_event)
                applied = self.store.apply_context_patches(patches, source_line=line_event)
                if applied:
                    await self._emit(
                        VN_EVENT_CONTEXT_UPDATED,
                        {
                            "session_id": self.profile.session_id,
                            "source_line": _line_ref(line_event),
                            "applied": applied,
                            "lane": "reasoner",
                        },
                    )
                return {"lane": "reasoner", "response": response, "applied": applied, "verification": verification}

        score, kind = _score_line(line_event.get("text", ""))
        if score < 1.8:
            return None
        if kind == "topic_label":
            return None
        text = strip_vn_tags(str(line_event.get("text") or "")).strip()
        if kind not in {"new_evidence", "contradiction", "choice", "rule_anomaly"}:
            return None
        if kind != "choice" and not _claim_family(text, kind):
            return None
        if kind == "new_evidence" and score < 3.2 and reasoner_route != "run_deep":
            return None
        patch = _rule_hypothesis_patch(line_event, kind, score)
        patches, verification = self._verify_context_patches([patch], line_event)
        applied = self.store.apply_context_patches(patches, source_line=line_event)
        if applied:
            await self._emit(
                VN_EVENT_CONTEXT_UPDATED,
                {
                    "session_id": self.profile.session_id,
                    "source_line": _line_ref(line_event),
                    "applied": applied,
                    "lane": "reasoner_rules",
                },
            )
        return {"lane": "reasoner_rules", "applied": applied, "verification": verification}

    def _verify_context_patches(
        self,
        patches: list[dict[str, Any]],
        line_event: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if self.verifier is None:
            return list(patches or []), []
        return self.verifier.verify_patches(list(patches or []), source_line=line_event)

    def _resolve_context_requests(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        assert self.store is not None
        layers = set()
        for req in requests:
            if isinstance(req, dict):
                for layer in req.get("layers") or []:
                    layers.add(str(layer))
                if req.get("layer"):
                    layers.add(str(req.get("layer")))
        if not layers:
            layers = {"characters", "hypotheses", "scene_summary", "short_memory"}
        out: dict[str, Any] = {}
        if "characters" in layers or "entities" in layers:
            out["characters"] = self.store.characters()
        if "hypotheses" in layers or "reasoning_graph" in layers:
            out["hypotheses"] = self.store.hypotheses()
        if "scene_summary" in layers or "summary" in layers:
            out["scene_summary"] = self.store.scene_summary()
        if "short_memory" in layers or "recent_lines" in layers:
            out["short_memory"] = self.store.short_memory()
        return out

    def _public_lookahead(self, lookahead: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(lookahead, dict):
            return {}
        safe = {
            "schema_version": lookahead.get("schema_version", "vn.lookahead.v1"),
            "current_script_id": lookahead.get("current_script_id", ""),
            "window": lookahead.get("window", {}),
            "spoiler_policy": "abstract_only",
            "density": lookahead.get("density", {}),
            "reaction_plan": [],
            "cadence": lookahead.get("cadence", {}),
            "source": lookahead.get("source", ""),
        }
        for item in lookahead.get("reaction_plan") or []:
            if not isinstance(item, dict):
                continue
            safe["reaction_plan"].append(
                {
                    "target_script_id": item.get("target_script_id", ""),
                    "kind": item.get("kind", ""),
                    "priority": item.get("priority", ""),
                    "suggested_action": item.get("suggested_action", ""),
                    "spoiler_safe_hint": item.get("spoiler_safe_hint", ""),
                    "context_topics_to_prepare": item.get("context_topics_to_prepare", []),
                    "speak_before_target": bool(item.get("speak_before_target", False)),
                    "distance_lines": item.get("distance_lines"),
                }
            )
        return safe

    async def _emit(self, method: str, params: dict[str, Any]) -> None:
        if self.event_emit is None:
            return
        result = self.event_emit(method, params)
        if inspect.isawaitable(result):
            await result


def _score_line(text: Any) -> tuple[float, str]:
    value = strip_vn_tags(str(text or ""))
    if not value:
        return 0.0, "low_density"
    if looks_like_topic_label(value):
        return 0.1, "topic_label"
    trailing_contrast_setup = _is_trailing_contrast_setup(value)
    score = 0.0
    kind = "low_density"
    if any(keyword in value for keyword in _EVIDENCE_KEYWORDS):
        score += 2.0
        kind = "new_evidence"
    if any(keyword in value for keyword in _MYSTERY_KEYWORDS):
        score += 2.4
        kind = "new_evidence" if kind == "low_density" else kind
    if any(keyword in value for keyword in _AFFECT_KEYWORDS):
        score += 1.4
        if kind == "low_density":
            kind = "emotional_beat"
    if any(keyword in value for keyword in _CHOICE_KEYWORDS):
        score += 2.0
        kind = "choice"
    if "？" in value or "?" in value:
        score += 0.9
    if "！" in value or "!" in value:
        score += 0.7
    if not trailing_contrast_setup and ("但是" in value or "可是" in value or "矛盾" in value or "不对" in value):
        score += 1.2
        kind = "contradiction"
    if "七大不可思议" in value and _has_count_anomaly_text(value):
        score += 2.2
        kind = "rule_anomaly"
    if len(_PUNCT_RE.findall(value)) >= 2:
        score += 0.4
    if len(value) <= 4:
        score *= 0.75
    return round(min(score, 5.0), 2), kind


def _fallback_speech(kind: str, language: str, line_text: Any = "") -> str:
    lang = str(language or "").lower()
    zh = lang.startswith("zh")
    ja = lang.startswith("ja") or "japanese" in lang
    text = strip_vn_tags(str(line_text or ""))
    if ja:
        if "复活" in text and any(marker in text for marker in {"只能", "使用1次", "使用１次", "一回", "一次"}):
            return "ちょっと待って。[EMO preset=thinking dur=8s] 復活を一回だけ使える、なんて前提を軽く出してきたわね。条件と代償を先に疑うべきよ。"
        if "复活秘术" in text or "复活" in text:
            return "復活の秘術、ね。[EMO preset=thinking dur=8s] ただの怪談扱いするには、条件の話が具体的すぎる。"
        if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
            return "呪いの珠はただの小道具じゃないわ。[EMO preset=thinking dur=8s] 誰が条件を握るかが問題になる。"
        if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
            return "条件を満たせば殺せる、なんて危険すぎるわ。[EMO preset=serious_speaking dur=8s] 条件そのものが武器になる。"
        if "确凿的证据" in text:
            return "証拠責任に話を戻したわね。[EMO preset=thinking dur=8s] 雑談じゃない、根拠を吐かせる流れよ。"
        if "咒主" in text or "魂渣" in text:
            return "呪主と魂滓は資源システムっぽいわね。[EMO preset=thinking dur=8s] まずはルール経済として記録するべきよ。"
        if "真货" in text or "实际存在" in text:
            return "「本物」って言い方、引っかかるわね。[EMO preset=thinking dur=8s] 噂を検証可能なルールに押し上げている。"
        if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
            return "視認条件が急に重要になったわ。[EMO preset=thinking dur=8s] 見えるかどうかは、普通の感覚の話じゃないかもしれない。"
        if kind == "rule_anomaly":
            return "七大なのに数が合わない？[EMO preset=thinking dur=8s] そういう命名のズレは、たいてい見落としじゃない。"
        if kind == "choice":
            return "急いで選ばないで。[EMO preset=thinking dur=8s] これは前の条件を覚えているか試している流れに見える。"
        if kind == "emotional_beat":
            return "今の反応、少し不自然ね。[EMO preset=serious_speaking dur=8s] ただの感情じゃなく、後で効く信号かもしれない。"
        if kind == "contradiction":
            return "今の言い方、少し引っかかる。[EMO preset=surprised dur=7s] 決定的じゃないけど、丸をつけておく価値はあるわ。"
        if kind == "new_evidence":
            return "この話は覚えておくべきね。[EMO preset=thinking dur=8s] 背景説明に見せて、ルールか動機の端が混じっている。"
        return "情報密度が上がったわね。[EMO preset=thinking dur=8s] 背景音として流すには早い。"
    if not zh:
        return "Hold on.[EMO preset=thinking dur=8s] That line feels more like a clue than filler."
    if "复活秘术" in text and ("实际存在" in text or "相信" in text):
        return "她把“实际存在”和“我相信”绑在一起了。[EMO preset=thinking dur=8s] 这不是证明，更像规则条件的影子。"
    if "复活秘术" in text:
        return "复活秘术先别当成噱头。[EMO preset=thinking dur=8s] 这里该拆的是条件、代价和谁在撒谎。"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return "诅咒珠不是普通道具。[EMO preset=thinking dur=8s] 现在争的是谁能控制杀人条件。"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return "“满足条件就能杀人”这点太危险了。[EMO preset=serious_speaking dur=8s] 条件本身就是武器。"
    if "确凿的证据" in text:
        return "他把话题推回证据责任了。[EMO preset=thinking dur=8s] 这不是闲聊，是在逼对方暴露根据。"
    if "咒主" in text or "魂渣" in text:
        return "咒主和魂渣像是资源系统。[EMO preset=thinking dur=8s] 先按规则经济来记。"
    if "真货" in text or "实际存在" in text:
        return "她说的是“真货”。[EMO preset=thinking dur=8s] 这不是介绍怪谈，是把传闻推成可验证的规则。"
    if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
        return "视认条件突然变重要了。[EMO preset=thinking dur=8s] 能不能“看见”可能不是普通感官问题。"
    if kind == "rule_anomaly":
        return "名字叫“七大”，数量却对不上。[EMO preset=thinking dur=8s] 这种命名误差通常不是白给的。"
    if kind == "choice":
        return "先别急着选。[EMO preset=thinking dur=8s] 这里像是在测试我们有没有记住前面的条件。"
    if kind == "emotional_beat":
        return "等一下，这里的情绪波动不太自然。[EMO preset=serious_speaking dur=8s] 我会把它先当成一个信号。"
    if kind == "contradiction":
        return "这句话有点别扭。[EMO preset=surprised dur=7s] 不是决定性矛盾，但值得先圈起来。"
    if kind == "new_evidence":
        return "这个说法先记下来。[EMO preset=thinking dur=8s] 它不像闲聊，更像规则或动机的一块边。"
    return "嗯，信息密度突然上来了。[EMO preset=thinking dur=8s] 先别把它当背景音放过去。"


def _rule_hypothesis_patch(line_event: dict[str, Any], kind: str, score: float) -> dict[str, Any]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    claim_map = {
        "new_evidence": "当前台词可能包含规则或证据线索。",
        "emotional_beat": "当前台词可能标记了有后续价值的情绪变化。",
        "contradiction": "当前台词可能和此前叙述框架存在不一致。",
        "choice": "当前台词可能引入了需要保留上下文的选择点。",
        "rule_anomaly": "怪谈名称或数量可能存在有意设计的不一致。",
    }
    claim = _claim_from_line(text, kind, line_event, as_hypothesis=True) or claim_map.get(kind, "当前台词值得稍后回看。")
    family = _claim_family(text, kind)
    return {
        "layer": "hypothesis",
        "target": "hypotheses",
        "action": "upsert",
        "item": {
            "id": f"hyp_{family}" if family else f"hyp_{text_hash(str(line_event.get('script_id') or line_event.get('line_id') or 'line'))}_{kind}",
            "claim": claim,
            "confidence": round(max(0.25, min(score / 5, 0.75)), 2),
            "status": "open",
            "tags": [tag for tag in [kind, family] if tag],
            "evidence_line_ids": [line_event.get("line_id")],
            "evidence_script_ids": [line_event.get("script_id")],
            "affect_anchor": kind if kind == "emotional_beat" else "",
            "dramatic_function": kind,
        },
    }


def _rule_evidence_node_patch(line_event: dict[str, Any], attention: dict[str, Any]) -> dict[str, Any]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    script_id = str(line_event.get("script_id") or "")
    line_id = str(line_event.get("line_id") or "")
    kind = str(attention.get("current_kind") or "evidence")
    claim = _claim_from_line(text, kind, line_event, as_hypothesis=False) or (
        f"当前台词包含 {kind} 线索。" if text else "出现了一条可能和剧情有关的台词。"
    )
    return {
        "layer": "candidate_fact",
        "target": "evidence_nodes",
        "action": "upsert",
        "item": {
            "id": f"ev_{text_hash(script_id or line_id or text)}_{kind}",
            "claim": claim,
            "quote": text,
            "kind": kind,
            "confidence": 0.72,
            "status": "open",
            "tags": [kind, str((attention.get("density") or ""))],
            "evidence_line_ids": [line_id],
            "evidence_script_ids": [script_id],
            "source": "attention_router",
            "router_reasons": list(attention.get("reasons") or [])[:6],
        },
    }


def _claim_from_line(text: str, kind: str, line_event: dict[str, Any], *, as_hypothesis: bool) -> str:
    speaker = str(line_event.get("speaker") or "").strip()
    actor = speaker or "台词"
    prefix = "工作假设：" if as_hypothesis else ""
    if "本所七大不可思议" in text and "真货" in text:
        return prefix + f"{actor}明确说“本所七大不可思议”是“真货”。"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return prefix + "台词把“诅咒珠”的持有/使用/交出作为当前冲突核心。"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return prefix + "台词说明“诅咒珠”可在满足条件时用诅咒杀人。"
    if "不留证据" in text and "诅咒" in text:
        return prefix + "台词说明诅咒杀人可能“不留证据”。"
    if "确凿的证据" in text:
        return prefix + "台词把继续对话的条件转向“确凿的证据”。"
    if "咒主" in text and any(marker in text for marker in {"好几个", "几个", "其他", "同时"}):
        return prefix + "台词暗示存在多个拥有诅咒之力的咒主。"
    if "魂渣" in text:
        return prefix + "台词把“魂渣”呈现为和咒主/复活相关的资源。"
    if "黎明之前" in text:
        return prefix + "台词设置了“黎明之前”的行动期限。"
    if "复活秘术" in text and ("实际存在" in text or "存在" in text):
        return prefix + f"{actor}称“复活秘术”实际存在。"
    if "复活秘术" in text:
        return prefix + "“复活秘术”的条件、代价和真实性仍需要继续追踪。"
    if "能看到我的样子" in text:
        return prefix + "台词询问“能看到我的样子吧”，提示可见性本身可能是规则。"
    if "能够看到" in text:
        return prefix + "台词说兴家君也“能够看到”各种东西，提示可见性本身可能是规则。"
    if "看不见" in text:
        return prefix + "台词说怀疑灵异的人可能“看不见”，提示可见性和相信/怀疑相关。"
    if any(marker in text for marker in {"能看到", "看得见"}):
        return prefix + "台词提到“能看到/看得见”，提示可见性本身可能是规则。"
    if "七大不可思议" in text and _has_count_anomaly_text(text):
        return prefix + "“七大不可思议”的名称与约９个故事的数量不一致。"
    if kind == "contradiction":
        return prefix + "当前台词引入了可能需要回查的前后不一致。"
    if kind == "emotional_beat":
        return prefix + "当前台词标记了可能有后续价值的情绪变化。"
    return ""


def _claim_family(text: str, kind: str) -> str:
    if "本所七大不可思议" in text and "真货" in text:
        return "honjo_seven_mysteries_real"
    if "诅咒珠" in text and any(marker in text for marker in {"交给", "使用", "拥有"}):
        return "curse_bead_control"
    if "满足条件" in text and "诅咒" in text and any(marker in text for marker in {"杀人", "杀死"}):
        return "curse_kill_condition"
    if "不留证据" in text and "诅咒" in text:
        return "curse_no_evidence"
    if "确凿的证据" in text:
        return "proof_requirement"
    if "咒主" in text and any(marker in text for marker in {"好几个", "几个", "其他", "同时"}):
        return "multiple_curse_masters"
    if "魂渣" in text:
        return "soul_dregs_resource"
    if "黎明之前" in text:
        return "dawn_deadline"
    if "复活秘术" in text:
        return "revival_secret_technique"
    if any(marker in text for marker in {"能看到", "能够看到", "看得见", "看不见"}):
        return "spirit_visibility_condition"
    if "七大不可思议" in text and _has_count_anomaly_text(text):
        return "seven_mysteries_count_anomaly"
    if any(marker in text for marker in {"刚才", "知道吧", "说过", "说了"}) and any(marker in text for marker in {"但是", "可是", "对了"}):
        return "memory_continuity_cue"
    return "choice_context" if kind == "choice" else ""


def _is_trailing_contrast_setup(text: str) -> bool:
    compact = re.sub(r"[\s。.!！?？…‥]+$", "", str(text or "").strip())
    return compact.endswith(("但是", "可是")) and len(compact) <= 18


def _has_count_anomaly_text(text: str) -> bool:
    value = strip_vn_tags(str(text or ""))
    return any(
        marker in value
        for marker in {
            "９个",
            "9个",
            "九个",
            "９个故事",
            "9个故事",
            "九个故事",
            "七个故事",
            "７个故事",
            "7个故事",
            "九大不可思议",
            "十五大不可思议",
        }
    )


def _rule_character_mention_patches(line_event: dict[str, Any], attention: dict[str, Any]) -> list[dict[str, Any]]:
    text = strip_vn_tags(str(line_event.get("text") or "")).strip()
    names = _extract_character_mentions(text)
    patches: list[dict[str, Any]] = []
    for name in names[:4]:
        char_id = f"char_{text_hash(name)}"
        patches.append(
            {
                "layer": "candidate_fact",
                "target": "characters",
                "action": "upsert",
                "item": {
                    "id": char_id,
                    "claim": name,
                    "names": [name],
                    "facts": [
                        {
                            "claim": f"{name} is mentioned in the displayed line.",
                            "evidence_line_ids": [line_event.get("line_id")],
                            "evidence_script_ids": [line_event.get("script_id")],
                            "status": "supported",
                        }
                    ],
                    "emotional_readings": [
                        {
                            "line_id": line_event.get("line_id"),
                            "script_id": line_event.get("script_id"),
                            "emotion": str(attention.get("current_kind") or ""),
                            "confidence": 0.55,
                            "affect_anchor": text[:80],
                        }
                    ]
                    if attention.get("current_kind") == "emotional_beat"
                    else [],
                    "evidence_refs": [
                        {"line_id": line_event.get("line_id"), "script_id": line_event.get("script_id")}
                    ],
                    "open_questions": [],
                },
            }
        )
    return patches


def _extract_character_mentions(text: str) -> list[str]:
    names: list[str] = []
    for match in re.finditer(r"([\u4e00-\u9fff]{1,5})(君|小姐|先生|同学|前辈|后辈)", text):
        name = (match.group(1) + match.group(2)).strip()
        if len(name) >= 2 and name not in names:
            names.append(name)
    # Common title card pattern after tag stripping.
    for match in re.finditer(r"(兴家彰吾|福永叶子|兴家君|叶子小姐)", text):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _rule_scene_summary_patch(short_memory: list[dict[str, Any]], line_event: dict[str, Any]) -> dict[str, Any]:
    recent = list(short_memory or [])[-12:]
    salient = []
    active_characters = []
    for item in recent:
        text = strip_vn_tags(str(item.get("text") or "")).strip()
        speaker = str(item.get("speaker") or "").strip()
        if speaker and speaker not in active_characters:
            active_characters.append(speaker)
        score, kind = _score_line(text)
        if score >= 1.8 or len(salient) < 3:
            salient.append(
                {
                    "line_id": item.get("line_id"),
                    "script_id": item.get("script_id"),
                    "kind": kind,
                    "text": text[:120],
                }
            )
    summary_bits = [entry["text"] for entry in salient[-5:] if entry.get("text")]
    summary = " / ".join(summary_bits)
    return {
        "layer": "summary",
        "target": "story_summary_log",
        "action": "append",
        "item": {
            "id": f"story_{line_event.get('line_id')}",
            "summary": summary,
            "important_beats": salient[-8:],
            "active_characters": active_characters[-8:],
            "open_questions": [],
            "evidence_refs": [
                {"line_id": line_event.get("line_id"), "script_id": line_event.get("script_id")}
            ],
            "affect_anchor": "",
            "dramatic_function": "rolling_scene_summary",
        },
    }


def _line_ref(line_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": line_event.get("seq"),
        "line_id": line_event.get("line_id", ""),
        "script_id": line_event.get("script_id", ""),
        "speaker": line_event.get("speaker", ""),
        "text_hash": line_event.get("text_hash", ""),
    }


def _normalize_speak_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    if value.startswith("[EMO"):
        value = "嗯，" + value
    return value


def _generic_lookahead_hint(kind: str, suggested_action: str) -> str:
    action = str(suggested_action or "")
    if action == "hold_until_target":
        return "A stronger beat follows shortly."
    if kind == "new_evidence":
        return "A plot-relevant information beat is in this window."
    if kind == "emotional_beat":
        return "A stronger emotional beat is in this window."
    if kind == "contradiction":
        return "A possible inconsistency is in this window."
    if kind == "choice":
        return "A decision-related beat is in this window."
    return "This looks like connective dialogue unless the current line is unusually salient."


def _safe_context_topics(raw_topics: Any) -> list[str]:
    allowed = {
        "characters",
        "hypotheses",
        "evidence",
        "evidence_map",
        "timeline",
        "reasoning_graph",
        "open_questions",
        "scene_summary",
    }
    topics: list[str] = []
    for topic in list(raw_topics or []):
        value = str(topic or "").strip().lower()
        if value in allowed and value not in topics:
            topics.append(value)
    return topics[:5]


def _compact_context_for_log(context_pack: dict[str, Any]) -> dict[str, Any]:
    out = dict(context_pack)
    if isinstance(out.get("short_memory"), list):
        out["short_memory"] = out["short_memory"][-8:]
    return out


def _context_metrics(context_pack: dict[str, Any]) -> dict[str, Any]:
    attention = context_pack.get("attention") or {}
    budget = attention.get("budget") or {}
    return {
        "production_call_shape": "single_current_line_plus_local_context",
        "current_line_count": 1 if context_pack.get("current_line") else 0,
        "short_memory_lines": len(context_pack.get("short_memory") or []),
        "story_summary_segments": len(context_pack.get("story_summary_log") or []),
        "hypothesis_nodes": len(context_pack.get("hypotheses") or []),
        "evidence_nodes": len(context_pack.get("evidence_nodes") or []),
        "verifier_feedback_items": len(context_pack.get("verifier_feedback") or []),
        "recent_kurisu_speech_items": len(context_pack.get("recent_kurisu_speech") or []),
        "max_context_lines_budget": budget.get("max_context_lines"),
        "attention_density": attention.get("density"),
        "attention_kind": attention.get("current_kind"),
        "attention_route": attention.get("route") or {},
    }


def _lookahead_metrics(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "production_call_shape": "current_line_plus_bounded_future_window_for_planner_only",
        "current_line_count": 1 if context.get("current_line") else 0,
        "future_window_lines": len(context.get("future_window") or []),
        "short_memory_lines": len(context.get("short_memory") or []),
        "future_text_redacted_in_logs": True,
        "immediate_lane_receives_future_text": False,
    }


def _compact_recent_reactions(reactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in reactions or []:
        if not isinstance(item, dict):
            continue
        response = item.get("response") or {}
        speak = response.get("speak") if isinstance(response, dict) else {}
        compact.append(
            {
                "line_id": item.get("line_id"),
                "script_id": item.get("script_id"),
                "decision": response.get("decision") if isinstance(response, dict) else "",
                "reason_label": response.get("reason_label") if isinstance(response, dict) else "",
                "importance": response.get("importance") if isinstance(response, dict) else 0.0,
                "speak_text": str((speak or {}).get("text") or "")[:220] if isinstance(speak, dict) else "",
            }
        )
    return compact


def _recent_kurisu_speech(reactions: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    speeches: list[dict[str, Any]] = []
    for item in reactions or []:
        if not isinstance(item, dict):
            continue
        response = item.get("response") or {}
        if not isinstance(response, dict) or response.get("decision") != "speak":
            continue
        speak = response.get("speak") or {}
        if not isinstance(speak, dict):
            continue
        text = re.sub(r"\[EMO[^\]]*\]", "", str(speak.get("text") or "")).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            continue
        speeches.append(
            {
                "line_id": item.get("line_id"),
                "script_id": item.get("script_id"),
                "text": text,
                "emotion_intent": speak.get("emotion_intent", ""),
                "reason_label": response.get("reason_label", ""),
            }
        )
    return speeches[-max(1, int(limit or 8)) :]


def _recent_same_speech(reactions: list[dict[str, Any]], text: Any) -> bool:
    current = _speech_repeat_key(str(text or ""))
    if not current:
        return False
    for item in reactions or []:
        response = (item or {}).get("response") or {}
        if not isinstance(response, dict) or response.get("decision") != "speak":
            continue
        speak = response.get("speak") or {}
        if isinstance(speak, dict) and _speech_repeat_key(str(speak.get("text") or "")) == current:
            return True
    return False


def _speech_repeat_key(text: str) -> str:
    value = re.sub(r"\[EMO[^\]]*\]", "", str(text or "")).strip()
    value = re.sub(r"\s+", "", value)
    return value[:80]


def _normalize_retrospective_bias(raw: dict[str, Any], line_count: int, *, source: str) -> dict[str, Any]:
    bias = dict(raw or {})
    attention_bias = bias.get("attention_bias") if isinstance(bias.get("attention_bias"), dict) else {}
    route_bias = bias.get("route_bias") if isinstance(bias.get("route_bias"), dict) else {}
    orientation = bias.get("character_orientation") if isinstance(bias.get("character_orientation"), dict) else {}
    ttl = _clamp_int(bias.get("ttl_lines"), 10, 80, 30)
    strength = _clamp_float(bias.get("strength"), 0.0, 1.0, 0.25)
    confidence = _clamp_float(bias.get("confidence"), 0.0, 1.0, 0.5)
    normalized = {
        "schema_version": "vn.retrospective.v1",
        "window": bias.get("window") if isinstance(bias.get("window"), dict) else {},
        "attention_bias": {
            "boost_kinds": _short_str_list(attention_bias.get("boost_kinds"), 6),
            "suppress_kinds": _short_str_list(attention_bias.get("suppress_kinds"), 6),
            "boost_topics": _short_str_list(attention_bias.get("boost_topics"), 8),
            "suppress_topics": _short_str_list(attention_bias.get("suppress_topics"), 8),
            "watch_for": _short_str_list(attention_bias.get("watch_for"), 8),
            "reaction_style": str(attention_bias.get("reaction_style") or "")[:240],
            "summary_debt": _short_str_list(attention_bias.get("summary_debt"), 6),
            "evidence_debt": _short_str_list(attention_bias.get("evidence_debt"), 6),
            "character_debt": _short_str_list(attention_bias.get("character_debt"), 6),
            "reasoning_debt": _short_str_list(attention_bias.get("reasoning_debt"), 6),
        },
        "character_orientation": {
            "working_assumptions": _short_str_list(orientation.get("working_assumptions"), 8),
            "emotional_stance": str(orientation.get("emotional_stance") or "")[:220],
            "uncertainty_style": str(orientation.get("uncertainty_style") or "")[:220],
            "plausible_mistakes": _short_str_list(orientation.get("plausible_mistakes"), 6),
            "next_reaction_bias": str(orientation.get("next_reaction_bias") or "")[:240],
            "avoid_sounding_like": _short_str_list(orientation.get("avoid_sounding_like"), 6),
        },
        "route_bias": {
            "immediate": _choice(route_bias.get("immediate"), {"normal", "quieter", "more_analytical"}, "normal"),
            "summary": _choice(route_bias.get("summary"), {"normal", "event_segments", "repair_summary"}, "normal"),
            "fact_extractor": _choice(route_bias.get("fact_extractor"), {"normal", "split_atomic_facts"}, "normal"),
            "character_modeler": _choice(route_bias.get("character_modeler"), {"normal", "normalize_entities"}, "normal"),
            "reasoner": _choice(route_bias.get("reasoner"), {"normal", "resolve_debts"}, "normal"),
        },
        "strength": strength,
        "ttl_lines": ttl,
        "expires_at_line_count": line_count + ttl,
        "confidence": confidence,
        "notes": _short_str_list(bias.get("notes"), 6),
        "source": source,
        "recorded_at_ms": now_ms(),
    }
    return normalized


def _rule_retrospective_bias(context_pack: dict[str, Any], line_count: int, window_lines: int) -> dict[str, Any]:
    recent_lines = list(context_pack.get("recent_lines") or [])
    recent_reactions = list(context_pack.get("recent_reactions") or [])
    evidence_nodes = list(context_pack.get("evidence_nodes") or [])
    hypotheses = list(context_pack.get("hypotheses") or [])
    chars = (context_pack.get("characters") or {}).get("characters") or []
    text_block = "\n".join(str(item.get("text") or "") for item in recent_lines if isinstance(item, dict))

    repeated_speaks = _repeated_speak_count(recent_reactions)
    quote_like_evidence = [
        str(item.get("claim") or "")[:80]
        for item in evidence_nodes
        if isinstance(item, dict)
        and str(item.get("claim") or "").strip()
        and str(item.get("claim") or "").strip() == str(item.get("quote") or "").strip()
        and len(str(item.get("claim") or "")) >= 18
    ]
    generic_hypotheses = [
        str(item.get("id") or "")
        for item in hypotheses
        if isinstance(item, dict) and str(item.get("claim") or "").startswith("Current line may")
    ]
    bad_names = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        for name in item.get("names") or []:
            value = str(name or "")
            if value.startswith(("我和", "我是", "被", "和")) or len(value) > 8:
                bad_names.append(value)

    boost_topics = _topic_hits(
        text_block,
        ["复活秘术", "本所七大不可思议", "置行堀", "午夜", "叶子", "兴家", "知道", "说过", "但是"],
    )
    watch_for = []
    working_assumptions = []
    plausible_mistakes = []
    emotional_stance = "保持轻微警惕，先把异常当信号而不是结论。"
    uncertainty_style = "用假设口吻；可以怀疑，但不要提前断言真相。"

    if any(topic in boost_topics for topic in ["知道", "说过", "但是"]):
        watch_for.append("角色暗示主角已经知道/说过某事时，优先检查记忆不一致")
        working_assumptions.append("主角的记忆或信息状态可能不稳定，但这仍只是工作假设。")
        plausible_mistakes.append("可能把普通犹豫误读成记忆矛盾。")
    if "复活秘术" in boost_topics:
        watch_for.append("再次出现术式、死亡条件、得到/失去等规则词时提高敏感度")
        working_assumptions.append("复活秘术像规则核心，Kurisu 会自然想拆条件和代价。")
        plausible_mistakes.append("可能过早把术式当成完整规则，而实际仍缺条件。")
    if any(topic in boost_topics for topic in ["本所七大不可思议", "置行堀"]):
        watch_for.append("民俗名词再次出现时，关注它是否从背景知识变成可操作规则")
        working_assumptions.append("民俗传说可能是谜题规则的外壳，而非单纯背景。")
    if "叶子" in boost_topics:
        working_assumptions.append("叶子目前应被视为情绪与动机锚点，不直接视作嫌疑结论。")

    reaction_style = ""
    suppress_kinds = []
    route_immediate = "normal"
    if repeated_speaks >= 2:
        reaction_style = "避免重复泛泛的情绪点评；下一次发声应绑定具体词、规则或矛盾。"
        suppress_kinds.append("emotional_beat")
        route_immediate = "quieter"
    elif quote_like_evidence or generic_hypotheses:
        reaction_style = "更偏分析：把直觉落到具体可检验点上，但保留 Kurisu 的犀利口吻。"
        route_immediate = "more_analytical"

    attention_bias = {
        "boost_kinds": ["evidence", "contradiction"] if quote_like_evidence or generic_hypotheses else [],
        "suppress_kinds": suppress_kinds,
        "boost_topics": boost_topics,
        "suppress_topics": [],
        "watch_for": watch_for,
        "reaction_style": reaction_style,
        "summary_debt": ["summary should describe event-state changes, not paste dialogue"] if _summary_looks_quoted(context_pack) else [],
        "evidence_debt": ["split quote-like evidence into atomic claims"] if quote_like_evidence else [],
        "character_debt": [f"normalize possible bad character names: {', '.join(bad_names[:3])}"] if bad_names else [],
        "reasoning_debt": ["replace generic hypothesis templates with story-specific working assumptions"] if generic_hypotheses else [],
    }
    route_bias = {
        "immediate": route_immediate,
        "summary": "event_segments" if attention_bias["summary_debt"] else "normal",
        "fact_extractor": "split_atomic_facts" if quote_like_evidence else "normal",
        "character_modeler": "normalize_entities" if bad_names else "normal",
        "reasoner": "resolve_debts" if generic_hypotheses or quote_like_evidence else "normal",
    }
    strength = 0.25
    if boost_topics:
        strength += 0.1
    if repeated_speaks >= 2:
        strength += 0.2
    if quote_like_evidence or generic_hypotheses or bad_names:
        strength += 0.2
    raw = {
        "schema_version": "vn.retrospective.v1",
        "window": context_pack.get("window") or {"past_lines": len(recent_lines)},
        "attention_bias": attention_bias,
        "character_orientation": {
            "working_assumptions": working_assumptions[:6],
            "emotional_stance": emotional_stance,
            "uncertainty_style": uncertainty_style,
            "plausible_mistakes": plausible_mistakes[:5],
            "next_reaction_bias": reaction_style or "如果下一段出现新信息，先指出具体可疑点，再给轻微角色化反应。",
            "avoid_sounding_like": ["全知旁白", "证据表朗读", "重复同一句情绪模板"],
        },
        "route_bias": route_bias,
        "strength": min(strength, 0.75),
        "ttl_lines": max(20, min(50, window_lines // 2)),
        "confidence": 0.55,
        "notes": ["rules fallback; no future text inspected"],
    }
    return _normalize_retrospective_bias(raw, line_count, source="rules")


def _retrospective_metrics(context_pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "production_call_shape": "past_displayed_lines_plus_reactions_for_soft_character_orientation",
        "future_window_lines": 0,
        "recent_lines": len(context_pack.get("recent_lines") or []),
        "recent_reactions": len(context_pack.get("recent_reactions") or []),
        "story_summary_segments": len(context_pack.get("story_summary_log") or []),
        "hypothesis_nodes": len(context_pack.get("hypotheses") or []),
        "evidence_nodes": len(context_pack.get("evidence_nodes") or []),
        "verifier_feedback_items": len(context_pack.get("verifier_feedback") or []),
        "output_is_soft_bias": True,
        "kurisu_reactions_are_not_game_facts": True,
    }


def _compact_retrospective_for_log(context_pack: dict[str, Any]) -> dict[str, Any]:
    out = dict(context_pack)
    out["recent_lines"] = list(out.get("recent_lines") or [])[-20:]
    out["recent_reactions"] = list(out.get("recent_reactions") or [])[-20:]
    out["hypotheses"] = list(out.get("hypotheses") or [])[-12:]
    out["evidence_nodes"] = list(out.get("evidence_nodes") or [])[-12:]
    out["verifier_feedback"] = list(out.get("verifier_feedback") or [])[-8:]
    out["story_summary_log"] = list(out.get("story_summary_log") or [])[-6:]
    return out


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _short_str_list(value: Any, limit: int) -> list[str]:
    out: list[str] = []
    for item in list(value or []):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:240])
        if len(out) >= limit:
            break
    return out


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        number = float(value)
    except Exception:
        number = default
    return max(lo, min(hi, number))


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = default
    return max(lo, min(hi, number))


def _repeated_speak_count(reactions: list[dict[str, Any]]) -> int:
    counts: dict[str, int] = {}
    for item in reactions:
        text = str((item or {}).get("speak_text") or "").strip()
        if not text:
            continue
        key = re.sub(r"\[EMO[^\]]*\]", "", text).strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return max(counts.values(), default=0)


def _topic_hits(text: str, topics: list[str]) -> list[str]:
    return [topic for topic in topics if topic and topic in text][:8]


def _summary_looks_quoted(context_pack: dict[str, Any]) -> bool:
    logs = list(context_pack.get("story_summary_log") or [])[-4:]
    if not logs:
        return False
    quoted = 0
    for item in logs:
        summary = str((item or {}).get("summary") or "")
        if " / " in summary or summary.count("……") >= 2:
            quoted += 1
    return quoted >= 2


def _redact_future_text(context: dict[str, Any]) -> dict[str, Any]:
    out = dict(context)
    redacted = []
    for item in out.get("future_window") or []:
        if not isinstance(item, dict):
            continue
        redacted.append(
            {
                "script_id": item.get("script_id", ""),
                "order": item.get("order", 0),
                "text_hash": text_hash(str(item.get("text") or "")),
            }
        )
    out["future_window"] = redacted
    return out
