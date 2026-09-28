"""Refine the offline candidate: alpha-correct resize, conservative edge smoothing, gentle idle."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageChops

from build_companion_portraits import atlas, digest, stabilize, timing_indices


def crop_alpha_correct(image, size, side_ratio, y_ratio):
    w, h = image.size
    side = max(96, min(int(min(w * side_ratio, h * .39)), w, h))
    x, y = int((w-side)/2), max(0, min(h-side, int(h*y_ratio)))
    # Pillow RGBA.resize already premultiplies. Explicit RGBa avoids multiplying
    # RGB by alpha twice, as the legacy helper accidentally does.
    return image.crop((x,y,x+side,y+side)).convert('RGBa').resize((size,size), Image.Resampling.LANCZOS).convert('RGBA')


def stabilize_edges(frames):
    """Three-frame filter only on stable, partially transparent silhouette pixels.

    Opaque face/mouth/eyes and fast-moving contours stay untouched. Filter in
    premultiplied colour to avoid ghost colours from transparent source pixels.
    No wrap filtering: a bad source loop seam must remain observable.
    """
    arrays = np.stack([np.asarray(f).astype(np.float32) for f in frames])
    output = arrays.copy()
    changed = 0
    for i in range(1, len(frames)-1):
        triple = arrays[i-1:i+2]
        alpha = triple[:,:,:,3] / 255
        low, high = alpha.min(axis=0), alpha.max(axis=0)
        stable_edge = (low > .02) & (high < .98) & (high-low < .25)
        weights = np.array([.25,.5,.25],np.float32)[:,None,None]
        filtered_a = (alpha*weights).sum(axis=0)
        premult = triple[:,:,:,:3]*alpha[:,:,:,None]
        filtered_rgb = (premult*weights[:,:,:,None]).sum(axis=0)/np.maximum(filtered_a[:,:,None],1e-8)
        output[i,:,:,3][stable_edge] = filtered_a[stable_edge]*255
        output[i,:,:,:3][stable_edge] = filtered_rgb[stable_edge]
        changed += int(stable_edge.sum())
    return [Image.fromarray(np.clip(np.round(a),0,255).astype(np.uint8)) for a in output], changed


def edge_metrics(frames):
    arr = np.stack([np.asarray(f).astype(np.float32) for f in frames])
    a = arr[:,:,:,3:4]/255
    # Metrics include real edge movement; reductions alone are not visual approval.
    band = ((a > .02) & (a < .98)).any(axis=0).squeeze()
    composite = arr[:,:,:,:3]*a + np.array([11,40,46])*(1-a)
    temporal = np.abs(np.diff(composite,n=2,axis=0))
    return {'edgeTemporalSecondDifference':float(temporal[:,band].mean()) if band.any() else 0,
            'partialEdgePixels':int(band.sum())}


def unpack(spec, directory):
    image = Image.open(directory / spec['url']).convert('RGBA')
    s,c = spec['size'],spec['columns']
    return [image.crop(((i%c)*s,(i//c)*s,(i%c+1)*s,(i//c+1)*s)) for i in spec['sequence']]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vn-root',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--assets',type=Path,required=True)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--idle-label',default='idle1',choices=['idle','idle1','idle_mic_wind'])
    parser.add_argument('--output-name',default='candidate132')
    parser.add_argument('--thinking-label',default='thinking_speaking1',choices=['thinking_speaking1','thinking_speaking2'])
    args=parser.parse_args()
    dest=args.assets/args.output_name
    dest.mkdir(exist_ok=False)
    sys.path.insert(0,str(args.vn_root.resolve()))
    spec=importlib.util.spec_from_file_location('legacy_builder',args.vn_root/'vn_portrait_cache_builder.py')
    legacy=importlib.util.module_from_spec(spec);spec.loader.exec_module(legacy)
    legacy.SPRITEFORGE_WORKSPACE=args.workspace.resolve()
    from vn_portrait_assets import rounded_alpha_mask
    old=json.loads((args.vn_root/'out/vn_portrait_cache/manifest.json').read_text())
    original=json.loads((args.assets/'full132/manifest.json').read_text())
    sources=json.loads((args.assets/'source-evidence.json').read_text())
    runtime=json.loads(args.runtime.read_text())
    manifest={'format':original['format'],'emotions':{},'crop':original['crop'],
              'sourceManifestSha256':digest(args.runtime),'edgeFilter':'single-premultiply + stable-edge-only temporal 1:2:1'}
    report={}
    for emotion,states in original['emotions'].items():
        entry=manifest['emotions'][emotion]={}
        for mode in ['idle','speaking']:
            before=unpack(states[mode],args.assets/'full132')
            frames=before
            duration=states[mode]['durationMs']
            if mode=='speaking' and emotion in sources['sources']:
                evidence=sources['sources'][emotion]
                paths=sorted(Path(evidence['sourceRoot']).glob('*.png'))
                indices=evidence['fullIndices']
                # Carry forward evidence rather than quietly using changed inputs.
                assert len(paths)==evidence['sourceFrameCount']
                for index in indices:
                    assert digest(paths[index])==evidence['selectedSHA256'][str(index)]
                raw=[Image.open(paths[i]).convert('RGBA') for i in indices]
                cropped=[crop_alpha_correct(f,132,old['crop_side_ratio'],old['crop_y_ratio']) for f in raw]
                for image in cropped:
                    image.putalpha(ImageChops.multiply(image.getchannel('A'),rounded_alpha_mask(132)))
                anchors=[legacy.crop_portrait(Image.open(paths[i]),output_size=132,
                    crop_side_ratio=old['crop_side_ratio'],crop_y_ratio=old['crop_y_ratio']) for i in evidence['oldAnchorIndices']]
                target=np.median(np.stack([legacy.foreground_mean_rgb(f)[0] for f in anchors]),axis=0)
                if emotion=='sided_thinking' and args.thinking_label=='thinking_speaking2':
                    profile=json.loads((args.workspace/'spriteforge_mouth_config.json').read_text(encoding='utf-8'))['profiles'][args.thinking_label]
                    paths=[legacy.profile_frame_path(profile,i) for i in range(len(profile['frame_names']))]
                    clip=runtime['clips'][args.thinking_label]
                    assert [p.stem for p in paths]==[Path(p).stem for p in clip['frames']]
                    indices,duration=timing_indices(len(paths),clip['frameIntervalMs'],24)
                    cropped=[crop_alpha_correct(Image.open(paths[i]).convert('RGBA'),132,old['crop_side_ratio'],old['crop_y_ratio']) for i in indices]
                    for image in cropped:
                        image.putalpha(ImageChops.multiply(image.getchannel('A'),rounded_alpha_mask(132)))
                    report['thinkingVariant']={'label':args.thinking_label,'durationMs':duration,'indices':indices,
                                              'selectedSHA256':{str(i):digest(paths[i]) for i in indices},
                                              'comparisonNote':'Different source from v1; its before/after edge metrics are not a paired estimate.'}
                corrected=stabilize(cropped,target,old['luma_strength'],legacy)
                frames,changed=stabilize_edges(corrected)
                # This filter cannot blur the opaque face, eyes or mouth.
                for a,b in zip(corrected,frames):
                    aa,bb=np.asarray(a),np.asarray(b)
                    assert np.array_equal(aa[aa[:,:,3]==255],bb[aa[:,:,3]==255])
                report[emotion]={'before':edge_metrics(before),'alphaCorrect':edge_metrics(corrected),
                                 'after':edge_metrics(frames),'filteredPixelsAcrossFrames':changed,
                                 'opaquePixelsUnchangedByTemporalFilter':True}
            entry[mode]=atlas(dest,f'{emotion}/{mode}',frames,duration)
        print(emotion,flush=True)
    # Select a reviewed source variant first; preserve its pacing at 8 FPS.
    label=args.idle_label
    graph=json.loads((args.workspace/'graph_config.json').read_text())
    node=next(n for n in graph['nodes'] if n['label']==label)
    paths=sorted((args.workspace/node['root']/'loop').glob('*.png'))
    clip=runtime['clips'][label]
    assert [p.stem for p in paths]==[Path(p).stem for p in clip['frames']]
    indices,duration=timing_indices(len(paths),clip['frameIntervalMs'],8)
    frames=[crop_alpha_correct(Image.open(paths[i]).convert('RGBA'),132,old['crop_side_ratio'],old['crop_y_ratio']) for i in indices]
    for frame in frames:
        frame.putalpha(ImageChops.multiply(frame.getchannel('A'),rounded_alpha_mask(132)))
    frames=legacy.normalize_portrait_luma(frames,old['luma_strength'])
    frames,_=stabilize_edges(frames)
    # Keep a selectable static alternative; it is the exact approved cache.
    manifest['emotions']['normal']['idleStatic']=manifest['emotions']['normal']['idle']
    manifest['emotions']['normal']['idle']=atlas(dest,'normal/idle-selected',frames,duration)
    report['selectedIdle']={'label':label,'durationMs':duration,'sourceIndices':indices,
                        'selectedSHA256':{str(i):digest(paths[i]) for i in indices}}
    manifest['totalFileBytes']=sum(f.stat().st_size for f in dest.rglob('*.webp'))
    (dest/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (dest/'edge-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
