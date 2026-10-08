"""Character catalog authoring without runtime selection or prompt mutation."""
from __future__ import annotations

from typing import Any

from core.character_profiles import CharacterStore
from llm.character_prompts import (
    MAX_CHARACTER_NAME_CHARS, MAX_CHARACTER_PERSONA_CHARS, active_ui_identity,
)
from server.protocol import Method
from server.ws_handler import RequestHandler


class CharacterHandler(RequestHandler):
    methods = [Method.CHARACTER_LIST, Method.CHARACTER_ACTIVE, Method.CHARACTER_CREATE,
        Method.CHARACTER_UPDATE, Method.CHARACTER_VALIDATE]

    def __init__(self, store: CharacterStore | None = None) -> None:
        self.store = store if store is not None else CharacterStore()

    async def handle(self, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if method == Method.CHARACTER_LIST:
            return {"characters": self.store.list(), "active": active_ui_identity(),
                "limits": {"name_max_chars": MAX_CHARACTER_NAME_CHARS,
                    "persona_max_chars": MAX_CHARACTER_PERSONA_CHARS}}
        if method == Method.CHARACTER_ACTIVE:
            return active_ui_identity()
        if method == Method.CHARACTER_CREATE:
            return {"character": self.store.create(name=params.get("name"), persona=params.get("persona", ""))}
        if method == Method.CHARACTER_UPDATE:
            return {"character": self.store.update(params.get("character_id"),
                name=params.get("name"), persona=params.get("persona", ""))}
        if method == Method.CHARACTER_VALIDATE:
            return {"character": self.store.validate(params.get("character_id"))}
        return None
