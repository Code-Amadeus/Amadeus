"""Curated catalog policy and deterministic generation, separate from Host admission.

Only the fixed Host checker inspects Host contracts. No wheel is installed/imported.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date
from email.parser import Parser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from jsonschema import Draft202012Validator
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "catalog/policy.json").read_text())
PIN = json.loads((ROOT / "catalog/toolchain.json").read_text())


def fail(message):
    raise ValueError(message)


def canonical(value):
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode()


def parse_json(data, limit=4 * 1024 * 1024):
    if len(data) > limit:
        fail("JSON exceeds size limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def read_json(path, limit=4 * 1024 * 1024):
    with Path(path).open("rb") as source:
        return parse_json(source.read(limit + 1), limit)


def https_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) < 33 or ord(c) == 127 for c in value)
    ):
        fail("URL must be bounded HTTPS without credentials")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        fail("URL must be HTTPS without credentials or fragments")
    _ = parsed.port  # reject invalid ports
    return value


def wheel_filename(url):
    https_url(url)
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if "/" in name or "\\" in name or ":" in name or not name.endswith(".whl"):
        fail("wheel URL must identify a concrete wheel filename")
    parse_wheel_filename(name)
    return name


def identity(entry):
    return entry["id"], entry["version"], entry["sha256"]


def validate_index(index):
    if len(canonical(index)) > POLICY["max_index_bytes"]:
        fail("catalog exceeds size limit")
    validator = Draft202012Validator(read_json(ROOT / "catalog/schema.json"))
    if next(validator.iter_errors(index), None):
        fail("unsupported catalog format or invalid required structure")
    versions = set()
    for entry in index["entries"]:
        key = entry["id"], entry["version"]
        if key in versions:
            fail("duplicate or conflicting package identity")
        versions.add(key)
        https_url(entry["url"])
        validate_review(entry, entry["id"], entry["sha256"])
        if entry["withdrawal"]:
            https_url(entry["withdrawal"]["evidence"])
    facts = set()
    for fact in index["security"]:
        key = identity(fact)
        if key in facts or fact["sequence"] > index["sequence"]:
            fail("duplicate safety identity or invalid decision sequence")
        facts.add(key)
        if (fact["status"], fact["reason"]) not in {
            ("blocked", "security"),
            ("allowed", "security-resolved"),
        }:
            fail("inconsistent safety decision")
        https_url(fact["evidence"])
    return index


def validate_transition(previous, current):
    validate_index(previous)
    validate_index(current)
    if current["sequence"] == previous["sequence"] and canonical(current) == canonical(
        previous
    ):
        return  # exact rebuild only
    if current["sequence"] != previous["sequence"] + 1:
        fail("publication sequence must increase by exactly one")
    old = {(e["id"], e["version"]): e for e in previous["entries"]}
    new = {(e["id"], e["version"]): e for e in current["entries"]}
    for key, entry in old.items():
        if key not in new or entry["sha256"] != new[key]["sha256"]:
            fail(
                "released identity cannot disappear or change bytes; withdraw it explicitly"
            )
    old_safety = {identity(e): e for e in previous["security"]}
    new_safety = {identity(e): e for e in current["security"]}
    if not old_safety.keys() <= new_safety.keys():
        fail("known safety facts cannot disappear")
    for key, fact in new_safety.items():
        if old_safety.get(key) != fact and fact["sequence"] != current["sequence"]:
            fail("changed safety decision requires the new publication sequence")


def incompatible_reasons(entry, capabilities):
    """Reference consumer semantics; never call the full manifest validator here."""
    summary = entry["compatibility"]
    reasons = []
    if (
        summary["delivery"] not in capabilities["delivery"]
        or summary["runtime"] not in capabilities["runtime"]
    ):
        reasons.append("unsupported delivery or runtime")
    allowed = {
        (c["kind"], c.get("role", ""), c["contract"])
        for c in capabilities["contributions"]
    }
    if any(
        (c["kind"], c["role"], c["contract"]) not in allowed
        for c in summary["contributions"]
    ):
        reasons.append("unsupported contribution contract")
    return reasons


def validate_review(source, package_id, digest):
    publisher, review = source["publisher"], source["review"]
    date.fromisoformat(review["date"])
    if package_id not in publisher["package_ids"] or not publisher["maintainers"]:
        fail("publisher ownership does not cover this package ID")
    if review["sha256"] != digest or review["policy"] != POLICY["version"]:
        fail("this artifact needs its own current-policy review")
    if review["reviewer"].casefold() in {
        person.casefold() for person in [*publisher["maintainers"], publisher["id"]]
    }:
        fail("independent reviewer is required, including reference packages")
    https_url(publisher["evidence"])
    https_url(review["evidence"])


def check_policy(wheel, inspection):
    """Distribution exclusions only: Host/local install rules are unchanged."""
    manifest = inspection["manifest"]
    if not re.fullmatch(POLICY["version_pattern"], manifest["version"]):
        fail("catalog policy: version must be X.Y.Z without pre-release/build suffix")
    distribution, version, build, tags = parse_wheel_filename(wheel.name)
    if (
        canonicalize_name(inspection["distribution"]) != distribution
        or str(version) != manifest["version"]
    ):
        fail("catalog policy: wheel filename identity differs from metadata")
    if build or {str(t) for t in tags} != {POLICY["wheel_tag"]}:
        fail("catalog policy: wheel filename must be py3-none-any without a build tag")
    if PIN["python"] not in SpecifierSet(inspection.get("requires_python", "")):
        fail("catalog policy: wheel Python requirement excludes the fixed target")
    for raw in inspection["requires_dist"]:
        requirement = Requirement(raw)
        if requirement.url:
            fail("catalog policy: direct URL dependencies are excluded")
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        if (
            canonicalize_name(requirement.name) not in POLICY["allowed_dependencies"]
            or requirement.extras
        ):
            fail("catalog policy: third-party dependencies are excluded from v1")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        headers = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(headers) != 1 or archive.getinfo(headers[0]).file_size > 65536:
            fail("catalog policy: missing or oversized WHEEL metadata")
        if headers[0].removesuffix("WHEEL") + "METADATA" not in names:
            fail("catalog policy: WHEEL must belong to the distribution metadata")
        metadata = Parser().parsestr(archive.read(headers[0]).decode("utf-8"))
        if metadata.get_all("Tag") != [POLICY["wheel_tag"]] or metadata.get_all(
            "Root-Is-Purelib"
        ) != ["true"]:
            fail("catalog policy: WHEEL must declare purelib and exactly py3-none-any")
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as member:
                magic = member.read(8)
            if Path(info.filename).suffix.lower() in POLICY[
                "native_suffixes"
            ] or magic.startswith(
                (
                    b"MZ",
                    b"\x7fELF",
                    b"\0asm",
                    b"\xcf\xfa\xed\xfe",
                    b"\xfe\xed\xfa\xcf",
                    b"\xca\xfe\xba\xbe",
                    b"\xce\xfa\xed\xfe",
                    b"\xfe\xed\xfa\xce",
                )
            ):
                fail("catalog policy: native/executable binary payload is excluded")
        package = inspection["package_root"]

        def owned_script(value):
            if not value.startswith("${package}/"):
                return False
            suffix = value[len("${package}/") :]
            return (
                bool(suffix)
                and not any(p in {"", ".", ".."} for p in suffix.split("/"))
                and package + "/" + suffix in names
            )

        for server in manifest["contributes"].get("mcp_server", []):
            if server["transport"] != "stdio":
                continue
            command, args = server["command"], server.get("args", [])
            if command == "${python}":
                args = list(args)
                while args and args[0] in {"-I", "-B", "-s", "-E", "-u"}:
                    args.pop(0)
                if len(args) >= 2 and args[0] == "-m":
                    module = args[1]
                    location = module.replace(".", "/")
                    if (
                        not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", module)
                        or module.split(".")[0] != package
                        or not ({location + ".py", location + "/__main__.py"} & names)
                    ):
                        fail(
                            "catalog policy: Python module must belong to the reviewed package"
                        )
                elif (
                    not args or not owned_script(args[0]) or not args[0].endswith(".py")
                ):
                    fail(
                        "catalog policy: Python must run a reviewed package module or .py script"
                    )
            elif not owned_script(command) or not command.endswith(".py"):
                fail(
                    "catalog policy: MCP command must be Python or a reviewed package script"
                )
            # Actual executable/platform behavior and arbitrary code downloads remain human review.


def verify_host(host, python):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(host), *args], text=True
        ).strip()

    if git("rev-parse", "HEAD") != PIN["host_commit"]:
        fail("Host validator commit does not match toolchain pin")
    paths = [
        "tools/extension_package.py",
        "server/extensions",
        "agent_host/skills.py",
        "pyproject.toml",
        "vendor/extension-sdk",
    ]
    if git("status", "--porcelain", "--", *paths):
        fail("pinned Host validator inputs are modified")
    lock = host / "tools/requirements-extension-authoring.txt"
    if hashlib.sha256(lock.read_bytes()).hexdigest() != PIN["requirements_sha256"]:
        fail("Host authoring dependency export differs from pin")
    result = subprocess.run(
        [str(python), "-I", str(host / "tools/extension_package.py"), "profile"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        fail("fixed Host checker environment is unavailable")
    profile = json.loads(result.stdout)["host_profile"]
    if (profile["python"], profile["platform"]) != (PIN["python"], PIN["platform"]):
        fail("catalog qualification requires pinned Windows/Python environment")
    return profile


class BoundedRedirect(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_artifact(source, artifacts):
    digest, url = source["wheel"]["sha256"], source["wheel"]["url"]
    if not re.fullmatch("[a-f0-9]{64}", digest):
        fail("invalid artifact digest")
    target = artifacts / digest / wheel_filename(url)
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download")
    try:
        with (
            build_opener(BoundedRedirect()).open(
                https_url(url), timeout=30
            ) as response,
            temporary.open("xb") as output,
        ):
            size, hasher = 0, hashlib.sha256()
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > POLICY["max_wheel_bytes"]:
                    fail("download exceeds wheel size limit")
                hasher.update(block)
                output.write(block)
        if hasher.hexdigest() != digest:
            fail("artifact digest mismatch")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def retained_entry(source, previous):
    """Reuse immutable reviewed facts from protected main, never a display/download cache.

    Safety/withdrawal publication must not depend on old asset URLs staying online.
    New bytes, publisher facts, reviews or checker pins still need full inspection.
    """
    if set(source) != {"wheel", "publisher", "review", "withdrawal"} or set(
        source["wheel"]
    ) != {"url", "sha256"}:
        fail("source contains derived metadata or unknown manual facts")
    if previous is None or previous["host_commit"] != PIN["host_commit"]:
        return None
    for entry in previous["entries"]:
        if (
            source["wheel"] == {"url": entry["url"], "sha256": entry["sha256"]}
            and source["publisher"] == entry["publisher"]
            and source["review"] == entry["review"]
            and entry["review"]["policy"] == POLICY["version"]
        ):
            return {**entry, "withdrawal": source["withdrawal"]}
    return None


def build_entry(source, artifacts, host, python, profile):
    if set(source) != {"wheel", "publisher", "review", "withdrawal"} or set(
        source["wheel"]
    ) != {"url", "sha256"}:
        fail("source contains derived metadata or unknown manual facts")
    digest = source["wheel"]["sha256"]
    if not re.fullmatch("[a-f0-9]{64}", digest):
        fail("invalid artifact digest")
    wheel = artifacts / digest / wheel_filename(source["wheel"]["url"])
    if wheel.stat().st_size > POLICY["max_wheel_bytes"]:
        fail("artifact exceeds size limit")
    with tempfile.TemporaryDirectory(prefix="catalog-artifact-") as temporary:
        snapshot = Path(temporary) / wheel.name
        shutil.copyfile(wheel, snapshot)
        return inspect_entry(source, snapshot, host, python, profile)


def artifact_display(manifest):
    """Public artifact facts only; never copy configuration defaults or environment values."""
    return {
        **{
            k: manifest[k]
            for k in (
                "display_name",
                "description",
                "author",
                "license",
                "homepage",
                "repository",
            )
            if k in manifest
        },
        "access": {
            "os": manifest.get("permissions", {}).get("os", {}),
            "services": [
                {
                    "kind": kind,
                    **{
                        key: item[key]
                        for key in (
                            "id",
                            "deployment",
                            "transport",
                            "command",
                            "args",
                            "url",
                        )
                        if key in item
                    },
                }
                for kind in ("voice_backend", "mcp_server")
                for item in manifest["contributes"].get(kind, [])
            ],
        },
    }


def inspect_entry(source, wheel, host, python, profile):
    digest = source["wheel"]["sha256"]
    with wheel.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            fail("artifact digest mismatch")
    result = subprocess.run(
        [
            str(python),
            "-I",
            str(host / "tools/extension_package.py"),
            "check-wheel",
            str(wheel),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        fail(
            "fixed Host check-wheel rejected artifact (run it locally for its sanitized diagnostic)"
        )
    inspection = json.loads(result.stdout)
    if inspection["digest"] != digest:
        fail("artifact changed during inspection")
    check_policy(wheel, inspection)
    manifest = inspection["manifest"]
    validate_review(source, manifest["id"], digest)
    contributions = [
        {"kind": kind, "role": item.get("role", ""), "contract": item["contract"]}
        for kind, items in manifest["contributes"].items()
        for item in items
    ]
    return {
        **{k: source[k] for k in ("publisher", "review", "withdrawal")},
        "id": manifest["id"],
        "version": manifest["version"],
        "sha256": digest,
        "url": source["wheel"]["url"],
        "size": wheel.stat().st_size,
        "display": artifact_display(manifest),
        "compatibility": {
            "host_api": manifest["engines"]["amadeus_host_api"],
            "python": str(
                SpecifierSet(manifest["runtime"].get("requires_python", ""))
                & SpecifierSet(inspection.get("requires_python", ""))
            ),
            "platforms": manifest["engines"].get("platforms", []),
            "sdk": profile["sdk_version"],
            "dependencies": inspection["requires_dist"],
            "contributions": sorted(contributions, key=lambda c: tuple(c.values())),
            "delivery": "python-wheel",
            "runtime": manifest["runtime"]["kind"],
        },
    }


def wheel_diff(before, after):
    """Bounded per-file text diffs and hashes over the actual reviewed wheel bytes."""

    def files(path):
        if path is None:
            return {}
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (
                len(infos) > 10000
                or sum(i.file_size for i in infos) > POLICY["max_wheel_bytes"]
            ):
                fail("diff archive exceeds limits")
            return {i.filename: archive.read(i) for i in infos if not i.is_dir()}

    old, new = files(before), files(after)
    changes = []
    for name in sorted(old.keys() | new.keys()):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        row = {
            "path": name,
            "status": "added" if a is None else "removed" if b is None else "modified",
            "before": hashlib.sha256(a).hexdigest() if a is not None else None,
            "after": hashlib.sha256(b).hexdigest() if b is not None else None,
        }
        try:
            if max(len(a or b""), len(b or b"")) <= 65536:
                lines = list(
                    difflib.unified_diff(
                        (a or b"").decode("utf-8").splitlines(),
                        (b or b"").decode("utf-8").splitlines(),
                        lineterm="",
                    )
                )
                row["text_diff"] = lines[:400]
                row["diff_truncated"] = len(lines) > 400
        except UnicodeError:
            pass
        changes.append(row)
    return changes
