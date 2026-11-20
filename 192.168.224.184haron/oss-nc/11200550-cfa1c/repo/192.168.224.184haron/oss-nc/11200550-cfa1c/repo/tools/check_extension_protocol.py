"""Check the pinned protocol copies; optionally verify the canonical Git source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
from email.parser import BytesParser

ROOT = Path(__file__).resolve().parents[1]


def digest(text: str) -> str:
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()


def check_protocol(source: Path | None = None) -> None:
    provenance = json.loads((ROOT / 'server/extensions/protocol-source.json').read_text(encoding='utf-8'))
    assert provenance['repository'] == 'https://github.com/Code-Amadeus/Amadeus-Extensions'
    assert len(provenance['commit']) == 40
    for entry in provenance['files']:
        copied = (ROOT / entry['host_path']).read_text(encoding='utf-8')
        assert digest(copied) == entry['host_sha256'], f"Protocol copy drift: {entry['host_path']}"
        if source is not None:
            original = subprocess.run(['git', '-C', str(source), 'show', f"{provenance['commit']}:{entry['source_path']}"],
                check=True, capture_output=True, text=True, encoding='utf-8').stdout
            assert digest(original) == entry['source_sha256'], f"Pinned source drift: {entry['source_path']}"
            if entry['source_path'].endswith('.json'):
                assert copied == original
            else:
                # The only local adaptations are the module help and schema location.
                assert copied.split('MANIFEST_NAME =', 1)[1] == original.split('MANIFEST_NAME =', 1)[1]
    sdk = provenance['sdk']
    wheel_path = ROOT / 'vendor/extension-sdk' / sdk['filename']
    assert hashlib.sha256(wheel_path.read_bytes()).hexdigest() == sdk['sha256'], 'SDK wheel digest mismatch'
    assert len(sdk['source_commit']) == 40
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_path, = [name for name in wheel.namelist() if name.endswith('.dist-info/METADATA')]
        metadata = BytesParser().parsebytes(wheel.read(metadata_path))
        assert metadata['Name'] == sdk['name'] and metadata['Version'] == sdk['version']
        assert metadata['License-Expression'] == 'Apache-2.0'
        assert metadata['License-File'] == 'LICENSE' and not metadata.get_all('Requires-Dist')
        license_path = metadata_path.rsplit('/', 1)[0] + '/licenses/LICENSE'
        for entry in sdk['source_files']:
            if entry['path'].startswith('sdk/python/src/'):
                member = entry['path'][len('sdk/python/src/'):]
            elif entry['path'] == 'sdk/python/LICENSE':
                member = license_path
            else:
                member = None
            if member is not None:
                assert hashlib.sha256(wheel.read(member)).hexdigest() == entry['sha256'], f'SDK source drift: {member}'
            if source is not None:
                original = subprocess.run(['git', '-C', str(source), 'show', f"{sdk['source_commit']}:{entry['path']}"],
                    check=True, capture_output=True).stdout
                assert hashlib.sha256(original).hexdigest() == entry['sha256'], f"SDK origin drift: {entry['path']}"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    check_protocol(parser.parse_args().source)
    print('Pinned extension protocol copies match')
