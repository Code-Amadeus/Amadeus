"""Headless inference imports and the exact-source optional demo packaging boundary."""
from __future__ import annotations
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import types
import zipfile
import configparser
from copy import deepcopy
from email.parser import BytesParser
import os
import shutil
import tarfile

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


def test_qwen_sources_and_only_declared_packaging_changes_match_upstream():
    source = ROOT / 'vendor/qwen-asr'
    expected = json.loads((source / 'upstream-files.json').read_bytes())
    for name, digest in expected.items():
        path = ROOT / 'vendor/qwen-asr.upstream-pyproject.toml' if name == 'pyproject.toml' else source / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    original = tomllib.loads((ROOT / 'vendor/qwen-asr.upstream-pyproject.toml').read_text(encoding='utf-8'))
    patched = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
    allowed = deepcopy(original)
    allowed['build-system']['requires'] = ['setuptools==84.2.1', 'wheel==0.47.0']
    allowed['project']['dependencies'].remove('gradio')
    allowed['project']['optional-dependencies']['demo'] = ['gradio>=6.16.0,<7']
    allowed['project']['license'] = 'Apache-2.0'
    allowed['project']['license-files'] = ['LICENSE']
    assert patched == allowed


def test_isolated_qwen_distributions_preserve_code_resources_and_metadata(tmp_path):
    source = ROOT / 'vendor/qwen-asr'
    expected = json.loads((source / 'upstream-files.json').read_bytes())
    project = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    work = tmp_path / 'source'
    shutil.copytree(source, work, ignore=shutil.ignore_patterns('build', '*.egg-info', '__pycache__'))
    output = tmp_path / 'distributions'
    uv = shutil.which('uv')
    assert uv, 'The supported development/CI environment requires the pinned uv frontend'
    # uv's default builds an sdist, then builds the wheel from that sdist in an
    # isolated PEP 517 environment. Do not import a development-env backend.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('UV_') and key not in {'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'}}
    result = subprocess.run([uv, 'build', '--no-config', '--no-sources',
                             '--python', sys.executable, '--out-dir', str(output)],
                            cwd=work, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    implementations = {name: digest for name, digest in expected.items() if name.startswith('qwen_asr/')}
    with tarfile.open(next(output.glob('*.tar.gz'))) as sdist:
        prefix = 'qwen_asr-0.0.6/'
        shipped = {member.name.removeprefix(prefix) for member in sdist.getmembers()
                   if member.isfile() and member.name.startswith(prefix + 'qwen_asr/')}
        assert shipped == set(implementations)
        for name, digest in implementations.items():
            assert hashlib.sha256(sdist.extractfile(prefix + name).read()).hexdigest() == digest, name
        for name in ('LICENSE', 'pyproject.toml'):
            assert sdist.extractfile(prefix + name).read() == (source / name).read_bytes()
    with zipfile.ZipFile(next(output.glob('*.whl'))) as wheel:
        assert {name for name in wheel.namelist() if name.startswith('qwen_asr/')} == set(implementations)
        for name, digest in implementations.items():
            assert hashlib.sha256(wheel.read(name)).hexdigest() == digest, name
        info = 'qwen_asr-0.0.6.dist-info/'
        metadata = BytesParser().parsebytes(wheel.read(info + 'METADATA'))
        wheel_metadata = BytesParser().parsebytes(wheel.read(info + 'WHEEL'))
        assert wheel_metadata['Generator'] == 'setuptools (84.2.1)'
        assert metadata['Name'] == project['name'] and metadata['Version'] == project['version']
        assert metadata['Requires-Python'] == project['requires-python']
        assert metadata['License-Expression'] == 'Apache-2.0'
        assert metadata['License'] is None
        assert metadata.get_all('License-File') == ['LICENSE']
        assert wheel.read(info + 'licenses/LICENSE') == (source / 'LICENSE').read_bytes()
        entry_points = configparser.ConfigParser(interpolation=None)
        entry_points.read_string(wheel.read(info + 'entry_points.txt').decode())
        assert dict(entry_points['console_scripts']) == project['scripts']
    requirements = {str(Requirement(item)) for item in project['dependencies']}
    for extra, values in project['optional-dependencies'].items():
        requirements.update(str(Requirement(item + f'; extra == "{extra}"')) for item in values)
    assert {str(Requirement(item)) for item in metadata.get_all('Requires-Dist')} == requirements
    assert set(metadata.get_all('Provides-Extra')) == set(project['optional-dependencies'])
