"""Host copy of the pinned extension manifest validator.

Use ``python tools/extension_package.py inspect-wheel PATH`` to inspect an
installable wheel. The adjacent schema and theme token allowlist supply the
canonical structural rules; protocol-source.json records their origin.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match


SCHEMA_PATH = Path(__file__).with_name("amadeus-extension.v0.schema.json")
THEME_TOKENS = json.loads(SCHEMA_PATH.with_name("theme-tokens.v0.json").read_text(encoding="utf-8"))
MANIFEST_NAME = "amadeus-extension.json"
MANIFEST_LIMIT_BYTES = 256 * 1024
SKILL_LIMIT_BYTES = 16 * 1024 * 1024

RUNNER_KINDS = ("voice_backend", "context_source", "observer")
TOPIC_CAPABILITIES = {
    "work.item.updated": "events.work:read",
    "conversation.turn.completed": "conversation.history:read",
}

_TEMPLATE = re.compile(r"\$\{([^}]*)\}")
_CONFIG_REFERENCE = re.compile(r"config\.([a-z][a-z0-9_]{0,63})")
_RELATIVE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
_PACKAGE_PREFIX = "${package}/"


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} {self.path}: {self.message}"


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _pointer(parts: Iterable[Any]) -> str:
    return "/" + "/".join(str(part) for part in parts)


def _message(error: Any) -> str:
    # The schema forbids a field in some positions with {"not": {}}.
    if error.validator == "not" and error.validator_value == {}:
        return "this field is not allowed here"
    message = error.message
    return message if len(message) <= 300 else message[:297] + "..."


def schema_issues(manifest: Any) -> list[Issue]:
    issues = []
    for error in _validator().iter_errors(manifest):
        # oneOf failures carry the useful explanation in their sub-errors.
        detail = best_match(error.context) if error.context else error
        issues.append(Issue("schema", _pointer(detail.absolute_path), _message(detail)))
    return sorted(issues, key=lambda issue: issue.path)


def _theme_font_literal(value: str) -> bool:
    if not 1 <= len(value) <= 128 or any(unicodedata.category(char)[0] not in 'LN' and char not in " '\"-" for char in value):
        return False
    family = value.strip()
    if len(family) >= 2 and family[0] in "'\"" and family[-1] == family[0]:
        family = family[1:-1]
    return bool(family) and '"' not in family and "'" not in family


def _theme_color_literal(value: str) -> bool:
    text = value.strip().lower()
    if re.fullmatch(r'#[a-f0-9]{3}(?:[a-f0-9]|[a-f0-9]{3}|[a-f0-9]{5})?', text):
        return True
    match = re.fullmatch(r'(rgb|rgba|hsl|hsla)\(([^()]*)\)', text)
    if not match:
        return False
    body = match[2].strip()
    if ',' in body:
        if '/' in body:
            return False
        parts = [part.strip() for part in body.split(',')]
        if match[1].startswith('rgb') and len({part.endswith('%') for part in parts[:3]}) != 1:
            return False
    else:
        groups = body.split('/')
        if len(groups) > 2:
            return False
        parts = groups[0].split()
        if len(parts) != 3:
            return False
        if len(groups) == 2:
            parts.append(groups[1].strip())
    if len(parts) not in (3, 4):
        return False

    def component(token: str, maximum: float, percent: bool = False) -> bool:
        number = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))(%?)', token)
        if not number or percent and number[2] != '%':
            return False
        numeric = float(number[1])
        return math.isfinite(numeric) and 0 <= numeric <= (100 if number[2] else maximum)

    if len(parts) == 4 and not component(parts[3], 1):
        return False
    if match[1].startswith('rgb'):
        return all(component(token, 255) for token in parts[:3])
    hue = re.fullmatch(r'([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?:deg)?', parts[0])
    return bool(hue and math.isfinite(float(hue[1])) and all(component(token, 1, True) for token in parts[1:3]))


def semantic_issues(manifest: dict[str, Any]) -> list[Issue]:
    """Rules S1-S9 of spec section 3.9. Requires a schema-valid manifest."""

    issues: list[Issue] = []
    contributes = manifest["contributes"]
    runtime = manifest["runtime"]
    config = manifest.get("config", {})
    permissions = manifest.get("permissions", {})

    for kind, items in contributes.items():
        seen: set[str] = set()
        for index, item in enumerate(items):
            if item["id"] in seen:
                issues.append(Issue(
                    "S1.duplicate_contribution_id",
                    f"/contributes/{kind}/{index}/id",
                    f"{kind} id {item['id']!r} is declared more than once",
                ))
            seen.add(item["id"])

    runner_kinds = [kind for kind in RUNNER_KINDS if kind in contributes]
    if runner_kinds and "entry" not in runtime:
        issues.append(Issue(
            "S2.runner_required",
            "/runtime",
            f"{', '.join(runner_kinds)} need runtime.kind 'python' with an entry",
        ))
    if "entry" in runtime and not runner_kinds:
        issues.append(Issue(
            "S9.unused_entry",
            "/runtime/entry",
            "an entry is allowed only when the package contributes a runner kind",
        ))

    if 'icon' in manifest and Path(manifest['icon']).suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
        issues.append(Issue('P5.package_icon', '/icon', 'Package icons must be PNG/JPEG'))
    for index, theme in enumerate(contributes.get('theme', [])):
        background = theme.get('background')
        if background and Path(background['image']).suffix.lower() not in {'.png', '.jpg', '.jpeg'}:
            issues.append(Issue('P4.theme_image', f'/contributes/theme/{index}/background/image', 'Theme v0 images must be PNG/JPEG'))
        for name, value in theme['tokens'].items():
            pointer = f'/contributes/theme/{index}/tokens/{name}'
            kind = THEME_TOKENS.get(name)
            if kind is None:
                code = 'T1.reserved_token' if name.startswith(('--danger', '--warning', '--success')) else 'T1.unknown_token'
                issues.append(Issue(code, pointer, 'Use only the published theme token allowlist'))
            elif (kind == 'font' and not _theme_font_literal(value)
                  or kind == 'color' and not _theme_color_literal(value)):
                issues.append(Issue('T2.css_input', pointer, 'Use a color literal or a single installed font family'))

    secret_keys = {key for key, field in config.items() if field.get("secret")}
    disclosed = set(permissions.get("os", {}).get("process", []))
    python_runtime = runtime["kind"] == "python"
    for index, server in enumerate(contributes.get("mcp_server", [])):
        base = f"/contributes/mcp_server/{index}"
        for field, value, secret_allowed in _template_fields(server):
            issues.extend(_template_issues(
                f"{base}/{field}", value, config, secret_keys, secret_allowed, python_runtime
            ))
        bearer = server.get("bearer_token")
        if bearer is not None:
            key = bearer[len("${config."):-1]
            if key in config and key not in secret_keys:
                issues.append(Issue(
                    "S3.bearer_not_secret",
                    f"{base}/bearer_token",
                    f"config.{key} must be a secret field",
                ))
        if server["transport"] == "stdio":
            issues.extend(_command_issues(f"{base}/command", server["command"], disclosed))

    requested = [item["capability"] for item in permissions.get("host", [])]
    for index, observer in enumerate(contributes.get("observer", [])):
        for topic in observer["topics"]:
            capability = TOPIC_CAPABILITIES[topic]
            if capability not in requested:
                issues.append(Issue(
                    "S5.capability_not_requested",
                    f"/contributes/observer/{index}/topics",
                    f"topic {topic!r} needs permissions.host capability {capability!r}",
                ))

    for key, field in config.items():
        issues.extend(_config_issues(f"/config/{key}", field))

    seen_capabilities: set[str] = set()
    for index, capability in enumerate(requested):
        if capability in seen_capabilities:
            issues.append(Issue(
                "S7.duplicate_capability",
                f"/permissions/host/{index}/capability",
                f"{capability!r} is requested more than once",
            ))
        seen_capabilities.add(capability)

    return issues


def _template_fields(server: dict[str, Any]) -> Iterator[tuple[str, str, bool]]:
    """Yield (field, value, secrets allowed) for every templated MCP field."""

    if server["transport"] == "stdio":
        yield "command", server["command"], False
        for index, value in enumerate(server.get("args", [])):
            yield f"args/{index}", value, False
        for name, value in server.get("env", {}).items():
            yield f"env/{name}", value, True
    else:
        yield "url", server["url"], False
        if "bearer_token" in server:
            yield "bearer_token", server["bearer_token"], True


def _template_issues(
    path: str,
    value: str,
    config: dict[str, Any],
    secret_keys: set[str],
    secret_allowed: bool,
    python_runtime: bool,
) -> list[Issue]:
    issues = []
    references = _TEMPLATE.findall(value)
    if value.count("${") != len(references):
        issues.append(Issue("S3.unterminated_variable", path, "a '${' has no closing '}'"))
    for reference in references:
        if reference in ("package", "data"):
            continue
        if reference == "python":
            if not python_runtime:
                issues.append(Issue(
                    "S4.python_without_runtime", path, "${python} needs runtime.kind 'python'"
                ))
            continue
        match = _CONFIG_REFERENCE.fullmatch(reference)
        if match is None:
            issues.append(Issue(
                "S3.unknown_variable", path, f"unknown template variable ${{{reference}}}"
            ))
        elif match.group(1) not in config:
            issues.append(Issue(
                "S3.undefined_config", path, f"config.{match.group(1)} is not declared"
            ))
        elif match.group(1) in secret_keys and not secret_allowed:
            issues.append(Issue(
                "S3.secret_position",
                path,
                f"secret config.{match.group(1)} may appear only in env values or bearer_token",
            ))
    return issues


def _command_issues(path: str, command: str, disclosed: set[str]) -> list[Issue]:
    if command == "${python}":
        return []
    if command.startswith(_PACKAGE_PREFIX):
        if _RELATIVE_PATH.fullmatch(command[len(_PACKAGE_PREFIX):]):
            return []
        return [Issue("S8.command_location", path, "a ${package}/ command must stay inside the package")]
    if "${" in command or "/" in command or "\\" in command:
        return [Issue(
            "S8.command_location",
            path,
            "command must be ${python}, a path under ${package}/, or a bare executable name",
        )]
    if command not in disclosed and "*" not in disclosed:
        return [Issue(
            "S8.undisclosed_process",
            path,
            f"bare command {command!r} must be listed in permissions.os.process",
        )]
    return []


def _config_issues(path: str, field: dict[str, Any]) -> list[Issue]:
    issues = []
    kind = field["type"]
    has_default = "default" in field
    if kind == "enum":
        values = [option if isinstance(option, str) else option["value"] for option in field["options"]]
        if len(values) != len(set(values)):
            issues.append(Issue("S6.duplicate_option", f"{path}/options", "option values must be unique"))
        if has_default and field["default"] not in values:
            issues.append(Issue(
                "S6.default_not_in_options", f"{path}/default", "default must be one of the options"
            ))
    if kind in ("integer", "number"):
        low, high = field.get("min"), field.get("max")
        if low is not None and high is not None and low > high:
            issues.append(Issue("S6.invalid_range", path, "min must not exceed max"))
        if has_default:
            value = field["default"]
            if (low is not None and value < low) or (high is not None and value > high):
                issues.append(Issue(
                    "S6.default_out_of_range", f"{path}/default", "default lies outside [min, max]"
                ))
    if kind in ("string", "url") and has_default:
        if len(field["default"]) > field.get("max_length", 4096):
            issues.append(Issue("S6.default_too_long", f"{path}/default", "default exceeds max_length"))
    return issues


def manifest_issues(manifest: Any) -> list[Issue]:
    """Schema issues, or semantic issues once the manifest is schema-valid."""

    return schema_issues(manifest) or semantic_issues(manifest)


def _is_link(path: Path) -> bool:
    # os.path.isjunction exists from Python 3.12; junctions are Windows-only links.
    is_junction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(is_junction and is_junction(path))


def package_issues(manifest: dict[str, Any], package_dir: Path) -> list[Issue]:
    """Checks of spec section 3.10 for files the manifest refers to."""

    issues = []
    for index, skill in enumerate(manifest["contributes"].get("skill", [])):
        issues.extend(_skill_issues(f"/contributes/skill/{index}/path", package_dir, skill["path"]))
    if 'icon' in manifest:
        issues.extend(_package_image_issues('/icon', package_dir, manifest['icon'], 1024 * 1024, 'P5.package_icon'))
    for index, theme in enumerate(manifest['contributes'].get('theme', [])):
        background = theme.get('background')
        if background:
            pointer = f'/contributes/theme/{index}/background/image'
            issues.extend(_package_image_issues(pointer, package_dir, background['image'], 8 * 1024 * 1024, 'P4.theme_image'))
    for index, server in enumerate(manifest["contributes"].get("mcp_server", [])):
        command = server.get("command", "")
        if server["transport"] != "stdio" or not command.startswith(_PACKAGE_PREFIX):
            continue
        relative = command[len(_PACKAGE_PREFIX):]
        pointer = f"/contributes/mcp_server/{index}/command"
        issues.extend(_package_file_issues(pointer, package_dir, relative))
    return issues


def _package_image_issues(pointer: str, package_dir: Path, relative: str, limit: int, code: str) -> list[Issue]:
    issues = _package_file_issues(pointer, package_dir, relative)
    if issues:
        return issues
    image = package_dir / relative
    if image.suffix.lower() not in {'.png', '.jpg', '.jpeg'} or image.stat().st_size > limit:
        return [Issue(code, pointer, f'Image must be PNG/JPEG, at most {limit // (1024 * 1024)} MiB')]
    with image.open('rb') as stream:
        header = stream.read(16)
    matches = (image.suffix.lower() == '.png' and header.startswith(b'\x89PNG\r\n\x1a\n')
               or image.suffix.lower() in {'.jpg', '.jpeg'} and header.startswith(b'\xff\xd8\xff'))
    return [] if matches else [Issue(code, pointer, 'Image signature does not match its file type')]


def _walk_to(pointer: str, package_dir: Path, relative: str) -> tuple[Path | None, list[Issue]]:
    target = package_dir
    for part in relative.split("/"):
        target = target / part
        if _is_link(target):
            return None, [Issue("P2.link_in_package", pointer, f"{relative} passes through a link")]
    return target, []


def _skill_issues(pointer: str, package_dir: Path, relative: str) -> list[Issue]:
    target, issues = _walk_to(pointer, package_dir, relative)
    if target is None:
        return issues
    if not target.is_dir():
        return [Issue("P2.missing_skill", pointer, f"{relative} is not a directory")]
    if not (target / "SKILL.md").is_file():
        return [Issue("P2.missing_skill", pointer, f"{relative}/SKILL.md is missing")]
    total = 0
    for current, directories, files in os.walk(target):
        for name in [*directories, *files]:
            entry = Path(current) / name
            if _is_link(entry):
                location = entry.relative_to(package_dir).as_posix()
                return [Issue("P2.link_in_package", pointer, f"{location} is a link")]
        total += sum((Path(current) / name).stat().st_size for name in files)
    if total > SKILL_LIMIT_BYTES:
        return [Issue("P2.skill_too_large", pointer, f"{relative} exceeds 16 MiB")]
    return []


def _package_file_issues(pointer: str, package_dir: Path, relative: str) -> list[Issue]:
    target, issues = _walk_to(pointer, package_dir, relative)
    if target is None:
        return issues
    if not target.is_file():
        return [Issue("P3.missing_file", pointer, f"{relative} does not exist in the package")]
    return []


def validate_path(path: Path) -> list[Issue]:
    """Validate a manifest file, or a package directory and its referenced files."""

    path = Path(path)
    package_dir = path if path.is_dir() else None
    manifest_path = path / MANIFEST_NAME if package_dir is not None else path
    if not manifest_path.is_file():
        return [Issue("P1.missing_manifest", "/", f"{manifest_path} does not exist")]
    if manifest_path.stat().st_size > MANIFEST_LIMIT_BYTES:
        return [Issue("P1.manifest_too_large", "/", "the manifest exceeds 256 KiB")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return [Issue("P1.invalid_json", "/", str(error))]
    issues = manifest_issues(manifest)
    if not issues and package_dir is not None:
        issues = package_issues(manifest, package_dir)
    return issues


def main(argv: list[str]) -> int:
    if not argv:
        print((__doc__ or "").strip(), file=sys.stderr)
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    failed = False
    for raw in argv:
        issues = validate_path(Path(raw))
        print(f"{raw}: {'invalid' if issues else 'ok'}")
        for issue in issues:
            print(f"  {issue}")
        failed = failed or bool(issues)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
