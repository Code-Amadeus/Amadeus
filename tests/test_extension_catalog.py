"""Pinned catalog reader compatibility, policy separation and dependency classification."""

import hashlib
import json
import sys
from pathlib import Path
import pytest
from server.extensions.catalog import policy, read_catalog
from server.extensions.sdk import dependency_mode

ROOT = Path(__file__).resolve().parents[1]


def entry():
    return dict(
        id="org.example.test",
        version="1.0.0",
        sha256="a" * 64,
        url="https://example.test/example-1.0.0-py3-none-any.whl",
        size=100,
        display={"display_name": "<script>third party text</script>"},
        compatibility=dict(
            host_api=">=0.1,<0.2",
            python=">=3.12",
            platforms=[sys.platform],
            sdk="0.1.0",
            dependencies=[],
            delivery="python-wheel",
            runtime="none",
            contributions=[dict(kind="skill", role="", contract="amadeus.skill/v0")],
        ),
        publisher=dict(
            id="author",
            maintainers=["author"],
            package_ids=["org.example.test"],
            evidence="https://example.test/ownership",
        ),
        review=dict(
            sha256="a" * 64,
            policy="curated-v1",
            reviewer="independent",
            date="2026-10-21",
            evidence="https://example.test/review",
        ),
        withdrawal=None,
    )


def index():
    return dict(
        format="amadeus.catalog/v1",
        sequence=1,
        source_commit="a" * 40,
        source_time="2026-10-21T00:00:00Z",
        generator_commit="b" * 40,
        host_commit=policy().PIN["host_commit"],
        entries=[entry()],
        security=[],
    )


def read(tmp_path, value):
    file = tmp_path / "catalog.json"
    file.write_text(json.dumps(value), encoding="utf-8")
    return read_catalog(file)


def test_catalog_source_hashes_are_independently_pinned():
    source = json.loads((ROOT / "server/extensions/catalog-source.json").read_text())
    assert source["commit"] == "58d3892d55c6a7ec6f8626153f503291bb10ab9a"
    for name, digest in source["files"].items():
        assert (
            hashlib.sha256((ROOT / "vendor/extension-catalog" / name).read_bytes()).hexdigest()
            == digest
        )


def test_future_type_contract_and_mixed_package_preserve_compatible_rows_and_security(tmp_path):
    original = index()
    fixture = json.loads(
        (ROOT / "vendor/extension-catalog/catalog/fixtures/compatibility.json").read_text()
    )
    for case in fixture["cases"]:
        future = entry()
        future["id"] += "." + case["name"]
        future["publisher"]["package_ids"] = [future["id"]]
        future["compatibility"].update(case["compatibility"])
        original["entries"].append(future)
    original["security"] = [
        dict(
            id=entry()["id"],
            version="1.0.0",
            sha256="a" * 64,
            status="blocked",
            sequence=1,
            reason="security",
            evidence="https://example.test/ban",
        )
    ]
    result = read(tmp_path, original)
    assert not result["entries"][0]["incompatible"]
    assert [not row["incompatible"] for row in result["entries"][1:]] == [
        case["compatible"] for case in fixture["cases"]
    ]
    assert result["security"] == original["security"]


@pytest.mark.parametrize(
    "field,value", [("format", "amadeus.catalog/v2"), ("security", [{}]), ("sequence", -1)]
)
def test_invalid_global_format_or_security_rejects_whole_snapshot(tmp_path, field, value):
    original = index()
    original[field] = value
    with pytest.raises(ValueError):
        read(tmp_path, original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_api", ">=9.0"),
        ("python", ">=99"),
        ("platforms", ["future-os"]),
        ("sdk", "9.0.0"),
        ("dependencies", ["third-party>=1"]),
    ],
)
def test_environment_and_dependency_incompatibility_is_per_entry(tmp_path, field, value):
    original = index()
    original["entries"][0]["compatibility"][field] = value
    assert read(tmp_path, original)["entries"][0]["incompatible"]


@pytest.mark.parametrize(
    "requires,expected",
    [
        ([], "offline"),
        (["amadeus-extension-sdk==0.1.0"], "offline"),
        (['third-party; python_version < "1.0"'], "offline"),
        (["third-party>=1"], "resolve"),
        (["amadeus-extension-sdk[extra]==0.1.0"], "resolve"),
    ],
)
def test_same_interpreter_dependency_classification(requires, expected):
    assert dependency_mode(requires) == expected


@pytest.mark.parametrize(
    "requires",
    [
        ["amadeus-extension-sdk==9.9"],
        ["amadeus-extension-sdk @ https://example.test/sdk.whl"],
        ["invalid requirement !"],
    ],
)
def test_invalid_requirements_do_not_become_offline(requires):
    with pytest.raises(ValueError):
        dependency_mode(requires)


def test_empty_platform_summary_means_no_platform_restriction(tmp_path):
    original = index()
    original["entries"][0]["compatibility"]["platforms"] = []
    assert not read(tmp_path, original)["entries"][0]["incompatible"]
