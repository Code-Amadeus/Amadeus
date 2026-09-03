"""Four-tier install contract: extras, profiles, uv.lock, and resolver behavior.

Freezes the L1–L4 install contract so future edits cannot silently drift:
which packages live in which extra, which modules each verify profile
requires, and how uv.lock routes torch to the PyTorch cu124 index only for
the Windows + local-cu124 selection. pyproject.toml is the single dependency
declaration; uv.lock is the single lockfile.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]

_BANNED_IN_L1 = ("torch", "pyaudio", "silero-vad", "onnxruntime")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _pyproject_extras() -> dict[str, list[str]]:
    return _pyproject()["project"]["optional-dependencies"]


def _uv_config() -> dict:
    return _pyproject()["tool"]["uv"]


def _dist_name(requirement: str) -> str:
    return re.split(r"[=<>!\[]", requirement.strip(), maxsplit=1)[0].strip().lower()


def _lock_package_blocks() -> list[str]:
    text = (ROOT / "uv.lock").read_text(encoding="utf-8")
    return [b for b in text.split("\n[[package]]\n") if b.strip()]


def _lock_root_block() -> str:
    for block in _lock_package_blocks():
        if block.startswith('name = "amadeus"\n'):
            return block
    raise AssertionError("uv.lock has no root amadeus package entry")


def _lock_root_requires_dist() -> list[dict]:
    block = _lock_root_block()
    return _lock_metadata_list("requires-dist", block)


def _lock_metadata_list(key: str, block: str) -> list[dict]:
    """Parse a `key = [ { ... } ]` list inside a lock package block."""
    m = re.search(rf"{key} = \[(.*?)\]\n(?:\n|(?:[a-z-]+ = )|\[|\Z)", block, re.S)
    if not m:
        return []
    return [
        {"name": x.group("name"), "marker": x.group("marker") or "",
         "spec": x.group("spec") or "", "index": x.group("index") or ""}
        for x in re.finditer(
            r"\{\s*name = \"(?P<name>[^\"]+)\""
            r"(?:, marker = \"(?P<marker>[^\"]+)\")?"
            r"(?:, specifier = \"(?P<spec>[^\"]+)\")?"
            r"(?:, index = \"(?P<index>[^\"]+)\")?",
            m.group(1),
        )
    ]


def test_voice_extra_is_torch_free_and_vad_pulls_silero_with_same_torch() -> None:
    extras = _pyproject_extras()
    voice = {_dist_name(d) for d in extras["voice"]}
    assert "pyaudio" in voice
    assert "scipy" in voice
    assert not voice & {"torch", "torchaudio", "silero-vad", "onnxruntime"}

    # vad carries silero plus the exact torch 2.5.1 the local stack uses, so
    # L3 → L4 is a same-version wheel-source swap, not a version jump.
    vad = {_dist_name(d) for d in extras["vad"]}
    assert vad == {"silero-vad", "torch"}
    assert any("torch==2.5.1" in d for d in extras["vad"])
    assert any("silero-vad==6.2.1" in d for d in extras["vad"])

    local = {_dist_name(d) for d in extras["local-cu124"]}
    assert "torch" in local and "onnxruntime" in local
    assert any("torch==2.5.1" in d for d in extras["local-cu124"])


def test_uv_sources_route_cu124_torch_only_for_windows_local_extra() -> None:
    uv = _uv_config()
    sources = uv["sources"]
    assert "torch" in sources and "torchaudio" in sources
    for name in ("torch", "torchaudio"):
        entries = sources[name]
        assert any(
            e.get("index") == "pytorch-cu124"
            and e.get("extra") == "local-cu124"
            and "win32" in e.get("marker", "")
            for e in entries
        ), f"{name} must route to pytorch-cu124 only for win32 + local-cu124"

    indexes = {i["name"] for i in uv["index"]}
    assert "pytorch-cu124" in indexes

    # uv.lock must carry the cu124 build entry, sourced from the PyTorch index,
    # for the win32 + local-cu124 marker only.
    reqs = _lock_root_requires_dist()
    cu124 = [
        r for r in reqs
        if r["name"] == "torch" and "extra == 'local-cu124'" in r["marker"]
        and "== 'win32'" in r["marker"]
    ]
    assert cu124, "uv.lock has no win32 + local-cu124 torch entry"
    assert "download.pytorch.org" in cu124[0]["index"], (
        "win32 local-cu124 torch must resolve from the pytorch-cu124 index"
    )
    # A non-win32 local-cu124 selection still exists but has no cu124 index.
    nonwin = [
        r for r in reqs
        if r["name"] == "torch" and "extra == 'local-cu124'" in r["marker"]
        and "== 'win32'" not in r["marker"]
    ]
    assert nonwin and not nonwin[0]["index"]


def test_lock_default_dependencies_serve_l1_only() -> None:
    """The no-extra (L1) resolution must stay free of every L2+ package."""
    default_names = {d["name"] for d in _lock_metadata_list("dependencies", _lock_root_block())}
    for banned in _BANNED_IN_L1:
        assert banned not in default_names, (
            f"{banned} must not be a default (L1) dependency; it belongs to L2+ tiers"
        )
    assert "aiohttp" in default_names


def test_vad_extra_in_lock_is_separate_tier() -> None:
    """uv.lock marks the vad-tier torch as its own extra selection."""
    reqs = _lock_root_requires_dist()
    vad_entries = [r for r in reqs if r["name"] == "torch" and "extra == 'vad'" in r["marker"]]
    assert vad_entries and vad_entries[0]["spec"] == "==2.5.1"
    assert any(r["name"] == "silero-vad" and "extra == 'vad'" in r["marker"] for r in reqs)


def test_verify_profiles_match_tier_imports() -> None:
    from tools import verify_python_environment as vpe

    # Tier import sets must stay aligned with the extras they verify.
    voice = {_dist_name(d) for d in _pyproject_extras()["voice"]}
    for module in vpe.VOICE_IMPORTS:
        assert module.replace("_", "-") in voice, f"VOICE_IMPORTS module {module} is not in the voice extra"
    vad = {_dist_name(d) for d in _pyproject_extras()["vad"]}
    for module in vpe.VAD_IMPORTS:
        assert module.replace("_", "-") in vad, f"VAD_IMPORTS module {module} is not in the vad extra"
    base = {_dist_name(d) for d in _pyproject()["project"]["dependencies"]}
    # Import name → distribution name aliases for modules whose PyPI dist
    # differs from the import path.
    aliases = {"pil": "pillow", "google.genai": "google-genai"}
    for module in vpe.BASE_IMPORTS:
        expected = aliases.get(module.lower(), module.replace("_", "-").lower())
        assert expected in base, f"BASE_IMPORTS module {module} is not a base dependency"


def test_verify_profile_ladder_is_a_strict_prefix_chain() -> None:
    from tools import verify_python_environment as vpe

    # L1 ⊂ L2 ⊂ L3 ⊂ L4: each release profile verifies a strict superset of
    # the tier below it, mirroring the install extras base → voice → vad → local-cu124.
    ladder = vpe.PROFILE_TIER_IMPORTS
    assert tuple(ladder) == ("cpu", "ci", "voice", "vad", "cu124")
    chain = [set(ladder[name]) for name in ("cpu", "voice", "vad", "cu124")]
    for lower, upper in zip(chain, chain[1:]):
        assert lower < upper
    assert set(vpe.VAD_IMPORTS) <= set(ladder["vad"]) - set(ladder["voice"])
    assert set(vpe.LOCAL_MODEL_IMPORTS) <= set(ladder["cu124"]) - set(ladder["vad"])


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for the lock consistency smoke")
def test_uv_lock_is_consistent_with_pyproject() -> None:
    """uv lock --check must pass: pyproject edits that drift the lock fail CI."""
    result = subprocess.run(
        [shutil.which("uv"), "lock", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"uv.lock is out of date with pyproject.toml:\n{result.stderr[-1000:]}"
    )
