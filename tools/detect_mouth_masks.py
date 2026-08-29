# -*- coding: utf-8 -*-
from __future__ import annotations
"""
tools/detect_mouth_masks.py
===========================
Detect the mouth region for each expression and compute the per-frame
mouth openness curve for loop animations.

The output config/mouth_masks.json is consumed by the PixiJS mouth-sync path.

Usage:
    python tools/detect_mouth_masks.py [--debug]

Dependencies:
    pip install opencv-python numpy

Output format (config/mouth_masks.json):
{
  "normal": {
    "cx": 12.0,          # Mouth center offset from the sprite center
    "cy": 85.0,
    "width": 48.0,       # Detected mouth width
    "height": 22.0,      # Detected mouth height at maximum openness
    "curve": -0.12,      # Upper-lip curve: positive=smile, negative=frown
    "frames": [          # Per-frame openness: 0=closed, 1=max open
      0.02, 0.15, 0.41, ...
    ]
  },
  ...
}
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = ROOT / "render" / "assets" / "images"
OUTPUT_PATH = ROOT / "config" / "mouth_masks.json"

# Vertical mouth search range relative to the non-transparent sprite bounds.
# Half-body sprites: the head is roughly 0-40%, and the mouth is around 30-42%.
MOUTH_Y_RANGE = (0.28, 0.44)

# Horizontal mouth search range relative to the non-transparent sprite bounds.
# Restrict to the center area to avoid antialiasing noise from long side hair.
MOUTH_X_CENTER_RATIO = 0.30   # Only inspect this central width ratio.

# Pixel-difference threshold for the change map.
DIFF_THRESHOLD = 18

# Morphological cleanup kernel size.
MORPH_KERNEL = 5

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def load_frames(paths: list[Path]) -> list[np.ndarray]:
    """Load RGBA frames, skipping unreadable files."""
    frames = []
    for p in paths:
        img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
        elif img.shape[2] == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        frames.append(img)
    return frames


def sprite_bbox(frame: np.ndarray):
    """Return the non-transparent pixel bounding box as (y0, y1, x0, x1)."""
    alpha = frame[:, :, 3]
    rows = np.any(alpha > 10, axis=1)
    cols = np.any(alpha > 10, axis=0)
    if not rows.any():
        return None
    y0, y1 = np.where(rows)[0][[0, -1]]
    x0, x1 = np.where(cols)[0][[0, -1]]
    return int(y0), int(y1), int(x0), int(x1)


def compute_mouth_roi(frames: list[np.ndarray], binarize_thresh: float = 0.25):
    """
    Find the mouth region on the lower face using inter-frame differences.
    Returns (roi_mask, cx, cy, width, height, curve), with coordinates centered
    on the sprite origin.

    Strategy:
    - For <= 6 frames, diff all frames against the reference frame because
      subtle static expressions need a lower threshold.
    - For longer clips, compare only the first MAX_REF_FRAMES against frames[0]
      so large hair/clothing motion does not drown out the mouth signal.
    - Apply a hard size constraint: mouth width <= sprite_width * 0.18.
    """
    h, w = frames[0].shape[:2]

    # Find the overall non-transparent sprite bounds first.
    bbox = sprite_bbox(frames[0])
    if bbox is None:
        return None
    sy0, sy1, sx0, sx1 = bbox
    sh = sy1 - sy0   # Actual sprite height.
    sw = sx1 - sx0   # Actual sprite width.

    # Mouth search ROI: vertical ratio range plus a centered horizontal band.
    roi_y0 = sy0 + int(sh * MOUTH_Y_RANGE[0])
    roi_y1 = sy0 + int(sh * MOUTH_Y_RANGE[1])
    cx_sprite = (sx0 + sx1) // 2
    half_x = int(sw * MOUTH_X_CENTER_RATIO / 2)
    roi_x0 = max(sx0, cx_sprite - half_x)
    roi_x1 = min(sx1, cx_sprite + half_x)

    # Build the change map.
    MAX_REF_FRAMES = 40   # Limit long-clip hair drift impact.
    n_frames = len(frames)
    use_n = min(n_frames, MAX_REF_FRAMES) if n_frames > 6 else n_frames
    ref = frames[0].astype(np.float32)
    change_map = np.zeros((h, w), dtype=np.float32)
    for i in range(1, use_n):
        diff = np.abs(frames[i].astype(np.float32) - ref)
        change_map += diff[:, :, :3].max(axis=2)

    # Keep only the mouth ROI.
    roi_change = np.zeros_like(change_map)
    roi_change[roi_y0:roi_y1, roi_x0:roi_x1] = change_map[roi_y0:roi_y1, roi_x0:roi_x1]

    if roi_change.max() < 1e-6:
        return None

    # Binarize to extract the most significant changing area.
    norm = roi_change / roi_change.max()
    binary = (norm > binarize_thresh).astype(np.uint8) * 255

    # Morphological cleanup: close holes, then remove noise.
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (MORPH_KERNEL, MORPH_KERNEL))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, k)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, k)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    # Hard mouth-width constraint. Prefer compact candidates.
    MAX_MOUTH_W = sw * 0.18
    valid = []
    for c in contours:
        if cv2.contourArea(c) < 60:
            continue
        bx, by, bw, bh = cv2.boundingRect(c)
        if bw <= MAX_MOUTH_W:
            valid.append(c)

    if not valid:
        # Width filtering removed all candidates; fall back to the narrowest contour.
        valid = [min(contours, key=lambda c: cv2.boundingRect(c)[2])]

    # Pick the largest valid candidate.
    largest = max(valid, key=cv2.contourArea)
    if cv2.contourArea(largest) < 60:
        return None

    # Fit an ellipse.
    if len(largest) >= 5:
        (ex, ey), (ea, eb), angle = cv2.fitEllipse(largest)
        mw = max(ea, eb)
        mh = min(ea, eb)
    else:
        bx, by, bw, bh = cv2.boundingRect(largest)
        ex, ey = bx + bw / 2, by + bh / 2
        mw, mh = float(bw), float(bh)

    # Clamp the final dimensions.
    mw = min(mw, MAX_MOUTH_W)
    mh = min(mh, sh * 0.12)   # Mouth height <= 12% of sprite height.

    # Estimate upper-lip curvature from the upper contour points.
    pts = largest.reshape(-1, 2)
    top_thresh = ey - mh * 0.3
    top_pts = pts[pts[:, 1] < top_thresh + 4]
    curve = 0.0
    if len(top_pts) >= 4:
        try:
            coeffs = np.polyfit(top_pts[:, 0].astype(float),
                                top_pts[:, 1].astype(float), 2)
            raw_curve = float(coeffs[0]) * (mw ** 2) / max(mh, 1.0)
            curve = float(np.clip(raw_curve, -1.5, 1.5))
        except Exception:
            pass

    # Convert to a sprite-centered coordinate system.
    cx_c = ex - w / 2
    cy_c = ey - h / 2

    roi_mask = binary
    return roi_mask, float(cx_c), float(cy_c), float(mw), float(mh), float(curve)


def compute_frame_openness(frames: list[np.ndarray], roi_mask: np.ndarray) -> list[float]:
    """
    Compute each frame's ROI openness relative to a reference frame.
    Values are normalized to 0=closed and 1=max open. The reference is the
    frame with the smallest ROI variation, i.e. the most closed-looking frame.
    """
    h, w = frames[0].shape[:2]
    mask_3d = roi_mask.astype(bool)

    # Compute the RGB mean inside the ROI for each frame.
    def roi_mean(frame):
        rgb = frame[:, :, :3].astype(np.float32)
        return rgb[mask_3d].mean(axis=0) if mask_3d.any() else np.zeros(3)

    means = [roi_mean(f) for f in frames]

    # Use the frame closest to the median ROI color as the closed reference.
    mean_arr = np.array(means)          # (N, 3)
    median_val = np.median(mean_arr, axis=0)
    dists_to_median = np.linalg.norm(mean_arr - median_val, axis=1)
    ref_idx = int(np.argmin(dists_to_median))
    ref_mean = means[ref_idx]

    # L2 distance from the reference frame is the raw openness value.
    raw = np.array([np.linalg.norm(m - ref_mean) for m in means])
    max_val = raw.max()
    if max_val < 1e-6:
        return [0.0] * len(frames)

    openness = (raw / max_val).tolist()
    return openness


# ──────────────────────────────────────────────
# Main detection logic
# ──────────────────────────────────────────────

def collect_frame_paths(expr_dir: Path) -> list[Path]:
    """
    Collect frame paths by priority:
    1. loop/ subdirectory
    2. in/ subdirectory when loop/ is absent
    3. frames directly under the expression directory

    Only collect kurisu_*.png frames used by the renderer. This avoids treating
    CRS_* source images or helper files in the same directory as mouth frames.
    """
    loop_dir = expr_dir / "loop"
    in_dir = expr_dir / "in"

    if loop_dir.exists():
        paths = sorted(loop_dir.glob("kurisu_*.png"))
        if paths:
            return paths

    if in_dir.exists():
        paths = sorted(in_dir.glob("kurisu_*.png"))
        if paths:
            return paths

    paths = sorted(expr_dir.glob("kurisu_*.png"))
    return [p for p in paths if not p.name.startswith(".")]


def process_expression(expr_name: str, expr_dir: Path) -> dict | None:
    frame_paths = collect_frame_paths(expr_dir)
    if len(frame_paths) < 2:
        print(f"  [{expr_name}] not enough frames ({len(frame_paths)}), skipped")
        return None

    print(f"  [{expr_name}] loading {len(frame_paths)} frames...")
    frames = load_frames(frame_paths)
    if len(frames) < 2:
        print(f"  [{expr_name}] failed to read image frames")
        return None

    result = compute_mouth_roi(frames, binarize_thresh=0.25)
    if result is None:
        # Retry with a lower threshold for subtle 3-frame static expressions.
        result = compute_mouth_roi(frames, binarize_thresh=0.06)
    if result is None:
        print(f"  [{expr_name}] mouth detection failed (insufficient change or no contour)")
        return None

    roi_mask, cx, cy, width, height, curve = result
    openness = compute_frame_openness(frames, roi_mask)

    print(f"  [{expr_name}] cx={cx:.1f} cy={cy:.1f} w={width:.1f} h={height:.1f} "
          f"curve={curve:.3f} | openness range {min(openness):.2f}~{max(openness):.2f}")

    # Save frame names for renderer-side loading.
    frame_names = [p.name for p in frame_paths]

    openness_arr = np.array(openness)
    closed_frame_idx = int(np.argmin(openness_arr))
    open_frame_idx = int(np.argmax(openness_arr))

    return {
        "cx": round(cx, 1),
        "cy": round(cy, 1),
        "width": round(width, 1),
        "height": round(height, 1),
        "curve": round(curve, 3),
        "closed_frame_idx": closed_frame_idx,
        "open_frame_idx": open_frame_idx,
        "frame_names": frame_names,
        "openness": [round(v, 4) for v in openness],
        # Internal-only data used for debug images.
        "_debug_frames": frames,
        "_debug_roi_mask": roi_mask,
    }


def _save_debug_image(expr_name, frames, roi_mask, cx, cy, width, height):
    """Visualize the detection result under tools/debug_mouth/."""
    debug_dir = Path(__file__).parent / "debug_mouth"
    debug_dir.mkdir(exist_ok=True)

    # Draw on the first frame.
    vis = frames[0].copy()
    h, w = vis.shape[:2]
    cx_abs = cx + w / 2
    cy_abs = cy + h / 2

    # Draw the ROI mask.
    mask_color = np.zeros_like(vis)
    mask_color[:, :, 2] = roi_mask   # Red channel.
    mask_color[:, :, 3] = (roi_mask > 0).astype(np.uint8) * 120
    vis = cv2.addWeighted(vis, 1.0, mask_color, 0.5, 0)

    # Draw the fitted ellipse.
    cv2.ellipse(vis,
                (int(cx_abs), int(cy_abs)),
                (int(width / 2), int(height / 2)),
                0, 0, 360,
                (0, 255, 0, 255), 2)

    out_path = debug_dir / f"{expr_name}_mouth.png"
    cv2.imwrite(str(out_path), vis)
    print(f"  [{expr_name}] debug image saved: {out_path}")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Detect sprite mouth regions automatically")
    parser.add_argument("--debug", action="store_true", help="Save debug visualization images")
    parser.add_argument("--expr", nargs="*", help="Process only the named expressions (default: all)")
    args = parser.parse_args()

    if not IMAGES_DIR.exists():
        print(f"Error: image directory not found: {IMAGES_DIR}")
        sys.exit(1)

    # Enumerate expression directories, excluding files/background assets.
    expr_dirs = sorted([
        d for d in IMAGES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ])

    if args.expr:
        expr_dirs = [d for d in expr_dirs if d.name in args.expr]
        if not expr_dirs:
            print(f"Requested expressions not found: {args.expr}")
            sys.exit(1)

    print(f"Found {len(expr_dirs)} expression directories")
    print(f"Image root: {IMAGES_DIR}")

    results = {}
    for expr_dir in expr_dirs:
        data = process_expression(expr_dir.name, expr_dir)
        if data:
            results[expr_dir.name] = data

    # ── Same-size median correction ───────────────────────────────────
    # The same character should have roughly consistent mouth cx/cy values
    # across expressions when the image size is the same. Group by cy range,
    # compute the group median, and replace outliers over 35px away.
    # For the large-image group, use the most negative/highest cy as the
    # reference so collar detections do not pollute the median.
    _median = lambda arr: sorted(arr)[len(arr) // 2]
    _ref_cy = lambda arr: min(arr)   # Most negative = highest image position.

    for threshold_cy, label in [(-120, "large"), (-50, "small")]:
        group = {k: v for k, v in results.items()
                 if (v["cy"] < threshold_cy if label == "large" else v["cy"] >= threshold_cy)}
        if len(group) < 2:
            continue
        med_cx = _median([v["cx"] for v in group.values()])
        # Use the highest mouth position for large images to avoid collar hits.
        med_cy = (_ref_cy([v["cy"] for v in group.values()])
                  if label == "large" else _median([v["cy"] for v in group.values()]))
        for name, data in group.items():
            if abs(data["cx"] - med_cx) > 35 or abs(data["cy"] - med_cy) > 35:
                print(f"  [{name}] mouth position outlier cx={data['cx']:.1f} cy={data['cy']:.1f}"
                      f" -> corrected to group median cx={med_cx:.1f} cy={med_cy:.1f}")
                data["cx"] = round(med_cx, 1)
                data["cy"] = round(med_cy, 1)
    # ─────────────────────────────────────────────────────────────────

    # Save debug images after correction so coordinates match the JSON output.
    if args.debug:
        for name, data in results.items():
            frames_dbg = data.pop("_debug_frames", None)
            roi_dbg = data.pop("_debug_roi_mask", None)
            if frames_dbg is not None and roi_dbg is not None:
                _save_debug_image(name, frames_dbg, roi_dbg,
                                  data["cx"], data["cy"], data["width"], data["height"])
    else:
        for data in results.values():
            data.pop("_debug_frames", None)
            data.pop("_debug_roi_mask", None)

    # Write the cleaned result without temporary debug fields.
    clean = {k: {fk: fv for fk, fv in v.items() if not fk.startswith("_")}
             for k, v in results.items()}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps({"version": 1, "expressions": clean}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"\n[OK] wrote mouth config for {len(clean)} expressions to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
