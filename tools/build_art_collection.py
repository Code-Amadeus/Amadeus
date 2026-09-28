"""Extend an existing art download collection with an independently optional Lite ZIP.

The outer ZIP is a download container, not an external_assets install bundle.
Original nested packages and sharing notice are preserved byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.asset_packages import ASSET_INDEX_PATH, AssetPackageError  # noqa: E402
from tools.external_assets import install_bundle, verify_bundle  # noqa: E402

COLLECTION_SCHEMA = "amadeus.runtime-art-asset-collection.v1"
NOTICE = "NONCOMMERCIAL_SHARING_NOTICE_zh-CN.txt"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_collection(*, base: Path, companion: Path, readme: Path, output: Path, date: str) -> dict:
    output = output.resolve()
    if output.exists():
        raise AssetPackageError(f"Output already exists: {output}")
    companion_manifest = verify_bundle(companion)
    if companion_manifest["packs"] != [{"id": "companion-kurisu", "spec_version": 1}]:
        raise AssetPackageError("Expected an independent companion-kurisu bundle")
    companion_name = f"04-amadeus-companion-kurisu-{date.replace('-', '.')}.zip"
    if not all(char.isdigit() or char == "-" for char in date) or len(date) != 10:
        raise AssetPackageError("Expected YYYY-MM-DD collection date")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="art-collection-", dir=output.parent) as temporary:
        stage = Path(temporary)
        staged_zip = stage / "collection.zip"
        checksums: dict[str, str] = {}
        verified = []
        with zipfile.ZipFile(base) as old, zipfile.ZipFile(staged_zip, "w", allowZip64=True) as archive:
            manifest = json.loads(old.read("COLLECTION_MANIFEST.json"))
            if manifest.get("schema") != COLLECTION_SCHEMA:
                raise AssetPackageError("Unsupported art collection")
            packages = manifest["packages"]
            names = [record["file"] for record in packages]
            if (len(set(names)) != len(names) or companion_name in names
                or any(not name.endswith(".zip") or "/" in name or "\\" in name or ":" in name for name in names)):
                raise AssetPackageError("Invalid nested package names")
            for record in packages:
                name = record["file"]
                with tempfile.TemporaryDirectory(prefix="nested-", dir=stage) as nested:
                    payload = Path(nested) / "package.zip"
                    digest = hashlib.sha256()
                    with old.open(name) as source, payload.open("xb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                            digest.update(chunk)
                    if payload.stat().st_size != record["bytes"] or digest.hexdigest() != record["sha256"]:
                        raise AssetPackageError(f"Original collection checksum mismatch: {name}")
                    if record["kind"].startswith("installable_"):
                        verify_bundle(payload)
                        verified.append(name)
                    # Stored outer ZIP avoids recompressing already compressed media archives.
                    archive.write(payload, name, compress_type=zipfile.ZIP_STORED)
                    checksums[name] = digest.hexdigest()

            installed = install_bundle(companion, project_root=stage / "clean-install", index_path=ASSET_INDEX_PATH)
            archive.write(companion, companion_name, compress_type=zipfile.ZIP_STORED)
            checksums[companion_name] = _digest(companion)
            packages.append({"file": companion_name, "bytes": companion.stat().st_size,
                             "sha256": checksums[companion_name], "kind": "installable_companion_runtime",
                             "optional": True, "content_files": companion_manifest["file_count"],
                             "content_bytes": companion_manifest["total_bytes"]})
            manifest.update(generated_date=date, asset_index_sha256=_digest(ASSET_INDEX_PATH),
                            validation="Existing installable bundles reverified; existing nested hashes unchanged; Companion verified and clean-installed.")
            for name, content in {
                "COLLECTION_MANIFEST.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                "README_zh-CN.md": readme.read_bytes(),
                NOTICE: old.read(NOTICE),
            }.items():
                archive.writestr(name, content)
                checksums[name] = hashlib.sha256(content).hexdigest()
            archive.writestr("SHA256SUMS.txt", "".join(f"{digest}  {name}\n" for name, digest in checksums.items()))

        # Read the completed ZIP back; verify every nested member and metadata hash.
        with zipfile.ZipFile(staged_zip) as archive:
            for name, expected in checksums.items():
                with archive.open(name) as member:
                    if hashlib.file_digest(member, "sha256").hexdigest() != expected:
                        raise AssetPackageError(f"Collection read-back failed: {name}")
        report = {"archive": output.name, "bytes": staged_zip.stat().st_size,
                  "sha256": _digest(staged_zip), "nested_hash_verification": "passed",
                  "existing_installable_bundles_reverified": verified,
                  "companion_clean_install": installed, "packages": packages}
        os.replace(staged_zip, output)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-collection", type=Path, required=True)
    parser.add_argument("--companion", type=Path, required=True)
    parser.add_argument("--readme", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    report = build_collection(base=args.base_collection, companion=args.companion,
                              readme=args.readme, output=args.output, date=args.date)
    args.output.with_suffix(".report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".zip.sha256").write_text(f"{report['sha256']}  {args.output.name}\n", encoding="ascii")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
