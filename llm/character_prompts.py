"""Validated character prompt data and one-pass host template rendering.

Role data is inert text. It is never interpreted as a template or execution
authority. Startup identity and request-bound accepted identity are separate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
import json
import re
from string import Template
from types import MappingProxyType
from typing import Mapping
from pathlib import Path

from core.character_profiles import (
    CharacterLoadError, CharacterValidationError, read_character_document,
    user_character_directory,
)

from llm.character_voice_lines import validate_voice_lines


DEFAULT_CHARACTER_ID = "kurisu"
MAX_CHARACTER_PROMPT_CHARS = 8192
NAME_KEYS = frozenset({"character_id", "short_name", "en_name", "family_first_name",
                       "ja_name", "display_name", "work_title"})
TEXT_KEYS = frozenset({
    "ja_identity", "ja_character_rule", "en_identity", "en_character_rule",
    "ja_delegate_example", "en_delegate_example", "hybrid_ja_keyword_example",
    "hybrid_ja_thinking_example", "hybrid_ja_receipt_example",
    "inherited_fallback_ja", "inherited_fallback_en", "cooperative_json_say",
    "cooperative_inline_say", "cooperative_amend_say", "cooperative_app_say",
    "vn_analyst", "vn_voice", "vn_christina", "vn_fallible", "vn_reflection_quality",
    "vn_prior_speech", "vn_retrospective_posture", "vn_tts_tone", "vn_mystery_assumption",
    "vn_mystery_style", "vn_no_orientation",
})
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
# Character ids cannot impersonate the finite Host participant/verification namespace.
_HOST_IDENTITIES = frozenset({"user", "app", "system", "application", "unknown"})


def validate_character_id(value: object) -> str:
    """Validate historic identity without requiring its prompt resource."""
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in _HOST_IDENTITIES:
        raise ValueError("invalid character id")
    return value


@dataclass(frozen=True)
class CharacterPrompts:
    character_id: str
    values: Mapping[str, str]
    # Resolved role commentary only. Fixed Host fact wording stays in its catalog.
    voice_lines: Mapping[str, str] = field(default_factory=dict)
    # UI labels never enter prompt bindings or the model-visible name schema.
    name: str = ""
    persona: str = ""
    ui_names: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_character_id(self.character_id)
        values = dict(self.values)
        if values.keys() != NAME_KEYS | TEXT_KEYS:
            raise ValueError("character prompt keys must match the required names and texts")
        for key, value in values.items():
            if not isinstance(value, str) or "\0" in value or len(value) > MAX_CHARACTER_PROMPT_CHARS:
                raise ValueError(f"invalid character prompt value: {key}")
            value.encode("utf-8", errors="strict")
        if values["character_id"] != self.character_id:
            raise ValueError("character id does not match resource identity")
        if not values["display_name"] or values["display_name"] != values["display_name"].strip():
            raise ValueError("character display_name must be nonempty with no surrounding whitespace")
        name = self.name or values["short_name"]
        _validate_role_text(name, "name", nonempty=True)
        _validate_role_text(self.persona, "persona")
        if not self.ui_names.keys() <= {"ui_name", "accessible_name"}:
            raise ValueError("invalid character UI labels")
        ui_names = {"ui_name": values["display_name"], "accessible_name": values["display_name"]}
        ui_names.update(self.ui_names)
        for value in ui_names.values():
            _validate_role_text(value, "UI label", nonempty=True)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "ui_names", MappingProxyType(ui_names))
        object.__setattr__(self, "values", MappingProxyType(values))
        object.__setattr__(self, "voice_lines", validate_voice_lines(
            self.voice_lines, max_chars=MAX_CHARACTER_PROMPT_CHARS))


def _validate_role_text(value: object, field_name: str, *, nonempty: bool = False) -> str:
    if (not isinstance(value, str) or "\0" in value or len(value) > MAX_CHARACTER_PROMPT_CHARS
            or (nonempty and (not value or value != value.strip()))):
        raise ValueError(f"invalid character {field_name}")
    value.encode("utf-8", errors="strict")
    return value


def parse_character_document(character_id: str, document: Mapping[str, object]) -> CharacterPrompts:
    """Expand inert persona text once, within the existing identity slots only."""
    validate_character_id(character_id)
    if not {"names"} <= document.keys() <= {"names", "texts", "voice_lines", "persona", "ui"}:
        raise ValueError("character file requires names and optional texts/voice_lines/persona/ui")
    names, texts, ui = document["names"], document.get("texts", {}), document.get("ui", {})
    if (not isinstance(names, dict) or not isinstance(texts, dict) or not isinstance(ui, dict)
            or not names.keys() <= NAME_KEYS | {"name"} or not texts.keys() <= TEXT_KEYS
            or not ui.keys() <= {"ui_name", "accessible_name"}):
        raise ValueError("invalid character prompt tables")
    primary = names.get("name", names.get("short_name"))
    _validate_role_text(primary, "primary name", nonempty=True)
    if names.get("character_id") != character_id:
        raise ValueError("character file requires its stable id and a nonempty primary name")
    persona = _validate_role_text(document.get("persona", ""), "persona")
    if persona and texts:
        raise ValueError("character persona cannot be combined with explicit texts")
    resolved = {key: primary for key in NAME_KEYS - {"character_id", "work_title"}}
    resolved.update(character_id=character_id, work_title="")
    resolved.update({key: value for key, value in names.items() if key != "name"})
    for key in NAME_KEYS - {"character_id", "work_title"}:
        if not isinstance(resolved[key], str) or not resolved[key].strip():
            raise ValueError(f"invalid character name: {key}")
    neutral = _neutral_texts(resolved)
    if persona:
        for key in ("ja_identity", "en_identity"):
            neutral[key] += persona + "\n\n"
    return CharacterPrompts(character_id, resolved | neutral | texts,
        document.get("voice_lines", {}), name=primary, persona=persona, ui_names=ui)


def load_fresh(character_id: str = DEFAULT_CHARACTER_ID, *, directory: Path | None = None) -> CharacterPrompts:
    """Read the current disk role; built-in identity cannot be shadowed locally."""
    try:
        validate_character_id(character_id)
        path = (files("characters").joinpath(f"{character_id}.toml")
            if character_id == DEFAULT_CHARACTER_ID else
            (Path(directory) if directory is not None else user_character_directory()) / f"{character_id}.toml")
        return parse_character_document(character_id, read_character_document(path))
    except CharacterLoadError:
        raise
    except UnicodeError as exc:
        raise CharacterValidationError("character values are not valid UTF-8") from exc
    except ValueError as exc:
        raise CharacterValidationError(str(exc)) from exc


@lru_cache(maxsize=32)
def load(character_id: str = DEFAULT_CHARACTER_ID) -> CharacterPrompts:
    return load_fresh(character_id)


def _neutral_texts(names: Mapping[str, str]) -> dict[str, str]:
    """Name-only roles rely on the model's established character knowledge."""
    name = names["short_name"]
    return {
        "ja_identity": f"あなたは{names['ja_name']}。その一貫した口調と性格で話してください。\n\n",
        "ja_character_rule": f"{names['ja_name']}として一貫した口調と性格で自然に話してください。\n",
        "en_identity": f"You are {names['en_name']}. Speak with the character's established voice and personality.\n\n",
        "en_character_rule": f"2) Maintain {names['en_name']}'s established personality consistently.\n",
        "ja_delegate_example": "（例:「確認します」「少々お待ちください」）。",
        "en_delegate_example": "(e.g., 'Let me check.', 'One moment.').",
        "hybrid_ja_keyword_example": "      形式例: 「〇〇ですね。確認します。」\n",
        "hybrid_ja_thinking_example": "      「順番に考えてみます。」\n",
        "hybrid_ja_receipt_example": "      「〇〇についてですね。承りました。」（〇〇はユーザーの発言から抽出）\n",
        "inherited_fallback_ja": f"あなたは{names['ja_name']}。メインチャットと同じ人格と言語を保ち、自然かつ簡潔に日本語で答えてください。",
        "inherited_fallback_en": f"You are {names['en_name']}. Keep the same language and personality as the main chat. Answer naturally and concisely.",
        "cooperative_json_say": "作成を始めます。",
        "cooperative_inline_say": "はい、作成します。",
        "cooperative_amend_say": "指定された変更と、もう一方の状態確認を承りました。",
        "cooperative_app_say": "アプリを作成し、完成後に開いて一緒に試します。",
        "vn_analyst": f"You are a companion analyst who stays in character as {name}, emotionally present when the story deserves it.",
        "vn_voice": f"If speaking, stay in {name}'s voice.",
        "vn_christina": f"Stay consistent with {name}'s established personality and reactions.",
        "vn_fallible": f"{name} may be wrong. Bias should make {name}'s errors plausible, bounded, and logically motivated by displayed text.",
        "vn_reflection_quality": f"Prefer character-useful guidance over recap. A good output helps {name} produce better reactions to text not yet seen.",
        "vn_prior_speech": f"Recent player dialogue is history, not an unanswered request. {name}'s prior speech is only {name}'s own commentary, not game fact.",
        "vn_retrospective_posture": f"This is not a local summary lane. Your main job is to shape the character's next interpretive posture: what {name} is suspicious about, what {name} is emotionally carrying, what {name} may reasonably misunderstand, and what kind of future lines deserve attention.",
        "vn_tts_tone": f"Keep {name}'s tone.",
        "vn_mystery_assumption": "复活秘术像规则核心，其条件和代价仍需依据已显示文本确认。",
        "vn_mystery_style": f"更偏分析：把直觉落到具体可检验点上，但保留 {name} 的口吻。",
        "vn_no_orientation": "No retrospective orientation yet. Avoid overclaiming.",
    }


