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
