"""Offline echo-cancellation probe for AEC debug captures.

This is not the production AEC implementation. It verifies that the captured
TTS reference and microphone streams are usable by:
  1. selecting a non-silent capture,
  2. estimating reference -> microphone echo delay,
  3. aligning the reference,
  4. running a simple NLMS adaptive echo canceller,
  5. writing WAVs and metrics for inspection.

Usage:
  python tools/aec_offline_probe.py
  python tools/aec_offline_probe.py logs/aec_capture/<capture_dir>
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.linalg import solve_toeplitz
from scipy.signal import lfilter

SR = 16000


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0


def _peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def _read_f32(path: Path) -> np.ndarray:
    if not path.exists():
        return np.zeros(0, dtype=np.float32)
    return np.fromfile(path, dtype=np.float32)


def _load_capture(path: Path) -> tuple[np.ndarray, np.ndarray]:
    ref = _read_f32(path / "reference.f32")
    mic = _read_f32(path / "mic.f32")
    n = min(ref.size, mic.size)
    return ref[:n].astype(np.float32, copy=False), mic[:n].astype(np.float32, copy=False)


def _score_capture(path: Path) -> float:
    ref, mic = _load_capture(path)
    if ref.size < SR or mic.size < SR:
        return 0.0
    start_delta = _start_delta(path)
    if start_delta is None or abs(start_delta) > 0.05:
        return 0.0
    return _rms(ref) * _rms(mic) * min(ref.size, mic.size)


def _start_delta(path: Path) -> float | None:
    def first_ts(meta_path: Path) -> float | None:
        if not meta_path.exists():
            return None
        with meta_path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    return float(json.loads(line)["t"])
        return None

    ref_t = first_ts(path / "reference.jsonl")
    mic_t = first_ts(path / "mic.jsonl")
    if ref_t is None or mic_t is None:
        return None
    return mic_t - ref_t


def _find_capture(root: Path) -> Path:
    candidates = [p for p in root.glob("*") if p.is_dir()]
    scored = sorted((( _score_capture(p), p) for p in candidates), reverse=True)
    for score, path in scored:
        if score > 0:
            return path
    raise FileNotFoundError(f"No non-silent capture found under {root}")


def _estimate_delay(ref: np.ndarray, mic: np.ndarray, max_delay_ms: int = 800) -> tuple[int, float]:
    n = min(ref.size, mic.size)
    if n <= SR // 2:
        return 0, 0.0

    ref = ref[:n] - float(ref[:n].mean())
    mic = mic[:n] - float(mic[:n].mean())
    max_lag = min(int(SR * max_delay_ms / 1000), n - 2048)
    best_lag = 0
    best_corr = -1.0

    # Coarse-to-fine search keeps the script fast while preserving sample-level
    # alignment near the peak.
    coarse_step = 40
    coarse: list[tuple[float, int]] = []
    for lag in range(0, max_lag + 1, coarse_step):
        a = ref[:-lag] if lag else ref
        b = mic[lag:] if lag else mic
        if a.size < 2048:
            continue
        denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
        coarse.append((float(np.dot(a, b) / denom), lag))
    coarse.sort(reverse=True)

    for _, center in coarse[:5]:
        start = max(0, center - coarse_step)
        stop = min(max_lag, center + coarse_step)
        for lag in range(start, stop + 1):
            a = ref[:-lag] if lag else ref
            b = mic[lag:] if lag else mic
            if a.size < 2048:
                continue
            denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
            corr = float(np.dot(a, b) / denom)
            if corr > best_corr:
                best_corr = corr
                best_lag = lag

    return best_lag, best_corr


def _align_reference(ref: np.ndarray, length: int, delay_samples: int) -> np.ndarray:
    aligned = np.zeros(length, dtype=np.float32)
    if delay_samples >= length:
        return aligned
    usable = min(ref.size, length - delay_samples)
    if usable > 0:
        aligned[delay_samples : delay_samples + usable] = ref[:usable]
    return aligned


def _nlms_cancel(
    mic: np.ndarray,
    ref_aligned: np.ndarray,
    *,
    taps: int = 1024,
    mu: float = 0.18,
    leak: float = 0.9998,
) -> tuple[np.ndarray, np.ndarray]:
    n = min(mic.size, ref_aligned.size)
    mic = mic[:n].astype(np.float32, copy=False)
    ref_aligned = ref_aligned[:n].astype(np.float32, copy=False)

    taps = max(64, int(taps))
    w = np.zeros(taps, dtype=np.float32)
    xbuf = np.zeros(taps, dtype=np.float32)
    residual = np.zeros(n, dtype=np.float32)
    echo = np.zeros(n, dtype=np.float32)
    eps = 1e-6

    for i in range(n):
        xbuf[1:] = xbuf[:-1]
        xbuf[0] = ref_aligned[i]
        y = float(np.dot(w, xbuf))
        e = float(mic[i] - y)
        norm = float(np.dot(xbuf, xbuf)) + eps
        w *= leak
        w += (mu * e / norm) * xbuf
        echo[i] = y
        residual[i] = e

    return residual, echo


def _wiener_cancel(
    mic: np.ndarray,
    ref_aligned: np.ndarray,
    *,
    taps: int = 1024,
    regularization: float = 1e-4,
) -> tuple[np.ndarray, np.ndarray]:
    n = min(mic.size, ref_aligned.size)
    mic = mic[:n].astype(np.float64, copy=False)
    ref_aligned = ref_aligned[:n].astype(np.float64, copy=False)
    taps = max(64, min(int(taps), max(64, n // 4)))

    rxx = np.empty(taps, dtype=np.float64)
    p = np.empty(taps, dtype=np.float64)
    for k in range(taps):
        rxx[k] = np.dot(ref_aligned[k:], ref_aligned[: n - k])
        p[k] = np.dot(mic[k:], ref_aligned[: n - k])
    rxx[0] += regularization * max(1e-12, rxx[0])

    weights = solve_toeplitz((rxx, rxx), p, check_finite=False)
    echo = lfilter(weights, [1.0], ref_aligned).astype(np.float32)
    residual = (mic - echo).astype(np.float32)
    return residual, echo


def _float_to_i16_bytes(frame: np.ndarray) -> bytes:
    pcm = np.clip(np.asarray(frame, dtype=np.float32), -1.0, 1.0)
    return (pcm * 32767.0).astype("<i2").tobytes()


def _bytes_to_float_i16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def _webrtc_cancel(
    mic: np.ndarray,
    ref: np.ndarray,
    *,
    delay_ms: float,
    enable_ns: bool = False,
    enable_agc: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    from aec_audio_processing import AudioProcessor

    frame = SR // 100  # WebRTC APM expects 10 ms frames.
    n = min(mic.size, ref.size)
    n = (n // frame) * frame
    mic = mic[:n].astype(np.float32, copy=False)
    ref = ref[:n].astype(np.float32, copy=False)

    ap = AudioProcessor(
        enable_aec=True,
        enable_ns=bool(enable_ns),
        enable_agc=bool(enable_agc),
        enable_vad=False,
    )
    ap.set_stream_format(SR, 1, SR, 1)
    ap.set_reverse_stream_format(SR, 1)
    ap.set_stream_delay(int(round(max(0.0, delay_ms))))

    out: list[np.ndarray] = []
    for start in range(0, n, frame):
        r = ref[start : start + frame]
        m = mic[start : start + frame]
        ap.process_reverse_stream(_float_to_i16_bytes(r))
        processed = ap.process_stream(_float_to_i16_bytes(m))
        out.append(_bytes_to_float_i16(processed))

    residual = np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)
    # WebRTC does not expose its internal echo estimate, so store the reference
    # on this channel for output inspection.
    return residual, ref[: residual.size]


def _erle_db(mic: np.ndarray, residual: np.ndarray) -> float:
    p_in = float(np.mean(np.square(mic))) + 1e-12
    p_out = float(np.mean(np.square(residual))) + 1e-12
    return 10.0 * math.log10(p_in / p_out)


def _write_wav(path: Path, audio: np.ndarray) -> None:
    audio = np.asarray(audio, dtype=np.float32)
    if audio.size:
        audio = np.clip(audio, -1.0, 1.0)
    sf.write(path, audio, SR, subtype="PCM_16")


def run(
    capture_dir: Path,
    output_root: Path,
    taps: int,
    mu: float,
    method: str,
    delay_override_ms: float | None = None,
) -> dict:
    ref, mic = _load_capture(capture_dir)
    if ref.size == 0 or mic.size == 0:
        raise ValueError(f"Capture has no overlapping audio: {capture_dir}")

    delay, corr = _estimate_delay(ref, mic)
    delay_ms = delay * 1000.0 / SR if delay_override_ms is None else float(delay_override_ms)
    ref_aligned = _align_reference(ref, mic.size, delay)
    if method == "webrtc":
        residual, echo = _webrtc_cancel(mic, ref, delay_ms=delay_ms)
        mic = mic[: residual.size]
        ref_aligned = ref[: residual.size]
    elif method == "wiener":
        residual, echo = _wiener_cancel(mic, ref_aligned, taps=taps)
    elif method == "nlms":
        residual, echo = _nlms_cancel(mic, ref_aligned, taps=taps, mu=mu)
    else:
        raise ValueError(f"Unknown method: {method}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = output_root / f"{stamp}_{capture_dir.name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_wav(out_dir / "mic.wav", mic)
    _write_wav(out_dir / "reference_aligned.wav", ref_aligned)
    _write_wav(out_dir / "estimated_echo.wav", echo)
    _write_wav(out_dir / "residual.wav", residual)

    metrics = {
        "capture_dir": str(capture_dir),
        "output_dir": str(out_dir),
        "sample_rate": SR,
        "samples": int(mic.size),
        "duration_sec": mic.size / SR,
        "delay_samples": int(delay),
        "delay_ms": delay_ms,
        "delay_corr": corr,
        "taps": int(taps),
        "mu": float(mu),
        "method": method,
        "start_delta_sec": _start_delta(capture_dir),
        "reference_rms": _rms(ref),
        "mic_rms": _rms(mic),
        "residual_rms": _rms(residual),
        "mic_peak": _peak(mic),
        "residual_peak": _peak(residual),
        "erle_db": _erle_db(mic, residual),
    }
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", nargs="?", help="Capture directory. Defaults to best non-silent capture.")
    parser.add_argument("--root", default="logs/aec_capture", help="Capture root directory.")
    parser.add_argument("--out", default="logs/aec_probe", help="Output directory.")
    parser.add_argument("--taps", type=int, default=1024)
    parser.add_argument("--mu", type=float, default=0.18)
    parser.add_argument("--method", choices=["webrtc", "wiener", "nlms"], default="webrtc")
    parser.add_argument("--delay-ms", type=float, default=None, help="Override estimated stream delay.")
    args = parser.parse_args()

    capture_dir = Path(args.capture) if args.capture else _find_capture(Path(args.root))
    metrics = run(
        capture_dir,
        Path(args.out),
        taps=args.taps,
        mu=args.mu,
        method=args.method,
        delay_override_ms=args.delay_ms,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
