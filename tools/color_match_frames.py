#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import cv2
import numpy as np


def _character_mask(bgr: np.ndarray) -> np.ndarray:
    """Ignore near-white background while keeping most character pixels."""
    maxc = bgr.max(axis=2)
    minc = bgr.min(axis=2)
    return (maxc < 245) & (minc > 4)


def _lab_stats(bgr: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    vals = lab[mask]
    if vals.size == 0:
        vals = lab.reshape(-1, 3)
    mean = vals.mean(axis=0)
    std = vals.std(axis=0)
    std = np.where(std < 1.0, 1.0, std)
    return mean, std


def _transfer_lab(
    src_bgr: np.ndarray,
    src_stats: tuple[np.ndarray, np.ndarray],
    dst_stats: tuple[np.ndarray, np.ndarray],
) -> np.ndarray:
    lab = cv2.cvtColor(src_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    src_mean, src_std = src_stats
    dst_mean, dst_std = dst_stats
    out = (lab - src_mean) * (dst_std / src_std) + dst_mean
    out = np.clip(out, 0, 255).astype(np.uint8)
    return cv2.cvtColor(out, cv2.COLOR_LAB2BGR)


def _blend_character_only(
    original: np.ndarray,
    corrected: np.ndarray,
    *,
    amount: float,
) -> np.ndarray:
    amount = float(max(0.0, min(1.0, amount)))
    if amount <= 0.0:
        return original.copy()
    mask = _character_mask(original)
    out = original.astype(np.float32)
    corr = corrected.astype(np.float32)
    out[mask] = out[mask] * (1.0 - amount) + corr[mask] * amount
    return np.clip(out, 0, 255).astype(np.uint8)


def _read_first(path: Path) -> np.ndarray:
    im = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if im is None:
        raise RuntimeError(f"failed to read image: {path}")
    return im


def match_dir_to_reference(
    src_dir: Path,
    ref_img_path: Path,
    *,
    amount: float,
    backup_dir: Path | None = None,
) -> None:
    files = sorted(src_dir.glob("*.png"))
    if not files:
        raise RuntimeError(f"no png files in {src_dir}")
    if backup_dir and not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        for fp in files:
            shutil.copy2(fp, backup_dir / fp.name)

    ref = _read_first(ref_img_path)
    ref_stats = _lab_stats(ref, _character_mask(ref))
    for fp in files:
        src = _read_first(fp)
        src_stats = _lab_stats(src, _character_mask(src))
        corrected = _transfer_lab(src, src_stats, ref_stats)
        out = _blend_character_only(src, corrected, amount=amount)
        cv2.imwrite(str(fp), out)


def ramp_match_dir(
    src_dir: Path,
    start_ref_path: Path,
    end_ref_path: Path,
    *,
    amount: float,
    backup_dir: Path | None = None,
) -> None:
    files = sorted(src_dir.glob("*.png"))
    if not files:
        raise RuntimeError(f"no png files in {src_dir}")
    if backup_dir and not backup_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        for fp in files:
            shutil.copy2(fp, backup_dir / fp.name)

    start_ref = _read_first(start_ref_path)
    end_ref = _read_first(end_ref_path)
    start_stats = _lab_stats(start_ref, _character_mask(start_ref))
    end_stats = _lab_stats(end_ref, _character_mask(end_ref))

    n = len(files)
    for idx, fp in enumerate(files):
        t = 0.0 if n <= 1 else idx / float(n - 1)
        src = _read_first(fp)
        src_stats = _lab_stats(src, _character_mask(src))
        mean = start_stats[0] * (1.0 - t) + end_stats[0] * t
        std = start_stats[1] * (1.0 - t) + end_stats[1] * t
        corrected = _transfer_lab(src, src_stats, (mean, std))
        out = _blend_character_only(src, corrected, amount=amount)
        cv2.imwrite(str(fp), out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Match frame colors to one or two reference images.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("single")
    one.add_argument("--dir", required=True)
    one.add_argument("--ref", required=True)
    one.add_argument("--amount", type=float, default=0.65)
    one.add_argument("--backup-dir", default=None)

    ramp = sub.add_parser("ramp")
    ramp.add_argument("--dir", required=True)
    ramp.add_argument("--start-ref", required=True)
    ramp.add_argument("--end-ref", required=True)
    ramp.add_argument("--amount", type=float, default=0.65)
    ramp.add_argument("--backup-dir", default=None)

    args = ap.parse_args()

    if args.cmd == "single":
        match_dir_to_reference(
            Path(args.dir),
            Path(args.ref),
            amount=args.amount,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )
    else:
        ramp_match_dir(
            Path(args.dir),
            Path(args.start_ref),
            Path(args.end_ref),
            amount=args.amount,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        )


if __name__ == "__main__":
    main()
