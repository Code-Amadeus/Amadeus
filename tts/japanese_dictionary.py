"""Optional Japanese pronunciation data and its local OpenJTalk compilation.

The official CSV is an external asset. Compiled dictionaries are disposable
cache files; neither installation nor compilation modifies personal sources.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import tempfile
from typing import Iterator

from config.asset_paths import PROJECT_ROOT

logger = logging.getLogger(__name__)
PACK_ID = "japanese-pronunciation"
PACK_TREE = "text/ja/english"
PACK_FILES = (
    "userdict.csv", "provenance.json", "NOTICE.md",
    "GPT-SoVITS-MIT.txt", "CMUdict-BSD-2-Clause.txt", "Amadeus-AGPL-3.0.txt",
)
_runtime_status = {"state": "not_loaded", "ready": False, "version": "",
                   "detail": "Japanese frontend has not loaded."}


class JapaneseDictionaryError(ValueError):
    pass


def dictionary_rows(data: bytes) -> Iterator[list[str]]:
    """Check the native 15-column OpenJTalk CSV without importing model extras."""
    try:
        count = 0
        for number, row in enumerate(csv.reader(io.StringIO(data.decode("utf-8-sig")), strict=True), 1):
            if len(row) != 15 or not row[0] or not row[11] or not row[12]:
                raise JapaneseDictionaryError(f"Invalid dictionary entry at line {number}: expected 15 columns and a reading")
            try:
                for value in row[1:4]:
                    if value not in {"", "*"}:
                        int(value)
                accent, mora = map(int, row[13].split("/"))
                if not 0 <= accent <= mora or mora <= 0:
                    raise ValueError
            except ValueError as exc:
                raise JapaneseDictionaryError(f"Invalid dictionary cost/accent at line {number}") from exc
            count += 1
            yield row
        if not count:
            raise JapaneseDictionaryError("Dictionary CSV is empty")
    except (UnicodeError, csv.Error) as exc:
        raise JapaneseDictionaryError(f"Cannot read dictionary CSV: {exc}") from exc


@dataclass(frozen=True)
class JapaneseDictionaryPack:
    root: Path
    version: str
    sha256: str
    entry_count: int

    def files(self) -> tuple[Path, ...]:
        return tuple(self.root / name for name in PACK_FILES)


def load_dictionary_pack(root: Path) -> JapaneseDictionaryPack:
    root = Path(root).resolve()
    try:
        for name in PACK_FILES:
            path = root / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
                raise JapaneseDictionaryError(f"Missing or empty dictionary pack member: {name}")
        raw = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("language") != "ja" or not isinstance(raw.get("version"), str) or not raw["version"].strip():
            raise JapaneseDictionaryError("Dictionary provenance requires a version and Japanese language")
        data = (root / "userdict.csv").read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        count = sum(1 for _ in dictionary_rows(data))
        if raw.get("sha256") != digest or raw.get("entry_count") != count:
            raise JapaneseDictionaryError("Dictionary content does not match its provenance hash/count")
        source = raw.get("source")
        if not isinstance(source, dict) or any(not source.get(key) for key in ("repository", "revision", "path", "sha256")):
            raise JapaneseDictionaryError("Dictionary provenance requires a pinned source and source hash")
        return JapaneseDictionaryPack(root, raw["version"], digest, count)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JapaneseDictionaryError(f"Cannot load dictionary pack: {exc}") from exc


@lru_cache(maxsize=8)
def _sources(project_root: Path) -> tuple[tuple[Path, ...], str]:
    # Resource installation and the enable switch require a backend restart.
    official = project_root / "assets" / PACK_TREE
    legacy = project_root / "GPT_SoVITS/text/ja_userdic/userdict.csv"
    personal = project_root / "assets/text/ja/user.csv"
    paths: list[Path] = []
    version = ""
    if official.exists():
        pack = load_dictionary_pack(official)
        paths.append(official / "userdict.csv")
        version = pack.version
    elif legacy.is_file():
        # Existing installations still use this source. New packs replace its
        # role without copying, editing, or deleting the installation's data.
        paths.append(legacy)
        version = "legacy-local"
    if personal.is_file():
        paths.append(personal)
    return tuple(paths), version


def _enabled() -> bool:
    from config.settings import ENABLE_JAPANESE_USER_DICTIONARY
    return ENABLE_JAPANESE_USER_DICTIONARY


@lru_cache(maxsize=16)
def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dictionary_cache_identity() -> str:
    """Speech cache identity; does not import pyopenjtalk in the host process."""
    if not _enabled():
        return "disabled"
    try:
        paths, _ = _sources(PROJECT_ROOT)
        return ":".join(_digest(path) for path in paths) or "none"
    except (JapaneseDictionaryError, OSError):
        return "invalid"


def dictionary_runtime_status() -> dict:
    return dict(_runtime_status)


def configure_japanese_dictionary(jtalk, *, project_root: Path = PROJECT_ROOT, enabled: bool | None = None) -> dict:
    """Install all selected dictionaries once when the Japanese frontend loads."""
    global _runtime_status
    enabled = _enabled() if enabled is None else enabled
    state = "not_installed"
    version = ""
    detail = "Optional Japanese dictionary is not installed."
    try:
        if not enabled:
            jtalk.unset_user_dict()
            state, detail = "disabled", "Japanese user dictionaries are disabled."
        else:
            paths, version = _sources(Path(project_root).resolve())
            compiled = []
            jtalk.unset_user_dict()
            for source in paths:
                for _ in dictionary_rows(source.read_bytes()):
                    pass
                base = Path(os.fsdecode(jtalk.OPEN_JTALK_DICT_DIR))
                system = base / "sys.dic"
                system_key = (str(base), system.stat().st_size, system.stat().st_mtime_ns)
                key = hashlib.sha256(repr((jtalk.__version__, system_key, _digest(source))).encode()).hexdigest()
                cache = Path(project_root) / ".cache/japanese_dictionary"
                target = cache / f"{key}.dict"
                if not target.is_file():
                    cache.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="compile-", dir=cache) as temporary:
                        output = Path(temporary) / "user.dict"
                        jtalk.mecab_dict_index(str(source), str(output))
                        os.replace(output, target)
                compiled.append(str(target))
            if compiled:
                jtalk.update_global_jtalk_with_user_dict(compiled)
                state, detail = "ready", f"Japanese dictionary {version or 'personal'} loaded ({len(compiled)} source(s))."
                logger.info("[JapaneseDictionary] %s", detail)
    except (JapaneseDictionaryError, OSError, RuntimeError, ValueError) as exc:
        jtalk.unset_user_dict()
        state, detail = "invalid", str(exc)
        logger.warning("[JapaneseDictionary] Optional dictionary unavailable: %s", exc)
    _runtime_status = {"state": state, "ready": state == "ready", "version": version, "detail": detail}
    return dictionary_runtime_status()
