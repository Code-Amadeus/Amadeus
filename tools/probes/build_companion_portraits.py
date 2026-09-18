"""Reproducible portrait experiment; source workspaces and installed media are read-only.

The existing VN builder supplies the exact crop/alpha rules. Output consists of
lossless WebP atlases and timing, with no runtime dependency on SpriteForge/Pixi.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timing_indices(source_count, source_interval_ms, target_fps):
    duration = source_count * source_interval_ms
    count = min(source_count, math.ceil(duration * target_fps / 1000))
    return [math.floor(i * source_count / count) for i in range(count)], duration


def matched_indices(anchors, subdivisions=4):
    # Preserve every old keyframe's 170ms onset and the last frame's hold.
    # Never invent backwards/AI-interpolated frames at the segment wrap.
    return [round(a + (anchors[min(k + 1, len(anchors) - 1)] - a) * j / subdivisions)
            for k, a in enumerate(anchors) for j in range(subdivisions)]


def stabilize(frames, target, strength, builder):
    result = []
    for frame in frames:
        arr = np.asarray(frame).astype(np.float32)
        mean, _ = builder.foreground_mean_rgb(frame)
        alpha = arr[:, :, 3:4]
        rgb = arr[:, :, :3]
        corrected = rgb + (target - mean).reshape(1, 1, 3) * strength
        weight = alpha / 255.0
        result.append(Image.fromarray(np.concatenate([
            np.clip(rgb * (1 - weight) + corrected * weight, 0, 255), alpha
        ], axis=2).astype(np.uint8)))
    return result


def frame_metrics(frames):
    # Compare composited pixels on the actual portrait background, not hidden RGB.
    arrays = []
    for frame in frames:
        bg = Image.new('RGBA', frame.size, (11, 40, 46, 255))
        bg.alpha_composite(frame)
        arrays.append(np.asarray(bg.convert('RGB')).astype(np.float32))
    differences = [float(np.abs(b - a).mean()) for a, b in zip(arrays, arrays[1:])]
    return {'meanAdjacentRGBDelta': float(np.mean(differences)) if differences else 0,
            'seamRGBDelta': float(np.abs(arrays[-1] - arrays[0]).mean())}


def atlas(output, name, frames, duration):
    size = frames[0].width
    unique, lookup, sequence = [], {}, []
    for frame in frames:
        key = hashlib.sha256(frame.tobytes()).digest()
        if key not in lookup:
            lookup[key] = len(unique)
            unique.append(frame)
        sequence.append(lookup[key])
    columns = min(8, len(unique))
    rows = math.ceil(len(unique) / columns)
    sheet = Image.new('RGBA', (columns * size, rows * size))
    for i, frame in enumerate(unique):
        sheet.paste(frame, ((i % columns) * size, (i // columns) * size))
    file = output / (name + '.webp')
    file.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(file, lossless=True, method=4, exact=True)
    # Lossless round-trip is an executable guarantee, not just an encoder option.
    with Image.open(file) as check:
        assert check.convert('RGBA').tobytes() == sheet.tobytes()
    return {'url': name + '.webp', 'size': size, 'columns': columns,
            'sequence': sequence, 'durationMs': duration,
            'decodedBytes': sheet.width * sheet.height * 4,
            'fileBytes': file.stat().st_size, 'sha256': digest(file),
            **frame_metrics(frames)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vn-root', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    # A fresh output makes reruns auditable and avoids replacing an accepted cache.
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.vn_root.resolve()))
    spec = importlib.util.spec_from_file_location('vn_builder', args.vn_root / 'vn_portrait_cache_builder.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.SPRITEFORGE_WORKSPACE = args.workspace.resolve()
    cache = args.vn_root / 'out/vn_portrait_cache'
    old = json.loads((cache / 'manifest.json').read_text(encoding='utf-8'))
    runtime = json.loads(args.runtime.read_text(encoding='utf-8'))
    mouth_path = args.workspace / 'spriteforge_mouth_config.json'
    profiles = json.loads(mouth_path.read_text(encoding='utf-8'))['profiles']
    evidence = {'builderSha256': digest(args.vn_root / 'vn_portrait_cache_builder.py'),
                'cropSha256': digest(args.vn_root / 'vn_portrait_assets.py'),
                'oldManifestSha256': digest(cache / 'manifest.json'),
                'runtimeManifestSha256': digest(args.runtime),
                'mouthConfigSha256': digest(mouth_path), 'sources': {}}
    variants = {name: {'format': 'amadeus.companion-atlas.experiment.v1', 'emotions': {}}
                for name in ['matched132', 'full132', 'full264']}
    contacts = []
    for emotion, states in old['emotions'].items():
        legacy = {mode: [Image.open(cache / f).convert('RGBA') for f in files]
                  for mode, files in states.items()}
        label = builder.MOUTH_PROFILE_BY_EMOTION.get(emotion)
        profile = profiles.get(label)
        generated = {}
        if profile:
            paths = [builder.profile_frame_path(profile, i) for i in range(len(profile['frame_names']))]
            clip = runtime['clips'][label]
            # Establish identity with the KTX2 frame index before borrowing its timing.
            assert [p.stem for p in paths] == [Path(p).stem for p in clip['frames']], label
            old_paths = builder.profile_speaking_paths(profile, len(legacy['speaking']))
            anchors = [paths.index(p) for p in old_paths]
            matched = matched_indices(anchors)
            full, full_duration = timing_indices(len(paths), clip['frameIntervalMs'], 24)
            selected = sorted(set(matched + full + anchors))
            raw = {i: Image.open(paths[i]).convert('RGBA') for i in selected}
            evidence['sources'][emotion] = {
                'label': label, 'sourceRoot': str(paths[0].parent),
                'sourceFrameCount': len(paths), 'sourceIntervalMs': clip['frameIntervalMs'],
                'oldAnchorIndices': anchors, 'matchedIndices': matched,
                'fullIndices': full, 'fullDurationMs': full_duration,
                'selectedSHA256': {str(i): digest(paths[i]) for i in selected}}
            for size in (132, 264):
                portraits = {i: builder.crop_portrait(raw[i], output_size=size,
                    crop_side_ratio=old['crop_side_ratio'], crop_y_ratio=old['crop_y_ratio']) for i in selected}
                if size == 264:
                    # Scale only the rounded mask radius (legacy helper uses an 18px constant).
                    # Reuse the same normalized crop, with a proportionate high-DPI alpha mask.
                    from PIL import ImageChops
                    from vn_portrait_assets import resize_rgba_premultiplied, rounded_alpha_mask
                    for i in selected:
                        w, h = raw[i].size
                        side = max(96, min(int(min(w * old['crop_side_ratio'], h * .39)), w, h))
                        x, y = int((w-side)/2), max(0, min(h-side, int(h*old['crop_y_ratio'])))
                        image = resize_rgba_premultiplied(raw[i].crop((x,y,x+side,y+side)), (size,size))
                        image.putalpha(ImageChops.multiply(image.getchannel('A'), rounded_alpha_mask(size, 36)))
                        portraits[i] = image
                target = np.median(np.stack([builder.foreground_mean_rgb(portraits[i])[0] for i in anchors]), axis=0)
                corrected = dict(zip(selected, stabilize([portraits[i] for i in selected], target, old['luma_strength'], builder)))
                if size == 132:
                    anchor_errors = [int(np.abs(np.asarray(corrected[i]).astype(int) - np.asarray(old_frame).astype(int)).max())
                                     for i, old_frame in zip(anchors, legacy['speaking'])]
                    evidence['sources'][emotion]['oldAnchorMaxPixelError'] = anchor_errors
                    # Report stale/different legacy media rather than claiming exact identity.
                    generated['matched132'] = ([corrected[i] for i in matched], len(anchors) * 170)
                generated[f'full{size}'] = ([corrected[i] for i in full], full_duration)
            raw.clear()
        for variant, manifest in variants.items():
            variant_dir = output / variant
            entry = manifest['emotions'][emotion] = {}
            # Preserve approved closed-mouth poses; do not loop an entry transition at idle.
            entry['idle'] = atlas(variant_dir, f'{emotion}/idle', legacy['idle'], len(legacy['idle'])*170)
            frames, duration = generated.get(variant, (legacy['speaking'], len(legacy['speaking'])*170))
            entry['speaking'] = atlas(variant_dir, f'{emotion}/speaking', frames, duration)
            if variant == 'full132':
                contacts.append((emotion, legacy['speaking'][0], frames[0], frames[len(frames)//2]))
        print(json.dumps({'emotion': emotion, 'generated': {k: len(v[0]) for k,v in generated.items()}}), flush=True)
    for variant, manifest in variants.items():
        manifest['sourceManifestSha256'] = evidence['runtimeManifestSha256']
        manifest['crop'] = {k: old[k] for k in ('crop_side_ratio','crop_y_ratio','luma_strength')}
        manifest['totalFileBytes'] = sum(s['fileBytes'] for e in manifest['emotions'].values() for s in e.values())
        (output / variant / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'source-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    sheet = Image.new('RGB', (620, len(contacts)*160+40), (11,40,46))
    draw = ImageDraw.Draw(sheet)
    draw.text((180,10), 'CURRENT            SOURCE 24fps       SOURCE MID', fill='white')
    for row,(label,*frames) in enumerate(contacts):
        y = row*160+40
        draw.text((6,y+55),label,fill='white')
        for column,frame in enumerate(frames):
            sheet.paste(frame,(175+column*145,y),frame)
    sheet.save(output / 'contact.png')
    print(json.dumps({'output': str(output), 'variants': {k:v['totalFileBytes'] for k,v in variants.items()}}), flush=True)


if __name__ == '__main__':
    main()