def active_character_id() -> str:
    """The selected identity is pinned before prompt modules can import it."""
    return _ACTIVE_CHARACTER.character_id


def active_character() -> CharacterPrompts:
    return _ACTIVE_CHARACTER


def _character(character_id: str | None) -> CharacterPrompts:
    if character_id is None or character_id == active_character_id():
        return _ACTIVE_CHARACTER
    return load(character_id)


def character_identity(character_id: str | None = None) -> dict[str, str]:
    character = _character(character_id)
    return {"character_id": character.character_id, "display_name": character.values["display_name"]}


def active_ui_identity() -> dict[str, str]:
    """The desktop identity is a projection of the immutable startup role."""
    character = active_character()
    return {"character_id": character.character_id, "name": character.name,
        "display_name": character.values["display_name"], "short_name": character.values["short_name"],
        **character.ui_names}


def text(key: str, *, character_id: str | None = None) -> str:
    character = _character(character_id)
    return character.values[key]


def bindings(*, character_id: str | None = None) -> dict[str, str]:
    character = _character(character_id)
    values = dict(character.values)
    # JSON samples require complete escaped string values, including quotes.
    examples = "/".join(value for value in (
        values["ja_name"], values["short_name"], values["work_title"]) if value)
    return values | {"role_identity_examples": examples} | {
        f"{key}_json": json.dumps(value, ensure_ascii=False) for key, value in values.items()}


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


# Parse configuration through its existing environment precedence. There is no
# setter: a new selection or edited resource requires a new process.
from config import settings as _settings

_ACTIVE_CHARACTER = load(_settings.CHARACTER_ID)
