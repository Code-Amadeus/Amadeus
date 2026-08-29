"""Audio-domain echo guard for barge-in ASR handoff.

The guard intentionally avoids text comparison. The assistant may speak
Japanese while Qwen ASR hears translated or malformed Chinese/English, so the
reliable signal is whether the candidate mic audio is acoustically explained by
recent TTS reference PCM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from config.settings import (
    ASR_ECHO_GUARD_CORR_THRESHOLD,
    ASR_ECHO_GUARD_BARGE_IN_RAW_CORR_THRESHOLD,
    ASR_ECHO_GUARD_BARGE_IN_RESIDUAL_CORR_THRESHOLD,
    ASR_ECHO_GUARD_BARGE_IN_RATIO_THRESHOLD,
    ASR_ECHO_GUARD_BARGE_IN_STRONG_RAW_CORR_THRESHOLD,
    ASR_ECHO_GUARD_ENABLED,
    ASR_ECHO_GUARD_MIN_REF_RMS,
    ASR_ECHO_GUARD_REFERENCE_PAD_MS,
    ASR_ECHO_GUARD_RESIDUAL_CORR_THRESHOLD,
    ASR_ECHO_GUARD_RESIDUAL_RATIO_THRESHOLD,
)
from tts.aec_realtime import get_realtime_aec_processor

logger = logging.getLogger(__name__)

_SR = 16000
_FRAME = 400
_HOP = 320
_BANDS = (
    (80, 250),
    (250, 500),
    (500, 900),
    (900, 1500),
    (1500, 2500),
    (2500, 3800),
    (3800, 5600),
    (5600, 7600),
)


@dataclass(frozen=True)
class EchoGuardDecision:
    drop: bool
    reason: str
    raw_corr: float
    residual_corr: float
    residual_ratio: float
    raw_rms: float
    residual_rms: float
    ref_rms: float


def _rms(audio: np.ndarray) -> float:
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    return float(np.sqrt(np.mean(np.square(arr))) if arr.size else 0.0)


def _band_features(audio: np.ndarray) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if x.size < _FRAME:
        return np.zeros((0, len(_BANDS)), dtype=np.float32)

    freqs = np.fft.rfftfreq(_FRAME, d=1.0 / _SR)
    band_bins = []
    for low, high in _BANDS:
        mask = np.where((freqs >= low) & (freqs < high))[0]
        band_bins.append(mask if mask.size else np.array([0]))

    window = np.hanning(_FRAME).astype(np.float32)
    rows: list[np.ndarray] = []
    for start in range(0, x.size - _FRAME + 1, _HOP):
        frame = x[start : start + _FRAME] * window
        power = np.abs(np.fft.rfft(frame)) ** 2
        rows.append(np.array([np.log1p(float(np.mean(power[idx]))) for idx in band_bins], dtype=np.float32))
    if not rows:
        return np.zeros((0, len(_BANDS)), dtype=np.float32)
    return np.vstack(rows)


def _corr_segment(candidate: np.ndarray, reference: np.ndarray) -> float:
    cand = _band_features(candidate)
    ref = _band_features(reference)
    if cand.shape[0] < 3 or ref.shape[0] < 3:
        return 0.0
    if ref.shape[0] < cand.shape[0]:
        cand = cand[: ref.shape[0]]
    max_offset = max(0, ref.shape[0] - cand.shape[0])
    cand_vec = cand.reshape(-1).astype(np.float32, copy=True)
    cand_vec -= float(np.mean(cand_vec))
    cand_norm = float(np.linalg.norm(cand_vec))
    if cand_norm <= 1e-6:
        return 0.0
    best = 0.0
    for offset in range(max_offset + 1):
        seg = ref[offset : offset + cand.shape[0]].reshape(-1).astype(np.float32, copy=True)
        seg -= float(np.mean(seg))
        denom = cand_norm * float(np.linalg.norm(seg))
        if denom <= 1e-6:
            continue
        best = max(best, float(np.dot(cand_vec, seg) / denom))
    return max(0.0, min(1.0, best))


def evaluate_echo_candidate(
    *,
    raw_mic: np.ndarray,
    residual: np.ndarray,
    reference: np.ndarray,
) -> EchoGuardDecision:
    raw = np.asarray(raw_mic, dtype=np.float32).reshape(-1)
    out = np.asarray(residual, dtype=np.float32).reshape(-1)
    ref = np.asarray(reference, dtype=np.float32).reshape(-1)

    raw_rms = _rms(raw)
    residual_rms = _rms(out)
    ref_rms = _rms(ref)
    residual_ratio = residual_rms / max(raw_rms, 1e-6)

    if raw.size == 0 or out.size == 0:
        return EchoGuardDecision(False, "empty_candidate", 0.0, 0.0, residual_ratio, raw_rms, residual_rms, ref_rms)
    if ref_rms < float(ASR_ECHO_GUARD_MIN_REF_RMS):
        return EchoGuardDecision(False, "low_reference", 0.0, 0.0, residual_ratio, raw_rms, residual_rms, ref_rms)

    raw_corr = _corr_segment(raw, ref)
    residual_corr = _corr_segment(out, ref)
    drop = (
        raw_corr >= float(ASR_ECHO_GUARD_CORR_THRESHOLD)
        and residual_corr >= float(ASR_ECHO_GUARD_RESIDUAL_CORR_THRESHOLD)
        and residual_ratio <= float(ASR_ECHO_GUARD_RESIDUAL_RATIO_THRESHOLD)
    )
    reason = "echo_like" if drop else "near_end_or_uncertain"
    return EchoGuardDecision(drop, reason, raw_corr, residual_corr, residual_ratio, raw_rms, residual_rms, ref_rms)


def should_drop_handoff_candidate(
    *,
    raw_mic: np.ndarray,
    residual: np.ndarray,
    start_time: float,
    end_time: float,
) -> EchoGuardDecision:
    if not ASR_ECHO_GUARD_ENABLED:
        return EchoGuardDecision(False, "disabled", 0.0, 0.0, 0.0, _rms(raw_mic), _rms(residual), 0.0)

    reference = get_realtime_aec_processor().get_reference_window(
        start_time,
        end_time,
        pad_s=float(ASR_ECHO_GUARD_REFERENCE_PAD_MS) / 1000.0,
    )
    decision = evaluate_echo_candidate(raw_mic=raw_mic, residual=residual, reference=reference)
    logger.info(
        "[EchoGuard] drop=%s reason=%s raw_corr=%.3f residual_corr=%.3f residual_ratio=%.3f "
        "raw_rms=%.4f residual_rms=%.4f ref_rms=%.4f",
        decision.drop,
        decision.reason,
        decision.raw_corr,
        decision.residual_corr,
        decision.residual_ratio,
        decision.raw_rms,
        decision.residual_rms,
        decision.ref_rms,
    )
    return decision


def should_suppress_barge_in_candidate(
    *,
    raw_mic: np.ndarray,
    residual: np.ndarray,
    start_time: float,
    end_time: float,
) -> EchoGuardDecision:
    """Return True-ish decision when a barge-in VAD start is likely self-echo.

    This runs before interrupting playback, so it is intentionally conservative:
    suppress only when the short candidate is acoustically explained by the TTS
    reference. Real near-end speech should keep enough mismatch in the residual.
    """

    if not ASR_ECHO_GUARD_ENABLED:
        return EchoGuardDecision(False, "disabled", 0.0, 0.0, 0.0, _rms(raw_mic), _rms(residual), 0.0)

    reference = get_realtime_aec_processor().get_reference_window(start_time, end_time, pad_s=0.45)
    decision = evaluate_echo_candidate(raw_mic=raw_mic, residual=residual, reference=reference)
    suppress = False
    reason = decision.reason
    if decision.ref_rms >= float(ASR_ECHO_GUARD_MIN_REF_RMS):
        normal_echo = (
            decision.raw_corr >= float(ASR_ECHO_GUARD_BARGE_IN_RAW_CORR_THRESHOLD)
            and decision.residual_corr >= float(ASR_ECHO_GUARD_BARGE_IN_RESIDUAL_CORR_THRESHOLD)
            and decision.residual_ratio <= float(ASR_ECHO_GUARD_BARGE_IN_RATIO_THRESHOLD)
        )
        strong_raw_echo = (
            decision.raw_corr >= float(ASR_ECHO_GUARD_BARGE_IN_STRONG_RAW_CORR_THRESHOLD)
            and decision.residual_corr >= float(ASR_ECHO_GUARD_BARGE_IN_RESIDUAL_CORR_THRESHOLD) * 0.75
        )
        suppress = normal_echo or strong_raw_echo
        reason = "barge_in_self_echo" if suppress else "barge_in_near_end_or_uncertain"

    logger.info(
        "[EchoGuard:BargeIn] suppress=%s reason=%s raw_corr=%.3f residual_corr=%.3f "
        "residual_ratio=%.3f raw_rms=%.4f residual_rms=%.4f ref_rms=%.4f",
        suppress,
        reason,
        decision.raw_corr,
        decision.residual_corr,
        decision.residual_ratio,
        decision.raw_rms,
        decision.residual_rms,
        decision.ref_rms,
    )
    return EchoGuardDecision(
        suppress,
        reason,
        decision.raw_corr,
        decision.residual_corr,
        decision.residual_ratio,
        decision.raw_rms,
        decision.residual_rms,
        decision.ref_rms,
    )
