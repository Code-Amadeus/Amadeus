"""A clean source archive can prepare and install the first-party SDK offline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from tools.build_extension_sdk import build_sdk
from tools.build_source_release import create_archive, select_paths

ROOT = Path(__file__).resolve().parents[1]
WHEEL = 'amadeus_extension_sdk-0.1.0-py3-none-any.whl'


def test_clean_archive_desktop_build_prepares_installable_sdk(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Desktop build requires Node.js')
    paths = ['tools/build_extension_sdk.py', 'electron/scripts/build-extension-sdk.mjs',
        'electron/src/main/pythonRuntime.ts', 'sdk/extension-python/pyproject.toml',
        'sdk/extension-python/src/amadeus_extension/__init__.py',
        'sdk/extension-python/src/amadeus_extension/runner.py']
    policy = json.loads((ROOT / 'release/source_release_policy.json').read_text(encoding='utf-8'))
    selected, excluded = select_paths(paths, policy)
    assert not excluded and set(paths) <= set(policy['required_files'])
    archive = tmp_path / 'source.zip'
    create_archive(root=ROOT, output=archive, report={'release_ready': True, 'version': 'test',
        'source_date_epoch': 1700000000, 'selected_files': [{'path': path} for path in selected]},
        policy=policy, modes={})
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path / 'extracted')
    clean = tmp_path / 'extracted/amadeus-test'
    assert not (clean / 'build').exists() and not (clean / '.git').exists()
    subprocess.run([node, '--experimental-strip-types', 'electron/scripts/build-extension-sdk.mjs'],
        cwd=clean, env={**os.environ, 'AMADEUS_PYTHON': sys.executable}, check=True, capture_output=True)
    wheel = clean / 'build/extension-sdk' / WHEEL
    assert wheel.is_file()
    environment = tmp_path / 'consumer'
    subprocess.run([sys.executable, '-I', '-m', 'venv', str(environment)], check=True, capture_output=True)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)],
        check=True, capture_output=True)
    subprocess.run([str(python), '-I', '-c',
        'from amadeus_extension import Extension; import amadeus_extension.runner'],
        cwd=tmp_path, check=True, capture_output=True)
    # Keep normal builds and installed desktop resources connected to this path.
    desktop = json.loads((ROOT / 'electron/package.json').read_text(encoding='utf-8'))
    assert 'build:extension-sdk' in desktop['scripts']['build']
    assert 'build:extension-sdk' in desktop['scripts']['electron:dev']
    assert any(entry['to'] == 'extension-sdk' for entry in desktop['build']['extraResources'])


def test_failed_sdk_build_preserves_previous_wheel(tmp_path):
    source = tmp_path / 'invalid-source'
    (source / 'src').mkdir(parents=True)
    (source / 'pyproject.toml').write_text('not valid TOML')
    output = tmp_path / 'wheels'
    output.mkdir()
    wheel = output / WHEEL
    wheel.write_bytes(b'previous completed wheel')
    with pytest.raises(RuntimeError, match='SDK build failed'):
        build_sdk(source, output)
    assert wheel.read_bytes() == b'previous completed wheel'
