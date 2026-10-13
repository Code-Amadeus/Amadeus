"""Measure hardware Pixi output against original PNG reference captures.

Readbacks occur outside timed playback. SSIM and PSNR use the original visible
region; transparent-background leakage is reported separately, not diluted by
the large empty canvas. No alpha thresholding is applied.
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def compare(reference, actual):
    if reference.shape != actual.shape:
        raise ValueError('Output dimensions changed')
    a, b = reference.astype(np.float32), actual.astype(np.float32)
    error = b[...,3]-a[...,3]
    zero, opaque, edge = a[...,3] == 0, a[...,3] == 255, (a[...,3] > 0) & (a[...,3] < 255)
    visible = a[...,3] > 0
    square, ssim = [], []
    for background in [0,255]:
        x = a[...,:3]*(a[...,3:4]/255)+background*(1-a[...,3:4]/255)
        y = b[...,:3]*(b[...,3:4]/255)+background*(1-b[...,3:4]/255)
        square.append(float(((x-y)**2)[visible].mean()))
        x = cv2.cvtColor(x,cv2.COLOR_RGB2GRAY); y = cv2.cvtColor(y,cv2.COLOR_RGB2GRAY)
        blur = lambda value: cv2.GaussianBlur(value,(11,11),1.5)
        mx, my = blur(x), blur(y)
        vx, vy, cov = blur(x*x)-mx*mx, blur(y*y)-my*my, blur(x*y)-mx*my
        c1, c2 = (0.01*255)**2, (0.03*255)**2
        score = ((2*mx*my+c1)*(2*cov+c2))/((mx*mx+my*my+c1)*(vx+vy+c2))
        ssim.append(float(score[visible].mean()))
    mse = sum(square)/2
    return dict(alphaMAE=float(np.abs(error).mean()), alphaMaxAbsError=float(np.abs(error).max()),
                zeroLeakFraction=float((b[...,3][zero] > 0).mean()) if zero.any() else 0,
                zeroMax=float(b[...,3][zero].max(initial=0)),
                opaqueLossFraction=float((b[...,3][opaque] < 255).mean()) if opaque.any() else 0,
                edgeAlphaMAE=float(np.abs(error[edge]).mean()) if edge.any() else 0,
                visibleCompositePSNR=None if mse == 0 else float(10*np.log10(255**2/mse)),
                visibleCompositeLumaSSIM=sum(ssim)/2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,required=True)
    parser.add_argument('--captures',type=Path,required=True)
    args = parser.parse_args()
    rows = []
    for capture in sorted(args.captures.glob('hardware-*.png')):
        index = int(capture.stem.split('-')[1])
        reference = np.asarray(Image.open(args.fixture/f'reference-{index:06d}.png').convert('RGBA'))
        actual = np.asarray(Image.open(capture).convert('RGBA'))
        rows.append(dict(sourceIndex=index, **compare(reference,actual)))
    if not rows:
        raise ValueError('No hardware captures found')
    result = dict(frames=rows, limitation='Diagnostic capture samples only; original PNG reference may differ from the installed crop/color variant.')
    target = args.captures/'capture-quality.json'
    with target.open('x',encoding='utf-8') as handle:
        json.dump(result,handle,indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
