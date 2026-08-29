"""Where work that belongs to no known project goes.

An instruction like "build me a chess game" names no project, and until now the
host inferred a destination for it: the intake fell through to ``Path.cwd()``,
which is the server's launch directory, which is the user's own repository. The
quarantined artifacts under ``.tmp/provider_artifacts/`` are what that looked
like after the fact.

This module gives that work a real destination instead of a guessed one. The
scratch root is a container, not a repository; each such task gets its **own**
git repository underneath it. Per-task repositories cost almost nothing and buy
two things: tasks cannot overwrite each other, and promoting one to a real
project later is just registering the directory that already exists -- no
worktree has to be carved out of another worktree.

See docs/work_destination_work_order.md.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import unicodedata
from pathlib import Path

from config import settings

logger = logging.getLogger("server")

# Long enough to stay readable in a file manager, short enough to leave room for
# the id that actually guarantees uniqueness.
_MAX_SLUG_LENGTH = 40


class ScratchUnavailable(RuntimeError):
    """The scratch root could not be prepared.

    Raised instead of falling back to any real directory: substituting a
    destination silently is the failure this module exists to remove.
    """


def scratch_root() -> Path:
    return Path(str(settings.WORK_SCRATCH_ROOT)).expanduser()


def ensure_scratch_root() -> Path:
    """Create the scratch container if absent and return it."""

    root = scratch_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScratchUnavailable(f"cannot create scratch root {root}: {exc}") from exc
    return root.resolve()


def is_scratch_path(path: str | Path | None) -> bool:
    """True when ``path`` is the scratch root or lives inside it."""

    if not path:
        return False
    try:
        target = Path(path).resolve()
        root = scratch_root().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return target == root or root in target.parents


def is_scratch_root(path: str | Path | None) -> bool:
    """True only for the container itself, not for what lives inside it.

    The container is not a destination -- naming nothing already selects it --
    but a draft promoted to a project keeps living underneath it, and that one
    *is* a destination. Only the root is excluded from routing choices.
    """

    if not path:
        return False
    try:
        return Path(path).resolve() == scratch_root().resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def slugify(title: str) -> str:
    """A readable directory stem for a spoken task title.

    Users occasionally go looking for these directories, and a name carried
    over from what they asked for is far easier to recognise than a random one.
    Non-Latin titles legitimately reduce to nothing here; the caller's id is
    what keeps the name unique, so an empty slug is not an error.
    """

    normalized = unicodedata.normalize("NFKD", str(title or ""))
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    return slug[:_MAX_SLUG_LENGTH].strip("-")


def create_scratch_workspace(title: str, *, unique_id: str) -> Path:
    """Create and initialise one task's own scratch repository."""

    root = ensure_scratch_root()
    suffix = str(unique_id or "").strip()[-8:] or "task"
    stem = slugify(title)
    directory = root / (f"{stem}-{suffix}" if stem else suffix)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # The id is unique per work item, so this only happens on a retry that
        # reached provisioning twice. Reusing it keeps that idempotent.
        pass
    except OSError as exc:
        raise ScratchUnavailable(
            f"cannot create scratch workspace {directory}: {exc}"
        ) from exc
    _git_init(directory)
    return directory.resolve()


def _git_init(directory: Path) -> None:
    """Make the directory a repository so diffs and promotion both work.

    A failure here is not fatal: the work still has a private directory to run
    in, which is the guarantee that matters. Only the diff canvas degrades, and
    it already handles a workspace without a HEAD.
    """

    if (directory / ".git").exists():
        return
    try:
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **(
                {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                if os.name == "nt"
                else {}
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("scratch workspace %s could not be git-initialised: %s", directory, exc)
