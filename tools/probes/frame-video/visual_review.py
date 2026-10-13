"""Create multi-background A/B sheets from software or hardware RGBA captures.

Full-canvas previews may be scaled; head/edge sheets retain one image pixel per
source pixel. This creates review evidence and never judges visual acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


BACKGROUNDS = (
    dict(name='black', kind='solid', rgb=[0, 0, 0]),
    dict(name='white', kind='solid', rgb=[255, 255, 255]),
    dict(name='green', kind='solid', rgb=[0, 255, 0]),
    dict(name='magenta', kind='solid', rgb=[255, 0, 255]),
    dict(name='checker', kind='checker', rgbA=[96, 96, 96], rgbB=[208, 208, 208], tilePixels=16),
)


def background_pixels(width: int, height: int, descriptor: dict) -> np.ndarray:
    if descriptor['kind'] == 'solid':
        return np.broadcast_to(np.array(descriptor['rgb'], np.uint8), (height, width, 3))
    yy, xx = np.indices((height, width))
    alternate = ((xx//descriptor['tilePixels'] + yy//descriptor['tilePixels']) & 1).astype(bool)
    return np.where(alternate[..., None], descriptor['rgbB'], descriptor['rgbA']).astype(np.uint8)


def composite(rgba: np.ndarray, background: np.ndarray) -> np.ndarray:
    """Straight-alpha compositing in stored RGB values; no alpha threshold."""
    if rgba.dtype != np.uint8 or rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError('Expected uint8 RGBA input')
    if background.shape != (*rgba.shape[:2], 3):
        raise ValueError('Background dimensions do not match RGBA input')
    alpha = rgba[..., 3:4].astype(np.float64)/255
    return np.rint(rgba[..., :3]*alpha + background*(1-alpha)).astype(np.uint8)


def crop_box(alpha: np.ndarray, kind: str, size: int) -> tuple[int, int, int, int]:
    """Deterministic head/left-edge locations from reference alpha, without a cutoff."""
    h, w = alpha.shape
    cw, ch = min(size, w), min(size, h)
    ys, xs = np.nonzero(alpha > 0)
    if not len(xs):
        cx, cy = w//2, h//2
    elif kind == 'head':
        cx, cy = (int(xs.min())+int(xs.max()))//2, int(ys.min())+ch//2
    elif kind == 'edge':
        # Left silhouette about one third down: include nearby empty background.
        target_y = int(ys.min())+(int(ys.max())-int(ys.min()))//3
        occupied_rows = np.unique(ys)
        cy = int(occupied_rows[np.argmin(np.abs(occupied_rows-target_y))])
        cx = int(np.flatnonzero(alpha[cy] > 0)[0])
    else:
        raise ValueError('Unknown automatic crop kind')
    x, y = max(0, min(cx-cw//2, w-cw)), max(0, min(cy-ch//2, h-ch))
    return x, y, cw, ch


def validate_crop(box: tuple[int, int, int, int], width: int, height: int):
    x, y, w, h = box
    if x < 0 or y < 0 or w < 1 or h < 1 or x+w > width or y+h > height:
        raise ValueError('Crop must fit the original display canvas')


def contact_sheet(reference: np.ndarray, actual: np.ndarray, *, source_index: int,
                  view: str, actual_label: str, box: tuple[int, int, int, int] | None = None,
                  preview_max: int = 320, reference_label: str = 'Reference') -> tuple[Image.Image, dict]:
    if reference.shape != actual.shape:
        raise ValueError('Reference/decoded display geometry differs')
    h, w = reference.shape[:2]
    x, y, cw, ch = box or (0, 0, w, h)
    validate_crop((x, y, cw, ch), w, h)
    # Cropped views are always 1:1, independent of the preview width budget.
    scale = min(1, preview_max/max(cw, ch)) if box is None else 1
    pw, ph = max(1, round(cw*scale)), max(1, round(ch*scale))
    margin, gutter, heading, row_label = 12, 12, 52, 24
    sheet = Image.new('RGB', (margin*2+pw*2+gutter, heading+len(BACKGROUNDS)*(ph+row_label)+margin), '#202020')
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default(size=14)
    draw.text((margin, 5), f'Frame {source_index} | {view} | crop {x},{y},{cw},{ch}', font=font, fill='white')
    draw.text((margin, 26), reference_label, font=font, fill='white')
    draw.text((margin+pw+gutter, 26), actual_label, font=font, fill='white')
    rows = []
    for row, descriptor in enumerate(BACKGROUNDS):
        top = heading+row*(ph+row_label)
        draw.text((margin, top), descriptor['name'], font=font, fill='white')
        background = background_pixels(w, h, descriptor)
        for column, rgba in enumerate((reference, actual)):
            # Composite before cropping so checker phase remains canvas-anchored.
            pixels = composite(rgba, background)[y:y+ch, x:x+cw]
            tile = Image.fromarray(pixels)
            if (pw, ph) != (cw, ch):
                tile = tile.resize((pw, ph), Image.Resampling.LANCZOS)
            sheet.paste(tile, (margin+column*(pw+gutter), top+row_label))
        rows.append(dict(background=descriptor['name'], top=top+row_label,
                         leftReference=margin, leftDecoded=margin+pw+gutter,
                         width=pw, height=ph))
    return sheet, dict(sourceIndex=source_index, view=view, crop=list((x, y, cw, ch)),
                       scaleX=pw/cw, scaleY=ph/ch, nativeScale=box is not None,
                       imageWidth=sheet.width, imageHeight=sheet.height, rows=rows)


def _read_rgba(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        if image.mode != 'RGBA':
            raise ValueError(f'Review requires an RGBA capture: {path.name}')
        return np.array(image)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_review(fixture: Path, output: Path, *, captures: Path | None = None,
                  frames: list[int] | None = None, crop_size=256, preview_max=320,
                  extra_crops: list[tuple[int, int, int, int]] | None = None) -> dict:
    if crop_size < 1 or preview_max < 1:
        raise ValueError('Crop/preview sizes must be positive')
    actual_dir, prefix = (captures, 'hardware') if captures is not None else (fixture, 'decoded')
    found = {int(path.stem.split('-')[1]): path for path in actual_dir.glob(f'{prefix}-*.png')}
    indices = sorted(found) if frames is None else sorted(set(frames))
    if not indices:
        raise ValueError('No decoded/hardware captures selected')
    pairs = []
    for index in indices:
        reference_path = fixture/f'reference-{index:06d}.png'
        if index < 0 or index not in found or not reference_path.is_file():
            raise ValueError(f'No complete reference/decoded pair for sourceIndex {index}')
        pairs.append((index, reference_path, found[index]))
    output.mkdir(parents=True, exist_ok=False)
    report = dict(schema='amadeus.frame-video-visual-review.v1',
                  decodedKind='hardware-rgba-capture' if captures is not None else 'software-rgba-roundtrip',
                  referenceKind='fixture-reference-png', backgrounds=list(BACKGROUNDS), frames=[], artifacts=[],
                  limitation='Capture samples only; PNG compositing uses stored RGB values. Native crops preserve 1:1 pixels. These artifacts make no acceptance or motion-stability decision.')
    actual_label = 'Hardware decode' if captures is not None else 'Software decode'
    for index, reference_path, actual_path in pairs:
        reference, actual = _read_rgba(reference_path), _read_rgba(actual_path)
        if reference.shape != actual.shape:
            raise ValueError(f'Display geometry differs at sourceIndex {index}')
        h, w = reference.shape[:2]
        frame = dict(sourceIndex=index, width=w, height=h,
                     reference=dict(path=str(reference_path.resolve()), sha256=_hash(reference_path)),
                     decoded=dict(path=str(actual_path.resolve()), sha256=_hash(actual_path)))
        report['frames'].append(frame)
        views = [('full', None), ('head-1to1', crop_box(reference[..., 3], 'head', crop_size)),
                 ('edge-1to1', crop_box(reference[..., 3], 'edge', crop_size))]
        views += [(f'custom-{i}-1to1', box) for i, box in enumerate(extra_crops or [])]
        for view, box in views:
            sheet, artifact = contact_sheet(reference, actual, source_index=index,
                                            view=view, actual_label=actual_label, box=box,
                                            preview_max=preview_max)
            filename = f'frame-{index:06d}-{view}.png'
            sheet.save(output/filename)
            report['artifacts'].append(dict(file=filename, sha256=_hash(output/filename), **artifact))
    (output/'review.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def parse_crop(value: str) -> tuple[int, int, int, int]:
    try:
        numbers = tuple(int(number) for number in value.split(','))
        if len(numbers) != 4:
            raise ValueError
        return numbers
    except ValueError as error:
        raise argparse.ArgumentTypeError('Crop must be x,y,width,height') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--captures', type=Path, help='Optional directory of hardware-NNNNNN.png')
    parser.add_argument('--frames', help='Comma-separated sourceIndex values; default all captures')
    parser.add_argument('--crop-size', type=int, default=256)
    parser.add_argument('--preview-max', type=int, default=320)
    parser.add_argument('--crop', type=parse_crop, action='append', help='Additional 1:1 x,y,width,height region')
    args = parser.parse_args()
    frames = None if args.frames is None else [int(value) for value in args.frames.split(',')]
    report = create_review(args.fixture, args.output, captures=args.captures, frames=frames,
                           crop_size=args.crop_size, preview_max=args.preview_max, extra_crops=args.crop)
    print(json.dumps(dict(frames=[item['sourceIndex'] for item in report['frames']],
                         artifacts=[item['file'] for item in report['artifacts']],
                         metadata=str((args.output/'review.json').resolve()))), flush=True)


if __name__ == '__main__':
    main()
