"""Old SpriteForge paths remain valid; optional video must match their identities."""
import hashlib
import json
from unittest.mock import Mock

import pytest

from render.frame_video import frame_source_config


def make_pack(tmp_path):
    root = tmp_path / 'existing-pack'
    video = root / 'video'
    (video / 'idle').mkdir(parents=True)
    raw = json.dumps({'version': 'corrected-v1', 'clips': {
        'idle': {'frames': ['body/0.ktx2', 'body/1.ktx2', 'body/2.ktx2'], 'frameIntervalMs': 20}}}).encode()
    (root / 'runtime_manifest.json').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    bundle = {'schema': 'amadeus.frame-video-bundle-experiment.v1', 'packVersion': 'corrected-v1',
              'manifestSha256': digest, 'productionLedgerSha256': 'verified-ledger', 'clips': [{'label': 'idle', 'path': 'idle/'}]}
    clip = {'schema': 'amadeus.frame-video-split-experiment.v1', 'frames': 3, 'frameIntervalMs': 20,
            'reference': {'clip': 'idle', 'packVersion': 'corrected-v1', 'manifestSha256': digest,
                          'productionLineageVerified': True, 'currentPackDimensionsMatch': True, 'productionLedgerSha256': 'verified-ledger'},
            'streams': {kind: {'video': kind + '.ivf'} for kind in ('color', 'alpha')}}
    (video / 'bundle.json').write_text(json.dumps(bundle), encoding='utf-8')
    (video / 'idle/clip.json').write_text(json.dumps(clip), encoding='utf-8')
    for kind in ('color', 'alpha'):
        (video / 'idle' / (kind + '.ivf')).write_bytes(b'synthetic stream')
    return root, video, bundle, clip


def test_old_pack_needs_no_new_path_or_manifest_fields(tmp_path):
    root = tmp_path / 'old'
    root.mkdir()
    original = '{"version":"legacy"}'
    (root / 'runtime_manifest.json').write_text(original, encoding='utf-8')
    config = frame_source_config(root)
    assert config == {'requested': 'auto', 'budgetMiB': 0, 'candidate': None, 'reason': 'video_pack_missing'}
    assert (root / 'runtime_manifest.json').read_text() == original


def test_matched_package_uses_file_urls_or_a_narrow_http_mount(tmp_path):
    root, video, _, _ = make_pack(tmp_path)
    config = frame_source_config(root)
    assert config['candidate']['baseUrl'] == video.as_uri() + '/'
    host = Mock()
    config = frame_source_config(root, asset_server=host)
    assert config['candidate']['baseUrl'] == '/frame-video/'
    prefix, files = host.mount_files.call_args.args
    assert prefix == '/frame-video'
    assert set(files) == {'bundle.json', 'idle/clip.json', 'idle/color.ivf', 'idle/alpha.ivf'}


@pytest.mark.parametrize('fault', ['version', 'manifest', 'ledger', 'coverage', 'timing', 'path', 'missing-stream', 'malformed'])
def test_bad_optional_video_keeps_static_package_available(tmp_path, fault):
    root, video, bundle, clip = make_pack(tmp_path)
    if fault == 'version': bundle['packVersion'] = 'old'
    elif fault == 'manifest': bundle['manifestSha256'] = 'wrong'
    elif fault == 'ledger': clip['reference']['productionLedgerSha256'] = 'wrong'
    elif fault == 'coverage': bundle['clips'] = []
    elif fault == 'timing': clip['frameIntervalMs'] = 5
    elif fault == 'path': clip['streams']['color']['video'] = '../outside.ivf'
    elif fault == 'missing-stream': (video / 'idle/alpha.ivf').unlink()
    (video / 'bundle.json').write_text(json.dumps(bundle), encoding='utf-8')
    (video / 'idle/clip.json').write_text('{' if fault == 'malformed' else json.dumps(clip), encoding='utf-8')
    host = Mock()
    config = frame_source_config(root, asset_server=host)
    assert config['candidate'] is None and config['reason'] == 'video_pack_invalid'
    host.mount_files.assert_not_called()


def test_explicit_static_and_manual_budget_are_preserved(tmp_path):
    root, _, _, _ = make_pack(tmp_path)
    assert frame_source_config(root, mode='static', budget_mib=768) == {
        'requested': 'static', 'budgetMiB': 768, 'candidate': None, 'reason': 'static_selected'}
