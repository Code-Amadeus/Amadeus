from __future__ import annotations

import json
from pathlib import Path

from packaging.markers import default_environment

from tools.audit_cu124_dependencies import (
    compare_dependencies,
    observed_requirements,
    parse_pip_audit,
    parse_requirement_files,
)


def test_requirement_parser_respects_markers_and_inline_comments(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "numpy==1.23.4\n"
        "opencc; sys_platform != 'linux'\n"
        "onnxruntime; sys_platform == 'darwin'  # optional mac path\n",
        encoding="utf-8",
    )
    environment = default_environment()
    environment["sys_platform"] = "win32"
    rows = parse_requirement_files([requirements], environment)
    active = {row.get("canonical_name") for row in rows if row.get("active")}
    assert active == {"numpy", "opencc"}
    assert rows[0]["source"].endswith("requirements.txt")


def test_dependency_comparison_reports_missing_mismatch_and_import_candidate() -> None:
    environment = {
        "packages": [
            {"name": "numpy", "version": "1.26.4", "license": "BSD"},
            {"name": "fastapi", "version": "1.0", "license": "MIT"},
            {"name": "torch", "version": "2.5.1+cu124", "license": "BSD-3-Clause"},
        ],
        "package_map": {"numpy": ["numpy"], "fastapi": ["fastapi"], "torch": ["torch"]},
    }
    requirements = [
        {
            "kind": "requirement",
            "active": True,
            "name": "numpy",
            "canonical_name": "numpy",
            "specifier": "==1.23.4",
            "raw": "numpy==1.23.4",
            "source": "requirements.txt",
            "line": 1,
        },
        {
            "kind": "requirement",
            "active": True,
            "name": "missing-package",
            "canonical_name": "missing-package",
            "specifier": ">=1",
            "raw": "missing-package>=1",
            "source": "requirements.txt",
            "line": 2,
        },
    ]
    imports = {
        "numpy": [{"path": "core/x.py", "line": 1}],
        "fastapi": [{"path": "server/x.py", "line": 2}],
        "torch": [{"path": "tts/x.py", "line": 3}],
    }
    result = compare_dependencies(environment, requirements, imports)
    assert [row["name"] for row in result["declared_missing"]] == ["missing-package"]
    assert [row["name"] for row in result["declared_mismatched"]] == ["numpy"]
    assert [row["module"] for row in result["imported_undeclared_candidates"]] == ["fastapi"]




def test_pyproject_expansion_marks_base_active_and_tiers_by_active_extras(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        "name = \"demo\"\n"
        "version = \"0.0.1\"\n"
        "requires-python = \">=3.12\"\n"
        "dependencies = [\"idna==3.10\"]\n"
        "\n"
        "[project.optional-dependencies]\n"
        "voice = [\"PyAudio==0.2.14\"]\n"
        "dev = [\"pytest==8.4.2\"]\n"
        "\n"
        "[tool.uv.sources]\n"
        "\n"
        "[[tool.uv.index]]\n"
        "name = \"unused\"\n"
        "url = \"https://example.invalid/simple\"\n"
        "explicit = true\n",
        encoding="utf-8",
    )
    from tools.audit_cu124_dependencies import parse_pyproject_dependencies
    rows = parse_pyproject_dependencies(pyproject, {}, active_extras=("voice",))
    by_name = {r["canonical_name"]: r for r in rows if r["kind"] == "requirement"}
    assert by_name["idna"]["active"] is True
    assert by_name["pyaudio"]["active"] is True
    assert "extra == \"voice\"" in by_name["pyaudio"]["marker"]
    assert by_name["pytest"]["active"] is False

def test_pyproject_expansion_respects_platform_markers(tmp_path: Path) -> None:
    from tools.audit_cu124_dependencies import parse_pyproject_dependencies

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\ndependencies = [\"pywin32; sys_platform == 'win32'\"]\n"
        "[project.optional-dependencies]\nvoice = [\"pyaudio; sys_platform == 'win32'\"]\n",
        encoding="utf-8",
    )
    rows = parse_pyproject_dependencies(pyproject, {"sys_platform": "darwin"}, active_extras=("voice",))
    assert all(not row["active"] for row in rows)
    rows = parse_pyproject_dependencies(pyproject, {"sys_platform": "win32"}, active_extras=("voice",))
    assert all(row["active"] for row in rows)


def test_pip_audit_parser_normalizes_findings(tmp_path: Path) -> None:
    source = tmp_path / "pip-audit.json"
    source.write_text(
        json.dumps(
            {
                "dependencies": [
                    {
                        "name": "example",
                        "version": "1.0",
                        "vulns": [
                            {
                                "id": "PYSEC-1",
                                "fix_versions": ["1.1"],
                                "aliases": ["CVE-1"],
                                "description": "intentionally discarded",
                            }
                        ],
                    }
                ],
                "fixes": [],
            }
        ),
        encoding="utf-8",
    )
    result = parse_pip_audit(source, tool_version="2.10.1", service="pypi")
    assert result["vulnerability_count"] == 1
    assert result["vulnerable_package_count"] == 1
    assert result["tool_version"] == "2.10.1"
    assert "description" not in result["vulnerable_packages"][0]["vulnerabilities"][0]


def test_observed_snapshot_never_contains_paths_or_direct_urls() -> None:
    report = {
        "environment": {
            "packages": [
                {"name": "torch", "version": "2.5.1+cu124"},
                {"name": "Example", "version": "1.0"},
            ]
        }
    }
    rendered = observed_requirements(report)
    assert "Example==1.0" in rendered
    assert "torch==2.5.1+cu124" in rendered
    assert "file://" not in rendered
    assert "C:\\" not in rendered
