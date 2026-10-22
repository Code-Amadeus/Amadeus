"""Optional package imagery never expands the package filesystem boundary."""
import copy
import os

import pytest

from server.extensions import manifest as vm


MANIFEST = {
    'schema': 'amadeus.extension/v0', 'id': 'org.example.icon', 'version': '0.1.0',
    'display_name': 'Example', 'description': 'Icon fixture', 'author': {'name': 'Example'},
    'engines': {'amadeus_host_api': '>=0.1,<0.2'}, 'runtime': {'kind': 'none'},
    'contributes': {'theme': [{'id': 'plain', 'contract': 'amadeus.ui.theme/v0', 'base': 'classic', 'tokens': {}}]},
}


def icon_manifest(icon):
    return {**copy.deepcopy(MANIFEST), 'icon': icon}


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
    assert vm.package_issues(manifest, tmp_path)[0].code == 'P3.missing_file'
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
