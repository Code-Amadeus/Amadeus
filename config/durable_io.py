"""Same-directory atomic replacement for serialized, user-owned state.

Serialization, locking, exclusive creation and recovery belong to the caller.
Replacement is the commit point: ordinary pre-commit I/O failures preserve the
old file. A directory-sync failure after commit is diagnostic, not a rollback.
This does not make several files transactional or serialize concurrent writers.
Caches, no-overwrite exports and diagnostic branch snapshots keep their own
publication contracts. VN context migration is explicitly deferred.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import tempfile
import time

logger = logging.getLogger(__name__)
_WINDOWS = os.name == "nt"
_RETRY_DELAYS = (0.02, 0.04, 0.08, 0.16, 0.32)


def _replace(source: str, destination: Path) -> None:
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if not _WINDOWS or getattr(exc, "winerror", None) not in (5, 32) or attempt == len(_RETRY_DELAYS):
                raise
            # Synchronous stores also serve event-loop callbacks. Preserve
            # their fail-fast ordering rather than sleeping on that thread or
            # moving stateful Session operations to a concurrent worker.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise
            time.sleep(_RETRY_DELAYS[attempt])


def _sync_directory(directory: Path) -> None:
    if _WINDOWS:
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Publish bytes atomically; the caller creates the destination directory."""
    destination = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, destination)
    finally:
        # After replacement the temporary name no longer exists. Cleanup must
        # not obscure a write failure or report a committed write as failed.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("temporary state file cleanup failed (%s)", type(exc).__name__)
    try:
        _sync_directory(destination.parent)
    except OSError as exc:
        logger.warning("state file replaced; durability unconfirmed (%s)", type(exc).__name__)


def write_text(path: str | os.PathLike[str], text: str) -> None:
    """Publish UTF-8 text without platform newline translation or a BOM."""
    write_bytes(path, text.encode("utf-8"))
