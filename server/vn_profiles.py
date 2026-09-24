"""User-owned VN launch settings. Runtime presets stay in the launch manager."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LaunchProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"game_{uuid.uuid4().hex[:12]}", pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=120)
    textSource: Literal["agent", "luna"] = "agent"
    gameExe: str = ""
    hookHelper: str = ""
    scriptPath: str = ""
    lunaWsUrl: str = ""
    launchGame: bool = True
    launchOverlay: bool = False
    stopWallpaper: bool = True
    closeGameOnStop: bool = False
    # None preserves the preset for profiles saved before companion settings existed.
    promptPack: Literal["base", "mystery"] | None = None
    capabilities: dict[Literal["immediate", "interaction", "summary", "retrospective", "lookahead", "reasoning"], bool] = Field(default_factory=dict)
    voiceInput: bool | None = None

    @field_validator("gameExe", "hookHelper", "scriptPath")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("Choose an absolute file path.")
        return value

    @model_validator(mode="after")
    def source_settings(self) -> "LaunchProfile":
        if self.textSource == "agent":
            if not self.gameExe or not self.hookHelper:
                raise ValueError("Choose the game executable and its Agent hook script.")
        else:
            url = urlsplit(self.lunaWsUrl)
            if url.scheme not in {"ws", "wss"} or not url.hostname:
                raise ValueError("Enter Luna's original-text WebSocket URL.")
            self.launchGame = False
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="vn-profiles-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data.model_dump(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
