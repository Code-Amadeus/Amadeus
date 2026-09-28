"""Package approved, generated small portraits; no runtime authoring-workspace dependency.

Use tools/external_assets.py afterwards to build a portable installable archive.
This authoring command requires Pillow; bundle consumers do not.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from render.companion_pack import FORMAT, load_companion_pack, validate_clip  # noqa: E402


def package(candidate: Path, alternate: Path, output: Path) -> dict:
    candidate, alternate, output = candidate.resolve(), alternate.resolve(), output.resolve()
    if output.exists():
        raise ValueError(f'Output already exists; choose a fresh directory: {output}')
    source = json.loads((candidate / 'manifest.json').read_text(encoding='utf-8'))
    secondary = json.loads((alternate / 'manifest.json').read_text(encoding='utf-8'))
    if 'normal' not in source.get('emotions', {}):
        raise ValueError('Candidate requires a normal portrait')
    result = {'format': FORMAT, 'id': 'kurisu', 'version': '2026.09.19',
              'sourceManifestSha256': source['sourceManifestSha256'], 'emotions': {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='companion-build-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'pack'
        stage.mkdir()

        def copy_clip(spec: dict, root: Path, target_url: str | None = None):
            url = spec['url']
            dimensions = validate_clip(spec)
            file = (root / url).resolve()
            file.relative_to(root)
            sequence = spec['sequence']
            size, columns, duration = spec['size'], spec['columns'], spec['durationMs']
            decoded = dimensions[0] * dimensions[1] * 4
            with Image.open(file) as image:
                if image.format != 'WEBP' or image.size != dimensions:
                    raise ValueError('Portrait dimensions differ from manifest')
                image.load()
            target_url = target_url or url
            destination = stage / target_url
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
            return {'url': target_url, 'size': size, 'columns': columns, 'sequence': sequence,
                    'durationMs': duration, 'decodedBytes': decoded,
                    'fileBytes': destination.stat().st_size,
                    'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}

        for emotion, states in source['emotions'].items():
            result['emotions'][emotion] = {mode: copy_clip(states[mode], candidate)
                                          for mode in ['idle', 'speaking', 'idleStatic'] if mode in states}
        result['emotions']['sided_thinking']['speakingAlternate'] = copy_clip(
            secondary['emotions']['sided_thinking']['speaking'], alternate, 'sided_thinking/speaking-alternate.webp')
        (stage / 'manifest.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        load_companion_pack(stage)
        os.replace(stage, output)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--alternate', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'assets/companion/kurisu')
    args = parser.parse_args()
    manifest = package(args.candidate, args.alternate, args.output)
    print(json.dumps({'output': str(args.output.resolve()), 'emotions': len(manifest['emotions'])}))


if __name__ == '__main__':
    main()
