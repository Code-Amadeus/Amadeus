"""Host consumer of the independently pinned catalog policy, never an execution owner."""

from __future__ import annotations
import importlib.util
import json
import platform
import sys
from functools import lru_cache
from pathlib import Path
from packaging.specifiers import SpecifierSet
from server.extensions.package import HOST_API, matches_version
from server.extensions.profile import PROFILE
from server.extensions.sdk import dependency_mode, sdk_spec

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def policy():
    spec = importlib.util.spec_from_file_location(
        "amadeus_catalog_policy", ROOT / "vendor/extension-catalog/tools/catalog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_catalog(path: Path) -> dict:
    owner = policy()
    index = owner.validate_index(owner.read_json(path))
    schema = json.loads(
        (ROOT / "server/extensions/amadeus-extension.v0.schema.json").read_text(encoding="utf-8")
    )
    definitions = schema["$defs"]

    def contracts(node):
        if "$ref" in node:
            yield from contracts(definitions[node["$ref"].rsplit("/", 1)[-1]])
        properties = node.get("properties", {})
        contract = properties.get("contract", {}).get("const")
        if contract:
            yield properties.get("role", {}).get("const", ""), contract
        for branch in ("then", "else"):
            if branch in node:
                yield from contracts(node[branch])

    capabilities = {
        "delivery": ["python-wheel"],
        "runtime": ["none", "python"],
        "contributions": [
            {"kind": row["kind"], "role": role, "contract": contract}
            for row in PROFILE
            if row["status"] == "open"
            for role, contract in contracts(
                definitions["contributes"]["properties"][row["kind"]]["items"]
            )
            if role == row.get("role", "")
        ],
    }
    entries = []
    for entry in index["entries"]:
        reasons = owner.incompatible_reasons(entry, capabilities)
        c = entry["compatibility"]
        try:
            if not matches_version(HOST_API, c["host_api"]):
                reasons.append("incompatible Host API")
            if c["platforms"] and sys.platform not in c["platforms"]:
                reasons.append("unsupported platform")
            if platform.python_version() not in SpecifierSet(c["python"]):
                reasons.append("incompatible Python version")
            if c["sdk"] != sdk_spec()["version"]:
                reasons.append("incompatible catalog SDK")
            if dependency_mode(c["dependencies"]) != "offline":
                reasons.append("catalog dependencies are unsupported")
            owner.wheel_filename(entry["url"])
        except ValueError:
            reasons.append("unsupported environment or delivery requirement")
        entries.append({**entry, "incompatible": reasons})
    return {**index, "entries": entries}
