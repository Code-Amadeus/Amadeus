"""One static inspection/configuration boundary shared by desktop and runtime."""
from __future__ import annotations

import hashlib
import configparser
import json
import os
import math
import platform
import re
import stat
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from server.extensions.manifest import MANIFEST_LIMIT_BYTES, MANIFEST_NAME, _is_link, manifest_issues, package_issues
from server.extensions.profile import validate_supported_contributions
from server.extensions.sdk import validate_sdk_requirements

HOST_API = "0.1"
MAX_WHEEL_BYTES = 256 * 1024 * 1024
MAX_WHEEL_ENTRIES = 10000


def matches_version(version: str, requirement: str) -> bool:
    """The protocol's numeric, comma-separated comparator grammar (no packaging dependency)."""
    actual = tuple(int(part) for part in version.split("."))
    for comparator in requirement.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*", comparator)
        if match is None:
            return False
        op, text = match.groups()
        wanted = tuple(int(part) for part in text.split("."))
        width = max(len(actual), len(wanted))
        left = actual + (0,) * (width - len(actual))
        right = wanted + (0,) * (width - len(wanted))
        if not {">=": left >= right, "<=": left <= right, "==": left == right,
                "!=": left != right, ">": left > right, "<": left < right}[op]:
            return False
    return True


def validate_manifest(manifest: Any, *, check_environment: bool = True) -> dict[str, Any]:
    try:
        json.dumps(manifest, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("manifest must contain finite JSON values") from None
    issues = manifest_issues(manifest)
    if issues:
        # Structural messages can contain arbitrary input; never echo them with config values.
        raise ValueError(f"invalid manifest: {issues[0].code} at {issues[0].path}")
    if manifest.get("permissions", {}).get("host"):
        raise ValueError("permissions.host is unsupported in this experiment")
    validate_supported_contributions(manifest["contributes"])
    if check_environment:
        engines = manifest["engines"]
        if not matches_version(HOST_API, engines["amadeus_host_api"]):
            raise ValueError("extension requires an incompatible Amadeus Host API")
        if sys.platform not in engines.get("platforms", [sys.platform]):
            raise ValueError("extension does not support this platform")
        runtime = manifest["runtime"]
        if runtime["kind"] == "python" and not matches_version(platform.python_version(), runtime["requires_python"]):
            raise ValueError("extension requires an incompatible Python version")
    return manifest


def validate_config(manifest: dict[str, Any], values: Any, *, allow_incomplete: bool = False) -> tuple[dict[str, Any], list[str]]:
    validate_manifest(manifest)
    if not isinstance(values, dict):
        raise ValueError("configuration must be an object")
    fields = manifest.get("config", {})
    if set(values) - set(fields):
        raise ValueError("configuration contains undeclared fields")
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for key, field in fields.items():
        value = values.get(key, field.get("default"))
        if value is None or value == "":
            if field.get("required"):
                missing.append(key)
            if value == "" and field["type"] in {"string", "url"}:
                resolved[key] = value
            continue
        kind = field["type"]
        valid = (
            isinstance(value, str) if kind in {"string", "url", "enum"} else
            type(value) is bool if kind == "boolean" else
            type(value) is int if kind == "integer" else
            type(value) in {int, float} and math.isfinite(value)
        )
        if not valid:
            raise ValueError(f"invalid configuration type for {key}")
        if kind in {"string", "url"} and len(value) > field.get("max_length", 4096):
            raise ValueError(f"configuration exceeds maximum length for {key}")
        if kind == "url":
            try:
                parsed = urlsplit(value)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"configuration requires an HTTP(S) URL for {key}")
        if kind in {"integer", "number"} and (
            ("min" in field and value < field["min"]) or
            ("max" in field and value > field["max"])
        ):
            raise ValueError(f"configuration outside declared bounds for {key}")
        if kind == "enum" and value not in [option if isinstance(option, str) else option["value"] for option in field["options"]]:
            raise ValueError(f"configuration is not a declared option for {key}")
        resolved[key] = value
    if missing and not allow_incomplete:
        raise ValueError("required configuration is missing: " + ", ".join(missing))
    return resolved, missing


