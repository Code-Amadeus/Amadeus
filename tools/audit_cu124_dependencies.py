"""Audit the observed Windows/Python 3.12/cu124 environment without mutating it.

The tool compares requirement declarations with installed metadata, scans
first-party production imports, records Torch/CUDA/cuDNN facts, runs
``pip check``, and can merge an externally generated ``pip-audit`` JSON file.
It deliberately emits package names and versions rather than ``pip freeze``
direct URLs, so local paths and credentials cannot leak into the snapshot.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from packaging.markers import Marker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYTHON = (
    ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else ROOT / ".venv" / "bin" / "python"
)
# Single source of dependency truth: pyproject.toml declares base + every
# optional tier (voice / vad / local-cu124 / dev); uv.lock is the lockfile.
DEFAULT_DECLARATION_FILE = ROOT / "pyproject.toml"
SCAN_ROOTS = (
    "agent_host",
    "asr",
    "config",
    "core",
    "llm",
    "openclaw",
    "render",
    "server",
    "tts",
    "vn_player",
    "vts",
    "wallpaper",
)
SCAN_FILES = ("local_tts_infer.py",)
EXTERNALLY_MANAGED_REQUIREMENTS = frozenset({"torch"})


ENVIRONMENT_PROBE = r"""
import importlib.metadata as metadata
import json
import platform
import sys

def short_license(dist):
    value = (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "").strip()
    if not value:
        return "NOASSERTION"
    first = value.splitlines()[0].strip()
    if len(value.splitlines()) > 1 or len(first) > 120:
        return "NON-SPDX-METADATA"
    return first

packages = []
for dist in metadata.distributions():
    name = (dist.metadata.get("Name") or "").strip()
    if not name:
        continue
    packages.append({"name": name, "version": dist.version, "license": short_license(dist)})
packages.sort(key=lambda item: item["name"].lower())
package_map = {
    name: sorted(set(values))
    for name, values in metadata.packages_distributions().items()
    if values
}
torch_info = {"installed": False}
try:
    import torch
    torch_info = {
        "installed": True,
        "version": str(torch.__version__),
        "cuda_build": str(torch.version.cuda or ""),
        "cuda_available": bool(torch.cuda.is_available()),
        "gpu_count": int(torch.cuda.device_count()),
        "cudnn_version": int(torch.backends.cudnn.version() or 0),
        "devices": [torch.cuda.get_device_name(index) for index in range(torch.cuda.device_count())],
    }
except Exception as exc:
    torch_info = {"installed": False, "error": f"{type(exc).__name__}: {exc}"}
