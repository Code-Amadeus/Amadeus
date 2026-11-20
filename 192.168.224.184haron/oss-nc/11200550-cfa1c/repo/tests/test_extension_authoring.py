"""Author tools use the installed-package validator without executing plugin code."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from server.extensions.package import inspect_wheel, validate_installed, validate_manifest
from tools.extension_package import check_wheel

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/extension_package.py'
SOURCE = ROOT / 'examples/extensions/handoff-skill/src/amadeus_example_handoff'


def package(tmp_path, *, broken=None):
    directory = tmp_path / 'amadeus_example_handoff'
    directory.mkdir()
    manifest = json.loads((SOURCE / 'amadeus-extension.json').read_text(encoding='utf-8'))
    (directory / 'amadeus-extension.json').write_text(json.dumps(manifest), encoding='utf-8')
    (directory / '__init__.py').write_text('raise AssertionError("package must not be imported")', encoding='utf-8')
    skill = directory / 'skills/handoff-brief'
    skill.mkdir(parents=True)
    if broken != 'missing':
        (skill / 'SKILL.md').write_text('---\nname: handoff-brief\ndescription: Static test\n---\nRead references/layout.json.\n'
            if broken != 'metadata' else 'not valid skill metadata', encoding='utf-8')
    (skill / 'references').mkdir()
    (skill / 'references/layout.json').write_text('{}', encoding='utf-8')
    wheel = tmp_path / 'example-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for path in directory.rglob('*'):
            if path.is_file():
                archive.write(path, str(path.relative_to(tmp_path)).replace('\\', '/'))
        archive.writestr('example-0.1.0.dist-info/METADATA', 'Name: example\nVersion: 0.1.0\n')
        archive.writestr('example-0.1.0.dist-info/entry_points.txt',
            f"[amadeus.extensions]\n{manifest['id']} = {directory.name}\n")
    return directory, manifest, wheel


def cli(*args, input=None):
    return subprocess.run([sys.executable, '-I', str(CLI), *map(str, args)],
                          input=input, capture_output=True, text=True, encoding='utf-8', timeout=20)


def test_source_and_wheel_checks_validate_resources_without_import(tmp_path):
    directory, manifest, wheel = package(tmp_path)
    validate_installed(manifest, directory)
    for command, target in [('check-package', directory), ('check-wheel', wheel)]:
        result = cli(command, target)
        assert result.returncode == 0, result.stderr + result.stdout
        payload = json.loads(result.stdout)
        assert payload['ok'] and payload['manifest'] == manifest
        assert 'skill_metadata' in payload['checked']
        assert {'native_theme', 'runtime', 'catalog_review'} <= set(payload['not_checked'])
        assert payload['host_profile']['platform'] == sys.platform
    assert check_wheel(wheel)['digest'] == inspect_wheel(wheel)['digest']


@pytest.mark.parametrize('broken', ['missing', 'metadata'])
def test_resource_failure_matches_desktop_installed_validation(tmp_path, broken):
    directory, manifest, wheel = package(tmp_path, broken=broken)
    # Existing inspection alone intentionally does not promise resource qualification.
    inspect_wheel(wheel)
    with pytest.raises(ValueError) as host_error:
        validate_installed(manifest, directory)
    result = cli('check-wheel', wheel)
    assert result.returncode == 1
    assert json.loads(result.stdout)['error'] == str(host_error.value)
    text = cli('check-package', directory, '--format', 'text')
    assert text.returncode == 1 and str(host_error.value) in text.stdout


def test_legacy_json_helper_shape_and_secret_redaction(tmp_path):
    _, manifest, _ = package(tmp_path)
    manifest['config'] = {'token': {'type': 'string', 'title': 'Token', 'secret': True, 'required': True}}
    body = json.dumps({'manifest': manifest, 'config': {'token': 'synthetic-private-value'}})
    for output in [[], ['--format', 'text']]:
        result = cli('validate-config', *output, input=body)
        assert result.returncode == 0
        assert 'synthetic-private-value' not in result.stdout + result.stderr
        if not output:
            assert json.loads(result.stdout) == {'ok': True, 'config': {}, 'missing': []}
    malformed = cli('validate-config', '--format', 'text', input='{"synthetic-private-value":')
    assert malformed.returncode == 1 and 'synthetic-private-value' not in malformed.stdout + malformed.stderr


def test_profile_reports_actual_host_and_does_not_open_new_contributions():
    payload = json.loads(cli('profile').stdout)
    profile = payload['host_profile']
    assert profile['platform'] == sys.platform
    assert {(row['kind'], row.get('role')) for row in profile['contributions'] if row['status'] == 'open'} == {
        ('voice_backend', 'tts'), ('mcp_server', None), ('skill', None), ('theme', None)}
    text = cli('profile', '--format', 'text')
    assert text.returncode == 0 and profile['python'] in text.stdout


def test_target_admission_matches_native_validator(tmp_path):
    directory, manifest, _ = package(tmp_path)
    manifest = copy.deepcopy(manifest)
    manifest['engines']['platforms'] = ['linux' if sys.platform == 'win32' else 'win32']
    (directory / 'amadeus-extension.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError) as error:
        validate_manifest(manifest)
    result = cli('check-package', directory)
    assert result.returncode == 1 and json.loads(result.stdout)['error'] == str(error.value)


@pytest.mark.parametrize('defect', ['version', 'sdk', 'entry_point'])
def test_source_check_discloses_unchecked_wheel_metadata(tmp_path, defect):
    source = ROOT / 'examples/extensions/mimo-tts/src/amadeus_example_mimo'
    directory = tmp_path / source.name
    shutil.copytree(source, directory)
    manifest = json.loads((directory / 'amadeus-extension.json').read_text(encoding='utf-8'))
    manifest['engines']['platforms'] = [sys.platform]
    (directory / 'amadeus-extension.json').write_text(json.dumps(manifest), encoding='utf-8')
    version = '9.9.0' if defect == 'version' else manifest['version']
    metadata = f"Name: example\nVersion: {version}\n"
    if defect == 'sdk':
        metadata += 'Requires-Dist: amadeus-extension-sdk==9.9\n'
    wheel = tmp_path / 'example-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for resource in directory.rglob('*'):
            if resource.is_file():
                archive.write(resource, resource.relative_to(tmp_path).as_posix())
        archive.writestr('example-0.1.0.dist-info/METADATA', metadata)
        entry = 'wrong_package' if defect == 'entry_point' else directory.name
        archive.writestr('example-0.1.0.dist-info/entry_points.txt',
                         f"[amadeus.extensions]\n{manifest['id']} = {entry}\n")
    source_result = cli('check-package', directory)
    assert source_result.returncode == 0
    payload = json.loads(source_result.stdout)
    assert 'host_admission' not in payload['checked']
    assert 'wheel_metadata' in payload['not_checked']
    assert 'check-wheel' in payload['next_step']
    source_text = cli('check-package', directory, '--format', 'text')
    assert 'Host admission' not in source_text.stdout
    assert 'Not checked: wheel metadata' in source_text.stdout
    assert 'check-wheel' in source_text.stdout
    with pytest.raises(ValueError) as installer_error:
        inspect_wheel(wheel)
    wheel_result = cli('check-wheel', wheel)
    assert wheel_result.returncode == 1
    assert json.loads(wheel_result.stdout)['error'] == str(installer_error.value)


def test_wrong_source_directory_is_input_error_not_validator_unavailable(tmp_path):
    for directory in [tmp_path, tmp_path / 'missing']:
        result = cli('check-package', directory)
        payload = json.loads(result.stdout)
        assert result.returncode == 1 and not payload['ok']
        assert 'amadeus-extension.json' in payload['error']
        assert 'unavailable' not in payload
        assert str(tmp_path) not in result.stdout
