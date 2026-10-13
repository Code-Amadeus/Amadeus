"""Extract decoder and observed readback events without publishing raw traces."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(events):
    start = [e['ts'] for e in events if e.get('name') == 'frame-video-playback-start']
    end = [e['ts'] for e in events if e.get('name') == 'frame-video-playback-end']
    if len(start) != 1 or len(end) != 1 or start[0] >= end[0]:
        raise ValueError('Expected one complete timed-playback interval')
    names = Counter(e.get('name','') for e in events)
    readbacks = [e for e in events if any(term in e.get('name','').lower() for term in ('readback','readpixels'))]
    timed = Counter(e['name'] for e in readbacks if start[0] <= e.get('ts',0) <= end[0])
    return dict(playbackTraceSeconds=(end[0]-start[0])/1e6,
                decoderEvents={k:v for k,v in names.items() if 'D3D11VideoDecoder' in k},
                diagnosticAndPlaybackReadbacks=dict(Counter(e['name'] for e in readbacks)),
                playbackReadbacks=dict(timed),
                limitation='Counts cover the enabled Chromium trace categories; absence is not proof that every driver/internal CPU copy is instrumented.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace',type=Path)
    args = parser.parse_args()
    events = json.loads(args.trace.read_text(encoding='utf-8'))['traceEvents']
    result = summarize(events)
    with args.trace.with_name('trace-summary.json').open('x',encoding='utf-8') as output:
        json.dump(result,output,indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
