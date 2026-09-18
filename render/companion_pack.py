"""Companion atlas install contract; no authoring or image-decoder dependency."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re


FORMAT = "amadeus.companion-atlas.v1"
BYTE_LIMIT = 16 * 1024 * 1024
MODES = ("idle", "speaking", "idleStatic", "speakingAlternate")


class CompanionPackError(ValueError):
    """An explicitly selected Companion pack is not usable."""


def validate_clip(spec: dict) -> tuple[int, int]:
    """Validate the renderer's timeline/budget contract and return atlas dimensions."""
    if not isinstance(spec, dict):
        raise CompanionPackError("Portrait state must be an object")
    url = spec.get("url")
    if not isinstance(url, str) or not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.webp", url):
        raise CompanionPackError("Unsafe portrait URL")
    size, columns = spec.get("size"), spec.get("columns")
    sequence, duration = spec.get("sequence"), spec.get("durationMs")
    if (type(size) is not int or not 1 <= size <= 512
        or type(columns) is not int or not 1 <= columns <= 16
        or not isinstance(sequence, list) or not 1 <= len(sequence) <= 2048
        or any(type(i) is not int or not 0 <= i < 2048 for i in sequence)
        or type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 60000):
        raise CompanionPackError("Invalid portrait timeline")
    dimensions = (columns * size, ((max(sequence) + columns) // columns) * size)
    decoded = dimensions[0] * dimensions[1] * 4
    if type(spec.get("decodedBytes")) is not int or decoded != spec["decodedBytes"] or decoded > BYTE_LIMIT:
        raise CompanionPackError("Portrait exceeds decoded memory budget")
    return dimensions


def load_companion_pack(root: Path) -> tuple[Path, ...]:
    """Return only manifest-indexed files after checking metadata and payload hashes.

    Authoring and the browser additionally decode WebP and check actual dimensions.
    Transport/status validation deliberately needs only the Python standard library.
    """
    root = Path(root).resolve()
    manifest_path = root / "manifest.json"
    try:
        if manifest_path.stat().st_size > 4 * 1024 * 1024:
            raise CompanionPackError("Companion manifest is too large")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
            raise CompanionPackError("Invalid companion atlas manifest")
        emotions = manifest.get("emotions")
        if not isinstance(emotions, dict) or "normal" not in emotions or not 1 <= len(emotions) <= 24:
            raise CompanionPackError("Companion requires normal and at most 24 expressions")
        files = {manifest_path}
        payloads: dict[Path, tuple[int, str]] = {}
        for states in emotions.values():
            if not isinstance(states, dict) or not {"idle", "speaking"} <= states.keys():
                raise CompanionPackError("Missing portrait state")
            for mode in MODES:
                if mode not in states:
                    continue
                spec = states[mode]
                validate_clip(spec)
                source = root / spec["url"]
                if source.resolve() != source or not source.is_file():
                    raise CompanionPackError(f"Missing or redirected portrait: {spec['url']}")
                size, digest = spec.get("fileBytes"), spec.get("sha256")
                if (type(size) is not int or not 0 < size <= BYTE_LIMIT
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise CompanionPackError(f"Invalid portrait file metadata: {spec['url']}")
                if source not in payloads:
                    actual_size = source.stat().st_size
                    if actual_size != size:
                        raise CompanionPackError(f"Portrait file size mismatch: {spec['url']}")
                    payloads[source] = (actual_size, hashlib.sha256(source.read_bytes()).hexdigest())
                if payloads[source][0] != size:
                    raise CompanionPackError(f"Portrait file size mismatch: {spec['url']}")
                if payloads[source][1] != digest:
                    raise CompanionPackError(f"Portrait checksum mismatch: {spec['url']}")
                files.add(source)
        return tuple(sorted(files))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionPackError(f"Companion pack could not be read: {exc}") from exc
