"""Exercise installed voice/VAD paths with local-model decoders unavailable."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
_PROBE = """
import importlib
import sys

profile = sys.argv[1]
blocked = {'av', 'ffmpeg'}
if profile == 'voice':
    blocked.update({'torch', 'torchaudio', 'silero_vad', 'onnxruntime'})

class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in blocked:
            raise ModuleNotFoundError('outside the selected voice tier', name=name)

sys.meta_path.insert(0, Blocker())
for name in list(sys.modules):
    if name.split('.')[0] in blocked:
        del sys.modules[name]
for name in blocked:
    try:
        importlib.import_module(name)
    except ModuleNotFoundError as exc:
        assert exc.name == name
    else:
        raise AssertionError(f'{name} was not blocked')

from tools import verify_python_environment as verifier
for name in (*verifier.BASE_IMPORTS, *verifier.PROJECT_IMPORTS,
             *verifier.PROFILE_TIER_IMPORTS[profile]):
    importlib.import_module(name)

import numpy as np
from asr.backends.openai_compatible import _wav_payload
from tts.backends.openai_compatible import _decode_pcm16le, _decode_wav

samples = np.array([0.0, 0.25, -0.25, 0.75, -0.75], dtype=np.float32)
rate, decoded = _decode_wav(_wav_payload(samples, 24000))
assert rate == 24000 and decoded.dtype == np.float32
np.testing.assert_allclose(decoded, samples, atol=2 / 32768)
pcm = (samples * 32767).astype('<i2').tobytes()
np.testing.assert_array_equal(_decode_pcm16le(pcm), decoded)

from asr.barge_in_detector import BargeInDetector

async def noop():
    pass

detector = BargeInDetector(tts_playing_fn=lambda: False, on_barge_in=noop)
expected = ('fallback', '') if profile == 'voice' else ('ready', '')
assert detector._probe_vad_capability() == expected
if profile == 'vad':
    import torch
    from silero_vad import VADIterator
    silence = torch.zeros(512)
    with torch.inference_mode():
        probability = float(detector._vad_model(silence, 16000))
        assert 0 <= probability < 0.5
        iterator = VADIterator(detector._vad_model, sampling_rate=16000)
        assert iterator(silence) is None
assert not detector.running
assert not {'av', 'ffmpeg'} & sys.modules.keys()
print('VOICE_TIER_OK')
"""


@pytest.mark.parametrize("profile", ["voice", "vad"])
def test_voice_tier_works_without_local_audio_decoders(profile: str) -> None:
    required = ["pyaudio", "aec_audio_processing", "scipy", "soundfile"]
    if profile == "vad":
        required.extend(("torch", "torchaudio", "silero_vad"))
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip(f"requires the {profile} tier: {', '.join(missing)}")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, profile], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VOICE_TIER_OK" in result.stdout
