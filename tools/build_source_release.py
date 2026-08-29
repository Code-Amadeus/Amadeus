"""Check and assemble a deterministic Amadeus public source archive.

Only files tracked by Git are candidates.  The command applies an explicit
allowlist, excludes model/creative/proprietary material, scans filenames and
text without printing secret values, validates third-party provenance, and
writes a SHA-256 manifest.  With no ``--output`` it is a non-mutating check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.check_third_party_provenance import (  # noqa: E402
    load_manifest as load_provenance_manifest,
    matches_any,
    release_blockers_for_paths,
    validate_manifest as validate_provenance_manifest,
)


DEFAULT_POLICY = ROOT / "release" / "source_release_policy.json"
TEXT_DECODING = "utf-8"


def _run_git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )
    return result.stdout


def load_policy(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding=TEXT_DECODING))
    if not isinstance(raw, dict):
        raise ValueError("source release policy must contain a JSON object")
    if raw.get("schema") != "amadeus.source-release-policy.v1":
        raise ValueError("unsupported source release policy schema")
    return raw


def tracked_file_modes(root: Path) -> dict[str, str]:
    raw = _run_git(root, "ls-files", "-s", "-z", binary=True)
    assert isinstance(raw, bytes)
    modes: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_bytes = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        path = path_bytes.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        modes[path] = mode
    return modes


def dirty_paths(root: Path) -> set[str]:
    raw = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", binary=True)
    assert isinstance(raw, bytes)
    records = [record for record in raw.split(b"\0") if record]
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        status = record[:2].decode("ascii", errors="replace")
        path = record[3:].decode("utf-8", errors="surrogateescape").replace("\\", "/")
        paths.add(path)
        if "R" in status or "C" in status:
            index += 1
            if index < len(records):
                paths.add(
                    records[index].decode("utf-8", errors="surrogateescape").replace("\\", "/")
                )
        index += 1
    return paths


def select_paths(paths: Iterable[str], policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    include_roots = {str(item).strip("/") for item in policy.get("include_roots", [])}
    include_files = {str(item).replace("\\", "/") for item in policy.get("include_files", [])}
    exclude_globs = [str(item) for item in policy.get("exclude_globs", [])]
    selected: list[str] = []
    excluded: list[str] = []
    for path in sorted(paths):
        normalized = path.replace("\\", "/")
        top_level = normalized.split("/", 1)[0]
        included = normalized in include_files or top_level in include_roots
        if not included or matches_any(normalized, exclude_globs):
            excluded.append(normalized)
            continue
        selected.append(normalized)
    return selected, excluded


def _issue(
    severity: str,
    rule: str,
    message: str,
    *,
    path: str = "",
    line: int | None = None,
    component: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"severity": severity, "rule": rule, "message": message}
    if path:
        payload["path"] = path
    if line is not None:
        payload["line"] = line
    if component:
        payload["component"] = component
    return payload


def scan_selected_files(
    root: Path,
    selected: Iterable[str],
    modes: dict[str, str],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    deny_globs = [str(item) for item in policy.get("deny_globs", [])]
    deny_allow_globs = [str(item) for item in policy.get("deny_allow_globs", [])]
    scan_extensions = {str(item).lower() for item in policy.get("content_scan_extensions", [])}
    max_file_bytes = int(policy.get("max_file_bytes", 0) or 0)
    large_allow = [str(item) for item in policy.get("large_file_allow_globs", [])]
    compiled_rules: list[tuple[str, str, re.Pattern[str]]] = []
    for rule in policy.get("content_rules", []):
        if not isinstance(rule, dict):
            continue
        compiled_rules.append(
            (
                str(rule.get("id") or "unnamed-content-rule"),
                str(rule.get("severity") or "error"),
                re.compile(str(rule.get("pattern") or "")),
            )
        )

    root_resolved = root.resolve()
    for relative in selected:
        if matches_any(relative, deny_globs) and not matches_any(relative, deny_allow_globs):
            issues.append(
                _issue("error", "denied-path", "selected path matches a deny rule", path=relative)
            )
        mode = modes.get(relative, "")
        if mode == "120000":
            issues.append(
                _issue(
                    "error",
                    "symlink-not-supported",
                    "source archives do not follow tracked symlinks",
                    path=relative,
                )
            )
            continue
        absolute = (root / relative).resolve()
        try:
            absolute.relative_to(root_resolved)
        except ValueError:
            issues.append(
                _issue("error", "path-escape", "tracked path resolves outside repository", path=relative)
            )
            continue
        if not absolute.is_file():
            issues.append(
                _issue("error", "missing-tracked-file", "tracked source file is missing", path=relative)
            )
            continue
        size = absolute.stat().st_size
        if max_file_bytes and size > max_file_bytes and not matches_any(relative, large_allow):
            issues.append(
                _issue(
                    "error",
                    "oversized-source-file",
                    f"file is {size} bytes; limit is {max_file_bytes}",
                    path=relative,
                )
            )

        suffix = absolute.suffix.lower()
        should_scan = suffix in scan_extensions or absolute.name.startswith(".env")
        if not should_scan:
            continue
        data = absolute.read_bytes()
        if b"\0" in data[:8192]:
            continue
        text = data.decode(TEXT_DECODING, errors="replace")
        for rule_id, severity, pattern in compiled_rules:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                issues.append(
                    _issue(
                        severity,
                        rule_id,
                        "content matched this rule; matched text is intentionally suppressed",
                        path=relative,
                        line=line,
                    )
                )
    return issues


def file_records(root: Path, selected: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in sorted(selected):
        absolute = root / relative
        data = absolute.read_bytes()
        records.append(
            {
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return records


def source_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding=TEXT_DECODING))
    return str(data.get("project", {}).get("version") or "0.0.0")


def build_report(
    *,
    root: Path,
    policy_path: Path,
    policy: dict[str, Any],
    allow_dirty_check: bool,
) -> dict[str, Any]:
    modes = tracked_file_modes(root)
    tracked = sorted(modes)
    selected, excluded = select_paths(tracked, policy)
    issues: list[dict[str, Any]] = []
    missing_selected = [path for path in selected if not (root / path).is_file()]
    missing_selected_set = set(missing_selected)
    selected_present = [path for path in selected if path not in missing_selected_set]
    for path in missing_selected:
        issues.append(
            _issue(
                "error",
                "selected-file-missing",
                "tracked release input is absent from the working tree",
                path=path,
            )
        )

    distribution_model = str(policy.get("distribution_model") or "")
    if distribution_model == "pending-owner-selection" or not distribution_model:
        issues.append(
            _issue(
                "error",
                "distribution-model-unselected",
                "rights holder must select the first-party publication model",
            )
        )
    first_party_license = policy.get("first_party_license")
    if not isinstance(first_party_license, dict):
        issues.append(
            _issue("error", "first-party-license", "first_party_license policy is missing")
        )
    else:
        license_status = str(first_party_license.get("status") or "")
        if license_status != "selected":
            issues.append(
                _issue(
                    "error",
                    "first-party-license-unselected",
                    "rights holder must select and scope the first-party license",
                    path=str(first_party_license.get("license_file") or "LICENSE"),
                )
            )

    for required in policy.get("required_files", []):
        required_path = str(required).replace("\\", "/")
        if required_path not in selected:
            issues.append(
                _issue(
                    "error",
                    "required-file-not-tracked",
                    "required release file is not selected from Git",
                    path=required_path,
                )
            )

    selected_dirty = sorted(set(selected) & dirty_paths(root))
    if selected_dirty and not allow_dirty_check:
        for path in selected_dirty:
            issues.append(
                _issue(
                    "error",
                    "dirty-selected-file",
                    "release input differs from the committed tree",
                    path=path,
                )
            )

    issues.extend(scan_selected_files(root, selected_present, modes, policy))

    provenance_path = root / str(policy.get("provenance_manifest") or "")
    if not provenance_path.is_file():
        issues.append(
            _issue(
                "error",
                "missing-provenance-manifest",
                "configured provenance manifest does not exist",
                path=provenance_path.relative_to(root).as_posix(),
            )
        )
    else:
        provenance = load_provenance_manifest(provenance_path)
        for item in validate_provenance_manifest(
            provenance,
            root=root,
            files=tracked,
            require_release_ready=False,
        ):
            if item["severity"] == "error":
                issues.append(
                    _issue(
                        "error",
                        f"provenance-{item['rule']}",
                        item["message"],
                        component=item.get("component", ""),
                    )
                )
        issues.extend(release_blockers_for_paths(provenance, selected_present))

    commit = str(_run_git(root, "rev-parse", "HEAD")).strip()
    source_date_epoch = int(str(_run_git(root, "show", "-s", "--format=%ct", "HEAD")).strip())
    policy_hash = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    issues.sort(
        key=lambda item: (
            item.get("severity", ""),
            item.get("rule", ""),
            item.get("path", ""),
            item.get("line", 0),
            item.get("component", ""),
        )
    )
    errors = [item for item in issues if item["severity"] == "error"]
    return {
        "schema": "amadeus.source-release-manifest.v1",
        "distribution_model": distribution_model,
        "version": source_version(root),
        "commit": commit,
        "source_date_epoch": source_date_epoch,
        "policy_sha256": policy_hash,
        "release_ready": not errors and not selected_dirty,
        "dirty_check_overridden": bool(allow_dirty_check and selected_dirty),
        "selected_file_count": len(selected_present),
        "excluded_file_count": len(excluded) + len(missing_selected),
        "selected_files": file_records(root, selected_present),
        "excluded_files": sorted([*excluded, *missing_selected]),
        "issues": issues,
    }


def _zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    value = datetime.fromtimestamp(source_date_epoch, tz=timezone.utc)
    if value.year < 1980:
        value = value.replace(year=1980, month=1, day=1, hour=0, minute=0, second=0)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)


def create_archive(
    *,
    root: Path,
    output: Path,
    report: dict[str, Any],
    policy: dict[str, Any],
    modes: dict[str, str],
) -> None:
    if not report.get("release_ready"):
        raise ValueError("refusing to build an archive that did not pass the release gate")
    prefix = str(policy.get("archive_prefix") or "amadeus-{version}").format(
        version=report["version"]
    )
    timestamp = _zip_timestamp(int(report["source_date_epoch"]))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"temporary archive already exists: {temporary}")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for record in report["selected_files"]:
                relative = str(record["path"])
                info = zipfile.ZipInfo(f"{prefix}/{relative}", date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                permissions = 0o755 if modes.get(relative) == "100755" else 0o644
                info.external_attr = (permissions & 0xFFFF) << 16
                archive.writestr(info, (root / relative).read_bytes(), compresslevel=9)
            manifest_bytes = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode(
                TEXT_DECODING
            )
            info = zipfile.ZipInfo(f"{prefix}/SOURCE_MANIFEST.json", date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, manifest_bytes, compresslevel=9)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists (pass --overwrite): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--manifest-output", default="")
    parser.add_argument("--output", default="", help="optional deterministic .zip output")
    parser.add_argument(
        "--allow-dirty-check",
        action="store_true",
        help="inspect a dirty tree without making it release-ready; never permits archive creation",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-issues", type=int, default=40)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.output and args.allow_dirty_check:
        raise SystemExit("--allow-dirty-check cannot be used with --output")
    policy_path = Path(args.policy).resolve()
    policy = load_policy(policy_path)
    report = build_report(
        root=ROOT,
        policy_path=policy_path,
        policy=policy,
        allow_dirty_check=bool(args.allow_dirty_check),
    )
    if args.manifest_output:
        _write_json(Path(args.manifest_output).resolve(), report, overwrite=bool(args.overwrite))

    errors = [item for item in report["issues"] if item["severity"] == "error"]
    warnings = [item for item in report["issues"] if item["severity"] == "warning"]
    print(
        "source release check: "
        f"{'passed' if report['release_ready'] else 'blocked'}; "
        f"{report['selected_file_count']} selected, {report['excluded_file_count']} excluded, "
        f"{len(errors)} errors, {len(warnings)} warnings"
    )
    for item in report["issues"][: max(args.max_issues, 0)]:
        location = item.get("path", "")
        if item.get("line") is not None:
            location += f":{item['line']}"
        component = f" [{item['component']}]" if item.get("component") else ""
        suffix = f" ({location})" if location else ""
        print(f"- {item['severity'].upper()} {item['rule']}{component}{suffix}: {item['message']}")
    remaining = len(report["issues"]) - max(args.max_issues, 0)
    if remaining > 0:
        print(f"- ... {remaining} more issues; use --manifest-output for the full report")

    if args.output:
        output = Path(args.output).resolve()
        if output.exists() and not args.overwrite:
            raise FileExistsError(f"output already exists (pass --overwrite): {output}")
        create_archive(
            root=ROOT,
            output=output,
            report=report,
            policy=policy,
            modes=tracked_file_modes(ROOT),
        )
        print(f"archive: {output}")
    return 0 if report["release_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