print(json.dumps({
    "python": {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable_name": __import__("pathlib").Path(sys.executable).name,
    },
    "packages": packages,
    "package_map": package_map,
    "torch": torch_info,
}, ensure_ascii=False))
"""


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


def probe_environment(python: Path) -> dict[str, Any]:
    result = _run([str(python), "-c", ENVIRONMENT_PROBE], check=True)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("environment probe returned a non-object")
    return payload


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def parse_requirement_files(paths: Iterable[Path], marker_environment: dict[str, str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = original.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("-r ", "--requirement ", "--index-url ", "--extra-index-url ")):
                entries.append(
                    {
                        "source": _display_path(path),
                        "line": line_number,
                        "raw": stripped,
                        "kind": "directive",
                    }
                )
                continue
            requirement_text = re.split(r"\s+#", stripped, maxsplit=1)[0].strip()
            try:
                requirement = Requirement(requirement_text)
            except InvalidRequirement as exc:
                entries.append(
                    {
                        "source": _display_path(path),
                        "line": line_number,
                        "raw": stripped,
                        "kind": "invalid",
                        "error": str(exc),
                    }
                )
                continue
            active = requirement.marker is None or requirement.marker.evaluate(marker_environment)
            entries.append(
                {
                    "source": _display_path(path),
                    "line": line_number,
                    "raw": stripped,
                    "kind": "requirement",
                    "name": requirement.name,
                    "canonical_name": canonicalize_name(requirement.name),
                    "specifier": str(requirement.specifier),
                    "marker": str(requirement.marker or ""),
                    "active": bool(active),
                }
            )
    return entries


def parse_pyproject_dependencies(
    pyproject_path: Path,
    marker_environment: dict[str, str],
    *,
    active_extras: tuple[str, ...] = ("voice", "vad", "local-cu124"),
) -> list[dict[str, Any]]:
    """Expand a pyproject.toml into the same declared-requirement entries as
    parse_requirement_files, so the comparison pipeline has one shape.

    Base `[project].dependencies` are always active; optional tiers are
    active only when named in `active_extras` (this audit targets the cu124
    full-stack profile, mirroring the former `.[voice,vad,local-cu124]`).
    The `extra == '<group>'` marker is preserved for reporting but is not
    evaluated against the environment: an installed env cannot reconstruct
    which extras were selected at install time.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups: list[tuple[str | None, str]] = [
        (None, line) for line in project.get("dependencies", [])
    ]
    for group, entries in project.get("optional-dependencies", {}).items():
        groups.extend((group, line) for line in entries)

    entries: list[dict[str, Any]] = []
    source = _display_path(pyproject_path)
    for line_number, (group, original) in enumerate(groups, 1):
        requirement_text = re.split(r"\s+#", original, maxsplit=1)[0].strip()
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as exc:
            entries.append(
                {
                    "source": source,
                    "line": line_number,
                    "raw": original,
                    "kind": "invalid",
                    "error": str(exc),
                }
            )
            continue
        if group is not None:
            base = requirement.marker
            combined = (
                f"({base}) and extra == '{group}'" if base else f"extra == '{group}'"
            )
            requirement.marker = Marker(combined)
        active = group is None or group in active_extras
        entries.append(
            {
                "source": source,
                "line": line_number,
                "raw": str(requirement),
                "kind": "requirement",
                "name": requirement.name,
                "canonical_name": canonicalize_name(requirement.name),
                "specifier": str(requirement.specifier),
                "marker": str(requirement.marker or ""),
                "active": bool(active),
            }
        )
    return entries


def _internal_module_names(root: Path) -> set[str]:
    names = {
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }
    names.update(path.stem for path in root.glob("*.py"))
    return names


