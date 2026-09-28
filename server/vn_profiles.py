"""User-owned VN launch settings. Runtime presets stay in the launch manager."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from vn_player.schemas import MAX_TERMINOLOGY_LENGTH


def validate_luna_ws_url(value: str) -> None:
    url = urlparse(value)
    if url.scheme not in {"ws", "wss"} or not url.hostname or url.path != "/api/ws/text/origin":
        raise ValueError("Luna original-text WebSocket URL must end in /api/ws/text/origin (ws:// or wss://).")


def inspect_game(executable: str) -> dict[str, str]:
    """Read the manifest belonging to a selected installed game, without guessing an exe."""
    path = Path(executable)
    if not path.is_absolute() or not path.is_file():
        return {}
    for parent in path.parents:
        if parent.name.lower() != "common" or parent.parent.name.lower() != "steamapps":
            continue
        matches = []
        for manifest in parent.parent.glob("appmanifest_*.acf"):
            try:
                fields = dict(re.findall(r'"([^"\r\n]+)"\s*"([^"\r\n]*)"', manifest.read_text(encoding="utf-8")))
                app_id, directory = fields.get("appid", ""), fields.get("installdir", "")
                if (app_id.isascii() and app_id.isdecimal() and directory
                        and Path(directory).name == directory
                        and path.resolve().is_relative_to((parent / directory).resolve())):
                    matches.append({"steamAppId": app_id, "name": fields.get("name", "")})
            except (OSError, UnicodeError):
                continue
        return matches[0] if len(matches) == 1 else {}
    return {}


class LaunchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"game_{uuid.uuid4().hex[:12]}", pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    textSource: Literal["agent", "luna"] = "agent"
    gameExe: str = ""
    hookHelper: str = ""
    scriptPath: str = ""
    lunaWsUrl: str = ""
    launchGame: bool | None = None
    launchMethod: Literal["exe", "steam"] = "exe"
    steamAppId: str = Field(default="", pattern=r"^(?:[1-9][0-9]{0,9})?$")
    launchOverlay: bool = True
    stopWallpaper: bool = True
    closeGameOnStop: bool = False
    # None preserves the preset for profiles saved before companion settings existed.
    promptPack: Literal["base", "mystery"] | None = None
    voiceInput: bool | None = None
    visionMode: Literal["off", "on_question"] = "off"
    commentaryFrequency: Literal["quiet", "balanced", "frequent"] = "balanced"
    terminology: str = Field(default="", max_length=MAX_TERMINOLOGY_LENGTH)

    @field_validator("gameExe", "hookHelper", "scriptPath")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("Choose an absolute file path.")
        return value

    @model_validator(mode="after")
    def source_settings(self) -> "LaunchProfile":
        if self.launchGame is None:
            self.launchGame = self.textSource == "agent"
        if self.textSource == "agent":
            if not self.gameExe or not self.hookHelper:
                raise ValueError("Choose the game executable and its Agent hook script.")
        else:
            validate_luna_ws_url(self.lunaWsUrl)
        if self.launchGame and not self.gameExe:
            raise ValueError("Choose the game executable or start the game manually.")
        if self.launchGame and self.launchMethod == "steam" and not self.steamAppId:
            raise ValueError("Enter the Steam app ID for this game.")
        return self


class ProfileFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    agentExe: str = ""
    profiles: list[LaunchProfile] = Field(default_factory=list)


class VNProfileStore:
    def __init__(self, project_root: Path) -> None:
        self.path = project_root / ".amadeus" / "vn-profiles.json"

    def load(self) -> ProfileFile:
        if not self.path.exists():
            return ProfileFile()
        # Surface corrupt settings; never silently overwrite the user's profiles.
        return ProfileFile.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, profile: LaunchProfile, *, agent_exe: str) -> None:
        data = self.load()
        if agent_exe and not Path(agent_exe).is_absolute():
            raise ValueError("Choose an absolute path to Agent.")
        if profile.textSource == "agent" and not agent_exe:
            raise ValueError("Choose agent.exe. This installation is shared by your game profiles.")
        data.agentExe = agent_exe
        data.profiles = [p for p in data.profiles if p.id != profile.id] + [profile]
        document = data.model_dump()
        # Empty optional terminology keeps saved profiles readable by older builds.
        for item in document["profiles"]:
            if not item["terminology"]:
                item.pop("terminology")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="vn-profiles-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
