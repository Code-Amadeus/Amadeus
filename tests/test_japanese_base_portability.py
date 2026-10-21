"""Run on Windows, Linux and macOS with the pinned native frontend installed."""
import json
from pathlib import Path
import platform

import pytest


def test_native_dictionary_bytes_match_the_resource_ownership_contract():
    pytest.importorskip("pyopenjtalk")
    from tools.audit_japanese_dictionary import native_base_hashes
    recipe = json.loads((Path(__file__).resolve().parents[1] /
                         "tools/data/japanese_dictionary_source.json").read_text(encoding="utf-8"))
    actual = native_base_hashes()
    expected = recipe["extension_filter"]["base_dictionary_sha256"]
    assert actual == expected, {"platform": platform.system(), "machine": platform.machine(),
                                "expected": expected, "actual": actual}
