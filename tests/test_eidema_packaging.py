"""The independent core must survive locked installs and source distribution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools.build_source_release import create_archive, select_paths
from tools.package_eidema import (ARCHIVE_CONFIG, BUNDLE, POLICY_READER, ROOT, SCHEMA,
                                  build_bundle, check_bundle, sync_policy_metadata)


def _bundle(root: Path) -> tuple[Path, dict]:
    folder = root / BUNDLE
    folder.mkdir(parents=True)
    filename = "eidema-0.1.1-py3-none-any.whl"
    wheel = folder / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("eidema-0.1.1.dist-info/METADATA", "Metadata-Version: 2.4\n"
                         "Name: eidema\nVersion: 0.1.1\nRequires-Python: >=3.10\n"
                         "Requires-Dist: numpy>=1.24\n")
        archive.writestr("eidema-0.1.1.dist-info/WHEEL", "Wheel-Version: 1.0\n"
                         "Root-Is-Purelib: true\nTag: py3-none-any\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {"schema": SCHEMA, "source": {"revision": "a" * 40, "tree": "b" * 40},
                "build": {"archive_config": ARCHIVE_CONFIG.copy()},
                "package": {"name": "eidema", "version": "0.1.1", "requires_python": ">=3.10",
                            "requires_dist": ["numpy>=1.24"]},
                "artifact": {"filename": filename, "sha256": digest, "size": wheel.stat().st_size}}
    (folder / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = f"vendor/eidema/{filename}"
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies=["eidema==0.1.1"]\n[tool.uv.sources]\n'
        f'eidema={{path="{path}"}}\n', encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname="eidema"\nversion="0.1.1"\n'
        f'source={{path="{path}"}}\nwheels=[{{hash="sha256:{digest}"}}]\n', encoding="utf-8",
    )
    return wheel, manifest


def test_bundle_bytes_declaration_and_lock_agree(tmp_path: Path) -> None:
    wheel, manifest = _bundle(tmp_path)
    assert check_bundle(tmp_path) == manifest
    wheel.write_bytes(wheel.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="bytes differ"):
        check_bundle(tmp_path)


@pytest.mark.parametrize("config", [None, {}, {"core.autocrlf": "true", "core.eol": "lf"},
                                    {"core.autocrlf": "false", "core.eol": "crlf"}])
def test_bundle_requires_pinned_archive_text_conversion(tmp_path: Path, config: dict | None) -> None:
    _, manifest = _bundle(tmp_path)
    manifest["build"]["archive_config"] = config
    (tmp_path / BUNDLE / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Git line-ending configuration"):
        check_bundle(tmp_path)


@pytest.mark.parametrize("file,old,new", [
    ("pyproject.toml", '"eidema==0.1.1"', '"eidema>=0.1"'),
    ("pyproject.toml", "vendor/eidema/eidema-0.1.1-py3-none-any.whl", "../Eidema"),
    ("uv.lock", "vendor/eidema/eidema-0.1.1-py3-none-any.whl", "../Eidema"),
    ("uv.lock", "sha256:", "sha512:"),
])
def test_unpinned_or_external_core_sources_fail(tmp_path: Path, file: str, old: str, new: str) -> None:
    _bundle(tmp_path)
    path = tmp_path / file
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    with pytest.raises(ValueError):
        check_bundle(tmp_path)


def test_policy_reader_is_generated_and_verified_from_wheel_bytes(tmp_path: Path) -> None:
    wheel, manifest = _bundle(tmp_path)
    data = b'from __future__ import annotations\nCONTRACT_VERSION = "synthetic-reader-v1"\n'
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("eidema/policy_metadata.py", data)
    old_digest = manifest["artifact"]["sha256"]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest["artifact"].update(sha256=digest, size=wheel.stat().st_size)
    (tmp_path / BUNDLE / "provenance.json").write_text(json.dumps(manifest), encoding="utf-8")
    lock = tmp_path / "uv.lock"
    lock.write_text(lock.read_text(encoding="utf-8").replace(old_digest, digest), encoding="utf-8")
    generated = sync_policy_metadata(tmp_path)
    assert (tmp_path / POLICY_READER).read_bytes() == data
    assert generated["host_policy_metadata"] == {
        "path": POLICY_READER.as_posix(), "wheel_member": "eidema/policy_metadata.py",
        "contract": "synthetic-reader-v1", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    assert check_bundle(tmp_path) == generated
    (tmp_path / POLICY_READER).write_bytes(data + b"# changed\n")
    with pytest.raises(ValueError, match="Host policy reader differs"):
        check_bundle(tmp_path)
    sync_policy_metadata(tmp_path)
    generated["host_policy_metadata"]["contract"] = "forged-contract"
    (tmp_path / BUNDLE / "provenance.json").write_text(json.dumps(generated), encoding="utf-8")
    with pytest.raises(ValueError, match="Host policy reader differs"):
        check_bundle(tmp_path)


def test_build_uses_only_reviewed_commit_and_reproduces_bytes(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "eidema").mkdir()
    committed_python = b'VALUE = "committed"\n'
    (source / "eidema/__init__.py").write_bytes(committed_python)
    (source / ".gitignore").write_bytes(b"eidema/ignored.py\n")
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n'
        '[project]\nname="eidema"\nversion="0.1.1"\nrequires-python=">=3.10"\n'
        'dependencies=["numpy>=1.24"]\n', encoding="utf-8",
    )
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("config", "core.eol", "lf")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    revision = git("rev-parse", "HEAD")
    (source / "eidema/__init__.py").write_text('VALUE = "unreviewed"\n', encoding="utf-8")
    (source / "eidema/ignored.py").write_text('VALUE = "ignored"\n', encoding="utf-8")
    (source / "eidema/untracked.py").write_text('VALUE = "untracked"\n', encoding="utf-8")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    git("config", "core.autocrlf", "true")
    git("config", "core.eol", "crlf")
    first = build_bundle(source, revision, root=tmp_path)
    first_wheel = (tmp_path / BUNDLE / first["artifact"]["filename"]).read_bytes()
    git("config", "core.autocrlf", "false")
    git("config", "core.eol", "lf")
    second = build_bundle(source, revision, root=tmp_path)
    assert first == second
    assert first_wheel == (tmp_path / BUNDLE / second["artifact"]["filename"]).read_bytes()
    assert first["build"]["reproducible_builds"] == 2
    assert first["build"]["archive_config"] == ARCHIVE_CONFIG
    assert first["source"]["revision"] == revision
    assert "host_policy_metadata" not in first and not (tmp_path / POLICY_READER).exists()
    assert str(source) not in json.dumps(first)
    with zipfile.ZipFile(tmp_path / BUNDLE / first["artifact"]["filename"]) as archive:
        assert archive.read("eidema/__init__.py") == committed_python
        assert "eidema/ignored.py" not in archive.namelist()
        assert "eidema/untracked.py" not in archive.namelist()


def test_committed_bundle_is_self_contained_and_carried_by_source_archive(tmp_path: Path) -> None:
    manifest = check_bundle()
    wheel = BUNDLE / manifest["artifact"]["filename"]
    paths = [str(BUNDLE / name).replace("\\", "/") for name in ("README.md", "provenance.json")]
    paths.extend((wheel.as_posix(), "tools/package_eidema.py", POLICY_READER.as_posix()))
    policy = json.loads((ROOT / "release/source_release_policy.json").read_text(encoding="utf-8"))
    selected, excluded = select_paths(paths, policy)
    assert excluded == []
    assert set(paths) <= set(policy["required_files"])
    report = {"release_ready": True, "version": "fixture", "source_date_epoch": 1700000000,
              "selected_files": [{"path": path} for path in selected]}
    output = tmp_path / "source.zip"
    create_archive(root=ROOT, output=output, report=report, policy=policy, modes={})
    with zipfile.ZipFile(output) as archive:
        assert archive.read(f"amadeus-fixture/{wheel.as_posix()}") == (ROOT / wheel).read_bytes()
        assert archive.read(f"amadeus-fixture/{POLICY_READER.as_posix()}") == (ROOT / POLICY_READER).read_bytes()
    # The archive's dependency is a wheel, independent of a development checkout.
    assert not any(path.suffix == ".py" for path in (ROOT / BUNDLE).iterdir())
