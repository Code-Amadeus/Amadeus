"""Validate the checked-in third-party provenance inventory.

The default command validates the inventory's structure and coverage.  It does
not claim that a release is ready merely because known blockers are recorded.
Pass ``--release-ready`` to make unresolved included components fail the gate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "LICENSES" / "provenance.json"
VALID_GATE_STATES = frozenset({"ready", "review", "blocked"})
VALID_RELEASE_ACTIONS = frozenset({"include", "exclude", "user-supplied", "dependency"})


def _normalize_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("./")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a small, slash-aware glob dialect with ``**`` support."""

    pattern = _normalize_path(pattern)
    chunks: list[str] = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    chunks.append("(?:.*/)?")
                    index += 1
                else:
                    chunks.append(".*")
                continue
            chunks.append("[^/]*")
        elif char == "?":
            chunks.append("[^/]")
        else:
            chunks.append(re.escape(char))
        index += 1
    chunks.append("$")
    return re.compile("".join(chunks))


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    normalized = _normalize_path(path)
    return any(glob_regex(pattern).match(normalized) for pattern in patterns)


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(
        _normalize_path(item.decode("utf-8", errors="surrogateescape"))
        for item in result.stdout.split(b"\0")
        if item
    )


def load_manifest(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("provenance manifest must contain a JSON object")
    return raw


def validate_manifest(
    manifest: dict[str, Any],
    *,
    root: Path,
    files: Iterable[str],
    require_release_ready: bool = False,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []

    def add(severity: str, rule: str, message: str, component: str = "") -> None:
        issues.append(
            {
                "severity": severity,
                "rule": rule,
                "component": component,
                "message": message,
            }
        )

    if manifest.get("schema") != "amadeus.third-party-provenance.v1":
        add("error", "schema", "unsupported or missing provenance schema")

    components = manifest.get("components")
    if not isinstance(components, list):
        add("error", "components", "components must be a JSON array")
        return issues

    file_list = list(files)
    identifiers: set[str] = set()
    valid_components: list[dict[str, Any]] = []
    for index, raw_component in enumerate(components):
        if not isinstance(raw_component, dict):
            add("error", "component-shape", f"component #{index} must be an object")
            continue
        component = raw_component
        identifier = str(component.get("id") or "").strip()
        if not identifier:
            add("error", "component-id", f"component #{index} has no id")
            continue
        if identifier in identifiers:
            add("error", "duplicate-id", f"duplicate component id: {identifier}", identifier)
        identifiers.add(identifier)

        patterns = component.get("paths")
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            add("error", "component-paths", "paths must be a string array", identifier)
            continue
        matched = [path for path in file_list if matches_any(path, patterns)]
        action = str(component.get("release_action") or "")
        gate = str(component.get("gate_status") or "")
        license_expression = str(component.get("license_expression") or "").strip()
        evidence = component.get("license_evidence")

        if action not in VALID_RELEASE_ACTIONS:
            add("error", "release-action", f"invalid release_action: {action!r}", identifier)
        if gate not in VALID_GATE_STATES:
            add("error", "gate-status", f"invalid gate_status: {gate!r}", identifier)
        if not matched and action not in {"dependency", "user-supplied"}:
            add("warning", "unmatched-component", "no tracked file matches this component", identifier)
        if not license_expression:
            add("error", "license-expression", "license_expression is required", identifier)
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            add("error", "license-evidence", "license_evidence must be a string array", identifier)
            evidence = []
        for relative in evidence:
            if not (root / relative).is_file():
                add(
                    "error",
                    "missing-license-evidence",
                    f"license evidence does not exist: {relative}",
                    identifier,
                )
        if action == "include" and gate == "ready":
            if license_expression == "NOASSERTION":
                add(
                    "error",
                    "ready-without-license",
                    "included ready component cannot use NOASSERTION",
                    identifier,
                )
            if not evidence:
                add(
                    "error",
                    "ready-without-evidence",
                    "included ready component requires local license evidence",
                    identifier,
                )
        if require_release_ready and action == "include" and gate != "ready" and matched:
            add(
                "error",
                "release-blocker",
                f"included component is not ready (gate_status={gate})",
                identifier,
            )
        valid_components.append(component)

    roots = manifest.get("inventory_roots")
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        add("error", "inventory-roots", "inventory_roots must be a string array")
        roots = []
    inventory_files = [
        path
        for path in file_list
        if any(path == _normalize_path(base) or path.startswith(_normalize_path(base) + "/") for base in roots)
    ]
    for path in inventory_files:
        if not any(matches_any(path, component.get("paths", [])) for component in valid_components):
            add("error", "uncovered-third-party-file", f"no component covers {path}")

    return issues


def release_blockers_for_paths(
    manifest: dict[str, Any], selected_paths: Iterable[str]
) -> list[dict[str, str]]:
    """Return one aggregate issue for every non-releasable selected component."""

    selected = list(selected_paths)
    issues: list[dict[str, str]] = []
    for component in manifest.get("components", []):
        if not isinstance(component, dict):
            continue
        patterns = component.get("paths", [])
        if not isinstance(patterns, list):
            continue
        if not any(matches_any(path, patterns) for path in selected):
            continue
        action = str(component.get("release_action") or "")
        gate = str(component.get("gate_status") or "")
        identifier = str(component.get("id") or "unknown")
        if action in {"exclude", "user-supplied"}:
            issues.append(
                {
                    "severity": "error",
                    "rule": "excluded-provenance-component",
                    "component": identifier,
                    "message": f"selected files include a component marked {action}",
                }
            )
        elif action == "include" and gate != "ready":
            issues.append(
                {
                    "severity": "error",
                    "rule": "unresolved-provenance-component",
                    "component": identifier,
                    "message": f"selected component is not release-ready (gate_status={gate})",
                }
            )
    return issues


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--release-ready", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    issues = validate_manifest(
        manifest,
        root=ROOT,
        files=tracked_files(ROOT),
        require_release_ready=bool(args.release_ready),
    )
    errors = [issue for issue in issues if issue["severity"] == "error"]
    payload = {
        "schema": "amadeus.third-party-provenance-check.v1",
        "manifest": manifest_path.relative_to(ROOT).as_posix(),
        "release_ready_required": bool(args.release_ready),
        "status": "passed" if not errors else "failed",
        "issues": issues,
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"provenance check: {payload['status']} ({len(errors)} errors, {len(issues)} total issues)")
        for issue in issues:
            component = f" [{issue['component']}]" if issue.get("component") else ""
            print(f"- {issue['severity'].upper()} {issue['rule']}{component}: {issue['message']}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
