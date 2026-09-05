from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

from tools.build_source_release import (
    build_report,
    create_archive,
    scan_selected_files,
    select_paths,
)
from tools.check_third_party_provenance import (
    matches_any,
    release_blockers_for_paths,
    validate_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def test_double_star_glob_matches_root_and_nested_files() -> None:
    assert matches_any("GPT_SoVITS/model.py", ["GPT_SoVITS/**"])
    assert matches_any("wallpaper/scene/deep/frame.png", ["wallpaper/**/*.png"])
    assert matches_any("wallpaper/frame.png", ["wallpaper/**/*.png"])
    assert not matches_any("tts/model.py", ["GPT_SoVITS/**"])


def test_release_selection_uses_allowlist_and_explicit_exclusions() -> None:
    policy = {
        "include_roots": ["server", "render"],
        "include_files": ["LICENSE", ".env.example"],
        "exclude_globs": [
            "assets/images/**",
            "render/web/vendor/live2dcubismcore.min.js",
        ],
    }
    selected, excluded = select_paths(
        [
            "LICENSE",
            ".env.example",
            ".env",
            "server/app.py",
            "render/headless_bridge.py",
            "legacy/pyqt/render_engine.py",
            "assets/images/private.png",
            "render/web/vendor/live2dcubismcore.min.js",
            "unrelated.txt",
        ],
        policy,
    )
    assert selected == [".env.example", "LICENSE", "render/headless_bridge.py", "server/app.py"]
    assert "legacy/pyqt/render_engine.py" in excluded
    assert "assets/images/private.png" in excluded
    assert "render/web/vendor/live2dcubismcore.min.js" in excluded
    assert ".env" in excluded


def test_content_scanner_suppresses_secret_value(tmp_path: Path) -> None:
    source = tmp_path / "settings.py"
    sample_value = "super-secret-value-that-must-not-appear"
    source.write_text(f'api_key = "{sample_value}"\n', encoding="utf-8")
    policy = {
        "deny_globs": [],
        "deny_allow_globs": [],
        "content_scan_extensions": [".py"],
        "content_rules": [
            {
                "id": "secret",
                "severity": "error",
                "pattern": r"api_key\s*=\s*['\"][^'\"]+['\"]",
            }
        ],
        "max_file_bytes": 1024,
        "large_file_allow_globs": [],
    }
    issues = scan_selected_files(tmp_path, ["settings.py"], {"settings.py": "100644"}, policy)
    assert len(issues) == 1
    rendered = json.dumps(issues)
    assert issues[0]["rule"] == "secret"
    assert sample_value not in rendered
    assert issues[0]["line"] == 1


def test_dirty_check_reports_tracked_deletion_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "a.py"
    source.write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    policy = {
        "distribution_model": "source",
        "first_party_license": {"status": "selected", "license_file": "LICENSE"},
        "required_files": [],
        "include_roots": ["src"],
        "include_files": [],
        "exclude_globs": [],
        "deny_globs": [],
        "deny_allow_globs": [],
        "content_scan_extensions": [".py"],
        "content_rules": [],
        "max_file_bytes": 1024,
        "large_file_allow_globs": [],
        "provenance_manifest": "missing-provenance.json",
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "src/a.py", "pyproject.toml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    source.unlink()

    report = build_report(
        root=tmp_path,
        policy_path=policy_path,
        policy=policy,
        allow_dirty_check=True,
    )

    assert report["selected_file_count"] == 0
    assert any(issue["rule"] == "selected-file-missing" for issue in report["issues"])


def test_provenance_validation_and_release_blockers(tmp_path: Path) -> None:
    license_file = tmp_path / "THIRD_PARTY_LICENSE"
    license_file.write_text("license text", encoding="utf-8")
    manifest = {
        "schema": "amadeus.third-party-provenance.v1",
        "inventory_roots": ["vendor"],
        "components": [
            {
                "id": "ready",
                "paths": ["vendor/ready/**"],
                "source_url": "https://example.invalid/ready",
                "source_revision": "v1",
                "license_expression": "MIT",
                "license_evidence": ["THIRD_PARTY_LICENSE"],
                "release_action": "include",
                "gate_status": "ready",
            },
            {
                "id": "review",
                "paths": ["vendor/review/**"],
                "source_url": "https://example.invalid/review",
                "source_revision": None,
                "license_expression": "NOASSERTION",
                "license_evidence": [],
                "release_action": "include",
                "gate_status": "review",
            },
            {
                "id": "excluded",
                "paths": ["vendor/proprietary.js"],
                "source_url": None,
                "source_revision": None,
                "license_expression": "LicenseRef-Proprietary",
                "license_evidence": [],
                "release_action": "exclude",
                "gate_status": "ready",
            },
        ],
    }
    files = ["vendor/ready/code.py", "vendor/review/code.py", "vendor/proprietary.js"]
    assert validate_manifest(manifest, root=tmp_path, files=files) == []
    blockers = release_blockers_for_paths(manifest, files)
    assert {(item["rule"], item["component"]) for item in blockers} == {
        ("unresolved-provenance-component", "review"),
        ("excluded-provenance-component", "excluded"),
    }


def test_archive_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("license\n", encoding="utf-8")
    records = []
    for relative in ["LICENSE", "src/a.py"]:
        data = (tmp_path / relative).read_bytes()
        records.append(
            {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    report = {
        "schema": "amadeus.source-release-manifest.v1",
        "version": "1.2.3",
        "commit": "a" * 40,
        "source_date_epoch": 1_700_000_000,
        "release_ready": True,
        "selected_files": records,
        "issues": [],
    }
    policy = {"archive_prefix": "amadeus-{version}"}
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    modes = {"LICENSE": "100644", "src/a.py": "100644"}
    create_archive(root=tmp_path, output=first, report=report, policy=policy, modes=modes)
    create_archive(root=tmp_path, output=second, report=report, policy=policy, modes=modes)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == [
            "amadeus-1.2.3/LICENSE",
            "amadeus-1.2.3/src/a.py",
            "amadeus-1.2.3/SOURCE_MANIFEST.json",
        ]


def test_current_source_policy_has_no_selected_provenance_blockers() -> None:
    policy_path = ROOT / "release" / "source_release_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))

    report = build_report(
        root=ROOT,
        policy_path=policy_path,
        policy=policy,
        allow_dirty_check=True,
    )

    allowed = {
        "unresolved-provenance-component",
    }
    unexpected = [
        issue for issue in report["issues"] if str(issue.get("rule") or "") not in allowed
    ]
    assert unexpected == []
    blocker_components = {
        str(issue.get("component") or "")
        for issue in report["issues"]
        if issue.get("rule") == "unresolved-provenance-component"
    }
    assert blocker_components == set()

    selected, excluded = select_paths(
        [
            "tools/AP_BWE_main/models/model.py",
            "tools/audio_sr.py",
            "tts/optional_ap_bwe.py",
        ],
        policy,
    )
    assert selected == ["tts/optional_ap_bwe.py"]
    assert excluded == [
        "tools/AP_BWE_main/models/model.py",
        "tools/audio_sr.py",
    ]


def test_gpt_sovits_release_boundary_excludes_optional_or_unverified_material() -> None:
    policy = json.loads(
        (ROOT / "release" / "source_release_policy.json").read_text(encoding="utf-8")
    )

    selected, excluded = select_paths(
        [
            "GPT_SoVITS/text/cantonese.py",
            "GPT_SoVITS/text/chinese2.py",
            "GPT_SoVITS/text/cmudict_cache.pickle",
            "GPT_SoVITS/text/g2pw/polyphonic.pickle",
            "GPT_SoVITS/text/ja_userdic/user.dict",
            "GPT_SoVITS/text/ja_userdic/userdict.csv",
            "local_tts_infer.py",
            "tools/uvr5/vr.py",
        ],
        policy,
    )

    assert selected == ["GPT_SoVITS/text/chinese2.py", "local_tts_infer.py"]
    assert excluded == [
        "GPT_SoVITS/text/cantonese.py",
        "GPT_SoVITS/text/cmudict_cache.pickle",
        "GPT_SoVITS/text/g2pw/polyphonic.pickle",
        "GPT_SoVITS/text/ja_userdic/user.dict",
        "GPT_SoVITS/text/ja_userdic/userdict.csv",
        "tools/uvr5/vr.py",
    ]


def test_gpt_sovits_provenance_records_immediate_upstream_reliance() -> None:
    manifest = json.loads((ROOT / "LICENSES" / "provenance.json").read_text(encoding="utf-8"))
    components = {component["id"]: component for component in manifest["components"]}

    gpt = components["gpt-sovits"]
    assert "9da7e17efe05041e31d3c3f42c8730ae890397f2" in gpt["source_revision"]
    assert gpt["release_action"] == "include"
    assert gpt["gate_status"] == "ready"
    assert "LicenseRef-" not in gpt["license_expression"]
    assert "local_tts_infer.py" in gpt["paths"]
    assert "LICENSES/GPT-SoVITS-UPSTREAM-RELIANCE.md" in gpt["license_evidence"]

    assert components["bert-vits2-cantonese-frontend"]["release_action"] == "exclude"
    assert components["gpt-sovits-local-dictionaries-and-caches"]["release_action"] == "exclude"
    assert components["gpt-sovits-uvr5-tools"]["release_action"] == "exclude"


def test_first_party_brand_assets_are_release_ready() -> None:
    manifest = json.loads((ROOT / "LICENSES" / "provenance.json").read_text(encoding="utf-8"))
    components = {component["id"]: component for component in manifest["components"]}

    for component_id in ("amadeus-brand-icons", "amadeus-desktop-wallpaper"):
        component = components[component_id]
        assert component["release_action"] == "include"
        assert component["gate_status"] == "ready"
        assert component["license_expression"] == "AGPL-3.0"
        assert "LICENSES/FIRST-PARTY-BRAND-ASSETS.md" in component["license_evidence"]
