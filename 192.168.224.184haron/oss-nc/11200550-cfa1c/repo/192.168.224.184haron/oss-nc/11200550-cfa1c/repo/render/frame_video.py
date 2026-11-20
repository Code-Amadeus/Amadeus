"""Discover an optional video companion inside the existing SpriteForge pack.

Old packs need no manifest or directory changes. The browser owns the final
codec capability decision; this module only qualifies installed package identity.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def frame_source_config(runtime_root: Path, *, mode: str = 'auto', budget_mib: int = 0,
                        asset_server=None) -> dict:
    result = {'requested': mode, 'budgetMiB': budget_mib, 'candidate': None,
              'reason': 'static_selected' if mode == 'static' else 'video_pack_missing'}
    files = {}
    try:
        if mode == 'static':
            return result
        video_root = runtime_root / 'video'
        if not (video_root / 'bundle.json').is_file():
            return result
        raw = (runtime_root / 'runtime_manifest.json').read_bytes()
        manifest = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        bundle = json.loads((video_root / 'bundle.json').read_text(encoding='utf-8'))
        if (bundle['schema'] != 'amadeus.frame-video-bundle-experiment.v1'
                or bundle['packVersion'] != manifest['version']
                or bundle['manifestSha256'] != digest):
            raise ValueError('Video package identity differs from the installed static pack')
        entries = bundle['clips']
        if (len(entries) != len(manifest['clips'])
                or {entry['label'] for entry in entries} != set(manifest['clips'])):
            raise ValueError('Video package does not cover the installed clips')
        files = {'bundle.json': video_root / 'bundle.json'}
        for entry in entries:
            label = entry['label']
            if not label.replace('_', '').isalnum() or entry['path'] != label + '/':
                raise ValueError('Invalid video clip path')
            path = video_root / label / 'clip.json'
            metadata = json.loads(path.read_text(encoding='utf-8'))
            reference, static = metadata['reference'], manifest['clips'][label]
            if (metadata['schema'] != 'amadeus.frame-video-split-experiment.v1'
                    or reference['clip'] != label or reference['manifestSha256'] != digest
                    or reference['packVersion'] != manifest['version']
                    or reference['productionLedgerSha256'] != bundle['productionLedgerSha256']
                    or not reference['productionLineageVerified'] or not reference['currentPackDimensionsMatch']
                    or metadata['frames'] != len(static['frames'])
                    or metadata['frameIntervalMs'] != static['frameIntervalMs']):
                raise ValueError('Video clip differs from installed source identities')
            files[label + '/clip.json'] = path
            for kind in ('color', 'alpha'):
                stream = metadata['streams'][kind]
                if stream['video'] != kind + '.ivf':
                    raise ValueError('Invalid video stream path')
                stream_path = video_root / label / stream['video']
                if not stream_path.is_file():
                    raise ValueError('Missing video stream')
                files[label + '/' + stream['video']] = stream_path
        base_url = video_root.resolve().as_uri() + '/'
        if asset_server is not None:
            base_url = '/frame-video/'
        result.update(reason='video_pack_available', candidate={
            'baseUrl': base_url,
            'pack': {'available': True, 'version': manifest['version'],
                     'sourceSha256': {'runtime_manifest.json': digest}},
        })
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(reason='video_pack_invalid', detail=str(exc))
    finally:
        if asset_server is not None:
            # The caller owns this surface's server. Replace only our prefix,
            # including revocation on static/missing/invalid package selection.
            # Publish only runtime metadata/streams, never quality images/logs.
            asset_server.mount_files('/frame-video', files if result['candidate'] else {})
    return result
