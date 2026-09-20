"""Startup reaper for Session-owned Continuity state without any transcript.

Session-isolation rule: a Session scope keeps its memories, tombstones, topic
mutes, relationship/affect state and consolidation journal only while its
transcript exists either live (``sessions/<id>.json``) or in the backup mirror
(``runtime/backup/sessions/<id>.json``).  If both are gone, the next server
start clears that scope entirely: a dialogue with no backup cannot leave
durable state behind.  ``__unsessioned__`` (voice turns without a session) and
the inert legacy ``global`` placeholder are exempt.

The reaper is fail-open: it must never block startup, and the startup backup
runs first so a snapshot of the pre-reap state always exists.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from core.continuity.store import ContinuityStore
from core.continuity.turn_ingest import session_path

logger = logging.getLogger(__name__)

_EXEMPT_SCOPES = {"global", "__unsessioned__"}
_OVERRIDE_ENV_KEYS = (
    "AMADEUS_SESSION_DIR",
    "AMADEUS_CONTINUITY_DB_PATH",
    "AMADEUS_WORK_LEDGER_PATH",
)


def reap_session_state(
    *,
    continuity_db: str | os.PathLike[str],
    sessions_dir: str | os.PathLike[str],
    backup_sessions_dir: str | os.PathLike[str],
) -> dict[str, dict[str, int]]:
    """Purge every scope that has neither a live nor a backed-up transcript."""

    db_path = Path(continuity_db)
    if not db_path.is_file():
        return {}
    store = ContinuityStore(db_path)
    purged: dict[str, dict[str, int]] = {}
    try:
        for scope in store.list_scopes():
            if scope in _EXEMPT_SCOPES:
                continue
            live = session_path(sessions_dir, scope)
            backup = session_path(backup_sessions_dir, scope)
            if live.is_file() or backup.is_file():
                continue
            counts = store.purge_scope(scope)
            purged[scope] = counts
            logger.info("reaped orphaned session state: %s %s", scope, counts)
    finally:
        store.close()
    return purged


def run_startup_reap(project_root: str | os.PathLike[str]) -> dict[str, dict[str, int]]:
    """Reap orphaned scopes for the default local state layout."""

    root = Path(project_root)
    if any(str(os.environ.get(key) or "").strip() for key in _OVERRIDE_ENV_KEYS):
        logger.debug("startup session reap skipped: state paths are overridden by environment")
        return {}
    try:
        return reap_session_state(
            continuity_db=root / "runtime" / "continuity.sqlite3",
            sessions_dir=root / "sessions",
            backup_sessions_dir=root / "runtime" / "backup" / "sessions",
        )
    except Exception:
        logger.exception("startup session reap failed; continuing")
        return {}


__all__ = ["reap_session_state", "run_startup_reap"]
