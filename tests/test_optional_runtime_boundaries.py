from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tts import optional_ap_bwe


ROOT = Path(__file__).resolve().parents[1]


def test_ap_bwe_boundary_does_not_import_implementation_eagerly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import tts.optional_ap_bwe; "
            "print('tools.audio_sr' in sys.modules)",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


def test_ap_bwe_boundary_loads_only_after_explicit_request(monkeypatch) -> None:
    calls: list[str] = []

    class FakeAPBWE:
        def __init__(self, device, config_adapter) -> None:
            self.device = device
            self.config_adapter = config_adapter

    def fake_import(name: str):
        calls.append(name)
        return SimpleNamespace(AP_BWE=FakeAPBWE)

    monkeypatch.setattr(optional_ap_bwe.importlib, "import_module", fake_import)
    marker = object()
    instance = optional_ap_bwe.create_ap_bwe("cpu", marker)

    assert calls == ["tools.audio_sr"]
    assert instance.device == "cpu"
    assert instance.config_adapter is marker


def test_ap_bwe_boundary_reports_missing_optional_component(monkeypatch) -> None:
    def missing(_name: str):
        raise ModuleNotFoundError("not installed")

    monkeypatch.setattr(optional_ap_bwe.importlib, "import_module", missing)
    with pytest.raises(optional_ap_bwe.APBWEUnavailable, match="optional"):
        optional_ap_bwe.create_ap_bwe("cpu", object())


@pytest.mark.parametrize(
    "relative",
    [
        "render/web/index.html",
        "render/web/wallpaper.html",
        "render/web/wallpaper_engine.html",
    ],
)
def test_live2d_vendor_is_local_and_explicit_opt_in(relative: str) -> None:
    source = (ROOT / relative).read_text(encoding="utf-8")
    before_opt_in = source.split("if (live2dCompatEnabled)", 1)[0]

    assert "live2dcubismcore.min.js" not in before_opt_in
    assert "pixi-live2d-display.cubism4.min.js" not in before_opt_in
    assert "live2dcubismcore.min.js" in source
    assert "pixi-live2d-display.cubism4.min.js" in source
    assert "cubism.live2d.com" not in source
    assert "cdn.jsdelivr.net/npm/pixi-live2d-display" not in source
