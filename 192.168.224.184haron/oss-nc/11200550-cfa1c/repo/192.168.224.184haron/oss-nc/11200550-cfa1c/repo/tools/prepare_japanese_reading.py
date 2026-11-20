"""Prepare an optional pinned tsqyomi v5 pack from existing local files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.japanese_reading import (  # noqa: E402
    MODEL_HASHES, PACK_TREE, SOURCE_REPOSITORY, SOURCE_REVISION, VERSION,
    JapaneseReadingError, load_reading_pack,
)


def prepare(source: Path, *, project_root: Path = ROOT, overwrite: bool = False) -> Path:
    destination = Path(project_root).resolve() / "assets" / PACK_TREE
    if not destination.resolve().is_relative_to(Path(project_root).resolve()):
        raise JapaneseReadingError("Model output escapes the project root")
    source = Path(source)
    for name, expected in MODEL_HASHES.items():
        with (source / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise JapaneseReadingError(f"Source model differs from the pinned release: {name}")
    provenance = {"version": VERSION, "language": "ja", "model": "tsqyomi-v5",
                  "source": {"repository": SOURCE_REPOSITORY, "revision": SOURCE_REVISION, "directory": "v5"},
                  "sha256": MODEL_HASHES, "license": "MIT", "copyright": "Copyright (c) 2026 Aivis Project"}
    files = {name: source / name for name in MODEL_HASHES}
    content = {"provenance.json": (json.dumps(provenance, indent=2) + "\n").encode(),
               "NOTICE.md": (ROOT / "LICENSES/TSQYOMI.md").read_bytes().replace(b"\r\n", b"\n"),
               "MIT.txt": (ROOT / "LICENSES/TSQYOMI-MIT.txt").read_bytes().replace(b"\r\n", b"\n")}
    for name in (*files, *content):
        target = destination / name
        if target.is_symlink():
            raise JapaneseReadingError(f"Model output must not be a symlink: {name}")
        expected = MODEL_HASHES.get(name) or hashlib.sha256(content[name]).hexdigest()
        if target.exists() and not overwrite:
            with target.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise JapaneseReadingError(f"Existing pack differs: {name}; use --overwrite explicitly")
    destination.mkdir(parents=True, exist_ok=True)
    import shutil
    for name, path in files.items():
        shutil.copyfile(path, destination / name)
    for name, data in content.items():
        (destination / name).write_bytes(data)
    load_reading_pack(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    print(prepare(args.source, project_root=args.project_root, overwrite=args.overwrite))
