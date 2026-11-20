"""Host-owned SDK selection and static Python dependency compatibility."""
from __future__ import annotations

import json
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

SOURCE = Path(__file__).with_name("protocol-source.json")


def sdk_spec() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))["sdk"]


def validate_sdk_requirements(requirements: list[str]) -> None:
    """Evaluate wheel requirements for the same interpreter that creates its venv.

    Inspection does not request extras. Do not print arbitrary requirement text:
    it can contain an authenticated direct URL, even for unrelated dependencies.
    """
    version = sdk_spec()["version"]
    for text in requirements:
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            raise ValueError("wheel contains an invalid Python dependency requirement") from None
        if canonicalize_name(requirement.name) != "amadeus-extension-sdk":
            continue
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        if requirement.url:
            raise ValueError("SDK direct URL dependencies are unsupported; Amadeus supplies its pinned SDK")
        if version not in requirement.specifier:
            raise ValueError(f"extension requires SDK {requirement.specifier}; Amadeus provides {version}")


def dependency_mode(requirements: list[str]) -> str:
    """Classify effective requirements in the same interpreter as installation."""
    validate_sdk_requirements(requirements)
    for text in requirements:
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            raise ValueError("wheel contains an invalid Python dependency requirement") from None
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        if canonicalize_name(requirement.name) != "amadeus-extension-sdk" or requirement.extras:
            return "resolve"
    return "offline"
