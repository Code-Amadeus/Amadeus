"""Compose common constraints, a selected VN type, and confirmed game settings.

Every game uses this same deterministic path. No game ID selects a legacy prompt
or bypasses composition; the profile supplies values, never generated content.
"""
from string import Template

from ..schemas import VNProfile
from . import base, mystery
from .common import PARTS
from .game import game_bindings, with_terminology


def system_prompt(profile: VNProfile, lane: str) -> str:
    packs = {"base": base.SYSTEMS, "mystery": mystery.SYSTEMS}
    bindings = game_bindings(profile)
    common = {name: Template(text).substitute(bindings) for name, text in PARTS.items()}
    return Template(packs[profile.prompt_pack][lane]).substitute(bindings | common)


def compose_messages(profile: VNProfile, lane: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt(profile, lane)},
        {"role": "user", "content": with_terminology(profile, context)},
    ]
