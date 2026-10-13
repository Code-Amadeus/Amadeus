"""Probe-only VP9 color plus lossless full-range grayscale alpha export.

Both IVF streams contain one visible frame per sourceIndex, with common recovery
keys. Alpha bytes go directly into Y (neutral U/V), avoiding range conversion or
thresholding. This is an experiment manifest, not a production renderer contract.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import time

import numpy as np
from PIL import Image


# Reuse the original probe's transparent-color preparation and IVF validation.
_spec = importlib.util.spec_from_file_location(
    'frame_video_existing_encoder', Path(__file__).with_name('encode.py'))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)


def source_layout(width: int, height: int) -> dict:
    if width < 1 or height < 1:
        raise ValueError('Source dimensions must be positive')
    return dict(width=width, height=height, codedWidth=(width + 1) & ~1,
                codedHeight=(height + 1) & ~1)


def split_frame(rgba: np.ndarray) -> tuple[np.ndarray, bytes]:
    """Pad, never resize; preserve alpha and all nonzero-alpha source RGB."""
    if rgba.dtype != np.uint8 or rgba.ndim != 3 or rgba.shape[2] != 4:
        raise ValueError('Expected uint8 RGBA source')
    h, w = rgba.shape[:2]
    layout = source_layout(w, h)
    padding = ((0, layout['codedHeight']-h), (0, layout['codedWidth']-w))
    color = _helpers.fill_transparent(rgba[..., :3], rgba[..., 3])
    color = np.pad(color, (*padding, (0, 0)), mode='edge')
    return color, alpha_payload(rgba[..., 3])


def alpha_payload(alpha: np.ndarray) -> bytes:
    """Direct full-range YUV420 input; preparing alpha needs no color fill."""
    if alpha.dtype != np.uint8 or alpha.ndim != 2:
        raise ValueError('Expected uint8 alpha plane')
    h, w = alpha.shape
    layout = source_layout(w, h)
    alpha = np.pad(alpha, ((0, layout['codedHeight']-h),
                          (0, layout['codedWidth']-w)), mode='edge')
    # Ordinary profile-0 YUV420, with full-range alpha as luma and no chroma.
    chroma = bytes([128]) * (layout['codedWidth'] * layout['codedHeight'] // 4)
    return alpha.tobytes() + chroma + chroma


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validated_index(data: bytes, packets: list[dict], count: int,
                    layout: dict, required_keys: list[int]) -> list[dict]:
    index = _helpers.index_ivf(data, packets, count)
    if struct.unpack_from('<HH', data, 12) != (layout['codedWidth'], layout['codedHeight']):
        raise ValueError('IVF/source coded geometry mismatch')
    for entry in index:
        # No alt-ref/lookahead: IVF order and timestamps must both identify source.
        timestamp = struct.unpack_from('<Q', data, entry['offset']-8)[0]
        if timestamp != entry['sourceIndex']:
            raise ValueError('IVF timestamp/sourceIndex mismatch')
    if any(not index[i]['key'] for i in required_keys):
        raise ValueError('Encoder did not honour common recovery keyframes')
    return index


def encode_stream(raw, video: Path, layout: dict, keys: list[int], ffmpeg: str,
                  *, alpha: bool, crf: int, threads: int) -> dict:
    force = ','.join(f'{i/60:.9f}' for i in keys)
    cmd = [ffmpeg, '-v', 'warning', '-f', 'rawvideo', '-pix_fmt',
           'yuv420p' if alpha else 'rgb24', '-s:v',
           f"{layout['codedWidth']}x{layout['codedHeight']}", '-r', '60']
    if alpha:
        cmd += ['-color_range', 'pc', '-colorspace', 'bt709']
    cmd += ['-i', 'pipe:0']
    if not alpha:
        cmd += ['-vf', 'scale=out_color_matrix=bt709:out_range=tv']
    cmd += ['-c:v', 'libvpx-vp9', '-pix_fmt', 'yuv420p', '-profile:v', '0',
            '-color_range', 'pc' if alpha else 'tv', '-colorspace', 'bt709',
            '-color_primaries', 'bt709', '-color_trc', 'bt709',
            '-crf', '0' if alpha else str(crf), '-b:v', '0',
            '-threads', str(threads), '-row-mt', '1', '-deadline', 'good',
            '-cpu-used', '4', '-auto-alt-ref', '0', '-lag-in-frames', '0',
            '-g', '9999', '-force_key_frames', force, '-fps_mode', 'passthrough']
    if alpha:
        cmd += ['-lossless', '1']
    cmd += ['-f', 'ivf', str(video)]
    input_hash = hashlib.sha256()
    started = time.perf_counter()
    with video.with_suffix('.encode.log').open('wb') as log:
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=log) as proc:
            try:
                for i, frame in enumerate(raw):
                    payload = alpha_payload(frame[..., 3]) if alpha else split_frame(frame)[0].tobytes()
                    input_hash.update(payload)
                    proc.stdin.write(payload)
                    if (i+1) % 100 == 0:
                        print(f"Encoded {'alpha' if alpha else 'color'} {i+1}/{len(raw)}", flush=True)
                proc.stdin.close()
                if proc.wait() != 0:
                    raise RuntimeError(f'VP9 encode failed; see {video.with_suffix(".encode.log").name}')
            except BaseException:
                proc.kill()
                raise
    return dict(inputPixelFormat='yuv420p' if alpha else 'rgb24',
                encodedInputSha256=input_hash.hexdigest(),
                elapsedSeconds=time.perf_counter()-started, command=cmd)


def _decoder(stack: ExitStack, video: Path, ffmpeg: str, *, alpha: bool,
             luma: bool = False):
    cmd = [ffmpeg, '-v', 'error', '-i', str(video)]
    if not luma:
        cmd += ['-vf', f"scale=in_color_matrix=bt709:in_range={'pc' if alpha else 'tv'}:out_range=pc"]
    cmd += ['-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt',
            'yuv420p' if luma else 'rgb24', 'pipe:1']
    kind = 'luma' if luma else 'rgb'
    log = stack.enter_context(video.with_suffix(f'.{kind}.decode.log').open('wb'))
    proc = stack.enter_context(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log))
    # Kill before Popen.__exit__ waits if validation fails while its pipe is full.
    stack.callback(lambda: proc.kill() if proc.poll() is None else None)
    return proc


def _read_frame(proc, size: int, index: int, label: str) -> bytes:
    payload = proc.stdout.read(size)
    if len(payload) != size:
        raise RuntimeError(f'{label} decoder returned fewer frames at sourceIndex {index}')
    return payload


def _finish_decoder(proc, label: str):
    if proc.stdout.read(1):
        raise RuntimeError(f'{label} decoder returned more frames than the source')
    if proc.wait() != 0:
        raise RuntimeError(f'{label} decode failed; see its decode.log')


def alpha_totals() -> dict:
    return dict(pixels=0, abs=0, sq=0, max=0, zeroPixels=0, zeroLeaks=0, zeroMax=0,
                opaquePixels=0, opaqueLoss=0, opaqueMin=255, edgePixels=0, edgeAbs=0,
                edgeMax=0, temporalAbs=0, temporalPixels=0)


def accumulate_alpha(totals: dict, source: np.ndarray, actual: np.ndarray,
                     previous_error: np.ndarray | None = None) -> tuple[dict, np.ndarray]:
    error = actual.astype(np.int16)-source.astype(np.int16)
    absolute = np.abs(error)
    zero, opaque, edge = source == 0, source == 255, (source > 0) & (source < 255)
    item = dict(sourcePixels=source.size, alphaMAE=float(absolute.mean()),
                alphaMaxAbsError=int(absolute.max()),
                zeroPixels=int(zero.sum()), zeroLeaks=int(np.count_nonzero(actual[zero])),
                zeroMax=int(actual[zero].max(initial=0)), opaquePixels=int(opaque.sum()),
                opaqueLoss=int(np.count_nonzero(actual[opaque] != 255)),
                opaqueMin=int(actual[opaque].min(initial=255)), edgePixels=int(edge.sum()),
                edgeAlphaMAE=float(absolute[edge].mean()) if edge.any() else 0,
                edgeAlphaMaxAbsError=int(absolute[edge].max(initial=0)),
                alphaTemporalErrorMAE=None if previous_error is None else
                float(np.abs(error-previous_error).mean()))
    totals['pixels'] += source.size
    totals['abs'] += int(absolute.sum())
    totals['sq'] += int((error.astype(np.int32)**2).sum())
    totals['max'] = max(totals['max'], item['alphaMaxAbsError'])
    for field in ('zeroPixels', 'zeroLeaks', 'opaquePixels', 'opaqueLoss', 'edgePixels'):
        totals[field] += item[field]
    totals['zeroMax'] = max(totals['zeroMax'], item['zeroMax'])
    totals['opaqueMin'] = min(totals['opaqueMin'], item['opaqueMin'])
    totals['edgeAbs'] += int(absolute[edge].sum())
    totals['edgeMax'] = max(totals['edgeMax'], item['edgeAlphaMaxAbsError'])
    if previous_error is not None:
        totals['temporalAbs'] += int(np.abs(error-previous_error).sum())
        totals['temporalPixels'] += source.size
    return item, error


def summarized_alpha(totals: dict) -> dict:
    return dict(framesPixels=totals['pixels'], alphaMAE=totals['abs']/max(1, totals['pixels']),
                alphaRMSE=float(np.sqrt(totals['sq']/max(1, totals['pixels']))),
                alphaMaxAbsError=totals['max'], zeroPixels=totals['zeroPixels'],
                zeroLeaks=totals['zeroLeaks'], zeroLeakFraction=totals['zeroLeaks']/max(1, totals['zeroPixels']),
                zeroMax=totals['zeroMax'], opaquePixels=totals['opaquePixels'],
                opaqueLoss=totals['opaqueLoss'], opaqueLossFraction=totals['opaqueLoss']/max(1, totals['opaquePixels']),
                opaqueMin=totals['opaqueMin'], edgePixels=totals['edgePixels'],
                edgeAlphaMAE=totals['edgeAbs']/max(1, totals['edgePixels']),
                edgeAlphaMaxAbsError=totals['edgeMax'],
                alphaTemporalErrorMAE=totals['temporalAbs']/max(1, totals['temporalPixels']),
                exactAlphaEndpoints=totals['zeroLeaks'] == 0 and totals['opaqueLoss'] == 0,
                exactAllAlpha=totals['max'] == 0)


def psnr(square_error: float, samples: float) -> float | None:
    # JSON null denotes either no applicable pixels or exact equality; MAE/max
    # and the explicitly recorded sample counts distinguish these cases.
    return None if not samples or not square_error else float(10*np.log10(255**2*samples/square_error))


def color_metrics(source: np.ndarray, rgb: np.ndarray, alpha: np.ndarray) -> dict:
    source_alpha = source[..., 3].astype(np.float64)/255
    actual_alpha = alpha.astype(np.float64)/255
    error = rgb.astype(np.float64)-source[..., :3]
    sq = error**2
    visible, edge = source_alpha > 0, (source_alpha > 0) & (source_alpha < 1)
    result = dict(visibleColorSq=float(sq[visible].sum()), visibleColorSamples=int(visible.sum())*3,
                  edgeColorSq=float(sq[edge].sum()), edgeColorSamples=int(edge.sum())*3,
                  alphaWeightedColorSq=float((sq*source_alpha[..., None]).sum()),
                  alphaWeightedColorSamples=float(source_alpha.sum())*3)
    for background, label in ((0, 'Black'), (255, 'White')):
        actual = rgb*actual_alpha[..., None]+background*(1-actual_alpha[..., None])
        expected = source[..., :3]*source_alpha[..., None]+background*(1-source_alpha[..., None])
        composite_sq = (actual-expected)**2
        result[f'composite{label}Sq'] = float(composite_sq.sum())
        result[f'composite{label}Samples'] = int(composite_sq.size)
        result[f'visibleComposite{label}Sq'] = float(composite_sq[visible].sum())
        result[f'visibleComposite{label}Samples'] = int(visible.sum())*3
        result[f'composite{label}MaxAbsError'] = float(np.abs(actual-expected).max())
    return result


def summarized_color(totals: dict) -> dict:
    result = {}
    for name in ('visibleColor', 'edgeColor', 'alphaWeightedColor', 'compositeBlack',
                 'compositeWhite', 'visibleCompositeBlack', 'visibleCompositeWhite'):
        samples, square_error = totals[name+'Samples'], totals[name+'Sq']
        result[name+'Samples'] = samples
        result[name+'PSNR'] = psnr(square_error, samples)
        result[name+'RMSE'] = float(np.sqrt(square_error/samples)) if samples else None
    for label in ('Black', 'White'):
        result[f'composite{label}MaxAbsError'] = totals[f'composite{label}MaxAbsError']
    return result


def measure_roundtrip(raw, color_video: Path, alpha_video: Path, layout: dict,
                      ffmpeg: str, out: Path, picks: set[int]) -> dict:
    w, h, cw, ch = (layout[k] for k in ('width', 'height', 'codedWidth', 'codedHeight'))
    luma_totals, rgb_alpha_totals, color_totals = alpha_totals(), alpha_totals(), {}
    per_frame, previous_error, previous_luma_error, max_channel_difference = [], None, None, 0
    # Verify codec-domain losslessness separately from its RGB presentation.
    with ExitStack() as stack:
        luma = _decoder(stack, alpha_video, ffmpeg, alpha=True, luma=True)
        for i, source in enumerate(raw):
            payload = _read_frame(luma, cw*ch*3//2, i, 'alpha luma')
            actual = np.frombuffer(payload, np.uint8, count=cw*ch).reshape(ch, cw)[:h, :w]
            _, previous_luma_error = accumulate_alpha(
                luma_totals, source[..., 3], actual, previous_luma_error)
        _finish_decoder(luma, 'alpha luma')
    with ExitStack() as stack:
        color = _decoder(stack, color_video, ffmpeg, alpha=False)
        alpha = _decoder(stack, alpha_video, ffmpeg, alpha=True)
        for i, source in enumerate(raw):
            rgb = np.frombuffer(_read_frame(color, cw*ch*3, i, 'color'), np.uint8).reshape(ch, cw, 3)[:h, :w]
            alpha_rgb = np.frombuffer(_read_frame(alpha, cw*ch*3, i, 'alpha RGB'), np.uint8).reshape(ch, cw, 3)[:h, :w]
            channel_difference = np.ptp(alpha_rgb.astype(np.int16), axis=2)
            max_channel_difference = max(max_channel_difference, int(channel_difference.max()))
            actual_alpha = alpha_rgb[..., 0]  # Renderer contract: take the red channel verbatim.
            alpha_item, previous_error = accumulate_alpha(
                rgb_alpha_totals, source[..., 3], actual_alpha, previous_error)
            colors = color_metrics(source, rgb, actual_alpha)
            for key, value in colors.items():
                if key.endswith('MaxAbsError'):
                    color_totals[key] = max(color_totals.get(key, 0), value)
                else:
                    color_totals[key] = color_totals.get(key, 0)+value
            per_frame.append(dict(sourceIndex=i, **alpha_item, **summarized_color(colors)))
            if i in picks:
                Image.fromarray(source).save(out/f'reference-{i:06d}.png')
                Image.fromarray(np.dstack((rgb, actual_alpha))).save(out/f'decoded-{i:06d}.png')
            if (i+1) % 100 == 0:
                print(f'Validated both streams {i+1}/{len(raw)}', flush=True)
        _finish_decoder(color, 'color')
        _finish_decoder(alpha, 'alpha RGB')
    return dict(frames=len(raw), displayWidth=w, displayHeight=h,
                codedWidth=cw, codedHeight=ch, decodedFrameCountsMatch=True,
                alphaLuma=summarized_alpha(luma_totals), alphaRgbRed=summarized_alpha(rgb_alpha_totals),
                alphaRgbMaxChannelDifference=max_channel_difference,
                colorComposite=summarized_color(color_totals), perFrame=per_frame)


def export(source: Path, output: Path, *, ffmpeg='ffmpeg', ffprobe='ffprobe',
           head_frames=18, key_interval=60, crf=18, threads=4) -> dict:
    metadata = json.loads((source/'source.json').read_text(encoding='utf-8'))
    n, w, h = (metadata[k] for k in ('frames', 'width', 'height'))
    if any(type(value) is not int or value < 1 for value in (n, w, h)):
        raise ValueError('Invalid source dimensions or frame count')
    if not 0 <= crf <= 63 or not 0 < head_frames < n or key_interval < 1 or threads < 1:
        raise ValueError('Invalid CRF, head, key interval or threads')
    raw_path = source/'source.rgba'
    if raw_path.stat().st_size != n*w*h*4:
        raise ValueError('Reference length mismatch')
    source_hash = sha256_file(raw_path)
    if source_hash != metadata['sourceSha256']:
        raise ValueError('Recorded reference hash does not match encoded source')
    layout = source_layout(w, h)
    raw = np.memmap(raw_path, np.uint8, mode='r', shape=(n, h, w, 4))
    output.mkdir(parents=True, exist_ok=False)
    keys = sorted({0, *range(head_frames, n, key_interval)})
    streams, encoding = {}, {}
    started = time.perf_counter()
    for label in ('color', 'alpha'):
        video = output/f'{label}.ivf'
        alpha = label == 'alpha'
        encoding[label] = encode_stream(raw, video, layout, keys, ffmpeg,
                                        alpha=alpha, crf=crf, threads=threads)
        probed = json.loads(subprocess.check_output([
            ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_packets', '-show_streams',
            '-show_entries', 'packet=size,flags:stream=codec_name,profile,pix_fmt,width,height,color_range,color_space',
            '-of', 'json', str(video)]))
        stream = probed['streams'][0]
        if (stream['codec_name'], stream['profile'], stream['pix_fmt']) != ('vp9', 'Profile 0', 'yuv420p'):
            raise ValueError('Expected ordinary 8-bit profile-0 VP9 stream')
        if stream.get('color_range') != ('pc' if alpha else 'tv'):
            raise ValueError('Encoded stream range differs from declared decoder range')
        encoded = video.read_bytes()
        index = validated_index(encoded, probed['packets'], n, layout, keys)
        streams[label] = dict(video=video.name, codec='vp09.00.51.08',
                              codedWidth=layout['codedWidth'], codedHeight=layout['codedHeight'],
                              colorSpace=dict(primaries='bt709', transfer='bt709', matrix='bt709', fullRange=alpha),
                              videoSha256=hashlib.sha256(encoded).hexdigest(), bytes=len(encoded), index=index)
        if alpha:
            streams[label]['sampleChannel'] = 'red'
    picks = {0, head_frames-1, head_frames, n//2, n-1}
    quality = measure_roundtrip(raw, output/'color.ivf', output/'alpha.ivf', layout, ffmpeg, output, picks)
    # Both timestamp sequences were checked against sourceIndex independently.
    manifest = dict(schema='amadeus.frame-video-split-experiment.v1', layout=layout,
                    frames=n, frameIntervalMs=metadata['frameIntervalMs'], headFrames=head_frames,
                    keyframeInterval=key_interval, pairing='sourceIndex', streams=streams,
                    sourceSha256=source_hash, reference=metadata)
    (output/'clip.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    report = dict(quality=quality, bytes=sum(stream['bytes'] for stream in streams.values()),
                  streamBytes={label: stream['bytes'] for label, stream in streams.items()},
                  sourceSha256=source_hash, colorCrf=crf, alphaLosslessYuv=True,
                  encoding=encoding, elapsedSeconds=time.perf_counter()-started,
                  ffmpegVersion=subprocess.check_output([ffmpeg, '-version'], text=True).splitlines()[0],
                  limitation='Software all-frame RGB/YUV round trip only. Hardware full-range alpha, paired decoder ownership, GPU pixel alignment and BC7 handoff require separate validation.')
    (output/'quality.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    summary = {key: value for key, value in report.items() if key not in ('quality', 'encoding')}
    summary['quality'] = {key: value for key, value in quality.items() if key != 'perFrame'}
    print(json.dumps(summary), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--head-frames', type=int, default=18)
    parser.add_argument('--key-interval', type=int, default=60)
    parser.add_argument('--crf', type=int, default=18, help='Color CRF; alpha is always lossless')
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    export(args.source, args.output, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
           head_frames=args.head_frames, key_interval=args.key_interval, crf=args.crf,
           threads=args.threads)


if __name__ == '__main__':
    main()