def inspect_wheel(path: Path) -> dict[str, Any]:
    """Inspect bytes and archive paths only. Never import/install package code."""
    path = Path(path)
    if path.suffix.lower() != ".whl" or not path.is_file():
        raise ValueError("an existing local .whl file is required")
    if path.stat().st_size > MAX_WHEEL_BYTES:
        raise ValueError("wheel exceeds the 256 MiB inspection limit")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_WHEEL_ENTRIES or sum(item.file_size for item in members) > MAX_WHEEL_BYTES:
            raise ValueError("wheel exceeds archive limits")
        names: set[str] = set()
        normalized_names: set[str] = set()
        for member in members:
            name = member.filename
            parts = PurePosixPath(name).parts
            raw_parts = name.rstrip("/").split("/")
            normalized = name.rstrip("/").casefold()
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (not parts or name.startswith("/") or "\\" in name or ":" in name or
                    any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in raw_parts) or
                    normalized in normalized_names or mode not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise ValueError("wheel contains unsafe or duplicate archive paths")
            if member.flag_bits & 1:
                raise ValueError("encrypted wheel archives are unsupported")
            # pip loads .pth files during interpreter startup, before our handshake.
            if name.endswith(".pth"):
                raise ValueError("wheel contains an executable Python startup .pth file")
            names.add(name)
            normalized_names.add(normalized)
        manifests = [item for item in members if PurePosixPath(item.filename).name == MANIFEST_NAME]
        metadata_members = [item for item in members if item.filename.endswith(".dist-info/METADATA")]
        if len(manifests) != 1 or len(metadata_members) != 1:
            raise ValueError("wheel must contain exactly one manifest and distribution metadata")
        member = manifests[0]
        if member.file_size > MANIFEST_LIMIT_BYTES:
            raise ValueError("manifest exceeds 256 KiB")
        manifest = validate_manifest(json.loads(archive.read(member).decode("utf-8")))
        package_root = str(PurePosixPath(member.filename).parent)
        if len(PurePosixPath(member.filename).parts) != 2 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package_root):
            raise ValueError("wheel manifest must be directly inside a top-level Python import package")
        entrypoint_members = [item for item in members if item.filename.endswith(".dist-info/entry_points.txt")]
        if (len(entrypoint_members) != 1 or entrypoint_members[0].file_size > MANIFEST_LIMIT_BYTES or
                PurePosixPath(entrypoint_members[0].filename).parent != PurePosixPath(metadata_members[0].filename).parent):
            raise ValueError("wheel must declare its extension entry-point metadata")
        points = configparser.ConfigParser(interpolation=None)
        points.optionxform = str
        try:
            points.read_string(archive.read(entrypoint_members[0]).decode("utf-8"))
            entries = points.items("amadeus.extensions", raw=True)
        except (configparser.Error, UnicodeError):
            raise ValueError("wheel extension entry-point metadata is invalid") from None
        if points.defaults() or entries != [(manifest["id"], package_root)]:
            raise ValueError("wheel must declare exactly one matching amadeus.extensions entry point")
        entry = manifest["runtime"].get("entry")
        if entry and entry.split(":", 1)[0].split(".", 1)[0] != package_root:
            raise ValueError("runtime entry must remain in the manifest's top-level import package")
        for server in manifest["contributes"].get("mcp_server", []):
            command = server.get("command", "")
            if command.startswith("${package}/"):
                location = str(PurePosixPath(package_root) / command[len("${package}/"):])
                if location not in names or archive.getinfo(location).is_dir():
                    raise ValueError("declared package executable is missing from wheel")
        if metadata_members[0].file_size > 1024 * 1024:
            raise ValueError("distribution metadata exceeds inspection limit")
        metadata = Parser().parsestr(archive.read(metadata_members[0]).decode("utf-8"))
        if not metadata.get("Name") or not metadata.get("Version"):
            raise ValueError("distribution metadata lacks identity")
        if metadata.get("Version") != manifest["version"]:
            raise ValueError("wheel version differs from extension manifest")
        if manifest['runtime']['kind'] == 'none' and metadata.get_all('Requires-Dist', []):
            raise ValueError('No-code extensions cannot declare Requires-Dist dependencies')
        validate_sdk_requirements(metadata.get_all("Requires-Dist", []))
        return {"manifest": manifest, "digest": digest, "manifest_member": member.filename,
                "package_root": package_root, "distribution": metadata["Name"],
                "requires_dist": metadata.get_all("Requires-Dist", []), "requires_python": metadata.get("Requires-Python", "")}


def prepare_data_wheel(path: Path, destination: Path) -> dict[str, Any]:
    """Extract inspected resources only; never create an interpreter or invoke pip."""
    inspection = inspect_wheel(path)
    if inspection['manifest']['runtime']['kind'] != 'none':
        raise ValueError('Resource extraction requires runtime.kind none')
    destination = Path(destination)
    if destination.exists() or _is_link(destination):
        raise ValueError('Resource destination must be new')
    destination.mkdir()
    prefix = inspection['package_root'] + '/'
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not member.filename.startswith(prefix):
                continue
            target = destination.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(archive.read(member))
                if os.name != 'nt':
                    # Preserve executable bits, never setuid/setgid or archive write permissions.
                    target.chmod(target.stat().st_mode | ((member.external_attr >> 16) & 0o111))
    validate_installed(inspection['manifest'], destination / inspection['package_root'])
    return inspection


def validate_installed(manifest: dict[str, Any], package_path: Path) -> None:
    validate_manifest(manifest)
    package_path = Path(package_path)
    manifest_path = package_path / MANIFEST_NAME
    if _is_link(package_path) or _is_link(manifest_path) or not manifest_path.is_file():
        raise ValueError("installed package manifest is missing or linked")
    if manifest_path.stat().st_size > MANIFEST_LIMIT_BYTES:
        raise ValueError("installed manifest exceeds 256 KiB")
    try:
        installed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("installed manifest is invalid JSON") from None
    if installed != manifest:
        raise ValueError("installed manifest differs from inspected package")
    entry = manifest["runtime"].get("entry")
    if entry and entry.split(":", 1)[0].split(".", 1)[0] != package_path.name:
        raise ValueError("installed runtime entry is outside the package")
    issues = package_issues(manifest, package_path)
    if issues:
        raise ValueError(f"invalid installed package: {issues[0].code} at {issues[0].path}")
    from agent_host.skills import read_skill_metadata
    for skill in manifest['contributes'].get('skill', ()):
        read_skill_metadata(package_path / skill['path'])
