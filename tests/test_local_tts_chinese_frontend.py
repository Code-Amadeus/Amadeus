"""Chinese frontend compatibility with pure-Python jieba and no jieba-fast.

Snapshots were captured from public main bf0d0fa using jieba-fast 0.53.
The G2PW external converter is fixed in the probe; its neural model and audio
quality remain separate acceptance gates. All other frontend code is real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from packaging.markers import default_environment
from packaging.requirements import Requirement
import pytest

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")
EXPECTED = json.loads((ROOT / "tests/fixtures/chinese_frontend.json").read_text(encoding="utf-8"))
VERSIONS = ("v1", "v2", "v2Pro", "v2ProPlus", "v3")


@pytest.fixture(scope="module")
def frontends_without_legacy_tokenizer():
    for module in ("jieba", "cn2an", "pypinyin"):
        pytest.importorskip(module, reason="Chinese frontend dependencies belong to optional T4")
    env = {**os.environ, "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "tests/_chinese_frontend_probe.py")],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_segmentation_pos_and_search_cut_without_jieba_fast(frontends_without_legacy_tokenizer):
    assert frontends_without_legacy_tokenizer["tokenization"] == EXPECTED["tokenization"]


def test_tone_sandhi_without_jieba_fast(frontends_without_legacy_tokenizer):
    assert frontends_without_legacy_tokenizer["tones"] == EXPECTED["tones"]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("case", range(len(EXPECTED["frontends"]["v1"])))
def test_chinese_phones_alignment_and_symbols_without_jieba_fast(
    frontends_without_legacy_tokenizer, version, case,
):
    family = "v1" if version == "v1" else "v2"
    assert frontends_without_legacy_tokenizer["frontends"][version][case] == EXPECTED["frontends"][family][case]


@pytest.mark.parametrize("version", VERSIONS)
def test_unknown_three_character_input_applies_existing_sandhi(
    frontends_without_legacy_tokenizer, version,
):
    actual = frontends_without_legacy_tokenizer["unknown_word"][version]
    assert actual["phones"] == ["l", "iao3", "w", "u2", "c", "an3", "."]
    assert actual["word2ph"] == [2, 2, 2, 1]
    assert actual["normalized"] == "憭怃憯."


@pytest.mark.skipif(UV is None, reason="uv is required to select lock branches")
@pytest.mark.parametrize("extra,platforms", [
    ("local-cpu", ("win32", "linux", "darwin")),
    ("local-cu124", ("win32",)),
    ("local-cu128", ("win32", "linux")),
    ("local-mps", ("darwin",)),
    ("local-rocm", ("win32",)),
])
def test_t4_exports_keep_jieba_and_omit_jieba_fast(extra, platforms):
    result = subprocess.run(
        [UV, "export", "--locked", "--offline", "--extra", extra,
         "--no-hashes", "--no-emit-project"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    requirements = [Requirement(line) for line in result.stdout.splitlines()
                    if line.startswith("jieba")]
    assert all(item.name != "jieba-fast" for item in requirements)
    for platform in platforms:
        system, os_name, machine = {
            "win32": ("Windows", "nt", "AMD64"),
            "linux": ("Linux", "posix", "x86_64"),
            "darwin": ("Darwin", "posix", "arm64"),
        }[platform]
        environment = {**default_environment(), "sys_platform": platform,
                       "platform_system": system, "os_name": os_name,
                       "platform_machine": machine}
        selected = [item for item in requirements if item.marker is None or item.marker.evaluate(environment)]
        assert len(selected) == 1 and str(selected[0].specifier) == "==0.42.1", platform
