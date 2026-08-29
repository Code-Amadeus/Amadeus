"""Build, verify, and install separately distributed Amadeus runtime assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.asset_packages import (  # noqa: E402
    ASSET_BUNDLE_FORMAT,
    ASSET_INDEX_PATH,
    AssetPackageError,
    AssetPackSpec,
    asset_pack_specs,
    asset_path,
    external_asset_pack_status,
    path_belongs_to_pack,
)
from render.character_pack import CharacterPackError, load_character_pack  # noqa: E402


BUNDLE_MANIFEST_NAME = "ASSET_BUNDLE_MANIFEST.json"
BUFFER_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024 * 1024
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(BUFFER_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_path(value: Any, *, allow_manifest: bool = False) -> PurePosixPath:
    text = str(value or "")
    if not text or "\x00" in text or "\\" in text or text.startswith(("/", "//")):
        raise AssetPackageError(f"unsafe bundle path: {text!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or path.as_posix() != text:
        raise AssetPackageError(f"bundle path is not normalized: {text!r}")
    for part in path.parts:
        if part in {"", ".", ".."} or ":" in part or part.rstrip(" .") != part:
            raise AssetPackageError(f"unsafe bundle path segment: {part!r}")
        if part.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
            raise AssetPackageError(f"reserved Windows path segment: {part!r}")
    if allow_manifest and path == PurePosixPath(BUNDLE_MANIFEST_NAME):
        return path
    if not path.parts or path.parts[0] != "assets" or len(path.parts) < 2:
        raise AssetPackageError(f"bundle file must live below assets/: {text!r}")
    return path


def _asset_relative(bundle_path: PurePosixPath) -> PurePosixPath:
    return PurePosixPath(*bundle_path.parts[1:])


def _resolved_source_files(
    project_root: Path,
    spec: AssetPackSpec,
) -> tuple[Path, ...]:
    asset_root = project_root / "assets"
    if spec.validator == "character_pack":
        if len(spec.trees) != 1:
            raise AssetPackageError("character pack requires exactly one runtime tree")
        pack = load_character_pack(asset_path(asset_root, spec.trees[0]))
        manifest_path = pack.root / "runtime_manifest.json"
        graph_path = pack.root / str(pack.manifest.get("graph") or "")
        mouth_path = pack.root / str(pack.manifest.get("mouthConfig") or "")
        indexed = {
            path.resolve()
            for paths in (*pack.clip_paths.values(), *pack.mouth_overlay_paths.values())
            for path in paths
        }
        present = {path.resolve() for path in pack.root.rglob("*.ktx2")}
        if present != indexed:
            raise AssetPackageError("character pack contains unindexed or missing KTX2 textures")
        if any(pack.root.rglob("*.png")):
            raise AssetPackageError("character runtime pack must not contain PNG authoring frames")
        files = {manifest_path.resolve(), graph_path.resolve(), mouth_path.resolve(), *indexed}
    else:
        files: set[Path] = set()
        for relative in spec.paths:
            source = asset_path(asset_root, relative)
            if not source.is_file():
                raise AssetPackageError(f"{spec.id} is missing required bundle file: {relative}")
            if source.is_symlink():
                raise AssetPackageError(f"asset bundle source must not be a symlink: {source}")
            files.add(source.resolve())
        for relative in spec.trees:
            tree = asset_path(asset_root, relative)
            if not tree.is_dir():
                raise AssetPackageError(f"{spec.id} is missing required bundle tree: {relative}")
            for source in tree.rglob("*"):
                if source.is_symlink():
                    raise AssetPackageError(f"asset bundle source must not be a symlink: {source}")
                if source.is_file():
                    files.add(source.resolve())

    for required in spec.required:
        if not asset_path(asset_root, required).is_file():
            raise AssetPackageError(f"{spec.id} is incomplete: missing {required}")
    return tuple(sorted(files, key=lambda item: item.relative_to(project_root).as_posix()))


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _copy_and_hash(source: BinaryIO, target: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(BUFFER_SIZE):
        target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def build_bundle(
    *,
    project_root: Path,
    output: Path,
    pack_ids: Iterable[str],
    index_path: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    output = Path(output).resolve()
    if output.exists() and not overwrite:
        raise AssetPackageError(f"output already exists: {output}")
    try:
        output.relative_to(project_root / "assets")
    except ValueError:
        pass
    else:
        raise AssetPackageError("bundle output must not be written inside the asset source tree")
    specs = asset_pack_specs(index_path or project_root / "assets" / "index.json")
    selected_ids = tuple(dict.fromkeys(str(pack_id) for pack_id in pack_ids))
    if not selected_ids:
        raise AssetPackageError("at least one external pack id is required")

    selected: list[AssetPackSpec] = []
    sources: dict[PurePosixPath, Path] = {}
    for pack_id in selected_ids:
        try:
            spec = specs[pack_id]
        except KeyError as exc:
            raise AssetPackageError(f"unknown external asset pack: {pack_id}") from exc
        selected.append(spec)
        for source in _resolved_source_files(project_root, spec):
            relative = PurePosixPath(source.relative_to(project_root).as_posix())
            normalized = _safe_archive_path(relative.as_posix())
            asset_relative = _asset_relative(normalized)
            if not path_belongs_to_pack(asset_relative, spec):
                raise AssetPackageError(f"source file is outside pack {pack_id}: {relative}")
            existing = sources.get(normalized)
            if existing is not None and existing != source:
                raise AssetPackageError(f"two source files map to {normalized}")
            sources[normalized] = source

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    records: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
            for relative in sorted(sources, key=lambda item: item.as_posix()):
                source = sources[relative]
                with source.open("rb") as input_stream:
                    with archive.open(_zip_info(relative.as_posix()), "w", force_zip64=True) as output_stream:
                        size, digest = _copy_and_hash(input_stream, output_stream)
                records.append({"path": relative.as_posix(), "size": size, "sha256": digest})

            manifest = {
                "format": ASSET_BUNDLE_FORMAT,
                "packs": [
                    {"id": spec.id, "spec_version": spec.spec_version} for spec in selected
                ],
                "file_count": len(records),
                "total_bytes": sum(record["size"] for record in records),
                "files": records,
            }
            manifest_bytes = json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8") + b"\n"
            archive.writestr(_zip_info(BUNDLE_MANIFEST_NAME), manifest_bytes)
        os.replace(temporary, output)
        return manifest
    finally:
        if temporary.exists():
            temporary.unlink()


def _is_special_zip_member(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return bool(mode and not stat.S_ISREG(mode))


def _read_bundle_contract(
    archive_path: Path,
    *,
    index_path: Path,
) -> tuple[dict[str, Any], tuple[AssetPackSpec, ...], dict[str, zipfile.ZipInfo]]:
    specs = asset_pack_specs(index_path)
    with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
        infos = archive.infolist()
        by_name: dict[str, zipfile.ZipInfo] = {}
        casefolded: set[str] = set()
        for info in infos:
            normalized = _safe_archive_path(
                info.filename,
                allow_manifest=info.filename == BUNDLE_MANIFEST_NAME,
            ).as_posix()
            folded = normalized.casefold()
            if normalized in by_name or folded in casefolded:
                raise AssetPackageError(f"duplicate bundle member: {normalized}")
            if (
                info.is_dir()
                or _is_special_zip_member(info)
                or info.flag_bits & 0x1
                or info.compress_type != zipfile.ZIP_STORED
            ):
                raise AssetPackageError(f"bundle contains an unsupported member: {normalized}")
            by_name[normalized] = info
            casefolded.add(folded)

        manifest_info = by_name.get(BUNDLE_MANIFEST_NAME)
        if manifest_info is None or manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise AssetPackageError("bundle manifest is missing or too large")
        try:
            manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetPackageError(f"bundle manifest is invalid: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("format") != ASSET_BUNDLE_FORMAT:
        raise AssetPackageError("unsupported external asset bundle format")
    raw_packs = manifest.get("packs")
    raw_files = manifest.get("files")
    if not isinstance(raw_packs, list) or not raw_packs:
        raise AssetPackageError("bundle must declare at least one pack")
    if not isinstance(raw_files, list) or not raw_files:
        raise AssetPackageError("bundle must declare at least one file")

    selected: list[AssetPackSpec] = []
    selected_ids: set[str] = set()
    for raw_pack in raw_packs:
        if not isinstance(raw_pack, dict):
            raise AssetPackageError("bundle pack entries must be objects")
        pack_id = str(raw_pack.get("id") or "")
        if pack_id in selected_ids or pack_id not in specs:
            raise AssetPackageError(f"bundle declares an unknown or duplicate pack: {pack_id}")
        spec = specs[pack_id]
        if raw_pack.get("spec_version") != spec.spec_version:
            raise AssetPackageError(f"bundle pack contract is incompatible: {pack_id}")
        selected.append(spec)
        selected_ids.add(pack_id)

    declared: dict[str, dict[str, Any]] = {}
    declared_casefolded: set[str] = set()
    total_bytes = 0
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise AssetPackageError("bundle file entries must be objects")
        path = _safe_archive_path(raw_file.get("path")).as_posix()
        if path.casefold() in declared_casefolded:
            raise AssetPackageError(f"bundle manifest contains a duplicate path: {path}")
        try:
            size = int(raw_file.get("size"))
        except (TypeError, ValueError) as exc:
            raise AssetPackageError(f"bundle file has an invalid size: {path}") from exc
        digest = str(raw_file.get("sha256") or "").lower()
        if size < 0 or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise AssetPackageError(f"bundle file metadata is invalid: {path}")
        info = by_name.get(path)
        if info is None or info.file_size != size:
            raise AssetPackageError(f"bundle file size or membership does not match: {path}")
        relative = _asset_relative(PurePosixPath(path))
        if not any(path_belongs_to_pack(relative, spec) for spec in selected):
            raise AssetPackageError(f"bundle file is outside its declared packs: {path}")
        declared[path] = {"path": path, "size": size, "sha256": digest}
        declared_casefolded.add(path.casefold())
        total_bytes += size

    if total_bytes > MAX_BUNDLE_BYTES:
        raise AssetPackageError("bundle exceeds the supported size limit")
    if manifest.get("file_count") != len(declared) or manifest.get("total_bytes") != total_bytes:
        raise AssetPackageError("bundle summary does not match its file records")
    if set(by_name) != {BUNDLE_MANIFEST_NAME, *declared}:
        raise AssetPackageError("bundle contains files not declared by its manifest")
    return manifest, tuple(selected), by_name


def _extract_verified_bundle(
    archive_path: Path,
    destination: Path,
    *,
    index_path: Path,
) -> tuple[dict[str, Any], tuple[AssetPackSpec, ...]]:
    manifest, selected, _ = _read_bundle_contract(archive_path, index_path=index_path)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as archive:
            for record in manifest["files"]:
                relative = _safe_archive_path(record["path"])
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(relative.as_posix(), "r") as source, target.open("xb") as output:
                    while chunk := source.read(BUFFER_SIZE):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise AssetPackageError(f"bundle checksum failed: {relative}")

        staged_assets = destination / "assets"
        for spec in selected:
            for required in spec.required:
                if not asset_path(staged_assets, required).is_file():
                    raise AssetPackageError(f"bundle is incomplete for {spec.id}: {required}")
            if spec.validator == "character_pack":
                load_character_pack(asset_path(staged_assets, spec.trees[0]))
            else:
                status = external_asset_pack_status(
                    spec.id,
                    asset_root=staged_assets,
                    index_path=index_path,
                )
                if not status["installed"]:
                    raise AssetPackageError(
                        f"bundle validation failed for {spec.id}: "
                        f"{status.get('message') or status['state']}"
                    )
        return manifest, selected
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def verify_bundle(
    archive_path: Path,
    *,
    index_path: Path = ASSET_INDEX_PATH,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="amadeus-asset-verify-") as temporary:
        manifest, _ = _extract_verified_bundle(
            Path(archive_path).resolve(),
            Path(temporary) / "staged",
            index_path=Path(index_path),
        )
    return manifest


def install_bundle(
    archive_path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    index_path: Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    project_root = Path(project_root).resolve()
    index_path = Path(index_path or project_root / "assets" / "index.json")
    staging = project_root / "runtime" / f"asset-install-{uuid.uuid4().hex}"
    manifest, selected = _extract_verified_bundle(
        Path(archive_path).resolve(),
        staging,
        index_path=index_path,
    )

    actions: list[tuple[dict[str, Any], Path, Path]] = []
    skipped = 0
    try:
        for record in manifest["files"]:
            relative = _safe_archive_path(record["path"])
            source = staging.joinpath(*relative.parts)
            target = project_root.joinpath(*relative.parts)
            resolved_parent = target.parent.resolve()
            try:
                resolved_parent.relative_to(project_root)
            except ValueError as exc:
                raise AssetPackageError(f"install target escapes the repository: {relative}") from exc
            if target.exists():
                if not target.is_file():
                    raise AssetPackageError(f"install target is not a regular file: {relative}")
                if _sha256_file(target) == record["sha256"]:
                    skipped += 1
                    continue
                if not overwrite:
                    raise AssetPackageError(
                        f"install would replace a different local asset: {relative}; "
                        "rerun with --overwrite only if that is intentional"
                    )
            actions.append((record, source, target))

        backup_root = staging / ".backup"
        committed: list[tuple[Path, Path | None]] = []
        try:
            for _, source, target in actions:
                target.parent.mkdir(parents=True, exist_ok=True)
                backup: Path | None = None
                if target.exists():
                    relative = target.relative_to(project_root)
                    backup = backup_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, backup)
                try:
                    os.replace(source, target)
                except Exception:
                    if backup is not None and backup.exists():
                        os.replace(backup, target)
                    raise
                committed.append((target, backup))
        except Exception:
            for target, backup in reversed(committed):
                if target.exists():
                    target.unlink()
                if backup is not None and backup.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
            raise

        return {
            "packs": [spec.id for spec in selected],
            "installed_files": len(actions),
            "unchanged_files": skipped,
            "total_files": len(manifest["files"]),
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _status_rows(project_root: Path, index_path: Path) -> list[dict[str, Any]]:
    specs = asset_pack_specs(index_path)
    rows = []
    for spec in specs.values():
        status = external_asset_pack_status(
            spec.id,
            asset_root=project_root / "assets",
            index_path=index_path,
        )
        if status["installed"] and spec.validator == "character_pack":
            try:
                pack = load_character_pack(asset_path(project_root / "assets", spec.trees[0]))
                status["version"] = str(pack.manifest.get("version") or "")
                status["frame_count"] = sum(len(paths) for paths in pack.clip_paths.values())
            except CharacterPackError as exc:
                status.update(installed=False, state="invalid", message=str(exc))
        rows.append(status)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="show installed external asset packs")
    status_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    build_parser = subparsers.add_parser("build", help="build an external asset bundle")
    build_parser.add_argument("pack_ids", nargs="+", help="pack ids from assets/index.json")
    build_parser.add_argument("--output", "-o", type=Path, required=True)
    build_parser.add_argument("--overwrite", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="verify a bundle without installing it")
    verify_parser.add_argument("archive", type=Path)

    install_parser = subparsers.add_parser("install", help="install a verified bundle")
    install_parser.add_argument("archive", type=Path)
    install_parser.add_argument("--overwrite", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "status":
            rows = _status_rows(PROJECT_ROOT, ASSET_INDEX_PATH)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(
                        f"{row['id']}: {row['state']} "
                        f"({row['present_required']}/{row['required_count']} required files)"
                    )
            return 0
        if args.command == "build":
            manifest = build_bundle(
                project_root=PROJECT_ROOT,
                output=args.output,
                pack_ids=args.pack_ids,
                overwrite=args.overwrite,
            )
            print(
                f"OK: {args.output.resolve()} | packs={len(manifest['packs'])} "
                f"files={manifest['file_count']} bytes={manifest['total_bytes']}"
            )
            return 0
        if args.command == "verify":
            manifest = verify_bundle(args.archive)
            print(
                f"OK: packs={len(manifest['packs'])} files={manifest['file_count']} "
                f"bytes={manifest['total_bytes']}"
            )
            return 0
        result = install_bundle(args.archive, overwrite=args.overwrite)
        print(
            f"OK: packs={','.join(result['packs'])} installed={result['installed_files']} "
            f"unchanged={result['unchanged_files']}"
        )
        return 0
    except (AssetPackageError, CharacterPackError, OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