def scan_imports(root: Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]]]:
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    parse_errors: list[dict[str, str]] = []
    candidates: list[Path] = []
    for relative in SCAN_ROOTS:
        base = root / relative
        if base.is_dir():
            candidates.extend(base.rglob("*.py"))
    for relative in SCAN_FILES:
        candidate = root / relative
        if candidate.is_file():
            candidates.append(candidate)

    for path in sorted(set(candidates)):
        if any(part in {"__pycache__", ".venv"} for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            parse_errors.append({"path": relative, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".", 1)[0]
                    evidence[module].append({"path": relative, "line": node.lineno})
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                module = node.module.split(".", 1)[0]
                evidence[module].append({"path": relative, "line": node.lineno})
    deduplicated: dict[str, list[dict[str, Any]]] = {}
    for module, rows in evidence.items():
        unique = {(row["path"], row["line"]): row for row in rows}
        deduplicated[module] = [unique[key] for key in sorted(unique)]
    return deduplicated, parse_errors


def compare_dependencies(
    environment: dict[str, Any],
    requirements: list[dict[str, Any]],
    imports: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    installed = {
        canonicalize_name(item["name"]): item
        for item in environment.get("packages", [])
        if isinstance(item, dict) and item.get("name")
    }
    active_requirements = [
        entry
        for entry in requirements
        if entry.get("kind") == "requirement" and entry.get("active")
    ]
    declared_names = {entry["canonical_name"] for entry in active_requirements}
    declared_missing: list[dict[str, Any]] = []
    declared_mismatched: list[dict[str, Any]] = []
    for entry in active_requirements:
        package = installed.get(entry["canonical_name"])
        if package is None:
            declared_missing.append(entry)
            continue
        specifier = str(entry.get("specifier") or "")
        if not specifier:
            continue
        try:
            satisfies = Version(str(package["version"])) in Requirement(entry["raw"].split(" #", 1)[0]).specifier
        except (InvalidRequirement, InvalidVersion):
            satisfies = False
        if not satisfies:
            declared_mismatched.append({**entry, "installed_version": package["version"]})

    internal = _internal_module_names(ROOT)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    package_map = environment.get("package_map", {})
    imported_undeclared: list[dict[str, Any]] = []
    unresolved_imports: list[dict[str, Any]] = []
    for module in sorted(imports):
        if module in internal or module in stdlib or module == "__future__":
            continue
        distributions = sorted(set(package_map.get(module, [])))
        canonical_distributions = {canonicalize_name(name) for name in distributions}
        declared = bool(canonical_distributions & declared_names)
        externally_managed = bool(canonical_distributions & EXTERNALLY_MANAGED_REQUIREMENTS)
        row = {
            "module": module,
            "distributions": distributions,
            "evidence": imports[module],
        }
        if not distributions:
            unresolved_imports.append(row)
        elif not declared and not externally_managed:
            imported_undeclared.append(row)

    direct_dependency_licenses: list[dict[str, str]] = []
    for name in sorted(declared_names | EXTERNALLY_MANAGED_REQUIREMENTS):
        package = installed.get(name)
        direct_dependency_licenses.append(
            {
                "name": package["name"] if package else name,
                "version": str(package["version"]) if package else "not-installed",
                "license": str(package.get("license") or "NOASSERTION") if package else "NOASSERTION",
                "declaration": "external-cu124" if name in EXTERNALLY_MANAGED_REQUIREMENTS else "pyproject",
            }
        )
    license_review_flags: list[dict[str, str]] = []
    for license_row in direct_dependency_licenses:
        license_value = license_row["license"].upper()
        if license_row["version"] == "not-installed" or license_value in {
            "NOASSERTION",
            "NON-SPDX-METADATA",
        }:
            license_review_flags.append(
                {**license_row, "reason": "license-metadata-unresolved"}
            )
        elif re.search(r"(?:^|[^A-Z])(?:A?GPL|LGPL)(?:[^A-Z]|$)", license_value):
            license_review_flags.append(
                {**license_row, "reason": "copyleft-or-dual-license-review"}
            )
    return {
        "declared_missing": declared_missing,
        "declared_mismatched": declared_mismatched,
        "imported_undeclared_candidates": imported_undeclared,
        "unresolved_import_candidates": unresolved_imports,
        "direct_dependency_licenses": direct_dependency_licenses,
        "license_review_flags": license_review_flags,
    }


def parse_pip_audit(
    path: Path | None,
    *,
    tool_version: str = "",
    service: str = "",
) -> dict[str, Any]:
    if path is None:
        return {
            "provided": False,
            "tool_version": tool_version,
            "service": service,
            "vulnerable_packages": [],
            "vulnerability_count": 0,
        }
    raw = json.loads(path.read_text(encoding="utf-8"))
    dependencies = raw.get("dependencies", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    vulnerable: list[dict[str, Any]] = []
    vulnerability_count = 0
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            continue
        vulns = dependency.get("vulns") or dependency.get("vulnerabilities") or []
        if not isinstance(vulns, list) or not vulns:
            continue
        normalized_vulns: list[dict[str, Any]] = []
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            normalized_vulns.append(
                {
                    "id": str(vuln.get("id") or vuln.get("name") or "unknown"),
                    "fix_versions": [str(item) for item in vuln.get("fix_versions", [])],
                    "aliases": [str(item) for item in vuln.get("aliases", [])],
                }
            )
        vulnerability_count += len(normalized_vulns)
        vulnerable.append(
            {
                "name": str(dependency.get("name") or "unknown"),
                "version": str(dependency.get("version") or "unknown"),
                "vulnerabilities": normalized_vulns,
            }
        )
    return {
        "provided": True,
        "source": path.name,
        "tool_version": tool_version,
        "service": service,
        "vulnerable_packages": vulnerable,
        "vulnerable_package_count": len(vulnerable),
        "vulnerability_count": vulnerability_count,
    }


def _git_facts() -> dict[str, Any]:
    commit = _run(["git", "rev-parse", "HEAD"], check=True).stdout.strip()
    dirty = bool(_run(["git", "status", "--porcelain"], check=True).stdout.strip())
    return {"commit": commit, "workspace_dirty": dirty}


def build_audit(
    *,
    python: Path,
    declaration_files: list[Path],
    pip_audit_path: Path | None,
    pip_audit_version: str = "",
    pip_audit_service: str = "",
) -> dict[str, Any]:
    environment = probe_environment(python)
    python_version = str(environment.get("python", {}).get("version") or "")
    marker_environment = default_environment()
    if python_version:
        marker_environment["python_full_version"] = python_version
        marker_environment["python_version"] = ".".join(python_version.split(".")[:2])
    requirements: list[dict[str, Any]] = []
    for path in declaration_files:
        if path.name == "pyproject.toml":
            requirements.extend(parse_pyproject_dependencies(path, marker_environment))
        else:
            requirements.extend(parse_requirement_files([path], marker_environment))
    imports, parse_errors = scan_imports(ROOT)
    comparison = compare_dependencies(environment, requirements, imports)
    pip_check = _run([str(python), "-m", "pip", "check"])
    vulnerability_audit = parse_pip_audit(
        pip_audit_path,
        tool_version=pip_audit_version,
        service=pip_audit_service,
    )
    attention = bool(
        pip_check.returncode
        or comparison["declared_missing"]
        or comparison["declared_mismatched"]
        or comparison["imported_undeclared_candidates"]
        or vulnerability_audit.get("vulnerability_count")
    )
    return {
        "schema": "amadeus.cu124-dependency-audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_facts(),
        "scope": {
            "profile": "windows-py312-cu124-observed",
            "requirement_files": [_display_path(path) for path in declaration_files],
            "scan_roots": list(SCAN_ROOTS),
            "scan_files": list(SCAN_FILES),
            "limitations": [
                "Import scanning cannot distinguish every optional or type-checking-only import.",
                "Installed-but-undeclared transitive packages are not automatically direct dependencies.",
                "An observed package snapshot is not a resolver-generated lock and contains no hashes.",
            ],
        },
        "status": "attention-required" if attention else "passed",
        "environment": environment,
        "pip_check": {
            "returncode": pip_check.returncode,
            "output": (pip_check.stdout + pip_check.stderr).strip(),
        },
        "requirements": requirements,
        "import_parse_errors": parse_errors,
        "comparison": comparison,
        "pip_audit": vulnerability_audit,
    }


def _md_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    environment = report["environment"]
    python = environment["python"]
    torch = environment["torch"]
    comparison = report["comparison"]
    pip_audit = report["pip_audit"]
    lines = [
        "# Windows Python 3.12 / cu124 dependency audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Commit: `{report['git']['commit']}`",
        f"- Workspace dirty during observation: `{str(report['git']['workspace_dirty']).lower()}`",
        f"- Status: **{report['status']}**",
        "",
        "## Observed environment",
        "",
        "| Fact | Value |",
        "| --- | --- |",
        f"| Python | `{_md_escape(python.get('version'))}` |",
        f"| Platform | `{_md_escape(python.get('platform'))}` |",
        f"| Torch | `{_md_escape(torch.get('version', 'not installed'))}` |",
        f"| Torch CUDA build | `{_md_escape(torch.get('cuda_build', ''))}` |",
        f"| CUDA available | `{_md_escape(torch.get('cuda_available', False))}` |",
        f"| GPU count | `{_md_escape(torch.get('gpu_count', 0))}` |",
        f"| cuDNN numeric version | `{_md_escape(torch.get('cudnn_version', 0))}` |",
        f"| Installed distributions | `{len(environment.get('packages', []))}` |",
        f"| pip check | `{'passed' if report['pip_check']['returncode'] == 0 else 'failed'}` |",
        "",
        "`pip check` only validates installed distribution metadata. It does not prove that the",
        "`pyproject.toml` declaration is complete or that the lockfile matches the environment.",
        "",
        "## Declaration drift",
        "",
    ]

    mismatched = comparison["declared_mismatched"]
    if mismatched:
        lines.extend(["| Requirement | Declared | Observed | Source |", "| --- | --- | --- | --- |"])
        for row in mismatched:
            lines.append(
                f"| `{_md_escape(row['name'])}` | `{_md_escape(row['specifier'])}` | "
                f"`{_md_escape(row['installed_version'])}` | `{row['source']}:{row['line']}` |"
            )
    else:
        lines.append("No active version mismatch was observed.")

    lines.extend(["", "### Declared but not installed", ""])
    missing = comparison["declared_missing"]
    if missing:
        for row in missing:
            lines.append(f"- `{row['name']}` — `{row['source']}:{row['line']}`")
    else:
        lines.append("None.")

    lines.extend(["", "### Imported distribution candidates absent from direct declarations", ""])
    undeclared = comparison["imported_undeclared_candidates"]
    if undeclared:
        lines.extend(["| Import | Installed distribution(s) | First evidence |", "| --- | --- | --- |"])
        for row in undeclared:
            first = row["evidence"][0]
            lines.append(
                f"| `{row['module']}` | `{', '.join(row['distributions'])}` | "
                f"`{first['path']}:{first['line']}` |"
            )
    else:
        lines.append("None.")
    lines.extend(
        [
            "",
            "These are candidates for profile declarations, not an instruction to put every optional backend into one base dependency group.",
            "",
            "## Vulnerability snapshot",
            "",
        ]
    )
    if not pip_audit.get("provided"):
        lines.append("No `pip-audit` JSON was supplied. Run the isolated audit command documented below.")
    else:
        lines.append(
            f"The supplied snapshot reports **{pip_audit['vulnerability_count']} vulnerabilities across "
            f"{pip_audit['vulnerable_package_count']} installed packages**. Reachability and profile ownership must be reviewed before upgrades."
        )
        if pip_audit.get("tool_version") or pip_audit.get("service"):
            lines.append(
                f"Tool: `pip-audit {pip_audit.get('tool_version') or 'unknown'}`; "
                f"service: `{pip_audit.get('service') or 'default'}`."
            )
        lines.extend(["", "| Package | Version | Findings | Fixed versions observed |", "| --- | --- | ---: | --- |"])
        for package in pip_audit["vulnerable_packages"]:
            fixes = sorted(
                {
                    fix
                    for vulnerability in package["vulnerabilities"]
                    for fix in vulnerability.get("fix_versions", [])
                }
            )
            lines.append(
                f"| `{_md_escape(package['name'])}` | `{_md_escape(package['version'])}` | "
                f"{len(package['vulnerabilities'])} | `{_md_escape(', '.join(fixes) or 'none-listed')}` |"
            )

    lines.extend(
        [
            "",
            "## Direct dependency license metadata",
            "",
            "This table reflects installed package metadata, which is evidence rather than a final legal conclusion.",
            "",
            "| Distribution | Observed version | Metadata license | Declaration |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in comparison["direct_dependency_licenses"]:
        lines.append(
            f"| `{_md_escape(row['name'])}` | `{_md_escape(row['version'])}` | "
            f"`{_md_escape(row['license'])}` | `{_md_escape(row['declaration'])}` |"
        )

    lines.extend(["", "### License metadata requiring review", ""])
    flags = comparison["license_review_flags"]
    if flags:
        lines.extend(["| Distribution | Version | Metadata | Reason |", "| --- | --- | --- | --- |"])
        for row in flags:
            lines.append(
                f"| `{_md_escape(row['name'])}` | `{_md_escape(row['version'])}` | "
                f"`{_md_escape(row['license'])}` | `{_md_escape(row['reason'])}` |"
            )
    else:
        lines.append("None.")

    lines.extend(
        [
            "",
            "## Release judgment",
            "",
            "1. The observed cu124 environment is internally consistent when `pip check` passes, but it is not reproducible from the current declarations.",
            "2. `numpy==1.23.4`, `librosa==0.9.2`, and `numba==0.56.4` must not remain the Python 3.12 release baseline when the tested environment uses newer versions.",
            "3. Local TTS, ASR, external/API, GUI, and development dependencies need separate profiles before a resolver-generated, hashed lock can be authoritative.",
            "4. Torch and torchaudio must be locked as an explicit cu124 pair from the PyTorch CUDA index; an unconstrained generic install is not acceptable.",
            "5. Vulnerability fixes must be validated per reachable profile; bulk-upgrading the working GPU environment is outside this audit.",
            "6. The observed PyQt5/PyQtWebEngine wheels advertise GPL v3. An Amadeus release must not treat them as ordinary permissive dependencies: remove them from the supported Electron profile, obtain appropriate commercial terms, or complete a deliberate GPL compatibility review against the selected first-party license.",
            "",
            "## Reproduction",
            "",
            "```powershell",
            "# Run pip-audit from an isolated throwaway env (uvx); do not mutate .venv.",
            "uvx pip-audit@2.10.1 `",
            "  --path .venv\\Lib\\site-packages --format json --desc off --aliases on `",
            "  --progress-spinner off --output build\\audit\\pip-audit-cu124.json",
            "",
            ".venv\\Scripts\\python.exe tools\\audit_cu124_dependencies.py `",
            "  --pip-audit-json build\\audit\\pip-audit-cu124.json `",
            "  --pip-audit-version 2.10.1 --pip-audit-service pypi `",
            "  --output-json build\\audit\\cu124-dependencies.json `",
            "  --output-markdown build\\audit\\cu124-dependencies.md `",
            "  --observed-output build\\audit\\windows-py312-cu124-observed.txt",
            "```",
            "",
            "The observed requirements file is an inventory only. Do not install from it as if it were a hashed lock.",
            "",
        ]
    )
    return "\n".join(lines)


def observed_requirements(report: dict[str, Any]) -> str:
    packages = report["environment"].get("packages", [])
    lines = [
        "# Observed package snapshot for Windows / Python 3.12 / torch cu124.",
        "# Generated from installed distribution metadata; not a resolver lock.",
        "# Contains no hashes, indexes, direct URLs, or local paths. DO NOT install as authoritative.",
    ]
    lines.extend(
        f"{item['name']}=={item['version']}"
        for item in sorted(packages, key=lambda row: canonicalize_name(row["name"]))
    )
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists (pass --overwrite): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=str(DEFAULT_PYTHON))
    parser.add_argument(
        "--requirements",
        action="append",
        default=[],
        help="declaration input(s): a pyproject.toml (expanded) or classic "
        "requirements file; defaults to the repo pyproject.toml",
    )
    parser.add_argument("--pip-audit-json", default="")
    parser.add_argument("--pip-audit-version", default="")
    parser.add_argument("--pip-audit-service", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--output-markdown", default="")
    parser.add_argument("--observed-output", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    python = Path(args.python).resolve()
    if not python.is_file():
        raise SystemExit(f"target Python does not exist: {python}")
    declaration_files = (
        [Path(value).resolve() for value in args.requirements]
        if args.requirements
        else [DEFAULT_DECLARATION_FILE]
    )
    for path in declaration_files:
        if not path.is_file():
            raise SystemExit(f"declaration file does not exist: {path}")
    pip_audit_path = Path(args.pip_audit_json).resolve() if args.pip_audit_json else None
    report = build_audit(
        python=python,
        declaration_files=declaration_files,
        pip_audit_path=pip_audit_path,
        pip_audit_version=str(args.pip_audit_version),
        pip_audit_service=str(args.pip_audit_service),
    )
    if args.output_json:
        _write(
            Path(args.output_json).resolve(),
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            overwrite=bool(args.overwrite),
        )
    if args.output_markdown:
        _write(
            Path(args.output_markdown).resolve(),
            render_markdown(report),
            overwrite=bool(args.overwrite),
        )
    if args.observed_output:
        _write(
            Path(args.observed_output).resolve(),
            observed_requirements(report),
            overwrite=bool(args.overwrite),
        )
    comparison = report["comparison"]
    print(
        "cu124 dependency audit: "
        f"{report['status']}; {len(report['environment'].get('packages', []))} installed, "
        f"{len(comparison['declared_missing'])} declared-missing, "
        f"{len(comparison['declared_mismatched'])} version mismatches, "
        f"{len(comparison['imported_undeclared_candidates'])} imported undeclared candidates, "
        f"{report['pip_audit'].get('vulnerability_count', 0)} vulnerability findings"
    )
    if args.strict and report["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
