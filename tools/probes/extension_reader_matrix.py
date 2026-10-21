"""Run unmodified pinned catalog readers against a future-entry fixture (no plugin execution)."""

from __future__ import annotations
import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--ref", action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    index = json.loads(args.fixture.read_text(encoding="utf-8"))["index"]
    cases = json.loads(
        (ROOT / "vendor/extension-catalog/catalog/fixtures/compatibility.json").read_text()
    )["cases"]
    baseline = copy.deepcopy(index["entries"][0])
    expected = []
    for case in cases:
        entry = copy.deepcopy(baseline)
        entry["id"] += "." + case["name"]
        entry["publisher"]["package_ids"] = [entry["id"]]
        entry["compatibility"].update(case["compatibility"])
        index["entries"].append(entry)
        expected.append(case["compatible"])
    index["security"] = [
        dict(
            id=baseline["id"],
            version=baseline["version"],
            sha256=baseline["sha256"],
            status="blocked",
            sequence=1,
            reason="security",
            evidence="https://fixture.test/blocked",
        )
    ]
    inputs = {
        "future-entries": index,
        "unknown-format": {**index, "format": "amadeus.catalog/v2"},
        "invalid-security": {**index, "security": [{}]},
    }
    paths = {}
    for name, value in inputs.items():
        path = destination / (name + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[name] = path
    resource = json.loads((ROOT / "electron/package.json").read_text())["build"]["extraResources"]
    patterns = next(item["filter"] for item in resource if item["to"] == "extension-host")
    git_paths = [p.removesuffix("/**/*") for p in patterns]
    report = {
        "fixture_authority": True,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "input_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()
        },
        "readers": [],
    }
    for ref in args.ref:
        commit = subprocess.check_output(
            ["git", "rev-parse", ref + "^{commit}"], cwd=ROOT, text=True
        ).strip()
        snapshot = destination / commit
        snapshot.mkdir()
        files = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", commit, "--", *git_paths], cwd=ROOT, text=True
        ).splitlines()
        hashes = {}
        for name in files:
            data = subprocess.check_output(["git", "show", commit + ":" + name], cwd=ROOT)
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        results = {}
        for name, path in paths.items():
            process = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(snapshot / "tools/extension_package.py"),
                    "validate-catalog",
                    str(path),
                ],
                cwd=snapshot,
                capture_output=True,
                text=True,
                timeout=30,
            )
            value = json.loads(process.stdout)
            if name == "future-entries":
                assert process.returncode == 0 and value["ok"], value
                catalog = value["catalog"]
                assert [
                    not e["incompatible"] for e in catalog["entries"][-len(cases) :]
                ] == expected
                assert all(not e["incompatible"] for e in catalog["entries"][: -len(cases)])
                assert catalog["security"] == index["security"]
                results[name] = {
                    "compatible_neighbors": len(catalog["entries"]) - len(cases),
                    "case_results": dict(zip((c["name"] for c in cases), expected)),
                    "safety_retained": True,
                }
            else:
                assert process.returncode != 0 and not value["ok"], value
                results[name] = "rejected"
        report["readers"].append({"commit": commit, "files": hashes, "results": results})
    (destination / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "readers": len(report["readers"]),
                "report": str(destination / "report.json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
