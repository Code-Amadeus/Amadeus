"""Provider-neutral matching of a cwd against trusted workspace roots."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable


CREATE_NO_WINDOW = 0x08000000


def parse_workspace_roots(value: str | None) -> tuple[str, ...]:
    """Parse a semicolon-delimited deployment setting without guessing paths."""

    roots: list[str] = []
    seen: set[str] = set()
    for item in str(value or "").split(";"):
        candidate = item.strip()
        if not candidate:
            continue
        key = os.path.normcase(candidate)
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return tuple(roots)


def cwd_matches_workspace_roots(
    cwd: str | None,
    roots: Iterable[str],
) -> bool:
    """Match containment or a linked worktree of the same Git repository."""

    if not cwd:
        return False
    try:
        target = Path(cwd).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    target_repository = _git_repository_identity(target)
    for item in roots:
        try:
            root = Path(item).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if _same_or_child(target, root):
            return True
        root_repository = _git_repository_identity(root)
        if (
            target_repository is not None
            and root_repository is not None
            and _same_path(
                target_repository["common_dir"],
                root_repository["common_dir"],
            )
            and _same_or_child(target, target_repository["worktree_root"])
        ):
            return True
    return False


def _git_repository_identity(path: Path) -> dict[str, Path] | None:
    if not path.is_dir():
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "rev-parse",
                "--show-toplevel",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    worktree_root = Path(lines[0]).resolve()
    common_dir = Path(lines[1])
    if not common_dir.is_absolute():
        common_dir = path / common_dir
    return {
        "worktree_root": worktree_root,
        "common_dir": common_dir.resolve(),
    }


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _same_or_child(target: Path, root: Path) -> bool:
    if _same_path(target, root):
        return True
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False
