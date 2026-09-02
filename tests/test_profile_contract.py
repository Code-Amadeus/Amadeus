"""Four-tier install contract: extras, profiles, locks, and resolver behavior.

Freezes the L1–L4 install contract so future edits cannot silently drift:
which packages live in which extra, which modules each verify profile
requires, and which tier each lock file serves.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_extras() -> dict[str, list[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def _dist_name(requirement: str) -> str:
    return re.split(r"[=<>!\[]", requirement.strip(), maxsplit=1)[0].strip().lower()


def test_voice_extra_is_torch_free_and_vad_extra_pulls_silero() -> None:
    extras = _pyproject_extras()
    voice = {_dist_name(d) for d in extras["voice"]}
    assert "pyaudio" in voice
    assert "scipy" in voice
    assert not voice & {"torch", "torchaudio", "silero-vad", "onnxruntime"}
    assert {_dist_name(d) for d in extras["vad"]} == {"silero-vad"}
    local = {_dist_name(d) for d in extras["local-cu124"]}
    assert "torch" in local and "onnxruntime" in local


def test_cu124_profile_requires_voice_vad_and_local_extras() -> None:
    text = (ROOT / "requirements-cu124.txt").read_text(encoding="utf-8")
    assert ".[voice,vad,local-cu124]" in text


def test_core_locks_serve_l1_only() -> None:
    for lock in ("windows-py312-cpu.txt", "windows-py312-ci.txt"):
        text = (ROOT / "requirements" / "locks" / lock).read_text(encoding="utf-8")
        for banned in ("torch", "pyaudio", "silero-vad", "onnxruntime"):
            assert not re.search(rf"^{banned}==", text, flags=re.IGNORECASE | re.MULTILINE), (
                f"{lock} must not contain {banned} (it belongs to L2+ tiers)"
            )


def test_verify_profiles_match_tier_imports() -> None:
    from tools import verify_python_environment as vpe

    # Tier import sets must stay aligned with the extras they verify.
    voice = {_dist_name(d) for d in _pyproject_extras()["voice"]}
    for module in vpe.VOICE_IMPORTS:
        assert module.replace("_", "-") in voice, f"VOICE_IMPORTS module {module} is not in the voice extra"
    vad = {_dist_name(d) for d in _pyproject_extras()["vad"]}
    for module in vpe.VAD_IMPORTS:
        assert module.replace("_", "-") in vad, f"VAD_IMPORTS module {module} is not in the vad extra"
    base = {_dist_name(d) for d in tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]}
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


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for the resolver smoke")
def test_resolver_smoke_torch_enters_only_at_vad_tier() -> None:
    """L1/L2 resolution must stay torch-free; the vad tier is where torch enters."""

    def _resolve(extra_args: tuple[str, ...], out: Path) -> str:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                shutil.which("uv"),
                "pip",
                "compile",
                "pyproject.toml",
                "--python-platform",
                "windows",
                "--python-version",
                "3.12",
                *extra_args,
                f"--output-file={out.relative_to(ROOT)}",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.read_text(encoding="utf-8")

    l2 = _resolve(("--extra", "voice"), ROOT / "build" / "contract-l2.txt")
    assert not re.search(r"^torch==", l2, flags=re.MULTILINE)
    assert re.search(r"^pyaudio==", l2, flags=re.MULTILINE)

    l3 = _resolve(("--extra", "voice", "--extra", "vad"), ROOT / "build" / "contract-l3.txt")
    assert re.search(r"^torch==", l3, flags=re.MULTILINE)
    assert re.search(r"^silero-vad==", l3, flags=re.MULTILINE)
