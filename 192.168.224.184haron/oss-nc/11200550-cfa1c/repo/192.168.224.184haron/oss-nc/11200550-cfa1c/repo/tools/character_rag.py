"""Build or inspect the optional character index: python -m tools.character_rag."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import time

from core.character_rag import (
    DEFAULT_MODEL, CharacterKnowledgeIndex, build_index, resolve_directory,
)


def main() -> None:
    # Use the same .env/process defaults as Chat. Keep startup diagnostics on
    # stderr so stdout remains one machine-readable JSON result.
    with redirect_stdout(sys.stderr):
        from config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Explicitly download/cache the model and build an index")
    build.add_argument("--source", type=Path, required=True, help="A JSON array of texts, or a directory of such JSON files")
    build.add_argument("--model", default=DEFAULT_MODEL)
    search = commands.add_parser("search", help="Inspect retrieval locally, without calling a chat model")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=settings.RAG_TOP_K)
    search.add_argument("--max-distance", type=float, default=settings.RAG_MAX_DISTANCE)
    for command in (build, search):
        command.add_argument("--index-dir", default=settings.RAG_INDEX_DIR)
    args = parser.parse_args()
    directory = resolve_directory(args.index_dir)
    if args.command == "build":
        result = build_index(args.source.resolve(), directory, args.model)
    else:
        if not 1 <= args.top_k <= 20 or not 0 <= args.max_distance <= 4:
            parser.error("top-k must be 1..20 and max-distance must be 0..4")
        started = time.perf_counter()
        index = CharacterKnowledgeIndex(directory)
        load_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        candidates = index.search(args.query, top_k=args.top_k)
        result = {
            "query": args.query,
            "index_dir": str(directory), "model": index.model_name,
            "index_sha256": index.index_sha256,
            "entries": len(index.texts), "top_k": args.top_k,
            "max_distance": args.max_distance,
            "candidates": candidates,
            "hits": [hit for hit in candidates if hit["distance"] <= args.max_distance],
            "load_ms": round(load_ms, 1),
            "search_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
