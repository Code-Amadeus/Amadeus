"""Check the pinned protocol copies; optionally verify the canonical Git source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    check_protocol(parser.parse_args().source)
    print('Pinned extension protocol copies match')
