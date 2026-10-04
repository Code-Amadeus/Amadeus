"""Display-space quality of resolution tiers vs. today's full-res bilinear minification.

Reference: premultiplied INTER_AREA downsample of the full-res frame to the display size.
Current:   GPU-like bilinear (no mipmaps) sampling of the straight-alpha full-res texture.
Tier t:    offline premultiplied INTER_AREA downsample to t*size, stored straight,
           then GPU-like bilinear to the display size.  (BC7 re-encode error not modelled.)
Metrics are computed inside the character's dilated alpha bounds, composited over the
CRT background colour (#0b1018) and over mid-grey.
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

root = Path(sys.argv[1])
frames = json.loads((root / "frames.json").read_text())
DISPLAY_HEIGHTS = {
    "1080p@1x wallpaper (458px)": 458,
    "1440p@1x wallpaper (610px)": 610,
    "2560x1600@1.5x wallpaper (678px)": 678,
    "MacBook 2880x1864@2x wallpaper (790px)": 790,
    "4K wallpaper (915px)": 915,
}
TIERS = [0.5, 0.75]
BGS = {"crt": (0x0B, 0x10, 0x18), "grey": (128, 128, 128)}


def premul_area(rgba, size):
    f = rgba.astype(np.float32) / 255.0
    a = f[..., 3:4]
    pm = np.concatenate([f[..., :3] * a, a], axis=2)
    small = cv2.resize(pm, size, interpolation=cv2.INTER_AREA)
    return small  # premultiplied float


def unpremul_to_u8(pm):
    a = pm[..., 3:4]
    rgb = np.where(a > 1e-6, pm[..., :3] / np.maximum(a, 1e-6), 0.0)
    out = np.concatenate([rgb, a], axis=2)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def gpu_bilinear(rgba_u8, size):
    # Straight-alpha bilinear, as Pixi samples NO_PREMULTIPLIED_ALPHA textures.
    return cv2.resize(rgba_u8.astype(np.float32) / 255.0, size, interpolation=cv2.INTER_LINEAR)


def composite_straight(f, bg):
    a = f[..., 3:4]
    return f[..., :3] * a + np.array(bg, np.float32) / 255.0 * (1 - a)


def composite_premul(pm, bg):
    a = pm[..., 3:4]
    return pm[..., :3] + np.array(bg, np.float32) / 255.0 * (1 - a)


def luma(rgb):
    return (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]) * 255.0


def ssim_map(x, y):
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    blur = lambda z: cv2.GaussianBlur(z, (11, 11), 1.5)
    mx, my = blur(x), blur(y)
    sxx, syy, sxy = blur(x * x) - mx * mx, blur(y * y) - my * my, blur(x * y) - mx * my
    return ((2 * mx * my + c1) * (2 * sxy + c2)) / ((mx * mx + my * my + c1) * (sxx + syy + c2))


results = {}
for name, H in DISPLAY_HEIGHTS.items():
    acc = {}
    for fr in frames:
        w, h = fr["w"], fr["h"]
        rgba = np.fromfile(root / fr["file"], dtype=np.uint8).reshape(h, w, 4)
        s = H / 1028.0
        size = (max(1, round(w * s)), max(1, round(h * s)))
        ref_pm = premul_area(rgba, size)
        mask = cv2.dilate((ref_pm[..., 3] > 0.01).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        cands = {"current_fullres_bilinear": gpu_bilinear(rgba, size)}
        for t in TIERS:
            tsize = (round(w * t), round(h * t))
            tier_u8 = unpremul_to_u8(premul_area(rgba, tsize))
            cands[f"tier_{t}"] = gpu_bilinear(tier_u8, size)
        for bgname, bg in BGS.items():
            ref = luma(composite_premul(ref_pm, bg))
            for cname, cand in cands.items():
                y = luma(composite_straight(cand, bg))
                ssim = float(ssim_map(ref, y)[mask].mean())
                mse = float(((ref - y) ** 2)[mask].mean())
                psnr = 10 * np.log10(255 ** 2 / max(mse, 1e-9))
                key = (bgname, cname)
                acc.setdefault(key, []).append((ssim, psnr))
    results[name] = {f"{bg}/{c}": {"ssim": round(float(np.mean([v[0] for v in vals])), 4),
                                   "psnr": round(float(np.mean([v[1] for v in vals])), 2)}
                     for (bg, c), vals in acc.items()}

for name, rows in results.items():
    print(name)
    for key, val in rows.items():
        print(f"   {key:<38} SSIM {val['ssim']:.4f}  PSNR {val['psnr']:.2f} dB")
(root / "tier_quality.json").write_text(json.dumps(results, indent=1))
