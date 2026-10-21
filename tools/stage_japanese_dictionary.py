"""Stage the pinned dictionary for application distribution; never install into user data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.japanese_dictionary import BUNDLED_TREE, PACK_TREE, PACK_ID, PACK_FILES, FULL_SOURCE_FILES, load_dictionary_pack
from tools.external_assets import install_bundle

PIN_PATH = ROOT / "release/japanese_dictionary.json"


def load_pin(path: Path = PIN_PATH) -> dict:
    pin = json.loads(path.read_bytes())
    if (not isinstance(pin, dict) or pin.get("format") != "amadeus.bundled-japanese-dictionary.v1"
            or pin.get("pack_id") != PACK_ID or not pin.get("version")
            or not re.fullmatch(r"[a-f0-9]{64}", pin.get("sha256", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", pin.get("resource_identity", ""))):
        raise ValueError("Invalid bundled Japanese dictionary pin")
    expected = {f"assets/{PACK_TREE}/{name}" for name in PACK_FILES + FULL_SOURCE_FILES}
    files = pin.get("files", [])
    if (not isinstance(files, list) or not all(isinstance(r, dict) for r in files)
            or len(files) != len(expected) or {r.get("path") for r in files} != expected
            or any(not re.fullmatch(r"[a-f0-9]{64}", r.get("sha256", ""))
                   or not isinstance(r.get("size"), int) or r["size"] < 1 for r in files)):
        raise ValueError("Bundled dictionary must contain exactly the nine pinned resource files")
    return pin


def validate_directory(directory: Path, pin: dict) -> list[dict]:
    expected = {Path(record["path"]).name for record in pin["files"]}
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != expected:
        raise ValueError("Bundled Japanese dictionary is missing or has unexpected files; stage the pinned resource first")
    records = []
    for entry in pin["files"]:
        file = directory / Path(entry["path"]).name
        data = file.read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"Bundled Japanese dictionary differs from release pin: {file.name}")
        records.append({"path": f"assets/{BUNDLED_TREE}/{file.name}", "size": len(data),
                        "sha256": entry["sha256"], "origin": "bundled_dictionary"})
    pack = load_dictionary_pack(directory)
    if pack.version != pin["version"] or pack.identity != pin["resource_identity"]:
        raise ValueError("Bundled dictionary version/identity differs from release pin")
    return sorted(records, key=lambda entry: entry["path"])


def stage_bundle(archive: Path, output_root: Path, *, pin_path: Path = PIN_PATH,
                 index_path: Path = ROOT / "assets/index.json", overwrite: bool = False) -> Path:
    pin = load_pin(pin_path)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError("Japanese dictionary archive SHA-256 differs from release pin")
    with tempfile.TemporaryDirectory(prefix="amadeus-bundled-dictionary-") as temp:
        source_root = Path(temp)
        installed = install_bundle(archive, project_root=source_root, index_path=index_path)
        if installed["packs"] != [PACK_ID]:
            raise ValueError("Application dictionary input must contain only japanese-pronunciation")
        source = source_root / "assets" / PACK_TREE
        records = validate_directory(source, pin)
        target = output_root / "assets" / BUNDLED_TREE
        if target.exists():
            try:
                validate_directory(target, pin)
                return target
            except ValueError:
                if not overwrite:
                    raise FileExistsError("Staged dictionary differs; use a fresh output root or --overwrite") from None
                if not {p.name for p in target.iterdir()} <= {Path(r["path"]).name for r in records}:
                    raise ValueError("Refusing to overwrite a staging directory containing unrelated files") from None
        target.mkdir(parents=True, exist_ok=True)
        for record in records:
            name = Path(record["path"]).name
            shutil.copyfile(source / name, target / name)
        validate_directory(target, pin)
        return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT / "build/bundled-assets")
    parser.add_argument("--pin", type=Path, default=PIN_PATH)
    parser.add_argument("--check", type=Path, help="verify an existing bundled dictionary directory")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.check:
        records = validate_directory(args.check, load_pin(args.pin))
        print(json.dumps({"files": len(records), "valid": True}))
    elif args.archive:
        print(stage_bundle(args.archive, args.output_root, pin_path=args.pin, overwrite=args.overwrite))
    else:
        parser.error("provide --archive or --check")


if __name__ == "__main__":
    main()
