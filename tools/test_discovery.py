"""Make test discovery itself a checked repository invariant."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Iterable


def declared_module_tests(test_dir: Path) -> dict[str, set[str]]:
    """Return every top-level ``test_*`` declaration keyed by POSIX path."""

    root = test_dir.resolve().parent
    declared: dict[str, set[str]] = {}
    for path in sorted(test_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        }
        declared[path.resolve().relative_to(root).as_posix()] = names
    return declared


def collected_by_file(node_ids: Iterable[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for raw in node_ids:
        node_id = str(raw or "").strip().replace("\\", "/")
        if "::" not in node_id:
            continue
        path = node_id.split("::", 1)[0]
        grouped[path].append(node_id)
    return dict(grouped)


def uncollected_declarations(
    declared: dict[str, set[str]],
    node_ids: Iterable[str],
) -> list[str]:
    """Return declarations for which pytest produced no collection node."""

    collected = tuple(
        str(node_id or "").strip().replace("\\", "/")
        for node_id in node_ids
    )
    missing: list[str] = []
    for path, names in sorted(declared.items()):
        for name in sorted(names):
            prefix = f"{path}::{name}"
            if not any(node_id == prefix or node_id.startswith(prefix + "[") for node_id in collected):
                missing.append(prefix)
    return missing
