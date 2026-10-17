"""Optional Japanese pronunciation data and its local OpenJTalk compilation.

The official CSV is an external asset. Compiled dictionaries are disposable
cache files; neither installation nor compilation modifies personal sources.
"""
from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path
import tempfile

from config.asset_paths import PROJECT_ROOT
from config.japanese_dictionary import (
    PACK_TREE, JapaneseDictionaryError, dictionary_rows, load_dictionary_pack,
)

logger = logging.getLogger(__name__)
_runtime_status = {"state": "not_loaded", "ready": False, "version": "",
                   "detail": "Japanese frontend has not loaded."}


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
