"""Headless inference imports and the exact-source optional demo packaging boundary."""
from __future__ import annotations
import builtins
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import tomllib
import types
import zipfile

import numpy as np
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def test_audio_decoder_does_not_import_legacy_ui_dependencies(tmp_path, monkeypatch):
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert name.split('.')[0] not in {'gradio', 'pandas'}, name
        return original_import(name, *args, **kwargs)

    calls = []
    decoded = np.array([0.25, -0.5], dtype=np.float32).tobytes()

    class Decoder:
        def output(self, *args, **kwargs):
            calls.append(('output', args, kwargs))
            return self

        def run(self, **kwargs):
            calls.append(('run', kwargs))
            return decoded, b''

    ffmpeg = types.ModuleType('ffmpeg')
    ffmpeg.input = lambda *args, **kwargs: Decoder()
    monkeypatch.setitem(sys.modules, 'ffmpeg', ffmpeg)
    monkeypatch.setattr(builtins, '__import__', checked_import)
    spec = importlib.util.spec_from_file_location('headless_audio_probe', ROOT / 'tools/my_utils.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    audio = tmp_path / 'reference.wav'
    audio.touch()
    result = module.load_audio(str(audio), 16000)
    np.testing.assert_array_equal(result, [0.25, -0.5])
    assert calls[0][2] == {'format': 'f32le', 'acodec': 'pcm_f32le', 'ac': 1, 'ar': 16000}
    assert calls[1][1]['cmd'] == ['ffmpeg', '-nostdin']


def test_qwen_inference_sources_match_upstream_and_demo_is_optional(tmp_path):
    source = ROOT / 'vendor/qwen-asr'
    expected = json.loads((source / 'upstream-files.json').read_text())
    for name, digest in expected.items():
        if name == 'pyproject.toml':
            continue  # Its distribution dependency boundary is asserted below.
        payload = (source / name).read_bytes()
        if name in {'qwen_asr/__init__.py', 'qwen_asr/core/transformers_backend/__init__.py',
                    'qwen_asr/core/vllm_backend/__init__.py'}:
            original, count = re.subn(rb'(?m)^(from [\w.]+ import ([A-Za-z_]\w*)) as \2$', rb'\1', payload)
            assert count == (1 if '/vllm_backend/' in name else 3)
            before, after = compile(original, name, 'exec'), compile(payload, name, 'exec')
            assert (before.co_code, before.co_names, before.co_consts) == (after.co_code, after.co_names, after.co_consts)
            payload = original
        assert hashlib.sha256(payload).hexdigest() == digest, name
    project = tomllib.loads((source / 'pyproject.toml').read_text())['project']
    assert 'gradio' not in {Requirement(item).name for item in project['dependencies']}
    assert {Requirement(item).name for item in project['optional-dependencies']['demo']} == {'gradio'}
    # Build in a disposable directory; inspecting distribution metadata imports no model code.
    import shutil
    work = tmp_path / 'source'
    shutil.copytree(source, work, ignore=shutil.ignore_patterns('build', '*.egg-info', '__pycache__'))
    output = tmp_path / 'wheels'
    output.mkdir()
    result = subprocess.run([sys.executable, '-I', '-c',
        'from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])', str(output)],
        cwd=work, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with zipfile.ZipFile(next(output.glob('*.whl'))) as wheel:
        from email.parser import BytesParser
        metadata = BytesParser().parsebytes(wheel.read('qwen_asr-0.0.6.dist-info/METADATA'))
    dependencies = [Requirement(item) for item in metadata.get_all('Requires-Dist')]
    gradio, = [item for item in dependencies if item.name == 'gradio']
    assert not gradio.marker.evaluate({'extra': ''})
    assert gradio.marker.evaluate({'extra': 'demo'})
