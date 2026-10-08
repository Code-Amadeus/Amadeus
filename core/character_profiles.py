"""Host-owned local character catalog and atomic name/persona persistence.

Built-in resources are loaded separately and always win their reserved identity.
Disk edits never mutate the character object pinned by the prompt module at boot.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any
import tomllib
import uuid


BUILTIN_CHARACTER_ID = "kurisu"
CHARACTER_LOAD_EXIT_CODE = 78
_WRITE_LOCK = RLock()


class CharacterLoadError(Exception):
    """A selected role could not be loaded; its message contains no role payload."""


class CharacterNotFoundError(CharacterLoadError, FileNotFoundError):
    pass


class CharacterValidationError(CharacterLoadError, ValueError):
    pass


class CharacterReadError(CharacterLoadError, OSError):
    pass


def user_character_directory() -> Path:
    from config import settings

    directory = Path(settings.CHARACTER_DIR).expanduser()
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parents[1] / directory
    return directory


def read_character_document(path: Any) -> dict[str, Any]:
    """Read a Path or packaged resource, returning only sanitized load errors."""
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CharacterNotFoundError("character file not found") from exc
    except UnicodeError as exc:
        raise CharacterValidationError("character file is not valid UTF-8") from exc
    except OSError as exc:
        raise CharacterReadError("character file could not be read") from exc
    try:
        return tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise CharacterValidationError("invalid character TOML") from exc


def _editable(document: dict[str, Any]) -> bool:
    names = document.get("names")
    return (isinstance(names, dict)
        and names.keys() <= {"character_id", "name"}
        and document.keys() <= {"names", "persona"})


def _toml_string(value: str) -> str:
    # JSON and TOML basic strings share escapes; TOML also forbids raw DEL.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")


def _serialize(document: dict[str, Any]) -> str:
    lines = []
    if "persona" in document:
        lines.append("persona = " + _toml_string(document["persona"]))
    for table, values in document.items():
        if table == "persona":
            continue
        lines.extend(["", f"[{table}]"])
        lines.extend(f"{key} = {_toml_string(value)}"
            for key, value in values.items())
    return "\n".join(lines).lstrip("\n") + "\n"


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    """Only a fully validated document reaches this same-directory replace."""
    contents = _serialize(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CharacterStore:
    """Fresh catalog reads and bounded authoring; this store never selects a role."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Path:
        return self._directory if self._directory is not None else user_character_directory()

    def _path(self, character_id: str) -> Path:
        from llm.character_prompts import validate_character_id

        validate_character_id(character_id)
        return self.directory / f"{character_id}.toml"

    def _record(self, character_id: str, document: dict[str, Any] | None = None) -> dict[str, Any]:
        from llm.character_prompts import active_character, load_fresh, parse_character_document

        builtin = character_id == BUILTIN_CHARACTER_ID
        record: dict[str, Any] = {"character_id": character_id, "name": character_id,
            "persona": "", "builtin": builtin, "valid": False, "editable": False, "pending_restart": False}
        try:
            if not builtin:
                document = document if document is not None else read_character_document(self._path(character_id))
                names = document.get("names", {})
                name = names.get("name", names.get("short_name")) if isinstance(names, dict) else None
                if isinstance(name, str):
                    record["name"] = name
                if isinstance(document.get("persona", ""), str):
                    record["persona"] = document.get("persona", "").strip()
                record["editable"] = _editable(document)
            role = (load_fresh(character_id) if builtin else
                parse_character_document(character_id, document))
            active = active_character()
            record.update(name=role.name, persona=role.persona, valid=True,
                pending_restart=character_id == active.character_id and role != active)
        except (CharacterLoadError, ValueError) as exc:
            record["error"] = str(exc)
        if not record["editable"]:
            record["edit_error"] = ("Built-in characters are read-only." if builtin else
                "This character uses advanced fields; edit its TOML file on disk.")
        return record

    def list(self) -> list[dict[str, Any]]:
        from llm.character_prompts import active_character_id

        records = [self._record(BUILTIN_CHARACTER_ID)]
        if self.directory.exists():
            records.extend(self._record(path.stem) for path in sorted(self.directory.glob("*.toml"))
                if path.is_file() and path.stem.casefold() != BUILTIN_CHARACTER_ID)
        active_id = active_character_id()
        if not any(record["character_id"] == active_id for record in records):
            records.append(self._record(active_id))
        return records

    def validate(self, character_id: str) -> dict[str, Any]:
        self._path(character_id)
        return self._record(character_id)

    def create(self, *, name: object, persona: object = "") -> dict[str, Any]:
        from llm.character_prompts import parse_character_document

        with _WRITE_LOCK:
            character_id = "character-" + uuid.uuid4().hex
            path = self._path(character_id)
            if path.exists():
                raise ValueError("generated character identity already exists")
            document = {"persona": persona, "names": {"character_id": character_id, "name": name}}
            role = parse_character_document(character_id, document)
            document["persona"] = role.persona
            _atomic_write(path, document)
            return self._record(character_id)

    def update(self, character_id: str, *, name: object, persona: object = "") -> dict[str, Any]:
        from llm.character_prompts import parse_character_document

        if character_id == BUILTIN_CHARACTER_ID:
            raise ValueError("Built-in characters are read-only.")
        with _WRITE_LOCK:
            path = self._path(character_id)
            document = read_character_document(path)
            if not _editable(document):
                raise ValueError("This character uses advanced fields; edit its TOML file on disk.")
            document = {"persona": persona, "names": {"character_id": character_id, "name": name}}
            role = parse_character_document(character_id, document)
            document["persona"] = role.persona
            _atomic_write(path, document)
            return self._record(character_id)
