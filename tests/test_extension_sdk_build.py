"""A clean source archive carries the same offline SDK as the installed desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from server.extensions.sdk import sdk_spec, validate_sdk_requirements
from tools.build_source_release import create_archive, select_paths

ROOT = Path(__file__).resolve().parents[1]


def test_clean_archive_contains_installable_sdk_without_python_build(tmp_path):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Desktop build requires Node.js')
    sdk = sdk_spec()
    paths = ['server/extensions/protocol-source.json', 'electron/scripts/check-extension-sdk.mjs',
             'electron/src/main/extensionSdk.ts', f"vendor/extension-sdk/{sdk['filename']}"]
    policy = json.loads((ROOT / 'release/source_release_policy.json').read_text(encoding='utf-8'))
    selected, excluded = select_paths(paths, policy)
    assert not excluded and set(paths) <= set(policy['required_files'])
    archive = tmp_path / 'source.zip'
    create_archive(root=ROOT, output=archive, report={'release_ready': True, 'version': 'test',
        'source_date_epoch': 1700000000, 'selected_files': [{'path': name} for name in selected]},
        policy=policy, modes={})
    with zipfile.ZipFile(archive) as source:
        source.extractall(tmp_path / 'extracted')
    clean = tmp_path / 'extracted/amadeus-test'
    assert not (clean / 'build').exists() and not (clean / '.git').exists()
    environment = {**os.environ, 'AMADEUS_PYTHON': str(tmp_path / 'no-python'), 'PATH': ''}
    command = [node, '--experimental-strip-types', 'electron/scripts/check-extension-sdk.mjs']
    subprocess.run(command, cwd=clean, env=environment, check=True, capture_output=True)
    wheel = clean / 'vendor/extension-sdk' / sdk['filename']
    consumer = tmp_path / 'consumer'
    subprocess.run([sys.executable, '-I', '-m', 'venv', str(consumer)], check=True, capture_output=True)
    python = consumer / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)],
        check=True, capture_output=True)
    subprocess.run([str(python), '-I', '-c',
        'from amadeus_extension import Extension; import amadeus_extension.runner'],
        cwd=tmp_path, check=True, capture_output=True)
    wheel.write_bytes(b'damaged artifact')
    failed = subprocess.run(command, cwd=clean, env=environment, capture_output=True, text=True)
    assert failed.returncode != 0 and 'SHA-256 verification' in failed.stderr
    desktop = json.loads((ROOT / 'electron/package.json').read_text(encoding='utf-8'))
    assert 'build:extension-sdk' not in desktop['scripts']
    assert any(entry['from'] == '../vendor/extension-sdk' and entry['to'] == 'extension-sdk'
               for entry in desktop['build']['extraResources'])


@pytest.mark.parametrize('requirement', [
    'amadeus-extension-sdk>=0.1,<0.2', 'Amadeus_Extension.SDK==0.1.0',
    'amadeus-extension-sdk~=0.1.0', 'amadeus-extension-sdk==0.1.*',
    'amadeus-extension-sdk>=0.2; python_version < "3.0"',
    'amadeus-extension-sdk>=0.2; extra == "optional"', 'other-package>=1',
])
def test_sdk_compatible_ranges_exact_versions_and_inactive_markers(requirement):
    validate_sdk_requirements([requirement])


@pytest.mark.parametrize(('requirement', 'reason'), [
    ('amadeus-extension-sdk>=0.2', 'requires SDK >=0.2; Amadeus provides 0.1.0'),
    ('amadeus-extension-sdk!=0.1.0', 'requires SDK !=0.1.0'),
    ('amadeus-extension-sdk==0.0.9; python_version >= "3.0"', 'requires SDK ==0.0.9'),
    ('amadeus-extension-sdk @ https://user:synthetic-secret@example.invalid/sdk.whl', 'SDK direct URL'),
    ('invalid !!! requirement', 'invalid Python dependency'),
])
def test_sdk_incompatible_requirements_have_actionable_sanitized_errors(requirement, reason):
    with pytest.raises(ValueError) as error:
        validate_sdk_requirements([requirement])
    assert reason in str(error.value) and 'synthetic-secret' not in str(error.value)


def test_wheel_inspection_checks_sdk_before_installing(tmp_path):
    from test_extensions import manifest
    from server.extensions.package import inspect_wheel
    wheel = tmp_path / 'synthetic-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('synthetic_voice/amadeus-extension.json', json.dumps(manifest()))
        archive.writestr('synthetic_voice/__init__.py', 'raise AssertionError("must not import")')
        archive.writestr('synthetic-0.1.0.dist-info/entry_points.txt', '[amadeus.extensions]\ncom.example.synthetic = synthetic_voice\n')
        archive.writestr('synthetic-0.1.0.dist-info/METADATA', 'Name: synthetic\nVersion: 0.1.0\nRequires-Dist: amadeus-extension-sdk>=0.2\n')
    with pytest.raises(ValueError, match='requires SDK >=0.2; Amadeus provides'):
        inspect_wheel(wheel)
