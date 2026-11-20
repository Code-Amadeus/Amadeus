"""Production provenance must tie source pixels to the installed indexed pack."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys

from PIL import Image
import pytest


spec = importlib.util.spec_from_file_location(
    'frame_video_prepare', Path(__file__).parents[1] / 'tools/probes/frame-video/prepare.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path):
    pack = tmp_path / 'pack'
    pack.mkdir()
    records = []
    for index in range(2):
        png = tmp_path / f'{index}.png'
        Image.new('RGBA', (3, 5), (23, 70, 140, index * 255)).save(png)
        header = bytearray(32)
        struct.pack_into('<II', header, 20, 3, 5)
        name = f'{index}.ktx2'
        (pack / name).write_bytes(header)
        records.append(dict(kind='body', label='idle_closed_eye', index=index,
                            runtimeRelative=name, sourcePng=str(png),
                            sourcePngSha256=hashlib.sha256(png.read_bytes()).hexdigest(),
                            outputSha256=hashlib.sha256(header).hexdigest()))
    (pack / 'runtime_manifest.json').write_text(json.dumps(dict(version='test', clips=dict(
        idle_closed_eye=dict(frames=['0.ktx2', '1.ktx2'], frameIntervalMs=17)))))
    ledger = tmp_path / 'ledger.json'
    ledger.write_text(json.dumps(dict(version='test', records=records)))
    return pack, ledger


def invoke(monkeypatch, pack, ledger, output):
    monkeypatch.setattr(sys, 'argv', ['prepare', '--pack', str(pack),
                                    '--production-manifest', str(ledger), '--output', str(output)])
    module.main()


def test_verified_source_preserves_indexed_rgba_and_records_lineage(tmp_path, monkeypatch):
    pack, ledger = fixture(tmp_path)
    output = tmp_path / 'out'
    invoke(monkeypatch, pack, ledger, output)
    meta = json.loads((output / 'source.json').read_text())
    assert meta['productionLineageVerified'] and meta['currentPackDimensionsMatch']
    assert meta['completeClip'] and meta['frames'] == 2
    expected = b''.join(Image.open(tmp_path / f'{i}.png').tobytes() for i in range(2))
    assert (output / 'source.rgba').read_bytes() == expected
    assert meta['sourceSha256'] == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize('tamper', ['png', 'ktx', 'order', 'version'])
def test_changed_source_or_pack_never_gets_verified_metadata(tmp_path, monkeypatch, tamper):
    pack, ledger = fixture(tmp_path)
    data = json.loads(ledger.read_text())
    if tamper == 'png':
        Image.new('RGBA', (3, 5), 'red').save(tmp_path / '0.png')
    elif tamper == 'ktx':
        (pack / '0.ktx2').write_bytes(b'changed')
    elif tamper == 'order':
        data['records'][0]['runtimeRelative'] = '1.ktx2'
    else:
        data['version'] = 'different'
    ledger.write_text(json.dumps(data))
    output = tmp_path / 'out'
    with pytest.raises(ValueError, match='hash mismatch|ordering differs|version differ'):
        invoke(monkeypatch, pack, ledger, output)
    assert not (output / 'source.json').exists()
