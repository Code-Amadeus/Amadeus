"""Build or verify the pinned wheel from the independent Eidema repository.

Builds consume an explicit committed revision, never the source checkout's dirty
files. Two isolated builds must agree byte for byte before the wheel and its
provenance are replaced. Normal installs need only the checked-in bundle.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path("vendor/eidema")
SCHEMA = "amadeus.eidema-wheel.v1"
ARCHIVE_CONFIG = {"core.autocrlf": "false", "core.eol": "lf"}
POLICY_READER = Path("server/memory_policy_metadata.py")
POLICY_MEMBER = "eidema/policy_metadata.py"


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command, cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


def wheel_metadata(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ValueError("Eidema wheel must contain exactly one package metadata file")
        metadata = BytesParser().parsebytes(archive.read(names[0]))
        wheel = BytesParser().parsebytes(archive.read(names[0].rsplit("/", 1)[0] + "/WHEEL"))
    if metadata["Name"] != "eidema" or wheel["Root-Is-Purelib"] != "true":
        raise ValueError("Expected the pure-Python Eidema core wheel")
    if wheel.get_all("Tag") != ["py3-none-any"]:
        raise ValueError("The bundled Eidema wheel must be platform independent")
    requirements = metadata.get_all("Requires-Dist", [])
    default_requirements = [item for item in requirements if "extra ==" not in item]
    if default_requirements != ["numpy>=1.24"]:
        raise ValueError("Eidema core must require only the existing NumPy dependency")
    return {"name": metadata["Name"], "version": metadata["Version"],
            "requires_python": metadata["Requires-Python"], "requires_dist": requirements}


def _check_bundle(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / BUNDLE / "provenance.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unsupported Eidema wheel provenance schema")
    if manifest.get("build", {}).get("archive_config") != ARCHIVE_CONFIG:
        raise ValueError("Eidema source export must pin the recorded Git line-ending configuration")
    source = manifest["source"]
    for field in ("revision", "tree"):
        if not re.fullmatch(r"[0-9a-f]{40}", source[field]):
            raise ValueError(f"Eidema source {field} must be a full Git object ID")
    artifact = manifest["artifact"]
    filename = artifact["filename"]
    version = manifest["package"]["version"]
    if filename != f"eidema-{version}-py3-none-any.whl":
        raise ValueError("Eidema wheel filename does not match its recorded version")
    path = BUNDLE / filename
    data = (root / path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if artifact["sha256"] != digest or artifact["size"] != len(data):
        raise ValueError("Eidema wheel bytes differ from the recorded provenance")
    if wheel_metadata(root / path) != manifest["package"]:
        raise ValueError("Eidema wheel metadata differs from the recorded provenance")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    if f"eidema=={version}" not in project["project"]["dependencies"]:
        raise ValueError("T1 must declare the exact bundled Eidema version")
    if project["tool"]["uv"]["sources"].get("eidema") != {"path": path.as_posix()}:
        raise ValueError("Eidema must resolve from the relative bundled wheel")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = [package for package in lock["package"] if package["name"] == "eidema"]
    if len(packages) != 1 or packages[0]["version"] != version:
        raise ValueError("uv.lock must contain the bundled Eidema version")
    package = packages[0]
    if package["source"] != {"path": path.as_posix()}:
        raise ValueError("uv.lock must resolve Eidema from the relative bundled wheel")
    if not any(wheel.get("hash") == f"sha256:{digest}" for wheel in package.get("wheels", [])):
        raise ValueError("uv.lock must retain the bundled Eidema wheel hash")
    return manifest


def _policy_reader(wheel: Path) -> tuple[bytes, dict[str, Any]] | None:
    with zipfile.ZipFile(wheel) as archive:
        if POLICY_MEMBER not in archive.namelist():
            return None  # Older SDK releases have no generated Host reader.
        data = archive.read(POLICY_MEMBER)
    assignments = [node for node in ast.parse(data).body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "CONTRACT_VERSION"
                           for target in node.targets)]
    if len(assignments) != 1:
        raise ValueError("Eidema policy reader must declare one contract version")
    contract = ast.literal_eval(assignments[0].value)
    if not isinstance(contract, str) or not contract:
        raise ValueError("Invalid Eidema policy reader contract")
    return data, {"path": POLICY_READER.as_posix(), "wheel_member": POLICY_MEMBER,
                  "contract": contract, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def check_bundle(root: Path = ROOT) -> dict[str, Any]:
    manifest = _check_bundle(root)
    reader = _policy_reader(root / BUNDLE / manifest["artifact"]["filename"])
    if reader is None:
        if "host_policy_metadata" in manifest:
            raise ValueError("Eidema wheel has no declared Host policy reader")
    else:
        data, record = reader
        if (manifest.get("host_policy_metadata") != record or not (root / POLICY_READER).is_file()
                or (root / POLICY_READER).read_bytes() != data):
            raise ValueError("Host policy reader differs from the bundled Eidema wheel")
    return manifest


def _write_policy_reader(root: Path, manifest: dict[str, Any]) -> None:
    reader = _policy_reader(root / BUNDLE / manifest["artifact"]["filename"])
    if reader is not None:
        data, record = reader
        (root / POLICY_READER).parent.mkdir(parents=True, exist_ok=True)
        (root / POLICY_READER).write_bytes(data)
        manifest["host_policy_metadata"] = record
    (root / BUNDLE / "provenance.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n",
    )


def sync_policy_metadata(root: Path = ROOT) -> dict[str, Any]:
    """Generate from a verified prebuilt bundle without importing the SDK."""
    manifest = _check_bundle(root)
    _write_policy_reader(root, manifest)
    return manifest


def build_bundle(source: Path, revision: str, *, root: Path = ROOT) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Supply the full reviewed Eidema commit ID")
    committed = _run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=source)
    if committed != revision:
        raise ValueError("Eidema revision did not resolve to the supplied commit")
    tree = _run(["git", "rev-parse", f"{revision}^{{tree}}"], cwd=source)
    epoch = int(_run(["git", "show", "-s", "--format=%ct", revision], cwd=source))
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to build the Eidema wheel")
    # Git archive applies text conversion too. The export must not inherit the
    # builder's checkout line-ending preferences (notably Windows autocrlf).
    archive_command = ["git"]
    for name, value in ARCHIVE_CONFIG.items():
        archive_command.extend(["-c", f"{name}={value}"])
    archive = subprocess.run(
        [*archive_command, "archive", "--format=tar", revision], cwd=source,
        check=True, capture_output=True,
    ).stdout
    environment = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "PYTHONHASHSEED": "0"}
    with tempfile.TemporaryDirectory(prefix="amadeus-eidema-build-") as temporary:
        work = Path(temporary)
        wheels = []
        for index in range(2):
            snapshot = work / f"source-{index}"
            snapshot.mkdir()
            with tarfile.open(fileobj=io.BytesIO(archive)) as source_archive:
                source_archive.extractall(snapshot, filter="data")
            output = work / f"dist-{index}"
            _run([
                uv, "build", "--wheel", "--offline", "--no-build-isolation",
                "--no-python-downloads", "--no-create-gitignore", "--python", sys.executable,
                "--out-dir", str(output), str(snapshot),
            ], cwd=root, env=environment)
            artifacts = list(output.glob("*.whl"))
            if len(artifacts) != 1:
                raise ValueError("Expected exactly one Eidema wheel from each build")
            wheels.append(artifacts[0])
        data = wheels[0].read_bytes()
        if wheels[0].name != wheels[1].name or data != wheels[1].read_bytes():
            raise ValueError("Isolated Eidema builds are not byte reproducible")
        metadata = wheel_metadata(wheels[0])
        manifest = {
            "schema": SCHEMA,
            "source": {"project": "Eidema", "revision": revision, "tree": tree},
            "source_date_epoch": epoch,
            "build": {
                "command": "python tools/package_eidema.py build --source <Eidema-checkout> --revision <reviewed-commit>",
                "python": platform.python_version(),
                "setuptools": importlib.metadata.version("setuptools"),
                "wheel": importlib.metadata.version("wheel"),
                "uv": _run([uv, "--version"], cwd=root),
                "reproducible_builds": 2,
                "archive_config": ARCHIVE_CONFIG,
            },
            "package": metadata,
            "artifact": {"filename": wheels[0].name,
                         "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        }
    destination = root / BUNDLE
    destination.mkdir(parents=True, exist_ok=True)
    (destination / manifest["artifact"]["filename"]).write_bytes(data)
    _write_policy_reader(root, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build twice from an explicit reviewed commit")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--revision", required=True)
    commands.add_parser("check", help="check bundle provenance, declaration and locked hash")
    commands.add_parser("sync-policy-metadata", help="copy the stdlib reader from the verified bundled wheel")
    args = parser.parse_args()
    if args.command == "build":
        manifest = build_bundle(args.source.resolve(), args.revision)
    elif args.command == "sync-policy-metadata":
        manifest = sync_policy_metadata()
    else:
        manifest = check_bundle()
    artifact = manifest["artifact"]
    print(f"Eidema {manifest['package']['version']}: {artifact['filename']} sha256:{artifact['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
