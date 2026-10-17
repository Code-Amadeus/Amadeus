"""Prepare the pinned optional Japanese dictionary offline; never publish it."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import unicodedata
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.japanese_dictionary import (  # noqa: E402
    PACK_TREE, JapaneseDictionaryError, dictionary_rows, load_dictionary_pack,
)
from tools.audit_japanese_dictionary import native_mora_counter, native_surface_normalizer, normalize_rows  # noqa: E402

RECIPE_PATH = PROJECT_ROOT / "tools/data/japanese_dictionary_source.json"
CORRECTIONS_PATH = PROJECT_ROOT / "tools/data/japanese_dictionary_corrections.csv"


def prepare_dictionary(source: Path, *, project_root: Path = PROJECT_ROOT,
                       recipe_path: Path = RECIPE_PATH, corrections_path: Path = CORRECTIONS_PATH,
                       overwrite: bool = False, count_mora: Callable[[str], int | None] | None = None,
                       normalize_surface: Callable[[str], str] | None = None) -> Path:
    recipe = json.loads(Path(recipe_path).read_text(encoding="utf-8"))
    data = Path(source).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != recipe["source"]["sha256"]:
        raise JapaneseDictionaryError("Source CSV differs from the pinned upstream; personal data must not be bundled")
    rows = list(dictionary_rows(data))
    if len(rows) != recipe["source"]["entry_count"]:
        raise JapaneseDictionaryError("Source entry count differs from the pinned upstream")
    def normalize(value: str) -> str:
        return unicodedata.normalize("NFKC", value).lower()
    correction_data = Path(corrections_path).read_bytes().replace(b"\r\n", b"\n")
    corrections = {normalize(row[0]): row for row in dictionary_rows(correction_data)}
    before = {normalize(item["word"]): item["before"] for item in recipe["corrections"]}
    if count_mora is None:
        count_mora, runtime_version = native_mora_counter()
        if runtime_version != recipe["normalization"]["native_runtime"]:
            raise JapaneseDictionaryError("Prepare with the recipe's pinned pyopenjtalk-plus runtime")
        normalize_surface = native_surface_normalizer() if normalize_surface is None else normalize_surface
    else:
        runtime_version = "injected"
    # A repair targets the reviewed bad pronunciation, not every reading of
    # the same surface. Preserve legitimate alternatives and their costs.
    corrected = []
    for row in rows:
        word = normalize(row[0])
        expected = before.get(word)
        matches = expected is not None and unicodedata.normalize("NFC", row[12]) == unicodedata.normalize("NFC", expected)
        if word in corrections and matches:
            row = list(corrections[word])
        corrected.append(row)
    existing = {normalize(row[0]) for row in rows}
    for word, correction in corrections.items():
        if word not in existing:
            row = list(correction)
            corrected.append(row)
    corrected, normalization = normalize_rows(corrected, count_mora, normalize_surface)
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(corrected)
    derivative = buffer.getvalue().encode("utf-8")
    metadata = {**recipe, "sha256": hashlib.sha256(derivative).hexdigest(), "entry_count": len(corrected),
                "corrections_sha256": hashlib.sha256(correction_data).hexdigest(),
                "normalization": {**recipe["normalization"],
                                  "results": {key: value for key, value in normalization.items() if key != "unresolved"}},
                "native_runtime": runtime_version}
    files = {
        "userdict.csv": derivative,
        "provenance.json": (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "NOTICE.md": (PROJECT_ROOT / "LICENSES/JAPANESE-DICTIONARY.md").read_bytes().replace(b"\r\n", b"\n"),
        "GPT-SoVITS-MIT.txt": (PROJECT_ROOT / "LICENSES/GPT-SoVITS-MIT.txt").read_bytes().replace(b"\r\n", b"\n"),
        "CMUdict-BSD-2-Clause.txt": (PROJECT_ROOT / "LICENSES/CMUdict-BSD-2-Clause.txt").read_bytes().replace(b"\r\n", b"\n"),
        "Amadeus-AGPL-3.0.txt": (PROJECT_ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n"),
    }
    destination = Path(project_root).resolve() / "assets" / PACK_TREE
    if not destination.resolve().is_relative_to(Path(project_root).resolve()):
        raise JapaneseDictionaryError("Dictionary output escapes the project root")
    for name, content in files.items():
        target = destination / name
        if target.is_symlink():
            raise JapaneseDictionaryError(f"Dictionary output must not be a symlink: {name}")
        if target.exists() and target.read_bytes() != content and not overwrite:
            raise JapaneseDictionaryError(f"Existing dictionary pack differs: {name}; use --overwrite explicitly")
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (destination / name).write_bytes(content)
    load_dictionary_pack(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Existing upstream CSV; no download is performed")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        output = prepare_dictionary(args.source, project_root=args.project_root, overwrite=args.overwrite)
    except (JapaneseDictionaryError, OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(f"Prepared optional dictionary: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
