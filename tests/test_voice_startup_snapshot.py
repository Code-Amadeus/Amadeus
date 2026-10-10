"""Parsed startup values stay authoritative at voice consumer boundaries."""
import asyncio
import io
from unittest.mock import Mock


def test_microphone_selection_uses_parsed_settings_after_environment_changes(monkeypatch):
    from config import settings
    from asr import microphone

    for key, value in {
        'MICROPHONE_DEVICE_INDEX': 7, 'MICROPHONE_PREFERRED_NAME': '  parsed mic  ',
        'MICROPHONE_FALLBACK_DEVICE_INDEX': -1, 'MICROPHONE_FALLBACK_NAME': 'backup',
    }.items():
        monkeypatch.setattr(settings, key, value)
        monkeypatch.setenv(key, 'changed-after-startup')
    assert microphone.configured_device_index() == 7
    assert microphone.configured_preferred_name() == 'parsed mic'
    assert microphone.configured_fallback_device_index() is None
    assert microphone.configured_fallback_name() == 'backup'


def test_pipeline_configuration_uses_parsed_concurrency(monkeypatch):
    from tts import pipeline

    monkeypatch.setattr(pipeline, '_exp_tts_semaphore', pipeline._exp_tts_semaphore)
    monkeypatch.setattr(pipeline, '_exp_tts_concurrency', pipeline._exp_tts_concurrency)
    monkeypatch.setattr(pipeline, 'EXP_TTS_MAX_CONCURRENCY', 2)
    monkeypatch.setenv('EXP_TTS_MAX_CONCURRENCY', '1')
    pipeline.configure(exp_tts_semaphore=asyncio.Semaphore(2))
    assert pipeline._exp_tts_concurrency == 2


def test_qwen_child_receives_parent_device_and_parsed_cuda_requirement(monkeypatch):
    from asr.backends import qwen3_asr as module

    backend = module.Qwen3ASRBackend()
    monkeypatch.setattr(backend, '_can_use_inprocess', lambda: False)
    monkeypatch.setattr(backend, '_select_python', lambda: __file__)
    monkeypatch.setenv('QWEN3_ASR_MODE', 'sidecar')
    monkeypatch.setenv('QWEN3_ASR_DEVICE', 'cuda')
    monkeypatch.setenv('QWEN3_ASR_REQUIRE_CUDA', 'true')
    monkeypatch.setattr(module, 'QWEN3_ASR_REQUIRE_CUDA', False)
    monkeypatch.setattr(module.Qwen3ASRBackend, '_shared_proc', None)
    process = Mock(stdout=io.BytesIO(b'{"type":"ready"}\n'), poll=lambda: None)
    launch = Mock(return_value=process)
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    backend.load('cpu')
    environment = launch.call_args.kwargs['env']
    assert environment['QWEN3_ASR_DEVICE'] == 'cpu'
    assert environment['QWEN3_ASR_REQUIRE_CUDA'] == 'false'
    # A later restart retains the same device instead of hard-coding CUDA.
    backend._proc = None
    reload = Mock(side_effect=RuntimeError('stop after checking requested device'))
    monkeypatch.setattr(backend, 'load', reload)
    import pytest
    import numpy as np
    with pytest.raises(RuntimeError, match='stop after checking'):
        backend.transcribe(np.zeros(1600, dtype=np.float32))
    reload.assert_called_once_with('cpu')
