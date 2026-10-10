"""Parsed startup values stay authoritative at voice consumer boundaries."""
import asyncio
import io
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest


def test_microphone_selection_uses_parsed_settings_after_environment_changes(monkeypatch):
    from config import settings
    # These pure configuration accessors must also run in the core CI tier.
    # Load a private module so its audio stub cannot leak to later voice tests.
    import importlib.util
    from pathlib import Path
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, 'pyaudio', ModuleType('pyaudio'))
        spec = importlib.util.spec_from_file_location(
            '_microphone_settings_probe', Path(__file__).parents[1] / 'asr/microphone.py',
        )
        microphone = importlib.util.module_from_spec(spec)
        imports.setitem(sys.modules, spec.name, microphone)
        spec.loader.exec_module(microphone)

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


def test_debug_capture_falls_back_to_parsed_microphone_unless_debug_override_exists(monkeypatch):
    from config import settings
    from tts.aec_debug_capture import _mic_index

    monkeypatch.setattr(settings, 'MICROPHONE_DEVICE_INDEX', 7)
    monkeypatch.setenv('MICROPHONE_DEVICE_INDEX', 'changed-after-startup')
    monkeypatch.delenv('AEC_DEBUG_MIC_INDEX', raising=False)
    assert _mic_index() == 7
    monkeypatch.setenv('AEC_DEBUG_MIC_INDEX', '3')
    assert _mic_index() == 3
    monkeypatch.setenv('AEC_DEBUG_MIC_INDEX', 'invalid')
    assert _mic_index() is None


@pytest.mark.parametrize('raw,device,expected', [('false', 'cpu', False), ('on', 'cuda', True)])
def test_qwen_child_receives_parent_device_and_parsed_cuda_requirement(monkeypatch, raw, device, expected):
    from asr.backends import qwen3_asr as module
    from config.catalog import read_catalog_value
    from config.environment import EnvironmentReader

    backend = module.Qwen3ASRBackend()
    monkeypatch.setattr(backend, '_can_use_inprocess', lambda: False)
    monkeypatch.setattr(backend, '_select_python', lambda: __file__)
    monkeypatch.setenv('QWEN3_ASR_MODE', 'sidecar')
    monkeypatch.setenv('QWEN3_ASR_DEVICE', 'cuda')
    monkeypatch.setenv('QWEN3_ASR_REQUIRE_CUDA', 'true')
    parsed = read_catalog_value(EnvironmentReader({'QWEN3_ASR_REQUIRE_CUDA': raw}), 'QWEN3_ASR_REQUIRE_CUDA')
    assert parsed is expected
    monkeypatch.setattr(module, 'QWEN3_ASR_REQUIRE_CUDA', parsed)
    monkeypatch.setattr(module.Qwen3ASRBackend, '_shared_proc', None)
    process = Mock(stdout=io.BytesIO(b'{"type":"ready"}\n'), poll=lambda: None)
    launch = Mock(return_value=process)
    monkeypatch.setattr(module.subprocess, 'Popen', launch)
    backend.load(device)
    environment = launch.call_args.kwargs['env']
    assert environment['QWEN3_ASR_DEVICE'] == device
    assert environment['QWEN3_ASR_REQUIRE_CUDA'] == str(expected).lower()
    # A later restart retains the same device instead of hard-coding CUDA.
    backend._proc = None
    reload = Mock(side_effect=RuntimeError('stop after checking requested device'))
    monkeypatch.setattr(backend, 'load', reload)
    import numpy as np
    with pytest.raises(RuntimeError, match='stop after checking'):
        backend.transcribe(np.zeros(1600, dtype=np.float32))
    reload.assert_called_once_with(device)


@pytest.mark.parametrize('require_cuda', [True, False])
def test_sidecar_enforces_cuda_requirement_and_preserves_optional_cpu_fallback(monkeypatch, require_cuda):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    torch = ModuleType('torch')
    torch.cuda = SimpleNamespace(is_available=lambda: False)
    torch.bfloat16, torch.float32 = 'bf16', 'fp32'
    torch.version = SimpleNamespace(cuda=None, hip=None)
    model = Mock()
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'qwen_asr', SimpleNamespace(Qwen3ASRModel=model))
    monkeypatch.setenv('QWEN3_ASR_DEVICE', 'cuda')
    monkeypatch.setenv('QWEN3_ASR_REQUIRE_CUDA', str(require_cuda).lower())
    from asr import qwen_model
    monkeypatch.setattr(qwen_model, 'resolve_qwen_model_source', lambda: 'synthetic-model')
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    sidecar = runpy.run_path(str(Path(__file__).parents[1] / 'asr/qwen3_asr_sidecar.py'))
    emitted = []
    monkeypatch.setitem(sidecar['main'].__globals__, '_emit', emitted.append)
    if require_cuda:
        with pytest.raises(SystemExit) as failure:
            sidecar['main']()
        assert failure.value.code == 1
        assert emitted[0]['type'] == 'error'
        assert 'requested CUDA' in emitted[0]['msg']
        model.from_pretrained.assert_not_called()
    else:
        sidecar['main']()
        assert emitted[0]['type'] == 'ready'
        assert emitted[0]['device'] == 'cpu'
        assert model.from_pretrained.call_args.kwargs['device_map'] == 'cpu'


def test_aec_device_calibration_and_explicit_delay_without_audio_dependencies(monkeypatch):
    from tts import aec_realtime as aec

    monkeypatch.delenv('AEC_REALTIME_DELAY_MS', raising=False)
    for device, expected in [('bluetooth', aec.AEC_DELAY_MS_BLUETOOTH),
                             ('internal', aec.AEC_DELAY_MS_INTERNAL),
                             ('usb', aec.AEC_DELAY_MS_USB), ('unknown', aec.AEC_REALTIME_DELAY_MS)]:
        assert aec.select_aec_delay_ms(device)[0] == expected
    monkeypatch.setenv('AEC_REALTIME_DELAY_MS', '80')
    monkeypatch.setattr(aec, 'AEC_REALTIME_DELAY_MS', 80.0)
    for device in ['bluetooth', 'internal', 'usb', 'unknown']:
        assert aec.select_aec_delay_ms(device) == (80.0, 'explicit AEC_REALTIME_DELAY_MS')
