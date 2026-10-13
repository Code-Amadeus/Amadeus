"""Encode the corrected installed pack sequentially; preserve all quality evidence.

Each clip verifies production PNG/KTX2 hashes before encoding. Only this run's
verified temporary RGBA is removed; originals and prior experiments are untouched.
"""
import argparse, hashlib, json, math, shutil, subprocess, sys, time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--pack', type=Path, required=True)
p.add_argument('--ledger', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--reuse', type=Path, action='append', default=[])
a = p.parse_args()
repo = Path.cwd()
out = a.output.resolve()
out.mkdir(parents=True, exist_ok=False)
manifest_bytes = (a.pack/'runtime_manifest.json').read_bytes()
manifest = json.loads(manifest_bytes)
ledger_sha = hashlib.sha256(a.ledger.read_bytes()).hexdigest()
manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
reuse = {}
for directory in a.reuse:
    m = json.loads((directory/'clip.json').read_text(encoding='utf-8'))
    reuse[m['reference']['clip']] = directory
clips, started = [], time.time()
for label, clip in manifest['clips'].items():
    assert label.replace('_', '').isalnum()
    print(f'START {label} {len(clips)+1}/{len(manifest["clips"])}', flush=True)
    reference, video = out/(label+'-source'), out/label
    with (out/(label+'.log')).open('w', encoding='utf-8') as log:
        subprocess.run([sys.executable, 'tools/probes/frame-video/prepare.py', '--production-manifest', str(a.ledger),
            '--pack', str(a.pack), '--clip', label, '--output', str(reference)], stdout=log, stderr=subprocess.STDOUT, check=True)
        source = json.loads((reference/'source.json').read_text(encoding='utf-8'))
        old = reuse.get(label)
        head = math.ceil(306/clip['frameIntervalMs'])
        if old:
            m = json.loads((old/'clip.json').read_text(encoding='utf-8'))
            assert m['sourceSha256'] == source['sourceSha256'] and m['headFrames'] >= head
            assert m['reference']['productionLedgerSha256'] == ledger_sha
            assert m['reference']['manifestSha256'] == manifest_sha
            for stream in m['streams'].values():
                assert hashlib.sha256((old/stream['video']).read_bytes()).hexdigest() == stream['videoSha256']
            shutil.copytree(old, video)
        else:
            subprocess.run([sys.executable, 'tools/probes/frame-video/split_encode.py', '--source', str(reference),
                '--output', str(video), '--head-frames', str(head), '--key-interval', '60', '--crf', '18', '--threads', '4'],
                stdout=log, stderr=subprocess.STDOUT, check=True)
        m = json.loads((video/'clip.json').read_text(encoding='utf-8'))
        quality = json.loads((video/'quality.json').read_text(encoding='utf-8'))
        assert quality['quality']['frames'] == len(clip['frames'])
        alpha = quality['quality']['alphaRgbRed']
        assert alpha['alphaMaxAbsError'] == 0 and alpha['zeroLeaks'] == 0, alpha
        clips.append(dict(label=label, path=label+'/', frames=m['frames'], headFrames=m['headFrames'],
            frameIntervalMs=m['frameIntervalMs'], bytes=quality['bytes'], reused=bool(old),
            sourceSha256=source['sourceSha256'], quality={k:v for k,v in quality['quality'].items() if k!='perFrame'}))
        (out/'progress.json').write_text(json.dumps(dict(completed=len(clips), total=len(manifest['clips']),
            elapsedSeconds=time.time()-started, clips=clips), indent=2), encoding='utf-8')
        # Only this invocation's verified temporary RGBA is removed; preserve
        # its provenance, all encoded output, quality evidence and source PNGs.
        raw = (reference/'source.rgba').resolve()
        assert raw.parent.parent == out and raw.name == 'source.rgba' and not raw.is_symlink()
        raw.unlink()
    print(f'DONE {label} bytes={quality["bytes"]} elapsed={time.time()-started:.1f}s', flush=True)
bundle = dict(schema='amadeus.frame-video-bundle-experiment.v1', packVersion=manifest['version'],
    manifestSha256=manifest_sha, productionLedgerSha256=ledger_sha, clips=clips,
    totalFrames=sum(c['frames'] for c in clips), totalBytes=sum(c['bytes'] for c in clips), elapsedSeconds=time.time()-started)
(out/'bundle.json').write_text(json.dumps(bundle, indent=2), encoding='utf-8')
print(json.dumps({k:v for k,v in bundle.items() if k!='clips'}), flush=True)
