"""Copy indexed authoring PNG pixels into an isolated video experiment reference.

The PNGs remain untouched. Matching names/counts are recorded separately from
matching current KTX2 dimensions; neither alone proves color-processing parity.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument('--png-dir', type=Path)
    source_group.add_argument('--production-manifest', type=Path,
                              help='Existing production ledger tying each input PNG to the installed KTX2 hash')
    parser.add_argument('--pack', required=True, type=Path)
    parser.add_argument('--clip', default='idle_closed_eye')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    manifest_bytes = (args.pack/'runtime_manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    clip = manifest['clips'][args.clip]
    count = len(clip['frames']) if args.limit is None else args.limit
    if not 1 <= count <= len(clip['frames']):
        parser.error('Invalid frame limit')
    production = None
    if args.production_manifest:
        ledger_bytes = args.production_manifest.read_bytes()
        ledger = json.loads(ledger_bytes)
        if ledger['version'] != manifest['version']:
            raise ValueError('Production ledger and installed pack version differ')
        production = sorted((r for r in ledger['records'] if r['kind'] == 'body' and r['label'] == args.clip),
                            key=lambda r: r['index'])
        if len(production) != len(clip['frames']):
            raise ValueError('Production ledger clip count differs')
        for i, record in enumerate(production):
            if record['index'] != i or record['runtimeRelative'] != clip['frames'][i]:
                raise ValueError('Production ledger source ordering differs')
        sources = [Path(r['sourcePng']) for r in production[:count]]
    else:
        sources = [args.png_dir/(Path(name).stem+'.png') for name in clip['frames'][:count]]
    if any(not source.is_file() for source in sources):
        raise ValueError('PNG filenames do not match the indexed clip')
    args.output.mkdir(parents=True, exist_ok=False)
    hashes, raw_hash, dimensions_match = [], hashlib.sha256(), True
    size = None
    with (args.output/'source.rgba').open('xb') as output:
        for i, source in enumerate(sources):
            png_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            if production:
                record = production[i]
                ktx_hash = hashlib.sha256((args.pack/clip['frames'][i]).read_bytes()).hexdigest()
                if png_hash != record['sourcePngSha256'] or ktx_hash != record['outputSha256']:
                    raise ValueError(f'Production PNG/KTX2 hash mismatch at {i}')
            with Image.open(source) as image:
                if image.mode != 'RGBA':
                    raise ValueError('Expected original RGBA PNG')
                size = size or image.size
                if image.size != size:
                    raise ValueError('Prototype requires constant dimensions within a clip')
                pixels = image.tobytes()
            with (args.pack/clip['frames'][i]).open('rb') as texture:
                header = texture.read(32)
            dimensions_match &= struct.unpack_from('<II', header, 20) == size
            output.write(pixels); raw_hash.update(pixels)
            hashes.append(png_hash)
            if (i+1) % 100 == 0:
                print(f'Prepared {i+1}/{count}', flush=True)
    if production and not dimensions_match:
        raise ValueError('Production PNG and installed KTX2 geometry differ')
    metadata = dict(schema='amadeus.frame-video-reference.v1', sourceKind='production-rgba-png' if production else 'authoring-rgba-png',
                    packVersion=manifest['version'], clip=args.clip, width=size[0], height=size[1], frames=count,
                    completeClip=count == len(clip['frames']), frameIntervalMs=clip['frameIntervalMs'],
                    manifestSha256=hashlib.sha256(manifest_bytes).hexdigest(), sourceSha256=raw_hash.hexdigest(),
                    pngSha256=hashes, currentPackDimensionsMatch=dimensions_match,
                    productionLineageVerified=bool(production),
                    limitation='Production ledger PNG/KTX2 hashes verified; codec errors still require pixel comparison.' if production else
                    'PNG names follow the runtime manifest. Current pack color-processing parity needs independent validation.')
    if production:
        metadata['productionLedgerSha256'] = hashlib.sha256(ledger_bytes).hexdigest()
    (args.output/'source.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in metadata.items() if k != 'pngSha256'}),flush=True)


if __name__ == '__main__':
    main()
