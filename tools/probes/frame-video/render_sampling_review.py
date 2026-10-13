"""Render a repeatable motion review from real source pixels and renderer traces.

This is diagnostic media. It does not modify or re-encode product assets.
"""
import argparse
from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def sha256(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--selections', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = json.loads((args.source / 'source.json').read_text(encoding='utf-8'))
    trace = json.loads(args.selections.read_text(encoding='utf-8'))
    if source['clip'] != trace['clip'] or source['frames'] != trace['count'] or source['frameIntervalMs'] != trace['intervalMs']:
        raise ValueError('Source and renderer trace differ')
    raw_path = args.source / 'source.rgba'
    digest = sha256(raw_path)
    if digest != source['sourceSha256']:
        raise ValueError('RGBA source hash differs from verified reference')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required')
    w, h, count = source['width'], source['height'], source['frames']
    raw = np.memmap(raw_path, dtype=np.uint8, mode='r', shape=(count, h, w, 4))
    args.output.mkdir(parents=True, exist_ok=False)
    scenario = trace['scenarios']['jitterWithin20ms']
    variants = [('off', 'Original / sampling OFF'), ('legacyFloor', 'Previous floor sampling'), ('on', 'Nearest sampling')]
    header_h, canvas_w = 64, w * len(variants)
    canvas_h = (h + header_h + 1) // 2 * 2
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 26)
    yy, xx = np.indices((h, w))
    grey = np.where((xx // 24 + yy // 24) % 2, 212, 236).astype(np.float32)[..., None]
    cache = {}
    def frame(index):
        if index not in cache:
            rgba = raw[index].astype(np.float32)
            a = rgba[..., 3:4] / 255
            cache[index] = Image.fromarray(np.rint(rgba[..., :3] * a + grey * (1-a)).clip(0,255).astype(np.uint8))
        return cache[index]
    output = args.output / 'sampling-motion.mp4'
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
        '-s', f'{canvas_w}x{canvas_h}', '-r', '60', '-i', '-', '-an', '-c:v', 'libx264', '-crf', '12',
        '-preset', 'fast', '-threads', '2', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]
    timeline = []
    for tick in range(45):
        at = tick * 1000 / 60
        selected = {}
        for key, _ in variants:
            rows = scenario[key]['rows']
            selected[key] = rows[max(0, bisect_right([row['timeMs'] for row in rows], at) - 1)]['displayed']
        timeline.append(selected)
    equal_ticks = sum(row['off'] == row['on'] for row in timeline)
    with (args.output / 'ffmpeg.stderr.log').open('wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log)
        try:
            for repeat in range(6):
                for tick, indices in enumerate(timeline):
                    canvas = Image.new('RGB', (canvas_w, canvas_h), (245,245,245))
                    draw = ImageDraw.Draw(canvas)
                    for column, (key, title) in enumerate(variants):
                        canvas.paste(frame(indices[key]), (column * w, header_h))
                        draw.text((column*w+12,6), title, font=font, fill=(20,20,20))
                        draw.text((column*w+12,34), f'Source {indices[key]:03d}   /   {tick/60:.3f}s', font=font, fill=(60,60,60))
                    process.stdin.write(canvas.tobytes())
                    if repeat == 0 and tick == 10:
                        canvas.save(args.output / 'comparison.png')
        finally:
            process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('Motion review encoding failed; see ffmpeg.stderr.log')
    report = dict(schema='amadeus.sampling-motion-review.v1', sourceSha256=digest,
        selectionsSha256=sha256(args.selections), videoSha256=sha256(output), width=canvas_w, height=canvas_h,
        fps=60, frames=270, durationSeconds=4.5, scenario='jitterWithin20ms', repetitions=6,
        originalAndNearestIdenticalSourceTicks=equal_ticks, ticksPerRepetition=len(timeline), timeline=timeline,
        limitations=['Synthetic presentation schedule with all textures ready; not a recording of actual GPU timing.',
            'Composite uses the same source PNG RGBA pixels on a checkerboard. MP4 encoding is for viewing only.',
            'Identity equality proves equality for this schedule, not every possible interruption or stall.'])
    (args.output / 'review.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key != 'timeline'}))


if __name__ == '__main__':
    main()
