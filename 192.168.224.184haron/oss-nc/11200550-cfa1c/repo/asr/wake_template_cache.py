"""Personal wake phrase template cache.

This is a tiny speaker-local fast path for wake detection.  It stores acoustic
features from previously successful wake segments and compares future VAD
segments against them before falling back to ASR text matching.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WakeTemplateMatch:
    matched: bool
    score: float
    distance: float
    template_count: int


def _safe_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(value or "default")).strip("._")
    return key or "default"


class WakeTemplateCache:
    def __init__(
        self,
        *,
        cache_dir: str | Path,
        threshold: float,
        max_templates: int,
        min_ms: float,
        max_ms: float,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.threshold = float(threshold)
        self.max_templates = max(1, int(max_templates))
        self.min_ms = float(min_ms)
        self.max_ms = float(max_ms)
        self._templates: dict[str, list[np.ndarray]] = {}
        self._loaded_keys: set[str] = set()
        self._mel_filters: dict[tuple[int, int, int], np.ndarray] = {}

    def template_count(self, device_key: str) -> int:
        key = _safe_key(device_key)
        self._ensure_loaded(key)
        return len(self._templates.get(key, []))

    def match(self, audio: np.ndarray, sample_rate: int, *, device_key: str) -> WakeTemplateMatch:
        key = _safe_key(device_key)
        self._ensure_loaded(key)
        templates = self._templates.get(key, [])
        if not templates:
            return WakeTemplateMatch(False, 0.0, float("inf"), 0)
        features = self._extract_features(audio, sample_rate)
        if features is None:
            return WakeTemplateMatch(False, 0.0, float("inf"), len(templates))

        best_distance = min(self._dtw_distance(features, template) for template in templates)
        # Empirical mapping: short same-speaker wake clips usually land well
        # below 0.15, while unrelated speech tends to be much farther away.
        score = float(np.exp(-3.4 * best_distance))
        return WakeTemplateMatch(score >= self.threshold, score, float(best_distance), len(templates))

    def add_positive(self, audio: np.ndarray, sample_rate: int, *, device_key: str) -> int:
        key = _safe_key(device_key)
        self._ensure_loaded(key)
        features = self._extract_features(audio, sample_rate)
        if features is None:
            return len(self._templates.get(key, []))

        templates = list(self._templates.get(key, []))
        if templates:
            best_distance = min(self._dtw_distance(features, template) for template in templates)
            if best_distance < 0.025:
                logger.debug("[WakeTemplate] skip near-duplicate template key=%s distance=%.4f", key, best_distance)
                return len(templates)

        templates.append(features.astype(np.float32, copy=False))
        if len(templates) > self.max_templates:
            templates = templates[-self.max_templates :]
        self._templates[key] = templates
        self._save(key)
        return len(templates)

    def _path_for_key(self, key: str) -> Path:
        return self.cache_dir / f"{_safe_key(key)}.npz"

    def _ensure_loaded(self, key: str) -> None:
        if key in self._loaded_keys:
            return
        self._loaded_keys.add(key)
        path = self._path_for_key(key)
        if not path.exists():
            self._templates[key] = []
            return
        try:
            data = np.load(path, allow_pickle=False)
            features = data["features"].astype(np.float32, copy=False)
            lengths = data["lengths"].astype(np.int32, copy=False)
            templates = [features[i, : int(length)].copy() for i, length in enumerate(lengths)]
            self._templates[key] = templates[-self.max_templates :]
            logger.info("[WakeTemplate] loaded key=%s templates=%s", key, len(self._templates[key]))
        except Exception:
            logger.exception("[WakeTemplate] failed to load cache key=%s path=%s", key, path)
            self._templates[key] = []

    def _save(self, key: str) -> None:
        templates = self._templates.get(key, [])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for_key(key)
        if not templates:
            return
        max_len = max(template.shape[0] for template in templates)
        bins = templates[0].shape[1]
        features = np.zeros((len(templates), max_len, bins), dtype=np.float32)
        lengths = np.zeros((len(templates),), dtype=np.int32)
        for i, template in enumerate(templates):
            length = template.shape[0]
            features[i, :length, :] = template
            lengths[i] = length
        np.savez_compressed(path, features=features, lengths=lengths)

    def _extract_features(self, audio: np.ndarray, sample_rate: int) -> np.ndarray | None:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return None
        duration_ms = x.size / float(sample_rate) * 1000.0
        if duration_ms < self.min_ms or duration_ms > self.max_ms:
            return None
        x = x - float(np.mean(x))
        rms = float(np.sqrt(np.mean(np.square(x))) + 1e-8)
        if rms < 1e-5:
            return None
        x = x / max(rms, 1e-8)
        x = self._trim_silence(x)

        frame_len = max(1, int(sample_rate * 0.025))
        hop = max(1, int(sample_rate * 0.010))
        n_fft = 512
        if x.size < frame_len:
            return None
        frame_count = 1 + (x.size - frame_len) // hop
        if frame_count < 3:
            return None
        window = np.hanning(frame_len).astype(np.float32)
        frames = np.stack([x[i * hop : i * hop + frame_len] * window for i in range(frame_count)])
        spectrum = np.abs(np.fft.rfft(frames, n=n_fft, axis=1)) ** 2
        filters = self._mel_filterbank(sample_rate, n_fft, 24)
        mel = np.maximum(spectrum @ filters.T, 1e-8)
        features = np.log(mel).astype(np.float32)
        features -= np.mean(features, axis=0, keepdims=True)
        features /= np.std(features, axis=0, keepdims=True) + 1e-5
        return features

    def _trim_silence(self, x: np.ndarray) -> np.ndarray:
        frame = 256
        hop = 128
        if x.size < frame:
            return x
        rms_values = []
        starts = []
        for start in range(0, x.size - frame + 1, hop):
            chunk = x[start : start + frame]
            rms_values.append(float(np.sqrt(np.mean(np.square(chunk)))))
            starts.append(start)
        if not rms_values:
            return x
        threshold = max(0.05, float(np.percentile(rms_values, 65)) * 0.25)
        active = [i for i, value in enumerate(rms_values) if value >= threshold]
        if not active:
            return x
        start = max(0, starts[active[0]] - frame)
        end = min(x.size, starts[active[-1]] + frame * 2)
        return x[start:end]

    def _mel_filterbank(self, sample_rate: int, n_fft: int, bins: int) -> np.ndarray:
        key = (sample_rate, n_fft, bins)
        cached = self._mel_filters.get(key)
        if cached is not None:
            return cached

        def hz_to_mel(hz: np.ndarray) -> np.ndarray:
            return 2595.0 * np.log10(1.0 + hz / 700.0)

        def mel_to_hz(mel: np.ndarray) -> np.ndarray:
            return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

        f_min = 80.0
        f_max = min(7600.0, sample_rate / 2.0)
        points = mel_to_hz(np.linspace(hz_to_mel(np.array([f_min]))[0], hz_to_mel(np.array([f_max]))[0], bins + 2))
        fft_bins = np.floor((n_fft + 1) * points / sample_rate).astype(int)
        filters = np.zeros((bins, n_fft // 2 + 1), dtype=np.float32)
        for i in range(bins):
            left, center, right = fft_bins[i], fft_bins[i + 1], fft_bins[i + 2]
            center = max(center, left + 1)
            right = max(right, center + 1)
            for j in range(left, center):
                if 0 <= j < filters.shape[1]:
                    filters[i, j] = (j - left) / max(1, center - left)
            for j in range(center, right):
                if 0 <= j < filters.shape[1]:
                    filters[i, j] = (right - j) / max(1, right - center)
        self._mel_filters[key] = filters
        return filters

    def _dtw_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        n, m = a.shape[0], b.shape[0]
        if n == 0 or m == 0:
            return float("inf")
        band = max(8, abs(n - m) + int(0.25 * max(n, m)))
        prev = np.full(m + 1, np.inf, dtype=np.float32)
        curr = np.full(m + 1, np.inf, dtype=np.float32)
        prev[0] = 0.0
        for i in range(1, n + 1):
            curr.fill(np.inf)
            j_start = max(1, i - band)
            j_end = min(m, i + band)
            av = a[i - 1]
            for j in range(j_start, j_end + 1):
                bv = b[j - 1]
                denom = (float(np.linalg.norm(av)) * float(np.linalg.norm(bv))) + 1e-6
                cost = 0.5 * (1.0 - float(np.dot(av, bv)) / denom)
                curr[j] = cost + min(prev[j], curr[j - 1], prev[j - 1])
            prev, curr = curr, prev
        return float(prev[m] / max(n, m))
