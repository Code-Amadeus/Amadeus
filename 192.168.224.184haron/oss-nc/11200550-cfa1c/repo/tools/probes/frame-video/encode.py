"""Probe-only side-by-side VP9 export and software round-trip measurement.

Consumes prepare.py output. Never edits a character manifest or claims a
verified production variant. Requires the existing numpy, Pillow and OpenCV.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import time

import cv2
import numpy as np
from PIL import Image


def fill_transparent(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Alpha-weighted push/pull; keep every nonzero-alpha source RGB intact."""
    color = rgb.astype(np.float32) * (alpha[..., None] / 255.0)
    weight = alpha.astype(np.float32)[..., None] / 255.0
    levels = []
    while max(color.shape[:2]) > 1:
        levels.append((color, weight))
        h, w = color.shape[:2]
        size = (max(1, (w + 1) // 2), max(1, (h + 1) // 2))
        color = cv2.resize(color, size, interpolation=cv2.INTER_AREA)
        weight = cv2.resize(weight, size, interpolation=cv2.INTER_AREA)[..., None]
    filled = color / np.maximum(weight, 1e-8)
    for color, weight in reversed(levels):
        up = cv2.resize(filled, (color.shape[1], color.shape[0]), interpolation=cv2.INTER_LINEAR)
        filled = np.where(weight > 0, color / np.maximum(weight, 1e-8), up)
    return np.where(alpha[..., None] > 0, rgb, np.clip(np.rint(filled), 0, 255)).astype(np.uint8)


def pack_frame(rgba: np.ndarray, gutter: int = 8) -> tuple[np.ndarray, dict]:
    h, w = rgba.shape[:2]
    padded_w, padded_h = (w + 1) & ~1, (h + 1) & ~1
    alpha_x = padded_w + gutter
    packed = np.zeros((padded_h, padded_w * 2 + gutter, 3), dtype=np.uint8)
    color = fill_transparent(rgba[..., :3], rgba[..., 3])
    # Extend boundary pixels into codec padding; never resize the authored frame.
    packed[:, :padded_w] = np.pad(color, ((0, padded_h-h), (0, padded_w-w), (0, 0)), mode='edge')
    alpha = np.repeat(rgba[..., 3:4], 3, axis=2)
    packed[:, alpha_x:alpha_x+padded_w] = np.pad(alpha, ((0, padded_h-h), (0, padded_w-w), (0, 0)), mode='edge')
    return packed, dict(codedWidth=packed.shape[1], codedHeight=padded_h,
                        width=w, height=h, colorX=0, alphaX=alpha_x, gutter=gutter)


def index_ivf(data: bytes, packets: list[dict], count: int) -> list[dict]:
    if len(data) < 32 or data[:4] != b'DKIF' or data[8:12] != b'VP90':
        raise ValueError('Expected VP9 IVF')
    if struct.unpack_from('<HH', data, 4) != (0, 32):
        raise ValueError('Unsupported IVF header')
    result, offset = [], 32
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError('Truncated IVF header')
        size = struct.unpack_from('<I', data, offset)[0]
        if not size or offset + 12 + size > len(data):
            raise ValueError('Truncated IVF packet')
        i = len(result)
        if i >= len(packets) or int(packets[i]['size']) != size:
            raise ValueError('ffprobe and IVF packet boundary mismatch')
        result.append(dict(sourceIndex=i, offset=offset+12, size=size, key='K' in packets[i]['flags']))
        offset += 12 + size
    if len(result) != count or len(packets) != count or struct.unpack_from('<I', data, 24)[0] != count:
        raise ValueError('Source / packet / IVF count mismatch')
    return result


def measure_roundtrip(raw, video: Path, layout: dict, ffmpeg: str, out: Path, picks: set[int]):
    w, h = layout['width'], layout['height']
    cw, ch = layout['codedWidth'], layout['codedHeight']
    # Explicit output conversion matches the declared encoded matrix and range.
    cmd = [ffmpeg, '-v', 'error', '-i', str(video), '-vf',
           'scale=in_color_matrix=bt709:in_range=tv:out_range=pc',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
    totals = dict(alphaAbs=0, alphaSq=0, alphaMax=0, zeroPixels=0, zeroLeaks=0,
                  zeroMax=0, opaquePixels=0, opaqueLoss=0, edgePixels=0, edgeAbs=0,
                  compositeSq=0, compositeCount=0, temporalAbs=0, temporalCount=0)
    previous_error = None
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=out.joinpath('decode.log').open('wb')) as proc:
        try:
            for i, source in enumerate(raw):
                payload = proc.stdout.read(cw * ch * 3)
                if len(payload) != cw * ch * 3:
                    raise RuntimeError(f'Decoder returned fewer frames at {i}')
                packed = np.frombuffer(payload, np.uint8).reshape(ch, cw, 3)
                rgb = packed[:h, :w].astype(np.float32)
                a = np.rint(packed[:h, layout['alphaX']:layout['alphaX']+w].astype(np.float32)
                            @ np.array([.2126, .7152, .0722], np.float32))
                source_a = source[..., 3].astype(np.float32)
                error = a-source_a
                absolute = np.abs(error)
                zero, opaque, edge = source_a == 0, source_a == 255, (source_a > 0) & (source_a < 255)
                totals['alphaAbs'] += float(absolute.sum()); totals['alphaSq'] += float((error*error).sum())
                totals['alphaMax'] = max(totals['alphaMax'], float(absolute.max()))
                totals['zeroPixels'] += int(zero.sum()); totals['zeroLeaks'] += int((a[zero] > 0).sum())
                totals['zeroMax'] = max(totals['zeroMax'], float(a[zero].max(initial=0)))
                totals['opaquePixels'] += int(opaque.sum()); totals['opaqueLoss'] += int((a[opaque] < 255).sum())
                totals['edgePixels'] += int(edge.sum()); totals['edgeAbs'] += float(absolute[edge].sum())
                for background in [0, 255]:
                    actual = rgb*(a[..., None]/255) + background*(1-a[..., None]/255)
                    expected = source[..., :3]*(source_a[..., None]/255) + background*(1-source_a[..., None]/255)
                    diff = actual-expected
                    totals['compositeSq'] += float((diff*diff).sum()); totals['compositeCount'] += diff.size
                if previous_error is not None:
                    totals['temporalAbs'] += float(np.abs(error-previous_error).sum())
                    totals['temporalCount'] += error.size
                previous_error = error
                if i in picks:
                    Image.fromarray(source).save(out / f'reference-{i:06d}.png')
                    Image.fromarray(np.dstack((rgb, a)).astype(np.uint8)).save(out / f'decoded-{i:06d}.png')
                if (i+1) % 100 == 0:
                    print(f'Validated {i+1}/{len(raw)}', flush=True)
            if proc.stdout.read(1):
                raise RuntimeError('Decoder returned more frames than the source')
            if proc.wait() != 0:
                raise RuntimeError('Software decode failed; see decode.log')
        except BaseException:
            proc.kill()
            raise
    mse = totals['compositeSq']/totals['compositeCount']
    return dict(frames=len(raw), alphaMAE=totals['alphaAbs']/(len(raw)*w*h),
                alphaMaxAbsError=totals['alphaMax'], zeroLeakFraction=totals['zeroLeaks']/max(1,totals['zeroPixels']),
                zeroMax=totals['zeroMax'], opaqueLossFraction=totals['opaqueLoss']/max(1,totals['opaquePixels']),
                edgeAlphaMAE=totals['edgeAbs']/max(1,totals['edgePixels']),
                alphaTemporalErrorMAE=totals['temporalAbs']/max(1,totals['temporalCount']),
                compositeBlackWhitePSNR=None if mse == 0 else float(10*np.log10(255**2/mse)),
                exactAlphaEndpoints=totals['zeroLeaks'] == 0 and totals['opaqueLoss'] == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--head-frames', type=int, default=18)
    parser.add_argument('--key-interval', type=int, default=60)
    parser.add_argument('--crf', type=int, default=18)
    parser.add_argument('--lossless', action='store_true')
    args = parser.parse_args()
    if args.lossless:
        args.crf = 0  # libvpx lossless fixes the quantizer range to 0..0.
    metadata = json.loads((args.source/'source.json').read_text(encoding='utf-8'))
    n, w, h = (metadata[k] for k in ('frames', 'width', 'height'))
    if not 0 <= args.crf <= 63 or not 0 < args.head_frames < n or args.key_interval < 1:
        parser.error('Invalid CRF, head or key interval')
    if (args.source/'source.rgba').stat().st_size != n*w*h*4:
        raise ValueError('Reference length mismatch')
    args.output.mkdir(parents=True, exist_ok=False)
    raw = np.memmap(args.source/'source.rgba', np.uint8, mode='r', shape=(n,h,w,4))
    _, layout = pack_frame(raw[0])
    keys = sorted({0, *range(args.head_frames, n, args.key_interval)})
    video = args.output/'clip.ivf'
    force = ','.join(f'{i/60:.9f}' for i in keys)
    cmd = [args.ffmpeg, '-v', 'warning', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s:v',
           f"{layout['codedWidth']}x{layout['codedHeight']}", '-r', '60', '-i', 'pipe:0',
           '-vf', 'scale=out_color_matrix=bt709:out_range=tv', '-c:v', 'libvpx-vp9', '-pix_fmt', 'yuv420p',
           '-color_range', 'tv', '-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
           '-crf', str(args.crf), '-b:v', '0', '-threads', '4', '-row-mt', '1', '-deadline', 'good',
           '-cpu-used', '4', '-auto-alt-ref', '0', '-lag-in-frames', '0', '-g', '9999',
           '-force_key_frames', force, '-fps_mode', 'passthrough', '-f', 'ivf']
    if args.lossless:
        cmd += ['-lossless', '1']
    started = time.perf_counter()
    with (args.output/'encode.log').open('wb') as log:
        with subprocess.Popen(cmd+[str(video)], stdin=subprocess.PIPE, stderr=log) as proc:
            try:
                for i, frame in enumerate(raw):
                    packed, _ = pack_frame(frame)
                    proc.stdin.write(packed.tobytes())
                    if (i+1) % 100 == 0:
                        print(f'Encoded {i+1}/{n}', flush=True)
                proc.stdin.close()
                if proc.wait() != 0:
                    raise RuntimeError('VP9 encoding failed; see encode.log')
            except BaseException:
                proc.kill()
                raise
    packets = json.loads(subprocess.check_output([args.ffprobe, '-v', 'error', '-select_streams', 'v:0',
                        '-show_packets', '-show_entries', 'packet=size,flags', '-of', 'json', str(video)]))['packets']
    encoded = video.read_bytes()
    index = index_ivf(encoded, packets, n)
    if any(not index[i]['key'] for i in keys):
        raise ValueError('Encoder did not honour required random-access keyframes')
    picks = {0, args.head_frames-1, args.head_frames, n//2, n-1}
    quality = measure_roundtrip(raw, video, layout, args.ffmpeg, args.output, picks)
    config = dict(schema='amadeus.frame-video-experiment.v1', codec='vp09.00.51.08', layout=layout,
                  frameIntervalMs=metadata['frameIntervalMs'], frames=n, headFrames=args.head_frames,
                  keyframeInterval=args.key_interval, video='clip.ivf', index=index,
                  reference=metadata, videoSha256=hashlib.sha256(encoded).hexdigest(),
                  colorSpace=dict(primaries='bt709', transfer='bt709', matrix='bt709', fullRange=False))
    (args.output/'clip.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    report = dict(quality=quality, bytes=len(encoded), elapsedSeconds=time.perf_counter()-started,
                  crf=args.crf, losslessYuv=args.lossless,
                  ffmpegVersion=subprocess.check_output([args.ffmpeg,'-version'], text=True).splitlines()[0],
                  limitation='Software RGB round trip against the recorded reference; BC7 handoff, SSIM and hardware equivalence need separate validation.')
    (args.output/'quality.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
