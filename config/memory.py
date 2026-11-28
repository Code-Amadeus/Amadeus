"""Backend memory startup policy; no user identity or path comes from the UI."""
from dataclasses import dataclass
import os
from pathlib import Path

from config.catalog import read_catalog_value
from config.environment import EnvironmentReader
from config.memory_jina import JINA_MODEL_ID

ROOT = Path(__file__).resolve().parents[1]
# One short final read/packing window for candidates retrieved on time.
MEMORY_FINAL_VALIDITY_TIMEOUT_S = .05


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    semantic_enabled: bool = True
    db_path: Path = ROOT / "runtime" / "eidema.sqlite3"
    user_id: str = "local-desktop-user"
    model: str = JINA_MODEL_ID
    model_backend: str = ""
    model_version: str = ""
    semantic_threshold: float | None = None
    # Retrieval window only; final validity and packing use the separate cap above.
    recall_timeout_s: float = .15
    # User-requested lookup has its own retrieval and final publication windows.
    explicit_lookup_timeout_s: float = 2.
    explicit_validity_timeout_s: float = 1.
    # A hard serialized model-reference ceiling, separate from token estimates
    # and from the larger internal candidate pool's codepoint budget.
    context_bytes: int = 4096
    source_runtime_enabled: bool = False
    use_enabled: bool = True
    extraction_enabled: bool = False
    capture_sources: bool = True
    scoped_recall_enabled: bool = True

    @classmethod
    def from_environment(cls, reader: EnvironmentReader):
        use_key = "AMADEUS_MEMORY_USE_ENABLED"
        extraction_key = "AMADEUS_MEMORY_EXTRACTION_ENABLED"
        legacy_sources = reader.boolean("AMADEUS_MEMORY_SOURCE_RUNTIME_ENABLED", False)
        use = read_catalog_value(reader, use_key)
        extract = read_catalog_value(reader, extraction_key)
        values = cls(
            enabled=reader.boolean("AMADEUS_MEMORY_ENABLED", True),
            semantic_enabled=reader.boolean("AMADEUS_MEMORY_SEMANTIC_ENABLED", True),
            db_path=Path(reader.string("AMADEUS_MEMORY_DB_PATH", str(cls.db_path))).expanduser(),
            user_id=reader.string("AMADEUS_MEMORY_USER_ID", "local-desktop-user"),
            model=reader.string("AMADEUS_MEMORY_MODEL", cls.model),
            model_version=reader.string("AMADEUS_MEMORY_MODEL_VERSION", ""),
            model_backend=reader.string("AMADEUS_MEMORY_MODEL_BACKEND", cls.model_backend),
            semantic_threshold=(reader.number("AMADEUS_MEMORY_SEMANTIC_THRESHOLD", 0)
                if reader.configured("AMADEUS_MEMORY_SEMANTIC_THRESHOLD") else None),
            recall_timeout_s=reader.number("AMADEUS_MEMORY_RECALL_TIMEOUT_S", .15),
            explicit_lookup_timeout_s=reader.number("AMADEUS_MEMORY_EXPLICIT_LOOKUP_TIMEOUT_S", 2.),
            explicit_validity_timeout_s=reader.number("AMADEUS_MEMORY_EXPLICIT_VALIDITY_TIMEOUT_S", 1.),
            context_bytes=reader.integer("AMADEUS_MEMORY_CONTEXT_BYTES", 4096),
            source_runtime_enabled=use or extract or legacy_sources,
            use_enabled=use,
            extraction_enabled=extract,
            capture_sources=extract or (legacy_sources and not reader.configured(extraction_key)),
            scoped_recall_enabled=reader.string("AMADEUS_MVP_SCOPED_RECALL", "1") == "1",
        )
        if (not values.user_id or values.model_backend not in ("", "jina-onnx-q4", "e5") or (values.semantic_threshold is not None and not -1 <= values.semantic_threshold <= 1)
                or not .01 <= values.recall_timeout_s <= 5
                or not .01 <= values.explicit_lookup_timeout_s <= 10
                or not .01 <= values.explicit_validity_timeout_s <= 5
                or not 256 <= values.context_bytes <= 16384):
            raise ValueError("invalid_memory_configuration")
        return values


# Standalone Session journal qualification can supply its own owner and grants.
# A bound desktop Host supplies both directly and bypasses these defaults.
def standalone_journal_enabled() -> bool:
    return os.environ.get("AMADEUS_SESSION_JOURNAL_ENABLED") == "1"


def standalone_journal_owner() -> tuple[str, str | None, str | None]:
    return (
        os.environ.get("AMADEUS_SESSION_JOURNAL_USER", ""),
        os.environ.get("AMADEUS_SESSION_JOURNAL_CHARACTER"),
        os.environ.get("AMADEUS_SESSION_JOURNAL_PROJECT") or None,
    )


def standalone_journal_grants() -> dict:
    return dict(
        lookup_allowed=os.environ.get("AMADEUS_JOURNAL_LOOKUP_ALLOWED", "1") == "1",
        ordinary_allowed=os.environ.get("AMADEUS_JOURNAL_ORDINARY_ALLOWED", "1") == "1",
        derived_lookup_allowed=os.environ.get("AMADEUS_JOURNAL_DERIVED_LOOKUP_ALLOWED", "1") == "1",
        formation_allowed=os.environ.get("AMADEUS_JOURNAL_FORMATION_ALLOWED", "1") == "1",
        policy_revision=os.environ.get("AMADEUS_JOURNAL_POLICY_REVISION", "g2-journal-grant-v1"),
    )
