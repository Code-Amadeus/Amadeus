"""Compare real static BC7 and paired-video captures of the same source frame."""
import argparse
import hashlib
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
    metadata = json.loads((args.fixture/'clip.json').read_text())
    index = metadata['headFrames']
    paths = dict(source=args.fixture/f'reference-{index:06d}.png',
                 static=args.captures/f'static-body-{index}.png',
                 video=args.captures/f'video-body-{index}.png')
    pixels = {key: np.array(Image.open(path).convert('RGBA')) for key, path in paths.items()}
    report = dict(sourceIndex=index, hashes={key: hashlib.sha256(path.read_bytes()).hexdigest()
                                           for key, path in paths.items()},
                  sourceToStatic=compare(pixels['source'], pixels['static']),
                  sourceToVideo=compare(pixels['source'], pixels['video']),
                  staticToVideo=compare(pixels['static'], pixels['video']),
                  limitation='Matched first-body-frame capture; static/video differences quantify the extra '
                  'boundary error, not motion acceptance. Static-reference zero differences are not source PNG leakage.')
    args.output.mkdir(parents=True, exist_ok=False)
    report['artifacts'] = []
    for view, box in [('full', None), ('head', crop_box(pixels['source'][..., 3], 'head', 256)),
                      ('edge', crop_box(pixels['source'][..., 3], 'edge', 256))]:
        sheet, details = contact_sheet(pixels['static'], pixels['video'], source_index=index,
                                       view=view, reference_label='Installed BC7', actual_label='Video', box=box)
        name = f'bc7-video-{view}.png'
        sheet.save(args.output/name)
        report['artifacts'].append(dict(file=name, **details))
    (args.output/'handoff.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'artifacts'}))


if __name__ == '__main__':
    main()
