"""Validated character prompt data and one-pass host template rendering.

Role data is inert text. It is never interpreted as a template or execution
authority. Startup identity and request-bound accepted identity are separate.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import json
import re
from string import Template
from types import MappingProxyType
from typing import Mapping
import tomllib


DEFAULT_CHARACTER_ID = "kurisu"
MAX_CHARACTER_PROMPT_CHARS = 8192
NAME_KEYS = frozenset({"character_id", "short_name", "en_name", "family_first_name",
                       "ja_name", "display_name", "work_title"})
TEXT_KEYS = frozenset({
    "ja_identity", "ja_character_rule", "en_identity", "en_character_rule",
    "ja_delegate_example", "en_delegate_example", "hybrid_ja_keyword_example",
    "hybrid_ja_thinking_example", "hybrid_ja_receipt_example", "llama_preheat",
    "inherited_fallback_ja", "inherited_fallback_en", "cooperative_json_say",
    "cooperative_inline_say", "cooperative_amend_say", "cooperative_app_say",
    "vn_analyst", "vn_voice", "vn_christina", "vn_fallible", "vn_reflection_quality",
    "vn_prior_speech", "vn_retrospective_posture", "vn_tts_tone", "vn_mystery_assumption",
    "vn_mystery_style",
})
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def validate_character_id(value: object) -> str:
    """Validate historic identity without requiring its prompt resource."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid character id")
    return value


@dataclass(frozen=True)
class CharacterPrompts:
    character_id: str
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        values = dict(self.values)
        if values.keys() != NAME_KEYS | TEXT_KEYS:
            raise ValueError("character prompt keys must match the required names and texts")
        for key, value in values.items():
            if not isinstance(value, str) or "\0" in value or len(value) > MAX_CHARACTER_PROMPT_CHARS:
                raise ValueError(f"invalid character prompt value: {key}")
        if values["character_id"] != self.character_id:
            raise ValueError("character id does not match resource identity")
        object.__setattr__(self, "values", MappingProxyType(values))


@lru_cache(maxsize=32)
def load(character_id: str = DEFAULT_CHARACTER_ID) -> CharacterPrompts:
    validate_character_id(character_id)
    document = tomllib.loads(files("characters").joinpath(f"{character_id}.toml").read_text(encoding="utf-8"))
    if document.keys() != {"names", "texts"}:
        raise ValueError("character file requires names and texts tables")
    names, texts = document["names"], document["texts"]
    if not isinstance(names, dict) or not isinstance(texts, dict) or names.keys() != NAME_KEYS or texts.keys() != TEXT_KEYS:
        raise ValueError("invalid character prompt tables")
    return CharacterPrompts(character_id, names | texts)


def active_character_id() -> str:
    """PR-1 keeps the historical identity; startup pinning belongs to PR-2."""
    return DEFAULT_CHARACTER_ID


def active_character() -> CharacterPrompts:
    return load(active_character_id())


def character_identity(character_id: str | None = None) -> dict[str, str]:
    character = load(active_character_id() if character_id is None else character_id)
    return {"character_id": character.character_id, "display_name": character.values["display_name"]}


def text(key: str, *, character_id: str | None = None) -> str:
    return load(active_character_id() if character_id is None else character_id).values[key]


def bindings(*, character_id: str | None = None) -> dict[str, str]:
    values = dict(load(active_character_id() if character_id is None else character_id).values)
    # JSON samples require complete escaped string values, including quotes.
    return values | {f"{key}_json": json.dumps(value, ensure_ascii=False) for key, value in values.items()}


def render(template: str, *, character_id: str | None = None, **extra: str) -> str:
    values = bindings(character_id=character_id)
    if values.keys() & extra.keys():
        raise ValueError("host template inputs cannot replace character slots")
    return Template(template).substitute(values | extra)


def character_override(character_id: str, language: str) -> str:
    """The existing editable Japanese persona belongs only to Kurisu."""
    if character_id != DEFAULT_CHARACTER_ID or language != "ja":
        return ""
    from llm.prompts import _character_prompt_ja
    return _character_prompt_ja
