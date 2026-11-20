"""Compare every bundle boundary capture with its verified production PNG."""
import argparse
import json
from pathlib import Path
import numpy as np
from PIL import Image
from compare_captures import compare
from visual_review import contact_sheet, crop_box


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--captures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle = json.loads((args.fixture/'bundle.json').read_text(encoding='utf-8'))
    content = json.loads((args.captures/'video-bundle-content.json').read_text(encoding='utf-8'))
    if not content['passed']:
        raise ValueError('Full material GPU content checks did not pass')
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for clip in bundle['clips']:
        label, index = clip['label'], clip['headFrames']
        paths = dict(source=args.fixture/label/f'reference-{index:06d}.png',
                     static=args.captures/f'bundle-{label}-static-{index}.png',
                     video=args.captures/f'bundle-{label}-video-{index}.png')
        pixels = {key: np.asarray(Image.open(path).convert('RGBA')) for key, path in paths.items()}
        rows.append(dict(label=label, sourceIndex=index,
                         sourceToStatic=compare(pixels['source'], pixels['static']),
                         sourceToVideo=compare(pixels['source'], pixels['video']),
                         staticToVideo=compare(pixels['static'], pixels['video'])))
    worst = sorted(rows, key=lambda row: row['sourceToVideo']['visibleCompositePSNR'])[:3]
    for row in worst:
        label, index = row['label'], row['sourceIndex']
        static = np.asarray(Image.open(args.captures/f'bundle-{label}-static-{index}.png').convert('RGBA'))
        video = np.asarray(Image.open(args.captures/f'bundle-{label}-video-{index}.png').convert('RGBA'))
        for view, box in [('full', None), ('edge', crop_box(static[...,3], 'edge', 256))]:
            sheet, _ = contact_sheet(static, video, source_index=index, view=view,
                                     reference_label='Installed BC7', actual_label='Video', box=box)
            sheet.save(args.output/f'{label}-{view}.png')
    result = dict(clips=len(rows), packVersion=bundle['packVersion'], rows=rows,
                  allSampledAlphaExact=all(row['sourceToVideo']['alphaMaxAbsError']==0 for row in rows),
                  allSampledTransparentPixelsClear=all(row['sourceToVideo']['zeroLeakFraction']==0 for row in rows),
                  minimumSampledVisiblePSNR=min(row['sourceToVideo']['visibleCompositePSNR'] for row in rows),
                  minimumSampledVisibleSSIM=min(row['sourceToVideo']['visibleCompositeLumaSSIM'] for row in rows),
                  worstLabels=[row['label'] for row in worst],
                  limitation='One hardware boundary frame per clip. Software verification covers every source frame; neither replaces motion review.')
    (args.output/'quality.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='rows'}))
    if not result['allSampledAlphaExact'] or not result['allSampledTransparentPixelsClear']:
        raise ValueError('Hardware alpha differs from the verified production source')


if __name__ == '__main__':
    main()
