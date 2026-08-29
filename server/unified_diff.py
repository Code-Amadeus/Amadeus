from __future__ import annotations

import re
import shlex
from typing import Any


_HUNK_HEADER = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@(?P<label>.*)$"
)


def parse_unified_diff(
    patch: str,
    *,
    changed_files: list[str] | None = None,
    untracked: list[str] | None = None,
    max_files: int = 48,
    max_lines: int = 2400,
) -> dict[str, Any]:
    """Convert a git-style unified diff into the provider-neutral canvas shape."""

    source = str(patch or "")
    files: list[dict[str, Any]] = []
    current_file: dict[str, Any] | None = None
    current_hunk: dict[str, Any] | None = None
    old_line = 0
    new_line = 0
    parsed_lines = 0
    truncated = False

    def append_file(old_path: str = "", new_path: str = "") -> dict[str, Any] | None:
        nonlocal truncated
        if len(files) >= max_files:
            truncated = True
            return None
        path = _display_path(new_path) or _display_path(old_path) or "unknown"
        item: dict[str, Any] = {
            "path": path,
            "oldPath": _display_path(old_path),
            "newPath": _display_path(new_path),
            "status": "modified",
            "additions": 0,
            "deletions": 0,
            "hunks": [],
        }
        files.append(item)
        return item

    for raw_line in source.splitlines():
        if raw_line.startswith("diff --git "):
            old_path, new_path = _paths_from_diff_header(raw_line)
            current_file = append_file(old_path, new_path)
            current_hunk = None
            continue

        if raw_line.startswith("--- "):
            old_path = _marker_path(raw_line[4:])
            if current_file is None:
                current_file = append_file(old_path, "")
            else:
                current_file["oldPath"] = _display_path(old_path)
            if current_file is None:
                continue
            if old_path == "/dev/null":
                current_file["status"] = "added"
            continue

        if raw_line.startswith("+++ "):
            new_path = _marker_path(raw_line[4:])
            if current_file is None:
                current_file = append_file("", new_path)
            else:
                current_file["newPath"] = _display_path(new_path)
                if new_path != "/dev/null":
                    current_file["path"] = _display_path(new_path)
            if current_file is None:
                continue
            if new_path == "/dev/null":
                current_file["status"] = "deleted"
            continue

        if current_file is None:
            continue

        if raw_line.startswith("new file mode "):
            current_file["status"] = "added"
            continue
        if raw_line.startswith("deleted file mode "):
            current_file["status"] = "deleted"
            continue
        if raw_line.startswith("rename from "):
            current_file["status"] = "renamed"
            current_file["oldPath"] = raw_line[len("rename from ") :].strip()
            continue
        if raw_line.startswith("rename to "):
            current_file["status"] = "renamed"
            current_file["newPath"] = raw_line[len("rename to ") :].strip()
            current_file["path"] = current_file["newPath"]
            continue

        hunk_match = _HUNK_HEADER.match(raw_line)
        if hunk_match:
            old_line = int(hunk_match.group("old_start"))
            new_line = int(hunk_match.group("new_start"))
            current_hunk = {
                "header": raw_line,
                "oldStart": old_line,
                "oldCount": int(hunk_match.group("old_count") or 1),
                "newStart": new_line,
                "newCount": int(hunk_match.group("new_count") or 1),
                "label": hunk_match.group("label").strip(),
                "lines": [],
            }
            current_file["hunks"].append(current_hunk)
            continue

        if current_hunk is None or not raw_line:
            continue
        prefix = raw_line[0]
        if prefix not in {" ", "+", "-", "\\"}:
            continue
        if parsed_lines >= max_lines:
            truncated = True
            continue

        text = raw_line[1:] if prefix != "\\" else raw_line
        if prefix == "+":
            kind = "add"
            line_item = {"kind": kind, "newLine": new_line, "text": text}
            new_line += 1
            current_file["additions"] += 1
        elif prefix == "-":
            kind = "remove"
            line_item = {"kind": kind, "oldLine": old_line, "text": text}
            old_line += 1
            current_file["deletions"] += 1
        elif prefix == " ":
            kind = "context"
            line_item = {
                "kind": kind,
                "oldLine": old_line,
                "newLine": new_line,
                "text": text,
            }
            old_line += 1
            new_line += 1
        else:
            kind = "meta"
            line_item = {"kind": kind, "text": text}
        current_hunk["lines"].append(line_item)
        parsed_lines += 1

    known_paths = {_path_key(item.get("path")) for item in files}
    untracked_keys = {_path_key(item) for item in (untracked or [])}
    for path in [*(changed_files or []), *(untracked or [])]:
        text = str(path or "").strip()
        key = _path_key(text)
        if not text or key in known_paths:
            continue
        if len(files) >= max_files:
            truncated = True
            break
        files.append(
            {
                "path": text,
                "oldPath": "",
                "newPath": text,
                "status": "untracked" if key in untracked_keys else "modified",
                "additions": 0,
                "deletions": 0,
                "hunks": [],
            }
        )
        known_paths.add(key)

    additions = sum(int(item.get("additions") or 0) for item in files)
    deletions = sum(int(item.get("deletions") or 0) for item in files)
    return {
        "files": files,
        "fileCount": len(files),
        "additions": additions,
        "deletions": deletions,
        "lineCount": parsed_lines,
        "truncated": truncated,
        "clean": not files and not source.strip(),
    }


def _paths_from_diff_header(line: str) -> tuple[str, str]:
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    if len(parts) >= 4:
        return parts[2], parts[3]
    return "", ""


def _marker_path(value: str) -> str:
    text = str(value or "").split("\t", 1)[0].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            parsed = shlex.split(text)
            return parsed[0] if parsed else text[1:-1]
        except ValueError:
            return text[1:-1]
    return text


def _display_path(value: Any) -> str:
    text = str(value or "").strip()
    if text in {"/dev/null", "dev/null"}:
        return ""
    if text.startswith(("a/", "b/")):
        return text[2:]
    return text


def _path_key(value: Any) -> str:
    return _display_path(value).replace("\\", "/").lower()
