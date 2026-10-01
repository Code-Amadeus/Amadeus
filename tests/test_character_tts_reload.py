from __future__ import annotations

from types import ModuleType

from tts.backend import TTSRuntimeAdapter
from tts.backends.gpt_sovits import GPTSoVITSBackend


def test_active_model_paths_prefer_character_then_global(monkeypatch):
    import tts.model_paths as model_paths

    import core.character_profile as profiles
    from config import settings

    monkeypatch.setattr(
        profiles,
        "active_voice",
        lambda: {"gpt_model": "", "sovits_model": "assets/model.pth"},
    )
    monkeypatch.setattr(settings, "TTS_GPT_MODEL_PATH", "assets/base.ckpt")
    monkeypatch.setattr(settings, "TTS_SOVITS_MODEL_PATH", "assets/base.pth")

    gpt, sovits = model_paths.resolve_active_tts_model_paths()
    assert gpt.endswith("assets\\base.ckpt") or gpt.endswith("assets/base.ckpt")
    assert sovits.endswith("assets\\model.pth") or sovits.endswith("assets/model.pth")


def test_embedded_reload_swaps_only_sovits_when_gpt_is_unchanged(monkeypatch):
    import sys

    from config import settings

    created = []

    class FakeInferencer:
        def __init__(self, **kwargs):
            self.gpt_path = kwargs["gpt_path"]
            self.sovits_path = kwargs["sovits_path"]
            self.reload_calls = []
            created.append(self)

        def reload_sovits(self, path):
            self.reload_calls.append(path)
            self.sovits_path = path
            return True

        def infer(self, **kwargs):
            return 24000, __import__("numpy").zeros(1, dtype="float32")

    fake_module = ModuleType("local_tts_infer")
    fake_module.TTSInferencer = FakeInferencer
    monkeypatch.setitem(sys.modules, "local_tts_infer", fake_module)
    monkeypatch.setattr(GPTSoVITSBackend, "_sidecar_enabled", staticmethod(lambda: False))
    monkeypatch.setattr(settings, "TTS_DEVICE", "cpu")

    current = {"gpt": "base.ckpt", "sovits": "kurisu.pth"}
    monkeypatch.setattr(
        "tts.model_paths.resolve_active_tts_model_paths",
        lambda: (current["gpt"], current["sovits"]),
    )

    backend = GPTSoVITSBackend()
    backend.load()
    assert created[0].sovits_path == "kurisu.pth"

    current["sovits"] = "yachiyo_e8.pth"
    assert backend.reload_active_voice() is True
    assert created[0].reload_calls == ["yachiyo_e8.pth"]
    assert len(created) == 1
    assert TTSRuntimeAdapter(backend).reload_active_voice() is False

    backend.close()
