"""Decode synthetic local audio with the installed T4 codec dependencies."""

from __future__ import annotations

import shutil
import wave

import numpy as np
import pytest


@pytest.fixture(params=[1, 2], ids=['mono', 'stereo'])
def local_wav(tmp_path, request):
    sample_rate = 24000
    time = np.arange(sample_rate // 4) / sample_rate
    mono = 0.25 * np.sin(2 * np.pi * 440 * time)
    channels = (mono,) if request.param == 1 else (mono, -mono)
    pcm = (np.column_stack(channels) * 32767).astype('<i2')
    path = tmp_path / 'reference.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(request.param)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path, pcm


def test_local_reference_audio_decodes_and_resamples_with_ffmpeg(local_wav):
    pytest.importorskip('ffmpeg', reason='requires local-models')
    if not shutil.which('ffmpeg'):
        pytest.skip('local reference decoding requires the FFmpeg executable')
    from tools.my_utils import load_audio

    path, pcm = local_wav
    decoded = load_audio(str(path), 16000)
    assert decoded.dtype == np.float32
    assert decoded.shape == (4000,)
    expected = 0.25 * np.sin(2 * np.pi * 440 * np.arange(4000) / 16000)
    if pcm.shape[1] == 2:
        # Opposite stereo channels cancel regardless of FFmpeg's mix gain.
        expected = np.zeros(4000)
    # Ignore the resampler's endpoint transients, retaining waveform fidelity.
    np.testing.assert_allclose(decoded[32:-32], expected[32:-32], atol=0.001)


def test_pyav_still_decodes_local_audio(local_wav):
    av = pytest.importorskip('av', reason='requires the local Qwen dependency chain')
    path, pcm = local_wav
    with av.open(str(path)) as container:
        frames = list(container.decode(audio=0))
    assert frames and all(frame.sample_rate == 24000 for frame in frames)
    decoded = np.concatenate([frame.to_ndarray().reshape(-1) for frame in frames])
    np.testing.assert_array_equal(decoded, pcm.reshape(-1))


def test_qwen_normalizes_local_audio(local_wav):
    pytest.importorskip('qwen_asr', reason='requires local-models')
    from qwen_asr.inference.utils import normalize_audio_input

    path, pcm = local_wav
    decoded = normalize_audio_input(str(path))
    assert decoded.dtype == np.float32
    assert decoded.shape == (4000,)
    expected = 0.25 * np.sin(2 * np.pi * 440 * np.arange(4000) / 16000)
    if pcm.shape[1] == 2:
        expected = np.zeros(4000)
    np.testing.assert_allclose(decoded[32:-32], expected[32:-32], atol=0.001)
