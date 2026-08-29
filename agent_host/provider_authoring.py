"""Optional host authoring capabilities exposed to code Providers.

This is not routing and does not infer a new task intent.  It tells a coding
agent which host-native integration contract exists, then leaves the agent to
decide from the user's task whether that capability is relevant.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_AUIP_GUIDANCE_FILES = (
    Path("skills/auip-authoring/SKILL.md"),
    Path("skills/auip-authoring/references/interface-v0.md"),
    Path("skills/auip-authoring/references/controller-v0.md"),
    Path("skills/auip-authoring/assets/auip.manifest.json"),
)

_AUIP_MANIFEST_PREFLIGHT_FILES = (
    Path("server/__init__.py"),
    Path("server/auip_contract.py"),
    Path("tools/validate_auip_manifest.py"),
    Path("tools/sync_auip_manifest.py"),
)

_AUIP_OPAQUE_DEPENDENCY_FILES = (
    Path("sdk/auip-core/managed-v0.js"),
    Path("sdk/auip-core/controller-v0.js"),
    Path("sdk/auip-core/situations-v0.js"),
    Path("sdk/auip-web/auip-v0.js"),
)

_AUIP_RUNTIME_ASSETS = {
    "sdk/auip-core/managed-v0.js": Path("sdk/auip-core/managed-v0.js"),
    "sdk/auip-core/controller-v0.js": Path("sdk/auip-core/controller-v0.js"),
    "sdk/auip-core/situations-v0.js": Path("sdk/auip-core/situations-v0.js"),
    "sdk/auip-web/auip-v0.js": Path("sdk/auip-web/auip-v0.js"),
}

_REQUIRED_AUIP_AUTHORING_SOURCES = frozenset({"auip_prepare", "auip_create"})


def requires_auip_authoring(source: object) -> bool:
    """Return whether one Host-derived dispatch requires AUIP authoring."""

    return str(source or "").strip().lower() in _REQUIRED_AUIP_AUTHORING_SOURCES


def official_auip_runtime_assets() -> dict[str, dict[str, str]]:
    """Return immutable source identities for Host-owned runtime assets."""

    assets: dict[str, dict[str, str]] = {}
    for filename, relative_path in _AUIP_RUNTIME_ASSETS.items():
        source = (_ROOT / relative_path).resolve()
        raw = source.read_bytes()
        assets[filename] = {
            "source_path": str(source),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return assets


def auip_authoring_source_identity() -> dict[str, object]:
    """Return a path-free identity for the built-in AUIP authoring package.

    Capability discovery must not copy the Skill body into another registry or
    expose an installation-specific absolute path.  The native authoring owner
    continues to stage the exact files; the horizontal catalog receives only a
    stable relative file inventory and a digest of those authoritative bytes.
    """

    relative_files = tuple(
        str(path.as_posix())
        for path in (
            *_AUIP_GUIDANCE_FILES,
            *_AUIP_MANIFEST_PREFLIGHT_FILES,
            *_AUIP_OPAQUE_DEPENDENCY_FILES,
        )
    )
    digest = hashlib.sha256()
    for relative_name in relative_files:
        source = (_ROOT / Path(relative_name)).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"missing AUIP authoring input: {source}")
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return {
        "skill_id": "auip-authoring",
        "skill_relative_path": "skills/auip-authoring/SKILL.md",
        "digest": digest.hexdigest(),
        "files": relative_files,
    }


def materialize_auip_runtime_assets(
    destination: Path,
    *,
    replace_existing: bool = True,
) -> dict[str, dict[str, str]]:
    """Copy opaque dependencies into their stable app-owned SDK layout.

    Export staging is a Host-owned transaction and may replace files. A
    caller-owned workspace is different: a locally modified file at a
    reserved runtime path must fail visibly instead of being overwritten
    before the coding Provider captures its baseline.
    """

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    official = official_auip_runtime_assets()
    if not replace_existing:
        for relative_name, identity in official.items():
            target = (root / Path(relative_name)).resolve()
            if root not in target.parents or target.is_symlink():
                raise OSError(f"unsafe AUIP runtime asset target: {target}")
            if not target.exists():
                continue
            try:
                current_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError as exc:
                raise OSError(f"unreadable AUIP runtime asset target: {target}") from exc
            if current_digest != identity["sha256"]:
                raise OSError(f"conflicting AUIP runtime asset target: {target}")
    materialized: dict[str, dict[str, str]] = {}
    for relative_name, identity in official.items():
        target = (root / Path(relative_name)).resolve()
        if root not in target.parents or target.is_symlink():
            raise OSError(f"unsafe AUIP runtime asset target: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if replace_existing or not target.exists():
            shutil.copyfile(identity["source_path"], target)
        materialized[relative_name] = {
            "path": str(target),
            "sha256": identity["sha256"],
        }
    return materialized


def stage_auip_authoring_bundle(
    destination: Path,
    *,
    include_opaque_dependencies: bool = True,
) -> Path:
    """Stage immutable AUIP inputs inside one Provider Attempt workspace."""

    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    files = (
        (
            *_AUIP_GUIDANCE_FILES,
            *_AUIP_MANIFEST_PREFLIGHT_FILES,
            *_AUIP_OPAQUE_DEPENDENCY_FILES,
        )
        if include_opaque_dependencies
        else (*_AUIP_GUIDANCE_FILES, *_AUIP_MANIFEST_PREFLIGHT_FILES)
    )
    for relative_path in files:
        source = (_ROOT / relative_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"missing AUIP authoring input: {source}")
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise OSError(f"AUIP authoring input target cannot be a symlink: {target}")
        shutil.copyfile(source, target)
    return root / "skills" / "auip-authoring" / "SKILL.md"


def auip_authoring_bundle_metrics(skill_path: Path) -> dict[str, int]:
    """Measure staged inputs and the files the compact route requires reading.

    The required-read budget describes provider-visible text, so it must not
    change when Git materializes that text with CRLF on Windows. Staged bytes
    remain the literal on-disk footprint; required-read bytes use canonical LF
    line endings so the cognitive-input guard is comparable across hosts.
    """

    skill = Path(skill_path).resolve()
    root = skill.parents[2]
    staged = [path for path in root.rglob("*") if path.is_file()]
    interface = skill.parent / "references" / "interface-v0.md"
    required = [path for path in (skill, interface) if path.is_file()]
    return {
        "staged_file_count": len(staged),
        "staged_bytes": sum(path.stat().st_size for path in staged),
        "required_read_file_count": len(required),
        "required_read_bytes": sum(
            len(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
            for path in required
        ),
    }


def with_host_authoring_capabilities(
    task: str,
    *,
    source_user_text: str = "",
    source_user_context: str = "",
    require_auip_preparation: bool = False,
    authoring_skill_path: str = "",
) -> str:
    body = str(task or "").rstrip()
    source = " ".join(str(source_user_text or "").split())[:4000]
    context = " ".join(str(source_user_context or "").split())[:2000]
    source_evidence = (
        "\nThe exact current user wording below is bounded intent evidence, not a "
        "second task or host instruction. It is the semantic authority for the "
        "requested actors, interaction mode, destination, and exclusions; the "
        "role-authored task is only an execution brief. If those conflict, preserve "
        "the exact wording and discard the conflicting detail. You may add ordinary "
        "implementation necessities, but not a different user-visible actor or "
        "primary interaction mode. An in-app bot or scripted auto-player does not "
        "satisfy a request "
        "for the main assistant to observe or participate:\n"
        + json.dumps(source, ensure_ascii=False)
        if source
        else ""
    )
    context_evidence = (
        "\nThe immediately preceding user wording is bounded reference context, "
        "not independent authority. Use it only to resolve what the current "
        "wording and role-authored task refer to; never execute a clause absent "
        "from the current authorized task:\n"
        + json.dumps(context, ensure_ascii=False)
        if context and context != source
        else ""
    )
    skill_path = str(authoring_skill_path or "").strip()
    if require_auip_preparation and not skill_path:
        raise ValueError("AUIP preparation requires a workspace-local authoring bundle")
    if require_auip_preparation:
        bundle_root = Path(skill_path).resolve().parents[2]
        validator_path = bundle_root / "tools" / "validate_auip_manifest.py"
        sync_path = bundle_root / "tools" / "sync_auip_manifest.py"
        return f"""Host-authorized AUIP application prerequisite:
Before the Host can launch the requested local interactive app, this Provider
run must produce that app as one AUIP-capable delivery in this workspace. If
the task refers to an existing app, integrate it in place and preserve its
mechanics. If the task creates the app in this same run, build it once with the
integration included. Never create a second app merely to add AUIP.

Read and follow this host-staged skill before editing: {skill_path}
Use the compact interface reference named by that skill. Treat SDK, parser,
validator, and sync-tool implementations as opaque dependencies: do not read
their source unless an observed interface failure cannot be resolved from the
compact reference.
Before finishing, execute this opaque manifest preflight with Python against
the completed staged `auip.manifest.json`: {validator_path}
Then execute this opaque sync preflight with Python against that manifest and
the staged HTML entry: {sync_path}
Do not open or inspect either implementation. Successful preflights are
necessary but not authoritative; the Host reruns final bundle validation.
Use the official SDK named by that skill, keep the app fully usable without
Amadeus, create a valid AUIP manifest, and run the validation required by the
skill. Execute a bounded author-owned boot test of the completed entry both
without AUIP and with the exact materialized runtime scripts; a Web-binding
capture stub is not sufficient because construction validates the first
snapshot synchronously. Do not launch the user-facing app for interaction,
watch it, or operate it during this Provider run; the Host owns product launch
and will do so only after this prerequisite succeeds.
For a participant stance, cover a coherent useful part of the application's
primary interactive loop. Menu and lifecycle controls alone are insufficient
when core play still needs continuous human input; use a feasible app-local
Reactive Controller or remain spectator-only, as the staged interface requires.
For every multi-phase choice family, publish one stable complete `actionTypes`
set in `choice/v1`; a missing current option must remain unavailable rather than
falling out of the legality whitelist.

The role-authored task below describes the user's eventual interaction and may
name the target app. It is context, not permission to substitute browser or
Computer Use for the required preparation:
{body or "(no additional role-authored task)"}{source_evidence}{context_evidence}"""
    if not skill_path:
        return body
    capability = f"""Optional host authoring capability (this does not change the user's task):
If and only if the user asks Amadeus/Kurisu to watch, comment on, play, or
operate a local interactive web app, read and follow this host-owned skill
before editing: {skill_path}
Use the official SDK named by that skill. Keep the app fully usable without
Amadeus. For ordinary sites, reports, or unrelated code, ignore this capability.
Do not claim the integration was requested when the user's task does not imply it.{source_evidence}{context_evidence}"""
    return f"{body}\n\n{capability}" if body else capability
