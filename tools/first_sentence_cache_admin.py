# -*- coding: utf-8 -*-
"""Inspect and repair the first-sentence exact-match audio cache.

This is intentionally a standalone maintenance tool. It is not imported by the
runtime path and does not change the main program behavior.

Examples:
    python tools/first_sentence_cache_admin.py --stats
    python tools/first_sentence_cache_admin.py --list --limit 20
    python tools/first_sentence_cache_admin.py --all-files --limit 20
    python tools/first_sentence_cache_admin.py --text "そうね"
    python tools/first_sentence_cache_admin.py --index 12 --delete --yes
    python tools/first_sentence_cache_admin.py --text "そうね" --rebuild --yes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config.settings import FIRST_SENTENCE_AUDIO_CACHE_DIR  # noqa: E402
from tools.prebuild_first_sentence_audio_cache import (  # noqa: E402
    _iter_items,
    _load_inferencer,
    _synthesize_to_cache,
)


@dataclass
class CacheInfo:
    duration: float | None
    sr: int | None
    samples: int | None
    metadata: dict[str, Any] | None


@dataclass
class ListedItem:
    index: int
    raw_text: str
    processed_text: str
    cache_path: Path
    cached: bool
    duration: float | None
    sr: int | None
    samples: int | None
    metadata: dict[str, Any] | None
    candidate_index: int | None = None


def _decode_meta_json(value: Any) -> dict[str, Any] | None:
    try:
        if isinstance(value, np.ndarray):
            value = value.item()
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _read_cache_info(path: Path) -> CacheInfo:
    if not path.exists():
        return CacheInfo(None, None, None, None)
    try:
        with np.load(path, allow_pickle=False) as data:
            sr = int(data["sr"])
            audio = np.asarray(data["audio"], dtype=np.float32)
            metadata = _decode_meta_json(data["meta_json"]) if "meta_json" in data.files else None
        if sr <= 0 or audio.size <= 0:
            return CacheInfo(None, sr, int(audio.size), metadata)
        return CacheInfo(float(audio.size) / float(sr), sr, int(audio.size), metadata)
    except Exception:
        return CacheInfo(None, None, None, None)


def _build_candidate_listing(limit: int | None = None) -> list[ListedItem]:
    items = _iter_items(limit=limit, shuffle=False)
    listed: list[ListedItem] = []
    for index, item in enumerate(items, start=1):
        info = _read_cache_info(item.cache_path)
        listed.append(
            ListedItem(
                index=index,
                raw_text=item.raw_text,
                processed_text=item.processed_text,
                cache_path=item.cache_path,
                cached=item.cache_path.exists(),
                duration=info.duration,
                sr=info.sr,
                samples=info.samples,
                metadata=info.metadata,
                candidate_index=index,
            )
        )
    return listed


def _iter_cache_files() -> list[Path]:
    root = Path(FIRST_SENTENCE_AUDIO_CACHE_DIR)
    if not root.exists():
        return []
    return sorted(root.glob("*/*.npz"))


def _build_all_file_listing(limit: int | None = None) -> list[ListedItem]:
    paths = _iter_cache_files()
    if limit is not None:
        paths = paths[:limit]
    listed: list[ListedItem] = []
    for index, path in enumerate(paths, start=1):
        info = _read_cache_info(path)
        meta = info.metadata or {}
        raw_text = str(meta.get("raw_text") or meta.get("processed_text") or "(unknown; no metadata)")
        processed_text = str(meta.get("processed_text") or raw_text)
        listed.append(
            ListedItem(
                index=index,
                raw_text=raw_text,
                processed_text=processed_text,
                cache_path=path,
                cached=path.exists(),
                duration=info.duration,
                sr=info.sr,
                samples=info.samples,
                metadata=info.metadata,
            )
        )
    return listed


def _select_items(
    items: list[ListedItem],
    *,
    indexes: list[int],
    text_filters: list[str],
    hash_prefixes: list[str],
    cached_only: bool,
    missing_only: bool,
    metadata_only: bool,
    no_metadata_only: bool,
) -> list[ListedItem]:
    selected = items
    if indexes:
        index_set = set(indexes)
        selected = [item for item in selected if item.index in index_set]
    if text_filters:
        lowered = [value.lower() for value in text_filters]
        selected = [
            item
            for item in selected
            if any(value in item.raw_text.lower() or value in item.processed_text.lower() for value in lowered)
        ]
    if hash_prefixes:
        prefixes = [value.lower().removesuffix(".npz") for value in hash_prefixes]
        selected = [
            item
            for item in selected
            if any(item.cache_path.stem.lower().startswith(prefix) for prefix in prefixes)
        ]
    if cached_only:
        selected = [item for item in selected if item.cached]
    if missing_only:
        selected = [item for item in selected if not item.cached]
    if metadata_only:
        selected = [item for item in selected if item.metadata is not None]
    if no_metadata_only:
        selected = [item for item in selected if item.metadata is None]
    return selected


def _format_item(item: ListedItem, *, show_path: bool, show_metadata: bool) -> str:
    status = "hit" if item.cached else "miss"
    if item.cached and item.duration is not None:
        detail = f"{item.duration:5.2f}s {item.sr or 0:5d}Hz"
    elif item.cached:
        detail = " unreadable"
    else:
        detail = "         "
    source = ""
    if item.metadata is not None:
        source = f" [{item.metadata.get('source', 'meta')}]"
    elif item.cached:
        source = " [old/no-meta]"
    suffix = f" -> {item.cache_path}" if show_path else f" -> {item.cache_path.name}"
    lines = [f"{item.index:03d} {status:4s} {detail}{source} {item.raw_text}{suffix}"]
    if show_metadata and item.metadata is not None:
        created_at = item.metadata.get("created_at")
        if isinstance(created_at, (int, float)):
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_at))
        else:
            created = "unknown"
        lines.append(
            f"    key={item.metadata.get('key', item.cache_path.stem)} created={created} "
            f"processed={item.metadata.get('processed_text', '')}"
        )
    return "\n".join(lines)


def _delete_items(items: list[ListedItem], *, yes: bool) -> int:
    deleted = 0
    for item in items:
        if not item.cache_path.exists():
            print(f"skip missing {item.index:03d} {item.raw_text}")
            continue
        if not yes:
            print(f"would delete {item.index:03d} {item.raw_text} -> {item.cache_path}")
            continue
        item.cache_path.unlink()
        deleted += 1
        print(f"deleted {item.index:03d} {item.raw_text} -> {item.cache_path.name}")
    if not yes and items:
        print("dry-run only; add --yes to delete")
    return deleted


def _rebuild_items(items: list[ListedItem], *, yes: bool) -> int:
    if not items:
        return 0
    if any(item.candidate_index is None for item in items):
        print("rebuild only supports configured candidate entries; omit --all-files")
        return 0
    if not yes:
        for item in items:
            print(f"would rebuild {item.index:03d} {item.raw_text} -> {item.cache_path.name}")
        print("dry-run only; add --yes to rebuild")
        return 0

    prebuild_items = _iter_items(limit=None, shuffle=False)
    by_index = {index: item for index, item in enumerate(prebuild_items, start=1)}
    inferencer = _load_inferencer()
    rebuilt = 0
    for item in items:
        prepared = by_index[item.candidate_index or item.index]
        t0 = time.perf_counter()
        status, elapsed, path = _synthesize_to_cache(inferencer, prepared, force=True)
        rebuilt += 1 if status.startswith("stored") else 0
        wall = time.perf_counter() - t0
        print(f"rebuilt {item.index:03d} {status:14s} synth={elapsed:6.2f}s wall={wall:6.2f}s {item.raw_text} -> {path.name}")
    return rebuilt


def _print_stats() -> None:
    candidate_items = _build_candidate_listing(limit=None)
    files = _iter_cache_files()
    file_infos = [_read_cache_info(path) for path in files]
    candidate_hits = sum(1 for item in candidate_items if item.cached)
    metadata_files = sum(1 for info in file_infos if info.metadata is not None)
    old_files = len(files) - metadata_files
    print(f"candidate_total={len(candidate_items)}")
    print(f"candidate_active_hits={candidate_hits}")
    print(f"candidate_missing={len(candidate_items) - candidate_hits}")
    print(f"cache_files_total={len(files)}")
    print(f"cache_files_with_metadata={metadata_files}")
    print(f"cache_files_without_metadata={old_files}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect/delete/rebuild first-sentence audio cache entries.")
    parser.add_argument("--stats", action="store_true", help="print candidate and disk cache statistics")
    parser.add_argument("--list", action="store_true", help="list matching cache entries")
    parser.add_argument("--all-files", action="store_true", help="inspect real .npz files on disk instead of configured candidates")
    parser.add_argument("--limit", type=int, default=None, help="only inspect the first N entries")
    parser.add_argument("--index", type=int, action="append", default=[], help="select listing index; can repeat")
    parser.add_argument("--text", action="append", default=[], help="select by raw or processed text substring; can repeat")
    parser.add_argument("--hash", action="append", default=[], help="select by cache filename/hash prefix; can repeat")
    parser.add_argument("--cached-only", action="store_true", help="only show/select entries that have cache files")
    parser.add_argument("--missing-only", action="store_true", help="only show/select entries without cache files")
    parser.add_argument("--metadata-only", action="store_true", help="only show/select cache files that contain metadata")
    parser.add_argument("--no-metadata-only", action="store_true", help="only show/select cache files without metadata")
    parser.add_argument("--show-metadata", action="store_true", help="print metadata details when available")
    parser.add_argument("--show-path", action="store_true", help="print full cache paths")
    parser.add_argument("--delete", action="store_true", help="delete selected cache files")
    parser.add_argument("--rebuild", action="store_true", help="rebuild selected configured candidates immediately")
    parser.add_argument("--force", action="store_true", help="accepted for compatibility; --rebuild already overwrites")
    parser.add_argument("--yes", action="store_true", help="confirm destructive or expensive actions")
    args = parser.parse_args()

    if args.cached_only and args.missing_only:
        parser.error("--cached-only and --missing-only cannot be used together")
    if args.metadata_only and args.no_metadata_only:
        parser.error("--metadata-only and --no-metadata-only cannot be used together")
    if args.delete and args.rebuild:
        parser.error("--delete and --rebuild cannot be used together")
    if args.rebuild and args.all_files:
        parser.error("--rebuild does not support --all-files")

    if args.stats:
        _print_stats()

    all_items = _build_all_file_listing(limit=args.limit) if args.all_files else _build_candidate_listing(limit=args.limit)
    selected = _select_items(
        all_items,
        indexes=args.index,
        text_filters=args.text,
        hash_prefixes=args.hash,
        cached_only=args.cached_only,
        missing_only=args.missing_only,
        metadata_only=args.metadata_only,
        no_metadata_only=args.no_metadata_only,
    )

    should_list = args.list or not (args.delete or args.rebuild or args.stats)
    if should_list:
        cached = sum(1 for item in selected if item.cached)
        print(f"selected={len(selected)} cached={cached} missing={len(selected) - cached}")
        for item in selected:
            print(_format_item(item, show_path=args.show_path, show_metadata=args.show_metadata))

    if args.delete:
        deleted = _delete_items(selected, yes=args.yes)
        print(f"deleted={deleted}")
    elif args.rebuild:
        rebuilt = _rebuild_items(selected, yes=args.yes)
        print(f"rebuilt={rebuilt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
