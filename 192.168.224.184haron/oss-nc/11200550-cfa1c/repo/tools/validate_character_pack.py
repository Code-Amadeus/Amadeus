"""Validate one Amadeus runtime character pack without loading the renderer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from render.character_pack import CharacterPack, CharacterPackError, load_character_pack  # noqa: E402


def validate_path(path: Path) -> CharacterPack:
    return load_character_pack(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="character-pack directory")
    args = parser.parse_args()

    try:
        pack = validate_path(args.path)
    except CharacterPackError as exc:
        print(f"INVALID [{exc.code}]: {exc}", file=sys.stderr)
        return 1

    graph = pack.graph
    manual_edges = sum(1 for edge in graph["edges"] if edge["prob"] == 0)
    frame_count = sum(len(paths) for paths in pack.clip_paths.values())
    print(
        "OK: "
        f"{pack.manifest.get('id')} {pack.manifest.get('version')} | "
        f"nodes={len(graph['nodes'])} edges={len(graph['edges'])} "
        f"manual_edges={manual_edges} clips={len(pack.clip_paths)} frames={frame_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
