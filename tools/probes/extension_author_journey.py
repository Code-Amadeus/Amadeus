"""Offline four-owner author journey using real wheels, installer, SDK and native checks."""

from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from config.subprocess_environment import isolated_child_environment
from config.process_tree import ProcessTree
from tools.probes.extension_theme_lifecycle import build_wheel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/extension-author-journey")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This combined installer/native GUI journey currently qualifies Windows only")
    base = args.output_dir.resolve() / uuid.uuid4().hex[:12]
    if not base.is_relative_to(ROOT / "build"):
        parser.error("Output must remain under this checkout build directory")
    base.mkdir(parents=True)
    wheels = base / "wheels"
    wheels.mkdir()
    env = isolated_child_environment(
        extra={
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "AMADEUS_TEST_PYTHON": sys.executable,
            "AMADEUS_AUTHOR_JOURNEY_ROOT": str(base),
            "PIP_NO_INDEX": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_FIND_LINKS": str(ROOT / "vendor/extension-sdk"),
        }
    )
    cases = [
        ("tone", "tone-tts", "tts", "amadeus_example_tone_tts"),
        ("notes", "notes-mcp", "mcp", "amadeus_example_notes_mcp"),
        ("handoff", "handoff-skill", "skill", "amadeus_example_handoff_skill"),
        ("theme", "desktop-theme", "theme", "amadeus_example_theme"),
    ]
    report = {"ok": False, "checks": [], "nativeDialogs": False, "remoteServices": False}

    def run(name, command, environment=env, timeout=180):
        with (base / (name + ".log")).open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            tree = ProcessTree(process)
            try:
                tree.resume()
                code = process.wait(timeout=timeout)
            finally:
                tree.terminate()
        if code:
            raise RuntimeError(name)
        report["checks"].append(name)

    try:
        for name, example, _, _ in cases:
            for version in ["0.1.0", "0.2.0"]:
                source = base / (name + "-" + version + "-source")
                shutil.copytree(
                    ROOT / "examples/extensions" / example,
                    source,
                    ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"),
                )
                project = source / "pyproject.toml"
                project.write_text(
                    project.read_text(encoding="utf-8").replace(
                        'version = "0.1.0"', f'version = "{version}"'
                    ),
                    encoding="utf-8",
                )
                (manifest_path,) = source.glob("src/*/amadeus-extension.json")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["version"] = version
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                if name == "handoff" and version == "0.2.0":
                    reference = manifest_path.parent / "skills/handoff-brief/references/layout.json"
                    reference.write_text(
                        reference.read_text(encoding="utf-8").replace(
                            "handoff-layout-v1", "handoff-layout-v2"
                        ),
                        encoding="utf-8",
                    )
                build_wheel(source, wheels, env)
        app = base / "installer-app"
        app.mkdir()
        (app / "package.json").write_text(
            json.dumps(
                {
                    "name": "author-installer-probe",
                    "version": "0.0.0",
                    "type": "module",
                    "main": str(ROOT / "electron/tests/extensionAuthoring.smoke.mjs"),
                }
            ),
            encoding="utf-8",
        )
        driver = [str(ROOT / "electron/node_modules/electron/dist/electron.exe"), str(app)]
        for version, phase in [("0.1.0", "--prepare"), ("0.2.0", "--update")]:
            run("installer" + phase, driver + [phase])
            for name, _, kind, distribution in cases:
                run(
                    name + "-" + version,
                    [
                        sys.executable,
                        "-I",
                        "tools/check_extension_contract.py",
                        "--execute",
                        "--kind",
                        kind,
                        "--wheel",
                        str(wheels / f"{distribution}-{version}-py3-none-any.whl"),
                        "--record",
                        str(base / f"{name}-{version}.json"),
                        "--scenario",
                        str(ROOT / f"examples/extensions/contract-scenarios/{name}.json"),
                        "--output",
                        str(base / f"{name}-{version}-report.json"),
                    ],
                )
        native_env = {
            **env,
            "AMADEUS_TEST_SKILL_SNAPSHOTS": str(base),
            "AMADEUS_TEST_AUTHOR_SNAPSHOTS": str(base),
        }
        run(
            "native-provider-regressions",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--tb=short",
                "tests/test_skill_pi_runtime.py",
                "tests/test_extension_contract.py",
            ],
            native_env,
            300,
        )
        run("installer--finish", driver + ["--finish"])
        report["ok"] = True
    except (RuntimeError, subprocess.SubprocessError) as error:
        report["failed_stage"] = (
            str(error) if isinstance(error, RuntimeError) else type(error).__name__
        )
    (base / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(base / "report.json"), **report}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
