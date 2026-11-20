"""File-backed context store for VN Player MVP."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from .schemas import VNProfile, coerce_patch_item, new_id, now_ms
from .text import normalize_for_match, strip_vn_tags, text_hash


def _looks_mojibake(text: str) -> bool:
    stripped = str(text or "").strip()
    if len(stripped) >= 6 and stripped.count("?") / max(1, len(stripped)) >= 0.6:
        return True
    markers = (
        "\ufffd",
        "\u9225",
        "\u93b4",
        "\u935b",
        "\u93b8",
        "\u56e7",
        "\u9287",
        "\u9288",
        "\u00e3",
    )
    return sum(1 for marker in markers if marker in text) >= 2 or "\ufffd" in text


class VNContextStore:
    def __init__(self, project_root: Path, profile: VNProfile) -> None:
        self.project_root = Path(project_root)
        self.profile = profile
        self.root = self.project_root / "sessions" / "vn" / profile.session_id
        self.context_dir = self.root / "context"
        self.context_pack_dir = self.context_dir / "context_packs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.context_pack_dir.mkdir(parents=True, exist_ok=True)
        self._short_memory: deque[dict[str, Any]] = deque(maxlen=profile.short_memory_lines)
        self._seq = 0
        self._load_existing_state()
        self.write_json(self.root / "profile.json", profile.to_dict())

    def _load_existing_state(self) -> None:
        state = self.read_json(self.context_dir / "current_state.json", default={})
        try:
            self._seq = int(state.get("last_seq") or 0)
        except Exception:
            self._seq = 0
        recent = self.read_json(self.context_dir / "short_memory.json", default=[])
        for item in recent[-self.profile.short_memory_lines:]:
            if isinstance(item, dict):
                self._short_memory.append(item)

    @staticmethod
    def read_json(path: Path, *, default: Any) -> Any:
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return default

    @staticmethod
    def write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.replace(path)
        except PermissionError:
            # Some Windows sandboxed runners allow file creation but deny the
            # atomic replace operation. Keep the durable JSON write available
            # for local evaluation while preserving atomic replace elsewhere.
            path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")
            try:
                tmp.unlink()
            except Exception:
                pass

    @staticmethod
    def append_jsonl(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")

    def next_seq(self) -> int:
        self._seq += 1
        self.write_json(
            self.context_dir / "current_state.json",
            {
                "last_seq": self._seq,
                "updated_at_ms": now_ms(),
                "session_id": self.profile.session_id,
            },
        )
        return self._seq

    def record_line(self, params: dict[str, Any], match: dict[str, Any] | None) -> dict[str, Any]:
        seq = self.next_seq()
        matched_line = (match or {}).get("line") or {}
        match_type = str((match or {}).get("match_type") or "")
        captured_text = strip_vn_tags(str(params.get("text") or ""))
        canonical_text = strip_vn_tags(str(matched_line.get("text") or ""))
        text = canonical_text if match_type in {"id", "sequence_after_anchor"} and canonical_text else captured_text
        line_id = str(params.get("line_id") or f"{seq:06d}")
        script_id = str(params.get("script_id") or matched_line.get("script_id") or "")
        speaker = str(params.get("speaker") or matched_line.get("speaker") or "")
        canonical_speaker = str(matched_line.get("speaker") or "")
        if match_type in {"id", "sequence_after_anchor"} and canonical_speaker:
            speaker = canonical_speaker
        elif match_type in {"id", "sequence_after_anchor"} and _looks_mojibake(speaker):
            speaker = ""
        event = {
            "seq": seq,
            "line_id": line_id,
            "script_id": script_id,
            "scene_id": str(params.get("scene_id") or matched_line.get("scene_id") or ""),
            "speaker": speaker,
            "text": text,
            "normalized_text": normalize_for_match(text),
            "text_hash": text_hash(text),
            "text_language": str(params.get("text_language") or matched_line.get("language") or ""),
            "captured_at_ms": int(params.get("captured_at_ms") or now_ms()),
            "recorded_at_ms": now_ms(),
            "match": {
                "type": (match or {}).get("match_type", ""),
                "score": (match or {}).get("score", 0.0),
            },
            "metadata": dict(params.get("metadata") or {}),
        }
        self.append_jsonl(self.root / "raw_lines.jsonl", event)
        self._short_memory.append(event)
        self.write_json(self.context_dir / "short_memory.json", list(self._short_memory))
        self._record_observed_fact(event)
        self._update_character_from_line(event)
        return event

    def short_memory(self) -> list[dict[str, Any]]:
        return list(self._short_memory)

    def scene_summary(self) -> dict[str, Any]:
        return self.read_json(
            self.context_dir / "scene_summary.json",
            default={"summary": "", "important_beats": [], "active_characters": [], "updated_at_ms": 0},
        )

    def story_summary_log(self) -> list[dict[str, Any]]:
        return self.read_json(self.context_dir / "story_summary_log.json", default=[])

    def hypotheses(self) -> list[dict[str, Any]]:
        return self.read_json(self.context_dir / "hypotheses.json", default=[])

    def evidence_nodes(self) -> list[dict[str, Any]]:
        return self.read_json(self.context_dir / "evidence_nodes.json", default=[])

    def verifier_feedback(self) -> list[dict[str, Any]]:
        return self.read_json(self.context_dir / "verifier_feedback.json", default=[])

    def characters(self) -> dict[str, Any]:
        return self.read_json(self.context_dir / "characters.json", default={"characters": []})

    def retrospective_bias(self) -> dict[str, Any]:
        return self.read_json(
            self.context_dir / "retrospective_bias.json",
            default={
                "schema_version": "vn.retrospective.v1",
                "attention_bias": {},
                "strength": 0.0,
                "ttl_lines": 0,
                "source": "empty",
            },
        )

    def save_retrospective_bias(self, bias: dict[str, Any]) -> None:
        payload = dict(bias or {})
        payload.setdefault("schema_version", "vn.retrospective.v1")
        payload.setdefault("recorded_at_ms", now_ms())
        self.write_json(self.context_dir / "retrospective_bias.json", payload)
        self.append_jsonl(self.root / "retrospective_bias.jsonl", payload)

    def write_context_pack(self, lane: str, pack: dict[str, Any]) -> None:
        self.write_json(self.context_pack_dir / f"{lane}.latest.json", pack)

    def record_reaction(self, response: dict[str, Any], line_event: dict[str, Any]) -> None:
        payload = {
            "seq": line_event.get("seq"),
            "line_id": line_event.get("line_id"),
            "script_id": line_event.get("script_id"),
            "recorded_at_ms": now_ms(),
            "response": response,
        }
        self.append_jsonl(self.root / "reactions.jsonl", payload)

    def record_model_call(self, lane: str, request: dict[str, Any], response: dict[str, Any] | str, *, ok: bool) -> None:
        payload = {
            "id": new_id("model_call"),
            "lane": lane,
            "ok": ok,
            "recorded_at_ms": now_ms(),
            "request": request,
            "response": response,
        }
        self.append_jsonl(self.root / "model_calls.jsonl", payload)

    def record_runtime_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.append_jsonl(
            self.root / "runtime_events.jsonl",
            {"type": event_type, "recorded_at_ms": now_ms(), "payload": payload},
        )

    def recent_reactions(self, limit: int = 80) -> list[dict[str, Any]]:
        path = self.root / "reactions.jsonl"
        if not path.is_file():
            return []
        items: list[dict[str, Any]] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        except Exception:
            return []
        return items[-max(1, int(limit or 80)) :]

    def record_verification_feedback(self, feedback: list[dict[str, Any]], *, source_line: dict[str, Any]) -> None:
        payload = {
            "source_seq": source_line.get("seq"),
            "source_line_id": source_line.get("line_id"),
            "source_script_id": source_line.get("script_id"),
            "recorded_at_ms": now_ms(),
            "feedback": feedback,
        }
        self.append_jsonl(self.root / "verification_events.jsonl", payload)
        existing = self.verifier_feedback()
        existing.extend(feedback)
        self.write_json(self.context_dir / "verifier_feedback.json", existing[-40:])

    def get_lines_by_refs(self, *, line_ids: list[str] | None = None, script_ids: list[str] | None = None) -> dict[str, Any]:
        wanted_line_ids = set(line_ids or [])
        wanted_script_ids = set(script_ids or [])
        by_line_id: dict[str, dict[str, Any]] = {}
        by_script_id: dict[str, dict[str, Any]] = {}
        lines: list[dict[str, Any]] = []
        try:
            with (self.root / "raw_lines.jsonl").open("r", encoding="utf-8") as f:
                for raw in f:
                    try:
                        item = json.loads(raw)
                    except Exception:
                        continue
                    line_id = str(item.get("line_id") or "")
                    script_id = str(item.get("script_id") or "")
                    if line_id:
                        by_line_id[line_id] = item
                    if script_id:
                        by_script_id[script_id] = item
                    if (line_id and line_id in wanted_line_ids) or (script_id and script_id in wanted_script_ids):
                        lines.append(item)
        except FileNotFoundError:
            pass
        return {"lines": lines, "by_line_id": by_line_id, "by_script_id": by_script_id}

    def save_lookahead_plan(self, plan: dict[str, Any]) -> None:
        self.write_json(self.context_dir / "lookahead_plan.json", plan)

    def apply_context_patches(self, patches: list[dict[str, Any]], *, source_line: dict[str, Any]) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for patch in patches or []:
            if not isinstance(patch, dict):
                continue
            layer = str(patch.get("layer") or "hypothesis")
            target = str(patch.get("target") or "")
            item = coerce_patch_item(patch.get("item"))
            if not target or not item:
                continue
            if not _patch_item_has_content(target, item):
                self.record_runtime_event("context_patch.rejected", {"reason": "empty_or_generic_item", "patch": patch})
                continue
            if patch.get("_verification_rejected"):
                self.record_runtime_event("context_patch.rejected", {"reason": "verification_rejected", "patch": patch})
                continue
            if layer == "observed_fact":
                # Facts are runtime-owned. Log rejection for audit.
                self.record_runtime_event("context_patch.rejected", {"reason": "observed_fact_runtime_owned", "patch": patch})
                continue
            if target == "hypotheses" or target == "reasoning_graph":
                applied.append(self._upsert_list_file(self.context_dir / "hypotheses.json", item, patch, source_line))
            elif target == "evidence" or target == "evidence_map" or target == "evidence_nodes":
                applied.append(self._upsert_list_file(self.context_dir / "evidence_nodes.json", item, patch, source_line))
            elif target == "entities" or target == "characters":
                applied.append(self._upsert_character(item, patch, source_line))
            elif target == "scene_summary":
                applied.append(self._update_scene_summary(item, patch, source_line))
            elif target == "story_summary" or target == "story_summary_log":
                applied.append(self._append_story_summary(item, patch, source_line))
            else:
                generic_path = self.context_dir / f"{_safe_target_filename(target)}.json"
                applied.append(self._upsert_list_file(generic_path, item, patch, source_line))
        if applied:
            self.append_jsonl(
                self.root / "context_patches.jsonl",
                {"source_seq": source_line.get("seq"), "recorded_at_ms": now_ms(), "applied": applied},
            )
        return applied

    def _upsert_list_file(
        self,
        path: Path,
        item: dict[str, Any],
        patch: dict[str, Any],
        source_line: dict[str, Any],
    ) -> dict[str, Any]:
        data = self.read_json(path, default=[])
        if isinstance(data, dict):
            values = data.get("items") or data.get("hypotheses") or []
        else:
            values = data
        if not isinstance(values, list):
            values = []
        item_id = str(item.get("id") or new_id("ctx"))
        item["id"] = item_id
        item.setdefault("status", "open")
        item.setdefault("confidence", 0.5)
        item.setdefault("evidence_line_ids", [source_line.get("line_id")])
        item["updated_at_ms"] = now_ms()
        item["layer"] = patch.get("layer") or "hypothesis"
        replaced = False
        for idx, existing in enumerate(values):
            if isinstance(existing, dict) and existing.get("id") == item_id:
                history = list(existing.get("revision_history") or [])
                history.append({"at_ms": now_ms(), "previous": existing})
                item["revision_history"] = history[-8:]
                values[idx] = item
                replaced = True
                break
        if not replaced:
            values.append(item)
        self.write_json(path, values)
        return {"target": str(patch.get("target") or path.stem), "item_id": item_id, "action": patch.get("action") or "upsert"}

    def _update_scene_summary(self, item: dict[str, Any], patch: dict[str, Any], source_line: dict[str, Any]) -> dict[str, Any]:
        current = self.scene_summary()
        history = list(current.get("revision_history") or [])
        history.append({"at_ms": now_ms(), "previous": current})
        merged = dict(current)
        merged.update(item)
        merged["updated_at_ms"] = now_ms()
        merged["source_line_id"] = source_line.get("line_id")
        merged["revision_history"] = history[-8:]
        self.write_json(self.context_dir / "scene_summary.json", merged)
        return {"target": "scene_summary", "action": patch.get("action") or "revise"}

    def _append_story_summary(self, item: dict[str, Any], patch: dict[str, Any], source_line: dict[str, Any]) -> dict[str, Any]:
        entries = self.story_summary_log()
        entry_id = str(item.get("id") or new_id("story_sum"))
        entry = dict(item)
        entry["id"] = entry_id
        entry.setdefault("summary", "")
        entry.setdefault("kind", "linear_story_summary")
        entry["source_line_id"] = source_line.get("line_id")
        entry["source_script_id"] = source_line.get("script_id")
        entry["created_at_ms"] = now_ms()
        entries.append(entry)
        self.write_json(self.context_dir / "story_summary_log.json", entries)
        self.append_jsonl(self.root / "story_summary_log.jsonl", entry)
        return {"target": "story_summary_log", "item_id": entry_id, "action": patch.get("action") or "append"}

    def _update_character_from_line(self, event: dict[str, Any]) -> None:
        speaker = str(event.get("speaker") or "").strip()
        if not speaker:
            return
        item = {
            "id": f"char_{text_hash(speaker)}",
            "names": [speaker],
            "first_seen_line_id": event.get("line_id"),
            "first_seen_script_id": event.get("script_id"),
            "facts": [
                {
                    "claim": "Character appeared as the current speaker.",
                    "evidence_line_ids": [event.get("line_id")],
                    "status": "supported",
                }
            ],
        }
        self._upsert_character(item, {"target": "characters", "layer": "candidate_fact"}, event)

    def _upsert_character(self, item: dict[str, Any], patch: dict[str, Any], source_line: dict[str, Any]) -> dict[str, Any]:
        data = self.characters()
        chars = list(data.get("characters") or [])
        names = list(item.get("names") or [])
        if not names and item.get("name"):
            names = [str(item.get("name"))]
        char_id = str(item.get("id") or (f"char_{text_hash(names[0])}" if names else new_id("char")))
        item["id"] = char_id
        item["names"] = names
        item.setdefault("facts", [])
        item.setdefault("relationships", [])
        item.setdefault("traits_observed", [])
        item.setdefault("suspicion_notes", [])
        item.setdefault("emotional_readings", [])
        item.setdefault("evidence_refs", [])
        item.setdefault("open_questions", [])
        item["updated_at_ms"] = now_ms()
        found = False
        for idx, char in enumerate(chars):
            if not isinstance(char, dict):
                continue
            same_id = char.get("id") == char_id
            same_name = bool(set(char.get("names") or []) & set(names))
            if same_id or same_name:
                merged = dict(char)
                for key, value in item.items():
                    if isinstance(value, list):
                        merged[key] = _merge_list(merged.get(key), value)
                    elif value:
                        merged[key] = value
                merged["updated_at_ms"] = now_ms()
                chars[idx] = merged
                found = True
                break
        if not found:
            item.setdefault("created_at_ms", now_ms())
            chars.append(item)
        self.write_json(self.context_dir / "characters.json", {"characters": chars})
        return {"target": "characters", "item_id": char_id, "action": patch.get("action") or "upsert"}

    def _record_observed_fact(self, event: dict[str, Any]) -> None:
        fact = {
            "id": f"obs_{event.get('line_id')}",
            "kind": "displayed_line",
            "claim": "A VN line was displayed.",
            "line_id": event.get("line_id"),
            "script_id": event.get("script_id"),
            "speaker": event.get("speaker"),
            "text": event.get("text"),
            "text_hash": event.get("text_hash"),
            "captured_at_ms": event.get("captured_at_ms"),
            "recorded_at_ms": now_ms(),
            "source": "runtime",
        }
        self.append_jsonl(self.root / "observed_facts.jsonl", fact)


def _merge_list(left: Any, right: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in list(left or []) + list(right or []):
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _safe_target_filename(target: str) -> str:
    value = "".join(ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in str(target or "context"))
    value = value.strip("._") or "context"
    return value[:80]


def _patch_item_has_content(target: str, item: dict[str, Any]) -> bool:
    if not isinstance(item, dict) or not item:
        return False
    generic_values = {"", "none", "null", "llm proposed a context update."}
    target_value = str(target or "").strip()
    content_keys = {
        "claim",
        "statement",
        "summary",
        "label",
        "text",
        "question",
        "notes",
        "quote",
        "name",
        "affect_anchor",
        "dramatic_function",
    }
    for key in content_keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip().lower() not in generic_values:
            return True
    if isinstance(item.get("names"), list) and any(str(name or "").strip() for name in item.get("names") or []):
        return True
    if target_value in {"characters", "entities"}:
        nested = item.get("characters") or item.get("entities")
        if isinstance(nested, dict) and nested:
            return True
        if isinstance(nested, list) and nested:
            return True
    if target_value in {"timeline", "reasoning_graph"}:
        if isinstance(item.get("events"), list) and item.get("events"):
            return True
        if isinstance(item.get("nodes"), list) and item.get("nodes"):
            return True
        if isinstance(item.get("edges"), list) and item.get("edges"):
            return True
    return False
