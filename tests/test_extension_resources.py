"""No-code preparation uses the same inspected wheel and never imports it."""
import json
import zipfile

import pytest

from server.extensions.package import inspect_wheel, prepare_data_wheel


def wheel(tmp_path, *, dependency=False, unsafe=False):
    manifest = {
        'schema': 'amadeus.extension/v0', 'id': 'org.example.resources', 'version': '0.1.0',
        'display_name': 'Resource fixture', 'description': 'Static Skill resources', 'author': {'name': 'Test'},
        'engines': {'amadeus_host_api': '>=0.1,<0.2'}, 'runtime': {'kind': 'none'},
        'contributes': {'skill': [{'id': 'guide', 'contract': 'amadeus.skill/v0', 'path': 'guide'}]},
    }
    output = tmp_path / 'resources-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('resources/amadeus-extension.json', json.dumps(manifest))
        archive.writestr('resources/__init__.py', 'raise RuntimeError("must never import during preparation")')
        archive.writestr('resources/guide/SKILL.md', '---\nname: guide\ndescription: Test guide\n---\nRead the resource.\n')
        archive.writestr('resources-0.1.0.dist-info/METADATA', 'Name: resources\nVersion: 0.1.0\n' + ('Requires-Dist: forbidden\n' if dependency else ''))
        archive.writestr('resources-0.1.0.dist-info/entry_points.txt', '[amadeus.extensions]\norg.example.resources = resources\n')
        archive.writestr('resources-0.1.0.data/scripts/unused', 'never extract outside the package')
        if unsafe:
            archive.writestr('resources/../outside.txt', 'invalid')
    return output


def test_no_code_preparation_extracts_only_inspected_package(tmp_path):
    source = wheel(tmp_path)
    destination = tmp_path / 'package'
    inspection = prepare_data_wheel(source, destination)
    assert inspection['digest'] == inspect_wheel(source)['digest']
    assert (destination / 'resources/guide/SKILL.md').read_text().endswith('Read the resource.\n')
    assert sorted(path.name for path in destination.iterdir()) == ['resources']
    assert not (tmp_path / 'venv').exists()
    with pytest.raises(ValueError, match='must be new'):
        prepare_data_wheel(source, destination)


@pytest.mark.parametrize('option,expected', [('dependency', 'Requires-Dist'), ('unsafe', 'unsafe')])
def test_no_code_invalid_wheel_is_rejected_before_extraction(tmp_path, option, expected):
    destination = tmp_path / 'package'
    with pytest.raises(ValueError, match=expected):
        prepare_data_wheel(wheel(tmp_path, **{option: True}), destination)
    assert not destination.exists()


@pytest.mark.skipif(__import__('os').name == 'nt', reason='POSIX executable permission contract')
def test_resource_mcp_command_preserves_execute_bits_without_special_permissions(tmp_path):
    import stat
    import subprocess
    source = wheel(tmp_path)
    with zipfile.ZipFile(source) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    manifest = json.loads(entries['resources/amadeus-extension.json'])
    manifest['contributes'] = {'mcp_server': [{'id': 'local', 'contract': 'amadeus.mcp-server/v0',
        'transport': 'stdio', 'command': '${package}/bin/server', 'args': []}]}
    entries['resources/amadeus-extension.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(source, 'w') as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
        command = zipfile.ZipInfo('resources/bin/server')
        command.create_system = 3
        command.external_attr = (stat.S_IFREG | 0o4755) << 16
        archive.writestr(command, '#!/bin/sh\nprintf "resource-command-ready"\n')
    destination = tmp_path / 'installed'
    prepare_data_wheel(source, destination)
    command = destination / 'resources/bin/server'
    assert stat.S_IMODE(command.stat().st_mode) & 0o111 == 0o111
    assert stat.S_IMODE(command.stat().st_mode) & 0o7000 == 0
    assert stat.S_IMODE((destination / 'resources/guide/SKILL.md').stat().st_mode) & 0o111 == 0
    assert subprocess.check_output([str(command)], text=True) == 'resource-command-ready'

def test_theme_only_has_no_runner_or_work_capability_projection(tmp_path):
    from server.extensions.runtime import ExtensionRuntime
    from server.capability_composition import extension_capability_packages
    definition = {
        'schema': 'amadeus.extension/v0', 'id': 'org.example.theme', 'version': '0.1.0',
        'display_name': 'Theme', 'description': 'No backend authority', 'author': {'name': 'Test'},
        'engines': {'amadeus_host_api': '>=0.1,<0.2'}, 'runtime': {'kind': 'none'},
        'contributes': {'theme': [{'id': 'day', 'contract': 'amadeus.ui.theme/v0', 'base': 'classic', 'tokens': {}}]},
    }
    (tmp_path / 'amadeus-extension.json').write_text(json.dumps(definition), encoding='utf-8')
    runtime = ExtensionRuntime([{'id': definition['id'], 'version': '0.1.0', 'revision': 2,
        'digest': 'a' * 64, 'enabled': True, 'manifest': definition,
        'package_path': str(tmp_path), 'data_path': str(tmp_path / 'data'), 'config': {}, 'secrets': {}}])
    assert not runtime.clients and not runtime.backends
    assert runtime.statuses()[0]['revision'] == 2
    assert runtime.statuses()[0]['contributions'] == []
    assert extension_capability_packages(runtime.records, runtime.statuses()) == ()
