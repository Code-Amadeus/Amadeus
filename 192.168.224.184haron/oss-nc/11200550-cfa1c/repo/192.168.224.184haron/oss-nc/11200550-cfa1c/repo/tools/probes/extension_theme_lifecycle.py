"""Windows real-owner theme lifecycle smoke; no native dialogs or remote services.

First run npm run build --prefix electron with this Python selected by
AMADEUS_PYTHON. Builds synthetic wheels offline in a fresh profile and runs the
actual installer/runtime/SDK. This does not certify GUI confirmation or playback.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]


def build_wheel(source: Path, destination: Path, environment: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])",
         str(destination)],
        cwd=source, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if result.returncode:
        raise RuntimeError("Synthetic wheel build failed; the selected Python needs setuptools")


def prepare_wheels(base: Path, environment: dict[str, str]) -> None:
    wheels = base / "wheels"
    wheels.mkdir()
    example = ROOT / "examples/extensions/desktop-theme"
    manifest_relative = Path("src/amadeus_example_theme/amadeus-extension.json")
    original = json.loads((example / manifest_relative).read_text(encoding="utf-8"))
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        source = base / f"theme-{version}"
        shutil.copytree(example, source, ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"))
        project = source / "pyproject.toml"
        project.write_text(project.read_text(encoding="utf-8").replace('version = "0.1.0"', f'version = "{version}"'), encoding="utf-8")
        manifest = copy.deepcopy(original)
        manifest["version"] = version
        if version != "0.1.0":
            manifest["contributes"]["theme"][0]["tokens"].update({
                "--bg": "#F2EAF5", "--accent": "#5D285E", "--accent-hover": "#4A1C4B", "--font": "Segoe UI",
            })
        if version == "0.3.0":
            manifest["contributes"]["theme"][0]["tokens"]["--text"] = "#F2EAF5"
        (source / manifest_relative).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        build_wheel(source, wheels, environment)

    tone = (ROOT / "tests/fixtures/extension_theme_tone.py").read_text(encoding="utf-8")
    for version in ("0.1.0", "0.2.0"):
        source = base / f"mixed-{version}"
        package = source / "src/amadeus_gui_tone"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(tone.replace("VERSION_VALUE", version), encoding="utf-8")
        theme = copy.deepcopy(original["contributes"]["theme"][0 if version == "0.1.0" else 1])
        theme["id"] = "mixed"
        if version == "0.2.0":
            theme["tokens"]["--font"] = "Segoe UI"
        manifest = {
            "schema": "amadeus.extension/v0", "id": "org.example.gui-mixed-theme", "version": version,
            "display_name": "Synthetic theme and tone", "description": "Local lifecycle probe; no speech service.",
            "author": {"name": "Amadeus test fixture"}, "engines": {"amadeus_host_api": ">=0.1,<0.2"},
            "runtime": {"kind": "python", "entry": "amadeus_gui_tone:extension", "requires_python": ">=3.12"},
            "contributes": {
                "theme": [theme],
                "voice_backend": [{"id": "tone", "role": "tts", "contract": "amadeus.voice.tts/v0",
                                   "deployment": "local", "languages": ["ja-JP", "en-US"], "streaming": True}],
            },
            "permissions": {"os": {"filesystem": [{"path": "${paths.data}", "access": "read_write"}]}},
        }
        (package / "amadeus-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        shutil.copyfile(example / "src/amadeus_example_theme/background.png", package / "background.png")
        (source / "pyproject.toml").write_text(
            '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n'
            f'[project]\nname="amadeus-gui-tone"\nversion="{version}"\nrequires-python=">=3.12"\n'
            'dependencies=["amadeus-extension-sdk==0.1.0"]\n'
            '[project.entry-points."amadeus.extensions"]\n"org.example.gui-mixed-theme"="amadeus_gui_tone"\n'
            '[tool.setuptools.packages.find]\nwhere=["src"]\n'
            '[tool.setuptools.package-data]\namadeus_gui_tone=["amadeus-extension.json","background.png"]\n',
            encoding="utf-8",
        )
        build_wheel(source, wheels, environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/extension-theme-lifecycle")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This native image/font/process probe currently qualifies Windows only")
    electron = (ROOT / "electron/node_modules/electron/dist/electron.exe").resolve()
    if not electron.is_file() or not (ROOT / "electron/dist/main/extensions.js").is_file():
        parser.error("Build the Electron app before running this probe")
    sdk = ROOT / "vendor/extension-sdk"
    sdk_pin = json.loads((ROOT / "server/extensions/protocol-source.json").read_text(encoding="utf-8"))["sdk"]
    if not (sdk / sdk_pin["filename"]).is_file():
        parser.error("The pinned SDK wheel must be present in vendor/extension-sdk")

    allowed = {"SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "Path", "PATHEXT",
               "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.update({
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(sdk),
        "AMADEUS_PYTHON": sys.executable,
    })
    base = args.output_dir.resolve() / uuid.uuid4().hex[:12]
    base.mkdir(parents=True, exist_ok=False)
    prepare_wheels(base, environment)
    environment["AMADEUS_THEME_PROBE_DIR"] = str(base)
    app_dir = base / "app"
    app_dir.mkdir()
    (app_dir / "package.json").write_text(json.dumps({
        "name": "amadeus-theme-owner-probe", "version": "0.0.0", "type": "module",
        "main": str(ROOT / "electron/tests/extensionThemeLifecycle.smoke.mjs"),
    }), encoding="utf-8")
    with (base / "electron.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(electron), str(app_dir)], cwd=ROOT,
                                   env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            process.wait(timeout=max(30, args.timeout))
        except subprocess.TimeoutExpired:
            # Terminate only the child tree created by this probe.
            import psutil
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            parent.terminate()
            psutil.wait_procs(children + [parent], timeout=5)
            print(f"Lifecycle probe timed out; inspect {base}")
            return 1
    report_path = base / "report.json"
    if not report_path.is_file():
        print(f"Lifecycle probe produced no report; inspect {base}")
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(json.dumps({"report": str(report_path), **report}, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
