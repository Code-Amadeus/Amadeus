"""Optional package imagery never expands the package filesystem boundary."""
import copy
import json
import os
from pathlib import Path
import zipfile

import pytest

from server.extensions import manifest as vm
from server.extensions.package import inspect_wheel, prepare_data_wheel, validate_manifest


MANIFEST = {
    'schema': 'amadeus.extension/v0', 'id': 'org.example.icon', 'version': '0.1.0',
    'display_name': 'Example', 'description': 'Icon fixture', 'author': {'name': 'Example'},
    'engines': {'amadeus_host_api': '>=0.1,<0.2'}, 'runtime': {'kind': 'none'},
    'contributes': {'theme': [{'id': 'plain', 'contract': 'amadeus.ui.theme/v0', 'base': 'classic', 'tokens': {}}]},
}


def icon_manifest(icon):
    return {**copy.deepcopy(MANIFEST), 'schema': 'amadeus.extension/v1', 'icon': icon}


def test_old_packages_need_no_icon(tmp_path):
    assert not vm.manifest_issues(MANIFEST)
    assert not vm.package_issues(MANIFEST, tmp_path)


@pytest.mark.parametrize('icon', ['https://example.com/a.png', '../a.png', '/a.png', 'C:/a.png', 'a\\b.png', '', None, 'a.svg'])
def test_icon_declaration_rejects_external_paths_and_active_formats(icon):
    assert vm.manifest_issues(icon_manifest(icon))


@pytest.mark.parametrize('name,header', [('icon.png', b'\x89PNG\r\n\x1a\n'), ('ICON.JPG', b'\xff\xd8\xff')])
def test_icon_package_checks_share_image_signature_and_byte_limits(tmp_path, name, header):
    manifest = icon_manifest(name)
    assert not vm.manifest_issues(manifest)
    assert vm.package_issues(manifest, tmp_path)[0].code == 'P5.package_icon'
    image = tmp_path / name
    image.write_bytes(header)
    assert not vm.package_issues(manifest, tmp_path)
    image.write_bytes(header + bytes(1024 * 1024))
    assert vm.package_issues(manifest, tmp_path)[0].code == 'P5.package_icon'
    image.write_bytes(b'<svg/>')
    assert vm.package_issues(manifest, tmp_path)[0].code == 'P5.package_icon'


@pytest.mark.skipif(os.name == 'nt', reason='Windows links are covered by the native Host junction test')
def test_icon_cannot_follow_a_link_out_of_its_package(tmp_path):
    package = tmp_path / 'package'
    package.mkdir()
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'\x89PNG\r\n\x1a\n')
    (package / 'icon.png').symlink_to(outside)
    assert vm.package_issues(icon_manifest('icon.png'), package)[0].code == 'P2.link_in_package'


def icon_wheel(tmp_path, definition, *, include_image=True):
    wheel = tmp_path / 'icon-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('icon_package/amadeus-extension.json', json.dumps(definition))
        archive.writestr('icon_package/__init__.py', 'raise AssertionError("must not execute")')
        archive.writestr('icon-0.1.0.dist-info/METADATA', 'Name: icon\nVersion: 0.1.0\n')
        archive.writestr('icon-0.1.0.dist-info/entry_points.txt', '[amadeus.extensions]\norg.example.icon = icon_package\n')
        if include_image and 'icon' in definition:
            image = Path(__file__).parents[1] / 'examples/extensions/desktop-theme/src/amadeus_example_theme/background.png'
            archive.writestr('icon_package/icon.png', image.read_bytes())
    return wheel


@pytest.mark.parametrize('schema,with_icon,accepted', [
    ('amadeus.extension/v0', False, True),
    ('amadeus.extension/v0', True, False),
    ('amadeus.extension/v1', False, True),
    ('amadeus.extension/v1', True, True),
    ('amadeus.extension/v2', False, False),
])
def test_wheel_admission_selects_schema_before_resource_preparation(tmp_path, schema, with_icon, accepted):
    definition = copy.deepcopy(MANIFEST)
    definition['schema'] = schema
    if with_icon:
        definition['icon'] = 'icon.png'
    wheel = icon_wheel(tmp_path, definition)
    destination = tmp_path / 'installed'
    if not accepted:
        with pytest.raises(ValueError, match='invalid manifest: schema'):
            prepare_data_wheel(wheel, destination)
        assert not destination.exists()
        return
    assert inspect_wheel(wheel)['manifest'] == definition
    assert prepare_data_wheel(wheel, destination)['manifest'] == definition
    installed = destination / 'icon_package'
    assert json.loads((installed / vm.MANIFEST_NAME).read_text(encoding='utf-8')) == definition
    assert (installed / 'icon.png').is_file() == with_icon


@pytest.mark.parametrize('schema', ['amadeus.extension/v0', 'amadeus.extension/v1'])
def test_manifest_version_keeps_host_engine_and_unknown_field_checks(schema):
    definition = {**copy.deepcopy(MANIFEST), 'schema': schema}
    definition['engines']['amadeus_host_api'] = '>=99'
    with pytest.raises(ValueError, match='incompatible Amadeus Host API'):
        validate_manifest(definition)
    definition['unexpected'] = 'not an extension point'
    with pytest.raises(ValueError, match='invalid manifest: schema'):
        validate_manifest(definition)


def test_missing_v1_icon_is_reported_by_package_icon_check(tmp_path):
    wheel = icon_wheel(tmp_path, icon_manifest('icon.png'), include_image=False)
    with pytest.raises(ValueError, match='P5.package_icon'):
        prepare_data_wheel(wheel, tmp_path / 'installed')
