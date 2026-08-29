from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any


CREATE_NO_WINDOW = 0x08000000


async def run_git(cwd: str, args: list[str]) -> dict[str, Any]:
    path = Path(cwd).resolve()
    if not path.exists() or not path.is_dir():
        return {"returncode": 2, "stdout": "", "stderr": f"cwd does not exist: {path}"}
    kwargs: dict[str, Any] = {
        "cwd": str(path),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    # Git quotes non-ASCII pathnames by default. Machine consumers must see
    # repository paths, not display-oriented C-style escape text.
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.quotepath=false",
        *args,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return {
        "returncode": int(proc.returncode or 0),
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def collect_diff(cwd: str, *, include_patch: bool = True) -> dict[str, Any]:
    resolved = str(Path(cwd).resolve())
    head_probe = await run_git(resolved, ["rev-parse", "--verify", "HEAD"])
    has_head = head_probe["returncode"] == 0
    if include_patch:
        diff_args = ["diff", "HEAD", "--"] if has_head else ["diff", "--"]
    else:
        diff_args = ["diff", "--name-only", "HEAD", "--"] if has_head else ["diff", "--name-only", "--"]
    diff = await run_git(resolved, diff_args)
    status = await run_git(
        resolved,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    changed_files, untracked = _paths_from_porcelain_v1_z(status["stdout"])
    return {
        "success": diff["returncode"] == 0 and status["returncode"] == 0,
        "cwd": resolved,
        "patch": diff["stdout"] if include_patch else "",
        "stderr": diff["stderr"],
        "returncode": diff["returncode"],
        "untracked": untracked,
        "head": has_head,
        "changed_files": _dedupe([*changed_files, *untracked]),
    }


def _paths_from_porcelain_v1_z(text: str) -> tuple[list[str], list[str]]:
    """Parse Git's stable NUL-delimited status without display quoting.

    In ``-z`` mode a rename/copy record names the current path first and the
    source path in the following NUL field. Only the current tree path is an
    artifact candidate.
    """

    changed: list[str] = []
    untracked: list[str] = []
    records = str(text or "").split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        path = record[3:]
        if not path or status == "!!":
            continue
        changed.append(path)
        if status == "??":
            untracked.append(path)
        if "R" in status or "C" in status:
            index += 1
    return _dedupe(changed), _dedupe(untracked)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in {"/dev/null", "dev/null"}:
            continue
        key = text.lower().replace("\\", "/")
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
