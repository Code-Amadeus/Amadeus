"""Optional Japanese data loading; source CSVs are never modified."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import tempfile

from config.asset_paths import PROJECT_ROOT
from config.japanese_dictionary import PACK_TREE, dictionary_rows, load_dictionary_pack

logger = logging.getLogger(__name__)
_runtime_status = {"state": "not_loaded", "ready": False, "version": "", "identity": None,
                   "detail": "Japanese frontend has not loaded."}


def _enabled() -> bool:
    from config.settings import ENABLE_JAPANESE_USER_DICTIONARY
    return ENABLE_JAPANESE_USER_DICTIONARY


def dictionary_runtime_status() -> dict:
    return dict(_runtime_status)


def configure_japanese_dictionary(jtalk, *, project_root: Path = PROJECT_ROOT, enabled: bool | None = None) -> dict:
    """Load independent sources without making optional failures fatal."""
    global _runtime_status
    state, version, identity = "invalid", "", None
    detail = "Optional Japanese dictionary unavailable."
    reset = None
    try:
        enabled = _enabled() if enabled is None else enabled
        reset = getattr(jtalk, "unset_user_dict", None)
        if not callable(reset):
            state = "unsupported"
            raise RuntimeError("Japanese user dictionaries require the pinned pyopenjtalk-plus API (unset_user_dict)")
        reset()
        project_root = Path(project_root).resolve()
        base = Path(os.fsdecode(jtalk.OPEN_JTALK_DICT_DIR))
        system = (base / "sys.dic").stat()
        native_key = (jtalk.__version__, str(base), system.st_size, system.st_mtime_ns)
        compiled, digests, issues = [], [], []
        cache = project_root / ".cache/japanese_dictionary"
        official = project_root / "assets" / PACK_TREE / "userdict.csv"
        legacy = project_root / "GPT_SoVITS/text/ja_userdic/userdict.csv"
        personal = project_root / "assets/text/ja/user.csv"
        sources = []
        if enabled:
            if official.parent.exists():
                sources.append(official)
            elif legacy.is_file():
                sources.append(legacy)
            if personal.is_file():
                sources.append(personal)
        for source in sources:
            label = source.relative_to(project_root).as_posix()
            try:
                pack = load_dictionary_pack(source.parent) if source == official else None
                data = source.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if pack is not None:
                    if digest != pack.sha256:
                        raise ValueError("Dictionary changed during loading; restart after installation finishes")
                else:
                    for _ in dictionary_rows(data):
                        pass
                key = hashlib.sha256(repr((native_key, digest)).encode()).hexdigest()
                target = cache / f"{key}.dict"
                if not target.is_file():
                    cache.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="compile-", dir=cache) as temporary:
                        snapshot = Path(temporary) / "source.csv"
                        snapshot.write_bytes(data)
                        output = Path(temporary) / "user.dict"
                        jtalk.mecab_dict_index(str(snapshot), str(output))
                        try:
                            os.replace(output, target)
                        except OSError:
                            # A peer may already have published and mmap'ed the
                            # same content key on Windows. Reuse its file.
                            if not target.is_file():
                                raise
            except Exception as exc:
                issues.append(f"{label}: {exc}")
                logger.warning("[JapaneseDictionary] Skipped %s: %s", label, exc)
                continue
            try:
                jtalk.update_global_jtalk_with_user_dict([*compiled, str(target)])
            except Exception as exc:
                # Recovery failure escapes to the outer optional boundary:
                # never claim an identity for an unknown native selection.
                if compiled:
                    jtalk.update_global_jtalk_with_user_dict(compiled)
                else:
                    reset()
                issues.append(f"{label}: {exc}")
                logger.warning("[JapaneseDictionary] Skipped %s: %s", label, exc)
                continue
            compiled.append(str(target))
            digests.append(digest)
            if pack is not None:
                version = pack.version
            elif source == legacy:
                version = "legacy-local"
        identity = hashlib.sha256(repr((native_key, tuple(digests))).encode()).hexdigest()
        if compiled:
            state = "ready"
            detail = f"Japanese dictionary {version or 'personal'} loaded ({len(compiled)} source(s))."
            # Windows can retain files mmap'ed by another live process. Their
            # cleanup is retried on a later successful startup.
            for old in cache.glob("*.dict"):
                if str(old) not in compiled and len(old.stem) == 64 and all(c in "0123456789abcdef" for c in old.stem):
                    try:
                        old.unlink()
                    except OSError as exc:
                        logger.debug("[JapaneseDictionary] Cache still in use: %s (%s)", old.name, exc)
        elif not enabled:
            state, detail = "disabled", "Japanese user dictionaries are disabled; using the base dictionary."
        elif issues:
            state, detail = "invalid", "Using the base Japanese dictionary."
        else:
            state, detail = "not_installed", "Optional Japanese dictionary is not installed; using the base dictionary."
        if issues:
            detail += " Skipped " + "; ".join(issues)
    except Exception as exc:
        # Configuration, native API and reset errors belong to this optional
        # boundary. An unsupported runtime must still import the base frontend.
        identity = None
        state = "unsupported" if state == "unsupported" else "invalid"
        detail = str(exc)
        if callable(reset):
            try:
                reset()
            except Exception as cleanup:
                logger.warning("[JapaneseDictionary] Native reset failed: %s", cleanup)
        logger.warning("[JapaneseDictionary] Optional dictionary unavailable: %s", exc)
    _runtime_status = {"state": state, "ready": state == "ready", "version": version,
                       "identity": identity, "detail": detail}
    return dictionary_runtime_status()
