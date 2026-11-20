"""Generate an odd-sized alpha ramp with exact endpoints and frame identities."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    frames = np.zeros((24,127,256,4),dtype=np.uint8)
    for i,frame in enumerate(frames):
        frame[...,:3] = [160,80,200]
        frame[...,3] = np.arange(256,dtype=np.uint8)[None,:]
        frame[:24,:,3] = 0
        frame[24:48,:,3] = 255
        # Large opaque blocks identify the frame independently of timestamps.
        for bit in range(5):
            frame[24:48,bit*32:(bit+1)*32,:3] = 255 if i & (1 << bit) else 0
            # Separate alpha cells identify the alpha stream independently of
            # color. Rows 72 onward retain the complete 0..255 ramp; the zero
            # and opaque endpoint bands above remain unchanged.
            frame[48:72,bit*32:(bit+1)*32,3] = 255 if i & (1 << bit) else 0
    payload = frames.tobytes()
    (args.output/'source.rgba').write_bytes(payload)
    metadata = dict(schema='amadeus.frame-video-reference.v1',sourceKind='synthetic-alpha-ramp',
                    clip='alpha-calibration',width=256,height=127,frames=24,frameIntervalMs=17,
                    sourceSha256=hashlib.sha256(payload).hexdigest(),completeClip=True,
                    alphaIdentityMarker=True)
    (args.output/'source.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')


if __name__ == '__main__':
    main()
