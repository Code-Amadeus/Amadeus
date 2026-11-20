"""Rebuild the checked-in PixiJS KTX2 browser shim from pinned npm sources.

This is a maintainer tool, not an install-time step. The application ships the
generated files so ordinary users do not need Node.js or network access.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_VERSION = "0.0.22"
ESBUILD_VERSION = "0.20.0"
PACKAGE_URL = (
    "https://registry.npmjs.org/pixi-basis-ktx2/-/"
    f"pixi-basis-ktx2-{PACKAGE_VERSION}.tgz"
)
PACKAGE_SHA512 = base64.b64decode(
    "/1fa2YjjfYTG3AZlvnLu1B0uZPgXuFY2zijoy/VoUwGokHQ6HwLy0LtVVxFWK9Vwl5zGSRJnGpEV0/34ydKfog=="
)

OUTPUT_PATHS = {
    "bundle": ROOT / "render" / "web" / "vendor" / "pixi-basis-ktx2.global.js",
    "transcoder_js": ROOT / "render" / "web" / "vendor" / "basis_transcoder.js",
    "transcoder_wasm": ROOT / "render" / "web" / "vendor" / "basis_transcoder.wasm",
    "license": ROOT / "LICENSES" / "pixi-basis-ktx2-MIT.txt",
}
TEXT_OUTPUTS = {"bundle", "transcoder_js", "license"}


def _download_package() -> bytes:
    request = urllib.request.Request(PACKAGE_URL, headers={"User-Agent": "Amadeus-vendor-tool"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    if hashlib.sha512(payload).digest() != PACKAGE_SHA512:
        raise RuntimeError("pixi-basis-ktx2 npm tarball integrity mismatch")
    return payload


def _extract_package(payload: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError(f"unsafe npm archive member: {member.name}")
        archive.extractall(destination, members=members, filter="data")


def _build_files() -> dict[str, bytes]:
    payload = _download_package()
    with tempfile.TemporaryDirectory(prefix="amadeus-pixi-basis-") as raw_temp:
        workspace = Path(raw_temp)
        source_root = workspace / "tmp"
        source_root.mkdir()
        _extract_package(payload, source_root)

        (source_root / "pixi-global-shim.js").write_text(
            '"use strict";\nmodule.exports = globalThis.PIXI;\n',
            encoding="utf-8",
            newline="\n",
        )
        entry = source_root / "pixi_basis_ktx2_global_entry.js"
        entry.write_text(
            '"use strict";\n'
            'const loader = require("./package/lib/cjs/index.js");\n'
            "globalThis.PixiBasisKtx2Shim = loader;\n",
            encoding="utf-8",
            newline="\n",
        )

        output_root = workspace / "out"
        output_root.mkdir()
        bundle = output_root / "pixi-basis-ktx2.global.js"
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required to rebuild the Pixi KTX2 vendor bundle")
        subprocess.run(
            [
                npm,
                "exec",
                "--yes",
                f"--package=esbuild@{ESBUILD_VERSION}",
                "--",
                "esbuild",
                "tmp/pixi_basis_ktx2_global_entry.js",
                "--bundle",
                "--platform=browser",
                "--format=iife",
                "--alias:@pixi/assets=./tmp/pixi-global-shim.js",
                "--alias:@pixi/compressed-textures=./tmp/pixi-global-shim.js",
                "--alias:@pixi/core=./tmp/pixi-global-shim.js",
                f"--outfile={bundle}",
                "--log-level=warning",
            ],
            cwd=workspace,
            check=True,
        )

        package_root = source_root / "package"
        transcoder_js = (package_root / "assets" / "basis_transcoder.js").read_bytes()
        # npm 0.0.22 ends this generated file with four stray spaces. Normalize
        # only that trailing whitespace so checkouts remain diff-clean.
        transcoder_js = transcoder_js.rstrip(b" \t\r\n") + b"\n"
        return {
            "bundle": bundle.read_bytes(),
            "transcoder_js": transcoder_js,
            "transcoder_wasm": (package_root / "assets" / "basis_transcoder.wasm").read_bytes(),
            "license": (package_root / "LICENSE").read_bytes(),
        }


def _check(generated: dict[str, bytes]) -> int:
    mismatches: list[str] = []
    for key, path in OUTPUT_PATHS.items():
        if not path.is_file():
            mismatches.append(path.relative_to(ROOT).as_posix())
            continue
        actual = path.read_bytes()
        expected = generated[key]
        if key in TEXT_OUTPUTS:
            actual = actual.replace(b"\r\n", b"\n")
            expected = expected.replace(b"\r\n", b"\n")
        if actual != expected:
            mismatches.append(path.relative_to(ROOT).as_posix())
    if mismatches:
        print("Pixi KTX2 vendor files differ:")
        for path in mismatches:
            print(f"- {path}")
        return 1
    print(f"Pixi KTX2 vendor files match pixi-basis-ktx2@{PACKAGE_VERSION}.")
    return 0


def _write(generated: dict[str, bytes]) -> int:
    for key, path in OUTPUT_PATHS.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(generated[key])
        print(f"updated {path.relative_to(ROOT).as_posix()}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify checked-in files (default)")
    mode.add_argument("--write", action="store_true", help="replace checked-in files")
    args = parser.parse_args()
    generated = _build_files()
    return _write(generated) if args.write else _check(generated)


if __name__ == "__main__":
    raise SystemExit(main())
